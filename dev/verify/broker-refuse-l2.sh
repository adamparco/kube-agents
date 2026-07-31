#!/usr/bin/env bash
# V-BRK-018 and V-ISO-006 at L2, and the journal half of Phase 9 acceptance bullet (d) — what a
# DEPLOYED broker does when it accepts an envelope and then discovers, several steps in, that it
# cannot proceed (09 §6.2, 09 §6.4, 06 §4.4 rows 3 and 4, 05 §8 CH6, 03 §6, 05 §1.2).
#
# `broker-execute-l2.sh` next to it in the chain submits one envelope that WORKS, and says so in its
# own words: "Nothing here fails, on purpose." This suite is the other half. Two submissions, two
# ways of not proceeding, and in both cases the thing under test is what did NOT happen:
#
#   A · split-snapshot — 06 §4.4 row 4, V-BRK-018. A two-target envelope whose second target the
#       actor cannot read. `execute.CaptureAll` is all-or-nothing, so the 403 on target 1 must stop
#       target 0 from being applied. Everything about the envelope is legitimate: real credential,
#       well-formed body, caller in scope, both operations individually valid. The refusal comes
#       from step 3 and from nowhere earlier.
#
#   B · journal-gone — 06 §4.4 row 3, and the journal half of Accept (d): "the brake refuses and the
#       agent pauses when the journal is unreachable." One ordinary envelope, submitted while the
#       actor's `actionrecords` grant has been revoked out from under the running broker. The brake
#       probes the ActionRecord store at step 5 and refuses 503 — four steps before the write-ahead
#       Create at step 7 would have been attempted, which is the whole point: nothing executes
#       unjournaled means nothing gets as far as trying.
#
#   C · journal-restored — 05 §8 CH6's last sentence, and the reason B and C together are V-ISO-006
#       rather than B alone. The grant goes back and THE SAME ENVELOPE is submitted again, to the
#       SAME RUNNING POD: it is accepted, and this time it leaves the record B proved could not be
#       written. See "WHY THE RESTORE IS AN ARM" below.
#
# WHY B AND C ARE V-ISO-006 AND NOT JUST V-BRK-018's NEIGHBOUR
#   09 §6.4 line 369 is `V-ISO-006 | CH6 journal down — broker refuses to execute ¬ | L2 | 9`, and
#   05 §8 CH6 spells the scenario out: "Make ActionRecord writes fail (remove the CRD's storage
#   version, OR DENY THE BROKER'S CREATE ON IT). The broker refuses to execute rather than executing
#   unjournaled; auto-brake pauses the agent; the audit log shows zero mutations by that actor
#   identity during the window; the failure is reported to humans. Restoring the journal restores
#   service without a broker restart."
#
#   Arm B induces CH6's SECOND listed fault verbatim — it denies the broker's create — and asserts
#   the refusal, the zero mutations and the zero records. What it had no claimant for is the ID: the
#   row was carried by a suite that named V-BRK-018 and nothing else, so 09's CH6 row had no results
#   line while the property behind it had been green for units. Binding it here is the whole of
#   P9-T11b-2; arm C is what the binding turned out to cost.
#
# WHY THE RESTORE IS AN ARM AND NOT CLEANUP
#   "Restoring the journal restores service without a broker restart" is the clause that separates a
#   broker that REFUSES from a broker that BRICKS, and it is invisible from arm B. A broker that
#   latched the fault on first sight — cached the failed probe forever, or wedged the brake — emits a
#   transcript byte-identical to a correct one for as long as the grant is stripped. The restore
#   already happened in this file: `cleanup()` did it, on the EXIT trap, with its result discarded.
#   Repairing the cluster and asserting the repair worked are the same three API calls; only one of
#   them is evidence. So the restore is now `restore_journal_grant`, called in the body and asserted
#   on, and the trap keeps calling it as the safety net for a run that dies before reaching arm C.
#
# WHY V-BRK-018 IS NOT VACUOUS UNDER SHADOW MODE, which is the hard part of this file
#   Phase 9 runs everything as a server-side dry run, so "neither target was applied" is a claim
#   about two objects that were never going to exist. Read that way the row proves nothing, and a
#   suite that asserted only `kubectl get` returning NotFound twice would be green against a broker
#   that ignored the 403 entirely and dry-ran both targets happily.
#
#   So the property is asserted on the JOURNAL, where a dry run does leave a trace. A submission
#   that got past step 3 produces a write-ahead ActionRecord (pipeline step 7/8) naming the two REAL
#   targets and carrying their captured pre-state. A submission stopped AT step 3 produces exactly
#   one record — the rejection record `rejection.go` writes — whose `spec.targets` is the single
#   sentinel `refused-before-target-resolution`, with no `spec.preState` and no `status.applied`.
#   Those two worlds are distinguishable in the API server, and arm A-6 is the distinction. The
#   NotFound checks are still made (A-7), as the cheap direct half; they are not what carries the row.
#
# HOW THE RECORD IS FOUND, since a refusal reply does not name one
#   `server.go`'s `write()` renders a refusal as `reason`, `message`, `decision` and
#   `retryAfterSeconds` — and no `actionId`, deliberately: the id of a record the caller may not read
#   is not the caller's business. So the `ar-<lowercase ULID>` lookup `broker-execute-l2.sh` uses is
#   unavailable here. What IS available is the correlation id the caller minted: `traceFromBody`
#   copies a well-formed `spec.trace.traceId` onto the rejection record precisely so a refusal can be
#   tied back to the conversation that caused it. The probe emits the id it sent; this suite lists
#   the namespace and matches on it. Arm A-2 asserts the missing `actionId` rather than working
#   around it, because a broker that started leaking one would make this whole method unnecessary
#   and nobody would notice.
#
# WHAT IS ASSERTED, in order:
#   P1a/P1b/AVAIL  the controller and the broker are the build under test, and the broker is up.
#   A-1  the split envelope was REFUSED, 403, reason `target-forbidden` — not accepted, and not
#        refused for some other reason that would happen to look the same.
#   A-2  the refusal reply carries NO `actionId`. See above.
#   A-3  the refusal message says the snapshot was all-or-nothing, in `snapshot.go`'s own words:
#        "so none of the 2 targets will be applied". A 403 whose message named only target 1 would
#        be a broker that refused ONE operation, which is a different and much weaker behaviour.
#   A-4  exactly ONE ActionRecord in the agent namespace carries the trace id the probe sent. Zero
#        means the refusal was not journaled (06 §4.1: the attempt is the evidence); two means
#        something wrote a second record for one submission.
#   A-5  that record is a refusal record: phase `Rejected`, `spec.classification.class` `forbidden`,
#        and `spec.intent` beginning `REFUSED target-forbidden`.
#   A-6  V-BRK-018's real arm — NO WRITE-AHEAD RECORD EXISTS. The one record's `spec.targets` is the
#        `refused-before-target-resolution` sentinel, it carries no `spec.preState`, and it carries
#        no `status.applied`. The pipeline stopped at step 3.
#   A-7  neither target object exists — the tenant ConfigMap and the out-of-scope Deployment, read
#        from the API server.
#   B-1  the journal-blind envelope was REFUSED, 503, reason `journal-unavailable`.
#   B-2  `decision` is `rejected` and `retryAfterSeconds` is 60 (`PausedRetryAfterSeconds`). A 503
#        with no retry hint is a 503 a caller cannot act on.
#   B-3  NO ActionRecord carries B's trace id, observed over a 20s window. This is asserted as a
#        CONSEQUENCE, not as a demand: the same revocation that made the brake's `List` fail also
#        removed `create actionrecords`, so `StoreRejectionJournal.Reject` cannot write and
#        `server.go` logs rather than escalating. Pinning it is what distinguishes row 3 firing —
#        the journal genuinely unreachable in both directions — from a brake that refused for some
#        other reason while the store was fine.
#   B-4  the tenant ConfigMap still does not exist.
#   C-1  the SAME envelope, submitted after the grant is restored, is ACCEPTED — 2xx, and not the
#        503 `journal-unavailable` it got a minute earlier. The brake re-probed; it did not latch.
#   C-2  the journal took the write: at least one ActionRecord carries C's trace id. This is the
#        arm, not C-1. A 2xx proves the brake let the submission past; only a record proves the
#        store it was refusing on behalf of is genuinely writable again. It is B-3 inverted, against
#        the same store, minutes apart, with the grant as the only difference.
#   C-3  WITHOUT A BROKER RESTART — the pod serving C-1 is the same pod, by name and by
#        restartCount, that was serving when the fault was staged. A broker that recovered by dying
#        and being rescheduled satisfies C-1 and C-2 and fails CH6's actual sentence, and on a
#        Deployment with a healthy probe that recovery is invisible within seconds.
#
# WHAT THIS DOES NOT CLAIM, so the next reader does not go looking
#   THE AUTO-PAUSE HALF OF ROW 3 IS NOT ASSERTED, AND IT CANNOT BE IN THIS FAULT. The consumer
#     exists as of P9-T9c-1 — this header used to say it did not, and that sentence was true when it
#     was written and is not now. What replaces it is narrower and permanent: the pause is recorded
#     ON THE ACTIONRECORD (`escalate.Recorder.record` Gets `journal.RecordName(actionID)` and patches
#     `status.escalation.pauseRequested`), and in THIS fault there is no record to put it on, because
#     `StoreRejectionJournal.Reject` is the write that just failed. `server.go`'s `autoPause` says so
#     itself and gives up: "a refusal asked for an auto-pause and there is no record to put it on;
#     the agent stays live". So B-3 being zero and the pause being unobservable are the same fact,
#     not two gaps. Asserting the pause needs CH6's FIRST listed fault instead — remove the CRD's
#     storage version — which breaks the store for every subject rather than for one, and is a
#     different and much less reversible experiment. Filed; not this unit. The refusal half is fully
#     asserted, and arm C now asserts the recovery half.
#   V-REV-003 (no generatable undo plan ⇒ reclassified gated) needs an operation whose inverse does
#     not exist, not a refusal. → P9-T9b-5b-ii-b, with V-BRK-021's L2 surface scan.
#   V-BRK-019's field-manager string is unreadable from a shadow: a server-side dry run persists no
#     `managedFields`. Unchanged by this file; still carried.
#   06 §4.4 ROWS 1, 2, 5, 6 AND 9. Rows 1/2 (freeze and agent unreadable) and row 6 (roster missing)
#     are brake-source faults `brake-fanout-l2.sh` owns; row 5 (no undo plan ⇒ gated) is V-REV-003;
#     row 9 (cannot verify and cannot roll back) needs an executed mutation. Four of nine rows are
#     out of scope on purpose and none of them is silently skipped.
#
# DESTRUCTIVE-TEST GUARD: scratch-GKE contexts only, anchored. This deletes and re-applies the Agent
# CR `platform-agent` in `kubeagents-system`, grants its actor identity write authority over a
# throwaway tenant namespace, and — the reason this suite is more destructive than its neighbours —
# TEMPORARILY REVOKES THE ACTOR'S `actionrecords` GRANT out from under the running broker. For the
# few seconds that is in force the deployed broker cannot journal anything, which is the fault under
# test and is also exactly what it sounds like. On the live install that would be a test blinding
# the fleet's audit trail. The revocation is now UNDONE IN THE BODY rather than only on the exit
# trap, because arm C asserts the undo — which means the window is shorter than it was, and that a
# failure to restore is a loud red arm rather than a warning inside a trap nobody reads.
# Exit: 0 = PROVEN · 1 = FAILED · 2 = refused target / P10 · 3 = DEFERRED (P1 or the run itself).
# Usage: dev/verify/broker-refuse-l2.sh [kube-context]
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions):
#   P1 image-under-test: the controller (`control-plane=controller-manager`) AND this agent's BROKER
#      pod (`kube-agents/agent=<agent>,kube-agents/role=actor`) — both, in full, for
#      `broker-execute-l2.sh`'s reason and one more of this suite's own. Every arm is a claim about
#      what a BINARY did: which step refused, in what order the brake ran relative to the write-ahead
#      Create, what `rejection.go` put in the record. A broker one generation behind would answer all
#      eleven arms about the previous build's pipeline and they would read green. Unverifiable → rc 3.
#   P3 admission-recreate: the Agent CR is deleted with `--wait=true` and re-applied on every run, so
#      the broker Deployment, its mesh Certificates and the pair NetworkPolicies are rendered by the
#      controller running NOW. The broker pod is resolved through `p3_pod_of_deploy`, by ownership,
#      so a pod from the previous generation can never be read as this one's. The driver pod, its
#      ConfigMap, the write overlay and the RBAC fault are all created and undone inside the run. The
#      ActionRecords are deliberately NOT recreated — they are the output — and are disambiguated by
#      the trace id the probe minted in THIS run. P3 is also what makes arm C's no-restart claim
#      meaningful: the pod is resolved once, by ownership, BEFORE the fault, and C-3 compares that
#      exact name and restartCount against a fresh read after the restore.
#   P6 runtime-authoritative: every assertion reads objects from the API server. Nothing is read from
#      the broker's reply body except the reply-shape arms, which are explicitly ABOUT the reply
#      (A-1, A-2, A-3, B-1, B-2) and are cross-checked against the journal by A-4 through A-6. The
#      driver's whole environment — endpoint, SAN, identity, token path, TLS dir — comes off the
#      RENDERED agent Deployment through `broker_driver_env`. The actor's authority is read with
#      `kubectl auth can-i` against the live authorizer, never inferred from the RBAC documents the
#      fixture applied.
set -uo pipefail

