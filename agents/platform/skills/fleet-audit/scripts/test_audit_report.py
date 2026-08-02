"""Unit tests for audit_report — the fleet-audit PR harness.

Run:
  python3 -m unittest discover -s agents/platform/skills/fleet-audit/scripts \
      -p 'test_audit_report.py' -v

Stdlib only, matching the other agent-script tests. No gh, gcloud, or GitHub
credentials are required: the validate/render/delta layer is pure, and the two
commands that do touch the network are driven through a single recorded seam
(audit_report.run_cmd) plus stubs for credential minting.
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

import audit_report  # noqa: E402
import gitops_workspace  # noqa: E402

AUDIT = "compliance-audit"
NOW = datetime(2026, 8, 1, 9, 30, tzinfo=timezone.utc)


def render_body(doc, **kwargs):
    """The rendered issue text.

    `render_issue_body` returns a `RenderedIssue` — the text plus the ids it
    actually managed to render — because the delta has to describe what a
    reader can see, not what the audit found. Most assertions here are about
    the prose, so they go through this; the ones that care about omission ask
    for the tuple directly.
    """
    return audit_report.render_issue_body(doc, **kwargs).body


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
    """Stands in for audit_report.run_cmd, recording every command and replying by rule.

    `failures` maps a command fragment to the return code that command should
    produce: with `check=True` it raises CalledProcessError exactly as
    subprocess would, and with `check=False` it returns the non-zero result.
    Without it every failure path in the harness is untestable, because a
    recorder that always succeeds can only ever exercise the happy path.
    """

    def __init__(self, replies=None, failures=None):
        self.calls: list[list[str]] = []
        self.cwds: list[str | None] = []
        self.replies = replies or {}
        self.failures = failures or {}
        # `git diff --cached --quiet` is the harness's commit classifier: rc 0
        # is "nothing staged, the fix is already on main", rc 1 is "there is a
        # commit to make". Defaulting to rc 0 like everything else would make
        # every remediation test silently take the no-op path.
        self.staged = True

    def __call__(self, cmd, *, check=True, capture=True, cwd=None):
        self.calls.append(list(cmd))
        self.cwds.append(None if cwd is None else str(cwd))
        joined = " ".join(cmd)
        for key, code in self.failures.items():
            if key in joined:
                if check:
                    raise CalledProcessError(code, cmd, "", "simulated failure")
                return CompletedProcess(cmd, code, "", "simulated failure")
        if "diff --cached --quiet" in joined:
            return CompletedProcess(cmd, 1 if self.staged else 0, "", "")
        self._simulate_clone(cmd)
        for key, payload in self.replies.items():
            if key in joined:
                return CompletedProcess(cmd, 0, payload, "")
        return CompletedProcess(cmd, 0, "", "")

    @staticmethod
    def _simulate_clone(cmd):
        """Make a recorded `git clone` leave a working tree behind.

        `gitops_workspace.ensure_workspace` verifies the clone produced a `.git`
        rather than trusting the exit code, so a recorder that only says "rc 0"
        would trip that guard on every run. Reproducing the one filesystem
        effect the real command has keeps the guard live — a clone the recorder
        is told to fail still leaves nothing, and still raises.
        """
        if cmd[:2] != ["git", "clone"] or len(cmd) < 3:
            return
        destination = Path(cmd[-1])
        (destination / ".git").mkdir(parents=True, exist_ok=True)

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
        """monkeypatch.setattr(audit_report, name, value), undone at teardown."""
        patcher = patch.object(audit_report, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, argv):
        """Invoke the CLI, capturing stdout/stderr into self.out / self.err."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = audit_report.main(argv)
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
    """Wires audit_report's I/O seam to a recorder and its two PVC paths to temp dirs.

    The workspace is *not* stubbed out. `ensure_workspace` is the code that was
    missing entirely — nothing in the pod ever cloned the GitOps repository, so
    every git call the harness made ran outside a working tree — and a seam
    that skips it would leave the replacement just as unexercised. It runs for
    real here, against a recorder whose `git clone` materialises a tree, so the
    clone-or-fetch decision and the identity configuration are covered.

    `self.workspace` is where the audit's own files land: a remediation path
    written with `self.touch(...)` has to be under the clone, because that is
    the tree `git add` stages from.
    """

    def setUp(self):
        super().setUp()
        self.harness = Recorder()
        self.gitops_root = self.tmp_path / "gitops"
        self.workspace = self.gitops_root / "acme__fleet"
        self.patch_attr("GITOPS_WORKSPACE", str(self.gitops_root))
        self.patch_attr("SCRATCH_DIR", str(self.tmp_path / "scratch"))
        self.patch_attr("run_cmd", self.harness)
        self.patch_attr("refresh_credentials", lambda repo=None: None)
        self.patch_attr("resolve_repo", lambda: "acme/fleet")
        self.patch_attr("repo_root", lambda: self.workspace)
        # Most tests describe a pod that has audited before, so the clone
        # already exists and `ensure_workspace` takes the fetch path. Tests
        # about the first run call `self.unclone()` to remove it.
        (self.workspace / ".git").mkdir(parents=True)

    def unclone(self):
        """Put the workspace back to how a freshly started pod finds it."""
        shutil.rmtree(self.workspace)

    def touch(self, relative):
        """Write a remediation file where the harness will look for it."""
        target = self.workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# remediation\n", encoding="utf-8")
        return target


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


