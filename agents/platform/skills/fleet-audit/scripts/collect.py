#!/opt/hermes/.venv/bin/python3
"""collect.py — Procedural collector for the fleet-audit checks that are code
wearing prose.

See docs/designs/fleet-audit-collectors-and-status.md §4.2, §4.3, §6.

**Scope of this file today: the collector engine, plus one stream's check
table.** `obtainability-audit` ships with two of its eleven checks converted
(`no-requests`, `no-memory-limit` — §3.1/§3.2 of
`governance/obtainability_audit_sop.md`), chosen because they need no
selector matching, no cross-object resolution, and no judgment: a container
either declares the field or it does not. The other nine obtainability
checks and every other stream's checks still run the way they do today, by
SOP prose executed as shell — this collector does not change that. Converting
one is proof the shape works; converting the rest is the next several phases
in the design's §10 work breakdown, each its own PR, deliberately.

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
        template_spec = (spec.get("template") or {}).get("spec") or {}
        out.append(
            {"kind": item["kind"], "ns": ns, "name": meta.get("name", ""), "spec": spec, "template": template_spec}
        )
    return out


def limitranges_by_namespace(dump: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for item in dump.get("items", []) or []:
        if item.get("kind") != "LimitRange":
            continue
        ns = (item.get("metadata") or {}).get("namespace", "")
        out.setdefault(ns, []).append(item)
    return out


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


def check_no_requests(workload: dict, limitranges: dict) -> dict | None:
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


def check_no_memory_limit(workload: dict, limitranges: dict) -> dict | None:
    missing = [
        container.get("name", "")
        for container in workload["template"].get("containers") or []
        if "memory" not in ((container.get("resources") or {}).get("limits") or {})
    ]
    if not missing or _has_default(limitranges, workload["ns"], "default", "memory"):
        return None
    return {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": f"containers missing a memory limit: {', '.join(missing)}",
    }


class CheckSpec(NamedTuple):
    slug: str
    run: Callable[[dict, dict], dict | None]
    severity: str
    autopilot_severity: str | None  # None: severity is mode-independent
    impact: str


OBTAINABILITY_CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec(
        "no-requests",
        check_no_requests,
        "major",
        "minor",
        "The scheduler and cluster autoscaler size this cluster as if this "
        "workload costs nothing; its pods are the first evicted under node "
        "pressure and its cost cannot be attributed.",
    ),
    CheckSpec(
        "no-memory-limit",
        check_no_memory_limit,
        "major",
        None,
        "A memory leak here is absorbed by the node, not by this pod — the "
        "kubelet evicts co-located workloads first.",
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
    limitranges = limitranges_by_namespace(dump)
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

    candidates = []
    for spec in checks:
        severity = spec.severity
        impact = spec.impact
        if cluster.get("autopilot") and spec.autopilot_severity:
            severity = spec.autopilot_severity
            impact = f"{impact} (Autopilot: severity downgraded — the platform injects requests at admission.)"
        for workload in workloads:
            hit = spec.run(workload, limitranges)
            if hit is None:
                continue
            candidates.append(
                {
                    "check": spec.slug,
                    "cluster": name,
                    "namespace": workload["ns"],
                    "object": hit["object"],
                    "severity": severity,
                    "excerpt": hit["excerpt"],
                    "impact": impact,
                    "needs_triage": None,
                }
            )

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