# MODES. `live` submits to a real broker and is what every claim above is about.
# `--negative-control` replays the two assertion blocks against synthesised observations that a
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

# The tenant the READABLE half of the envelope aims at. Its own namespace, for the reason
# `broker-execute-l2.sh` gives, and its own name so the two suites cannot leave each other evidence.
TENANT_NS=kubeagents-refuse-tenant

# The namespace the UNREADABLE half aims at, and the one piece of fixture that carries the whole
# split-snapshot scenario. It exists and the actor has no authority over it: the read overlay is
# applied to TENANT_NS only, and `actor-grant-platform.yaml.template`'s ClusterRole — the actor's
# one cluster-wide grant — names no `apps` group at all. So a Deployment here is Forbidden by the
# SHIPPED grant rather than by anything this suite arranged, which is the difference between
# measuring the broker and measuring the fixture. Asserted with `auth can-i` before anything is
# submitted; if the actor CAN read it, the run is DEFERRED rather than passed.
OUTSIDE_NS=kubeagents-refuse-outside

DRIVER_POD=broker-refuse-l2-driver
DRIVER_CM=broker-refuse-l2-code
UNTRUSTED_SECRET=broker-refuse-l2-untrusted
PROBE=dev/verify/fixtures/broker_refuse_probe.py

# Kept in step with the probe's own constants, and cross-checked against the `note` lines it emits
# rather than trusted: `target_name` below is read out of the transcript, and these are only the
# fallback for a run that produced no note. A suite holding the sole copy would delete-check the
# right object today and the wrong one the day the probe's constant changed.
TARGET_NAME=broker-refuse-l2-shadow-target
UNREADABLE_NAME=broker-refuse-l2-unreadable-target

# NEGATIVE CONTROL DOES NOT EXERCISE: (LSN-060.) The control SYNTHESISES every observation — the
# HTTP status, the reason word, the record count, the record document, the two existence answers,
# the pod name and restartCount on either side of the fault — and hands them to the assertion blocks
# directly, so everything upstream of the assertions is unmeasured by it:
#   - the envelope build and the three HTTP POSTs to the deployed broker. A synthesised status is
#     not a broker's answer; the ¬ arm cannot tell a running broker from an absent one
#   - THE RBAC FAULT ITSELF, AND ITS UNDO. The strip, `restore_journal_grant`, and the two `auth
#     can-i` convergence polls are the entire mechanism of scenarios B and C, and the control never
#     calls them. A strip that silently patched nothing would leave B's live arm asserting a 503
#     that never comes, and the ¬ arm green
#   - THE POD IDENTITY READ. C-3's four strings are synthesised, so what the control proves is that
#     the arm can tell a replacement from a survivor — not that `broker_restarts` reads the right
#     pod, and not that `p3_pod_of_deploy` resolves by ownership rather than by the name it was
#     given. Those are live-arm properties and P3's
#   - the trace-id search of the API server (A-4, B-3). The control passes a COUNT; it never runs
#     the list-and-match that produces one. This is `broker-execute-l2.sh`'s exact scar — a lookup
#     line that could not have worked against any commit, green in ¬ for weeks because ¬ skipped it
#   - the `auth can-i` preconditions that establish the second namespace is genuinely unreadable
#   - the P1 digest arms and the broker's Availability, which run before either mode
# What it does prove, and all it proves: the two assertion blocks are not always-green — each of
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

# 2 x P1 + broker Available + A-1..A-7 + B-1..B-4 + C-1..C-3.
EXPECTED_ASSERTIONS=17

