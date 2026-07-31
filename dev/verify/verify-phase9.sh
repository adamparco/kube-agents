#!/usr/bin/env bash
# Phase 9 (The action pipeline) — consolidated gate + full regression.
#
# Phase 9 is the phase where an agent stops describing a change and makes one. It adds the Action
# Envelope, the broker that receives it, the classifier that rules on it, the journal that records
# it, the undo plan that can take it back, and the brake that stops all of it. This script proves
# Accept (a)–(e) from 07 §2 and re-runs every prior gate.
#
# ONE TARGET, NOT DEFAULTED — same argument as verify-phase8.sh, which is this script's template.
# The target is required positionally. A default would be read from `kubectl config current-context`,
# which on this machine may well be `platform-agent-host`, the live install; this script deletes
# pods, mints tokens for a deliberately-wrong caller and drives denial paths.
#
#   A. L0 — the hermetic layer, run as `L0-CHAIN.txt`, not as a list copied out of it -------------
#      Phase 9's hermetic evidence (the classifier corpus lint, the undo corpus lint, the
#      model-free classifier scan, pause-is-not-scale-to-zero, the scope label, one-broker-per-agent,
#      broker supply-chain minimality, the actor grant, the journal-status/VAP parity, the negative
#      controls check itself) is ALREADY enumerated in L0-CHAIN.txt, which CI runs on every PR.
#      Re-listing those lines here would be a second definition site for "what L0 means" and would
#      drift (V-MET-013). This section runs the file.
#   B. Accept (a) — an envelope flows end to end and produces a record with a valid undo plan -----
#      broker-per-agent-l2.sh (the pair exists at all, V-BRK-012), reference-index-l2.sh
#      (V-REV-010), verify-prober-l2.sh (V-PRO-027) and rollback-replayer-l2.sh (V-REV-011) are the
#      undo machinery. The end-to-end flow itself is broker-execute-l2.sh, which T9b-5 owes and
#      which this section detects BY ARTIFACT rather than by remembering that it is pending.
#   C. Accept (b) — the classifier over live state, and a policy that cannot loosen ---------------
#      classify-live-state-l2.sh (V-GAT-022). The fixture-corpus half is hermetic and runs in
#      section A (classifier-corpus-lint.py) and section H (`go test ./...` through the regression).
#   D. Accept (c) — an envelope claiming somebody else's scope is rejected -------------------------
#      broker-auth-l2.sh: mTLS required-and-verified, the projected token's audience, the TokenReview,
#      and the scope-in-the-body refusal. V-BRK-007 · 008 · 009 · 010 · 017 · 031, every one of them
#      BLOCKING-ALWAYS, which is why this section has no deferral arm that reaches a green.
#   E. Accept (d) — pause and freeze with the inference stack down; refusal with no journal --------
#      brake-fanout-l2.sh (V-REV-006) is the brake half and it is live. The journal-unavailable half
#      is the broker's: broker-refuse-l2.sh revokes the actionrecords grant out from under a running
#      broker and reads the 503. Detected by artifact AND by chain membership, then run.
#   F. Accept (e) — no agent identity anywhere in the fleet holds a write verb --------------------
#      A full two-sided `auth can-i` sweep, which is actor-grant-sweep-l2.sh, T9b-5's. The L0 half —
#      that the grant has exactly one definition site — is actor-grant-single-sourced.py in
#      section A. Detected by artifact.
#   G. The phase's own unfinished work, detected rather than remembered --------------------------
#      V-BRK-021's L2 half and planning defect 2's guard 1. Both are looked for as artifacts, so
#      this gate goes green on its own when the work lands and cannot be talked into it before.
#   H. The ratchet — full prior regression --------------------------------------------------------
#      verify-phase8.sh on the same cluster => phases 2–7, chaos C1–C4, 03 §11 negatives, goldens,
#      `go test ./...`, the phase-7 seam artifacts and all six Phase-8 suites.
#
# DEFERRED, NOT FAKED (recorded, never asserted green): printed in section I, and each one is a row
# in docs/build/LEDGER.md with a named external blocker. Nothing in section I is counted as a pass,
# and nothing BLOCKING-ALWAYS is allowed to appear there (09 §9.6) — a BLOCKING-ALWAYS gap is a
# section-B/F/G failure instead, which is the difference between "we could not run this" and "this
# is not built yet".
#
# WHY THE MISSING-ARTIFACT ARMS ARE FAILURES AND NOT SKIPS. Sections B, E, F and G are RED today, on
# purpose, because T9b-5 has not landed. That is the gate working. The alternative — a comment saying
# "broker-execute-l2.sh pending" — is a gate that reports green on a phase whose central acceptance
# bullet has never been executed once, and the only thing standing between that and a milestone is
# whoever remembers to read the comment. Section E of verify-phase8.sh is the precedent and it is the
# reason V-CTR-002's ten-rule gap could not be quietly closed by editing prose.
#
# DESTRUCTIVE-TEST GUARD: the context must be a scratch GKE (`gke-scratch-*`). `platform-agent-host`
# is outer-loop install verification only and is NOT a destructive-test target (binding.md §Targets).
# Usage: dev/verify/verify-phase9.sh <context>
#
# Exit: 0 = every Phase-9 acceptance bullet proven · 1 = at least one failed or is unbuilt ·
#       2 = refused target, or the cluster cannot run the experiment (P10).
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions). Declared, not assumed: LSN-001 and LSN-002 each recurred
# against scripts whose authors believed the preconditions held.
#   P1 image-under-test:  kubeagents-system/control-plane=controller-manager, asserted here via
#      p1_assert_build_under_test. Phase 9's operator is what renders the broker Deployment, the
#      broker Service, the per-agent ServiceAccount and the ConfigMap the pod actually reads, so a
#      stale operator makes sections B through G statements about code that is not in this tree. The
#      broker image has its OWN P1, asserted inside broker-per-agent-l2.sh and broker-auth-l2.sh
#      against the digest the controller hands out — it is not asserted here, because this script
#      does not know which image the controller chose and guessing would be the LSN-001 shape with a
#      paragraph in front of it.
#   P3 admission-recreate: none — this script creates no object of its own. Each sub-script owns the
#      fixtures for the admission property it claims and answers P3 in its own block, which is the
#      only place that answer can be given without guessing about code this script does not read.
#      Where a sub-script asserts on a pod it just recreated it must reach that pod by ownership
#      (`p3_pod_of_deploy`), never by a label selector, which still matches the generation the
#      recreate deleted (LSN-025).
#   P6 runtime-authoritative: the live objects each sub-script reads back from the API server, and,
#      where a config claim is made, the operator-rendered ConfigMap — never the image-baked
#      /opt/data/config.yaml it is mounted over (LSN-003). Phase 9 adds a second instance of the same
#      trap: the broker's route table and its allowlist are compiled into the broker binary, so the
#      only honest place to read them is the running process, which is what broker-auth-l2.sh probes.
#   P4 dataplane:         none of this script's own sections makes a NetworkPolicy claim. Section H
#      delegates to verify-phase8.sh, which asks its egress target for its dataplane at run time and
#      DEFERS rather than passes on one not known to enforce (LSN-006). The Phase-9 egress hole —
#      the broker's own egress to the API server — is P9-T7d-4 and is unscheduled; it is a ledger
#      finding, not a claim this script makes.
#   P9 controller-written state: every `.status` read in the sub-scripts is polled or preceded by a
#      `kubectl wait --for=`, never slept on (LSN-024); enforced by the gate's P9 lint.
#   P10 control-plane-healthy: asserted first, below, as a hard stop.
set -uo pipefail

