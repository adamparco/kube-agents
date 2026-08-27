#!/opt/hermes/.venv/bin/python3
"""collect.py — Procedural collector for the fleet-audit checks that are code
wearing prose.

See docs/designs/fleet-audit-collectors-and-status.md §4.2, §4.3, §6.

**Scope of this file today: the collector engine, plus three streams' check
tables in full.** `obtainability-audit`'s complete eleven-check roster
(§3.1–§3.11 of `governance/obtainability_audit_sop.md`), `compliance-audit`'s
complete eleven-check roster (§2.1–§2.11 of `governance/compliance_audit_sop.md`),
and `ai-security-audit`'s complete six-check roster (§3.1–§3.6 of
`governance/ai_security_audit_sop.md`) are all converted — every check any of
the three SOPs defines is fully mechanical (design §2, point 2: none needed a
`needs_triage` judgment call, including ai-security's §3.4, whose severity
forks on whether the same container also trips §3.2 — a fact both checks can
compute from the same dump), so nothing was left on the SOP side to skip.
Every other stream's checks still run the way they do today, by SOP prose
executed as shell — this collector does not change that yet. The three
streams collect in genuinely different shapes: obtainability answers every
check from one dump; compliance reads a workload dump plus RBAC, NetworkPolicy
and Namespace, ServiceAccount, and two distinct `gcloud` calls, none of them
optional; ai-security reads a workload dump and a Service dump, the second
backing exactly one check (`inference-endpoint-public`) that joins it against
the first. `collect_cluster` is a thin dispatcher over a per-stream *context
builder* for exactly this reason — the engine below is what every stream
shares, not what the first one happened to need. Converting the rest is the
next several phases in the design's §10 work breakdown, each its own PR,
deliberately.

What this file does for the checks it covers:

  1. Enumerates the fleet (`gcloud container clusters list`).
  2. Fetches per-cluster credentials into an isolated kubeconfig — the same
     path convention every SOP already uses (`AGENTS.md`, "Cluster
     Credentials") — then runs the stream's context builder: one dump for
     obtainability, several distinct `kubectl`/`gcloud` reads for
     compliance, two dumps for ai-security, each behind its own fail-closed
     `jq -e`-equivalent gate so a truncated or empty result cannot read as a
     clean cluster.
  3. Runs every covered check's filter against the collected context, in
     parallel across clusters (a thread pool; each cluster's kubeconfig is a
     private file, so no cluster's read can bleed into another's — see
     §4.3).
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
import re
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


def run_and_gate(argv: list[str], kubeconfig: Path, *, run: RunFn = default_run) -> tuple[dict | None, Run]:
    """One collection command, behind a fail-closed gate.

    The gate is the ai-security SOP's pattern (`ai_security_audit_sop.md:89`):
    a `kubectl`/`gcloud` that failed leaves empty or truncated output, and
    reading that as "nothing here" is indistinguishable from a genuinely
    empty result unless something checks the output is well-formed *before*
    any check trusts it. Returns `(parsed_json_or_None, command_run)` —
    `None` means the gate failed; the caller records that as
    `outcome: "gate-failed"` for the whole cluster, never as a shorter
    candidate list from the checks that happened to run first.

    A `kubectl get <kinds> ... -o json` list response gates on `.items`
    being a list; a `gcloud ... --format=json(...)` object response has no
    such envelope, so it gates on parsing as an object at all. Both share
    the same failure mode this function exists to catch: exit 0 with
    truncated or empty output, which the 4 MiB credential-proxy cap makes a
    real possibility, not a theoretical one.
    """
    env = {**os.environ, "KUBECONFIG": str(kubeconfig)}
    result = run(argv, env=env, timeout=DEFAULT_TIMEOUT_S)
    if result.rc != 0 or not result.stdout.strip():
        return None, result
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, result
    if "get" in argv and isinstance(parsed, dict) and not isinstance(parsed.get("items"), list):
        return None, result
    if not isinstance(parsed, (dict, list)):
        return None, result
    return parsed, result


class GateFailure(Exception):
    """Raised by a stream's context builder when a required collection
    command fails its gate. `collect_cluster` turns this into
    `outcome: "gate-failed"` for the whole cluster — a stream that collects
    from several commands fails closed on any one of them, the same way a
    single-dump stream does; a partially-gated cluster is not a smaller
    success, it is the false-all-clear shape at a different scale."""


def dump_state(
    kubeconfig: Path, cluster: str, *, run: RunFn = default_run
) -> tuple[Path, Run, bool]:
    """`obtainability-audit`'s one dump, behind `run_and_gate`. Kept as its
    own function (rather than inlined into its context builder) because its
    fixed dump-to-a-named-file shape predates the multi-collection builder
    contract and nothing else needs a file on disk — every check reads the
    parsed dict `run_and_gate` already returns.
    """
    dump_path = Path(SCRATCH_DIR) / f"wra_state_{cluster}.json"
    parsed, result = run_and_gate(["kubectl", "get", DUMP_COMMAND_KINDS, "-A", "-o", "json"], kubeconfig, run=run)
    if parsed is not None:
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        dump_path.write_text(result.stdout, encoding="utf-8")
    return dump_path, result, parsed is not None


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


# --------------------------------------------------------------------------- #
# compliance-audit: all eleven checks of §2.
#
# Unlike obtainability's Deployment/StatefulSet/DaemonSet templates, this
# stream's workload dump includes bare Pods (owned ones excluded — audit the
# owning controller, per the SOP's own reasoning) and reads `.spec` directly,
# resolved per kind: a Pod's `.spec` *is* the pod spec; a CronJob's is nested
# two objects deeper. `_pod_spec_of` is that resolution, done once instead of
# three times.
# --------------------------------------------------------------------------- #

COMPLIANCE_WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "CronJob", "Pod")
COMPLIANCE_DUMP_KINDS = "deploy,sts,ds,cronjob,pod"
_SYSTEM_SA_NAMESPACE_RE_PARTS = (
    "kube-system", "gmp-system", "cnrm-system", "configconnector-operator-system", "krmapihosting-system",
)


def _pod_spec_of(item: dict) -> dict:
    kind, spec = item.get("kind"), item.get("spec") or {}
    if kind == "Pod":
        return spec
    if kind == "CronJob":
        return (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
    return (spec.get("template") or {}).get("spec") or {}


def normalize_compliance_workloads(dump: dict) -> list[dict]:
    """Every object surviving compliance's universal suppressions, as
    `{kind, ns, name, spec}` where `spec` is the resolved **pod** spec —
    compliance's checks read `securityContext`, `hostNetwork`, `volumes`,
    never the owning object's own fields. Bare Pods are included and owned
    ones excluded (audit the controller, never the pod, whose name carries a
    random suffix) — the opposite inclusion rule from `normalize_workloads`,
    which drops Pods from its kind list entirely because obtainability reads
    templates, not live objects.
    """
    out = []
    for item in dump.get("items", []) or []:
        if item.get("kind") not in COMPLIANCE_WORKLOAD_KINDS:
            continue
        meta = item.get("metadata") or {}
        ns = meta.get("namespace", "")
        if _is_system_namespace(ns):
            continue
        labels = meta.get("labels") or {}
        if "addonmanager.kubernetes.io/mode" in labels:
            continue
        if (meta.get("annotations") or {}).get("components.gke.io/component-name"):
            continue
        if item.get("kind") == "Pod" and meta.get("ownerReferences"):
            continue
        out.append({"kind": item["kind"], "ns": ns, "name": meta.get("name", ""), "spec": _pod_spec_of(item)})
    return out


def check_privileged_container(workload: dict, context: dict) -> dict | None:
    bad = [
        c.get("name", "")
        for c in (workload["spec"].get("containers") or []) + (workload["spec"].get("initContainers") or [])
        if (c.get("securityContext") or {}).get("privileged") is True
        or "SYS_ADMIN" in ((c.get("securityContext") or {}).get("capabilities") or {}).get("add", [])
    ]
    if not bad:
        return None
    return {"object": f"{workload['kind']}/{workload['name']}", "excerpt": f"privileged/SYS_ADMIN: {', '.join(bad)}"}


def check_host_namespace(workload: dict, context: dict) -> dict | None:
    spec = workload["spec"]
    host_pid, host_ipc, host_net = bool(spec.get("hostPID")), bool(spec.get("hostIPC")), bool(spec.get("hostNetwork"))
    if not (host_pid or host_ipc or host_net):
        return None
    severity = "critical" if (host_pid or host_ipc) else "major"
    has_host_port = any(p.get("hostPort") for c in spec.get("containers") or [] for p in c.get("ports") or [])
    if severity == "major" and workload["kind"] == "DaemonSet" and has_host_port:
        # §2.2's ingress/gateway data-plane downgrade: hostNetwork is the
        # only flag set and a hostPort is declared -- record it rather than
        # suppressing silently.
        severity = "minor"
    return {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": f"hostNetwork={host_net} hostPID={host_pid} hostIPC={host_ipc}",
        "severity": severity,
    }


_SENSITIVE_HOSTPATHS = ("/", "/etc", "/proc", "/var/run/docker.sock", "/run/containerd/containerd.sock")


def check_hostpath_mount(workload: dict, context: dict) -> dict | None:
    host_volumes = {
        v["name"]: v["hostPath"]["path"]
        for v in workload["spec"].get("volumes") or []
        if v.get("hostPath", {}).get("path")
    }
    if not host_volumes:
        return None
    mounted = []
    for container in (workload["spec"].get("containers") or []) + (workload["spec"].get("initContainers") or []):
        for vm in container.get("volumeMounts") or []:
            if vm.get("name") in host_volumes:
                mounted.append((host_volumes[vm["name"]], bool(vm.get("readOnly"))))
    if not mounted:
        return None

    def sensitive(path: str) -> bool:
        return path in _SENSITIVE_HOSTPATHS or path.startswith("/var/lib/kubelet")

    critical = any(sensitive(p) or not ro for p, ro in mounted)
    return {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": "; ".join(f"{p} readOnly={ro}" for p, ro in mounted),
        "severity": "critical" if critical else "major",
    }


_SYSTEM_PRINCIPAL_RE = re.compile(r"^system:")
_MANAGED_IDENTITY_RE = re.compile(r"^gke-|^service-\d+@|\.gserviceaccount\.com$")
_ORG_EMAIL_GROUP_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _is_non_system_subject(subject: dict) -> tuple[bool, bool]:
    """Returns (flagged, is_org_email_group) for one binding subject."""
    kind, name, ns = subject.get("kind"), subject.get("name", ""), subject.get("namespace", "")
    if kind == "ServiceAccount":
        return ns not in _SYSTEM_SA_NAMESPACE_RE_PARTS and not ns.startswith("gke-") and not ns.startswith("config-management-"), False
    if kind in ("User", "Group"):
        if _SYSTEM_PRINCIPAL_RE.match(name) or _MANAGED_IDENTITY_RE.search(name):
            return False, False
        is_org_group = kind == "Group" and bool(_ORG_EMAIL_GROUP_RE.match(name))
        return True, is_org_group
    return False, False


def check_cluster_admin_binding(context: dict) -> list[dict]:
    hits = []
    for crb in context.get("clusterrolebindings") or []:
        role_ref = crb.get("roleRef") or {}
        if role_ref.get("name") != "cluster-admin":
            continue
        name = (crb.get("metadata") or {}).get("name", "")
        for subject in crb.get("subjects") or []:
            flagged, is_org_group = _is_non_system_subject(subject)
            if not flagged:
                continue
            target = f"{subject.get('kind')}/{subject.get('namespace', '-')}/{subject.get('name')}"
            hits.append(
                {
                    "namespace": "",
                    "object": f"ClusterRoleBinding/{name}",
                    "excerpt": f"{name} -> {target}",
                    "severity": "minor" if is_org_group else "critical",
                }
            )
    return hits


_WILDCARD_BOOTSTRAP_LABEL = "kubernetes.io/bootstrapping"


def check_wildcard_rbac(context: dict) -> list[dict]:
    bound_non_system = set()
    for kind_key in ("clusterrolebindings", "rolebindings"):
        for binding in context.get(kind_key) or []:
            role_ref = binding.get("roleRef") or {}
            for subject in binding.get("subjects") or []:
                flagged, _ = _is_non_system_subject(subject)
                if flagged:
                    bound_non_system.add((role_ref.get("kind"), role_ref.get("name")))

    hits = []
    for role in (context.get("roles") or []):
        meta = role.get("metadata") or {}
        if (meta.get("labels") or {}).get(_WILDCARD_BOOTSTRAP_LABEL) == "rbac-defaults":
            continue
        if meta.get("name", "").startswith("system:"):
            continue
        wildcard_rules = [
            rule
            for rule in role.get("rules") or []
            if "*" in (rule.get("verbs") or [])
            and (
                (rule.get("apiGroups") or []) == [""]
                or "*" in (rule.get("resources") or [])
                or "*" in (rule.get("apiGroups") or [])
            )
        ]
        # Vendor-apiGroup exception: a wildcard confined to one non-core
        # apiGroup that is not "*" itself is the operator-owns-its-own-CRDs
        # pattern, not an escalation. A wildcard over the core group ("")
        # is never suppressed, which the list-equality check above already
        # requires explicitly rather than falling out of the "*" membership
        # test (an apiGroups list of [""] contains no "*" at all).
        wildcard_rules = [
            r
            for r in wildcard_rules
            if (r.get("apiGroups") or []) == [""]
            or "*" in (r.get("apiGroups") or [])
            or len(set(r.get("apiGroups") or [])) != 1
        ]
        if not wildcard_rules:
            continue
        key = (role.get("kind"), meta.get("name"))
        if key not in bound_non_system:
            continue
        ns = meta.get("namespace", "")
        hits.append(
            {
                "namespace": ns,
                "object": f"{role['kind']}/{meta.get('name')}",
                "excerpt": json.dumps(wildcard_rules),
                "severity": "critical" if role.get("kind") == "ClusterRole" else "major",
            }
        )
    return hits


def check_netpol_missing(context: dict) -> list[dict]:
    hits = []
    netpols_by_ns: dict[str, list[dict]] = {}
    for netpol in context.get("networkpolicies") or []:
        ns = (netpol.get("metadata") or {}).get("namespace", "")
        netpols_by_ns.setdefault(ns, []).append(netpol)
    pod_count_by_ns: dict[str, int] = {}
    for wl in context.get("workloads") or []:
        if wl["kind"] == "Pod":
            pod_count_by_ns[wl["ns"]] = pod_count_by_ns.get(wl["ns"], 0) + 1
    # §2.6's Do-NOT-flag case: a namespace already covered fleet-wide by a
    # Dataplane V2 ClusterNetworkPolicy is not a default-allow posture just
    # because it has no *namespaced* NetworkPolicy of its own.
    has_cluster_network_policy = bool(context.get("cluster_network_policies"))

    for ns_item in context.get("namespaces") or []:
        ns = (ns_item.get("metadata") or {}).get("name", "")
        if _is_system_namespace(ns):
            continue
        policies = netpols_by_ns.get(ns, [])
        if not policies:
            if pod_count_by_ns.get(ns, 0) == 0:
                continue  # no workloads, no exposure, pure churn
            if has_cluster_network_policy:
                continue
            hits.append(
                {"namespace": ns, "object": f"Namespace/{ns}", "excerpt": "zero NetworkPolicies", "severity": "major"}
            )
            continue
        allow_all = [
            p
            for p in policies
            if (p.get("spec") or {}).get("podSelector") == {}
            and (
                any(rule == {} for rule in (p.get("spec") or {}).get("ingress") or [])
                or not (p.get("spec") or {}).get("policyTypes")
            )
        ]
        if allow_all and len(allow_all) == len(policies):
            for p in allow_all:
                hits.append(
                    {
                        "namespace": ns,
                        "object": f"NetworkPolicy/{(p.get('metadata') or {}).get('name', '')}",
                        "excerpt": "allow-all (podSelector: {} with an empty ingress rule)",
                        "severity": "minor",
                    }
                )
    return hits


def check_default_sa_automount(context: dict) -> list[dict]:
    unsafe_sa_namespaces = set()
    for sa in context.get("serviceaccounts") or []:
        meta = sa.get("metadata") or {}
        if sa.get("automountServiceAccountToken") is not False:
            unsafe_sa_namespaces.add(meta.get("namespace", ""))

    hits = []
    for wl in context.get("workloads") or []:
        sa_name = wl["spec"].get("serviceAccountName") or wl["spec"].get("serviceAccount") or "default"
        if sa_name != "default":
            continue
        if wl["ns"] not in unsafe_sa_namespaces:
            continue
        if wl["spec"].get("automountServiceAccountToken") is False:
            continue
        hits.append(
            {
                "namespace": wl["ns"],
                "object": f"{wl['kind']}/{wl['name']}",
                "excerpt": "resolves to the default ServiceAccount with automount not disabled",
            }
        )
    return hits


def check_workload_identity_off(context: dict) -> list[dict]:
    describe = context.get("cluster_describe") or {}
    pool = ((describe.get("workloadIdentityConfig") or {}).get("workloadPool") or "").strip()
    if pool:
        return []
    return [{"namespace": "", "object": "Cluster", "excerpt": "workloadIdentityConfig.workloadPool is empty"}]


def check_legacy_metadata(context: dict) -> list[dict]:
    hits = []
    for pool in context.get("node_pools") or []:
        mode = ((pool.get("config") or {}).get("workloadMetadataConfig") or {}).get("mode") or ""
        if mode == "GKE_METADATA":
            continue
        hits.append(
            {
                "namespace": "",
                "object": f"NodePool/{pool.get('name', '')}",
                "excerpt": f"workloadMetadataConfig.mode={mode or '(empty)'}",
            }
        )
    return hits


def check_public_control_plane(context: dict) -> list[dict]:
    describe = context.get("cluster_describe") or {}
    private_cfg = describe.get("privateClusterConfig") or {}
    public_endpoint_enabled = (
        (describe.get("controlPlaneEndpointsConfig") or {}).get("ipEndpointsConfig") or {}
    ).get("enablePublicEndpoint")
    reachable = private_cfg.get("enablePrivateEndpoint") is not True or public_endpoint_enabled is True
    if not reachable:
        return []
    man_cfg = describe.get("masterAuthorizedNetworksConfig") or {}
    unrestricted = man_cfg.get("enabled") is not True or "0.0.0.0/0" in (man_cfg.get("cidrBlocks") or [])
    if not unrestricted:
        return []
    return [{"namespace": "", "object": "Cluster", "excerpt": "public endpoint reachable with no restrictive authorized networks"}]


def _namespace_labels(context: dict, ns: str) -> dict:
    for item in context.get("namespaces") or []:
        meta = item.get("metadata") or {}
        if meta.get("name") == ns:
            return meta.get("labels") or {}
    return {}


def check_podsecurity_gaps(workload: dict, context: dict) -> dict | None:
    if check_privileged_container(workload, context) is not None:
        return None  # 2.1's finding subsumes this one; never emit both
    if _namespace_labels(context, workload["ns"]).get("pod-security.kubernetes.io/enforce") == "restricted":
        return None  # admission already guarantees it
    pod_sc = workload["spec"].get("securityContext") or {}
    bad = []
    for container in (workload["spec"].get("containers") or []) + (workload["spec"].get("initContainers") or []):
        c_sc = container.get("securityContext") or {}
        if "runAsNonRoot" in c_sc:
            non_root = c_sc["runAsNonRoot"]
        elif "runAsNonRoot" in pod_sc:
            non_root = pod_sc["runAsNonRoot"]
        else:
            non_root = None
        run_as_user = c_sc.get("runAsUser", pod_sc.get("runAsUser"))
        seccomp_type = ((c_sc.get("seccompProfile") or {}).get("type") or (pod_sc.get("seccompProfile") or {}).get("type") or "")
        if non_root is not True or run_as_user == 0 or seccomp_type not in ("RuntimeDefault", "Localhost"):
            bad.append(container.get("name", ""))
    if not bad:
        return None
    return {"object": f"{workload['kind']}/{workload['name']}", "excerpt": f"containers: {', '.join(bad)}"}


# --------------------------------------------------------------------------- #
# ai-security-audit: §3.1 `inference-endpoint-public` … §3.6
# `model-image-floating-tag`. Same dump kinds as compliance (`deploy,sts,ds,
# cronjob,pod`), plus a `svc` dump for §3.1's exposure check, so the workload
# normalizer here mirrors `normalize_compliance_workloads`'s suppressions but
# adds `lbl` (the pod template's labels, needed to match a Service selector
# against a workload in §3.1) and the §2 AI-workload discriminator, so the
# list this collector hands every check is already narrowed to AI workloads
# the way `$PRE`'s last `select` narrows the SOP's own pipeline.
# --------------------------------------------------------------------------- #

AI_MODEL_IMAGE_RE = re.compile(
    r"(^|/)(vllm|sglang|text-generation-inference|tgi|tritonserver|torchserve|tensorflow-serving|"
    r"kserve|ollama|ray|llama|mlserver|seldon|lorax|aibrix)([-:@/]|$)"
)
AI_ACCELERATOR_KEY_RE = re.compile(r"nvidia\.com/gpu|google\.com/tpu")


def _is_ai_workload(spec: dict) -> bool:
    containers = spec.get("containers") or []
    if any(AI_MODEL_IMAGE_RE.search(c.get("image") or "") for c in containers):
        return True
    for c in containers:
        limits = (c.get("resources") or {}).get("limits") or {}
        if any(AI_ACCELERATOR_KEY_RE.search(key) for key in limits):
            return True
    return False


def _pod_template_labels_of(item: dict) -> dict:
    """The same three-way fallback chain as the SOP's `lbl` field: a
    Deployment/StatefulSet/DaemonSet's pod template labels, a CronJob's
    (nested one level deeper), or a bare Pod's own labels."""
    spec = item.get("spec") or {}
    labels = ((spec.get("template") or {}).get("metadata") or {}).get("labels")
    if labels:
        return labels
    labels = (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("metadata", {}).get("labels")
    if labels:
        return labels
    return (item.get("metadata") or {}).get("labels") or {}


def normalize_ai_workloads(dump: dict) -> list[dict]:
    out = []
    for item in dump.get("items", []) or []:
        if item.get("kind") not in COMPLIANCE_WORKLOAD_KINDS:
            continue
        meta = item.get("metadata") or {}
        ns = meta.get("namespace", "")
        if _is_system_namespace(ns):
            continue
        labels = meta.get("labels") or {}
        if "addonmanager.kubernetes.io/mode" in labels:
            continue
        if (meta.get("annotations") or {}).get("components.gke.io/component-name"):
            continue
        if item.get("kind") == "Pod" and meta.get("ownerReferences"):
            continue
        spec = _pod_spec_of(item)
        if not _is_ai_workload(spec):
            continue
        out.append({"kind": item["kind"], "ns": ns, "name": meta.get("name", ""), "spec": spec, "lbl": _pod_template_labels_of(item)})
    return out


def _ai_containers(spec: dict) -> list[dict]:
    return (spec.get("containers") or []) + (spec.get("initContainers") or [])


TRUST_REMOTE_CODE_ARG_RE = re.compile(r"trust[-_]remote[-_]code(?!=(0|false|no))", re.IGNORECASE)
TRUST_REMOTE_CODE_ENV_NAME_RE = re.compile(r"TRUST_REMOTE_CODE", re.IGNORECASE)


def _container_trusts_remote_code(c: dict) -> bool:
    tokens = [str(t) for t in (c.get("args") or [])] + [str(t) for t in (c.get("command") or [])]
    if any(TRUST_REMOTE_CODE_ARG_RE.search(t) for t in tokens):
        return True
    for e in c.get("env") or []:
        if TRUST_REMOTE_CODE_ENV_NAME_RE.search(e.get("name") or "") and str(e.get("value", "")).lower() in ("1", "true", "yes"):
            return True
    return False


def check_model_remote_code_trusted(workload: dict, context: dict) -> dict | None:
    bad = [c.get("name", "") for c in _ai_containers(workload["spec"]) if _container_trusts_remote_code(c)]
    if not bad:
        return None
    return {"object": f"{workload['kind']}/{workload['name']}", "excerpt": f"containers: {', '.join(bad)}"}


def check_weights_mount_writable(workload: dict, context: dict) -> dict | None:
    vols_by_name = {v.get("name"): v for v in workload["spec"].get("volumes") or []}
    bad = []
    for c in workload["spec"].get("containers") or []:
        for m in c.get("volumeMounts") or []:
            if m.get("readOnly", False):
                continue
            vol = vols_by_name.get(m.get("name"))
            if vol is None:
                continue
            csi, pvc = vol.get("csi"), vol.get("persistentVolumeClaim")
            if (csi is not None and not csi.get("readOnly", False)) or (pvc is not None and not pvc.get("readOnly", False)):
                bad.append(f"{c.get('name', '')}:{m.get('name')}:{m.get('mountPath')}")
    if not bad:
        return None
    return {"object": f"{workload['kind']}/{workload['name']}", "excerpt": "; ".join(bad)}


AI_URL_RE = re.compile(r"(^|=)(http|ftp)://")
AI_MODEL_FLAG_RE = re.compile(r"^--model(-id)?(=|$)")
AI_REVISION_FLAG_RE = re.compile(r"^--revision(=|$)")


def check_model_artifact_unpinned_source(workload: dict, context: dict) -> dict | None:
    bad = []
    escalate = False
    for c in _ai_containers(workload["spec"]):
        args_cmd = [str(a) for a in (c.get("args") or [])] + [str(a) for a in (c.get("command") or [])]
        env_vals = [str(e.get("value", "")) for e in (c.get("env") or [])]
        has_url = any(AI_URL_RE.search(v) for v in args_cmd + env_vals)
        has_model_flag = any(AI_MODEL_FLAG_RE.search(a) for a in args_cmd)
        has_revision_flag = any(AI_REVISION_FLAG_RE.search(a) for a in args_cmd)
        if has_url or (has_model_flag and not has_revision_flag):
            bad.append(c.get("name", ""))
            if _container_trusts_remote_code(c):
                escalate = True  # §3.4: escalates to critical alongside a 3.2 finding on the same container
    if not bad:
        return None
    hit = {"object": f"{workload['kind']}/{workload['name']}", "excerpt": f"containers: {', '.join(bad)}"}
    if escalate:
        hit["severity"] = "critical"
    return hit


AI_CREDENTIAL_ENV_NAME_RE = re.compile(
    r"HF_[A-Z_]*TOKEN|HUGGING_?FACE.*TOKEN|OPENAI_API_KEY|ANTHROPIC_API_KEY|WANDB_API_KEY|"
    r"(MODEL|REGISTRY|INFERENCE).*(TOKEN|KEY|SECRET|PASSWORD)",
    re.IGNORECASE,
)
# §3.5's own named non-secret examples (`HF_TOKEN_PATH`, `OPENAI_API_KEY_FILE`,
# `MODEL_REGISTRY_KEY_ID`): a name ending in one of these suffixes names a
# path, a file, or an identifier *about* a credential, never the credential's
# value itself, no matter what it matches upstream of the suffix.
AI_CREDENTIAL_ENV_NAME_SAFE_SUFFIX_RE = re.compile(r"_(PATH|FILE|ID)$", re.IGNORECASE)


def check_model_credential_plaintext_env(workload: dict, context: dict) -> dict | None:
    bad = []
    for c in _ai_containers(workload["spec"]):
        for e in c.get("env") or []:
            name = e.get("name") or ""
            if (
                e.get("value")
                and e.get("valueFrom") is None
                and AI_CREDENTIAL_ENV_NAME_RE.search(name)
                and not AI_CREDENTIAL_ENV_NAME_SAFE_SUFFIX_RE.search(name)
            ):
                bad.append(f"{c.get('name', '')}:{name}")
    if not bad:
        return None
    return {"object": f"{workload['kind']}/{workload['name']}", "excerpt": f"set with a literal value: {', '.join(bad)}"}


AI_FLOATING_TAG_RE = re.compile(r":(latest|main|master|dev|nightly|stable)$")
AI_TAG_RE = re.compile(r":[^/]*$")
AI_DIGEST_RE = re.compile(r"@sha256:")


def check_model_image_floating_tag(workload: dict, context: dict) -> dict | None:
    bad = []
    for c in _ai_containers(workload["spec"]):
        img = c.get("image") or ""
        if not img or AI_DIGEST_RE.search(img):
            continue
        if AI_FLOATING_TAG_RE.search(img) or not AI_TAG_RE.search(img):
            bad.append(f"{c.get('name', '')}:{img}")
    if not bad:
        return None
    return {"object": f"{workload['kind']}/{workload['name']}", "excerpt": "; ".join(bad)}


def check_inference_endpoint_public(context: dict) -> list[dict]:
    hits = []
    for svc in context.get("services") or []:
        meta, spec = svc.get("metadata") or {}, svc.get("spec") or {}
        if spec.get("type") != "LoadBalancer":
            continue
        annotations = meta.get("annotations") or {}
        if annotations.get("networking.gke.io/load-balancer-type") == "Internal":
            continue
        if annotations.get("cloud.google.com/load-balancer-type") == "Internal":
            continue
        selector = spec.get("selector") or {}
        if not selector:
            continue
        ns = meta.get("namespace", "")
        matched = any(
            w["ns"] == ns and all(w.get("lbl", {}).get(k) == v for k, v in selector.items())
            for w in context.get("ai_workloads") or []
        )
        if not matched:
            continue
        hits.append(
            {
                "namespace": ns,
                "object": f"Service/{meta.get('name', '')}",
                "excerpt": "type=LoadBalancer, no internal-LB annotation, selects an AI workload in this namespace",
            }
        )
    return hits


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

COMPLIANCE_CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec(
        "privileged-container",
        "workload",
        check_privileged_container,
        "critical",
        None,
        "Container has full host device and kernel access; compromising this "
        "workload compromises the node.",
    ),
    CheckSpec(
        "host-namespace",
        "workload",
        check_host_namespace,
        "major",  # overridden per hit: critical for hostPID/hostIPC
        None,
        "Workload shares the node's process/IPC/network namespace, bypassing "
        "pod isolation and NetworkPolicy enforcement.",
    ),
    CheckSpec(
        "hostpath-mount",
        "workload",
        check_hostpath_mount,
        "major",  # overridden per hit: critical for a sensitive or writable path
        None,
        "Workload mounts a node filesystem path, giving it access to state "
        "belonging to the node and to other tenants' pods.",
    ),
    CheckSpec(
        "cluster-admin-binding",
        "cluster",
        check_cluster_admin_binding,
        "critical",  # overridden per hit: minor for an org-email Group
        None,
        "Subject holds unrestricted read/write on every resource in the "
        "cluster, including Secrets in every namespace.",
    ),
    CheckSpec(
        "wildcard-rbac",
        "cluster",
        check_wildcard_rbac,
        "critical",  # overridden per hit: major for a namespaced Role
        None,
        "Subject can perform any verb on any resource in this scope, "
        "including reading Secrets and creating privileged pods — an "
        "unbounded escalation path.",
    ),
    CheckSpec(
        "netpol-missing",
        "cluster",
        check_netpol_missing,
        "major",  # overridden per hit: minor for allow-all-only
        None,
        "Every pod in this namespace accepts traffic from every pod in the "
        "cluster; a compromise anywhere reaches these workloads unimpeded.",
    ),
    CheckSpec(
        "default-sa-automount",
        "cluster",
        check_default_sa_automount,
        "major",
        None,
        "Workload mounts an API-server credential it does not use, handing "
        "an attacker an authenticated foothold for free.",
    ),
    CheckSpec(
        "workload-identity-off",
        "cluster",
        check_workload_identity_off,
        "critical",
        None,
        "All pods on this cluster share the node service account's Google "
        "Cloud permissions; there is no per-workload IAM boundary.",
    ),
    CheckSpec(
        "legacy-metadata",
        "cluster",
        check_legacy_metadata,
        "critical",
        None,
        "Any pod on this node pool can read the node service account's "
        "access token from the legacy metadata endpoint and escalate to "
        "that identity's full Google Cloud permissions.",
    ),
    CheckSpec(
        "public-control-plane",
        "cluster",
        check_public_control_plane,
        "critical",
        None,
        "The cluster's API server accepts connections from any address on "
        "the internet; credential compromise or an API-server CVE is "
        "directly exploitable from outside the network.",
    ),
    CheckSpec(
        "podsecurity-gaps",
        "workload",
        check_podsecurity_gaps,
        "minor",
        None,
        "Containers run as root and/or without a seccomp filter, so a "
        "runtime escape has an unfiltered syscall surface and immediate "
        "root in the namespace it reaches.",
    ),
)

