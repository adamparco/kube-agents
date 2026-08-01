#!/usr/bin/env bash
# Phase 3 (Developer Team Agent + isolation proof) — consolidated L2 verification harness.
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
# NOT covered here (run separately):
#   EGRESS ENFORCEMENT — P3-K6 proves the policy is STRUCTURALLY correct (shape, tier selector, zero
#         0.0.0.0/0, server-dry-run valid) and nothing more. Enforcement — the agent pod actually
#         cannot reach 169.254.169.254 or the open internet — is a different claim needing traffic,
#         and it is `dev/verify/egress-enforcement-l2.sh`, one line up the same L2 chain. The split is
#         not an artifact of the substrate: it was one until 2026-07-26 (kindnet accepted these
#         policies and enforced none of them, LSN-006), but the L2 cluster now runs GKE Dataplane V2
#         and enforces for real. Two scripts because they are two claims. Do NOT read a green P3-K6 as
#         "egress is enforced on this cluster" — read egress-enforcement-l2.sh's exit code for that.
#   Router go-test suites — deterministic: `cd k8s-operator && go test ./...`.
#   V-G* — cloud identity / cross-cluster / live chat.
#
# PREREQUISITE: the full stack is deployed to the target cluster (cert-manager + `make deploy` + the
#   VAP). `dev/cluster/up.sh` produces exactly that; see INSTALL.md "Method 3 — Remote Development".
#
# DESTRUCTIVE-TEST GUARD: only runs against a scratch-GKE context.
# Usage: dev/verify/verify-phase3.sh [kube-context]
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions). Written from a reading of THIS script, not inferred by the
# gates that reach it (verify-phase5/6/7.sh all do).
#   P1 image-under-test:  kubeagents-system/control-plane=controller-manager — P3-K1 asks whether the
#      webhook enforces the placement clause, P3-K4 asks whether it enforces cardinality and tier
#      immutability, and P3-K2 asks what the operator rendered into the dev-team pod. All three are
#      answered by the operator image, and the operator runs in kubeagents-system even though the
#      objects it renders here live in team-x. Asserted via p1_assert_build_under_test. P3-K3 (SAR),
#      P3-K6 (netpol shape) and P3-K7 (renderer + VAP) do not depend on that image.
#   P3 admission-recreate: deploy/developer-team-team-x-gateway in team-x — P3-K2 reads a pod's
#      serviceAccountName, image and tier label, and re-applying an Agent CR that already exists is a
#      no-op, so the pod it read was whatever the last build left behind. This was `sleep 6` then read
#      until 2026-07-25, the same LSN-002 shape as verify-phase2 V-K9 and found the same way.
#      p3_force_recreate now deletes the Deployment so the controller re-renders it. P3-K1's
#      foreign-placement CR, P3-K4's duplicate and P3-K6/K7's dry-runs are fresh admissions already:
#      the first two are objects that do not exist until this run creates them, and a server-side dry
#      run admits in full and persists nothing.
#   P6 runtime-authoritative: the live API server for every admission verdict, pod spec and SAR answer.
#      The netpol clauses in P3-K6 are the one place this script judges working-tree YAML instead of an
#      applied object, and that is deliberate and stated: 30-netpol-*.yaml carries REPLACE_WITH_* CIDR
#      placeholders and is therefore NEVER applied live here, so the tree file is the only artifact
#      that exists to judge — which is also why P3-K6 is a SHAPE claim and not an enforcement one
#      (a file is not traffic; see the pointer above). No claim here reads the image-baked config
#      file that the operator shadows with a rendered ConfigMap at runtime (LSN-003).
set -uo pipefail  # -e omitted: kubectl exit codes are inspected manually.

CTX="${1:-gke-scratch-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
K="kubectl --context $CTX"
NS=kubeagents-system
NSX=team-x
TEAMX=examples/gitops-repo/clusters/cluster-a/namespaces/team-x
VAP=examples/gitops-repo/policy/vap-agent-readonly.yaml
CR="$TEAMX/60-developer-team-agent.yaml"
IDENTITY="$TEAMX/50-developer-team-identity.yaml"
# The dev-team CR is the BOTTOM of a three-tier chain (`parentRef: cluster-admin-cluster-a`, which is
# itself `parentRef: platform-agent`), and since P8-T9 the webhook refuses a child whose parent it
# cannot read (06 §1.2 V-6). Both ancestors are seeded from their shipped manifests, platform first —
# see dev/lib/parent-chain.sh for why that is setup and not laundering.
PARENTS=(
  examples/gitops-repo/fleet/platform-agent.yaml
  examples/gitops-repo/clusters/cluster-a/agents/agent.yaml
)

case "$CTX" in
  gke-scratch-*) : ;;
  *) echo "REFUSING: context '$CTX' is not a scratch cluster (destructive-test guard)." >&2; exit 2 ;;
esac

