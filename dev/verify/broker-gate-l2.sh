#!/usr/bin/env bash
# V-REV-003 at L2 — "an action with no generatable undo plan is reclassified GATED and never
# auto-executes" (09 §6.5, 03 §4.1 step 6, 06 §4.2 step 6, 06 §4.3.1), asked of a DEPLOYED broker.
#
# `broker-execute-l2.sh` submits one envelope that works. `broker-refuse-l2.sh` submits two that are
# refused outright. This suite is the third outcome the pipeline has and the only one neither of
# those can reach: an envelope that is legitimate in every way — real credential, well-formed body,
# caller in scope, target inside the actor's granted namespace, every verb authorized — and is
# PARKED anyway, because 06 §4.3.1's strategy table cannot produce an inverse for it.
#
# TWO SUBMISSIONS, AND THE ROW IS THE DIFFERENCE BETWEEN THEM
#   A broker that gated EVERYTHING would satisfy V-REV-003's sentence perfectly and be worthless,
#   and that is not a hypothetical: three separate places in this pipeline downgrade a plan to
#   `none` when they meet an error — `checkRecreatable` when the reference index cannot answer,
#   `undo.Validate` when no dry-run client is wired, `generateOne` when a snapshot is missing. Each
#   fails closed, correctly, and each of them would make a one-submission suite green while proving
#   nothing about reclassification. So there are two, seconds apart, over the same credential and
#   the same namespace, differing in exactly one thing — whether an undo step exists:
#
#     F · no-undo-plan — `patch` an `apps/v1 Deployment` THAT DOES NOT EXIST. `undo.StrategyFor`
#         maps `patch` to `restore` for either existence; `execute.capture` narrows the NotFound to
#         `Existed: false` with a nil pre-state; and `generateOne`'s restore arm refuses on exactly
#         that — "no pre-state snapshot was supplied for an operation over an object that already
#         existed". `Undoable()` goes false, `classify`'s step 6 raises the class to gated, and
#         `stepGate` parks the action at step 7. Steps 8 through 11 never run.
#
#     C · undo-plan-control — `apply` an absent ConfigMap. Its inverse is `delete`, which IS a step;
#         `rollback.PlanDryRunner` treats a delete step's NotFound as "would apply", so the plan
#         validates, the class stays routine and the action is accepted and shadow-executed.
#
#   D-1 asserts the difference itself. F gated and C accepted is the reclassification; F gated and
#   C gated is a broker with a stuck gate, and that observation must not be able to read as green.
#
# WHY THE CONTROL IS A DIFFERENT VERB AND KIND, WHICH IS A REAL COST
#   The tightest control would be the same `patch` over a Deployment that DOES exist — one variable,
#   literally. It was rejected on the code. That plan's step is a server-side apply of the whole
#   captured pre-state under the AGENT's field manager, over an object created by this suite's
#   `kubectl apply` and therefore owned by a different one. `PlanDryRunner.dryRunApply` passes no
#   force flag, so a field-ownership conflict downgrades the plan and gates the control — for a
#   reason that is an artifact of how the fixture was made, not a property of the broker. A control
#   that can gate for a reason unrelated to the experiment is worse than one that differs in verb.
#
#   `apply` an absent ConfigMap is used instead because it is the one operation ALREADY PROVEN to
#   reach the accepting path on this cluster: it is `broker_execute_probe`'s target operation
#   character for character, and V-REV-001 is scored `pass` at L2 on the record it produced.
#   Borrowing a known-good control is the point of it being a control.
#
# WHY THE FAULT IS A DEPLOYMENT AND NOT A CONFIGMAP
#   `classify/floor.go`'s `statefulKinds` contains `{Group:"", Kind:"ConfigMap"}`, so any ConfigMap
#   operation able to produce this refusal would ALSO be gated by `RuleDestructiveStatefulDelete` or
#   a neighbour, and a gate with two independent causes cannot attribute itself to either. F-5 is
#   the arm that would go quiet: it reads the record's `spec.classification.reasons[]` and requires
#   `no-undo-plan` to be among them. `apps/Deployment` is on no floor list, so it is the only rule
#   available. The patch body is one annotation for the same reason — a patch touching
#   `securityContext`, `serviceAccountName` or a pod-security label would draw `RuleSecurityLoosen`
#   in alongside it.
#
# WHY THE ENVELOPES SEND `dryRun: false`, WHICH IS THIS SUITE'S ONE SHARP EDGE
#   06 §4.2 step 6's rule is `UndoPlanGateApplies(dryRun, present) = !dryRun && !present`: A DRY RUN
#   SUPPRESSES THE NO-UNDO-PLAN GATE, deliberately, and `pipeline.go`'s step 4 feeds it the
#   envelope's own value rather than the effective one for that exact reason. A `dryRun: true`
#   submission would still come back gated — via the brake's row 5, `BrakeRuleUndoPlanUnusable`, at
#   step 5 — and this suite would be scoring V-REV-003 on a rule that is not the one 03 §4.1 names.
#   Sending false also makes the row's second clause, "never auto-executes", a real request: the
#   caller is asking the broker to execute for real and the only thing between the ask and a write
#   is the gate.
#
#   WHAT KEEPS THAT SAFE IS SHADOW MODE, IMPOSED HERE AND READ BACK. Before either submission this
#   suite patches `spec.operations.dryRunOnly: true` onto the Agent CR and re-reads it from the API
#   server. `pipeline.Submit` computes `mayExecute = !env.DryRun && !shadowed(view)` — a one-way
#   composition no caller can clear — so every execution is forced to a server-side dry run and the
#   worst case of a broker that failed to gate is a shadow write. Phase 9's shape ("exercise it
#   end-to-end with no write authority anywhere; the broker runs every action in dry-run", 07 §2) is
#   preserved. If the read-back disagrees, the run is DEFERRED before anything is submitted.
#
# WHAT IS ASSERTED, in order:
#   P1a/P1b/AVAIL  the controller and this agent's broker are the build under test, and it is up.
#   F-1  the unplannable envelope came back 202 `gated`, phase `PendingApproval` — not accepted, and
#        not refused for some other reason that would happen to keep the object absent too.
#   F-2  the gated reply carries an actionId. A parked action IS journaled, and 06 §4.3's approval
#        path needs a handle; this is the arm that separates "gated" from "refused".
#   F-3  exactly one ActionRecord in the agent namespace carries the trace id the probe minted.
#   F-4  that record is a gated record: `status.phase` `PendingApproval`, class `gated`.
#   F-5  THE GATE IS ATTRIBUTABLE. `spec.classification.reasons[]` carries the rule `no-undo-plan`,
#        and `spec.classification.undoable` is false. Without this arm the row is satisfied by any
#        gate at all — a policy overlay, a floor rule, a caller-requested approval.
#   F-6  V-REV-003's second clause — NEVER AUTO-EXECUTES. The record carries no `status.applied`
#        and no `spec.preState` (`stepGate` nils it: a stale snapshot on a PendingApproval record is
#        an undo plan that would restore the wrong bytes). The pipeline stopped at step 7.
#   F-7  the Deployment still does not exist, read from the API server.
#   C-1  the control came back accepted, not gated. This is what makes F non-vacuous.
#   C-2  exactly one ActionRecord carries the control's trace id, in phase `DryRun`.
#   C-3  the control's plan was GENERATED AND VALIDATED: `undoable` true, `spec.undo.strategy`
#        `delete`, `validated` true, at least one step. The variable between the two runs, named.
#   C-4  the control's record carries NO `no-undo-plan` reason. A broker that attached the rule to
#        everything would pass F-5 for a reason F-5 cannot see.
#   C-5  shadow mode held: the record's `spec.dryRun` is true and the ConfigMap does not exist.
#   D-1  the differential: F gated, C accepted. Stated as its own arm so a stuck gate cannot pass.
#
# WHAT THIS DOES NOT CLAIM, so the next reader does not go looking
#   THE FAULT TARGET'S CONTINUED ABSENCE IS OVER-DETERMINED and F-7 is not what carries the row.
#     Shadow mode alone would produce it, and so would the gate, and the two are indistinguishable
#     from outside. "Never auto-executes" is carried by F-6 — the record has no `status.applied` and
#     no captured pre-state, which is a claim about how far the PIPELINE got and is false for any
#     broker that reached step 8. F-7 is the cheap direct half and is kept because a gated action
#     that somehow created its target is a live safety defect worth a line of its own.
#   THE APPROVAL PATH IS NOT EXERCISED. This suite parks an action and leaves it parked. Approving
#     it, watching the re-snapshot, and executing on the approval is 06 §4.3's own surface and needs
#     an ApprovalRoster with a decision in it — Phase 10, and V-REV-004's territory, not this row's.
#   `status.operations.dryRunOnly` IS NOT READ, because nothing writes it. The API type exists
#     (`OperationsStatus.DryRunOnly`) and no controller populates it, the same gap
#     `status.broker.journalReachable` has. The read-back below is therefore against `spec`, which
#     is also what `pipeline.shadowed` consults, so it is the field that actually decides. Asserting
#     the status field would be asserting a property no code has.
#   06 §4.4 ROW 5 IS NOT SEPARATELY SCORED. The brake raises an unusable plan to gated too, and on
#     this envelope it fires alongside the classifier — `undo-plan-unusable` appears in the record's
#     reasons next to `no-undo-plan`, and F-5 accepts the presence of the latter rather than the
#     absence of the former. Distinguishing the two paths is an L1 question and `undo_test.go` owns
#     it; at L2 both are the same binary refusing to guess.
#
# DESTRUCTIVE-TEST GUARD: scratch-GKE contexts only, anchored. This deletes and re-applies the Agent
# CR `platform-agent` in `kubeagents-system`, PATCHES ITS SPEC into shadow mode, and grants its
# actor identity write authority over a throwaway tenant namespace — and then submits two envelopes
# that ask, in earnest, to be executed for real. Shadow mode is the only thing standing between the
# second of those and a write. On the live install that is a test reconfiguring the fleet's brake.
# Exit: 0 = PROVEN · 1 = FAILED · 2 = refused target / P10 · 3 = DEFERRED (P1 or the run itself).
# Usage: dev/verify/broker-gate-l2.sh [kube-context]
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions):
#   P1 image-under-test: the controller (`control-plane=controller-manager`) AND this agent's BROKER
#      pod (`kube-agents/agent=<agent>,kube-agents/role=actor`) — both, in full. Every arm is a claim
#      about what a BINARY did: which step raised the class, whether the write-ahead Create at step 8
#      was reached, what `stepGate` put in the record and what it took back out. A broker one
#      generation behind would answer all thirteen arms about the previous build's pipeline and they
#      would read green. Unverifiable → rc 3.
#   P3 admission-recreate: the Agent CR is deleted with `--wait=true` and re-applied on every run, so
#      the broker Deployment, its mesh Certificates and the pair NetworkPolicies are rendered by the
#      controller running NOW — and so that the `dryRunOnly` patch below is applied to a CR this run
#      created rather than to whatever a previous run left. The broker pod is resolved through
#      `p3_pod_of_deploy`, by ownership, so a pod from the previous generation can never be read as
#      this one's. The driver pod, its ConfigMap and the write overlay are created and undone inside
#      the run. The ActionRecords are deliberately NOT recreated — they are the output — and are
#      disambiguated by the trace ids the probe minted in THIS run.
#   P6 runtime-authoritative: every assertion reads objects from the API server. The shadow-mode
#      read-back is a `kubectl get` of the CR's own spec, never the patch's exit code. The reply-shape
#      arms (F-1, F-2, C-1) are explicitly ABOUT the reply and are cross-checked against the journal
#      by F-3 through F-6 and C-2 through C-5. The driver's whole environment — endpoint, SAN,
#      identity, token path, TLS dir — comes off the RENDERED agent Deployment through
#      `broker_driver_env`. The actor's authority is read with `kubectl auth can-i` against the live
#      authorizer, never inferred from the RBAC documents the fixture applied.
set -uo pipefail

