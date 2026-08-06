"""Unit tests for the kanban result delivery installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import asyncio
import unittest

from kanban_handoff_clip import DEFAULT_LIMIT
from kanban_result_delivery import (
    CLIPPED_TAIL,
    RESULT_LIMIT,
    deliver_result,
    result_message,
)

CARD = "t_7f3e0a5e"

# The completion that shipped on 2026-08-05: a status line asserting a manifest
# had been provided, and the manifest that should have travelled with it.
SUMMARY = (
    "Successfully audited and cataloged all platform cron jobs, scheduled "
    "audits, background tasks, GitOps declarations, and GKE controller "
    "states. Provided a detailed manifest mapping schedules, targets, active "
    "states, and recent execution statuses."
)
ANSWER = (
    "Active platform cron jobs (6 of 11 enabled):\n"
    "- compliance-audit — 20 6 * * * — Security & RBAC Posture Audit\n"
    "- obtainability-audit — 50 6 * * * — Workload Reliability Audit\n"
    "- github-issue-resolver — */30 * * * * — GitHub Issue Resolver"
)

# What the notifier's completion line carries: a newline, then the clipped
# summary. The leading newline is why the comparison normalises whitespace.
DELIVERED = "\n" + SUMMARY


class _Adapter:
    def __init__(self, error=None):
        self.sent = []
        self.error = error

    async def send(self, chat_id, content, metadata=None):
        if self.error:
            raise self.error
        self.sent.append((chat_id, content, metadata))
        return True


class _Task:
    def __init__(self, result):
        self.result = result


class ResultMessageTest(unittest.TestCase):
    def test_the_incident_manifest_reaches_chat(self):
        msg = result_message(CARD, DELIVERED, ANSWER)
        self.assertIn(ANSWER, msg)
        self.assertIn(CARD, msg)
        self.assertNotIn(CLIPPED_TAIL, msg)

    def test_the_report_is_not_the_summary(self):
        # The whole point: the line already sent said a manifest was provided.
        # It is not the manifest, so the manifest is still worth a message.
        self.assertTrue(result_message(CARD, DELIVERED, ANSWER))

    def test_nothing_to_say(self):
        for empty in (None, "", "   ", "\n\t  \n"):
            with self.subTest(result=empty):
                self.assertEqual(result_message(CARD, DELIVERED, empty), "")

    def test_a_result_already_in_the_line_is_not_repeated(self):
        # The legacy branch: no summary in the payload, so the completion line
        # was built from task.result. Sending it again is pure noise.
        self.assertEqual(result_message(CARD, "\n" + ANSWER, ANSWER), "")

    def test_the_repeat_check_ignores_whitespace_and_case(self):
        self.assertEqual(
            result_message(CARD, "\n  ACTIVE   JOBS:  none\n", "Active jobs: none"),
            "",
        )

    def test_a_result_buried_in_a_longer_line_is_not_repeated(self):
        delivered = f"\nDone. {ANSWER} Nothing else to report."
        self.assertEqual(result_message(CARD, delivered, ANSWER), "")

    def test_a_line_clipped_short_of_the_result_still_gets_it(self):
        # clip_handoff cut the completion line at DEFAULT_LIMIT. Everything
        # past the cut has not been delivered to anyone.
        long_answer = ANSWER + "\n" + ("- filler-job — 0 * * * — Filler\n" * 80)
        self.assertGreater(len(long_answer), DEFAULT_LIMIT)
        delivered = "\n" + long_answer[:DEFAULT_LIMIT]
        self.assertIn(ANSWER, result_message(CARD, delivered, long_answer))

    def test_no_line_at_all_still_delivers(self):
        for delivered in (None, ""):
            with self.subTest(delivered=delivered):
                self.assertIn(ANSWER, result_message(CARD, delivered, ANSWER))

    def test_a_non_string_result_is_still_delivered(self):
        # task.result is a TEXT column, so this should not happen — but losing
        # a deliverable to a type check is the failure this patch exists to
        # prevent, so coerce rather than drop.
        self.assertIn("42", result_message(CARD, DELIVERED, 42))


class ClippingTest(unittest.TestCase):
    def test_a_runaway_result_is_bounded_and_says_so(self):
        msg = result_message(CARD, DELIVERED, "spam " * 4000)
        self.assertTrue(msg.endswith(CLIPPED_TAIL))
        self.assertLess(len(msg), RESULT_LIMIT + 200)

    def test_a_result_within_budget_is_untouched(self):
        body = "x " * 100
        msg = result_message(CARD, DELIVERED, body)
        self.assertNotIn(CLIPPED_TAIL, msg)
        self.assertTrue(msg.endswith(body.strip()))

    def test_the_budget_sits_between_a_status_line_and_a_report(self):
        # Absolute, not relative to the constant: the completion line's 1200 is
        # sized for a status line, and the manifest card t_7f3e0a5e should have
        # delivered was roughly 4 KB. A budget inside that gap would clip the
        # deliverable this patch exists to deliver.
        self.assertGreater(RESULT_LIMIT, 4500)
        self.assertLess(RESULT_LIMIT, 20000)

    def test_the_limit_is_honoured(self):
        msg = result_message(CARD, DELIVERED, "word " * 200, limit=100)
        self.assertTrue(msg.endswith(CLIPPED_TAIL))
        self.assertLess(len(msg), 100 + len(CLIPPED_TAIL) + 60)


class DeliverResultTest(unittest.TestCase):
    def _deliver(self, result, delivered=DELIVERED, adapter=None):
        adapter = adapter or _Adapter()
        sent = asyncio.run(
            deliver_result(
                adapter=adapter,
                chat_id="spaces/AAAA",
                metadata={"thread_id": "spaces/AAAA/threads/B"},
                task_id=CARD,
                delivered=delivered,
                task=_Task(result),
            )
        )
        return sent, adapter.sent

    def test_it_sends_the_report(self):
        sent, posts = self._deliver(ANSWER)
        self.assertTrue(sent)
        self.assertEqual(len(posts), 1)
        chat_id, content, metadata = posts[0]
        self.assertEqual(chat_id, "spaces/AAAA")
        self.assertEqual(metadata, {"thread_id": "spaces/AAAA/threads/B"})
        self.assertIn(ANSWER, content)

    def test_it_sends_nothing_when_there_is_nothing_to_send(self):
        self.assertEqual(self._deliver(None), (False, []))
        self.assertEqual(self._deliver(""), (False, []))
        self.assertEqual(self._deliver(ANSWER, delivered="\n" + ANSWER), (False, []))

    def test_a_task_without_a_result_attribute_is_survivable(self):
        adapter = _Adapter()
        sent = asyncio.run(
            deliver_result(
                adapter=adapter,
                chat_id="spaces/AAAA",
                metadata=None,
                task_id=CARD,
                delivered=DELIVERED,
                task=None,
            )
        )
        self.assertEqual((sent, adapter.sent), (False, []))

    def test_a_send_failure_is_the_caller_s_to_catch(self):
        # Documented contract: the notifier wraps this call the way it wraps
        # artifact delivery, so swallowing here would hide the failure twice.
        with self.assertRaises(RuntimeError):
            self._deliver(ANSWER, adapter=_Adapter(error=RuntimeError("chat is down")))


if __name__ == "__main__":
    unittest.main()