AI_SECURITY_CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec(
        "inference-endpoint-public",
        "cluster",
        check_inference_endpoint_public,
        "critical",
        None,
        "This model server is reachable from the public internet. Anyone who finds the address can "
        "send it inference traffic, consume its accelerator capacity, and probe whatever the model "
        "can reach.",
    ),
    CheckSpec(
        "model-remote-code-trusted",
        "workload",
        check_model_remote_code_trusted,
        "critical",
        None,
        "The model loader executes arbitrary code shipped inside the model repository, with this "
        "pod's ServiceAccount, network access, and mounted volumes. A compromised or swapped model "
        "artifact is remote code execution in this namespace.",
    ),
    CheckSpec(
        "weights-mount-writable",
        "workload",
        check_weights_mount_writable,
        "major",
        None,
        "The serving process can overwrite its own model weights. Any code execution in this pod "
        "becomes a persistent, replica-wide model swap that outlives the compromised pod and is "
        "invisible to an image scanner.",
    ),
    CheckSpec(
        "model-artifact-unpinned-source",
        "workload",
        check_model_artifact_unpinned_source,
        "major",  # overridden per hit: critical alongside a model-remote-code-trusted finding on the same container
        None,
        "The model artifact this container loads is not pinned: the bytes that arrive at the next "
        "pod restart are whatever the source serves then. Nothing in the manifest records which "
        "model is actually running.",
    ),
    CheckSpec(
        "model-credential-plaintext-env",
        "workload",
        check_model_credential_plaintext_env,
        "major",
        None,
        "A model-registry credential is embedded in this workload's pod spec in plaintext. It is "
        "visible to anyone who can describe the pod or read the manifest in Git, and it is not "
        "rotatable without a redeploy.",
    ),
    CheckSpec(
        "model-image-floating-tag",
        "workload",
        check_model_image_floating_tag,
        "minor",
        None,
        "The image this container runs is not reproducible: a restart can pull different bytes than "
        "the ones running now, with no manifest change to review.",
    ),
)

