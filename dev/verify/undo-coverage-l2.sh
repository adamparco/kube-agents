#!/usr/bin/env bash
# V-REV-001 at L2, at POPULATION SCALE — thirty-seven envelopes derived from the classifier corpus,
# submitted to a DEPLOYED broker by a real caller, and every ActionRecord the journal holds
# afterwards (09 §6.3, 03 §11, 06 §4.3).
#
# V-REV-001 IS A PERCENTAGE, AND UNTIL THIS FILE EXISTED THE TREE HAD n=1.
#   "100% of executed non-gated ActionRecords carry a validated undo plan." `broker-execute-l2.sh`
#   proves the property for ONE record — the first record in the tree written by a broker rather
#   than by a test — and says in its own words that one record carrying a validated plan is not a
#   population claim, leaving the denominator to this unit. 09 §11.11 keeps V-REV-001 (coverage) and
#   V-REV-002 (correctness) apart "precisely because the first is cheap and reassuring and the
#   second is the one that matters"; cheap and reassuring is exactly what an n=1 coverage row is,
#   and a coverage row is worth something only once there is a population under it.
#
# WHERE THE POPULATION COMES FROM
#   `dev/verify/fixtures/soak_corpus.py`, which derives it from
#   `verification/fixtures/classifier-corpus.yaml` filtered by `actor-tenant-write-grant.yaml` — the
#   cases this identity is actually authorized to attempt. Derived rather than hand-written because
#   a second hand-written population would be a second copy of one decision ([[LSN-031]]), and it
#   would drift from the corpus the classifier suites score. That file self-tests on the L0 chain
#   and refuses to shrink silently: a closed set of rejection reasons, `selected + rejected ==
#   total`, and floors on size, verb count and kind count.
#
# WHAT IS ASSERTED, in order:
#   A-1  THE SOAK RAN. At least `ACCEPT_FLOOR` of the submitted envelopes were ACCEPTED — answered
#        with an actionId. Below the floor this is DEFERRED, not failed: a broker that refused
#        everything has told us nothing about undo coverage, and reporting that as a V-REV-001 red
#        would file a transport or admission problem under a reversibility row.
#   A-2  EVERY ACCEPTED ACTION WAS JOURNALED. For each actionId the broker returned there is an
#        `ActionRecord` at `ar-<lowercase actionId>` (`journal.RecordName`, 06 §4.3), read from the
#        API server. A reply naming a journal entry that was never written is a defect no per-record
#        assertion below would ever reach, because the record it would assert on does not exist.
#   A-3  V-REV-001 ITSELF, over the EXECUTED NON-GATED subset. `status.timestamps.executionStarted`
#        is non-empty (06 §4.1: the broker stamps it when it issues the first mutating call — that
#        is what "executed" means, and a dry-run apply IS one) and `spec.classification.class` is
#        one the broker chose and is not gated. Every such record must carry `spec.undo.strategy`
#        non-empty, `spec.undo.validated` true, and — for a strategy other than `none` — at least
#        one step. `none` on a NON-GATED record is itself a failure: `undo/strategy.go`'s table ends
#        "anything else ⇒ none ⇒ gated", so a non-gated record whose plan is `none` is two
#        components disagreeing about the same action.
#   A-4  THE PERCENTAGE IS NOT VACUOUS. 100% of two records is a number, not evidence. The judged
#        population must reach `POP_FLOOR` and span at least `POP_MIN_VERBS` distinct verbs. Below
#        either, DEFERRED — the property could not be run at scale, and a check that could not run
#        its property is never a pass.
#   A-5  THE DENOMINATOR IS THIS RUN'S. The tenant and journal namespaces are REUSED across runs and
#        never deleted ([[LSN-045]]), so records from every previous soak are sitting next to these.
#        Every record in the population must be one this run's own submissions named. Mining the
#        namespace instead would count a green record from last week as evidence about this commit,
#        and the count would go UP each run, which reads like coverage improving.
#   A-6  THE SHADOW HELD, across the whole population. Every `absent`-seeded target is still absent,
#        and every `present`-seeded target still exists with the SAME `metadata.resourceVersion` it
#        had before the run. Thirty-seven dry-run writes, nothing written. `broker-execute-l2.sh`
#        can only ask this of one absent ConfigMap; a `delete` and a `scale` in shadow are the two
#        operations whose escape would be least visible, and both are in here.
#
# WHAT THIS DOES NOT CLAIM, and where each went instead
#   THAT THE BROKER CLASSIFIED ANY CASE CORRECTLY. The corpus's `class` column is carried into the
#     transcript as `expectClass` and is never compared to the record. The soak partitions on the
#     class the BROKER chose, and that is deliberate: a soak that filtered on the expected class
#     would score V-REV-001 over a population the classifier never agreed to, and would go green on
#     a broker that gated all thirty-seven. Class fidelity is V-CLS's row, scored at L1 against the
#     same corpus by `dev/verify/classifier-corpus.sh`.
#   V-REV-002 (`undo <id>` restores prior state). Requires executing an undo. Phase 9 has no write
#     authority anywhere (07 §2) and this suite executes nothing.
#   V-REV-003 (no generatable undo plan ⇒ reclassified gated). The NEGATIVE of A-3, and it needs a
#     population whose inverses do NOT exist. This population is built so that every case has one.
#     → P9-T9b-5b-ii.
#   V-BRK-006 (write-ahead ordering). Proven at n=1 by `broker-execute-l2.sh`, which owns the row.
#     Re-asserting it over thirty-seven records here would be a second copy of one property with no
#     new information, and would make this file's failure ambiguous between two rows.
#   THE PHASE LIFECYCLE. `broker-execute-l2.sh` reads the served CRD's enum and judges the phase
#     word. One record is enough for that property and this file does not repeat it.
#
# DESTRUCTIVE-TEST GUARD: scratch-GKE contexts only, anchored. This deletes and re-applies the Agent
# CR `platform-agent` in `kubeagents-system`, grants the actor identity WRITE authority over a
# throwaway tenant namespace, seeds three dozen objects into it, runs a pod, and submits thirty-seven
# actions to a live broker. On the live install that is a test deleting the fleet's own agent and
# widening a production identity.
# Exit: 0 = PROVEN · 1 = FAILED · 2 = refused target / P10 · 3 = DEFERRED (P1 or the run itself).
# Usage: dev/verify/undo-coverage-l2.sh [kube-context]
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions):
#   P1 image-under-test: the controller (`control-plane=controller-manager`) AND this agent's BROKER
#      pod (`kube-agents/agent=<agent>,kube-agents/role=actor`) — both, and in full. Every arm here
#      is a claim about what a BINARY did: the classifier that chose each class, the planner that
#      generated each undo plan, the executor that stamped each execution. A broker one generation
#      behind the tree would answer all six arms about the previous build's pipeline, in green.
#      Unverifiable → rc 3.
#   P3 admission-recreate: the Agent CR is deleted with `--wait=true` and re-applied on every run,
#      so the broker Deployment, its mesh Certificates and the pair NetworkPolicies are rendered by
#      the controller running NOW. The broker pod is resolved through `p3_pod_of_deploy`, by
#      ownership. The soak's TARGET OBJECTS are re-seeded every run and their pre-state is captured
#      after seeding, so A-6 compares this run's before against this run's after. The ActionRecords
#      are deliberately NOT recreated — they are the output, they cannot be deleted ([[LSN-045]]),
#      and A-5 exists precisely because they accumulate.
#   P6 runtime-authoritative: every assertion reads `ActionRecord` objects and target objects from
#      the API server. Nothing is read from the broker's replies except the actionId that names
#      where to look, and A-2 exists to catch a reply that named nothing. The driver's environment —
#      endpoint, SAN, identity, token path, TLS dir — comes off the RENDERED agent Deployment
#      through `broker_driver_env`.
#   P9 polled-not-slept: the record mine polls to a deadline rather than sleeping a fixed interval,
#      because thirty-seven journal writes do not all land at the same moment and a fixed wait is
#      how "the broker is slow" becomes "the broker did not journal".
set -uo pipefail

