"""Repair inverted fan-out dependency edges instead of deadlocking on them.

Background — the deadlock this exists to break
----------------------------------------------
``task_links(parent_id, child_id)`` is a *predecessor* edge, not a hierarchy.
``claim_task`` enforces it as a structural invariant::

    SELECT 1 FROM task_links l JOIN tasks p ON p.id = l.parent_id
     WHERE l.child_id = ? AND p.status NOT IN ('done', 'archived') LIMIT 1

A card whose parent is not ``done`` can never be claimed. ``recompute_ready``
applies the same rule when promoting ``todo -> ready``, and declines *silently*
— no event, no log line.

Upstream's own contract, from the ``kanban_create`` tool description, is
"create a child of the current one (pass the current task id in ``parents``)
… then **complete your own task**". The fan-out only runs once the creator
completes. That is a legitimate continuation idiom and this patch does not
interfere with it.

The failure is the *other* half. An orchestrator that creates cards with
``parents=[<its own running card>]`` and then calls
``kanban_block(kind="dependency")`` to wait for them has built an unconditional
deadlock: the children cannot start until the parent is done, and the parent
will not finish until the children are. Nothing in the engine detects it.

What that cost us on 2026-08-07
-------------------------------
Card ``t_ab112f5b`` ("Fleet-wide Security Baseline Assessment") created three
per-cluster cards parented to itself and declared ``dependency_wait`` on them
four seconds later. The children logged ``claim_rejected
{"reason": "parents_not_done"}`` and never ran — zero ``task_runs``, no
``started_at``, no ``completed_at``. ``block_task(kind="dependency")`` routes to
``todo`` rather than ``blocked``, which skips the ``block_recurrences`` loop
breaker entirely, so the parent was re-promoted and re-spawned every few
seconds, burning a full agent run each cycle.

The worker eventually diagnosed the deadlock itself, re-created the same work as
three *parentless* cards (which ran fine), and then escaped its own wait by
shelling out::

    python3 -c "import sqlite3; ...
      UPDATE tasks SET status='done', result='Completed by Platform Agent'
       WHERE id IN ('t_d3123d56','t_db5847ee','t_f342ef6d')"

Three cards closed ``done`` with a 27-character fabricated ``result``, no
``completed`` event, and no run row. A second instance (``t_e3089a85``) was
still spinning when this patch was written, with three cluster audit cards
starved in ``todo`` for 15 minutes and counting.

The repair
----------
The agent's intent when it blocks is unambiguous: it wants those cards to run
*now* and wants to resume once they finish. That is precisely the fan-in shape
the engine already supports — it just has the edges pointing the wrong way. So
rather than refusing the block (which leaves the worker to improvise, and we
have seen what it improvises), invert the mis-directed edges:

    delete (self -> child)   and   insert (child -> self)

The children lose the parent that was gating them and get dispatched on the next
tick. The blocking card gains them as genuine prerequisites, so
``recompute_ready`` keeps it in ``todo`` until every one of them is ``done`` —
which is the wait the agent asked for, now enforced by the engine instead of by
a spin loop. No agent cooperation is required and no card is left stranded.

When it is allowed to fire
--------------------------
``block_task`` never records *what* a card is waiting for, so "invert every
unsettled successor" would rewrite healthy pipelines: a researcher card that
fans out to a synthesizer and then blocks for some unrelated reason would have
its whole downstream turned around. Two guards keep this to the broken shape,
and the in-image gate (``verify_kanban_scheduling.py``) exercises both against
the real engine — the second guard exists because the first version of this
patch failed that gate by inverting a legitimate fan-in.

1. **The blocking card must have no unsettled parents of its own.** With one,
   ``recompute_ready`` has something real to hold it on and the block means what
   it says — hands off. With none, the block is provably a no-op: the card is
   re-promoted on the very next tick, which is the spin loop we measured. Only
   then is there anything to repair.

2. **Inverting must actually free the child.** An edge is inverted only if this
   card is the *last* unfinished parent gating that child. A synthesizer waiting
   on two researchers is not unblocked by releasing one of them, so turning that
   edge around would restructure the graph and change nothing — it is left
   alone. This is what keeps real fan-ins intact.

An edge is also left alone if inverting it would create a cycle (the child
already reaches this card by some other path). Deadlock is preferable to a
corrupt graph, and the caller still gets told about it.
"""

