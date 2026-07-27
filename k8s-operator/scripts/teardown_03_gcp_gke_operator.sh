#!/usr/bin/env bash
# ==============================================================================
# 🧹 Step 2: Teardown Kubernetes Operator (CRDs & Controller Manager)
# ==============================================================================
# Idempotent script to clean up the deployed operator and CRDs.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$SCRIPT_DIR" == */scripts ]]; then
  OPERATOR_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
else
  OPERATOR_DIR="${SCRIPT_DIR}"
fi
VARS_FILE="${SCRIPT_DIR}/vars.sh"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
source "${SCRIPT_DIR}/common.sh" "$@"

# ─── Configuration State Restoration ──────────────────────────────────────────
ensure_teardown_state

# The cluster the `make` targets below delete from, named rather than inherited (LSN-018: a context
# override the Makefile did not read was accepted and ignored, and the CRD went to whatever
# `kubectl config current-context` was). Both `connect_cluster` and CI's get-gke-credentials write
# exactly this context name; set KUBE_CONTEXT to point the run somewhere else.
KUBE_CONTEXT="${KUBE_CONTEXT:-gke_${PROJECT_ID}_${REGION}_${CLUSTER_NAME}}"

# ─── Confirmation Prompt ──────────────────────────────────────────────────────
confirm_action "This will permanently undeploy the Kubernetes Operator and remove its CRDs from the GKE cluster." \
  "GCP Project:$PROJECT_ID" \
  "GKE Cluster:$CLUSTER_NAME"

gcloud config set project "$PROJECT_ID" --quiet

# ─── Step 1: Connect to GKE Cluster ───────────────────────────────────────────
CLUSTER_EXISTS=$(cluster_exists)
if [ -n "$CLUSTER_EXISTS" ]; then
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    echo -e "  ${C_GREEN}[DRY-RUN] Would connect to GKE cluster and check for operator deployment.${C_RESET}"
  else
    connect_cluster || true
  fi
else
  echo -e "  ${C_GREEN}✓ GKE cluster '${CLUSTER_NAME}' does not exist. Skipping operator cleanup.${C_RESET}"
  exit 0
fi

# ─── Step 1b: Remove agent admission policies ─────────────────────────────────
# Mirrors step 4 of provision_03. Cluster-scoped, so `make undeploy` does not touch them.
echo -e "  ${C_CYAN}ℹ Removing agent admission policies (VAP)...${C_RESET}"
if [ "${DRY_RUN:-0}" -eq 1 ]; then
  echo -e "  ${C_GREEN}[DRY-RUN] Would delete ValidatingAdmissionPolicies kube-agents-agent-readonly, kube-agents-agent-pod-hardening and kube-agents-agent-scope-journal.${C_RESET}"
else
  kubectl delete validatingadmissionpolicybinding kube-agents-agent-readonly kube-agents-agent-pod-hardening kube-agents-agent-scope-journal --ignore-not-found 2>/dev/null || true
  kubectl delete validatingadmissionpolicy kube-agents-agent-readonly kube-agents-agent-pod-hardening kube-agents-agent-scope-journal --ignore-not-found 2>/dev/null || true
  echo -e "  ${C_GREEN}✓ Agent admission policies removed.${C_RESET}"
fi

# ─── Step 1c: Remove the control-namespace quota ──────────────────────────────
# Mirrors step 4 of provision_03. `make undeploy` deletes the namespace on a full teardown, which
# takes the quota with it — but a partial teardown that leaves the namespace standing would
# otherwise strand a quota with no control plane to size it against, and the next provision run
# would apply over it rather than from a clean slate.
echo -e "  ${C_CYAN}ℹ Removing control-namespace ResourceQuota...${C_RESET}"
if [ "${DRY_RUN:-0}" -eq 1 ]; then
  echo -e "  ${C_GREEN}[DRY-RUN] Would delete ResourceQuota ${CONTROL_QUOTA_NAME:-${NAMESPACE}-quota} in ${NAMESPACE}.${C_RESET}"
else
  kubectl delete resourcequota "${CONTROL_QUOTA_NAME:-${NAMESPACE}-quota}" -n "${NAMESPACE}" --ignore-not-found 2>/dev/null || true
  echo -e "  ${C_GREEN}✓ Control-namespace ResourceQuota removed.${C_RESET}"
fi

# ─── Step 2: Undeploy Operator Manager ────────────────────────────────────────
OPERATOR_DEPLOYED=""
if [ "${DRY_RUN:-0}" -ne 1 ]; then
  OPERATOR_DEPLOYED=$(kubectl get deployment kubeagents-controller-manager -n "${NAMESPACE}" --ignore-not-found 2>/dev/null || echo "")
fi

if [ "${DRY_RUN:-0}" -eq 1 ] || [ -n "$OPERATOR_DEPLOYED" ]; then
  echo -e "  ${C_CYAN}ℹ Undeploying Operator Controller Manager from GKE cluster...${C_RESET}"
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    echo -e "  ${C_GREEN}[DRY-RUN] Would undeploy Operator Controller Manager.${C_RESET}"
  else
    make -C "$OPERATOR_DIR" undeploy KUBE_CONTEXT="$KUBE_CONTEXT" ignore-not-found=true
    echo -e "  ${C_GREEN}✓ Operator Controller Manager undeployed successfully.${C_RESET}"
  fi
else
  echo -e "  ${C_GREEN}✓ Operator Controller Manager is already undeployed.${C_RESET}"
fi

# ─── Step 3: Uninstall Custom Resource Definitions (CRDs) ─────────────────────
CRDS_INSTALLED=""
if [ "${DRY_RUN:-0}" -ne 1 ]; then
  CRDS_INSTALLED=$(kubectl get crds -o jsonpath='{.items[*].metadata.name}' 2>/dev/null | grep -o 'agents.kubeagents.x-k8s.io' || echo "")
fi

if [ "${DRY_RUN:-0}" -eq 1 ] || [ -n "$CRDS_INSTALLED" ]; then
  echo -e "  ${C_CYAN}ℹ Uninstalling CRDs from GKE cluster...${C_RESET}"
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    echo -e "  ${C_GREEN}[DRY-RUN] Would uninstall Custom Resource Definitions (CRDs).${C_RESET}"
  else
    make -C "$OPERATOR_DIR" uninstall KUBE_CONTEXT="$KUBE_CONTEXT" ignore-not-found=true
    echo -e "  ${C_GREEN}✓ CRDs uninstalled successfully.${C_RESET}"
  fi
else
  echo -e "  ${C_GREEN}✓ CRDs are already uninstalled.${C_RESET}"
fi
