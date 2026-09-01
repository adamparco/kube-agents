#!/opt/hermes/.venv/bin/python3
"""fleet_waste.py — Procedural collector for the Fleet Waste Audit
(`fleet-wide-cost-analysis`).

See docs/designs/fleet-audit-collectors-and-status.md §4.2, §10 phase 4, and
governance/fleet_wide_cost_analysis_sop.md.

This stream's own collector: its targets are both GKE clusters (the eleven
`kubectl` object kinds in `collect_cluster`'s `dump_kinds`, plus a Cloud
Monitoring usage read) and GCP projects
(`gcloud compute disks/addresses/forwarding-rules/target-pools/backend-services`),
so its manifest mixes cluster-named entries with `project/<id>` entries the
same way `networking_audit.py` does (§3's "project-scoped GCP objects" rule).

§1 scopes this to "every project the agent can see", so a bare invocation
discovers every project with at least one cluster the same way
`patch_readiness.py`'s `get_target_projects` does for its sibling stream,
rather than auditing only the active gcloud project; `--project` overrides
discovery for a scoped run. Project-scoped facts (live PV handles, Service
names, referenced addresses) are unioned only across the clusters in the
same project before that project's disk/address/LB checks run — a project
never sees another project's cluster state.

**Usage comes from Cloud Monitoring, not from sampling.** §2 used to require
three `kubectl top` reads five minutes apart per cluster, and the ten minutes
of wall clock that bought was the smaller of its two costs. The larger one was
what a ten-minute Monday-morning window cannot see: a nightly batch peak, a
weekday traffic curve, anything that makes a workload look idle at the moment
you happen to look at it. Every caveat §2 carried — require all three samples
to agree, take the peak and never the mean, keep an absolute floor, never
propose a request below 2x the observed peak — was scaffolding around that
blind spot.

GKE already ships per-container CPU and memory to Cloud Monitoring on every
cluster, retained for weeks. `fetch_usage_peaks` asks it for the peak over the
trailing `USAGE_WINDOW_HOURS` instead, which is both faster (two HTTP reads
per cluster, no sleeping at all) and strictly better evidence: a week-long
peak has already seen the batch job the sample window missed. It reads through
in-process ADC on the agent's own service account, so no token is ever
materialized for a subprocess, and `roles/monitoring.viewer` is the only grant
it needs.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple

MANIFEST_VERSION = 1
KUBECONFIG_DIR = Path(os.environ.get("HERMES_HOME") or "/opt/data") / ".kubeconfigs"
DEFAULT_TIMEOUT_S = 60
# Was 64, sized so every cluster's ten-minute sampling window ran
# concurrently rather than queuing behind an earlier one. Nothing sleeps any
# more -- per-cluster work is a handful of subprocess reads and two HTTP
# reads -- so this drops back to the 8 every other collector in the stream
# uses, which also keeps the shared Monitoring session inside urllib3's
# default connection pool.
MAX_WORKERS = 8

# How far back `fetch_usage_peaks` looks for a workload's peak. A week, to
# match this stream's weekly cadence: every finding is then "this controller
# has not needed that reservation since the last time we said so", and the
# window covers the weekday traffic curve and the weekly batch job that §2's
# ten-minute sample was blind to. Monitoring retains these metrics well past
# this, so the bound is a judgement about relevance, not availability.
USAGE_WINDOW_HOURS = 168
# Alignment happens twice. The primary period buckets each container's raw
# points before they are summed across the containers of a pod -- it has to
# be short enough that a spike stays a spike, since a wide bucket averages
# one away and understates the peak. The secondary pass then takes the max
# across those buckets, which is the number we want, and collapses the
# response to one point per pod: measured on a 137-pod cluster, one page and
# 0.3s rather than ten pages and 3.3s, with both routes agreeing on all 137.
USAGE_ALIGNMENT_S = 300
MONITORING_SCOPE = "https://www.googleapis.com/auth/monitoring.read"
MONITORING_TIMEOUT_S = 120
CPU_METRIC = "kubernetes.io/container/cpu/core_usage_time"
MEM_METRIC = "kubernetes.io/container/memory/used_bytes"

SYSTEM_NAMESPACES = frozenset(
    {
        "kube-system", "kube-public", "kube-node-lease", "gmp-system", "gmp-public", "gke-gmp-system",
        "cnrm-system", "configconnector-operator-system", "krmapihosting-system", "istio-system",
        "asm-system", "anthos-identity-service", "gatekeeper-system", "composer-system",
    }
)


def _is_system_namespace(ns: str) -> bool:
    return ns in SYSTEM_NAMESPACES or ns.startswith("gke-") or ns.startswith("config-management-")


def log(msg: str) -> None:
    print(f"[fleet_waste] {msg}", file=sys.stderr, flush=True)


class Run(NamedTuple):
    argv: list[str]
    rc: int
    stdout: str
    stderr: str
    duration_s: float


RunFn = Callable[..., Run]
# Anything with a `requests`-shaped `.get(url, params=..., timeout=...)`. Kept
# structural rather than typed to `AuthorizedSession` so tests can hand in a
# stub without importing google.auth, which is not a test-time dependency.
SessionFn = Any


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


def get_target_projects(cli_project: str | None, *, run: RunFn) -> list[str]:
    """§1's project scope: "every project the agent can see". A `--project`
    override skips discovery entirely, for a scoped or a test run; otherwise
    this discovers the active project plus every other project with at
    least one cluster, the same way `patch_readiness.py`'s
    `get_target_projects` does for its own sibling stream."""
    if cli_project:
        return [cli_project]

    result = run(["gcloud", "config", "get-value", "project"])
    base = result.stdout.strip() if result.rc == 0 else ""
    projects = [base] if base else []

    _, list_result = run_and_gate(["gcloud", "projects", "list", "--format", "value(projectId)"], run=run)
    if list_result.rc != 0:
        return projects  # discovery unavailable; the base project is the whole scope

    candidates = [p.strip() for p in (list_result.stdout or "").splitlines() if p.strip() and p.strip() != base]
    for candidate in candidates:
        parsed, _ = run_and_gate(
            ["gcloud", "container", "clusters", "list", "--project", candidate, "--format", "json"], run=run
        )
        # `[]` and `None` are different answers and only the first one means the
        # project owes this audit nothing. A project this probe could not read
        # stays in scope so `collect_fleet` records the loss as a `gate-failed`
        # target; dropped here it leaves no trace in the manifest at all, and a
        # project nobody could enumerate then reads exactly like one holding no
        # clusters. `patch_readiness.py` carries the same guard.
        if parsed is None or parsed:
            projects.append(candidate)
    return projects


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
        "autopilot": bool((c.get("autopilot") or {}).get("enabled")),
        "outcome": "unreachable",
        "error": f"cluster status is {c.get('status') or 'unknown'}, not RUNNING; no check was evaluated against it",
    }


def enumerate_clusters(project: str, *, run: RunFn) -> tuple[list[dict], list[dict]]:
    result = run(
        ["gcloud", "container", "clusters", "list", "--project", project, "--format", "json(name,location,status,autopilot.enabled)"]
    )
    if result.rc != 0:
        raise RuntimeError(f"cluster enumeration failed (rc={result.rc}): {result.stderr.strip()[:500]}")
    clusters = json.loads(result.stdout or "[]")
    running = [
        {"name": c["name"], "location": c.get("location"), "project": project, "autopilot": bool((c.get("autopilot") or {}).get("enabled"))}
        for c in clusters
        if c.get("status") == "RUNNING"
    ]
    return running, [not_running_entry(c, project) for c in clusters if c.get("status") != "RUNNING"]


# --------------------------------------------------------------------------- #
# `kubectl top` parsing — plain columnar text, not JSON.
# --------------------------------------------------------------------------- #

CPU_RE = re.compile(r"^(\d+(?:\.\d+)?)(m)?$")
MEM_RE = re.compile(r"^(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti)?$")


def parse_cpu_cores(s: str) -> float | None:
    m = CPU_RE.match((s or "").strip())
    if not m:
        return None
    value, unit = m.groups()
    return float(value) / 1000.0 if unit == "m" else float(value)


MEM_UNIT_TO_MIB = {"Ki": 1 / 1024, "Mi": 1.0, "Gi": 1024.0, "Ti": 1024.0 * 1024.0}
BYTES_PER_MIB = 1024.0 * 1024.0


def parse_mem_mib(s: str) -> float | None:
    m = MEM_RE.match((s or "").strip())
    if not m:
        return None
    value, unit = m.groups()
    if unit is None:
        # A Kubernetes resource.Quantity with no suffix is a byte count
        # (e.g. a container's `resources.requests.memory: "134217728"`),
        # never MiB -- treating it as already-MiB overstates a bare-byte
        # request by a factor of 2^20.
        return float(value) / BYTES_PER_MIB
    return float(value) * MEM_UNIT_TO_MIB[unit]


def default_monitoring_session() -> SessionFn:
    """An ADC-authenticated session for the Monitoring read API.

    Imported lazily: `google.auth` ships in the agent image but is not a
    test-time dependency, and a module-level import would make every unit test
    in this file unrunnable outside the pod.

    This is the whole reason the usage read does not go through `run`. The
    credential proxy that fronts `gcloud` refuses `auth print-access-token`
    outright (policy rule `gcp.access-token-disclosure`), and rightly so -- a
    token printed to stdout is a token in the model's context. Reading in
    process never materializes one.
    """
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(scopes=[MONITORING_SCOPE])
    return AuthorizedSession(credentials)


def _point_value(point: dict) -> float | None:
    value = point.get("value") or {}
    if value.get("doubleValue") is not None:
        return float(value["doubleValue"])
    if value.get("int64Value") is not None:
        return float(value["int64Value"])
    return None


def fetch_usage_peaks(
    project: str,
    cluster: str,
    *,
    session: SessionFn,
    now: datetime,
    window_hours: int = USAGE_WINDOW_HOURS,
) -> tuple[dict[tuple[str, str], tuple[float, float]], bool, Run]:
    """§2's usage figures, read from Cloud Monitoring rather than sampled.

    Returns `(peaks, available, result)`. `peaks` maps `(namespace, pod)` to
    that pod's `(peak_cpu_cores, peak_mem_mib)` over the trailing
    `window_hours` -- deliberately the same key and the same two units the
    `kubectl top pods` parse produced, so `check_overrequest` reads it
    unchanged.

    `available=False` is §2's metrics degradation and reaches the manifest as
    a limitation rather than a silent zero. It covers the empty answer as well
    as the failed one: a 200 carrying no time series means this cluster is not
    shipping system metrics, and treating that as "usage was zero" would read
    every workload on it as pure waste.

    Both metrics are per-*container*. `REDUCE_SUM` over
    `(namespace_name, pod_name)` adds a pod's containers back together -- and,
    for memory, its `memory_type` breakdown, so the figure is comparable to
    the pod's summed requests.
    """
    started = time.monotonic()
    start = now - timedelta(hours=window_hours)
    url = f"https://monitoring.googleapis.com/v3/projects/{project}/timeSeries"
    # Recorded in the manifest in place of an argv. Not runnable as-is, but it
    # names the project, the cluster, both metrics and the window, which is
    # what a reader checking the evidence behind a finding needs.
    label = (
        f"GET monitoring.googleapis.com/v3/projects/{project}/timeSeries"
        f' filter=resource.labels.cluster_name="{cluster}"'
        f" metrics={CPU_METRIC},{MEM_METRIC} window={window_hours}h"
    )

    def fail(rc: int, message: str) -> tuple[dict, bool, Run]:
        return {}, False, Run([label], rc, "", message[:300], time.monotonic() - started)

    if session is None:
        return fail(-1, "no Cloud Monitoring session: ADC credentials were unavailable at startup")

    peaks: dict[str, dict[tuple[str, str], float]] = {"cpu": {}, "mem": {}}
    for metric, aligner, key in ((CPU_METRIC, "ALIGN_RATE", "cpu"), (MEM_METRIC, "ALIGN_MAX", "mem")):
        sink = peaks[key]
        page_token = None
        while True:
            params = {
                "filter": f'metric.type="{metric}" AND resource.labels.cluster_name="{cluster}"',
                "interval.startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "interval.endTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "aggregation.alignmentPeriod": f"{USAGE_ALIGNMENT_S}s",
                "aggregation.perSeriesAligner": aligner,
                "aggregation.crossSeriesReducer": "REDUCE_SUM",
                "aggregation.groupByFields": ["resource.labels.namespace_name", "resource.labels.pod_name"],
                "secondaryAggregation.alignmentPeriod": f"{window_hours * 3600}s",
                "secondaryAggregation.perSeriesAligner": "ALIGN_MAX",
                "pageSize": "2000",
            }
            if page_token:
                params["pageToken"] = page_token
            try:
                response = session.get(url, params=params, timeout=MONITORING_TIMEOUT_S)
            except Exception as exc:
                return fail(-1, f"{metric}: {type(exc).__name__}: {exc}")
            if response.status_code != 200:
                return fail(response.status_code, f"{metric}: {response.text}")
            body = response.json()
            for series in body.get("timeSeries") or []:
                labels = (series.get("resource") or {}).get("labels") or {}
                pod_key = (labels.get("namespace_name", ""), labels.get("pod_name", ""))
                for point in series.get("points") or []:
                    value = _point_value(point)
                    if value is not None:
                        sink[pod_key] = max(sink.get(pod_key, 0.0), value)
            page_token = body.get("nextPageToken")
            if not page_token:
                break

    merged = {
        pod_key: (peaks["cpu"].get(pod_key, 0.0), peaks["mem"].get(pod_key, 0.0) / BYTES_PER_MIB)
        for pod_key in set(peaks["cpu"]) | set(peaks["mem"])
    }
    if not merged:
        return fail(0, f'no time series for cluster_name="{cluster}" over the trailing {window_hours}h')

    # The manifest digests a command's stdout to prove two runs saw the same
    # thing. There is no stdout here, so stand in the parsed answer, rounded
    # so a digest tracks a real change in usage rather than float noise.
    rendered = json.dumps(
        sorted((ns, pod, round(cpu, 4), round(mem, 1)) for (ns, pod), (cpu, mem) in merged.items())
    )
    return merged, True, Run([label], 0, rendered, "", time.monotonic() - started)


# --------------------------------------------------------------------------- #
# Object normalization — every kind Step 2 dumps, filtered to what each check
# actually needs. `dump` is the parsed `kubectl get <kinds> -A -o json`.
# --------------------------------------------------------------------------- #


def _by_kind(dump: dict, kind: str) -> list[dict]:
    return [i for i in dump.get("items", []) or [] if i.get("kind") == kind]


def build_context(dump: dict) -> dict:
    return {
        "nodes": _by_kind(dump, "Node"),
        "pods": _by_kind(dump, "Pod"),
        "pvcs": _by_kind(dump, "PersistentVolumeClaim"),
        "pvs": _by_kind(dump, "PersistentVolume"),
        "services": _by_kind(dump, "Service"),
        "jobs": _by_kind(dump, "Job"),
        "cronjobs": _by_kind(dump, "CronJob"),
        "pdbs": _by_kind(dump, "PodDisruptionBudget"),
        "namespaces": _by_kind(dump, "Namespace"),
        "resourcequotas": _by_kind(dump, "ResourceQuota"),
        "statefulsets": _by_kind(dump, "StatefulSet"),
    }


def _age_days(timestamp: str, *, now: datetime) -> float | None:
    if not timestamp:
        return None
    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now - ts).total_seconds() / 86400.0


def _whole_days(age: float) -> int:
    """Elapsed days, floored -- never the next day up.

    `f"{age:.0f}"` rounds to nearest, which reads as a harmless half-day of
    imprecision until the number lands next to a threshold. It did: the same
    `argocd-webhook-ip` the docstring below is about was 29.69 days old on
    2026-09-01, printed as "(30d ago)", and the model titled the finding
    "unused for 30+ days" -- a claim about a threshold, derived from a rounded
    number, and false. Rounding down cannot produce that: a model given "29d
    ago" has nothing to round up from.

    Floored rather than truncated toward zero only matters for a negative age,
    which means the clock moved backwards between the object's creation and the
    read. Clamp that to 0 rather than print "-1d", which would assert the thing
    is from the future.
    """
    return int(age) if age > 0 else 0


def _ago(age: float | None) -> str:
    """The elapsed-days parenthetical an excerpt puts after a timestamp.

    Every check here that gates on age computes one, decides with it, and then
    quoted the raw ISO timestamp and threw the number away. The model reading
    the manifest still has to say how long the thing has been idle -- that is
    the finding -- so it re-derived the age by doing date arithmetic on the
    string, and got it wrong: an address reserved on 2026-08-02, read on
    2026-09-01, was published as "unused for 28 days". Nothing downstream
    catches that, because `adopt_collector_evidence` replaces the model's
    evidence with the collector's and leaves the title alone, so the wrong
    number is the one a reader sees next to correct evidence.

    Returning the number the gate already used costs nothing and removes the
    arithmetic from the model's job. Empty when the age is unknown rather than
    guessing: a missing timestamp is why `_age_days` returns None, and "(0d
    ago)" would assert something about it.
    """
    return "" if age is None else f" ({_whole_days(age)}d ago)"


# --------------------------------------------------------------------------- #
# 3.2 orphan-pv
# --------------------------------------------------------------------------- #

BACKUP_ANNOTATION_PREFIXES = ("velero.io/", "gke.io/backup-")


def _matches_live_statefulset_pvc(claim_name: str, sts_names: set[str]) -> bool:
    """`<volumeClaimTemplate>-<statefulSet>-<ordinal>` for a StatefulSet that
    still exists is a scaled-to-zero StatefulSet's own claim, deliberate and
    never waste -- regardless of what the volumeClaimTemplate is named."""
    return any(re.match(rf"^.+-{re.escape(sts)}-\d+$", claim_name) for sts in sts_names if sts)


def check_orphan_pv(context: dict, *, now: datetime) -> list[dict]:
    pvc_exists = {(p["metadata"].get("namespace", ""), p["metadata"].get("name", "")) for p in context["pvcs"]}
    sts_names = {s.get("metadata", {}).get("name", "") for s in context.get("statefulsets", [])}
    hits = []
    for pv in context["pvs"]:
        meta, spec, status = pv.get("metadata", {}), pv.get("spec", {}), pv.get("status", {})
        name = meta.get("name", "")
        if spec.get("persistentVolumeReclaimPolicy") != "Retain":
            continue
        annotations = meta.get("annotations") or {}
        if any(k.startswith(prefix) for k in annotations for prefix in BACKUP_ANNOTATION_PREFIXES):
            continue
        if annotations.get("addonmanager.kubernetes.io/mode"):
            continue
        phase = status.get("phase", "")
        claim_ref = spec.get("claimRef") or {}
        claim_ns, claim_name = claim_ref.get("namespace", ""), claim_ref.get("name", "")
        if claim_name:
            if (claim_ns, claim_name) in pvc_exists:
                continue
            if _matches_live_statefulset_pvc(claim_name, sts_names):
                continue

        if phase in ("Released", "Failed"):
            transition = status.get("lastPhaseTransitionTime", "")
            age = _age_days(transition, now=now) if transition else _age_days(meta.get("creationTimestamp", ""), now=now)
            fallback_note = "" if transition else " (lastPhaseTransitionTime absent; using object AGE)"
            if age is None or age < 7:
                continue
            hits.append(
                {
                    "object": f"PersistentVolume/{name}",
                    "excerpt": f"phase={phase} since {transition or meta.get('creationTimestamp')}{_ago(age)}{fallback_note}, capacity={spec.get('capacity', {}).get('storage')}",
                    "severity": "major" if _is_large_or_ssd(spec) else "minor",
                }
            )
        elif phase == "Available" and not claim_ref:
            age = _age_days(meta.get("creationTimestamp", ""), now=now)
            if age is None or age < 30:
                continue
            hits.append(
                {
                    "object": f"PersistentVolume/{name}",
                    "excerpt": f"phase=Available, unclaimed, AGE={_whole_days(age)}d, storageClass={spec.get('storageClassName')}",
                    "severity": "major" if _is_large_or_ssd(spec) else "minor",
                }
            )
    return hits


def _gib(quantity: str) -> float:
    quantity = quantity or "0"
    if quantity.endswith("Ti"):
        return float(quantity[:-2]) * 1024
    if quantity.endswith("Gi"):
        return float(quantity[:-2])
    if quantity.endswith("Mi"):
        return float(quantity[:-2]) / 1024
    return 0.0


def _is_large_or_ssd(spec: dict) -> bool:
    gib = _gib((spec.get("capacity") or {}).get("storage", "0"))
    sc = (spec.get("storageClassName") or "").lower()
    return gib >= 100 or "ssd" in sc or "extreme" in sc


# --------------------------------------------------------------------------- #
# 3.3 unconsumed-pvc
# --------------------------------------------------------------------------- #


def check_unconsumed_pvc(context: dict, *, now: datetime) -> list[dict]:
    referenced = set()
    for pod in context["pods"]:
        ns = pod.get("metadata", {}).get("namespace", "")
        for vol in (pod.get("spec") or {}).get("volumes") or []:
            claim = (vol.get("persistentVolumeClaim") or {}).get("claimName")
            if claim:
                referenced.add((ns, claim))
    sts_by_ns: dict[str, set[str]] = {}
    for sts in context.get("statefulsets", []):
        sts_by_ns.setdefault(sts.get("metadata", {}).get("namespace", ""), set()).add(sts.get("metadata", {}).get("name", ""))

    hits = []
    for pvc in context["pvcs"]:
        meta, spec, status = pvc.get("metadata", {}), pvc.get("spec", {}), pvc.get("status", {})
        ns, name = meta.get("namespace", ""), meta.get("name", "")
        if _is_system_namespace(ns):
            continue
        if status.get("phase") != "Bound":
            continue
        if (ns, name) in referenced:
            continue
        if _matches_live_statefulset_pvc(name, sts_by_ns.get(ns, set())):
            continue
        annotations = meta.get("annotations") or {}
        if any(k.startswith("configsync.gke.io/") for k in annotations):
            continue
        age = _age_days(meta.get("creationTimestamp", ""), now=now)
        if age is None or age < 14:
            continue
        capacity = (status.get("capacity") or {}).get("storage", "0")
        gib = _gib(capacity)
        sc = (spec.get("storageClassName") or "").lower()
        hits.append(
            {
                "namespace": ns,
                "object": f"PersistentVolumeClaim/{name}",
                "excerpt": f"Bound, {capacity}, {sc}, unreferenced by any pod, AGE={_whole_days(age)}d",
                "severity": "major" if gib >= 100 or "ssd" in sc else "minor",
            }
        )
    return hits


# --------------------------------------------------------------------------- #
# 3.7 idle-nodepool / 3.8 scaledown-blocked
# --------------------------------------------------------------------------- #


def _pod_daemonset_owned(pod: dict) -> bool:
    return any(o.get("kind") == "DaemonSet" for o in (pod.get("metadata", {}).get("ownerReferences") or []))


def _sum_requests(pods: list[dict]) -> tuple[float, float]:
    cpu_total, mem_total = 0.0, 0.0
    for pod in pods:
        for c in (pod.get("spec") or {}).get("containers") or []:
            req = (c.get("resources") or {}).get("requests") or {}
            cpu_total += parse_cpu_cores(str(req.get("cpu", "0"))) or 0
            mem_total += parse_mem_mib(str(req.get("memory", "0"))) or 0
    return cpu_total, mem_total


def _allocatable(node: dict) -> tuple[float, float]:
    alloc = (node.get("status") or {}).get("allocatable") or {}
    return parse_cpu_cores(str(alloc.get("cpu", "0"))) or 0, parse_mem_mib(str(alloc.get("memory", "0"))) or 0


MACHINE_TYPE_VCPU_RE = re.compile(r"^[a-z0-9]+-(?:standard|highmem|highcpu|megamem|ultramem)-(\d+)(?:-\w+)?$")
CUSTOM_MACHINE_TYPE_VCPU_RE = re.compile(r"^(?:[a-z0-9]+-)?custom-(\d+)-\d+$")
# `a2-highgpu-1g`, `a2-ultragpu-8g`, `a3-megagpu-8g`, `ct5lp-hightpu-4t`. The
# trailing number on an accelerator machine type counts GPUs or TPU chips, not
# vCPUs, so the family cannot go in the pattern above: `highgpu` sat in it and
# never matched anything, because the suffix is `8g` rather than `8`. Reading
# the digit as a vCPU count would be worse than not matching -- `a3-highgpu-8g`
# is a 208-vCPU machine and would have scored 8. Every member of these families
# is far past the 8-vCPU line §3.7's severity rule draws, so they are answered
# directly. `config.accelerators` covers most of them anyway, but not TPU pools:
# a TPU node pool carries its topology, not an `accelerators` list, and §3.7 is
# explicit that "an idle accelerator pool with a non-zero floor is the single
# largest reclaimable item this audit can find".
ACCELERATOR_MACHINE_RE = re.compile(r"^[a-z0-9]+-(?:high|ultra|mega)(?:gpu|tpu)-\d+[a-z]?$")


def _machine_type_vcpus(machine_type: str) -> int | None:
    for pattern in (MACHINE_TYPE_VCPU_RE, CUSTOM_MACHINE_TYPE_VCPU_RE):
        m = pattern.match(machine_type or "")
        if m:
            return int(m.group(1))
    return None


def _is_big_machine(machine_type: str) -> bool:
    """§3.7's "machine type of >= 8 vCPU, or attached accelerators" severity leg."""
    if ACCELERATOR_MACHINE_RE.match(machine_type or ""):
        return True
    return (_machine_type_vcpus(machine_type) or 0) >= 8


