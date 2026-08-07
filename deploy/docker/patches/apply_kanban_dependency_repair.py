"""Wire ``repair_inverted_dependencies`` into ``block_task``'s dependency branch.

One anchored edit in ``hermes_cli/kanban_db.py`` plus an import trailer. The
call sits immediately before the ``dependency_wait`` event, which is only
reached once the status UPDATE has taken (``cur.rowcount != 1`` returns early
above it) — so a block that was a no-op never touches the link graph.

See the module docstring in kanban_dependency_repair.py for why this exists.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

RELATIVE = "hermes_cli/kanban_db.py"

ANCHOR = (
    '            _append_event(\n'
    '                conn, task_id, "dependency_wait",\n'
    '                {"reason": reason, "kind": kind}, run_id=run_id,\n'
)

PATCHED = (
    '            # kube-agents patch: a card that waits on cards which list *it*\n'
    '            # as their parent has deadlocked — claim_task refuses them until\n'
    '            # this card is done, and this card is waiting on them. Invert\n'
    '            # those edges so they dispatch now and this card resumes when\n'
    '            # they finish. See hermes_cli/kanban_dependency_repair.py.\n'
    '            _kanban_repair_inverted_deps(conn, task_id, reason)\n'
) + ANCHOR

TRAILER = (
    "\n\n# kube-agents patch: see hermes_cli/kanban_dependency_repair.py\n"
    "from hermes_cli.kanban_dependency_repair import (  # noqa: E402\n"
    "    repair_inverted_dependencies as _kanban_repair_inverted_deps,\n"
    ")\n"
)


def apply(root: Path) -> None:
    path = root / RELATIVE
    source = path.read_text()

    count = source.count(ANCHOR)
    if count != 1:
        raise SystemExit(
            f"dependency-repair patch: expected 1 occurrence of the "
            f"dependency_wait anchor in {RELATIVE}, found {count}.\n"
            f"--- anchor ---\n{ANCHOR}"
        )

    source = source.replace(ANCHOR, PATCHED) + TRAILER
    ast.parse(source)
    path.write_text(source)
    print(f"dependency-repair patch: {RELATIVE} (1 anchor)")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
