#!/usr/bin/env python3
"""undo_coverage_probe.py — submit the whole soak population at a deployed broker, one envelope per
corpus case, and report where the broker says it put each answer.

WHY A THIRD PROBE
    `broker_probe.py` presents ten credentials and asks what is refused. `broker_execute_probe.py`
    submits ONE envelope so that `broker-execute-l2.sh` has an ActionRecord to read. Neither can
    carry V-REV-001, because V-REV-001 says **100%** — it is a population claim, and n=1 is not a
    population. `broker-execute-l2.sh` says so in its own words and names this unit as where the
    denominator comes from.

    Folding the soak into `broker_execute_probe.py` would mean one fixture whose single scenario
    wants a specific undo strategy for a specific absent ConfigMap, and whose thirty-seven others
    want nothing in particular except to have been classified and journaled. Those are two probes.

WHERE THE POPULATION COMES FROM, AND WHY IT IS NOT IN THIS FILE
    `dev/verify/fixtures/soak_corpus.py` derives it from `verification/fixtures/classifier-corpus.yaml`
    and the actor's tenant write grant — the same corpus the classifier suites score, filtered to
    the cases this identity is actually authorized to attempt. The suite runs that deriver, base64s
    the table, and hands it over as `PROBE_CORPUS_B64`. This probe never decides which cases exist.

    Base64 because `broker_driver_run` renders extra env into an UNQUOTED heredoc and refuses any
    value carrying a quote, dollar, backtick or backslash, and reads the list line by line — so a
    multi-line TSV cannot pass through it in any spelling. Base64's alphabet is entirely inside what
    the driver permits, and the encoding is one line.

WHAT THE CORPUS SUPPLIES AND WHAT THIS FILE SYNTHESIZES
    The corpus supplies the operation's SHAPE and TARGET: verb, group, kind, and the object name the
    suite seeded. It does NOT supply payloads — only three of the thirty-seven cases carry a
    `touchedPaths`, and one of those names `/spec/selector`, which is immutable and which the API
    server would refuse even in a dry run. So the payload is synthesized here, from a deterministic
    rotation keyed by row index, chosen so that every one of them has an inverse:

        patch  → a merge-patch that adds an annotation, adds a label, or moves
                 `progressDeadlineSeconds`. 06 §4.3.1's inverse of `patch` is `restore`, which needs
                 a pre-state snapshot and produces steps.
        apply  → a full object over an ABSENT name. §4.3.1's inverse is `delete`, a step.
        create → the same, for the same reason.
        delete → the object the suite seeded. §4.3.1's inverse is `recreate`, and a Deployment is
                 recreatable (`undo/strategy.go`'s `nonRecreatableKinds` does not list it).
        scale  → to 1 from the seeded 0. §4.3.1's inverse is `restore`.

    Not one of them has `none` as a correct answer. That is the whole design constraint: an
    operation whose correct undo plan is empty cannot distinguish a working planner from a silent
    one, and a population of them would score V-REV-001 green while proving nothing.

WHAT THIS PROBE DOES NOT DECIDE
    Whether any record is correct, or even whether one exists. It holds the agent's READER identity,
    whose grant does not reach the journal. It reports the `actionId` and namespace the broker
    returned for each case and stops; `dev/verify/undo-coverage-l2.sh` reads the API server.

    It also does not decide the CLASS. The corpus's `class` column is carried through to the output
    as `expectClass` purely so a human reading the transcript can see what the corpus expected, and
    the suite is under standing instruction never to assert the live class equals it: the soak reads
    back the class the broker CHOSE and partitions on that. A soak that filtered on the expected
    class would be scoring V-REV-001 over a population the classifier never agreed to.

OUTPUT CONTRACT, read by `dev/verify/undo-coverage-l2.sh`
    One JSON object per line on stdout, and nothing else on stdout:
        {"scenario":    str,          # the corpus case id, or a fixture word like `config`
         "outcome":     "http" | "transport-error" | "probe-error" | "note",
         "status":      int | null,
         "reason":      str,          # the broker's machine-readable refusal reason, when refused
         "decision":    str,          # the broker's decision word, when it made one
         "phase":       str,
         "actionId":    str,          # what the suite reads the journal by
         "namespace":   str,          # where the broker says it wrote it
         "verb":        str,          # from the corpus row
         "kind":        str,          # from the corpus row
         "target":      str,          # the object name, so the suite reads the name it aimed at
         "expectClass": str,          # the corpus's expectation. NOT an assertion. See above.
         "detail":      str}
    The first four words are `broker_probe.py`'s, spelled identically on purpose.

WHY EVERY CASE GETS ITS OWN NONCE
    A nonce is single-use (`broker/antireplay.go`). Thirty-seven envelopes sharing one would produce
    one acceptance and thirty-six replay refusals, and the soak would score V-REV-001 over a
    population of one while reporting a 97% refusal rate as a broker defect.

WHY A FAILED CASE DOES NOT ABORT THE RUN
    A refusal is data. The classifier is entitled to gate a case the corpus called routine, and the
    broker is entitled to refuse an envelope this file built wrong. Both belong in the transcript
    where the suite can partition on them; aborting on the first non-2xx would turn one gated
    Deployment delete into "the soak could not run".
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import sys

import action_envelope
import broker_client
from broker_client import BrokerConfig

# The corpus table's columns, in `soak_corpus.COLUMNS` order. Named here rather than positionally
# indexed so a column inserted upstream fails on the header comparison below instead of silently
# shifting every field one to the left.
COLUMNS = (
    "id",
    "class",
    "verb",
    "group",
    "kind",
    "resource",
    "subresource",
    "rbacVerbs",
    "target",
    "seed",
    "srcNs",
)

# The apiVersion group's VERSION, which the corpus does not carry: it derives resources from kinds
# and both of its kinds are v1. Kept as a closed map rather than a default so that a corpus which
# grows a non-v1 kind fails loudly here — a probe that defaulted to "v1" would aim an `apps/v1`
# envelope at a `v2` API and file the 404 as a broker defect.
KIND_VERSION = {"ConfigMap": "v1", "Deployment": "v1"}

# The annotation and label the synthesized patches write. Distinct from the seeding label the suite
# applies, so a patch that somehow escaped the shadow is legible as such in the object.
PATCH_ANNOTATION = "kube-agents/soak-patch"
PATCH_LABEL = "kube-agents/soak-round"


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
    verb: str = "",
    kind: str = "",
    target: str = "",
    expect_class: str = "",
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
                "verb": verb,
                "kind": kind,
                "target": target,
                "expectClass": expect_class,
                "detail": detail,
            }
        ),
        flush=True,
    )


def parse_corpus(text: str) -> list[dict[str, str]]:
    """The `soak_corpus.py --table` TSV, as rows.

    The header line is COMPARED, not skipped. `soak_corpus.py` prints its columns as a `#`-prefixed
    header, and this file reads them by name from a fixed tuple; if the two ever disagree the fields
    silently shift and every envelope is built against the wrong column. Comparing costs one line
    and turns that into a refusal naming both spellings.
    """
    rows: list[dict[str, str]] = []
    header_seen = False
    for line in text.splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        if line.startswith("#"):
            if not header_seen and line.startswith("#id"):
                header_seen = True
                got = tuple(line.lstrip("#").split("\t"))
                if got != COLUMNS:
                    raise ValueError(f"corpus header is {got}, this probe reads {COLUMNS}")
            continue
        parts = line.split("\t")
        if len(parts) != len(COLUMNS):
            raise ValueError(f"corpus row has {len(parts)} fields, expected {len(COLUMNS)}: {line[:120]}")
        rows.append(dict(zip(COLUMNS, parts)))
    if not header_seen:
        raise ValueError("corpus carried no `#id...` header line; the column order is unverifiable")
    return rows


def target_ref(row: dict[str, str], ns: str) -> dict[str, str]:
    """06 §4.1's closed target shape: group/version/kind/namespace/name. No `apiVersion`.

    `DecodeEnvelope` reads the envelope with `DisallowUnknownFields`, so a target carrying the
    apiVersion spelling an agent would reach for first is a 400 `unknown-field`, not a lenient
    coercion.
    """
    kind = row["kind"]
    version = KIND_VERSION.get(kind)
    if version is None:
        raise ValueError(f"no apiVersion known for kind {kind!r}; add it to KIND_VERSION")
    ref = {"version": version, "kind": kind, "namespace": ns, "name": row["target"]}
    if row["group"]:
        ref["group"] = row["group"]
    return ref


def patch_body(row: dict[str, str], index: int) -> dict[str, object]:
    """A deterministic, undoable merge-patch, rotated by row index.

    Rotated rather than uniform so the population is not thirty-one copies of one operation: the
    classifier reads touched paths, and a soak in which every patch touches the same path measures
    one classifier decision thirty-one times. Deterministic rather than random because a corpus row
    must produce the same envelope on every run — a re-run that built a different operation would
    make a red run unreproducible, and `Math.random`-shaped test data is how a flake becomes
    permanent.

    Everything here is reversible by a `restore` from the pre-state snapshot, and nothing here
    touches an immutable field. `spec.selector` in particular is deliberately absent: the corpus
    case `gat-066` names it in `touchedPaths`, and the API server refuses to change it even under
    `--dry-run=server`, which would come back as an execution failure that looks like a broker
    defect.
    """
    generic: dict[str, object] = {"metadata": {"annotations": {PATCH_ANNOTATION: row["id"]}}}
    if row["kind"] != "Deployment":
        return generic
    return [
        generic,
        {"metadata": {"labels": {PATCH_LABEL: f"r{index % 7}"}}},
        {"spec": {"progressDeadlineSeconds": 600 + (index % 60)}},
    ][index % 3]


def desired_state(row: dict[str, str], ns: str) -> dict[str, object]:
    """The full object an `apply`/`create` aims at, for a name that is ABSENT before the run.

    Only ConfigMap is reachable here today — `soak_corpus.SEED_FOR_VERB` seeds `apply` and `create`
    absent, and the grant's only createable absent kind in the corpus is a ConfigMap — but the
    Deployment arm is written anyway rather than raising, because the alternative is a probe that
    dies on a corpus change instead of exercising it.
    """
    name, kind = row["target"], row["kind"]
    meta = {"name": name, "namespace": ns}
    if kind == "ConfigMap":
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": meta,
            "data": {"case": row["id"], "written": "this must never exist"},
        }
    if kind == "Deployment":
        labels = {"app": name}
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": meta,
            "spec": {
                "replicas": 0,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {"containers": [{"name": "pause", "image": "registry.k8s.io/pause:3.9"}]},
                },
            },
        }
    raise ValueError(f"no desiredState shape for kind {kind!r}")


def build_operation(row: dict[str, str], ns: str, index: int) -> dict[str, object]:
    """One operation, one object. Never two.

    One op per envelope on purpose. A multi-op envelope produces ONE ActionRecord covering several
    writes, and V-REV-001's denominator is records, not operations — so a soak that batched would
    shrink its own denominator by a factor it never reported. `soak_corpus.py` rejects multi-op
    corpus cases for the same reason, one layer up.
    """
    verb = row["verb"]
    op: dict[str, object] = {"op": verb, "target": target_ref(row, ns)}
    if verb in ("apply", "create"):
        op["desiredState"] = desired_state(row, ns)
    elif verb == "patch":
        op["patch"] = {"type": "application/merge-patch+json", "body": patch_body(row, index)}
    elif verb == "scale":
        # From the seeded 0 to 1. Non-trivial on purpose: `scale` to the replica count the object
        # already has is an operation whose restore is a no-op, and a no-op restore is exactly the
        # empty plan this population is built to exclude.
        op["scale"] = {"replicas": 1}
    elif verb == "delete":
        pass
    else:
        raise ValueError(f"no operation shape for verb {verb!r}")
    return op


def main() -> int:
    cfg = BrokerConfig.from_env()
    try:
        cfg.require()
    except Exception as exc:  # noqa: BLE001
        emit("config", outcome="probe-error", detail=f"{type(exc).__name__}: {exc}")
        return 1

    tenant_ns = os.environ.get("PROBE_TENANT_NAMESPACE", "").strip()
    if not tenant_ns:
        # Not a default, for `broker_execute_probe.py`'s reason: the namespace decides whether the
        # executor is authorized at all, so guessing it would turn a harness misconfiguration into
        # a broker defect.
        emit("config", outcome="probe-error", detail="PROBE_TENANT_NAMESPACE is unset; no tenant to aim at")
        return 1

    raw = os.environ.get("PROBE_CORPUS_B64", "").strip()
    if not raw:
        emit("config", outcome="probe-error", detail="PROBE_CORPUS_B64 is unset; there is no population to submit")
        return 1
    try:
        rows = parse_corpus(base64.b64decode(raw, validate=True).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        emit("config", outcome="probe-error", detail=f"PROBE_CORPUS_B64: {type(exc).__name__}: {exc}"[:400])
        return 1
    if not rows:
        emit("config", outcome="probe-error", detail="PROBE_CORPUS_B64 decoded to zero rows")
        return 1

    emit(
        "corpus",
        outcome="note",
        namespace=tenant_ns,
        detail=f"{len(rows)} case(s): " + ",".join(r["id"] for r in rows),
        reason="the population this run will submit, one envelope each",
    )

    # --- the door, first --------------------------------------------------------------------------
    # A 401 here is this fixture's credentials, not a V-REV-001 result. The suite reads a failure of
    # this line as could-not-run.
    try:
        client = broker_client.BrokerClient(cfg)
        probe_nonce = client.fetch_nonce()
        emit("nonce-accepted", outcome="http", status=200, detail=f"nonce issued, {len(probe_nonce)} chars")
    except Exception as exc:  # noqa: BLE001
        emit("nonce-accepted", outcome="probe-error", detail=f"{type(exc).__name__}: {exc}"[:400])
        return 1

    # --- the soak -----------------------------------------------------------------------------------
    for index, row in enumerate(rows):
        cid = row["id"]
        common = {
            "verb": row["verb"],
            "kind": row["kind"],
            "target": row["target"],
            "expect_class": row["class"],
        }
        try:
            operations = [build_operation(row, tenant_ns, index)]
        except ValueError as exc:
            emit(cid, outcome="probe-error", detail=f"operation: {exc}"[:400], **common)
            continue

        # A fresh nonce per envelope. See the module docstring: they are single-use.
        try:
            nonce = client.fetch_nonce()
        except Exception as exc:  # noqa: BLE001
            emit(cid, outcome="transport-error", detail=f"nonce: {type(exc).__name__}: {exc}"[:400], **common)
            continue

        try:
            envelope = action_envelope.build_envelope(
                agent_identity=cfg.identity,
                intent=f"undo-coverage-l2 soak: {row['verb']} {row['kind']} (corpus {cid})",
                operations=operations,
                requester={"kind": "system", "id": "undo-coverage-l2"},
                # `cron` for `broker_execute_probe.py`'s reason: 06 §4.1's trigger sources are a
                # closed set of seven and `verification` is not one of them. The filed finding is
                # that the client-side builder does not mirror the set; this is not the unit that
                # closes it.
                trigger={"source": "cron"},
                # SPAN_ID is deliberately unset in the driver pod: when it IS set, `session_trace()`
                # emits `parentSpanId`, which the broker's `Trace` struct has no field for and
                # `DisallowUnknownFields` turns into a 400. A filed defect in shipped agent code.
                trace=broker_client.session_trace(),
                nonce=nonce,
                rationale=(
                    "V-REV-001 at population scale: every executed non-gated record must carry a "
                    "validated undo plan, over a corpus the classifier chose the classes for."
                ),
                dry_run=True,
            )
        except Exception as exc:  # noqa: BLE001
            emit(cid, outcome="probe-error", detail=f"envelope: {type(exc).__name__}: {exc}"[:400], **common)
            continue

        try:
            reply = client.post_envelope(envelope)
        except Exception as exc:  # noqa: BLE001
            emit(cid, outcome="transport-error", detail=f"{type(exc).__name__}: {exc}"[:400], **common)
            continue

        emit(
            cid,
            outcome="http",
            status=reply.get("_status"),
            reason=str(reply.get("reason", "")),
            decision=str(reply.get("decision", "")),
            phase=str(reply.get("phase", "")),
            action_id=str(reply.get("actionId", "")),
            namespace=str(reply.get("namespace", "")),
            detail=str(reply.get("message", ""))[:300],
            **common,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
