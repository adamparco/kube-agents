"""Stop a card that asked for information closing with none attached.

Installed into the image at ``/opt/hermes/tools/report_back_completion.py`` and
wired into ``tools/kanban_tools.py`` by ``deploy/docker/Dockerfile``.

Observed in production on 2026-08-05. Card ``t_7f3e0a5e`` — "List enabled cron
jobs and scheduled audits", body "…Report back with their schedules, targets,
and active states" — ran for four minutes on the platform profile, produced a
correct and genuinely comprehensive manifest, and closed ``done`` having
delivered none of it. Its completion event reads::

    {"result_len": 0, "summary": "Successfully audited and cataloged all
     platform cron jobs, scheduled audits, background tasks, GitOps
     declarations, and GKE controller states. Provided a detailed manifest
     mapping schedules, targets, active states, and recent execution
     statuses."}

``result_len: 0``. The manifest itself was the worker's final chat message, so
it landed in the session log and nowhere else — not in ``task.result``, not in
a comment, not in an attachment. The requester reads the card and the
completion notification; both carried a summary *asserting* that a manifest had
been provided, with no manifest anywhere near them. The work was done twice:
once by the agent, once by the human who went and did it by hand.

Upstream permits this. ``_handle_complete`` gates only on
``if not (summary or result)``, so a summary alone always satisfies it, and the
tool schema actively steers away from the field that would have carried the
content — ``result`` is described as a "Short result log line (legacy field)…
Use ``summary`` instead when possible". A model following that advice on a
report-back card has nowhere correct to put the answer. The schema wording is
patched alongside this gate, because a gate that fires often is a worse fix
than a prompt that stops the mistake being made.

The rule
--------

Reject a completion when **all** of the following hold:

1. ``result`` is empty — nothing durable on the card,
2. no ``artifacts`` were declared and no other structured facts ride in
   ``metadata`` — the deliverable is not a file and not a data payload,
3. neither ``summary`` nor any comment on the card carries content of its own
   (see ``carries_content``) — nobody wrote the answer down anywhere,
4. and the card *asked* for information back, or the handoff *claims* to have
   supplied some.

All four together are the signature of the failure above. Any one of them
missing describes ordinary work: "fix the flaky test" → "Fixed by widening the
timeout" trips (1) and (2) and (3) but not (4), and completes untouched.

Termination
-----------

**Any non-empty ``result`` satisfies this gate.** That is deliberate and it is
the property that matters most here. A gate a worker cannot satisfy is worse
than the bug it guards, and a length or quality floor on ``result`` is exactly
that: a card whose honest answer is "no cron jobs are enabled" cannot clear a
600-character bar, and would retry until it hit the turn limit. So the gate
asks only that the answer be written to the card at all. Rejections carry a
message saying what to put there, which is where the quality comes from — the
existing ``HallucinatedCardsError`` and artifact-preservation rejections in
``kanban_tools.py`` work the same way, and models comply with an explicit
retry instruction far more reliably than with a threshold they must guess at.

Two properties of the surrounding code make that bound safe to rely on.
``_handle_complete`` has exactly one caller — the tool registry — so no
scheduler or auto-completer can be wedged by a rejection, only a model that can
read the message and retry. And ``hermes kanban complete`` on the CLI writes
through ``kb.complete_task`` directly, so a human closing a card by hand never
meets this gate at all.

Every predicate below fails open. A card that asks for nothing and a card the
matcher cannot read both complete exactly as they do today; the cost of a false
negative is one un-delivered report, and the cost of a false positive is a
worker wedged out of closing a card it has finished.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

#: A handoff at least this long is carrying its own content, whatever it says.
#: Sized above a 1-3 sentence summary (the incident's was 251 characters) and
#: below any real report — the manifest that should have been on the card was
#: roughly 4 KB.
CONTENT_MIN_CHARS = 600

#: …or this many list-shaped lines, which is a report of any length.
CONTENT_MIN_LIST_LINES = 3

#: Metadata keys the kernel stamps itself. Their presence says nothing about
#: whether the worker attached anything, so they do not count as a payload.
#: ``worker_session_id`` is added by ``_stamp_worker_session_metadata``.
STAMPED_METADATA_KEYS = frozenset({"worker_session_id"})

# --- did the card ask for information back? ---------------------------------

# Matched against title + body. Deliberately broad: on its own this predicate
# decides nothing, because the gate also requires an empty result, no
# artifacts, and no content anywhere in the handoff.
_REQUESTS_REPORT = re.compile(
    r"""
      \breport(?:s|ing)?\s+back\b
    | \breport\s+(?:on|with)\b
    | \blist\b                      # \b keeps "blacklist" and "listing" out
    | \bwhat\s+(?:are|is|was|were)\b
    | \bhow\s+(?:many|much)\b
    | \b(?:tell|show|give|send)\s+(?:me|us)\b
    | \blet\s+(?:me|us)\s+know\b
    | \bsummar(?:y|ise|ize)\b
    | \benumerate\b
    | \bcatalog(?:ue)?\b
    | \binventory\b
    | \baudit\b
    | \binvestigate\b
    | \banaly[sz]e\b
    | \bidentify\b
    | \bfind\s+out\b
    | \bwrite\s+up\b
    | \bbreakdown\b
    | \bprovide\s+(?:a|an|the)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# --- does the handoff claim a deliverable it did not attach? ----------------

