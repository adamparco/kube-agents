#!/usr/bin/env bash
# reader-scope-l2.sh — V-CTN-001 at L2, and the L2 half of V-CTN-004.
#
# 03 §11, "(carried) Scope, reads":
#
#     for each agent, `kubectl auth can-i get|list|watch` as the **reader** SA returns yes only
#     within its tier scope; a Developer Team reader returns **no** in any other namespace, a
#     Cluster Admin reader **no** for any other cluster.
#
# 09 line 307 gives that sentence the ID V-CTN-001, levels L2 and L3, and a `¬` — a mandatory
# negative control. It is BLOCKING-ALWAYS, so 09 §9.6 forbids deferring it, which is why this file
# exits 0, 1 or 2 and never 3. V-CTN-004 ("the reader holds only read verbs, universally") already
# has an L0 arm in `dev/tests/reader-holds-only-read-verbs.py`; that arm reads the TREE, this one
# asks the API server's authorizer, and the two are different questions about the same sentence.
#
# WHY A NEW SUITE RATHER THAN A LINE IN AN EXISTING ONE. Three things were already true when this
# was written and none of them is V-CTN-001:
#
#   * `verify-phase3.sh` P3-K3 asks five questions of ONE tier's reader (developer-team) and names
#     no check ID. One tier is not "for each agent".
#   * `verify-phase4.sh` regresses the same five.
#   * No script anywhere asked a question as the PLATFORM or CLUSTER-ADMIN reader SA. Two of the
#     three tiers had never had their read scope measured at any level, and the row had zero rows
#     in `verification/results.csv`.
#
# So the shape of this file is fixed by the sentence: all three tiers, both directions, and the ID
# named in the banner and in the verdict so a `grep V-CTN-001 dev/verify/*.sh` finds the thing that
# asserts it.
#
# THE ROSTER IS DERIVED, NEVER LISTED ([[LSN-036]]). Nothing below names a ServiceAccount, a
# namespace, a role or a resource:
#
#   * the TIERS come from `agents/*/SOUL.md` (one directory per tier — the same set `make validate`
#     polices) and are cross-checked against the `spec.tier` enum in the Agent CRD. Disagreement is
#     a finding, not a merge: two definition sites that drift is how a fourth tier ships with no
#     reader identity and no check notices.
#   * the READER ServiceAccounts come from the live cluster, selected by the label the L0 arm
#     defines (`ROLE_LABEL`/`TIER_LABEL`, imported — see below).
#   * the SCOPE of each tier is read off the bindings that actually name that SA: a
#     ClusterRoleBinding means cluster scope, a RoleBinding means that binding's namespace. Nothing
#     here decides in advance which tier is namespaced.
#   * the GRANTS come from the Role/ClusterRole each of those bindings points at, with `*` in
#     `apiGroups`/`resources`/`verbs` expanded against the API server's own discovery document.
#   * the RESOURCE UNIVERSE for the negatives is that same discovery document, so a resource
#     installed tomorrow is swept tomorrow.
#
# THE POSITIVE HALF IS LOAD-BEARING ([[LSN-035]]). A containment check whose every assertion is a
# `no` is satisfiable by deleting the reader, and a scope that grants nothing is trivially inside
# itself. Arm L2-C asks the yes-side for every tier, derived from what that tier's own roles grant
# and filtered through discovery so the suite cannot ask about a resource the server does not
# serve. If any tier contributes no yes-question, that is a failure and not a quiet skip.
#
# IMPORTED, NOT RESTATED. `READ_VERBS`, `ROLE_LABEL` and `TIER_LABEL` are imported from
# `dev/tests/reader-holds-only-read-verbs.py` (V-CTN-004's L0 arm) and `ESCALATION_VERBS`,
# `WILDCARD`, `RBAC_GROUP`, `RBAC_RESOURCES` and `IDENTITY_RESOURCES` from
# `dev/tests/controller-mints-no-rbac.py`, by path, with importlib — the idiom
# `dev/verify/manager-role-l2.sh` established. V-MET-013: the allow-list that decides what a reader
# may do has exactly one definition site, and an L2 suite that retyped `("get","list","watch")`
# would be a second one that agrees today.
#
# THE VERB AXIS IS DERIVED FROM THE SERVER TOO. The non-read verbs swept in L2-F are the union of
# what discovery says each resource supports, minus `READ_VERBS` — the API server's own statement
# of what there is to be denied — plus the three escalation verbs, which discovery never advertises
# because they are RBAC-only. A hand-written list of write verbs is a deny-list, and 09 §11.4 is
# the record of a deny-list admitting `impersonate` because nobody thought to deny it.
#
# ------------------------------------------------------------------------------------------------
# THE "ANY OTHER CLUSTER" CLAUSE — NOT COVERED AT L2, AND SAID SO
# ------------------------------------------------------------------------------------------------
# 03 §11 ends "...a Cluster Admin reader **no** for any other cluster." There is one cluster at L2
# and this suite does not pretend otherwise.
#
# The tempting proxy is the `spec.scope.clusterName` on the cluster-admin Agent CR: ask whether the
# reader can read an object stamped with a DIFFERENT cluster name. It is vacuous, and measurably so.
# RBAC has no cluster axis — `SubjectAccessReview` carries group, resource, subresource, namespace,
# name and verb, and nothing that names a cluster — so the only "same-shaped object stamped with
# another clusterName" available is an `Agent` CR, whose apiGroup `kubeagents.x-k8s.io` appears in
# no reader role at all. The `no` would come from the GROUP axis (which arm L2-E already asserts,
# by name, for every tier) and would be identical for a stamp naming this cluster. That is a `no`
# produced by not asking, which is the exact failure the `¬` on this row exists to refuse.
#
# What this file does instead:
#   (1) it asserts the two cluster-axis properties that ARE visible to one API server — arm L2-G:
#       no reader ClusterRole carries an `aggregationRule` (the one mechanism by which a reader's
#       reach grows without its own role or binding changing), and no reader rule carries
#       `resourceNames` (the only place a grant can be bounded to named objects, so the only place
#       a cross-cluster bound could honestly appear). Both can fail; neither is the clause.
#   (2) it declares the clause NOT COVERED, in the run output, under `L2-G`, so a green run cannot
#       be read as having asserted it. The honest home for it is the L3 arm on a second cluster.
#
# ------------------------------------------------------------------------------------------------
# NEGATIVE CONTROL DOES NOT EXERCISE: ([[LSN-060]].) `--negative-control` runs with NO cluster: it
# synthesises the reader roster, the bindings, the roles, the discovery document AND the transcript,
# then injects one defect per arm. What it therefore cannot say anything about:
#   - the COLLECTION statements. Every `kubectl get` above the analysis is bypassed, including the
#     label selector that finds the readers at all. A selector that matches nothing would produce
#     an empty roster live and a full one under the control, and only arm L2-A distinguishes them.
#   - the ASK statement. The control never runs the authorizer; the transcript is computed by a
#     model of RBAC in `model_answer`. A malformed invocation, a flag typo, or a `--subresource`
#     passed as `TYPE/SUB` ([[LSN-044]]) is invisible to it — `resource_word` guards that at
#     runtime, in the live path only.
#   - the DISCOVERY walk. Group versions are read from the live `/apis` document live and hand-built
#     under the control, so a group whose sub-request fails is a live-only failure mode.
#   - the "any other cluster" clause, for the reason argued above. It is not covered by the live run
#     either; the control cannot cover what the live run does not assert.
#   - the CLOUD/project axis. A reader ServiceAccount's `iam.gke.io/gcp-service-account` annotation
#     is a cross-project reach vector and this suite asserts nothing about it: that is V-CTN-030's
#     row, at phase 11, and two checks with an opinion about one annotation is how a finding gets
#     attributed to whichever suite happened to run first.
#
# ------------------------------------------------------------------------------------------------
# PRECONDITIONS
# ------------------------------------------------------------------------------------------------
# P1 image-under-test: none — no first-party image participates in this suite. The subject under
#      test is the API server's own authorizer and the RBAC objects it evaluates; no agent pod, no
#      broker and no controller is started, read or asked anything, so there is no digest whose
#      staleness could change an answer.
# P3 admission-recreate: none — nothing is created, so nothing can be grandfathered. The authorizer
#      re-evaluates live Roles, ClusterRoles and bindings on every request, so an answer obtained
#      now is an answer about the rules in force now, which is the property P3 exists to buy.
# P6 runtime-authoritative: none — the artifact under test IS the running control plane's authorizer
#      and the RBAC objects it holds, obtained from the API server. There is no image-baked file in
#      this path for a rendered artifact to shadow; the answer is the runtime one by construction.
# P10 control-plane-healthy: asserted via p10_assert_control_plane_healthy before the first
#      question. A cluster that has stopped converging still answers a SubjectAccessReview, but
#      `kubectl get` of the roster can return partial lists, and a short roster is a suite that
#      reports containment about the tiers it happened to see.
#
# Run:  bash dev/verify/reader-scope-l2.sh gke-scratch-kube-agents-dev
#       bash dev/verify/reader-scope-l2.sh --negative-control

