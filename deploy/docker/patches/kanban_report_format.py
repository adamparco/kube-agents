"""One home for what a card's ``result`` is supposed to look like.

Installed into the image at ``/opt/hermes/tools/kanban_report_format.py``. Two
callers wire it in:

``apply_kanban_report_format.py``
    appends :data:`REPORT_FORMAT_STANZA` to a new card's ``body`` at
    ``kanban_create``, so the worker is told the shape before it starts.

``gateway/kanban_notifier.py``
    reads :func:`result_shape_defects` and :func:`serious_defects` to record the
    shape of a report as it is delivered — a WARNING naming the edit to make for
    the two defects that always render wrongly, INFO for the two that are
    matters of taste. It imports this module lazily and inside a blanket
    ``except``, so a mistake here can never wedge the delivery path.

Nothing refuses a completion over shape. ``kanban_result_required.py`` briefly
did, and removing it is why :func:`serious_defects` is named for severity rather
than for a gate; that module's docstring records the measurement that settled it.
The predicate lives here rather than in its callers because the stanza, the
schema wording and the delivery log must not answer "is this report well-shaped?"
three different ways.

Why
---
On 2026-08-08 a fan-out of four near-identical sleep cards (``t_4867e94b`` and
its children) produced four different report formats. All four wrote valid
standard Markdown — the persona contract added in the previous commit worked, and
the Block Kit renderer rendered every one of them without error. They still did
not look alike, because none of the card bodies said anything about *shape* and
the persona only says "use standard Markdown", not how much structure:

``t_c781d6b0`` (read as fine)
    312 chars. One ``###`` heading, a lead sentence, three bullets, every epoch
    value in backticks.

``t_88cdceb1`` (read as wrong)
    240 chars. An ``#`` H1 *and* a ``###``, so Slack drew two banner ``header``
    blocks over four lines of content — and the H1 restates the card title the
    notifier has already put in the message. Raw floats, no backticks.

``t_c60439af`` (read as wrong)
    189 chars. A ``###`` immediately followed by three bullets, no prose at all:
    a banner over a bare list. Raw floats, no backticks.

The difference between the one that read as fine and the two that did not is
small, mechanical, and entirely expressible as a rule — which is what this module
is. Prose in a persona competes with the immediate task text and loses; the
stanza below travels *in* the task text.

What is serious and what is only taste
--------------------------------------
:func:`result_shape_defects` reports four defects. Only the two in
:data:`SERIOUS_DEFECTS` are worth a WARNING, because a log line that argues
about taste trains its reader to ignore it:

``top-level-heading`` (serious — WARNING)
    An ``#`` H1. Always wrong here and mechanically fixable — the chat message
    already carries the card title as its heading, so an H1 is a duplicate
    banner. Demoting it to ``##`` is a one-character edit. Fenced code is
    exempt; an *unfenced* manifest or diff is not, and its leading ``#``
    comment reads as an H1. That report is defective anyway — unfenced, it
    renders as a wall of text — so it still warns, and the advice names the
    fence as the edit, because demoting a YAML comment marker would break it.

``ascii-substitute`` (serious — WARNING)
    ``=== Title ===`` or an ALL-CAPS numbered section, with no real Markdown
    anywhere. The report meant to have structure and expressed it in the one
    form Slack cannot see, so it renders flat.

``heading-without-prose`` (taste — INFO)
    A heading whose section is nothing but list items. Usually ugly, sometimes
    correct — a card that genuinely only has three values to report is not
    improved by a manufactured sentence.

``unquoted-numerics`` (taste — INFO)
    Bare high-precision numbers outside backticks. The most reliable signal
    separating the good card from the bad ones above, and still not worth
    waking anybody over: it is cosmetic, and a false positive on a number that
    was meant to be prose would be maddening.

Why the stanza knows which chat it is writing for
-------------------------------------------------
The stanza shipped platform-blind: one text, appended to every card, naming
Google Chat's limitations whoever the requester was. Four of its seven bullets
justified a rule with a fact about a platform the card might not be going to,
and the table bullet was the only rule in the stanza that came with a reason
*not* to follow it — "Google Chat drops tables", stated twice.

On 2026-08-17 card ``t_c730cf24`` ("List non-Kubernetes cron jobs") answered
with ten jobs written as three-line bullet groups, 2,449 characters, no table.
It was delivered to **Slack**, where ``plugins/platforms/slack/block_kit.py``
turns a pipe table into a native Block Kit ``table`` block with per-column
alignment. Two near-identical sibling cards — ``t_9f6b9c49`` and
``t_061b70b7``, both "list the cron jobs" — had used tables, so the stanza was
not suppressing them outright; it was leaving a platform-dependent choice to be
made with a platform-blind brief. The detector agreed the report was fine
(``result_shape_defects`` returned ``()``), because table use is not a defect
and must not become one: a table is right for repeated fields and wrong for
prose, and no regex can tell those apart.

So the fix is to stop telling a Slack card that its tables will be dropped.
:func:`with_report_format` takes the delivery platform and picks between
:data:`REPORT_FORMAT_STANZA` — unchanged, and still what a Google Chat or
unknown-target card gets — and :data:`REPORT_FORMAT_STANZA_TABLES`. The two
share the bullets that are genuinely platform-invariant and differ only where
the old text asserted something about the renderer.

Unknown stays conservative. Cron, CLI and API-server cards have no chat session
behind them, so :func:`current_platform` returns ``""`` and they keep the text
that is safe on the narrower platform. Being wrong in that direction costs a
table; being wrong the other way costs a wall of pipe characters.

What this does *not* cover: a child card fanned out by a worker. The platform
comes from the ContextVar bound to the session calling ``kanban_create``, which
is the same context ``_maybe_auto_subscribe`` needs — and a dispatcher-spawned
worker does not inherit its parent's, which is why children have to be handed
their subscription explicitly by ``kanban_notify_propagate.py`` after the fact.
By then the body is written. So a fan-out child keeps the conservative stanza
even when its report is ultimately propagated to Slack. That is the behaviour
this module already had for every card, not a regression, and closing it means
rewriting a child's body at propagate time rather than at creation.

The Slack variant names its destination out loud ("this card is being delivered
to Slack"). That is deliberate. ``agents/platform/SOUL.md`` §0 tells every
worker to "write for the narrower of the two" and calls a table "a Slack-only
luxury", and that persona line is *correct* for a card whose target is unknown.
The stanza can outrank it only by being more specific than it, which is the
same reason this module exists at all: prose in a persona competes with the
immediate task text and loses.
"""

