#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 12: Deploy the child agent tiers (cluster-admin, developer-team)
# ==============================================================================
# provision_08 deploys the platform tier. This step adds the two tiers below it so a fresh
# install exercises the full hierarchy rather than a single agent.
#
# Their GSAs and Workload Identity bindings come from provision_04 (steps 4 and 5); this step
# only creates the in-cluster identity, the API-server secret, and the Agent CR.
#
# Set CLUSTER_ADMIN_ENABLED=false to skip the cluster-admin tier, or DEVELOPER_TEAM_NAMESPACE=''
# to skip the tenant tier (the developer-team tier requires the cluster-admin tier: the webhook
# rejects a child whose parentRef does not resolve).
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh" "$@"

print_step "Checking Local Prerequisites"
check_prereqs "kubectl" "envsubst" "openssl"

print_step "Setting up Configuration State for Agent Tiers"
load_state

: "${NAMESPACE:=kubeagents-system}"

# Identity + naming. Defaults mirror the exemplars in examples/gitops-repo/.
export CLUSTER_ADMIN_ENABLED="${CLUSTER_ADMIN_ENABLED:-true}"
export CLUSTER_ADMIN_KSA_NAME="${CLUSTER_ADMIN_KSA_NAME:-cluster-admin-agent}"
export CLUSTER_ADMIN_GSA_NAME="${CLUSTER_ADMIN_GSA_NAME:-kubeagents-cluster-admin-gsa}"
export CLUSTER_ADMIN_AGENT_NAME="${CLUSTER_ADMIN_AGENT_NAME:-cluster-admin-${CLUSTER_NAME}}"

export DEVELOPER_TEAM_NAMESPACE="${DEVELOPER_TEAM_NAMESPACE:-team-x}"
export DEVELOPER_TEAM_KSA_NAME="${DEVELOPER_TEAM_KSA_NAME:-developer-team-agent}"
export DEVELOPER_TEAM_GSA_NAME="${DEVELOPER_TEAM_GSA_NAME:-kubeagents-developer-team-gsa}"
export DEVELOPER_TEAM_AGENT_NAME="${DEVELOPER_TEAM_AGENT_NAME:-developer-team-${DEVELOPER_TEAM_NAMESPACE}}"

# Images. Falling back to AGENT_IMAGE's registry keeps a source-built install consistent:
# without an explicit image the controller resolves the per-tier ghcr.io default instead.
_registry_of() { echo "${1%/*}"; }
if [ -n "${AGENT_IMAGE:-}" ]; then
  _reg="$(_registry_of "${AGENT_IMAGE}")"
  export CLUSTER_ADMIN_IMAGE="${CLUSTER_ADMIN_IMAGE:-${_reg}/cluster-admin-agent}"
  export DEVELOPER_TEAM_IMAGE="${DEVELOPER_TEAM_IMAGE:-${_reg}/developer-team-agent}"
  export CLUSTER_ADMIN_TAG="${CLUSTER_ADMIN_TAG:-${AGENT_TAG:-v0.1.0}}"
  export DEVELOPER_TEAM_TAG="${DEVELOPER_TEAM_TAG:-${AGENT_TAG:-v0.1.0}}"
else
  export CLUSTER_ADMIN_IMAGE="${CLUSTER_ADMIN_IMAGE:-ghcr.io/gke-labs/kube-agents/cluster-admin-agent}"
  export DEVELOPER_TEAM_IMAGE="${DEVELOPER_TEAM_IMAGE:-ghcr.io/gke-labs/kube-agents/developer-team-agent}"
  export CLUSTER_ADMIN_TAG="${CLUSTER_ADMIN_TAG:-v0.1.0}"
  export DEVELOPER_TEAM_TAG="${DEVELOPER_TEAM_TAG:-v0.1.0}"
fi

# Child tiers default to no chat integration: a single Slack app token supports one Socket Mode
# connection, which the platform tier already holds.
export CLUSTER_ADMIN_GOOGLE_CHAT_ENABLED="${CLUSTER_ADMIN_GOOGLE_CHAT_ENABLED:-false}"
export CLUSTER_ADMIN_SLACK_ENABLED="${CLUSTER_ADMIN_SLACK_ENABLED:-false}"
export DEVELOPER_TEAM_GOOGLE_CHAT_ENABLED="${DEVELOPER_TEAM_GOOGLE_CHAT_ENABLED:-false}"
export DEVELOPER_TEAM_SLACK_ENABLED="${DEVELOPER_TEAM_SLACK_ENABLED:-false}"

