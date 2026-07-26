#!/usr/bin/env bash
# Phase 2 (Cluster Admin Agent + cascade) — consolidated L2 verification harness.
#
# Re-runnable gate for the load-bearing inner-loop suites (07 §2 Accept a–e + 03 §11 negatives):
#   V-K1  post-rename webhook serving   — duplicate (tier,scope) REJECTED; tier PATCH REJECTED
#   V-K2  VAP attenuation               — delegates to negative-attenuation.sh (write/impersonate/wrong-scope DENIED)
#   V-K3  read-only per-tier SAR        — cluster-admin SA get/list/watch=yes; writes+priv-esc=no
#   V-K8  cascade render -> VAP dry-run — rendered identity ADMITTED; write-verb tamper DENIED
#   V-K9  bootstrap ordering (partial)  — Agent CR before CRD FAILS; in-order reconciles pod bound to pre-created SA
#   V-K10 no-break-glass                — controller/router ClusterRoles grant no write on rbac resources
#
# NOT covered here (run separately):
#   V-K11 egress enforcement — a different claim, needing traffic rather than YAML. It is
#         `dev/verify/egress-enforcement-l2.sh`, one line up the same L2 chain.
#   V-K4/K5/K6/K7 — deterministic go-test suites: `cd k8s-operator && go test ./...`.
#   V-G1..V-G4 — scratch-GKE cloud identity / cross-cluster / live chat / live cascade.
#
# PREREQUISITE: the full stack is deployed to the target cluster (cert-manager + `make deploy` + the
#   VAP). `dev/cluster/up.sh` produces exactly that; see INSTALL.md "Method 3 — Remote Development".
#
# DESTRUCTIVE-TEST GUARD: only runs against a scratch-GKE context.
# Usage: dev/verify/verify-phase2.sh [kube-context]
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions). Written from a reading of THIS script. verify-phase7.sh
# reaches it transitively and deliberately declined to declare one on its behalf, because a
# precondition written by a caller is a guess wearing the costume of a fact.
#   P1 image-under-test:  kubeagents-system/control-plane=controller-manager — V-K1 asks whether the
#      webhook rejects a duplicate (tier,scope) and a tier PATCH, and V-K9 asks what the operator
#      rendered into the agent pod. Both answers are produced BY the operator image, so a stale
#      operator turns them into statements about code that is not in the tree (LSN-001, three
#      recurrences). Asserted via p1_assert_build_under_test. V-K3 (SAR) and V-K10 (a static grep of
#      the shipped role.yaml) do not depend on it and stay meaningful either way.
#   P3 admission-recreate: deploy/cluster-admin-cluster-a-gateway in kubeagents-system — V-K9 reads a
#      pod's serviceAccountName, image and tier label, and `apply` on an Agent CR that already exists
#      changes nothing, so the pod under inspection could predate this build entirely. Until 2026-07-25
#      this section was `apply` then `sleep 6` then read, which is LSN-002 exactly: it described the
#      past whenever the CR was already there, which is every re-run. p3_force_recreate now deletes the
#      Deployment and the controller re-renders it. V-K1's duplicate and its tier PATCH are fresh
#      admissions by construction; V-K8's tamper is a server-side dry run, which admits in full and
#      persists nothing, so nothing there can be grandfathered.
#   P6 runtime-authoritative: the live API server — the pod spec read back after reconcile, the VAP
#      failurePolicy read from the cluster, and the admission verdicts themselves. No config claim is
#      made here, so the file the operator shadows with a rendered ConfigMap at runtime (LSN-003) is
#      never read. The one file this script does judge, k8s-operator/config/rbac/role.yaml in V-K10, is
#      deliberately the SOURCE artifact: the claim is about what the shipped chart grants to every
#      installation, not about what one cluster happens to have applied.
set -uo pipefail  # -e omitted: kubectl exit codes are inspected manually.

CTX="${1:-gke-scratch-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
K="kubectl --context $CTX"
NS=kubeagents-system
AGENT=examples/gitops-repo/clusters/cluster-a/agents/agent.yaml
IDENTITY=examples/gitops-repo/clusters/cluster-a/agents/identity/cluster-admin-identity.yaml
VAP=examples/gitops-repo/policy/vap-agent-readonly.yaml

case "$CTX" in
  gke-scratch-*) : ;;
  *) echo "REFUSING: context '$CTX' is not a scratch cluster (destructive-test guard)." >&2; exit 2 ;;
esac

