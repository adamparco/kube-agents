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
#   J. THIS phase's ratchet, derived rather than remembered ---------------------------------------
#      Sections B–F run the Accept list. Section H runs the PRIOR ratchet. Until 2026-07-31 nothing
#      ran Phase 9's OWN ratchet, and 23 of its 75 required check IDs — 8 BLOCKING-ALWAYS — had never
#      been run at all while every section above stayed green. This is planning defect 4, whose
#      declared resolution ("verify-phase9.sh runs the ratchet, not the Accept list") went into the
#      acceptance table and never into this script. Section J is that resolution, and it derives the
#      required set from 09 §10 + the phase file rather than from a list in here, because a list in
#      here is one more place to forget. It is RED today by construction, exactly as B, E, F and G
#      are, and its redness IS the worklist. See dev/tests/phase-ratchet-is-asserted.py.
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
# 57 is the count today, not a round number and not a floor with slack in it. A floor below the real
# count tolerates exactly the change it exists to notice — L2_CHAIN_FLOOR spent three phases at 6
# against a 14-line chain for want of this sentence. Raise it in the same commit that adds a line;
# lower it only in the commit that argues a line out (V-MET-014).
#
# IT WAS 43 AGAINST A 56-LINE CHAIN UNTIL 2026-07-31, which is thirteen lines of slack and the exact
# failure the sentence above describes, arriving in the file that describes it. The rule is prose,
# and prose on the artifact is not a mechanization ([[LSN-019]]) — a lint that derives this floor
# rather than remembering it is queued for the improvement pass.
if [ "$l0_n" -lt 57 ]; then
  bad "L0-CHAIN.txt yielded only $l0_n runnable lines; there were 57 when this gate was written. The"
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
# THIS ARM WAS A FALSE PASS UNTIL 2026-07-31, for the third time in this file and by the third
# route. It discovered the claimant with `grep -l 'V-BRK-021' dev/verify/*-l2.sh | head -1`, and the
# tree's one match was broker-refuse-l2.sh — in a comment saying it does NOT carry the property
# ("→ P9-T9b-5b-ii-b, with V-BRK-021's L2 surface scan"). That file is a live L2-CHAIN.txt line, so
# both halves of the old test were satisfied by a note recording the absence of the thing under
# test. Same shape as the Accept (d) arm above and the guard-1 arm below: a detector aimed at a NAME
# is indistinguishable, in its own output, from a detector that is satisfied.
#
# Retargeted to discover by the PROPERTY, which for a surface scan is the refusal vocabulary the
# SHIPPED server answers with. A claimant is a suite that:
#   - is a live line of dev/L2-CHAIN.txt, so the live tree runs it against a deployed broker, AND is
#     also run by a live line of dev/L0-CHAIN.txt under a FLAG rather than a cluster context, so the
#     ¬ tree runs on every PR. Both, because a check split off from its implementation has two trees
#     to be green on, and a scan with no committed ¬ row is a scan whose own arms have never been
#     shown to fail. The L0 clause matches the SHAPE of the invocation — an `-l2.sh` reached from the
#     no-cluster chain can only be running its control mode, since every other path takes a context —
#     rather than the control flag's spelling, which is a convention and not the property;
#   - has ONE function whose body scans all four vocabularies at once — unknown path, wrong method,
#     query parameter, bypass header. One function and not four, because "the suite mentions 404
#     somewhere" is true of every suite that has ever seen a 404;
#   - reads both port outcomes in that same body, so a scan that can only ever report "closed" —
#     which is also what a scan pointed at nothing reports — does not count; and
#   - compares a COUNT against a floor, which is the line between scanning a surface and spot-
#     checking the one route somebody remembered. Bounded, and stated as such: a regex over one
#     function body cannot attribute a floor to a dimension, so this clause says the scan bounds its
#     own size SOMEWHERE, not that each of the five dimensions carries a floor of its own. The suite
#     carries five; keeping them five is that suite's own arms, not this gate's.
#
# Every one of the four reason strings is RESOLVED out of k8s-operator/internal/broker/*.go from the
# code path that emits it, never spelled here — the guard-1 arm's constant-resolution trick, applied
# to four. Rename a reason in the server without renaming it in the suite and this arm fails, rather
# than quietly unhooking and going green on a suite that now matches nothing.
#
# The verdict travels through a file rather than `$(...)` because /bin/bash 3.2 — what macOS ships
# and what this script is run under — mis-parses a heredoc nested inside a command substitution and
# reports the whole file as an unterminated quote. The guard-1 arm below uses `if python3 - <<PY`
# for the same reason; this one needs a sentence out of the detector as well as its verdict, so the
# sentence goes to a file and the verdict stays in the exit status.
B21_OUT=/tmp/p9-b21-verdict.txt
if python3 - >"$B21_OUT" 2>&1 <<'PY'
import pathlib, re, sys