# If a child tier does enable chat, it needs its own closed allowlist (06 §1.2 V-7).
# render_allowlist_block emits nothing when the integration is off and fails the
# run when it is on with an empty or all-blank list — previously these templates
# carried no allowedUsers key at all, so enabling chat produced a CR the API
# server rejected with a message that pointed at the CRD rather than at vars.sh.
export CLUSTER_ADMIN_ALLOWED_USERS_BLOCK
CLUSTER_ADMIN_ALLOWED_USERS_BLOCK="$(render_allowlist_block "${CLUSTER_ADMIN_GOOGLE_CHAT_ENABLED}" "${CLUSTER_ADMIN_ALLOWED_USERS:-}" "cluster-admin Google Chat (CLUSTER_ADMIN_ALLOWED_USERS)")"
export CLUSTER_ADMIN_SLACK_ALLOWED_USERS_BLOCK
CLUSTER_ADMIN_SLACK_ALLOWED_USERS_BLOCK="$(render_allowlist_block "${CLUSTER_ADMIN_SLACK_ENABLED}" "${CLUSTER_ADMIN_SLACK_ALLOWED_USERS:-}" "cluster-admin Slack (CLUSTER_ADMIN_SLACK_ALLOWED_USERS)")"
export DEVELOPER_TEAM_ALLOWED_USERS_BLOCK
DEVELOPER_TEAM_ALLOWED_USERS_BLOCK="$(render_allowlist_block "${DEVELOPER_TEAM_GOOGLE_CHAT_ENABLED}" "${DEVELOPER_TEAM_ALLOWED_USERS:-}" "developer-team Google Chat (DEVELOPER_TEAM_ALLOWED_USERS)")"
export DEVELOPER_TEAM_SLACK_ALLOWED_USERS_BLOCK
DEVELOPER_TEAM_SLACK_ALLOWED_USERS_BLOCK="$(render_allowlist_block "${DEVELOPER_TEAM_SLACK_ENABLED}" "${DEVELOPER_TEAM_SLACK_ALLOWED_USERS:-}" "developer-team Slack (DEVELOPER_TEAM_SLACK_ALLOWED_USERS)")"

TIER_VARS='$NAMESPACE $PROJECT_ID $REGION $CLUSTER_NAME
$CLUSTER_ADMIN_KSA_NAME $CLUSTER_ADMIN_GSA_NAME $CLUSTER_ADMIN_AGENT_NAME
$CLUSTER_ADMIN_IMAGE $CLUSTER_ADMIN_TAG $CLUSTER_ADMIN_GOOGLE_CHAT_ENABLED $CLUSTER_ADMIN_SLACK_ENABLED
$CLUSTER_ADMIN_ALLOWED_USERS_BLOCK $CLUSTER_ADMIN_SLACK_ALLOWED_USERS_BLOCK
$DEVELOPER_TEAM_NAMESPACE $DEVELOPER_TEAM_KSA_NAME $DEVELOPER_TEAM_GSA_NAME $DEVELOPER_TEAM_AGENT_NAME
$DEVELOPER_TEAM_IMAGE $DEVELOPER_TEAM_TAG $DEVELOPER_TEAM_GOOGLE_CHAT_ENABLED $DEVELOPER_TEAM_SLACK_ENABLED
$DEVELOPER_TEAM_ALLOWED_USERS_BLOCK $DEVELOPER_TEAM_SLACK_ALLOWED_USERS_BLOCK'

# Each agent's Hermes API server reads its key from its own Secret.
ensure_api_secret() {
  local name="$1" ns="$2"
  if kubectl get secret "${name}" -n "${ns}" >/dev/null 2>&1; then
    return 0
  fi
  print_info "Creating Secret ${name} in ${ns}..."
  kubectl create secret generic "${name}" \
      --namespace="${ns}" \
      --from-literal=API_SERVER_KEY="$(openssl rand -hex 16)" \
      --dry-run=client -o yaml | kubectl apply -f -
}

