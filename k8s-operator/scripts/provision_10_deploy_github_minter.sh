#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 8: Deploy GitHub Token Minter
# ==============================================================================
# Idempotent script that deploys the GitHub Token Minter.
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
init_var "REGION" "us-east4" "Enter GKE GCP Region"
init_var "CLUSTER_NAME" "platform-agent-host" "Enter GKE Cluster Name"
init_var "KMS_KEYRING" "github-token-minter-keyring" "Enter Cloud KMS Keyring Name"
init_var "KMS_KEY" "github-token-minter-key" "Enter Cloud KMS Key Name"

export GOOGLE_CLOUD_QUOTA_PROJECT="${PROJECT_ID}"

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

# Step 2: Enable KMS API
verify_kms_api() {
  local out=$(gcloud services list --enabled --project="$PROJECT_ID" --format="value(config.name)" 2>/dev/null || echo "")
  echo "$out" | grep -q 'cloudkms.googleapis.com'
}

execute_kms_api() {
  print_info "Enabling Cloud KMS API..."
  gcloud services enable \
      cloudkms.googleapis.com \
      --project="$PROJECT_ID"
}

# Step 3: Deploy GitHub Token Minter
verify_github_minter() {
  if [ -z "${GITHUB_ORG:-}" ] || [ -z "${GITHUB_REPO:-}" ] || [ -z "${GITHUB_APP_ID:-}" ]; then
    print_info "GitHub integration not configured. Skipping Minter deployment."
    return 0
  fi

  # Always return false to ensure configuration updates (like KMS key changes)
  # are applied to the Deployment workloads.
  return 1
}

