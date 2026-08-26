#!/opt/hermes/.venv/bin/python3
"""collect.py — Procedural collector for the fleet-audit checks that are code
wearing prose.

See docs/designs/fleet-audit-collectors-and-status.md §4.2, §4.3, §6.

**Scope of this file today: the collector engine, plus one stream's check
table in full.** `obtainability-audit`'s complete eleven-check roster
(§3.1–§3.11 of `governance/obtainability_audit_sop.md`) is converted — every
check the SOP defines is fully mechanical (design §2, point 2: this stream
needed zero `needs_triage` judgment calls), so nothing was left on the SOP
side to skip. Every other stream's checks still run the way they do today,
by SOP prose executed as shell — this collector does not change that yet.
Converting one stream in full is proof the shape holds under its harder
cases too (label-selector matching for PDBs/HPAs/Services, a check whose
severity forks on which of two conditions fired, a check that iterates
objects other than workloads); converting the rest is the next several
phases in the design's §10 work breakdown, each its own PR, deliberately.

What this file does for the checks it covers:

  1. Enumerates the fleet (`gcloud container clusters list`).
  2. Fetches per-cluster credentials into an isolated kubeconfig — the same
     path convention every SOP already uses (`AGENTS.md`, "Cluster
     Credentials") — and dumps workload state once, behind a fail-closed
     `jq -e`-equivalent gate so a truncated or empty dump cannot read as a
     clean cluster.
  3. Runs every covered check's filter against that one dump, in parallel
     across clusters (a thread pool; each cluster's kubeconfig is a private
     file, so no cluster's read can bleed into another's — see §4.3).
  4. Emits the run manifest (§6): for every enumerated cluster, an outcome,
     the literal collection commands (so `checks_run` stays falsifiable —
     §4.1), and every candidate finding.

The agent's job on a covered check shrinks to: run this script, read the
manifest, and — because every check converted so far is fully mechanical,
needing no `needs_triage` judgment — copy each candidate into `findings.json`
with the recommendation prose the validator requires. Nothing here writes to
a cluster; every subprocess this module runs is `gcloud`/`kubectl` read
verbs, in the same register `command_policy.py` already allows an agent's
own shell to run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, NamedTuple

MANIFEST_VERSION = 1
SCRATCH_DIR = os.environ.get("FLEET_AUDIT_SCRATCH_DIR") or "/opt/data/scratch"
KUBECONFIG_DIR = Path(os.environ.get("HERMES_HOME") or "/opt/data") / ".kubeconfigs"
DEFAULT_TIMEOUT_S = 60
MAX_WORKERS = 8

# The fleet-wide system-namespace set (S1), spelled identically in every SOP
# that names it. Kept here rather than imported from audit_report.py: this
# script ships and runs standalone (see the module docstring), and a
# constant four SOPs already agree on is safer copied once than imported
# across a module boundary that changes what "the collector" depends on.
SYSTEM_NAMESPACES = frozenset(
    {
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "gmp-system",
        "gmp-public",
        "gke-gmp-system",
        "cnrm-system",
        "configconnector-operator-system",
        "krmapihosting-system",
        "istio-system",
        "asm-system",
        "anthos-identity-service",
        "gatekeeper-system",
        "composer-system",
    }
)


def _is_system_namespace(ns: str) -> bool:
    return (
        ns in SYSTEM_NAMESPACES
        or ns.startswith("gke-")
        or ns.startswith("config-management-")
    )


def log(msg: str) -> None:
    print(f"[collect] {msg}", file=sys.stderr, flush=True)


class Run(NamedTuple):
    """One subprocess's outcome, in the shape the manifest records it."""

    argv: list[str]
    rc: int
    stdout: str
    stderr: str
    duration_s: float


RunFn = Callable[..., Run]


def default_run(argv: list[str], *, env: dict | None = None, timeout: int = DEFAULT_TIMEOUT_S) -> Run:
    """The real subprocess call. Tests inject a fake in its place — every
    driver function below takes `run` as a parameter rather than calling
    `subprocess.run` directly, so nothing here needs a live cluster to test.
    """
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, env=env, timeout=timeout
        )
        return Run(argv, proc.returncode, proc.stdout, proc.stderr, time.monotonic() - t0)
    except subprocess.TimeoutExpired as exc:
        return Run(argv, 124, exc.stdout or "", exc.stderr or "", time.monotonic() - t0)


