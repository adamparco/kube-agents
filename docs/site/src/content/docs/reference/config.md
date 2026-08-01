---
title: Config reference
description: The agent's runtime Hermes config — what the operator renders, and what the baked file is for.
sidebar:
  order: 1
---

The agent's runtime wiring tells Hermes which MCP servers to start, which toolsets to expose to which
surfaces, and which plugins to load.

:::note[Two files, one of them authoritative]
[`agents/platform/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/config.yaml)
is **baked into the agent image as a fallback**. At runtime the operator renders its own config into a
`ConfigMap` and mounts it over that path, so the rendered ConfigMap is what the pod actually reads.
The two are kept deliberately consistent, but when they disagree the render wins — and the render is
the only one that knows the cluster's namespace, model endpoint, and enabled chat platforms.

To see what a running agent is using, read the ConfigMap, not the image:

```bash
kubectl get configmap -n kubeagents-system -l kube-agents/tier=platform \
  -o jsonpath='{.items[*].data.config\.yaml}'
```

:::

## What the operator renders

```yaml
mcp_servers:
  platform_control:
    command: /opt/hermes/.venv/bin/python3
    args:
      - /opt/data/scripts/platform_mcp_server.py
    connect_timeout: 120
    # 5-minute timeout to support long reasoning chains
    timeout: 300
    env:
      KUBERNETES_SERVICE_HOST: ${KUBERNETES_SERVICE_HOST}
      KUBERNETES_SERVICE_PORT: ${KUBERNETES_SERVICE_PORT}
      HERMES_HOME: ${HERMES_HOME}
      GOOGLE_CHAT_PROJECT_ID: ${GOOGLE_CHAT_PROJECT_ID}
      GOOGLE_CHAT_SUBSCRIPTION_NAME: ${GOOGLE_CHAT_SUBSCRIPTION_NAME}
      API_SERVER_KEY: ${API_SERVER_KEY}
  agent_common:
    command: /opt/hermes/.venv/bin/python3
    args:
      - /opt/data/scripts/agent_common_server.py
  developer_knowledge:
    command: /opt/hermes/.venv/bin/python3
    args:
      - /opt/data/scripts/mcp_http_bridge.py
      - https://developerknowledge.googleapis.com/mcp
    connect_timeout: 30
    timeout: 120

platform_toolsets:
  cli:
    - hermes-cli
    - mcp-agent_common
    - mcp-platform_control
    - mcp-developer_knowledge
  api_server:
    - hermes-api-server
    - mcp-agent_common
    - mcp-platform_control
    - mcp-developer_knowledge

model:
  provider: custom
  base_url: http://litellm.kubeagents-system.svc.cluster.local/v1
  default: model-default

memory:
  memory_enabled: false
  user_profile_enabled: false
  provider: multiuser_memory

plugins:
  enabled:
    - hermes_otel
    - session_store
    - session_otel_bridge
    - tool_call_audit
    - incident_context
```

## Sections

### `mcp_servers`

Every MCP server the agent runs is an **in-pod stdio process**. There is no remote MCP server in the
config, by design.

- **`platform_control`** — In-pod Python MCP server
  ([`platform_mcp_server.py`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/scripts/platform_mcp_server.py)).
  Chat message routing, session state, agent-internal ops, and the `plan_action` / `submit_action`
  tools the `apply-change` skill uses to reach the Action Broker. Env vars are injected from the
  pod's environment.
- **`agent_common`** — Shared utilities available to every tier (`agent_common_server.py`).
- **`developer_knowledge`** — Google's remote documentation MCP endpoint, reached through
  `mcp_http_bridge.py`: a small stdio-to-HTTP bridge that runs in the pod. It is read-only
  documentation lookup, and it is the only thing in the config that leaves the cluster.

`connect_timeout: 120` on `platform_control` allows for cold-start latency; `timeout: 300`
accommodates long reasoning chains.

:::caution[No cluster-mutating MCP]
Earlier drafts proxied a remote GKE MCP endpoint that could create and modify clusters. It is
deliberately gone, and nothing replaced it in this config: the identity on the agent pod holds no
write verb on anything. The agent still acts — mutation leaves the pod as an **Action Envelope**
submitted through `platform_control` to the tier's **Action Broker**, a separate workload holding the
only write identity in the scope. See [Declarative workflow](/kube-agents/concepts/declarative-workflow/)
and [Read-only by construction](/kube-agents/concepts/platform-agent/).
:::

### `platform_toolsets`

Toolsets group MCP servers into named bundles for different Hermes surfaces:

- **`cli`** — Exposed to the Hermes CLI (interactive terminal usage inside the pod).
- **`api_server`** — Exposed to the Hermes REST API (chat integrations, external callers).

Both expose the same three MCP servers, differing only in their Hermes-native tool
(`hermes-cli` / `hermes-api-server`).

### `model`

Rendered by the operator, not baked: `base_url` points at the in-cluster LiteLLM Service
(`litellm`, port 80 → container port 4000) in the agent's own namespace. See
[Inference gateway](/kube-agents/concepts/inference-gateway/).

### `memory`

Explicitly disabled — the agent doesn't retain per-user memory across sessions. Every conversation
starts fresh. The `multiuser_memory` provider name is set for future use.

### `plugins`

Hermes plugins enabled:

- **`hermes_otel`** — OpenTelemetry export.
- **`session_store`** — durable session state (writes to the pod's persistent volume if configured).
- **`session_otel_bridge`** — enriches OTel spans with session context (see [Session metadata](/kube-agents/concepts/observability/#session-metadata-plumbing)).
- **`tool_call_audit`** — writes per-tool-call records for audit and debug.
- **`incident_context`** — attaches incident state to the conversation.

## Related files

- [`agents/platform/SOUL.md`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/SOUL.md) — persona / system prompt.
- [`agents/platform/AGENTS.md`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/AGENTS.md) — workspace runtime instructions.
- [`agents/platform/cron/jobs.json`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/cron/jobs.json) — cron watchdog definitions. See [Cron jobs reference](/kube-agents/reference/cron-jobs/).
