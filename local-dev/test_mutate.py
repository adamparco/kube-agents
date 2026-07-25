#!/usr/bin/env python3
"""The mutation harness has to be safer than the thing it is testing (LSN-022).

Mutation testing is how this build decides whether a check is real: break the
property on purpose, confirm the check goes red, put the file back. The putting-back
is the dangerous half, and on 2026-07-25 it took two hours of finished work with it.

What happened, precisely. Five L2 scripts had just gained hand-written PRECONDITIONS
blocks and P1 digest assertions — written, never committed, not even staged. A
mutation test then mutated three of those same files and reverted with
`git checkout <path>`. `git checkout` restores the path from the INDEX; the index
held HEAD; the unstaged work was overwritten by a version that predated it. No
reflog entry, no stash, no dangling blob: git had never been shown those bytes, so
there was nothing to recover. It surfaced only because the NEXT mutation's output
named three preconditions fields that were supposed to be there and weren't.

The generalization is not "be careful with git checkout". It is that a revert must
be defined by *what the file contained a moment ago*, and git can only answer *what
the file contained at a commit or in the index*. Those two answers coincide exactly
when the tree is clean — which is precisely when a mutation test is least necessary.
So the harness gets its own revert primitive, `local-dev/mutate.sh`, whose restore is
a byte copy taken before the command ran, and this file is what keeps it honest.

Properties under test, each corresponding to a way the primitive could quietly fail:

  * restores after success, after failure, and after a signal — a restore that only
    runs on the happy path is worse than none, because the failure case is the one a
    mutation test is deliberately provoking
  * restores UNSTAGED content in a real git repository — the actual LSN-022 scenario,
    asserted against a file whose working-tree content differs from both HEAD and the
    index, so a `git checkout`-shaped implementation cannot pass
  * passes the command's exit code through — mutation tests compose as
    `mutate.sh f -- sh -c 'break && ! check'`, and a swallowed code makes every
    mutation look successful
  * refuses a path that does not exist, without running the command — a snapshot of a
    missing file restores nothing, so the command would mutate an unprotected path
  * preserves the mode — restoring a verify script without its exec bit turns the
    next L2 run into a mystery
"""

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MUTATE = REPO / "local-dev" / "mutate.sh"


def run(args, **kw):
    return subprocess.run(
        ["bash", str(MUTATE)] + args,
        capture_output=True,
        text=True,
        cwd=kw.pop("cwd", REPO),
        **kw,
    )


