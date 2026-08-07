#!/usr/bin/env python3
"""Host tests for cron_tick_lock_scope. No Hermes install required.

These cover the two properties the in-image gate cannot cheaply reach: that
``release()`` is safe to call twice (the patch calls it twice by design, once
at dispatch and once in the untouched ``finally``), and that ``JobLocks``
fails toward FIRING rather than toward silence.
"""

import fcntl
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cron_tick_lock_scope import (  # noqa: E402
    JobLocks, sanitise_job_id, try_acquire,
)

# A real second process, because that is the only claimant that matters: every
# platform tick is a freshly spawned `hermes cron tick`, so the guard is
# worthless if it only holds within one interpreter.
_HOLDER = """
import fcntl, sys, time
sys.path.insert(0, {here!r})
from cron_tick_lock_scope import JobLocks
from pathlib import Path
locks = JobLocks(lambda: Path({tmp!r}), fcntl)
print("CLAIMED" if locks.claim("github-issue-resolver") else "REFUSED", flush=True)
time.sleep(30)
"""


class LockScopeTests(unittest.TestCase):
    """Every case gets its own lock directory, so none can see another's files."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)

    def acquire(self, name):
        """``try_acquire`` whose handle is closed at teardown, or None.

        An unreleased ``AdvisoryLock`` leaks its file handle, and unittest
        surfaces that as a ResourceWarning on every CI run — noise that trains
        readers to ignore warnings from this suite.
        """
        lock = try_acquire(self.tmp / name, fcntl)
        if lock is not None:
            self.addCleanup(lock.release)
        return lock

    def job_locks(self, lock_dir_fn=None):
        """A ``JobLocks`` whose surviving claims are released at teardown.

        Same reason as ``acquire``: a claim left held leaks its handle and the
        suite reports a ResourceWarning it did not mean to.
        """
        locks = JobLocks(lock_dir_fn or (lambda: self.tmp), fcntl)
        # Straight off `_held`'s values rather than through `release()`, which
        # re-resolves the store — the point is to close handles at teardown,
        # not to re-exercise the lookup.
        self.addCleanup(lambda: [lk.release() for lk in list(locks._held.values())])
        return locks

    def test_release_is_idempotent(self):
        lock = self.acquire("a.lock")
        self.assertIsNotNone(lock)
        self.assertFalse(lock.released)
        lock.release()
        self.assertTrue(lock.released)
        lock.release()  # must not raise, must not reopen
        self.assertIsNotNone(self.acquire("a.lock"))

    def test_release_closes_handle_with_no_locking_module(self):
        lock = try_acquire(self.tmp / "b.lock", None, None)  # released below
        self.assertIsNotNone(lock)
        handle = lock._handle
        lock.release()
        self.assertTrue(handle.closed)

    def test_second_acquire_fails_while_held(self):
        first = self.acquire("c.lock")
        self.assertIsNotNone(first)
        self.assertIsNone(self.acquire("c.lock"))
        first.release()
        self.assertIsNotNone(self.acquire("c.lock"))

    def test_joblocks_claim_and_release(self):
        a = self.job_locks()
        b = self.job_locks()  # stands in for a sibling process
        self.assertIs(a.claim("github-issue-resolver"), True)
        self.assertIs(b.claim("github-issue-resolver"), False)
        self.assertIs(b.claim("compliance-audit"), True)
        a.release("github-issue-resolver")
        self.assertIs(b.claim("github-issue-resolver"), True)

    def test_joblocks_second_claim_by_the_same_registry_is_refused(self):
        """The in-process half of the guard: a registry never claims twice."""
        a = self.job_locks()
        self.assertIs(a.claim("github-issue-resolver"), True)
        self.assertIs(a.holds("github-issue-resolver"), True)
        self.assertIs(a.claim("github-issue-resolver"), False)
        a.release("github-issue-resolver")
        self.assertIs(a.holds("github-issue-resolver"), False)

    def test_joblocks_is_honoured_across_real_processes(self):
        """The property the patch actually needs: two OS processes, one winner.

        Also proves the kernel — not a janitor — hands the claim back, which is
        the whole reason flock was chosen over a ledger row.
        """
        src = _HOLDER.format(here=str(Path(__file__).parent), tmp=str(self.tmp))
        holder = subprocess.Popen([sys.executable, "-c", src],
                                  stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "CLAIMED")
            mine = self.job_locks()
            self.assertIs(
                mine.claim("github-issue-resolver"), False,
                "a second process claimed a job another process is running")
            self.assertIs(
                mine.claim("compliance-audit"), True,
                "the holder's claim leaked onto an unrelated job id")
        finally:
            holder.kill()
            holder.wait(timeout=30)
            if holder.stdout is not None:
                holder.stdout.close()
        # SIGKILL leaves no chance to release cleanly; the kernel must do it.
        deadline = time.monotonic() + 10
        reclaimed = False
        while time.monotonic() < deadline and not reclaimed:
            reclaimed = self.job_locks().claim("github-issue-resolver")
            if not reclaimed:
                time.sleep(0.1)
        self.assertTrue(
            reclaimed, "the claim outlived the SIGKILLed process that held it")

    def test_joblocks_release_of_unknown_id_is_a_noop(self):
        self.job_locks().release("never-claimed")

    def test_joblocks_fails_toward_firing(self):
        """An unusable lock directory must not silence cron."""
        def broken():
            raise OSError("read-only file system")
        self.assertIs(self.job_locks(broken).claim("compliance-audit"), True)

    def test_joblocks_fails_toward_firing_on_a_non_oserror(self):
        """Only a held flock may answer False — not a surprising exception.

        In the image ``lock_dir_fn`` reaches ``get_hermes_home()``. A
        ValueError out of there once meant the whole tick aborted having
        dispatched nothing.
        """
        def broken():
            raise ValueError("HERMES_HOME is not a path")
        self.assertIs(self.job_locks(broken).claim("compliance-audit"), True)

    def test_joblocks_fails_toward_firing_when_the_lock_file_will_not_open(self):
        """The reachable half of the fail-open rule.

        ``mkdir(parents=True, exist_ok=True)`` succeeds on an EXISTING
        read-only directory, so the directory guard never fires for the case
        that actually happens: `<HERMES_HOME>/cron/` present but unwritable, or
        the volume out of space. If the open failure were read as "held", every
        job on the profile would stop firing while the log claimed each one was
        already running elsewhere.
        """
        def refuse(*_args, **_kwargs):
            raise PermissionError("read-only lock directory")
        locks = JobLocks(lambda: self.tmp, fcntl, None, refuse)
        self.assertIs(locks.claim("compliance-audit"), True)
        self.assertIs(locks.holds("compliance-audit"), False)

    def test_joblocks_separates_two_stores_that_share_a_job_id(self):
        """One process may tick several profiles; the store is part of the id."""
        first, second = self.tmp / "a" / "cron", self.tmp / "b" / "cron"
        store = [first]
        locks = self.job_locks(lambda: store[0])
        self.assertIs(locks.claim("compliance-audit"), True)
        store[0] = second
        self.assertIs(locks.claim("compliance-audit"), True,
                      "a claim on one profile refused the same id on another")
        store[0] = first
        self.assertIs(locks.holds("compliance-audit"), True)
        locks.release("compliance-audit")
        self.assertIs(locks.holds("compliance-audit"), False)
        store[0] = second
        self.assertIs(locks.holds("compliance-audit"), True,
                      "releasing one store's claim released the other's")

    def test_sanitise_job_id(self):
        self.assertEqual(sanitise_job_id("github-issue-resolver"),
                         "github-issue-resolver")
        self.assertNotIn("/", sanitise_job_id("../../etc/passwd"))
        self.assertEqual(sanitise_job_id(""), "_")
        self.assertEqual(len(sanitise_job_id("x" * 500)), 120)


if __name__ == "__main__":
    unittest.main()