DEV_CTX="${1:-}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [ -z "$DEV_CTX" ]; then
  cat >&2 <<'USAGE'
REFUSING: verify-phase9.sh will not pick a target for you.

  usage: dev/verify/verify-phase9.sh <context>
  e.g.   dev/verify/verify-phase9.sh gke-scratch-kube-agents-dev

  Stand it up first: dev/cluster/up.sh

This script deletes pods, mints a bearer token for a deliberately-wrong caller, applies test-only
RBAC and drives the broker's denial paths. A default target is a guess about which cluster you
meant, and the guess would be read from `kubectl config current-context`, which on this machine may
well be the live GKE cluster.
USAGE
  exit 2
fi

# Anchored `case`, both ends (LSN-005). Not a substring match: `gke-scratch-` as a prefix is the
# whole point, and `*gke-scratch*` would happily admit the live cluster if someone ever named a
# context after it.
case "$DEV_CTX" in
  gke-scratch-*) : ;;
  *) echo "REFUSING: context '$DEV_CTX' is not gke-scratch-* (destructive-test guard)." >&2; exit 2 ;;
esac

fail=0
pass()  { echo "PASS: $1"; }
bad()   { echo "FAIL: $1"; fail=1; }
note()  { echo "  NOTE: $1"; }
defer() { echo "  DEFERRED (not faked): $1"; }
# `|| exit 2` rather than the bare `cd` verify-phase8.sh uses. Section A runs 43 L0 commands relative
# to the working directory and sections B-G resolve script paths the same way, so a cd that failed
# would not produce a wrong answer — it would produce 43 wrong answers about a tree that is not this
# one. Could-not-run is 2, never 1.
cd "$REPO_ROOT" || { echo "REFUSING: cannot cd to $REPO_ROOT" >&2; exit 2; }

