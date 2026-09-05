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
import shlex
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NamedTuple

MANIFEST_VERSION = 1

# A digest of this file, published in the manifest. `audit_report.py` compares
# it against the previous run's to tell a finding that stopped reproducing from
# a check that stopped looking; see `render_delta_comment`.
# Long enough that two collector sources will not collide, short enough to
# read in a log line. It has to agree across every collector: the comparison
# is between one run's revision and the last one's, so a file that truncated
# differently would report a moved collector on the run that changed it.
REVISION_DIGEST_CHARS = 12
CHECKS_REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[
    :REVISION_DIGEST_CHARS
]

DEFAULT_TIMEOUT_S = 60
MAX_WORKERS = 8
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

def discover_projects(base_project: str | None, *, run: RunFn) -> list[str]:
    """§1.1's project scope, on the same terms as every sibling collector:
    `--project` scopes the run, and without one this is the active project plus
    every other project that answers `clusters list` with at least one cluster.

    This used to add project IDs scraped out of `/opt/data/INVENTORY.raw.md`
    with `re.compile(r"\\b([a-z][a-z0-9-]{4,28}[a-z0-9])\\b")`, which matches
    any lowercase English word of six to thirty characters. That file is
    model-written prose with no project-ID marker in its contract, so
    `cluster`, `namespace`, `production` and `monitoring` all became targets
    and the run fanned out one `gcloud container clusters list
    --project=namespace` per token. The scrape also ran unconditionally, after
    the `--project` branch, so `--project` never actually scoped anything --
    contradicting both this module's `--help` and
    `fleet_consistency_drift_sop.md` §1.1. `patch_readiness.py` and
    `fleet_waste.py` both discover against `gcloud projects list`, which
    answers with project IDs rather than with English.
    """
    if base_project:
        return [base_project]

    result = run(["gcloud", "config", "get-value", "project"])
    base = result.stdout.strip() if result.rc == 0 else ""
    projects = [base] if base else []

    _, list_result = run_and_gate(["gcloud", "projects", "list", "--format", "value(projectId)"], run=run)
    if list_result.rc != 0:
        return projects  # discovery unavailable; the base project is the whole scope

    for candidate in (p.strip() for p in (list_result.stdout or "").splitlines()):
        if not candidate or candidate in projects:
            continue
        parsed, _ = run_and_gate(
            ["gcloud", "container", "clusters", "list", "--project", candidate, "--format", "json"], run=run
        )
        # `[]` and `None` are different answers, and only the first means the
        # project owes this audit nothing. A project this probe could not read
        # stays in scope so the manifest records the loss, exactly as the two
        # sibling collectors' copies of this guard explain.
        if parsed is None or parsed:
            projects.append(candidate)
    return projects


def enumerate_project_clusters(project: str, *, run: RunFn) -> tuple[list[dict], dict | None, str | None]:
    """One `clusters list` call, the full Cluster resources this collector
    reads every facet from. Returns `(clusters, command_record, error)` --
    `clusters` is `[]`, `command_record` is `None` and `error` carries what
    gcloud said when the call itself failed, so the caller knows this project
    contributed nothing rather than that it genuinely has no clusters, and can
    say so in the manifest rather than only in a log line."""
    argv = ["gcloud", "container", "clusters", "list", "--project", project, "--format", "json"]
    parsed, result = run_and_gate(argv, run=run)
    if parsed is None:
        log(f"{project}: clusters list gate failed (rc={result.rc}); no clusters known from this project")
        return [], None, f"clusters list rc={result.rc}: {result.stderr.strip()[:300] or 'no stderr'}"
    for c in parsed:
        c["_project"] = project
    return parsed, _record(shlex.join(argv), result), None


