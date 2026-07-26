#!/usr/bin/env python3
"""L1 tests for the headless MCP bridge (kube-agents Phase 8, P8-T4).

The bridge exists because its predecessor hung. `mcp-remote` wanted an
interactive OAuth flow that cannot complete in a pod, so the agent's MCP client
waited on a server that would never answer. "Does not hang" is therefore the
property under test, and it has two halves that fail independently:

  * the **process** must not block — no identity means exit non-zero, fast;
  * the **client** must not block — every request carrying an `id` gets exactly
    one reply, including when the remote is broken, slow, or absent.

Both halves are asserted against wall-clock bounds, because the failure being
guarded against is precisely "correct eventually, after forever". Everything runs
against stdlib HTTP stubs on loopback: no cluster, no network, no credentials.

Picked up by `python3 -m unittest discover dev`, which is already in the
harness regress chain — the mechanization is the file being discovered, not a
line in a document promising someone will run it (LSN-019).
"""

from __future__ import annotations

import http.server
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BRIDGE = REPO / "deploy" / "shared" / "defaults" / "scripts" / "mcp_http_bridge.py"

sys.path.insert(0, str(BRIDGE.parent))
import mcp_http_bridge  # noqa: E402


class StubHandler(http.server.BaseHTTPRequestHandler):
    """Serves both roles: the metadata token endpoint and the remote MCP endpoint.

    One server for both keeps the fixture to a single port and a single teardown.
    Which role a request lands in is decided by path, exactly as it would be by
    hostname in production.
    """

    behaviour = "json"  # json | sse | http500 | slow | notification

    def log_message(self, *_args):  # keep the test output readable
        pass

    def do_GET(self):
        if self.path.startswith("/computeMetadata/v1/instance/service-accounts"):
            if self.headers.get("Metadata-Flavor") != "Google":
                self.send_error(403, "missing Metadata-Flavor")
                return
            body = json.dumps({"access_token": "stub-token-abc", "expires_in": 3600})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body.encode())
            return
        self.send_error(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        req = json.loads(self.rfile.read(n) or b"{}")
        type(self).last_auth = self.headers.get("Authorization")
        type(self).last_request = req

        if type(self).behaviour == "http500":
            self.send_error(500, "stub is broken on purpose")
            return
        if type(self).behaviour == "slow":
            time.sleep(30)  # far beyond the timeout the test sets
            return
        if type(self).behaviour == "notification":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        reply = {"jsonrpc": "2.0", "id": req.get("id"), "result": {"echo": req.get("method")}}
        if type(self).behaviour == "sse":
            payload = f"data: {json.dumps(reply)}\n\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        body = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Mcp-Session-Id", "stub-session-1")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class BridgeTestBase(unittest.TestCase):
    def setUp(self):
        StubHandler.behaviour = "json"
        StubHandler.last_auth = None
        StubHandler.last_request = None
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        self.host = f"127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        os.environ["GCE_METADATA_HOST"] = self.host
        self.addCleanup(os.environ.pop, "GCE_METADATA_HOST", None)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def run_bridge(self, lines: list[str]) -> list[dict]:
        """Drive the bridge in-process over a fixed stdin, return what it wrote."""
        token = mcp_http_bridge.Token()
        bridge = mcp_http_bridge.Bridge(f"http://{self.host}/mcp", token)
        out = io.StringIO()
        bridge.run(io.StringIO("".join(l + "\n" for l in lines)), out)
        return [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]


