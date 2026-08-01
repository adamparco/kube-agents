#!/opt/hermes/.venv/bin/python3
"""detect-drift (developer-team tier) — find workload drift read-only, then remediate through the broker.

The Developer Team Agent's drift sweep runs this on a schedule. It reads its **one namespace**: the
workloads in it, the containers inside them, the PVCs they bind and the alerts that watch them
([02](02-agent-personas.md) §2.5.2, developer-team row). Detection is a pure function over JSON the
agent captured with a read-only `get` — this script opens no socket, holds no credential, and mutates
nothing.

WHAT CHANGED, AND WHY IT IS NOT A SMALLER CHANGE THAN IT LOOKS
--------------------------------------------------------------
This skill used to end in a **corrective pull request**: a git branch, an OKF observation entry, and
a handoff to `submit-suggestion`. 02 §2.5.1 puts "opening a GitHub issue, an OKF entry, or a pull
request for work inside its own authority" on the same footing as a failed action, so that whole
path is gone. What replaces it is the **operations** half of an Action Envelope (06 §4.1): this
script emits the concrete changes, and the agent hands them to `apply-change`'s `submit_action`,
which is the only thing in the system that writes.

The agent is safe to act because scope, gating, the initiative budget and the undo plan are enforced
in the broker — a separate process, under a different identity, that the agent cannot reach. So this
script never decides a risk class and never says one out loud (03 §5): it emits operations, the
broker classifies them, and a remediation that comes back `gated` is **reported as gated**, not
skipped and not routed around. Deleting a PVC is gated for this tier (02 §5) — so the delete goes
into the envelope and the answer comes back parked. It does not get skipped, and it does not get
reshaped into something that would classify lower.

WHAT THE DEVELOPER-TEAM TIER CAN SEE, WHICH IS WHAT IT MAY CHECK
------------------------------------------------------------------
The reader identity on this pod stops hard at the namespace edge (03 §3.2, §4.2): one namespace, and
**no cluster-scoped object at all**. Nodes, node pools, StorageClasses, cluster add-ons and every
other namespace are not merely out of policy — an attempt to read one is refused by RBAC. So every
subject below is namespaced, and the one thing this tier does with a finding that turns out to be
about the cluster is **escalate** it to the Cluster Admin Agent (02 §5, §2.3).

  stuck-rollout            a rollout that has not progressed -> roll back, or ESCALATE if it is capacity
  single-replica           a Deployment running one replica with no declared reason
  missing-readiness-probe  a container with no readiness probe
  resources-mismatched     requests or limits far from observed usage
  image-unpinned           an image on `latest`, or with no tag and no digest at all
  orphaned-pvc             a PVC no workload has consumed for a sustained window -> GATED
  alert-untuned            an alert that fires constantly and resolves itself

Two load-bearing details of the diff survive the conversion unchanged, because they were never about
the PR:

  1. DESIRED-AUTHORITATIVE, SERVER-DEFAULT-TOLERANT. Drift is "does every field the manifest
     specifies still match live?". Fields live adds that desired never specified (server defaults
     like `terminationGracePeriodSeconds`, controller-added fields) are NOT drift, so benign defaults
     do not produce a remediation that changes nothing.
  2. CANONICAL IGNORE-SET. `status`, `managedFields`, `resourceVersion`, `uid`, `creationTimestamp`,
     `generation`, `selfLink`, and the noisy `last-applied-configuration` / `revision` annotations
     are stripped from both sides before diffing.

UNITS, BECAUSE A QUANTITY PARSER IS A THIRD PLACE TO BE WRONG
---------------------------------------------------------------
The namespace inventory carries CPU in **millicores** and memory in **MiB**, as plain integers, on
both the request/limit side and the observed side. This script does no `resource.Quantity` parsing:
the numbers it compares are the numbers it was handed, and the patches it emits spell them back as
`<n>m` and `<n>Mi`.

Exit codes: 0 = no drift; 2 = drift found (and, with --emit-operations, the operations were printed);
1 = error. Never a nonzero-because-it-changed-something: this script only ever *reads*.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# Fields dropped from BOTH sides before diffing — cluster/server bookkeeping, never manifest-authored.
IGNORE_KEYS = {
    "managedFields",
    "resourceVersion",
    "uid",
    "creationTimestamp",
    "generation",
    "selfLink",
}
IGNORE_ANNOTATIONS = {
    "kubectl.kubernetes.io/last-applied-configuration",
    "deployment.kubernetes.io/revision",
}

# Right-sizing thresholds. Wide, because the remediation restarts every pod in the workload and a
# narrow band would restart the team's service on every sweep.
DEFAULT_MIN_REPLICAS = 2
DEFAULT_OVERPROVISION_FACTOR = 2.5  # request above P95 x this -> over-requested
DEFAULT_UNDERPROVISION_FACTOR = 1.1  # request below P95 x this -> under-requested
HEADROOM = 1.25  # a corrected request, as a multiple of observed P95
DEFAULT_ORPHAN_DAYS = 14
DEFAULT_STUCK_MINUTES = 30
DEFAULT_NOISY_FIRES_PER_DAY = 6.0
DEFAULT_ACTIONED_RATIO = 0.1

# A stuck rollout whose pods cannot be placed is not this tier's problem to solve — it is node
# capacity, which 02 §5 says escalate rather than attempt.
CAPACITY_REASONS = {"Unschedulable", "InsufficientCapacity", "FailedScheduling"}

MERGE_PATCH = "application/merge-patch+json"
JSON_PATCH = "application/json-patch+json"


def load_manifest(path: str) -> dict:
    """Load a manifest as a dict. JSON always; YAML if PyYAML is present (it is in the agent image)."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        import yaml  # deferred; only needed for YAML inputs

        return yaml.safe_load(text)


