# Provisioning & Teardown Scripts Reference

This directory contains the automation scripts for provisioning and tearing down the GCP and GKE infrastructure required by the `kube-agents` platform agent and operator.

## Architecture & Configuration Flow

All scripts are modular and idempotent. They share a single configuration state stored in a local [vars.sh](vars.sh) file (which is git-ignored).

Start from the annotated template — it documents every variable, including the ones that are
easy to get wrong (image registry and region, the Minty organization requirement, Slack
allowlists):

```bash
cp vars.sh.example vars.sh && $EDITOR vars.sh
```

A fully populated `vars.sh` makes the whole pipeline non-interactive; anything left unset is
prompted for on first run.

When any script is run:

1. It checks if [vars.sh](vars.sh) exists.
2. If any required variables are missing, the script prompts the user for them, exports them, and appends them to [vars.sh](vars.sh).
3. If they are already defined in [vars.sh](vars.sh), the script sources them and runs non-interactively.

> [!NOTE]
> Because the provisioning scripts persist configuration state in [vars.sh](vars.sh), running the script again will reuse the same options selected on the first run. If you want to change configuration variables, manually edit [vars.sh](vars.sh) or perform a teardown first.

---

## File Directory

### Orchestration Scripts

- **[provision.sh](provision.sh)**: Master script that coordinates the sequential execution of all core provisioning steps. It deploys the images named by `OPERATOR_IMAGE` / `ROUTER_IMAGE` / `AGENT_IMAGE` + `AGENT_TAG`; **no provisioning step builds an image**.
- **[live_refresh.sh](live_refresh.sh)** (`make live-refresh`): The build half plus `provision.sh`, for refreshing an install that already exists. Builds all seven first-party images on Cloud Build, confirms each tag resolves in Artifact Registry, writes the pins into `vars.sh`, runs the pipeline, and then compares every running container's `imageID` against the digests it published. Reads the target cluster from `vars.sh` and requires the cluster name typed back (`--yes` to skip); refuses `gke-scratch-*`, which belongs to `dev/cluster/reload-images.sh`. Exit codes: `0` ok, `1` usage, `2` refused, `3` missing tooling or config, `4` an image did not build or is absent from the registry, `5` published but the cluster did not converge.
- **[teardown.sh](teardown.sh)**: Master script that coordinates the teardown steps in reverse order (conditionally including auxiliary scripts).

#### Provisioning Steps

1. **[provision_01_gcp_cluster.sh](provision_01_gcp_cluster.sh)**
   - Sets up initial project configs.
   - Enables GKE Service API (`container.googleapis.com`).
   - Provisions a GKE Standard Cluster with Workload Identity enabled.
   - Points `kubectl` credentials to the new cluster and creates the target namespace.
2. **[provision_02_gvisor_nodepool.sh](provision_02_gvisor_nodepool.sh)**
   - Provisions a dedicated GKE Sandbox (gVisor) node pool (defaults to `gvisor-pool`, configurable via `GVISOR_POOL_NAME`). Executed automatically if `ENABLE_GVISOR=true`.
3. **[provision_03_gcp_gke_operator.sh](provision_03_gcp_gke_operator.sh)**
   - Installs `cert-manager` (`v1.14.4`) if not present (including leader-election compatibility patching for GKE Autopilot clusters).
   - Installs Custom Resource Definitions (CRDs) for `Agent`.
   - Deploys the Operator controller manager into the GKE cluster, honouring `OPERATOR_IMAGE` and `ROUTER_IMAGE` from [vars.sh](vars.sh). Leave them unset to run the published `ghcr.io/gke-labs` images; set them to run images built from this source tree.
   - Applies the agent admission policies (`kube-agents-agent-readonly`, `kube-agents-agent-pod-hardening`). These are cluster-scoped and applied **before** any `Agent` CR, so a write-capable tier role or an unhardened agent pod is rejected at admission rather than grandfathered in.
   - Configures **kage-router**: parked at 0 replicas unless `GOOGLE_CHAT_ENABLED=true` and `CHAT_SUB_NAME` is set, since the router only drains an inbound Chat subscription and would otherwise crash-loop on its placeholder configuration.