# MODES. `live` submits to a real broker and is what every claim above is about.
# `--negative-control` replays the three assertion blocks against synthesised observations that a
# MISBEHAVING broker would have produced, and requires each to go red. See run_negative_control.
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

# The tenant both envelopes aim at. Its own namespace, so this suite and its neighbours cannot leave
# each other evidence — `broker-execute-l2.sh` and `broker-refuse-l2.sh` each have their own, and a
# shared one would make "the object does not exist" a claim about whoever ran last.
TENANT_NS=kubeagents-gate-tenant

DRIVER_POD=broker-gate-l2-driver
DRIVER_CM=broker-gate-l2-code
UNTRUSTED_SECRET=broker-gate-l2-untrusted
PROBE=dev/verify/fixtures/broker_gate_probe.py

# Kept in step with the probe's own constants, and cross-checked against the `note` lines it emits
# rather than trusted: the names below are read out of the transcript, and these are only the
# fallback for a run that produced none. A suite holding the sole copy would existence-check the
# right object today and the wrong one the day the probe's constant changed.
UNPLANNABLE_NAME=broker-gate-l2-absent-deployment
CONTROL_NAME=broker-gate-l2-control-target

# NEGATIVE CONTROL DOES NOT EXERCISE: (LSN-060.) The control SYNTHESISES every observation — the two
# HTTP replies, the record counts, the record documents, the two existence answers — and hands them
# to the assertion blocks directly, so everything upstream of the assertions is unmeasured by it:
#   - the envelope build and the two HTTP POSTs to the deployed broker. A synthesised 202 is not a
#     broker's answer; the ¬ arm cannot tell a running broker from an absent one
#   - THE SHADOW-MODE PATCH AND ITS READ-BACK, which is the entire safety argument for sending
#     `dryRun: false`. A patch that silently applied nothing would leave the live run submitting two
#     executable envelopes, and the ¬ arm 22/22 green
#   - THE ABSENCE OF THE FAULT TARGET, which is the entire mechanism of scenario F. If the Deployment
#     existed, `generateOne` would produce a restore step, the plan would validate, and F would be a
#     second control — the ¬ arm never runs the pre-flight `kubectl get` that rules that out
#   - the trace-id search of the API server (F-3, C-2). The control passes a COUNT; it never runs the
#     list-and-match that produces one. This is `broker-execute-l2.sh`'s exact scar — a lookup line
#     that could not have worked against any commit, green in ¬ for weeks because ¬ skipped it
#   - the `auth can-i` preconditions that establish the actor is entitled to both operations, without
#     which F would be a 403 at step 3 wearing a gate's clothes
#   - the P1 digest arms and the broker's Availability, which run before either mode
# What it does prove, and all it proves: the three assertion blocks are not always-green — each of
# the defects below is caught by the arm that targets it, named in the output.
fail=0

# EVERY ARM IS COUNTED, AND THE COUNT IS ASSERTED AT THE END, for `broker-auth-l2.sh`'s reason:
# `fail` stays 0 when no assertion runs, so a suite that skipped its whole body would print a PROVEN
# banner. Change EXPECTED_ASSERTIONS deliberately, in the same commit as the arm.
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

# 2 x P1 + broker Available + F-1..F-7 + C-1..C-5 + D-1.
EXPECTED_ASSERTIONS=16

# ------------------------------------------------------------------------------------------------
# jrec <dotted key> — one field out of $RECORD, via python. `broker-execute-l2.sh`'s helper,
# verbatim in behaviour: lists and dicts report their LENGTH, which is what lets an arm ask "how
# many undo steps" and "is there a preState" through one accessor.
# ------------------------------------------------------------------------------------------------
jrec() {
  printf '%s' "$RECORD" | python3 -c '
import json, sys
try:
    doc = json.loads(sys.stdin.read() or "{}")
except ValueError:
    print("")
    sys.exit(0)
for part in sys.argv[1].split("."):
    if isinstance(doc, dict):
        doc = doc.get(part)
    elif isinstance(doc, list) and part.isdigit() and int(part) < len(doc):
        doc = doc[int(part)]
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

# ------------------------------------------------------------------------------------------------
# jrules — the RULE NAMES on $RECORD's classification reasons, space-separated.
#
# A second accessor rather than `jrec spec.classification.reasons`, which would report the LENGTH of
# the list and answer "three reasons" to a question that is "which three". F-5 and C-4 are both
# about which rule fired, and a count cannot tell `no-undo-plan` from `security-loosen`.
# ------------------------------------------------------------------------------------------------
jrules() {
  printf '%s' "$RECORD" | python3 -c '
import json, sys
try:
    doc = json.loads(sys.stdin.read() or "{}")
except ValueError:
    doc = {}
cls = ((doc.get("spec") or {}).get("classification") or {})
print(" ".join(str((r or {}).get("rule", "")) for r in (cls.get("reasons") or []) if r))
'
}

# has_rule <rules> <rule> — exact word match, so `no-undo-plan` is not satisfied by a future
# `no-undo-plan-needed`. Whitespace-padded containment rather than a case glob for that reason.
has_rule() {
  case " $1 " in
    *" $2 "*) return 0 ;;
    *) return 1 ;;
  esac
}

