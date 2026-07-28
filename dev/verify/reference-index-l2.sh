#!/usr/bin/env bash
# reference-index-l2.sh — V-REV-010 at L2 (Phase 9, P9-T7c-3b).
#
# 09 §6.3: "A `recreate` downgrade is decided from live cluster state, not from the payload or from a
# partial scan ¬". Level L2, weight 9, negative control mandatory.
#
# What runs: a build-tagged Go probe (k8s-operator/test/l2/reference_index_l2_test.go) that drives
# refindex.Source -- the production undo.ReferenceIndex -- through undo.Generate against the cluster
# named below.
#
#   L2-1  The downgrade, live: an object with no inbound owner reference plans as `recreate`; the
#         same object plans as `none`, naming the referrer, once something owns it.
#   L2-2  NEGATIVE CONTROL: a Pod that mounts the target ConfigMap BY NAME must not downgrade
#         anything. This is the half that matters. A by-name reference resolves to a recreate under
#         the same name, so gating on it would gate every delete of any object anything mentions.
#         An adapter that reported every reference-shaped field it found would pass L2-1 perfectly.
#   L2-3  The harm, demonstrated rather than described. Deleting the owner and letting a REAL
#         garbage collector run shows the dependent being destroyed, and recreating the owner from
#         its snapshot shows a new UID and no dependent -- the exact state a `recreate` plan would
#         have reported as a completed undo. envtest runs no kube-controller-manager, so 06 §4.3.1's
#         premise is untestable anywhere below L2.
#   L2-4  The scan over the full live discovery surface. The adapter fails the WHOLE call on any
#         kind it cannot list; on envtest that clause is nearly free, and on GKE -- managed CRDs,
#         the metrics aggregation layer, whatever is installed -- it is the clause's real cost. If
#         it fires here, that is a fact about this deployment's grants and it belongs in the
#         evidence, not in a skip.
#
# WHY A GO PROBE RATHER THAN A CURL AT THE BROKER: same reason as classify-live-state-l2.sh. The
# broker's pipeline is `broker.UnavailablePipeline{}` until P9-T7c-3d wires these adapters into
# cmd/broker, so no deployed surface reaches the undo planner yet. What is under test is the
# production reference index against a real API server, which is what V-REV-010 states. When 3d
# lands, the end-to-end form belongs in broker-execute-l2.sh (P9-T9); this is not a substitute.
#
# DESTRUCTIVE-TEST GUARD: scratch-GKE contexts only. This script creates namespaces, creates objects
# inside them, and DELETES AN OBJECT TO PROVOKE THE GARBAGE COLLECTOR. The guard is anchored and
# duplicated in the Go probe, which is the thing that actually writes.
# Usage: dev/verify/reference-index-l2.sh [kube-context]
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions). Declared, not assumed:
#   P1 image-under-test: none — nothing under test here runs from a deployed image, so there is no
#      digest to pin. The probe compiles the WORKING TREE and connects to the cluster as a client,
#      which makes the build under test the working tree by construction — the property P1 exists to
#      establish. Asserting P1 anyway would mean pinning an image this check never looks at, and a
#      precondition satisfied by an irrelevant act reads as coverage while providing none. The
#      waiver has a named expiry: when P9-T7c-3d installs pipeline.New in cmd/broker, the reference
#      index under test becomes the one in the pod, and the end-to-end successor to this check
#      (broker-execute-l2.sh, P9-T9) needs P1 in full.
#   P3 admission-recreate: none — no object this probe reads predates the run. Namespaces are
#      created here with GenerateName, every ConfigMap, Pod and owner reference inside them is
#      created here, and all of it is deleted on the way out. P3 guards a claim made about an object
#      admitted under rules no longer in force; the create-reference-delete shape makes that
#      unrepresentable rather than merely unlikely. The writes are in the Go probe, not in this
#      shell. The one deliberate deletion (L2-3) targets an object this run created seconds earlier.
#   P6 runtime-authoritative: every ownerReference this probe plans against is RE-READ from the API
#      server by the index, never taken from the object that was submitted. The UID that makes an
#      owner reference match is assigned BY the API server and cannot be known before the write, so
#      reading anything but live state here would be circular — which is the property under test.
#   P10 control-plane-healthy: asserted below, before any verdict. The probe creates namespaces and
#      enumerates the full discovery surface; an API server that is not answering produces a red
#      that describes the cluster rather than the code, which is LSN-026 and cost three false
#      security failures. L2-3 additionally depends on the kube-controller-manager actually running
#      its garbage collector, and the probe fails loudly rather than skipping if it does not.
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
echo " V-REV-010 — the recreate downgrade follows the live owner graph"
echo " context: $CTX"
echo "===================================================================="

$K version >/dev/null 2>&1 || { echo "FAIL: context '$CTX' is not reachable." >&2; exit 1; }

# P10 (LSN-026), before any claim. rc 2 = could-not-run, never 1.
. "$REPO_ROOT/dev/lib/preconditions.sh"
p10_assert_control_plane_healthy "$K" "$CTX" || exit 2

# The probe is behind the `l2` build tag, so `go test ./...` and the L0 chain never compile it and no
# CI runner can reach a cluster by accident. -run pins this script to ITS OWN tests: test/l2/ is a
# shared package, and without the filter a red belonging to another check's probe would be reported
# here as V-REV-010 failing. Adding a test to this package means adding it to this pattern.
echo
echo "--- running the reference index against $CTX ---"
out="$(cd "$REPO_ROOT/k8s-operator" && KAGE_L2_CONTEXT="$CTX" \
  go test -tags l2 -count=1 -v -timeout 15m -run 'TestREV010' ./test/l2/ 2>&1)"
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
  "TestREV010RecreateDowngradeFollowsTheLiveOwnerGraph" \
  "TestREV010TheGarbageCollectorDoesWhatTheDowngradePrevents" \
  "TestREV010ScanCoversTheFullLiveDiscoverySurface"
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

# The negative control has to be seen to have RUN, not merely to have not failed. A probe that
# skipped step 2 entirely would exit 0 and satisfy every line above.
if printf '%s' "$out" | grep -q "step 2: negative control"; then
  pass "the mandatory negative control (09 §6) executed and is in the evidence above"
else
  bad "the negative control did not execute; a ¬ check with no negative control is not that check passing"
fi

# L2-3's whole value is that a real GC ran. If the cluster silently had none, the probe fails, but
# name the marker here too so the evidence file shows the demonstration happened.
if printf '%s' "$out" | grep -q "the garbage collector deleted the dependent"; then
  pass "the harm the downgrade prevents was demonstrated live, not asserted from a fixture"
else
  bad "the garbage-collection demonstration did not run; 06 §4.3.1's premise is unevidenced at L2"
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "V-REV-010: PASS at L2 on $CTX"
else
  echo "V-REV-010: FAIL on $CTX"
fi
exit "$fail"