fail=0
pass() { echo "PASS: $1"; }
bad()  { echo "FAIL: $1"; fail=1; }
cd "$REPO_ROOT"
# P1 and P3 are executed here, not described — the block above is only honest because of these calls.
. "$REPO_ROOT/dev/lib/preconditions.sh"
. "$REPO_ROOT/dev/lib/parent-chain.sh"

# P10 (LSN-026), before any claim: can this cluster still RUN the experiment? Rationale and the
# three false failures that bought it are at the definition site. rc 2 = could-not-run, never 1.
p10_assert_control_plane_healthy "$K" "$CTX" || exit 2

echo "===================================================================="
echo " Phase 3 verification — context: $CTX"
echo "===================================================================="

# --- Preconditions: CRD present, VAP enforcing, K8s >= 1.30 ------------------------------------
echo; echo "== preconditions =="
$K get crd agents.kubeagents.x-k8s.io >/dev/null 2>&1 && pass "agents CRD present" || bad "agents CRD missing (run make deploy)"
$K apply -f "$VAP" >/dev/null 2>&1 || { echo "could not apply VAP"; exit 1; }
fp="$($K get validatingadmissionpolicy kube-agents-agent-readonly -o jsonpath='{.spec.failurePolicy}' 2>/dev/null)"
[ "$fp" = "Fail" ] && pass "VAP failurePolicy=Fail" || bad "VAP failurePolicy is '$fp' (must be Fail — HALT cond 3)"
minor="$($K version -o json 2>/dev/null | grep -m1 '"minor"' | grep -oE '[0-9]+' | head -1)"
[ -n "$minor" ] && [ "$minor" -ge 30 ] && pass "K8s >= 1.30 (minor=$minor, VAP GA)" || bad "K8s minor=$minor < 30 (VAP not GA — HALT cond 3)"

# P1 — the webhook that decides P3-K1 and P3-K4, and the renderer that produces the pod P3-K2 reads,
# are the same operator image. Ask before claiming anything about them (LSN-001).
p1_assert_build_under_test "$K" "$NS" control-plane=controller-manager
case "$?" in
  0) pass "P1: the running operator is the build under test" ;;
  3) echo "  DEFERRED (not faked): P1 unverifiable — reason printed above; the webhook/render claims below are about an unknown build." ;;
  *) bad "P1: the cluster is NOT running the build under test (LSN-001 — P3-K1/K2/K4 describe other code)" ;;
esac

# This script OWNS the Agent CRs it applies. Same reasoning as the trap in verify-phase2.sh: on the
# disposable Kind clusters these suites were written against, "leave it" and "clean it" were the same
# act; on one long-lived cluster they are not, and the leftover gateway sat in ImagePullBackOff on the
# unpublished ghcr example tag from one run to the next, owned by no check.
#
# THREE CRs, because two of them are supposed to be rejected and a trap must not assume the thing the
# suite is testing: `-dup` (P3-K4's cardinality probe) and the foreign-placement copy in `default`
# (P3-K1's isolation-escape probe). If the webhook regresses and admits either, the assertion fails
# loudly AND the object is cleaned up -- a failing run must not also poison the next one.
#
# NAMESPACE team-x AND ITS SA, RESOURCEQUOTA AND NETWORKPOLICY ARE DELIBERATELY KEPT. verify-phase4.sh
# reads the developer-team ServiceAccount for its SubjectAccessReview regression; delete it here and
# that check degrades to a note instead of failing, which is the silent-skip shape LSN-021 is about.
# The CR is a rendering of the operator under test and belongs to this run; the namespace scaffolding
# is a cluster fixture and does not.
#
# The two seeded ancestors go with them, for the same reason: they are this run's objects, they exist
# only so the dev-team CR can be submitted at all, and leaving them behind would make the NEXT run's
# V-6 pass for a reason that run did not establish.
SEEDED=()
cleanup() {
  $K -n "$NSX" delete agent developer-team-team-x developer-team-team-x-dup \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
  $K -n default delete agent developer-team-team-x \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
  unseed_parent_agents "$K" "${SEEDED[@]:-}"
}
# P12 ([[LSN-066]]): this trap is installed AFTER p10_assert_control_plane_healthy, whose
# p12_assert_exclusive_l2 took the one-suite-per-cluster lock and put `_l2_lock_exit_handler` on
# EXIT. Replacing that trap here would leak the lock to the next acquirer's stale break, so the
# release is chained in. It cannot change this script's exit status: bash runs the EXIT trap with
# the pending status and only an explicit `exit` inside the trap overrides it.
trap 'cleanup; l2_lock_release' EXIT

