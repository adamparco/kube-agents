#!/opt/hermes/.venv/bin/python3
"""collect.py — Procedural collector for the fleet-audit checks that are code
wearing prose.

See docs/designs/fleet-audit-collectors-and-status.md §4.2, §4.3, §6.

**Scope of this file today: the collector engine, plus three streams' check
tables in full.** `obtainability-audit`'s complete fifteen-check roster
(§3.1–§3.15 of `governance/obtainability_audit_sop.md`), `compliance-audit`'s
complete twelve-check roster (§2.1–§2.12 of `governance/compliance_audit_sop.md`),
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
import datetime
import hashlib
import ipaddress
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

SCRATCH_DIR = os.environ.get("FLEET_AUDIT_SCRATCH_DIR") or "/opt/data/scratch"
KUBECONFIG_DIR = Path(os.environ.get("HERMES_HOME") or "/opt/data") / ".kubeconfigs"
DEFAULT_TIMEOUT_S = 60
MAX_WORKERS = 8

# What `default_run` reports when *it* gave up, following the shell convention
# for a command killed by `timeout(1)`. It is this collector's own marker and
# never something a `kubectl` or `gcloud` actually returned, so any branch that
# reads an exit code as a statement about the cluster has to exclude it first.
# On 2026-09-06 `kcc-object-wedged` did not: it took `rc != 0` on `kubectl get
# gcp -A` to mean the API server does not serve the category, and published
# "Config Connector is not installed on this cluster" about the one cluster in
# the fleet that runs Config Connector, on the strength of its own timeout.
TIMEOUT_RC = 124

# `workload_declarations` walks a GitOps clone. A file is applied to a known
# cluster only when it sits at `clusters/<name>/...`, which is three path
# components at minimum -- the root, the cluster, and the file itself.
GITOPS_CLUSTER_TREE_ROOT = "clusters"
GITOPS_CLUSTER_TREE_DEPTH = 2
GIT_DIR_NAME = ".git"
# Spelled the same way `audit_report.KCC_API_GROUP_SUFFIX` spells it, and
# copied rather than imported for the reason `SYSTEM_NAMESPACES` is below.
KCC_API_GROUP_SUFFIX = "cnrm.cloud.google.com"
# `check_kcc_object_wedged`. `Ready` is the one condition every Config
# Connector kind carries; `audit_report.KCC_READY_CONDITION` spells it the
# same way, copied for the reason above.
KCC_READY_CONDITION = "Ready"
# The `Ready=False` reason KCC sets on an object that is fine in itself and is
# only waiting on another object. It is what separates a cause from a symptom,
# and the whole reason this check does not emit one finding per unready object:
# on 2026-09-05 a single `PubSubTopic` failing on a missing IAM role put
# fifteen `ContainerCluster`s into this state behind it.
#
# Only this one. `DependencyNotFound` is a different reason with a different
# meaning -- the referent does not exist at all, so no other object's repair
# will ever clear it -- and it is deliberately not listed here, which leaves it
# a cause. A live probe on 2026-09-06 confirmed KCC writes the two distinctly:
# a `ComputeFirewall` pointed at an absent `networkRef` got
# `DependencyNotFound`, and the same firewall pointed at a network that existed
# but had failed to create got `DependencyNotReady`.
KCC_DEPENDENCY_NOT_READY_REASON = "DependencyNotReady"
# How much of a controller message survives into the excerpt. KCC pastes the
# whole upstream API error in, which for a 403 runs to several hundred
# characters of URL and quota-project boilerplate after the part that matters.
MAX_KCC_WEDGE_MESSAGE_CHARS = 240
# How many blocked objects an excerpt names before it stops and counts. Naming
# all fifteen is not more informative than naming three and saying fifteen.
MAX_KCC_BLOCKED_NAMED = 3

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
        return Run(argv, TIMEOUT_RC, exc.stdout or "", exc.stderr or "", time.monotonic() - t0)


# See `fleet_waste.AUDITABLE_STATUSES` for the reasoning, which applies
# unchanged here: GKE sets `RECONCILING` while work proceeds on a cluster whose
# API server stays up, it is transient and ordinary, and excluding it dropped
# clusters from audits for the duration of any routine config change. Each
# collector is a standalone script with no shared import, so the constant is
# duplicated rather than imported.
AUDITABLE_STATUSES = frozenset({"RUNNING", "RECONCILING"})


def not_running_entry(c: dict, project: str) -> dict:
    """A manifest target for a cluster whose state rules out auditing it.

    Every collector here filters `clusters list` down to `AUDITABLE_STATUSES`,
    which is right — a PROVISIONING cluster has no API server to read and a
    STOPPING one is on its way out. Dropping the rest on the floor is what is wrong. The
    manifest is the run's only account of the fleet it saw, so a cluster that
    never reaches it is indistinguishable from a cluster that does not exist,
    and the document is free to publish a fleet-wide all-clear over a fleet
    quietly missing it. DEGRADED is the case that makes this bite: the state
    likeliest to be worth a finding is the one the filter throws away silently.

    Recorded as a non-`collected` target, it becomes something the document has
    to place in `scope.skipped` with a reason, and `coverage_gaps` renders it as
    "not audited". `fleet_drift.py` reaches the same conclusion through
    `cluster_eligibility`; it enumerates every cluster and carries the reason as
    a limitation instead, because its checks compare clusters against each other
    rather than reading inside them.
    """
    return {
        "name": c.get("name", ""),
        "project": project,
        "location": c.get("location") or c.get("zone") or "",
        "autopilot": bool((c.get("autopilot") or {}).get("enabled")),
        "outcome": "unreachable",
        "error": f"cluster status is {c.get('status') or 'unknown'}, which is neither RUNNING nor RECONCILING; no check was evaluated against it",
    }


def enumerate_clusters(project: str, *, run: RunFn = default_run) -> tuple[list[dict], list[dict]]:
    """Every auditable cluster in `project`, as `{name, location, project,
    autopilot}`, plus a manifest target for each cluster that is not.
    Raises on a `gcloud` failure — an audit that could not enumerate the fleet
    has nothing to report against, the same rule `handle_start`'s callers
    already follow for a bare `gcloud` failure.
    """
    result = run(["gcloud", "container", "clusters", "list", "--project", project, "--format", "json"])
    if result.rc != 0:
        raise RuntimeError(f"cluster enumeration failed (rc={result.rc}): {result.stderr.strip()[:500]}")
    try:
        clusters = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"cluster enumeration returned non-JSON: {exc}") from exc
    running = [
        {
            "name": c["name"],
            "location": c.get("location") or c.get("zone"),
            "project": project,
            "autopilot": bool((c.get("autopilot") or {}).get("enabled")),
        }
        for c in clusters
        if c.get("status") in AUDITABLE_STATUSES
    ]
    return running, [not_running_entry(c, project) for c in clusters if c.get("status") not in AUDITABLE_STATUSES]


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


DUMP_COMMAND_KINDS = (
    "deployments,statefulsets,daemonsets,poddisruptionbudgets,horizontalpodautoscalers,services,limitranges"
    # `cronjobs,jobs` serve 3.12 alone. They are safe to add to the shared dump
    # because `normalize_workloads` filters on `WORKLOAD_KINDS`, so neither kind
    # reaches the workload set the other ten checks read -- and this dump has
    # exactly one caller, `_collect_obtainability`, so no other stream pays for
    # them either.
    ",cronjobs,jobs"
)

# How long a CronJob must have been firing without a success before 3.12 calls
# it chronically failing, measured as `lastScheduleTime - lastSuccessfulTime`.
#
# Both ends come out of the dump, so this check needs no clock -- which is why
# it is a gap between two recorded times rather than an age against `now`.
# Nothing else in this file reads wall-clock time, the tests would have to
# freeze it, and a collector whose verdict depends on when it ran is a
# collector whose findings cannot be reproduced from its own manifest.
#
# Deliberately not scaled to the schedule's period. The instinct is that a
# weekly CronJob should get a week of grace and an hourly one an hour, but that
# has it backwards: the rarer the schedule, the *more* a single failure costs,
# because the next attempt is a week away rather than sixty seconds. A flat
# floor gives an hourly job twenty-four consecutive failures before it is worth
# saying anything, and reports a weekly job the day after its one failure --
# which is the right answer in both directions. It also avoids parsing cron
# expressions, which this collector has no reason to learn.
STALE_SUCCESS_GAP_HOURS = 24
SECONDS_PER_HOUR = 3600
# Minutes, not hours, for the overlap check's excerpt: the schedules it can
# fire on are the ones whose period a run can outlast, and printing "0.1h"
# where a reader wants "6m" hides the comparison the finding is making.
SECONDS_PER_MINUTE = 60


def run_and_gate(
    argv: list[str],
    kubeconfig: Path,
    *,
    run: RunFn = default_run,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> tuple[dict | None, Run]:
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
    truncated or empty output, which the credential proxy's output cap makes
    a real possibility, not a theoretical one. That cap is 16 MiB per stream
    as the operator deploys it, 4 MiB if `CREDENTIAL_PROXY_MAX_OUTPUT_BYTES`
    is unset; either way a 16-cluster `-A -o json` dump can reach it.
    """
    env = {**os.environ, "KUBECONFIG": str(kubeconfig)}
    result = run(argv, env=env, timeout=timeout)
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
    kubeconfig: Path, cluster: str, *, project: str = "", location: str = "", run: RunFn = default_run
) -> tuple[Path, Run, bool]:
    """`obtainability-audit`'s one dump, behind `run_and_gate`. Kept as its
    own function (rather than inlined into its context builder) because its
    fixed dump-to-a-named-file shape predates the multi-collection builder
    contract and nothing else needs a file on disk — every check reads the
    parsed dict `run_and_gate` already returns.

    Keyed the way `kubeconfig_path` is, on the whole `(project, cluster,
    location)` triple. The design's thread-safety rule is that a worker writes
    only to paths keyed by its own cluster, and it named this file as the
    example of a name no two threads can collide on — but a cluster name is
    unique within a project, not across the fleet, and this collector runs
    eight projects at once. Two clusters called `prod` in two projects wrote
    the same path, and the loser re-read the winner's dump: not a truncated
    file or a crash, but one cluster's workloads published under the other's
    name, with a manifest recording a clean rc=0 read.
    """
    dump_path = Path(SCRATCH_DIR) / f"wra_state_{project}_{cluster}_{location}.json"
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

# The owners S3 is entitled to defer to. Kind and API group both have to
# match: `Job` in `batch` is the built-in whose children S5 drops, while a
# `Job` some operator defines in its own group is a CRD wearing the name.
BUILTIN_OWNER_GROUPS = frozenset({"", "apps", "batch"})
BUILTIN_OWNER_KINDS = frozenset(WORKLOAD_KINDS) | {
    "ReplicaSet",
    "ReplicationController",
    "Job",
    "CronJob",
}


def _defers_to_owner(meta: dict) -> bool:
    """True when S3's promise — audit the owning controller instead — is one
    this audit can keep.

    S3 skips an owned workload on the grounds that its replica count, PDB, and
    probes belong to its controller rather than to a human. That holds for a
    built-in controller: the audit reads Deployments, StatefulSets, and
    DaemonSets directly, a ReplicaSet's own owner is one of those, and S5 puts
    Jobs and CronJobs out of scope outright. It does not hold for a CRD the
    audit never dumps. There the deferral has nowhere to defer to — the
    workload is dropped and the owner is never looked at either — so a real gap
    goes unreported forever instead of being reported against a better object.

    Across the sixteen clusters of this fleet, S3 as an unconditional
    `ownerReferences` test suppressed exactly one workload:
    `kubeagents-system/platform-agent-gateway`, owned by a `PlatformAgent`.
    That is the harness's own gateway, in the one namespace S1 deliberately
    keeps in scope so that the harness audits itself. The rule was costing
    nothing but its single counterexample, and the counterexample was the
    object the surrounding prose most wanted covered.
    """
    for ref in meta.get("ownerReferences") or []:
        api_version = str(ref.get("apiVersion", ""))
        # `apps/v1` → `apps`; `v1` and an absent apiVersion → the core group.
        group = api_version.rsplit("/", 1)[0] if "/" in api_version else ""
        if str(ref.get("kind", "")) in BUILTIN_OWNER_KINDS and group in BUILTIN_OWNER_GROUPS:
            return True
    return False


# The markers a reconciling controller stamps on an object it owns, and the
# phrase each one licenses. All three are set by the writer at apply time and
# read here off the same dump every check already runs on, so this costs no
# extra call.
#
# Helm 3 writes both halves of its pair on every object in a release, and the
# release namespace is not the object's -- cert-manager's Deployments live in
# `cert-manager` under a release of the same name, while the kube-agents chart
# installs into `kubeagents-system`. Argo CD's tracking id is
# `<application>:<group>/<Kind>:<namespace>/<name>`, so the Application name is
# the leading segment. `app.kubernetes.io/managed-by` is the fallback, and only
# the fallback: Helm sets it to the literal `Helm`, which names no release and
# is strictly worse than the annotation pair beside it.
_HELM_RELEASE_ANNOTATION = "meta.helm.sh/release-name"
_HELM_NAMESPACE_ANNOTATION = "meta.helm.sh/release-namespace"
_ARGOCD_TRACKING_ANNOTATION = "argocd.argoproj.io/tracking-id"
_MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
_HELM_MANAGED_BY = "helm"


def reconciler_of(meta: dict) -> str | None:
    """What continuously reasserts this object's spec, named, or None.

    The fact a `manual` remediation needs and never carried. A manual
    remediation is prose telling a reader to make a change by hand, and on an
    object some controller reconciles, the hand-made change is undone -- by the
    next `helm upgrade`, by Argo CD's next sync, by an operator within seconds.
    The ledger said none of that. On 2026-09-06, 23 of the fleet's 67
    workload-scoped findings were `manual` on a reconciled object: twelve under
    Argo CD, ten under Helm, and one -- `probes-liveness` on
    `Deployment/platform-agent-gateway` -- under the harness's own operator.
    Compliance's `podsecurity-gaps` finding on `Deployment/litellm` was the
    clearest of them, telling the reader the workload was "applied out-of-band"
    and to patch the Deployment directly, when it is a Helm release the
    kube-agents chart declares.

    Same defect 749f8304 fixed one layer down, where a `gcloud` remediation
    proposed a change Config Connector would revert. This is its Kubernetes
    half.

    Returns a noun phrase, not a slug, because the only consumer is a sentence.
    None means no marker was found -- an object applied with `kubectl apply -f`
    and reconciled by nobody, where a hand-applied change is exactly as durable
    as the finding implies.
    """
    annotations = meta.get("annotations") or {}
    release = str(annotations.get(_HELM_RELEASE_ANNOTATION) or "").strip()
    if release:
        namespace = str(annotations.get(_HELM_NAMESPACE_ANNOTATION) or "").strip()
        where = f" in {namespace}" if namespace else ""
        return f"the Helm release `{release}`{where}"
    tracking = str(annotations.get(_ARGOCD_TRACKING_ANNOTATION) or "").strip()
    if tracking:
        return f"the Argo CD Application `{tracking.split(':', 1)[0]}`"
    managed_by = str((meta.get("labels") or {}).get(_MANAGED_BY_LABEL) or "").strip()
    if managed_by and managed_by.lower() != _HELM_MANAGED_BY:
        return f"`{managed_by}`"
    return None


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
        if _defers_to_owner(meta):  # S3
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
                "reconciler": reconciler_of(meta),
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
    """Excludes any owned HPA (`ownerReferences` present), KEDA's included.

    This is not `_defers_to_owner`, and deliberately so. A workload owned by a
    CRD still has its own PDB and probe gaps to answer for, which is why S3 no
    longer drops it. An HPA owned by a `ScaledObject` is different in kind: the
    `min`/`max` this audit would read are a copy the operator writes from the
    CRD, so 3.6 would be grading a projection of a configuration it cannot see.
    The SOP calls that exclusion out under 3.6 for exactly that reason.
    """
    return {
        ns: [hpa for hpa in hpas if not (hpa.get("metadata") or {}).get("ownerReferences")]
        for ns, hpas in _by_namespace(dump, "HorizontalPodAutoscaler").items()
    }


def workload_keys(dump: dict) -> set[tuple[str, str, str]]:
    """Every workload in the dump as `(ns, kind, name)`, suppressed or not.

    "Is this object this audit's business" and "does this object exist" are
    different questions, and `workloads` only answers the first. A check that
    reports a reference as broken is answering the second, so it has to read
    the dump rather than the audited set — otherwise opting a Deployment out of
    the audit is enough to make something else report it as missing.
    """
    return {
        ((item.get("metadata") or {}).get("namespace", ""), item["kind"], (item.get("metadata") or {}).get("name", ""))
        for item in dump.get("items", []) or []
        if item.get("kind") in WORKLOAD_KINDS
    }


def cronjobs_with_jobs(dump: dict) -> list[dict]:
    """Every non-system CronJob paired with the Jobs it still retains.

    Joined here rather than in the check because the join is the awkward part:
    a Job names its CronJob in `ownerReferences`, so the dump has to be walked
    once per CronJob to find them, and doing that inside the check would make
    it quadratic on a fleet where one namespace holds hundreds of Jobs.

    S1, S2 and S4 are applied here. S3 is not, because it would drop every
    CronJob in the fleet -- a CronJob is owned by nothing. Neither is S5, which
    exists to keep Jobs and CronJobs out of the *workload* checks and is
    exactly the exclusion 3.12 is written to reach past; the SOP says so under
    S5 rather than leaving a reader to infer it from this function.

    S4 matters more here than anywhere else in this file. Every other check in
    this audit reports a shape the operator can change, so an unwanted finding
    can be answered by fixing it. This one reports that a schedule is failing,
    and an operator who knows why and is not going to act -- a CronJob kept
    deliberately broken, one whose fix is waiting on something else -- has no
    way to make it stop except the opt-out label.
    """
    out = []
    for item in dump.get("items", []) or []:
        if item.get("kind") != "CronJob":
            continue
        meta = item.get("metadata") or {}
        ns = meta.get("namespace", "")
        if _is_system_namespace(ns):  # S1
            continue
        labels = meta.get("labels") or {}
        annotations = meta.get("annotations") or {}
        if "addonmanager.kubernetes.io/mode" in labels:  # S2
            continue
        if labels.get(OPT_OUT_KEY) == "exempt" or annotations.get(OPT_OUT_KEY) == "exempt":  # S4
            continue
        name = meta.get("name", "")
        owned = [
            job
            for job in dump.get("items", []) or []
            if job.get("kind") == "Job"
            and (job.get("metadata") or {}).get("namespace", "") == ns
            and any(
                ref.get("kind") == "CronJob" and ref.get("name") == name
                for ref in ((job.get("metadata") or {}).get("ownerReferences") or [])
            )
        ]
        owned.sort(key=lambda job: (job.get("metadata") or {}).get("creationTimestamp", ""))
        out.append({"cronjob": item, "ns": ns, "name": name, "jobs": owned})
    return out


def build_context(dump: dict, workloads: list[dict]) -> dict:
    return {
        "limitranges": limitranges_by_namespace(dump),
        "pdbs": pdbs_by_namespace(dump),
        "hpas": hpas_by_namespace(dump),
        "services": services_by_namespace(dump),
        "cronjobs": cronjobs_with_jobs(dump),
        "workloads": workloads,
        "workload_keys": workload_keys(dump),
    }


def _selecting_services(workload: dict, context: dict) -> list[dict]:
    matched = []
    for svc in context["services"].get(workload["ns"], []):
        spec = svc.get("spec") or {}
        selector = spec.get("selector")
        if spec.get("type") == "ExternalName" or not selector:
            continue
        if selector_matches({"matchLabels": selector}, workload["pod_labels"]):
            matched.append(svc)
    return matched


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


_REQUEST_RESOURCES = ("cpu", "memory")

# §3.1's Impact, for the two arms where the sentence in `OBTAINABILITY_CHECKS`
# is false. That sentence ends "its pods are the first evicted under node
# pressure", which is a claim about QoS class, and only a BestEffort pod is
# first. §3.1 flags a container missing `cpu` *or* `memory`, so what it catches
# need not be BestEffort at all, and an owner told their pod goes first goes
# looking for a pressure event that reached it before it reached anything else.
#
# Note that the ubiquitous cpu-request-only workloads -- `kube-proxy`,
# `antrea-controller` -- are *not* what these arms are for, however often they
# get cited as the motivating case. They live in `kube-system`, S1 drops the
# namespace before `check_no_requests` ever runs, and no finding for either can
# exist. The arms serve user-namespace workloads in the same shape. All 7
# findings on this fleet are BestEffort, so neither arm has a live instance
# here; both were graded against the API server's own `status.qosClass`
# instead, over all 79 workloads on the host cluster.
#
# The sentence is two independent halves: what the missing requests cost, and
# what the pod's QoS class means. They vary separately -- a pod whose every
# missing request is limit-backed can still be Burstable because a *different*
# container declared a request and no ceiling -- so composing them is the only
# way each stays true. Deriving the class from the first half is what made the
# original single sentence wrong.
_QOS_BEST_EFFORT = "BestEffort"
_QOS_BURSTABLE = "Burstable"
_QOS_GUARANTEED = "Guaranteed"

# Scoped to a container, not to the pod. `unbacked_missing` is a union across
# containers, so a pod-level "sized without cpu or memory" is false as soon as
# a sibling declares one of them: two containers each limiting a different
# resource put both into the union while the pod is sized with both.
_IMPACT_UNRESERVED = (
    "{resources} goes unreserved on at least one container here, so the "
    "scheduler and cluster autoscaler size this cluster below the pod's real "
    "demand and that share of the cost cannot be attributed."
)
_IMPACT_CEILING_RESERVED = (
    "Every missing request is backed by a limit on the same container, so "
    "Kubernetes copies that limit into the request: the scheduler reserves this "
    "workload at its ceiling rather than at its steady-state size. The cost is "
    "bin-packing headroom held against a peak that may never arrive, and a "
    "reservation nobody wrote down and can review."
)
# Neither of these says "evicted first" or "evicted last by class", because the
# kubelet does not rank by class -- it sorts on whether usage exceeds requests,
# then Pod Priority, then usage relative to requests. Replacing one false
# eviction claim with another is the mistake this Impact already made once.
# §3.1's third arm. Named rather than left to `CheckSpec.impact` so the hit can
# set it like the other two: an arm that falls through to the table default is
# an arm `adopt_arm_impact` never sees, and the model is then free to publish a
# Burstable sentence over a BestEffort pod -- the exact substitution the two
# constants above exist to prevent. `OBTAINABILITY_CHECKS` points its table
# entry at this same string, so the fallback and the arm cannot drift apart.
_IMPACT_BEST_EFFORT = (
    "The scheduler and cluster autoscaler size this cluster as if this "
    "workload costs nothing; its pods are the first evicted under node "
    "pressure and its cost cannot be attributed."
)
_IMPACT_BY_QOS = {
    _QOS_GUARANTEED: (
        " Every container carries both limits with a request that matches, so "
        "the pod is Guaranteed: its usage cannot exceed its requests, which is "
        "the kubelet's first sort key under node pressure. It is in the last "
        "group evicted, not the first."
    ),
    _QOS_BURSTABLE: (
        " The pod is Burstable, not BestEffort. Eviction does not follow the "
        "class, though: the kubelet sorts on whether usage exceeds requests and "
        "then on Pod Priority, so a memory request left at zero puts a pod in "
        "the same first group as a BestEffort one, while an unreserved cpu "
        "request does not affect eviction at all."
    ),
}

