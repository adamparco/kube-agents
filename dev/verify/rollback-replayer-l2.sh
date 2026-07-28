#!/usr/bin/env bash
# rollback-replayer-l2.sh — V-REV-011 at L2 (Phase 9, P9-T7c-3c-ii).
#
# 09 §6.3: "A rollback replays the pre-state or refuses, and never replays a body that is not the
# pre-state ¬". Level L1+L2, weight 9, negative control mandatory. The L1 half is
# internal/broker/rollback/rollback_test.go plus rollback_envtest_test.go, run by `make test`; this
# is the half that needs controllers, a garbage collector and a kubelet.
#
# What runs: a build-tagged Go probe (k8s-operator/test/l2/rollback_replayer_l2_test.go) that drives
# rollback.Replayer — the production verify.Rollbacker — against the cluster named below.
#
#   L2-1  THE PRE-STATE IS THE WORLD, NOT A FIELD. A two-replica Deployment is scaled to zero, its
#         pods really drained, and the scale step replayed. The assertion is not `spec.replicas == 2`;
#         it is two Ready replicas put back by a real Deployment controller and a real kubelet. A
#         replayer that wrote the number and restored nothing passes every L1 assertion in the suite.
#         Paired with it: an annotation somebody else set between the action and the rollback must
#         SURVIVE, because a scale step restores one field and applying the whole snapshot would also
#         satisfy "the replicas are back".
#   L2-2  A REAL DELETION AND A REAL GARBAGE COLLECTOR. The Deployment is deleted with Foreground
#         propagation, and the probe waits for the GC to reap the ReplicaSets before recreating. At
#         L1 a delete is a bookkeeping change with nothing behind it; here the name becomes free on
#         the cluster's schedule and the recreate races the collection the way production does. The
#         restored object must get a NEW uid — otherwise the delete never happened and the leg is
#         vacuous — and the controller must take it up again.
#   L2-3  A NAME SOMEBODY ELSE TOOK. The recreate is refused with AlreadyExists, and the stranger is
#         re-read afterwards: same uid, same generation, same labels. Then the COUNTERFACTUAL, on a
#         fourth object: the same body, the same field manager, Apply instead of Create. If apply
#         also refused, the choice of verb in replayCreate would be arbitrary and the refusal above
#         would prove nothing. It does not refuse — it merges and reports success — and the probe
#         prints what it did.
#   L2-4  NEGATIVE CONTROL. Every clause of this check is "refuses rather than X", and each X is the
#         benign, plausible, passing answer; a probe that only staged refusals could not tell a
#         correct replayer from one that refuses the restores an operator is relying on. So every
#         refusal is paired with the control that must still succeed, and every refusal is followed by
#         a read of the live object. The headline is the Secret: a snapshot captured through the
#         production sanitizer holds `sha256:` placeholders instead of values, the replay is refused
#         NAMING the keys, and — the thing L1 cannot ask, because L1's writer is a fake — the live
#         Secret is read back and still holds its password at an unchanged resourceVersion. Its
#         control is a Secret body with its material intact, which must replay; that body has to be
#         hand-rehydrated, because undo.Sanitize redacts every Secret unconditionally and so the only
#         Secret body a real plan can carry is one the replayer must refuse. See the note below.
#   L2-5  THE UID PIN AGAINST A REAL DELETION. A ConfigMap is created, deleted for real, and recreated
#         at the same name; the original's snapshot is refused against the replacement, the
#         replacement is re-read unchanged, and the control — the same plan pinned to the uid the
#         replacement actually has — restores.
#   L2-6  THE FIRST FAILING STEP STOPS THE REPLAY AND SAYS HOW FAR IT GOT. A two-step plan whose first
#         step is a real successful write and whose second addresses a different object than its
#         target. The message must say one step has already been applied and is NOT reverted, and the
#         probe verifies that the first step's write really is on the cluster — otherwise the count in
#         the message is a number with nothing behind it. That sentence is what tells the operator
#         woken by the page whether the cluster is in the pre-state, the post-state, or neither.
#
# WHAT L2-4 TURNED UP, recorded here because the control leg is where a reader will meet it: A
# SECRET RESTORE NEVER SUCCEEDS IN PRODUCTION TODAY. undo.Sanitize redacts every Secret with no
# switch to turn it off — correctly, since a Secret body persisted into a CRD with its material
# intact would be the exfiltration the design exists to prevent — so the only Secret body a plan can
# carry is one this replayer must refuse. Meanwhile the plan's own caveat tells the operator the
# material "lives in the journal store and is verified against those digests on replay". It does
# not: the only unredacted copy is execute.Snapshot.Live, which never leaves memory. The refusal is
# the right behaviour and the caveat is the wrong promise. Closing the gap is the journal.BlobSink
# work, which is deferred and human-owned; the finding is in this unit's ledger row.
#
# WHAT THIS CHECK DOES NOT CLAIM: the out-of-band digest clause end to end. A step whose body lives
# in the journal store needs a journal.BlobSink, and the production sink (bucket + Workload Identity)
# is a deferred, human-owned item. The digest comparison itself is asserted at L1 against a fake
# sink; what L2-6's tail adds is that the nil-sink path is a NAMED refusal on a real cluster rather
# than a panic on a nil interface. When the production sink lands, the end-to-end form belongs with
# it, not here.
#
# WHY A GO PROBE RATHER THAN A CURL AT THE BROKER: same reason as verify-prober-l2.sh. The broker's
# pipeline is `broker.UnavailablePipeline{}` until P9-T7c-3d wires these adapters into cmd/broker, so
# no deployed surface reaches the replayer yet. What is under test is the production Rollbacker
# against a real cluster with real controllers, which is what V-REV-011 states. When 3d lands, the
# end-to-end form belongs in broker-execute-l2.sh (P9-T9); this is not a substitute.
#
# DESTRUCTIVE-TEST GUARD: scratch-GKE contexts only. This script creates namespaces, runs workloads
# in them, scales one to zero, DELETES OBJECTS FOR REAL so the API server issues new uids, and
# deliberately merges a foreign body into one object to demonstrate what apply would have done. The
# guard is anchored and duplicated in the Go probe, which is the thing that actually writes.
# Usage: dev/verify/rollback-replayer-l2.sh [kube-context]
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions). Declared, not assumed:
#   P1 image-under-test: none — nothing under test here runs from a deployed image, so there is no
#      digest to pin. The probe compiles the WORKING TREE and connects to the cluster as a client,
#      which makes the build under test the working tree by construction — the property P1 exists to
#      establish. Asserting P1 anyway would mean pinning an image this check never looks at, and a
#      precondition satisfied by an irrelevant act reads as coverage while providing none. The waiver
#      has a named expiry: when P9-T7c-3d installs pipeline.New in cmd/broker, the replayer under test
#      becomes the one in the pod, and the end-to-end successor to this check (broker-execute-l2.sh,
#      P9-T9) needs P1 in full.
#   P3 admission-recreate: none — no object this probe reads predates the run, and two of its six legs
#      exist precisely BECAUSE an object is deliberately deleted and recreated so that its uid
#      changes. Namespaces are created here with GenerateName; every Deployment, ConfigMap and Secret
#      inside them is created here and deleted on the way out. P3 guards a claim made about an object
#      admitted under rules no longer in force; the create-observe-delete shape makes that
#      unrepresentable rather than merely unlikely. The writes are in the Go probe, not in this shell.
#   P6 runtime-authoritative: this check IS the runtime-authoritative property, twice over. Every
#      verdict is re-read from the API server after a controller acted — ready replica counts, the new
#      uid the recreate got, the stranger's generation, the Secret's material, the first step's write.
#      Nothing is taken from the object that was submitted or from the replayer's return value. L2-1
#      is the sharpest case: the replayer returning nil is exactly the answer that must not be
#      believed, because "the request was accepted" and "the pre-state is back" are different claims.
#   P10 control-plane-healthy: asserted below, before any verdict. The probe creates namespaces, waits
#      on the Deployment, ReplicaSet and garbage-collection controllers, and needs a kubelet to run
#      pause containers; an API server that is not answering produces a red that describes the cluster
#      rather than the code, which is LSN-026 and cost three false security failures. L2-1 and L2-2
#      additionally depend on a schedulable node; the probe fails loudly with its last observation
#      rather than skipping if one is absent.
set -uo pipefail

