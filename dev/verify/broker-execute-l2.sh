#!/usr/bin/env bash
# V-BRK-006 · V-REV-001 at L2 — one well-formed envelope, submitted to a DEPLOYED broker by a real
# caller, and the ActionRecord the journal actually holds afterwards (09 §6.2, 05 §1.2, 03 §11,
# 06 §4.1).
#
# This is Phase 9 acceptance bullet (a): "an envelope flows end-to-end in shadow mode and produces a
# well-formed `ActionRecord` with a valid undo plan." Until this file existed that sentence had
# never been executed once against a cluster. Four suites prove the undo machinery — the index
# resolves, the prober reads back, the replayer restores — and not one of them submits an envelope.
# Every one of them starts from an `ActionRecord` that a test wrote.
#
# THE THING THAT MAKES THIS DIFFERENT FROM `broker-auth-l2.sh`
#   That suite asks what the broker does with a CREDENTIAL, and its one positive exists so its nine
#   refusals are not vacuous — it says in its own words that it does not care whether the pipeline
#   accepted the envelope. This suite asks what the broker does with an ENVELOPE, and the answer is
#   an object in the API server that outlives the request. Everything asserted below is read back
#   from that object, never from the reply body that claims it.
#
# WHAT IS ASSERTED, in order:
#   L2-0  THE DOOR, AND THEN THE PIPELINE. The shipped client gets a nonce and the shipped envelope
#         builder's output is ACCEPTED — not merely un-refused at the auth layer, but answered with
#         an `actionId` and a namespace. A run that stops here has learned nothing about the
#         journal, so its failure is fatal and reported as could-not-run, not as a red.
#   L2-1  THE RECORD EXISTS, in the namespace the broker named, under the actionId it returned. The
#         suite polls for it (P9) rather than sleeping. This is the first time in the tree that an
#         `ActionRecord` read by a check was written by the broker rather than by the check.
#   L2-2  V-BRK-006's L2 clause — WRITE-AHEAD. The record's `metadata.creationTimestamp`, which the
#         API SERVER assigns, is not after `status.timestamps.executionStarted`, which the BROKER
#         records when it issues the first mutating call. Two clocks, two writers, one ordering: the
#         journal entry was durable before the mutation was attempted. A broker that journaled after
#         executing would invert this, and no other check in the tree would notice.
#         V-BRK-006's OTHER clause — "a broker killed mid-action leaves no unjournaled write" — is
#         L4 and is NOT claimed here. 09 §6 lists the row at L2 and L4 for exactly this reason.
#   L2-3  THE SHADOW HELD. `spec.dryRun` is true on the journaled record, and the ConfigMap the
#         operation aimed at DOES NOT EXIST. The second half is the one that matters and it is only
#         answerable because the object was absent to begin with and nobody here creates it: had it
#         existed, "unchanged" would also be satisfied by a broker that never ran.
#   L2-4  V-REV-001 at L2 — the record carries a VALIDATED UNDO PLAN: `spec.undo.strategy` is set to
#         something other than the empty string, `spec.undo.validated` is true, and a strategy other
#         than `none` carries at least one step. The operation was chosen so that `none` would be a
#         wrong answer rather than a permitted one (see the probe's `target_configmap`).
#   L2-5  THE PHASE IS ONE THE LIFECYCLE ADMITS, and it is terminal. Read against
#         `actionrecord_phases.go`'s own transition table via the CRD's enum, so a broker inventing
#         a phase word is caught here rather than at whatever later check assumes the set.
#
# WHAT THIS DOES NOT CLAIM, and where each went instead
#   V-REV-001 SAYS "100%", AND THIS IS n=1. One record carrying a validated plan is not a population
#     claim, and this suite does not pretend otherwise: it proves the property holds for an envelope
#     the BROKER classified, which is the thing no fixture-written record can establish, and leaves
#     the population to P9-T8b-4b-ii-2b-ii's corpus soak — which needs this submission path to
#     exist before it can run at all. The `results.csv` row says n=1 in its notes.
#   V-BRK-018 (snapshot-persist failure ⇒ neither target applied) is fault injection against a
#     multi-target envelope. Nothing here fails, on purpose. → P9-T9b-5b-ii.
#   V-BRK-019 (the field manager string) is not observable from a shadow: a server-side dry-run does
#     not persist `managedFields`, so there is nothing to read the manager off. It needs a real
#     apply, which Phase 9 does not do. → carried, unscheduled, named here so it is not lost.
#   V-REV-002 (`undo <id>` restores prior state) requires executing an undo. Not this unit.
#   V-REV-003 (no generatable undo plan ⇒ reclassified gated) is the NEGATIVE of L2-4 and needs an
#     operation whose inverse does not exist. This envelope's inverse does. → P9-T9b-5b-ii.
#   THAT THE PUBLISHED AGENT IMAGE CARRIES THIS TRANSPORT CODE. The driver pod mounts
#     `agents/platform/scripts/` from the working tree onto a stock `python:3.12-slim`, for the
#     reason `broker-auth-l2.sh` gives: what is under test is the shipped source against the
#     deployed broker, and image parity is P1's job on a different row.
#
# DESTRUCTIVE-TEST GUARD: scratch-GKE contexts only, anchored. This deletes and re-applies the Agent
# CR `platform-agent` in `kubeagents-system`, grants the actor identity WRITE authority over a
# throwaway tenant namespace, runs a pod, and submits an action to a live broker. On the live
# install that is a test deleting the fleet's own agent and widening a production identity.
# Exit: 0 = PROVEN · 1 = FAILED · 2 = refused target / P10 · 3 = DEFERRED (P1 or the run itself).
# Usage: dev/verify/broker-execute-l2.sh [kube-context]
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions):
#   P1 image-under-test: the controller (`control-plane=controller-manager`) AND this agent's BROKER
#      pod (`kube-agents/agent=<agent>,kube-agents/role=actor`) — both, and in full, which is the
#      difference between this suite and `actor-overlay-admission-l2.sh` next to it in the chain.
#      That one asserts what an API server does with a manifest and declares P1 `none`; every claim
#      HERE is a claim about what a BINARY did — the classifier that chose the class, the planner
#      that generated the undo plan, the journal writer that ordered the write against the
#      execution. A broker one generation behind the tree would answer all five arms about the
#      previous build's pipeline, and they would read green. Unverifiable → rc 3.
#   P3 admission-recreate: the Agent CR is deleted with `--wait=true` and re-applied on every run,
#      so the broker Deployment, its mesh Certificates and the pair NetworkPolicies are rendered by
#      the controller running NOW. The broker pod is resolved through `p3_pod_of_deploy`, by
#      ownership, so a pod from the previous generation of the same Deployment can never be read as
#      this one's. The tenant namespace, the driver pod, its ConfigMap and the write overlay are all
#      created and destroyed inside the run. The ActionRecord is deliberately NOT recreated — it is
#      the output — and is instead disambiguated by the actionId the broker returned in THIS run.
#   P6 runtime-authoritative: every assertion reads the `ActionRecord` object from the API server.
#      Nothing is read from the broker's reply body, from its log, or from a golden. The driver's
#      whole environment — endpoint, SAN, identity, token path, TLS dir — comes off the RENDERED
#      agent Deployment through `broker_driver_env`, never recomputed from the naming functions in
#      `broker_manifests.go`. The legal phase set is read from the CRD the API server is serving,
#      not from the Go enum in the tree, so a CRD that shipped a different set is a FAIL and not an
#      agreement between two copies of the same list.
set -uo pipefail

