# Kubernetes Agentic Harness Installation & Setup Guide

This comprehensive, step-by-step guide explains how to install, configure, deploy, and verify the **Kubernetes Agentic Harness (`kube-agents`)** across different environments—from automated Google Cloud Platform (GCP) / GKE deployments to local development clusters and third-party multi-agent orchestrators.

---

## Table of Contents

1. [Architecture & Overview](#architecture--overview)
2. [Prerequisites & Tooling Matrix](#prerequisites--tooling-matrix)
3. [Method 1: Automated GCP & GKE Provisioning (Recommended)](#method-1-automated-gcp--gke-provisioning-recommended)
   - [Modular Pipeline Stages](#modular-pipeline-stages)
   - [Step-by-Step Execution](#step-by-step-execution)
4. [Method 2: Manual Kubernetes Cluster Deployment](#method-2-manual-kubernetes-cluster-deployment)
   - [Step 1: Install cert-manager](#step-1-install-cert-manager)
   - [Step 2: Create API Key & Access Secrets](#step-2-create-api-key--access-secrets)
   - [Step 3: Build & Push the Operator Image](#step-3-build--push-the-operator-image)
   - [Step 4: Deploy the Operator & CRDs](#step-4-deploy-the-operator--crds)
   - [Step 5: Deploy Integrations (LiteLLM & GitHub)](#step-5-deploy-integrations-litellm--github)
   - [Step 6: Apply Custom Resources](#step-6-apply-custom-resources)
5. [Method 3: Local Development & Fast Iteration](#method-3-local-development--fast-iteration)
   - [Phase 2 — Kind inner loop (Cluster Admin Agent + cascade)](#phase-2--kind-inner-loop-cluster-admin-agent--cascade)
   - [Phase 3 — Kind inner loop (Developer Team Agent + namespace isolation)](#phase-3--kind-inner-loop-developer-team-agent--namespace-isolation)
   - [Phase 4 — Coordination & knowledge (push-first proactivity + OKF)](#phase-4--coordination--knowledge-push-first-proactivity--okf)
   - [Phase 5 — Security gate & hardening (review-gate CI, egress, pod hardening, attribution)](#phase-5--security-gate--hardening-review-gate-ci-egress-pod-hardening-attribution)
   - [Phase 6 — Failure-isolation & resilience (chaos: no cascade)](#phase-6--failure-isolation--resilience-chaos-no-cascade)
   - [Phase 7 — Cloud-agnostic seams (Terraform, second CI/CD, provider-neutral observability)](#phase-7--cloud-agnostic-seams-terraform-second-cicd-provider-neutral-observability)
6. [Teardown & Cleanup](#teardown--cleanup)
7. [Troubleshooting & Common FAQ](#troubleshooting--common-faq)

---

## Architecture & Overview

The Kubernetes Agentic Harness manages Kubernetes operations via an autonomous **Platform Agent (`platform`)** acting as the master custodian and architect.

- **Agent Configuration (`agents/platform`)**: Contains the system prompt and persona identity (`SOUL.md`), workspace instructions (`AGENTS.md`), runtime configuration (`config.yaml`), scheduled governance jobs (`cron/jobs.json`), operational playbooks (`governance/`), and reusable skills (`skills/`).
- **Kubernetes Operator (`k8s-operator`)**: A Kubebuilder-powered Go operator that manages Custom Resource Definitions (`PlatformAgent`) and reconciles cluster lifecycle state.
- **Integrations**: Supports LiteLLM Gateway for LLM provider routing (Gemini, OpenAI, Anthropic) and enterprise messaging bridges (Google Chat, Slack).

---

## Prerequisites & Tooling Matrix

Before beginning installation, ensure your environment meets the following requirements:

| CLI Tool / Utility              | Required Version | Verification Command       | Description                                                    |
| :------------------------------ | :--------------- | :------------------------- | :------------------------------------------------------------- |
| **Go**                          | `1.24+`          | `go version`               | Required for building operator binaries and running tests.     |
| **Docker / Podman**             | `20.10+`         | `docker --version`         | Required to build container images for the operator.           |
| **kubectl**                     | `1.28+`          | `kubectl version --client` | Communicates with your target Kubernetes or GKE cluster.       |
| **Google Cloud SDK (`gcloud`)** | Latest           | `gcloud version`           | Needed for GKE cluster access, IAM, and Artifact Registry.     |
| **Helm**                        | `3.10+`          | `helm version`             | Used for installing cluster dependencies like `cert-manager`.  |
| **gettext (`envsubst`)**        | Standard         | `envsubst --version`       | Used by Makefile deployment targets for template substitution. |

---

## Method 1: Automated GCP & GKE Provisioning (Recommended)

For full end-to-end setups on Google Cloud Platform (GCP) with GKE Standard, Workload Identity, Pub/Sub, LiteLLM, GitHub Token Minter, and Inference Replay Proxy, use the automated provisioning pipeline in `k8s-operator/`.

### Modular Pipeline Stages

The automated installer executes idempotent stages sequentially:

1. **01: GKE Cluster Setup** (`make gcp-provision-01-cluster`)
2. **02: gVisor Sandbox Pool** (`make gcp-provision-02-gvisor`)
3. **03: Operator CRDs & Manager** (`make gcp-provision-03-operator`)
4. **04: GCP IAM & Workload Identity** (`make gcp-provision-04-iam`)
5. **05: Google Chat Pub/Sub Topic** (`make gcp-provision-05-gchat`)
6. **06: Slack Configuration** (`make gcp-provision-06-slack`)
7. **07: Kubernetes API Secrets** (`make gcp-provision-07-secrets`)
8. **08: PlatformAgent CR Deployment** (`make gcp-provision-08-deploy`)
9. **09: LiteLLM Gateway** (`make gcp-provision-09-litellm`)
10. **10: GitHub Token Minter** (`make gcp-provision-10-github`)
11. **11: Inference Replay Proxy** (`make gcp-provision-11-inference-replay`)

### Step-by-Step Execution

#### Step 1: Authenticate with Google Cloud

Authenticate your `gcloud` CLI and set Application Default Credentials:

```bash
gcloud auth login
gcloud auth application-default login
```

#### Step 2: Execute Provisioning

Navigate to the `k8s-operator` directory and launch the provisioning pipeline:

```bash
cd k8s-operator
make gcp-provision
```

- On the first run, the script prompts for configuration inputs (GCP Project ID, region, cluster name, model provider, API key, etc.) and saves them locally in `scripts/vars.sh`.
- Subsequent invocations reuse `scripts/vars.sh` for non-interactive idempotency.

> [!NOTE]
> Because the provisioning scripts persist configuration state in `scripts/vars.sh`, running the script again will reuse the same options selected on the first run. If you want to change configuration variables, manually edit `scripts/vars.sh` or perform a teardown first.

- **Dry-run check**: To preview actions without modifying cloud infrastructure:
  ```bash
  make gcp-provision ARGS="--dry-run"
  ```

> [!TIP]
> Each stage of the provisioning pipeline can also be run individually using step-specific Makefile targets (e.g., `make gcp-provision-01-cluster`, `make gcp-provision-02-gvisor`, ..., `make gcp-provision-11-inference-replay`). See [k8s-operator/README.md](k8s-operator/README.md#running-individual-steps-with-make) for the complete list of individual provisioning and teardown targets.

#### Step 3: Verify Running Components

Verify that the operator, LiteLLM gateway, and custom resources are healthy:

```bash
kubectl get deployments -n kubeagents-system
kubectl get pods -n kubeagents-system
kubectl get platformagents --all-namespaces
```

#### Step 4: ChatGPT OAuth Authentication (If Applicable)

If you chose `chatgpt` as your `MODEL_PROVIDER`, follow the printed OAuth Device Flow instructions or check the LiteLLM gateway logs:

```bash
kubectl logs -n kubeagents-system deployment/litellm -f
```

#### Step 5: Enable Google Chat & Slack Integrations (Manual Required Steps)

If you enabled Google Chat (`GOOGLE_CHAT_ENABLED=true`) or Slack (`SLACK_ENABLED=true`) during provisioning, perform the following required manual steps after `make gcp-provision` completes:

##### 1. Google Chat Configuration (`GOOGLE_CHAT_ENABLED=true`)

1. **Configure the Google Chat API endpoint in GCP Console**:
   - Open the Google Chat API configuration page: `https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat?project=<PROJECT_ID>`
   - Set the **App name** to `GKE Platform Agent Bot`.
   - Set the **Avatar URL** to `https://platform-agent.nousresearch.com/docs/img/logo.png`.
   - Under **Connection settings**, select **Cloud Pub/Sub** and enter the Cloud Pub/Sub topic created during provisioning:
     ```text
     projects/<PROJECT_ID>/topics/<CHAT_TOPIC_NAME>
     ```
   - Under **Visibility**, select **Specific people and groups in your domain** and enter your email address (`ALLOWED_USERS`).
2. **Send a Test Direct Message**:
   - Send a DM to the bot in Google Chat with the message `"Hi Platform Agent"`.
3. **Approve Pairing Code (Optional / First-time setup)**:
   - If pairing mode is enabled, approve the pairing code displayed in the gateway logs:
     ```bash
     kubectl exec -it deploy/platform-agent-gateway -n kubeagents-system -- hermes pairing approve google_chat <PAIRING_CODE>
     ```
   - Re-display these instructions at any time from the `k8s-operator` directory:
     ```bash
     ./scripts/print_instructions_gchat.sh
     ```

##### 2. Slack Configuration (`SLACK_ENABLED=true`)

1. **Verify Slack App Settings**:
   - Ensure **Socket Mode** is enabled in your Slack App console.
   - Verify that your Bot Token (`SLACK_BOT_TOKEN`) has the required scopes: `app_mentions:read`, `channels:history`, `chat:write`, `channels:read`, `groups:read`, `im:read`, `mpim:read`.
2. **Test Bot Connection**:
   - Invite the bot to a channel or send a direct message: `"Hi Platform Agent"`.
3. **Approve Pairing Code (Optional / First-time setup)**:
   - If pairing mode is enabled, approve the pairing code displayed in the gateway logs:
     ```bash
     kubectl exec -it deploy/platform-agent-gateway -n kubeagents-system -- hermes pairing approve slack <PAIRING_CODE>
     ```
   - Re-display these instructions at any time from the `k8s-operator` directory:
     ```bash
     ./scripts/print_instructions_slack.sh
     ```

---

## Method 2: Manual Kubernetes Cluster Deployment

If you are installing into an existing Kubernetes or GKE cluster without using the automated GCP provisioning pipeline, follow these steps.

### Step 1: Install cert-manager

The Kubernetes Operator requires `cert-manager` (version `1.13.0+`) to generate and rotate admission webhook TLS certificates.

- **Standard Kubernetes / GKE Standard Cluster (via Helm)**:

  ```bash
  helm repo add jetstack https://charts.jetstack.io
  helm repo update
  helm install cert-manager jetstack/cert-manager \
    --namespace cert-manager \
    --create-namespace \
    --set installCRDs=true
  ```

- **GKE Autopilot Cluster (Leader Election Workaround)**:
  GKE Autopilot restricts coordination Leases in `kube-system`. Disable leader election during install:
  ```bash
  helm install cert-manager jetstack/cert-manager \
    --namespace cert-manager \
    --create-namespace \
    --set installCRDs=true \
    --set controller.leaderElection.enabled=false \
    --set cainjector.leaderElection.enabled=false
  ```

### Step 2: Create API Key & Access Secrets

Create the `kubeagents-system` namespace and add your model provider credentials:

```bash
kubectl create namespace kubeagents-system --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic platform-agent-secrets \
  --namespace kubeagents-system \
  --from-literal=GEMINI_API_KEY="your-gemini-api-key" \
  --from-literal=API_SERVER_KEY="your-api-server-key" \
  --from-literal=ANTHROPIC_API_KEY="your-anthropic-api-key" \
  --from-literal=OPENAI_API_KEY="your-openai-api-key"
```

### Step 3: Build & Push the Operator Image

Set your registry destination and build the container image:

```bash
cd k8s-operator

export IMG=us-central1-docker.pkg.dev/<YOUR_PROJECT>/<YOUR_REPO>/kube-agents-operator:latest

make docker-build IMG=$IMG
make docker-push IMG=$IMG
```

### Step 4: Deploy the Operator & CRDs

Install the Custom Resource Definitions (CRDs) and deploy the controller manager deployment:

```bash
export KUBE_CONTEXT=$(kubectl config current-context)   # name the cluster; see the note below
make install
make deploy IMG=$IMG
```

> **`KUBE_CONTEXT` is how these targets choose a cluster.** Every target under `##@ Deployment`
> passes `--context` explicitly. Leave `KUBE_CONTEXT` unset and the target reads your ambient
> context, but **refuses** to proceed unless it looks like a throwaway (`kind-*`, `gke-scratch-*`)
> — it prints the command that would name it deliberately and exits 2. Passing a whole command
> line (`KUBECTL="kubectl --context …"`) is rejected outright: `make` accepts any assignment
> whether or not the Makefile reads one, and that override used to be silently discarded, which
> is worse than having none.

Verify controller readiness:

```bash
kubectl rollout status deployment -n kubeagents-system
```

### Step 5: Deploy Integrations (LiteLLM & GitHub)

To optionally deploy the LiteLLM Gateway or GitHub Token Minter:

```bash
# Deploy LiteLLM Gateway
export MODEL_PROVIDER=gemini
export MODEL_DEFAULT_NAME=gemini-3.5-flash
make deploy-litellm

# Deploy GitHub Integration (requires pre-configured github-app-credentials secret and env vars)
export PROJECT_ID="your-gcp-project-id"
export REGION="your-gcp-region"
export CLUSTER_NAME="your-gke-cluster-name"
export KMS_KEYRING="your-kms-keyring"
export KMS_KEY="your-kms-key"
export KMS_KEY_VERSION="your-kms-key-version"
export GITHUB_ORG="your-github-org"
export GITHUB_REPO="your-github-repo"
export GITHUB_MINTER_KSA_NAME="kubeagents-github-minter"
export GITHUB_MINTER_GSA_NAME="kubeagents-github-minter-gsa"
export PLATFORM_AGENT_GSA_NAME="kubeagents-platform-agent-gsa"
make deploy-github
```

### Step 6: Apply Custom Resources

Submit a sample `PlatformAgent` Custom Resource to activate cluster governance (run inside `k8s-operator/`):

```bash
kubectl apply -f examples/platformagent.yaml
kubectl get platformagents -A
```

---

## Method 3: Local Development & Fast Iteration

For developer testing on a workstation against a local cluster (e.g., Kind) or remote GKE cluster without building container images:

1. **Set your active Kubernetes context**:
   ```bash
   kubectl config current-context
   ```
2. **Install CRDs**: (`KUBE_CONTEXT` is required unless the context above is `kind-*` /
   `gke-scratch-*` — see the note in Step 4)
   ```bash
   cd k8s-operator
   make install KUBE_CONTEXT=$(kubectl config current-context)
   ```
3. **Run the controller locally with webhooks disabled**:
   ```bash
   ENABLE_WEBHOOKS=false make run
   ```
4. **Fast Remote Rebuild & Update**:
   To rebuild and push an updated container image and trigger immediate deployment rollout in GKE:
   ```bash
   make dev-rebuild-agent ARGS="platform"
   ```

### Kind inner loop — build & load your **local** images first (read this before any phase)

The Kind phases below all assume the cluster is running **your working-tree code**, not the
upstream published image. Two things make this non-obvious, and both have bitten this repo:

- **`make deploy` does _not_ build.** It only runs `kustomize set image` + `kubectl apply`. So
  `make deploy IMG=ghcr.io/gke-labs/kube-agents/k8s-operator:v0.1.0` deploys the **upstream**
  binary and compiles nothing of yours. To test your changes you must build a **local** image,
  `kind load` it, and point the workload at it.
- **There are two image families**, built by two different Makefiles:
  - **operator image** (the controller + webhook) — `k8s-operator/Makefile`, target `docker-build`
  - **agent images** (`platform` / `cluster-admin` / `developer-team`) — the **root** `Makefile`,
    target `docker-build-agents`

Use the helper — it does build → `kind load` → repoint the running workload, and is guarded to
Kind/scratch-GKE contexts so it can never touch a real cluster:

```bash
# operator (controller + webhook): build kube-agents/k8s-operator:dev, load it, restart the controller
local-dev/kind/reload-images.sh operator kind-kube-agents-dev

# agent images: build kube-agents/<tier>-agent:latest and load them
local-dev/kind/reload-images.sh agents   kind-kube-agents-dev

# both
local-dev/kind/reload-images.sh all      kind-kube-agents-dev
```

Two rules the helper encodes so you don't get silently-stale results:

- **`imagePullPolicy` trap.** The controller renders agent pods with `imagePullPolicy: PullAlways`
  by **default**, which makes the kubelet ignore your `kind load`ed image and re-pull from the
  registry (i.e. run the **upstream** image). For local Kind testing, the Agent CR must set
  `spec.deployment.imagePullPolicy: IfNotPresent` (the example CRs already do). The operator
  Deployment itself already uses `IfNotPresent`.
- **Stale-image rule.** Local images reuse a fixed tag (`:dev`, `:latest`). Same tag +
  `IfNotPresent` means the kubelet will **not** refresh a copy it already has — so after **any**
  source change you must rebuild **and** reload **and** restart. The helper always does all three.

> If you only want the upstream published image (a quick smoke test, not testing your code), you
> can `cd k8s-operator && make deploy IMG=ghcr.io/gke-labs/kube-agents/k8s-operator:v0.1.0` — but
> understand that this tests **upstream**, not your working tree.

### Phase 2 — Kind inner loop (Cluster Admin Agent + cascade)

Phase 2 adds the tier-discriminated `Agent` CRD (renamed from `PlatformAgent`), the read-only
**Cluster Admin Agent** persona, the standalone **kage-router** ChatOps front door, the **F4
provisioning cascade** (the Platform Agent proposes a subordinate cluster-admin bundle as a GitOps PR),
and the **spoke bootstrap** ordered apply waves. Verify the whole inner loop on a local Kind cluster:

1. **Create a Kind cluster** (K8s ≥ 1.30 — the VAP requires `ValidatingAdmissionPolicy` GA):
   ```bash
   kind create cluster --name kube-agents-dev --image kindest/node:v1.31.2
   ```
2. **Deploy the stack** (cert-manager → controller/CRD/webhooks/router → VAP). Build & load your
   **local** operator image first (see "Kind inner loop" above), then deploy that tag — do **not**
   deploy the upstream `ghcr.io/...:v0.1.0` tag if you want to test your working tree:
   ```bash
   kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.14.7/cert-manager.yaml
   kubectl -n cert-manager wait --for=condition=Available deploy --all --timeout=180s
   make -C k8s-operator docker-build IMG=kube-agents/k8s-operator:dev
   kind load docker-image kube-agents/k8s-operator:dev --name kube-agents-dev
   cd k8s-operator && make deploy IMG=kube-agents/k8s-operator:dev && cd ..
   kubectl apply -f examples/gitops-repo/policy/vap-agent-readonly.yaml
   ```
3. **Run the consolidated verification gate** (destructive; guarded to Kind contexts only):
   ```bash
   local-dev/kind/verify-phase2.sh kind-kube-agents-dev
   ```
   It exercises the load-bearing suites: live webhook serving (duplicate `(tier,scope)` + tier
   immutability rejected), VAP attenuation (write/impersonate/wrong-scope denied), read-only per-tier
   SAR, the cascade render → VAP dry-run, bootstrap ordering (pod binds the pre-created SA), and the
   no-break-glass check. The deterministic router/index suites run under `cd k8s-operator && go test ./...`.
4. **Egress enforcement (V-K11)** needs a NetworkPolicy-enforcing CNI — kindnet does **not** enforce it.
   Create a throwaway cluster with `disableDefaultCNI: true` and install Calico to verify the per-tier
   default-deny egress (including the metadata-server `169.254.169.254` block). See
   [docs/build/LEDGER.md](docs/build/LEDGER.md) §Verification log for the exact steps.

### Phase 3 — Kind inner loop (Developer Team Agent + namespace isolation)

Phase 3 adds the read-only **Developer Team Agent** (one per namespace), the load-bearing **A1
placement clause** (a developer-team `Agent` must be created in the namespace it scopes —
`metadata.namespace == spec.scope.namespace`), the per-namespace **isolation baseline**
(default-deny NetworkPolicy + a per-tier egress allowlist, `ResourceQuota`, and in-namespace
`ExternalName` aliases for the shared hub services), the **`propose-developer-team`** cascade on the
Cluster Admin Agent, and the router completion (NL confidence/clarify, candidate validity, thread
affinity, audit attribution). It reuses the Phase 2 stack on the same Kind cluster.

> **Image refresh (important).** The webhook/controller run inside the operator image. After **any**
> change to `k8s-operator/internal/webhook` or `.../controller`, refresh the running image before
> verifying — a same-tag image with `imagePullPolicy: IfNotPresent` will otherwise keep serving the
> stale build and can **silently under-enforce** an admission invariant (this is exactly how a
> Phase 3 placement escape first slipped through). The helper does build → `kind load` → restart in
> one guarded step:
>
> ```bash
> local-dev/kind/reload-images.sh operator kind-kube-agents-dev
> ```
>
> Equivalent longhand, if you prefer to see each step:
>
> ```bash
> make -C k8s-operator docker-build IMG=kube-agents/k8s-operator:dev
> kind load docker-image kube-agents/k8s-operator:dev --name kube-agents-dev
> kubectl -n kubeagents-system set image deploy/kubeagents-controller-manager manager=kube-agents/k8s-operator:dev
> kubectl -n kubeagents-system rollout restart deploy/kubeagents-controller-manager
> kubectl -n kubeagents-system rollout status  deploy/kubeagents-controller-manager --timeout=120s
> ```

1. **Run the consolidated Phase 3 gate** (destructive; guarded to Kind contexts only). It applies the
   `team-x` tenant bundle (`namespaces/team-x/` `00`→`60`, in numeric order) and the dev-team `Agent`
   CR, then asserts the whole isolation proof:
   ```bash
   local-dev/kind/verify-phase3.sh kind-kube-agents-dev
   ```
   It exercises: the **placement clause** (matching namespace admitted, foreign `metadata.namespace`
   rejected), the **reconciled dev-team pod** (bound to the pre-created `developer-team-agent` SA, the
   `developer-team-agent:<tag>` image, and the `kube-agents/tier=developer-team` label), **read-only,
   namespace-scoped SAR** (reads in `team-x` only — no cross-namespace, cluster-scoped, write, or
   privilege-escalation access), **duplicate `(tier,scope)` + tier immutability** rejection, **VAP
   attenuation** (delegated to `negative-attenuation.sh`), the **isolation netpol shape** (default-deny
   - egress tier selector, a pure allowlist with **no `0.0.0.0/0`**, server-dry-run valid) plus the
     `ExternalName` aliases, and the **cascade** `render_developer_team.py` → VAP dry-run (identity
     admitted, write-verb tamper denied). The deterministic router suites run under
     `cd k8s-operator && go test ./...`.
2. **Egress enforcement** carries the same kindnet caveat as Phase 2 — `verify-phase3.sh` validates
   the egress policy **structurally** (shape, tier selector, zero `0.0.0.0/0`) and defers real
   enforcement (agent pod cannot reach `169.254.169.254` or the open internet) to a Calico cluster.

### Phase 4 — Coordination & knowledge (push-first proactivity + OKF)

Phase 4 turns on **indirect coordination** and **push-first proactivity** without loosening the
read-only ceiling. Three deltas land: (1) **push-first event triggers** (04 §4) — a per-tier
Kubernetes watch on the agent's **own** read-only SA (namespace-scoped for developer-team, cluster-wide
for cluster-admin/platform, with a **fail-closed startup guard**), plus alert + GitHub webhooks funneled
over a deferrable subscribe-only `eventingress` component — all delivering into the **local**
session-inject seam (now bound to `127.0.0.1` and bearer/owner-authenticated); the **heartbeat is
demoted to a backstop**; (2) **OKF read + escalation** (06 §5) — a `read-knowledge` skill does a sparse,
read-only `knowledge/`-only checkout, and a lower tier raises a cross-tier request as a
`knowledge/escalation/<slug>.md` PR that the **parent picks up on its next sweep** — never a direct
agent-to-agent call (invariant 3); (3) **proactive SOPs** — the Platform Agent's **drift-detection SOP**
opens a corrective PR unprompted (SC4, 01 §7), and per-tier **heartbeat SOPs** run scoped audits. A
trigger changes only _when_ an agent wakes, never _what_ it may do — every resulting change still flows
through a reviewed `submit-suggestion` PR.

> **Same image-refresh caveat as Phase 3.** The watcher sidecar, the controller's per-tier watcher-arg
> rendering, and the seam hardening all ship inside the operator/agent images at the shared `v0.1.0` tag.
> The **live** Event→session spawn and the cloud transport legs therefore require a rebuild +
> `kind load` + `rollout restart` before they can be trusted on Kind (a stale same-tag image reads green
> while running Phase-3 code). The gate below proves the Phase-4 **logic** hermetically so it is
> trustworthy without a rebuild; the live spawn/transport is explicitly deferred.

1. **Run the consolidated Phase 4 gate** (the live regression is destructive and guarded to Kind
   contexts; the hermetic acceptance runs anywhere, so this is CI-safe with no cluster):
   ```bash
   local-dev/kind/verify-phase4.sh kind-kube-agents-dev
   ```
   It proves 07 §2 Phase 4 Accept **(a)–(e)** hermetically — **(a)** per-tier scoped watcher +
   fail-closed `validate()` + controller `--owner`/`--scope-namespace` rendering + the hardened
   inject seam (bearer/owner auth, `kind` discriminator); **(b)** the escalation round-trip is
   **indirect** (written via `submit-suggestion --dry-run`, read back via `read-knowledge`, with the
   child egress NetworkPolicy carrying **no parent-tier destination** — cross-tier flow is GitOps +
   loopback only); **(c)** a runbook is retrieved through the sparse read-only OKF path (which can
   never push) with `okf-validate` green; **(d)** per-tier heartbeats run **scoped** audits
   (cluster-admin over its cluster, developer-team over its namespace only) and route any change to a
   PR; **(e)** injected drift yields a **corrective-PR artifact** while the drifted live object stays
   present (detect-and-propose, never fix) — then re-runs the load-bearing **regression** live on Kind
   (03 §11 `negative-attenuation.sh`, the dev-team read-only SAR under a trigger, and the 08 §7
   controller-mints-no-RBAC golden).
2. **Deferred, not faked:** the live Event→session spawn and the cloud transport (alert Pub/Sub
   delivery, GitHub webhook HMAC) need the rebuilt Phase-4 image / scratch-GKE — the gate proves the
   in-pod terminus and all rendering/scoping logic instead. **05 §8 chaos** (failure-isolation) is
   Phase 6 and is marked N-A here rather than silently skipped.

### Phase 5 — Security gate & hardening (review-gate CI, egress, pod hardening, attribution)

Phase 5 makes the security model **continuously enforced** rather than set-once, without relaxing any
invariant (agents stay read-only, the only write path is a reviewed PR). Four deltas land: (1) the
**review-gate CI** (06 §7) — the agent-driven `review-security-k8s-*` skills run on every PR (and a
heartbeat re-run) via a **headless detector**, emit findings tagged with a **severity**, and a
**hermetic Python scorer** turns "any unmitigated high/critical" into a **merge block**; a finding is
mitigated only by a matching, non-expired entry in `security-review-waivers.yaml` (fingerprint =
`sha256(agent\nfile\nnormalize(message))[:16]`). (2) A **per-tier egress allowlist** for all three tiers
(platform is net-new) plus a **real enforcement proof** on Calico. (3) The **hardened pod-security
context on every agent pod** made continuously enforced — PSS `enforce: restricted` on the namespace
plus a focused `vap-agent-pod-hardening` VAP that requires `readOnlyRootFilesystem: true` on every
`kube-agents/tier` pod (restricted-PSS does not cover it), composing with — never colliding with — the
RBAC-governing `vap-agent-readonly`. (4) **End-to-end attribution** — the authenticated requester +
per-turn trace id flow router → inject seam → session → PR, stamped as durable `Requested-by:` /
`Trace-Id:` trailers on the mutation PR (which squash-merge lands in `main`'s history).

> **Enforcement needs the right substrate.** Two Accept criteria can only be _proven_ on capable
> infrastructure: egress **enforcement** (b) needs a NetworkPolicy-enforcing CNI — the default `kindnet`
> dev cluster does **not** enforce, so the shape is checked structurally on Kind and actual deny/allow is
> proven on a **Calico** cluster (`local-dev/kind/kind-calico.yaml` + `local-dev/tests/egress-enforcement.sh`),
> deferred-not-faked where Calico is unreachable; the pod-hardening **VAP** (c) needs K8s ≥ 1.30 (VAP GA —
> the dev cluster is v1.31.x). A freshly-applied VAP binding also has a short activation delay, so the
> gate polls the admission dry-run until the binding is live before judging.

1. **Run the consolidated Phase 5 gate** (the live checks are destructive and guarded to Kind contexts;
   the hermetic acceptance runs anywhere, so this is CI-safe with no cluster):
   ```bash
   local-dev/kind/verify-phase5.sh kind-kube-agents-dev
   ```
   It proves 07 §2 Phase 5 Accept **(a)–(d)** — **(a)** `score_findings.py` BLOCKS an unmitigated `high`
   (exit 1), PASSES a clean set (exit 0), lets a matching non-expired waiver mitigate, and still BLOCKS on
   an **expired** waiver (negative control), backed by the scorer + extractor unit suites; **(b)** all
   three tier egress netpols are pure allowlists (`policyTypes:[Egress]`, tier `podSelector`, **no
   `0.0.0.0/0`**), and live egress enforcement is exercised (DEFERRED, non-fatal, on kindnet — PROVEN
   separately on Calico); **(c)** the go goldens carry `readOnlyRootFilesystem: true` on every rendered
   container, the namespace carries the PSS `restricted` label, both VAPs are present, and — live on Kind
   — the pod-hardening VAP **rejects** an un-hardened `kube-agents/tier` pod (the error names
   `readOnlyRootFilesystem`), **admits** a hardened one, and leaves a non-agent pod **untouched** (scope
   proof); **(d)** `submit-suggestion` stamps the `Requested-by:` / `Trace-Id:` trailers (flag > env >
   autonomous fallback, single-line, idempotent, reaching the dry-run artifact) and the router audit ties
   `Sender` to the `TraceID` carried through to dispatch. It then re-runs the load-bearing **regression**
   live on Kind (03 §11 `negative-attenuation.sh`) plus the full prior-phase gates
   (`verify-phase{2,3,4}.sh`) and `go test ./...`.
2. **Deferred, not faked:** egress **enforcement** on kindnet defers to the Calico run; the **live
   headless detector** in `review-gate.yml` needs the `ANTHROPIC_API_KEY` secret + live creds and skips
   gracefully on fork PRs (like `auto_request_review`) — the scorer, which is the authoritative gate,
   always runs and is proven hermetically; the **hostname-precise L7 egress proxy**, **cross-object
   webhook**, **gVisor execution sandbox**, and **per-request user down-scoping** remain deferred
   hardening (08 §5). **05 §8 chaos** (failure-isolation) is Phase 6 and is marked N-A here rather than
   silently skipped.

### Phase 6 — Failure-isolation & resilience (chaos: no cascade)

Phase 6 is a **validation phase** — it adds no new persona and no new write path. It graduates the
05 §8 **failure-isolation (chaos)** suite from deferred to a live, load-bearing gate, proving the
design's central resilience claim: **no cascade failure** (04 §6). Four experiments run against the
existing Kind cluster (`local-dev/kind/chaos-suite.sh`):

- **C1 — controller down.** Scale `kubeagents-controller-manager` → 0. A running pod stays Ready
  (running pods continue), the deleted agent Deployment is **not** recreated (no reconciles without the
  controller), and on scale-up the controller re-acquires leadership and recreates it (reconcile /
  provisioning **resumes**). — Accept **(b)**.
- **C2 — controller up.** Delete the real agent Deployment → the controller recreates it (owned by its
  `Agent` CR); delete a running pod → its Deployment recreates it. The controller **relaunches** agent
  workloads. — Accept **(c)**.
- **C3 — Cluster Admin Agent down.** Kill the cluster-admin pod; its Developer Team pod stays
  UID-stable + Ready across the whole window (**no cascade**) and the cluster-admin is relaunched. —
  Accept **(b)**.
- **C4 — hub down.** Scale a hub-inference stand-in → 0; the spoke workload keeps running its
  last-applied state, is structurally decoupled from the hub (owned by its own ReplicaSet, no hub
  ownerRef), and **no Config Sync / Config Connector / Argo / Flux CRD is required** (unopinionated
  actuation, 05 §8 bullet 4). — Accept **(a)**.

Run the consolidated Phase 6 gate — the NET-NEW chaos suite plus the full prior-phase regression
(the live ops are destructive and **guarded to Kind contexts**; every op is reversible, single-object,
and self-cleaning):

```bash
local-dev/kind/verify-phase6.sh kind-kube-agents-dev
```

> **The dev cluster must run the locally-built controller.** The published `k8s-operator:v0.1.0` image
> predates the Phase 5 pod hardening — it renders agent pods **without** `readOnlyRootFilesystem`, which
> the `vap-agent-pod-hardening` VAP (correctly) rejects at admission, so a recreated pod never appears.
> Before Phase 6, build and load the current controller so its live rendering matches source:
>
> ```bash
> cd k8s-operator && make docker-build IMG=kube-agents/k8s-operator:dev
> kind load docker-image kube-agents/k8s-operator:dev --name kube-agents-dev
> kubectl -n kubeagents-system set image deploy/kubeagents-controller-manager manager=kube-agents/k8s-operator:dev
> ```
>
> With the hardened controller deployed, a recreated agent pod is **admitted** (it stays `Pending` on a
> single-node dev cluster because the controller bakes prod-correct ~2Gi+ requests across a 4-container
> pod — faithful, not a failure). This is the first point where the **live controller-rendered** agent
> pod is observed carrying `readOnlyRootFilesystem: true` and passing the hardening VAP end-to-end.

> **Deferred, not faked (04 §6 honest scoping).** The **literal** spoke agent-reasoning-pause under a
> real hub outage — the spoke agent blocking because it cannot reach real hub-hosted inference/Minty
> over private networking — needs two clusters and is deferred to a **scratch-GKE** run. C4 proves the
> load-bearing half on Kind (cluster state + workloads survive hub loss) and never asserts the rest
> green. Parent → child is an **authority/lifecycle** edge, not a runtime dependency; the hub is
> shared-fate for agent **reasoning**, not for the cluster's **running state**.

### Phase 7 — Cloud-agnostic seams (Terraform, second CI/CD, provider-neutral observability)

Phase 7 is the **final** roadmap phase. Like Phase 6 it adds **no new persona and no new write path** —
it reduces GKE coupling ([01](docs/design/01-vision-scope.md) §6) by turning three already-unopinionated
_contracts_ into real, tested artifacts, while keeping GKE/GCP as the **zero-config default** (every knob
unset ⇒ current behavior, byte-for-byte). Three seams land:

- **IaC — Terraform HCL as well as KCC YAML.** `spec.iac.format: terraform` now has a real, committed
  provisioning exemplar. The repo ships a matched pair of equivalent clusters:
  `examples/gitops-repo/clusters/cluster-a/provisioning/*.yaml` (KCC, `format: kcc`) and
  `examples/gitops-repo/clusters/cluster-b/provisioning/*.tf` (Terraform HCL, `format: terraform`). The
  reference pipeline already dispatches `.tf`→`terraform apply` / `.y*ml`→`kubectl apply`.
- **A second CI/CD — CircleCI alongside GitHub Actions.** `examples/gitops-repo/.circleci/config.yml`
  actuates the same GitOps repo with the **same** KCC/HCL dispatch and least-privilege per-target creds as
  `.github/workflows/apply.yml`, proving actuation is genuinely unopinionated — and, per **invariant 2**,
  introducing **no agent-held write credential** (the pipeline is the privileged writer, not the agent).
- **Provider-neutral observability.** The OTLP export endpoint moves from baked-at-build to the standard
  `OTEL_EXPORTER_OTLP_ENDPOINT` env (resolved by the entrypoint, **defaulting to the existing
  `gke-managed-otel` collector**), and the observability skill's backend base URLs resolve from
  `KUBEAGENTS_OBS_BACKEND` / `OBS_*_BASE_URL` (**defaulting to `gcp`**), with a documented Prometheus/OTLP
  path for a non-GCP target.

Run the consolidated Phase 7 gate — the net-new seam validators, the vanilla (non-GKE) core-concept
acceptance, and the full prior-phase regression (the live ops are destructive and **guarded to Kind
contexts**):

```bash
local-dev/kind/verify-phase7.sh kind-kube-agents-dev
```

It proves 07 §2 Phase 7 Accept **(a)–(c)**:

- **Section A — seam validators (hermetic, CI-safe with no cluster).** `iac-parity.py` (the Terraform HCL
  exemplar is **structurally valid** — required `terraform{}`/`google_container_cluster`/
  `google_container_node_pool` blocks + attributes, balanced braces — **and semantically equivalent** to
  the KCC exemplar: same location, release channel, node machine type/count, networking; `apply.yml`
  dispatches each format correctly; a bad-HCL negative control fails) → Accept **(a)**;
  `circleci-parity.py` (valid `version: 2.1` config with an apply job + a workflow filtered to `main`,
  same KCC/HCL dispatch + per-target least-priv creds as `apply.yml`, no agent-held credential; a
  malformed-config negative control fails) → Accept **(b)**; `observability-seam.py` + `otel-endpoint.sh`
  (an env override changes the resolved endpoint/backend to a non-`googleapis.com` target; **unset ⇒ the
  exact current GKE default**, no regression; a non-GCP profile with a required URL unset **fails loudly**
  rather than silently falling back to Google).
- **Section B — vanilla (Kind, non-GKE) core-concept acceptance** → Accept **(c)**. On a vanilla upstream
  Kubernetes node (asserted from the node `kubeletVersion` carrying **no `-gke` suffix**), the Phase 1–3
  cloud-neutral core concepts hold with **no GKE dependency**: read-only agent SAR, GitOps-PR-only
  mutation, namespace isolation, the `(tier,scope)` cardinality webhook, VAP attenuation, and
  deterministic ChatOps routing (`inference_calls == 0`, proven hermetically by
  `go test -run TestGateway_ThreadAffinity`). An explicit **no-GKE-dependency** static assertion scans the
  cloud-neutral **mechanism** path (VAP, webhook, controller RBAC, router Go) for any `*.googleapis.com` /
  GKE-only API reference; the cloud-**coupled** Workload-Identity→GSA annotation is flagged
  deferred-not-faked (D1), not scanned or faked green.
- **Section C — full regression.** `verify-phase6.sh` → transitively chaos C1–C4 + `verify-phase{2,3,4,5}.sh`
  - 03 §11 `negative-attenuation.sh` + goldens + `go test ./...`, all still green (the seam changes are
    additive and default-preserving, so nothing prior moves).

> **Deferred, not faked.** A **real second cloud** — an EKS/AKS cluster with its cloud identity (IRSA /
> AAD Workload Identity) and a live `terraform apply` / cross-cloud pipeline run (D1/D2). **CLI-level
> artifact validation** — `terraform validate`/`fmt`/`apply` and `circleci config validate`: the
> `terraform` and `circleci` binaries are absent on the build host, so structural + semantic parity is
> proven **hermetically** (via `go`/`python3`) instead, exactly as Calico stood in for kindnet's missing
> NetworkPolicy enforcement in earlier phases. A **live non-GCP observability backend** queried
> end-to-end (D3). None of these are asserted green.

## Teardown & Cleanup

To safely remove provisioned resources:

### Automated Cloud Teardown

To clean up all GCP/GKE cluster resources, IAM bindings, secrets, and subscriptions provisioned by `make gcp-provision`:

```bash
cd k8s-operator
make gcp-teardown
```

You can also run step-specific teardowns:

- `make gcp-teardown-11-inference-replay`: Undeploy Inference Replay proxy
- `make gcp-teardown-10-github`: Remove GitHub Token Minter
- `make gcp-teardown-09-litellm`: Undeploy LiteLLM Gateway
- `make gcp-teardown-08-deploy`: Delete PlatformAgent CR
- `make gcp-teardown-07-secrets`: Delete Kubernetes secrets
- `make gcp-teardown-06-slack`: Reset Slack configuration
- `make gcp-teardown-05-gchat`: Remove Google Chat Pub/Sub resources
- `make gcp-teardown-04-iam`: Clean up Workload Identity and GSAs
- `make gcp-teardown-03-operator`: Undeploy operator controller and CRDs
- `make gcp-teardown-02-gvisor`: Delete gVisor node pool
- `make gcp-teardown-01-cluster`: Decommission GKE Standard cluster

### Manual Local Uninstall

To uninstall the operator controller and CRDs manually:

```bash
cd k8s-operator
export KUBE_CONTEXT=$(kubectl config current-context)
make undeploy
make uninstall
```

---

## Troubleshooting & Common FAQ

### 1. Workload Identity Authorization Errors (`403 Permission Denied`)

- Ensure the GKE Kubernetes Service Account (`kubeagents-system/kubeagents-platform-agent-ksa`) is correctly annotated with the GCP Service Account email (`iam.gke.io/gcp-service-account`).
- Verify IAM bindings using:
  ```bash
  gcloud iam service-accounts get-iam-policy <GSA_EMAIL>
  ```

### 2. Admission Webhook Errors (`x509: certificate signed by unknown authority`)

- Confirm `cert-manager` pods are running in the `cert-manager` namespace:
  ```bash
  kubectl get pods -n cert-manager
  ```
- If running the controller locally via `make run`, ensure `ENABLE_WEBHOOKS=false` is explicitly set to bypass webhooks.

### 3. GKE Autopilot Pod Pending on Lease Resources

- Check if your deployment is stuck waiting for leader election Leases in `kube-system`. Disable leader election arguments `--leader-elect=false` when deploying controllers to GKE Autopilot clusters.
