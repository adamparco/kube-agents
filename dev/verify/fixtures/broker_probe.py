#!/usr/bin/env python3
"""broker_probe.py — present a series of different credentials at one DEPLOYED broker and report,
as one JSON object per line, exactly what it said to each.

This runs INSIDE the cluster, in the pod `dev/lib/broker-driver.sh` renders, as the agent's own
reader ServiceAccount. It is the client `broker-per-agent-l2.sh` said it did not have: that suite
proves each broker pod *runs* and states as a non-claim that it does not prove the broker *serves*,
"because no client here holds a certificate". This holds one.

WHY IT IS A POD AND NOT A PORT-FORWARD
    The shipped client's own module docstring says "The server certificate is verified against
    `KUBEAGENTS_BROKER_SAN`, not against the host in the URL", which would have made a
    `https://127.0.0.1:PORT` tunnel from the working tree verify correctly. It is not true.
    `BrokerConfig.san` is read from the environment, required by `require()`, and then never used
    again; `build_ssl_context` sets `check_hostname = True` and `urllib` derives `server_hostname`
    from the URL, so the name actually checked is the endpoint's host. Nothing is broken today —
    the two strings are equal — but the shortcut it appeared to license does not exist. Filed as a
    finding against the shipped client; not fixed from a test fixture.

    Being in the cluster is the better answer anyway: the request crosses the real `<agent>-broker`
    Service, the real `<agent>-broker-ingress` NetworkPolicy and the real `<agent>-to-broker` egress
    hop, none of which a tunnel touches.

WHAT IS SHIPPED CODE AND WHAT IS THE FIXTURE
    `broker_client` and `action_envelope` are the files this repo ships to every agent tier, mounted
    byte-for-byte from the working tree. The POSITIVE scenarios go through them and nothing else.

    The negatives deliberately do NOT. The shipped client offers no way to omit the Authorization
    header, to present a different token, or to build a context without a client certificate — that
    absence is a property `dev/test_broker_client.py` asserts, and a seam added here to exercise it
    would be a seam an agent could reach. So the negatives use `urllib` directly, with the TLS
    material and the endpoint still taken from the shipped `BrokerConfig`. Every one of them is a
    claim about what the SERVER refuses, which is where the claim belongs.

OUTPUT CONTRACT, read by `dev/verify/broker-auth-l2.sh`
    One JSON object per line on stdout, and nothing else on stdout:
        {"scenario": str,
         "outcome":  "http" | "transport-error" | "probe-error",
         "status":   int | null,      # HTTP status, when one was received
         "reason":   str,             # the broker's machine-readable refusal reason, when parsed
         "detail":   str}             # prose: the broker's message, or the exception
    `transport-error` means no HTTP status was ever received — the connection did not become a
    usable TLS session. For the mTLS negatives that IS the pass condition, and it is a distinct
    outcome from an HTTP refusal precisely so the suite cannot accept one for the other.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request

import action_envelope
import broker_client
from broker_client import CA_CERT, CLIENT_CERT, CLIENT_KEY, BrokerConfig

# Set by the driver pod. Each names a credential the agent itself never has; they exist so the
# refusal paths can be exercised with real material rather than with a mock.
ENV_FOREIGN_TLS_DIR = "PROBE_FOREIGN_TLS_DIR"
ENV_FOREIGN_TOKEN = "PROBE_FOREIGN_TOKEN"
ENV_UNTRUSTED_TLS_DIR = "PROBE_UNTRUSTED_TLS_DIR"
ENV_DEFAULT_AUDIENCE_TOKEN_FILE = "PROBE_DEFAULT_AUDIENCE_TOKEN_FILE"

TIMEOUT = 30


def emit(
    scenario: str,
    *,
    outcome: str,
    status: int | None = None,
    reason: str = "",
    detail: str = "",
    retry: int = 0,
) -> None:
    """One transcript line. `retry` is the broker's `retryAfterSeconds`, and zero is a real answer.

    It is carried because V-BRK-031's whole property is the SPLIT between a refusal that could
    succeed on a retry and one that never will, and the status alone does not express it: 403
    `target-forbidden` and 503 `snapshot-failed` are both refusals of the same read, and the only
    machine-readable difference an agent runtime can act on is whether it was given a wait.
    """
    print(
        json.dumps(
            {
                "scenario": scenario,
                "outcome": outcome,
                "status": status,
                "reason": reason,
                "detail": detail,
                "retryAfterSeconds": retry,
            }
        ),
        flush=True,
    )


def context_for(tls_dir: str | None, *, ca_dir: str) -> ssl.SSLContext:
    """A TLS context whose CLIENT half is chosen and whose SERVER half never is.

    `ca_dir` is always the agent's own mesh CA: every scenario here verifies the broker's identity,
    including the ones that expect to be refused. A negative that also stopped checking the server
    would be two changes at once, and a refusal it produced could be the server's or the client's.
    """
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=os.path.join(ca_dir, CA_CERT))
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    if tls_dir is not None:
        ctx.load_cert_chain(certfile=os.path.join(tls_dir, CLIENT_CERT), keyfile=os.path.join(tls_dir, CLIENT_KEY))
    return ctx


def _body_of(reader: object) -> tuple[dict[str, object], str]:
    """Whatever the peer sent, as (parsed-broker-reply, raw-text).

    Both, because a non-JSON body is itself a result here: Go's TLS listener answers a plaintext
    request on an HTTPS port with `400 Bad Request` and the bare sentence "Client sent an HTTP
    request to an HTTPS server." — an HTTP status the BROKER never produced. A reader that only
    parsed JSON would report that as `{}` and it would be indistinguishable from a broker refusal
    with an empty reason.

    Reading it can also fail outright: the same listener resets the connection immediately after
    writing that line, so `exc.read()` raises `ConnectionResetError` from inside the HTTPError
    handler. That escaped the first time this ran and killed the probe four scenarios early.
    """
    try:
        raw = reader.read() or b""  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        return {}, f"<body unreadable: {type(exc).__name__}: {exc}>"
    text = raw.decode("utf-8", "replace").strip()
    try:
        parsed = json.loads(raw or b"{}")
    except ValueError:
        return {}, text
    return (parsed, text) if isinstance(parsed, dict) else ({}, text)


def raw_get(scenario: str, url: str, *, ctx: ssl.SSLContext | None, token: str | None) -> None:
    """One GET, with exactly the credentials named, and every failure mode reported as itself."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", "application/json")
    if token is not None:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            body, text = _body_of(resp)
            emit(scenario, outcome="http", status=resp.status, reason=str(body.get("reason", "")), detail=(str(body.get("message", "")) or text)[:400])
    except urllib.error.HTTPError as exc:
        # A refusal is an answer, and the broker returns the same JSON shape for one. Decoded, not
        # raised: the reason string is the whole point of these scenarios.
        body, text = _body_of(exc)
        emit(scenario, outcome="http", status=exc.code, reason=str(body.get("reason", "")), detail=(str(body.get("message", "")) or text)[:400])
    except Exception as exc:  # noqa: BLE001 — the class name IS the evidence for a transport negative
        emit(scenario, outcome="transport-error", detail=f"{type(exc).__name__}: {exc}"[:400])


