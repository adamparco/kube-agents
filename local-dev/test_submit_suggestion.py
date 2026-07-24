#!/usr/bin/env python3
"""Hermetic tests for submit_suggestion.py --dry-run (Phase 4 D3).

Dependency-free (stdlib unittest + a real local git repo) so the corrective-PR artifact path is
provable in the offline build inner loop, with NO real GitHub. Proves:
  - --dry-run emits the diff (branch vs main) and, with --artifact-dir, writes the artifact files;
  - --dry-run performs NO `git push` and NO `gh pr create` (both are sabotaged to fail loudly if
    ever invoked, and the remote is asserted to have received nothing);
  - --dry-run is hermetic: the token broker (refresh_git_credentials) is never called;
  - tier scoping still fails closed under --dry-run (a developer-team branch under the wrong prefix,
    or a protected branch, is refused before any artifact is produced);
  - all three tier copies are byte-identical.
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
PLATFORM_SCRIPTS = REPO_ROOT / "agents/platform/scripts"  # for github_token_refresh import
SUBMIT_SCRIPTS = REPO_ROOT / "agents/platform/skills/submit-suggestion/scripts"

sys.path.insert(0, str(PLATFORM_SCRIPTS))
sys.path.insert(0, str(SUBMIT_SCRIPTS))

import submit_suggestion  # noqa: E402


def _git(cwd: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", cwd, *args], check=True, capture_output=True, text=True
    ).stdout


def _make_proposal_repo(root: str, branch: str) -> None:
    """A GitOps repo on `main` with a committed change on `branch` — the state the agent is in when
    it calls submit-suggestion (local branch + commit already made)."""
    manifest = os.path.join(root, "clusters", "cluster-a", "agents")
    os.makedirs(manifest)
    target = os.path.join(manifest, "netpol.yaml")
    with open(target, "w") as fh:
        fh.write("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: before\n")

    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")

    _git(root, "checkout", "-q", "-b", branch)
    with open(target, "w") as fh:
        fh.write("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: after\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "propose: rename to after")


class _NoNetwork:
    """Context manager that sabotages every network/write side effect for the duration of a dry run:
    refresh_git_credentials, push_branch and create_pull_request all raise if called. A clean dry run
    must never touch any of them."""

    def __init__(self, tc: unittest.TestCase) -> None:
        self.tc = tc
        self._saved: dict = {}

    def _boom(self, name):
        def _f(*_a, **_k):
            self.tc.fail(f"--dry-run must not call {name}()")
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


class TestDryRun(unittest.TestCase):
    def setUp(self) -> None:
        self.branch = "platform-agent/fix-netpol"
        self.repo = tempfile.mkdtemp(prefix="submit-src-")
        _make_proposal_repo(self.repo, self.branch)
        self._cwd = os.getcwd()
        os.chdir(self.repo)

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        subprocess.run(["rm", "-rf", self.repo], check=False)

    def test_dry_run_emits_diff_and_artifact_without_push_or_pr(self) -> None:
        artifact = tempfile.mkdtemp(prefix="submit-artifact-")
        try:
            with _NoNetwork(self):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    diff = submit_suggestion.dry_run(
                        self.branch, "platform", "Fix netpol", "corrective PR", artifact
                    )
            # The diff reflects the committed change (before -> after).
            self.assertIn("name: after", diff)
            self.assertIn("name: before", diff)
            self.assertIn("name: after", buf.getvalue())  # also on stdout
            # Artifact files exist and carry the proposal.
            self.assertEqual(
                (Path(artifact) / "branch.txt").read_text().strip(), self.branch
            )
            self.assertIn("name: after", (Path(artifact) / "suggestion.diff").read_text())
            self.assertIn("Fix netpol", (Path(artifact) / "pr.md").read_text())
            # No push happened: the repo has no remote and no new remote refs.
            remotes = _git(self.repo, "remote")
            self.assertEqual(remotes.strip(), "", "dry-run must not configure/push a remote")
        finally:
            subprocess.run(["rm", "-rf", artifact], check=False)

    def test_dry_run_via_main_exits_zero_and_prints_diff(self) -> None:
        """The full argv path (main) must take the dry-run branch, print the diff, and never call the
        token broker / push / PR helpers."""
        argv = sys.argv
        try:
            with _NoNetwork(self):
                sys.argv = [
                    "submit_suggestion.py",
                    "--branch", self.branch,
                    "--title", "Fix netpol",
                    "--body", "corrective PR",
                    "--tier", "platform",
                    "--dry-run",
                ]
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = submit_suggestion.main()  # returns None (no sys.exit) on success
            self.assertIsNone(rc)
            self.assertIn("name: after", buf.getvalue())
        finally:
            sys.argv = argv

    def test_dry_run_refuses_wrong_tier_prefix(self) -> None:
        with _NoNetwork(self):
            with self.assertRaises(ValueError):
                submit_suggestion.dry_run(
                    self.branch, "developer-team", "x", "y", None
                )  # platform-agent/ branch, developer-team tier

    def test_dry_run_refuses_protected_branch(self) -> None:
        with _NoNetwork(self):
            with self.assertRaises(ValueError):
                submit_suggestion.dry_run("main", "platform", "x", "y", None)

    def test_dry_run_requires_existing_local_branch(self) -> None:
        with _NoNetwork(self):
            with self.assertRaises(ValueError):
                submit_suggestion.dry_run(
                    "platform-agent/does-not-exist", "platform", "x", "y", None
                )


class TestTierCopiesIdentical(unittest.TestCase):
    def test_all_three_submit_suggestion_identical(self) -> None:
        paths = [
            REPO_ROOT / f"agents/{t}/skills/submit-suggestion/scripts/submit_suggestion.py"
            for t in ("platform", "cluster-admin", "developer-team")
        ]
        blobs = [p.read_bytes() for p in paths]
        self.assertEqual(blobs[0], blobs[1])
        self.assertEqual(blobs[0], blobs[2])


if __name__ == "__main__":
    unittest.main()
