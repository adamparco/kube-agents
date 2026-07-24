#!/usr/bin/env bash
# Phase 6 (Failure-isolation & resilience) consolidated gate + regression.
#
# Phase 6 adds NO new persona and NO new write path — it is the validation phase that graduates the
# 05 §8 "failure isolation (chaos)" suite from deferred (N-A through Phases 4-5) to a live, load-bearing
# gate. This script proves the phase Accept criteria and re-runs every prior gate so the chaos work did
# not regress the rest of the design.
#
#   A. NET-NEW load-bearing — 05 §8 failure isolation (chaos):  local-dev/kind/chaos-suite.sh
#        C1 controller down  -> running pods continue + NO new reconciles + resume on restart  (Accept b)
#        C2 controller up    -> it relaunches agent pods (Deployment + pod)                     (Accept c)
#        C3 cluster-admin down -> its dev-team agents keep running (NO cascade) + relaunch      (Accept b)
#        C4 hub down         -> spoke keeps last-applied state, decoupled, no bundled engine    (Accept a)
#        + 05 §8 bullet 4 unopinionated actuation (no Config Sync / Connector / Argo / Flux required)
#      A chaos failure is a HALT condition (05 §8 is load-bearing; a cascade breaks 04 §6).
#
#   B. Full regression — the Phase 5 gate, which itself transitively re-runs:
#        verify-phase{2,3,4}.sh   (Accept criteria of Phases 2-4 — includes 05 §8 bullet 2 placement A1
#                                  in phase 3, and 08 §7 controller-mints-no-RBAC in phase 4)
#        03 §11 negative-attenuation.sh  (the read-only ceiling: write/impersonate/wrong-scope DENIED)
#        go goldens + go test ./...      (05 §8 bullet 1 agent/controller pod spec; router/watcher/controller)
#      Any regression here is a HALT condition, not a "note and move on".
#
# So the four 05 §8 bullets are ALL live after this gate: (1) pod spec [go goldens, via B], (2) placement
# [phase 3, via B], (3) failure isolation [chaos-suite, A], (4) unopinionated actuation [chaos-suite, A].
#
# DEFERRED (not faked, 04 §6 honest scoping): the LITERAL spoke agent-reasoning-pause under real hub loss
# (real inference/Minty over private networking) needs two clusters -> scratch-GKE. C4 proves the
# load-bearing half (cluster state + workloads survive hub loss) on Kind and never asserts the rest green.
#
# Destructive-test guard: Kind context only; chaos-suite.sh is reversible + single-object + self-cleaning.
# Usage: local-dev/kind/verify-phase6.sh [kube-context]
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
cd "$REPO_ROOT"

echo "===================================================================="
echo " Phase 6 verification (failure-isolation & resilience) — context: $CTX"
echo "===================================================================="

reachable=1
kubectl --context "$CTX" version >/dev/null 2>&1 || reachable=0

# ---- A. NET-NEW load-bearing: 05 §8 failure isolation (chaos) ---------------------------------------
echo; echo "== A. 05 §8 failure isolation (chaos) — chaos-suite.sh (NET-NEW load-bearing) =="
if [ "$reachable" -eq 1 ]; then
  if bash local-dev/kind/chaos-suite.sh "$CTX" >/tmp/p6-chaos.log 2>&1; then
    pass "05 §8 chaos suite green: C1-C4 + unopinionated actuation (no cascade — 04 §6; Accept a-c)"
    grep -E '^(PASS|  NOTE|  DEFERRED)' /tmp/p6-chaos.log | sed 's/^/    /'
  else
    bad "05 §8 chaos suite FAILED — cascade / no-recovery / reconcile regression (HALT: 05 §8 is load-bearing)"
    tail -40 /tmp/p6-chaos.log
  fi
else
  bad "context '$CTX' unreachable — the 05 §8 chaos suite is load-bearing and cannot be skipped for the gate"
fi

# ---- B. Full regression: the Phase 5 gate (transitively phase{2,3,4} + 03 §11 negatives + go test) --
echo; echo "== B. Full regression — verify-phase5.sh (=> phase{2,3,4} Accept + 03 §11 + goldens + go test) =="
if bash local-dev/kind/verify-phase5.sh "$CTX" >/tmp/p6-regress.log 2>&1; then
  pass "Phase 5 gate green -> Phases 2-5 not regressed (05 §8 bullets 1-2 goldens/placement + 03 §11 + go test)"
  # Surface the transitive sub-gate summary lines so the regression is legible in this gate's output.
  grep -E 'verify-phase[234]\.sh (green|FAILED)|negative-attenuation|go test \./\.\.\.|readOnlyRootFilesystem goldens|ALL CHECKS PASSED' \
    /tmp/p6-regress.log | sed 's/^/    /'
else
  bad "verify-phase5.sh FAILED — a prior phase regressed (HALT). Full log: /tmp/p6-regress.log"
  tail -40 /tmp/p6-regress.log
fi

echo
echo "  DEFERRED (not faked, 04 §6): the LITERAL spoke agent-reasoning-pause under real hub loss (real"
echo "  inference/Minty over private networking) -> two-cluster / scratch-GKE. C4 proves the load-bearing"
echo "  half (cluster state + workloads survive hub loss) on Kind."
echo
echo "===================================================================="
if [ "$fail" -eq 0 ]; then echo " Phase 6 verification: ALL CHECKS PASSED"; else echo " Phase 6 verification: FAILURES ABOVE (see HALT conditions)"; fi
echo "===================================================================="
exit "$fail"
