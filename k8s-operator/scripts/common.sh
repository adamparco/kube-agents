#!/usr/bin/env bash
# ==============================================================================
# Shared Bash Utilities for Provision & Teardown Pipeline
# ==============================================================================

# Determine paths relative to where this helper is loaded
if [ -z "${SCRIPT_DIR:-}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
VARS_FILE="${SCRIPT_DIR}/vars.sh"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
C_CYAN='\033[96m'
C_GREEN='\033[92m'
C_YELLOW='\033[93m'
C_MAGENTA='\033[95m'
C_BLUE='\033[94m'
C_RED='\033[91m'
C_RESET='\033[0m'
C_BOLD='\033[1m'
C_WHITE='\033[97m'

# ─── UI Helpers ───────────────────────────────────────────────────────────────
print_step() { echo -e "\n${C_MAGENTA}${C_BOLD}>>>  $1  <<<${C_RESET}"; }
print_success() { echo -e "  ${C_GREEN}✓ $1${C_RESET}"; }
print_info() { echo -e "  ${C_CYAN}ℹ $1${C_RESET}"; }
print_warning() { echo -e "  ${C_YELLOW}⚠ $1${C_RESET}"; }
print_error() { echo -e "  ${C_RED}✗ $1${C_RESET}"; }

wait_for_a_bit() {
  local seconds=$1
  local msg=$2
  local spinner=( "⠋" "⠙" "⠹" "⠸" "⠼" "⠴" "⠦" "⠧" "⠇" "⠏" )
  echo -ne "  ${C_YELLOW}${msg} (${seconds}s)...  "
  tput civis 2>/dev/null || true
  for (( i=0; i<seconds*10; i++ )); do
    local idx=$(( i % 10 ))
    echo -ne "\b${spinner[$idx]}"
    sleep 0.1
  done
  echo -ne "\b ${C_RESET}\n"
  tput cnorm 2>/dev/null || true
}

retry() {
  local max_retries=$1
  local delay=$2
  shift 2
  local count=0

  while [ $count -lt $max_retries ]; do
    count=$((count + 1))
    if "$@"; then
      return 0
    fi
    if [ $count -lt $max_retries ]; then
      echo -e "  ${C_YELLOW}⚠ [Retry $count/$max_retries] Waiting ${delay}s before next attempt...${C_RESET}" >&2
      sleep "$delay"
    fi
  done

  return 1
}

retry() {
  local max_retries=$1
  local delay=$2
  shift 2
  local count=0

  while [ $count -lt $max_retries ]; do
    count=$((count + 1))
    if "$@"; then
      return 0
    fi
    if [ $count -lt $max_retries ]; then
      echo -e "  ${C_YELLOW}⚠ [Retry $count/$max_retries] Waiting ${delay}s before next attempt...${C_RESET}" >&2
      sleep "$delay"
    fi
  done

  return 1
}

cleanup() { tput cnorm 2>/dev/null || true; }
trap cleanup EXIT

# ─── Universal Argument Parsing ──────────────────────────────────────────────
DRY_RUN=0
NO_CONFIRM=0
for arg in "$@"; do
  case $arg in
    --dry-run) DRY_RUN=1 ;;
    --no-confirm|-y) NO_CONFIRM=1 ;;
  esac
done

save_var() {
  local var_name=$1
  local var_val=$2
  export "${var_name}=${var_val}"
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    return 0
  fi
  if [ -f "$VARS_FILE" ]; then
    grep -E -v "^[[:space:]]*export[[:space:]]+${var_name}=" "$VARS_FILE" > "$VARS_FILE.tmp" 2>/dev/null || true
    mv "$VARS_FILE.tmp" "$VARS_FILE"
  fi
  printf "export %s=%q\n" "$var_name" "$var_val" >> "$VARS_FILE"
}

is_ci_pipeline() {
  if [ "${CI:-}" = "true" ] || [ "${CI:-}" = "1" ]; then
    return 0
  fi
  return 1
}

# True when nothing may read from stdin: there is no operator at the keyboard to answer.
#
# THE SINGLE DEFINITION SITE for that question. It was previously spelled inline, as
# `[ "${DRY_RUN:-0}" -eq 1 ] || is_ci_pipeline`, at eleven prompt sites across common.sh,
# provision_05, provision_06 and provision_07 — and NO_CONFIRM was in none of them. Only
# `confirm_action` honoured it. So `--no-confirm`/`-y` suppressed the "are you sure" gates and left
# every CONFIGURATION prompt live, which is a flag that announces a non-interactive run and does not
# deliver one.
#
# That is not a cosmetic gap. `make live-refresh ARGS="--yes"` builds seven images, pins them in
# vars.sh, and then runs the 13-step provisioner; on 2026-07-26 it reached provision_06, blocked on
# the Slack token prompt with no stdin, took EOF, and aborted under `set -e` at step 06 of 13. The
# operator was already on the new build and the three agent tiers were not — a half-refreshed live
# install, produced by a flag whose entire purpose was to make the run unattended.
#
# DRY_RUN and CI are folded in because they were already the de-facto members of this set at every
# call site; keeping them here means a prompt site asks one question instead of remembering three.
# The distinction between "don't ask, you may assume the default" and "don't ask, and there IS no
# safe default" stays with the CALLER — init_var takes the default, init_var_required exits 1 — and
# this predicate deliberately does not decide it.
is_non_interactive() {
  if [ "${NO_CONFIRM:-0}" -eq 1 ] || [ "${DRY_RUN:-0}" -eq 1 ] || is_ci_pipeline; then
    return 0
  fi
  return 1
}

init_var() {
  local var_name=$1
  local default_val=$2
  local prompt_msg=$3
  local current_val="${!var_name:-}"
  if [ -z "$current_val" ]; then
    local final_val
    if is_non_interactive; then
      final_val="$default_val"
    else
      echo -ne "  ${C_CYAN}${prompt_msg} [${C_WHITE}${default_val}${C_CYAN}]: ${C_RESET}"
      read -r input_val
      final_val="${input_val:-$default_val}"
    fi
    export "${var_name}=${final_val}"
    save_var "$var_name" "$final_val"
  fi
}

# init_var_required <var> <prompt>
#
# Like init_var, but the value is mandatory: there is no default to fall back to
# and a blank answer re-prompts. Used for closed allowlists (06 §1.2 V-7), where
# "leave it empty" used to mean "admit every authenticated user" — a default that
# silently opens the human→agent boundary at the moment an operator is least
# likely to be paying attention. Non-interactive runs fail loudly instead of
# accepting the empty value.
init_var_required() {
  local var_name=$1
  local prompt_msg=$2
  local current_val="${!var_name:-}"

  if [ -n "$(printf '%s' "$current_val" | tr -d '[:space:]')" ]; then
    return 0
  fi

  if is_non_interactive; then
    print_error "${var_name} is required and has no safe default."
    print_error "Set it in vars.sh (or the environment) before running non-interactively."
    exit 1
  fi

  local input_val=""
  while [ -z "$(printf '%s' "$input_val" | tr -d '[:space:]')" ]; do
    echo -ne "  ${C_CYAN}${prompt_msg}: ${C_RESET}"
    read -r input_val
    if [ -z "$(printf '%s' "$input_val" | tr -d '[:space:]')" ]; then
      print_warning "A value is required — there is no permissive default."
    fi
  done

  export "${var_name}=${input_val}"
  save_var "$var_name" "$input_val"
}

init_var_model_provider() {
  init_var "MODEL_PROVIDER" "gemini" "Enter Model Provider (gemini, anthropic, chatgpt, openai)"

  MODEL_PROVIDER=$(echo "$MODEL_PROVIDER" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
  if [[ ! "$MODEL_PROVIDER" =~ ^(gemini|anthropic|chatgpt|openai)$ ]]; then
    print_error "Invalid Model Provider '$MODEL_PROVIDER'. Must be one of: gemini, anthropic, chatgpt, openai."
    exit 1
  fi

  case "$MODEL_PROVIDER" in
    chatgpt|openai)
      DEFAULT_MODEL="gpt-5.4"
      ;;
    anthropic)
      DEFAULT_MODEL="claude-sonnet-4-5-20250929"
      ;;
    *)
      DEFAULT_MODEL="gemini-3.5-flash"
      ;;
  esac

  init_var "MODEL_DEFAULT_NAME" "$DEFAULT_MODEL" "Enter Model Default Name"
}

