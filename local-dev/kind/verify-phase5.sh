#!/usr/bin/env bash
# Phase 5 (Security gate & hardening) — consolidated verification harness.
#
# Re-runnable gate for 07 §2 Phase 5 Accept (a–d) + the touched suites (05 §8 is Phase 6, so N-A here;
# 03 §11 is the load-bearing regression) and the prior-phase regressions. Phase 5 is the merge-time
# review gate, egress lockdown, pod hardening, and mutation attribution — so its acceptance splits into
# a HERMETIC core (stdlib python scorer + go goldens + router audit test, all cluster-free and the
# authoritative signal) and a LIVE core on the Kind cluster (VAP admission dry-run — GA on k8s >= 1.30 —
# plus egress ENFORCEMENT, which kindnet cannot do and is PROVEN separately on Calico).
#
#   P5-A (a)  A PR with an unmitigated high finding is BLOCKED; a matching non-expired waiver mitigates.
#             HERMETIC: score_findings.py — an unmitigated high exits 1 (BLOCK), a clean set exits 0
#             (PASS), a valid waiver for the finding's fingerprint exits 0 (mitigated), an EXPIRED waiver
#             still exits 1 (negative control — waivers can't silently rot into a bypass). Plus the
#             scorer + extractor unit suites (33 + 7 cases) so the decision logic itself is locked.
#   P5-B (b)  Egress outside the tier allowlist is DENIED.
#             STRUCTURAL: all THREE tier egress netpols are pure allowlists — policyTypes:[Egress] with a
#             tier podSelector (default-deny) and NO 0.0.0.0/0 escape (the cloud-metadata / off-allowlist
#             negative). LIVE: egress-enforcement.sh proves a same-shaped policy actually BLOCKS an
#             off-allowlist dest on an enforcing CNI; rc 3 (DEFERRED on kindnet) is non-fatal here because
#             enforcement is PROVEN on the Calico cluster (P5-T6); rc 1 (FAILED) is a halt.
#   P5-C (c)  Every agent pod runs under the hardened security context.
#             HERMETIC: go TestAgentsGolden (full render byte-lock) + TestClusterAdminRender_LoadBearing
#             (asserts readOnlyRootFilesystem:true on EVERY rendered container, so a golden regen can't
#             silently weaken it). STRUCTURAL: PSS `enforce: restricted` namespace label + both VAPs
#             present. LIVE: with the pod-hardening VAP installed, `apply --dry-run=server` REJECTS an
#             un-hardened agent-tier pod (error names readOnlyRootFilesystem — proves it's the VAP),
#             ADMITS a hardened one, and leaves a non-agent pod (no tier label) UNTOUCHED (scope proof).
#   P5-D (d)  Every proposed mutation is attributable.
#             HERMETIC: test_submit_suggestion.py (Requested-by:/Trace-Id: trailers — flag>env>autonomous
#             fallback, single-line, idempotent, reach the dry-run artifact) + go router audit test
#             (a delivered turn ties Sender to TraceID and carries it to dispatch).
#   REGRESS   LIVE: negative-attenuation.sh (03 §11 — write / impersonate / wrong-scope DENIED, the
#             load-bearing read-only ceiling) + prior-phase gates verify-phase{2,3,4}.sh. HERMETIC:
#             `go test ./...` across the operator (goldens, router, controller, watcher). 05 §8 chaos is
#             Phase 6 (not yet built) → N-A, flagged not skipped.
#
# NOT covered here — deferred, NOT faked (same discipline as verify-phase2/3/4):
#   - Egress ENFORCEMENT on Calico is a SEPARATE run (egress-enforcement.sh against the Calico cluster,
#     P5-T6 PROVEN); on the default kindnet dev cluster it DEFERs (rc 3), which this gate treats as
#     non-fatal and says so. The tier-netpol SHAPE is proven structurally here.
#   - The live review-gate CI workflow (review-gate.yml) runs in GitHub Actions; its scoring core is the
#     hermetic scorer proven above. LIVE end-to-end webhook delivery is scratch-GKE / CI, not Kind.
#   - 05 §8 chaos (failure-isolation) — Phase 6, NOT YET BUILT → N-A this phase (not a silent skip).
#
# DESTRUCTIVE-TEST GUARD: the live checks only run against a Kind context. The hermetic suite runs
# anywhere (it never touches a cluster), so this gate is CI-runnable even with no cluster reachable.
# Usage: local-dev/kind/verify-phase5.sh [kube-context]
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions).
#   P1 image-under-test:  none for anything this script claims itself. P5-A is a stdlib scorer, P5-B
#      and P5-C read tree YAML and go goldens, P5-D is a python suite plus a go test, and the live half
#      exercises a ValidatingAdmissionPolicy and the CNI — the API server and the dataplane decide
#      those, not a first-party image. The operator-image-dependent claims in a Phase-5 run all live in
#      the REGRESSION block, inside verify-phase{2,3}.sh, and each of those asserts P1 itself against
#      kubeagents-system/control-plane=controller-manager. Asserting it a third time here would put the
#      check somewhere other than next to the evidence it protects, which is how it became decorative
#      the first time. If a claim of this script's OWN ever starts depending on the operator image,
#      this waiver stops being true and the assertion belongs here.
#   P3 admission-recreate: the three P5-C admission probes, via `apply --dry-run=server`. A server-side
#      dry run runs the full admission chain and persists nothing, so each of the three — unhardened
#      agent pod rejected, hardened one admitted, non-agent pod untouched — is decided under the rules
#      in force right now and cannot be answered by an object that was admitted under older ones
#      (LSN-002). The VAP itself is re-applied immediately before them, and the probe then POLLS for up
#      to ~40s so binding-activation latency reads as latency rather than as an enforcement gap.
#   P6 runtime-authoritative: the API server for the live admission verdicts, and the CNI dataplane for
#      the egress result. The structural checks read tree YAML deliberately: the claim in P5-B and P5-C
#      is that every shipped tier netpol is a pure allowlist and every rendered container carries
#      readOnlyRootFilesystem, which is a property of the artifacts kube-agents ships rather than of one
#      cluster's current contents. No claim reads the image-baked config file that the operator shadows
#      with a rendered ConfigMap at runtime (LSN-003).
set -uo pipefail  # -e omitted: exit codes are inspected manually.

