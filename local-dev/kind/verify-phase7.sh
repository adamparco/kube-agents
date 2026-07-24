#!/usr/bin/env bash
# Phase 7 (Cloud-agnostic seams) — consolidated gate + full regression.
#
# Phase 7 is the FINAL roadmap phase (07 §2 "Phase 7 — Cloud-agnostic seams (later)"). It adds NO new
# persona, NO new agent capability, and NO new write path — it turns three already-unopinionated seams
# into real, tested artifacts and proves the cloud-neutral core on a vanilla, non-GKE target while
# HONESTLY DEFERRING the pieces that need a real second cloud. This script proves the phase Accept
# criteria (a–c) and re-runs every prior gate so the cloud-agnostic refactor did not weaken the
# read-only ceiling, isolation, or resilience.
#
#   A. NET-NEW seam artifacts (hermetic — structural + semantic; no cluster needed) ---------------
#        P7-T1  iac-parity.py        — real Terraform HCL exemplar (cluster-b, iac.format: terraform)
#                                      structurally valid AND semantically equivalent to the KCC
#                                      exemplar (cluster-a); apply.yml dispatches .tf→terraform /
#                                      .yaml→kubectl; bad-HCL negative control fails.       (Accept a)
#        P7-T2  circleci-parity.py   — a 2nd reference pipeline (.circleci/config.yml) actuates the
#                                      same repo with the same KCC/HCL dispatch + per-target least-priv
#                                      creds as apply.yml, adding NO agent-held write credential
#                                      (invariant 2); malformed-config negative control fails. (Accept b)
#        P7-T3  observability-seam.py + otel-endpoint.sh — the OTLP export endpoint and the obs-skill
#                                      backend base URLs resolve from the environment, with GCP as the
#                                      zero-config default (unset ⇒ byte-for-byte no regression) and a
#                                      documented non-GCP path.                              (Accept c)
#
#   B. P7-T4 — VANILLA (Kind, non-GKE) CORE-CONCEPT ACCEPTANCE (Accept c) -------------------------
#      Kind is a vanilla, non-GKE Kubernetes distribution (kindnet CNI, no GKE/GCP API). On it the
#      Phase-1–3 CLOUD-NEUTRAL core concepts hold with NO GKE dependency:
#        B1  deterministic ChatOps routing — go-test TestGateway_ThreadAffinity: a bound thread's bare
#            follow-up sticks with inference_calls==0 (the router core is deterministic, no model call).
#        B2  no-GKE-dependency assertion (static) — the core RBAC / webhook / VAP / router MECHANISM
#            path references no *.googleapis.com host and no GKE-only API group. (The cloud-COUPLED
#            identity — the iam.gke.io/gcp-service-account WI annotation on the read-only identities —
#            is the one GKE-specific seam a 2nd cloud swaps for IRSA / AAD Workload Identity; it is
#            DEFERRED-NOT-FAKED per D1, never asserted clean here.)
#        B3  live core concepts on this vanilla target — verify-phase2.sh + verify-phase3.sh:
#            read-only per-tier SAR (get/list/watch only; no writes; no priv-esc), GitOps-PR-only
#            mutation (VAP attenuation ceiling), namespace isolation, the (tier,scope) cardinality
#            webhook + tier immutability — all pure-Kubernetes, no GKE API.
#
#   C. P7-T5 — FULL REGRESSION (must stay green — HALT on failure) --------------------------------
#        verify-phase6.sh → 05 §8 chaos C1–C4 (no cascade) + verify-phase{2,3,4,5}.sh (prior Accept,
#        incl. Calico egress + hardening VAP) + 03 §11 negative-attenuation (read-only ceiling) +
#        readOnlyRootFilesystem goldens + `go test ./...` (08 §7 controller mints no RBAC). The seam
#        changes are additive (default-preserving), so nothing prior should move.
#
# DEFERRED, NOT FAKED (recorded, never asserted green):
#   D1  a real SECOND CLOUD — EKS/AKS cluster + cloud identity (IRSA / AAD WI) + a live second-cloud
#       apply. Kind proves the cloud-NEUTRAL core; the cloud-COUPLED identity binding is deferred.
#   D2  CLI-level IaC validation — `terraform validate`/`fmt`/`apply` (no terraform binary; structural
#       + semantic parity proven hermetically instead — same pattern as Calico for kindnet's NetworkPolicy).
#   D2b `circleci config validate` (no circleci binary — structural + dispatch parity proven hermetically).
#   D3  a live NON-GCP observability backend queried end-to-end (query translation is backend-specific).
#   (Also still carried: the scratch-GKE V-G cloud checks, and the 08 §5 deferred hardening.)
#
# DESTRUCTIVE-TEST GUARD: only runs against a Kind context (verify-phase6 → chaos-suite scales/deletes
# reversible, single-object, self-cleaning fixtures; guarded the same way in every prior phase).
# Usage: local-dev/kind/verify-phase7.sh [kube-context]
set -uo pipefail

CTX="${1:-kind-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

case "$CTX" in
  kind-*) : ;;
  *) echo "REFUSING: context '$CTX' is not a Kind cluster (destructive-test guard)." >&2; exit 2 ;;
