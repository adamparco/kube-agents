#!/usr/bin/env bash
# ==============================================================================
# 🧹 Teardown Step 12: Remove the child agent tiers
# ==============================================================================
# Mirrors provision_12. Deletes leaf-first (developer-team, then cluster-admin) so no Agent is
# left with a dangling parentRef mid-teardown. The Agent CRs go first while the controller is
# still running, letting it clean up the Deployments/PVCs it owns.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh" "$@"

load_state

: "${NAMESPACE:=kubeagents-system}"
CLUSTER_ADMIN_KSA_NAME="${CLUSTER_ADMIN_KSA_NAME:-cluster-admin-agent}"
CLUSTER_ADMIN_AGENT_NAME="${CLUSTER_ADMIN_AGENT_NAME:-cluster-admin-${CLUSTER_NAME}}"
DEVELOPER_TEAM_NAMESPACE="${DEVELOPER_TEAM_NAMESPACE:-team-x}"
DEVELOPER_TEAM_KSA_NAME="${DEVELOPER_TEAM_KSA_NAME:-developer-team-agent}"
DEVELOPER_TEAM_AGENT_NAME="${DEVELOPER_TEAM_AGENT_NAME:-developer-team-${DEVELOPER_TEAM_NAMESPACE}}"

confirm_action "This will delete the cluster-admin and developer-team agents and their identities." \
  "Cluster Admin Agent:${CLUSTER_ADMIN_AGENT_NAME}" \
  "Developer Team Agent:${DEVELOPER_TEAM_AGENT_NAME} (namespace ${DEVELOPER_TEAM_NAMESPACE})"

if [ "${DRY_RUN:-0}" -eq 1 ]; then
  echo -e "  ${C_GREEN}[DRY-RUN] Would delete both child agent tiers and their RBAC.${C_RESET}"
  exit 0
fi

# ─── developer-team (leaf) ────────────────────────────────────────────────────
if [ -n "${DEVELOPER_TEAM_NAMESPACE}" ]; then
  echo -e "  ${C_CYAN}ℹ Removing developer-team tier...${C_RESET}"
  kubectl delete agent "${DEVELOPER_TEAM_AGENT_NAME}" -n "${DEVELOPER_TEAM_NAMESPACE}" --ignore-not-found=true --timeout=120s || true
  kubectl delete rolebinding "${DEVELOPER_TEAM_KSA_NAME}-explorer" -n "${DEVELOPER_TEAM_NAMESPACE}" --ignore-not-found=true || true
  kubectl delete role "${DEVELOPER_TEAM_KSA_NAME}-explorer" -n "${DEVELOPER_TEAM_NAMESPACE}" --ignore-not-found=true || true
  kubectl delete serviceaccount "${DEVELOPER_TEAM_KSA_NAME}" -n "${DEVELOPER_TEAM_NAMESPACE}" --ignore-not-found=true || true
  kubectl delete secret "${DEVELOPER_TEAM_KSA_NAME}-secrets" -n "${DEVELOPER_TEAM_NAMESPACE}" --ignore-not-found=true || true
  # The ExternalName aliases provision_12 applied. Deleted only if they are still ExternalName:
  # if someone has since replaced one with a real Service, that Service is theirs, and teardown of
  # this platform is not a licence to delete it. The same asymmetry as the namespace below.
  for _alias in litellm github-token-minter; do
    if [ "$(kubectl get service "${_alias}" -n "${DEVELOPER_TEAM_NAMESPACE}" -o jsonpath='{.spec.type}' 2>/dev/null || echo "")" = "ExternalName" ]; then
      kubectl delete service "${_alias}" -n "${DEVELOPER_TEAM_NAMESPACE}" --ignore-not-found=true || true
    fi
  done
  # The tenant namespace is intentionally left in place: it may hold workloads this platform
  # does not own. Delete it explicitly if you want it gone.
  echo -e "  ${C_GREEN}✓ developer-team tier removed (namespace ${DEVELOPER_TEAM_NAMESPACE} kept).${C_RESET}"
fi

# ─── cluster-admin (parent) ───────────────────────────────────────────────────
echo -e "  ${C_CYAN}ℹ Removing cluster-admin tier...${C_RESET}"
kubectl delete agent "${CLUSTER_ADMIN_AGENT_NAME}" -n "${NAMESPACE}" --ignore-not-found=true --timeout=120s || true
kubectl delete clusterrolebinding "${CLUSTER_ADMIN_KSA_NAME}-explorer" --ignore-not-found=true || true
kubectl delete clusterrole "${CLUSTER_ADMIN_KSA_NAME}-explorer" --ignore-not-found=true || true
kubectl delete serviceaccount "${CLUSTER_ADMIN_KSA_NAME}" -n "${NAMESPACE}" --ignore-not-found=true || true
kubectl delete secret "${CLUSTER_ADMIN_KSA_NAME}-secrets" -n "${NAMESPACE}" --ignore-not-found=true || true
echo -e "  ${C_GREEN}✓ cluster-admin tier removed.${C_RESET}"

echo -e "\n${C_GREEN}${C_BOLD}✅ Agent tiers cleaned up!${C_RESET}"
