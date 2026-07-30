#!/usr/bin/env python3
"""broker_execute_probe.py — submit ONE well-formed envelope at a deployed broker and report where
the broker says it put the answer, so a suite outside the cluster can read the journal and judge it.

WHY THIS IS A SECOND PROBE AND NOT AN ELEVENTH SCENARIO IN THE FIRST
    `broker_probe.py` presents ten credentials and asks what is refused. Its one positive,
    `envelope-accepted`, exists so the nine refusals are not vacuous, and it says in its own words
    that it does not care whether the pipeline accepted the envelope: "a policy refusal and an
    acceptance are both proof that the transport carried a real envelope".

    This probe cares about precisely the thing that one disclaims. Accept (a) is "an envelope flows
    end-to-end in shadow mode and produces a well-formed ActionRecord with a valid undo plan", and
    every clause after the comma is about what the broker DID with the envelope, not about whether
    it let it through. Folding this in would mean one fixture whose ten scenarios want a 401 and
    whose eleventh wants a 202, reporting through one exit code.

WHAT IS SHIPPED CODE HERE
    All of it. `broker_client` and `action_envelope` are mounted byte-for-byte from the working
    tree, and the submission goes through them and nothing else. There is no `urllib` in this file —
    unlike the negative arms of `broker_probe.py`, which must construct requests the shipped client
    offers no way to construct. Every request made here is one a real agent could make.

WHAT THIS PROBE DOES NOT DECIDE
    Whether the record is correct. It cannot: the ActionRecord is a cluster object and this pod
    holds the agent's reader identity, whose grant does not reach the journal. The probe reports the
    `actionId` and `namespace` the broker returned and stops. `broker-execute-l2.sh` does the
    reading, against the API server, which is also the only arrangement in which the read is
    P6-correct — the record is fetched from the server that stores it, never inferred from the reply
    body that claims it.

OUTPUT CONTRACT, read by `dev/verify/broker-execute-l2.sh`
    One JSON object per line on stdout, and nothing else on stdout:
        {"scenario":  str,
         "outcome":   "http" | "transport-error" | "probe-error" | "note",
         "status":    int | null,
         "reason":    str,            # the broker's machine-readable refusal reason, when refused
         "decision":  str,            # the broker's decision word, when it made one
         "phase":     str,            # the phase the broker says the record is in
         "actionId":  str,            # what the suite reads the journal by
         "namespace": str,            # where the broker says it wrote it
         "detail":    str}
    The first three words are `broker_probe.py`'s, spelled identically on purpose: two probes with
    two spellings of "the connection never became a TLS session" are two probes a suite has to
    special-case. `note` is the one word this probe adds, and it exists for a reason the other probe
    does not have — this suite must go looking for an object AFTER the run, and the probe is where
    that object's identity is decided. Emitting it as a line means the suite reads the name it was
    actually aimed at rather than keeping a second copy of it that can drift.
"""

from __future__ import annotations

import json
import os
import sys

import action_envelope
import broker_client
from broker_client import BrokerConfig

# The object the operation aims at. Absent before the run, and it is the whole point that it is
# still absent after: see `target_configmap()`.
TARGET_NAME = "broker-execute-l2-shadow-target"


def emit(
    scenario: str,
    *,
    outcome: str,
    status: int | None = None,
    reason: str = "",
    decision: str = "",
    phase: str = "",
    action_id: str = "",
    namespace: str = "",
    detail: str = "",
) -> None:
    """One transcript line."""
    print(
        json.dumps(
            {
                "scenario": scenario,
                "outcome": outcome,
                "status": status,
                "reason": reason,
                "decision": decision,
                "phase": phase,
                "actionId": action_id,
                "namespace": namespace,
                "detail": detail,
            }
        ),
        flush=True,
    )


def emit_reply(scenario: str, reply: dict) -> None:
    """A broker reply, in the shape above.

    `actionId` and `namespace` are carried even when the reply is a refusal, and that is not
    padding: 06 §4.1 requires most refusals to be journaled too, so a refusal naming no record is
    itself something the suite can report. `_status` is the key `post_envelope` records the HTTP
    code under.
    """
    emit(
        scenario,
        outcome="http",
        status=reply.get("_status"),
        reason=str(reply.get("reason", "")),
        decision=str(reply.get("decision", "")),
        phase=str(reply.get("phase", "")),
        action_id=str(reply.get("actionId", "")),
        namespace=str(reply.get("namespace", "")),
        detail=str(reply.get("message", ""))[:400],
    )


