#!/usr/bin/env python3
"""Wire gateway/kanban_result_delivery.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. One anchored edit:
the completion message gains the card's ``result`` before it is built.

The anchor is the ``completed`` branch's ``msg = (...)`` assignment. It is the
only ``msg`` block in ``gateway/kanban_watchers.py`` ending in ``done"``, and
crucially it sits *after* the handoff/``_clip_handoff`` lines that
``apply_kanban_wake_kinds`` and the ``kanban_handoff_clip`` edit in the
Dockerfile touch — so this patch composes with those instead of fighting them
for the same lines, whatever order the build applies them in.

Why the change is needed is documented in the module docstring of
``deploy/docker/patches/kanban_result_delivery.py``. Usage::

    python3 apply_kanban_result_delivery.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

RELATIVE = "gateway/kanban_watchers.py"

INDENT = " " * 28

ANCHOR = (
    f"{INDENT}msg = (\n"
    f"{INDENT}    f\"✔ {{board_tag}}{{tag}}Kanban {{sub['task_id']}} done\"\n"
    f"{INDENT}    f\" — {{title}}{{handoff}}\"\n"
    f"{INDENT})\n"
)

PATCHED = (
    f"{INDENT}# kube-agents patch: see gateway/kanban_result_delivery.py\n"
    f"{INDENT}handoff += _kanban_result_block(handoff, task)\n"
) + ANCHOR

# Appended rather than inserted: the name is resolved when the notifier loop
# runs, long after the module finishes importing. Same placement the
# kanban_handoff_clip and kanban_wake_kinds patches use.
TRAILER = (
    "\n\n# kube-agents patch: see gateway/kanban_result_delivery.py\n"
    "from gateway.kanban_result_delivery import "
    "result_block_for_task as _kanban_result_block\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    path = root / RELATIVE
    if not path.is_file():
        raise SystemExit(f"kanban_result_delivery patch: {path} does not exist")
    source = path.read_text()
    found = source.count(ANCHOR)
    if found != 1:
        raise SystemExit(
            f"kanban_result_delivery patch: {RELATIVE}: expected 1 occurrence "
            f"of anchor, found {found}. Upstream Hermes changed — re-derive "
            f"the anchor before bumping the base image.\n"
            f"--- anchor ---\n{ANCHOR}"
        )
    source = source.replace(ANCHOR, PATCHED) + TRAILER
    try:
        ast.parse(source)
    except SyntaxError as e:
        raise SystemExit(
            f"kanban_result_delivery patch: {RELATIVE} no longer parses after "
            f"patching: {e}"
        )
    path.write_text(source)
    print(f"kanban_result_delivery patch: {RELATIVE} (1 anchor)")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