set -uo pipefail

MODE=live
if [ "${1:-}" = "--negative-control" ]; then MODE=negative-control; shift; fi

CTX="${1:-gke-scratch-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
K="kubectl --context $CTX"
PARALLEL="${SWEEP_PARALLEL:-16}"

# --- DESTRUCTIVE-TEST GUARD ---------------------------------------------------------------------
# Anchored, never a substring (LSN-005). `*gke-scratch*` accepts `my-gke-scratch-of-prod`, and the
# live install `platform-agent-host` is one `*` away. Placed above every network call — including
# the reachability probe — because a suite that dials a cluster before deciding whether it is
# allowed to has already touched it. It runs in `--negative-control` mode too, against the default
# context, so the guard cannot rot in a mode nobody points at a cluster.
case "$CTX" in
  gke-scratch-*) : ;;
  *)
    echo "REFUSING: context '$CTX' is not a scratch cluster (destructive-test guard)." >&2
    echo "  This impersonates every reader ServiceAccount in the cluster and sweeps the" >&2
    echo "  authorizer. Name the dev cluster explicitly:" >&2
    echo "    $0 gke-scratch-kube-agents-dev" >&2
    exit 2
    ;;
esac

WORK="$(mktemp -d "${TMPDIR:-/tmp}/reader-scope-l2.XXXXXX")"
FAILFILE="$WORK/failures"
CNTFILE="$WORK/assertions"
: >"$FAILFILE"
: >"$CNTFILE"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT INT TERM

# Counters live in FILES, not in shell variables. `producer | consumer` runs the consumer in a
# subshell, so a `fail=1` set inside one is discarded when the pipeline ends:
# `dev/verify/webhook-negatives-l2.sh` ran 31 assertions, printed four `FAIL:` lines and exited 0
# for exactly that reason. Every scoring path below reads from a pipe or a redirect, so the state
# has to outlive the subshell.
pass() { echo "PASS: $1"; echo x >>"$CNTFILE"; }
bad()  { echo "FAIL: $1"; echo x >>"$CNTFILE"; echo x >>"$FAILFILE"; }
note() { echo "  $1"; }
count_of() { if [ -s "$1" ]; then wc -l <"$1" | tr -d ' '; else echo 0; fi; }

cd "$REPO_ROOT" || exit 1

ANALYZE="$WORK/analyze.py"

echo "===================================================================="
echo " V-CTN-001 at L2 — the reader reads only inside its tier scope"
echo " V-CTN-004 at L2 — the reader holds only read verbs (L0 arm imported)"
echo " 03 §11 'Scope, reads' — mode: $MODE — ctx: $CTX"
echo "===================================================================="