# present <jrec-result> — is there anything there? Empty means the key was absent; `0` means the key
# was an EMPTY list or dict, which for `spec.preState` and `status.applied` is the same statement
# and must not be read as "one entry".
present() {
  [ -n "$1" ] && [ "$1" != "0" ]
}

# ------------------------------------------------------------------------------------------------
# F — the no-undo-plan assertion block
#
#   assert_gated <status> <decision> <phase> <action_id> <n_records> <target_exists>
#   $RECORD is the matched ActionRecord as JSON, or empty when none matched.
#
# A function because `--negative-control` replays exactly these arms against observations nobody's
# broker produced. Every derivation stays inside it, so the ¬ arm exercises the derivation too
# rather than a simplified copy that could agree with a misbehaving broker for a reason the live
# path would not.
# ------------------------------------------------------------------------------------------------
assert_gated() {
  local status="$1" decision="$2" phase="$3" action_id="$4" n_records="$5" target_exists="$6"
  local rphase class undoable rules pre applied

  rphase="$(jrec status.phase)"
  class="$(jrec spec.classification.class)"
  undoable="$(jrec spec.classification.undoable)"
  rules="$(jrules)"
  pre="$(jrec spec.preState)"
  applied="$(jrec status.applied)"

  echo
  echo "== F-1: the unplannable envelope came back GATED =="
  if [ -z "$status" ]; then
    bad "F-1: the probe reported no HTTP status for the unplannable submission. Nothing was observed, so nothing is being judged."
  elif [ "$decision" = "accepted" ]; then
    bad "F-1: THE UNPLANNABLE ACTION WAS NOT GATED — HTTP $status, decision 'accepted'. The broker took an action it cannot roll back and ran it. 03 §4.1 step 6 and 06 §4.2 step 6 both say it must be parked for a human; neither happened."
  elif [ "$decision" != "gated" ]; then
    bad "F-1: the reply's decision is '${decision:-<none>}', want 'gated' (HTTP $status). A refusal is not a gate — it leaves nothing for a human to approve, and it would keep the object absent for a reason V-REV-003 is not about."
  elif [ "$status" != "202" ]; then
    bad "F-1: decision 'gated' arrived with HTTP $status, want 202. stepGate answers StatusAccepted; another code means this reply came from somewhere other than the gate."
  elif [ "$phase" != "PendingApproval" ]; then
    bad "F-1: the reply's phase is '${phase:-<none>}', want 'PendingApproval'. A gated action that reports any other phase is telling its caller the wrong thing about what to wait for."
  else
    pass "F-1: HTTP 202, decision 'gated', phase 'PendingApproval' — the action was parked, not run and not refused"
  fi

  echo
  echo "== F-2: the gated reply names the record it parked =="
  if [ -z "$action_id" ]; then
    bad "F-2: the gated reply carries no actionId. A parked action is journaled and 06 §4.3's approval path needs a handle on it; a gate nobody can name is a gate nobody can lift."
  else
    pass "F-2: the reply names actionId '$action_id' — parked and journaled, which is what separates a gate from a refusal"
  fi

  echo
  echo "== F-3: exactly one ActionRecord carries this submission's trace id =="
  if [ "$n_records" = "0" ]; then
    bad "F-3: no ActionRecord carries the trace id this submission sent. stepGate Creates the record BEFORE it answers 202, so a 202 with no record means the reply was written by something that never journaled."
  elif [ "$n_records" != "1" ]; then
    bad "F-3: $n_records ActionRecords carry this submission's trace id. One submission, one record; more than one means something wrote a second entry for the same attempt."
  else
    pass "F-3: exactly one ActionRecord carries the trace id, found by listing the API server rather than by trusting the reply"
  fi

  echo
  echo "== F-4: that record is a gated record =="
  if [ "$n_records" != "1" ]; then
    bad "F-4: there is no single record to judge (n=$n_records), so the gate's shape was not read."
  elif [ "$class" != "gated" ]; then
    bad "F-4: the record's spec.classification.class is '${class:-<none>}', want 'gated'. The reply said gated and the journal says otherwise, and the journal is what an auditor reads."
  elif [ "$rphase" != "PendingApproval" ]; then
    bad "F-4: the record's status.phase is '${rphase:-<none>}', want 'PendingApproval'. A record in any other phase claims an outcome the action never had."
  else
    pass "F-4: class 'gated', status.phase 'PendingApproval' — the journal agrees with the reply"
  fi

  echo
  echo "== F-5: V-REV-003 — the gate is attributable to the MISSING UNDO PLAN =="
  if [ "$n_records" != "1" ]; then
    bad "F-5: there is no single record to judge (n=$n_records), so the gate was not attributed to anything."
  elif ! has_rule "$rules" no-undo-plan; then
    bad "F-5: the record's classification reasons are '${rules:-<none>}' and none of them is 'no-undo-plan'. The action was gated by some OTHER rule — a policy overlay, a floor rule, a caller-requested approval — and a gate this suite cannot attribute is not evidence for V-REV-003."
  elif [ "$undoable" != "false" ]; then
    bad "F-5: spec.classification.undoable is '${undoable:-<none>}', want false. The record carries the no-undo-plan reason and then claims the action is undoable, so one of the two is lying about the same plan."
  else
    pass "F-5: reasons carry 'no-undo-plan' (with '$rules') and undoable is false — the reclassification is 06 §4.2 step 6's, named in the journal"
  fi

  echo
  echo "== F-6: V-REV-003 — NEVER AUTO-EXECUTES; the pipeline stopped at step 7 =="
  if [ "$n_records" != "1" ]; then
    bad "F-6: there is no single record to judge (n=$n_records), so V-REV-003's no-execution arm did not run."
  elif present "$applied"; then
    bad "F-6: the record carries status.applied ($applied entries). Something was applied — in shadow mode that means a server-side dry run the API server authorized for real — after a gate that was supposed to precede it. This is the row's second clause failing."
  elif present "$pre"; then
    bad "F-6: the record carries spec.preState ($pre entries). stepGate nils it deliberately (a stale snapshot on a PendingApproval record is an undo plan that restores the wrong bytes), so pre-state here means step 8 ran and the action was not parked at step 7."
  else
    pass "F-6: no status.applied and no spec.preState — steps 8 through 11 never ran, so nothing was captured and nothing was executed"
  fi

  echo
  echo "== F-7: the target object does not exist =="
  case "$target_exists" in
    no) pass "F-7: the Deployment does not exist — the cheap direct half, read from the API server (see 'over-determined' in the header: F-6 is what carries the clause)" ;;
    yes) bad "F-7: THE TARGET DEPLOYMENT EXISTS. A gated action created its own target. Nothing in this suite creates it and a patch cannot, so this is a live safety defect and not a reporting one." ;;
    *) bad "F-7: could not determine whether the target Deployment exists (got '$target_exists'); the no-mutation arm did not run" ;;
  esac
}

