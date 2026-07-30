"""Talk to this agent's own Action Broker, and to nothing else (06 §9, 08 §2.3).

This is the transport half of the write path. `action_envelope.py` builds the envelope; this file
carries it to the one process allowed to act on it, and turns the reply back into something an LLM
can read. `submit_action` and `plan_action` -- the two MCP tools of 06 §9 -- are plain functions
here, and `platform_mcp_server.py` holds only the `@mcp.tool()` wrappers that call them. That split
is not cosmetic: `mcp` and `pydantic` are not installed in the check environment, so anything left
inside the MCP module is unreachable to an L0 test. Logic lives here; the decorator lives there.

WHAT THIS MODULE IS TRYING NOT TO BE
------------------------------------
A convenience layer. Every field it fills in on the caller's behalf is a field the caller can no
longer get wrong, and every field it *would* let the caller override is a field the broker has to
defend. So:

  * There is no `headers=` parameter, no `url=` parameter, and no `verify=` parameter. The
    destination is the endpoint the operator injected, the routes are the broker's two constants,
    and TLS verification is not expressible as off. A client whose destination is an argument is a
    client that can be pointed somewhere else by whatever writes that argument -- and in this
    process, that is an LLM.
  * Nothing sets a `X-Kube-Agents-*` header. The broker refuses all ten of them outright, journals
    the attempt, and raises a security event (`bypassHeaders` in `server.go`), which is the correct
    treatment of a request that arrives carrying one. This module's job is to never be the reason
    one arrives: a developer reaching for `X-Kube-Agents-Dry-Run` instead of the body's `dryRun` is
    the realistic way that happens, and it fails as a security alarm rather than as a typo.
  * `dryRun` is a parameter of the *envelope*, which means it is inside the idempotency key. A plan
    and the write it previews therefore have different keys and cannot deduplicate each other,
    which is why `plan_action` cannot be `submit_action` with a header.

WHAT IT REFUSES BEFORE SENDING
------------------------------
Only what it can be certain about locally, and always because sending would be worse than failing:

  * **No `KUBEAGENTS_AGENT_IDENTITY`** -- the key would be computed over an empty identity and
    collide with every other agent's. Rendered by the operator (V-BRK-030) beside the endpoint, so
    its absence means the pod was not rendered by an operator that knows about it.
  * **No token file, or an empty one** -- the request would arrive unauthenticated and be refused,
    but the refusal would read as an authorization problem rather than as a missing mount.
  * **No client certificate or CA** -- see below; this one is the important one.

The broker validates all of it again and its answer is the only one that counts. These are for the
caller's benefit, not the broker's.

THE TLS CONTEXT IS THE SECURITY BOUNDARY, AND IT HAS NO OFF SWITCH
------------------------------------------------------------------
mTLS is how the broker knows the caller is a pod it issued a certificate to, and the projected
token is how it knows *which* agent. Both, always (08 §2.3). The failure mode this module is shaped
against is the one every HTTP client in every language eventually grows: a fallback that retries
without verification when the handshake fails, added by someone debugging a certificate problem and
never removed. So there is exactly one place a context is built, it always loads the CA, it always
loads the client keypair, and `check_hostname` and `verify_mode` are set explicitly rather than
left at their defaults -- not because the defaults are wrong today, but because an explicit
assignment is a thing a check can read and a default is not.

The server certificate is verified against `KUBEAGENTS_BROKER_SAN`, not against the host in the
URL. They are the same string today. They are separate variables because the endpoint is a Service
DNS name that a cluster's DNS could be made to resolve elsewhere, and the SAN is what the operator
actually put in the certificate.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from typing import Any

import action_envelope
from action_envelope import EnvelopeError

# --- the five variables the operator renders, and the two routes the broker serves ----------------
# These names are a contract with `agentBrokerEnvVars` in
# `k8s-operator/internal/controller/broker_manifests.go`, and the paths are a contract with
# `ActionsPath` / `NoncePath` in `k8s-operator/internal/broker/server.go`. Both are restated here
# because Python cannot import Go -- and both are read back out of those files and compared by
# `dev/test_broker_client.py`, because a restatement nothing compares is how a rename on one side
# becomes a write path that is silently dead on the other ([[LSN-041]]).

ENV_ENDPOINT = "KUBEAGENTS_BROKER_ENDPOINT"
ENV_SAN = "KUBEAGENTS_BROKER_SAN"
ENV_TOKEN_FILE = "KUBEAGENTS_BROKER_TOKEN_FILE"
ENV_TLS_DIR = "KUBEAGENTS_BROKER_TLS_DIR"
ENV_IDENTITY = "KUBEAGENTS_AGENT_IDENTITY"

ACTIONS_PATH = "/v1alpha1/actions"
NONCE_PATH = "/v1alpha1/nonce"

CLIENT_CERT = "tls.crt"
CLIENT_KEY = "tls.key"
CA_CERT = "ca.crt"

# Generous for a classify-plan-snapshot-journal-execute-verify round trip, finite so a hung broker
# does not hang the agent's whole turn. The broker's own deadline is shorter; this is the backstop
# for the case where it never answers at all.
TIMEOUT_SECONDS = 120
NONCE_TIMEOUT_SECONDS = 15


class BrokerError(RuntimeError):
    """The broker could not be reached, or answered something this client cannot parse.

    Distinct from a *refusal*, which is a perfectly good answer that happens to be "no" and comes
    back as a rendered `ActionResponse`. Conflating the two would let a network partition read as a
    policy decision.
    """


# --- configuration --------------------------------------------------------------------------------


class BrokerConfig:
    """Where the broker is and how to prove who we are. Read from the environment, never argued.

    Constructed from `os.environ` by `from_env()`. The constructor takes explicit values only so a
    test can build one without mutating the process environment; nothing in the agent calls it
    directly.
    """

    def __init__(self, *, endpoint: str, san: str, token_file: str, tls_dir: str, identity: str) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.san = san
        self.token_file = token_file
        self.tls_dir = tls_dir
        self.identity = identity

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "BrokerConfig":
        e = os.environ if env is None else env
        return cls(
            endpoint=e.get(ENV_ENDPOINT, ""),
            san=e.get(ENV_SAN, ""),
            token_file=e.get(ENV_TOKEN_FILE, ""),
            tls_dir=e.get(ENV_TLS_DIR, ""),
            identity=e.get(ENV_IDENTITY, ""),
        )

    def cert_path(self, name: str) -> str:
        return os.path.join(self.tls_dir, name)

    def require(self) -> None:
        """Refuse a configuration that cannot produce an authenticated, correctly-keyed request.

        Each of these names the variable, because the person reading the message is an LLM
        explaining itself to a human, and "the broker refused" is not a thing either can act on.
        """
        missing = [name for name, value in ((ENV_ENDPOINT, self.endpoint), (ENV_SAN, self.san)) if not value]
        if missing:
            raise BrokerError(
                f"this pod has no broker wiring: {', '.join(missing)} is unset. "
                "The operator renders these onto every agent container; an agent without them was not "
                "rendered by a broker-aware operator."
            )
        if not self.identity:
            # Deliberately not defaulted. A key computed over an empty identity is well-formed and
            # collides with every other agent's, and the broker -- which derives the identity itself
            # -- would refuse it as a key mismatch rather than as the wiring fault it is.
            raise BrokerError(
                f"{ENV_IDENTITY} is unset, so the idempotency key cannot be computed for this agent. "
                "This is rendered by the operator alongside the broker endpoint (V-BRK-030)."
            )
        if not self.tls_dir:
            raise BrokerError(f"{ENV_TLS_DIR} is unset; the broker requires a client certificate (mTLS), always.")

    def read_token(self) -> str:
        if not self.token_file:
            raise BrokerError(f"{ENV_TOKEN_FILE} is unset; the broker requires a projected ServiceAccount token, always.")
        try:
            token = open(self.token_file, "r", encoding="utf-8").read().strip()
        except OSError as exc:
            raise BrokerError(f"cannot read the broker token at {self.token_file}: {exc}") from exc
        if not token:
            # An empty file is the shape a projected volume has for a few hundred milliseconds
            # after the pod starts, and an empty Bearer token is refused with a message about
            # authorization rather than about timing.
            raise BrokerError(f"the broker token at {self.token_file} is empty; the projected volume may not be mounted yet")
        return token


# --- transport ------------------------------------------------------------------------------------


def build_ssl_context(cfg: BrokerConfig) -> ssl.SSLContext:
    """The one place a TLS context is constructed. Verification is not a parameter.

    `check_hostname` and `verify_mode` are assigned rather than inherited from
    `create_default_context`, which already sets both to these values. The redundancy is the point:
    a future edit that switched either off has to delete a line that says what it is doing, and a
    check can assert the line is there. An inherited default cannot be distinguished from an
    absent decision.
    """
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=cfg.cert_path(CA_CERT))
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_cert_chain(certfile=cfg.cert_path(CLIENT_CERT), keyfile=cfg.cert_path(CLIENT_KEY))
    return ctx


class BrokerClient:
    """A conversation with one broker. Two routes, no others.

    `opener` exists so `dev/test_broker_client.py` can drive the full request path -- headers,
    body, route, response parsing -- without a TLS listener. It is not a seam the agent uses: the
    default builds the verified context above, and nothing in the MCP tools passes one.
    """

    def __init__(self, cfg: BrokerConfig | None = None, opener: Any = None) -> None:
        self.cfg = cfg or BrokerConfig.from_env()
        self.cfg.require()
        self._opener = opener

    def _open(self, req: urllib.request.Request, timeout: int) -> dict[str, Any]:
        if self._opener is not None:
            return self._opener(req, timeout)
        ctx = build_ssl_context(self.cfg)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return _decode(resp.read(), resp.status)
        except urllib.error.HTTPError as exc:
            # A refusal is an answer. The broker returns the same JSON shape for success and for
            # every refusal precisely so a client parses one thing -- so a 4xx is decoded, not
            # raised. Only a body that is not that shape is an error.
            return _decode(exc.read(), exc.code)
        except urllib.error.URLError as exc:
            raise BrokerError(f"cannot reach the broker at {self.cfg.endpoint}: {exc.reason}") from exc
        except ssl.SSLError as exc:
            # Named separately and never retried. A handshake failure means the certificate the
            # broker presented is not one this pod's CA signed, and the only safe response to that
            # is to stop.
            raise BrokerError(f"TLS handshake with the broker failed: {exc}. Not retrying without verification.") from exc

    def _request(self, path: str, *, method: str, body: bytes | None) -> urllib.request.Request:
        req = urllib.request.Request(self.cfg.endpoint + path, data=body, method=method)
        req.add_header("Authorization", "Bearer " + self.cfg.read_token())
        req.add_header("Accept", "application/json")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        return req

    def fetch_nonce(self) -> str:
        """One nonce, for one submission.

        Never cached. The broker's replay guard retires a nonce on use, so a client that held one
        across two submissions would get the second refused as a replay -- and "replay detected" is
        an alarming thing to find in a journal when the cause is a client optimisation.
        """
        reply = self._open(self._request(NONCE_PATH, method="GET", body=None), NONCE_TIMEOUT_SECONDS)
        nonce = reply.get("nonce") or ""
        if not nonce:
            raise BrokerError(f"the broker issued no nonce: {reply.get('reason') or reply.get('message') or reply}")
        return nonce

    def post_envelope(self, envelope: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        return self._open(self._request(ACTIONS_PATH, method="POST", body=body), TIMEOUT_SECONDS)


def _decode(raw: bytes, status: int) -> dict[str, Any]:
    try:
        reply = json.loads(raw or b"{}")
    except ValueError as exc:
        raise BrokerError(f"the broker returned HTTP {status} with a body that is not JSON: {raw[:200]!r}") from exc
    if not isinstance(reply, dict):
        raise BrokerError(f"the broker returned HTTP {status} with a JSON {type(reply).__name__}, expected an object")
    reply["_status"] = status
    return reply


# --- the two tools of 06 §9 -------------------------------------------------------------------------


def submit_action(
    intent: str,
    operations: list[dict[str, Any]],
    *,
    trigger: dict[str, Any] | None = None,
    requester: dict[str, Any] | None = None,
    trace: dict[str, Any] | None = None,
    rationale: str = "",
    require_approval: bool = False,
    max_objects: int | None = None,
    client: BrokerClient | None = None,
    dry_run: bool = False,
) -> str:
    """The one mutation tool (06 §9). Build, sign for dedup, send, render the answer.

    There is no tier, scope, risk class or approval state to pass. 03 §4.1 step 1 derives (tier,
    scope) from the authenticated identity, and the broker refuses an envelope that names either --
    so the absence of those parameters is the API telling the caller the truth about what it can
    influence. `requireApproval` is the one direction a caller may push: it can ask for *more*
    gating than the classifier decided, never less.
    """
    c = client or BrokerClient()
    try:
        envelope = action_envelope.build_envelope(
            agent_identity=c.cfg.identity,
            intent=intent,
            operations=operations,
            requester=requester or session_requester(),
            trigger=trigger or {"source": "agent"},
            trace=trace or session_trace(),
            nonce=c.fetch_nonce(),
            rationale=rationale,
            dry_run=dry_run,
            require_approval=require_approval,
            max_objects=max_objects,
        )
    except EnvelopeError as exc:
        # Refused locally, before a nonce was spent or anything was journaled. Rendered in the same
        # shape as a broker refusal so the caller has one thing to read, but labelled so it is not
        # mistaken for a policy decision.
        return f"REFUSED (not sent): {exc}"

    return render_response(c.post_envelope(envelope), dry_run=dry_run)


def plan_action(
    intent: str,
    operations: list[dict[str, Any]],
    *,
    trigger: dict[str, Any] | None = None,
    requester: dict[str, Any] | None = None,
    trace: dict[str, Any] | None = None,
    rationale: str = "",
    max_objects: int | None = None,
    client: BrokerClient | None = None,
) -> str:
    """`submit_action` with `dryRun: true` -- the classification and the undo plan, no execution.

    Not a separate route and not a header. `dryRun` is an envelope field, so it is inside the
    idempotency key: a plan and the write it previews hash differently and cannot deduplicate one
    another. A plan that suppressed the subsequent real write would be the single most confusing
    failure this system could produce, and the key is what makes it impossible rather than
    unlikely.
    """
    return submit_action(
        intent,
        operations,
        trigger=trigger,
        requester=requester,
        trace=trace,
        rationale=rationale,
        max_objects=max_objects,
        client=client,
        dry_run=True,
    )


# --- what the session contributes -------------------------------------------------------------------


def session_trace() -> dict[str, Any]:
    """The trace this turn belongs to.

    `TRACE_ID` is set by the agent runtime when a turn begins. A fresh one is generated when it is
    absent rather than refusing, because a missing trace degrades an incident timeline and a
    refused action degrades the product -- and unlike the identity, nothing downstream is made
    *wrong* by a trace that only covers the broker leg.
    """
    trace_id = (os.environ.get("TRACE_ID") or "").strip().lower()
    if not action_envelope._HEX32.match(trace_id):
        trace_id = action_envelope.new_nonce()
    out: dict[str, Any] = {"traceId": trace_id}
    span = (os.environ.get("SPAN_ID") or "").strip()
    if span:
        out["parentSpanId"] = span
    return out


def session_requester() -> dict[str, Any]:
    """Who asked for this, as far as the agent can tell.

    Recorded, never trusted: 06 §4.1 says an unsigned `requester.id` produces a record carrying
    `attributionUnverified: true`. The default is the agent itself, which is the honest answer when
    nothing in the session names a human -- attributing an autonomous action to whoever last spoke
    to the agent is worse than saying "the agent did this".
    """
    user = (os.environ.get("SESSION_USER_ID") or "").strip()
    if user:
        return {"kind": "human", "id": user}
    return {"kind": "agent", "id": os.environ.get("AGENT_NAME") or os.environ.get(ENV_IDENTITY) or "agent"}


# --- rendering ----------------------------------------------------------------------------------


# Every key read out of a reply is a `json` tag on `broker.Response` in `server.go`. The set is
# named here, once, so `dev/test_broker_client.py` can read the struct's tags and assert this is a
# subset -- a field renamed on the Go side otherwise turns into a rendered "unknown" that reads
# like a broker that answered vaguely.
RESPONSE_FIELDS = ("actionId", "namespace", "decision", "phase", "message", "reason", "traceId", "retryAfterSeconds")


def render_response(reply: dict[str, Any], *, dry_run: bool = False) -> str:
    """Turn the broker's reply into text an LLM can act on without re-deriving the schema.

    A refusal is rendered as prominently as an acceptance. The failure being avoided is an agent
    that reads a 403 body, finds no `actionId`, and reports "done" -- which is the one wrong answer
    that is worse than any error, because it puts a false statement about the cluster into a
    human's hands.
    """
    status = reply.get("_status", 0)
    reason = reply.get("reason") or ""
    decision = reply.get("decision") or ""
    message = reply.get("message") or ""

    if reason or decision == "rejected" or status >= 400:
        lines = [f"REFUSED by the broker: {reason or 'unspecified'}"]
        if message:
            lines.append(message)
        retry = reply.get("retryAfterSeconds") or 0
        if retry:
            lines.append(f"This is temporary: retry after {retry}s.")
        if reply.get("traceId"):
            lines.append(f"trace: {reply['traceId']}")
        return "\n".join(lines)

    head = "PLANNED (dry run, nothing was changed)" if dry_run else f"SUBMITTED: decision={decision or 'accepted'}"
    lines = [head]
    if message:
        lines.append(message)
    if reply.get("actionId"):
        lines.append(f"actionId: {reply['actionId']}")
    if reply.get("namespace"):
        lines.append(f"record namespace: {reply['namespace']}")
    if reply.get("phase"):
        lines.append(f"phase: {reply['phase']}")
    if reply.get("traceId"):
        lines.append(f"trace: {reply['traceId']}")
    return "\n".join(lines)
