#!/usr/bin/env python3
"""Make the circuit breaker's trip survive the same dispatcher tick that wrote it.

Three anchored edits in ``hermes_cli/kanban_db.py``, all inside
``_record_task_failure``. No new module, no new query, no schema change.

THE DEFECT

``dispatch_once`` runs ``detect_crashed_workers`` and then, twenty lines later,
``recompute_ready(conn, failure_limit=failure_limit)``. Those two functions each
decide whether a card is over its retry budget, and they do not use the same
threshold.

``_record_task_failure`` trips on ``if force_trip or failures >= effective_limit``
and persists ``consecutive_failures = old + 1`` -- the raw attempt count. Two
callers reach that branch with a threshold the dispatcher never sees:

* the clean-exit protocol-violation path passes ``force_trip=True`` after
  adjudicating its own violation streak against ``_PROTOCOL_VIOLATION_FAILURE_LIMIT``
  (3, or the card's ``max_retries``). A below-budget violation deliberately does
  not tick the unified counter, so a card arriving here normally has
  ``consecutive_failures == 0`` and leaves with 1.
* the systemic same-error path passes ``failure_limit=1``, so it trips at 1.

``recompute_ready`` then re-derives the verdict from ``consecutive_failures``
alone, against ``max_retries`` or the dispatcher's ``failure_limit`` (2 by
default), sees 1 < 2, and executes ``UPDATE tasks SET status = 'ready'`` plus a
``promoted`` event. The next lines of the same ``dispatch_once`` claim and spawn
the card. The block lasts zero ticks: a policy reading "stop after 3 protocol
violations and hand to a human" actually stops after 4.

WHAT THIS PATCH DOES, AND WHAT IT DELIBERATELY DOES NOT

It does not add a second source of truth. The obvious fix -- have
``recompute_ready`` read the ``gave_up`` event the breaker just wrote -- was
built and rejected: ``assign_task`` zeroes ``consecutive_failures`` on a profile
change (its comment calls reassignment "an explicit recovery action") and emits
kind ``assigned``, not ``unblocked``, without changing status. An event-stickiness
fix short-circuits on the event before the zeroed counter is consulted and pins
the reassigned card in ``blocked`` forever. It also has to ``json.loads`` a
nullable ``payload`` column inside a write transaction, and to treat
``task_events.id`` as a durable clock that ``_rebuild_drifted_tables``
reassigns.

Instead: keep the counter as the single source of truth and store the right
number in it. When the breaker trips, the budget IS exhausted, so persist the
exhausted budget rather than the raw attempt count -- the threshold the trip was
decided against, and, when there is no per-task override, never below
``DEFAULT_FAILURE_LIMIT``, which is the floor of what the reader will test.

Because the counter stays the arbiter, every counter-zeroing recovery path keeps
working untouched: ``unblock_task``, ``complete_task`` via
``_clear_failure_counter``, ``assign_task`` when it changes the assignee, and
``reassign_task``. (``reclaim_task`` is NOT one of them and never was -- it
bails unless the card is ``running`` or still holds a ``claim_lock``, and the
trip cleared both.) Note that ``assign_task`` to the SAME profile is a no-op on
the counter, so post-patch it no longer frees a tripped card; reassign to a
different profile, or use ``hermes kanban unblock``. A card blocked purely by a
parent dependency never trips, so it still auto-recovers. ``gave_up`` does not
become sticky.

The ``gave_up`` payload is not touched: ``payload["failures"]`` still reports the
true attempt count. Reconstructing the stored counter from that payload needs
the floor too, because the floor is invisible in it::

    stored = max(failures, effective_limit)                    # limit_source == "task"
    stored = max(failures, effective_limit, DEFAULT_FAILURE_LIMIT)   # otherwise

``limit_source == "task"`` is exactly ``task_override is not None``. A systemic
trip is the arm that makes the naive one-line formula wrong: it stores 2 while
its payload says ``{"failures": 1, "effective_limit": 1}``.

KNOWN LIMIT: ``detect_crashed_workers`` never sees the dispatcher's configured
``failure_limit``, so the floor makes the two derivations agree under the shipped
configuration (``kanban.failure_limit`` unset, ``DEFAULT_FAILURE_LIMIT = 2``).
Raise it and the trips become revertible again in order: a systemic trip (stored
2) once ``kanban.failure_limit`` exceeds 2, and a protocol trip (stored 3) once
it exceeds 3. Closing that needs the limit plumbed into
``detect_crashed_workers``, which is a larger change to a function
``kanban_claim_fencing`` already edits.

BEHAVIOUR CHANGE: a card that carries a breaker trip AND an undone parent no
longer auto-promotes when the parent completes. That is the same guard #35072
already applies to normally-tripped cards; a force-tripped card is by definition
one whose budget was declared exhausted. Expect more cards to sit in ``blocked``
awaiting a human. ``kanban_diagnostics._rule_repeated_failures`` (threshold 3)
surfaces the protocol-violation case.

Usage::

    python3 apply_kanban_breaker_counter.py [HERMES_ROOT]  # /opt/hermes
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

RELATIVE = "hermes_cli/kanban_db.py"

# Build marker the Dockerfile greps for.
BUILD_MARKER = "persisted_failures = max(failures, effective_limit)"

TRIP_ANCHOR = (
    "        if force_trip or failures >= effective_limit:\n"
    "            # Trip the breaker.\n"
)

TRIP_PATCHED = (
    "        if force_trip or failures >= effective_limit:\n"
    "            # Trip the breaker.\n"
    "            # kube-agents patch: persist the exhausted budget, not the raw\n"
    "            # attempt count. ``recompute_ready`` re-derives this same verdict\n"
    "            # from ``consecutive_failures`` alone, against its own limit, and\n"
    "            # promotes the card straight back to ``ready`` in the same\n"
    "            # ``dispatch_once`` tick whenever the stored count is below that\n"
    "            # limit -- which is exactly what a ``force_trip`` (protocol\n"
    "            # violation, decided against its own streak) or a caller-lowered\n"
    "            # ``failure_limit`` (systemic crash, 1) leaves behind. Flooring\n"
    "            # the stored counter at the threshold this trip was decided\n"
    "            # against makes the two derivations agree with no new state, and\n"
    "            # keeps the counter the single arbiter -- so every path that\n"
    "            # zeroes it (unblock_task, complete_task, reassign_task, and\n"
    "            # assign_task to a DIFFERENT profile) still releases the card.\n"
    "            # reclaim_task does not: it bails unless the card is running or\n"
    "            # still holds a claim_lock, and the trip cleared both.\n"
    "            # See deploy/docker/patches/apply_kanban_breaker_counter.py.\n"
    "            persisted_failures = max(failures, effective_limit)\n"
    "            if task_override is None:\n"
    "                # No per-task override, so the reader resolves against the\n"
    "                # dispatcher's limit, whose shipped value is the module\n"
    "                # default. Never store below it.\n"
    "                persisted_failures = max(\n"
    "                    persisted_failures, DEFAULT_FAILURE_LIMIT\n"
    "                )\n"
)

# Spawn-failure branch bind tuple. The bind line alone occurs four times in the
# function (two in this blocked branch, two in the below-threshold branch that
# must keep the raw count), so the preceding WHERE clause is load-bearing for
# uniqueness -- it is what distinguishes this branch from its sibling.
SPAWN_BIND_ANCHOR = (
    "                    \"WHERE id = ? AND status IN ('running', 'ready')\",\n"
    "                    (failures, error[:500], task_id),\n"
)

SPAWN_BIND_PATCHED = (
    "                    \"WHERE id = ? AND status IN ('running', 'ready')\",\n"
    "                    (persisted_failures, error[:500], task_id),\n"
)

# Timeout/crash branch bind tuple.
CRASH_BIND_ANCHOR = (
    "                    \"WHERE id = ? AND status IN ('ready', 'running')\",\n"
    "                    (failures, error[:500], task_id),\n"
)

CRASH_BIND_PATCHED = (
    "                    \"WHERE id = ? AND status IN ('ready', 'running')\",\n"
    "                    (persisted_failures, error[:500], task_id),\n"
)

EDITS = (
    ("_record_task_failure trip floor", TRIP_ANCHOR, TRIP_PATCHED),
    ("spawn-path blocked bind", SPAWN_BIND_ANCHOR, SPAWN_BIND_PATCHED),
    ("crash-path blocked bind", CRASH_BIND_ANCHOR, CRASH_BIND_PATCHED),
)


def apply(root: Path) -> None:
    path = root / RELATIVE
    source = path.read_text()

    for label, anchor, patched in EDITS:
        count = source.count(anchor)
        if count != 1:
            raise SystemExit(
                f"breaker-counter patch: expected 1 occurrence of the {label} "
                f"anchor in {RELATIVE}, found {count}.\n"
                f"--- anchor ---\n{anchor}"
            )
        source = source.replace(anchor, patched)

    ast.parse(source)
    path.write_text(source)
    print(f"breaker-counter patch: {RELATIVE} ({len(EDITS)} anchors)")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