# MODES. `live` submits to a real broker and is what every claim above is about.
# `--negative-control` is the mandatory `¬` arm for V-BRK-006: it replays the assertion block
# against hand-written records that a MISBEHAVING broker would have written, and requires each to
# go red. See run_negative_control for why a transcript is the only form the `¬` can take when the
# real run's evidence is an object the broker had to create correctly to produce at all.
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

# The tenant the operation aims at. Its own namespace, not `kubeagents-system`: see the probe's
# `target_configmap` for why the executor's authorization decides this, and P9-T9b-5a's ruling for
# why the write grant that makes it possible is deliberately outside the admission policy's
# population rather than a carve-out inside it.
TENANT_NS=kubeagents-execute-tenant

DRIVER_POD=broker-execute-l2-driver
DRIVER_CM=broker-execute-l2-code
UNTRUSTED_SECRET=broker-execute-l2-untrusted
PROBE=dev/verify/fixtures/broker_execute_probe.py

# NEGATIVE CONTROL DOES NOT EXERCISE: (LSN-060, and this suite is the lesson.) The control
# SYNTHESISES the record document — thirteen mutated JSONs handed straight to the assertion block —
# so everything upstream of the assertions is unmeasured by it:
#   - the envelope build and the HTTP POST to the deployed broker (L2-0). A synthesised document
#     is not a broker's output; the ¬ arm cannot tell a running broker from an absent one
#   - the API-server lookup of the record by name (L2-1). This is not hypothetical: the arm asked
#     for the RAW action id for as long as it existed, against objects named `ar-<lowercase ULID>`
#     (`journal.RecordName`, 06 §4.3), and could not have found a record against any commit. The
#     ¬ arm was 13/13 green throughout, because it never ran the line
#   - the P1 digest arms and the broker's Availability, which run before either mode
#   - the read of the TARGET object (L2-3b). The control mutates the record's own document to
#     claim a mutation happened; it never asks the cluster whether one did
# What it does prove, and all it proves: the assertion block is not always-green — each of the
# thirteen defects is caught by the arm that targets it, named in the output.
fail=0

