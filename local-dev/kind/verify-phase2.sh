#!/usr/bin/env bash
# Phase 2 (Cluster Admin Agent + cascade) — consolidated Kind verification harness.
#
# Re-runnable gate for the load-bearing inner-loop suites (07 §2 Accept a–e + 03 §11 negatives):
#   V-K1  post-rename webhook serving   — duplicate (tier,scope) REJECTED; tier PATCH REJECTED
#   V-K2  VAP attenuation               — delegates to negative-attenuation.sh (write/impersonate/wrong-scope DENIED)
#   V-K3  read-only per-tier SAR        — cluster-admin SA get/list/watch=yes; writes+priv-esc=no
#   V-K8  cascade render -> VAP dry-run — rendered identity ADMITTED; write-verb tamper DENIED
#   V-K9  bootstrap ordering (partial)  — Agent CR before CRD FAILS; in-order reconciles pod bound to pre-created SA
#   V-K10 no-break-glass                — controller/router ClusterRoles grant no write on rbac resources
#
# NOT covered here (need infra beyond a single Kind node — run separately):
#   V-K11 egress enforcement — needs a NetworkPolicy-enforcing CNI (kindnet does NOT enforce).
#         Proven on a throwaway Calico cluster; see docs/build/LEDGER.md §Verification log.
#   V-K4/K5/K6/K7 — deterministic go-test suites: `cd k8s-operator && go test ./...`.
#   V-G1..V-G4 — scratch-GKE cloud identity / cross-cluster / live chat / live cascade.
#
# PREREQUISITE: the full stack is deployed to the target Kind cluster
#   (cert-manager + `make deploy` + the VAP). See INSTALL.md "Phase 2 — Kind inner loop".
#
# DESTRUCTIVE-TEST GUARD: only runs against a Kind context.
# Usage: local-dev/kind/verify-phase2.sh [kube-context]
set -uo pipefail  # -e omitted: kubectl exit codes are inspected manually.

CTX="${1:-kind-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
K="kubectl --context $CTX"
NS=kubeagents-system
AGENT=examples/gitops-repo/clusters/cluster-a/agents/agent.yaml
IDENTITY=examples/gitops-repo/clusters/cluster-a/agents/identity/cluster-admin-identity.yaml
VAP=examples/gitops-repo/policy/vap-agent-readonly.yaml

case "$CTX" in
  kind-*) : ;;
  *) echo "REFUSING: context '$CTX' is not a Kind cluster (destructive-test guard)." >&2; exit 2 ;;
esac

fail=0
pass() { echo "PASS: $1"; }
bad()  { echo "FAIL: $1"; fail=1; }
cd "$REPO_ROOT"

echo "===================================================================="
echo " Phase 2 Kind verification — context: $CTX"
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

# --- V-K9 (out-of-order half): Agent CR before CRD would fail (proved on fresh cluster). --------
# Here the CRD exists, so instead prove identity-before-pod: apply identity + CR in order.
echo; echo "== V-K9: in-order identity -> Agent CR; pod binds pre-created SA =="
$K apply -f "$IDENTITY" >/dev/null 2>&1 && pass "identity applied (VAP-clean read-only ClusterRole admitted)" || bad "identity apply failed"
$K apply -f "$AGENT" >/dev/null 2>&1 && pass "Agent CR admitted by webhook" || bad "Agent CR rejected (unexpected)"
sleep 6
sa="$($K -n $NS get pod -l app=cluster-admin-cluster-a-gateway -o jsonpath='{.items[0].spec.serviceAccountName}' 2>/dev/null)"
img="$($K -n $NS get pod -l app=cluster-admin-cluster-a-gateway -o jsonpath='{.items[0].spec.containers[0].image}' 2>/dev/null)"
tier="$($K -n $NS get pod -l app=cluster-admin-cluster-a-gateway -o jsonpath='{.items[0].metadata.labels.kube-agents/tier}' 2>/dev/null)"
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
if bash local-dev/tests/negative-attenuation.sh "$CTX"; then pass "VAP attenuation suite green"; else bad "VAP attenuation suite FAILED (HALT cond 1)"; fi

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
if [ "$fail" -eq 0 ]; then echo " Phase 2 Kind verification: ALL CHECKS PASSED"; else echo " Phase 2 Kind verification: FAILURES ABOVE (see HALT conditions)"; fi
echo "===================================================================="
exit "$fail"