CHECK_TABLES: dict[str, tuple[CheckSpec, ...]] = {
    "obtainability-audit": OBTAINABILITY_CHECKS,
    "compliance-audit": COMPLIANCE_CHECKS,
    "ai-security-audit": AI_SECURITY_CHECKS,
}


# --------------------------------------------------------------------------- #
# Orchestration
#
# A stream's *context builder* owns how it collects (one dump, or several —
# compliance-audit's RBAC/NetworkPolicy/ServiceAccount/gcloud reads have no
# single-dump shape to share); `collect_cluster` owns what every stream does
# with the result once collected, which is identical regardless of shape:
# run each check, apply the autopilot/per-hit severity rule, emit candidates.
# --------------------------------------------------------------------------- #


class CollectedContext(NamedTuple):
    context: dict
    workloads: list[dict]  # for "workload"-kind checks; [] for a stream with none
    commands: dict[str, dict]  # check slug -> {command, rc, duration_s, output_sha256}


def _record(argv_str: str, result: Run) -> dict:
    return {
        "command": argv_str,
        "rc": result.rc,
        "duration_s": round(result.duration_s, 2),
        "output_sha256": output_digest(result.stdout),
    }


def _collect_obtainability(cluster: dict, kubeconfig: Path, checks: tuple[CheckSpec, ...], *, run: RunFn) -> CollectedContext:
    dump_path, dump_run, gate_ok = dump_state(kubeconfig, cluster["name"], run=run)
    if not gate_ok:
        raise GateFailure(f"dump gate failed (rc={dump_run.rc}): {dump_run.stderr.strip()[:300]}")
    dump = json.loads(dump_path.read_text(encoding="utf-8"))
    workloads = normalize_workloads(dump)
    record = _record(f"KUBECONFIG={kubeconfig} kubectl get {DUMP_COMMAND_KINDS} -A -o json", dump_run)
    return CollectedContext(build_context(dump, workloads), workloads, {spec.slug: record for spec in checks})


