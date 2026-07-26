#!/usr/bin/env bash
# up.sh — bring up THE inner-loop cluster, whole. One cluster, two nodes, an enforcing dataplane,
# the operator under test, the VAP and the agent images. Exit 0 means every L2 suite in
# `dev/L2-CHAIN.txt` can run against it; there is no second target to remember.
#
# WHY ONE CLUSTER
#   This replaces `up-egress.sh` (Calico, 1 node) and `up-2node.sh` (kindnet, 2 nodes). Those two
#   plus this one differed in exactly two create-time knobs, CNI and node count, which are
#   orthogonal — so the union is a single cluster and nothing was lost by merging them. What was
#   gained: 4 Kind nodes became 2, three control planes became one, and the host stopped being the
#   binding constraint. That matters more than it sounds — LSN-026 is three false security failures
#   caused by nothing but the memory pressure of carrying two of these at once.
#
#   Both knobs are load-bearing and kind-config.yaml explains why in full: kindnet enforces no
#   NetworkPolicy (LSN-006 / P4), and RWO excludes per NODE so CLAIM 2 needs a second one (LSN-015).
#
# WHAT IT INSTALLS, in order: host preflight -> cluster -> Calico -> cert-manager -> operator (build,
# load, deploy) -> read-only VAP -> the three tier agent images on BOTH nodes.
#
# Idempotent: safe to re-run, and re-running is the supported way to pick up a source change.
# Exit: 0 = ready · 2 = refused (host too small) · 3 = tool missing · 4 = wrong node count.
# Usage: dev/cluster/up.sh
#   CLUSTER=kube-agents-dev  KIND_IMAGE=kindest/node:v1.31.2  CALICO_VERSION=v3.28.0
#   ALLOW_TIGHT_MEMORY=0  SKIP_AGENT_IMAGES=0
set -euo pipefail

CLUSTER="${CLUSTER:-kube-agents-dev}"
KIND_IMAGE="${KIND_IMAGE:-kindest/node:v1.31.2}"
CALICO_VERSION="${CALICO_VERSION:-v3.28.0}"
CERT_MANAGER_VERSION="${CERT_MANAGER_VERSION:-v1.14.7}"
OP_IMG="${OP_IMG:-kube-agents/k8s-operator:dev}"
SKIP_AGENT_IMAGES="${SKIP_AGENT_IMAGES:-0}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
CTX="kind-$CLUSTER"
K="kubectl --context $CTX"

for tool in kind kubectl docker; do
  command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: $tool is not installed." >&2; exit 3; }
done

# --- host preflight ------------------------------------------------------------------------------
# assert_host_capacity is the single definition site for "can this host hold a Kind cluster", and
# invariants-gate.py `check_cluster_creating_scripts_assert_host_capacity` fails any script that
# runs `kind create cluster` without calling it. Two resources, two separate incidents, and the
# reason both live there rather than one: a preflight grown one outage at a time only ever measures
# the PREVIOUS outage (LSN-027). Override with ALLOW_TIGHT_MEMORY=1 if you know better.
. "$HERE/../lib/host-capacity.sh"
assert_host_capacity

# --- cluster ---------------------------------------------------------------------------------------
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "== Kind cluster '$CLUSTER' already exists =="
else
  echo "== creating '$CLUSTER': 2 nodes, default CNI disabled =="
  kind create cluster --name "$CLUSTER" --image "$KIND_IMAGE" --config "$HERE/kind-config.yaml"
fi

# Asserted, not assumed. A one-node cluster here fails nothing loudly — it silently turns
# V-CMP-004's CLAIM 2 back into a deferral, which is the quietest kind of regression there is.
nodes="$($K get nodes --no-headers 2>/dev/null | wc -l | tr -d ' ')"
if [ "${nodes:-0}" -lt 2 ]; then
  echo "ERROR: '$CLUSTER' has ${nodes:-0} node(s), need 2. Delete it and re-run:" >&2
  echo "  kind delete cluster --name $CLUSTER && $0" >&2
  exit 4
fi
echo "   $nodes nodes present"

# --- Calico (the enforcing dataplane, P4) ------------------------------------------------------
# Nodes stay NotReady until a CNI is installed — expected here, not a failure.
if ! $K -n kube-system get daemonset calico-node >/dev/null 2>&1; then
  echo "== installing Calico $CALICO_VERSION =="
  $K apply -f \
    "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/calico.yaml"
fi
echo "== waiting for the dataplane =="
$K -n kube-system rollout status ds/calico-node --timeout=300s
$K wait --for=condition=Ready nodes --all --timeout=300s

# --- cert-manager (the webhook's serving cert) --------------------------------------------------
if ! $K get ns cert-manager >/dev/null 2>&1; then
  echo "== installing cert-manager $CERT_MANAGER_VERSION =="
  $K apply -f "https://github.com/cert-manager/cert-manager/releases/download/${CERT_MANAGER_VERSION}/cert-manager.yaml"
fi
echo "== waiting for cert-manager =="
$K -n cert-manager wait --for=condition=Available deploy --all --timeout=300s

# --- operator ---------------------------------------------------------------------------------------
echo "== building and loading the operator image under test =="
make -C "$REPO_ROOT/k8s-operator" docker-build IMG="$OP_IMG"
kind load docker-image "$OP_IMG" --name "$CLUSTER"

# KUBE_CONTEXT=, not KUBECTL=. The Makefile rejects a command-line KUBECTL override outright,
# because accepting one it does not read is what let a CRD land on the wrong cluster (LSN-018).
echo "== deploying the controller, CRD and webhooks to $CTX =="
make -C "$REPO_ROOT/k8s-operator" deploy IMG="$OP_IMG" KUBE_CONTEXT="$CTX"
# On a re-run the Deployment spec is unchanged, so `kind load` alone leaves the OLD image running
# and P1 fails with "older than the source". Restart unconditionally; it is free on a fresh install.
$K -n kubeagents-system rollout restart deploy/kubeagents-controller-manager >/dev/null 2>&1 || true
$K -n kubeagents-system rollout status deploy/kubeagents-controller-manager --timeout=300s

echo "== applying the read-only VAP =="
$K apply -f "$REPO_ROOT/examples/gitops-repo/policy/vap-agent-readonly.yaml"

# --- agent images -----------------------------------------------------------------------------------
# `kind load` puts the image on EVERY node, which matters because the two agents are meant to land on
# DIFFERENT nodes; an image on only one turns a cross-node placement into an ImagePullBackOff, and
# that reads as "the agents cannot coexist" — a false failure with the right shape to be believed.
if [ "$SKIP_AGENT_IMAGES" = "1" ]; then
  echo "== SKIP_AGENT_IMAGES=1: not building the tier agent images =="
  echo "   multi-agent-namespace-l2.sh will DEFER on the missing image rather than fail. Build later:"
  echo "     dev/cluster/reload-images.sh agents $CTX"
else
  echo "== building and loading the tier agent images onto both nodes =="
  bash "$HERE/reload-images.sh" agents "$CTX"
fi

cat <<EOF

====================================================================
 '$CLUSTER' is ready.  Context: $CTX
   2 nodes · Calico (NetworkPolicy ENFORCED) · CRD + controller + webhooks · read-only VAP
====================================================================
Every line of dev/L2-CHAIN.txt targets this one cluster:
    while read -r c; do case "\$c" in ''|\#*) continue ;; esac; eval "\$c"; done < dev/L2-CHAIN.txt

Phase gate:   dev/verify/verify-phase8.sh $CTX
Tear down:    kind delete cluster --name $CLUSTER
EOF
