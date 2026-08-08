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
``kanban_block`` takes ``reason`` and ``kind`` and nothing else, so
``block_task`` never records *what* a card is waiting for and the shape of the
graph is the only evidence there is. "Invert every unsettled successor" reads
that evidence wrong — it rewrites healthy pipelines, which is the bug this
module shipped with and which the last section describes.

What separates the two shapes is *when the edge appeared*. A pipeline's
successor was created by whoever planned the pipeline, before the card that now
blocks had ever been claimed. The deadlocking children were created by this
card, out of a ``kanban_create`` that named it in ``parents``, after it started
running. So an edge is inverted only when the child's ``created`` event is newer
than the blocking card's first ``claimed`` event. ``create_task`` writes the one
and ``claim_task`` / ``reclaim_task`` the other, and ``task_events.id`` is a
single ``AUTOINCREMENT`` sequence for the whole board, so the comparison is an
exact ordering. The obvious alternative — ``tasks.created_at`` against
``tasks.started_at`` — is whole seconds, and a planner laying out a pipeline in
the same second the dispatcher claims its first card is indistinguishable from
the deadlock by those.

The watermark is the *first* claim, the same instant ``claim_task``'s
``started_at = COALESCE(started_at, ?)`` records, not the current run's. A
worker that fans out three cards and is then killed leaves them exactly as
deadlocked as one that blocks; when the card is re-claimed and blocks for real,
those three still need releasing.

A card with no ``claimed`` event has no window at all, the comparison is NULL
and nothing is repaired. That is the right answer for a card someone forced into
``running`` with raw SQL.

The second condition is that inverting must actually free the child: the edge is
turned around only if this card is the *last* unfinished parent gating it. A
child created with two parents is not released by releasing one of them, so
turning that edge around would restructure the graph and change nothing. It also
holds the repair to the flat fan-out we have actually observed, the one shape
where no inversion can change the answer the cycle probe gives for the next
child in the batch.

An edge is also left alone if inverting it would create a cycle (the child
already reaches this card by some other path). Deadlock is preferable to a
corrupt graph, and the caller still gets told about it.

The in-image gate (``verify_kanban_scheduling.py``) drives all of this against
the real engine rather than a mock, because every one of these conditions is
about scheduling behaviour that the schema alone does not show.

The guard that did not work
---------------------------
Until this was measured the first condition was "the blocking card must have no
unsettled parents of its own", on the theory that a card with a live
prerequisite is waiting on *that* and means its block literally. It never
discriminated anything. ``claim_task`` refuses to move a card to ``running``
while any parent is unsettled and ``recompute_ready`` only promotes
``todo -> ready`` once every parent is ``done`` or ``archived``, so every card
``block_task`` will accept has settled parents by construction. Replayed against
the engine on a throwaway board, an ordinary pipeline stage (``A`` done, ``B``
claimed, ``B -> C``) and the ``t_ab112f5b`` deadlock both reported no unsettled
parents — and the guard duly let the pipeline through, inverting ``B -> C`` into
``C -> B`` so ``C`` was dispatched ahead of the stage it exists to follow. A
two-input fan-in went the same way the moment one of its inputs finished.

The only shape that makes the predicate true is ``link_tasks`` attaching a new
parent to an already-running card, and there the card's fan-out children are
still genuinely deadlocked — so suppressing the repair was wrong in that case
too. The guard is deleted rather than repaired.
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


def find_deadlocked_children(conn, task_id: str) -> list[str]:
    """Unsettled cards ``task_id`` fanned out after it started and alone gates.

    Two conditions beyond "is an unsettled child", both explained at length in
    the module docstring. The run window — the child's ``created`` event is
    newer than this card's first ``claimed`` event — is what tells a card this
    one fanned out apart from a pipeline successor somebody else planned. The
    ``NOT EXISTS`` is what stops the repair rewriting an edge whose inversion
    would free nothing. Either subquery returning NULL makes the comparison NULL
    and drops the row, which is the conservative answer for a card that reached
    ``running`` without ever being claimed.
    """
    rows = conn.execute(
        "SELECT l.child_id FROM task_links l "
        "JOIN tasks c ON c.id = l.child_id "
        f"WHERE l.parent_id = ? AND c.status NOT IN {SETTLED} "
        "  AND (SELECT MIN(birth.id) FROM task_events birth "
        "        WHERE birth.task_id = l.child_id AND birth.kind = 'created') "
        "      > (SELECT MIN(started.id) FROM task_events started "
        "          WHERE started.task_id = l.parent_id "
        "            AND started.kind = 'claimed') "
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

    Calling it twice is a no-op the second time: the first pass turned every
    edge it touched around, so the card has no outgoing edges left for
    ``find_deadlocked_children`` to find.
    """
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
        # The probe has to follow the edge about to be *inserted*, not the one
        # just deleted: adding child -> task_id closes a loop exactly when
        # task_id can still reach child by some other path.
        if _reaches(conn, task_id, child):
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