CTX="${1:-kind-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
K="kubectl --context $CTX"

SCORER=scripts/review-gate/score_findings.py
PLATEG=examples/gitops-repo/fleet/netpol-platform-egress.yaml
CADMEG=examples/gitops-repo/clusters/cluster-a/agents/netpol-cluster-admin-egress.yaml
DEVEG=examples/gitops-repo/clusters/cluster-a/namespaces/team-x/30-netpol-developer-team-egress.yaml
VAP_HARDEN=examples/gitops-repo/policy/vap-agent-pod-hardening.yaml
VAP_READONLY=examples/gitops-repo/policy/vap-agent-readonly.yaml
NS_PSS=examples/gitops-repo/clusters/cluster-a/namespaces/team-x/00-namespace.yaml

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
echo " Phase 5 verification (Security gate & hardening) — context: $CTX"
echo "===================================================================="

# ============================ HERMETIC ACCEPTANCE (a–d) ============================
# These prove the Phase-5 logic without a cluster; they are the authoritative acceptance signal.

# --- P5-A (a): merge gate BLOCKS an unmitigated high; a valid waiver mitigates -------------------
echo; echo "== P5-A (a): review gate — unmitigated high BLOCKS, clean PASSES, waiver mitigates, expiry re-blocks =="
FIND='[{"agent":"platform","file":"a.yaml","message":"privileged pod","severity":"high"}]'
FP="$(echo "$FIND" | python3 "$SCORER" --fingerprint 2>/dev/null | awk 'NR==1{print $1}')"
WDIR="$(mktemp -d)"
cat > "$WDIR/valid.yaml" <<EOF
waivers:
  - fingerprint: $FP
    justification: accepted risk (verify-phase5 fixture)
    approved_by: security-lead
    expires: "2099-12-31"
EOF
cat > "$WDIR/expired.yaml" <<EOF
waivers:
  - fingerprint: $FP
    justification: stale waiver (verify-phase5 negative control)
    approved_by: security-lead
    expires: "2020-01-01"
