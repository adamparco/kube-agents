#!/usr/bin/env python3
"""broker_gate_probe.py — submit two envelopes that differ in exactly one thing: whether the broker
can generate an undo plan for them. V-REV-003, at L2, against the deployed binary.

WHAT V-REV-003 SAYS, AND WHY IT NEEDS TWO SUBMISSIONS
    "An action with no generatable undo plan is **reclassified gated** and never auto-executes."

    One submission cannot carry that. A broker that gates EVERYTHING satisfies the sentence
    perfectly and is useless — and it is not a hypothetical failure, because three separate things
    in this pipeline downgrade a plan to `none` on any error they meet: `checkRecreatable` when the
    reference index cannot answer, `undo.Validate` when no dry-run client is wired, and
    `generateOne` when a snapshot is missing. Each of those fails closed, correctly, and each of
    them would make a one-submission suite green while proving nothing about reclassification.

    So there are two, and the ONE variable between them is whether 06 §4.3.1's strategy table can
    produce a step:

      · `no-undo-plan` — a `patch` of a Deployment that DOES NOT EXIST. `StrategyFor("patch", …)`
        is `restore` for either existence; step 3 captures a NotFound as `Existed=false` with no
        pre-state; and `generateOne`'s restore arm refuses on exactly that — "no pre-state snapshot
        was supplied for an operation over an object that already existed". Strategy drops to
        `none`, `Undoable()` is false, and the action must come back GATED.

      · `undo-plan-control` — an `apply` of an absent ConfigMap. The inverse is `delete`, which is a
        step; the plan validates, and the action must come back ACCEPTED and shadow-execute.

    Same broker, same credential, same namespace, same submission path, seconds apart. If the
    control is accepted and the fault is gated, the reclassification is the difference between them
    and cannot be anything else.

WHY THE CONTROL IS A DIFFERENT VERB AND KIND, WHICH IS A REAL COST
    The tightest control would be the same `patch` over a Deployment that DOES exist — one variable,
    literally. It was rejected. That plan's step is a server-side apply of the whole captured
    pre-state under the agent's own field manager, and the object it would apply over was created by
    this suite's `kubectl apply`, owned by a different manager. A field-ownership conflict there
    downgrades the plan and gates the control — for a reason that is an artifact of how the fixture
    was created, not a property of the broker. A control that can gate for a reason unrelated to the
    experiment is worse than one that differs in verb.

    The `apply`-a-ConfigMap operation is used instead because it is the one operation ALREADY PROVEN
    to reach the accepting path on this cluster: it is `broker_execute_probe.target_configmap`
    character for character, and V-REV-001 is scored `pass` at L2 on the strength of the record it
    produced. Borrowing a known-good control is the point of it being a control.

WHY `dry_run=False` HERE, WHEN EVERY OTHER PROBE IN THIS FAMILY SENDS TRUE
    Because with `dry_run=True` the reclassification under test does not happen. 06 §4.2 step 6's
    rule is `UndoPlanGateApplies(dryRun, present) = !dryRun && !present` — a dry run SUPPRESSES the
    no-undo-plan gate, deliberately, and `pipeline.go`'s step 4 feeds it the envelope's own value
    rather than the effective one for that exact reason. A `dry_run=True` submission would still
    come back gated, via the brake's row 5 at step 5, and the suite would score V-REV-003 on a rule
    that is not the one 03 §4.1 names.

    Sending `dry_run=False` also makes the second clause — "never auto-executes" — a real request
    rather than a rhetorical one: this caller is asking the broker to execute for real, and the only
    thing between the ask and a write is the gate.

    WHAT KEEPS THAT SAFE IS NOT THIS FILE. `dev/verify/broker-gate-l2.sh` sets
    `spec.operations.dryRunOnly: true` on the Agent CR before either submission and asserts the
    broker observed it. `pipeline.Submit` computes `mayExecute = !env.DryRun && !shadowed(view)`, so
    shadow mode alone forces every execution to a server-side dry run and the worst case of a broker
    that failed to gate is a shadow write. Phase 9's whole shape — "exercise it end-to-end with no
    write authority anywhere; the broker runs every action in dry-run" (07 §2) — is preserved, and
    the suite reads the CR back rather than trusting that it patched.

    The consequence, stated because it is a limitation and not a footnote: the target object's
    continued absence is over-determined. Shadow mode alone would produce it. So the "never
    auto-executes" clause is carried by the REPLY and the RECORD — decision `gated`, phase
    `PendingApproval`, no `status.applied` — and the suite says so where it asserts it.

WHAT IS SHIPPED CODE HERE
    All of it, as in the other three probes. `broker_client` and `action_envelope` are mounted from
    the working tree; there is no `urllib` in this file. A gate produced by a hand-built request
    would be a gate on something no agent can send.

OUTPUT CONTRACT, read by `dev/verify/broker-gate-l2.sh`
    One JSON object per line on stdout, and nothing else on stdout. `scenario` is a LINE TAG, not
    the run's scenario name — `scenario-note`, `nonce-accepted`, `target`, `submit`, `config` —
    because the suite selects lines by it and two lines sharing a tag would make the selection
    depend on which came first. The key spellings and the four outcome words are
    `broker_refuse_probe.py`'s, character for character:
        {"scenario":  str,
         "outcome":   "http" | "transport-error" | "probe-error" | "note",
         "status":    int | null,
         "reason":    str,
         "decision":  str,            # "gated" on the fault, "accepted" on the control
         "phase":     str,            # "PendingApproval" on the fault
         "actionId":  str,            # present on both — a gated action IS journaled
         "traceId":   str,            # the handle the suite reads the journal by
         "namespace": str,
         "retryAfterSeconds": int | null,
         "detail":    str}
"""

