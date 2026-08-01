#!/usr/bin/env bash
# webhook-negatives-l2.sh — V-CTR-002 in full: every one of 06 §1.2's rules V-1…V-10 has a negative
# test that is REJECTED, and the rejection NAMES THE FIELD PATH (Phase 8, P8-T9).
#
# It also carries V-CTN-015 and V-CTN-016 at L2, bound 2026-07-31 (P9-T11g-4).
#
# WHY TWO CONTAINMENT ROWS LIVE IN A CONTRACT SUITE
#   09 §6 lines 321-322 are `V-CTN-015 | (tier, scope) cardinality: a duplicate Agent CR is rejected
#   ¬ | 08 §7 | L2 | 8` and `V-CTN-016 | Developer-team placement: metadata.namespace must equal
#   spec.scope.namespace ¬ | 08 §7 | L2 | 8`. Those are the V-5 and V-4 arms below, sentence for
#   sentence, and they have been running since P8-T9 under V-CTR-002's name alone.
#
#   The two rows are not a restatement of V-CTR-002. V-CTR-002 asks a CONTRACT question — does every
#   rule in 06 §1.2 have a negative test whose rejection names its field path — and it would stay
#   green if the placement rule were re-scoped to something harmless, as long as the rejection kept
#   naming `metadata.namespace`. V-CTN-015 and V-CTN-016 ask the CONTAINMENT question 08 §7 asks: a
#   developer-team agent that is not in the namespace it scopes has its pod rendered outside the
#   tenant's isolation controls, and a duplicate (tier, scope) means two actor identities answering
#   for one scope with no arbiter between them. Same arms, and the reason both IDs point here is that
#   one execution is better evidence than two — but a reader who breaks the rule must see all three
#   rows go red, not one.
#
#   BOTH ARMS GAINED A POSITIVE CONTROL WHEN THE IDS WERE BOUND, because both are ¬ rows in 09 §6 and
#   09 line 292 is explicit: "Negative controls are mandatory for every check marked ¬. A check that
#   only demonstrates the happy path is not evidence for a security or safety property." Read in the
#   direction that matters here, a check that only demonstrates the REJECTION is not evidence either
#   — an over-blocking rule refuses the fixture and every legitimate object beside it. V-4 also had
#   its parent changed; the arm says why.
#
# P8-T1 delivered the V-7 slice (dev/verify/closed-allowlist-l2.sh) and recorded V-CTR-002 as
# `partial` with the gap named: V-6, V-8 and V-10 were not implemented in the webhook at all, so
# there was nothing to negatively test. P8-T9 implements those three rules and this script closes
# the check.
#
# WHY THE FIELD PATH IS ASSERTED AND NOT JUST THE REJECTION. A test that only checks "the API server
# said no" passes just as happily when the API server said no for an unrelated reason — a typo in the
# fixture, a missing required field, a CRD that failed to install. Every arm below therefore asserts
# the specific field path the rule is supposed to name. That is also the operator-facing half of the
# rule: a rejection that does not say what to fix sends the reader to the CRD instead of to their own
# manifest.
#
# WHY THIS CANNOT BE A GO TEST. `internal/webhook`'s unit tests call the validator function directly.
# They cannot prove the webhook is REGISTERED, that its failurePolicy is `fail`, that the CRD's CEL
# compiles, or that the two layers agree about which one rejects first. Only a real API server runs
# admission, and nothing in a Go build ever compiles CEL (LSN-016). The L1 tests and this script cover
# genuinely different failures; neither replaces the other.
#
# THE ONE ARM THAT IS NOT A REJECTION. V-9 ("no authority fields") is enforced by the CRD's
# STRUCTURAL SCHEMA, and a structural schema PRUNES an unknown field rather than refusing the object
# (06 §1.2 names the outcome "field pruned", not `Invalid`). Asserting a rejection there would be
# asserting the wrong mechanism, and it would fail against a correct implementation. That arm reads
# the object the API server echoes back and asserts the forbidden field is GONE — which is the actual
# security property, since a pruned `spec.rbac` grants nothing.
#
# DESTRUCTIVE-TEST GUARD: scratch-GKE contexts only.
# Usage: dev/verify/webhook-negatives-l2.sh [kube-context]
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions):
#   P1 image-under-test:  kubeagents-system/control-plane=controller-manager — every rejection below is produced by the
#      webhook served BY the operator image. A stale operator rejects with the OLD rules, and the
#      three rules this unit adds would show as green against code that is not running. That is
#      LSN-001 aimed squarely at a suite whose whole output is "the new rules fire".
#      Asserted via p1_assert_build_under_test.
#   P3 admission-recreate: every negative fixture is submitted with --dry-run=server, so admission runs in full on a
#      freshly-constructed object each time and nothing is grandfathered. The one PERSISTED object
#      (the parent below) is deleted and recreated by this script rather than reused.
#   P6 runtime-authoritative: the LIVE CRD and the LIVE webhook configuration, not the checked-in YAML, which is an input.
#      Section 0 re-applies the CRD server-side so the rules under test are the ones in the tree.
#   P10 cluster-health: p10_assert_control_plane_healthy — a wedged API server rejects everything, which would score a
#      perfect 10/10 on a suite made entirely of expected rejections. This suite is the single most
#      false-green-prone one in the build, which is why the positive controls in section 12 are not
#      optional garnish.
set -uo pipefail

