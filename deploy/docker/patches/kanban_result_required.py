"""Make ``result`` the single field that carries a completed card's answer.

Installed into the image at ``/opt/hermes/tools/kanban_result_required.py`` and
wired into ``tools/kanban_tools.py`` by
``deploy/docker/patches/apply_kanban_result_required.py``.

The companion patch ``gateway/kanban_result_delivery.py`` posts ``result`` into
the chat thread. This one makes sure there is something in it to post.

Why
---
On 2026-08-07 a user asked which platform cron jobs were enabled. Three cards
ran (``t_8d1cf5cf``, ``t_97f721b6``, ``t_68fbd0b6``); all three closed ``done``;
the user never got the list. Dumping the board afterwards showed
``tasks.result IS NULL`` and ``result_len: 0`` on every one of them. The workers
had built the catalogue — it is still sitting in
``/opt/data/kanban/logs/t_8d1cf5cf.log``, 5.5 KB of it — printed it to their own
stdout, and then dropped it on the floor at the ``kanban_complete`` call.

They were doing what they were told. Upstream's own tool schema describes
``result`` as::

    Short result log line (legacy field, maps to task.result). Use ``summary``
    instead when possible; this exists for compatibility with callers that
    still set --result on the CLI.

and ``summary`` as "Human-readable handoff, 1-3 sentences", while the gate below
it accepts ``summary or result``. So the model is told the only field that can
carry a report is legacy, told to prefer a field documented as 1-3 sentences,
and then allowed to close the card with just that. A worker holding a 5.5 KB
answer follows the schema and throws it away.

Worse, ``summary`` cannot carry a report even if a worker tries: the kernel
writes the completion event as (``hermes_cli/kanban_db.py``)::

    ev_summary = (summary if summary is not None else result) or ""
    ev_summary = ev_summary.strip().splitlines()[0][:400] if ev_summary else ""

First line only, 400 characters, no ellipsis. The chat notifier reads that field
and nothing else, so a multi-line summary loses everything after line one
silently.

What this changes
-----------------
Two things, both deterministic — there is no attempt to classify a card as
"asked for information" or not. Every card has an answer, even if the answer is
one line ("Restarted the deployment; 3/3 pods ready"), so every card is
required to carry one.

1. **Schema wording.** ``result`` becomes the required field that carries the
   deliverable and says it is posted to the requester verbatim; ``summary``
   becomes an explicitly one-line, 400-character status header. A prompt that
   prevents the mistake is worth more than a gate that rejects it.

2. **The gate.** Upstream's ``if not (summary or result)`` becomes a check that
   ``result`` carries something.

Never wedging the card
----------------------
A gate that can refuse forever is a worse bug than the one it fixes, so the
refusal fires **once per task**. The first content-free completion is rejected
with an instruction; a second one is accepted, promoting ``summary`` into
``result`` so the card still closes and still carries the best text available.
One nudge is what a model needs to correct a tool call, and the card is
guaranteed to reach ``done`` either way.

Scope: the gate lives in the ``kanban_complete`` tool handler, which is reached
only by a worker's own tool call. The CLI and the scheduler write through
``kb.complete_task`` directly and are unaffected, so no cron run and no human
can be blocked by it.
"""

from __future__ import annotations

# Task ids already refused once. Keyed by id rather than a bare flag because a
# worker process is nominally one task, but nothing in the tool contract
# promises that, and a shared process must not spend task B's only nudge on
# task A.
_nudged: set[str] = set()

MISSING_RESULT_ERROR = (
    "result is required and was empty. `result` is what the person who asked "
    "actually receives — the gateway posts it into their chat thread verbatim. "
    "Call kanban_complete again with the full answer to this card in `result`: "
    "the list, the report, the findings, the numbers, whatever was asked for, "
    "complete and not summarised away. Do not leave it only in your transcript, "
    "in a file, or in a comment — none of those reach the user. Keep `summary` "
    "as the one-line status header."
)


def require_result(
    task_id: object,
    summary: object,
    result: object,
) -> tuple[str | None, object]:
    """Validate a completion's ``result``.

    Returns ``(error, result_to_store)``. ``error`` is ``None`` when the
    completion may proceed; otherwise it is the text to hand back to the worker
    and the completion must not be written.

    A non-empty ``result`` always passes — there is no length or quality floor,
    because a card whose honest answer is one line must be able to close. An
    empty one is refused the first time and, on the second attempt for the same
    task, accepted with ``summary`` promoted into ``result`` so the card cannot
    be wedged shut by a worker that will not fill the field in.
    """
    if result is not None and str(result).strip():
        return None, result

    key = str(task_id)
    if key not in _nudged:
        _nudged.add(key)
        return MISSING_RESULT_ERROR, result

    # Second attempt: take what we can get rather than hold the card open.
    # ``summary`` may itself be empty, in which case the card closes with an
    # empty result exactly as upstream would have allowed.
    return None, summary if (summary is not None and str(summary).strip()) else result


# --- Schema wording ---------------------------------------------------------
# Applied to the live ``KANBAN_COMPLETE_SCHEMA`` dict at import time rather than
# by rewriting the string literals in place. Upstream builds each description
# from adjacent string literals wrapped at some arbitrary column, so a textual
# anchor would break on a reflow that changed nothing semantic. Matching against
# the assembled value is stable under rewrapping and still fails loudly when the
# wording itself changes.
#
# Each OLD string is an exact copy of upstream's assembled text; ``apply_schema``
# raises if one stops matching, which is the signal to re-derive it against the
# new base image.