echo "===================================================================="
echo " Phase 9 verification (the action pipeline)"
echo "   target: $DEV_CTX"
echo "===================================================================="

dev_up=1; kubectl --context "$DEV_CTX" version >/dev/null 2>&1 || dev_up=0

. "$REPO_ROOT/dev/lib/preconditions.sh"

# P10 FIRST, and as a hard stop rather than a per-section state — verify-phase8.sh's comment explains
# why at length and the reasoning is unchanged: on a cluster with no scheduler, fixture pods never
# leave Pending and every claim whose fixtures never ran reports its property ABSENT, which is
# indistinguishable in the output from the property being violated. Exit 2 — could-not-run — never 1.
# "Phase 9's write path is not safe" is a sentence someone acts on.
if [ "$dev_up" -eq 1 ]; then
  if ! p10_assert_control_plane_healthy "kubectl --context $DEV_CTX" "$DEV_CTX"; then
    echo >&2
    echo "REFUSING to render a Phase 9 verdict: $DEV_CTX cannot run the experiment (P10)." >&2
    echo "  This is NOT a claim that Phase 9 regressed. Repair or recreate the cluster and re-run:" >&2
    echo "    bash dev/cluster/down.sh && bash dev/cluster/up.sh" >&2
    exit 2
  fi
fi

# P1. `dev_ok` carries three states forward so that "could not look" reaches the report as a
# deferral and "does not match" reaches it as a failure, never as a skip (LSN-001 recurred three
# times against runbooks that said the same thing).
dev_ok=1
if [ "$dev_up" -eq 1 ]; then
  p1_assert_build_under_test "kubectl --context $DEV_CTX" kubeagents-system control-plane=controller-manager
  case "$?" in
    0) pass "P1: the operator on $DEV_CTX is the build under test" ;;
    3) dev_ok=3 ;;
    *) bad "P1: $DEV_CTX is not running the build under test"; dev_ok=0 ;;
  esac
else
  bad "target '$DEV_CTX' is unreachable — every section below B is load-bearing and cannot be skipped"
  dev_ok=0
fi

# run_l2 <label> <script> <context> <what-it-proves>
#   rc 0 pass · 1 fail · 2 could-not-run · 3 DEFERRED. The 3 is why this is a function and not a loop
#   over a list: a claim-level deferral with a named blocker and a P1-UNVERIFIABLE both exit 3, and
#   they are not the same event. P1 is asserted above precisely so that, by the time we get here, a 3
#   can only be the former.
run_l2() {
  local label="$1" script="$2" ctx="$3" what="$4" log="/tmp/p9-${1}.log"
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

# p1_gated <section-letter> <what-the-green-would-have-been-about>
#   The three-state P1 read, applied once per section instead of copied into each. A dev_ok of 3 is a
#   DEFERRAL THAT STILL FAILS THE GATE: the suites below are load-bearing, so a run that cannot
#   establish which code it exercised does not discharge them.
p1_gated() {
  case "$dev_ok" in
    1) return 0 ;;
    3) defer "section $1 — P1 unverifiable on $DEV_CTX, so a green would be about $2."
       echo "           Run dev/cluster/reload-images.sh operator $DEV_CTX, then run this again."
       fail=1; return 1 ;;
    *) return 1 ;;
  esac
}

# ==== A. L0 — run the chain file, do not re-list it =================================================
echo; echo "== A. L0 hermetic layer — L0-CHAIN.txt run exactly as the file instructs =="
l0_n=0; l0_bad=0
while read -r c; do
  case "$c" in ''|\#*) continue ;; esac
  l0_n=$((l0_n + 1))
  if ! eval "$c" >"/tmp/p9-l0-${l0_n}.log" 2>&1; then
    l0_bad=$((l0_bad + 1))
    bad "L0: \`$c\` FAILED — log /tmp/p9-l0-${l0_n}.log"
    tail -12 "/tmp/p9-l0-${l0_n}.log"
  fi