CTX="${1:-gke-scratch-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

case "$CTX" in
  gke-scratch-*) : ;;
  *) echo "REFUSING: context '$CTX' is not a scratch cluster (destructive-test guard)." >&2; exit 2 ;;
esac

K="kubectl --context $CTX"
CRD="$REPO_ROOT/k8s-operator/config/crd/bases/kubeagents.x-k8s.io_agents.yaml"
NS=kubeagents-system
PROJECT=vctr-l2-negatives
PARENT=wn-platform-parent
TEAM_NS=wn-team-x

# WHY THE FAILURE FLAG IS A FILE AND NOT A VARIABLE. Nearly every assertion in this file is invoked
# as `child_yaml ... | reject ...`, and in bash EVERY component of a pipeline runs in a subshell. A
# `fail=1` set inside `reject` is therefore assigned in a child process and discarded when it exits.
# Until 2026-07-31 this suite printed `FAIL:` lines and exited 0 with a PASS banner — the exit code
# no consumer could distinguish from a clean run, on the line `dev/L2-CHAIN.txt` uses to gate V-CTR-002
# and (from this unit) V-CTN-015 and V-CTN-016. It was found by breaking the suite on purpose while
# binding those two IDs, which is the only reason it was found at all: a suite that cannot go red
# looks exactly like a suite that is passing.
#
# A file survives the subshell. `bad` is the single choke point — every `reject`, `admit` and inline
# failure goes through it — so recording there covers call sites that do not exist yet.
FAILFILE="$(mktemp)"
pass() { echo "PASS: $1"; }
bad()  { echo "FAIL: $1"; echo x >>"$FAILFILE"; }
# The count is read once at the end; `fail` is derived, never assigned by an assertion.
failures() { [ -s "$FAILFILE" ] && wc -l <"$FAILFILE" | tr -d ' ' || echo 0; }
cd "$REPO_ROOT" || exit 1

echo "===================================================================="
echo " V-CTR-002 — 06 §1.2 V-1…V-10 negative suite — ctx: $CTX"
echo "===================================================================="

$K version >/dev/null 2>&1 || { echo "FAIL: context '$CTX' is not reachable." >&2; exit 1; }

# P10 (LSN-026), before any claim. rc 2 = could-not-run, never 1: a suite of expected rejections
# cannot tell "the rule fired" from "the cluster is broken" without this.
. "$REPO_ROOT/dev/lib/preconditions.sh"
p10_assert_control_plane_healthy "$K" "$CTX" || exit 2

