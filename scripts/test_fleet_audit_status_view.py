"""Tests for the fleet-audit status view.

The contract under test is the read side of
docs/designs/fleet-audit-collectors-and-status.md §4.6: the one-projection read
(`kubectl exec -i … -- python3 -` with report_status.py on stdin), the
`--json`/`--file` round trip that makes the view reproducible off-cluster, the
four flags (NO STORE, DIED, NEVER, STALE), and the exit codes that keep "I
could not look" from rendering as "nothing is wrong".

The subprocess boundary is stubbed everywhere; no test reaches a cluster.
"""

import contextlib
import io
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_audit_status_view as view  # noqa: E402

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def latest(**overrides):
    """One projected `latest` — report_status.py's envelope minus `document`,
    plus the derived counts."""
    base = {
        "audit_id": "compliance-audit",
        "finished_at": "2026-08-26T06:31:12+00:00",
        "status": "UPDATED",
        "issue_number": 12,
        "issue_url": "https://github.com/acme/fleet/issues/12",
        "partial": False,
        "coverage_gaps": [],
        "collect_s": 95.0,
        "inspect_s": 214.0,
        "publish_s": 41.5,
        "prs_opened": ["https://github.com/acme/fleet/pull/9"],
        "prs_closed": [],
        "silent_ok": None,
        "id_scheme": "sha1-12",
        "new": 3,
        "resolved": 1,
        "current": 57,
        "findings": 57,
        "critical": 2,
        "clusters": 4,
        "skipped": 0,
    }
    base.update(overrides)
    return base


def stream(liveness="completed", last=None, started=None, error=None, runs=()):
    return {
        "started": started,
        "latest": last,
        "runs": list(runs),
        "liveness": liveness,
        "error": error,
    }


def projection(streams=None, root_exists=True):
    return {
        "root": "/opt/data/fleet-audit/reports",
        "root_exists": root_exists,
        "generated_at": NOW.isoformat(),
        "ceiling_s": 7200,
        "streams": streams or {},
    }


def started(age_s=300.0):
    epoch = NOW.timestamp() - age_s
    return {
        "t0": datetime.fromtimestamp(epoch, timezone.utc).isoformat(),
        "epoch": epoch,
        "age_s": age_s,
        "pid": 4321,
        "nonce": "beefcafe",
    }


class FakeKubectl:
    """The subprocess boundary, recorded and canned. `get pods` answers with
    the pod list; `exec` answers with the projection."""

    def __init__(
        self,
        pods=("agent-0",),
        get_rc=0,
        get_stderr="",
        exec_rc=0,
        exec_stdout=None,
        exec_stderr="",
    ):
        self.pods = list(pods)
        self.get_rc = get_rc
        self.get_stderr = get_stderr
        self.exec_rc = exec_rc
        self.exec_stdout = (
            json.dumps(projection()) if exec_stdout is None else exec_stdout
        )
        self.exec_stderr = exec_stderr
        self.calls = []

    def __call__(self, cmd, capture_output=False, text=False, input=None):
        self.calls.append({"cmd": list(cmd), "input": input})
        if "get" in cmd:
            return CompletedProcess(cmd, self.get_rc, " ".join(self.pods), self.get_stderr)
        return CompletedProcess(cmd, self.exec_rc, self.exec_stdout, self.exec_stderr)

    @property
    def exec_call(self):
        return next(c for c in self.calls if "exec" in c["cmd"])


def run_main(argv, fake=None):
    """main() with the subprocess boundary stubbed. Returns (rc, out, err)."""
    fake = fake or FakeKubectl()
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(view.subprocess, "run", fake):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = view.main(argv)
    return rc, out.getvalue(), err.getvalue()


class TestScrub(unittest.TestCase):
    def test_control_characters_never_reach_the_terminal(self):
        self.assertEqual(view.scrub("a\x1b]8;;evil\x07b"), "a�]8;;evil�b")

    def test_none_becomes_empty(self):
        self.assertEqual(view.scrub(None), "")


