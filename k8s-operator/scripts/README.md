# Provisioning & Teardown Scripts Reference

This directory contains the automation scripts for provisioning and tearing down the GCP and GKE infrastructure required by the `kube-agents` platform agent and operator.

> **This page is the canonical description of what each script does.** `INSTALL.md`, the operator
> README, and the documentation site all link here rather than restating the steps. If you change a
> script's behaviour, update it here — and nowhere else.

## Architecture & Configuration Flow

All scripts are modular and idempotent. They share a single configuration state stored in a local `vars.sh` file (which is git-ignored).

When any script is run:

1. It checks if `vars.sh` exists.
2. If any required variables are missing, the script prompts the user for them, exports them, and appends them to `vars.sh`.
3. If they are already defined in `vars.sh`, the script sources them and runs non-interactively.

> [!NOTE]
> Because the provisioning scripts persist configuration state in `vars.sh`, running the script again will reuse the same options selected on the first run. If you want to change configuration variables, manually edit `vars.sh` or perform a teardown first. `vars.sh` is saved with strict file permissions (`umask 077`, `chmod 600`). Set `PERSIST_SECRETS_ON_DISK=false` to prevent storing credentials in `vars.sh`, or `ALLOW_UNENCRYPTED_SECRETS=true` to bypass CMEK pre-flight checks on unencrypted clusters.

### Shared defaults live in `common.sh`

`common.sh` is the single home for the values both entry points must agree on. The per-step scripts
read them through `init_var`, and the repository-root `install.sh` sources the same file rather than
keeping its own copies:

| Symbol                                  | What it fixes                                                               |
| --------------------------------------- | --------------------------------------------------------------------------- |
| `DEFAULT_CLUSTER_NAME`                  | GKE cluster name (`platform-agent-host`)                                    |
| `DEFAULT_REGION`                        | GCP region (`us-central1`)                                                  |
| `DEFAULT_MODEL_PROVIDER`                | Model provider (`gemini`)                                                   |
| `DEFAULT_VERTEX_LOCATION`               | Vertex AI location used by `vertex_ai` (`global`)                           |
| `DEFAULT_LITELLM_KSA_NAME`              | LiteLLM gateway Kubernetes SA (`kubeagents-litellm`)                        |
| `DEFAULT_LITELLM_GSA_NAME`              | LiteLLM gateway Google SA (`kubeagents-litellm-gsa`)                        |
| `DEFAULT_REGISTRY_PREFIX`               | Container registry prefix                                                   |
| `default_model_for_provider <provider>` | The default model for a provider                                            |
| `is_valid_model_provider <provider>`    | Accepted providers: `gemini`, `anthropic`, `vertex_ai`, `chatgpt`, `openai` |
| `is_valid_permission_set <set>`         | Accepted GCP IAM permission sets: `read-only`, `gke-admin`, `custom`        |
| `derive_kms_location <region>`          | Region for Cloud KMS (strips a zone suffix)                                 |

Change a default here and both the pipeline and the installer follow. Do **not** restate these
values in `install.sh`, in a chart, or in prose — link to this table instead.

### Changing the model or the provider after a first run

`MODEL_PROVIDER`, `MODEL_DEFAULT_NAME`, `VERTEX_PROJECT`, and `VERTEX_LOCATION` are the exception to
the note above. `load_state` sources `vars.sh` **after** the environment, so a saved value normally
lands on top of an exported one; for these four the export wins instead, the step warns that it is
overriding what was saved, and the winning value is written back to `vars.sh` so the later steps
agree with it. Exporting `MODEL_PROVIDER` on its own also moves `MODEL_DEFAULT_NAME` to the new
provider's default, because carrying the old provider's model across produces a string like
`vertex_ai/gemini-3.5-flash` that only fails at the gateway; export `MODEL_DEFAULT_NAME` as well to
pin a different model. Everything else still follows the saved-state-wins rule.

