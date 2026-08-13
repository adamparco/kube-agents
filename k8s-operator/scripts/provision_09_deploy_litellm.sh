#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 9: Deploy LiteLLM Gateway
# ==============================================================================
# Idempotent script that connects to GKE and deploys the LiteLLM Gateway.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$SCRIPT_DIR" == */scripts ]]; then
  OPERATOR_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
else
  OPERATOR_DIR="${SCRIPT_DIR}"
fi
VARS_FILE="${SCRIPT_DIR}/vars.sh"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
source "${SCRIPT_DIR}/common.sh" "$@"

# ─── Prerequisites Check ──────────────────────────────────────────────────────
print_step "Checking Local Prerequisites"
check_prereqs "gcloud" "kubectl" "envsubst"

# ─── Configuration & State Restoration ────────────────────────────────────────
print_step "Setting up Configuration State for Agent Deployment"
load_state

ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
DEFAULT_PROJECT_ID="${ACTIVE_PROJECT:-$(whoami 2>/dev/null || echo "user")}"

init_var "PROJECT_ID" "$DEFAULT_PROJECT_ID" "Enter Target GCP Project ID"
init_var "REGION" "$DEFAULT_REGION" "Enter GKE GCP Region"
init_var "CLUSTER_NAME" "$DEFAULT_CLUSTER_NAME" "Enter GKE Cluster Name"
init_var_model_provider


# ─── Step Implementations ─────────────────────────────────────────────────────

# Step 1: Connect kubectl
verify_kubeconfig() {
  local current_ctx
  current_ctx=$(kubectl config current-context 2>/dev/null || echo "")
  [[ "$current_ctx" == *"${PROJECT_ID}"* && "$current_ctx" == *"${CLUSTER_NAME}"* ]] && \
  kubectl get namespace "$NAMESPACE" >/dev/null 2>&1
}
execute_kubeconfig() {
  connect_cluster
}

# Step 2: Deploy LiteLLM Gateway
verify_litellm() {
  # Always return false to ensure that Kustomize builds and configs are applied idempotently on every run
  return 1
}
# vertex_ai is the one provider whose gateway can come up perfectly and still
# serve nothing. Without the GSA and Workload Identity binding from
# provision_04_gcp_iam.sh the pods still start, still pass their probes, and
# still report a successful rollout — every completion then fails as a 403 that
# surfaces only in the agent's logs. The CI redeploy workflow
# (.github/workflows/reusable-deploy-integrations.yml) runs steps 07 and 09 but
# not 04, so the check is here rather than left to the operator.
#
# Only a missing GSA or a missing Workload Identity binding blocks the deploy.
# Both are facts about objects in PROJECT_ID. Everything else the check can
# report — an aiplatform grant that may be inherited, a policy this caller may
# not read — is a warning, because main's step 09 made no gcloud IAM call at
# all and a deploy service account without iam.serviceAccounts.get must not
# start failing on a permission this step never used to need.
preflight_vertex_iam() {
  [ "${MODEL_PROVIDER:-}" = "vertex_ai" ] || return 0
  if skip_vertex_iam_setup; then
    print_warning "SKIP_VERTEX_IAM_SETUP=true; deploying without checking the gateway's Workload Identity setup."
    return 0
  fi

  local state=0
  verify_litellm_vertex_iam_state || state=$?
  case "$state" in
    0) return 0 ;;
    1)
      print_error "MODEL_PROVIDER=vertex_ai but ${LITELLM_VERTEX_IAM_REASON}."
      print_error "Run provision_04_gcp_iam.sh first — it creates the GSA, grants roles/aiplatform.user on VERTEX_PROJECT_ID, and binds Workload Identity."
      print_error "Set SKIP_VERTEX_IAM_SETUP=true if this install's Vertex IAM is managed outside the pipeline."
      return 1
      ;;
    *)
      print_warning "Could not confirm the gateway's Vertex AI access: ${LITELLM_VERTEX_IAM_REASON}."
      print_warning "Deploying anyway. If completions come back 403, run provision_04_gcp_iam.sh or check that grant by hand."
      return 0
      ;;
  esac
}

execute_litellm() {
  preflight_vertex_iam || return 1
  print_info "Deploying LiteLLM Gateway into GKE..."
  export NAMESPACE MODEL_PROVIDER MODEL_DEFAULT_NAME
  # Only the vertex_ai overlay reads these; exporting them unconditionally keeps
  # the branch out of the recipe, and envsubst never sees them for the other
  # providers because their allowlist does not name them. No fallback defaults
  # here: init_var_vertex_ai has already settled both Vertex values, and the
  # make target's LITELLM_REQUIRE_VERTEX_VARS refuses an empty one loudly
  # rather than silently substituting the cluster's own project or region.
  export PROJECT_ID VERTEX_PROJECT_ID VERTEX_LOCATION LITELLM_KSA_NAME LITELLM_GSA_NAME
  make -C "${OPERATOR_DIR}" deploy-litellm || return 1
}


# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Connect kubectl" verify_kubeconfig execute_kubeconfig 0
run_step "2. Deploy LiteLLM Gateway" verify_litellm execute_litellm 0

# ─── Conclusion Checklist ─────────────────────────────────────────────────────
echo -e "\n${C_GREEN}${C_BOLD}✓ LiteLLM Gateway deployed successfully to GKE!${C_RESET}"