def check_idle_nodepool(context: dict, node_pools: list[dict], *, now: datetime) -> list[dict]:
    nodes_by_pool: dict[str, list[dict]] = {}
    for node in context["nodes"]:
        pool = (node.get("metadata", {}).get("labels") or {}).get("cloud.google.com/gke-nodepool", "")
        nodes_by_pool.setdefault(pool, []).append(node)

    running_pods = [p for p in context["pods"] if (p.get("status") or {}).get("phase") == "Running"]
    pods_by_node: dict[str, list[dict]] = {}
    for pod in running_pods:
        if _pod_daemonset_owned(pod):
            continue
        node_name = (pod.get("spec") or {}).get("nodeName", "")
        if node_name:
            pods_by_node.setdefault(node_name, []).append(pod)

    hits = []
    for pool in node_pools:
        pool_name = pool.get("name", "")
        nodes = nodes_by_pool.get(pool_name, [])
        if not nodes or len(node_pools) <= 1:
            continue
        # §3.7's "pools created < 7 days ago" exclusion, measured off the
        # *oldest* node rather than whichever one the dump listed first. The
        # dump comes back name-sorted, so `nodes[0]` is the alphabetically
        # first node, and a pool that has run for months exempts itself the
        # moment an autoscaler adds a node whose name happens to sort early.
        # The oldest node is the closest lower bound on the pool's own age that
        # the object dump carries.
        ages = [
            age
            for node in nodes
            if (age := _age_days((node.get("metadata", {}) or {}).get("creationTimestamp", ""), now=now))
            is not None
        ]
        if ages and max(ages) < 7:
            continue
        if any(node.get("spec", {}).get("unschedulable") for node in nodes):
            continue

        # §3.7 flags when *every node in the pool* is under 15%, not when the
        # pool averages under 15%. The two disagree exactly where it matters:
        # a ten-node pool with one node full and nine empty averages 10% and
        # was reported as an idle pool whose floor should drop to zero, when
        # the node holding the workload is precisely what stops it shrinking.
        # The aggregate figures are still computed, because the excerpt quotes
        # them and a reader wants the pool-level number.
        cpu_alloc_total = mem_alloc_total = cpu_req_total = mem_req_total = 0.0
        every_node_idle = True
        for node in nodes:
            node_name = node.get("metadata", {}).get("name", "")
            cpu_alloc, mem_alloc = _allocatable(node)
            cpu_req, mem_req = _sum_requests(pods_by_node.get(node_name, []))
            cpu_alloc_total += cpu_alloc
            mem_alloc_total += mem_alloc
            cpu_req_total += cpu_req
            mem_req_total += mem_req
            if cpu_alloc <= 0 or mem_alloc <= 0:
                # A node reporting no allocatable capacity is one this check
                # cannot judge -- NotReady, or mid-registration. Treat it as
                # not-idle rather than divide by zero: an unreadable node is
                # not evidence the pool is reclaimable.
                every_node_idle = False
            elif cpu_req / cpu_alloc > 0.15 or mem_req / mem_alloc > 0.15:
                every_node_idle = False
        if not every_node_idle:
            continue
        if cpu_alloc_total == 0 or mem_alloc_total == 0:
            continue
        cpu_pct, mem_pct = cpu_req_total / cpu_alloc_total, mem_req_total / mem_alloc_total

        autoscaling = pool.get("autoscaling") or {}
        floor_nonzero = not autoscaling.get("enabled") or (autoscaling.get("minNodeCount") or 0) >= 1
        if not floor_nonzero:
            continue

        machine_type = ((pool.get("config") or {}).get("machineType") or "")
        has_accelerator = bool((pool.get("config") or {}).get("accelerators"))
        severity = "major" if len(nodes) >= 3 or _is_big_machine(machine_type) or has_accelerator else "minor"
        hits.append(
            {
                "object": f"NodePool/{pool_name}",
                "excerpt": f"{len(nodes)} node(s), {machine_type}, min={autoscaling.get('minNodeCount')}, non-DS CPU {cpu_pct * 100:.0f}% / mem {mem_pct * 100:.0f}% of allocatable",
                "severity": severity,
                "_node_names": {n.get("metadata", {}).get("name", "") for n in nodes},
            }
        )
    return hits