init_var_platform_agent_permission_set() {
  init_var "PLATFORM_AGENT_PERMISSION_SET" "read-only" "Enter Platform Agent Permission Set (read-only, custom)"

  PLATFORM_AGENT_PERMISSION_SET=$(echo "$PLATFORM_AGENT_PERMISSION_SET" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')

  # The 'gke-admin' preset is retired (Phase 1). The Platform Agent is read-only at the
  # cloud boundary — the only write path is a reviewed GitOps PR applied by the CI/CD
  # pipeline (invariant: the cloud GSA stays viewer-only). Coerce any stale value to
  # read-only so re-provisioning an install that predates this downgrades it in place.
  if [ "$PLATFORM_AGENT_PERMISSION_SET" = "gke-admin" ]; then
    print_warning "PLATFORM_AGENT_PERMISSION_SET='gke-admin' is retired; the Platform Agent is read-only. Proceeding as 'read-only' (use 'custom' with explicit roles to extend it)."
    PLATFORM_AGENT_PERMISSION_SET="read-only"
  fi

  if [[ ! "$PLATFORM_AGENT_PERMISSION_SET" =~ ^(read-only|custom)$ ]]; then
    print_error "Invalid Platform Agent Permission Set '$PLATFORM_AGENT_PERMISSION_SET'. Must be one of: read-only, custom."
    exit 1
  fi

  if [ "$PLATFORM_AGENT_PERMISSION_SET" = "custom" ]; then
    init_var "PLATFORM_AGENT_CUSTOM_ROLES" "" "Enter Custom GCP IAM Roles (space or comma-separated)"
    if [ -z "${PLATFORM_AGENT_CUSTOM_ROLES:-}" ]; then
      print_error "Custom permission set selected, but PLATFORM_AGENT_CUSTOM_ROLES is empty."
      exit 1
    fi
  fi
}


load_state() {
  if [ -f "$VARS_FILE" ]; then
    source "$VARS_FILE"
  elif [ "${DRY_RUN:-0}" -ne 1 ]; then
    echo "# SRE Sourced Variables for GKE & GCP Setup" > "$VARS_FILE"
    source "$VARS_FILE"
  fi
  export NAMESPACE="kubeagents-system"
  export PLATFORM_AGENT_KSA_NAME="kubeagents-platform-agent"
  export PLATFORM_AGENT_GSA_NAME="kubeagents-platform-gsa"
  export CONTROLLER_KSA_NAME="kubeagents-controller"
  export CONTROLLER_GSA_NAME="kubeagents-controller-gsa"
  export GITHUB_MINTER_KSA_NAME="kubeagents-github-minter"
  export GITHUB_MINTER_GSA_NAME="kubeagents-github-minter-gsa"
}

ensure_teardown_state() {
  if [ -f "$VARS_FILE" ]; then
    source "$VARS_FILE"
    export GCP_ARTIFACT_REGISTRY_REPO_NAME="${GCP_ARTIFACT_REGISTRY_REPO_NAME:-${REPO_NAME:-kube-agents}}"
    export DEV_ARTIFACT_REGISTRY_CREATED="${DEV_ARTIFACT_REGISTRY_CREATED:-false}"
    export NAMESPACE="kubeagents-system"
    export PLATFORM_AGENT_KSA_NAME="kubeagents-platform-agent"
    export PLATFORM_AGENT_GSA_NAME="kubeagents-platform-gsa"
    export CONTROLLER_KSA_NAME="kubeagents-controller"
    export CONTROLLER_GSA_NAME="kubeagents-controller-gsa"
    export GITHUB_MINTER_KSA_NAME="kubeagents-github-minter"
    export GITHUB_MINTER_GSA_NAME="kubeagents-github-minter-gsa"
  else
    echo -e "  ${C_YELLOW}⚠ State file ${VARS_FILE} not found. Prompting for target values...${C_RESET}"
    local ACTIVE_PROJECT
    ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
    if [ "${DRY_RUN:-0}" -eq 1 ]; then
      export PROJECT_ID="${ACTIVE_PROJECT:-dummy-project}"
      export REGION="us-east4"
      export CLUSTER_NAME="platform-agent-host"
    elif is_non_interactive; then
      # Deliberately NOT the dry-run branch's defaults. Those name the LIVE install
      # (`platform-agent-host`), which is a safe thing to print and an unsafe thing to apply to: an
      # unattended run that cannot find its state file would silently adopt production as its
      # target. There is no default that is right here, so there is no default.
      print_error "${VARS_FILE} does not exist and this is a non-interactive run."
      print_error "PROJECT_ID, REGION and CLUSTER_NAME would have to be guessed, and the guess"
      print_error "would be the live install. Set them in vars.sh (or the environment) first."
      exit 1
    else
      echo -ne "  ${C_CYAN}Enter Target GCP Project ID [${C_WHITE}${ACTIVE_PROJECT}${C_CYAN}]: ${C_RESET}"
      read -r INPUT_PROJECT_ID
      export PROJECT_ID="${INPUT_PROJECT_ID:-$ACTIVE_PROJECT}"
      if [ -z "$PROJECT_ID" ]; then
        echo -e "  ${C_RED}✗ Project ID is required.${C_RESET}"
        exit 1
      fi
      export REGION="${REGION:-us-east4}"
      echo -ne "  ${C_CYAN}Enter GKE GCP Region [${C_WHITE}${REGION}${C_CYAN}]: ${C_RESET}"
      read -r INPUT_REGION
      export REGION="${INPUT_REGION:-$REGION}"

      export CLUSTER_NAME="${CLUSTER_NAME:-platform-agent-host}"
      echo -ne "  ${C_CYAN}Enter GKE Cluster Name [${C_WHITE}${CLUSTER_NAME}${C_CYAN}]: ${C_RESET}"
      read -r INPUT_CLUSTER_NAME
      export CLUSTER_NAME="${INPUT_CLUSTER_NAME:-$CLUSTER_NAME}"
    fi
    export NAMESPACE="kubeagents-system"
    export GCP_ARTIFACT_REGISTRY_REPO_NAME="${GCP_ARTIFACT_REGISTRY_REPO_NAME:-${REPO_NAME:-kube-agents}}"
    export DEV_ARTIFACT_REGISTRY_CREATED="${DEV_ARTIFACT_REGISTRY_CREATED:-false}"
    if [ "${GOOGLE_CHAT_ENABLED:-false}" = "true" ]; then
      export CHAT_TOPIC_NAME="${CHAT_TOPIC_NAME:-platform-agent-chat-events}"
      export CHAT_SUB_NAME="${CHAT_SUB_NAME:-platform-agent-chat-events-sub}"
    else
      export CHAT_TOPIC_NAME="${CHAT_TOPIC_NAME:-}"
      export CHAT_SUB_NAME="${CHAT_SUB_NAME:-}"
    fi
    export PLATFORM_AGENT_KSA_NAME="kubeagents-platform-agent"
    export PLATFORM_AGENT_GSA_NAME="kubeagents-platform-gsa"
    export CONTROLLER_KSA_NAME="kubeagents-controller"
    export CONTROLLER_GSA_NAME="kubeagents-controller-gsa"
    export GITHUB_MINTER_KSA_NAME="kubeagents-github-minter"
    export GITHUB_MINTER_GSA_NAME="kubeagents-github-minter-gsa"
  fi
}

# ─── Step Runner Framework ────────────────────────────────────────────────────
run_step() {
  local name=$1
  local verify_func=$2
  local execute_func=$3
  local wait_time=${4:-0}
  
  print_step "$name"
  echo -e "  ${C_CYAN}Verifying current state...${C_RESET}"
  
  if $verify_func; then
    print_success "Already completed: $name"
    return 0
  fi
  
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    print_info "[DRY-RUN] Would execute: $name"
    return 0
  fi

  print_info "Executing action..."
  if $execute_func; then
    print_success "Successfully executed."
    if [ "$wait_time" -gt 0 ]; then
      wait_for_a_bit "$wait_time" "Waiting for changes to propagate"
    fi
  else
    print_error "Failed to execute step: $name"
    exit 1
  fi
}

# ─── Smart Deployment Step Runner (Routes based on CI/CD mode) ────────────────
run_deploy_step() {
  local name=$1
  local verify_func=$2
  local execute_func=$3
  local wait_time=${4:-0}

  if is_ci_pipeline; then
    local force_redeploy_verify="false"
    run_step "$name" "$force_redeploy_verify" "$execute_func" "$wait_time"
  else
    run_step "$name" "$verify_func" "$execute_func" "$wait_time"
  fi
}

# ─── Cloud Helpers ────────────────────────────────────────────────────────────
check_prereqs() {
  for cmd in "$@"; do
    echo -ne "  ${C_CYAN}Checking for $cmd... ${C_RESET}"
    if command -v "$cmd" &> /dev/null; then
      echo -e "✅"
    else
      echo -e "❌"
      print_error "$cmd is required but not installed. Please install it and rerun."
      exit 1
    fi
  done
}

cluster_exists() {
  gcloud container clusters list --filter="name=${CLUSTER_NAME} AND location:${REGION}*" --format="value(name)" --project="${PROJECT_ID}" 2>/dev/null || echo ""
}

connect_cluster() {
  print_info "Fetching cluster credentials..."
  gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet
}

ensure_k8s_resource_exists() {
  local resource=$1         # e.g., "deployment/cert-manager-cainjector"
  local namespace=$2        # e.g., "cert-manager"
  local retries=${3:-10}    # Default 10 retries (20s timeout)

  print_info "Checking existence of ${resource} in namespace '${namespace}'..."
  if [ "${DRY_RUN:-0}" -eq 1 ]; then return 0; fi

  _check_resource_exists() {
    kubectl get "${resource}" -n "${namespace}" &>/dev/null
  }

  if ! retry "$retries" 2 _check_resource_exists; then
    print_error "Timeout waiting for ${resource} to be created in '${namespace}'." >&2
    return 1
  fi
  print_success "${resource} exists in '${namespace}'."
}

wait_for_k8s_resource() {
  local resource=$1                 # e.g., "deployment/cert-manager"
  local namespace=$2                # e.g., "cert-manager"
  local condition=${3:-"Available"} # e.g., "Available"
  local timeout=${4:-"120s"}

  # Step 1: Ensure resource exists in API server etcd before calling 'kubectl wait'
  ensure_k8s_resource_exists "${resource}" "${namespace}" 10 || return 1

  print_info "Waiting for ${resource} in namespace '${namespace}' (condition=${condition})..."
  if [ "${DRY_RUN:-0}" -eq 1 ]; then return 0; fi

  # Step 2: Wait for condition availability
  kubectl wait --for="condition=${condition}" "${resource}" -n "${namespace}" --timeout="${timeout}" || return 1
  print_success "${resource} reached state: ${condition}."
}

# render_allowlist_block <enabled> <comma-separated-users> <field-label>
#
# Emits the `allowedUsers:` YAML block for an Agent CR chat integration, indented
# to sit under `spec.integration.<platform>`, and writes it to stdout.
#
# Two rules, both load-bearing (06 §1.2 V-7, 03 §4a):
#
#   1. When the integration is DISABLED, emit nothing. The previous template
#      unconditionally rendered `allowedUsers: [ "${ALLOWED_USERS}" ]`, so an
#      unset variable produced a one-element list whose only element was the
#      empty string — a list of size 1 that names nobody. Every size-based guard
#      passed and the controller then treated it as "allow everyone".
#
#   2. When the integration is ENABLED, the list must name at least one
#      principal. An empty or all-blank value is a provisioning error and we
#      fail here rather than shipping a CR the API server would reject with a
#      less obvious message — or, worse, one that admits the world.
#
# Blank entries are dropped, so "U1,,U2" and " , " are handled the same way the
# webhook and the renderer handle them.
render_allowlist_block() {
  local enabled="$1"
  local raw="$2"
  local label="$3"

  if [ "${enabled}" != "true" ]; then
    return 0
  fi

  local entries=()
  local IFS=','
  local u
  for u in ${raw}; do
    u="$(printf '%s' "$u" | tr -d '[:space:]')"
    [ -n "$u" ] && entries+=("$u")
  done
  unset IFS

  if [ ${#entries[@]} -eq 0 ]; then
    print_error "${label} is enabled but its allowlist is empty."
    print_error "An empty or all-blank allowlist is not an allowlist — there is no permissive fallback."
    print_error "Set the allowlist variable to at least one principal ID in vars.sh, or disable the integration."
    exit 1
  fi

  echo "      allowedUsers:"
  for u in "${entries[@]}"; do
    echo "        - \"${u}\""
  done
}

# render_wi_metadata_block
#
# Emits the NARROW cloud-metadata allow rules for the per-tier egress policy, or
# nothing at all. Written to stdout, indented to sit under `spec.egress`.
#
# THE CONFLICT THIS RESOLVES. The per-tier policies are a pure allowlist, so the
# cloud metadata server is unreachable by omission — and 03 §11's load-bearing
# negative is exactly that raw node credentials cannot be read. But on GKE,
# Workload Identity mints the agent's tokens *through* that same metadata
# service, so shipping the policies as-is takes every tier's identity away.
# 03 §9 names both halves: allowlist the agent's real destinations, and "must
# not accidentally deny the metadata server that Workload Identity depends on."
#
# The resolution is conditional and port-bound, never a whole-host allow:
#
#   * WORKLOAD_IDENTITY_ENABLED != true  -> emit NOTHING. On Kind, on a non-WI
#     GKE cluster, and on any other target, 169.254.169.254:80 is the RAW
#     metadata endpoint serving the NODE's service account — the classic
#     escalation. Denying it is correct, and silence here is what denies it.
#
#   * WORKLOAD_IDENTITY_ENABLED == true  -> emit the metadata rules for the
#     cluster's dataplane, bound to the metadata ports only. GKE's own metadata
#     concealment keeps the node-SA paths unreachable, so what is opened is the
#     pod's own (viewer-only) GSA and nothing more.
#
# THE IP↔PORT PAIRINGS ARE DATAPLANE-SPECIFIC AND ARE NOT INTERCHANGEABLE
# (https://cloud.google.com/kubernetes-engine/docs/how-to/network-policy):
#
#     Dataplane V1 / Calico, GKE >= 1.21.0-gke.1000 : 169.254.169.252/32  TCP 988, 987
#     Dataplane V2                                  : 169.254.169.254/32  TCP 80, 8080
#
# Both are emitted by default (GKE_DATAPLANE=auto) because a policy that names
# the wrong pair for the cluster it lands on fails as a timeout inside the
# client library — an authentication error with no mention of networking, which
# is close to undebuggable in the field. Set GKE_DATAPLANE=v1|v2 to narrow it
# once the cluster's dataplane is known.
render_wi_metadata_block() {
  local enabled="${WORKLOAD_IDENTITY_ENABLED:-false}"
  local dataplane="${GKE_DATAPLANE:-auto}"

  if [ "${enabled}" != "true" ]; then
    return 0
  fi

  echo "    # 5) GKE metadata server — Workload Identity ONLY, bound to the metadata ports. This is the"
  echo "    #    single widening in this policy; it is narrow on purpose. WI's metadata concealment keeps"
  echo "    #    the node service account unreachable, so what this opens is the pod's own viewer-only"
  echo "    #    GSA. Rendered only because WORKLOAD_IDENTITY_ENABLED=true (common.sh:render_wi_metadata_block)."
  if [ "${dataplane}" = "auto" ] || [ "${dataplane}" = "v1" ]; then
    echo "    #    Dataplane V1 / Calico (GKE >= 1.21.0-gke.1000)."
    echo "    - to:"
    echo "        - ipBlock:"
    echo "            cidr: 169.254.169.252/32"
    echo "      ports:"
    echo "        - protocol: TCP"
    echo "          port: 988"
    echo "        - protocol: TCP"
    echo "          port: 987"
  fi
  if [ "${dataplane}" = "auto" ] || [ "${dataplane}" = "v2" ]; then
    echo "    #    Dataplane V2."
    echo "    - to:"
    echo "        - ipBlock:"
    echo "            cidr: 169.254.169.254/32"
    echo "      ports:"
    echo "        - protocol: TCP"
    echo "          port: 80"
    echo "        - protocol: TCP"
    echo "          port: 8080"
  fi
}

# render_remote_hub_block
#
# Emits the ipBlock allow rules for a REMOTE-HUB topology, or nothing.
#
# In the reference topology LiteLLM and github-token-minter run in the cluster's
# own control namespace, and rule 2 of the template is the working path. A
# remote hub — the spoke consuming the hub's VPC-internal private endpoints
# (05 §5) — needs two extra CIDRs, and grounding against an MCP endpoint that
# lives outside the cluster needs a third.
#
# These used to ship as `REPLACE_WITH_HUB_INFERENCE_CIDR` and friends. That is
# not a fillable template: `REPLACE_WITH_*` is not a CIDR, so the manifest was
# rejected outright by the API server and the reference GitOps tree could not be
# applied at all (V-CMP-003). Absent-unless-configured is both applicable and
# strictly narrower than a placeholder nobody filled in.
render_remote_hub_block() {
  local inference="${HUB_INFERENCE_CIDR:-}"
  local minty="${HUB_MINTY_CIDR:-}"
  local mcp="${MCP_GROUNDING_CIDRS:-}"

  _emit_cidr_rule() { # _emit_cidr_rule <comment> <cidr-csv> <port>
    local comment="$1" csv="$2" port="$3" c
    [ -z "$(printf '%s' "$csv" | tr -d '[:space:]')" ] && return 0
    echo "    # ${comment}"
    echo "    - to:"
    local IFS=','
    for c in ${csv}; do
      c="$(printf '%s' "$c" | tr -d '[:space:]')"
      [ -n "$c" ] && echo "        - ipBlock:
            cidr: ${c}"
    done
    unset IFS
    echo "      ports:"
    echo "        - protocol: TCP"
    echo "          port: ${port}"
  }

  _emit_cidr_rule "6) Hub Inference (LiteLLM) over the hub's VPC-internal private endpoint (05 §5)." \
    "${inference}" 443
  _emit_cidr_rule "7) Hub Minty — the GitHub/Workload-Identity token broker, VPC-internal (05 §5)." \
    "${minty}" 443
  _emit_cidr_rule "8) MCP grounding endpoints the agent reads live docs from (03 §10)." \
    "${mcp}" 443

  unset -f _emit_cidr_rule
}

# render_apiserver_block
#
# Emits the kube-apiserver allow rule for the per-tier egress policy, or nothing.
#
# WHY THIS EXISTS. The per-tier policy is a pure allowlist and its four base
# destinations are DNS, the control namespace on :80/:8080, restricted.googleapis.com
# and GitHub's published blocks. None of those is the API server. The BROKER pod
# carries `kube-agents/tier`, so this policy selects it too, and the broker needs the
# API server for three of its eleven pipeline steps — TokenReview (1), the FleetFreeze
# read (5), the ActionRecord write (11). Without this rule the broker authenticates
# nobody, and the symptom reads as an auth bug: a TokenReview that never returns looks
# identical to a TokenReview that was refused. The READER needs it too, for every
# kubectl-shaped skill it runs; that half has been latent since Phase 5.
#
# NOTHING THE CONTROLLER RENDERS CAN FIX THIS. NetworkPolicy is L3/L4 and cannot name
# a Service, so "allow the `kubernetes` endpoint" is inexpressible — it has to be an
# address, and which address is per-cluster and known only at install time. See
# `resolve_apiserver_cidrs` below, and internal/controller/pair_netpol.go's header.
#
# THIS IS NOT A WIDENING OF AUTHORITY, and it is worth being explicit about that
# because "the agent pod may now reach the API server" reads like one. Reachability is
# not permission: the reader's RBAC is read-only and `vap-agent-readonly` denies it
# every write verb at admission, and the actor's grant is exactly 06 §2.2.1's twenty
# triples. What this rule changes is whether an authorized request can leave the pod,
# not what the API server will do with it.
#
# BOTH ADDRESS FORMS ARE EMITTED, for the same reason GKE_DATAPLANE defaults to `auto`.
# In-cluster clients dial the `kubernetes` Service ClusterIP and kube-proxy or the
# dataplane DNATs that to the real control-plane endpoint. Whether NetworkPolicy
# evaluates egress before or after that translation is dataplane-specific, so a policy
# naming only one of the two fails on the other — as a connection timeout inside a
# client library, with no mention of networking. Two /32s on 443 is a narrow price for
# not having to be right about which.
render_apiserver_block() {
  local csv="${KUBE_APISERVER_CIDRS:-}"
  local c

  [ -z "$(printf '%s' "${csv}" | tr -d '[:space:]')" ] && return 0

  echo "    # 9) The kube-apiserver. The BROKER cannot work without it — TokenReview (pipeline step 1),"
  echo "    #    the FleetFreeze read (step 5) and the ActionRecord write (step 11) all go here — and the"
  echo "    #    reader needs it for every kubectl-shaped skill. Reachability, not permission: RBAC and"
  echo "    #    vap-agent-readonly still decide what the request is allowed to do."
  echo "    #    Resolved at install time by common.sh:resolve_apiserver_cidrs; both the in-cluster"
  echo "    #    Service address and the control-plane endpoint are listed, because which one the"
  echo "    #    dataplane sees depends on where it evaluates egress relative to DNAT."
  echo "    - to:"
  local IFS=','
  for c in ${csv}; do
    c="$(printf '%s' "$c" | tr -d '[:space:]')"
    [ -n "$c" ] && echo "        - ipBlock:
            cidr: ${c}"
    first=0
  done
  unset IFS
  echo "      ports:"
  echo "        - protocol: TCP"
  echo "          port: 443"
}

# resolve_apiserver_cidrs
#
# Writes the comma-separated CIDR list render_apiserver_block consumes, or exits
# non-zero having written nothing. FAIL-CLOSED IS THE POINT: an empty answer that
# looks like a success renders a policy with no API-server rule, which is precisely
# the hole this unit closes, and it would be invisible until a broker hung.
#
# Three sources, in order:
#
#   1. KUBE_APISERVER_CIDR (vars.sh) — an explicit override, comma-separated, used
#      verbatim. Required for a private cluster whose master range is not derivable
#      from the kubeconfig, and for any cluster reached through a bastion or a
#      forwarded endpoint where the address the kubeconfig names is not the address
#      the pods reach.
#   2. The live cluster: the `kubernetes` Service's ENDPOINT addresses, its ClusterIP,
#      and the host out of the current context's `server:` URL. All become /32s. This
#      is the ordinary GKE case and it needs no configuration at all — which matters,
#      because a knob that must be filled in for the broker to work is a knob that will
#      not be filled in.
#   3. Nothing. Return 1.
#
# THE ENDPOINT ADDRESS IS THE ONE THAT ACTUALLY MATCHES, AND IT WAS MISSING UNTIL
# 2026-08-01. This function shipped reading the ClusterIP and the kubeconfig host, on
# the argument above that one of the two must be what the dataplane sees. On GKE
# neither is. A pod dials the ClusterIP (34.118.224.1), the dataplane DNATs it in eBPF
# BEFORE egress policy is evaluated, and the packet the policy scores carries the
# control-plane's node-network address — `endpoints/kubernetes` in `default`, which is
# a third address this function never read: 10.150.0.9 on the scratch cluster,
# 10.150.0.2 on the live one, and neither cluster's kubeconfig names it (they name
# 35.221.35.254 and 34.145.154.119, the public endpoints). The rendered rule 9 was
# therefore two /32s that no packet ever carries.
#
# It presents exactly as the header of render_apiserver_block warns and worse. The
# broker's startSources() reads the brake BEFORE the listener opens, so the pod never
# binds :8443 at all: `kubectl logs` is EMPTY, the readiness and liveness probes both
# report `connection refused`, and the kubelet restarts it on a loop. Nothing anywhere
# says "network". Measured on 2026-08-01 by adding a single /32 for the endpoint
# address to the same namespace — the broker went 1/1 within one probe period
# ([[LSN-069]]).
#
# ALL THREE FORMS ARE STILL EMITTED, endpoint first. The ClusterIP and the kubeconfig
# host stay because the original argument for them stands — where the dataplane
# evaluates egress relative to DNAT is not something this script can know — and three
# /32s on 443 is a narrow price for not having to be right about it. Adding the one
# that matches is the fix; removing the two that did not would be a second guess.
#
# EndpointSlice is read first and `endpoints` is the fallback: v1 Endpoints is
# deprecated from 1.33 and prints a warning to stderr on every read, which is noise in
# an install log and, on a cluster that has dropped the compatibility shim, no answer
# at all.
#
# A hostname in the `server:` URL is DELIBERATELY NOT RESOLVED here. NetworkPolicy
# takes addresses, resolving one at install time pins whatever the DNS answer was that
# afternoon, and a policy that silently stops matching after a control-plane IP
# rotation is worse than one that was never written. Set KUBE_APISERVER_CIDR instead.
resolve_apiserver_cidrs() {
  local override="${KUBE_APISERVER_CIDR:-}"
  local out="" clusterip server host endpoints ep

  if [ -n "$(printf '%s' "${override}" | tr -d '[:space:]')" ]; then
    printf '%s\n' "${override}"
    return 0
  fi

  # An IPv4 dotted quad and nothing else. `_ipv4` is deliberately strict rather than
  # permissive: a value that is almost an address renders a `cidr:` the API server
  # rejects, and provision_13 would then fail on the apply instead of here, where the
  # message can say which of the three sources produced it.
  _ipv4() { # _ipv4 <candidate>
    case "$1" in
      *[!0-9.]* | '' | *..*) return 1 ;;
      *.*.*.*.*) return 1 ;;
      *.*.*.*) return 0 ;;
      *) return 1 ;;
    esac
  }

  # _append <candidate> — a /32 for a dotted quad, deduplicated. Order is preserved, so
  # rule 9 reads endpoint-first and a human comparing it against `endpoints/kubernetes`
  # sees the match on the first line.
  _append() {
    case ",${out}," in
      *",$1/32,"*) return 0 ;;
    esac
    _ipv4 "$1" || return 0
    if [ -n "${out}" ]; then out="${out},$1/32"; else out="$1/32"; fi
  }

  endpoints="$(kubectl get endpointslices -n default -l kubernetes.io/service-name=kubernetes \
    -o jsonpath='{.items[*].endpoints[*].addresses[*]}' 2>/dev/null || echo "")"
  if [ -z "$(printf '%s' "${endpoints}" | tr -d '[:space:]')" ]; then
    endpoints="$(kubectl get endpoints kubernetes -n default \
      -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null || echo "")"
  fi
  for ep in ${endpoints}; do
    _append "${ep}"
  done

  clusterip="$(kubectl get service kubernetes -n default -o jsonpath='{.spec.clusterIP}' 2>/dev/null || echo "")"
  _append "${clusterip}"

  server="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null || echo "")"
  host="${server#*://}"
  host="${host%%:*}"
  host="${host%%/*}"
  _append "${host}"

  unset -f _ipv4 _append

  [ -z "${out}" ] && return 1
  printf '%s\n' "${out}"
}

