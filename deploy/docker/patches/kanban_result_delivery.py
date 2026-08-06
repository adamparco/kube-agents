"""Deliver a completed card's ``result`` into the chat that asked for it.

Installed into the image at ``/opt/hermes/gateway/kanban_result_delivery.py``
and wired into ``gateway/kanban_watchers.py`` by
``deploy/docker/patches/apply_kanban_result_delivery.py``.

The companion patch ``tools/report_back_completion.py`` stops a card that asked
for information closing with none of it attached. That makes the answer
*durable* — it is on the card, and ``kanban_show`` returns it in full. It does
not make it *arrive*, because nothing upstream posts ``result`` to chat.

The chat line comes from ``ev.payload["summary"]``, and that payload is written
by ``hermes_cli/kanban_db.py`` as::

    ev_summary = (summary if summary is not None else result) or ""
    ev_summary = ev_summary.strip().splitlines()[0][:400] if ev_summary else ""

One line, 400 characters. So the summary channel is not merely the wrong place
for a report — it is structurally incapable of carrying one, and a worker doing
exactly what the gate now demands (status line in ``summary``, deliverable in
``result``) sends a chat message announcing a manifest it never shows. That is
the 2026-08-05 incident with one step removed: the report is recoverable rather
than lost, but the person who asked still has to go and fetch it, and on
2026-08-05 they instead compiled it by hand.

Note the ``summary if summary is not None else result`` fallback: a worker that
sets only ``result`` gets its report's *first line* as the chat line, which is
why the notifier's ``elif task and task.result`` branch never fires and why the
comparison below is against what was actually shown rather than against
"whether a summary existed".

This module supplies the missing second message. Once the completion line has
been delivered, the notifier posts ``result`` itself — unless the line it just
sent already carried the same text, which is what happens when a worker puts
one body of text in both fields.

Delivery-side length is not a problem on the platform this harness ships to:
the notifier calls ``adapter.send()`` directly rather than going through
``gateway/delivery.py``, and the bundled ``google_chat`` adapter's ``send()``
chunks anything over its 4000-character limit into follow-on messages. The
budget below exists to bound a runaway result, not to fit one message.
"""

from __future__ import annotations

import logging

try:  # in-image: both modules live in the gateway package
    from gateway.kanban_handoff_clip import clip_handoff
except ImportError:  # host-side unit tests: siblings in deploy/docker/patches
    from kanban_handoff_clip import clip_handoff

logger = logging.getLogger("gateway.run")

#: How much of ``result`` reaches chat. The completion line's own budget is
#: 1200 (``kanban_handoff_clip.DEFAULT_LIMIT``) because it is a status line;
#: this is a report and needs room. Sized above the ~4 KB manifest that card
#: t_7f3e0a5e should have delivered, so that deliverable arrives whole, and far
#: enough below "unbounded" that a worker dumping a log cannot flood the space.
RESULT_LIMIT = 6000

HEADER = "📄 Kanban {task_id} — result:\n\n"

#: Only appended when the body was actually cut. ``kanban_show`` is a tool the
#: user cannot call, so the pointer is phrased as something they can ask for.
CLIPPED_TAIL = "\n\n(Clipped — ask for the full card to see the rest.)"


def _normalise(text: str) -> str:
    """Collapse whitespace and case, so two renderings of one report compare equal."""
    return " ".join(text.split()).casefold()


def result_message(
    task_id: object,
    delivered: object,
    result: object,
    limit: int = RESULT_LIMIT,
) -> str:
    """Return the follow-up message carrying ``result``, or ``""`` to send none.

    ``delivered`` is the handoff the completion line already carried. When the
    result is contained in it there is nothing new to say and a second copy is
    noise; when the line was clipped short of the whole result, the fuller text
    is worth the extra message.
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
    message = HEADER.format(task_id=task_id) + clipped
    if len(clipped) < len(body):
        message += CLIPPED_TAIL
    return message


async def deliver_result(
    adapter: object,
    chat_id: str,
    metadata: object,
    task_id: object,
    delivered: object,
    task: object,
) -> bool:
    """Post a completed card's ``result``. True when a message was sent.

    Mirrors ``_deliver_kanban_artifacts``: the caller wraps the call, because
    the completion notification has already gone out and the cursor is about to
    advance — a failure here must not wedge the tick or re-send the primary
    notification on the next one.
    """
    message = result_message(task_id, delivered, getattr(task, "result", None))
    if not message:
        return False
    await adapter.send(chat_id, message, metadata=metadata)
    logger.debug("kanban notifier: delivered result for %s (%d chars)", task_id, len(message))
    return True
