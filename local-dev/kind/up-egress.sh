#!/usr/bin/env bash
# Bring up the NetworkPolicy-ENFORCING Kind cluster (Calico) — the only inner-loop target on which an
# egress claim may be green (LSN-006; `binding.md` precondition P4).
#
# kindnet, the default Kind CNI, accepts a NetworkPolicy and silently enforces nothing. A suite that
# applies a default-deny policy on kindnet and then asserts "the pod is contained" passes while
# containing nothing — which is why V-CTN-020's L2 instance is pinned to THIS cluster and is recorded
# as `deferred`, never `pass`, anywhere else.
#
# Idempotent: safe to re-run. Creating the cluster is the slow part (~2 min); Calico's rollout is the
# other ~2 min. Both are waited on, so when this script exits 0 the dataplane is actually programming
# policy — not merely installed.
#
# Usage: local-dev/kind/up-egress.sh
#   CLUSTER=kube-agents-egress  KIND_IMAGE=kindest/node:v1.31.2  CALICO_VERSION=v3.28.0
set -euo pipefail

CLUSTER="${CLUSTER:-kube-agents-egress}"
KIND_IMAGE="${KIND_IMAGE:-kindest/node:v1.31.2}"
CALICO_VERSION="${CALICO_VERSION:-v3.28.0}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CTX="kind-$CLUSTER"

if ! command -v kind >/dev/null 2>&1; then
  echo "kind not found. Install: go install sigs.k8s.io/kind@latest  (or: brew install kind)" >&2
  exit 1
fi

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "Kind cluster '$CLUSTER' already exists."
else
  echo "== creating '$CLUSTER' with the default CNI disabled =="
  kind create cluster --name "$CLUSTER" --image "$KIND_IMAGE" --config "$HERE/kind-calico.yaml"
fi

# Nodes stay NotReady until a CNI is installed — that is expected here, not a failure.
if ! kubectl --context "$CTX" -n kube-system get daemonset calico-node >/dev/null 2>&1; then
  echo "== installing Calico $CALICO_VERSION =="
  kubectl --context "$CTX" apply -f \
    "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/calico.yaml"
fi

echo "== waiting for the dataplane to be ready =="
kubectl --context "$CTX" -n kube-system rollout status ds/calico-node --timeout=300s
kubectl --context "$CTX" wait --for=condition=Ready nodes --all --timeout=300s

echo
echo "Cluster '$CLUSTER' ready with an ENFORCING dataplane. Context: $CTX"
echo "Prove enforcement:  local-dev/tests/egress-enforcement.sh $CTX"
echo "Prove V-CTN-020:    local-dev/kind/egress-enforcement-l2.sh $CTX"