execute_github_minter() {
  if [ -z "${GITHUB_ORG:-}" ] || [ -z "${GITHUB_REPO:-}" ] || [ -z "${GITHUB_APP_ID:-}" ]; then
    return 0
  fi

  # Ensure KMS Keyring and Key exist.
  print_info "Ensuring KMS Keyring '${KMS_KEYRING}' exists..."
  if ! gcloud kms keyrings describe "${KMS_KEYRING}" --location="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud kms keyrings create "${KMS_KEYRING}" --location="${REGION}" --project="${PROJECT_ID}" || return 1
  fi

  print_info "Ensuring KMS Key '${KMS_KEY}' exists..."
  if ! gcloud kms keys describe "${KMS_KEY}" --location="${REGION}" --keyring="${KMS_KEYRING}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud kms keys create "${KMS_KEY}" \
        --location="${REGION}" \
        --keyring="${KMS_KEYRING}" \
        --purpose=asymmetric-signing \
        --default-algorithm=rsa-sign-pkcs1-2048-sha256 \
        --import-only \
        --skip-initial-version-creation \
        --project="${PROJECT_ID}" || return 1
  fi

  # Ensure the Minter GSA has signer permissions on the KMS key.
  local gsa_email="${GITHUB_MINTER_GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
  print_info "Ensuring GSA has signer permissions on KMS key..."
  gcloud kms keys add-iam-policy-binding "${KMS_KEY}" \
      --location="${REGION}" \
      --keyring="${KMS_KEYRING}" \
      --member="serviceAccount:${gsa_email}" \
      --role="roles/cloudkms.signerVerifier" \
      --project="${PROJECT_ID}" \
      --condition=None \
      --quiet >/dev/null || return 1

  # Import PEM if provided and no version exists
  local versions=$(gcloud kms keys versions list --key="${KMS_KEY}" --keyring="${KMS_KEYRING}" --location="${REGION}" --project="${PROJECT_ID}" --filter="state=ENABLED" --format="value(name)" 2>/dev/null)
  if [ -z "$versions" ]; then
    if [ -n "${GITHUB_PEM_PATH}" ] && [ -f "${GITHUB_PEM_PATH}" ]; then
      # Import with openssl + gcloud only. `gcloud kms keys versions import` performs the
      # RSA-OAEP/AES wrapping against the import job's public key client-side, so the only
      # gap is GitHub issuing PKCS#1 while KMS requires PKCS#8 — one openssl call. This
      # replaces cloning and `go run`-ing the upstream Minty CLI: no Go toolchain, no network
      # clone, and no third-party code executed against the private key.
      if ! command -v openssl &>/dev/null; then
        print_warning "openssl is required to convert the GitHub private key to PKCS#8."
        print_warning "Skipping automatic import. You must import the key manually later."
      else
        print_info "Importing GitHub Private Key PEM into KMS..."

        local import_job="${KMS_IMPORT_JOB:-kage-minty-import-job}"
        if ! gcloud kms import-jobs describe "${import_job}" --location="${REGION}" \
              --keyring="${KMS_KEYRING}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
          print_info "Creating KMS import job '${import_job}'..."
          gcloud kms import-jobs create "${import_job}" \
              --location="${REGION}" \
              --keyring="${KMS_KEYRING}" \
              --import-method=rsa-oaep-3072-sha256-aes-256 \
              --protection-level=software \
              --project="${PROJECT_ID}" >/dev/null 2>&1 || true
        fi

        # A freshly created import job generates its wrapping key asynchronously.
        local job_state=""
        for _ in $(seq 1 30); do
          job_state=$(gcloud kms import-jobs describe "${import_job}" --location="${REGION}" \
              --keyring="${KMS_KEYRING}" --project="${PROJECT_ID}" --format='value(state)' 2>/dev/null || echo "")
          [ "$job_state" = "ACTIVE" ] && break
          sleep 10
        done

        if [ "$job_state" != "ACTIVE" ]; then
          print_error "KMS import job '${import_job}' never became ACTIVE (state=${job_state:-unknown})."
        else
          local tmp_dir; tmp_dir=$(mktemp -d); chmod 700 "$tmp_dir"
          if openssl pkcs8 -topk8 -inform PEM -outform DER -nocrypt \
               -in "${GITHUB_PEM_PATH}" -out "${tmp_dir}/key.pkcs8.der" 2>/dev/null &&
             gcloud kms keys versions import \
                --project="${PROJECT_ID}" \
                --location="${REGION}" \
                --keyring="${KMS_KEYRING}" \
                --key="${KMS_KEY}" \
                --import-job="${import_job}" \
                --algorithm=rsa-sign-pkcs1-2048-sha256 \
                --target-key-file="${tmp_dir}/key.pkcs8.der" >/dev/null 2>&1; then
            rm -rf "$tmp_dir"
            print_success "Successfully imported GitHub Private Key into KMS."
          else
            rm -rf "$tmp_dir"
            print_error "Failed to import GitHub Private Key to KMS. You must import it manually."
          fi
        fi
      fi
    else
      print_warning "No GitHub Private Key PEM path provided or file not found."
      print_warning "KMS Key '${KMS_KEY}' has no active version. Minter will fail to start until you import the key."
      print_warning "You can import it later manually using Minty CLI:"
      print_warning "  openssl pkcs8 -topk8 -inform PEM -outform DER -nocrypt -in /path/to/pem -out /tmp/key.der && \\"
      print_warning "  gcloud kms keys versions import --project=${PROJECT_ID} --location=${REGION} --keyring=${KMS_KEYRING} --key=${KMS_KEY} --import-job=${KMS_IMPORT_JOB:-kage-minty-import-job} --algorithm=rsa-sign-pkcs1-2048-sha256 --target-key-file=/tmp/key.der"
    fi
  fi

  # Resolve the latest active (ENABLED) version number dynamically
  print_info "Resolving active KMS key version number..."
  local active_version
  active_version=$(gcloud kms keys versions list --key="${KMS_KEY}" --keyring="${KMS_KEYRING}" --location="${REGION}" --project="${PROJECT_ID}" --filter="state=ENABLED" --format="value(name)" 2>/dev/null | awk -F'/' '{print $NF}' | sort -n | tail -n 1)
  
  if [ -n "$active_version" ]; then
    export KMS_KEY_VERSION="${active_version}"
    print_success "Resolved active KMS key version: ${KMS_KEY_VERSION}"
  else
    print_warning "No active (ENABLED) version found for KMS Key '${KMS_KEY}'."
    print_warning "Defaulting KMS_KEY_VERSION to '1'. The Token Minter deployment will fail its readiness probes until a key is imported."
    export KMS_KEY_VERSION="1"
  fi

  print_info "Deploying GitHub Token Minter workloads..."
  local GITHUB_INTEGRATION_DIR="${OPERATOR_DIR}/config/integrations/github"
  
  if [ -d "$GITHUB_INTEGRATION_DIR" ]; then
    # Ensure all variables are exported for envsubst
    export PROJECT_ID REGION CLUSTER_NAME NAMESPACE GITHUB_MINTER_KSA_NAME GITHUB_MINTER_GSA_NAME KMS_KEYRING KMS_KEY KMS_KEY_VERSION GITHUB_ORG GITHUB_REPO KSA_NAME PLATFORM_AGENT_GSA_NAME
    make -C "${OPERATOR_DIR}" deploy-github || return 1
  else
    print_error "GitHub integration directory not found at ${GITHUB_INTEGRATION_DIR}"
    return 1
  fi

  # Minty signs its GitHub App JWT with the KMS key on every request, so a key with no
  # ENABLED version fails the readiness probe rather than erroring at deploy time. Surface
  # that here instead of leaving a silently unready Service the agent cannot resolve.
  if ! wait_for_k8s_resource "deployment/github-token-minter" "${NAMESPACE}" "Available" "180s"; then
    print_error "github-token-minter did not become Available."
    print_error "Most common cause: KMS key '${KMS_KEY}' has no ENABLED version (the GitHub App"
    print_error "private key was never imported). Check: kubectl logs -n ${NAMESPACE} deploy/github-token-minter"
    return 1
  fi
}

