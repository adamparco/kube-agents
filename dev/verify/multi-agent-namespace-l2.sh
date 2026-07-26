#!/usr/bin/env bash
# Two agents in ONE namespace — LSN-015 (Phase 8, P8-T4). V-CMP-001 / V-CMP-004.
#
# LSN-015: "A single-instance fixture cannot see a multi-instance conflict." Every L2 fixture before
# this one created ONE agent per namespace, so none of them could observe what happens when a second
# arrives — and something did: `system-metadata` was a fixed, namespace-scoped PVC name with
# ReadWriteOnce, so the second agent in a namespace wedged in ContainerCreating on a multi-attach
# error. kubeagents-system is EXACTLY that topology by design (the cluster-admin tier sits beside the
# platform tier), so the defect was not exotic, it was the reference install.
#
# The generalized form is the reason this script exists rather than a unit test: where the design says
# N, the fixture must be N. Cardinality is part of the property, not a test-setup detail.
#
# TWO CLAIMS, AT TWO DIFFERENT COSTS — kept separate on purpose.
#
#   CLAIM 1 (section 2): the claims are PER-AGENT. Two Agent CRs in one namespace must produce two
#   distinct `<name>-system-metadata` PVCs, and no claim may be referenced by both Deployments. This
#   is a controller-output property: it is decided at reconcile time and is fully observable whether
#   or not a single pod ever schedules. It runs here, and it fails if the naming regresses.
#
#   CLAIM 2 (section 3): the SYMPTOM is gone — both pods actually reach Ready side by side. This one
#   cannot be faked into existence on any cluster. ReadWriteOnce excludes per NODE, not per pod: two
#   pods on the SAME node mount an RWO claim quite happily, so a single-node cluster cannot exhibit a
#   multi-attach at all, and "both pods came up" there is not evidence of anything. That is LSN-015
#   applied to itself one level up — the fixture needs N=2 nodes for the same reason it needs N=2
#   agents. It also needs enough memory for two agent pods (~2.7Gi each as rendered), an agent image
#   this commit's build actually put in the registry, and the ServiceAccount + API-key Secret the
#   CRs reference but nothing creates.
#   `dev/cluster/up.sh` produces exactly that cluster; section 3 checks each condition and
#   defers on the one that is missing rather than failing the claim for an environmental reason.
#
# So section 3 DEFERS, loudly and with the measured numbers, unless the cluster can actually host the
# conflict. A deferral naming an external blocker is honest; a pass on a cluster that is physically
# incapable of showing the failure is not (09 §6, V-MET-014).
#
# DESTRUCTIVE-TEST GUARD: scratch-GKE contexts only, anchored. This creates a namespace and
# two Agent CRs, so the guard is load-bearing.
# Exit: 0 = PROVEN · 1 = FAILED · 2 = refused target · 3 = DEFERRED.
# Usage: dev/verify/multi-agent-namespace-l2.sh [kube-context]
#
# PRECONDITIONS (binding.md §Preconditions; linted by invariants-gate.py
# check_l2_scripts_declare_preconditions). Declared, not assumed: LSN-001 and LSN-002 each
# recurred against scripts whose authors believed the preconditions held.
#   P1 image-under-test:  kubeagents-system/control-plane=controller-manager — CLAIM 1 is a controller-OUTPUT property
#      (per-agent PVC naming), decided by the running operator at reconcile time. A stale operator
#      reproduces the old bare-namespace claim and the check reads it as a regression, or worse,
#      the fixed operator is absent and the run is green about code that is not there.
#   P3 admission-recreate: both Agent CRs and the namespace. Section 1 creates them fresh in a per-run namespace, so the
#      Deployments and PVCs are produced by the operator under the rules currently in force.
#   P6 runtime-authoritative: the live PersistentVolumeClaims and Deployments the operator produced. Not agent_manifests.go's
#      naming function — that is asserted separately at L1, and the two are the point of the pair.
set -uo pipefail

CTX="${1:-gke-scratch-kube-agents-dev}"
K="kubectl --context $CTX"
NS="multi-agent-l2"
A1="alpha-agent"
A2="beta-agent"