def die(msg):
    print(msg)
    sys.exit(1)


root = pathlib.Path(".")
l2p, l0p = root / "dev/L2-CHAIN.txt", root / "dev/L0-CHAIN.txt"
srvp = root / "k8s-operator/internal/broker/server.go"
envp = root / "k8s-operator/internal/broker/envelope.go"
for p in (l2p, l0p, srvp, envp):
    if not p.exists():
        die(f"cannot resolve the property — {p} is missing, so nothing here was checked")

srv, envsrc = srvp.read_text(), envp.read_text()

# What the shipped server answers, per code path. Not a list of strings this gate believes in.
PATHS = {
    "an unknown path": (srv, r"http\.StatusNotFound,\s*Response\{\s*Reason:\s*\"([^\"]+)\""),
    "a wrong method": (srv, r"http\.StatusMethodNotAllowed,\s*Response\{\s*Reason:\s*\"([^\"]+)\""),
    "a query parameter": (srv, r"len\(r\.URL\.Query\(\)\)\s*>\s*0.*?Reason:\s*\"([^\"]+)\""),
    "a bypass header": (envsrc, r"ReasonBypassKey\s*=\s*\"([^\"]+)\""),
}
vocab = {}
for what, (src, pat) in PATHS.items():
    m = re.search(pat, src, re.S)
    if not m:
        die(
            f"cannot resolve what the shipped server answers for {what}, so this arm would be "
            "scanning a vocabulary of its own invention"
        )
    vocab[what] = m.group(1)

# The bypass rejection has to run AHEAD of the mux or the scan's unauthenticated-route evidence is
# a property of one handler rather than of the server.
if not re.search(
    r"func \(s \*Server\) ServeHTTP.*?rejectBypassHeaders\(r\).*?s\.mux\.ServeHTTP", srv, re.S
):
    die(
        "server.go no longer rejects bypass headers ahead of the mux, so a refusal on an "
        "unauthenticated route no longer attributes to the server"
    )