EOF
echo "$FIND" | python3 "$SCORER" --waivers /dev/null >/dev/null 2>&1; rc=$?
[ "$rc" -eq 1 ] && pass "unmitigated high finding BLOCKS merge (scorer rc=1)" \
  || bad "unmitigated high did NOT block (scorer rc=$rc, want 1 — Acc a)"
echo '[]' | python3 "$SCORER" --waivers /dev/null >/dev/null 2>&1; rc=$?
[ "$rc" -eq 0 ] && pass "clean finding set PASSES merge (scorer rc=0)" \
  || bad "clean set did NOT pass (scorer rc=$rc, want 0)"
if [ -n "$FP" ]; then
  echo "$FIND" | python3 "$SCORER" --waivers "$WDIR/valid.yaml" --today 2026-01-01 >/dev/null 2>&1; rc=$?
  [ "$rc" -eq 0 ] && pass "matching non-expired waiver MITIGATES the high (scorer rc=0)" \
    || bad "valid waiver did NOT mitigate (scorer rc=$rc, want 0 — Acc a)"
  echo "$FIND" | python3 "$SCORER" --waivers "$WDIR/expired.yaml" --today 2026-01-01 >/dev/null 2>&1; rc=$?
  [ "$rc" -eq 1 ] && pass "EXPIRED waiver still BLOCKS (negative control — no silent bypass)" \
    || bad "expired waiver bypassed the gate (scorer rc=$rc, want 1 — Acc a)"
else
  bad "could not compute finding fingerprint (--fingerprint returned empty — Acc a)"
fi
rm -rf "$WDIR"
# Lock the decision logic itself: the scorer + extractor unit suites.
pytest_ok "score_findings unit suite (severity gate, waiver match, expiry, aggregated + flat shapes)" \
          "scripts/review-gate/test_score_findings.py"
pytest_ok "extract_findings unit suite (detector output → normalized findings)" \
          "scripts/review-gate/test_extract_findings.py"

# --- P5-B (b): egress outside the allowlist is denied -------------------------------------------
echo; echo "== P5-B (b): tier egress netpols are pure allowlists (default-deny, no 0.0.0.0/0) =="
for pair in "platform:$PLATEG" "cluster-admin:$CADMEG" "developer-team:$DEVEG"; do
  tier="${pair%%:*}"; f="${pair#*:}"
  if [ ! -f "$f" ]; then bad "$tier egress netpol missing ($f)"; continue; fi
  ok=1
  grep -qE '^[[:space:]]*-[[:space:]]*Egress' "$f" || { bad "$tier egress netpol has no policyTypes:[Egress] (not default-deny)"; ok=0; }
  grep -qE "kube-agents/tier:[[:space:]]*$tier" "$f" || { bad "$tier egress netpol podSelector does not select its own tier"; ok=0; }
  # A 0.0.0.0/0 rule (outside a full-line comment) would be a blanket egress escape hatch.
  if grep -vE '^[[:space:]]*#' "$f" | grep -qE '0\.0\.0\.0/0'; then bad "$tier egress netpol contains a 0.0.0.0/0 escape (Acc b)"; ok=0; fi
  [ "$ok" -eq 1 ] && pass "$tier egress netpol: policyTypes:[Egress] + tier podSelector + no 0.0.0.0/0 (pure allowlist)"
done

# --- P5-C (c): every agent pod runs under the hardened security context -------------------------
echo; echo "== P5-C (c): agent pods hardened — readOnlyRootFilesystem goldens + PSS label + VAPs present =="
( cd k8s-operator && go test ./internal/testing/ -run 'TestAgentsGolden|TestClusterAdminRender_LoadBearing' >/tmp/p5-golden.log 2>&1 ) \
  && pass "go goldens green: rendered agent pods carry readOnlyRootFilesystem on EVERY container (H-A)" \
  || { bad "go golden/render test FAILED (Acc c / H-A — hardened context regressed)"; tail -20 /tmp/p5-golden.log; }