The rule is the provisioning pipeline's, and only the provisioning pipeline's. `install.sh` exports
the four values it resolved before it runs `make gcp-provision`, so the answer you gave the
installer — or its `--model-provider` flag — is the one the pipeline uses even when a stale export
is still sitting in the shell that launched it. Teardown goes the other way: `ensure_teardown_state`
sources `vars.sh` and then `ensure_teardown_model_state` only fills in what the file did not set, so
an export can supply a value that was never saved but can never replace one that was. That is the
behaviour you want from a teardown, whose job is to name the objects the last provision actually
created. The override warning names both values, so the provisioning log says which one was used.

### How `install.sh` relates to these scripts

The zero-friction installer at the repository root does **not** provision anything itself. It
collects configuration, writes `scripts/vars.sh`, and then runs `make gcp-provision`, which executes
the pipeline below; its Day-2 control panel (`./install.sh --menu`) re-applies configuration by
calling `provision_08_deploy_platform_agent.sh` directly. These scripts remain the only thing that
talks to GCP. The installer sources `common.sh` before the interview, so its defaults, its
accepted values, and its validation messages are the ones defined here.

### Container images

All kube-agents images default to the `ghcr.io/gke-labs/kube-agents` registry prefix. Export
`REGISTRY_PREFIX` before the first pipeline run to pull mirrored images from a private registry
instead; it is persisted in `vars.sh` like every other option and becomes the default for
`OPERATOR_IMAGE` (step 03), `AGENT_IMAGE` (step 08), and `REPLAY_IMAGE` (step 11), each of
which can still be set individually. Changing it after a first run requires editing the saved
`REGISTRY_PREFIX` and `*_IMAGE` values in `vars.sh` (saved state wins over a new export); the
scripts warn when an export is ignored or a saved image no longer matches the prefix.

`IMAGE_TAG` is the deliberate exception to `vars.sh` reuse: tags change between deploys, so
`provision.sh` asks for it once per pipeline run (or takes an exported `IMAGE_TAG`) and shares
it with every step without saving it. Step 03 also forwards
`PLATFORM_AGENT_IMAGE`, `CREDENTIAL_PROXY_IMAGE`, and `FLUENT_BIT_IMAGE` overrides to the
operator Deployment. See the docs site's
[Docker images page](../../docs/site/src/content/docs/deploy/docker-images.md) for the list of
images to mirror and override precedence.

### Enabling GitHub after a first install

`GITHUB_ORG`, `GITHUB_REPO`, and `GITHUB_APP_ID` gate **three** steps, not just step 10:

| Step | Gated on        | What it skips otherwise                                                   |
| ---- | --------------- | ------------------------------------------------------------------------- |
| 04   | all three       | The Token Minter GSA and its Workload Identity binding                    |
| 07   | `GITHUB_APP_ID` | The `github-app-credentials` Secret the Minter mounts                     |
| 10   | all three       | The KMS keyring, the private-key import, and the Minter Deployment itself |

So filling these in after an install that ran without them and re-running only step 10 leaves the
Minter pods in `CreateContainerConfigError` with `secret "github-app-credentials" not found`. Add
the variables to `vars.sh`, then re-run **04 → 07 → 10** in that order. `GITHUB_ORG` must name a
GitHub organization; steps 04 and 10 look it up and refuse to continue for a personal account or a
name that does not exist, because the Minter can only resolve installations under `/orgs/{org}/`
(`SKIP_GITHUB_ORG_CHECK=true` bypasses that check). See
[`config/integrations/github/README.md`](../config/integrations/github/README.md).

### `MODEL_PROVIDER=vertex_ai` adds IAM to the LiteLLM steps

`vertex_ai` is the only provider whose gateway authenticates to the model backend with Workload
Identity rather than a key, so it is the only one for which these scripts give LiteLLM a GCP
identity. `VERTEX_PROJECT` may differ from `PROJECT_ID`, and the split matters for what gets
cleaned up:

