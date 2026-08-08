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
name while every process that made those claims is gone. The new process
therefore matches ``host_prefix`` against claims made by its *predecessor* and
runs ``os.kill(pid, 0)`` against PIDs it never issued.

(The pod runs with ``shareProcessNamespace: true``, so the PID *namespace* is the
pod's and does outlive the container — which is why the replacement dispatcher
came up as PID 12329 rather than reusing 4. That makes a false *positive* on
``os.kill(pid, 0)`` less likely than it would be with a fresh namespace, but it
does not make the adjudication correct: the predecessor's workers are dead, and
attributing their deaths to the cards is exactly the bug below.)

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

The two fixes
-------------
1. **Adjudicate only your own claims.** ``claim_is_self`` compares the whole
   token, not the host half, so a previous life's PIDs are never probed. A
   colliding PID in a recycled namespace can no longer be mistaken for a live
   worker, and a dead one can no longer be reported as a crash.

2. **Release dead foreign claims immediately, and charge them.**
   ``release_dead_foreign_claims`` sweeps ``running`` cards whose claim belongs
   to someone else *and* whose recorded worker PID is not alive, and puts them
   back in ``ready`` for the dispatcher to pick up. This is what removes the
   900-second dark window after a rollout or a crash.
   ``charge_reclaimed_cards`` then spends one unit of each card's ordinary retry
   budget on the release.

   The liveness test is what makes the sweep safe in every caller, not just the
   gateway. A CLI invocation has its own ``_claimer_id()`` and would otherwise
   consider the live gateway's claims foreign — but the gateway's workers have
   live PIDs, so they are left strictly alone. Where a PID does collide with an
   unrelated live process the card simply falls back to the existing TTL, which
   is the behaviour we already have today.

   The sweep honours the same launch-window grace period the per-row loop below
   it applies, for the same reason and from the same source
   (``_resolve_crash_grace_seconds``, injected). Running ahead of that check
   would give foreign claims a stricter liveness test than the process's own —
   a divergence with no justification behind it, since a card claimed seconds
   ago by any process is a card whose worker may not be on ``/proc`` yet.

Why the release is charged, and why it goes back to ``ready``
-------------------------------------------------------------
The first version of this module released the card for free — ``todo``, no
failure recorded — on the reasoning that an infrastructure event is not the
card's fault and must not spend its retry budget. That reasoning conflated two
different questions. Whose claim it was says nothing about whether the *run*
failed, and the sweep's own precondition is that the worker process is provably
dead with no result written. That is a failed run by the same definition
``detect_crashed_workers`` uses one loop later for a worker of our own.

Left uncharged it is also an unbounded loop, which is the concrete failure this
paragraph exists for. A card whose work kills the dispatcher — an OOM, the
2026-08-07 SIGBUS — is reclaimed by the replacement process, dispatched again,
kills it again. Nothing counts, so nothing ever stops it: replayed against a
real board through the shipped engine, six cycles of claim → reclaim → promote
left ``consecutive_failures`` at 0 every single time. The card burns a worker
slot and takes the gateway down with it for as long as the pod keeps coming
back.