# ─── Step 1: kubectl ──────────────────────────────────────────────────────────
verify_kubeconfig() {
  local ctx; ctx=$(kubectl config current-context 2>/dev/null || echo "")
  [[ "$ctx" == *"${PROJECT_ID}"* && "$ctx" == *"${CLUSTER_NAME}"* ]] && \
  kubectl get namespace "$NAMESPACE" >/dev/null 2>&1
}
execute_kubeconfig() { connect_cluster; }

# ─── Step 2: cluster-admin tier ───────────────────────────────────────────────
verify_cluster_admin() {
  [ "${CLUSTER_ADMIN_ENABLED}" != "true" ] && return 0
  kubectl get agent "${CLUSTER_ADMIN_AGENT_NAME}" -n "${NAMESPACE}" >/dev/null 2>&1
}
execute_cluster_admin() {
  if [ "${CLUSTER_ADMIN_ENABLED}" != "true" ]; then
    print_info "CLUSTER_ADMIN_ENABLED is not true. Skipping cluster-admin tier."
    return 0
  fi
  if ! kubectl get agent platform-agent -n "${NAMESPACE}" >/dev/null 2>&1; then
    print_error "platform-agent not found in ${NAMESPACE}. Run provision_08 first — the webhook"
    print_error "rejects a cluster-admin Agent whose parentRef does not resolve."
    return 1
  fi

  ensure_api_secret "${CLUSTER_ADMIN_KSA_NAME}-secrets" "${NAMESPACE}" || return 1

  print_info "Rendering and applying cluster-admin tier (image ${CLUSTER_ADMIN_IMAGE}:${CLUSTER_ADMIN_TAG})..."
  envsubst "${TIER_VARS}" < "${SCRIPT_DIR}/cluster-admin-agent.yaml.template" \
    | kubectl apply -f - || return 1

  wait_for_k8s_resource "deployment/${CLUSTER_ADMIN_AGENT_NAME}-gateway" "${NAMESPACE}" "Available" "300s" || return 1
}

# ─── Step 3: developer-team tier ──────────────────────────────────────────────
verify_developer_team() {
  [ -z "${DEVELOPER_TEAM_NAMESPACE}" ] && return 0
  kubectl get agent "${DEVELOPER_TEAM_AGENT_NAME}" -n "${DEVELOPER_TEAM_NAMESPACE}" >/dev/null 2>&1
}
execute_developer_team() {
  if [ -z "${DEVELOPER_TEAM_NAMESPACE}" ]; then
    print_info "DEVELOPER_TEAM_NAMESPACE is empty. Skipping developer-team tier."
    return 0
  fi
  if [ "${CLUSTER_ADMIN_ENABLED}" != "true" ]; then
    print_info "developer-team requires the cluster-admin tier as its parent. Skipping."
    return 0
  fi

  # The Namespace is in the template, but the Secret must exist before the pod starts.
  kubectl create namespace "${DEVELOPER_TEAM_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  ensure_api_secret "${DEVELOPER_TEAM_KSA_NAME}-secrets" "${DEVELOPER_TEAM_NAMESPACE}" || return 1
  apply_tenant_quota "${DEVELOPER_TEAM_NAMESPACE}" || return 1
  # Before the CR, not after: the controller renders this pod's model endpoint as
  # litellm.<its own namespace>.svc, which does not exist in a tenant namespace until these aliases
  # do. Applied afterwards, the pod's first inference call fails on NXDOMAIN while the readiness
  # wait below counts down against an error that never mentions DNS.
  apply_tenant_service_aliases "${DEVELOPER_TEAM_NAMESPACE}" || return 1

  print_info "Rendering and applying developer-team tier (image ${DEVELOPER_TEAM_IMAGE}:${DEVELOPER_TEAM_TAG})..."
  envsubst "${TIER_VARS}" < "${SCRIPT_DIR}/developer-team-agent.yaml.template" \
    | kubectl apply -f - || return 1

  wait_for_k8s_resource "deployment/${DEVELOPER_TEAM_AGENT_NAME}-gateway" "${DEVELOPER_TEAM_NAMESPACE}" "Available" "300s" || return 1
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Connect kubectl" verify_kubeconfig execute_kubeconfig 0
run_deploy_step "2. Deploy Cluster Admin Agent tier" verify_cluster_admin execute_cluster_admin 5
run_deploy_step "3. Deploy Developer Team Agent tier" verify_developer_team execute_developer_team 5

echo -e "\n${C_GREEN}${C_BOLD}✓ Agent tiers deployed successfully!${C_RESET}"