SAFE_TO_EVICT_ANNOTATION = "cluster-autoscaler.kubernetes.io/safe-to-evict"
# What the cluster autoscaler itself accepts. It reads the annotation with Go's
# `strconv.ParseBool`, so `"False"`, `"FALSE"` and `"0"` pin a pod exactly as
# `"false"` does. An exact-string comparison against `"false"` reads all three
# as "annotation absent" and takes the permissive branch, so a node the
# autoscaler will never drain went unreported -- and on the other side,
# `!= "true"` read `"True"` as unset and reported a node whose owner had
# explicitly cleared it. Both directions are wrong against the same parse.
_EVICT_TRUE = frozenset({"1", "t", "true"})
_EVICT_FALSE = frozenset({"0", "f", "false"})


def _safe_to_evict(annotations: dict) -> bool | None:
    """The `safe-to-evict` annotation as set-true, set-false, or unset.

    Unset covers a value the autoscaler cannot parse either, which it ignores.
    """
    raw = (annotations.get(SAFE_TO_EVICT_ANNOTATION) or "").strip().lower()
    if raw in _EVICT_TRUE:
        return True
    if raw in _EVICT_FALSE:
        return False
    return None


def check_scaledown_blocked(context: dict, idle_pool_hits: list[dict]) -> list[dict]:
    flagged_nodes: set[str] = set()
    for hit in idle_pool_hits:
        flagged_nodes |= hit.get("_node_names", set())
    if not flagged_nodes:
        return []

    pdb_selectors = [((pdb.get("spec") or {}).get("selector") or {}) for pdb in context["pdbs"]]

    def _selector_matches(selector: dict, labels: dict) -> bool:
        return all(labels.get(k) == v for k, v in (selector.get("matchLabels") or {}).items())

    hits = []
    seen_nodes = set()
    for pod in context["pods"]:
        node_name = (pod.get("spec") or {}).get("nodeName", "")
        if node_name not in flagged_nodes or node_name in seen_nodes:
            continue
        ns = pod.get("metadata", {}).get("namespace", "")
        if _is_system_namespace(ns):
            continue
        owners = pod.get("metadata", {}).get("ownerReferences") or []
        annotations = pod.get("metadata", {}).get("annotations") or {}
        evictable = _safe_to_evict(annotations)
        has_local_storage = any(("emptyDir" in v or "hostPath" in v) for v in (pod.get("spec") or {}).get("volumes") or [])

        blocked_by_pdb = any(_selector_matches(sel, (pod.get("metadata", {}).get("labels") or {})) for sel in pdb_selectors)
        if blocked_by_pdb:
            continue  # already reported by obtainability-audit's 3.3/3.4

        bare_pod = not owners
        unevictable = evictable is False or (bare_pod and has_local_storage) or (has_local_storage and evictable is not True)
        if not unevictable:
            continue

        permanent = (bare_pod and has_local_storage) or evictable is False
        pod_name = pod.get("metadata", {}).get("name", "")
        # The raw annotation, not the parse: the excerpt is evidence, and a
        # reader checking it against `kubectl get pod -o yaml` needs to see what
        # is really on the object.
        hits.append(
            {
                "object": f"Node/{node_name}",
                "excerpt": f"pod {ns}/{pod_name} blocks drain (ownerReferences={'none' if bare_pod else 'set'}, safe-to-evict={annotations.get(SAFE_TO_EVICT_ANNOTATION)}, local-storage={has_local_storage})",
                "severity": "critical" if permanent else "major",
            }
        )
        seen_nodes.add(node_name)
    return hits