# EVERY ARM IS COUNTED, AND THE COUNT IS ASSERTED AT THE END. `broker-auth-l2.sh` carries the full
# argument for why; the one-line version is that this file had a sibling which printed six PASS
# lines and a PROVEN banner having asserted none of its rows, because `fail` stays 0 when no
# assertion runs. Change EXPECTED_ASSERTIONS deliberately, in the same commit as the arm.
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

# 2 x P1 + broker Available + L2-0 accepted + L2-1 exists + L2-2 write-ahead + L2-3a dryRun
# + L2-3b not-mutated + L2-4 undo plan + L2-5 phase.
EXPECTED_ASSERTIONS=10

# ------------------------------------------------------------------------------------------------
# The assertion block, over one record's JSON. A function because `--negative-control` replays
# exactly these arms against records nobody's broker wrote.
#
# It takes the record as JSON on stdin and the target's existence as an argument, because those are
# the only two inputs: everything else is derived. Keeping the derivation inside means the `¬` arm
# exercises the derivation too, rather than a simplified copy of it that could agree with a
# misbehaving broker for a reason the live path would not.
# ------------------------------------------------------------------------------------------------

# jrec <jsonpath-ish key> — one field out of $RECORD, via python, because `jq` is not a dependency
# this repo has taken on and `kubectl -o jsonpath` cannot be re-run against a captured document.
jrec() {
  printf '%s' "$RECORD" | python3 -c '
import json, sys
doc = json.load(sys.stdin)
for part in sys.argv[1].split("."):
    if isinstance(doc, dict):
        doc = doc.get(part)
    else:
        doc = None
    if doc is None:
        break
if doc is None:
    print("")
elif isinstance(doc, bool):
    print("true" if doc else "false")
elif isinstance(doc, (list, dict)):
    print(len(doc))
else:
    print(doc)
' "$1"
}

# not_after <rfc3339-a> <rfc3339-b> — true when a <= b. Two clocks are being compared and both are
# RFC3339 with second granularity, so EQUAL IS A PASS: a broker that journals and then immediately
# executes will very often produce identical stamps, and treating that as a violation would make
# the arm fail on a fast pipeline, which is the opposite of the property.
not_after() {
  python3 -c '
import sys
from datetime import datetime

def parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

sys.exit(0 if parse(sys.argv[1]) <= parse(sys.argv[2]) else 1)
' "$1" "$2" 2>/dev/null
}

