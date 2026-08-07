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

# Guard 2: a real fan-in must survive one of its inputs blocking. Releasing a
# is pointless while b is still unfinished, so the pipeline is left intact.
# (The first version of this patch failed exactly here, inverting a -> sink.)
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

# Guard 1: a card with a genuine unfinished prerequisite is waiting on that,
# not on its own successors. Its downstream must not be rewritten.
conn = fresh()
upstream = new_card(conn, "prerequisite")
mid = new_card(conn, "middle stage", parents=[upstream])
tail = new_card(conn, "final stage", parents=[mid])
conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (mid,))
conn.commit()
K.block_task(conn, mid, reason="waiting on the prerequisite", kind="dependency")
check(
    "a block that can hold is left alone",
    "dependency_repaired" not in kinds(conn, mid),
    "the repair rewrote a pipeline whose wait was already enforceable",
)
check(
    "the pipeline keeps its direction",
    conn.execute(
        "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?", (mid, tail)
    ).fetchone()
    is not None,
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
    status(conn, orphan) in ("todo", "ready"),
    f"left in {status(conn, orphan)!r}",
)
events = kinds(conn, orphan)
check("the release is recorded as reclaimed", "reclaimed" in events)
check(
    "the card keeps its retry budget",
    "gave_up" not in events and "crashed" not in events,
    f"events: {events}",
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

# --- C. Per-worker crashes must not look systemic ---------------------------
print("failure fingerprints:")
prints = {K._error_fingerprint(f"pid {p} not alive") for p in range(1044, 1050)}
check(
    "six dead workers produce six fingerprints",
    len(prints) == 6,
    f"collapsed to {len(prints)} — failure_limit would drop to 1",
)
check(
    "a genuinely repeated error still groups",
    len({K._error_fingerprint("provider returned 503") for _ in range(4)}) == 1,
)

print()
if FAILURES:
    print(f"verify_kanban_scheduling: {len(FAILURES)} FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("verify_kanban_scheduling: all checks passed")
