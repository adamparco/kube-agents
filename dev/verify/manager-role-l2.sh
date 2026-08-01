#!/usr/bin/env bash
# V-CTN-017 at L2 — "The controller mints no RBAC — parse its ClusterRole, do not inspect it by
# eye ¬" (09 §6.1, levels L0 and L2, BLOCKING-ALWAYS, phase 8; source 08 §7).
#
# THE L0 HALF IS GREEN AND IT CANNOT REACH THIS.
# `dev/tests/controller-mints-no-rbac.py` parses `k8s-operator/config/` and the `+kubebuilder:rbac`
# markers in `k8s-operator/**/*.go` and proves eight properties over them: no verb on any RBAC
# resource, identity resources read-only, no credential subresource, no wildcard in any position,
# an allow-list of ordinary verbs, no `escalate`/`bind`/`impersonate`, marker set == rendered set
# triple-for-triple, and no dangling `roleRef`. Every one of those is a statement about FILES, and
# that check says so itself in its own docstring:
#
#     "The live half. A `ClusterRoleBinding` applied out of band, or an aggregation label pulling
#      the controller's SA into `cluster-admin`, is invisible to any parse of the repository."
#
# This is that half. Four things follow that only a cluster can answer:
#
#   1. A RULE THAT WAS NEVER APPLIED LOOKS IDENTICAL TO ONE THAT WAS. `git` records the grant; the
#      API server records the authority. On the day this script was written the two disagreed on
#      this very cluster — `k8s-operator/config/rbac/role.yaml` had gained
#      `authorization.k8s.io/subjectaccessreviews: create` hours earlier and the installed
#      `kubeagents-manager-role` did not have it. The L0 half was green throughout, correctly.
#   2. RBAC IS A UNION ACROSS EVERY BINDING, AND NO FILE CONTAINS THE UNION. A second
#      ClusterRoleBinding naming the controller's ServiceAccount and pointing at `cluster-admin`
#      adds `*/*/*` to what that identity may do, and adds nothing at all to the repository. It is
#      the shortest path from "the controller mints no RBAC" to "the controller mints anything",
#      and no parse of `config/` can see one step of it.
#   3. AN AGGREGATED ClusterRole HAS NO RULES IN THE TREE. `aggregationRule` is filled in by the
#      controller-manager at runtime from whatever ClusterRoles match its selector; the L0 half
#      refuses to read such a role (it reports it as unparsed rather than empty) precisely because
#      the tree does not contain the answer. The live object does — the API server writes the
#      resolved `rules:` back onto it — so this level can read what the tree cannot.
#   4. AN AUTHORIZER ANSWER IS NOT A RULE. Every property above is about objects. What a request is
#      actually permitted to do is a function of every rule, every binding, every aggregation and
#      every other authorizer in the chain, evaluated by the API server and by nothing else. The
#      sweep in L2-5 asks it directly.
#
# THE CORPUS IS THE WHOLE CONTROL PLANE, NAMED AFTER ITS CENTRE. `kubeagents-manager-role` is the
# role 08 §7's sentence is about and this script asserts it BY NAME (L2-1b, a hard non-vacuity
# arm). It is not the only identity the control plane runs as: the brake controller, the router and
# the leader-election Role are the same statement wearing three more hats, and an operator that
# minted RBAC through its router's ClusterRole would satisfy a check that only read the manager's.
# So the corpus is DISCOVERED — every Role and ClusterRole the tree installs, plus every role any
# binding drags in by naming a control-plane ServiceAccount (LSN-036: a roster rots, a derivation
# does not).
#
# ONE DEFINITION OF "VIOLATION", TWO LEVELS. The assertion block imports
# `dev/tests/controller-mints-no-rbac.py` BY PATH and calls its `scan()`, its verb allow-list, its
# signal strings and its `PROBE_ROLE`. It does not restate them. A rule reworded there is reworded
# here on the same commit, and the two arms cannot drift into disagreeing about what 08 §7 forbids.
#
# WHAT IS ASSERTED, in order:
#   L2-1  NON-VACUITY. The live corpus is discovered and is not empty (role floor, rule floor,
#         `kubeagents-manager-role` present by name, control-plane subjects found, the served
#         resource list read), and the imported scanner is shown to still fire — `PROBE_ROLE` must
#         provoke all eight of its signals through this script's own import of it ([[LSN-035]]).
#   L2-2  DRIFT, both directions. For every Role and ClusterRole the install tree defines, the live
#         object of the mapped name must grant the same (group, resource, verb) triples — nothing
#         missing, nothing extra. This is the arm that catches an unapplied commit and a hand-edited
#         live role with equal force.
#   L2-3  THE SCAN, over what is installed. `scan()` from the L0 half, run over every rule of every
#         live role in the corpus. Properties 2-6 of 08 §7, asserted against the cluster.
#   L2-4  THE BINDING SET. Every live ClusterRoleBinding and RoleBinding — in ANY namespace — whose
#         subjects name a control-plane ServiceAccount must be one the tree declares. An extra one
#         is the out-of-band grant of (2), named and printed.
#   L2-5  ROLEREF CLOSURE. Every one of those bindings must point at a role the tree declares and
#         that exists live. A `roleRef` to `cluster-admin` fails here by name, and its rules are
#         dragged into L2-3's scan besides, where they fire three wildcard findings.
#   L2-6  NO AGGREGATION. No role in the corpus carries an `aggregationRule`.
#   L2-7  THE AUTHORIZER SWEEP (A-1…A-4). Every question derived from the L0 half's own constants —
#         `RBAC_RESOURCES` x (`ORDINARY_VERBS` + `ESCALATION_VERBS`), the identity resources and
#         their credential subresources, `impersonate` on users and groups, and the superuser
#         question — asked of the live authorizer as each control-plane ServiceAccount, together
#         with POSITIVE CONTROLS derived from the grants the tree says that same identity holds. The
#         positive half is not decoration: a mistyped `--as=` subject answers `no` to everything,
#         and a negative-only sweep would call that a perfect pass.
#
# WHAT THIS DOES NOT CLAIM. It says nothing about the agent identities — those are V-BRK-013 and
# `actor-grant-sweep-l2.sh`. It says nothing about what the controller PROCESS does with the
# authority it holds; a controller that never uses its grant and one that uses it constantly are
# the same object here. And a namespaced RBAC grant to a control-plane SA in a namespace this
# script's sweep does not name is caught by L2-4 (which enumerates RoleBindings across ALL
# namespaces) rather than by the sweep, which asks its namespaced questions in the operator's own.
#
# DESTRUCTIVE-TEST GUARD: scratch-GKE contexts only, anchored. This run is READ-ONLY — it applies
# nothing, deletes nothing and patches nothing — and the guard is here anyway, for the reason
# LSN-005 exists: the guard is what keeps a later edit that adds a decoy binding from being one
# line away from adding it on the live install. A read-only script in `dev/verify/` is a script
# somebody will make write.
# Exit: 0 = PROVEN · 1 = FAILED · 2 = refused target, or the cluster cannot run the experiment.
# Usage: dev/verify/manager-role-l2.sh [kube-context]
#        dev/verify/manager-role-l2.sh --negative-control
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions):
#   P1 image-under-test: none — the subject of this check is not a first-party binary. Every claim
#      is about RBAC objects the API server holds and about the answers its authorizer gives; the
#      controller image could be any digest, or absent, and each property above would mean exactly
#      what it means now. Nor would a digest assertion be the stronger test here: L2-2 compares the
#      installed rules against the tree triple-for-triple, which detects a stale INSTALL directly
#      rather than by proxy — including the stale install of a perfectly current image.
#   P3 admission-recreate: none — nothing is created, so nothing can be grandfathered. This suite
#      writes no object at all; admission has no role in a run that only reads, and the objects it
#      reads were admitted by whatever rules were in force when the install ran, which is precisely
#      the state the check is trying to measure rather than a confound to control for.
#   P6 runtime-authoritative: every rule is read from the cluster with `kubectl get -o json`, and
#      every verdict in L2-7 comes from `kubectl auth can-i`, which is the API server's own
#      authorizer evaluating the union of every binding. The repository is read ONLY as the
#      expected value in L2-2 — never as the observed one, which is what the L0 half is for.
#   P9 status-polled: not applicable — no `.status` subtree is read, and no object is created whose
#      reconciliation would have to be awaited.
#   P10 substrate: p10_assert_control_plane_healthy before anything is collected.
set -uo pipefail