from __future__ import annotations

import re

__all__ = [
    "REPORT_FORMAT_STANZA",
    "REPORT_FORMAT_STANZA_TABLES",
    "TABLE_RENDERING_PLATFORMS",
    "FORMAT_MARKER",
    "current_platform",
    "renders_tables",
    "stanza_for_platform",
    "has_format_directive",
    "with_report_format",
    "result_shape_defects",
    "serious_defects",
    "SERIOUS_DEFECTS",
    "DEFECT_ADVICE",
]

#: Substring that marks a body as already carrying the stanza. Matched instead of
#: the whole block so a caller who pasted the stanza and then edited a bullet
#: still counts as having one — the point is not to append a second copy.
FORMAT_MARKER = "## Report format"

#: Platform identifiers whose renderer turns a Markdown pipe table into a real
#: table. Slack is the only one the harness ships to today:
#: ``plugins/platforms/slack/block_kit.py`` parses a pipe table into a native
#: Block Kit ``table`` block with per-column alignment, falling back to aligned
#: monospace past Slack's 100-row / 20-column / 10k-character limits. The
#: bundled ``google_chat`` adapter has no table handling at all, so a pipe table
#: arrives there as its literal pipe characters.
#:
#: Matched against ``HERMES_SESSION_PLATFORM``, which the messaging gateway sets
#: to the plugin directory name (``slack``, ``google_chat``).
TABLE_RENDERING_PLATFORMS = frozenset({"slack"})

# The bullets below are shared verbatim by both stanzas: they state a rule whose
# justification does not depend on the renderer. Anything whose *reason* names a
# platform lives in the per-variant text instead, because a Slack card told that
# Google Chat drops its tables is exactly the bug this split exists to fix.

_LEAD_BULLET = """\
- Lead with the answer: what is true, or what is wrong and what you want done.
  Then the detail. Do not narrate the request back or how you investigated."""

_BACKTICKS_BULLET = """\
- Wrap raw values — ids, paths, epochs, durations, counts — in backticks."""

#: Flat text on both renderers, so the rule and its reason are both invariant.
_NO_ASCII_BULLET = """\
- Do not use `=== Title ===`, `1. SECTION`, or hand-aligned columns. Slack
  renders those as flat text."""

