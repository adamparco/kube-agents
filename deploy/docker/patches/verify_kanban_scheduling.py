#!/usr/bin/env python3
"""Build gate for the kanban dependency-repair and claim-fencing patches.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after both appliers.
The appliers only prove their anchors matched. This drives the *patched* engine
against a real kanban database — real schema, real ``create_task`` /
``claim_task`` / ``block_task`` / ``detect_crashed_workers`` — because both bugs
were emergent scheduling behaviour, not a bad string.

The two scenarios are the ones observed on the cluster on 2026-08-07:

  A. A card creates work with ``parents=[<itself>]`` and then blocks on it.
     Before the patch the children were permanently unclaimable and the parent
     respawned every few seconds; after it, the children dispatch and the parent
     waits on them for real.
  B. The gateway restarts. The replacement process must not adjudicate its
     predecessor's worker PIDs, and must hand those cards straight back instead
     of leaving them dark for the 900-second claim TTL.

Usage::

    cd /opt/hermes && python3 verify_kanban_scheduling.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

FAILURES: list[str] = []


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


from hermes_cli import kanban_db as K  # noqa: E402

TMP = Path(tempfile.mkdtemp())


def fresh():
    """A real board, created through the engine's own schema path."""
    db = TMP / f"kanban{len(list(TMP.iterdir()))}.db"
    return K.connect(db)


def status(conn, tid):
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
    return row["status"] if row else None


def failures(conn, tid):
    row = conn.execute(
        "SELECT consecutive_failures FROM tasks WHERE id = ?", (tid,)
    ).fetchone()
    return int(row["consecutive_failures"] or 0) if row else None


def kinds(conn, tid):
    return [
        r["kind"]
        for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,)
        )
    ]


def new_card(conn, title, parents=()):
    task = K.create_task(
        conn, title=title, assignee="platform", parents=tuple(parents)
    )
    return task.id if hasattr(task, "id") else str(task)


# --- A. The self-parenting deadlock -----------------------------------------
print("dependency deadlock repair:")
conn = fresh()

parent = new_card(conn, "Fleet-wide Security Baseline Assessment")
K.recompute_ready(conn)
claimed = K.claim_task(conn, parent)
check("the orchestrator card claims normally", claimed is not None)

kids = [new_card(conn, f"Audit cluster {n}", parents=[parent]) for n in (1, 2, 3)]
K.recompute_ready(conn)
check(
    "children parented to a running card start unclaimable",
    all(K.claim_task(conn, k) is None for k in kids),
    "the invariant this patch exists to work around has changed upstream",
)

blocked = K.block_task(
    conn, parent, reason="waiting on the three cluster audits", kind="dependency"
)
check("the dependency block is accepted", blocked is True)
check("the repair event is recorded", "dependency_repaired" in kinds(conn, parent))

K.recompute_ready(conn)
claimed_kids = [k for k in kids if K.claim_task(conn, k) is not None]
check(
    "every child dispatches after the block",
    len(claimed_kids) == 3,
    f"only {len(claimed_kids)}/3 became claimable",
)
check(
    "the blocking card now waits instead of respawning",
    K.claim_task(conn, parent) is None,
    "the parent is claimable while its prerequisites are unfinished",
)

for k in kids:
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (k,))
conn.commit()
K.recompute_ready(conn)
check(
    "the wait resolves once the children finish",
    K.claim_task(conn, parent) is not None,
    f"parent stuck in {status(conn, parent)!r}",
)

# A real fan-in must survive one of its inputs blocking. The sink was planned
# before a was ever claimed, so it is nobody's fan-out, and releasing a would be
# pointless anyway while b is unfinished. (The first version of this patch
# failed exactly here, inverting a -> sink.)
conn = fresh()
a = new_card(conn, "researcher a")
b = new_card(conn, "researcher b")
sink = new_card(conn, "synthesizer", parents=[a, b])
K.recompute_ready(conn)
K.claim_task(conn, a)
K.block_task(conn, a, reason="waiting on an external approval", kind="dependency")
check(
    "a correct fan-in is left alone",
    "dependency_repaired" not in kinds(conn, a),
    "the repair fired on a graph that was already correct",
)
K.recompute_ready(conn)
check("the fan-in sink still waits for its inputs", K.claim_task(conn, sink) is None)
check(
    "the blocked input is still an input, not a successor",
    conn.execute(
        "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?", (a, sink)
    ).fetchone()
    is not None,
    "the pipeline was turned around",
)