done < dev/L0-CHAIN.txt
# 43 is the count on the day this gate was written, not a round number and not a floor with slack in
# it. A floor below the real count tolerates exactly the change it exists to notice — L2_CHAIN_FLOOR
# spent three phases at 6 against a 14-line chain for want of this sentence. Raise it in the same
# commit that adds a line; lower it only in the commit that argues a line out (V-MET-014).
if [ "$l0_n" -lt 43 ]; then
  bad "L0-CHAIN.txt yielded only $l0_n runnable lines; there were 43 when this gate was written. The"
  bad "  chain shrank, so 'L0 green' now covers less than it says (V-MET-014)."
elif [ "$l0_bad" -eq 0 ]; then
  pass "L0 chain green — $l0_n/$l0_n (incl. classifier corpus, undo corpus, model-free classifier, pause≠scale-to-zero, scope label, one-broker-per-agent, broker supply chain, actor grant, journal/VAP parity)"
fi

# ==== B. Accept (a) — an envelope end to end, and an undo plan that is worth the name ===============
echo; echo "== B. Accept (a) — envelope -> ActionRecord -> valid undo plan, in shadow mode =="
if p1_gated B "an operator build nobody can name"; then
  run_l2 broker-per-agent dev/verify/broker-per-agent-l2.sh "$DEV_CTX" \
    "each Agent gets exactly one broker Deployment, Service and ServiceAccount of its own (V-BRK-012)"
  run_l2 reference-index dev/verify/reference-index-l2.sh "$DEV_CTX" \
    "the reference index resolves the objects an undo plan names, live (V-REV-010)"
  run_l2 verify-prober dev/verify/verify-prober-l2.sh "$DEV_CTX" \
    "the post-write prober reads back what was written and reports a divergence as one (V-PRO-027)"
  run_l2 rollback-replayer dev/verify/rollback-replayer-l2.sh "$DEV_CTX" \
    "a recorded undo plan replays against a live API server and restores the prior state (V-REV-011)"
fi
# The write authority the end-to-end arm stands on, and the ruling that grants it. Listed inside B
# rather than in a section of its own because it is not an acceptance bullet — it is the reason the
# next arm can exist at all. `execute/client.go` issues real API calls with `client.DryRunAll`, and a
# server-side dry-run is AUTHORIZED before it is dry-run, so an actor holding only the 06 §2.2.1
# broker-operations grant gets a 403 at step 8 exactly as a live write would. The overlay supplies
# that authority; this suite proves the overlay is bounded to what its fixture says, and that the
# admission ruling behind it — the fixture wears neither agent label, so `vap-agent-readonly` does
# not select it, so nothing was carved out of a BLOCKING-ALWAYS policy for a test — still holds
# against the deployed policy rather than against a paragraph someone wrote about the CEL.
if p1_gated B "an operator build nobody can name"; then
  run_l2 actor-overlay-admission dev/verify/actor-overlay-admission-l2.sh "$DEV_CTX" \
    "the test-only tenant write overlay is admitted only because it is outside the policy's population, and grants exactly its four verbs in one namespace (P9-T9b-5a ruling; no check ID)"
fi

# The bullet's own verb is "flows end-to-end", and none of the four above is that. Detected by
# artifact so it flips green when T9b-5 lands and not when someone edits this comment.
EXEC="dev/verify/broker-execute-l2.sh"
if [ -f "$EXEC" ]; then
  if p1_gated B "an operator build nobody can name"; then
    run_l2 broker-execute "$EXEC" "$DEV_CTX" \
      "one envelope traverses submit -> classify -> journal -> shadow-execute and yields a well-formed ActionRecord with a valid undo plan (V-BRK-006/018/019, V-REV-002/003)"
  fi
else
  bad "Accept (a) UNPROVEN: there is no $EXEC. The four suites above prove the undo MACHINERY —"
  bad "  the index resolves, the prober reads back, the replayer restores — and not one of them"
  bad "  submits an envelope. Accept (a) is the sentence 'an envelope flows end-to-end in shadow mode"
  bad "  and produces a well-formed ActionRecord with a valid undo plan', and it has never been"
  bad "  executed once against a cluster. V-BRK-006 · 018 · 019 and V-REV-002 are BLOCKING-ALWAYS and"
  bad "  may not be deferred (09 §9.6) — this gate is RED until P9-T9b-5 lands, and that is the check"
  bad "  working, not a bug in it."