def cluster_eligibility(c: dict, *, now: datetime) -> str | None:
    """§1's scope rules for a cluster read but not compared. Returns the
    `limitations` sentence, or None when the cluster is a normal voting
    candidate."""
    status = c.get("status", "")
    if status != "RUNNING":
        return f"status {status}: excluded from every cohort, no facet compared."
    # `.get("createTime", "")` returns `None` for a key that is present and null,
    # and `None.replace` is an `AttributeError` that `except ValueError` does not
    # catch. There is no caller between here and `main` that catches it either,
    # and the SOP invokes this module as `fleet_drift.py > manifest_….json`, so
    # the shell has already truncated the manifest by the time the traceback
    # prints: one cluster with an unexpected `createTime` loses the whole fleet.
    # An unreadable creation time means "cannot tell how old this is", which is
    # the same answer as an absent one -- treat the cluster as settled rather
    # than excluding it from every cohort on a field it may simply not carry.
    created = c.get("createTime")
    if isinstance(created, str) and created:
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
    """Which axis splits this fleet into comparable groups.

    A single resolved environment used to be enough to pick `environment` for
    the whole fleet, and on a fleet nobody has labelled that makes coverage
    *worse* than having no signal at all. Sixteen live clusters, two of them
    merely carrying `test` somewhere in their name: inference resolved those
    two, the fleet switched to environment cohorts, and both landed alone in
    cohorts of one while the other fourteen piled into `unknown`. Two clusters
    lost all coverage. Strip those two names and the same fleet cohorts by mode
    and compares all sixteen.

    So the two signals are not interchangeable. A label is the customer
    declaring how they organize their fleet and any one of them settles it. A
    name token is our guess, and a guess about a couple of clusters should not
    redraw the cohorts for everybody -- it earns the strategy only when it
    resolves enough of the fleet to be the fleet's actual naming convention.
    """
    envs = [environment_of(c) for c in clusters]
    if any(source == "label" for _, source in envs):
        return "environment"
    named = sum(1 for env, _ in envs if env != "unknown")
    if named and named * 2 >= len(clusters):
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
    # GKE carries this on `masterAuthorizedNetworksConfig` or, for clusters on the
    # newer surface, `controlPlaneEndpointsConfig.ipEndpointsConfig`, and rejects
    # both at once -- so reading one field alone drifts the other's clusters OFF.
    ip_cfg = (c.get("controlPlaneEndpointsConfig") or {}).get("ipEndpointsConfig") or {}
    for manc in (c.get("masterAuthorizedNetworksConfig"), ip_cfg.get("authorizedNetworksConfig")):
        manc = manc or {}
        if manc.get("enabled") and manc.get("cidrBlocks"):
            return "ON"
    return "OFF"


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


# Every two-token facet spells "the control is not in force" one of two ways.
_DEGRADED = frozenset({"OFF", "DECRYPTED"})


def _flag_off_only(observed: str, baseline: str) -> bool:
    return observed in _DEGRADED and baseline not in _DEGRADED


def _flag_less_only(observed: str, baseline: str) -> bool:
    """`_flag_off_only` for the three-token `_pool_fraction` facets.

    Flags a cluster that covers fewer of its pools than the cohort does, and
    stays quiet when it covers more. The facets scored this way state an impact
    ("nodes boot unverified", "cannot absorb load the way its peers do") and
    carry a remediation that turns the feature *on*, so in the other direction
    the finding reads backwards and cannot be closed: enabling the feature on
    the remaining pools moves the token to ALL, which still differs from a NONE
    baseline, so the same finding returns on the next run.
    """
    rank = {"NONE": 0, "SOME": 1, "ALL": 2}
    return rank.get(observed, 0) < rank.get(baseline, 0)


def _tokens(value: str) -> set[str]:
    return set(value.split(",")) if value != "NONE" else set()


def _flag_not_superset(observed: str, baseline: str) -> bool:
    return not _tokens(observed).issuperset(_tokens(baseline))