# ------------------------------------------------------------------------------------------------
# C — the control assertion block
#
#   assert_control <status> <decision> <phase> <action_id> <n_records> <target_exists>
#   $RECORD is the matched ActionRecord as JSON, or empty when none matched.
# ------------------------------------------------------------------------------------------------
assert_control() {
  local status="$1" decision="$2" phase="$3" action_id="$4" n_records="$5" target_exists="$6"
  local rphase undoable strategy validated steps rules dry

  rphase="$(jrec status.phase)"
  undoable="$(jrec spec.classification.undoable)"
  strategy="$(jrec spec.undo.strategy)"
  validated="$(jrec spec.undo.validated)"
  steps="$(jrec spec.undo.steps)"
  rules="$(jrules)"
  dry="$(jrec spec.dryRun)"

  echo
  echo "== C-1: the control was ACCEPTED, which is what makes the gate above mean something =="
  if [ -z "$status" ]; then
    bad "C-1: the probe reported no HTTP status for the control submission. Nothing was observed, so nothing is being judged."
  elif [ "$decision" = "gated" ]; then
    bad "C-1: THE CONTROL WAS GATED TOO. An operation whose inverse is a single delete step was parked for a human. A broker that gates everything satisfies V-REV-003's sentence and is useless, and scenario F above is not evidence of anything while this is true."
  elif [ "$status" -lt 200 ] || [ "$status" -ge 300 ]; then
    bad "C-1: the control was refused — HTTP $status, decision '${decision:-<none>}', actionId '${action_id:-<none>}'. The control is a legitimate envelope over an authorized target; a refusal means the experiment could not be staged, and scenario F's gate cannot be attributed to the undo plan while the accepting path is unreachable."
  elif [ "$decision" != "accepted" ]; then
    bad "C-1: the control's decision is '${decision:-<none>}', want 'accepted' (HTTP $status, phase '${phase:-<none>}')."
  else
    pass "C-1: HTTP $status, decision 'accepted', phase '${phase:-<none>}' — the same broker, credential and namespace took an envelope it COULD invert"
  fi

  echo
  echo "== C-2: the control was journaled as a completed shadow run =="
  if [ "$n_records" = "0" ]; then
    bad "C-2: no ActionRecord carries the control's trace id. The accepted reply claims an action that left no journal entry."
  elif [ "$n_records" != "1" ]; then
    bad "C-2: $n_records ActionRecords carry the control's trace id. One submission, one record."
  elif [ "$rphase" != "DryRun" ]; then
    bad "C-2: the control's record is in phase '${rphase:-<none>}', want 'DryRun'. Shadow mode terminates an executed action at PhaseDryRun; any other phase means it did not go down the path this control exists to open."
  else
    pass "C-2: one record, phase 'DryRun' — the control reached the executor and terminated where shadow mode terminates"
  fi

  echo
  echo "== C-3: the control's undo plan GENERATED AND VALIDATED — the variable between the two runs =="
  if [ "$n_records" != "1" ]; then
    bad "C-3: there is no control record to judge (n=$n_records), so the plan was not read."
  elif [ "$undoable" != "true" ]; then
    bad "C-3: the control record's spec.classification.undoable is '${undoable:-<none>}', want true. Then BOTH runs were unplannable and the difference F-5 attributes the gate to does not exist."
  elif [ "$strategy" != "delete" ]; then
    bad "C-3: spec.undo.strategy is '${strategy:-<none>}', want 'delete'. 06 §4.3.1 inverts an apply over an absent object with a delete; another strategy here means StrategyFor read a different existence than the snapshot recorded."
  elif [ "$validated" != "true" ]; then
    bad "C-3: spec.undo.validated is '${validated:-<none>}', want true. An unvalidated plan is one PlanDryRunner could not confirm would apply, which is the same state scenario F is in — the two runs would not be differing."
  elif [ -z "$steps" ] || [ "$steps" -lt 1 ]; then
    bad "C-3: the control's plan carries ${steps:-no} step(s). A validated strategy with nothing to run is a plan in name only."
  else
    pass "C-3: undoable true, strategy 'delete', validated, $steps step(s) — the plan the fault could not produce"
  fi

  echo
  echo "== C-4: the control carries no no-undo-plan reason =="
  if [ "$n_records" != "1" ]; then
    bad "C-4: there is no control record to judge (n=$n_records), so the control's reasons were not read."
  elif has_rule "$rules" no-undo-plan; then
    bad "C-4: the control's record carries a 'no-undo-plan' reason (reasons: '$rules') for an action whose plan validated. The rule is being attached to everything, so F-5 passed for a reason F-5 cannot see."
  else
    pass "C-4: the control's reasons are '${rules:-<none>}' and none is 'no-undo-plan' — the rule fires on the fault and not on the control"
  fi

  echo
  echo "== C-5: shadow mode held over the one run that reached the executor =="
  if [ "$n_records" != "1" ]; then
    bad "C-5: there is no control record to judge (n=$n_records), so shadow mode was not confirmed at the record."
  elif [ "$dry" != "true" ]; then
    bad "C-5: the control record's spec.dryRun is '${dry:-<none>}', want true. The envelope asked to execute for real and the broker recorded that it did — shadow mode was not in force, and the only thing this suite relied on to make 'dryRun: false' safe was not there."
  else
    case "$target_exists" in
      no) pass "C-5: spec.dryRun true and the ConfigMap does not exist — the accepted action was executed as a server-side dry run, as 07 §2 requires of Phase 9" ;;
      yes) bad "C-5: THE CONTROL CONFIGMAP EXISTS. An accepted action was executed for real while the record says spec.dryRun true, so shadow mode was recorded and not applied." ;;
      *) bad "C-5: could not determine whether the control ConfigMap exists (got '$target_exists'); the no-mutation arm did not run" ;;
    esac
  fi
}

# ------------------------------------------------------------------------------------------------
# D — the differential
#
#   assert_differential <fault-decision> <control-decision>
#
# Its own arm, and not an inference a reader is left to make from F-1 and C-1 sitting near each
# other. V-REV-003 is a statement about a DIFFERENCE, and the two ways of failing it that a
# per-scenario arm cannot see are "everything is gated" and "nothing is".
# ------------------------------------------------------------------------------------------------
assert_differential() {
  local f="$1" c="$2"

  echo
  echo "== D-1: the reclassification is the difference between the two submissions =="
  if [ "$f" = "gated" ] && [ "$c" = "accepted" ]; then
    pass "D-1: the unplannable envelope was gated and the plannable one was accepted — same broker, same credential, same namespace, seconds apart. The generatable undo plan is the only thing that differed."
  elif [ -n "$f" ] && [ "$f" = "$c" ]; then
    bad "D-1: both submissions came back '$f'. V-REV-003 is scored on the DIFFERENCE between them, and there is none: this broker treats an invertible action and an uninvertible one identically."
  else
    bad "D-1: the differential is inverted or incomplete — fault '${f:-<none>}', control '${c:-<none>}', want 'gated' and 'accepted'."
  fi
}

# ------------------------------------------------------------------------------------------------
# The `¬` arm
# ------------------------------------------------------------------------------------------------
# WHY SYNTHESISED OBSERVATIONS AND NOT A MUTATION. Making a real broker execute an unplannable
# action, or attach `no-undo-plan` to a plan that validated, means editing the Go pipeline —
# `dev/mutate.py`'s job at L1, and not something an L2 suite can stage against a deployed binary.
# What this arm proves is the thing an L2 suite CAN get wrong on its own: that the assertion blocks
# distinguish a correct gate from a broken one at all. What it deliberately does NOT prove is listed
# in full at the top of this file.
#
# EACH MUTANT MUST BE CAUGHT BY THE ARM THAT TARGETS IT ([[LSN-035]]). Every row carries a needle and
# counts as caught only when a FAIL line CONTAINS it — not merely when something somewhere went red.
# Without that, breaking `jrec` would "catch" every mutant at once by failing every arm, and the
# control would read green while asserting that the suite is broken.
#
# THE VERDICT IS READ OFF THE OUTPUT, NEVER OFF `$fail`: the assertion blocks run inside a command
# substitution, which is a subshell, so every `fail=1` they set dies with it.
GOOD_FAULT_RECORD='{"spec":{"dryRun":true,"classification":{"class":"gated","undoable":false,"reasons":[{"rule":"no-undo-plan","class":"gated","detail":"no undo plan could be generated for this envelope"},{"rule":"undo-plan-unusable","class":"gated","detail":"no pre-state snapshot was supplied for an operation over an object that already existed"}]},"undo":{"strategy":"none","validated":false,"steps":[]}},"status":{"phase":"PendingApproval"}}'
GOOD_CONTROL_RECORD='{"spec":{"dryRun":true,"classification":{"class":"routine","undoable":true,"reasons":[{"rule":"default-routine","class":"routine","detail":"nothing in this envelope raises it"}]},"undo":{"strategy":"delete","validated":true,"steps":[{"op":"delete"}]}},"status":{"phase":"DryRun"}}'

