"""Unit tests for the report-back gate installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import unittest

from report_back_completion import (
    CONTENT_MIN_CHARS,
    carries_content,
    declares_payload,
    promises_deliverable,
    report_back_violation,
    requests_report,
)

# Card t_7f3e0a5e, verbatim from the kanban DB on 2026-08-05. It closed done
# with result_len 0; the manifest it describes reached nobody.
INCIDENT_TITLE = "List enabled cron jobs and scheduled audits"
INCIDENT_BODY = (
    "Please list all enabled cron jobs, scheduled audits, and background "
    "tasks configured across the platform, the GitOps repository, or GKE "
    "clusters. Report back with their schedules, targets, and active states."
)
INCIDENT_SUMMARY = (
    "Successfully audited and cataloged all platform cron jobs, scheduled "
    "audits, background tasks, GitOps declarations, and GKE controller "
    "states. Provided a detailed manifest mapping schedules, targets, active "
    "states, and recent execution statuses."
)
# What should have been in `result`.
INCIDENT_ANSWER = "\n".join(
    [
        "Active platform cron jobs (6 of 11 enabled):",
        "- compliance-audit — 20 6 * * * — Security & RBAC Posture Audit",
        "- obtainability-audit — 50 6 * * * — Workload Reliability Audit",
        "- github-issue-resolver — */30 * * * * — GitHub Issue Resolver",
    ]
)
# The metadata the kernel stamps on every completion by the owning worker.
STAMPED = {"worker_session_id": "20260805_223456_eeda96"}


class TheIncidentTest(unittest.TestCase):
    """The 2026-08-05 completion must be rejected, and its retry accepted."""

    def violation(self, **overrides):
        kwargs = dict(
            title=INCIDENT_TITLE,
            body=INCIDENT_BODY,
            summary=INCIDENT_SUMMARY,
            result="",
            metadata=dict(STAMPED),
        )
        kwargs.update(overrides)
        return report_back_violation(**kwargs)

    def test_the_completion_that_shipped_is_rejected(self):
        self.assertIsNotNone(self.violation())

    def test_the_rejection_says_where_to_put_the_answer(self):
        message = self.violation()
        self.assertIn("kanban_complete blocked", message)
        self.assertIn("`result`", message)
        self.assertIn("artifacts", message)
        # The belief that produced the bug: the chat message is the delivery.
        self.assertIn("chat message is not on the card", message)
        # And that a retry is available — a worker that reads this as terminal
        # blocks the card instead of fixing it.
        self.assertIn("still in-flight", message)

    def test_the_retry_with_the_real_answer_is_accepted(self):
        self.assertIsNone(self.violation(result=INCIDENT_ANSWER))

    def test_the_stamped_session_id_alone_is_not_a_payload(self):
        # metadata is never empty on a worker's own completion, so the payload
        # check has to see past what the kernel put there.
        self.assertFalse(declares_payload(dict(STAMPED)))
        self.assertIsNotNone(self.violation())


class TerminationTest(unittest.TestCase):
    """Any non-empty result clears the gate. Nothing else is safe."""

    def test_any_non_empty_result_is_accepted(self):
        for result in ("no cron jobs are enabled", "none", "0", INCIDENT_ANSWER):
            with self.subTest(result=result):
                self.assertIsNone(
                    report_back_violation(
                        title=INCIDENT_TITLE, body=INCIDENT_BODY, result=result
                    )
                )

    def test_a_short_true_answer_is_not_held_to_a_length_floor(self):
        # The loop this avoids: a card whose honest answer is one line can
        # never satisfy a content threshold, and would retry until the turn
        # limit. carries_content() says no; the gate must still say yes.
        answer = "No cron jobs are enabled."
        self.assertFalse(carries_content(answer))
        self.assertIsNone(
            report_back_violation(
                title=INCIDENT_TITLE, body=INCIDENT_BODY, result=answer
            )
        )

    def test_whitespace_is_not_a_result(self):
        self.assertIsNotNone(
            report_back_violation(
                title=INCIDENT_TITLE,
                body=INCIDENT_BODY,
                summary=INCIDENT_SUMMARY,
                result="   \n  ",
            )
        )


class OrdinaryWorkTest(unittest.TestCase):
    """Cards that asked for a change, not a report, complete untouched."""

    def test_a_fix_card_with_a_terse_summary_completes(self):
        self.assertIsNone(
            report_back_violation(
                title="Fix the flaky checkout test",
                body="It fails about one run in five on CI.",
                summary="Fixed by widening the timeout to 30s.",
            )
        )

    def test_a_deploy_card_completes(self):
        self.assertIsNone(
            report_back_violation(
                title="Roll out v2.3.1 to staging",
                body="Standard rollout, no migration.",
                summary="Rolled out and healthy across all three replicas.",
            )
        )

    def test_an_empty_card_completes(self):
        self.assertIsNone(report_back_violation())


class TriggersTest(unittest.TestCase):
    """Either signal fires the gate: the ask, or the claim."""

    def test_a_request_with_no_content_is_caught(self):
        self.assertIsNotNone(
            report_back_violation(
                title="List the pods stuck in CrashLoopBackOff",
                body="",
                summary="Checked every namespace.",
            )
        )

    def test_a_claim_with_no_content_is_caught_on_any_card(self):
        # No report verb anywhere in the card — the summary alone gives it away.
        self.assertIsNotNone(
            report_back_violation(
                title="Rotate the signing keys",
                body="Quarterly rotation.",
                summary="Rotated. See below for the full breakdown.",
            )
        )

    def test_neither_signal_means_no_gate(self):
        self.assertFalse(requests_report("Rotate the signing keys", "Quarterly."))
        self.assertFalse(promises_deliverable("Rotated all four keys.", ""))
        self.assertIsNone(
            report_back_violation(
                title="Rotate the signing keys",
                body="Quarterly.",
                summary="Rotated all four keys.",
            )
        )

    def test_report_verbs(self):
        for phrase in (
            "List the enabled jobs",
            "Report back with the totals",
            "What are the current quotas?",
            "How many clusters are unpatched?",
            "Tell me which nodes are cordoned",
            "Summarize the incident",
            "Enumerate the open findings",
            "Audit the RBAC bindings",
            "Investigate the latency spike",
            "Analyze the cost trend",
            "Provide a rollout plan",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(requests_report(phrase, ""))

    def test_words_that_merely_contain_a_verb_do_not_count(self):
        # \b keeps "blacklist"/"listing"/"auditing" from reading as a request.
        for phrase in (
            "Update the blacklist entry",
            "Fix the listing page",
            "Rename the whitelist file",
        ):
            with self.subTest(phrase=phrase):
                self.assertFalse(requests_report(phrase, ""))

    def test_promissory_phrases(self):
        for phrase in (
            "Provided a detailed manifest of every job",
            "Provided the requested figures",
            "The following jobs are enabled",
            "Results are as follows",
            "See below for details",
            "Below is the mapping",
            "Compiled a full inventory",
            "See attached",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(promises_deliverable(phrase, ""))


class SatisfiedElsewhereTest(unittest.TestCase):
    """Content the card already holds makes the gate unnecessary."""

    def test_a_file_deliverable_is_accepted(self):
        # _handle_complete folds artifacts into metadata before the gate runs.
        self.assertIsNone(
            report_back_violation(
                title=INCIDENT_TITLE,
                body=INCIDENT_BODY,
                summary=INCIDENT_SUMMARY,
                metadata={"artifacts": ["/tmp/cron-manifest.md"], **STAMPED},
            )
        )

    def test_structured_findings_in_metadata_are_accepted(self):
        self.assertIsNone(
            report_back_violation(
                title=INCIDENT_TITLE,
                body=INCIDENT_BODY,
                summary=INCIDENT_SUMMARY,
                metadata={"findings": [{"job": "compliance-audit"}], **STAMPED},
            )
        )

    def test_an_empty_metadata_value_is_not_a_payload(self):
        for empty in ({}, [], "", None):
            with self.subTest(empty=empty):
                self.assertFalse(declares_payload({"findings": empty, **STAMPED}))

    def test_a_summary_that_is_itself_the_report_is_accepted(self):
        self.assertIsNone(
            report_back_violation(
                title=INCIDENT_TITLE,
                body=INCIDENT_BODY,
                summary=INCIDENT_ANSWER,
            )
        )

    def test_a_published_url_in_the_summary_is_accepted(self):
        self.assertIsNone(
            report_back_violation(
                title="Audit the RBAC bindings",
                body="",
                summary="Findings published to https://example.invalid/issues/30",
            )
        )

    def test_a_comment_carrying_the_report_is_accepted(self):
        self.assertIsNone(
            report_back_violation(
                title=INCIDENT_TITLE,
                body=INCIDENT_BODY,
                summary=INCIDENT_SUMMARY,
                comments=["irrelevant chatter", INCIDENT_ANSWER],
            )
        )

    def test_chatter_in_the_comments_is_not_a_report(self):
        self.assertIsNotNone(
            report_back_violation(
                title=INCIDENT_TITLE,
                body=INCIDENT_BODY,
                summary=INCIDENT_SUMMARY,
                comments=["starting now", "still working on it"],
            )
        )


class CarriesContentTest(unittest.TestCase):
    """What counts as the answer rather than a label for one."""

    def test_length_alone_qualifies(self):
        self.assertTrue(carries_content("x" * CONTENT_MIN_CHARS))
        self.assertFalse(carries_content("x" * (CONTENT_MIN_CHARS - 1)))

    def test_the_incident_summary_does_not_qualify(self):
        self.assertFalse(carries_content(INCIDENT_SUMMARY))

    def test_list_shapes_qualify(self):
        for body in (
            "- a\n- b\n- c",
            "1. a\n2. b\n3. c",
            "* a\n* b\n* c",
            "| job | cron |\n| a | 1 |\n| b | 2 |",
            "Job ID: a\nSchedule: 20 6 * * *\nState: enabled",
        ):
            with self.subTest(body=body):
                self.assertTrue(carries_content(body))

    def test_two_list_lines_are_not_yet_a_report(self):
        self.assertFalse(carries_content("- a\n- b"))

    def test_pointers_qualify(self):
        self.assertTrue(carries_content("see https://example.invalid/x"))
        self.assertTrue(carries_content("written to /opt/data/report.md"))

    def test_prose_that_only_looks_like_a_path_does_not_qualify(self):
        # A date and an "and/or" are not deliverables.
        self.assertFalse(carries_content("due 6/10/2026, blocked and/or waiting"))

    def test_empty_inputs_do_not_qualify(self):
        self.assertFalse(carries_content("", None, "   "))


class FailsOpenTest(unittest.TestCase):
    """A heuristic must never be the reason a finished card cannot close."""

    def test_unreadable_arguments_do_not_block(self):
        class Exploding:
            def __str__(self):
                raise RuntimeError("nope")

        self.assertIsNone(
            report_back_violation(
                title=Exploding(),
                body=Exploding(),
                summary=Exploding(),
                comments=[Exploding()],
            )
        )

    def test_a_non_dict_metadata_does_not_block_on_a_plain_card(self):
        self.assertIsNone(
            report_back_violation(
                title="Rotate keys", body="", summary="Done.", metadata="nonsense"
            )
        )


if __name__ == "__main__":
    unittest.main()