esac

fail=0
pass() { echo "PASS: $1"; }
bad()  { echo "FAIL: $1"; fail=1; }
note() { echo "  NOTE: $1"; }
defer(){ echo "  DEFERRED (not faked): $1"; }
cd "$REPO_ROOT"

echo "===================================================================="
echo " Phase 7 verification (cloud-agnostic seams) — context: $CTX"
echo "===================================================================="

reachable=1
kubectl --context "$CTX" version >/dev/null 2>&1 || reachable=0

# ==== A. NET-NEW seam artifacts (T1–T3) — hermetic (structural + semantic; no cluster) ===============
echo; echo "== A. Cloud-agnostic seam artifacts (hermetic) — T1 IaC parity, T2 pipeline parity, T3 obs seam =="

run_hermetic() { # <label> <cmd...>
  local label="$1"; shift
  if "$@" >"/tmp/p7-${label}.log" 2>&1; then
    pass "$label green"
    grep -E '^  PASS' "/tmp/p7-${label}.log" | sed 's/^/    /'
  else
    bad "$label FAILED — see /tmp/p7-${label}.log"
    tail -20 "/tmp/p7-${label}.log"
  fi
}

if command -v python3 >/dev/null 2>&1; then
  run_hermetic "iac-parity"        python3 local-dev/tests/iac-parity.py "$REPO_ROOT"          # T1 (Accept a)
  run_hermetic "circleci-parity"   python3 local-dev/tests/circleci-parity.py "$REPO_ROOT"     # T2 (Accept b)
  run_hermetic "observability-seam" python3 local-dev/tests/observability-seam.py "$REPO_ROOT" # T3 (Accept c)
else
  bad "python3 not found — the T1/T2/T3 hermetic parity validators cannot run (they are load-bearing)"
fi
run_hermetic "otel-endpoint" bash local-dev/tests/otel-endpoint.sh "$REPO_ROOT"                # T3 (Accept c)

# ==== B. P7-T4 — vanilla (Kind, non-GKE) core-concept acceptance (Accept c) ==========================
echo; echo "== B. P7-T4 vanilla core-concept acceptance — the cloud-neutral core holds on a non-GKE target =="

# Confirm the target really is a vanilla, non-GKE distribution (so 'passes on a 2nd target' is honest).
# Read the SERVER-side node kubeletVersion (authoritative) — NOT `kubectl version`'s first gitVersion,
# which is the client build (this host's kubectl is gcloud's `-gke` kubectl and would mis-flag Kind).
if [ "$reachable" -eq 1 ]; then
  gv="$(kubectl --context "$CTX" get nodes -o jsonpath='{.items[0].status.nodeInfo.kubeletVersion}' 2>/dev/null)"
  cni="$(kubectl --context "$CTX" -n kube-system get pods -o name 2>/dev/null | grep -m1 -iE 'kindnet|calico' || true)"
  case "$gv" in *gke*|*-gke.*) bad "target node kubeletVersion '$gv' is a GKE build — not a vanilla 2nd target";; *) pass "target is vanilla upstream Kubernetes (node kubeletVersion $gv, no GKE suffix)";; esac
  [ -n "$cni" ] && note "CNI is ${cni##*/} (a generic CNI, not a GKE-managed dataplane)"
else
  note "context '$CTX' unreachable — B1/B2 (hermetic/static) still run; B3 (live) will be skipped"
fi

# --- B1: deterministic ChatOps routing (inference_calls==0) — hermetic go-test ------------------------
echo; echo "-- B1: deterministic ChatOps routing (inference_calls==0) --"
if command -v go >/dev/null 2>&1; then
  if ( cd k8s-operator && go test ./internal/router/ -run 'TestGateway_ThreadAffinity' -count=1 -v ) \
       >/tmp/p7-router-determinism.log 2>&1; then
    if grep -q -- '--- PASS: TestGateway_ThreadAffinity/deterministic_turn_binds' /tmp/p7-router-determinism.log; then
      pass "deterministic routing: a bound thread's bare follow-up sticks with inference_calls==0 (no model call)"
    else
      pass "TestGateway_ThreadAffinity green (deterministic sticky routing)"
      note "expected subtest name not matched in -v output; parent test still passed"
    fi
  else
    bad "TestGateway_ThreadAffinity FAILED — router core is not deterministic (see /tmp/p7-router-determinism.log)"
    tail -20 /tmp/p7-router-determinism.log
  fi
else
  bad "go not found — the deterministic-routing (inference_calls==0) test cannot run"
fi