# --- setup: the two ancestors the dev-team CR hangs beneath (06 §1.2 V-6) ----------------------
# NOT a check. Applied platform-first, because the cluster-admin manifest is a child too and would be
# refused on its own. Without this the three dev-team CRs below are all rejected on a NotFound parent
# and P3-K1/K2/K4 report "placement" and "cardinality" failures that are nothing of the kind.
echo; echo "== setup: parent chain for the dev-team child (06 §1.2 V-6) =="
for pf in "${PARENTS[@]}"; do
  if ref="$(seed_parent_agent "$K" "$pf")"; then
    SEEDED+=("$ref")
    echo "  seeded $ref from $pf (scaleToZero; removed on exit)"
  else
    bad "could not seed $pf, so P3-K1/K2/K4 below would fail on a NotFound parent rather than on what they test: $ref"
  fi
done

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
# P3 — the CR applied above already existed, so its apply was a no-op and the pod below is whatever the
# previous build left in team-x. `sleep 6` cannot tell those apart. Delete the Deployment and let the
# controller re-render it, so the three assertions describe the renderer in this tree (LSN-002).
p3_force_recreate "$K" "$NSX" deploy/developer-team-team-x-gateway 90 \
  || bad "P3: could not force-recreate the dev-team Deployment — P3-K2 below would be about the past (LSN-002)"
# Resolve the pod by OWNERSHIP and pin it by name. The selector-based poll this replaces was flaky at
# 2-in-3 and both halves of it were wrong: `-o name | head -1` matched the PREVIOUS run's pod, which
# the recreate had orphaned but which GC had not reached yet, and then three separate `.items[0]`
# reads each re-listed a set that was changing underneath them. The symptom was always EMPTY, never a
# wrong value — one run read the SA fine and got '' for the image and the tier one call later, as GC
# removed the pod mid-sequence. `p3_pod_of_deploy` walks Deployment uid -> ReplicaSet -> Pod, so it
# cannot return a pod belonging to the generation P3 just deleted, and the three assertions below all
# read the same pinned object (LSN-024).
pod="$(p3_pod_of_deploy "$K" "$NSX" developer-team-team-x-gateway 120)"
# Existence, not Ready: this reads spec fields only, and a Pending pod already has all of them. Kept
# deliberately weak. It was written weak because the pod stayed Pending on the old single-node host,
# and it stays weak because Ready is not what P3-K2 claims — the claim is what the operator RENDERED
# (serviceAccountName, image, tier label), which is decided at admission. Requiring Ready here would
# couple three renderer assertions to image pulls and scheduling, and fail them for reasons that have
# nothing to do with the renderer.
[ -n "$pod" ] \
  && pass "controller re-rendered the dev-team pod after the forced recreate (fresh admission, current renderer): $pod" \
  || bad "no pod owned by the current dev-team Deployment within 120s of the forced recreate — the assertions below would read nothing (HALT cond 4)"
sa="$($K -n "$NSX" get pod "$pod" -o jsonpath='{.spec.serviceAccountName}' 2>/dev/null)"
img="$($K -n "$NSX" get pod "$pod" -o jsonpath='{.spec.containers[0].image}' 2>/dev/null)"
tier="$($K -n "$NSX" get pod "$pod" -o jsonpath='{.metadata.labels.kube-agents/tier}' 2>/dev/null)"
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
if bash dev/tests/negative-attenuation.sh "$CTX"; then pass "VAP attenuation suite green"; else bad "VAP attenuation suite FAILED (HALT cond 1)"; fi

# --- P3-K6: namespace isolation shape (structural; enforcement is egress-enforcement-l2.sh) ----
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
echo "  NOTE: every check above judges YAML. ENFORCEMENT is dev/verify/egress-enforcement-l2.sh."

# --- P3-K7: cascade render -> VAP dry-run (rendered admit; write-verb tamper deny) -------------
echo; echo "== P3-K7: cascade render -> VAP dry-run =="
if command -v python3 >/dev/null 2>&1; then
  TMP="$(mktemp -d)"
  # No --github-cidrs: P8-T4 removed it (GitHub's blocks are fixed in the egress template, rule 4).
  # This line kept passing it for a day, argparse exited 2, the bundle was never written, and the
  # only symptom was "no identity file" below. Hence the rc capture — a renderer that fails must say
  # WHY, not leave the next reader to guess from an absent file. Mechanized in
  # dev/tests/cli-contract.py, which now fails L0 if any caller passes a flag no parser has.
  render_err="$(agents/cluster-admin/skills/provision-developer-team/scripts/render_developer_team.py \
      --cluster cluster-a --namespace team-x --project-id demo-proj --location us-central1 \
      --team-lead-chat-id users/1 --hub-inference-cidr 10.8.0.16/32 --hub-minty-cidr 10.8.0.32/32 \
      --mcp-cidrs 10.8.0.64/32 --repo-root "$TMP" 2>&1 >/dev/null)"; render_rc=$?
  [ "$render_rc" -eq 0 ] || bad "renderer exited $render_rc: $render_err"
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
if [ "$fail" -eq 0 ]; then echo " Phase 3 verification: ALL CHECKS PASSED"; else echo " Phase 3 verification: FAILURES ABOVE (see HALT conditions)"; fi
echo "===================================================================="
exit "$fail"