# assert_record <target-exists: yes|no>
#   $RECORD must be set. Every arm here is about the object, not the reply.
assert_record() {
  local target_exists="$1"
  local created started dry strategy validated steps phase legal

  # ----------------------------------------------------------------------------------------------
  # L2-2: V-BRK-006 at L2 — the journal entry was durable before the mutation was attempted
  # ----------------------------------------------------------------------------------------------
  echo
  echo "== L2-2: V-BRK-006 — write-ahead, across two clocks =="
  created="$(jrec metadata.creationTimestamp)"
  started="$(jrec status.timestamps.executionStarted)"
  if [ -z "$created" ]; then
    bad "V-BRK-006: the record carries no metadata.creationTimestamp. That is server-assigned; a record without one did not come from an API server."
  elif [ -z "$started" ]; then
    # NOT a pass by default. A record with no executionStarted is a record whose mutation was never
    # attempted, and "the journal preceded a thing that did not happen" is vacuous. 06 §4.1 has the
    # broker stamp this when it issues the first mutating call, and a dry-run apply IS one.
    bad "V-BRK-006: the record has creationTimestamp=$created but NO status.timestamps.executionStarted. Either the executor never ran, or it ran without stamping — and either way the write-ahead ordering has nothing to compare against."
  elif not_after "$created" "$started"; then
    pass "V-BRK-006: the record was created at $created (API server) and execution began at $started (broker) — the journal entry preceded the mutation"
  else
    bad "V-BRK-006: WRITE-AHEAD INVERTED. The record was created at $created but the broker records execution as having begun at $started, which is EARLIER. A mutation was issued before the journal entry was durable."
  fi

  # ----------------------------------------------------------------------------------------------
  # L2-3: the shadow held — both halves
  # ----------------------------------------------------------------------------------------------
  echo
  echo "== L2-3: the shadow held =="
  dry="$(jrec spec.dryRun)"
  if [ "$dry" = "true" ]; then
    pass "spec.dryRun is true on the journaled record — the shadow is recorded, not merely requested"
  else
    bad "spec.dryRun is '$dry' on the journaled record. The envelope asked for a dry run; the journal did not record one, so V-BRK-024's history source would learn familiarity from a shadow."
  fi

  case "$target_exists" in
    no) pass "the target object does not exist — a shadow run mutated nothing, asserted against the API server rather than against the broker's word for it" ;;
    yes) bad "THE TARGET OBJECT EXISTS. A dry-run submission created it. This is the one failure in this file that is a live safety defect and not a reporting one." ;;
    *) bad "could not determine whether the target object exists (got '$target_exists'); the no-mutation arm did not run" ;;
  esac

  # ----------------------------------------------------------------------------------------------
  # L2-4: V-REV-001 at L2 — a validated undo plan, on a record the BROKER classified
  # ----------------------------------------------------------------------------------------------
  echo
  echo "== L2-4: V-REV-001 (n=1) — the undo plan the broker generated =="
  strategy="$(jrec spec.undo.strategy)"
  validated="$(jrec spec.undo.validated)"
  steps="$(jrec spec.undo.steps)"
  [ -n "$steps" ] || steps=0
  if [ -z "$strategy" ]; then
    bad "V-REV-001: the record carries no spec.undo.strategy. The action was journaled with no undo plan at all."
  elif [ "$validated" != "true" ]; then
    bad "V-REV-001: the undo plan has strategy '$strategy' but validated=$validated. An unvalidated plan is a plan nobody checked against the API server, which is precisely what the previous unit made the planner do."
  elif [ "$strategy" = "none" ]; then
    # `none` is a legal strategy in general and a WRONG ANSWER for this operation in particular.
    # 06 §4.3.1's inverse of "apply over an absent object" is `delete`, which is a step. A planner
    # that answered `none` here would be a planner that gave up, and it would pass an arm written
    # as "strategy is set".
    bad "V-REV-001: the undo plan is strategy 'none' for an apply over an ABSENT object, whose 06 §4.3.1 inverse is a delete. The planner produced no inverse for an operation that has one."
  elif [ "$steps" -lt 1 ]; then
    bad "V-REV-001: the undo plan is strategy '$strategy', validated, and carries $steps steps. A non-'none' strategy with no steps restores nothing."
  else
    pass "V-REV-001: strategy '$strategy', validated, $steps step(s) — generated by the broker from an envelope it received, not written by a fixture"
  fi

  # ----------------------------------------------------------------------------------------------
  # L2-5: the phase is one the served CRD admits
  # ----------------------------------------------------------------------------------------------
  echo
  echo "== L2-5: the terminal phase is one the lifecycle admits =="
  phase="$(jrec status.phase)"
  if [ -z "$phase" ]; then
    bad "the record carries no status.phase. Nothing advanced it out of the zero value."
  elif [ -z "${LEGAL_PHASES:-}" ]; then
    bad "the legal phase set could not be read from the served CRD, so '$phase' cannot be judged. Asserting it against a list in this file would be two copies agreeing."
  else
    legal=no
    for p in $LEGAL_PHASES; do
      [ "$p" = "$phase" ] && legal=yes && break
    done
    if [ "$legal" = yes ]; then
      pass "status.phase is '$phase', which the served ActionRecord CRD's enum admits"
    else
      bad "status.phase is '$phase', which the served CRD's enum does NOT admit (legal: $LEGAL_PHASES). The broker wrote a phase word the API is not supposed to accept."
    fi
  fi
}