# MODES. `live` submits to a real broker and is what every claim above is about.
# `--negative-control` is the mandatory `¬` arm: it replays the assertion block against synthesized
# populations that a MISBEHAVING broker — or a MISBEHAVING SUITE — would have produced, and requires
# each to go red or to defer, by the arm that targets it.
MODE=live
if [ "${1:-}" = "--negative-control" ]; then
  MODE=negative-control
  shift
fi

CTX="${1:-gke-scratch-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
K="kubectl --context $CTX"

NS=kubeagents-system
AGENT=platform-agent
AGENT_MANIFEST=examples/gitops-repo/fleet/platform-agent.yaml

# Its OWN tenant namespace, not `broker-execute-l2.sh`'s. Two suites seeding three dozen objects
# into one namespace would race whenever both are in the same chain run, and A-6's "unchanged
# resourceVersion" would be measuring the other suite's writes. Created once and REUSED forever —
# never deleted, per [[LSN-045]], which is the condition A-5 is written for rather than against.
TENANT_NS=kubeagents-undo-soak-tenant

DRIVER_POD=undo-coverage-l2-driver
DRIVER_CM=undo-coverage-l2-code
UNTRUSTED_SECRET=undo-coverage-l2-untrusted
PROBE=dev/verify/fixtures/undo_coverage_probe.py
CORPUS_TOOL=dev/verify/fixtures/soak_corpus.py

# The label every seeded target carries. Not used for attribution — A-5 attributes through the
# actionId set, because the broker writes the ActionRecords and no label of ours reaches them
# (`journal.Labels` writes tier, scope, risk-class, trigger, chain-id, status and undo-of, and
# nothing a submitter chose). It exists so a human can find and delete the seed objects by hand.
SEED_LABEL=kube-agents/soak-seed
SEED_LABEL_VALUE=undo-coverage-l2

# Thirty-seven envelopes are submitted one at a time, each fetching its own nonce. The library
# default of 300s is `broker_probe.py`'s ten-scenario budget and is not this one's.
BROKER_DRIVER_TIMEOUT=900
export BROKER_DRIVER_TIMEOUT

# FLOORS. Deliberately below the corpus's own `MIN_SELECTED = 20` in neither case: that constant is
# the floor on what may be SUBMITTED and is enforced where the population is derived. These are
# floors on what survived the round trip, and they are the difference between "V-REV-001 holds at
# scale" and "V-REV-001 holds for the four records that made it".
ACCEPT_FLOOR=20 # accepted submissions, below which nothing here is measurable
POP_FLOOR=20    # executed non-gated records, below which 100% is a number and not evidence
POP_MIN_VERBS=2 # distinct verbs in that population

# The non-gated classes, space-padded for a substring test. `routine` and `elevated` are 03 §5's
# non-gated pair; `gated` and `forbidden` are the other two, and a record in either is outside
# V-REV-001's population BY THE ROW'S OWN WORDING, not by an exclusion this file invented.
NON_GATED_PAD=" routine elevated "

# NEGATIVE CONTROL DOES NOT EXERCISE: ([[LSN-060]].) The control SYNTHESIZES both tables — the
# per-record population and the per-target world — and hands them straight to the assertion block,
# so everything upstream of the assertions is unmeasured by it:
#   - the corpus derivation, its base64 transport, and the envelope construction in the probe. A
#     synthesized POP row is not a broker's answer; the ¬ arm cannot tell a running broker from an
#     absent one, nor a corpus of thirty-seven from a corpus of zero
#   - the SEEDING and the two bulk reads that produce `before` and `after`. The control mutates the
#     world table to claim an object changed; it never asks the cluster whether one did. A seeding
#     step that silently created nothing would leave every row `absent/absent` and A-6 would pass
#     live while the ¬ arm stayed 16/16
#   - the record MINE — the poll, the `ar-<lowercase>` name derivation, and the JSON field
#     extraction that fills columns 5..11. `broker-execute-l2.sh` shipped a lookup that could not
#     have found a record against any commit, green in its ¬ arm throughout, for exactly this reason
#   - the P1 digest arms and the broker's Availability, which run before either mode
# What it does prove, and all it proves: the assertion block distinguishes a good population from a
# bad one, and each of the sixteen defects is caught or deferred by the arm that targets it, named
# in the output.
fail=0
deferrals=0

# EVERY ARM IS COUNTED, AND THE COUNT IS ASSERTED AT THE END, for the reason `broker-auth-l2.sh`
# gives at length: `fail` stays 0 when no assertion runs, and a suite that asserted nothing prints
# a PROVEN banner. Change EXPECTED_ASSERTIONS deliberately, in the same commit as the arm.
assertions=0
pass() {
  assertions=$((assertions + 1))
  echo "PASS: $1"
}
bad() {
  assertions=$((assertions + 1))
  echo "FAIL: $1"
  fail=1
}
# A third verdict, and it is NOT a soft pass. An arm that could not run its property reports
# `deferred` with a named blocker (PROTOCOL §5); the run's exit code becomes 3 and no `results.csv`
# row is written from it. It increments `assertions` because the arm DID run — it ran and answered
# "not measurable", which is a different thing from never having executed.
defer() {
  assertions=$((assertions + 1))
  deferrals=$((deferrals + 1))
  echo "DEFER: $1"
}

