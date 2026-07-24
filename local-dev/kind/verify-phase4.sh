#!/usr/bin/env bash
# Phase 4 (Coordination & knowledge) — consolidated verification harness.
#
# Re-runnable gate for 07 §2 Phase 4 Accept (a–e) + the touched suites (04 §9, 06 §10, 03 §11) and the
# load-bearing regressions. Phase 4 is push-first proactivity + indirect (GitOps/OKF) coordination, so
# MOST of its acceptance is HERMETIC by design (go golden + dependency-free python + the OKF validator) —
# the live Kind cluster is used only for the REGRESSION subset that the existing Phase-3 stack can prove
# without the Phase-4 image (03 §11 attenuation + read-only per-tier SAR under a trigger).
#
#   P4-K1 (a)  A K8s watch fires a reaction, scoped to the tier, WITHOUT a heartbeat poll.
#              HERMETIC: go test TestValidateScopeNamespace (fail-closed — a namespace-scoped tier with
#              no --scope-namespace EXITS non-zero instead of crash-looping a cluster-wide watch) +
#              TestWatcherArgsPerTierScoping (controller renders --owner=<EffectiveTier>, not hardcoded
#              platform, and --scope-namespace only for developer-team). Seam that receives the wake:
#              test_inject_auth (S1 — /sessions + /inject reject no/invalid bearer & owner mismatch) +
#              test_inject_render (S2 — {kind:alert|github|escalation} render, unknown kind → 400).
#   P4-K2 (b)  An escalation written by a lower tier is picked up by its parent — never a direct call.
#              HERMETIC: test_raise_escalation (writes knowledge/escalation/<slug>.md via submit-suggestion
#              --dry-run; parent re-derives its own scope, ignores `to:`) + read_knowledge retrieves it.
#              STRUCTURAL (invariant 3): the child egress NetworkPolicy carries NO parent-tier destination
#              (no agent→agent path); cross-tier flow is GitOps remote + loopback only.
#   P4-K3 (c)  An agent retrieves a runbook via the OKF read-only path (can never become a write path).
#              HERMETIC: test_read_knowledge (sparse knowledge/-ONLY checkout — clusters/ never materializes,
#              read script hard-refuses push/commit) + okf-validate PASS on the seeded tree.
#   P4-K4 (d)  Per-tier heartbeats run SCOPED audits; anything to change goes through propose→review.
#              HERMETIC: test_heartbeat_sops (cluster-admin=one cluster, dev-team=one namespace w/ over-reach
#              guard; NO_REPLY when healthy; change → submit-suggestion, never a direct mutation; backstop).
#   P4-K5 (e)  Inject drift → the Platform Agent opens a corrective PR unprompted, never a direct fix.
#              HERMETIC: test_detect_drift (desired-authoritative, server-default-tolerant read-only diff;
#              drifted live object still present — detect-and-propose, never fix) + test_submit_suggestion
#              (--dry-run halts after the local branch+commit, before git push / gh pr create).
#   REGRESS    LIVE on the deployed Phase-3 Kind stack: negative-attenuation.sh (03 §11 — write / impersonate
#              / wrong-scope DENIED) + read-only per-tier SAR still holds (invariant 1 under a push trigger);
#              08 §7 controller-mints-no-RBAC (go golden — controller ClusterRole has no rbac apiGroup).
#
# NOT covered here — deferred, NOT faked (same discipline as verify-phase2/3):
#   - LIVE Event→session spawn and the cloud transport legs (alert Pub/Sub delivery, GitHub webhook HMAC)
#     need the Phase-4 watcher/eventingress image REBUILT + `kind load` + `rollout restart` (the stale-image
#     caveat: a same-tag image reads green while running Phase-3 code) or scratch-GKE. D1 is Kind-provable
#     only at the IN-POD terminus (synthetic POST) which requires that rebuilt image; the transport is
#     scratch-GKE-deferred. This gate proves the terminus/render/scoping logic hermetically instead.
#   - 05 §8 chaos (failure-isolation) — Phase 6, NOT YET BUILT → N-A this phase (not a silent skip).
#   - Calico egress ENFORCEMENT + V-G scratch-GKE cloud identity — as documented in verify-phase3.sh.
#
# DESTRUCTIVE-TEST GUARD: the live regression only runs against a Kind context. The hermetic suite runs
# anywhere (it never touches a cluster), so this gate is CI-runnable even with no cluster reachable.
# Usage: local-dev/kind/verify-phase4.sh [kube-context]
set -uo pipefail  # -e omitted: exit codes are inspected manually.

CTX="${1:-kind-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
K="kubectl --context $CTX"
NSX=team-x
TEAMX=examples/gitops-repo/clusters/cluster-a/namespaces/team-x
DEVEG="$TEAMX/30-netpol-developer-team-egress.yaml"
CADMEG="examples/gitops-repo/clusters/cluster-a/agents/netpol-cluster-admin-egress.yaml"

case "$CTX" in
  kind-*) : ;;
  *) echo "REFUSING: context '$CTX' is not a Kind cluster (destructive-test guard)." >&2; exit 2 ;;
