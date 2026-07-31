#!/usr/bin/env python3
"""broker_refuse_probe.py — submit an envelope the deployed broker is expected to REFUSE, and report
the refusal precisely enough that a suite outside the cluster can go and find its journal record.

WHY A THIRD PROBE
    `broker_probe.py` asks what the DOOR refuses: ten credentials, nine of which never become a
    session. `broker_execute_probe.py` asks what the PIPELINE does with a well-formed envelope it
    accepts. Neither can ask this one's question, which is what the pipeline does when it accepts an
    envelope and then, several steps in, discovers it cannot proceed:

      · `split-snapshot` — two targets, one of them unreadable. 06 §4.4 row 4 and V-BRK-018: the
        snapshot is all-or-nothing, so the second target's 403 must stop the FIRST one from being
        applied. The envelope is well-formed, the credential is real, the caller is in scope; the
        refusal comes from step 3 and from nowhere earlier.

      · `journal-gone` — one ordinary target, submitted while the actor's `actionrecords` grant is
        revoked out from under the running broker. 06 §4.4 row 3: the brake sees the journal is
        unreachable and refuses 503 before the pipeline reaches its write-ahead Create. This is the
        journal half of Accept (d), and it is the only scenario here that needs the suite to change
        the cluster between runs.

    Both are things the broker does correctly and quietly. A refusal leaves nothing behind on the
    target — which is exactly why it needs a probe that reports what it asked for: absence proves
    nothing unless something is known to have tried.

WHY THE TRACE ID IS THE OUTPUT THAT MATTERS
    A refusal reply is deliberately thin. `server.go`'s `write()` renders `reason`, `message`,
    `decision` and `retryAfterSeconds` — and no `actionId`, because the action id of a record the
    caller may not read is not the caller's business. So the suite cannot look the record up by the
    handle the accepting path gives it. What it CAN do is look it up by the correlation id the
    caller itself minted: `rejection.go`'s `traceFromBody` copies a well-formed `trace.traceId`
    onto the record precisely so a refusal can be tied back to the conversation that caused it.

    That is the whole reason this probe emits `traceId` on every line. It is the only handle onto
    the artifact, it is decided here, and a suite keeping its own second copy of it would be a copy
    that can drift.

WHAT IS SHIPPED CODE HERE
    All of it, as in the other two: `broker_client` and `action_envelope` are mounted from the
    working tree and every request is one a real agent could make. There is no `urllib` in this
    file. A refusal produced by a hand-built request would be a refusal of something no agent can
    send.

WHAT THIS PROBE DOES NOT DECIDE
    Whether the refusal was journaled, whether the record's phase is `Rejected`, whether the
    targets were left alone. All three are cluster reads and this pod holds the agent's reader
    identity, whose grant does not reach the journal. `dev/verify/broker-refuse-l2.sh` does the
    reading against the API server — the same P6 arrangement as the execute suite, for the same
    reason: the record is fetched from the server that stores it, never inferred from a reply body.

OUTPUT CONTRACT, read by `dev/verify/broker-refuse-l2.sh`
    One JSON object per line on stdout, and nothing else on stdout. `scenario` is a LINE TAG, not
    the run's scenario name — `scenario-note`, `nonce-accepted`, `target`, `unreadable`, `submit`,
    `config` — because the suite selects lines by it and two lines sharing a tag would make the
    selection depend on which came first. Which scenario ran is on the `scenario-note` line, and
    is not needed for selection because the suite invokes the two runs separately and reads each
    run's transcript on its own:
        {"scenario":  str,
         "outcome":   "http" | "transport-error" | "probe-error" | "note",
         "status":    int | null,
         "reason":    str,            # the broker's machine-readable refusal reason
         "decision":  str,            # "rejected" on every refusal the broker renders
         "phase":     str,            # empty on a refusal; carried so the shape matches
         "actionId":  str,            # empty on a refusal, and asserted to be
         "traceId":   str,            # what the suite reads the journal by
         "namespace": str,
         "retryAfterSeconds": int | null,
         "detail":    str}
    The first four outcome words and the field spellings are `broker_execute_probe.py`'s, character
    for character. Two probes with two spellings of "the connection never became a TLS session" are
    two probes every suite has to special-case.
"""

from __future__ import annotations

import json
import os
import sys

import action_envelope
import broker_client
from broker_client import BrokerConfig

# The object the readable half of `split-snapshot` aims at, and the object `journal-gone` aims at.
# Absent before the run, and it is the whole point that it is still absent after: in the first
# scenario because the sibling target's 403 must take it down with it, in the second because the
# brake refused before the pipeline ever reached an executor.
TARGET_NAME = "broker-refuse-l2-shadow-target"