OLD_TOOL_DESCRIPTION = (
    "Mark your current task done with a structured handoff for "
    "downstream workers and humans. Prefer ``summary`` for a "
    "human-readable 1-3 sentence description of what you did; put "
    "machine-readable facts in ``metadata`` (changed_files, "
    "tests_run, decisions, findings, etc). At least one of "
    "``summary`` or ``result`` is required. "
)

NEW_TOOL_DESCRIPTION = (
    "Mark your current task done. ``result`` is REQUIRED and carries the "
    "answer: whatever this card asked for goes there in full, and the "
    "gateway posts it verbatim into the chat thread of the person who "
    "asked. ``summary`` is a one-line status header, NOT the report — only "
    "its first line survives and only the first 400 characters of that, so "
    "anything you put there and nowhere else is lost. Put machine-readable "
    "facts in ``metadata`` (changed_files, tests_run, decisions, findings, "
    "etc). "
)

OLD_SUMMARY_DESCRIPTION = (
    "Human-readable handoff, 1-3 sentences. Appears in "
    "Run History on the dashboard and in downstream "
    "workers' context."
)

NEW_SUMMARY_DESCRIPTION = (
    "One-line status header: what happened, in a single "
    "sentence. Only the FIRST LINE reaches chat, and only "
    "its first 400 characters — the kernel cuts the rest "
    "with no ellipsis. Never put the deliverable here; it "
    "goes in ``result``. Appears in Run History on the "
    "dashboard and in downstream workers' context."
)

OLD_RESULT_DESCRIPTION = (
    "Short result log line (legacy field, maps to "
    "task.result). Use ``summary`` instead when "
    "possible; this exists for compatibility with "
    "callers that still set --result on the CLI."
)

NEW_RESULT_DESCRIPTION = (
    "REQUIRED. The complete answer to this card — the "
    "list, the report, the findings, the RCA, the "
    "numbers. Not truncated and not reduced to one line: "
    "the gateway posts this verbatim into the chat thread "
    "of the person who asked, so it is the only thing they "
    "actually receive. If the card asked a question, the "
    "whole answer goes here. Do not summarise it away, and "
    "do not leave it only in your transcript, in a file, or "
    "in a comment — none of those reach the user."
)

OLD_GATE = (
    "    if not (summary or result):\n"
    "        return tool_error(\n"
    "            \"provide at least one of: summary (preferred), result\"\n"
    "        )\n"
)

NEW_GATE = (
    "    # kube-agents patch: see tools/kanban_result_required.py\n"
    "    _result_err, result = _require_result(tid, summary, result)\n"
    "    if _result_err:\n"
    "        return tool_error(_result_err)\n"
)


def _swap(schema: dict, path: tuple[str, ...], old: str, new: str) -> None:
    """Replace ``old`` with ``new`` at ``path`` in ``schema``, or raise.

    ``old`` is matched as a substring so the tool description — whose tail
    documents ``created_cards`` and ``artifacts`` and is left alone — can have
    its opening paragraph swapped without restating the rest.
    """
    node: object = schema
    for key in path[:-1]:
        if not isinstance(node, dict) or key not in node:
            raise KeyError(
                f"kanban_result_required: {'.'.join(path)} missing from the "
                f"kanban_complete schema. Upstream Hermes changed — re-derive "
                f"the schema patch before bumping the base image."
            )
        node = node[key]
    leaf = path[-1]
    if not isinstance(node, dict) or leaf not in node:
        raise KeyError(
            f"kanban_result_required: {'.'.join(path)} missing from the "
            f"kanban_complete schema. Upstream Hermes changed — re-derive the "
            f"schema patch before bumping the base image."
        )
    current = node[leaf]
    if not isinstance(current, str) or old not in current:
        raise ValueError(
            f"kanban_result_required: {'.'.join(path)} does not contain the "
            f"expected upstream wording. Upstream Hermes changed — re-derive "
            f"the schema patch before bumping the base image.\n"
            f"--- expected to find ---\n{old}\n--- actual ---\n{current!r}"
        )
    node[leaf] = current.replace(old, new)


def apply_schema(schema: dict) -> dict:
    """Rewrite ``kanban_complete``'s schema in place and return it.

    Called from ``tools/kanban_tools.py`` immediately before the tool is
    registered, so the corrected wording is what the registry sees regardless of
    whether ``registry.register`` stores the dict by reference or copies it.
    """
    _swap(schema, ("description",), OLD_TOOL_DESCRIPTION, NEW_TOOL_DESCRIPTION)
    props = ("parameters", "properties")
    _swap(
        schema, props + ("summary", "description"),
        OLD_SUMMARY_DESCRIPTION, NEW_SUMMARY_DESCRIPTION,
    )
    _swap(
        schema, props + ("result", "description"),
        OLD_RESULT_DESCRIPTION, NEW_RESULT_DESCRIPTION,
    )
    # Advertise the requirement in the place a model is most likely to honour
    # it. The handler enforces it either way; this stops a well-behaved caller
    # from having to be corrected at all.
    params = schema.get("parameters")
    if isinstance(params, dict):
        required = params.get("required")
        if isinstance(required, list) and "result" not in required:
            required.append("result")
    return schema
