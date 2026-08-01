---
title: Glossary
description: Terminology used in kube-agents and the wider agentic-Kubernetes ecosystem.
sidebar:
  order: 4
---

Terms used throughout the `kube-agents` docs and the wider agentic-Kubernetes ecosystem. Ported from [`docs/glossary.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/glossary.md).

## `kube-agents` concepts

### Platform Agent

The single autonomous agent shipped in `agents/platform/`. Configured with the `SOUL.md` persona, a library of skills, governance SOPs, and cron watchdogs. Deployed as a Kubernetes Deployment running the [Hermes runtime](https://github.com/NousResearch/hermes-agent).

### Governance SOP

A standard operating procedure in `agents/platform/governance/`. Codifies how a fleet-wide audit or reconciliation is performed. Invoked by cron watchdogs or on request.

### Skill

A Claude-style `SKILL.md` bundle in `agents/<tier>/skills/`. Loaded on demand based on its frontmatter `description`. Skills are allocated to the tier whose authority they match, so an agent only sees its own set.

### Watchdog

A cron-scheduled job in `agents/platform/cron/jobs.json` that fires a pre-authored prompt at the Platform Agent on a schedule.

### Declarative workflow

The path every infrastructure change takes: the agent describes the desired state as an **Action Envelope** and submits it with `apply-change`; its **Action Broker** classifies, gates, executes, verifies and journals it. Git is a write-behind mirror or an executing GitOps engine, never the gate.

### Action Broker

The companion workload beside each agent pod, holding the **actor** identity — the only write identity in that agent's scope. It derives `(tier, scope)` from the authenticated caller rather than from the envelope, plans an undo before executing, and journals an `ActionRecord`.

### Action Envelope

The declarative request an agent submits to its broker: `intent`, `operations`, and `trigger_source` (which of `chat`, `undo`, `watch`, `alert`, `cron`, `delegation`, `escalation` put the agent in motion). It carries no field for the agent's own tier, scope, risk class, or approval.

## Runtime and framework

### Hermes

The agent runtime the Platform Agent runs on ([nousresearch/hermes-agent](https://github.com/NousResearch/hermes-agent)). Handles chat ingress, tool-calling loop, MCP server lifecycle, and plugin execution.

### MCP (Model Context Protocol)

Anthropic's protocol for exposing tools and data sources to LLMs. `kube-agents` runs its MCP servers in-pod — `platform_control`, `agent_common`, and `developer_knowledge` — and reaches remote HTTP MCP endpoints through the in-pod `mcp_http_bridge.py` stdio bridge.

### LiteLLM

Multi-provider LLM proxy exposing an OpenAI-compatible Completions API. Default inference gateway in `kube-agents`.

### vLLM

Open-source inference server for local model serving. Alternative to LiteLLM when models need to run in-cluster on GPUs.

### Minty (GitHub Token Minter)

In-cluster broker that mints short-lived GitHub App installation tokens via GCP KMS. Lets the write-behind IaC mirror, the rendered provisioning bundles, and `github-issue-resolver` reach GitHub without a long-lived credential. Unrelated to the Action Broker, and not on the change path.

## Related Kubernetes-native agent projects

### Agent Substrate

Open-source Kubernetes-native platform for orchestrating AI agent workloads. Introduces Workers (managed compute pools) and Actors (agent instances) for multiplexed, stateful execution.

Source: [agent-substrate/substrate](https://github.com/agent-substrate/substrate).

### Agent Sandbox

Kubernetes SIG Apps project for isolated, stateful, singleton agent workloads. Provides warm pod pools, stable identity, and sandboxed execution (gVisor / Kata).

Source: [kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox).

### Agent Executor (AX)

Distributed agent runtime from Google with durable-execution features — pause, resume, snapshot, replay — to survive infrastructure failures.

Source: [google/ax](https://github.com/google/ax).