# 2 x P1 + broker Available + A-1 .. A-6.
EXPECTED_ASSERTIONS=9

# ------------------------------------------------------------------------------------------------
# The assertion block, over two tables. A function because `--negative-control` replays exactly
# these arms against tables nobody's broker produced.
#
# $POP   one line per ACCEPTED submission, tab-separated:
#          1 cid  2 verb  3 actionId  4 found(yes|no)  5 phase  6 class  7 dryRun
#          8 strategy  9 validated  10 steps  11 executionStarted
# $WORLD one line per corpus row, tab-separated:
#          1 cid  2 verb  3 seed(present|absent)  4 before  5 after
#          where `before`/`after` are a resourceVersion, the word `absent`, or the word `unknown`
# $RUN_IDS          newline-separated actionIds this run collected — the authoritative denominator
# $SUBMITTED        corpus rows the probe was given
# $ACCEPTED         submissions the broker answered with an actionId
# $NS_RECORD_TOTAL  ActionRecords sitting in $RECORD_NS, this run's and every previous run's
# $RECORD_NS        where they are, for the message
#
# Everything is derived INSIDE, so the `¬` arm exercises the derivation rather than a simplified
# copy of it that could agree with a broken suite for a reason the live path would not.
# ------------------------------------------------------------------------------------------------
assert_population() {
  local summary exec_n n ok gated unclassified nostrat none unval stepless verbs cats
  local missing miss_list foreign foreign_list
  local w_created w_mutated w_deleted w_unreadable w_ok

  # ----------------------------------------------------------------------------------------------
  # A-1: the soak ran at all
  # ----------------------------------------------------------------------------------------------
  echo
  echo "== A-1: the broker accepted enough of the population to measure anything =="
  if [ "$ACCEPTED" -lt "$ACCEPT_FLOOR" ]; then
    defer "SUBMISSION FLOOR: only $ACCEPTED of $SUBMITTED envelopes were accepted, and the floor is $ACCEPT_FLOOR. Nothing below is a statement about undo coverage — a broker that refused the population has told us about admission, transport or the envelope builder, and filing that under V-REV-001 would put a red on the wrong row."
  else
    pass "A-1: $ACCEPTED of $SUBMITTED envelopes accepted (floor $ACCEPT_FLOOR) — there is a population to judge"
  fi

  # ----------------------------------------------------------------------------------------------
  # A-2: every accepted action was journaled
  # ----------------------------------------------------------------------------------------------
  echo
  echo "== A-2: every accepted actionId names an ActionRecord the API server holds =="
  missing="$(printf '%s\n' "$POP" | awk -F'\t' 'NF>=4 && $4!="yes" { n++ } END { print n+0 }')"
  miss_list="$(printf '%s\n' "$POP" | awk -F'\t' 'NF>=4 && $4!="yes" { printf "%s/%s ", $1, $3 }')"
  if [ "$missing" -gt 0 ]; then
    bad "A-2 MINING MISS: $missing of $ACCEPTED accepted actions have no ActionRecord at ar-<lowercase actionId> in $RECORD_NS. The broker answered with a journal entry that was never written: $miss_list"
  else
    pass "A-2: all $ACCEPTED accepted actions were journaled — every record read from the API server, none inferred from a reply body"
  fi

  # ----------------------------------------------------------------------------------------------
  # A-3 / A-4: V-REV-001, and whether its percentage means anything
  # ----------------------------------------------------------------------------------------------
  summary="$(printf '%s\n' "$POP" | awk -F'\t' -v ng="$NON_GATED_PAD" '
    NF >= 11 && $4 == "yes" && $11 != "" {
      exec_n++
      if ($6 == "")                     { unclassified++; next }
      if (index(ng, " " $6 " ") == 0)   { gated++;        next }
      n++; verbs[$2] = 1
      if ($8 == "")                     { nostrat++;  next }
      if ($8 == "none")                 { none++;     next }
      if ($9 != "true")                 { unval++;    next }
      if ($10 + 0 < 1)                  { stepless++; next }
      ok++
    }
    END {
      nv = 0
      for (v in verbs) nv++
      printf "%d %d %d %d %d %d %d %d %d %d\n",
        exec_n+0, n+0, ok+0, gated+0, unclassified+0, nostrat+0, none+0, unval+0, stepless+0, nv
    }
  ')"
  read -r exec_n n ok gated unclassified nostrat none unval stepless verbs <<<"$summary"

  echo
  echo "== A-3: V-REV-001 — every executed non-gated record carries a validated undo plan =="
  echo "  executed: $exec_n · non-gated: $n · gated (outside the row's population): $gated · distinct verbs: $verbs"
  cats=""
  [ "$nostrat" -gt 0 ] && cats="$cats no-undo-plan($nostrat)"
  [ "$none" -gt 0 ] && cats="$cats strategy-none-on-non-gated($none)"
  [ "$unval" -gt 0 ] && cats="$cats unvalidated-plan($unval)"
  [ "$stepless" -gt 0 ] && cats="$cats stepless-plan($stepless)"
  [ "$unclassified" -gt 0 ] && cats="$cats unclassified-executed-record($unclassified)"
  if [ -n "$cats" ]; then
    bad "V-REV-001 FAILED:$cats — of $exec_n executed records, $ok carry a usable plan. A record with no strategy was journaled with no undo plan at all; 'none' on a NON-GATED record is undo/strategy.go's table and the classifier disagreeing about one action; an unvalidated plan is one nobody checked against the API server; a non-'none' strategy with zero steps restores nothing; and an executed record with no class cannot be shown to be outside this population, so it is not silently dropped from it."
  elif [ "$n" -eq 0 ]; then
    defer "EMPTY EXECUTED POPULATION: $exec_n record(s) executed and none of them is non-gated ($gated gated). 100% of nothing is not a coverage result — V-REV-001 could not be run, so it is deferred rather than scored."
  else
    pass "V-REV-001: $ok/$n = 100% of executed non-gated records carry a validated undo plan, on classes the BROKER chose"
  fi

  echo
  echo "== A-4: the percentage is not vacuous =="
  if [ "$n" -lt "$POP_FLOOR" ]; then
    defer "NON-VACUITY FLOOR: the judged population is $n record(s) and the floor is $POP_FLOOR. A-3's percentage is arithmetic over a handful; V-REV-001 at scale could not be run."
  elif [ "$verbs" -lt "$POP_MIN_VERBS" ]; then
    defer "ONE VERB: the judged population of $n record(s) spans $verbs distinct verb(s) and the floor is $POP_MIN_VERBS. 06 §4.3.1 is a table of six rows, and a soak that exercised one of them has measured one row."
  else
    pass "A-4: $n record(s) across $verbs verb(s) — above the $POP_FLOOR/$POP_MIN_VERBS floors, so A-3's 100% is over a population"
  fi

  # ----------------------------------------------------------------------------------------------
  # A-5: the denominator is this run's — guard 3 against a namespace nobody may delete
  # ----------------------------------------------------------------------------------------------
  echo
  echo "== A-5: every record in the population belongs to THIS run =="
  foreign="$(awk -F'\t' 'NR==FNR { if ($1 != "") ids[$1] = 1; next }
                         NF >= 3 && $3 != "" && !($3 in ids) { c++ }
                         END { print c+0 }' \
    <(printf '%s\n' "$RUN_IDS") <(printf '%s\n' "$POP"))"
  foreign_list="$(awk -F'\t' 'NR==FNR { if ($1 != "") ids[$1] = 1; next }
                              NF >= 3 && $3 != "" && !($3 in ids) { printf "%s/%s ", $1, $3 }' \
    <(printf '%s\n' "$RUN_IDS") <(printf '%s\n' "$POP"))"
  if [ "$foreign" -gt 0 ]; then
    bad "A-5 FOREIGN RECORD IN THE POPULATION: $foreign of $ACCEPTED population rows carry an actionId this run never received: $foreign_list. $RECORD_NS is never deleted ([[LSN-045]]) and holds $NS_RECORD_TOTAL records from every soak that ever ran; a denominator built by listing it would grow every run and read like coverage improving."
  elif [ "$ACCEPTED" -gt 0 ] && [ -z "$(printf '%s' "$RUN_IDS" | tr -d '[:space:]')" ]; then
    bad "A-5 FOREIGN RECORD IN THE POPULATION: the population has $ACCEPTED row(s) and this run collected no actionIds at all, so nothing in it can be attributed. An unattributable population is not this run's by default."
  else
    pass "A-5: all $ACCEPTED population rows are attributed to actionIds this run received; $RECORD_NS holds $NS_RECORD_TOTAL record(s) in total and the other $((NS_RECORD_TOTAL - ACCEPTED)) are outside the denominator"
  fi

  # ----------------------------------------------------------------------------------------------
  # A-6: the shadow held, over every target
  # ----------------------------------------------------------------------------------------------
  echo
  echo "== A-6: thirty-odd dry-run writes, and the cluster is where it was =="
  summary="$(printf '%s\n' "$WORLD" | awk -F'\t' '
    NF >= 5 {
      if ($4 == "unknown" || $5 == "unknown") { unreadable++; next }
      if ($3 == "absent") {
        if ($5 != "absent") { created++; next }
        ok++; next
      }
      if ($4 == "absent")  { unreadable++; next }
      if ($5 == "absent")  { deleted++;    next }
      if ($4 != $5)        { mutated++;    next }
      ok++
    }
    END { printf "%d %d %d %d %d\n", created+0, mutated+0, deleted+0, unreadable+0, ok+0 }
  ')"
  read -r w_created w_mutated w_deleted w_unreadable w_ok <<<"$summary"
  cats=""
  [ "$w_created" -gt 0 ] && cats="$cats SHADOW CREATED AN OBJECT($w_created)"
  [ "$w_mutated" -gt 0 ] && cats="$cats SHADOW MUTATED AN OBJECT($w_mutated)"
  [ "$w_deleted" -gt 0 ] && cats="$cats SHADOW DELETED AN OBJECT($w_deleted)"
  [ "$w_unreadable" -gt 0 ] && cats="$cats PRE-STATE UNREADABLE($w_unreadable)"
  if [ -n "$cats" ]; then
    bad "A-6:$cats — $w_ok target(s) held. The first three are live safety defects and not reporting ones: Phase 9 grants the broker no write authority anywhere (07 §2) and every envelope asked for a dry run. The fourth is not a pass by default — a target whose before-state could not be read cannot be shown to be unchanged, and scoring it green is how an object nobody looked at becomes evidence that nothing was written."
  else
    pass "A-6: all $w_ok seeded target(s) are exactly as the run found them — absent ones still absent, present ones at the same metadata.resourceVersion"
  fi
}

