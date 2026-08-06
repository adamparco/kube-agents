"""URL-safe clipping for the kanban notifier's completion handoff.

Installed into the image at ``/opt/hermes/gateway/kanban_handoff_clip.py`` and
wired into ``gateway/kanban_watchers.py`` by ``deploy/docker/Dockerfile``.

Upstream builds the chat line for a completed card as::

    lines = payload_summary.strip().splitlines()
    h = lines[0][:200] if lines else payload_summary[:200]

Two things go wrong with that, and both were observed in production on
2026-08-03:

1. **The hard 200-character slice cuts mid-token.** A workload-reliability
   audit reported "… The audit ledger has been updated at
   https://github.com/gke-agentic/adamparco-infra/is" — the delivered message
   was exactly 200 characters, and `/issues/30` had been sliced down to `/is`.
   The link the whole report pointed at arrived broken.
2. **Everything after the first line is discarded**, so a worker that writes a
   perfectly good multi-line summary sees all but its opening line vanish.

``clip_handoff`` raises the budget well past anything the notifier is handed
and, if it ever does have to clip, clips on a whitespace boundary so the text
ends at a whole token — a URL survives intact or is dropped entirely, never
truncated into a dead link.

What this patch does **not** reach, and what the numbers above are easy to
misread as: the notifier is not the first thing to cut the summary.
``hermes_cli/kanban_db.py`` stores the completion event as
``summary.strip().splitlines()[0][:400]``, and ``ev.payload["summary"]`` is all
the notifier ever reads. So by the time ``clip_handoff`` sees the text it is
already one line of at most 400 characters and the 1200-character budget never
binds — the real effect of this patch is that all 400 of those characters now
survive instead of 200. The upstream 400-character cut is still a hard slice
that can sever a URL; a summary has to carry its link inside that budget, which
is what ``agents/platform/SOUL.md`` §0 tells the Platform Agent. The
``task.result`` slice is patched for symmetry only: ``ev_summary`` falls back to
``result``, so ``payload_summary`` is truthy whenever a result exists and the
``elif`` branch is unreachable in practice.

A deliverable therefore cannot travel in ``summary`` at all. It travels in
``result``, which ``gateway/kanban_result_delivery.py`` posts as a follow-up
message.
"""

from __future__ import annotations

# Generous enough that a real audit summary is delivered whole; small enough
# that a runaway summary cannot flood the channel.
DEFAULT_LIMIT = 1200

ELLIPSIS = " […]"


def clip_handoff(text: object, limit: int = DEFAULT_LIMIT) -> str:
    """Return ``text`` clipped to ``limit`` characters without severing a token.

    Blank input yields an empty string. Text within budget is returned with only
    surrounding whitespace stripped. Over budget, the text is cut back to the
    last whitespace that fits and ``[…]`` is appended, so the final token is
    always whole — which is what keeps a trailing URL clickable.
    """
    if text is None:
        return ""
    body = str(text).strip()
    if limit <= 0 or len(body) <= limit:
        return body

    # Too small to hold a character and the marker: the marker is what gives
    # way, not the budget. `max(1, limit - 4)` kept one character and then
    # appended four more, so `limit=1` returned five — a clip that overshot the
    # only number it was given.
    if limit <= len(ELLIPSIS):
        return body[:limit].rstrip()

    head = body[: limit - len(ELLIPSIS)]
    cut = max(head.rfind(" "), head.rfind("\n"), head.rfind("\t"))
    if cut > 0:
        head = head[:cut]
    # No whitespace to cut on (one enormous token): a hard cut is the only
    # option left, and there is no partial-URL risk worth preserving in a token
    # that long.
    return head.rstrip() + ELLIPSIS