# --- P1 -----------------------------------------------------------------------------------------
p1_assert_build_under_test "$K" "$NS" control-plane=controller-manager
case "$?" in
  0) pass "P1: the running operator is the build under test" ;;
  3) echo "DEFERRED: P1 unverifiable (see above). Every claim below is webhook behaviour, so none of"
     echo "  them would mean anything against an operator that may not be this source."
     exit 3 ;;
  *) bad "P1: the cluster is not running the build under test"; exit 1 ;;
esac

# --- section 0: the CRD installs (this is what compiles the CEL) ---------------------------------
echo; echo "== 0) CRD applies — every x-kubernetes-validations rule compiles =="
if out="$($K apply --server-side --force-conflicts -f "$CRD" 2>&1)"; then
  pass "CRD applied"
else
  bad "CRD REJECTED — a CEL rule does not compile: $out"
  exit 1
fi

$K create namespace "$TEAM_NS" --dry-run=client -o yaml | $K apply -f - >/dev/null 2>&1

# --- the persisted parent ------------------------------------------------------------------------
# V-5 (duplicate) and V-6 (ceiling) are CROSS-OBJECT rules: they compare the candidate against an
# object that must really be in etcd. A --dry-run=server create persists nothing, so these two rules
# are the only ones in the file that need a real object to exist.
#
# scaleToZero keeps the operator from rendering a running pod for it. That is not tidiness: agent
# images are pulled from Artifact Registry by digest, a fixture agent left with replicas>0 sits in
# ImagePullBackOff, and three phases of L2 suites inherited exactly that pod as ambient residue and
# read it as scenery (LSN-026). A fixture that leaves nothing running cannot become someone else's
# confusing failure.
parent_yaml() { # $1 = extra spec lines (indented 2), e.g. the paused block
  cat <<EOF
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: Agent
metadata:
  name: $PARENT
  namespace: $NS
spec:
  tier: platform
  scope:
    projectId: $PROJECT
  deployment:
    scaleToZero: true
  harness:
    clusterName: kube-agents-dev
    location: us-central1
    hermes:
      dashboardEnabled: false
      apiServerSecretRef:
        name: platform-agent-secrets
        key: API_SERVER_KEY
${1:-}
EOF
}

cleanup() {
  $K delete agent "$PARENT" "wn-platform-parent-eq" "wn-cluster-admin-parent" -n "$NS" --ignore-not-found --wait=false >/dev/null 2>&1
  $K delete namespace "$TEAM_NS" --ignore-not-found --wait=false >/dev/null 2>&1
  rm -f "$FAILFILE"
}
# P12 ([[LSN-066]]): this trap is installed AFTER p10_assert_control_plane_healthy, whose
# p12_assert_exclusive_l2 took the one-suite-per-cluster lock and put `_l2_lock_exit_handler` on
# EXIT. Replacing that trap here would leak the lock to the next acquirer's stale break, so the
# release is chained in. It cannot change this script's exit status: bash runs the EXIT trap with
# the pending status and only an explicit `exit` inside the trap overrides it.
trap 'cleanup; l2_lock_release' EXIT INT TERM

# P3: delete any leftover from an earlier run rather than reusing it — a grandfathered parent may
# predate the rules under test.
$K delete agent "$PARENT" -n "$NS" --ignore-not-found >/dev/null 2>&1
if out="$(parent_yaml | $K apply -f - 2>&1)"; then
  pass "fixture parent $PARENT created (platform tier, project $PROJECT, scaled to zero)"
else
  bad "could not create the fixture parent, so V-5 and V-6 cannot be judged: $out"
  exit 1
fi

# --- the assertion helper -------------------------------------------------------------------------
# reject <rule> <field-path> <yaml-on-stdin>
# Asserts the API server REFUSES the object AND that the refusal names <field-path>.
reject() {
  local rule="$1" want="$2" out rc
  out="$($K apply --dry-run=server -f - 2>&1)"; rc=$?
  if [ $rc -eq 0 ]; then
    bad "$rule: ADMITTED — the rule did not fire at all"
    return
  fi
  if printf '%s' "$out" | grep -qF "$want"; then
    pass "$rule: rejected, naming $want"
  else
    bad "$rule: rejected, but the message does NOT name $want — it said: $out"
  fi
}