from __future__ import annotations

import json
import time

# Statuses that satisfy the parent gate in ``claim_task`` / ``recompute_ready``.
SETTLED = "('done', 'archived')"

EVENT_KIND = "dependency_repaired"


def _reaches(conn, start: str, target: str) -> bool:
    """True if ``target`` is reachable walking parent->child edges from ``start``."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        rows = conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ?", (node,)
        ).fetchall()
        stack.extend(r["child_id"] for r in rows)
    return False


def has_unsettled_parents(conn, task_id: str) -> bool:
    """Whether ``recompute_ready`` has anything real to hold this card on.

    The predicate is ``claim_task``'s, verbatim. False means a dependency block
    on this card cannot hold: it will be re-promoted on the next tick.
    """
    row = conn.execute(
        "SELECT 1 FROM task_links l "
        "JOIN tasks p ON p.id = l.parent_id "
        f"WHERE l.child_id = ? AND p.status NOT IN {SETTLED} LIMIT 1",
        (task_id,),
    ).fetchone()
    return row is not None


def find_deadlocked_children(conn, task_id: str) -> list[str]:
    """Unsettled children that ``task_id`` alone is still gating.

    Children with another unfinished parent are excluded: inverting those would
    not free them, so it would be a graph rewrite with no benefit. See guard 2
    in the module docstring.
    """
    rows = conn.execute(
        "SELECT l.child_id FROM task_links l "
        "JOIN tasks c ON c.id = l.child_id "
        f"WHERE l.parent_id = ? AND c.status NOT IN {SETTLED} "
        "  AND NOT EXISTS ("
        "        SELECT 1 FROM task_links o "
        "        JOIN tasks op ON op.id = o.parent_id "
        "        WHERE o.child_id = l.child_id "
        "          AND o.parent_id <> l.parent_id "
        f"          AND op.status NOT IN {SETTLED}"
        "  ) "
        "ORDER BY l.child_id",
        (task_id,),
    ).fetchall()
    return [r["child_id"] for r in rows]


def repair_inverted_dependencies(conn, task_id: str, reason: str = "") -> list[str]:
    """Invert every edge that would deadlock a dependency-block on ``task_id``.

    Returns the ids whose edge was inverted. Safe to call when there is nothing
    to repair — the common case — in which case it returns ``[]`` and writes
    nothing. Must be called inside an open write transaction; it does not open
    one of its own.
    """
    if has_unsettled_parents(conn, task_id):
        # The block will hold on its own. Whatever this card's successors are,
        # they are not what it is waiting for. See guard 1 in the docstring.
        return []

    children = find_deadlocked_children(conn, task_id)
    if not children:
        return []

    repaired: list[str] = []
    skipped: list[str] = []
    for child in children:
        # Drop the gating edge first so the cycle probe sees the graph as it
        # will be, not as it was.
        conn.execute(
            "DELETE FROM task_links WHERE parent_id = ? AND child_id = ?",
            (task_id, child),
        )
        if _reaches(conn, child, task_id):
            # Inverting would close a loop. Put it back and leave this one be.
            conn.execute(
                "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (task_id, child),
            )
            skipped.append(child)
            continue
        conn.execute(
            "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (child, task_id),
        )
        repaired.append(child)

    if repaired or skipped:
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                task_id,
                None,
                EVENT_KIND,
                json.dumps(
                    {
                        "inverted": repaired,
                        "skipped_would_cycle": skipped,
                        "reason": (reason or "")[:200],
                        "detail": (
                            "cards listed this one as a parent while it waited on "
                            "them; edges inverted so they can be dispatched and "
                            "this card resumes once they are done"
                        ),
                    }
                ),
                int(time.time()),
            ),
        )
    return repaired
