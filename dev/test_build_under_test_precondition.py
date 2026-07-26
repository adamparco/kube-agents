#!/usr/bin/env python3
"""P1's freshness judgment, exercised against a fake repository and fake registry tags (L0).

WHY THIS EXISTS. P1 answers "is the cluster running the build under test", and its second half --
"was that build made from the source as it stands" -- has already failed silently once: it passed
green over an operator six hours and twenty-five minutes older than the source it claimed to be
built from, which is LSN-001's actual recurrence mode occurring inside the check written to end it.
That defect was found by running the check, and a run only ever exercises one branch.

There are six outcomes here and a live cluster can produce exactly one of them: the good one. Every
other branch -- a build of a different commit, a digest with no commit-carrying tag, an uncommitted
edit that postdates the build -- requires arranging a repository and a registry into a state nobody
would arrange on purpose. So both are faked, and the seam is the function's own signature:
`_p1_assert_tag_is_current <image> <tags>` takes the registry's answer as a STRING, so the registry
is a Python literal here, and the git half is a real throwaway repository in /tmp.

What this does NOT cover, and where it is covered instead: the digest lookup itself (does the
running imageID resolve in Artifact Registry) is not hermetic by nature -- it is a question asked of
a remote registry -- and is verified live in the P1 rows of the LEDGER.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PRECONDITIONS = REPO / "dev/lib/preconditions.sh"

# Any digest-bearing reference whose last path segment maps to a build input in _p1_build_inputs.
OPERATOR = "us-east4-docker.pkg.dev/p/kube-agents/k8s-operator@sha256:" + "de" * 32
# ...and one that does not. An unmapped image is state 3 by design: the digest resolved, so the
# cluster runs something from the right registry, and nothing here can say whether it is current.
UNMAPPED = "us-east4-docker.pkg.dev/p/kube-agents/platform-agent@sha256:" + "de" * 32


class Fixture:
    """A throwaway git repo shaped like this one: a k8s-operator/ directory with one commit."""

    def __init__(self, tmp: str):
        self.dir = Path(tmp)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "p1@example.invalid")
        self._git("config", "user.name", "P1 Fixture")
        (self.dir / "k8s-operator").mkdir()
        (self.dir / "k8s-operator/main.go").write_text("package main\n")
        (self.dir / "docs").mkdir()
        (self.dir / "docs/prose.md").write_text("words\n")
        self._git("add", "k8s-operator/main.go", "docs/prose.md")
        self._git("commit", "-qm", "initial")
        self.head = self._git("rev-parse", "--short", "HEAD").strip()

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.dir, capture_output=True, text=True, check=True
        ).stdout

    def dirty(self, rel: str, mtime: int) -> None:
        """Leave an uncommitted edit with an mtime we control, so the epoch comparison is exact."""
        p = self.dir / rel
        p.write_text(p.read_text() + "// edited\n")
        os.utime(p, (mtime, mtime))

    def judge(self, image: str, tags: str) -> tuple[int, str]:
        script = (
            f'. "{PRECONDITIONS}"\n'
            f'_p1_assert_tag_is_current "{image}" "{tags}"\n'
            'echo "P1_RC=$?"\n'
        )
        proc = subprocess.run(
            ["bash", "-c", script], cwd=self.dir, capture_output=True, text=True
        )
        out = proc.stdout + proc.stderr
        marker = [ln for ln in proc.stdout.splitlines() if ln.startswith("P1_RC=")]
        assert marker, f"P1 emitted no verdict line:\n{out}"
        return int(marker[-1].split("=", 1)[1]), out


class TestP1Freshness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fx = Fixture(self._tmp.name)

    # --- clean tree ---------------------------------------------------------------------------
    def test_tag_naming_head_on_a_clean_tree_is_fresh(self):
        rc, out = self.fx.judge(OPERATOR, f"buildcache dev-{self.fx.head}")
        self.assertEqual(rc, 0, out)

    def test_the_makefile_src_prefix_counts_as_provenance_too(self):
        """`make cloud-build-push` tags src-<sha>; reload-images.sh tags dev-<sha>. Both say where."""
        rc, out = self.fx.judge(OPERATOR, f"src-{self.fx.head} latest")
        self.assertEqual(rc, 0, out)

    def test_a_build_of_a_different_commit_is_a_failure_not_a_deferral(self):
        rc, out = self.fx.judge(OPERATOR, "dev-0000000")
        self.assertEqual(rc, 1, out)
        self.assertIn("DIFFERENT COMMIT", out)

    def test_the_right_tag_among_several_is_the_one_that_decides(self):
        """A digest carries every tag ever pushed to it, including stale ones from earlier builds."""
        rc, out = self.fx.judge(OPERATOR, f"dev-0000000 buildcache dev-{self.fx.head}")
        self.assertEqual(rc, 0, out)

    def test_a_digest_with_no_commit_carrying_tag_is_undecidable(self):
        rc, out = self.fx.judge(OPERATOR, "buildcache latest")
        self.assertEqual(rc, 3, out)

    def test_an_unmapped_image_is_undecidable_and_says_so(self):
        rc, out = self.fx.judge(UNMAPPED, f"dev-{self.fx.head}")
        self.assertEqual(rc, 3, out)
        self.assertIn("_p1_build_inputs", out)

    # --- dirty tree ---------------------------------------------------------------------------
    #
    # The case the harness actually lives in. reload-images.sh stamps `-dirty-<epoch>` into the tag
    # when the tree is dirty precisely so this comparison is possible: a commit sha cannot speak for
    # an uncommitted edit, and the build's own timestamp is the only evidence left.
    def test_a_clean_build_with_uncommitted_operator_changes_is_stale(self):
        self.fx.dirty("k8s-operator/main.go", 1_700_000_000)
        rc, out = self.fx.judge(OPERATOR, f"dev-{self.fx.head}")
        self.assertEqual(rc, 1, out)
        self.assertIn("UNCOMMITTED", out)

    def test_a_dirty_build_newer_than_the_edit_is_fresh(self):
        self.fx.dirty("k8s-operator/main.go", 1_700_000_000)
        rc, out = self.fx.judge(OPERATOR, f"dev-{self.fx.head}-dirty-1700000010")
        self.assertEqual(rc, 0, out)

    def test_a_dirty_build_older_than_the_edit_is_stale(self):
        self.fx.dirty("k8s-operator/main.go", 1_700_000_000)
        rc, out = self.fx.judge(OPERATOR, f"dev-{self.fx.head}-dirty-1699999990")
        self.assertEqual(rc, 1, out)
        self.assertIn("BEFORE the newest edit", out)

    def test_dirt_outside_the_build_inputs_does_not_make_the_image_stale(self):
        """A docs edit must not mark the operator stale. A check that fires on unrelated changes
        gets ignored on the day it is right."""
        self.fx.dirty("docs/prose.md", 4_100_000_000)
        rc, out = self.fx.judge(OPERATOR, f"dev-{self.fx.head}")
        self.assertEqual(rc, 0, out)


class TestP1Helpers(unittest.TestCase):
    def _sh(self, snippet: str) -> str:
        return subprocess.run(
            ["bash", "-c", f'. "{PRECONDITIONS}"\n{snippet}'],
            capture_output=True,
            text=True,
        ).stdout.strip()

    def test_repo_path_strips_tag_and_digest_but_keeps_a_registry_port(self):
        cases = {
            "us-east4-docker.pkg.dev/p/r/img@sha256:ab": "us-east4-docker.pkg.dev/p/r/img",
            "us-east4-docker.pkg.dev/p/r/img:dev-abc123": "us-east4-docker.pkg.dev/p/r/img",
            "localhost:5000/p/img:tag": "localhost:5000/p/img",
            "localhost:5000/p/img": "localhost:5000/p/img",
        }
        for ref, want in cases.items():
            with self.subTest(ref=ref):
                self.assertEqual(self._sh(f'_p1_repo_path "{ref}"'), want)

    def test_abbreviated_shas_compare_as_prefixes_in_both_directions(self):
        """`git rev-parse --short` lengthens as the object count grows, so the tag written at build
        time and the sha computed now can differ in length and still name one commit."""
        self.assertEqual(self._sh('_p1_sha_eq abc1234 abc1234def && echo yes'), "yes")
        self.assertEqual(self._sh('_p1_sha_eq abc1234def abc1234 && echo yes'), "yes")
        self.assertEqual(self._sh('_p1_sha_eq abc1234 abc1235 || echo no'), "no")
        self.assertEqual(self._sh('_p1_sha_eq "" abc1234 || echo no'), "no")


if __name__ == "__main__":
    unittest.main()