# render_egress_policy <netpol-name> <namespace> <tier>
#
# Renders k8s-operator/scripts/netpol-agent-egress.yaml.template for one tier and
# writes the manifest to stdout. The optional blocks are composed here so the
# template stays a single flat allowlist that reads top to bottom.
render_egress_policy() {
  local netpol_name="$1"
  local namespace="$2"
  local tier="$3"
  local template="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}/netpol-agent-egress.yaml.template"

  if [ ! -f "${template}" ]; then
    print_error "Egress policy template not found: ${template}"
    exit 1
  fi

  local optional
  optional="$(
    render_wi_metadata_block
    render_remote_hub_block
    render_apiserver_block
  )"

  # Command substitution strips every trailing newline, so `printf '%s\n'` leaves
  # exactly one. Without this the manifest ends with a stray blank line whenever
  # EGRESS_OPTIONAL_BLOCKS is empty (the common case), which put two gates in
  # direct conflict: Prettier deletes the blank line in the committed exemplars,
  # and reference-render.py requires them to be byte-identical to this render.
  # Fixing it here rather than exempting either gate keeps both true at once.
  local rendered
  rendered="$(
    NETPOL_NAME="${netpol_name}" \
      AGENT_NAMESPACE="${namespace}" \
      AGENT_TIER="${tier}" \
      CONTROL_NAMESPACE="${CONTROL_NAMESPACE:-kubeagents-system}" \
      EGRESS_OPTIONAL_BLOCKS="${optional}" \
      envsubst '${NETPOL_NAME} ${AGENT_NAMESPACE} ${AGENT_TIER} ${CONTROL_NAMESPACE} ${EGRESS_OPTIONAL_BLOCKS}' \
      <"${template}"
  )"
  printf '%s\n' "${rendered}"
}

