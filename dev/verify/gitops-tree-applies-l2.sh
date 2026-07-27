#!/usr/bin/env bash
# V-CMP-003 at L2 — "every shipped manifest is appliable as shipped" (Phase 8, P8-T2).
#
# WHY THIS IS AN L2 CHECK AND NOT A GREP. The obvious version of this check is a token grep for
# `REPLACE_WITH_` over examples/gitops-repo/. That check is wrong in both directions, and the reference
# tree contains a live example of each:
#
#   * `cidr: REPLACE_WITH_HUB_INFERENCE_CIDR` — FATAL. Not a fillable template: the API server's CIDR
#     validator rejects it, `apply.yml` applies the cluster tree recursively, so ONE such field made the
#     entire bundle un-appliable. This is what P8-T2 removed.
#   * `- "users/REPLACE_WITH_TEAM_LEAD_ID"` — FINE, and deliberately kept. It applies, and it matches no
#     user, so the router refuses everyone. Fail-closed and loud. Replacing it with a plausible-looking
#     numeric ID to satisfy a grep would make the tree *less* safe: a placeholder somebody misses should
#     look like a placeholder.
#
# A grep cannot tell those apart without a hand-written list of "fields that matter" — and a list I
# author is a list I can quietly extend the next time a check goes red. The API server already knows the
# difference, for free and without my opinion. So: apply the whole shipped tree with
# `--dry-run=server` and let it answer. Server-side dry-run runs admission, the CRD schema, CEL rules
# and every field validator, and mutates nothing.
#
# NOTE ON SCOPE: this is completeness, not correctness. It proves the tree can land, not that landing it
# produces a working fleet — that is the per-phase verify scripts' job.
#
# DESTRUCTIVE-TEST GUARD: scratch-GKE contexts only, anchored. Nothing here writes to the
# cluster, but --dry-run=server against a live production API server is still a request I will not send
# without an explicit scratch target.
# Exit: 0 = V-CMP-003 PROVEN · 1 = FAILED · 2 = refused target · 3 = DEFERRED.
# Usage: dev/verify/gitops-tree-applies-l2.sh [kube-context]
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions). Declared, not assumed: LSN-001 and LSN-002 each
# recurred against scripts whose authors believed the preconditions held.
#   P1 image-under-test:  none — this is a server-side dry-run of shipped YAML against the API server's own validators.
#      No first-party image participates; the operator is not even required to be installed.
#   P3 admission-recreate: every manifest UNDER TEST is applied with --dry-run=server, which runs admission in full
#      and persists nothing, so no manifest this check judges can have been grandfathered. Section 1b
#      does persist the tree's two parent Agents — see the long note there — but they are setup, not
#      subjects: each is deleted and recreated by this script rather than reused, and the shipped
#      files that declare them are still dry-run in the loop like every other file.
#   P6 runtime-authoritative: examples/gitops-repo/ as shipped, plus the rendered egress policies. The tree IS the artifact
#      under test here; there is no rendered layer above it.
set -uo pipefail

CTX="${1:-gke-scratch-kube-agents-dev}"
K="kubectl --context $CTX"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TREE="$REPO/examples/gitops-repo"

case "$CTX" in
  gke-scratch-*) : ;;
  *)
    echo "REFUSING: context '$CTX' is not a scratch cluster (destructive-test guard)." >&2
    exit 2
    ;;
esac

fail=0
pass() { echo "PASS: $1"; }
bad() {
  echo "FAIL: $1"
  fail=1
}

echo "== 0) preconditions =="
if ! $K version >/dev/null 2>&1; then
  echo "DEFERRED: context '$CTX' is not reachable."
  exit 3
fi
# P10 (LSN-026), before any claim: can this cluster still RUN the experiment? Rationale and the
# three false failures that bought it are at the definition site. rc 2 = could-not-run, never 1.
. "$REPO/dev/lib/preconditions.sh"
. "$REPO/dev/lib/parent-chain.sh"
p10_assert_control_plane_healthy "$K" "$CTX" || exit 2
if ! $K get crd agents.kubeagents.x-k8s.io >/dev/null 2>&1; then
  echo "DEFERRED: the Agent CRD is not installed on '$CTX', so the Agent CRs in the tree would be"
  echo "  rejected as an unknown kind — which is a missing prerequisite, not a defect in the tree."
  echo "  Install it first:  make -C k8s-operator install"
  exit 3