# check slug -> which named collection(s) it needs, so a gate failure on one
# collection (e.g. the gcloud describe) does not also invalidate checks that
# only need another (e.g. the workload dump). Every slug not listed here
# needs no cross-reference beyond the workload dump.
_COMPLIANCE_CHECK_SOURCES: dict[str, tuple[str, ...]] = {
    "cluster-admin-binding": ("rbac",),
    "wildcard-rbac": ("rbac",),
    "netpol-missing": ("netpol", "namespaces", "workloads", "ccnp"),
    "default-sa-automount": ("serviceaccounts", "workloads"),
    "workload-identity-off": ("describe",),
    "legacy-metadata": ("node_pools",),
    "public-control-plane": ("describe",),
}

# §1's four node-facing checks Autopilot's admission controller rules out
# structurally, with the SOP's own canonical reasons verbatim. On an
# Autopilot cluster these must never appear in the manifest's `commands` --
# an agent that copies `commands` verbatim into `checks_run` (§2's
# instruction for every other check) and *also* follows §1's instruction to
# record these four in `checks_not_applicable` would name the same slug in
# both lists, which the validator rejects.
_COMPLIANCE_AUTOPILOT_NOT_APPLICABLE: tuple[tuple[str, str], ...] = (
    ("privileged-container", "GKE Autopilot: privileged containers are rejected at admission and cannot exist here."),
    ("host-namespace", "GKE Autopilot: hostPID/hostIPC/hostNetwork are rejected at admission and cannot exist here."),
    ("hostpath-mount", "GKE Autopilot: hostPath volumes are rejected at admission and cannot exist here."),
    ("legacy-metadata", "GKE Autopilot: no user-managed node pools to carry a metadata setting."),
)