# MODES. `live` reads a real cluster and is what every claim above is about. `--negative-control`
# replays the same assertion block against synthesised live state and requires each of twelve
# injected defects to be caught by the arm that targets it, named in the output.
MODE=live
if [ "${1:-}" = "--negative-control" ]; then
  MODE=negative-control
  shift
fi

CTX="${1:-gke-scratch-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
K="kubectl --context $CTX"

# How many `auth can-i` calls are in flight at once. The table is ~250 questions and each one is an
# API round trip; serial is a minute of wall clock for no reason. Bounded rather than unbounded
# because this API server is shared with everything else running against the scratch cluster.
PARALLEL="${SWEEP_PARALLEL:-8}"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/manager-role-l2.XXXXXX")"
STATE="$WORK/state"
ANALYZE="$WORK/manager_role_assert.py"
QUESTIONS="$WORK/questions.tsv"
TRANSCRIPT="$WORK/transcript.tsv"
mkdir -p "$STATE"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

# NEGATIVE CONTROL DOES NOT EXERCISE: (LSN-060.) `--negative-control` SYNTHESISES the live state and
# the transcript from the install tree and hands them to the assertion block, so everything upstream
# of the block is unmeasured by it:
#   - every `kubectl` invocation. The `get -o json` collection, the `auth can-i` flag shapes,
#     `--subresource=`, `--as=`, the namespace axis. A malformed query answers nothing and A-2 is
#     what notices, but the control never runs one. ONE PART IS EXERCISED: `resource_word` is fired
#     directly at the end of the control run, so the `*/*)` refusal is known to trigger rather than
#     merely known to be written ([[LSN-044]]).
#   - CONTROL-PLANE SUBJECT DISCOVERY from live Deployments. The control is handed a state directory
#     that already contains the Deployment list; it never derives one from a cluster.
#   - the served-resource filter, which decides which positive controls are asked at all. The
#     control writes its own `api-resources.txt`.
#   - P10, the reachability probe, and the destructive-test guard — all live-mode only.
#   - the SHELL half of the transcript encoding. The control round-trips its synthesised answers
#     through the same TSV writer and reader the live path uses — which is why it now catches the
#     empty-subresource collapse — but the `while IFS=$'\t' read` loop that produces those lines on
#     a real run is shell, and only a real run executes it.
#   - the install itself. A synthesised state that mirrors the tree exactly is the baseline the
#     defects are injected into, so a REAL cluster disagreeing with the tree is exactly what the
#     control cannot tell you. That disagreement is L2-2, and it needs the cluster.
# What the control proves, and all it proves: the ten arms are not always-green, and each of the
# twelve defects below is caught by the arm that targets it, named in the output.

# Non-empty lines, on stdin or in a file, ALWAYS exiting 0. Not `grep -c .`: grep exits 1 on zero
# matches, so the idiomatic `$(grep -c . f || echo 0)` prints "0" from grep AND "0" from the
# fallback, and the two-line string it yields turns every downstream `[ "$n" -ge 6 ]` into
# "integer expression expected" ([[LSN-029]]).
count() { awk 'NF{n++} END{print n+0}' "$@"; }

fail=0
# EVERY ARM IS COUNTED AND THE COUNT IS ASSERTED AT THE END. `fail` stays 0 when no assertion runs,
# so a suite that skipped its whole body would print a PROVEN banner. Change EXPECTED_ASSERTIONS
# deliberately, in the same commit as the arm.
assertions=0
pass() {
  assertions=$((assertions + 1))
  echo "PASS: $1"
}
bad() {
  assertions=$((assertions + 1))
  echo "FAIL: $1"
  fail=1
}
note() { echo "  $1"; }

# LIVE: L2-1 (5 sub-arms are folded into one line each: corpus floor, manager-role by name, probe
# self-test, subjects, served list) + L2-2 + L2-3 + L2-4 + L2-5 + L2-6 + A-1…A-4 = 14.
# NEGATIVE CONTROL: the synthesised baseline (1) + twelve injected defects = 13.
if [ "$MODE" = negative-control ]; then
  EXPECTED_ASSERTIONS=13
else
  EXPECTED_ASSERTIONS=14
fi

cd "$REPO_ROOT" || exit 1

