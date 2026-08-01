#!/usr/bin/env python3
"""detect-drift, all three tiers, against the operations contract that replaced the corrective PR.

WHAT THIS FILE REPLACES, AND WHY IT IS A REPLACEMENT AND NOT A DELETION
------------------------------------------------------------------------
The previous version of this file tested one tier (platform) against a path that no longer exists:
`detect_drift` imported `submit_suggestion`, opened a git branch, wrote an OKF observation and
emitted a corrective pull request under `--emit-corrective --work-dir --object-path --dry-run
--artifact-dir`. 02 §2.5.1 put "a pull request for work inside its own authority" on the same
footing as a failed action, so the whole path went, and the file stopped importing. Three scripts
were left with no coverage at all.

Deleting it was not available. `dev/assertion-baseline.json` ratchets assertion count (V-MET-003,
09 §11.7) precisely so that a conversion cannot quietly shrink a suite, and 07 §5's rule is that the
replacement lands before the retirement. So this is the replacement: three tiers instead of one, and
the properties that survived the conversion (the desired-authoritative diff, the ignore-set, the
read-only guarantee) carried over intact rather than re-derived.

WHAT IS ASSERTED, AND WHY EACH ONE IS WORTH A CHECK
-----------------------------------------------------
    the roster is derived        every `agents/*/` tier, joined against 02 §2.1's own table.
                                 A hardcoded three stops covering a fourth ([[LSN-036]]).
    the diff core is shared      `strip` / `find_drift` / `object_slug` / `target_of` / the
                                 ignore-set are ONE implementation living in three files. Pinned by
                                 AST equality so one tier's diff cannot quietly diverge.
    exit codes                   0 clean / 2 drift / 1 error, on all three.
    the envelope join            every operation any tier emits builds a valid `ActionEnvelope`
                                 through `action_envelope.build_envelope` — the thing that actually
                                 has to be true for a finding to become a write.
    tier scope containment       the security-relevant one. Developer-team reads nothing
                                 cluster-scoped (03 §3.2: "any other namespace; cluster or project
                                 scope; cluster-scoped objects"), emits no cloud target, and its
                                 only upward move is `escalate`. Each arm carries a live control —
                                 another tier the same scan must fire on — so it cannot pass by
                                 reading nothing.
    no self-classification       03 §4.1 step 4 puts risk in the broker. A script that names a risk
                                 class has taken a decision it is not allowed to take.
    hermetic                     no socket, no credential, no `broker_client`, no git — by import
                                 set AND by running a full survey with the network primitives
                                 booby-trapped.
    blocked names one fact       a finding that cannot be remediated says which single fact is
                                 missing instead of guessing it into an envelope. Asserted as a
                                 pair: blocked without the fact, an operation with it.
    the PR path stays gone       no `submit_suggestion`, no git, none of the five removed flags —
                                 in executable content, because all three module docstrings still
                                 *describe* the path they removed.

HOW THE PROSE/CODE DISTINCTION IS DRAWN
-----------------------------------------
Every "the script must not mention X" arm reads the module through `ast`, with docstrings stripped.
The three scripts state the rules they follow in their own docstrings — "never decides a risk
class", "used to end in a corrective pull request", "which is node capacity" — so a naive grep
flags the sentence that states the rule and passes the file that breaks it. This is the same
distinction `dev/test_apply_change_skill.py` draws with `instruction_text` for markdown.

NO FIXTURE FILES, NO PyYAML, NO NETWORK, NO CLUSTER
-----------------------------------------------------
Inventories are built in this file and written to a tmpdir. `load_manifest` only reaches for PyYAML
when the input fails to parse as JSON, and system python3 has none ([[LSN-007]]), so every fixture
is JSON.
"""

from __future__ import annotations

import ast
import copy
import io
import importlib.util
import json
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[1]
AGENTS = REPO / "agents"
SKILL = "detect-drift"
PERSONAS = REPO / "docs/design/02-agent-personas.md"


# --- the roster, derived twice and joined -----------------------------------------------------------


def tier_dirs() -> tuple[str, ...]:
    """Every agent tier in the tree, read off the filesystem.

    Deriving rather than writing `("platform", "cluster-admin", "developer-team")` is the whole
    reason this suite can be trusted to keep covering the thing it covers: a hardcoded roster keeps
    passing on the day a fourth tier is added, which is [[LSN-036]] exactly. `dev/
    test_live_refresh_image_set.py` derives `AGENT_DIRS` the same way for the same reason.
    """
    return tuple(sorted(p.name for p in AGENTS.iterdir() if p.is_dir() and (p / "skills").is_dir()))


TIERS = tier_dirs()


def skill_allocation() -> tuple[tuple[str, ...], dict[str, dict[str, str]]]:
    """02 §2.1's allocation table, parsed: (tiers that ship detect-drift, mesh grants).

    The mesh grants are `{"delegate": {"platform": "cluster-admin", ...}, "escalate": {...}}` —
    read out of the `✅ → <tier>` cells, so the direction each verb may travel is the spec's
    statement of it and not a copy in this file.
    """
    section = PERSONAS.read_text().split("### 2.1 Skill allocation", 1)[1].split("\n### ", 1)[0]
    rows = [line for line in section.splitlines() if line.strip().startswith("|")]
    assert len(rows) > 3, "02 §2.1 no longer holds a skill-allocation table"

    def cells(line: str) -> list[str]:
        return [c.strip() for c in line.strip().strip("|").split("|")]

    columns = [c.lower().replace(" ", "-") for c in cells(rows[0])[1:]]
    drift_tiers: tuple[str, ...] = ()
    mesh: dict[str, dict[str, str]] = {"delegate": {}, "escalate": {}}
    for row in rows[2:]:
        cell = cells(row)
        if len(cell) != len(columns) + 1:
            continue
        subject, marks = cell[0], cell[1:]
        if f"`{SKILL}`" in subject:
            drift_tiers = tuple(t for t, m in zip(columns, marks) if "✅" in m)
        for verb in mesh:
            if f"`{verb}`" in subject:
                for tier, mark in zip(columns, marks):
                    if "✅" in mark and "→" in mark:
                        mesh[verb][tier] = mark.split("→", 1)[1].strip()
    return drift_tiers, mesh


SPEC_DRIFT_TIERS, MESH = skill_allocation()


def script_path(tier: str) -> Path:
    return AGENTS / tier / "skills" / SKILL / "scripts" / "detect_drift.py"


def skill_md(tier: str) -> Path:
    return AGENTS / tier / "skills" / SKILL / "SKILL.md"


def envelope_path(tier: str) -> Path:
    return AGENTS / tier / "scripts" / "action_envelope.py"


# --- loading three modules that share a name --------------------------------------------------------

_LOADED: dict[str, ModuleType] = {}


def _load(path: Path, alias: str) -> ModuleType:
    if alias not in _LOADED:
        spec = importlib.util.spec_from_file_location(alias, path)
        assert spec and spec.loader, f"cannot load {path}"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # `if __name__ == "__main__"` does not fire under an alias
        _LOADED[alias] = module
    return _LOADED[alias]


def drift(tier: str) -> ModuleType:
    return _load(script_path(tier), f"_detect_drift_{tier.replace('-', '_')}")


def envelope(tier: str) -> ModuleType:
    return _load(envelope_path(tier), f"_action_envelope_{tier.replace('-', '_')}")


# --- AST helpers: every "the script must / must not" arm reads executable content only ---------------


def parsed(source: str) -> ast.Module:
    """The module with every docstring removed.

    All three scripts narrate the rules they obey in prose. `strip`'s docstring differs across the
    three by one word (mirror- / baseline- / manifest-authored) while the code is identical, the
    module docstrings describe the pull-request path they deleted, and `orphaned_pvcs` says "gated"
    out loud. Judging prose here would produce three false reds and one false green.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                if isinstance(body[0].value.value, str):
                    node.body = body[1:] or [ast.Pass()]
    return tree


def top_level_defs(source: str) -> dict[str, str]:
    """Every module-level function and constant, as normalized AST text keyed by name."""
    tree = parsed(source)
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = ast.dump(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = ast.dump(node.value)
    return out


def imported_modules(source: str) -> set[str]:
    """Top-level package of every import, including the ones deferred inside a function body."""
    mods: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    return mods


def declared_flags(source: str) -> set[str]:
    """Every `--flag` the argument parser declares."""
    flags: set[str] = set()
    for node in ast.walk(parsed(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("--"):
                    flags.add(arg.value)
    return flags


def subject_flags(source: str) -> set[str]:
    """The mutually-exclusive subject group: `--desired` plus the one inventory flag for this tier."""
    flags: set[str] = set()
    for node in ast.walk(parsed(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subject"
        ):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    flags.add(arg.value)
    return flags


def survey_flag(tier: str) -> str:
    """`--fleet` / `--cluster` / `--namespace-state`, read out of the parser rather than listed here."""
    others = subject_flags(script_path(tier).read_text()) - {"--desired"}
    assert len(others) == 1, f"{tier} declares subject flags {sorted(others)}; expected exactly one inventory flag"
    return others.pop()


def finding_kwargs(source: str) -> set[str]:
    """The keyword-only parameters of `finding()` — the shapes a finding on this tier can take."""
    for node in parsed(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == "finding":
            return {a.arg for a in node.args.kwonlyargs}
    raise AssertionError("no top-level finding() in this script")


def handoff_targets(source: str) -> set[str]:
    """Every literal passed as `handoff=` — the skills this tier's findings hand work to."""
    out: set[str] = set()
    for node in ast.walk(parsed(source)):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "handoff" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    out.add(kw.value.value)
    return out


