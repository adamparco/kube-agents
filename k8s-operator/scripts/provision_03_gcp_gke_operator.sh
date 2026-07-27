#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 2: Deploy Kubernetes Operator (CRDs & Controller Manager)
# ==============================================================================
# Idempotent script that installs the CRDs and deploys the operator to the cluster.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$SCRIPT_DIR" == */scripts ]]; then
  OPERATOR_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
else
  OPERATOR_DIR="${SCRIPT_DIR}"
fi
VARS_FILE="${SCRIPT_DIR}/vars.sh"

source "${SCRIPT_DIR}/common.sh" "$@"

# ─── Prerequisites Check ──────────────────────────────────────────────────────
print_step "Checking Local Prerequisites"
check_prereqs "gcloud" "kubectl" "make"

# ─── Configuration & State Restoration ────────────────────────────────────────
print_step "Setting up Configuration State for Operator Deployment"
load_state

ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
DEFAULT_PROJECT_ID="${ACTIVE_PROJECT:-$(whoami 2>/dev/null || echo "user")}"

init_var "PROJECT_ID" "$DEFAULT_PROJECT_ID" "Enter Target GCP Project ID"
init_var "REGION" "us-east4" "Enter GKE GCP Region"
init_var "CLUSTER_NAME" "platform-agent-host" "Enter GKE Cluster Name"

# The cluster the `make` targets below write to, named rather than inherited (LSN-018: a context
# override the Makefile did not read was accepted and ignored, and the CRD went to whatever
# `kubectl config current-context` was). Both `connect_cluster` and CI's get-gke-credentials write
# exactly this context name; set KUBE_CONTEXT to point the run somewhere else.
KUBE_CONTEXT="${KUBE_CONTEXT:-gke_${PROJECT_ID}_${REGION}_${CLUSTER_NAME}}"

# ─── Step Implementations ─────────────────────────────────────────────────────

# Step 1: Connect kubectl
verify_kubeconfig() {
  local current_ctx
  current_ctx=$(kubectl config current-context 2>/dev/null || echo "")
  [[ "$current_ctx" == *"${PROJECT_ID}"* && "$current_ctx" == *"${CLUSTER_NAME}"* ]] && \
  (kubectl get ns "${NAMESPACE}" >/dev/null 2>&1 || kubectl get ns default >/dev/null 2>&1)
}
execute_kubeconfig() {
  connect_cluster
}

# Step 2: Ensure cert-manager is installed
verify_cert_manager() {
  kubectl get crd certificates.cert-manager.io >/dev/null 2>&1
}
execute_cert_manager() {
  print_info "cert-manager not found. Installing cert-manager..."
  
  # Check if the cluster is a GKE Autopilot cluster
  local is_autopilot
  is_autopilot=$(kubectl get nodes -o jsonpath='{.items[*].spec.providerID}' 2>/dev/null | grep -q "gce://.*/gk3-" && echo "true" || echo "false")

  if [ "$is_autopilot" = "true" ]; then
    print_info "GKE Autopilot cluster detected. Deploying cert-manager with leader-election disabled..."
    kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.14.4/cert-manager.yaml || return 1
    
    # Wait for the deployments to be created by the API server
    ensure_k8s_resource_exists "deployment/cert-manager-cainjector" "cert-manager" || return 1
    ensure_k8s_resource_exists "deployment/cert-manager" "cert-manager" || return 1
    
    # Patch deployments to disable leader election due to Autopilot kube-system namespace restrictions
    print_info "Patching cert-manager cainjector and controller arguments..."
    kubectl patch deployment cert-manager-cainjector -n cert-manager --type='json' -p='[{"op": "replace", "path": "/spec/template/spec/containers/0/args/1", "value": "--leader-elect=false"}]' || return 1
    kubectl patch deployment cert-manager -n cert-manager --type='json' -p='[{"op": "replace", "path": "/spec/template/spec/containers/0/args/2", "value": "--leader-elect=false"}]' || return 1
  else
    print_info "Standard cluster detected. Installing standard cert-manager..."
    kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.14.4/cert-manager.yaml || return 1
  fi

  # Wait for cert-manager pods to become healthy
  wait_for_k8s_resource "deployment/cert-manager" "cert-manager" "Available" "120s" || return 1
  wait_for_k8s_resource "deployment/cert-manager-cainjector" "cert-manager" "Available" "120s" || return 1
  wait_for_k8s_resource "deployment/cert-manager-webhook" "cert-manager" "Available" "120s" || return 1
}

# Step 3: Deploy Operator (CRDs & Controller manager)
verify_operator() {
  # Always return false to ensure operator updates/re-deployments are applied
  return 1
}
execute_operator() {
  print_info "Installing Custom Resource Definitions (CRDs)..."
  make -C "$OPERATOR_DIR" install KUBE_CONTEXT="$KUBE_CONTEXT" || return 1

  # Honour OPERATOR_IMAGE / ROUTER_IMAGE from vars.sh. Without this, `make deploy` falls back to
  # the Makefile defaults (ghcr.io/gke-labs/...:v0.1.0) and silently ships the PUBLISHED images
  # even when the operator was built from local source into the project's Artifact Registry.
  local -a deploy_args=()
  if [ -n "${OPERATOR_IMAGE:-}" ]; then
    print_info "Deploying controller image ${OPERATOR_IMAGE}"
    deploy_args+=("IMG=${OPERATOR_IMAGE}")
  fi
  # `make deploy` only rewrites the controller image; the router is pinned in
  # config/router/kustomization.yaml, so repoint it here.
  if [ -n "${ROUTER_IMAGE:-}" ]; then
    print_info "Deploying kage-router image ${ROUTER_IMAGE}"
    (cd "$OPERATOR_DIR/config/router" && "$OPERATOR_DIR/bin/kustomize" edit set image "kage-router=${ROUTER_IMAGE}") || return 1
  fi

  print_info "Deploying Operator Controller Manager to the GKE cluster..."
  make -C "$OPERATOR_DIR" deploy KUBE_CONTEXT="$KUBE_CONTEXT" "${deploy_args[@]}" || return 1
  wait_for_k8s_resource "deployment/kubeagents-controller-manager" "${NAMESPACE:-kubeagents-system}" "Available" "180s" || return 1
}