fi
pass "cluster reachable and the Agent CRD is installed"

# Namespaces the tree's manifests declare. Creating them is setup, not the thing under test: a
# NotFound namespace would fail every manifest for a reason that has nothing to do with V-CMP-003.
echo
echo "== 1) namespaces the tree targets =="
for ns in kubeagents-system team-x; do
  $K create namespace "$ns" --dry-run=client -o yaml | $K apply -f - >/dev/null 2>&1
done
pass "target namespaces present"

# --- 1b) the parent chain the tree's own Agent CRs declare ------------------------------------------
# WHY THIS EXISTS. 06 §1.2 V-6 (the cross-object ceiling, implemented in P8-T9) resolves a child
# Agent's `spec.parentRef.name` against a REAL object and refuses the child when the parent cannot be
# read — an unverifiable ceiling is not a pass. That is the right rule, and it costs this check
# something specific: `--dry-run=server` persists nothing, so a one-shot pass over the tree submits
# `clusters/cluster-a/agents/agent.yaml` (a cluster-admin child) into a cluster where its parent
# `platform-agent` has never existed, and V-6 correctly refuses it. The tree is not broken — a real
# reconciler (Argo, Flux, provision_08) retries to convergence and lands parents first. A single
# non-converging pass cannot, and `find | sort` makes it worse: `clusters/` sorts BEFORE `fleet/`, so
# the children are submitted first even though the tree declares the chain correctly.
#
# So the chain is established as SETUP, for the same reason section 1 creates namespaces: a NotFound
# parent would fail the child manifests for a reason that has nothing to do with V-CMP-003. Two
# properties keep this from becoming a way to launder a failure:
#
#   1. The parents are DISCOVERED from the tree by tier, not from a path list maintained here. A path
#      list is something I could quietly extend the next time this goes red; `grep '^  tier:'` is not.
#   2. The parent files are still dry-run in the loop below like every other file. Persisting them
#      here does not excuse them from the check — it makes them subjects twice.
#
# The apply/scaleToZero/cleanup mechanics live in dev/lib/parent-chain.sh, because this was the first
# of FOUR suites V-6 sent looking for the same three lines (verify-phase2, verify-phase3 and
# multi-agent-namespace-l2 followed within the hour). What stays here is the part that is specific to
# this check: discovering the parents from the tree rather than from a list.
echo
echo "== 1b) parent Agents the tree's children reference (setup for 06 §1.2 V-6) =="

PARENT_FILES=()
for tier in platform cluster-admin; do # platform first: cluster-admin is itself a child
  while IFS= read -r f; do
    [ -n "$f" ] && PARENT_FILES+=("$f")
  done < <(grep -rl '^kind: Agent$' "$TREE" --include='*.yaml' | xargs grep -l "^  tier: $tier\$" 2>/dev/null | sort)
done

seeded=() # entries are "<namespace>/<name>"
seed_cleanup() { unseed_parent_agents "$K" "${seeded[@]:-}"; }
trap seed_cleanup EXIT INT TERM

for pf in "${PARENT_FILES[@]:-}"; do
  [ -n "$pf" ] || continue
  prel="${pf#"$REPO"/}"
  if ref="$(seed_parent_agent "$K" "$pf")"; then
    seeded+=("$ref")
    pass "seeded parent $ref from $prel"
  else
    bad "could not seed parent from $prel, so its children cannot be judged: $ref"
  fi
done
if [ "${#PARENT_FILES[@]:-0}" -eq 0 ]; then
  bad "found no platform/cluster-admin Agent in $TREE — either the tree lost its parents or the"
  bad "  tier discovery above stopped matching, and every child below would fail V-6 spuriously"
fi

# --- 2) the whole shipped tree, server-side ---------------------------------------------------------
echo
echo "== 2) server-side dry-run over every shipped manifest =="

