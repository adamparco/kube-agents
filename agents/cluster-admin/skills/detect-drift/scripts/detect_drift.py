#!/opt/hermes/.venv/bin/python3
"""detect-drift (cluster-admin tier) — find cluster drift read-only, then remediate through the broker.

The Cluster Admin Agent's drift sweep runs this on a schedule. It reads its **one cluster**: the
namespaces in it, the node pools under it, the add-ons installed in it, and the child agents that own
its tenants ([02](02-agent-personas.md) §2.5.2, cluster-admin row). Detection is a pure function over
JSON the agent captured with a read-only `get` — this script opens no socket, holds no credential,
and mutates nothing.

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
skipped and not routed around.

WHAT THE CLUSTER-ADMIN TIER CAN SEE, WHICH IS WHAT IT MAY CHECK
----------------------------------------------------------------
The reader identity on this pod is cluster-wide and read-only (03 §3.1, §4.2): every namespace in
this one cluster, its node pools, its add-ons and the `Agent` CRs beneath it. That breadth means the
tier notices things it may not do — it provisions and bounds a namespace, then the Developer Team
Agent operates the workloads inside it (02 §4). The five subjects below sit on both sides of that
line, deliberately.

  namespace-missing-baseline  a namespace running with no ResourceQuota / LimitRange / NetworkPolicy
  addon-behind-supported      an add-on running behind the version its channel supports
  nodepool-misprovisioned     a node pool chronically over- or under-provisioned
  workload-missing-pdb        no PodDisruptionBudget ahead of a planned upgrade -> DELEGATE
  namespace-without-agent     a namespace with no Developer Team Agent          -> provision it

Two load-bearing details of the diff survive the conversion unchanged, because they were never about
the PR:

  1. DESIRED-AUTHORITATIVE, SERVER-DEFAULT-TOLERANT. Drift is "does every field the baseline
     specifies still match live?". Fields live adds that desired never specified (server defaults
     like `terminationGracePeriodSeconds`, controller-added fields) are NOT drift, so benign defaults
     do not produce a remediation that changes nothing.
  2. CANONICAL IGNORE-SET. `status`, `managedFields`, `resourceVersion`, `uid`, `creationTimestamp`,
     `generation`, `selfLink`, and the noisy `last-applied-configuration` / `revision` annotations
     are stripped from both sides before diffing.

THIS SCRIPT CARRIES NO COPY OF THE TENANCY BASELINE, ON PURPOSE
-----------------------------------------------------------------
The quota, the limit range and the default-deny policy a tenant namespace gets are already written
down elsewhere — the Platform Agent defines the tenancy model (02 §3) and `provision-developer-team`
renders it. A third copy here is a third thing to drift, which is the lesson `dev/test_skill_templates.py`
exists to hold. So a missing-baseline finding either uses the objects the caller passed in with
`--baseline`, or it reports the gap and names the baseline it is missing. It never hand-authors one.

Exit codes: 0 = no drift; 2 = drift found (and, with --emit-operations, the operations were printed);
1 = error. Never a nonzero-because-it-changed-something: this script only ever *reads*.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# Fields dropped from BOTH sides before diffing — cluster/server bookkeeping, never baseline-authored.
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

# The tenancy kinds every namespace in this cluster is expected to carry, in the order a finding reads
# them out. Membership is also what `--baseline` has to be able to supply.
BASELINE_KINDS = ("ResourceQuota", "LimitRange", "NetworkPolicy")

# A node pool is misprovisioned when sustained P95 utilization sits outside this band. The band is
# wide and the window is long on purpose: the remediation is a resize, and a narrow band produces a
# resize a day — which is a flap the brake would eventually stop, after it had spent the budget.
DEFAULT_MIN_UTILIZATION = 0.35
DEFAULT_MAX_UTILIZATION = 0.85
DEFAULT_SUSTAINED_DAYS = 7
TARGET_UTILIZATION = 0.60

MERGE_PATCH = "application/merge-patch+json"


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
    baseline-authored fields."""
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
# A finding carries its own remediation, and there are exactly four shapes it can take. Three of them
# are NOT "emit an operation", and that is the point: 02 §2.5.1 makes ending a diagnosis with no
# action a defect, and 02 §4 makes operating a tenant's workloads a refusal. `delegate` and `handoff`
# are how a cluster finding still ends in work; `blocked` names the one fact that is missing, so the
# agent can go get it rather than guess.