def read_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().strip()


def probe_target_operation() -> list[dict[str, object]]:
    """One well-formed operation, on an object chosen to make the outcome uninteresting.

    Whether the pipeline ACCEPTS this envelope is not what is being measured — a policy refusal and
    an acceptance are both proof that the transport carried a real envelope from an authenticated
    caller. What must not happen is a 401 or a 403, and that is what the suite asserts.

    The shape is 06 §4.1's closed schema, which `DecodeEnvelope` enforces with
    `DisallowUnknownFields`: `target` is group/version/kind/name (no `apiVersion`), and the payload
    for an `apply` is `desiredState`. A near-miss here would come back as a 400 `unknown-field`,
    which the suite would accept — the row is about 401 and 403 — so it would pass while proving
    less than it says. Written out rather than trusted.
    """
    ns = os.environ.get("PROBE_NAMESPACE", "kubeagents-system")
    name = "broker-auth-l2-probe-target"
    return [
        {
            "op": "apply",
            "target": {"version": "v1", "kind": "ConfigMap", "namespace": ns, "name": name},
            "desiredState": {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": name, "namespace": ns},
                "data": {"probe": "broker-auth-l2"},
            },
        }
    ]


def main() -> int:
    cfg = BrokerConfig.from_env()
    try:
        cfg.require()
    except Exception as exc:  # noqa: BLE001
        emit("config", outcome="probe-error", detail=f"{type(exc).__name__}: {exc}")
        return 1

    own_tls = cfg.tls_dir
    foreign_tls = os.environ.get(ENV_FOREIGN_TLS_DIR, "")
    untrusted_tls = os.environ.get(ENV_UNTRUSTED_TLS_DIR, "")
    foreign_token = os.environ.get(ENV_FOREIGN_TOKEN, "")
    default_audience_file = os.environ.get(ENV_DEFAULT_AUDIENCE_TOKEN_FILE, "")

    nonce_url = cfg.endpoint + broker_client.NONCE_PATH

    # --- the baseline, FIRST ---------------------------------------------------------------------
    # Every scenario below is an assertion that something is refused, and a refusal proves nothing
    # about a door that was never open. This runs first and the suite treats its failure as fatal.
    own_token = ""
    try:
        client = broker_client.BrokerClient(cfg)
        nonce = client.fetch_nonce()
        emit("nonce-accepted", outcome="http", status=200, reason="", detail=f"nonce issued, {len(nonce)} chars")
        own_token = cfg.read_token()
    except Exception as exc:  # noqa: BLE001
        emit("nonce-accepted", outcome="probe-error", detail=f"{type(exc).__name__}: {exc}"[:400])
        return 1

    # The same credentials carrying a real envelope, built by the shipped builder. `post_envelope`
    # rather than `submit_action`: the tool renders the reply to human text and drops the status
    # code, which is the one field a check needs.
    try:
        envelope = action_envelope.build_envelope(
            agent_identity=cfg.identity,
            intent="broker-auth-l2 transport probe",
            operations=probe_target_operation(),
            requester={"kind": "system", "id": "broker-auth-l2"},
            # `cron` because 06 §4.1's trigger sources are a CLOSED set of seven — chat, watch,
            # alert, cron, delegation, escalation, undo — and an unattended automated submission is
            # the one this is. The first run of this probe used "verification" and came back 400
            # `invalid-envelope`, which is a second instance of the `parentSpanId` finding:
            # `action_envelope.py` mirrors the operation verbs and the patch media types from the Go
            # side but not the trigger sources, so the client-side courtesy check passes anything
            # non-empty and the caller learns the real set from a refusal. Folded into P9-T8b-4c.
            trigger={"source": "cron"},
            # The shipped trace builder, with SPAN_ID deliberately unset in the driver pod. When it
            # IS set, `session_trace()` emits `parentSpanId` and the broker's `Trace` struct has no
            # such field — `DisallowUnknownFields` turns the whole envelope into a 400
            # `unknown-field`. That is a real defect in shipped agent code, filed against
            # `broker_client.py`, and it is not this fixture's to fix: setting SPAN_ID here would
            # measure it, which belongs in the unit that repairs it.
            trace=broker_client.session_trace(),
            nonce=client.fetch_nonce(),
            rationale="V-BRK-007/008/009/010/017: establish that an authenticated caller gets in, so that the refusals below are not vacuous.",
            dry_run=True,
        )
        reply = client.post_envelope(envelope)
        emit(
            "envelope-accepted",
            outcome="http",
            status=reply.get("_status"),
            reason=reply.get("reason", ""),
            detail=(reply.get("message") or reply.get("decision") or "")[:400],
            retry=int(reply.get("retryAfterSeconds") or 0),
        )
    except Exception as exc:  # noqa: BLE001
        emit("envelope-accepted", outcome="probe-error", detail=f"{type(exc).__name__}: {exc}"[:400])

    own_ctx = context_for(own_tls, ca_dir=own_tls)

    # --- V-BRK-009: valid mTLS, no token / a bad token --------------------------------------------
    raw_get("no-token", nonce_url, ctx=own_ctx, token=None)
    raw_get("bad-token", nonce_url, ctx=own_ctx, token="not-a-token")

    # --- V-BRK-008 / V-BRK-017: the default-audience token ----------------------------------------
    # The pod's automounted `kubernetes.io/serviceaccount` token: the SAME ServiceAccount the broker
    # expects, genuinely signed, and `TokenReview` returns `authenticated: true` for it. A broker
    # that stopped reading at that field would accept it and fail nothing else in this file.
    if default_audience_file:
        try:
            raw_get("default-audience", nonce_url, ctx=own_ctx, token=read_file(default_audience_file))
        except OSError as exc:
            emit("default-audience", outcome="probe-error", detail=f"{type(exc).__name__}: {exc}")
    else:
        emit("default-audience", outcome="probe-error", detail=f"{ENV_DEFAULT_AUDIENCE_TOKEN_FILE} is unset")

    # --- V-BRK-007 / V-BRK-009: the transport layer, with a valid token in hand -------------------
    # Each of these carries the GOOD token. That is what makes them V-BRK-009's second arm rather
    # than a restatement of the first: the token layer is satisfied and the request still must not
    # get through.
    raw_get("no-client-cert", nonce_url, ctx=context_for(None, ca_dir=own_tls), token=own_token)

    plaintext_url = cfg.endpoint.replace("https://", "http://", 1) + broker_client.NONCE_PATH
    raw_get("plaintext", plaintext_url, ctx=None, token=own_token)

    if untrusted_tls:
        raw_get("untrusted-client-cert", nonce_url, ctx=context_for(untrusted_tls, ca_dir=own_tls), token=own_token)
    else:
        emit("untrusted-client-cert", outcome="probe-error", detail=f"{ENV_UNTRUSTED_TLS_DIR} is unset")

    # --- the two layers must AGREE, and the caller must be this broker's own -----------------------
    if foreign_tls:
        foreign_ctx = context_for(foreign_tls, ca_dir=own_tls)
        # Certificate from workload X, token from workload Y — both individually valid, both signed
        # by the trusted mesh CA. Without the binding this is an authorized write attributed to the
        # wrong agent.
        raw_get("peer-mismatch", nonce_url, ctx=foreign_ctx, token=own_token)
        # V-BRK-010: a consistent FOREIGN reader — certificate and token agree with each other and
        # name another agent. The `<agent>-broker-ingress` NetworkPolicy admitted this connection,
        # which is the point: the policy does not authenticate anything, the broker does.
        if foreign_token:
            raw_get("foreign-caller", nonce_url, ctx=foreign_ctx, token=foreign_token)
        else:
            emit("foreign-caller", outcome="probe-error", detail=f"{ENV_FOREIGN_TOKEN} is unset")
    else:
        emit("peer-mismatch", outcome="probe-error", detail=f"{ENV_FOREIGN_TLS_DIR} is unset")
        emit("foreign-caller", outcome="probe-error", detail=f"{ENV_FOREIGN_TLS_DIR} is unset")

    return 0


if __name__ == "__main__":
    sys.exit(main())
