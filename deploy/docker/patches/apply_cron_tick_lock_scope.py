#!/usr/bin/env python3
"""Wire tools/cron_tick_lock_scope.py into the Hermes source tree.

Six anchored edits, all in ``cron/scheduler.py``. See the module docstring in
``deploy/docker/patches/cron_tick_lock_scope.py`` for the defect. Usage::

    python3 apply_cron_tick_lock_scope.py [HERMES_ROOT]   # default /opt/hermes

Must run AFTER apply_cron_run_scope.py: every anchor below was verified against
the post-cron_run_scope source in the running image, count == 1 for each.

Not idempotent, deliberately. A second run raises SystemExit because every
anchor has been consumed by the first -- that is the intended signal that the
build is applying the same surgery twice.
"""

import ast
import sys
from pathlib import Path

# --- 1. import + the per-job lock registry ----------------------------------
# `msvcrt` is only bound in the ImportError branch of the fcntl import, so on
# Unix the NAME does not exist. globals().get() rather than a bare reference.
IMPORT_ANCHOR = "def _get_lock_paths() -> tuple[Path, Path]:"
IMPORT_PATCHED = (
    "# kube-agents patch: see tools/cron_tick_lock_scope.py\n"
    "from tools.cron_tick_lock_scope import AdvisoryLock, JobLocks\n"
    "\n"
    "# Cross-process mirror of _running_job_ids. flock, not a ledger row: the\n"
    "# kernel releases the claim when a tick process dies, so it cannot wedge.\n"
    "_job_locks = JobLocks(\n"
    "    lambda: _get_lock_paths()[0], fcntl, globals().get(\"msvcrt\")\n"
    ")\n"
    "\n"
    "\n"
    "def _get_lock_paths() -> tuple[Path, Path]:"
)

# --- 2. own the tick lock's handle ------------------------------------------
ACQUIRE_ANCHOR = (
    "    except (OSError, IOError):\n"
    '        logger.debug("Tick skipped — another instance holds the lock")\n'
    "        if lock_fd is not None:\n"
    "            lock_fd.close()\n"
    "        return 0\n"
    "\n"
    "    try:\n"
)
ACQUIRE_PATCHED = (
    "    except (OSError, IOError):\n"
    '        logger.debug("Tick skipped — another instance holds the lock")\n'
    "        if lock_fd is not None:\n"
    "            lock_fd.close()\n"
    "        return 0\n"
    "\n"
    "    # kube-agents patch: the tick lock guards the scheduling decision, not\n"
    "    # job execution. See tools/cron_tick_lock_scope.py.\n"
    '    _tick_lock = AdvisoryLock(lock_fd, fcntl, globals().get("msvcrt"))\n'
    "\n"
    "    try:\n"
)

# --- 3. release once dispatch is done, before the sync wait -----------------
RELEASE_ANCHOR = (
    "        if sync:\n"
    "            # Sync mode (tests / manual ticks): wait for all dispatched jobs,\n"
)
RELEASE_PATCHED = (
    "        # kube-agents patch: every due job's next_run_at has been advanced\n"
    "        # and every job has been submitted by this point, so at-most-once is\n"
    "        # already secured. Releasing here instead of in the finally stops one\n"
    "        # slow job blocking the whole profile for its entire runtime.\n"
    "        # See tools/cron_tick_lock_scope.py.\n"
    "        _tick_lock.release()\n"
    "\n"
    "        if sync:\n"
    "            # Sync mode (tests / manual ticks): wait for all dispatched jobs,\n"
)

# --- 4. the finally becomes the idempotent backstop -------------------------
FINALLY_ANCHOR = (
    "    finally:\n"
    "        if fcntl:\n"
    "            try:\n"
    "                fcntl.flock(lock_fd, fcntl.LOCK_UN)\n"
    "            except (OSError, IOError):\n"
    "                pass\n"
    "        elif msvcrt:\n"
    "            try:\n"
    "                msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)\n"
    "            except (OSError, IOError):\n"
    "                pass\n"
    "        lock_fd.close()\n"
)
FINALLY_PATCHED = (
    "    finally:\n"
    "        # kube-agents patch: idempotent — a no-op when the dispatch path\n"
    "        # already released, and still the only release on the early-return\n"
    "        # and exception paths. See tools/cron_tick_lock_scope.py.\n"
    "        _tick_lock.release()\n"
)