def finding(
    check: str,
    subject: str,
    evidence: str,
    *,
    operations: list[dict] | None = None,
    delegate: str | None = None,
    handoff: str | None = None,
    blocked: str | None = None,
) -> dict:
    out: dict = {"check": check, "subject": subject, "evidence": evidence}
    if operations:
        out["operations"] = operations
    if delegate:
        out["delegate"] = delegate
    if handoff:
        out["handoff"] = handoff
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


def baseline_drift(desired: dict, live: dict) -> list[dict]:
    """A cluster object this tier declared says one thing and the cluster says another. Re-assert it.

    `apply` and not `patch`: the declaration is the whole authored object, a patch would leave a
    field someone deleted by hand still deleted, and server-side apply is what the broker executes
    anyway (03 §4.1 step 9).
    """
    drifts = find_drift(strip(desired), strip(live))
    if not drifts:
        return []
    fields = ", ".join(d["path"] for d in drifts[:6]) + ("…" if len(drifts) > 6 else "")
    found = finding(
        "baseline-drift",
        object_slug(desired),
        f"{len(drifts)} field(s) diverged from the declared baseline: {fields}",
        operations=[{"op": "apply", "target": target_of(desired), "desiredState": desired}],
    )
    found["fields"] = drifts
    return [found]


def _baseline_object(baseline: dict, kind: str, namespace: str) -> dict | None:
    """The caller-supplied baseline object for `kind`, stamped into `namespace`.

    `--baseline` is a map of kind -> the object this cluster's tenancy model says a namespace gets.
    Only `metadata.namespace` (and a name, if the template left one out) is filled in; every rule in
    the body comes from the supplied object, so this script cannot widen or narrow a baseline it did
    not author.
    """
    template = (baseline or {}).get(kind)
    if not isinstance(template, dict):
        return None
    obj = json.loads(json.dumps(template))  # copy: one template, many namespaces
    meta = obj.setdefault("metadata", {})
    meta["namespace"] = namespace
    if not meta.get("name"):
        meta["name"] = f"{kind.lower()}-baseline"
    return obj


def namespace_baseline_gaps(inventory: dict, baseline: dict | None) -> list[dict]:
    """A namespace in this cluster is running with no quota, no limits, or no network policy.

    Creating a control that was never there is the cluster tier's own work — this is exactly what
    "provisions and bounds a namespace" means (02 §4) — so the remediation is a plain `create` of
    each missing object and the broker decides the rest.
    """
    findings: list[dict] = []
    for entry in inventory.get("namespaces") or []:
        name = str(entry.get("name") or "")
        present = {
            "ResourceQuota": bool(entry.get("resourceQuota")),
            "LimitRange": bool(entry.get("limitRange")),
            "NetworkPolicy": bool(entry.get("networkPolicies")),
        }
        missing = [kind for kind in BASELINE_KINDS if not present[kind]]
        if not missing:
            continue

        evidence = f"namespace {name} is running with no {', '.join(missing)}"
        operations, unresolved = [], []
        for kind in missing:
            obj = _baseline_object(baseline or {}, kind, name)
            if obj is None:
                unresolved.append(kind)
            else:
                operations.append({"op": "create", "target": target_of(obj), "desiredState": obj})
        if unresolved:
            findings.append(
                finding(
                    "namespace-missing-baseline",
                    f"namespace/{name}",
                    evidence,
                    operations=operations,
                    blocked=(
                        f"the tenancy baseline for {', '.join(unresolved)} — pass it with --baseline, read from the "
                        "model the Platform Agent published or from the assets provision-developer-team renders. "
                        "Do not hand-author one here."
                    ),
                )
            )
        else:
            findings.append(finding("namespace-missing-baseline", f"namespace/{name}", evidence, operations=operations))
    return findings