# ------------------------------------------------------------------------------------------------
# The `¬` arm
# ------------------------------------------------------------------------------------------------
# WHY SYNTHESIZED TABLES AND NOT A MUTATION. Every arm above reads records a broker had to build
# correctly to produce at all; there is no cheap way to make a REAL broker journal an unvalidated
# plan or leak a write out of a dry run. Those mutations are edits to the Go pipeline, which is
# `dev/mutate.py`'s job at L1 against a compiled binary, not something an L2 suite can stage against
# a deployed one. What this arm proves is the thing an L2 suite CAN get wrong on its own: that the
# assertion block distinguishes a good population from a bad one at all.
#
# EACH MUTANT MUST BE CAUGHT BY THE ARM THAT TARGETS IT ([[LSN-035]]). Every row carries a needle,
# and a row counts as caught only when a FAIL (or DEFER) line CONTAINS that needle. Without it,
# breaking the awk would "catch" every mutant at once by failing every arm on all of them, and the
# control would read 16/16 while asserting that the suite is broken.
#
# THE VERDICT IS READ OFF THE OUTPUT, NEVER OFF `$fail`. `assert_population` runs inside a command
# substitution, which is a subshell, so every `fail=1` and every `deferrals++` it sets dies with it.
# A ¬ arm that tested `$fail` here would score all sixteen the same way.
synth_pop() { # <good-rows> [extra-line...]
  local i verb
  for i in $(seq 1 "$1"); do
    case $((i % 3)) in
      1) verb="patch" ;;
      2) verb="apply" ;;
      *) verb="scale" ;;
    esac
    printf 'gat-%03d\t%s\tAID%03d\tyes\tDryRun\troutine\ttrue\trestore\ttrue\t2\t2026-07-31T10:00:00Z\n' "$i" "$verb" "$i"
  done
  shift
  local line
  for line in "$@"; do printf '%s\n' "$line"; done
}

synth_ids() { # <good-rows> [extra-id...]
  local i
  for i in $(seq 1 "$1"); do printf 'AID%03d\n' "$i"; done
  shift
  local x
  for x in "$@"; do printf '%s\n' "$x"; done
}

synth_world() { # <present-rows> [extra-line...]
  local i
  for i in $(seq 1 "$1"); do printf 'gat-%03d\tpatch\tpresent\trv%d\trv%d\n' "$i" "$i" "$i"; done
  # Three absent-seeded targets, so the baseline exercises BOTH branches of A-6 rather than only
  # the resourceVersion comparison.
  for i in 1 2 3; do printf 'cm-%03d\tapply\tabsent\tabsent\tabsent\n' "$i"; done
  shift
  local line
  for line in "$@"; do printf '%s\n' "$line"; done
}