run_negative_control() {
  local name expect needle out n_fail rc=0 total=0 caught=0
  local doc f1 f2 f3 f4 f5 f6 d1 d2

  # --- F ------------------------------------------------------------------------------------------
  # name | expect | needle | record-json | status | decision | phase | actionId | n | target
  while IFS='|' read -r name expect needle doc f1 f2 f3 f4 f5 f6; do
    [ -n "$name" ] || continue
    total=$((total + 1))
    RECORD="$doc"
    out="$(assert_gated "$f1" "$f2" "$f3" "$f4" "$f5" "$f6" 2>&1)"
    n_fail="$(printf '%s\n' "$out" | grep -c '^FAIL:')"
    if [ "$expect" = green ]; then
      if [ "$n_fail" -eq 0 ]; then
        echo "  ok   $name — the correct observation passes, so the arms below are not always-red"
        caught=$((caught + 1))
      else
        echo "  MISS $name — a CORRECT observation was failed $n_fail time(s); every mutant below would be caught for the wrong reason"
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
  done <<CASES
F-baseline|green|-|$GOOD_FAULT_RECORD|202|gated|PendingApproval|ar-01jzz|1|no
F-auto-executed|red|WAS NOT GATED|$GOOD_FAULT_RECORD|202|accepted|DryRun|ar-01jzz|1|no
F-no-status|red|no HTTP status|$GOOD_FAULT_RECORD||gated|PendingApproval|ar-01jzz|1|no
F-refused-instead|red|decision is 'rejected'|$GOOD_FAULT_RECORD|403|rejected||{}|1|no
F-wrong-code|red|want 202|$GOOD_FAULT_RECORD|200|gated|PendingApproval|ar-01jzz|1|no
F-wrong-reply-phase|red|the reply's phase is|$GOOD_FAULT_RECORD|202|gated|Pending|ar-01jzz|1|no
F-no-actionid|red|carries no actionId|$GOOD_FAULT_RECORD|202|gated|PendingApproval||1|no
F-not-journaled|red|no ActionRecord carries the trace id|$GOOD_FAULT_RECORD|202|gated|PendingApproval|ar-01jzz|0|no
F-two-records|red|2 ActionRecords carry|$GOOD_FAULT_RECORD|202|gated|PendingApproval|ar-01jzz|2|no
F-wrong-class|red|classification.class is 'routine'|{"spec":{"classification":{"class":"routine","undoable":false,"reasons":[{"rule":"no-undo-plan"}]}},"status":{"phase":"PendingApproval"}}|202|gated|PendingApproval|ar-01jzz|1|no
F-wrong-record-phase|red|record's status.phase is 'Executing'|{"spec":{"classification":{"class":"gated","undoable":false,"reasons":[{"rule":"no-undo-plan"}]}},"status":{"phase":"Executing"}}|202|gated|PendingApproval|ar-01jzz|1|no
F-gated-for-another-rule|red|none of them is 'no-undo-plan'|{"spec":{"classification":{"class":"gated","undoable":false,"reasons":[{"rule":"security-loosen"},{"rule":"caller-requested-approval"}]}},"status":{"phase":"PendingApproval"}}|202|gated|PendingApproval|ar-01jzz|1|no
F-no-reasons-at-all|red|none of them is 'no-undo-plan'|{"spec":{"classification":{"class":"gated","undoable":false}},"status":{"phase":"PendingApproval"}}|202|gated|PendingApproval|ar-01jzz|1|no
F-claims-undoable|red|undoable is 'true'|{"spec":{"classification":{"class":"gated","undoable":true,"reasons":[{"rule":"no-undo-plan"}]}},"status":{"phase":"PendingApproval"}}|202|gated|PendingApproval|ar-01jzz|1|no
F-something-applied|red|carries status.applied|{"spec":{"classification":{"class":"gated","undoable":false,"reasons":[{"rule":"no-undo-plan"}]}},"status":{"phase":"PendingApproval","applied":[{"kind":"Deployment"}]}}|202|gated|PendingApproval|ar-01jzz|1|no
F-prestate-kept|red|carries spec.preState|{"spec":{"classification":{"class":"gated","undoable":false,"reasons":[{"rule":"no-undo-plan"}]},"preState":[{"kind":"Deployment"}]},"status":{"phase":"PendingApproval"}}|202|gated|PendingApproval|ar-01jzz|1|no
F-target-created|red|THE TARGET DEPLOYMENT EXISTS|$GOOD_FAULT_RECORD|202|gated|PendingApproval|ar-01jzz|1|yes
F-target-unknown|red|could not determine whether the target Deployment exists|$GOOD_FAULT_RECORD|202|gated|PendingApproval|ar-01jzz|1|unknown
CASES

  # --- C ------------------------------------------------------------------------------------------
  # name | expect | needle | record-json | status | decision | phase | actionId | n | target
  while IFS='|' read -r name expect needle doc f1 f2 f3 f4 f5 f6; do
    [ -n "$name" ] || continue
    total=$((total + 1))
    RECORD="$doc"
    out="$(assert_control "$f1" "$f2" "$f3" "$f4" "$f5" "$f6" 2>&1)"
    n_fail="$(printf '%s\n' "$out" | grep -c '^FAIL:')"
    if [ "$expect" = green ]; then
      if [ "$n_fail" -eq 0 ]; then
        echo "  ok   $name — the correct observation passes, so the arms below are not always-red"
        caught=$((caught + 1))
      else
        echo "  MISS $name — a CORRECT observation was failed $n_fail time(s)"
        printf '%s\n' "$out" | grep '^FAIL:' | sed 's/^/       /'
        rc=1
      fi
    elif printf '%s\n' "$out" | grep '^FAIL:' | grep -qF "$needle"; then
      echo "  ok   $name — caught by the arm that targets it ('$needle')"
      caught=$((caught + 1))
    else
      echo "  MISS $name — went red $n_fail time(s) but no FAIL line mentions '$needle'"
      printf '%s\n' "$out" | grep '^FAIL:' | sed 's/^/       /'
      rc=1
    fi
  done <<CCASES
C-baseline|green|-|$GOOD_CONTROL_RECORD|202|accepted|DryRun|ar-01jzy|1|no
C-gated-too|red|THE CONTROL WAS GATED TOO|$GOOD_CONTROL_RECORD|202|gated|PendingApproval|ar-01jzy|1|no
C-no-status|red|no HTTP status|$GOOD_CONTROL_RECORD||accepted|DryRun|ar-01jzy|1|no
C-refused|red|the control was refused|$GOOD_CONTROL_RECORD|403|rejected||{}|1|no
C-odd-decision|red|the control's decision is 'deferred'|$GOOD_CONTROL_RECORD|202|deferred|DryRun|ar-01jzy|1|no
C-not-journaled|red|no ActionRecord carries the control's trace id|$GOOD_CONTROL_RECORD|202|accepted|DryRun|ar-01jzy|0|no
C-two-records|red|2 ActionRecords carry the control's trace id|$GOOD_CONTROL_RECORD|202|accepted|DryRun|ar-01jzy|2|no
C-wrong-phase|red|want 'DryRun'|{"spec":{"dryRun":true,"classification":{"undoable":true,"reasons":[]},"undo":{"strategy":"delete","validated":true,"steps":[{"op":"delete"}]}},"status":{"phase":"Verified"}}|202|accepted|Verified|ar-01jzy|1|no
C-not-undoable|red|undoable is 'false'|{"spec":{"dryRun":true,"classification":{"undoable":false,"reasons":[]},"undo":{"strategy":"delete","validated":true,"steps":[{"op":"delete"}]}},"status":{"phase":"DryRun"}}|202|accepted|DryRun|ar-01jzy|1|no
C-gave-up-planning|red|spec.undo.strategy is 'none'|{"spec":{"dryRun":true,"classification":{"undoable":true,"reasons":[]},"undo":{"strategy":"none","validated":true,"steps":[{"op":"delete"}]}},"status":{"phase":"DryRun"}}|202|accepted|DryRun|ar-01jzy|1|no
C-unvalidated|red|spec.undo.validated is 'false'|{"spec":{"dryRun":true,"classification":{"undoable":true,"reasons":[]},"undo":{"strategy":"delete","validated":false,"steps":[{"op":"delete"}]}},"status":{"phase":"DryRun"}}|202|accepted|DryRun|ar-01jzy|1|no
C-stepless|red|step(s). A validated strategy|{"spec":{"dryRun":true,"classification":{"undoable":true,"reasons":[]},"undo":{"strategy":"delete","validated":true,"steps":[]}},"status":{"phase":"DryRun"}}|202|accepted|DryRun|ar-01jzy|1|no
C-rule-on-everything|red|carries a 'no-undo-plan' reason|{"spec":{"dryRun":true,"classification":{"undoable":true,"reasons":[{"rule":"no-undo-plan"}]},"undo":{"strategy":"delete","validated":true,"steps":[{"op":"delete"}]}},"status":{"phase":"DryRun"}}|202|accepted|DryRun|ar-01jzy|1|no
C-shadow-not-recorded|red|spec.dryRun is 'false'|{"spec":{"dryRun":false,"classification":{"undoable":true,"reasons":[]},"undo":{"strategy":"delete","validated":true,"steps":[{"op":"delete"}]}},"status":{"phase":"DryRun"}}|202|accepted|DryRun|ar-01jzy|1|no
C-target-created|red|THE CONTROL CONFIGMAP EXISTS|$GOOD_CONTROL_RECORD|202|accepted|DryRun|ar-01jzy|1|yes
C-target-unknown|red|could not determine whether the control ConfigMap exists|$GOOD_CONTROL_RECORD|202|accepted|DryRun|ar-01jzy|1|unknown
CCASES

  # --- D ------------------------------------------------------------------------------------------
  # name | expect | needle | fault-decision | control-decision
  while IFS='|' read -r name expect needle d1 d2; do
    [ -n "$name" ] || continue
    total=$((total + 1))
    out="$(assert_differential "$d1" "$d2" 2>&1)"
    n_fail="$(printf '%s\n' "$out" | grep -c '^FAIL:')"
    if [ "$expect" = green ]; then
      if [ "$n_fail" -eq 0 ]; then
        echo "  ok   $name — the correct observation passes"
        caught=$((caught + 1))
      else
        echo "  MISS $name — a CORRECT observation was failed $n_fail time(s)"
        printf '%s\n' "$out" | grep '^FAIL:' | sed 's/^/       /'
        rc=1
      fi
    elif printf '%s\n' "$out" | grep '^FAIL:' | grep -qF "$needle"; then
      echo "  ok   $name — caught by the arm that targets it ('$needle')"
      caught=$((caught + 1))
    else
      echo "  MISS $name — went red $n_fail time(s) but no FAIL line mentions '$needle'"
      printf '%s\n' "$out" | grep '^FAIL:' | sed 's/^/       /'
      rc=1
    fi
  done <<'DCASES'
D-baseline|green|-|gated|accepted
D-everything-gated|red|both submissions came back 'gated'|gated|gated
D-nothing-gated|red|both submissions came back 'accepted'|accepted|accepted
D-inverted|red|the differential is inverted|accepted|gated
D-fault-missing|red|the differential is inverted||accepted
DCASES

  echo
  echo "negative control: $caught/$total"
  return $rc
}