CTX="${1:-gke-scratch-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

case "$CTX" in
  gke-scratch-*) : ;;
  *) echo "REFUSING: context '$CTX' is not a scratch cluster (destructive-test guard)." >&2; exit 2 ;;
esac

K="kubectl --context $CTX"

fail=0
pass() { echo "PASS: $1"; }
bad()  { echo "FAIL: $1"; fail=1; }
cd "$REPO_ROOT"

echo "===================================================================="
echo " V-REV-011 — a rollback replays the pre-state or refuses"
echo " context: $CTX"
echo "===================================================================="

$K version >/dev/null 2>&1 || { echo "FAIL: context '$CTX' is not reachable." >&2; exit 1; }

# P10 (LSN-026), before any claim. rc 2 = could-not-run, never 1.
. "$REPO_ROOT/dev/lib/preconditions.sh"
p10_assert_control_plane_healthy "$K" "$CTX" || exit 2

# The probe is behind the `l2` build tag, so `go test ./...` and the L0 chain never compile it and no
# CI runner can reach a cluster by accident. -run pins this script to ITS OWN tests: test/l2/ is a
# shared package, and without the filter a red belonging to another check's probe would be reported
# here as V-REV-011 failing. Adding a test to this package means adding it to this pattern.
#
# The timeout is 25m rather than 15m: three separate legs wait on a kubelet to schedule and pull
# registry.k8s.io/pause, and on the garbage collector to reap a ReplicaSet under Foreground
# propagation, which is the part that varies with cluster load rather than with the code.
echo
echo "--- running the replayer against $CTX ---"
out="$(cd "$REPO_ROOT/k8s-operator" && KAGE_L2_CONTEXT="$CTX" \
  go test -tags l2 -count=1 -v -timeout 25m -run 'TestREV011' ./test/l2/ 2>&1)"
