#!/usr/bin/env bash
# up.sh — bring up THE inner-loop cluster, whole. One cluster, two nodes, an enforcing dataplane,
# the operator under test, the VAP and the agent images. Exit 0 means every line of
# `dev/L2-CHAIN.txt` can run against it; there is no second target to remember.
#
# WHY THE CLUSTER IS REMOTE
#   This ran on Kind inside Colima on a laptop until 2026-07-26, and the host — not the work — was
#   the binding constraint. Two lessons in two days were pure artifacts of it: LSN-026 (2 GiB of VM
#   memory crash-looped kube-scheduler and kube-controller-manager, and three unrelated SECURITY
#   properties reported FAIL when nothing was wrong with any of them) and LSN-027 (the default
#   `fs.inotify.max_user_instances` of 128, exhausted by two control planes, killed a cluster at
#   kubeadm `wait-control-plane` with an error whose whole vocabulary is slow-and-small). Neither
#   failure mode exists on a managed control plane.
#
#   The move also closes a CORRECTNESS gap, which is the better reason. kindnet accepts a
#   NetworkPolicy, returns 201, stores it, and enforces nothing (LSN-006) — so every green from a
#   network check there was a statement about the API server's willingness to persist YAML. This
#   cluster is built with Dataplane V2, which enforces, so V-CTN-020 stops being a known liability
#   and becomes a real pass.
#
# WHY TWO NODES, and why it is asserted rather than assumed
#   RWO volumes exclude per NODE, so V-CMP-004's CLAIM 2 needs a second one (LSN-015). A one-node
#   cluster fails nothing loudly here; it silently turns that claim back into a deferral, which is
#   the quietest kind of regression there is. `pause.sh` scales to zero and `resume.sh` restores
#   two, so the everyday cheap path cannot leave you at one by accident either.
#
# WHAT IT INSTALLS, in order: project preflight -> cluster -> credentials + context rename ->
# node/dataplane assertions -> cert-manager -> operator (Cloud Build, push, deploy BY DIGEST) ->
# read-only VAP -> the three tier agent images.
#
# Idempotent: safe to re-run, and re-running is the supported way to pick up a source change.
# Exit: 0 = ready · 2 = refused (project preflight) · 3 = tool missing · 4 = cluster is the wrong
#       shape (node count, or a dataplane P4 does not know to enforce).
# Usage: dev/cluster/up.sh
#   CLUSTER=kube-agents-dev  PROJECT_ID=<gcloud default>  ZONE=us-east4-a
#   MACHINE_TYPE=e2-standard-4  NUM_NODES=2  SKIP_AGENT_IMAGES=0
set -euo pipefail

CLUSTER="${CLUSTER:-kube-agents-dev}"
PROJECT_ID="${PROJECT_ID:-}"
# Not `${PROJECT_ID:-$(gcloud ...)}`: under `set -e` a failing substitution in an assignment
# aborts here, with gcloud's exit code and no message, instead of reaching the check below.
[ -n "$PROJECT_ID" ] || PROJECT_ID="$(gcloud config get core/project 2>/dev/null)" || PROJECT_ID=""
ZONE="${ZONE:-us-east4-a}"
MACHINE_TYPE="${MACHINE_TYPE:-e2-standard-4}"
NUM_NODES="${NUM_NODES:-2}"
RELEASE_CHANNEL="${RELEASE_CHANNEL:-regular}"
CERT_MANAGER_VERSION="${CERT_MANAGER_VERSION:-v1.14.7}"
SKIP_AGENT_IMAGES="${SKIP_AGENT_IMAGES:-0}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"

# THE CONTEXT NAME IS A SECURITY CONTROL, not a label. Every destructive guard in dev/ is an
# anchored `case "$CTX" in gke-scratch-*)` and invariants-gate.py asserts they stay anchored
# (LSN-005). gcloud's own context name is `gke_<project>_<zone>_<cluster>`, which matches no arm,
# so the rename below is what makes this cluster addressable by the suite at all — and it is also
# what keeps `platform-agent-host` un-addressable by it, since nothing renames that one.
CTX="gke-scratch-$CLUSTER"
K="kubectl --context $CTX"

for tool in gcloud kubectl git; do
  command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: $tool is not installed." >&2; exit 3; }
done
[ -n "$PROJECT_ID" ] || {
  echo "ERROR: no GCP project set. gcloud config set project <id>, or PROJECT_ID=<id> $0" >&2
  exit 3
}

# --- project preflight -----------------------------------------------------------------------------
# lib/substrate-capacity.sh is the single definition site for "can this substrate hold the cluster",
# one function per substrate, each annotated with the create command it covers. invariants-gate.py
# `check_cluster_creating_scripts_assert_capacity` parses those annotations and fails any script
# that runs a covered create command without calling its preflight. The reason it lives there rather
# than here: a preflight grown one outage at a time only ever measures the PREVIOUS outage (LSN-027),
# so there has to be one place to add the next resource.
# shellcheck source=dev/lib/substrate-capacity.sh
. "$HERE/../lib/substrate-capacity.sh"
PROJECT_ID="$PROJECT_ID" REGION="${ZONE%-*}" assert_project_capacity