def _collect_compliance(cluster: dict, kubeconfig: Path, checks: tuple[CheckSpec, ...], *, run: RunFn) -> CollectedContext:
    name, project, location = cluster["name"], cluster["project"], cluster["location"]
    commands: dict[str, dict] = {}
    context: dict = {"workloads": []}

    def gated(argv: list[str]) -> tuple[dict | list | None, Run]:
        return run_and_gate(argv, kubeconfig, run=run)

    # Every collection below is gate-checked independently, and a gate
    # failure raises immediately — this stream fails the whole cluster
    # closed on any missing input, the same as a single-dump stream, per
    # `GateFailure`'s own docstring.
    workload_argv = ["kubectl", "get", COMPLIANCE_DUMP_KINDS, "-A", "-o", "json"]
    parsed, result = gated(workload_argv)
    if parsed is None:
        raise GateFailure(f"workload dump gate failed (rc={result.rc}): {result.stderr.strip()[:300]}")
    context["workloads"] = normalize_compliance_workloads(parsed)
    workload_record = _record(f"KUBECONFIG={kubeconfig} {' '.join(workload_argv)}", result)
    for spec in checks:
        if spec.slug not in _COMPLIANCE_CHECK_SOURCES:
            commands[spec.slug] = workload_record

    rbac_argv = ["kubectl", "get", "clusterroles,roles,clusterrolebindings,rolebindings", "-A", "-o", "json"]
    parsed, result = gated(rbac_argv)
    if parsed is None:
        raise GateFailure(f"RBAC dump gate failed (rc={result.rc}): {result.stderr.strip()[:300]}")
    items = parsed.get("items", [])
    context["roles"] = [i for i in items if i.get("kind") in ("ClusterRole", "Role")]
    context["clusterrolebindings"] = [i for i in items if i.get("kind") == "ClusterRoleBinding"]
    context["rolebindings"] = [i for i in items if i.get("kind") == "RoleBinding"]
    record = _record(f"KUBECONFIG={kubeconfig} {' '.join(rbac_argv)}", result)
    for slug in ("cluster-admin-binding", "wildcard-rbac"):
        commands[slug] = record

    netpol_argv = ["kubectl", "get", "netpol,ns", "-A", "-o", "json"]
    parsed, result = gated(netpol_argv)
    if parsed is None:
        raise GateFailure(f"NetworkPolicy/Namespace dump gate failed (rc={result.rc}): {result.stderr.strip()[:300]}")
    items = parsed.get("items", [])
    context["networkpolicies"] = [i for i in items if i.get("kind") == "NetworkPolicy"]
    context["namespaces"] = [i for i in items if i.get("kind") == "Namespace"]
    commands["netpol-missing"] = _record(f"KUBECONFIG={kubeconfig} {' '.join(netpol_argv)}", result)

    # A deliberate exception to "every read above raises": Dataplane V2's
    # `ClusterNetworkPolicy` CRD (§2.6's Do-NOT-flag case, `kubectl get ccnp
    # -o name`) is not installed on every cluster, so a failure here almost
    # always means "this cluster has no such CRD," not "this input is
    # missing." Gating the whole cluster closed on that would fail every
    # compliance-audit run on a cluster without the CRD -- worse than the
    # false positive it exists to suppress. Absence reads as zero
    # ClusterNetworkPolicies, the same posture as before this read existed.
    ccnp_argv = ["kubectl", "get", "ccnp", "-A", "-o", "json"]
    ccnp_parsed, ccnp_result = run_and_gate(ccnp_argv, kubeconfig, run=run)
    context["cluster_network_policies"] = [i for i in (ccnp_parsed or {}).get("items", [])] if ccnp_parsed else []

    sa_argv = ["kubectl", "get", "sa", "-A", "--field-selector", "metadata.name=default", "-o", "json"]
    parsed, result = gated(sa_argv)
    if parsed is None:
        raise GateFailure(f"ServiceAccount dump gate failed (rc={result.rc}): {result.stderr.strip()[:300]}")
    context["serviceaccounts"] = parsed.get("items", [])
    commands["default-sa-automount"] = _record(f"KUBECONFIG={kubeconfig} {' '.join(sa_argv)}", result)

    describe_argv = [
        "gcloud", "container", "clusters", "describe", name, "--location", location, "--project", project,
        "--format", "json(workloadIdentityConfig,privateClusterConfig,masterAuthorizedNetworksConfig,"
        "controlPlaneEndpointsConfig)",
    ]
    parsed, result = gated(describe_argv)
    if parsed is None:
        raise GateFailure(f"cluster describe gate failed (rc={result.rc}): {result.stderr.strip()[:300]}")
    context["cluster_describe"] = parsed
    describe_command = " ".join(describe_argv)
    for slug in ("workload-identity-off", "public-control-plane"):
        commands[slug] = _record(describe_command, result)

    node_pools_argv = ["gcloud", "container", "node-pools", "list", "--cluster", name, "--location", location, "--project", project, "--format", "json"]
    parsed, result = gated(node_pools_argv)
    if parsed is None:
        raise GateFailure(f"node-pools list gate failed (rc={result.rc}): {result.stderr.strip()[:300]}")
    context["node_pools"] = parsed if isinstance(parsed, list) else []
    commands["legacy-metadata"] = _record(" ".join(node_pools_argv), result)

    return CollectedContext(context, context["workloads"], commands)