# The unreadable half of `split-snapshot`. A Deployment, in a namespace the read overlay does not
# cover: `actor-grant-platform.yaml.template`'s ClusterRole names no `apps` group at all, so this
# is Forbidden by the SHIPPED grant rather than by anything the suite arranged. The name is a
# sentinel — the object does not exist and must not be created, because a 403 and a 404 are
# different answers and only the 403 exercises the row under test.
UNREADABLE_NAME = "broker-refuse-l2-unreadable-target"

SCENARIOS = ("split-snapshot", "journal-gone")


def emit(
    tag: str,
    *,
    outcome: str,
    status: int | None = None,
    reason: str = "",
    decision: str = "",
    phase: str = "",
    action_id: str = "",
    trace_id: str = "",
    namespace: str = "",
    retry_after: int | None = None,
    detail: str = "",
) -> None:
    """One transcript line.

    `tag` is the LINE TAG, not the scenario name: the suite's `field()` matches on it and takes the
    first hit, so two lines sharing a tag make one of them unreadable. The JSON key is still called
    `scenario` because that is the column `field()` indexes on.
    """
    print(
        json.dumps(
            {
                "scenario": tag,
                "outcome": outcome,
                "status": status,
                "reason": reason,
                "decision": decision,
                "phase": phase,
                "actionId": action_id,
                "traceId": trace_id,
                "namespace": namespace,
                "retryAfterSeconds": retry_after,
                "detail": detail,
            }
        ),
        flush=True,
    )


def _int_or_none(v: object) -> int | None:
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def emit_reply(tag: str, reply: dict, trace_id: str) -> None:
    """A broker reply, in the shape above.

    `actionId` is carried even though a refusal is expected to omit it — the suite asserts the
    omission, and a field this probe dropped would make that assertion unfalsifiable. `_status` is
    the key `post_envelope` records the HTTP code under.
    """
    emit(
        tag,
        outcome="http",
        status=reply.get("_status"),
        reason=str(reply.get("reason", "")),
        decision=str(reply.get("decision", "")),
        phase=str(reply.get("phase", "")),
        action_id=str(reply.get("actionId", "")),
        trace_id=trace_id,
        namespace=str(reply.get("namespace", "")),
        retry_after=_int_or_none(reply.get("retryAfterSeconds")),
        detail=str(reply.get("message", ""))[:400],
    )


def readable_target(ns: str) -> dict[str, object]:
    """The half of the envelope that WOULD have worked.

    Same shape and same reasoning as `broker_execute_probe.target_configmap`: a ConfigMap `apply`
    over an absent object, in the tenant namespace the P9-T9b-5a overlay grants read and write
    authority over. Its job here is different, though. In the execute suite it is the operation
    under test; here it is the control — the target that is demonstrably reachable, so that when
    nothing is applied the reason is the sibling target and not this one.
    """
    return {
        "op": "apply",
        "target": {"version": "v1", "kind": "ConfigMap", "namespace": ns, "name": TARGET_NAME},
        "desiredState": {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": TARGET_NAME, "namespace": ns},
            "data": {"probe": "broker-refuse-l2", "written": "this must never exist"},
        },
    }


def unreadable_target(ns: str) -> dict[str, object]:
    """The half that stops the other one.

    A `delete`, which carries no `desiredState` and therefore cannot be waved through as a
    malformed operation — if this envelope is refused, it is refused for the reason under test and
    not at validation. `execute.CaptureAll` reads pre-state for every target before any of them is
    applied, and reading a Deployment in a namespace outside the overlay is Forbidden.
    """
    return {
        "op": "delete",
        "target": {
            "group": "apps",
            "version": "v1",
            "kind": "Deployment",
            "namespace": ns,
            "name": UNREADABLE_NAME,
        },
    }


