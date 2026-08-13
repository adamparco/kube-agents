#!/usr/bin/env bash
# ==============================================================================
# 🧹 Step 9: Teardown LiteLLM Gateway
# ==============================================================================
# Idempotent script to undeploy the LiteLLM gateway.
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

# ─── Confirmation Prompt ──────────────────────────────────────────────────────
confirm_action "This will permanently undeploy the LiteLLM Gateway." \
  "GCP Project:$PROJECT_ID" \
  "GKE Cluster:$CLUSTER_NAME" \
  "Namespace:$NAMESPACE"

gcloud config set project "$PROJECT_ID" --quiet

# ─── Step 1: Connect to GKE Cluster ───────────────────────────────────────────
CLUSTER_EXISTS=$(cluster_exists)
if [ -n "$CLUSTER_EXISTS" ]; then
  connect_cluster || true
else
  echo -e "  ${C_GREEN}✓ GKE cluster '${CLUSTER_NAME}' does not exist. Skipping LiteLLM Gateway cleanup.${C_RESET}"
  exit 0
fi


# ─── Step 2: Undeploy LiteLLM Gateway ─────────────────────────────────────────
echo -e "  ${C_CYAN}ℹ Undeploying LiteLLM Gateway...${C_RESET}"
if [ "${DRY_RUN:-0}" -eq 1 ]; then
  echo -e "  ${C_GREEN}[DRY-RUN] Would undeploy LiteLLM Gateway in namespace '${NAMESPACE}'.${C_RESET}"
else
  export NAMESPACE MODEL_PROVIDER MODEL_DEFAULT_NAME
  # undeploy-litellm renders the manifests to learn what to delete, and for
  # vertex_ai two of those names depend on these: the ServiceAccount is
  # ${LITELLM_KSA_NAME}, and the ConfigMap is named after a checksum of the
  # substituted config.yaml, which carries VERTEX_PROJECT and VERTEX_LOCATION.
  # Render with the wrong values and the delete asks for a ConfigMap that was
  # never created while the real one stays behind.
  #
  # ensure_teardown_state hard-sets LITELLM_KSA_NAME and LITELLM_GSA_NAME to the
  # DEFAULT_* constants in both of its branches, so those two are always right
  # for an install that did not rename them. The other three are only as good as
  # the state it found: PROJECT_ID comes from vars.sh on the file branch, and
  # VERTEX_PROJECT/VERTEX_LOCATION fall back to empty and DEFAULT_VERTEX_LOCATION
  # respectively. On a vertex_ai install torn down without a vars.sh — or with
  # one written before these variables existed — the checksum will not match the
  # deployed ConfigMap's and that ConfigMap survives the teardown. Deleting it by
  # name is the fix: `kubectl get configmap -n "$NAMESPACE" -l
  # app.kubernetes.io/name=litellm` lists every generation. The Makefile's own
  # fallbacks do not help here; they exist to keep the rendered stream parseable
  # so the delete reaches the other five objects, not to reconstruct a checksum.
  export PROJECT_ID LITELLM_KSA_NAME LITELLM_GSA_NAME VERTEX_PROJECT VERTEX_LOCATION
  make -C "${OPERATOR_DIR}" undeploy-litellm || true
  echo -e "  ${C_GREEN}✓ LiteLLM Gateway undeploy command completed.${C_RESET}"
fi

echo -e "\n${C_GREEN}${C_BOLD}✅ LiteLLM Gateway successfully undeployed!${C_RESET}"