_DEFAULT_INTRO = """\
Put the full answer in `result` as standard Markdown — the gateway posts it
verbatim into the requester's chat thread, where Slack renders it as blocks
and Google Chat flattens headings to bold, drops tables, and splits anything
past 4000 characters across messages:"""

_DEFAULT_HEADINGS_BULLET = """\
- Use `##` for sections. Never `#` — the chat message already shows the card
  title, so an H1 renders as a second, duplicate banner — and no `###`: Google
  Chat flattens every level to bold, so a sub-level is invisible there. If you
  are triaging an incident, SOUL.md §7 fixes the sections; use exactly those."""

_DEFAULT_LENGTH_BULLET = """\
- Aim under 2,000 characters. Past 4,000 Google Chat delivers your report as
  several messages rather than one, so if the deliverable is genuinely longer,
  publish it, link it, and keep `result` to the headline findings and that
  link. Never drop a finding to fit."""

_DEFAULT_LINKS_BULLET = """\
- Link every artifact you name — cluster, workload, card, PR, issue, console
  view — as `[text](url)`. Both platforms convert it; a bare id is clickable on
  neither."""

_DEFAULT_TABLES_BULLET = """\
- Put tabular data in a Markdown pipe table with a `---` separator row, but
  keep it to a few short columns and never let the table be the only place a
  fact lives — Google Chat drops it."""

_SLACK_INTRO = """\
Put the full answer in `result` as standard Markdown — the gateway posts it
verbatim into the requester's Slack thread, where Block Kit turns `##` into a
real header, a `|` pipe table into a real table, and `---` into a divider:"""

#: Same rule as the default, different reason. ``_HEADER_RE`` in the Slack
#: renderer matches ``#{1,6}`` and emits one ``header`` block for every level,
#: so a `###` is not a sub-level there either — it is the same banner, and the
#: worker needs to be told that in terms of the platform it is writing for.
_SLACK_HEADINGS_BULLET = """\
- Use `##` for sections. Never `#` — the chat message already shows the card
  title, so an H1 renders as a second, duplicate banner — and no `###`: Slack
  draws every heading level as the same header block, so a sub-level is
  invisible. If you are triaging an incident, SOUL.md §7 fixes the sections;
  use exactly those."""

#: The 2,000-character aim survives the platform split. Slack's adapter chunks
#: at 39,000 rather than 4,000, so the delivery reason does not apply — but the
#: readability one does, and dropping the rule here would make the two stanzas
#: disagree about length for no reason anybody has measured.
_SLACK_LENGTH_BULLET = """\
- Aim under 2,000 characters. Slack will carry far more than that in one
  message, but a report past it stops being read, so if the deliverable is
  genuinely longer, publish it, link it, and keep `result` to the headline
  findings and that link. Never drop a finding to fit."""

_SLACK_LINKS_BULLET = """\
- Link every artifact you name — cluster, workload, card, PR, issue, console
  view — as `[text](url)`. Slack converts it; a bare id is not clickable."""

#: The bullet this split exists for. It names the destination out loud because
#: it has to outrank ``agents/platform/SOUL.md`` §0 — "write for the narrower of
#: the two", "a table is a Slack-only luxury" — which is right for a card whose
#: target is unknown and wrong for this one. A generic re-permission would read
#: as the weaker of two conflicting instructions; naming the platform makes it
#: the more specific one. The limits are Slack's real ones, from
#: ``plugins/platforms/slack/block_kit.py``: past them the renderer falls back
#: to aligned monospace, which is a degradation rather than a loss.
_SLACK_TABLES_BULLET = """\
- Put tabular data in a Markdown pipe table with a `---` separator row. This
  card is being delivered to Slack, which renders it as a real table with
  aligned columns, so prefer one over repeated bullet groups whenever you are
  reporting the same fields for several things. Keep the cells short; past 100
  rows or 20 columns Slack falls back to monospace text."""


def _stanza(intro: str, headings: str, length: str, links: str, tables: str) -> str:
    """Assemble one stanza from its four variable clauses and three fixed ones.

    Bullet order is part of the contract: the lead-with-the-answer rule has to
    be read first, and the two the detector measures for a WARNING
    (``top-level-heading``, ``ascii-substitute``) bracket the list so neither is
    buried in the middle of it.
    """
    return "{marker}\n\n{intro}\n\n{bullets}".format(
        marker=FORMAT_MARKER,
        intro=intro,
        bullets="\n".join(
            (
                _LEAD_BULLET,
                headings,
                length,
                links,
                tables,
                _BACKTICKS_BULLET,
                _NO_ASCII_BULLET,
            )
        ),
    )