def target_configmap(ns: str) -> list[dict[str, object]]:
    """One well-formed operation, on an object chosen so that every part of the answer is legible.

    Three choices, each load-bearing:

    · A ConfigMap `apply`, because it is the simplest operation in 06 §4.1 that still produces a
      non-trivial undo plan. The 06 §4.3.1 inverse of "apply over an absent object" is `delete`,
      which is a step — so `strategy: none` would be a wrong answer here rather than a permitted
      one, and V-REV-002/003 have something to bite on. An operation whose correct undo plan is
      empty cannot distinguish a working planner from a silent one.

    · The TENANT namespace rather than `kubeagents-system`, for two reasons. It is what the
      P9-T9b-5a overlay grants write authority over; and `execute/client.go` issues its calls with
      `client.DryRunAll`, which the API server AUTHORIZES before it dry-runs. Aimed anywhere the
      broker's identity cannot write, this comes back 403 from inside the executor — a failure that
      looks nothing like the thing it is.

    · Absent, and created by nobody, because the claim under test is that shadow mode does not
      mutate. Were the object to exist beforehand, the suite could only check it was unchanged,
      which a broker that never ran would also satisfy. Absent, "did anything get written?" is a
      question with a one-word answer: `kubectl get` returns NotFound.

    The shape is 06 §4.1's closed schema, which `DecodeEnvelope` reads with
    `DisallowUnknownFields`: `target` is group/version/kind/namespace/name — no `apiVersion` — and
    an `apply` carries `desiredState`.
    """
    return [
        {
            "op": "apply",
            "target": {"version": "v1", "kind": "ConfigMap", "namespace": ns, "name": TARGET_NAME},
            "desiredState": {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": TARGET_NAME, "namespace": ns},
                "data": {"probe": "broker-execute-l2", "written": "this must never exist"},
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

    tenant_ns = os.environ.get("PROBE_TENANT_NAMESPACE", "").strip()
    if not tenant_ns:
        # Not a default. `PROBE_NAMESPACE` in the other probe defaults to `kubeagents-system`
        # because the object there is never reached; here the namespace decides whether the
        # executor is authorized at all, so guessing it would turn a harness misconfiguration into
        # a broker defect.
        emit("config", outcome="probe-error", detail="PROBE_TENANT_NAMESPACE is unset; no tenant to aim at")
        return 1

    # --- the door, first --------------------------------------------------------------------------
    # `broker_probe.py`'s baseline argument, run in the other direction. There, an open door makes
    # the refusals meaningful; here, a 401 on the submission would be filed as "Accept (a) failed"
    # when what actually happened is that this fixture's credentials are wrong. The suite reads a
    # failure of this line as could-not-run, not as a red.
    try:
        client = broker_client.BrokerClient(cfg)
        nonce = client.fetch_nonce()
        emit("nonce-accepted", outcome="http", status=200, detail=f"nonce issued, {len(nonce)} chars")
    except Exception as exc:  # noqa: BLE001
        emit("nonce-accepted", outcome="probe-error", detail=f"{type(exc).__name__}: {exc}"[:400])
        return 1

    emit(
        "target",
        outcome="note",
        namespace=tenant_ns,
        detail=TARGET_NAME,
        reason="the object the operation aims at; it must not exist after a shadow run",
    )

    # --- the submission ---------------------------------------------------------------------------
    # `dry_run=True` is asked for explicitly rather than left to the broker's shadow-mode forcing.
    # They are different claims and this is the first one: an agent that ASKS for a shadow gets one.
    # That the broker would have forced it anyway, from the Agent CR's
    # `spec.operations.dryRunOnly`, is a property the suite reads off the CR separately — asserting
    # both through one submission would let either alone carry the row.
    try:
        envelope = action_envelope.build_envelope(
            agent_identity=cfg.identity,
            intent="broker-execute-l2: one envelope, end to end, in shadow mode",
            operations=target_configmap(tenant_ns),
            requester={"kind": "system", "id": "broker-execute-l2"},
            # `cron` because 06 §4.1's trigger sources are a CLOSED set of seven and
            # `action_envelope.py` mirrors the operation verbs and the patch media types from the Go
            # side but not those — so the client-side check passes anything non-empty and a wrong
            # word here returns a 400 `invalid-envelope` that reads like a broker defect. The other
            # probe learned this from a refusal; the finding is filed, and this is not the unit that
            # closes it.
            trigger={"source": "cron"},
            # The shipped trace builder, with SPAN_ID deliberately unset in the driver pod: when it
            # IS set, `session_trace()` emits `parentSpanId`, the broker's `Trace` struct has no such
            # field, and `DisallowUnknownFields` turns the envelope into a 400 `unknown-field`. A
            # real defect in shipped agent code, already filed. Setting SPAN_ID here would measure
            # it, which belongs to the unit that repairs it.
            trace=broker_client.session_trace(),
            nonce=nonce,
            rationale=(
                "V-BRK-006/018/019 and V-REV-002/003: submit, classify, journal, shadow-execute, "
                "and leave behind a record whose undo plan is worth the name."
            ),
            dry_run=True,
        )
    except Exception as exc:  # noqa: BLE001
        emit("envelope-built", outcome="probe-error", detail=f"{type(exc).__name__}: {exc}"[:400])
        return 1

    try:
        emit_reply("shadow-submit", client.post_envelope(envelope))
    except Exception as exc:  # noqa: BLE001
        emit("shadow-submit", outcome="transport-error", detail=f"{type(exc).__name__}: {exc}"[:400])
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
