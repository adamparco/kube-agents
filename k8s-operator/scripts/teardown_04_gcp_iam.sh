#!/usr/bin/env bash
# ==============================================================================
# 🧹 Step 4: Teardown Controller & Agent GCP Workload Identity & GCP IAM
# ==============================================================================
# Idempotent script to remove cluster management and Workload Identity bindings
# from the Controller manager and all Agent GSAs, and delete the GSAs.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VARS_FILE="${SCRIPT_DIR}/vars.sh"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
source "${SCRIPT_DIR}/common.sh" "$@"

# ─── Configuration State Restoration ──────────────────────────────────────────
ensure_teardown_state

# ─── Confirmation Prompt ──────────────────────────────────────────────────────
# The prompt has to list what this run will actually touch, and PROJECT_ID is
# not the whole answer: the LiteLLM gateway's roles/aiplatform.user binding
# lives on VERTEX_PROJECT, which for the shared-serving-project arrangement this
# feature exists for is a project the install does not own. Naming it is the
# only warning an operator gets before the policy there is edited. Conversely,
# SKIP_VERTEX_IAM_SETUP removes the whole LiteLLM arm below, so under that flag
# the prompt must not advertise a GSA that will still be standing afterwards.
confirm_message="This will remove GSA permissions, Workload Identity bindings, and delete GSAs for the Controller and Platform Agent."
confirm_items=(
  "GCP Project:$PROJECT_ID"
  "Controller GSA:$CONTROLLER_GSA_NAME"
  "Platform Agent GSA:$PLATFORM_AGENT_GSA_NAME"
)
if ! skip_vertex_iam_setup; then
  confirm_message="This will remove GSA permissions, Workload Identity bindings, and delete GSAs for the Controller, Platform Agent, and LiteLLM gateway."
  confirm_items+=("LiteLLM GSA:$LITELLM_GSA_NAME")
  if [ -n "${VERTEX_PROJECT:-}" ] && [ "${VERTEX_PROJECT}" != "${PROJECT_ID}" ]; then
    confirm_message="${confirm_message} It also removes the LiteLLM gateway's roles/aiplatform.user binding from a SECOND project, ${VERTEX_PROJECT}."
    confirm_items+=("Vertex Project:$VERTEX_PROJECT")
  fi
fi
confirm_action "$confirm_message" "${confirm_items[@]}"

gcloud config set project "$PROJECT_ID" --quiet

