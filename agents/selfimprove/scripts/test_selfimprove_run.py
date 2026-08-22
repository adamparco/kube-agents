#!/usr/bin/env python3
"""Tests for the runner's handoff: how a turn's findings get back to the runner.

The file the agent writes is the only channel out of a run, and the first live
run lost a 34-minute investigation through it -- the turn exhausted its
iteration budget, exited 0, and the runner recorded `outcome=ok findings=0`.
Two things came out of that: the recovery below, which reads JSON out of a
response that was never written to the file, and the usage logging, which makes
a truncated turn say so instead of passing for a clean one.

Everything here is pure. `selfimprove_run` imports only the standard library and
the ledger at module scope, so these run in CI with no cluster and no Hermes.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import selfimprove_run as R  # noqa: E402


FINDING = {"signal": "errors", "severity": "high", "title": "t", "location": "l"}


class RecoverFindingsTests(unittest.TestCase):
    """`recover_findings` accepts every shape a turn actually hands back."""

    def test_a_bare_array_is_the_file_written_as_asked(self):
        self.assertEqual(R.recover_findings(json.dumps([FINDING])), [FINDING])

    def test_an_empty_array_is_a_real_answer_and_is_not_none(self):
        # The distinction the caller depends on: [] means the run found
        # nothing, None means the run handed nothing back. They are logged
        # differently because only the second one is a defect.
        self.assertEqual(R.recover_findings("[]"), [])
        self.assertIsNotNone(R.recover_findings("[]"))

    def test_a_json_fence_in_prose_is_read(self):
        text = "Here is what I found:\n```json\n%s\n```\nThat is all." % json.dumps([FINDING])
        self.assertEqual(R.recover_findings(text), [FINDING])

    def test_an_unlabelled_fence_is_read(self):
        text = "Findings:\n```\n%s\n```" % json.dumps([FINDING])
        self.assertEqual(R.recover_findings(text), [FINDING])

    def test_unfenced_json_embedded_in_prose_is_read(self):
        text = "I found one problem. Findings: %s Done." % json.dumps([FINDING])
        self.assertEqual(R.recover_findings(text), [FINDING])

    def test_a_dict_wrapper_is_unwrapped(self):
        self.assertEqual(R.recover_findings(json.dumps({"findings": [FINDING]})), [FINDING])

    def test_a_bracket_inside_a_string_does_not_unbalance_the_scan(self):
        item = dict(FINDING, evidence=["saw ] and } in the log line"])
        text = "prose before %s prose after" % json.dumps([item])
        self.assertEqual(R.recover_findings(text), [item])

    def test_braces_in_prose_before_the_array_do_not_win(self):
        # `{ }` balances first and parses, but it is not a findings list, so the
        # scan has to keep going rather than stop at the first thing that is
        # valid JSON.
        text = "Templates use { } for substitution. Findings: %s" % json.dumps([FINDING])
        self.assertEqual(R.recover_findings(text), [FINDING])

    def test_the_iteration_budget_warning_does_not_block_recovery(self):
        # Exactly what a capped turn prints ahead of its response.
        text = "⚠ Iteration budget reached (400/400) — response may be incomplete\n%s" % json.dumps(
            [FINDING]
        )
        self.assertEqual(R.recover_findings(text), [FINDING])

    def test_prose_with_no_json_recovers_nothing(self):
        self.assertIsNone(R.recover_findings("I investigated and found nothing conclusive."))

    def test_empty_and_blank_text_recover_nothing(self):
        self.assertIsNone(R.recover_findings(""))
        self.assertIsNone(R.recover_findings("   \n  "))

    def test_truncated_json_recovers_nothing_rather_than_half_a_finding(self):
        self.assertIsNone(R.recover_findings('[{"title": "cut off here'))

    def test_non_dict_members_are_dropped(self):
        self.assertEqual(R.recover_findings(json.dumps([FINDING, "junk", 7])), [FINDING])


class ReadFindingsTests(unittest.TestCase):
    """The file is authoritative; the response is the fallback."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "findings.json")

    def test_the_file_is_read_when_it_exists(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump([FINDING], handle)
        self.assertEqual(R.read_findings(self.path, "ignored"), [FINDING])

    def test_the_file_wins_over_the_response(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump([], handle)
        self.assertEqual(R.read_findings(self.path, json.dumps([FINDING])), [])

    def test_a_missing_file_falls_back_to_the_response(self):
        self.assertEqual(R.read_findings(self.path, json.dumps([FINDING])), [FINDING])

    def test_a_missing_file_and_an_unusable_response_is_nothing_found(self):
        self.assertEqual(R.read_findings(self.path, "no json here"), [])

    def test_a_garbage_file_is_nothing_found_rather_than_a_crash(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("not json at all")
        self.assertEqual(R.read_findings(self.path, "also not json"), [])


class SlugTests(unittest.TestCase):
    def test_a_filing_label_becomes_a_usable_filename(self):
        # Labels carry a fingerprint after a colon, which cannot go in a path.
        self.assertEqual(R._slug("file:a1b2c3"), "file-a1b2c3")

    def test_a_plain_label_is_unchanged(self):
        self.assertEqual(R._slug("investigate"), "investigate")


if __name__ == "__main__":
    unittest.main()
