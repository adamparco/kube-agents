"""Unit tests for the result delivery installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import ast
import tempfile
import unittest
from pathlib import Path

from apply_kanban_result_delivery import ANCHOR, RELATIVE, apply
from kanban_handoff_clip import DEFAULT_LIMIT, ELLIPSIS, clip_handoff
from kanban_result_delivery import (
    RESULT_LIMIT,
    SEPARATOR,
    handoff_with_result,
    result_block,
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


def notifier_tail(payload_summary, task):
    """Build the completion message's tail the way the patched notifier does.

    The three lines before the call are copied from the ``completed`` branch of
    ``gateway/kanban_watchers.py`` — see UPSTREAM_WATCHERS below, which carries
    the same code at its real indentation. They matter to these tests because
    the ``elif`` is where ``handoff`` becomes a clip of the very field this
    module delivers, and testing ``handoff_with_result`` on a status line
    invented by the test would miss that entirely.
    """
    handoff = ""
    if payload_summary:
        handoff = f"\n{clip_handoff(payload_summary)}"
    elif task and task.result:
        handoff = f"\n{clip_handoff(task.result)}"
    return handoff_with_result(handoff, task)


def report_of_length(length):
    """A plausible multi-line report of exactly ``length`` characters.

    Real text with whitespace in it, because ``clip_handoff`` cuts on a token
    boundary: a run of one repeated character would take the no-whitespace
    branch and clip somewhere these tests do not mean to exercise.
    """
    lines = []
    while len("\n".join(lines)) < length:
        i = len(lines) + 1
        lines.append(f"{i}. cron-job-{i} — schedule `0 {i} * * *` — enabled")
    body = "\n".join(lines)[:length]
    # An exact cut can land on the newline between two lines, and ``result``
    # is stripped before it is measured, which would put the body one short of
    # the boundary the test is aiming at.
    return body[:-1] + "." if body[-1].isspace() else body


class HandoffWithResultTest(unittest.TestCase):
    def test_a_missing_task_row_leaves_the_status_line_alone(self):
        self.assertEqual(handoff_with_result("\nstatus", None), "\nstatus")

    def test_a_task_without_a_result_attribute_leaves_the_status_line_alone(self):
        self.assertEqual(handoff_with_result("\nstatus", object()), "\nstatus")

    def test_a_raising_task_row_cannot_wedge_the_notifier(self):
        class Exploding:
            @property
            def result(self):
                raise RuntimeError("boom")

        self.assertEqual(handoff_with_result("\nstatus", Exploding()), "\nstatus")

    def test_an_absent_handoff_still_delivers_the_report(self):
        for delivered in (None, ""):
            self.assertIn("cron-job-1", handoff_with_result(delivered, _Task(INCIDENT_RESULT)))

    def test_the_summary_branch_keeps_the_status_line_and_adds_the_report(self):
        tail = notifier_tail(INCIDENT_SUMMARY, _Task(INCIDENT_RESULT))
        self.assertIn(INCIDENT_SUMMARY, tail)
        self.assertEqual(tail.count(INCIDENT_RESULT), 1)


class ClipBoundaryTest(unittest.TestCase):
    """The lengths at which the duplicate-delivery bug switched on.

    Under ``DEFAULT_LIMIT`` the notifier's status line is the whole report, the
    containment test in :func:`result_block` sees it, and nothing is appended.
    Over ``DEFAULT_LIMIT`` the status line is a clipped prefix — the report can
    no longer be found inside it, so the old ``handoff +=`` wiring sent the
    opening of the report and then the report. A 60-line cron catalogue arrived
    with jobs 1 to 19 printed twice.
    """

    LENGTHS = (DEFAULT_LIMIT - 1, DEFAULT_LIMIT, DEFAULT_LIMIT * 4)

    def assert_delivered_once(self, tail, body):
        opening = body.splitlines()[0]
        self.assertEqual(tail.count(body), 1, "the report itself is duplicated")
        self.assertEqual(
            tail.count(opening),
            1,
            "the report's opening lines are duplicated by the clipped status line",
        )

    def test_the_no_summary_branch_delivers_the_report_exactly_once(self):
        for length in self.LENGTHS:
            with self.subTest(length=length):
                body = report_of_length(length)
                self.assertEqual(len(body), length)
                self.assert_delivered_once(notifier_tail(None, _Task(body)), body)

    def test_the_no_summary_branch_drops_the_clip_marker_it_no_longer_needs(self):
        # Over budget the status line is discarded outright, so the reader
        # never sees a "[…]" promising more above text that is already whole.
        body = report_of_length(DEFAULT_LIMIT * 4)
        self.assertIn(ELLIPSIS, clip_handoff(body))
        self.assertNotIn(ELLIPSIS, notifier_tail(None, _Task(body)))

    def test_the_summary_branch_delivers_the_report_exactly_once(self):
        for length in self.LENGTHS:
            with self.subTest(length=length):
                body = report_of_length(length)
                tail = notifier_tail(INCIDENT_SUMMARY, _Task(body))
                self.assertIn(INCIDENT_SUMMARY, tail)
                self.assert_delivered_once(tail, body)

    def test_a_status_line_that_is_not_the_report_is_never_dropped(self):
        # The distinction the fix turns on: a clipped prefix of the report is
        # redundant, a summary that happens to be long is not.
        summary = report_of_length(DEFAULT_LIMIT * 2).replace("cron-job", "audit-step")
        body = report_of_length(DEFAULT_LIMIT * 4)
        tail = notifier_tail(summary, _Task(body))
        self.assertIn("audit-step-1 ", tail)
        self.assert_delivered_once(tail, body)


# The notifier's completion branch, reduced to the lines the patch cares about
# but kept at its real nesting depth — the anchor is indentation-sensitive, and
# the `_clip_handoff` lines above it are the ones apply_kanban_wake_kinds and
# the Dockerfile's kanban_handoff_clip edit rewrite, so this excerpt is also
# what proves the two patches compose instead of fighting for the same lines.
UPSTREAM_WATCHERS = '''\
class _Watchers:
    async def _kanban_notifier_watcher(self, interval=5.0):
        while self._running:
            try:
                for d in deliveries:
                    for ev in d["events"]:
                        kind = ev.kind
                        if kind == "completed":
                            handoff = ""
                            payload_summary = None
                            if ev.payload and ev.payload.get("summary"):
                                payload_summary = str(ev.payload["summary"])
                            if payload_summary:
                                h = _clip_handoff(payload_summary)
                                handoff = f"\\n{h}"
                            elif task and task.result:
                                r = _clip_handoff(task.result)
                                handoff = f"\\n{r}"
                            msg = (
                                f"✔ {board_tag}{tag}Kanban {sub['task_id']} done"
                                f" — {title}{handoff}"
                            )
                        elif kind == "blocked":
                            msg = "blocked"
                        await adapter.send(sub["chat_id"], msg)
            except Exception:
                pass
'''


def patch_tree(source):
    root = Path(tempfile.mkdtemp())
    target = root / RELATIVE
    target.parent.mkdir(parents=True)
    target.write_text(source)
    apply(root)
    return target.read_text()


class ApplyTest(unittest.TestCase):
    def test_the_anchor_matches_upstream_exactly_once(self):
        self.assertEqual(UPSTREAM_WATCHERS.count(ANCHOR), 1)

    def test_the_hook_lands_after_the_clip_and_before_the_message(self):
        # Ordering is the whole contract: the hook has to see the handoff the
        # clip produced in order to decide the clip was redundant, and the
        # message has to be built from what the hook returned.
        patched = patch_tree(UPSTREAM_WATCHERS)
        clip = patched.rindex("r = _clip_handoff(task.result)")
        hook = patched.index("handoff = _kanban_handoff_with_result(handoff, task)")
        message = patched.index("msg = (")
        self.assertTrue(clip < hook < message)

    def test_the_hook_replaces_the_handoff_rather_than_appending_to_it(self):
        patched = patch_tree(UPSTREAM_WATCHERS)
        self.assertNotIn("handoff +=", patched)

    def test_the_import_trailer_is_appended(self):
        patched = patch_tree(UPSTREAM_WATCHERS)
        self.assertIn(
            "from gateway.kanban_result_delivery import "
            "handoff_with_result as _kanban_handoff_with_result",
            patched,
        )

    def test_the_patched_module_still_parses(self):
        ast.parse(patch_tree(UPSTREAM_WATCHERS))

    def test_a_drifted_anchor_fails_loudly(self):
        drifted = UPSTREAM_WATCHERS.replace("{tag}Kanban", "{tag}kanban")
        with self.assertRaises(SystemExit) as ctx:
            patch_tree(drifted)
        self.assertIn("found 0", str(ctx.exception))

    def test_applying_twice_fails_rather_than_silently_no_opping(self):
        # The patched text keeps the anchor, so counting it cannot catch a
        # re-run: before the explicit guard a second pass exited 0 and left a
        # second hook call and a second trailer import behind.
        root = Path(tempfile.mkdtemp())
        target = root / RELATIVE
        target.parent.mkdir(parents=True)
        target.write_text(UPSTREAM_WATCHERS)
        apply(root)
        with self.assertRaises(SystemExit) as ctx:
            apply(root)
        self.assertIn("already patched", str(ctx.exception))
        patched = target.read_text()
        self.assertEqual(
            patched.count("handoff = _kanban_handoff_with_result(handoff, task)"), 1
        )
        self.assertEqual(
            patched.count("from gateway.kanban_result_delivery import"), 1
        )

    def test_a_missing_file_fails_loudly(self):
        with self.assertRaises(SystemExit) as ctx:
            apply(Path(tempfile.mkdtemp()))
        self.assertIn("does not exist", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