# admit <label> <yaml-on-stdin> — the positive control for a rule.
admit() {
  local label="$1" out rc
  out="$($K apply --dry-run=server -f - 2>&1)"; rc=$?
  if [ $rc -eq 0 ]; then
    pass "$label: admitted (positive control)"
  else
    bad "$label: REFUSED, but it is valid — the rule is over-blocking: $out"
  fi
}

# child_yaml_platform <name> <ns> <scope-lines> [extra-spec-lines] — a root (platform) Agent, which
# carries no parentRef. Used for the extra fixture parents and for the positive controls, which need
# scopes of their own so V-5 does not refuse them for colliding with $PARENT.
child_yaml_platform() {
  cat <<EOF
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: Agent
metadata:
  name: $1
  namespace: $2
spec:
  tier: platform
  scope:
$3
  deployment:
    scaleToZero: true
  harness:
    clusterName: kube-agents-dev
    location: us-central1
    hermes:
      dashboardEnabled: false
      apiServerSecretRef:
        name: platform-agent-secrets
        key: API_SERVER_KEY
${4:-}
EOF
}

# child_yaml <name> <tier> <ns> <parent> <scope-lines> [extra-spec-lines]
child_yaml() {
  cat <<EOF
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: Agent
metadata:
  name: $1
  namespace: $3
spec:
  tier: $2
  scope:
$5
  parentRef:
    name: $4
  deployment:
    scaleToZero: true
  harness:
    clusterName: kube-agents-dev
    location: us-central1
    hermes:
      dashboardEnabled: false
      apiServerSecretRef:
        name: platform-agent-secrets
        key: API_SERVER_KEY
${6:-}
EOF
}

# --- V-1: tier enum + immutability ----------------------------------------------------------------
echo; echo "== V-1) spec.tier is a closed enum and is immutable =="

child_yaml wn-v1-enum bogus-tier "$NS" "$PARENT" "    projectId: $PROJECT" \
  | reject "V-1 (enum)" "spec.tier"

# Immutability needs a persisted object to mutate: the parent. Flipping its tier to cluster-admin is
# refused by the CEL rule `self == oldSelf` and by the webhook's own update path.
parent_yaml | sed 's/^  tier: platform$/  tier: cluster-admin/' \
  | reject "V-1 (immutable)" "spec.tier"

# --- V-2: per-tier required scope fields -----------------------------------------------------------
echo; echo "== V-2) per-tier required scope fields are present =="

child_yaml wn-v2 cluster-admin "$NS" "$PARENT" "    projectId: $PROJECT" \
  | reject "V-2 (cluster-admin without scope.clusterName)" "spec.scope.clusterName"

child_yaml wn-v2b developer-team "$TEAM_NS" "$PARENT" \
  "    projectId: $PROJECT
    clusterName: cluster-1" \
  | reject "V-2 (developer-team without scope.namespace)" "spec.scope.namespace"

# --- V-3: parentRef required for non-platform tiers -------------------------------------------------
echo; echo "== V-3) parentRef.name is required for non-platform tiers =="

cat <<EOF | reject "V-3 (cluster-admin with no parentRef)" "spec.parentRef.name"
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: Agent
metadata:
  name: wn-v3
  namespace: $NS
spec:
  tier: cluster-admin
  scope:
    projectId: $PROJECT
    clusterName: cluster-1
  deployment:
    scaleToZero: true
  harness:
    clusterName: kube-agents-dev
    location: us-central1
    hermes:
      dashboardEnabled: false
      apiServerSecretRef:
        name: platform-agent-secrets
        key: API_SERVER_KEY
EOF

# --- V-4: developer-team placement ------------------------------------------------------------------
echo; echo "== V-4) a developer-team Agent lives in the namespace it scopes =="