def missing_child_agents(inventory: dict) -> list[dict]:
    """02 §6: a tenant namespace with no Developer Team Agent is a defect the tier above remediates,
    not a configuration someone forgot to fill in.

    The remediation is `provision-developer-team`, which renders the child's whole bundle from the
    tier template. This skill does not emit those operations itself — a second copy of the child
    bundle is a second copy that drifts, and the tier template is the thing that makes an over-grant
    inexpressible (03 §4.2).
    """
    return [
        finding(
            "namespace-without-agent",
            f"namespace/{entry.get('name')}",
            f"namespace {entry.get('name')} holds tenant workloads and has no Developer Team Agent",
            handoff="provision-developer-team",
        )
        for entry in inventory.get("namespaces") or []
        if entry.get("tenant") and not entry.get("developerTeamAgent")
    ]


def addon_versions(inventory: dict) -> list[dict]:
    """An add-on this cluster runs is behind the version its channel supports."""
    findings: list[dict] = []
    for addon in inventory.get("addons") or []:
        installed, supported = addon.get("installedVersion"), addon.get("supportedVersion")
        if not supported or installed == supported:
            continue
        name = addon.get("name")
        evidence = f"add-on {name} is on {installed} and its channel supports {supported}"
        target, container, image = addon.get("target"), addon.get("container"), addon.get("supportedImage")
        if not (target and container and image):
            findings.append(
                finding(
                    "addon-behind-supported",
                    f"addon/{name}",
                    evidence,
                    blocked=f"the workload reference and the {supported} image for {name}",
                )
            )
            continue
        findings.append(
            finding(
                "addon-behind-supported",
                f"addon/{name}",
                evidence,
                operations=[
                    {
                        "op": "patch",
                        "target": target,
                        "patch": {
                            "type": MERGE_PATCH,
                            "body": {
                                "spec": {"template": {"spec": {"containers": [{"name": container, "image": image}]}}}
                            },
                        },
                    }
                ],
            )
        )
    return findings


def nodepool_provisioning(inventory: dict, *, low: float, high: float, sustained_days: int) -> list[dict]:
    """A node pool whose sustained P95 utilization has sat outside the band long enough to act on.

    "Chronically" is the whole check: a pool that spiked yesterday is not over-provisioned, and
    resizing on a spike is how an agent burns its initiative budget oscillating. A pool is only
    reported when its measurement window is at least `sustained_days` long.
    """
    findings: list[dict] = []
    for pool in inventory.get("nodePools") or []:
        utilization, count = pool.get("utilizationP95"), pool.get("nodeCount")
        window = int(pool.get("windowDays") or 0)
        if utilization is None or not count or window < sustained_days:
            continue
        utilization, count = float(utilization), int(count)
        if low <= utilization <= high:
            continue

        name = pool.get("name")
        direction = "over-provisioned" if utilization < low else "under-provisioned"
        wanted = max(1, round(count * utilization / TARGET_UTILIZATION))
        if wanted == count:
            continue  # the band was crossed but the resize rounds to nothing; do not submit a no-op
        evidence = (
            f"node pool {name} has run at P95 {utilization:.0%} across {window}d on {count} node(s) — "
            f"{direction} against the {low:.0%}–{high:.0%} band"
        )
        resource = pool.get("resource")
        if not resource:
            findings.append(
                finding(
                    "nodepool-misprovisioned",
                    f"nodepool/{name}",
                    evidence,
                    blocked=f"the cloud resource path for node pool {name} "
                    f"(projects/<project>/locations/<loc>/clusters/<cluster>/nodePools/{name})",
                )
            )
            continue
        findings.append(
            finding(
                "nodepool-misprovisioned",
                f"nodepool/{name}",
                f"{evidence}; {count} -> {wanted} node(s) lands it near {TARGET_UTILIZATION:.0%}",
                operations=[
                    {
                        "op": "apply",
                        "cloudTarget": {
                            "provider": "gcp",
                            "service": "container.googleapis.com",
                            "resource": resource,
                            "method": "setSize",
                        },
                        "desiredState": {"nodeCount": wanted},
                    }
                ],
            )
        )
    return findings