# --- cluster -----------------------------------------------------------------------------------
if gcloud container clusters describe "$CLUSTER" \
     --zone "$ZONE" --project "$PROJECT_ID" --format='value(name)' >/dev/null 2>&1; then
  echo "== cluster '$CLUSTER' already exists in $ZONE =="
else
  echo "== creating '$CLUSTER' in $ZONE: $NUM_NODES x $MACHINE_TYPE, Dataplane V2, Workload Identity =="
  echo "   (5-8 minutes)"
  # ZONAL, not regional, and that is not a cost footnote. `--num-nodes` on a regional cluster is
  # PER ZONE, so the same flag would build six nodes across three zones — triple the bill for a dev
  # cluster whose only shape requirement is "more than one node".
  #
  # --enable-dataplane-v2 is the whole point: it is what lets the NetworkPolicy suites pass rather
  # than defer, and it CANNOT be turned on later (GKE requires cluster recreation), so it belongs
  # at create time or nowhere.
  #
  # --workload-pool is here for the reverse reason: it is free now, and the WI deferrals in
  # egress-enforcement-l2.sh named "no metadata server" as a blocker they would no longer have. It
  # does not DISCHARGE those deferrals — nobody has bound a GSA to an agent KSA on this cluster —
  # but it stops the substrate from being the excuse.
  gcloud container clusters create "$CLUSTER" \
    --project "$PROJECT_ID" \
    --zone "$ZONE" \
    --num-nodes "$NUM_NODES" \
    --machine-type "$MACHINE_TYPE" \
    --release-channel "$RELEASE_CHANNEL" \
    --enable-dataplane-v2 \
    --workload-pool "$PROJECT_ID.svc.id.goog" \
    --enable-image-streaming
fi

# --- credentials and the context rename -----------------------------------------------------------
echo "== fetching credentials and naming the context '$CTX' =="
gcloud container clusters get-credentials "$CLUSTER" --zone "$ZONE" --project "$PROJECT_ID"
GEN_CTX="gke_${PROJECT_ID}_${ZONE}_${CLUSTER}"
if kubectl config get-contexts -o name 2>/dev/null | grep -qx "$GEN_CTX"; then
  # `get-credentials` re-creates the generated name on every call, so this runs on re-runs too.
  # The stale entry is deleted first: a leftover `gke-scratch-*` from a deleted cluster would make
  # the rename fail and leave the suite pointed at a context that no longer resolves.
  kubectl config delete-context "$CTX" >/dev/null 2>&1 || true
  kubectl config rename-context "$GEN_CTX" "$CTX" >/dev/null
fi
kubectl config get-contexts -o name | grep -qx "$CTX" || {
  echo "ERROR: context '$CTX' does not exist after get-credentials + rename." >&2
  echo "  Expected to rename '$GEN_CTX'. Contexts present:" >&2
  kubectl config get-contexts -o name | sed 's/^/    /' >&2
  exit 4
}

# --- shape assertions -------------------------------------------------------------------------
# Two properties the L2 suite silently depends on. Both are asserted here, at bring-up, because
# both fail QUIETLY downstream: a missing node turns CLAIM 2 into a deferral, and a dataplane P4
# does not recognise turns every network check into one. A deferral that appears because the
# cluster is the wrong shape is indistinguishable, in the ledger, from one that appears because the
# work is genuinely not done — so the shape is checked where it can still be a hard error.
echo "== waiting for nodes =="
$K wait --for=condition=Ready nodes --all --timeout=300s
nodes="$($K get nodes --no-headers 2>/dev/null | wc -l | tr -d ' ')"
if [ "${nodes:-0}" -lt 2 ]; then
  echo "ERROR: '$CLUSTER' has ${nodes:-0} Ready node(s), need >= 2." >&2
  echo "  If this cluster is paused, resume it:  dev/cluster/resume.sh" >&2
  echo "  If a pool was resized down, restore it:" >&2
  echo "    gcloud container clusters resize $CLUSTER --zone $ZONE --num-nodes 2 --quiet" >&2
  exit 4
fi
echo "   $nodes Ready nodes"

# P4 from the library, not a local `grep -qi calico`. A bespoke copy hard-codes one product and then
# defers on a cluster that enforces perfectly — a FALSE deferral on a BLOCKING-ALWAYS security
# property, which is the exact bug that was removed from verify-phase8.sh. One definition site
# (V-MET-013).
# shellcheck source=dev/lib/preconditions.sh
. "$HERE/../lib/preconditions.sh"
if p4_assert_enforcing_dataplane "$K"; then
  echo "   dataplane: $P4_DATAPLANE — NetworkPolicy is ENFORCED here"
