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

This module supplies the missing text. It goes into **the completion message
the notifier already builds**, rather than a second message, and that is
deliberate: the existing send site is wrapped in the notifier's failure
counter, cursor rewind, and subscription-drop logic
(``gateway/kanban_watchers.py``). One message inherits all of it. A follow-up
``adapter.send()`` would sit outside that machinery, after the cursor has
advanced, and would need its own — a second failure path guarding the payload
that matters most.

The notifier's own clip gives way
---------------------------------
:func:`handoff_with_result` replaces the notifier's ``handoff`` rather than
appending to it, and it has to. Where the completion event carries no
``summary``, ``kanban_watchers.py`` builds the status line out of the very
field this module exists to deliver::

    elif task and task.result:
        r = _clip_handoff(task.result)
        handoff = f"\n{r}"

``delivered`` is then a 1200-character clip of ``result``, so asking whether
``result`` already appears inside it — the containment test
:func:`result_block` does — is asking whether a report fits inside its own
prefix. Under ``kanban_handoff_clip.DEFAULT_LIMIT`` it does, and the block
correctly stays empty. Over it, it never does: the message went out carrying
the first 1200 characters of the report, the ``[…]`` marker, a blank line,
and then the same report over again from the top. Measured on a 60-line cron
catalogue, jobs 1 to 19 arrived twice. Every result long enough to need this
module at all was delivered doubled, because ``RESULT_LIMIT`` is 30000
precisely for reports that outgrow a status line.

Appending cannot fix that — only the caller of the clip can decide the clip
was a mistake — so the hook returns the finished tail instead. When the status
line is merely a clipped prefix of the report, it is dropped and the report is
sent once, whole. That branch got *more* reachable, not less, when
``tools/kanban_result_required.py`` began folding a whitespace-only
``summary`` to ``None`` to stop ``complete_task`` indexing line zero of a
blank string and wedging the card.

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
    from gateway.kanban_handoff_clip import ELLIPSIS, clip_handoff
except ImportError:  # host-side unit tests: siblings in deploy/docker/patches
    from kanban_handoff_clip import ELLIPSIS, clip_handoff

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
    if delivered and normalised in _normalise(str(delivered)):
        return ""
    clipped = clip_handoff(body, limit)
    if len(clipped) < len(body):
        return SEPARATOR + clipped + CLIPPED_TAIL.format(limit=limit)
    return SEPARATOR + clipped


def _is_clipped_prefix_of(delivered: str, body: str) -> bool:
    """Whether ``delivered`` is just the opening of ``body``, possibly clipped.

    Written as a prefix test rather than an equality test against
    ``clip_handoff(body)`` so it still holds if the notifier's status line is
    built some other way. Upstream's own version of that line was a raw
    ``lines[0][:160]`` slice before the ``kanban_handoff_clip`` edit replaced
    it, and either shape is the same fact about the message: the reader has
    seen this text already, and is about to see all of it.
    """
    head = _normalise(delivered)
    marker = _normalise(ELLIPSIS)
    if marker and head.endswith(marker):
        head = head[: -len(marker)].rstrip()
    return bool(head) and _normalise(body).startswith(head)


def handoff_with_result(delivered: object, task: object) -> str:
    """Return the completion message's whole tail: status line and report.

    Replaces the notifier's ``handoff`` — see the module docstring for why
    appending to it cannot work. ``delivered`` is what the notifier built,
    ``task`` is whatever ``_kb.get_task`` returned, which is ``None`` for a row
    that vanished between the claim and the send.

    Fails to ``delivered`` unchanged rather than raising. This runs on the
    delivery path: a completion notification that loses its report is bad, one
    that raises, rewinds the cursor and re-sends forever is worse, and one that
    drops the status line it already had is worse again.
    """
    text = "" if delivered is None else str(delivered)
    try:
        result = getattr(task, "result", None)
        block = result_block(text, result)
        if not block:
            return text
        if _is_clipped_prefix_of(text, str(result).strip()):
            return block
        return text + block
    except Exception:  # pragma: no cover - defensive
        return text