# ------------------------------------------------------------------------------------------------
# The `¬` arm
# ------------------------------------------------------------------------------------------------
# WHY A TRANSCRIPT AND NOT A MUTATION. Every arm above reads a record the broker had to build
# correctly to produce at all, so there is no cheap way to make a REAL broker emit an inverted
# write-ahead ordering or an unvalidated plan — the mutations that would do it are edits to the Go
# pipeline, which is `dev/mutate.py`'s job at L1 and not something an L2 suite can stage against a
# deployed binary. What this arm proves is the thing an L2 suite CAN get wrong on its own: that the
# assertion block distinguishes a good record from a bad one at all.
#
# EACH MUTANT MUST BE CAUGHT BY THE ARM THAT TARGETS IT ([[LSN-035]]). Every row carries a needle,
# and a row counts as caught only when a FAIL line CONTAINS that needle — not merely when something
# somewhere went red. Without that, breaking `jrec` would "catch" all eight mutants at once by
# failing every arm on every one of them, and the negative control would read 8/8 while asserting
# that the suite is broken.
#
# THE VERDICT IS READ OFF THE OUTPUT, NEVER OFF `$fail`. `assert_record` runs inside a command
# substitution, which is a subshell, so every `fail=1` it sets dies with it. A `¬` arm that tested
# `$fail` here would see 0 for all eight mutants and report them all as escapes — or, with the
# comparison the other way round, all as caught. `broker-auth-l2.sh` counts `^PASS:`/`^FAIL:` lines
# for the same reason.
run_negative_control() {
  local name expect needle doc out n_fail rc=0 total=0 caught=0

  # The served CRD is not reachable in this mode, so the legal set is the tree's — which is correct
  # HERE and would be wrong in the live path: this arm is testing the assertion block, not the
  # cluster, and `invented-phase` needs a set to be invented against. Kept in step with
  # `actionrecord_types.go`'s kubebuilder enum.
  LEGAL_PHASES="Pending PendingApproval Executing Verified Failed RolledBack Undone Rejected Expired DryRun"

  # name | expect | needle | record
  while IFS='|' read -r name expect needle doc; do
    [ -n "$name" ] || continue
    total=$((total + 1))
    RECORD="$doc"
    out="$(assert_record no 2>&1)"
    n_fail="$(printf '%s\n' "$out" | grep -c '^FAIL:')"
    if [ "$expect" = green ]; then
      if [ "$n_fail" -eq 0 ]; then
        echo "  ok   $name — the correct record passes, so the arms below are not always-red"
        caught=$((caught + 1))
      else
        echo "  MISS $name — a CORRECT record was failed $n_fail time(s); every mutant below would be caught for the wrong reason"
        printf '%s\n' "$out" | grep '^FAIL:' | sed 's/^/       /'
        rc=1
      fi
    elif printf '%s\n' "$out" | grep '^FAIL:' | grep -qF "$needle"; then
      echo "  ok   $name — caught by the arm that targets it ('$needle')"
      caught=$((caught + 1))
    else
      echo "  MISS $name — went red $n_fail time(s) but no FAIL line mentions '$needle', so the property it targets is not what caught it"
      printf '%s\n' "$out" | grep '^FAIL:' | sed 's/^/       /'
      rc=1
    fi
  done <<'CASES'
baseline|green|-|{"metadata":{"creationTimestamp":"2026-07-30T10:00:00Z"},"spec":{"dryRun":true,"undo":{"strategy":"delete","validated":true,"steps":[{"op":"delete"}]}},"status":{"phase":"DryRun","timestamps":{"executionStarted":"2026-07-30T10:00:02Z"}}}
inverted-write-ahead|red|WRITE-AHEAD INVERTED|{"metadata":{"creationTimestamp":"2026-07-30T10:00:05Z"},"spec":{"dryRun":true,"undo":{"strategy":"delete","validated":true,"steps":[{"op":"delete"}]}},"status":{"phase":"DryRun","timestamps":{"executionStarted":"2026-07-30T10:00:02Z"}}}
no-execution-stamp|red|NO status.timestamps.executionStarted|{"metadata":{"creationTimestamp":"2026-07-30T10:00:00Z"},"spec":{"dryRun":true,"undo":{"strategy":"delete","validated":true,"steps":[{"op":"delete"}]}},"status":{"phase":"DryRun","timestamps":{}}}
no-creation-stamp|red|no metadata.creationTimestamp|{"metadata":{},"spec":{"dryRun":true,"undo":{"strategy":"delete","validated":true,"steps":[{"op":"delete"}]}},"status":{"phase":"DryRun","timestamps":{"executionStarted":"2026-07-30T10:00:02Z"}}}
shadow-not-recorded|red|spec.dryRun is 'false'|{"metadata":{"creationTimestamp":"2026-07-30T10:00:00Z"},"spec":{"dryRun":false,"undo":{"strategy":"delete","validated":true,"steps":[{"op":"delete"}]}},"status":{"phase":"DryRun","timestamps":{"executionStarted":"2026-07-30T10:00:02Z"}}}
no-plan-at-all|red|no spec.undo.strategy|{"metadata":{"creationTimestamp":"2026-07-30T10:00:00Z"},"spec":{"dryRun":true},"status":{"phase":"DryRun","timestamps":{"executionStarted":"2026-07-30T10:00:02Z"}}}
unvalidated-plan|red|validated=false|{"metadata":{"creationTimestamp":"2026-07-30T10:00:00Z"},"spec":{"dryRun":true,"undo":{"strategy":"delete","validated":false,"steps":[{"op":"delete"}]}},"status":{"phase":"DryRun","timestamps":{"executionStarted":"2026-07-30T10:00:02Z"}}}
gave-up-planning|red|strategy 'none' for an apply over an ABSENT object|{"metadata":{"creationTimestamp":"2026-07-30T10:00:00Z"},"spec":{"dryRun":true,"undo":{"strategy":"none","validated":true,"steps":[]}},"status":{"phase":"DryRun","timestamps":{"executionStarted":"2026-07-30T10:00:02Z"}}}
stepless-plan|red|carries 0 steps|{"metadata":{"creationTimestamp":"2026-07-30T10:00:00Z"},"spec":{"dryRun":true,"undo":{"strategy":"delete","validated":true,"steps":[]}},"status":{"phase":"DryRun","timestamps":{"executionStarted":"2026-07-30T10:00:02Z"}}}
no-phase|red|no status.phase|{"metadata":{"creationTimestamp":"2026-07-30T10:00:00Z"},"spec":{"dryRun":true,"undo":{"strategy":"delete","validated":true,"steps":[{"op":"delete"}]}},"status":{"timestamps":{"executionStarted":"2026-07-30T10:00:02Z"}}}
invented-phase|red|does NOT admit|{"metadata":{"creationTimestamp":"2026-07-30T10:00:00Z"},"spec":{"dryRun":true,"undo":{"strategy":"delete","validated":true,"steps":[{"op":"delete"}]}},"status":{"phase":"Finished","timestamps":{"executionStarted":"2026-07-30T10:00:02Z"}}}
CASES

  # The last two mutants are not fields of the record: they are the world the record was produced
  # in. A shadow run that CREATED the object must go red even when the record itself is flawless,
  # and a run that could not determine whether it did must not be scored as proof that it did not.
  # Neither is reachable by editing a document, which is why they are here rather than above.
  local perfect='{"metadata":{"creationTimestamp":"2026-07-30T10:00:00Z"},"spec":{"dryRun":true,"undo":{"strategy":"delete","validated":true,"steps":[{"op":"delete"}]}},"status":{"phase":"DryRun","timestamps":{"executionStarted":"2026-07-30T10:00:02Z"}}}'
  local world wname wneedle
  while IFS='|' read -r wname world wneedle; do
    [ -n "$wname" ] || continue
    total=$((total + 1))
    RECORD="$perfect"
    out="$(assert_record "$world" 2>&1)"
    if printf '%s\n' "$out" | grep '^FAIL:' | grep -qF "$wneedle"; then
      echo "  ok   $wname — caught by the arm that targets it ('$wneedle')"
      caught=$((caught + 1))
    else
      echo "  MISS $wname — a flawless record with world='$world' did not produce a FAIL mentioning '$wneedle'"
      printf '%s\n' "$out" | sed 's/^/       /'
      rc=1
    fi
  done <<'WORLD'
shadow-mutated-the-target|yes|THE TARGET OBJECT EXISTS
target-existence-unknown|unknown|the no-mutation arm did not run
WORLD

  echo
  echo "negative control: $caught/$total"
  return $rc
}