# No `mapfile` here: macOS ships bash 3.2 and the harness runs on it.
MANIFESTS=()
while IFS= read -r line; do
  MANIFESTS+=("$line")
done < <(
  find "$TREE" -type f \( -name '*.yaml' -o -name '*.yml' \) \
    -not -path '*/.github/*' -not -name 'kustomization.yaml' | sort
)

if [ "${#MANIFESTS[@]:-0}" -eq 0 ]; then
  bad "found no manifests under $TREE — this check would pass vacuously"
  exit 1
fi
echo "  ...: ${#MANIFESTS[@]} YAML files under examples/gitops-repo/"

skipped=0
deferred=0
applied=0
refused=0

for m in "${MANIFESTS[@]}"; do
  rel="${m#"$REPO"/}"

  # Not every YAML in a GitOps repo is a Kubernetes manifest — .circleci/config.yml is a pipeline
  # definition. Discriminate on the file's own structure (a top-level apiVersion), not on a path list
  # I maintain: a path list is something I could quietly extend the next time this goes red.
  if ! grep -qE '^apiVersion:' "$m"; then
    echo "SKIP: $rel (no top-level apiVersion — not a Kubernetes manifest)"
    skipped=$((skipped + 1))
    continue
  fi

  # Not every manifest in the tree is meant to APPLY. `policy/tests/*_negatives.yaml` exists to be
  # REFUSED — it is the fixture that proves the read-only VAP still bites — so "it applied" is the
  # failing outcome for it, not the passing one. Which question to ask is declared by the file, in a
  # `# kube-agents/expect-denied-by: <policy>` line, for the same reason the apiVersion test above is
  # structural: a list of excused paths maintained HERE is a list I could quietly extend the next time
  # this goes red. The declaration is two-sided and cannot be used as an escape hatch — marking a real
  # manifest expect-denied makes it fail the moment the API server admits it.
  #
  # This never fired on the old two-cluster Kind loop: the egress cluster this ran against had no VAP
  # installed, so the negatives fixture applied cleanly and was counted as evidence of appliability.
  # One long-lived cluster that carries the policy tree is what turned the question the right way up.
  expect_denied_by="$(sed -n 's|^# *kube-agents/expect-denied-by: *||p' "$m" | head -1 | tr -d ' \r')"
  if [ -n "$expect_denied_by" ]; then
    if ! $K get validatingadmissionpolicybinding "$expect_denied_by" >/dev/null 2>&1; then
      echo "DEFER: $rel — declares it must be refused by '$expect_denied_by', which is not bound on"
      echo "        this cluster. Its contract is unjudgeable here. Not a pass. Needs a target with"
      echo "        the policy tree applied (dev/cluster/up.sh applies it)."
      deferred=$((deferred + 1))
      continue
    fi
    if out="$($K apply --dry-run=server -f "$m" 2>&1)"; then
      bad "$rel was ADMITTED — it declares it must be REFUSED by '$expect_denied_by', and that policy"
      printf '        %s\n' "IS bound on this cluster. The policy did not fire: this is exactly the defect the fixture exists to catch."
    elif printf '%s' "$out" | grep -q "$expect_denied_by"; then
      pass "$rel REFUSED by '$expect_denied_by', as it declares it must be"
      refused=$((refused + 1))
    else
      bad "$rel was refused, but NOT by '$expect_denied_by' — a schema or RBAC error is not evidence"
      printf '        %s\n' "$(printf '%s' "$out" | head -4)"
    fi
    continue
  fi

  if out="$($K apply --dry-run=server -f "$m" 2>&1)"; then
    pass "$rel"
    applied=$((applied + 1))
    continue
  fi

  # A missing CRD is a missing PREREQUISITE, not a defect in the manifest. The Config Connector
  # types (ContainerCluster/ContainerNodePool) need the ConfigConnector addon, which no target in
  # this build has enabled, so those files are simply not judgeable here — record them as deferred
  # with the blocker named, never as a pass. The discriminator is the API server's own error text,
  # not my opinion about which files matter.
  if printf '%s' "$out" | grep -q 'no matches for kind\|ensure CRDs are installed first'; then
    echo "DEFER: $rel — $(printf '%s' "$out" | grep -o 'no matches for kind [^ ]* in version [^ ]*' | head -1)"
    echo "        blocker: that CRD is not installed on this cluster. Not a pass. Needs a target"
    echo "        that has it (Config Connector types => the ConfigConnector addon, enabled)."
    deferred=$((deferred + 1))
    continue
  fi

  bad "$rel is NOT APPLIABLE as shipped:"
  printf '        %s\n' "$(printf '%s' "$out" | head -4)"
