#!/usr/bin/env python3
"""Tests for the self-improvement ledger, fingerprint and promotion gate.

The gate is the part of this feature that decides whether an autonomous agent
opens a pull request on a human's repository, so it is the part that has to be
right when nobody is watching. All of it is pure -- no Kubernetes import at
module scope in selfimprove_ledger.py -- which is what lets these run in CI with
no cluster.

Every test that involves time passes `now` explicitly. A test that let the clock
run would be a test whose failure depends on when it is run, and the window
arithmetic here is exactly the kind of thing that fails at midnight.
"""

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import selfimprove_ledger as L  # noqa: E402


NOW = dt.datetime(2026, 8, 22, 12, 0, 0, tzinfo=dt.timezone.utc)


def finding(**overrides):
    base = {
        "signal": "errors",
        "severity": "high",
        "title": "Reconciler retries a Secret it cannot read",
        "location": "k8s-operator/internal/controller/platformagent_controller.go:412",
        "summary": "The reconcile loop retries forever.",
        "evidence": ["2026-08-22T09:00:00Z E0822 secrets is forbidden"],
        "proposed_fix": "Fail the reconcile with a clear status condition.",
    }
    base.update(overrides)
    return base


def gate(**overrides):
    base = {
        "rules": [
            {"severity": "critical", "minOccurrencesPerDay": 1},
            {"severity": "high", "minOccurrencesPerDay": 5},
        ],
        "maxPullRequestsPerDay": 2,
        "cooldownHours": 24,
    }
    base.update(overrides)
    return base


class NormaliseTests(unittest.TestCase):
    def test_strips_the_parts_that_differ_between_sightings(self):
        a = L.normalise("pod platform-agent-gateway-7d9f4c8b6-xk2vn failed at 2026-08-22T09:14:03Z")
        b = L.normalise("pod platform-agent-gateway-5a1c2d3e4-pq8zt failed at 2026-08-22T11:47:51Z")
        self.assertEqual(a, b)

    def test_keeps_genuinely_different_text_different(self):
        self.assertNotEqual(
            L.normalise("reconciler cannot read Secret"),
            L.normalise("reconciler cannot read ConfigMap"),
        )

    def test_a_title_keeps_its_digits(self):
        """The digit sweep is scoped to locations, and this is why.

        "skill 1 fails" and "skill 2 fails" are two bugs. Collapsing them gives
        one entry whose occurrence count is their sum -- and the count is what
        the gate reads, so over-normalising a title does not lose information
        so much as manufacture a promotion out of two unrelated sightings.
        """
        self.assertNotEqual(
            L.normalise("github-issue-resolver retry 1 exhausted"),
            L.normalise("github-issue-resolver retry 2 exhausted"),
        )


class NormaliseLocationTests(unittest.TestCase):
    def test_line_numbers_collapse(self):
        """A line number drifts on every commit that touches the file above it.

        Without this the same bug fingerprints differently after an unrelated
        import is added, its count resets to one, and a `critical` that should
        have been filed on the second sighting never clears the gate.
        """
        self.assertEqual(
            L.normalise_location("k8s-operator/internal/controller/platformagent.go:412"),
            L.normalise_location("k8s-operator/internal/controller/platformagent.go:418"),
        )

    def test_the_file_still_distinguishes(self):
        self.assertNotEqual(
            L.normalise_location("gateway.py:12"), L.normalise_location("runner.py:12")
        )

    def test_a_column_is_collapsed_with_the_line(self):
        self.assertEqual(
            L.normalise_location("selfimprove_run.py:88:14"),
            L.normalise_location("selfimprove_run.py:91:3"),
        )


class FingerprintTests(unittest.TestCase):
    def test_is_stable_across_incidental_variation(self):
        first = L.fingerprint("errors", "Gateway timed out at 2026-08-22T09:00:00Z", "gateway.py:12")
        second = L.fingerprint("errors", "Gateway timed out at 2026-08-22T18:31:44Z", "gateway.py:12")
        self.assertEqual(first, second)

    def test_severity_is_not_part_of_identity(self):
        """A re-graded finding is the same finding.

        This is the property that keeps occurrence counts accumulating when the
        agent changes its mind about how bad something is. Without it, the third
        sighting of a bug graded `high`, `critical`, `high` looks like three
        separate findings with one sighting each and nothing is ever promoted.
        """
        one, _ = L.record_finding(L.empty_ledger(), finding(severity="high"), "abc", NOW)
        two, _ = L.record_finding(L.empty_ledger(), finding(severity="critical"), "abc", NOW)
        self.assertEqual(one, two)

    def test_signal_and_location_are_part_of_identity(self):
        base = L.fingerprint("errors", "Same title", "a.py:1")
        self.assertNotEqual(base, L.fingerprint("latency", "Same title", "a.py:1"))
        self.assertNotEqual(base, L.fingerprint("errors", "Same title", "b.py:1"))