# The agent image these CRs run. This USED to be hard-coded to ghcr.io/gke-labs/...:v0.1.0, which no
# inner-loop cluster has ever had — so even on a cluster that could host CLAIM 2, both pods would
# have gone ImagePullBackOff and the claim would have failed for a reason unrelated to RWO. It now
# names the tag `reload-images.sh agents` pushes for THIS commit, and section 3 resolves that tag to
# a digest in Artifact Registry before asserting anything (P8 also wants zero ghcr.io/gke-labs
# containers). The dirty-tree variant carries an epoch that cannot be re-derived here, so a dirty
# tree resolves the clean-commit tag; that is visible in the deferral message when it is absent.
PROJECT_ID="${PROJECT_ID:-$(gcloud config get core/project 2>/dev/null)}"
REGION="${REGION:-us-east4}"
AR_REPO="${AR_REPO:-kube-agents}"
AGENT_IMAGE_REPO="${AGENT_IMAGE_REPO:-$REGION-docker.pkg.dev/$PROJECT_ID/$AR_REPO}"
AGENT_IMAGE_TAG="${AGENT_IMAGE_TAG:-dev-$(git -C "$(dirname "$0")/../.." rev-parse --short HEAD 2>/dev/null || echo unknown)}"

case "$CTX" in
  gke-scratch-*) : ;;
  *)
    echo "REFUSING: context '$CTX' is not a scratch cluster (destructive-test guard)." >&2
    exit 2
    ;;
esac

fail=0
pass() { echo "PASS: $1"; }
bad() {
  echo "FAIL: $1"
  fail=1
}
cleanup() { $K delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true; }
trap cleanup EXIT

# --- 0) the cluster and the operator must be there ----------------------------------------------
echo "== 0) preconditions =="
if ! $K version >/dev/null 2>&1; then
  echo "DEFERRED: context '$CTX' is not reachable."
  exit 3
fi
# P10 (LSN-026), before any claim: can this cluster still RUN the experiment? Rationale and the
# three false failures that bought it are at the definition site. rc 2 = could-not-run, never 1.
. "$(dirname "$0")/../lib/preconditions.sh"
p10_assert_control_plane_healthy "$K" "$CTX" || exit 2
if ! $K get crd agents.kubeagents.x-k8s.io >/dev/null 2>&1; then
  echo "DEFERRED: the Agent CRD is not installed on '$CTX' — nothing would reconcile these CRs."
  echo "  Stand it up: dev/cluster/up.sh"
  exit 3
fi
if ! $K -n kubeagents-system get deploy kubeagents-controller-manager >/dev/null 2>&1; then
  echo "DEFERRED: no controller-manager on '$CTX'; the CRs would sit unreconciled and every"
  echo "  assertion below would fail for a reason that has nothing to do with PVC naming."
  exit 3
fi

# P1: the running operator must BE the build under test. This used to read the imageID, print it,
# and follow it with a note reminding the reader to rebuild -- which is addressed to whoever already
# knows, and is why LSN-001 recurred three times. p1_assert_build_under_test compares the digest and
# returns three states, so "could not look" is a deferral and "does not match" is a failure.
p1_assert_build_under_test "$K" kubeagents-system control-plane=controller-manager
case "$?" in
  0) pass "P1: the running operator is the build under test" ;;
  3) echo "DEFERRED: P1 unverifiable (see above); every claim below would be about unknown code."
     exit 3 ;;
  *) bad "P1: the cluster is not running the build under test"; exit 1 ;;
esac

# --- 1) two agents, ONE namespace ---------------------------------------------------------------
echo "== 1) creating two Agent CRs in namespace $NS =="
$K create namespace "$NS" --dry-run=client -o yaml | $K apply -f - >/dev/null 2>&1 || true

