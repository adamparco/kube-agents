"""Unit tests for the result delivery installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import unittest

from kanban_handoff_clip import clip_handoff
from kanban_result_delivery import (
    RESULT_LIMIT,
    SEPARATOR,
    result_block,
    result_block_for_task,
)

# The status line the incident actually delivered, and the catalogue it should
# have carried with it.
INCIDENT_SUMMARY = (
    "Successfully inspected and cataloged all 9 active platform-agent-level and "
    "system-wide cron jobs. Compiled their detailed purposes, schedules, and "
    "active configurations."
)
INCIDENT_RESULT = "\n".join(
    f"{i}. cron-job-{i} — schedule `0 {i} * * *` — enabled" for i in range(1, 10)
)


class _Task:
    def __init__(self, result=None):
        self.result = result


class ResultBlockTest(unittest.TestCase):
    def test_the_incident_catalogue_is_delivered(self):
        block = result_block(INCIDENT_SUMMARY, INCIDENT_RESULT)
        self.assertTrue(block.startswith(SEPARATOR))
        self.assertIn("cron-job-1", block)
        self.assertIn("cron-job-9", block)
        # Nothing was lost: every line of the catalogue is present.
        for line in INCIDENT_RESULT.splitlines():
            self.assertIn(line, block)

    def test_multi_line_results_survive_whole(self):
        # The failure the summary channel cannot avoid: it keeps only line one.
        self.assertEqual(len(INCIDENT_RESULT.splitlines()), 9)
        block = result_block("status", INCIDENT_RESULT)
        self.assertEqual(len(block.strip().splitlines()), 9)

    def test_an_empty_result_delivers_nothing(self):
        for empty in (None, "", "   ", "\n\t "):
            self.assertEqual(result_block(INCIDENT_SUMMARY, empty), "")

    def test_a_result_already_in_the_status_line_is_not_repeated(self):
        # What happens when a worker puts one body of text in both fields, and
        # when the require-result gate promotes summary into result.
        self.assertEqual(result_block(INCIDENT_SUMMARY, INCIDENT_SUMMARY), "")

    def test_dedup_ignores_whitespace_and_case(self):
        delivered = "Restarted the deployment; 3/3 pods ready"
        self.assertEqual(result_block(delivered, "restarted  the\ndeployment;   3/3 PODS ready"), "")

    def test_a_longer_result_is_delivered_even_if_it_starts_the_same(self):
        delivered = "Found 9 jobs."
        block = result_block(delivered, "Found 9 jobs.\n\n" + INCIDENT_RESULT)
        self.assertIn("cron-job-5", block)

    def test_no_status_line_still_delivers(self):
        for delivered in (None, ""):
            self.assertIn("cron-job-1", result_block(delivered, INCIDENT_RESULT))

    def test_a_runaway_result_is_clipped_and_says_so(self):
        huge = " ".join(f"token{i}" for i in range(20000))
        self.assertGreater(len(huge), RESULT_LIMIT)
        block = result_block("status", huge)
        self.assertIn("clipped", block.lower())
        self.assertIn(str(RESULT_LIMIT), block)

    def test_a_result_at_the_limit_is_not_marked_clipped(self):
        body = "x" * 100
        block = result_block("status", body)
        self.assertNotIn("clipped", block.lower())
        self.assertEqual(block, SEPARATOR + body)

    def test_clipping_never_severs_a_url(self):
        url = "https://github.com/gke-agentic/adamparco-infra/issues/30"
        body = " ".join(f"token{i}" for i in range(20000)) + " " + url
        block = result_block("status", body, limit=200)
        # Either the whole link or none of it — never a prefix that 404s.
        self.assertNotIn("https://github.com/gke-agentic/adamparco-infra/is\n", block)
        self.assertEqual(clip_handoff(body, 200), block[len(SEPARATOR):].split("\n\n[")[0])

    def test_the_budget_leaves_room_under_the_slack_ceiling(self):
        # Slack's adapter chunks at MAX_MESSAGE_LENGTH = 39000. The status line
        # (<=1200), the title (<=120), and the clip marker must all fit too.
        self.assertLess(RESULT_LIMIT + 1200 + 120 + len("[Result clipped at 30000 characters]"), 39000)


class ResultBlockForTaskTest(unittest.TestCase):
    def test_it_reads_the_task_result(self):
        self.assertIn("cron-job-1", result_block_for_task("status", _Task(INCIDENT_RESULT)))

    def test_a_missing_task_row_is_silent(self):
        self.assertEqual(result_block_for_task("status", None), "")

    def test_a_task_without_a_result_attribute_is_silent(self):
        self.assertEqual(result_block_for_task("status", object()), "")

    def test_a_raising_task_row_cannot_wedge_the_notifier(self):
        class Exploding:
            @property
            def result(self):
                raise RuntimeError("boom")

        self.assertEqual(result_block_for_task("status", Exploding()), "")


if __name__ == "__main__":
    unittest.main()
