"""Unit tests for the kanban handoff clip installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import unittest

from kanban_handoff_clip import DEFAULT_LIMIT, ELLIPSIS, clip_handoff

# The summary behind the 2026-08-03 broken-link report. Upstream's 200-character
# slice delivered this ending in ".../adamparco-infra/is".
LEDGER_URL = "https://github.com/gke-agentic/adamparco-infra/issues/30"
PRODUCTION_SUMMARY = (
    "Workload Reliability Audit executed successfully: 44 findings (0 critical, 28 major, "
    "16 minor) across 3 clusters. The audit ledger has been updated at " + LEDGER_URL
)


class ClipHandoffTest(unittest.TestCase):
    def test_the_production_summary_survives_whole(self):
        self.assertGreater(len(PRODUCTION_SUMMARY), 200)
        self.assertEqual(clip_handoff(PRODUCTION_SUMMARY), PRODUCTION_SUMMARY)
        self.assertIn(LEDGER_URL, clip_handoff(PRODUCTION_SUMMARY))

    def test_a_url_is_dropped_rather_than_severed(self):
        clipped = clip_handoff(PRODUCTION_SUMMARY, 200)
        self.assertLessEqual(len(clipped), 200)
        self.assertTrue(clipped.endswith(ELLIPSIS))
        # Either the whole link or none of it — never a prefix that 404s.
        self.assertNotIn("https://", clipped)

    def test_every_clip_ends_on_a_whole_token(self):
        text = " ".join(f"token{i}" for i in range(200))
        for limit in range(20, 400, 7):
            clipped = clip_handoff(text, limit)
            self.assertLessEqual(len(clipped), limit)
            body = clipped[: -len(ELLIPSIS)] if clipped.endswith(ELLIPSIS) else clipped
            for token in body.split():
                self.assertIn(token, text.split(), f"token {token!r} was cut at limit {limit}")

    def test_lines_after_the_first_are_kept(self):
        text = "Headline with a link " + LEDGER_URL + "\nSecond line.\nThird line."
        self.assertEqual(clip_handoff(text), text)

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual(clip_handoff("  hello  \n"), "hello")

    def test_blank_input_is_empty(self):
        self.assertEqual(clip_handoff(None), "")
        self.assertEqual(clip_handoff(""), "")
        self.assertEqual(clip_handoff("   "), "")

    def test_non_string_input_is_coerced(self):
        self.assertEqual(clip_handoff(42), "42")

    def test_a_single_oversized_token_is_hard_cut(self):
        clipped = clip_handoff("x" * 50, 20)
        self.assertLessEqual(len(clipped), 20)
        self.assertTrue(clipped.endswith(ELLIPSIS))

    def test_non_positive_limit_returns_the_body(self):
        self.assertEqual(clip_handoff("hello", 0), "hello")

    def test_default_limit_is_generous_enough_for_a_real_summary(self):
        self.assertGreaterEqual(DEFAULT_LIMIT, 1000)


if __name__ == "__main__":
    unittest.main()