#: Appended to a new card's ``body`` when it says nothing about report shape and
#: the delivery platform is Google Chat or unknown. Written as instructions to
#: the worker, in the second person, because that is what the rest of a card
#: body is and the model reads the whole thing as one brief. Everything the
#: detector below measures is stated here, so the card, the schema wording and
#: the delivery log never describe different contracts. The reverse does not
#: hold: the lead-with-the-answer, `###`, length and link rules are stated and
#: not measured, because they came from ``SOUL.md`` §7 and this stanza travels
#: *in* the task text, where the persona does not. Measuring them would mean new
#: defect classes and a louder delivery log for something no reader has yet
#: called wrong — the WARNING tier stays where the evidence is.
#:
#: Still the default, and deliberately byte-for-byte what it was before the
#: platform split: it is what every card created without a chat session behind
#: it goes on getting, and a test pins the text so the split cannot quietly
#: reword the conservative case while nobody is reading it.
REPORT_FORMAT_STANZA = _stanza(
    _DEFAULT_INTRO,
    _DEFAULT_HEADINGS_BULLET,
    _DEFAULT_LENGTH_BULLET,
    _DEFAULT_LINKS_BULLET,
    _DEFAULT_TABLES_BULLET,
)

#: The variant for a card whose report is going somewhere that renders tables.
#: Same rules, same order, same marker — only the clauses that asserted
#: something about the renderer are restated for the platform in hand.
REPORT_FORMAT_STANZA_TABLES = _stanza(
    _SLACK_INTRO,
    _SLACK_HEADINGS_BULLET,
    _SLACK_LENGTH_BULLET,
    _SLACK_LINKS_BULLET,
    _SLACK_TABLES_BULLET,
)

#: Phrases that mean the caller has already said something about shape. A body
#: carrying any of these is left alone: an explicit instruction from the
#: orchestrator outranks a generic stanza, and stapling ours underneath it would
#: give the worker two briefs to reconcile.
_EXISTING_DIRECTIVE = re.compile(
    r"\b(?:report|output|response) format\b"
    r"|\bformat (?:your |the )?(?:result|report|answer|output|response)\b",
    re.IGNORECASE,
)

#: An ATX heading of any level.
_ATX_HEADING = re.compile(r"^ {0,3}(#{1,6}) +(\S.*)$", re.MULTILINE)

#: An H1 specifically.
_H1 = re.compile(r"^ {0,3}# +\S", re.MULTILINE)

#: Block-level Markdown Block Kit turns into structure. Kept in step with
#: ``gateway/kanban_notifier.py``'s copy — see that module's note on why the
#: notifier holds its own.
_BLOCK_MARKDOWN = re.compile(
    r"^ {0,3}(?:#{1,6} +\S|\|.*\||(?:-{3,}|\*{3,}|_{3,}) *$|```)",
    re.MULTILINE,
)

#: ``=== Title ===`` or an ALL-CAPS numbered section — structure Slack cannot see.
_ASCII_STRUCTURE = re.compile(
    r"^ *(?:={2,}[^=\n]+={2,} *|\d+[.)] +[A-Z][A-Z0-9 _/&'\"-]{3,}) *$",
    re.MULTILINE,
)

#: A list item.
_LIST_ITEM = re.compile(r"^ *(?:[-*+] |\d+[.)] )")

#: A fenced code block, removed before the numeric scan so a code sample full of
#: floats is not read as unquoted prose.
_FENCE = re.compile(r"```.*?```", re.DOTALL)

#: Inline code, removed for the same reason — a backticked value is exactly what
#: we are asking for and must not be counted against the report.
_INLINE_CODE = re.compile(r"`[^`\n]*`")

#: A high-precision bare number: six or more significant digits, which is a raw
#: epoch, duration or id rather than a quantity a human wrote on purpose. "3
#: pods" and "99.9%" are left alone deliberately.
_LONG_NUMBER = re.compile(r"(?<![\w.`])\d[\d,]*\.?\d*(?![\w`])")

#: How many bare long numbers before it counts as a defect. Two, not one, so a
#: single figure quoted in a sentence does not trip it.
UNQUOTED_NUMERIC_MIN = 2

#: Significant digits that make a number "raw" rather than written-by-hand.
LONG_NUMBER_MIN_DIGITS = 6

