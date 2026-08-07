#!/usr/bin/env python3
"""Build gate for the kanban breaker-counter patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_kanban_breaker_counter.py``. The applier only proves three anchors
matched exactly once. That says nothing about whether the trip now survives the
tick, and -- more importantly -- nothing about whether the recovery paths still
release a blocked card. The rejected event-stickiness version of this fix passed
every anchor check and silently pinned reassigned cards in ``blocked`` forever,
so the recovery cases below are the point of this script, not decoration.

Every case is driven against the real patched ``hermes_cli.kanban_db`` on a real
(in-memory) board through the real ``_record_task_failure`` and
``recompute_ready``, in the same order ``dispatch_once`` calls them. Nine
scenarios, the same nine that were executed against the live image before this
patch was written:

  1. protocol ``force_trip`` (limit 3)          FIXED    -- was reverted in-tick
  2. systemic ``failure_limit=1``               FIXED    -- was reverted in-tick
  3. an ordinary two-failure trip               UNCHANGED - no inflation
  4. ``assign_task`` recovery                   PRESERVED - the rejected fix broke this
  4b. ``assign_task`` to the SAME profile       CHANGED   -- no longer recovers
  5. ``unblock_task`` recovery                  PRESERVED
  6. parent-gated card, never tripped           PRESERVED
  7. ``max_retries=5`` override                 FIXED    -- honours the override
  8. worker ``kanban_block`` stickiness         UNCHANGED
  9. spawn path (``release_claim``) at limit 1  FIXED    -- the second patched bind

4, 5, 6 and 8 are preservation tests. They are not decoration either: they are
exactly the cases the rejected event-reader fix broke, and they are the reason
this fix keeps ``consecutive_failures`` as the single arbiter. 4b is the one
case this patch deliberately DOES change, pinned here so it stays a decision
rather than becoming a surprise.

Usage::

    cd /opt/hermes && python3 verify_kanban_breaker_counter.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
if str(HERMES) not in sys.path:
    sys.path.insert(0, str(HERMES))

from hermes_cli import kanban_db as kb  # noqa: E402

FAILURES: list[str] = []

# What ``dispatch_once`` passes to ``recompute_ready`` under the shipped
# configuration (``kanban.failure_limit`` unset on every profile).
DISPATCHER_LIMIT = kb.DEFAULT_SPAWN_FAILURE_LIMIT
PROTOCOL_LIMIT = kb._PROTOCOL_VIOLATION_FAILURE_LIMIT
# The constant the patch itself floors against. It is a distinct name from
# ``DEFAULT_SPAWN_FAILURE_LIMIT`` above, currently aliased to it in kanban_db.py
# (``DEFAULT_SPAWN_FAILURE_LIMIT = DEFAULT_FAILURE_LIMIT``). Referencing both
# separately means the day they diverge this gate fails rather than passing on a
# coincidence.
FLOOR = kb.DEFAULT_FAILURE_LIMIT


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


def board() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(kb.SCHEMA_SQL)
    return conn


def make(conn, tid, *, status="ready", max_retries=None):
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, "
        "consecutive_failures, max_retries) VALUES (?, ?, ?, ?, 0, ?)",
        (tid, tid, status, int(time.time()), max_retries),
    )
    conn.commit()


def claim(conn, tid):
    """Put ``tid`` into the state ``claim_task`` leaves behind.

    Open-coded rather than calling ``kb.claim_task`` on purpose: that helper
    resolves the current board off the filesystem (``get_current_board``),
    which drags a build-time dependency on ``$HERMES_HOME`` layout into a test
    whose entire subject is two SQL binds. The columns below are exactly what
    its UPDATE plus ``_start_run`` write.
    """
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO task_runs (task_id, status, claim_lock, worker_pid, "
        "started_at) VALUES (?, 'running', ?, ?, ?)",
        (tid, f"verify-{tid}", 4242, now),
    )
    conn.execute(
        "UPDATE tasks SET status = 'running', claim_lock = ?, "
        "claim_expires = ?, worker_pid = ?, started_at = ?, "
        "current_run_id = ? WHERE id = ?",
        (f"verify-{tid}", now + 900, 4242, now, cur.lastrowid, tid),
    )
    conn.commit()


def state(conn, tid):
    row = conn.execute(
        "SELECT status, consecutive_failures FROM tasks WHERE id = ?", (tid,)
    ).fetchone()
    return row["status"], int(row["consecutive_failures"])


def gave_up_payload(conn, tid):
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'gave_up' "
        "ORDER BY id DESC LIMIT 1", (tid,)
    ).fetchone()
    return json.loads(row["payload"]) if row and row["payload"] else {}


def stored_from_payload(payload):
    """Reconstruct ``consecutive_failures`` from the untouched ``gave_up`` row.

    Two arms, because the floor the patch applies is INVISIBLE in the payload:
    it only fires when there is no per-task override, and the payload records
    that as ``limit_source``, not as a number. ``max(failures,
    effective_limit)`` alone is right for the ``task`` arm and wrong for the
    systemic one, which stores 2 while its payload reads
    ``{"failures": 1, "effective_limit": 1}``. The audit trail is the only
    account of why a card is blocked, so a formula that is right for three of
    four arms is not good enough to document.
    """
    stored = max(payload.get("failures", 0), payload.get("effective_limit", 0))
    if payload.get("limit_source") != "task":
        stored = max(stored, FLOOR)
    return stored


print(f"breaker-counter verify (dispatcher limit {DISPATCHER_LIMIT}):")

# --- 1, 2, 7, 9: the arms that used to be reverted in the same tick ---------
conn = board()

# 1. Protocol violation. detect_crashed_workers adjudicates its own streak and
#    passes force_trip=True, so the raw counter lands at 1 -- below the
#    dispatcher's 2, which is what let recompute_ready undo the trip.
make(conn, "proto")
kb._record_task_failure(
    conn,
    "proto",
    error="clean exit without a terminal kanban call",
    outcome="crashed",
    failure_limit=PROTOCOL_LIMIT,
    force_trip=True,
)
check(
    "1. protocol force_trip stores the threshold it was decided against",
    state(conn, "proto") == ("blocked", PROTOCOL_LIMIT),
    f"got {state(conn, 'proto')}, want ('blocked', {PROTOCOL_LIMIT})",
)
promoted = kb.recompute_ready(conn, failure_limit=DISPATCHER_LIMIT)
check(
    "1. protocol trip survives recompute_ready",
    state(conn, "proto")[0] == "blocked" and promoted == 0,
    f"state {state(conn, 'proto')}, promoted {promoted}",
)

# 2. Systemic same-error crash. The caller lowers the limit to 1.
make(conn, "systemic")
kb._record_task_failure(
    conn, "systemic", error="container OOMKilled", outcome="crashed",
    failure_limit=1,
)
check(
    "2. systemic trip is floored at the dispatcher default",
    state(conn, "systemic") == ("blocked", DISPATCHER_LIMIT),
    f"got {state(conn, 'systemic')}, want ('blocked', {DISPATCHER_LIMIT})",
)
promoted = kb.recompute_ready(conn, failure_limit=DISPATCHER_LIMIT)
check(
    "2. systemic trip survives recompute_ready",
    state(conn, "systemic")[0] == "blocked" and promoted == 0,
    f"state {state(conn, 'systemic')}, promoted {promoted}",
)

# 7. A per-task max_retries override must be honoured, not flattened to the
#    module default -- recompute_ready resolves against max_retries first.
make(conn, "override", max_retries=5)
kb._record_task_failure(
    conn, "override", error="clean exit without a terminal kanban call",
    outcome="crashed", failure_limit=5, force_trip=True,
)
check(
    "7. max_retries override is stored, not the default floor",
    state(conn, "override") == ("blocked", 5),
    f"got {state(conn, 'override')}, want ('blocked', 5)",
)
promoted = kb.recompute_ready(conn, failure_limit=DISPATCHER_LIMIT)
check(
    "7. override trip survives recompute_ready",
    state(conn, "override")[0] == "blocked" and promoted == 0,
    f"state {state(conn, 'override')}, promoted {promoted}",
)

# 9. The spawn-failure path takes the OTHER patched UPDATE (release_claim=True,
#    the one whose WHERE clause is ('running', 'ready')). Without its own case
#    that bind could be left unpatched and everything above would still pass.
make(conn, "spawn")
claim(conn, "spawn")
kb._record_task_failure(
    conn, "spawn", error="failed to spawn worker", outcome="spawn_failed",
    failure_limit=1, release_claim=True, end_run=True,
)
check(
    "9. spawn-path trip is floored too",
    state(conn, "spawn") == ("blocked", DISPATCHER_LIMIT),
    f"got {state(conn, 'spawn')}, want ('blocked', {DISPATCHER_LIMIT})",
)
spawn_row = conn.execute(
    "SELECT claim_lock, worker_pid, current_run_id FROM tasks WHERE id = 'spawn'"
).fetchone()
check(
    "9. the spawn-path claim is still released and the run closed",
    spawn_row["claim_lock"] is None
    and spawn_row["worker_pid"] is None
    and spawn_row["current_run_id"] is None,
    f"claim_lock={spawn_row['claim_lock']!r} pid={spawn_row['worker_pid']!r} "
    f"run={spawn_row['current_run_id']!r}",
)
promoted = kb.recompute_ready(conn, failure_limit=DISPATCHER_LIMIT)
check(
    "9. spawn-path trip survives recompute_ready",
    state(conn, "spawn")[0] == "blocked" and promoted == 0,
    f"state {state(conn, 'spawn')}, promoted {promoted}",
)

# --- 3: no inflation on the ordinary path -----------------------------------
make(conn, "normal")
kb._record_task_failure(
    conn, "normal", error="boom 1", outcome="crashed",
    failure_limit=DISPATCHER_LIMIT,
)
check(
    "3. the first ordinary failure neither blocks nor inflates",
    state(conn, "normal") == ("ready", 1),
    f"got {state(conn, 'normal')}, want ('ready', 1)",
)
kb._record_task_failure(
    conn, "normal", error="boom 2", outcome="crashed",
    failure_limit=DISPATCHER_LIMIT,
)
check(
    "3. the ordinary trip stores the true count",
    state(conn, "normal") == ("blocked", DISPATCHER_LIMIT),
    f"got {state(conn, 'normal')}, want ('blocked', {DISPATCHER_LIMIT})",
)

# --- the audit trail must not change meaning --------------------------------
payload = gave_up_payload(conn, "proto")
check(
    "the gave_up payload still reports the true attempt count",
    payload.get("failures") == 1,
    f"payload: {payload}",
)
check(
    "the gave_up payload still reports the deciding threshold",
    payload.get("effective_limit") == PROTOCOL_LIMIT,
    f"payload: {payload}",
)

# Every arm, not just the one where the floor happens not to bite. Asserting
# recoverability on ``proto`` alone is vacuous: its effective_limit (3) already
# exceeds the floor, so the two formulas agree there and the systemic arm --
# the one that motivated this patch -- goes unchecked.
for tid in ("proto", "systemic", "override", "spawn"):
    payload = gave_up_payload(conn, tid)
    stored = state(conn, tid)[1]
    check(
        f"the stored counter is recoverable from {tid}'s untouched payload",
        stored_from_payload(payload) == stored,
        f"payload {payload} reconstructs to {stored_from_payload(payload)}, "
        f"column holds {stored}",
    )
check(
    "the systemic arm is the one that needs the two-arm formula",
    max(gave_up_payload(conn, "systemic").get("failures", 0),
        gave_up_payload(conn, "systemic").get("effective_limit", 0))
    != state(conn, "systemic")[1],
    "the naive one-line formula now agrees on the systemic arm, so the "
    "docstring's two-arm caveat has gone stale — re-derive it before trusting "
    "the simpler form",
)
conn.close()

# --- 4, 5, 6, 8: preservation. This is what the rejected fix broke. ---------
conn = board()

# 4. assign_task zeroes the counter on a profile change and calls that "an
#    explicit recovery action". It emits kind 'assigned', not 'unblocked', and
#    does not change status -- so any fix that reads the gave_up EVENT instead
#    of the counter pins this card in blocked forever.
make(conn, "reassigned")
kb._record_task_failure(
    conn, "reassigned", error="pv", outcome="crashed",
    failure_limit=PROTOCOL_LIMIT, force_trip=True,
)
check(
    "4. the reassigned card starts out tripped",
    state(conn, "reassigned")[0] == "blocked",
)
kb.assign_task(conn, "reassigned", "platform")
check(
    "4. assign_task still zeroes the counter",
    state(conn, "reassigned")[1] == 0,
    f"state {state(conn, 'reassigned')}",
)
promoted = kb.recompute_ready(conn, failure_limit=DISPATCHER_LIMIT)
check(
    "4. assign_task still recovers a force-tripped card",
    state(conn, "reassigned") == ("ready", 0) and promoted == 1,
    f"state {state(conn, 'reassigned')}, promoted {promoted}",
)

# 4b. The behaviour change operators have to know about, asserted rather than
#     only written down. assign_task only zeroes the counter when the assignee
#     actually CHANGES; to the same profile it just rewrites the column. That
#     was survivable before this patch because a systemic trip stored 1, below
#     the dispatcher's 2, so recompute_ready promoted the card whatever
#     assign_task did. Now the stored count is the exhausted budget and the
#     no-op assign no longer frees it. Reassign elsewhere, or unblock.
kb._record_task_failure(
    conn, "reassigned", error="container OOMKilled", outcome="crashed",
    failure_limit=1,
)
check(
    "4b. the card is tripped again before the same-profile assign",
    state(conn, "reassigned") == ("blocked", DISPATCHER_LIMIT),
    f"got {state(conn, 'reassigned')}, want ('blocked', {DISPATCHER_LIMIT})",
)
kb.assign_task(conn, "reassigned", "platform")  # same profile: a no-op reassign
promoted = kb.recompute_ready(conn, failure_limit=DISPATCHER_LIMIT)
check(
    "4b. assign_task to the SAME profile no longer frees a tripped card",
    state(conn, "reassigned") == ("blocked", DISPATCHER_LIMIT) and promoted == 0,
    f"state {state(conn, 'reassigned')}, promoted {promoted} — if this now "
    "recovers, the counter stopped being the arbiter and the docstring's "
    "recovery-path list needs redoing",
)

# 5. unblock_task is the operator's exit.
make(conn, "unblocked")
kb._record_task_failure(
    conn, "unblocked", error="pv", outcome="crashed",
    failure_limit=PROTOCOL_LIMIT, force_trip=True,
)
kb.unblock_task(conn, "unblocked")
check(
    "5. unblock_task still recovers a force-tripped card",
    state(conn, "unblocked") == ("ready", 0),
    f"got {state(conn, 'unblocked')}, want ('ready', 0)",
)

# 6. A card gated only by a parent never trips, so it must still auto-promote.
make(conn, "parent", status="done")
make(conn, "child", status="todo")
conn.execute(
    "INSERT INTO task_links (parent_id, child_id) VALUES ('parent', 'child')"
)
conn.commit()
kb.recompute_ready(conn, failure_limit=DISPATCHER_LIMIT)
check(
    "6. a parent-gated card with no trip still auto-promotes",
    state(conn, "child") == ("ready", 0),
    f"got {state(conn, 'child')}, want ('ready', 0)",
)

# 8. A worker/operator kanban_block stays sticky, as before. Note the counter
#    is 0 here, so only _has_sticky_block can hold this card.
make(conn, "sticky", status="blocked")
with kb.write_txn(conn):
    kb._append_event(conn, "sticky", "blocked", {"reason": "review-required"})
promoted = kb.recompute_ready(conn, failure_limit=DISPATCHER_LIMIT)
check(
    "8. a worker kanban_block is still sticky",
    state(conn, "sticky")[0] == "blocked",
    f"state {state(conn, 'sticky')}, promoted {promoted}",
)

conn.close()

print()
if FAILURES:
    print(f"verify_kanban_breaker_counter: {len(FAILURES)} FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("verify_kanban_breaker_counter: all checks passed")
