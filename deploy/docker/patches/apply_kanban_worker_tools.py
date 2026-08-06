#!/usr/bin/env python3
"""Wire tools/kanban_worker_tools.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. Eight anchored
string replacements in one file — an import plus one ``check_fn`` per
worker-only tool — with the same guarantee as the other patches here: every
anchor must be found exactly once, the file must still parse, and anything else
fails the build loudly rather than shipping a half-patched image.

Why the change is needed is documented in the module docstring of
``deploy/docker/patches/kanban_worker_tools.py``. Usage::

    python3 apply_kanban_worker_tools.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from kanban_worker_tools import WORKER_ONLY_TOOLS

RELATIVE = "tools/kanban_tools.py"

# The tail of `_check_kanban_orchestrator_mode`. `_check_kanban_mode` ends with
# `return True` above the same final line, so this two-line sequence is unique.
#
# The import has to land here rather than being appended to the end of the file
# the way the kanban_handoff_clip patch does. `check_fn=` is evaluated at import
# time, several hundred lines above the end of the module, so a trailing import
# would raise NameError before it ever ran.
IMPORT_ANCHOR = (
    "        return False\n"
    "    return _profile_has_kanban_toolset()\n"
)

IMPORT_PATCHED = IMPORT_ANCHOR + (
    "\n\n"
    "# kube-agents patch: see tools/kanban_worker_tools.py\n"
    "from tools.kanban_worker_tools import (\n"
    "    check_kanban_worker_mode as _check_kanban_worker_mode,\n"
    ")\n"
)

# Handler name per tool, so the anchor pins the whole registration block rather
# than a bare `check_fn=` line that appears a dozen times.
HANDLERS = {
    "kanban_complete": "_handle_complete",
    "kanban_block": "_handle_block",
    "kanban_heartbeat": "_handle_heartbeat",
    "kanban_link": "_handle_link",
    "kanban_attach": "_handle_attach",
    "kanban_attach_url": "_handle_attach_url",
    "kanban_attachments": "_handle_attachments",
}


def _registration(tool: str, check_fn: str) -> str:
    """Render the anchored slice of a ``registry.register`` call."""
    return (
        f'    name="{tool}",\n'
        '    toolset="kanban",\n'
        f"    schema={tool.upper()}_SCHEMA,\n"
        f"    handler={HANDLERS[tool]},\n"
        f"    check_fn={check_fn},\n"
    )


def build_patches() -> tuple:
    """Return ``(anchor, replacement, expected_count)`` triples."""
    missing = set(WORKER_ONLY_TOOLS) - set(HANDLERS)
    if missing:
        raise SystemExit(
            "kanban_worker_tools patch: no handler mapping for "
            f"{', '.join(sorted(missing))} — kanban_worker_tools.py and this "
            "applier have drifted apart."
        )
    edits = [(IMPORT_ANCHOR, IMPORT_PATCHED, 1)]
    for tool in WORKER_ONLY_TOOLS:
        edits.append(
            (
                _registration(tool, "_check_kanban_mode"),
                _registration(tool, "_check_kanban_worker_mode"),
                1,
            )
        )
    return tuple(edits)


def apply(root: Path) -> None:
    """Apply every patch under ``root``, or raise SystemExit with the reason."""
    path = root / RELATIVE
    if not path.is_file():
        raise SystemExit(f"kanban_worker_tools patch: {path} does not exist")
    source = path.read_text()
    edits = build_patches()
    for anchor, replacement, expected in edits:
        found = source.count(anchor)
        if found != expected:
            raise SystemExit(
                f"kanban_worker_tools patch: {RELATIVE}: expected {expected} "
                f"occurrence(s) of anchor, found {found}. Upstream Hermes "
                f"changed — re-derive the anchor before bumping the base "
                f"image.\n--- anchor ---\n{anchor}"
            )
        source = source.replace(anchor, replacement)
    try:
        ast.parse(source)
    except SyntaxError as e:
        raise SystemExit(
            f"kanban_worker_tools patch: {RELATIVE} no longer parses after "
            f"patching: {e}"
        )
    path.write_text(source)
    print(f"kanban_worker_tools patch: {RELATIVE} ({len(edits)} anchors)")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