run_negative_control() {
  local out rc=0 total=0 caught=0

  # `nc_case <name> <expect: green|red|defer> <needle>` — the tables are already in POP/WORLD/etc.
  nc_case() {
    local nm="$1" ex="$2" nd="$3"
    total=$((total + 1))
    out="$(assert_population 2>&1)"
    if [ "$ex" = green ]; then
      if ! printf '%s\n' "$out" | grep -qE '^(FAIL|DEFER):'; then
        echo "  ok   $nm — a correct population passes, so the arms below are not always-red"
        caught=$((caught + 1))
      else
        echo "  MISS $nm — a CORRECT population was failed or deferred; every mutant below would be caught for the wrong reason"
        printf '%s\n' "$out" | grep -E '^(FAIL|DEFER):' | sed 's/^/       /'
        rc=1
      fi
      return
    fi
    local prefix='^FAIL:'
    [ "$ex" = defer ] && prefix='^DEFER:'
    if printf '%s\n' "$out" | grep -E "$prefix" | grep -qF "$nd"; then
      echo "  ok   $nm — caught by the arm that targets it ('$nd')"
      caught=$((caught + 1))
    else
      echo "  MISS $nm — no ${prefix#^} line mentions '$nd', so the property it targets is not what caught it"
      printf '%s\n' "$out" | grep -E '^(FAIL|DEFER):' | sed 's/^/       /'
      rc=1
    fi
  }

  # The baseline every case below mutates one thing away from.
  RECORD_NS="$NS"
  SUBMITTED=37
  ACCEPTED=24
  NS_RECORD_TOTAL=130
  POP="$(synth_pop 24)"
  WORLD="$(synth_world 24)"
  RUN_IDS="$(synth_ids 24)"
  nc_case baseline green -

  # --- A-1 ----------------------------------------------------------------------------------------
  ACCEPTED=5
  POP="$(synth_pop 5)"
  RUN_IDS="$(synth_ids 5)"
  nc_case submission-floor defer "SUBMISSION FLOOR"

  # --- A-2 ----------------------------------------------------------------------------------------
  ACCEPTED=25
  POP="$(synth_pop 24 'gat-999	patch	AID999	no					')"
  RUN_IDS="$(synth_ids 24 AID999)"
  nc_case unjournaled-action red "MINING MISS"

  # --- A-3, one row per defect --------------------------------------------------------------------
  ACCEPTED=25
  RUN_IDS="$(synth_ids 24 AID999)"
  POP="$(synth_pop 24 'gat-999	patch	AID999	yes	DryRun	routine	true			0	2026-07-31T10:00:00Z')"
  nc_case no-undo-plan red "no-undo-plan"

  POP="$(synth_pop 24 'gat-999	patch	AID999	yes	DryRun	routine	true	none	true	0	2026-07-31T10:00:00Z')"
  nc_case gave-up-planning red "strategy-none-on-non-gated"

  POP="$(synth_pop 24 'gat-999	patch	AID999	yes	DryRun	routine	true	restore	false	2	2026-07-31T10:00:00Z')"
  nc_case unvalidated-plan red "unvalidated-plan"

  POP="$(synth_pop 24 'gat-999	patch	AID999	yes	DryRun	routine	true	restore	true	0	2026-07-31T10:00:00Z')"
  nc_case stepless-plan red "stepless-plan"

  POP="$(synth_pop 24 'gat-999	patch	AID999	yes	DryRun		true	restore	true	2	2026-07-31T10:00:00Z')"
  nc_case unclassified-executed-record red "unclassified-executed-record"

  # An executed population in which every record was GATED. Not a failure — gated records are
  # outside V-REV-001's population by the row's own wording — and not a pass either.
  ACCEPTED=24
  RUN_IDS="$(synth_ids 24)"
  POP="$(printf '%s\n' "$(synth_pop 24)" | awk -F'\t' 'BEGIN{OFS="\t"} { $6="gated"; print }')"
  nc_case every-record-gated defer "EMPTY EXECUTED POPULATION"

  # --- A-4 ----------------------------------------------------------------------------------------
  # Above A-1's floor and below A-4's: twenty-four accepted, of which sixteen were gated. A-3 is
  # green on the eight that remain, and green on eight is exactly what A-4 exists to refuse.
  POP="$(printf '%s\n' "$(synth_pop 24)" | awk -F'\t' 'BEGIN{OFS="\t"} NR>8 { $6="gated" } { print }')"
  nc_case below-vacuity-floor defer "NON-VACUITY FLOOR"

  POP="$(printf '%s\n' "$(synth_pop 24)" | awk -F'\t' 'BEGIN{OFS="\t"} { $2="patch"; print }')"
  nc_case single-verb-population defer "ONE VERB"

  # --- A-5 ----------------------------------------------------------------------------------------
  ACCEPTED=25
  POP="$(synth_pop 24 'gat-999	patch	AIDOLD	yes	DryRun	routine	true	restore	true	2	2026-07-31T10:00:00Z')"
  RUN_IDS="$(synth_ids 24)"
  nc_case record-from-an-earlier-run red "FOREIGN RECORD IN THE POPULATION"

  ACCEPTED=24
  POP="$(synth_pop 24)"
  RUN_IDS=""
  nc_case unattributable-population red "FOREIGN RECORD IN THE POPULATION"

  # --- A-6 ----------------------------------------------------------------------------------------
  ACCEPTED=24
  POP="$(synth_pop 24)"
  RUN_IDS="$(synth_ids 24)"
  WORLD="$(synth_world 24 'cm-999	apply	absent	absent	rv7')"
  nc_case shadow-created-an-object red "SHADOW CREATED AN OBJECT"

  WORLD="$(synth_world 24 'gat-999	patch	present	rv1	rv2')"
  nc_case shadow-mutated-an-object red "SHADOW MUTATED AN OBJECT"

  WORLD="$(synth_world 24 'gat-999	delete	present	rv1	absent')"
  nc_case shadow-deleted-an-object red "SHADOW DELETED AN OBJECT"

  WORLD="$(synth_world 24 'gat-999	patch	present	unknown	rv2')"
  nc_case pre-state-unreadable red "PRE-STATE UNREADABLE"

  echo
  echo "negative control: $caught/$total"
  return $rc
}

if [ "$MODE" = negative-control ]; then
  echo "== undo-coverage-l2.sh --negative-control: does the assertion block tell a good population from a bad one? =="
  run_negative_control
  exit $?