if [ "$MODE" = negative-control ]; then
  echo "== broker-gate-l2.sh --negative-control: do the assertion blocks tell a correct gate from a broken one? =="
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
    echo "  This suite deletes and re-applies the Agent CR '$AGENT' in $NS, PATCHES ITS SPEC into" >&2
    echo "  shadow mode, widens its actor identity over a throwaway namespace, and then submits two" >&2
    echo "  envelopes that ask to be executed for real — shadow mode is the only thing standing" >&2
    echo "  between the second of those and a write. On the live install that is a test" >&2
    echo "  reconfiguring the fleet's brake." >&2
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
  actor_overlay_revoke_write "$K" "$TENANT_NS" >/dev/null 2>&1
  actor_overlay_revoke "$K" "$TENANT_NS" >/dev/null 2>&1
  broker_driver_delete "$K" "$NS" "$DRIVER_POD" "$DRIVER_CM" "$UNTRUSTED_SECRET"
  # The Agent CR goes rather than being patched back out of shadow mode. Un-patching would leave a
  # CR whose spec is this suite's idea of the default; deleting it means the next thing to apply the
  # manifest gets the manifest's own spec, which is the only version anyone has reviewed.
  $K -n "$NS" delete agent "$AGENT" --ignore-not-found --wait=false >/dev/null 2>&1
  echo
  echo "CLEANED UP: the overlays are revoked, the driver pod and its ConfigMap are gone, and the"
  echo "  Agent CR — shadow-mode patch and all — is deleted. THE TENANT NAMESPACE IS LEFT STANDING"
  echo "  and so are the ActionRecords in $NS — [[LSN-045]]: the journal-retention policy denies"
  echo "  DELETE of an ActionRecord until export confirms, so a namespace holding one never finishes"
  echo "  terminating and a suite that tried would hang on its own evidence. The gated record is"
  echo "  also the artifact a human reads when this run goes red."
}
# P12 ([[LSN-066]]): this trap is installed AFTER p10_assert_control_plane_healthy, whose
# p12_assert_exclusive_l2 took the one-suite-per-cluster lock and put `_l2_lock_exit_handler` on
# EXIT. Replacing that trap here would leak the lock to the next acquirer's stale break, so the
# release is chained in. It cannot change this script's exit status: bash runs the EXIT trap with
# the pending status and only an explicit `exit` inside the trap overrides it.
trap 'cleanup; l2_lock_release' EXIT

# ------------------------------------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------------------------------------
echo
echo "== fixtures: the tenant namespace, the Agent CR, and the identity its broker runs as =="

printf 'apiVersion: v1\nkind: Namespace\nmetadata:\n  name: %s\n' "$TENANT_NS" | $K apply -f - >/dev/null || {
  echo "FAIL: could not create namespace $TENANT_NS" >&2
  exit 1
}
echo "  tenant namespace: $TENANT_NS"

# THE FAULT TARGET'S ABSENCE IS THE EXPERIMENT, NOT A TIDINESS CONCERN. If the Deployment exists,
# `execute.capture` returns a pre-state, `generateOne`'s restore arm succeeds, the plan validates,
# and scenario F becomes a second control that reports itself as a gate that did not happen. The
# control ConfigMap's absence matters for the ordinary reason: absence afterwards is C-5's evidence.
# Both are cleared LOUDLY, because a leftover is also what a real failure of this suite looks like
# and deleting it silently would erase the one trace of it.
for spec in "deployment $TENANT_NS $UNPLANNABLE_NAME" "configmap $TENANT_NS $CONTROL_NAME"; do
  # shellcheck disable=SC2086
  set -- $spec
  if $K -n "$2" get "$1" "$3" >/dev/null 2>&1; then
    echo "  WARNING: $1 $2/$3 EXISTED BEFORE THIS RUN. Nothing in this suite creates it, so either a"
    echo "    previous run's shadow mutated the cluster — the failure F-7/C-5 exist to find — or"
    echo "    someone made it by hand. It is being deleted so this run's evidence is this run's; if"
    echo "    either arm has ever gone red on this cluster, that is the thing to go and read."
    $K -n "$2" delete "$1" "$3" --ignore-not-found --wait=true >/dev/null 2>&1
  fi
done

# P3: deleted with --wait=true before it is applied, so everything the controller renders from it is
# this generation's — and so the shadow-mode patch below lands on a CR this run created.
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

