---
title: Platform Agent
description: The persona, safety rails, and tool wiring that make the Platform Agent behave like a Platform Custodian rather than a chatbot.
sidebar:
  order: 1
---

The Platform Agent is a single autonomous agent with a defined role — **Fleet Operator** for exactly one GCP project. It's not a general-purpose Kubernetes assistant. The rules of its behavior are codified in [`SOUL.md`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/SOUL.md), which the Hermes runtime loads as the system prompt.

It is the **root** of the agent hierarchy: it has no parent, and every Cluster Admin Agent in the project is a direct child.

## Core truths (from `SOUL.md §1`)

- **Act, then report.** In scope, reversible, below the gate threshold: the agent does it. No pre-announcement and no proposal. An answer that ends in a recommendation, a ticket, or a pull request for work the agent was already allowed to do is treated as a defect.
- **It holds no write credential — which is why it can be decisive.** Every mutation goes to the agent's **Action Broker**, a separate process beside it holding the only write identity in the scope. The agent submits an Action Envelope with `apply-change` and cannot skip a step of the broker's pipeline.
- **It does not set its own risk class.** The classifier reads the target objects and the diff, not the agent's confidence.
- **Scope is enforced, not remembered.** Fleet-wide read across its one project; write nowhere else. Cluster internals and namespace workloads belong to the tiers below, and are not in its write surface.
- **Coordination is direct.** Work inside a cluster goes to that cluster's agent with `delegate` — one hop, seconds, callee re-authorizes. Not a note left somewhere for someone to pick up.
- **Refusals are information.** When the broker returns `forbidden`, the agent states the refusal and the rule behind it. Re-submitting the same intent in a different shape is a security event, not persistence.
- **The brake belongs to humans.** `pause`, `freeze`, `undo`, and the `contested` marker exist for people to use on the agent. It never operates them and never approves its own gated actions.
- **Autonomous recovery.** `SOUL.md §8` is a bounded ladder — retry, one alternative, roll back, escalate, page a human — with no rung skipped silently.
- **Proactive stance.** The agent doesn't wait to be asked. It surfaces and then _fixes_ drift, version skew, security baseline violations, and policy gaps within its own scope, and delegates the rest.

## Runtime wiring

The persona runs inside the Platform Agent Deployment on top of the [Hermes runtime](https://github.com/NousResearch/hermes-agent) (`nousresearch/hermes-agent`). The wiring lives in [`agents/platform/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/config.yaml).

### MCP servers

| Server                | Where                                                                          | Purpose                                             |
| --------------------- | ------------------------------------------------------------------------------ | --------------------------------------------------- |
| `platform_control`    | In-pod, `agents/platform/scripts/platform_mcp_server.py`                       | Chat message handling, session, agent-internal ops. |
| `agent_common`        | In-pod, `agents/platform/scripts/agent_common_server.py`                       | Utilities shared by every tier.                     |
| `developer_knowledge` | In-pod bridge (`mcp_http_bridge.py`) → `developerknowledge.googleapis.com/mcp` | Read-only documentation lookup.                     |

Every server is an in-pod stdio process. `developer_knowledge` is the only one that talks to anything outside the cluster, and it reaches it through `mcp_http_bridge.py` — a small stdio-to-HTTP bridge that runs in the pod, with no browser and no interactive OAuth.

There is deliberately **no cluster-mutating MCP server**. An earlier draft proxied a remote GKE MCP endpoint that could create and modify clusters; it was removed, because a tool that can mutate a cluster directly is a tool that bypasses classification, gating, and the journal. Cluster and cloud state is read through the Kubernetes API with viewer-only credentials, and changed only by submitting an Action Envelope to the Action Broker — `plan_action` and `submit_action` on `platform_control`. The broker holds the write identity; the pod does not.

### Toolsets

`config.yaml` groups the servers into toolsets:

- `cli` — used by the Hermes CLI (interactive terminal usage).
- `api_server` — used by the Hermes REST API (Chat, external callers).

Both include `hermes-cli`/`hermes-api-server` plus `mcp-agent_common`, `mcp-platform_control`, and `mcp-developer_knowledge`.

### Plugins

- `hermes_otel` — OpenTelemetry export to the GKE Managed OTel collector.
- `session_store` — durable session state.
- `session_otel_bridge` — annotates spans with session context.
- `tool_call_audit` — writes tool-call telemetry for audit and debug.

## Behavioral shape

- **Systematic root-cause analysis.** `SOUL.md §7` requires the agent to trace symptom → mechanism → config/demand before it will call an investigation done. Surface status strings like "CrashLoopBackOff" are the _start_ of an investigation, not the answer.
- **Grounding sources on every report.** Before finalising a diagnosis, the agent must extract verbatim tool output (specification blocks, event strings, termination traces) and cite them.
- **Human-readable reports.** Raw JSON, tool schemas, and CLI exit codes never appear in the agent's user-facing messages. Console links use the templates in `SOUL.md §6`.

## Where to go next

- [ChatOps](/kube-agents/concepts/chatops/) — how humans reach the agent (and how it reaches back).
- [Skills](/kube-agents/concepts/skills/) — the loadable capability bundles.
- [Autonomous watchdogs](/kube-agents/concepts/autonomous-watchdogs/) — the cron surface that makes it proactive.
- [Declarative workflow](/kube-agents/concepts/declarative-workflow/) — the Action Envelope every mutation travels in, and where Git still fits.