# ------------------------------------------------------------------------------------------------
# jrec <dotted key> — one field out of $RECORD, via python. `broker-execute-l2.sh`'s helper,
# verbatim in behaviour: lists and dicts report their LENGTH, which is what lets an arm ask "how
# many targets" and "is there a preState" through one accessor.
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
# A — the split-snapshot assertion block
#
#   assert_split <status> <reason> <action_id> <detail> <n_records> <target_exists> <unreadable_exists>
#   $RECORD is the matched ActionRecord as JSON, or empty when none matched.
#
# A function because `--negative-control` replays exactly these arms against observations nobody's
# broker produced. Every derivation stays inside it, so the ¬ arm exercises the derivation too
# rather than a simplified copy that could agree with a misbehaving broker for a reason the live
# path would not.
# ------------------------------------------------------------------------------------------------
assert_split() {
  local status="$1" reason="$2" action_id="$3" detail="$4" n_records="$5"
  local target_exists="$6" unreadable_exists="$7"
  local phase class intent targets target_name pre applied

  echo
  echo "== A-1: the split envelope was refused at step 3 =="
  if [ -z "$status" ]; then
    bad "A-1: the probe reported no HTTP status for the split submission. Nothing was observed, so nothing is being judged."
  elif [ "$status" -ge 200 ] && [ "$status" -lt 300 ]; then
    bad "A-1: the split envelope was NOT REFUSED — HTTP $status. One of its two targets is unreadable by the actor, and the broker accepted it anyway. V-BRK-018's all-or-nothing snapshot did not hold."
  elif [ "$status" != "403" ] || [ "$reason" != "target-forbidden" ]; then
    bad "A-1: refused for the wrong reason — HTTP $status reason '$reason', want 403 'target-forbidden'. A refusal that arrives from a different step is not evidence about the snapshot."
  else
    pass "A-1: HTTP 403 'target-forbidden' — the pre-state capture refused before anything was applied"
  fi

  echo
  echo "== A-2: the refusal names no actionId =="
  if [ -n "$action_id" ]; then
    bad "A-2: the refusal reply carries an actionId ('$action_id'). 06 §4.1's refusal shape does not include one, and a broker handing out record ids on a refused submission tells an unauthorized caller which journal entries exist."
  else
    pass "A-2: the refusal carries no actionId, which is why the record below is found by trace id"
  fi

  echo
  echo "== A-3: the refusal says ALL of the targets were dropped, not one =="
  case "$detail" in
    *"so none of the 2 targets will be applied"*)
      pass "A-3: the message carries snapshot.go's all-or-nothing wording — the sibling target went down with the unreadable one"
      ;;
    *)
      bad "A-3: the refusal message does not say the snapshot was all-or-nothing. Got: ${detail:-<empty>}. A 403 naming only the unreadable target would be a broker that refused ONE operation and may have applied the other."
      ;;
  esac

  echo
  echo "== A-4: exactly one ActionRecord carries this submission's trace id =="
  if [ "$n_records" = "0" ]; then
    bad "A-4: no ActionRecord carries the trace id this submission sent. 06 §4.1 journals the attempt — 'the attempt is the evidence' — and a refusal nobody recorded is a refusal no audit can see."
  elif [ "$n_records" != "1" ]; then
    bad "A-4: $n_records ActionRecords carry this submission's trace id. One submission, one record; more than one means something wrote a second entry for the same attempt."
  else
    pass "A-4: exactly one ActionRecord carries the trace id, found by listing the API server rather than by trusting the reply"
  fi

  echo
  echo "== A-5: that record is a refusal record =="
  phase="$(jrec status.phase)"
  class="$(jrec spec.classification.class)"
  intent="$(jrec spec.intent)"
  if [ "$n_records" != "1" ]; then
    bad "A-5: there is no single record to judge (n=$n_records), so the refusal's shape was not read."
  elif [ "$phase" != "Rejected" ]; then
    bad "A-5: the record is in phase '${phase:-<none>}', want 'Rejected'. A refused submission that is journaled in any other phase claims an outcome it never had."
  elif [ "$class" != "forbidden" ]; then
    bad "A-5: the record's spec.classification.class is '${class:-<none>}', want 'forbidden'. rejection.go classifies every refusal forbidden; anything else means this record came from somewhere other than the refusal path."
  else
    case "$intent" in
      "REFUSED target-forbidden"*)
        pass "A-5: phase Rejected, class forbidden, intent '${intent:0:60}...' — written by rejection.go, not by the accepting path"
        ;;
      *)
        bad "A-5: the record's spec.intent does not begin 'REFUSED target-forbidden'. Got: ${intent:-<empty>}."
        ;;
    esac
  fi

  echo
  echo "== A-6: V-BRK-018 — NO WRITE-AHEAD RECORD EXISTS; the pipeline stopped at step 3 =="
  targets="$(jrec spec.targets)"
  target_name="$(jrec spec.targets.0.name)"
  pre="$(jrec spec.preState)"
  applied="$(jrec status.applied)"
  if [ "$n_records" != "1" ]; then
    bad "A-6: there is no single record to judge (n=$n_records), so V-BRK-018's journal arm did not run."
  elif [ "$target_name" != "refused-before-target-resolution" ]; then
    bad "A-6: the record resolved $targets target(s), the first named '${target_name:-<none>}'. That is a WRITE-AHEAD record: the pipeline got past step 3 and captured pre-state for targets it had been refused on. V-BRK-018's all-or-nothing snapshot did not hold."
  elif [ -n "$pre" ]; then
    bad "A-6: the record carries spec.preState ($pre entries) alongside the refusal sentinel. Pre-state exists only if CaptureAll returned, which is the thing that was supposed to have failed."
  elif [ -n "$applied" ]; then
    bad "A-6: the record carries status.applied ($applied entries). Something was applied — in shadow mode that means a server-side dry run the API server authorized for real — after a refusal that was supposed to precede it."
  else
    pass "A-6: the only record is the refusal sentinel, with no preState and no status.applied — nothing was resolved, captured or applied for either target"
  fi

  echo
  echo "== A-7: neither target object exists =="
  case "$target_exists" in
    no)
      case "$unreadable_exists" in
        no) pass "A-7: neither the tenant ConfigMap nor the out-of-scope Deployment exists — asserted against the API server, not against the broker's word for it" ;;
        yes) bad "A-7: THE UNREADABLE TARGET EXISTS. The Deployment the actor was supposed to be refused on is in the cluster, so the 403 above was not about an object nobody could touch." ;;
        *) bad "A-7: could not determine whether the unreadable target exists (got '$unreadable_exists'); the no-mutation arm did not run" ;;
      esac
      ;;
    yes) bad "A-7: THE TARGET OBJECT EXISTS. A submission that was refused at step 3 created the ConfigMap anyway. This is the one failure in this file that is a live safety defect and not a reporting one." ;;
    *) bad "A-7: could not determine whether the target object exists (got '$target_exists'); the no-mutation arm did not run" ;;
  esac
}

# ------------------------------------------------------------------------------------------------
# B — the journal-gone assertion block
#
#   assert_journal_gone <status> <reason> <decision> <retry> <n_records> <target_exists>
# ------------------------------------------------------------------------------------------------
assert_journal_gone() {
  local status="$1" reason="$2" decision="$3" retry="$4" n_records="$5" target_exists="$6"

  echo
  echo "== B-1: the brake refused because the journal was unreachable =="
  if [ -z "$status" ]; then
    bad "B-1: the probe reported no HTTP status for the journal-blind submission. Nothing was observed, so nothing is being judged."
  elif [ "$status" -ge 200 ] && [ "$status" -lt 300 ]; then
    bad "B-1: the journal-blind envelope was NOT REFUSED — HTTP $status. The broker accepted an action it could not journal, which is the exact inverse of 06 §4.4 row 3."
  elif [ "$status" != "503" ] || [ "$reason" != "journal-unavailable" ]; then
    bad "B-1: refused for the wrong reason — HTTP $status reason '$reason', want 503 'journal-unavailable'. A refusal from a different rule is not evidence that the brake saw the journal fail."
  else
    pass "B-1: HTTP 503 'journal-unavailable' — the brake probed the ActionRecord store at step 5 and refused"
  fi

  echo
  echo "== B-2: the refusal is actionable =="
  if [ "$decision" != "rejected" ]; then
    bad "B-2: the reply's decision is '${decision:-<none>}', want 'rejected'. The caller cannot tell a refusal from a deferral."
  elif [ "$retry" != "60" ]; then
    bad "B-2: retryAfterSeconds is '${retry:-<none>}', want 60 (PausedRetryAfterSeconds). A 503 with no retry hint is a 503 the caller can only respond to by guessing."
  else
    pass "B-2: decision 'rejected', retryAfterSeconds 60 — a transient refusal the caller can act on"
  fi

  echo
  echo "== B-3: nothing was journaled, because nothing could be =="
  if [ "$n_records" = "0" ]; then
    pass "B-3: no ActionRecord carries this submission's trace id — the store was unreachable in both directions, which is what makes the 503 above a journal fault and not some other rule wearing its name"
  else
    bad "B-3: $n_records ActionRecord(s) carry this submission's trace id. The broker refused with 'journal-unavailable' and then wrote to the journal, so whatever the brake observed, it was not this store being unreachable."
  fi

  echo
  echo "== B-4: nothing executed =="
  case "$target_exists" in
    no) pass "B-4: the tenant ConfigMap does not exist — the refusal preceded the executor, as 'nothing executes unjournaled' requires" ;;
    yes) bad "B-4: THE TARGET OBJECT EXISTS. An action the broker refused as unjournalable was executed anyway." ;;
    *) bad "B-4: could not determine whether the target object exists (got '$target_exists'); the no-mutation arm did not run" ;;
  esac
}