# Two DIFFERENT tiers: the admission webhook enforces cardinality on the (tier, scope) key, so two
# agents of the same tier in one namespace is a rejection, not a conflict. The topology that actually
# ships — and the one that broke — is two tiers sharing a namespace.
create_agent() { # create_agent <name> <tier> <extra-scope-yaml> <extra-spec-yaml>
  local name="$1" tier="$2" scope_extra="$3" spec_extra="$4"
  # A tier-appropriate spec: the developer-team tier carries the A1 placement clause
  # (metadata.namespace == spec.scope.namespace); the cluster-admin tier is cluster-scoped, must NOT
  # set it, and requires a parentRef. Both reference a ServiceAccount and a Secret that do not exist
  # yet, and CLAIM 1 runs anyway — deliberately, and it is worth being precise about why. The PVC
  # names are reconciled from the CR, so the naming property is decided before any pod is scheduled;
  # CLAIM 1 is therefore strictly cheaper than CLAIM 2 and must not acquire CLAIM 2's dependencies.
  # Section 3 seeds both fixtures (lib/agent-fixtures.sh) at the point it actually needs pods.
  cat <<YAML | $K apply -f - >/dev/null || bad "could not create Agent $name"
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: Agent
metadata:
  name: $name
  namespace: $NS
spec:
  tier: $tier
  scope:
    projectId: multi-agent-l2-project
    clusterName: cluster-a
${scope_extra}
  harness:
    clusterName: cluster-a
    location: us-central1
    hermes:
      agentHome: /opt/data
      dashboardEnabled: true
      apiServerSecretRef:
        name: ${name}-secrets
        key: API_SERVER_KEY
  deployment:
    image: ${AGENT_IMAGE_REPO}/${tier}-agent
    tag: ${AGENT_IMAGE_TAG}
    imagePullPolicy: IfNotPresent
  security:
    serviceAccountName: ${name}-sa
${spec_extra}
YAML
}

create_agent "$A1" cluster-admin "" "  parentRef:
    name: platform-agent"
create_agent "$A2" developer-team "    namespace: $NS" "  parentRef:
    name: $A1"

# Reconcile is asynchronous; poll for the claims rather than sleeping a guessed interval.
for _ in $(seq 1 30); do
  n="$($K -n "$NS" get pvc --no-headers 2>/dev/null | grep -c 'system-metadata')"
  [ "$n" -ge 2 ] && break
  sleep 2
done

# --- 2) CLAIM 1: the claims are per-agent -------------------------------------------------------
echo "== 2) CLAIM 1 — system-metadata is per-agent, not per-namespace =="
CLAIMS="$($K -n "$NS" get pvc -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null | grep 'system-metadata' | sort)"
echo "  claims found: $(echo "$CLAIMS" | tr '\n' ' ')"

for name in "$A1" "$A2"; do
  if echo "$CLAIMS" | grep -qx "${name}-system-metadata"; then
    pass "$name has its own claim ${name}-system-metadata"
  else
    bad "$name has no per-agent system-metadata claim — the namespace-scoped name is back"
  fi
done

# Both remaining checks in this section are ABSENCE checks, and absence is trivially true on an
# empty namespace. The first run of this script proved that the hard way: the CRs were rejected for
# a missing required field, zero PVCs were created, and "no bare claim" plus "no shared claim" both
# reported PASS against nothing at all. Gate them on the claims actually existing, so a reconcile
# that never happened can never read as a property that holds.
CLAIM_COUNT="$(echo "$CLAIMS" | grep -c 'system-metadata')"
if [ "$CLAIM_COUNT" -lt 2 ]; then
  bad "only $CLAIM_COUNT system-metadata claim(s) exist — the absence checks below would be vacuous, so they are not being run"
else
  # The bare namespace-scoped name is the exact defect; its presence means the old constructor is live.
  if echo "$CLAIMS" | grep -qx "system-metadata"; then
    bad "a bare namespace-scoped 'system-metadata' claim exists — LSN-015's defect has regressed"
  else
    pass "no bare namespace-scoped 'system-metadata' claim"
  fi
fi

# And no claim may be shared between the two Deployments, which is the property that actually
# prevents the multi-attach — distinct NAMES would not help if both pods still mounted one of them.
$K -n "$NS" get deploy -o json >/tmp/ma-deploys.json 2>/dev/null
SHARED="$(python3 - <<'PY'
import json, collections
try:
    d = json.load(open("/tmp/ma-deploys.json"))
except Exception:
    raise SystemExit("")
owners = collections.defaultdict(set)
for i in d.get("items", []):
    for v in i["spec"]["template"]["spec"].get("volumes", []):
        pvc = v.get("persistentVolumeClaim")
        if pvc:
            owners[pvc["claimName"]].add(i["metadata"]["name"])
print(" ".join(c for c, o in owners.items() if len(o) > 1))
PY
)"
DEPLOY_COUNT="$($K -n "$NS" get deploy --no-headers 2>/dev/null | wc -l | tr -d ' ')"
if [ -n "$SHARED" ]; then
  bad "these claims are mounted by BOTH agent Deployments: $SHARED"