def blockable_checks(source: str) -> set[str]:
    """The check names that have a `blocked=` call site — the checks that CAN say "I need one fact"."""
    out: set[str] = set()
    for node in ast.walk(parsed(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "finding"
            and any(kw.arg == "blocked" for kw in node.keywords)
        ):
            assert node.args and isinstance(node.args[0], ast.Constant), "finding()'s check name is no longer a literal"
            out.add(node.args[0].value)
    return out


def read_keys(source: str) -> set[str]:
    """Every string constant the module passes to `.get(...)` — i.e. what it reads out of its input.

    This is how the developer-team containment arm is expressed. A word scan over the source cannot
    work: `stuck_rollouts` builds the evidence string "which is node capacity, not this workload" at
    runtime, and `escalate="cluster-admin"` is a literal the tier is *supposed* to carry. What the
    tier may not do is READ a cluster-scoped collection, and every such read goes through `.get`.
    """
    out: set[str] = set()
    for node in ast.walk(parsed(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    out.add(arg.value)
    return out


RISK_NAME = re.compile(r"risk|severit|classif", re.I)
# 03 §5's four classes, plus the four words a re-implementation reaches for instead. A script has no
# business emitting any of them: the broker computes the class from the objects and the diff.
RISK_CLASSES = frozenset({"routine", "elevated", "gated", "forbidden", "low", "medium", "high", "critical"})


def risk_vocabulary(source: str) -> set[str]:
    """Anything in executable content that reads as the script naming a risk class."""
    hits: set[str] = set()
    for node in ast.walk(parsed(source)):
        if isinstance(node, ast.Name) and RISK_NAME.search(node.id):
            hits.add(f"name:{node.id}")
        elif isinstance(node, ast.arg) and RISK_NAME.search(node.arg):
            hits.add(f"arg:{node.arg}")
        elif isinstance(node, ast.Attribute) and RISK_NAME.search(node.attr):
            hits.add(f"attr:{node.attr}")
        elif isinstance(node, ast.keyword) and node.arg and RISK_NAME.search(node.arg):
            hits.add(f"kwarg:{node.arg}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if RISK_NAME.search(node.value):
                hits.add(f"str:{node.value[:48]}")
            if node.value.strip().lower() in RISK_CLASSES:
                hits.add(f"class:{node.value}")
    return hits


def risk_flavoured(obj: Any) -> set[str]:
    """The same property over emitted data: no finding or operation carries a risk claim."""
    hits: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and RISK_NAME.search(k):
                hits.add(f"key:{k}")
            hits |= risk_flavoured(v)
    elif isinstance(obj, list):
        for item in obj:
            hits |= risk_flavoured(item)
    elif isinstance(obj, str) and obj.strip().lower() in RISK_CLASSES:
        hits.add(f"value:{obj}")
    return hits


# --- running a tier ---------------------------------------------------------------------------------


def cli(tier: str, argv: list[str]) -> tuple[int, str, str]:
    """`main()` with a patched argv — the real exit code, without a subprocess.

    `main()` catches `Exception` and returns 1; argparse raises `SystemExit`, which is a
    `BaseException` and goes straight past it, so both paths have to be caught here to see the code
    a shell would.
    """
    module = drift(tier)
    out, err = io.StringIO(), io.StringIO()
    saved = sys.argv
    sys.argv = ["detect_drift.py", *argv]
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = module.main()
            except SystemExit as exit_:
                code = exit_.code if isinstance(exit_.code, int) else 1
    finally:
        sys.argv = saved
    return code, out.getvalue(), err.getvalue()


def report(tier: str, argv: list[str]) -> dict[str, Any]:
    """`--json`: `{"drift": bool, "findings": [...], "operations": [...]}`."""
    code, out, err = cli(tier, [*argv, "--json"])
    assert code in (0, 2), f"{tier} exited {code}: {err.strip()}"
    return json.loads(out)


def write_json(directory: Path, name: str, payload: Any) -> str:
    path = directory / name
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return str(path)


def clone(obj: Any) -> Any:
    return copy.deepcopy(obj)


# --- fixtures ---------------------------------------------------------------------------------------
#
# One object pair, shared by all three tiers (every tier takes `--desired`/`--live`), plus one
# inventory per tier. The inventories are built to exercise every finding SHAPE the tier has —
# operation, delegate/escalate, handoff, blocked — because the shapes are what the containment
# arms are about. Finding counts are never asserted: they are a property of these fixtures, not of
# the contract, and pinning them turns every future check into a red line here.

DESIRED = {
    "apiVersion": "networking.k8s.io/v1",
    "kind": "NetworkPolicy",
    "metadata": {"name": "default-deny", "namespace": "team-x"},
    "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
}

# Live as the cluster returns it: the authored fields, plus server bookkeeping and defaults.
LIVE_CLEAN = {
    "apiVersion": "networking.k8s.io/v1",
    "kind": "NetworkPolicy",
    "metadata": {
        "name": "default-deny",
        "namespace": "team-x",
        "uid": "abc-123",
        "resourceVersion": "998877",
        "creationTimestamp": "2026-07-01T00:00:00Z",
        "generation": 3,
        "selfLink": "/apis/networking.k8s.io/v1/namespaces/team-x/networkpolicies/default-deny",
        "managedFields": [{"manager": "kube-apiserver"}],
        "annotations": {
            "kubectl.kubernetes.io/last-applied-configuration": "{...}",
            "deployment.kubernetes.io/revision": "4",
        },
    },
    "spec": {
        "podSelector": {},
        "policyTypes": ["Ingress", "Egress"],
        "terminationGracePeriodSeconds": 30,  # a server default desired never mentioned
    },
    "status": {"conditions": []},
}

LIVE_DRIFTED = clone(LIVE_CLEAN)
LIVE_DRIFTED["spec"]["policyTypes"] = ["Ingress"]  # somebody dropped Egress on the live object


def clean_container(name: str) -> dict:
    """A container no developer-team check has anything to say about.

    No `requests`/`usageP95` at all, because `_corrected` returns None the moment either side is
    absent — which keeps the control containers from accidentally becoming right-sizing findings.
    """
    return {
        "name": name,
        "image": f"registry.example.com:5000/{name}:1.4.2",
        "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8080}},
    }


PLATFORM_FLEET = {
    "projectId": "kage-test",
    "clusters": [
        {"name": "cluster-a", "location": "us-east4-a", "controlPlaneVersion": "1.31.4-gke.1183000"},
        {"name": "cluster-b", "location": "us-east4", "controlPlaneVersion": "v1.30.9"},
        {"name": "cluster-c", "controlPlaneVersion": "1.29.8"},  # no location -> no resource path
    ],
    "agents": [
        {"tier": "cluster-admin", "cluster": "cluster-a"},
        {"tier": "cluster-admin", "scope": {"clusterName": "cluster-b"}},
    ],
    "governedNamespaces": [
        {
            "cluster": "cluster-a",
            "namespace": "team-x",
            "baseline": ["ResourceQuota", "LimitRange", "NetworkPolicy"],
            "present": ["ResourceQuota"],
        },
        {"cluster": "cluster-b", "namespace": "team-y", "baseline": ["ResourceQuota"], "present": ["ResourceQuota"]},
    ],
}

PLATFORM_FLEET_CLEAN = {
    "projectId": "kage-test",
    "clusters": [{"name": "cluster-a", "location": "us-east4-a", "controlPlaneVersion": "1.31.4-gke.1183000"}],
    "agents": [{"tier": "cluster-admin", "cluster": "cluster-a"}],
    "governedNamespaces": [
        {"cluster": "cluster-a", "namespace": "team-x", "baseline": ["ResourceQuota"], "present": ["ResourceQuota"]}
    ],
}

CLUSTER_ADMIN_STATE = {
    "namespaces": [
        {
            "name": "team-x",
            "tenant": True,
            "developerTeamAgent": "developer-team-team-x",
            "resourceQuota": True,
            "limitRange": True,
            "networkPolicies": ["default-deny"],
        },
        {  # tenant with a full baseline and no agent -> handoff
            "name": "team-y",
            "tenant": True,
            "resourceQuota": True,
            "limitRange": True,
            "networkPolicies": ["default-deny"],
        },
        {"name": "team-z", "tenant": False},  # no quota, no limits, no policy -> blocked without --baseline
    ],
    "addons": [
        {
            "name": "gateway",
            "installedVersion": "1.2.0",
            "supportedVersion": "1.3.0",
            "target": {
                "group": "apps",
                "version": "v1",
                "kind": "Deployment",
                "namespace": "gateway-system",
                "name": "gateway-controller",
            },
            "container": "controller",
            "supportedImage": "registry.example.com:5000/gateway@sha256:" + "a" * 64,
        },
        {"name": "csi-driver", "installedVersion": "2.0.0", "supportedVersion": "2.1.0"},  # -> blocked
    ],
    "nodePools": [
        {
            "name": "pool-a",
            "utilizationP95": 0.15,
            "nodeCount": 10,
            "windowDays": 21,
            "resource": "projects/kage-test/locations/us-east4-a/clusters/cluster-a/nodePools/pool-a",
        },
        {"name": "pool-b", "utilizationP95": 0.95, "nodeCount": 4, "windowDays": 30},  # no resource -> blocked
        {"name": "pool-c", "utilizationP95": 0.99, "nodeCount": 3, "windowDays": 2},  # window too short -> nothing
    ],
    "upgrade": {"planned": True, "targetVersion": "1.31"},
    "workloads": [
        {"kind": "Deployment", "name": "api", "namespace": "team-x", "replicas": 3},  # no PDB -> delegate
        {"kind": "Deployment", "name": "cron", "namespace": "team-x", "replicas": 1},  # single replica -> nothing
        {"kind": "Deployment", "name": "web", "namespace": "team-x", "replicas": 4, "podDisruptionBudget": "web-pdb"},
    ],
}

CLUSTER_ADMIN_STATE_CLEAN = {
    "namespaces": [
        {
            "name": "team-x",
            "tenant": True,
            "developerTeamAgent": "developer-team-team-x",
            "resourceQuota": True,
            "limitRange": True,
            "networkPolicies": ["default-deny"],
        }
    ],
    "addons": [{"name": "gateway", "installedVersion": "1.3.0", "supportedVersion": "1.3.0"}],
    "nodePools": [
        {
            "name": "pool-a",
            "utilizationP95": 0.6,
            "nodeCount": 5,
            "windowDays": 30,
            "resource": "projects/kage-test/locations/us-east4-a/clusters/cluster-a/nodePools/pool-a",
        }
    ],
    "upgrade": {"planned": False},
    "workloads": [{"kind": "Deployment", "name": "api", "namespace": "team-x", "replicas": 3}],
}

# The tenancy baseline `--baseline` supplies. The script must never author one of these itself
# (its own docstring: "a third copy here is a third thing to drift"), so the values here only ever
# arrive from outside.
TENANCY_BASELINE = {
    "ResourceQuota": {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": {"name": "tenant-quota"},
        "spec": {"hard": {"requests.cpu": "8", "requests.memory": "16Gi"}},
    },
    "LimitRange": {
        "apiVersion": "v1",
        "kind": "LimitRange",
        "metadata": {"name": "tenant-limits"},
        "spec": {"limits": [{"type": "Container", "default": {"cpu": "500m"}}]},
    },
    "NetworkPolicy": {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "default-deny"},
        "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
    },
}

DEVELOPER_TEAM_STATE = {
    "namespace": "team-x",
    "workloads": [
        {  # the new pods cannot be placed: node capacity, so escalate rather than roll back
            "kind": "Deployment",
            "name": "api",
            "replicas": 3,
            "rollout": {"inProgress": True, "stalledMinutes": 90, "reason": "Unschedulable", "updatedReplicas": 0},
            "containers": [clean_container("api")],
        },
        {  # placed and unhealthy, with the previous template to hand -> roll back
            "kind": "Deployment",
            "name": "web",
            "replicas": 3,
            "rollout": {
                "inProgress": True,
                "stalledMinutes": 45,
                "reason": "ProgressDeadlineExceeded",
                "updatedReplicas": 1,
                "previousTemplate": {
                    "spec": {"containers": [{"name": "web", "image": "registry.example.com:5000/web:1.4.1"}]}
                },
            },
            "containers": [clean_container("web")],
        },
        {  # placed and unhealthy, previous template unknown -> blocked on that one fact
            "kind": "Deployment",
            "name": "worker",
            "replicas": 2,
            "rollout": {
                "inProgress": True,
                "stalledMinutes": 60,
                "reason": "ReplicaSetCreateError",
                "updatedReplicas": 0,
            },
            "containers": [clean_container("worker")],
        },
        {  # one replica, no declared reason -> scale
            "kind": "Deployment",
            "name": "cache",
            "replicas": 1,
            "rollout": {"inProgress": False},
            "containers": [clean_container("cache")],
        },
        {  # one replica, declared singleton -> not a finding
            "kind": "Deployment",
            "name": "leader",
            "replicas": 1,
            "singleton": True,
            "rollout": {"inProgress": False},
            "containers": [clean_container("leader")],
        },
        {
            "kind": "Deployment",
            "name": "billing",
            "replicas": 3,
            "rollout": {"inProgress": False},
            "containers": [
                {  # probe missing, health signal known -> patch it in
                    "name": "probe-known",
                    "image": "registry.example.com:5000/billing@sha256:" + "b" * 64,
                    "healthPath": "/readyz",
                    "healthPort": 8080,
                },
                {  # probe missing, health signal unknown -> blocked, never guessed
                    "name": "probe-unknown",
                    "image": "registry.example.com:5000/ledger@sha256:" + "c" * 64,
                },
                {  # requests far above observed P95, with a memory limit under the correction
                    "name": "fat",
                    "image": "registry.example.com:5000/fat@sha256:" + "d" * 64,
                    "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8080}},
                    "requests": {"cpu": 2000, "memory": 4096},
                    "limits": {"memory": 512},
                    "usageP95": {"cpu": 100, "memory": 300},
                },
                {  # `:latest`, with the running digest to hand -> pin to what is already running
                    "name": "latest-pinnable",
                    "image": "registry.example.com:5000/app:latest",
                    "runningDigest": "sha256:" + "e" * 64,
                    "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8080}},
                },
                {  # no tag at all and no digest to hand -> blocked, never a guessed tag
                    "name": "latest-unpinnable",
                    "image": "registry.example.com:5000/legacy",
                    "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8080}},
                },
            ],
        },
    ],
    "persistentVolumeClaims": [
        {"name": "scratch", "unusedDays": 40, "capacity": "50Gi", "uid": "9d1c-scratch"},  # -> gated delete
        {"name": "live", "unusedDays": 90, "consumers": ["api"]},  # in use -> not a finding
        {"name": "recent", "unusedDays": 3},  # not sustained -> not a finding
    ],
    "alerts": [
        {
            "name": "cpu-throttle",
            "firesPerDay": 24,
            "actionedRatio": 0.0,
            "target": {
                "group": "monitoring.coreos.com",
                "version": "v1",
                "kind": "PrometheusRule",
                "namespace": "team-x",
                "name": "team-x-rules",
            },
            "rulePath": "/spec/groups/0/rules/2",
            "p90SelfResolveSeconds": 180,
            "forSeconds": 60,
        },
        {"name": "pod-restart", "firesPerDay": 30, "actionedRatio": 0.02},  # -> blocked
        {"name": "page-me", "firesPerDay": 10, "actionedRatio": 0.9},  # acted on -> not a finding
    ],
}

DEVELOPER_TEAM_STATE_CLEAN = {
    "namespace": "team-x",
    "workloads": [
        {
            "kind": "Deployment",
            "name": "api",
            "replicas": 3,
            "rollout": {"inProgress": False},
            "containers": [clean_container("api")],
        }
    ],
    "persistentVolumeClaims": [{"name": "data", "unusedDays": 2}],
    "alerts": [{"name": "page-me", "firesPerDay": 1, "actionedRatio": 0.9}],
}


def _unblock_platform(state: dict) -> dict:
    """Give cluster-c the cloud resource path its skew finding was blocked on."""
    for cluster in state["clusters"]:
        if cluster["name"] == "cluster-c":
            cluster["resource"] = "projects/kage-test/locations/us-east4-b/clusters/cluster-c"
    return state


def _unblock_cluster_admin(state: dict) -> dict:
    """Give the csi-driver add-on the workload reference and image its finding was blocked on."""
    for addon in state["addons"]:
        if addon["name"] == "csi-driver":
            addon["target"] = {
                "group": "apps",
                "version": "v1",
                "kind": "DaemonSet",
                "namespace": "gce-pd-csi-driver",
                "name": "csi-gce-pd-node",
            }
            addon["container"] = "gce-pd-driver"
            addon["supportedImage"] = "registry.example.com:5000/csi@sha256:" + "f" * 64
    return state


def _unblock_developer_team(state: dict) -> dict:
    """Give the unpinned image the digest it is running right now."""
    for workload in state["workloads"]:
        for container in workload.get("containers", []):
            if container["name"] == "latest-unpinnable":
                container["runningDigest"] = "sha256:" + "1" * 64
    return state


@dataclass(frozen=True)
class TierFixture:
    """Everything tier-shaped about a tier, in one place so the roster guard can see it.

    The inventories cannot be derived — each tier surveys a different subject — so the mechanized
    protection is the opposite one: `test_every_tier_has_a_fixture` fails loudly when a tier appears
    with no entry here, rather than the suite silently covering two of three.
    """

    drifty: dict
    clean: dict
    unblock: Callable[[dict], dict]
    blocked_check: str  # the check whose blocked finding `unblock` turns into an operation
    blocked_subject: str


FIXTURES: dict[str, TierFixture] = {
    "platform": TierFixture(
        PLATFORM_FLEET, PLATFORM_FLEET_CLEAN, _unblock_platform, "fleet-version-skew", "cluster/cluster-c"
    ),
    "cluster-admin": TierFixture(
        CLUSTER_ADMIN_STATE,
        CLUSTER_ADMIN_STATE_CLEAN,
        _unblock_cluster_admin,
        "addon-behind-supported",
        "addon/csi-driver",
    ),
    "developer-team": TierFixture(
        DEVELOPER_TEAM_STATE,
        DEVELOPER_TEAM_STATE_CLEAN,
        _unblock_developer_team,
        "image-unpinned",
        "billing/latest-unpinnable",
    ),
}


class DriftCase(unittest.TestCase):
    """Fixtures on disk, one tmpdir for the whole class. Nothing here touches the repo tree."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="detect-drift-")
        cls.dir = Path(cls._tmp.name)
        cls.desired = write_json(cls.dir, "desired.json", DESIRED)
        cls.live_clean = write_json(cls.dir, "live-clean.json", LIVE_CLEAN)
        cls.live_drifted = write_json(cls.dir, "live-drifted.json", LIVE_DRIFTED)
        cls.baseline = write_json(cls.dir, "baseline.json", TENANCY_BASELINE)
        cls.inventory = {t: write_json(cls.dir, f"{t}-drifty.json", FIXTURES[t].drifty) for t in FIXTURES}
        cls.inventory_clean = {t: write_json(cls.dir, f"{t}-clean.json", FIXTURES[t].clean) for t in FIXTURES}

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def survey(self, tier: str, *extra: str) -> dict[str, Any]:
        return report(tier, [survey_flag(tier), self.inventory[tier], *extra])


# --- the roster --------------------------------------------------------------------------------------


class TestTheRosterIsDerived(unittest.TestCase):
    """Three independent statements of who ships detect-drift, held equal to each other."""

    def test_the_filesystem_and_the_spec_agree_on_the_tier_list(self):
        self.assertGreaterEqual(len(TIERS), 3, f"only {TIERS} under agents/ — 02 §2 defines three personas")
        self.assertEqual(
            set(SPEC_DRIFT_TIERS),
            set(TIERS),
            f"02 §2.1 marks detect-drift cross-cutting for {sorted(SPEC_DRIFT_TIERS)} but agents/ holds "
            f"{sorted(TIERS)}. One of the two grew a tier the other has not heard of.",
        )

    def test_every_tier_ships_the_skill_and_the_script(self):
        for tier in TIERS:
            with self.subTest(tier=tier):
                self.assertTrue(skill_md(tier).is_file(), f"{tier} has no {SKILL}/SKILL.md")
                self.assertTrue(script_path(tier).is_file(), f"{tier} has no {SKILL}/scripts/detect_drift.py")
                self.assertTrue(envelope_path(tier).is_file(), f"{tier} has no scripts/action_envelope.py")

    def test_every_tier_has_a_fixture(self):
        """The LSN-036 guard for the one thing in this file that cannot be derived.

        Each tier surveys a different subject, so its inventory has to be written by hand. What can
        be mechanized is the failure: a fourth tier makes this red instead of making the suite
        quietly cover three of four.
        """
        self.assertEqual(
            set(FIXTURES),
            set(TIERS),
            f"no drift fixture for {sorted(set(TIERS) - set(FIXTURES))}; this suite would silently stop "
            "covering that tier",
        )

    def test_the_subject_flag_is_derivable_for_every_tier(self):
        for tier in TIERS:
            with self.subTest(tier=tier):
                self.assertIn("--desired", subject_flags(script_path(tier).read_text()))
                self.assertTrue(survey_flag(tier).startswith("--"))


# --- the shared diff core ------------------------------------------------------------------------------


# Deliberately per-tier: the four places the three scripts are supposed to differ. Everything else
# that appears in all three must be identical.
DIVERGES_BY_DESIGN = frozenset({"finding", "parse_args", "render", "run"})

# Named so that deleting or renaming one of them is a red line rather than a smaller shared set.
THE_DIFF_CORE = ("IGNORE_KEYS", "IGNORE_ANNOTATIONS", "strip", "_stable", "find_drift", "object_slug", "target_of")


class TestTheDiffCoreIsShared(unittest.TestCase):
    """One diff implementation living in three files, pinned so it cannot diverge in one of them.

    WHY AST EQUALITY AND NOT A BYTE HASH. The three copies are not byte-identical and should not be:
    `strip`'s docstring says "mirror-authored" on platform, "baseline-authored" on cluster-admin and
    "manifest-authored" on developer-team, which is correct prose about identical code. A byte hash
    would go red on that and the one-line green would be to delete the wording — a check that makes
    the artifact worse to satisfy itself. `ast.dump` of the docstring-stripped node compares what
    executes and nothing else, which is the property that actually matters: a divergence in `strip`
    means one tier stops ignoring `resourceVersion` and reports drift on every object it looks at.
    """

    def setUp(self):
        self.defs = {tier: top_level_defs(script_path(tier).read_text()) for tier in TIERS}

    def test_the_named_diff_core_exists_in_every_tier(self):
        for name in THE_DIFF_CORE:
            for tier in TIERS:
                with self.subTest(name=name, tier=tier):
                    self.assertIn(name, self.defs[tier], f"{tier} no longer defines {name}")

    def test_every_shared_name_is_identical_or_declared_divergent(self):
        common = set.intersection(*(set(d) for d in self.defs.values()))
        self.assertGreaterEqual(len(common), len(THE_DIFF_CORE), "the shared surface has collapsed")
        for name in sorted(common - DIVERGES_BY_DESIGN):
            with self.subTest(name=name):
                shapes = {tier: self.defs[tier][name] for tier in TIERS}
                differing = sorted(t for t in TIERS if shapes[t] != shapes[TIERS[0]])
                self.assertFalse(
                    differing,
                    f"`{name}` differs on {differing}. It is defined in every tier and is not on the "
                    "divergent-by-design list, so one copy has been edited and the others have not.",
                )

    def test_the_divergent_list_is_not_stale(self):
        """An allowlist that outlives the thing it excuses is the next LSN-036."""
        for name in sorted(DIVERGES_BY_DESIGN):
            with self.subTest(name=name):
                present = [t for t in TIERS if name in self.defs[t]]
                self.assertEqual(present, list(TIERS), f"{name} is excused from the shared check but only {present} "
                                                       "define it")
                shapes = {self.defs[t][name] for t in TIERS}
                self.assertGreater(
                    len(shapes),
                    1,
                    f"`{name}` is on the divergent-by-design list but every tier's copy is now identical. "
                    "Either it belongs in the shared core, or the list is excusing nothing.",
                )


# --- exit codes ----------------------------------------------------------------------------------------


class TestExitCodes(DriftCase):
    """0 clean, 2 drift, 1 error — the contract every caller of this script reads.

    A drift sweep is driven by cron and by the agent's own turn, and both branch on the code. A
    tier that returned 0 on drift would report a clean fleet forever.
    """

    def test_zero_when_the_object_matches(self):
        for tier in TIERS:
            with self.subTest(tier=tier):
                code, out, _ = cli(tier, ["--desired", self.desired, "--live", self.live_clean])
                self.assertEqual(code, 0, f"{tier} reported drift on a clean object: {out}")
                self.assertIn("no drift", out)

    def test_zero_when_the_inventory_is_clean(self):
        for tier in TIERS:
            with self.subTest(tier=tier):
                code, out, err = cli(tier, [survey_flag(tier), self.inventory_clean[tier]])
                self.assertEqual(code, 0, f"{tier} reported drift on a clean inventory: {out}{err}")

    def test_two_when_the_object_has_drifted(self):
        for tier in TIERS:
            with self.subTest(tier=tier):
                code, out, _ = cli(tier, ["--desired", self.desired, "--live", self.live_drifted])
                self.assertEqual(code, 2, f"{tier} missed a dropped policyType")
                self.assertIn("policyTypes", out)

    def test_two_when_the_inventory_has_drifted(self):
        for tier in TIERS:
            with self.subTest(tier=tier):
                code, out, err = cli(tier, [survey_flag(tier), self.inventory[tier]])
                self.assertEqual(code, 2, f"{tier} found nothing in an inventory built to drift: {out}{err}")

    def test_one_when_desired_arrives_without_live(self):
        for tier in TIERS:
            with self.subTest(tier=tier):
                code, _, err = cli(tier, ["--desired", self.desired])
                self.assertEqual(code, 1, f"{tier} did not report a usage error")
                self.assertIn("--live", err)

    def test_one_when_an_input_cannot_be_read(self):
        missing = str(self.dir / "does-not-exist.json")
        for tier in TIERS:
            with self.subTest(tier=tier):
                code, _, err = cli(tier, [survey_flag(tier), missing])
                self.assertEqual(code, 1, f"{tier} did not report an IO error")
                self.assertIn("detect-drift:", err)

    def test_a_malformed_inventory_is_an_error_and_not_a_clean_report(self):
        """The dangerous failure is 0, not 1: a garbled capture read as "no drift"."""
        bad = self.dir / "not-json.json"
        bad.write_text("{ this is not json", encoding="utf-8")
        for tier in TIERS:
            with self.subTest(tier=tier):
                code, _, _ = cli(tier, [survey_flag(tier), str(bad)])
                self.assertNotEqual(code, 0, f"{tier} read an unparseable inventory as a clean fleet")

    def test_a_missing_subject_flag_is_never_success(self):
        """DOCUMENTS A DEFECT RATHER THAN ASSERTING THE BUG.

        The module docstring says "1 = error", but the required mutually-exclusive subject group is
        enforced by argparse, which exits **2** — the same code as "drift found". A cron wrapper
        that branches on 2 reads a usage error as a fleet that drifted. Reported for routing, not
        fixed here; the assertion is the one that holds either way, so this line stays green
        whichever code the fix settles on.
        """
        for tier in TIERS:
            with self.subTest(tier=tier):
                code, _, _ = cli(tier, [])
                self.assertNotEqual(code, 0, f"{tier} exited 0 with no subject argument")


# --- the diff properties that survived the conversion ----------------------------------------------------


class TestTheDiffIsDesiredAuthoritative(DriftCase):
    """Carried over from the suite this file replaces — these were never about the pull request."""

    def test_server_defaults_and_bookkeeping_are_not_drift(self):
        """`uid`, `resourceVersion`, `managedFields`, `status`, the noisy annotations, and a field
        live added that desired never specified. Any of them counting would produce a remediation
        that changes nothing, on every sweep, forever."""
        for tier in TIERS:
            with self.subTest(tier=tier):
                out = report(tier, ["--desired", self.desired, "--live", self.live_clean])
                self.assertFalse(out["drift"], f"{tier} reported drift on server defaults: {out['findings']}")
                self.assertEqual(out["operations"], [])

    def test_a_dropped_field_is_drift_and_carries_an_apply_of_the_whole_object(self):
        """`apply`, not `patch`: a patch would leave a hand-deleted field deleted."""
        for tier in TIERS:
            with self.subTest(tier=tier):
                out = report(tier, ["--desired", self.desired, "--live", self.live_drifted])
                self.assertTrue(out["drift"])
                self.assertEqual(len(out["operations"]), 1, out["operations"])
                op = out["operations"][0]
                self.assertEqual(op["op"], "apply")
                self.assertEqual(op["desiredState"], DESIRED, "the remediation must re-assert the authored object")
                self.assertEqual(
                    op["target"],
                    {
                        "group": "networking.k8s.io",
                        "version": "v1",
                        "kind": "NetworkPolicy",
                        "name": "default-deny",
                        "namespace": "team-x",
                    },
                )

    def test_the_ignore_set_is_the_same_set_in_every_tier(self):
        first = drift(TIERS[0])
        for tier in TIERS:
            with self.subTest(tier=tier):
                self.assertEqual(drift(tier).IGNORE_KEYS, first.IGNORE_KEYS)
                self.assertEqual(drift(tier).IGNORE_ANNOTATIONS, first.IGNORE_ANNOTATIONS)
        for key in ("managedFields", "resourceVersion", "uid", "creationTimestamp", "generation", "selfLink"):
            self.assertIn(key, first.IGNORE_KEYS)
        self.assertNotIn("status", first.IGNORE_KEYS, "status is dropped by name in strip(), not via the set")
        self.assertEqual(first.strip({"status": {"x": 1}, "spec": {"a": 1}}), {"spec": {"a": 1}})

    def test_detection_never_writes_to_its_inputs(self):
        """Read-only, byte for byte. The reader identity holds no write verb (03 §3.1); a script
        that edited its own capture would be doing something the pod cannot do and the operator
        would find out from an alarm, not from here."""
        watched = [Path(self.desired), Path(self.live_drifted), Path(self.baseline)] + [
            Path(p) for p in self.inventory.values()
        ]
        before = {p: p.read_bytes() for p in watched}
        for tier in TIERS:
            cli(tier, ["--desired", self.desired, "--live", self.live_drifted, "--json"])
            cli(tier, [survey_flag(tier), self.inventory[tier], "--json"])
        cli("cluster-admin", ["--cluster", self.inventory["cluster-admin"], "--baseline", self.baseline, "--json"])
        for path, content in before.items():
            with self.subTest(path=path.name):
                self.assertEqual(path.read_bytes(), content, f"{path.name} was modified by a read-only sweep")


# --- the envelope join -----------------------------------------------------------------------------------

SCOPE_IDENTITY = {tier: f"{tier}/kage-test/cluster-a/team-x" for tier in TIERS}
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"


def build(tier: str, operations: list[dict]) -> dict:
    api = envelope(tier)
    return api.build_envelope(
        agent_identity=SCOPE_IDENTITY[tier],
        intent="close the drift this sweep found",
        operations=operations,
        requester={"kind": "agent", "id": SCOPE_IDENTITY[tier]},
        trigger={"source": "cron"},
        trace={"traceId": TRACE_ID},
        nonce=api.new_nonce(),
    )


class TestEveryOperationBuildsAnEnvelope(DriftCase):
    """The join the whole conversion rests on.

    A finding is worth nothing until `apply-change`'s `submit_action` can put its operations in an
    envelope the broker accepts. `build_envelope` refuses locally what the broker refuses remotely —
    an unknown verb, a missing target, two target shapes at once, an unknown patch media type, a bad
    propagation policy. Running the scripts' real output through it is the difference between "the
    operations look right" and "the operations are submittable".
    """

    def test_the_whole_sweep_builds_one_envelope(self):
        for tier in TIERS:
            with self.subTest(tier=tier):
                operations = self.survey(tier)["operations"]
                self.assertTrue(operations, f"{tier}'s drifty inventory produced no operations at all")
                built = build(tier, operations)
                self.assertEqual(built["kind"], "ActionEnvelope")
                self.assertTrue(built["idempotencyKey"].startswith("sha256:"))
                self.assertEqual(built["operations"], operations, "build_envelope must not rewrite the operations")

    def test_each_operation_builds_on_its_own(self):
        """One at a time, so a refusal names the operation rather than the sweep."""
        for tier in TIERS:
            for index, operation in enumerate(self.survey(tier)["operations"]):
                with self.subTest(tier=tier, op=index, verb=operation.get("op")):
                    build(tier, [operation])

    def test_the_object_remediation_builds_too(self):
        for tier in TIERS:
            with self.subTest(tier=tier):
                out = report(tier, ["--desired", self.desired, "--live", self.live_drifted])
                build(tier, out["operations"])

    def test_the_baseline_creates_build(self):
        """cluster-admin's namespace baseline, stamped from the objects `--baseline` supplied."""
        out = report("cluster-admin", ["--cluster", self.inventory["cluster-admin"], "--baseline", self.baseline])
        creates = [op for op in out["operations"] if op["op"] == "create"]
        self.assertTrue(creates, "no baseline object was created for a namespace missing all three kinds")
        build("cluster-admin", creates)
        for op in creates:
            self.assertEqual(op["target"]["namespace"], "team-z", "a baseline stamped into the wrong namespace")

    def test_the_envelope_carries_no_tier_scope_or_risk_claim(self):
        """03 §4.1 step 1: the broker derives (tier, scope) from the authenticated identity. A field
        claiming either could only ever be an attempt to override it."""
        for tier in TIERS:
            with self.subTest(tier=tier):
                built = build(tier, self.survey(tier)["operations"])
                for forbidden in ("tier", "scope", "riskClass", "risk_class", "risk", "approved"):
                    self.assertNotIn(forbidden, built)

    def test_every_verb_is_one_the_contract_closes_over(self):
        valid = envelope(TIERS[0]).VALID_OPS
        for tier in TIERS:
            for op in self.survey(tier)["operations"]:
                with self.subTest(tier=tier, verb=op.get("op")):
                    self.assertIn(op["op"], valid)


# --- tier scope containment ------------------------------------------------------------------------------

# 03 §3.2, Developer Team row: "Any other namespace; cluster or project scope; cluster-scoped
# objects". Every one of these is a collection whose mere READ is refused by RBAC on that pod, so a
# tier that names one in a `.get` is describing an input it can never be handed.
CLUSTER_SCOPED_READS = frozenset(
    {
        "nodes",
        "nodePools",
        "nodePool",
        "nodeCount",
        "utilizationP95",
        "addons",
        "clusters",
        "cluster",
        "namespaces",
        "storageClasses",
        "persistentVolumes",
        "clusterRoles",
        "governedNamespaces",
    }
)

CLUSTER_SCOPED_KINDS = frozenset(
    {
        "Node",
        "NodePool",
        "StorageClass",
        "PersistentVolume",
        "Namespace",
        "ClusterRole",
        "ClusterRoleBinding",
        "CustomResourceDefinition",
        "ValidatingAdmissionPolicy",
        "MutatingWebhookConfiguration",
    }
)


class TestTierScopeContainment(DriftCase):
    """The security-relevant arms. Each carries a live control so it cannot pass by reading nothing.

    The failure being guarded is not an agent that misbehaves — it is a *script* that hands the
    broker an operation outside its tier's scope. The broker would refuse it (03 §4.1 step 3, one
    out-of-scope target rejects the whole envelope), so the visible symptom is a drift sweep whose
    every remediation is rejected, reported to the team as the tier being broken.
    """

    def test_the_mesh_verbs_a_tier_can_emit_are_the_ones_02_grants_it(self):
        """Read out of 02 §2.1's own table, in the containment direction only.

        Only the *absence* is asserted: a tier 02 does not grant `delegate` must not have a
        `delegate=` shape. The converse would be wrong — cluster-admin is granted `escalate` and its
        detect-drift legitimately never uses it, because none of its five subjects is above its own
        scope.
        """
        self.assertTrue(MESH["delegate"] and MESH["escalate"], "02 §2.1's delegate/escalate rows did not parse")
        for tier in TIERS:
            kwargs = finding_kwargs(script_path(tier).read_text())
            for verb in ("delegate", "escalate"):
                with self.subTest(tier=tier, verb=verb):
                    if tier not in MESH[verb]:
                        self.assertNotIn(
                            verb,
                            kwargs,
                            f"{tier}'s finding() can carry `{verb}`, which 02 §2.1 does not grant that tier. "
                            f"{verb} for {tier} is not a hop it has a skill for.",
                        )

    def test_developer_team_has_escalate_and_no_delegate_or_handoff(self):
        """Its only upward move. `delegate` reaches down and there is nothing below a namespace;
        `handoff` names a sibling skill, and the two provisioning skills are the tiers' above."""
        kwargs = finding_kwargs(script_path("developer-team").read_text())
        self.assertIn("escalate", kwargs)
        self.assertNotIn("delegate", kwargs)
        self.assertNotIn("handoff", kwargs)

    def test_every_handoff_names_a_skill_that_tier_actually_ships(self):
        """Derived from the filesystem: a handoff to a skill that is not installed is a dead end the
        agent discovers at runtime and reports as prose."""
        fired = 0
        for tier in TIERS:
            for target in handoff_targets(script_path(tier).read_text()):
                fired += 1
                with self.subTest(tier=tier, handoff=target):
                    self.assertTrue(
                        (AGENTS / tier / "skills" / target / "SKILL.md").is_file(),
                        f"{tier} hands off to `{target}`, which it does not ship",
                    )
        self.assertGreater(fired, 0, "no handoff literal found in any tier — the scan is reading nothing")

    def test_developer_team_reads_nothing_cluster_scoped(self):
        keys = read_keys(script_path("developer-team").read_text())
        trespass = keys & CLUSTER_SCOPED_READS
        self.assertFalse(
            trespass,
            f"developer-team's detect-drift reads {sorted(trespass)} out of its inventory. 03 §3.2 stops that "
            "tier at the namespace edge — an attempt to read one of these is refused by RBAC, not merely "
            "discouraged, so the check would be dead code that reports nothing.",
        )

    def test_the_cluster_scoped_read_scan_fires_on_a_tier_that_does_read_them(self):
        """Live control. Without it, a `read_keys` that returned the empty set would pass the arm
        above forever."""
        hits = {t: read_keys(script_path(t).read_text()) & CLUSTER_SCOPED_READS for t in TIERS}
        firing = {t: sorted(v) for t, v in hits.items() if v}
        self.assertTrue(
            firing,
            "no tier reads a cluster-scoped collection at all, which means the scan above is vacuous",
        )
        self.assertNotIn("developer-team", firing)

    def test_developer_team_emits_only_namespaced_targets_inside_its_own_namespace(self):
        operations = self.survey("developer-team")["operations"]
        self.assertTrue(operations)
        for index, op in enumerate(operations):
            with self.subTest(op=index, verb=op.get("op")):
                self.assertNotIn("cloudTarget", op, "a namespace-scoped tier has no cloud API to call")
                self.assertNotIn("targetSelector", op, "a selector fans out; this tier names its objects")
                target = op["target"]
                self.assertNotIn(target["kind"], CLUSTER_SCOPED_KINDS, f"{target['kind']} is cluster-scoped")
                self.assertEqual(
                    target.get("namespace"),
                    DEVELOPER_TEAM_STATE["namespace"],
                    f"an operation reaches {target.get('namespace')!r}, outside this agent's one namespace",
                )

    def test_the_cloud_target_arm_fires_on_a_tier_that_does_emit_one(self):
        """Live control for the `cloudTarget` assertion above."""
        emitters = [
            t for t in TIERS if any("cloudTarget" in op for op in self.survey(t)["operations"])
        ]
        self.assertTrue(emitters, "no tier emits a cloudTarget, so the developer-team arm is vacuous")
        self.assertNotIn("developer-team", emitters)

    def test_developer_team_declares_no_cluster_or_fleet_flag(self):
        flags = declared_flags(script_path("developer-team").read_text())
        trespass = {f for f in flags if re.search(r"cluster|fleet|node|project", f)}
        self.assertFalse(trespass, f"developer-team's parser offers {sorted(trespass)}")
        other = {f for t in TIERS if t != "developer-team" for f in declared_flags(script_path(t).read_text())}
        self.assertTrue(
            {f for f in other if re.search(r"cluster|fleet|node|project", f)},
            "no other tier declares a cluster/fleet flag either, so the arm above proves nothing",
        )

    def test_a_cluster_internal_finding_is_delegated_and_never_applied(self):
        """The other half of containment: a tier that CAN see something it may not write hands it
        one hop down rather than reaching in (02 §3 for platform, 02 §4 for cluster-admin)."""
        delegated = 0
        for tier in TIERS:
            if tier not in MESH["delegate"]:
                continue
            child = MESH["delegate"][tier]
            for found in self.survey(tier)["findings"]:
                if "delegate" not in found:
                    continue
                delegated += 1
                with self.subTest(tier=tier, check=found["check"]):
                    self.assertNotIn(
                        "operations",
                        found,
                        f"{tier} delegates {found['check']} AND submits it itself — the broker would refuse "
                        "the write and the callee would do it anyway",
                    )
                    self.assertTrue(
                        found["delegate"].startswith(child),
                        f"{tier} delegates to {found['delegate']!r}; 02 §2.1 says its one hop is {child}",
                    )
        self.assertGreater(delegated, 0, "no tier produced a delegated finding — the fixtures stopped covering it")

    def test_an_out_of_scope_workload_finding_escalates_and_never_acts(self):
        for tier in TIERS:
            if tier not in MESH["escalate"]:
                continue
            parent = MESH["escalate"][tier]
            escalations = [f for f in self.survey(tier)["findings"] if "escalate" in f]
            if tier == "developer-team":
                self.assertTrue(escalations, "the capacity-stuck rollout must escalate, not roll back")
            for found in escalations:
                with self.subTest(tier=tier, check=found["check"]):
                    self.assertNotIn("operations", found, "an escalated finding must not also be acted on")
                    self.assertEqual(found["escalate"], parent)

    def test_a_handed_off_finding_carries_no_operations_of_its_own(self):
        """02 §6: the child bundle is rendered by the provisioning skill from the tier template,
        which is the mechanism that makes an over-grant inexpressible (03 §4.2). A second copy built
        here would be a second copy that drifts."""
        handed = 0
        for tier in TIERS:
            for found in self.survey(tier)["findings"]:
                if "handoff" in found:
                    handed += 1
                    with self.subTest(tier=tier, check=found["check"]):
                        self.assertNotIn("operations", found)
        self.assertGreater(handed, 0, "no tier produced a handoff finding — the fixtures stopped covering it")


# --- no self-classification -------------------------------------------------------------------------------


class TestNoScriptStatesARiskClass(DriftCase):
    """03 §4.1 step 4 and 03 §5: the broker classifies, from the objects and the diff.

    A script that decided its own class would be deciding whether its own change needs a human, and
    the interesting direction is not the honest mistake — it is the shape where a remediation gets
    reworded until it classifies lower. Nothing in the script may state a class, so there is nothing
    to reword.
    """

    def test_no_executable_line_names_a_risk_class(self):
        for tier in TIERS:
            with self.subTest(tier=tier):
                hits = risk_vocabulary(script_path(tier).read_text())
                self.assertFalse(
                    hits,
                    f"{tier}'s detect_drift names {sorted(hits)} in executable content. Risk is the broker's "
                    "to compute; a script that states one has taken a decision it may not take.",
                )

    def test_no_finding_or_operation_carries_a_risk_claim(self):
        for tier in TIERS:
            with self.subTest(tier=tier):
                out = self.survey(tier)
                hits = risk_flavoured(out["findings"]) | risk_flavoured(out["operations"])
                self.assertFalse(hits, f"{tier} emitted {sorted(hits)}")

    def test_the_gated_remediation_is_submitted_and_not_reshaped(self):
        """Deleting a PVC is gated for developer-team (02 §5), and the script submits it anyway.

        The wrong repairs are both silent: skipping it (a decision the team never gets to make) and
        shrinking it into something that classifies lower. So the delete has to be in the
        operations, spelled as a delete.
        """
        operations = self.survey("developer-team")["operations"]
        deletes = [op for op in operations if op["op"] == "delete"]
        self.assertEqual(len(deletes), 1, f"expected exactly the orphaned PVC delete, got {deletes}")
        self.assertEqual(deletes[0]["target"]["kind"], "PersistentVolumeClaim")
        self.assertEqual(
            deletes[0]["delete"].get("preconditions", {}).get("uid"),
            "9d1c-scratch",
            "an approval that sits in the queue overnight must not land on a different PVC of the same name",
        )
        build("developer-team", deletes)


# --- hermetic ------------------------------------------------------------------------------------------------

ALLOWED_IMPORTS = frozenset({"__future__", "argparse", "json", "re", "sys", "yaml"})


class TestTheScriptsAreHermetic(DriftCase):
    """Detection is a pure function over JSON the agent already captured.

    Asserted twice, because the two failure modes are different. The import set catches the shape
    where a future edit reaches for `requests` or `broker_client`; the booby-trapped run catches the
    shape where something already imported opens a socket at survey time.
    """

    def test_the_import_set_is_stdlib_json_and_nothing_else(self):
        for tier in TIERS:
            with self.subTest(tier=tier):
                extra = imported_modules(script_path(tier).read_text()) - ALLOWED_IMPORTS
                self.assertFalse(extra, f"{tier}'s detect_drift imports {sorted(extra)}")

    def test_no_import_of_the_write_path_or_the_retired_pr_path(self):
        for tier in TIERS:
            imports = imported_modules(script_path(tier).read_text())
            for banned in ("submit_suggestion", "broker_client", "action_envelope", "socket", "subprocess", "os",
                           "http", "urllib", "requests", "google", "kubernetes"):
                with self.subTest(tier=tier, module=banned):
                    self.assertNotIn(banned, imports)

    def test_a_full_sweep_opens_no_socket_and_spawns_no_process(self):
        """Booby-trap the primitives, then run every tier's whole survey through them."""
        saved = (socket.socket, socket.create_connection, subprocess.Popen, subprocess.run)

        def boom(name):
            def _f(*_a, **_k):
                raise AssertionError(f"detect-drift called {name}()")

            return _f

        socket.socket = boom("socket.socket")
        socket.create_connection = boom("socket.create_connection")
        subprocess.Popen = boom("subprocess.Popen")
        subprocess.run = boom("subprocess.run")
        try:
            for tier in TIERS:
                with self.subTest(tier=tier):
                    self.assertEqual(cli(tier, [survey_flag(tier), self.inventory[tier], "--json"])[0], 2)
                    self.assertEqual(cli(tier, ["--desired", self.desired, "--live", self.live_drifted])[0], 2)
        finally:
            socket.socket, socket.create_connection, subprocess.Popen, subprocess.run = saved

    def test_no_script_reads_a_credential_path(self):
        """The pod holds no write credential (02 §2.2) and the reader token is the runtime's to
        present. A script that opened one of these would be reaching for something it must not have."""
        for tier in TIERS:
            source = ast.unparse(parsed(script_path(tier).read_text()))
            for secret in ("/var/run/secrets", ".kube/config", "KUBECONFIG", "GITHUB_TOKEN", "token", "credential"):
                with self.subTest(tier=tier, secret=secret):
                    self.assertNotIn(secret, source)


# --- blocked names one fact -----------------------------------------------------------------------------------


class TestBlockedNamesTheMissingFact(DriftCase):
    """"I cannot do this yet, and here is the one thing I need" — never a guess in an envelope.

    A readiness probe on the wrong path is an outage dressed as a fix, and a guessed image tag is a
    deploy nobody asked for. Asserted as a pair, because "it produced a blocked finding" is half the
    property: supply the missing fact and the SAME check must produce a real operation. A script
    that reported everything blocked would pass the first half on its own.
    """

    def test_every_tier_can_report_blocked(self):
        for tier in TIERS:
            with self.subTest(tier=tier):
                blocked = [f for f in self.survey(tier)["findings"] if "blocked" in f]
                self.assertTrue(blocked, f"{tier} never reports a finding blocked; it has nowhere to say 'I need X'")
                for found in blocked:
                    self.assertIsInstance(found["blocked"], str)
                    self.assertGreater(
                        len(found["blocked"]), 20, f"{found['check']} says {found['blocked']!r}, which names no fact"
                    )

    def test_every_check_that_can_block_does_block(self):
        """Every `blocked=` call site in the source is reached by the fixture, and vice versa.

        The half that matters is the first one. `test_every_tier_can_report_blocked` is satisfied by
        ANY blocked finding, so a single check quietly losing its blocked branch — one `or "/healthz"`
        on a missing readiness path, which is the guessed probe the skill calls "an outage dressed as
        a fix" — leaves the tier still reporting other checks blocked and the suite still green. This
        arm compares the set of checks that CAN block against the set that DID, so the branch has to
        stay reachable. It also fails when a fixture stops exercising a blocked path, which is the
        same failure seen from the other side.
        """
        for tier in TIERS:
            with self.subTest(tier=tier):
                can = blockable_checks(script_path(tier).read_text())
                self.assertTrue(can, f"{tier} has no blocked path at all; every gap becomes a guess")
                did = {f["check"] for f in self.survey(tier)["findings"] if "blocked" in f}
                self.assertEqual(
                    can,
                    did,
                    f"{tier}: checks that can block but did not = {sorted(can - did)}; "
                    f"blocked without a call site = {sorted(did - can)}",
                )

    def test_a_blocked_finding_still_carries_its_evidence_and_subject(self):
        """It has to be actionable by a human reading the report, not just by the next sweep."""
        for tier in TIERS:
            for found in self.survey(tier)["findings"]:
                if "blocked" not in found:
                    continue
                with self.subTest(tier=tier, check=found["check"]):
                    self.assertTrue(found["subject"])
                    self.assertTrue(found["evidence"])

    def test_supplying_the_missing_fact_turns_blocked_into_an_operation(self):
        for tier in TIERS:
            fixture = FIXTURES[tier]
            with self.subTest(tier=tier, check=fixture.blocked_check):
                before = self._named(self.survey(tier)["findings"], fixture)
                self.assertIn("blocked", before, f"{fixture.blocked_check} was not blocked to begin with")
                self.assertFalse(before.get("operations"), "a blocked finding must not also guess an operation")

                path = write_json(self.dir, f"{tier}-unblocked.json", fixture.unblock(clone(fixture.drifty)))
                after = self._named(report(tier, [survey_flag(tier), path])["findings"], fixture)
                self.assertNotIn("blocked", after, "the fact was supplied and the finding is still blocked")
                self.assertTrue(after.get("operations"), "the fact was supplied and no operation was produced")
                build(tier, after["operations"])

    def test_the_baseline_gap_names_the_kinds_it_will_not_author(self):
        """cluster-admin, specifically: it must never hand-author a tenancy baseline (a third copy
        of the quota is a third thing to drift). With a partial `--baseline` it may create the kind
        it was given and must stay blocked on the kinds it was not."""
        partial = write_json(self.dir, "baseline-partial.json", {"ResourceQuota": TENANCY_BASELINE["ResourceQuota"]})
        out = report("cluster-admin", ["--cluster", self.inventory["cluster-admin"], "--baseline", partial])
        gaps = [f for f in out["findings"] if f["check"] == "namespace-missing-baseline"]
        self.assertTrue(gaps)
        for found in gaps:
            with self.subTest(subject=found["subject"]):
                self.assertIn("blocked", found)
                self.assertIn("LimitRange", found["blocked"])
                self.assertIn("NetworkPolicy", found["blocked"])
                kinds = {op["target"]["kind"] for op in found.get("operations", [])}
                self.assertEqual(
                    kinds,
                    {"ResourceQuota"},
                    "the script invented a baseline object it was not given; the quota, limit range and "
                    "default-deny policy are the Platform Agent's to define",
                )

    @staticmethod
    def _named(findings: list[dict], fixture: TierFixture) -> dict:
        for found in findings:
            if found["check"] == fixture.blocked_check and found["subject"] == fixture.blocked_subject:
                return found
        raise AssertionError(f"no {fixture.blocked_check} finding for {fixture.blocked_subject} in {findings}")


# --- the pull-request path stays gone -----------------------------------------------------------------------

RETIRED_FLAGS = ("--emit-corrective", "--work-dir", "--object-path", "--dry-run", "--artifact-dir")


class TestThePullRequestPathIsGone(DriftCase):
    """02 §2.5.1 put "a pull request for work inside its own authority" on the footing of a failed
    action. The path is gone; this is the arm that keeps it gone.

    Every scan here is over executable content, because all three module docstrings still narrate
    the path they deleted — a grep over the raw file flags the paragraph that states the rule.
    """

    def test_no_script_imports_or_mentions_submit_suggestion(self):
        for tier in TIERS:
            with self.subTest(tier=tier):
                source = ast.unparse(parsed(script_path(tier).read_text()))
                self.assertNotIn("submit_suggestion", source)
                self.assertNotIn("submit-suggestion", source)

    def test_no_script_shells_out_to_git_or_gh(self):
        for tier in TIERS:
            source = ast.unparse(parsed(script_path(tier).read_text()))
            for token in ("git ", "gh pr", "git.", "pull request", "GitHub", "okf", "observation"):
                with self.subTest(tier=tier, token=token):
                    self.assertNotIn(token, source)

    def test_none_of_the_five_retired_flags_is_declared(self):
        for tier in TIERS:
            flags = declared_flags(script_path(tier).read_text())
            for retired in RETIRED_FLAGS:
                with self.subTest(tier=tier, flag=retired):
                    self.assertNotIn(retired, flags)

    def test_emit_operations_is_the_flag_that_replaced_them(self):
        for tier in TIERS:
            with self.subTest(tier=tier):
                self.assertIn("--emit-operations", declared_flags(script_path(tier).read_text()))
                code, out, _ = cli(tier, [survey_flag(tier), self.inventory[tier], "--emit-operations"])
                self.assertEqual(code, 2)
                operations = json.loads(out)
                self.assertIsInstance(operations, list, "--emit-operations must print a bare operations array")
                self.assertEqual(operations, self.survey(tier)["operations"])

    def test_the_skill_md_promises_only_flags_the_parser_declares(self):
        """A join, not a list restated here: the agent has nothing but this text to learn the CLI
        from, so a flag the skill shows and the parser does not take is a command the agent runs
        once, sees fail, and reports in prose."""
        for tier in TIERS:
            declared = declared_flags(script_path(tier).read_text())
            mentioned = set(re.findall(r"--[a-z][a-z0-9-]*", skill_md(tier).read_text()))
            with self.subTest(tier=tier):
                self.assertTrue(mentioned, f"{tier}'s SKILL.md shows no invocation at all")
                self.assertFalse(
                    mentioned - declared,
                    f"{tier}'s SKILL.md tells the agent to pass {sorted(mentioned - declared)}, which "
                    f"detect_drift.py does not accept",
                )
                self.assertIn("--emit-operations", mentioned, "the skill never shows how to get the operations out")

    def test_no_skill_md_still_documents_a_retired_flag(self):
        for tier in TIERS:
            text = skill_md(tier).read_text()
            for retired in RETIRED_FLAGS:
                with self.subTest(tier=tier, flag=retired):
                    self.assertNotIn(retired, text)


# --- non-vacuity ------------------------------------------------------------------------------------------------


class TestTheChecksAreNotVacuous(DriftCase):
    """Every scan in this file, run against something it must reject.

    A suite of vacuous passes reads green, and green is exactly what a reviewer checks. These arms
    exist so that a helper which silently starts returning the empty set — a changed AST shape, a
    renamed function, a regex that no longer matches — is caught by this file rather than by an
    incident. Same purpose as `test_the_rule_is_scanning_something` in `dev/test_apply_change_skill.py`.
    """

    def _mutate(self, tier: str, old: str, new: str) -> str:
        source = script_path(tier).read_text()
        self.assertIn(old, source, f"the mutation target {old!r} is no longer in {tier}'s script")
        return source.replace(old, new, 1)

    def test_the_shared_core_check_sees_a_divergence(self):
        """Take `resourceVersion` out of one tier's ignore-set — the change that would make that
        tier report drift on every object it looks at."""
        mutant = self._mutate("platform", '    "resourceVersion",\n', "")
        theirs = top_level_defs(mutant)["IGNORE_KEYS"]
        others = top_level_defs(script_path("cluster-admin").read_text())["IGNORE_KEYS"]
        self.assertNotEqual(theirs, others, "the shared-core comparison cannot see a changed ignore-set")

    def test_the_shared_core_check_ignores_a_docstring_edit(self):
        """The other direction: it must NOT go red on prose, or the one-line green is to delete the
        wording that makes each copy readable."""
        mutant = self._mutate("platform", "mirror-authored", "completely different prose here")
        self.assertEqual(
            top_level_defs(mutant)["strip"],
            top_level_defs(script_path("cluster-admin").read_text())["strip"],
            "the comparison is reading docstrings, so it will go red on correct code",
        )

    def test_the_mesh_verb_check_sees_a_delegate_grown_by_developer_team(self):
        mutant = self._mutate("developer-team", "    escalate: str | None = None,", "    delegate: str | None = None,")
        self.assertIn("delegate", finding_kwargs(mutant), "finding_kwargs cannot see a new hand-off shape")
        self.assertNotIn("developer-team", MESH["delegate"], "02 §2.1 now grants developer-team delegate")

    def test_the_cluster_scoped_read_check_sees_a_nodepool_read(self):
        mutant = self._mutate("developer-team", 'inventory.get("alerts")', 'inventory.get("nodePools")')
        self.assertTrue(read_keys(mutant) & CLUSTER_SCOPED_READS, "read_keys cannot see a cluster-scoped read")

    def test_the_risk_class_check_sees_a_self_classification(self):
        mutant = self._mutate(
            "platform", 'out: dict = {"check": check', 'out: dict = {"riskClass": "routine", "check": check'
        )
        hits = risk_vocabulary(mutant)
        self.assertTrue(hits, "risk_vocabulary cannot see a script classifying its own change")
        self.assertIn("class:routine", hits)

    def test_the_risk_claim_check_sees_one_in_emitted_data(self):
        found = clone(self.survey("platform")["findings"][0])
        found["riskClass"] = "elevated"
        self.assertTrue(risk_flavoured([found]), "risk_flavoured cannot see a risk claim on a finding")

    def test_the_import_check_sees_the_pr_path_coming_back(self):
        mutant = self._mutate("platform", "import argparse", "import argparse\nimport submit_suggestion")
        self.assertIn("submit_suggestion", imported_modules(mutant))
        self.assertTrue(imported_modules(mutant) - ALLOWED_IMPORTS)

    def test_the_retired_flag_check_sees_one_coming_back(self):
        mutant = self._mutate(
            "platform",
            'p.add_argument("--json"',
            'p.add_argument("--emit-corrective", action="store_true")\n    p.add_argument("--json"',
        )
        self.assertIn("--emit-corrective", declared_flags(mutant))

    def test_the_envelope_join_refuses_an_operation_the_broker_would(self):
        """The join is only worth having if `build_envelope` actually refuses things."""
        api = envelope("platform")
        good = self.survey("platform")["operations"]
        self.assertTrue(good)
        for name, broken in (
            ("unknown verb", [{**clone(good[0]), "op": "upsert"}]),
            ("no target shape", [{"op": "apply", "desiredState": {"kind": "ConfigMap"}}]),
            ("two target shapes", [{**clone(good[0]), "target": {"version": "v1", "kind": "Node", "name": "n"}}]),
            ("empty", []),
        ):
            with self.subTest(case=name):
                with self.assertRaises(api.EnvelopeError):
                    build("platform", broken)

    def test_the_fixtures_actually_exercise_every_finding_shape(self):
        """If a fixture stopped producing operations, or stopped producing blocked findings, most of
        this file would pass while testing nothing. Assert the shapes are present — not the counts,
        which are a property of these fixtures and not of the contract."""
        for tier in TIERS:
            out = self.survey(tier)
            shapes = {key for found in out["findings"] for key in found if key in
                      ("operations", "delegate", "handoff", "escalate", "blocked")}
            with self.subTest(tier=tier):
                self.assertIn("operations", shapes, f"{tier}'s fixture produces no remediation at all")
                self.assertIn("blocked", shapes, f"{tier}'s fixture never exercises the blocked path")
                upward = {"delegate", "handoff"} if tier != "developer-team" else {"escalate"}
                self.assertTrue(
                    shapes & upward,
                    f"{tier}'s fixture never exercises {sorted(upward)} — the containment arms have nothing to read",
                )


if __name__ == "__main__":
    unittest.main()
