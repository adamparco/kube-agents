#!/usr/bin/env bash
# Phase 3 (Developer Team Agent + isolation proof) — consolidated Kind verification harness.
#
# Re-runnable gate for the load-bearing inner-loop suites (07 §2 Phase 3 Accept + 03 §3/§11):
#   P3-K1  placement clause (A1)      — matching metadata.namespace==scope.namespace ADMITTED;
#                                        foreign metadata.namespace REJECTED (namespace-isolation escape)
#   P3-K2  reconciled dev-team pod    — pod bound to pre-created SA developer-team-agent;
#                                        image developer-team-agent:<tag>; tier label developer-team
#   P3-K3  read-only per-tier SAR (A2)— dev-team SA get/list/watch in team-x=yes; kube-system=no;
#                                        writes=no; cluster-scoped=no; priv-esc=no; '* *'=no
#   P3-K4  cardinality + immutability — duplicate (tier,scope) REJECTED; tier PATCH REJECTED
#   P3-K5  VAP attenuation            — delegates to negative-attenuation.sh (write/impersonate/wrong-scope DENIED)
#   P3-K6  namespace isolation shape  — default-deny netpol (podSelector {}, Ingress+Egress, no rules);
#                                        egress netpol selects tier, PURE ALLOWLIST (no 0.0.0.0/0), dry-run valid;
#                                        ExternalName aliases present
#   P3-K7  cascade render -> VAP      — render_developer_team.py identity ADMITTED (dry-run);
#                                        write-verb tamper DENIED
#
# NOT covered here (need infra beyond a single kindnet node — run separately):
#   Calico EGRESS ENFORCEMENT — kindnet does NOT enforce NetworkPolicy, so P3-K6 proves the policy is
#         STRUCTURALLY correct (shape, tier selector, zero 0.0.0.0/0, server-dry-run valid) only.
#         Enforcement (agent pod cannot reach 169.254.169.254 / the open internet) is proven on a
#         throwaway Calico cluster; see docs/build/LEDGER.md §Verification log. Do NOT read a green
#         P3-K6 as "egress is enforced on this cluster".
#   Router go-test suites — deterministic: `cd k8s-operator && go test ./...`.
#   V-G* — scratch-GKE cloud identity / cross-cluster / live chat.
#
# PREREQUISITE: the full stack is deployed to the target Kind cluster
#   (cert-manager + `make deploy` + the VAP). See INSTALL.md "Phase 2 — Kind inner loop".
#
# DESTRUCTIVE-TEST GUARD: only runs against a Kind context.
# Usage: local-dev/kind/verify-phase3.sh [kube-context]
set -uo pipefail  # -e omitted: kubectl exit codes are inspected manually.

CTX="${1:-kind-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
K="kubectl --context $CTX"
NS=kubeagents-system
NSX=team-x
TEAMX=examples/gitops-repo/clusters/cluster-a/namespaces/team-x
VAP=examples/gitops-repo/policy/vap-agent-readonly.yaml
CR="$TEAMX/60-developer-team-agent.yaml"
IDENTITY="$TEAMX/50-developer-team-identity.yaml"

case "$CTX" in
  kind-*) : ;;
  *) echo "REFUSING: context '$CTX' is not a Kind cluster (destructive-test guard)." >&2; exit 2 ;;
esac

fail=0
pass() { echo "PASS: $1"; }
bad()  { echo "FAIL: $1"; fail=1; }
cd "$REPO_ROOT"

echo "===================================================================="
echo " Phase 3 Kind verification — context: $CTX"
echo "===================================================================="