#: Below this a result is a one-liner and has no shape to get wrong. The old
#: floor here was 600, which is why the two cards that prompted this module — 240
#: and 189 characters — could never be measured at all.
SHAPE_MIN_CHARS = 150

#: The defects worth a WARNING rather than an INFO. See the module docstring.
SERIOUS_DEFECTS = ("top-level-heading", "ascii-substitute")

#: What to tell a worker about each defect. Phrased as the edit to make, not as a
#: complaint, because this text is handed straight back to a model that has to
#: act on it.
DEFECT_ADVICE = {
    "top-level-heading": (
        "`result` starts a line with `#`. If that is a heading, use `##` "
        "instead — the chat message already carries this card's title, so an H1 "
        "renders as a second banner saying the same thing. If it is a comment "
        "in a manifest, diff or script, put that block in a ``` fence: "
        "unfenced, it renders as a wall of text and its first comment reads as "
        "a heading. Do not delete the `#` in that case."
    ),
    "ascii-substitute": (
        "`result` marks its sections with `=== Title ===` or ALL-CAPS numbering "
        "and has no real Markdown. Slack renders that as flat text. Use `##` "
        "headings. A pipe table with a `---` separator row is fine for a few "
        "short columns, but Google Chat drops tables, so never let one be the "
        "only place a fact lives."
    ),
    "heading-without-prose": (
        "`result` is a heading over a bare list. Put the answer on the first "
        "line — what is true, or what is wrong and what you want done — then "
        "the list. One line, not a preamble."
    ),
    "unquoted-numerics": (
        "`result` has raw values outside backticks. Wrap ids, paths, epochs, "
        "durations and counts in backticks."
    ),
}


def renders_tables(platform: object) -> bool:
    """Whether ``platform``'s renderer turns a pipe table into a real table.

    False for anything unrecognised, including ``None`` and ``""``. Unknown has
    to mean "assume the narrower renderer": a cron or CLI card has no chat
    session behind it, and promising it a table it will not get is the more
    expensive of the two mistakes.
    """
    if platform is None:
        return False
    return str(platform).strip().lower().replace("-", "_") in TABLE_RENDERING_PLATFORMS


def current_platform() -> str:
    """The messaging platform of the session creating this card, or ``""``.

    Reads the ``HERMES_SESSION_PLATFORM`` ContextVar the messaging gateway sets
    before agent dispatch — the same value ``_maybe_auto_subscribe`` keys the
    completion notification off, which is what makes this the platform the
    report will actually be delivered to rather than a guess about it.

    Imported lazily and behind a blanket ``except``: this module is imported by
    ``tools/kanban_tools.py`` on the card-creation path, and making ``tools``
    depend on ``gateway`` at module scope would put a second package on that
    import graph for the sake of one word in a prompt. Any failure degrades to
    ``""``, which is the conservative stanza.

    That blanket ``except`` is also the one thing here that can fail silently —
    a renamed gateway symbol reads exactly like a cron card — so the build's
    ``verify_kanban_report_format.py`` binds a real Slack session and asserts
    the value comes back, rather than only that the call does not raise.

    ``get_session_env`` falls back to ``os.environ`` when the ContextVar was
    never set in this context, which is what makes the value correct under the
    cron scheduler and the CLI. The agent container sets no
    ``HERMES_SESSION_PLATFORM`` of its own, so that fallback is inert in
    production; setting one process-wide would give every cron and API-server
    card the wording of a chat platform it is not being delivered to.
    """
    try:
        from gateway.session_context import get_session_env

        return str(get_session_env("HERMES_SESSION_PLATFORM", "") or "").strip()
    except Exception:  # pragma: no cover - defensive
        return ""


def stanza_for_platform(platform: object) -> str:
    """The stanza a card delivered to ``platform`` should carry."""
    return REPORT_FORMAT_STANZA_TABLES if renders_tables(platform) else REPORT_FORMAT_STANZA


def has_format_directive(body: object) -> bool:
    """Whether ``body`` already tells the worker how to shape its report."""
    if body is None:
        return False
    text = str(body)
    if FORMAT_MARKER in text:
        return True
    return bool(_EXISTING_DIRECTIVE.search(text))


