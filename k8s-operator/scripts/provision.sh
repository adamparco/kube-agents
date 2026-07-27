#!/usr/bin/env bash
# ==============================================================================
# 🤖 Master GKE Standard & Cloud-Agnostic Operator E2E Provisioner
# ==============================================================================
# Orchestrates GCP/GKE bootstrapping, operator and agent container builds,
# manual GSA/PubSub setup, IAM configuration, and CR application.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "${SCRIPT_DIR}/common.sh" "$@"

# Flags that must reach the 13 child scripts.
#
# Each child re-parses its own argv (`source common.sh "$@"`) and resets DRY_RUN and NO_CONFIRM to
# 0 before doing so, so a flag this orchestrator merely *holds in a variable* does not reach them.
# It has to be forwarded explicitly. `--dry-run` always was; `--no-confirm` was not, and that gap
# is what made `--no-confirm` a promise the pipeline could not keep: provision.sh suppressed its
# own confirmation and then handed every child a fully interactive run.
#
# The visible cost was `make live-refresh ARGS="--yes"` on 2026-07-26 — seven images built, vars.sh
# pinned, operator rolled, and then a hard stop at step 06 of 13 on a Slack token prompt with no
# stdin. The install was left with a new operator and three agent tiers on the previous build.
#
# An array, not a string: `$DRY_RUN_ARG` was unquoted to let the empty case vanish, which is a
# word-splitting bug waiting for the first flag that contains a space.
STEP_ARGS=()
if [ "$DRY_RUN" -eq 1 ]; then
  STEP_ARGS+=("--dry-run")
fi
if [ "${NO_CONFIRM:-0}" -eq 1 ]; then
  STEP_ARGS+=("--no-confirm")
fi

echo -e "${C_MAGENTA}${C_BOLD}🚀 Starting GKE Platform Agent provisioning pipeline...${C_RESET}"

"${SCRIPT_DIR}/provision_01_gcp_cluster.sh" "${STEP_ARGS[@]}"
"${SCRIPT_DIR}/provision_02_gvisor_nodepool.sh" "${STEP_ARGS[@]}"
"${SCRIPT_DIR}/provision_03_gcp_gke_operator.sh" "${STEP_ARGS[@]}"
"${SCRIPT_DIR}/provision_04_gcp_iam.sh" "${STEP_ARGS[@]}"
"${SCRIPT_DIR}/provision_05_gcp_gchat.sh" "${STEP_ARGS[@]}"
"${SCRIPT_DIR}/provision_06_slack.sh" "${STEP_ARGS[@]}"
"${SCRIPT_DIR}/provision_07_gcp_k8s_secrets.sh" "${STEP_ARGS[@]}"
"${SCRIPT_DIR}/provision_08_deploy_platform_agent.sh" "${STEP_ARGS[@]}"
"${SCRIPT_DIR}/provision_09_deploy_litellm.sh" "${STEP_ARGS[@]}"
"${SCRIPT_DIR}/provision_10_deploy_github_minter.sh" "${STEP_ARGS[@]}"
"${SCRIPT_DIR}/provision_11_deploy_inference_replay.sh" "${STEP_ARGS[@]}"
"${SCRIPT_DIR}/provision_12_deploy_agent_tiers.sh" "${STEP_ARGS[@]}"
# Containment lands last, once every tier it governs exists — applying a default-deny policy to a
# namespace whose Services are not up yet turns a slow rollout into a confusing one.
"${SCRIPT_DIR}/provision_13_apply_network_policies.sh" "${STEP_ARGS[@]}"

echo -e "\n${C_MAGENTA}${C_BOLD}>>>  Infrastructure & Cloud Resources Provisioned Successfully!  <<<${C_RESET}"

load_state

echo -e "${C_YELLOW}${C_BOLD}======================= START COPY&PASTE =======================${C_RESET}"
echo -e "${C_YELLOW}Your Kubernetes Operator and Custom Resources are ready!${C_RESET}"
echo -e "Next steps to run the operator and interact with your bot:\n"

"${SCRIPT_DIR}/print_instructions_gchat.sh" "${STEP_ARGS[@]}"
"${SCRIPT_DIR}/print_instructions_slack.sh" "${STEP_ARGS[@]}"

echo -e "${C_CYAN}${C_BOLD}--- [General Operator & Deployment Next Steps] ---${C_RESET}"
echo -e "[ ] Run the new Operator manager locally or deploy it:"
echo -e "       To run locally: ${C_WHITE}ENABLE_WEBHOOKS=false make run${C_RESET} (from k8s-operator directory)"
echo -e "       To deploy to cluster: ${C_WHITE}make deploy IMG=<your-docker-registry>/kube-agents-operator:latest${C_RESET}"
echo -e ""

echo -e "[ ] Monitor Gateway pod rollout progress:"
echo -e "       ${C_WHITE}kubectl get pods -n ${NAMESPACE:-kubeagents-system}${C_RESET}"
echo -e ""

if [ "$MODEL_PROVIDER" = "chatgpt" ]; then
  get_chatgpt_auth_info
  echo -e ""
  echo -e "[ ] ${C_YELLOW}Complete ChatGPT OAuth Device Flow Authentication:${C_RESET}"
  echo -e "       Because you selected 'chatgpt' as the model provider, LiteLLM must be authenticated"
  echo -e "       via OpenAI's OAuth Device Flow. Please follow these steps to authenticate:"
  if [ -n "$CHATGPT_URL" ] && [ -n "$CHATGPT_CODE" ]; then
    echo -e "       - Open your browser and navigate to: ${C_CYAN}${CHATGPT_URL}${C_RESET}"
    echo -e "       - Enter the code: ${C_CYAN}${CHATGPT_CODE}${C_RESET} and log in to authorize the device."
  else
    echo -e "       - View the LiteLLM gateway logs to check the authentication instructions:"
    echo -e "         ${C_CYAN}kubectl logs -n ${NAMESPACE:-kubeagents-system} deployment/litellm -f${C_RESET}"
    echo -e "       - Follow the instructions displayed in the logs to authorize the device."
  fi
  echo -e "       - Once authorized, the LiteLLM gateway will automatically pair with your ChatGPT subscription."
  echo -e ""
fi

echo -e "======================== END COPY&PASTE ========================\n"
