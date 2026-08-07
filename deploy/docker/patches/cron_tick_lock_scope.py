#!/usr/bin/env python3
"""Narrow the cron tick lock to the scheduling decision it actually protects.

``cron/scheduler.py::tick`` takes an exclusive ``flock`` on
``<HERMES_HOME>/cron/.tick.lock`` and, when ``sync=True`` (the signature
default), does not release it until the ``finally`` that runs *after*
``concurrent.futures.as_completed`` has waited out every job it dispatched. The
lock therefore covers job EXECUTION, not just the scheduling decision.

That is invisible on the default profile, which the gateway ticks in-process
with ``sync=False``. It is not invisible on the platform profile, which is
ticked only by ``agents/chat/scripts/profile_cron_tick.py`` spawning
``hermes cron tick`` -- whose CLI body is verbatim ``tick(verbose=True)``. So a
fleet audit holds the profile's lock for its whole run, and every spawn during
that window hits ``LOCK_EX | LOCK_NB``, logs a DEBUG skip, and returns 0 having
dispatched nothing.

Measured on the live cluster over 17 hours: 3 of 35 ``github-issue-resolver``
firings blocked, 418s / 179s / 1142s late, each unblocking within seconds of
the blocking audit's ``finished_at``. The dispatcher's own durations in the
default ledger are trimodal and give the mechanism away --
0.10 / 45.14 / 0.12 / 0.13 / 0.11 / 0.12 / 2.37 / 2.29 / 2.65 / 26.85 / 0.12
across 06:18:55-06:38:55: ~0.12s is 'nothing due, no spawn', 45.14s is the
budget handoff at the audit's start, ~2.4s is 'spawned and the child died on
the flock', 26.85s is the first spawn after the audit released. Platform
ledger: 38 terminal executions, 0 overlapping pairs. Default ledger: 707
terminal, 190 overlapping.

The lock's own comment (scheduler.py, above ``advance_next_runs``) states its
purpose: advance ``next_run_at`` for the whole due set BEFORE any execution
begins, 'to preserve at-most-once semantics'. That purpose is served in full by
holding the lock through dispatch. Holding it through execution buys one extra
thing and one only -- a cross-process in-flight guard, because
``_running_job_ids`` is a module-level set and every platform tick is a fresh
process. This module supplies both halves:

``AdvisoryLock``
    Owns the tick lock's file handle with an IDEMPOTENT ``release()``, so the
    dispatch path can release early while the untouched ``finally`` still
    releases on the early-return paths (``can_dispatch`` gate, no due jobs) and
    on any exception.

``JobLocks``
    Replaces what the wide lock was implicitly providing: one ``flock`` file
    per job id, ``<HERMES_HOME>/cron/.job-<id>.lock``, claimed beside
    ``_running_job_ids.add`` and released beside ``_running_job_ids.discard``.

Why flock and not a ledger row. A ledger-based guard was the obvious design and
is wrong here. ``cron/executions.py::_owner_is_live`` returns True on any
exception ('fail safe: inability to prove death must not rewrite state') --
correct for a recovery sweep, but for a dispatch gate True means SUPPRESS, so
an import failure silently kills the job forever. It also returns
``pid == os.getpid()`` when ``process_started_at`` is None, which from a fresh
tick process is always False for a sibling's row -- the guard just vanishes.
And ``recover_interrupted_executions`` is reachable only from
``CronSchedulerProvider.recover_interrupted()``, called by the in-process
ticker and the multiplex loop, NEITHER of which runs for the platform profile:
that ledger has 6 permanent ``status='running'`` rows against 0 on the default
store, four of them on job ids that are still scheduled. A ledger gate would
have wedged them permanently. An flock cannot wedge: the kernel releases it
when the holding process exits, however it exits.

Failure polarity is deliberate, and it is the whole safety argument. This is a
DISPATCH gate: False means "do not run this job". So ``JobLocks.claim`` returns
False for exactly one reason -- a live process holds the flock -- and True for
every other outcome, including an unresolvable store, an unwritable lock
directory, and a lock file that will not open. Getting that backwards would
stop every job on the profile while logging "already running in another
process", which is both silent and actively misleading to whoever debugs it.
Note that ``mkdir(parents=True, exist_ok=True)`` SUCCEEDS on an existing
read-only directory, so the directory guard alone does not cover the case where
``<HERMES_HOME>/cron/`` is unwritable but present -- the open has to be
separated from the flock for the two to be told apart, which is why
``try_acquire`` is split into an open and ``_lock_handle``.

Everything here is dependency-injected (``fcntl``/``msvcrt``/``open`` are
parameters) so ``test_cron_tick_lock_scope.py`` can drive it on a host without
Hermes installed.

Not fixed here, but fixed alongside: ``profile-cron-tick`` used to be scheduled
``interval: 1 minute`` and actually ran every two -- 493 of 545 inter-run gaps
were 120s, only 48 were 60s -- because Hermes re-anchors an ``interval`` job to
the moment the last run FINISHED while the gateway ticker sleeps a fixed sixty
seconds after each tick returns. That is the ``cron-tick-drift`` item, and it is
closed in the same change set by moving every job on both rosters to a cron
expression (``* * * * *``), which snaps up to the next wall-clock minute and so
cannot drift. ``test_profile_cron_tick.py::test_no_job_on_this_roster_uses_an_interval_schedule``
fails the build on any surviving ``interval`` job, so do not reintroduce one.
What remains after that is narrower and is NOT addressed anywhere: a dispatch
that actually ran a watchdog blocks up to ``DEFAULT_BUDGET_SECONDS`` (45) on the
subprocess and usually costs the minute after it. See the schedule bullet in
docs/site/src/content/docs/concepts/autonomous-watchdogs.md.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Callable, Dict, Optional

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def sanitise_job_id(job_id: Any) -> str:
    """Map a job id onto a filename component. Never empty, never a path."""
    cleaned = _UNSAFE.sub("_", str(job_id))[:120]
    return cleaned or "_"


class AdvisoryLock:
    """A held advisory file lock whose ``release()`` is idempotent.

    The tick path calls ``release()`` once it has finished dispatching; the
    untouched ``finally`` calls it again. The second call must be a no-op, and
    must not unlock a handle some other object has since reopened -- hence the
    ``_released`` flag rather than a bare unlock.
    """

    def __init__(self, handle, fcntl_mod=None, msvcrt_mod=None) -> None:
        self._handle = handle
        self._fcntl = fcntl_mod
        self._msvcrt = msvcrt_mod
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._fcntl is not None:
            try:
                self._fcntl.flock(self._handle, self._fcntl.LOCK_UN)
            except (OSError, IOError):
                pass
        elif self._msvcrt is not None:
            try:
                self._msvcrt.locking(self._handle.fileno(), self._msvcrt.LK_UNLCK, 1)
            except (OSError, IOError):
                pass
        try:
            self._handle.close()
        except Exception:
            pass


def _lock_handle(handle, fcntl_mod=None, msvcrt_mod=None) -> Optional[AdvisoryLock]:
    """flock an already-open handle. None -- and closed -- if it is held.

    Split out of ``try_acquire`` so a caller can tell "the file would not open"
    from "another process holds it". ``JobLocks.claim`` has to: the first must
    fire the job, the second must skip it.
    """
    try:
        if fcntl_mod is not None:
            fcntl_mod.flock(handle, fcntl_mod.LOCK_EX | fcntl_mod.LOCK_NB)
        elif msvcrt_mod is not None:
            msvcrt_mod.locking(handle.fileno(), msvcrt_mod.LK_NBLCK, 1)
    except (OSError, IOError):
        try:
            handle.close()
        except Exception:
            pass
        return None
    return AdvisoryLock(handle, fcntl_mod, msvcrt_mod)


def try_acquire(path, fcntl_mod=None, msvcrt_mod=None, opener=open) -> Optional[AdvisoryLock]:
    """Take an exclusive non-blocking advisory lock on ``path``.

    Returns the holder, or None if the lock is held elsewhere or the file
    cannot be opened. This collapses the two cases, which is fine for the tick
    lock -- both mean "do not tick" -- and wrong for a per-job dispatch gate,
    so ``JobLocks.claim`` calls the open and ``_lock_handle`` itself.
    """
    try:
        handle = opener(str(path), "w", encoding="utf-8")
    except (OSError, IOError):
        return None
    return _lock_handle(handle, fcntl_mod, msvcrt_mod)


class JobLocks:
    """Cross-process mirror of ``_running_job_ids``, one flock file per job.

    ``lock_dir_fn`` is called on every claim rather than cached: the tick
    lock's own directory is resolved per call for exactly the same reason
    (``_get_lock_paths`` -- 'so profile/env changes are honored').
    """

    def __init__(self, lock_dir_fn: Callable[[], Any], fcntl_mod=None,
                 msvcrt_mod=None, opener=open) -> None:
        self._lock_dir_fn = lock_dir_fn
        self._fcntl = fcntl_mod
        self._msvcrt = msvcrt_mod
        self._opener = opener
        self._held: Dict[str, AdvisoryLock] = {}
        self._mutex = threading.Lock()

    def _lock_path(self, job_id: Any) -> Optional[str]:
        """This job's lock file, or None if the store cannot be resolved.

        The PATH is the identity, not the job id. ``lock_dir_fn`` is per store,
        so one process ticking two profiles gets two different files for the
        same id -- gateway multiplex does that, and so would per-cluster
        profiles scaffolded from one template, which share every job id by
        construction. Keying ``_held`` on the id alone would have one profile's
        claim refuse another profile's job.

        The bare ``except`` is the same fail-open rule as ``claim``: this is a
        dispatch gate, and nothing it cannot answer may become a skip.
        """
        try:
            directory = self._lock_dir_fn()
            directory.mkdir(parents=True, exist_ok=True)
            return str(directory / (".job-" + sanitise_job_id(job_id) + ".lock"))
        except Exception:
            return None

    def claim(self, job_id: Any) -> bool:
        """True if this process may run ``job_id`` now.

        False means one thing and one thing only: a live process -- this one or
        another -- holds the flock. An unresolvable store, an unwritable lock
        directory and a lock file that will not open all return True. See the
        failure-polarity paragraph in the module docstring; a storage problem
        that answered False here would silence every job on the profile.
        """
        path = self._lock_path(job_id)
        if path is None:
            return True  # cannot resolve the store -> do not suppress the job
        with self._mutex:
            if path in self._held:
                return False
        try:
            handle = self._opener(path, "w", encoding="utf-8")
        except (OSError, IOError):
            return True  # cannot open the lock file -> do not suppress the job
        lock = _lock_handle(handle, self._fcntl, self._msvcrt)
        if lock is None:
            return False  # genuinely held by a live process
        with self._mutex:
            if path in self._held:  # raced another thread; keep the first
                lock.release()
                return False
            self._held[path] = lock
        return True

    def release(self, job_id: Any) -> None:
        path = self._lock_path(job_id)
        with self._mutex:
            if path is not None:
                lock = self._held.pop(path, None)
            else:
                # The store stopped resolving mid-tick. The filename is the
                # only identity left, and releasing on a best-effort match
                # beats leaking the claim until the process exits.
                suffix = "/.job-" + sanitise_job_id(job_id) + ".lock"
                key = next((k for k in self._held if k.endswith(suffix)), None)
                lock = self._held.pop(key, None) if key is not None else None
        if lock is not None:
            lock.release()

    def holds(self, job_id: Any) -> bool:
        path = self._lock_path(job_id)
        with self._mutex:
            return path is not None and path in self._held
