"""Fence ``detect_crashed_workers`` to this process life, and charge what it hands back.

Two anchored edits in ``hermes_cli/kanban_db.py`` plus an import trailer:

1. ``detect_crashed_workers`` — sweep dead owners' claims back to ``ready``
   first, then adjudicate worker PIDs only for claims this exact process made,
   rather than for anything sharing the pod name. The sweep is handed
   ``_resolve_crash_grace_seconds`` so it applies the same launch-window grace
   as the per-row loop it runs ahead of, and its return value is kept.
2. The tail of the same function — charge each swept card one failure through
   ``_record_task_failure``, after the sweep's transaction has closed and
   outside the ``crash_details`` fingerprint pass. Without this a card that
   kills the dispatcher is reclaimed for free by the replacement process and
   cycles forever.

``_error_fingerprint`` IS NOT PATCHED, and that is a decision rather than an
omission. An earlier version of this file replaced upstream's
``re.sub(r'\\bpid \\d+\\b', 'pid N', ...)`` with a bare ``error_text[:80]``. Every
error text that reaches the fingerprint is built by ``detect_crashed_workers``
itself and every one of them is PID-prefixed, so keeping the PID gave each
worker its own bucket, ``_fp_counts`` could never reach the ``>= 3`` the
systemic heuristic tests, and the detector that halts a board where everything
is failing identically stopped firing at all. Edit 1 removes the cause that
edit was reaching for; see the module docstring in kanban_claim_fencing.py.

The other three ``host_prefix`` comparisons (``release_stale_claims``,
``_terminate_worker``, ``enforce_max_runtime``) are deliberately left alone.
Narrowing them is not locally safe: ``_terminate_worker`` reports
``host_local: False`` by returning a never-attempted termination, which
``_worker_survived_termination`` then has to interpret, and
``release_stale_claims`` uses the same flag to choose between extending and
reclaiming. Edit 1 makes those paths near-unreachable for foreign claims anyway
— dead owners are handed back long before their 900s TTL — and the 1-hour
``last_heartbeat_at`` backstop still bounds the residual PID-collision case.

See the module docstring in kanban_claim_fencing.py for the incident.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

RELATIVE = "hermes_cli/kanban_db.py"

FENCE_ANCHOR = (
    '        rows = conn.execute(\n'
    '            "SELECT id, worker_pid, claim_lock, started_at FROM tasks "\n'
    '            "WHERE status = \'running\' AND worker_pid IS NOT NULL"\n'
    '        ).fetchall()\n'
    '        host_prefix = f"{_claimer_id().split(\':\', 1)[0]}:"\n'
    '        for row in rows:\n'
    '            # Only check liveness for claims owned by this host.\n'
    '            lock = row["claim_lock"] or ""\n'
    '            if not lock.startswith(host_prefix):\n'
    '                continue\n'
)

FENCE_PATCHED = (
    '        # kube-agents patch: under Kubernetes the host half of the claim\n'
    '        # token is the pod name, so it survives a container restart even\n'
    '        # though every process that wrote it is gone. Hand back whatever a\n'
    '        # dead owner still holds, then adjudicate PIDs only for claims this\n'
    '        # exact process made. The sweep gets the same launch-window grace\n'
    '        # period as the per-row loop below; what it hands back is charged\n'
    '        # after this transaction closes.\n'
    '        # See hermes_cli/kanban_claim_fencing.py.\n'
    '        _kanban_claimer = _claimer_id()\n'
    '        _kanban_reclaimed = _kanban_release_dead_foreign_claims(\n'
    '            conn, _kanban_claimer, _pid_alive, _resolve_crash_grace_seconds\n'
    '        )\n'
    '        rows = conn.execute(\n'
    '            "SELECT id, worker_pid, claim_lock, started_at FROM tasks "\n'
    '            "WHERE status = \'running\' AND worker_pid IS NOT NULL"\n'
    '        ).fetchall()\n'
    '        for row in rows:\n'
    '            # Only check liveness for claims made by this process life.\n'
    '            lock = row["claim_lock"] or ""\n'
    '            if not _kanban_claim_is_self(lock, _kanban_claimer):\n'
    '                continue\n'
)

# The tail of ``detect_crashed_workers``: past the ``crash_details`` accounting
# loop, so ``auto_blocked`` exists and the sweep's transaction is long closed,
# and before the side-channel stash that publishes it to ``dispatch_once``.
CHARGE_ANCHOR = (
    "    # Stash auto-blocked ids on the function for the dispatch loop to pick up.\n"
)

CHARGE_PATCHED = (
    "    # kube-agents patch: a card the sweep handed back ran and produced\n"
    "    # nothing, which is a failed run whoever's claim died -- and left\n"
    "    # uncharged it is an unbounded one, because a card that kills the\n"
    "    # dispatcher is reclaimed by the replacement process and dispatched\n"
    "    # again with its counter still at zero. Charged here rather than inside\n"
    "    # the sweep because ``_record_task_failure`` opens its own ``write_txn``,\n"
    "    # and in its own loop rather than as ``crash_details`` entries because a\n"
    "    # rollout releases every in-flight card with one identical error text,\n"
    "    # which the fingerprint pass above would read as systemic and abandon.\n"
    "    # See hermes_cli/kanban_claim_fencing.py.\n"
    "    auto_blocked.extend(\n"
    "        _kanban_charge_reclaimed_cards(\n"
    "            conn, _kanban_reclaimed, _record_task_failure\n"
    "        )\n"
    "    )\n"
    "    # Stash auto-blocked ids on the function for the dispatch loop to pick up.\n"
)

TRAILER = (
    "\n\n# kube-agents patch: see hermes_cli/kanban_claim_fencing.py\n"
    "from hermes_cli.kanban_claim_fencing import (  # noqa: E402\n"
    "    charge_reclaimed_cards as _kanban_charge_reclaimed_cards,\n"
    "    claim_is_self as _kanban_claim_is_self,\n"
    "    release_dead_foreign_claims as _kanban_release_dead_foreign_claims,\n"
    ")\n"
)

EDITS = (
    ("detect_crashed_workers fence", FENCE_ANCHOR, FENCE_PATCHED),
    ("reclaim charging", CHARGE_ANCHOR, CHARGE_PATCHED),
)


def apply(root: Path) -> None:
    path = root / RELATIVE
    source = path.read_text()

    for label, anchor, patched in EDITS:
        count = source.count(anchor)
        if count != 1:
            raise SystemExit(
                f"claim-fencing patch: expected 1 occurrence of the {label} "
                f"anchor in {RELATIVE}, found {count}.\n"
                f"--- anchor ---\n{anchor}"
            )
        source = source.replace(anchor, patched)

    source += TRAILER
    ast.parse(source)
    path.write_text(source)
    print(f"claim-fencing patch: {RELATIVE} ({len(EDITS)} anchors)")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