# --------------------------------------------------------------------------- #
# 3.9 terminal-pods
# --------------------------------------------------------------------------- #

GC_OWNED_LABEL_PREFIXES = ("workflows.argoproj.io/", "tekton.dev/", "fluxcd.io/")
#: The Job conditions that mean "this Job is over". `SuccessCriteriaMet` is
#: what a Job with a `successPolicy` gets instead of `Complete`.
JOB_TERMINAL_CONDITIONS = ("Complete", "Failed", "SuccessCriteriaMet")


def _job_finished_at(status: dict) -> str:
    """When a Job without a `completionTime` last transitioned to a terminal
    condition.

    A failed Job never gets a `completionTime`, so the condition list is the
    only timestamp there -- and it is a *list*, appended to in the order the
    controller set each condition. `conditions[0]` is therefore whichever one
    was set first, which on a suspended-then-failed Job is `Suspended` and on a
    Job the controller has since un-suspended is a condition whose
    `lastTransitionTime` is when it stopped applying. Both dated the Job's
    death to the wrong moment, and 3.9's whole gate is "terminal for >= 7 days".
    """
    candidates = [
        c.get("lastTransitionTime", "")
        for c in status.get("conditions") or []
        if c.get("type") in JOB_TERMINAL_CONDITIONS and c.get("status") == "True"
    ]
    return max((c for c in candidates if c), default="")


def check_terminal_pods(context: dict, *, now: datetime) -> list[dict]:
    terminal = [p for p in context["pods"] if (p.get("status") or {}).get("phase") in ("Succeeded", "Failed")]
    by_ns: dict[str, list[dict]] = {}
    for pod in terminal:
        ns = pod.get("metadata", {}).get("namespace", "")
        if _is_system_namespace(ns):
            continue
        labels = pod.get("metadata", {}).get("labels") or {}
        if any(k.startswith(p) for k in labels for p in GC_OWNED_LABEL_PREFIXES):
            continue
        age = _age_days(pod.get("metadata", {}).get("creationTimestamp", ""), now=now)
        if age is not None and age < 1:
            continue
        by_ns.setdefault(ns, []).append(pod)

    hits = []
    total = sum(len(v) for v in by_ns.values())
    for ns, pods in by_ns.items():
        oldest = min((p.get("metadata", {}).get("creationTimestamp", "") for p in pods), default="")
        oldest_age = _age_days(oldest, now=now)
        old_enough = any((_age_days(p.get("metadata", {}).get("creationTimestamp", ""), now=now) or 0) >= 7 for p in pods)
        if len(pods) >= 50 or old_enough:
            severity = "major" if len(pods) > 500 or total > 2000 else "minor"
            hits.append({"namespace": ns, "object": f"Namespace/{ns}", "excerpt": f"{len(pods)} terminal pods, oldest from {oldest}{_ago(oldest_age)}", "severity": severity})

    for job in context["jobs"]:
        meta, spec, status = job.get("metadata", {}), job.get("spec", {}), job.get("status", {})
        ns = meta.get("namespace", "")
        if _is_system_namespace(ns):
            continue
        if any(o.get("kind") == "CronJob" for o in (meta.get("ownerReferences") or [])):
            continue
        if spec.get("ttlSecondsAfterFinished") is not None:
            continue
        done = status.get("completionTime") or _job_finished_at(status)
        if not (status.get("succeeded") or status.get("failed")):
            continue
        age = _age_days(done, now=now)
        if age is None or age < 7:
            continue
        hits.append({"namespace": ns, "object": f"Job/{meta.get('name', '')}", "excerpt": f"finished {done}{_ago(age)}, no ttlSecondsAfterFinished", "severity": "minor"})

    for cj in context["cronjobs"]:
        meta, spec = cj.get("metadata", {}), cj.get("spec", {})
        ns = meta.get("namespace", "")
        if _is_system_namespace(ns):
            continue
        if (spec.get("successfulJobsHistoryLimit") or 0) > 10:
            hits.append({"namespace": ns, "object": f"CronJob/{meta.get('name', '')}", "excerpt": f"successfulJobsHistoryLimit={spec.get('successfulJobsHistoryLimit')}", "severity": "minor"})
    return hits


# --------------------------------------------------------------------------- #
# 3.10 idle-namespace
# --------------------------------------------------------------------------- #