# The control namespace's blast-radius bound. Runs HERE — after `make deploy` has created
# `kubeagents-system`, and before steps 08-12 put the platform agent, LiteLLM, the token minter,
# the replay proxy and the cluster-admin gateway into it — so every one of those pods is admitted
# against the quota on the step that creates it. Applied later, a mis-sized quota instead springs
# on whoever triggers the next rollout; see control-quota.yaml.template for the incident that
# taught this.
#
# Always reconciles, for the same reason step 3 does: `make deploy` can recreate the namespace, and
# a quota that exists with the WRONG numbers is the failure being prevented, not a state to skip.
verify_control_quota() {
  return 1
}
execute_control_quota() {
  apply_control_quota "${NAMESPACE:-kubeagents-system}"
}

# The Phase 5 admission layer: the read-only RBAC ceiling and the agent pod hardening rule.
# These are cluster-scoped and must exist BEFORE any Agent CR is applied (step 08), so a
# non-conforming agent pod or a write-capable tier role is rejected at admission rather than
# grandfathered in. Both match ONLY resources labelled kube-agents/tier.
verify_policy() {
  kubectl get validatingadmissionpolicy kube-agents-agent-readonly >/dev/null 2>&1 &&
    kubectl get validatingadmissionpolicy kube-agents-agent-pod-hardening >/dev/null 2>&1
}
execute_policy() {
  local policy_dir="$OPERATOR_DIR/../examples/gitops-repo/policy"
  if [ ! -d "$policy_dir" ]; then
    print_info "Policy directory not found at ${policy_dir}. Skipping."
    return 0
  fi
  print_info "Applying agent admission policies (read-only ceiling + pod hardening)..."
  kubectl apply -f "${policy_dir}/vap-agent-readonly.yaml" || return 1
  kubectl apply -f "${policy_dir}/vap-agent-pod-hardening.yaml" || return 1
}

# The kage-router is the Google CHAT front door: it drains an inbound Pub/Sub subscription and
# re-publishes to per-agent topics. config/router ships it with replicas: 1 and both env values
# EMPTY (V-CMP-003 — a REPLACE_WITH_* placeholder there used to reach a running pod and fail as a
# credentials error that never mentioned the real cause). So on a Slack-only or un-wired install it
# still crash-loops until this step runs — now with "missing required --project-id" — which is why
# this step always reconciles: park it at zero unless Chat is actually wired.
verify_router_config() {
  # Always reconcile: `make deploy` in step 3 resets replicas/env from the kustomize base.
  return 1
}
execute_router_config() {
  local ns="${NAMESPACE:-kubeagents-system}"
  if ! kubectl get deployment kubeagents-router -n "$ns" >/dev/null 2>&1; then
    print_info "kage-router not deployed. Skipping."
    return 0
  fi

  if [ "${GOOGLE_CHAT_ENABLED:-false}" != "true" ] || [ -z "${CHAT_SUB_NAME:-}" ]; then
    print_info "Google Chat disabled (or no inbound subscription). Parking kage-router at 0 replicas."
    kubectl scale deployment/kubeagents-router -n "$ns" --replicas=0 || return 1
    kubectl annotate deployment/kubeagents-router -n "$ns" \
        kube-agents/parked-reason="Google Chat disabled; no inbound subscription to drain" \
        --overwrite >/dev/null || return 1
    return 0
  fi

  print_info "Wiring kage-router to subscription ${CHAT_SUB_NAME}..."
  # Workload Identity for the router KSA (its GSA is created in provision_04 step 6).
  kubectl annotate serviceaccount "${ROUTER_KSA_NAME:-kubeagents-router}" -n "$ns" \
      iam.gke.io/gcp-service-account="${ROUTER_GSA_NAME:-kubeagents-router-gsa}@${PROJECT_ID}.iam.gserviceaccount.com" \
      --overwrite || return 1
  kubectl set env deployment/kubeagents-router -n "$ns" \
      KAGE_PROJECT_ID="${PROJECT_ID}" \
      KAGE_INBOUND_SUBSCRIPTION="${CHAT_SUB_NAME}" || return 1
  kubectl scale deployment/kubeagents-router -n "$ns" --replicas=1 || return 1
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Connect kubectl" verify_kubeconfig execute_kubeconfig 0
run_deploy_step "2. Ensure cert-manager" verify_cert_manager execute_cert_manager 5
run_deploy_step "3. Deploy Kubernetes Operator" verify_operator execute_operator 0
run_deploy_step "4. Bound the control namespace (ResourceQuota)" verify_control_quota execute_control_quota 5
run_deploy_step "5. Apply agent admission policies (VAP)" verify_policy execute_policy 5
run_deploy_step "6. Configure kage-router" verify_router_config execute_router_config 5

print_success "Kubernetes Operator deployed successfully!"
