"""Fence task claims to the process life that made them.

Background — one identity, two lifetimes
----------------------------------------
``_claimer_id()`` builds the lock token that stamps every claimed card::

    return f"{host}:{os.getpid()}"

Under Kubernetes ``socket.gethostname()`` is the **pod name**, so the token is
pod-scoped. ``detect_crashed_workers`` then decides whose worker PIDs it is
entitled to adjudicate using only the host half::

    host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
    ...
    if not lock.startswith(host_prefix):
        continue

Upstream's docstring says "the whole design is single-host", and on a single
host that holds. In a container it does not: a container restart keeps the pod
name and resets the PID namespace. The new process therefore matches
``host_prefix`` against claims made by its *predecessor* and runs
``os.kill(pid, 0)`` against PIDs from a namespace that no longer exists.

What that cost us on 2026-08-07
-------------------------------
The gateway took a ``Fatal Python error: Bus error`` (exit 135) at 04:20:30 and
the container restarted 3 seconds later. At 04:20:51 the fresh dispatcher
adjudicated six in-flight cards claimed by the previous life — every one of them
carrying ``claimer: platform-agent-gateway-75b5f6ddf6-7dkd7:4``, the *old*
dispatcher PID — and marked all six ``crashed`` with ``pid <N> not alive``.

They did not merely retry. ``_error_fingerprint`` normalises the PID out of the
message::

    fp = re.sub(r'\\bpid \\d+\\b', 'pid N', error_text[:80])

so six independent workers killed by one infrastructure event collapsed to a
single fingerprint. Count 6 >= 3 tripped the systemic heuristic, which passes
``failure_limit=1``, so each card gave up on its *first* failure::

    gave_up {"failures": 1, "effective_limit": 1, "limit_source": "dispatcher",
             "error": "pid 1046 not alive", "trigger_outcome": "crashed"}

One gateway crash permanently abandoned every card in flight.

Separately, nothing releases a claim when a pod goes away. ``release_stale_claims``
selects purely on ``claim_expires < now``, and the TTL is
``DEFAULT_CLAIM_TTL_SECONDS = 900``. There is no SIGTERM drain and no startup
adoption, so an ordinary rollout costs a full 15 minutes of dark cards. Measured
on the same day: five cards claimed at 03:59:04, pod deleted at 04:00:11,
reclaimed at 04:14:24 — exactly one TTL after the last heartbeat.

The three fixes
---------------
1. **Adjudicate only your own claims.** ``claim_is_self`` compares the whole
   token, not the host half, so a previous life's PIDs are never probed. A
   colliding PID in a recycled namespace can no longer be mistaken for a live
   worker, and a dead one can no longer be reported as a crash.

2. **Release dead foreign claims immediately.** ``release_dead_foreign_claims``
   sweeps ``running`` cards whose claim belongs to someone else *and* whose
   recorded worker PID is not alive, and puts them back in ``todo`` for the
   dispatcher to pick up. This is what removes the 900-second dark window after
   a rollout or a crash.

   The liveness test is what makes this safe in every caller, not just the
   gateway. A CLI invocation has its own ``_claimer_id()`` and would otherwise
   consider the live gateway's claims foreign — but the gateway's workers have
   live PIDs, so they are left strictly alone. Where a PID does collide with an
   unrelated live process the card simply falls back to the existing TTL, which
   is the behaviour we already have today.

   Cards released this way are ``reclaimed``, not ``crashed``: an infrastructure
   event is not the card's fault and must not spend its retry budget.

3. **Keep the PID in the fingerprint.** ``pid 1044 not alive`` and
   ``pid 1045 not alive`` are two workers, not one systemic fault. Leaving the
   PID in place means the systemic heuristic no longer fires on them and each
   card keeps its normal ``failure_limit`` of 2. Strictly more forgiving than
   the current behaviour, and it only affects PID-bearing messages, which are
   per-worker by construction.
"""

from __future__ import annotations

import json
import time

RECLAIM_ERROR = "released: claim held by a process that is gone"

EVENT_KIND = "reclaimed"


def claim_is_self(lock: str | None, claimer_id: str) -> bool:
    """True when ``lock`` was written by this exact process life.

    Replaces upstream's host-prefix comparison, which cannot tell a container
    restart from the process that is currently running.
    """
    return bool(lock) and lock == claimer_id


def release_dead_foreign_claims(conn, claimer_id: str, pid_alive) -> list[str]:
    """Return ``running`` cards whose owner is gone to ``todo``.

    A card qualifies when all three hold:

    * its ``claim_lock`` is not this process life's token,
    * it has a recorded ``worker_pid``,
    * that PID is not alive.

    Must be called inside an open write transaction. Returns the released ids.
    """
    rows = conn.execute(
        "SELECT id, worker_pid, claim_lock, current_run_id FROM tasks "
        "WHERE status = 'running' AND claim_lock IS NOT NULL "
        "AND worker_pid IS NOT NULL"
    ).fetchall()

    released: list[str] = []
    now = int(time.time())
    for row in rows:
        lock = row["claim_lock"] or ""
        if claim_is_self(lock, claimer_id):
            continue
        try:
            pid = int(row["worker_pid"])
        except (TypeError, ValueError):
            continue
        if pid_alive(pid):
            # Either a real live worker or a PID collision. The TTL remains the
            # backstop; never take a card away from something that might be
            # running it.
            continue

        conn.execute(
            "UPDATE tasks SET status = 'todo', claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL, current_run_id = NULL "
            "WHERE id = ?",
            (row["id"],),
        )
        conn.execute(
            "UPDATE task_runs SET status = 'reclaimed', outcome = 'reclaimed', "
            "ended_at = ?, error = ? WHERE task_id = ? AND status = 'running'",
            (now, RECLAIM_ERROR, row["id"]),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                row["id"],
                row["current_run_id"],
                EVENT_KIND,
                json.dumps(
                    {
                        "stale_lock": lock,
                        "worker_pid": pid,
                        "claimer": claimer_id,
                        "reason": "owner_process_gone",
                        "fenced": True,
                    }
                ),
                now,
            ),
        )
        released.append(row["id"])
    return released