class RecordFindingTests(unittest.TestCase):
    def test_the_agent_cannot_set_its_own_history(self):
        """Identity fields the agent supplies are honoured; history is not.

        An agent that could write its own occurrence count could talk itself
        past the gate in a single run, which would make the frequency half of
        the severity/frequency contract meaningless.
        """
        ledger = L.empty_ledger()
        _, entry = L.record_finding(
            ledger,
            finding(promotions=[{"at": L.to_iso(NOW), "url": "https://example.invalid/pr/1"}]),
            "abc123",
            NOW,
        )
        self.assertEqual(entry["promotions"], [])

    def test_unknown_signal_and_severity_fall_back_rather_than_raising(self):
        _, entry = L.record_finding(
            L.empty_ledger(), finding(signal="wishlist", severity="catastrophic"), "abc", NOW
        )
        self.assertEqual(entry["signal"], "other")
        # `low`, not `critical`: an unparseable grade must not buy priority.
        self.assertEqual(entry["severity"], "low")

    def test_repeat_sightings_accumulate_on_one_entry(self):
        ledger = L.empty_ledger()
        L.record_finding(ledger, finding(occurrences=3), "abc", NOW - dt.timedelta(hours=2))
        fp, entry = L.record_finding(ledger, finding(occurrences=4), "abc", NOW)
        self.assertEqual(len(ledger["findings"]), 1)
        self.assertEqual(L.occurrences_in_window(entry, NOW), 7)
        self.assertEqual(entry["first_seen"], L.to_iso(NOW - dt.timedelta(hours=2)))
        self.assertEqual(entry["last_seen"], L.to_iso(NOW))


class OccurrenceWindowTests(unittest.TestCase):
    def test_counts_only_inside_the_window(self):
        entry = {
            "sightings": [
                {"at": L.to_iso(NOW - dt.timedelta(hours=30)), "count": 100},
                {"at": L.to_iso(NOW - dt.timedelta(hours=2)), "count": 3},
            ]
        }
        self.assertEqual(L.occurrences_in_window(entry, NOW), 3)

    def test_a_malformed_timestamp_is_ignored_not_counted(self):
        """An unparseable sighting withholds a promotion rather than granting one."""
        entry = {"sightings": [{"at": "not a date", "count": 99}]}
        self.assertEqual(L.occurrences_in_window(entry, NOW), 0)


class GateTests(unittest.TestCase):
    def _ledger_with(self, count, **kw):
        ledger = L.empty_ledger()
        fp, _ = L.record_finding(ledger, finding(occurrences=count, **kw), "abc", NOW)
        return ledger, fp

    def test_promotes_when_severity_and_frequency_are_both_met(self):
        ledger, fp = self._ledger_with(6)
        promoted, reasons = L.evaluate_gate(ledger, gate(), [fp], NOW)
        self.assertEqual(promoted, [fp])
        self.assertIn("promoted", reasons[fp])

    def test_holds_when_frequency_is_short(self):
        ledger, fp = self._ledger_with(4)  # rule wants 5
        promoted, reasons = L.evaluate_gate(ledger, gate(), [fp], NOW)
        self.assertEqual(promoted, [])
        self.assertIn("rule wants 5", reasons[fp])

    def test_a_severity_with_no_rule_is_never_promoted(self):
        """`medium` and `low` are excluded by omission, not by a separate switch."""
        ledger, fp = self._ledger_with(10_000, severity="medium")
        promoted, reasons = L.evaluate_gate(ledger, gate(), [fp], NOW)
        self.assertEqual(promoted, [])
        self.assertIn("no promotion rule", reasons[fp])

    def test_critical_clears_on_a_single_occurrence(self):
        ledger, fp = self._ledger_with(1, severity="critical")
        promoted, _ = L.evaluate_gate(ledger, gate(), [fp], NOW)
        self.assertEqual(promoted, [fp])

    def test_cooldown_blocks_a_refile(self):
        ledger, fp = self._ledger_with(9)
        L.record_promotion(ledger, fp, "https://example.invalid/pr/1", "abc", NOW - dt.timedelta(hours=3))
        promoted, reasons = L.evaluate_gate(ledger, gate(), [fp], NOW)
        self.assertEqual(promoted, [])
        self.assertIn("cooldown", reasons[fp])

    def test_cooldown_expires(self):
        ledger, fp = self._ledger_with(9)
        L.record_promotion(ledger, fp, "https://example.invalid/pr/1", "abc", NOW - dt.timedelta(hours=25))
        promoted, _ = L.evaluate_gate(ledger, gate(), [fp], NOW)
        self.assertEqual(promoted, [fp])

    def test_the_daily_budget_caps_one_run(self):
        """Six criticals in one run must not become six pull requests.

        The budget is decremented inside the loop rather than only compared
        against the ledger, because nothing has been filed yet when the second
        candidate is considered.
        """
        ledger = L.empty_ledger()
        fps = []
        for i in range(6):
            fp, _ = L.record_finding(
                ledger,
                finding(severity="critical", title="Critical number %d" % i, location="a.py:%d" % i),
                "abc",
                NOW,
            )
            fps.append(fp)
        promoted, reasons = L.evaluate_gate(ledger, gate(), fps, NOW)
        self.assertEqual(len(promoted), 2)
        self.assertEqual(sum(1 for r in reasons.values() if "budget" in r), 4)

    def test_the_budget_spans_runs_not_just_one_run(self):
        ledger = L.empty_ledger()
        old, _ = L.record_finding(ledger, finding(title="Earlier", location="a.py:1"), "abc", NOW)
        L.record_promotion(ledger, old, "https://example.invalid/pr/1", "abc", NOW - dt.timedelta(hours=1))
        L.record_promotion(ledger, old, "https://example.invalid/pr/2", "abc", NOW - dt.timedelta(hours=2))
        fresh, _ = L.record_finding(
            ledger, finding(severity="critical", title="Now", location="b.py:1"), "abc", NOW
        )
        promoted, reasons = L.evaluate_gate(ledger, gate(), [fresh], NOW)
        self.assertEqual(promoted, [])
        self.assertIn("budget", reasons[fresh])

    def test_worse_severities_are_considered_first_under_a_tight_budget(self):
        ledger = L.empty_ledger()
        high, _ = L.record_finding(
            ledger, finding(severity="high", occurrences=50, title="High", location="a.py:1"), "abc", NOW
        )
        critical, _ = L.record_finding(
            ledger, finding(severity="critical", occurrences=1, title="Critical", location="b.py:1"), "abc", NOW
        )
        promoted, _ = L.evaluate_gate(ledger, gate(maxPullRequestsPerDay=1), [high, critical], NOW)
        self.assertEqual(promoted, [critical])

    def test_an_empty_gate_promotes_nothing(self):
        """The failure mode of a misrendered or missing config must be silence."""
        ledger, fp = self._ledger_with(10_000, severity="critical")
        promoted, _ = L.evaluate_gate(ledger, {}, [fp], NOW)
        self.assertEqual(promoted, [])