# ─── Helper Functions for Teardown ────────────────────────────────────────────
cleanup_agent_iam() {
  local ksa_name=$1
  local gsa_name=$2
  shift 2
  local roles=("$@")
  
  local gsa_email="${gsa_name}@${PROJECT_ID}.iam.gserviceaccount.com"
  
  local gsa_exists=0
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    gsa_exists=1
  elif gcloud iam service-accounts describe "${gsa_email}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gsa_exists=1
  fi

  if [ "$gsa_exists" -eq 1 ]; then
    # bash 3.2, which is what macOS ships, treats "${arr[@]}" on an empty array
    # as an unbound variable under `set -u`. This function is called with no
    # roles for the minter GSA, so guard the expansion the way provision_03 does.
    if [ ${#roles[@]} -gt 0 ]; then
      echo -e "  ${C_CYAN}ℹ Removing project-level IAM policy bindings for ${gsa_name}...${C_RESET}"
      for role in "${roles[@]}"; do
        if [ "${DRY_RUN:-0}" -eq 1 ]; then
          echo -e "  ${C_GREEN}[DRY-RUN] Would remove project-level IAM policy binding '${role}' for ${gsa_name}.${C_RESET}"
        else
          gcloud projects remove-iam-policy-binding "${PROJECT_ID}" \
              --member="serviceAccount:${gsa_email}" \
              --role="${role}" \
              --quiet 2>/dev/null || true
        fi
      done
    fi

    echo -e "  ${C_CYAN}ℹ Removing Workload Identity Policy Binding for ${gsa_name}...${C_RESET}"
    local wi_member="serviceAccount:${PROJECT_ID}.svc.id.goog[${NAMESPACE}/${ksa_name}]"
    if [ "${DRY_RUN:-0}" -eq 1 ]; then
      echo -e "  ${C_GREEN}[DRY-RUN] Would remove Workload Identity binding for ${gsa_name} to ${ksa_name}.${C_RESET}"
    else
      gcloud iam service-accounts remove-iam-policy-binding "${gsa_email}" \
          --role="roles/iam.workloadIdentityUser" \
          --member="${wi_member}" \
          --project="${PROJECT_ID}" \
          --quiet 2>/dev/null || true
    fi

    echo -e "  ${C_CYAN}ℹ Deleting GSA ${gsa_name}...${C_RESET}"
    if [ "${DRY_RUN:-0}" -eq 1 ]; then
      echo -e "  ${C_GREEN}[DRY-RUN] Would delete GSA ${gsa_name}.${C_RESET}"
    else
      gcloud iam service-accounts delete "${gsa_email}" --project="${PROJECT_ID}" --quiet || true
      echo -e "  ${C_GREEN}✓ GSA '${gsa_name}' successfully removed.${C_RESET}"
    fi
  else
    echo -e "  ${C_GREEN}✓ GSA '${gsa_name}' does not exist. Skipping cleanup.${C_RESET}"
  fi
}

# The LiteLLM gateway's grant is the one binding this pipeline makes outside
# PROJECT_ID: provision_04 grants roles/aiplatform.user on VERTEX_PROJECT, which
# on a shared serving project is a project this install does not own and will
# not be deleting. cleanup_agent_iam only ever touches PROJECT_ID, so the
# cross-project binding has to be removed here — and before the GSA is deleted,
# or it is left behind as a dangling deleted:serviceAccount member.
cleanup_litellm_vertex_grant() {
  local gsa_email="${LITELLM_GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

  # An empty VERTEX_PROJECT means one of two different things: a gemini install
  # that never had this grant, or a teardown running without a vars.sh to read
  # the project out of (ensure_teardown_state leaves it empty in that branch).
  # The GSA tells them apart — only a vertex_ai install created it — and in the
  # second case the grant is on a project this teardown can no longer name, so
  # say so rather than returning a silent success.
  if [ -z "${VERTEX_PROJECT:-}" ]; then
    if [ "${DRY_RUN:-0}" -eq 0 ] &&
       gcloud iam service-accounts describe "${gsa_email}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
      print_warning "VERTEX_PROJECT is unknown but ${gsa_email} exists. If its roles/aiplatform.user grant is on a different project, remove it by hand — this teardown cannot name that project."
    fi
    return 0
  fi

  # Same project: cleanup_agent_iam's own role loop removes it.
  [ "${VERTEX_PROJECT}" != "${PROJECT_ID}" ] || return 0

  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    echo -e "  ${C_GREEN}[DRY-RUN] Would remove roles/aiplatform.user for ${LITELLM_GSA_NAME} on Vertex project ${VERTEX_PROJECT}.${C_RESET}"
    return 0
  fi

  echo -e "  ${C_CYAN}ℹ Removing roles/aiplatform.user for ${LITELLM_GSA_NAME} on Vertex project ${VERTEX_PROJECT}...${C_RESET}"
  local err
  if err="$(gcloud projects remove-iam-policy-binding "${VERTEX_PROJECT}" \
      --member="serviceAccount:${gsa_email}" \
      --role="roles/aiplatform.user" \
      --condition=None \
      --quiet 2>&1 >/dev/null)"; then
    echo -e "  ${C_GREEN}✓ Vertex AI grant on '${VERTEX_PROJECT}' removed.${C_RESET}"
    return 0
  fi

  # A binding that is not there — never granted, or a second teardown pass — is
  # the expected outcome, not a problem. Anything else (most often no rights to
  # edit that project's policy) leaves a live grant on a project this install
  # does not own, which is worth saying out loud. Neither fails the teardown.
  case "$err" in
    *"not found"*|*NOT_FOUND*)
      echo -e "  ${C_GREEN}✓ No roles/aiplatform.user binding for ${gsa_email} on '${VERTEX_PROJECT}'.${C_RESET}"
      ;;
    *)
      echo -e "  ${C_YELLOW}⚠ Could not remove roles/aiplatform.user for ${gsa_email} on '${VERTEX_PROJECT}': $(printf '%s\n' "$err" | head -n 1)${C_RESET}"
      echo -e "  ${C_YELLOW}⚠ Remove that binding by hand; it outlives this install.${C_RESET}"
      ;;
  esac
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────

platform_roles=(
    "roles/container.clusterAdmin"
    "roles/container.admin"
    "roles/compute.viewer"
    "roles/monitoring.admin"
    "roles/logging.admin"
    "roles/container.clusterViewer"
    "roles/container.viewer"
    "roles/monitoring.viewer"
    "roles/logging.viewer"
    "roles/iam.serviceAccountUser"
    "roles/iam.securityReviewer"
    "roles/aiplatform.user"
    "roles/mcp.toolUser"
)
if [ -n "${PLATFORM_AGENT_CUSTOM_ROLES:-}" ]; then
  custom_roles_str=""
  if declare -p PLATFORM_AGENT_CUSTOM_ROLES 2>/dev/null | grep -q 'declare -a'; then
    custom_roles_str="${PLATFORM_AGENT_CUSTOM_ROLES[*]}"
  else
    custom_roles_str="${PLATFORM_AGENT_CUSTOM_ROLES}"
  fi
  custom_roles=(${custom_roles_str//,/ })
  # Same bash 3.2 caveat: a value that word-splits to nothing leaves this empty.
  if [ ${#custom_roles[@]} -gt 0 ]; then
    platform_roles+=("${custom_roles[@]}")
  fi
fi

cleanup_agent_iam "${PLATFORM_AGENT_KSA_NAME}" "${PLATFORM_AGENT_GSA_NAME}" "${platform_roles[@]}"



# Clean up GitHub Token Minter GSA
cleanup_agent_iam "${GITHUB_MINTER_KSA_NAME}" "${GITHUB_MINTER_GSA_NAME}"

# Clean up the LiteLLM gateway GSA. Deliberately not gated on MODEL_PROVIDER:
# only a vertex_ai install ever created this GSA, but an install switched back
# to gemini afterwards still has it, still has its Workload Identity binding,
# and still has a live grant on the Vertex project. When it was never created,
# cleanup_agent_iam reports "does not exist" and does nothing. The vertex grant
# is dropped first, while the GSA is still there to name.
#
# It IS gated on SKIP_VERTEX_IAM_SETUP, which is the flag for an install whose
# Vertex IAM belongs to someone else — a platform team, or Terraform, on a
# shared serving project. provision_04 created none of these objects under that
# flag, so removing them here would delete another owner's service account and
# drop a binding in a project this install may not even be entitled to edit.
# The name is not a safeguard: common.sh hard-sets LITELLM_GSA_NAME to a
# constant, and the chart's README tells Helm users to create the GSA at exactly
# that name, so an out-of-band GSA matches what this would delete.
if skip_vertex_iam_setup; then
  print_warning "Skipping LiteLLM GSA teardown: SKIP_VERTEX_IAM_SETUP=true. ${LITELLM_GSA_NAME}, its Workload Identity binding, and any Vertex grant are left exactly as they are."
else
  cleanup_litellm_vertex_grant
  cleanup_agent_iam "${LITELLM_KSA_NAME}" "${LITELLM_GSA_NAME}" "roles/aiplatform.user"
fi

echo -e "\n${C_GREEN}${C_BOLD}✅ Controller & Agent GCP IAM configurations fully cleaned up!${C_RESET}"