def _missing_tokens(observed: str, baseline: str) -> list[str]:
    """The baseline tokens the outlier does not carry.

    `_flag_not_superset` computes this difference to decide and throws it away,
    leaving the model to re-derive it from `observed` and `baseline` in order to
    write a title. It got that derivation wrong on the live fleet:
    `drift-peer-std-4` observed `NONE` against a `SYSTEM_COMPONENTS,WORKLOADS`
    baseline and published as "logging component set missing WORKLOADS relative
    to its cohort" -- one of the two missing components, reading as though system
    logging still worked. The cluster carried `loggingService: none` and logged
    nothing at all. Two lines below, the same finding's excerpt said
    `observed: NONE` and its impact line said "no logging component config at
    all", so the title contradicted its own evidence, and the title is the line a
    reader sees first and the one the ledger's finding table shows.

    Meaningful only for the `_flag_not_superset` facets, whose tokens are
    comma-joined sets; the call site passes `None` for the rest rather than
    emitting a line whose set framing does not apply to an `ON`/`OFF` facet.
    """
    return sorted(_tokens(baseline) - _tokens(observed))


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
    Facet("shielded-nodes", ".shieldedNodes.enabled", "major", False, False, norm_shielded_nodes, _flag_off_only),
    Facet("secure-boot", ".nodePools[].config.shieldedInstanceConfig.enableSecureBoot", "major", True, False, norm_secure_boot, _flag_less_only),
    Facet("integrity-monitoring", ".nodePools[].config.shieldedInstanceConfig.enableIntegrityMonitoring", "minor", True, False, norm_integrity_monitoring, _flag_less_only),
    Facet("network-policy", ".networkConfig.datapathProvider / .networkPolicy.enabled", "major", False, False, norm_network_policy, _flag_off_only),
    Facet("private-nodes", ".privateClusterConfig.enablePrivateNodes", "critical", False, False, norm_private_nodes, _flag_off_only),
    Facet("private-endpoint", ".privateClusterConfig.enablePrivateEndpoint / .controlPlaneEndpointsConfig.ipEndpointsConfig.enablePublicEndpoint", "major", False, False, norm_private_endpoint, _flag_off_only),
    Facet("authorized-networks", ".masterAuthorizedNetworksConfig / .controlPlaneEndpointsConfig.ipEndpointsConfig.authorizedNetworksConfig", "critical", False, False, norm_authorized_networks, _flag_off_only),
    Facet("logging-components", ".loggingConfig.componentConfig.enableComponents", _logging_severity, False, False, norm_logging_components, _flag_not_superset),
    Facet("monitoring-components", ".monitoringConfig.componentConfig.enableComponents", "minor", False, False, norm_monitoring_components, _flag_not_superset),
    Facet("managed-prometheus", ".monitoringConfig.managedPrometheusConfig.enabled", "minor", False, False, norm_managed_prometheus, _flag_off_only),
    Facet("binary-authorization", ".binaryAuthorization.evaluationMode", "major", False, False, norm_binary_authorization, _flag_off_only),
    Facet("node-autoprovisioning", ".autoscaling.enableNodeAutoprovisioning", "minor", True, False, norm_node_autoprovisioning, _flag_off_only),
    Facet("pool-autoscaling", ".nodePools[].autoscaling.enabled", "minor", True, False, norm_pool_autoscaling, _flag_less_only),
    Facet("intra-node-visibility", ".networkConfig.enableIntraNodeVisibility", "minor", False, False, norm_intra_node_visibility, _flag_ne),
    Facet("datapath-provider", ".networkConfig.datapathProvider", "major", False, True, norm_datapath_provider, _flag_ne),
    Facet("label-keys", ".resourceLabels", "minor", False, False, norm_label_keys, _flag_not_superset),
    Facet("image-type", ".nodePools[].config.imageType", "minor", True, False, norm_image_type, _flag_not_superset),
    Facet("database-encryption", ".databaseEncryption.state", "critical", False, False, norm_database_encryption, _flag_off_only),
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