def check_idle_namespace(context: dict, *, now: datetime) -> list[dict]:
    active_ns = {
        p.get("metadata", {}).get("namespace", "")
        for p in context["pods"]
        if (p.get("status") or {}).get("phase") in ("Running", "Pending")
    }
    pvc_gib_by_ns: dict[str, float] = {}
    for pvc in context["pvcs"]:
        ns = pvc.get("metadata", {}).get("namespace", "")
        cap = ((pvc.get("status") or {}).get("capacity") or {}).get("storage", "0")
        pvc_gib_by_ns[ns] = pvc_gib_by_ns.get(ns, 0) + _gib(cap)
    lb_ns = {s.get("metadata", {}).get("namespace", "") for s in context["services"] if (s.get("spec") or {}).get("type") == "LoadBalancer"}
    # A ResourceQuota used to be a third way in here, and it is not billable.
    # Kubernetes reserves nothing for one: it is an admission gate on the sum of
    # the requests of the pods in its namespace, it holds no capacity, no
    # scheduler consults it on behalf of anyone else, and deleting it frees
    # nothing and saves nothing. Nothing in the fleet-waste dump is cheaper to
    # keep. The arm was untested -- every case passed `resourcequotas: []` --
    # and on the reference fleet it produced exactly one finding, an empty
    # `gitops-managed` whose only object was a quota with `used` all zeroes, for
    # which the model wrote that the quota reserved "10 vCPU / 20 GiB of request
    # headroom ... that no other namespace on this Autopilot cluster can use".
    # Every clause of that is false, and it is the kind of false a cost audit
    # can least afford: a reader who checks one savings number and finds it
    # imaginary stops believing the ones that are real.

    hits = []
    for ns_obj in context["namespaces"]:
        name = ns_obj.get("metadata", {}).get("name", "")
        if _is_system_namespace(name) or name in active_ns:
            continue
        if (ns_obj.get("status") or {}).get("phase") == "Terminating":
            continue
        annotations = ns_obj.get("metadata", {}).get("annotations") or {}
        if any(k.startswith("configsync.gke.io/") or k.startswith("kustomize.toolkit.fluxcd.io/") for k in annotations):
            continue
        age = _age_days(ns_obj.get("metadata", {}).get("creationTimestamp", ""), now=now)
        if age is None or age < 30:
            continue
        billable = name in lb_ns or pvc_gib_by_ns.get(name, 0) > 0
        if not billable:
            continue
        # Floor the capacity for the same reason `_whole_days` floors an age:
        # this number sits next to a threshold. A namespace holding one
        # 102000Mi PVC is 99.6 GiB, which `:.0f` printed as "100 GiB" while the
        # severity gate -- reading the raw value -- graded it `minor`, so the
        # excerpt asserted the threshold in the same sentence the severity
        # denied it. Gate on the floored number too: `floor(x) >= 100` and
        # `x >= 100` are the same test, and reading one value twice is what
        # makes the two impossible to disagree again.
        gib = pvc_gib_by_ns.get(name, 0)
        whole_gib = int(gib)
        # "<1" rather than "0" when there really is a claim: flooring a 500Mi
        # PVC to "0 GiB of PVCs" would deny the storage that made the namespace
        # billable three lines up. A namespace billable only through a
        # LoadBalancer holds no PVCs and still prints "0", which the excerpt
        # names the LoadBalancer alongside so the reason is never absent.
        gib_text = str(whole_gib) if whole_gib or not gib else "<1"
        severity = "major" if name in lb_ns or whole_gib >= 100 else "minor"
        hits.append(
            {
                "object": f"Namespace/{name}",
                "excerpt": f"no Running/Pending pods for {_whole_days(age)}d; holds {'a LoadBalancer Service, ' if name in lb_ns else ''}{gib_text} GiB of PVCs",
                "severity": severity,
            }
        )
    return hits


# --------------------------------------------------------------------------- #
# 3.1 overrequest (usage-sampling)
# --------------------------------------------------------------------------- #


def _owner_key(owners: list[dict]) -> tuple[str, str] | None:
    """The controller that owns a pod, as `(kind, name)`.

    `ownerReferences[0]` is not it. The API guarantees at most one entry with
    `controller: true` and says nothing about the order of the rest, so a pod
    carrying a second, non-controlling reference -- which is how several
    operators mark ownership alongside the ReplicaSet -- could aggregate under
    either one, and under a different one next week. That moves the finding's
    `object`, and §5 is explicit that a `check`/`cluster`/`namespace`/`object`
    that moves is announced as fixed one week and re-announced as new the next.
    Sorting the non-controller fallback keeps the answer stable even where no
    reference claims to be the controller.
    """
    controllers = [o for o in owners if o.get("controller")]
    pick = controllers[0] if controllers else min(owners, key=lambda o: (o.get("kind", ""), o.get("name", "")), default=None)
    if not pick:
        return None
    return (pick.get("kind", ""), pick.get("name", ""))


def check_overrequest(context: dict, usage_peaks: dict, *, now: datetime, autopilot: bool) -> list[dict]:
    """`usage_peaks` is `fetch_usage_peaks`'s `(ns, pod) -> (cores, MiB)`.

    It used to be a list of three `kubectl top` samples, and the flag rule
    used to be "every sample agrees usage is under 20% of requests, and the
    reclaimable delta is measured against the highest of them". One peak over
    a week is the same rule with the sampling error taken out: the max across
    samples *is* the peak, and a run of samples all agreeing is exactly the
    condition that the peak clears the bar.
    """
    if not usage_peaks:
        return []
    by_owner: dict[tuple, dict] = {}
    for pod in context["pods"]:
        meta, spec, status = pod.get("metadata", {}), pod.get("spec", {}), pod.get("status", {})
        ns, name = meta.get("namespace", ""), meta.get("name", "")
        if _is_system_namespace(ns):
            continue
        if status.get("phase") in ("Pending", "Terminating"):
            continue
        age = _age_days(status.get("startTime", ""), now=now)
        if age is not None and age < (1 / 24):
            continue
        owners = meta.get("ownerReferences") or []
        if any(o.get("kind") in ("Job",) for o in owners):
            continue
        if any(o.get("kind") == "DaemonSet" for o in owners):
            continue
        containers = spec.get("containers") or []
        requests = [(c.get("resources") or {}).get("requests") or {} for c in containers]
        limits = [(c.get("resources") or {}).get("limits") or {} for c in containers]
        if not any(requests):
            continue  # obtainability-audit's `no-requests` owns this
        owner_key = _owner_key(owners) or ("Pod", name)
        entry = by_owner.setdefault(owner_key, {"ns": ns, "pods": [], "oldest_h": None})
        entry["pods"].append({"ns": ns, "name": name, "requests": requests, "limits": limits})
        # A peak is only as long as the pod that reported it. Monitoring keeps a
        # week of history, but a Deployment rolled an hour ago has an hour of it,
        # and a finding that says "over the trailing 168h" about that controller
        # is claiming to have watched something that did not exist. Carry the
        # longest-lived pod's age so the excerpt can state the window it really
        # measured.
        if age is not None:
            entry["oldest_h"] = max(entry["oldest_h"] or 0.0, age * 24)

    hits = []
    for (kind, name), entry in by_owner.items():
        cpu_req_total = mem_req_total = 0.0
        cpu_lim_total = mem_lim_total = 0.0
        for pod in entry["pods"]:
            for req in pod["requests"]:
                cpu_req_total += parse_cpu_cores(str(req.get("cpu", "0"))) or 0
                mem_req_total += parse_mem_mib(str(req.get("memory", "0"))) or 0
            for lim in pod["limits"]:
                cpu_lim_total += parse_cpu_cores(str(lim.get("cpu", "0"))) or 0
                mem_lim_total += parse_mem_mib(str(lim.get("memory", "0"))) or 0
        if cpu_req_total == 0 and mem_req_total == 0:
            continue
        guaranteed = cpu_req_total == cpu_lim_total and mem_req_total == mem_lim_total and cpu_lim_total > 0

        peak_cpu = peak_mem = 0.0
        for pod in entry["pods"]:
            cpu, mem = usage_peaks.get((pod["ns"], pod["name"]), (0.0, 0.0))
            peak_cpu += cpu
            peak_mem += mem
        if cpu_req_total and peak_cpu / cpu_req_total > 0.2:
            continue
        if mem_req_total and peak_mem / mem_req_total > 0.2:
            continue

        delta_cpu = cpu_req_total - peak_cpu
        delta_mem_gib = (mem_req_total - peak_mem) / 1024.0
        if delta_cpu < 2 and delta_mem_gib < 4:
            continue

        oldest_h = entry["oldest_h"]
        window_h = USAGE_WINDOW_HOURS if oldest_h is None else min(USAGE_WINDOW_HOURS, round(oldest_h))
        severity = "major" if delta_cpu >= 8 or delta_mem_gib >= 32 else "minor"
        if autopilot and severity == "minor":
            severity = "major"

        hits.append(
            {
                "namespace": entry["ns"],
                "object": f"{kind}/{name}",
                "excerpt": f"requests {cpu_req_total:.2f} vCPU / {mem_req_total / 1024.0:.1f} GiB; peak observed {peak_cpu:.2f} vCPU / {peak_mem / 1024.0:.1f} GiB over the trailing {window_h}h (Cloud Monitoring)",
                "severity": severity,
                "_guaranteed": guaranteed,
            }
        )
    return hits