def enumerate_clusters(project: str, *, run: RunFn = default_run) -> list[dict]:
    """Every `RUNNING` cluster in `project`, as `{name, location, project,
    autopilot}`. Raises on a `gcloud` failure — an audit that could not
    enumerate the fleet has nothing to report against, the same rule
    `handle_start`'s callers already follow for a bare `gcloud` failure.
    """
    result = run(["gcloud", "container", "clusters", "list", "--project", project, "--format", "json"])
    if result.rc != 0:
        raise RuntimeError(f"cluster enumeration failed (rc={result.rc}): {result.stderr.strip()[:500]}")
    try:
        clusters = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"cluster enumeration returned non-JSON: {exc}") from exc
    return [
        {
            "name": c["name"],
            "location": c.get("location") or c.get("zone"),
            "project": project,
            "autopilot": bool((c.get("autopilot") or {}).get("enabled")),
        }
        for c in clusters
        if c.get("status") == "RUNNING"
    ]


def kubeconfig_path(project: str, cluster: str, location: str) -> Path:
    return KUBECONFIG_DIR / f"kubeconfig_{project}_{cluster}_{location}.yaml"


def fetch_credentials(
    project: str, cluster: str, location: str, *, run: RunFn = default_run
) -> tuple[Path, Run]:
    """Isolated per-cluster kubeconfig — the collector's own copy of the
    convention every SOP already follows, so a parallel read of one cluster
    cannot answer with another's contents (§4.3's per-command `KUBECONFIG`
    rule; never `export`, which is exactly the shared state that would let
    two threads race each other's context).
    """
    kc = kubeconfig_path(project, cluster, location)
    kc.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "KUBECONFIG": str(kc)}
    result = run(
        [
            "gcloud", "container", "clusters", "get-credentials", cluster,
            "--location", location, "--project", project,
        ],
        env=env,
    )
    return kc, result


DUMP_COMMAND_KINDS = "deployments,statefulsets,daemonsets,poddisruptionbudgets,horizontalpodautoscalers,services,limitranges"


def dump_state(
    kubeconfig: Path, cluster: str, *, run: RunFn = default_run
) -> tuple[Path, Run, bool]:
    """One JSON dump answering every §3 check, behind a fail-closed gate.

    The gate is the ai-security SOP's pattern (`ai_security_audit_sop.md:89`):
    a `kubectl` that failed leaves an empty or truncated file, and reading
    that as "zero workloads" is indistinguishable from a genuinely empty
    cluster unless something checks the dump is well-formed *before* any
    check trusts it. Returns `(dump_path, collection_run, gate_ok)` — the
    caller records `gate_ok is False` as `outcome: "gate-failed"`, never as a
    shorter candidate list.
    """
    dump_path = Path(SCRATCH_DIR) / f"wra_state_{cluster}.json"
    env = {**os.environ, "KUBECONFIG": str(kubeconfig)}
    result = run(
        ["kubectl", "get", DUMP_COMMAND_KINDS, "-A", "-o", "json"],
        env=env,
        timeout=DEFAULT_TIMEOUT_S,
    )
    gate_ok = False
    if result.rc == 0 and result.stdout.strip():
        try:
            parsed = json.loads(result.stdout)
            gate_ok = isinstance(parsed.get("items"), list)
        except json.JSONDecodeError:
            gate_ok = False
    if gate_ok:
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        dump_path.write_text(result.stdout, encoding="utf-8")
    return dump_path, result, gate_ok


def output_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# obtainability-audit: §3.1 `no-requests`, §3.2 `no-memory-limit`.
#
# Both are pure functions over the parsed dump — no subprocess, no cluster
# access — which is what "detection is code that happens to be written in
# prose" (design §2, point 2) means concretely: everything below is a direct
# transcription of the SOP's "Flag when" / "Do NOT flag" prose, and it is
# exercised by golden-dump tests the prose alone could never have.
# --------------------------------------------------------------------------- #

WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet")
OPT_OUT_KEY = "kubeagents.x-k8s.io/reliability-audit"


