---
name: kube-agents
description: >-
  Connect to, control, and collaborate with remote kube-agents deployments over the
  v2/next A2A JetStream message bus (a2a-jetstream/0.4). Use when discovering or switching
  kube-agents clusters, sending prompts to the PlatformAgent or persistent /session workers,
  delegating multi-step investigations, inspecting cluster blackboard topics (TOPICS-STATE,
  TOPICS-JOURNAL), viewing kube-agents GitHub Pull Requests and diffs natively in
  Jetski, or handling bi-directional push notifications from kube-agents.
---

# kube-agents A2A v2/next Skill for Jetski

Use the `kube-agents` MCP server tools (`mcp_kube-agents_*` / `call_mcp_tool` on server `kube-agents`) to interact natively with any `kube-agents` deployment running the `v2`/`next` A2A JetStream message bus (`a2a-jetstream/0.4`).

---

## 1. Discovery, Switching & Persistent Connections

- **`discover_deployments`**: Scans kubeconfig contexts in parallel for `PlatformAgent` CRs (`kubeagents.dev`), NATS services (`<pa>-a2a-nats`), and credential secrets (`<pa>-a2a-nats-creds`). Found deployments and credentials are saved to `~/.config/kube-agents/mcp-config.json`.
- **`connect_deployment`**: Connects to a saved deployment by `name`, project ID, cluster name, or kubeconfig context, automatically starting a supervised `kubectl port-forward` tunnel if `direct_nats_url` is unset, verifying JetStream streams (`TASKS`, `DIRECTORY`, `TOPICS-STATE`, `TOPICS-JOURNAL`), bootstrapping historical state, and starting the background push listener.
- **`connection_status`**: Displays live bus health, stream message counts, durable consumer liveness, registered `DIRECTORY` agent cards (`platform`, `session-*`), recent folded tasks, and active conversation state.

---

## 2. Sending Prompts, `/session` Workers & Delegation

Use **`send_message`** to execute turns on the remote `kube-agents` bus and stream back structured results:

- **`mode: "auto"` (Default)**: Sends to the active persistent `/session` worker if one is bound to the conversation, or directly to `platform`.
- **`mode: "platform"`**: Dispatches directly to the `platform` agent (`a2a.cmd.platform.<task>`).
- **`mode: "session"`**: Automatically spawns a dedicated interactive session worker pod (`session-<tok>`) via `/session` on `chat.console.<tok>.in` if none is active, then routes the prompt through `chat.console.<tok>.in`.
- **`mode: "delegate"`**: Ensures a `/session` worker is active and prefixes the prompt with `delegate: ` so the session worker delegates heavy multi-step work to `platform`, tracks child task progress, and synthesizes the result via a `wake` turn.

### Clean, Native UX (Collapsed Thinking by Default & Optional `debug` Mode)
Every `send_message` call folds the remote turn into a clean, crisp native Jetski response:
1. **Collapsed Thinking by Default (`lib.ArtifactThinking`)**:
   - Thinking is formatted inside a **collapsed** `<details>\n<summary>Thought for Xs</summary>\n\n- ...\n\n</details>` block (never `<details open>`).
   - Always preserve this collapsed `<details><summary>Thought for Xs</summary>...</details>` block at the top of your reply so it looks native to Jetski (`▸ Thought for Xs`) and expands only if the user clicks it.
2. **Optional Debug Mode (`debug: false` by Default)**:
   - By default (`debug: false`), tool call tables (`🛠️ Tool Activity Stream`), timings/durations, task/context IDs, and MCP `notifications/progress` tool-card expansion are **hidden** so the UI remains super clean and crisp.
   - If the user asks to debug `kube-agents` itself (e.g., to see tool calls, durations, task IDs, or delegation flowcharts), pass `debug: true` on `send_message` / `task_control` or toggle persistent debug mode via `manage_session(action: "debug_on")` (and `action: "debug_off"` to turn it back off).
3. **`result` (`lib.ArtifactResult`)**: Structured `kube-agents.chat/v1` report (`headline`, `tone`, `rows`, `plan`, `fold`, `choices`, `links`).

---

## 3. Session Lifecycle & Live Task Control

- **`manage_session`**:
  - `action: "session_on"`: Spawn a persistent `/session` worker pod (`s-<id>`).
  - `action: "session_off"`: Gracefully terminate the `/session` worker (`/session off`).
  - `action: "new"` / `"switch"`: Create or switch to a named conversation alias.
  - `action: "list"` / `"transcript"`: List conversations or view the full conversation transcript.
- **`task_control`**:
  - `action: "steer"`: Inject a mid-flight steering instruction into a running task (`steer <message>`).
  - `action: "stop"`: Cancel the active task on the gateway or `a2a.ctrl.<agent>.<task>`.
  - `action: "get"` / `"list"` / `"status"`: Inspect full folded task state (`get` supports `wait_timeout_seconds` to wait and stream live `notifications/progress` frames until the task settles), list recent tasks on `TASKS`, or trigger a gateway `status` replay.
  - **Important**: Never spin in a rapid polling loop calling `task_control(get)` without `wait_timeout_seconds`. If a task is still running after `send_message`, pass `wait_timeout_seconds: 120` to `task_control(action: "get")` or rely on reactive `agentapi` push wakeups.

---

## 4. Blackboard Topics & Agent Directory

Use **`read_topics`** to inspect the cluster's shared knowledge base:
- `action: "list"`: List all topics in `TOPICS-STATE` and `TOPICS-JOURNAL` with publisher and summary.
- `action: "read"` (with `topic`, e.g., `"upgrade-readiness"`, `"blueprint"`, `"annotations"`): Read the full payload and structured JSON of a blackboard topic.
- `action: "cards"`: View registered `DIRECTORY` agent cards (`platform`, `s-*`).

---

## 5. Native Pull Request & Diff Viewing in Jetski

Whenever `kube-agents` opens or references a GitHub Pull Request (or when the user asks to inspect a PR):
1. Call **`inspect_pr`** with `pr` (`https://github.com/<owner>/<repo>/pull/<num>`, `<owner>/<repo>#<num>`, or `<num>`).
2. `inspect_pr` downloads the PR metadata and unified diff to `~/.cache/kube-agents/prs/<owner>-<repo>-pr-<num>/`, creating:
   - `pr-<num>.diff` (full unified diff)
   - `PR_SUMMARY.md`
   - `files/<sanitized-path>.patch` (individual per-file unified diffs)
3. Present clickable `file://` links to the `.patch` and `.diff` files so the user can open them directly in Jetski's native diff viewer, and create a `.url.json` URL artifact (`UserFacing: true`) in the conversation artifact directory for the GitHub PR URL.

---

## 6. Bi-Directional Push Notifications

`kube-agents` pushes autonomous cluster findings, background task completions, and `chat.notify.*` notifications to Jetski via three mechanisms:
1. **Reactive Wakeup (`agentapi send-message`)**: When `jetski_conversation_id` is bound, background events on `chat.notify.*`, `chat.console.*.out`, and `a2a.tasks.>` wake the Jetski conversation automatically.
2. **Piggybacked Event Drain**: Every MCP tool call automatically drains and appends any pending push events at the bottom of the tool response—no polling timer required.
3. **Hook Sync (`kube-agents-mcp --hook`)**: `SessionStart` and `PreInvocation` hooks bind the current Jetski `conversationId` and inject any queued push events before the model runs.