fail=0
pass() { echo "PASS: $1"; }
bad()  { echo "FAIL: $1"; fail=1; }
cd "$REPO_ROOT"
# P1 and P3 are executed here, not described. Both were prose in binding.md for four phases and both
# recurred anyway; the declaration block above is only honest because these two calls exist.
. "$REPO_ROOT/dev/lib/preconditions.sh"

# P10 (LSN-026), before any claim: can this cluster still RUN the experiment? Rationale and the
# three false failures that bought it are at the definition site. rc 2 = could-not-run, never 1.
p10_assert_control_plane_healthy "$K" "$CTX" || exit 2

echo "===================================================================="
echo " Phase 2 verification — context: $CTX"
echo "===================================================================="

# --- Preconditions: CRD present, stale CRD gone, VAP enforcing, K8s >= 1.30 -------------------
echo; echo "== preconditions =="
$K get crd agents.kubeagents.x-k8s.io >/dev/null 2>&1 && pass "agents CRD present" || bad "agents CRD missing (run make deploy)"
if $K get crd platformagents.kubeagents.x-k8s.io >/dev/null 2>&1; then bad "stale platformagents CRD still present (HALT cond 6)"; else pass "stale platformagents CRD gone"; fi
$K apply -f "$VAP" >/dev/null 2>&1 || { echo "could not apply VAP"; exit 1; }
fp="$($K get validatingadmissionpolicy kube-agents-agent-readonly -o jsonpath='{.spec.failurePolicy}' 2>/dev/null)"
[ "$fp" = "Fail" ] && pass "VAP failurePolicy=Fail" || bad "VAP failurePolicy is '$fp' (must be Fail — HALT cond 3)"
minor="$($K version -o json 2>/dev/null | grep -m1 '"minor"' | grep -oE '[0-9]+' | head -1)"
[ -n "$minor" ] && [ "$minor" -ge 30 ] && pass "K8s >= 1.30 (minor=$minor, VAP GA)" || bad "K8s minor=$minor < 30 (VAP not GA — HALT cond 3)"

# P1 — is the operator serving this webhook and rendering this pod the build under test? Everything
# V-K1 and V-K9 claim below is produced by that image. This runs first so the answer arrives as a
# failure rather than as a footnote under a green run (LSN-001).
p1_assert_build_under_test "$K" "$NS" control-plane=controller-manager
case "$?" in
  0) pass "P1: the running operator is the build under test" ;;
  3) echo "  DEFERRED (not faked): P1 unverifiable — reason printed above; V-K1/V-K9 below are about an unknown build." ;;
  *) bad "P1: the cluster is NOT running the build under test (LSN-001 — V-K1/V-K9 describe other code)" ;;
esac

# This script OWNS the Agent CR it applies, and takes it away again. On the disposable Kind clusters
# this suite was written against, ownership was free: the cluster went in the bin after the campaign,
# so "leave it behind" and "clean it up" were the same thing. One long-lived cluster makes them
# different, and the difference had two costs.
#
# The visible one: the shipped example pins ghcr.io/gke-labs/kube-agents/cluster-admin-agent:v0.1.0,
# which answers an anonymous pull with 403, so every run left a gateway wedged in ImagePullBackOff.
# That red was never evidence of anything -- nothing read it and nothing failed on it -- and it was
# equally consistent with a reverted digest patch, a full node, or a missing SA. An ambient red that
# no check owns is how the NEXT real failure gets misattributed (LSN-026). The publication gap it
# gestured at is now a ledger deferral with a blocker and a promote-when, which is a claim something
# actually checks.
#
# The invisible one, and the reason this is a fix rather than tidying: chaos-suite.sh was reading this
# leftover CR as a fixture it never created. It passed only because phase 2 happened to run first on a
# cluster nobody reset. It now applies its own.
#
# Deleting the CR is enough -- the Deployment, ReplicaSet and pod are ownerReferenced to it, and the
# Agent CRD carries no finalizer, so the CR goes immediately and its children follow via GC. The
# identity ClusterRole/SA and the VAP are deliberately NOT deleted: they are cluster fixtures other
# suites depend on, and unlike the CR they are not a rendering of the operator under test.
#
# Placed after P1 and before the first apply, so it is armed for every exit path below, including a
# `bad` that halts mid-suite. No assertion is weakened: the trap runs strictly after the last read,
# and :127 still asserts the ghcr tag string on the rendered pod exactly as before.
cleanup() {
  $K -n "$NS" delete agent cluster-admin-cluster-a cluster-admin-cluster-a-dup \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
}
trap cleanup EXIT

