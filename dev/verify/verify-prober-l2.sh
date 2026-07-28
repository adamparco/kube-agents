#!/usr/bin/env bash
# verify-prober-l2.sh — V-PRO-027 at L2 (Phase 9, P9-T7c-3c-i).
#
# 09 §6.6: "Verification evidence is read from the cluster, and a probe that cannot observe its
# property says so rather than substituting a benign default ¬". Level L1+L2, weight 9, negative
# control mandatory. The L1 half is internal/broker/probe/probe_envtest_test.go, run by `make test`;
# this is the half that needs controllers.
#
# What runs: a build-tagged Go probe (k8s-operator/test/l2/verify_prober_l2_test.go) that drives
# probe.Source -- the production verify.Prober -- against the cluster named below.
#
#   L2-1  The write returned success and the verdict is not Satisfied. That sentence is the whole of
#         04 §5.1's opening, and it is only assertable where a REAL deployment controller has not yet
#         converged. Then the rollout converges and the same predicate flips to Satisfied.
#   L2-2  A REAL KUBELET restarts a real container. Two Deployments in one namespace: `steady` on
#         pause, and `flapper` on busybox running `sleep 20; exit 7` -- Ready first, dead after, which
#         is the "rolled out and then fell over" shape the restart clause exists for. flapper's count
#         must go above zero WHILE steady's stays at zero, at the same moment. A prober that answered
#         from the namespace, or from the write's return value, cannot pass both halves of that pair.
#   L2-3  A REAL ENDPOINT-SLICE CONTROLLER publishes the endpoint count, from live pod readiness. And
#         the legitimate zero: a Service selecting nothing is (0, nil), because the predicate reads
#         zero as Pending and turning it into an error would make every not-yet-ready Service a probe
#         failure. The programmed address is the cluster IP the API server really assigned.
#   L2-4  GKE'S REAL AUTHORIZER CHAIN answers the SubjectAccessReview -- RBAC and the IAM webhook
#         authorizer, not RBAC alone as at L1. Both directions: the bound subject allowed, the unbound
#         one denied, and the denial arriving as an answer rather than as an evaluation error.
#   L2-5  NEGATIVE CONTROL. Every clause of this check is "returns X rather than Y", and each Y is the
#         benign, plausible, passing answer. The headline is the two-leg admission probe in a
#         Pod-Security-`restricted` namespace: on GKE the admission chain has more in it than envtest
#         has, so "submit something illegal and watch it be rejected" has several sources other than
#         the LimitRange under test. Both legs are refused there, so the probe must report
#         not-observed. A one-leg implementation reports true, and would go on reporting true for a
#         LimitRange that had been deleted. Alongside it: an uncountable restart count errors rather
#         than returning zero; a routing object with no Service backend errors rather than returning
#         zero; an absent capability is ErrProbeUnsupported rather than a satisfied answer; and the
#         dry-run admission probe persists nothing on a cluster where a leftover pod would schedule.
#   L2-6  The settle-window replacement. A ConfigMap is created, its uid captured, DELETED for real --
#         real finalizers, real garbage collector -- and recreated at the same name. The replacement
#         is present and healthy, so every other probe in the package would verify it; only the uid
#         pin refuses it. envtest cannot stage a real deletion, so this is an L2 property.
#
# WHAT THIS CHECK DOES NOT CLAIM: the provider row. probe.ProviderState reads Config Connector's
# ContainerNodePool / ContainerCluster status, and Config Connector is not installed on the scratch
# cluster. That row's L2 instance is V-PRO-013, which is deferred against 09 §12 row T-9 and owns its
# own blocker. V-PRO-027 was written narrowly enough that this is a note rather than a skip: none of
# the six clauses above touches the provider row.
#
# WHY A GO PROBE RATHER THAN A CURL AT THE BROKER: same reason as reference-index-l2.sh. The broker's
# pipeline is `broker.UnavailablePipeline{}` until P9-T7c-3d wires these adapters into cmd/broker, so
# no deployed surface reaches the verifier yet. What is under test is the production prober against a
# real cluster with real controllers, which is what V-PRO-027 states. When 3d lands, the end-to-end
# form belongs in broker-execute-l2.sh (P9-T9); this is not a substitute.
#
# DESTRUCTIVE-TEST GUARD: scratch-GKE contexts only. This script creates namespaces, runs workloads
# in them, deliberately crashloops one, and DELETES AN OBJECT to make the API server issue a new uid.
# The guard is anchored and duplicated in the Go probe, which is the thing that actually writes.
# Usage: dev/verify/verify-prober-l2.sh [kube-context]
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions). Declared, not assumed:
#   P1 image-under-test: none — nothing under test here runs from a deployed image, so there is no
#      digest to pin. The probe compiles the WORKING TREE and connects to the cluster as a client,
#      which makes the build under test the working tree by construction — the property P1 exists to
#      establish. Asserting P1 anyway would mean pinning an image this check never looks at, and a
#      precondition satisfied by an irrelevant act reads as coverage while providing none. The waiver
#      has a named expiry: when P9-T7c-3d installs pipeline.New in cmd/broker, the prober under test
#      becomes the one in the pod, and the end-to-end successor to this check (broker-execute-l2.sh,
#      P9-T9) needs P1 in full.
#   P3 admission-recreate: none — no object this probe reads predates the run, and the whole point of
#      L2-6 is that one of them is deliberately recreated so that its uid changes. Namespaces are
#      created here with GenerateName; every Deployment, Service, Role, LimitRange, PDB, Ingress and
#      ConfigMap inside them is created here and deleted on the way out. P3 guards a claim made about
#      an object admitted under rules no longer in force; the create-observe-delete shape makes that
#      unrepresentable rather than merely unlikely. The writes are in the Go probe, not in this shell.
#   P6 runtime-authoritative: this check IS the runtime-authoritative property. Every number it
#      asserts — restart counts, endpoint counts, the programmed address, the admission verdicts, the
#      authorization decisions, the uid — is re-read from the API server after a controller wrote it,
#      never taken from the object that was submitted. Reading anything but live state here would be
#      circular, which is what L2-1 and L2-5 exist to make impossible.
#   P10 control-plane-healthy: asserted below, before any verdict. The probe creates namespaces, waits
#      on four separate controllers, and submits dry-run admission requests; an API server that is not
#      answering produces a red that describes the cluster rather than the code, which is LSN-026 and
#      cost three false security failures. L2-2 additionally depends on a schedulable node with a
#      running kubelet, and L2-3 on the endpoint-slice controller; the probe fails loudly with its
#      last observation rather than skipping if either is absent.
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
echo " V-PRO-027 — verification evidence is read from the cluster"
echo " context: $CTX"
echo "===================================================================="

