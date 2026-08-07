"""Deliver a completed card's ``result`` into the chat that asked for it.

Installed into the image at ``/opt/hermes/gateway/kanban_result_delivery.py``
and wired into ``gateway/kanban_watchers.py`` by
``deploy/docker/patches/apply_kanban_result_delivery.py``.

The companion patch ``tools/kanban_result_required.py`` makes sure a card
closes with its answer in ``result``. That makes the answer *durable* — it is
on the card and ``kanban_show`` returns it whole. It does not make it *arrive*,
because nothing upstream posts ``result`` to chat.

The chat line for a completed card is built from ``ev.payload["summary"]``,
which the kernel writes as (``hermes_cli/kanban_db.py``)::

    ev_summary = (summary if summary is not None else result) or ""
    ev_summary = ev_summary.strip().splitlines()[0][:400] if ev_summary else ""

One line, 400 characters. The summary channel is therefore not merely the wrong
place for a report — it is structurally incapable of carrying one. A worker
that does exactly what the schema now asks (status line in ``summary``, the
deliverable in ``result``) would still send a chat message announcing a
catalogue it never shows.

This module supplies the missing text. It is **appended to the completion
message the notifier already builds**, rather than sent as a second message,
and that is deliberate: the existing send site is wrapped in the notifier's
failure counter, cursor rewind, and subscription-drop logic
(``gateway/kanban_watchers.py``). One message inherits all of it. A follow-up
``adapter.send()`` would sit outside that machinery, after the cursor has
advanced, and would need its own — a second failure path guarding the payload
that matters most.

Length is safe on both platforms this harness ships to. The notifier calls
``adapter.send()`` directly, and ``send()`` chunks: the Slack adapter declares
``splits_long_messages = True`` with ``MAX_MESSAGE_LENGTH = 39000`` and splits
on code-block boundaries; the bundled ``google_chat`` adapter chunks at 4000.
``RESULT_LIMIT`` is well inside the smaller of those headrooms once the status
line and title are accounted for, and exists to bound a worker that dumps a log
rather than to fit a single message.
"""

from __future__ import annotations

try:  # in-image: both modules live in the gateway package
    from gateway.kanban_handoff_clip import clip_handoff
except ImportError:  # host-side unit tests: siblings in deploy/docker/patches
    from kanban_handoff_clip import clip_handoff

#: How much of ``result`` reaches chat. The status line's own budget is 1200
#: (``kanban_handoff_clip.DEFAULT_LIMIT``) because it is a status line; this is
#: the report and needs room. Sized far above the ~5.5 KB catalogue that card
#: t_8d1cf5cf should have delivered, and far enough below the 39000-character
#: Slack ceiling that the status line, the title, and the clip marker all fit
#: alongside it.
RESULT_LIMIT = 30000

#: Separates the status line from the report. A blank line is enough: the
#: status line is already on its own line under the ``✔ … done — <title>``
#: header, so the result reads as the body of the same message.
SEPARATOR = "\n\n"

CLIPPED_TAIL = (
    "\n\n[Result clipped at {limit} characters — ask for the full card "
    "to see the rest.]"
)


def _normalise(text: str) -> str:
    """Collapse whitespace and case, so two renderings of one report compare equal."""
    return " ".join(text.split()).casefold()


def result_block(
    delivered: object,
    result: object,
    limit: int = RESULT_LIMIT,
) -> str:
    """Return the text to append to a completion message, or ``""`` for none.

    ``delivered`` is the handoff the message already carries (the clipped
    status line). When the result is contained in it there is nothing new to
    say and a second copy is noise — which is exactly what happens when a
    worker puts one body of text in both fields, or when the require-result
    gate promoted ``summary`` into ``result`` to let a card close.
    """
    if result is None:
        return ""
    body = str(result).strip()
    if not body:
        return ""
    normalised = _normalise(body)
    if not normalised:
        return ""
    if delivered and normalised in _normalise(str(delivered)):
        return ""
    clipped = clip_handoff(body, limit)
    if len(clipped) < len(body):
        return SEPARATOR + clipped + CLIPPED_TAIL.format(limit=limit)
    return SEPARATOR + clipped


def result_block_for_task(delivered: object, task: object) -> str:
    """``result_block`` against a task row, tolerating a missing or odd row.

    The notifier holds ``task`` as whatever ``_kb.get_task`` returned, which is
    ``None`` for a row that vanished between the claim and the send. Called on
    the delivery path, so it fails to the empty string rather than raising: a
    completion notification that loses its report is bad, one that raises and
    rewinds the cursor forever is worse.
    """
    try:
        return result_block(delivered, getattr(task, "result", None))
    except Exception:  # pragma: no cover - defensive
        return ""