IMPACT = {
    "overrequest": "This controller reserves far more than it uses, so the scheduler and autoscaler size the cluster for capacity nothing needs.",
    "orphan-pv": "The backing disk still exists and no claim can bind it -- capacity paid for and unusable.",
    "unconsumed-pvc": "Provisioned storage sits bound with nothing reading or writing it.",
    "unattached-disk": "A persistent disk bills continuously whether or not anything is attached to it.",
    "idle-address": "A reserved external IP bills continuously whether or not anything answers on it.",
    "orphan-lb": "An orphaned forwarding rule keeps a load balancer, and usually an external IP, alive for nothing.",
    "idle-nodepool": "Nodes reserved by a non-zero autoscaler floor sit idle instead of being reclaimed.",
    "scaledown-blocked": "An unevictable pod on an under-allocated node blocks both scale-down and security patching.",
    "terminal-pods": "Finished objects accumulate in etcd and slow every full API-server list.",
    "idle-namespace": "A namespace with no running workload still holds a load balancer or bound storage.",
}


def _emit(slug: str, hit: dict) -> dict:
    return {
        "check": slug,
        "namespace": hit.get("namespace", ""),
        "object": hit["object"],
        "severity": hit["severity"],
        "excerpt": hit["excerpt"],
        "impact": IMPACT[slug],
        "needs_triage": None,
    }


def _fleet_facts(context: dict) -> dict:
    """What the project-scoped compute checks (3.4, 3.6) need to know about
    *this* cluster's live objects, so `collect_fleet` can union them across
    every cluster before running those checks -- a PV's backing disk or a
    Service a forwarding rule targets can live on any cluster in the
    project, not necessarily the one whose dump happened to mention it."""
    pv_handles = set()
    for pv in context["pvs"]:
        spec = pv.get("spec", {})
        handle = (spec.get("csi") or {}).get("volumeHandle") or (spec.get("gcePersistentDisk") or {}).get("pdName")
        if handle:
            pv_handles.add(handle.rsplit("/", 1)[-1])
    service_names = {
        f"{s.get('metadata', {}).get('namespace', '')}/{s.get('metadata', {}).get('name', '')}" for s in context["services"]
    }
    referenced_addresses = set()
    for svc in context["services"]:
        annotations = svc.get("metadata", {}).get("annotations") or {}
        for key in LB_ANNOTATION_KEYS:
            value = annotations.get(key)
            if value:
                referenced_addresses.update(v.strip() for v in value.split(","))
    return {"pv_handles": pv_handles, "service_names": service_names, "referenced_addresses": referenced_addresses}


def empty_fleet_facts() -> dict:
    """The `fleet_facts` half of a `collect_cluster` return for a cluster
    nothing could be read from."""
    return {"pv_handles": set(), "service_names": set(), "referenced_addresses": set()}


def crashed_entry(cluster: dict, exc: BaseException) -> dict:
    """A `clusters[]` entry for a worker that raised something unmodelled.

    `future.result()` re-raises, so one unhandled exception on one cluster
    aborts `collect_fleet` — and the SOP invokes this collector as
    `fleet_waste.py … > manifest_fleet-wide-cost-analysis.json`, so by then
    the shell has already truncated the file. The run loses the whole fleet to
    one bad object instead of one cluster. `gate-failed` is the shape the
    document already carries for "enumerated, could not be read", and it also
    closes §3.6's `all_reachable` gate for that project, which is the
    conservative answer when a cluster's objects went unseen.
    """
    print(
        f"[fleet_waste] {cluster.get('project', '?')}/{cluster.get('name', '?')}: "
        f"collector raised {type(exc).__name__}: {exc}",
        file=sys.stderr,
    )
    return {
        "name": cluster.get("name", "?"),
        "project": cluster.get("project", "?"),
        "location": cluster.get("location", "?"),
        "autopilot": bool(cluster.get("autopilot")),
        "outcome": "gate-failed",
        "error": f"collector raised {type(exc).__name__}: {exc}"[:300],
    }


def collect_cluster(cluster: dict, *, run: RunFn, session: SessionFn, now: datetime) -> tuple[dict, dict]:
    """Returns `(manifest_entry, fleet_facts)` — the second only populated
    when the object dump succeeded; `collect_fleet` unions it across every
    cluster before running the project-scoped checks that need it."""
    name, project, location = cluster["name"], cluster["project"], cluster["location"]
    # A cluster property `enumerate_clusters` already resolved, so it rides on
    # every shape below: the mode does not stop being true because this run
    # failed to read inside the cluster.
    mode = {"autopilot": bool(cluster.get("autopilot"))}
    empty_facts = empty_fleet_facts()
    kubeconfig, cred_run = fetch_credentials(project, name, location, run=run)
    if cred_run.rc != 0:
        return {"name": name, "project": project, "location": location, **mode, "outcome": "unreachable", "error": f"get-credentials rc={cred_run.rc}: {cred_run.stderr.strip()[:300]}"}, empty_facts

    dump_kinds = "nodes,pods,pvc,pv,svc,jobs,cronjobs,pdb,ns,resourcequota,sts"

    env = {**os.environ, "KUBECONFIG": str(kubeconfig)}
    dump_argv = ["kubectl", "get", dump_kinds, "-A", "-o", "json"]
    parsed, result = run_and_gate(dump_argv, run=run, env=env)
    if parsed is None:
        return {"name": name, "project": project, "location": location, **mode, "outcome": "gate-failed", "error": f"object dump gate failed (rc={result.rc}): {result.stderr.strip()[:300]}"}, empty_facts
    dump_record = _record(f"KUBECONFIG={kubeconfig} {shlex.join(dump_argv)}", result)
    context = build_context(parsed)
    fleet_facts = _fleet_facts(context)

    node_pools_argv = ["gcloud", "container", "node-pools", "list", "--cluster", name, "--location", location, "--project", project, "--format", "json"]
    # Gated, unlike the bare `run` this used to be. An unreadable node-pool
    # list -- denied, throttled, a bad `--location` -- parsed to `[]`, and a
    # cluster with no node pools has no idle ones, so 3.7 and 3.8 recorded
    # their command and reported nothing found. The evidence line carried the
    # non-zero rc, but nothing downstream reads it: the ledger said the pools
    # were checked and were fine.
    node_pools, pools_result = run_and_gate(node_pools_argv, run=run)
    pools_readable = node_pools is not None
    node_pools = node_pools or []
    pools_record = _record(shlex.join(node_pools_argv), pools_result)
    limitations: list[str] = []
    not_applicable: list[dict] = []

    usage_peaks, metrics_ok, usage_result = fetch_usage_peaks(project, name, session=session, now=now)
    usage_record = _record(usage_result.argv[0], usage_result)

    candidates = []
    commands = {
        "orphan-pv": dump_record, "unconsumed-pvc": dump_record, "terminal-pods": dump_record, "idle-namespace": dump_record,
    }
    candidates += [_emit("orphan-pv", h) for h in check_orphan_pv(context, now=now)]
    candidates += [_emit("unconsumed-pvc", h) for h in check_unconsumed_pvc(context, now=now)]
    candidates += [_emit("terminal-pods", h) for h in check_terminal_pods(context, now=now)]
    candidates += [_emit("idle-namespace", h) for h in check_idle_namespace(context, now=now)]

    # Autopilot owns its node pools, so 3.7/3.8 are inapplicable there. Only a
    # Standard cluster owes them, so only a Standard cluster can be short of
    # them -- claiming the limitation on Autopilot too would raise a gap for a
    # check that target does not owe, which is the double-counted disposition
    # 7301c594 removed.
    #
    # The collector declares that itself rather than leaving it to the model,
    # because the model has to remember *both* slugs and on 2026-08-29 it
    # remembered one: three Autopilot clusters came back with `idle-nodepool`
    # not-applicable and `scaledown-blocked` simply absent, which §6 reads --
    # correctly, on what it was given -- as a check nobody ran. The weekly
    # `fleet-wide-cost-analysis` published `partial: true` with three coverage
    # gaps naming a check that cannot exist on those clusters. `autopilot` is a
    # fact the collector already holds, so the disposition belongs here where it
    # is the same on every run.
    if not cluster.get("autopilot"):
        if pools_readable:
            commands["idle-nodepool"] = pools_record
            commands["scaledown-blocked"] = dump_record
            idle_pool_hits = check_idle_nodepool(context, node_pools, now=now)
            candidates += [_emit("idle-nodepool", h) for h in idle_pool_hits]
            candidates += [_emit("scaledown-blocked", h) for h in check_scaledown_blocked(context, idle_pool_hits)]
        else:
            limitations.append(
                f"idle-nodepool and scaledown-blocked could not be measured on "
                f"this cluster: `gcloud container node-pools list` failed "
                f"(rc={pools_result.rc}) — "
                f"{pools_result.stderr.strip()[:200] or 'no stderr'}"
            )
    else:
        not_applicable += [
            {
                "check": slug,
                "reason": (
                    "Autopilot manages this cluster's nodes and exposes no node "
                    "pools to size or to find scaledown-blocked, so the check has "
                    "no object to run against."
                ),
            }
            for slug in ("idle-nodepool", "scaledown-blocked")
        ]

    if metrics_ok:
        commands["overrequest"] = usage_record
        for hit in check_overrequest(context, usage_peaks, now=now, autopilot=bool(cluster.get("autopilot"))):
            emitted = _emit("overrequest", hit)
            if hit.get("_guaranteed"):
                emitted["needs_triage"] = "guaranteed-qos"
            candidates.append(emitted)
    elif not context["nodes"]:
        # A cluster with no nodes cannot be over-requesting: there is no
        # capacity for a reservation to waste, and nothing has run for a
        # request to be measured against. The usage read comes back empty
        # there for the same reason the cluster is empty -- no containers ran,
        # so none reported -- so the branch below would read a vacuum as a
        # degradation. On 2026-08-29 it did: `fleet-wide-cost-analysis`
        # published `partial: true` over two freshly created Autopilot peers
        # whose whole object set was fifteen Pending pods and no nodes. That is
        # 7301c594's failure again from the other side. A `partial` that fires
        # on every empty cluster is one operators learn to scroll past, and the
        # gap it hides next time will be a real one.
        not_applicable.append(
            {
                "check": "overrequest",
                "reason": (
                    "This cluster has no nodes, so no workload is scheduled and "
                    "no reservation is holding capacity: there is nothing for a "
                    "request to be over against. Cloud Monitoring returns no "
                    "container time series for it for the same reason -- nothing "
                    "ran to report any."
                ),
            }
        )
    else:
        # §2's metrics degradation. `overrequest` already dropped out of
        # `commands` on its own, so §6 was raising it as a gap with no reason
        # attached -- a reader saw the check named and had nothing to tell them
        # whether it was denied, throttled, or never attempted.
        limitations.append(
            f"overrequest could not be measured on this cluster: the Cloud "
            f"Monitoring usage read failed (rc={usage_result.rc}) — "
            f"{usage_result.stderr.strip()[:200] or 'no detail'}"
        )

    entry = {
        "name": name, "project": project, "location": location, **mode, "outcome": "collected",
        "commands": [{"check": slug, **record} for slug, record in commands.items()],
        "candidates": candidates,
    }
    if limitations:
        entry["limitations"] = "; ".join(limitations)
    if not_applicable:
        entry["checks_not_applicable"] = not_applicable
    return entry, fleet_facts


