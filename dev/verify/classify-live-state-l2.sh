#!/usr/bin/env bash
# classify-live-state-l2.sh — V-GAT-022 at L2 (Phase 9, P9-T7c-3a).
#
# 09 §6: "Classification reads **live state**, not the payload: a byte-identical envelope classifies
# differently once the target namespace gains its production label; a payload asserting the label
# does not ¬". Level L2, weight 10, negative control mandatory.
#
# What runs: a build-tagged Go probe (k8s-operator/test/l2/live_state_l2_test.go) that drives
# classify.ClientLiveState -- the production LiveState -- against the cluster named below. It
# performs the four-step experiment the check describes and reports each class transition.
#
#   L2-1  V-GAT-022 positive: the SAME envelope classifies higher once the target namespace gains
#         kube-agents/environment=production. Nothing about the request changes between the two runs.
#   L2-2  V-GAT-022 NEGATIVE CONTROL: on an unlabelled namespace, an envelope whose PAYLOAD asserts
#         the production label classifies identically to one that does not. This is the half that
#         matters. A classifier reading the payload passes L2-1 perfectly and is worthless, because
#         the caller writes the payload and would therefore choose its own risk class.
#   L2-3  06 §4.2's ladder, live: an object labelled staging inside a production namespace is
#         STAGING. Without this a classifier that ORed the four rungs together would pass L2-1, and
#         a staging carve-out inside a production cluster would be impossible to express.
#   L2-4  The blast-radius denominator over a real discovery surface. GKE serves its own CRDs, the
#         metrics aggregation layer and whatever the customer installed; envtest serves none of
#         that. The count is asserted to move by exactly the number of unowned objects created, and
#         the kind count is logged so "how much of this cluster can the broker actually see" is on
#         the record rather than assumed.
#
# WHY A GO PROBE RATHER THAN A CURL AT THE BROKER: the broker's pipeline is still
# `broker.UnavailablePipeline{}` until P9-T7c-3d wires the adapters into cmd/broker, so there is no
# deployed surface that reaches the classifier yet. What is under test here is the classifier plus
# its live-state adapter against a real GKE API server, which is exactly what V-GAT-022 states and
# what an envtest server cannot provide: a real discovery surface, real RBAC, and real admission on
# the namespace whose labels are being read. When 3d lands, the end-to-end form of this belongs in
# broker-execute-l2.sh (P9-T9); this script is not a substitute for that and does not claim to be.
#
# DESTRUCTIVE-TEST GUARD: scratch-GKE contexts only. This script creates and deletes namespaces.
# The guard is anchored and duplicated in the Go probe, which is the thing that actually writes.
# Usage: dev/verify/classify-live-state-l2.sh [kube-context]
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions). Declared, not assumed:
#   P1 image-under-test: none — nothing under test here runs from a deployed image, so there is no
#      digest to pin. The probe compiles the WORKING TREE and connects to the cluster as a client,
#      which makes the build under test the working tree by construction — the very property P1
#      exists to establish. Asserting P1 anyway would mean deploying an image this check never
#      looks at, and a precondition satisfied by an irrelevant act is worse than an absent one
#      because it reads as coverage. This waiver has an expiry and it is named: when P9-T7c-3d
#      installs pipeline.New in cmd/broker, the classifier under test becomes the one in the pod,
#      and the end-to-end successor to this check (broker-execute-l2.sh, P9-T9) needs P1 in full.
#   P3 admission-recreate: none — no object this probe reads predates the run. The namespace is
#      created here with GenerateName, every ConfigMap and Secret inside it is created here, and
#      all of it is deleted on the way out, so there is nothing for an older admission ruleset to
#      have grandfathered. P3 guards against a claim made about an object admitted under rules that
#      are no longer in force; the create-classify-delete shape makes that unrepresentable rather
#      than merely unlikely. The creates and deletes are in the Go probe, not in this shell.
#   P6 runtime-authoritative: every label this probe classifies against is RE-READ from the API
#      server after the write, never assumed from the object that was submitted. A namespace passes
#      through PSA and whatever else this cluster installs, so the label set the classifier sees is
#      the cluster's, not the request's. That is the property under test, so reading anything else
#      would be circular.
#   P10 control-plane-healthy: asserted below, before any verdict. The probe creates namespaces and
#      reads discovery; an API server that is not answering produces a red that describes the
#      cluster rather than the code, which is LSN-026 and cost three false security failures.
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
echo " V-GAT-022 — classification reads live state, not the payload"
echo " context: $CTX"
echo "===================================================================="

$K version >/dev/null 2>&1 || { echo "FAIL: context '$CTX' is not reachable." >&2; exit 1; }

# P10 (LSN-026), before any claim. rc 2 = could-not-run, never 1.
. "$REPO_ROOT/dev/lib/preconditions.sh"
p10_assert_control_plane_healthy "$K" "$CTX" || exit 2

# The probe is behind the `l2` build tag, so `go test ./...` and the L0 chain never compile it and
# no CI runner can reach a cluster by accident. Both assertions are in one package and one run: they
# share the connection cost, and a failure in either is a failure of this script.
#
# -run pins this script to ITS OWN tests. test/l2/ is a shared package and other checks now keep
# probes in it; without the filter, their red would be reported here as V-GAT-022 failing, and a
# check that goes red for a reason outside its own subject is a check nobody can act on.
echo
echo "--- running the classifier against $CTX ---"
out="$(cd "$REPO_ROOT/k8s-operator" && KAGE_L2_CONTEXT="$CTX" \
  go test -tags l2 -count=1 -v -timeout 15m -run 'TestGAT022' ./test/l2/ 2>&1)"
rc=$?
echo "$out"
echo "--- end probe output ---"
echo

# Verdicts are read off the probe's own PASS lines rather than off its exit code alone, so a
# compile failure or a panic cannot be mistaken for "no assertion failed".
if [ "$rc" -ne 0 ]; then
  bad "the L2 probe exited $rc — see the output above"
fi

for want in \
  "TestGAT022ClassificationReadsLiveStateNotThePayload" \
  "TestGAT022DenominatorOverARealDiscoverySurface"
do
  if printf '%s' "$out" | grep -q -- "--- PASS: $want"; then
    pass "$want"
  else
    bad "$want did not report PASS — the assertion did not run, or it failed"
  fi
done

# The negative control has to be seen to have RUN, not merely to have not failed. A probe that
# skipped step 2 entirely would exit 0 and satisfy every line above.
if printf '%s' "$out" | grep -q "step 2: negative control"; then
  pass "the mandatory negative control (09 §6) executed and is in the evidence above"
else
  bad "the negative control did not execute; a ¬ check with no negative control is not that check passing"
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "V-GAT-022: PASS at L2 on $CTX"
else
  echo "V-GAT-022: FAIL on $CTX"
fi
exit "$fail"