def _collect_ai_security(cluster: dict, kubeconfig: Path, checks: tuple[CheckSpec, ...], *, run: RunFn) -> CollectedContext:
    """Two dumps, the same shape the SOP's own §2 describes: the workload
    dump backs every `workload`-kind check, the Service dump backs
    `inference-endpoint-public` alone. Either failing fails the whole
    cluster closed, the same trade-off compliance-audit's several
    independent reads accept."""
    workload_argv = ["kubectl", "get", COMPLIANCE_DUMP_KINDS, "-A", "-o", "json"]
    parsed, result = run_and_gate(workload_argv, kubeconfig, run=run)
    if parsed is None:
        raise GateFailure(f"workload dump gate failed (rc={result.rc}): {result.stderr.strip()[:300]}")
    ai_workloads = normalize_ai_workloads(parsed)
    workload_record = _record(f"KUBECONFIG={kubeconfig} {' '.join(workload_argv)}", result)

    svc_argv = ["kubectl", "get", "svc", "-A", "-o", "json"]
    svc_parsed, svc_result = run_and_gate(svc_argv, kubeconfig, run=run)
    if svc_parsed is None:
        raise GateFailure(f"service dump gate failed (rc={svc_result.rc}): {svc_result.stderr.strip()[:300]}")
    svc_record = _record(f"KUBECONFIG={kubeconfig} {' '.join(svc_argv)}", svc_result)

    context = {"ai_workloads": ai_workloads, "services": svc_parsed.get("items", [])}
    commands = {
        spec.slug: (svc_record if spec.slug == "inference-endpoint-public" else workload_record) for spec in checks
    }
    return CollectedContext(context, ai_workloads, commands)


