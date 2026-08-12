#!/usr/bin/env python3
"""Let bold and italic survive an inline code span in Slack Block Kit output.

Run by ``deploy/docker/Dockerfile`` against the Hermes tree at
``plugins/platforms/slack/block_kit.py``.

The bug
-------
``_inline_elements`` turns a run of markdown into ``rich_text`` children — the
elements Slack uses for list items, block quotes and table cells. It tokenized
inline code first and *by splitting the run on it*::

    for m in _INLINE_CODE_RE.finditer(s):
        _walk_links(s[pos:m.start()], style)   # the gap BEFORE the code span
        emit_text(m.group(1), {"code": True})
        pos = m.end()
    _walk_links(s[pos:], style)                # the gap AFTER it

Each gap reaches the emphasis scan as its own string, so a ``**`` that opens
before a code span and closes after it can never pair up: ``_BOLD_RE`` sees
``"**"`` in one call and ``"** (us-east4) …"`` in the next, matches neither, and
``_walk_emphasis`` falls through to ``emit_text(s, …)``, which emits the
delimiters verbatim. The docstring's promise that "unmatched markup is emitted
verbatim … so this never loses characters" is what turns a parse miss into
literal asterisks on screen.

Two standard-Markdown spellings render wrongly as a result:

``**`code`**`` (emphasis wrapping a code span)
    Renders as a literal ``**``, a code chip, then a literal ``**``.

``**bold with `code` inside**`` (a code span inside an emphasis run)
    Renders as literal ``**bold with``, a code chip, then ``inside**``. Nothing
    is bold, because the only place code is detected is the top-level ``walk``
    and ``_walk_emphasis`` never recurses back into it.

Measured on card ``t_549d081c`` (a fleet health check that fanned out to four
Cluster Agents on 2026-08-12). Its ``result`` opened each list item with
``**`adam-new-cluster`** (us-east4) -> …`` and Slack showed the asterisks:
``{"type": "text", "text": "**"}``, ``{"text": "adam-new-cluster", "style":
{"code": true}}``, ``{"type": "text", "text": "** (us-east4) -> …"}``.

Only the ``rich_text`` path is affected. Section blocks go through the adapter's
``format_message`` (``render_blocks(content, mrkdwn_fn=self.format_message)``),
which protects inline code behind placeholders *before* converting ``**x**`` to
``*x*``, so the same markdown in a paragraph has always rendered correctly. That
asymmetry is why this reads as a formatting bug rather than a broken agent: the
identical source renders one way in a paragraph and another in a list item.

The agents cannot route around it. ``agents/platform/SOUL.md`` §0 tells every
worker to "write standard Markdown and not Slack's own mrkdwn — ``**bold**``,
not ``*bold*`` — because the adapter converts for you". ``**`code`**`` is
standard Markdown, and backticked identifiers are required by the report-format
stanza in ``kanban_report_format.py``, so the two rules together steer workers
straight into this case.

The fix
-------
Mask instead of split. Each inline code span is replaced by a ``\\x00N\\x00``
sentinel and held in a list; links and emphasis then scan one continuous string,
and ``emit_text`` restores the spans as it emits, giving each the style of the
run it sits in. Code stays opaque either way — no markdown is interpreted inside
a span, which was the point of tokenizing it first — but the emphasis scan is no
longer handed a pre-chopped string it cannot match across.

A restored span carries a combined style, ``{"bold": true, "code": true}``,
which is what Slack's own composer emits for bolded code and what the upstream
``code_style = dict(style)`` line was already written to allow — that ``dict``
copy could only ever be empty before this change, because ``walk`` ran only at
the top level with ``{}``.

``_unmask`` handles the one place a code element cannot go: a Slack ``link``
element carries flat ``text``/``url`` strings with no children, so a sentinel
landing there is restored to its original backticked source instead. That also
repairs ``[`code`](url)``, which upstream shredded into three elements because
the code split ran before the link scan.

Upstream: not reported. The renderer is Hermes-internal and this directory is
the repository's normal route for a Hermes fix.

Usage::

    python3 apply_slack_code_emphasis.py [HERMES_ROOT]  # /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

RELATIVE = "plugins/platforms/slack/block_kit.py"

# Asserted in the built bundle by the Dockerfile, so a patch that silently stops
# applying fails the image build instead of shipping literal asterisks.
BUILD_MARKER = "_CODE_SENTINEL_RE"

# ---------------------------------------------------------------------------
# 1) The comment above the inline regexes still has to describe what happens.
# ---------------------------------------------------------------------------

ORDER_COMMENT = "# Order matters: code first (opaque), then links, then emphasis.\n"

ORDER_COMMENT_PATCHED = (
    "# Order matters: code first (masked opaque, see _CODE_SENTINEL_RE), then\n"
    "# links, then emphasis.\n"
)

# ---------------------------------------------------------------------------
# 2) The sentinel pattern, minted next to the regexes it has to survive.
# ---------------------------------------------------------------------------

STRIKE = '_STRIKE_RE = re.compile(r"~~(.+?)~~")\n'

STRIKE_PATCHED = STRIKE + (
    "# kube-agents patch: what a masked inline-code span leaves behind for the\n"
    "# emphasis scan to step over as one opaque word. NUL cannot occur in a Slack\n"
    "# message, and the digits between the delimiters are neither whitespace nor\n"
    "# `*`/`_`, so _ITALIC_RE's lookarounds still pair around a masked span.\n"
    '_CODE_SENTINEL_RE = re.compile(r"\\x00(\\d+)\\x00")\n'
)

# ---------------------------------------------------------------------------
# 3) The tokenizer itself: emit_text gains a restore step, walk masks.
# ---------------------------------------------------------------------------

TOKENIZER = '''\
    def emit_text(s: str, style: Optional[Dict[str, bool]] = None) -> None:
        if not s:
            return
        el: Dict[str, Any] = {"type": "text", "text": s}
        if style:
            el["style"] = style
        elements.append(el)

    # Tokenize by the highest-priority markers first using a single scan.
    # We recursively split on code, then links, then emphasis to keep spans
    # from overlapping incorrectly.
    def walk(s: str, style: Dict[str, bool]) -> None:
        pos = 0
        # inline code is opaque — no nested styling
        for m in _INLINE_CODE_RE.finditer(s):
            _walk_links(s[pos:m.start()], style)
            code_style = dict(style)
            code_style["code"] = True
            emit_text(m.group(1), code_style or None)
            pos = m.end()
        _walk_links(s[pos:], style)
'''

TOKENIZER_PATCHED = '''\
    def _append(s: str, style: Optional[Dict[str, bool]] = None) -> None:
        if not s:
            return
        el: Dict[str, Any] = {"type": "text", "text": s}
        if style:
            el["style"] = style
        elements.append(el)

    def emit_text(s: str, style: Optional[Dict[str, bool]] = None) -> None:
        """Emit ``s``, restoring masked code spans as code-styled elements.

        A restored span inherits the style of the run it sits inside, so
        ``**`x`**`` emits one element styled bold *and* code rather than the
        literal asterisks the split-on-code tokenizer used to leave behind.
        """
        if not s:
            return
        pos = 0
        for m in _CODE_SENTINEL_RE.finditer(s):
            _append(s[pos:m.start()], style)
            code_style = dict(style or {})
            code_style["code"] = True
            _append(codes[int(m.group(1))], code_style)
            pos = m.end()
        _append(s[pos:], style)

    # kube-agents patch — see deploy/docker/patches/apply_slack_code_emphasis.py.
    # Inline code is masked rather than split on. Upstream handed the emphasis
    # scan each gap between code spans as a separate string, so a `**` opening
    # before a span and closing after it never paired and both delimiters
    # reached Slack as literal asterisks; card t_549d081c posted
    # "**`adam-new-cluster`** (us-east4) -> …" into a user's thread that way.
    # Masking keeps a span opaque — no markdown is interpreted inside it — while
    # leaving links and emphasis one continuous string to match across.
    codes: List[str] = []

    def _mask_code(s: str) -> str:
        def take(m: re.Match) -> str:
            codes.append(m.group(1))
            return f"\\x00{len(codes) - 1}\\x00"

        # A NUL already in the text would make a sentinel ambiguous. It cannot
        # occur in a Slack message, so dropping it costs nothing.
        return _INLINE_CODE_RE.sub(take, s.replace("\\x00", ""))

    def _unmask(s: str) -> str:
        """Restore masked spans to their original backticked source.

        For the one place a code element cannot go: a Slack ``link`` carries
        flat ``text``/``url`` strings with no child elements, so the backticks
        come back rather than the span being dropped.
        """
        return _CODE_SENTINEL_RE.sub(lambda m: f"`{codes[int(m.group(1))]}`", s)

    def walk(s: str, style: Dict[str, bool]) -> None:
        _walk_links(_mask_code(s), style)
'''

# ---------------------------------------------------------------------------
# 4) A link's flat text/url get the source restored into them.
# ---------------------------------------------------------------------------

LINK = (
    '            link_el: Dict[str, Any] = '
    '{"type": "link", "url": m.group(2), "text": m.group(1)}\n'
)

LINK_PATCHED = """\
            link_el: Dict[str, Any] = {
                "type": "link",
                "url": _unmask(m.group(2)),
                "text": _unmask(m.group(1)),
            }
"""


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix="slack_code_emphasis")
    patch.refuse_if_patched(BUILD_MARKER)
    patch.substitute(ORDER_COMMENT, ORDER_COMMENT_PATCHED)
    patch.substitute(STRIKE, STRIKE_PATCHED)
    patch.substitute(TOKENIZER, TOKENIZER_PATCHED)
    patch.substitute(LINK, LINK_PATCHED)
    patch.commit("4 anchors")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