# An ordinary pipeline stage that blocks for a reason of its own. This is the
# scenario the patch shipped broken: by the time `mid` runs its prerequisite is
# `done`, so the parent-set test that used to guard the repair could not tell it
# apart from the t_ab112f5b deadlock and inverted mid -> tail, dispatching the
# final stage ahead of the middle one. Driven through claim_task rather than a
# raw UPDATE precisely because that is what makes the parents settled.
conn = fresh()
upstream = new_card(conn, "prerequisite")
mid = new_card(conn, "middle stage", parents=[upstream])
tail = new_card(conn, "final stage", parents=[mid])
conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (upstream,))
conn.commit()
K.recompute_ready(conn)
check(
    "the middle stage claims once its prerequisite is done",
    K.claim_task(conn, mid) is not None,
    f"mid stuck in {status(conn, mid)!r}",
)
K.block_task(conn, mid, reason="waiting on an external approval", kind="dependency")
check(
    "a pipeline stage that blocks is left alone",
    "dependency_repaired" not in kinds(conn, mid),
    "the repair rewrote a pipeline it had no hand in creating",
)
check(
    "the pipeline keeps its direction",
    conn.execute(
        "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?", (mid, tail)
    ).fetchone()
    is not None,
)
K.recompute_ready(conn)
check(
    "the final stage still waits its turn",
    K.claim_task(conn, tail) is None,
    "the successor was dispatched ahead of the stage it follows",
)

# --- B. Claim fencing across a container restart ----------------------------
print("claim fencing:")
conn = fresh()

DEAD_PID = 4_194_303  # above every plausible pid_max, so _pid_alive is False
check("the probe pid really is dead", not K._pid_alive(DEAD_PID))

import socket  # noqa: E402

pod = socket.gethostname()
predecessor = f"{pod}:4"  # same pod name, the process that died
check(
    "the predecessor's token would have passed upstream's host-prefix test",
    predecessor.startswith(f"{pod.split(':')[0]}:"),
)

orphan = new_card(conn, "card in flight when the gateway crashed")
K.recompute_ready(conn)
K.claim_task(conn, orphan, claimer=predecessor)
conn.execute(
    "UPDATE tasks SET worker_pid = ?, started_at = 1 WHERE id = ?", (DEAD_PID, orphan)
)
conn.commit()
check("the card is running under the old claim", status(conn, orphan) == "running")

crashed = K.detect_crashed_workers(conn)
check(
    "the successor does not report its predecessor's worker as a crash",
    orphan not in crashed,
    "a recycled PID namespace was adjudicated as if it were still ours",
)
check(
    "the card is handed back rather than left for the 900s TTL",
    status(conn, orphan) == "ready",
    f"left in {status(conn, orphan)!r}",
)
events = kinds(conn, orphan)
check("the release is recorded as reclaimed", "reclaimed" in events)
check(
    "one gateway crash costs the card one retry, not the card",
    "gave_up" not in events and "crashed" not in events,
    f"events: {events}",
)
check(
    "and that retry is actually spent",
    failures(conn, orphan) == 1,
    f"consecutive_failures is {failures(conn, orphan)}, so nothing bounds a reclaim loop",
)


def reclaim_cycle(conn, tid, cycle):
    """One turn of claim -> the dispatcher dies -> the successor sweeps."""
    K.claim_task(conn, tid, claimer=f"{pod}:{900 + cycle}")
    conn.execute(
        "UPDATE tasks SET worker_pid = ?, started_at = 1 WHERE id = ?", (DEAD_PID, tid)
    )
    conn.commit()
    K.detect_crashed_workers(conn)
    K.recompute_ready(conn)


# A card that kills the dispatcher gets reclaimed by whatever comes up next.
# Uncharged that is a closed loop — claim, restart, reclaim, promote, forever —
# so the breaker has to be on this path, at its own threshold.
conn = fresh()
poison = new_card(conn, "card whose work takes the gateway down with it")
K.recompute_ready(conn)
for cycle in range(K.DEFAULT_FAILURE_LIMIT + 2):
    if status(conn, poison) == "blocked":
        break
    reclaim_cycle(conn, poison, cycle)
check(
    "a card that keeps killing its dispatcher is parked instead of cycling",
    status(conn, poison) == "blocked",
    f"still {status(conn, poison)!r} with {failures(conn, poison)} failures after "
    f"{K.DEFAULT_FAILURE_LIMIT + 2} reclaims",
)
check(
    "the breaker used its own budget rather than a second one",
    failures(conn, poison) >= K.DEFAULT_FAILURE_LIMIT,
    f"parked at {failures(conn, poison)} of {K.DEFAULT_FAILURE_LIMIT}",
)
check("parking the poison card is announced", "gave_up" in kinds(conn, poison))
check(
    "a parked card stays parked",
    K.claim_task(conn, poison) is None,
    "recompute_ready promoted a card the breaker had given up on",
)