if [ "$MODE" = negative-control ]; then
  echo "== broker-execute-l2.sh --negative-control: does the assertion block tell a good record from a bad one? =="
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
    echo "  WRITE authority over a namespace, and submits an action to a live broker. On the live" >&2
    echo "  install that is a test deleting the fleet's own agent and widening a production identity." >&2
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

cleanup() {
  # The write grant first and unconditionally, for the reason P9-T9b-5a's suite gives: a widened
  # identity is the one thing here that is dangerous if the run dies halfway, and it is cheap to
  # revoke twice.
  actor_overlay_revoke_write "$K" "$TENANT_NS" >/dev/null 2>&1
  actor_overlay_revoke "$K" "$TENANT_NS" >/dev/null 2>&1
  broker_driver_delete "$K" "$NS" "$DRIVER_POD" "$DRIVER_CM" "$UNTRUSTED_SECRET"
  $K -n "$NS" delete agent "$AGENT" --ignore-not-found --wait=false >/dev/null 2>&1
  echo
  echo "CLEANED UP: the write and read overlays are revoked, the driver pod and its ConfigMap are"
  echo "  gone, and the Agent CR is deleted. The TENANT NAMESPACE IS LEFT STANDING and so is the"
  echo "  ActionRecord in it — [[LSN-045]]: the journal-retention policy denies DELETE of an"
  echo "  ActionRecord until export confirms, so a namespace holding one never finishes terminating"
  echo "  and a suite that tried would hang on its own evidence. The record is also the artifact a"
  echo "  human reads when this run goes red."
}
trap cleanup EXIT

# ------------------------------------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------------------------------------
echo
echo "== fixtures: the tenant namespace, the Agent CR, and the identity its broker runs as =="

# `apply` rather than `create`: `create` on an existing namespace is an error, and `create` followed
# by `apply` leaves a last-applied-configuration warning on every subsequent run.
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

# The overlay. Read AND write: the executor's step-3 pre-state read needs the first, and the
# server-side dry-run apply needs the second, because the API server AUTHORIZES a dry-run before it
# dry-runs it. Without the write half this run fails 403 inside the executor, which looks nothing
# like the thing it is.
actor_overlay_apply_write "$K" "$NS" "$AGENT" "$TENANT_NS" || {
  echo "DEFERRED: the actor could not be granted authority over $TENANT_NS; the executor would be"
  echo "  refused by the API server before the pipeline reached anything this suite measures."
  exit 3
}

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
    echo "  classifier, the planner and the journal writer DID; an unidentifiable binary makes all"
    echo "  of them statements about an unknown build."
    exit 3
    ;;
  *)
    bad "P1: the broker is not running the build under test"
    exit 1
    ;;
