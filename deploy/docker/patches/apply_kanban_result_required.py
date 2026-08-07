#!/usr/bin/env python3
"""Wire tools/kanban_result_required.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. Two anchored edits:
the completion gate, and an import/schema-fixup block placed immediately before
``kanban_complete`` is registered.

The schema wording is NOT edited textually — ``apply_schema`` rewrites the live
``KANBAN_COMPLETE_SCHEMA`` dict at import time. Placing the call before
``registry.register`` rather than at end-of-file means the registry cannot
capture the pre-patch wording even if it copies the dict it is handed.

Why the change is needed is documented in the module docstring of
``deploy/docker/patches/kanban_result_required.py``. Usage::

    python3 apply_kanban_result_required.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kanban_result_required import NEW_GATE, OLD_GATE  # noqa: E402

RELATIVE = "tools/kanban_tools.py"

# The registration block. Anchored on all four leading lines because
# ``registry.register(\n    name=`` recurs for every kanban tool, and the
# ``schema=KANBAN_COMPLETE_SCHEMA`` line is what makes this one the right
# insertion point.
REGISTER_ANCHOR = (
    'registry.register(\n'
    '    name="kanban_complete",\n'
    '    toolset="kanban",\n'
    '    schema=KANBAN_COMPLETE_SCHEMA,\n'
)

# Imported at module scope so ``apply_schema`` runs while the module is still
# importing — before any tool call and before the registry hands the schema to a
# model. ``_require_result`` is resolved later, when a worker actually completes.
REGISTER_PATCHED = (
    "# kube-agents patch: see tools/kanban_result_required.py\n"
    "from tools.kanban_result_required import (\n"
    "    apply_schema as _apply_result_schema,\n"
    "    require_result as _require_result,\n"
    ")\n"
    "\n"
    "_apply_result_schema(KANBAN_COMPLETE_SCHEMA)\n"
    "\n"
) + REGISTER_ANCHOR


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    path = root / RELATIVE
    if not path.is_file():
        raise SystemExit(f"kanban_result_required patch: {path} does not exist")
    source = path.read_text()

    for label, anchor in (("gate", OLD_GATE), ("registration", REGISTER_ANCHOR)):
        found = source.count(anchor)
        if found != 1:
            raise SystemExit(
                f"kanban_result_required patch: {RELATIVE}: expected 1 "
                f"occurrence of the {label} anchor, found {found}. Upstream "
                f"Hermes changed — re-derive the anchor before bumping the "
                f"base image.\n--- anchor ---\n{anchor}"
            )

    source = source.replace(OLD_GATE, NEW_GATE)
    source = source.replace(REGISTER_ANCHOR, REGISTER_PATCHED)

    try:
        ast.parse(source)
    except SyntaxError as e:
        raise SystemExit(
            f"kanban_result_required patch: {RELATIVE} no longer parses after "
            f"patching: {e}"
        )
    path.write_text(source)
    print(f"kanban_result_required patch: {RELATIVE} (2 anchors)")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