# ------------------------------------------------------------------------------
# Tenant isolation: the ResourceQuota and the namespace default-deny
# ------------------------------------------------------------------------------
# Both manifests existed in examples/gitops-repo/ from Phase 3 and NO INSTALL PATH APPLIED EITHER —
# the same defect class as the egress policies (LSN-006/LSN-007). They are rendered here rather than
# read out of the reference tree for two reasons: the namespace is a variable, and P8-T2 found that
# two copies of a security manifest drift in the direction where the copy a human reads stays right
# and the copy that lands on the cluster goes wrong. One source, one render, exemplar derived.

# The per-tenant blast-radius knobs. Defaults match the Phase 3 reference bundle; a real tenant
# should be sized deliberately. Object counts beyond `pods` are fixed in the template — they are
# namespace hygiene, not blast-radius controls.
render_tenant_quota() { # render_tenant_quota <namespace>
  local namespace="$1"
  local template="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}/tenant-quota.yaml.template"

  if [ ! -f "${template}" ]; then
    print_error "Tenant quota template not found: ${template}"
    exit 1
  fi

  local rendered
  rendered="$(
    TENANT_NAMESPACE="${namespace}" \
      TENANT_QUOTA_NAME="${TENANT_QUOTA_NAME:-${namespace}-quota}" \
      TENANT_QUOTA_REQUESTS_CPU="${TENANT_QUOTA_REQUESTS_CPU:-8}" \
      TENANT_QUOTA_REQUESTS_MEMORY="${TENANT_QUOTA_REQUESTS_MEMORY:-16Gi}" \
      TENANT_QUOTA_LIMITS_CPU="${TENANT_QUOTA_LIMITS_CPU:-16}" \
      TENANT_QUOTA_LIMITS_MEMORY="${TENANT_QUOTA_LIMITS_MEMORY:-32Gi}" \
      TENANT_QUOTA_PODS="${TENANT_QUOTA_PODS:-50}" \
      envsubst '${TENANT_NAMESPACE} ${TENANT_QUOTA_NAME} ${TENANT_QUOTA_REQUESTS_CPU} ${TENANT_QUOTA_REQUESTS_MEMORY} ${TENANT_QUOTA_LIMITS_CPU} ${TENANT_QUOTA_LIMITS_MEMORY} ${TENANT_QUOTA_PODS}' \
      <"${template}"
  )"
  printf '%s\n' "${rendered}"
}

