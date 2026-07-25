---
title: Provisioning scripts
description: The modular sub-scripts that make up `./provision.sh` and their teardown counterparts.
sidebar:
  order: 3
---

The provisioner in [`k8s-operator/scripts/`](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator/scripts) is composed of one orchestrator (`provision.sh`) and a set of idempotent step scripts (plus their teardown mirrors and an optional gVisor step). This page catalogs each step; the [quick start](/kube-agents/install/quickstart-gke/) shows the operator's-eye view.

Shared state — cluster name, region, project ID, model provider, GitOps repo — lives in `k8s-operator/scripts/vars.sh` (git-ignored). Each script sources it; missing values prompt the user and get appended to `vars.sh`.

## Orchestrators

- **[`provision.sh`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/scripts/provision.sh)** — runs the numbered steps in order (skipping opt-in steps unless enabled).
- **[`teardown.sh`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/scripts/teardown.sh)** — runs the steps in reverse.

Both accept `--dry-run` to print planned actions without applying them.

## Provisioning steps

### 01. GKE cluster

`provision_01_gcp_cluster.sh` — Enables the required GCP APIs, provisions a GKE Standard cluster with Workload Identity, sets `kubectl` credentials, and creates the target namespace (`kubeagents-system`).

### 02. gVisor node pool (opt-in)

`provision_02_gvisor_nodepool.sh` — Only runs if `ENABLE_GVISOR=true`. Provisions a dedicated GKE Sandbox (gVisor) node pool (`gvisor-pool` by default, overridable via `GVISOR_POOL_NAME`) for sandboxed skill execution.

### 03. Operator CRDs + controller

`provision_03_gcp_gke_operator.sh` — Installs the `Agent` CRD and deploys the operator controller manager into the cluster.

### 04. IAM + Workload Identity

`provision_04_gcp_iam.sh` — Creates GSAs for the controller and Platform Agent, binds Kubernetes SAs to them via Workload Identity, and grants read-only GKE permissions (`read-only` (default) or `custom`). The Platform Agent is read-only at the cloud boundary; the retired `gke-admin` preset is coerced to `read-only` and any stale admin bindings are removed on the next run.

### 05. Google Chat Pub/Sub

`provision_05_gcp_gchat.sh` — Creates the Pub/Sub topic and subscription that the Google Chat app publishes events into. Prints the topic name for you to configure in the Chat API console.

### 06. Slack (opt-in)

`provision_06_slack.sh` — Only configures Slack if `SLACK_ENABLED=true`. Collects bot token, app token, allowed users, and home channel, and stores them as Kubernetes secrets. `SLACK_ALLOWED_USERS` is required when Slack is enabled: an empty allowlist is rejected by admission, not silently opened up.

### 07. LLM API key Secret

`provision_07_gcp_k8s_secrets.sh` — Prompts for the model provider (`gemini` / `anthropic` / `openai`) and API key, and creates the `platform-agent-secrets` Secret in the target namespace.

### 08. Platform-tier Agent CR

`provision_08_deploy_platform_agent.sh` — Renders `platform-agent.yaml` from a template (via `envsubst`), then `kubectl apply`s the platform-tier `Agent` CR to trigger the operator's reconciliation.

### 09. LiteLLM Gateway

`provision_09_deploy_litellm.sh` — Deploys the LiteLLM Deployment + Service. The `Agent` config references this Service (`litellm`, port 80 → container port 4000) as its Completions API endpoint.

### 10. Minty (GitHub Token Minter)

`provision_10_deploy_github_minter.sh` — Sets up a GCP KMS keyring + key for token signing, then deploys Minty. See the [Token minter](/kube-agents/deploy/token-minter/) deploy page for details.

### 11. Inference replay (opt-in)

`provision_11_deploy_inference_replay.sh` — Only runs if `INFERENCE_REPLAY_ENABLED=true`. Deploys the [inference-replay proxy](/kube-agents/concepts/inference-gateway/#inference-replay) with a PVC for the cache and re-points the `litellm` Service to route through the proxy.

### 12. Child agent tiers

`provision_12_deploy_agent_tiers.sh` — Step 08 deploys the platform tier; this step adds the two tiers below it, so a fresh install exercises the full hierarchy instead of a single agent. For each of the **cluster-admin** and **developer-team** tiers it creates the in-cluster identity, the API-server secret, and the `Agent` CR. The GSAs and Workload Identity bindings come from step 04.

Set `CLUSTER_ADMIN_ENABLED=false` to skip the cluster-admin tier, or `DEVELOPER_TEAM_NAMESPACE=''` to skip the tenant tier. The developer-team tier requires the cluster-admin tier — the webhook rejects a child whose `parentRef` does not resolve.

### 13. Network policies

`provision_13_apply_network_policies.sh` — Applies the per-tier egress allowlist to each agent, then the tenant namespace's default-deny floor. Runs last, after every tier exists: allowlist first and floor second, because floor-first would cut a Ready agent pod off from DNS and inference for as long as the next `kubectl` call takes.

:::caution[Enforcement is a property of the CNI, not of this step]
A cluster accepts `NetworkPolicy` objects whether or not it can enforce them. On GKE, enforcement needs **Dataplane V2 or Calico**; kindnet accepts every policy and enforces nothing. The step reports which case it is in rather than implying containment it cannot deliver, and the verification harness treats an egress claim on a non-enforcing dataplane as `deferred`, never `pass`.
:::

Knobs: `EGRESS_POLICIES_ENABLED=false` to skip entirely, `WORKLOAD_IDENTITY_ENABLED=true` to append the narrow metadata-server allow, `GKE_DATAPLANE=auto|v1|v2`, and `HUB_INFERENCE_CIDR` / `HUB_MINTY_CIDR` / `MCP_GROUNDING_CIDRS` for remote-hub topology.

## Teardown steps

Mirror the provisioning steps in reverse. Full table on [Uninstall](/kube-agents/install/uninstall/).

## Development helpers (`dev/`)

- **[`dev/dev_rebuild_agent.sh`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/scripts/dev/dev_rebuild_agent.sh)** — Fast local iteration on the Platform Agent workspace image.
- **[`dev/teardown_dev_01_gcp_artifact_registry.sh`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/scripts/dev/teardown_dev_01_gcp_artifact_registry.sh)** — Deletes the dev-only Artifact Registry created by `dev_rebuild_agent.sh`.

## Common gotchas

- **cert-manager.** The operator's admission webhook needs it. Step 03 installs it for you when `certificates.cert-manager.io` is absent, so there is nothing to do by hand; on Autopilot it is deployed with leader election disabled.
- **`vars.sh` collision.** If you rerun the provisioner against a different project without wiping `vars.sh`, you'll target the previous project. Delete `vars.sh` to reset.
- **Autopilot leader election.** cert-manager on Autopilot needs leader election disabled — see [Prerequisites](/kube-agents/install/prerequisites/#gke-autopilot-install).