4. **[provision_04_gcp_iam.sh](provision_04_gcp_iam.sh)**
   - Enables GCP Service APIs (`container.googleapis.com` and `cloudresourcemanager.googleapis.com`).
   - Pre-provisions GCP Service Accounts (GSAs) for the Platform Agent and conditionally for the GitHub Token Minter.
   - Configures Workload Identity policy bindings mapping the Kubernetes SAs to the GCP GSAs.
   - Grants read-only GKE and monitoring permissions to the Platform Agent GSA based on the selected permission set (`read-only` (default) or `custom`). The agent is read-only at the cloud boundary — the retired `gke-admin` preset is coerced to `read-only`, and any stale admin bindings are actively removed.
   - Configures Workload Identity policy bindings and annotations for the GitHub Token Minter GSA/KSA if GitHub integration is configured.
   - Creates the viewer-only GSAs and Workload Identity bindings for the **cluster-admin** and **developer-team** tiers, and for the **kage-router** (`roles/pubsub.subscriber` only, and only when `GOOGLE_CHAT_ENABLED=true`). Child tiers are read-only at the cloud boundary just like the platform tier.
5. **[provision_05_gcp_gchat.sh](provision_05_gcp_gchat.sh)**
   - Enables GCP Service APIs (`pubsub.googleapis.com` and `chat.googleapis.com`).
   - Sets up the Pub/Sub Topic and Subscription for Google Chat events (skipped if `GOOGLE_CHAT_ENABLED=false`).
   - Configures IAM policy bindings allowing the Platform Agent GSA to read incoming messages from the Pub/Sub subscription.
   - Note: Access can be restricted to specific users by configuring `GOOGLE_CHAT_ALLOWED_USERS`.
6. **[provision_06_slack.sh](provision_06_slack.sh)**
   - Configures Slack integration parameters, bot tokens, app tokens, and home channel settings (skipped if `SLACK_ENABLED=false`).
   - **Note:** You must create a Slack App and obtain tokens before running this. [See the Slack App Setup Guide](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/slack).
   - Note: Access can be restricted to specific users by configuring `SLACK_ALLOWED_USERS`.