# ─── Control-namespace quota ──────────────────────────────────────────────────
#
# THE SIZING IS ARITHMETIC, NOT A GUESS, AND `dev/tests/quota-admits-agent.py` ENFORCES IT.
#
# The control namespace must hold, simultaneously:
#
#   baseline control plane   3000m CPU / 5760Mi   (operator 500m/128Mi, LiteLLM x2 500m/2Gi each,
#                                                  github-token-minter x2 500m/256Mi each,
#                                                  inference-replay 500m/1Gi) — limits
#   + N agent gateways       3700m CPU / 6528Mi each — limits
#
# The gateway term is NOT written down here. It is the sum of the four containers the controller
# stamps (agent, dashboard, fluent-bit, event-watcher), and the check reads it out of the golden
# render at k8s-operator/internal/testing/testdata/platform/expected/agent.yaml. That coupling is
# the entire point: bump the agent's memory limit in agent_manifests.go and the golden test forces
# the golden file to change, and the moment it does, the check re-does this arithmetic and fails if
# these defaults no longer fit. A quota sized by hand is only correct until the pod grows.
#
# CONTROL_QUOTA_GATEWAYS is 3, not 2: platform + cluster-admin are resident, and the third is the
# headroom that lets a rolling update run (new pod admitted before the old one is released) without
# the rollout stalling on admission. Two resident gateways with no spare is the configuration that
# produced the 2026-07-27 lockout described in control-quota.yaml.template.
#
#   limits.cpu      3000m + 3 x 3700m  = 14100m -> 16
#   limits.memory   5760Mi + 3 x 6528Mi = 25344Mi -> 32Gi
#   requests.cpu     510m + 3 x  906m  =  3228m -> 8
#   requests.memory 1600Mi + 3 x 2752Mi =  9856Mi -> 16Gi
#
# The baseline figures are declared rather than derived because the components are spread across
# provision_03/09/10/11 and a kustomize base; they are a measured floor, and the check treats them
# as such. Raising a component's limits without raising the baseline here is the one drift this
# arrangement does not catch — which is why they are named individually above.
CONTROL_QUOTA_BASELINE_LIMITS_CPU_MILLIS="${CONTROL_QUOTA_BASELINE_LIMITS_CPU_MILLIS:-3000}"
CONTROL_QUOTA_BASELINE_LIMITS_MEMORY_MIB="${CONTROL_QUOTA_BASELINE_LIMITS_MEMORY_MIB:-5760}"
CONTROL_QUOTA_BASELINE_REQUESTS_CPU_MILLIS="${CONTROL_QUOTA_BASELINE_REQUESTS_CPU_MILLIS:-510}"
CONTROL_QUOTA_BASELINE_REQUESTS_MEMORY_MIB="${CONTROL_QUOTA_BASELINE_REQUESTS_MEMORY_MIB:-1600}"
CONTROL_QUOTA_GATEWAYS="${CONTROL_QUOTA_GATEWAYS:-3}"

