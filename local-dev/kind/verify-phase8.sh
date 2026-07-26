#!/usr/bin/env bash
# Phase 8 (Make it real) — consolidated gate + full regression.
#
# Phase 8 adds no new persona and no new write path. It takes six things the tree DESCRIBED and makes
# them true: the allowlist actually closes, the egress policies are actually enforced, the tenant
# isolation manifests are actually applied, a multi-tier install actually works, the images are
# actually published, and the invariants gate is actually a mechanism. This script proves Accept
# (a)–(f) and re-runs every prior gate.
#
# TWO TARGETS, NEITHER DEFAULTED. Phase 8's claims do not all live on one cluster:
#   * `kind-kube-agents-dev`    runs kindnet and hosts the operator — the only place a webhook,
#                               renderer or admission claim can be made.
#   * `kind-kube-agents-egress` runs Calico and hosts NO operator — the only Kind target where an
#                               egress claim may be green (LSN-006, binding.md P4).
# So both are required positionally and neither has a default. On 2026-07-25 `L2-CHAIN.txt` appended
# one target to every line; followed literally it sent two operator-dependent checks at the
# operator-less cluster, where both correctly returned P1-UNVERIFIABLE — two of six lines silently not
# run, visible only because P1 refuses to guess. A default target here would rebuild that defect in a
# script whose whole job is to be the phase's single verdict.
#
#   A. L0 — the hermetic layer, run as `L0-CHAIN.txt`, not as a list copied out of it -------------
#      Phase 8's hermetic evidence (image provenance, egress render, install-path wiring, reference
#      render, closed allowlist, docs truth, the invariants gate itself) is ALREADY enumerated in
#      L0-CHAIN.txt, which CI runs on every PR. Re-listing those lines here would be a second
#      definition site for "what L0 means" and would drift (V-MET-013). This section runs the file.
#                                                                        (Accept a, c, e — L0 halves)
#   B. Accept (a)/(b) — install and multi-tier, live ----------------------------------------------
#      tenant-isolation-l2.sh (quota + default-deny applied FROM THE INSTALL PATH, enforced on
#      Calico), gitops-tree-applies-l2.sh (the shipped GitOps tree applies cleanly), and
#      multi-agent-namespace-l2.sh (two agents, one namespace, distinct per-agent claims).
#   C. Accept (c) — the allowlist closes at admission ---------------------------------------------
#      closed-allowlist-l2.sh on the operator cluster: a blank/empty allowlist is REJECTED by the
#      live webhook, and no rendered pod carries the retired permissive-user env var (V-CTR-014).
#      That env var is deliberately NOT spelled out here. closed-allowlist.py bans the bare
#      identifier outside the doc paths and a short list of files that assert its ABSENCE, and this
#      gate only delegates to closed-allowlist-l2.sh — it makes no assertion of its own, so it has
#      no claim on the exemption. Naming it here cost nothing and would have spread a retired
#      identifier to a seventh file; the lint caught it on this script's first run.
#   D. Accept (d) — egress enforced while WI still works ------------------------------------------
#      egress-enforcement-l2.sh on Calico. P4 is not taken on trust: this script reads the CNI off
#      the egress target and DEFERS rather than passes if it is not Calico.
#   E. Accept (c)/(e) completeness — the phase's own unfinished work, detected mechanically --------
#      V-CTR-002 is BLOCKING-PHASE and P8-T1 left it `partial`; V-MET-011 is BLOCKING-ALWAYS and may
#      not be deferred at all. Rather than a comment saying "T9/T10 pending", this section looks for
#      the artifacts those units must produce and fails while they are absent. The gate therefore
#      goes green on its own when the work lands, and cannot be talked into it before.
#   F. Accept (f) — full prior regression ---------------------------------------------------------
#      verify-phase7.sh on the operator cluster => phases 2–6, chaos C1–C4, 03 §11 negatives,
#      goldens, `go test ./...`, and the phase-7 seam artifacts.
#
# DEFERRED, NOT FAKED (recorded, never asserted green):
#   D1  V-CTN-020 at L3 — the live-WI half. `platform-agent-host` has no Dataplane V2 and enabling it
#       is a cluster recreation on a non-destructive-test target (binding.md §Targets). The
#       BLOCKING-ALWAYS L2 instance is NOT deferred and runs in section D.
#   D2  LSN-015 CLAIM 2 (both agent pods Ready in one namespace) — needs 2 schedulable nodes and
#       ~6Gi allocatable; the dev Kind is single-node. The per-agent isolation claims are not deferred.
#   D3  V-CMP-003's Config Connector slice — the CRDs are absent from Kind (21/22).
#   D4  the live-target checklist in docs/build/phase-8-live-checklist.md — every L3 step that needs a
#       human on a real GKE cluster. Listed there so it is visible, never asserted here.
#
# DESTRUCTIVE-TEST GUARD: both contexts must be Kind or a scratch GKE. `platform-agent-host` is
# outer-loop install verification only and is NOT a destructive-test target (binding.md §Targets).
# Usage: local-dev/kind/verify-phase8.sh <dev-context> <egress-context>
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions). Declared, not assumed: LSN-001 and LSN-002 each recurred
# against scripts whose authors believed the preconditions held.
#   P1 image-under-test:  kubeagents-system/control-plane=controller-manager on the DEV context only,
#      asserted here via p1_assert_build_under_test. Sections B (multi-agent), C and F all rest on the
#      operator's webhook and renderer, so a stale operator makes them statements about code that is
#      not in the tree. The EGRESS context deliberately runs no operator: its claims are about what
#      Calico enforces against manifests applied from the install path, and asserting P1 there would
#      assert something known to be false. Section A is hermetic and is unaffected.
#   P3 admission-recreate: none of this script's own — it creates no object. Each sub-script owns the
#      fixtures for the admission property it claims and answers P3 in its own block, which is the
#      only place that answer can be given without guessing about code this script does not read.
#      Where a sub-script asserts on a pod it just recreated it must reach that pod by ownership
#      (`p3_pod_of_deploy`), never by a label selector, which still matches the generation the
#      recreate deleted (LSN-025).
#   P4 dataplane:         section D is green only on Calico/Dataplane V2. Read off the egress target
#      at run time rather than inferred from its name, and DEFERRED — never passed — if absent.
#   P6 runtime-authoritative: the live objects each sub-script reads back from the API server, and,
#      where a config claim is made, the operator-rendered ConfigMap — never the image-baked
#      /opt/data/config.yaml it is mounted over (LSN-003).
#   P9 controller-written state: every `.status` read in the sub-scripts is polled or preceded by a
#      `kubectl wait --for=`, never slept on (LSN-024); enforced by the gate's P9 lint.
set -uo pipefail