def normalize_workloads(dump: dict) -> list[dict]:
    """Every workload template surviving S1–S5, as `{kind, ns, name, spec}`.

    Templates, not live pods (`spec.template.spec`) — admission-time
    defaulting never reaches here, matching the SOP's own reason for reading
    templates over Pods.
    """
    out = []
    for item in dump.get("items", []) or []:
        if item.get("kind") not in WORKLOAD_KINDS:
            continue
        meta = item.get("metadata") or {}
        ns = meta.get("namespace", "")
        if _is_system_namespace(ns):  # S1
            continue
        labels = meta.get("labels") or {}
        annotations = meta.get("annotations") or {}
        if "addonmanager.kubernetes.io/mode" in labels:  # S2
            continue
        if meta.get("ownerReferences"):  # S3
            continue
        if labels.get(OPT_OUT_KEY) == "exempt" or annotations.get(OPT_OUT_KEY) == "exempt":  # S4
            continue
        spec = item.get("spec") or {}
        if spec.get("replicas") == 0:  # S5 (Job/CronJob ownership is covered by S3 above)
            continue
        template_meta = (spec.get("template") or {}).get("metadata") or {}
        template_spec = (spec.get("template") or {}).get("spec") or {}
        out.append(
            {
                "kind": item["kind"],
                "ns": ns,
                "name": meta.get("name", ""),
                "spec": spec,
                "template": template_spec,
                "pod_labels": template_meta.get("labels") or {},
            }
        )
    return out


def selector_matches(selector: dict, labels: dict) -> bool:
    """Kubernetes `LabelSelector` semantics: `matchLabels` (exact) and every
    `matchExpressions` term are ANDed together. An absent or empty selector
    matches every pod in the namespace — the exact footgun 3.3's remediation
    guards against emitting, not a bug to guard against here; callers that
    read a *live* selector into this function are the ones responsible for
    that check.
    """
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr.get("key"), expr.get("operator"), expr.get("values") or []
        if op == "In" and labels.get(key) not in values:
            return False
        if op == "NotIn" and labels.get(key) in values:
            return False
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
    return True