# --- B2: no-GKE-dependency assertion (static) over the core MECHANISM path ---------------------------
echo; echo "-- B2: no-GKE-dependency assertion (core RBAC / webhook / VAP / router path) --"
# Forbidden in the cloud-NEUTRAL core mechanism: any GCP API host or GKE-only API group. The KCC
# provisioning YAML (container.cnrm.cloud.google.com), the agent's MCP grounding tool endpoint, and the
# WI identity annotation are cloud-COUPLED SEAMS by design — they are NOT part of this mechanism path and
# are covered by B2's deferral note below, not scanned here.
CORE_PATHS=(
  "examples/gitops-repo/policy/vap-agent-readonly.yaml"
  "examples/gitops-repo/policy/vap-agent-pod-hardening.yaml"
  "k8s-operator/internal/webhook/agent_webhook.go"
  "k8s-operator/config/rbac/role.yaml"
)
FORBIDDEN='googleapis\.com|cnrm\.cloud\.google\.com|container\.google|\.gke\.io|gkehub|iam\.gke'
hits=0
for f in "${CORE_PATHS[@]}"; do
  if [ -f "$REPO_ROOT/$f" ] && grep -EnH "$FORBIDDEN" "$REPO_ROOT/$f" 2>/dev/null; then hits=1; fi
done
# The router mechanism (non-test Go) must also be clean.
if grep -REnI "$FORBIDDEN" "$REPO_ROOT/k8s-operator/internal/router" --include='*.go' 2>/dev/null | grep -v '_test\.go'; then hits=1; fi
if [ "$hits" -eq 0 ]; then
  pass "core RBAC/webhook/VAP/router mechanism references NO *.googleapis.com host and NO GKE-only API group"
else
  bad "a core-mechanism file references a GKE/GCP API — the read-only ceiling/isolation/routing is GKE-coupled (HALT)"
fi
defer "cloud-COUPLED identity — the iam.gke.io/gcp-service-account WI annotation on the read-only"
echo "           identities is the one GKE-specific binding a 2nd cloud swaps for IRSA / AAD WI (D1)."

# --- B3: live core concepts on this vanilla target — verify-phase{2,3}.sh ----------------------------
echo; echo "-- B3: live core concepts on vanilla Kind — read-only SAR, isolation, cardinality webhook, VAP --"
if [ "$reachable" -eq 1 ]; then
  if bash local-dev/kind/verify-phase2.sh "$CTX" >/tmp/p7-phase2.log 2>&1; then
    pass "verify-phase2.sh green on vanilla Kind (cluster-admin read-only SAR + cardinality webhook + VAP attenuation)"
  else
    bad "verify-phase2.sh FAILED on vanilla Kind — a core concept does not hold on the 2nd target (HALT)"
    tail -25 /tmp/p7-phase2.log
  fi
  if bash local-dev/kind/verify-phase3.sh "$CTX" >/tmp/p7-phase3.log 2>&1; then
    pass "verify-phase3.sh green on vanilla Kind (dev-team namespace isolation + read-only SAR + placement)"
  else
    bad "verify-phase3.sh FAILED on vanilla Kind — namespace isolation / SAR does not hold on the 2nd target (HALT)"
    tail -25 /tmp/p7-phase3.log
  fi
else
  note "SKIP B3 live core-concept checks — context '$CTX' unreachable (B1/B2 above are cluster-independent)"
fi

# ==== C. P7-T5 — full regression (HALT on any failure) ==============================================
echo; echo "== C. Full regression — verify-phase6.sh (=> chaos C1–C4 + verify-phase{2,3,4,5} + 03 §11 + goldens + go test) =="
if [ "$reachable" -eq 1 ]; then
  if bash local-dev/kind/verify-phase6.sh "$CTX" >/tmp/p7-regress.log 2>&1; then
    pass "verify-phase6.sh green — 05 §8 chaos + Phases 2–5 Accept + 03 §11 negatives + goldens + go test NOT regressed"
    grep -E 'chaos suite green|verify-phase[2345]\.sh green|negative-attenuation suite green|go test \./\.\.\. green|ALL CHECKS PASSED' \
      /tmp/p7-regress.log | sed 's/^/    /'
  else
    bad "verify-phase6.sh FAILED — a load-bearing suite (chaos / 03 §11) or a prior phase regressed (HALT). Log: /tmp/p7-regress.log"
    tail -40 /tmp/p7-regress.log
  fi
else
  bad "context '$CTX' unreachable — the full regression (chaos + prior gates) is load-bearing and cannot be skipped"
fi

# ==== D. Deferrals (printed, never asserted green) ==================================================
echo; echo "== D. Deferred-not-faked (recorded; never asserted green) =="
defer "a real SECOND CLOUD — EKS/AKS cluster + cloud identity (IRSA / AAD WI) + a live second-cloud apply (D1)."
defer "CLI-level IaC validation — terraform validate/fmt/apply (no terraform binary; parity proven hermetically) (D2)."
defer "circleci config validate (no circleci binary; structural + dispatch parity proven hermetically) (D2b)."
defer "a live NON-GCP observability backend queried end-to-end + query translation (backend-specific) (D3)."
defer "still carried: scratch-GKE V-G cloud checks; 08 §5 cross-object webhook / gVisor sandbox / per-request down-scoping."

echo
echo "===================================================================="
if [ "$fail" -eq 0 ]; then echo " Phase 7 verification: ALL CHECKS PASSED"; else echo " Phase 7 verification: FAILURES ABOVE (see HALT conditions)"; fi
echo "===================================================================="
exit "$fail"