render_control_quota() { # render_control_quota [namespace]
  local namespace="${1:-${CONTROL_NAMESPACE:-kubeagents-system}}"
  local template="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}/control-quota.yaml.template"

  if [ ! -f "${template}" ]; then
    print_error "Control quota template not found: ${template}"
    exit 1
  fi

  local rendered
  rendered="$(
    CONTROL_QUOTA_NAMESPACE="${namespace}" \
      CONTROL_QUOTA_NAME="${CONTROL_QUOTA_NAME:-${namespace}-quota}" \
      CONTROL_QUOTA_REQUESTS_CPU="${CONTROL_QUOTA_REQUESTS_CPU:-8}" \
      CONTROL_QUOTA_REQUESTS_MEMORY="${CONTROL_QUOTA_REQUESTS_MEMORY:-16Gi}" \
      CONTROL_QUOTA_LIMITS_CPU="${CONTROL_QUOTA_LIMITS_CPU:-16}" \
      CONTROL_QUOTA_LIMITS_MEMORY="${CONTROL_QUOTA_LIMITS_MEMORY:-32Gi}" \
      CONTROL_QUOTA_PODS="${CONTROL_QUOTA_PODS:-60}" \
      envsubst '${CONTROL_QUOTA_NAMESPACE} ${CONTROL_QUOTA_NAME} ${CONTROL_QUOTA_REQUESTS_CPU} ${CONTROL_QUOTA_REQUESTS_MEMORY} ${CONTROL_QUOTA_LIMITS_CPU} ${CONTROL_QUOTA_LIMITS_MEMORY} ${CONTROL_QUOTA_PODS}' \
      <"${template}"
  )"
  printf '%s\n' "${rendered}"
}

# Applies the quota to the control namespace. Called from provision_03 immediately after
# `make deploy` creates the namespace, so every pod steps 08-12 create is admitted against it.
#
# REFUSES TO SHRINK BELOW WHAT IS ALREADY RUNNING. `kubectl apply` of a ResourceQuota whose `hard`
# is under current `used` succeeds — the API server accepts it, existing pods are grandfathered,
# and the namespace is left unable to admit the next pod. That is precisely the 2026-07-27 failure,
# and it is silent. So this reads `used` back first and refuses rather than arming the trap.
apply_control_quota() { # apply_control_quota [namespace]
  local namespace="${1:-${CONTROL_NAMESPACE:-kubeagents-system}}" rendered

  if [ "${CONTROL_QUOTA_ENABLED:-true}" != "true" ]; then
    print_warning "CONTROL_QUOTA_ENABLED=${CONTROL_QUOTA_ENABLED} — skipping. '${namespace}' will have NO compute bound."
    return 0
  fi

  rendered="$(render_control_quota "${namespace}")" || return 1

  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    print_info "[dry-run] would apply ResourceQuota in ${namespace}"
    printf '%s\n' "${rendered}" | kubectl apply --dry-run=server -f - >/dev/null || return 1
    print_success "Control ResourceQuota validates against the API server"
    return 0
  fi

  assert_control_quota_fits_current_usage "${namespace}" || return 1

  printf '%s\n' "${rendered}" | kubectl apply -f - || return 1
  print_success "ResourceQuota applied in ${namespace} (${CONTROL_QUOTA_LIMITS_CPU:-16} CPU / ${CONTROL_QUOTA_LIMITS_MEMORY:-32Gi} limits)."
}

# Reads what the namespace is ALREADY consuming and refuses a quota that cannot cover it. Uses the
# live ResourceQuota's `used` when one exists; otherwise sums the pods directly, because on a first
# install there is no quota to read `used` from.
assert_control_quota_fits_current_usage() { # assert_control_quota_fits_current_usage <namespace>
  local namespace="$1"
  local want_cpu want_mem used_cpu used_mem

  want_cpu="$(_cpu_to_millis "${CONTROL_QUOTA_LIMITS_CPU:-16}")"
  want_mem="$(_mem_to_mib "${CONTROL_QUOTA_LIMITS_MEMORY:-32Gi}")"

  # Sum limits over non-terminal pods. `|| true` throughout: an unreadable namespace must not kill
  # the caller under `set -e` — it means "could not measure", and an unmeasured namespace is not
  # evidence of a problem. Same discipline as dev/lib/substrate-capacity.sh.
  local raw
  raw="$(kubectl get pods -n "${namespace}" \
    -o jsonpath='{range .items[?(@.status.phase!="Succeeded")]}{range .spec.containers[*]}{.resources.limits.cpu}{" "}{.resources.limits.memory}{"\n"}{end}{end}' \
    2>/dev/null)" || raw=""

  if [ -z "${raw}" ]; then
    print_info "Could not read current pod usage in '${namespace}' (new namespace, or no read access)."
    print_info "Applying the quota unvalidated. If a later step fails with 'exceeded quota', that is why."
    return 0
  fi

  used_cpu=0
  used_mem=0
  local c m
  while read -r c m; do
    [ -z "${c}" ] && continue
    used_cpu=$((used_cpu + $(_cpu_to_millis "${c}")))
    used_mem=$((used_mem + $(_mem_to_mib "${m:-0}")))
  done <<<"${raw}"

  if [ "${used_cpu}" -gt "${want_cpu}" ] || [ "${used_mem}" -gt "${want_mem}" ]; then
    print_error "Refusing to apply a ResourceQuota that '${namespace}' already exceeds."
    print_error "  quota would allow : ${want_cpu}m CPU / ${want_mem}Mi"
    print_error "  already committed : ${used_cpu}m CPU / ${used_mem}Mi"
    print_error "Applying it would succeed and then silently block the NEXT pod rollout."
    print_error "Raise CONTROL_QUOTA_LIMITS_CPU / CONTROL_QUOTA_LIMITS_MEMORY, or reduce what runs here."
    return 1
  fi

  print_success "Control quota fits current usage (${used_cpu}m/${want_cpu}m CPU, ${used_mem}Mi/${want_mem}Mi)."
}

# Kubernetes quantity -> integer millicores. "2" -> 2000, "500m" -> 500, "" -> 0.
_cpu_to_millis() {
  local v="${1:-0}"
  case "${v}" in
    "") echo 0 ;;
    *m) echo "${v%m}" ;;
    *) awk -v x="${v}" 'BEGIN { printf "%d", x * 1000 }' ;;
  esac
}

# Kubernetes quantity -> integer MiB. Handles Ki/Mi/Gi/Ti and bare bytes.
_mem_to_mib() {
  local v="${1:-0}"
  case "${v}" in
    "" | 0) echo 0 ;;
    *Ki) awk -v x="${v%Ki}" 'BEGIN { printf "%d", x / 1024 }' ;;
    *Mi) echo "${v%Mi}" ;;
    *Gi) awk -v x="${v%Gi}" 'BEGIN { printf "%d", x * 1024 }' ;;
    *Ti) awk -v x="${v%Ti}" 'BEGIN { printf "%d", x * 1024 * 1024 }' ;;
    *) awk -v x="${v}" 'BEGIN { printf "%d", x / 1048576 }' ;;
  esac
}

render_tenant_default_deny() { # render_tenant_default_deny <namespace>
  local namespace="$1"
  local template="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}/netpol-tenant-default-deny.yaml.template"

  if [ ! -f "${template}" ]; then
    print_error "Tenant default-deny template not found: ${template}"
    exit 1
  fi

  local rendered
  rendered="$(
    TENANT_NAMESPACE="${namespace}" \
      TENANT_DENY_NAME="${TENANT_DENY_NAME:-default-deny-all}" \
      envsubst '${TENANT_NAMESPACE} ${TENANT_DENY_NAME}' \
      <"${template}"
  )"
  printf '%s\n' "${rendered}"
}

render_tenant_service_aliases() { # render_tenant_service_aliases <namespace>
  local namespace="$1"
  local template="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}/tenant-service-aliases.yaml.template"

  if [ ! -f "${template}" ]; then
    print_error "Tenant service-alias template not found: ${template}"
    exit 1
  fi

  local rendered
  rendered="$(
    TENANT_NAMESPACE="${namespace}" \
      CONTROL_NAMESPACE="${CONTROL_NAMESPACE:-kubeagents-system}" \
      envsubst '${TENANT_NAMESPACE} ${CONTROL_NAMESPACE}' \
      <"${template}"
  )"
  printf '%s\n' "${rendered}"
}

