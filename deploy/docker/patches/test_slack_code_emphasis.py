"""Unit tests for the Slack code-emphasis patch applied by the Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

These tests apply the patch to a fixture and then *run* the result, because the
bug is behavioural: an applier that matched all four anchors and still emitted
``{"type": "text", "text": "**"}`` would be no use. ``UPSTREAM`` below is the
genuine inline-parsing region of ``plugins/platforms/slack/block_kit.py``,
copied verbatim from the pinned base image, so the anchors are exercised against
the text they were derived from rather than against a paraphrase.
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

from apply_slack_code_emphasis import BUILD_MARKER, RELATIVE, apply

# Verbatim from plugins/platforms/slack/block_kit.py in the pinned base image,
# with only the module preamble reduced to the imports this region needs. Every
# anchor in the applier points into the text below.
UPSTREAM = '''\
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# Order matters: code first (opaque), then links, then emphasis.
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"(?<!!)\\[([^\\]]+)\\]\\(([^()\\s]+(?:\\([^()]*\\)[^()\\s]*)*)\\)")
_BOLD_RE = re.compile(r"(?:\\*\\*|__)(.+?)(?:\\*\\*|__)")
_ITALIC_RE = re.compile(r"(?<![\\*_])(?:\\*|_)(?![\\*_\\s])(.+?)(?<![\\*_\\s])(?:\\*|_)(?![\\*_])")
_STRIKE_RE = re.compile(r"~~(.+?)~~")


def _inline_elements(text: str) -> List[Dict[str, Any]]:
    """Parse a run of inline markdown into rich_text section child elements.

    Produces ``text`` elements (optionally styled bold/italic/strike/code) and
    ``link`` elements.  Unmatched markup is emitted verbatim as plain text, so
    this never loses characters.
    """
    elements: List[Dict[str, Any]] = []

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

    def _walk_links(s: str, style: Dict[str, bool]) -> None:
        pos = 0
        for m in _LINK_RE.finditer(s):
            _walk_emphasis(s[pos:m.start()], style)
            link_el: Dict[str, Any] = {"type": "link", "url": m.group(2), "text": m.group(1)}
            if style:
                link_el["style"] = dict(style)
            elements.append(link_el)
            pos = m.end()
        _walk_emphasis(s[pos:], style)

    def _walk_emphasis(s: str, style: Dict[str, bool]) -> None:
        if not s:
            return
        # Try bold, then strike, then italic, recursing into the inner span.
        for rx, key in ((_BOLD_RE, "bold"), (_STRIKE_RE, "strike"), (_ITALIC_RE, "italic")):
            m = rx.search(s)
            if m:
                _walk_emphasis(s[:m.start()], style)
                inner_style = dict(style)
                inner_style[key] = True
                _walk_emphasis(m.group(1), inner_style)
                _walk_emphasis(s[m.end():], style)
                return
        emit_text(s, dict(style) if style else None)

    walk(text, {})
    return elements or [{"type": "text", "text": text}]
'''

# The line from card t_549d081c that sent literal asterisks to a user's thread.
REPORTED = "**`adam-new-cluster`** (us-east4) -> Spawning worker card `t_79d6d3d1`"


def build(source=UPSTREAM):
    """Materialise a fake Hermes tree containing ``source``."""
    root = Path(tempfile.mkdtemp())
    path = root / RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return root, path


def load(path, name):
    """Import the patched fixture so its behaviour can be asserted on."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def flatten(elements):
    """(text, style, type) per element — the shape the assertions read against."""
    return [(e.get("text"), e.get("style"), e["type"]) for e in elements]