| Step          | What it does for `vertex_ai`                                                                                                                                                                                                                                                                                                                                                      |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 04            | Enables `aiplatform.googleapis.com` on `VERTEX_PROJECT` if it is not already on, creates the LiteLLM GSA in `PROJECT_ID`, grants it `roles/aiplatform.user` on `VERTEX_PROJECT`, and binds `roles/iam.workloadIdentityUser` for the gateway's KSA. Prints why it skipped for any other provider.                                                                                  |
| 09            | Re-checks the GSA and that binding before deploying. A missing GSA or binding fails the step; a grant it could not confirm is a warning and the deploy continues.                                                                                                                                                                                                                 |
| `teardown_04` | Removes the `roles/aiplatform.user` binding from `VERTEX_PROJECT` — the one grant this pipeline writes outside `PROJECT_ID`, so nothing else would — then the Workload Identity binding and the GSA, as it does for the other Agent GSAs. Not gated on `MODEL_PROVIDER`: an install switched back to `gemini` still has all three. Skipped in full under `SKIP_VERTEX_IAM_SETUP`. |

Step 04 chooses no provider of its own on a non-interactive run: with `MODEL_PROVIDER` unset it
skips the LiteLLM step and says so, leaving the question to step 07. Export `MODEL_PROVIDER` (and
`VERTEX_PROJECT`) before running 04 to get the IAM in one pass.

`teardown_09` renders the same overlay to learn the names it has to delete, which is why
`ensure_teardown_state` defaults the model variables rather than leaving them unset: rendered
against the wrong provider, the delete names objects that were never created and leaves the real
ones in the namespace.

`SKIP_VERTEX_IAM_SETUP=true` is honoured by three scripts: step 04 creates nothing, step 09 stops
pre-flighting the GSA and its binding, and `teardown_04` leaves the GSA, its Workload Identity
binding, and the Vertex grant standing. Nothing is created, changed, or removed while it is set,
which is the whole point when those objects belong to someone else. When to reach for it, and what
has to be in place instead, is on the site's
[inference gateway page](../../docs/site/src/content/docs/concepts/inference-gateway.md) — the
canonical home for that; keep this paragraph to which scripts honour the flag.

---

## File Directory

### Orchestration Scripts

- **[provision.sh](provision.sh)**: Master script that coordinates the sequential execution of all core provisioning steps.
- **[teardown.sh](teardown.sh)**: Master script that coordinates the teardown steps in reverse order (conditionally including auxiliary scripts).

### Pipeline steps

Generated from each script's own comment banner.

<!-- BEGIN GENERATED: provisioning-steps -->
<!-- Regenerate with: make docs-generate -- do not edit by hand. -->
<!-- prettier-ignore-start -->

### Provisioning steps