# The founding property of this patch: one infrastructure event must never
# abandon every card that happened to be in flight. Six identical reclaim
# messages must not read as one systemic fault.
conn = fresh()
fleet = [new_card(conn, f"card {n} in flight during the rollout") for n in range(6)]
K.recompute_ready(conn)
for n, tid in enumerate(fleet):
    K.claim_task(conn, tid, claimer=f"{pod}:{800 + n}")
    conn.execute(
        "UPDATE tasks SET worker_pid = ?, started_at = 1 WHERE id = ?", (DEAD_PID, tid)
    )
conn.commit()
K.detect_crashed_workers(conn)
check(
    "a rollout hands every in-flight card back at once",
    all(status(conn, t) == "ready" for t in fleet),
    f"statuses: {[status(conn, t) for t in fleet]}",
)
check(
    "and abandons none of them",
    all(failures(conn, t) == 1 for t in fleet),
    f"failures: {[failures(conn, t) for t in fleet]}",
)

# Our own dead worker must still be adjudicated — the fence narrows the check,
# it must not disable it.
conn = fresh()
mine = new_card(conn, "card whose worker really did die")
K.recompute_ready(conn)
K.claim_task(conn, mine)
conn.execute(
    "UPDATE tasks SET worker_pid = ?, started_at = 1 WHERE id = ?", (DEAD_PID, mine)
)
conn.commit()
check(
    "our own dead worker is still detected",
    mine in K.detect_crashed_workers(conn),
    "the fence disabled crash detection instead of narrowing it",
)

# A live foreign worker is never taken away.
conn = fresh()
import os  # noqa: E402

live = new_card(conn, "card held by a live process elsewhere")
K.recompute_ready(conn)
K.claim_task(conn, live, claimer="some-other-pod:7")
conn.execute(
    "UPDATE tasks SET worker_pid = ?, started_at = 1 WHERE id = ?", (os.getpid(), live)
)
conn.commit()
K.detect_crashed_workers(conn)
check(
    "a live foreign worker keeps its claim",
    status(conn, live) == "running",
    "the sweep reclaimed a card from a process that is still running it",
)

# --- C. A burst of identical crashes must still look systemic ---------------
#
# ``_error_fingerprint`` is reachable from exactly two call sites, both inside
# ``detect_crashed_workers``, and every message that path builds carries a pid.
# So the ``pid N`` substitution is not cosmetic: without it no two concurrent
# workers can share a bucket, ``_fp_counts`` never reaches 3, and the heuristic
# that halts a board where everything is failing the same way is dead code.
# This patch leaves the substitution alone — the fence above is what keeps a
# predecessor's workers out of the bucket in the first place.
print("failure fingerprints:")
for template in ("pid {} not alive", "pid {} exited with code 137"):
    prints = {K._error_fingerprint(template.format(p)) for p in range(1044, 1050)}
    check(
        f"six workers felled by one event share a fingerprint ({template})",
        len(prints) == 1,
        f"split into {len(prints)} — the systemic heuristic can never reach 3",
    )
check(
    "distinct faults keep distinct fingerprints",
    K._error_fingerprint("pid 1044 exited with code 137")
    != K._error_fingerprint("pid 1044 killed by signal 9"),
)

# And the detector it feeds must actually fire, through the real engine.
DEAD_PIDS = [DEAD_PID - n for n in range(4)]
check("the probe pids really are dead", not any(K._pid_alive(p) for p in DEAD_PIDS))

conn = fresh()
burst = [new_card(conn, f"card {n} whose worker was OOM-killed") for n in range(4)]
K.recompute_ready(conn)
for tid, p in zip(burst, DEAD_PIDS):
    K.claim_task(conn, tid)
    conn.execute(
        "UPDATE tasks SET worker_pid = ?, started_at = 1 WHERE id = ?", (p, tid)
    )
conn.commit()
K.detect_crashed_workers(conn)
check(
    "four of our own workers dying the same way in one tick reads as systemic",
    all(status(conn, t) == "blocked" for t in burst),
    f"statuses: {[status(conn, t) for t in burst]} — is_systemic never fired",
)

# Below the heuristic's threshold nothing is systemic: two crashes are two
# crashes, and each card keeps the rest of its budget.
conn = fresh()
pair = [new_card(conn, f"card {n} whose worker died alone") for n in range(2)]
K.recompute_ready(conn)
for tid, p in zip(pair, DEAD_PIDS):
    K.claim_task(conn, tid)
    conn.execute(
        "UPDATE tasks SET worker_pid = ?, started_at = 1 WHERE id = ?", (p, tid)
    )
conn.commit()
K.detect_crashed_workers(conn)
check(
    "two crashes in a tick are just two crashes",
    all(status(conn, t) == "ready" for t in pair),
    f"statuses: {[status(conn, t) for t in pair]} — the heuristic fired below 3",
)

print()
if FAILURES:
    print(f"verify_kanban_scheduling: {len(FAILURES)} FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("verify_kanban_scheduling: all checks passed")