# --- V-K9 (out-of-order half): Agent CR before CRD would fail (proved on fresh cluster). --------
# Here the CRD exists, so instead prove identity-before-pod: apply identity + CR in order.
echo; echo "== V-K9: in-order identity -> Agent CR; pod binds pre-created SA =="
$K apply -f "$IDENTITY" >/dev/null 2>&1 && pass "identity applied (VAP-clean read-only ClusterRole admitted)" || bad "identity apply failed"
$K apply -f "$AGENT" >/dev/null 2>&1 && pass "Agent CR admitted by webhook" || bad "Agent CR rejected (unexpected)"
# P3 — applying an Agent CR that already exists changes nothing, so the three assertions below used to
# read whatever pod the previous build left behind (`sleep 6` cannot tell a fresh pod from an old one).
# Delete the Deployment and let the controller re-render it: that is what makes them assertions about
# the renderer in this tree rather than about the past (LSN-002).
# Since the EXIT trap above took ownership of the CR, `p3_force_recreate`'s "the Deployment does not
# exist; the next apply is a genuine admission" branch (rc 0, preconditions.sh) is now the EXPECTED
# steady state rather than a rare one -- the previous run deleted it. Do not read that as dead code
# and delete the call: it is what keeps this assertion honest on a cluster where the CR DID survive,
# which is exactly the case the trap cannot cover (a SIGKILL, or a run that never reached the trap).
p3_force_recreate "$K" "$NS" deploy/cluster-admin-cluster-a-gateway 90 \
  || bad "P3: could not force-recreate the agent Deployment — V-K9 below would be about the past (LSN-002)"
# Resolve the pod by OWNERSHIP and pin it by name — see `p3_pod_of_deploy`. This block was the twin
# of the one in verify-phase3.sh, which failed 2 runs in 3 on 2026-07-25: a selector poll matches the
# pod of the generation P3 just deleted (orphaned, not yet GC'd, so no deletionTimestamp to filter
# on), and three separate `.items[0]` reads re-list a set that is changing between them. This copy
# was not passing because it was correct, it was passing because GC happened to be quick enough here
# — the defect and the luck are both invisible from inside the run (LSN-024).
pod="$(p3_pod_of_deploy "$K" "$NS" cluster-admin-cluster-a-gateway 120)"
# Existence, not Ready, is the right bar: every field read below lives in the spec, which a Pending pod
# already has. The claim is what the operator RENDERED, which is decided at admission — requiring Ready
# would couple it to image pulls and scheduling and fail it for reasons the renderer did not cause.
[ -n "$pod" ] \
  && pass "controller re-rendered the agent pod after the forced recreate (fresh admission, current renderer): $pod" \
  || bad "no pod owned by the current agent Deployment within 120s of the forced recreate — the three assertions below would read nothing (HALT cond 4)"
sa="$($K -n $NS get pod "$pod" -o jsonpath='{.spec.serviceAccountName}' 2>/dev/null)"
img="$($K -n $NS get pod "$pod" -o jsonpath='{.spec.containers[0].image}' 2>/dev/null)"
tier="$($K -n $NS get pod "$pod" -o jsonpath='{.metadata.labels.kube-agents/tier}' 2>/dev/null)"
[ "$sa" = "cluster-admin-agent" ] && pass "pod bound to pre-created SA cluster-admin-agent" || bad "pod SA is '$sa' (HALT cond 4)"
case "$img" in *cluster-admin-agent:*) pass "pod image is cluster-admin-agent:<tag> ($img)";; *) bad "pod image '$img' not cluster-admin-agent:<tag> (HALT cond 4)";; esac
[ "$tier" = "cluster-admin" ] && pass "pod carries kube-agents/tier=cluster-admin" || bad "pod tier label is '$tier'"

# --- V-K1: live webhook serving — duplicate (tier,scope) + tier PATCH rejected -----------------
echo; echo "== V-K1: webhook serving (duplicate + tier immutability) =="
dup="$(sed 's/name: cluster-admin-cluster-a$/name: cluster-admin-cluster-a-dup/' "$AGENT")"
out="$(printf '%s' "$dup" | $K apply -f - 2>&1)"; rc=$?
if [ $rc -ne 0 ] && echo "$out" | grep -qiE 'Duplicate|must be unique'; then pass "duplicate (tier,scope) REJECTED by webhook"; else
  bad "duplicate not rejected as a cardinality violation (HALT cond 7): $out"; printf '%s' "$dup" | $K delete -f - >/dev/null 2>&1 || true; fi
