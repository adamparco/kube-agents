"""Tests for the fleet-audit status view.

The contract under test is the read side of
docs/designs/fleet-audit-collectors-and-status.md §4.6: offline `--file`
loading, the scrub boundary, degradation to placeholders instead of crashes,
and the three flags (STALE, DIED, partial `⚠`) that make the raw rows
trustworthy.
"""

import contextlib
import io
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_audit_status_view as view  # noqa: E402

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def cm_with(streams):
    return {
        "metadata": {"resourceVersion": "1"},
        "data": {f"{k}.json": json.dumps(v) for k, v in streams.items()},
    }


def row(**overrides):
    base = {
        "at": "2026-08-26T06:31:12+00:00",
        "phase": "finished",
        "status": "UPDATED",
        "partial": False,
        "new": 3,
        "resolved": 1,
        "findings": 57,
        "critical": 2,
        "prs_opened": 1,
        "prs_closed": 0,
        "issue_url": "https://github.com/acme/fleet/issues/12",
        "inspect_s": 214.0,
        "publish_s": 41.5,
        "note": "",
    }
    base.update(overrides)
    return base


class TestScrub(unittest.TestCase):
    def test_control_characters_never_reach_the_terminal(self):
        self.assertEqual(view.scrub("a\x1b]8;;evil\x07b"), "a�]8;;evil�b")

    def test_none_becomes_empty(self):
        self.assertEqual(view.scrub(None), "")


class TestLoadStreams(unittest.TestCase):
    def test_parses_stream_keys_and_ignores_foreign_ones(self):
        cm = cm_with({"compliance-audit": {"last": row()}})
        cm["data"]["README"] = "not a stream"
        streams = view.load_streams(cm)
        self.assertIn("compliance-audit", streams)
        self.assertNotIn("README", streams)

    def test_a_corrupt_key_becomes_an_error_row_not_a_crash(self):
        cm = {"data": {"compliance-audit.json": "not json"}}
        self.assertEqual(
            view.load_streams(cm)["compliance-audit"],
            {"error": "unparseable stream document"},
        )


class TestNextFire(unittest.TestCase):
    def test_daily(self):
        after = datetime(2026, 8, 26, 6, 31, tzinfo=timezone.utc)
        fire = view.next_fire("20 6 * * *", after)
        self.assertEqual(fire, datetime(2026, 8, 27, 6, 20, tzinfo=timezone.utc))

    def test_weekly_monday(self):
        # 2026-08-26 is a Wednesday; cron dow 1 is Monday.
        after = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
        fire = view.next_fire("20 7 * * 1", after)
        self.assertEqual(fire, datetime(2026, 8, 31, 7, 20, tzinfo=timezone.utc))

    def test_anything_fancier_abstains(self):
        self.assertIsNone(view.next_fire("*/5 * * * *", NOW))
        self.assertIsNone(view.next_fire("20 6 1 * *", NOW))
        self.assertIsNone(view.next_fire("garbage", NOW))


class TestFlags(unittest.TestCase):
    JOB = {"enabled": True, "expr": "20 6 * * *"}

    def test_a_recent_run_is_not_stale(self):
        doc = {"last": row(at=(NOW - timedelta(hours=2)).isoformat())}
        self.assertEqual(view.flags_for(doc, self.JOB, NOW), [])

    def test_a_missed_fire_is_stale(self):
        doc = {"last": row(at=(NOW - timedelta(days=3)).isoformat())}
        self.assertEqual(view.flags_for(doc, self.JOB, NOW), ["STALE"])

    def test_a_stale_started_stub_is_a_death(self):
        doc = {
            "last": row(at=(NOW - timedelta(days=3)).isoformat(), phase="started")
        }
        self.assertEqual(view.flags_for(doc, self.JOB, NOW), ["STALE", "DIED"])

    def test_an_in_flight_run_never_trips_died(self):
        doc = {"last": row(at=(NOW - timedelta(minutes=5)).isoformat(), phase="started")}
        self.assertEqual(view.flags_for(doc, self.JOB, NOW), [])

    def test_a_disabled_stream_abstains(self):
        doc = {"last": row(at=(NOW - timedelta(days=30)).isoformat())}
        self.assertEqual(view.flags_for(doc, {"enabled": False}, NOW), [])