class TestRenderBody(unittest.TestCase):
    def test_renders_scope_findings_and_footer(self):
        doc = make_doc()
        body = render_body(doc, generated_at=NOW)
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
        body = render_body(make_doc(), generated_at=NOW)
        self.assertIn("`/remediate <finding-id>`", body)
        self.assertIn("write access", body)

    def test_body_names_no_staged_files(self):
        # The ledger is an issue: it has no diff, so it must never claim one.
        body = render_body(make_doc(), generated_at=NOW)
        self.assertNotIn("Remediation files in this PR", body)

    def test_evidence_command_is_fenced(self):
        body = render_body(make_doc(), generated_at=NOW)
        self.assertIn("```bash\nkubectl get networkpolicy -n payments\n```", body)

    def test_severity_groups_ordered_critical_major_minor(self):
        body = render_body(
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
        body = render_body(
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
        body = render_body(doc, generated_at=NOW)
        self.assertIn("### Skipped", body)
        self.assertIn("**Coverage is partial.**", body)
        self.assertIn("| `dr-west` | control plane unreachable |", body)

    def test_no_skipped_section_when_none_skipped(self):
        body = render_body(make_doc(), generated_at=NOW)
        self.assertNotIn("### Skipped", body)
        self.assertNotIn("Coverage is partial", body)

    def test_gcloud_remediation_shows_command_and_stages_nothing(self):
        finding = make_finding(
            remediation={
                "kind": "gcloud",
                "note": "gcloud container clusters update prod-us-east --enable-shielded-nodes",
            }
        )
        body = render_body(
            make_doc(findings=[finding]), generated_at=NOW
        )
        self.assertIn("- **Remediation (gcloud):**", body)
        self.assertIn("gcloud container clusters update prod-us-east", body)
        self.assertEqual(audit_report.manifest_paths([finding]), [])

    def test_manifest_remediation_links_the_path(self):
        body = render_body(make_doc(), generated_at=NOW)
        self.assertIn(
            "[`clusters/prod-us-east/payments-netpol.yaml`]"
            "(clusters/prod-us-east/payments-netpol.yaml)",
            body,
        )

    def test_cluster_scoped_finding_renders_without_namespace(self):
        body = render_body(
            make_doc(findings=[make_finding(namespace="", obj="ClusterRole/admin")]),
            generated_at=NOW,
        )
        self.assertIn("_cluster-scoped_", body)

    def test_body_is_deterministic_regardless_of_input_order(self):
        first = render_body(
            make_doc(findings=THREE_SEVERITIES), generated_at=NOW
        )
        second = render_body(
            make_doc(findings=list(reversed(THREE_SEVERITIES))),
            generated_at=NOW,
        )
        self.assertEqual(first, second)

    def test_title_and_commit_subject(self):
        self.assertEqual(
            audit_report.issue_title(AUDIT, THREE_SEVERITIES),
            "[audit] Security & RBAC Posture Audit — 4 findings (2 critical)",
        )
        self.assertEqual(
            audit_report.commit_subject(AUDIT, THREE_SEVERITIES),
            "chore(audit): compliance-audit — 4 findings (2 critical, 1 major, 1 minor)",
        )

    def test_single_finding_is_not_pluralised(self):
        one = [make_finding(fid="only-one")]
        self.assertEqual(
            audit_report.issue_title(AUDIT, one),
            "[audit] Security & RBAC Posture Audit — 1 finding (1 critical)",
        )
        self.assertEqual(
            audit_report.commit_subject(AUDIT, one),
            "chore(audit): compliance-audit — 1 finding (1 critical, 0 major, 0 minor)",
        )
        body = render_body(
            make_doc(findings=one), generated_at=NOW
        )
        self.assertIn("1 finding: 1 critical, 0 major, 0 minor.", body)
        self.assertNotIn("1 findings", body)

    def test_zero_findings_title_is_pluralised(self):
        self.assertEqual(
            audit_report.issue_title(AUDIT, []),
            "[audit] Security & RBAC Posture Audit — 0 findings (0 critical)",
        )

    def test_excerpt_is_trimmed(self):
        long_excerpt = "\n".join(f"line {i}" for i in range(200))
        trimmed = audit_report.trim_excerpt(long_excerpt)
        self.assertLessEqual(trimmed.count("\n"), audit_report.MAX_EXCERPT_LINES)
        self.assertIn("excerpt truncated", trimmed)

    def test_excerpt_containing_a_fence_does_not_break_out(self):
        finding = make_finding(excerpt="```\nnested fence\n```")
        body = render_body(
            make_doc(findings=[finding]), generated_at=NOW
        )
        self.assertIn("````text", body)


# --------------------------------------------------------------------------- #
# Hidden delta block
# --------------------------------------------------------------------------- #


class TestDeltaBlock(unittest.TestCase):
    def test_block_is_sorted_and_exact(self):
        self.assertEqual(
            audit_report.delta_block(["b", "a"]), '<!-- audit-findings: ["a","b"] -->'
        )

    def test_round_trip(self):
        ids = ["zeta", "alpha", "mid"]
        body = render_body(
            make_doc(findings=[make_finding(fid=i) for i in ids]),
            generated_at=NOW,
        )
        self.assertEqual(audit_report.parse_delta_block(body), sorted(ids))

    def test_missing_or_broken_block_parses_as_empty(self):
        self.assertEqual(audit_report.parse_delta_block(""), [])
        self.assertEqual(audit_report.parse_delta_block(None), [])
        self.assertEqual(audit_report.parse_delta_block("no marker here"), [])
        self.assertEqual(
            audit_report.parse_delta_block("<!-- audit-findings: [oops] -->"), []
        )

    def test_compute_delta(self):
        new, resolved = audit_report.compute_delta(["a", "b"], ["b", "c"])
        self.assertEqual(new, ["c"])
        self.assertEqual(resolved, ["a"])

    def test_delta_across_two_rendered_runs(self):
        run_one = render_body(
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
        run_two = render_body(run_two_doc, generated_at=NOW)

        previous_ids = audit_report.parse_delta_block(run_one)
        current_ids = audit_report.parse_delta_block(run_two)
        new, resolved = audit_report.compute_delta(previous_ids, current_ids)
        self.assertEqual(new, ["c"])
        self.assertEqual(resolved, ["a"])

        titles = audit_report.parse_finding_titles(run_one)
        self.assertEqual(titles["a"], "Alpha finding")

        comment = audit_report.render_delta_comment(
            AUDIT, new, resolved, run_two_doc["findings"], titles, NOW
        )
        self.assertIn("**1 new**", comment)
        self.assertIn("Charlie finding", comment)
        self.assertIn("**1 resolved**", comment)
        # Resolved findings are named by the title recovered from the old body.
        self.assertIn("Alpha finding", comment)

    def test_no_comment_when_nothing_changed(self):
        self.assertIsNone(audit_report.render_delta_comment(AUDIT, [], [], [], {}, NOW))


# --------------------------------------------------------------------------- #
# Schema validation
# --------------------------------------------------------------------------- #


class TestValidation(unittest.TestCase):
    def test_valid_document_passes(self):
        self.assertEqual(audit_report.validate_findings(make_doc(), AUDIT)["audit"], AUDIT)

    def test_zero_findings_is_valid(self):
        audit_report.validate_findings(make_doc(findings=[]), AUDIT)

    def test_unknown_audit_id_rejected(self):
        with self.assertRaisesRegex(audit_report.ValidationError, "unknown audit id"):
            audit_report.validate_audit_id("not-an-audit")

    def test_audit_id_mismatch_rejected(self):
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(make_doc(audit="obtainability-audit"), AUDIT)
        self.assertIn("audit:", str(exc.exception))
        self.assertIn("obtainability-audit", str(exc.exception))

    def test_empty_scope_clusters_rejected(self):
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(make_doc(clusters=[]), AUDIT)
        self.assertIn("scope.clusters", str(exc.exception))
        self.assertIn("not a clean run", str(exc.exception))

    def test_missing_evidence_command_rejected(self):
        doc = make_doc(findings=[make_finding(), make_finding(fid="second")])
        del doc["findings"][1]["evidence"]["command"]
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(doc, AUDIT)
        self.assertIn("findings[1].evidence.command", str(exc.exception))

    def test_empty_evidence_command_rejected(self):
        doc = make_doc(findings=[make_finding(command="   ")])
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(doc, AUDIT)
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
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(doc, AUDIT)
        self.assertIn("findings[2].id", str(exc.exception))
        self.assertIn("findings[0]", str(exc.exception))

    def test_manifest_without_path_rejected(self):
        doc = make_doc(
            findings=[make_finding(remediation={"kind": "manifest", "note": "fix it"})]
        )
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(doc, AUDIT)
        self.assertIn("findings[0].remediation.path", str(exc.exception))

    def test_gcloud_with_path_rejected(self):
        doc = make_doc(
            findings=[
                make_finding(
                    remediation={"kind": "gcloud", "path": "a.yaml", "note": "n"}
                )
            ]
        )
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(doc, AUDIT)
        self.assertIn("findings[0].remediation.path", str(exc.exception))

    def test_bad_severity_rejected(self):
        doc = make_doc(findings=[make_finding(severity="catastrophic")])
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(doc, AUDIT)
        self.assertIn("findings[0].severity", str(exc.exception))

    def test_bad_remediation_kind_rejected(self):
        doc = make_doc(
            findings=[make_finding(remediation={"kind": "ansible", "note": "n"})]
        )
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(doc, AUDIT)
        self.assertIn("findings[0].remediation.kind", str(exc.exception))

    def test_empty_namespace_allowed(self):
        audit_report.validate_findings(
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
                with self.assertRaises(audit_report.ValidationError) as exc:
                    audit_report.validate_findings(doc, AUDIT)
                self.assertIn("findings[0].remediation.path", str(exc.exception))

    def test_findings_must_be_a_list(self):
        doc = make_doc()
        doc["findings"] = {"nope": True}
        with self.assertRaisesRegex(audit_report.ValidationError, "findings:"):
            audit_report.validate_findings(doc, AUDIT)

    def test_skipped_entry_needs_a_reason(self):
        doc = make_doc(skipped=[{"cluster": "dr-west"}])
        with self.assertRaises(audit_report.ValidationError) as exc:
            audit_report.validate_findings(doc, AUDIT)
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
        for audit_id, human in audit_report.AUDITS.items():
            if audit_id in names:
                with self.subTest(audit=audit_id):
                    self.assertEqual(
                        human,
                        names[audit_id],
                        f"audit_report.AUDITS[{audit_id!r}] is {human!r} but "
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
                    audit_report.assert_pushable(branch)

    def test_remediation_branch_is_pushable(self):
        # The audit report branch is gone; the only branch the harness ever
        # pushes is a remediation branch, so that is what the guard must clear.
        for audit_id in audit_report.AUDITS:
            with self.subTest(audit=audit_id):
                branch = audit_report.group_branch_for(
                    audit_id, [manifest_finding("a", "clusters/prod/netpol.yaml")]
                )
                self.assertTrue(branch.startswith(f"platform-agent/fix-{audit_id}-"))
                self.assertEqual(audit_report.assert_pushable(branch), branch)


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
            audit_report.manifest_paths(findings),
            [
                "clusters/prod-us-east/payments-netpol.yaml",
                "clusters/stage-eu/psp.yaml",
            ],
        )

    def test_git_add_command_is_explicit(self):
        cmd = audit_report.build_git_add_command(["a.yaml", "b.yaml"])
        self.assertEqual(
            cmd,
            ["git", "--literal-pathspecs", "add", "--", "a.yaml", "b.yaml"],
        )

    def test_literal_pathspecs_precedes_the_subcommand(self):
        # `git add --literal-pathspecs` is an error: the flag is git-level, so
        # it has to sit before `add` or the whole guard fails at runtime.
        cmd = audit_report.build_git_add_command(["a.yaml"])
        self.assertLess(cmd.index("--literal-pathspecs"), cmd.index("add"))

    def test_wildcard_pathspecs_refused(self):
        for pathspec in (".", "-A", "--all", "-a", "*", ":/"):
            with self.subTest(pathspec=pathspec):
                with self.assertRaisesRegex(ValueError, "wildcard pathspec"):
                    audit_report.build_git_add_command([pathspec])

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
                with self.assertRaises(audit_report.ValidationError):
                    audit_report.validate_findings(
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

        prefix = audit_report.build_git_add_command(["one.yaml"])[1:3]
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
            audit_report.build_git_add_command([])


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
        # Nothing here is auto-promotable — the manifest findings are below
        # `critical` and the one critical is a `gcloud` remediation, which has
        # no file to put in a pull request. That isolates the reporting path,
        # which is what this test is about; auto-promotion has its own.
        doc = make_doc(
            findings=[
                make_finding(fid="a", severity="major"),
                make_finding(fid="b", severity="major"),  # duplicate path
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
                    remediation={"kind": "gcloud", "note": "gcloud x"},
                ),
            ]
        )

        rc = self.run_finish(doc)
        self.assertEqual(rc, 0)

        # A report is an issue now: no branch, no staging, no commit, no push —
        # and `finish` does not check anything out either. It reattaches to the
        # tree `start` prepared, because the audit's remediation manifests are
        # sitting untracked in it.
        self.assertEqual(self.git_add_calls(self.harness), [])
        self.assertFalse(self.harness.matching("git", "commit"))
        self.assertFalse(self.harness.matching("git", "push"))
        self.assertEqual(self.harness.matching("git", "checkout"), [])

        joined = [" ".join(c) for c in self.harness.calls]
        self.assertNotIn("git clean -fdq", joined)
        self.assertNotIn("git reset --hard --quiet", joined)

        create = self.harness.matching("issue", "create")[0]
        self.assertIn("--label", create)
        self.assertIn("agent:audit", create)
        self.assertIn("audit:compliance-audit", create)
        self.assertIn("--body-file", create)
        self.assertFalse(self.harness.matching("issue", "edit", "--title"))
        # The whole point of the split: reporting never *writes* a pull
        # request. It still reads them — that is how a finding learns whether
        # a fix is already in flight.
        for verb in ("create", "edit", "close", "comment"):
            self.assertEqual(self.harness.gh_calls("pr", verb), [], verb)

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
                "partial": False,
                "coverage_gaps": [],
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
        previous_body = render_body(
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

        self.assertTrue(self.harness.gh_calls("issue", "comment", "42"))

        self.assertEqual(
            self.stdout_json(),
            {
                "status": "UPDATED",
                "issue_url": "https://github.com/acme/fleet/issues/42",
                "new": 1,
                "resolved": 1,
                "prs_opened": [],
                "prs_closed": [],
                "partial": False,
                "coverage_gaps": [],
            },
        )

    def test_no_comment_when_findings_unchanged(self):
        doc = make_doc()
        previous_body = render_body(doc, generated_at=NOW)
        self.harness.replies = {
            "issue list": self.issue_list(),
            "--json body": json.dumps({"body": previous_body}),
        }
        self.touch("clusters/prod-us-east/payments-netpol.yaml")

        self.run_finish(doc)

        # Body still refreshed, but silence when nothing changed.
        self.assertTrue(self.harness.matching("issue", "edit", "--title"))
        self.assertFalse(self.harness.gh_calls("issue", "comment"))
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

        self.assertFalse(self.harness.gh_calls("issue", "comment"))
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

    def test_a_missing_remediation_file_degrades_one_finding_not_the_report(self):
        # This used to abort the run. One finding whose promised manifest the
        # audit forgot to write would suppress the other nine criticals — the
        # report is the thing with value, and it was the thing thrown away.
        self.harness.replies = {"issue list": "[]"}
        # Deliberately do NOT create the manifest on disk.
        rc = self.run_finish(make_doc())
        self.assertEqual(rc, 0)
        self.assertIn("remediation file is missing", self.err)
        self.assertTrue(self.harness.matching("issue", "create"))
        # Degraded to manual, so it must not become a pull request either.
        self.assertEqual(self.harness.gh_calls("pr", "create"), [])

    def test_a_degraded_finding_says_why_it_has_no_pull_request(self):
        self.harness.replies = {"issue list": "[]"}
        findings = list(make_doc()["findings"])
        audit_report.degrade_missing_remediations(findings, self.workspace)
        self.assertEqual(findings[0]["remediation"]["kind"], "manual")
        self.assertEqual(findings[0]["remediation"]["path"], "")
        self.assertIn("did not write it", findings[0]["remediation"]["note"])

    def test_a_present_remediation_file_is_left_alone(self):
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        findings = list(make_doc()["findings"])
        self.assertEqual(
            audit_report.degrade_missing_remediations(findings, self.workspace), []
        )
        self.assertEqual(findings[0]["remediation"]["kind"], "manifest")


class TestFinishClean(HarnessTestCase):
    def test_clean_run_closes_the_open_ledger_as_completed(self):
        previous_body = render_body(
            make_doc(findings=[make_finding(fid="a"), make_finding(fid="b")]),
            generated_at=NOW,
        )
        self.harness.replies = {
            "issue list": self.issue_list(),
            "--json body": json.dumps({"body": previous_body}),
        }

        rc = self.run_finish(make_doc(findings=[]))
        self.assertEqual(rc, 0)

        self.assertTrue(self.harness.gh_calls("issue", "comment", "42"))
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
                "partial": False,
                "coverage_gaps": [],
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
        self.assertFalse(self.harness.gh_calls("issue", "comment"))
        self.assertEqual(
            self.stdout_json(),
            {
                "status": "CLEAN",
                "issue_url": None,
                "new": 0,
                "resolved": 0,
                "prs_opened": [],
                "prs_closed": [],
                "partial": False,
                "coverage_gaps": [],
            },
        )

    def test_clean_comment_names_date_and_scope(self):
        comment = audit_report.render_clean_comment(AUDIT, make_doc(findings=[]), NOW)
        self.assertIn("2026-08-01 09:30 UTC", comment)
        self.assertIn("0 findings", comment)
        self.assertIn("`prod-us-east`", comment)
        self.assertIn("`stage-eu`", comment)
        self.assertIn("closed as completed", comment)

    def test_clean_comment_over_a_gap_does_not_announce_a_close(self):
        # The ledger stays open over a coverage gap, so a comment that says it is
        # "being closed as completed" is a statement the reader can check and find
        # false — on the very issue it is posted to.
        doc = make_doc(
            findings=[],
            skipped=[{"cluster": "prod-eu-1", "reason": "API server unreachable"}],
        )
        comment = audit_report.render_clean_comment(AUDIT, doc, NOW)
        self.assertNotIn("closing", comment)
        self.assertNotIn("closed as completed", comment)
        self.assertIn("did not see the whole fleet", comment)
        self.assertIn("the ledger stays open", comment.lower())
        self.assertIn("prod-eu-1", comment)
        self.assertIn("API server unreachable", comment)

    def test_clean_comment_treats_a_limitation_as_a_gap_too(self):
        # `limitations` was invisible to this comment: only `scope.skipped` was
        # rendered, so a cluster that was read but not fully checked produced an
        # unqualified all-clear.
        doc = make_doc(
            findings=[],
            clusters=[
                {
                    "name": "prod-us-east",
                    "location": "us-east1",
                    "project": "acme",
                    "limitations": "Autopilot: node-level checks did not run",
                }
            ],
        )
        comment = audit_report.render_clean_comment(AUDIT, doc, NOW)
        self.assertNotIn("closed as completed", comment)
        self.assertIn("Autopilot: node-level checks did not run", comment)


# --------------------------------------------------------------------------- #
# start
# --------------------------------------------------------------------------- #


class TestStart(HarnessTestCase):
    def setUp(self):
        super().setUp()
        # handle_start pre-creates /opt/data/scratch; keep the tests off the real FS.
        patcher = patch.object(audit_report.os, "makedirs", lambda *a, **k: None)
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
                "workspace": str(self.workspace),
                "findings_path": str(self.tmp_path / "findings_compliance-audit.json"),
                "pending_remediation_requests": [],
            },
        )

    def test_the_workspace_is_named_so_manifests_can_be_written_into_it(self):
        # The agent does not start in a working tree, so a `remediation.path`
        # is meaningless unless `start` says what it is relative to.
        self.harness.replies = {"issue list": "[]"}
        self.run_main(["start", "--audit", AUDIT])
        reported = Path(json.loads(self.out)["workspace"])
        self.assertEqual(reported, self.workspace)
        self.assertTrue((reported / ".git").exists())

    def test_null_issue_when_none_open(self):
        self.harness.replies = {"issue list": "[]"}
        self.run_main(["start", "--audit", "obtainability-audit"])
        self.assertIsNone(json.loads(self.out)["issue"])

    def test_no_report_branch_is_created(self):
        # The report branch is gone. `start` establishes the GitOps clone and
        # leaves it on main; it never cuts a branch of its own and never
        # pushes.
        self.harness.replies = {"issue list": "[]"}
        self.run_main(["start", "--audit", AUDIT])
        checkouts = self.harness.matching("git", "checkout")
        self.assertEqual(checkouts, [["git", "checkout", "-B", "main", "origin/main"]])
        self.assertFalse(self.harness.matching("git", "push"))
        self.assertFalse(self.harness.matching("git", "commit"))

    def test_the_gitops_clone_is_established_before_github_is_read(self):
        # Every git and gh call the harness makes runs inside this clone. It
        # did not exist: nothing in the pod ever cloned the GitOps repository,
        # so `git rev-parse --show-toplevel` failed and no remediation pull
        # request could ever have been opened.
        self.unclone()
        self.harness.replies = {"issue list": "[]"}
        self.run_main(["start", "--audit", AUDIT])
        clones = [c for c in self.harness.calls if c[:2] == ["git", "clone"]]
        self.assertEqual(len(clones), 1)
        self.assertEqual(clones[0][-1], str(self.workspace))
        self.assertTrue((self.workspace / ".git").is_dir())
        self.assertTrue(self.harness.matching("git", "config", "user.email"))

    def test_a_second_run_fetches_instead_of_cloning_again(self):
        self.unclone()
        self.harness.replies = {"issue list": "[]"}
        self.run_main(["start", "--audit", AUDIT])
        self.harness.calls.clear()
        self.run_main(["start", "--audit", AUDIT])
        self.assertFalse([c for c in self.harness.calls if c[:2] == ["git", "clone"]])
        self.assertTrue(self.harness.matching("git", "fetch"))

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
                "audit:remediation",
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

        def explode(*_args, **_kwargs):
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
        return render_body(doc, generated_at=NOW)

    def test_body_stays_under_the_github_limit_at_250_findings(self):
        body = self.render(make_doc(findings=bulk_findings(250)))
        self.assertLess(len(body), audit_report.MAX_BODY_CHARS)

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
        title = audit_report.issue_title(AUDIT, findings)
        self.assertIn("250 findings", title)
        # The rendered body must not silently disagree with the title.
        self.assertIn("250", body)

    def test_delta_block_lists_exactly_the_rendered_ids(self):
        findings = bulk_findings(250)
        body = self.render(make_doc(findings=findings))
        recorded = audit_report.parse_delta_block(body)
        ordered = [f["id"] for f in audit_report.sort_findings(findings)]

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
        self.assertLess(len(body), audit_report.MAX_BODY_CHARS)

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
        self.assertLess(len(body), audit_report.MAX_BODY_CHARS)
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
        comment = audit_report.render_clean_comment(AUDIT, doc, NOW)
        self.assertLess(len(comment), audit_report.MAX_BODY_CHARS)

    def test_delta_comment_stays_under_the_limit(self):
        # Newly reachable: capping the body means N is no longer pinned under
        # ~67 by the body failing first, so this path stops being dead code.
        findings = bulk_findings(250)
        comment = audit_report.render_delta_comment(
            AUDIT,
            [f["id"] for f in findings],
            [f"gone-{i:04d}" for i in range(250)],
            findings,
            {f["id"]: f["title"] for f in findings},
            NOW,
        )
        self.assertLess(len(comment), audit_report.MAX_BODY_CHARS)

    def test_long_command_is_trimmed(self):
        finding = make_finding(command="kubectl get pods " + "x" * 5000)
        rendered = "\n".join(audit_report.render_finding(finding))
        self.assertLess(len(rendered), 4000)
        self.assertIn("truncated", rendered.lower())

    def test_selection_is_a_prefix_of_the_sorted_order(self):
        findings = bulk_findings(3, severity="minor") + bulk_findings(
            2, severity="critical", prefix="c"
        )
        rendered, omitted = audit_report.select_rendered_findings(findings, 1)
        self.assertEqual(len(rendered), 1)
        self.assertEqual(rendered[0]["severity"], "critical")
        self.assertEqual(len(omitted), 4)

    def test_at_least_one_finding_always_renders(self):
        rendered, _ = audit_report.select_rendered_findings(bulk_findings(5), 0)
        self.assertEqual(len(rendered), 1)


# --------------------------------------------------------------------------- #
# Schema — recommendation, limitations, and the finding-id charset
# --------------------------------------------------------------------------- #


class TestRecommendation(BaseTestCase):
    def assert_rejected(self, recommendation, pattern="recommendation"):
        doc = make_doc(findings=[make_finding(recommendation=recommendation)])
        with self.assertRaisesRegex(audit_report.ValidationError, pattern):
            audit_report.validate_findings(doc, AUDIT)

    def test_missing_recommendation_is_rejected(self):
        doc = make_doc(findings=[make_finding()])
        del doc["findings"][0]["recommendation"]
        with self.assertRaisesRegex(audit_report.ValidationError, "recommendation"):
            audit_report.validate_findings(doc, AUDIT)

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
            audit_report.render_finding(
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
        self.assertTrue(audit_report.validate_findings(doc, AUDIT))

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
        with self.assertRaisesRegex(audit_report.ValidationError, "limitations"):
            audit_report.validate_findings(doc, AUDIT)

    def test_a_cluster_cannot_be_both_audited_and_skipped(self):
        # The Autopilot false-all-clear: the collision this field exists to end.
        doc = make_doc(
            clusters=[
                {"name": "prod-us-east", "location": "us-east1", "project": "acme"}
            ],
            skipped=[{"cluster": "prod-us-east", "reason": "Autopilot"}],
        )
        with self.assertRaises(audit_report.ValidationError):
            audit_report.validate_findings(doc, AUDIT)

    def test_duplicate_skipped_entries_are_rejected(self):
        doc = make_doc(
            findings=[],
            skipped=[
                {"cluster": "dr-west", "reason": "unreachable"},
                {"cluster": "dr-west", "reason": "unreachable again"},
            ],
        )
        with self.assertRaises(audit_report.ValidationError):
            audit_report.validate_findings(doc, AUDIT)

    def test_a_finding_cannot_name_a_skipped_cluster(self):
        doc = make_doc(
            findings=[make_finding(cluster="dr-west")],
            skipped=[{"cluster": "dr-west", "reason": "control plane unreachable"}],
        )
        with self.assertRaises(audit_report.ValidationError):
            audit_report.validate_findings(doc, AUDIT)


class TestFindingIdCharset(BaseTestCase):
    def test_usable_ids_are_accepted(self):
        for fid in ("a", "netpol-missing-payments", "v1.2.3-drift", "a" * 100):
            with self.subTest(fid=fid):
                self.assertEqual(audit_report.validate_finding_id(fid, "where"), fid)

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
                with self.assertRaises(audit_report.ValidationError):
                    audit_report.validate_finding_id(fid, "where")

    def test_accepted_ids_survive_git_check_ref_format(self):
        # The rule is only worth anything if git agrees with it.
        git = shutil.which("git")
        if git is None:  # pragma: no cover - git is present locally and in CI
            self.skipTest("git not on PATH")
        for fid in ("a", "netpol-missing-payments", "v1.2.3-drift", "a" * 100):
            with self.subTest(fid=fid):
                branch = audit_report.group_branch_for(AUDIT, [make_finding(fid=fid)])
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
        groups = audit_report.remediation_groups(
            [manifest_finding("a", "x.yaml"), manifest_finding("b", "y.yaml")]
        )
        self.assertEqual(self.ids(groups), [["a"], ["b"]])

    def test_a_shared_path_merges_two_findings(self):
        # compliance_audit_sop.md points every finding in a namespace at one
        # shared default-sa-automount.yaml, so this is the common case.
        groups = audit_report.remediation_groups(
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
            audit_report, "_finding_paths", lambda f: paths[f["id"]]
        ):
            groups = audit_report.remediation_groups(findings)
        self.assertEqual(self.ids(groups), [["a", "b", "c"]])

    def test_grouping_is_independent_of_input_order(self):
        findings = [
            manifest_finding("c", "other.yaml"),
            manifest_finding("b", "shared.yaml"),
            manifest_finding("a", "shared.yaml"),
        ]
        self.assertEqual(
            self.ids(audit_report.remediation_groups(findings)),
            [["a", "b"], ["c"]],
        )

    def test_non_manifest_findings_do_not_form_groups(self):
        groups = audit_report.remediation_groups(
            [
                make_finding(fid="a", remediation={"kind": "gcloud", "note": "g"}),
                make_finding(fid="b", remediation={"kind": "manual", "note": "m"}),
            ]
        )
        self.assertEqual(groups, [])

    def test_branch_is_keyed_on_the_paths_not_the_finding_ids(self):
        # Finding ids are regenerated from scratch every run. Keying the branch
        # on one of them means that the day a group's lowest id resolves, the
        # survivors rename their branch, the open pull request is orphaned, and
        # a duplicate opens against the same file. The path set is what makes
        # the group a group, and it is stable across id churn.
        first = audit_report.group_branch_for(
            AUDIT, [manifest_finding("zeta", "s.yaml"), manifest_finding("alpha", "s.yaml")]
        )
        after_alpha_resolved = audit_report.group_branch_for(
            AUDIT, [manifest_finding("zeta", "s.yaml")]
        )
        self.assertEqual(first, after_alpha_resolved)
        self.assertTrue(first.startswith(f"platform-agent/fix-{AUDIT}-s-"))

    def test_a_different_path_set_gets_a_different_branch(self):
        one = audit_report.group_branch_for(AUDIT, [manifest_finding("a", "s.yaml")])
        two = audit_report.group_branch_for(AUDIT, [manifest_finding("a", "t.yaml")])
        self.assertNotEqual(one, two)

    def test_branch_ordering_within_a_group_does_not_change_the_name(self):
        group = [manifest_finding("a", "b.yaml"), manifest_finding("b", "a.yaml")]
        self.assertEqual(
            audit_report.group_branch_for(AUDIT, group),
            audit_report.group_branch_for(AUDIT, list(reversed(group))),
        )

    def test_an_empty_group_cannot_be_named(self):
        with self.assertRaises(ValueError):
            audit_report.group_branch_for(AUDIT, [])

    def test_group_paths_are_deduplicated_and_sorted(self):
        group = [manifest_finding("a", "s.yaml"), manifest_finding("b", "s.yaml")]
        self.assertEqual(audit_report.group_paths(group), ["s.yaml"])


# --------------------------------------------------------------------------- #
# §4 finding states and promotion (§3.1, Q4)
# --------------------------------------------------------------------------- #


class TestFindingState(BaseTestCase):
    def test_all_six_states(self):
        cases = [
            (True, None, audit_report.STATE_OPEN),
            (True, {"state": "OPEN"}, audit_report.STATE_PR_OPEN),
            (True, {"state": "MERGED"}, audit_report.STATE_PR_MERGED_PERSISTS),
            (True, {"state": "CLOSED"}, audit_report.STATE_REFUSED),
            (False, None, audit_report.STATE_RESOLVED),
            (False, {"state": "MERGED"}, audit_report.STATE_RESOLVED_MERGED),
        ]
        for reproduces, pr, expected in cases:
            with self.subTest(reproduces=reproduces, pr=pr):
                self.assertEqual(audit_report.derive_finding_state(reproduces, pr), expected)

    def test_merged_at_counts_as_merged_even_without_a_state(self):
        self.assertEqual(
            audit_report.derive_finding_state(True, {"mergedAt": "2026-08-01T00:00:00Z"}),
            audit_report.STATE_PR_MERGED_PERSISTS,
        )

    def test_every_state_has_a_label(self):
        for state in (
            audit_report.STATE_OPEN,
            audit_report.STATE_PR_OPEN,
            audit_report.STATE_PR_MERGED_PERSISTS,
            audit_report.STATE_RESOLVED_MERGED,
            audit_report.STATE_RESOLVED,
            audit_report.STATE_REFUSED,
        ):
            self.assertIn(state, audit_report.STATE_LABELS)


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
        plan = audit_report.promotion_candidates(findings, {})
        self.assertEqual(plan.promote, ["crit"])
        self.assertEqual(plan.withheld, [])

    def test_a_finding_with_an_existing_pr_is_not_promoted_again(self):
        findings = [manifest_finding("crit", "a.yaml")]
        plan = audit_report.promotion_candidates(
            findings, {"crit": {"state": "CLOSED"}}
        )
        self.assertEqual(plan.promote, [])
        self.assertEqual(plan.withheld, [])

    def test_auto_promotion_is_capped_and_names_the_withheld(self):
        findings = [
            manifest_finding(f"c-{i:02d}", f"{i}.yaml", severity="critical")
            for i in range(9)
        ]
        plan = audit_report.promotion_candidates(findings, {})
        self.assertEqual(len(plan.promote), audit_report.AUTO_PROMOTION_CAP)
        self.assertEqual(len(plan.withheld), 4)
        self.assertEqual(set(plan.promote) & set(plan.withheld), set())

    def test_an_explicit_request_bypasses_the_cap(self):
        findings = [
            manifest_finding(f"c-{i:02d}", f"{i}.yaml", severity="critical")
            for i in range(9)
        ] + [manifest_finding("asked", "asked.yaml", severity="minor")]
        plan = audit_report.promotion_candidates(
            findings, {}, requested=["asked", "c-08"]
        )
        self.assertIn("asked", plan.promote)
        self.assertIn("c-08", plan.promote)
        # The two requested are uncapped; the auto path still yields cap-many.
        self.assertEqual(len(plan.promote), 2 + audit_report.AUTO_PROMOTION_CAP)
        self.assertNotIn("c-08", plan.withheld)

    def test_a_requested_non_manifest_finding_is_not_promoted(self):
        findings = [
            make_finding(fid="g", remediation={"kind": "gcloud", "note": "g"}),
        ]
        plan = audit_report.promotion_candidates(findings, {}, requested=["g"])
        self.assertEqual(plan.promote, [])


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
        return audit_report.parse_remediate_commands(comments, self.findings)

    def test_an_authorized_request_is_accepted(self):
        targets, refusals, _ = self.parse([comment("/remediate netpol-missing")])
        self.assertEqual(targets, ["netpol-missing"])
        self.assertEqual(refusals, [])

    def test_a_commenter_without_write_access_is_refused_once(self):
        targets, refusals, _ = self.parse(
            [comment("/remediate netpol-missing", association="NONE", login="drive-by")]
        )
        self.assertEqual(targets, [])
        self.assertEqual(len(refusals), 1)
        self.assertIn("write access", refusals[0]["reasons"][0])
        self.assertEqual(refusals[0]["comment_id"], "IC_1")

    def test_a_non_manifest_target_is_refused(self):
        targets, refusals, _ = self.parse([comment("/remediate cluster-old")])
        self.assertEqual(targets, [])
        self.assertEqual(len(refusals), 1)
        self.assertIn("gcloud", refusals[0]["reasons"][0])

    def test_an_unknown_target_is_refused(self):
        _, refusals, _ = self.parse([comment("/remediate no-such-finding")])
        self.assertIn("not a finding", refusals[0]["reasons"][0])

    def test_a_fenced_command_never_fires(self):
        body = "Here is how you would ask:\n\n```\n/remediate netpol-missing\n```\n"
        targets, refusals, _ = self.parse([comment(body)])
        self.assertEqual(targets, [])
        self.assertEqual(refusals, [])

    def test_remediate_all_expands_to_promotable_targets_only(self):
        targets, refusals, _ = self.parse([comment("/remediate all")])
        self.assertEqual(targets, ["netpol-missing"])
        self.assertEqual(refusals, [])

    def test_a_command_must_start_the_line(self):
        targets, _, _ = self.parse([comment("maybe we should /remediate netpol-missing")])
        self.assertEqual(targets, [])

    def test_one_refusal_per_comment_not_per_bad_target(self):
        body = "/remediate cluster-old\n/remediate no-such-finding\n"
        _, refusals, _ = self.parse([comment(body)])
        self.assertEqual(len(refusals), 1)
        self.assertEqual(len(refusals[0]["reasons"]), 2)

    def test_targets_are_deduplicated_and_sorted(self):
        targets, _, _ = self.parse(
            [
                comment("/remediate netpol-missing", node_id="IC_1"),
                comment("/remediate netpol-missing", node_id="IC_2"),
            ]
        )
        self.assertEqual(targets, ["netpol-missing"])


class TestMarkers(BaseTestCase):
    def test_persists_marker_round_trips(self):
        body = f"Some text\n\n{audit_report.persists_marker('abc')}\n"
        self.assertTrue(audit_report.has_marker(body, audit_report.PERSISTS_MARKER_RE, "abc"))
        self.assertFalse(audit_report.has_marker(body, audit_report.PERSISTS_MARKER_RE, "xyz"))

    def test_refused_marker_round_trips(self):
        body = f"Reply\n{audit_report.refused_marker('IC_9')}\n"
        self.assertTrue(audit_report.has_marker(body, audit_report.REFUSED_MARKER_RE, "IC_9"))
        self.assertFalse(audit_report.has_marker(body, audit_report.REFUSED_MARKER_RE, "IC_8"))

    def test_absent_body_has_no_marker(self):
        self.assertFalse(audit_report.has_marker(None, audit_report.PERSISTS_MARKER_RE, "abc"))
        self.assertFalse(audit_report.has_marker("", audit_report.PERSISTS_MARKER_RE, "abc"))


class TestDeltaBlockAnchoring(BaseTestCase):
    def test_a_marker_quoted_inside_an_excerpt_cannot_hijack_the_real_block(self):
        # An opener injected mid-line must not start a match that spans into
        # the real block below it.
        body = (
            "Evidence:\n"
            '    text <!-- audit-findings: ["injected"] and more\n'
            "\n"
            + audit_report.delta_block(["real-one", "real-two"])
            + "\n"
        )
        self.assertEqual(audit_report.parse_delta_block(body), ["real-one", "real-two"])


# --------------------------------------------------------------------------- #
# Remediation pull requests — the Tier 2 half of the two-tier model
# --------------------------------------------------------------------------- #


def pr(number, branch, state="OPEN", merged_at=None, body="", url=None):
    return {
        "number": number,
        "headRefName": branch,
        "state": state,
        "mergedAt": merged_at,
        "url": url or f"https://github.com/acme/fleet/pull/{number}",
        "body": body,
    }


class TestSelectPrByHead(BaseTestCase):
    def test_highest_number_wins(self):
        # A branch reused after its first PR merged must report the live one.
        prs = [pr(3, "b"), pr(11, "b"), pr(7, "b")]
        self.assertEqual(audit_report._select_pr_by_head(prs, "b")["number"], 11)

    def test_no_match_is_none(self):
        self.assertIsNone(audit_report._select_pr_by_head([pr(1, "other")], "b"))
        self.assertIsNone(audit_report._select_pr_by_head([], "b"))
        self.assertIsNone(audit_report._select_pr_by_head(None, "b"))

    def test_a_fork_qualified_head_still_matches(self):
        # gh reports `owner:branch` for a cross-repository PR. Accepting the
        # suffix keeps the lookup working if remediation ever moves to a fork,
        # instead of silently reporting every finding as having no PR.
        prs = [pr(4, "adamparco:platform-agent/fix-x")]
        found = audit_report._select_pr_by_head(prs, "platform-agent/fix-x")
        self.assertEqual(found["number"], 4)

    def test_a_bare_substring_does_not_match(self):
        self.assertIsNone(audit_report._select_pr_by_head([pr(4, "xfix-x")], "fix-x"))


class TestReconcileRemediationPrs(BaseTestCase):
    def setUp(self):
        super().setUp()
        # a and b share a path, so they are one group on one branch; c is alone.
        self.findings = [
            make_finding(fid="a"),
            make_finding(fid="b"),
            make_finding(
                fid="c",
                remediation={
                    "kind": "manifest",
                    "path": "clusters/stage-eu/psp.yaml",
                    "note": "n",
                },
            ),
        ]

    def test_one_pr_fans_out_to_every_member_of_its_group(self):
        branch = audit_report.group_branch_for(AUDIT, self.findings[:2])
        by_finding, urls = audit_report.reconcile_remediation_prs(
            AUDIT, self.findings, [pr(9, branch)]
        )
        self.assertEqual(by_finding["a"]["number"], 9)
        self.assertEqual(by_finding["b"]["number"], 9)
        self.assertIsNone(by_finding["c"])
        self.assertEqual(urls["a"], urls["b"])
        self.assertNotIn("c", urls)

    def test_no_prs_leaves_every_finding_unlinked(self):
        by_finding, urls = audit_report.reconcile_remediation_prs(AUDIT, self.findings, [])
        self.assertEqual(set(by_finding), {"a", "b", "c"})
        self.assertTrue(all(v is None for v in by_finding.values()))
        self.assertEqual(urls, {})


class TestOpenRemediationPr(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.group = [make_finding(fid="a")]
        self.path = "clusters/prod-us-east/payments-netpol.yaml"
        self.snapshot = {self.path: b"# fix\n"}
        self.branch = audit_report.group_branch_for(AUDIT, self.group)

    def open_it(self, existing=None):
        # Called directly rather than through main(), so redirect the harness's
        # own log lines instead of letting them print over the test output.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = audit_report.open_remediation_pr(
                "acme/fleet",
                AUDIT,
                self.group,
                snapshot=self.snapshot,
                root=self.workspace,
                issue_number=42,
                existing=existing,
                generated_at=NOW,
            )
        self.err = err.getvalue()
        return result

    def test_branch_commit_push_then_create_in_that_order(self):
        self.harness.replies = {"pr create": "https://github.com/acme/fleet/pull/8\n"}
        url = self.open_it()

        self.assertEqual(url, "https://github.com/acme/fleet/pull/8")
        branch = self.branch
        order = [c for c in self.harness.calls if c[0] in ("git", "gh")]
        self.assertEqual(order[0], ["git", "fetch", "origin", "main"])
        self.assertEqual(
            order[1], ["git", "checkout", "--force", "-B", branch, "origin/main"]
        )
        self.assertEqual(
            order[2], ["git", "--literal-pathspecs", "add", "--", self.path]
        )
        self.assertEqual(order[3], ["git", "diff", "--cached", "--quiet"])
        self.assertEqual(order[4][:2], ["git", "commit"])
        self.assertEqual(order[5], ["git", "push", "-f", "origin", branch])
        self.assertEqual(order[6][:2], ["gh", "pr"])

        # The file the pull request carries comes from the snapshot, not from
        # whatever survived the forced checkout.
        self.assertEqual((self.workspace / self.path).read_bytes(), b"# fix\n")

    def test_create_carries_all_four_labels(self):
        self.harness.replies = {"pr create": "https://github.com/acme/fleet/pull/8\n"}
        self.open_it()
        create = self.harness.gh_calls("pr", "create")[0]
        for label in (
            "agent:audit",
            f"audit:{AUDIT}",
            "audit:remediation",
            "severity:critical",
        ):
            self.assertIn(label, create, label)
        self.assertIn("--base", create)
        self.assertIn("main", create)

    def test_nothing_to_commit_opens_no_pull_request(self):
        # main already carries the fix. Opening a diff-less PR is the exact
        # mistake the ledger split exists to end.
        self.harness.staged = False
        self.assertIsNone(self.open_it())
        self.assertFalse(self.harness.matching("git", "commit"))
        self.assertFalse(self.harness.matching("git", "push"))
        self.assertEqual(self.harness.gh_calls("pr", "create"), [])

    def test_an_unreadable_index_is_never_read_as_already_fixed(self):
        # rc 0 is "nothing staged" and rc 1 is "there is a commit to make".
        # Anything else means git could not read the index — a missing
        # committer identity, a failed hook, a corrupt .git — and inferring
        # "already fixed on main" from it drops the fix on the floor silently,
        # every run, forever.
        self.harness.failures = {"diff --cached --quiet": 128}
        with self.assertRaises(RuntimeError):
            self.open_it()
        self.assertFalse(self.harness.matching("git", "push"))

    def test_an_open_pr_is_edited_not_duplicated(self):
        existing = pr(8, self.branch)
        url = self.open_it(existing=existing)
        self.assertEqual(url, "https://github.com/acme/fleet/pull/8")
        self.assertEqual(self.harness.gh_calls("pr", "create"), [])
        edit = self.harness.gh_calls("pr", "edit")[0]
        self.assertIn("8", edit)
        self.assertIn("--body-file", edit)

    def test_a_closed_pr_on_the_branch_is_replaced_not_reopened(self):
        self.harness.replies = {"pr create": "https://github.com/acme/fleet/pull/9\n"}
        self.open_it(existing=pr(8, self.branch, state="CLOSED"))
        self.assertEqual(self.harness.gh_calls("pr", "edit"), [])
        self.assertEqual(len(self.harness.gh_calls("pr", "create")), 1)


class TestRemediationPrBody(BaseTestCase):
    def test_part_of_not_closes(self):
        # "Closes #N" would retire the ledger the moment one fix merged.
        body = audit_report.render_remediation_pr_body(
            AUDIT, [make_finding(fid="a")], issue_number=42, generated_at=NOW
        )
        self.assertIn("Part of #42", body)
        self.assertNotIn("Closes #42", body)

    def test_body_records_the_findings_it_covers(self):
        group = [make_finding(fid="a"), make_finding(fid="b")]
        body = audit_report.render_remediation_pr_body(
            AUDIT, group, issue_number=42, generated_at=NOW
        )
        self.assertEqual(audit_report.parse_delta_block(body), ["a", "b"])
        self.assertIn("## Files", body)
        self.assertIn("clusters/prod-us-east/payments-netpol.yaml", body)

    def test_title_names_the_head_finding_and_counts_the_rest(self):
        one = [make_finding(fid="a", title="No NetworkPolicy")]
        self.assertEqual(
            audit_report.remediation_pr_title(AUDIT, one),
            "fix(compliance-audit): No NetworkPolicy",
        )
        two = one + [make_finding(fid="b", severity="minor", title="Other")]
        self.assertTrue(
            audit_report.remediation_pr_title(AUDIT, two).endswith("(+1 more)")
        )


class TestStaleClose(HarnessTestCase):
    def close(self, prs, current_ids):
        return audit_report.close_stale_remediation_prs(
            "acme/fleet", AUDIT, prs, current_ids, {"a": "Old title"}, {}, NOW
        )

    def test_closes_and_comments_but_never_deletes_the_branch(self):
        stale = pr(8, "platform-agent/fix-x", body=audit_report.delta_block(["a"]))
        closed = self.close([stale], set())

        self.assertEqual(closed, ["https://github.com/acme/fleet/pull/8"])
        comment = self.harness.gh_calls("pr", "comment")[0]
        self.assertIn("8", comment)
        close = self.harness.gh_calls("pr", "close")[0]
        # The branch outlives the pull request: a returning finding pushes to it.
        self.assertNotIn("--delete-branch", close)
        # Comment before close, so the reason is on the PR when it closes.
        self.assertLess(
            self.harness.calls.index(comment), self.harness.calls.index(close)
        )

    def test_a_pr_with_one_live_finding_stays_open(self):
        live = pr(8, "platform-agent/fix-x", body=audit_report.delta_block(["a", "b"]))
        self.assertEqual(self.close([live], {"b"}), [])
        self.assertEqual(self.harness.gh_calls("pr", "close"), [])

    def test_an_already_closed_pr_is_left_alone(self):
        done = pr(
            8, "platform-agent/fix-x", state="MERGED", body=audit_report.delta_block(["a"])
        )
        self.assertEqual(self.close([done], set()), [])
        self.assertEqual(self.harness.gh_calls("pr", "close"), [])

    def test_a_pr_with_no_hidden_block_is_left_alone(self):
        # Hand-opened, or opened by an older harness: it says nothing about
        # which findings it covers, so closing it would be a guess.
        self.assertEqual(self.close([pr(8, "b", body="hello")], set()), [])
        self.assertEqual(self.harness.gh_calls("pr", "close"), [])


class TestMergedButPersists(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.finding = make_finding(fid="a")
        self.merged = pr(
            8,
            "platform-agent/fix-x",
            state="MERGED",
            merged_at="2026-07-01T00:00:00Z",
        )

    def run_it(self, prs_by_finding):
        audit_report.comment_on_merged_but_persisting(
            "acme/fleet", AUDIT, [self.finding], prs_by_finding, NOW
        )

    def test_comments_once_and_never_reopens(self):
        self.harness.replies = {"--json comments": json.dumps({"comments": []})}
        self.run_it({"a": self.merged})
        comment = self.harness.gh_calls("pr", "comment")[0]
        self.assertIn("8", comment)
        self.assertEqual(self.harness.gh_calls("pr", "reopen"), [])

    def test_silent_when_the_marker_is_already_in_the_pr_body(self):
        self.merged["body"] = f"merged\n{audit_report.persists_marker('a')}\n"
        self.harness.replies = {"--json comments": json.dumps({"comments": []})}
        self.run_it({"a": self.merged})
        self.assertEqual(self.harness.gh_calls("pr", "comment"), [])

    def test_silent_when_the_marker_is_already_in_a_pr_comment(self):
        prior = {"body": f"said it\n{audit_report.persists_marker('a')}\n"}
        self.harness.replies = {"--json comments": json.dumps({"comments": [prior]})}
        self.run_it({"a": self.merged})
        self.assertEqual(self.harness.gh_calls("pr", "comment"), [])

    def test_an_open_pr_is_not_the_persists_case(self):
        self.run_it({"a": pr(8, "platform-agent/fix-x")})
        self.assertEqual(self.harness.gh_calls("pr", "comment"), [])


class TestReplyToRefusals(HarnessTestCase):
    def refusal(self, comment_id="IC_1"):
        return {
            "comment_id": comment_id,
            "author": "drive-by",
            "reasons": ["no write access"],
        }

    def test_one_reply_carrying_the_requesting_comment_id(self):
        audit_report.reply_to_refusals("acme/fleet", 42, [self.refusal()], [], NOW)
        self.assertEqual(len(self.harness.gh_calls("issue", "comment")), 1)

    def test_silent_when_that_comment_was_already_answered(self):
        answered = [{"body": f"earlier\n{audit_report.refused_marker('IC_1')}\n"}]
        audit_report.reply_to_refusals("acme/fleet", 42, [self.refusal()], answered, NOW)
        self.assertEqual(self.harness.gh_calls("issue", "comment"), [])

    def test_a_different_comment_still_gets_its_own_reply(self):
        answered = [{"body": f"earlier\n{audit_report.refused_marker('IC_1')}\n"}]
        audit_report.reply_to_refusals(
            "acme/fleet", 42, [self.refusal("IC_2")], answered, NOW
        )
        self.assertEqual(len(self.harness.gh_calls("issue", "comment")), 1)


class TestAutoPromotionInFinish(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.harness.replies = {
            "issue list": "[]",
            "issue create": "https://github.com/acme/fleet/issues/7\n",
            "pr create": "https://github.com/acme/fleet/pull/8\n",
            "rev-parse --abbrev-ref": "feature-branch\n",
        }

    def test_a_critical_manifest_finding_gets_a_pull_request(self):
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        self.assertEqual(self.run_finish(make_doc()), 0)
        out = self.stdout_json()
        self.assertEqual(out["prs_opened"], ["https://github.com/acme/fleet/pull/8"])
        self.assertEqual(len(self.harness.gh_calls("pr", "create")), 1)

    def test_the_ledger_is_rewritten_once_the_pull_request_exists(self):
        # The body was rendered before the PR had a number, so it could not
        # have linked it. One extra edit beats making a reader wait a day.
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        self.run_finish(make_doc())
        self.assertTrue(self.harness.gh_calls("issue", "edit", "7"))

    def test_a_gcloud_critical_is_never_auto_promoted(self):
        doc = make_doc(
            findings=[
                make_finding(fid="a", remediation={"kind": "gcloud", "note": "x"})
            ]
        )
        self.assertEqual(self.run_finish(doc), 0)
        self.assertEqual(self.harness.gh_calls("pr", "create"), [])

    def test_the_cap_holds_and_the_ledger_names_what_it_withheld(self):
        findings = []
        for i in range(7):
            findings.append(
                make_finding(
                    fid=f"crit-{i}",
                    title=f"Crit {i}",
                    remediation={
                        "kind": "manifest",
                        "path": f"clusters/prod-us-east/f{i}.yaml",
                        "note": "n",
                    },
                )
            )
            self.touch(f"clusters/prod-us-east/f{i}.yaml")

        self.assertEqual(self.run_finish(make_doc(findings=findings)), 0)
        self.assertEqual(
            len(self.harness.gh_calls("pr", "create")), audit_report.AUTO_PROMOTION_CAP
        )
        body = render_body(
            make_doc(findings=findings),
            generated_at=NOW,
            audit_id=AUDIT,
            withheld=["crit-5", "crit-6"],
        )
        self.assertIn("crit-5", body)
        self.assertIn("/remediate", body)

    def test_the_working_tree_is_left_as_it_was_found(self):
        target = self.touch("clusters/prod-us-east/payments-netpol.yaml")
        target.write_bytes(b"original\n")
        self.run_finish(make_doc())
        # Forced checkouts happened, but the caller's branch and file survive.
        self.assertEqual(target.read_bytes(), b"original\n")
        self.assertIn(
            ["git", "checkout", "--force", "feature-branch"], self.harness.calls
        )

    def test_a_failed_pr_create_does_not_fail_the_run(self):
        # The ledger is already published; the finding shows as having no PR
        # and the next run retries. Losing the report costs more.
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        self.harness.failures = {"pr create": 1}
        self.assertEqual(self.run_finish(make_doc()), 0)
        self.assertEqual(self.stdout_json()["prs_opened"], [])
        self.assertIn("could not publish the fix", self.err)


class TestRemediateSubcommand(HarnessTestCase):
    def run_remediate(self, doc, findings, extra=()):
        path = self.write_findings(doc)
        argv = ["remediate", "--audit", AUDIT, "--findings-file", path]
        for fid in findings:
            argv += ["--finding", fid]
        return self.run_main([*argv, *extra])

    def test_an_unknown_id_is_rejected_before_any_side_effect(self):
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        self.assertEqual(self.run_remediate(make_doc(), ["nope"]), 2)
        self.assertIn("not in", self.err)
        self.assertEqual(self.harness.calls, [])

    def test_a_non_manifest_target_is_rejected(self):
        doc = make_doc(
            findings=[
                make_finding(fid="a", remediation={"kind": "manual", "note": "x"})
            ]
        )
        self.assertEqual(self.run_remediate(doc, ["a"]), 2)
        self.assertIn("manifest", self.err)
        self.assertEqual(self.harness.calls, [])

    def test_dry_run_renders_the_body_and_touches_nothing(self):
        rc = self.run_remediate(make_doc(), ["no-network-policy"], ["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.harness.calls, [])
        self.assertIn("## Files", self.out)
        self.assertIn("WOULD OPEN: platform-agent/fix-", self.err)
        # Resolving the ledger number is a gh call, which a dry run may not
        # make — so it says why the link is missing instead of just omitting it.
        self.assertIn("the 'Part of #N' link is omitted", self.err)

    def test_dry_run_links_the_ledger_when_it_is_named(self):
        self.run_remediate(
            make_doc(), ["no-network-policy"], ["--dry-run", "--issue", "42"]
        )
        self.assertIn("Part of #42", self.out)
        self.assertEqual(self.harness.calls, [])

    def test_it_opens_the_pull_request_and_reports_it(self):
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        self.harness.replies = {
            "issue list": self.issue_list(),
            "pr create": "https://github.com/acme/fleet/pull/8\n",
        }
        rc = self.run_remediate(make_doc(), ["no-network-policy"])
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.stdout_json(),
            {
                "status": "REMEDIATED",
                "prs_opened": ["https://github.com/acme/fleet/pull/8"],
                "already_open": [],
                "refused": [],
            },
        )

    def test_one_unwritten_manifest_does_not_sink_the_whole_batch(self):
        # `/remediate all` expands to every id in the document. Answering a
        # request for several fixes with zero, because one manifest was never
        # written, is the least useful outcome available.
        doc = make_doc(
            findings=[
                make_finding(
                    fid="written",
                    remediation={
                        "kind": "manifest",
                        "path": "clusters/prod-us-east/written.yaml",
                        "note": "n",
                    },
                ),
                make_finding(
                    fid="unwritten",
                    remediation={
                        "kind": "manifest",
                        "path": "clusters/prod-us-east/unwritten.yaml",
                        "note": "n",
                    },
                ),
            ]
        )
        self.touch("clusters/prod-us-east/written.yaml")
        self.harness.replies = {
            "issue list": self.issue_list(),
            "pr create": "https://github.com/acme/fleet/pull/8\n",
        }
        rc = self.run_remediate(doc, ["written", "unwritten"])
        self.assertEqual(rc, 0)
        out = self.stdout_json()
        self.assertEqual(out["prs_opened"], ["https://github.com/acme/fleet/pull/8"])
        self.assertEqual(out["refused"], ["unwritten"])
        self.assertIn("REFUSED unwritten", self.err)
        # The refused finding must not reach a branch of its own.
        self.assertFalse(self.harness.matching("checkout", "unwritten"))

    def test_a_batch_with_nothing_left_to_do_is_still_an_error(self):
        # Partial success is worth reporting; total failure reported as exit 0
        # with an empty list would read as "done".
        doc = make_doc(
            findings=[
                make_finding(
                    fid="unwritten",
                    remediation={
                        "kind": "manifest",
                        "path": "clusters/prod-us-east/unwritten.yaml",
                        "note": "n",
                    },
                )
            ]
        )
        self.harness.replies = {"issue list": self.issue_list()}
        self.assertEqual(self.run_remediate(doc, ["unwritten"]), 2)
        self.assertIn("not on disk", self.err)
        self.assertEqual(self.harness.gh_calls("pr", "create"), [])

    def test_an_uncapped_request_beats_the_auto_promotion_cap(self):
        findings = []
        for i in range(7):
            findings.append(
                make_finding(
                    fid=f"crit-{i}",
                    severity="minor",
                    remediation={
                        "kind": "manifest",
                        "path": f"clusters/prod-us-east/f{i}.yaml",
                        "note": "n",
                    },
                )
            )
            self.touch(f"clusters/prod-us-east/f{i}.yaml")
        self.harness.replies = {
            "issue list": self.issue_list(),
            "pr create": "https://github.com/acme/fleet/pull/8\n",
        }
        rc = self.run_remediate(
            make_doc(findings=findings), [f.get("id") for f in findings]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.harness.gh_calls("pr", "create")), 7)


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
        self.assertFalse(self.harness.gh_calls("issue", "comment"))

    def test_a_failed_delta_comment_is_survivable(self):
        # Losing the delta comment costs one notification; aborting would
        # leave the ledger correct but the run marked failed to the cron.
        previous_body = render_body(
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
        # `--add-label` and not the bare `severity:critical`: the remediation
        # `gh pr create` carries the same severity as a plain `--label`, and a
        # substring injection would fire on that instead.
        self.harness.failures = {"--add-label severity:critical": 1}
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


# --------------------------------------------------------------------------- #
# Credential redaction — the backstop the five SOPs promise
# --------------------------------------------------------------------------- #


class TestRedaction(unittest.TestCase):
    def assertRedacted(self, text, secret):
        out = audit_report.redact_secrets(text)
        self.assertNotIn(secret, out)
        self.assertIn(audit_report.REDACTED, out)
        return out

    def test_a_named_credential_field_is_blanked(self):
        for line in (
            "password: hunter2correcthorse",
            'token: "ghs_liveLiveLiveLiveLive"',
            "  api_key = AKIAIOSFODNN7EXAMPLE",
            "client-key-data: LS0tLS1CRUdJTiBSU0E=",
            "- authorization: Basic Zm9vOmJhcg==",
        ):
            with self.subTest(line=line):
                out = audit_report.redact_secrets(line)
                self.assertIn(audit_report.REDACTED, out)

    def test_the_field_name_survives_so_the_reader_knows_what_was_hidden(self):
        out = audit_report.redact_secrets("password: hunter2correcthorse")
        self.assertTrue(out.startswith("password: "))

    def test_a_secret_payload_block_is_blanked_whatever_the_keys_are_called(self):
        out = self.assertRedacted(
            "apiVersion: v1\nkind: Secret\ndata:\n  benign-name: c3VwZXJzZWNyZXQ=\n"
            "  another: b3RoZXI=\n",
            "c3VwZXJzZWNyZXQ=",
        )
        self.assertNotIn("b3RoZXI=", out)
        # Structure survives: a reader can still see the shape of the object.
        self.assertIn("kind: Secret", out)
        self.assertIn("benign-name:", out)

    def test_the_secret_block_ends_when_the_indent_does(self):
        out = audit_report.redact_secrets(
            "data:\n  key: c2VjcmV0\nmetadata:\n  name: payments-db\n"
        )
        self.assertIn("name: payments-db", out)

    def test_a_private_key_body_goes_but_the_header_stays(self):
        out = self.assertRedacted(
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END RSA PRIVATE KEY-----",
            "MIIEow",
        )
        self.assertIn("-----BEGIN RSA PRIVATE KEY-----", out)
        self.assertIn("-----END RSA PRIVATE KEY-----", out)

    def test_self_identifying_tokens_go_wherever_they_appear(self):
        for secret in (
            "ghp_0123456789abcdefghij",
            "github_pat_11ABCDEFG0123456789abcdef",
            "ya29.a0ARrdaM9abcdefghijklmnop",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
        ):
            with self.subTest(secret=secret):
                self.assertRedacted(f"log line before {secret} and after", secret)

    def test_a_bearer_header_is_redacted(self):
        self.assertRedacted(
            "Authorization: Bearer abcdefghijklmnopqrstuv", "abcdefghijklmnopqrstuv"
        )

    def test_ordinary_audit_evidence_is_left_intact(self):
        # Over-redaction destroys the artifact. Bare base64 and long opaque ids
        # are normal in audit output and must survive.
        for benign in (
            "No resources found in payments namespace.",
            "nodeVersion: 1.29.4-gke.1043004",
            "image: gcr.io/acme/api@sha256:3f5b1c2d4e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b",
            "sizeGb: 500",
            "c3VwZXJzZWNyZXQK",
        ):
            with self.subTest(benign=benign):
                self.assertEqual(audit_report.redact_secrets(benign), benign)

    def test_a_jsonpath_naming_a_secret_key_is_not_a_secret(self):
        # There is no value after the colon, so the command survives verbatim —
        # the SOPs require pasting the reproducing command exactly.
        command = "kubectl get secret db -o jsonpath='{.data.token}'"
        self.assertEqual(audit_report.redact_secrets(command), command)

    def test_the_renderer_redacts_evidence_it_is_handed(self):
        doc = make_doc(
            findings=[make_finding(excerpt="  token: ghs_abcdefghijklmnopqrstu\n")]
        )
        body = render_body(doc, generated_at=NOW)
        self.assertNotIn("ghs_abcdefghijklmnopqrstu", body)
        self.assertIn(audit_report.REDACTED, body)

    def test_redaction_survives_the_none_and_empty_cases(self):
        self.assertEqual(audit_report.redact_secrets(None), "")
        self.assertEqual(audit_report.redact_secrets(""), "")


class TestClipText(unittest.TestCase):
    def test_a_short_field_is_returned_unchanged(self):
        self.assertEqual(audit_report.clip_text("  hello  ", 40), "hello")

    def test_an_oversized_field_is_clipped_and_says_so(self):
        out = audit_report.clip_text("x" * 500, 40)
        self.assertTrue(out.endswith("…(truncated)"))
        self.assertLess(len(out), 500)

    def test_clipping_redacts_first_so_a_secret_cannot_hide_past_the_limit(self):
        out = audit_report.clip_text("password: hunter2correcthorse", 400)
        self.assertNotIn("hunter2correcthorse", out)

    def test_every_free_text_field_is_capped(self):
        # A single oversized field used to be able to push the body past
        # GitHub's limit on its own, and since at least one finding always
        # renders, that published nothing at all.
        huge = "y" * 40_000
        doc = make_doc(
            findings=[
                make_finding(
                    title=huge,
                    impact=huge,
                    recommendation={"action": huge, "rationale": huge, "risk": huge},
                    remediation={"kind": "manual", "note": huge},
                )
            ]
        )
        body = render_body(doc, generated_at=NOW)
        self.assertLessEqual(len(body), audit_report.MAX_BODY_CHARS)


class TestNewlineNormalisation(unittest.TestCase):
    def test_a_browser_authored_command_still_fires(self):
        # GitHub's web comment box submits CRLF. Every marker pattern here ends
        # in `[ \t]*$`, and \r is neither — so without folding, a /remediate
        # typed in a browser was silently ignored.
        findings = [manifest_finding("netpol", "a.yaml")]
        parsed = audit_report.parse_remediate_commands(
            [
                {
                    "id": "IC_1",
                    "body": "please fix\r\n/remediate netpol\r\n",
                    "author": {"login": "dev"},
                    "authorAssociation": "MEMBER",
                }
            ],
            findings,
        )
        self.assertEqual(parsed.targets, ["netpol"])

    def test_a_crlf_body_still_yields_its_delta_block(self):
        body = audit_report.delta_block(["a", "b"]).replace("\n", "\r\n")
        self.assertEqual(audit_report.parse_delta_block(body), ["a", "b"])

    def test_a_crlf_marker_is_still_found(self):
        body = f"reply\r\n{audit_report.refused_marker('IC_9')}\r\n"
        self.assertTrue(
            audit_report.has_marker(body, audit_report.REFUSED_MARKER_RE, "IC_9")
        )


class TestFenceScanning(unittest.TestCase):
    def strip(self, text):
        return audit_report.strip_fenced_blocks(text)

    def test_a_command_inside_a_fence_is_removed(self):
        self.assertNotIn("/remediate", self.strip("a\n```\n/remediate x\n```\nb"))

    def test_text_between_two_fenced_blocks_survives(self):
        # The old non-greedy regex paired fence 1 with fence 2 and fence 3 with
        # fence 4, so a command sitting between blocks two and three was
        # swallowed — or, with an odd fence count, a real command inside a
        # block leaked through.
        out = self.strip("```\nin one\n```\n/remediate real\n```\nin two\n```")
        self.assertIn("/remediate real", out)
        self.assertNotIn("in one", out)
        self.assertNotIn("in two", out)

    def test_an_unterminated_fence_swallows_the_rest(self):
        self.assertNotIn("/remediate x", self.strip("```\n/remediate x\n"))

    def test_a_tilde_fence_is_a_fence(self):
        self.assertNotIn("/remediate x", self.strip("~~~\n/remediate x\n~~~"))

    def test_a_shorter_run_inside_a_longer_fence_does_not_close_it(self):
        out = self.strip("````\n```\n/remediate x\n```\n````\n/remediate real")
        self.assertNotIn("/remediate x", out)
        self.assertIn("/remediate real", out)

    def test_a_backtick_fence_is_not_closed_by_tildes(self):
        self.assertNotIn("/remediate x", self.strip("```\n~~~\n/remediate x\n```"))


class TestPathContainment(unittest.TestCase):
    def test_a_normalised_path_is_returned_not_just_accepted(self):
        # Grouping, the branch digest, the `git add` pathspec and the existence
        # check all have to see one spelling, or `a/b.yaml` and `./a/b.yaml`
        # become two groups and two pull requests that conflict.
        self.assertEqual(
            audit_report._require_repo_relative("./clusters//prod/x.yaml", "where"),
            "clusters/prod/x.yaml",
        )

    def test_two_spellings_of_one_path_group_together(self):
        findings = [
            manifest_finding("a", "./clusters/prod/x.yaml"),
            manifest_finding("b", "clusters/prod//x.yaml"),
        ]
        audit_report.validate_findings(make_doc(findings=findings), AUDIT)
        groups = audit_report.remediation_groups(findings)
        self.assertEqual(len(groups), 1)

    def test_the_refusals_hold(self):
        for path in (
            "/etc/passwd",
            "../outside.yaml",
            "clusters/../../outside.yaml",
            ".git/config",
            "clusters/*.yaml",
            "clusters/x?.yaml",
            "clusters/[ab].yaml",
            ":(glob)clusters/x.yaml",
            "clusters\\prod\\x.yaml",
            "clusters/prod/",
            "",
            ".",
            "..",
        ):
            with self.subTest(path=path):
                with self.assertRaises(audit_report.ValidationError):
                    audit_report._require_repo_relative(path, "where")


# --------------------------------------------------------------------------- #
# Partial coverage — what a run may not conclude when it could not look
# --------------------------------------------------------------------------- #


class TestCoverageGaps(unittest.TestCase):
    def test_a_skipped_cluster_is_a_gap(self):
        gaps = audit_report.coverage_gaps(
            make_doc(skipped=[{"cluster": "dr-west", "reason": "unreachable"}])
        )
        self.assertEqual(len(gaps), 1)
        self.assertIn("dr-west", gaps[0])
        self.assertIn("not audited", gaps[0])

    def test_a_limitation_on_a_read_cluster_is_also_a_gap(self):
        gaps = audit_report.coverage_gaps(
            make_doc(
                clusters=[
                    {
                        "name": "prod-us-east",
                        "location": "us-east1",
                        "project": "acme-prod",
                        "limitations": "Autopilot: node-level checks did not run",
                    }
                ]
            )
        )
        self.assertEqual(len(gaps), 1)
        self.assertIn("partially audited", gaps[0])

    def test_a_complete_run_has_no_gaps(self):
        self.assertEqual(audit_report.coverage_gaps(make_doc()), [])


class TestPartialCoverageGating(HarnessTestCase):
    """A run that could not look must not conclude anything from absence."""

    PARTIAL = [{"cluster": "dr-west", "reason": "control plane unreachable"}]

    def test_a_clean_but_partial_run_leaves_the_ledger_open(self):
        self.harness.replies = {"issue list": self.issue_list()}
        self.assertEqual(
            self.run_finish(make_doc(findings=[], skipped=self.PARTIAL)), 0
        )
        out = self.stdout_json()
        self.assertEqual(out["status"], "CLEAN")
        self.assertTrue(out["partial"])
        self.assertEqual(len(out["coverage_gaps"]), 1)
        # The all-clear is still said, but the ledger is not retired.
        self.assertTrue(self.harness.gh_calls("issue", "comment", "42"))
        self.assertEqual(self.harness.gh_calls("issue", "close"), [])

    def test_a_clean_but_partial_run_reports_nothing_as_resolved(self):
        self.harness.replies = {"issue list": self.issue_list()}
        self.run_finish(make_doc(findings=[], skipped=self.PARTIAL))
        self.assertEqual(self.stdout_json()["resolved"], 0)

    def test_a_clean_and_complete_run_still_closes_the_ledger(self):
        self.harness.replies = {"issue list": self.issue_list()}
        self.run_finish(make_doc(findings=[]))
        self.assertTrue(self.harness.matching("issue", "close", "42"))
        self.assertFalse(self.stdout_json()["partial"])

    def test_a_partial_run_closes_no_remediation_pull_request(self):
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        self.harness.replies = {
            "issue list": self.issue_list(),
            "pr list": json.dumps(
                [pr(8, "platform-agent/fix-x-gone", body=audit_report.delta_block(["gone"]))]
            ),
        }
        self.run_finish(make_doc(skipped=self.PARTIAL))
        self.assertEqual(self.harness.gh_calls("pr", "close"), [])
        self.assertIn("no remediation pull request was closed", self.err)

    def test_a_partial_run_announces_nothing_as_resolved(self):
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        previous = render_body(
            make_doc(findings=[make_finding(fid="gone"), make_finding()]),
            generated_at=NOW,
        )
        self.harness.replies = {
            "issue list": self.issue_list(),
            "--json body": json.dumps({"body": previous}),
        }
        self.run_finish(make_doc(skipped=self.PARTIAL))
        self.assertEqual(self.stdout_json()["resolved"], 0)
        self.assertTrue(self.stdout_json()["partial"])

    def test_a_truncated_body_is_not_a_coverage_gap(self):
        # `partial` used to be `bool(gaps) or rendered.partial`, which made
        # `partial: true` with an empty `coverage_gaps` reachable — a flag the
        # SOPs tell the agent to explain, with nothing to explain it with.
        #
        # The two are different kinds of incomplete. A gap means the audit did
        # not look, which is why it suppresses the resolved count. Truncation
        # means it looked and could not print it all: the title counts are
        # still true and resolution accounting is untouched, so the run may
        # conclude everything a complete run may. It is surfaced in the body
        # and the log, not here.
        many = [make_finding(fid=f"f-{n:04d}", severity="minor") for n in range(400)]
        self.harness.replies = {"issue list": self.issue_list()}
        self.assertEqual(self.run_finish(make_doc(findings=many)), 0)
        out = self.stdout_json()
        self.assertFalse(out["partial"])
        self.assertEqual(out["coverage_gaps"], [])
        self.assertIn("do not fit", self.err.replace("did not fit", "do not fit"))

    def test_partial_is_true_exactly_when_there_are_coverage_gaps(self):
        # The documented invariant, asserted on both `finish` branches: five
        # SOPs and SKILL.md now say "if and only if", and a reader is entitled
        # to report from either field.
        cases = [
            ("clean, complete", make_doc(findings=[]), False),
            ("clean, gap", make_doc(findings=[], skipped=self.PARTIAL), True),
            ("findings, complete", make_doc(), False),
            ("findings, gap", make_doc(skipped=self.PARTIAL), True),
            (
                "findings, limitation only",
                make_doc(
                    clusters=[
                        {
                            "name": "prod-us-east",
                            "location": "us-east1",
                            "project": "acme",
                            "limitations": "Autopilot: node checks did not run",
                        }
                    ]
                ),
                True,
            ),
        ]
        for label, doc, expected in cases:
            with self.subTest(label):
                self.setUp()
                self.touch("clusters/prod-us-east/payments-netpol.yaml")
                self.harness.replies = {"issue list": self.issue_list()}
                self.run_finish(doc)
                out = self.stdout_json()
                self.assertEqual(out["partial"], expected)
                self.assertEqual(bool(out["coverage_gaps"]), out["partial"])


# --------------------------------------------------------------------------- #
# Close semantics — whose close is final
# --------------------------------------------------------------------------- #


class TestCloseSemantics(unittest.TestCase):
    def test_a_harness_close_is_recognised_by_its_label(self):
        self.assertTrue(
            audit_report.pr_closed_by_harness(
                {"state": "CLOSED", "labels": [{"name": audit_report.STALE_CLOSED_LABEL}]}
            )
        )

    def test_a_human_close_is_not(self):
        self.assertFalse(
            audit_report.pr_closed_by_harness({"state": "CLOSED", "labels": []})
        )

    def test_a_merged_pull_request_is_not_a_harness_close(self):
        self.assertFalse(
            audit_report.pr_closed_by_harness(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-07-01T00:00:00Z",
                    "labels": [{"name": audit_report.STALE_CLOSED_LABEL}],
                }
            )
        )

    def test_a_finding_the_harness_withdrew_can_be_re_promoted(self):
        findings = [manifest_finding("crit", "a.yaml", severity="critical")]
        closed_by_us = {
            "state": "CLOSED",
            "labels": [{"name": audit_report.STALE_CLOSED_LABEL}],
        }
        plan = audit_report.promotion_candidates(findings, {"crit": closed_by_us})
        self.assertEqual(plan.promote, ["crit"])

    def test_a_finding_a_human_closed_is_never_re_promoted(self):
        # Re-opening it would overrule a person, daily, forever.
        findings = [manifest_finding("crit", "a.yaml", severity="critical")]
        plan = audit_report.promotion_candidates(
            findings, {"crit": {"state": "CLOSED", "labels": []}}
        )
        self.assertEqual(plan.promote, [])

    def test_an_explicit_request_over_an_open_pr_is_reported_not_forced(self):
        findings = [manifest_finding("crit", "a.yaml")]
        plan = audit_report.promotion_candidates(
            findings, {"crit": {"state": "OPEN"}}, requested=["crit"]
        )
        self.assertEqual(plan.promote, [])
        self.assertEqual(plan.already_open, ["crit"])


class TestStaleClose(HarnessTestCase):
    def close_it(self, prs, current_ids=(), live_branches=None):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            closed = audit_report.close_stale_remediation_prs(
                "acme/fleet",
                AUDIT,
                prs,
                set(current_ids),
                {},
                {},
                NOW,
                live_branches=live_branches,
            )
        self.err = err.getvalue()
        return closed

    def stale_pr(self, number=8, branch="platform-agent/fix-x-old"):
        return pr(number, branch, body=audit_report.delta_block(["gone"]))

    def test_the_label_is_applied_before_the_close(self):
        # A crash between the two leaves a labelled *open* pull request, which
        # is recoverable. The other order leaves an unlabelled closed one,
        # which reads as a human rejection forever.
        self.close_it([self.stale_pr()])
        order = [c for c in self.harness.calls if c[:2] == ["gh", "pr"]]
        label = next(i for i, c in enumerate(order) if "--add-label" in c)
        close = next(i for i, c in enumerate(order) if c[2] == "close")
        self.assertLess(label, close)
        self.assertIn(audit_report.STALE_CLOSED_LABEL, order[label])

    def test_the_branch_is_never_deleted(self):
        self.close_it([self.stale_pr()])
        for call in self.harness.calls:
            self.assertNotIn("--delete-branch", call)

    def test_a_failed_close_is_not_reported_as_closed(self):
        self.harness.failures = {"pr close": 1}
        self.assertEqual(self.close_it([self.stale_pr()]), [])
        self.assertIn("could not close PR #8", self.err)

    def test_a_pull_request_already_closed_as_stale_is_not_re_commented(self):
        marked = pr(
            8,
            "platform-agent/fix-x-old",
            body=audit_report.delta_block(["gone"])
            + "\n"
            + audit_report.stale_closed_marker(8),
        )
        self.assertEqual(self.close_it([marked]), [])
        self.assertEqual(self.harness.gh_calls("pr", "close"), [])

    def test_a_live_finding_keeps_its_pull_request_open(self):
        self.assertEqual(self.close_it([self.stale_pr()], current_ids={"gone"}), [])

    def test_an_orphaned_branch_is_closed_even_though_its_finding_lives(self):
        # The group's path set changed, so the work moved to a different
        # branch. Left open, this pull request conflicts with the new one.
        closed = self.close_it(
            [self.stale_pr()],
            current_ids={"gone"},
            live_branches={"platform-agent/fix-x-new"},
        )
        self.assertEqual(len(closed), 1)
        comment = self.harness.gh_calls("pr", "comment")[0]
        self.assertIn("8", comment)

    def test_a_branch_that_is_still_live_is_left_alone(self):
        self.assertEqual(
            self.close_it(
                [self.stale_pr()],
                current_ids={"gone"},
                live_branches={"platform-agent/fix-x-old"},
            ),
            [],
        )


# --------------------------------------------------------------------------- #
# Ledger selection and pagination
# --------------------------------------------------------------------------- #


class TestFindExistingIssue(HarnessTestCase):
    def find(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = audit_report.find_existing_issue("acme/fleet", AUDIT)
        self.err = err.getvalue()
        return result

    def test_the_highest_number_wins_so_the_choice_converges(self):
        # Duplicates exist because some run created one — and that run wrote
        # this stream's state into the higher number and linked it from every
        # pull request it opened. Preferring the lower one abandons that work
        # every run and the audit alternates between two ledgers forever.
        self.harness.replies = {
            "issue list": json.dumps(
                [
                    {"number": 7, "url": "https://github.com/acme/fleet/issues/7"},
                    {"number": 42, "url": "https://github.com/acme/fleet/issues/42"},
                ]
            )
        }
        number, url = self.find()
        self.assertEqual(number, 42)
        self.assertTrue(url.endswith("/42"))
        self.assertIn("7", self.err)

    def test_no_ledger_is_not_an_error(self):
        self.harness.replies = {"issue list": "[]"}
        self.assertEqual(self.find(), (None, None))

    def test_an_outage_raises_rather_than_reporting_no_ledger(self):
        self.harness.failures = {"issue list": 1}
        with self.assertRaises(audit_report.GitHubLookupError):
            self.find()

    def test_unparseable_output_raises(self):
        self.harness.replies = {"issue list": "not json"}
        with self.assertRaises(audit_report.GitHubLookupError):
            self.find()


class TestRemediationPrPaging(HarnessTestCase):
    def test_a_full_page_raises_rather_than_being_silently_truncated(self):
        # A truncated page reads as "no pull request", so the harness would
        # re-open fixes that already exist and re-promote findings a human
        # closed. Refusing the run is the only safe answer.
        self.harness.replies = {
            "pr list": json.dumps(
                [
                    pr(i, f"platform-agent/fix-{AUDIT}-{i}")
                    for i in range(audit_report.MAX_PR_PAGE)
                ]
            )
        }
        with self.assertRaises(audit_report.GitHubLookupError):
            audit_report.list_remediation_prs("acme/fleet", AUDIT)

    def test_a_short_page_is_returned(self):
        self.harness.replies = {"pr list": json.dumps([pr(1, "b")])}
        self.assertEqual(len(audit_report.list_remediation_prs("acme/fleet", AUDIT)), 1)


# --------------------------------------------------------------------------- #
# Acknowledging a /remediate that worked
# --------------------------------------------------------------------------- #


class TestAcknowledgements(HarnessTestCase):
    def test_an_accepted_request_gets_exactly_one_answer(self):
        # Silence is not an answer to a command: a requester who sees nothing
        # cannot tell "not run yet" from "ignored", so they ask again.
        audit_report.ack_remediate_requests(
            "acme/fleet",
            42,
            {"IC_1": ["netpol"]},
            {"netpol": "pull request opened — https://example.invalid/1"},
            [],
            NOW,
        )
        comments = self.harness.gh_calls("issue", "comment")
        self.assertEqual(len(comments), 1)

    def test_the_same_request_is_never_answered_twice(self):
        answered = [{"body": f"earlier\n{audit_report.acked_marker('IC_1')}\n"}]
        audit_report.ack_remediate_requests(
            "acme/fleet", 42, {"IC_1": ["netpol"]}, {}, answered, NOW
        )
        self.assertEqual(self.harness.gh_calls("issue", "comment"), [])

    def test_the_answer_names_the_outcome_of_each_target(self):
        body = audit_report.render_ack_comment(
            "IC_1", ["a", "b"], {"a": "pull request opened — u"}, NOW
        )
        self.assertIn("`a` — pull request opened — u", body)
        self.assertIn("`b` — no pull request was opened", body)
        self.assertIn(audit_report.acked_marker("IC_1"), body)

    def test_an_accepted_request_is_recorded_against_its_comment(self):
        parsed = audit_report.parse_remediate_commands(
            [
                {
                    "id": "IC_7",
                    "body": "/remediate netpol",
                    "author": {"login": "dev"},
                    "authorAssociation": "MEMBER",
                }
            ],
            [manifest_finding("netpol", "a.yaml")],
        )
        self.assertEqual(parsed.accepted_by_comment, {"IC_7": ["netpol"]})


class TestRemediationOutcomes(unittest.TestCase):
    def plan(self, **kw):
        return audit_report.PromotionPlan(
            kw.get("promote", []), kw.get("withheld", []), kw.get("already_open", [])
        )

    def requests(self, targets):
        return audit_report.RemediateRequests(targets, [], {})

    def test_a_freshly_opened_pull_request_is_named_by_url(self):
        out = audit_report._remediation_outcomes(
            self.requests(["a"]),
            self.plan(promote=["a"]),
            {"a": {"url": "https://example.invalid/1"}},
            ["https://example.invalid/1"],
        )
        self.assertIn("opened", out["a"])
        self.assertIn("https://example.invalid/1", out["a"])

    def test_an_untouched_open_pull_request_says_so(self):
        out = audit_report._remediation_outcomes(
            self.requests(["a"]),
            self.plan(already_open=["a"]),
            {"a": {"url": "https://example.invalid/1"}},
            [],
        )
        self.assertIn("already open", out["a"])
        self.assertIn("force-pushed", out["a"])

    def test_a_failure_is_reported_as_a_retry_not_as_success(self):
        out = audit_report._remediation_outcomes(
            self.requests(["a"]), self.plan(promote=["a"]), {}, []
        )
        self.assertIn("no pull request was opened", out["a"])


# --------------------------------------------------------------------------- #
# Body budget bookkeeping
# --------------------------------------------------------------------------- #


class TestRenderedIssue(unittest.TestCase):
    def flood(self, n):
        return make_doc(
            findings=[
                make_finding(fid=f"f-{i:04d}", severity="minor", title=f"Finding {i}")
                for i in range(n)
            ]
        )

    def test_a_complete_render_reports_nothing_omitted(self):
        rendered = audit_report.render_issue_body(make_doc(), generated_at=NOW)
        self.assertFalse(rendered.partial)
        self.assertEqual(rendered.omitted, [])
        self.assertEqual(rendered.rendered_ids, ["no-network-policy"])

    def test_a_truncated_render_says_which_ids_it_dropped(self):
        rendered = audit_report.render_issue_body(self.flood(400), generated_at=NOW)
        self.assertTrue(rendered.partial)
        self.assertLessEqual(len(rendered.body), audit_report.MAX_BODY_CHARS)
        self.assertEqual(
            len(rendered.rendered_ids) + len(rendered.omitted), 400
        )

    def test_the_delta_block_carries_only_what_a_reader_can_see(self):
        # The delta compares against the hidden block. If it listed findings
        # the body never rendered, every omitted finding would be announced as
        # newly resolved the moment the body got shorter.
        rendered = audit_report.render_issue_body(self.flood(400), generated_at=NOW)
        self.assertEqual(
            audit_report.parse_delta_block(rendered.body),
            sorted(rendered.rendered_ids),
        )

    def test_the_delta_comment_admits_partial_coverage_of_the_description(self):
        comment = audit_report.render_delta_comment(
            AUDIT, [], [], [], {}, NOW, omitted=12
        )
        self.assertIsNotNone(comment)
        self.assertIn("partial", comment)
        self.assertIn("12", comment)


# --------------------------------------------------------------------------- #
# The GitOps workspace — the clone that was never made
# --------------------------------------------------------------------------- #


class TestGitopsWorkspace(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp_path / "gitops"
        self.calls = []

    def runner(self, cmd, *, cwd=None, check=True):
        self.calls.append(list(cmd))
        if cmd[:2] == ["git", "clone"]:
            (Path(cmd[-1]) / ".git").mkdir(parents=True, exist_ok=True)
        return CompletedProcess(cmd, 0, "", "")

    def test_the_path_is_one_flat_directory_per_repository(self):
        self.assertEqual(
            gitops_workspace.workspace_path("acme/fleet", self.root),
            self.root / "acme__fleet",
        )

    def test_a_malformed_repository_is_refused(self):
        for repo in ("fleet", "", "acme/"):
            with self.subTest(repo=repo):
                with self.assertRaises(ValueError):
                    gitops_workspace.workspace_path(repo, self.root)

    def test_the_first_run_clones_and_lands_on_main(self):
        target = gitops_workspace.ensure_workspace(
            "acme/fleet", self.runner, root=self.root
        )
        self.assertTrue((target / ".git").is_dir())
        self.assertEqual(self.calls[0][:2], ["git", "clone"])
        self.assertIn(["git", "checkout", "-B", "main", "origin/main"], self.calls)

    def test_a_later_run_fetches_instead_of_cloning(self):
        gitops_workspace.ensure_workspace("acme/fleet", self.runner, root=self.root)
        self.calls.clear()
        gitops_workspace.ensure_workspace("acme/fleet", self.runner, root=self.root)
        self.assertFalse([c for c in self.calls if c[:2] == ["git", "clone"]])
        self.assertIn(["git", "fetch", "--quiet", "--prune", "origin"], self.calls)

    def test_a_half_finished_clone_is_cleared_rather_than_blocking_forever(self):
        target = gitops_workspace.workspace_path("acme/fleet", self.root)
        (target / "leftover").mkdir(parents=True)
        gitops_workspace.ensure_workspace("acme/fleet", self.runner, root=self.root)
        self.assertFalse((target / "leftover").exists())
        self.assertTrue((target / ".git").is_dir())

    def test_a_clone_that_produced_no_tree_raises(self):
        def dead(cmd, *, cwd=None, check=True):
            return CompletedProcess(cmd, 0, "", "")

        with self.assertRaises(RuntimeError):
            gitops_workspace.ensure_workspace("acme/fleet", dead, root=self.root)

    def test_the_working_tree_is_reset_before_use(self):
        gitops_workspace.ensure_workspace("acme/fleet", self.runner, root=self.root)
        joined = [" ".join(c) for c in self.calls]
        self.assertIn("git reset --hard --quiet", joined)
        self.assertIn("git clean -fdq", joined)

    def test_reset_false_fetches_but_scrubs_nothing(self):
        # `finish` reattaches to a tree that already holds the audit's
        # remediation manifests, untracked. A clean here deletes every one of
        # them and the run then reports each fix as a file the model forgot.
        gitops_workspace.ensure_workspace("acme/fleet", self.runner, root=self.root)
        self.calls.clear()
        gitops_workspace.ensure_workspace(
            "acme/fleet", self.runner, root=self.root, reset=False
        )
        joined = [" ".join(c) for c in self.calls]
        self.assertIn("git fetch --quiet --prune origin", joined)
        self.assertNotIn("git clean -fdq", joined)
        self.assertNotIn("git reset --hard --quiet", joined)
        self.assertFalse([c for c in self.calls if c[:2] == ["git", "checkout"]])

    def test_reset_false_still_clones_when_there_is_nothing_to_preserve(self):
        target = gitops_workspace.ensure_workspace(
            "acme/fleet", self.runner, root=self.root, reset=False
        )
        self.assertEqual(self.calls[0][:2], ["git", "clone"])
        self.assertTrue((target / ".git").is_dir())

    def test_an_untracked_manifest_survives_a_real_reattach(self):
        """The mocked runner cannot see this one, so run real git.

        `git clean -fd` on the way into `finish` is invisible to a recorded
        runner: nothing actually deletes anything, so the fixture the test
        wrote is still on disk and the assertion passes on code that would
        wipe the tree in production.
        """
        if shutil.which("git") is None:  # pragma: no cover - git is always present
            self.skipTest("git is not on PATH")

        def real(cmd, *, cwd=None, check=True):
            return subprocess.run(
                cmd, cwd=cwd, check=check, capture_output=True, text=True
            )

        origin = self.tmp_path / "origin.git"
        seed = self.tmp_path / "seed"
        seed.mkdir()
        for cmd in (
            ["git", "init", "--quiet", "--bare", "--initial-branch=main", str(origin)],
            ["git", "init", "--quiet", "--initial-branch=main", str(seed)],
        ):
            subprocess.run(cmd, check=True, capture_output=True)
        (seed / "README.md").write_text("seed\n", encoding="utf-8")
        for cmd in (
            ["git", "config", "user.email", "t@example.com"],
            ["git", "config", "user.name", "T"],
            ["git", "add", "README.md"],
            ["git", "commit", "--quiet", "-m", "seed"],
            ["git", "remote", "add", "origin", str(origin)],
            ["git", "push", "--quiet", "origin", "main"],
        ):
            subprocess.run(cmd, cwd=seed, check=True, capture_output=True)

        target = gitops_workspace.ensure_workspace(
            "acme/fleet", real, root=self.root, remote_url=str(origin)
        )
        manifest = target / "clusters/prod/netpol.yaml"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("kind: NetworkPolicy\n", encoding="utf-8")

        gitops_workspace.ensure_workspace(
            "acme/fleet", real, root=self.root, remote_url=str(origin), reset=False
        )
        self.assertTrue(manifest.is_file(), "finish deleted the fix it was about to open")

        gitops_workspace.ensure_workspace(
            "acme/fleet", real, root=self.root, remote_url=str(origin), reset=True
        )
        self.assertFalse(manifest.exists(), "start must hand the audit a clean tree")

    def test_the_identity_is_repository_local_never_global(self):
        target = gitops_workspace.workspace_path("acme/fleet", self.root)
        gitops_workspace.configure_identity(target, self.runner)
        self.assertTrue(self.calls)
        for call in self.calls:
            self.assertNotIn("--global", call)
        self.assertEqual(self.calls[0][:3], ["git", "config", "user.name"])

    def test_the_lock_is_best_effort_not_a_reason_to_skip_the_audit(self):
        # A read-only or absent PVC must cost a retry, not the day's audit.
        with gitops_workspace.workspace_lock("/proc/nonexistent/gitops"):
            pass

    def test_the_lock_serialises_and_releases(self):
        with gitops_workspace.workspace_lock(self.root):
            pass
        with gitops_workspace.workspace_lock(self.root):
            pass


# --------------------------------------------------------------------------- #
# Dry run — must describe the run that would actually happen
# --------------------------------------------------------------------------- #


class TestDryRunParity(HarnessTestCase):
    def dry(self, doc):
        rc = self.run_finish(doc, argv_extra=["--dry-run"])
        self.assertEqual(rc, 0)
        return self.out, self.err

    def test_it_touches_nothing(self):
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        self.dry(make_doc())
        self.assertEqual(self.harness.calls, [])

    def test_it_reports_the_branch_the_real_run_would_push(self):
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        _, err = self.dry(make_doc())
        expected = audit_report.group_branch_for(AUDIT, [make_finding()])
        self.assertIn(expected, err)

    def test_it_degrades_a_missing_manifest_like_the_real_run(self):
        _, err = self.dry(make_doc())
        self.assertIn("degrades to a manual remediation", err)
        self.assertIn("(no remediation pull requests)", err)

    def test_it_names_the_coverage_gaps(self):
        _, err = self.dry(
            make_doc(skipped=[{"cluster": "dr-west", "reason": "unreachable"}])
        )
        self.assertIn("COVERAGE GAP: dr-west", err)

    def test_a_clean_partial_run_does_not_claim_the_ledger_would_close(self):
        _, err = self.dry(
            make_doc(
                findings=[], skipped=[{"cluster": "dr-west", "reason": "unreachable"}]
            )
        )
        self.assertIn("left OPEN, not closed", err)

    def test_a_clean_complete_run_says_the_ledger_would_close(self):
        _, err = self.dry(make_doc(findings=[]))
        self.assertIn("would be closed", err)

    def test_it_prints_the_body_that_would_be_published(self):
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        out, _ = self.dry(make_doc())
        self.assertIn("## Findings", out)
        self.assertIn("audit-findings:", out)


class TestRepoResolution(BaseTestCase):
    """The repository must be resolvable before a clone exists.

    The old path ran `git config --get remote.origin.url` in the current
    directory. The audit crons start in the agent's profile directory, which is
    not a working tree, so that call returned nothing and the run died before
    it could clone anything — the token it needed to clone is repo-scoped, and
    the repo came from the clone. SETTINGS.md breaks the cycle.
    """

    def settings(self, text):
        path = self.tmp_path / "SETTINGS.md"
        path.write_text(text, encoding="utf-8")
        self.patch_attr("SETTINGS_PATH", str(path))
        return path

    def test_the_operator_written_line_is_parsed(self):
        self.settings(
            "# GKE Scope Configuration\n"
            "- **Git Repo:** https://github.com/acme/fleet.git\n"
        )
        self.assertEqual(audit_report.resolve_repo(), "acme/fleet")

    def test_an_ssh_remote_is_parsed(self):
        self.settings("- **Git Repo:** git@github.com:acme/fleet.git\n")
        self.assertEqual(audit_report.resolve_repo(), "acme/fleet")

    def test_a_bare_owner_name_is_parsed(self):
        self.settings("- **Git Repo:** acme/fleet\n")
        self.assertEqual(audit_report.resolve_repo(), "acme/fleet")

    def test_the_unset_placeholder_is_not_a_repository(self):
        # The operator writes the literal `None` when the CR omits the repo.
        # Treating that as an owner/name would send every gh call to a repo
        # called "None".
        self.settings("- **Git Repo:** None\n")
        self.assertIsNone(audit_report.repo_from_settings())

    def test_a_missing_settings_file_is_not_an_error_on_its_own(self):
        self.patch_attr("SETTINGS_PATH", str(self.tmp_path / "absent.md"))
        self.assertIsNone(audit_report.repo_from_settings())

    def test_it_falls_back_to_the_git_remote(self):
        self.patch_attr("SETTINGS_PATH", str(self.tmp_path / "absent.md"))
        module = type(sys)("github_token_refresh")
        module.get_current_git_repo = lambda: "acme/from-remote"
        with patch.dict(sys.modules, {"github_token_refresh": module}):
            self.assertEqual(audit_report.resolve_repo(), "acme/from-remote")

    def test_both_sources_failing_names_both_sources(self):
        missing = self.tmp_path / "absent.md"
        self.patch_attr("SETTINGS_PATH", str(missing))
        module = type(sys)("github_token_refresh")
        module.get_current_git_repo = lambda: None
        with patch.dict(sys.modules, {"github_token_refresh": module}):
            with self.assertRaises(RuntimeError) as caught:
                audit_report.resolve_repo()
        self.assertIn(str(missing), str(caught.exception))
        self.assertIn("origin remote", str(caught.exception))

    def test_settings_wins_over_whatever_directory_the_agent_is_in(self):
        self.settings("- **Git Repo:** https://github.com/acme/fleet\n")
        module = type(sys)("github_token_refresh")

        def explode():
            raise AssertionError("the git remote must not be consulted first")

        module.get_current_git_repo = explode
        with patch.dict(sys.modules, {"github_token_refresh": module}):
            self.assertEqual(audit_report.resolve_repo(), "acme/fleet")


class TestCredentialOrdering(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.minted = []
        self.order = []
        self.patch_attr("refresh_credentials", self._refresh)
        self.patch_attr("resolve_repo", self._resolve)
        self.patch_attr(
            "findings_path_for",
            lambda audit_id: str(self.tmp_path / f"findings_{audit_id}.json"),
        )
        patcher = patch.object(audit_report.os, "makedirs", lambda *a, **k: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _resolve(self):
        self.order.append("resolve")
        return "acme/fleet"

    def _refresh(self, repo=None):
        self.order.append("refresh")
        self.minted.append(repo)

    def test_the_repo_is_resolved_before_the_token_is_minted(self):
        self.harness.replies = {"issue list": "[]"}
        self.run_main(["start", "--audit", AUDIT])
        self.assertEqual(self.order[:2], ["resolve", "refresh"])

    def test_the_token_is_minted_for_the_resolved_repository(self):
        # Not for whatever `git config` reports in the current directory —
        # there is no clone there, so the no-argument call raises.
        self.harness.replies = {"issue list": "[]"}
        self.run_main(["start", "--audit", AUDIT])
        self.assertEqual(self.minted, ["acme/fleet"])

    def test_credentials_are_minted_before_the_clone(self):
        self.unclone()
        self.harness.replies = {"issue list": "[]"}
        self.run_main(["start", "--audit", AUDIT])
        clone = next(
            i for i, c in enumerate(self.harness.calls) if c[:2] == ["git", "clone"]
        )
        self.assertLess(self.order.index("refresh"), 2)
        self.assertGreaterEqual(clone, 0)


if __name__ == "__main__":
    unittest.main()