out="$($K -n $NS patch agent cluster-admin-cluster-a --type=merge -p '{"spec":{"tier":"platform"}}' 2>&1)"; rc=$?
if [ $rc -ne 0 ] && echo "$out" | grep -qiE 'immutable'; then pass "tier PATCH REJECTED (immutable)"; else bad "tier PATCH not rejected (HALT cond 7): $out"; fi

# --- V-K3: read-only per-tier SAR -------------------------------------------------------------
echo; echo "== V-K3: read-only SAR for the cluster-admin SA =="
SA="system:serviceaccount:$NS:cluster-admin-agent"
for v in get list watch; do
  a="$($K auth can-i $v pods --as=$SA -A 2>/dev/null)"
  [ "$a" = "yes" ] && pass "$v pods = yes" || bad "$v pods = '$a' (expected yes)"
done
for v in create update patch delete deletecollection; do
  a="$($K auth can-i $v secrets --as=$SA -A 2>/dev/null)"
  [ "$a" = "no" ] && pass "$v secrets = no" || bad "$v secrets = '$a' (HALT cond 1 — read-only invariant)"
done
for pair in "impersonate users" "escalate clusterroles.rbac.authorization.k8s.io" "bind clusterroles.rbac.authorization.k8s.io" "create clusterroles.rbac.authorization.k8s.io"; do
  a="$($K auth can-i $pair --as=$SA -A 2>/dev/null)"
  [ "$a" = "no" ] && pass "$pair = no" || bad "$pair = '$a' (privilege escalation — HALT cond 1)"
done
a="$($K auth can-i '*' '*' --as=$SA -A 2>/dev/null)"; [ "$a" = "no" ] && pass "'* *' superuser = no" || bad "SA is a superuser (HALT cond 1)"

# --- V-K2: VAP attenuation (delegate to the adversarial script) --------------------------------
echo; echo "== V-K2: VAP attenuation (negative-attenuation.sh) =="
if bash dev/tests/negative-attenuation.sh "$CTX"; then pass "VAP attenuation suite green"; else bad "VAP attenuation suite FAILED (HALT cond 1)"; fi

# --- V-K8: cascade render -> VAP dry-run (rendered admit; write-verb tamper deny) --------------
echo; echo "== V-K8: cascade render -> VAP dry-run =="
if command -v python3 >/dev/null 2>&1; then
  TMP="$(mktemp -d)"
  ( cd agents/platform && ./skills/propose-cluster-admin/scripts/render_cluster_admin.py \
      --cluster cluster-vfy --project-id demo-proj --location us-central1 --admin-chat-id users/1 \
      --hub-inference-cidr 10.8.0.16/32 --hub-minty-cidr 10.8.0.32/32 \
      --github-cidrs 140.82.112.0/20 --mcp-cidrs 10.8.0.64/32 --repo-root "$TMP" >/dev/null 2>&1 )
  RID="$TMP/clusters/cluster-vfy/agents/identity/cluster-admin-identity.yaml"
  if [ -f "$RID" ]; then
    $K apply --server-side --dry-run=server -f "$RID" >/dev/null 2>&1 && pass "rendered identity admitted by VAP (dry-run)" || bad "rendered identity rejected (unexpected)"
    tampered="$(sed 's/verbs: \["get", "list", "watch"\]/verbs: ["get", "list", "watch", "create"]/' "$RID")"
    out="$(printf '%s' "$tampered" | $K apply --server-side --dry-run=server -f - 2>&1)"; rc=$?
    if [ $rc -ne 0 ] && echo "$out" | grep -qiE 'read verbs|Forbidden'; then pass "write-verb tamper DENIED by VAP (dry-run)"; else bad "write-verb tamper not denied (HALT cond 1): $out"; fi
  else bad "render produced no identity file"; fi
  rm -rf "$TMP"
else echo "  (skip V-K8 — python3 not found)"; fi

# --- V-K10: no-break-glass (static) -----------------------------------------------------------
echo; echo "== V-K10: no break-glass (controller/router mint no RBAC) =="
if grep -REn 'clusterroles|clusterrolebindings|"roles"|rolebindings' k8s-operator/config/rbac/role.yaml 2>/dev/null | grep -qiE 'create|update|patch|delete'; then
  bad "controller RBAC includes a write verb on rbac resources (HALT cond 1)"
else pass "controller RBAC grants no write on rbac resources"; fi

echo
echo "===================================================================="
if [ "$fail" -eq 0 ]; then echo " Phase 2 verification: ALL CHECKS PASSED"; else echo " Phase 2 verification: FAILURES ABOVE (see HALT conditions)"; fi
echo "===================================================================="
exit "$fail"