# Read and write over the tenant namespace. Both halves are needed: step 3 reads pre-state (and for
# the fault it must be allowed to read, so that the NotFound it gets is a NotFound and not a 403),
# and the executor's dry-run apply is AUTHORIZED before it is dry-run.
actor_overlay_apply_write "$K" "$NS" "$AGENT" "$TENANT_NS" || {
  echo "DEFERRED: the actor could not be granted authority over $TENANT_NS. Both envelopes would be"
  echo "  refused at step 3, and a 403 is not a gate — scenario F would keep its target absent for a"
  echo "  reason V-REV-003 is not about."
  exit 3
}

ACTOR_SA="$(actor_overlay_actor_sa "$K" "$NS" "$AGENT")" || {
  echo "DEFERRED: the Agent publishes no actor service account; there is no subject to authorize."
  exit 3
}
ACTOR_SUBJECT="system:serviceaccount:${NS}:${ACTOR_SA}"
echo "  actor identity: $ACTOR_SUBJECT"

# ------------------------------------------------------------------------------------------------
# SHADOW MODE, IMPOSED AND READ BACK (P6)
# ------------------------------------------------------------------------------------------------
# The one thing that makes `dryRun: false` safe to send. Not an assertion — it is a condition of the
# experiment, and a cluster that will not enter shadow mode is a run that must not happen rather
# than a broker that misbehaved. `spec`, not `status`: `pipeline.shadowed` reads
# `Spec.Operations.Brake()`, and `status.operations.dryRunOnly` has no writer at all.
echo
echo "== shadow mode: spec.operations.dryRunOnly on the Agent CR =="
$K -n "$NS" patch "agent/$AGENT" --type=merge \
  -p '{"spec":{"operations":{"dryRunOnly":true}}}' >/dev/null 2>&1 || {
  echo "DEFERRED: the Agent CR could not be patched into shadow mode. Both envelopes below ask the"
  echo "  broker to execute for real, and shadow mode is what makes that a server-side dry run."
  exit 3
}
shadow="$($K -n "$NS" get "agent/$AGENT" -o jsonpath='{.spec.operations.dryRunOnly}' 2>/dev/null)"
if [ "$shadow" != "true" ]; then
  echo "DEFERRED: spec.operations.dryRunOnly reads back as '${shadow:-<unset>}', not 'true'. The patch"
  echo "  returned success and the API server does not agree, so nothing below is safe to submit."
  exit 3
fi
echo "  the API server agrees: spec.operations.dryRunOnly=true"

# ------------------------------------------------------------------------------------------------
# The experiment's own preconditions, asked of the LIVE AUTHORIZER (P6)
# ------------------------------------------------------------------------------------------------
# Neither is a property of the broker, so neither is an assertion. If the actor cannot patch the
# Deployment, F is a 403 at step 3 that happens to leave the object absent — the observation would
# look like a gate to every arm that only reads the object.
echo
echo "== preconditions: the actor is entitled to both operations =="
actor_overlay_can "$K" "$ACTOR_SUBJECT" patch deployments.apps yes -n "$TENANT_NS" || {
  echo "DEFERRED: the actor cannot patch Deployments in $TENANT_NS, so scenario F would be refused at"
  echo "  step 3 rather than gated at step 7. A refusal keeps the object absent too, which is exactly"
  echo "  the confusion F-1 and F-6 exist to prevent — and a run that cannot stage the gate is a"
  echo "  scenario that did not happen, not a broker that failed."
  exit 3
}
actor_overlay_can "$K" "$ACTOR_SUBJECT" get deployments.apps yes -n "$TENANT_NS" || {
  echo "DEFERRED: the actor cannot read Deployments in $TENANT_NS. Step 3's capture would get a 403"
  echo "  instead of a NotFound, and execute.capture narrows only NotFound to Existed=false — the"
  echo "  nil pre-state this whole scenario turns on would never be produced."
  exit 3
}
actor_overlay_can "$K" "$ACTOR_SUBJECT" create configmaps yes -n "$TENANT_NS" || {
  echo "DEFERRED: the actor cannot create ConfigMaps in $TENANT_NS, so the control would be refused"
  echo "  and there would be nothing for the fault's gate to be different FROM."
  exit 3
}
echo "  patch/get deployments and create configmaps: all yes, per the live authorizer"

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
    echo "DEFERRED: P1 unverifiable for the broker. Every arm here is a claim about which STEP of a"
    echo "  binary's pipeline raised the class and how far the pipeline then got; an unidentifiable"
    echo "  binary makes all of them statements about an unknown build."
    exit 3
    ;;
  *)
    bad "P1: the broker is not running the build under test"
    exit 1
    ;;
esac

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

# The brake caches its observation of the Agent CR for `DefaultCacheTTL` (5s) and re-probes lazily on
# the next Observe. 15s is three TTLs — long enough that neither submission can be answered from a
# view filled before the shadow-mode patch landed, short enough not to stall the run. The broker was
# started from a CR that had no `spec.operations` at all, so this wait is not optional: without it
# the first envelope could be evaluated against a cached view in which `shadowed()` is false.
echo
echo "  waiting three brake cache TTLs so the shadow-mode patch is certainly observed"
sleep 15

# ------------------------------------------------------------------------------------------------
# Driver plumbing, shared by both runs
# ------------------------------------------------------------------------------------------------
broker_driver_use_probe "$PROBE" || {
  echo "FAIL: $PROBE is not where this suite says it is" >&2
  exit 1
}
# shellcheck disable=SC2034
BROKER_DRIVER_TENANT_NS="$TENANT_NS"

broker_driver_apply_code "$K" "$NS" "$DRIVER_CM" || {
  echo "FAIL: could not stage the shipped transport code" >&2
  exit 1
}
broker_driver_untrusted_keypair "$K" "$NS" "$UNTRUSTED_SECRET" || {
  echo "FAIL: could not generate the placeholder keypair the driver pod mounts" >&2
  exit 1
}

# flatten <driver stdout> — the probe's JSON lines as tab-separated rows, one per line tag.
flatten() {
  python3 -c '
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
    retry = r.get("retryAfterSeconds")
    print("\t".join([
        r.get("scenario") or "",
        r.get("outcome") or "",
        "" if status is None else str(status),
        r.get("reason") or "",
        r.get("decision") or "",
        r.get("phase") or "",
        r.get("actionId") or "",
        r.get("traceId") or "",
        r.get("namespace") or "",
        "" if retry is None else str(retry),
        (r.get("detail") or "").replace("\t", " ")[:1000],
    ]))
'
}

# field <flat> <tag> <1=outcome 2=status 3=reason 4=decision 5=phase 6=actionId 7=traceId
#                     8=namespace 9=retry 10=detail>
field() {
  printf '%s\n' "$1" | awk -F'\t' -v s="$2" -v i="$(($3 + 1))" '$1 == s { print $i; exit }'
}

# records_for_trace <trace-id> — how many ActionRecords in $NS carry it, and the first match's JSON.
# Two lines: the count, then the document (or `{}`), so one API call answers both the count arm and
# the shape arms. LISTED AND MATCHED rather than looked up by the reply's actionId, so that the fault
# and the control are found by the SAME lookup and neither can be right for a reason the other is
# not. `json.dumps` keeps the document on one line so `sed -n 2p` can lift it out whole.
records_for_trace() { # <trace-id> -> "<count>\n<json>"
  $K -n "$NS" get actionrecords -o json 2>/dev/null | python3 -c '
import json, sys
want = sys.argv[1]
try:
    items = json.load(sys.stdin).get("items", [])
except ValueError:
    items = []
hits = [i for i in items if ((i.get("spec") or {}).get("trace") or {}).get("traceId") == want]
print(len(hits))
print(json.dumps(hits[0]) if hits else "{}")
' "$1"
}

