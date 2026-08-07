"""Unit tests for the require-result gate installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import copy
import unittest

import kanban_result_required as krr
from kanban_result_required import (
    MISSING_RESULT_ERROR,
    NEW_RESULT_DESCRIPTION,
    NEW_SUMMARY_DESCRIPTION,
    NEW_TOOL_DESCRIPTION,
    OLD_RESULT_DESCRIPTION,
    OLD_SUMMARY_DESCRIPTION,
    OLD_TOOL_DESCRIPTION,
    apply_schema,
    require_result,
)

# The card that started this: the worker held a 5.5 KB catalogue, called
# kanban_complete with a 169-character status line and no result, and the
# catalogue was never seen again.
INCIDENT_SUMMARY = (
    "Successfully inspected and cataloged all 9 active platform-agent-level and "
    "system-wide cron jobs. Compiled their detailed purposes, schedules, and "
    "active configurations."
)
INCIDENT_RESULT = "\n".join(f"{i}. cron-job-{i} — schedule 0 {i} * * *" for i in range(1, 10))


def _schema():
    """A minimal stand-in shaped like upstream's KANBAN_COMPLETE_SCHEMA."""
    return copy.deepcopy({
        "name": "kanban_complete",
        "description": OLD_TOOL_DESCRIPTION + "If you created new tasks, list them.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": OLD_SUMMARY_DESCRIPTION},
                "result": {"type": "string", "description": OLD_RESULT_DESCRIPTION},
                "metadata": {"type": "object", "description": "Free-form dict."},
            },
            "required": [],
        },
    })


class RequireResultTest(unittest.TestCase):
    def setUp(self):
        krr._nudged.clear()
        self.addCleanup(krr._nudged.clear)

    def test_a_result_passes_through_untouched(self):
        err, out = require_result("t_1", INCIDENT_SUMMARY, INCIDENT_RESULT)
        self.assertIsNone(err)
        self.assertEqual(out, INCIDENT_RESULT)

    def test_a_one_line_result_is_enough(self):
        # No length floor: a card whose honest answer is one line must close.
        err, out = require_result("t_1", "Restarted it.", "3/3 pods ready")
        self.assertIsNone(err)
        self.assertEqual(out, "3/3 pods ready")

    def test_the_incident_completion_is_refused(self):
        err, out = require_result("t_8d1cf5cf", INCIDENT_SUMMARY, None)
        self.assertEqual(err, MISSING_RESULT_ERROR)
        self.assertIsNone(out)

    def test_the_refusal_says_what_to_do(self):
        err, _ = require_result("t_1", INCIDENT_SUMMARY, None)
        self.assertIn("result", err)
        self.assertIn("kanban_complete again", err)
        # The three dead ends the incident workers actually chose.
        for dead_end in ("transcript", "file", "comment"):
            self.assertIn(dead_end, err)

    def test_whitespace_only_result_counts_as_empty(self):
        err, _ = require_result("t_1", INCIDENT_SUMMARY, "   \n\t  ")
        self.assertEqual(err, MISSING_RESULT_ERROR)

    def test_the_retry_carrying_a_result_is_accepted(self):
        require_result("t_1", INCIDENT_SUMMARY, None)
        err, out = require_result("t_1", INCIDENT_SUMMARY, INCIDENT_RESULT)
        self.assertIsNone(err)
        self.assertEqual(out, INCIDENT_RESULT)

    def test_a_card_is_never_wedged_shut(self):
        # Second empty completion for the same task closes the card rather than
        # refusing forever; summary is promoted so something survives.
        self.assertEqual(require_result("t_1", INCIDENT_SUMMARY, None)[0], MISSING_RESULT_ERROR)
        err, out = require_result("t_1", INCIDENT_SUMMARY, None)
        self.assertIsNone(err)
        self.assertEqual(out, INCIDENT_SUMMARY)

    def test_a_totally_empty_second_completion_still_closes(self):
        require_result("t_1", None, None)
        err, out = require_result("t_1", None, None)
        self.assertIsNone(err)
        self.assertIsNone(out)

    def test_each_task_gets_its_own_nudge(self):
        # A shared worker process must not spend task B's nudge on task A.
        self.assertEqual(require_result("t_a", "s", None)[0], MISSING_RESULT_ERROR)
        self.assertEqual(require_result("t_b", "s", None)[0], MISSING_RESULT_ERROR)

    def test_non_string_task_ids_do_not_collide_by_accident(self):
        require_result(1, "s", None)
        # str(1) == "1" — the same key, correctly.
        err, _ = require_result("1", "s", None)
        self.assertIsNone(err)


class ApplySchemaTest(unittest.TestCase):
    def test_all_three_descriptions_are_rewritten(self):
        schema = apply_schema(_schema())
        props = schema["parameters"]["properties"]
        self.assertIn(NEW_TOOL_DESCRIPTION, schema["description"])
        self.assertEqual(props["summary"]["description"], NEW_SUMMARY_DESCRIPTION)
        self.assertEqual(props["result"]["description"], NEW_RESULT_DESCRIPTION)

    def test_the_legacy_framing_is_gone(self):
        schema = apply_schema(_schema())
        blob = repr(schema)
        # The exact wording that told workers to throw the answer away.
        self.assertNotIn("legacy field", blob)
        self.assertNotIn("Use ``summary`` instead", blob)
        self.assertNotIn("1-3 sentence", blob)

    def test_the_tool_description_tail_is_preserved(self):
        schema = apply_schema(_schema())
        self.assertIn("If you created new tasks, list them.", schema["description"])

    def test_result_becomes_a_required_parameter(self):
        schema = apply_schema(_schema())
        self.assertIn("result", schema["parameters"]["required"])

    def test_required_is_not_double_appended(self):
        schema = _schema()
        apply_schema(schema)
        schema["description"] = OLD_TOOL_DESCRIPTION
        schema["parameters"]["properties"]["summary"]["description"] = OLD_SUMMARY_DESCRIPTION
        schema["parameters"]["properties"]["result"]["description"] = OLD_RESULT_DESCRIPTION
        apply_schema(schema)
        self.assertEqual(schema["parameters"]["required"].count("result"), 1)

    def test_the_new_wording_names_the_real_limits(self):
        # The persona previously advertised 1200 characters; the kernel cuts at
        # 400 on the first line. A worker must be told the number that is true.
        self.assertIn("400", NEW_SUMMARY_DESCRIPTION)
        self.assertIn("FIRST LINE", NEW_SUMMARY_DESCRIPTION)
        self.assertIn("400", NEW_TOOL_DESCRIPTION)
        self.assertIn("REQUIRED", NEW_RESULT_DESCRIPTION)

    def test_a_changed_upstream_description_fails_loudly(self):
        schema = _schema()
        schema["parameters"]["properties"]["result"]["description"] = "something else"
        with self.assertRaises(ValueError) as ctx:
            apply_schema(schema)
        self.assertIn("re-derive", str(ctx.exception))

    def test_a_missing_field_fails_loudly(self):
        schema = _schema()
        del schema["parameters"]["properties"]["summary"]
        with self.assertRaises(KeyError) as ctx:
            apply_schema(schema)
        self.assertIn("re-derive", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
