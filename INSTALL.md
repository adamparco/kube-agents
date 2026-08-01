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
5. [Method 3: Remote Development & Fast Iteration](#method-3-remote-development--fast-iteration)
   - [Bring the cluster up](#bring-the-cluster-up)
   - [Build your working tree onto the cluster](#build-your-working-tree-onto-the-cluster-read-this-before-any-phase)
   - [Phase 2 — inner loop (Cluster Admin Agent + cascade)](#phase-2--inner-loop-cluster-admin-agent--cascade)
   - [Phase 3 — inner loop (Developer Team Agent + namespace isolation)](#phase-3--inner-loop-developer-team-agent--namespace-isolation)
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
- **Kubernetes Operator (`k8s-operator`)**: A Kubebuilder-powered Go operator that manages the `Agent` Custom Resource Definition — one kind discriminated by `spec.tier` into `platform`, `cluster-admin`, and `developer-team` — and reconciles cluster lifecycle state.
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
8. **08: Platform-tier Agent CR** (`make gcp-provision-08-deploy`)
9. **09: LiteLLM Gateway** (`make gcp-provision-09-litellm`)
10. **10: GitHub Token Minter** (`make gcp-provision-10-github`)
11. **11: Inference Replay Proxy** (`make gcp-provision-11-inference-replay`)
12. **12: Child Agent Tiers** (`make gcp-provision-12-agent-tiers`) — cluster-admin + developer-team
13. **13: Network Policies** (`make gcp-provision-13-network-policies`) — per-tier egress allowlist, then the tenant default-deny floor

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

#### Refreshing an existing install

Once the install exists, `make live-refresh` (from the repo root, or from `k8s-operator/`) is the
single command that puts your working tree on it:

```bash
make live-refresh
```

It builds all seven first-party images on Cloud Build at tag `src-<sha>`, confirms each one resolves
in Artifact Registry, writes the five image pins into `scripts/vars.sh`, runs all 13 provisioning
steps, and then compares every running container's `imageID` against the digests it just published —
so a green run means the cluster really is on that build, not that a command completed.

- The target cluster is **read from `scripts/vars.sh`, never prompted for**, and the script prints
  it along with the exact tag change and requires you to type the cluster name back. Pass
  `ARGS="--yes"` to skip that prompt in automation.
- It **refuses a `gke-scratch-*` cluster** and points at `dev/cluster/reload-images.sh`, which is
  the inner-loop equivalent and deploys by digest.
- A dirty working tree is refused, because the tag `src-<sha>` would name a commit that does not
  contain your changes. `ARGS="--allow-dirty"` tags it as visibly not a commit.
- Other flags: `ARGS="--dry-run"`, `ARGS="--skip-build"` (images for this tag already exist),
  `ARGS="--tag my-experiment"`.

Exit codes distinguish the failures that need different responses: `2` refused, `3` missing tooling
or configuration, `4` an image did not build or is absent from the registry, `5` the images are
published and correct but a workload did not converge onto them.

#### Step 3: Verify Running Components

Verify that the operator, LiteLLM gateway, and custom resources are healthy:

```bash
kubectl get deployments -n kubeagents-system
kubectl get pods -n kubeagents-system
kubectl get agents --all-namespaces
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
> context, but **refuses** to proceed unless that context matches the anchored `gke-scratch-*` arm
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

Submit a sample `Agent` Custom Resource to activate cluster governance (run inside `k8s-operator/`):

```bash
kubectl apply -f examples/agent.yaml
kubectl get agents -A
```

---

## Method 3: Remote Development & Fast Iteration

The inner development loop runs against one remote cluster: GKE **`kube-agents-dev`** in project
`adamparco-kage`, zone `us-east4-a` — zonal, two `e2-standard-4` nodes, **Dataplane V2**, **Workload
Identity**, image streaming. Every verification entry point below targets it, and `dev/cluster/up.sh`
builds exactly the stack they expect.

Its kube context is **`gke-scratch-kube-agents-dev`**, and the `gke-scratch-` prefix is a security
control rather than a label. The suites below apply deliberately-bad RBAC, delete pods and exercise
denial paths, so each one guards itself with an anchored `case "$CTX" in gke-scratch-*)` and refuses
every other context. gcloud's own generated name for this cluster —
`gke_adamparco-kage_us-east4-a_kube-agents-dev` — matches no guard, which is why `up.sh` renames it:
the rename is what makes the cluster addressable by the suite at all, and nothing renames
`platform-agent-host`, which is how the live install in the same project stays un-addressable by it.

### Bring the cluster up

You need `gcloud` authenticated (`gcloud auth login` plus `gcloud auth application-default login`),
`kubectl`, and a GCP project with the `container`, `cloudbuild`, `artifactregistry` and `compute`
APIs enabled. `up.sh` assumes none of it: a project preflight (`dev/lib/substrate-capacity.sh`) runs
before anything is created and checks that a project is set, that those four APIs are enabled, that
the regional CPUS quota has room for the 8 vCPU this cluster needs, and that Artifact Registry
answers — refusing with exit 2 and the exact `gcloud services enable …` line to run. It also closes
by naming what it did **not** measure, because a preflight grown one incident at a time only ever
measures the previous incident, and a disabled `container.googleapis.com` fails `clusters create`
with an access error that reads like an IAM problem and sends you to permissions, where everything
is already correct.

```bash
bash dev/cluster/up.sh      # create + cert-manager + operator + VAP + agent images (5-8 min cold)
bash dev/cluster/pause.sh   # every node pool -> 0
bash dev/cluster/resume.sh  # nodes back, ~2 min
bash dev/cluster/down.sh    # delete the cluster and its kube context
```

`up.sh` is idempotent, and re-running it is the supported way to pick up a source change. It asserts
two properties at bring-up instead of assuming them, because both fail quietly downstream: at least
**two Ready nodes** (`ReadWriteOnce` excludes per _node_, so the multi-attach conflict a co-existence
claim rests on cannot be exhibited at all on one) and a dataplane precondition P4 recognises. Either
one missing is exit 4 here, rather than a deferral discovered forty minutes into a gate.

**`pause.sh` is the normal between-session action.** It resizes the node pools to zero, which leaves
the control-plane fee as the whole bill and keeps every API object exactly as you left it — etcd
lives in the control plane, so the CRDs, the Agent CRs, the VAP and the namespaces all survive a
pause. What does not survive is the running pods; `resume.sh` restores two nodes in about two
minutes and the controllers reconcile. `down.sh` is rarely what you want: deleting costs 3-5 minutes
plus a full cert-manager + operator + agent-image reinstall to undo, and it is for a cluster that is
the wrong _shape_ — most often a dataplane P4 does not recognise, which GKE cannot enable in place.
It refuses any cluster name but `kube-agents-dev`, compared with `=` and not a glob, because it
addresses the cluster through the GCP API by name and never uses a context at all, so the name is
the only guard available to it.

Running, this costs roughly **$0.35-0.45/hr**; paused, about **$0.10/hr** for the control plane.

For a tight edit-run loop that needs no image build, the controller still runs from your host
against the cluster with the webhook off:

```bash
kubectl config use-context gke-scratch-kube-agents-dev
cd k8s-operator
make install KUBE_CONTEXT=gke-scratch-kube-agents-dev
ENABLE_WEBHOOKS=false make run
```

Two things that mode is not. The in-cluster controller keeps running and a host-run one does not
join its leader election (`--leader-elect` defaults to false off-cluster), so scale
`deploy/kubeagents-controller-manager` to 0 first unless you want two reconcilers writing the same
objects. And nothing under `dev/verify/` is meaningful with `ENABLE_WEBHOOKS=false`: the cardinality,
placement and tier-immutability claims are admission claims, so a green would be a statement about a
webhook that is not serving.

To rebuild and roll out a **platform** agent image on the Method 1 install (the cluster named in
`k8s-operator/scripts/vars.sh`, not the inner-loop cluster), use `make dev-rebuild-agent ARGS="platform"`.
That covers the platform agent image only — not the operator, the router, or the two child tiers —
and it does not write the tag back to `vars.sh`, so the next `provision_08` reverts it. To refresh
the whole install, use [`make live-refresh`](#refreshing-an-existing-install).

### Build your working tree onto the cluster (read this before any phase)

The phases below all assume the cluster is running **your working-tree code**, not the upstream
published image. Two things make this non-obvious, and both have bitten this repo:

- **`make deploy` does _not_ build.** It only runs `kustomize set image` + `kubectl apply`. So
  `make deploy IMG=ghcr.io/gke-labs/kube-agents/k8s-operator:v0.1.0` deploys the **upstream**
  binary and compiles nothing of yours.
- **There are three image families**: the **operator image** (the controller + webhook), the
  **router image** (`kage-router`, built from `k8s-operator/Dockerfile.router` — the same tree, a
  different binary), and the three **agent images** (`platform` / `cluster-admin` /
  `developer-team`). A change under `k8s-operator/internal/router/` needs the second, other changes
  under `k8s-operator/` the first, a change under `agents/<tier>/` the third, and none of them is
  rebuilt by deploying. The router is the one to watch: `make deploy` does not rewrite its image at
  all — it is pinned in `config/router/kustomization.yaml` — and the pinned default
  `ghcr.io/gke-labs/kube-agents/kage-router:v0.1.0` answers an anonymous pull with `403`, so a
  cluster that never builds it gets a router in `ErrImagePull` rather than one running upstream code.

One helper builds either or both. It is guarded to `gke-scratch-*` contexts, so it can never repoint
a workload on a cluster that matters:

```bash
# operator (controller + webhook): build, push, repoint the controller at the new digest
bash dev/cluster/reload-images.sh operator gke-scratch-kube-agents-dev

# the kage-router (the read-only ChatOps front door — a separate image from the same tree)
bash dev/cluster/reload-images.sh router   gke-scratch-kube-agents-dev

# the three tier agent images, plus a patch of every Agent CR of the matching tier
bash dev/cluster/reload-images.sh agents   gke-scratch-kube-agents-dev

# all three
bash dev/cluster/reload-images.sh all      gke-scratch-kube-agents-dev
```

**It builds on Cloud Build, and that is not one option among several.** This host is arm64 and every
image target is amd64: a local `docker build` produces images no node can execute, and the failure
does not arrive at build time — it arrives minutes later as a CrashLoopBackOff with
`exec format error` in some other component. The agent tiers are `FROM nousresearch/hermes-agent`, a
whole userspace rather than one static binary, so the `$PREBUILT_BINARY` cross-compile hatch that
serves the Go images does not rescue them either.

`gcloud builds submit` reporting success tells you a build ran; it does not tell you what is in the
registry under that tag. So the helper reads the digest back out of Artifact Registry
(`us-east4-docker.pkg.dev/adamparco-kage/kube-agents`) and **deploys by digest** — `kubectl set image`
with `manager=…@sha256:…` for the controller, a merge patch of `spec.deployment.image` for each Agent
CR. A tag that built and does not resolve is exit 4, not a warning: every result downstream of it
would describe whatever was there before.

Two consequences worth stating outright, because each replaces an instruction that used to be
load-bearing:

- **There is no `kind load` and no `rollout restart`.** A changed digest changes the Deployment spec,
  which _is_ a rollout; an unchanged digest is genuinely the same image, and restarting it would
  prove nothing.
- **The stale-image trap is closed structurally, not merely detected.** The old loop reused a fixed
  tag (`:dev`, `:latest`), and same tag + `imagePullPolicy: IfNotPresent` means the kubelet keeps a
  copy it already has — so a rebuilt image silently did not take effect and the gate reported on the
  previous build. That is LSN-001, which recurred three times. A digest names one immutable manifest,
  so the outcome is now unrepresentable rather than warned about, and the pull policy is irrelevant
  instead of load-bearing. Precondition **P1** still asserts it, because "unrepresentable through
  this path" and "true on this cluster right now" are different claims: each gate reads the running
  pod's `imageID`, resolves that digest in Artifact Registry, and requires the tags it carries to
  name the commit this tree is on (`dev-<sha>`, or `dev-<sha>-dirty-<epoch>` for an uncommitted edit,
  whose epoch is compared against the mtime of the dirty files). A cluster running the upstream image
  or a build of a different commit fails the gate instead of passing it.

> If you only want the upstream published image (a quick smoke test, not testing your code), you
> can `cd k8s-operator && make deploy IMG=ghcr.io/gke-labs/kube-agents/k8s-operator:v0.1.0 KUBE_CONTEXT=gke-scratch-kube-agents-dev`
> — but understand that this tests **upstream**, not your working tree, and P1 will say so.

### Phase 2 — inner loop (Cluster Admin Agent + cascade)

Phase 2 adds the tier-discriminated `Agent` CRD, the read-only
**Cluster Admin Agent** persona, the standalone **kage-router** ChatOps front door, the **F4
provisioning cascade** (the Platform Agent proposes a subordinate cluster-admin bundle as a GitOps PR),
and the **spoke bootstrap** ordered apply waves. Verify the whole inner loop on the dev cluster:

1. **Bring up the stack** — cluster → cert-manager → the operator you just built, deployed at its
   digest → the read-only VAP → the three tier agent images. `up.sh` does all of it, in that order,
   and is idempotent, so this is also how you re-run it after a source change:
   ```bash
   bash dev/cluster/up.sh
   ```
   The VAP needs `ValidatingAdmissionPolicy` GA (K8s ≥ 1.30). The regular release channel is well
   past that, and `verify-phase2.sh` asserts the server version rather than trusting the channel.
2. **Run the consolidated verification gate** (destructive; guarded to `gke-scratch-*` contexts only):
   ```bash
   dev/verify/verify-phase2.sh gke-scratch-kube-agents-dev
   ```
   It exercises the load-bearing suites: live webhook serving (duplicate `(tier,scope)` + tier
   immutability rejected), VAP attenuation (write/impersonate/wrong-scope denied), read-only per-tier
   SAR, the cascade render → VAP dry-run, bootstrap ordering (pod binds the pre-created SA), and the
   no-break-glass check. The deterministic router/index suites run under `cd k8s-operator && go test ./...`.
3. **Egress enforcement (V-K11) is a separate script, and it now passes.** It is a different claim
   from everything above — it needs traffic, not YAML — so it lives one line up the same L2 chain:
   ```bash
   dev/verify/egress-enforcement-l2.sh gke-scratch-kube-agents-dev
   ```
   This was a standing deferral for six phases: kindnet, the CNI it was measured on, accepts a
   NetworkPolicy, returns 201, stores it and enforces nothing, so every green from a network check
   there was a statement about the API server's willingness to persist YAML (LSN-006). Dataplane V2
   enforces, so the **shipped rendered policy** is now proven to deny for real: an off-allowlist
   destination blocked, an off-allowlist port blocked on an allowlisted namespace, the metadata
   address absent, and a no-policy baseline first so a deny cannot be a DNS failure in disguise.
   **The rule that produced the deferral survives untouched:** enforcement is a property of the
   dataplane, not of the API server, and a cluster accepts a NetworkPolicy whether or not it will
   ever enforce one. `p4_assert_enforcing_dataplane` in `dev/lib/preconditions.sh` is therefore an
   **allow-list** of dataplanes known to enforce — `calico-node`, `anetd` (Dataplane V2), `cilium` —
   and anything it does not recognise is `deferred`, never `pass`. A deny-list would get today's case
   right and the next unrecognised dataplane wrong, in the one direction that produces a false green.

### Phase 3 — inner loop (Developer Team Agent + namespace isolation)

Phase 3 adds the read-only **Developer Team Agent** (one per namespace), the load-bearing **A1
placement clause** (a developer-team `Agent` must be created in the namespace it scopes —
`metadata.namespace == spec.scope.namespace`), the per-namespace **isolation baseline**
(default-deny NetworkPolicy + a per-tier egress allowlist, `ResourceQuota`, and in-namespace
`ExternalName` aliases for the shared hub services), the **`provision-developer-team`** cascade on the
Cluster Admin Agent, and the router completion (NL confidence/clarify, candidate validity, thread
affinity, audit attribution). It reuses the Phase 2 stack on the same cluster.

> **Image refresh (important).** The webhook and the controller run inside the operator image, so
> after **any** change to `k8s-operator/internal/webhook` or `.../controller` the cluster is still
> serving the previous build until you push a new one:
>
> ```bash
> bash dev/cluster/reload-images.sh operator gke-scratch-kube-agents-dev
> ```
>
> What changed is the cost of forgetting. A same-tag image with `imagePullPolicy: IfNotPresent` used
> to keep serving the stale build and **silently under-enforce** an admission invariant — which is
> exactly how a Phase 3 placement escape first slipped through. Deploying by digest ends that
> particular silence: the gate's P1 assertion resolves the running digest in Artifact Registry and
> halts with "the cluster runs a build of a DIFFERENT COMMIT" rather than reporting green about code
> you are not testing.
>
> Equivalent longhand, if you prefer to see each step:
>
> ```bash
> gcloud builds submit --config deploy/docker/cloudbuild.yaml \
>   --substitutions=_IMAGE_URI=us-east4-docker.pkg.dev/adamparco-kage/kube-agents/k8s-operator:dev-$(git rev-parse --short HEAD),_CACHE_URI=us-east4-docker.pkg.dev/adamparco-kage/kube-agents/k8s-operator:buildcache,_CONTEXT=k8s-operator,_DOCKERFILE=k8s-operator/Dockerfile .
> D=$(gcloud artifacts docker images describe us-east4-docker.pkg.dev/adamparco-kage/kube-agents/k8s-operator:dev-$(git rev-parse --short HEAD) --format='value(image_summary.digest)')
> kubectl -n kubeagents-system set image deploy/kubeagents-controller-manager manager=us-east4-docker.pkg.dev/adamparco-kage/kube-agents/k8s-operator@$D
> kubectl -n kubeagents-system rollout status deploy/kubeagents-controller-manager --timeout=180s
> ```

1. **Run the consolidated Phase 3 gate** (destructive; guarded to `gke-scratch-*` contexts only). It applies the
   `team-x` tenant bundle (`namespaces/team-x/` `00`→`60`, in numeric order) and the dev-team `Agent`
   CR, then asserts the whole isolation proof:
   ```bash
   dev/verify/verify-phase3.sh gke-scratch-kube-agents-dev
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
2. **Egress enforcement is proven here too, and the two-script split is no longer a substrate
   artifact.** `verify-phase3.sh` judges the egress policy **structurally** — shape, tier selector,
   a pure allowlist with zero `0.0.0.0/0`, server-dry-run valid — and asserts nothing about traffic;
   enforcement is `egress-enforcement-l2.sh`, exactly as in Phase 2, and it passes on this cluster.
   Keep reading them as two claims rather than one: a green P3-K6 says the shipped policy is well
   formed, and only the enforcement script's exit code says an agent pod cannot in fact reach
   `169.254.169.254` or the open internet. A file is not traffic even on a dataplane that enforces.

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
trigger changes only _when_ an agent wakes, never _what_ it may do — every resulting change still
flows through the tier's own change path, which the trigger neither widens nor bypasses.

> **Both halves of Phase 4's coordination model have since been superseded, and this section is kept
> as the record of what Phase 4 accepted.** The P13-T5 persona conversion (02 §2.1) replaced the
> reviewed-GitOps-PR change path with **`apply-change`** — an Action Envelope submitted to the tier's
> own Action Broker — and replaced the indirect, OKF-mediated escalation with **`escalate`**, a direct
> one-hop mesh call to `parentRef`, which is the opposite of invariant 3 as stated above. `read-knowledge`
> survives unchanged; OKF remains the knowledge layer and stops being a coordination channel.
> **Consequence for the gate below:** its (b) leg's skill-level half tested the deleted escalation
> module, and its (e) leg's second half tested the deleted proposal module. Both invocations were
> removed from the gate rather than left pointed at files that error at import; the properties they
> carried now live in `dev/tests/mesh-skills-encode-the-contract.py` (the mesh contract, on the L0
> chain) and `dev/test_apply_change_skill.py` (the envelope path). What the (b) leg still asserts
> structurally — that a child's egress NetworkPolicy carries no parent-tier destination — is a
> Phase 4 property that the mesh supersedes, so read a green (b) as a record of what Phase 4
> accepted, not as a current claim about agent-to-agent reachability.

> **The image half of this caveat is gone; the transport half is not.** The watcher sidecar, the
> controller's per-tier watcher-arg rendering and the seam hardening all ship inside the
> operator/agent images, which used to share the `v0.1.0` tag — so a stale same-tag image read green
> while running Phase-3 code, and the live Event→session spawn could not be trusted without a rebuild
> nobody could verify had happened. `reload-images.sh` builds those images and deploys them **by
> digest**, so the **in-pod terminus** is now reachable evidence rather than a blocked claim. What no
> target in this build can originate is the **transport**: real alert Pub/Sub delivery and a real
> GitHub webhook HMAC. The gate below proves the terminus, the render and the per-tier scoping
> hermetically, which is why it is trustworthy with or without a cluster.

1. **Run the consolidated Phase 4 gate** (the live regression is destructive and guarded to
   `gke-scratch-*` contexts; the hermetic acceptance runs anywhere, so this is CI-safe with no cluster):
   ```bash
   dev/verify/verify-phase4.sh gke-scratch-kube-agents-dev
   ```
   It proves 07 §2 Phase 4 Accept **(a)–(e)** hermetically — **(a)** per-tier scoped watcher +
   fail-closed `validate()` + controller `--owner`/`--scope-namespace` rendering + the hardened
   inject seam (bearer/owner auth, `kind` discriminator); **(b)** the escalation round-trip is
   **indirect** (the escalation file written by the child's dry-run change path, read back via
   `read-knowledge`, with the
   child egress NetworkPolicy carrying **no parent-tier destination** — cross-tier flow is GitOps +
   loopback only); **(c)** a runbook is retrieved through the sparse read-only OKF path (which can
   never push) with `okf-validate` green; **(d)** per-tier heartbeats run **scoped** audits
   (cluster-admin over its cluster, developer-team over its namespace only) and route any change to a
   PR; **(e)** injected drift yields a **corrective-PR artifact** while the drifted live object stays
   present (detect-and-propose, never fix) — then re-runs the load-bearing **regression** live on the
   dev cluster (03 §11 `negative-attenuation.sh`, the dev-team read-only SAR under a trigger, and the
   08 §7 controller-mints-no-RBAC golden).
2. **Deferred, not faked:** the cloud transport legs — alert Pub/Sub delivery and GitHub webhook HMAC
   — have no originator in this build, so the gate proves the in-pod terminus and all
   rendering/scoping logic instead. **05 §8 chaos** (failure-isolation) is Phase 6 and is marked N-A
   here rather than silently skipped.

### Phase 5 — Security gate & hardening (review-gate CI, egress, pod hardening, attribution)

Phase 5 makes the security model **continuously enforced** rather than set-once, without relaxing any
invariant (agents stay read-only, the only write path is a reviewed PR). Four deltas land: (1) the
**review-gate CI** (06 §7) — the agent-driven `review-security-k8s-*` skills run on every PR (and a
heartbeat re-run) via a **headless detector**, emit findings tagged with a **severity**, and a
**hermetic Python scorer** turns "any unmitigated high/critical" into a **merge block**; a finding is
mitigated only by a matching, non-expired entry in `security-review-waivers.yaml` (fingerprint =
`sha256(agent\nfile\nnormalize(message))[:16]`). (2) A **per-tier egress allowlist** for all three tiers
(platform is net-new) plus a **real enforcement proof** on the dev cluster. (3) The **hardened pod-security
context on every agent pod** made continuously enforced — PSS `enforce: restricted` on the namespace
plus a focused `vap-agent-pod-hardening` VAP that requires `readOnlyRootFilesystem: true` on every
`kube-agents/tier` pod (restricted-PSS does not cover it), composing with — never colliding with — the
RBAC-governing `vap-agent-readonly`. (4) **End-to-end attribution** — the authenticated requester +
per-turn trace id flow router → inject seam → session → the change path, and at Phase 5 that path
ended in a mutation PR carrying durable `Requested-by:` / `Trace-Id:` trailers (which squash-merge
lands in `main`'s history).

> **The attribution terminus moved with the change path.** P13-T5 deleted the GitOps change path, so
> nothing stamps git trailers any more; 06 §4.1 carries requester and trace on the **Action Envelope**
> and journals them on the `ActionRecord` instead. What Phase 5 proved and what still holds is the
> **carriage** — router → inject seam → session, with the router audit tying `Sender` to the `TraceID`
> that reaches dispatch. The terminus itself has no live witness in this tree today.

> **Both criteria that needed capable infrastructure now have it.** Egress **enforcement** (b) needs a
> NetworkPolicy-enforcing CNI, and Dataplane V2 is one, so `dev/tests/egress-enforcement.sh` proves an
> off-allowlist destination is actually blocked on the same cluster the rest of the gate runs on,
> rather than the shape being checked here and the deny/allow answered somewhere else.
> That moved the gate as well as the result: an `rc 3` (no enforcing dataplane) was a tolerated,
> non-fatal deferral and is now a **failure**, because the only accepted target is built with
> Dataplane V2, so rc 3 there means a broken cluster or an allow-list that has not learned its
> dataplane — not an expected outcome. On a BLOCKING-ALWAYS security property, "nobody could measure
> it" and "there is nothing to measure it with" must not print the same thing. The pod-hardening
> **VAP** (c) needs K8s ≥ 1.30 (VAP GA), which the regular release channel satisfies; a
> freshly-applied VAP binding still has a short activation delay, so the gate polls the admission
> dry-run until the binding is live before judging.

1. **Run the consolidated Phase 5 gate** (the live checks are destructive and guarded to `gke-scratch-*`
   contexts; the hermetic acceptance runs anywhere, so this is CI-safe with no cluster):
   ```bash
   dev/verify/verify-phase5.sh gke-scratch-kube-agents-dev
   ```
   It proves 07 §2 Phase 5 Accept **(a)–(d)** — **(a)** `score_findings.py` BLOCKS an unmitigated `high`
   (exit 1), PASSES a clean set (exit 0), lets a matching non-expired waiver mitigate, and still BLOCKS on
   an **expired** waiver (negative control), backed by the scorer + extractor unit suites; **(b)** all
   three tier egress netpols are pure allowlists (`policyTypes:[Egress]`, tier `podSelector`, **no
   `0.0.0.0/0`**), and live egress enforcement is **PROVEN** on this cluster — a no-policy baseline
   first, so that an off-allowlist destination going dark afterwards can only be the policy and not a
   DNS or scheduling failure wearing its costume; **(c)** the go goldens carry
   `readOnlyRootFilesystem: true` on every rendered container, the namespace carries the PSS
   `restricted` label, both VAPs are present, and — live —
   the pod-hardening VAP **rejects** an un-hardened `kube-agents/tier` pod (the error names
   `readOnlyRootFilesystem`), **admits** a hardened one, and leaves a non-agent pod **untouched** (scope
   proof); **(d)** the router audit ties `Sender` to the `TraceID` carried through to dispatch — the
   half that asserted git trailers on a proposal PR (flag > env > autonomous fallback, single-line,
   idempotent) tested a module P13-T5 deleted, and was removed from the gate rather than left pointed
   at a missing file. A brokered change is attributed by its `ActionRecord`, not by a git trailer, so
   the remaining router-audit tie is the whole of (d) now. It then re-runs the load-bearing **regression**
   live on the dev cluster (03 §11 `negative-attenuation.sh`) plus the full prior-phase gates
   (`verify-phase{2,3,4}.sh`) and `go test ./...`.
2. **Deferred, not faked:** the **live headless detector** in `review-gate.yml` needs the
   `ANTHROPIC_API_KEY` secret + live creds and skips gracefully on fork PRs — the scorer, which is the authoritative gate, always runs and is proven
   hermetically; the **hostname-precise L7 egress proxy**, **cross-object webhook**, **gVisor
   execution sandbox**, and **per-request user down-scoping** remain deferred hardening (08 §5). The
   gVisor node pool is deliberately absent from the dev cluster — 08 §5's sandbox checks are
   unwritten, so a pool would be an extra node running nothing, and `up.sh` prints the single
   `gcloud container node-pools create … --sandbox type=gvisor` command that adds it the day they
   land. **05 §8 chaos** (failure-isolation) is Phase 6 and is marked N-A here rather than silently
   skipped.

### Phase 6 — Failure-isolation & resilience (chaos: no cascade)

Phase 6 is a **validation phase** — it adds no new persona and no new write path. It graduates the
05 §8 **failure-isolation (chaos)** suite from deferred to a live, load-bearing gate, proving the
design's central resilience claim: **no cascade failure** (04 §6). Four experiments run against the
dev cluster (`dev/verify/chaos-suite.sh`):

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
(the live ops are destructive and **guarded to `gke-scratch-*` contexts**; every op is reversible,
single-object, and self-cleaning):

```bash
dev/verify/verify-phase6.sh gke-scratch-kube-agents-dev
```

> **The cluster must run the controller you built — and now it does by construction.** The published
> `k8s-operator:v0.1.0` image predates the Phase 5 pod hardening: it renders agent pods **without**
> `readOnlyRootFilesystem`, which the `vap-agent-pod-hardening` VAP (correctly) rejects at admission,
> so a recreated pod never appears and C2 reads as a controller that failed to relaunch. `up.sh`
> deploys the operator it just built, at its digest, so a cluster brought up by it is already right;
> after a source change, one command restores that:
>
> ```bash
> bash dev/cluster/reload-images.sh operator gke-scratch-kube-agents-dev
> ```
>
> With the hardened controller deployed, a recreated agent pod is **admitted**. The suite tolerates a
> replacement that is still `Pending` — the controller bakes prod-correct ~2Gi+ requests across a
> 4-container pod, and C2's claim is that the controller recreates the object, not that a node had
> room for it. This is the point where the **live controller-rendered** agent pod is observed carrying
> `readOnlyRootFilesystem: true` and passing the hardening VAP end-to-end.

> **Deferred, not faked (04 §6 honest scoping).** The **literal** spoke agent-reasoning-pause under a
> real hub outage — the spoke agent blocking because it cannot reach real hub-hosted inference/Minty
> over private networking — needs **two** clusters, and the inner loop provisions one. C4 proves the
> load-bearing half here (cluster state + workloads survive hub loss) and never asserts the rest
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

Run the consolidated Phase 7 gate — the net-new seam validators, the core-concept acceptance, and the
full prior-phase regression (the live ops are destructive and **guarded to `gke-scratch-*` contexts**):

```bash
dev/verify/verify-phase7.sh gke-scratch-kube-agents-dev
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
- **Section B — core-concept acceptance** → Accept **(c)**. The Phase 1–3 cloud-neutral core concepts
  hold with **no GKE dependency**: read-only agent SAR, GitOps-PR-only mutation, namespace isolation,
  the `(tier,scope)` cardinality webhook, VAP attenuation, and deterministic ChatOps routing
  (`inference_calls == 0`, proven hermetically by `go test -run TestGateway_ThreadAffinity`). An explicit
  **no-GKE-dependency** static assertion scans the cloud-neutral **mechanism** path (VAP, webhook,
  controller RBAC, router Go) for any `*.googleapis.com` / GKE-only API reference; it reads the mechanism
  source and not the cluster under it, which is what makes it the load-bearing cloud-neutrality claim and
  keeps it hermetic. The cloud-**coupled** Workload-Identity→GSA annotation is flagged deferred-not-faked
  (D1), not scanned or faked green.

  **B0 — the target itself being a vanilla, non-GKE distribution — is the one criterion here that moved
  backwards.** It used to be asserted directly, by reading the node `kubeletVersion` and requiring **no
  `-gke` suffix**; the inner loop is a GKE cluster, so the assertion now has nothing to run against and
  is recorded as a deferral (**D4**) rather than deleted — the criterion is still the right one and the
  blocker is purely which clusters exist. The script still reads and prints the `kubeletVersion`, because
  a deferral that stops looking cannot tell you the day it stopped being true. The live half
  (`verify-phase2.sh` + `verify-phase3.sh`) still runs and is still load-bearing; what it no longer is,
  while B0 is deferred, is evidence of **portability**, since the distribution under it is the same one
  the live install runs on.

- **Section C — full regression.** `verify-phase6.sh` → transitively chaos C1–C4 + `verify-phase{2,3,4,5}.sh`
  - 03 §11 `negative-attenuation.sh` + goldens + `go test ./...`, all still green (the seam changes are
    additive and default-preserving, so nothing prior moves).

> **Deferred, not faked.** A **vanilla, non-GKE Kubernetes target** (D4) — B0 above; the blocker is that
> none is provisioned. D4 is weaker than D1 and separately promotable: D1 wants a second **cloud** with
> its own identity system, D4 wants only a second **distribution**, and a k3s VM would discharge it. A
> **real second cloud** — an EKS/AKS cluster with its cloud identity (IRSA / AAD Workload Identity) and a
> live `terraform apply` / cross-cloud pipeline run (D1/D2). **CLI-level artifact validation** —
> `terraform validate`/`fmt`/`apply` and `circleci config validate`: the `terraform` and `circleci`
> binaries are absent on the build host, so structural + semantic parity is proven **hermetically** (via
> `go`/`python3`) instead. A **live non-GCP observability backend** queried end-to-end (D3). None of
> these are asserted green.

## Teardown & Cleanup

To safely remove provisioned resources:

### Automated Cloud Teardown

To clean up all GCP/GKE cluster resources, IAM bindings, secrets, and subscriptions provisioned by `make gcp-provision`:

```bash
cd k8s-operator
make gcp-teardown
```

You can also run step-specific teardowns:

- `make gcp-teardown-13-network-policies`: Remove the agent egress and tenant default-deny policies
- `make gcp-teardown-12-agent-tiers`: Remove the cluster-admin and developer-team tiers
- `make gcp-teardown-11-inference-replay`: Undeploy Inference Replay proxy
- `make gcp-teardown-10-github`: Remove GitHub Token Minter
- `make gcp-teardown-09-litellm`: Undeploy LiteLLM Gateway
- `make gcp-teardown-08-deploy`: Delete the platform-tier Agent CR
- `make gcp-teardown-07-secrets`: Delete Kubernetes secrets
- `make gcp-teardown-06-slack`: Reset Slack configuration
- `make gcp-teardown-05-gchat`: Remove Google Chat Pub/Sub resources
- `make gcp-teardown-04-iam`: Clean up Workload Identity and GSAs
- `make gcp-teardown-03-operator`: Undeploy operator controller and CRDs
- `make gcp-teardown-02-gvisor`: Delete gVisor node pool
- `make gcp-teardown-01-cluster`: Decommission GKE Standard cluster

### Manual Uninstall

To uninstall the operator controller and CRDs manually, naming the cluster you mean:

```bash
cd k8s-operator
make undeploy  KUBE_CONTEXT=gke_<project>_<region>_<cluster>
make uninstall KUBE_CONTEXT=gke_<project>_<region>_<cluster>
```

Type the context rather than `KUBE_CONTEXT=$(kubectl config current-context)`. That form reads as a
safety measure and is the opposite of one: it launders whatever context happens to be selected past
the check that exists to make you say which cluster you are deleting a CRD from, and deleting a CRD
deletes every custom resource of that kind. `make uninstall` has no undo and no confirmation.

For the inner-loop cluster there is nothing to uninstall — `bash dev/cluster/down.sh` deletes the
whole cluster, and `bash dev/cluster/pause.sh` is what you want between sessions.

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