_COLLECTORS: dict[str, Callable[..., CollectedContext]] = {
    "obtainability-audit": _collect_obtainability,
    "compliance-audit": _collect_compliance,
    "ai-security-audit": _collect_ai_security,
}


def collect_cluster(
    cluster: dict, audit_id: str, checks: tuple[CheckSpec, ...], *, run: RunFn = default_run
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

    try:
        collected = _COLLECTORS[audit_id](cluster, kubeconfig, checks, run=run)
    except GateFailure as exc:
        return {
            "name": name, "project": project, "location": location,
            "outcome": "gate-failed",
            "error": str(exc),
        }

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
            for workload in collected.workloads:
                hit = spec.run(workload, collected.context)
                if hit is not None:
                    candidates.append(emit(spec, hit, workload["ns"]))
        else:
            for hit in spec.run(collected.context):
                candidates.append(emit(spec, hit, hit.get("namespace", "")))

    not_applicable_slugs: set[str] = set()
    checks_not_applicable: list[dict] = []
    if audit_id == "compliance-audit" and cluster.get("autopilot"):
        applicable = {spec.slug for spec in checks}
        for slug, reason in _COMPLIANCE_AUTOPILOT_NOT_APPLICABLE:
            if slug in applicable:
                not_applicable_slugs.add(slug)
                checks_not_applicable.append({"check": slug, "reason": reason})

    result = {
        "name": name, "project": project, "location": location,
        "outcome": "collected",
        "commands": [{"check": spec.slug, **collected.commands[spec.slug]} for spec in checks if spec.slug not in not_applicable_slugs],
        "candidates": candidates,
    }
    if checks_not_applicable:
        result["checks_not_applicable"] = checks_not_applicable
    return result


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
            pool.submit(collect_cluster, cluster, audit_id, checks, run=run): index
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