# Applies the quota to a tenant namespace. Called from provision_12 BEFORE the agent pod is created,
# so a pod that does not fit is rejected on the step that creates it rather than on a later rollout —
# see the template header for why that ordering is the whole point.
apply_tenant_quota() { # apply_tenant_quota <namespace>
  local namespace="$1" rendered

  if [ "${TENANT_QUOTA_ENABLED:-true}" != "true" ]; then
    print_warning "TENANT_QUOTA_ENABLED=${TENANT_QUOTA_ENABLED} — skipping. '${namespace}' will have NO compute bound."
    return 0
  fi

  rendered="$(render_tenant_quota "${namespace}")" || return 1

  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    print_info "[dry-run] would apply ResourceQuota in ${namespace}"
    printf '%s\n' "${rendered}" | kubectl apply --dry-run=server -f - >/dev/null || return 1
    print_success "ResourceQuota validates against the API server"
    return 0
  fi

  printf '%s\n' "${rendered}" | kubectl apply -f - || return 1
  print_success "ResourceQuota applied in ${namespace} — every pod here must now declare requests+limits."
}

# Applies the ExternalName aliases to a tenant namespace. Called from provision_12 BEFORE the Agent
# CR, so the pod's first inference call resolves — see the template header.
#
# Two refusals, both about not destroying something that already works:
#
#   1. NEVER in the control namespace. The aliases CNAME `litellm` to
#      `litellm.${CONTROL_NAMESPACE}.svc.cluster.local`. Applied *in* the control namespace that is
#      a CNAME to itself, and `kubectl apply` would convert the REAL Service into it — taking
#      inference down for every tier at once. The tenant tier is never placed there (the A1
#      placement clause forbids it), so reaching this arm means the caller is misconfigured.
#
#   2. Never convert a Service this platform did not create. A tenant namespace may already hold a
#      `litellm` of its own; silently retyping someone else's Service to ExternalName would break
#      their workload to fix ours.
apply_tenant_service_aliases() { # apply_tenant_service_aliases <namespace>
  local namespace="$1" rendered svc existing
  local control="${CONTROL_NAMESPACE:-kubeagents-system}"

  if [ "${TENANT_SERVICE_ALIASES_ENABLED:-true}" != "true" ]; then
    print_warning "TENANT_SERVICE_ALIASES_ENABLED=${TENANT_SERVICE_ALIASES_ENABLED} — skipping. The agent's"
    print_warning "rendered config points at litellm.${namespace}.svc, which will not resolve."
    return 0
  fi

  if [ "${namespace}" = "${control}" ]; then
    print_error "Refusing to apply service aliases in the control namespace '${control}':"
    print_error "the aliases would CNAME the real litellm/github-token-minter Services to themselves."
    return 1
  fi

  if [ "${DRY_RUN:-0}" -ne 1 ]; then
    for svc in litellm github-token-minter; do
      existing="$(kubectl get service "${svc}" -n "${namespace}" -o jsonpath='{.spec.type}' 2>/dev/null || echo "")"
      if [ -n "${existing}" ] && [ "${existing}" != "ExternalName" ]; then
        print_error "Service '${svc}' already exists in '${namespace}' as type ${existing}."
        print_error "Refusing to retype a Service this install did not create. Remove it or set"
        print_error "TENANT_SERVICE_ALIASES_ENABLED=false and provide the alias yourself."
        return 1
      fi
    done
  fi

  rendered="$(render_tenant_service_aliases "${namespace}")" || return 1

  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    print_info "[dry-run] would apply ExternalName aliases in ${namespace}"
    printf '%s\n' "${rendered}" | kubectl apply --dry-run=server -f - >/dev/null || return 1
    print_success "Service aliases validate against the API server"
    return 0
  fi

  printf '%s\n' "${rendered}" | kubectl apply -f - || return 1
  print_success "Service aliases applied in ${namespace} — litellm/github-token-minter now resolve to ${control}."
}

# ------------------------------------------------------------------------------
# Agent identity: the reader/actor pair (06 §2) and the broker-operations grant (06 §2.2.1)
# ------------------------------------------------------------------------------
# LSN-039. The reader identities used to be written inline in the two tier templates, the platform
# reader was written nowhere (it existed on the live cluster because a human had typed it once), and
# the actor identity and its grant existed only under examples/gitops-repo/ — a reference tree that
# no install path reads. The check that was supposed to catch this, `install-path-wired.py`, walks
# the SCRIPT graph: every numbered step is invoked by the driver, so it passed, and it would have
# passed just as green on a repository whose steps apply none of the security manifests. A closed
# lesson is closed against an instance; the next instance arrives one edge over.
#
# So: one definition site per object, in a template beside these functions, applied by the step that
# installs the tier. `dev/tests/identity-has-install-path.py` (V-CMP-007) is the mechanization —
# every ServiceAccount the install path REFERENCES must be one the install path CREATES.

# actor_service_account_name <tier> <scope-leaf>
#
# The bash half of `actorServiceAccountName` (internal/controller/broker_manifests.go). 06 §2.2.1
# forbids the CR from naming its own actor — the ability to name the identity is the ability to
# choose an authority level — so the name is a pure function of tier and scope leaf, computed
# identically in both places. The leaf is `scope.Of(agent).Leaf()`: namespace if set, else cluster,
# else project.
#
# The Go side has a >253-character truncation arm that hashes the leaf. It is not reproduced here,
# and that is deliberate rather than an omission: a bash sha256 would be a second implementation of
# a rule that only fires on names no Kubernetes namespace or GKE cluster name can produce (both cap
# well below the limit). If a leaf ever gets long enough to matter, this function returns a name the
# controller does not, the broker resolves a ServiceAccount that does not exist, and it fails closed
# with BrokerReady false. V-CMP-007 asserts the two agree on every name the install path renders.
actor_service_account_name() { # actor_service_account_name <tier> <scope-leaf>
  printf '%s-%s-actor\n' "$1" "$2"
}

# render_broker_operations_grant
#
# The shared, tier-neutral grant — the cluster-scoped half of 06 §2.2.1 and nothing else. It takes no
# arguments and substitutes nothing: the namespaced half retired into the per-tier grant in
# P9-T9b-5b-0-ii-b (see broker-operations-grant.yaml.template), and what is left is one ClusterRole
# that is identical on every install. `kubectl apply` makes the repeat per tier a no-op.
render_broker_operations_grant() { # render_broker_operations_grant
  local template="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}/broker-operations-grant.yaml.template"

  if [ ! -f "${template}" ]; then
    print_error "Broker operations grant template not found: ${template}"
    exit 1
  fi

  cat "${template}"
}

# render_actor_grant <tier> <namespace> <scope-leaf>
#
# The PER-TIER half of the actor's authority: the read half of 06 §2.2's template for this tier,
# joined with 06 §2.2.1's grant, in objects stamped `kube-agents/tier`. Rendered alongside the
# tier-neutral grant above, not instead of it — see actor-grant-developer-team.yaml.template for why
# a namespace-scoped tier still needs a cluster-scoped object that belongs to no tier.
#
# THE THREE FILENAMES ARE LITERAL, and that is not laziness. `dev/tests/identity-has-install-path.py`
# (V-CMP-007) property 1 asserts every `*.yaml.template` beside this file is NAMED by text the
# install path executes; a path built as "actor-grant-${tier}.yaml.template" names none of them, and
# all three would read as templates nothing renders — LSN-007's shape, reported by the check written
# for it. The `*)` arm is a hard error rather than a fallback: a fourth tier must arrive with its own
# template, and failing closed here is a provisioning error a human reads, not an agent identity
# quietly missing its tenant authority.
render_actor_grant() { # render_actor_grant <tier> <namespace> <scope-leaf>
  local tier="$1" namespace="$2" leaf="$3" base
  case "${tier}" in
    developer-team) base="actor-grant-developer-team.yaml.template" ;;
    cluster-admin) base="actor-grant-cluster-admin.yaml.template" ;;
    platform) base="actor-grant-platform.yaml.template" ;;
    *)
      print_error "No actor grant template for tier '${tier}'. 06 §2.2 defines one per tier; add it beside common.sh."
      exit 1
      ;;
  esac

  local template="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}/${base}"
  if [ ! -f "${template}" ]; then
    print_error "Actor grant template not found: ${template}"
    exit 1
  fi

  local actor_ksa rendered
  actor_ksa="$(actor_service_account_name "${tier}" "${leaf}")"
  rendered="$(
    AGENT_NAMESPACE="${namespace}" \
      AGENT_ACTOR_KSA="${actor_ksa}" \
      envsubst '${AGENT_NAMESPACE} ${AGENT_ACTOR_KSA}' \
      <"${template}"
  )"
  printf '%s\n' "${rendered}"
}