| # | Script | What it does |
| :-: | ------ | ------------ |
| 1 | [`provision_01_gcp_cluster.sh`](provision_01_gcp_cluster.sh) | **GCP APIs & GKE Cluster Initialization** — Idempotent setup script that enables GCP APIs (including Backup for GKE), provisions Cloud KMS database encryption keys, and bootstraps the bare GKE cluster with the BackupRestore addon enabled. The target namespace is created later, by the operator deploy in step 03. |
| 2 | [`provision_02_gvisor_nodepool.sh`](provision_02_gvisor_nodepool.sh) | **Optional Dedicated gVisor Node Pool Initialization** — Idempotent script to bootstrap a dedicated GKE Sandbox (gVisor) node pool on an existing GKE Standard cluster. Can be run independently for migration. |
| 3 | [`provision_03_gcp_gke_operator.sh`](provision_03_gcp_gke_operator.sh) | **Deploy Kubernetes Operator (CRDs & Controller Manager)** — Idempotent script that installs the CRDs and deploys the operator to the cluster. |
| 4 | [`provision_04_gcp_iam.sh`](provision_04_gcp_iam.sh) | **Controller & Agent GCP Workload Identity & GCP IAM Permissions** — Idempotent script for granting GKE cluster management and Workload Identity permissions to the Operator Controller Manager and Agent GSAs. For MODEL_PROVIDER=vertex_ai it also creates the LiteLLM gateway's GSA and grants it roles/aiplatform.user on VERTEX_PROJECT — a second project, when the models are served from one the cluster does not own. |
| 5 | [`provision_05_gcp_gchat.sh`](provision_05_gcp_gchat.sh) | **Google Chat & Pub/Sub Setup** — Configures the Google Chat backend: Pub/Sub routing, the Agent's Service Account, and grants the Service Account permission to read incoming chat messages. Also enables the Workspace Add-ons and Chat APIs and provisions their service identities — without the Chat API identity, Google Chat fails silently. |
| 6 | [`provision_06_slack.sh`](provision_06_slack.sh) | **Slack Integration Setup** — Configures Slack bot tokens, app tokens, and home channel settings. |
| 7 | [`provision_07_gcp_k8s_secrets.sh`](provision_07_gcp_k8s_secrets.sh) | **GKE Kubernetes Secrets Setup** — Idempotent setup script to validate and configure Kubernetes secrets directly. Requires CMEK database encryption unless ALLOW_UNENCRYPTED_SECRETS=true is set. |
| 8 | [`provision_08_deploy_platform_agent.sh`](provision_08_deploy_platform_agent.sh) | **Deploy PlatformAgent Custom Resource Manifest** — Idempotent script that connects to GKE, renders the platform-agent.yaml template, deploys it to the cluster, and fails unless the operator reconciles the change into the agent Deployment (override the wait budget with AGENT_READY_TIMEOUT, default 600s). Whether the Deployment then rolls out is verified by step 13, after the agent's dependencies exist. |
| 9 | [`provision_09_deploy_litellm.sh`](provision_09_deploy_litellm.sh) | **Deploy LiteLLM Gateway** — Idempotent script that connects to GKE and deploys the LiteLLM Gateway. |
| 10 | [`provision_10_deploy_github_minter.sh`](provision_10_deploy_github_minter.sh) | **Deploy GitHub Token Minter** — Idempotent script that deploys the GitHub Token Minter. Runs only when GITHUB_ORG, GITHUB_REPO, and GITHUB_APP_ID are all set; skipped otherwise. |
| 11 | [`provision_11_deploy_inference_replay.sh`](provision_11_deploy_inference_replay.sh) | **Deploy Inference Replay Proxy (optional)** — Idempotent script that deploys the Inference Replay proxy in front of the LiteLLM gateway. Skipped unless INFERENCE_REPLAY_ENABLED=true. The proxy intercepts the `litellm` Service so agents need no configuration changes. With REPLAY_MODE=off (default) it is a pure pass-through; flip the `inference-replay-config` ConfigMap to `on` to start recording/replaying. |
| 12 | [`provision_12_gke_backup_plan.sh`](provision_12_gke_backup_plan.sh) | **GKE Backup Plan (optional)** — Sets up Google Cloud Backup for GKE BackupPlan for automated cluster and persistent volume snapshots. Skipped unless ENABLE_GKE_BACKUP_PLAN=true. Note: If BACKUP_CRON_SCHEDULE or BACKUP_RETAIN_DAYS are modified after initial provisioning, re-running this script automatically reconciles the existing backup plan in-place using 'gcloud beta container backup-restore backup-plans update'. Cost: Incurs charges based on the number of GKE pods backed up and persistent volume snapshot storage used. Defaults to ENABLE_GKE_BACKUP_PLAN=false. Security: Backups include Kubernetes Secrets and persistent volume data, so GCP IAM policies should restrict backup/restore permissions to authorized admins. |
| 13 | [`provision_13_verify_agent_rollout.sh`](provision_13_verify_agent_rollout.sh) | **Verify PlatformAgent Rollout** — Final gate of the pipeline: waits for the agent Deployment to finish rolling out and fails with diagnostics if it does not (override the timeout with AGENT_READY_TIMEOUT, default 600s). Runs last because the agent's model backend — the litellm Service — only exists after step 9, so step 8 cannot verify readiness itself. |

### Teardown steps

