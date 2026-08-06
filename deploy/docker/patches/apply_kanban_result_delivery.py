#!/usr/bin/env python3
"""Wire gateway/kanban_result_delivery.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. Independent of the
other appliers: it edits ``gateway/kanban_watchers.py``, which
``apply_cron_run_scope.py`` and ``apply_report_back_completion.py`` do not
touch. It does share the file with the inline ``kanban_handoff_clip`` step
above it, but on disjoint anchors — that step rewrites the two length slices
and appends an import at the end of the file; this one inserts a delivery block
and an import near the top. Either order applies.

Two anchored replacements in one file: the import, and the send that was
missing. Every anchor must be found the exact number of times expected and the
file must still parse, or the build fails rather than shipping a half-patched
image. Why the edit is needed is documented in the module docstring of
``deploy/docker/patches/kanban_result_delivery.py``. Usage::

    python3 apply_kanban_result_delivery.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# --- the import -------------------------------------------------------------

IMPORT_ANCHOR = "from agent.i18n import t"

IMPORT_PATCHED = (
    IMPORT_ANCHOR + "\n"
    "\n"
    "# kube-agents patch: see gateway/kanban_result_delivery.py\n"
    "from gateway.kanban_result_delivery import (\n"
    "    deliver_result as _deliver_kanban_result,\n"
    ")"
)

# --- the send that was missing ----------------------------------------------

# Sits between the completion line and the artifact upload, so the chat reads
# status → report → attachments. Its own ``if kind == "completed"`` rather than
# a branch nested in the artifact one: a failure on either side must not skip
# the other, and each already isolates its own.
ARTIFACT_BLOCK = (
    "                            # After delivering the text notification, surface\n"
    "                            # any artifact paths the worker referenced in\n"
    "                            # ``kanban_complete(summary=..., artifacts=[...])``\n"
    "                            # (or the legacy ``result`` field) as native\n"
    "                            # uploads. ``extract_local_files`` finds bare\n"
    "                            # absolute paths in the summary;\n"
    "                            # ``send_document`` / ``send_image_file`` uploads\n"
    "                            # them. Only fires on the ``completed`` event so\n"
    "                            # we never spam attachments on retries.\n"
    '                            if kind == "completed":\n'
)

RESULT_BLOCK = (
    "                            # kube-agents patch: the line just sent carries\n"
    "                            # the run's ``summary`` — a status line. When\n"
    "                            # the card asked for information the answer is\n"
    "                            # in ``result`` (see\n"
    "                            # tools/report_back_completion.py), which\n"
    "                            # upstream reads only for legacy rows that have\n"
    "                            # no summary at all, so the answer never reaches\n"
    "                            # the person who asked. Post it as a follow-up,\n"
    "                            # unless the line above already carried it.\n"
    "                            # ``handoff`` is what that line actually showed.\n"
    "                            # See gateway/kanban_result_delivery.py.\n"
    '                            if kind == "completed":\n'
    "                                try:\n"
    "                                    await _deliver_kanban_result(\n"
    "                                        adapter=adapter,\n"
    '                                        chat_id=sub["chat_id"],\n'
    "                                        metadata=metadata,\n"
    '                                        task_id=sub["task_id"],\n'
    "                                        delivered=handoff,\n"
    "                                        task=task,\n"
    "                                    )\n"
    "                                except Exception as res_exc:\n"
    "                                    logger.debug(\n"
    '                                        "kanban notifier: result delivery for %s failed: %s",\n'
    '                                        sub["task_id"], res_exc,\n'
    "                                    )\n"
)

ARTIFACT_BLOCK_PATCHED = RESULT_BLOCK + ARTIFACT_BLOCK

# (relative path, [(anchor, replacement, expected occurrences)])
PATCHES = (
    (
        "gateway/kanban_watchers.py",
        (
            (IMPORT_ANCHOR, IMPORT_PATCHED, 1),
            (ARTIFACT_BLOCK, ARTIFACT_BLOCK_PATCHED, 1),
        ),
    ),
)


def apply(root: Path) -> None:
    """Apply every patch under ``root``, or raise SystemExit with the reason."""
    for relative, edits in PATCHES:
        path = root / relative
        if not path.is_file():
            raise SystemExit(f"kanban_result_delivery patch: {path} does not exist")
        source = path.read_text()
        for anchor, replacement, expected in edits:
            found = source.count(anchor)
            if found != expected:
                raise SystemExit(
                    f"kanban_result_delivery patch: {relative}: expected "
                    f"{expected} occurrence(s) of anchor, found {found}. "
                    f"Upstream Hermes changed — re-derive the anchor before "
                    f"bumping the base image.\n--- anchor ---\n{anchor}"
                )
            source = source.replace(anchor, replacement)
        try:
            ast.parse(source)
        except SyntaxError as e:
            raise SystemExit(
                f"kanban_result_delivery patch: {relative} no longer parses "
                f"after patching: {e}"
            )
        path.write_text(source)
        print(f"kanban_result_delivery patch: {relative} ({len(edits)} anchors)")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