from __future__ import annotations

import json
import os
import sys

import action_envelope
import broker_client
from broker_client import BrokerConfig

# The `no-undo-plan` target: a Deployment that DOES NOT EXIST and that nothing in the suite creates.
#
# Absence is the whole mechanism. `execute.capture` narrows `apierrors.IsNotFound` — and only that
# — to `Existed: false` with a nil pre-state, and `generateOne`'s `UndoRestore` arm refuses on a nil
# pre-state. Create this object and the plan becomes generatable and the scenario evaporates, which
# is why the suite asserts it is absent BEFORE submitting rather than assuming.
#
# A Deployment rather than a ConfigMap for one reason: `classify`'s `statefulKinds` contains
# ConfigMap, so any ConfigMap operation that could produce this refusal would ALSO be gated by
# `RuleDestructiveStatefulDelete` or its neighbours, and a gate with two independent causes cannot
# attribute itself to either. `apps/Deployment` is in no floor list, so `no-undo-plan` is the only
# reason available. The actor's write overlay grants `patch deployments` in the tenant namespace,
# which is what makes the operation one this caller is entitled to ask for at all.
UNPLANNABLE_NAME = "broker-gate-l2-absent-deployment"

# The `undo-plan-control` target: an absent ConfigMap, `broker_execute_probe.TARGET_NAME`'s twin.
# Absent for that probe's reason — "did anything get written?" has a one-word answer when the object
# started out missing.
CONTROL_NAME = "broker-gate-l2-control-target"

SCENARIOS = ("no-undo-plan", "undo-plan-control")


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
    first hit, so two lines sharing a tag make one of them unreadable.
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

    `actionId` is carried and is expected to be PRESENT on both scenarios, which is the difference
    from the refuse probe: a gated action is parked, not refused, and `stepGate` creates its record
    before answering 202. `_status` is the key `post_envelope` records the HTTP code under.
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


def unplannable_operation(ns: str) -> dict[str, object]:
    """The operation whose undo plan cannot be generated.

    A merge patch adding one annotation. The body is deliberately the most boring change available:
    `classify`'s direction analysis reads patch bodies for security loosening, and a patch that
    touched `securityContext`, `serviceAccountName` or a pod-security label would be gated by
    `RuleSecurityLoosen` as well — a second independent cause for the outcome under test, which
    would make the observation unattributable. An annotation is on no `looseningFieldPaths` path.

    The shape is 06 §4.1's closed schema, which `DecodeEnvelope` reads with `DisallowUnknownFields`:
    `target` is group/version/kind/namespace/name — no `apiVersion` — and a `patch` carries a
    `patch` object with a media `type` and a `body`, and nothing else. `application/merge-patch+json`
    is one of the three types both `action_envelope.VALID_PATCH_TYPES` and the Go side's
    `validPatchTypes` accept, and its body is an object rather than an array — the Go validator
    cross-checks that pairing and rejects a mismatch.
    """
    return {
        "op": "patch",
        "target": {
            "group": "apps",
            "version": "v1",
            "kind": "Deployment",
            "namespace": ns,
            "name": UNPLANNABLE_NAME,
        },
        "patch": {
            "type": "application/merge-patch+json",
            "body": {"metadata": {"annotations": {"kube-agents.io/probe": "broker-gate-l2"}}},
        },
    }


