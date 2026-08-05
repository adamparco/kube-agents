#!/usr/bin/env python3
"""Wire gateway/kanban_wake_kinds.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. Only two lines
change, but the anchor carries nested quotes and a set comprehension, which is
past the point where an inline ``python3 -c`` in a Dockerfile stays legible. The
guarantee is the same as the other patches here: the anchor must be found
exactly once, the file must still parse, and anything else fails the build
loudly rather than shipping a half-patched image.

Why the change is needed is documented in the module docstring of
``deploy/docker/patches/kanban_wake_kinds.py``. Usage::

    python3 apply_kanban_wake_kinds.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

RELATIVE = "gateway/kanban_watchers.py"

INDENT = " " * 24

ANCHOR = (
    f'{INDENT}_WAKE_KINDS = ("completed", "gave_up", "crashed", "timed_out", "blocked")\n'
    f'{INDENT}_wake_kinds = {{ev.kind for ev in d["events"] if ev.kind in _WAKE_KINDS}}\n'
)

PATCHED = (
    f"{INDENT}# kube-agents patch: see gateway/kanban_wake_kinds.py\n"
    f'{INDENT}_wake_kinds = _wake_kinds_for(d["events"])\n'
)

# Appended rather than inserted: unlike a `check_fn=`, this name is resolved
# when the notifier loop runs, long after the module finishes importing. Same
# placement the kanban_handoff_clip patch uses.
TRAILER = (
    "\n\n# kube-agents patch: see gateway/kanban_wake_kinds.py\n"
    "from gateway.kanban_wake_kinds import wake_kinds_for as _wake_kinds_for\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    path = root / RELATIVE
    if not path.is_file():
        raise SystemExit(f"kanban_wake_kinds patch: {path} does not exist")
    source = path.read_text()
    found = source.count(ANCHOR)
    if found != 1:
        raise SystemExit(
            f"kanban_wake_kinds patch: {RELATIVE}: expected 1 occurrence of "
            f"anchor, found {found}. Upstream Hermes changed — re-derive the "
            f"anchor before bumping the base image.\n--- anchor ---\n{ANCHOR}"
        )
    source = source.replace(ANCHOR, PATCHED) + TRAILER
    try:
        ast.parse(source)
    except SyntaxError as e:
        raise SystemExit(
            f"kanban_wake_kinds patch: {RELATIVE} no longer parses after "
            f"patching: {e}"
        )
    path.write_text(source)
    print(f"kanban_wake_kinds patch: {RELATIVE} (1 anchor)")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