# Matched against summary + result. These are the phrases of a handoff that
# points at content: either deictically ("the following", "see below") or by
# assertion ("provided a detailed manifest" — the incident, verbatim). If the
# text carrying them also carries no content, the thing pointed at is missing.
_PROMISES_DELIVERABLE = re.compile(
    r"""
      \bprovided?\s+(?:a|an|the)\b
    | \b(?:see|listed|outlined|detailed|documented|shown|described)\s+below\b
    | \bbelow\s+(?:is|are)\b
    | \bthe\s+following\b
    | \bas\s+follows\b
    | \b(?:full|complete|detailed|comprehensive|itemi[sz]ed)\s+
      (?:list|report|manifest|breakdown|inventory|catalog(?:ue)?
        |audit|summary|mapping|analysis|rundown)\b
    | \bsee\s+attached\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# --- does a piece of text carry content of its own? -------------------------

#: A bullet, a number, a table row, or a "Key: value" line. Three of them are a
#: report; one is a sentence that happens to start with a dash.
_LIST_LINE = re.compile(
    r"^\s*(?:[-*•+]\s+|\d+[.)]\s+|\|)|\S.*\|.*\S|^\s*\S[^:\n]{0,60}:\s+\S",
)

#: A URL or an absolute path — the answer is elsewhere, and this says where.
_POINTS_SOMEWHERE = re.compile(
    r"https?://\S|(?<![\w.])/(?:[\w.@-]+/){1,}[\w.@-]+",
)


def _text(value: object) -> str:
    """Coerce a possibly-None tool argument to strippable text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def carries_content(*texts: object) -> bool:
    """True when any of ``texts`` is plausibly the answer rather than a label.

    Three ways to qualify, any one of which is enough:

    * length — past ``CONTENT_MIN_CHARS`` nobody is writing a 1-3 sentence
      handoff, they are writing the report;
    * shape — three or more bullet, numbered, table, or ``Key: value`` lines;
    * a pointer — a URL or absolute path, which is a legitimate handoff for
      work whose deliverable was published to an issue, a PR, or a file.

    A short prose answer ("no cron jobs are enabled") qualifies under none of
    them, which is why this predicate never gates ``result``: it decides only
    whether a *summary* or *comment* has already made the gate unnecessary.
    """
    for value in texts:
        text = _text(value)
        if not text:
            continue
        if len(text) >= CONTENT_MIN_CHARS:
            return True
        if _POINTS_SOMEWHERE.search(text):
            return True
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if sum(1 for ln in lines if _LIST_LINE.search(ln)) >= CONTENT_MIN_LIST_LINES:
            return True
    return False


def declares_payload(metadata: object) -> bool:
    """True when ``metadata`` carries worker-supplied facts or artifacts.

    ``artifacts`` is folded into ``metadata`` by ``_handle_complete`` before
    this runs, so both the "here is the file" and the "here are the structured
    findings" handoffs are covered by the one check. Keys the kernel stamps
    itself are ignored — see ``STAMPED_METADATA_KEYS``.

    ``_handle_complete`` rejects a non-dict ``metadata`` before this runs, so
    the type check here is belt-and-braces for direct callers and tests.
    """
    if not isinstance(metadata, dict):
        return False
    for key, value in metadata.items():
        if key in STAMPED_METADATA_KEYS:
            continue
        if value in (None, "", [], {}, ()):
            continue
        return True
    return False


def requests_report(title: object, body: object) -> bool:
    """True when the card asked for information to come back."""
    return bool(_REQUESTS_REPORT.search(f"{_text(title)}\n{_text(body)}"))


def promises_deliverable(summary: object, result: object) -> bool:
    """True when the handoff points at content it should have carried."""
    return bool(_PROMISES_DELIVERABLE.search(f"{_text(summary)}\n{_text(result)}"))


def report_back_violation(
    *,
    title: object = "",
    body: object = "",
    summary: object = "",
    result: object = "",
    metadata: object = None,
    comments: Iterable[object] = (),
) -> Optional[str]:
    """Return a rejection message, or ``None`` when the completion may proceed.

    ``comments`` is accepted lazily on purpose. Reading a card's comments costs
    a query on every completion, and the answer only ever matters for the small
    minority of handoffs that have already failed every cheaper check — so the
    call site passes them in only once this function has been asked without
    them and said no. Callers that cannot read comments may omit them; the
    worst case is a rejection a comment would have prevented, and the message
    tells the worker how to clear it.
    """
    try:
        if _text(result):
            return None  # Something durable is on the card. Always enough.
        if declares_payload(metadata):
            return None
        if carries_content(summary, *comments):
            return None
        promised = promises_deliverable(summary, result)
        if not (promised or requests_report(title, body)):
            return None
        return _message(promised)
    except Exception:  # noqa: BLE001 — never wedge a worker over a heuristic
        return None


def _message(promised: bool) -> str:
    """The rejection a worker reads. Says what is wrong and what to send.

    Modelled on the ``HallucinatedCardsError`` rejection alongside it: name the
    block, state that no state changed, and give the retry. The middle sentence
    is the one that matters — a model that believes its final chat message is
    the deliverable has no reason to look for another channel unless told the
    requester cannot see it.
    """
    lede = (
        "your handoff says you provided one but nothing is attached"
        if promised
        else "this card asked for information to be reported back and the "
        "handoff carries none"
    )
    return (
        f"kanban_complete blocked: {lede}. `result` is empty, no `artifacts` "
        f"were declared, and no comment on the card carries the content. Your "
        f"final chat message is not on the card — it goes to the session log, "
        f"and the person who asked reads the card. Your task is still "
        f"in-flight (no state change). Retry kanban_complete with the answer "
        f"itself in `result`: the actual list, table, or report, not a "
        f"description of one. If the deliverable is a file, pass its absolute "
        f"path in `artifacts`. If you already published it to an issue, PR, or "
        f"document, put that URL in `summary`. Keep `summary` as your 1-3 "
        f"sentence handoff either way."
    )