# --------------------------------------------------------------------------- #
# Project-scoped GCP compute checks (3.4, 3.5, 3.6)
# --------------------------------------------------------------------------- #

LB_ANNOTATION_KEYS = ("kubernetes.io/ingress.global-static-ip-name", "networking.gke.io/load-balancer-ip", "cloud.google.com/load-balancer-ip", "networking.gke.io/addresses")
NON_WASTE_ADDRESS_PURPOSES = {"GCE_ENDPOINT", "VPC_PEERING", "PRIVATE_SERVICE_CONNECT", "NAT_AUTO", "SHARED_LOADBALANCER_VIP", "IPSEC_INTERCONNECT"}


def _idle_since(disk: dict) -> tuple[str, str]:
    """When the disk stopped being used, and how the excerpt should say it.

    `creationTimestamp` is the wrong clock here for any disk that was ever
    attached. The SOP justifies the 30-day threshold as outliving "node
    upgrades, pod rescheduling, and maintenance windows" — churn, in other
    words — and none of those touch creation time. A boot disk created a year
    ago and detached this morning clears a creation-age filter with eleven
    months to spare, and it is exactly the churn the threshold exists to
    exclude. Reading it as waste would also make the excerpt's "unattached
    since" a false statement by eleven months.

    GCE stamps `lastDetachTimestamp` on every detach and omits it for a disk
    that has never been attached — the one case where creation really is the
    moment it went idle, and the excerpt says so rather than implying a detach
    that never happened.
    """
    detached = disk.get("lastDetachTimestamp")
    if detached:
        return str(detached), f"unattached since {detached}"
    created = disk.get("creationTimestamp", "")
    return str(created), f"never attached, created {created}"


def check_unattached_disk(disks: list[dict], live_pv_handles: set[str], *, now: datetime) -> list[dict]:
    hits = []
    for disk in disks:
        if disk.get("users"):
            continue
        idle_since, idle_phrase = _idle_since(disk)
        age = _age_days(idle_since, now=now)
        if age is None or age < 30:
            continue
        if disk.get("name", "") in live_pv_handles:
            continue
        size_gb = disk.get("sizeGb")
        size_gb = float(size_gb) if size_gb else 0
        disk_type = (disk.get("type") or "").lower()
        hits.append(
            {
                "object": f"Disk/{disk.get('name', '')}",
                "excerpt": f"{idle_phrase}{_ago(age)}, {size_gb:.0f} GB, {disk.get('type')} ({_scope_flag(disk)})",
                "severity": "major" if size_gb >= 500 or "ssd" in disk_type or "extreme" in disk_type else "minor",
            }
        )
    return hits


def _location_of(obj: dict) -> str:
    """`us-east4-a`, `us-east4`, or `global` for any compute resource.

    gcloud returns `zone` and `region` as full selfLink URLs and omits both keys
    for a global resource, so a finding that passes either field through carries
    a URL where a location belongs -- and nothing at all in the global case.
    Handles a bare name too, which is what some projections return.
    """
    for key in ("zone", "region"):
        value = obj.get(key) or ""
        if value:
            return value.rsplit("/", 1)[-1]
    return "global"


def _scope_flag(obj: dict) -> str:
    """The gcloud scope flag a remediation command for `obj` must carry.

    Every `gcloud compute` verb has to be told where to look, and getting it
    wrong does not fail loudly: with no flag gcloud resolves against whatever
    region it happens to be configured for, so a global address -- or a disk in
    another zone -- answers `was not found`. A reader takes that for a finding
    somebody has already remediated rather than a command written wrong, so the
    resource stays on the bill and a true finding is discredited. Emitting the
    flag next to the object is what keeps the agent from having to infer it.
    """
    location = _location_of(obj)
    if location == "global":
        return "--global"
    return f"--zone={location}" if obj.get("zone") else f"--region={location}"


#: How many of a roll-up's members the excerpt names before it stops. Enough to
#: start on without opening the console; short of the point where one finding's
#: evidence crowds the rest of the ledger out of the 60,000-character body §5
#: warns about.
ROLLUP_EXCERPT_MEMBERS = 12


def check_idle_address(addresses: list[dict], referenced_addresses: set[str], *, project: str, now: datetime) -> list[dict]:
    hits = []
    idle = []
    for addr in addresses:
        if addr.get("addressType") != "EXTERNAL" or addr.get("status") != "RESERVED":
            continue
        if (addr.get("purpose") or "") in NON_WASTE_ADDRESS_PURPOSES:
            continue
        if addr.get("name") in referenced_addresses or addr.get("address") in referenced_addresses:
            continue
        age = _age_days(addr.get("creationTimestamp", ""), now=now)
        if age is None or age < 14:
            continue
        idle.append((addr, age))
    if len(idle) >= 10:
        # §3.5's roll-up is per *project*, and §5 requires a roll-up to be named
        # after the scope it covers rather than after one of its members. It was
        # named `Address/rollup-<region of idle[0]>`: a region only the first
        # address is necessarily in -- so a project leaking addresses across
        # three regions published one finding claiming all of them were in one --
        # and an identity that moves the moment that address is released, which
        # re-announces the same leak as a new finding. The scope is the project.
        by_location: dict[str, list[str]] = {}
        for addr, _ in idle:
            by_location.setdefault(_location_of(addr), []).append(addr.get("name", ""))
        breakdown = ", ".join(f"{loc} ({len(names)})" for loc, names in sorted(by_location.items()))
        names = sorted(n for n in (a.get("name", "") for a, _ in idle) if n)
        shown = ", ".join(names[:ROLLUP_EXCERPT_MEMBERS])
        if len(names) > ROLLUP_EXCERPT_MEMBERS:
            shown += f", and {len(names) - ROLLUP_EXCERPT_MEMBERS} more"
        return [
            {
                "object": f"Project/{project}",
                "excerpt": f"{len(idle)} external addresses RESERVED and unattached across {breakdown}: {shown}",
                "severity": "major",
            }
        ]
    for addr, age in idle:
        hits.append({"object": f"Address/{addr.get('name', '')}", "excerpt": f"RESERVED and unattached since {addr.get('creationTimestamp')}{_ago(age)} ({_scope_flag(addr)})", "severity": "minor"})
    return hits


def check_orphan_lb(forwarding_rules: list[dict], target_pools: list[dict], backend_services: list[dict], known_services: set[str], *, now: datetime) -> list[dict]:
    hits = []
    # The GKE service controller writes this description as a JSON object --
    # `{"kubernetes.io/service-name":"ns/name","kubernetes.io/api-version":"v1"}`
    # -- so the key is followed by a closing quote before the colon. A pattern
    # requiring `service-name:` therefore matched no real forwarding rule at
    # all: every one of them fell through the `if not m: continue` and 3.6's
    # orphaned-rule leg has never emitted a finding against a live fleet. The
    # optional quotes accept both that shape and the bare `key: value` form the
    # SOP's own example uses.
    svc_name_re = re.compile(r"""kubernetes\.io/service-name["']?\s*:\s*["']?([\w.-]+/[\w.-]+)""")
    for rule in forwarding_rules:
        desc = rule.get("description", "") or ""
        m = svc_name_re.search(desc)
        if not m:
            continue
        if "multiclusteringress" in desc.lower() or "multiclusterservice" in desc.lower():
            continue
        if m.group(1) in known_services:
            continue
        age = _age_days(rule.get("creationTimestamp", ""), now=now)
        if age is None or age < 7:
            continue
        hits.append({"object": f"ForwardingRule/{rule.get('name', '')}", "excerpt": f"targets deleted Service {m.group(1)}, created {rule.get('creationTimestamp')}{_ago(age)} ({_scope_flag(rule)})", "severity": "major"})
    for pool in target_pools:
        if not pool.get("instances"):
            hits.append({"object": f"TargetPool/{pool.get('name', '')}", "excerpt": f"zero instances ({_scope_flag(pool)})", "severity": "major"})
    for backend in backend_services:
        if not backend.get("backends"):
            hits.append({"object": f"BackendService/{backend.get('name', '')}", "excerpt": f"zero backends ({_scope_flag(backend)})", "severity": "major"})
    return hits


