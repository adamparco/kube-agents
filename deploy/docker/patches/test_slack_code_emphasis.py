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

    def test_upstream_keeps_identifiers_intact_across_a_code_span(self):
        """The baseline the intra-word guard has to preserve.

        Upstream's split on code is what keeps these two underscores apart. The
        fix removes the split, so without a guard this line regresses — pinning
        the pre-fix behaviour is what makes that regression detectable.
        """
        _, path = build()
        mod = load(path, "block_kit_upstream_gap")
        elements = mod._inline_elements(
            "Card t_549d081c: cluster `adam-new-cluster` needs machine_type e2"
        )
        self.assertEqual(
            "".join(t for t, _, _ in flatten(elements)),
            "Card t_549d081c: cluster adam-new-cluster needs machine_type e2",
        )

    def test_upstream_mangles_two_identifiers_in_one_gap(self):
        """The pre-existing weakness the guard repairs as a side effect."""
        _, path = build()
        mod = load(path, "block_kit_upstream_samegap")
        got = "".join(
            t for t, _, _ in flatten(
                mod._inline_elements("Card t_549d081c needs machine_type e2")
            )
        )
        self.assertEqual(got, "Card t549d081c needs machinetype e2")


class PatchedFixture:
    """Patch a fresh fixture per test and render real text through the result."""

    def setUp(self):
        self.root, self.path = build()
        apply(self.root)
        self.mod = load(self.path, f"block_kit_{type(self).__name__}")

    def render(self, text):
        return flatten(self.mod._inline_elements(text))

    def assertPlain(self, text):
        """The line renders as itself: one unstyled element, nothing consumed.

        The single strongest assertion available here. It catches a delimiter
        eaten, a sentinel left unrestored, a spurious style, and a run split in
        two — all in one, and all of them silent in production.
        """
        self.assertEqual(self.render(text), [(text, None, "text")])


