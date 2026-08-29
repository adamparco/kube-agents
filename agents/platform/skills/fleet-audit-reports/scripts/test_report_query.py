"""Unit tests for report_query — the bounded read side of the report store.

Run:
  python3 -m unittest discover -s agents/platform/skills/fleet-audit-reports/scripts \
      -p 'test_report_query.py' -v

Stdlib only, matching the other agent-script tests. No pod and no cluster: the
store is a directory of JSON files, so every case here builds one in a temp
directory and drives the CLI through `main`.

The property most of these tests defend is boundedness (§4.9). `finding` is the
only subcommand allowed to return prose, and PROSE is planted in every
evidence, impact and recommendation field so that a subcommand which starts
leaking the document fails here rather than in a context window.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import report_query  # noqa: E402

AUDIT = "compliance-audit"
OTHER = "obtainability-audit"
PROSE = "PROSE-MARKER"


@contextlib.contextmanager
def no_helpers():
    """The module as it loads where the writer skill is not installed.

    Stood up rather than reproduced by breaking the checkout: the import
    happens once, at module load, so the only way to exercise the guard from
    inside the process is to put it in the state a failed import leaves.
    """
    saved, saved_error = report_query.report_status, report_query.IMPORT_ERROR
    report_query.report_status = None
    report_query.IMPORT_ERROR = (
        f"cannot import report_status from {report_query.HELPERS_DIR}: boom. "
        "The fleet-audit skill must be installed alongside this one — it owns "
        "the report store and the helpers that read it."
    )
    try:
        yield
    finally:
        report_query.report_status = saved
        report_query.IMPORT_ERROR = saved_error


def finding(fid, severity="critical", cluster="prod-us-east", check="netpol-missing"):
    """A finding shaped exactly as the validated document carries one."""
    return {
        "id": fid,
        "severity": severity,
        "title": f"{fid} needs attention",
        "cluster": cluster,
        "namespace": "payments",
        "object": "Namespace/payments",
        "evidence": {
            "command": "kubectl --context prod-us-east get networkpolicy -n payments",
            "excerpt": f"{PROSE} excerpt",
        },
        "impact": f"{PROSE} impact",
        "recommendation": {
            "action": f"{PROSE} action",
            "rationale": f"{PROSE} rationale",
            "risk": f"{PROSE} risk",
        },
        "check": check,
    }


def envelope(audit_id, finished_at, findings, **overrides):
    """One run's envelope, the keys `audit_report.report_envelope` writes."""
    body = {
        "audit_id": audit_id,
        "finished_at": finished_at,
        "status": "UPDATED",
        "issue_number": 128,
        "issue_url": "https://github.com/acme/fleet/issues/128",
        "partial": False,
        "coverage_gaps": [],
        "collect_s": 18.2,
        "inspect_s": 214.0,
        "publish_s": 41.5,
        "prs_opened": [],
        "prs_closed": [],
        "silent_ok": False,
        "new_ids": [],
        "resolved_ids": [],
        "current_ids": sorted(f["id"] for f in findings),
        "id_scheme": 2,
        "document": {
            "audit": audit_id,
            "scope": {
                "clusters": [{"name": "prod-us-east"}, {"name": "prod-autopilot"}],
                "skipped": [{"cluster": "dr-west", "reason": "control plane unreachable"}],
            },
            "findings": findings,
        },
    }
    body.update(overrides)
    return body


class StoreTestCase(unittest.TestCase):
    """A store on disk, and the CLI driven over it."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="report-store-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def stream_dir(self, audit_id):
        path = Path(self.root) / audit_id
        (path / "runs").mkdir(parents=True, exist_ok=True)
        return path

    def write_run(self, audit_id, stamp, findings, *, latest=True, **overrides):
        """One ring entry, and (by default) the `latest.json` copy of it."""
        directory = self.stream_dir(audit_id)
        text = json.dumps(
            envelope(audit_id, f"{stamp}+00:00", findings, **overrides), indent=2, sort_keys=True
        )
        (directory / "runs" / f"{stamp}.json").write_text(text, encoding="utf-8")
        if latest:
            (directory / "latest.json").write_text(text, encoding="utf-8")
        return f"{stamp}.json"

    def write_claim(self, audit_id, age_s):
        """The `started.json` a run holds while it is in flight."""
        directory = self.stream_dir(audit_id)
        (directory / "started.json").write_text(
            json.dumps({"t0": "2026-08-26T06:20:00+00:00", "epoch": time.time() - age_s,
                        "pid": 4, "nonce": "abc123"}),
            encoding="utf-8",
        )

    def query(self, *argv):
        """The CLI, as its caller sees it: an exit code and one JSON object."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = report_query.main(["--root", self.root, *argv])
        return code, json.loads(out.getvalue())

    def ok(self, *argv):
        code, payload = self.query(*argv)
        self.assertEqual(code, 0, payload.get("error"))
        self.assertIsNone(payload["error"])
        return payload

    def refused(self, *argv):
        code, payload = self.query(*argv)
        self.assertEqual(code, 2)
        self.assertTrue(payload["error"])
        return payload


