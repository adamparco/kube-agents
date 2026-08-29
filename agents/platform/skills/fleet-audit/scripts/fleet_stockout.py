#!/opt/hermes/.venv/bin/python3
"""fleet_stockout.py — Procedural collector for the Fleet Stockout
Prevention & Capacity Audit (`stockout-prevention`).

See docs/designs/fleet-audit-collectors-and-status.md §4.2, §10 phase 4, and
governance/stockout_prevention_sop.md.

**Partial conversion, deliberately.** This stream's twelve checks split
into two groups by how confident this collector can be in the field it is
reading. Ten read structures this repository's other collectors already
read with confidence — a `ComputeClass`/`Deployment`/`StatefulSet`/
`StorageClass`/`Node` dump, `gcloud container node-pools list`, `gcloud compute
reservations list`, `gcloud compute regions describe --format=json(quotas)`
— and are converted here: `ccc-missing-fallbacks`, `ccc-no-ondemand-floor`,
`ccc-large-vm-scarcity`, `ccc-priority-starvation`,
`ccc-mixed-disk-generations`, `ccc-hyperdisk-incompatible`,
`quota-exhaustion-risk`, `single-zone-nodepool`, `reservation-mismatch-risk`,
`dangling-compute-class`. Two do not: `spot-scarcity-risk` reads a beta
Spot capacity-advice API (`gcloud beta compute advice capacity-history`)
whose response shape this repository has not exercised anywhere else, and
`autoscaler-out-of-resources` parses `jsonPayload` fields out of a Cloud
Logging query against an internal autoscaler-visibility log schema. Neither
is a case of "closed-form severity rule" (design §2) the way the other ten
are — encoding an unverified schema as tested code would make a wrong guess
look like a fact, which is worse than leaving the SOP's own manual
instructions in place. They stay prose-only until someone can confirm the
real response shape against a live cluster.

`dangling-compute-class` (3.12) is one of the ten, but only three of its
four sub-conditions are covered: (a) a dangling `nodeSelector` reference,
(c) `nodePoolAutoCreation.enabled: false` with no matching pool label, and
(d) a GPU workload missing its toleration. 3.12(b) — a ComputeClass whose
own `status.conditions` reports invalid configuration — is not: this
repository has not exercised that CRD's condition `type`/`reason` values
anywhere else, the same reason `spot-scarcity-risk` and
`autoscaler-out-of-resources` stay prose-only. Check 3.12(b) by hand
alongside 3.8 and 3.11.

The ComputeClass field names and family-generation lists below (Gen 2 vs
Gen 4/Hyperdisk-compatible in `ccc-mixed-disk-generations`, the
Hyperdisk-incompatible families in `ccc-hyperdisk-incompatible`) are exactly
what `governance/stockout_prevention_sop.md` §3 already specifies — this
collector implements that contract rather than re-deriving one, the same
choice every other converted stream makes when a field's real-world shape
is not independently verifiable from this repository alone.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, NamedTuple

MANIFEST_VERSION = 1
KUBECONFIG_DIR = Path(os.environ.get("HERMES_HOME") or "/opt/data") / ".kubeconfigs"
DEFAULT_TIMEOUT_S = 60
MAX_WORKERS = 8

GEN2_FAMILIES = {"n2", "n2d", "c2"}
GEN4_HYPERDISK_FAMILIES = {"c4", "n4", "c3"}  # §3.5's list, exactly -- §3.6 lists a different, wider set for its own check
HYPERDISK_INCOMPATIBLE_FAMILIES = {"c2", "n2", "e2"}
HYPERDISK_TYPES = {"hyperdisk-balanced", "hyperdisk-throughput", "hyperdisk-extreme"}


def log(msg: str) -> None:
    print(f"[fleet_stockout] {msg}", file=sys.stderr, flush=True)


class Run(NamedTuple):
    argv: list[str]
    rc: int
    stdout: str
    stderr: str
    duration_s: float


RunFn = Callable[..., Run]


def default_run(argv: list[str], *, env: dict | None = None, timeout: int = DEFAULT_TIMEOUT_S) -> Run:
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout)
        return Run(argv, proc.returncode, proc.stdout, proc.stderr, time.monotonic() - t0)
    except subprocess.TimeoutExpired as exc:
        return Run(argv, 124, exc.stdout or "", exc.stderr or "", time.monotonic() - t0)
    except Exception as exc:
        return Run(argv, -1, "", str(exc), time.monotonic() - t0)


def run_and_gate(argv: list[str], *, run: RunFn, env: dict | None = None) -> tuple[object | None, Run]:
    result = run(argv, env=env)
    if result.rc != 0 or not result.stdout.strip():
        return None, result
    try:
        return json.loads(result.stdout), result
    except json.JSONDecodeError:
        return None, result


def output_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record(argv_str: str, result: Run) -> dict:
    return {
        "command": argv_str,
        "rc": result.rc,
        "duration_s": round(result.duration_s, 2),
        "output_sha256": output_digest(result.stdout),
    }


def kubeconfig_path(project: str, cluster: str, location: str) -> Path:
    return KUBECONFIG_DIR / f"kubeconfig_{project}_{cluster}_{location}.yaml"


def fetch_credentials(project: str, cluster: str, location: str, *, run: RunFn) -> tuple[Path, Run]:
    kc = kubeconfig_path(project, cluster, location)
    kc.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "KUBECONFIG": str(kc)}
    result = run(
        ["gcloud", "container", "clusters", "get-credentials", cluster, "--location", location, "--project", project],
        env=env,
    )
    return kc, result


def enumerate_clusters(project: str, *, run: RunFn) -> list[dict]:
    result = run(
        [
            "gcloud", "container", "clusters", "list", "--project", project,
            "--format", "json(name,location,status,autopilot.enabled,autoscaling.enableNodeAutoprovisioning)",
        ]
    )
    if result.rc != 0:
        raise RuntimeError(f"cluster enumeration failed (rc={result.rc}): {result.stderr.strip()[:500]}")
    clusters = json.loads(result.stdout or "[]")
    return [
        {
            "name": c["name"],
            "location": c.get("location"),
            "project": project,
            "autopilot": bool((c.get("autopilot") or {}).get("enabled")),
            # A cluster-level setting, not derivable from any one node
            # pool's own autoscaling config -- see `check_single_zone_nodepool`.
            "has_nap": bool((c.get("autoscaling") or {}).get("enableNodeAutoprovisioning")),
        }
        for c in clusters
        if c.get("status") == "RUNNING"
    ]


def region_of(location: str) -> str:
    """A zonal location (`us-central1-a`) truncated to its region
    (`us-central1`); a regional location is returned unchanged."""
    parts = (location or "").rsplit("-", 1)
    return parts[0] if len(parts) == 2 and len(parts[1]) == 1 else location


# --------------------------------------------------------------------------- #
# ComputeClass structural analysis (3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.12)
# --------------------------------------------------------------------------- #


def _priority_is_spot(p: dict) -> bool:
    return bool(p.get("spot")) or p.get("provisioningModel") == "SPOT"


def _priority_family(p: dict) -> str:
    if p.get("machineFamily"):
        return p["machineFamily"]
    mt = p.get("machineType") or ""
    return mt.split("-", 1)[0] if mt else ""


def _priority_size_class(p: dict) -> str:
    """A coarse vCPU-count bucket, used only to detect that priorities vary
    *some* size dimension -- not the precise count §3.3 reasons about."""
    mt = p.get("machineType") or ""
    m = re.search(r"-(\d+)$", mt)
    return m.group(1) if m else ""


def check_ccc_missing_fallbacks(cc: dict) -> dict | None:
    priorities = (cc.get("spec") or {}).get("priorities") or []
    if not priorities:
        return None
    families = {_priority_family(p) for p in priorities if _priority_family(p)}
    spots = {_priority_is_spot(p) for p in priorities}
    sizes = {_priority_size_class(p) for p in priorities if _priority_size_class(p)}
    zones = {tuple(sorted(p.get("zones") or [])) for p in priorities if p.get("zones")}
    dimensions_varied = sum(1 for s in (families, spots, sizes, zones) if len(s) > 1)
    if dimensions_varied >= 2:
        return None
    return {
        "object": f"ComputeClass/{cc['metadata']['name']}",
        "excerpt": f"priorities vary {dimensions_varied}/4 obtainability dimensions (families={sorted(families)}, spot-mix={sorted(spots)})",
    }


def check_ccc_no_ondemand_floor(cc: dict, referenced_by_inference: bool) -> dict | None:
    priorities = (cc.get("spec") or {}).get("priorities") or []
    if not priorities or not all(_priority_is_spot(p) for p in priorities):
        return None
    hit = {"object": f"ComputeClass/{cc['metadata']['name']}", "excerpt": f"{len(priorities)} priorities, all Spot, no On-Demand floor"}
    if referenced_by_inference:
        # §3.2's escalation, reusing ai-security-audit's own discriminator
        # (`collect.py`'s `_is_ai_workload`) rather than a second "which
        # workloads count as inference" rule the two SOPs could drift apart
        # on.
        hit["severity"] = "critical"
        hit["excerpt"] += "; referenced by an inference workload"
    return hit


def check_ccc_large_vm_scarcity(cc: dict) -> list[dict]:
    from fleet_waste import _machine_type_vcpus  # shared with fleet-wide-cost-analysis's own vCPU parser

    priorities = (cc.get("spec") or {}).get("priorities") or []
    families = {_priority_family(p) for p in priorities if _priority_family(p)}
    hits = []
    for p in priorities:
        mt = p.get("machineType") or ""
        vcpus = _machine_type_vcpus(mt)
        if vcpus and vcpus > 32 and len(families) < 2:
            hits.append({"object": f"ComputeClass/{cc['metadata']['name']}", "excerpt": f"priority requests {mt} ({vcpus} vCPU) with only {len(families)} machine famil{'y' if len(families) == 1 else 'ies'} in the chain"})
    return hits


def check_ccc_priority_starvation(cc: dict) -> dict | None:
    priorities = (cc.get("spec") or {}).get("priorities") or []
    if len(priorities) > 10:
        return {"object": f"ComputeClass/{cc['metadata']['name']}", "excerpt": f"{len(priorities)} priority rules (> 10)"}
    return None


def check_ccc_mixed_disk_generations(cc: dict, stateful_referencing: bool) -> dict | None:
    if not stateful_referencing:
        return None
    priorities = (cc.get("spec") or {}).get("priorities") or []
    families = {_priority_family(p) for p in priorities if _priority_family(p)}
    has_gen2 = bool(families & GEN2_FAMILIES)
    has_gen4 = bool(families & GEN4_HYPERDISK_FAMILIES)
    if has_gen2 and has_gen4:
        return {"object": f"ComputeClass/{cc['metadata']['name']}", "excerpt": f"priorities mix Gen 2 ({sorted(families & GEN2_FAMILIES)}) and Gen 4/Hyperdisk ({sorted(families & GEN4_HYPERDISK_FAMILIES)}) families on a stateful, PV-backed workload"}
    return None


def check_ccc_hyperdisk_incompatible(cc: dict, uses_hyperdisk: bool) -> dict | None:
    if not uses_hyperdisk:
        return None
    priorities = (cc.get("spec") or {}).get("priorities") or []
    families = {_priority_family(p) for p in priorities if _priority_family(p)}
    incompatible = families & HYPERDISK_INCOMPATIBLE_FAMILIES
    if incompatible:
        return {"object": f"ComputeClass/{cc['metadata']['name']}", "excerpt": f"fallback includes Hyperdisk-incompatible families {sorted(incompatible)}"}
    return None


def check_dangling_compute_class(workload: dict, compute_classes_by_name: dict[str, dict], node_pool_labels: set[str]) -> dict | None:
    spec = workload.get("spec") or {}
    template_spec = ((spec.get("template") or {}).get("spec")) or spec
    selector = (template_spec.get("nodeSelector") or {}).get("cloud.google.com/compute-class")
    if selector and selector not in compute_classes_by_name:
        return {"object": f"{workload['kind']}/{workload['metadata']['name']}", "excerpt": f"nodeSelector references ComputeClass {selector!r}, which does not exist"}
    if selector:
        cc = compute_classes_by_name[selector]
        auto_create = ((cc.get("spec") or {}).get("nodePoolAutoCreation") or {}).get("enabled")
        if auto_create is False and node_pool_labels and selector not in node_pool_labels:
            return {"object": f"{workload['kind']}/{workload['metadata']['name']}", "excerpt": f"references ComputeClass {selector!r} with nodePoolAutoCreation disabled and no matching node pool label/taint"}
    if selector:
        requests_gpu = any("nvidia.com/gpu" in ((c.get("resources") or {}).get("requests") or {}) for c in template_spec.get("containers") or [])
        tolerates_gpu = any(t.get("key") == "nvidia.com/gpu" for t in template_spec.get("tolerations") or [])
        if requests_gpu and not tolerates_gpu:
            return {"object": f"{workload['kind']}/{workload['metadata']['name']}", "excerpt": f"GPU workload references ComputeClass {selector!r} without an nvidia.com/gpu toleration"}
    return None


# --------------------------------------------------------------------------- #
# 3.9 single-zone-nodepool
# --------------------------------------------------------------------------- #


def check_single_zone_nodepool(pool: dict, has_nap: bool, current_node_count: int) -> dict | None:
    """`current_node_count` must be the pool's *live* node count (counted
    from the cluster's own `Node` objects, grouped by the
    `cloud.google.com/gke-nodepool` label) -- `initialNodeCount` is a
    creation-time field the GKE API never updates as the autoscaler scales
    the pool, so it cannot stand in for "how close to `maxNodeCount` is this
    pool right now"."""
    locations = pool.get("locations") or []
    autoscaling = pool.get("autoscaling") or {}
    max_nodes = autoscaling.get("maxNodeCount")
    if len(locations) <= 1 and autoscaling.get("enabled") and not has_nap:
        return {"object": f"NodePool/{pool.get('name', '')}", "excerpt": f"single-zone ({locations}), autoscaling enabled, no NAP and no regional multi-zone configuration"}
    if max_nodes and current_node_count >= 0.9 * max_nodes:
        return {"object": f"NodePool/{pool.get('name', '')}", "excerpt": f"{current_node_count}/{max_nodes} live nodes ({current_node_count / max_nodes * 100:.0f}% of maxNodeCount)"}
    return None


