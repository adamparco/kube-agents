#!/usr/bin/env python3
"""Build gate for the cron tick-lock-scope patch.

Run by deploy/docker/Dockerfile from /opt/hermes after
apply_cron_tick_lock_scope.py. The applier only proves six anchors matched; it
proves nothing about WHERE the release landed, and a release placed one
statement too early would silently break at-most-once.

Three checks, each a way the patch could match its anchors and still be wrong
or harmful:

1. STATIC ORDERING. Parse the patched ``cron/scheduler.py::tick`` and assert the
   ``_tick_lock.release()`` statement sits inside the ``try``, strictly AFTER
   the ``advance_next_runs(...)`` call and after the last ``_submit_with_guard``
   call, and strictly BEFORE the ``if sync:`` node. Releasing before the advance
   or before dispatch would reintroduce double-firing. The ``finally`` is also
   asserted to be the idempotent backstop and nothing else -- if it still
   touches ``lock_fd`` directly, the old wide-scope unlock survived alongside
   the new one.

2. HEAD-OF-LINE IS GONE. Against a throwaway HERMES_HOME with one ``no_agent``
   job whose script sleeps, run ``tick(sync=True)`` in a child process; once the
   ledger shows the job running, assert ``.tick.lock`` can be flock'd from the
   parent. On today's image that flock fails; on the patched image it succeeds.
   This is the defect, stated as an assertion.

3. THE DOUBLE-FIRE GUARD IS ARMED. In the same window, assert
   ``.job-<id>.lock`` can NOT be flock'd -- and that after the child exits it
   CAN be, proving the kernel released it rather than a janitor having to.
   Check 3 exists because the guard is the thing the narrowing trades away;
   shipping it untested by the build is how the fix becomes worse than the bug.

Checks 2 and 3 are real cross-process lock exercises, not AST assertions: a
separate interpreter holds the locks, and this process is the second claimant.

Usage::

    cd /opt/hermes && python3 verify_cron_tick_lock_scope.py
"""

from __future__ import annotations

import ast
import fcntl
import json
import os
import subprocess
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
if str(HERMES) not in sys.path:
    sys.path.insert(0, str(HERMES))

FAILURES: list[str] = []


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


# --- 1. static ordering ------------------------------------------------------
def check_release_placement() -> None:
    print("release placement (cron/scheduler.py::tick):")
    tree = ast.parse((HERMES / "cron" / "scheduler.py").read_text())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "tick"), None)
    check("tick() is still a module-level function", fn is not None)
    if fn is None:
        return

    # tick() has TWO top-level Try nodes: the acquire's try/except, and the
    # try/finally that wraps the whole body. Only the second one is ours.
    tries = [n for n in fn.body if isinstance(n, ast.Try) and n.finalbody]
    check("tick() still has exactly one try/finally", len(tries) == 1,
          f"found {len(tries)}")
    if len(tries) != 1:
        return
    outer = tries[0]
    body = outer.body

    def linenos(pred):
        return [n.lineno for n in ast.walk(outer) if pred(n)]

    releases = [n.lineno for n in body
                if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                and getattr(n.value.func, "attr", None) == "release"
                and getattr(getattr(n.value.func, "value", None), "id", None) == "_tick_lock"]
    advances = linenos(lambda n: isinstance(n, ast.Call)
                       and getattr(n.func, "id", None) == "advance_next_runs")
    submits = linenos(lambda n: isinstance(n, ast.Call)
                      and getattr(n.func, "id", None) == "_submit_with_guard")
    syncs = [n.lineno for n in body
             if isinstance(n, ast.If) and getattr(n.test, "id", None) == "sync"]

    check("exactly one _tick_lock.release() in the try body", len(releases) == 1,
          f"found {len(releases)}")
    check("advance_next_runs still called under the lock", len(advances) == 1,
          f"found {len(advances)}")
    check("_submit_with_guard still called under the lock", len(submits) >= 1,
          f"found {len(submits)}")
    check("if sync: still in the try body", len(syncs) == 1, f"found {len(syncs)}")
    if releases and advances and submits and syncs:
        check("release is after advance_next_runs", releases[0] > advances[0],
              f"release at {releases[0]}, advance at {advances[0]}")
        check("release is after every dispatch", releases[0] > max(submits),
              f"release at {releases[0]}, last submit at {max(submits)}")
        check("release is before the sync wait", releases[0] < syncs[0],
              f"release at {releases[0]}, if sync: at {syncs[0]}")

    finally_src = "\n".join(ast.unparse(s) for s in outer.finalbody)
    check("finally still releases as a backstop",
          "_tick_lock.release()" in finally_src, f"finally body: {finally_src!r}")
    check("the old wide-scope unlock is gone from the finally",
          "lock_fd" not in finally_src,
          "the finally still unlocks lock_fd directly, so the narrowing did "
          f"not take: {finally_src!r}")

    # The registry the guard depends on has to exist at module scope, and it
    # must not reference a bare `msvcrt` -- that name is only bound inside the
    # ImportError branch of the fcntl import, so on Unix it does not exist.
    module_src = "\n".join(
        ast.unparse(n) for n in tree.body if isinstance(n, ast.Assign)
        and any(getattr(t, "id", None) == "_job_locks" for t in n.targets)
    )
    check("_job_locks is created at module scope", "JobLocks(" in module_src,
          f"found {module_src!r}")
    check("_job_locks does not reference a bare msvcrt",
          "globals().get('msvcrt')" in module_src
          or 'globals().get("msvcrt")' in module_src,
          f"found {module_src!r}")