# metadata.namespace = kubeagents-system, scope.namespace = wn-team-x. Without this clause the pod
# would be rendered OUTSIDE the tenant's isolation controls (03 §3, §11).
#
# THE PARENT HERE IS A CLUSTER-ADMIN, AND THAT IS THE POINT. This arm read
# `child_yaml wn-v4 developer-team "$NS" "$PARENT"` until 2026-07-31 — a developer-team child
# parented by the PLATFORM agent. That fixture is the same shape as V-6(b) forty lines down, which
# exists precisely to prove that a developer-team child skipping a level is refused. So it violated
# two rules, and its refusal was two rules deep: exactly the [[LSN-035]] shape this file's own header
# warns about, in the file that warns about it. The rejection stayed attributable only because the
# webhook aggregates and the message happened to keep naming `metadata.namespace`.
#
# `cluster-admin` is the tier immediately above `developer-team`, so with $CA_PARENT the ONLY rule
# this object violates is placement. The admitted control below is the other half: the same child
# with `metadata.namespace` corrected must be accepted, which is what proves the placement clause
# caused the refusal rather than something else about a fixture nobody re-derived.
# It also needs a cluster of its own. `cluster-1` is the cluster four other arms below scope their
# cluster-admin fixtures to, and (tier, scope) is unique — V-5's rule, asserted twenty lines up —
# so a persisted cluster-admin at (project, cluster-1) makes every one of them collide and be
# refused for cardinality instead of for the clause it names. Two of those arms are negatives, which
# would have gone green on the wrong rejection; one is a positive control, which goes red. That is
# the same LSN-035 hazard as the parent tier, reached through the fixture's scope rather than its
# shape, and it is why this parent lives at `cluster-v4` alone.
CA_PARENT=wn-cluster-admin-parent
CA_CLUSTER=cluster-v4
$K delete agent "$CA_PARENT" -n "$NS" --ignore-not-found >/dev/null 2>&1
if out="$(child_yaml "$CA_PARENT" cluster-admin "$NS" "$PARENT" \
  "    projectId: $PROJECT
    clusterName: $CA_CLUSTER" | $K apply -f - 2>&1)"; then
  child_yaml wn-v4 developer-team "$NS" "$CA_PARENT" \
    "    projectId: $PROJECT
    clusterName: $CA_CLUSTER
    namespace: $TEAM_NS" \
    | reject "V-4 (placement)" "metadata.namespace"
  child_yaml wn-v4-ok developer-team "$TEAM_NS" "$CA_PARENT" \
    "    projectId: $PROJECT
    clusterName: $CA_CLUSTER
    namespace: $TEAM_NS" \
    | admit "V-4 (same child, placed in the namespace it scopes)"
else
  bad "V-4: could not create the cluster-admin fixture parent, so the placement rule cannot be"
  bad "  attributed to itself: $out"
fi

# --- V-5: (tier, scope) cardinality -------------------------------------------------------------------
echo; echo "== V-5) exactly one non-terminating Agent per (tier, scope) =="

# A SECOND platform agent for the same project, under a different name. The parent above is the
# incumbent, which is why it had to be persisted.
cat <<EOF | reject "V-5 (duplicate (platform, $PROJECT))" "spec.scope"
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: Agent
metadata:
  name: wn-v5-duplicate
  namespace: $NS
spec:
  tier: platform
  scope:
    projectId: $PROJECT
  deployment:
    scaleToZero: true
  harness:
    clusterName: kube-agents-dev
    location: us-central1
    hermes:
      dashboardEnabled: false
      apiServerSecretRef:
        name: platform-agent-secrets
        key: API_SERVER_KEY
EOF

# The positive control, and the reason V-5 needs one more than most arms here. "Exactly one
# non-terminating Agent per (tier, scope)" fails in two directions, and only one of them is visible
# from the negative: a rule that refused EVERY second platform Agent, regardless of scope, would make
# the arm above green and the product unusable. This is a platform Agent for a DIFFERENT project,
# submitted while $PARENT is standing — the cardinality key is (tier, scope), so it must be admitted.
child_yaml_platform wn-v5-other-scope "$NS" "    projectId: $PROJECT-other" \
  | admit "V-5 (second platform Agent, different scope)"

