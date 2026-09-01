"""Unit tests for the ``deliver: "chat"`` platform plugin.

Covers ``adapter.py`` on its own. What it cannot cover is that Hermes resolves
``deliver: "chat"`` to this plugin at all — that is
``deploy/docker/plugins/verify_chat_relay.py``, which drives the real
``cron/scheduler.py::_deliver_result`` against the installed tree at image build
time.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import adapter as mod


class RecordingRelay:
    """A stdlib HTTP server standing in for the Session KV server."""

    def __init__(self, status: int = 200, body: bytes = b"{}") -> None:
        self.status = status
        self.body = body
        self.requests: list[dict] = []
        server_self = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — stdlib naming
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length).decode("utf-8")
                server_self.requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization", ""),
                        "body": json.loads(raw) if raw else {},
                    }
                )
                self.send_response(server_self.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(server_self.body)))
                self.end_headers()
                self.wfile.write(server_self.body)

            def log_message(self, *_args) -> None:
                """Keep the test output clean."""

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "RecordingRelay":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[0], self._server.server_address[1]
        return f"http://{host}:{port}/v1/cron-reports"


def wrapped(title: str, job_id: str, body: str) -> str:
    """``_deliver_result``'s wrapper, byte for byte."""
    return (
        f"Cronjob Response: {title}\n"
        f"(job_id: {job_id})\n"
        f"-------------\n\n"
        f"{body}\n\n"
        f'To stop or manage this job, send me a new message (e.g. "stop reminder {title}").'
    )


class TestParseCronWrapper(unittest.TestCase):
    def test_the_wrapper_yields_id_title_and_a_clean_report(self):
        job_id, title, report = mod.parse_cron_wrapper(
            wrapped("GitHub Repo Watcher", "github-repo-watcher", "the issues sweep failed")
        )
        self.assertEqual(job_id, "github-repo-watcher")
        self.assertEqual(title, "GitHub Repo Watcher")
        self.assertEqual(report, "the issues sweep failed")

    def test_a_multi_line_report_keeps_its_shape(self):
        body = "## Findings\n\n- one\n- two\n\n```\ncode\n```"
        _, _, report = mod.parse_cron_wrapper(wrapped("Audit", "a", body))
        self.assertEqual(report, body)

    def test_a_report_that_itself_mentions_the_footer_text(self):
        body = 'Tell the user: To stop or manage this job, send me a new message (e.g. "x").'
        _, _, report = mod.parse_cron_wrapper(wrapped("Audit", "a", body))
        self.assertEqual(report, body, "only the trailing footer may be stripped")

    def test_no_wrapper_relays_the_whole_message_anonymously(self):
        job_id, title, report = mod.parse_cron_wrapper("just the report")
        self.assertEqual((job_id, title), ("", ""))
        self.assertEqual(report, "just the report")

    def test_an_empty_message(self):
        self.assertEqual(mod.parse_cron_wrapper(""), ("", "", ""))

    def test_a_header_like_first_line_that_is_not_the_wrapper(self):
        self.assertEqual(
            mod.parse_cron_wrapper("Cronjob Response: x\nbut no job_id line"),
            ("", "", "Cronjob Response: x\nbut no job_id line"),
        )