def collect_project_compute(project: str, all_reachable: bool, fleet_facts: dict, *, run: RunFn, now: datetime) -> dict | None:
    # `--filter=-users:*` and not `"--filter", "-users:*"`: a filter value
    # starting with `-` reads as a flag to gcloud's own argument parser, which
    # then rejects the command for the argument it thinks is missing
    # (`argument --filter: expected one argument`, rc=2). This read therefore
    # failed on every run since it was written, and because the five reads below
    # gate as one it took the whole project target down with it -- every weekly
    # `fleet-wide-cost-analysis` published `project/<p>` as `gate-failed`, so
    # `unattached-disk` has never once been evaluated by the collector.
    disks_argv = ["gcloud", "compute", "disks", "list", "--project", project, "--filter=-users:*", "--format", "json"]
    disks_parsed, disks_result = run_and_gate(disks_argv, run=run)
    addr_argv = ["gcloud", "compute", "addresses", "list", "--project", project, "--filter", "status!=IN_USE", "--format", "json"]
    addr_parsed, addr_result = run_and_gate(addr_argv, run=run)
    fwd_argv = ["gcloud", "compute", "forwarding-rules", "list", "--project", project, "--format", "json"]
    fwd_parsed, fwd_result = run_and_gate(fwd_argv, run=run)
    tp_argv = ["gcloud", "compute", "target-pools", "list", "--project", project, "--format", "json"]
    tp_parsed, tp_result = run_and_gate(tp_argv, run=run)
    bs_argv = ["gcloud", "compute", "backend-services", "list", "--project", project, "--format", "json"]
    bs_parsed, bs_result = run_and_gate(bs_argv, run=run)

    # Name the read that failed and what it said. "one or more compute list
    # reads failed" was what this returned for as long as the disks filter was
    # broken, and it is the reason nobody noticed: five reads gate as one, the
    # message fingers none of them, and the only way to learn which had been
    # failing all along was to run all five by hand against a live project.
    failed = [
        f"{shlex.join(argv)} rc={result.rc}: {result.stderr.strip()[:200] or 'no stderr'}"
        for argv, parsed, result in (
            (disks_argv, disks_parsed, disks_result),
            (addr_argv, addr_parsed, addr_result),
            (fwd_argv, fwd_parsed, fwd_result),
            (tp_argv, tp_parsed, tp_result),
            (bs_argv, bs_parsed, bs_result),
        )
        if parsed is None
    ]
    if failed:
        return {
            "name": f"project/{project}",
            "project": project,
            "location": "global",
            "outcome": "gate-failed",
            "error": f"{len(failed)} of 5 compute list reads failed -- " + "; ".join(failed),
        }

    candidates = [_emit("unattached-disk", h) for h in check_unattached_disk(disks_parsed, fleet_facts["pv_handles"], now=now)]
    candidates += [_emit("idle-address", h) for h in check_idle_address(addr_parsed, fleet_facts["referenced_addresses"], project=project, now=now)]
    if all_reachable:
        candidates += [_emit("orphan-lb", h) for h in check_orphan_lb(fwd_parsed, tp_parsed, bs_parsed, fleet_facts["service_names"], now=now)]

    entry = {
        "name": f"project/{project}",
        "project": project,
        "location": "global",
        "outcome": "collected",
        "commands": [
            {"check": "unattached-disk", **_record(shlex.join(disks_argv), disks_result)},
            {"check": "idle-address", **_record(shlex.join(addr_argv), addr_result)},
        ]
        + ([{"check": "orphan-lb", **_record(shlex.join(fwd_argv), fwd_result)}] if all_reachable else []),
        "candidates": candidates,
    }
    if not all_reachable:
        # §6 already reports the missing check -- `orphan-lb` drops out of
        # `commands`, so the roster half of `coverage_gaps` names it whatever
        # this entry says in prose. What it cannot supply is why, and a gap
        # reading "orphan-lb did not run" sends a reader looking for a broken
        # gcloud read that is not there: the three compute reads all succeeded
        # and the check was withheld on purpose. §3.6 needs every cluster in
        # the project to say which Services exist, because a forwarding rule is
        # only orphaned if *no* cluster claims it -- so one unreadable cluster
        # would turn every load balancer it serves into a false positive. The
        # unreadable clusters are their own manifest entries, carrying the
        # stderr that explains each one.
        entry["limitations"] = (
            "orphan-lb was not evaluated for this project: §3.6 compares "
            "forwarding rules against the Service names of every cluster in "
            "the project, and this run holds a complete Service list for none "
            "of them -- either a cluster could not be read or the project "
            "contributed no readable cluster at all -- so a rule its Services "
            "reference would read as orphaned. See this project's cluster "
            "entries in this manifest for the reason each one failed."
        )
    return entry


def collect_fleet(project: str | None = None, *, run: RunFn = default_run, session: SessionFn = None, max_workers: int = MAX_WORKERS, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if session is None:
        # One session for the fleet: `AuthorizedSession` locks around token
        # refresh and its connection pool is thread-safe, and building one per
        # cluster would re-resolve ADC against the metadata server every time.
        # Failing to build one is not fatal -- `fetch_usage_peaks` turns a
        # `None` session into the same honest per-cluster limitation an API
        # error produces, and every check that reads object state still runs.
        try:
            session = default_monitoring_session()
        except Exception as exc:
            log(f"Cloud Monitoring credentials unavailable, overrequest will be skipped fleet-wide: {exc}")

    projects = get_target_projects(project, run=run)

    clusters: list[dict] = []
    unaudited: list[dict] = []
    for p in projects:
        try:
            running, not_running = enumerate_clusters(p, run=run)
            clusters.extend(running)
            unaudited.extend(not_running)
        except RuntimeError as exc:
            # A log line is not a record. The manifest is the only account of
            # what this run managed to read, and a project whose clusters could
            # not be listed used to leave nothing in it -- its `project/<p>`
            # compute entry still arrived as `collected`, so the document saw a
            # project with two of three checks and zero clusters, which is
            # exactly what a genuinely cluster-free project looks like. Recorded
            # as a target, the loss is something the document has to account for
            # and §6 turns it into a coverage gap.
            log(f"{p}: cluster enumeration failed, no clusters known from this project: {exc}")
            unaudited.append(
                {
                    "name": f"project/{p}/clusters",
                    "project": p,
                    "location": "global",
                    "outcome": "gate-failed",
                    "error": str(exc),
                }
            )

    results: list[tuple[dict, dict]] = [None] * len(clusters)
    with ThreadPoolExecutor(max_workers=max(1, min(len(clusters), max_workers))) as pool:
        futures = {pool.submit(collect_cluster, c, run=run, session=session, now=now): i for i, c in enumerate(clusters)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:  # noqa: BLE001 — see crashed_entry
                results[index] = (crashed_entry(clusters[index], exc), empty_fleet_facts())

    # Group per project: the "all reachable" gate for orphan-lb (§3.6) and
    # the cross-cluster fact union it and the disk/address checks read are
    # each scoped to one project, per the SOP's own per-project Do-NOT-flag
    # rule -- a cluster unreachable in project A must not suppress project
    # B's checks, and a PV handle from project A's cluster must not suppress
    # a genuinely unattached disk in project B.
    by_project: dict[str, list[tuple[dict, dict]]] = {}
    for cluster, result in zip(clusters, results):
        by_project.setdefault(cluster["project"], []).append(result)
    # The clusters that never reached a worker belong to the gate too. §3.6
    # withholds `orphan-lb` unless *every* cluster in the project was read, and
    # a DEGRADED or PROVISIONING cluster is one that was not: it goes straight
    # into `scope.skipped` per §2, and its Services are exactly the ones a
    # forwarding rule might still reference. Reading the gate off `by_project`
    # alone -- which only ever holds RUNNING clusters -- meant a project could
    # have a cluster in `scope.skipped` and still publish `orphan-lb` findings
    # against Service names it had not finished collecting, which is the false
    # positive §3.6 calls the highest-risk cross-check in the audit.
    skipped_by_project: dict[str, list[dict]] = {}
    for entry in unaudited:
        skipped_by_project.setdefault(entry.get("project", ""), []).append(entry)

    cluster_entries: list[dict] = []
    project_entries: list[dict] = []
    for p in projects:
        group = by_project.get(p, [])
        group_entries = [entry for entry, _ in group]
        cluster_entries.extend(group_entries)
        all_reachable = (
            bool(group_entries)
            and not skipped_by_project.get(p)
            and all(e["outcome"] == "collected" for e in group_entries)
        )
        fleet_facts = {"pv_handles": set(), "service_names": set(), "referenced_addresses": set()}
        for _, facts in group:
            for key in fleet_facts:
                fleet_facts[key] |= facts[key]
        project_entry = collect_project_compute(p, all_reachable, fleet_facts, run=run, now=now)
        if project_entry:
            project_entries.append(project_entry)

    return {
        "version": MANIFEST_VERSION,
        "audit": "fleet-wide-cost-analysis",
        "started_at": started_at,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "clusters": cluster_entries + project_entries + unaudited,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", help="single project to audit; omit to run §1's project discovery")
    args = parser.parse_args(argv)
    manifest = collect_fleet(args.project)
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
