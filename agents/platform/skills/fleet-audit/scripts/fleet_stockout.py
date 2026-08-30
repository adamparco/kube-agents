#!/opt/hermes/.venv/bin/python3
"""fleet_stockout.py — Procedural collector for the Fleet Stockout
Prevention & Capacity Audit (`stockout-prevention`).

See docs/designs/fleet-audit-collectors-and-status.md §4.2, §10 phase 4, and
governance/stockout_prevention_sop.md.

**All twelve checks are converted.** Ten read structures this repository's
other collectors already read with confidence — a `ComputeClass`/`Deployment`/
`StatefulSet`/`StorageClass`/`Node` dump, `gcloud container node-pools list`,
`gcloud compute reservations list`, `gcloud compute regions describe
--format=json(quotas)`: `ccc-missing-fallbacks`, `ccc-no-ondemand-floor`,
`ccc-large-vm-scarcity`, `ccc-priority-starvation`,
`ccc-mixed-disk-generations`, `ccc-hyperdisk-incompatible`,
`quota-exhaustion-risk`, `single-zone-nodepool`, `reservation-mismatch-risk`,
`dangling-compute-class`.

The other two — `spot-scarcity-risk`, off the beta Spot capacity-advice API
(`gcloud beta compute advice capacity-history`), and
`autoscaler-out-of-resources`, off a Cloud Logging query against the
`cluster-autoscaler-visibility` schema — were prose-only for one reason: this
repository had not exercised either response shape anywhere else, and encoding
an unverified schema as tested code makes a wrong guess look like a fact. Both
shapes were read live against `adamparco-kage` on 2026-08-29 and are now
pinned by `test_fleet_stockout.py` against captured responses:

- `capacity-history` returns `{location, machineType, preemptionHistory:
  [{interval: {startTime, endTime}, preemptionRate: <float>}], priceHistory:
  [{interval, listPrice: {currencyCode, nanos, units?}}]}`. `preemptionRate`
  is a fraction, one entry per daily interval, 28 of them on a 30-day window.
  `listPrice.units` is absent below one currency unit, which is why
  `spot_list_price` reads both halves rather than `units` alone.
- `cluster-autoscaler-visibility` writes a stockout under *two* schemas, and a
  filter reading one silently passes a cluster failing under the other.
  `jsonPayload.resultInfo.results[].errorMsg.{messageId, parameters[]}` is a
  scale-up that was attempted and failed — the live sample carried
  `scale.up.error.out.of.resources` with the affected instance group in
  `parameters[0]`. `jsonPayload.noDecisionStatus.noScaleUp
  .unhandledPodGroups[].napFailureReasons[].messageId` is the
  node-auto-provisioning side, which never gets as far as an attempt. Healthy
  ticks carry neither and write `jsonPayload.status` instead.

Two sub-conditions are still uncovered, both for the reason the two checks
above used to have — this repository has not exercised the shape anywhere, and
a guess encoded as tested code looks like a fact. Check both by hand:

- **3.10(b)**, a `ComputeClass` targeting a reservation that does not exist or
  sits in an unreachable zone. `check_reservation_affinity` covers 3.10(a) and
  `check_reservation` covers 3.10(c); resolving a named reservation against the
  zones a cluster can actually reach is the part nothing here does.
- **3.12(b)**, a ComputeClass whose own `status.conditions` reports invalid
  configuration. `check_dangling_compute_class` covers 3.12(a), (c) and (d);
  that CRD's condition `type`/`reason` values are the unexercised shape.

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
import shlex
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

# §3.11. The three message ids that mean a scale-up failed for want of
# capacity, quota or pod IPs. Every other id the autoscaler emits is a
# scheduling decision rather than a stockout -- `scale.up.error.waiting.for
# .instances.timeout` and the `no.scale.up.in.backoff` family are the common
# ones -- and matching them would turn an ordinary busy cluster critical.
AUTOSCALER_LOG_ID = "container.googleapis.com/cluster-autoscaler-visibility"
AUTOSCALER_FRESHNESS = "24h"
AUTOSCALER_LOG_LIMIT = 1000
AUTOSCALER_STOCKOUT_MESSAGE_IDS = {
    "scale.up.error.out.of.resources",
    "scale.up.error.quota.exceeded",
    "scale.up.error.ip.space.exhausted",
}

# §3.8's ">20%", as a fraction, against the mean of the daily preemption
# rates the API returns.
SPOT_PREEMPTION_CEILING = 0.20
# Below this many daily intervals the mean is one bad day wide. A shape with
# fewer is reported as unmeasured rather than clean: a new machine type in a
# new region is exactly where a stockout hides, and it is also exactly where
# the history is too short to prove one.
SPOT_MIN_INTERVALS = 7
# Per cluster. A ComputeClass chain can name a dozen shapes and each is its own
# API round trip; past this the rest are named in `limitations` rather than
# read, because a collector that quietly stops looking is the failure this
# whole stream reports on.
SPOT_MAX_SHAPES = 8


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


def not_running_entry(c: dict, project: str) -> dict:
    """A manifest target for a cluster whose state rules out auditing it.

    Filtering `clusters list` down to RUNNING is right -- a PROVISIONING
    cluster has no API server to read. Dropping the rest without a trace is
    not: the manifest is the run's only account of the fleet it saw, so a
    cluster absent from it reads exactly like a cluster that does not exist,
    and the document can publish a fleet-wide all-clear over a fleet quietly
    missing it. DEGRADED is the case that makes this bite. Recorded as a
    non-`collected` target, the loss is something the document has to place in
    `scope.skipped` with a reason. `collect.py` carries the same helper.
    """
    return {
        "name": c.get("name", ""),
        "project": project,
        "location": c.get("location") or c.get("zone") or "",
        "outcome": "unreachable",
        "error": f"cluster status is {c.get('status') or 'unknown'}, not RUNNING; no check was evaluated against it",
    }


def enumerate_clusters(project: str, *, run: RunFn) -> tuple[list[dict], list[dict]]:
    result = run(
        [
            "gcloud", "container", "clusters", "list", "--project", project,
            "--format", "json(name,location,status,autopilot.enabled,autoscaling.enableNodeAutoprovisioning)",
        ]
    )
    if result.rc != 0:
        raise RuntimeError(f"cluster enumeration failed (rc={result.rc}): {result.stderr.strip()[:500]}")
    clusters = json.loads(result.stdout or "[]")
    running = [
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
    return running, [not_running_entry(c, project) for c in clusters if c.get("status") != "RUNNING"]


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


def _priority_is_pod_family(p: dict) -> bool:
    """Does this priority delegate the machine shape to GKE rather than name one?

    An Autopilot-mode ComputeClass (`spec.autopilot.enabled`) writes its chain as
    `podFamily: general-purpose` instead of a `machineFamily`/`machineType`. GKE's
    own capacity broker then chooses the shape at scale-up time, across families
    and zones.
    """
    return bool(p.get("podFamily")) and not p.get("machineFamily") and not p.get("machineType")


def check_ccc_missing_fallbacks(cc: dict) -> dict | None:
    priorities = (cc.get("spec") or {}).get("priorities") or []
    if not priorities:
        return None
    # §3.1 flags a chain "pinned to a single machine family". A pod-family
    # priority is pinned to no machine family at all -- it hands the choice to
    # GKE, which is the broadest fallback the API can express, so there is
    # nothing here to flag.
    #
    # Scoring the dimensions anyway is what made this the fleet's loudest false
    # positive. `_priority_family` reads only `machineFamily`/`machineType`, so
    # every pod-family entry returned "", `families` came back *empty*, and an
    # unreadable chain scored the same 0/4 as a genuinely pinned one. That fired
    # `critical` against `autopilot`, `autopilot-arm` and `autopilot-spot` -- the
    # classes GKE pre-installs and reconciles on every Autopilot cluster -- so
    # each cluster contributed three findings whose own remediation had to
    # conclude `kind: manual`, because a GKE-managed object has no manifest in
    # the GitOps clone to write to. 49 of one run's 67 findings were this.
    #
    # `all()`, not `any()`: a chain mixing pod-family and machine-typed entries
    # was hand-authored, its machine-typed entries are real pins, and a finding
    # against it is something an operator can act on.
    if all(_priority_is_pod_family(p) for p in priorities):
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
    # The guard `check_ccc_missing_fallbacks` carries, for the reason §3.1
    # already gives and §3.2 omits: `autopilot-spot` is one of the three classes
    # GKE pre-installs and reconciles on every Autopilot cluster, its whole
    # chain is the single priority `{podFamily: general-purpose, spot: true}`,
    # and it is GKE-managed, so there is no manifest in the GitOps clone to
    # append an On-Demand rule to. §3.2's remediation is `kind: manifest`; on
    # this object it cannot be written, which is precisely why §3.1 excludes the
    # same three. Being all-Spot is what that class *is*, not a way someone
    # misconfigured it, and the finding's own recommendation conceded as much by
    # telling the reader to go and declare a different class instead. It fired
    # once per Autopilot cluster on every run -- 17 of one run's 18 findings.
    #
    # Not suppressed when an inference workload actually selects it. There the
    # risk is real and immediate in a way the §3.2 escalation already grades
    # `critical`, and a manual finding beats silence even though the object
    # still has no manifest. Nothing on this fleet selects any ComputeClass at
    # all, so today this branch costs the ledger nothing and saves it 17.
    if all(_priority_is_pod_family(p) for p in priorities) and not referenced_by_inference:
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


def check_dangling_compute_class(workload: dict, compute_classes_by_name: dict[str, dict], node_pool_labels: set[str] | None) -> dict | None:
    """`node_pool_labels` is `None` when the pool labels are unknown -- an
    Autopilot cluster with no user node pools, or a `node-pools list` the
    caller could not read -- and a set, possibly empty, when they are known.

    The second arm used to test the set for truthiness, which silently
    conflated the two: a Standard cluster whose pools carry no
    `cloud.google.com/compute-class` label at all is the arm's own target case,
    and an empty set turned it off there. Only `None` turns it off now, and
    §3.9's `limitations` sentence is what says the read failed.
    """
    spec = workload.get("spec") or {}
    template_spec = ((spec.get("template") or {}).get("spec")) or spec
    selector = (template_spec.get("nodeSelector") or {}).get("cloud.google.com/compute-class")
    if selector and selector not in compute_classes_by_name:
        return {"object": f"{workload['kind']}/{workload['metadata']['name']}", "excerpt": f"nodeSelector references ComputeClass {selector!r}, which does not exist"}
    if selector:
        cc = compute_classes_by_name[selector]
        auto_create = ((cc.get("spec") or {}).get("nodePoolAutoCreation") or {}).get("enabled")
        if auto_create is False and node_pool_labels is not None and selector not in node_pool_labels:
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
# §3.8 Spot capacity advice, §3.11 autoscaler visibility
# --------------------------------------------------------------------------- #


def autoscaler_message_ids(entries: object) -> dict[str, dict]:
    """§3.11's stockout message ids out of a `cluster-autoscaler-visibility` read.

    Both schemas, keyed by id, each carrying how many entries named it, the
    window it spanned, and whatever `parameters` the autoscaler attached — for
    `scale.up.error.out.of.resources` that is the instance group it could not
    grow, which is the one thing a reader needs to act on.
    """
    found: dict[str, dict] = {}
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        payload = entry.get("jsonPayload") or {}
        errors = [
            result.get("errorMsg") or {}
            for result in ((payload.get("resultInfo") or {}).get("results") or [])
            if isinstance(result, dict)
        ]
        no_scale_up = (payload.get("noDecisionStatus") or {}).get("noScaleUp") or {}
        for group in no_scale_up.get("unhandledPodGroups") or []:
            if isinstance(group, dict):
                errors += [
                    reason
                    for reason in (group.get("napFailureReasons") or [])
                    if isinstance(reason, dict)
                ]
        stamp = str(entry.get("timestamp") or "")
        for err in errors:
            message_id = err.get("messageId")
            if message_id not in AUTOSCALER_STOCKOUT_MESSAGE_IDS:
                continue
            slot = found.setdefault(
                message_id, {"count": 0, "parameters": [], "first_seen": "", "last_seen": ""}
            )
            slot["count"] += 1
            for param in err.get("parameters") or []:
                if str(param) not in slot["parameters"]:
                    slot["parameters"].append(str(param))
            if stamp:
                slot["first_seen"] = min(slot["first_seen"] or stamp, stamp)
                slot["last_seen"] = max(slot["last_seen"], stamp)
    return found


def check_autoscaler_out_of_resources(message_ids: dict[str, dict], cluster: str) -> list[dict]:
    """One finding per distinct message id, not per log entry.

    A cluster wedged against a regional stockout emits the same id every
    autoscaler tick, and the SOP's remediation branches on the id rather than
    on the occurrence — three hundred findings saying `out.of.resources` are
    one problem written three hundred times.
    """
    hits = []
    for message_id in sorted(message_ids):
        seen = message_ids[message_id]
        where = (
            seen["parameters"][0].rsplit("/", 1)[-1]
            if seen["parameters"]
            else "no instance group named"
        )
        # The window comes from the entries themselves rather than from
        # `AUTOSCALER_FRESHNESS`: this function does not issue the read and
        # cannot know what window it asked for, and a hardcoded "over the last
        # 24h" beside timestamps that say otherwise is worse than no claim.
        window = (
            f", {seen['first_seen'][:19]} .. {seen['last_seen'][:19]}"
            if seen["first_seen"]
            else ""
        )
        hits.append(
            {
                "object": f"Cluster/{cluster}",
                "excerpt": (
                    f"{message_id}, {seen['count']} occurrence"
                    f"{'' if seen['count'] == 1 else 's'} in the autoscaler "
                    f"visibility log{window}; first affected: {where}"
                ),
            }
        )
    return hits


def spot_shapes(compute_classes: list[dict], node_pools: list[dict]) -> dict[str, dict]:
    """The concrete Spot machine types this cluster asks for, and who asks.

    `capacity-history` takes one `--machine-type` and nothing coarser, so a
    priority naming only `machineFamily` has no shape to query — those are
    counted here and reported as unmeasured rather than dropped.

    `families` is what §3.8's "without alternative family fallbacks" tests. A
    node pool has no fallback chain at all, so it carries 1 by construction:
    when its shape runs out, nothing else is tried.
    """
    shapes: dict[str, dict] = {}

    def add(machine_type: str, owner: str, families: int) -> None:
        slot = shapes.setdefault(machine_type, {"owners": [], "families": families})
        if owner not in slot["owners"]:
            slot["owners"].append(owner)
        slot["families"] = max(slot["families"], families)

    for cc in compute_classes:
        priorities = (cc.get("spec") or {}).get("priorities") or []
        families = len({_priority_family(p) for p in priorities if _priority_family(p)})
        owner = f"ComputeClass/{cc.get('metadata', {}).get('name', '')}"
        for priority in priorities:
            if _priority_is_spot(priority) and priority.get("machineType"):
                add(str(priority["machineType"]), owner, families)
    for pool in node_pools:
        config = pool.get("config") or {}
        if config.get("spot") and config.get("machineType"):
            add(str(config["machineType"]), f"NodePool/{pool.get('name', '')}", 1)
    return shapes


def spot_without_a_shape(
    compute_classes: list[dict], node_pools: list[dict]
) -> tuple[list[str], list[str]]:
    """Spot requests `capacity-history` cannot be asked about, split by why.

    `(unqueryable, unpinned)`, and the split is the whole point. A priority
    naming a machine family but no machine type is a real gap: the shape exists,
    the API takes one `--machine-type` and has no way to be asked about a
    family. A priority pinning *neither* — GKE's own `autopilot-spot`, which
    ships on every Autopilot cluster — has no shape to be scarce, because every
    family is available to it; §3.8's "without alternative family fallbacks"
    cannot be true of it by construction.

    One belongs in `limitations` and the other in `checks_not_applicable`, and
    reporting both as "this cluster does not use Spot" was wrong about both.
    """
    unqueryable, unpinned = [], []
    for cc in compute_classes:
        name = cc.get("metadata", {}).get("name", "")
        for priority in (cc.get("spec") or {}).get("priorities") or []:
            if not _priority_is_spot(priority) or priority.get("machineType"):
                continue
            family = _priority_family(priority)
            bucket, label = (unqueryable, f"{name}:{family}") if family else (unpinned, f"ComputeClass/{name}")
            if label not in bucket:
                bucket.append(label)
    for pool in node_pools:
        config = pool.get("config") or {}
        if config.get("spot") and not config.get("machineType"):
            unqueryable.append(f"NodePool/{pool.get('name', '')}")
    return unqueryable, unpinned


def mean_preemption_rate(advice: object) -> tuple[float | None, int]:
    """The mean daily preemption rate and how many intervals it averages.

    The mean rather than the maximum, deliberately. §3.8 is about a shape being
    a bad bet, and one 60% afternoon inside a month of 5% is a zonal incident
    that already resolved — flagging on the peak turns every shape in the fleet
    critical after any bad day.
    """
    history = (advice or {}).get("preemptionHistory") or [] if isinstance(advice, dict) else []
    rates = [
        float(entry["preemptionRate"])
        for entry in history
        if isinstance(entry, dict) and isinstance(entry.get("preemptionRate"), (int, float))
    ]
    if not rates:
        return None, 0
    return sum(rates) / len(rates), len(rates)


def spot_list_price(advice: object) -> str:
    """The most recent list price, as a display string, or `""`.

    `units` is absent below one currency unit and `nanos` is absent above a
    whole one, so both halves are read. Context for the excerpt only — no
    check branches on it.
    """
    history = (advice or {}).get("priceHistory") or [] if isinstance(advice, dict) else []
    prices = [entry for entry in history if isinstance(entry, dict) and entry.get("listPrice")]
    if not prices:
        return ""
    price = prices[-1]["listPrice"]
    amount = float(price.get("units") or 0) + float(price.get("nanos") or 0) / 1e9
    return f"{amount:.4f} {price.get('currencyCode') or ''}".strip()


def check_spot_scarcity(
    machine_type: str, shape: dict, region: str, advice: object
) -> tuple[dict | None, str | None]:
    """§3.8 for one shape: `(finding, limitation)`, at most one of them set.

    The SOP's two halves are both required — a preemption rate above the
    ceiling *and* no alternative family to fall back to. A chain that already
    spans two families survives its worst shape being preempted, which is
    exactly what "Do NOT flag: comprehensive multi-family fallbacks" means.
    """
    rate, intervals = mean_preemption_rate(advice)
    owners = ", ".join(shape["owners"])
    if rate is None:
        return None, (
            f"spot-scarcity-risk could not be measured for {machine_type} in "
            f"{region}: capacity-history returned no preemptionHistory "
            f"(requested by {owners})"
        )
    if intervals < SPOT_MIN_INTERVALS:
        return None, (
            f"spot-scarcity-risk for {machine_type} in {region} rests on "
            f"{intervals} daily interval(s), under the {SPOT_MIN_INTERVALS} this "
            f"check needs to mean anything (requested by {owners})"
        )
    if rate <= SPOT_PREEMPTION_CEILING or shape["families"] >= 2:
        return None, None
    price = spot_list_price(advice)
    return {
        "object": owners.split(", ")[0],
        "excerpt": (
            f"Spot {machine_type} in {region} preempted at a mean "
            f"{rate * 100:.1f}% per day over {intervals} days (ceiling "
            f"{SPOT_PREEMPTION_CEILING * 100:.0f}%), and the requesting chain "
            f"names {shape['families']} machine famil"
            f"{'y' if shape['families'] == 1 else 'ies'} — no alternative to fall "
            f"back to" + (f"; list price {price}/h" if price else "")
        ),
    }, None


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
    "spot-scarcity-risk": "Spot machine shapes have high historical preemption rates and severe obtainability constraints, putting workload uptime at extreme risk.",
    "autoscaler-out-of-resources": "Autoscaler has actively failed scale-up attempts due to physical cloud stockouts, quota exhaustion, or pod subnet IP exhaustion.",
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
    "spot-scarcity-risk": "major",
    "autoscaler-out-of-resources": "critical",
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
    dump_record = _record(f"KUBECONFIG={kubeconfig} {shlex.join(dump_argv)}", result)

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
        pools_record = _record(shlex.join(node_pools_argv), pools_result)
    has_nap = bool(cluster.get("has_nap"))
    # ComputeClass-managed pools carry the class name as this label, not as
    # their own pool name -- matching against pool names would test the
    # wrong field and never actually find the reference.
    node_pool_labels = (
        {v for p in node_pools for v in [((p.get("config") or {}).get("labels") or {}).get("cloud.google.com/compute-class")] if v}
        if pools_readable
        else None
    )

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

    # §3.11. One read per cluster, and it is recorded whether or not it found
    # anything: "the autoscaler logged no stockout in 24h" is the answer this
    # check exists to give, and a clean cluster that never records the read is
    # indistinguishable from one nobody looked at.
    logging_argv = [
        "gcloud", "logging", "read",
        f'log_id("{AUTOSCALER_LOG_ID}") AND resource.labels.cluster_name="{name}" '
        f"AND (jsonPayload.noDecisionStatus.noScaleUp:* OR jsonPayload.resultInfo.results.errorMsg:*)",
        "--project", project, "--freshness", AUTOSCALER_FRESHNESS,
        "--limit", str(AUTOSCALER_LOG_LIMIT), "--format", "json",
    ]
    entries, logging_result = run_and_gate(logging_argv, run=run)
    if logging_result.rc == 0:
        commands["autoscaler-out-of-resources"] = _record(shlex.join(logging_argv), logging_result)
        # `entries` is None for an empty result set as well as for unparseable
        # output, because gcloud prints nothing at all when nothing matched.
        # rc == 0 already told us the read succeeded, so an empty window is a
        # clean cluster rather than a gap.
        for hit in check_autoscaler_out_of_resources(autoscaler_message_ids(entries), name):
            candidates.append(_emit("autoscaler-out-of-resources", hit))
    else:
        limitations.append(
            f"autoscaler-out-of-resources could not be measured on this cluster: "
            f"`gcloud logging read` failed (rc={logging_result.rc}) — "
            f"{logging_result.stderr.strip()[:200] or 'no stderr'}"
        )

    # §3.8. `capacity-history` takes one machine type per call, so the cost is
    # one read per distinct Spot shape rather than one per cluster. Ordered so
    # the ceiling, when it bites, drops the same shapes on every run instead of
    # whichever ones a dict happened to yield first.
    shapes = spot_shapes(compute_classes, node_pools if pools_readable else [])
    region = region_of(location)
    for machine_type in sorted(shapes)[:SPOT_MAX_SHAPES]:
        advice_argv = [
            "gcloud", "beta", "compute", "advice", "capacity-history",
            "--region", region, "--machine-type", machine_type,
            "--provisioning-model", "SPOT", "--types", "PREEMPTION,PRICE",
            "--project", project, "--format", "json",
        ]
        advice, advice_result = run_and_gate(advice_argv, run=run)
        if advice_result.rc != 0:
            limitations.append(
                f"spot-scarcity-risk could not be measured for {machine_type} in "
                f"{region}: `gcloud beta compute advice capacity-history` failed "
                f"(rc={advice_result.rc}) — {advice_result.stderr.strip()[:200] or 'no stderr'}"
            )
            continue
        commands["spot-scarcity-risk"] = _record(shlex.join(advice_argv), advice_result)
        # A list of one, on every response seen so far. Unwrapped here rather
        # than in the helpers so they take the shape the API documents.
        first = advice[0] if isinstance(advice, list) and advice else advice
        hit, limitation = check_spot_scarcity(machine_type, shapes[machine_type], region, first)
        if hit:
            candidates.append(_emit("spot-scarcity-risk", hit))
        if limitation:
            limitations.append(limitation)
    if len(shapes) > SPOT_MAX_SHAPES:
        limitations.append(
            f"spot-scarcity-risk read {SPOT_MAX_SHAPES} of this cluster's "
            f"{len(shapes)} distinct Spot machine shapes; the rest were not "
            f"measured: {', '.join(sorted(shapes)[SPOT_MAX_SHAPES:])}"
        )
    unqueryable, unpinned = spot_without_a_shape(compute_classes, node_pools if pools_readable else [])
    if unqueryable:
        limitations.append(
            f"spot-scarcity-risk could not be measured for Spot requests that "
            f"name no machine type, which `capacity-history` has no way to "
            f"query: {', '.join(unqueryable)}"
        )
    elif not shapes:
        # No command to record, so §6 would otherwise read the missing record as
        # a check nobody ran. Declared not-applicable for the same reason the
        # Autopilot branch above declares one: it is a fact already in hand.
        not_applicable.append(
            {
                "check": "spot-scarcity-risk",
                "reason": (
                    f"Every Spot request on this cluster leaves the machine shape "
                    f"entirely to GKE ({', '.join(unpinned)}), so no shape can be "
                    f"scarce for it — every family is available to it, which is "
                    f"what this check tests for."
                    if unpinned
                    else "No ComputeClass priority or node pool on this cluster "
                    "requests Spot capacity, so there is no Spot machine shape "
                    "to ask `capacity-history` about."
                ),
            }
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
        quota_records[region] = _record(shlex.join(q_argv), result)
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
        commands.append({"check": "reservation-mismatch-risk", **_record(shlex.join(res_argv), res_result)})
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

    clusters, not_running = enumerate_clusters(resolved_project, run=run)
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
        "clusters": [e for e in cluster_entries if e] + ([project_entry] if project_entry else []) + not_running,
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