# --------------------------------------------------------------------------- #
# 3.10 reservation-mismatch-risk
# --------------------------------------------------------------------------- #


def check_reservation(reservation: dict) -> dict | None:
    specific = reservation.get("specificReservation") or {}
    count, in_use = specific.get("count"), specific.get("inUseCount")
    if count is None or in_use is None or count == 0:
        return None
    ratio = in_use / count
    if ratio <= 0.5 and (count - in_use) >= 4:
        return {
            "object": f"Reservation/{reservation.get('name', '')}",
            "excerpt": f"inUseCount={in_use}/{count} ({ratio * 100:.0f}% used, {count - in_use} idle)",
            "severity": "major",
        }
    return None


def check_reservation_affinity(cc: dict) -> dict | None:
    priorities = (cc.get("spec") or {}).get("priorities") or []
    for p in priorities:
        affinity = ((p.get("reservations") or {}).get("affinity") or "")
        if affinity in ("AnyBestEffort", "Automatic"):
            return {
                "object": f"ComputeClass/{cc['metadata']['name']}",
                "excerpt": f"reservations.affinity={affinity!r} bypasses this ComputeClass's priority chain",
                "severity": "critical",
            }
    return None


# --------------------------------------------------------------------------- #
# 3.7 quota-exhaustion-risk
# --------------------------------------------------------------------------- #