# ------------------------------------------------------------------------------------------------
# C — the journal-restored assertion block. 05 §8 CH6's last sentence.
#
#   assert_journal_restored <status> <reason> <n_records> <pod_before> <pod_after> \
#                           <restarts_before> <restarts_after>
#
# THE POD IDENTITY IS PASSED IN AS FOUR STRINGS rather than read here, so the `¬` arm can synthesise
# a restart it has no way to cause. Two pieces, not one: a pod that CRASHED and was restarted by the
# kubelet keeps its name and bumps `restartCount`, and a pod that was RESCHEDULED gets a new name.
# CH6 says "without a broker restart" and both of those are one.
# ------------------------------------------------------------------------------------------------
assert_journal_restored() {
  local status="$1" reason="$2" n_records="$3"
  local pod_before="$4" pod_after="$5" restarts_before="$6" restarts_after="$7"

  echo
  echo "== C-1: the same envelope is accepted once the journal is back =="
  if [ -z "$status" ]; then
    bad "C-1: the probe reported no HTTP status for the post-restore submission. The recovery arm observed nothing, so it is judging nothing."
  elif [ "$status" = "503" ] && [ "$reason" = "journal-unavailable" ]; then
    bad "C-1: STILL 503 'journal-unavailable' after the grant was restored. The brake latched the fault rather than re-probing, so this broker does not refuse — it bricks, and 05 §8 CH6's last sentence is false of it."
  elif [ "$status" -lt 200 ] || [ "$status" -ge 300 ]; then
    bad "C-1: the post-restore submission was refused HTTP $status reason '${reason:-<none>}'. Not the latch above, but service was not restored either; whatever this refusal is, it was not there before the fault."
  else
    pass "C-1: HTTP $status — the submission refused 503 a minute ago is accepted now, so the brake re-probed the store instead of latching"
  fi

  echo
  echo "== C-2: the write the fault prevented actually lands =="
  # `-gt 0` and not `= 1`: B-3's count is the assertion about how many records a submission may
  # produce, and it is made where it is falsifiable. Here the question is whether the store is
  # writable at all, and demanding an exact count would make this arm fail on a broker that
  # journaled a rejection AND a write-ahead record — which would be a different finding entirely,
  # and not one this arm should be the one to report.
  case "$n_records" in
    '' | *[!0-9]*) n_records="" ;;
  esac
  if [ -z "$n_records" ]; then
    bad "C-2: no record count was observed for the post-restore submission; the arm did not run"
  elif [ "$n_records" -gt 0 ]; then
    pass "C-2: $n_records ActionRecord(s) carry the post-restore trace id — the store B-3 proved was unwritable took this write, minutes later, with the grant as the only difference"
  else
    bad "C-2: ZERO ActionRecords carry the post-restore trace id. The broker answered 2xx and journaled nothing, which is the state 'nothing executes unjournaled' exists to make impossible — and it is worse than the 503, because this one does not tell the caller."
  fi

  echo
  echo "== C-3: and without a broker restart =="
  if [ -z "$pod_before" ] || [ -z "$pod_after" ]; then
    bad "C-3: the broker pod identity was not observed on both sides of the fault (before='${pod_before:-<none>}' after='${pod_after:-<none>}'); the no-restart arm did not run"
  elif [ "$pod_before" != "$pod_after" ]; then
    bad "C-3: THE BROKER POD WAS REPLACED — '$pod_before' before the fault, '$pod_after' after. Service came back because a new process came up, which is exactly the recovery 05 §8 CH6 says must not be necessary."
  elif [ "$restarts_before" != "$restarts_after" ]; then
    bad "C-3: THE BROKER CONTAINER RESTARTED — restartCount '$restarts_before' before the fault, '$restarts_after' after. Same pod, new process; 'without a broker restart' is false."
  else
    pass "C-3: the same pod '$pod_after' at the same restartCount '$restarts_after' served the refusal and the recovery — the journal came back, the broker did not have to"
  fi
}

# ------------------------------------------------------------------------------------------------
# The `¬` arm
# ------------------------------------------------------------------------------------------------
# WHY SYNTHESISED OBSERVATIONS AND NOT A MUTATION. Making a real broker leak an actionId on a
# refusal, or write a write-ahead record for a submission it refused, means editing the Go pipeline
# — `dev/mutate.py`'s job at L1, and not something an L2 suite can stage against a deployed binary.
# What this arm proves is the thing an L2 suite CAN get wrong on its own: that the assertion blocks
# distinguish a correct refusal from a broken one at all. What it deliberately does NOT prove is
# listed in full at the top of this file.
#
# EACH MUTANT MUST BE CAUGHT BY THE ARM THAT TARGETS IT ([[LSN-035]]). Every row carries a needle and
# counts as caught only when a FAIL line CONTAINS it — not merely when something somewhere went red.
# Without that, breaking `jrec` would "catch" every mutant at once by failing every arm, and the
# control would read green while asserting that the suite is broken.
#
# THE VERDICT IS READ OFF THE OUTPUT, NEVER OFF `$fail`: the assertion blocks run inside a command
# substitution, which is a subshell, so every `fail=1` they set dies with it.
GOOD_RECORD='{"spec":{"intent":"REFUSED target-forbidden: step 3: capturing pre-state for 2 targets","classification":{"class":"forbidden"},"targets":[{"kind":"ActionEnvelope","name":"refused-before-target-resolution"}]},"status":{"phase":"Rejected"}}'