# --- 2 + 3. behavioural ------------------------------------------------------
SLOW_SCRIPT = "import time\ntime.sleep(12)\nprint('slow job done')\n"

CHILD = (
    "import sys\n"
    "sys.path.insert(0, {root!r})\n"
    "from cron.scheduler import tick\n"
    "tick(verbose=False)\n"
)


def flockable(path: Path) -> bool:
    """Whether an exclusive non-blocking flock on ``path`` succeeds right now."""
    try:
        fh = open(path, "w", encoding="utf-8")
    except OSError:
        return False
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    fcntl.flock(fh, fcntl.LOCK_UN)
    fh.close()
    return True


def flockable_within(path: Path, seconds: float = 3.0) -> bool:
    """``flockable``, retried — for the assertion that must eventually be true.

    ``create_execution`` runs inside ``_submit_with_guard``, several statements
    BEFORE ``_tick_lock.release()``. So the in-flight ledger row that wakes the
    poller below legitimately exists for a short window during which the tick
    lock is still held. A single shot that landed in that window would fail the
    docker build with "one slow job still starves the whole profile" — a scary
    and false diagnosis of a correctly patched image. Only this direction
    needs the retry; "the per-job lock IS held" must be true the instant the
    row appears, so that one stays a single shot.
    """
    deadline = time.monotonic() + seconds
    while True:
        if flockable(path):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def check_behaviour() -> None:
    print("lock behaviour (a real second process holds the locks):")
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        (home / "cron").mkdir(parents=True)
        (home / "scripts").mkdir(parents=True)
        (home / "scripts" / "slow_job.py").write_text(SLOW_SCRIPT)
        (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{
            "id": "slow-job", "name": "Slow Job",
            "schedule": {"kind": "interval", "minutes": 1, "display": "every 1m"},
            "prompt": "", "no_agent": True, "script": "slow_job.py", "skills": [],
            "enabled": True, "deliver": "local",
            "next_run_at": "2000-01-01T00:00:00+00:00", "state": "scheduled",
        }]}))

        env = dict(os.environ, HERMES_HOME=str(home))
        # The child's output is kept, not discarded: if the throwaway
        # HERMES_HOME fails to dispatch, this log is the only account of why,
        # and an opaque build failure here is expensive to diagnose.
        log = home / "child.log"
        child_log = open(log, "w", encoding="utf-8")
        child = subprocess.Popen(
            [sys.executable, "-c", CHILD.format(root=str(HERMES))], env=env,
            stdout=child_log, stderr=subprocess.STDOUT)
        try:
            ledger = home / "cron" / "executions.db"
            deadline = time.monotonic() + 20
            running = False
            while time.monotonic() < deadline and not running:
                if ledger.is_file():
                    try:
                        con = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
                        running = bool(list(con.execute(
                            "SELECT 1 FROM executions WHERE job_id='slow-job' "
                            "AND finished_at IS NULL")))
                        con.close()
                    except sqlite3.Error:
                        pass
                if not running:
                    if child.poll() is not None:
                        break  # the tick is over; no in-flight window to test
                    time.sleep(0.2)
            check("slow job reached the ledger as in-flight", running,
                  "the throwaway HERMES_HOME never dispatched — the harness "
                  "is broken, not the patch")
            if not running:
                child.wait(timeout=60)
                child_log.close()
                print("--- child output ---")
                print(log.read_text()[-4000:] or "(empty)")
                print("--- end child output ---")
                return

            check("tick lock is free while a job runs (the defect)",
                  flockable_within(home / "cron" / ".tick.lock"),
                  "tick() is still holding .tick.lock across the as_completed "
                  "wait, so one slow job still starves the whole profile")
            check("per-job lock is held while the job runs (the guard)",
                  not flockable(home / "cron" / ".job-slow-job.lock"),
                  "nothing replaced the cross-process in-flight guard the wide "
                  "lock used to provide")
        finally:
            child.wait(timeout=60)
            child_log.close()

        check("per-job lock is released by the kernel on process exit",
              flockable(home / "cron" / ".job-slow-job.lock"),
              "the claim outlived its holder — this is the wedging failure "
              "mode flock was chosen to avoid")


if __name__ == "__main__":
    print("verify_cron_tick_lock_scope")
    check_release_placement()
    check_behaviour()
    print()
    if FAILURES:
        print(f"verify_cron_tick_lock_scope: {len(FAILURES)} FAILED")
        for f in FAILURES:
            print(f"  - {f}")
        raise SystemExit(1)
    print("verify_cron_tick_lock_scope: all checks passed")
