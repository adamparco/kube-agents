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
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from subprocess import CompletedProcess
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
    """Stands in for audit_pr.run_cmd, recording every command and replying by rule."""

    def __init__(self, replies=None):
        self.calls: list[list[str]] = []
        self.replies = replies or {}

    def __call__(self, cmd, *, check=True, capture=True):
        self.calls.append(list(cmd))
        for key, payload in self.replies.items():
            if key in " ".join(cmd):
                return CompletedProcess(cmd, 0, payload, "")
        return CompletedProcess(cmd, 0, "", "")

    def matching(self, *fragments):
        return [
            call
            for call in self.calls
            if all(fragment in " ".join(call) for fragment in fragments)
        ]


class BaseTestCase(unittest.TestCase):
    """A temp working tree, patch bookkeeping, and captured stdout/stderr."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)
        self.out = ""
        self.err = ""

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
        return [c for c in recorder.calls if c[:2] == ["git", "add"]]


class HarnessTestCase(BaseTestCase):
    """Wires audit_pr's I/O seam to a recorder and pins the repo root to the temp tree."""

    def setUp(self):
        super().setUp()
        self.harness = Recorder()
        self.patch_attr("run_cmd", self.harness)
        self.patch_attr("refresh_credentials", lambda: None)
        self.patch_attr("resolve_repo", lambda: "acme/fleet")
        self.patch_attr("repo_root", lambda: self.tmp_path)
        self.patch_attr("current_branch", lambda: audit_pr.branch_for(AUDIT))


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


