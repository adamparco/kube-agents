#!/usr/bin/env python3
"""Headless stdio <-> HTTP bridge for a remote MCP server (kube-agents, Phase 8 P8-T4).

WHAT THIS REPLACES AND WHY. The `developer_knowledge` MCP server used to be
`node /opt/mcp-remote/dist/proxy.js https://developerknowledge.googleapis.com/mcp`.
`mcp-remote` authenticates with an OAuth 2.0 authorization-code flow: it opens a
browser and listens on a loopback port for the redirect. There is no browser in a
pod, so the flow never completes and the bridge never exits — the agent's MCP
client sits waiting on a server that will never answer. That is the worst shape a
failure can take here, because it is indistinguishable from "still starting up".

An agent pod already has an identity: the GKE metadata server mints a token bound
to its Kubernetes ServiceAccount through Workload Identity. This bridge uses that
token. No browser, no callback listener, no interactive step exists in this file.

THE TWO SEPARATE WAYS A BRIDGE CAN HANG, AND WHAT IS DONE ABOUT EACH.

  1. *The process blocks.* Every network operation here has a finite timeout, and
     the token is fetched **before** the loop starts. If the pod has no usable
     identity the bridge exits non-zero within seconds, with the reason on stderr.
     It does not stay up answering every call with an error: a bridge that cannot
     authenticate cannot serve anything, and staying alive would only convert one
     legible startup failure into an unbounded series of confusing tool failures.

  2. *The client blocks.* Subtler and just as fatal: an MCP request carrying an
     `id` that never receives a response leaves the caller waiting forever, even
     though this process is alive and healthy. So every request with an `id` gets
     exactly one reply — a JSON-RPC error object if the hop failed. Notifications
     (no `id`) get no reply, which is what the protocol requires.

TRANSPORT. MCP Streamable HTTP. Each JSON-RPC message read from stdin is POSTed to
the endpoint; the reply is either `application/json` (one message) or
`text/event-stream` (a short SSE stream, terminated once the message carrying our
`id` arrives). The `Mcp-Session-Id` header the server returns on initialize is
echoed on every later request.

Usage:
    mcp_http_bridge.py <endpoint-url>

Environment:
    GCE_METADATA_HOST         metadata server host[:port]  (default metadata.google.internal)
    MCP_BRIDGE_TIMEOUT        per-request seconds          (default 60)
    MCP_BRIDGE_TOKEN_TIMEOUT  token fetch seconds          (default 5; the metadata
                              server is link-local, so slow means broken)
    MCP_BRIDGE_SCOPES         comma-separated OAuth scopes (default: the identity's own)

Stdlib only, deliberately: this runs on the boot path of every agent pod, and the
one thing it must never do is fail to start because of a missing dependency.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_METADATA_HOST = "metadata.google.internal"
TOKEN_PATH = "/computeMetadata/v1/instance/service-accounts/default/token"

REQUEST_TIMEOUT = float(os.environ.get("MCP_BRIDGE_TIMEOUT", "60"))
TOKEN_TIMEOUT = float(os.environ.get("MCP_BRIDGE_TOKEN_TIMEOUT", "5"))

# Refresh this long before expiry. A token that expires mid-request produces a 401
# the caller reads as "the knowledge API rejected me", which is a lie.
REFRESH_MARGIN = 60.0


def log(msg: str) -> None:
    """Diagnostics go to stderr. Stdout is the JSON-RPC channel and nothing else."""
    print(f"mcp-bridge: {msg}", file=sys.stderr, flush=True)


class Token:
    """The pod's own identity, fetched from the metadata server and cached.

    Not `google.auth`: that pulls in `requests` through `google-api-core`, and a
    boot-path bridge that dies on an import is a worse failure than the one being
    fixed. The endpoint below is the same one `google.auth` would call, and
    `GCE_METADATA_HOST` is the same override it honours.
    """

    def __init__(self) -> None:
        host = os.environ.get("GCE_METADATA_HOST", DEFAULT_METADATA_HOST)
        self.url = f"http://{host}{TOKEN_PATH}"
        scopes = os.environ.get("MCP_BRIDGE_SCOPES", "").strip()
        if scopes:
            self.url += "?scopes=" + urllib.parse.quote(scopes)
        self._value = ""
        self._expires_at = 0.0

    def get(self) -> str:
        if self._value and time.monotonic() < self._expires_at - REFRESH_MARGIN:
            return self._value
        req = urllib.request.Request(self.url, headers={"Metadata-Flavor": "Google"})
        with urllib.request.urlopen(req, timeout=TOKEN_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        token = payload.get("access_token")
        if not token:
            raise RuntimeError(f"metadata server returned no access_token: {payload!r}")
        self._value = token
        # Absent expires_in, assume the shortest plausible life rather than the
        # longest — re-fetching too often is free, using a dead token is not.
        self._expires_at = time.monotonic() + float(payload.get("expires_in", 300))
        return self._value


def error_reply(msg_id: object, code: int, message: str) -> dict:
    """A JSON-RPC error, so the caller gets an answer instead of silence."""
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def read_sse(stream, want_id: object) -> list[dict]:
    """Drain an SSE body, returning the JSON messages it carried.

    Stops once the message answering `want_id` has been seen. Servers normally
    close the stream themselves; stopping on our own answer means a server that
    holds the connection open cannot stall the next request behind it.
    """
    out: list[dict] = []
    for raw in stream:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if not body:
            continue
        try:
            msg = json.loads(body)
        except json.JSONDecodeError:
            log(f"dropping non-JSON SSE frame: {body[:120]!r}")
            continue
        out.append(msg)
        if want_id is not None and isinstance(msg, dict) and msg.get("id") == want_id:
            break
    return out


class Bridge:
    def __init__(self, endpoint: str, token: Token) -> None:
        self.endpoint = endpoint
        self.token = token
        self.session_id = ""

    def headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token.get()}",
        }
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def forward(self, msg: object) -> list[dict]:
        """POST one message; return the messages to write back to stdout."""
        msg_id = msg.get("id") if isinstance(msg, dict) else None
        data = json.dumps(msg).encode("utf-8")
        req = urllib.request.Request(self.endpoint, data=data, headers=self.headers())
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "text/event-stream" in ctype:
                    return read_sse(resp, msg_id)
                body = resp.read().decode("utf-8").strip()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            log(f"HTTP {exc.code} from {self.endpoint}: {detail}")
            if msg_id is None:
                return []
            return [error_reply(msg_id, -32000, f"remote MCP returned HTTP {exc.code}")]
        except Exception as exc:  # timeouts, DNS, TLS, a dead metadata server mid-run
            log(f"{type(exc).__name__} talking to {self.endpoint}: {exc}")
            if msg_id is None:
                return []
            return [error_reply(msg_id, -32000, f"remote MCP unreachable: {exc}")]

        # 202 with an empty body is the correct answer to a notification.
        if not body:
            return []
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            log(f"non-JSON response body: {body[:120]!r}")
            if msg_id is None:
                return []
            return [error_reply(msg_id, -32700, "remote MCP returned a non-JSON body")]
        return parsed if isinstance(parsed, list) else [parsed]

    def run(self, stdin, stdout) -> int:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # Malformed input is the client's bug, not a reason to die: a
                # parse error mid-session would take out every later request too.
                log(f"dropping unparseable stdin line: {line[:120]!r}")
                continue
            for reply in self.forward(msg):
                stdout.write(json.dumps(reply) + "\n")
                stdout.flush()
        return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        log("usage: mcp_http_bridge.py <endpoint-url>")
        return 2
    endpoint = argv[1]

    token = Token()
    try:
        token.get()
    except Exception as exc:
        # The whole point of this file. Say what failed, name the likely cause,
        # and exit — do not wait for an identity that is not coming.
        log(f"cannot obtain a workload identity token ({type(exc).__name__}: {exc}).")
        log(f"  metadata endpoint: {token.url}")
        log("  On GKE this means Workload Identity is off for this pod, or the egress")
        log("  NetworkPolicy does not admit the metadata server. This bridge does not")
        log("  fall back to an interactive OAuth flow, so it stops here rather than hanging.")
        return 1

    log(f"authenticated; bridging stdio <-> {endpoint}")
    return Bridge(endpoint, token).run(sys.stdin, sys.stdout)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