elif [ "${DEPLOY_COUNT:-0}" -lt 2 ]; then
  bad "only $DEPLOY_COUNT Deployment(s) reconciled — 'no shared PVC' would be vacuous, so it is not being claimed"
else
  pass "no PVC is referenced by both agents' Deployments"
fi

# --- 3) CLAIM 2: the symptom, if the cluster can host it ----------------------------------------
echo "== 3) CLAIM 2 — both pods run side by side =="
NODES="$($K get nodes --no-headers 2>/dev/null | grep -cv 'SchedulingDisabled')"
ALLOC_KI="$($K get nodes -o jsonpath='{.items[0].status.allocatable.memory}' 2>/dev/null | tr -d 'Ki')"
ALLOC_GI=$(((${ALLOC_KI:-0}) / 1048576))
NEED_GI=6 # ~2.7Gi per agent pod as rendered, times two, plus the control plane.

if [ "$NODES" -lt 2 ]; then
  echo "DEFERRED: $NODES schedulable node(s). ReadWriteOnce excludes per NODE, so two pods on one"
  echo "  node share an RWO claim without complaint — this cluster cannot exhibit the multi-attach"
  echo "  it would need to exhibit for 'both pods came up' to mean anything. CLAIM 1 above still"
  echo "  stands on its own; CLAIM 2 is not evidence here and is NOT being recorded as a pass."
  echo "  Unblock: dev/cluster/up.sh — it builds a 2-node cluster (control-plane + worker) with"
  echo "  the full stack. A 1-node cluster here means the node pool was resized down; bring it"
  echo "  back with dev/cluster/resume.sh, or re-run up.sh."
  [ "$fail" -eq 0 ] && exit 3 || exit 1
fi
if [ "$ALLOC_GI" -lt "$NEED_GI" ]; then
  echo "DEFERRED: ${ALLOC_GI}Gi allocatable, need ~${NEED_GI}Gi. An agent pod requests ~2.7Gi"
  echo "  (agent 2Gi + dashboard 512Mi + fluent-bit 128Mi + event-watcher 64Mi) and two will not fit,"
  echo "  so a Pending pod here would mean 'the node is small', not 'the claim multi-attached'."
  echo "  Unblock: the node pool is too small for this claim. Re-create with a larger machine"
  echo "  type (dev/cluster/up.sh sets e2-standard-4), then re-run."
  [ "$fail" -eq 0 ] && exit 3 || exit 1
fi

# The image must be PULLABLE, and this is checked rather than hoped for. This cluster exists so the
# two pods land on DIFFERENT nodes; an image that does not resolve converts the very outcome we want
# into an ImagePullBackOff, which would then be recorded as "the agents cannot coexist" — a false
# failure with the right shape to be believed.
#
# WHAT REPLACED `docker exec <node> crictl images`. The old form asked each node's image store
# whether a side-loaded tag was present, which was the right question for `kind load` and has no
# analogue on a managed cluster — there is no docker socket to the nodes, and the kubelet pulls
# rather than being handed a copy. The question is now asked of the registry the kubelet pulls
# FROM, and answered with a digest rather than a tag. That is strictly stronger than the grep it
# replaces: a tag present in a node's store says nothing about WHICH build it is, and same-tag
# staleness is LSN-001 exactly. Presence on the node that actually ran the pod is then asserted
# after the fact, from the pod's own resolved imageID, which is the only authoritative answer.
image_missing=""
for tier in cluster-admin developer-team; do
  uri="$AGENT_IMAGE_REPO/${tier}-agent:$AGENT_IMAGE_TAG"
  digest="$(gcloud artifacts docker images describe "$uri" --project "$PROJECT_ID" \
    --format='value(image_summary.digest)' 2>/dev/null)"
  if [ -z "$digest" ]; then
    image_missing="$image_missing\n    $uri does not resolve in Artifact Registry"
  else
    echo "  $tier-agent -> ${digest:0:19}..."
  fi