7. **[provision_07_gcp_k8s_secrets.sh](provision_07_gcp_k8s_secrets.sh)**
   - Prompts for/reads the `MODEL_PROVIDER` and corresponding `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, or `OPENAI_API_KEY`.
   - Generates a secure random `API_SERVER_KEY` if not already set.
   - Creates the Kubernetes Secret (`platform-agent-secrets`) containing model API keys, the server key, and Slack tokens directly in the target GKE namespace.
   - Creates the Kubernetes Secret (`github-app-credentials`) if `GITHUB_APP_ID` is configured.
8. **[provision_08_deploy_platform_agent.sh](provision_08_deploy_platform_agent.sh)**
   - Uses `envsubst` to render `platform-agent.yaml` from its template.
   - Automatically enables the `gvisor` runtime class in the rendered manifest if `ENABLE_GVISOR=true`.
   - Applies the resulting `Agent` Custom Resource (CR) to deploy the platform agent instance.
9. **[provision_09_deploy_litellm.sh](provision_09_deploy_litellm.sh)**
   - Deploys the LiteLLM Gateway to the GKE cluster.
10. **[provision_10_deploy_github_minter.sh](provision_10_deploy_github_minter.sh)**
    - Enables Cloud KMS API (`cloudkms.googleapis.com`).
    - Sets up Google Cloud KMS keyrings and keys for token signing and grants signer/verifier roles to the Minter GSA.
    - Preflights the GitHub side: warns when the target repository has no commits, since agents deliver changes as pull requests and a repo with no default branch cannot accept one.
    - Imports the GitHub App private key (`GITHUB_PEM_PATH`) into Cloud KMS using `openssl` + `gcloud` (creates the KMS import job and waits for it to become `ACTIVE`). Deploys the highest **ENABLED** key version, so disable superseded versions after rotating.
    - Deploys the GitHub Token Minter and waits for it to become `Available`, reporting a missing KMS key version as the likely cause on failure.
    - **Requires `GITHUB_ORG` to be a real GitHub Organization** — Minty resolves installations via `GET /orgs/{org}/installation`, which 404s for personal user accounts. See [the integration README](../config/integrations/github/README.md).
11. **[provision_11_deploy_inference_replay.sh](provision_11_deploy_inference_replay.sh)**
    - Opt-in via `INFERENCE_REPLAY_ENABLED=true`; otherwise skipped.
    - Prompts for `REPLAY_IMAGE` (the proxy container image).
    - Deploys the Inference Replay proxy: PVC + ConfigMap (mode=off pass-through), Deployment, a `litellm-gateway` Service pointing at the original LiteLLM pods, and a replacement `litellm` Service routing traffic through the proxy. Toggle caching on at runtime via `kubectl patch configmap inference-replay-config -n <ns> --type merge -p '{"data":{"mode":"on"}}'`.

12. **[provision_12_deploy_agent_tiers.sh](provision_12_deploy_agent_tiers.sh)**
    - Deploys the **cluster-admin** and **developer-team** tiers below the platform agent: read-only identity, per-agent API-server Secret, and the `Agent` CR for each, applied identity-before-pod.
    - Skips with `CLUSTER_ADMIN_ENABLED=false`, or `DEVELOPER_TEAM_NAMESPACE=''` for just the tenant tier. The developer-team tier requires the cluster-admin tier, whose `parentRef` must resolve or the webhook rejects the CR.
    - Derives the child images from `AGENT_IMAGE`'s registry so a source-built install stays consistent. Without an explicit image the controller falls back to the per-tier `ghcr.io/gke-labs` default baked into the binary.

### Auxiliary & Development Scripts

- **[common.sh](common.sh)**: Shared utility functions, color output, logging, prompt helpers, and state management.
- **[platform-agent.yaml.template](platform-agent.yaml.template)**: Manifest template used by `provision_08_deploy_platform_agent.sh` to render the `Agent` Custom Resource.
- **[print_instructions_gchat.sh](print_instructions_gchat.sh)**: Helper script that prints Google Chat integration post-provisioning instructions.
- **[print_instructions_slack.sh](print_instructions_slack.sh)**: Helper script that prints Slack integration post-provisioning instructions.
- **[dev/dev_rebuild_agent.sh](dev/dev_rebuild_agent.sh)**: Fast local development utility that builds, pushes, and redeploys agent container images.

### Teardown Steps

- **[teardown_11_deploy_inference_replay.sh](teardown_11_deploy_inference_replay.sh)**: Always executed by master teardown; undeploys the proxy (including the cache PVC) if present and re-applies the LiteLLM Service manifest to restore the original selector. Idempotent no-op if the proxy was never deployed.
- **[teardown_10_deploy_github_minter.sh](teardown_10_deploy_github_minter.sh)**: Cleans up the GitHub Token Minter deployment and disables/schedules Cloud KMS key versions for destruction.
- **[teardown_09_deploy_litellm.sh](teardown_09_deploy_litellm.sh)**: Undeploys the LiteLLM Gateway from the cluster.
- **[teardown_08_deploy_platform_agent.sh](teardown_08_deploy_platform_agent.sh)**: Safely deletes the `Agent` Custom Resource and cleans up local manifests.
- **[teardown_07_gcp_k8s_secrets.sh](teardown_07_gcp_k8s_secrets.sh)**: Deletes the Kubernetes secrets in GKE.
- **[teardown_06_slack.sh](teardown_06_slack.sh)**: Resets Slack integration configuration state and tokens.
- **[teardown_05_gcp_gchat.sh](teardown_05_gcp_gchat.sh)**: Deletes the Google Chat Pub/Sub topic and subscription.
- **[teardown_04_gcp_iam.sh](teardown_04_gcp_iam.sh)**: Removes all GCP IAM policy bindings, Workload Identity mappings, and deletes the GSAs for the Platform Agent and GitHub Token Minter.
- **[teardown_03_gcp_gke_operator.sh](teardown_03_gcp_gke_operator.sh)**: Removes the Operator manager deployment and unregisters CRDs.
- **[teardown_02_gvisor_nodepool.sh](teardown_02_gvisor_nodepool.sh)**: Deletes the dedicated gVisor node pool without destroying the cluster.
- **[dev/teardown_dev_01_gcp_artifact_registry.sh](dev/teardown_dev_01_gcp_artifact_registry.sh)**: Conditionally executed by master teardown if local dev artifact registry was created.
- **[teardown_01_gcp_cluster.sh](teardown_01_gcp_cluster.sh)**: Deletes the GKE Standard cluster and removes the local state file `vars.sh`.

---

## Direct Usage Examples

Normally, these scripts are run via the parent Makefile targets. However, they can also be run directly.

### Run Provision Pipeline

Execute the master script from this directory:

```bash
./provision.sh
```

To run a dry-run check (simulates commands without modifying cloud resources):

```bash
./provision.sh --dry-run
```

### Run Teardown Pipeline

Clean up the provisioned environment:

```bash
./teardown.sh
```

### Run Specific Step

For example, if you want to update IAM configurations:

```bash
./provision_04_gcp_iam.sh
```

---

## Troubleshooting

Failure modes hit during real installs, and what they actually mean. Most present as something
unrelated to the true cause.

### Images

**Agent pods `CrashLoopBackOff` with `exec format error`.** The agent images are `amd64`. A local
`docker build` on Apple silicon produces `arm64` images the GKE nodes cannot execute. Build with
Cloud Build (`make cloud-build-push` from the repo root), which builds natively as `amd64` and
pushes straight to Artifact Registry.

**Images push to a registry the cluster never pulls from.** The root `Makefile` defaults to
`LOCATION ?= us-east4`, where this project's Artifact Registry repo lives. If your cluster and
Artifact Registry live elsewhere you must pass it explicitly, or the push silently succeeds into
the wrong region:

```bash
make docker-push LOCATION=us-central1
```

**The deploy runs published images even though you built from source.** `make deploy` falls back
to the Makefile's `IMG`/`ROUTER_IMG` defaults (`ghcr.io/gke-labs/...`). Set `OPERATOR_IMAGE` and
`ROUTER_IMAGE` in `vars.sh` — `provision_03` passes them through. For the child tiers, the
controller resolves a per-tier `ghcr.io` default unless the `Agent` CR pins
`spec.deployment.image`; `provision_12` sets it from `AGENT_IMAGE`'s registry.

**A rebuilt image doesn't take effect.** Same-tag images are not re-pulled when
`imagePullPolicy: IfNotPresent` and the tag already exists on the node. Use an immutable tag (a
git SHA) rather than `:latest`, so what is running is provable and every change forces a pull.

### Pods that never start

**`FailedCreate: ... violates PodSecurity "restricted"`.** The operator namespace is labelled
`pod-security.kubernetes.io/enforce: restricted`, so every pod in it needs `runAsNonRoot`,
`seccompProfile: RuntimeDefault`, `allowPrivilegeEscalation: false` and `capabilities.drop:
["ALL"]`. Note this only bites on a **freshly created namespace** — pods admitted before the
label existed are grandfathered, so an in-place upgrade can look healthy while a clean install
fails.

**`FailedCreate: error looking up service account`.** The agent's KSA is created by
`provision_04`, not by the controller (the controller only _references_ it — 08 §4). Deleting and
recreating the namespace removes the KSA and its Workload Identity annotation; re-run
`provision_04`.

**`Multi-Attach error for volume`.** Two agents in one namespace both mounting the same
`ReadWriteOnce` claim. Each agent gets its own `<agent-name>-system-metadata` PVC; if you see a
shared `system-metadata` claim, the controller predates that fix — rebuild it.

**`Pending` with no node.** With `ENABLE_GVISOR=true` the agent pod requests
`runtimeClassName: gvisor` and only schedules onto the gVisor node pool. Confirm the pool exists
(`provision_02`).

### kage-router

**Router `CrashLoopBackOff` with `InvalidArgument ... REPLACE_WITH_PROJECT_ID`.** The router is
the **Google Chat** front door and ships with placeholder env. With `GOOGLE_CHAT_ENABLED=false`,
`provision_03` parks it at 0 replicas; this error means that step was skipped or `make deploy`
re-applied the base manifest afterwards. Re-run `provision_03`.

**Router `PermissionDenied` on the Pub/Sub subscription.** Its KSA needs a Workload Identity
annotation pointing at a GSA holding `roles/pubsub.subscriber` (`provision_04` step 6, which only
runs when `GOOGLE_CHAT_ENABLED=true`).

### Minty / GitHub

**The agent reports it cannot resolve `github-token-minter...svc.cluster.local`.** Usually Minty
is not deployed, or its pods are unready so the Service has no endpoints — an unready Service
looks like a DNS failure from the client side. Check `kubectl get deploy github-token-minter -n
kubeagents-system` before assuming a networking problem.

**Minty pods never become Ready.** It signs with KMS on every request; a key with no `ENABLED`
version fails the probe. Confirm with `gcloud kms keys versions list`.

**`failed to get access token url for org <name>: ... 404`.** `GITHUB_ORG` is a personal user
account. Minty calls `GET /orgs/{org}/installation`, which does not exist for users. Installing
the App does not help — the repository must live in an Organization, and the App must be owned by
that org. See [the integration README](../config/integrations/github/README.md).

**Tokens mint but GitHub rejects them.** The KMS key version is not the App's key. Compare public
moduli (see the integration README) — a mismatch produces JWTs GitHub silently refuses.

**The agent cannot open a pull request against an empty repository.** A repo with no commits has
no default branch. Seed it with a README first; `provision_10` warns when it finds none.

### Slack

**The agent connects but never responds.** Almost always the bot is not in any channel — invite
it with `/invite @your-agent`. Verify the tokens independently:

```bash
curl -s -H "Authorization: Bearer $SLACK_BOT_TOKEN" https://slack.com/api/auth.test
curl -s -X POST -H "Authorization: Bearer $SLACK_APP_TOKEN" https://slack.com/api/apps.connections.open
```

`users.conversations` returning zero channels confirms it.

**The Agent CR is rejected with `allowedUsers must contain at least one non-blank entry`.**
`SLACK_ALLOWED_USERS` is empty (or holds only whitespace) while the Slack integration is enabled.
This is admission refusing to create the agent at all, not a warning: an empty allowlist is not an
allowlist, and there is no permissive fallback to fall back to. Set `SLACK_ALLOWED_USERS` to a
comma-separated list of Slack member IDs (`U…`) in `vars.sh` and re-run the step.

**`@mentions` work but DMs go nowhere.** Two different switches, and only one of them is the
obvious one. In the Slack App config, **App Home → Show Tabs → Messages Tab** makes the tab visible;
the **"Allow users to send Slash Commands and messages from the messages tab"** checkbox _under_ it
is what actually delivers DMs. Enable the checkbox, not just the tab. Nothing in the token or scope
output distinguishes this case — `auth.test` passes either way.

**Required scopes.** Bot: `app_mentions:read`, `channels:read`, `channels:history`, `groups:read`,
`groups:history`, `im:read`, `im:history`, `mpim:read`, `mpim:history`, `chat:write`, `users:read`.
App-level token: `connections:write`.

### Inference

**`connection refused` to the gateway on port 4000.** The `litellm` Service listens on **port 80**
and forwards to container port 4000. In-cluster clients use `http://litellm` (or
`http://litellm.<namespace>.svc.cluster.local`) with no port. Only a `kubectl port-forward` straight
at the pod sees 4000.

**Gemini 3 tool calls fail with `400 INVALID_ARGUMENT: Function call is missing a
thought_signature`.** Google requires an encrypted `thought_signature` on every multi-turn tool call
from a Gemini 3.x model, and LiteLLM injects the required placeholder only when the configured model
name literally contains the substring `gemini-3`. A name that Google _serves_ as Gemini 3.x but that
does not match that substring — `gemini-pro-latest` was the one that bit us — gets no injection and
fails on the second turn of any tool-using conversation. Keep `MODEL_DEFAULT_NAME` on a name
containing `gemini-3` (the shipped default, `gemini-3.5-flash`, does) and keep LiteLLM at
≥ v1.80.5.

### Scripts

**A step blocks waiting for input.** `init_var` prompts for anything unset, including optional
values such as `GITHUB_ORG`. Pre-populate `vars.sh` (start from `vars.sh.example`) for a
non-interactive run.

**A step reports "Already completed" but you changed something.** Steps are idempotent and skip
when their verify function passes. Change the underlying value in `vars.sh`, or delete the
resource, then re-run.
