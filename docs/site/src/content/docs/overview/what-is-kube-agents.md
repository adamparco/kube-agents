---
title: What is kube-agents
description: The concrete artifacts that make up kube-agents — what installs where, and what runs when the operator is done reconciling.
---

`kube-agents` is a small collection of first-party components you install into a Kubernetes cluster (GKE today) plus a persona-and-skills workspace that tells the resulting agent how to behave.

## The components

### 1. Kubernetes operator (`k8s-operator/`)

A Go controller built with [Kubebuilder](https://kubebuilder.io) that defines the `Agent` custom resource — one kind discriminated by `spec.tier` — and reconciles it into a running agent Deployment, Service, ServiceAccount, RBAC bindings, and a `ConfigMap` for the persona and skills. Source: [`k8s-operator/`](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator).

### 2. Platform Agent (k8s-deployment)

The platform-tier `Agent` CR reconciles into a Deployment running the [Hermes runtime](https://github.com/NousResearch/hermes-agent) (`nousresearch/hermes-agent`). Inside the pod:

- **Persona (`SOUL.md`)** — the system prompt. Describes the Platform Agent's role, safety rails, autonomous recovery ladder, and reporting style.
- **Skills** (`agents/platform/skills/*/SKILL.md`) — Claude-style skill bundles the agent loads on demand.
- **Governance SOPs** (`agents/platform/governance/*.md`) — standard operating procedures the cron watchdogs invoke.
- **Cron watchdogs** (`agents/platform/cron/jobs.json`) — scheduled autonomous jobs, each pointing at a governance SOP.
- **MCP servers** — rendered by the operator into the pod's config (the file baked at `agents/platform/config.yaml` is a fallback). Shipping today, all in-pod: `platform_control` (chat + agent-internal tooling), `agent_common` (utilities shared by every tier), and `developer_knowledge` (read-only documentation lookup, reached through the in-pod `mcp_http_bridge.py`). There is deliberately no cluster-mutating MCP server.
- **Toolsets** — `cli` and `api_server` variants aggregate the MCP servers into what the Hermes CLI and REST API surface.

### 3. Inference gateway

The Platform Agent talks to an LLM through a **Completions API** proxy so provider choice is a config toggle:

- **[LiteLLM](https://litellm.ai)** for hosted models — Gemini (default), Anthropic, OpenAI, or a personal ChatGPT subscription. Example manifests: [`examples/litellm-gemini/`](https://github.com/gke-labs/kube-agents/tree/main/examples/litellm-gemini), [`examples/litellm-chatgpt-subscription/`](https://github.com/gke-labs/kube-agents/tree/main/examples/litellm-chatgpt-subscription).
- **[vLLM](https://vllm.ai)** for local, open models on GPU node pools — [`examples/vllm-gemma/`](https://github.com/gke-labs/kube-agents/tree/main/examples/vllm-gemma) serves Gemma via GKE's official inference tutorial.
- An optional **inference-replay proxy** in front of either can cache responses from a `PersistentVolumeClaim` so demos and tests replay deterministically — [`examples/inference-replay/`](https://github.com/gke-labs/kube-agents/tree/main/examples/inference-replay).

### 4. GitHub Token Minter (Minty)

Short-lived GitHub App installation tokens signed via GCP KMS and delivered through Workload Identity. This lets the write-behind IaC mirror, the provisioning bundles the cascade skills render, and the `github-issue-resolver` watchdog reach your GitOps repo without a long-lived PAT. It is not on the change path — an agent's mutation goes to its Action Broker. Source: [`k8s-operator/config/integrations/github/`](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator/config/integrations/github).

## What actually runs after `./provision.sh`

Once the [provisioning script](/kube-agents/install/quickstart-gke/) finishes, you have:

- A GKE cluster with Workload Identity.
- The operator controller manager Deployment.
- Three `Agent` custom resources — platform, cluster-admin, developer-team — and the reconciled agent Deployments, running Hermes.
- Per-tier egress NetworkPolicies and a default-deny floor on the tenant namespace (enforced only on Dataplane V2 or Calico).
- A LiteLLM Deployment (or vLLM if you opted in).
- A Minty Deployment plus a GCP KMS keyring and key.
- A Google Chat Pub/Sub topic + subscription and a Kubernetes `Secret` holding your model provider API key.
- Optionally: Slack tokens as a `Secret` (only if `SLACK_ENABLED=true` during provisioning) and the inference-replay proxy (only if `INFERENCE_REPLAY_ENABLED=true`).

## What is _not_ included

- **No Helm chart yet** — [PR #353](https://github.com/gke-labs/kube-agents/pull/353) is proposing one. Today, install is via `./provision.sh` + Kustomize.
- **No local Kind path yet** — same PR proposes `dev/setup-kind.sh`. Today you need a real cluster.
- **No web UI or CLI beyond `kubectl` port-forward + the Hermes API** — chat is the primary user interface.
- **No cross-cloud abstractions** — the shipping MCP toolset, IAM assumptions, and provisioning scripts all target GKE. The runtime and persona are cluster-agnostic; the skill catalog is not.

## Where to go next

- [Proactive autonomy](/kube-agents/overview/proactive-autonomy/) — the background watchdogs and how they close loops.
- [Architecture](/kube-agents/overview/architecture/) — how requests and cron ticks flow through the components.
- [Quick start (GKE)](/kube-agents/install/quickstart-gke/) — run `./provision.sh` end-to-end.