# A quantity Kubernetes does not count: `0`, `0m`, `0Mi`, `0.0`. Anything that
# will not parse counts as non-zero -- a quantity this cannot read is far more
# likely to be a real reservation than a zero spelled strangely.
_QUANTITY_NUMBER = re.compile(r"^\s*([+-]?[0-9.]+)")


def _is_zero_quantity(quantity) -> bool:
    match = _QUANTITY_NUMBER.match(str(quantity))
    if not match:
        return False
    try:
        return float(match.group(1)) == 0.0
    except ValueError:
        return False


def _declared(resources: dict, field: str, resource: str) -> bool:
    """Whether `field` carries a countable quantity for `resource`.

    Kubernetes' QoS computation reads cpu and memory alone and skips any
    quantity that is not greater than zero, so `nvidia.com/gpu: 1`,
    `ephemeral-storage: 1Gi` and `cpu: 0` all leave a container silent.
    """
    quantity = (resources.get(field) or {}).get(resource)
    return quantity is not None and not _is_zero_quantity(quantity)


def _qos_containers(workload: dict) -> list[dict]:
    """Every container Kubernetes' QoS computation reads.

    Deliberately not `_effective_containers`. QoS iterates `spec.containers`
    and *all* of `spec.initContainers` with no `restartPolicy` filter, so a
    plain init container's requests decide the class even though they never
    count toward the pod's effective request — the two questions need two
    container sets, and reusing one for both is how the class came out wrong.
    Ephemeral containers are absent from both; upstream excludes them because
    they cannot declare resources.
    """
    template = workload["template"]
    return list(template.get("containers") or []) + list(template.get("initContainers") or [])


def _qos_class(containers: list[dict], limitranges: dict, namespace: str) -> str:
    """Kubernetes' QoS algorithm, read off the workload spec.

    Guaranteed needs every container to carry a `cpu` *and* a `memory` limit
    above zero with a request equal to it; an absent request is copied from the
    limit, so a container declaring limits alone qualifies. A pod where no
    container declares cpu or memory at all is BestEffort. Everything else is
    Burstable.

    Wrong in one direction on purpose. Two spellings of one quantity (`100m`
    and `0.1`) compare unequal, and a LimitRange `defaultRequest` can inject a
    request below the limit; both send the pod to Burstable. Burstable is the
    branch that claims the least, so an unmodelled shape landing there
    understates rather than misstates.
    """
    declares_compute = False
    guaranteed = True
    for container in containers:
        resources = container.get("resources") or {}
        for resource in _REQUEST_RESOURCES:
            has_request = _declared(resources, "requests", resource)
            has_limit = _declared(resources, "limits", resource)
            declares_compute = declares_compute or has_request or has_limit
            if not has_limit:
                guaranteed = False
            elif has_request:
                if resources["requests"][resource] != resources["limits"][resource]:
                    guaranteed = False
            elif _has_default(limitranges, namespace, "defaultRequest", resource):
                guaranteed = False
    if not declares_compute:
        return _QOS_BEST_EFFORT
    return _QOS_GUARANTEED if guaranteed else _QOS_BURSTABLE


def check_no_requests(workload: dict, context: dict) -> dict | None:
    limitranges = context["limitranges"]
    containers = _effective_containers(workload)
    missing_by_container = {}
    # Whether a limit already covers every request the check is about to report
    # missing. A property of the pod, not of one container, and half of the
    # Impact; the QoS class below is the other half.
    unbacked_missing: set[str] = set()
    for container in containers:
        resources = container.get("resources") or {}
        requests, limits = resources.get("requests") or {}, resources.get("limits") or {}
        missing = [
            resource
            for resource in _REQUEST_RESOURCES
            if resource not in requests
            and not _has_default(limitranges, workload["ns"], "defaultRequest", resource)
        ]
        # A limit with no request is not an unreserved resource: Kubernetes
        # copies the limit into the request before the scheduler sees the pod.
        # It stays a finding -- §3.1 wants the request declared, not inferred
        # from a ceiling -- but it is the opposite failure from an unreserved
        # one, so it must not draw the unreserved sentence.
        unbacked_missing |= {resource for resource in missing if resource not in limits}
        if missing:
            missing_by_container[container.get("name", "")] = missing
    if not missing_by_container:
        return None
    hit = {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": "; ".join(f"{c}: missing {','.join(m)}" for c, m in missing_by_container.items()),
    }
    qos = _qos_class(_qos_containers(workload), limitranges, workload["ns"])
    if qos == _QOS_BEST_EFFORT:
        # Same string the table carries, set here so this arm is flagged
        # authoritative like the other two rather than falling through.
        hit["impact"] = _IMPACT_BEST_EFFORT
        return hit
    if unbacked_missing:
        head = _IMPACT_UNRESERVED.format(resources=" or ".join(sorted(unbacked_missing)))
    else:
        head = _IMPACT_CEILING_RESERVED
    hit["impact"] = head + _IMPACT_BY_QOS[qos]
    return hit


# §3.2 has two arms and they point in opposite directions, because the kubelet
# ranks a memory-pressure eviction on whether usage exceeds the memory *request*
# -- never on the QoS class. An uncapped container that also declares no request
# exceeds zero on its first byte and sits in the first bucket from the start; an
# uncapped container with a request is in the last bucket until the leak passes
# that request. Only the second one shields itself at the neighbours' expense,
# and it is the first that every live finding is.
#
# kubernetes.io contradicts itself here -- `pod-qos.md` still says Burstable pods
# "are evicted only after all BestEffort Pods are evicted", which upstream issue
# #129759 has open against the code -- so both arms name the sort keys rather
# than asserting an order a reader can find an official page against.
_IMPACT_NO_LIMIT_UNREQUESTED = (
    "Nothing caps memory here and no memory request is declared either, so a "
    "leak makes this workload the node's first casualty rather than its "
    "neighbours'. The kubelet ranks memory-pressure eviction by whether usage "
    "exceeds the memory request, then by Pod Priority, then by how far usage "
    "sits above that request — never by QoS class — so a request of zero puts "
    "this pod in the first group from its first byte, and at equal priority the "
    "leak ranks it ahead of the BestEffort pods beside it. The kernel's OOM "
    "killer is a separate mechanism that can fire before the kubelet reacts, "
    "and it scores these containers at the top of its range too."
)
_IMPACT_NO_LIMIT_REQUESTED = (
    "Nothing caps memory here, so a leak grows until the node is under "
    "pressure, and up to the declared request the kubelet does evict "
    "co-located workloads first. Past that request this pod joins the group "
    "evicted first, ranked by how far its usage exceeds it. The kernel's OOM "
    "killer scores a container down in proportion to its memory request, so "
    "the larger the request the more of the node a leak here can take before "
    "the kernel picks it over a neighbour."
)