fi

# ==== C. Accept (b) — the classifier over live state; a policy that tightens and cannot loosen ======
echo; echo "== C. Accept (b) — four classes over live cluster state; ChangePolicy tightens only =="
if p1_gated C "an operator build nobody can name"; then
  run_l2 classify-live-state dev/verify/classify-live-state-l2.sh "$DEV_CTX" \
    "the classifier's ruling on live objects matches the corpus, and a ChangePolicy can only tighten (V-GAT-022)"
fi
note "the fixture-corpus half of (b) is hermetic: classifier-corpus-lint.py and"
echo "        classifier-is-model-free.py ran in section A, and the four-class corpus itself runs in"
echo "        \`go test ./...\` through section H. It is not re-listed here (V-MET-013)."

# ==== D. Accept (c) — an envelope claiming somebody else's scope is rejected ========================
echo; echo "== D. Accept (c) — mTLS, audience, TokenReview, and the scope-in-the-body refusal =="
if p1_gated D "an operator build nobody can name"; then
  run_l2 broker-auth dev/verify/broker-auth-l2.sh "$DEV_CTX" \
    "the live broker derives (tier, scope) from the authenticated caller and refuses a body that claims another (V-BRK-007/008/009/010/017/031)"
fi

# ==== E. Accept (d) — pause and freeze with inference down; refusal with no journal =================
echo; echo "== E. Accept (d) — the brake bites with the inference stack down; no journal, no write =="
if p1_gated E "an operator build nobody can name"; then
  run_l2 brake-fanout dev/verify/brake-fanout-l2.sh "$DEV_CTX" \
    "pause and freeze fan out to every agent in scope and take effect with the inference stack down (V-REV-006)"
fi
# The second half of (d) is the broker's, not the brake's: taking the journal away from a RUNNING
# broker and watching it decline.
#
# THIS ARM POINTED AT THE WRONG FILE UNTIL 2026-07-31, AND PASSED. It tested `[ -f "$EXEC" ]` and
# then asserted, in its own PASS line, that broker-execute-l2.sh "carries the journal-unavailable
# refusal" — which that suite does not and never claimed to: it submits one envelope that WORKS and
# says so in its own header ("Nothing here fails, on purpose"). A detector aimed at the wrong
# artifact reads exactly like a detector that is satisfied, which is [[LSN-060]]'s shape arriving
# through a different door. Retargeted at the suite that actually carries the property, in the same
# unit that built it, and strengthened while it was open: existence alone was never enough, because
# a suite in no chain line is evidence nobody gathers (section G's argument, applied here).
REFUSE="dev/verify/broker-refuse-l2.sh"
if [ ! -f "$REFUSE" ]; then
  bad "Accept (d) HALF UNPROVEN: there is no $REFUSE. V-BRK-023 proved at L1+envtest that a Confirmer"
  bad "  refuses all four flavours of unavailable, but nothing has yet taken the journal away from a"
  bad "  RUNNING broker and watched it decline. brake-fanout-l2.sh above is the BRAKE half of (d) and"
  bad "  does not reach the broker; broker-execute-l2.sh is the accepting path and refuses nothing."
elif ! grep -qF "$REFUSE" dev/L2-CHAIN.txt; then
  bad "Accept (d) journal half: $REFUSE exists but is in no live line of dev/L2-CHAIN.txt, so nothing"
  bad "  runs it as part of an L2 run. Evidence that is not in the chain is evidence nobody gathers."
elif p1_gated E "an operator build nobody can name"; then
  run_l2 broker-refuse "$REFUSE" "$DEV_CTX" \
    "the actionrecords grant is revoked out from under a RUNNING broker and it refuses 503 journal-unavailable rather than executing unjournaled; and a two-target envelope with one unreadable target applies neither (Accept d journal half, V-BRK-018)"
fi

# ==== F. Accept (e) — no agent identity anywhere in the fleet holds a write verb ====================
echo; echo "== F. Accept (e) — full two-sided \`auth can-i\` sweep over every agent identity =="
SWEEP="dev/verify/actor-grant-sweep-l2.sh"
if [ -f "$SWEEP" ]; then
  if p1_gated F "an operator build nobody can name"; then
    run_l2 actor-grant-sweep "$SWEEP" "$DEV_CTX" \
      "every agent ServiceAccount in the fleet answers no to every mutating verb, and the broker's answers yes (Accept e)"
  fi
