#!/opt/hermes/.venv/bin/python3
"""patch_readiness.py — Procedural collector for the Upgrade & Patch
Readiness Audit (`security-patch-orchestrator`).

See docs/designs/fleet-audit-collectors-and-status.md §4.2, §10 phase 4, and
governance/security_patch_orchestrator_sop.md.

This stream's own collector, in the shape §4.2 calls "a per-stream collector
of their own shape" rather than folded into `collect.py`: every other
converted stream reads workload state through `kubectl`, which needs a
per-cluster kubeconfig; this one reads only GKE control-plane and node-pool
*metadata* through `gcloud container`, and needs no kubeconfig at all. Its
collection is also flatter than any `kubectl`-based stream's: one
`clusters list` call per project already returns every cluster's full
resource, node pools included (the SOP's own §1, point 2), so eight of the
ten checks below read data already in memory — the one per-pool
`node-pools describe` the SOP's own §3 command lines show is never actually
issued, because `clusters list` already carries every field those describes
would return. Only the version-currency checks (`master-behind`,
`stale-image-type`) need a second call, `get-server-config`, and that one is
cached per distinct `(project, location)` pair, not re-issued per cluster —
the SOP's own §2 instruction.

Two GCP surfaces, so two manifest failure shapes:

- A project's `clusters list` failing means none of its clusters are known
  at all. The project gets one `gate-failed` `project/<id>` entry saying so,
  because a project that leaves no trace in the manifest reads exactly like
  one holding no clusters, and the document is then free to publish a
  fleet-wide verdict over clusters nobody enumerated.
- A location's `get-server-config` failing means its clusters are still
  fully collected for every check that does not need a baseline; the
  manifest's `commands` for that cluster simply has no entry for
  `master-behind`/`stale-image-type`, and the SOP tells the agent to write
  a `limitations` note naming those two rather than treating the cluster as
  unreachable. `cross_check_manifest` never objects to a `checks_run` that
  is a subset of a `"collected"` cluster's `commands` — only to one that
  claims more than the manifest backs.

A check being absent from `commands` is therefore ambiguous on its own, and
`checks_not_applicable` is the half of it that is not a gap: on Autopilot the
four node-pool checks have no object to run against, which is a property of
the cluster rather than a read that failed. The collector declares those
itself — see `_AUTOPILOT_NOT_APPLICABLE`.
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, NamedTuple

MANIFEST_VERSION = 1

# A digest of this file, published in the manifest. `audit_report.py` compares
# it against the previous run's to tell a finding that stopped reproducing from
# a check that stopped looking; see `render_delta_comment`.
CHECKS_REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]

DEFAULT_TIMEOUT_S = 60
MAX_WORKERS = 8

# Node images no longer serviced: the pre-containerd runtimes and Windows's
# retired servicing channel. See §3.9's table for the replacement each maps to.
DEPRECATED_IMAGE_TYPES = {"COS": "COS_CONTAINERD", "UBUNTU": "UBUNTU_CONTAINERD", "WINDOWS_SAC": "WINDOWS_LTSC_CONTAINERD"}
BLOCKING_EXCLUSION_SCOPES = {"NO_UPGRADES", "NO_MINOR_OR_NODE_UPGRADES"}


def log(msg: str) -> None:
    print(f"[patch_readiness] {msg}", file=sys.stderr, flush=True)


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


def run_and_gate(argv: list[str], *, run: RunFn = default_run) -> tuple[object | None, Run]:
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


def get_target_projects(cli_project: str | None, *, run: RunFn) -> list[str]:
    """§1's project scope: the current project, plus — when `--project` was
    not given and the caller can list projects at all — every other project
    with at least one cluster. A `--project` override skips discovery
    entirely, for a scoped or a test run."""
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
        # project owes this audit nothing. Dropping the second here is what makes
        # the loss invisible: `collect_project` already writes a `gate-failed`
        # `project/<id>` entry when this exact command fails, and that entry is
        # the manifest's only record that a project existed and could not be
        # read -- but a probe that filters the project out of scope first means
        # it is never written, and the run publishes a fleet-wide verdict over a
        # fleet quietly one project short. Keep it and let the gate fire.
        if parsed is None or parsed:
            projects.append(candidate)
    return projects


# --------------------------------------------------------------------------- #
# Version arithmetic — §2's rule, verbatim.
# --------------------------------------------------------------------------- #

VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-gke\.(\d+))?$")


def parse_version(v: str) -> tuple[int, int, int, int] | None:
    m = VERSION_RE.match(v or "")
    if not m:
        return None
    major, minor, patch, build = m.groups()
    return (int(major), int(minor), int(patch), int(build or 0))


def minor_of(v: str) -> tuple[int, int] | None:
    parsed = parse_version(v)
    return parsed[:2] if parsed else None


def normalize_server_config(raw: dict) -> dict:
    channels = {c.get("channel"): c for c in raw.get("channels") or [] if c.get("channel")}
    return {
        "channels": channels,
        "validMasterVersions": raw.get("validMasterVersions") or [],
        "validImageTypes": {t.upper() for t in raw.get("validImageTypes") or []},
    }


# --------------------------------------------------------------------------- #
# Checks. Each takes the cluster's raw `gcloud container clusters list` item
# (`clusters describe`'s shape is identical) plus, where needed, the cached
# baseline for its location. `object` follows §2: `Cluster/<name>` for a
# cluster-scoped finding, `NodePool/<pool>` for a per-pool one.
# --------------------------------------------------------------------------- #


def _release_channel(cluster: dict) -> str:
    """The cluster's channel, with `UNSPECIFIED` reported as no channel.

    GKE spells "this cluster is on a static version" two ways: the
    `releaseChannel` object absent, and `releaseChannel.channel:
    "UNSPECIFIED"`. `check_no_channel` has always treated them alike; the
    version checks did not, and truthiness let `UNSPECIFIED` into the
    channel branch, where it missed in `baseline["channels"]` and returned
    `None`. That exempted exactly the clusters that most need the check --
    static-version ones, which take no automatic control-plane patches -- and
    it did so silently, with `master-behind` still recorded in the manifest as
    a check that ran and found nothing.
    """
    channel = (cluster.get("releaseChannel") or {}).get("channel") or ""
    return "" if channel == "UNSPECIFIED" else channel


def _upgrade_in_progress(cluster: dict) -> bool:
    """§3's universal suppression gate, cluster half: a `RECONCILING` cluster
    is mid-upgrade and its version drift is the upgrade, not a finding."""
    return (cluster.get("status") or "") == "RECONCILING"


def check_master_behind(cluster: dict, baseline: dict | None) -> dict | None:
    if baseline is None:
        return None
    if _upgrade_in_progress(cluster):
        return None
    current = cluster.get("currentMasterVersion") or ""
    current_t = parse_version(current)
    if current_t is None:
        return None
    channel = _release_channel(cluster)
    if channel:
        info = baseline["channels"].get(channel)
        if info is None:
            return None
        valid, default = info.get("validVersions") or [], info.get("defaultVersion")
    else:
        valid, default = baseline["validMasterVersions"], None

    # An empty roster is a baseline that did not carry the field, not a fleet
    # where no version is offered. `current not in valid` is true of every
    # cluster against `[]`, so the branch below would call the whole fleet
    # critical -- "absent from validVersions" on clusters running the version
    # the channel had just promoted. `get-server-config` returning a channel
    # with no `validVersions` is the SOP's own "inspect the raw output before
    # relying on a field" case (§2), and the honest answer to a baseline that
    # says nothing is to say nothing.
    if not valid:
        return None
    if current not in valid:
        return {"object": f"Cluster/{cluster['name']}", "excerpt": f"currentMasterVersion={current} absent from validVersions", "severity": "critical"}
    if not default:
        return None
    default_t = parse_version(default)
    if default_t is None:
        return None
    if current_t[:2] < default_t[:2]:
        return {"object": f"Cluster/{cluster['name']}", "excerpt": f"currentMasterVersion={current} is a minor behind channel default {default}", "severity": "major"}
    if current_t[:2] == default_t[:2] and current_t < default_t:
        return {"object": f"Cluster/{cluster['name']}", "excerpt": f"currentMasterVersion={current} is behind channel default {default} on the same minor", "severity": "minor"}
    return None


def _pool_status_excludes(pool: dict, cluster: dict) -> bool:
    return (pool.get("status") or "") in ("RECONCILING", "PROVISIONING") or _upgrade_in_progress(cluster)


def check_pool_skew(cluster: dict) -> list[dict]:
    if (cluster.get("autopilot") or {}).get("enabled"):
        return []
    master_t = parse_version(cluster.get("currentMasterVersion") or "")
    if master_t is None:
        return []
    hits = []
    for pool in cluster.get("nodePools") or []:
        if _pool_status_excludes(pool, cluster):
            continue
        pool_t = parse_version(pool.get("version") or "")
        if pool_t is None:
            continue
        name = pool.get("name", "")
        if pool_t > master_t:
            hits.append({"object": f"NodePool/{name}", "excerpt": f"pool version {pool.get('version')} ahead of control plane {cluster.get('currentMasterVersion')}", "severity": "major"})
            continue
        if pool_t[0] != master_t[0]:
            hits.append({"object": f"NodePool/{name}", "excerpt": f"pool on a different major version ({pool.get('version')} vs {cluster.get('currentMasterVersion')})", "severity": "critical"})
            continue
        minor_gap = master_t[1] - pool_t[1]
        if minor_gap >= 3:
            hits.append({"object": f"NodePool/{name}", "excerpt": f"pool {minor_gap} minors behind control plane", "severity": "critical"})
        elif minor_gap == 2:
            hits.append({"object": f"NodePool/{name}", "excerpt": "pool 2 minors behind control plane, at GKE's skew ceiling", "severity": "major"})
        elif minor_gap == 1:
            auto = ((pool.get("management") or {}).get("autoUpgrade"))
            hits.append({"object": f"NodePool/{name}", "excerpt": "pool 1 minor behind control plane", "severity": "major" if auto is False else "minor"})
        elif minor_gap == 0 and pool_t[2:] < master_t[2:]:
            # Same minor, older patch/build. Exactly one patch behind is GKE
            # upgrading the control plane first and draining pools after --
            # transient, and the SOP's own "Do NOT flag" case for it.
            if master_t[2] - pool_t[2] == 1:
                continue
            hits.append({"object": f"NodePool/{name}", "excerpt": f"pool on an older patch than the control plane ({pool.get('version')} vs {cluster.get('currentMasterVersion')})", "severity": "minor"})
    return hits


def check_fleet_spread(clusters: list[dict]) -> list[dict]:
    """§3.3, over the whole fleet. `clusters` is every cluster the run audited,
    not one project's — the spread is a property of the fleet, and computing it
    per project both misses a fleet whose two minors live in two projects and
    emits one finding per project on a fleet where they do not.

    §3's suppression gate applies here as it does to 3.1 and 3.2, and it has to
    remove the cluster from the computation rather than only from the finding:
    a cluster halfway through its upgrade is the one most likely to be the
    outlier that makes the fleet look two minors wide.
    """
    minors = {}
    for c in clusters:
        if _upgrade_in_progress(c):
            continue
        m = minor_of(c.get("currentMasterVersion") or "")
        if m is not None:
            minors.setdefault(m, []).append(c["name"])
    if len(minors) < 2:
        return []
    oldest, newest = min(minors), max(minors)
    if newest[0] != oldest[0] or newest[1] - oldest[1] < 2:
        return []
    laggard = sorted(minors[oldest])[0]
    return [
        {
            "object": f"Cluster/{laggard}",
            "excerpt": f"fleet spans {oldest[0]}.{oldest[1]}–{newest[0]}.{newest[1]}, {newest[1] - oldest[1]} minors wide",
        }
    ]


def check_no_channel(cluster: dict) -> dict | None:
    if _release_channel(cluster):
        return None
    return {"object": f"Cluster/{cluster['name']}", "excerpt": "releaseChannel.channel is empty"}


def _observed(mapping: dict, key: str, path: str) -> str:
    """`<path>=<value>` when the key is there, `<path> absent` when it is not.

    A disabled setting and a missing one are different observations with
    different fixes -- one is flipped, the other has to be created -- and an
    excerpt reading "false or absent" commits to neither. It also hides the
    move between them: a pool that grows an explicit `autoRepair: false`
    publishes an excerpt byte-identical to the one it had while the field was
    missing, so `carry_unchanged_findings` treats a real change as no change
    and the run-over-run diff shows nothing happened. `json.dumps` rather than
    `str` so the value reads as it does in the API response -- `false`, not
    `False`, and `null` for a key present but unset.
    """
    return f"{path}={json.dumps(mapping[key])}" if key in mapping else f"{path} absent"


def check_no_autoupgrade(cluster: dict) -> list[dict]:
    if (cluster.get("autopilot") or {}).get("enabled"):
        return []
    return [
        {
            "object": f"NodePool/{p.get('name', '')}",
            "excerpt": _observed(p.get("management") or {}, "autoUpgrade", "management.autoUpgrade"),
        }
        for p in cluster.get("nodePools") or []
        if not (p.get("management") or {}).get("autoUpgrade")
    ]


def check_no_autorepair(cluster: dict) -> list[dict]:
    if (cluster.get("autopilot") or {}).get("enabled"):
        return []
    return [
        {
            "object": f"NodePool/{p.get('name', '')}",
            "excerpt": _observed(p.get("management") or {}, "autoRepair", "management.autoRepair"),
        }
        for p in cluster.get("nodePools") or []
        if not (p.get("management") or {}).get("autoRepair")
    ]


def check_no_maintenance_window(cluster: dict) -> dict | None:
    window = ((cluster.get("maintenancePolicy") or {}).get("window") or {})
    # `in`, not truthiness: an API response can carry an empty-but-present
    # `recurringWindow: {}` while still populating it a moment later, and a
    # falsy-empty-dict-means-absent test would flag a cluster mid-populate.
    if "dailyMaintenanceWindow" in window or "recurringWindow" in window:
        return None
    return {"object": f"Cluster/{cluster['name']}", "excerpt": "no maintenancePolicy.window configured"}


def check_blocking_exclusion(cluster: dict, *, now: datetime, has_version_finding: bool) -> dict | None:
    # `maintenanceExclusions` is a map keyed by exclusion name
    # (`{name: {startTime, endTime, maintenanceExclusionOptions}}`), not a
    # list -- iterating it as a list would walk the names, not the windows.
    window = ((cluster.get("maintenancePolicy") or {}).get("window") or {})
    exclusions = window.get("maintenanceExclusions") or {}
    best = None
    for name, exclusion in exclusions.items():
        scope = ((exclusion.get("maintenanceExclusionOptions") or {}).get("scope")) or ""
        if scope not in BLOCKING_EXCLUSION_SCOPES:
            continue
        try:
            start = datetime.fromisoformat((exclusion.get("startTime") or "").replace("Z", "+00:00"))
            end = datetime.fromisoformat((exclusion.get("endTime") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if not (start <= now <= end):
            continue
        # `.days` truncates, so a 30-day-23-hour freeze read as 30 and fell
        # through: the SOP's threshold is "longer than 30 days", and comparing
        # the timedelta itself is the only reading of that which does not lose
        # the last day.
        long_freeze = (end - now) > timedelta(days=30)
        if not (long_freeze or has_version_finding):
            continue
        severity = "major" if has_version_finding else "minor"
        best = {"object": f"Cluster/{cluster['name']}", "excerpt": f"exclusion {name} (scope {scope}) until {exclusion.get('endTime')}", "severity": severity}
    return best


def check_stale_image_type(cluster: dict, baseline: dict | None) -> list[dict]:
    if baseline is None or (cluster.get("autopilot") or {}).get("enabled"):
        return []
    valid = baseline["validImageTypes"]
    hits = []
    for pool in cluster.get("nodePools") or []:
        image_type = ((pool.get("config") or {}).get("imageType") or "").upper()
        if not image_type:
            continue
        if image_type not in valid or image_type in DEPRECATED_IMAGE_TYPES:
            hits.append({"object": f"NodePool/{pool.get('name', '')}", "excerpt": f"config.imageType={image_type}"})
    return hits


def check_no_notifications(cluster: dict) -> dict | None:
    pubsub = ((cluster.get("notificationConfig") or {}).get("pubsub") or {})
    if not pubsub.get("enabled"):
        return {
            "object": f"Cluster/{cluster['name']}",
            "excerpt": _observed(pubsub, "enabled", "notificationConfig.pubsub.enabled"),
        }
    event_types = ((pubsub.get("filter") or {}) or {}).get("eventType") or []
    if event_types and "UPGRADE_AVAILABLE_EVENT" not in event_types:
        return {"object": f"Cluster/{cluster['name']}", "excerpt": f"pubsub filter excludes UPGRADE_AVAILABLE_EVENT: {event_types}"}
    return None


# Default severity per check, mode-independent. A hit's own "severity" key
# overrides this for the checks whose rule forks on which condition fired
# (master-behind, pool-skew, blocking-exclusion) -- everything else here is
# one severity always, so the default is the whole rule.
SEVERITY = {
    "master-behind": "critical",  # always overridden per hit
    "pool-skew": "critical",  # always overridden per hit
    "fleet-spread": "minor",
    "no-channel": "major",
    "no-autoupgrade": "major",
    "no-autorepair": "minor",
    "no-maintenance-window": "minor",
    "blocking-exclusion": "minor",  # overridden to major per hit when it holds back a version finding
    "stale-image-type": "major",
    "no-notifications": "minor",
}

IMPACT = {
    "master-behind": "Control plane is outside the supported window and receives no further patches until it moves.",
    "pool-skew": "This node pool's version skew against the control plane risks or already blocks the cluster's next control-plane upgrade.",
    "fleet-spread": "API-compatibility testing and rollout playbooks must cover every minor version this fleet spans.",
    "no-channel": "This cluster is on a static version with no release channel; it receives no automatic control-plane security patches.",
    "no-autoupgrade": "This node pool will drift out of the skew window on its own and eventually block the control plane.",
    "no-autorepair": "Unhealthy nodes stay in this pool until an operator notices.",
    "no-maintenance-window": "Automatic upgrades on this cluster can begin at any hour, including business hours.",
    "blocking-exclusion": "A maintenance exclusion is currently suppressing upgrades on this cluster.",
    "stale-image-type": "This node pool's image type is no longer offered at this location and cannot take node-image patches.",
    "no-notifications": "This cluster publishes no GKE upgrade notifications; upgrade-available signals reach no one between audits.",
}


def _emit(slug: str, hit: dict) -> dict:
    return {
        "check": slug,
        "namespace": "",
        "object": hit["object"],
        "severity": hit.get("severity") or SEVERITY[slug],
        "excerpt": hit["excerpt"],
        "impact": IMPACT[slug],
        "needs_triage": None,
    }


# The four checks whose object does not exist on Autopilot, with the reason.
# Google owns an Autopilot cluster's nodes and exposes no user-managed node
# pool, which is why `check_pool_skew`, `check_no_autoupgrade`,
# `check_no_autorepair` and `check_stale_image_type` each already return no
# hits there.
#
# Returning no hits was the whole of it, and that is the bug. This collector
# issues one `clusters list` per project -- never a `describe`, per the module
# docstring -- and records it against every slug, so an Autopilot cluster's
# manifest said `pool-skew` ran at rc=0 and found nothing
# -- indistinguishable from a Standard cluster whose pools are all in step.
# Whether the run then told the truth came down to whether the model happened
# to know GKE well enough to overrule its own manifest. On 2026-08-29 it did,
# and three Autopilot clusters came back `checks_run=6 n/a=4`; the same
# manifest read by a model that did not would report ten checks passed on a
# cluster where four of them were never evaluated.
#
# `fleet_waste.py` reached this conclusion first and its comment records the
# other direction of the same failure: a model that remembers three of four
# slugs publishes a coverage gap for a check the cluster does not owe. Neither
# direction is the model's to get right. `autopilot` is a fact the collector
# already holds, so the disposition belongs here, where it is the same on every
# run -- and cross_check_manifest's note on `checks_not_applicable` asks for
# exactly this, in every collector, as the prerequisite to adjudicating the
# field at all.
_AUTOPILOT_NOT_APPLICABLE = {
    "pool-skew": (
        "GKE Autopilot: Google manages the nodes and exposes no user node pool "
        "whose version could skew from the control plane."
    ),
    "no-autoupgrade": (
        "GKE Autopilot: node auto-upgrade is always on and not user-settable, "
        "so there is no node pool management setting to inspect."
    ),
    "no-autorepair": (
        "GKE Autopilot: node auto-repair is always on and not user-settable, "
        "so there is no node pool management setting to inspect."
    ),
    "stale-image-type": (
        "GKE Autopilot: Google selects the node image and exposes no user node "
        "pool carrying a config.imageType."
    ),
}


def collect_one_cluster(cluster: dict, baseline: dict | None, *, now: datetime) -> tuple[list[str], list[dict], list[dict]]:
    """The check slugs this cluster has data for, its candidates, and the
    checks its shape rules out. A slug in none of the three (only ever
    `master-behind`, when the location's baseline could not be fetched) is a
    coverage gap the SOP tells the agent to name in `limitations`, not a gate
    failure."""
    slugs = ["pool-skew", "no-channel", "no-autoupgrade", "no-autorepair", "no-maintenance-window", "blocking-exclusion", "no-notifications"]
    candidates = []

    master_behind_hit = check_master_behind(cluster, baseline) if baseline is not None else None
    pool_skew_hits = check_pool_skew(cluster)
    # §3.8's escalation is specifically "a critical/major version finding" --
    # a minor one (3.1c's same-minor patch lag, 3.2's patch-only drift)
    # does not, on its own, justify calling out a long freeze as major.
    critical_major = {"critical", "major"}
    has_version_finding = (master_behind_hit or {}).get("severity") in critical_major or any(
        h.get("severity") in critical_major for h in pool_skew_hits
    )

    for hit in [check_no_channel(cluster)]:
        if hit:
            candidates.append(_emit("no-channel", hit))
    for hit in [check_no_maintenance_window(cluster)]:
        if hit:
            candidates.append(_emit("no-maintenance-window", hit))
    for hit in [check_blocking_exclusion(cluster, now=now, has_version_finding=has_version_finding)]:
        if hit:
            candidates.append(_emit("blocking-exclusion", hit))
    for hit in [check_no_notifications(cluster)]:
        if hit:
            candidates.append(_emit("no-notifications", hit))
    candidates += [_emit("pool-skew", hit) for hit in pool_skew_hits]
    candidates += [_emit("no-autoupgrade", hit) for hit in check_no_autoupgrade(cluster)]
    candidates += [_emit("no-autorepair", hit) for hit in check_no_autorepair(cluster)]

    if baseline is not None:
        slugs += ["master-behind", "stale-image-type"]
        if master_behind_hit is not None:
            candidates.append(_emit("master-behind", master_behind_hit))
        candidates += [_emit("stale-image-type", hit) for hit in check_stale_image_type(cluster, baseline)]

    # Declared whether or not the baseline arrived: a check that cannot apply
    # to this cluster is not waiting on a read. `master-behind` is the other
    # half of that -- an Autopilot control plane has a real version, so a
    # missing baseline leaves it a genuine gap rather than an inapplicable
    # check, and it stays out of this table.
    if (cluster.get("autopilot") or {}).get("enabled"):
        not_applicable = [{"check": slug, "reason": reason} for slug, reason in _AUTOPILOT_NOT_APPLICABLE.items()]
        slugs = [slug for slug in slugs if slug not in _AUTOPILOT_NOT_APPLICABLE]
    else:
        not_applicable = []

    return slugs, candidates, not_applicable


def crashed_entries(project: str, exc: BaseException) -> list[dict]:
    """The `clusters[]` entries for a worker that raised something unmodelled.

    `future.result()` re-raises, so one unhandled exception on one project
    aborts `collect_fleet` — and the SOP invokes this collector as
    `patch_readiness.py … > manifest_security-patch-orchestrator.json`, so by
    then the shell has already truncated the file. The run loses every project
    to one bad object instead of one. The shape is the one the gate-failed
    branch above already uses, for the same reason it gives: a project missing
    from the manifest reads as a project holding no clusters.
    """
    log(f"{project}: collector raised {type(exc).__name__}: {exc}")
    return [
        {
            "name": f"project/{project}",
            "project": project,
            "location": "global",
            "outcome": "gate-failed",
            "error": f"collector raised {type(exc).__name__}: {exc}"[:300],
        }
    ]


# §1.5's skip list, verbatim. A cluster in one of these states is not a
# cluster this audit has an opinion about: mid-flight or broken means its
# version data is meaningless, and an alpha cluster cannot be upgraded and
# expires on its own. The SOP puts both in `scope.skipped`.
_UNAUDITABLE_STATUSES = {
    "PROVISIONING": "cluster status is PROVISIONING; version data is not yet meaningful",
    "STOPPING": "cluster status is STOPPING; the object is mid-delete",
    "ERROR": "cluster status is ERROR; the object is broken and its reported version is not trustworthy",
}


def out_of_scope_reason(cluster: dict) -> str | None:
    """Why §1.5 puts this cluster in `scope.skipped`, or `None` to audit it.

    The collector has to answer this, not the model. Marking such a cluster
    `collected` makes `cross_check_manifest` demand it in `scope.clusters`
    while §1.5 orders it into `scope.skipped` — and the two lists may not
    overlap, so on a fleet holding one PROVISIONING or alpha cluster every
    document was rejected whichever list the model chose. `RECONCILING` is
    deliberately not here: §3's gate suppresses that cluster's *version*
    findings and its policy checks still run, which is a different disposition
    from not auditing it at all.
    """
    status = (cluster.get("status") or "").upper()
    if status in _UNAUDITABLE_STATUSES:
        return _UNAUDITABLE_STATUSES[status]
    if cluster.get("enableKubernetesAlpha"):
        return "enableKubernetesAlpha=true; alpha clusters cannot be upgraded and auto-expire by design"
    return None


def attach_fleet_spread(entries: list[dict]) -> None:
    """Run §3.3 once over the whole fleet and attach its finding, in place.

    Computing it inside `collect_project` made it per project, which §3.3 is
    not: "across all audited clusters … emit exactly **one** finding". A fleet
    running 1.28 in one project and 1.31 in another reported nothing, because
    neither project spans two minors on its own; a fleet spread across three
    projects reported it three times, each naming a different laggard, so the
    run-over-run delta churned as clusters moved between them.
    """
    readable = [e for e in entries if e.get("outcome") == "collected"]
    hits = check_fleet_spread(
        [
            {"name": e["name"], "currentMasterVersion": e.get("_master_version") or "", "status": e.get("_status") or ""}
            for e in readable
        ]
    )
    by_name: dict[str, dict] = {}
    for entry in readable:
        by_name.setdefault(entry["name"], entry)
    for hit in hits:
        target = by_name.get(hit["object"].split("/", 1)[1])
        if target is not None:
            target.setdefault("candidates", []).append(_emit("fleet-spread", hit))
    for entry in entries:
        entry.pop("_master_version", None)
        entry.pop("_status", None)


def collect_project(project: str, *, run: RunFn, now: datetime) -> list[dict]:
    argv = ["gcloud", "container", "clusters", "list", "--project", project, "--format", "json"]
    parsed, result = run_and_gate(argv, run=run)
    if parsed is None:
        log(f"{project}: clusters list gate failed (rc={result.rc}); no clusters known this run")
        # One `project/<p>` entry rather than nothing at all. The manifest is
        # the only record of what the collector managed to read, and a project
        # that drops out of it entirely is indistinguishable from one that
        # holds no clusters -- so cross_check_manifest has nothing to hold the
        # document to, and the run publishes a fleet-wide verdict over clusters
        # nobody enumerated. Recorded as `gate-failed`, the loss is a target the
        # document has to account for, and §6 turns that into a coverage gap.
        return [
            {
                "name": f"project/{project}",
                "project": project,
                "location": "global",
                "outcome": "gate-failed",
                "error": f"clusters list rc={result.rc}: {result.stderr.strip()[:300] or 'no stderr'}",
            }
        ]
    clusters_record = _record(shlex.join(argv), result)

    by_location: dict[str, list[dict]] = {}
    for c in parsed:
        by_location.setdefault(c.get("location") or c.get("zone") or "", []).append(c)

    baselines: dict[str, tuple[dict, dict] | None] = {}
    for location in by_location:
        sc_argv = ["gcloud", "container", "get-server-config", "--location", location, "--project", project, "--format", "json"]
        sc_parsed, sc_result = run_and_gate(sc_argv, run=run)
        if sc_parsed is None:
            log(f"{project}/{location}: get-server-config gate failed (rc={sc_result.rc}); master-behind/stale-image-type unavailable there")
            baselines[location] = None
        else:
            baselines[location] = (normalize_server_config(sc_parsed), _record(shlex.join(sc_argv), sc_result))

    entries = []
    for c in parsed:
        location = c.get("location") or c.get("zone") or ""
        skip_reason = out_of_scope_reason(c)
        if skip_reason:
            log(f"{project}/{c.get('name', '?')}: out of scope — {skip_reason}")
            entries.append(
                {
                    "name": c.get("name", "?"),
                    "project": project,
                    "location": location,
                    "autopilot": bool((c.get("autopilot") or {}).get("enabled")),
                    "outcome": "out-of-scope",
                    # The key `cross_check_manifest` renders when it has to
                    # explain why a target could not just be documented, and
                    # the same one the two failure outcomes use. A reader of
                    # the manifest needs the reason wherever it came from.
                    "error": skip_reason,
                }
            )
            continue
        baseline_pair = baselines.get(location)
        baseline = baseline_pair[0] if baseline_pair else None
        slugs, candidates, not_applicable = collect_one_cluster(c, baseline, now=now)
        commands = {slug: clusters_record for slug in slugs}
        if baseline_pair is not None:
            # `master-behind` unconditionally -- collect_one_cluster adds it
            # whenever the baseline arrived, and no cluster shape rules it out.
            # `stale-image-type` only if it survived the not-applicable filter:
            # writing it here regardless would put the same slug in `commands`
            # and in `checks_not_applicable` at once, which is the incoherence
            # this declaration exists to remove.
            commands["master-behind"] = baseline_pair[1]
            if "stale-image-type" in slugs:
                commands["stale-image-type"] = baseline_pair[1]
        # Recorded for every cluster, not only the laggard §3.3 attaches the
        # finding to. Every cluster in `parsed` handed its minor to the spread
        # computation, so the check ran against all of them and the same
        # `clusters list` output is its evidence -- but §6 reads `commands` as
        # the roster of checks that ran, so recording it only on a hit makes a
        # tight fleet indistinguishable from one nobody measured, and every
        # clean run reports a coverage gap it does not have.
        commands["fleet-spread"] = clusters_record
        entry = {
            "name": c["name"],
            "project": project,
            "location": location,
            "autopilot": bool((c.get("autopilot") or {}).get("enabled")),
            "outcome": "collected",
            "commands": [{"check": slug, **record} for slug, record in commands.items()],
            "candidates": candidates,
            # Consumed and removed by `attach_fleet_spread`, which needs every
            # audited cluster's minor and cannot get it from one project's
            # worker. Underscored so a leak shows up as an unknown manifest key
            # rather than as plausible data.
            "_master_version": c.get("currentMasterVersion") or "",
            "_status": c.get("status") or "",
        }
        if not_applicable:
            entry["checks_not_applicable"] = not_applicable
        entries.append(entry)
    return entries


def collect_fleet(project: str | None = None, *, run: RunFn = default_run, max_workers: int = MAX_WORKERS, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    projects = get_target_projects(project, run=run)

    results: list[list[dict]] = [[] for _ in projects]
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(collect_project, p, run=run, now=now): i for i, p in enumerate(projects)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:  # noqa: BLE001 — see crashed_entries
                results[index] = crashed_entries(projects[index], exc)

    entries = [entry for group in results for entry in group]
    attach_fleet_spread(entries)

    return {
        "version": MANIFEST_VERSION,
        "checks_revision": CHECKS_REVISION,
        "audit": "security-patch-orchestrator",
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