def _by_namespace(dump: dict, kind: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for item in dump.get("items", []) or []:
        if item.get("kind") != kind:
            continue
        ns = (item.get("metadata") or {}).get("namespace", "")
        out.setdefault(ns, []).append(item)
    return out


def limitranges_by_namespace(dump: dict) -> dict[str, list[dict]]:
    return _by_namespace(dump, "LimitRange")


def pdbs_by_namespace(dump: dict) -> dict[str, list[dict]]:
    return _by_namespace(dump, "PodDisruptionBudget")


def services_by_namespace(dump: dict) -> dict[str, list[dict]]:
    return _by_namespace(dump, "Service")


def hpas_by_namespace(dump: dict) -> dict[str, list[dict]]:
    """Excludes KEDA-owned HPAs (`ownerReferences` present) — the same S3
    reasoning `normalize_workloads` applies to workloads: a KEDA
    `ScaledObject` owns the real configuration, and this audit does not read
    CRDs."""
    return {
        ns: [hpa for hpa in hpas if not (hpa.get("metadata") or {}).get("ownerReferences")]
        for ns, hpas in _by_namespace(dump, "HorizontalPodAutoscaler").items()
    }


def build_context(dump: dict, workloads: list[dict]) -> dict:
    return {
        "limitranges": limitranges_by_namespace(dump),
        "pdbs": pdbs_by_namespace(dump),
        "hpas": hpas_by_namespace(dump),
        "services": services_by_namespace(dump),
        "workloads": workloads,
    }


def selected_by_a_service(workload: dict, context: dict) -> bool:
    for svc in context["services"].get(workload["ns"], []):
        spec = svc.get("spec") or {}
        selector = spec.get("selector")
        if spec.get("type") == "ExternalName" or not selector:
            continue
        if selector_matches({"matchLabels": selector}, workload["pod_labels"]):
            return True
    return False


def _has_default(limitranges: dict, ns: str, field: str, resource: str) -> bool:
    """`field` is `"default"` (a limit) or `"defaultRequest"` (a request)."""
    for lr in limitranges.get(ns, []):
        for limit in (lr.get("spec") or {}).get("limits") or []:
            if resource in (limit.get(field) or {}):
                return True
    return False


def _effective_containers(workload: dict) -> list[dict]:
    """Regular containers plus native sidecars — `initContainers` with
    `restartPolicy: Always` count toward the pod's effective request, per
    §3.1. Plain init containers never do."""
    containers = list(workload["template"].get("containers") or [])
    for ic in workload["template"].get("initContainers") or []:
        if ic.get("restartPolicy") == "Always":
            containers.append(ic)
    return containers


def check_no_requests(workload: dict, context: dict) -> dict | None:
    limitranges = context["limitranges"]
    missing_by_container = {}
    for container in _effective_containers(workload):
        requests = ((container.get("resources") or {}).get("requests")) or {}
        missing = [
            resource
            for resource in ("cpu", "memory")
            if resource not in requests
            and not _has_default(limitranges, workload["ns"], "defaultRequest", resource)
        ]
        if missing:
            missing_by_container[container.get("name", "")] = missing
    if not missing_by_container:
        return None
    return {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": "; ".join(f"{c}: missing {','.join(m)}" for c, m in missing_by_container.items()),
    }


def check_no_memory_limit(workload: dict, context: dict) -> dict | None:
    missing = [
        container.get("name", "")
        for container in workload["template"].get("containers") or []
        if "memory" not in ((container.get("resources") or {}).get("limits") or {})
    ]
    if not missing or _has_default(context["limitranges"], workload["ns"], "default", "memory"):
        return None
    return {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": f"containers missing a memory limit: {', '.join(missing)}",
    }


def check_no_pdb(workload: dict, context: dict) -> dict | None:
    if workload["kind"] == "DaemonSet":
        return None
    replicas = workload["spec"].get("replicas", 1) or 1
    if replicas < 2:
        return None
    for pdb in context["pdbs"].get(workload["ns"], []):
        if selector_matches((pdb.get("spec") or {}).get("selector") or {}, workload["pod_labels"]):
            return None
    return {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": f"replicas={replicas}, no PodDisruptionBudget matches this workload's pod labels",
    }


def check_blocking_pdb(context: dict) -> list[dict]:
    """Cluster-scoped: iterates PDBs, not workloads, because the finding is
    about the PDB's own shape. A PDB matching no workload (orphaned) or a
    workload scaled to zero is deliberately left unreported here — the SOP
    rates the first `minor` at most and the second not at all, and this
    conversion covers the `critical` drain-blocking case, not every PDB
    config-rot shape. Multi-match (more than one workload sharing a PDB's
    selector) reports against the first match; the SOP does not specify a
    tie-break and this is conservative — under-reporting a real block is
    unlikely when the norm is one PDB per workload.
    """
    hits = []
    for ns, pdbs in context["pdbs"].items():
        for pdb in pdbs:
            spec = pdb.get("spec") or {}
            name = (pdb.get("metadata") or {}).get("name", "")
            max_unavailable, min_available = spec.get("maxUnavailable"), spec.get("minAvailable")
            selector = spec.get("selector") or {}
            matched = next(
                (
                    wl
                    for wl in context["workloads"]
                    if wl["ns"] == ns and selector_matches(selector, wl["pod_labels"])
                ),
                None,
            )
            replicas = matched["spec"].get("replicas", 1) or 1 if matched else None
            blocking = max_unavailable in (0, "0%") or min_available == "100%"
            if isinstance(min_available, int) and matched is not None and min_available >= replicas:
                blocking = True
            if not blocking or matched is None or replicas == 0:
                continue
            hits.append(
                {
                    "namespace": ns,
                    "object": f"PodDisruptionBudget/{name}",
                    "excerpt": f"maxUnavailable={max_unavailable!r} minAvailable={min_available!r} "
                    f"against {matched['kind']}/{matched['name']} (replicas={replicas})",
                }
            )
    return hits


def check_no_hpa(workload: dict, context: dict) -> dict | None:
    if workload["kind"] != "Deployment":
        return None
    replicas = workload["spec"].get("replicas", 1) or 1
    if replicas < 3:
        return None
    for hpa in context["hpas"].get(workload["ns"], []):
        target = (hpa.get("spec") or {}).get("scaleTargetRef") or {}
        if (
            target.get("apiVersion") == "apps/v1"
            and target.get("kind") == "Deployment"
            and target.get("name") == workload["name"]
        ):
            return None
    return {
        "object": f"Deployment/{workload['name']}",
        "excerpt": f"replicas={replicas}, no HorizontalPodAutoscaler targets this Deployment",
    }


def check_hpa_cannot_scale(context: dict) -> list[dict]:
    """Cluster-scoped: two independent flag conditions on the HPA itself,
    (a) `major` — pinned (`minReplicas == maxReplicas`) and (b) `minor` —
    dangling (target absent from the dump). Severity rides on the hit, not
    the check table default, because the two sub-cases disagree."""
    known = {(wl["ns"], wl["kind"], wl["name"]) for wl in context["workloads"]}
    hits = []
    for ns, hpas in context["hpas"].items():
        for hpa in hpas:
            name = (hpa.get("metadata") or {}).get("name", "")
            spec = hpa.get("spec") or {}
            min_r, max_r = spec.get("minReplicas"), spec.get("maxReplicas")
            target = spec.get("scaleTargetRef") or {}
            if min_r is not None and min_r == max_r:
                hits.append(
                    {
                        "namespace": ns,
                        "object": f"HorizontalPodAutoscaler/{name}",
                        "excerpt": f"minReplicas == maxReplicas == {min_r}; autoscaling is cosmetic",
                        "severity": "major",
                    }
                )
                continue
            target_key = (ns, target.get("kind"), target.get("name"))
            if target.get("kind") in ("Deployment", "StatefulSet") and target_key not in known:
                hits.append(
                    {
                        "namespace": ns,
                        "object": f"HorizontalPodAutoscaler/{name}",
                        "excerpt": f"scaleTargetRef {target.get('kind')}/{target.get('name')} not found",
                        "severity": "minor",
                    }
                )
    return hits


_HOSTNAME_KEY = "kubernetes.io/hostname"
_ZONE_KEY = "topology.kubernetes.io/zone"


def check_rigid_scheduling(workload: dict, context: dict) -> dict | None:
    node_selector = workload["template"].get("nodeSelector") or {}
    hits = []
    if _HOSTNAME_KEY in node_selector:
        hits.append(("critical", f"nodeSelector pins {_HOSTNAME_KEY}={node_selector[_HOSTNAME_KEY]}"))
    zone = node_selector.get(_ZONE_KEY)
    zonal_storage = workload["kind"] == "StatefulSet" and bool(workload["spec"].get("volumeClaimTemplates"))
    if zone and not zonal_storage:
        hits.append(("major", f"nodeSelector pins {_ZONE_KEY}={zone}"))
    required = (
        (workload["template"].get("affinity") or {}).get("nodeAffinity") or {}
    ).get("requiredDuringSchedulingIgnoredDuringExecution") or {}
    for term in required.get("nodeSelectorTerms") or []:
        for expr in term.get("matchExpressions") or []:
            values = expr.get("values") or []
            if expr.get("key") == _HOSTNAME_KEY and len(values) == 1:
                hits.append(("critical", f"nodeAffinity pins {_HOSTNAME_KEY}={values[0]}"))
            elif expr.get("key") == _ZONE_KEY and len(values) == 1 and not zonal_storage:
                hits.append(("major", f"nodeAffinity pins {_ZONE_KEY}={values[0]}"))
    if not hits:
        return None
    hits.sort(key=lambda h: 0 if h[0] == "critical" else 1)  # report the worse of the two, if both fire
    severity, excerpt = hits[0]
    return {"object": f"{workload['kind']}/{workload['name']}", "excerpt": excerpt, "severity": severity}


def check_no_spread(workload: dict, context: dict) -> dict | None:
    if workload["kind"] == "DaemonSet":
        return None
    replicas = workload["spec"].get("replicas", 1) or 1
    if replicas < 2:
        return None
    if workload["template"].get("topologySpreadConstraints"):
        return None
    anti_affinity = ((workload["template"].get("affinity") or {}).get("podAntiAffinity")) or {}

    def keyed_on_topology(terms, preferred):
        for entry in terms:
            term = entry.get("podAffinityTerm", entry) if preferred else entry
            if term.get("topologyKey") in (_HOSTNAME_KEY, _ZONE_KEY):
                return True
        return False

    required = anti_affinity.get("requiredDuringSchedulingIgnoredDuringExecution") or []
    preferred = anti_affinity.get("preferredDuringSchedulingIgnoredDuringExecution") or []
    if keyed_on_topology(required, False) or keyed_on_topology(preferred, True):
        return None
    return {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": f"replicas={replicas}, no topologySpreadConstraints or podAntiAffinity",
    }


_SELF_HEALTH_SIDECARS = {"istio-proxy", "cloud-sql-proxy", "gke-metadata-server"}


def check_probes_readiness(workload: dict, context: dict) -> dict | None:
    if not selected_by_a_service(workload, context):
        return None
    missing = [
        c.get("name", "")
        for c in workload["template"].get("containers") or []
        if c.get("name") not in _SELF_HEALTH_SIDECARS and not c.get("readinessProbe")
    ]
    if not missing:
        return None
    return {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": f"Service-backed, containers missing a readiness probe: {', '.join(missing)}",
    }


def check_probes_liveness(workload: dict, context: dict) -> dict | None:
    missing = [
        c.get("name", "")
        for c in workload["template"].get("containers") or []
        if c.get("name") not in _SELF_HEALTH_SIDECARS and not c.get("livenessProbe")
    ]
    if not missing:
        return None
    return {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": f"containers missing a liveness probe: {', '.join(missing)}",
    }


def check_single_replica(workload: dict, context: dict) -> dict | None:
    if workload["kind"] != "Deployment":
        return None
    if (workload["spec"].get("replicas", 1) or 1) != 1:
        return None
    if (workload["spec"].get("strategy") or {}).get("type") == "Recreate":
        return None
    if not selected_by_a_service(workload, context):
        return None
    return {"object": f"Deployment/{workload['name']}", "excerpt": "single replica, Service-backed"}


class CheckSpec(NamedTuple):
    slug: str
    kind: str  # "workload": run(workload, context) -> hit|None, one call per workload.
    #             "cluster": run(context) -> list[hit], one call per cluster.
    run: Callable
    severity: str  # A hit's own "severity" key overrides this (§3.4, §3.6, §3.7's two-condition checks).
    autopilot_severity: str | None  # None: severity is mode-independent
    impact: str


OBTAINABILITY_CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec(
        "no-requests",
        "workload",
        check_no_requests,
        "major",
        "minor",
        "The scheduler and cluster autoscaler size this cluster as if this "
        "workload costs nothing; its pods are the first evicted under node "
        "pressure and its cost cannot be attributed.",
    ),
    CheckSpec(
        "no-memory-limit",
        "workload",
        check_no_memory_limit,
        "major",
        None,
        "A memory leak here is absorbed by the node, not by this pod — the "
        "kubelet evicts co-located workloads first.",
    ),
    CheckSpec(
        "no-pdb",
        "workload",
        check_no_pdb,
        "major",
        None,
        "Nothing constrains the eviction API, so a single node drain during "
        "an upgrade can terminate every replica at once.",
    ),
    CheckSpec(
        "blocking-pdb",
        "cluster",
        check_blocking_pdb,
        "critical",
        None,
        "Blocks every node drain in this cluster indefinitely: node-pool "
        "upgrades, node auto-repair, and autoscaler scale-down all stall "
        "until a human deletes or edits this PDB.",
    ),
    CheckSpec(
        "no-hpa",
        "workload",
        check_no_hpa,
        "minor",
        None,
        "Capacity is pinned at a hand-chosen number: the workload cannot "
        "absorb a traffic spike and cannot give capacity back when idle.",
    ),
    CheckSpec(
        "hpa-cannot-scale",
        "cluster",
        check_hpa_cannot_scale,
        "major",  # overridden per hit; see check_hpa_cannot_scale
        None,
        "An HPA is attached but cannot scale this workload in either "
        "direction, or targets an object that no longer exists.",
    ),
    CheckSpec(
        "rigid-scheduling",
        "workload",
        check_rigid_scheduling,
        "major",  # overridden per hit: critical for a hostname pin
        None,
        "This pod's scheduling is pinned to one node or one zone: the next "
        "node upgrade, repair, or zonal event takes it down and it may not "
        "come back.",
    ),
    CheckSpec(
        "no-spread",
        "workload",
        check_no_spread,
        "minor",
        None,
        "Nothing guarantees these replicas are on different nodes; losing "
        "one node can take the whole workload out despite the replica count.",
    ),
    CheckSpec(
        "probes-readiness",
        "workload",
        check_probes_readiness,
        "major",
        None,
        "Every rollout sends production traffic to pods that are not yet "
        "serving, and a broken new version is never detected as broken.",
    ),
    CheckSpec(
        "probes-liveness",
        "workload",
        check_probes_liveness,
        "minor",
        None,
        "A wedged process is never restarted automatically; recovery "
        "requires a human.",
    ),
    CheckSpec(
        "single-replica",
        "workload",
        check_single_replica,
        "minor",
        None,
        "Zero-downtime is impossible: every rollout, node drain, and node "
        "repair is a full outage for this service.",
    ),
)

