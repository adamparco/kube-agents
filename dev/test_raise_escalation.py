#!/usr/bin/env python3
"""Hermetic tests for the escalation round-trip (Phase 4 D5).

Dependency-free (stdlib unittest + real local git repos) so the indirect cross-tier coordination path
is provable offline, with NO real GitHub. Proves:
  - raise-escalation writes a valid OKF `escalation` entry and produces the corrective-PR artifact
    (branch + diff) under --dry-run, with NO push / NO PR / NO token broker;
  - the full round-trip: a lower tier raises it → (human merge) → the parent retrieves it via
    read-knowledge, and okf-validate accepts the entry — all through GitOps, never a direct call;
  - raise-escalation refuses the top (platform) tier;
  - both lower-tier copies of the helper are byte-identical.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_DEV = REPO_ROOT / "dev"
# developer-team escalates to cluster-admin; its helper self-adds the submit-suggestion + token paths.
DT_RAISE = REPO_ROOT / "agents/developer-team/skills/raise-escalation/scripts"
DT_READ = REPO_ROOT / "agents/developer-team/skills/read-knowledge/scripts"

sys.path.insert(0, str(DT_RAISE))
sys.path.insert(0, str(DT_READ))
sys.path.insert(0, str(LOCAL_DEV))

import raise_escalation  # noqa: E402
import read_knowledge  # noqa: E402
import submit_suggestion  # noqa: E402  (same module raise_escalation imports)


def _git(cwd: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", cwd, *args], check=True, capture_output=True, text=True
    ).stdout


def _make_gitops_repo(root: str) -> None:
    """A GitOps repo on main with a knowledge/ tree and a deployable clusters/ tree."""
    os.makedirs(os.path.join(root, "knowledge", "escalation"))
    os.makedirs(os.path.join(root, "clusters", "cluster-a"))
    with open(os.path.join(root, "knowledge", "index.md"), "w") as fh:
        fh.write("---\ntype: index\ntitle: KB\n---\n\n# Knowledge Base\n")
    with open(os.path.join(root, "knowledge", "escalation", ".gitkeep"), "w") as fh:
        fh.write("")
    with open(os.path.join(root, "clusters", "cluster-a", "ns.yaml"), "w") as fh:
        fh.write("apiVersion: v1\nkind: Namespace\nmetadata:\n  name: team-x\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed gitops repo")


class _NoNetwork:
    """Sabotage every write/network side effect for the duration of a dry run."""

    def __init__(self, tc: unittest.TestCase) -> None:
        self.tc = tc
        self._saved: dict = {}

    def _boom(self, name):
        def _f(*_a, **_k):
            self.tc.fail(f"dry-run escalation must not call {name}()")
        return _f

    def __enter__(self):
        for name in ("refresh_git_credentials", "push_branch", "create_pull_request"):
            self._saved[name] = getattr(submit_suggestion, name)
            setattr(submit_suggestion, name, self._boom(name))
        return self

    def __exit__(self, *exc):
        for name, fn in self._saved.items():
            setattr(submit_suggestion, name, fn)
        return False


class TestRaiseEscalation(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = tempfile.mkdtemp(prefix="gitops-src-")
        _make_gitops_repo(self.repo)

    def tearDown(self) -> None:
        subprocess.run(["rm", "-rf", self.repo], check=False)

    def test_dry_run_writes_valid_entry_and_artifact_without_push(self) -> None:
        artifact = tempfile.mkdtemp(prefix="esc-artifact-")
        try:
            with _NoNetwork(self):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = raise_escalation.run([
                        "--repo", self.repo, "--ref", "main",
                        "--title", "team-x needs cluster egress",
                        "--summary", "team-x must reach restricted.googleapis.com; cluster-scoped, outside our authority.",
                        "--tier", "developer-team",
                        "--created", "2026-07-24",
                        "--dry-run", "--artifact-dir", artifact,
                    ])
            self.assertEqual(rc, 0)
            branch = (Path(artifact) / "branch.txt").read_text().strip()
            self.assertEqual(branch, "developer-team-agent/escalation-team-x-needs-cluster-egress")
            diff = (Path(artifact) / "suggestion.diff").read_text()
            self.assertIn("knowledge/escalation/team-x-needs-cluster-egress.md", diff)
            self.assertIn("type: escalation", diff)
            self.assertIn("from: developer-team", diff)
            # `to:` is advisory only — present, but the parent ignores it.
            self.assertIn("to: cluster-admin", diff)
            self.assertIn("ADVISORY", diff)
        finally:
            subprocess.run(["rm", "-rf", artifact], check=False)

    def test_round_trip_parent_reads_via_read_knowledge(self) -> None:
        """dev-team raises → human merges the escalation PR → the parent retrieves it via read-knowledge,
        and okf-validate accepts the entry. No direct agent→agent call anywhere in the path."""
        work = tempfile.mkdtemp(prefix="gitops-work-")
        try:
            # A working copy the lower tier writes into (full clone incl. clusters/).
            subprocess.run(["git", "clone", "-q", self.repo, work], check=True, capture_output=True, text=True)
            with _NoNetwork(self):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = raise_escalation.run([
                        "--work-dir", work,
                        "--title", "team-x needs cluster egress",
                        "--summary", "cluster-scoped request outside namespace authority.",
                        "--tier", "developer-team",
                        "--created", "2026-07-24",
                        "--dry-run",
                    ])
            self.assertEqual(rc, 0)
            # Simulate the human merging the escalation PR into main.
            branch = "developer-team-agent/escalation-team-x-needs-cluster-egress"
            _git(work, "checkout", "-q", "main")
            _git(work, "merge", "-q", "--no-edit", branch)

            # okf-validate accepts the merged knowledge tree.
            res = subprocess.run(
                [sys.executable, str(LOCAL_DEV / "okf-validate.py"), os.path.join(work, "knowledge")],
                capture_output=True, text=True,
            )
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

            # The PARENT retrieves the escalation via the read-only sparse path (clones work@main).
            rc_read = read_knowledge.run(["--repo", work, "--ref", "main", "--type", "escalation"])
            self.assertEqual(rc_read, 0)
        finally:
            subprocess.run(["rm", "-rf", work], check=False)

    def test_refuses_platform_tier(self) -> None:
        with self.assertRaises(ValueError):
            raise_escalation.resolve_tier("platform")


class TestTierCopiesIdentical(unittest.TestCase):
    def test_helper_and_skill_identical_across_lower_tiers(self) -> None:
        for rel in (
            "skills/raise-escalation/scripts/raise_escalation.py",
            "skills/raise-escalation/SKILL.md",
        ):
            ca = (REPO_ROOT / "agents/cluster-admin" / rel).read_bytes()
            dt = (REPO_ROOT / "agents/developer-team" / rel).read_bytes()
            self.assertEqual(ca, dt, f"{rel} differs across lower tiers")


if __name__ == "__main__":
    unittest.main()