class TestRender(unittest.TestCase):
    ROSTER = {"compliance-audit": {"enabled": True, "expr": "20 6 * * *"}}

    def test_a_full_row_renders_its_fields(self):
        out = view.render(
            {"compliance-audit": {"last": row()}}, self.ROSTER, NOW, "jobs.json"
        )
        self.assertIn("compliance-audit", out)
        self.assertIn("UPDATED", out)
        self.assertIn("57 (2 c)", out)
        self.assertIn("+3 / −1", out)
        self.assertIn("3m34s", out)
        self.assertIn("#12", out)

    def test_a_rostered_stream_with_no_rows_reads_never_ran(self):
        out = view.render({}, self.ROSTER, NOW, "jobs.json")
        self.assertIn("never ran", out)

    def test_partial_runs_warn_and_print_their_gaps(self):
        doc = {"last": row(partial=True, note="prod-eu-1: API server unreachable")}
        out = view.render({"compliance-audit": doc}, self.ROSTER, NOW, "jobs.json")
        self.assertIn("⚠", out)
        self.assertIn("coverage gaps:", out)
        self.assertIn("prod-eu-1", out)

    def test_an_unknown_status_renders_as_a_warning_not_success(self):
        doc = {"last": row(status="SOMETHING_NEW")}
        out = view.render({"compliance-audit": doc}, self.ROSTER, NOW, "jobs.json")
        self.assertIn("SOMETHING_NEW ?", out)

    def test_a_started_stub_reads_as_running(self):
        doc = {"last": row(phase="started", at=NOW.isoformat())}
        out = view.render({"compliance-audit": doc}, self.ROSTER, NOW, "jobs.json")
        self.assertIn("running…", out)

    def test_the_note_is_scrubbed(self):
        doc = {"last": row(partial=True, note="bad\x1b]8;;x\x07note")}
        out = view.render({"compliance-audit": doc}, self.ROSTER, NOW, "jobs.json")
        self.assertNotIn("\x1b", out)

    def test_last_run_uses_the_system_local_zone_by_default(self):
        # No tz argument: local_time() must consult the machine's zone, not a
        # hardcoded one — assert it against the same conversion, not a fixed
        # clock time, so the test does not encode any particular zone either.
        at = datetime.fromisoformat(row()["at"])
        self.assertIn(view.local_time(at), view.render(
            {"compliance-audit": {"last": row()}}, self.ROSTER, NOW, "jobs.json"
        ))

    def test_local_time_honors_an_explicit_zone(self):
        # 06:31 UTC on 2026-08-26 is 2:31 am US/Eastern (EDT) — used here only
        # to prove the conversion works, not as the tool's default.
        at = datetime.fromisoformat(row()["at"])
        self.assertEqual(
            view.local_time(at, timezone(timedelta(hours=-4))), "Aug 26 2:31 am"
        )


class TestMainOffline(unittest.TestCase):
    def test_file_mode_needs_no_kubectl(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "cm.json"
            path.write_text(
                json.dumps(cm_with({"compliance-audit": {"last": row()}})),
                encoding="utf-8",
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = view.main(["--file", str(path), "--roster", "/nonexistent"])
        self.assertEqual(rc, 0)
        self.assertIn("compliance-audit", out.getvalue())

    def test_json_mode_emits_the_merged_documents(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "cm.json"
            path.write_text(
                json.dumps(cm_with({"compliance-audit": {"last": row()}})),
                encoding="utf-8",
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = view.main(
                    ["--file", str(path), "--roster", "/nonexistent", "--json"]
                )
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertIn("compliance-audit", payload["streams"])


class TestRosterLoading(unittest.TestCase):
    def test_the_checked_in_roster_yields_the_eight_streams(self):
        roster = view.load_roster(view.DEFAULT_ROSTER)
        self.assertGreaterEqual(len(roster), 8)
        self.assertIn("compliance-audit", roster)
        for job in roster.values():
            self.assertIn("expr", job)

    def test_a_missing_roster_degrades_to_empty(self):
        self.assertEqual(view.load_roster(Path("/nonexistent")), {})


class TestFormatting(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(view.duration(214.0), "3m34s")
        self.assertEqual(view.duration(41.5), "41s")
        self.assertEqual(view.duration(None), "?")

    def test_issue_ref(self):
        self.assertEqual(view.issue_ref("https://github.com/a/b/issues/12"), "#12")
        self.assertEqual(view.issue_ref(None), "—")


if __name__ == "__main__":
    unittest.main()