class UpstreamBugTest(unittest.TestCase):
    """Pin the behaviour being fixed, so the patch is not asserted into a vacuum."""

    def test_upstream_leaks_literal_asterisks(self):
        _, path = build()
        mod = load(path, "block_kit_upstream")
        texts = [t for t, _, _ in flatten(mod._inline_elements(REPORTED))]
        self.assertIn("**", texts)


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.root, self.path = build()
        apply(self.root)
        self.mod = load(self.path, "block_kit_patched")

    def render(self, text):
        return flatten(self.mod._inline_elements(text))

    # -- the reported defect --------------------------------------------------

    def test_emphasis_survives_a_wrapped_code_span(self):
        """The t_549d081c line: no literal ``**``, and the chip is bold."""
        got = self.render(REPORTED)
        self.assertNotIn("**", [t for t, _, _ in got])
        self.assertEqual(
            got[0], ("adam-new-cluster", {"bold": True, "code": True}, "text")
        )
        self.assertEqual(got[1][0], " (us-east4) -> Spawning worker card ")
        self.assertEqual(got[2], ("t_79d6d3d1", {"code": True}, "text"))

    def test_emphasis_survives_a_code_span_inside_it(self):
        """``**bold with `code` inside**`` — every part keeps the bold style."""
        self.assertEqual(
            self.render("**bold with `code` inside**"),
            [
                ("bold with ", {"bold": True}, "text"),
                ("code", {"bold": True, "code": True}, "text"),
                (" inside", {"bold": True}, "text"),
            ],
        )

    def test_italic_and_strike_reach_a_code_span_too(self):
        self.assertEqual(
            self.render("*`i`* and ~~`s`~~"),
            [
                ("i", {"italic": True, "code": True}, "text"),
                (" and ", None, "text"),
                ("s", {"strike": True, "code": True}, "text"),
            ],
        )

    # -- nothing else moved ---------------------------------------------------

    def test_plain_constructs_are_unchanged(self):
        for text, expected in [
            ("**bold** trailing", [("bold", {"bold": True}, "text"),
                                   (" trailing", None, "text")]),
            ("`code` **bold** mix", [("code", {"code": True}, "text"),
                                     (" ", None, "text"),
                                     ("bold", {"bold": True}, "text"),
                                     (" mix", None, "text")]),
            ("plain prose only", [("plain prose only", None, "text")]),
            ("a * b * c stars", [("a * b * c stars", None, "text")]),
        ]:
            with self.subTest(text=text):
                self.assertEqual(self.render(text), expected)

    def test_code_stays_opaque(self):
        """Markdown inside a span is still not interpreted — the point of masking."""
        self.assertEqual(
            self.render("`a **b** c`"), [("a **b** c", {"code": True}, "text")]
        )

    def test_an_unpaired_backtick_is_still_emitted_verbatim(self):
        self.assertEqual(
            self.render("unmatched ` tick and **bold**"),
            [("unmatched ` tick and ", None, "text"),
             ("bold", {"bold": True}, "text")],
        )

    def test_a_nul_in_the_input_cannot_forge_a_sentinel(self):
        """A crafted \\x00N\\x00 in the source must not index into the code list."""
        self.assertEqual(
            self.render("\x000\x00 and `real`"),
            [("0 and ", None, "text"), ("real", {"code": True}, "text")],
        )

    # -- links ----------------------------------------------------------------

    def test_a_plain_link_is_unchanged(self):
        self.assertEqual(
            self.render("[plain link](https://example.com/a_b) after"),
            [("plain link", None, "link"), (" after", None, "text")],
        )

    def test_a_code_span_in_link_text_is_restored_not_dropped(self):
        """A Slack link has no child elements, so the backticks come back.

        Upstream shredded this into three elements because the code split ran
        before the link scan; one link element is the repair, not a regression.
        """
        got = self.render("[`code-link`](https://example.com) after")
        self.assertEqual(got[0], ("`code-link`", None, "link"))
        self.assertEqual(got[1], (" after", None, "text"))

    # -- documented residue ---------------------------------------------------

    def test_bold_italic_around_code_still_leaves_a_stray_star(self):
        """``***`x`***`` is improved but not fixed, and that is deliberate.

        ``_walk_emphasis`` maps one regex to one style key, so ``***`` is only
        ever reached as ``**`` wrapping ``*``; with the inner ``*`` adjacent to a
        masked span there is no closing ``*`` for the italic rule to pair with.
        Upstream emitted ``***`` and ``*** wow`` as literal text with no styling
        at all, so this asserts the improvement rather than pinning a bug: fewer
        stray characters and the bold now applies. Fixing it properly means
        teaching the emphasis walk about multi-style spans, which is a larger
        change than the defect warrants.
        """
        self.assertEqual(
            self.render("***`x`*** wow"),
            [
                ("*", {"bold": True}, "text"),
                ("x", {"bold": True, "code": True}, "text"),
                ("* wow", None, "text"),
            ],
        )

    # -- the applier's own guarantees ----------------------------------------

    def test_the_build_marker_is_present(self):
        self.assertIn(BUILD_MARKER, self.path.read_text())

    def test_a_second_run_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            apply(self.root)
        self.assertIn("already patched", str(caught.exception))


class DriftTest(unittest.TestCase):
    def test_a_moved_anchor_fails_the_build(self):
        """An anchor that stops matching must stop the image, not be skipped."""
        moved = UPSTREAM.replace(
            "        # inline code is opaque — no nested styling\n", ""
        )
        root, _ = build(moved)
        with self.assertRaises(SystemExit) as caught:
            apply(root)
        self.assertIn("found 0", str(caught.exception))

    def test_a_missing_file_fails_the_build(self):
        with self.assertRaises(SystemExit):
            apply(Path(tempfile.mkdtemp()))


if __name__ == "__main__":
    unittest.main()