CHECK_TABLES: dict[str, tuple[CheckSpec, ...]] = {"obtainability-audit": OBTAINABILITY_CHECKS}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def collect_cluster(
    cluster: dict, checks: tuple[CheckSpec, ...], *, run: RunFn = default_run
) -> dict:
    """One manifest `clusters[]` entry (§6): every enumerated cluster gets
    one, whatever happened — `outcome` says which of the three shapes it is.
    """
    name, project, location = cluster["name"], cluster["project"], cluster["location"]
    kubeconfig, cred_run = fetch_credentials(project, name, location, run=run)
    if cred_run.rc != 0:
        return {
            "name": name, "project": project, "location": location,
            "outcome": "unreachable",
            "error": f"get-credentials rc={cred_run.rc}: {cred_run.stderr.strip()[:300]}",
        }

    dump_path, dump_run, gate_ok = dump_state(kubeconfig, name, run=run)
    if not gate_ok:
        return {
            "name": name, "project": project, "location": location,
            "outcome": "gate-failed",
            "error": f"dump gate failed (rc={dump_run.rc}): {dump_run.stderr.strip()[:300]}",
        }

    dump = json.loads(dump_path.read_text(encoding="utf-8"))
    workloads = normalize_workloads(dump)
    context = build_context(dump, workloads)
    collection_command = f"KUBECONFIG={kubeconfig} kubectl get {DUMP_COMMAND_KINDS} -A -o json"
    digest = output_digest(dump_run.stdout)

    commands = [
        {
            "check": spec.slug,
            "command": collection_command,
            "rc": dump_run.rc,
            "duration_s": round(dump_run.duration_s, 2),
            "output_sha256": digest,
        }
        for spec in checks
    ]

    def emit(spec: CheckSpec, hit: dict, default_namespace: str) -> dict:
        severity = hit.get("severity") or spec.severity
        impact = spec.impact
        if severity == spec.severity and cluster.get("autopilot") and spec.autopilot_severity:
            severity = spec.autopilot_severity
            impact = f"{impact} (Autopilot: severity downgraded — the platform injects requests at admission.)"
        return {
            "check": spec.slug,
            "cluster": name,
            "namespace": hit.get("namespace", default_namespace),
            "object": hit["object"],
            "severity": severity,
            "excerpt": hit["excerpt"],
            "impact": impact,
            "needs_triage": None,
        }

    candidates = []
    for spec in checks:
        if spec.kind == "workload":
            for workload in workloads:
                hit = spec.run(workload, context)
                if hit is not None:
                    candidates.append(emit(spec, hit, workload["ns"]))
        else:
            for hit in spec.run(context):
                candidates.append(emit(spec, hit, hit.get("namespace", "")))

    return {
        "name": name, "project": project, "location": location,
        "outcome": "collected",
        "commands": commands,
        "candidates": candidates,
    }


def collect_fleet(
    audit_id: str,
    project: str,
    *,
    run: RunFn = default_run,
    max_workers: int = MAX_WORKERS,
) -> dict:
    checks = CHECK_TABLES.get(audit_id)
    if not checks:
        raise ValueError(f"no check table for {audit_id!r} yet — see this file's module docstring")

    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    clusters = enumerate_clusters(project, run=run)

    results = [None] * len(clusters)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(collect_cluster, cluster, checks, run=run): index
            for index, cluster in enumerate(clusters)
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()

    return {
        "version": MANIFEST_VERSION,
        "audit": audit_id,
        "started_at": started_at,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "clusters": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("audit", choices=sorted(CHECK_TABLES))
    parser.add_argument("--project", required=True)
    args = parser.parse_args(argv)
    manifest = collect_fleet(args.audit, args.project)
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