def with_report_format(body: object, platform: object = None) -> object:
    """Return ``body`` with the report-format stanza appended, if it needs one.

    Idempotent, and a no-op for a caller who already gave format instructions —
    see :data:`_EXISTING_DIRECTIVE`. A card created with no body at all gets the
    stanza as its whole body: an empty brief is the case where the worker has the
    least to go on and the default shape matters most.

    ``platform`` is the chat platform the finished report will be posted to, as
    :func:`current_platform` reports it. It selects between the two stanzas and
    nothing else; ``None`` — the default, and what every caller that does not
    know passes — keeps the conservative text. The lookup is not done here so
    this stays a pure function of its arguments: the impure read belongs at the
    one call site that is on a live session, not in the function the tests and
    the build verifier drive.

    ``body`` is returned unchanged, and with its own type, whenever there is
    nothing to add, so a ``None`` body stays ``None`` for callers downstream that
    distinguish it from ``""``.
    """
    stanza = stanza_for_platform(platform)
    if body is not None and not str(body).strip():
        # Whitespace-only. Treat as absent, but keep returning a string so the
        # handler's own `body` semantics do not change shape underneath it.
        return stanza
    if body is None:
        return stanza
    text = str(body)
    if has_format_directive(text):
        return body
    return text.rstrip() + "\n\n" + stanza


def _strip_code(body: str) -> str:
    """Remove fenced and inline code so their contents are not scanned."""
    return _INLINE_CODE.sub("", _FENCE.sub("", body))


def _has_unquoted_numerics(body: str) -> bool:
    """Whether ``body`` shows raw high-precision numbers outside code."""
    bare = _strip_code(body)
    hits = 0
    for match in _LONG_NUMBER.finditer(bare):
        digits = sum(ch.isdigit() for ch in match.group())
        if digits >= LONG_NUMBER_MIN_DIGITS:
            hits += 1
            if hits >= UNQUOTED_NUMERIC_MIN:
                return True
    return False


def _has_heading_without_prose(body: str) -> bool:
    """Whether every heading in ``body`` is followed only by list items.

    True when the report carries at least one heading and not one line of
    ordinary prose anywhere outside a heading, a list item or a code block.
    """
    if not _ATX_HEADING.search(body):
        return False
    in_fence = False
    for line in _strip_code(body).splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped:
            continue
        if _ATX_HEADING.match(line) or _LIST_ITEM.match(line):
            continue
        if set(stripped) <= set("-*_| "):
            # A divider or a table rule, not a sentence.
            continue
        return False
    return True


def result_shape_defects(
    result: object,
    min_chars: int = SHAPE_MIN_CHARS,
) -> tuple[str, ...]:
    """Name every shape defect in ``result``, most serious first.

    Returns an empty tuple for a result that is fine, too short to have a shape,
    or absent. Pure and total: it never raises, because both callers are on a
    path where an exception would cost more than a missed warning.
    """
    if result is None:
        return ()
    try:
        body = str(result).strip()
    except Exception:  # pragma: no cover - defensive
        return ()
    if len(body) < min_chars:
        return ()

    defects: list[str] = []
    # Fenced code is removed first: a shell comment inside a code block is not a
    # heading, and this defect is the serious tier, so reading one as an H1 would
    # warn about a well-shaped report for an edit the worker cannot make. Inline
    # code stays — an H1 has to open a line, so it can never hide inside a
    # backtick pair, and removing one would let ``\`--dry-run\` # note`` collapse
    # onto its hash and read as a heading nobody wrote.
    #
    # An *unfenced* manifest or diff still trips this: ``# Managed by
    # kube-agents`` at column 0 is a YAML comment, not a heading. That report is
    # defective either way — unfenced YAML renders as a wall of text in Slack —
    # so it still warns, and ``DEFECT_ADVICE`` names the fence as the other edit
    # rather than sending the worker off to demote a comment marker.
    if _H1.search(_FENCE.sub("", body)):
        defects.append("top-level-heading")
    if not _BLOCK_MARKDOWN.search(body) and _ASCII_STRUCTURE.search(body):
        defects.append("ascii-substitute")
    if _has_heading_without_prose(body):
        defects.append("heading-without-prose")
    if _has_unquoted_numerics(body):
        defects.append("unquoted-numerics")
    return tuple(defects)


def serious_defects(
    result: object, min_chars: int = SHAPE_MIN_CHARS
) -> tuple[str, ...]:
    """The subset of :func:`result_shape_defects` worth a WARNING."""
    found = result_shape_defects(result, min_chars=min_chars)
    return tuple(d for d in found if d in SERIOUS_DEFECTS)