class TestHappyPath(BridgeTestBase):
    def test_request_is_relayed_and_answered(self):
        replies = self.run_bridge([json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})])
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["id"], 1)
        self.assertEqual(replies[0]["result"]["echo"], "tools/list")

    def test_metadata_token_is_presented_as_a_bearer(self):
        """The point of the rewrite: the pod's own identity, no interactive flow."""
        self.run_bridge([json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})])
        self.assertEqual(StubHandler.last_auth, "Bearer stub-token-abc")

    def test_session_id_is_echoed_on_later_requests(self):
        token = mcp_http_bridge.Token()
        bridge = mcp_http_bridge.Bridge(f"http://{self.host}/mcp", token)
        bridge.forward({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(bridge.session_id, "stub-session-1")

    def test_sse_response_is_unwrapped(self):
        StubHandler.behaviour = "sse"
        replies = self.run_bridge([json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call"})])
        self.assertEqual([r["id"] for r in replies], [7])

    def test_notification_gets_no_reply(self):
        """A message with no `id` must produce no output — silence is the protocol."""
        StubHandler.behaviour = "notification"
        replies = self.run_bridge([json.dumps({"jsonrpc": "2.0", "method": "notifications/ready"})])
        self.assertEqual(replies, [])


class TestNeverLeavesTheClientWaiting(BridgeTestBase):
    """Half two of "does not hang": the process is fine, the caller is stranded."""

    def test_remote_error_becomes_a_jsonrpc_error(self):
        StubHandler.behaviour = "http500"
        replies = self.run_bridge([json.dumps({"jsonrpc": "2.0", "id": 42, "method": "tools/list"})])
        self.assertEqual(len(replies), 1, "an HTTP 500 must still produce a reply")
        self.assertEqual(replies[0]["id"], 42)
        self.assertIn("500", replies[0]["error"]["message"])

    def test_unreachable_remote_becomes_a_jsonrpc_error(self):
        token = mcp_http_bridge.Token()
        # Port 1 on loopback: nothing listens, so this is a connection refusal.
        bridge = mcp_http_bridge.Bridge("http://127.0.0.1:1/mcp", token)
        out = io.StringIO()
        bridge.run(io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 9, "method": "x"}) + "\n"), out)
        replies = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["id"], 9)

    def test_slow_remote_times_out_rather_than_blocking(self):
        StubHandler.behaviour = "slow"
        mcp_http_bridge.REQUEST_TIMEOUT = 2.0
        self.addCleanup(setattr, mcp_http_bridge, "REQUEST_TIMEOUT", 60.0)
        started = time.monotonic()
        replies = self.run_bridge([json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})])
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 15, f"bridge blocked {elapsed:.1f}s on a slow remote")
        self.assertEqual(len(replies), 1)
        self.assertIn("error", replies[0])

    def test_malformed_stdin_does_not_kill_the_session(self):
        """One bad line must not take out every request behind it."""
        replies = self.run_bridge(
            ["{not json", json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/list"})]
        )
        self.assertEqual([r["id"] for r in replies], [5])


class TestFailsFastWithoutIdentity(unittest.TestCase):
    """Half one: no identity means exit non-zero, quickly, with a reason.

    Run as a subprocess, because what is being asserted is the exit status and
    the wall clock of the real entry point — the thing a pod actually executes.
    """

    def test_no_metadata_server_exits_nonzero_within_seconds(self):
        env = {
            **os.environ,
            # Port 1 on loopback refuses immediately; the timeout bound is proven
            # by the slow-remote test, and a blackhole IP here would just make
            # this test slow for everyone.
            "GCE_METADATA_HOST": "127.0.0.1:1",
            "MCP_BRIDGE_TOKEN_TIMEOUT": "2",
        }
        started = time.monotonic()
        proc = subprocess.run(
            [sys.executable, str(BRIDGE), "https://example.invalid/mcp"],
            input="",
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        elapsed = time.monotonic() - started
        self.assertNotEqual(proc.returncode, 0, "a bridge with no identity must not report success")
        self.assertLess(elapsed, 20, f"bridge took {elapsed:.1f}s to give up on the metadata server")
        self.assertIn("workload identity", proc.stderr.lower())

    def test_no_endpoint_argument_is_a_usage_error(self):
        proc = subprocess.run(
            [sys.executable, str(BRIDGE)], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(proc.returncode, 2)

    def test_bridge_contains_no_interactive_flow(self):
        """The regression guard for the actual defect.

        Weaker than the behavioural tests above and kept anyway: those prove this
        implementation does not hang, while this one refuses the *shape* of the
        code that hung — a browser launch or a loopback redirect listener. Someone
        adding OAuth back "just as a fallback" trips this before it ships.
        """
        src = BRIDGE.read_text()
        for banned in ("webbrowser", "socketserver", "HTTPServer", "input(", "getpass"):
            self.assertNotIn(banned, src, f"{banned} implies an interactive or callback flow")


class TestNothingStillWiresTheOAuthProxy(unittest.TestCase):
    """`mcp-remote` is gone; keep it gone.

    The bridge fixes nothing if a config anywhere still starts the proxy — and
    there are five places that could: the shared default, each tier's overlay,
    and the operator's runtime-authoritative render. The install path is what
    decides which one wins, so all of them are checked (LSN-007).
    """

    CANDIDATES = [
        "deploy/shared/defaults/config.yaml",
        "agents/platform/config.yaml",
        "agents/cluster-admin/config.yaml",
        "agents/developer-team/config.yaml",
        "k8s-operator/internal/controller/agent_manifests.go",
        "deploy/docker/Dockerfile",
    ]

    @staticmethod
    def _code_part(line: str, suffix: str) -> str:
        """Return the part of `line` a parser would act on, with any comment removed.

        Comments are excluded deliberately, not for convenience. The reason the
        proxy is gone is written down in three of these files, and a check that
        cannot tell an explanation from a wiring would force the explanation to be
        deleted — losing exactly the note that stops someone re-adding it. A
        commented-out proxy block is not wiring either: nothing starts it.
        """
        marker = "//" if suffix == ".go" else "#"
        idx = line.find(marker)
        # Only a marker at the start of the line or after whitespace opens a comment,
        # so `//` inside a URL and `#` inside a quoted string are still scanned.
        if idx == -1 or not (idx == 0 or line[idx - 1].isspace()):
            return line
        return line[:idx]

    def test_no_config_or_renderer_references_mcp_remote(self):
        offenders = []
        for rel in self.CANDIDATES:
            path = REPO / rel
            if not path.is_file():
                offenders.append(f"{rel}: missing — this check has stopped covering it")
                continue
            suffix = path.suffix
            for n, line in enumerate(path.read_text().splitlines(), 1):
                code = self._code_part(line, suffix)
                if "mcp-remote" in code or "proxy.js" in code:
                    offenders.append(f"{rel}:{n}: {line.strip()[:100]}")
        self.assertEqual(offenders, [], "the headless-hanging OAuth proxy is wired again:\n" + "\n".join(offenders))

    def test_the_scanner_still_catches_a_real_rewiring(self):
        """A check that cannot fail is not evidence (09 §6, V-MET-014).

        Stripping comments is the kind of exemption that quietly turns a guard
        into decoration, so the guard is shown failing on the three shapes a real
        re-wiring would take, and passing on the prose that explains its absence.
        """
        strip = self._code_part
        self.assertIn("mcp-remote", strip('      - "/opt/mcp-remote/dist/proxy.js"', ".yaml"))
        self.assertIn("proxy.js", strip('\targs: []string{"/opt/mcp-remote/dist/proxy.js"},', ".go"))
        self.assertIn("mcp-remote", strip("RUN git clone https://github.com/x/mcp-remote.git", ""))
        self.assertNotIn("mcp-remote", strip("  # mcp-remote was removed here", ".yaml"))
        self.assertNotIn("proxy.js", strip("\t// proxy.js is gone", ".go"))

    def test_rendered_goldens_use_the_bridge(self):
        """The goldens are what the operator actually produces. Check those too."""
        goldens = sorted((REPO / "k8s-operator/internal/testing/testdata").glob("*/expected/agent.yaml"))
        self.assertTrue(goldens, "no rendered goldens found — this check covers nothing")
        for g in goldens:
            text = g.read_text()
            self.assertNotIn("proxy.js", text, f"{g.relative_to(REPO)} still renders the OAuth proxy")
            self.assertIn("mcp_http_bridge.py", text, f"{g.relative_to(REPO)} does not render the bridge")


if __name__ == "__main__":
    unittest.main()