DEV_CTX="${1:-}"
EGR_CTX="${2:-}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [ -z "$DEV_CTX" ] || [ -z "$EGR_CTX" ]; then
  cat >&2 <<'USAGE'
REFUSING: verify-phase8.sh needs BOTH targets, and will not pick either for you.

  usage: local-dev/kind/verify-phase8.sh <dev-context> <egress-context>
  e.g.   local-dev/kind/verify-phase8.sh kind-kube-agents-dev kind-kube-agents-egress

Phase 8's claims live on two clusters. The operator, its webhook and its renderer exist only on the
dev target; an egress claim may be green only on the Calico target (binding.md P4). A default would
send half the phase gate at the wrong cluster and report the other half as the whole — which is
exactly what L2-CHAIN.txt's single-target run loop did on 2026-07-25.
USAGE
  exit 2
fi

# Anchored `case`, both ends, on both contexts (LSN-005). Not a substring match: `gke-scratch-` as a
# prefix is the whole point, and `*gke-scratch*` would happily admit the live cluster if someone ever
# named a context after it.
for c in "$DEV_CTX" "$EGR_CTX"; do
  case "$c" in
    kind-*|gke-scratch-*) : ;;
    *) echo "REFUSING: context '$c' is neither kind-* nor gke-scratch-* (destructive-test guard)." >&2; exit 2 ;;
  esac
done
if [ "$DEV_CTX" = "$EGR_CTX" ]; then
  echo "REFUSING: both targets are '$DEV_CTX'. One cluster cannot host both an operator-backed" >&2
  echo "  admission claim and a Calico-enforced egress claim; passing the same context twice would" >&2
  echo "  make one of the two sections quietly meaningless." >&2
  exit 2
fi

fail=0
pass()  { echo "PASS: $1"; }
bad()   { echo "FAIL: $1"; fail=1; }
note()  { echo "  NOTE: $1"; }
defer() { echo "  DEFERRED (not faked): $1"; }
cd "$REPO_ROOT"

echo "===================================================================="
echo " Phase 8 verification (make it real)"
echo "   operator target: $DEV_CTX"
echo "   egress target:   $EGR_CTX"
echo "===================================================================="

dev_up=1; kubectl --context "$DEV_CTX" version >/dev/null 2>&1 || dev_up=0
egr_up=1; kubectl --context "$EGR_CTX" version >/dev/null 2>&1 || egr_up=0

