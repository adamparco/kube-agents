#!/opt/hermes/.venv/bin/python3
"""detect-drift (platform tier) — find fleet drift read-only, then remediate it through the broker.

The Platform Agent's drift sweep runs this on a schedule. It reads its **project**: the clusters in
the fleet, the child agents that govern them, the namespaces whose tenancy baseline it owns, and the
IaC mirror it reconciles against ([02](02-agent-personas.md) §2.5.2, platform row). Detection is a
pure function over JSON the agent captured with a read-only `get` — this script opens no socket,
holds no credential, and mutates nothing.

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

WHAT THE PLATFORM TIER CAN SEE, WHICH IS WHAT IT MAY CHECK
-----------------------------------------------------------
The reader identity on this pod is project-wide and read-only (03 §3.1, §3.2): the fleet's clusters,
its cloud resources, and the `Agent` CRs beneath it. So the four subjects below are fleet subjects,
and the one thing this tier must NOT do with a finding is fix it inside a cluster: namespace-scoped
tenancy objects and cluster internals are outside its templated write surface, so it **delegates**
them to the Cluster Admin Agent that owns them (02 §3, §2.3) rather than reaching in.

  mirror-drift               the executed state of an object no longer matches the IaC mirror
  fleet-version-skew         a cluster's control plane is a minor behind the newest in the fleet
  tenancy-baseline-missing   a governed namespace is missing a baseline kind  -> DELEGATE
  cluster-without-agent      a cluster in the project has no Cluster Admin Agent -> provision it

Two load-bearing details of the diff survive the conversion unchanged, because they were never about
the PR:

  1. DESIRED-AUTHORITATIVE, SERVER-DEFAULT-TOLERANT. Drift is "does every field the mirror specifies
     still match live?". Fields live adds that desired never specified (server defaults like
     `terminationGracePeriodSeconds`, controller-added fields) are NOT drift, so benign defaults do
     not produce a remediation that changes nothing.
  2. CANONICAL IGNORE-SET. `status`, `managedFields`, `resourceVersion`, `uid`, `creationTimestamp`,
     `generation`, `selfLink`, and the noisy `last-applied-configuration` / `revision` annotations
     are stripped from both sides before diffing.

Exit codes: 0 = no drift; 2 = drift found (and, with --emit-operations, the operations were printed);
1 = error. Never a nonzero-because-it-changed-something: this script only ever *reads*.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# Fields dropped from BOTH sides before diffing — cluster/server bookkeeping, never mirror-authored.
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

# `1.31.4-gke.1183000`, `v1.30.9`, `1.29` — only the (major, minor) pair decides skew. Patch and the
# `-gke.N` suffix move on their own release cadence and comparing them produces a finding a week.
GKE_VERSION = re.compile(r"^v?(\d+)\.(\d+)")


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
    mirror-authored fields."""
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
# action a defect, and 02 §3 makes reaching into a cluster's internals a refusal. `delegate` and
# `handoff` are how a fleet finding still ends in work; `blocked` names the one fact that is missing,
# so the agent can go get it rather than guess.


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


def mirror_drift(desired: dict, live: dict) -> list[dict]:
    """The IaC mirror says one thing and the cluster says another. Re-assert the mirror.

    `apply` and not `patch`: the mirror is the whole authored object, a patch would leave a field
    someone deleted by hand still deleted, and server-side apply is what the broker executes anyway
    (03 §4.1 step 9).
    """
    drifts = find_drift(strip(desired), strip(live))
    if not drifts:
        return []
    fields = ", ".join(d["path"] for d in drifts[:6]) + ("…" if len(drifts) > 6 else "")
    found = finding(
        "mirror-drift",
        object_slug(desired),
        f"{len(drifts)} field(s) diverged from the IaC mirror: {fields}",
        operations=[{"op": "apply", "target": target_of(desired), "desiredState": desired}],
    )
    found["fields"] = drifts
    return [found]


def cluster_resource(cluster: dict, project_id: str) -> str | None:
    """`projects/<p>/locations/<l>/clusters/<c>` — the cloudTarget resource path, if it is derivable."""
    if cluster.get("resource"):
        return str(cluster["resource"])
    location, name = cluster.get("location"), cluster.get("name")
    if project_id and location and name:
        return f"projects/{project_id}/locations/{location}/clusters/{name}"
    return None


