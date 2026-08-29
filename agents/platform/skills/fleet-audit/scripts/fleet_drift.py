#!/opt/hermes/.venv/bin/python3
"""fleet_drift.py — Procedural collector for the Fleet Consistency Drift
Audit (`fleet-consistency-drift`).

See docs/designs/fleet-audit-collectors-and-status.md §4.2, §10 phase 4, and
governance/fleet_consistency_drift_sop.md.

This stream's own collector: every one of its nineteen facets reads GKE
control-plane and node-pool metadata through `gcloud container`, no
`kubectl` and no kubeconfig, so it needs neither `collect.py`'s per-cluster
credential fetch nor `patch_readiness.py`'s per-location baseline —
`clusters list` alone, once per project, returns every field every facet
compares, the same shape `clusters describe` would (the SOP's own §1.3
frames `describe` as each finding's `evidence.command`; this collector
issues the cheaper per-project `list` instead and records that as the
command it actually ran, per §4.1's rule that the manifest publishes what
happened, not what the SOP originally described).

**What is procedural here and what stays judgment.** Normalizing a raw
field to one comparable token per facet (§4), computing the majority and
its confidence (§3), and walking the severity ladder are all closed-form —
exactly what §2's design point calls "code wearing prose". What does not
move: a finding still needs the three-field `recommendation` prose the
validator requires non-empty, and a `kind: manifest` remediation still
needs a human's GitOps declaration lookup. Those stay in the SOP for the
agent to do once the manifest hands it the outliers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NamedTuple

MANIFEST_VERSION = 1
DEFAULT_TIMEOUT_S = 60
MAX_WORKERS = 8
INVENTORY_PATH = "/opt/data/INVENTORY.raw.md"
COHORT_FLOOR = 3
SEVERITY_LEVELS = ("critical", "major", "minor")
ENV_SYNONYMS = {
    "prod": "prod", "prd": "prod", "production": "prod",
    "staging": "staging", "stg": "staging", "stage": "staging", "preprod": "staging",
    "dev": "dev", "development": "dev", "sandbox": "dev", "sbx": "dev",
    "test": "test", "qa": "test", "uat": "test",
}


def log(msg: str) -> None:
    print(f"[fleet_drift] {msg}", file=sys.stderr, flush=True)


class Run(NamedTuple):
    argv: list[str]
    rc: int
    stdout: str
    stderr: str
    duration_s: float


RunFn = Callable[..., Run]


def default_run(argv: list[str], *, timeout: int = DEFAULT_TIMEOUT_S) -> Run:
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return Run(argv, proc.returncode, proc.stdout, proc.stderr, time.monotonic() - t0)
    except subprocess.TimeoutExpired as exc:
        return Run(argv, 124, exc.stdout or "", exc.stderr or "", time.monotonic() - t0)
    except Exception as exc:
        return Run(argv, -1, "", str(exc), time.monotonic() - t0)


def default_read_text(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def run_and_gate(argv: list[str], *, run: RunFn) -> tuple[object | None, Run]:
    result = run(argv)
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


# --------------------------------------------------------------------------- #
# §1: fleet enumeration
# --------------------------------------------------------------------------- #

PROJECT_ID_RE = re.compile(r"\b([a-z][a-z0-9-]{4,28}[a-z0-9])\b")


def discover_projects(base_project: str | None, *, run: RunFn, read_text: Callable[[str], str | None] = default_read_text) -> list[str]:
    """§1.1: the active project plus any project IDs already recorded in the
    onboarding inventory file. That file supplies project IDs only, never
    expected values -- parsed defensively since its format is prose, not a
    contract this collector owns."""
    projects: list[str] = []
    if base_project:
        projects.append(base_project)
    else:
        result = run(["gcloud", "config", "get-value", "project"])
        if result.rc == 0 and result.stdout.strip():
            projects.append(result.stdout.strip())

    text = read_text(INVENTORY_PATH)
    if text:
        for match in PROJECT_ID_RE.findall(text):
            if match not in projects:
                projects.append(match)
    return projects


def enumerate_project_clusters(project: str, *, run: RunFn) -> tuple[list[dict], dict | None]:
    """One `clusters list` call, the full Cluster resources this collector
    reads every facet from. Returns `(clusters, command_record)` --
    `clusters` is `[]` and `command_record` is `None` when the call itself
    failed, so the caller knows this project contributed nothing rather
    than that it genuinely has no clusters."""
    argv = ["gcloud", "container", "clusters", "list", "--project", project, "--format", "json"]
    parsed, result = run_and_gate(argv, run=run)
    if parsed is None:
        log(f"{project}: clusters list gate failed (rc={result.rc}); no clusters known from this project")
        return [], None
    for c in parsed:
        c["_project"] = project
    return parsed, _record(" ".join(argv), result)


def cluster_eligibility(c: dict, *, now: datetime) -> str | None:
    """§1's scope rules for a cluster read but not compared. Returns the
    `limitations` sentence, or None when the cluster is a normal voting
    candidate."""
    status = c.get("status", "")
    if status != "RUNNING":
        return f"status {status}: excluded from every cohort, no facet compared."
    created = c.get("createTime", "")
    try:
        created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if (now - created_dt).total_seconds() < 24 * 3600:
            return f"created {created}: under 24h, excluded from every cohort."
    except ValueError:
        pass
    return None


# --------------------------------------------------------------------------- #
# §2: cohorts
# --------------------------------------------------------------------------- #


def cluster_mode(c: dict) -> str:
    return "autopilot" if (c.get("autopilot") or {}).get("enabled") else "standard"


def environment_of(c: dict) -> tuple[str, str]:
    """Returns `(environment, source)` -- `source` is `"label"` when a real
    label/field supplied it, `"inferred"` when a name-token guess did, and
    `"unknown"` when neither could. §4's confidence ladder reads `source`
    to decide whether a finding rests on an inferred cohort."""
    labels = c.get("resourceLabels") or {}
    for key in ("environment", "env", "stage", "tier"):
        val = (labels.get(key) or c.get(key) or "").lower()
        if val:
            return ENV_SYNONYMS.get(val, val), "label"
    tokens = re.split(r"[-_]", c.get("name", "").lower())
    for token in tokens:
        if token in ENV_SYNONYMS:
            return ENV_SYNONYMS[token], "inferred"
    return "unknown", "unknown"


def decide_cohort_strategy(clusters: list[dict]) -> str:
    envs = {environment_of(c)[0] for c in clusters}
    if envs - {"unknown"}:
        return "environment"
    if len({c.get("_project", "") for c in clusters}) > 1:
        return "project"
    return "mode-only"


def cohort_key(c: dict, strategy: str, env: str) -> tuple:
    mode = cluster_mode(c)
    if strategy == "environment":
        return (mode, env)
    if strategy == "project":
        return (mode, c.get("_project", ""))
    return (mode,)


# --------------------------------------------------------------------------- #
# §4: facet normalization. Each returns a token, or `None` to exclude the
# cluster from that facet's vote (an unreadable or inapplicable value) --
# the cluster still counts toward every other facet and stays in
# `scope.clusters`.
# --------------------------------------------------------------------------- #


def _pool_fraction(cluster: dict, get_flag: Callable[[dict], bool], exclude_pool: Callable[[dict], bool] | None = None) -> str | None:
    pools = [p for p in cluster.get("nodePools") or [] if not (exclude_pool and exclude_pool(p))]
    if not pools:
        return None
    flags = [bool(get_flag(p)) for p in pools]
    if all(flags):
        return "ALL"
    if not any(flags):
        return "NONE"
    return "SOME"


def _is_windows_pool(p: dict) -> bool:
    return ((p.get("config") or {}).get("imageType") or "").upper().startswith("WINDOWS")


def norm_release_channel(c: dict) -> str | None:
    ch = (c.get("releaseChannel") or {}).get("channel") or ""
    return None if ch in ("", "UNSPECIFIED") else ch


def norm_shielded_nodes(c: dict) -> str:
    return "ON" if (c.get("shieldedNodes") or {}).get("enabled") else "OFF"


def norm_secure_boot(c: dict) -> str | None:
    return _pool_fraction(c, lambda p: ((p.get("config") or {}).get("shieldedInstanceConfig") or {}).get("enableSecureBoot"), _is_windows_pool)


def norm_integrity_monitoring(c: dict) -> str | None:
    return _pool_fraction(c, lambda p: ((p.get("config") or {}).get("shieldedInstanceConfig") or {}).get("enableIntegrityMonitoring"), _is_windows_pool)


def norm_network_policy(c: dict) -> str:
    if (c.get("networkConfig") or {}).get("datapathProvider") == "ADVANCED_DATAPATH":
        return "DPV2"
    enabled = (c.get("networkPolicy") or {}).get("enabled")
    disabled_addon = ((c.get("addonsConfig") or {}).get("networkPolicyConfig") or {}).get("disabled")
    return "CALICO" if enabled and not disabled_addon else "OFF"


def norm_private_nodes(c: dict) -> str:
    return "ON" if (c.get("privateClusterConfig") or {}).get("enablePrivateNodes") else "OFF"


def norm_private_endpoint(c: dict) -> str:
    pcc = c.get("privateClusterConfig") or {}
    if "enablePrivateEndpoint" in pcc:
        return "ON" if pcc.get("enablePrivateEndpoint") else "OFF"
    cpe = (c.get("controlPlaneEndpointsConfig") or {}).get("ipEndpointsConfig") or {}
    if "enablePublicEndpoint" in cpe:
        return "OFF" if cpe.get("enablePublicEndpoint") else "ON"
    return "OFF"


def norm_authorized_networks(c: dict) -> str:
    manc = c.get("masterAuthorizedNetworksConfig") or {}
    return "ON" if manc.get("enabled") and manc.get("cidrBlocks") else "OFF"


def _component_set(cfg: dict | None) -> str:
    comps = sorted(set((cfg or {}).get("enableComponents") or []))
    return ",".join(comps) if comps else "NONE"


def norm_logging_components(c: dict) -> str:
    return _component_set((c.get("loggingConfig") or {}).get("componentConfig"))


def norm_monitoring_components(c: dict) -> str:
    return _component_set((c.get("monitoringConfig") or {}).get("componentConfig"))


def norm_managed_prometheus(c: dict) -> str:
    return "ON" if ((c.get("monitoringConfig") or {}).get("managedPrometheusConfig") or {}).get("enabled") else "OFF"


def norm_binary_authorization(c: dict) -> str:
    ba = c.get("binaryAuthorization") or {}
    mode = ba.get("evaluationMode")
    if mode is not None:
        return "OFF" if mode in ("DISABLED", "EVALUATION_MODE_UNSPECIFIED") else "ON"
    return "ON" if ba.get("enabled") else "OFF"


def norm_node_autoprovisioning(c: dict) -> str:
    return "ON" if (c.get("autoscaling") or {}).get("enableNodeAutoprovisioning") else "OFF"


def norm_pool_autoscaling(c: dict) -> str | None:
    return _pool_fraction(c, lambda p: (p.get("autoscaling") or {}).get("enabled"), lambda p: bool((p.get("config") or {}).get("taints")))


def norm_intra_node_visibility(c: dict) -> str:
    return "ON" if (c.get("networkConfig") or {}).get("enableIntraNodeVisibility") else "OFF"


def norm_datapath_provider(c: dict) -> str:
    return "ADVANCED_DATAPATH" if (c.get("networkConfig") or {}).get("datapathProvider") == "ADVANCED_DATAPATH" else "LEGACY_DATAPATH"


def norm_label_keys(c: dict) -> str:
    keys = sorted(k for k in (c.get("resourceLabels") or {}) if not k.startswith("goog"))
    return ",".join(keys) if keys else "NONE"


def norm_image_type(c: dict) -> str:
    types = set()
    for p in c.get("nodePools") or []:
        if _is_windows_pool(p):
            continue
        img = ((p.get("config") or {}).get("imageType") or "").upper()
        if img:
            types.add(img.replace("_CONTAINERD", ""))  # a rename, not a divergence
    return ",".join(sorted(types)) if types else "NONE"


def norm_database_encryption(c: dict) -> str:
    return "ENCRYPTED" if (c.get("databaseEncryption") or {}).get("state") == "ENCRYPTED" else "DECRYPTED"


def _flag_ne(observed: str, baseline: str) -> bool:
    return observed != baseline


def _flag_off_only(observed: str, baseline: str) -> bool:
    return observed == "OFF" and baseline != "OFF"


def _flag_not_superset(observed: str, baseline: str) -> bool:
    base_set = set(baseline.split(",")) if baseline != "NONE" else set()
    obs_set = set(observed.split(",")) if observed != "NONE" else set()
    return not obs_set.issuperset(base_set)


def _logging_severity(observed: str) -> str:
    parts = observed.split(",") if observed != "NONE" else []
    return "major" if "SYSTEM_COMPONENTS" not in parts else "minor"


class Facet(NamedTuple):
    slug: str
    field_path: str
    base_severity: str | Callable[[str], str]
    standard_only: bool
    autopilot_excluded: bool  # never flagged in an autopilot cohort, though still computed
    normalize: Callable[[dict], str | None]
    should_flag: Callable[[str, str], bool]


FACETS: tuple[Facet, ...] = (
    Facet("release-channel", ".releaseChannel.channel", "minor", False, False, norm_release_channel, _flag_ne),
    Facet("shielded-nodes", ".shieldedNodes.enabled", "major", False, False, norm_shielded_nodes, _flag_ne),
    Facet("secure-boot", ".nodePools[].config.shieldedInstanceConfig.enableSecureBoot", "major", True, False, norm_secure_boot, _flag_ne),
    Facet("integrity-monitoring", ".nodePools[].config.shieldedInstanceConfig.enableIntegrityMonitoring", "minor", True, False, norm_integrity_monitoring, _flag_ne),
    Facet("network-policy", ".networkConfig.datapathProvider / .networkPolicy.enabled", "major", False, False, norm_network_policy, _flag_off_only),
    Facet("private-nodes", ".privateClusterConfig.enablePrivateNodes", "critical", False, False, norm_private_nodes, _flag_ne),
    Facet("private-endpoint", ".privateClusterConfig.enablePrivateEndpoint", "major", False, False, norm_private_endpoint, _flag_ne),
    Facet("authorized-networks", ".masterAuthorizedNetworksConfig.enabled/.cidrBlocks", "critical", False, False, norm_authorized_networks, _flag_ne),
    Facet("logging-components", ".loggingConfig.componentConfig.enableComponents", _logging_severity, False, False, norm_logging_components, _flag_not_superset),
    Facet("monitoring-components", ".monitoringConfig.componentConfig.enableComponents", "minor", False, False, norm_monitoring_components, _flag_not_superset),
    Facet("managed-prometheus", ".monitoringConfig.managedPrometheusConfig.enabled", "minor", False, False, norm_managed_prometheus, _flag_ne),
    Facet("binary-authorization", ".binaryAuthorization.evaluationMode", "major", False, False, norm_binary_authorization, _flag_off_only),
    Facet("node-autoprovisioning", ".autoscaling.enableNodeAutoprovisioning", "minor", True, False, norm_node_autoprovisioning, _flag_ne),
    Facet("pool-autoscaling", ".nodePools[].autoscaling.enabled", "minor", True, False, norm_pool_autoscaling, _flag_ne),
    Facet("intra-node-visibility", ".networkConfig.enableIntraNodeVisibility", "minor", False, False, norm_intra_node_visibility, _flag_ne),
    Facet("datapath-provider", ".networkConfig.datapathProvider", "major", False, True, norm_datapath_provider, _flag_ne),
    Facet("label-keys", ".resourceLabels", "minor", False, False, norm_label_keys, _flag_not_superset),
    Facet("image-type", ".nodePools[].config.imageType", "minor", True, False, norm_image_type, _flag_not_superset),
    Facet("database-encryption", ".databaseEncryption.state", "critical", False, False, norm_database_encryption, _flag_ne),
)
FACETS_BY_SLUG = {f.slug: f for f in FACETS}


# --------------------------------------------------------------------------- #
# §3: baseline, confidence, severity ladder
# --------------------------------------------------------------------------- #


def compute_baseline(tokens: dict[str, str]) -> tuple[str, int, int, float] | None:
    n = len(tokens)
    if n < COHORT_FLOOR:
        return None
    counts = Counter(tokens.values())
    t_star, m = counts.most_common(1)[0]
    r = m / n
    if r < 2 / 3:
        return None
    return t_star, m, n, r


def apply_severity_ladder(base: str, r: float, k: int, inferred: bool) -> tuple[str | None, list[str]]:
    steps, applied = 0, []
    if r < 0.90:
        steps += 1
        applied.append(f"r={r:.2f}<0.90")
    if r < 0.80:
        steps += 1
        applied.append(f"r={r:.2f}<0.80")
    if k >= 3:
        steps += 1
        applied.append(f"k={k}>=3")
    if inferred:
        steps += 1
        applied.append("inferred environment")
    idx = SEVERITY_LEVELS.index(base) + steps
    if idx >= len(SEVERITY_LEVELS):
        return None, applied
    return SEVERITY_LEVELS[idx], applied


def build_excerpt(field_path: str, t_star: str, m: int, n: int, cohort_label: str, peer_names: list[str], observed: str, sev: str, base_sev: str, downgrades: list[str], r: float) -> str:
    peers = peer_names[:6]
    more = f", +{len(peer_names) - 6} more" if len(peer_names) > 6 else ""
    downgrade_text = ", ".join(downgrades) if downgrades else "none"
    return (
        f"baseline: {field_path}={t_star} in {m}/{n} clusters of cohort {cohort_label}\n"
        f"peers: {', '.join(peers)}{more}\n"
        f"observed: {observed}\n"
        f"consensus: {r:.2f} -> severity {sev} (base {base_sev}, {downgrade_text})"
    )


IMPACT = {
    "release-channel": "This cluster receives control-plane patches on a different schedule than its cohort.",
    "shielded-nodes": "Nodes boot unverified where every peer verifies them.",
    "secure-boot": "Nodes boot unverified where every peer verifies them.",
    "integrity-monitoring": "Node boot integrity is unmonitored where every peer monitors it.",
    "network-policy": "Pod-to-pod traffic is unrestricted here where peers segment it.",
    "private-nodes": "Node surface is exposed here that every peer keeps private.",
    "private-endpoint": "The control plane is reachable here in a way every peer keeps private.",
    "authorized-networks": "The control plane accepts connections from an unrestricted range here where peers restrict it.",
    "logging-components": "This cluster is invisible to fleet dashboards and alerts built on the peers' component set.",
    "monitoring-components": "This cluster is invisible to fleet dashboards and alerts built on the peers' component set.",
    "managed-prometheus": "This cluster's metrics are not queryable the way its peers' are.",
    "binary-authorization": "Unsigned or unattested images can run here where peers block them.",
    "node-autoprovisioning": "This cluster cannot absorb load the way its peers do without manual intervention.",
    "pool-autoscaling": "This cluster cannot absorb load the way its peers do without manual intervention.",
    "intra-node-visibility": "This cluster emits different flow telemetry than its cohort.",
    "datapath-provider": "This cluster enforces network policy through a different engine than its cohort.",
    "label-keys": "This cluster drops out of cost attribution and label-scoped queries its peers appear in.",
    "image-type": "This cluster's nodes carry a different patch cadence and hardening baseline than its peers.",
    "database-encryption": "Secrets in this cluster's etcd are not wrapped with the customer-managed key every peer uses.",
    "uncohorted": "This cluster diverges from its cohort on so many facets that its cohort labelling, not each facet, is likely wrong.",
}


def _emit(slug: str, cluster_name: str, excerpt: str, severity: str) -> dict:
    return {
        "check": slug,
        "namespace": "",
        "object": f"Cluster/{cluster_name}",
        "severity": severity,
        "excerpt": excerpt,
        "impact": IMPACT[slug],
        "needs_triage": None,
    }


def cohort_layout(clusters: list[dict], *, now: datetime) -> tuple[dict[str, str], dict[tuple, list[dict]], dict[str, tuple[str, str]]]:
    """§1's eligibility and §2's cohorting, as `(ineligible, cohorts, env_of)`.

    Shared by the vote and by `cohort_limitations`, which has to agree with it
    exactly: a cluster the vote skipped and the limitations did not explain is
    the silent-clean failure this stream is most prone to.
    """
    ineligible: dict[str, str] = {}
    eligible: list[dict] = []
    for c in clusters:
        why = cluster_eligibility(c, now=now)
        if why is None:
            eligible.append(c)
        else:
            ineligible[c["name"]] = why

    strategy = decide_cohort_strategy(eligible)
    env_of: dict[str, tuple[str, str]] = {c["name"]: environment_of(c) for c in eligible}

    cohorts: dict[tuple, list[dict]] = {}
    for c in eligible:
        env, _ = env_of[c["name"]]
        cohorts.setdefault(cohort_key(c, strategy, env), []).append(c)
    return ineligible, cohorts, env_of


def cohort_limitations(clusters: list[dict], *, now: datetime) -> dict[str, str]:
    """§2.4's `limitations` sentence for every cluster no facet could compare.

    Drift is comparative, so a cluster with too few peers has nothing to drift
    from and every facet abstains for it. That is a legitimate outcome and the
    SOP says so — but until this function existed the manifest recorded it as
    `outcome: "collected"` with an empty `commands` list, and `"collected"`
    tells the model that every applicable check already ran and it must not
    re-run the cluster by hand. A live four-cluster fleet split into cohorts of
    2, 1 and 1, every one under the floor, and the collector reported four
    clusters fully collected four seconds after it started, having compared
    nothing.

    Two exclusions, both named by the SOP. `cluster_eligibility` already
    returns its sentence and only ever needed plumbing; the undersized-cohort
    sentence is §2.4's own wording.

    A cluster whose cohort did reach the floor is absent from the result even
    if some individual facet abstained for it — that cluster was compared, and
    §4 is explicit that a facet returning no token excludes the cluster from
    that facet's vote alone.
    """
    ineligible, cohorts, _ = cohort_layout(clusters, now=now)
    out = dict(ineligible)
    for key, members in cohorts.items():
        if len(members) >= COHORT_FLOOR:
            continue
        label = "/".join(str(k) for k in key)
        for c in members:
            out[c["name"]] = (
                f"cohort {label} has only {len(members)} comparable clusters "
                f"(minimum {COHORT_FLOOR}), no facet compared"
            )
    return out


def compute_drift(clusters: list[dict], *, now: datetime) -> tuple[dict[str, list[str]], dict[str, list[dict]]]:
    """Returns `(checks_run_by_cluster, candidates_by_cluster)` -- the
    facets actually voted on for each cluster, and the outlier findings
    that survived the severity ladder."""
    checks_run: dict[str, list[str]] = {c["name"]: [] for c in clusters}
    candidates: dict[str, list[dict]] = {c["name"]: [] for c in clusters}
    outlier_facet_count: dict[str, int] = {c["name"]: 0 for c in clusters}

    _, cohorts, env_of = cohort_layout(clusters, now=now)

    for key, members in cohorts.items():
        if len(members) < COHORT_FLOOR:
            continue
        mode = key[0]
        cohort_label = "/".join(str(k) for k in key)
        for facet in FACETS:
            if facet.standard_only and mode == "autopilot":
                continue
            tokens: dict[str, str] = {}
            for c in members:
                token = facet.normalize(c)
                if token is not None:
                    tokens[c["name"]] = token
            baseline = compute_baseline(tokens)
            if baseline is None:
                continue
            t_star, m, n, r = baseline
            for c in members:
                if c["name"] not in tokens:
                    continue
                checks_run[c["name"]].append(facet.slug)
            if facet.autopilot_excluded and mode == "autopilot":
                continue
            baseline_members = [name for name, tok in tokens.items() if tok == t_star]
            baseline_inferred = any(env_of[name][1] == "inferred" for name in baseline_members)
            outliers = {name: tok for name, tok in tokens.items() if facet.should_flag(tok, t_star)}
            k = len(outliers)
            for name, observed in outliers.items():
                inferred = baseline_inferred or env_of[name][1] == "inferred"
                base_sev = facet.base_severity(observed) if callable(facet.base_severity) else facet.base_severity
                sev, downgrades = apply_severity_ladder(base_sev, r, k, inferred)
                if sev is None:
                    continue
                excerpt = build_excerpt(facet.field_path, t_star, m, n, cohort_label, sorted(tokens), observed, sev, base_sev, downgrades, r)
                candidates[name].append(_emit(facet.slug, name, excerpt, sev))
                outlier_facet_count[name] += 1

    # §3.6 split-cluster guard
    for name, count in outlier_facet_count.items():
        if count >= 6:
            facet_names = sorted({cand["check"] for cand in candidates[name]})
            candidates[name] = [
                _emit(
                    "uncohorted", name,
                    f"outlier on {count} facets in one run: {', '.join(facet_names)} -- likely a cohort-labelling problem, not {count} independent drifts.",
                    "major",
                )
            ]
    return checks_run, candidates


def collect_project(project: str, *, run: RunFn, now: datetime) -> tuple[list[dict], dict | None]:
    return enumerate_project_clusters(project, run=run)


def collect_fleet(project: str | None = None, *, run: RunFn = default_run, read_text: Callable[[str], str | None] = default_read_text, max_workers: int = MAX_WORKERS, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    projects = discover_projects(project, run=run, read_text=read_text)

    all_clusters: list[dict] = []
    command_by_project: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(collect_project, p, run=run, now=now): p for p in projects}
        for future in as_completed(futures):
            clusters, record = future.result()
            all_clusters.extend(clusters)
            if record is not None:
                command_by_project[futures[future]] = record

    checks_run, candidates = compute_drift(all_clusters, now=now)
    limitations = cohort_limitations(all_clusters, now=now)

    entries = []
    for c in all_clusters:
        project_name = c.get("_project", "")
        record = command_by_project.get(project_name)
        commands = [{"check": slug, **record} for slug in checks_run.get(c["name"], [])] if record else []
        entry = {
            "name": c["name"],
            "project": project_name,
            "location": c.get("location") or c.get("zone") or "",
            # Still `collected` when a cohort floored out. Nothing was
            # compared, but nothing the model can run by hand would compare it
            # either -- the peers do not exist -- and `gate-failed` asks for
            # exactly that retry. The `limitations` sentence beside it is what
            # carries the truth, and §6's coverage arithmetic reads it.
            "outcome": "collected",
            "commands": commands,
            "candidates": candidates.get(c["name"], []),
        }
        note = limitations.get(c["name"])
        if note:
            entry["limitations"] = note
        entries.append(entry)

    return {
        "version": MANIFEST_VERSION,
        "audit": "fleet-consistency-drift",
        "started_at": started_at,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "clusters": entries,
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