# run_scenario <PROBE_SCENARIO> — drive the pod once and print its flattened transcript on stdout.
#
# EVERY COULD-NOT-RUN PATH RETURNS 3 AND THE CALLER PROPAGATES IT. `exit 3` here would be a lie: the
# function is always invoked inside a command substitution, so an exit ends the SUBSHELL, the
# assignment succeeds with an empty value, and the run carries on into arms that would then judge a
# scenario that never happened. Every call site is `flat="$(run_scenario x)" || exit $?`.
run_scenario() {
  local scenario="$1" out flat nonce rc

  BROKER_DRIVER_EXTRA_ENV="PROBE_SCENARIO=$scenario"
  export BROKER_DRIVER_EXTRA_ENV

  out="$(broker_driver_run "$K" "$NS" "$AGENT" "$AGENT" "$DRIVER_POD" "$DRIVER_CM" "$UNTRUSTED_SECRET")"
  rc=$?
  # A MEASURED collision is not an inability to measure. Filing it as DEFERRED would be a deferral
  # with no external blocker (09 §11.8) on a BLOCKING-ALWAYS suite ([[LSN-067]]).
  if [ "$rc" -eq 4 ]; then
    bad "$scenario: two submissions collided onto one actionId, so the broker answered this one from an earlier submission's record. The experiment ran; its reading is void."
    return 1
  fi
  if [ "$rc" -ne 0 ]; then
    echo "DEFERRED: the driver pod could not be run to completion for '$scenario'." >&2
    return 3
  fi
  echo "$out" | sed 's/^/  | /' >&2

  flat="$(printf '%s\n' "$out" | flatten)"
  if [ -z "$flat" ]; then
    echo "DEFERRED: the driver pod produced no parseable probe output for '$scenario'." >&2
    return 3
  fi

  nonce="$(field "$flat" nonce-accepted 1)"
  if [ "$nonce" != "http" ]; then
    echo "DEFERRED: the door never opened for '$scenario' ($(field "$flat" nonce-accepted 10))." >&2
    echo "  Without a proven-open door a 202 that never arrives and a 401 at the front are the same" >&2
    echo "  observation. This is broker-auth-l2.sh's territory." >&2
    return 3
  fi

  # The caller's own request, read back off the built envelope. A run in which the probe sent
  # `dryRun: true` would come back gated through the brake's row 5 instead of the classifier's step
  # 6, and F-5 would pass on a rule 03 §4.1 does not name.
  case "$(field "$flat" dry-run-note 10)" in
    absent | False | false) : ;;
    *)
      echo "DEFERRED: '$scenario' built an envelope whose own dryRun is" >&2
      echo "  '$(field "$flat" dry-run-note 10)'. A dry-run envelope SUPPRESSES 06 §4.2 step 6's" >&2
      echo "  no-undo-plan rule outright, so this submission would score V-REV-003 on a different" >&2
      echo "  rule. The probe and this suite disagree about what is being asked." >&2
      return 3
      ;;
  esac

  printf '%s\n' "$flat"
}

# ================================================================================================
# F — no-undo-plan
# ================================================================================================
echo
echo "== F: submitting a patch of an object that does not exist, so no undo plan can be generated =="

F_FLAT="$(run_scenario no-undo-plan)" || exit $?

f_status="$(field "$F_FLAT" submit 2)"
f_decision="$(field "$F_FLAT" submit 4)"
f_phase="$(field "$F_FLAT" submit 5)"
f_action="$(field "$F_FLAT" submit 6)"
f_trace="$(field "$F_FLAT" target 7)"
f_target_name="$(field "$F_FLAT" target 10)"
f_target_ns="$(field "$F_FLAT" target 8)"
[ -n "$f_target_name" ] || f_target_name="$UNPLANNABLE_NAME"
[ -n "$f_target_ns" ] || f_target_ns="$TENANT_NS"

if [ -z "$f_trace" ]; then
  echo "DEFERRED: the probe emitted no trace id for the fault run, so there is no handle on the record."
  exit 3
fi
echo "  fault trace id: $f_trace"

# Polled (P9), 30s: `stepGate` Creates the record synchronously BEFORE it answers 202, so a record
# that has not appeared in half a minute is absent rather than late — and a generous poll here would
# hide a broker that answers before it journals.
F_N=0
RECORD='{}'
deadline=$((SECONDS + 30))
while [ "$SECONDS" -lt "$deadline" ]; do
  f_res="$(records_for_trace "$f_trace")"
  F_N="$(printf '%s\n' "$f_res" | sed -n 1p)"
  RECORD="$(printf '%s\n' "$f_res" | sed -n 2p)"
  [ "${F_N:-0}" != "0" ] && break
  sleep 2
done
[ -n "$F_N" ] || F_N=0
echo "  ActionRecords in $NS carrying that trace id: $F_N"

if $K -n "$f_target_ns" get deployment "$f_target_name" >/dev/null 2>&1; then
  f_target_exists=yes
else
  f_target_exists=no
fi

assert_gated "$f_status" "$f_decision" "$f_phase" "$f_action" "$F_N" "$f_target_exists"

# ================================================================================================
# C — undo-plan-control
# ================================================================================================
echo
echo "== C: the control — an apply whose inverse is a delete, so the plan generates =="

C_FLAT="$(run_scenario undo-plan-control)" || exit $?

c_status="$(field "$C_FLAT" submit 2)"
c_decision="$(field "$C_FLAT" submit 4)"
c_phase="$(field "$C_FLAT" submit 5)"
c_action="$(field "$C_FLAT" submit 6)"
c_trace="$(field "$C_FLAT" target 7)"
c_target_name="$(field "$C_FLAT" target 10)"
c_target_ns="$(field "$C_FLAT" target 8)"
[ -n "$c_target_name" ] || c_target_name="$CONTROL_NAME"
[ -n "$c_target_ns" ] || c_target_ns="$TENANT_NS"

if [ -z "$c_trace" ]; then
  echo "DEFERRED: the probe emitted no trace id for the control run, so there is no handle on its record."
  exit 3
fi
echo "  control trace id: $c_trace"

C_N=0
RECORD='{}'
deadline=$((SECONDS + 30))
while [ "$SECONDS" -lt "$deadline" ]; do
  c_res="$(records_for_trace "$c_trace")"
  C_N="$(printf '%s\n' "$c_res" | sed -n 1p)"
  RECORD="$(printf '%s\n' "$c_res" | sed -n 2p)"
  [ "${C_N:-0}" != "0" ] && break
  sleep 2
done
[ -n "$C_N" ] || C_N=0
echo "  ActionRecords in $NS carrying that trace id: $C_N"

if $K -n "$c_target_ns" get configmap "$c_target_name" >/dev/null 2>&1; then
  c_target_exists=yes
else
  c_target_exists=no
fi

assert_control "$c_status" "$c_decision" "$c_phase" "$c_action" "$C_N" "$c_target_exists"

# ================================================================================================
# D — the differential
# ================================================================================================
assert_differential "$f_decision" "$c_decision"

# ------------------------------------------------------------------------------------------------
if [ "$assertions" -ne "$EXPECTED_ASSERTIONS" ]; then
  echo
  bad "only $assertions of $EXPECTED_ASSERTIONS assertions ran. The verdict below would be about arms that never executed."
fi

# ------------------------------------------------------------------------------------------------
# [[LSN-067]] once for the whole run, not once per arm. The driver's action ledger is cumulative
# across this entire process, so this is the only place the whole population can be counted at once.
# broker_driver_run already asserts after every submission, but every call site in this file maps a
# non-zero driver rc to DEFERRED — an inability to run the experiment — and THIS verdict is not
# that: it says the submissions did run and did not mint one action each, so an arm above was
# answered by a record some other submission minted. Scored so it reaches the exit code.
#
# AFTER the assertion-count guard, so a red here can never make that guard report "only N of M" with
# N greater than M. BEFORE the ledger is deleted, which cleanup does from the EXIT trap — i.e. after
# this line. Zero submissions returns 0 and prints NOT-EVALUATED: this is the instrument check, not
# a claim that anything was submitted.
broker_driver_assert_distinct_actions ||
  bad "LSN-067: the submissions this run made did not mint one distinct actionId each, so at least one arm above was scored against another submission's record. The instrument failed; the verdicts measured through it are void."

echo
echo "===================================================================="
if [ "$fail" -eq 0 ]; then
  echo " PROVEN: V-REV-003 at L2"
  echo " An envelope whose undo plan cannot be generated was reclassified GATED — named as such in"
  echo " the journal, by the no-undo-plan rule — and parked at step 7 with no captured pre-state and"
  echo " nothing applied; while the same broker, over the same credential and namespace, accepted an"
  echo " otherwise-identical submission whose inverse existed."
  echo "===================================================================="
  exit 0
fi
echo " FAILED — see the FAIL lines above."
echo "===================================================================="
exit 1