done
if [ -n "$image_missing" ]; then
  echo "DEFERRED: the agent image for this commit is not in the registry the cluster pulls from, so"
  echo "  a cross-node placement — the exact result this cluster exists to produce — would fail to"
  echo "  pull and be indistinguishable from the RWO conflict this claim is looking for:"
  printf '%b\n' "$image_missing"
  echo "  Unblock: dev/cluster/reload-images.sh agents $CTX"
  [ "$fail" -eq 0 ] && exit 3 || exit 1
fi

# Only now: the pods need a ServiceAccount and an API-key Secret that nothing else creates. Seeded
# HERE, after CLAIM 1, so CLAIM 1 keeps holding without them (see create_agent) and this stays the
# only section that needs a startable pod.
echo "  seeding the fixtures the pods need to start:"
. "$(dirname "$0")/../lib/agent-fixtures.sh"
seed_agent_fixtures "$K" "$NS" "$A1"
seed_agent_fixtures "$K" "$NS" "$A2"

for name in "$A1" "$A2"; do
  if $K -n "$NS" wait --for=condition=Available "deployment/${name}-gateway" --timeout=240s >/dev/null 2>&1; then
    pass "$name's Deployment became Available alongside the other agent"
  else
    # Report what the CONTAINER is stuck on, not only PodScheduled. A multi-attach shows up as an
    # unschedulable/attach message, but a missing fixture or image shows up in the container's
    # waiting reason and the old message rendered it as "no message" — a failure with no cause
    # attached is the one most likely to be blamed on whatever this script is nominally testing.
    sched="$($K -n "$NS" get pods -l "kube-agents/agent=$name" -o jsonpath='{.items[0].status.conditions[?(@.type=="PodScheduled")].message}' 2>/dev/null)"
    waiting="$($K -n "$NS" get pods -l "kube-agents/agent=$name" -o jsonpath='{range .items[0].status.containerStatuses[*]}{.name}={.state.waiting.reason}:{.state.waiting.message}{" "}{end}' 2>/dev/null)"
    bad "$name never became Available with a second agent in the namespace: ${sched:-no scheduling message} | containers: ${waiting:-none reported}"
  fi
done

# The other half of what the crictl grep used to claim, asked where the answer is authoritative:
# the image is on the node that ran the pod. `.status.containerStatuses[].imageID` is written by the
# kubelet AFTER it has the layers, so a resolved digest here is proof of local presence in a way the
# node's image list never was — that list can hold a tag pointing at a different build. An empty
# imageID on a Running pod means the runtime reported no manifest digest, which is worth saying out
# loud rather than passing over: every downstream claim about "the build under test" rests on it.
# `platform-agent` is the container name for every tier, not just the platform one — the operator
# hard-codes it in agent_manifests.go. Named explicitly rather than taken as `[0]`, because the pod
# also carries a dashboard, fluent-bit and event-watcher, and an index would silently start
# describing whichever of those the renderer happens to emit first.
for name in "$A1" "$A2"; do
  iid="$($K -n "$NS" get pods -l "kube-agents/agent=$name" \
    -o jsonpath='{.items[0].status.containerStatuses[?(@.name=="platform-agent")].imageID}' 2>/dev/null)"
  case "$iid" in
    *@sha256:*) pass "$name's agent container resolved to a digest on its node (${iid##*@})" ;;
    "") bad "$name's agent container reports no imageID — the node that ran it cannot be shown to hold the image under test" ;;
    *) bad "$name's agent container resolved to '$iid', which names no digest (LSN-001: a tag is not a build)" ;;
  esac
done

# Same node => the RWO conflict was never actually put to the test, even with two nodes present.
N1="$($K -n "$NS" get pods -l "kube-agents/agent=$A1" -o jsonpath='{.items[0].spec.nodeName}' 2>/dev/null)"
N2="$($K -n "$NS" get pods -l "kube-agents/agent=$A2" -o jsonpath='{.items[0].spec.nodeName}' 2>/dev/null)"
if [ -n "$N1" ] && [ "$N1" = "$N2" ]; then
  echo "NOTE: both pods landed on '$N1'. Co-located pods share an RWO claim without error, so this"
  echo "  run did not exercise the cross-node case even though the cluster could have."
else
  pass "pods landed on different nodes ($N1 / $N2) — the cross-node RWO case was exercised"
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "RESULT: PROVEN — two agents coexist in one namespace."
  exit 0
fi
echo "RESULT: FAILED"
exit 1