# --- 5. claim the per-job lock beside the process-local guard ---------------
GUARD_ANCHOR = (
    "            with _running_lock:\n"
    "                if job_id in _running_job_ids:\n"
    "                    logger.info(\"Job '%s' already running — skipping\", job.get(\"name\", job_id))\n"
    "                    return None\n"
    "                _running_job_ids.add(job_id)\n"
    "            # Record the attempt before executor dispatch. Recovery classifies\n"
    "            # abandoned records as unknown; it never automatically retries them.\n"
    '            execution = create_execution(job_id, source="builtin")\n'
    '            dispatched_job = dict(job, execution_id=execution["id"])\n'
    "            _ctx = contextvars.copy_context()\n"
    "\n"
    "            def _run_and_release(j=dispatched_job, ctx=_ctx):\n"
    "                try:\n"
    "                    return ctx.run(_process_job, j)\n"
    "                finally:\n"
    "                    with _running_lock:\n"
    '                        _running_job_ids.discard(j["id"])\n'
)
GUARD_PATCHED = (
    "            with _running_lock:\n"
    "                if job_id in _running_job_ids:\n"
    "                    logger.info(\"Job '%s' already running — skipping\", job.get(\"name\", job_id))\n"
    "                    return None\n"
    "                _running_job_ids.add(job_id)\n"
    "            # kube-agents patch: _running_job_ids is module-level, so it is\n"
    "            # empty in every freshly spawned `hermes cron tick`. With the tick\n"
    "            # lock now released at dispatch, a second process can reach here\n"
    "            # for the same job if that job outlives its own period. Mirror the\n"
    "            # claim with a per-job flock the kernel releases on process death.\n"
    "            # See tools/cron_tick_lock_scope.py.\n"
    "            if not _job_locks.claim(job_id):\n"
    "                logger.info(\n"
    "                    \"Job '%s' already running in another process — skipping\",\n"
    '                    job.get("name", job_id),\n'
    "                )\n"
    "                with _running_lock:\n"
    "                    _running_job_ids.discard(job_id)\n"
    "                return None\n"
    "            # Record the attempt before executor dispatch. Recovery classifies\n"
    "            # abandoned records as unknown; it never automatically retries them.\n"
    '            execution = create_execution(job_id, source="builtin")\n'
    '            dispatched_job = dict(job, execution_id=execution["id"])\n'
    "            _ctx = contextvars.copy_context()\n"
    "\n"
    "            def _run_and_release(j=dispatched_job, ctx=_ctx):\n"
    "                try:\n"
    "                    return ctx.run(_process_job, j)\n"
    "                finally:\n"
    "                    with _running_lock:\n"
    '                        _running_job_ids.discard(j["id"])\n'
    '                    _job_locks.release(j["id"])\n'
)

# --- 6. release it on the dispatch-failure path too -------------------------
SUBMIT_ERR_ANCHOR = (
    "            except Exception as submit_err:\n"
    "                with _running_lock:\n"
    "                    _running_job_ids.discard(job_id)\n"
)
SUBMIT_ERR_PATCHED = (
    "            except Exception as submit_err:\n"
    "                with _running_lock:\n"
    "                    _running_job_ids.discard(job_id)\n"
    "                _job_locks.release(job_id)\n"
)

PATCHES = (
    (
        "cron/scheduler.py",
        (
            (IMPORT_ANCHOR, IMPORT_PATCHED, 1),
            (ACQUIRE_ANCHOR, ACQUIRE_PATCHED, 1),
            (RELEASE_ANCHOR, RELEASE_PATCHED, 1),
            (FINALLY_ANCHOR, FINALLY_PATCHED, 1),
            (GUARD_ANCHOR, GUARD_PATCHED, 1),
            (SUBMIT_ERR_ANCHOR, SUBMIT_ERR_PATCHED, 1),
        ),
    ),
)


def apply(root: Path) -> None:
    for relative, edits in PATCHES:
        path = root / relative
        if not path.is_file():
            raise SystemExit(f"cron_tick_lock_scope patch: {path} does not exist")
        source = path.read_text()
        for anchor, replacement, expected in edits:
            found = source.count(anchor)
            if found != expected:
                raise SystemExit(
                    f"cron_tick_lock_scope patch: {relative}: expected {expected} "
                    f"occurrence(s) of anchor, found {found}. Upstream Hermes "
                    f"changed — re-derive the anchor before bumping the base "
                    f"image.\n--- anchor ---\n{anchor}"
                )
            source = source.replace(anchor, replacement)
        try:
            ast.parse(source)
        except SyntaxError as e:
            raise SystemExit(
                f"cron_tick_lock_scope patch: {relative} no longer parses after "
                f"patching: {e}"
            )
        path.write_text(source)
        print(f"cron_tick_lock_scope patch: {relative} ({len(edits)} anchors)")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
