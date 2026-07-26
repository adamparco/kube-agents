#!/usr/bin/env bash
# ==============================================================================
# 🧹 Teardown Step 13: Remove the agent network policies and the tenant floor
# ==============================================================================
# Mirrors provision_13. Runs FIRST in teardown.sh, before the agent tiers come down, for the same
# reason provision_13 runs last: while these policies are in force the agent pods can only reach the
# allowlist, and teardown of a tier can involve the controller draining or finalizing a workload.
# Removing the containment before removing the thing contained keeps teardown from stalling on
# traffic the policy would have dropped.
#
# WHY THIS SCRIPT HAD TO EXIST. teardown_12 deliberately KEEPS the tenant namespace ("it may hold
# workloads this platform does not own"), so everything provision_13 created in that namespace
# survives every teardown. On the next provision those stale policies are silently re-adopted — a
# NetworkPolicy left behind from an older allowlist would quietly govern the new install. P8-T2
# shipped provision_13 with no teardown at all; `dev/tests/install-path-wired.py` is what
# noticed.
#
# Order is the reverse of provision_13: floor first, then the per-tier allowlists, so no pod is ever
# left holding a deny floor with its allowlist already deleted.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh" "$@"

load_state

: "${NAMESPACE:=kubeagents-system}"
DEVELOPER_TEAM_NAMESPACE="${DEVELOPER_TEAM_NAMESPACE:-team-x}"
TENANT_DENY_NAME="${TENANT_DENY_NAME:-default-deny-all}"
TENANT_QUOTA_NAME="${TENANT_QUOTA_NAME:-${DEVELOPER_TEAM_NAMESPACE}-quota}"

confirm_action "This will remove the agent egress allowlists, the tenant default-deny floor, and the tenant ResourceQuota." \
  "Egress policies:platform-egress, cluster-admin-egress (${NAMESPACE}); developer-team-egress (${DEVELOPER_TEAM_NAMESPACE})" \
  "Tenant floor:${TENANT_DENY_NAME} (${DEVELOPER_TEAM_NAMESPACE})" \
  "Tenant quota:${TENANT_QUOTA_NAME} (${DEVELOPER_TEAM_NAMESPACE})"

if [ "${DRY_RUN:-0}" -eq 1 ]; then
  echo -e "  ${C_GREEN}[DRY-RUN] Would delete the tenant floor, the three per-tier egress policies, and the tenant quota.${C_RESET}"
  exit 0
fi

# ─── the tenant floor, first ──────────────────────────────────────────────────
if [ -n "${DEVELOPER_TEAM_NAMESPACE}" ]; then
  echo -e "  ${C_CYAN}ℹ Removing the tenant default-deny floor...${C_RESET}"
  kubectl delete networkpolicy "${TENANT_DENY_NAME}" -n "${DEVELOPER_TEAM_NAMESPACE}" --ignore-not-found=true || true
fi

# ─── the per-tier allowlists ──────────────────────────────────────────────────
echo -e "  ${C_CYAN}ℹ Removing the per-tier egress allowlists...${C_RESET}"
kubectl delete networkpolicy "platform-egress" -n "${NAMESPACE}" --ignore-not-found=true || true
kubectl delete networkpolicy "cluster-admin-egress" -n "${NAMESPACE}" --ignore-not-found=true || true
if [ -n "${DEVELOPER_TEAM_NAMESPACE}" ]; then
  kubectl delete networkpolicy "developer-team-egress" -n "${DEVELOPER_TEAM_NAMESPACE}" --ignore-not-found=true || true
fi

# ─── the tenant quota ─────────────────────────────────────────────────────────
# provision_12 applies this (it must precede the pod it governs), but teardown_12 keeps the tenant
# namespace, so the quota outlives it. Removing it here rather than there keeps the whole tenant
# isolation bundle — quota, floor, allowlist — reversible from one place.
if [ -n "${DEVELOPER_TEAM_NAMESPACE}" ]; then
  echo -e "  ${C_CYAN}ℹ Removing the tenant ResourceQuota...${C_RESET}"
  kubectl delete resourcequota "${TENANT_QUOTA_NAME}" -n "${DEVELOPER_TEAM_NAMESPACE}" --ignore-not-found=true || true
fi

echo -e "\n${C_GREEN}${C_BOLD}✅ Network policies and tenant isolation removed!${C_RESET}"
echo -e "  ${C_YELLOW}⚠ Agent pods now have UNRESTRICTED egress until provision_13 runs again.${C_RESET}"
