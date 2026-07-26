#!/usr/bin/env bash
# reload-images.sh — build kube-agents images FROM YOUR WORKING TREE and load them
# into a Kind cluster, so the inner-loop gates test YOUR code, never the upstream
# published image (ghcr.io/gke-labs/...:v0.1.0).
#
# WHY THIS EXISTS
#   `make deploy` does NOT build — it only runs `kustomize set image` + `kubectl apply`.
#   Deploying the published tag therefore tests the UPSTREAM binary, not your changes.
#   And a Kind cluster only sees an image after `kind load docker-image ...`.
#   This script does build -> kind load -> point the running workload at the local image,
#   which is the whole "test my local changes on Kind" loop in one command.
#
# THE imagePullPolicy TRAP (important)
#   The controller renders agent pods with imagePullPolicy: PullAlways by DEFAULT
#   (k8s-operator/internal/controller/agent_manifests.go). With PullAlways the kubelet
#   IGNORES the kind-loaded image and re-pulls from the registry — silently running the
#   upstream image. For local Kind testing your Agent CR MUST set:
#       spec.deployment.imagePullPolicy: IfNotPresent   # (or Never)
#   The example CRs already do this. The operator Deployment already uses IfNotPresent.
#
# THE stale-image rule
#   Local images reuse a fixed tag (e.g. :dev). Same tag + IfNotPresent = the kubelet will
#   NOT refresh a copy it already has. This script always rebuilds AND reloads, so a
#   `set image`/`rollout restart` afterward genuinely picks up your latest source.
#
# Usage: local-dev/kind/reload-images.sh [operator|agents|all] [kube-context]
#   operator (default)  build+load the controller image, repoint + restart the controller
#   agents              build+load the three tier agent images (you then restart the agent
#                       Deployments; their CRs must use imagePullPolicy IfNotPresent)
#   all                 both
set -uo pipefail

TARGET="${1:-operator}"
CTX="${2:-kind-kube-agents-dev}"
CLUSTER="${CTX#kind-}"                       # kind context is kind-<clustername>
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

OP_IMG="kube-agents/k8s-operator:dev"        # local tag — deliberately NOT the ghcr name
AGENT_REPO="kube-agents"                      # -> kube-agents/<tier>-agent:$AGENT_TAG
# ONE definition site for the agent tag, passed to `make` and used to find what `make` produced.
#
# This used to be two: the loop below looked for `:latest` while the build took whatever the root
# Makefile's `TAG ?=` happened to be. P8-T5 changed that default to `src-<sha>` to stop publishing
# `:latest`, and this script kept looking for a tag nothing produced any more — so every tier missed,
# each printed "(skip … not built for this REPO)", and the script exited 0 having loaded NOTHING.
# That is LSN-021 (a command is "run" and does nothing) and V-MET-013 (two definition sites that
# drifted). It is also the whole reason the agent gateways sat in ImagePullBackOff: the one script
# whose job is to put those images on the host had been quietly declining to.
AGENT_TAG="${AGENT_TAG:-dev}"
NS=kubeagents-system

# --- DESTRUCTIVE-TEST GUARD: only touch a Kind (or scratch-GKE) context ---------------------
case "$CTX" in
  kind-*|gke-scratch-*) : ;;
  *) echo "REFUSING: context '$CTX' is not a Kind cluster (image-reload guard)." >&2; exit 2 ;;
esac

command -v kind >/dev/null 2>&1 || { echo "ERROR: kind is not installed (brew install kind)." >&2; exit 3; }
command -v docker >/dev/null 2>&1 || { echo "ERROR: docker is not available." >&2; exit 3; }
cd "$REPO_ROOT"

K="kubectl --context $CTX"

reload_operator() {
  echo "== operator =="
  echo "-> building $OP_IMG from local source"
  make -C k8s-operator docker-build IMG="$OP_IMG"
  echo "-> loading $OP_IMG into kind cluster '$CLUSTER'"
  kind load docker-image "$OP_IMG" --name "$CLUSTER"
  echo "-> repointing the controller at the local image and restarting"
  $K -n "$NS" set image deploy/kubeagents-controller-manager manager="$OP_IMG"
  $K -n "$NS" rollout restart deploy/kubeagents-controller-manager
  $K -n "$NS" rollout status  deploy/kubeagents-controller-manager --timeout=120s
  echo "OK: controller now running $OP_IMG"
  echo "   verify: $K -n $NS get deploy kubeagents-controller-manager -o jsonpath='{.spec.template.spec.containers[0].image}'"
}

reload_agents() {
  echo "== agents =="
  echo "-> building agent images ($AGENT_REPO/<tier>-agent:$AGENT_TAG) from local source"
  make docker-build-agents REPO="$AGENT_REPO" TAG="$AGENT_TAG"
  local missing=0
  for tier in platform cluster-admin developer-team; do
    img="$AGENT_REPO/${tier}-agent:$AGENT_TAG"
    if docker image inspect "$img" >/dev/null 2>&1; then
      echo "-> loading $img into kind cluster '$CLUSTER'"
      kind load docker-image "$img" --name "$CLUSTER"
    else
      # HARD failure, not a skip. The soft "(skip …)" this replaces is what let the tag drift go
      # unnoticed: `make` had just been told to build this exact name, so it being absent means the
      # build did not do what this script asked. Reporting that as a skip and exiting 0 turns a
      # broken reload into a silent no-op, and the only symptom is an ImagePullBackOff somewhere
      # else, minutes later, that looks like a cluster problem.
      echo "ERROR: $img is absent after 'make docker-build-agents' was told to build it." >&2
      missing=$((missing + 1))
    fi
  done
  if [ "$missing" -gt 0 ]; then
    echo "ERROR: $missing of 3 agent images missing — refusing to report success." >&2
    return 4
  fi
  cat <<EOF
OK: all 3 agent images built and loaded at tag '$AGENT_TAG'. To make the cluster USE them, each
Agent CR must set BOTH — an empty spec.deployment.image does NOT fall back to the local build, it
defaults to ghcr.io/gke-labs/kube-agents/<tier>-agent:v0.1.0, which this host has never had:
    spec.deployment.image:           $AGENT_REPO/<tier>-agent:$AGENT_TAG
    spec.deployment.imagePullPolicy: IfNotPresent   # default PullAlways ignores kind-loaded images
  then restart the agent workload, e.g.:
    $K -n <namespace> rollout restart deploy/<agent-name>-gateway
EOF
}

case "$TARGET" in
  operator) reload_operator ;;
  agents)   reload_agents ;;
  all)      reload_operator; reload_agents ;;
  *) echo "usage: $0 [operator|agents|all] [kube-context]" >&2; exit 1 ;;
esac