class ApplyTest(PatchedFixture, unittest.TestCase):
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

    def test_a_crafted_soh_cannot_forge_an_underscore(self):
        """A \\x01 in the source is dropped, not restored as an underscore."""
        self.assertEqual(
            self.render("a\x01b and `real`"),
            [("ab and ", None, "text"), ("real", {"code": True}, "text")],
        )

    # -- intra-word underscores (regression found in review of PR #666) ------

    def test_identifier_underscores_do_not_pair_across_a_code_span(self):
        """Masking must not let two identifiers' underscores find each other.

        With the run no longer split on code, ``_ITALIC_RE`` — which has no
        intra-word rule — would pair the ``_`` in ``t_549d081c`` with the one in
        ``machine_type``, italicising everything between, swallowing the chip,
        and deleting both delimiters. Upstream leaves this line alone, so it
        would be a regression introduced by the fix, and a worse one than the
        defect being fixed: literal asterisks are lossless, a silently mangled
        card ID is not.
        """
        self.assertEqual(
            self.render(
                "Card t_549d081c: cluster `adam-new-cluster` needs machine_type e2"
            ),
            [
                ("Card t_549d081c: cluster ", None, "text"),
                ("adam-new-cluster", {"code": True}, "text"),
                (" needs machine_type e2", None, "text"),
            ],
        )

    def test_double_underscores_do_not_pair_across_a_code_span(self):
        """The same hole, reached through ``_BOLD_RE``'s ``__`` alternative."""
        self.assertEqual(
            self.render("foo__bar `x` baz__qux"),
            [
                ("foo__bar ", None, "text"),
                ("x", {"code": True}, "text"),
                (" baz__qux", None, "text"),
            ],
        )

    def test_delimited_underscore_emphasis_still_works(self):
        """The guard is intra-word only, so real ``_``/``__`` still emphasise.

        This is what forces ``[^\\W_]`` rather than ``\\w`` in the guard: ``_``
        is itself a word character, so ``\\w`` matches the *inner* underscore of
        each ``__`` pair and degrades real bold to italic.
        """
        for text, expected in [
            ("_ital_ x", [("ital", {"italic": True}, "text"),
                          (" x", None, "text")]),
            ("__bold__ x", [("bold", {"bold": True}, "text"),
                            (" x", None, "text")]),
        ]:
            with self.subTest(text=text):
                self.assertEqual(self.render(text), expected)

    def test_an_underscore_in_a_link_url_is_restored(self):
        """A masked underscore must not be dropped from a link's flat url."""
        elements = self.mod._inline_elements("[t](https://example.com/a_b) x")
        self.assertEqual(elements[0]["type"], "link")
        self.assertEqual(elements[0]["url"], "https://example.com/a_b")

    def test_the_same_gap_case_is_repaired_as_a_side_effect(self):
        """Two identifiers with no span between them — upstream mangles this.

        Pinned so the improvement is deliberate: the guard fixes a pre-existing
        upstream weakness that the old split-on-code tokenizer never addressed.
        """
        self.assertEqual(
            self.render("Card t_549d081c needs machine_type e2"),
            [("Card t_549d081c needs machine_type e2", None, "text")],
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


class RealWorldTextTest(PatchedFixture, unittest.TestCase):
    """The text these reports actually carry, rather than markdown specimens.

    A Platform Agent report is mostly paths, URLs, flags, selectors, machine
    types and card IDs — strings full of characters that markdown also uses as
    delimiters. Every case below is a line an agent could plausibly emit, and
    the risk in each is the same: a renderer that treats punctuation as markup
    silently deletes it, and the reader has no way to tell that the identifier
    they were given is not the identifier that was logged.
    """

    # -- file paths ----------------------------------------------------------

    def test_file_paths_are_not_emphasis(self):
        """Snake-case path segments are the single most common false positive.

        Two ``_``-bearing segments in one line is the ordinary case for a log
        path, and upstream italicises everything between them and drops both
        underscores — ``/var/log/kube_agents/agent.log`` arrives in Slack as
        ``/var/log/kube`` + italic. The guard is what makes these render as
        typed.
        """
        for path in [
            "/opt/hermes/plugins/platforms/slack/block_kit.py",
            "wrote /var/log/kube_agents/agent.log and /var/log/kube_agents/err.log",
            "~/.kube/config_backup vs ~/.kube/config_old",
            "C:\\Users\\adam\\my_file.txt",
            "file__with__dunders.py",
            "snake_case_name_with_many_parts",
            "relative/path_a/../path_b/file.yaml",
            "multiple___underscores___here",
            "trailing underscore_ and _leading underscore",
        ]:
            with self.subTest(path=path):
                self.assertPlain(path)

    def test_a_path_keeps_its_underscores_either_side_of_a_code_span(self):
        self.assertEqual(
            self.render("see `/etc/config` then /var/lib/my_dir/my_file.yaml"),
            [
                ("see ", None, "text"),
                ("/etc/config", {"code": True}, "text"),
                (" then /var/lib/my_dir/my_file.yaml", None, "text"),
            ],
        )

    # -- urls ----------------------------------------------------------------

    def test_bare_urls_are_not_emphasis(self):
        """A query string is a dense field of markdown delimiters.

        ``?x=1&y=2#sec_3`` pairs its ``_`` with an earlier one in the path, so
        the URL a reader copies out of Slack is not the URL that was fetched.
        """
        for url in [
            "https://example.com/a_b",
            "https://example.com/foo_bar?x=1&y=2#sec_3",
            "https://ex.com/path*glob",
            "<https://example.com/a_b|my_label>",
            "https://ex.com/a_b/c_d/e_f?filter=name_prefix",
        ]:
            with self.subTest(url=url):
                self.assertPlain(url)

    def test_a_url_keeps_its_underscores_across_a_code_span(self):
        self.assertEqual(
            self.render("docs at https://ex.com/x_y.html, code `t_1`, more m_n"),
            [
                ("docs at https://ex.com/x_y.html, code ", None, "text"),
                ("t_1", {"code": True}, "text"),
                (", more m_n", None, "text"),
            ],
        )

    def test_a_markdown_link_keeps_underscores_in_both_url_and_label(self):
        got = self.mod._inline_elements("[a_b](https://example.com/c_d) and e_f")
        self.assertEqual(got[0]["type"], "link")
        self.assertEqual(got[0]["url"], "https://example.com/c_d")
        self.assertEqual(got[0]["text"], "a_b")
        self.assertEqual(got[1], {"type": "text", "text": " and e_f"})

    # -- asterisks as wildcards ----------------------------------------------

    def test_a_lone_wildcard_is_not_emphasis(self):
        """Shell and selector wildcards, which sit next to whitespace or alone.

        ``_ITALIC_RE`` rejects a delimiter followed by whitespace, and a single
        unpaired ``*`` has nothing to pair with, so the everyday glob survives.
        """
        for command in [
            "kubectl get pods -n * -o wide",
            "rm -rf /tmp/*",
            "SELECT * FROM pods WHERE ns = 'a'",
            "glob **/*.py matched 3 files",
            "chmod 755 *.sh && ls *.yaml",
            "2 * 3 * 4 = 24",
            "match *.yaml and *.yml",
            "--set image.tag=* is not allowed",
            "ls -l *",
            "ls *.sh *.py",
            "glob a*b only",
        ]:
            with self.subTest(command=command):
                self.assertPlain(command)

    def test_a_wildcard_inside_a_code_span_is_opaque(self):
        self.assertEqual(
            self.render("`kubectl get po -A -l app=*`"),
            [("kubectl get po -A -l app=*", {"code": True}, "text")],
        )

    def test_two_intra_word_asterisks_do_pair_and_that_is_deliberate(self):
        """The asymmetry with ``_``: CommonMark allows intra-word ``*``.

        ``cp a*b c*d`` is italicised by upstream with no code span in sight, so
        this is not something the patch introduced, and it is not something the
        guard should suppress — masking intra-word ``*`` the way ``_`` is masked
        would also kill legitimate ``a**b**c``. Pinned rather than fixed so the
        asymmetry is a decision on the record instead of an oversight, and so a
        future attempt to "make the guard symmetric" fails here first.

        The practical residue is a two-wildcard selector such as
        ``app=*,tier=*``, which loses both stars. Escaping them, or wrapping the
        selector in backticks, renders it correctly.
        """
        for text, expected in [
            ("cp a*b c*d", [("cp a", None, "text"),
                            ("b c", {"italic": True}, "text"),
                            ("d", None, "text")]),
            ("app=*,tier=* selector", [("app=", None, "text"),
                                       (",tier=", {"italic": True}, "text"),
                                       (" selector", None, "text")]),
            ("a**b**c", [("a", None, "text"),
                         ("b", {"bold": True}, "text"),
                         ("c", None, "text")]),
        ]:
            with self.subTest(text=text):
                self.assertEqual(self.render(text), expected)

    def test_intra_word_asterisks_now_pair_across_a_code_span_too(self):
        """Masking makes the code-span case agree with the no-span case.

        Upstream left ``a*b `x` c*d`` alone only because the split hid each
        ``*`` from the other, while italicising the identical ``cp a*b c*d``.
        Consistency is the right outcome for ``*`` — the opposite of ``_``,
        where consistency is reached by never emphasising at all.
        """
        self.assertEqual(
            self.render("a*b `x` c*d"),
            [
                ("a", None, "text"),
                ("b ", {"italic": True}, "text"),
                ("x", {"italic": True, "code": True}, "text"),
                (" c", {"italic": True}, "text"),
                ("d", None, "text"),
            ],
        )

    # -- shell, flags and kubernetes identifiers ------------------------------

    def test_shell_and_kubernetes_text_is_not_emphasis(self):
        for line in [
            "export KUBE_AGENTS_HOME=/opt/hermes && echo $KUBE_AGENTS_HOME",
            "flag --dry-run=client, var $MY_VAR, file my_file",
            "e2-standard-8 and n2_standard_16",
            "a_b*c_d",
            ":tada: a_b :rocket:",
        ]:
            with self.subTest(line=line):
                self.assertPlain(line)

    def test_the_reported_card_line_renders_end_to_end(self):
        """Both defects in one line: a wrapped chip and two loose identifiers."""
        self.assertEqual(
            self.render("- **`c1`** ok, **`c2`** ok, plan_id p_1"),
            [
                ("- ", None, "text"),
                ("c1", {"bold": True, "code": True}, "text"),
                (" ok, ", None, "text"),
                ("c2", {"bold": True, "code": True}, "text"),
                (" ok, plan_id p_1", None, "text"),
            ],
        )

    # -- unicode --------------------------------------------------------------

    def test_non_ascii_identifiers_count_as_words(self):
        """``[^\\W_]`` is unicode-aware, so the guard is not ASCII-only.

        An ASCII-only guard (``[a-zA-Z0-9]``) would leave ``café_latte`` and
        ``漢_字`` mangled — the class of bug that only shows up in someone
        else's locale.
        """
        for text, expected in [
            ("漢_字 and `code` and 中_文",
             [("漢_字 and ", None, "text"),
              ("code", {"code": True}, "text"),
              (" and 中_文", None, "text")]),
            ("café_latte and `x` and naïve_test",
             [("café_latte and ", None, "text"),
              ("x", {"code": True}, "text"),
              (" and naïve_test", None, "text")]),
        ]:
            with self.subTest(text=text):
                self.assertEqual(self.render(text), expected)

    def test_an_emoji_is_not_a_word_character(self):
        """So ``🚀_x`` keeps its underscore by the unpaired rule, not the guard."""
        self.assertEqual(
            self.render("emoji 🚀_x and `y`"),
            [("emoji 🚀_x and ", None, "text"), ("y", {"code": True}, "text")],
        )

    # -- invariants -----------------------------------------------------------

    def test_no_sentinel_ever_reaches_a_rendered_element(self):
        """The failure mode with no visible symptom until Slack renders it.

        A sentinel that escapes shows up as a control character in the thread,
        or worse is swallowed by the client. Nothing else in the suite would
        notice, because every other assertion names the text it expects.
        """
        for text in [
            "Card t_549d081c: cluster `adam-new-cluster` needs machine_type e2",
            "**`chip`** and `plain` and a_b",
            "[a_b](https://ex.com/c_d) `code` e_f",
            "`a_b` `c_d` `e_f` g_h",
            "\x00 \x01 `x` a_b",
        ]:
            with self.subTest(text=text):
                for rendered, _, _ in self.render(text):
                    self.assertNotIn("\x00", rendered)
                    self.assertNotIn("\x01", rendered)

    def test_code_spans_are_restored_in_order(self):
        """Sentinels index a list, so a mis-numbered restore silently permutes."""
        self.assertEqual(
            self.render("`one` a_b `two` c_d `three`"),
            [
                ("one", {"code": True}, "text"),
                (" a_b ", None, "text"),
                ("two", {"code": True}, "text"),
                (" c_d ", None, "text"),
                ("three", {"code": True}, "text"),
            ],
        )

    def test_unterminated_and_empty_code_markers_are_verbatim(self):
        for text in [
            "an unclosed ` backtick with a_b after",
            "`` and a_b",
            "``",
        ]:
            with self.subTest(text=text):
                self.assertPlain(text)


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