rc=$?
echo "$out"
echo "--- end probe output ---"
echo

# Verdicts are read off the probe's own PASS lines rather than off its exit code alone, so a compile
# failure or a panic cannot be mistaken for "no assertion failed".
if [ "$rc" -ne 0 ]; then
  bad "the L2 probe exited $rc — see the output above"
fi

for want in \
  "TestREV011ARealReplayRestoresTheWorldAndNotJustTheField" \
  "TestREV011ARecreateAfterARealDeletionRefusesANameSomebodyElseTook" \
  "TestREV011NoRefusalIsSilentAndNoRestoreIsARewrite"
do
  if printf '%s' "$out" | grep -q -- "--- PASS: $want"; then
    pass "$want"
  else
    bad "$want did not report PASS — the assertion did not run, or it failed"
  fi
done

# -run takes a regexp, and a typo in it silently matches nothing while still exiting 0 -- `go test`
# prints `ok ... [no tests to run]` and a warning on stderr. The three named PASS lines above do
# catch that, but they report it as three assertion failures, which sends a reader looking at the
# cluster for a bug that is in this file. Name the real cause. Both forms are checked: an empty
# selection is not an `ok` line this script accepts.
if printf '%s' "$out" | grep -q "no tests to run"; then
  bad "the -run pattern selected NOTHING — this is a bug in this script, not in the cluster or the code"
elif printf '%s' "$out" | grep -q "^ok  .*test/l2"; then
  pass "the probe package ran to completion"
else
  bad "no 'ok' line for test/l2 — the package did not build, or it panicked"
fi

# Each numbered step has to be seen to have RUN, not merely to have not failed. A probe that returned
# early would exit 0 and satisfy every line above. The step markers are printed by the probe as it
# passes each one, so their absence is the difference between "asserted" and "reached".
while IFS='|' read -r marker what; do
  [ -n "$marker" ] || continue
  if printf '%s' "$out" | grep -q -- "$marker"; then
    pass "$what"
  else
    bad "$what — this step did not execute"
  fi
done <<'STEPS'
step 1: the pre-state came back as running pods|L2-1 a real controller restored the world
step 2: the replay reverted only the field|L2-1 the bystander's change survived, under the agent's manager
step 3: a real deletion, with the real garbage collector|L2-2 a real deletion and a real GC
step 4: the recreate restored the workload onto the freed name|L2-2 the recreate onto a freed name
step 5: the recreate refused a taken name|L2-3 a name somebody else took
step 6: apply at a taken name|L2-3 the counterfactual — what apply would have done
step 7: negative control -- the redacted Secret was refused|L2-4 the mandatory negative control (09 §6)
step 8: the live Secret still holds its material|L2-4 the refusal wrote nothing
step 9: the control -- a Secret body with its material intact replays|L2-4 the control against a blanket refusal
step 10: a snapshot was refused against a post-deletion replacement|L2-5 the uid pin after a real deletion
step 11: the control -- a correctly-pinned restore writes|L2-5 the control against a blanket refusal
step 12: the replay stopped at the failing step|L2-6 the partial replay is reported, not hidden
step 13: an out-of-band body with no sink refused by name|L2-6 a missing sink refuses rather than panicking
STEPS

# The two sharpest lines, named separately so the evidence file records that these specific
# substitutions were tested rather than merely that their steps ran.
#
# The Secret one is the security property: at L1 "nothing was written" is a property of a fake
# writer, and the question an operator actually has is whether the live Secret still has its
# password. The counterfactual is the design justification for execute.ClientApplier.Create existing
# at all -- if apply refused here too, the verb would not matter.
if printf '%s' "$out" | grep -q "step 8: the live Secret still holds its material"; then
  pass "a replay of a redacted snapshot did not overwrite live Secret material with its own digests"
else
  bad "the live-Secret read-back did not run — the refusal was asserted but its effect was not"
fi
if printf '%s' "$out" | grep -q "step 6: apply at a taken name"; then
  pass "the create-vs-apply counterfactual was measured on a real cluster, not argued"
else
  bad "the counterfactual did not run — the refusal in step 5 proves nothing about the verb"
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "V-REV-011: PASS at L2 on $CTX"
else
  echo "V-REV-011: FAIL on $CTX"
fi
exit "$fail"