def control_operation(ns: str) -> dict[str, object]:
    """The operation whose undo plan CAN be generated, and which must therefore be accepted.

    `broker_execute_probe.target_configmap`'s single operation, unchanged. Its 06 §4.3.1 inverse is
    `delete`, and `rollback.PlanDryRunner` treats the NotFound that a delete-step dry run returns
    before the action has run as "would apply" — which is why this plan validates and the fault's
    does not, and why V-REV-001 could be scored `pass` at L2 off the record it leaves.
    """
    return {
        "op": "apply",
        "target": {"version": "v1", "kind": "ConfigMap", "namespace": ns, "name": CONTROL_NAME},
        "desiredState": {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": CONTROL_NAME, "namespace": ns},
            "data": {"probe": "broker-gate-l2", "written": "this must never exist"},
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

    emit(
        "scenario-note",
        outcome="note",
        detail=scenario,
        reason="which of the two runs this transcript is",
    )

    cfg = BrokerConfig.from_env()
    try:
        cfg.require()
    except Exception as exc:  # noqa: BLE001
        emit("config", outcome="probe-error", detail=f"{type(exc).__name__}: {exc}")
        return 1

    tenant_ns = os.environ.get("PROBE_TENANT_NAMESPACE", "").strip()
    if not tenant_ns:
        # Not a default, for the other probes' reason: the namespace decides whether the executor is
        # authorized at all, so guessing it turns a harness misconfiguration into a broker defect.
        emit("config", outcome="probe-error", detail="PROBE_TENANT_NAMESPACE is unset; no tenant to aim at")
        return 1

    # --- the door, first --------------------------------------------------------------------------
    # The baseline every probe in this family opens with. It matters more in the fault run than
    # usual: without a proven-open door, a 202 that never arrives and a 401 at the front are the same
    # observation. A failure of this line is could-not-run, never a red.
    try:
        client = broker_client.BrokerClient(cfg)
        nonce = client.fetch_nonce()
        emit("nonce-accepted", outcome="http", status=200, detail=f"nonce issued, {len(nonce)} chars")
    except Exception as exc:  # noqa: BLE001
        emit("nonce-accepted", outcome="probe-error", detail=f"{type(exc).__name__}: {exc}"[:400])
        return 1

    if scenario == "no-undo-plan":
        operations = [unplannable_operation(tenant_ns)]
        target_name = UNPLANNABLE_NAME
        intent = "broker-gate-l2: patch an object that is not there, so no undo plan can be generated"
        rationale = (
            "V-REV-003: 06 §4.3.1's strategy table maps a patch to `restore`, and a restore needs a "
            "pre-state. There is none, because the object does not exist. The plan refuses, the "
            "class is raised to gated, and a human decides -- the action must not auto-execute."
        )
    else:
        operations = [control_operation(tenant_ns)]
        target_name = CONTROL_NAME
        intent = "broker-gate-l2: the control -- an operation whose undo plan generates and validates"
        rationale = (
            "V-REV-003's other side. The inverse of an apply over an absent object is a delete, "
            "which is a step, so this plan validates and the action is accepted. Without this run, "
            "a broker that gated every submission would satisfy V-REV-003 and be worthless."
        )

    # `session_trace()` is the shipped builder. The id it mints is how the suite finds the record --
    # for the fault run the reply DOES carry an actionId, but the suite matches on the trace id for
    # both runs so that the two lookups are the same lookup and neither can be right for a reason
    # the other is not.
    trace = broker_client.session_trace()

    try:
        envelope = action_envelope.build_envelope(
            agent_identity=cfg.identity,
            intent=intent,
            operations=operations,
            requester={"kind": "system", "id": "broker-gate-l2"},
            # `cron` for the other probes' reason: 06 §4.1's trigger sources are a closed set of
            # seven that `action_envelope.py` does not mirror, so a wrong word here returns a 400
            # `invalid-envelope` that reads like a broker defect. Filed; not this unit.
            trigger={"source": "cron"},
            trace=trace,
            nonce=nonce,
            rationale=rationale,
            # FALSE, AND IT IS THE POINT OF THIS PROBE. See the header: `dry_run=True` suppresses
            # 06 §4.2 step 6's no-undo-plan rule outright, so the true value would score V-REV-003
            # on the brake's row 5 instead of on the rule 03 §4.1 names. Shadow mode is imposed by
            # the suite through `spec.operations.dryRunOnly` on the Agent CR, which the pipeline
            # composes one-way and no caller can clear.
            dry_run=False,
        )
    except Exception as exc:  # noqa: BLE001
        # Tagged `submit`, not `config`: the suite reads the submission's outcome off the `submit`
        # line, and an envelope that could not be BUILT is a submission that did not happen. Under a
        # `config` tag it would leave `submit` absent, which reads as an empty field rather than as a
        # failure.
        emit("submit", outcome="probe-error", detail=f"{type(exc).__name__}: {exc}"[:400])
        return 1

    trace_id = str(envelope.get("trace", {}).get("traceId", ""))
    emit(
        "target",
        outcome="note",
        trace_id=trace_id,
        namespace=tenant_ns,
        detail=target_name,
        reason="the trace id the record must carry, and the object that must not exist after",
    )
    # THE ENVELOPE'S OWN `dryRun`, READ BACK OUT OF THE BUILT DOCUMENT, not restated from the
    # argument. `build_envelope` writes the key only when the flag is true -- `if dry_run:
    # envelope["dryRun"] = True` -- so a false request is expressed by OMISSION, and the Go side's
    # `Envelope.DryRun` zero value supplies the false. That is correct on both sides and it is also
    # exactly the kind of asymmetry a suite should not take on faith: the assertion this line feeds
    # is "the key is absent", and if some future builder starts emitting `dryRun: false` explicitly
    # the suite sees `false` here and is still right.
    emit(
        "dry-run-note",
        outcome="note",
        trace_id=trace_id,
        detail="absent" if "dryRun" not in envelope else str(envelope["dryRun"]),
        reason="the envelope's own dryRun field as built; `absent` and `False` both mean this caller asked to execute for real",
    )

    try:
        emit_reply("submit", client.post_envelope(envelope), trace_id)
    except Exception as exc:  # noqa: BLE001
        emit("submit", outcome="transport-error", trace_id=trace_id, detail=f"{type(exc).__name__}: {exc}"[:400])
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