# --- Preconditions: CRD present, VAP enforcing, K8s >= 1.30 ------------------------------------
echo; echo "== preconditions =="
$K get crd agents.kubeagents.x-k8s.io >/dev/null 2>&1 && pass "agents CRD present" || bad "agents CRD missing (run make deploy)"
$K apply -f "$VAP" >/dev/null 2>&1 || { echo "could not apply VAP"; exit 1; }
fp="$($K get validatingadmissionpolicy kube-agents-agent-readonly -o jsonpath='{.spec.failurePolicy}' 2>/dev/null)"
[ "$fp" = "Fail" ] && pass "VAP failurePolicy=Fail" || bad "VAP failurePolicy is '$fp' (must be Fail — HALT cond 3)"
minor="$($K version -o json 2>/dev/null | grep -m1 '"minor"' | grep -oE '[0-9]+' | head -1)"
[ -n "$minor" ] && [ "$minor" -ge 30 ] && pass "K8s >= 1.30 (minor=$minor, VAP GA)" || bad "K8s minor=$minor < 30 (VAP not GA — HALT cond 3)"

# --- P3-K1: placement clause (A1) — apply namespace prereqs + identity, then CR ----------------
# The egress netpol (30-) carries REPLACE_WITH_* CIDR placeholders (invalid CIDRs) so it is NOT
# applied live here — it is substituted + dry-run validated in P3-K6. Everything else applies clean.
echo; echo "== P3-K1: placement clause (metadata.namespace must equal scope.namespace) =="
for f in 00-namespace 10-resourcequota 20-netpol-default-deny 40-service-aliases 50-developer-team-identity; do
  $K apply -f "$TEAMX/$f.yaml" >/dev/null 2>&1 && pass "$f applied" || bad "$f apply failed (unexpected)"
done
$K apply -f "$CR" >/dev/null 2>&1 && pass "dev-team Agent CR admitted (metadata.namespace==scope.namespace)" || bad "dev-team Agent CR rejected (unexpected)"
# Foreign placement: same scope.namespace=team-x, but metadata.namespace=default (2-space, unquoted
# metadata line only — scope.namespace stays "team-x"). Placement is validated BEFORE the cardinality
# List, so this rejects on the placement message even though the (tier,scope) key collides.
mismatch="$(sed 's/^  namespace: team-x$/  namespace: default/' "$CR")"
out="$(printf '%s' "$mismatch" | $K apply -f - 2>&1)"; rc=$?
if [ $rc -ne 0 ] && echo "$out" | grep -qiE 'must equal spec.scope.namespace|scoped namespace'; then
  pass "foreign metadata.namespace REJECTED (namespace-isolation escape blocked)"
else
  bad "foreign-placement CR not rejected on the placement clause (HALT cond 7): $out"
  printf '%s' "$mismatch" | $K -n default delete -f - >/dev/null 2>&1 || true
fi

# --- P3-K2: reconciled dev-team pod — pre-created SA + dev-team image + tier label --------------
echo; echo "== P3-K2: reconciled dev-team pod (SA / image / tier label) =="
sleep 6
sel="app=developer-team-team-x-gateway"
sa="$($K -n $NSX get pod -l "$sel" -o jsonpath='{.items[0].spec.serviceAccountName}' 2>/dev/null)"
img="$($K -n $NSX get pod -l "$sel" -o jsonpath='{.items[0].spec.containers[0].image}' 2>/dev/null)"
tier="$($K -n $NSX get pod -l "$sel" -o jsonpath='{.items[0].metadata.labels.kube-agents/tier}' 2>/dev/null)"
[ "$sa" = "developer-team-agent" ] && pass "pod bound to pre-created SA developer-team-agent" || bad "pod SA is '$sa' (HALT cond 4)"
case "$img" in *developer-team-agent:*) pass "pod image is developer-team-agent:<tag> ($img)";; *) bad "pod image '$img' not developer-team-agent:<tag> (HALT cond 4)";; esac
[ "$tier" = "developer-team" ] && pass "pod carries kube-agents/tier=developer-team" || bad "pod tier label is '$tier'"

# --- P3-K3: read-only per-tier SAR for the dev-team SA (A2 isolation) --------------------------
echo; echo "== P3-K3: read-only + namespace-scoped SAR for the developer-team SA =="
SA="system:serviceaccount:$NSX:developer-team-agent"
for v in get list watch; do
  a="$($K auth can-i $v pods --as=$SA -n $NSX 2>/dev/null)"
  [ "$a" = "yes" ] && pass "$v pods in $NSX = yes" || bad "$v pods in $NSX = '$a' (expected yes)"