def main() -> int:
    scenario = os.environ.get("PROBE_SCENARIO", "").strip()
    if scenario not in SCENARIOS:
        emit(
            "config",
            outcome="probe-error",
            detail=f"PROBE_SCENARIO must be one of {', '.join(SCENARIOS)}; got {scenario!r}",
        )
        return 1

    emit("scenario-note", outcome="note", detail=scenario, reason="which of the two runs this transcript is")

    cfg = BrokerConfig.from_env()
    try:
        cfg.require()
    except Exception as exc:  # noqa: BLE001
        emit("config", outcome="probe-error", detail=f"{type(exc).__name__}: {exc}")
        return 1

    tenant_ns = os.environ.get("PROBE_TENANT_NAMESPACE", "").strip()
    if not tenant_ns:
        # Not a default, for `broker_execute_probe.py`'s reason: the namespace decides whether the
        # executor is authorized at all, so guessing it turns a harness misconfiguration into a
        # broker defect.
        emit("config", outcome="probe-error", detail="PROBE_TENANT_NAMESPACE is unset; no tenant to aim at")
        return 1

    outside_ns = os.environ.get("PROBE_OUTSIDE_NAMESPACE", "").strip()
    if scenario == "split-snapshot" and not outside_ns:
        # Same argument, one step stronger. Aiming the unreadable half at the TENANT namespace
        # would produce a 200 and a green suite that measured nothing; aiming it at a namespace
        # this probe invented would produce a 403 the suite cannot attribute. The caller names it.
        emit("config", outcome="probe-error", detail="PROBE_OUTSIDE_NAMESPACE is unset; nothing to be refused by")
        return 1

    # --- the door, first --------------------------------------------------------------------------
    # The baseline every probe in this family opens with, and here it carries more weight than
    # usual: this suite EXPECTS a refusal, so without a proven-open door a 401 at the submission and
    # a 403 from step 3 are the same observation. A failure of this line is could-not-run, never a
    # red.
    try:
        client = broker_client.BrokerClient(cfg)
        nonce = client.fetch_nonce()
        emit("nonce-accepted", outcome="http", status=200, detail=f"nonce issued, {len(nonce)} chars")
    except Exception as exc:  # noqa: BLE001
        emit("nonce-accepted", outcome="probe-error", detail=f"{type(exc).__name__}: {exc}"[:400])
        return 1

    if scenario == "split-snapshot":
        operations = [readable_target(tenant_ns), unreadable_target(outside_ns)]
        intent = "broker-refuse-l2: two targets, one unreadable; neither may be applied"
        rationale = (
            "V-BRK-018: `execute.CaptureAll` is all-or-nothing, so a 403 on the second target's "
            "pre-state must stop the first target being applied — including in shadow mode, where "
            "'applied' means a server-side dry run the API server authorizes for real."
        )
    else:
        operations = [readable_target(tenant_ns)]
        intent = "broker-refuse-l2: one ordinary target, submitted with the journal unreachable"
        rationale = (
            "06 §4.4 row 3 and Accept (d): nothing executes unjournaled. The brake probes the "
            "ActionRecord store at step 5 and refuses 503 with an auto-pause, which is four steps "
            "before the write-ahead Create would have been attempted."
        )

    # `session_trace()` is the shipped builder and the id it mints is the ONLY handle the suite will
    # have on the record — see the header. Built once and read back out of the envelope rather than
    # regenerated, so the id reported and the id sent cannot differ.
    trace = broker_client.session_trace()

    try:
        envelope = action_envelope.build_envelope(
            agent_identity=cfg.identity,
            intent=intent,
            operations=operations,
            requester={"kind": "system", "id": "broker-refuse-l2"},
            # `cron` for `broker_execute_probe.py`'s reason: 06 §4.1's trigger sources are a closed
            # set of seven that `action_envelope.py` does not mirror, so a wrong word here returns a
            # 400 `invalid-envelope` that reads like a broker defect. Filed; not this unit.
            trigger={"source": "cron"},
            trace=trace,
            nonce=nonce,
            rationale=rationale,
            # Asked for explicitly, as in the execute probe. It also matters to the claim: a refusal
            # that only happens because the caller asked for a real write would prove nothing about
            # shadow mode, which is the mode everything in Phase 9 runs in.
            dry_run=True,
        )
    except Exception as exc:  # noqa: BLE001
        # Tagged `submit` and not `config`: the suite reads the outcome of the submission off the
        # `submit` line, and an envelope that could not be BUILT is a submission that did not
        # happen. Under a `config` tag it would leave `submit` absent, which reads as an empty
        # field rather than as a failure.
        emit("submit", outcome="probe-error", detail=f"{type(exc).__name__}: {exc}"[:400])
        return 1

    trace_id = str(envelope.get("trace", {}).get("traceId", ""))
    emit(
        "target",
        outcome="note",
        trace_id=trace_id,
        namespace=tenant_ns,
        detail=TARGET_NAME,
        reason="the trace id the refusal record must carry, and the object that must not exist after",
    )
    if scenario == "split-snapshot":
        emit(
            "unreadable",
            outcome="note",
            trace_id=trace_id,
            namespace=outside_ns,
            detail=UNREADABLE_NAME,
            reason="the unreadable sibling; it must not exist before the run either",
        )

    try:
        emit_reply("submit", client.post_envelope(envelope), trace_id)
    except Exception as exc:  # noqa: BLE001
        emit("submit", outcome="transport-error", trace_id=trace_id, detail=f"{type(exc).__name__}: {exc}"[:400])
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