class TestRenderBody(unittest.TestCase):
    def test_renders_scope_findings_and_footer(self):
        doc = make_doc()
        body = audit_pr.render_body(
            doc,
            staged_paths=["clusters/prod-us-east/payments-netpol.yaml"],
            generated_at=NOW,
        )
        self.assertIn("maintained in place by the `compliance-audit` watchdog", body)
        self.assertIn("## Scope", body)
        self.assertIn("| `prod-us-east` | us-east1 | `acme-prod` |", body)
        self.assertIn("## Findings", body)
        self.assertIn("### Critical (1)", body)
        self.assertIn("kubectl get networkpolicy -n payments", body)
        self.assertIn("No resources found in payments namespace.", body)
        self.assertIn("All pod-to-pod traffic in payments is unrestricted.", body)
        self.assertIn("## Remediation files in this PR", body)
        self.assertIn("- `clusters/prod-us-east/payments-netpol.yaml`", body)
        self.assertIn(
            "Generated by the Platform Agent `compliance-audit` watchdog", body
        )
        self.assertTrue(
            body.rstrip().endswith('<!-- audit-findings: ["no-network-policy"] -->')
        )

    def test_evidence_command_is_fenced(self):
        body = audit_pr.render_body(make_doc(), staged_paths=[], generated_at=NOW)
        self.assertIn("```bash\nkubectl get networkpolicy -n payments\n```", body)

    def test_severity_groups_ordered_critical_major_minor(self):
        body = audit_pr.render_body(
            make_doc(findings=THREE_SEVERITIES), staged_paths=[], generated_at=NOW
        )
        order = [
            body.index("### Critical (2)"),
            body.index("### Major (1)"),
            body.index("### Minor (1)"),
        ]
        self.assertEqual(order, sorted(order))
        self.assertIn("4 findings: 2 critical, 1 major, 1 minor.", body)

    def test_empty_severity_group_is_omitted(self):
        body = audit_pr.render_body(
            make_doc(findings=[make_finding(severity="minor")]),
            staged_paths=[],
            generated_at=NOW,
        )
        self.assertIn("### Minor (1)", body)
        self.assertNotIn("### Critical", body)
        self.assertNotIn("### Major", body)

    def test_skipped_clusters_declare_partial_coverage(self):
        doc = make_doc(
            skipped=[{"cluster": "dr-west", "reason": "control plane unreachable"}]
        )
        body = audit_pr.render_body(doc, staged_paths=[], generated_at=NOW)
        self.assertIn("### Skipped", body)
        self.assertIn("**Coverage is partial.**", body)
        self.assertIn("| `dr-west` | control plane unreachable |", body)

    def test_no_skipped_section_when_none_skipped(self):
        body = audit_pr.render_body(make_doc(), staged_paths=[], generated_at=NOW)
        self.assertNotIn("### Skipped", body)
        self.assertNotIn("Coverage is partial", body)

    def test_gcloud_remediation_shows_command_and_stages_nothing(self):
        finding = make_finding(
            remediation={
                "kind": "gcloud",
                "note": "gcloud container clusters update prod-us-east --enable-shielded-nodes",
            }
        )
        body = audit_pr.render_body(
            make_doc(findings=[finding]), staged_paths=[], generated_at=NOW
        )
        self.assertIn("- **Remediation (gcloud):**", body)
        self.assertIn("gcloud container clusters update prod-us-east", body)
        self.assertIn("No files changed", body)
        self.assertEqual(audit_pr.manifest_paths([finding]), [])

    def test_manifest_remediation_links_the_path(self):
        body = audit_pr.render_body(make_doc(), staged_paths=[], generated_at=NOW)
        self.assertIn(
            "[`clusters/prod-us-east/payments-netpol.yaml`]"
            "(clusters/prod-us-east/payments-netpol.yaml)",
            body,
        )

    def test_cluster_scoped_finding_renders_without_namespace(self):
        body = audit_pr.render_body(
            make_doc(findings=[make_finding(namespace="", obj="ClusterRole/admin")]),
            staged_paths=[],
            generated_at=NOW,
        )
        self.assertIn("_cluster-scoped_", body)

    def test_body_is_deterministic_regardless_of_input_order(self):
        first = audit_pr.render_body(
            make_doc(findings=THREE_SEVERITIES), staged_paths=[], generated_at=NOW
        )
        second = audit_pr.render_body(
            make_doc(findings=list(reversed(THREE_SEVERITIES))),
            staged_paths=[],
            generated_at=NOW,
        )
        self.assertEqual(first, second)

    def test_title_and_commit_subject(self):
        self.assertEqual(
            audit_pr.pr_title(AUDIT, THREE_SEVERITIES),
            "[audit] Security & RBAC Posture Audit — 4 findings (2 critical)",
        )
        self.assertEqual(
            audit_pr.commit_subject(AUDIT, THREE_SEVERITIES),
            "chore(audit): compliance-audit — 4 findings (2 critical, 1 major, 1 minor)",
        )

    def test_single_finding_is_not_pluralised(self):
        one = [make_finding(fid="only-one")]
        self.assertEqual(
            audit_pr.pr_title(AUDIT, one),
            "[audit] Security & RBAC Posture Audit — 1 finding (1 critical)",
        )
        self.assertEqual(
            audit_pr.commit_subject(AUDIT, one),
            "chore(audit): compliance-audit — 1 finding (1 critical, 0 major, 0 minor)",
        )
        body = audit_pr.render_body(
            make_doc(findings=one), staged_paths=[], generated_at=NOW
        )
        self.assertIn("1 finding: 1 critical, 0 major, 0 minor.", body)
        self.assertNotIn("1 findings", body)

    def test_zero_findings_title_is_pluralised(self):
        self.assertEqual(
            audit_pr.pr_title(AUDIT, []),
            "[audit] Security & RBAC Posture Audit — 0 findings (0 critical)",
        )

    def test_excerpt_is_trimmed(self):
        long_excerpt = "\n".join(f"line {i}" for i in range(200))
        trimmed = audit_pr.trim_excerpt(long_excerpt)
        self.assertLessEqual(trimmed.count("\n"), audit_pr.MAX_EXCERPT_LINES)
        self.assertIn("excerpt truncated", trimmed)

    def test_excerpt_containing_a_fence_does_not_break_out(self):
        finding = make_finding(excerpt="```\nnested fence\n```")
        body = audit_pr.render_body(
            make_doc(findings=[finding]), staged_paths=[], generated_at=NOW
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
        body = audit_pr.render_body(
            make_doc(findings=[make_finding(fid=i) for i in ids]),
            staged_paths=[],
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
        run_one = audit_pr.render_body(
            make_doc(
                findings=[
                    make_finding(fid="a", title="Alpha finding"),
                    make_finding(fid="b", title="Bravo finding"),
                ]
            ),
            staged_paths=[],
            generated_at=NOW,
        )
        run_two_doc = make_doc(
            findings=[
                make_finding(fid="b", title="Bravo finding"),
                make_finding(fid="c", title="Charlie finding"),
            ]
        )
        run_two = audit_pr.render_body(run_two_doc, staged_paths=[], generated_at=NOW)

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

    def test_audit_branch_is_pushable(self):
        for audit_id in audit_pr.AUDITS:
            with self.subTest(audit=audit_id):
                branch = audit_pr.branch_for(audit_id)
                self.assertEqual(branch, f"platform-agent/audit-{audit_id}")
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
        self.assertEqual(cmd, ["git", "add", "--", "a.yaml", "b.yaml"])

    def test_wildcard_pathspecs_refused(self):
        for pathspec in (".", "-A", "--all", "-a", "*", ":/"):
            with self.subTest(pathspec=pathspec):
                with self.assertRaisesRegex(ValueError, "wildcard pathspec"):
                    audit_pr.build_git_add_command([pathspec])

    def test_empty_staging_set_refuses_to_build_an_add(self):
        with self.assertRaisesRegex(ValueError, "no explicit paths"):
            audit_pr.build_git_add_command([])


# --------------------------------------------------------------------------- #
# finish — end-to-end over the recorded seam
# --------------------------------------------------------------------------- #


class TestFinishWithFindings(HarnessTestCase):
    def test_opens_a_pr_and_stages_only_named_paths(self):
        self.harness.replies = {
            "pr list": "[]",
            "pr create": "https://github.com/acme/fleet/pull/7\n",
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

        adds = self.git_add_calls(self.harness)
        self.assertEqual(len(adds), 1)
        self.assertEqual(
            adds[0],
            [
                "git",
                "add",
                "--",
                "clusters/prod-us-east/payments-netpol.yaml",
                "clusters/stage-eu/psp.yaml",
            ],
        )

        # The wildcard stagers must never appear anywhere in the command stream.
        for call in self.harness.calls:
            if call[:2] == ["git", "add"]:
                self.assertNotIn(".", call)
                self.assertNotIn("-A", call)
                self.assertNotIn("--all", call)

        commit = self.harness.matching("git", "commit")[0]
        self.assertIn("--allow-empty", commit)
        self.assertIn(
            "chore(audit): compliance-audit — 4 findings (2 critical, 1 major, 1 minor)",
            commit,
        )

        push = self.harness.matching("git", "push")[0]
        self.assertEqual(
            push,
            [
                "git",
                "push",
                "-f",
                "origin",
                "platform-agent/audit-compliance-audit",
            ],
        )

        create = self.harness.matching("pr", "create")[0]
        self.assertIn("--label", create)
        self.assertIn("agent:audit", create)
        self.assertIn("audit:compliance-audit", create)
        self.assertFalse(self.harness.matching("pr", "edit", "--title"))

    def test_opened_status_json(self):
        self.harness.replies = {
            "pr list": "[]",
            "pr create": "https://github.com/acme/fleet/pull/7\n",
        }
        self.touch("clusters/prod-us-east/payments-netpol.yaml")
        self.run_finish(make_doc())
        self.assertEqual(
            self.stdout_json(),
            {
                "status": "OPENED",
                "pr_url": "https://github.com/acme/fleet/pull/7",
                "new": 1,
                "resolved": 0,
            },
        )

    def test_updates_in_place_and_posts_delta(self):
        previous_body = audit_pr.render_body(
            make_doc(
                findings=[
                    make_finding(fid="a", title="Alpha finding"),
                    make_finding(fid="b", title="Bravo finding"),
                ]
            ),
            staged_paths=[],
            generated_at=NOW,
        )
        self.harness.replies = {
            "pr list": json.dumps(
                [{"number": 42, "url": "https://github.com/acme/fleet/pull/42"}]
            ),
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

        self.assertFalse(self.harness.matching("pr", "create"))
        edit = self.harness.matching("pr", "edit", "--title")[0]
        self.assertEqual(edit[:4], ["gh", "pr", "edit", "42"])
        self.assertIn("--body-file", edit)

        self.assertTrue(self.harness.matching("pr", "comment", "42"))

        self.assertEqual(
            self.stdout_json(),
            {
                "status": "UPDATED",
                "pr_url": "https://github.com/acme/fleet/pull/42",
                "new": 1,
                "resolved": 1,
            },
        )

    def test_no_comment_when_findings_unchanged(self):
        doc = make_doc()
        previous_body = audit_pr.render_body(doc, staged_paths=[], generated_at=NOW)
        self.harness.replies = {
            "pr list": json.dumps(
                [{"number": 42, "url": "https://github.com/acme/fleet/pull/42"}]
            ),
            "--json body": json.dumps({"body": previous_body}),
        }
        self.touch("clusters/prod-us-east/payments-netpol.yaml")

        self.run_finish(doc)

        # Body still refreshed, but silence when nothing changed.
        self.assertTrue(self.harness.matching("pr", "edit", "--title"))
        self.assertFalse(self.harness.matching("pr", "comment"))
        result = self.stdout_json()
        self.assertEqual(result["status"], "UPDATED")
        self.assertEqual(result["new"], 0)
        self.assertEqual(result["resolved"], 0)

    def test_empty_commit_when_no_manifest_remediations(self):
        self.harness.replies = {
            "pr list": "[]",
            "pr create": "https://github.com/acme/fleet/pull/9\n",
        }
        doc = make_doc(
            findings=[
                make_finding(remediation={"kind": "gcloud", "note": "gcloud ..."})
            ]
        )
        self.assertEqual(self.run_finish(doc), 0)
        self.assertEqual(self.git_add_calls(self.harness), [])
        self.assertIn("--allow-empty", self.harness.matching("git", "commit")[0])

    def test_missing_remediation_file_is_a_hard_error(self):
        self.harness.replies = {"pr list": "[]"}
        # Deliberately do NOT create the manifest on disk.
        rc = self.run_finish(make_doc())
        self.assertEqual(rc, 2)
        self.assertIn("does not exist under the repository root", self.err)
        self.assertEqual(self.git_add_calls(self.harness), [])
        self.assertFalse(self.harness.matching("git", "push"))


class TestFinishClean(HarnessTestCase):
    def test_clean_run_closes_the_open_pr(self):
        previous_body = audit_pr.render_body(
            make_doc(findings=[make_finding(fid="a"), make_finding(fid="b")]),
            staged_paths=[],
            generated_at=NOW,
        )
        self.harness.replies = {
            "pr list": json.dumps(
                [{"number": 42, "url": "https://github.com/acme/fleet/pull/42"}]
            ),
            "--json body": json.dumps({"body": previous_body}),
        }

        rc = self.run_finish(make_doc(findings=[]))
        self.assertEqual(rc, 0)

        self.assertTrue(self.harness.matching("pr", "comment", "42"))
        self.assertTrue(self.harness.matching("pr", "close", "42"))
        # Nothing is committed, pushed, or deleted on a clean run.
        self.assertFalse(self.harness.matching("git", "push"))
        self.assertFalse(self.harness.matching("git", "commit"))
        self.assertFalse(self.harness.matching("branch", "-D"))

        self.assertEqual(
            self.stdout_json(),
            {
                "status": "CLEAN",
                "pr_url": "https://github.com/acme/fleet/pull/42",
                "new": 0,
                "resolved": 2,
            },
        )

    def test_clean_run_with_no_open_pr_is_a_no_op(self):
        self.harness.replies = {"pr list": "[]"}
        self.assertEqual(self.run_finish(make_doc(findings=[])), 0)
        self.assertFalse(self.harness.matching("pr", "close"))
        self.assertFalse(self.harness.matching("pr", "comment"))
        self.assertEqual(
            self.stdout_json(),
            {
                "status": "CLEAN",
                "pr_url": None,
                "new": 0,
                "resolved": 0,
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

    def test_emits_one_json_line(self):
        self.harness.replies = {
            "pr list": json.dumps(
                [{"number": 42, "url": "https://github.com/acme/fleet/pull/42"}]
            )
        }
        self.assertEqual(self.run_main(["start", "--audit", AUDIT]), 0)

        out = self.out.strip()
        self.assertNotIn("\n", out)
        self.assertEqual(
            json.loads(out),
            {
                "branch": "platform-agent/audit-compliance-audit",
                "existing_pr": 42,
                "repo": "acme/fleet",
                "findings_path": "/opt/data/scratch/findings_compliance-audit.json",
            },
        )

    def test_null_existing_pr_when_none_open(self):
        self.harness.replies = {"pr list": "[]"}
        self.run_main(["start", "--audit", "obtainability-audit"])
        self.assertIsNone(json.loads(self.out)["existing_pr"])

    def test_creates_branch_and_labels(self):
        self.harness.replies = {"pr list": "[]"}
        self.run_main(["start", "--audit", AUDIT])

        self.assertTrue(self.harness.matching("git", "checkout", "main"))
        self.assertTrue(
            self.harness.matching(
                "git", "checkout", "-B", "platform-agent/audit-compliance-audit"
            )
        )
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


if __name__ == "__main__":
    unittest.main()