. "$REPO_ROOT/local-dev/kind/lib/preconditions.sh"

# P10 FIRST, and as a hard stop rather than a per-section state. Reachability is not health: on this
# script's first end-to-end run both targets answered `kubectl version` while the egress cluster's
# kube-scheduler and kube-controller-manager sat in CrashLoopBackOff, and the run went on to report
# that tenant isolation did not hold, that the egress default-deny did not hold, and that chaos C2
# failed to replace a deleted pod. Every one of those was false — with no scheduler the fixture pods
# never left Pending, and a claim whose fixtures never ran reports its property ABSENT.
#
# Nothing below is worth attempting on a cluster in that state, which is why this exits instead of
# setting a flag: a partial run would publish the sections that happen not to need a pod alongside
# the ones that silently could not test anything, and the two look identical in the output. Exit 2 —
# could-not-run — never 1. "Phase 8's security properties do not hold" is a sentence someone acts on.
for _t in "$DEV_CTX:$dev_up" "$EGR_CTX:$egr_up"; do
  [ "${_t##*:}" -eq 1 ] || continue
  if ! p10_assert_control_plane_healthy "kubectl --context ${_t%:*}" "${_t%:*}"; then
    echo >&2
    echo "REFUSING to render a Phase 8 verdict: ${_t%:*} cannot run the experiment (P10)." >&2
    echo "  This is NOT a claim that Phase 8 regressed. Repair or recreate the cluster and re-run:" >&2
    echo "    kind delete cluster --name \"\${CLUSTER}\" && bash local-dev/kind/up-egress.sh   # or up.sh" >&2
    exit 2
  fi
done

# P1 on the dev target only — see the PRECONDITIONS block. `dev_ok` carries three states forward so
# that "could not look" reaches the report as a deferral and "does not match" reaches it as a
# failure, never as a skip (LSN-001 recurred three times against runbooks that said the same thing).
dev_ok=1
if [ "$dev_up" -eq 1 ]; then
  p1_assert_build_under_test "kubectl --context $DEV_CTX" kubeagents-system control-plane=controller-manager
  case "$?" in
    0) pass "P1: the operator on $DEV_CTX is the build under test" ;;
    3) dev_ok=3 ;;
    *) bad "P1: $DEV_CTX is not running the build under test"; dev_ok=0 ;;
  esac
else
  bad "operator target '$DEV_CTX' is unreachable — sections B, C and F are load-bearing and cannot be skipped"
  dev_ok=0
fi

# P4 read off the cluster, not inferred from the context name. A name is a claim about a dataplane;
# the dataplane is a fact about it, and only one of those can be wrong without anyone noticing.
calico=0
if [ "$egr_up" -eq 1 ]; then
  if kubectl --context "$EGR_CTX" -n kube-system get pods -o name 2>/dev/null | grep -qi 'calico'; then
    calico=1
    pass "P4: $EGR_CTX runs Calico — an egress claim may be green here"
  else
    cni="$(kubectl --context "$EGR_CTX" -n kube-system get pods -o name 2>/dev/null | grep -m1 -iE 'kindnet|cilium|calico' || echo 'unknown')"
    note "P4: $EGR_CTX runs ${cni##*/}, not Calico — section D will be DEFERRED, never passed (LSN-006)"
  fi
else
  note "egress target '$EGR_CTX' is unreachable — section D will be DEFERRED, never passed"
fi

# run_l2 <label> <script> <context> <what-it-proves>
#   rc 0 pass · 1 fail · 2 could-not-run · 3 DEFERRED. The 3 is why this is a function and not a loop
#   over a list: a claim-level deferral with a named blocker (LSN-015 CLAIM 2) and a P1-UNVERIFIABLE
#   both exit 3, and they are not the same event. P1 is asserted above precisely so that, by the time
#   we get here, a 3 from the dev target can only be the former.
run_l2() {
  local label="$1" script="$2" ctx="$3" what="$4" log="/tmp/p8-${1}.log"
  bash "$script" "$ctx" >"$log" 2>&1
  case "$?" in
    0) pass "$label green on $ctx — $what"
       grep -E '^PASS' "$log" | tail -6 | sed 's/^/    /' ;;
    3) defer "$label returned DEFERRED on $ctx. Its own log names the blocker; not counted as a pass."
       grep -E 'DEFERRED|CLAIM' "$log" | head -4 | sed 's/^/    /' ;;
    2) bad "$label COULD NOT RUN on $ctx — $what is unproven. Log: $log"
       tail -12 "$log" ;;
    *) bad "$label FAILED on $ctx — $what does not hold (HALT). Log: $log"
       tail -25 "$log" ;;
  esac
}