grep -qE 'pod-security.kubernetes.io/enforce:[[:space:]]*restricted' "$NS_PSS" \
  && pass "namespace carries PSS enforce: restricted (baseline hardened context — H-B)" \
  || bad "namespace missing PSS enforce: restricted label (Acc c / H-B)"
for vf in "$VAP_HARDEN" "$VAP_READONLY"; do
  if [ -f "$vf" ] && grep -q 'kind: ValidatingAdmissionPolicy' "$vf"; then
    pass "VAP present: $(basename "$vf")"
  else
    bad "VAP missing or malformed: $vf (Acc c)"
  fi
done

# --- P5-D (d): every proposed mutation is attributable ------------------------------------------
echo; echo "== P5-D (d): mutation attribution — PR trailers + audit ties Sender→trace_id =="
pytest_ok "submit-suggestion stamps Requested-by:/Trace-Id: trailers (flag>env>autonomous; single-line; idempotent; reach artifact)" \
          "local-dev/test_submit_suggestion.py"
( cd k8s-operator && go test ./internal/router/ -run 'TestGateway_AuditAttributionSurface' >/tmp/p5-audit.log 2>&1 ) \
  && pass "router audit: a delivered turn ties Sender to TraceID and carries it to dispatch (T-A)" \
  || { bad "router attribution audit test FAILED (Acc d / T-A)"; tail -20 /tmp/p5-audit.log; }

# ============================ LIVE ACCEPTANCE (Kind) ============================
# VAP admission is GA on k8s >= 1.30 (dev cluster is v1.31.x); egress enforcement needs Calico.
echo; echo "== LIVE (Kind $CTX): VAP admission dry-run (c) + egress enforcement (b) =="
if $K version >/dev/null 2>&1; then
  # (c) LIVE: install the pod-hardening VAP, then prove admission with server-side dry-run in a namespace
  #     WITHOUT PSS enforcement (default) so the ONLY thing that can reject a labeled-but-unhardened pod
  #     is THIS VAP — isolating the proof from PSS.
  if $K apply -f "$VAP_HARDEN" >/tmp/p5-vapapply.log 2>&1; then
    note "pod-hardening VAP applied to $CTX (Deny binding, scoped to kube-agents/tier pods)"
    IMG=registry.k8s.io/pause:3.9
    # Un-hardened agent-tier pod: has the tier label, container omits readOnlyRootFilesystem → VAP must
    # reject. A freshly-applied VAP binding has a short activation delay, so poll (up to ~40s) until it
    # rejects before judging — this distinguishes propagation latency from a real enforcement gap.
    unhard=""; urc=0
    for _ in $(seq 1 20); do
      unhard="$($K apply --dry-run=server -f - <<EOF 2>&1
apiVersion: v1
kind: Pod
metadata:
  name: p5-unhardened
  namespace: default
  labels: { kube-agents/tier: platform }
spec:
  containers:
    - name: c
      image: $IMG
EOF
)"; urc=$?
      [ "$urc" -ne 0 ] && break
      sleep 2
    done
    if [ "$urc" -ne 0 ] && echo "$unhard" | grep -qi 'readOnlyRootFilesystem'; then
      pass "VAP REJECTS un-hardened agent-tier pod at admission (error names readOnlyRootFilesystem)"
    else
      bad "VAP did NOT reject un-hardened agent-tier pod (rc=$urc — Acc c live)"; echo "$unhard" | tail -6
    fi
    # Hardened agent-tier pod: readOnlyRootFilesystem:true → admitted.
    hard="$($K apply --dry-run=server -f - <<EOF 2>&1
apiVersion: v1
kind: Pod
metadata:
  name: p5-hardened
  namespace: default
  labels: { kube-agents/tier: platform }
spec:
  containers:
    - name: c
      image: $IMG
      securityContext: { readOnlyRootFilesystem: true }
EOF
)"; hrc=$?
    [ "$hrc" -eq 0 ] && pass "VAP ADMITS a hardened agent-tier pod (readOnlyRootFilesystem:true)" \
      || { bad "VAP rejected a correctly-hardened pod (rc=$hrc — false positive, Acc c live)"; echo "$hard" | tail -6; }
    # Non-agent pod: no tier label → VAP scope excludes it, admitted despite no readOnlyRootFilesystem.
    nona="$($K apply --dry-run=server -f - <<EOF 2>&1