$K version >/dev/null 2>&1 || { echo "FAIL: context '$CTX' is not reachable." >&2; exit 1; }

# P10 (LSN-026), before any claim. rc 2 = could-not-run, never 1.
. "$REPO_ROOT/dev/lib/preconditions.sh"
p10_assert_control_plane_healthy "$K" "$CTX" || exit 2

# The probe is behind the `l2` build tag, so `go test ./...` and the L0 chain never compile it and no
# CI runner can reach a cluster by accident. -run pins this script to ITS OWN tests: test/l2/ is a
# shared package, and without the filter a red belonging to another check's probe would be reported
# here as V-PRO-027 failing. Adding a test to this package means adding it to this pattern.
#
# The timeout is 20m rather than 15m: L2-2 waits on a kubelet to schedule, pull busybox, run it for
# twenty seconds, and restart it, and the pull is the part that varies with the node's cache.
echo
echo "--- running the prober against $CTX ---"
out="$(cd "$REPO_ROOT/k8s-operator" && KAGE_L2_CONTEXT="$CTX" \
  go test -tags l2 -count=1 -v -timeout 20m -run 'TestPRO027' ./test/l2/ 2>&1)"
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
  "TestPRO027EvidenceIsReadFromTheClusterNotFromTheWrite" \
  "TestPRO027NoProbeSubstitutesABenignDefault" \
  "TestPRO027AReplacedTargetIsNotTheOneTheActionTouched"
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
step 1: the write returned success and the verdict is not satisfied|L2-1 the unconverged write
step 2: the rollout converged and the prober saw it happen|L2-1 the converged rollout
step 3: restarts are counted live, per workload|L2-2 the live kubelet restart, isolated to its workload
step 4: endpoints come from the endpointslice controller|L2-3 endpoints published by a controller
step 5: subjectaccessreview answered by the cluster's real authorizer chain|L2-4 the real authorizer chain
step 6: negative control|L2-5 the mandatory negative control (09 §6)
step 7: the settle-window replacement|L2-6 the replaced target
STEPS

# The single sharpest line in the negative control: both admission legs refused by something that is
# not the LimitRange, and the probe still saying it observed nothing. Named separately so the
# evidence file records that this specific substitution was tested, not just that step 6 ran.
if printf '%s' "$out" | grep -q "the two-leg admission probe reported not-observed"; then
  pass "a one-leg admission probe would have claimed enforcement here and this one did not"
else
  bad "the two-leg admission probe's discriminating case did not run"
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "V-PRO-027: PASS at L2 on $CTX"
else
  echo "V-PRO-027: FAIL on $CTX"
fi
exit "$fail"