class MutateRestores(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _file(self, name, content):
        p = self.dir / name
        p.write_text(content)
        return p

    def test_restores_after_successful_command(self):
        f = self._file("a.txt", "original\n")
        r = run([str(f), "--", "sh", "-c", f"echo mutated > {f}"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(f.read_text(), "original\n")

    def test_restores_after_failing_command(self):
        """The case a mutation test provokes on purpose."""
        f = self._file("a.txt", "original\n")
        r = run([str(f), "--", "sh", "-c", f"echo mutated > {f}; exit 1"])
        self.assertEqual(r.returncode, 1)
        self.assertEqual(f.read_text(), "original\n")

    def test_propagates_exit_code(self):
        f = self._file("a.txt", "x\n")
        for code in (0, 1, 2, 7, 42):
            with self.subTest(code=code):
                r = run([str(f), "--", "sh", "-c", f"exit {code}"])
                self.assertEqual(r.returncode, code)

    def test_restores_every_file(self):
        """Three files mutated, three files back — the LSN-022 blast radius was three."""
        files = [self._file(f"f{i}.txt", f"original-{i}\n") for i in range(3)]
        cmd = "; ".join(f"echo mutated > {f}" for f in files)
        r = run([str(f) for f in files] + ["--", "sh", "-c", cmd])
        self.assertEqual(r.returncode, 0, r.stderr)
        for i, f in enumerate(files):
            self.assertEqual(f.read_text(), f"original-{i}\n")

    def test_restores_on_signal_and_the_command_cannot_write_afterwards(self):
        """SIGTERM mid-command restores, and nothing the command spawned survives to undo it.

        A mutation test that gets interrupted — Ctrl-C, a timeout, a killed session —
        leaves the tree mutated unless the trap covers signals; that mutated tree is
        what the next checkpoint commits. Both halves of this are things the first
        implementation got wrong:

          * bash runs a trap only when the FOREGROUND command returns, so with the
            command in the foreground the signal was queued behind `sleep` and the
            script hung instead of restoring
          * killing the command alone orphaned what IT had spawned, and the orphan
            outlived the restore — still holding the path, free to write the mutation
            back after the script had reported success

        So the assertion is not just "restored" but "still restored a few seconds
        later", with a command that deliberately writes late.
        """
        f = self._file("a.txt", "original\n")
        marker = self.dir / "started"
        proc = subprocess.Popen(
            [
                "bash",
                str(MUTATE),
                str(f),
                "--",
                "sh",
                "-c",
                f"echo mutated > {f}; touch {marker}; sleep 3; echo late-write >> {f}",
            ],
            cwd=REPO,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        def _cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()
            proc.stderr.close()

        self.addCleanup(_cleanup)
        deadline = time.time() + 15
        while not marker.exists() and time.time() < deadline:
            if proc.poll() is not None:
                self.fail(f"mutate.sh exited early: {proc.communicate()}")
            time.sleep(0.05)
        self.assertTrue(marker.exists(), "command never ran")

        proc.send_signal(signal.SIGTERM)
        try:
            # wait(), not communicate(): an orphaned descendant would hold the stdout pipe
            # open and this test would hang on EOF rather than report the real defect.
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("mutate.sh did not exit after SIGTERM")
        self.assertEqual(proc.returncode, 143, "SIGTERM should exit 128+15")
        self.assertEqual(f.read_text(), "original\n")

        time.sleep(4)  # past the command's late write
        self.assertEqual(
            f.read_text(), "original\n", "a survivor of the killed command wrote after the restore"
        )

    def test_preserves_mode(self):
        f = self._file("s.sh", "#!/bin/sh\necho hi\n")
        f.chmod(0o755)
        r = run([str(f), "--", "sh", "-c", f"echo broken > {f}; chmod 600 {f}"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(f.read_text(), "#!/bin/sh\necho hi\n")
        self.assertTrue(os.access(f, os.X_OK), "exec bit not restored")


class MutateRefuses(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_missing_file_is_exit_2_and_command_never_runs(self):
        missing = self.dir / "nope.txt"
        ran = self.dir / "ran"
        r = run([str(missing), "--", "sh", "-c", f"touch {ran}"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("no such file", r.stderr)
        self.assertFalse(ran.exists(), "command ran despite an unsnapshottable path")

    def test_one_missing_among_several_refuses_all(self):
        good = self.dir / "good.txt"
        good.write_text("keep\n")
        ran = self.dir / "ran"
        r = run(
            [str(good), str(self.dir / "gone.txt"), "--", "sh", "-c", f"touch {ran}"]
        )
        self.assertEqual(r.returncode, 2)
        self.assertFalse(ran.exists())

    def test_no_command_is_usage(self):
        f = self.dir / "a.txt"
        f.write_text("x\n")
        self.assertEqual(run([str(f), "--"]).returncode, 2)
        self.assertEqual(run([str(f)]).returncode, 2)
        self.assertEqual(run([]).returncode, 2)


class MutateBeatsGitCheckout(unittest.TestCase):
    """The LSN-022 scenario itself, reproduced.

    A file with three distinct versions live at once: HEAD, the index, and the
    working tree. Only the working-tree version is correct to restore. A
    `git checkout <path>` implementation returns the index version and a
    `git checkout HEAD -- <path>` implementation returns HEAD's; both pass every
    other test in this file, and both are the bug.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
        self.env = env
        for cmd in (
            ["git", "init", "-q", "-b", "main"],
            ["git", "config", "user.email", "t@example.com"],
            ["git", "config", "user.name", "t"],
        ):
            subprocess.run(cmd, cwd=self.dir, env=env, check=True, capture_output=True)

    def _git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.dir, env=self.env, check=True, capture_output=True
        )

    def test_restores_unstaged_working_tree_content(self):
        f = self.dir / "verify.sh"
        f.write_text("committed\n")
        self._git("add", "verify.sh")
        self._git("commit", "-qm", "initial")

        f.write_text("staged\n")
        self._git("add", "verify.sh")

        # The two hours of unstaged work.
        f.write_text("unstaged work nobody has shown git\n")

        r = subprocess.run(
            ["bash", str(MUTATE), "verify.sh", "--", "sh", "-c", "echo mutated > verify.sh"],
            cwd=self.dir,
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            f.read_text(),
            "unstaged work nobody has shown git\n",
            "restored a git-known version instead of what was on disk — this is LSN-022",
        )


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