done

echo
echo "  tree: $applied appliable · $refused refused-as-declared · $deferred deferred · $skipped not-a-manifest"

# --- 3) the installer's own rendered output ---------------------------------------------------------
# The tree is what a human copies; render_egress_policy is what the installer actually applies. Both
# have to land, and they are separate artifacts.
echo
echo "== 3) server-side dry-run over the installer's rendered egress policies =="
for spec in "platform-egress kubeagents-system platform" \
  "cluster-admin-egress kubeagents-system cluster-admin" \
  "developer-team-egress team-x developer-team"; do
  set -- $spec
  for wi in false true; do
    rendered="$(
      cd "$REPO/k8s-operator/scripts" || exit 1
      SCRIPT_DIR="$REPO/k8s-operator/scripts"
      # shellcheck disable=SC1091
      source ./common.sh --dry-run >/dev/null 2>&1
      WORKLOAD_IDENTITY_ENABLED="$wi" GKE_DATAPLANE=auto render_egress_policy "$1" "$2" "$3"
    )"
    if out="$(printf '%s\n' "$rendered" | $K apply --dry-run=server -f - 2>&1)"; then
      pass "rendered $1 (WORKLOAD_IDENTITY_ENABLED=$wi)"
    else
      bad "rendered $1 (WI=$wi) is NOT APPLIABLE:"
      printf '        %s\n' "$(printf '%s' "$out" | head -4)"
    fi
  done
done

# --- 4) negative control: the check must be able to fail --------------------------------------------
echo
echo "== 4) negative control =="
CONTROL="$(
  cd "$REPO/k8s-operator/scripts" || exit 1
  SCRIPT_DIR="$REPO/k8s-operator/scripts"
  # shellcheck disable=SC1091
  source ./common.sh --dry-run >/dev/null 2>&1
  HUB_INFERENCE_CIDR="REPLACE_WITH_HUB_INFERENCE_CIDR" \
    render_egress_policy "control-egress" "kubeagents-system" "platform"
)"
if printf '%s\n' "$CONTROL" | $K apply --dry-run=server -f - >/dev/null 2>&1; then
  bad "control: the API server ACCEPTED a policy with 'cidr: REPLACE_WITH_HUB_INFERENCE_CIDR'. This"
  bad "  check cannot detect the defect it exists for, so its passes above are not evidence."
else
  pass "control: the API server rejects a placeholder CIDR (the Phase 5 defect would be caught)"
fi

echo
if [ "$fail" -ne 0 ]; then
  echo "V-CMP-003 (L2): FAILURES ABOVE"
  exit 1
fi

if [ "$deferred" -gt 0 ]; then
  echo "V-CMP-003 (L2): PROVEN for $((applied + refused)) of $((applied + refused + deferred)) judgeable manifests"
  echo "  ($applied appliable, $refused refused by the policy each declares), plus all six rendered egress"
  echo "  policies, with the negative control firing. $deferred manifest(s) DEFERRED on a named blocker"
  echo "  (those CRDs are on no target here) — record this as PARTIAL, not pass, and close it on a"
  echo "  target that has those CRDs."
else
  echo "V-CMP-003 (L2): PROVEN — every shipped manifest lands the way it declares it should ($applied"
  echo "  appliable, $refused refused by the policy named in the file), every rendered egress policy is"
  echo "  appliable, and the check demonstrably rejects the un-appliable placeholder it was written for."
fi
exit 0