def build_excerpt(field_path: str, t_star: str, m: int, n: int, cohort_label: str, peer_names: list[str], observed: str, sev: str, base_sev: str, downgrades: list[str], r: float, missing: list[str] | None = None) -> str:
    peers = peer_names[:6]
    more = f", +{len(peer_names) - 6} more" if len(peer_names) > 6 else ""
    downgrade_text = ", ".join(downgrades) if downgrades else "none"
    missing_line = f"missing: {', '.join(missing)}\n" if missing else ""
    return (
        f"baseline: {field_path}={t_star} in {m}/{n} clusters of cohort {cohort_label}\n"
        f"peers: {', '.join(peers)}{more}\n"
        f"observed: {observed}\n"
        f"{missing_line}"
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


def ckey(c: dict) -> tuple[str, str, str]:
    """A cluster's identity inside this collector.

    Not its name. Cluster names are project-scoped in GKE, so `prod` in two
    projects is two clusters -- and this is the one collector that sweeps every
    project in the fleet by design, which is exactly where the collision lands.
    Keying the per-cluster dicts by bare name merged the pair three ways:
    `checks_run` accumulated both clusters' facets into one list, which §6's
    validator rejects as a duplicate `checks_run` entry and which fails the run;
    `candidates` handed each manifest entry the other cluster's findings, each
    published under its own `Cluster/<name>` object and so indistinguishable;
    and `outlier_facet_count` summed the two, which trips §3.6's six-facet
    split-cluster guard on a pair of clusters that each diverge on three.
    `ineligible` and `env_of` were last-write-wins on top of that.

    The finding's `object` stays `Cluster/<name>` -- §6 specifies that, and the
    harness derives identity from `cluster` alongside it.
    """
    return (c.get("_project", ""), c.get("location") or c.get("zone") or "", c.get("name", ""))


def cohort_layout(clusters: list[dict], *, now: datetime) -> tuple[dict[tuple, str], dict[tuple, list[dict]], dict[tuple, tuple[str, str]], str]:
    """§1's eligibility and §2's cohorting, as `(ineligible, cohorts, env_of,
    strategy)`, the first three keyed by `ckey`.

    Shared by the vote and by `cohort_limitations`, which has to agree with it
    exactly: a cluster the vote skipped and the limitations did not explain is
    the silent-clean failure this stream is most prone to.
    """
    ineligible: dict[tuple, str] = {}
    eligible: list[dict] = []
    for c in clusters:
        why = cluster_eligibility(c, now=now)
        if why is None:
            eligible.append(c)
        else:
            ineligible[ckey(c)] = why

    strategy = decide_cohort_strategy(eligible)
    env_of: dict[tuple, tuple[str, str]] = {ckey(c): environment_of(c) for c in eligible}

    cohorts: dict[tuple, list[dict]] = {}
    for c in eligible:
        env, _ = env_of[ckey(c)]
        cohorts.setdefault(cohort_key(c, strategy, env), []).append(c)
    return ineligible, cohorts, env_of, strategy


def cohort_limitations(clusters: list[dict], *, now: datetime) -> dict[tuple, str]:
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
    ineligible, cohorts, env_of, _strategy = cohort_layout(clusters, now=now)
    out = dict(ineligible)
    labelled = sum(1 for _, source in env_of.values() if source == "label")
    for key, members in cohorts.items():
        if len(members) >= COHORT_FLOOR:
            continue
        label = "/".join(str(k) for k in key)
        # A floored-out cohort is 1 or 2 members, so this line reads "only 1
        # comparable clusters" in the single-member case that kube-agents-host
        # hits on every run -- the most-read sentence the stream emits.
        noun = "cluster" if len(members) == 1 else "clusters"
        for c in members:
            out[ckey(c)] = (
                f"cohort {label} has only {len(members)} comparable {noun} "
                f"(minimum {COHORT_FLOOR}), no facet compared"
                f"{_unlabelled_cause(key, labelled, len(env_of))}"
            )
    return out


def _unlabelled_cause(key: tuple, labelled: int, total: int) -> str:
    """Why the `unknown` cohort floored out, when the rest of the fleet did not.

    The floor sentence is true and gives the reader nothing to do with it. On
    the live fleet fifteen of sixteen clusters carry `environment=test` and
    kube-agents-host carries no environment label at all, so it cohorts alone
    under §2.3's rule that `unknown` never merges into a named cohort -- and
    the install's own host cluster is the one cluster this stream can never
    compare, on this run or any future one. Nothing in "cohort
    standard/unknown has only 1 comparable cluster" says that a label is the
    difference, so the gap reads as a quirk of fleet size and gets waited out
    rather than fixed.

    Counted by label rather than by resolved environment, because the sentence
    claims the other clusters carry one. Under the inferred strategy they do
    not -- their environment came from a name token -- and "12 of 16 do" would
    then be false. Counting the source keeps it literally true, and doubles as
    the guard on the `unknown` test: `decide_cohort_strategy` returns
    `environment` for any label at all, so a non-zero count means the key's
    last element really is an environment and not a project that happens to be
    named `unknown`.
    """
    if key[-1:] != ("unknown",) or not labelled:
        return ""
    return (
        f"; it carries no environment label while {labelled} of {total} do,"
        " and an unlabelled cluster never joins a named cohort -- label it"
        " to compare it"
    )


def autopilot_not_applicable(clusters: list[dict]) -> dict[tuple, list[dict]]:
    """§6's `checks_not_applicable` for the facets `compute_drift` refuses to
    compute on Autopilot.

    Five facets carry `standard_only`, and line 565 drops each of them for an
    Autopilot cohort. Dropping them is right — every one reads a field under
    `.nodePools[]` or names a node-management setting Google owns there — but
    until this function existed the collector said nothing about having done
    it. A slug missing from `commands` is also exactly how a check nobody ran
    looks, so §6 counts it as a coverage gap unless the model happens to know
    which GKE settings Autopilot withholds and excuses it by hand. `fleet_waste`
    already ran that experiment: a model that remembered one of two slugs
    published three coverage gaps for a check those clusters do not owe.

    Keyed off the cluster's own mode rather than its cohort's. The two agree
    wherever line 565 fires — an Autopilot cohort's members are all Autopilot —
    but the roster arithmetic in §6 is per-cluster, so an Autopilot cluster
    whose cohort floored out has the same five inapplicable checks and should
    have the same fourteen in its denominator. Its `limitations` sentence then
    accounts for those fourteen instead of overstating nineteen.

    `datapath-provider` is deliberately absent. It carries `autopilot_excluded`
    rather than `standard_only`: the facet is computed and recorded in
    `checks_run`, and only the flagging is suppressed, so the manifest already
    makes a claim about it that this table would contradict.
    """
    standard_only_slugs = [f.slug for f in FACETS if f.standard_only]
    out: dict[tuple, list[dict]] = {}
    for c in clusters:
        if cluster_mode(c) != "autopilot":
            continue
        out[ckey(c)] = [
            {
                "check": slug,
                "reason": (
                    "GKE Autopilot: Google manages the nodes and exposes no user node pool, "
                    f"so `{FACETS_BY_SLUG[slug].field_path}` has no value to compare against "
                    "the cohort."
                ),
            }
            for slug in standard_only_slugs
        ]
    return out


def _autoscaling_countable_pools(c: dict) -> list[dict]:
    """The pools `norm_pool_autoscaling` actually votes over -- the same
    `exclude_pool` predicate it hands `_pool_fraction`, which drops the tainted
    pools §4.8 calls deliberately fixed-size."""
    return [p for p in c.get("nodePools") or [] if not (p.get("config") or {}).get("taints")]


def _shape_mismatch(facet: Facet, cluster: dict, baseline_clusters: list[dict]) -> bool:
    """§4.8's "do NOT flag single-pool clusters against multi-pool peers".

    A one-pool cluster can only ever normalize to `ALL` or `NONE`; `SOME` is
    unreachable for it. So against a cohort whose baseline is `SOME` -- a
    baseline only multi-pool clusters can hold -- it is an outlier that no
    change can bring into line: enabling autoscaling on its single pool moves it
    to `ALL`, still not `SOME`, and the finding returns next week having cost a
    node-pool update. That is the same unclosable shape `_flag_less_only`
    already guards in the other direction, and §4.8 names it explicitly.

    Scoped to `pool-autoscaling` because that is the facet §4.8 says it for.
    §4.3's `secure-boot` and `integrity-monitoring` share the ALL/SOME/NONE
    scale but list a different set of suppressions and not this one.
    """
    if facet.slug != "pool-autoscaling":
        return False
    if len(_autoscaling_countable_pools(cluster)) != 1:
        return False
    return any(len(_autoscaling_countable_pools(peer)) > 1 for peer in baseline_clusters)


def compute_drift(clusters: list[dict], *, now: datetime) -> tuple[dict[tuple, list[str]], dict[tuple, list[dict]]]:
    """Returns `(checks_run_by_cluster, candidates_by_cluster)` -- the
    facets actually voted on for each cluster, and the outlier findings
    that survived the severity ladder. Both are keyed by `ckey`, not by
    cluster name; see `ckey` for why a name is not an identity here."""
    checks_run: dict[tuple, list[str]] = {ckey(c): [] for c in clusters}
    candidates: dict[tuple, list[dict]] = {ckey(c): [] for c in clusters}
    outlier_facet_count: dict[tuple, int] = {ckey(c): 0 for c in clusters}

    _, cohorts, env_of, strategy = cohort_layout(clusters, now=now)
    # §3.5 downgrades a finding whose "cohort membership rests on an inferred
    # environment". Under the `project` and `mode-only` strategies no cohort key
    # holds an environment at all, so no membership rests on one -- but
    # `environment_of` still reports `inferred` for any cluster with `test` or
    # `prod` somewhere in its name, and reading that unconditionally downgraded
    # every finding in the cohort (`baseline_inferred` is an `any()` over the
    # baseline holders, so one such name was enough). That is a step the SOP
    # does not ask for, and a step is the difference between a `minor` finding
    # and a dropped one.
    env_matters = strategy == "environment"

    for key, members in cohorts.items():
        if len(members) < COHORT_FLOOR:
            continue
        mode = key[0]
        cohort_label = "/".join(str(k) for k in key)
        for facet in FACETS:
            if facet.standard_only and mode == "autopilot":
                continue
            tokens: dict[tuple, str] = {}
            for c in members:
                token = facet.normalize(c)
                if token is not None:
                    tokens[ckey(c)] = token
            baseline = compute_baseline(tokens)
            if baseline is None:
                continue
            t_star, m, n, r = baseline
            voters = [c for c in members if ckey(c) in tokens]
            for c in voters:
                checks_run[ckey(c)].append(facet.slug)
            if facet.autopilot_excluded and mode == "autopilot":
                continue
            baseline_clusters = [c for c in voters if tokens[ckey(c)] == t_star]
            baseline_inferred = env_matters and any(env_of[ckey(c)][1] == "inferred" for c in baseline_clusters)
            # The clusters that hold the baseline, not every cluster that voted.
            # `peers:` sits one line under "in {m}/{n} clusters" and one line
            # over the outlier's own `observed:`, so listing all `n` names
            # contradicted both of its neighbours: it printed 10 names beside a
            # claim that 9 clusters agree, and among them the very cluster the
            # finding is about. A reader checking the comparison against
            # `drift-peer-std-4 emits no logging components` found
            # `drift-peer-std-4` in the list of clusters that do.
            peer_names = sorted(c.get("name", "") for c in baseline_clusters)
            # §3.2 defines `k` as `n - m`, the count of voting members not on
            # the baseline token -- how split the cohort is. `len(outliers)` is
            # a different number wherever `should_flag` is narrower than "differs
            # from `t*`", which is every facet except the two on `_flag_ne`: a
            # cluster that diverges upward is not flagged but is still divergent.
            # `len(outliers) <= n - m` always, so reading it here under-counted
            # the split and under-applied §3.5's `k >= 3` step -- publishing at a
            # severity above the one the SOP specifies, and keeping findings the
            # SOP would have dropped below `minor`.
            k = n - m
            for c in voters:
                observed = tokens[ckey(c)]
                if not facet.should_flag(observed, t_star):
                    continue
                if _shape_mismatch(facet, c, baseline_clusters):
                    continue
                name = c.get("name", "")
                inferred = baseline_inferred or (env_matters and env_of[ckey(c)][1] == "inferred")
                base_sev = facet.base_severity(observed) if callable(facet.base_severity) else facet.base_severity
                sev, downgrades = apply_severity_ladder(base_sev, r, k, inferred)
                if sev is None:
                    continue
                # Only the set-valued facets have a "missing" to name, and
                # `_flag_not_superset` is exactly the predicate that says so:
                # it is the gate that already took this difference.
                missing = _missing_tokens(observed, t_star) if facet.should_flag is _flag_not_superset else None
                excerpt = build_excerpt(facet.field_path, t_star, m, n, cohort_label, peer_names, observed, sev, base_sev, downgrades, r, missing)
                candidates[ckey(c)].append(_emit(facet.slug, name, excerpt, sev))
                outlier_facet_count[ckey(c)] += 1

    # §3.6 split-cluster guard
    for cluster_key, count in outlier_facet_count.items():
        if count >= 6:
            facet_names = sorted({cand["check"] for cand in candidates[cluster_key]})
            candidates[cluster_key] = [
                _emit(
                    "uncohorted", cluster_key[-1],
                    f"outlier on {count} facets in one run: {', '.join(facet_names)} -- likely a cohort-labelling problem, not {count} independent drifts.",
                    "major",
                )
            ]
    return checks_run, candidates


def collect_project(project: str, *, run: RunFn, now: datetime) -> tuple[list[dict], dict | None, str | None]:
    return enumerate_project_clusters(project, run=run)


def collect_fleet(project: str | None = None, *, run: RunFn = default_run, max_workers: int = MAX_WORKERS, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    projects = discover_projects(project, run=run)

    all_clusters: list[dict] = []
    command_by_project: dict[str, dict] = {}
    failed_projects: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(collect_project, p, run=run, now=now): p for p in projects}
        for future in as_completed(futures):
            try:
                clusters, record, error = future.result()
            except Exception as exc:  # noqa: BLE001
                # `future.result()` re-raises, so one unhandled exception on
                # one project used to abort the whole run — and the SOP
                # invokes this as `fleet_drift.py … > manifest_….json`, so by
                # then the shell had truncated the file and the fleet was lost
                # to one bad object. A failed project is a shape this loop
                # already has: it lands in `failed_projects`, which §6 turns
                # into a coverage gap the document must account for.
                log(f"{futures[future]}: collector raised {type(exc).__name__}: {exc}")
                clusters, record, error = [], None, f"collector raised {type(exc).__name__}: {exc}"[:300]
            all_clusters.extend(clusters)
            if record is not None:
                command_by_project[futures[future]] = record
            elif error:
                failed_projects[futures[future]] = error

    checks_run, candidates = compute_drift(all_clusters, now=now)
    limitations = cohort_limitations(all_clusters, now=now)
    not_applicable = autopilot_not_applicable(all_clusters)

    entries = []
    for c in all_clusters:
        project_name = c.get("_project", "")
        record = command_by_project.get(project_name)
        cluster_key = ckey(c)
        commands = [{"check": slug, **record} for slug in checks_run.get(cluster_key, [])] if record else []
        entry = {
            "name": c["name"],
            "project": project_name,
            "location": c.get("location") or c.get("zone") or "",
            "autopilot": cluster_mode(c) == "autopilot",
            # Still `collected` when a cohort floored out. Nothing was
            # compared, but nothing the model can run by hand would compare it
            # either -- the peers do not exist -- and `gate-failed` asks for
            # exactly that retry. The `limitations` sentence beside it is what
            # carries the truth, and §6's coverage arithmetic reads it.
            "outcome": "collected",
            "commands": commands,
            "candidates": candidates.get(cluster_key, []),
        }
        note = limitations.get(cluster_key)
        if note:
            entry["limitations"] = note
        skipped = not_applicable.get(cluster_key)
        if skipped:
            entry["checks_not_applicable"] = skipped
        entries.append(entry)

    # A project whose `clusters list` failed contributed no clusters, and with
    # no entry of its own it contributes no evidence of that either -- the
    # manifest then reads exactly like a fleet that never had those clusters in
    # it. That is worse here than in a per-cluster stream: drift compares each
    # cluster against its cohort peers, so clusters missing from the comparison
    # quietly change what counts as an outlier, and every surviving cluster's
    # verdict is computed against a fleet nobody knows is short. Recording the
    # project as a gate-failed target is what makes the loss say so --
    # cross_check_manifest requires the document to account for it, and §6
    # turns that into a coverage gap.
    entries += [
        {
            "name": f"project/{project_name}",
            "project": project_name,
            "location": "global",
            "outcome": "gate-failed",
            "error": error,
        }
        for project_name, error in sorted(failed_projects.items())
    ]

    return {
        "version": MANIFEST_VERSION,
        "checks_revision": CHECKS_REVISION,
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
