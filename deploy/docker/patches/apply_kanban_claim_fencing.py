"""Fence ``detect_crashed_workers`` to this process life, and keep PIDs in fingerprints.

Two anchored edits in ``hermes_cli/kanban_db.py`` plus an import trailer:

1. ``detect_crashed_workers`` — sweep dead owners' claims back to ``todo`` first,
   then adjudicate worker PIDs only for claims this exact process made, rather
   than for anything sharing the pod name. The sweep is handed
   ``_resolve_crash_grace_seconds`` so it applies the same launch-window grace
   as the per-row loop it runs ahead of.
2. ``_error_fingerprint`` — stop normalising ``pid <N>`` to ``pid N``, which
   collapsed N per-worker crashes into one "systemic" fingerprint and dropped
   every affected card's failure limit to 1.

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
    '        # period as the per-row loop below.\n'
    '        # See hermes_cli/kanban_claim_fencing.py.\n'
    '        _kanban_claimer = _claimer_id()\n'
    '        _kanban_release_dead_foreign_claims(\n'
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

FP_ANCHOR = (
    "    fp = re.sub(r'\\bpid \\d+\\b', 'pid N', error_text[:80])\n"
)

FP_PATCHED = (
    "    # kube-agents patch: the PID must survive fingerprinting. Normalising it\n"
    "    # away made N workers killed by one gateway crash look like one systemic\n"
    "    # error, which trips failure_limit=1 and gives up on the first failure.\n"
    "    # See hermes_cli/kanban_claim_fencing.py.\n"
    "    fp = error_text[:80]\n"
)

TRAILER = (
    "\n\n# kube-agents patch: see hermes_cli/kanban_claim_fencing.py\n"
    "from hermes_cli.kanban_claim_fencing import (  # noqa: E402\n"
    "    claim_is_self as _kanban_claim_is_self,\n"
    "    release_dead_foreign_claims as _kanban_release_dead_foreign_claims,\n"
    ")\n"
)

EDITS = (
    ("detect_crashed_workers fence", FENCE_ANCHOR, FENCE_PATCHED),
    ("_error_fingerprint pid", FP_ANCHOR, FP_PATCHED),
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