done
# Cross-namespace reads must be denied (namespaced Role, not a ClusterRole).
a="$($K auth can-i get pods --as=$SA -n kube-system 2>/dev/null)"; [ "$a" = "no" ] && pass "get pods in kube-system = no (namespace-scoped)" || bad "get pods in kube-system = '$a' (HALT cond 1 — isolation escape)"
# Cluster-scoped reads must be denied.
a="$($K auth can-i get nodes --as=$SA 2>/dev/null)"; [ "$a" = "no" ] && pass "get nodes (cluster-scoped) = no" || bad "get nodes = '$a' (HALT cond 1)"
# Writes in its own namespace must be denied (read-only invariant).
for v in create update patch delete deletecollection; do
  a="$($K auth can-i $v secrets --as=$SA -n $NSX 2>/dev/null)"
  [ "$a" = "no" ] && pass "$v secrets in $NSX = no" || bad "$v secrets in $NSX = '$a' (HALT cond 1 — read-only invariant)"
done
# Privilege escalation must be denied.
for pair in "impersonate users" "escalate roles.rbac.authorization.k8s.io" "bind roles.rbac.authorization.k8s.io" "create roles.rbac.authorization.k8s.io"; do
  a="$($K auth can-i $pair --as=$SA -n $NSX 2>/dev/null)"
  [ "$a" = "no" ] && pass "$pair = no" || bad "$pair = '$a' (privilege escalation — HALT cond 1)"
done
a="$($K auth can-i '*' '*' --as=$SA -A 2>/dev/null)"; [ "$a" = "no" ] && pass "'* *' superuser = no" || bad "SA is a superuser (HALT cond 1)"

# --- P3-K4: cardinality (duplicate) + tier immutability ---------------------------------------
echo; echo "== P3-K4: duplicate (tier,scope) + tier immutability =="
dup="$(sed 's/^  name: developer-team-team-x$/  name: developer-team-team-x-dup/' "$CR")"
out="$(printf '%s' "$dup" | $K apply -f - 2>&1)"; rc=$?
if [ $rc -ne 0 ] && echo "$out" | grep -qiE 'Duplicate|must be unique'; then pass "duplicate (tier,scope) REJECTED by webhook"; else
  bad "duplicate not rejected as a cardinality violation (HALT cond 7): $out"; printf '%s' "$dup" | $K delete -f - >/dev/null 2>&1 || true; fi
out="$($K -n $NSX patch agent developer-team-team-x --type=merge -p '{"spec":{"tier":"platform"}}' 2>&1)"; rc=$?
if [ $rc -ne 0 ] && echo "$out" | grep -qiE 'immutable'; then pass "tier PATCH REJECTED (immutable)"; else bad "tier PATCH not rejected (HALT cond 7): $out"; fi

# --- P3-K5: VAP attenuation (delegate to the adversarial script) -------------------------------
echo; echo "== P3-K5: VAP attenuation (negative-attenuation.sh) =="
if bash local-dev/tests/negative-attenuation.sh "$CTX"; then pass "VAP attenuation suite green"; else bad "VAP attenuation suite FAILED (HALT cond 1)"; fi

# --- P3-K6: namespace isolation shape (structural; Calico enforcement deferred) ----------------
echo; echo "== P3-K6: namespace isolation netpol shape + ExternalName aliases =="
DD="$TEAMX/20-netpol-default-deny.yaml"
EG="$TEAMX/30-netpol-developer-team-egress.yaml"
AL="$TEAMX/40-service-aliases.yaml"
# default-deny: empty podSelector + both Ingress and Egress in policyTypes.
if grep -qE 'podSelector:\s*\{\}' "$DD" && grep -q '\- Ingress' "$DD" && grep -q '\- Egress' "$DD"; then
  pass "default-deny selects all pods (podSelector {}) for Ingress+Egress"
