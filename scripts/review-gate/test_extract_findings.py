#!/usr/bin/env python3
"""Unit tests for the detector JSON extractor (Phase 5 / P5-T4). Dependency-free."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import extract_findings as ef


class TestExtract(unittest.TestCase):
    def test_bare_array(self):
        self.assertEqual(ef.extract_json_array('[{"agent":"x","findings":[]}]'), [{"agent": "x", "findings": []}])

    def test_markdown_fenced(self):
        raw = 'Here are the findings:\n```json\n[{"agent":"rbac","findings":[]}]\n```\nDone.'
        self.assertEqual(ef.extract_json_array(raw), [{"agent": "rbac", "findings": []}])

    def test_prose_around_array(self):
        raw = 'I reviewed everything. [{"agent":"pod","findings":[{"message":"m","file":"f","line":"1","severity":"high"}]}] end'
        out = ef.extract_json_array(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["findings"][0]["severity"], "high")

    def test_array_with_bracket_in_string(self):
        # A ']' inside a JSON string must not prematurely close the array.
        raw = '[{"agent":"x","findings":[{"message":"array notation a[0]","file":"f","line":"1","severity":"low"}]}]'
        out = ef.extract_json_array(raw)
        self.assertEqual(out[0]["findings"][0]["message"], "array notation a[0]")

    def test_garbage_returns_empty(self):
        self.assertEqual(ef.extract_json_array("no json here at all"), [])

    def test_empty_returns_empty(self):
        self.assertEqual(ef.extract_json_array(""), [])
        self.assertEqual(ef.extract_json_array(None), [])

    def test_first_valid_array_wins_after_bad_candidate(self):
        raw = 'garbage [not, valid, json here] then [{"agent":"x","findings":[]}]'
        self.assertEqual(ef.extract_json_array(raw), [{"agent": "x", "findings": []}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