run_negative_control() {
  local name expect needle out n_fail rc=0 total=0 caught=0
  local doc a1 a2 a3 a4 a5 a6 a7 b1 b2 b3 b4 b5 b6

  # --- A ------------------------------------------------------------------------------------------
  # name | expect | needle | record-json | status | reason | actionId | detail | n | target | unreadable
  while IFS='|' read -r name expect needle doc a1 a2 a3 a4 a5 a6 a7; do
    [ -n "$name" ] || continue
    total=$((total + 1))
    RECORD="$doc"
    out="$(assert_split "$a1" "$a2" "$a3" "$a4" "$a5" "$a6" "$a7" 2>&1)"
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
A-baseline|green|-|$GOOD_RECORD|403|target-forbidden||step 3: capturing pre-state for 2 targets: snapshot: target 1 (Deployment out/dep) could not be captured, so none of the 2 targets will be applied: forbidden|1|no|no
A-accepted|red|NOT REFUSED|$GOOD_RECORD|202|||accepted|1|no|no
A-no-status|red|no HTTP status|$GOOD_RECORD|||||1|no|no
A-wrong-reason|red|refused for the wrong reason|$GOOD_RECORD|503|snapshot-failed||so none of the 2 targets will be applied|1|no|no
A-actionid-leaked|red|carries an actionId|$GOOD_RECORD|403|target-forbidden|01JZZZ|so none of the 2 targets will be applied|1|no|no
A-one-target-only|red|does not say the snapshot was all-or-nothing|$GOOD_RECORD|403|target-forbidden||target 1 (Deployment out/dep) could not be captured: forbidden|1|no|no
A-not-journaled|red|no ActionRecord carries the trace id|$GOOD_RECORD|403|target-forbidden||so none of the 2 targets will be applied|0|no|no
A-two-records|red|2 ActionRecords carry|$GOOD_RECORD|403|target-forbidden||so none of the 2 targets will be applied|2|no|no
A-wrong-phase|red|in phase 'Executing'|{"spec":{"intent":"REFUSED target-forbidden: x","classification":{"class":"forbidden"},"targets":[{"name":"refused-before-target-resolution"}]},"status":{"phase":"Executing"}}|403|target-forbidden||so none of the 2 targets will be applied|1|no|no
A-wrong-class|red|classification.class is 'routine'|{"spec":{"intent":"REFUSED target-forbidden: x","classification":{"class":"routine"},"targets":[{"name":"refused-before-target-resolution"}]},"status":{"phase":"Rejected"}}|403|target-forbidden||so none of the 2 targets will be applied|1|no|no
A-not-a-refusal-intent|red|does not begin 'REFUSED target-forbidden'|{"spec":{"intent":"apply a ConfigMap","classification":{"class":"forbidden"},"targets":[{"name":"refused-before-target-resolution"}]},"status":{"phase":"Rejected"}}|403|target-forbidden||so none of the 2 targets will be applied|1|no|no
A-write-ahead-record|red|That is a WRITE-AHEAD record|{"spec":{"intent":"REFUSED target-forbidden: x","classification":{"class":"forbidden"},"targets":[{"name":"broker-refuse-l2-shadow-target"},{"name":"broker-refuse-l2-unreadable-target"}]},"status":{"phase":"Rejected"}}|403|target-forbidden||so none of the 2 targets will be applied|1|no|no
A-prestate-captured|red|carries spec.preState|{"spec":{"intent":"REFUSED target-forbidden: x","classification":{"class":"forbidden"},"targets":[{"name":"refused-before-target-resolution"}],"preState":[{"kind":"ConfigMap"}]},"status":{"phase":"Rejected"}}|403|target-forbidden||so none of the 2 targets will be applied|1|no|no
A-something-applied|red|carries status.applied|{"spec":{"intent":"REFUSED target-forbidden: x","classification":{"class":"forbidden"},"targets":[{"name":"refused-before-target-resolution"}]},"status":{"phase":"Rejected","applied":[{"kind":"ConfigMap"}]}}|403|target-forbidden||so none of the 2 targets will be applied|1|no|no
A-target-created|red|THE TARGET OBJECT EXISTS|$GOOD_RECORD|403|target-forbidden||so none of the 2 targets will be applied|1|yes|no
A-unreadable-created|red|THE UNREADABLE TARGET EXISTS|$GOOD_RECORD|403|target-forbidden||so none of the 2 targets will be applied|1|no|yes
A-target-unknown|red|could not determine whether the target object exists|$GOOD_RECORD|403|target-forbidden||so none of the 2 targets will be applied|1|unknown|no
CASES

  # --- B ------------------------------------------------------------------------------------------
  # name | expect | needle | status | reason | decision | retry | n | target
  while IFS='|' read -r name expect needle b1 b2 b3 b4 b5 b6; do
    [ -n "$name" ] || continue
    total=$((total + 1))
    out="$(assert_journal_gone "$b1" "$b2" "$b3" "$b4" "$b5" "$b6" 2>&1)"
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
  done <<'BCASES'
B-baseline|green|-|503|journal-unavailable|rejected|60|0|no
B-accepted|red|NOT REFUSED|202||accepted||0|no
B-no-status|red|no HTTP status|||||0|no
B-wrong-reason|red|refused for the wrong reason|403|target-forbidden|rejected||0|no
B-no-decision|red|decision is '<none>'|503|journal-unavailable||60|0|no
B-no-retry|red|retryAfterSeconds is '<none>'|503|journal-unavailable|rejected||0|no
B-journaled-anyway|red|1 ActionRecord(s) carry|503|journal-unavailable|rejected|60|1|no
B-target-created|red|THE TARGET OBJECT EXISTS|503|journal-unavailable|rejected|60|0|yes
BCASES

  # --- C ------------------------------------------------------------------------------------------
  # name | expect | needle | status | reason | n | pod-before | pod-after | restarts-before | restarts-after
  #
  # `C-latched` is the case this whole arm exists for. It is the ONLY observation in this file that
  # a run against a permanently-wedged broker produces, and until arm C existed it was also what a
  # correct run produced, because nothing looked after the restore.
  local c1 c2 c3 c4 c5 c6 c7
  while IFS='|' read -r name expect needle c1 c2 c3 c4 c5 c6 c7; do
    [ -n "$name" ] || continue
    total=$((total + 1))
    out="$(assert_journal_restored "$c1" "$c2" "$c3" "$c4" "$c5" "$c6" "$c7" 2>&1)"
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
  done <<'CCASES'
C-baseline|green|-|202||1|broker-abc|broker-abc|0|0
C-latched|red|STILL 503|503|journal-unavailable|0|broker-abc|broker-abc|0|0
C-no-status|red|no HTTP status|||1|broker-abc|broker-abc|0|0
C-other-refusal|red|was refused HTTP 403|403|target-forbidden|1|broker-abc|broker-abc|0|0
C-accepted-unjournaled|red|ZERO ActionRecords carry the post-restore trace id|202||0|broker-abc|broker-abc|0|0
C-count-unknown|red|no record count was observed|202||?|broker-abc|broker-abc|0|0
C-pod-replaced|red|THE BROKER POD WAS REPLACED|202||1|broker-abc|broker-xyz|0|0
C-container-restarted|red|THE BROKER CONTAINER RESTARTED|202||1|broker-abc|broker-abc|0|1
C-pod-unobserved|red|was not observed on both sides|202||1|broker-abc||0|0
CCASES

  echo
  echo "negative control: $caught/$total"
  return $rc
}

if [ "$MODE" = negative-control ]; then
  echo "== broker-refuse-l2.sh --negative-control: do the assertion blocks tell a correct refusal from a broken one? =="
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
    echo "  This suite deletes and re-applies the Agent CR '$AGENT' in $NS, widens its actor identity" >&2
    echo "  over a throwaway namespace, and TEMPORARILY REVOKES that identity's actionrecords grant" >&2
    echo "  out from under the running broker — for those seconds the broker cannot journal anything." >&2
    echo "  On the live install that is a test blinding the fleet's audit trail." >&2
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

JOURNAL_STRIPPED=no
JOURNAL_SNAPSHOT_DIR=""