class TestAsProjection(unittest.TestCase):
    def test_a_valid_document_passes(self):
        self.assertEqual(view.as_projection(json.dumps(projection()), "x")["ceiling_s"], 7200)

    def test_non_json_is_an_exit_2_error_not_an_empty_fleet(self):
        with self.assertRaises(view.ProjectionError) as caught:
            view.as_projection("Traceback (most recent call last):", "pod")
        self.assertIn("not JSON", str(caught.exception))

    def test_json_without_streams_is_rejected(self):
        with self.assertRaises(view.ProjectionError):
            view.as_projection('{"root": "/x"}', "pod")


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

    def flags(self, stream_doc, job=None, root_exists=True):
        return view.flags_for(stream_doc, job or self.JOB, NOW, root_exists)

    def test_a_recent_run_carries_no_flag(self):
        recent = latest(finished_at=(NOW - timedelta(hours=2)).isoformat())
        self.assertEqual(self.flags(stream(last=recent)), [])

    def test_a_missing_store_is_no_store(self):
        self.assertEqual(self.flags(stream(), root_exists=False), ["NO STORE"])

    def test_an_unreadable_stream_is_no_store(self):
        doc = stream(liveness="error", error="latest.json: not a JSON object")
        self.assertEqual(self.flags(doc), ["NO STORE"])

    def test_died_needs_no_roster_and_no_schedule(self):
        doc = stream(liveness="died", started=started(age_s=9000))
        self.assertEqual(view.flags_for(doc, {}, NOW, True), ["DIED"])

    def test_an_in_flight_run_never_trips_died(self):
        doc = stream(liveness="running", started=started(age_s=300))
        self.assertEqual(self.flags(doc), [])

    def test_never_fires_when_the_store_was_readable(self):
        self.assertEqual(self.flags(stream(liveness="never")), ["NEVER"])

    def test_never_does_not_fire_when_the_store_was_not(self):
        # The store is the thing that failed; claiming the stream never ran
        # would be the ConfigMap's silent lie in a new place.
        self.assertEqual(self.flags(stream(liveness="never"), root_exists=False), ["NO STORE"])

    def test_a_stream_absent_from_the_projection_reads_as_never(self):
        self.assertEqual(self.flags({}), ["NEVER"])

    def test_a_missed_fire_is_stale(self):
        old = latest(finished_at=(NOW - timedelta(days=3)).isoformat())
        self.assertEqual(self.flags(stream(last=old)), ["STALE"])

    def test_a_disabled_stream_abstains_from_stale_and_never(self):
        old = latest(finished_at=(NOW - timedelta(days=30)).isoformat())
        self.assertEqual(self.flags(stream(last=old), {"enabled": False}), [])
        self.assertEqual(self.flags(stream(liveness="never"), {"enabled": False}), [])