class TestProfileName(unittest.TestCase):
    def test_a_named_profile(self):
        with patch.dict(os.environ, {"HERMES_HOME": "/opt/data/profiles/platform"}):
            self.assertEqual(mod.profile_name(), "platform")

    def test_a_cluster_profile(self):
        with patch.dict(os.environ, {"HERMES_HOME": "/opt/data/profiles/cluster-prod-a"}):
            self.assertEqual(mod.profile_name(), "cluster-prod-a")

    def test_the_root_home_is_not_called_data(self):
        with patch.dict(os.environ, {"HERMES_HOME": "/opt/data"}):
            self.assertEqual(mod.profile_name(), "default")

    def test_an_unset_home(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(mod.profile_name(), "default")


class TestIsConnected(unittest.TestCase):
    """The one switch. Unset in the gateway, set by ``profile_cron_tick.py``."""

    def test_unset_keeps_the_platform_out_of_the_gateway(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(mod.is_connected(None))

    def test_blank_is_unset(self):
        with patch.dict(os.environ, {mod.HOME_CHANNEL_ENV: "   "}):
            self.assertFalse(mod.is_connected(None))

    def test_set_switches_the_relay_on(self):
        with patch.dict(os.environ, {mod.HOME_CHANNEL_ENV: "cron-reports"}):
            self.assertTrue(mod.is_connected(None))

    def test_there_is_no_adapter_to_build(self):
        with self.assertRaises(NotImplementedError):
            mod._no_adapter(None)


class TestStandaloneSend(unittest.TestCase):
    MESSAGE = wrapped("GitHub Repo Watcher", "github-repo-watcher", "the issues sweep failed")

    def test_a_report_reaches_the_route_with_its_key(self):
        with RecordingRelay() as relay:
            with patch.dict(
                os.environ,
                {
                    "SESSION_KV_API_KEY": "k",
                    "CRON_REPORT_RELAY_URL": relay.url,
                    "HERMES_HOME": "/opt/data/profiles/platform",
                },
            ):
                result = asyncio.run(
                    mod.standalone_send(None, "cron-reports", self.MESSAGE)
                )
            self.assertTrue(result.get("success"), result)
            self.assertEqual(len(relay.requests), 1)
            sent = relay.requests[0]
            self.assertEqual(sent["path"], "/v1/cron-reports")
            self.assertEqual(sent["authorization"], "Bearer k")
            self.assertEqual(
                sent["body"],
                {
                    "job_id": "github-repo-watcher",
                    "profile": "platform",
                    "title": "GitHub Repo Watcher",
                    "report": "the issues sweep failed",
                    # Empty because this HERMES_HOME has no roster to read, which
                    # is the safe answer: the field only ever removes targets.
                    "also_delivered_to": [],
                },
            )

    def test_the_cron_wrapper_never_reaches_the_chat_agent(self):
        """It would ask the Chat Agent to relay Hermes' own plumbing text."""
        with RecordingRelay() as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                asyncio.run(mod.standalone_send(None, "c", self.MESSAGE))
            report = relay.requests[0]["body"]["report"]
        self.assertNotIn("Cronjob Response:", report)
        self.assertNotIn("To stop or manage this job", report)

    def test_an_unwrapped_message_still_relays(self):
        with RecordingRelay() as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                result = asyncio.run(mod.standalone_send(None, "c", "bare report"))
            self.assertTrue(result.get("success"), result)
            self.assertEqual(relay.requests[0]["body"]["report"], "bare report")
            self.assertEqual(relay.requests[0]["body"]["job_id"], "")

    def test_an_empty_report_is_a_silent_tick(self):
        """`github-repo-watcher` prints nothing on a clean sweep, 144 times a day."""
        with RecordingRelay() as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                result = asyncio.run(
                    mod.standalone_send(
                        None, "c", wrapped("GitHub Repo Watcher", "ghw", "")
                    )
                )
            self.assertTrue(result.get("success"), result)
            self.assertEqual(result.get("skipped"), "empty_report")
            self.assertEqual(relay.requests, [], "nothing should have been sent")

    def test_a_whitespace_report_is_silence_too(self):
        with RecordingRelay() as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                result = asyncio.run(mod.standalone_send(None, "c", "   \n\t "))
            self.assertEqual(result.get("skipped"), "empty_report")
            self.assertEqual(relay.requests, [])

    def test_an_emphasised_silence_marker_is_still_silence(self):
        """The leak this guard exists for.

        Upstream's matcher takes `[SILENT]` bare, lowercased, or among prose,
        and where it applies `standalone_send` is never called. It does not
        take the marker in a code span or in bold -- and the audit SOPs tell
        every run to copy `chat_summary` verbatim, on a quiet run that field is
        exactly `[SILENT]`, and these agents write markdown. Emphasise it once
        and the operator gets a message reading "[SILENT]" from a run whose
        whole point was to stay quiet.
        """
        for dressed in ("`[SILENT]`", "**[SILENT]**", "_[SILENT]_", "  **`[silent]`**  "):
            with self.subTest(report=dressed):
                with RecordingRelay() as relay:
                    with patch.dict(
                        os.environ,
                        {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
                    ):
                        result = asyncio.run(
                            mod.standalone_send(
                                None, "c", wrapped("Compliance Audit", "ca", dressed)
                            )
                        )
                self.assertTrue(result.get("success"), result)
                self.assertEqual(result.get("skipped"), "empty_report")
                self.assertEqual(relay.requests, [], f"{dressed!r} was relayed")

    def test_a_real_report_is_never_mistaken_for_silence(self):
        """Undressing strips punctuation off both ends; it must not eat a report.

        The summary line the harness renders is the exact shape at risk -- it
        ends in a URL and can begin with an emphasised count -- so it is the
        one checked, alongside a report that merely mentions the marker.
        """
        real = [
            "2 critical, 5 medium (3 new, 1 resolved) — https://github.com/x/y/issues/41",
            "**3 high** (no change) — https://github.com/x/y/issues/12",
            "The run emitted [SILENT] on its first attempt, then found 4 criticals.",
            "---",  # a horizontal rule: punctuation, but not the dress this strips
        ]
        for report in real:
            with self.subTest(report=report[:40]):
                self.assertFalse(mod.is_silent_report(report))

    def test_a_report_of_nothing_but_emphasis_is_silence(self):
        """`***` undresses to empty, and that is the wanted answer.

        It carries no content, so the alternative is posting three asterisks to
        the operator's home channel -- which is what the previous
        `report.strip()` guard did.
        """
        for report in ("***", "_", "~~~", "  **  ", "`"):
            with self.subTest(report=report):
                self.assertTrue(mod.is_silent_report(report))

    def test_every_kind_of_whitespace_the_old_blank_test_caught_is_still_silence(self):
        """This predicate replaced `not report.strip()` and must not narrow it.

        `_MARKDOWN_DRESS` can only list ASCII whitespace, so undressing alone
        called a report of one NBSP non-empty and relayed it -- and
        `submit_cron_report` rejects a blank report with an HTTP 400 that lands
        in `last_delivery_error`, the failure the guard exists to prevent. The
        blast radius reaches past this plugin: `slack_relay_patch` imports this
        function and dropped its own blank test in favour of it.
        """
        for report in ("\xa0", "　", "\x0b", "\x0c", "\x1c", " ", " ", "\xa0 \t　"):
            with self.subTest(report=report):
                self.assertTrue(report.strip() == "", "fixture is not whitespace")
                self.assertTrue(mod.is_silent_report(report))

    def test_the_upstream_matcher_is_not_consulted_at_all(self):
        """Every case must answer the same in the pod as it does here.

        The predicate used to delegate to `cron.scheduler`, which is absent from
        this checkout, so every silence test took the `except` fallback and the
        branch that actually ships was ungraded. Two of the tests above --
        including the one asserting a report that mentions the marker is
        relayed -- passed for that reason alone.

        Planting a matcher that answers the opposite of the truth pins that the
        deployed branch and the tested branch are now the same code. If the
        delegation comes back, this fails on both lines at once.
        """
        import sys
        import types

        calls = []

        def inverted(text):
            calls.append(text)
            return "SILENT" not in text.upper()

        fake = types.ModuleType("cron.scheduler")
        fake._is_cron_silence_response = inverted
        pkg = types.ModuleType("cron")
        pkg.scheduler = fake
        with patch.dict(sys.modules, {"cron": pkg, "cron.scheduler": fake}):
            self.assertTrue(mod.is_silent_report("**[SILENT]**"))
            self.assertFalse(mod.is_silent_report("3 critical findings"))
        self.assertEqual([], calls, "the upstream matcher was consulted")

    def test_an_alert_that_quotes_the_marker_in_prose_is_relayed(self):
        """The reason the delegation had to go.

        `standalone_send` is the sender for every out-of-process `hermes send`,
        not only for cron: `session_kv_server._post_initial_alert` pages through
        it and `_send_to_chat` posts the composed cron report through it.
        Upstream's matcher accepts the marker on its own line among prose, which
        is correct for a model's response to a cron prompt and wrong for these
        -- an alert about a run that published nothing quotes the marker while
        saying so, and got `{"success": True, "skipped": "empty_text"}` with no
        `message_id`, so the caller could not tell the page had been dropped.
        """
        alert = (
            "Incident: audit-runner CrashLoopBackOff in prod-eu.\n"
            "The run never emitted its summary; the last thing hermes recorded was\n"
            "[SILENT]\n"
            "which is why nothing was posted at 06:00. Investigating."
        )
        self.assertFalse(mod.is_silent_report(alert))
        # The marker leading and trailing the prose, not only embedded in it:
        # both are shapes the undress could have eaten from the ends.
        self.assertFalse(mod.is_silent_report("[SILENT]\nwas recorded at 06:00."))
        self.assertFalse(mod.is_silent_report("The 06:00 run recorded\n[SILENT]"))

    def test_the_marker_padded_with_non_ascii_whitespace_is_still_silence(self):
        # The dress strip stops at the NBSP, leaving the asterisks in place, so
        # the fallback compared "**[SILENT]**" against the marker and relayed it.
        for report in ("\xa0**[SILENT]**\xa0", "　[SILENT]　", "\xa0`[SILENT]`\xa0"):
            with self.subTest(report=report):
                self.assertTrue(mod.is_silent_report(report))

    def test_silence_is_not_a_missing_key(self):
        """A quiet tick has nothing to authenticate, so an unset key is not its problem.

        Otherwise the guard would just trade one every-ten-minutes
        ``last_delivery_error`` for another.
        """
        with patch.dict(os.environ, {}, clear=True):
            result = asyncio.run(mod.standalone_send(None, "c", ""))
        self.assertTrue(result.get("success"), result)
        self.assertNotIn("error", result)

    def test_no_key_is_refused_before_the_request(self):
        with RecordingRelay() as relay:
            with patch.dict(
                os.environ, {"CRON_REPORT_RELAY_URL": relay.url}, clear=True
            ):
                result = asyncio.run(mod.standalone_send(None, "c", "r"))
            self.assertIn("SESSION_KV_API_KEY", result.get("error", ""))
            self.assertEqual(relay.requests, [], "nothing should have been sent")

    def test_a_server_error_is_reported_not_raised(self):
        with RecordingRelay(status=500) as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                result = asyncio.run(mod.standalone_send(None, "c", "r"))
        self.assertIn("500", result.get("error", ""))

    def test_an_unreachable_relay_is_reported_not_raised(self):
        with patch.dict(
            os.environ,
            {
                "SESSION_KV_API_KEY": "k",
                # Port 1 is reserved and never listening.
                "CRON_REPORT_RELAY_URL": "http://127.0.0.1:1/v1/cron-reports",
            },
        ):
            result = asyncio.run(mod.standalone_send(None, "c", "r"))
        self.assertIn("unreachable", result.get("error", "").lower())

    def test_no_failure_string_carries_the_key(self):
        """These strings end up in ``last_delivery_error`` and in the log."""
        secret = "s3cr3t-session-kv-key"
        with RecordingRelay(status=503) as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": secret, "CRON_REPORT_RELAY_URL": relay.url},
            ):
                result = asyncio.run(mod.standalone_send(None, "c", "r"))
        self.assertNotIn(secret, json.dumps(result))

    def test_every_failure_is_a_dict_send_message_understands(self):
        """``_send_via_adapter`` requires ``success`` or ``error`` — never a raise."""
        with patch.dict(
            os.environ,
            {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": "not-a-url"},
        ):
            result = asyncio.run(mod.standalone_send(None, "c", "r"))
        self.assertIsInstance(result, dict)
        self.assertTrue(result.get("success") or result.get("error"))

    def test_a_failure_names_the_leg_that_broke(self):
        """The route relays synchronously, so its verdict is the delivery result.

        A bare "HTTP 502" in `last_delivery_error` says a watchdog went quiet and
        nothing about why; the route's `detail` names the leg.
        """
        detail = b'{"detail":"chat relay failed: composed but not delivered to google_chat"}'
        with RecordingRelay(status=502, body=detail) as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                result = asyncio.run(mod.standalone_send(None, "c", "r"))
        self.assertIn("502", result["error"])
        self.assertIn("composed but not delivered to google_chat", result["error"])

    def test_an_unparseable_error_body_still_reports_the_status(self):
        for body in (b"", b"<html>gateway timeout</html>", b'{"detail":null}', b'{"detail":"  "}'):
            with self.subTest(body=body):
                with RecordingRelay(status=502, body=body) as relay:
                    with patch.dict(
                        os.environ,
                        {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
                    ):
                        result = asyncio.run(mod.standalone_send(None, "c", "r"))
                self.assertEqual(result["error"], "chat relay answered HTTP 502")

    def test_a_long_detail_is_bounded(self):
        """It is stored per job run, so it cannot be a whole report."""
        detail = json.dumps({"detail": "x" * 5000}).encode()
        with RecordingRelay(status=502, body=detail) as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                result = asyncio.run(mod.standalone_send(None, "c", "r"))
        self.assertLess(len(result["error"]), 300)

    def test_a_composed_delivery_is_a_plain_success(self):
        """The route says `relay: "ok"` on the healthy path."""
        body = b'{"status":"delivered","relay":"ok","session_id":"s1"}'
        with RecordingRelay(body=body) as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                result = asyncio.run(mod.standalone_send(None, "c", self.MESSAGE))
        self.assertTrue(result.get("success"), result)
        self.assertNotIn("error", result)

    def test_a_degraded_relay_is_recorded_as_a_delivery_error(self):
        """200 plus `relay: "degraded"` means posted raw, not composed.

        `error` is the only field the scheduler reads, and `last_delivery_error`
        the only place a run record can carry it — so a front door that has been
        down all week must not produce run records identical to healthy ones.
        """
        body = b'{"status":"delivered","relay":"degraded","session_id":"s1"}'
        with RecordingRelay(body=body) as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                result = asyncio.run(mod.standalone_send(None, "c", self.MESSAGE))
        self.assertNotIn("success", result)
        self.assertIn("degraded", result["error"])

    def test_the_degraded_string_says_the_report_did_arrive(self):
        """Otherwise `cronjob list` reads as "nothing was sent" and invites a
        re-run, which would post the same finding to the channel twice."""
        body = b'{"status":"delivered","relay":"degraded"}'
        with RecordingRelay(body=body) as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                result = asyncio.run(mod.standalone_send(None, "c", "r"))
        error = result["error"]
        self.assertIn("was posted", error)
        self.assertIn("do not re-run", error.lower())

    def test_the_degraded_string_says_which_degradation_it_was(self):
        """`degraded` covers two outcomes and they want opposite responses.

        A failed Chat Agent turn leaves raw text in every channel; a send that
        never landed leaves one channel with nothing. Both used to print the
        first one's sentence, so an operator whose Google Chat leg had been
        failing was told to go and find `[unrelayed]` text — which was not
        there, in a channel that was not the one missing the report.
        """
        detail = "the composed report never reached google_chat (it reached slack)"
        body = json.dumps(
            {"status": "delivered", "relay": "degraded", "relay_detail": detail}
        ).encode()
        with RecordingRelay(body=body) as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                result = asyncio.run(mod.standalone_send(None, "c", "r"))
        error = result["error"]
        self.assertIn(detail, error)
        self.assertNotIn("unrelayed", error)
        # Still the two things the string has always had to say.
        self.assertIn("was posted", error)
        self.assertIn("do not re-run", error.lower())

    def test_a_runaway_detail_is_bounded_before_it_becomes_the_error(self):
        """The error string is stored as `last_delivery_error`, once per run.

        `_http_error_detail` bounds its own contribution at 200 characters for
        that reason; `relay_detail` comes from the same route and lands in the
        same field, so it takes the same bound. Left unbounded, a route that
        echoes a stack trace or the report itself writes the whole thing into
        the job record.
        """
        detail = "google_chat rejected the send: " + "x" * 5000
        body = json.dumps(
            {"status": "delivered", "relay": "degraded", "relay_detail": detail}
        ).encode()
        with RecordingRelay(body=body) as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                result = asyncio.run(mod.standalone_send(None, "c", "r"))
        error = result["error"]
        self.assertIn("google_chat rejected the send:", error)
        self.assertNotIn("x" * 201, error)
        # And the two things the string has always had to say survive the cut.
        self.assertIn("was posted", error)
        self.assertIn("do not re-run", error.lower())

    def test_a_degraded_relay_with_no_detail_keeps_the_sentence_it_had(self):
        """A route too old to send `relay_detail` had only the one cause.

        So the fallback is not a guess about what happened — before the second
        cause was reported separately, `degraded` from such a route did mean the
        turn failed. Keeping the old wording for exactly that case is what makes
        the change safe to deploy against a gateway that has not restarted yet.
        """
        body = b'{"status":"delivered","relay":"degraded","session_id":"s1"}'
        with RecordingRelay(body=body) as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                result = asyncio.run(mod.standalone_send(None, "c", "r"))
        self.assertIn("[unrelayed]", result["error"])
        self.assertIn("Chat Agent turn", result["error"])

    def test_a_detail_on_a_healthy_relay_changes_nothing(self):
        # The field is only ever read under a `degraded` verdict, so a route
        # that sends an empty one alongside `ok` is still a plain success.
        body = b'{"status":"delivered","relay":"ok","relay_detail":""}'
        with RecordingRelay(body=body) as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                result = asyncio.run(mod.standalone_send(None, "c", self.MESSAGE))
        self.assertTrue(result.get("success"), result)

    def test_a_2xx_that_says_nothing_about_the_relay_is_a_success(self):
        """An older route, or one that answered before the field existed."""
        for body in (b"{}", b"", b"<html>ok</html>", b'{"relay":null}', b"[]"):
            with self.subTest(body=body):
                with RecordingRelay(body=body) as relay:
                    with patch.dict(
                        os.environ,
                        {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
                    ):
                        result = asyncio.run(mod.standalone_send(None, "c", "r"))
                self.assertTrue(result.get("success"), result)

    def test_a_verdict_never_turns_a_failure_into_a_success(self):
        """A non-2xx body is not read for a verdict — the status decides."""
        body = b'{"relay":"ok"}'
        with RecordingRelay(status=502, body=body) as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                result = asyncio.run(mod.standalone_send(None, "c", "r"))
        self.assertIn("502", result["error"])

    def test_post_returns_an_error_a_verdict_and_a_detail(self):
        """Every path out of `_post` is a 3-tuple; the callers unpack it."""
        body = b'{"relay":"degraded","relay_detail":"the send never reached slack"}'
        with RecordingRelay(body=body) as relay:
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    mod._post(relay.url, {}, "k"),
                    (None, "degraded", "the send never reached slack"),
                )
        with patch.dict(os.environ, {}, clear=True):
            error, verdict, detail = mod._post("not-a-url", {}, "k")
        self.assertIsNotNone(error)
        self.assertEqual((verdict, detail), ("", ""))

    def test_the_timeout_outlasts_a_chat_agent_turn(self):
        """Time out before the route answers and a delivered report is recorded
        as a failure. `_run_relay_turn` allows the turn itself 300s."""
        self.assertGreater(mod.RELAY_TIMEOUT_SECONDS, 300.0)

    def test_the_default_route_is_the_loopback_session_kv_server(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(mod.relay_url(), mod.DEFAULT_RELAY_URL)
        self.assertTrue(mod.DEFAULT_RELAY_URL.startswith("http://127.0.0.1:8699/"))


class TestDeclaredSilent(unittest.TestCase):
    """The guard for a silent run that described its silence instead of emitting it.

    Every text-based test above needs the marker to be somewhere in the
    message. The failure this covers has no marker at all: the run decided to
    stay quiet, and then wrote "the audit published successfully, `silent_ok:
    true`, nothing moved" — which is a message, delivered, from a run whose
    whole point was that there was nothing to deliver.
    """

    PROSE = (
        "The audit published successfully: `status: \"UPDATED\"`, `new: 0`, "
        "`resolved: 0`, `silent_ok: true`, `partial: false` — ledger issue #38 "
        "was rewritten with the same 31 findings as last run."
    )

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def write(self, stream: str, *, silent: bool, age_s: float = 20.0) -> None:
        import datetime

        finished = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            seconds=age_s
        )
        os.makedirs(os.path.join(self.root, stream), exist_ok=True)
        with open(os.path.join(self.root, stream, "latest.json"), "w") as handle:
            json.dump(
                {"silent_ok": silent, "finished_at": finished.isoformat()}, handle
            )

    def declared(self, job_id: str) -> bool:
        with patch.dict(os.environ, {"FLEET_AUDIT_REPORTS_DIR": self.root}):
            with patch.object(mod, "_REPORTS_DIR", self.root):
                return mod.declared_silent(job_id)

    def test_a_fresh_silent_report_silences_whatever_the_run_wrote(self):
        self.write("compliance-audit", silent=True)
        self.assertTrue(self.declared("compliance-audit"))

    def test_a_loud_report_is_left_alone(self):
        """The ordinary case: findings moved, so the summary must reach the channel."""
        self.write("compliance-audit", silent=False)
        self.assertFalse(self.declared("compliance-audit"))

    def test_a_run_in_flight_means_the_record_is_the_previous_run_s(self):
        """The age test asks how old the record is, never whose it is.

        `release_run_lock` unlinks `started.json` as the last act of a successful
        `finish`, after the envelope lands, so the file being present means no
        `finish` has completed since the current run began -- `latest.json` is
        the run before this one. Without this the second run inside the 900s
        window inherits the first's verdict: a re-trigger that crashes gets its
        "RUN FAILED" text swallowed as a silent tick, `last_status=ok`, no
        `last_delivery_error`, nothing in the channel.
        """
        self.write("compliance-audit", silent=True)
        with open(os.path.join(self.root, "compliance-audit", "started.json"), "w") as h:
            json.dump({"nonce": "n", "t0": "2026-09-01T06:25:00+00:00"}, h)
        self.assertFalse(self.declared("compliance-audit"))

    def test_a_stale_report_decides_nothing(self):
        """It belongs to an earlier run, so it must not silence this delivery."""
        self.write("compliance-audit", silent=True, age_s=mod._SILENCE_WINDOW_SECONDS + 60)
        self.assertFalse(self.declared("compliance-audit"))

    def test_a_report_from_the_future_decides_nothing(self):
        """A clock that jumped would otherwise silence deliveries indefinitely."""
        self.write("compliance-audit", silent=True, age_s=-3600)
        self.assertFalse(self.declared("compliance-audit"))

    def test_a_job_that_is_not_an_audit_stream_is_unaffected(self):
        self.write("compliance-audit", silent=True)
        for job_id in ("", "github-repo-watcher", "../compliance-audit"):
            with self.subTest(job_id=job_id):
                self.assertFalse(self.declared(job_id))

    def test_a_traversal_cannot_reach_a_report_planted_outside_the_store(self):
        """The one input the segment test alone lets through.

        ``Path("..").name`` is ``".."``, not ``""`` -- pathlib keeps the
        segment where ``os.path.basename`` drops it -- so ``job_id ==
        Path(job_id).name`` holds for ``..``. The planted file below sits one
        level above the store, inside the agent's own writable volume, and
        would otherwise supply both the silence verdict and, through
        ``recorded_summary``, the text posted in place of the report.

        The job id reaches here from message text, so this is reachable
        without any access to the store: it is the wrapper that names the
        stream.
        """
        import datetime

        parent = os.path.dirname(self.root.rstrip("/"))
        planted = os.path.join(parent, "latest.json")
        finished = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with open(planted, "w") as handle:
            json.dump(
                {"silent_ok": True, "summary": "ATTACKER TEXT", "finished_at": finished},
                handle,
            )
        self.addCleanup(os.remove, planted)
        self.assertFalse(self.declared(".."))
        with patch.dict(os.environ, {"FLEET_AUDIT_REPORTS_DIR": self.root}):
            with patch.object(mod, "_REPORTS_DIR", self.root):
                self.assertEqual(mod.recorded_summary(".."), "")

    def test_an_unreadable_store_fails_open(self):
        """A bad read must not drop a report — it leaves the text test in charge."""
        os.makedirs(os.path.join(self.root, "compliance-audit"))
        with open(os.path.join(self.root, "compliance-audit", "latest.json"), "w") as h:
            h.write("{not json")
        self.assertFalse(self.declared("compliance-audit"))
        self.write("obtainability-audit", silent=True)
        os.remove(os.path.join(self.root, "obtainability-audit", "latest.json"))
        with open(os.path.join(self.root, "obtainability-audit", "latest.json"), "w") as h:
            json.dump({"silent_ok": True}, h)  # no finished_at
        self.assertFalse(self.declared("obtainability-audit"))

    def test_prose_about_silence_is_not_relayed(self):
        """End to end through the sender, which is where the leak happened."""
        self.write("compliance-audit", silent=True)
        with RecordingRelay() as relay:
            with patch.dict(
                os.environ,
                {
                    "SESSION_KV_API_KEY": "k",
                    "CRON_REPORT_RELAY_URL": relay.url,
                    "FLEET_AUDIT_REPORTS_DIR": self.root,
                },
            ):
                with patch.object(mod, "_REPORTS_DIR", self.root):
                    result = asyncio.run(
                        mod.standalone_send(
                            None,
                            "c",
                            wrapped("Compliance Audit", "compliance-audit", self.PROSE),
                        )
                    )
        self.assertTrue(result.get("success"), result)
        self.assertEqual(relay.requests, [], "the silent run's prose was relayed")

    def test_the_same_prose_from_a_loud_run_is_relayed(self):
        """The control. Without it the test above passes on a sender that drops everything."""
        self.write("compliance-audit", silent=False)
        with RecordingRelay() as relay:
            with patch.dict(
                os.environ,
                {
                    "SESSION_KV_API_KEY": "k",
                    "CRON_REPORT_RELAY_URL": relay.url,
                    "FLEET_AUDIT_REPORTS_DIR": self.root,
                },
            ):
                with patch.object(mod, "_REPORTS_DIR", self.root):
                    result = asyncio.run(
                        mod.standalone_send(
                            None,
                            "c",
                            wrapped("Compliance Audit", "compliance-audit", self.PROSE),
                        )
                    )
        self.assertTrue(result.get("success"), result)
        self.assertEqual(len(relay.requests), 1, "the loud run's report was dropped")


class TestRecordedSummary(unittest.TestCase):
    """A loud run relays the line `finish` composed, not the one the model wrote.

    Same disagreement as ``TestDeclaredSilent``, opposite direction. The run
    has something to report and the SOP tells it to report the recorded
    ``chat_summary`` verbatim; 36 of the reference install's 38 non-silent runs
    posted a headed multi-section report instead, median 1.6kB.
    """

    SUMMARY = (
        "Security & RBAC Posture Audit: 16 critical, 2 major, 1 minor "
        "(3 new, 1 resolved) — https://github.com/gke-agentic/adamparco-infra/issues/57"
    )
    COMPOSED = (
        "## Security & RBAC Posture Audit — complete\n\n"
        "The run finished and published to the ledger. Here is the breakdown by\n"
        "cluster, with the three new findings called out first.\n\n"
        "- 16 clusters expose a public control plane\n"
        "- `argocd` grants a wildcard ClusterRole\n\n"
        "Ledger: https://github.com/gke-agentic/adamparco-infra/issues/57"
    )

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def write(self, stream: str, payload: dict, *, age_s: float = 20.0) -> None:
        import datetime

        finished = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            seconds=age_s
        )
        os.makedirs(os.path.join(self.root, stream), exist_ok=True)
        body = dict(payload)
        body.setdefault("finished_at", finished.isoformat())
        with open(os.path.join(self.root, stream, "latest.json"), "w") as handle:
            json.dump(body, handle)

    def relayed(self, body: str, job_id: str = "compliance-audit"):
        with RecordingRelay() as relay:
            with patch.dict(
                os.environ,
                {
                    "SESSION_KV_API_KEY": "k",
                    "CRON_REPORT_RELAY_URL": relay.url,
                    "FLEET_AUDIT_REPORTS_DIR": self.root,
                },
            ):
                with patch.object(mod, "_REPORTS_DIR", self.root):
                    result = asyncio.run(
                        mod.standalone_send(
                            None, "c", wrapped("Compliance Audit", job_id, body)
                        )
                    )
        self.assertTrue(result.get("success", True), result)
        return [r["body"]["report"] for r in relay.requests]

    def test_the_recorded_summary_replaces_a_composed_report(self):
        self.write("compliance-audit", {"silent_ok": False, "chat_summary": self.SUMMARY})
        self.assertEqual(self.relayed(self.COMPOSED), [self.SUMMARY])

    def test_a_run_in_flight_does_not_lend_its_summary_to_a_failure(self):
        """The worst shape of the stale read, and the reason it is not cosmetic.

        A loud run finishes; a re-run starts inside the 900s window and dies
        before `finish`; hermes delivers its own "RUN FAILED" text. Keyed on age
        alone the previous run's `chat_summary` displaces that text and is posted
        as a success -- the channel is told "16 critical, 2 major (3 new, 1
        resolved)" about a run that never reached a cluster. Substituting a stale
        report and reporting it delivered is the one failure the relay exists to
        prevent, so the failure text has to survive intact.
        """
        self.write("compliance-audit", {"silent_ok": False, "chat_summary": self.SUMMARY})
        with open(os.path.join(self.root, "compliance-audit", "started.json"), "w") as h:
            json.dump({"nonce": "n", "t0": "2026-09-01T06:25:00+00:00"}, h)
        failure = "RUN FAILED: audit_report.py start exited 1"
        self.assertEqual(self.relayed(failure), [failure])

    def test_a_run_that_obeyed_is_relayed_unchanged(self):
        self.write("compliance-audit", {"silent_ok": False, "chat_summary": self.SUMMARY})
        self.assertEqual(self.relayed(self.SUMMARY), [self.SUMMARY])

    def test_a_report_with_no_recorded_summary_is_left_alone(self):
        """Every report written before the field existed, and every non-audit job."""
        self.write("compliance-audit", {"silent_ok": False})
        self.assertEqual(self.relayed(self.COMPOSED), [self.COMPOSED])

    def test_a_stale_summary_does_not_replace_a_later_report(self):
        self.write(
            "compliance-audit",
            {"silent_ok": False, "chat_summary": self.SUMMARY},
            age_s=mod._SILENCE_WINDOW_SECONDS + 60,
        )
        self.assertEqual(self.relayed(self.COMPOSED), [self.COMPOSED])

    def test_a_non_audit_job_is_untouched(self):
        self.write("compliance-audit", {"silent_ok": False, "chat_summary": self.SUMMARY})
        self.assertEqual(
            self.relayed(self.COMPOSED, job_id="github-repo-watcher"), [self.COMPOSED]
        )

    def test_a_recorded_marker_never_becomes_the_message(self):
        """`silent_ok` and `chat_summary` disagreeing must not post "[SILENT]".

        Belt and braces: `declared_silent` already stops the silent case, so
        reaching here means the report claims to be loud while carrying the
        marker. Substituting it would put the word in the channel, which is the
        one outcome both halves of this guard exist to prevent.
        """
        for marker in ("[SILENT]", "**[SILENT]**", "  [silent]  "):
            with self.subTest(chat_summary=marker):
                self.write(
                    "compliance-audit", {"silent_ok": False, "chat_summary": marker}
                )
                self.assertEqual(self.relayed(self.COMPOSED), [self.COMPOSED])


class TestSiblingDeliveryTargets(unittest.TestCase):
    """Which platforms the scheduler is posting this same report to itself.

    Verified against the live install on 2026-08-30 before being written: two
    probes addressed to Google Chat, one from the relay fan-out and one from
    ``deliver: "all"``'s direct leg, both arrived. That is the duplicate this
    function exists to subtract.
    """

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        os.makedirs(os.path.join(self.home, "cron"))
        # Only the home-channel variables matter, and an ambient one on the
        # machine running the suite would change the answer.
        env = patch.dict(os.environ, {"HERMES_HOME": self.home})
        env.start()
        self.addCleanup(env.stop)
        for key in [k for k in os.environ if k.endswith("_HOME_CHANNEL")]:
            del os.environ[key]

    def _roster(self, deliver):
        path = os.path.join(self.home, "cron", "jobs.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"jobs": [{"id": "audit", "deliver": deliver}]}, handle)

    def test_relay_only_delivery_has_no_siblings(self):
        """``deliver: "chat"`` is the relay and nothing else, so fan out freely."""
        self._roster("chat")
        os.environ["SLACK_HOME_CHANNEL"] = "D0BKGRBM6RH"
        os.environ["GOOGLE_CHAT_HOME_CHANNEL"] = "spaces/AAA"
        self.assertEqual(mod.sibling_delivery_targets("audit"), [])

    def test_all_names_every_platform_with_a_home_channel(self):
        self._roster("all")
        os.environ["SLACK_HOME_CHANNEL"] = "D0BKGRBM6RH"
        os.environ["GOOGLE_CHAT_HOME_CHANNEL"] = "spaces/AAA"
        os.environ["CHAT_HOME_CHANNEL"] = "cron-reports"
        self.assertEqual(
            mod.sibling_delivery_targets("audit"), ["google_chat", "slack"]
        )

    def test_all_skips_the_platform_this_install_cannot_address(self):
        """The live shape: no ``SLACK_HOME_CHANNEL`` in the cron child.

        ``home_target_env`` rebuilds home channels from ``config.yaml``, whose
        ``slack:`` section carries none — so the scheduler drops Slack from
        ``all`` and the relay leg is the only thing that reaches it. Naming it
        here would suppress that leg and leave Slack with nothing at all.
        """
        self._roster("all")
        os.environ["GOOGLE_CHAT_HOME_CHANNEL"] = "spaces/AAA"
        os.environ["CHAT_HOME_CHANNEL"] = "cron-reports"
        self.assertEqual(mod.sibling_delivery_targets("audit"), ["google_chat"])

    def test_an_explicit_list_names_only_what_it_lists(self):
        self._roster("chat,slack")
        os.environ["SLACK_HOME_CHANNEL"] = "D0BKGRBM6RH"
        os.environ["GOOGLE_CHAT_HOME_CHANNEL"] = "spaces/AAA"
        self.assertEqual(mod.sibling_delivery_targets("audit"), ["slack"])

    def test_no_job_id_adopts_no_jobs_deliver(self):
        """An empty id is "no wrapper", not "the job whose id is blank".

        Every delivery under ``cron.wrap_response: false`` arrives without a
        wrapper and yields ``job_id == ""``, and the lookup compared that
        against ``job.get("id") or ""`` -- so it matched the first hand-edited
        entry with a missing id and subtracted platforms on the strength of a
        different job's ``deliver``. Here that would suppress both legs of a
        delivery the store says nothing about.
        """
        path = os.path.join(self.home, "cron", "jobs.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {"jobs": [{"name": "hand edited, no id", "deliver": "all"},
                          {"id": "audit", "deliver": "chat"}]},
                handle,
            )
        os.environ["SLACK_HOME_CHANNEL"] = "D0BKGRBM6RH"
        os.environ["GOOGLE_CHAT_HOME_CHANNEL"] = "spaces/AAA"
        self.assertEqual(mod.sibling_delivery_targets(""), [])
        # The real job still resolves, so this narrowed nothing that works.
        self.assertEqual(mod.sibling_delivery_targets("audit"), [])

    def test_a_json_list_is_read_the_same_as_the_comma_form(self):
        """The test above calls a comma string "an explicit list"; this is one.

        A JSON list is the shape hermes treats as native — `hermes_cli/cron.py`
        coerces a string *into* a list and never the reverse — so it is the one
        an operator writing the roster by hand is most likely to produce. It
        used to reach `str()`, come back as `"['chat', 'slack']"`, and split
        into two tokens matching no platform at all. The empty result that
        produced is the same empty result `deliver: "chat"` legitimately
        returns, so nothing anywhere reported a problem: Slack simply received
        the scheduler's copy and the relay's composed copy both.
        """
        for shape in (["chat", "slack"], ("chat", "slack")):
            with self.subTest(deliver=type(shape).__name__):
                self._roster(list(shape))
                os.environ["SLACK_HOME_CHANNEL"] = "D0BKGRBM6RH"
                os.environ["GOOGLE_CHAT_HOME_CHANNEL"] = "spaces/AAA"
                self.assertEqual(mod.sibling_delivery_targets("audit"), ["slack"])

    def test_a_single_entry_json_list_of_the_relay_is_still_relay_only(self):
        # The `deliver: ["chat"]` spelling of the roster's own default. It must
        # reach the same "no siblings" answer as the bare string, not a token
        # set that happens to resolve to nothing for the wrong reason.
        self._roster(["chat"])
        os.environ["SLACK_HOME_CHANNEL"] = "D0BKGRBM6RH"
        os.environ["GOOGLE_CHAT_HOME_CHANNEL"] = "spaces/AAA"
        self.assertEqual(mod.sibling_delivery_targets("audit"), [])

    def test_a_json_list_saying_all_expands_the_same_way(self):
        self._roster(["all"])
        os.environ["SLACK_HOME_CHANNEL"] = "D0BKGRBM6RH"
        os.environ["GOOGLE_CHAT_HOME_CHANNEL"] = "spaces/AAA"
        os.environ["CHAT_HOME_CHANNEL"] = "cron-reports"
        self.assertEqual(mod.sibling_delivery_targets("audit"), ["google_chat", "slack"])

    def test_an_explicit_chat_id_target_is_still_that_platform(self):
        """``platform:chat_id[:thread]`` is a form the scheduler resolves.

        ``_resolve_single_delivery_target`` splits on the first ``:`` and looks
        the prefix up, so the report goes to Slack. Reading the part whole left
        ``slack:D0BKGRBM6RH`` matching no platform and no ``*_HOME_CHANNEL``,
        this returned nothing, and the relay posted a second composed copy into
        the channel the scheduler had just delivered to. Confirmed against the
        live scheduler in the pod on 2026-09-01.
        """
        os.environ["SLACK_HOME_CHANNEL"] = "D0BKGRBM6RH"
        os.environ["GOOGLE_CHAT_HOME_CHANNEL"] = "spaces/AAA"
        for deliver, expected in (
            ("chat,slack:D0BKGRBM6RH", ["slack"]),
            ("chat,google_chat:spaces/AAA:spaces/AAA/threads/T", ["google_chat"]),
            ("chat,SLACK:D0BKGRBM6RH", ["slack"]),
        ):
            with self.subTest(deliver=deliver):
                self._roster(deliver)
                self.assertEqual(mod.sibling_delivery_targets("audit"), expected)

    def test_a_semicolon_is_not_a_separator_the_scheduler_honours(self):
        """Over-reporting is the one direction this must never err in.

        ``cron/scheduler.py::_resolve_delivery_targets`` splits on ``,`` alone,
        so ``slack;x`` is one part it cannot resolve and it delivers nowhere.
        Splitting on ``;`` here named ``slack`` as handled anyway, the relay
        subtracted it, and the report reached no channel at all while the run
        recorded ``ok`` -- the exact silent drop this function's docstring says
        to fail away from. On ``,`` alone the token matches no platform, the
        relay posts, and the channel gets one copy.
        """
        self._roster("chat,slack;x")
        os.environ["SLACK_HOME_CHANNEL"] = "D0BKGRBM6RH"
        self.assertEqual(mod.sibling_delivery_targets("audit"), [])

    def test_a_platform_named_without_a_home_channel_is_not_a_sibling(self):
        """It resolves to nothing, so the scheduler sends it nowhere."""
        self._roster("chat,slack")
        os.environ["GOOGLE_CHAT_HOME_CHANNEL"] = "spaces/AAA"
        self.assertEqual(mod.sibling_delivery_targets("audit"), [])

    def test_an_empty_home_channel_is_not_a_target(self):
        """The scheduler requires a non-empty chat id, so test the value."""
        self._roster("all")
        os.environ["SLACK_HOME_CHANNEL"] = "   "
        os.environ["GOOGLE_CHAT_HOME_CHANNEL"] = "spaces/AAA"
        self.assertEqual(mod.sibling_delivery_targets("audit"), ["google_chat"])

    def test_a_job_the_roster_does_not_carry_names_nothing(self):
        self._roster("all")
        os.environ["GOOGLE_CHAT_HOME_CHANNEL"] = "spaces/AAA"
        self.assertEqual(mod.sibling_delivery_targets("no-such-job"), [])

    def test_an_unreadable_roster_names_nothing(self):
        """Fails toward relaying. Over-reporting would drop a delivery."""
        os.environ["GOOGLE_CHAT_HOME_CHANNEL"] = "spaces/AAA"
        self.assertEqual(mod.sibling_delivery_targets("audit"), [])

    def test_a_corrupt_roster_names_nothing(self):
        with open(os.path.join(self.home, "cron", "jobs.json"), "w") as handle:
            handle.write("{not json")
        os.environ["GOOGLE_CHAT_HOME_CHANNEL"] = "spaces/AAA"
        self.assertEqual(mod.sibling_delivery_targets("audit"), [])

    def test_the_field_rides_along_on_the_relay_payload(self):
        self._roster("all")
        os.environ["GOOGLE_CHAT_HOME_CHANNEL"] = "spaces/AAA"
        os.environ["CHAT_HOME_CHANNEL"] = "cron-reports"
        with RecordingRelay() as relay:
            with patch.dict(
                os.environ,
                {"SESSION_KV_API_KEY": "k", "CRON_REPORT_RELAY_URL": relay.url},
            ):
                asyncio.run(
                    mod.standalone_send(
                        None, "cron-reports", wrapped("Audit", "audit", "a finding")
                    )
                )
        self.assertEqual(
            relay.requests[0]["body"]["also_delivered_to"], ["google_chat"]
        )


class TestRegistration(unittest.TestCase):
    """What the scheduler reads off the ``PlatformEntry``."""

    def test_the_entry_carries_what_cron_delivery_needs(self):
        captured = {}

        class Ctx:
            def register_platform(self, **kwargs):
                captured.update(kwargs)

        mod.register(Ctx())
        self.assertEqual(captured["name"], "chat")
        self.assertEqual(captured["cron_deliver_env_var"], mod.HOME_CHANNEL_ENV)
        self.assertIs(captured["standalone_sender_fn"], mod.standalone_send)
        self.assertIs(captured["is_connected"], mod.is_connected)

    def test_the_platform_name_matches_this_directory(self):
        """``Platform._missing_`` admits a plugin platform by directory name."""
        self.assertEqual(
            mod.PLATFORM_NAME, os.path.basename(os.path.dirname(os.path.abspath(mod.__file__)))
        )

    def test_reports_are_never_chunked(self):
        """A split report would start one Chat Agent turn per piece."""
        captured = {}

        class Ctx:
            def register_platform(self, **kwargs):
                captured.update(kwargs)

        mod.register(Ctx())
        self.assertEqual(captured["max_message_length"], 0)


if __name__ == "__main__":
    unittest.main()