| # | Script | What it does |
| :-: | ------ | ------------ |
| 1 | [`teardown_01_gcp_cluster.sh`](teardown_01_gcp_cluster.sh) | **Teardown GKE Cluster & Local State** — Idempotent script to clean up the GKE Standard Cluster and local state files. |
| 2 | [`teardown_02_gvisor_nodepool.sh`](teardown_02_gvisor_nodepool.sh) | **Optional Teardown of Dedicated gVisor Node Pool** — Idempotent script to clean up the dedicated GKE Sandbox (gVisor) node pool and RuntimeClass. Can be run independently to test disabling gVisor. |
| 3 | [`teardown_03_gcp_gke_operator.sh`](teardown_03_gcp_gke_operator.sh) | **Teardown Kubernetes Operator (CRDs & Controller Manager)** — Idempotent script to clean up the deployed operator and CRDs. |
| 4 | [`teardown_04_gcp_iam.sh`](teardown_04_gcp_iam.sh) | **Teardown Controller & Agent GCP Workload Identity & GCP IAM** — Idempotent script to remove cluster management and Workload Identity bindings from the Controller manager and all Agent GSAs, and delete the GSAs. Also removes the LiteLLM gateway's roles/aiplatform.user grant from VERTEX_PROJECT — possibly a second project — unless SKIP_VERTEX_IAM_SETUP leaves that IAM to its external owner. |
| 5 | [`teardown_05_gcp_gchat.sh`](teardown_05_gcp_gchat.sh) | **Teardown Google Chat & Pub/Sub Setup** — Idempotent script to clean up GChat Pub/Sub Topic/Subscription and the Bot GSA. |
| 6 | [`teardown_06_slack.sh`](teardown_06_slack.sh) | **Teardown Slack Integration Setup** — Idempotent script to clean up Slack integration state and tokens. |
| 7 | [`teardown_07_gcp_k8s_secrets.sh`](teardown_07_gcp_k8s_secrets.sh) | **Teardown GKE Secrets** — Idempotent script to clean up Kubernetes secrets and sanitize local state. |
| 8 | [`teardown_08_deploy_platform_agent.sh`](teardown_08_deploy_platform_agent.sh) | **Teardown PlatformAgent Custom Resource** — Idempotent script to clean up the applied PlatformAgent Custom Resource (CR) and delete the local generated manifest file. |
| 9 | [`teardown_09_deploy_litellm.sh`](teardown_09_deploy_litellm.sh) | **Teardown LiteLLM Gateway** — Idempotent script to undeploy the LiteLLM gateway. |
| 10 | [`teardown_10_deploy_github_minter.sh`](teardown_10_deploy_github_minter.sh) | **Teardown GitHub Token Minter** — Idempotent script to clean up the GitHub Token Minter. |
| 11 | [`teardown_11_deploy_inference_replay.sh`](teardown_11_deploy_inference_replay.sh) | **Teardown Inference Replay Proxy** — Idempotent script to undeploy the Inference Replay proxy and restore the original LiteLLM Service. Safe to run even when the proxy was never deployed. |
| 12 | [`teardown_12_gke_backup_plan.sh`](teardown_12_gke_backup_plan.sh) | **Teardown GKE Backup Plan** — Idempotent script to delete the Google Cloud Backup for GKE BackupPlan. Safely deletes any remaining backup snapshots in background batches before removing the BackupPlan. Safe to run even if the backup plan was never created. Set PRESERVE_BACKUPS=true to preserve existing BackupPlan and snapshots during teardown (defaults to false). |

<!-- prettier-ignore-end -->
<!-- END GENERATED: provisioning-steps -->

### Auxiliary & Development Scripts

- **[common.sh](common.sh)**: Shared utility functions, color output, logging, prompt helpers, and state management.
- **[platform-agent.yaml.template](platform-agent.yaml.template)**: Manifest template used by `provision_08_deploy_platform_agent.sh` to render the `PlatformAgent` Custom Resource.
- **[print_instructions_gchat.sh](print_instructions_gchat.sh)**: Helper script that prints Google Chat integration post-provisioning instructions.
- **[print_instructions_slack.sh](print_instructions_slack.sh)**: Helper script that prints Slack integration post-provisioning instructions.
- **[dev/dev_rebuild_agent.sh](dev/dev_rebuild_agent.sh)**: Fast local development utility that builds, pushes, and redeploys agent container images.

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