apiVersion: v1
kind: Pod
metadata:
  name: p5-nonagent
  namespace: default
spec:
  containers:
    - name: c
      image: $IMG
EOF
)"; nrc=$?
    [ "$nrc" -eq 0 ] && pass "VAP leaves a non-agent pod (no tier label) UNTOUCHED (scope proof)" \
      || { bad "VAP rejected a non-agent pod (rc=$nrc — over-broad scope, Acc c live)"; echo "$nona" | tail -6; }
  else
    bad "could not apply pod-hardening VAP to $CTX (Acc c live)"; tail -10 /tmp/p5-vapapply.log
  fi

  # (b) LIVE: egress ENFORCEMENT. kindnet does not enforce NetworkPolicy → DEFER (rc 3), non-fatal here
  #     because P5-T6 PROVED it on the Calico cluster. rc 1 (FAILED) is a halt condition.
  bash local-dev/tests/egress-enforcement.sh "$CTX" >/tmp/p5-egress.log 2>&1; erc=$?
  case "$erc" in
    0) pass "egress enforcement PROVEN live on $CTX (off-allowlist dest DENIED, on-allowlist ALLOWED)";;
    3) note "egress enforcement DEFERRED on $CTX (kindnet does not enforce NetworkPolicy) — PROVEN separately on Calico (P5-T6). Non-fatal.";;
    *) bad "egress enforcement FAILED on $CTX (rc=$erc — HALT, Acc b)"; tail -20 /tmp/p5-egress.log;;
  esac
else
  note "context '$CTX' unreachable — LIVE VAP + egress SKIPPED (hermetic acceptance above still authoritative)."
  note "Re-run against a deployed Kind stack (INSTALL 'Phase 2 — Kind inner loop') to exercise live admission."
fi

# ============================ REGRESSION (load-bearing) ============================
echo; echo "== REGRESSION (03 §11 + prior-phase gates + full go suite) =="
# 03 §11 adversarial attenuation (write / impersonate / wrong-scope DENIED) — the load-bearing negatives.
if $K version >/dev/null 2>&1; then
  if bash local-dev/tests/negative-attenuation.sh "$CTX" >/tmp/p5-neg.log 2>&1; then
    pass "03 §11 negative-attenuation suite green (read-only ceiling not regressed by the security gate)"
  else
    bad "03 §11 negative-attenuation suite FAILED (HALT — read-only ceiling regressed)"; tail -25 /tmp/p5-neg.log
  fi
else
  note "context '$CTX' unreachable — 03 §11 live negatives SKIPPED."
fi
# Prior-phase gates re-run end to end (each self-gates on cluster reachability).
for p in 2 3 4; do
  if bash "local-dev/kind/verify-phase$p.sh" "$CTX" >/tmp/p5-regress-$p.log 2>&1; then
    pass "verify-phase$p.sh green (Phase $p not regressed)"
  else
    bad "verify-phase$p.sh FAILED (Phase $p regressed — HALT)"; tail -20 /tmp/p5-regress-$p.log
  fi
done
# Full operator suite (goldens, router, controller, watcher) — deterministic, no cluster.
( cd k8s-operator && go test ./... >/tmp/p5-gotest.log 2>&1 ) \
  && pass "go test ./... green across the operator (goldens/router/controller/watcher — full regress)" \
  || { bad "go test ./... FAILED (regress)"; tail -25 /tmp/p5-gotest.log; }

echo
echo "  DEFERRED (not faked): egress ENFORCEMENT on kindnet → Calico run (P5-T6 PROVEN); live review-gate"
echo "  webhook → GitHub Actions / scratch-GKE.  05 §8 chaos → Phase 6 (not yet built, N-A)."
echo
echo "===================================================================="
if [ "$fail" -eq 0 ]; then echo " Phase 5 verification: ALL CHECKS PASSED"; else echo " Phase 5 verification: FAILURES ABOVE (see HALT conditions)"; fi
echo "===================================================================="
exit "$fail"