else
  echo "ERROR: '$CLUSTER' has no dataplane P4 knows to enforce NetworkPolicy (see above)." >&2
  echo "  Dataplane V2 cannot be enabled on an existing cluster; GKE requires recreation. If this" >&2
  echo "  cluster predates --enable-dataplane-v2, replace it:  dev/cluster/down.sh && $0" >&2
  exit 4
fi

# --- cert-manager (the webhook's serving cert) --------------------------------------------------
if ! $K get ns cert-manager >/dev/null 2>&1; then
  echo "== installing cert-manager $CERT_MANAGER_VERSION =="
  $K apply -f "https://github.com/cert-manager/cert-manager/releases/download/${CERT_MANAGER_VERSION}/cert-manager.yaml"
fi
echo "== waiting for cert-manager =="
$K -n cert-manager wait --for=condition=Available deploy --all --timeout=300s

# --- operator ---------------------------------------------------------------------------------------
# Build FIRST, then deploy at the resulting digest. The order matters, and the other order does not
# work: `make deploy` needs an IMG, and on a cluster with no Deployment yet there is nothing for
# `reload-images.sh operator` to repoint. Deploying the upstream tag as a placeholder and repointing
# afterwards would mean rolling out somebody else's binary in the middle of a script whose whole job
# is to put YOUR code on the cluster.
echo "== building the operator image under test on Cloud Build =="
OP_REF="$(bash "$HERE/reload-images.sh" digest "$CTX")" || {
  echo "ERROR: could not build and resolve the operator image (see above)." >&2; exit 4; }

# KUBE_CONTEXT=, not KUBECTL=. The Makefile rejects a command-line KUBECTL override outright,
# because accepting one it does not read is what let a CRD land on the wrong cluster (LSN-018).
echo "== deploying the controller, CRD and webhooks to $CTX ($OP_REF) =="
make -C "$REPO_ROOT/k8s-operator" deploy IMG="$OP_REF" KUBE_CONTEXT="$CTX"
$K -n kubeagents-system rollout status deploy/kubeagents-controller-manager --timeout=300s

# No `rollout restart` here, and its absence is the point. The Kind version needed one because
# side-loading a fixed `:dev` tag left the Deployment spec unchanged, so the kubelet kept the copy
# it already had and P1 failed with "older than the source" — the LSN-001 stale-image trap in
# script form. IMG is a digest now. A changed digest changes the spec, which IS a rollout; an
# unchanged digest is genuinely the same image, and restarting it would prove nothing.

echo "== applying the read-only VAP =="
$K apply -f "$REPO_ROOT/examples/gitops-repo/policy/vap-agent-readonly.yaml"

# --- agent images -----------------------------------------------------------------------------------
# No per-node placement worry any more. `kind load` had to put the image on EVERY node, because an
# image present on only one turns a cross-node placement into an ImagePullBackOff — a false failure
# shaped exactly like "the agents cannot coexist". Nodes here pull from Artifact Registry, so every
# node can fetch every image and the whole class of concern is gone.
if [ "$SKIP_AGENT_IMAGES" = "1" ]; then
  echo "== SKIP_AGENT_IMAGES=1: not building the tier agent images =="
  echo "   multi-agent-namespace-l2.sh will DEFER on the missing image rather than fail. Build later:"
  echo "     dev/cluster/reload-images.sh agents $CTX"
else
  echo "== building and pushing the three tier agent images =="
  bash "$HERE/reload-images.sh" agents "$CTX"
fi

cat <<EOF

====================================================================
 '$CLUSTER' is ready.  Context: $CTX
   $nodes nodes · $P4_DATAPLANE (NetworkPolicy ENFORCED) · Workload Identity
   CRD + controller + webhooks at a digest · read-only VAP
====================================================================
Every line of dev/L2-CHAIN.txt targets this one cluster:
    while read -r c; do case "\$c" in ''|\#*) continue ;; esac; eval "\$c"; done < dev/L2-CHAIN.txt

Phase gate:   dev/verify/verify-phase8.sh $CTX
Pick up code: dev/cluster/reload-images.sh all $CTX
Stop paying:  dev/cluster/pause.sh      (nodes -> 0; resume.sh restores them in ~2 min)
Tear down:    dev/cluster/down.sh

NOT installed, deliberately: the gVisor sandbox pool. 08 §5's sandbox checks are unwritten
(verify-phase7.sh §D carries that deferral), so a pool would be an extra node running nothing —
progress-shaped spend that discharges no claim. When those checks land, one command adds it:
    gcloud container node-pools create gvisor-pool --cluster $CLUSTER --zone $ZONE \\
      --machine-type $MACHINE_TYPE --num-nodes 1 --sandbox type=gvisor \\
      --workload-metadata=GKE_METADATA --project $PROJECT_ID
EOF