# ------------------------------------------------------------------------------------------------
# The analysis. Written out rather than kept in a sibling file so the derivation, the model
# authorizer the control scores against, and the scoring all move in one diff.
# ------------------------------------------------------------------------------------------------
cat >"$ANALYZE" <<'PYEOF'
"""Derivation, question generation, scoring and the self-test for reader-scope-l2.sh.

Not a standalone check: it is written into a temp dir by the suite and invoked with a subcommand.
The suite owns every cluster call; this file owns every judgement.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import sys

QCOLS = ("qid", "arm", "subject", "loc", "verb", "resource", "subresource", "expect", "why")

# `loc` is a location token rather than a namespace, because "which namespace" is not the only
# axis: a request can be cluster-scoped (no namespace at all) or all-namespaces, and those are
# different questions with different answers. An empty tab-separated field is unreadable by
# `while IFS=<tab> read`, so every optional column carries a sentinel instead.
LOC_CLUSTER = "-"
LOC_ALL = "A"
SUB_NONE = "-"

ARMS = (
    ("L2-A", "V-CTN-001", "the reader roster is complete and every tier's reader holds something"),
    ("L2-B", "V-CTN-001", "every binding that names a reader points at that tier's reader role"),
    ("L2-C", "V-CTN-001", "in-scope reads are permitted, for every tier"),
    ("L2-D", "V-CTN-001", "no read succeeds outside the tier's namespace scope"),
    ("L2-E", "V-CTN-001", "no read succeeds outside the tier's resource scope"),
    ("L2-F", "V-CTN-004", "no verb outside the read allow-list succeeds, for any reader"),
    ("L2-G", "V-CTN-001", "no reader grant is open-ended (aggregation / resourceNames)"),
    ("L2-H", "V-CTN-001", "every question this suite generated was actually asked and answered"),
)
ARM_IDS = tuple(a[0] for a in ARMS)

MAX_EXAMPLES = 6


def load(repo: str, rel: str, modname: str):
    path = pathlib.Path(repo) / rel
    if not path.exists():
        raise SystemExit(
            "FAIL: %s is missing; there is nothing to import, and restating its constants here "
            "would make this file a second definition site for them (V-MET-013)." % rel
        )
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def l0_reader(repo):
    return load(repo, "dev/tests/reader-holds-only-read-verbs.py", "reader_holds_only_read_verbs")


def l0_rbac(repo):
    return load(repo, "dev/tests/controller-mints-no-rbac.py", "controller_mints_no_rbac")


# -------------------------------------------------------------------------------------------
# Tier derivation. Two definition sites, compared rather than merged.
# -------------------------------------------------------------------------------------------

CRD = "k8s-operator/config/crd/bases/kubeagents.x-k8s.io_agents.yaml"
TIER_ENUM = re.compile(
    r"^(?P<ind>\s+)tier:\s*$(?P<body>(?:\n(?P=ind)\s+.*|\n\s*)*)", re.MULTILINE
)
ENUM_ITEM = re.compile(r"^\s+-\s+([a-z][a-z0-9-]*)\s*$", re.MULTILINE)


def tiers_from_tree(repo: str) -> list[str]:
    """One directory per tier under agents/, identified by the persona file every tier has."""
    root = pathlib.Path(repo) / "agents"
    return sorted(d.name for d in root.iterdir() if (d / "SOUL.md").is_file()) if root.is_dir() else []


def tiers_from_crd(repo: str) -> list[str]:
    """The `spec.tier` enum, which is what the API server will actually accept."""
    path = pathlib.Path(repo) / CRD
    if not path.exists():
        return []
    m = TIER_ENUM.search(path.read_text(encoding="utf-8"))
    if not m:
        return []
    body = m.group("body")
    head = body.split("type: string", 1)[0]
    if "enum:" not in head:
        return []
    return sorted(set(ENUM_ITEM.findall(head.split("enum:", 1)[1])))


# -------------------------------------------------------------------------------------------
# Live state
# -------------------------------------------------------------------------------------------


def _items(path: pathlib.Path) -> list:
    if not path.exists():
        return []
    try:
        doc = json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise SystemExit("FAIL: %s is not JSON (%s); the collection step did not produce a "
                         "readable document, and an empty roster reads as containment." % (path, exc))
    return doc.get("items") or []


def role_key(kind: str, namespace: str, name: str) -> str:
    return "%s/%s/%s" % (kind, namespace if kind == "Role" else "", name)


def collect(repo: str, work: str) -> dict:
    w = pathlib.Path(work)
    mod = l0_reader(repo)

    readers = {}
    for sa in _items(w / "sas.json"):
        meta = sa.get("metadata") or {}
        labels = meta.get("labels") or {}
        tier = labels.get(mod.TIER_LABEL, "")
        entry = {
            "namespace": meta.get("namespace", ""),
            "name": meta.get("name", ""),
            "tier": tier,
        }
        entry["subject"] = "system:serviceaccount:%s:%s" % (entry["namespace"], entry["name"])
        readers.setdefault(tier, []).append(entry)

    roles = {}
    for r in _items(w / "roles.json"):
        meta = r.get("metadata") or {}
        kind = r.get("kind") or ""
        roles[role_key(kind, meta.get("namespace", ""), meta.get("name", ""))] = {
            "kind": kind,
            "namespace": meta.get("namespace", ""),
            "name": meta.get("name", ""),
            "labels": meta.get("labels") or {},
            "rules": r.get("rules") or [],
            "aggregationRule": r.get("aggregationRule"),
        }

    by_subject = {}
    for tier, entries in readers.items():
        for e in entries:
            by_subject[(e["namespace"], e["name"])] = tier

    bindings = []
    for b in _items(w / "bindings.json"):
        meta = b.get("metadata") or {}
        hits = []
        for s in b.get("subjects") or []:
            if s.get("kind") != "ServiceAccount":
                continue
            ns = s.get("namespace") or meta.get("namespace", "")
            tier = by_subject.get((ns, s.get("name", "")))
            if tier:
                hits.append({"namespace": ns, "name": s.get("name", ""), "tier": tier})
        if not hits:
            continue
        ref = b.get("roleRef") or {}
        bindings.append({
            "kind": b.get("kind") or "",
            "name": meta.get("name", ""),
            "namespace": meta.get("namespace", ""),
            "roleRef": {"kind": ref.get("kind", ""), "name": ref.get("name", "")},
            "readers": hits,
        })

    served = {}
    for f in sorted(w.glob("disc.*.json")):
        group = f.name[len("disc."):-len(".json")]
        if group == "-":
            group = ""
        try:
            doc = json.loads(f.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            continue
        for res in doc.get("resources") or []:
            name = res.get("name", "")
            if not name or "/" in name:
                continue
            served["%s|%s" % (group, name)] = {
                "group": group,
                "name": name,
                "namespaced": bool(res.get("namespaced")),
                "verbs": sorted(res.get("verbs") or []),
            }

    namespaces = sorted(
        (n.get("metadata") or {}).get("name", "") for n in _items(w / "namespaces.json")
    )

    return {
        "tiers_tree": tiers_from_tree(repo),
        "tiers_crd": tiers_from_crd(repo),
        "readers": readers,
        "roles": roles,
        "bindings": bindings,
        "served": served,
        "namespaces": [n for n in namespaces if n],
    }


# -------------------------------------------------------------------------------------------
# Derivation: scope and grants, per tier
# -------------------------------------------------------------------------------------------


def tier_bindings(state: dict, tier: str) -> list:
    return [b for b in state["bindings"] if any(h["tier"] == tier for h in b["readers"])]


def tier_scope(state: dict, tier: str) -> tuple:
    """(cluster_wide, [namespaces]) — read off the bindings, never assumed from the tier name."""
    cluster = False
    nss = set()
    for b in tier_bindings(state, tier):
        if b["kind"] == "ClusterRoleBinding":
            cluster = True
        else:
            nss.add(b["namespace"])
    return cluster, sorted(nss)


def served_groups(state: dict) -> list:
    return sorted({v["group"] for v in state["served"].values()})


def expand(state: dict, rule: dict):
    """A rule -> the concrete (group, resource, verbs) it grants, `*` resolved against discovery."""
    groups = rule.get("apiGroups") or []
    resources = rule.get("resources") or []
    verbs = set(rule.get("verbs") or [])
    all_groups = served_groups(state)
    for g in groups:
        for gg in (all_groups if g == "*" else [g]):
            names = set()
            for r in resources:
                if r == "*":
                    names |= {
                        v["name"] for v in state["served"].values() if v["group"] == gg
                    }
                elif "/" not in r:
                    names.add(r)
            for n in sorted(names):
                yield gg, n, set(verbs)


def tier_grants(state: dict, tier: str) -> dict:
    """{(group, resource): {verbs}} — the union of every role every binding of this tier names."""
    out = {}
    for b in tier_bindings(state, tier):
        ref = b["roleRef"]
        role = state["roles"].get(role_key(ref["kind"], b["namespace"], ref["name"]))
        if not role:
            continue
        for rule in role["rules"]:
            for g, n, verbs in expand(state, rule):
                out.setdefault("%s|%s" % (g, n), set()).update(verbs)
    return out


def read_grants(state: dict, tier: str, read_verbs) -> dict:
    grants = tier_grants(state, tier)
    out = {}
    for key, verbs in grants.items():
        got = set(read_verbs) if "*" in verbs else (verbs & set(read_verbs))
        if got:
            out[key] = got
    return out


def resource_word(group: str, name: str) -> str:
    return name if not group else "%s.%s" % (name, group)


def find_served(state: dict, name: str):
    """The served (group, resource) entries with this plural — how an escalation target is located."""
    return [v for v in state["served"].values() if v["name"] == name]


# -------------------------------------------------------------------------------------------
# Question generation
# -------------------------------------------------------------------------------------------


class Q:
    __slots__ = QCOLS

    def __init__(self, **kw):
        for c in QCOLS:
            setattr(self, c, kw.get(c, ""))

    def row(self) -> str:
        return "\t".join(str(getattr(self, c)) for c in QCOLS)


def default_loc(cluster: bool, nss: list, namespaced: bool) -> str:
    if not namespaced:
        return LOC_CLUSTER
    if cluster:
        return LOC_ALL
    return nss[0] if nss else LOC_CLUSTER


def generate(state: dict, repo: str) -> list:
    rd = l0_reader(repo)
    rb = l0_rbac(repo)
    read_verbs = tuple(rd.READ_VERBS)

    qs = []
    n = 0

    def add(arm, subject, loc, verb, resource, sub, expect, why):
        nonlocal n
        n += 1
        qs.append(Q(qid="q%05d" % n, arm=arm, subject=subject, loc=loc, verb=verb,
                    resource=resource, subresource=sub, expect=expect, why=why))

    all_served = sorted(state["served"].items())
    tiers = sorted(state["readers"])

    for tier in tiers:
        for reader in sorted(state["readers"][tier], key=lambda e: (e["namespace"], e["name"])):
            subj = reader["subject"]
            cluster, nss = tier_scope(state, tier)
            granted = read_grants(state, tier, read_verbs)
            if not granted:
                continue

            # ---- L2-C: the yes-side, derived from what this tier's own roles grant -------------
            for key in sorted(granted):
                meta = state["served"].get(key)
                if not meta:
                    continue  # granted but not served: there is no question to ask
                word = resource_word(meta["group"], meta["name"])
                if cluster:
                    loc = LOC_ALL if meta["namespaced"] else LOC_CLUSTER
                    for v in sorted(granted[key]):
                        add("L2-C", subj, loc, v, word, SUB_NONE, "yes",
                            "%s is inside the %s tier's scope" % (word, tier))
                elif meta["namespaced"]:
                    for ns in nss:
                        for v in sorted(granted[key]):
                            add("L2-C", subj, ns, v, word, SUB_NONE, "yes",
                                "%s in %s is the %s tier's own scope" % (word, ns, tier))

            # ---- L2-D: the namespace axis --------------------------------------------------
            # Only a namespace-bound reader has one. A cluster-bound reader's scope IS the cluster,
            # and inventing an "other namespace" for it would be asserting a boundary the design
            # does not claim -- the boundary it does claim is the resource axis, which is L2-E.
            if not cluster and nss:
                others = [ns for ns in state["namespaces"] if ns not in nss]
                # The full granted set, in the namespace that hosts another tier's reader. Derived
                # as "somewhere the design put a different principal", not picked for convenience:
                # if any namespace is going to be reachable by accident it is one the install
                # already writes RBAC into.
                neighbours = sorted({
                    e["namespace"]
                    for t, entries in state["readers"].items() if t != tier
                    for e in entries
                    if e["namespace"] not in nss
                })
                for nb in neighbours:
                    for key in sorted(granted):
                        meta = state["served"].get(key)
                        if not meta or not meta["namespaced"]:
                            continue
                        word = resource_word(meta["group"], meta["name"])
                        for v in sorted(granted[key]):
                            add("L2-D", subj, nb, v, word, SUB_NONE, "no",
                                "%s is not the %s tier's namespace" % (nb, tier))
                # One probe per granted apiGroup in EVERY other namespace, plus all-namespaces.
                # The probe is the lexicographically first served namespaced resource of that
                # group: a rule, so it moves when the API surface moves, and not a name in a list.
                probes = {}
                for key in sorted(granted):
                    meta = state["served"].get(key)
                    if not meta or not meta["namespaced"]:
                        continue
                    probes.setdefault(meta["group"], meta)
                for loc in others + [LOC_ALL]:
                    if loc in neighbours:
                        continue
                    for _, meta in sorted(probes.items()):
                        word = resource_word(meta["group"], meta["name"])
                        for v in sorted(granted["%s|%s" % (meta["group"], meta["name"])]):
                            where = ("every namespace at once" if loc == LOC_ALL
                                     else "namespace %s" % loc)
                            add("L2-D", subj, loc, v, word, SUB_NONE, "no",
                                "%s is outside the %s tier's namespace scope" % (where, tier))
                # A RoleBinding cannot grant a cluster-scoped resource at all, so every granted
                # group's cluster-scoped members are a `no` this tier must give.
                for key in sorted(granted):
                    meta = state["served"].get(key)
                    if not meta or meta["namespaced"]:
                        continue
                    word = resource_word(meta["group"], meta["name"])
                    for v in sorted(granted[key]):
                        add("L2-D", subj, LOC_CLUSTER, v, word, SUB_NONE, "no",
                            "%s is cluster-scoped and the %s tier is bound in a namespace"
                            % (word, tier))

            # ---- L2-E: the resource axis ---------------------------------------------------
            for key, meta in all_served:
                if key in granted:
                    continue
                word = resource_word(meta["group"], meta["name"])
                loc = default_loc(cluster, nss, meta["namespaced"])
                add("L2-E", subj, loc, read_verbs[0], word, SUB_NONE, "no",
                    "no role bound to the %s tier's reader grants %s" % (tier, word))

            # ---- L2-F: the verb axis (V-CTN-004 at L2) -------------------------------------
            for key in sorted(granted):
                meta = state["served"].get(key)
                if not meta:
                    continue
                word = resource_word(meta["group"], meta["name"])
                loc = default_loc(cluster, nss, meta["namespaced"])
                for v in sorted(set(meta["verbs"]) - set(read_verbs)):
                    add("L2-F", subj, loc, v, word, SUB_NONE, "no",
                        "%s is not one of %s" % (v, ", ".join(read_verbs)))
            for v in sorted(rb.ESCALATION_VERBS):
                if v == "impersonate":
                    targets = sorted(rb.IDENTITY_RESOURCES)
                else:
                    targets = sorted(rb.RBAC_RESOURCES)
                for t in targets:
                    for meta in find_served(state, t):
                        if v in ("escalate", "bind") and meta["group"] != rb.RBAC_GROUP:
                            continue
                        word = resource_word(meta["group"], meta["name"])
                        loc = default_loc(cluster, nss, meta["namespaced"])
                        add("L2-F", subj, loc, v, word, SUB_NONE, "no",
                            "%s is not a write and is worse than one" % v)
            wild_loc = LOC_ALL if cluster else (nss[0] if nss else LOC_CLUSTER)
            add("L2-F", subj, wild_loc, rb.WILDCARD, rb.WILDCARD, SUB_NONE, "no",
                "a reader that answers yes to the wildcard has no scope at all")
            for t in sorted(rb.IDENTITY_RESOURCES):
                for meta in find_served(state, t):
                    word = resource_word(meta["group"], meta["name"])
                    loc = default_loc(cluster, nss, meta["namespaced"])
                    add("L2-F", subj, loc, "create", word, "token", "no",
                        "that subresource mints a credential")

    return qs


# -------------------------------------------------------------------------------------------
# Static arms (no transcript needed)
# -------------------------------------------------------------------------------------------


def arm_a(state: dict, repo: str) -> list:
    rd = l0_reader(repo)
    out = []
    tree, crd = state["tiers_tree"], state["tiers_crd"]
    if not tree:
        out.append("L2-A: no tier directory under agents/ carries a SOUL.md, so the tier universe "
                   "is empty and every per-tier assertion below would be vacuously satisfied "
                   "(LSN-035)")
        return out
    if not crd:
        out.append("L2-A: the spec.tier enum could not be read out of %s. The tier universe then "
                   "has one definition site instead of two, and a tier the API server accepts but "
                   "the tree has no directory for would be swept by nothing" % CRD)
    elif sorted(tree) != sorted(crd):
        out.append("L2-A: the tier universe disagrees between its two definition sites — "
                   "agents/ has %s, the CRD enum has %s. One of them describes a tier with no "
                   "reader identity or a reader identity with no tier" % (tree, crd))
    for tier in tree:
        entries = state["readers"].get(tier) or []
        if not entries:
            out.append("L2-A: tier %r has no ServiceAccount labelled %s=reader. A tier whose "
                       "reader cannot be named is a tier whose read scope has never been measured "
                       "at any level" % (tier, rd.ROLE_LABEL))
            continue
        if not tier_bindings(state, tier):
            out.append("L2-A: the %s tier's reader SA is a subject of no RoleBinding and no "
                       "ClusterRoleBinding. Its scope is empty, so every negative below is "
                       "satisfied by it holding nothing (LSN-035)" % tier)
            continue
        if not read_grants(state, tier, rd.READ_VERBS):
            out.append("L2-A: the %s tier's reader is bound, but no role it is bound to grants any "
                       "of %s on any served resource. The yes-side of 03 §11 has nothing to "
                       "assert" % (tier, ", ".join(rd.READ_VERBS)))
    unlabelled = [t for t in state["readers"] if not t]
    if unlabelled:
        out.append("L2-A: %d reader ServiceAccount(s) carry no %s label, so they classify to no "
                   "tier and this suite would sweep them under no scope"
                   % (len(state["readers"][""]), rd.TIER_LABEL))
    return out


def arm_b(state: dict, repo: str) -> list:
    rd = l0_reader(repo)
    out = []
    for b in state["bindings"]:
        where = "%s %s" % (b["kind"], ("%s/%s" % (b["namespace"], b["name"])).lstrip("/"))
        tiers = sorted({h["tier"] for h in b["readers"]})
        if len(tiers) > 1:
            out.append("L2-B: %s names the readers of %s in one subjects list. Whatever it grants, "
                       "it grants to all of them, and two tiers with one scope is not two scopes"
                       % (where, tiers))
        ref = b["roleRef"]
        role = state["roles"].get(role_key(ref["kind"], b["namespace"], ref["name"]))
        if not role:
            out.append("L2-B: %s names a reader and its roleRef %s/%s resolves to no Role or "
                       "ClusterRole this suite can read. An unresolvable grant is a grant nothing "
                       "below sweeps" % (where, ref["kind"], ref["name"]))
            continue
        labels = role["labels"]
        if labels.get(rd.ROLE_LABEL) != "reader":
            out.append("L2-B: %s makes the %s reader a subject of %s/%s, whose %s label is %r and "
                       "not 'reader'. One word in a subjects list is all it takes, and no reader "
                       "role changes when it happens"
                       % (where, ",".join(tiers), ref["kind"], ref["name"], rd.ROLE_LABEL,
                          labels.get(rd.ROLE_LABEL)))
            continue
        rtier = labels.get(rd.TIER_LABEL)
        if rtier not in tiers:
            out.append("L2-B: %s binds the %s reader to %s/%s, a reader role of tier %r. The "
                       "reader then holds another tier's scope, which is the boundary 03 §11 is "
                       "about" % (where, ",".join(tiers), ref["kind"], ref["name"], rtier))
    return out


def arm_g(state: dict, repo: str) -> list:
    rd = l0_reader(repo)
    out = []
    seen = 0
    for b in state["bindings"]:
        ref = b["roleRef"]
        role = state["roles"].get(role_key(ref["kind"], b["namespace"], ref["name"]))
        if not role or role["labels"].get(rd.ROLE_LABEL) != "reader":
            continue
        seen += 1
        where = "%s %s" % (role["kind"], role["name"])
        if role.get("aggregationRule"):
            out.append("L2-G: %s carries an aggregationRule. Its rules are then written by the "
                       "label selector, so any ClusterRole anyone labels correctly widens this "
                       "reader without touching its role or its binding — and nothing in the "
                       "diff of that change mentions the reader at all" % where)
        for rule in role["rules"]:
            if rule.get("resourceNames"):
                out.append("L2-G: %s bounds a rule with resourceNames %s. resourceNames is the "
                           "only axis on which an RBAC grant can name objects, so a grant that "
                           "uses it is making a claim about identity this suite's answers cannot "
                           "see" % (where, rule.get("resourceNames")))
    if not seen:
        out.append("L2-G: no binding in the roster resolves to a role labelled %s=reader, so this "
                   "arm inspected nothing (LSN-035)" % rd.ROLE_LABEL)
    return out


# -------------------------------------------------------------------------------------------
# Scoring the transcript
# -------------------------------------------------------------------------------------------


def score_transcript(state: dict, qs: list, answers: dict) -> dict:
    """{arm: [findings]} for the transcript arms, plus L2-H for anything unanswered."""
    out = {a: [] for a in ARM_IDS}
    tiers_seen = {a: set() for a in ARM_IDS}
    subj_tier = {
        e["subject"]: t for t, entries in state["readers"].items() for e in entries
    }
    counts = {a: 0 for a in ARM_IDS}

    for q in qs:
        got = answers.get(q.qid, "")
        if got not in ("yes", "no"):
            out["L2-H"].append(
                "L2-H: %s (%s: %s %s at %s as %s) came back %r, which is neither yes nor no. An "
                "unanswered question is not a denied one, and scoring it as either is how a "
                "transport failure reads as a security property"
                % (q.qid, q.arm, q.verb, q.resource, q.loc, q.subject, got)
            )
            continue
        counts[q.arm] += 1
        tiers_seen[q.arm].add(subj_tier.get(q.subject, "?"))
        if got != q.expect:
            sub = "" if q.subresource == SUB_NONE else " --subresource=%s" % q.subresource
            where = {LOC_CLUSTER: "cluster-scope", LOC_ALL: "all namespaces"}.get(
                q.loc, "namespace %s" % q.loc)
            out[q.arm].append(
                "%s: %s may %s %s%s at %s — expected %s, got %s (%s)"
                % (q.arm, q.subject, q.verb, q.resource, sub, where, q.expect, got, q.why)
            )

    # Non-vacuity, per arm and per tier. An arm that asked nothing is not an arm that passed.
    for arm in ("L2-C", "L2-E", "L2-F"):
        for tier in sorted(state["readers"]):
            if not tier:
                continue
            if tier not in tiers_seen[arm]:
                out[arm].append(
                    "%s: the %s tier contributed no question to this arm, so its verdict says "
                    "nothing about that tier (LSN-035)" % (arm, tier)
                )
    if not counts["L2-D"]:
        namespaced = [
            t for t in state["readers"] if t and not tier_scope(state, t)[0] and tier_scope(state, t)[1]
        ]
        if namespaced:
            out["L2-D"].append(
                "L2-D: %s is bound in a namespace and this arm generated no question for it. The "
                "'no in any other namespace' half of 03 §11 was not asked" % ", ".join(namespaced)
            )
    return out, counts


# -------------------------------------------------------------------------------------------
# Subcommands
# -------------------------------------------------------------------------------------------


def emit(kind: str, msg: str) -> None:
    sys.stdout.write("%s\t%s\n" % (kind, msg.replace("\t", " ").replace("\n", " ")))


def scales(counts: dict, state: dict) -> dict:
    """What each arm was measured OVER. An arm reports its own denominator or it reports nothing.

    Not cosmetic: `L2-B ... over 0 question(s)` is what a provenance arm that swept no binding
    would also print, and the two must not look alike (LSN-035).
    """
    readers = sum(len(v) for v in state["readers"].values())
    roles = len({(b["roleRef"]["kind"], b["roleRef"]["name"]) for b in state["bindings"]})
    asked = sum(counts.get(a, 0) for a in ("L2-C", "L2-D", "L2-E", "L2-F"))
    out = {
        "L2-A": "%d tier(s), %d reader identity(ies)" % (len(state["tiers_tree"]), readers),
        "L2-B": "%d binding(s) that name a reader" % len(state["bindings"]),
        "L2-G": "%d role(s) those bindings resolve to" % roles,
        "L2-H": "%d answer(s)" % asked,
    }
    for a in ("L2-C", "L2-D", "L2-E", "L2-F"):
        out[a] = "%d question(s)" % counts.get(a, 0)
    return out


def report(arm_findings: dict, counts: dict, state: dict) -> None:
    over = scales(counts, state)
    for arm, cid, statement in ARMS:
        findings = arm_findings.get(arm) or []
        scale = " over %s" % over.get(arm, "?")
        if findings:
            emit("FAIL", "%s (%s) — %s%s" % (arm, cid, statement, scale))
            for f in findings[:MAX_EXAMPLES]:
                emit("MORE", f)
            if len(findings) > MAX_EXAMPLES:
                emit("MORE", "... and %d more" % (len(findings) - MAX_EXAMPLES))
        else:
            emit("PASS", "%s (%s) — %s%s" % (arm, cid, statement, scale))


def cmd_plan(repo: str, work: str) -> int:
    state = collect(repo, work)
    qs = generate(state, repo)
    w = pathlib.Path(work)
    w.joinpath("state.json").write_text(json.dumps(state, default=list), encoding="utf-8")
    w.joinpath("questions.tsv").write_text(
        "".join(q.row() + "\n" for q in qs), encoding="utf-8"
    )
    tiers = sorted(t for t in state["readers"] if t)
    sys.stdout.write("tiers=%s readers=%d bindings=%d served=%d namespaces=%d questions=%d\n" % (
        ",".join(tiers),
        sum(len(v) for v in state["readers"].values()),
        len(state["bindings"]), len(state["served"]), len(state["namespaces"]), len(qs)))
    for t in tiers:
        cluster, nss = tier_scope(state, t)
        sys.stdout.write("  %-16s scope=%s grants=%d\n" % (
            t, "cluster-wide" if cluster else ("namespaces " + ",".join(nss) or "none"),
            len(read_grants(state, t, l0_reader(repo).READ_VERBS))))
    return 0


def cmd_score(repo: str, work: str) -> int:
    w = pathlib.Path(work)
    state = json.loads(w.joinpath("state.json").read_text(encoding="utf-8"))
    qs = []
    for line in w.joinpath("questions.tsv").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        qs.append(Q(**dict(zip(QCOLS, parts))))
    answers = {}
    for line in w.joinpath("answers.tsv").read_text(encoding="utf-8").splitlines():
        if "\t" not in line:
            continue
        qid, got = line.split("\t", 1)
        answers[qid] = got.strip()

    arm_findings, counts = score_transcript(state, qs, answers)
    arm_findings["L2-A"] = arm_a(state, repo)
    arm_findings["L2-B"] = arm_b(state, repo)
    arm_findings["L2-G"] = arm_g(state, repo)
    report(arm_findings, counts, state)
    emit("NOTE", "L2-G — NOT COVERED: '...a Cluster Admin reader no for any other cluster' "
                 "(03 §11) is not asserted here. RBAC carries no cluster axis, so the only "
                 "'object stamped with another clusterName' available is an Agent CR, whose "
                 "apiGroup no reader role grants — the no would come from L2-E and would be "
                 "identical for this cluster's own name. That is the L3 arm's property.")
    return 0


def cmd_selector(repo: str) -> int:
    sys.stdout.write("%s=reader\n" % l0_reader(repo).ROLE_LABEL)
    return 0


def cmd_groups(work: str) -> int:
    doc = json.loads((pathlib.Path(work) / "apis.json").read_text(encoding="utf-8"))
    for g in doc.get("groups") or []:
        gv = (g.get("preferredVersion") or {}).get("groupVersion")
        if gv:
            sys.stdout.write("%s\n" % gv)
    return 0


# -------------------------------------------------------------------------------------------
# The negative control: no cluster, one injected defect per arm
# -------------------------------------------------------------------------------------------

CTRL_VERBS = ("get", "list", "watch", "create", "update", "patch", "delete", "deletecollection")


def synth_state(repo: str) -> dict:
    """A cluster-shaped fixture. Concrete on purpose: it is a fixture, not a roster."""
    rd = l0_reader(repo)
    tiers = tiers_from_tree(repo) or ["platform", "cluster-admin", "developer-team"]
    tenant = "team-x"
    system = "kubeagents-system"

    served = {}

    def serve(group, name, namespaced, verbs=CTRL_VERBS):
        served["%s|%s" % (group, name)] = {
            "group": group, "name": name, "namespaced": namespaced, "verbs": sorted(verbs)}

    serve("", "pods", True)
    serve("", "secrets", True)
    serve("", "serviceaccounts", True)
    serve("", "nodes", False)
    serve("apps", "deployments", True)
    serve("rbac.authorization.k8s.io", "roles", True)
    serve("rbac.authorization.k8s.io", "clusterroles", False)
    serve("kubeagents.x-k8s.io", "agents", True)
    serve("certificates.k8s.io", "certificatesigningrequests", False)

    readers, roles, bindings = {}, {}, []
    for tier in tiers:
        namespaced_tier = tier == tiers[-1]
        ns = tenant if namespaced_tier else system
        name = "%s-agent" % tier
        readers[tier] = [{
            "namespace": ns, "name": name, "tier": tier,
            "subject": "system:serviceaccount:%s:%s" % (ns, name)}]
        kind = "Role" if namespaced_tier else "ClusterRole"
        rname = "%s-agent-explorer" % tier
        roles[role_key(kind, ns, rname)] = {
            "kind": kind, "namespace": ns if kind == "Role" else "", "name": rname,
            "labels": {rd.ROLE_LABEL: "reader", rd.TIER_LABEL: tier},
            "rules": [{"apiGroups": ["", "apps"], "resources": ["*"],
                       "verbs": list(rd.READ_VERBS)}],
            "aggregationRule": None,
        }
        bindings.append({
            "kind": "RoleBinding" if namespaced_tier else "ClusterRoleBinding",
            "name": "%s-agent-explorer" % tier,
            "namespace": ns if namespaced_tier else "",
            "roleRef": {"kind": kind, "name": rname},
            "readers": [{"namespace": ns, "name": name, "tier": tier}],
        })

    # An unrelated role the fixture can be mis-bound to, so mutation 2 has somewhere to point.
    ops = "%s-agent-operations" % tiers[-1]
    roles[role_key("Role", tenant, ops)] = {
        "kind": "Role", "namespace": tenant, "name": ops,
        "labels": {rd.ROLE_LABEL: "actor", rd.TIER_LABEL: tiers[-1]},
        "rules": [], "aggregationRule": None,
    }

    return {
        "tiers_tree": list(tiers),
        "tiers_crd": list(tiers),
        "readers": readers,
        "roles": roles,
        "bindings": bindings,
        "served": served,
        "namespaces": sorted({tenant, system, "kube-system", "default"}),
    }


def model_answer(state: dict, q, read_verbs) -> str:
    """An independent model of RBAC, so the control's transcript is computed and not copied.

    Deriving the transcript from each question's own `expect` would make every arm green by
    construction and leave the state-shaped mutations with nothing to move.
    """
    tier = None
    for t, entries in state["readers"].items():
        if any(e["subject"] == q.subject for e in entries):
            tier = t
            break
    if tier is None:
        return "no"
    cluster, nss = tier_scope(state, tier)
    if q.loc == LOC_ALL and not cluster:
        return "no"
    if q.loc == LOC_CLUSTER and not cluster:
        return "no"
    if q.loc not in (LOC_ALL, LOC_CLUSTER) and not cluster and q.loc not in nss:
        return "no"
    grants = tier_grants(state, tier)
    want = q.resource
    group = ""
    name = want
    if "." in want:
        name, group = want.split(".", 1)
    key = "%s|%s" % (group, name)
    verbs = grants.get(key)
    if not verbs:
        return "no"
    if q.subresource != SUB_NONE:
        return "no" if q.verb not in verbs else "yes"
    return "yes" if (q.verb in verbs or "*" in verbs) else "no"


class ControlError(Exception):
    """A mutation that did not apply. LSN-063: a MISS whose defect never landed is a defect here."""


def negative_control(repo: str) -> int:
    rd = l0_reader(repo)
    read_verbs = tuple(rd.READ_VERBS)

    def build():
        st = synth_state(repo)
        qs = generate(st, repo)
        ans = {q.qid: model_answer(st, q, read_verbs) for q in qs}
        return st, qs, ans

    def evaluate(st, qs, ans):
        findings, counts = score_transcript(st, qs, ans)
        findings["L2-A"] = arm_a(st, repo)
        findings["L2-B"] = arm_b(st, repo)
        findings["L2-G"] = arm_g(st, repo)
        return findings

    base_state, base_qs, base_ans = build()
    baseline = evaluate(base_state, base_qs, base_ans)
    dirty = {a: v for a, v in baseline.items() if v}
    if dirty:
        emit("FAIL", "the control's own fixture is not clean before any mutation: %s"
                     % {a: v[:1] for a, v in dirty.items()})
        return 1

    def first_of(qs, arm, expect):
        for q in sorted(qs, key=lambda x: x.qid):
            if q.arm == arm and q.expect == expect:
                return q
        raise ControlError("no %s question expecting %r exists in the fixture, so the mutation "
                           "aimed at that arm could not be applied (LSN-063)" % (arm, expect))

    def flip(arm, expect, to):
        def apply(st, qs, ans):
            q = first_of(qs, arm, expect)
            if ans.get(q.qid) == to:
                raise ControlError("the authorizer model already answers %r for %s, so flipping it "
                                   "changes nothing (LSN-063)" % (to, q.qid))
            ans[q.qid] = to
            return "%s %s %s at %s as %s now answers %s" % (
                arm, q.verb, q.resource, q.loc, q.subject, to)
        return apply

    def drop_reader_for_a_declared_tier(st, qs, ans):
        ghost = "gpu-operator"
        if ghost in st["tiers_tree"]:
            raise ControlError("the fixture already declares %r" % ghost)
        st["tiers_tree"].append(ghost)
        st["tiers_crd"].append(ghost)
        return "a tier both definition sites declare (%r) has no reader ServiceAccount" % ghost

    def misbind_a_reader(st, qs, ans):
        tier = st["tiers_tree"][-1]
        reader = st["readers"][tier][0]
        ops = "%s-agent-operations" % tier
        if role_key("Role", reader["namespace"], ops) not in st["roles"]:
            raise ControlError("the fixture has no non-reader role to mis-bind to")
        st["bindings"].append({
            "kind": "RoleBinding", "name": "broker-operations",
            "namespace": reader["namespace"],
            "roleRef": {"kind": "Role", "name": ops},
            "readers": [{"namespace": reader["namespace"], "name": reader["name"], "tier": tier}],
        })
        return ("the %s reader is a subject of a binding whose roleRef is an actor role "
                "(with no rules, so no answer moves and only provenance does)" % tier)

    def aggregate_a_reader_role(st, qs, ans):
        for key in sorted(st["roles"]):
            role = st["roles"][key]
            if role["labels"].get(rd.ROLE_LABEL) == "reader" and role["kind"] == "ClusterRole":
                role["aggregationRule"] = {
                    "clusterRoleSelectors": [{"matchLabels": {"rbac.example/aggregate": "true"}}]}
                return "%s %s is now written by a label selector" % (role["kind"], role["name"])
        raise ControlError("the fixture has no reader ClusterRole to aggregate")

    def lose_an_answer(st, qs, ans):
        q = sorted(qs, key=lambda x: x.qid)[0]
        ans[q.qid] = "error: the server could not find the requested resource"
        return "%s came back with a transport error instead of yes or no" % q.qid

    mutations = (
        ("L2-A", drop_reader_for_a_declared_tier,
         "a tier that both definition sites declare ships with no reader identity"),
        ("L2-B", misbind_a_reader,
         "a reader SA is added to the subjects list of a binding that is not its reader binding"),
        ("L2-C", flip("L2-C", "yes", "no"),
         "an in-scope read is refused, i.e. the tier's scope grants less than 03 §11 says"),
        ("L2-D", flip("L2-D", "no", "yes"),
         "a read succeeds in a namespace outside the tier's scope"),
        ("L2-E", flip("L2-E", "no", "yes"),
         "a read succeeds on a resource no role bound to that reader grants"),
        ("L2-F", flip("L2-F", "no", "yes"),
         "a verb outside the imported read allow-list succeeds"),
        ("L2-G", aggregate_a_reader_role,
         "a reader ClusterRole becomes aggregated, so anyone with the label widens it"),
        ("L2-H", lose_an_answer,
         "a question comes back unanswered and is scored as if it had been denied"),
    )

    caught = 0
    for target, apply, description in mutations:
        st, qs, ans = build()
        try:
            what = apply(st, qs, ans)
        except ControlError as exc:
            emit("FAIL", "control [%s] MISAPPLIED — %s. %s" % (target, description, exc))
            continue
        findings = evaluate(st, qs, ans)
        hit = bool(findings.get(target))
        collateral = sorted(a for a, v in findings.items() if v and a != target)
        extra = "" if not collateral else "; also fired: %s" % ",".join(collateral)
        if hit:
            caught += 1
            emit("PASS", "control [%s] CAUGHT — %s (%s)%s" % (target, description, what, extra))
        else:
            emit("FAIL", "control [%s] MISSED — %s (%s). The mutation applied and the arm that "
                         "owns the property stayed green%s" % (target, description, what, extra))

    if caught == len(mutations):
        emit("PASS", "every arm has a mutation that only it catches: %d/%d, one per arm, none "
                     "scored by the presence of a failure alone" % (caught, len(mutations)))
    else:
        emit("FAIL", "%d of %d mutations were caught by the arm that owns the property"
                     % (caught, len(mutations)))
    return 0


def main(argv: list) -> int:
    if len(argv) < 2:
        sys.stderr.write("usage: analyze.py <plan|score|selector|groups|control> ...\n")
        return 2
    cmd = argv[1]
    if cmd == "plan":
        return cmd_plan(argv[2], argv[3])
    if cmd == "score":
        return cmd_score(argv[2], argv[3])
    if cmd == "selector":
        return cmd_selector(argv[2])
    if cmd == "groups":
        return cmd_groups(argv[2])
    if cmd == "control":
        return negative_control(argv[2])
    sys.stderr.write("unknown subcommand %r\n" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
PYEOF

# ------------------------------------------------------------------------------------------------
# Scoring stream. Reading from a redirect rather than a pipe would already be safe, but pass()/bad()
# write their state to files regardless, so a future refactor to a pipeline cannot silently lose a
# failure the way webhook-negatives-l2.sh did.
# ------------------------------------------------------------------------------------------------
TAB="$(printf '\t')"
score_stream() {
  local kind msg
  while IFS="$TAB" read -r kind msg; do
    case "$kind" in
      PASS) pass "$msg" ;;
      FAIL) bad "$msg" ;;
      MORE) echo "        $msg" ;;
      NOTE) note "$msg" ;;
      "")   : ;;
      *)    echo "  $kind $msg" ;;
    esac
  done
}

# ------------------------------------------------------------------------------------------------
# Negative control: no cluster at all
# ------------------------------------------------------------------------------------------------
if [ "$MODE" = negative-control ]; then
  echo
  echo "== negative control — synthesised roster and transcript, one defect per arm =="
  if ! python3 "$ANALYZE" control "$REPO_ROOT" >"$WORK/control.out" 2>"$WORK/control.err"; then
    echo "FAIL: the control could not run:" >&2
    cat "$WORK/control.err" >&2
    exit 1
  fi
  [ -s "$WORK/control.err" ] && cat "$WORK/control.err" >&2
  score_stream <"$WORK/control.out"
  echo
  EXPECTED_ASSERTIONS=9
  got="$(count_of "$CNTFILE")"
  if [ "$got" -ne "$EXPECTED_ASSERTIONS" ]; then
    msg="the control scored $got assertion(s); $EXPECTED_ASSERTIONS were expected."
    bad "$msg A control that quietly stops running a mutation is the shape LSN-063 is about"
  fi
  echo "===================================================================="
  if [ "$(count_of "$FAILFILE")" -eq 0 ]; then
    echo "NEGATIVE CONTROL PASSED — $(count_of "$CNTFILE") assertion(s), V-CTN-001 / V-CTN-004"
    exit 0
  fi
  echo "NEGATIVE CONTROL FAILED — $(count_of "$FAILFILE") of $(count_of "$CNTFILE") assertion(s)"
  exit 1
fi

# ------------------------------------------------------------------------------------------------
# Live run
# ------------------------------------------------------------------------------------------------
$K version >/dev/null 2>&1 || { echo "FAIL: context '$CTX' is not reachable." >&2; exit 1; }

# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/preconditions.sh"
p10_assert_control_plane_healthy "$K" "$CTX" || exit 2

echo
echo "== collecting the roster from the cluster =="
SELECTOR="$(python3 "$ANALYZE" selector "$REPO_ROOT")" || {
  echo "FAIL: could not read the reader label out of the L0 arm." >&2; exit 1; }
echo "  reader selector (imported, not typed here): $SELECTOR"

$K get serviceaccounts -A -l "$SELECTOR" -o json >"$WORK/sas.json" 2>/dev/null || {
  echo "FAIL: could not list reader ServiceAccounts." >&2; exit 1; }
$K get rolebindings,clusterrolebindings -A -o json >"$WORK/bindings.json" 2>/dev/null || {
  echo "FAIL: could not list bindings." >&2; exit 1; }
$K get roles,clusterroles -A -o json >"$WORK/roles.json" 2>/dev/null || {
  echo "FAIL: could not list roles." >&2; exit 1; }
$K get namespaces -o json >"$WORK/namespaces.json" 2>/dev/null || {
  echo "FAIL: could not list namespaces." >&2; exit 1; }
$K get --raw /api/v1 >"$WORK/disc.-.json" 2>/dev/null || {
  echo "FAIL: could not read the core discovery document." >&2; exit 1; }
$K get --raw /apis >"$WORK/apis.json" 2>/dev/null || {
  echo "FAIL: could not read the API group list." >&2; exit 1; }

groups=0
while IFS= read -r gv; do
  [ -n "$gv" ] || continue
  g="${gv%%/*}"
  if $K get --raw "/apis/$gv" >"$WORK/disc.$g.json" 2>/dev/null; then
    groups=$((groups + 1))
  else
    rm -f "$WORK/disc.$g.json"
    echo "  WARNING: the discovery document for $gv could not be read; its resources are outside"
    echo "           every sweep below, in both directions."
  fi
done < <(python3 "$ANALYZE" groups "$WORK")
echo "  discovery: core + $groups group(s)"

echo
echo "== deriving the questions =="
python3 "$ANALYZE" plan "$REPO_ROOT" "$WORK" || {
  echo "FAIL: the derivation step failed." >&2; exit 1; }

QUESTIONS="$WORK/questions.tsv"
total="$(count_of "$QUESTIONS")"
if [ "$total" -eq 0 ]; then
  msg="the derivation produced no questions at all. Nothing below is evidence about V-CTN-001,"
  bad "$msg and an empty sweep is the shape a green run is least able to distinguish from containment (LSN-035)"
  echo "===================================================================="
  echo "V-CTN-001 at L2 — FAILED (nothing was asked)"
  exit 1
fi

# LSN-044. `auth can-i` parses a positional `TYPE/NAME` as an OBJECT, never as a subresource, so a
# resource word that has picked up a slash asks about an object nobody was ever granted — and on a
# `want_no` that is a vacuous green. Every resource here is computed from discovery, so the guard
# has to be at runtime; `--subresource=` is the only way a subresource is ever named below.
resource_word() {
  case "$1" in
    */*)
      echo "  suite bug: the resource word '$1' contains a slash, which kubectl reads as naming an" >&2
      echo "  object. Pass --subresource= instead (LSN-044)." >&2
      printf 'malformed'
      return
      ;;
  esac
  printf '%s' "$1"
}

ask_chunk() {
  local chunk="$1" out="$2"
  local qid subject loc verb res sub args ans
  : >"$out"
  while IFS="$TAB" read -r qid _ subject loc verb res sub _ _; do
    [ -n "$qid" ] || continue
    args=(auth can-i "$verb" "$(resource_word "$res")" "--as=$subject")
    case "$loc" in
      -) : ;;
      A) args+=(--all-namespaces) ;;
      *) args+=(-n "$loc") ;;
    esac
    [ "$sub" = "-" ] || args+=("--subresource=$sub")
    ans="$($K "${args[@]}" 2>/dev/null | tr -d '[:space:]')"
    case "$ans" in
      yes) ans=yes ;;
      no)  ans=no ;;
      *)   ans="unreadable:${ans:-empty}" ;;
    esac
    printf '%s\t%s\n' "$qid" "$ans" >>"$out"
  done <"$chunk"
}

echo
echo "== asking the authorizer $total question(s), $PARALLEL at a time =="
rm -f "$WORK"/chunk.* "$WORK"/ans.*
awk -v p="$PARALLEL" -v d="$WORK" 'NF{print > (d "/chunk." (NR % p))}' "$QUESTIONS"
i=0
while [ "$i" -lt "$PARALLEL" ]; do
  if [ -f "$WORK/chunk.$i" ]; then
    ask_chunk "$WORK/chunk.$i" "$WORK/ans.$i" &
  fi
  i=$((i + 1))
done
wait
cat "$WORK"/ans.* >"$WORK/answers.tsv" 2>/dev/null
echo "  answered: $(count_of "$WORK/answers.tsv") of $total"

echo
echo "== the verdict, by arm =="
python3 "$ANALYZE" score "$REPO_ROOT" "$WORK" >"$WORK/score.out" 2>"$WORK/score.err" || {
  echo "FAIL: the scoring step failed:" >&2; cat "$WORK/score.err" >&2; exit 1; }
[ -s "$WORK/score.err" ] && cat "$WORK/score.err" >&2
score_stream <"$WORK/score.out"

EXPECTED_ASSERTIONS=8
got="$(count_of "$CNTFILE")"
if [ "$got" -ne "$EXPECTED_ASSERTIONS" ]; then
  msg="$got arm(s) were scored; $EXPECTED_ASSERTIONS were expected."
  bad "$msg An arm that stops rendering a verdict is an arm whose property nothing asserts, and the run still ends in a banner"
fi

echo
echo "===================================================================="
if [ "$(count_of "$FAILFILE")" -eq 0 ]; then
  echo "V-CTN-001 at L2 — PASSED. $(count_of "$CNTFILE") arm(s), $total question(s) put to the"
  echo "  authorizer as $( (cut -f3 "$QUESTIONS" | sort -u | wc -l) | tr -d ' ') reader identities."
  echo "V-CTN-004 at L2 — PASSED (arm L2-F; the L0 arm's READ_VERBS is the allow-list, imported)."
  echo "  NOT COVERED: the 'any other cluster' clause of 03 §11 — see L2-G above and this file's"
  echo "  header. It is the L3 arm's property and this run asserts nothing about it."
  exit 0
fi
echo "V-CTN-001 / V-CTN-004 at L2 — FAILED. $(count_of "$FAILFILE") of $(count_of "$CNTFILE") arm(s)"
exit 1