# ================================================================================================
# THE ASSERTION BLOCK, written out and then run. One file, three subcommands: `namespace` (where
# the install lives, read from the kustomization so this script and the installer cannot disagree),
# `questions` (the derived sweep table), `score` (the ten arms), `negative-control` (the same ten
# arms over synthesised defects).
# ================================================================================================
cat >"$ANALYZE" <<'PYEOF'
#!/usr/bin/env python3
"""The assertion block for V-CTN-017 at L2. Imports the L0 half rather than restating it.

Every notion of "what 08 §7 forbids" -- the RBAC resource set, the identity resources, the verb
allow-list, the escalation verbs, the wildcard, the eight signal strings, the probe document and
the scanner itself -- is read out of `dev/tests/controller-mints-no-rbac.py` at import time. This
file adds only what a cluster can answer: which objects are actually installed, which bindings
actually name a control-plane identity, and what the authorizer actually says.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import sys

# Fields of one sweep question, and of one transcript row (which is a question plus `answer`).
QCOLS = ("subject", "verb", "resource", "subresource", "scope", "expect")
# The wire form of "this question has no subresource". A sentinel rather than an empty column,
# because `while IFS=$'\t' read -r ...` in the asking loop CANNOT read an empty tab-delimited field:
# a tab is IFS whitespace, so bash collapses a run of them into one delimiter and every column after
# the empty one shifts left by one. Measured on the first live run -- 205 of 253 questions were
# asked with `scope` in the `subresource` slot, every one of them answered, and A-1 was the arm that
# noticed. An empty column is unreadable; a dash is not.
SUBRESOURCE_NONE = "-"

# The kustomize overlay that installs the control plane. It is the single definition site for the
# tree-name -> live-name mapping, so this file reads it rather than hardcoding `kubeagents-`.
KUSTOMIZATION = "k8s-operator/config/default/kustomization.yaml"
NAME_PREFIX_RE = re.compile(r"^namePrefix:\s*(\S+)\s*$", re.M)
NAMESPACE_RE = re.compile(r"^namespace:\s*(\S+)\s*$", re.M)

# The credential subresource of each identity resource, from the L0 half's own docstring on
# SIG_SUBRESOURCE: "`serviceaccounts/token` returns a bearer token for that SA and
# `certificatesigningrequests/approval` signs a client certificate". Not derived, because there is
# nothing to derive it from -- but GUARDED: if `IDENTITY_RESOURCES` grows a key this table does not
# know, the run fails rather than quietly sweeping one fewer credential path (LSN-036).
CREDENTIAL_SUBRESOURCE = {"serviceaccounts": "token", "certificatesigningrequests": "approval"}
# Which API group each identity resource lives in, and whether it is namespaced. Same guard.
IDENTITY_SHAPE = {
    "serviceaccounts": ("", "ns"),
    "certificatesigningrequests": ("certificates.k8s.io", "cluster"),
}
# Namespaced members of RBAC_RESOURCES. A namespaced question in the operator's own namespace
# DOMINATES the cluster-wide one -- `-n NS` answers yes for a ClusterRole grant and for a local
# RoleBinding alike, where `-A` only answers yes for the former -- so each kind is asked at the
# scope that can see the most. A RoleBinding in some OTHER namespace is out of the sweep's reach by
# construction and is L2-4's property, not this one's.
RBAC_NAMESPACED = frozenset({"roles", "rolebindings"})

# How many positive controls to derive per subject. A cap rather than the full read set: the
# manager role alone grants reads on dozens of types and the sweep is already ~250 round trips. Any
# one of them proves the subject string resolves; eight proves it for a spread of API groups.
POSITIVE_CAP = 8
# Below this the sweep is not asking enough to mean anything. Four subjects' worth of RBAC
# questions is 44 each; one subject alone clears this.
MIN_QUESTIONS = 60
# At least this many positive controls, across all subjects, or the whole negative half is
# unguarded against a subject string nobody resolves ([[LSN-035]]).
MIN_POSITIVES = 3
# Served resource types on any cluster that can run this check at all.
MIN_SERVED = 30


def load_l0(repo: str):
    path = pathlib.Path(repo) / "dev" / "tests" / "controller-mints-no-rbac.py"
    if not path.exists():
        raise SystemExit(f"FAIL: the L0 half is missing at {path}; there is nothing to import.")
    spec = importlib.util.spec_from_file_location("controller_mints_no_rbac", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def install_map(repo: str) -> tuple[str, str]:
    """(namePrefix, namespace) -- how a tree name becomes a live name."""
    path = pathlib.Path(repo) / KUSTOMIZATION
    text = path.read_text()
    prefix = NAME_PREFIX_RE.search(text)
    namespace = NAMESPACE_RE.search(text)
    if not prefix or not namespace:
        raise SystemExit(
            f"FAIL: {KUSTOMIZATION} declares no namePrefix and/or no namespace. Every live name "
            f"this check looks up is derived from those two lines; without them it would be "
            f"guessing at what the install is called."
        )
    return prefix.group(1), namespace.group(1)


def tree_corpus(m):
    """The install tree, as (kind, name) -> set of triples, plus its bindings."""
    rbac_texts, _go_texts, _probe = m._inputs()
    corpus = m.parse_rbac(rbac_texts)
    roles: dict[tuple[str, str], set] = {}
    for rule in corpus.rules:
        kind, _, name = rule.where.split()[1].partition("/")
        roles.setdefault((kind, name), set()).update(rule.triples())
    binds: dict[tuple[str, str], tuple[str, str]] = {}
    for where, ref_kind, ref_name in corpus.bindings:
        kind, _, name = where.split()[1].partition("/")
        binds[(kind, name)] = (ref_kind, ref_name)
    return corpus, roles, binds


# ------------------------------------------------------------------------------------------------
# Live state
# ------------------------------------------------------------------------------------------------


def _items(statedir: str, fname: str) -> list:
    path = pathlib.Path(statedir) / fname
    if not path.exists():
        return []
    text = path.read_text().strip()
    if not text:
        return []
    doc = json.loads(text)
    return doc.get("items") or []


def live_state(m, statedir: str) -> dict:
    """Every live object this check reads, keyed the way the arms want it."""
    roles: dict[tuple[str, str, str], dict] = {}  # (kind, ns, name) -> object
    for kind, fname in (("ClusterRole", "clusterroles.json"), ("Role", "roles.json")):
        for obj in _items(statedir, fname):
            meta = obj.get("metadata") or {}
            roles[(kind, meta.get("namespace") or "", meta.get("name") or "")] = obj
    bindings: dict[tuple[str, str, str], dict] = {}
    for kind, fname in (
        ("ClusterRoleBinding", "clusterrolebindings.json"),
        ("RoleBinding", "rolebindings.json"),
    ):
        for obj in _items(statedir, fname):
            meta = obj.get("metadata") or {}
            bindings[(kind, meta.get("namespace") or "", meta.get("name") or "")] = obj
    deployments = []
    for obj in _items(statedir, "deployments.json"):
        meta = obj.get("metadata") or {}
        spec = ((obj.get("spec") or {}).get("template") or {}).get("spec") or {}
        deployments.append((meta.get("name") or "", spec.get("serviceAccountName") or "default"))
    served_path = pathlib.Path(statedir) / "api-resources.txt"
    served = set()
    if served_path.exists():
        served = {ln.strip() for ln in served_path.read_text().splitlines() if ln.strip()}
    return {"roles": roles, "bindings": bindings, "deployments": deployments, "served": served}


def role_triples(m, key: tuple[str, str, str], obj: dict) -> tuple[list, set]:
    """(the object's rules as L0 `Rule`s, its triples). `where` names the LIVE object, not a file."""
    kind, ns, name = key
    where_base = f"live {kind}/{name}" if not ns else f"live {kind}/{name} in {ns}"
    rules, triples = [], set()
    for i, rule in enumerate(obj.get("rules") or []):
        if not isinstance(rule, dict):
            continue
        parsed = m.Rule(
            f"{where_base} rule {i}",
            tuple(rule.get("apiGroups") or []),
            tuple(rule.get("resources") or []),
            tuple(rule.get("verbs") or []),
        )
        rules.append(parsed)
        triples |= parsed.triples()
    return rules, triples


def sa_subjects(obj: dict, namespace: str) -> set:
    """ServiceAccount subjects of a binding that live in the control plane's namespace."""
    out = set()
    for subject in obj.get("subjects") or []:
        if not isinstance(subject, dict):
            continue
        if subject.get("kind") != "ServiceAccount":
            continue
        if (subject.get("namespace") or "") != namespace:
            continue
        out.add(subject.get("name") or "")
    return out - {""}


def discover(m, repo: str, statedir: str) -> dict:
    """The corpus every arm is asserted over. Derived; nothing here is a roster."""
    prefix, namespace = install_map(repo)
    _corpus, tree_roles, tree_binds = tree_corpus(m)
    state = live_state(m, statedir)

    expected_roles = {}  # live (kind, ns, name) -> tree (kind, name)
    for (kind, name), _triples in tree_roles.items():
        ns = namespace if kind == "Role" else ""
        expected_roles[(kind, ns, prefix + name)] = (kind, name)
    expected_bindings = {}  # live (kind, ns, name) -> tree (kind, name)
    for (kind, name), _ref in tree_binds.items():
        ns = namespace if kind == "RoleBinding" else ""
        expected_bindings[(kind, ns, prefix + name)] = (kind, name)

    # Seed 1: every ServiceAccount a Deployment in the control-plane namespace runs as. This is what
    # notices a component that arrived without anybody telling this check about it.
    subjects = {sa for _name, sa in state["deployments"]}
    # Seed 2: every ServiceAccount named by a binding the tree declares. This is what keeps the
    # discovery working when a component is scaled to zero or has no Deployment at all.
    for key in expected_bindings:
        obj = state["bindings"].get(key)
        if obj:
            subjects |= sa_subjects(obj, namespace)
    subjects -= {"default"}

    # The closure: EVERY live binding, in any namespace, that names one of those subjects. This is
    # the arm an out-of-band `cluster-admin` grant cannot hide from, because it is found by its
    # subject rather than by its name or its labels.
    touching = {}
    for key, obj in state["bindings"].items():
        named = sa_subjects(obj, namespace) & subjects
        if named:
            touching[key] = (obj, sorted(named))

    # Roles in scope: the ones the tree installs, plus every role any touching binding points at.
    scanned: dict[tuple[str, str, str], dict] = {}
    dangling = []
    for key in expected_roles:
        obj = state["roles"].get(key)
        if obj is not None:
            scanned[key] = obj
    for key, (obj, _named) in sorted(touching.items()):
        ref = obj.get("roleRef") or {}
        ref_kind, ref_name = str(ref.get("kind") or ""), str(ref.get("name") or "")
        ref_ns = key[1] if ref_kind == "Role" else ""
        target = (ref_kind, ref_ns, ref_name)
        found = state["roles"].get(target)
        if found is None:
            dangling.append((key, target))
            continue
        scanned[target] = found

    return {
        "prefix": prefix,
        "namespace": namespace,
        "tree_roles": tree_roles,
        "tree_binds": tree_binds,
        "expected_roles": expected_roles,
        "expected_bindings": expected_bindings,
        "state": state,
        "subjects": sorted(subjects),
        "touching": touching,
        "scanned": scanned,
        "dangling": dangling,
    }


# ------------------------------------------------------------------------------------------------
# The sweep table
# ------------------------------------------------------------------------------------------------


def word_of(group: str, resource: str) -> str:
    return resource if not group else f"{resource}.{group}"


def guard_identity_tables(m) -> None:
    unknown = set(m.IDENTITY_RESOURCES) - set(CREDENTIAL_SUBRESOURCE)
    unknown |= set(m.IDENTITY_RESOURCES) - set(IDENTITY_SHAPE)
    if unknown:
        raise SystemExit(
            f"FAIL: the L0 half now names identity resource(s) {sorted(unknown)} that this sweep "
            f"has no shape or credential subresource for. Add them here in the same commit — a "
            f"sweep that silently asks one fewer question is the vacuity this check exists to "
            f"refuse ([[LSN-036]])."
        )


def questions(m, disc: dict) -> list[dict]:
    """The whole table, derived from the L0 half's constants and the tree's own grants."""
    guard_identity_tables(m)
    namespace = disc["namespace"]
    prefix = disc["prefix"]
    verbs = sorted(m.ORDINARY_VERBS | m.ESCALATION_VERBS)
    rows: list[dict] = []

    def add(subject, verb, resource, subresource, scope, expect):
        rows.append(
            {
                "subject": subject,
                "verb": verb,
                "resource": resource,
                "subresource": subresource,
                "scope": scope,
                "expect": expect,
            }
        )

    # What the TREE says each subject may do, followed through the live binding graph. Used only to
    # derive positive controls: a `yes` expectation has to come from somewhere the check believes.
    granted: dict[str, set] = {sa: set() for sa in disc["subjects"]}
    for key, (obj, named) in disc["touching"].items():
        ref = obj.get("roleRef") or {}
        tree_key = (str(ref.get("kind") or ""), str(ref.get("name") or "").removeprefix(prefix))
        for sa in named:
            granted.setdefault(sa, set()).update(disc["tree_roles"].get(tree_key, set()))

    for sa in disc["subjects"]:
        subject = f"system:serviceaccount:{namespace}:{sa}"

        # 08 §7 property 2: no verb on any RBAC resource, reads included.
        for resource in sorted(m.RBAC_RESOURCES):
            scope = "ns" if resource in RBAC_NAMESPACED else "cluster"
            word = word_of(m.RBAC_GROUP, resource)
            for verb in verbs:
                add(subject, verb, word, "", scope, "no")

        # 08 §7 property 3: identity resources may be read and never written, and no subresource of
        # one may be touched at all. Reads are permitted, so they are not asked here — they are the
        # positive-control pool below, where the tree says the subject holds them.
        for resource in sorted(m.IDENTITY_RESOURCES):
            group, scope = IDENTITY_SHAPE[resource]
            word = word_of(group, resource)
            for verb in verbs:
                if verb in m.READ_VERBS:
                    continue
                add(subject, verb, word, "", scope, "no")
            for verb in sorted(m.ORDINARY_VERBS):
                add(subject, verb, word, CREDENTIAL_SUBRESOURCE[resource], scope, "no")

        # 08 §7 property 6, on the two principals that are not resources at all.
        for principal in ("users", "groups"):
            add(subject, "impersonate", principal, "", "cluster", "no")

        # The superuser question, on both axes. `*` is a wildcard REQUEST rather than a wildcard
        # rule, so a `yes` here means something granted `*` on `*` -- which is the shape of every
        # accidental `cluster-admin` binding.
        add(subject, m.WILDCARD, m.WILDCARD, "", "cluster", "no")
        add(subject, m.WILDCARD, m.WILDCARD, "", "ns", "no")

        # POSITIVE CONTROLS. Derived from the grants the tree says this identity holds, filtered to
        # types the API server actually serves -- an unserved type answers `no` for a reason that
        # has nothing to do with authority, and a positive control that can answer `no` for the
        # wrong reason is worse than none.
        pool = sorted(
            word_of(g, r)
            for g, r, v in granted.get(sa, set())
            if v in m.READ_VERBS and "/" not in r and m.WILDCARD not in (g, r, v)
        )
        seen: list[str] = []
        for word in pool:
            if word in seen or word not in disc["state"]["served"]:
                continue
            seen.append(word)
            if len(seen) >= POSITIVE_CAP:
                break
        for word in seen:
            add(subject, "get", word, "", "ns", "yes")

    return rows


# ------------------------------------------------------------------------------------------------
# The arms
# ------------------------------------------------------------------------------------------------


def attribute(m, row: dict) -> str:
    """Which of the L0 half's signals a wrongly-permitted question corresponds to."""
    resource, verb = row["resource"], row["verb"]
    base = resource.split(".", 1)[0]
    if m.WILDCARD in (verb, resource):
        return m.SIG_WILD_VERBS if verb == m.WILDCARD else m.SIG_WILD_RESOURCES
    if row["subresource"]:
        return m.SIG_SUBRESOURCE
    if base in m.RBAC_RESOURCES:
        return m.SIG_RBAC
    if verb in m.ESCALATION_VERBS:
        return m.SIG_ESCALATION
    if base in m.IDENTITY_RESOURCES:
        return m.SIG_IDENTITY
    return m.SIG_UNLISTED


def score(m, disc: dict, table: list[dict], transcript: list[dict]) -> int:
    """Ten arms. One `PASS:`/`FAIL:` line each; detail lines carry no prefix."""
    out: list[str] = []
    bad = [0]

    def ok(msg):
        out.append(f"PASS: {msg}")

    def no(msg):
        out.append(f"FAIL: {msg}")
        bad[0] = 1

    def detail(msg):
        out.append(f"    {msg}")

    prefix, namespace = disc["prefix"], disc["namespace"]
    state, scanned = disc["state"], disc["scanned"]

    # ---- L2-1 non-vacuity ----------------------------------------------------------------------
    all_rules: list = []
    for key, obj in sorted(scanned.items()):
        rules, _triples = role_triples(m, key, obj)
        all_rules.extend(rules)
    n_roles, n_rules = len(scanned), len(all_rules)
    if n_roles >= m.MIN_ROLES and n_rules >= m.MIN_RULES:
        ok(
            f"L2-1a: the live control-plane RBAC corpus is {n_roles} role(s) and {n_rules} rule(s) "
            f"(floors {m.MIN_ROLES}/{m.MIN_RULES}) — the scan below has something to scan"
        )
    else:
        no(
            f"L2-1a: the live corpus is {n_roles} role(s) and {n_rules} rule(s), under the L0 "
            f"half's own floors of {m.MIN_ROLES}/{m.MIN_RULES}. A scan over nothing is the greenest "
            f"thing in this repository ([[LSN-035]]); discovery broke, or the control plane is not "
            f"installed on this cluster"
        )

    manager = ("ClusterRole", "", prefix + m.GENERATED_ROLE)
    if manager in scanned:
        ok(f"L2-1b: `{manager[2]}` — the role 08 §7's sentence is about — is installed and in scope")
    else:
        no(
            f"L2-1b: no ClusterRole named `{manager[2]}` on this cluster. 09 §6.1 names it; every "
            f"other arm here could be green over three other roles while the one under test is "
            f"absent, which is a pass about the wrong object"
        )

    probe_rules = m.parse_rbac({"probe.yaml": m.PROBE_ROLE}).rules
    fired = m.scan(probe_rules)
    missing_signals = [s for s in m.PROBE_SIGNALS if not any(s in f for f in fired)]
    if probe_rules and not missing_signals:
        ok(
            f"L2-1c: the imported scanner still fires — `PROBE_ROLE` provoked all "
            f"{len(m.PROBE_SIGNALS)} of the L0 half's signals through this file's own import of it"
        )
    else:
        no(
            f"L2-1c: the imported scanner did NOT fire on {len(missing_signals)} of its own signals "
            f"({'; '.join(missing_signals) or 'the probe parsed to nothing'}). L2-3 below would be "
            f"green because it cannot see, not because there is nothing to see"
        )

    n_deploy = len(state["deployments"])
    if disc["subjects"] and len(disc["subjects"]) >= max(1, n_deploy):
        ok(
            f"L2-1d: {len(disc['subjects'])} control-plane ServiceAccount(s) discovered "
            f"({', '.join(disc['subjects'])}) covering all {n_deploy} Deployment(s) in {namespace}"
        )
    else:
        no(
            f"L2-1d: discovery found {len(disc['subjects'])} control-plane ServiceAccount(s) for "
            f"{n_deploy} Deployment(s) in {namespace}. The binding closure and the whole sweep are "
            f"keyed on that set; a short one is a check that swept the identities it could see"
        )

    if len(state["served"]) >= MIN_SERVED:
        ok(f"L2-1e: {len(state['served'])} served resource types read from the API server")
    else:
        no(
            f"L2-1e: only {len(state['served'])} served resource types were read (floor "
            f"{MIN_SERVED}). Every positive control is filtered through that list, so an empty one "
            f"silently removes the sweep's only guard against an unresolvable subject"
        )

    # ---- L2-2 drift, both directions ------------------------------------------------------------
    drift = 0
    for live_key, tree_key in sorted(disc["expected_roles"].items()):
        want = disc["tree_roles"].get(tree_key, set())
        obj = state["roles"].get(live_key)
        if obj is None:
            drift += 1
            detail(
                f"{tree_key[0]}/{tree_key[1]} is defined in {m.CONFIG_TREE}/ and there is no live "
                f"`{live_key[2]}` on this cluster — the install never applied it"
            )
            continue
        _rules, have = role_triples(m, live_key, obj)
        only_tree = sorted(want - have)
        only_live = sorted(have - want)
        if only_tree:
            drift += 1
            detail(
                f"{live_key[2]}: {len(only_tree)} triple(s) in the tree and NOT on the cluster — "
                f"the install is behind the repository: {only_tree[:6]}"
            )
        if only_live:
            drift += 1
            detail(
                f"{live_key[2]}: {len(only_live)} triple(s) on the cluster and in NO file — "
                f"somebody edited the live role: {only_live[:6]}"
            )
    if drift == 0 and disc["expected_roles"]:
        ok(
            f"L2-2: all {len(disc['expected_roles'])} control-plane role(s) grant exactly what "
            f"{m.CONFIG_TREE}/ says they grant, triple for triple, in both directions"
        )
    else:
        no(
            f"L2-2: {drift} drift finding(s) between the install tree and the cluster (detail "
            f"above). The L0 half reads the tree and is green; the tree is not the cluster, and "
            f"08 §7 is a statement about what the controller CAN do"
        )

    # ---- L2-3 the scan, over what is installed ---------------------------------------------------
    findings = m.scan(all_rules)
    if not findings:
        ok(
            f"L2-3: `scan()` from the L0 half over all {len(all_rules)} installed rule(s) — no verb "
            f"on any RBAC object, identity resources read-only, no credential subresource, no "
            f"wildcard, no verb outside the allow-list, no escalate/bind/impersonate"
        )
    else:
        for finding in findings[:12]:
            detail(finding)
        no(
            f"L2-3: {len(findings)} finding(s) in the RBAC that is actually installed. The "
            f"controller can mint RBAC on this cluster right now"
        )

    # ---- L2-4 the binding set --------------------------------------------------------------------
    observed = set(disc["touching"])
    expected = set(disc["expected_bindings"])
    extra = sorted(observed - expected)
    absent = sorted(expected - observed)
    if not extra and not absent and observed:
        ok(
            f"L2-4: all {len(observed)} live binding(s) naming a control-plane ServiceAccount are "
            f"ones {m.CONFIG_TREE}/ declares, and every declared one is installed"
        )
    else:
        for key in extra:
            ref = (disc["touching"][key][0].get("roleRef") or {})
            detail(
                f"OUT OF BAND: {key[0]}/{key[2]}"
                f"{' in ' + key[1] if key[1] else ''} names "
                f"{', '.join(disc['touching'][key][1])} and points at "
                f"{ref.get('kind')}/{ref.get('name')} — no file in the tree declares it, so no "
                f"parse of the tree could ever have seen it"
            )
        for key in absent:
            detail(f"DECLARED AND ABSENT: {key[0]}/{key[2]} is in the tree and not on the cluster")
        no(
            f"L2-4: {len(extra)} out-of-band binding(s) and {len(absent)} missing one(s). RBAC is "
            f"a union across every binding, and no file contains the union"
        )

    # ---- L2-5 roleRef closure --------------------------------------------------------------------
    off_tree = []
    for key, (obj, named) in sorted(disc["touching"].items()):
        ref = obj.get("roleRef") or {}
        ref_kind, ref_name = str(ref.get("kind") or ""), str(ref.get("name") or "")
        tree_key = (ref_kind, ref_name.removeprefix(prefix))
        if tree_key not in disc["tree_roles"]:
            off_tree.append((key, ref_kind, ref_name, named))
    for key, target in disc["dangling"]:
        detail(
            f"DANGLING: {key[0]}/{key[2]} points at {target[0]}/{target[2]}, which does not exist "
            f"on this cluster — the binding grants nothing today and everything on the day "
            f"somebody creates a role with that name"
        )
    for key, ref_kind, ref_name, named in off_tree:
        detail(
            f"OFF-TREE ROLEREF: {key[0]}/{key[2]} binds {', '.join(named)} to "
            f"{ref_kind}/{ref_name}, which {m.CONFIG_TREE}/ does not define. If that is "
            f"`cluster-admin`, the controller mints anything it likes"
        )
    if not off_tree and not disc["dangling"]:
        ok(
            f"L2-5: every binding that names a control-plane identity resolves to a role the tree "
            f"defines and the cluster has — the closure is closed"
        )
    else:
        no(
            f"L2-5: {len(off_tree)} off-tree roleRef(s) and {len(disc['dangling'])} dangling one(s) "
            f"(detail above)"
        )

    # ---- L2-6 no aggregation ---------------------------------------------------------------------
    aggregated = [k for k, o in sorted(scanned.items()) if o.get("aggregationRule") is not None]
    if not aggregated:
        ok(f"L2-6: no role in the corpus carries an `aggregationRule`")
    else:
        no(
            f"L2-6: {len(aggregated)} role(s) carry an `aggregationRule` "
            f"({', '.join(k[2] for k in aggregated)}). Their rules are whatever labelled "
            f"ClusterRole the controller-manager decides to merge in next, so the grant this "
            f"check just read is not a grant anybody wrote down"
        )

    # ---- A-1…A-4 the authorizer sweep -------------------------------------------------------------
    def key_of(row):
        return tuple(row[c] for c in QCOLS)

    want = {key_of(r) for r in table}
    got = {key_of(r): r for r in transcript}
    unanswered = sorted(want - set(got))
    surplus = sorted(set(got) - want)
    if len(want) >= MIN_QUESTIONS and not unanswered and not surplus:
        ok(f"A-1: all {len(want)} derived question(s) were asked of the live authorizer, exactly once each")
    else:
        for k in unanswered[:6]:
            detail(f"never asked: {k}")
        for k in surplus[:6]:
            detail(f"answered but not derived: {k}")
        no(
            f"A-1: the sweep derived {len(want)} question(s) (floor {MIN_QUESTIONS}), "
            f"{len(unanswered)} went unasked and {len(surplus)} answers match no question. A "
            f"transcript that does not cover the table is a table nobody ran"
        )

    malformed = sorted(k for k, r in got.items() if r["answer"] not in ("yes", "no"))
    if not malformed:
        ok(f"A-2: every one of the {len(got)} answers is a real `yes`/`no` from the authorizer")
    else:
        for k in malformed[:6]:
            detail(f"{got[k]['answer']!r} for {k}")
        no(
            f"A-2: {len(malformed)} question(s) got no usable answer (`malformed` is this script's "
            f"own refusal of a resource word containing a slash — [[LSN-044]] — and an empty "
            f"answer is a query that errored). An unanswered negative row is not a `no`"
        )

    permitted = sorted(
        k for k, r in got.items() if r["expect"] == "no" and r["answer"] == "yes"
    )
    n_negative = sum(1 for r in table if r["expect"] == "no")
    if not permitted:
        ok(
            f"A-3: all {n_negative} question(s) 08 §7 requires a `no` to were refused by the live "
            f"authorizer, for every control-plane identity"
        )
    else:
        for k in permitted[:12]:
            row = got[k]
            detail(
                f"{row['subject']} MAY `{row['verb']}` {row['resource']}"
                f"{'/' + row['subresource'] if row['subresource'] else ''} "
                f"({'cluster-wide' if row['scope'] == 'cluster' else 'in ' + namespace}): "
                f"{attribute(m, row)}"
            )
        no(
            f"A-3: {len(permitted)} of {n_negative} forbidden question(s) answered `yes`. This is "
            f"the union of every binding, so the grant may be in no file at all"
        )

    refused = sorted(k for k, r in got.items() if r["expect"] == "yes" and r["answer"] == "no")
    n_positive = sum(1 for r in table if r["expect"] == "yes")
    if n_positive >= MIN_POSITIVES and not refused:
        ok(
            f"A-4: all {n_positive} positive control(s) answered `yes` — the subject strings "
            f"resolve, so the {n_negative} `no`s above are refusals and not typos"
        )
    else:
        for k in refused[:8]:
            row = got[k]
            detail(
                f"{row['subject']} may NOT `{row['verb']}` {row['resource']} in {namespace}, "
                f"though {m.CONFIG_TREE}/ grants it — either the install is behind the tree "
                f"(see L2-2) or the subject string names nobody"
            )
        no(
            f"A-4: {n_positive} positive control(s) derived (floor {MIN_POSITIVES}), {len(refused)} "
            f"of them refused. A sweep whose subject resolves to nobody answers `no` to every "
            f"question and passes its negative half perfectly ([[LSN-035]])"
        )

    for line in out:
        print(line)
    return bad[0]


# ------------------------------------------------------------------------------------------------
# Negative control -- no cluster, twelve synthesised defects
# ------------------------------------------------------------------------------------------------


def synthesise(m, repo: str) -> tuple[dict, list[dict], list[dict]]:
    """A live state that mirrors the install tree exactly, and a transcript that satisfies it."""
    prefix, namespace = install_map(repo)
    _corpus, tree_roles, tree_binds = tree_corpus(m)

    sa = prefix + "controller"
    roles_json = {"ClusterRole": [], "Role": []}
    for (kind, name), triples in sorted(tree_roles.items()):
        rules = [
            {"apiGroups": [g], "resources": [r], "verbs": [v]} for g, r, v in sorted(triples)
        ]
        meta = {"name": prefix + name}
        if kind == "Role":
            meta["namespace"] = namespace
        roles_json[kind].append({"metadata": meta, "rules": rules})
    bindings_json = {"ClusterRoleBinding": [], "RoleBinding": []}
    for (kind, name), (ref_kind, ref_name) in sorted(tree_binds.items()):
        meta = {"name": prefix + name}
        if kind == "RoleBinding":
            meta["namespace"] = namespace
        bindings_json[kind].append(
            {
                "metadata": meta,
                "roleRef": {"kind": ref_kind, "name": prefix + ref_name},
                "subjects": [{"kind": "ServiceAccount", "name": sa, "namespace": namespace}],
            }
        )
    served = set()
    for triples in tree_roles.values():
        for g, r, _v in triples:
            if "/" not in r and m.WILDCARD not in (g, r):
                served.add(word_of(g, r))
    served |= {word_of(m.RBAC_GROUP, r) for r in m.RBAC_RESOURCES}
    # A representative `api-resources -o name` for a cluster that is not this repository's. The
    # LIVE run reads the real one; this is the fixture that stands in for it, and it has to be
    # plausible enough to clear the same floor the live one does -- a control whose fixture is
    # thinner than the floor tests the floor rather than the arm.
    served |= {
        "bindings", "componentstatuses", "configmaps", "endpoints", "events", "limitranges",
        "namespaces", "nodes", "persistentvolumeclaims", "persistentvolumes", "pods",
        "podtemplates", "replicationcontrollers", "resourcequotas", "secrets", "serviceaccounts",
        "services", "controllerrevisions.apps", "daemonsets.apps", "deployments.apps",
        "replicasets.apps", "statefulsets.apps", "cronjobs.batch", "jobs.batch",
        "ingresses.networking.k8s.io", "networkpolicies.networking.k8s.io",
        "poddisruptionbudgets.policy", "storageclasses.storage.k8s.io",
        "customresourcedefinitions.apiextensions.k8s.io", "leases.coordination.k8s.io",
        "certificatesigningrequests.certificates.k8s.io",
        "subjectaccessreviews.authorization.k8s.io", "runtimeclasses.node.k8s.io",
    }
    return (
        {
            "prefix": prefix,
            "namespace": namespace,
            "sa": sa,
            "roles_json": roles_json,
            "bindings_json": bindings_json,
            "served": sorted(served),
        },
        roles_json,
        bindings_json,
    )


def write_state(tmp: pathlib.Path, synth: dict) -> str:
    tmp.mkdir(parents=True, exist_ok=True)
    files = {
        "clusterroles.json": {"items": synth["roles_json"]["ClusterRole"]},
        "roles.json": {"items": synth["roles_json"]["Role"]},
        "clusterrolebindings.json": {"items": synth["bindings_json"]["ClusterRoleBinding"]},
        "rolebindings.json": {"items": synth["bindings_json"]["RoleBinding"]},
        "deployments.json": {
            "items": [
                {
                    "metadata": {"name": "controller-manager"},
                    "spec": {"template": {"spec": {"serviceAccountName": synth["sa"]}}},
                }
            ]
        },
    }
    for name, doc in files.items():
        (tmp / name).write_text(json.dumps(doc))
    (tmp / "api-resources.txt").write_text("\n".join(synth["served"]) + "\n")
    return str(tmp)


def negative_control(repo: str) -> int:
    import copy
    import tempfile

    m = load_l0(repo)
    base_synth, _r, _b = synthesise(m, repo)
    rc = 0

    def run(synth: dict, mutate_transcript=None) -> list[str]:
        """Score one synthesised world; return its output lines."""
        with tempfile.TemporaryDirectory() as td:
            statedir = write_state(pathlib.Path(td) / "state", synth)
            disc = discover(m, repo, statedir)
            table = questions(m, disc)
            transcript = [dict(row, answer=row["expect"]) for row in table]
            if mutate_transcript is not None:
                transcript = mutate_transcript(transcript)
            # THROUGH THE WIRE FORMAT, not around it. The live path writes the transcript as TSV
            # from a shell loop and reads it back here, and the first live run lost 205 of 253
            # rows to that encoding (see SUBRESOURCE_NONE). A control that hands the arms
            # in-memory dicts cannot see an encoding defect, and this one was invisible to every
            # arm but A-1.
            wire = pathlib.Path(td) / "transcript.tsv"
            wire.write_text(
                "".join(f"{emit_tsv(row)}\t{row['answer']}\n" for row in transcript)
            )
            transcript = read_tsv(str(wire), QCOLS + ("answer",))
            buf: list[str] = []
            import io
            import contextlib

            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                score(m, disc, table, transcript)
            buf = stream.getvalue().splitlines()
            return buf

    def fails(lines: list[str]) -> str:
        return "\n".join(ln for ln in lines if ln.startswith("FAIL:"))

    baseline = run(base_synth)
    if fails(baseline):
        print(
            "FAIL: the synthesised baseline does not pass its own assertion block, so nothing "
            "below proves anything. The mirror of the install tree should be clean by construction:"
        )
        for line in baseline:
            if line.startswith("FAIL:"):
                print(f"    {line}")
        return 1
    print(
        f"PASS: baseline — a synthesised cluster that mirrors {m.CONFIG_TREE}/ exactly clears all "
        f"{sum(1 for ln in baseline if ln.startswith('PASS:'))} arms"
    )

    def clone() -> dict:
        return copy.deepcopy(base_synth)

    def manager_of(synth: dict) -> dict:
        name = synth["prefix"] + m.GENERATED_ROLE
        for obj in synth["roles_json"]["ClusterRole"]:
            if obj["metadata"]["name"] == name:
                return obj
        raise SystemExit("FAIL: the synthesised baseline has no manager role to mutate.")

    defects: list[tuple[str, str, dict, object]] = []

    d = clone()
    manager_of(d)["rules"].pop()
    defects.append(("D-1 a grant in the tree that the install never applied", "L2-2", d, None))

    d = clone()
    manager_of(d)["rules"].append(
        {"apiGroups": [m.RBAC_GROUP], "resources": ["rolebindings"], "verbs": ["create"]}
    )
    defects.append(("D-2 a live role that grants `create rolebindings`", "L2-2", d, None))

    d = clone()
    d["roles_json"]["ClusterRole"][0]["rules"].append(
        {"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]}
    )
    defects.append(("D-3 a wildcard rule on a live role", "L2-3", d, None))

    d = clone()
    d["roles_json"]["ClusterRole"].append(
        {
            "metadata": {"name": "cluster-admin"},
            "rules": [{"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]}],
        }
    )
    d["bindings_json"]["ClusterRoleBinding"].append(
        {
            "metadata": {"name": "someone-was-debugging"},
            "roleRef": {"kind": "ClusterRole", "name": "cluster-admin"},
            "subjects": [
                {"kind": "ServiceAccount", "name": d["sa"], "namespace": d["namespace"]}
            ],
        }
    )
    defects.append(("D-4 an out-of-band ClusterRoleBinding to cluster-admin", "L2-4", d, None))

    d = clone()
    d["roles_json"]["ClusterRole"].append(
        {
            "metadata": {"name": "cluster-admin"},
            "rules": [{"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]}],
        }
    )
    d["bindings_json"]["ClusterRoleBinding"].append(
        {
            "metadata": {"name": "someone-was-debugging"},
            "roleRef": {"kind": "ClusterRole", "name": "cluster-admin"},
            "subjects": [
                {"kind": "ServiceAccount", "name": d["sa"], "namespace": d["namespace"]}
            ],
        }
    )
    defects.append(
        ("D-5 the same binding, seen by the scan it drags cluster-admin into", "L2-3", d, None)
    )

    d = clone()
    d["bindings_json"]["ClusterRoleBinding"].append(
        {
            "metadata": {"name": "points-at-nothing"},
            "roleRef": {"kind": "ClusterRole", "name": "a-role-that-does-not-exist-yet"},
            "subjects": [
                {"kind": "ServiceAccount", "name": d["sa"], "namespace": d["namespace"]}
            ],
        }
    )
    defects.append(("D-6 a binding whose roleRef resolves to nothing", "L2-5", d, None))

    d = clone()
    manager_of(d)["aggregationRule"] = {
        "clusterRoleSelectors": [{"matchLabels": {"rbac.example/aggregate": "true"}}]
    }
    defects.append(("D-7 an aggregationRule on the manager role", "L2-6", d, None))

    d = clone()
    d["roles_json"]["ClusterRole"] = [
        o for o in d["roles_json"]["ClusterRole"] if o["metadata"]["name"] != d["prefix"] + m.GENERATED_ROLE
    ]
    defects.append(("D-8 the manager role absent from the cluster entirely", "L2-1b", d, None))

    d = clone()
    d["roles_json"] = {"ClusterRole": [], "Role": []}
    defects.append(("D-9 no control-plane RBAC installed at all", "L2-1a", d, None))

    def flip_negative(transcript):
        out = []
        flipped = False
        for row in transcript:
            if not flipped and row["expect"] == "no":
                row = dict(row, answer="yes")
                flipped = True
            out.append(row)
        return out

    defects.append(
        ("D-10 the authorizer permits a forbidden verb", "A-3", clone(), flip_negative)
    )

    def flip_positive(transcript):
        out = []
        flipped = False
        for row in transcript:
            if not flipped and row["expect"] == "yes":
                row = dict(row, answer="no")
                flipped = True
            out.append(row)
        return out

    defects.append(
        ("D-11 the sweep subject resolves to nobody, so every answer is `no`", "A-4", clone(), flip_positive)
    )

    defects.append(("D-12 the sweep did not run", "A-1", clone(), lambda t: []))

    for label, arm, synth, mutate in defects:
        lines = run(synth, mutate)
        hit = [ln for ln in lines if ln.startswith(f"FAIL: {arm}")]
        if hit:
            print(f"PASS: {label} -> caught by {arm}")
        else:
            rc = 1
            print(
                f"FAIL: {label} was NOT caught by {arm}. An arm that cannot go red is not an "
                f"assertion; it is a sentence."
            )
            for ln in lines:
                if ln.startswith("FAIL:"):
                    print(f"    (it did fire: {ln})")
    return rc


# ------------------------------------------------------------------------------------------------


def read_tsv(path: str, columns: tuple[str, ...]) -> list[dict]:
    rows = []
    for line in pathlib.Path(path).read_text().splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        parts += [""] * (len(columns) - len(parts))
        row = dict(zip(columns, parts[: len(columns)]))
        if row.get("subresource") == SUBRESOURCE_NONE:
            row["subresource"] = ""
        rows.append(row)
    return rows


def emit_tsv(row: dict) -> str:
    values = [row[c] for c in QCOLS]
    values[QCOLS.index("subresource")] = row["subresource"] or SUBRESOURCE_NONE
    return "\t".join(values)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: manager_role_assert.py {namespace|questions|score|negative-control} ...")
        return 2
    cmd = argv[1]
    if cmd == "namespace":
        _prefix, namespace = install_map(argv[2])
        print(namespace)
        return 0
    if cmd == "negative-control":
        return negative_control(argv[2])
    repo, statedir = argv[2], argv[3]
    m = load_l0(repo)
    disc = discover(m, repo, statedir)
    if cmd == "questions":
        for row in questions(m, disc):
            print(emit_tsv(row))
        return 0
    if cmd == "score":
        table = questions(m, disc)
        transcript = read_tsv(argv[4], QCOLS + ("answer",))
        return score(m, disc, table, transcript)
    print(f"unknown subcommand {cmd!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
PYEOF

# ------------------------------------------------------------------------------------------------
# score <statedir> <transcript> — fold the assertion block's PASS/FAIL lines into this suite's
# counters. A wrapper rather than a copy, so `--negative-control` exercises the arms the live run
# uses.
# ------------------------------------------------------------------------------------------------
score() {
  local out rc line
  out="$(python3 "$ANALYZE" "$@" 2>&1)"
  rc=$?
  while IFS= read -r line; do
    case "$line" in
      PASS:*) pass "${line#PASS: }" ;;
      FAIL:*) bad "${line#FAIL: }" ;;
      *) [ -n "$line" ] && echo "$line" ;;
    esac
  done <<<"$out"
  return $rc
}

# ------------------------------------------------------------------------------------------------
# resource_word <resource> — the word that goes in the positional slot, or the refusal `malformed`.
#
# THE RUNTIME HALF OF [[LSN-044]]. The word is COMPUTED from a table this script derives from the
# L0 half's constants, so no static scan can prove it never contains a slash —
# `dev/tests/cluster-check-hygiene.py` property 1b demands a `*/*)` arm for exactly that reason.
# kubectl parses a positional `TYPE/NAME`, not `TYPE/SUBRESOURCE`, so `serviceaccounts/token` in
# this slot asks about a ServiceAccount NAMED `token` — and since almost every row in this table is
# a NEGATIVE, the `no` that comes back would be a `no` about an object nobody was ever granted. The
# credential-subresource rows would go green having asked nothing. `malformed` is outside the
# analyzer's alphabet for the answer column, so a refused row fails A-2 rather than scoring as a
# refusal by the authorizer.
# ------------------------------------------------------------------------------------------------
resource_word() {
  case "$1" in
    */*)
      echo "  suite bug: the resource word '$1' contains a slash. The subresource belongs in the" >&2
      echo "  table's own column, which ask_chunk passes as --subresource=. Refusing (LSN-044)." >&2
      printf 'malformed'
      return
      ;;
  esac
  printf '%s' "$1"
}

# ================================================================================================
# NEGATIVE CONTROL — no cluster, twelve synthesised defects
# ================================================================================================
if [ "$MODE" = negative-control ]; then
  echo "== manager-role-l2.sh --negative-control: can V-CTN-017's L2 arms tell a clean install from a compromised one? =="
  score negative-control "$REPO_ROOT"

  # The one part of the live path the synthesised worlds do reach: a guard nothing ever fires is
  # indistinguishable from a guard whose pattern is wrong.
  got="$(resource_word "serviceaccounts/token" 2>/dev/null)"
  if [ "$got" = "malformed" ]; then
    note "resource_word refused 'serviceaccounts/token' as designed (LSN-044)"
  else
    echo "FAIL: resource_word returned '$got' for a slashed resource; the LSN-044 guard is dead."
    fail=1
  fi

  echo
  if [ "$assertions" -ne "$EXPECTED_ASSERTIONS" ]; then
    echo "FAIL: only $assertions of $EXPECTED_ASSERTIONS assertions ran. The verdict below would be about arms that never executed."
    fail=1
  fi
  if [ "$fail" -eq 0 ]; then
    echo "V-CTN-017 --negative-control: the L2 assertion block is not always-green."
    exit 0
  fi
  echo "V-CTN-017 --negative-control: FAILED — an arm above cannot go red."
  exit 1
fi

# ================================================================================================
# LIVE
# ================================================================================================
# --- DESTRUCTIVE-TEST GUARD ---------------------------------------------------------------------
# Anchored, never a substring (LSN-005). `*gke-scratch*` accepts `my-gke-scratch-of-prod`, and the
# live install `platform-agent-host` is one `*` away. The default arm exits non-zero.
case "$CTX" in
  gke-scratch-*) : ;;
  *) echo "REFUSING: context '$CTX' is not a scratch cluster (destructive-test guard)." >&2; exit 2 ;;
esac

echo "===================================================================="
echo " V-CTN-017 at L2 — the controller mints no RBAC — ctx: $CTX"
echo "===================================================================="

$K version >/dev/null 2>&1 || {
  echo "FAIL: context '$CTX' is not reachable." >&2
  exit 1
}

# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/preconditions.sh"
p10_assert_control_plane_healthy "$K" "$CTX" || exit 2

# --- collect ------------------------------------------------------------------------------------
# Everything the arms read, in one pass, so no arm can be judging a different moment than another.
NS="$(python3 "$ANALYZE" namespace "$REPO_ROOT")"
if [ -z "$NS" ]; then
  echo "FAIL: could not read the install namespace out of the kustomization." >&2
  exit 2
fi
note "install namespace: $NS (read from k8s-operator/config/default/kustomization.yaml)"

$K get clusterroles -o json >"$STATE/clusterroles.json" 2>/dev/null
$K get roles --all-namespaces -o json >"$STATE/roles.json" 2>/dev/null
$K get clusterrolebindings -o json >"$STATE/clusterrolebindings.json" 2>/dev/null
$K get rolebindings --all-namespaces -o json >"$STATE/rolebindings.json" 2>/dev/null
$K -n "$NS" get deployments -o json >"$STATE/deployments.json" 2>/dev/null
$K api-resources -o name >"$STATE/api-resources.txt" 2>/dev/null

if [ "$(count "$STATE/clusterroles.json")" -lt 2 ]; then
  echo "FAIL: could not read ClusterRoles from '$CTX'. Every arm below is about objects this run" >&2
  echo "  never managed to collect; a green over an empty collection is the failure V-CTN-017" >&2
  echo "  exists to refuse. 09 §9.6: a BLOCKING-ALWAYS check that cannot run is itself a finding." >&2
  exit 2
fi

# --- the sweep ------------------------------------------------------------------------------------
if ! python3 "$ANALYZE" questions "$REPO_ROOT" "$STATE" >"$QUESTIONS" 2>"$WORK/questions.err"; then
  echo "FAIL: the question table would not derive:" >&2
  cat "$WORK/questions.err" >&2
  exit 1
fi
note "$(count "$QUESTIONS") authorizer questions derived from the L0 half's own constants"

# ask_chunk <infile> <outfile> — one worker. The resource word is refused before it is asked if it
# carries a slash; the answer column then holds `malformed`, which A-2 fails on.
ask_chunk() {
  local infile="$1" outfile="$2"
  local subject verb resource subresource scope expect word answer
  : >"$outfile"
  while IFS=$'\t' read -r subject verb resource subresource scope expect; do
    [ -n "${subject:-}" ] || continue
    word="$(resource_word "$resource")"
    if [ "$word" = "malformed" ]; then
      answer=malformed
    else
      set -- auth can-i "$verb" "$word" "--as=$subject" --request-timeout=30s
      # `-` is the table's sentinel for "no subresource"; see SUBRESOURCE_NONE in the analyzer for
      # why the column cannot simply be left empty.
      if [ -n "$subresource" ] && [ "$subresource" != "-" ]; then
        set -- "$@" "--subresource=$subresource"
      fi
      case "$scope" in
        ns) set -- "$@" -n "$NS" ;;
        *) set -- "$@" -A ;;
      esac
      answer="$($K "$@" 2>/dev/null | tail -1)"
      [ -n "$answer" ] || answer=empty
    fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$subject" "$verb" "$resource" "$subresource" "$scope" "$expect" "$answer" >>"$outfile"
  done <"$infile"
}

rm -f "$WORK"/chunk.* "$WORK"/answers.*
awk -v p="$PARALLEL" -v d="$WORK" 'NF{print > (d "/chunk." (NR % p))}' "$QUESTIONS"
for chunk in "$WORK"/chunk.*; do
  [ -e "$chunk" ] || continue
  ask_chunk "$chunk" "$WORK/answers.$(basename "$chunk")" &
done
wait
cat "$WORK"/answers.* >"$TRANSCRIPT" 2>/dev/null
note "$(count "$TRANSCRIPT") answers collected from the live authorizer"

# --- score ----------------------------------------------------------------------------------------
echo
score score "$REPO_ROOT" "$STATE" "$TRANSCRIPT"

# ------------------------------------------------------------------------------------------------
echo
if [ "$assertions" -ne "$EXPECTED_ASSERTIONS" ]; then
  echo "FAIL: only $assertions of $EXPECTED_ASSERTIONS assertions ran. The verdict below would be about arms that never executed."
  fail=1
fi

if [ "$fail" -eq 0 ]; then
  echo "V-CTN-017 at L2: PROVEN — the RBAC that is INSTALLED on '$CTX' grants the control plane no"
  echo "  verb on any RBAC object, read-only access to identity resources, no credential"
  echo "  subresource, no wildcard and no escalate/bind/impersonate; it matches the install tree"
  echo "  triple for triple; every binding that names a control-plane identity is one the tree"
  echo "  declares; and the API server's own authorizer refused every one of those questions when"
  echo "  asked directly, while answering the positive controls."
  exit 0
fi
echo "V-CTN-017 at L2: FAILED — see the FAIL lines above."
exit 1
