#!/usr/bin/env python3
"""Hermetic tests for the read-knowledge path + shared-parser parity (Phase 4 D4).

Dependency-free (stdlib unittest + a real local git repo) so the read-only OKF path is provable in the
offline build inner loop. Proves:
  - a runbook is retrieved by type from a sparse checkout;
  - the sparse checkout materializes ONLY knowledge/ (no clusters/) — the read path can't fetch a
    deployable tree;
  - the read path hard-refuses push/commit intent;
  - read_knowledge and okf-validate import the SAME parser (no schema drift).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_DEV = REPO_ROOT / "local-dev"
PLATFORM_READ = REPO_ROOT / "agents/platform/skills/read-knowledge/scripts"

sys.path.insert(0, str(LOCAL_DEV))
sys.path.insert(0, str(PLATFORM_READ))

import okf_frontmatter  # noqa: E402
import read_knowledge  # noqa: E402


def _git(cwd: str, *args: str) -> None:
    subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, text=True)


def _make_gitops_repo(root: str) -> None:
    """Build a minimal GitOps repo on branch main: knowledge/ (an index + a runbook) plus a deployable
    clusters/ tree the read path must NOT materialize."""
    os.makedirs(os.path.join(root, "knowledge", "runbook"))
    os.makedirs(os.path.join(root, "clusters", "cluster-a", "agents"))
    with open(os.path.join(root, "knowledge", "index.md"), "w") as fh:
        fh.write("---\ntype: index\ntitle: KB\n---\n\n# Knowledge Base\n")
    with open(os.path.join(root, "knowledge", "runbook", "pod-crashloop.md"), "w") as fh:
        fh.write(
            "---\ntype: runbook\ntitle: Pod CrashLoopBackOff\nstatus: active\n---\n\n"
            "# CrashLoopBackOff\n\nCheck the container logs and recent image change.\n"
        )
    # A deployable manifest that must never be checked out by a read.
    with open(os.path.join(root, "clusters", "cluster-a", "agents", "agent.yaml"), "w") as fh:
        fh.write("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: should-not-be-read\n")

    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed gitops repo")


class TestReadKnowledge(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = tempfile.mkdtemp(prefix="gitops-src-")
        _make_gitops_repo(self.repo)

    def tearDown(self) -> None:
        subprocess.run(["rm", "-rf", self.repo], check=False)

    def test_sparse_checkout_materializes_only_knowledge(self) -> None:
        work = tempfile.mkdtemp(prefix="okf-work-")
        try:
            read_knowledge.sparse_checkout_knowledge(self.repo, "main", work, token=None)
            # knowledge/ present, deployable clusters/ absent.
            self.assertTrue(os.path.isdir(os.path.join(work, "knowledge")))
            self.assertFalse(
                os.path.isdir(os.path.join(work, "clusters")),
                "read path leaked the deployable clusters/ tree",
            )
            # assert_only_knowledge agrees (fails closed otherwise).
            read_knowledge.assert_only_knowledge(work)
            entries = read_knowledge.collect_entries(os.path.join(work, "knowledge"))
            runbooks = [e for e in entries if e["type"] == "runbook"]
            self.assertEqual(len(runbooks), 1)
            self.assertEqual(runbooks[0]["link"], os.path.join("runbook", "pod-crashloop.md"))
        finally:
            subprocess.run(["rm", "-rf", work], check=False)

    def test_run_lists_runbook_by_type(self) -> None:
        rc = read_knowledge.run(["--repo", self.repo, "--ref", "main", "--type", "runbook"])
        self.assertEqual(rc, 0)

    def test_run_reads_entry_by_link(self) -> None:
        rc = read_knowledge.run(
            ["--repo", self.repo, "--ref", "main", "--link", "runbook/pod-crashloop.md"]
        )
        self.assertEqual(rc, 0)

    def test_no_match_returns_4(self) -> None:
        rc = read_knowledge.run(["--repo", self.repo, "--ref", "main", "--type", "metric-definition"])
        self.assertEqual(rc, 4)

    def test_refuses_push_intent(self) -> None:
        # A smuggled write token anywhere on the command line fails closed before any git runs.
        # run() raises WriteRefused; main() maps it to exit code 3.
        with self.assertRaises(read_knowledge.WriteRefused):
            read_knowledge.run(["--repo", self.repo, "push"])
        argv = sys.argv
        try:
            sys.argv = ["read_knowledge.py", "--repo", self.repo, "commit"]
            self.assertEqual(read_knowledge.main(), 3)
        finally:
            sys.argv = argv

    def test_git_wrapper_refuses_non_readonly_subcommand(self) -> None:
        with self.assertRaises(read_knowledge.WriteRefused):
            read_knowledge.git(["-C", self.repo, "commit", "-m", "nope"])
        with self.assertRaises(read_knowledge.WriteRefused):
            read_knowledge.git(["-C", self.repo, "push"])


class TestParserParity(unittest.TestCase):
    """okf-validate and read-knowledge must use the SAME parser (no drift)."""

    def test_read_knowledge_uses_shared_parser(self) -> None:
        self.assertIs(read_knowledge.parse_frontmatter, okf_frontmatter.parse_frontmatter)

    def test_okf_validate_uses_shared_parser(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "okf_validate_mod", str(LOCAL_DEV / "okf-validate.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertIs(mod.parse_frontmatter, okf_frontmatter.parse_frontmatter)


if __name__ == "__main__":
    unittest.main()