# restore_journal_grant — put the actor's `actionrecords` authority back. rc 0 = restored.
#
# CALLED TWICE ON PURPOSE, and idempotent because of it: once from the body, where arm C asserts
# that the broker recovers from it, and once from the EXIT trap, which is the safety net for a run
# that died before reaching the body's call. `JOURNAL_STRIPPED=no` on success is what stops the trap
# doing the work a second time — and, more importantly, what stops a SILENT second attempt from
# being the thing that actually repaired the cluster after arm C already reported it had not.
#
# SNAPSHOTS FIRST, `seed_agent_identity` SECOND, and both. The strip is discovery-based — it removes
# `actionrecords` from every role object actually bound to the actor, which on a long-lived cluster
# includes objects NO TEMPLATE OWNS (see the strip's comment). Re-seeding re-renders only the
# shipped ones, so an unowned object stripped here would stay stripped forever and the next run
# would find the fault already staged. Restoring the snapshot is what puts back exactly what was
# there; the re-seed is the belt for anything the snapshot missed.
restore_journal_grant() {
  [ "$JOURNAL_STRIPPED" = yes ] || return 0
  echo "  restoring the actor's actionrecords grant (snapshots, then a re-seed)"
  local restore_failed=no snap
  if [ -n "$JOURNAL_SNAPSHOT_DIR" ] && [ -d "$JOURNAL_SNAPSHOT_DIR" ]; then
    for snap in "$JOURNAL_SNAPSHOT_DIR"/*.json; do
      [ -f "$snap" ] || continue
      $K apply -f "$snap" >/dev/null 2>&1 || {
        restore_failed=yes
        echo "  WARNING: could not restore $snap" >&2
      }
    done
  fi
  seed_agent_identity "$K" "$NS" "$AGENT" >/dev/null 2>&1 || {
    restore_failed=yes
    echo "  WARNING: the re-seed failed. The actor may still be unable to journal; re-run seed_agent_identity by hand." >&2
  }
  if [ "$restore_failed" = no ]; then
    JOURNAL_STRIPPED=no
    [ -n "$JOURNAL_SNAPSHOT_DIR" ] && rm -rf "$JOURNAL_SNAPSHOT_DIR"
    return 0
  fi
  if [ -n "$JOURNAL_SNAPSHOT_DIR" ]; then
    # Kept on purpose: these files are the only record of what the grant looked like before the
    # strip, and a restore that failed is exactly when somebody needs them.
    echo "  the pre-strip grant snapshots are KEPT at $JOURNAL_SNAPSHOT_DIR — re-apply them by hand." >&2
  fi
  return 1
}

cleanup() {
  # The journal grant FIRST and unconditionally. Everything else in this trap is tidying; this one
  # is repair. A run killed between the strip and the restore leaves the deployed broker unable to
  # journal, which is a cluster that fails closed on every submission and gives no clue why. On a
  # run that reached arm C this is a no-op — `restore_journal_grant` already cleared the flag.
  if [ "$JOURNAL_STRIPPED" = yes ]; then
    echo
    restore_journal_grant
  fi
  actor_overlay_revoke_write "$K" "$TENANT_NS" >/dev/null 2>&1
  actor_overlay_revoke "$K" "$TENANT_NS" >/dev/null 2>&1
  broker_driver_delete "$K" "$NS" "$DRIVER_POD" "$DRIVER_CM" "$UNTRUSTED_SECRET"
  $K -n "$NS" delete agent "$AGENT" --ignore-not-found --wait=false >/dev/null 2>&1
  echo
  echo "CLEANED UP: the journal grant is restored, the overlays are revoked, the driver pod and its"
  echo "  ConfigMap are gone, and the Agent CR is deleted. BOTH NAMESPACES ARE LEFT STANDING and so"
  echo "  is the ActionRecord in $NS — [[LSN-045]]: the journal-retention policy denies DELETE of an"
  echo "  ActionRecord until export confirms, so a namespace holding one never finishes terminating"
  echo "  and a suite that tried would hang on its own evidence. The record is also the artifact a"
  echo "  human reads when this run goes red."
}
trap cleanup EXIT

# ------------------------------------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------------------------------------
echo
echo "== fixtures: two namespaces, the Agent CR, and the identity its broker runs as =="

for ns in "$TENANT_NS" "$OUTSIDE_NS"; do
  printf 'apiVersion: v1\nkind: Namespace\nmetadata:\n  name: %s\n' "$ns" | $K apply -f - >/dev/null || {
    echo "FAIL: could not create namespace $ns" >&2
    exit 1
  }
done
echo "  tenant namespace (readable target):     $TENANT_NS"
echo "  out-of-scope namespace (unreadable):    $OUTSIDE_NS"

# The two objects must be absent BEFORE the run, because absence afterwards is the evidence. A
# leftover from a previous run would make A-7 red for a reason that is not this run's, so it is
# cleared — but LOUDLY, because a leftover is also exactly what a real V-BRK-018 failure looks like
# and deleting it silently would erase the one trace of it.
for spec in "configmap $TENANT_NS $TARGET_NAME" "deployment $OUTSIDE_NS $UNREADABLE_NAME"; do
  # shellcheck disable=SC2086
  set -- $spec
  if $K -n "$2" get "$1" "$3" >/dev/null 2>&1; then
    echo "  WARNING: $1 $2/$3 EXISTED BEFORE THIS RUN. Nothing in this suite creates it, so either a"
    echo "    previous run's shadow mutated the cluster — the failure A-7 exists to find — or someone"
    echo "    made it by hand. It is being deleted so this run's evidence is this run's; if A-7 has"
    echo "    ever gone red on this cluster, that is the thing to go and read."
    $K -n "$2" delete "$1" "$3" --ignore-not-found --wait=true >/dev/null 2>&1
  fi
done

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

# Read and write over the TENANT namespace only. The readable half of the split envelope needs both
# — step 3 reads pre-state and the executor's dry-run apply is AUTHORIZED before it is dry-run — and
# the out-of-scope namespace deliberately gets neither.
actor_overlay_apply_write "$K" "$NS" "$AGENT" "$TENANT_NS" || {
  echo "DEFERRED: the actor could not be granted authority over $TENANT_NS; the readable half of the"
  echo "  split envelope would be refused too, and a 403 that names both targets proves nothing about"
  echo "  all-or-nothing."
  exit 3
}

ACTOR_SA="$(actor_overlay_actor_sa "$K" "$NS" "$AGENT")" || {
  echo "DEFERRED: the Agent publishes no actor service account; there is no subject to fault."
  exit 3
}
ACTOR_SUBJECT="system:serviceaccount:${NS}:${ACTOR_SA}"
echo "  actor identity: $ACTOR_SUBJECT"

# ------------------------------------------------------------------------------------------------
# The experiment's own preconditions, asked of the LIVE AUTHORIZER (P6)
# ------------------------------------------------------------------------------------------------
# Neither of these is a property of the broker, so neither is an assertion: they are the conditions
# under which the split-snapshot scenario means anything at all. If the actor turns out to be able
# to read the out-of-scope Deployment, the 403 this suite is waiting for will never arrive and the
# right answer is DEFERRED — a scenario that could not be staged, not a broker that misbehaved.
echo
echo "== preconditions: the split is a real split =="
actor_overlay_can "$K" "$ACTOR_SUBJECT" get configmaps yes -n "$TENANT_NS" || {
  echo "DEFERRED: the actor cannot read ConfigMaps in $TENANT_NS, so BOTH halves of the envelope are"
  echo "  unreadable. The refusal would arrive whichever target came first, and A-3's all-or-nothing"
  echo "  wording would be satisfied by a broker that simply refuses everything."
  exit 3
}
echo "  the readable half is readable"
actor_overlay_can "$K" "$ACTOR_SUBJECT" get deployments.apps no -n "$OUTSIDE_NS" || {
  echo "DEFERRED: the actor CAN read Deployments in $OUTSIDE_NS. The unreadable half is readable, so"
  echo "  the snapshot would succeed and this envelope would test the accepting path — which is"
  echo "  broker-execute-l2.sh's job, already done, and not evidence for V-BRK-018."
  exit 3
}
echo "  the unreadable half is unreadable, per the shipped grant"

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
    echo "  binary's pipeline refused and in what order; an unidentifiable binary makes all of them"
    echo "  statements about an unknown build."
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
        r.get("traceId") or "",
        r.get("actionId") or "",
        r.get("namespace") or "",
        "" if retry is None else str(retry),
        (r.get("detail") or "").replace("\t", " ")[:1000],
    ]))
'
}

# field <flat> <tag> <1=outcome 2=status 3=reason 4=decision 5=traceId 6=actionId 7=namespace 8=retry 9=detail>
field() {
  printf '%s\n' "$1" | awk -F'\t' -v s="$2" -v i="$(($3 + 1))" '$1 == s { print $i; exit }'
}

# records_for_trace <trace-id> — how many ActionRecords in $NS carry it, and the first match's JSON.
# Two lines: the count, then the document (or `{}`), so one API call answers both A-4 and A-5/A-6.
# LISTED AND MATCHED rather than looked up by name, because a refusal reply names no record; see the
# header. `json.dumps` keeps the document on one line so `sed -n 2p` can lift it out whole.
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

# ================================================================================================
# A — split-snapshot
# ================================================================================================
echo
echo "== A: submitting a two-target envelope whose second target the actor cannot read =="

BROKER_DRIVER_EXTRA_ENV="PROBE_SCENARIO=split-snapshot
PROBE_OUTSIDE_NAMESPACE=$OUTSIDE_NS"
export BROKER_DRIVER_EXTRA_ENV

a_out="$(broker_driver_run "$K" "$NS" "$AGENT" "$AGENT" "$DRIVER_POD" "$DRIVER_CM" "$UNTRUSTED_SECRET")"
driver_rc=$?
if [ "$driver_rc" -ne 0 ]; then
  echo "DEFERRED: the driver pod could not be run to completion, so no envelope was ever submitted."
  echo "  An inability to run the experiment, not a property that failed (P10's distinction)."
  exit 3
fi
echo "$a_out" | sed 's/^/  | /'

A_FLAT="$(printf '%s\n' "$a_out" | flatten)"
if [ -z "$A_FLAT" ]; then
  echo "DEFERRED: the driver pod produced no parseable probe output for scenario A."
  exit 3
fi

a_nonce="$(field "$A_FLAT" nonce-accepted 1)"
if [ "$a_nonce" != "http" ]; then
  echo "DEFERRED: the door never opened for scenario A — the shipped client could not even get a"
  echo "  nonce ($(field "$A_FLAT" nonce-accepted 9)). Without a proven-open door, a 403 from step 3"
  echo "  and a 401 at the front are the same observation. This is broker-auth-l2.sh's territory."
  exit 3
fi

a_status="$(field "$A_FLAT" submit 2)"
a_reason="$(field "$A_FLAT" submit 3)"
a_action="$(field "$A_FLAT" submit 6)"
a_detail="$(field "$A_FLAT" submit 9)"
a_trace="$(field "$A_FLAT" target 5)"
a_target_name="$(field "$A_FLAT" target 9)"
a_target_ns="$(field "$A_FLAT" target 7)"
a_unread_name="$(field "$A_FLAT" unreadable 9)"
a_unread_ns="$(field "$A_FLAT" unreadable 7)"

# The names come from the probe's `note` lines; the constants at the top are the fallback for a run
# that produced none, and a mismatch between them is a probe/suite drift worth seeing.
[ -n "$a_target_name" ] || a_target_name="$TARGET_NAME"
[ -n "$a_target_ns" ] || a_target_ns="$TENANT_NS"
[ -n "$a_unread_name" ] || a_unread_name="$UNREADABLE_NAME"
[ -n "$a_unread_ns" ] || a_unread_ns="$OUTSIDE_NS"

if [ -z "$a_trace" ]; then
  echo "DEFERRED: the probe emitted no trace id for scenario A, so there is no handle on the record."
  exit 3
fi
echo "  scenario A trace id: $a_trace"

# Polled (P9), 30s: `rejection.go` writes the record synchronously inside the refusal, so a record
# that has not appeared in half a minute is absent rather than late — and a generous poll here would
# hide a broker that answers before it journals.
A_N=0
RECORD='{}'
deadline=$((SECONDS + 30))
while [ "$SECONDS" -lt "$deadline" ]; do
  a_res="$(records_for_trace "$a_trace")"
  A_N="$(printf '%s\n' "$a_res" | sed -n 1p)"
  RECORD="$(printf '%s\n' "$a_res" | sed -n 2p)"
  [ "${A_N:-0}" != "0" ] && break
  sleep 2
done
[ -n "$A_N" ] || A_N=0
echo "  ActionRecords in $NS carrying that trace id: $A_N"

if $K -n "$a_target_ns" get configmap "$a_target_name" >/dev/null 2>&1; then
  a_target_exists=yes
else
  a_target_exists=no
fi
if $K -n "$a_unread_ns" get deployment "$a_unread_name" >/dev/null 2>&1; then
  a_unread_exists=yes
else
  a_unread_exists=no
fi

assert_split "$a_status" "$a_reason" "$a_action" "$a_detail" "$A_N" "$a_target_exists" "$a_unread_exists"

# ================================================================================================
# B — journal-gone
# ================================================================================================
echo
echo "== B: revoking the actor's actionrecords grant out from under the running broker =="

# broker_restarts — the restartCount of every container in the broker pod, joined. C-3 reads this
# before the fault and again after the recovery. Joined rather than summed so a two-container pod
# where one restarts and another is added cannot cancel out to the same number.
broker_restarts() {
  $K -n "$NS" get "pod/$broker_pod" \
    -o jsonpath='{range .status.containerStatuses[*]}{.name}={.restartCount},{end}' 2>/dev/null
}

# Captured HERE and not at pod-resolution time: C-3's claim is CH6's sentence exactly — the journal
# came back without the BROKER having to — so the window it measures is the fault's window and not
# the whole run's. A crash during arm A is a real problem and a different arm's to report.
POD_BEFORE="$broker_pod"
RESTARTS_BEFORE="$(broker_restarts)"
echo "  broker pod before the fault: $POD_BEFORE (restarts: ${RESTARTS_BEFORE:-<none>})"

# EVERY OBJECT THAT CARRIES THE GRANT, DISCOVERED — not a list of the ones this suite expects.
# `actor-grant-platform.yaml.template` puts `actionrecords get/list/watch` on the CLUSTER-scoped
# ClusterRole as well as `get/list/watch/create` on the namespaced Role, so a strip that took only
# one of them would leave the brake's `List` satisfied by the other: the probe succeeds, no 503
# arrives, and the suite reports a broker that ignored a journal fault when what actually happened
# is that there was no fault.
#
# Naming those two objects is still not enough, and this is the part that had to be learned from a
# cluster. On 2026-07-31 this suite stripped exactly that pair and the authorizer still said `yes`:
# `gke-scratch-kube-agents-dev` also carried `Role/kubeagents-broker-operations` and
# `RoleBinding/platform-agent-broker-operations` in kubeagents-system, granting the actor
# `actionrecords get/list/watch/create` — RESIDUE of an older generation of the templates, from
# before the split moved the namespaced verbs onto the per-tier Role. No template renders those
# objects today, so nothing ever removed them; `kubectl apply` does not delete what it stopped
# rendering. A hardcoded pair therefore tests the grant this repo BELIEVES it ships. Walking the
# bindings tests the grant the cluster actually has, which is the only one the authorizer consults.
#
# `approvalrosters` is left alone deliberately: removing it would trip 06 §4.4 row 6 (roster
# missing) as well, and a refusal satisfying two rules at once is evidence for neither.
#
# A stdin→stdout filter rather than a function taking a kubectl target, so each call below reads
# exactly as what it is: get, filter, apply. Resources are removed from each rule and the rule is
# dropped only when nothing is left, because a rule mixing `actionrecords` with something else must
# lose one and keep the other — the fault under test is "the journal is unreachable", not "the actor
# has no authority".
drop_actionrecords_rules() {
  python3 -c '
import json, sys
GONE = {"actionrecords", "actionrecords/status"}
doc = json.load(sys.stdin)
for k in ("resourceVersion", "uid", "creationTimestamp", "generation", "managedFields", "selfLink"):
    doc.get("metadata", {}).pop(k, None)
kept = []
for rule in doc.get("rules") or []:
    res = [r for r in (rule.get("resources") or []) if r not in GONE]
    if not res:
        continue
    rule["resources"] = res
    kept.append(rule)
doc["rules"] = kept
json.dump(doc, sys.stdout)
'
}

# `<kind> <name>` for every role object bound to the actor's ServiceAccount: the cluster-scoped
# bindings, then the namespaced ones in $NS. The kind comes out of each `roleRef` rather than being
# assumed, because a RoleBinding may point at a ClusterRole. Subjects are matched on kind, name AND
# namespace — a same-named ServiceAccount elsewhere is a different identity, and stripping its grant
# would be collateral damage in a namespace this suite never named.
bound_role_refs() {
  $K get clusterrolebindings -o json 2>/dev/null | python3 -c '
import json, sys
name, ns = sys.argv[1], sys.argv[2]
for b in (json.load(sys.stdin).get("items") or []):
    for s in (b.get("subjects") or []):
        if s.get("kind") == "ServiceAccount" and s.get("name") == name and s.get("namespace") == ns:
            print("clusterrole", (b.get("roleRef") or {}).get("name", ""))
            break
' "$ACTOR_SA" "$NS"
  $K -n "$NS" get rolebindings -o json 2>/dev/null | python3 -c '
import json, sys
name, ns = sys.argv[1], sys.argv[2]
for b in (json.load(sys.stdin).get("items") or []):
    for s in (b.get("subjects") or []):
        if s.get("kind") == "ServiceAccount" and s.get("name") == name and s.get("namespace") in (ns, None):
            ref = b.get("roleRef") or {}
            print(ref.get("kind", "").lower(), ref.get("name", ""))
            break
' "$ACTOR_SA" "$NS"
}

# The volatile metadata a snapshot must not carry back: `kubectl apply` of a document with a stale
# `resourceVersion` is rejected as a conflict, which would turn the restore into a no-op with a
# message nobody reads.
clean_meta() {
  python3 -c '
import json, sys
doc = json.load(sys.stdin)
for k in ("resourceVersion", "uid", "creationTimestamp", "generation", "managedFields", "selfLink"):
    doc.get("metadata", {}).pop(k, None)
json.dump(doc, sys.stdout)
'
}

JOURNAL_SNAPSHOT_DIR="$(mktemp -d)"
# Set BEFORE the first apply, not after the loop: a run killed mid-loop has stripped something, and
# the trap must know to repair even when it cannot know how far the loop got.
JOURNAL_STRIPPED=yes
stripped=0
# Fed by a heredoc rather than a pipe so the loop runs in THIS shell and `stripped` survives it.
while read -r ref_kind ref_name; do
  [ -n "$ref_name" ] || continue
  case "$ref_kind" in
    clusterrole) doc="$($K get clusterrole "$ref_name" -o json 2>/dev/null)" ;;
    role) doc="$($K -n "$NS" get role "$ref_name" -o json 2>/dev/null)" ;;
    *) continue ;;
  esac
  [ -n "$doc" ] || continue
  printf '%s' "$doc" | grep -q '"actionrecords' || continue
  printf '%s' "$doc" | clean_meta >"$JOURNAL_SNAPSHOT_DIR/$ref_kind.$ref_name.json"
  printf '%s' "$doc" | drop_actionrecords_rules | $K apply -f - >/dev/null 2>&1
  echo "  stripped actionrecords from $ref_kind/$ref_name"
  stripped=$((stripped + 1))
done <<EOF
$(bound_role_refs | sort -u)
EOF

if [ "$stripped" -eq 0 ]; then
  echo "DEFERRED: no role object bound to $ACTOR_SUBJECT grants actionrecords, so there was nothing"
  echo "  to revoke and no fault to stage. Either the grant moved or the bindings did; scenario B"
  echo "  cannot ask its question until this suite is pointed at wherever it went."
  exit 3
fi

# Converged on, not slept through (P9). RBAC changes propagate through the authorizer's caches, and
# a fixed sleep is how "the broker did not see the fault" becomes "the broker ignored the fault".
echo "  waiting for the authorizer to agree the grant is gone"
gone=no
deadline=$((SECONDS + 60))
while [ "$SECONDS" -lt "$deadline" ]; do
  if [ "$($K auth can-i list actionrecords --as="$ACTOR_SUBJECT" -n "$NS" 2>/dev/null)" = "no" ]; then
    gone=yes
    break
  fi
  sleep 2
done
if [ "$gone" != yes ]; then
  echo "DEFERRED: the actor can still list ActionRecords after 60s, so the fault was never staged."
  echo "  Scenario B would submit into a healthy journal and assert a 503 that is not coming — a"
  echo "  failure of the experiment, not of the broker."
  exit 3
fi
echo "  the authorizer says no; the journal is unreachable to the broker's identity"

# The brake caches its observation for `DefaultCacheTTL` (5s) and re-probes lazily on the next
# Observe. 15s is three TTLs — long enough that the next submission cannot be answered from a cache
# filled while the grant was still live, short enough that the cluster is not left blinded.
sleep 15

BROKER_DRIVER_EXTRA_ENV="PROBE_SCENARIO=journal-gone"
export BROKER_DRIVER_EXTRA_ENV

b_out="$(broker_driver_run "$K" "$NS" "$AGENT" "$AGENT" "$DRIVER_POD" "$DRIVER_CM" "$UNTRUSTED_SECRET")"
driver_rc=$?
if [ "$driver_rc" -ne 0 ]; then
  echo "DEFERRED: the driver pod could not be run to completion for scenario B."
  exit 3
fi
echo "$b_out" | sed 's/^/  | /'

B_FLAT="$(printf '%s\n' "$b_out" | flatten)"
if [ -z "$B_FLAT" ]; then
  echo "DEFERRED: the driver pod produced no parseable probe output for scenario B."
  exit 3
fi

b_nonce="$(field "$B_FLAT" nonce-accepted 1)"
if [ "$b_nonce" != "http" ]; then
  echo "DEFERRED: the door never opened for scenario B ($(field "$B_FLAT" nonce-accepted 9)). The"
  echo "  nonce route does not touch the journal, so this is a transport problem and not the fault"
  echo "  under test."
  exit 3
fi

b_status="$(field "$B_FLAT" submit 2)"
b_reason="$(field "$B_FLAT" submit 3)"
b_decision="$(field "$B_FLAT" submit 4)"
b_retry="$(field "$B_FLAT" submit 8)"
b_trace="$(field "$B_FLAT" target 5)"
b_target_name="$(field "$B_FLAT" target 9)"
b_target_ns="$(field "$B_FLAT" target 7)"
[ -n "$b_target_name" ] || b_target_name="$TARGET_NAME"
[ -n "$b_target_ns" ] || b_target_ns="$TENANT_NS"

if [ -z "$b_trace" ]; then
  echo "DEFERRED: the probe emitted no trace id for scenario B, so B-3 has nothing to search for."
  exit 3
fi
echo "  scenario B trace id: $b_trace"

# B-3 IS A NEGATIVE, SO IT IS OBSERVED OVER A WINDOW RATHER THAN POLLED UNTIL TRUE. There is no
# moment at which "no record was written" becomes final, so the honest form is: watch for 20s and
# fail if one ever appears. Searched while the grant is still stripped, because a restore first
# would leave a window in which a retry — if one existed — could write the record this arm is
# asserting the absence of, and the arm would be measuring the restore's timing.
B_N=0
deadline=$((SECONDS + 20))
while [ "$SECONDS" -lt "$deadline" ]; do
  B_N="$(records_for_trace "$b_trace" | sed -n 1p)"
  [ "${B_N:-0}" != "0" ] && break
  sleep 4
done
[ -n "$B_N" ] || B_N=0
echo "  ActionRecords in $NS carrying that trace id after 20s: $B_N"

if $K -n "$b_target_ns" get configmap "$b_target_name" >/dev/null 2>&1; then
  b_target_exists=yes
else
  b_target_exists=no
fi

assert_journal_gone "$b_status" "$b_reason" "$b_decision" "$b_retry" "$B_N" "$b_target_exists"

# ================================================================================================
# C — journal-restored. 05 §8 CH6's last sentence.
# ================================================================================================
echo
echo "== C: restoring the grant and submitting the same envelope again =="

# A FAILED RESTORE IS could-not-run, NOT a red arm. If the grant does not go back, arm C would be
# submitting into the SAME fault B just measured and asserting the opposite outcome — a failure of
# the experiment, not of the broker, and rc 1 would name the wrong defect (LSN-026). The trap still
# runs and will try again; the snapshots are kept either way.
if ! restore_journal_grant; then
  echo "DEFERRED: the actor's actionrecords grant could not be restored, so arm C has no recovered"
  echo "  journal to submit into. The cluster may still be blinded — see the WARNING above and the"
  echo "  kept snapshots."
  exit 3
fi

# Converged on, for the strip's reason inverted: the authorizer's caches take a moment to agree the
# grant is BACK, and a submission made before they do would be refused 503 by a brake that is right.
echo "  waiting for the authorizer to agree the grant is back"
back=no
deadline=$((SECONDS + 60))
while [ "$SECONDS" -lt "$deadline" ]; do
  if [ "$($K auth can-i list actionrecords --as="$ACTOR_SUBJECT" -n "$NS" 2>/dev/null)" = "yes" ]; then
    back=yes
    break
  fi
  sleep 2
done
if [ "$back" != yes ]; then
  echo "DEFERRED: the actor still cannot list ActionRecords 60s after the restore. Same reasoning as"
  echo "  above: arm C would be measuring a fault that was never lifted."
  exit 3
fi
echo "  the authorizer says yes again"

# The same three `DefaultCacheTTL`s as the strip, and for the mirror-image reason: the brake caches
# its observation, so a submission inside the TTL could be answered from a cache filled while the
# grant was still stripped. Sleeping is the honest way to ask "did it re-probe" rather than "did it
# happen to have expired" — and it is what makes a C-1 pass evidence of a re-probe.
sleep 15

BROKER_DRIVER_EXTRA_ENV="PROBE_SCENARIO=journal-restored"
export BROKER_DRIVER_EXTRA_ENV

c_out="$(broker_driver_run "$K" "$NS" "$AGENT" "$AGENT" "$DRIVER_POD" "$DRIVER_CM" "$UNTRUSTED_SECRET")"
driver_rc=$?
if [ "$driver_rc" -ne 0 ]; then
  echo "DEFERRED: the driver pod could not be run to completion for scenario C."
  exit 3
fi
echo "$c_out" | sed 's/^/  | /'

C_FLAT="$(printf '%s\n' "$c_out" | flatten)"
if [ -z "$C_FLAT" ]; then
  echo "DEFERRED: the driver pod produced no parseable probe output for scenario C."
  exit 3
fi

c_nonce="$(field "$C_FLAT" nonce-accepted 1)"
if [ "$c_nonce" != "http" ]; then
  echo "DEFERRED: the door never opened for scenario C ($(field "$C_FLAT" nonce-accepted 9))."
  exit 3
fi

c_status="$(field "$C_FLAT" submit 2)"
c_reason="$(field "$C_FLAT" submit 3)"
c_trace="$(field "$C_FLAT" target 5)"

if [ -z "$c_trace" ]; then
  echo "DEFERRED: the probe emitted no trace id for scenario C, so C-2 has nothing to search for."
  exit 3
fi
echo "  scenario C trace id: $c_trace"

# C-2 IS A POSITIVE, SO IT IS POLLED UNTIL TRUE — the mirror of B-3's window. The write-ahead Create
# happens before the reply is written, so the record is expected to be there already; the poll is
# for the API server's read-after-write, not for the broker.
C_N=0
deadline=$((SECONDS + 30))
while [ "$SECONDS" -lt "$deadline" ]; do
  C_N="$(records_for_trace "$c_trace" | sed -n 1p)"
  [ "${C_N:-0}" != "0" ] && break
  sleep 3
done
[ -n "$C_N" ] || C_N=0
echo "  ActionRecords in $NS carrying the post-restore trace id: $C_N"

# Re-resolved by ownership rather than re-read by the name held in POD_BEFORE: a `get pod/<name>`
# that 404s would return an empty string and land in the "not observed" arm, when the thing that
# actually happened — the pod is gone and a new one took over — is precisely what C-3 exists to
# catch and must report as a REPLACEMENT.
POD_AFTER="$(p3_pod_of_deploy "$K" "$NS" "$broker_deploy" 60)"
broker_pod="${POD_AFTER:-$broker_pod}"
RESTARTS_AFTER="$(broker_restarts)"
echo "  broker pod after the recovery: ${POD_AFTER:-<none>} (restarts: ${RESTARTS_AFTER:-<none>})"

assert_journal_restored "$c_status" "$c_reason" "$C_N" \
  "$POD_BEFORE" "$POD_AFTER" "$RESTARTS_BEFORE" "$RESTARTS_AFTER"

# ------------------------------------------------------------------------------------------------
if [ "$assertions" -ne "$EXPECTED_ASSERTIONS" ]; then
  echo
  bad "only $assertions of $EXPECTED_ASSERTIONS assertions ran. The verdict below would be about arms that never executed."
fi

echo
echo "===================================================================="
if [ "$fail" -eq 0 ]; then
  echo " PROVEN: V-BRK-018 · V-ISO-006 (05 §8 CH6) at L2 · the journal half of Phase 9 acceptance (d)"
  echo " A two-target envelope with one unreadable target was refused at step 3 and left no"
  echo " write-ahead record, no captured pre-state and neither object behind; a broker whose journal"
  echo " had been revoked out from under it refused 503 rather than executing unjournaled, and wrote"
  echo " nothing; and when the grant went back the same envelope was accepted and journaled by the"
  echo " same pod at the same restartCount — the journal recovered, the broker never had to."
  echo "===================================================================="
  exit 0
fi
echo " FAILED — see the FAIL lines above."
echo "===================================================================="
exit 1