def live(p):
    return [ln.strip() for ln in p.read_text().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


l2_lines, l0_lines = live(l2p), live(l0p)
FUNC = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{")

claimant = None
for suite in sorted(root.glob("dev/verify/*-l2.sh")):
    rel = suite.as_posix()
    if not any(rel in ln for ln in l2_lines):
        continue
    text = suite.read_text()
    lines = text.splitlines()
    bodies, name, buf = {}, None, []
    for line in lines:
        m = FUNC.match(line)
        if m:
            name, buf = m.group(1), []
        elif name is not None and line.startswith("}"):
            bodies[name] = "\n".join(buf)
            name = None
        elif name is not None:
            buf.append(line)
    for fn, body in bodies.items():
        if not all(f'"{v}"' in body for v in vocab.values()):
            continue
        if "port-open" not in body or "port-closed" not in body:
            continue
        if not re.search(r"\-lt|\-ne", body):
            continue
        # A function nothing calls checks nothing (the CHECKS-table clause of the guard-1 arm).
        called = [
            ln
            for ln in lines
            if re.search(rf"\b{re.escape(fn)}\b", ln)
            and not ln.lstrip().startswith("#")
            and not FUNC.match(ln)
        ]
        if not called:
            continue
        claimant = (rel, fn)
        break
    if claimant:
        break

if not claimant:
    die(
        "no live line of dev/L2-CHAIN.txt runs a suite that scans the deployed surface — one "
        "function answering for all of "
        + ", ".join(f"{w} ({v})" for w, v in vocab.items())
        + ", both port outcomes, and a count against a floor"
    )

rel, fn = claimant
if not any(re.search(rf"{re.escape(rel)}\s+--\S+", ln) for ln in l0_lines):
    die(
        f"{rel} carries the scan in {fn}() and runs at L2, but no live line of dev/L0-CHAIN.txt "
        "runs it under a flag, so the control tree that shows those arms can fail runs nowhere"
    )
print(f"{rel}:{fn}()")
PY
then
  pass "V-BRK-021 L2: $(tail -1 "$B21_OUT") scans the deployed surface in the vocabulary server.go emits, runs from dev/L2-CHAIN.txt, and its ¬ runs from dev/L0-CHAIN.txt (L0 half green since 2026-07-30)"
else
  # A traceback lands here too, through the 2>&1 — a detector that crashed is a detector that
  # measured nothing, and it must read as such rather than as a missing artifact.
  bad "V-BRK-021 L2 UNPROVEN: $(tail -1 "$B21_OUT")"
  bad "  Its L0 half is green (results.csv row 138, P9-T7c-2c) and 09 §6 requires L0 AND L2. The L2"
  bad "  half is the clause the L0 half cannot reach: debug routes, override query params and the"
  bad "  ten X-Kube-Agents-* bypass headers all 404/405 against a RUNNING broker, one reachable"
  bad "  port on the pod, and no build-tag-guarded skip path in the image the controller handed"
  bad "  out — a skip path compiled out of \`go test\` is invisible to any source scan. V-BRK is"
  bad "  BLOCKING-ALWAYS and may not be deferred (09 §9.6). P9-T9b-5b-ii-b-2."
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

# ==== J. This phase's own ratchet, derived from 09 §10 ==============================================
echo; echo "== J. 09 §10 phase-9 ratchet — every required check ID has a green results row (09 §9.4) =="
if python3 dev/tests/phase-ratchet-is-asserted.py --phase 9 >/tmp/p9-ratchet.log 2>&1; then
  pass "$(tail -3 /tmp/p9-ratchet.log | tr -d '\n')"
else
  bad "the Phase 9 ratchet is not asserted — required check IDs have no green row in"
  bad "  verification/results.csv. This is the section that would have caught planning defect 4;"
  bad "  the list below is the worklist, not a formatting problem. A BLOCKING-ALWAYS member may not"
  bad "  be deferred to close the phase (09 §9.6)."
  sed 's/^/    /' /tmp/p9-ratchet.log
fi

# ==== K. V-MET-002 — full coverage of the load-bearing suites =======================================
# Until 2026-07-31 this was the ONLY place V-MET-002 ran: red by construction until the 09 §6
# catalog grew the rows asserting the published remainder, and a required PR check that stays red
# for a phase reddens every unrelated commit. The remainder is now zero, the live arm is a line on
# dev/L0-CHAIN.txt, and `invariants-gate.py`'s arm that protected this section retired in the same
# commit that moved the line.
#
# The section STAYS, and not out of sentiment. Section J above reports V-MET-002 as green or not
# from the results file; this section is what says WHICH obligations are uncovered, by ID and in
# their own words, so a red is legible without opening three artifacts. That was worth having when
# the number was sixteen and it will be worth having the next time it is not zero -- 09 §8.1's
# draw-down does not end at Phase 9, it ends at every phase that adds an owned section.
echo; echo "== K. V-MET-002 — every load-bearing-owned requirement maps to >=1 check (09 §8) =="
if python3 dev/tests/load-bearing-coverage-is-full.py >/tmp/p9-vmet002.log 2>&1; then
  pass "$(tail -1 /tmp/p9-vmet002.log)"
else
  bad "V-MET-002 is red — obligations owned by V-CTN/V-BRK/V-REV/V-ISO/V-ADV map to no check."
  bad "  09 §8.1 dates this to 'before Phase 10 grants the first write credential', so it is the"
  bad "  last of the draw-down and not a deferral: a BLOCKING-ALWAYS check may not be deferred at"
  bad "  all (09 §9.6). Close each by mapping an honest catalog row, or by adding one to 09 §6."
  sed 's/^/    /' /tmp/p9-vmet002.log
fi

echo
echo "===================================================================="
if [ "$fail" -eq 0 ]; then
  echo " Phase 9 verification: ALL CHECKS PASSED"
else
  echo " Phase 9 verification: FAILURES ABOVE (see HALT conditions)"
fi
echo "===================================================================="
exit "$fail"