# ==== A. L0 — run the chain file, do not re-list it ==================================================
echo; echo "== A. L0 hermetic layer — L0-CHAIN.txt run exactly as the file instructs =="
l0_n=0; l0_bad=0
while read -r c; do
  case "$c" in ''|\#*) continue ;; esac
  l0_n=$((l0_n + 1))
  if ! eval "$c" >"/tmp/p8-l0-${l0_n}.log" 2>&1; then
    l0_bad=$((l0_bad + 1))
    bad "L0: \`$c\` FAILED — log /tmp/p8-l0-${l0_n}.log"
    tail -12 "/tmp/p8-l0-${l0_n}.log"
  fi
done < local-dev/L0-CHAIN.txt
if [ "$l0_n" -lt 13 ]; then
  bad "L0-CHAIN.txt yielded only $l0_n runnable lines; there were 13 when this gate was written. The"
  bad "  chain shrank, so 'L0 green' now covers less than it says (V-MET-014)."
elif [ "$l0_bad" -eq 0 ]; then
  pass "L0 chain green — $l0_n/$l0_n (image provenance, egress render, install-path wiring, reference render, closed allowlist, docs truth, invariants gate)"
fi

# ==== B. Accept (a)/(b) — install and multi-tier, live ==============================================
echo; echo "== B. Accept (a)/(b) — tenant isolation applied from the install path; multi-tier install =="
if [ "$egr_up" -eq 1 ]; then
  run_l2 tenant-isolation local-dev/kind/tenant-isolation-l2.sh "$EGR_CTX" \
    "the ResourceQuota and namespace default-deny are applied BY PROVISIONING and bind"
  run_l2 gitops-tree local-dev/kind/gitops-tree-applies-l2.sh "$EGR_CTX" \
    "the shipped GitOps tree applies cleanly with no REPLACE_WITH_* reaching a live object"
else
  bad "egress target '$EGR_CTX' unreachable — the tenant-isolation and GitOps-tree claims are load-bearing"
fi
if [ "$dev_ok" -eq 1 ]; then
  run_l2 multi-agent local-dev/kind/multi-agent-namespace-l2.sh "$DEV_CTX" \
    "two agents share a namespace with distinct per-agent identities, claims and volumes (LSN-015)"
elif [ "$dev_ok" -eq 3 ]; then
  defer "the multi-tier claim — P1 unverifiable on $DEV_CTX, so a green would be about unknown code."
  fail=1
fi

# ==== C. Accept (c) — the allowlist closes at admission =============================================
echo; echo "== C. Accept (c) — empty/blank allowlist rejected at admission; no permissive-user env (V-CTR-014) =="
if [ "$dev_ok" -eq 1 ]; then
  run_l2 closed-allowlist local-dev/kind/closed-allowlist-l2.sh "$DEV_CTX" \
    "the live webhook rejects a blank allowlist and no rendered pod carries the escape hatch (V-CTR-014)"
elif [ "$dev_ok" -eq 3 ]; then
  defer "the admission claim — P1 unverifiable on $DEV_CTX. A webhook check against an unknown"
  echo "           operator build proves nothing about this tree."
  fail=1
fi

# ==== D. Accept (d) — egress enforced, on a dataplane that enforces =================================
echo; echo "== D. Accept (d) — off-allowlist egress blocked while Workload Identity still works =="
if [ "$calico" -eq 1 ]; then
  run_l2 egress-enforcement local-dev/kind/egress-enforcement-l2.sh "$EGR_CTX" \
    "default-deny holds, port narrowing is enforced, and the metadata allow is absent without WI"
else
  defer "V-CTN-020's L2 instance — $EGR_CTX does not run an enforcing dataplane. On kindnet a"
  echo "           NetworkPolicy is accepted and ignored, so every negative here would pass for the"
  echo "           wrong reason (LSN-006). BLOCKING-ALWAYS: this is a failure of the RUN, not of the"
  echo "           claim — stand up the Calico target with local-dev/kind/up-egress.sh and re-run."
  fail=1
fi
defer "V-CTN-020's live-WI half at L3 — platform-agent-host has no Dataplane V2 and is not a"
echo "           destructive-test target (D1). Carried, with a named blocker, never asserted here."