def missing_disruption_budgets(inventory: dict) -> list[dict]:
    """A tenant workload with no PodDisruptionBudget, with a cluster upgrade coming.

    The upgrade is this tier's to run and the exposure is this tier's to notice, but a PDB is a
    namespace-scoped object about someone else's workload: 02 §4 says operate the cluster, delegate
    the workload. So the finding is ours and the write is the Developer Team Agent's, joined by one
    hop of delegation (02 §2.3) that re-authorizes in its own scope. Submitting the PDB from here
    would be the tier reaching into a tenant, and the broker would refuse it.
    """
    upgrade = inventory.get("upgrade") or {}
    if not upgrade.get("planned"):
        return []  # a PDB gap with no upgrade scheduled is the tenant's own backlog, not a cluster finding
    target_version = upgrade.get("targetVersion") or "the planned upgrade"
    findings: list[dict] = []
    for workload in inventory.get("workloads") or []:
        if workload.get("podDisruptionBudget"):
            continue
        replicas = int(workload.get("replicas") or 0)
        if replicas < 2:
            continue  # a single-replica workload has no disruption budget to express; that is its own finding
        namespace, name = workload.get("namespace"), workload.get("name")
        findings.append(
            finding(
                "workload-missing-pdb",
                f"{namespace}/{name}",
                f"{workload.get('kind') or 'workload'} {name} runs {replicas} replicas with no PodDisruptionBudget, "
                f"and {target_version} is scheduled",
                delegate=f"developer-team-{namespace}",
            )
        )
    return findings


def survey_cluster(inventory: dict, baseline: dict | None, args: argparse.Namespace) -> list[dict]:
    return (
        namespace_baseline_gaps(inventory, baseline)
        + missing_child_agents(inventory)
        + addon_versions(inventory)
        + nodepool_provisioning(
            inventory,
            low=args.min_utilization,
            high=args.max_utilization,
            sustained_days=args.sustained_days,
        )
        + missing_disruption_budgets(inventory)
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
    lines = [f"What I noticed — {len(findings)} drift finding(s) in this cluster:"]
    for f in findings:
        lines.append(f"  [{f['check']}] {f['subject']}: {f['evidence']}")
        if f.get("operations"):
            lines.append(f"      -> remediate: {len(f['operations'])} operation(s) for apply-change/submit_action")
        if f.get("delegate"):
            lines.append(f"      -> delegate to {f['delegate']} — this is workload scope, not the cluster's to apply")
        if f.get("handoff"):
            lines.append(f"      -> hand off to the {f['handoff']} skill")
        if f.get("blocked"):
            lines.append(f"      -> blocked on {f['blocked']}")
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Read-only cluster drift detection for the Cluster Admin Agent.")
    subject = p.add_mutually_exclusive_group(required=True)
    subject.add_argument("--desired", help="Path to a declared baseline manifest for one object (JSON or YAML).")
    subject.add_argument("--cluster", help="Path to the cluster inventory JSON (namespaces, nodePools, addons, …).")
    p.add_argument("--live", help="With --desired: the live object JSON (e.g. `kubectl get -o json`).")
    p.add_argument(
        "--baseline",
        help="Map of " + "/".join(BASELINE_KINDS) + " -> the tenancy object a namespace gets. Never authored here.",
    )
    p.add_argument("--min-utilization", type=float, default=DEFAULT_MIN_UTILIZATION, help="Node-pool band floor.")
    p.add_argument("--max-utilization", type=float, default=DEFAULT_MAX_UTILIZATION, help="Node-pool band ceiling.")
    p.add_argument(
        "--sustained-days", type=int, default=DEFAULT_SUSTAINED_DAYS, help="Days a pool must sit outside the band."
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
        findings = baseline_drift(load_manifest(args.desired), load_manifest(args.live))
    else:
        baseline = load_manifest(args.baseline) if args.baseline else None
        findings = survey_cluster(load_manifest(args.cluster), baseline, args)

    operations = all_operations(findings)

    if args.emit_operations:
        print(json.dumps(operations, indent=2))
    elif args.json:
        print(json.dumps({"drift": bool(findings), "findings": findings, "operations": operations}, indent=2))
    elif not findings:
        print("no drift: the cluster matches every state this tier asserts.")
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