class PruneTests(unittest.TestCase):
    def test_drops_stale_findings_but_keeps_promoted_ones(self):
        ledger = L.empty_ledger()
        stale, _ = L.record_finding(
            ledger, finding(title="Stale", location="a.py:1"), "abc", NOW - dt.timedelta(days=45)
        )
        filed, _ = L.record_finding(
            ledger, finding(title="Filed", location="b.py:1"), "abc", NOW - dt.timedelta(days=45)
        )
        L.record_promotion(ledger, filed, "https://example.invalid/pr/1", "abc", NOW - dt.timedelta(days=44))
        L.prune(ledger, NOW)
        self.assertNotIn(stale, ledger["findings"])
        # Kept because forgetting it would let the loop re-file a pull request
        # that already exists.
        self.assertIn(filed, ledger["findings"])

    def test_drops_sightings_outside_the_window(self):
        ledger = L.empty_ledger()
        fp, _ = L.record_finding(ledger, finding(occurrences=5), "abc", NOW - dt.timedelta(hours=48))
        L.record_finding(ledger, finding(occurrences=2), "abc", NOW)
        L.prune(ledger, NOW)
        self.assertEqual(len(ledger["findings"][fp]["sightings"]), 1)


class CoerceTests(unittest.TestCase):
    def test_nonsense_becomes_an_empty_ledger_rather_than_an_exception(self):
        """The run that would have rewritten a corrupt ledger is the one that crashes on it."""
        for junk in (None, [], "", 7, {"findings": "not a dict"}):
            self.assertEqual(L.coerce(junk)["findings"], {})

    def test_run_history_is_bounded(self):
        raw = {"findings": {}, "runs": [{"n": i} for i in range(L.RUN_HISTORY + 20)]}
        self.assertEqual(len(L.coerce(raw)["runs"]), L.RUN_HISTORY)


class SummaryTests(unittest.TestCase):
    def test_the_brief_carries_the_fingerprint_the_agent_must_reuse(self):
        ledger = L.empty_ledger()
        fp, _ = L.record_finding(ledger, finding(occurrences=3), "abc", NOW)
        text = L.summarise_for_prompt(ledger, NOW)
        self.assertIn(fp, text)
        self.assertIn("Reconciler retries a Secret it cannot read", text)

    def test_an_empty_ledger_says_so_rather_than_rendering_a_blank(self):
        self.assertTrue(L.summarise_for_prompt(L.empty_ledger(), NOW).strip())


if __name__ == "__main__":
    unittest.main()