fi

# ================================================================================================
# LIVE
# ================================================================================================
case "$CTX" in
  gke-scratch-*) : ;;
  *)
    echo "REFUSED: '$CTX' is not an anchored gke-scratch-* context." >&2
    echo "  This suite deletes and re-applies the Agent CR '$AGENT' in $NS, grants its actor identity" >&2
    echo "  WRITE authority over a namespace, seeds three dozen objects into it, and submits" >&2
    echo "  thirty-seven actions to a live broker. On the live install that is a test deleting the" >&2
    echo "  fleet's own agent and widening a production identity." >&2
    exit 2
    ;;
esac

$K version >/dev/null 2>&1 || {
  echo "FAIL: context '$CTX' is not reachable." >&2
  exit 1
}

# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/preconditions.sh"
# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/agent-fixtures.sh"
# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/actor-overlay.sh"
# shellcheck disable=SC1091
. "$REPO_ROOT/dev/lib/broker-driver.sh"
p10_assert_control_plane_healthy "$K" "$CTX" || exit 2

TMP="$(mktemp -d)" || exit 1
cleanup() {
  # The write grant first and unconditionally: a widened identity is the one thing here that is
  # dangerous if the run dies halfway, and it is cheap to revoke twice.
  actor_overlay_revoke_write "$K" "$TENANT_NS" >/dev/null 2>&1
  actor_overlay_revoke "$K" "$TENANT_NS" >/dev/null 2>&1
  broker_driver_delete "$K" "$NS" "$DRIVER_POD" "$DRIVER_CM" "$UNTRUSTED_SECRET"
  $K -n "$NS" delete agent "$AGENT" --ignore-not-found --wait=false >/dev/null 2>&1
  rm -rf "$TMP"
  echo
  echo "CLEANED UP: the write and read overlays are revoked, the driver pod and its ConfigMap are"
  echo "  gone, and the Agent CR is deleted. THE TENANT NAMESPACE AND ITS SEEDED OBJECTS ARE LEFT"
  echo "  STANDING, and so is every ActionRecord — [[LSN-045]]: the journal-retention policy denies"
  echo "  DELETE of an ActionRecord until export confirms, so a namespace holding one never finishes"
  echo "  terminating and a suite that tried would hang on its own evidence. A-5 is written for that"
  echo "  world rather than against it, and the records are what a human reads when this goes red."
}
trap cleanup EXIT

# ------------------------------------------------------------------------------------------------
# The population
# ------------------------------------------------------------------------------------------------
echo
echo "== the population, derived from the classifier corpus and the actor's grant =="
TABLE="$(python3 "$REPO_ROOT/$CORPUS_TOOL" --table)" || {
  echo "DEFERRED: $CORPUS_TOOL could not derive a population. That file self-tests on the L0 chain;"
  echo "  a failure here is a corpus or grant change it refused, not a broker result."
  exit 3
}
printf '%s\n' "$TABLE" | sed -n '2p' | sed 's/^/  /'
SUBMITTED="$(printf '%s\n' "$TABLE" | grep -cv '^#')"
echo "  $SUBMITTED envelope(s) to submit, one operation each"

# ------------------------------------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------------------------------------
echo
echo "== fixtures: the tenant namespace, the Agent CR, and the identity its broker runs as =="
printf 'apiVersion: v1\nkind: Namespace\nmetadata:\n  name: %s\n' "$TENANT_NS" | $K apply -f - >/dev/null || {
  echo "FAIL: could not create the tenant namespace $TENANT_NS" >&2
  exit 1
}
echo "  tenant namespace: $TENANT_NS"

# P3: deleted with --wait=true before it is applied, so everything the controller renders from it is
# this generation's.
$K -n "$NS" delete agent "$AGENT" --ignore-not-found --wait=true >/dev/null 2>&1
$K apply -f "$REPO_ROOT/$AGENT_MANIFEST" >/dev/null || {
  echo "FAIL: could not apply $AGENT_MANIFEST" >&2
  exit 1
}
seed_agent_fixtures "$K" "$NS" "$AGENT" || {
  echo "FAIL: could not seed fixtures for $AGENT" >&2
  exit 1
}
seed_agent_identity "$K" "$NS" "$AGENT" || {
  echo "FAIL: could not seed the actor identity for $AGENT" >&2
  exit 1
}

# Read AND write. The executor's pre-state read needs the first, and a server-side dry run needs the
# second, because the API server AUTHORIZES a dry run before it dry-runs it. Without the write half
# every submission fails 403 from inside the executor, which looks nothing like the thing it is.
actor_overlay_apply_write "$K" "$NS" "$AGENT" "$TENANT_NS" || {
  echo "DEFERRED: the actor could not be granted authority over $TENANT_NS; every submission would be"
  echo "  refused by the API server before the pipeline reached anything this suite measures."
  exit 3
}

# ------------------------------------------------------------------------------------------------
# Seeding the targets
# ------------------------------------------------------------------------------------------------
# The `present` half is applied and the `absent` half is DELETED, every run. Both directions matter:
# a `patch` at an object that is not there fails in the executor and never reaches the planner, and
# an `apply` at an object that IS there has `restore` as its inverse rather than `delete`, which is
# a different row of 06 §4.3.1 than the one the corpus row was selected for.
#
# Deployments are seeded at `replicas: 0`. Nothing schedules, nothing pulls an image, and the object
# is still a complete Deployment with a scale subresource — which is what the `scale` case needs.
echo
echo "== seeding $SUBMITTED targets in $TENANT_NS =="
printf '%s\n' "$TABLE" | python3 -c '
import sys

ns, label, value = sys.argv[1], sys.argv[2], sys.argv[3]
docs = []
for line in sys.stdin:
    line = line.rstrip("\n")
    if not line or line.startswith("#"):
        continue
    f = line.split("\t")
    kind, name, seed = f[4], f[8], f[9]
    if seed != "present":
        continue
    if kind == "ConfigMap":
        docs.append(
            f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {name}\n  namespace: {ns}\n"
            f"  labels:\n    {label}: {value}\ndata:\n  seeded: \"true\"\n"
        )
    elif kind == "Deployment":
        docs.append(
            f"apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {name}\n  namespace: {ns}\n"
            f"  labels:\n    {label}: {value}\nspec:\n  replicas: 0\n  selector:\n"
            f"    matchLabels:\n      app: {name}\n  template:\n    metadata:\n      labels:\n"
            f"        app: {name}\n    spec:\n      containers:\n        - name: pause\n"
            f"          image: registry.k8s.io/pause:3.9\n"
        )
    else:
        sys.exit(f"no seed shape for kind {kind!r} (case {f[0]}); add one rather than skipping it")