class TestSharedHelpers(StoreTestCase):
    """§4.9: the two readers share one parser of the envelope."""

    def test_the_helpers_come_from_the_sibling_writer_skill(self):
        self.assertIsNotNone(
            report_query.report_status,
            f"report_status was not importable from {report_query.HELPERS_DIR}",
        )
        self.assertEqual(report_query.HELPERS_DIR.name, "scripts")
        self.assertEqual(report_query.HELPERS_DIR.parent.name, "fleet-audit")
        self.assertTrue((report_query.HELPERS_DIR / "report_status.py").is_file())
        self.assertEqual(
            Path(report_query.report_status.__file__).resolve(),
            report_query.HELPERS_DIR / "report_status.py",
        )

    def test_a_missing_sibling_is_reported_and_never_fallen_back_from(self):
        """The guard names the path it looked in and answers nothing else."""
        self.assertIsNone(report_query.IMPORT_ERROR)
        with no_helpers():
            code, payload = self.query("streams")
        self.assertEqual(code, 2)
        self.assertIn(str(report_query.HELPERS_DIR), payload["error"])
        self.assertIn("fleet-audit skill must be installed", payload["error"])
        self.assertEqual(payload["looked_in"], str(report_query.HELPERS_DIR))
        self.assertNotIn("streams", payload)


class TestStreams(StoreTestCase):
    def test_a_completed_stream_reports_its_counts(self):
        self.write_run(
            AUDIT,
            "20260826T063100.000000Z",
            [finding("a"), finding("b", severity="minor")],
            new_ids=["a"],
            resolved_ids=["x", "y"],
        )
        row = self.ok("streams")["streams"][0]
        self.assertEqual(row["audit_id"], AUDIT)
        self.assertEqual(row["liveness"], "completed")
        self.assertEqual(row["findings"], 2)
        self.assertEqual(row["critical"], 1)
        self.assertEqual(row["new"], 1)
        self.assertEqual(row["resolved"], 2)
        self.assertEqual(row["current"], 2)
        self.assertEqual(row["clusters"], 2)
        self.assertEqual(row["skipped"], 1)
        self.assertEqual(row["runs"], 1)
        self.assertEqual(row["issue_number"], 128)

    def test_gaps_are_a_count_not_eight_streams_of_prose(self):
        self.write_run(
            AUDIT,
            "20260826T063100.000000Z",
            [],
            partial=True,
            coverage_gaps=["dr-west: control plane unreachable", "prod-autopilot: 7/11 checks"],
        )
        row = self.ok("streams")["streams"][0]
        self.assertTrue(row["partial"])
        self.assertEqual(row["gaps"], 2)

    def test_running_and_never_are_told_apart(self):
        self.write_claim(AUDIT, age_s=60)
        self.stream_dir(OTHER)
        rows = {row["audit_id"]: row for row in self.ok("streams")["streams"]}
        self.assertEqual(rows[AUDIT]["liveness"], "running")
        self.assertLess(rows[AUDIT]["age_s"], 7200)
        self.assertEqual(rows[OTHER]["liveness"], "never")
        self.assertIsNone(rows[OTHER]["finished_at"])

    def test_a_run_past_the_ceiling_is_dead(self):
        self.write_claim(AUDIT, age_s=report_query.report_status.CEILING_S + 60)
        self.assertEqual(self.ok("streams")["streams"][0]["liveness"], "died")

    def test_an_absent_root_is_an_error_not_an_empty_fleet(self):
        shutil.rmtree(self.root)
        code, payload = self.query("streams")
        self.assertEqual(code, 2)
        self.assertFalse(payload["root_exists"])
        self.assertIn("unknown, not clean", payload["error"])
        self.assertEqual(payload["streams"], [])

    def test_an_unparseable_envelope_is_an_error_row_and_a_nonzero_exit(self):
        self.stream_dir(AUDIT)
        (Path(self.root) / AUDIT / "latest.json").write_text("{not json", encoding="utf-8")
        code, payload = self.query("streams")
        self.assertEqual(code, 2)
        self.assertIn(AUDIT, payload["error"])
        row = payload["streams"][0]
        self.assertEqual(row["liveness"], "error")
        self.assertIn("latest.json", row["error"])

    def test_one_broken_stream_does_not_hide_the_others(self):
        self.write_run(OTHER, "20260826T070500.000000Z", [finding("q")])
        self.stream_dir(AUDIT)
        (Path(self.root) / AUDIT / "latest.json").write_text("[]", encoding="utf-8")
        rows = {row["audit_id"]: row for row in self.query("streams")[1]["streams"]}
        self.assertEqual(rows[OTHER]["findings"], 1)
        self.assertEqual(rows[AUDIT]["liveness"], "error")