def check_quota(quota: dict) -> dict | None:
    limit, usage = quota.get("limit"), quota.get("usage")
    if not limit:
        return None
    ratio = usage / limit
    if ratio >= 0.9:
        return {"object": f"Quota/{quota.get('metric', '')}", "excerpt": f"{quota.get('metric')}: {usage}/{limit} ({ratio * 100:.0f}%)"}
    return None


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #

IMPACT = {
    "ccc-missing-fallbacks": "Pinned to a single machine family or narrow configuration: any zonal capacity exhaustion causes scale-up to fail and leaves pods unschedulable.",
    "ccc-no-ondemand-floor": "If Spot VM capacity is preempted or exhausted in the region, the workload has no on-demand floor and remains permanently in Pending state.",
    "ccc-large-vm-scarcity": "Very large VM shapes (>32 cores) draw from thin regional capacity pools and are highly prone to sudden stockouts during scale-up.",
    "ccc-priority-starvation": "Excessive priority rules (>10) exceed the autoscaler solver cache limit, triggering backoff loops that starve lower priorities.",
    "ccc-mixed-disk-generations": "Stateful PV workload mixes Gen 2 and Gen 4 machine families, causing volume attachment failures and deadlocks when scaling across nodes.",
    "ccc-hyperdisk-incompatible": "Autoscaler fallback lands on an older machine family that does not support Hyperdisk, causing node provisioning or pod volume attachment to fail.",
    "quota-exhaustion-risk": "Workload resource requests across fleet exceed regional GCP quota limits; Cluster Autoscaler cannot provision additional nodes even if physical capacity exists.",
    "single-zone-nodepool": "Node pool is locked to a single zone or near its scaling ceiling: any zonal stockout or scale event halts cluster auto-scaling.",
    "reservation-mismatch-risk": "ComputeClass fallback priorities are rendered inert by Automatic reservation affinity, or expensive guaranteed reservation capacity sits idle during stockouts.",
    "dangling-compute-class": "Workload cannot be scheduled due to dangling class references, invalid CRD configuration, or missing node tolerations, causing permanent Pending state.",
}
SEVERITY = {
    "ccc-missing-fallbacks": "critical",
    "ccc-no-ondemand-floor": "major",  # overridden to critical when referenced by an inference workload
    "ccc-large-vm-scarcity": "major",
    "ccc-priority-starvation": "critical",
    "ccc-mixed-disk-generations": "critical",
    "ccc-hyperdisk-incompatible": "critical",
    "quota-exhaustion-risk": "critical",
    "single-zone-nodepool": "major",
    "reservation-mismatch-risk": "major",  # overridden to critical for a broken/bypassed binding
    "dangling-compute-class": "critical",
}