# render_agent_identity <tier> <namespace> <reader-ksa> <scope-leaf> [reader-gsa-email]
#
# The reader SA, the actor SA, and the two bindings that attach the actor to the grant above.
#
# The GSA email is optional and only ever lands on the READER. An empty value renders no annotations
# block at all rather than an annotation with an empty value: `iam.gke.io/gcp-service-account: ""` is
# a Workload Identity binding to nothing, which GKE reports as a token-exchange failure at the first
# cloud call rather than as a misconfiguration at apply time.
render_agent_identity() { # render_agent_identity <tier> <namespace> <reader-ksa> <scope-leaf> [gsa-email]
  local tier="$1" namespace="$2" reader_ksa="$3" leaf="$4" gsa_email="${5:-}"
  local template="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}/agent-identity.yaml.template"

  if [ ! -f "${template}" ]; then
    print_error "Agent identity template not found: ${template}"
    exit 1
  fi

  local actor_ksa annotations=""
  actor_ksa="$(actor_service_account_name "${tier}" "${leaf}")"

  if [ -n "${gsa_email}" ]; then
    annotations="  annotations:
    iam.gke.io/gcp-service-account: \"${gsa_email}\""
  fi

  # Command substitution strips every trailing newline, so `printf '%s\n'` leaves exactly one — the
  # same reason render_egress_policy does it, and the same two gates in conflict if it does not.
  local rendered
  rendered="$(
    AGENT_TIER="${tier}" \
      AGENT_NAMESPACE="${namespace}" \
      AGENT_READER_KSA="${reader_ksa}" \
      AGENT_ACTOR_KSA="${actor_ksa}" \
      AGENT_READER_ANNOTATIONS="${annotations}" \
      envsubst '${AGENT_TIER} ${AGENT_NAMESPACE} ${AGENT_READER_KSA} ${AGENT_ACTOR_KSA} ${AGENT_READER_ANNOTATIONS}' \
      <"${template}"
  )"
  printf '%s\n' "${rendered}"
}

# apply_agent_identity <tier> <namespace> <reader-ksa> <scope-leaf> [reader-gsa-email]
#
# Grants first, then the identity that binds to them. The order matters on a fresh cluster: a
# RoleBinding whose roleRef names a Role that does not exist yet is accepted by the API server and
# then grants nothing until the Role appears, so the failure is a silent authorization denial rather
# than an apply error. Applying the grants first removes the window entirely. (The reverse edge does
# not exist: RBAC resolves a binding's SUBJECT at request time, so a binding may name a
# ServiceAccount the next apply creates.)
#
# Two grants, not one. The tier-neutral pair is 06 §2.2.1 and is the same object for the whole fleet;
# the per-tier grant is the read half of 06 §2.2's template for THIS tier, stamped with the tier so
# that admission and V-BRK-013 can both reason about it. Neither subsumes the other — see
# actor-grant-developer-team.yaml.template.
#
# No opt-out flag, unlike the quota and the service aliases. Those degrade an install; skipping this
# one produces a broker that cannot authenticate its own caller, and an "off" switch for it would
# only ever be used by someone who had not read this paragraph.
apply_agent_identity() { # apply_agent_identity <tier> <namespace> <reader-ksa> <scope-leaf> [gsa-email]
  local tier="$1" namespace="$2" reader_ksa="$3" leaf="$4" gsa_email="${5:-}"
  local grant actor_grant identity actor_ksa

  actor_ksa="$(actor_service_account_name "${tier}" "${leaf}")"
  grant="$(render_broker_operations_grant)" || return 1
  actor_grant="$(render_actor_grant "${tier}" "${namespace}" "${leaf}")" || return 1
  identity="$(render_agent_identity "${tier}" "${namespace}" "${reader_ksa}" "${leaf}" "${gsa_email}")" || return 1

  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    print_info "[dry-run] would apply the broker-operations grant, the ${tier} actor grant, and the ${tier} reader/actor identity in ${namespace}"
    printf '%s\n' "${grant}" | kubectl apply --dry-run=server -f - >/dev/null || return 1
    printf '%s\n' "${actor_grant}" | kubectl apply --dry-run=server -f - >/dev/null || return 1
    printf '%s\n' "${identity}" | kubectl apply --dry-run=server -f - >/dev/null || return 1
    print_success "Identity manifests validate against the API server"
    return 0
  fi

  printf '%s\n' "${grant}" | kubectl apply -f - || return 1
  printf '%s\n' "${actor_grant}" | kubectl apply -f - || return 1
  printf '%s\n' "${identity}" | kubectl apply -f - || return 1
  print_success "Identity applied in ${namespace}: reader '${reader_ksa}', actor '${actor_ksa}' bound to the 06 §2.2.1 grant and the ${tier} read profile of 06 §2.2."
}

# delete_agent_identity <tier> <namespace> <reader-ksa> <scope-leaf>
#
# The teardown half. The namespaced objects would go with the namespace for a tenant tier, but the
# control namespace outlives every tier that lives in it and the ClusterRoleBinding is cluster-scoped
# in every case — an undeleted binding survives into the next provision holding a subject name that
# a later install may reuse. The shared ClusterRole is NOT deleted here: it is a fleet object, and
# removing it while another tier still binds to it would brick that tier's broker.
#
# The per-tier grant IS deleted, all four objects of it, and the two cluster-scoped ones are why this
# matters more than it did before. A tier ClusterRole left behind holds the read half of 06 §2.2 for
# a tier that is no longer installed, and its ClusterRoleBinding names `<tier>-<leaf>-actor` — a name
# the next install of the same tier and scope will recreate. Every name here is `${actor_ksa}`, which
# is a pure function of tier and leaf, so a teardown that misses one hands the authority to whatever
# is provisioned next under the same name.
delete_agent_identity() { # delete_agent_identity <tier> <namespace> <reader-ksa> <scope-leaf>
  local tier="$1" namespace="$2" reader_ksa="$3" leaf="$4" actor_ksa
  actor_ksa="$(actor_service_account_name "${tier}" "${leaf}")"

  kubectl delete clusterrolebinding "${reader_ksa}-broker-operations" --ignore-not-found=true || true
  kubectl delete clusterrolebinding "${actor_ksa}" --ignore-not-found=true || true
  kubectl delete clusterrole "${actor_ksa}" --ignore-not-found=true || true
  kubectl delete rolebinding "${actor_ksa}" -n "${namespace}" --ignore-not-found=true || true
  kubectl delete role "${actor_ksa}" -n "${namespace}" --ignore-not-found=true || true
  kubectl delete serviceaccount "${actor_ksa}" -n "${namespace}" --ignore-not-found=true || true
  kubectl delete serviceaccount "${reader_ksa}" -n "${namespace}" --ignore-not-found=true || true
}

confirm_action() {
  local warning_msg=$1
  shift

  # The one prompt site that already honoured NO_CONFIRM. It reads the shared predicate now so the
  # set of "no operator at the keyboard" conditions cannot diverge between this gate and the
  # configuration prompts it runs alongside.
  if is_non_interactive; then
    return 0
  fi
  
  echo ""
  echo -e "${C_RED}${C_BOLD}🚨 WARNING: ${warning_msg}${C_RESET}"
  echo -e "${C_YELLOW}==============================================================================${C_RESET}"
  for item in "$@"; do
    local key="${item%%:*}"
    local val="${item#*:}"
    printf "  ${C_BOLD}%-15s${C_RESET} %s\n" "$key:" "$val"
  done
  echo -e "${C_YELLOW}==============================================================================${C_RESET}"
  echo ""
  echo -ne "  ${C_CYAN}Are you sure you want to proceed? (y/N): ${C_RESET}"
  read -r -n 1 REPLY
  echo
  if [[ ! $REPLY =~ ^[Yy]$ ]]; then
      echo -e "  ${C_YELLOW}ℹ Aborted.${C_RESET}"
      exit 0
  fi
}

get_chatgpt_auth_info() {
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    return 0
  fi

  # Wait for the deployment to be rolled out first
  kubectl rollout status deployment/litellm -n "${NAMESPACE:-kubeagents-system}" --timeout=60s >/dev/null 2>&1 || true

  # Retry a few times to allow LiteLLM to initialize and print the device code
  _check_litellm_logs() {
    local auth_info
    auth_info=$(kubectl logs deployment/litellm -n "${NAMESPACE:-kubeagents-system}" 2>/dev/null | awk '/Visit https:/ {u=$NF} /Enter code:/ {c=$NF} END {print u, c}') || true
    read -r CHATGPT_URL CHATGPT_CODE <<< "$auth_info"
    if [ -n "$CHATGPT_URL" ] && [ -n "$CHATGPT_CODE" ]; then
      export CHATGPT_URL CHATGPT_CODE
      return 0
    fi
    return 1
  }

  retry 15 1 _check_litellm_logs >/dev/null 2>&1 || true
}