class TestShow(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.older = self.write_run(
            AUDIT, "20260825T063100.000000Z", [finding("a")], latest=False
        )
        self.newest = self.write_run(
            AUDIT, "20260826T063100.000000Z", [finding("a"), finding("b", severity="major")]
        )

    def test_the_document_never_crosses_this_boundary(self):
        payload = self.ok("show", AUDIT)
        self.assertEqual(payload["run"], "latest.json")
        self.assertNotIn("document", payload["envelope"])
        self.assertNotIn(PROSE, json.dumps(payload))

    def test_it_carries_the_outcome_the_delta_and_the_durations(self):
        row = self.ok("show", AUDIT)["envelope"]
        self.assertEqual(row["status"], "UPDATED")
        self.assertEqual(row["issue_url"], "https://github.com/acme/fleet/issues/128")
        self.assertEqual(row["findings"], 2)
        self.assertEqual(row["critical"], 1)
        self.assertEqual(row["publish_s"], 41.5)
        self.assertEqual(row["coverage_gaps"], [])

    def test_a_stamp_reads_that_run_with_or_without_the_extension(self):
        for spelling in (self.older, self.older[: -len(".json")]):
            with self.subTest(run=spelling):
                payload = self.ok("show", AUDIT, "--run", spelling)
                self.assertEqual(payload["run"], self.older)
                self.assertEqual(payload["envelope"]["findings"], 1)

    def test_root_may_follow_the_subcommand(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = report_query.main(["show", AUDIT, "--root", self.root])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["root"], self.root)

    def test_an_absent_stamp_names_the_ring(self):
        payload = self.refused("show", AUDIT, "--run", "20990101T000000.000000Z")
        self.assertIn("20990101T000000.000000Z.json", payload["error"])
        self.assertEqual(payload["runs"], [self.older, self.newest])

    def test_an_absent_stream_lists_the_streams_that_exist(self):
        payload = self.refused("show", "ai-security-audit")
        self.assertIn("ai-security-audit", payload["error"])
        self.assertEqual(payload["streams"], [AUDIT])

    def test_an_absent_store_says_so_rather_than_answering(self):
        shutil.rmtree(self.root)
        payload = self.refused("show", AUDIT)
        self.assertFalse(payload["root_exists"])
        self.assertIn("report store not found", payload["error"])


class TestUnknownIsNotClean(StoreTestCase):
    """A stream with no `latest.json` has no record, which is not a clean run."""

    def test_a_missing_latest_is_unknown(self):
        self.stream_dir(AUDIT)
        payload = self.refused("show", AUDIT)
        self.assertIn("unknown, not clean", payload["error"])
        self.assertEqual(payload["liveness"], "never")
        self.assertEqual(payload["runs"], [])

    def test_a_failed_write_leaves_the_ring_and_says_which_run_it_lost(self):
        """`write_report` unlinks `latest.json` when its write fails, so the
        ring can hold entries while the newest record is gone."""
        self.write_run(AUDIT, "20260826T063100.000000Z", [finding("a")])
        os.unlink(Path(self.root) / AUDIT / "latest.json")
        payload = self.refused("findings", AUDIT)
        self.assertIn("unknown, not clean", payload["error"])
        self.assertEqual(payload["runs"], ["20260826T063100.000000Z.json"])
        self.assertEqual(payload["liveness"], "never")


class TestFindings(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.write_run(
            AUDIT,
            "20260826T063100.000000Z",
            [
                finding("minor-one", severity="minor", cluster="prod-autopilot", check="hostpath"),
                finding("crit-one"),
                finding("major-one", severity="major", cluster="prod-autopilot"),
                finding("crit-two", cluster="prod-autopilot"),
            ],
        )

    def test_identity_columns_only(self):
        payload = self.ok("findings", AUDIT)
        self.assertEqual(payload["total"], 4)
        self.assertEqual(payload["returned"], 4)
        self.assertFalse(payload["truncated"])
        for row in payload["findings"]:
            self.assertEqual(
                sorted(row), ["check", "cluster", "id", "severity", "title"]
            )
        self.assertNotIn(PROSE, json.dumps(payload))

    def test_severity_first_so_truncation_eats_the_least_severe_end(self):
        payload = self.ok("findings", AUDIT, "--limit", "2")
        self.assertEqual([row["id"] for row in payload["findings"]], ["crit-one", "crit-two"])
        self.assertEqual(payload["matched"], 4)
        self.assertEqual(payload["returned"], 2)
        self.assertTrue(payload["truncated"])

    def test_the_filters_are_exact_and_case_insensitive(self):
        cases = (
            (("--severity", "critical"), ["crit-one", "crit-two"]),
            (("--severity", "CRITICAL"), ["crit-one", "crit-two"]),
            (("--cluster", "prod-us-east"), ["crit-one"]),
            (("--check", "hostpath"), ["minor-one"]),
            (("--severity", "critical", "--cluster", "prod-autopilot"), ["crit-two"]),
            (("--cluster", "dr-west"), []),
        )
        for flags, expected in cases:
            with self.subTest(flags=flags):
                payload = self.ok("findings", AUDIT, *flags)
                self.assertEqual([row["id"] for row in payload["findings"]], expected)
                self.assertEqual(payload["matched"], len(expected))
                self.assertEqual(payload["total"], 4)

    def test_a_clean_run_is_zero_findings_and_not_an_error(self):
        self.write_run(OTHER, "20260826T070500.000000Z", [], status="CLEAN")
        payload = self.ok("findings", OTHER)
        self.assertEqual(payload["findings"], [])
        self.assertEqual(payload["status"], "CLEAN")

    def test_a_document_that_is_not_a_document_is_refused(self):
        self.write_run(OTHER, "20260826T070500.000000Z", [], document=None)
        self.assertIn("no findings document", self.refused("findings", OTHER)["error"])


class TestFinding(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.write_run(
            AUDIT,
            "20260826T063100.000000Z",
            [finding("netpol-missing-payments"), finding("other", severity="minor")],
        )

    def test_the_one_path_that_returns_prose(self):
        payload = self.ok("finding", AUDIT, "netpol-missing-payments")
        body = payload["finding"]
        self.assertEqual(body["id"], "netpol-missing-payments")
        self.assertEqual(body["impact"], f"{PROSE} impact")
        self.assertEqual(body["recommendation"]["risk"], f"{PROSE} risk")
        self.assertEqual(body["evidence"]["excerpt"], f"{PROSE} excerpt")

    def test_an_unknown_id_offers_candidates_rather_than_the_roster(self):
        payload = self.refused("finding", AUDIT, "netpol-missing-payment")
        self.assertIn("netpol-missing-payment", payload["error"])
        self.assertEqual(payload["available"], 2)
        self.assertEqual(payload["ids"], ["netpol-missing-payments", "other"])

    def test_the_hint_list_is_capped(self):
        many = [finding(f"f{index:03d}", severity="minor") for index in range(60)]
        self.write_run(OTHER, "20260826T070500.000000Z", many)
        payload = self.refused("finding", OTHER, "nope")
        self.assertEqual(payload["available"], 60)
        self.assertEqual(len(payload["ids"]), report_query.MAX_ID_HINTS)


class TestDiff(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.first = self.write_run(
            AUDIT, "20260824T063100.000000Z", [finding("a"), finding("b")], latest=False
        )
        self.second = self.write_run(
            AUDIT, "20260825T063100.000000Z", [finding("b"), finding("c")], latest=False
        )
        self.third = self.write_run(
            AUDIT, "20260826T063100.000000Z", [finding("b"), finding("d", severity="minor")]
        )

    def test_it_defaults_to_the_newest_two(self):
        payload = self.ok("diff", AUDIT)
        self.assertEqual((payload["from"], payload["to"]), (self.second, self.third))
        self.assertEqual([row["id"] for row in payload["added"]], ["d"])
        self.assertEqual([row["id"] for row in payload["resolved"]], ["c"])
        self.assertEqual(payload["unchanged"], 1)
        self.assertNotIn(PROSE, json.dumps(payload))

    def test_added_and_resolved_carry_titles(self):
        self.assertEqual(
            self.ok("diff", AUDIT)["added"][0]["title"], "d needs attention"
        )

    def test_explicit_stamps_span_the_whole_ring(self):
        payload = self.ok("diff", AUDIT, "--from", self.first, "--to", self.third)
        self.assertEqual([row["id"] for row in payload["added"]], ["d"])
        self.assertEqual([row["id"] for row in payload["resolved"]], ["a"])
        self.assertEqual(payload["added_total"], 1)
        self.assertEqual(payload["resolved_total"], 1)

    def test_to_alone_diffs_against_the_run_before_it(self):
        payload = self.ok("diff", AUDIT, "--to", self.second)
        self.assertEqual(payload["from"], self.first)
        self.assertEqual([row["id"] for row in payload["resolved"]], ["a"])

    def test_the_oldest_entry_has_nothing_behind_it(self):
        payload = self.refused("diff", AUDIT, "--to", self.first)
        self.assertIn("oldest entry", payload["error"])
        self.assertEqual(len(payload["runs"]), 3)

    def test_an_empty_ring_is_refused(self):
        self.stream_dir(OTHER)
        self.assertIn("ring is empty", self.refused("diff", OTHER)["error"])

    def test_a_single_entry_ring_is_refused(self):
        self.write_run(OTHER, "20260826T070500.000000Z", [finding("z")])
        self.assertIn("oldest entry", self.refused("diff", OTHER)["error"])

    def test_an_unknown_stamp_names_the_ring(self):
        payload = self.refused("diff", AUDIT, "--to", "20990101T000000.000000Z")
        self.assertEqual(payload["runs"], [self.first, self.second, self.third])

    def test_the_lists_are_bounded(self):
        self.write_run(
            OTHER,
            "20260825T070500.000000Z",
            [finding(f"old{index}", severity="minor") for index in range(5)],
            latest=False,
        )
        self.write_run(
            OTHER,
            "20260826T070500.000000Z",
            [finding(f"new{index}", severity="minor") for index in range(5)],
        )
        payload = self.ok("diff", OTHER, "--limit", "2")
        self.assertEqual(len(payload["added"]), 2)
        self.assertEqual(payload["added_total"], 5)
        self.assertEqual(payload["resolved_total"], 5)
        self.assertTrue(payload["truncated"])


class TestRuns(StoreTestCase):
    def test_it_lists_stamps_without_reading_a_single_envelope(self):
        first = self.write_run(AUDIT, "20260825T063100.000000Z", [finding("a")], latest=False)
        second = self.write_run(AUDIT, "20260826T063100.000000Z", [finding("a")])
        # A ring entry nothing can parse. `runs` still answers, because a stamp
        # listing that reads fourteen documents is the cost this command exists
        # to avoid.
        broken = Path(self.root) / AUDIT / "runs" / "20260827T063100.000000Z.json"
        broken.write_text("{not json", encoding="utf-8")
        payload = self.ok("runs", AUDIT)
        self.assertEqual(payload["runs"], [first, second, broken.name])
        self.assertEqual(payload["count"], 3)
        self.assertEqual(payload["newest"], broken.name)
        self.assertEqual(payload["liveness"], "completed")

    def test_a_temp_file_mid_write_is_not_a_run(self):
        self.write_run(AUDIT, "20260826T063100.000000Z", [finding("a")])
        (Path(self.root) / AUDIT / "runs" / "tmpabc123.tmp").write_text("{", encoding="utf-8")
        self.assertEqual(self.ok("runs", AUDIT)["count"], 1)

    def test_an_absent_stream_is_refused(self):
        self.assertEqual(self.refused("runs", AUDIT)["streams"], [])


class TestBoundedOutput(StoreTestCase):
    """The rule §4.9 exists for: only `finding` returns prose."""

    def test_no_other_subcommand_emits_the_document(self):
        self.write_run(AUDIT, "20260825T063100.000000Z", [finding("a")], latest=False)
        self.write_run(AUDIT, "20260826T063100.000000Z", [finding("a"), finding("b")])
        for argv in (
            ("streams",),
            ("show", AUDIT),
            ("findings", AUDIT),
            ("diff", AUDIT),
            ("runs", AUDIT),
        ):
            with self.subTest(command=argv[0]):
                self.assertNotIn(PROSE, json.dumps(self.ok(*argv)))

    def test_every_answer_carries_an_error_key(self):
        """Null on success, a sentence on failure — so a caller never has to
        tell an absent key from a null one."""
        self.write_run(AUDIT, "20260826T063100.000000Z", [finding("a")])
        self.assertIn("error", self.ok("show", AUDIT))
        self.assertIn("error", self.refused("show", OTHER))


if __name__ == "__main__":
    unittest.main()