class TestRender(unittest.TestCase):
    ROSTER = {"compliance-audit": {"enabled": True, "expr": "20 6 * * *"}}

    def render(self, streams, roster=None, root_exists=True):
        return view.render(
            projection(streams, root_exists=root_exists),
            self.ROSTER if roster is None else roster,
            NOW,
            "jobs.json",
            "ns/agent-0 [platform-agent]",
        )

    def test_a_full_row_renders_its_fields(self):
        out = self.render({"compliance-audit": stream(last=latest())})
        self.assertIn("compliance-audit", out)
        self.assertIn("UPDATED", out)
        self.assertIn("57 (2 c)", out)
        self.assertIn("+3 / −1", out)
        self.assertIn("1m35s", out)  # collect_s
        self.assertIn("3m34s", out)  # inspect_s
        self.assertIn("#12", out)

    def test_the_prs_column_counts_the_url_list(self):
        urls = ["https://x/pull/1", "https://x/pull/2"]
        out = self.render({"compliance-audit": stream(last=latest(prs_opened=urls))})
        self.assertRegex(out, r"\s2\s")

    def test_the_header_names_the_store_and_the_source(self):
        out = self.render({})
        self.assertIn("/opt/data/fleet-audit/reports", out)
        self.assertIn("ns/agent-0 [platform-agent]", out)
        self.assertIn("roster: jobs.json", out)

    def test_a_rostered_stream_with_no_files_reads_never_ran(self):
        out = self.render({})
        self.assertIn("never ran", out)
        self.assertIn("NEVER", out)

    def test_partial_runs_warn_and_print_their_gaps(self):
        gaps = ["prod-eu-1: API server unreachable"]
        out = self.render(
            {"compliance-audit": stream(last=latest(partial=True, coverage_gaps=gaps))}
        )
        self.assertIn("⚠", out)
        self.assertIn("coverage gaps:", out)
        self.assertIn("prod-eu-1", out)

    def test_an_unknown_status_renders_as_a_warning_not_success(self):
        out = self.render({"compliance-audit": stream(last=latest(status="SOMETHING_NEW"))})
        self.assertIn("SOMETHING_NEW ?", out)

    def test_a_held_stream_reads_as_running(self):
        doc = stream(liveness="running", started=started(300), last=latest())
        self.assertIn("running…", self.render({"compliance-audit": doc}))

    def test_a_stream_error_reaches_the_status_cell(self):
        doc = stream(liveness="error", error="started.json: not a JSON object")
        out = self.render({"compliance-audit": doc})
        self.assertIn("started.json: not a JSON object", out)
        self.assertIn("NO STORE", out)

    def test_a_missing_store_says_so_below_the_table(self):
        out = self.render({}, root_exists=False)
        self.assertIn("store directory absent on the pod", out)

    def test_a_long_coverage_gap_is_clipped_to_one_line(self):
        """The table is what this view exists to show, and gaps can bury it.

        The live install writes four-sentence gaps explaining a refused `gcloud`
        flag; six of those scroll the table off the terminal. The full text
        stays in the envelope for `fleet-audit-reports` to read.
        """
        gap = "prod-eu-1: " + "the api server refused the read. " * 20
        out = self.render(
            {"compliance-audit": stream(last=latest(partial=True, coverage_gaps=[gap]))}
        )
        printed = next(line for line in out.splitlines() if "prod-eu-1" in line)
        self.assertLessEqual(len(printed.strip()), view.GAP_WIDTH)
        self.assertTrue(printed.endswith("…"))
        self.assertIn("prod-eu-1", printed)

    def test_a_multi_line_coverage_gap_stays_on_one_line(self):
        # A collector that writes a newline into a gap would otherwise split the
        # cell across two lines and misalign nothing but read as two gaps.
        gap = "prod-eu-1:\nthe api server\nrefused the read"
        out = self.render(
            {"compliance-audit": stream(last=latest(partial=True, coverage_gaps=[gap]))}
        )
        self.assertIn("  compliance-audit: prod-eu-1: the api server refused the read", out)

    def test_a_short_coverage_gap_is_printed_whole(self):
        gap = "prod-eu-1: API server unreachable"
        out = self.render(
            {"compliance-audit": stream(last=latest(partial=True, coverage_gaps=[gap]))}
        )
        self.assertIn(f"  compliance-audit: {gap}", out)
        self.assertNotIn("…", out)

    def test_coverage_gaps_are_scrubbed(self):
        gaps = ["bad\x1b]8;;x\x07gap"]
        out = self.render(
            {"compliance-audit": stream(last=latest(partial=True, coverage_gaps=gaps))}
        )
        self.assertNotIn("\x1b", out)

    def test_last_run_uses_the_system_local_zone_by_default(self):
        # No tz argument: local_time() must consult the machine's zone, not a
        # hardcoded one — assert it against the same conversion, not a fixed
        # clock time, so the test does not encode any particular zone either.
        at = datetime.fromisoformat(latest()["finished_at"])
        out = self.render({"compliance-audit": stream(last=latest())})
        self.assertIn(view.local_time(at), out)

    def test_local_time_honors_an_explicit_zone(self):
        # 06:31 UTC on 2026-08-26 is 2:31 am US/Eastern (EDT) — used here only
        # to prove the conversion works, not as the tool's default.
        at = datetime.fromisoformat(latest()["finished_at"])
        self.assertEqual(
            view.local_time(at, timezone(timedelta(hours=-4))), "Aug 26 2:31 am"
        )


class TestProjectionRead(unittest.TestCase):
    def test_the_script_is_streamed_in_on_stdin(self):
        fake = FakeKubectl()
        rc, _, _ = run_main(["--roster", "/nonexistent"], fake)
        self.assertEqual(rc, 0)
        call = fake.exec_call
        self.assertEqual(call["cmd"][-3:], ["--", "python3", "-"])
        self.assertIn("-i", call["cmd"])
        self.assertEqual(call["input"], view.PROJECTION_SCRIPT.read_text(encoding="utf-8"))

    def test_the_container_defaults_to_the_agent_not_the_sidecar(self):
        fake = FakeKubectl()
        run_main(["--roster", "/nonexistent"], fake)
        cmd = fake.exec_call["cmd"]
        self.assertEqual(cmd[cmd.index("-c") + 1], "platform-agent")

    def test_an_explicit_container_overrides_it(self):
        fake = FakeKubectl()
        run_main(["--roster", "/nonexistent", "--container", "other"], fake)
        cmd = fake.exec_call["cmd"]
        self.assertEqual(cmd[cmd.index("-c") + 1], "other")

    def test_discovery_filters_by_label_and_running_phase(self):
        fake = FakeKubectl()
        run_main(["--roster", "/nonexistent"], fake)
        get = fake.calls[0]["cmd"]
        self.assertIn("app.kubernetes.io/name=platform-agent", get)
        self.assertIn("status.phase=Running", get)

    def test_several_running_pods_pick_one_and_say_which(self):
        fake = FakeKubectl(pods=("agent-b", "agent-a"))
        rc, _, err = run_main(["--roster", "/nonexistent"], fake)
        self.assertEqual(rc, 0)
        self.assertIn("2 Running agent pods", err)
        self.assertIn("agent-a", err)
        self.assertIn("--pod overrides", err)
        self.assertIn("agent-a", fake.exec_call["cmd"])

    def test_an_explicit_pod_skips_discovery(self):
        fake = FakeKubectl()
        run_main(["--roster", "/nonexistent", "--pod", "agent-9"], fake)
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("agent-9", fake.exec_call["cmd"])