else bad "default-deny netpol is not a full Ingress+Egress deny (03 §3 isolation baseline)"; fi
# egress: selects the dev-team tier, and is a PURE ALLOWLIST — no 0.0.0.0/0 anywhere.
grep -qE 'kube-agents/tier:\s*developer-team' "$EG" && pass "egress netpol selects kube-agents/tier=developer-team" || bad "egress netpol does not select the dev-team tier"
# Strip full-line comments first — the rationale comments legitimately mention "0.0.0.0/0".
if grep -vE '^[[:space:]]*#' "$EG" | grep -qE '0\.0\.0\.0/0'; then bad "egress netpol contains a 0.0.0.0/0 rule (metadata-server / open-internet escape — HALT cond 1)"; else pass "egress netpol has NO 0.0.0.0/0 (pure allowlist)"; fi
# egress is structurally valid once the CIDR placeholders are filled (server dry-run).
sub="$(sed -E 's#REPLACE_WITH_HUB_INFERENCE_CIDR#10.8.0.16/32#; s#REPLACE_WITH_HUB_MINTY_CIDR#10.8.0.32/32#; s#REPLACE_WITH_GITHUB_CIDRS#140.82.112.0/20#; s#REPLACE_WITH_MCP_GROUNDING_CIDRS#10.8.0.64/32#' "$EG")"
printf '%s' "$sub" | $K apply --dry-run=server -f - >/dev/null 2>&1 && pass "egress netpol is server-dry-run valid (CIDRs substituted)" || bad "egress netpol failed server dry-run after CIDR substitution"
# ExternalName aliases present (in-namespace stable names for the hub services).
if grep -q 'type: ExternalName' "$AL" && grep -q 'name: litellm' "$AL" && grep -q 'name: github-token-minter' "$AL"; then
  pass "ExternalName aliases present (litellm, github-token-minter)"
else bad "ExternalName service aliases missing (litellm / github-token-minter)"; fi
echo "  NOTE: kindnet does not enforce NetworkPolicy — egress ENFORCEMENT is proven on Calico separately."

# --- P3-K7: cascade render -> VAP dry-run (rendered admit; write-verb tamper deny) -------------
echo; echo "== P3-K7: cascade render -> VAP dry-run =="
if command -v python3 >/dev/null 2>&1; then
  TMP="$(mktemp -d)"
  agents/cluster-admin/skills/propose-developer-team/scripts/render_developer_team.py \
      --cluster cluster-a --namespace team-x --project-id demo-proj --location us-central1 \
      --team-lead-chat-id users/1 --hub-inference-cidr 10.8.0.16/32 --hub-minty-cidr 10.8.0.32/32 \
      --github-cidrs 140.82.112.0/20 --mcp-cidrs 10.8.0.64/32 --repo-root "$TMP" >/dev/null 2>&1
  RID="$TMP/clusters/cluster-a/namespaces/team-x/50-developer-team-identity.yaml"
  if [ -f "$RID" ]; then
    $K apply --server-side --dry-run=server -f "$RID" >/dev/null 2>&1 && pass "rendered dev-team identity admitted by VAP (dry-run)" || bad "rendered identity rejected (unexpected)"
    tampered="$(sed 's/verbs: \["get", "list", "watch"\]/verbs: ["get", "list", "watch", "create"]/' "$RID")"
    out="$(printf '%s' "$tampered" | $K apply --server-side --dry-run=server -f - 2>&1)"; rc=$?
    if [ $rc -ne 0 ] && echo "$out" | grep -qiE 'read verbs|Forbidden'; then pass "write-verb tamper DENIED by VAP (dry-run)"; else bad "write-verb tamper not denied (HALT cond 1): $out"; fi
  else bad "render produced no identity file at $RID"; fi
  rm -rf "$TMP"
else echo "  (skip P3-K7 — python3 not found)"; fi

echo
echo "===================================================================="
if [ "$fail" -eq 0 ]; then echo " Phase 3 Kind verification: ALL CHECKS PASSED"; else echo " Phase 3 Kind verification: FAILURES ABOVE (see HALT conditions)"; fi
echo "===================================================================="
exit "$fail"