``todo`` was half of why the breaker could not see it. ``recompute_ready``
applies its failure-limit guard (#35072) only to ``blocked`` rows; a ``todo``
row is promoted unconditionally, so even a card carrying an exhausted counter
walks straight back to ``ready``. ``ready`` is also what upstream's own crash
path releases to, and what ``_record_task_failure(release_claim=False)``
requires — its trip branch is gated on ``status IN ('ready', 'running')``.

Charging goes through ``_record_task_failure`` with no ``failure_limit``
argument, so the threshold is the one the dispatcher already resolves for every
other failure: per-task ``max_retries``, else ``kanban.failure_limit``, else
``DEFAULT_FAILURE_LIMIT``. No second budget, no new column. A poison card lands
in ``blocked`` with a ``gave_up`` event after the second reclaim and waits for a
human, exactly as a card whose worker crashes twice does. The price is that a
rollout mid-flight costs each in-flight card one retry, and two rollouts across
one card's life park it; ``hermes kanban unblock`` is the way out, and a card
that has survived two full rollouts without ever finishing is worth a look
anyway.

Why the PID stays in the fingerprint after all
----------------------------------------------
This patch used to make a third edit: it replaced upstream's
``re.sub(r'\\bpid \\d+\\b', 'pid N', ...)`` in ``_error_fingerprint`` with a bare
``error_text[:80]``, so that ``pid 1044 not alive`` and ``pid 1045 not alive``
would count as two failures rather than one systemic fault. That edit has been
reverted, because ``_error_fingerprint`` is reachable from exactly two call
sites — both inside ``detect_crashed_workers``, both over the ``crash_details``
error texts — and every message that path can produce is PID-prefixed::

    f"pid {pid} exited with code {code}"
    f"pid {pid} killed by signal {code}"
    f"pid {pid} not alive"

With the PID left in, no two concurrent workers can ever share a fingerprint, so
``_fp_counts`` never reaches the ``>= 3`` the systemic heuristic tests and
``is_systemic`` is dead code. Driving the shipped engine with four of its own
workers all OOM-killed in one tick (``exited with code 137``, distinct PIDs)
produced four fingerprints and an empty ``auto_blocked``: the detector that is
supposed to stop a board where everything is failing the same way could not
fire at all.

The edit was aimed at a cause that fix 1 had already removed. The six
fingerprints of 2026-08-07 existed only because the successor adjudicated its
predecessor's workers; with ``claim_is_self`` in place those cards never enter
``crash_details``, they go through the sweep instead. What is left in that
bucket after the fence is a burst of *our own* workers dying the same way in a
single tick, which is what the heuristic was built for. Reclaims are charged in
their own loop and never join the fingerprint pass, so a rollout that hands back
six cards at once still cannot collapse into one systemic verdict — the property
this module was written to protect.
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


def release_dead_foreign_claims(
    conn, claimer_id: str, pid_alive, grace_seconds=None
) -> list[str]:
    """Return ``running`` cards whose owner is gone to ``ready``.

    A card qualifies when all four hold:

    * its ``claim_lock`` is not this process life's token,
    * it has a recorded ``worker_pid``,
    * it is past the launch-window grace period,
    * that PID is not alive.

    ``grace_seconds`` is ``_resolve_crash_grace_seconds`` — injected rather than
    imported so this module never depends on ``kanban_db``. Omitting it skips
    the grace check, which is what the tests want and what a caller with no
    ``started_at`` column can live with.

    Must be called inside an open write transaction. Returns the released ids,
    which the caller owes to ``charge_reclaimed_cards`` once that transaction
    has closed.
    """
    rows = conn.execute(
        "SELECT id, worker_pid, claim_lock, current_run_id, started_at FROM tasks "
        "WHERE status = 'running' AND claim_lock IS NOT NULL "
        "AND worker_pid IS NOT NULL"
    ).fetchall()

    released: list[str] = []
    now = int(time.time())
    grace = None
    for row in rows:
        lock = row["claim_lock"] or ""
        if claim_is_self(lock, claimer_id):
            continue
        try:
            pid = int(row["worker_pid"])
        except (TypeError, ValueError):
            continue
        if grace_seconds is not None:
            started_at = row["started_at"]
            if started_at is not None:
                if grace is None:
                    grace = grace_seconds()
                if now - started_at < grace:
                    # Too new to adjudicate: the worker may not be on /proc yet.
                    # Same test, same source as the per-row loop in
                    # ``detect_crashed_workers``.
                    continue
        if pid_alive(pid):
            # Either a real live worker or a PID collision. The TTL remains the
            # backstop; never take a card away from something that might be
            # running it.
            continue

        # ``ready``, not ``todo``: ``recompute_ready`` promotes a ``todo`` row
        # without consulting its failure counter, which is how a card that kills
        # the dispatcher used to escape the breaker forever. It is also the
        # status ``_record_task_failure`` needs to see to trip.
        conn.execute(
            "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL, current_run_id = NULL "
            "WHERE id = ?",
            (row["id"],),
        )
        # ``ended_at IS NULL`` mirrors ``_end_run``: a run already closed by some
        # other path must not have its outcome rewritten to ``reclaimed``.
        conn.execute(
            "UPDATE task_runs SET status = 'reclaimed', outcome = 'reclaimed', "
            "ended_at = ?, error = ? WHERE task_id = ? AND status = 'running' "
            "AND ended_at IS NULL",
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


def charge_reclaimed_cards(conn, task_ids, record_failure) -> list[str]:
    """Spend one retry on each card the sweep handed back. Returns those parked.

    ``record_failure`` is ``_record_task_failure``, injected for the same reason
    ``pid_alive`` is. It is called the way the crash path calls it — the card is
    already back at ``ready`` with its run closed, so no claim to release and no
    run to end — and with no ``failure_limit``, so the threshold is the one the
    dispatcher resolves for every other failure kind rather than a second budget
    invented here.

    Must be called OUTSIDE the transaction the sweep ran in:
    ``_record_task_failure`` opens its own ``write_txn`` and ``write_txn`` does
    not nest.

    Deliberately a separate loop from the ``crash_details`` pass rather than an
    extra entry in it. Every card released by one rollout carries the same
    ``RECLAIM_ERROR`` text, so folding them into ``_fp_counts`` would make any
    three of them look systemic, drop ``failure_limit`` to 1 and abandon the lot
    on their first reclaim — the 2026-08-07 outcome, reached by a new route.
    """
    tripped: list[str] = []
    for task_id in task_ids:
        if record_failure(
            conn,
            task_id,
            error=RECLAIM_ERROR,
            outcome=EVENT_KIND,
            release_claim=False,
            end_run=False,
            event_payload_extra={"reason": "owner_process_gone", "fenced": True},
        ):
            tripped.append(task_id)
    return tripped
