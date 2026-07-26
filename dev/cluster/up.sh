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
# Asked BEFORE the deploy that dirties it, because afterwards the two cases are indistinguishable.
MANAGER_KUSTOMIZATION="k8s-operator/config/manager/kustomization.yaml"
KUSTOMIZATION_WAS_DIRTY="$(git -C "$REPO_ROOT" status --porcelain -- "$MANAGER_KUSTOMIZATION" 2>/dev/null)" ||
  KUSTOMIZATION_WAS_DIRTY=""
OP_REF="$(bash "$HERE/reload-images.sh" digest "$CTX")" || {
  echo "ERROR: could not build and resolve the operator image (see above)." >&2; exit 4; }

# KUBE_CONTEXT=, not KUBECTL=. The Makefile rejects a command-line KUBECTL override outright,
# because accepting one it does not read is what let a CRD land on the wrong cluster (LSN-018).
echo "== deploying the controller, CRD and webhooks to $CTX ($OP_REF) =="
make -C "$REPO_ROOT/k8s-operator" deploy IMG="$OP_REF" KUBE_CONTEXT="$CTX"

# `make deploy` runs `kustomize edit set image`, which WRITES config/manager/kustomization.yaml in
# the working tree. Restoring it is not tidiness. That path is inside P1's build-input scope for the
# operator image (`_p1_build_inputs` maps k8s-operator/* to `k8s-operator`), so a deploy leaves a
# dirty file whose mtime is NEWER than the image that was just built -- and P1's freshness half then
# reports "built from a dirty tree BEFORE the newest edit", failing every gate run after a clean
# bring-up. The check is right; the bring-up was manufacturing the condition it detects. Restore
# only if the file was clean going in, so an edit somebody is actually working on is never discarded.
if [ -z "$KUSTOMIZATION_WAS_DIRTY" ]; then
  git -C "$REPO_ROOT" checkout -- "$MANAGER_KUSTOMIZATION" 2>/dev/null || true
fi

$K -n kubeagents-system rollout status deploy/kubeagents-controller-manager --timeout=300s

# No `rollout restart` here, and its absence is the point. The Kind version needed one because
# side-loading a fixed `:dev` tag left the Deployment spec unchanged, so the kubelet kept the copy
# it already had and P1 failed with "older than the source" — the LSN-001 stale-image trap in
# script form. IMG is a digest now. A changed digest changes the spec, which IS a rollout; an
# unchanged digest is genuinely the same image, and restarting it would prove nothing.

# --- router -------------------------------------------------------------------------------------
# The router ships in the same `make deploy` bundle as the controller but NOT under the same image
# knob: `make deploy` only rewrites `controller`, and kage-router is pinned separately in
# config/router/kustomization.yaml. Left alone it resolves to the published
# ghcr.io/gke-labs/kube-agents/kage-router:v0.1.0, which answers an anonymous pull with 403 -- so
# every bring-up ended with a router in ImagePullBackOff. That is 09 §11.9 ("built, never wired")
# happening inside the harness's own installer, and it was not a regression from Kind: the Kind loop
# never built the router either, it just failed later and less visibly. Built and repointed here so
# `up.sh` finishes with a cluster whose every workload is running code from this tree.
echo "== building the router image under test on Cloud Build =="
router_rc=0
bash "$HERE/reload-images.sh" router "$CTX" || router_rc=$?
case "$router_rc" in
  0) ROUTER_STATE="Running" ;;
  # 5 is "the digest is deployed, the pod will not start". Named here rather than swallowed, and
  # repeated in the closing banner, because a CrashLoopBackOff nobody warned you about is the most
  # expensive line on a fresh cluster: it looks exactly like the bring-up broke.
  5) ROUTER_STATE="CrashLoopBackOff — EXPECTED, see below" ;;
  *) echo "ERROR: could not build the router image (see above)." >&2; exit 4 ;;
esac

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
   kage-router at a digest · $ROUTER_STATE
====================================================================
Every line of dev/L2-CHAIN.txt targets this one cluster:
    while read -r c; do case "\$c" in ''|\#*) continue ;; esac; eval "\$c"; done < dev/L2-CHAIN.txt

Phase gate:   dev/verify/verify-phase8.sh $CTX
Pick up code: dev/cluster/reload-images.sh all $CTX
Stop paying:  dev/cluster/pause.sh      (nodes -> 0; resume.sh restores them in ~2 min)
Tear down:    dev/cluster/down.sh
EOF

# The router paragraph is printed under the condition it describes, not unconditionally. Told every
# time, it is a prediction: it reads as true on the run where the router is fine, and a disclosure
# that survives its own falsification has stopped disclosing anything. Told only on rc 5, it is a
# report -- and the `else` arm means the interesting case, a router that unexpectedly came up, is no
# longer the one outcome this banner is silent about. Nothing in the repo reads the router's state,
# so silence there would last until someone happened to look.
if [ "$router_rc" -eq 5 ]; then
  cat <<EOF

THE ROUTER CRASHLOOPS HERE, AND THAT IS THE CORRECT OUTCOME, not a broken bring-up. It runs the
image built from this tree — that part is now proven rather than assumed — and exits on
\`missing required --project-id\`, because config/router/deployment.yaml ships KAGE_PROJECT_ID and
KAGE_INBOUND_SUBSCRIPTION as EMPTY strings — deliberately, per V-CMP-003, so the failure names the
variable to set instead of a placeholder flowing into the Pub/Sub client and surfacing as a missing
credentials file — and its ServiceAccount carries no Workload Identity annotation. provision_03
step 5 is what sets them on a real install. Wiring them needs a Pub/Sub subscription and a GSA, which
is L3 work on a live install, not something an inner-loop cluster can or should invent. The router's
routing logic is proven hermetically against the pstest fake (go test ./internal/router/), so
nothing in dev/L2-CHAIN.txt depends on this pod. Confirm the reason, do not assume it:
    kubectl --context $CTX -n kubeagents-system logs deploy/kubeagents-router
EOF
else
  cat <<EOF

THE ROUTER CAME UP, WHICH THIS TREE DOES NOT EXPECT. config/router/deployment.yaml ships
KAGE_PROJECT_ID and KAGE_INBOUND_SUBSCRIPTION empty (V-CMP-003) and the ServiceAccount carries no
Workload Identity annotation, so \`missing required --project-id\` is the documented outcome and rc 5
is the documented return. Something wired this cluster out of band, or the config changed and the
disclosure did not. Either way a recorded gap is now false, which is a ledger edit and not a nicety:
    docs/build/LEDGER.md   -- Deferrals, the router row
    dev/cluster/reload-images.sh   -- the rc 4 / rc 5 split and the contract above it
EOF
fi

cat <<EOF

NOT installed, deliberately: the gVisor sandbox pool. 08 §5's sandbox checks are unwritten
(verify-phase7.sh §D carries that deferral), so a pool would be an extra node running nothing —
progress-shaped spend that discharges no claim. When those checks land, one command adds it:
    gcloud container node-pools create gvisor-pool --cluster $CLUSTER --zone $ZONE \\
      --machine-type $MACHINE_TYPE --num-nodes 1 --sandbox type=gvisor \\
      --workload-metadata=GKE_METADATA --project $PROJECT_ID
EOF