esac

# Polled, not slept on (P9): `.status` is controller-written, and a fixed wait is how a slow image
# pull becomes "the broker refused the envelope".
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

# The legal phase set, from the CRD THE API SERVER IS SERVING (P6). Not from
# `actionrecord_phases.go`, and not from a list in this file: either of those would be this suite
# agreeing with a copy of the same enum, and would pass on a cluster serving a different CRD.
LEGAL_PHASES="$($K get crd actionrecords.kubeagents.x-k8s.io \
  -o jsonpath='{.spec.versions[?(@.name=="v1alpha1")].schema.openAPIV3Schema.properties.status.properties.phase.enum[*]}' 2>/dev/null)"
if [ -z "$LEGAL_PHASES" ]; then
  echo "  WARNING: the served CRD publishes no enum for status.phase; L2-5 will report that it could not judge."
else
  echo "  legal phases, from the served CRD: $LEGAL_PHASES"
fi

# ------------------------------------------------------------------------------------------------
# Submit
# ------------------------------------------------------------------------------------------------
echo
echo "== submitting one envelope from inside the cluster =="

broker_driver_use_probe "$PROBE" || {
  echo "FAIL: $PROBE is not where this suite says it is" >&2
  exit 1
}
# Read by `broker_driver_run`, which wires it into the pod as PROBE_TENANT_NAMESPACE.
# shellcheck disable=SC2034
BROKER_DRIVER_TENANT_NS="$TENANT_NS"

broker_driver_apply_code "$K" "$NS" "$DRIVER_CM" || {
  echo "FAIL: could not stage the shipped transport code" >&2
  exit 1
}
# The untrusted keypair is not used by this probe, and the Secret is created anyway because
# `broker_driver_run` mounts it unconditionally — a pod referencing an absent Secret never starts.
# Cheaper than a second code path in the driver for the sake of one file.
broker_driver_untrusted_keypair "$K" "$NS" "$UNTRUSTED_SECRET" || {
  echo "FAIL: could not generate the placeholder keypair the driver pod mounts" >&2
  exit 1
}

# The foreign agent argument is this agent: nothing here presents a foreign credential, and passing
# a second agent that the fixtures never seeded would fail the token mint for no purpose.
driver_out="$(broker_driver_run "$K" "$NS" "$AGENT" "$AGENT" "$DRIVER_POD" "$DRIVER_CM" "$UNTRUSTED_SECRET")"
driver_rc=$?
if [ "$driver_rc" -ne 0 ]; then
  echo "DEFERRED: the driver pod could not be run to completion, so no envelope was ever submitted."
  echo "  An inability to run the experiment, not a property that failed (P10's distinction)."
  exit 3
fi

echo "$driver_out" | sed 's/^/  | /'

FLAT="$(printf '%s\n' "$driver_out" | python3 -c '
import json, sys

for line in sys.stdin:
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        r = json.loads(line)
    except ValueError:
        continue
    status = r.get("status")
    print("\t".join([
        r.get("scenario") or "",
        r.get("outcome") or "",
        "" if status is None else str(status),
        r.get("reason") or "",
        r.get("decision") or "",
        r.get("phase") or "",
        r.get("actionId") or "",
        r.get("namespace") or "",
        (r.get("detail") or "").replace("\t", " ")[:1000],
    ]))
')"

if [ -z "$FLAT" ]; then
  echo "DEFERRED: the driver pod produced no parseable probe output."
  exit 3
fi

field() { # <scenario> <1=outcome 2=status 3=reason 4=decision 5=phase 6=actionId 7=namespace 8=detail>
  printf '%s\n' "$FLAT" | awk -F'\t' -v s="$1" -v i="$(($2 + 1))" '$1 == s { print $i; exit }'
}

# ------------------------------------------------------------------------------------------------
# L2-0: the envelope was accepted
# ------------------------------------------------------------------------------------------------
echo
echo "== L2-0: the pipeline ACCEPTED the envelope (so there is something to read back) =="

sub_outcome="$(field shadow-submit 1)"
sub_status="$(field shadow-submit 2)"
sub_reason="$(field shadow-submit 3)"
action_id="$(field shadow-submit 6)"
record_ns="$(field shadow-submit 7)"
sub_detail="$(field shadow-submit 8)"

# The name the operation aimed at, taken from the probe's own `note` line rather than kept as a
# second copy here. A suite that hardcoded the name would still delete-check the right object today
# and the wrong one the day the probe's constant changed.
target_name="$(field target 8)"
target_ns="$(field target 7)"

