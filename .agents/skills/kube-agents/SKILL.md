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

# kube-agents A2A v2/next Skill for Jetski (Pure Proxy Mode)

Use the `kube-agents` MCP server tools (`mcp_kube-agents_*` / `call_mcp_tool` on server `kube-agents`) to interact natively with any `kube-agents` deployment running the `v2`/`next` A2A JetStream message bus (`a2a-jetstream/0.4`).

> **CRITICAL — Dumb Proxy Rule (No `kubectl` or Local Shell Calls)**:
> - When using `kube-agents`, Jetski acts strictly as a proxy to send messages, stream progress/thinking, and format responses (including GitHub PR diffs).
> - **NEVER** run `kubectl`, `gcloud`, `sqlite3`, or other local shell commands via `run_command` unless the user **explicitly** asks you to run a local `kubectl` or shell command.
> - Route all questions, status checks, audit queries, and cluster operations through `mcp_kube-agents_*` tools.
> - Once `send_message` or `task_control(action: "get")` returns a final (non-`STREAMING_STEP_CONTINUE`) response, **do NOT** call `task_control(action: "get")` again on that completed task—format and output the response immediately.

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

### Live Step-by-Step Streaming & Clean, Native UX (Collapsed by Default, Optional `debug` Mode)
Every `send_message` and `task_control(action: "get")` call streams live steps into Jetski's UI while keeping all details hidden by default in expandable sections:
1. **Live Step-by-Step Progress (`STREAMING_STEP_CONTINUE`) & Waiting for Kanban Cards**:
   - While a remote task (or its delegated Kanban worker card `t_<id>`) is executing steps, `send_message` (and follow-up `task_control(action: "get")` calls) return 1 new step per poll via `_custom_tool_result` with a human-readable step `title` (e.g. `Listing GKE clusters in the fleet (5s)`) so each poll appends a single step item to Jetski's collapsible `Exploring` / `Explored ›` list.
   - Whenever `send_message` or `task_control(action: "get")` returns `STREAMING_STEP_CONTINUE: Task <id> is actively running...`, **immediately** call `task_control(action: "get", task_id: "<id>", toolSummary: "<current_step>")` **without outputting any intermediate assistant text**. This keeps all streaming steps grouped inside Jetski's collapsible `Exploring` / `Explored ›` section.
   - **NEVER Stop Early or Promise Async Push for In-Turn Work**: Never stop while a user-triggered action or Kanban card (`t_<id>`, such as `apply` opening a GitOps PR) is still `todo` or `in_progress`, and **NEVER** tell the user *"Bi-directional push is active — as soon as the PR is opened, the notification and diff will arrive here automatically."* Keep streaming via `STREAMING_STEP_CONTINUE` until the card finishes and returns the PR URL, then call `inspect_pr` in the same turn.
2. **Expandable Step Details Inside the Tool Collapsible (No Duplicate `<details>` Block in Final Chat Message)**:
   - Each step row in Jetski's collapsible tool group (`Executed <step> (Xs) ›`) expands on click to show the remote **Agent & Task ID**, **Thought / Reasoning**, and **Tool Activity** (tool name, argument summary, status, and duration).
   - Once the task settles, output the final structured answer directly **without** adding a duplicate `<details><summary>Thought for Xs</summary>...</details>` block in the chat message, so steps appear only once in the collapsible tool group above.
3. **Optional Debug Mode (`debug: false` by Default)**:
   - By default (`debug: false`), tool call tables (`🛠️ Tool Activity Stream`), timings/durations, and task/context IDs are **hidden** so the UI remains super clean and crisp.
   - If the user asks to debug `kube-agents` itself (e.g., to see tool calls, durations, task IDs, or delegation flowcharts), pass `debug: true` on `send_message` / `task_control` or toggle persistent debug mode via `manage_session(action: "debug_on")` (and `action: "debug_off"` to turn it back off).
4. **`result` (`lib.ArtifactResult`)**: Structured `kube-agents.chat/v1` report (`headline`, `tone`, `rows`, `plan`, `fold`, `choices`, `links`).

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
  - `action: "get"` / `"list"` / `"status"`: Inspect full folded task state (`get` automatically waits for the next streaming step when called on an active task, returning `STREAMING_STEP_CONTINUE` until the task settles), list recent tasks on `TASKS`, or trigger a gateway `status` replay.
  - **Important**: When `STREAMING_STEP_CONTINUE` is returned, immediately call `task_control(action: "get", task_id: "<id>")` with no intermediate prose until the task settles. Once it returns a final response without `STREAMING_STEP_CONTINUE`, do NOT call `task_control(action: "get")` again.

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