else
  bad "Accept (e) UNPROVEN: there is no $SWEEP. actor-grant-single-sourced.py (section A) proves the"
  bad "  grant has ONE definition site; it does not prove what the API server will actually answer."
  bad "  The bullet says 'verified by a full auth can-i sweep', and a sweep is two-sided or it is not"
  bad "  one — every agent identity must answer no, and the broker's identity must answer yes, or a"
  bad "  fleet whose RBAC failed to apply at all would pass the negative half perfectly. P9-T9b-5."
fi

# ==== G. The phase's own unfinished work, detected rather than remembered ===========================
echo; echo "== G. Phase-9 completeness — BLOCKING-ALWAYS gaps in this phase's own ledger =="
# V-BRK-021 is levels L0, L2 in 09 §6. Its L0 half went green on 2026-07-30 (P9-T7c-2c, results.csv
# row 138, reshaped from the route-COUNT form). The L2 half is the shipped-image clause — debug
# routes, override query params and bypass headers all 404/405 against a RUNNING broker, one
# listening port on the pod, and no build-tag-guarded skip path in the image the controller actually
# handed out. L1 evidence exists (row 54) and is not one of the required levels, so it does not
# discharge this.
#
# Detected as: some dev/verify/*-l2.sh claims the ID, and that script is in L2-CHAIN.txt. Both halves
# matter. A script that claims it but is in no chain is evidence nobody runs; a chain line that runs
# a script claiming nothing is a chain that has grown a line without growing a claim.
b21="$(grep -l 'V-BRK-021' dev/verify/*-l2.sh 2>/dev/null | head -1)"
if [ -n "$b21" ] && grep -qF "$b21" dev/L2-CHAIN.txt; then
  pass "V-BRK-021 L2: claimed by $b21, which is a live line of dev/L2-CHAIN.txt (L0 half green since 2026-07-30)"
elif [ -n "$b21" ]; then
  bad "V-BRK-021 L2: $b21 claims the ID but is in no live line of dev/L2-CHAIN.txt, so nothing runs"
  bad "  it as part of an L2 run. Evidence that is not in the chain is evidence nobody gathers."
else
  bad "V-BRK-021 L2 MISSING: no dev/verify/*-l2.sh claims it. Its L0 half is green (results.csv row"
  bad "  138, P9-T7c-2c) and 09 §6 requires L0 AND L2. The L2 half is the clause the L0 half cannot"
  bad "  reach: debug routes, override query params and the ten X-Kube-Agents-* bypass headers all"
  bad "  404/405 against a RUNNING broker, exactly one listening port on the pod, and no"
  bad "  build-tag-guarded skip path in the image the controller handed out. A source scan proves"
  bad "  what the tree says; only a probe proves what was shipped. V-BRK is BLOCKING-ALWAYS and may"
  bad "  not be deferred (09 §9.6). P9-T9b-5."
fi
# Planning defect 2, guard 1 — "the single worst outcome of this decision", in that paragraph's own
# words. The tenant test-only overlay grants write verbs to a fixture identity; the guard is the lint
# that keeps it out of an install path.
#
# The first version of this arm matched a FUNCTION NAME containing "test_only" or "overlay" and went
# green on the first run. It was right by accident: the guard had landed the same day under
# P9-T8b-4b-ii-2a as check_test_only_grants_are_confined (V-CTN-037), so the name match and the truth
# coincided. A name match is not the property, and the whole reason section G exists is that this
# gate is supposed to be able to tell those apart. It now asserts three things about the artifact:
# the check discovers by the MARKER (`kube-agents/test-only-grant`) rather than by a path, which is
# what makes it more than a headcount of the one fixture anybody remembers (LSN-036); it is
# REGISTERED in the gate's CHECKS table, because a lint nothing calls checks nothing; and the gate
# itself is a line of L0-CHAIN.txt, so it runs on every PR rather than when someone thinks of it.
if python3 - <<'PY'
import pathlib, re, sys
gate = pathlib.Path("dev/tests/invariants-gate.py")
chain = pathlib.Path("dev/L0-CHAIN.txt")
if not gate.exists() or not chain.exists():
    sys.exit(1)