def parse_minor(version: str | None) -> tuple[int, int] | None:
    m = GKE_VERSION.match(str(version or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def version_skew(inventory: dict) -> list[dict]:
    """A cluster whose control plane is behind the newest minor running in the same fleet."""
    project_id = str(inventory.get("projectId") or "")
    clusters = inventory.get("clusters") or []
    minors = {c.get("name"): parse_minor(c.get("controlPlaneVersion")) for c in clusters}
    known = [m for m in minors.values() if m]
    if len(known) < 2:
        return []  # one cluster, or none with a readable version: there is no fleet to skew against
    newest = max(known)

    findings: list[dict] = []
    for cluster in clusters:
        name = cluster.get("name")
        mine = minors.get(name)
        if not mine or mine >= newest:
            continue
        newest_str = f"{newest[0]}.{newest[1]}"
        evidence = (
            f"control plane is on {cluster.get('controlPlaneVersion')} ({mine[0]}.{mine[1]}) while the "
            f"fleet's newest is {newest_str}"
        )
        resource = cluster_resource(cluster, project_id)
        if not resource:
            findings.append(
                finding(
                    "fleet-version-skew",
                    f"cluster/{name}",
                    evidence,
                    blocked=f"the cloud resource path for cluster {name} (projects/<project>/locations/<loc>/clusters/{name})",
                )
            )
            continue
        findings.append(
            finding(
                "fleet-version-skew",
                f"cluster/{name}",
                evidence,
                operations=[
                    {
                        "op": "apply",
                        "cloudTarget": {
                            "provider": "gcp",
                            "service": "container.googleapis.com",
                            "resource": resource,
                            "method": "update",
                        },
                        "desiredState": {"desiredMasterVersion": newest_str},
                    }
                ],
            )
        )
    return findings


def missing_child_agents(inventory: dict) -> list[dict]:
    """02 §6: a cluster with no Cluster Admin Agent is a defect the tier above remediates, not a
    configuration someone forgot to fill in.

    The remediation is `provision-cluster-admin`, which renders the child's whole bundle from the
    tier template. This skill does not emit those operations itself — a second copy of the child
    bundle is a second copy that drifts, and the tier template is the thing that makes an over-grant
    inexpressible (03 §4.2).
    """
    governed = {
        str(a.get("cluster") or (a.get("scope") or {}).get("clusterName") or "")
        for a in inventory.get("agents") or []
        if a.get("tier") == "cluster-admin"
    }
    return [
        finding(
            "cluster-without-agent",
            f"cluster/{cluster.get('name')}",
            f"cluster {cluster.get('name')} in {cluster.get('location') or 'the project'} has no Cluster Admin Agent",
            handoff="provision-cluster-admin",
        )
        for cluster in inventory.get("clusters") or []
        if cluster.get("name") not in governed
    ]


def tenancy_baseline_gaps(inventory: dict) -> list[dict]:
    """A namespace the platform governs is missing a kind its tenancy baseline requires.

    The platform tier **defines** the tenancy model and does not apply it (02 §2.1, §3): a
    namespace-scoped ResourceQuota or NetworkPolicy is cluster-internal, outside this tier's
    templated write surface, so submitting it would be refused rather than merely impolite. The
    finding therefore ends in a one-hop delegation to that cluster's Cluster Admin Agent (02 §2.3),
    which re-authorizes in its own scope and runs its own broker pipeline.
    """
    findings: list[dict] = []
    for entry in inventory.get("governedNamespaces") or []:
        required = list(entry.get("baseline") or [])
        present = set(entry.get("present") or [])
        missing = [kind for kind in required if kind not in present]
        if not missing:
            continue
        cluster, namespace = entry.get("cluster"), entry.get("namespace")
        findings.append(
            finding(
                "tenancy-baseline-missing",
                f"{cluster}/{namespace}",
                f"namespace {namespace} is running with no {', '.join(missing)} — the tenancy baseline requires "
                f"{', '.join(required)}",
                delegate=f"cluster-admin-{cluster}",
            )
        )
    return findings


def survey_fleet(inventory: dict) -> list[dict]:
    return version_skew(inventory) + missing_child_agents(inventory) + tenancy_baseline_gaps(inventory)


# --- reporting ------------------------------------------------------------------------------------


def all_operations(findings: list[dict]) -> list[dict]:
    return [op for f in findings for op in f.get("operations") or []]


def render(findings: list[dict]) -> str:
    """The `What I noticed` beat of the 02 §2.5.4 report, plus what each finding ends in.

    The other three beats belong to the agent, because this script cannot know them: `What I did`
    comes from the broker's reply, `How I verified` from the observation window after it, and the
    undo handle from the `ActionRecord`. Nothing here states a risk class — the broker computes it.
    """
    lines = [f"What I noticed — {len(findings)} drift finding(s) in the fleet:"]
    for f in findings:
        lines.append(f"  [{f['check']}] {f['subject']}: {f['evidence']}")
        if f.get("operations"):
            lines.append(f"      -> remediate: {len(f['operations'])} operation(s) for apply-change/submit_action")
        if f.get("delegate"):
            lines.append(f"      -> delegate to {f['delegate']} — this is cluster-internal, not the platform's to apply")
        if f.get("handoff"):
            lines.append(f"      -> hand off to the {f['handoff']} skill")
        if f.get("blocked"):
            lines.append(f"      -> blocked on {f['blocked']}")
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Read-only fleet drift detection for the Platform Agent.")
    subject = p.add_mutually_exclusive_group(required=True)
    subject.add_argument("--desired", help="Path to the IaC-mirror manifest for one object (JSON or YAML).")
    subject.add_argument("--fleet", help="Path to the fleet inventory JSON (clusters, agents, governedNamespaces).")
    p.add_argument("--live", help="With --desired: the live object JSON (e.g. `kubectl get -o json`).")
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
        findings = mirror_drift(load_manifest(args.desired), load_manifest(args.live))
    else:
        findings = survey_fleet(load_manifest(args.fleet))

    operations = all_operations(findings)

    if args.emit_operations:
        print(json.dumps(operations, indent=2))
    elif args.json:
        print(json.dumps({"drift": bool(findings), "findings": findings, "operations": operations}, indent=2))
    elif not findings:
        print("no drift: the fleet matches every state this tier asserts.")
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