# ==== E. The phase's own unfinished work, detected rather than remembered ===========================
echo; echo "== E. Phase-8 completeness — BLOCKING-PHASE and BLOCKING-ALWAYS gaps =="
# V-CTR-002 is BLOCKING-PHASE and P8-T1 closed only its V-7 slice. The remaining obligation is a
# negative test per rule V-1..V-10, each denial naming its field path. Detected by looking for the
# artifact P8-T9 must produce, so this flips green when the work lands and not when someone edits a
# comment. Ten rules, and a file that exists but tests three of them is not the claim.
NEG="local-dev/kind/webhook-negatives-l2.sh"
if [ -f "$NEG" ]; then
  covered="$(grep -oE '\bV-(10|[1-9])\b' "$NEG" | sort -u | wc -l | tr -d ' ')"
  if [ "$covered" -ge 10 ]; then
    run_l2 webhook-negatives "$NEG" "$DEV_CTX" "each of 06 §1.2 V-1..V-10 has a negative rejected with its field path"
  else
    bad "V-CTR-002 INCOMPLETE: $NEG covers $covered of the 10 rules in 06 §1.2. V-CTR-002 is"
    bad "  BLOCKING-PHASE — the phase gate does not pass while it is partial (P8-T9)."
  fi
else
  bad "V-CTR-002 INCOMPLETE: P8-T1 closed the V-7 slice only; rules V-6 (cross-object ceiling), V-8"
  bad "  (budget clamp) and V-10 (reader-only SA override) are unimplemented and there is no"
  bad "  V-1..V-10 negative suite at $NEG. V-CTR-002 is BLOCKING-PHASE (09 §9.6) — this gate is RED"
  bad "  until P8-T9 lands, and that is the check working, not a bug in it."
fi
# V-MET-011 is BLOCKING-ALWAYS, so 09 §9.6 forbids deferring it. "The generator is not written" is
# unwritten work, not an external blocker — hence a gate, not a deferrals row.
TRACE="verification/traceability.yaml"
if [ -f "$TRACE" ]; then
  pass "V-MET-011: $TRACE exists — the traceability matrix is checked in (bidirectional lint lives in spec-ids.py, run in section A)"
else
  bad "V-MET-011 MISSING: $TRACE is not checked in. Every bullet in the six Verification sections of"
  bad "  01–08 (176 of them) must map to at least one check ID in 09. V-MET is BLOCKING-ALWAYS and"
  bad "  may not be deferred (P8-T10)."
fi

# ==== F. Accept (f) — full prior regression =========================================================
echo; echo "== F. Accept (f) — verify-phase7.sh (=> phases 2–6, chaos C1–C4, 03 §11, goldens, go test) =="
if [ "$dev_ok" -eq 1 ]; then
  if bash local-dev/kind/verify-phase7.sh "$DEV_CTX" >/tmp/p8-regress.log 2>&1; then
    pass "verify-phase7.sh green — the whole prior ratchet survived Phase 8"
    grep -E 'ALL CHECKS PASSED|verify-phase[23456]\.sh green|chaos suite green|go test' /tmp/p8-regress.log \
      | tail -8 | sed 's/^/    /'
  else
    bad "verify-phase7.sh FAILED — Phase 8 regressed a prior phase (HALT). Log: /tmp/p8-regress.log"
    grep -E '^FAIL' /tmp/p8-regress.log | head -20
  fi
elif [ "$dev_ok" -eq 3 ]; then
  defer "the full regression — P1 unverifiable. This is a deferral and NOT a pass: the suite is"
  echo "           load-bearing, so a run that cannot establish which code it exercised does not"
  echo "           discharge it. Rebuild, kind load, rollout restart, and run this again."
  fail=1
fi

# ==== G. Deferrals and the live-target checklist ====================================================
echo; echo "== G. Deferred-not-faked (recorded; never asserted green) =="
defer "LSN-015 CLAIM 2 — both agent pods Ready in one namespace needs 2 schedulable nodes and ~6Gi"
echo "           allocatable; the dev Kind is single-node. The per-agent isolation claims are NOT deferred (D2)."
defer "V-CMP-003's Config Connector slice — those CRDs are absent from Kind (21/22) (D3)."
defer "every L3 step needing a human on a real GKE cluster — enumerated in"
echo "           docs/build/phase-8-live-checklist.md, with the command and the expected observation"
echo "           for each. Listed so it is visible; nothing there is asserted by this script (D4)."

echo
echo "===================================================================="
if [ "$fail" -eq 0 ]; then
  echo " Phase 8 verification: ALL CHECKS PASSED"
else
  echo " Phase 8 verification: FAILURES ABOVE (see HALT conditions)"
fi
echo "===================================================================="
exit "$fail"
