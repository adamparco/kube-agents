#!/usr/bin/env python3
"""Wire tools/report_back_completion.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``, after
``apply_cron_run_scope.py`` — the import anchor below survives that patch, but
not the other way round, so the order in the Dockerfile is load-bearing.

Four anchored replacements in one file. Two install the gate; two rewrite the
tool schema that helped cause the bug, because a schema telling the model that
``result`` is a "legacy field" it should avoid will keep producing completions
the gate then has to reject. Prompt first, gate second.

Every anchor must be found the exact number of times expected and the file must
still parse, or the build fails rather than shipping a half-patched image. Why
each edit is needed is documented in the module docstring of
``deploy/docker/patches/report_back_completion.py``. Usage::

    python3 apply_report_back_completion.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# --- the import -------------------------------------------------------------

IMPORT_ANCHOR = "from hermes_cli.config import cfg_get, load_config"

IMPORT_PATCHED = (
    IMPORT_ANCHOR + "\n"
    "\n"
    "# kube-agents patch: see tools/report_back_completion.py\n"
    "from tools.report_back_completion import (\n"
    "    carries_content,\n"
    "    report_back_violation,\n"
    ")"
)

# --- the gate ---------------------------------------------------------------

# Sits between the goal-judge gate and the write, where ``task`` has been
# loaded but nothing has been mutated yet — so a rejection leaves the card
# in-flight and the worker free to retry, which is what its message promises.
COMPLETE_WRITE = (
    "            try:\n"
    "                ok = kb.complete_task(\n"
    "                    conn, tid,\n"
    "                    result=result, summary=summary, metadata=metadata,\n"
    "                    created_cards=created_cards,\n"
    "                    expected_run_id=_worker_run_id(tid),\n"
    "                )\n"
)

COMPLETE_WRITE_PATCHED = (
    "            # kube-agents patch: a card that asked for information must\n"
    "            # not close with none of it attached. Card t_7f3e0a5e closed\n"
    "            # done on 2026-08-05 with result_len 0 and a summary saying a\n"
    "            # manifest had been provided; the manifest was the worker's\n"
    "            # final chat message and reached nobody. See\n"
    "            # tools/report_back_completion.py.\n"
    "            if task is not None:\n"
    "                report_back_err = report_back_violation(\n"
    "                    title=task.title,\n"
    "                    body=task.body,\n"
    "                    summary=summary,\n"
    "                    result=result,\n"
    "                    metadata=metadata,\n"
    "                )\n"
    "                if report_back_err:\n"
    "                    # Only now is reading the comments worth a query:\n"
    "                    # every cheaper check has already failed. Content in\n"
    "                    # a comment is content on the card, so it counts.\n"
    "                    try:\n"
    "                        posted = [\n"
    "                            c.body for c in kb.list_comments(conn, tid)\n"
    "                        ]\n"
    "                    except Exception:\n"
    "                        logger.exception(\n"
    '                            "kanban_complete: comment read failed for %s",\n'
    "                            tid,\n"
    "                        )\n"
    "                        posted = []\n"
    "                    if not carries_content(*posted):\n"
    "                        return tool_error(report_back_err)\n"
    "\n" + COMPLETE_WRITE
)

# --- the schema that caused it ----------------------------------------------

# Upstream: "Short result log line (legacy field, maps to task.result). Use
# ``summary`` instead when possible". A model that follows this on a card
# asking for a report has nowhere correct to put the report.
RESULT_PROP = (
    '            "result": {\n'
    '                "type": "string",\n'
    '                "description": (\n'
    '                    "Short result log line (legacy field, maps to "\n'
    '                    "task.result). Use ``summary`` instead when "\n'
    '                    "possible; this exists for compatibility with "\n'
    '                    "callers that still set --result on the CLI."\n'
    "                ),\n"
    "            },\n"
)

RESULT_PROP_PATCHED = (
    '            "result": {\n'
    '                "type": "string",\n'
    '                "description": (\n'
    '                    "The deliverable itself, stored on the card as "\n'
    '                    "task.result. If this task asked you for "\n'
    '                    "information — a list, a report, an audit, an "\n'
    '                    "answer — put that content here, in full. Whoever "\n'
    '                    "asked reads the card; your final chat message is "\n'
    '                    "not on it and they will never see it. Put the "\n'
    '                    "answer here, not a description of the answer: "\n'
    '                    "\\"Provided a detailed manifest\\" is not a "\n'
    '                    "manifest. Omit only when the deliverable is a file "\n'
    '                    "(list it in ``artifacts``) or is already published "\n'
    '                    "somewhere (put that URL in ``summary``)."\n'
    "                ),\n"
    "            },\n"
)

MAIN_DESC = (
    '        "tests_run, decisions, findings, etc). At least one of "\n'
    '        "``summary`` or ``result`` is required. If you created new "\n'
)

MAIN_DESC_PATCHED = (
    '        "tests_run, decisions, findings, etc). At least one of "\n'
    '        "``summary`` or ``result`` is required — and if the task asked "\n'
    '        "you for information, the content belongs in ``result``, "\n'
    '        "because a summary describing a deliverable is not the "\n'
    '        "deliverable. If you created new "\n'
)

# (relative path, [(anchor, replacement, expected occurrences)])
PATCHES = (
    (
        "tools/kanban_tools.py",
        (
            (IMPORT_ANCHOR, IMPORT_PATCHED, 1),
            (COMPLETE_WRITE, COMPLETE_WRITE_PATCHED, 1),
            (RESULT_PROP, RESULT_PROP_PATCHED, 1),
            (MAIN_DESC, MAIN_DESC_PATCHED, 1),
        ),
    ),
)


def apply(root: Path) -> None:
    """Apply every patch under ``root``, or raise SystemExit with the reason."""
    for relative, edits in PATCHES:
        path = root / relative
        if not path.is_file():
            raise SystemExit(f"report_back_completion patch: {path} does not exist")
        source = path.read_text()
        for anchor, replacement, expected in edits:
            found = source.count(anchor)
            if found != expected:
                raise SystemExit(
                    f"report_back_completion patch: {relative}: expected "
                    f"{expected} occurrence(s) of anchor, found {found}. "
                    f"Upstream Hermes changed — re-derive the anchor before "
                    f"bumping the base image.\n--- anchor ---\n{anchor}"
                )
            source = source.replace(anchor, replacement)
        try:
            ast.parse(source)
        except SyntaxError as e:
            raise SystemExit(
                f"report_back_completion patch: {relative} no longer parses "
                f"after patching: {e}"
            )
        path.write_text(source)
        print(f"report_back_completion patch: {relative} ({len(edits)} anchors)")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