if [ "$sub_outcome" != "http" ] || [ -z "$action_id" ]; then
  echo "DEFERRED: the envelope was not accepted, so no journal entry exists to judge."
  echo "  outcome='$sub_outcome' status='$sub_status' reason='$sub_reason' — $sub_detail"
  echo
  echo "  This is reported as could-not-run rather than as a failure of V-BRK-006 or V-REV-001,"
  echo "  because neither row is about admission. Diagnose here first: a 400 'invalid-envelope'"
  echo "  means the shipped builder and the broker's schema disagree (the trigger-source and"
  echo "  parentSpanId findings are both of that shape); a 403 from inside the executor means the"
  echo "  write overlay did not take; a 401 means this is broker-auth-l2.sh's problem, not this"
  echo "  suite's."
  $K -n "$NS" logs "pod/$broker_pod" --tail=40 2>/dev/null | sed 's/^/  broker| /'
  exit 3
fi
pass "the envelope was accepted — HTTP $sub_status, decision '$(field shadow-submit 4)', actionId $action_id in namespace ${record_ns:-<unnamed>}"

[ -n "$record_ns" ] || record_ns="$NS"
[ -n "$target_ns" ] || target_ns="$TENANT_NS"

# ------------------------------------------------------------------------------------------------
# L2-1: the record is in the API server
# ------------------------------------------------------------------------------------------------
echo
echo "== L2-1: the ActionRecord the broker named is in the API server =="

# THE OBJECT NAME IS NOT THE ACTION ID. `journal.RecordName` is `"ar-" + strings.ToLower(actionID)`
# (06 §4.3, k8s-operator/internal/journal/ulid.go) — lowercased because an object name must be a DNS
# subdomain, and a ULID is uppercase. This line asked for the raw id for as long as it has existed
# and could not have found the record against ANY commit; it went unnoticed because the only thing
# that had ever exercised it was `--negative-control`, which synthesises the record document and
# feeds it straight to the assertion block, never touching the lookup. An arm whose ¬ form skips the
# very statement under test is an arm nothing has measured.
#
# Derived here rather than read off the reply on purpose: the reply is the broker's claim, and the
# whole point of L2-1 is to check that claim against the API server. Deriving the name the same way
# the broker's own code derives it keeps the two joined by the rule, not by a returned string.
record_name="ar-$(printf '%s' "$action_id" | tr '[:upper:]' '[:lower:]')"

# Polled (P9). The reply is the broker's word that it wrote the record; the poll is the API server's.
# 30s because a durable write that has not landed in half a minute is not slow, it is absent — and
# the poll must not be generous enough to hide a broker that reports before it writes.
RECORD=""
deadline=$((SECONDS + 30))
while [ "$SECONDS" -lt "$deadline" ]; do
  RECORD="$($K -n "$record_ns" get actionrecord "$record_name" -o json 2>/dev/null)"
  [ -n "$RECORD" ] && break
  sleep 2
done

if [ -z "$RECORD" ]; then
  bad "the broker answered with actionId '$action_id' in namespace '$record_ns' and no ActionRecord named '$record_name' exists 30s later. The reply named a journal entry that was never written."
  echo
  echo "  what IS in $record_ns:"
  $K -n "$record_ns" get actionrecords 2>&1 | sed 's/^/    /'
else
  pass "ActionRecord $record_ns/$record_name exists — read from the API server, not from the reply body"
fi

# ------------------------------------------------------------------------------------------------
# Did the shadow mutate anything?
# ------------------------------------------------------------------------------------------------
if [ -n "$target_name" ] && $K -n "$target_ns" get configmap "$target_name" >/dev/null 2>&1; then
  target_exists=yes
elif [ -z "$target_name" ]; then
  target_exists=unknown
else
  target_exists=no
fi

# ------------------------------------------------------------------------------------------------
# L2-2 .. L2-5, over the record
# ------------------------------------------------------------------------------------------------
if [ -n "$RECORD" ]; then
  assert_record "$target_exists"
else
  echo
  echo "SKIPPING L2-2 through L2-5: there is no record to read. Those arms are NOT counted as passes;"
  echo "  the assertion count below will disagree with EXPECTED_ASSERTIONS and fail the run."
fi

# ------------------------------------------------------------------------------------------------
if [ "$assertions" -ne "$EXPECTED_ASSERTIONS" ]; then
  echo
  bad "only $assertions of $EXPECTED_ASSERTIONS assertions ran. The verdict below would be about arms that never executed."
fi

echo
echo "===================================================================="
if [ "$fail" -eq 0 ]; then
  echo " PROVEN: V-BRK-006 (L2 clause) · V-REV-001 (n=1) at L2"
  echo " An envelope built by the shipped client reached a deployed broker, and the journal entry it"
  echo " produced was durable before the mutation, recorded the shadow, carried a validated undo"
  echo " plan, and left the target object untouched."
  echo "===================================================================="
  exit 0
fi
echo " FAILED — see the FAIL lines above."
echo "===================================================================="
exit 1