# --- V-6: the cross-object ceiling ----------------------------------------------------------------------
echo; echo "== V-6) child scope ⊂ parent scope, and the parent tier is the one immediately above =="

# (a) parent does not exist — the ceiling is UNVERIFIABLE, which is a rejection, not a pass.
child_yaml wn-v6-dangling cluster-admin "$NS" no-such-parent \
  "    projectId: $PROJECT
    clusterName: cluster-1" \
  | reject "V-6 (dangling parentRef)" "spec.parentRef.name"

# (b) wrong parent tier — a developer-team child parented by the PLATFORM agent skips a level, and
# would be bounded by the whole project instead of by one cluster.
child_yaml wn-v6-tier developer-team "$TEAM_NS" "$PARENT" \
  "    projectId: $PROJECT
    clusterName: cluster-1
    namespace: $TEAM_NS" \
  | reject "V-6 (parent is the wrong tier)" "spec.parentRef.name"

# (c) outside the parent's project.
child_yaml wn-v6-project cluster-admin "$NS" "$PARENT" \
  "    projectId: some-other-project
    clusterName: cluster-1" \
  | reject "V-6 (child outside the parent's project)" "spec.parentRef.name"

# (d) not a STRICT subset — same scope as the parent is an authority clone, not an attenuation.
#
# This arm needs its own parent, and the reason is worth stating. A cluster-admin child MUST carry
# scope.clusterName (V-2), so it can only have a scope identical to a platform parent that also
# carries one — which is legal, since V-2 requires only projectId of the platform tier. Reusing
# $PARENT here produces a child with no clusterName, and V-2 rejects it first: the arm goes green
# while never reaching the clause it names. That is the failure mode this whole file exists to
# prevent, and the first draft of this line had it.
EQ_PARENT=wn-platform-parent-eq
EQ_PROJECT=$PROJECT-eq
$K delete agent "$EQ_PARENT" -n "$NS" --ignore-not-found >/dev/null 2>&1
if out="$(child_yaml_platform "$EQ_PARENT" "$NS" \
  "    projectId: $EQ_PROJECT
    clusterName: cluster-1" | $K apply -f - 2>&1)"; then
  child_yaml wn-v6-equal cluster-admin "$NS" "$EQ_PARENT" \
    "    projectId: $EQ_PROJECT
    clusterName: cluster-1" \
    | reject "V-6 (scope identical to the parent's)" "spec.parentRef.name"
else
  bad "V-6 (equal scope): could not create the second fixture parent: $out"
fi

# (e) the brake covers provisioning (03 §6): pause the parent, then try to create a child.
if out="$(parent_yaml "  operations:
    paused: true
    pauseReason: webhook-negatives-l2 probe" | $K apply -f - 2>&1)"; then
  child_yaml wn-v6-paused cluster-admin "$NS" "$PARENT" \
    "    projectId: $PROJECT
    clusterName: cluster-1" \
    | reject "V-6 (paused parent may not provision)" "spec.parentRef.name"
  # Restore, and use the restoration as the positive control: the SAME child that was just refused
  # must be admitted once the brake is off. That is what proves the pause caused the refusal rather
  # than something else about the fixture.
  parent_yaml "  operations:
    paused: false" | $K apply -f - >/dev/null 2>&1
  child_yaml wn-v6-paused cluster-admin "$NS" "$PARENT" \
    "    projectId: $PROJECT
    clusterName: cluster-1" \
    | admit "V-6 (same child, parent unpaused)"
else
  bad "V-6 (paused parent): could not pause the fixture parent: $out"
fi

# --- V-7: the closed allowlist ------------------------------------------------------------------------
echo; echo "== V-7) an enabled chat integration carries a non-blank allowlist =="

# The exhaustive shape matrix (absent / [] / [\"\"] / whitespace, both platforms) is
# dev/verify/closed-allowlist-l2.sh. This arm is V-CTR-002's roll-call entry for the rule, so that
# every one of V-1…V-10 is represented in ONE place and a missing rule is visible as a missing line.
child_yaml wn-v7 platform "$NS" "$PARENT" "    projectId: $PROJECT" \
  "  integration:
    googleChat:
      enabled: true
      projectId: $PROJECT
      topicName: wn-v7-events
      subscriptionName: wn-v7-events-sub
      allowedUsers:
        - \"   \"" \
  | reject "V-7 (all-blank allowlist)" "allowedUsers"

# --- V-8: the budget clamp ------------------------------------------------------------------------------
echo; echo "== V-8) no initiativeBudget leaf above its code ceiling; flapWindow not below its floor =="

# One above the 06 §1.1 ceiling for each of the two most security-relevant leaves, plus the floor.
# The exhaustive ten-leaf sweep with both sides of every boundary is the L1 table in
# internal/webhook/agent_ceiling_test.go; what only a live API server can add is that the webhook is
# actually reached, which one leaf demonstrates as well as ten.
child_yaml wn-v8a platform "$NS" "$PARENT" "    projectId: $PROJECT" \
  "  operations:
    initiativeBudget:
      selfInitiated:
        elevatedPerHour: 11" \
  | reject "V-8 (selfInitiated.elevatedPerHour = 11, ceiling 10)" "spec.operations.initiativeBudget.selfInitiated.elevatedPerHour"

child_yaml wn-v8b platform "$NS" "$PARENT" "    projectId: $PROJECT" \
  "  operations:
    initiativeBudget:
      maxObjectsPerAction: 51" \
  | reject "V-8 (maxObjectsPerAction = 51, ceiling 50)" "spec.operations.initiativeBudget.maxObjectsPerAction"

child_yaml wn-v8c platform "$NS" "$PARENT" "    projectId: $PROJECT" \
  "  operations:
    initiativeBudget:
      flapWindow: 1m" \
  | reject "V-8 (flapWindow = 1m, floor 5m)" "spec.operations.initiativeBudget.flapWindow"

# Boundary positive control: AT the ceiling is admitted. Without this, a webhook that rejected every
# budget would score a perfect three-for-three above.
#
# Its own project, because V-5 refuses a second platform Agent in $PROJECT and would refuse this one
# for a reason that has nothing to do with V-8. The negative arms above do not need this — V-8 runs
# at step 2c and V-5 at step 3a, so they are refused by V-8 before cardinality is ever consulted —
# and that asymmetry is exactly how a positive control earns its place: it fails where the negatives
# cannot.
child_yaml_platform wn-v8d "$NS" "    projectId: $PROJECT-v8d" \
  "  operations:
    initiativeBudget:
      selfInitiated:
        elevatedPerHour: 10
      maxObjectsPerAction: 50
      flapWindow: 5m" \
  | admit "V-8 (every leaf exactly AT its ceiling/floor)"

# --- V-9: the schema is closed --------------------------------------------------------------------------
echo; echo "== V-9) unknown fields under spec are pruned — no authority field can be smuggled in =="

# V-9 has TWO outcomes, and the difference between them is the whole property.
#
# `kubectl apply` sends --field-validation=Strict by default, so the API server REFUSES an unknown
# field and names it. That is the arm an operator sees, and it is worth asserting on its own.
#
# But strict validation is the CLIENT's request, and a client can decline it. The security property
# is not "kubectl complains" — it is that an authority field cannot enter the stored object even
# when the submitter explicitly asks the server not to complain about it. So the second arm sends
# `--validate=ignore` (`kubectl apply`'s spelling of --field-validation=Ignore, which apply does not
# accept), which is what an attacker with a raw API client would do, and asserts the field is
# PRUNED: admitted, and gone. 06 §1.2 V-9 names that outcome "field pruned", not `Invalid`, and
# asserting a rejection there would be asserting the wrong mechanism.
v9_yaml() {
  child_yaml_platform wn-v9 "$NS" "    projectId: $PROJECT-v9" \
    "  rbac:
    rules:
      - apiGroups: [\"*\"]
        resources: [\"*\"]
        verbs: [\"*\"]"
}

v9_yaml | reject "V-9 (strict: the default client path)" "spec.rbac"

v9_out="$(v9_yaml | $K apply --dry-run=server --validate=ignore -o json -f - 2>&1)"
v9_rc=$?
if [ $v9_rc -ne 0 ]; then
  bad "V-9 (pruning): refused even with --validate=ignore; expected ADMITTED-and-pruned: $v9_out"
elif printf '%s' "$v9_out" | grep -q '"rbac"'; then
  bad "V-9 (pruning): spec.rbac SURVIVED into the returned object — the schema is not closed, and a"
  bad "  client that declines strict validation can carry an authority field into etcd"
else
  pass "V-9 (pruning): with strict validation declined, spec.rbac was pruned — the wildcard grant did not survive"
fi

# --- V-10: the reader-only ServiceAccount override -----------------------------------------------------------
echo; echo "== V-10) spec.security.serviceAccountName may name only the tier's reader SA =="

# The actor SA is the identity that holds the scoped WRITE authority. Being able to name it is being
# able to choose it (03 §3.3/§3.4).
child_yaml wn-v10a platform "$NS" "$PARENT" "    projectId: $PROJECT" \
  "  security:
    serviceAccountName: platform-$PROJECT-actor" \
  | reject "V-10 (an actor SA)" "spec.security.serviceAccountName"

child_yaml wn-v10b platform "$NS" "$PARENT" "    projectId: $PROJECT" \
  "  security:
    serviceAccountName: default" \
  | reject "V-10 (an arbitrary SA)" "spec.security.serviceAccountName"

child_yaml wn-v10c platform "$NS" "$PARENT" "    projectId: $PROJECT" \
  "  security:
    serviceAccountName: cluster-admin-agent" \
  | reject "V-10 (another tier's reader SA)" "spec.security.serviceAccountName"

# --- positive controls -------------------------------------------------------------------------------------
echo; echo "== 12) positive controls — a valid Agent at each tier is ADMITTED =="

# Every arm above is a rejection. A webhook that refused EVERYTHING, or an API server too wedged to
# admit anything, would score a perfect run without these three lines. This is the anti-false-green
# clause of 09 §11 made concrete for a suite that is otherwise all negatives.
child_yaml wn-ok-ca cluster-admin "$NS" "$PARENT" \
  "    projectId: $PROJECT
    clusterName: cluster-1" \
  | admit "a properly-attenuated cluster-admin child"

child_yaml_platform wn-ok-sa "$NS" "    projectId: $PROJECT-oksa" \
  "  security:
    serviceAccountName: platform-agent" \
  | admit "the platform tier's own reader SA"

# A developer-team agent needs a cluster-admin parent, which does not exist here; the cluster-admin
# arm above already proves the child path end to end, so this control uses the tier that can be
# built without one.
child_yaml_platform wn-ok-budget "$NS" "    projectId: $PROJECT-okbudget" \
  "  operations:
    paused: false
    initiativeBudget:
      selfInitiated:
        routinePerHour: 30
        elevatedPerHour: 6
      humanRequested:
        routinePerHour: 120
      maxObjectsPerAction: 25
      flapWindow: 30m
      flapThreshold: 3" \
  | admit "the 06 §1.1 DEFAULT budget (every leaf below its ceiling)"

echo
echo "===================================================================="
fail="$(failures)"
if [ "$fail" -eq 0 ]; then
  echo " V-CTR-002: PASS — V-1…V-10 all negatively tested, every rejection named its field path"
  echo " V-CTN-015: PASS — a duplicate (platform, $PROJECT) Agent is rejected naming spec.scope,"
  echo "   and a second platform Agent in a DIFFERENT scope is still admitted"
  echo " V-CTN-016: PASS — a developer-team Agent whose metadata.namespace is not its"
  echo "   spec.scope.namespace is rejected naming metadata.namespace, under a cluster-admin parent"
  echo "   so placement is the only rule it violates; the corrected child is admitted"
else
  echo " V-CTR-002 · V-CTN-015 · V-CTN-016: FAIL — $fail failed assertion(s), see the FAIL lines above"
fi
echo "===================================================================="
[ "$fail" -eq 0 ] || exit 1
exit 0