esac

fail=0
pass() { echo "PASS: $1"; }
bad()  { echo "FAIL: $1"; fail=1; }
note() { echo "  NOTE: $1"; }
cd "$REPO_ROOT"

# Run a python module dependency-free; PASS on rc 0, FAIL otherwise (show tail on failure).
pytest_ok() { # <label> <path> [args...]
  local label="$1"; shift; local path="$1"; shift
  if out="$(python3 "$path" "$@" 2>&1)"; then pass "$label"; else bad "$label"; echo "$out" | tail -12; fi
}

echo "===================================================================="
echo " Phase 4 verification (Coordination & knowledge) — context: $CTX"
echo "===================================================================="

# ============================ HERMETIC ACCEPTANCE (a–e) ============================
# These prove the Phase-4 logic without a cluster or a rebuilt image; they are the acceptance signal.

# --- P4-K1 (a): scoped watch fires a reaction, tier-scoped, without a heartbeat -----------------
echo; echo "== P4-K1 (a): per-tier scoped watcher + fail-closed validate + hardened wake seam =="
( cd k8s-operator
  go test ./cmd/k8s-event-watcher/ -run 'TestValidateScopeNamespace' >/tmp/p4-validate.log 2>&1 ) \
  && pass "watcher validate() fail-closed: ns-scoped tier w/o --scope-namespace REJECTED; cluster tiers OK" \
  || { bad "watcher fail-closed validate() go-test FAILED (Acc a / invariant 4)"; tail -15 /tmp/p4-validate.log; }
( cd k8s-operator
  go test ./internal/controller/ -run 'TestWatcherArgsPerTierScoping' >/tmp/p4-args.log 2>&1 ) \
  && pass "controller renders --owner=<EffectiveTier> + --scope-namespace only for developer-team" \
  || { bad "controller per-tier watcher-args go-test FAILED (Acc a / invariant 4)"; tail -15 /tmp/p4-args.log; }
# The wake seam that a fired watch delivers into (S1 auth + S2 kind branch), dependency-free copies:
seam_auth=0; seam_render=0
for tier in platform cluster-admin developer-team; do
  python3 "agents/$tier/scripts/test_inject_auth.py"   >/tmp/p4-auth.log   2>&1 || seam_auth=1
  python3 "agents/$tier/scripts/test_inject_render.py" >/tmp/p4-render.log 2>&1 || seam_render=1
done
[ "$seam_auth" -eq 0 ]   && pass "S1 seam auth enforced across all 3 tiers (bearer/owner; no/invalid → 401/403)" || { bad "S1 seam-auth test FAILED (invariant 5 wake-vector gap)"; tail -12 /tmp/p4-auth.log; }
[ "$seam_render" -eq 0 ] && pass "S2 inject kind-discriminator across all 3 tiers (alert/github/escalation; unknown → 400)" || { bad "S2 inject-render test FAILED"; tail -12 /tmp/p4-render.log; }
note "LIVE Event→session spawn needs the Phase-4 watcher image (rebuild + kind load + rollout) — deferred; logic proven above."

# --- P4-K2 (b): escalation round-trip is indirect (GitOps/OKF only, invariant 3) ---------------
echo; echo "== P4-K2 (b): escalation written by a child, picked up by parent — never a direct call =="
pytest_ok "raise-escalation writes escalation/<slug>.md via submit-suggestion --dry-run; parent re-derives own scope" \
          "local-dev/test_raise_escalation.py"
# Structural invariant 3: the child egress allowlist has NO parent-tier destination (no agent→agent path).
# Strip full-line comments (they legitimately name other tiers in prose), then look for a tier SELECTOR.
if grep -vE '^[[:space:]]*#' "$DEVEG" | grep -qE 'kube-agents/tier:[[:space:]]*(cluster-admin|platform)'; then
  bad "developer-team egress netpol names a parent tier as a destination (agent→agent path — invariant 3)"
else
  pass "developer-team egress netpol has NO parent-tier (cluster-admin/platform) destination (invariant 3)"
fi
if grep -vE '^[[:space:]]*#' "$CADMEG" | grep -qE 'kube-agents/tier:[[:space:]]*platform'; then
  bad "cluster-admin egress netpol names platform as a destination (agent→agent path — invariant 3)"
else
  pass "cluster-admin egress netpol has NO platform destination (invariant 3); cross-tier flow is GitOps+loopback only"
fi

# --- P4-K3 (c): retrieve a runbook via the read-only OKF path -----------------------------------
echo; echo "== P4-K3 (c): OKF read — sparse knowledge/-only checkout returns a runbook, can't push =="
pytest_ok "read-knowledge: sparse knowledge/-only tree (no clusters/), retrieves runbook by type/link, refuses push" \
          "local-dev/test_read_knowledge.py"
pytest_ok "okf-validate PASS on the seeded knowledge tree (every entry typed, links resolve)" \
          "local-dev/okf-validate.py" "examples/gitops-repo/knowledge"