# Preflight the GitHub side. Neither check can be done from inside the cluster, and both
# produce confusing downstream failures: an uninitialised repo has no default branch, so the
# agent's PR (its only sanctioned write path) cannot be opened at all.
verify_github_preflight() {
  # Advisory only — never block provisioning on a missing local gh CLI.
  return 0
}
execute_github_preflight() {
  if [ -z "${GITHUB_ORG:-}" ] || [ -z "${GITHUB_REPO:-}" ]; then
    return 0
  fi
  if ! command -v gh >/dev/null 2>&1; then
    print_info "gh CLI not found; skipping GitHub-side preflight."
    return 0
  fi

  local slug="${GITHUB_ORG}/${GITHUB_REPO}"
  if ! gh repo view "$slug" >/dev/null 2>&1; then
    print_warning "Repository ${slug} is not reachable with the current gh credentials."
    return 0
  fi

  local branches
  branches=$(gh api "repos/${slug}/branches" --jq 'length' 2>/dev/null || echo "0")
  if [ "${branches:-0}" -eq 0 ]; then
    print_warning "Repository ${slug} has no commits (no default branch)."
    print_warning "Agents cannot open pull requests against an empty repository."
    print_warning "Seed it first, e.g.: gh api repos/${slug}/contents/README.md -X PUT \\"
    print_warning "  -f message='chore: initialize' -f content=\"\$(printf '# %s' '${GITHUB_REPO}' | base64)\""
  else
    print_success "Repository ${slug} is initialized (${branches} branch(es))."
  fi
}


# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Connect kubectl" verify_kubeconfig execute_kubeconfig 0
run_step "2. Enable Cloud KMS API" verify_kms_api execute_kms_api 0
run_step "3. Preflight GitHub repository" verify_github_preflight execute_github_preflight 0
run_step "4. Deploy GitHub Token Minter" verify_github_minter execute_github_minter 10

# ─── Conclusion Checklist ─────────────────────────────────────────────────────
echo -e "\n${C_GREEN}${C_BOLD}✓ GitHub Token Minter deployed successfully to GKE!${C_RESET}"
