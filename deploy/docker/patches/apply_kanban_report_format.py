#!/usr/bin/env python3
"""Wire tools/kanban_report_format.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. One edit: the card
body a worker is handed at ``kanban_create`` gains the report-format stanza when
it carries no format instructions of its own — the variant written for the chat
platform the finished report will be delivered to, or the conservative one when
there is no chat session behind the card.

Why the body and not the persona: the persona is read once at the top of a run
and then competes with several thousand tokens of immediate task text, which is
why the fan-out of 2026-08-08 produced four different formats from one persona.
The card body is the task text. Every rule the stanza states is one the detector
in ``kanban_report_format`` can measure and the notifier may log about, so a
report is never complained about for a rule it was not given. Nothing refuses a
completion over shape — the stanza is an instruction, not a gate.

The anchor is the two-line slice that reads ``body`` and moves on to
``parents``. ``body = args.get("body")`` alone recurs (``_handle_update`` reads
it too), and the handler is not a call site ``find_call`` can select on, so the
pair is the narrowest thing that names *this* read. The insertion point for the
import is chosen the way ``apply_kanban_result_required.py`` chooses its own --
by asking for the statement that registers ``kanban_create``, which survives a
reformat that a line-slice would not.

Usage::

    python3 apply_kanban_report_format.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib  # noqa: E402

RELATIVE = "tools/kanban_tools.py"

#: The tool whose registration the import has to precede.
REGISTERED_TOOL = "kanban_create"

OLD_BODY_READ = '''    body = args.get("body")
    parents = args.get("parents") or []
'''

# ``_report_format_platform()`` is read here rather than inside
# ``with_report_format`` so the stanza chooser stays a pure function of its
# arguments. It resolves the same ``HERMES_SESSION_PLATFORM`` ContextVar that
# ``_maybe_auto_subscribe`` uses further down this handler to decide where the
# completion notification goes — so the stanza is written for the platform the
# report will actually be delivered to, not for a guess. Off a chat session it
# returns "" and the conservative stanza is used, which is the pre-patch text.
NEW_BODY_READ = '''    body = args.get("body")
    # kube-agents patch: see tools/kanban_report_format.py
    body = _with_report_format(body, platform=_report_format_platform())
    parents = args.get("parents") or []
'''

# Imported at module scope rather than inside the handler: an ImportError has to
# surface when the module loads and the build's verify can see it, not on the
# first card created in production. ``current_platform`` itself defers its
# ``gateway`` import to call time — see its docstring for why ``tools`` must not
# grow a module-scope dependency on ``gateway``.
REGISTER_PREAMBLE = (
    "# kube-agents patch: see tools/kanban_report_format.py\n"
    "from tools.kanban_report_format import with_report_format as _with_report_format\n"
    "from tools.kanban_report_format import current_platform as _report_format_platform\n"
    "\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix="kanban_report_format")

    patch.substitute(OLD_BODY_READ, NEW_BODY_READ, label="create body")

    site = patch.find_call(
        "registry.register",
        label=f"{REGISTERED_TOOL} registration",
        name=REGISTERED_TOOL,
    )
    # The handler assertion is the one that matters: the edit above is inside
    # `_handle_create`, so a registration wired to some other function would mean
    # the stanza is being appended on a path nothing reaches.
    site.expect(toolset="kanban", handler=patchlib.Ident("_handle_create"))
    patch.insert(site.start, REGISTER_PREAMBLE)

    patch.commit("1 anchor + 1 registration")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