src = gate.read_text()
MARKER = "kube-agents/test-only-grant"
# Split into function bodies so "references the marker" is a claim about ONE check, not about the
# file — every string in the file is in the file.
bodies = {}
name = None
for line in src.splitlines():
    m = re.match(r"^def (\w+)", line)
    if m:
        name = m.group(1)
        bodies[name] = []
    elif name and (line.startswith((" ", "\t")) or not line.strip()):
        bodies[name].append(line)
    elif line.strip():
        name = None
registry = src.split("CHECKS = [", 1)[-1]
# The names a check may legitimately use to MEAN the marker: the literal itself, or a module
# constant whose VALUE is the literal. Resolving the constant rather than accepting its spelling is
# the difference between this arm and the name match it replaced -- when this was first written it
# also accepted the bare identifier `TEST_ONLY_MARKER`, and a mutant that hollowed the constant out
# to "unused-sentinel" while keeping the name sailed straight through it.
aliases = {MARKER} | {
    m.group(1) for m in re.finditer(rf'^(\w+)\s*=\s*[\'"]{re.escape(MARKER)}[\'"]', src, re.M)
}
guards = [
    n
    for n, body in bodies.items()
    if n.startswith("check_")
    and any(a in "\n".join(body) for a in aliases)
    and re.search(rf"\b{re.escape(n)}\b", registry)
]
runs_in_ci = any(
    line.strip() and not line.startswith("#") and "invariants-gate.py" in line
    for line in chain.read_text().splitlines()
)
sys.exit(0 if guards and runs_in_ci else 1)
PY
then
  pass "planning defect 2 guard 1: a marker-discovered test-only-grant confinement check is registered in invariants-gate.py, which is a live line of L0-CHAIN.txt"
else
  bad "planning defect 2 guard 1 MISSING: invariants-gate.py has no check confining the tenant"
  bad "  test-only overlay. The overlay grants create/update/patch on roles and clusterroles to a"
  bad "  fixture ServiceAccount so that Accept (e)'s sweep has something to be denied against. The"
  bad "  guard is the lint that refuses a reference to it from k8s-operator/scripts/, deploy/ or"
  bad "  config/ — i.e. the thing that keeps it out of an install path. Until it exists, the overlay"
  bad "  is one careless kustomization away from being real RBAC on a real cluster. P9-T9b-5."
fi

# ==== H. The ratchet — full prior regression ========================================================
echo; echo "== H. 09 §10 ratchet — verify-phase8.sh (=> phases 2–7, chaos C1–C4, 03 §11, goldens, go test) =="
if p1_gated H "an operator build nobody can name"; then
  if bash dev/verify/verify-phase8.sh "$DEV_CTX" >/tmp/p9-regress.log 2>&1; then
    pass "verify-phase8.sh green — the whole prior ratchet survived Phase 9"
    grep -E 'ALL CHECKS PASSED|verify-phase[2345678]\.sh green|chaos suite green|go test' /tmp/p9-regress.log \
      | tail -8 | sed 's/^/    /'
  else
    bad "verify-phase8.sh FAILED — Phase 9 regressed a prior phase (HALT). Log: /tmp/p9-regress.log"
    grep -E '^FAIL' /tmp/p9-regress.log | head -20
  fi
fi

# ==== I. Deferrals (recorded; never asserted green) =================================================
echo; echo "== I. Deferred-not-faked (recorded; never asserted green) =="
defer "V-CTN-020's live-WI half at L3 and V-CMP-003's Config Connector slice — inherited from Phase 8,"
echo "           unchanged by Phase 9, and printed by verify-phase8.sh in section H with their own"
echo "           blockers. Repeated here only so a reader of THIS output is not told a shorter story."
defer "P9-T7d-4 — the broker's egress to the API server has no NetworkPolicy of its own. This is a"
echo "           finding, not a deferral with an external blocker: it is unscheduled work, and it is"
echo "           recorded in the ledger as such. Named here because a reader of a green Phase-9 gate"
echo "           would otherwise reasonably conclude the broker's network surface had been examined."

echo
echo "===================================================================="
if [ "$fail" -eq 0 ]; then
  echo " Phase 9 verification: ALL CHECKS PASSED"
else
  echo " Phase 9 verification: FAILURES ABOVE (see HALT conditions)"
fi
echo "===================================================================="
exit "$fail"