# Negative control — the validator must still FAIL a frontmatter-less entry (proves it isn't a no-op).
TMP_BAD="$(mktemp -d)"; printf '# no frontmatter\n' > "$TMP_BAD/broken.md"
if python3 local-dev/okf-validate.py "$TMP_BAD" >/dev/null 2>&1; then bad "okf-validate PASSED a frontmatter-less entry (validator is a no-op)"; else pass "okf-validate negative control: frontmatter-less entry REJECTED"; fi
rm -rf "$TMP_BAD"

# --- P4-K4 (d): per-tier heartbeats run scoped audits, change → PR ------------------------------
echo; echo "== P4-K4 (d): per-tier heartbeat SOPs — scoped, backstop, no direct mutation =="
pytest_ok "heartbeat SOPs: cluster-admin=cluster / dev-team=namespace-only (over-reach guard); NO_REPLY; change→PR; backstop" \
          "local-dev/test_heartbeat_sops.py"

# --- P4-K5 (e): inject drift → corrective PR artifact, drifted object still present -------------
echo; echo "== P4-K5 (e): drift detection — read-only diff → corrective-PR artifact, never a direct fix =="
pytest_ok "detect-drift: desired-authoritative server-default-tolerant read-only diff; drifted object still present" \
          "local-dev/test_detect_drift.py"
pytest_ok "submit-suggestion --dry-run halts after local branch+commit — no git push, no gh pr create" \
          "local-dev/test_submit_suggestion.py"

# ============================ LIVE REGRESSION (Kind, load-bearing) ============================
# Runs against the already-deployed Phase-3 stack (no Phase-4 image needed). Skips (flagged, non-fatal to
# the hermetic gate) if the cluster is unreachable so CI can run the hermetic suite standalone.
echo; echo "== REGRESSION (03 §11 + read-only SAR under trigger + 08 §7) — live on $CTX =="
if $K version >/dev/null 2>&1; then
  # 03 §11 adversarial attenuation (write / impersonate / wrong-scope DENIED) — the load-bearing negatives.
  if bash local-dev/tests/negative-attenuation.sh "$CTX" >/tmp/p4-neg.log 2>&1; then
    pass "03 §11 negative-attenuation suite green (not regressed by push-first work)"
  else
    bad "03 §11 negative-attenuation suite FAILED (HALT — read-only ceiling regressed)"; tail -25 /tmp/p4-neg.log
  fi
  # Invariant 1 under a trigger: the dev-team SA the watch identity uses is still read-only + ns-scoped.
  SA="system:serviceaccount:$NSX:developer-team-agent"
  if $K get sa developer-team-agent -n "$NSX" >/dev/null 2>&1; then
    r_get="$($K auth can-i get pods --as=$SA -n $NSX 2>/dev/null)"
    r_watch="$($K auth can-i watch events --as=$SA -n $NSX 2>/dev/null)"
    r_write="$($K auth can-i create pods --as=$SA -n $NSX 2>/dev/null)"
    r_xns="$($K auth can-i get pods --as=$SA -n kube-system 2>/dev/null)"
    r_cluster="$($K auth can-i get nodes --as=$SA 2>/dev/null)"
    if [ "$r_get" = yes ] && [ "$r_watch" = yes ] && [ "$r_write" = no ] && [ "$r_xns" = no ] && [ "$r_cluster" = no ]; then
      pass "dev-team watch identity still read-only + namespace-scoped (get/watch=yes; create/x-ns/cluster=no)"
    else
      bad "dev-team SAR regressed (get=$r_get watch=$r_watch create=$r_write x-ns=$r_xns cluster=$r_cluster — HALT invariant 1)"
    fi
  else
    note "developer-team-agent SA not present in $NSX — run verify-phase3.sh preconditions first (SAR regression not checked)."
  fi
else
  note "context '$CTX' unreachable — LIVE regression SKIPPED (hermetic acceptance above still authoritative)."
  note "Re-run against a deployed Kind stack (INSTALL 'Phase 2 — Kind inner loop') to exercise 03 §11 + SAR."
fi
# 08 §7 — controller mints no RBAC/SA (go golden; deterministic, no cluster).
( cd k8s-operator && go test ./internal/controller/ >/tmp/p4-ctrl.log 2>&1 ) \
  && pass "08 §7 controller package golden green (build-manifests + reconcile: mints no RBAC/SA — regress)" \
  || { bad "08 §7 controller golden FAILED (regress)"; tail -15 /tmp/p4-ctrl.log; }

echo
echo "  DEFERRED (not faked): live Event→session spawn + cloud transport (Pub/Sub/GitHub HMAC) → Phase-4"
echo "  image rebuild / scratch-GKE.  05 §8 chaos → Phase 6 (not yet built, N-A).  Calico egress"
echo "  ENFORCEMENT + V-G cloud identity → separate Calico / scratch-GKE runs (see verify-phase3.sh)."
echo
echo "===================================================================="
if [ "$fail" -eq 0 ]; then echo " Phase 4 verification: ALL CHECKS PASSED"; else echo " Phase 4 verification: FAILURES ABOVE (see HALT conditions)"; fi
echo "===================================================================="
exit "$fail"
