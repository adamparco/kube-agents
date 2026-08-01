"""Unit tests for audit_pr — the fleet-audit PR harness.

Run:
  python3 -m unittest discover -s agents/platform/skills/fleet-audit/scripts \
      -p 'test_audit_pr.py' -v

Stdlib only, matching the other agent-script tests. No gh, gcloud, or GitHub
credentials are required: the validate/render/delta layer is pure, and the two
commands that do touch the network are driven through a single recorded seam
(audit_pr.run_cmd) plus stubs for credential minting.
"""

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit_pr  # noqa: E402

AUDIT = "compliance-audit"
NOW = datetime(2026, 8, 1, 9, 30, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def make_finding(
    fid="no-network-policy",
    severity="critical",
    title="Namespace has no NetworkPolicy",
    cluster="prod-us-east",
    namespace="payments",
    obj="Namespace/payments",
    command="kubectl get networkpolicy -n payments",
    excerpt="No resources found in payments namespace.",
    impact="All pod-to-pod traffic in payments is unrestricted.",
    remediation=None,
    recommendation=None,
):
    return {
        "id": fid,
        "severity": severity,
        "title": title,
        "cluster": cluster,
        "namespace": namespace,
        "object": obj,
        "evidence": {"command": command, "excerpt": excerpt},
        "impact": impact,
        "recommendation": recommendation
        or {
            "action": "Apply a default-deny NetworkPolicy to the payments namespace.",
            "rationale": (
                "Namespace-scoped default-deny is the smallest change that closes "
                "east-west exposure; a mesh AuthorizationPolicy would only cover "
                "injected pods."
            ),
            "risk": (
                "Unlabelled cross-namespace traffic into payments breaks on apply. "
                "Check current flows first."
            ),
        },
        "remediation": remediation
        or {
            "kind": "manifest",
            "path": "clusters/prod-us-east/payments-netpol.yaml",
            "note": "Apply a default-deny NetworkPolicy.",
        },
    }


def make_doc(findings=None, audit=AUDIT, clusters=None, skipped=None):
    return {
        "audit": audit,
        "scope": {
            "clusters": clusters
            if clusters is not None
            else [
                {
                    "name": "prod-us-east",
                    "location": "us-east1",
                    "project": "acme-prod",
                },
                {
                    "name": "stage-eu",
                    "location": "europe-west1",
                    "project": "acme-stage",
                },
            ],
            "skipped": skipped if skipped is not None else [],
        },
        "findings": findings if findings is not None else [make_finding()],
    }


THREE_SEVERITIES = [
    make_finding(fid="minor-one", severity="minor", title="Minor one"),
    make_finding(fid="crit-one", severity="critical", title="Crit one"),
    make_finding(fid="major-one", severity="major", title="Major one"),
    make_finding(fid="crit-two", severity="critical", title="Crit two"),
]


class Recorder:
    """Stands in for audit_pr.run_cmd, recording every command and replying by rule.

    `failures` maps a command fragment to the return code that command should
    produce: with `check=True` it raises CalledProcessError exactly as
    subprocess would, and with `check=False` it returns the non-zero result.
    Without it every failure path in the harness is untestable, because a
    recorder that always succeeds can only ever exercise the happy path.
    """

    def __init__(self, replies=None, failures=None):
        self.calls: list[list[str]] = []
        self.replies = replies or {}
        self.failures = failures or {}

    def __call__(self, cmd, *, check=True, capture=True):
        self.calls.append(list(cmd))
        joined = " ".join(cmd)
        for key, code in self.failures.items():
            if key in joined:
                if check:
                    raise CalledProcessError(code, cmd, "", "simulated failure")
                return CompletedProcess(cmd, code, "", "simulated failure")
        for key, payload in self.replies.items():
            if key in joined:
                return CompletedProcess(cmd, 0, payload, "")
        return CompletedProcess(cmd, 0, "", "")

    def matching(self, *fragments):
        return [
            call
            for call in self.calls
            if all(fragment in " ".join(call) for fragment in fragments)
        ]

    def gh_calls(self, *path):
        """Every `gh <path...>` call, matched on argv position, not substring.

        `matching` is unsafe for a short fragment like "pr": a temp body file
        named /tmp/tmpri8dla1x.md makes `gh issue create` look like `gh pr
        create`. Anything asserting that a *pull request* was never touched
        has to go through here.
        """
        return [c for c in self.calls if c[: len(path) + 1] == ["gh", *path]]


class BaseTestCase(unittest.TestCase):
    """A temp working tree, patch bookkeeping, and captured stdout/stderr."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)
        self.out = ""
        self.err = ""

    def issue_list(self, number=42, url="https://github.com/acme/fleet/issues/42"):
        return json.dumps([{"number": number, "url": url}])

    def patch_attr(self, name, value):
        """monkeypatch.setattr(audit_pr, name, value), undone at teardown."""
        patcher = patch.object(audit_pr, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, argv):
        """Invoke the CLI, capturing stdout/stderr into self.out / self.err."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = audit_pr.main(argv)
        self.out = out.getvalue()
        self.err = err.getvalue()
        return code

    def stdout_json(self):
        return json.loads(self.out.strip())

    def write_findings(self, doc):
        path = self.tmp_path / "findings.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return str(path)

    def touch(self, relative):
        target = self.tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# remediation\n", encoding="utf-8")
        return target

    def run_finish(self, doc, argv_extra=(), audit=AUDIT):
        findings_file = self.write_findings(doc)
        return self.run_main(
            ["finish", "--audit", audit, "--findings-file", findings_file, *argv_extra]
        )

    def git_add_calls(self, recorder):
        # "add" is not at a fixed index: the harness passes git-level flags
        # (--literal-pathspecs) ahead of the subcommand.
        return [c for c in recorder.calls if c[0] == "git" and "add" in c[:3]]


class HarnessTestCase(BaseTestCase):
    """Wires audit_pr's I/O seam to a recorder and pins the repo root to the temp tree."""

    def setUp(self):
        super().setUp()
        self.harness = Recorder()
        self.patch_attr("run_cmd", self.harness)
        self.patch_attr("refresh_credentials", lambda: None)
        self.patch_attr("resolve_repo", lambda: "acme/fleet")
        self.patch_attr("repo_root", lambda: self.tmp_path)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


class TestRenderBody(unittest.TestCase):
    def test_renders_scope_findings_and_footer(self):
        doc = make_doc()
        body = audit_pr.render_issue_body(doc, generated_at=NOW)
        self.assertIn("This issue is the ledger for the `compliance-audit` audit", body)
        self.assertIn("## Scope", body)
        self.assertIn("| `prod-us-east` | us-east1 | `acme-prod` |", body)
        self.assertIn("## Findings", body)
        self.assertIn("### Critical (1)", body)
        self.assertIn("kubectl get networkpolicy -n payments", body)
        self.assertIn("No resources found in payments namespace.", body)
        self.assertIn("All pod-to-pod traffic in payments is unrestricted.", body)
        self.assertIn(
            "Generated by the Platform Agent `compliance-audit` watchdog", body
        )
        self.assertTrue(
            body.rstrip().endswith('<!-- audit-findings: ["no-network-policy"] -->')
        )

    def test_body_explains_how_to_ask_for_a_remediation_pr(self):
        body = audit_pr.render_issue_body(make_doc(), generated_at=NOW)
        self.assertIn("`/remediate <finding-id>`", body)
        self.assertIn("write access", body)

    def test_body_names_no_staged_files(self):
        # The ledger is an issue: it has no diff, so it must never claim one.
        body = audit_pr.render_issue_body(make_doc(), generated_at=NOW)
        self.assertNotIn("Remediation files in this PR", body)

    def test_evidence_command_is_fenced(self):
        body = audit_pr.render_issue_body(make_doc(), generated_at=NOW)
        self.assertIn("```bash\nkubectl get networkpolicy -n payments\n```", body)

    def test_severity_groups_ordered_critical_major_minor(self):
        body = audit_pr.render_issue_body(
            make_doc(findings=THREE_SEVERITIES), generated_at=NOW
        )
        order = [
            body.index("### Critical (2)"),
            body.index("### Major (1)"),
            body.index("### Minor (1)"),
        ]
        self.assertEqual(order, sorted(order))
        self.assertIn("4 findings: 2 critical, 1 major, 1 minor.", body)

    def test_empty_severity_group_is_omitted(self):
        body = audit_pr.render_issue_body(
            make_doc(findings=[make_finding(severity="minor")]),
            generated_at=NOW,
        )
        self.assertIn("### Minor (1)", body)
        self.assertNotIn("### Critical", body)
        self.assertNotIn("### Major", body)

    def test_skipped_clusters_declare_partial_coverage(self):
        doc = make_doc(
            skipped=[{"cluster": "dr-west", "reason": "control plane unreachable"}]
        )
        body = audit_pr.render_issue_body(doc, generated_at=NOW)
        self.assertIn("### Skipped", body)
        self.assertIn("**Coverage is partial.**", body)
        self.assertIn("| `dr-west` | control plane unreachable |", body)

    def test_no_skipped_section_when_none_skipped(self):
        body = audit_pr.render_issue_body(make_doc(), generated_at=NOW)
        self.assertNotIn("### Skipped", body)
        self.assertNotIn("Coverage is partial", body)

    def test_gcloud_remediation_shows_command_and_stages_nothing(self):
        finding = make_finding(
            remediation={
                "kind": "gcloud",
                "note": "gcloud container clusters update prod-us-east --enable-shielded-nodes",
            }
        )
        body = audit_pr.render_issue_body(
            make_doc(findings=[finding]), generated_at=NOW
        )
        self.assertIn("- **Remediation (gcloud):**", body)
        self.assertIn("gcloud container clusters update prod-us-east", body)
        self.assertEqual(audit_pr.manifest_paths([finding]), [])

    def test_manifest_remediation_links_the_path(self):
        body = audit_pr.render_issue_body(make_doc(), generated_at=NOW)
        self.assertIn(
            "[`clusters/prod-us-east/payments-netpol.yaml`]"
            "(clusters/prod-us-east/payments-netpol.yaml)",
            body,
        )

    def test_cluster_scoped_finding_renders_without_namespace(self):
        body = audit_pr.render_issue_body(
            make_doc(findings=[make_finding(namespace="", obj="ClusterRole/admin")]),
            generated_at=NOW,
        )
        self.assertIn("_cluster-scoped_", body)

    def test_body_is_deterministic_regardless_of_input_order(self):
        first = audit_pr.render_issue_body(
            make_doc(findings=THREE_SEVERITIES), generated_at=NOW
        )
        second = audit_pr.render_issue_body(
            make_doc(findings=list(reversed(THREE_SEVERITIES))),
            generated_at=NOW,
        )
        self.assertEqual(first, second)

    def test_title_and_commit_subject(self):
        self.assertEqual(
            audit_pr.issue_title(AUDIT, THREE_SEVERITIES),
            "[audit] Security & RBAC Posture Audit — 4 findings (2 critical)",
        )
        self.assertEqual(
            audit_pr.commit_subject(AUDIT, THREE_SEVERITIES),
            "chore(audit): compliance-audit — 4 findings (2 critical, 1 major, 1 minor)",
        )

    def test_single_finding_is_not_pluralised(self):
        one = [make_finding(fid="only-one")]
        self.assertEqual(
            audit_pr.issue_title(AUDIT, one),
            "[audit] Security & RBAC Posture Audit — 1 finding (1 critical)",
        )
        self.assertEqual(
            audit_pr.commit_subject(AUDIT, one),
            "chore(audit): compliance-audit — 1 finding (1 critical, 0 major, 0 minor)",
        )
        body = audit_pr.render_issue_body(
            make_doc(findings=one), generated_at=NOW
        )
        self.assertIn("1 finding: 1 critical, 0 major, 0 minor.", body)
        self.assertNotIn("1 findings", body)

    def test_zero_findings_title_is_pluralised(self):
        self.assertEqual(
            audit_pr.issue_title(AUDIT, []),
            "[audit] Security & RBAC Posture Audit — 0 findings (0 critical)",
        )

    def test_excerpt_is_trimmed(self):
        long_excerpt = "\n".join(f"line {i}" for i in range(200))
        trimmed = audit_pr.trim_excerpt(long_excerpt)
        self.assertLessEqual(trimmed.count("\n"), audit_pr.MAX_EXCERPT_LINES)
        self.assertIn("excerpt truncated", trimmed)

    def test_excerpt_containing_a_fence_does_not_break_out(self):
        finding = make_finding(excerpt="```\nnested fence\n```")
        body = audit_pr.render_issue_body(
            make_doc(findings=[finding]), generated_at=NOW
        )
        self.assertIn("````text", body)


# --------------------------------------------------------------------------- #
# Hidden delta block
# --------------------------------------------------------------------------- #


class TestDeltaBlock(unittest.TestCase):
    def test_block_is_sorted_and_exact(self):
        self.assertEqual(
            audit_pr.delta_block(["b", "a"]), '<!-- audit-findings: ["a","b"] -->'
        )

    def test_round_trip(self):
        ids = ["zeta", "alpha", "mid"]
        body = audit_pr.render_issue_body(
            make_doc(findings=[make_finding(fid=i) for i in ids]),
            generated_at=NOW,
        )
        self.assertEqual(audit_pr.parse_delta_block(body), sorted(ids))

    def test_missing_or_broken_block_parses_as_empty(self):
        self.assertEqual(audit_pr.parse_delta_block(""), [])
        self.assertEqual(audit_pr.parse_delta_block(None), [])
        self.assertEqual(audit_pr.parse_delta_block("no marker here"), [])
        self.assertEqual(
            audit_pr.parse_delta_block("<!-- audit-findings: [oops] -->"), []
        )

    def test_compute_delta(self):
        new, resolved = audit_pr.compute_delta(["a", "b"], ["b", "c"])
        self.assertEqual(new, ["c"])
        self.assertEqual(resolved, ["a"])

    def test_delta_across_two_rendered_runs(self):
        run_one = audit_pr.render_issue_body(
            make_doc(
                findings=[
                    make_finding(fid="a", title="Alpha finding"),
                    make_finding(fid="b", title="Bravo finding"),
                ]
            ),
            generated_at=NOW,
        )
        run_two_doc = make_doc(
            findings=[
                make_finding(fid="b", title="Bravo finding"),
                make_finding(fid="c", title="Charlie finding"),
            ]
        )
        run_two = audit_pr.render_issue_body(run_two_doc, generated_at=NOW)

        previous_ids = audit_pr.parse_delta_block(run_one)
        current_ids = audit_pr.parse_delta_block(run_two)
        new, resolved = audit_pr.compute_delta(previous_ids, current_ids)
        self.assertEqual(new, ["c"])
        self.assertEqual(resolved, ["a"])

        titles = audit_pr.parse_finding_titles(run_one)
        self.assertEqual(titles["a"], "Alpha finding")

        comment = audit_pr.render_delta_comment(
            AUDIT, new, resolved, run_two_doc["findings"], titles, NOW
        )
        self.assertIn("**1 new**", comment)
        self.assertIn("Charlie finding", comment)
        self.assertIn("**1 resolved**", comment)
        # Resolved findings are named by the title recovered from the old body.
        self.assertIn("Alpha finding", comment)

    def test_no_comment_when_nothing_changed(self):
        self.assertIsNone(audit_pr.render_delta_comment(AUDIT, [], [], [], {}, NOW))


# --------------------------------------------------------------------------- #
# Schema validation
# --------------------------------------------------------------------------- #


class TestValidation(unittest.TestCase):
    def test_valid_document_passes(self):
        self.assertEqual(audit_pr.validate_findings(make_doc(), AUDIT)["audit"], AUDIT)

    def test_zero_findings_is_valid(self):
        audit_pr.validate_findings(make_doc(findings=[]), AUDIT)

    def test_unknown_audit_id_rejected(self):
        with self.assertRaisesRegex(audit_pr.ValidationError, "unknown audit id"):
            audit_pr.validate_audit_id("not-an-audit")

    def test_audit_id_mismatch_rejected(self):
        with self.assertRaises(audit_pr.ValidationError) as exc:
            audit_pr.validate_findings(make_doc(audit="obtainability-audit"), AUDIT)
        self.assertIn("audit:", str(exc.exception))
        self.assertIn("obtainability-audit", str(exc.exception))

    def test_empty_scope_clusters_rejected(self):
        with self.assertRaises(audit_pr.ValidationError) as exc:
            audit_pr.validate_findings(make_doc(clusters=[]), AUDIT)
        self.assertIn("scope.clusters", str(exc.exception))
        self.assertIn("not a clean run", str(exc.exception))

    def test_missing_evidence_command_rejected(self):
        doc = make_doc(findings=[make_finding(), make_finding(fid="second")])
        del doc["findings"][1]["evidence"]["command"]
        with self.assertRaises(audit_pr.ValidationError) as exc:
            audit_pr.validate_findings(doc, AUDIT)
        self.assertIn("findings[1].evidence.command", str(exc.exception))

    def test_empty_evidence_command_rejected(self):
        doc = make_doc(findings=[make_finding(command="   ")])
        with self.assertRaises(audit_pr.ValidationError) as exc:
            audit_pr.validate_findings(doc, AUDIT)
        self.assertIn("findings[0].evidence.command", str(exc.exception))
        self.assertIn("dropped, not softened", str(exc.exception))

    def test_duplicate_ids_rejected(self):
        doc = make_doc(
            findings=[
                make_finding(fid="dupe"),
                make_finding(fid="other"),
                make_finding(fid="dupe"),
            ]
        )
        with self.assertRaises(audit_pr.ValidationError) as exc:
            audit_pr.validate_findings(doc, AUDIT)
        self.assertIn("findings[2].id", str(exc.exception))
        self.assertIn("findings[0]", str(exc.exception))

    def test_manifest_without_path_rejected(self):
        doc = make_doc(
            findings=[make_finding(remediation={"kind": "manifest", "note": "fix it"})]
        )
        with self.assertRaises(audit_pr.ValidationError) as exc:
            audit_pr.validate_findings(doc, AUDIT)
        self.assertIn("findings[0].remediation.path", str(exc.exception))

    def test_gcloud_with_path_rejected(self):
        doc = make_doc(
            findings=[
                make_finding(
                    remediation={"kind": "gcloud", "path": "a.yaml", "note": "n"}
                )
            ]
        )
        with self.assertRaises(audit_pr.ValidationError) as exc:
            audit_pr.validate_findings(doc, AUDIT)
        self.assertIn("findings[0].remediation.path", str(exc.exception))

    def test_bad_severity_rejected(self):
        doc = make_doc(findings=[make_finding(severity="catastrophic")])
        with self.assertRaises(audit_pr.ValidationError) as exc:
            audit_pr.validate_findings(doc, AUDIT)
        self.assertIn("findings[0].severity", str(exc.exception))

    def test_bad_remediation_kind_rejected(self):
        doc = make_doc(
            findings=[make_finding(remediation={"kind": "ansible", "note": "n"})]
        )
        with self.assertRaises(audit_pr.ValidationError) as exc:
            audit_pr.validate_findings(doc, AUDIT)
        self.assertIn("findings[0].remediation.kind", str(exc.exception))

    def test_empty_namespace_allowed(self):
        audit_pr.validate_findings(
            make_doc(findings=[make_finding(namespace="")]), AUDIT
        )

    def test_path_escaping_repo_root_rejected(self):
        for bad in ("../../etc/passwd", "/etc/passwd"):
            with self.subTest(path=bad):
                doc = make_doc(
                    findings=[
                        make_finding(
                            remediation={"kind": "manifest", "path": bad, "note": "n"}
                        )
                    ]
                )
                with self.assertRaises(audit_pr.ValidationError) as exc:
                    audit_pr.validate_findings(doc, AUDIT)
                self.assertIn("findings[0].remediation.path", str(exc.exception))

    def test_findings_must_be_a_list(self):
        doc = make_doc()
        doc["findings"] = {"nope": True}
        with self.assertRaisesRegex(audit_pr.ValidationError, "findings:"):
            audit_pr.validate_findings(doc, AUDIT)

    def test_skipped_entry_needs_a_reason(self):
        doc = make_doc(skipped=[{"cluster": "dr-west"}])
        with self.assertRaises(audit_pr.ValidationError) as exc:
            audit_pr.validate_findings(doc, AUDIT)
        self.assertIn("scope.skipped[0].reason", str(exc.exception))


# --------------------------------------------------------------------------- #
# Audit catalogue
# --------------------------------------------------------------------------- #


class TestAuditCatalogue(unittest.TestCase):
    def test_human_names_match_the_cron_watchdogs(self):
        """The PR title must name the same audit the cron catalogue does."""
        jobs_file = (
            Path(__file__).resolve().parents[4] / "platform" / "cron" / "jobs.json"
        )
        if not jobs_file.is_file():  # not shipped alongside the skill at runtime
            self.skipTest(f"{jobs_file} not present")
        jobs = json.loads(jobs_file.read_text(encoding="utf-8"))["jobs"]
        names = {job["id"]: job["name"] for job in jobs}
        for audit_id, human in audit_pr.AUDITS.items():
            if audit_id in names:
                with self.subTest(audit=audit_id):
                    self.assertEqual(
                        human,
                        names[audit_id],
                        f"audit_pr.AUDITS[{audit_id!r}] is {human!r} but "
                        f"cron/jobs.json calls it {names[audit_id]!r}",
                    )


# --------------------------------------------------------------------------- #
# Protected branches
# --------------------------------------------------------------------------- #


class TestProtectedBranches(unittest.TestCase):
    def test_refuses_to_push_protected_branch(self):
        for branch in ("main", "master", "production", "MAIN", " main "):
            with self.subTest(branch=branch):
                with self.assertRaisesRegex(ValueError, "CRITICAL SECURITY REFUSAL"):
                    audit_pr.assert_pushable(branch)

    def test_remediation_branch_is_pushable(self):
        # The audit report branch is gone; the only branch the harness ever
        # pushes is a remediation branch, so that is what the guard must clear.
        for audit_id in audit_pr.AUDITS:
            with self.subTest(audit=audit_id):
                branch = audit_pr.group_branch_for(audit_id, [make_finding(fid="a")])
                self.assertEqual(branch, f"platform-agent/fix-{audit_id}-a")
                self.assertEqual(audit_pr.assert_pushable(branch), branch)


# --------------------------------------------------------------------------- #
# Staging set
# --------------------------------------------------------------------------- #


class TestStaging(unittest.TestCase):
    def test_distinct_manifest_paths_only(self):
        findings = [
            make_finding(fid="a"),  # clusters/prod-us-east/payments-netpol.yaml
            make_finding(fid="b"),  # same path -> deduplicated
            make_finding(
                fid="c",
                remediation={
                    "kind": "manifest",
                    "path": "clusters/stage-eu/psp.yaml",
                    "note": "n",
                },
            ),
            make_finding(fid="d", remediation={"kind": "gcloud", "note": "gcloud ..."}),
            make_finding(fid="e", remediation={"kind": "manual", "note": "call SRE"}),
        ]
        self.assertEqual(
            audit_pr.manifest_paths(findings),
            [
                "clusters/prod-us-east/payments-netpol.yaml",
                "clusters/stage-eu/psp.yaml",
            ],
        )

    def test_git_add_command_is_explicit(self):
        cmd = audit_pr.build_git_add_command(["a.yaml", "b.yaml"])
        self.assertEqual(
            cmd,
            ["git", "--literal-pathspecs", "add", "--", "a.yaml", "b.yaml"],
        )

    def test_literal_pathspecs_precedes_the_subcommand(self):
        # `git add --literal-pathspecs` is an error: the flag is git-level, so
        # it has to sit before `add` or the whole guard fails at runtime.
        cmd = audit_pr.build_git_add_command(["a.yaml"])
        self.assertLess(cmd.index("--literal-pathspecs"), cmd.index("add"))

    def test_wildcard_pathspecs_refused(self):
        for pathspec in (".", "-A", "--all", "-a", "*", ":/"):
            with self.subTest(pathspec=pathspec):
                with self.assertRaisesRegex(ValueError, "wildcard pathspec"):
                    audit_pr.build_git_add_command([pathspec])

    def test_glob_metacharacters_refused_in_a_declared_path(self):
        # --literal-pathspecs makes these harmless to git, but a path with a
        # glob in it is a sign the agent meant to stage a set, not a file.
        for path in (
            "clusters/*.yaml",
            "clusters/prod-?/netpol.yaml",
            "clusters/[ab]/netpol.yaml",
            "clusters/x].yaml",
        ):
            with self.subTest(path=path):
                with self.assertRaises(audit_pr.ValidationError):
                    audit_pr.validate_findings(
                        make_doc(
                            findings=[
                                make_finding(
                                    remediation={
                                        "kind": "manifest",
                                        "path": path,
                                        "note": "n",
                                    }
                                )
                            ]
                        ),
                        AUDIT,
                    )

    def test_literal_pathspec_flag_defeats_a_glob_against_real_git(self):
        # Defence in depth, measured rather than assumed. The validator above
        # already refuses a glob, so this exercises the *second* layer: it
        # takes the flag prefix the harness actually emits and points it at a
        # repo holding a file literally named '*.yaml' alongside two files the
        # glob would match. Without the flag git stages all three.
        git = shutil.which("git")
        if git is None:  # pragma: no cover - git is present locally and in CI
            self.skipTest("git not on PATH")

        prefix = audit_pr.build_git_add_command(["one.yaml"])[1:3]
        self.assertEqual(prefix, ["--literal-pathspecs", "add"])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def run(*args, **kw):
                return subprocess.run(
                    [git, *args], cwd=root, check=True, capture_output=True, text=True, **kw
                )

            run("init", "-q", "-b", "main")
            run("config", "user.email", "audit@example.invalid")
            run("config", "user.name", "audit")
            for name in ("*.yaml", "one.yaml", "two.yaml"):
                (root / name).write_text("x\n", encoding="utf-8")

            run(*prefix, "--", "*.yaml")
            staged = run("diff", "--cached", "--name-only").stdout.split()
            self.assertEqual(staged, ["*.yaml"])

    def test_empty_staging_set_refuses_to_build_an_add(self):
        with self.assertRaisesRegex(ValueError, "no explicit paths"):
            audit_pr.build_git_add_command([])


# --------------------------------------------------------------------------- #
# finish — end-to-end over the recorded seam
# --------------------------------------------------------------------------- #


class TestFinishWithFindings(HarnessTestCase):
    def test_opens_the_ledger_issue_and_touches_no_branch(self):
        self.harness.replies = {
            "issue list": "[]",
            "issue create": "https://github.com/acme/fleet/issues/7\n",
        }
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        self.touch("clusters/stage-eu/psp.yaml")
        doc = make_doc(
            findings=[
                make_finding(fid="a"),
                make_finding(fid="b"),  # duplicate path
                make_finding(
                    fid="c",
                    severity="minor",
                    remediation={
                        "kind": "manifest",
                        "path": "clusters/stage-eu/psp.yaml",
                        "note": "n",
                    },
                ),
                make_finding(
                    fid="d",
                    severity="major",
                    remediation={"kind": "gcloud", "note": "gcloud x"},
                ),
            ]
        )

        rc = self.run_finish(doc)
        self.assertEqual(rc, 0)

        # A report is an issue now: no branch, no staging, no commit, no push.
        self.assertEqual(self.git_add_calls(self.harness), [])
        self.assertFalse(self.harness.matching("git", "commit"))
        self.assertFalse(self.harness.matching("git", "push"))
        self.assertFalse(self.harness.matching("git", "checkout"))

        create = self.harness.matching("issue", "create")[0]
        self.assertIn("--label", create)
        self.assertIn("agent:audit", create)
        self.assertIn("audit:compliance-audit", create)
        self.assertIn("--body-file", create)
        self.assertFalse(self.harness.matching("issue", "edit", "--title"))
        # The whole point of the split: reporting never opens a pull request.
        self.assertEqual(self.harness.gh_calls("pr"), [])

    def test_opened_status_json(self):
        self.harness.replies = {
            "issue list": "[]",
            "issue create": "https://github.com/acme/fleet/issues/7\n",
        }
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        self.run_finish(make_doc())
        self.assertEqual(
            self.stdout_json(),
            {
                "status": "OPENED",
                "issue_url": "https://github.com/acme/fleet/issues/7",
                "new": 1,
                "resolved": 0,
                "prs_opened": [],
                "prs_closed": [],
            },
        )

    def test_severity_label_is_applied_to_the_new_issue(self):
        self.harness.replies = {
            "issue list": "[]",
            "issue create": "https://github.com/acme/fleet/issues/7\n",
        }
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        self.run_finish(make_doc())
        label = self.harness.matching("issue", "edit", "severity:critical")
        self.assertTrue(label)
        self.assertEqual(label[0][:4], ["gh", "issue", "edit", "7"])

    def test_updates_in_place_and_posts_delta(self):
        previous_body = audit_pr.render_issue_body(
            make_doc(
                findings=[
                    make_finding(fid="a", title="Alpha finding"),
                    make_finding(fid="b", title="Bravo finding"),
                ]
            ),
            generated_at=NOW,
        )
        self.harness.replies = {
            "issue list": self.issue_list(),
            "--json body": json.dumps({"body": previous_body}),
        }
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        doc = make_doc(
            findings=[
                make_finding(fid="b", title="Bravo finding"),
                make_finding(fid="c", title="Charlie finding"),
            ]
        )

        rc = self.run_finish(doc)
        self.assertEqual(rc, 0)

        self.assertFalse(self.harness.matching("issue", "create"))
        edit = self.harness.matching("issue", "edit", "--title")[0]
        self.assertEqual(edit[:4], ["gh", "issue", "edit", "42"])
        self.assertIn("--body-file", edit)

        self.assertTrue(self.harness.matching("issue", "comment", "42"))

        self.assertEqual(
            self.stdout_json(),
            {
                "status": "UPDATED",
                "issue_url": "https://github.com/acme/fleet/issues/42",
                "new": 1,
                "resolved": 1,
                "prs_opened": [],
                "prs_closed": [],
            },
        )

    def test_no_comment_when_findings_unchanged(self):
        doc = make_doc()
        previous_body = audit_pr.render_issue_body(doc, generated_at=NOW)
        self.harness.replies = {
            "issue list": self.issue_list(),
            "--json body": json.dumps({"body": previous_body}),
        }
        self.touch("clusters/prod-us-east/payments-netpol.yaml")

        self.run_finish(doc)

        # Body still refreshed, but silence when nothing changed.
        self.assertTrue(self.harness.matching("issue", "edit", "--title"))
        self.assertFalse(self.harness.matching("issue", "comment"))
        result = self.stdout_json()
        self.assertEqual(result["status"], "UPDATED")
        self.assertEqual(result["new"], 0)
        self.assertEqual(result["resolved"], 0)

    def test_unreadable_previous_body_suppresses_the_delta(self):
        # None is not "": an unreadable body makes the delta unknowable, and
        # announcing every live finding as new is worse than announcing none.
        self.harness.replies = {"issue list": self.issue_list()}
        self.harness.failures = {"--json body": 1}
        self.touch("clusters/prod-us-east/payments-netpol.yaml")

        self.assertEqual(self.run_finish(make_doc()), 0)

        self.assertFalse(self.harness.matching("issue", "comment"))
        result = self.stdout_json()
        self.assertEqual(result["status"], "UPDATED")
        self.assertEqual(result["new"], 0)
        self.assertEqual(result["resolved"], 0)
        self.assertIn("unreadable", self.err)

    def test_gcloud_only_run_still_publishes(self):
        self.harness.replies = {
            "issue list": "[]",
            "issue create": "https://github.com/acme/fleet/issues/9\n",
        }
        doc = make_doc(
            findings=[
                make_finding(remediation={"kind": "gcloud", "note": "gcloud ..."})
            ]
        )
        self.assertEqual(self.run_finish(doc), 0)
        self.assertEqual(self.git_add_calls(self.harness), [])
        self.assertTrue(self.harness.matching("issue", "create"))

    def test_missing_remediation_file_is_a_hard_error(self):
        self.harness.replies = {"issue list": "[]"}
        # Deliberately do NOT create the manifest on disk.
        rc = self.run_finish(make_doc())
        self.assertEqual(rc, 2)
        self.assertIn("does not exist under the repository root", self.err)
        self.assertFalse(self.harness.matching("issue", "create"))
        self.assertFalse(self.harness.matching("issue", "edit"))


class TestFinishClean(HarnessTestCase):
    def test_clean_run_closes_the_open_ledger_as_completed(self):
        previous_body = audit_pr.render_issue_body(
            make_doc(findings=[make_finding(fid="a"), make_finding(fid="b")]),
            generated_at=NOW,
        )
        self.harness.replies = {
            "issue list": self.issue_list(),
            "--json body": json.dumps({"body": previous_body}),
        }

        rc = self.run_finish(make_doc(findings=[]))
        self.assertEqual(rc, 0)

        self.assertTrue(self.harness.matching("issue", "comment", "42"))
        close = self.harness.matching("issue", "close", "42")
        self.assertTrue(close)
        # "completed", never "not planned": a clean fleet is done, not rejected.
        self.assertIn("--reason", close[0])
        self.assertIn("completed", close[0])
        # Nothing is committed, pushed, or deleted on a clean run.
        self.assertFalse(self.harness.matching("git", "push"))
        self.assertFalse(self.harness.matching("git", "commit"))
        self.assertFalse(self.harness.matching("branch", "-D"))

        self.assertEqual(
            self.stdout_json(),
            {
                "status": "CLEAN",
                "issue_url": "https://github.com/acme/fleet/issues/42",
                "new": 0,
                "resolved": 2,
                "prs_opened": [],
                "prs_closed": [],
            },
        )

    def test_a_failed_all_clear_comment_still_closes_the_ledger(self):
        # The close used to sit outside the try/finally, so a 422 on the
        # comment left the ledger open forever with no explanation.
        self.harness.replies = {"issue list": self.issue_list()}
        self.harness.failures = {"issue comment": 1}

        self.assertEqual(self.run_finish(make_doc(findings=[])), 0)

        self.assertTrue(self.harness.matching("issue", "close", "42"))
        self.assertIn("could not post the all-clear comment", self.err)

    def test_clean_run_with_no_open_ledger_is_a_no_op(self):
        self.harness.replies = {"issue list": "[]"}
        self.assertEqual(self.run_finish(make_doc(findings=[])), 0)
        self.assertFalse(self.harness.matching("issue", "close"))
        self.assertFalse(self.harness.matching("issue", "comment"))
        self.assertEqual(
            self.stdout_json(),
            {
                "status": "CLEAN",
                "issue_url": None,
                "new": 0,
                "resolved": 0,
                "prs_opened": [],
                "prs_closed": [],
            },
        )

    def test_clean_comment_names_date_and_scope(self):
        comment = audit_pr.render_clean_comment(AUDIT, make_doc(findings=[]), NOW)
        self.assertIn("2026-08-01 09:30 UTC", comment)
        self.assertIn("0 findings", comment)
        self.assertIn("`prod-us-east`", comment)
        self.assertIn("`stage-eu`", comment)


# --------------------------------------------------------------------------- #
# start
# --------------------------------------------------------------------------- #


class TestStart(HarnessTestCase):
    def setUp(self):
        super().setUp()
        # handle_start pre-creates /opt/data/scratch; keep the tests off the real FS.
        patcher = patch.object(audit_pr.os, "makedirs", lambda *a, **k: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.patch_attr(
            "findings_path_for",
            lambda audit_id: str(self.tmp_path / f"findings_{audit_id}.json"),
        )

    def test_emits_one_json_line(self):
        self.harness.replies = {"issue list": self.issue_list()}
        self.assertEqual(self.run_main(["start", "--audit", AUDIT]), 0)

        out = self.out.strip()
        self.assertNotIn("\n", out)
        self.assertEqual(
            json.loads(out),
            {
                "issue": 42,
                "repo": "acme/fleet",
                "findings_path": str(self.tmp_path / "findings_compliance-audit.json"),
                "pending_remediation_requests": [],
            },
        )

    def test_null_issue_when_none_open(self):
        self.harness.replies = {"issue list": "[]"}
        self.run_main(["start", "--audit", "obtainability-audit"])
        self.assertIsNone(json.loads(self.out)["issue"])

    def test_no_branch_is_touched(self):
        # The report branch is gone. `start` reads GitHub and nothing else.
        self.harness.replies = {"issue list": "[]"}
        self.run_main(["start", "--audit", AUDIT])
        self.assertFalse(self.harness.matching("git", "checkout"))
        self.assertFalse(self.harness.matching("git", "pull"))
        self.assertFalse(self.harness.matching("git", "fetch"))

    def test_a_stale_findings_file_is_removed(self):
        # A crashed run must not leave a document for the next one to publish.
        self.harness.replies = {"issue list": "[]"}
        stale = self.tmp_path / f"findings_{AUDIT}.json"
        stale.write_text('{"audit": "stale"}', encoding="utf-8")
        self.run_main(["start", "--audit", AUDIT])
        self.assertFalse(stale.exists())

    def test_pending_remediate_requests_are_reported(self):
        self.harness.replies = {
            "issue list": self.issue_list(),
            "--json comments": json.dumps(
                {
                    "comments": [
                        comment("/remediate no-network-policy"),
                        comment("/remediate nope", association="NONE"),
                    ]
                }
            ),
        }
        self.run_main(["start", "--audit", AUDIT])
        self.assertEqual(
            json.loads(self.out)["pending_remediation_requests"],
            ["no-network-policy"],
        )

    def test_a_gh_outage_fails_loudly_rather_than_reporting_no_ledger(self):
        # Returning "no issue" on a transport failure would open a duplicate.
        self.harness.failures = {"issue list": 1}
        self.assertEqual(self.run_main(["start", "--audit", AUDIT]), 1)
        self.assertIn("could not list issues", self.err)

    def test_creates_labels(self):
        self.harness.replies = {"issue list": "[]"}
        self.run_main(["start", "--audit", AUDIT])

        created = {c[3] for c in self.harness.matching("label", "create")}
        self.assertEqual(
            created,
            {
                "agent:audit",
                "audit:compliance-audit",
                "severity:critical",
                "severity:major",
                "severity:minor",
            },
        )

    def test_unknown_audit_id_touches_nothing(self):
        self.assertEqual(self.run_main(["start", "--audit", "made-up-audit"]), 2)
        self.assertEqual(self.harness.calls, [])


# --------------------------------------------------------------------------- #
# --dry-run
# --------------------------------------------------------------------------- #


class TestDryRun(BaseTestCase):
    """No credential or repo-root stubs here on purpose: --dry-run must not need them."""

    def test_renders_body_without_side_effects(self):
        recorder = Recorder()
        self.patch_attr("run_cmd", recorder)

        def explode():
            raise AssertionError("--dry-run must not touch credentials")

        self.patch_attr("refresh_credentials", explode)
        self.patch_attr("resolve_repo", explode)

        rc = self.run_finish(make_doc(), argv_extra=("--dry-run",))
        self.assertEqual(rc, 0)

        self.assertIn("## Findings", self.out)
        self.assertIn("<!-- audit-findings:", self.out)
        self.assertEqual([c for c in recorder.calls if c[0] == "gh"], [])
        for call in recorder.calls:
            self.assertNotEqual(call[:2], ["git", "add"])
            self.assertNotEqual(call[:2], ["git", "push"])
            self.assertNotEqual(call[:2], ["git", "commit"])

    def test_dry_run_still_rejects_bad_findings(self):
        doc = make_doc(clusters=[])
        self.assertEqual(self.run_finish(doc, argv_extra=("--dry-run",)), 2)
        self.assertIn("scope.clusters", self.err)

    def test_dry_run_clean_renders_the_close_comment(self):
        self.patch_attr("run_cmd", Recorder())
        self.assertEqual(
            self.run_finish(make_doc(findings=[]), argv_extra=("--dry-run",)), 0
        )
        self.assertIn("is now clean", self.out)


# --------------------------------------------------------------------------- #
# Size budget — the difference between a stream that publishes and one that 422s
# --------------------------------------------------------------------------- #


def bulk_findings(count, severity="minor", prefix="f"):
    """`count` findings with distinct ids and SOP-shaped prose."""
    return [
        make_finding(
            fid=f"{prefix}-{i:04d}",
            severity=severity,
            title=f"Finding {i}: workload deviates from the baseline",
            namespace=f"ns-{i:04d}",
            obj=f"Deployment/app-{i:04d}",
            command=(
                f"kubectl --context prod-us-east -n ns-{i:04d} get deployment "
                f"app-{i:04d} -o jsonpath='{{.spec.template.spec.containers[*].resources}}'"
            ),
            excerpt="\n".join(f"line {n} of captured output" for n in range(12)),
            remediation={
                "kind": "manifest",
                "path": f"clusters/prod-us-east/ns-{i:04d}-app-{i:04d}.yaml",
                "note": "Apply the corrected manifest.",
            },
        )
        for i in range(count)
    ]


class TestRenderBudget(BaseTestCase):
    def render(self, doc):
        return audit_pr.render_issue_body(doc, generated_at=NOW)

    def test_body_stays_under_the_github_limit_at_250_findings(self):
        body = self.render(make_doc(findings=bulk_findings(250)))
        self.assertLess(len(body), audit_pr.MAX_BODY_CHARS)

    def test_ten_findings_render_untruncated(self):
        findings = bulk_findings(10)
        body = self.render(make_doc(findings=findings))
        self.assertNotIn("further findings omitted", body)
        for finding in findings:
            self.assertIn(finding["id"], body)

    def test_truncation_notice_names_the_omitted_count(self):
        body = self.render(make_doc(findings=bulk_findings(250)))
        self.assertRegex(body, r"\d+ further finding\(s\) are omitted")

    def test_title_carries_the_true_total_even_when_truncated(self):
        findings = bulk_findings(250)
        body = self.render(make_doc(findings=findings))
        title = audit_pr.issue_title(AUDIT, findings)
        self.assertIn("250 findings", title)
        # The rendered body must not silently disagree with the title.
        self.assertIn("250", body)

    def test_delta_block_lists_exactly_the_rendered_ids(self):
        findings = bulk_findings(250)
        body = self.render(make_doc(findings=findings))
        recorded = audit_pr.parse_delta_block(body)
        ordered = [f["id"] for f in audit_pr.sort_findings(findings)]

        self.assertTrue(recorded)
        self.assertLess(len(recorded), len(findings), "fixture must overflow")
        # The recorded set is a prefix of the severity-first order, so
        # truncation only ever eats the least-severe end.
        self.assertEqual(recorded, ordered[: len(recorded)])
        # Every recorded id is genuinely in the body, and the first id that is
        # not recorded is genuinely absent — otherwise the next run reads a
        # truncated finding as resolved and announces a fix that never happened.
        for fid in recorded:
            self.assertIn(fid, body)
        self.assertNotIn(ordered[len(recorded)], body)

    def test_criticals_survive_a_flood_of_minor_findings(self):
        findings = bulk_findings(5, severity="critical", prefix="crit") + bulk_findings(
            300, severity="minor"
        )
        body = self.render(make_doc(findings=findings))
        for i in range(5):
            self.assertIn(f"crit-{i:04d}", body)
        self.assertLess(len(body), audit_pr.MAX_BODY_CHARS)

    def test_scope_only_body_cannot_overflow(self):
        # Zero findings, an enormous fleet: this overflowed at 148,627 chars
        # before the scope tables were capped.
        doc = make_doc(
            findings=[],
            clusters=[
                {"name": f"c-{i:04d}", "location": "us-east1", "project": "acme"}
                for i in range(1200)
            ],
            skipped=[
                {"cluster": f"s-{i:04d}", "reason": "control plane unreachable"}
                for i in range(1200)
            ],
        )
        body = self.render(doc)
        self.assertLess(len(body), audit_pr.MAX_BODY_CHARS)
        self.assertIn("more", body)

    def test_clean_comment_stays_under_the_limit_at_900_skipped(self):
        doc = make_doc(
            findings=[],
            clusters=[
                {"name": f"c-{i:04d}", "location": "us-east1", "project": "acme"}
                for i in range(900)
            ],
            skipped=[
                {"cluster": f"s-{i:04d}", "reason": "unreachable"} for i in range(900)
            ],
        )
        comment = audit_pr.render_clean_comment(AUDIT, doc, NOW)
        self.assertLess(len(comment), audit_pr.MAX_BODY_CHARS)

    def test_delta_comment_stays_under_the_limit(self):
        # Newly reachable: capping the body means N is no longer pinned under
        # ~67 by the body failing first, so this path stops being dead code.
        findings = bulk_findings(250)
        comment = audit_pr.render_delta_comment(
            AUDIT,
            [f["id"] for f in findings],
            [f"gone-{i:04d}" for i in range(250)],
            findings,
            {f["id"]: f["title"] for f in findings},
            NOW,
        )
        self.assertLess(len(comment), audit_pr.MAX_BODY_CHARS)

    def test_long_command_is_trimmed(self):
        finding = make_finding(command="kubectl get pods " + "x" * 5000)
        rendered = "\n".join(audit_pr.render_finding(finding))
        self.assertLess(len(rendered), 4000)
        self.assertIn("truncated", rendered.lower())

    def test_selection_is_a_prefix_of_the_sorted_order(self):
        findings = bulk_findings(3, severity="minor") + bulk_findings(
            2, severity="critical", prefix="c"
        )
        rendered, omitted = audit_pr.select_rendered_findings(findings, 1)
        self.assertEqual(len(rendered), 1)
        self.assertEqual(rendered[0]["severity"], "critical")
        self.assertEqual(len(omitted), 4)

    def test_at_least_one_finding_always_renders(self):
        rendered, _ = audit_pr.select_rendered_findings(bulk_findings(5), 0)
        self.assertEqual(len(rendered), 1)


# --------------------------------------------------------------------------- #
# Schema — recommendation, limitations, and the finding-id charset
# --------------------------------------------------------------------------- #


class TestRecommendation(BaseTestCase):
    def assert_rejected(self, recommendation, pattern="recommendation"):
        doc = make_doc(findings=[make_finding(recommendation=recommendation)])
        with self.assertRaisesRegex(audit_pr.ValidationError, pattern):
            audit_pr.validate_findings(doc, AUDIT)

    def test_missing_recommendation_is_rejected(self):
        doc = make_doc(findings=[make_finding()])
        del doc["findings"][0]["recommendation"]
        with self.assertRaisesRegex(audit_pr.ValidationError, "recommendation"):
            audit_pr.validate_findings(doc, AUDIT)

    def test_each_sub_field_is_required(self):
        full = {"action": "a", "rationale": "r", "risk": "k"}
        for field in ("action", "rationale", "risk"):
            with self.subTest(missing=field):
                partial = {k: v for k, v in full.items() if k != field}
                self.assert_rejected(partial, field)

    def test_empty_sub_field_is_rejected(self):
        for field in ("action", "rationale", "risk"):
            with self.subTest(empty=field):
                rec = {"action": "a", "rationale": "r", "risk": "k"}
                rec[field] = "   "
                self.assert_rejected(rec, field)

    def test_wrong_type_is_rejected(self):
        self.assert_rejected("just a string")
        self.assert_rejected(["action", "rationale", "risk"])
        self.assert_rejected({"action": 5, "rationale": "r", "risk": "k"}, "action")

    def test_recommendation_renders_all_three_fields(self):
        rendered = "\n".join(
            audit_pr.render_finding(
                make_finding(
                    recommendation={
                        "action": "Do the thing.",
                        "rationale": "Because the alternative is worse.",
                        "risk": "Traffic may drop; check flows first.",
                    }
                )
            )
        )
        self.assertIn("Do the thing.", rendered)
        self.assertIn("Because the alternative is worse.", rendered)
        self.assertIn("Traffic may drop; check flows first.", rendered)


class TestScopeLimitations(BaseTestCase):
    def test_limitations_are_accepted(self):
        doc = make_doc(
            clusters=[
                {
                    "name": "prod-us-east",
                    "location": "us-east1",
                    "project": "acme-prod",
                    "limitations": "Autopilot: checks 2.1-2.3 did not run.",
                }
            ]
        )
        self.assertTrue(audit_pr.validate_findings(doc, AUDIT))

    def test_empty_limitations_entry_is_rejected(self):
        doc = make_doc(
            clusters=[
                {
                    "name": "prod-us-east",
                    "location": "us-east1",
                    "project": "acme-prod",
                    "limitations": "   ",
                }
            ]
        )
        with self.assertRaisesRegex(audit_pr.ValidationError, "limitations"):
            audit_pr.validate_findings(doc, AUDIT)

    def test_a_cluster_cannot_be_both_audited_and_skipped(self):
        # The Autopilot false-all-clear: the collision this field exists to end.
        doc = make_doc(
            clusters=[
                {"name": "prod-us-east", "location": "us-east1", "project": "acme"}
            ],
            skipped=[{"cluster": "prod-us-east", "reason": "Autopilot"}],
        )
        with self.assertRaises(audit_pr.ValidationError):
            audit_pr.validate_findings(doc, AUDIT)

    def test_duplicate_skipped_entries_are_rejected(self):
        doc = make_doc(
            findings=[],
            skipped=[
                {"cluster": "dr-west", "reason": "unreachable"},
                {"cluster": "dr-west", "reason": "unreachable again"},
            ],
        )
        with self.assertRaises(audit_pr.ValidationError):
            audit_pr.validate_findings(doc, AUDIT)

    def test_a_finding_cannot_name_a_skipped_cluster(self):
        doc = make_doc(
            findings=[make_finding(cluster="dr-west")],
            skipped=[{"cluster": "dr-west", "reason": "control plane unreachable"}],
        )
        with self.assertRaises(audit_pr.ValidationError):
            audit_pr.validate_findings(doc, AUDIT)


class TestFindingIdCharset(BaseTestCase):
    def test_usable_ids_are_accepted(self):
        for fid in ("a", "netpol-missing-payments", "v1.2.3-drift", "a" * 100):
            with self.subTest(fid=fid):
                self.assertEqual(audit_pr.validate_finding_id(fid, "where"), fid)

    def test_ids_git_would_refuse_are_rejected(self):
        # Each of these produces a branch `git check-ref-format` rejects once
        # the id becomes part of platform-agent/fix-<audit>-<id>.
        for fid in (
            "has:colon",
            "has space",
            "has..dots",
            "has*star",
            "ends.lock",
            "UPPERCASE",
            "-leading-dash",
            "trailing-dash-",
            "a" * 101,
            "",
            "has~tilde",
            "has^caret",
            "has?question",
            "has[bracket",
            "has\\backslash",
            "has\tab",
        ):
            with self.subTest(fid=fid):
                with self.assertRaises(audit_pr.ValidationError):
                    audit_pr.validate_finding_id(fid, "where")

    def test_accepted_ids_survive_git_check_ref_format(self):
        # The rule is only worth anything if git agrees with it.
        git = shutil.which("git")
        if git is None:  # pragma: no cover - git is present locally and in CI
            self.skipTest("git not on PATH")
        for fid in ("a", "netpol-missing-payments", "v1.2.3-drift", "a" * 100):
            with self.subTest(fid=fid):
                branch = audit_pr.group_branch_for(AUDIT, [make_finding(fid=fid)])
                proc = subprocess.run(
                    [git, "check-ref-format", f"refs/heads/{branch}"],
                    capture_output=True,
                )
                self.assertEqual(proc.returncode, 0, branch)


# --------------------------------------------------------------------------- #
# Remediation grouping (§5)
# --------------------------------------------------------------------------- #


def manifest_finding(fid, path, severity="critical"):
    return make_finding(
        fid=fid,
        severity=severity,
        remediation={"kind": "manifest", "path": path, "note": "n"},
    )


class TestRemediationGroups(BaseTestCase):
    def ids(self, groups):
        return [[f["id"] for f in group] for group in groups]

    def test_disjoint_paths_are_separate_groups(self):
        groups = audit_pr.remediation_groups(
            [manifest_finding("a", "x.yaml"), manifest_finding("b", "y.yaml")]
        )
        self.assertEqual(self.ids(groups), [["a"], ["b"]])

    def test_a_shared_path_merges_two_findings(self):
        # compliance_audit_sop.md points every finding in a namespace at one
        # shared default-sa-automount.yaml, so this is the common case.
        groups = audit_pr.remediation_groups(
            [
                manifest_finding("a", "shared.yaml"),
                manifest_finding("b", "shared.yaml"),
                manifest_finding("c", "other.yaml"),
            ]
        )
        self.assertEqual(self.ids(groups), [["a", "b"], ["c"]])

    def test_grouping_is_transitive(self):
        # Today's schema is one path per finding, which makes groups plain
        # equivalence classes and never exercises the union step. The union-find
        # is written to be transitive anyway, so drive it through the path
        # accessor: a—b share x, b—c share y, so all three are one PR.
        paths = {"a": {"x.yaml"}, "b": {"x.yaml", "y.yaml"}, "c": {"y.yaml"}}
        findings = [manifest_finding(fid, f"{fid}.yaml") for fid in ("a", "b", "c")]
        with patch.object(
            audit_pr, "_finding_paths", lambda f: paths[f["id"]]
        ):
            groups = audit_pr.remediation_groups(findings)
        self.assertEqual(self.ids(groups), [["a", "b", "c"]])

    def test_grouping_is_independent_of_input_order(self):
        findings = [
            manifest_finding("c", "other.yaml"),
            manifest_finding("b", "shared.yaml"),
            manifest_finding("a", "shared.yaml"),
        ]
        self.assertEqual(
            self.ids(audit_pr.remediation_groups(findings)),
            [["a", "b"], ["c"]],
        )

    def test_non_manifest_findings_do_not_form_groups(self):
        groups = audit_pr.remediation_groups(
            [
                make_finding(fid="a", remediation={"kind": "gcloud", "note": "g"}),
                make_finding(fid="b", remediation={"kind": "manual", "note": "m"}),
            ]
        )
        self.assertEqual(groups, [])

    def test_branch_is_named_for_the_lowest_sorted_id(self):
        group = [manifest_finding("zeta", "s.yaml"), manifest_finding("alpha", "s.yaml")]
        self.assertEqual(
            audit_pr.group_branch_for(AUDIT, group),
            f"platform-agent/fix-{AUDIT}-alpha",
        )

    def test_group_paths_are_deduplicated_and_sorted(self):
        group = [manifest_finding("a", "s.yaml"), manifest_finding("b", "s.yaml")]
        self.assertEqual(audit_pr.group_paths(group), ["s.yaml"])


# --------------------------------------------------------------------------- #
# §4 finding states and promotion (§3.1, Q4)
# --------------------------------------------------------------------------- #


class TestFindingState(BaseTestCase):
    def test_all_six_states(self):
        cases = [
            (True, None, audit_pr.STATE_OPEN),
            (True, {"state": "OPEN"}, audit_pr.STATE_PR_OPEN),
            (True, {"state": "MERGED"}, audit_pr.STATE_PR_MERGED_PERSISTS),
            (True, {"state": "CLOSED"}, audit_pr.STATE_REFUSED),
            (False, None, audit_pr.STATE_RESOLVED),
            (False, {"state": "MERGED"}, audit_pr.STATE_RESOLVED_MERGED),
        ]
        for reproduces, pr, expected in cases:
            with self.subTest(reproduces=reproduces, pr=pr):
                self.assertEqual(audit_pr.derive_finding_state(reproduces, pr), expected)

    def test_merged_at_counts_as_merged_even_without_a_state(self):
        self.assertEqual(
            audit_pr.derive_finding_state(True, {"mergedAt": "2026-08-01T00:00:00Z"}),
            audit_pr.STATE_PR_MERGED_PERSISTS,
        )

    def test_every_state_has_a_label(self):
        for state in (
            audit_pr.STATE_OPEN,
            audit_pr.STATE_PR_OPEN,
            audit_pr.STATE_PR_MERGED_PERSISTS,
            audit_pr.STATE_RESOLVED_MERGED,
            audit_pr.STATE_RESOLVED,
            audit_pr.STATE_REFUSED,
        ):
            self.assertIn(state, audit_pr.STATE_LABELS)


class TestPromotion(BaseTestCase):
    def test_only_critical_manifest_findings_auto_promote(self):
        findings = [
            manifest_finding("crit", "a.yaml", severity="critical"),
            manifest_finding("maj", "b.yaml", severity="major"),
            make_finding(
                fid="crit-gcloud",
                severity="critical",
                remediation={"kind": "gcloud", "note": "g"},
            ),
        ]
        promote, withheld = audit_pr.promotion_candidates(findings, {})
        self.assertEqual(promote, ["crit"])
        self.assertEqual(withheld, [])

    def test_a_finding_with_an_existing_pr_is_not_promoted_again(self):
        findings = [manifest_finding("crit", "a.yaml")]
        promote, withheld = audit_pr.promotion_candidates(
            findings, {"crit": {"state": "CLOSED"}}
        )
        self.assertEqual(promote, [])
        self.assertEqual(withheld, [])

    def test_auto_promotion_is_capped_and_names_the_withheld(self):
        findings = [
            manifest_finding(f"c-{i:02d}", f"{i}.yaml", severity="critical")
            for i in range(9)
        ]
        promote, withheld = audit_pr.promotion_candidates(findings, {})
        self.assertEqual(len(promote), audit_pr.AUTO_PROMOTION_CAP)
        self.assertEqual(len(withheld), 4)
        self.assertEqual(set(promote) & set(withheld), set())

    def test_an_explicit_request_bypasses_the_cap(self):
        findings = [
            manifest_finding(f"c-{i:02d}", f"{i}.yaml", severity="critical")
            for i in range(9)
        ] + [manifest_finding("asked", "asked.yaml", severity="minor")]
        promote, withheld = audit_pr.promotion_candidates(
            findings, {}, requested=["asked", "c-08"]
        )
        self.assertIn("asked", promote)
        self.assertIn("c-08", promote)
        # The two requested are uncapped; the auto path still yields cap-many.
        self.assertEqual(len(promote), 2 + audit_pr.AUTO_PROMOTION_CAP)
        self.assertNotIn("c-08", withheld)

    def test_a_requested_non_manifest_finding_is_not_promoted(self):
        findings = [
            make_finding(fid="g", remediation={"kind": "gcloud", "note": "g"}),
        ]
        promote, _ = audit_pr.promotion_candidates(findings, {}, requested=["g"])
        self.assertEqual(promote, [])


# --------------------------------------------------------------------------- #
# /remediate parsing (§3.1) and idempotency markers
# --------------------------------------------------------------------------- #


def comment(body, association="MEMBER", login="dev", node_id="IC_1"):
    return {
        "id": node_id,
        "body": body,
        "author": {"login": login},
        "authorAssociation": association,
    }


class TestRemediateCommands(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.findings = [
            manifest_finding("netpol-missing", "a.yaml"),
            make_finding(fid="cluster-old", remediation={"kind": "gcloud", "note": "g"}),
        ]

    def parse(self, comments):
        return audit_pr.parse_remediate_commands(comments, self.findings)

    def test_an_authorized_request_is_accepted(self):
        targets, refusals = self.parse([comment("/remediate netpol-missing")])
        self.assertEqual(targets, ["netpol-missing"])
        self.assertEqual(refusals, [])

    def test_a_commenter_without_write_access_is_refused_once(self):
        targets, refusals = self.parse(
            [comment("/remediate netpol-missing", association="NONE", login="drive-by")]
        )
        self.assertEqual(targets, [])
        self.assertEqual(len(refusals), 1)
        self.assertIn("write access", refusals[0]["reasons"][0])
        self.assertEqual(refusals[0]["comment_id"], "IC_1")

    def test_a_non_manifest_target_is_refused(self):
        targets, refusals = self.parse([comment("/remediate cluster-old")])
        self.assertEqual(targets, [])
        self.assertEqual(len(refusals), 1)
        self.assertIn("gcloud", refusals[0]["reasons"][0])

    def test_an_unknown_target_is_refused(self):
        _, refusals = self.parse([comment("/remediate no-such-finding")])
        self.assertIn("not a finding", refusals[0]["reasons"][0])

    def test_a_fenced_command_never_fires(self):
        body = "Here is how you would ask:\n\n```\n/remediate netpol-missing\n```\n"
        targets, refusals = self.parse([comment(body)])
        self.assertEqual(targets, [])
        self.assertEqual(refusals, [])

    def test_remediate_all_expands_to_promotable_targets_only(self):
        targets, refusals = self.parse([comment("/remediate all")])
        self.assertEqual(targets, ["netpol-missing"])
        self.assertEqual(refusals, [])

    def test_a_command_must_start_the_line(self):
        targets, _ = self.parse([comment("maybe we should /remediate netpol-missing")])
        self.assertEqual(targets, [])

    def test_one_refusal_per_comment_not_per_bad_target(self):
        body = "/remediate cluster-old\n/remediate no-such-finding\n"
        _, refusals = self.parse([comment(body)])
        self.assertEqual(len(refusals), 1)
        self.assertEqual(len(refusals[0]["reasons"]), 2)

    def test_targets_are_deduplicated_and_sorted(self):
        targets, _ = self.parse(
            [
                comment("/remediate netpol-missing", node_id="IC_1"),
                comment("/remediate netpol-missing", node_id="IC_2"),
            ]
        )
        self.assertEqual(targets, ["netpol-missing"])


class TestMarkers(BaseTestCase):
    def test_persists_marker_round_trips(self):
        body = f"Some text\n\n{audit_pr.persists_marker('abc')}\n"
        self.assertTrue(audit_pr.has_marker(body, audit_pr.PERSISTS_MARKER_RE, "abc"))
        self.assertFalse(audit_pr.has_marker(body, audit_pr.PERSISTS_MARKER_RE, "xyz"))

    def test_refused_marker_round_trips(self):
        body = f"Reply\n{audit_pr.refused_marker('IC_9')}\n"
        self.assertTrue(audit_pr.has_marker(body, audit_pr.REFUSED_MARKER_RE, "IC_9"))
        self.assertFalse(audit_pr.has_marker(body, audit_pr.REFUSED_MARKER_RE, "IC_8"))

    def test_absent_body_has_no_marker(self):
        self.assertFalse(audit_pr.has_marker(None, audit_pr.PERSISTS_MARKER_RE, "abc"))
        self.assertFalse(audit_pr.has_marker("", audit_pr.PERSISTS_MARKER_RE, "abc"))


class TestDeltaBlockAnchoring(BaseTestCase):
    def test_a_marker_quoted_inside_an_excerpt_cannot_hijack_the_real_block(self):
        # An opener injected mid-line must not start a match that spans into
        # the real block below it.
        body = (
            "Evidence:\n"
            '    text <!-- audit-findings: ["injected"] and more\n'
            "\n"
            + audit_pr.delta_block(["real-one", "real-two"])
            + "\n"
        )
        self.assertEqual(audit_pr.parse_delta_block(body), ["real-one", "real-two"])


# --------------------------------------------------------------------------- #
# Failure paths — reachable only now that Recorder can fail
# --------------------------------------------------------------------------- #


class TestFailurePaths(HarnessTestCase):
    def test_a_failed_issue_create_is_fatal(self):
        self.harness.replies = {"issue list": "[]"}
        self.harness.failures = {"issue create": 1}
        self.touch("clusters/prod-us-east/payments-netpol.yaml")

        rc = self.run_finish(make_doc())

        self.assertNotEqual(rc, 0)
        self.assertEqual(self.out, "")
        self.assertIn("subprocess failed with exit code 1", self.err)

    def test_a_failed_issue_edit_is_fatal(self):
        self.harness.replies = {"issue list": self.issue_list()}
        self.harness.failures = {"issue edit 42 -R acme/fleet --title": 1}
        self.touch("clusters/prod-us-east/payments-netpol.yaml")

        rc = self.run_finish(make_doc())

        self.assertNotEqual(rc, 0)
        # No delta comment on a ledger whose body was never rewritten.
        self.assertFalse(self.harness.matching("issue", "comment"))

    def test_a_failed_delta_comment_is_survivable(self):
        # Losing the delta comment costs one notification; aborting would
        # leave the ledger correct but the run marked failed to the cron.
        previous_body = audit_pr.render_issue_body(
            make_doc(findings=[make_finding(fid="a")]), generated_at=NOW
        )
        self.harness.replies = {
            "issue list": self.issue_list(),
            "--json body": json.dumps({"body": previous_body}),
        }
        self.harness.failures = {"issue comment": 1}
        self.touch("clusters/prod-us-east/payments-netpol.yaml")

        rc = self.run_finish(make_doc())

        self.assertEqual(rc, 0)
        self.assertIn("could not post the delta comment", self.err)
        self.assertEqual(self.stdout_json()["status"], "UPDATED")

    def test_a_failed_severity_label_is_survivable(self):
        self.harness.replies = {
            "issue list": "[]",
            "issue create": "https://github.com/acme/fleet/issues/7\n",
        }
        self.harness.failures = {"severity:critical": 1}
        self.touch("clusters/prod-us-east/payments-netpol.yaml")

        self.assertEqual(self.run_finish(make_doc()), 0)
        self.assertEqual(self.stdout_json()["status"], "OPENED")

    def test_recorder_raises_on_check_true_and_returns_on_check_false(self):
        # The fault-injection seam itself, so a silently-broken Recorder cannot
        # make every failure test above vacuously pass.
        recorder = Recorder(failures={"gh issue list": 1})
        with self.assertRaises(CalledProcessError):
            recorder(["gh", "issue", "list"])
        result = recorder(["gh", "issue", "list"], check=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(recorder(["git", "status"]).returncode, 0)


if __name__ == "__main__":
    unittest.main()