class TestExitCodes(unittest.TestCase):
    def test_no_pod_is_exit_2_and_names_the_namespace(self):
        rc, _, err = run_main(
            ["--roster", "/nonexistent", "-n", "nope"], FakeKubectl(pods=())
        )
        self.assertEqual(rc, 2)
        self.assertIn("no agent pod found in namespace nope", err)

    def test_a_failed_exec_is_exit_2_and_carries_kubectls_stderr(self):
        fake = FakeKubectl(exec_rc=1, exec_stderr="Error from server (Forbidden): denied")
        rc, _, err = run_main(["--roster", "/nonexistent"], fake)
        self.assertEqual(rc, 2)
        self.assertIn("found but exec failed", err)
        self.assertIn("Forbidden", err)

    def test_output_that_is_not_json_is_exit_2(self):
        fake = FakeKubectl(exec_stdout="python3: command not found")
        rc, _, err = run_main(["--roster", "/nonexistent"], fake)
        self.assertEqual(rc, 2)
        self.assertIn("not JSON", err)

    def test_a_missing_store_is_exit_1(self):
        fake = FakeKubectl(exec_stdout=json.dumps(projection(root_exists=False)))
        rc, out, _ = run_main(["--roster", "/nonexistent"], fake)
        self.assertEqual(rc, 1)
        self.assertIn("store directory absent on the pod", out)

    def test_an_unreadable_stream_is_exit_1(self):
        doc = projection({"compliance-audit": stream(liveness="error", error="boom")})
        rc, _, _ = run_main(["--roster", "/nonexistent"], FakeKubectl(exec_stdout=json.dumps(doc)))
        self.assertEqual(rc, 1)

    def test_a_readable_store_is_exit_0(self):
        doc = projection({"compliance-audit": stream(last=latest())})
        rc, _, _ = run_main(["--roster", "/nonexistent"], FakeKubectl(exec_stdout=json.dumps(doc)))
        self.assertEqual(rc, 0)


class TestOfflineRoundTrip(unittest.TestCase):
    DOC = None

    def setUp(self):
        self.doc = projection({"compliance-audit": stream(last=latest())})

    def test_json_output_is_what_file_consumes(self):
        fake = FakeKubectl(exec_stdout=json.dumps(self.doc))
        rc, emitted, _ = run_main(["--roster", "/nonexistent", "--json"], fake)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(emitted), self.doc)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "projection.json"
            path.write_text(emitted, encoding="utf-8")
            rc_file, rendered, _ = run_main(
                ["--roster", "/nonexistent", "--file", str(path)], FakeKubectl(pods=())
            )
        self.assertEqual(rc_file, 0)
        self.assertIn("compliance-audit", rendered)
        self.assertIn("UPDATED", rendered)
        self.assertIn(f"file {path}", rendered)

    def test_file_mode_reaches_no_cluster(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "projection.json"
            path.write_text(json.dumps(self.doc), encoding="utf-8")
            fake = FakeKubectl(pods=())
            rc, out, _ = run_main(["--roster", "/nonexistent", "--file", str(path)], fake)
        self.assertEqual(rc, 0)
        self.assertEqual(fake.calls, [])
        self.assertIn("compliance-audit", out)

    def test_stdin_is_a_file_source(self):
        fake = FakeKubectl(pods=())
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(view.subprocess, "run", fake), \
                mock.patch.object(view.sys, "stdin", io.StringIO(json.dumps(self.doc))):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = view.main(["--roster", "/nonexistent", "--file", "-"])
        self.assertEqual(rc, 0)
        self.assertIn("compliance-audit", out.getvalue())

    def test_an_unreadable_file_is_exit_2(self):
        rc, _, err = run_main(
            ["--roster", "/nonexistent", "--file", "/nonexistent/projection.json"]
        )
        self.assertEqual(rc, 2)
        self.assertIn("could not read", err)


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

    def test_count_cell(self):
        self.assertEqual(view.count_cell(["a", "b"]), "2")
        self.assertEqual(view.count_cell([]), "0")
        self.assertEqual(view.count_cell(3), "3")
        self.assertEqual(view.count_cell(None), "—")


if __name__ == "__main__":
    unittest.main()