def strip(obj):
    """Recursively drop the ignore-set (and `status`, and noisy annotations) so the diff sees only
    manifest-authored fields."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in IGNORE_KEYS or k == "status":
                continue
            if k == "annotations" and isinstance(v, dict):
                v = {ak: av for ak, av in v.items() if ak not in IGNORE_ANNOTATIONS}
                if not v:
                    continue
            out[k] = strip(v)
        return out
    if isinstance(obj, list):
        return [strip(x) for x in obj]
    return obj


def _stable(obj) -> str:
    return json.dumps(obj, sort_keys=True)


def find_drift(desired, live, path: str = "") -> list[dict]:
    """Return the list of drifted paths. Desired-authoritative: only fields present in `desired` are
    checked, so server-defaulted / controller-added fields in `live` never count as drift."""
    drifts: list[dict] = []
    if isinstance(desired, dict):
        if not isinstance(live, dict):
            return [{"path": path or "/", "desired": desired, "live": live, "kind": "type-mismatch"}]
        for k in sorted(desired):
            child = f"{path}.{k}" if path else k
            if k not in live:
                drifts.append({"path": child, "desired": desired[k], "live": None, "kind": "missing-in-live"})
            else:
                drifts += find_drift(desired[k], live[k], child)
    elif isinstance(desired, list):
        live_list = live if isinstance(live, list) else []
        live_canon = [_stable(x) for x in live_list]
        for i, elem in enumerate(desired):
            if _stable(elem) not in live_canon:
                drifts.append({"path": f"{path}[{i}]", "desired": elem, "live": None, "kind": "missing-list-elem"})
    else:
        if desired != live:
            drifts.append({"path": path or "/", "desired": desired, "live": live, "kind": "changed"})
    return drifts


def object_slug(desired: dict, override: str | None = None) -> str:
    if override:
        return override
    kind = str(desired.get("kind", "object")).lower()
    name = str((desired.get("metadata") or {}).get("name", "unknown")).lower()
    return re.sub(r"[^a-z0-9]+", "-", f"{kind}-{name}").strip("-") or "object"


# --- findings -------------------------------------------------------------------------------------
#
# A finding carries its own remediation, and there are exactly three shapes it can take here. Two of
# them are NOT "emit an operation", and that is the point: 02 §2.5.1 makes ending a diagnosis with no
# action a defect, and 02 §5 makes attempting cluster-level work a refusal. `escalate` is how a
# workload finding that turns out to be about the cluster still ends in work; `blocked` names the one
# fact that is missing, so the agent can go and read it rather than guess.


def finding(
    check: str,
    subject: str,
    evidence: str,
    *,
    operations: list[dict] | None = None,
    escalate: str | None = None,
    blocked: str | None = None,
) -> dict:
    out: dict = {"check": check, "subject": subject, "evidence": evidence}
    if operations:
        out["operations"] = operations
    if escalate:
        out["escalate"] = escalate
    if blocked:
        out["blocked"] = blocked
    return out


def target_of(obj: dict) -> dict:
    """The 06 §4.1 target reference for a Kubernetes object, read out of the object itself."""
    api = str(obj.get("apiVersion") or "v1")
    group, _, version = api.rpartition("/")
    meta = obj.get("metadata") or {}
    target = {"group": group, "version": version or "v1", "kind": obj.get("kind") or "", "name": meta.get("name") or ""}
    if meta.get("namespace"):
        target["namespace"] = meta["namespace"]
    return target


def workload_target(workload: dict, namespace: str) -> dict:
    """The target reference for an inventory workload entry (`apps/v1` unless it says otherwise)."""
    api = str(workload.get("apiVersion") or "apps/v1")
    group, _, version = api.rpartition("/")
    return {
        "group": group,
        "version": version or "v1",
        "kind": workload.get("kind") or "Deployment",
        "namespace": workload.get("namespace") or namespace,
        "name": workload.get("name") or "",
    }


def container_patch(workload: dict, namespace: str, container: dict) -> dict:
    """A merge patch that reaches exactly one container in a workload's pod template.

    A strategic merge on `containers` keys by `name`, so naming the container and changing nothing
    else is what makes this a one-container edit rather than a list replacement that drops the
    sidecars.
    """
    return {
        "op": "patch",
        "target": workload_target(workload, namespace),
        "patch": {
            "type": MERGE_PATCH,
            "body": {"spec": {"template": {"spec": {"containers": [container]}}}},
        },
    }


def manifest_drift(desired: dict, live: dict) -> list[dict]:
    """The team's manifest says one thing and the namespace says another. Re-assert the manifest.

    `apply` and not `patch`: the manifest is the whole authored object, a patch would leave a field
    someone deleted by hand still deleted, and server-side apply is what the broker executes anyway
    (03 §4.1 step 9).
    """
    drifts = find_drift(strip(desired), strip(live))
    if not drifts:
        return []
    fields = ", ".join(d["path"] for d in drifts[:6]) + ("…" if len(drifts) > 6 else "")
    found = finding(
        "manifest-drift",
        object_slug(desired),
        f"{len(drifts)} field(s) diverged from the declared manifest: {fields}",
        operations=[{"op": "apply", "target": target_of(desired), "desiredState": desired}],
    )
    found["fields"] = drifts
    return [found]


def _containers(workload: dict) -> list[dict]:
    return [c for c in workload.get("containers") or [] if isinstance(c, dict)]


def is_unpinned(image: str) -> bool:
    """`repo:latest`, or `repo` with no tag at all, is unpinned. A digest reference never is.

    The `rsplit("/")` matters: a registry with a port (`registry.example.com:5000/app`) puts a colon
    in the reference that is not a tag, and reading it as one calls every such image unpinned.
    """
    if not image or "@" in image:
        return False
    last = image.rsplit("/", 1)[-1]
    tag = last.rsplit(":", 1)[1] if ":" in last else ""
    return tag in ("", "latest")


def stuck_rollouts(inventory: dict, *, stuck_minutes: int) -> list[dict]:
    """A rollout that has been part-way for longer than its progress deadline.

    Two different problems wear the same shape here, and telling them apart is the whole check. If
    the new pods cannot be **placed**, this is node capacity: 02 §5 says escalate to the Cluster
    Admin Agent rather than attempt it, and rolling back would hide a cluster problem behind a
    workload fix. If the new pods are placed and unhealthy, it is the team's own rollout and the
    remediation is a roll back to the pod template of the revision that was working.
    """
    namespace = str(inventory.get("namespace") or "")
    findings: list[dict] = []
    for workload in inventory.get("workloads") or []:
        rollout = workload.get("rollout") or {}
        stalled = int(rollout.get("stalledMinutes") or 0)
        if not rollout.get("inProgress") or stalled < stuck_minutes:
            continue
        name, reason = workload.get("name"), str(rollout.get("reason") or "")
        subject = f"{workload.get('kind') or 'Deployment'}/{name}"
        evidence = (
            f"{name} has been mid-rollout for {stalled}m with {rollout.get('updatedReplicas', 0)}/"
            f"{workload.get('replicas', '?')} updated replica(s) ready ({reason or 'no reason reported'})"
        )
        if reason in CAPACITY_REASONS:
            findings.append(
                finding(
                    "stuck-rollout",
                    subject,
                    f"{evidence} — the new pods cannot be scheduled, which is node capacity, not this workload",
                    escalate="cluster-admin",
                )
            )
            continue
        previous = rollout.get("previousTemplate")
        if not previous:
            findings.append(
                finding(
                    "stuck-rollout",
                    subject,
                    evidence,
                    blocked=f"the pod template of the last healthy revision of {name} (read it off that ReplicaSet)",
                )
            )
            continue
        findings.append(
            finding(
                "stuck-rollout",
                subject,
                f"{evidence}; rolling back to the last revision that went ready",
                operations=[
                    {
                        "op": "patch",
                        "target": workload_target(workload, namespace),
                        "patch": {"type": MERGE_PATCH, "body": {"spec": {"template": previous}}},
                    }
                ],
            )
        )
    return findings


def single_replica_workloads(inventory: dict, *, min_replicas: int) -> list[dict]:
    """A Deployment running one replica has no availability during a node drain or a rollout.

    A workload that genuinely cannot run two — a leader-less singleton, a batch runner, anything
    holding a ReadWriteOnce volume it cannot share — declares `"singleton": true` in the inventory
    and is not a finding. That flag lives in the inventory rather than in a label this script
    invents, because inventing an annotation is how a convention nothing enforces gets born.
    """
    namespace = str(inventory.get("namespace") or "")
    findings: list[dict] = []
    for workload in inventory.get("workloads") or []:
        if workload.get("singleton"):
            continue
        replicas = workload.get("replicas")
        if replicas is None or int(replicas) >= min_replicas:
            continue
        name = workload.get("name")
        findings.append(
            finding(
                "single-replica",
                f"{workload.get('kind') or 'Deployment'}/{name}",
                f"{name} runs {replicas} replica(s) — one node drain or one rollout is a full outage",
                operations=[
                    {
                        "op": "scale",
                        "target": workload_target(workload, namespace),
                        "scale": {"replicas": min_replicas},
                    }
                ],
            )
        )
    return findings


def missing_readiness_probes(inventory: dict) -> list[dict]:
    """A container with no readiness probe takes traffic before it can serve it.

    The probe cannot be guessed: a wrong readiness path is an outage dressed as a fix. So this emits
    one only when the inventory carries the container's health path and port — both readable from
    what is already running — and otherwise reports the finding blocked on that one fact.
    """
    namespace = str(inventory.get("namespace") or "")
    findings: list[dict] = []
    for workload in inventory.get("workloads") or []:
        for container in _containers(workload):
            if container.get("readinessProbe"):
                continue
            cname, wname = container.get("name"), workload.get("name")
            evidence = f"container {cname} in {wname} has no readiness probe — it takes traffic before it is ready"
            path, port = container.get("healthPath"), container.get("healthPort")
            if not (path and port):
                findings.append(
                    finding(
                        "missing-readiness-probe",
                        f"{wname}/{cname}",
                        evidence,
                        blocked=(
                            f"the readiness signal for {cname} — an HTTP path and port. Ask the team, or read it off "
                            "the liveness probe or the Service port if one of those is already right."
                        ),
                    )
                )
                continue
            findings.append(
                finding(
                    "missing-readiness-probe",
                    f"{wname}/{cname}",
                    evidence,
                    operations=[
                        container_patch(
                            workload,
                            namespace,
                            {
                                "name": cname,
                                "readinessProbe": {
                                    "httpGet": {"path": path, "port": port},
                                    "initialDelaySeconds": 5,
                                    "periodSeconds": 10,
                                },
                            },
                        )
                    ],
                )
            )
    return findings


def _corrected(requested, observed, *, over: float, under: float) -> int | None:
    """The corrected request, or None when the current one is already inside the band."""
    if not requested or not observed:
        return None
    requested, observed = int(requested), int(observed)
    if observed * under <= requested <= observed * over:
        return None
    return max(1, round(observed * HEADROOM))


def resource_mismatches(inventory: dict, *, over: float, under: float) -> list[dict]:
    """Requests or limits far from what the workload actually uses.

    Over-requesting spends the namespace's quota on nothing and starves the team's next deployment;
    under-requesting gets the pod evicted or OOMKilled. Both are corrected toward observed P95 plus
    headroom, and a memory limit below the corrected request is raised with it — otherwise the fix
    would post a request the container's own ceiling forbids.
    """
    namespace = str(inventory.get("namespace") or "")
    findings: list[dict] = []
    for workload in inventory.get("workloads") or []:
        for container in _containers(workload):
            requests = container.get("requests") or {}
            usage = container.get("usageP95") or {}
            cname, wname = container.get("name"), workload.get("name")

            cpu = _corrected(requests.get("cpu"), usage.get("cpu"), over=over, under=under)
            memory = _corrected(requests.get("memory"), usage.get("memory"), over=over, under=under)
            if cpu is None and memory is None:
                continue

            resources: dict = {"requests": {}}
            parts: list[str] = []
            if cpu is not None:
                resources["requests"]["cpu"] = f"{cpu}m"
                parts.append(f"cpu {requests.get('cpu')}m -> {cpu}m against P95 {usage.get('cpu')}m")
            if memory is not None:
                resources["requests"]["memory"] = f"{memory}Mi"
                parts.append(f"memory {requests.get('memory')}Mi -> {memory}Mi against P95 {usage.get('memory')}Mi")
                limit = (container.get("limits") or {}).get("memory")
                if limit and int(limit) < memory:
                    resources["limits"] = {"memory": f"{memory}Mi"}
                    parts.append(f"memory limit {limit}Mi -> {memory}Mi so the corrected request fits under it")

            findings.append(
                finding(
                    "resources-mismatched",
                    f"{wname}/{cname}",
                    f"container {cname} in {wname}: {'; '.join(parts)}",
                    operations=[container_patch(workload, namespace, {"name": cname, "resources": resources})],
                )
            )
    return findings


def unpinned_images(inventory: dict) -> list[dict]:
    """An image on `latest` means the next restart is a deploy nobody made.

    The right pin is the digest that is **already running**, readable from pod status — so this never
    picks a new version, it freezes the one the team already has. Without that digest the finding is
    blocked on it rather than guessing a tag, because guessing a tag is a deploy.
    """
    namespace = str(inventory.get("namespace") or "")
    findings: list[dict] = []
    for workload in inventory.get("workloads") or []:
        for container in _containers(workload):
            image = str(container.get("image") or "")
            if not is_unpinned(image):
                continue
            cname, wname = container.get("name"), workload.get("name")
            evidence = f"container {cname} in {wname} runs {image} — the next restart could be a different build"
            digest = container.get("runningDigest")
            if not digest:
                findings.append(
                    finding(
                        "image-unpinned",
                        f"{wname}/{cname}",
                        evidence,
                        blocked=f"the digest {cname} is running right now (`.status.containerStatuses[].imageID`)",
                    )
                )
                continue
            repository = image.rsplit(":", 1)[0] if ":" in image.rsplit("/", 1)[-1] else image
            findings.append(
                finding(
                    "image-unpinned",
                    f"{wname}/{cname}",
                    f"{evidence}; pinning it to the build already running ({digest})",
                    operations=[
                        container_patch(workload, namespace, {"name": cname, "image": f"{repository}@{digest}"})
                    ],
                )
            )
    return findings


def orphaned_pvcs(inventory: dict, *, orphan_days: int) -> list[dict]:
    """A PVC nothing has mounted for a sustained window is quota the team is paying for twice.

    Deleting one is **gated** for this tier (02 §5): a PVC is stateful and not reconstructable, so
    the broker parks it for a human. Submit it anyway and report it as parked — a gated remediation
    that never gets submitted is a decision the team never gets to make.

    The delete carries `preconditions.uid` whenever the inventory has one, so an approval that sits
    in the queue overnight cannot land on a *different* PVC that took the same name in the meantime.
    """
    namespace = str(inventory.get("namespace") or "")
    findings: list[dict] = []
    for pvc in inventory.get("persistentVolumeClaims") or []:
        if pvc.get("consumers"):
            continue
        unused = int(pvc.get("unusedDays") or 0)
        if unused < orphan_days:
            continue
        name = pvc.get("name")
        operation: dict = {
            "op": "delete",
            "target": {
                "group": "",
                "version": "v1",
                "kind": "PersistentVolumeClaim",
                "namespace": namespace,
                "name": name,
            },
            "delete": {"propagationPolicy": "Foreground"},
        }
        if pvc.get("uid"):
            operation["delete"]["preconditions"] = {"uid": pvc["uid"]}
        findings.append(
            finding(
                "orphaned-pvc",
                f"persistentvolumeclaim/{name}",
                f"PVC {name} ({pvc.get('capacity') or 'unknown size'}) has had no consumer for {unused}d",
                operations=[operation],
            )
        )
    return findings


def untuned_alerts(inventory: dict, *, fires_per_day: float, actioned_ratio: float) -> list[dict]:
    """An alert that fires constantly and resolves itself is an alert nobody reads any more.

    The tuning is one deterministic move — hold the alert for longer than its own firings last, so
    the ones that clear on their own stop paging — and it needs the alert's observed self-resolve
    time. It is a JSON Patch at a named pointer because a rule lives in a list inside a list, and a
    merge patch on `spec.groups` would replace every other rule in the file.

    This never deletes an alert and never moves a threshold. Both are judgements about what the team
    wants to be told, and neither of them is drift.
    """
    findings: list[dict] = []
    for alert in inventory.get("alerts") or []:
        fires = float(alert.get("firesPerDay") or 0)
        actioned = float(alert.get("actionedRatio") or 0)
        if fires < fires_per_day or actioned > actioned_ratio:
            continue
        name = alert.get("name")
        evidence = (
            f"alert {name} fires {fires:.0f}x/day and {actioned:.0%} of those were acted on — it is training the "
            "team to ignore it"
        )
        target, rule_path = alert.get("target"), alert.get("rulePath")
        self_resolve, current_for = alert.get("p90SelfResolveSeconds"), alert.get("forSeconds")
        if not (target and rule_path and self_resolve):
            findings.append(
                finding(
                    "alert-untuned",
                    f"alert/{name}",
                    evidence,
                    blocked=(
                        f"the rule object and JSON-Pointer path for {name}, plus how long its firings last before "
                        "they self-resolve (p90SelfResolveSeconds)"
                    ),
                )
            )
            continue
        wanted = max(int(self_resolve) + 60, int(current_for or 0) + 60)
        findings.append(
            finding(
                "alert-untuned",
                f"alert/{name}",
                f"{evidence}; holding it for {wanted}s instead of {int(current_for or 0)}s clears the firings that "
                "resolve on their own",
                operations=[
                    {
                        "op": "patch",
                        "target": target,
                        "patch": {
                            "type": JSON_PATCH,
                            "body": [{"op": "replace", "path": f"{rule_path}/for", "value": f"{wanted}s"}],
                        },
                    }
                ],
            )
        )
    return findings


def survey_namespace(inventory: dict, args: argparse.Namespace) -> list[dict]:
    return (
        stuck_rollouts(inventory, stuck_minutes=args.stuck_minutes)
        + single_replica_workloads(inventory, min_replicas=args.min_replicas)
        + missing_readiness_probes(inventory)
        + resource_mismatches(inventory, over=args.overprovision_factor, under=args.underprovision_factor)
        + unpinned_images(inventory)
        + orphaned_pvcs(inventory, orphan_days=args.orphan_days)
        + untuned_alerts(inventory, fires_per_day=args.noisy_fires_per_day, actioned_ratio=args.actioned_ratio)
    )


# --- reporting ------------------------------------------------------------------------------------


def all_operations(findings: list[dict]) -> list[dict]:
    return [op for f in findings for op in f.get("operations") or []]


def render(findings: list[dict]) -> str:
    """The `What I noticed` beat of the 02 §2.5.4 report, plus what each finding ends in.

    The other three beats belong to the agent, because this script cannot know them: `What I did`
    comes from the broker's reply, `How I verified` from the observation window after it, and the
    undo handle from the `ActionRecord`. Nothing here states a risk class — the broker computes it.
    """
    lines = [f"What I noticed — {len(findings)} drift finding(s) in this namespace:"]
    for f in findings:
        lines.append(f"  [{f['check']}] {f['subject']}: {f['evidence']}")
        if f.get("operations"):
            lines.append(f"      -> remediate: {len(f['operations'])} operation(s) for apply-change/submit_action")
        if f.get("escalate"):
            lines.append(f"      -> escalate to {f['escalate']} — this is past the namespace edge")
        if f.get("blocked"):
            lines.append(f"      -> blocked on {f['blocked']}")
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Read-only workload drift detection for the Developer Team Agent.")
    subject = p.add_mutually_exclusive_group(required=True)
    subject.add_argument("--desired", help="Path to the team's declared manifest for one object (JSON or YAML).")
    subject.add_argument("--namespace-state", help="Path to the namespace inventory JSON (workloads, PVCs, alerts).")
    p.add_argument("--live", help="With --desired: the live object JSON (e.g. `kubectl get -o json`).")
    p.add_argument("--min-replicas", type=int, default=DEFAULT_MIN_REPLICAS, help="Replicas a workload should have.")
    p.add_argument(
        "--overprovision-factor",
        type=float,
        default=DEFAULT_OVERPROVISION_FACTOR,
        help="A request above P95 x this is over-requested.",
    )
    p.add_argument(
        "--underprovision-factor",
        type=float,
        default=DEFAULT_UNDERPROVISION_FACTOR,
        help="A request below P95 x this is under-requested.",
    )
    p.add_argument("--orphan-days", type=int, default=DEFAULT_ORPHAN_DAYS, help="Days a PVC must sit with no consumer.")
    p.add_argument("--stuck-minutes", type=int, default=DEFAULT_STUCK_MINUTES, help="Minutes a rollout may be stalled.")
    p.add_argument(
        "--noisy-fires-per-day",
        type=float,
        default=DEFAULT_NOISY_FIRES_PER_DAY,
        help="Fires per day that count as noisy.",
    )
    p.add_argument(
        "--actioned-ratio",
        type=float,
        default=DEFAULT_ACTIONED_RATIO,
        help="Below this actioned share, the alert is noise.",
    )
    p.add_argument("--json", action="store_true", help="Emit the whole report as JSON.")
    p.add_argument(
        "--emit-operations",
        action="store_true",
        help="Print only the Action Envelope operations, ready for apply-change's submit_action.",
    )
    return p.parse_args(argv)


def run(argv: list[str]) -> int:
    args = parse_args(argv)

    if args.desired:
        if not args.live:
            raise ValueError("--desired requires --live (the read-only `get` of the same object).")
        findings = manifest_drift(load_manifest(args.desired), load_manifest(args.live))
    else:
        findings = survey_namespace(load_manifest(args.namespace_state), args)

    operations = all_operations(findings)

    if args.emit_operations:
        print(json.dumps(operations, indent=2))
    elif args.json:
        print(json.dumps({"drift": bool(findings), "findings": findings, "operations": operations}, indent=2))
    elif not findings:
        print("no drift: the namespace matches every state this tier asserts.")
    else:
        print(render(findings))

    return 2 if findings else 0


def main() -> int:
    try:
        return run(sys.argv[1:])
    except Exception as e:  # noqa: BLE001 — clean error, non-zero exit
        print(f"detect-drift: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