def _emit(slug: str, hit: dict) -> dict:
    return {
        "check": slug,
        "namespace": hit.get("namespace", ""),
        "object": hit["object"],
        "severity": hit.get("severity") or SEVERITY[slug],
        "excerpt": hit["excerpt"],
        "impact": IMPACT[slug],
        "needs_triage": None,
    }


def collect_cluster(cluster: dict, *, run: RunFn) -> dict:
    name, project, location = cluster["name"], cluster["project"], cluster["location"]
    kubeconfig, cred_run = fetch_credentials(project, name, location, run=run)
    if cred_run.rc != 0:
        return {"name": name, "project": project, "location": location, "outcome": "unreachable", "error": f"get-credentials rc={cred_run.rc}: {cred_run.stderr.strip()[:300]}"}

    env = {**os.environ, "KUBECONFIG": str(kubeconfig)}
    dump_argv = ["kubectl", "get", "computeclasses,deployments,statefulsets,storageclasses,nodes", "-A", "-o", "json"]
    parsed, result = run_and_gate(dump_argv, run=run, env=env)
    if parsed is None:
        return {"name": name, "project": project, "location": location, "outcome": "gate-failed", "error": f"object dump gate failed (rc={result.rc}): {result.stderr.strip()[:300]}"}
    dump_record = _record(f"KUBECONFIG={kubeconfig} {' '.join(dump_argv)}", result)

    items = parsed.get("items", [])
    compute_classes = [i for i in items if i.get("kind") == "ComputeClass"]
    deployments = [i for i in items if i.get("kind") == "Deployment"]
    statefulsets = [i for i in items if i.get("kind") == "StatefulSet"]
    storage_classes = {i["metadata"]["name"]: i for i in items if i.get("kind") == "StorageClass"}
    workloads = deployments + statefulsets
    compute_classes_by_name = {cc["metadata"]["name"]: cc for cc in compute_classes}

    # §3.9's ">= 90% of maxNodeCount" test needs the pool's *live* node
    # count, which the GKE `NodePool` resource itself never exposes --
    # `initialNodeCount` is creation-time and the autoscaler never updates
    # it. Count live `Node` objects by the same nodepool label
    # `fleet_waste.py`'s idle-nodepool check already groups by.
    live_node_count_by_pool: dict[str, int] = {}
    for node in (i for i in items if i.get("kind") == "Node"):
        pool = (node.get("metadata", {}).get("labels") or {}).get("cloud.google.com/gke-nodepool", "")
        live_node_count_by_pool[pool] = live_node_count_by_pool.get(pool, 0) + 1

    autopilot = bool(cluster.get("autopilot"))
    # Not attempted on Autopilot: `node-pools list` answers HTTP 400 there
    # ("Autopilot node pools cannot be accessed or modified"), so the record
    # would be a guaranteed failure for a read whose only consumers -- §3.9's
    # single-zone-nodepool and the node pool labels below -- cannot apply to a
    # cluster with no user node pools anyway.
    #
    # `pools_readable` rather than testing `node_pools` for emptiness. A read
    # that failed and a Standard cluster that genuinely holds no pools both
    # produced `[]`, and the check downstream was skipped for either -- so a
    # denied read silently dropped `single-zone-nodepool` from the manifest
    # with nothing to say it had been attempted, which §6 reads as a check
    # nobody ran and cannot explain. They are different facts and they now
    # take different paths.
    node_pools: list[dict] = []
    pools_result = None
    pools_record: dict | None = None
    pools_readable = False
    if not autopilot:
        node_pools_argv = ["gcloud", "container", "node-pools", "list", "--cluster", name, "--location", location, "--project", project, "--format", "json"]
        pools_result = run(node_pools_argv)
        pools_readable = pools_result.rc == 0
        node_pools = json.loads(pools_result.stdout) if pools_readable and pools_result.stdout.strip() else []
        pools_record = _record(" ".join(node_pools_argv), pools_result)
    has_nap = bool(cluster.get("has_nap"))
    # ComputeClass-managed pools carry the class name as this label, not as
    # their own pool name -- matching against pool names would test the
    # wrong field and never actually find the reference.
    node_pool_labels = {
        v for p in node_pools for v in [((p.get("config") or {}).get("labels") or {}).get("cloud.google.com/compute-class")] if v
    }

    candidates: list[dict] = []
    commands: dict[str, dict] = {}

    stateful_names_using_hyperdisk = set()
    for sts in statefulsets:
        for vct in sts.get("spec", {}).get("volumeClaimTemplates", []) or []:
            sc_name = (vct.get("spec") or {}).get("storageClassName")
            provisioner = (storage_classes.get(sc_name) or {}).get("provisioner", "")
            params = (storage_classes.get(sc_name) or {}).get("parameters", {}) or {}
            if params.get("type") in HYPERDISK_TYPES or "hyperdisk" in provisioner.lower():
                stateful_names_using_hyperdisk.add((sts["metadata"].get("namespace", ""), sts["metadata"]["name"]))

    cc_referenced_by_stateful = set()
    cc_referenced_by_hyperdisk = set()
    for sts in statefulsets:
        cc_ref = ((sts.get("spec", {}).get("template", {}).get("spec", {}) or {}).get("nodeSelector") or {}).get("cloud.google.com/compute-class")
        if not cc_ref:
            continue
        # §3.5 flags "a stateful workload *using PersistentVolumes*" -- a
        # StatefulSet with no `volumeClaimTemplates` has nothing that can
        # deadlock on a machine-family mismatch, so it does not count here
        # even though it references the ComputeClass.
        if sts.get("spec", {}).get("volumeClaimTemplates"):
            cc_referenced_by_stateful.add(cc_ref)
        if (sts["metadata"].get("namespace", ""), sts["metadata"]["name"]) in stateful_names_using_hyperdisk:
            cc_referenced_by_hyperdisk.add(cc_ref)

    from collect import _is_ai_workload  # ai-security-audit's own inference-workload discriminator, per §3.2

    cc_referenced_by_inference = set()
    for workload in workloads:
        spec = workload.get("spec") or {}
        template_spec = ((spec.get("template") or {}).get("spec")) or spec
        cc_ref = (template_spec.get("nodeSelector") or {}).get("cloud.google.com/compute-class")
        if cc_ref and _is_ai_workload(template_spec):
            cc_referenced_by_inference.add(cc_ref)

    if compute_classes:
        for cc_slug in ("ccc-missing-fallbacks", "ccc-no-ondemand-floor", "ccc-large-vm-scarcity", "ccc-priority-starvation"):
            commands[cc_slug] = dump_record
        for cc in compute_classes:
            for hit in [check_ccc_missing_fallbacks(cc)]:
                if hit:
                    candidates.append(_emit("ccc-missing-fallbacks", hit))
            for hit in [check_ccc_no_ondemand_floor(cc, cc["metadata"]["name"] in cc_referenced_by_inference)]:
                if hit:
                    candidates.append(_emit("ccc-no-ondemand-floor", hit))
            candidates += [_emit("ccc-large-vm-scarcity", hit) for hit in check_ccc_large_vm_scarcity(cc)]
            for hit in [check_ccc_priority_starvation(cc)]:
                if hit:
                    candidates.append(_emit("ccc-priority-starvation", hit))

        if statefulsets:
            commands["ccc-mixed-disk-generations"] = dump_record
            for cc in compute_classes:
                stateful_referencing = cc["metadata"]["name"] in cc_referenced_by_stateful
                for hit in [check_ccc_mixed_disk_generations(cc, stateful_referencing)]:
                    if hit:
                        candidates.append(_emit("ccc-mixed-disk-generations", hit))
            commands["ccc-hyperdisk-incompatible"] = dump_record
            for cc in compute_classes:
                uses_hyperdisk = cc["metadata"]["name"] in cc_referenced_by_hyperdisk
                for hit in [check_ccc_hyperdisk_incompatible(cc, uses_hyperdisk)]:
                    if hit:
                        candidates.append(_emit("ccc-hyperdisk-incompatible", hit))

        commands["reservation-mismatch-risk"] = dump_record
        for cc in compute_classes:
            for hit in [check_reservation_affinity(cc)]:
                if hit:
                    candidates.append(_emit("reservation-mismatch-risk", hit))

    commands["dangling-compute-class"] = dump_record
    for workload in workloads:
        for hit in [check_dangling_compute_class(workload, compute_classes_by_name, node_pool_labels)]:
            if hit:
                candidates.append(_emit("dangling-compute-class", hit))

    not_applicable: list[dict] = []
    limitations: list[str] = []
    if autopilot:
        # Declared by the collector rather than left to the model, for the
        # reason cross_check_manifest's note on `checks_not_applicable` gives:
        # until every collector says which checks it skipped and why, nothing
        # can adjudicate the field, and whether a run tells the truth about it
        # comes down to how well the model happens to know GKE. It is the same
        # disposition on every run, and `autopilot` is a fact already in hand.
        not_applicable.append(
            {
                "check": "single-zone-nodepool",
                "reason": (
                    "GKE Autopilot: Google places the nodes and exposes no user "
                    "node pool whose locations could be a single zone."
                ),
            }
        )
    elif pools_readable:
        # Recorded even when the cluster holds no pools. Zero pools is a real
        # answer to "is any pool single-zone" -- no -- and dropping the check
        # for it makes an empty Standard cluster indistinguishable from one
        # whose pools nobody looked at.
        commands["single-zone-nodepool"] = pools_record
        for pool in node_pools:
            live_count = live_node_count_by_pool.get(pool.get("name", ""), 0)
            for hit in [check_single_zone_nodepool(pool, has_nap, live_count)]:
                if hit:
                    candidates.append(_emit("single-zone-nodepool", hit))
    else:
        limitations.append(
            f"single-zone-nodepool could not be measured on this cluster: "
            f"`gcloud container node-pools list` failed (rc={pools_result.rc}) — "
            f"{pools_result.stderr.strip()[:200] or 'no stderr'}. The same failure "
            f"left dangling-compute-class without node pool labels, so its "
            f"nodePoolAutoCreation arm did not run either"
        )

    entry = {
        "name": name, "project": project, "location": location,
        "outcome": "collected",
        "commands": [{"check": slug, **record} for slug, record in commands.items()],
        "candidates": candidates,
    }
    if not_applicable:
        entry["checks_not_applicable"] = not_applicable
    if limitations:
        entry["limitations"] = "; ".join(limitations)
    return entry