def check_no_memory_limit(workload: dict, context: dict) -> dict | None:
    limitranges = context["limitranges"]
    missing, unrequested, requested = [], [], []
    for container in workload["template"].get("containers") or []:
        resources = container.get("resources") or {}
        if "memory" in (resources.get("limits") or {}):
            continue
        name = container.get("name", "")
        missing.append(name)
        # Key presence decides whether the limit is missing, matching §3.1's
        # reading of the manifest; the request is read with `_declared`
        # instead, because the arm turns on the quantity the eviction ranking
        # subtracts and `requests.memory: 0` is a request of zero. A LimitRange
        # `defaultRequest` counts: it is injected before the scheduler sees the
        # pod, so the ranking reads it even though the manifest is silent.
        if _declared(resources, "requests", "memory") or _has_default(
            limitranges, workload["ns"], "defaultRequest", "memory"
        ):
            requested.append(name)
        else:
            unrequested.append(name)
    if not missing or _has_default(limitranges, workload["ns"], "default", "memory"):
        return None
    if unrequested and requested:
        impact = (
            f"{', '.join(unrequested)}: {_IMPACT_NO_LIMIT_UNREQUESTED} "
            f"{', '.join(requested)}: {_IMPACT_NO_LIMIT_REQUESTED}"
        )
    else:
        impact = _IMPACT_NO_LIMIT_UNREQUESTED if unrequested else _IMPACT_NO_LIMIT_REQUESTED
    return {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": f"containers missing a memory limit: {', '.join(missing)}",
        "impact": impact,
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
                    # The PDB's own reconciler, not the matched workload's. The
                    # finding is about the PDB's shape and the fix edits the PDB,
                    # so a note naming what holds the Deployment would send the
                    # reader to the wrong chart.
                    "reconciler": reconciler_of(pdb.get("metadata") or {}),
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
    the check table default, because the two sub-cases disagree.

    The only cluster-scoped check that walks a `*_by_namespace` map with no
    workload to anchor it, so it is the only one that has to apply S1 and S2
    itself. `check_no_hpa` reads `context["hpas"]` under a workload's own
    namespace and `check_blocking_pdb` drops a PDB whose target is not in the
    audited set, so both inherit the suppressions for free. This one did
    not, and GKE makes that expensive: `kube-state-metrics` lives in
    `gke-managed-cim` and `opentelemetry-collector` in `gke-managed-otel`, S1
    keeps their StatefulSet and Deployment out of `workloads`, and their HPAs
    then read as pointing at objects that do not exist. That was 17 `minor`
    findings across a 16-cluster fleet — one per cluster — about resources
    Google owns and the operator cannot edit.

    Dangling resolves against `workload_keys` rather than `workloads` for the
    other half of the same mistake: the target of a *user* HPA can leave the
    audited set by being exempted (S4) or scaled to zero (S5) while plainly
    still existing, and "scaleTargetRef … not found" is a claim about the
    cluster, not about this audit's roster.
    """
    known = context["workload_keys"]
    hits = []
    for ns, hpas in context["hpas"].items():
        if _is_system_namespace(ns):  # S1
            continue
        for hpa in hpas:
            meta = hpa.get("metadata") or {}
            if "addonmanager.kubernetes.io/mode" in (meta.get("labels") or {}):  # S2
                continue
            name = meta.get("name", "")
            reconciler = reconciler_of(meta)
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
                        "reconciler": reconciler,
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
                        "reconciler": reconciler,
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


def _containers_behind_a_service(workload: dict, services: list[dict]) -> list[dict]:
    """The containers a Service actually routes traffic to.

    A container with no readiness probe counts as ready the moment it starts,
    so a probe-less log shipper cannot hold traffic off a pod. Only the
    container serving the Service's `targetPort` can, which makes it the only
    one whose missing probe is the risk this check names.

    Resolving that container means reading the native sidecars too --
    `initContainers` with `restartPolicy: Always` serve ports like any other
    container. The gateway's own Service targets 8643, which belongs to the
    `envoy-credential-proxy` sidecar and nothing under `containers`; a check
    that reads only `containers` reported the one container holding the
    Service port as having no probe when it has one.

    Declaring `ports` is optional -- kubelet routes to a port a container never
    named -- so when nothing in the pod declares a matching one there is no
    routing to infer, and every container stays in the path as before.
    """
    targets = set()
    for svc in services:
        for port in (svc.get("spec") or {}).get("ports") or []:
            targets.add(port.get("targetPort", port.get("port")))
    targets.discard(None)
    containers = _effective_containers(workload)
    behind = [
        c
        for c in containers
        if any(p.get("containerPort") in targets or p.get("name") in targets for p in c.get("ports") or [])
    ]
    return behind or containers


#: Port names the ecosystem reserves for a Prometheus scrape endpoint. The name
#: is the discriminator and the number is not: `ServiceMonitor` and
#: `PodMonitor` both select on `port` by name, so this is the convention a
#: chart author is already following, while 9402 and 8080 mean nothing on their
#: own.
_METRICS_PORT_NAMES = frozenset({"metrics", "http-metrics", "https-metrics", "telemetry"})

#: The three things "Service-backed" can mean, as the trailing `(scope)` of an
#: exposure line. §3.9 and §3.11 of the SOP key their impact claims off these
#: strings verbatim, so they are named rather than spelled inline.
_SCOPE_SERVING = "serving traffic"
_SCOPE_METRICS_ONLY = "metrics scrape only"
_SCOPE_NO_PORTS = "no ports declared"


def _metrics_only_ports(services: list[dict]) -> bool:
    """Every port every selecting Service exposes is a metrics scrape port.

    A workload whose only Service is a scrape endpoint is "Service-backed" in
    the sense 3.9 flags on, and in no other sense: nothing routes a user
    request to it. Prometheus retries a failed scrape, so a probe-less pod
    joining that Service early costs a gap in a graph, not a dropped request.

    Named ports only, and conservatively: an unnamed port is not treated as
    metrics, so a single-port Service that omits the name keeps the finding
    exactly as it read before. That is the safe direction -- the line this
    feeds suppresses an impact claim, and suppressing it wrongly is the
    expensive error.
    """
    ports = [port for svc in services for port in (svc.get("spec") or {}).get("ports") or []]
    return bool(ports) and all(port.get("name") in _METRICS_PORT_NAMES for port in ports)


def _exposure_scope(services: list[dict]) -> str:
    """Which of the three things "Service-backed" means for this workload.

    `_metrics_only_ports` answers one question and cannot answer this one: it
    is false both for a Service carrying user requests and for a Service that
    declares no ports at all, and those are not the same claim. A port-less
    Service routes nothing through its own ClusterIP; if it is headless its DNS
    records still hand out pod IPs, so a client that already knows a port can
    reach the pods anyway. Neither of the other two scopes is true of it, so it
    gets its own rather than being rounded to whichever is nearer -- rounding
    it to `serving traffic` is what made a Service with no ports publish "every
    rollout sends production traffic to pods that are not yet serving".
    """
    ports = [port for svc in services for port in (svc.get("spec") or {}).get("ports") or []]
    if not ports:
        return _SCOPE_NO_PORTS
    return _SCOPE_METRICS_ONLY if _metrics_only_ports(services) else _SCOPE_SERVING


def _exposure_line(services: list[dict]) -> str:
    """`selecting services: name[port,port] (scope)` — what "Service-backed" means here.

    Both checks that gate on a selecting Service publish an Impact line about
    production traffic, and neither can tell whether there is any. On
    2026-09-01 the live fleet had `cert-manager` and `cert-manager-cainjector`
    each selected by one Service whose only port is `http-metrics/9402`, and
    `argocd-notifications-controller` by one whose only port is `metrics/9001`;
    nothing distinguishes them from `argocd-redis` (`tcp-redis/6379`) or
    `kube-agents-controller-manager` (an unnamed 443 to a webhook) except the
    ports, so name the ports and say which case it is.
    """
    exposure = "; ".join(
        "{}[{}]".format(
            (svc.get("metadata") or {}).get("name", "?"),
            ",".join(str(port.get("name") or port.get("port")) for port in (svc.get("spec") or {}).get("ports") or []) or "no ports",
        )
        for svc in services
    )
    return f"selecting services: {exposure} ({_exposure_scope(services)})"


def check_probes_readiness(workload: dict, context: dict) -> dict | None:
    services = _selecting_services(workload, context)
    if not services:
        return None
    missing = [
        c.get("name", "")
        for c in _containers_behind_a_service(workload, services)
        if c.get("name") not in _SELF_HEALTH_SIDECARS and not c.get("readinessProbe")
    ]
    if not missing:
        return None
    return {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": (
            f"Service-backed, containers missing a readiness probe: {', '.join(missing)}\n"
            f"{_exposure_line(services)}"
        ),
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
    services = _selecting_services(workload, context)
    if not services:
        return None
    # Same claim, same qualifier as `check_probes_readiness`. Giving one check
    # the exposure line and not the other left the 2026-09-01 run publishing
    # `cert-manager` and `cert-manager-cainjector` as "single replica,
    # Service-backed" -- a rollout-drops-user-traffic claim -- one section
    # below the readiness finding on those same two Deployments that had just
    # said the only Service in front of them is a scrape endpoint.
    return {
        "object": f"Deployment/{workload['name']}",
        "excerpt": f"single replica, Service-backed\n{_exposure_line(services)}",
    }


# Kubernetes' own default when a pod spec omits `terminationGracePeriodSeconds`.
# Named because the excerpt has to print a number either way, and printing the
# absent case as `0` or `None` would describe a pod that is killed instantly.
DEFAULT_GRACE_PERIOD_S = 30


def check_rollout_drops_traffic(workload: dict, context: dict) -> dict | None:
    """A Service-backed workload whose containers have no `preStop` hook.

    Endpoint removal and `SIGTERM` are two independent, concurrent reactions to
    a pod being deleted: the kubelet starts terminating the container while the
    endpoints controller is still propagating the removal out to every node's
    kube-proxy and to every Ingress or mesh sidecar holding a connection. The
    container almost always wins that race, so requests keep arriving at a
    process that has already begun shutting down and are answered with a
    connection reset. A `preStop` hook is the only thing in the pod lifecycle
    that delays `SIGTERM`, which is why the fix is a hook that sleeps rather
    than anything in the application.

    That makes this the one rollout defect nobody sees in a rolling update: the
    Deployment goes green, the new pods pass their probes, and the only trace
    is a burst of 502s in whatever sits in front. It costs errors on every
    deploy, every node drain, every autoscaler scale-down, and every
    preemption -- which on Spot capacity is continuous.

    Narrowed three ways, because unnarrowed this fires on essentially every
    workload in a fleet and a check that flags everything has told you nothing.
    `_SCOPE_SERVING` drops the workloads whose only Service is a metrics scrape
    endpoint -- Prometheus retries, so a reset there costs a gap in a graph
    rather than a user's request -- and the same qualifier is what
    `check_probes_readiness` and `check_single_replica` already use to avoid
    the same false claim. More than one replica, because a single-replica
    workload has a hard outage on every rollout whatever its hooks do, and
    `check_single_replica` is the finding that says so; adding a `preStop`
    sleep there delays the outage rather than removing it. And only the
    containers actually behind the Service's `targetPort`, for the reason
    `_containers_behind_a_service` gives: a log shipper with no hook is not in
    the request path and cannot reset anything.

    DaemonSets are in scope with no replica test. One pod per node is not the
    single-replica case -- the other nodes keep serving -- so a rolling update
    of a Service-backed DaemonSet drops exactly the traffic this describes.
    """
    if workload["kind"] == "Deployment" or workload["kind"] == "StatefulSet":
        if (workload["spec"].get("replicas", 1) or 1) <= 1:
            return None
    services = _selecting_services(workload, context)
    if not services or _exposure_scope(services) != _SCOPE_SERVING:
        return None
    missing = [
        c.get("name", "")
        for c in _containers_behind_a_service(workload, services)
        if c.get("name") not in _SELF_HEALTH_SIDECARS and not (c.get("lifecycle") or {}).get("preStop")
    ]
    if not missing:
        return None
    grace = workload["template"].get("terminationGracePeriodSeconds")
    if grace is None:
        grace = DEFAULT_GRACE_PERIOD_S
    return {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": (
            f"Service-backed, containers with no preStop hook: {', '.join(missing)}\n"
            f"terminationGracePeriodSeconds={grace}\n"
            f"{_exposure_line(services)}"
        ),
    }


def _max_unavailable_count(strategy: dict, replicas: int) -> int | None:
    """`maxUnavailable` resolved against a replica count, or `None` if unset.

    The field is an `IntOrString`: `2` means two pods, `"25%"` means a
    proportion, and Kubernetes rounds a percentage *down* for `maxUnavailable`
    so that rounding can never take out more pods than asked. Reproducing that
    rounding direction matters here -- rounding up would make `"50%"` of 3
    resolve to 2, and this check would then call a perfectly ordinary rollout a
    total outage.
    """
    raw = strategy.get("maxUnavailable")
    if raw is None:
        return None
    text = str(raw).strip()
    if text.endswith("%"):
        try:
            return int(replicas * float(text[:-1]) / 100)
        except ValueError:
            return None
    try:
        return int(text)
    except ValueError:
        return None


def check_strategy_causes_downtime(workload: dict, context: dict) -> dict | None:
    """A Deployment whose own update strategy takes every replica down at once.

    Two spellings of the same outage. `strategy.type: Recreate` tells the
    Deployment controller to delete every existing pod and only then create the
    replacements, so a workload with three replicas serves nothing from the
    first deletion until the first new pod passes its readiness probe -- an
    image pull plus a cold start, tens of seconds at best and minutes for
    anything that warms a cache on boot. `RollingUpdate` with `maxUnavailable`
    at or above the replica count is the same thing written the long way: the
    controller is permitted to take all of them down in one step, and it does.

    This is worth its own check because it is invisible in every other reading
    of the object. The Deployment reports `Available` between rollouts, the
    replica count is whatever the author intended, the probes are correct, and
    there is no event to find afterwards -- the outage lasts only as long as
    the rollout and leaves nothing behind but errors in a client's logs. It
    surfaces only when someone reads `spec.strategy`, and until now nothing in
    this collector did except to suppress: `check_single_replica` returns
    `None` on `Recreate` (a single replica has an outage on any strategy, so
    naming the strategy there would be the wrong finding), and returns `None`
    again whenever `replicas != 1`. A `Recreate` Deployment with three replicas
    fell through both arms and was reported by nothing.

    Not a finding on a single replica, for that same reason, and not one on
    `maxUnavailable` merely being large: `maxUnavailable: 2` of 3 is a
    deliberate speed-over-capacity choice that still leaves a pod serving.
    Only reaching the replica count is unambiguous, because at that point the
    strategy permits zero surviving pods.
    """
    if workload["kind"] != "Deployment":
        return None
    replicas = workload["spec"].get("replicas", 1) or 1
    if replicas <= 1:
        return None
    strategy = workload["spec"].get("strategy") or {}
    kind = strategy.get("type") or "RollingUpdate"
    if kind == "Recreate":
        detail = f"strategy.type=Recreate, replicas={replicas}"
    else:
        unavailable = _max_unavailable_count(strategy.get("rollingUpdate") or {}, replicas)
        if unavailable is None or unavailable < replicas:
            return None
        raw = (strategy.get("rollingUpdate") or {}).get("maxUnavailable")
        detail = (
            f"strategy.rollingUpdate.maxUnavailable={raw} resolves to {unavailable} "
            f"of {replicas} replicas"
        )
    services = _selecting_services(workload, context)
    exposure = f"\n{_exposure_line(services)}" if services else ""
    return {
        "object": f"Deployment/{workload['name']}",
        "excerpt": f"{detail}{exposure}",
        # A rollout that drops every replica of something serving user traffic
        # is an outage; the same strategy on a workload nothing routes to is a
        # latent one, waiting for the first Service to be pointed at it.
        "severity": "major" if services and _exposure_scope(services) == _SCOPE_SERVING else "minor",
    }


def _rfc3339(stamp: str) -> float | None:
    """Kubernetes timestamp -> epoch seconds, or `None` if it will not parse.

    `None` rather than an exception because every caller here is deciding
    whether to *report* something: a timestamp the API served in a shape this
    does not expect is a reason to stay quiet about that object, not a reason
    to fail the cluster's whole collection and lose the other ten checks.
    """
    if not stamp:
        return None
    try:
        return datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc
        ).timestamp()
    except (ValueError, TypeError):
        return None


def _failure_summary(finished: list[dict]) -> str:
    """The `Failed` condition's reason on the most recent Job, as a clause.

    Empty string when there is nothing to add, so the caller can concatenate it
    unconditionally.

    Without this the finding says a schedule has stopped succeeding and stops
    there, which sends the reader to `kubectl describe` to learn the one thing
    that decides what to do about it. `BackoffLimitExceeded` means the
    container is exiting non-zero and the fix is in the image or its
    arguments; `DeadlineExceeded` means it is running out of
    `activeDeadlineSeconds`, and the fix is usually the deadline or the
    schedule's period rather than the code. Those are different remediations
    and the Job already carries which one applies.

    Only the newest Job, not all of them: the retained set is at most
    `failedJobsHistoryLimit` deep and consecutive failures of one schedule
    almost always share a reason, so listing three of them pads the excerpt
    without adding a fact. `finished` arrives sorted by `creationTimestamp`
    because `cronjobs_with_jobs` sorts it.
    """
    if not finished:
        return ""
    conditions = (finished[-1].get("status") or {}).get("conditions") or []
    for condition in conditions:
        if condition.get("type") == "Failed" and str(condition.get("status")) == "True":
            reason = str(condition.get("reason") or "").strip()
            return f" (most recent: {reason})" if reason else ""
    return ""


def check_schedule_never_succeeds(context: dict) -> list[dict]:
    """Cluster-scoped: iterates CronJobs, which are not in `workloads` at all.

    The other ten checks read pod templates and ask whether the workload is
    configured to survive something. This one asks whether a schedule is still
    producing anything, which is not a question a template can answer -- so it
    reads `status` on the CronJob and on the Jobs it retains, and reports the
    CronJob rather than any individual Job. A Job is one attempt; the finding
    is about the run of them.

    Both times come off the dump and neither is compared against `now`, for
    the reason `STALE_SUCCESS_GAP_HOURS` gives.
    """
    hits = []
    for entry in context["cronjobs"]:
        cronjob = entry["cronjob"]
        spec, status = cronjob.get("spec") or {}, cronjob.get("status") or {}
        if spec.get("suspend"):
            continue
        last_schedule = _rfc3339(status.get("lastScheduleTime", ""))
        if last_schedule is None:
            # Never fired. A CronJob that has not come due yet has nothing to
            # have failed at, and one whose schedule has passed without firing
            # is a scheduler problem this check would misattribute.
            continue
        finished = [
            job
            for job in entry["jobs"]
            if (job.get("status") or {}).get("succeeded") or (job.get("status") or {}).get("failed")
        ]
        if not finished or any((job.get("status") or {}).get("succeeded") for job in finished):
            # An active Job counts as neither, deliberately: an hourly schedule
            # almost always has one in flight, and letting it veto the check
            # would make the check unreachable on the schedules it most needs
            # to cover.
            continue
        last_success = status.get("lastSuccessfulTime", "")
        # Never succeeded at all -> measure from when the CronJob was created,
        # which is the earliest moment it could have. Without this arm the
        # worst case in the check's remit, a schedule that has never once
        # worked, is the one case it stays silent about.
        baseline_field = "lastSuccessfulTime" if last_success else "creationTimestamp"
        baseline = _rfc3339(last_success or (cronjob.get("metadata") or {}).get("creationTimestamp", ""))
        if baseline is None:
            continue
        gap_hours = (last_schedule - baseline) / SECONDS_PER_HOUR
        if gap_hours < STALE_SUCCESS_GAP_HOURS:
            continue
        hits.append(
            {
                "namespace": entry["ns"],
                "object": f"CronJob/{entry['name']}",
                "excerpt": (
                    f"schedule={spec.get('schedule', '')!r} suspend=false; "
                    f"last fired {status.get('lastScheduleTime', '')}, "
                    f"{baseline_field}={last_success or (cronjob.get('metadata') or {}).get('creationTimestamp', '')} "
                    f"({gap_hours:.0f}h without a successful run); "
                    f"{len(finished)} retained Job(s), all failed"
                    f"{_failure_summary(finished)}"
                ),
                "reconciler": reconciler_of(cronjob.get("metadata") or {}),
            }
        )
    return hits


# The CronJob controller's own default when `spec.concurrencyPolicy` is unset.
# It is the permissive one, which is why the absent case and the explicit case
# are the same finding.
DEFAULT_CONCURRENCY_POLICY = "Allow"

# How many retained Job creation times it takes to call a cadence observed.
# Two timestamps make one gap, and one gap is a coincidence -- a single retry
# would read as the schedule's period. Three give two gaps to take the wider
# of, which is what `_observed_period_s` does.
MIN_JOBS_FOR_PERIOD = 3


def _observed_period_s(jobs: list[dict]) -> float | None:
    """How often this schedule actually fires, read off the Jobs it retained.

    The alternative is parsing `spec.schedule`, and the comment on
    `STALE_SUCCESS_GAP_HOURS` already gives the reason not to: this collector
    has no business learning cron syntax, and a verdict that depends on a
    hand-rolled cron parser is a verdict that fails in whichever direction the
    parser is wrong. Consecutive `creationTimestamp`s are the same fact,
    already in the dump, in a form that needs no clock and no grammar.

    The *widest* gap rather than the narrowest. Retained Jobs are a sample with
    holes in it -- `successfulJobsHistoryLimit` evicts, and a run that was
    skipped or suspended leaves a gap of several periods -- so the narrowest
    gap is the closest thing to the true period and the widest is an
    overestimate of it. Overestimating is the safe direction here: the caller
    reports overlap when a run lasts *longer* than a period, so a period that
    is too long makes the check quieter, never louder.
    """
    stamps = sorted(
        stamp
        for stamp in (_rfc3339((job.get("metadata") or {}).get("creationTimestamp", "")) for job in jobs)
        if stamp is not None
    )
    if len(stamps) < MIN_JOBS_FOR_PERIOD:
        return None
    gaps = [later - earlier for earlier, later in zip(stamps, stamps[1:]) if later > earlier]
    return max(gaps) if gaps else None


def _longest_run_s(jobs: list[dict]) -> tuple[float | None, str]:
    """The longest completed run among the retained Jobs, and which one it was.

    Only Jobs that both started and finished. A Job still running has no
    `completionTime`, and treating "started a long time ago and has not
    finished" as a duration would report the one case this check must not
    guess at: a Job that is wedged rather than slow.
    """
    longest, name = None, ""
    for job in jobs:
        status = job.get("status") or {}
        start = _rfc3339(status.get("startTime", ""))
        end = _rfc3339(status.get("completionTime", ""))
        if start is None or end is None or end < start:
            continue
        if longest is None or (end - start) > longest:
            longest, name = end - start, (job.get("metadata") or {}).get("name", "")
    return longest, name


def check_cronjob_runs_overlap(context: dict) -> list[dict]:
    """Cluster-scoped: a CronJob that takes longer to run than it waits to rerun.

    `concurrencyPolicy: Allow` -- the default, so almost always unstated -- lets
    the controller start the next run whether or not the last one finished. That
    is harmless until a run outlasts its own period, and then it is a pile-up:
    every period adds a worker without removing one, so a schedule that fires
    every five minutes and takes six accumulates runs until something in the
    cluster runs out. The failure is not the CronJob's. It lands on whatever
    the job talks to -- connection-pool exhaustion on a database, duplicate rows
    from two copies of the same import, a node's memory going to N copies of a
    batch process -- which is why the CronJob itself looks healthy the whole
    time and every retained Job reports `succeeded`.

    Measured, not predicted. The check does not read `spec.schedule` and does
    not guess at how long a run should take: it compares the longest run the
    cluster actually recorded against the widest gap between the runs it
    actually started, both off timestamps already in the dump. So a hit says
    this schedule has already overlapped, not that its policy would allow it
    to -- which is the difference between a finding and a lint rule, given that
    the permissive policy is the default and therefore describes most CronJobs
    in any fleet.

    `Forbid` and `Replace` are both excluded because both bound the pile-up:
    `Forbid` skips the new run, `Replace` kills the old one. Which of the two
    to move to is a real decision -- skipping a run loses that period's work,
    killing one loses the work in flight -- so this reports the collision and
    leaves the choice to whoever knows what the job does.
    """
    hits = []
    for entry in context["cronjobs"]:
        cronjob = entry["cronjob"]
        spec = cronjob.get("spec") or {}
        if spec.get("suspend"):
            continue
        policy = spec.get("concurrencyPolicy") or DEFAULT_CONCURRENCY_POLICY
        if policy != DEFAULT_CONCURRENCY_POLICY:
            continue
        period = _observed_period_s(entry["jobs"])
        if period is None:
            continue
        longest, slowest = _longest_run_s(entry["jobs"])
        if longest is None or longest < period:
            continue
        stated = "concurrencyPolicy=Allow" if spec.get("concurrencyPolicy") else "concurrencyPolicy unset (defaults to Allow)"
        hits.append(
            {
                "namespace": entry["ns"],
                "object": f"CronJob/{entry['name']}",
                "excerpt": (
                    f"schedule={spec.get('schedule', '')!r} {stated}; "
                    f"slowest retained run {slowest} took {longest / SECONDS_PER_MINUTE:.1f}m "
                    f"against an observed period of {period / SECONDS_PER_MINUTE:.1f}m "
                    f"across {len(entry['jobs'])} retained Job(s), "
                    "so a run is still going when the next one starts"
                ),
                "reconciler": reconciler_of(cronjob.get("metadata") or {}),
            }
        )
    return hits


# --------------------------------------------------------------------------- #
# compliance-audit: all twelve checks of §2.
#
# Unlike obtainability's Deployment/StatefulSet/DaemonSet templates, this
# stream's workload dump includes bare Pods (owned ones excluded — audit the
# owning controller, per the SOP's own reasoning) and reads `.spec` directly,
# resolved per kind: a Pod's `.spec` *is* the pod spec; a CronJob's is nested
# two objects deeper. `_pod_spec_of` is that resolution, done once instead of
# three times.
# --------------------------------------------------------------------------- #

COMPLIANCE_WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "CronJob", "Pod")
# The subset carrying `spec.replicas`, so a zero there means "no pod". A
# DaemonSet has no such field (its count is the node count), and a bare Pod
# either exists or does not.
_SCALABLE_WORKLOAD_KINDS = ("Deployment", "StatefulSet")
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


# What every excerpt on a suspended CronJob gains, and why one is not enough to
# drop the finding. `spec.suspend: true` stops the controller creating Jobs, so
# nothing in the template is running now and nothing will start on a schedule --
# but the template is still what runs the moment someone unsuspends it, which is
# the shape both compliance and ai-security exist to audit, and suspension is a
# field one `kubectl patch` undoes.
#
# What it does change is the tense a reader should give the impact. The
# 2026-09-05 ai-security report told an operator that `CronJob/ai-batch-finetune`
# pulls an unpinned model and that "the bytes that arrive at the next pod restart
# are whatever the source serves then" -- about a CronJob suspended since it was
# created, whose `status.lastScheduleTime` is empty and which has never produced
# a Job. Every word of the detection was right and the sentence built on it
# described something that cannot currently happen.
#
# Stated here rather than left to the model because `adopt_collector_evidence`
# overwrites the model's excerpt with this one: prose in an SOP would not reach
# the reader, and the excerpt is what the impact gets written from.
_SUSPENDED_CRONJOB_NOTE = (
    " (CronJob is suspended: spec.suspend=true, so no Job runs on the schedule "
    "until it is resumed -- the template below is still what runs when it is)"
)


def _is_suspended_cronjob(item: dict) -> bool:
    return item.get("kind") == "CronJob" and (item.get("spec") or {}).get("suspend") is True


# The same defect in the other direction, for the kinds that cannot suspend. A
# Deployment or StatefulSet at `spec.replicas: 0` has no pod, so every present
# tense in an impact is wrong for the same reason, and it is undone by the same
# one-field patch -- audit the template, correct the tense.
#
# The instance: the 2026-09-06 08:55Z ai-security report told an operator that
# `Deployment/ai-embeddings-tei` "executes arbitrary code shipped inside the
# model repository, with this pod's ServiceAccount, network access, and mounted
# volumes". That Deployment has sat at `replicas: 0` throughout -- there is no
# pod, so there is no ServiceAccount in use and nothing is mounted. The same
# finding then told them to confirm the fix with `kubectl logs
# deploy/ai-embeddings-tei --tail=50`, which returns nothing at zero replicas.
# That is the sharper half of the cost: a wrong tense misleads, but a
# verification step that cannot produce output sends the operator looking for a
# broken cluster.
#
# Not folded into `_SUSPENDED_CRONJOB_NOTE`: the two are disjoint by kind, and
# the field a reader has to go look at differs. The note names the kind because,
# unlike suspension, this reaches two of them.
def _scaled_to_zero_note(kind: str) -> str:
    return (
        f" ({kind} is scaled to zero: spec.replicas=0, so no pod is running from "
        "this template and commands that read one return nothing until it is "
        "scaled back up -- the template below is still what runs when it is)"
    )


# Explicit `0` only. An absent `replicas` means one, not none, and a `None` from
# a partial object is unknown rather than zero -- both are the running case.
def _is_scaled_to_zero(item: dict) -> bool:
    return (
        item.get("kind") in _SCALABLE_WORKLOAD_KINDS
        and (item.get("spec") or {}).get("replicas") == 0
    )


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
        out.append({
            "kind": item["kind"], "ns": ns, "name": meta.get("name", ""),
            "spec": _pod_spec_of(item), "suspended": _is_suspended_cronjob(item),
            "scaled_to_zero": _is_scaled_to_zero(item),
            "reconciler": reconciler_of(meta),
        })
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


# §2.2's flag-when is an **or** over three independent namespaces, so one
# sentence covering all three is false on most of what the check catches: a
# hostNetwork-only pod crosses no process boundary, and a hostPID/hostIPC-only
# pod is still fully inside NetworkPolicy. Each flag contributes its own clause
# and only the ones actually set are published.
_IMPACT_HOST_PID = (
    "hostPID puts every other pod's process table in this workload's /proc, so "
    "the command lines and argv-borne configuration of every tenant on the node "
    "are readable from here; where the container runs as root, which is the "
    "default, /proc/<pid>/root also reaches into those containers' filesystems."
)
_IMPACT_HOST_IPC = (
    "hostIPC shares the node's System V IPC and POSIX shared-memory segments, "
    "so this workload can read and write memory that other tenants' processes "
    "expect to be private to their own pod."
)
# Not "bypasses enforcement", which reads as a policy that is merely weaker.
# Neither GKE Dataplane V2 nor Calico enforces NetworkPolicy on a host-networked
# pod at all -- upstream leaves the behaviour undefined, and Calico's
# `IsValidCalicoWorkloadEndpoint` rejects such pods outright, which is why they
# vanish as rule peers as well as targets (projectcalico#1987, closed
# not_planned).
_IMPACT_HOST_NETWORK = (
    "hostNetwork takes this pod out of NetworkPolicy rather than loosening it: "
    "neither GKE Dataplane V2 nor Calico enforces policy on a host-networked "
    "pod, and it disappears as a rule peer as well, so a podSelector elsewhere "
    "written to admit it never matches. From another node its traffic arrives "
    "as the node IP, which only an ipBlock over the node CIDR can describe; "
    "from the same node it is allowed unconditionally, with no ipBlock "
    "recourse. It also reaches every node-local listener bound to 127.0.0.1, "
    "including ones whose owners took loopback for an isolation boundary."
)


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
    clauses = [
        clause
        for flag, clause in (
            (host_pid, _IMPACT_HOST_PID),
            (host_ipc, _IMPACT_HOST_IPC),
            (host_net, _IMPACT_HOST_NETWORK),
        )
        if flag
    ]
    return {
        "object": f"{workload['kind']}/{workload['name']}",
        "excerpt": f"hostNetwork={host_net} hostPID={host_pid} hostIPC={host_ipc}",
        "severity": severity,
        "impact": " ".join(clauses),
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
        crb_meta = crb.get("metadata") or {}
        name = crb_meta.get("name", "")
        reconciler = reconciler_of(crb_meta)
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
                    "reconciler": reconciler,
                }
            )
    return hits


_WILDCARD_BOOTSTRAP_LABEL = "kubernetes.io/bootstrapping"

# Verbs that make a wildcard *scope* an escalation rather than a broad read.
# Used only by the second stage-1 branch below, the one that fires without a
# `*` in `verbs`; a rule that already carries the verb wildcard is caught
# whatever it lists.
#
# Deliberately excludes get/list/watch. A ClusterRole of `apiGroups: ["*"],
# resources: ["*"], verbs: ["get","list","watch"]` is the ordinary
# cluster-monitoring shape -- Prometheus, a backup agent, the `view` role's
# cousins -- and reporting every one of those as critical is the
# false-positive flood this audit already learned to avoid once. Reading every
# Secret in the fleet is a real concern and it is not this check's; it needs a
# rule that can tell a scraper from an escalation, which this one cannot.
_ESCALATING_VERBS = frozenset(
    {"create", "update", "patch", "delete", "deletecollection", "impersonate", "escalate", "bind"}
)

# The three verbs RBAC escalation prevention keys on. A subject holding one of
# them reaches the ceiling directly; every other enumerated verb reaches it the
# long way, and §2.5 forbids describing the two the same way.
_RBAC_DIRECT_VERBS = ("bind", "escalate", "impersonate")
_IMPACT_RBAC_ANY_VERB = (
    "Subject can perform any verb on any resource in this scope, including "
    "reading Secrets and creating privileged pods -- an unbounded escalation path."
)
# §2.5 branch 3, one clause per verb family actually present and no others.
# Ordered by what an owner acts on first, not alphabetically.
_RBAC_VERB_CLAUSES = (
    (("get", "list"), "read every Secret in every namespace"),
    (("create",), "create privileged pods directly"),
    (
        ("patch", "update"),
        "rewrite an existing privileged workload into one it controls and take the node that runs it",
    ),
    (("delete", "deletecollection"), "destroy any object in the cluster"),
)
# §2.5 says `delete` *alone* is "data loss and denial of service rather than
# credential theft". Alone is load-bearing: appended to a list that also grants
# `get`, it would deny the credential theft the sentence just described.
_RBAC_DELETE_ONLY_TAIL = " That is data loss and denial of service rather than credential theft."
_IMPACT_RBAC_INDIRECT_TAIL = (
    " RBAC escalation prevention refuses a subject holding none of `bind`, "
    "`escalate` or `impersonate` a binding granting more than it already has, "
    "so the route to cluster-admin here is real but indirect."
)


def _wildcard_rbac_impact(verbs: set[str]) -> str:
    """§2.5's Impact, chosen by the verbs the matched rules actually grant.

    The model was asked to do this from the excerpt and did it for one finding
    out of two: `ClusterRole/argocd-server` holds `delete`, `get`, `patch` on
    every resource in every group and published the wildcard sentence, which
    collapses exactly the direct/indirect distinction the branch exists to
    keep. The verbs are already parsed here, so the branch is arithmetic rather
    than a reading comprehension task.
    """
    if "*" in verbs:
        return _IMPACT_RBAC_ANY_VERB
    direct = [v for v in _RBAC_DIRECT_VERBS if v in verbs]
    if direct:
        # Same ceiling as the wildcard, so the sentence holds -- but say which
        # verb reaches it: that one verb is the whole finding, and an owner
        # trimming the list needs to know it cannot stay.
        return f"{_IMPACT_RBAC_ANY_VERB} `{direct[0]}` is the verb that reaches it."
    clauses = [text for family, text in _RBAC_VERB_CLAUSES if verbs.intersection(family)]
    if not clauses:
        # A verb list matching none of the shapes above. No hit reaches here
        # today -- stage 1 admits a rule only for a `*` verb or a member of
        # `_ESCALATING_VERBS`, and every one of those is a direct verb or a
        # clause family. It stays because the two lists are edited separately:
        # widening `_ESCALATING_VERBS` alone must not silently promote a new
        # verb to the wildcard sentence. §2.5 picks this direction on purpose --
        # understating an escalation costs a reader one follow-up, overstating
        # one costs the audit its standing.
        return (
            "The rule's scope is every resource in every API group. Read the verbs in the "
            "excerpt for what that grants; it is not a grant of every verb."
            + _IMPACT_RBAC_INDIRECT_TAIL
        )
    if len(clauses) > 1:
        listed = f"{', '.join(clauses[:-1])}, and {clauses[-1]}"
        tail = ""
    else:
        listed = clauses[0]
        tail = _RBAC_DELETE_ONLY_TAIL if verbs & set(_RBAC_VERB_CLAUSES[-1][0]) else ""
    return (
        f"The verbs are enumerated rather than wildcarded, so this is not a grant of every "
        f"verb. With what it does grant, the subject can {listed}."
        + tail
        + _IMPACT_RBAC_INDIRECT_TAIL
    )


def _binding_principal(subject: dict) -> str:
    """How a subject is spelled to `kubectl auth can-i --as`.

    The remediation §2.5 mandates is `kubectl auth can-i --list --as=<subject>`,
    and until this was carried on the hit the model had to invent the subject:
    `ClusterRole/argocd-server`'s finding told the operator to enumerate
    `argocd-application-controller` instead -- the *other* finding's subject,
    holding `verbs: ["*"]` on `["*"]`, a strict superset. Diffing a proposed
    replacement against a superset passes whatever the replacement says.
    """
    kind, name = subject.get("kind"), str(subject.get("name") or "")
    if kind == "ServiceAccount":
        return f"system:serviceaccount:{subject.get('namespace', '')}:{name}"
    return name


def check_wildcard_rbac(context: dict) -> list[dict]:
    """The universal suppressions on line 126 of the SOP say "every check in
    this section", and this is the check that never applied them: it has two
    suppressions of its own -- the `rbac-defaults` label and the `system:` name
    prefix -- and neither covers a GKE add-on.

    `kubelet-api-admin` is the one that costs. GKE ships it on every cluster
    with `verbs: ["*"]` over five `nodes/*` subresources in the core group,
    bound to `User/kube-apiserver` so the API server can reach kubelets for
    `kubectl logs` and `kubectl exec`. The subject test reads that user as
    non-system because the name carries no `system:` prefix, so the role
    counted as bound, and every cluster in the fleet reported one `critical`.
    Four of the eight stored compliance runs rendered it and four dropped it,
    which is worse than either -- the ledger is keyed on (check, cluster,
    namespace, object), so the same object churned as new and resolved run to
    run. The run that rendered it per cluster made it 16 of 34 findings. It is
    also unfixable: the label is `Reconcile`, so the add-on manager reverts an
    edit, and a server-side dry-run patch is refused outright.

    The label test is the same S2 rung the workload checks use, and the SOP
    names this exact failure -- "flagging these is the fastest way to get this
    audit switched off".
    """
    # Keyed the same as before, but keeping the subjects rather than throwing
    # them away: the remediation names a principal, and the only place that
    # principal exists is the binding.
    bound_non_system: dict[tuple, list[str]] = {}
    for kind_key in ("clusterrolebindings", "rolebindings"):
        for binding in context.get(kind_key) or []:
            role_ref = binding.get("roleRef") or {}
            for subject in binding.get("subjects") or []:
                flagged, _ = _is_non_system_subject(subject)
                if flagged:
                    key = (role_ref.get("kind"), role_ref.get("name"))
                    principal = _binding_principal(subject)
                    if principal not in bound_non_system.setdefault(key, []):
                        bound_non_system[key].append(principal)

    hits = []
    for role in (context.get("roles") or []):
        meta = role.get("metadata") or {}
        if (meta.get("labels") or {}).get(_WILDCARD_BOOTSTRAP_LABEL) == "rbac-defaults":
            continue
        if meta.get("name", "").startswith("system:"):
            continue
        if "addonmanager.kubernetes.io/mode" in (meta.get("labels") or {}):  # S2
            continue
        wildcard_rules = [
            rule
            for rule in role.get("rules") or []
            if (
                "*" in (rule.get("verbs") or [])
                and (
                    (rule.get("apiGroups") or []) == [""]
                    or "*" in (rule.get("resources") or [])
                    or "*" in (rule.get("apiGroups") or [])
                )
            )
            # Second branch: the scope is every resource in every apiGroup and
            # the verbs are written out instead of wildcarded. Requiring a `*`
            # in `verbs` missed those, and the miss is not academic -- on the
            # reference fleet `ClusterRole/argocd-server` holds
            # `apiGroups: ["*"], resources: ["*"], verbs: ["delete","get",
            # "patch"]`, bound to `ServiceAccount/argocd/argocd-server`, and
            # graded clean. `get` on every resource in every group is every
            # Secret in every namespace; `patch` on every resource is enough to
            # rewrite a Deployment into a privileged pod. Enumerating three
            # verbs rather than typing `*` is a spelling, not a boundary.
            or (
                (rule.get("apiGroups") or []) == ["*"]
                and "*" in (rule.get("resources") or [])
                and _ESCALATING_VERBS & {str(v).lower() for v in (rule.get("verbs") or [])}
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
        verbs = {
            str(v).lower() for rule in wildcard_rules for v in (rule.get("verbs") or [])
        }
        # The subjects go in the excerpt rather than the recommendation because
        # `adopt_collector_evidence` restores the excerpt over whatever the run
        # published: a principal written anywhere else is a principal the model
        # is free to replace with a plausible-looking wrong one.
        principals = ", ".join(bound_non_system[key])
        hits.append(
            {
                "namespace": ns,
                "object": f"{role['kind']}/{meta.get('name')}",
                "excerpt": f"{json.dumps(wildcard_rules)}; bound to {principals}",
                "severity": "critical" if role.get("kind") == "ClusterRole" else "major",
                "impact": _wildcard_rbac_impact(verbs),
                # The role's, not the binding's. Both appear in this check --
                # the principals in the excerpt come from `bound_non_system` --
                # but the remediation narrows the rule, so the reader needs
                # whatever holds the role.
                "reconciler": reconciler_of(meta),
            }
        )
    return hits


# Cilium writes the pod's namespace into the endpoint's label set under this
# key, and Dataplane V2's ClusterNetworkPolicy selects on it. `k8s:` is the
# source prefix Cilium adds to labels it learned from Kubernetes; a policy may
# be written with or without it.
_CILIUM_NAMESPACE_LABELS = ("k8s:io.kubernetes.pod.namespace", "io.kubernetes.pod.namespace")


def _ccnp_coverage(policies: list[dict]) -> tuple[bool, set[str]]:
    """Which namespaces the cluster-wide policies actually put behind ingress
    enforcement: `(covers_every_namespace, the_named_ones)`.

    This used to be `bool(policies)` — one ClusterNetworkPolicy anywhere in the
    cluster suppressed §2.6 in every namespace. GKE installs Dataplane V2
    policies of its own, and a single one selecting one workload's labels was
    enough to make the whole cluster report no default-allow namespaces, which
    is the same silence a cluster with real coverage produces.

    Two independent questions, and a policy has to answer both to suppress a
    namespace. *Which endpoints* — an empty `endpointSelector` matches every
    endpoint in the cluster, and a selector naming the namespace label covers
    that namespace; any other selector picks out particular pods and leaves the
    namespace's posture unchanged. *Enforcing what* — Cilium isolates ingress
    only for a policy that carries an `ingress` section, so an egress-only
    cluster policy suppresses nothing here: §2.6 is about who can reach these
    pods.
    """
    covers_all = False
    covered: set[str] = set()
    for policy in policies:
        spec = policy.get("spec") or {}
        specs = [spec] + [s for s in (policy.get("specs") or []) if isinstance(s, dict)]
        for one in specs:
            if not one.get("ingress") and not one.get("ingressDeny"):
                continue
            selector = one.get("endpointSelector")
            if not selector:
                covers_all = True
                continue
            labels = (selector.get("matchLabels") or {}) if isinstance(selector, dict) else {}
            for key in _CILIUM_NAMESPACE_LABELS:
                if labels.get(key):
                    covered.add(labels[key])
    return covers_all, covered


# §2.6's partial-coverage arm. The table's sentence -- "every pod in this
# namespace accepts traffic from every pod in the cluster" -- is true of the
# other two arms and flatly false of this one, where the policies that exist
# are working and cover everything except the pods named in the excerpt. The
# SOP forbids that sentence here by name, and the run published it anyway over
# `kubeagents-system`, whose four policies police five of six pods; the title
# took the right branch while the Impact told the operator their policies do
# nothing. Written on the hit so `adopt_arm_impact` can hold it.
_IMPACT_NETPOL_PARTIAL = (
    "The named workloads accept traffic from every pod in the cluster, while "
    "the rest of the namespace is policed -- so the gap is invisible in a "
    "policy review that only asks whether this namespace has NetworkPolicies."
)


def check_netpol_missing(context: dict) -> list[dict]:
    """§2.6's exposure test is `kubectl get pods -n <ns> | wc -l`, so it reads
    `pod_namespaces` — every namespace holding a live Pod — and not the audited
    workload set.

    Those are different sets, and the difference swallowed the check.
    `normalize_compliance_workloads` drops any Pod carrying `ownerReferences`,
    because compliance audits the controller and never the pod, whose name
    carries a random suffix. Every pod a Deployment, StatefulSet, DaemonSet or
    Job creates carries one — so counting Pods in that set returned zero for
    the ordinary namespace, which then read as "zero workloads, no exposure,
    pure churn" and was skipped. The namespace the check exists to find, one
    running a Deployment with no NetworkPolicy, was the exact case it could not
    report: on this 16-cluster fleet `cert-manager` had three pods and no
    policy across every run, and the stream flagged nothing.
    """
    hits = []
    netpols_by_ns: dict[str, list[dict]] = {}
    for netpol in context.get("networkpolicies") or []:
        ns = (netpol.get("metadata") or {}).get("namespace", "")
        netpols_by_ns.setdefault(ns, []).append(netpol)
    pod_namespaces = context["pod_namespaces"]
    # §2.6's Do-NOT-flag case: a namespace already covered by a Dataplane V2
    # ClusterNetworkPolicy is not a default-allow posture just because it has
    # no *namespaced* NetworkPolicy of its own.
    ccnp_all, ccnp_covered = _ccnp_coverage(context.get("cluster_network_policies") or [])

    for ns_item in context.get("namespaces") or []:
        ns = (ns_item.get("metadata") or {}).get("name", "")
        if _is_system_namespace(ns):
            continue
        policies = netpols_by_ns.get(ns, [])
        if not policies:
            if ns not in pod_namespaces:
                continue  # no pods, no exposure, pure churn
            if ccnp_all or ns in ccnp_covered:
                continue
            # Namespace-scoped, because `adopt_collector_evidence` puts this
            # string under a cluster-wide `kubectl get netpol -A` command. Bare
            # "zero NetworkPolicies" then reads as a claim about the cluster,
            # and on kube-agents-host -- which has eleven, none of them in
            # cert-manager -- the one false line on an otherwise accurate
            # finding is what gets the audit switched off.
            # A plain count, with no verdict attached to it. The tally includes
            # policies in system namespaces and allow-all ones, so "and this
            # namespace is the gap" would be reading more into the number than
            # it carries; the reader needs to know the cluster is not
            # uniformly unpoliced, which the count alone says.
            elsewhere = sum(len(v) for k, v in netpols_by_ns.items() if k != ns)
            excerpt = "no NetworkPolicy in this namespace"
            if elsewhere:
                excerpt += f"; {elsewhere} in other namespaces of this cluster"
            hits.append({"namespace": ns, "object": f"Namespace/{ns}", "excerpt": excerpt, "severity": "major"})
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
            continue
        # A namespace holding policies is not therefore a covered namespace.
        # NetworkPolicy is additive and pod-scoped: a pod no policy selects has
        # no policy applied to it and stays reachable from anywhere, exactly as
        # if the namespace had none. The branch above answers "does this
        # namespace have a policy"; only the pod labels answer "does this pod
        # have one", and the second is the question the exposure turns on.
        # On the reference fleet that is `kubeagents-system`, whose four
        # policies name four workloads by label and leave the operator's own
        # manager pod -- 8081 and a 10250 webhook -- selected by none.
        if ccnp_all or ns in ccnp_covered:
            continue
        uncovered = _pods_no_policy_selects(context.get("pods") or [], ns, policies)
        if uncovered:
            # Namespace-scoped, not pod-scoped: a pod name carries a
            # ReplicaSet hash and a random suffix, so keying the ledger on one
            # would resolve and re-raise this finding on every rollout. The
            # workload names go in the excerpt, where churn costs nothing.
            live = sum(1 for p in context.get("pods") or [] if p.get("ns") == ns and _is_live_pod(p))
            names = sorted({_pod_workload_ref(p, context.get("workloads") or []) for p in uncovered})
            hits.append(
                {
                    "namespace": ns,
                    "object": f"Namespace/{ns}",
                    "excerpt": (
                        f"{len(uncovered)} of {live} pods here are selected by no NetworkPolicy that "
                        f"enforces Ingress: {', '.join(names)}; {len(policies)} "
                        f"{'policy' if len(policies) == 1 else 'policies'} in this namespace cover the rest"
                    ),
                    "severity": "major",
                    "impact": _IMPACT_NETPOL_PARTIAL,
                }
            )
    return hits


def _is_live_pod(pod: dict) -> bool:
    """A finished Job pod is not an exposure and its name is pure churn."""
    return str(pod.get("phase") or "") not in ("Succeeded", "Failed")


# A ReplicaSet is always named `<deployment>-<pod-template-hash>` and a
# CronJob's Job `<cronjob>-<timestamp>`. Neither intermediate object is in
# `COMPLIANCE_DUMP_KINDS`, so the parent is derived by dropping that last
# segment and then confirmed against the dump rather than assumed.
_OWNER_PARENT_KIND = {"ReplicaSet": "Deployment", "Job": "CronJob"}


def _controller_refs(meta: dict) -> list[dict]:
    """Owner references, the controlling one first.

    A pod may carry several; at most one is the controller, and that is the
    object that recreates it and the object whose template a fix has to edit.
    The rest are kept behind it rather than dropped, because an owner set with
    no `controller: true` at all is legal and still names something better than
    the pod.
    """
    refs = (meta.get("ownerReferences") or [])
    return [
        {"kind": o.get("kind", ""), "name": o.get("name", "")}
        for o in sorted(refs, key=lambda o: not o.get("controller"))
    ]


def _pod_workload_ref(pod: dict, workloads: list[dict]) -> str:
    """`Kind/name` of an object the reader can actually `kubectl get`.

    Not the `app.kubernetes.io/name` label, which this used to return. A label
    value is not a name and nothing requires it to match one: on this fleet
    `Deployment/kube-agents-controller-manager` carries
    `app.kubernetes.io/name: kube-agents-operator`, and the 2026-09-05
    compliance report named `kube-agents-operator` as the uncovered workload in
    `kubeagents-system`. No object anywhere in the cluster has that name, so
    the one actionable string in the finding resolved to `NotFound`.

    Nor the pod's own name, which the label was reached for to avoid -- it
    carries a ReplicaSet hash and a random suffix, and this excerpt is
    rewritten on every rollout if it moves. `ownerReferences` gives both
    properties at once: the controller behind the pod is a real object and its
    name is stable across restarts. Where the walk cannot land on something the
    dump confirms, it degrades to the pod, which is at least a thing that
    exists.
    """
    ns = pod.get("ns", "")
    declared = {(w.get("kind"), w.get("ns"), w.get("name")) for w in workloads}
    for owner in pod.get("owners") or []:
        kind, name = owner.get("kind", ""), owner.get("name", "")
        if not kind or not name:
            continue
        parent = _OWNER_PARENT_KIND.get(kind)
        if parent and (parent, ns, name.rpartition("-")[0]) in declared:
            return f"{parent}/{name.rpartition('-')[0]}"
        return f"{kind}/{name}"
    return f"Pod/{pod.get('name') or ''}"


def _enforces_ingress(policy: dict) -> bool:
    """`policyTypes` is optional. Kubernetes derives it from which rule blocks
    are present, and an empty spec derives to `["Ingress"]` -- a deny-all. So
    absent means ingress is enforced unless the policy is egress-only.
    """
    spec = policy.get("spec") or {}
    declared = spec.get("policyTypes")
    if declared:
        return "Ingress" in declared
    return not (spec.get("egress") and "ingress" not in spec)


def _pods_no_policy_selects(pods: list[dict], ns: str, policies: list[dict]) -> list[dict]:
    ingress_policies = [p for p in policies if _enforces_ingress(p)]
    out = []
    for pod in pods:
        if pod.get("ns") != ns or not _is_live_pod(pod):
            continue
        labels = pod.get("labels") or {}
        if not any(selector_matches((p.get("spec") or {}).get("podSelector") or {}, labels) for p in ingress_policies):
            out.append(pod)
    return out


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
                # A cluster check that nonetheless names a workload: it walks
                # `context["workloads"]` itself rather than being called once
                # per workload, because the ServiceAccount half of the test is
                # a namespace fact. The record carries the same field the
                # workload checks read.
                "reconciler": wl.get("reconciler"),
            }
        )
    return hits


def _cluster_object(context: dict) -> str:
    """`Cluster/<name>` for a finding whose object is the cluster itself.

    A bare `Cluster` was what these two checks emitted until 2026-08-29, and it
    is not a name: the finding id derives from `object`, so the compliance
    stream reported the same four public control planes as resolved and
    re-opened them as new the day the collector started supplying the string.
    `audit_report.validate_findings` now refuses a bare kind outright; this is
    the other half, so the collector never asks for the refusal.
    """
    name = str(context.get("cluster_name") or "").strip()
    if not name:
        raise GateFailure(
            "the collector context carries no cluster_name, so a cluster-scoped "
            "finding cannot name its object"
        )
    return f"Cluster/{name}"


def check_workload_identity_off(context: dict) -> list[dict]:
    describe = context.get("cluster_describe") or {}
    pool = ((describe.get("workloadIdentityConfig") or {}).get("workloadPool") or "").strip()
    if pool:
        return []
    return [{"namespace": "", "object": _cluster_object(context), "excerpt": "workloadIdentityConfig.workloadPool is empty"}]


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


def _is_default_route(cidr: str) -> bool:
    """Whether this CIDR admits every address of its family.

    A prefix length of zero rather than a string match, so `::/0` is caught
    beside `0.0.0.0/0`. An allowlist is only worth the name if something is
    outside it, and a dual-stack cluster can write the v6 default route into a
    field where only the v4 one was ever recognised.
    """
    try:
        return ipaddress.ip_network(cidr.strip(), strict=False).prefixlen == 0
    except ValueError:
        return False


def _grants_google_cloud_access(cfg: dict) -> bool:
    """Whether this authorized-networks config excepts Google Cloud's own public IPs.

    `gcpPublicCidrsAccessEnabled` (the `--enable-google-cloud-access` flag)
    admits every external address Google Cloud owns, on top of whatever
    `cidrBlocks` says. Absent reads as granted, because the API default is on:
    `gcloud container clusters update --enable-master-authorized-networks
    --master-authorized-networks=<CIDR>` leaves the field `true` on a cluster
    that never mentioned it, which was confirmed against a live cluster rather
    than inferred. Reading absent as `false` would call that cluster narrowed.
    """
    return cfg.get("gcpPublicCidrsAccessEnabled") is not False


def _has_restrictive_authorized_networks(describe: dict) -> bool:
    """Whether some authorized-networks surface narrows control-plane access.

    GKE carries the config on either `masterAuthorizedNetworksConfig` or
    `controlPlaneEndpointsConfig.ipEndpointsConfig.authorizedNetworksConfig` and
    rejects a cluster that sets both, so reading one field alone calls a cluster
    restricted through the other wide open. `cidrBlocks` holds CidrBlock objects
    (`{displayName, cidrBlock}`), never bare strings, so the allow-all entry is
    matched on the field rather than by membership in the list.

    Google Cloud access disqualifies a config however narrow its `cidrBlocks`
    are. The addresses it admits are not Google's own infrastructure -- they are
    the external IPs of every Compute Engine VM, Cloud Run service and Cloud
    Function in every Google Cloud project, so an attacker rents one and is
    inside the allowlist. That is not a narrowing worth suppressing a
    control-plane-exposure finding over, and suppressing on it was the worse
    half of a pair: the published remediation for that finding did not turn the
    grant off, so acting on the finding cleared it while leaving the endpoint
    reachable from anywhere a VM can be started.

    An enabled config with no blocks at all is caught by the same rule rather
    than by one of its own. It used to be read as the strict end of the setting
    -- shut to everything but "Google's own access" -- which had the sense of
    the field backwards.
    """
    ip_cfg = (describe.get("controlPlaneEndpointsConfig") or {}).get("ipEndpointsConfig") or {}
    for cfg in (describe.get("masterAuthorizedNetworksConfig"), ip_cfg.get("authorizedNetworksConfig")):
        cfg = cfg or {}
        if cfg.get("enabled") is not True or _grants_google_cloud_access(cfg):
            continue
        blocks = [b.get("cidrBlock") if isinstance(b, dict) else b for b in (cfg.get("cidrBlocks") or [])]
        if not any(_is_default_route(str(b or "")) for b in blocks):
            return True
    return False


def _json_scalar(value: object) -> str:
    """A field's value the way the `gcloud … --format=json` output spelled it.

    An excerpt quotes a JSON read, so `true` belongs there rather than Python's
    `True`, and a field GKE omitted has to read as omitted rather than as the
    `False` a `.get()` default would put in its place -- absent and `false` are
    different states on every field this renders.
    """
    if value is None:
        return "absent"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _authorized_networks_excerpt(describe: dict) -> str:
    """Both authorized-networks surfaces, as read, for the finding's excerpt.

    Named even when empty. GKE carries the config on one surface or the other
    and returns the unused one as `{}`, so a reader who sees only the populated
    field cannot tell a cluster that left the feature off from one this check
    forgot to look at. `gcpPublicCidrsAccessEnabled` is rendered where GKE set
    it because it is the field that distinguishes clusters this check otherwise
    grades identically -- it is what GKE writes into an otherwise-empty
    `masterAuthorizedNetworksConfig` on a cluster that never enabled the
    feature.

    Which way that field cuts depends on `enabled`, and the excerpt says which,
    because the reader's next move differs. With `enabled` off the grant is
    inert: every address already reaches the endpoint, and excepting some of
    them from an allowlist nobody is applying adds nothing. With `enabled` on
    it is the finding -- the `cidrBlocks` beside it can be a single `/32` and
    the endpoint still answers every Compute Engine VM on Google Cloud. That
    case is annotated even where GKE returned no field at all, because the
    default is on and a reader who sees a tight `cidrBlocks` with no mention of
    the grant concludes the opposite of the truth.
    """
    ip_cfg = (describe.get("controlPlaneEndpointsConfig") or {}).get("ipEndpointsConfig") or {}
    parts: list[str] = []
    for label, cfg in (
        ("masterAuthorizedNetworksConfig", describe.get("masterAuthorizedNetworksConfig")),
        ("ipEndpointsConfig.authorizedNetworksConfig", ip_cfg.get("authorizedNetworksConfig")),
    ):
        cfg = cfg or {}
        blocks = [str(b.get("cidrBlock") if isinstance(b, dict) else b) for b in (cfg.get("cidrBlocks") or [])]
        part = f"{label}.enabled={_json_scalar(cfg.get('enabled'))}, cidrBlocks=[{','.join(blocks)}]"
        live = cfg.get("enabled") is True and _grants_google_cloud_access(cfg)
        if cfg.get("gcpPublicCidrsAccessEnabled") is not None or live:
            part += f", gcpPublicCidrsAccessEnabled={_json_scalar(cfg.get('gcpPublicCidrsAccessEnabled'))}"
            if live:
                part += " (admits every Google Cloud external IP on top of cidrBlocks)"
            elif cfg.get("enabled") is not True:
                part += " (inert: authorized networks not enabled)"
        parts.append(part)
    return "; ".join(parts)


def _external_control_plane_paths(describe: dict) -> list[str]:
    """Every way the control plane answers from outside the VPC, as read.

    Two independent endpoints, and authorized networks gates only one of them.
    The IP endpoint is the one this check was written for. The DNS endpoint is
    a separate address (`gke-<hash>.<region>.gke.goog`) that GKE serves when
    `dnsEndpointConfig.allowExternalTraffic` is set, and no IP allowlist
    applies to it at all -- it is gated by IAM alone, so enabling authorized
    networks does not close it and a reader who acts on this finding would be
    left with the cluster still reachable.

    `ipEndpointsConfig.enabled` is the master switch under which
    `enablePublicEndpoint` sits. A cluster created with `--no-enable-ip-access`
    serves no IP endpoint at all, and reading `enablePublicEndpoint` alone
    calls it internet-reachable over an address it does not have. That
    combination does not exist on this fleet, so this is a false positive the
    check has not made yet rather than one it made.
    """
    endpoints = describe.get("controlPlaneEndpointsConfig") or {}
    ip_cfg = endpoints.get("ipEndpointsConfig") or {}
    private_cfg = describe.get("privateClusterConfig") or {}
    paths = []
    if ip_cfg.get("enabled") is False:
        pass  # No IP endpoint at all; `enablePublicEndpoint` below it is moot.
    elif ip_cfg.get("enablePublicEndpoint") is not None:
        if ip_cfg.get("enablePublicEndpoint") is True:
            paths.append(
                "controlPlaneEndpointsConfig.ipEndpointsConfig.enablePublicEndpoint="
                f"{_json_scalar(ip_cfg.get('enablePublicEndpoint'))}"
            )
    elif private_cfg.get("enablePrivateEndpoint") is not True:
        # The legacy inversion, read only where GKE returns no current field.
        paths.append(
            "privateClusterConfig.enablePrivateEndpoint="
            f"{_json_scalar(private_cfg.get('enablePrivateEndpoint'))}"
        )
    dns_cfg = endpoints.get("dnsEndpointConfig") or {}
    if dns_cfg.get("allowExternalTraffic") is True:
        paths.append(
            "controlPlaneEndpointsConfig.dnsEndpointConfig.allowExternalTraffic=true "
            "(DNS endpoint, not gated by authorized networks)"
        )
    return paths


def _allowlisted_but_for_google_cloud(describe: dict) -> bool:
    """Whether an allowlist is configured and Google Cloud access is what defeats it.

    Distinguishes the cluster that never enabled authorized networks from the
    one that enabled them and left the grant on. Both reach the internet, but
    only the second has an operator who believes the endpoint is closed, and
    the sentence each needs is different.
    """
    ip_cfg = (describe.get("controlPlaneEndpointsConfig") or {}).get("ipEndpointsConfig") or {}
    for cfg in (describe.get("masterAuthorizedNetworksConfig"), ip_cfg.get("authorizedNetworksConfig")):
        cfg = cfg or {}
        if cfg.get("enabled") is not True or not _grants_google_cloud_access(cfg):
            continue
        blocks = [b.get("cidrBlock") if isinstance(b, dict) else b for b in (cfg.get("cidrBlocks") or [])]
        if not any(_is_default_route(str(b or "")) for b in blocks):
            return True
    return False


_IMPACT_PUBLIC_IP_ENDPOINT = (
    "The cluster's API server accepts connections from any address on the "
    "internet; credential compromise or an API-server CVE is directly "
    "exploitable from outside the network."
)
# Separated from the sentence above because the operator here has already done
# the thing that sentence would prompt -- turned authorized networks on -- and
# telling them the endpoint answers the whole internet reads as a check that
# did not notice. The exposure is narrower and the remaining work is a
# different flag, so the finding has to say which.
_IMPACT_GOOGLE_CLOUD_ACCESS = (
    "Authorized networks is enabled on the cluster's public IP endpoint, but "
    "gcpPublicCidrsAccessEnabled excepts Google Cloud's external addresses "
    "from the allowlist — those of every Compute Engine VM, Cloud Run service "
    "and Cloud Function in every Google Cloud project, not this project's "
    "alone. An attacker starts a VM anywhere on Google Cloud and is inside the "
    "allowlist, so the allowlist narrows reachability to whoever can create a "
    "Google Cloud resource rather than closing it."
)
# The two endpoints are not the same exposure, and saying so overstates the
# one that is left. Reaching the DNS endpoint costs an attacker a Google
# identity carrying `container.clusters.connect` before a single byte reaches
# the API server, where the IP endpoint puts the server itself on the internet.
# Still a finding: authorized networks is the control an operator reaches for
# here and it does not apply, so the sentence has to say what does.
_IMPACT_PUBLIC_DNS_ENDPOINT = (
    "The IP endpoint is allowlisted, but the cluster also serves a DNS "
    "endpoint that resolves and answers from any address on the internet. "
    "Authorized networks do not gate it — IAM does, so reaching the API "
    "server needs a Google identity holding container.clusters.connect, and "
    "the exposure is that identity's blast radius rather than an unauthenticated "
    "API server. Widening the authorized-network list, or narrowing it, changes "
    "nothing about this path."
)


def check_public_control_plane(context: dict) -> list[dict]:
    """Whether the API server answers from the internet.

    Two generations of the same setting, and only one of them is authoritative
    on any given cluster. `controlPlaneEndpointsConfig.ipEndpointsConfig.
    enablePublicEndpoint` is the current field and says outright whether the
    public endpoint is served; `privateClusterConfig.enablePrivateEndpoint` is
    the legacy inversion of it, and GKE keeps returning that block for
    compatibility with only the addresses filled in. Reading them as an `or`
    took the union of two readings of the same fact: a cluster that turned the
    public endpoint off the current way, and so carries no legacy
    `enablePrivateEndpoint: true`, was reported as reachable from the internet
    at `critical`. Prefer the current field wherever GKE returns it and fall
    back to the legacy one only when it does not.

    Authorized networks answers for the IP endpoint and for nothing else, so it
    suppresses that path rather than the whole finding. A cluster whose IP
    endpoint is allowlisted and whose DNS endpoint takes external traffic is
    still answering the internet, and returning nothing for it told the
    operator the opposite -- the one shape where this check's silence was a
    false negative rather than a pass.

    An allowlist that excepts Google Cloud's own external addresses does not
    suppress the IP endpoint either, and that is the shape the fleet's own
    remediation used to produce: `--enable-master-authorized-networks
    --master-authorized-networks=<CIDR>` leaves `gcpPublicCidrsAccessEnabled`
    on, so acting on this finding closed it while the endpoint still answered
    every VM on Google Cloud. See `_has_restrictive_authorized_networks`.
    """
    describe = context.get("cluster_describe") or {}
    paths = _external_control_plane_paths(describe)
    if _has_restrictive_authorized_networks(describe):
        paths = [p for p in paths if "dnsEndpointConfig" in p]
    if not paths:
        return []
    dns_only = all("dnsEndpointConfig" in path for path in paths)
    impact = _IMPACT_PUBLIC_IP_ENDPOINT
    if dns_only:
        impact = _IMPACT_PUBLIC_DNS_ENDPOINT
    elif _allowlisted_but_for_google_cloud(describe):
        impact = _IMPACT_GOOGLE_CLOUD_ACCESS
    decided = "; ".join(paths)
    # Name the fields and the values, not the conclusion. `adopt_collector_evidence`
    # overwrites the model's excerpt with this string, so it is the only evidence
    # the finding will ever carry, and the constant sentence it used to be --
    # "public endpoint reachable with no restrictive authorized networks" -- was
    # byte-identical on all sixteen clusters of this fleet. That is unfalsifiable
    # by a reader and it hid a real difference: a cluster serving the endpoint
    # through the current field with `gcpPublicCidrsAccessEnabled` set read the
    # same as one caught by the legacy inversion with the whole config absent.
    excerpt = f"{decided}; {_authorized_networks_excerpt(describe)}"
    return [
        {
            "namespace": "",
            "object": _cluster_object(context),
            "excerpt": excerpt,
            "impact": impact,
        }
    ]


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
        # Which of the three fired, not just that one did. This check reads
        # three independent settings and a bare container name throws away the
        # only part a reader needs: the fix for `runAsUser=0` is not the fix
        # for a missing seccomp profile. `audit_report.py`'s
        # `adopt_collector_evidence` cites "a full securityContext breakdown
        # [coming back] as `containers: litellm-container`" as the detail loss
        # it exists to stop -- and then publishes this excerpt over the model's,
        # so the collector has to be the one carrying the detail.
        # `allowPrivilegeEscalation` and `capabilities` have no pod-level
        # fallback to read: `PodSecurityContext` carries neither field, so the
        # container's own value is the only one there is.
        allow_escalation = c_sc.get("allowPrivilegeEscalation")
        dropped = [str(cap).upper() for cap in ((c_sc.get("capabilities") or {}).get("drop") or [])]
        reasons = []
        if non_root is not True:
            reasons.append(f"runAsNonRoot={json.dumps(non_root)}")
        if run_as_user == 0:
            reasons.append("runAsUser=0")
        if seccomp_type not in ("RuntimeDefault", "Localhost"):
            reasons.append(f"seccompProfile.type={seccomp_type or 'absent'}")
        if allow_escalation is not False:
            reasons.append(f"allowPrivilegeEscalation={json.dumps(allow_escalation)}")
        if "ALL" not in dropped:
            reasons.append(f"capabilities.drop={json.dumps(dropped)}")
        if reasons:
            bad.append(f"{container.get('name', '')} ({', '.join(reasons)})")
    if not bad:
        return None
    return {"object": f"{workload['kind']}/{workload['name']}", "excerpt": f"containers: {'; '.join(bad)}"}


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

# Serving runtimes only. A name earns a place here by being a process that
# loads a model and answers inference requests, and by being distinctive enough
# that the `(^|/)`/`([-:@/]|$)` anchors cannot land it on something else. Two
# rejected candidates say where the line is: `litellm` is a proxy in front of
# model servers rather than one itself, so it holds no weights and belongs to
# the credential prong below instead; `nim` would match `nimlang/nim:2.0` under
# these anchors, and NVIDIA's NIM containers request a GPU anyway, so the
# accelerator prong already has them. `tei` is left off for the same reason
# `nim` is -- three letters, no distinctiveness -- while its full name is
# spelled out.
AI_MODEL_IMAGE_RE = re.compile(
    r"(^|/)(vllm|sglang|text-generation-inference|tgi|text-embeddings-inference|"
    r"tritonserver|torchserve|tensorflow-serving|kserve|ollama|ray|llama|mlserver|"
    r"seldon|lorax|aibrix|lmdeploy|xinference|localai|openllm)([-:@/]|$)"
)
AI_ACCELERATOR_KEY_RE = re.compile(r"nvidia\.com/gpu|google\.com/tpu")

# The third prong, and the only one that does not depend on recognising a name.
# An image allowlist is never finished, and the gap is not hypothetical: the
# `_EMPTY_SCOPE_REASON` comment further down names this stream's real exposure
# as a model server whose runtime nobody has listed, serving on CPU so no
# accelerator gives it away, reported as a clean cluster. A container holding
# `OPENAI_API_KEY` is talking to a model provider whatever its image is called.
#
# This is what puts a model gateway in scope, which the image prong deliberately
# will not do. That prong is right that a gateway holds no weights, and wrong
# about the consequence: of the six checks it opens, four cannot fire on a
# weightless workload at all -- 3.3 needs a CSI or PVC volume, 3.2 a
# `trust_remote_code` directive, 3.4 a `--model` flag or a plaintext artifact
# URL, 3.6 a floating tag. The two that remain are the two worth asking about a
# gateway, because a gateway is defined by holding provider credentials on
# behalf of everything behind it: whether its endpoint is public (3.1) and
# whether those credentials are literals (3.5). This fleet's `litellm` answers
# no to both -- ClusterIP, three `secretKeyRef`s -- which is a verdict the
# stream could not previously reach.
#
# Whole-name and named-provider only, which is stricter than §3.5's name rule
# below and deliberately so. Admitting a workload here subjects it to all six
# checks, including being asked whether its endpoint is a public *inference*
# endpoint; `(MODEL|REGISTRY|INFERENCE).*KEY` is a fair heuristic once a
# workload is known to serve models and a bad one for deciding that it does.
# §3.5 unions the two, so a provider named only here is still read for a
# plaintext value.
#
# `envFrom` is deliberately not read. The credential's name lives in the Secret
# rather than in the pod spec, so all a pod spec can say is "this container has
# some envFrom" -- true on this fleet of `ip-masq-agent` on five clusters and of
# no model server anywhere.
AI_PROVIDER_CREDENTIAL_ENV_RE = re.compile(
    r"^(OPENAI|ANTHROPIC|GEMINI|COHERE|MISTRAL|TOGETHER|GROQ|PERPLEXITY|DEEPSEEK|"
    r"FIREWORKS|ANYSCALE|AZURE_OPENAI|VERTEXAI|XAI|WANDB)_API_KEY$"
    r"|^(HF|HUGGING_FACE_HUB|HUGGINGFACE)_(TOKEN|API_TOKEN)$"
    r"|^REPLICATE_API_TOKEN$",
    re.IGNORECASE,
)


def _is_ai_workload(spec: dict) -> bool:
    containers = spec.get("containers") or []
    if any(AI_MODEL_IMAGE_RE.search(c.get("image") or "") for c in containers):
        return True
    for c in containers:
        limits = (c.get("resources") or {}).get("limits") or {}
        if any(AI_ACCELERATOR_KEY_RE.search(key) for key in limits):
            return True
    for c in containers:
        # Named, not valued: a `secretKeyRef` is the correct way to hold one of
        # these and still means the workload holds it. Whether the value is a
        # literal is 3.5's question, and answering it here would put exactly the
        # workloads that got it right out of the audit's reach.
        if any(AI_PROVIDER_CREDENTIAL_ENV_RE.search(e.get("name") or "") for e in c.get("env") or []):
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
        out.append({
            "kind": item["kind"], "ns": ns, "name": meta.get("name", ""), "spec": spec,
            "lbl": _pod_template_labels_of(item), "suspended": _is_suspended_cronjob(item),
            "scaled_to_zero": _is_scaled_to_zero(item),
            "reconciler": reconciler_of(meta),
        })
    return out


def _ai_containers(spec: dict) -> list[dict]:
    return (spec.get("containers") or []) + (spec.get("initContainers") or [])


# Programs whose inline-code flag turns the token after it into a command line
# (`sh`, and the shells that accept `-c`), and programs where it turns that token
# into source in some other language (`python -c`, `node -e`). The distinction
# decides what the tokens are worth reading as, so the two lists stay separate.
_INLINE_CODE_SHELLS = ("sh", "bash", "ash", "dash", "zsh", "ksh")
_INLINE_CODE_INTERPRETERS = ("python", "python2", "python3", "node", "nodejs", "perl", "ruby")
# `-c`, and the bundles a shell is routinely invoked with: `-ec`, `-lc`, `-exc`.
_SHELL_INLINE_FLAG_RE = re.compile(r"^-[a-z]*c$")
_INTERPRETER_INLINE_FLAG_RE = re.compile(r"^-(c|e)$")


def _container_argv(container: dict) -> list[str]:
    """`command` then `args`, the order the kubelet hands them to the process.

    `command` overrides the image's ENTRYPOINT and `args` its CMD, so this is the
    process's real argv. The order is load-bearing for anything reading a flag's
    value out of the next token: a `--model` ending `command` takes its value from
    the first element of `args`, and the reversed concatenation this replaced
    reported the flag with nothing beside it.
    """
    return [str(t) for t in (container.get("command") or [])] + [
        str(t) for t in (container.get("args") or [])
    ]


class _Argv(NamedTuple):
    """How one container's `command`/`args` resolve for the checks that read them.

    `tokens` is everything worth searching for a literal; `flags` is the subset a
    flag parser actually receives, which is empty when nothing in the container
    parses flags at all. See `_resolve_argv`.
    """

    tokens: list[str]
    flags: list[str]


def _resolve_argv(container: dict) -> _Argv:
    """The tokens a flag-parsing model loader in this container actually receives.

    A check that reads `--model` out of a manifest is claiming some argument
    parser will act on it, and `command:` is what decides whether one ever sees
    it. Three shapes, and only the third differs from the flat token list:

    * **No `command:`** -- the image's own ENTRYPOINT is the model server and
      `args` are the flags it parses. Every workload that runs its image the way
      the publisher shipped it, including this fleet's `ai-embeddings-tei`.
    * **A `command:` naming a program** -- `["python3", "-m", "vllm.entrypoints.
      openai.api_server"]`, `["text-generation-launcher"]`. The program parses the
      rest, so the whole argv counts.
    * **A `command:` invoking inline code** -- everything after the code string is
      a positional argument to that inline program (`$0`, `$1`, `sys.argv[1:]`),
      not a flag. Under `sh -c` the code string *is* a command line, so its words
      are what a parser sees and are returned in place of the positionals -- which
      also finds the `--model` in `sh -c "vllm serve --model foo"` that a
      token-prefix match cannot see, because no single token starts with it. Under
      `python -c` or `node -e` the code string is source in another language and
      nothing here parses flags at all, so this returns nothing.

    That last case is not hypothetical. On 2026-09-06 this fleet published
    `CronJob/ai-batch-finetune` as a `major` 3.4 finding reading `--model
    meta-llama/Llama-3.1-8B-Instruct with no --revision`, against a container whose
    entrypoint is `python3 -c "print('fixture; never scheduled')"`. The flag lands
    in `sys.argv` beside a print statement, no model is fetched at any point, the
    published impact ("the bytes that arrive at the next pod restart are whatever
    the source serves then") describes something that cannot happen, and the
    prescribed fix -- add `--revision <sha>` beside it -- changes nothing.

    3.2 deliberately does not use this. Its predicate is the textual presence of a
    trust directive anywhere the container carries one, and `trust_remote_code=True`
    written inside a `python -c` one-liner is a real instruction to the loader, not
    an inert flag. Different claim, different token set -- so this returns both.
    """
    command = [str(t) for t in (container.get("command") or [])]
    args = [str(t) for t in (container.get("args") or [])]
    argv = command + args
    if not command:
        return _Argv(argv, args)
    program = command[0].rsplit("/", 1)[-1]
    shell = program in _INLINE_CODE_SHELLS
    if not shell and program not in _INLINE_CODE_INTERPRETERS:
        return _Argv(argv, argv)
    flag_re = _SHELL_INLINE_FLAG_RE if shell else _INTERPRETER_INLINE_FLAG_RE
    for i, token in enumerate(argv[1:], start=1):
        if not flag_re.fullmatch(token) or i + 1 >= len(argv):
            continue
        if not shell:
            return _Argv(argv, [])
        try:
            words = shlex.split(argv[i + 1])
        except ValueError:
            # Unbalanced quoting. Read nothing rather than guess at a split, for
            # the same reason the interpreter arm returns nothing: a wrong token
            # list is a finding about an argument the process never receives.
            return _Argv(argv, [])
        # The words replace the code string rather than joining it, so a URL
        # inside a `sh -c` line is reported once, as itself, instead of twice --
        # once more with the whole command line labelled "plaintext URL".
        return _Argv(argv[: i + 1] + words + argv[i + 2 :], words)
    return _Argv(argv, argv)


TRUST_REMOTE_CODE_ARG_RE = re.compile(r"trust[-_]remote[-_]code(?!=(0|false|no))", re.IGNORECASE)
TRUST_REMOTE_CODE_ENV_NAME_RE = re.compile(r"TRUST_REMOTE_CODE", re.IGNORECASE)


# Returns which setting trusts remote code, not merely that one does. The two
# arms take different fixes -- a token in `args` is removed from the command
# line, an env var from the container's `env` -- so a bare container name leaves
# the reader unable to check the recommendation against the evidence.
# `check_podsecurity_gaps` above carries the same reasoning at length, including
# why the collector rather than the model has to be the one holding the detail.
#
# The 2026-09-06 08:55Z report is the instance: evidence `containers: embeddings`
# under a recommendation to "remove the TRUST_REMOTE_CODE=true environment
# variable". The recommendation was right and nothing published alongside it
# said so. `str | None` rather than a reason list because the arms are not
# independent settings the way §2.4's five are -- either one alone is the whole
# finding, and the first one found is enough to act on.
def _container_trusts_remote_code(c: dict) -> str | None:
    for t in _resolve_argv(c).tokens:
        if TRUST_REMOTE_CODE_ARG_RE.search(t):
            return f"arg {t}"
    for e in c.get("env") or []:
        if TRUST_REMOTE_CODE_ENV_NAME_RE.search(e.get("name") or "") and str(e.get("value", "")).lower() in ("1", "true", "yes"):
            return f"env {e.get('name')}={e.get('value')}"
    return None


def check_model_remote_code_trusted(workload: dict, context: dict) -> dict | None:
    bad = []
    for c in _ai_containers(workload["spec"]):
        reason = _container_trusts_remote_code(c)
        if reason:
            bad.append(f"{c.get('name', '')} ({reason})")
    if not bad:
        return None
    return {"object": f"{workload['kind']}/{workload['name']}", "excerpt": f"containers: {', '.join(bad)}"}


def _env_paths_under(container: dict, mount_path: str) -> list[str]:
    """Env vars on this container whose literal value is a path inside `mount_path`.

    A container that keeps HOME, a cache directory, or a model directory inside the
    weights mount writes to that mount, so adding `readOnly: true` to it stops the
    process instead of hardening it -- ollama derives OLLAMA_MODELS=$HOME/.ollama/models
    and prunes it at every start, vLLM writes HF_HOME, and both exit on boot. The
    remediation for 3.3 only sees this finding's evidence, so the conflict has to be
    named here for it to move the write path out in the same change rather than
    flipping the flag blind.
    """
    if not mount_path:
        return []
    base = mount_path.rstrip("/")
    hits = []
    for e in container.get("env") or []:
        v = e.get("value")
        if not isinstance(v, str) or e.get("valueFrom") is not None:
            continue
        if v == base or v.startswith(base + "/"):
            hits.append(f"{e.get('name')}={v}")
    return hits


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
                entry = f"{c.get('name', '')}:{m.get('name')}:{m.get('mountPath')}"
                writers = _env_paths_under(c, m.get("mountPath"))
                if writers:
                    entry += " (container writes here: " + ", ".join(writers) + ")"
                bad.append(entry)
    if not bad:
        return None
    return {"object": f"{workload['kind']}/{workload['name']}", "excerpt": "; ".join(bad)}


AI_URL_RE = re.compile(r"(^|=)(http|ftp)://")
AI_MODEL_FLAG_RE = re.compile(r"^--model(-id)?(=|$)")
AI_REVISION_FLAG_RE = re.compile(r"^--revision(=|$)")
# Userinfo and query string are where a signed-URL token or a basic-auth
# password rides along, and this excerpt is published to a public GitHub
# issue. The scheme, host and path are the whole of what the finding needs.
AI_URL_CREDENTIAL_RE = re.compile(r"://[^/@\s]*@|\?\S*")


def _ai_safe_url(value: str) -> str:
    return AI_URL_CREDENTIAL_RE.sub(lambda m: "://" if m.group(0).endswith("@") else "?…", value)


def check_model_artifact_unpinned_source(workload: dict, context: dict) -> dict | None:
    bad = []
    escalate = False
    for c in _ai_containers(workload["spec"]):
        argv = _resolve_argv(c)
        env_vals = [str(e.get("value", "")) for e in (c.get("env") or [])]
        urls = list(dict.fromkeys(v for v in argv.tokens + env_vals if AI_URL_RE.search(v)))
        # The flag arm reads only what a flag parser in this container would see,
        # which is not always the whole argv -- see `_resolve_argv`. The URL arm
        # above stays on every token, because a plaintext fetch is a plaintext
        # fetch whoever issues it.
        flag_argv = argv.flags
        # `--model=x` carries its value; bare `--model x` leaves it in the next
        # argument, and reporting the flag without the model name names nothing.
        models = [
            a if "=" in a else " ".join(flag_argv[i : i + 2])
            for i, a in enumerate(flag_argv)
            if AI_MODEL_FLAG_RE.search(a)
        ]
        has_revision_flag = any(AI_REVISION_FLAG_RE.search(a) for a in flag_argv)
        # Name the value that tripped the check, not just the container it sat
        # in. The two conditions fail for different reasons and take different
        # fixes -- a plaintext URL wants a digest-addressed source, an
        # unpinned `--model` wants a `--revision` -- and an excerpt reading
        # `containers: inference` distinguishes neither, nor says what to go
        # and look at. Both can hold on one container, so both are reported.
        reasons = []
        if urls:
            reasons.append("plaintext URL " + ", ".join(_ai_safe_url(u) for u in urls[:3]))
        if models and not has_revision_flag:
            # Redacted for the same reason the URL clause above is: a `--model`
            # value is very often a URL, and this one reaches the same public
            # issue. Leaving a token here would have made the redaction on the
            # line above decorative -- the identical string arrives through
            # both clauses whenever a container passes its model as a URL.
            safe_models = [_ai_safe_url(m) for m in models[:3]]
            reasons.append(f"{', '.join(safe_models)} with no --revision")
        if reasons:
            bad.append(f"{c.get('name', '')}: {'; '.join(reasons)}")
            if _container_trusts_remote_code(c):
                escalate = True  # §3.4: escalates to critical alongside a 3.2 finding on the same container
    if not bad:
        return None
    hit = {"object": f"{workload['kind']}/{workload['name']}", "excerpt": " | ".join(bad)}
    if escalate:
        hit["severity"] = "critical"
    return hit


# Unioned with `AI_PROVIDER_CREDENTIAL_ENV_RE` at the call site rather than
# merged into it: that one is anchored whole-name because it decides scope,
# while the `(MODEL|REGISTRY|INFERENCE).*` heuristic here is safe only because
# everything reaching it is already a known AI workload. Keeping both means a
# provider that only the anchored list names -- `COHERE_API_KEY`,
# `MISTRAL_API_KEY` -- cannot admit a workload to the audit and then go unread
# for a plaintext value once it is in.
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


# The name rule says a literal is there; only the value says whether it is a
# secret. `HF_TOKEN=hf_EXAMPLE_PLACEHOLDER_NOT_A_REAL_TOKEN` -- shipped in this
# fleet's own ai-inference demo -- is the same shape to a name-only rule as a
# live Hugging Face token, so any fleet carrying example manifests turns this
# check into noise at `major`.
AI_CREDENTIAL_PLACEHOLDER_WORD_RE = re.compile(
    r"EXAMPLE|PLACEHOLDER|CHANGE|DUMMY|FAKE|SAMPLE|REDACTED|NOT_?A_?REAL|TODO|FIXME"
    r"|YOUR|HERE|TOKEN|KEY|SECRET|PASSWORD|PASS|CRED|API|VALUE|NONE|REAL|NOT|ME|X{4,}",
    re.IGNORECASE,
)
# `$(FOO)` is Kubernetes' own env expansion, `${FOO}` and `{{ FOO }}` a
# templater's: the literal holds a reference to a value kept elsewhere, which
# is the opposite of the thing this check is looking for. Closing delimiter
# required -- an unterminated `$(` expands to nothing, so treating it as a
# reference would let it swallow the secret that follows it.
AI_CREDENTIAL_REFERENCE_RE = re.compile(r"\$\([^)]*\)|\$\{[^}]*\}|\{\{.*?\}\}")
_WORD_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")
#: An opaque run this long is the secret itself. Below it, a fragment is
#: decoration -- the `hf` in `hf_EXAMPLE_...`, the `sk` in `sk-...`.
_OPAQUE_RUN = 4


def _reads_as_inert(value: str) -> bool:
    """Does the whole value read as a placeholder or an unexpanded reference?

    Whole value, not any part of it: a substring test downgrades
    ``sk-proj-Todo7x...`` because it contains "todo", and a DSN whose *host* is
    ``db.example.com`` because of the host, while its password is live. So
    strip the reference expressions, split what is left on punctuation, and
    require every remaining run of ``_OPAQUE_RUN`` characters or more to be a
    placeholder word. One high-entropy run is enough to fail the test, which is
    the safe direction -- failing it keeps the finding at ``major``.

    The cost is the other direction: ``Bearer ${TOKEN}`` and AWS's own
    ``AKIAIOSFODNN7EXAMPLE`` do not read as inert here, because "bearer" is not
    a placeholder word and the AWS key is a single unbroken run. Both stay at
    ``major``, which is a wrong severity on a real finding rather than a
    suppressed one.
    """
    residue = AI_CREDENTIAL_REFERENCE_RE.sub(" ", value)
    referenced = residue != value
    matched = False
    for token in _WORD_SPLIT_RE.split(residue):
        if not token:
            continue
        if AI_CREDENTIAL_PLACEHOLDER_WORD_RE.fullmatch(token):
            matched = True
        elif len(token) >= _OPAQUE_RUN:
            return False
    return matched or referenced


#: The inert arm's consequence. The `CheckSpec` default states a credential *is*
#: embedded and must be rotated, which is the one sentence §3.5 of the SOP names
#: as false here: this arm fired precisely because every value reads as a
#: placeholder, so asserting a live credential contradicts the reason the finding
#: is `minor` and sends the owner to rotate something that does not exist.
_IMPACT_CREDENTIAL_INERT = (
    "A variable named for a model-registry credential carries a literal value "
    "rather than a `secretKeyRef`. Every value reads as a placeholder, so "
    "probably nothing is exposed today -- but the workload is shaped so that the "
    "day a real token is pasted in, it lands in `kubectl describe` output and in "
    "Git with no further change."
)


# What a literal credential can be replaced with, read off a live object instead
# of invented: `<variable name>` -> `(secret name, secret key)`, for the
# variables some container in `namespace` already sources from a `secretKeyRef`.
#
# `optional: true` is required, and it is the whole safety argument. This audit
# cannot read Secrets -- RBAC denies `secrets.get` and `secrets.list` fleet-wide,
# which is what makes §5's "never paste a credential into an excerpt" true by
# construction rather than by instruction -- so it can never confirm that the
# Secret behind a reference exists. Under `optional: true` it does not have to: a
# missing Secret leaves the variable unset, which is exactly as harmless as the
# placeholder being replaced. Under `optional: false` a missing Secret is
# `CreateContainerConfigError`, and the audit would have stopped a running
# workload to fix a `minor` finding about a value that was never a credential.
#
# One namespace, because a Secret is namespaced and a reference across namespaces
# does not resolve. Disagreement is dropped rather than arbitrated: two containers
# naming different Secrets for one variable means the namespace has no single
# answer, and choosing between them would be this collector inventing the Secret
# name the SOP's §4 forbids it to invent.
def _namespace_secret_refs(namespace: str, workloads: list[dict]) -> dict[str, tuple[str, str]]:
    seen: dict[str, set[tuple[str, str]]] = {}
    for other in workloads:
        if other.get("ns") != namespace:
            continue
        for c in _ai_containers(other.get("spec") or {}):
            for e in c.get("env") or []:
                ref = ((e.get("valueFrom") or {}).get("secretKeyRef")) or {}
                name, key, var = ref.get("name"), ref.get("key"), e.get("name") or ""
                if not (ref.get("optional") and name and key and var):
                    continue
                seen.setdefault(var, set()).add((str(name), str(key)))
    return {var: refs.pop() for var, refs in seen.items() if len(refs) == 1}


def check_model_credential_plaintext_env(workload: dict, context: dict) -> dict | None:
    bad, inert = [], []
    for c in _ai_containers(workload["spec"]):
        for e in c.get("env") or []:
            name, value = e.get("name") or "", e.get("value")
            if (
                value
                and e.get("valueFrom") is None
                and (
                    AI_CREDENTIAL_ENV_NAME_RE.search(name)
                    or AI_PROVIDER_CREDENTIAL_ENV_RE.search(name)
                )
                and not AI_CREDENTIAL_ENV_NAME_SAFE_SUFFIX_RE.search(name)
            ):
                bad.append(f"{c.get('name', '')}:{name}")
                if _reads_as_inert(value):
                    inert.append(f"{c.get('name', '')}:{name}")
    if not bad:
        return None
    hit = {"object": f"{workload['kind']}/{workload['name']}", "excerpt": f"set with a literal value: {', '.join(bad)}"}
    # Downgraded, never dropped: looking like a placeholder is not proof of
    # being one, and suppressing a real credential is the worse error by far.
    # A mixed workload keeps `major` -- one live token is not made safe by the
    # placeholders beside it.
    if len(inert) == len(bad):
        hit["severity"] = "minor"
        # Both arms, or neither. Setting `severity` alone leaves `emit` falling
        # back to `spec.impact` -- the `major` sentence -- and leaves
        # `impact_authoritative` unset, so `adopt_arm_impact` never corrects it
        # and `carry_unchanged_findings` republishes it forever.
        hit["impact"] = _IMPACT_CREDENTIAL_INERT
        # What was measured, not what it proves. `adopt_collector_evidence`
        # forces this excerpt onto the finding but never copies `severity`, so
        # the sentence has to still read correctly under a `major` the model
        # kept -- and "not a live secret" beside `major` reads as the report
        # contradicting itself.
        hit["excerpt"] += "; every value matches this check's placeholder or unexpanded-reference patterns"
        # Named here so the model can promote the remediation to `manifest`.
        # Only on the inert arm: on the `major` arm a value may be a live token,
        # and swapping it for a reference to a Secret whose contents this audit
        # cannot read would silently change which credential the workload uses
        # -- or drop it. The placeholder this arm fired on has no such value to
        # preserve, so the swap can only improve the declaration.
        #
        # All or nothing across `bad`. A partial rewrite leaves the finding open
        # against the variables it did not reach, and §3.1's suppression of a
        # resize that leaves its own finding standing is the same rule.
        refs = _namespace_secret_refs(workload.get("ns") or "", context.get("ai_workloads") or [])
        wanted = {entry.split(":", 1)[1] for entry in bad}
        if wanted and wanted <= refs.keys():
            shape = ", ".join(
                f"{var} -> secretKeyRef name={refs[var][0]} key={refs[var][1]} optional=true"
                for var in sorted(wanted)
            )
            hit["excerpt"] += (
                f"; a container in namespace {workload.get('ns') or ''} already sources every one "
                f"of them from a Secret ({shape}), so copying that reference over these literals "
                f"fixes the finding without this audit creating a Secret or reading a value"
            )
    return hit


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


def _is_private_address(value: str) -> bool:
    """Is this load-balancer address unreachable from the public internet?

    A hostname is not resolved -- the collector makes no network calls -- so it
    counts as public. That keeps the finding on something unverifiable rather
    than dropping it, which is the safe direction for this check.
    """
    try:
        return ipaddress.ip_address(value).is_private
    except ValueError:
        return False


def _restricting_source_ranges(spec: dict) -> list[str]:
    """The Service's source-range allowlist, or ``[]`` if it restricts nothing.

    GKE programs ``loadBalancerSourceRanges`` into the firewall rule in front of
    the forwarding rule, so it is enforcement rather than intent. Three ways a
    populated field still restricts nothing, all of which return ``[]``: a
    default route (``0.0.0.0/0``, ``::/0``) admits the whole internet, an
    unparseable entry means the allowlist cannot be read at all, and an empty
    list was never a restriction. Erring towards "unrestricted" keeps the
    severity where it was.

    The unparseable case is why this does not simply call `_is_default_route`
    for everything: there, a CIDR that will not parse is "not the allow-all
    entry" and keeps the control-plane finding, which is that caller's safe
    direction. Here it is "cannot vouch for this allowlist", and the safe
    direction is the opposite one.
    """
    ranges = [r.strip() for r in (spec.get("loadBalancerSourceRanges") or []) if isinstance(r, str) and r.strip()]
    for r in ranges:
        try:
            ipaddress.ip_network(r, strict=False)
        except ValueError:
            return []
        if _is_default_route(r):
            return []
    return ranges


# Prefixes reserved for documentation and examples, which no host anywhere is
# ever assigned: RFC 5737's three IPv4 blocks, RFC 3849's IPv6 block, and RFC
# 6666's discard prefix. Deliberately *not* the RFC 1918 ranges -- those address
# real hosts, and an operator who allowlists one on an external load balancer may
# be admitting traffic that arrives over Interconnect or a VPN with its source
# intact. Only these can be asserted to admit nobody.
_DOCUMENTATION_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32", "100::/64")
)


def _is_documentation_range(cidr: str) -> bool:
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False
    return any(net.version == doc.version and net.subnet_of(doc) for doc in _DOCUMENTATION_NETWORKS)


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
        # An annotation records what was asked for; `status.loadBalancer` records
        # what was handed out, and it is already in the same dump. A Service whose
        # every assigned address is private is not reachable from the internet
        # whatever its annotations say, so calling it a public endpoint is just
        # wrong. With no address yet the load balancer is still provisioning and
        # the annotations are all there is -- the behaviour before this check
        # looked at status at all.
        # Both fields, not `ip or hostname`: an ingress entry may carry each,
        # and short-circuiting on `ip` would throw away a hostname that
        # `_is_private_address` treats as public. That is the one direction
        # that loses a finding.
        assigned = [
            addr
            for ing in ((svc.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
            for addr in (ing.get("ip") or "", ing.get("hostname") or "")
            if addr
        ]
        public = [addr for addr in assigned if not _is_private_address(addr)]
        # Dropped rather than downgraded, unlike the placeholder branch in
        # `check_model_credential_plaintext_env`. That one guesses at intent
        # from a value's shape and can be wrong about a live secret; this one
        # reads routability off the assigned address, and an RFC 1918 address
        # is not reachable from the internet whatever else is true. A hostname
        # never lands here -- `_is_private_address` calls it public -- so the
        # branch needs every entry to be an unambiguously private literal.
        if assigned and not public:
            continue
        hit = {
            "namespace": ns,
            "object": f"Service/{meta.get('name', '')}",
            # §3.5 is always `manual` -- the correct caller range is a fact
            # about intended usage this audit cannot read -- so the reader is
            # being told to go and edit something, and on 2026-09-06 that
            # something was a Service the Argo CD Application
            # `workloads-adamparco-gitops` reasserts on every sync.
            "reconciler": reconciler_of(meta),
            # The count, never the address. `ai_security_audit_sop.md`
            # (Red Lines, and again under check 3.5) forbids publishing
            # the address of a reachable model endpoint, and these
            # findings are filed as issues on a public repository --
            # writing one here would hand a reader the target. It is not
            # advisory either: `adopt_collector_evidence` overwrites the
            # model's excerpt with this string, so an SOP-compliant
            # excerpt would be replaced by whatever is written here.
            "excerpt": "type=LoadBalancer, no internal-LB annotation, selects an AI workload in this namespace"
            + (
                f"; {len(public)} assigned address{'es' if len(public) > 1 else ''}, none of them private"
                if public
                else ""
            ),
        }
        # Downgraded rather than dropped, the opposite of the private-address
        # branch above. A private address settles reachability outright; an
        # allowlist only bounds who reaches it, and a `/8` of public space
        # bounds very little. So the endpoint stays a finding and the severity
        # stops claiming what `impact` says of an unrestricted one -- that
        # anyone who finds the address can use it.
        #
        # Counts again, not the ranges themselves: which networks are trusted
        # is the other half of the target, and this goes in a public issue.
        ranges = _restricting_source_ranges(spec)
        if ranges:
            hit["severity"] = "major"
            # Set alongside the severity, never without it. `emit` derives
            # `impact_authoritative` from `hit["impact"]` alone, so an arm that
            # moved the severity and left the impact published the unrestricted
            # `critical` sentence -- "anyone who finds the address" -- on a
            # `major` finding, with nothing to stop the model rewriting it and
            # nothing for `adopt_arm_impact` to hold it to afterwards. That is
            # the flaw `test_a_downgraded_credential_candidate_is_impact_authoritative`
            # was written for on 3.5; this arm had it too.
            hit["impact"] = (
                "This model server is reachable from outside the cluster, bounded by the CIDR "
                "allowlist in spec.loadBalancerSourceRanges. Any caller inside an allowlisted "
                "range can send it inference traffic, consume its accelerator capacity, and probe "
                "whatever the model can reach."
            )
            hit["excerpt"] += (
                f"; loadBalancerSourceRanges admits {len(ranges)} CIDR{'s' if len(ranges) > 1 else ''}, not the whole internet"
            )
            # One more step down, for the same reason `major` is a step down
            # from `critical`: the severity should not claim an exposure the
            # allowlist rules out. Where every entry is a documentation prefix
            # no host is ever assigned, the allowlist admits nobody, and the
            # 2026-09-05 report nevertheless told an operator that "anyone
            # connecting from within that allowed range can send it inference
            # traffic, consume its accelerator capacity, and probe whatever the
            # model can reach" about `192.0.2.0/24`. Not dropped, unlike the
            # all-private-address branch above: that one settles reachability
            # from what the platform assigned, while this one is a Service
            # holding a public address and forwarding rule for callers who
            # cannot exist -- a real misconfiguration, just not an exposure.
            if all(_is_documentation_range(r) for r in ranges):
                hit["severity"] = "minor"
                hit["impact"] = (
                    "This Service holds a public load-balancer address and forwarding rule that no "
                    "caller can reach: every CIDR in its allowlist is a prefix reserved for "
                    "documentation, so no host is ever assigned one. Nothing is exposed. What is "
                    "wrong is the allowlist -- an example value left in place -- which means the "
                    "endpoint costs a public address and serves no one, and whoever set it "
                    "intended some real range."
                )
                hit["excerpt"] += (
                    "; every one of them is a reserved documentation prefix (RFC 5737/RFC 3849), "
                    "which no host is ever assigned -- the allowlist admits no caller at all, so "
                    "this endpoint holds a public address and reaches nobody"
                )
        hits.append(hit)
    return hits


def _kcc_ready_condition(item: dict) -> dict | None:
    """The `Ready` condition off a Config Connector object, if it has one."""
    status = item.get("status")
    conditions = status.get("conditions") if isinstance(status, dict) else None
    for condition in conditions or []:
        if isinstance(condition, dict) and condition.get("type") == KCC_READY_CONDITION:
            return condition
    return None


def check_kcc_object_wedged(context: dict) -> list[dict]:
    """Cluster-scoped: Config Connector objects stuck `Ready=False`.

    The audit reads GCP and reads the cluster. It never reads the thing that
    turns the GitOps repository into either of them, so a Config Connector that
    has stopped applying is invisible to every other check in this skill --
    while being the one condition that makes every `manifest` remediation the
    skill produces a no-op. `audit_report.disclose_stalled_kcc_remediations`
    covers the half where some finding's fix edits the stalled object's file.
    This covers the rest, which is most of it: nothing in the ledger has to
    point at an object for it to be wedged.

    Twice in two days on this fleet, and neither showed up anywhere a person
    was looking. On 2026-09-05 a `PubSubTopic` failed on a missing
    `roles/pubsub.editor` and took fifteen `ContainerCluster`s down with it. On
    2026-09-06 two `ComputeFirewall`s -- the merged remediation for a
    `firewall-world-open-ingress` critical -- retried a 403 every two minutes
    from 17:33 to 19:41 because the controller's service account had
    `compute.networkAdmin`, which grants the firewall read, and not
    `compute.securityAdmin`, which is where `compute.firewalls.update` lives.
    The rules stayed open to `0.0.0.0/0` on tcp:22 and tcp:3389 across fourteen
    nodes holding an external IP for four hours after the fix merged. Nothing
    crash-loops, no event lands outside the object, and the audit went on
    republishing the finding with no way to say why it was not clearing.

    One hit per *cause*. An object reporting `DependencyNotReady` is fine in
    itself and waiting on another object, so emitting one finding each would
    have turned that first incident into sixteen findings with one fix -- the
    flood this skill's `60k` body budget then spends on symptoms instead of
    causes. They are counted onto every cause's excerpt instead. The exception
    is a cluster where *every* unready object is a dependent: the cause is then
    not a Config Connector object at all (an external reference, or one deleted
    out from under its dependents), there is nothing else to name, and a
    `minor` hit each is the only way the reader hears about it.

    A dependent is only ever `DependencyNotReady`. `DependencyNotFound` reads
    like its neighbour and means the opposite thing for this check: the
    referent does not exist, so no other object here is going to be repaired
    into clearing it, and the object naming the missing reference *is* the
    cause. It stays a cause, and folding it in with the symptoms would drop the
    one object anybody can act on.
    """
    items = [i for i in context.get("kcc_objects") or [] if isinstance(i, dict)]
    causes: list[tuple[str, str, str, str]] = []  # kind, namespace, name, why
    blocked: list[tuple[str, str, str, str]] = []
    for item in items:
        ready = _kcc_ready_condition(item)
        # No condition yet means KCC has not reached the object, which is the
        # normal state for seconds after an apply and is not evidence of
        # anything. Only an explicit `False` is -- the same bar
        # `audit_report.kcc_resource_health` sets, for the same reason.
        if ready is None or str(ready.get("status")) != "False":
            continue
        meta = item.get("metadata") or {}
        kind = str(item.get("kind") or "")
        name = str(meta.get("name") or "")
        if not kind or not name:
            continue
        namespace = str(meta.get("namespace") or "")
        reason = str(ready.get("reason") or "").strip()
        message = str(ready.get("message") or "").strip()
        if reason and message:
            why = f"{reason}: {message}"
        else:
            why = reason or message or "no reason given"
        why = " ".join(why.split())[:MAX_KCC_WEDGE_MESSAGE_CHARS]
        entry = (kind, namespace, name, why)
        if reason == KCC_DEPENDENCY_NOT_READY_REASON:
            blocked.append(entry)
        else:
            causes.append(entry)

    if not causes and not blocked:
        return []

    def _tail(exclude: tuple[str, str, str, str] | None) -> str:
        others = [b for b in blocked if b != exclude]
        if not others:
            return ""
        named = ", ".join(f"{k}/{n}" for k, _, n, _ in sorted(others)[:MAX_KCC_BLOCKED_NAMED])
        if len(others) > MAX_KCC_BLOCKED_NAMED:
            named += f", +{len(others) - MAX_KCC_BLOCKED_NAMED} more"
        return (
            f"; {len(others)} other Config Connector object(s) here report "
            f"{KCC_DEPENDENCY_NOT_READY_REASON} behind an unready dependency ({named})"
        )

    symptoms_only = not causes
    hits = []
    for entry in sorted(causes if causes else blocked):
        kind, namespace, name, why = entry
        hit = {
            "namespace": namespace,
            "object": f"{kind}/{name}",
            "excerpt": (
                f"Config Connector reports {KCC_READY_CONDITION}=False on "
                f"{kind}/{name}: {why}" + _tail(entry)
            ),
        }
        if symptoms_only:
            # Symptom-only: see the docstring. Rated below a cause because the
            # object itself is correct and the fix is somewhere this read
            # cannot see, so the reader's first move is to find the dependency
            # rather than to edit this file.
            hit["severity"] = "minor"
            hit["impact"] = (
                "This Config Connector object is waiting on a dependency that is not itself a "
                "Config Connector object on this cluster, so nothing here declares the thing it "
                "needs. Its GCP resource holds whatever state it had when the reference broke, "
                "and any change merged to its manifest is not applied."
            )
        hits.append(hit)
    return hits


class CheckSpec(NamedTuple):
    slug: str
    kind: str  # "workload": run(workload, context) -> hit|None, one call per workload.
    #             "cluster": run(context) -> list[hit], one call per cluster.
    run: Callable
    severity: str  # A hit's own "severity" key overrides this (§3.4, §3.6, §3.7's two-condition checks).
    autopilot_severity: str | None  # None: severity is mode-independent
    impact: str  # A hit's own "impact" key overrides this (§3.1's non-BestEffort arms).


OBTAINABILITY_CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec(
        "no-requests",
        "workload",
        check_no_requests,
        "major",
        "minor",
        # Every arm of §3.1 sets its own, so this is unreachable in practice;
        # it stays as the BestEffort arm's own string rather than a fourth
        # sentence nothing produces.
        _IMPACT_BEST_EFFORT,
    ),
    CheckSpec(
        "no-memory-limit",
        "workload",
        check_no_memory_limit,
        "major",
        None,
        # Both arms of §3.2 set their own, so this is unreachable in practice.
        # It stays as the arm that describes a container with no memory request
        # -- the shape the fleet is actually made of -- rather than a third
        # sentence nothing produces.
        _IMPACT_NO_LIMIT_UNREQUESTED,
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
    CheckSpec(
        "schedule-never-succeeds",
        "cluster",
        check_schedule_never_succeeds,
        "major",
        None,
        "Whatever this schedule exists to do has not been done since its last "
        "successful run, and nothing reports that on its own: the CronJob is "
        "still listed, still enabled, and still firing on time.",
    ),
    CheckSpec(
        "rollout-drops-traffic",
        "workload",
        check_rollout_drops_traffic,
        "major",
        None,
        "Every rollout, node drain, and preemption resets the connections in "
        "flight to the terminating pod: the container is sent SIGTERM while "
        "the endpoint removal is still propagating, so requests arrive at a "
        "process that has already begun shutting down.",
    ),
    CheckSpec(
        "strategy-causes-downtime",
        "workload",
        check_strategy_causes_downtime,
        "major",
        None,
        "The update strategy permits every replica to be down at once, so a "
        "rollout is a full outage lasting until the first replacement pod "
        "passes its readiness probe -- an image pull plus a cold start.",
    ),
    CheckSpec(
        "cronjob-runs-overlap",
        "cluster",
        check_cronjob_runs_overlap,
        "major",
        None,
        "Runs of this schedule are piling up on each other: each period starts "
        "another copy without the last one having finished, so the load on "
        "whatever the job writes to grows until something there gives out.",
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
        # Every hit composes its own from the flags actually set, so this is
        # unreachable in practice; it stays as the arm that carries the check's
        # own severity default rather than a fourth sentence nothing produces.
        _IMPACT_HOST_NETWORK,
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
        "Subject holds write access to every resource in this scope — enough "
        "to read Secrets in any namespace it covers and rewrite a workload "
        "into a privileged pod. Wildcarding the verbs and enumerating them "
        "reach the same ceiling.",
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
        # Not "a credential it does not use": the check reads the SA reference
        # and the automount flag, and neither says whether the workload calls
        # the API server. A finding that asserts an unobservable is one an owner
        # can refute from memory, and refuting a true finding on a false clause
        # is how a whole audit stops being read.
        "Workload mounts an API-server credential by default rather than by "
        "request, handing an attacker who lands in the container an "
        "authenticated foothold for free.",
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
        # Both arms set their own, so this is unreachable in practice; it stays
        # as the IP-endpoint arm rather than a third sentence nothing produces.
        _IMPACT_PUBLIC_IP_ENDPOINT,
    ),
    CheckSpec(
        "podsecurity-gaps",
        "workload",
        check_podsecurity_gaps,
        "minor",
        None,
        "Containers miss the container-level settings the restricted Pod "
        "Security Standard requires: running as root, an unfiltered syscall "
        "surface, retained Linux capabilities, or privilege escalation left "
        "enabled. A runtime escape starts with capabilities to use rather "
        "than having to acquire them.",
    ),
    CheckSpec(
        "kcc-object-wedged",
        "cluster",
        check_kcc_object_wedged,
        "critical",
        None,
        # Overridden on the symptom-only arm; see check_kcc_object_wedged.
        "Config Connector has stopped applying this declaration. The GCP "
        "resource holds whatever state it drifted to, the repository says "
        "something else, and every pull request merged against this object -- "
        "including a remediation this audit proposes -- changes nothing on the "
        "cloud until the controller is unblocked.",
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
    dump_path, dump_run, gate_ok = dump_state(
        kubeconfig, cluster["name"], project=cluster["project"], location=cluster["location"], run=run
    )
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
    "kcc-object-wedged": ("kcc",),
}

# `kubectl get gcp` is the category Config Connector registers across every CRD
# it installs, so one read returns every object it owns -- rather than the
# ~200 `kubectl get <kind>` calls that enumerating the CRDs would cost.
KCC_CATEGORY = "gcp"

# The one read in this file that does not fit `DEFAULT_TIMEOUT_S`, because a
# category read is not one list call. `kubectl` expands `gcp` to every CRD
# carrying the category -- 221 of them on this fleet's hub -- and issues a list
# against each, so the cost scales with how much of Config Connector is
# installed rather than with how many objects exist. Measured from the agent
# pod against an idle hub holding 19 objects: 10-14 seconds.
#
# That fits inside 60 comfortably, which is why the check looked fine until it
# ran for real. The collector sweeps `MAX_WORKERS` clusters at once, and the
# fifteen without Config Connector answer this read in about a tenth of a
# second, so the hub is the only one that is still working when the other seven
# in its wave are hammering the same pod -- and on 2026-09-06, the first day
# the check was live, it went past 60 and was recorded as inapplicable. The
# check has never once executed against the only cluster it can apply to.
#
# 240 is four times the default and roughly twenty times the idle read. It is
# affordable because it is one read on one cluster: the other fifteen fail
# fast, so the worst case adds minutes to a sweep that already runs for half an
# hour, and only when something is genuinely wrong.
KCC_READ_TIMEOUT_S = 240

# §1's three node-facing checks Autopilot's admission controller rules out
# for in-scope workloads, with the SOP's own canonical reasons verbatim. On
# an Autopilot cluster these must never appear in the manifest's `commands`
# -- an agent that copies `commands` verbatim into `checks_run` (§2's
# instruction for every other check) and *also* follows §1's instruction to
# record these in `checks_not_applicable` would name the same slug in both
# lists, which the validator rejects.
#
# Each reason is scoped two ways on purpose, because the earlier wording
# ("privileged containers are rejected at admission and cannot exist here")
# was false on both counts. Privileged containers, host namespaces and
# hostPath mounts all run on this fleet's Autopilot clusters today -- they
# are Google's own system add-ons in kube-system, which S1 and S3 suppress,
# so "in-scope workloads" rather than a claim about the cluster. And
# admission rejecting them is a policy a WorkloadAllowlist can lift
# (`autogke-disallow-privilege`, `autogke-no-write-mode-hostpath`, GKE >=
# 1.32.0-gke.1000000), so the trailing clause is a fact the collector reads
# for -- see `autopilot_allowlists` -- rather than one it assumes.
_COMPLIANCE_AUTOPILOT_NOT_APPLICABLE: tuple[tuple[str, str], ...] = (
    (
        "privileged-container",
        "GKE Autopilot: admission rejects privileged: true and the SYS_ADMIN capability for "
        "in-scope workloads, and this cluster carries no WorkloadAllowlist that would exempt one.",
    ),
    (
        "host-namespace",
        "GKE Autopilot: admission rejects hostPID/hostIPC/hostNetwork for in-scope workloads, "
        "and this cluster carries no WorkloadAllowlist that would exempt one.",
    ),
    (
        "hostpath-mount",
        "GKE Autopilot: admission rejects write-mode hostPath for in-scope workloads and allows "
        "read access under /var/log alone, and this cluster carries no WorkloadAllowlist "
        "that would exempt one.",
    ),
)


def _collect_compliance(cluster: dict, kubeconfig: Path, checks: tuple[CheckSpec, ...], *, run: RunFn) -> CollectedContext:
    name, project, location = cluster["name"], cluster["project"], cluster["location"]
    commands: dict[str, dict] = {}
    # `cluster_name` is here for the two cluster-scoped checks, whose object is
    # the cluster itself and which are handed nothing else that names it.
    context: dict = {"workloads": [], "cluster_name": name}

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
    # Raw, from the same dump: `netpol-missing`'s exposure test asks whether a
    # namespace runs pods, which is not the same question as whether it holds
    # anything this audit is allowed to name.
    context["pod_namespaces"] = {
        (i.get("metadata") or {}).get("namespace", "")
        for i in parsed.get("items", []) or []
        if i.get("kind") == "Pod"
    }
    # The same pods again, with the labels kept. "Does this namespace hold a
    # NetworkPolicy" and "is this pod selected by one" are different questions,
    # and only the labels can answer the second. The owner refs answer a third:
    # which object a reader is supposed to go and fix, since a pod name is a
    # hash away from stable and a label value names nothing at all.
    context["pods"] = [
        {
            "ns": (i.get("metadata") or {}).get("namespace", ""),
            "name": (i.get("metadata") or {}).get("name", ""),
            "labels": (i.get("metadata") or {}).get("labels") or {},
            "phase": (i.get("status") or {}).get("phase", ""),
            "owners": _controller_refs(i.get("metadata") or {}),
        }
        for i in parsed.get("items", []) or []
        if i.get("kind") == "Pod"
    ]
    workload_record = _record(f"KUBECONFIG={kubeconfig} {shlex.join(workload_argv)}", result)
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
    record = _record(f"KUBECONFIG={kubeconfig} {shlex.join(rbac_argv)}", result)
    for slug in ("cluster-admin-binding", "wildcard-rbac"):
        commands[slug] = record

    netpol_argv = ["kubectl", "get", "netpol,ns", "-A", "-o", "json"]
    parsed, result = gated(netpol_argv)
    if parsed is None:
        raise GateFailure(f"NetworkPolicy/Namespace dump gate failed (rc={result.rc}): {result.stderr.strip()[:300]}")
    items = parsed.get("items", [])
    context["networkpolicies"] = [i for i in items if i.get("kind") == "NetworkPolicy"]
    context["namespaces"] = [i for i in items if i.get("kind") == "Namespace"]
    commands["netpol-missing"] = _record(f"KUBECONFIG={kubeconfig} {shlex.join(netpol_argv)}", result)

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

    # Ungated for the reason `ccnp` above is: Config Connector is installed on
    # the hub that reconciles the fleet's GCP resources and on nothing else, so
    # a failure here means "this cluster does not run KCC" on fifteen of this
    # fleet's sixteen clusters. Gating would fail every one of them closed on
    # the absence of a CRD they are not supposed to have. Absence is reported
    # as an inapplicable check rather than as a clean one -- a cluster with no
    # Config Connector has not cleared anything.
    kcc_argv = ["kubectl", "get", KCC_CATEGORY, "-A", "-o", "json"]
    kcc_parsed, kcc_result = run_and_gate(kcc_argv, kubeconfig, run=run, timeout=KCC_READ_TIMEOUT_S)
    context["kcc_objects"] = (kcc_parsed or {}).get("items") or []
    # `run_and_gate` returns `None` for four different things, and only one of
    # them means "no Config Connector here". Collapsing them would publish
    # "Config Connector is not installed on this cluster" as the audit's own
    # claim about a cluster where the read timed out or came back truncated --
    # a statement the collector cannot support, on the strength of a read that
    # failed. Each gets the reason that is true of it.
    kcc_reason = ""
    if kcc_parsed is not None and not context["kcc_objects"]:
        kcc_reason = (
            f"Config Connector is installed on this cluster -- `kubectl get "
            f"{KCC_CATEGORY} -A` was served -- but it declares no GCP "
            "resources, so there is nothing whose reconciliation could stall."
        )
    elif kcc_result.rc == TIMEOUT_RC:
        # Split out ahead of the `rc != 0` arm below, which is only entitled to
        # read an exit code as the API server's answer when the API server is
        # the one that produced it. A timeout is this collector giving up, and
        # the cluster it is most likely to give up on is the one where the read
        # has the most work to do -- which is the cluster that runs Config
        # Connector, the exact opposite of what the arm below would have said.
        kcc_reason = (
            f"Undetermined: `kubectl get {KCC_CATEGORY} -A` did not finish "
            f"within {KCC_READ_TIMEOUT_S}s and was killed, so whether Config "
            "Connector is installed here -- and whether anything it owns is "
            "stalled -- was not established. This check cleared nothing on "
            "this cluster."
        )
    elif kcc_result.rc != 0:
        kcc_reason = (
            f"Config Connector is not installed on this cluster: `kubectl get "
            f"{KCC_CATEGORY} -A` exited {kcc_result.rc}, which is what the API "
            "server answers for a resource type it does not serve. Nothing "
            "here declares a GCP resource. This fleet runs one Config "
            "Connector, on the hub cluster."
        )
    elif kcc_parsed is None:
        kcc_reason = (
            f"Undetermined: `kubectl get {KCC_CATEGORY} -A` exited 0 but "
            "returned output this collector could not read as a list of "
            "objects, so whether Config Connector is installed here -- and "
            "whether anything it owns is stalled -- was not established. This "
            "check cleared nothing on this cluster."
        )
    if kcc_reason:
        context.setdefault("not_applicable", {})["kcc-object-wedged"] = kcc_reason
    else:
        commands["kcc-object-wedged"] = _record(
            f"KUBECONFIG={kubeconfig} {shlex.join(kcc_argv)}", kcc_result
        )

    sa_argv = ["kubectl", "get", "sa", "-A", "--field-selector", "metadata.name=default", "-o", "json"]
    parsed, result = gated(sa_argv)
    if parsed is None:
        raise GateFailure(f"ServiceAccount dump gate failed (rc={result.rc}): {result.stderr.strip()[:300]}")
    context["serviceaccounts"] = parsed.get("items", [])
    commands["default-sa-automount"] = _record(f"KUBECONFIG={kubeconfig} {shlex.join(sa_argv)}", result)

    describe_argv = [
        "gcloud", "container", "clusters", "describe", name, "--location", location, "--project", project,
        "--format", "json(workloadIdentityConfig,privateClusterConfig,masterAuthorizedNetworksConfig,"
        "controlPlaneEndpointsConfig,nodePools[].name,nodePools[].config.workloadMetadataConfig)",
    ]
    parsed, result = gated(describe_argv)
    if parsed is None:
        raise GateFailure(f"cluster describe gate failed (rc={result.rc}): {result.stderr.strip()[:300]}")
    context["cluster_describe"] = parsed
    describe_command = shlex.join(describe_argv)
    describe_record = _record(describe_command, result)
    for slug in ("workload-identity-off", "public-control-plane"):
        commands[slug] = describe_record

    # Not attempted on Autopilot, where the API refuses it outright:
    # `node-pools list` answers HTTP 400 "Autopilot node pools cannot be
    # accessed or modified". Gating on that read anyway threw the whole
    # cluster away for the single check it backs. The table was simply
    # unreachable: the gate raises here, `collect_cluster` returns
    # `gate-failed`, and the not-applicable block that would have said so
    # never runs. Three of this fleet's four clusters were Autopilot, so every
    # daily `compliance-audit` collected one cluster and gate-failed the rest
    # -- eleven checks per cluster that had already succeeded, discarded on
    # the twelfth.
    #
    # The refusal is one API surface being closed, not the node pools being
    # absent, and the fix for the gate failure used to be to declare
    # `legacy-metadata` inapplicable -- publishing "no user-managed node
    # pools to carry a metadata setting" for a cluster that has five pools,
    # each carrying `config.metadata` and `config.workloadMetadataConfig`,
    # readable through the `clusters describe` above. That cost real coverage:
    # every Autopilot pool reports the compliant `GKE_METADATA`, so the check
    # does not fail to apply, it runs and passes, and saying otherwise
    # published an enforced control as unverified. Worse, the "run the
    # detection anyway and withhold the declaration if it fires" guard in
    # `collect_cluster` was structurally dead for this one slug: with
    # `node_pools` hardcoded empty, `check_legacy_metadata` returned no hits
    # by construction on every Autopilot cluster forever, so nothing could
    # ever falsify the reason. Take the pools from the describe projection
    # instead, and record that describe as the command behind the check.
    if cluster.get("autopilot"):
        context["node_pools"] = context["cluster_describe"].get("nodePools") or []
        commands["legacy-metadata"] = describe_record
    else:
        node_pools_argv = ["gcloud", "container", "node-pools", "list", "--cluster", name, "--location", location, "--project", project, "--format", "json"]
        parsed, result = gated(node_pools_argv)
        if parsed is None:
            raise GateFailure(f"node-pools list gate failed (rc={result.rc}): {result.stderr.strip()[:300]}")
        context["node_pools"] = parsed if isinstance(parsed, list) else []
        commands["legacy-metadata"] = _record(shlex.join(node_pools_argv), result)

    # Whether the three admission premises `_COMPLIANCE_AUTOPILOT_NOT_APPLICABLE`
    # rests on are still intact on this cluster. They hold only while nothing
    # lifts them, and Google documents two things that do: a customer-authored
    # `WorkloadAllowlist` naming `autogke-disallow-privilege` or
    # `autogke-no-write-mode-hostpath` (GKE >= 1.32.0-gke.1000000), and a
    # partner allowlist synchronised in (>= 1.32.2-gke.1652000). With one
    # installed, a privileged or hostPath workload is admissible and the three
    # checks are checks that ran and found nothing, not checks the cluster's
    # shape rules out -- so read for it rather than assert its absence in a
    # reason string an operator is meant to trust.
    #
    # Non-gated, for the reason the `ccnp` read above is: the four CRDs come
    # from Autopilot itself, so a cluster without them answers "the server
    # doesn't have a resource type", which is the answer (no allowlists) and
    # not a missing input. The residual risk is an RBAC denial reading as
    # absence; it is the same trade the `ccnp` read makes, and it fails to the
    # behaviour that predates this read rather than past it.
    if cluster.get("autopilot"):
        allowlist_argv = [
            "kubectl", "get",
            "workloadallowlists.auto.gke.io,allowlistsynchronizers.auto.gke.io,"
            "allowlistedworkloads.auto.gke.io,allowlistedv2workloads.auto.gke.io",
            "-o", "json",
        ]
        allowlist_parsed, _ = run_and_gate(allowlist_argv, kubeconfig, run=run)
        context["autopilot_allowlists"] = [
            f"{i.get('kind') or '?'}/{(i.get('metadata') or {}).get('name', '')}"
            for i in (allowlist_parsed or {}).get("items", []) or []
        ]

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
    workload_record = _record(f"KUBECONFIG={kubeconfig} {shlex.join(workload_argv)}", result)

    svc_argv = ["kubectl", "get", "svc", "-A", "-o", "json"]
    svc_parsed, svc_result = run_and_gate(svc_argv, kubeconfig, run=run)
    if svc_parsed is None:
        raise GateFailure(f"service dump gate failed (rc={svc_result.rc}): {svc_result.stderr.strip()[:300]}")
    svc_record = _record(f"KUBECONFIG={kubeconfig} {shlex.join(svc_argv)}", svc_result)

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


# Why a target's per-workload checks had nothing to examine, per stream. Each
# stream filters the same dump differently -- obtainability reads templates and
# drops Pods, compliance reads resolved pod specs and keeps unowned ones,
# ai-security keeps only what `_is_ai_workload` matches -- so "empty" means a
# different thing in each and one sentence for all three would be false for two
# of them.
_EMPTY_SCOPE_REASON: dict[str, str] = {
    "obtainability-audit": (
        "No workload on this cluster is in scope: every Deployment, StatefulSet, "
        "DaemonSet and Job the dump returned is in a system namespace, "
        "addon-managed, owned by another object, opted out, or scaled to zero. "
        "This check examined nothing here, which is not the same as finding nothing."
    ),
    "compliance-audit": (
        "No workload on this cluster is in scope: every Deployment, StatefulSet, "
        "DaemonSet, CronJob and unowned Pod the dump returned is in a system "
        "namespace, addon-managed, or a GKE component. This check examined nothing "
        "here, which is not the same as finding nothing."
    ),
}

# `ai-security-audit` is deliberately absent, and the omission is the whole
# reason this is a table rather than a constant. What separates it from the two
# above is *what the empty set is empty of*. Obtainability and compliance drop
# workloads that exist and could carry the defect -- a system namespace, an
# addon, an owned Pod, one scaled to zero -- so "no finding" there is an
# unexamined absence and the reason says so. ai-security's filter is the
# subject definition itself: with no AI workload on the cluster there is no
# object a public inference endpoint or a writable weights mount could be true
# of. "Six checks ran and matched nothing" is a verdict about the cluster, not
# a gap in the audit, and §1 of the SOP says the same in the instruction it
# calls the most important in that section.
#
# An earlier version of this comment argued the omission mechanically instead,
# and every step of it was wrong: it claimed all six checks take an AI workload
# so an empty scope leaves `commands` empty, forcing a `limitations` note and
# pinning the stream `partial` forever. `inference-endpoint-public` is
# `"cluster"`-kind, so `commands` keeps an entry and `checks_run` is never
# empty; the arm below writes `checks_not_applicable`, which is not
# `limitations`; and `checks_not_applicable` leaves the coverage denominator
# without producing a gap (`audit_report.checks_unevaluated`), so it could not
# make a run `partial` even if it did fire. Do not re-derive the omission from
# that chain -- it does not hold, and the argument above does not need it.
#
# The real exposure is upstream, in `_is_ai_workload`: a model server under an
# image name `AI_MODEL_IMAGE_RE` does not list, serving on CPU so no
# accelerator limit gives it away, is invisible to this stream and reported as
# a clean cluster. That is a discriminator false negative, not a bookkeeping
# one, and no entry in this table would catch it.
#
# `AI_PROVIDER_CREDENTIAL_ENV_RE` is the answer to part of that, and only part.
# It admits a workload on a mark no allowlist has to keep up with -- holding a
# named provider credential -- which covers the unlisted runtime that pulls from
# Hugging Face or calls a hosted provider, and put this fleet's `litellm` in
# scope for the first time. What it still cannot see is a model server that
# neither matches a listed image nor holds a credential: one serving weights
# baked into its own image, on CPU, anonymously. Judge a "clean cluster" verdict
# from this stream against that shape before believing it.


def workload_declarations(root: Path) -> dict[tuple[str, str, str, str], set[str]]:
    """Index the Kubernetes objects a GitOps clone declares, by cluster tree.

    Maps `(cluster, kind, namespace, metadata.name)` to the set of
    clone-relative paths declaring it — a set, because two files claiming one
    object is the ambiguity `declaration_for` refuses to resolve rather than
    guessing between.

    This is `audit_report.kcc_declarations` for workload objects, and it exists
    for the reason that one gives: "parsed rather than grepped, deliberately".
    Config Connector resources got that treatment; the Kubernetes objects every
    other stream flags did not, and their SOPs still tell the *model* to run
    `grep -rl "name: <object>"` once per finding and decide for itself. It does
    not decide consistently. On 2026-09-06 the live fleet carried 116 findings
    in which `Deployment/waste-unsized` was `kind: manifest` with a correct
    path under `unsized-workload` and `kind: manual` -- "no pull request is
    possible" -- under `probes-liveness`, `no-requests` and `no-memory-limit`,
    which are the same object in the same file. Fourteen findings were `manual`
    on objects this index resolves.

    Config Connector resources are skipped, not overlooked: `kcc_declarations`
    already indexes them, keyed by the GCP resource name that `spec.resourceID`
    can override, and a `Cluster/<name>` finding must keep resolving through
    that table rather than through this one.

    Returns `{}` when PyYAML is absent or the clone is unreadable, which leaves
    every candidate unannotated and the SOP's grep as the only answer -- the
    behaviour that shipped before this existed.
    """
    try:
        import yaml  # noqa: PLC0415 -- optional; absence disables the annotation
    except ImportError:
        return {}

    index: dict[tuple[str, str, str, str], set[str]] = {}
    try:
        paths = sorted(root.rglob("*.yaml")) + sorted(root.rglob("*.yml"))
    except OSError:
        return {}
    for path in paths:
        # `.git` holds packed objects, not manifests, and rglob walks into it.
        if GIT_DIR_NAME in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            docs = list(yaml.safe_load_all(text))
        except yaml.YAMLError:
            # A file this collector cannot parse is one it cannot make a claim
            # about. Skipping leaves those findings unannotated.
            continue
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        parts = relative.parts
        # Only a file under `clusters/<name>/` is applied to a known cluster.
        # Anything else -- `gcp/`, `bootstrap/`, the repo root -- is either a
        # Config Connector resource this index skips or hub infrastructure no
        # per-cluster finding should resolve to, so it is indexed under no
        # cluster and can never match: `declaration_for` requires the cluster.
        if len(parts) <= GITOPS_CLUSTER_TREE_DEPTH or parts[0] != GITOPS_CLUSTER_TREE_ROOT:
            continue
        cluster = parts[1]
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            api = str(doc.get("apiVersion") or "")
            kind = str(doc.get("kind") or "")
            if not kind or KCC_API_GROUP_SUFFIX in api:
                continue
            meta = doc.get("metadata")
            if not isinstance(meta, dict):
                continue
            name = str(meta.get("name") or "")
            namespace = str(meta.get("namespace") or "")
            if not name or not namespace:
                continue
            index.setdefault((cluster, kind, namespace, name), set()).add(str(relative))
    return index


def declaration_for(
    index: dict[tuple[str, str, str, str], set[str]],
    cluster: str,
    namespace: str,
    obj: str,
) -> dict | None:
    """Where the GitOps repo declares one candidate's object, or `None`.

    `None` means unannotated — no claim either way — and is what an absent
    index, an object outside `Kind/name` form, or a genuine miss all return.
    A hit carries `path` (the declaration itself, for a remediation that
    *changes* the object) and `directory` (its parent, for one that *creates* a
    sibling beside it, as a new PDB or NetworkPolicy does).

    The match is exact on all four of cluster, kind, namespace and name. Every
    looser arm was measured against the live fleet's 116 findings first and
    matched nothing the exact arm had not already matched, so the fallbacks
    that would have made this kind-blind or cluster-blind -- the two failure
    modes every SOP warns the model's `grep` has -- buy nothing and are not
    here. A namespaced object resolved into another cluster's tree fails that
    tree's sync, which is worse than resolving nothing.
    """
    kind, _, name = obj.partition("/")
    if not kind or not name or not cluster or not namespace:
        return None
    paths = index.get((cluster, kind, namespace, name))
    if not paths or len(paths) > 1:
        # Two files declaring one object is a duplicate resource id that Argo
        # and Config Sync both reject; picking one would name the wrong file
        # half the time. Say nothing and let the SOP's `manual` branch hold.
        return None
    path = next(iter(paths))
    parent = str(Path(path).parent)
    return {"path": path, "directory": parent}


def collect_cluster(
    cluster: dict,
    audit_id: str,
    checks: tuple[CheckSpec, ...],
    *,
    run: RunFn = default_run,
    declarations: dict[tuple[str, str, str, str], set[str]] | None = None,
) -> dict:
    """One manifest `clusters[]` entry (§6): every enumerated cluster gets
    one, whatever happened — `outcome` says which of the three shapes it is.
    """
    name, project, location = cluster["name"], cluster["project"], cluster["location"]
    autopilot = bool(cluster.get("autopilot"))
    kubeconfig, cred_run = fetch_credentials(project, name, location, run=run)
    if cred_run.rc != 0:
        return {
            "name": name, "project": project, "location": location,
            "autopilot": autopilot,
            "outcome": "unreachable",
            "error": f"get-credentials rc={cred_run.rc}: {cred_run.stderr.strip()[:300]}",
        }

    try:
        collected = _COLLECTORS[audit_id](cluster, kubeconfig, checks, run=run)
    except GateFailure as exc:
        return {
            "name": name, "project": project, "location": location,
            "autopilot": autopilot,
            "outcome": "gate-failed",
            "error": str(exc),
        }

    def emit(spec: CheckSpec, hit: dict, default_namespace: str, workload: dict | None = None) -> dict:
        severity = hit.get("severity") or spec.severity
        # Same override `severity` already has, for the same reason: a check
        # whose arms differ in what they prove cannot state one consequence for
        # all of them, and the arm is only known where the hit is built.
        impact = hit.get("impact") or spec.impact
        # Which arm fired is an observation, not prose, and only the hit knows
        # it. Flagged so `adopt_arm_impact` can hold the model to this sentence
        # the way `adopt_collector_evidence` holds it to the excerpt -- and so
        # a later run cannot carry a stale arm sentence forward over a
        # corrected one. The `spec.impact` default is deliberately *not*
        # flagged: there the model's object-specific rewrite is usually the
        # better sentence, naming the quota or the cluster the constant cannot.
        arm_specific = bool(hit.get("impact"))
        if severity == spec.severity and cluster.get("autopilot") and spec.autopilot_severity:
            severity = spec.autopilot_severity
            impact = f"{impact} (Autopilot: severity downgraded — the platform injects requests at admission.)"
        excerpt = hit["excerpt"]
        if workload and workload.get("suspended"):
            excerpt += _SUSPENDED_CRONJOB_NOTE
        if workload and workload.get("scaled_to_zero"):
            excerpt += _scaled_to_zero_note(workload["kind"])
        emitted = {
            "check": spec.slug,
            "cluster": name,
            "namespace": hit.get("namespace", default_namespace),
            "object": hit["object"],
            "severity": severity,
            "excerpt": excerpt,
            "impact": impact,
            "needs_triage": None,
        }
        if arm_specific:
            emitted["impact_authoritative"] = True
        # Where the GitOps repo declares this object, when it does. Absent
        # means unannotated, never "no declaration exists": the index is empty
        # without `--workspace`, and the SOP's own grep is still the answer
        # then. See `workload_declarations`.
        if declarations:
            declaration = declaration_for(declarations, name, emitted["namespace"], hit["object"])
            if declaration:
                emitted["declaration"] = declaration
        # What reasserts this object's spec, where something does. A workload
        # check gets it off the workload record; a cluster check has to put it
        # on the hit itself, because `workload` is None there and only the
        # check knows which of its inputs `hit["object"]` names.
        #
        # This used to be workload-only, on the premise that "a cluster-scoped
        # hit has no object whose spec a controller could be holding". That is
        # false for six of the ten cluster checks -- `inference-endpoint-public`
        # names a Service, `blocking-pdb` a PodDisruptionBudget,
        # `hpa-cannot-scale` an HPA, the two RBAC checks a binding or a role,
        # and `default-sa-automount` a workload it reached without iterating
        # `collected.workloads`. Cluster-scoped means the check runs once per
        # cluster, not that its findings name the cluster. The one that cost:
        # `inference-endpoint-public` on `Service/ai-inference-unsafe`, a manual
        # finding telling the owner to choose a fix, on an object the Argo CD
        # Application `workloads-adamparco-gitops` holds. The four checks left
        # out name a GKE cluster, a node pool, or -- `netpol-missing` -- a
        # Namespace whose fix is a NetworkPolicy that does not exist yet, where
        # "a change applied by hand is reverted" would be false.
        reconciler = (workload or {}).get("reconciler") or hit.get("reconciler")
        if reconciler:
            emitted["reconciler"] = reconciler
        return emitted

    candidates = []
    for spec in checks:
        if spec.kind == "workload":
            for workload in collected.workloads:
                hit = spec.run(workload, collected.context)
                if hit is not None:
                    candidates.append(emit(spec, hit, workload["ns"], workload))
        else:
            for hit in spec.run(collected.context):
                candidates.append(emit(spec, hit, hit.get("namespace", "")))

    not_applicable_slugs: set[str] = set()
    checks_not_applicable: list[dict] = []
    if audit_id == "compliance-audit" and cluster.get("autopilot"):
        applicable = {spec.slug for spec in checks}
        # A check that found something plainly applied. Each reason below
        # asserts admission rejects the shape for in-scope workloads, so a
        # candidate is that premise being wrong on this cluster -- a preview
        # channel, a workload predating the conversion, an exemption Google
        # granted -- and the manifest must not say both. Declaring it
        # inapplicable while carrying its candidate is the incoherence the
        # `commands` filter already avoids, and it resolves the wrong way: the
        # finding is real and the claim about the cluster's shape is not.
        #
        # Every slug in the table is backed by a detection fed the real
        # workload dump, so this guard has something to guard for each of
        # them. It did not always: `legacy-metadata` was in the table while
        # `_collect_compliance` handed `check_legacy_metadata` a hardcoded
        # empty `node_pools`, so it returned no hits by construction and could
        # never falsify its own reason. Keep that property -- a slug added
        # here whose detection cannot see real input silently disarms this.
        found = {c["check"] for c in candidates}
        # The second way each premise fails. Every reason ends by saying this
        # cluster carries no WorkloadAllowlist, and an allowlist is exactly
        # what lifts the admission rule the rest of the sentence rests on --
        # so one existing makes all three declarations false at once, whether
        # or not any check fired. `_collect_compliance` reads for them; the
        # three then report as checks that ran, which they did.
        allowlists = collected.context.get("autopilot_allowlists") or []
        if allowlists:
            print(
                f"[collect] {project}/{name}: {len(allowlists)} Autopilot workload "
                f"allowlist object(s) present ({', '.join(sorted(allowlists)[:3])}); "
                f"admission can be exempted here, so §1's node-facing checks are "
                f"reported as checks that ran rather than declared inapplicable",
                file=sys.stderr,
            )
        for slug, reason in _COMPLIANCE_AUTOPILOT_NOT_APPLICABLE:
            if slug not in applicable or allowlists:
                continue
            if slug in found:
                print(
                    f"[collect] {project}/{name}: {slug} is declared inapplicable on "
                    f"Autopilot but produced candidates here; reporting it as a check "
                    f"that ran",
                    file=sys.stderr,
                )
                continue
            not_applicable_slugs.add(slug)
            checks_not_applicable.append({"check": slug, "reason": reason})

    if not collected.workloads and audit_id in _EMPTY_SCOPE_REASON:
        # Nothing survived S1-S5, so every per-workload check on this target
        # examined an empty set. Reporting those as checks that ran is what
        # the 2026-09-05 obtainability run did on 12 of 16 clusters: the
        # document said 11 checks on 16 targets with nothing inapplicable and
        # no limitation, which reads as fourteen healthy clusters when twelve
        # of them were never in scope at all. A check with nothing to examine
        # has not cleared anything, and the manifest is the only place that
        # distinction still exists -- by publish time a clean cluster and an
        # empty one are the same absence of candidates.
        #
        # `outcome` is already "collected", so the dump succeeded; this is the
        # scope being empty, not the read failing. Cluster-scoped checks are
        # untouched: an HPA pointing at a workload that no longer exists, a
        # public control plane, a cluster-admin binding are all still true of
        # a cluster running nothing.
        reason = _EMPTY_SCOPE_REASON[audit_id]
        for spec in checks:
            if spec.kind != "workload" or spec.slug in not_applicable_slugs:
                continue
            not_applicable_slugs.add(spec.slug)
            checks_not_applicable.append({"check": spec.slug, "reason": reason})

    # A check the collector itself established has nothing to examine here,
    # from a read it made. The two blocks above infer inapplicability from a
    # property of the cluster (Autopilot) or of the scope (no workloads); this
    # one carries a fact only the read knows -- that the CRD is absent, or
    # installed and empty. Same coherence guard as the Autopilot block, and for
    # the same reason: a manifest must never both declare a check inapplicable
    # and carry its candidates.
    found = {c["check"] for c in candidates}
    applicable = {spec.slug for spec in checks}
    for slug, reason in sorted((collected.context.get("not_applicable") or {}).items()):
        if slug not in applicable or slug in not_applicable_slugs:
            continue
        if slug in found:
            print(
                f"[collect] {project}/{name}: {slug} was declared inapplicable by the "
                f"collector but produced candidates here; reporting it as a check that ran",
                file=sys.stderr,
            )
            continue
        not_applicable_slugs.add(slug)
        checks_not_applicable.append({"check": slug, "reason": reason})

    result = {
        "name": name, "project": project, "location": location,
        "autopilot": autopilot,
        "outcome": "collected",
        "commands": [{"check": spec.slug, **collected.commands[spec.slug]} for spec in checks if spec.slug not in not_applicable_slugs],
        "candidates": candidates,
    }
    if checks_not_applicable:
        result["checks_not_applicable"] = checks_not_applicable
    return result


def crashed_entry(cluster: dict, exc: BaseException) -> dict:
    """A `clusters[]` entry for a worker that raised something unmodelled.

    `future.result()` re-raises, so one unhandled exception on one cluster
    aborts `collect_fleet` — and every SOP invokes this collector as
    `collect.py … > manifest_<audit>.json`, so by then the shell has already
    truncated the file. The run loses the whole fleet to one bad object
    instead of one cluster, and the operator sees an empty manifest rather
    than a reason. `gate-failed` is the shape the document already carries for
    "enumerated, could not be read": the validator counts it as a scope loss
    and the remaining clusters still publish.
    """
    print(
        f"[collect] {cluster.get('project', '?')}/{cluster.get('name', '?')}: "
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


def collect_fleet(
    audit_id: str,
    project: str,
    *,
    run: RunFn = default_run,
    max_workers: int = MAX_WORKERS,
    workspace: Path | None = None,
) -> dict:
    checks = CHECK_TABLES.get(audit_id)
    if not checks:
        raise ValueError(f"no check table for {audit_id!r} yet — see this file's module docstring")

    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    clusters, not_running = enumerate_clusters(project, run=run)

    # Built once for the whole fleet, before the pool: every cluster's
    # candidates resolve against the same clone, and walking it per cluster
    # would read the same tree sixteen times to get the same answer.
    declarations = workload_declarations(workspace) if workspace else {}

    results = [None] * len(clusters)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(
                collect_cluster, cluster, audit_id, checks, run=run, declarations=declarations
            ): index
            for index, cluster in enumerate(clusters)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:  # noqa: BLE001 — see crashed_entry
                results[index] = crashed_entry(clusters[index], exc)

    return {
        "version": MANIFEST_VERSION,
        "checks_revision": CHECKS_REVISION,
        "audit": audit_id,
        "started_at": started_at,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "clusters": results + not_running,
    }


def resolve_project(cli_project: str | None, *, run: RunFn = default_run) -> str:
    """The project to enumerate, from the flag or from the pod's environment.

    `--project` was `required=True`, and the three cron prompts that name this
    script name it without one, so the literal command each prompt hands the
    agent exited 2 on argparse before reaching a single check. Every sibling
    collector -- `fleet_drift.py`, `fleet_stockout.py`, `fleet_waste.py`,
    `patch_readiness.py`, `networking_audit.py` -- already treats the flag as
    an override over a discovered default; this one was the outlier, and the
    outlier is what the prompts were written against.

    Same env chain `networking_audit.get_target_projects` uses, minus
    `MONITORED_PROJECT_IDS`: `enumerate_clusters` takes one project, so a
    multi-project variable has no single right answer here and is left to the
    fleet-wide collectors that can sweep it.
    """
    if cli_project:
        return cli_project
    for env_var in ("GCP_PROJECT_ID", "GKE_PROJECT_ID", "PROJECT_ID"):
        value = os.environ.get(env_var, "").strip()
        if value:
            return value
    result = run(["gcloud", "config", "get-value", "project"])
    if result.rc == 0 and result.stdout.strip():
        return result.stdout.strip()
    raise RuntimeError(
        "no project to audit: pass --project, or set GCP_PROJECT_ID, "
        "GKE_PROJECT_ID or PROJECT_ID, or configure a gcloud default project"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("audit", choices=sorted(CHECK_TABLES))
    parser.add_argument(
        "--project",
        help="single project to audit; omit to use GCP_PROJECT_ID/GKE_PROJECT_ID/PROJECT_ID",
    )
    parser.add_argument(
        "--workspace",
        help=(
            "the GitOps clone `audit_report.py start` made, so each candidate "
            "carries where the repository declares its object; omit and no "
            "candidate is annotated"
        ),
    )
    args = parser.parse_args(argv)
    try:
        project = resolve_project(args.project)
    except RuntimeError as exc:
        print(f"collect.py: {exc}", file=sys.stderr)
        return 2
    workspace = Path(args.workspace) if args.workspace else None
    if workspace is not None and not workspace.is_dir():
        # Loud, and not fatal. A typo here would otherwise annotate nothing and
        # read exactly like a repository that declares none of the fleet, which
        # is the answer that sends every finding to `manual`.
        print(
            f"collect.py: --workspace {str(workspace)!r} is not a directory; "
            "no candidate will carry a declaration",
            file=sys.stderr,
        )
        workspace = None
    manifest = collect_fleet(args.audit, project, workspace=workspace)
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