if not docs:
    sys.exit("the corpus selected no present-seeded target; the soak would measure only absent ones")
sys.stdout.write("---\n".join(docs))
' "$TENANT_NS" "$SEED_LABEL" "$SEED_LABEL_VALUE" >"$TMP/seeds.yaml" || {
  echo "FAIL: could not render the seed manifests" >&2
  exit 1
}
$K apply -f "$TMP/seeds.yaml" >/dev/null || {
  echo "FAIL: could not apply the seed manifests" >&2
  exit 1
}

# The `absent` half. `--ignore-not-found` because the steady state after a clean previous run is
# that they are already gone; the delete exists for the run that went red halfway through.
printf '%s\n' "$TABLE" | grep -v '^#' | awk -F'\t' '$10 == "absent" { print $6, $9 }' |
  while read -r resource name; do
    $K -n "$TENANT_NS" delete "$resource" "$name" --ignore-not-found --wait=true >/dev/null 2>&1
  done
echo "  seeded, and the absent half confirmed absent"

# ------------------------------------------------------------------------------------------------
# The pre-state. One bulk read per resource kind, not one per object: thirty-seven `kubectl get`s
# is a minute of wall clock and a minute in which the cluster is not the thing being measured.
# ------------------------------------------------------------------------------------------------
snapshot() { # <file>
  {
    $K -n "$TENANT_NS" get configmaps -o json 2>/dev/null
    $K -n "$TENANT_NS" get deployments -o json 2>/dev/null
  } >"$1"
}
snapshot "$TMP/before.json"

# ------------------------------------------------------------------------------------------------
# P1
# ------------------------------------------------------------------------------------------------
echo
echo "== P1: the controller and this agent's broker are the build under test =="
p1_assert_build_under_test "$K" "$NS" control-plane=controller-manager
case "$?" in
  0) pass "P1: the running controller is the build under test" ;;
  3)
    echo "DEFERRED: P1 unverifiable for the controller. It renders the broker, its mesh Certificates"
    echo "  and the pair NetworkPolicies; nothing below would be evidence about this commit."
    exit 3
    ;;
  *)
    bad "P1: the controller is not running the build under test"
    exit 1
    ;;
esac

broker_deploy="${AGENT}-broker"
broker_pod="$(p3_pod_of_deploy "$K" "$NS" "$broker_deploy" 180)"
if [ -z "$broker_pod" ]; then
  echo "DEFERRED: no pod is owned by deploy/$broker_deploy after 180s. There is no broker to submit to."
  $K -n "$NS" describe "deploy/$broker_deploy" 2>&1 | tail -20
  exit 3
fi
echo "  broker pod (by ownership, P3): $broker_pod"

p1_assert_build_under_test "$K" "$NS" "kube-agents/agent=$AGENT,kube-agents/role=actor"
case "$?" in
  0) pass "P1: the broker is running the build under test" ;;
  3)
    echo "DEFERRED: P1 unverifiable for the broker. Every arm here is a claim about what the"
    echo "  classifier, the planner and the executor DID across thirty-seven actions; an"
    echo "  unidentifiable binary makes all of them statements about an unknown build."
    exit 3
    ;;
  *)
    bad "P1: the broker is not running the build under test"
    exit 1
    ;;
esac

# Polled, not slept on (P9).
echo
echo "== waiting for the broker to become Available =="
avail=""
deadline=$((SECONDS + 240))
while [ "$SECONDS" -lt "$deadline" ]; do
  avail="$($K -n "$NS" get "deploy/$broker_deploy" \
    -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' 2>/dev/null)"
  [ "$avail" = "True" ] && break
  sleep 3
done
if [ "$avail" != "True" ]; then
  echo "DEFERRED: deploy/$broker_deploy never became Available (condition='${avail:-none}')."
  $K -n "$NS" describe "pod/$broker_pod" 2>&1 | tail -25
  exit 3
fi
pass "deploy/$broker_deploy is Available"

# ------------------------------------------------------------------------------------------------
# Submit
# ------------------------------------------------------------------------------------------------
echo
echo "== submitting $SUBMITTED envelopes from inside the cluster =="

broker_driver_use_probe "$PROBE" || {
  echo "FAIL: $PROBE is not where this suite says it is" >&2
  exit 1
}
# shellcheck disable=SC2034
BROKER_DRIVER_TENANT_NS="$TENANT_NS"
# The corpus, base64'd onto one line: `broker_driver_run` renders extra env into an unquoted heredoc
# and refuses a quote, dollar, backtick or backslash, and reads the list line by line. Base64's
# alphabet is entirely inside what it permits.
BROKER_DRIVER_EXTRA_ENV="PROBE_CORPUS_B64=$(printf '%s\n' "$TABLE" | base64 | tr -d '\n')"
# shellcheck disable=SC2034
export BROKER_DRIVER_EXTRA_ENV

broker_driver_apply_code "$K" "$NS" "$DRIVER_CM" || {
  echo "FAIL: could not stage the shipped transport code" >&2
  exit 1
}
broker_driver_untrusted_keypair "$K" "$NS" "$UNTRUSTED_SECRET" || {
  echo "FAIL: could not generate the placeholder keypair the driver pod mounts" >&2
  exit 1
}

driver_out="$(broker_driver_run "$K" "$NS" "$AGENT" "$AGENT" "$DRIVER_POD" "$DRIVER_CM" "$UNTRUSTED_SECRET")"
driver_rc=$?
if [ "$driver_rc" -ne 0 ]; then
  echo "DEFERRED: the driver pod could not be run to completion, so the soak never ran."
  echo "  An inability to run the experiment, not a property that failed (P10's distinction)."
  exit 3
fi
printf '%s\n' "$driver_out" >"$TMP/probe.jsonl"
printf '%s\n' "$driver_out" | grep -c '^{' | sed 's/^/  transcript lines: /'

# The post-state, read immediately after the run and before anything else touches the namespace.
snapshot "$TMP/after.json"

# ------------------------------------------------------------------------------------------------
# The mine
# ------------------------------------------------------------------------------------------------
# Where the broker says it put them. Read off the replies rather than assumed, and cross-checked:
# a run whose replies name two namespaces is a run whose denominator is ambiguous, and it says so.
RECORD_NS="$(python3 -c '
import json, sys
seen = []
for line in open(sys.argv[1]):
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        r = json.loads(line)
    except ValueError:
        continue
    ns = (r.get("namespace") or "").strip()
    if r.get("actionId") and ns and ns not in seen:
        seen.append(ns)