def collect_project(project: str, cluster_regions: set[str], *, run: RunFn) -> dict | None:
    res_argv = ["gcloud", "compute", "reservations", "list", "--project", project, "--format", "json"]
    reservations, res_result = run_and_gate(res_argv, run=run)

    quota_records: dict[str, dict] = {}
    quota_candidates: list[dict] = []
    for region in cluster_regions:
        q_argv = ["gcloud", "compute", "regions", "describe", region, "--project", project, "--format", "json(quotas)"]
        parsed, result = run_and_gate(q_argv, run=run)
        if parsed is None:
            continue
        quota_records[region] = _record(" ".join(q_argv), result)
        for quota in parsed.get("quotas") or []:
            for hit in [check_quota(quota)]:
                if hit:
                    hit["excerpt"] = f"{region}: {hit['excerpt']}"
                    quota_candidates.append(_emit("quota-exhaustion-risk", hit))

    if reservations is None and not quota_records:
        return None

    commands = []
    candidates = []
    if reservations is not None:
        commands.append({"check": "reservation-mismatch-risk", **_record(" ".join(res_argv), res_result)})
        for reservation in reservations:
            for hit in [check_reservation(reservation)]:
                if hit:
                    candidates.append(_emit("reservation-mismatch-risk", hit))
    if quota_records:
        commands.append({"check": "quota-exhaustion-risk", **next(iter(quota_records.values()))})
        candidates.extend(quota_candidates)

    return {
        "name": f"project/{project}",
        "project": project,
        "location": "global",
        "outcome": "collected",
        "commands": commands,
        "candidates": candidates,
    }


def collect_fleet(project: str | None = None, *, run: RunFn = default_run, max_workers: int = MAX_WORKERS) -> dict:
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    resolved_project = project
    if not resolved_project:
        result = run(["gcloud", "config", "get-value", "project"])
        resolved_project = result.stdout.strip() if result.rc == 0 else ""

    clusters = enumerate_clusters(resolved_project, run=run)
    cluster_entries = [None] * len(clusters)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(collect_cluster, c, run=run): i for i, c in enumerate(clusters)}
        for future in as_completed(futures):
            cluster_entries[futures[future]] = future.result()

    regions = {region_of(c["location"]) for c in clusters if c.get("location")}
    project_entry = collect_project(resolved_project, regions, run=run)

    return {
        "version": MANIFEST_VERSION,
        "audit": "stockout-prevention",
        "started_at": started_at,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "clusters": [e for e in cluster_entries if e] + ([project_entry] if project_entry else []),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", help="project to audit; omit to use the active gcloud project")
    args = parser.parse_args(argv)
    manifest = collect_fleet(args.project)
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