print(" ".join(seen))
' "$TMP/probe.jsonl")"
case "$RECORD_NS" in
  '')
    RECORD_NS="$NS"
    echo "  no reply named a journal namespace; falling back to $NS"
    ;;
  *' '*)
    echo "FAIL: the replies name more than one journal namespace ($RECORD_NS). The denominator would" >&2
    echo "  span two namespaces and A-5's attribution would be reporting on one of them." >&2
    exit 1
    ;;
esac

RUN_IDS="$(python3 -c '
import json, sys
for line in open(sys.argv[1]):
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        r = json.loads(line)
    except ValueError:
        continue
    if r.get("outcome") == "http" and r.get("actionId"):
        print(r["actionId"])
' "$TMP/probe.jsonl")"
ACCEPTED="$(printf '%s\n' "$RUN_IDS" | grep -c '[^[:space:]]')"

echo
echo "== mining $RECORD_NS for the $ACCEPTED record(s) this run's replies named =="
# POLLED (P9). Thirty-seven journal writes do not land at the same instant, and a fixed sleep
# followed by one read is how "the broker is slow" becomes "the broker did not journal". 90s
# because a durable write that has not landed in a minute and a half is absent, not slow.
want="$(printf '%s\n' "$RUN_IDS" | tr '[:upper:]' '[:lower:]' | sed '/^$/d; s/^/ar-/' | sort)"
deadline=$((SECONDS + 90))
while [ "$SECONDS" -lt "$deadline" ]; do
  $K -n "$RECORD_NS" get actionrecords -o json >"$TMP/records.json" 2>/dev/null
  have="$(python3 -c '
import json, sys
try:
    doc = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
for item in doc.get("items", []):
    print(item.get("metadata", {}).get("name", ""))
' "$TMP/records.json" | sort)"
  [ -z "$(comm -23 <(printf '%s\n' "$want") <(printf '%s\n' "$have"))" ] && break
  sleep 3
done
NS_RECORD_TOTAL="$(python3 -c '
import json, sys
try:
    print(len(json.load(open(sys.argv[1])).get("items", [])))
except Exception:
    print(0)
' "$TMP/records.json")"
echo "  $RECORD_NS holds $NS_RECORD_TOTAL ActionRecord(s) in total, this run's and every earlier run's"

# ------------------------------------------------------------------------------------------------
# The two tables
# ------------------------------------------------------------------------------------------------
POP="$(python3 -c '
import json, sys

probe, records = sys.argv[1], sys.argv[2]

by_name = {}
try:
    for item in json.load(open(records)).get("items", []):
        by_name[item.get("metadata", {}).get("name", "")] = item
except Exception:
    pass


def dig(doc, path):
    for part in path.split("."):
        if not isinstance(doc, dict):
            return None
        doc = doc.get(part)
        if doc is None:
            return None
    return doc


for line in open(probe):
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        r = json.loads(line)
    except ValueError:
        continue
    if r.get("outcome") != "http" or not r.get("actionId"):
        continue
    action_id = r["actionId"]
    # `journal.RecordName` is `"ar-" + strings.ToLower(actionID)` (06 §4.3, journal/ulid.go).
    # Derived the way the broker derives it, not read off the reply: the reply is the claim, and
    # the point of the lookup is to check the claim against the API server.
    rec = by_name.get("ar-" + action_id.lower())
    if rec is None:
        print("\t".join([r.get("scenario", ""), r.get("verb", ""), action_id, "no", "", "", "", "", "", "", ""]))
        continue
    steps = dig(rec, "spec.undo.steps")
    print(
        "\t".join(
            [
                r.get("scenario", ""),
                r.get("verb", ""),
                action_id,
                "yes",
                str(dig(rec, "status.phase") or ""),
                str(dig(rec, "spec.classification.class") or ""),
                "true" if dig(rec, "spec.dryRun") is True else "false",
                str(dig(rec, "spec.undo.strategy") or ""),
                "true" if dig(rec, "spec.undo.validated") is True else "false",
                str(len(steps) if isinstance(steps, list) else 0),
                str(dig(rec, "status.timestamps.executionStarted") or ""),
            ]
        )
    )
' "$TMP/probe.jsonl" "$TMP/records.json")"

WORLD="$(printf '%s\n' "$TABLE" | python3 -c '
import json, sys

before_path, after_path = sys.argv[1], sys.argv[2]


def versions(path):
    """name -> resourceVersion, over the concatenated bulk reads. Keyed by (kind, name) because a
    ConfigMap and a Deployment may legitimately share a name and one would otherwise mask the
    other -- which would compare the wrong object and call the shadow held."""
    out = {}
    buf, depth, started = "", 0, False
    for ch in open(path).read():
        if ch == "{":
            depth += 1
            started = True
        if started:
            buf += ch
        if ch == "}":
            depth -= 1
            if depth == 0 and started:
                try:
                    doc = json.loads(buf)
                except ValueError:
                    doc = {}
                for item in doc.get("items", []):
                    meta = item.get("metadata", {})
                    out[(item.get("kind", ""), meta.get("name", ""))] = meta.get("resourceVersion", "")
                buf, started = "", False
    return out


before, after = versions(before_path), versions(after_path)

for line in sys.stdin:
    line = line.rstrip("\n")
    if not line or line.startswith("#"):
        continue
    f = line.split("\t")
    cid, verb, kind, name, seed = f[0], f[2], f[4], f[8], f[9]
    b = before.get((kind, name), "absent")
    a = after.get((kind, name), "absent")
    print("\t".join([cid, verb, seed, b or "unknown", a or "unknown"]))
' "$TMP/before.json" "$TMP/after.json")"

# ------------------------------------------------------------------------------------------------
assert_population

echo
if [ "$assertions" -ne "$EXPECTED_ASSERTIONS" ]; then
  bad "only $assertions of $EXPECTED_ASSERTIONS assertions ran. The verdict below would be about arms that never executed."
fi

echo
echo "===================================================================="
if [ "$fail" -ne 0 ]; then
  echo " FAILED — see the FAIL lines above."
  echo "===================================================================="
  exit 1
fi
if [ "$deferrals" -ne 0 ]; then
  echo " DEFERRED ($deferrals arm(s)) — see the DEFER lines above."
  echo " V-REV-001 is NOT scored from this run: an arm that could not run its property is deferred"
  echo " with a named blocker, never recorded as a pass."
  echo "===================================================================="
  exit 3
fi
echo " PROVEN: V-REV-001 at L2, at population scale"
echo " $SUBMITTED envelopes derived from the classifier corpus reached a deployed broker; every"
echo " executed non-gated ActionRecord it wrote carries a validated undo plan; the population is"
echo " this run's own and no target object moved."
echo "===================================================================="
exit 0
