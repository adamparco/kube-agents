# AGENTS.md - Your Workspace

This folder is home. Treat it that way.

## Session Startup

Use runtime-provided startup context first, including `AGENTS.md` and `SOUL.md`.
Do not manually reread startup files unless the user explicitly asks or the context is missing vital information.
A glossary of agentic terms lives at `/opt/defaults/docs/glossary.md` (or `docs/glossary.md` in the workspace). Read it **only** when you actually hit harness terminology you cannot ground — **Agent Substrate** and the like — or when the user asks about it. Every kanban card is a fresh session, so reading it unconditionally costs a model turn per card for a file most tasks never need.

## Memory

You wake up fresh each session. Maintain continuity through:

- **Daily notes:** `memory/YYYY-MM-DD.md` — records of agent provisions, cluster setup tasks, and policy audits.
- **Long-term:** `MEMORY.md` — long-term project memories (loaded only in direct main sessions with your human, never shared).

## Receiving Work

- The Chat Agent routes user requests to you. When invoked with **`work kanban task <id>`**, follow the Kanban worker protocol in `SOUL.md` §0: `kanban_show` to read the task, do the work, then ALWAYS `kanban_complete` (the full answer in `result`, a one-line status header in `summary`) or `kanban_block`. Never exit a kanban run without one of those.
- **A governance job arrives as a card.** Every governance job's live schedule sits in the Chat Agent profile's roster (`/opt/data/cron/jobs.json`), because that profile owns the only ticking gateway; your own roster carries the same jobs as disabled tombstones. Its scheduler files one card per due job, carrying that job's own prompt, and you are the worker. Do exactly what the card says, then `kanban_complete`. No person is waiting on a scheduled card: it carries no chat subscription, so your closing report posts nowhere — `result` is written for the board's record and for whoever comes looking when a schedule appears to have stopped.
- **"Run the `<x>` cron job now" → file its card, do not re-enact it.** `cronjob` is not the route. The image ships your profile a roster of disabled tombstones — one per relocated governance job, shipped `enabled: false` so the start-up merge switches off the enabled copies an upgraded volume still has — so `cronjob(action='list')` will show the governance jobs. Those are not the live schedule: `profile-cron-tick` would fire an enabled entry here, which is exactly why they are disabled, and their prompts are frozen at the release that shipped them. `cronjob(action='run')` executes a job synchronously in the session that calls it, which is the re-enactment this bullet exists to prevent. Instead, for **each** job the request names:

  1. `HERMES_HOME=/opt/data /opt/hermes/.venv/bin/python3 /opt/data/scripts/platform_cron_dispatch.py <job-id>` — this is the same code path the schedule uses, so the card gets that job's prompt verbatim and the "already running" guard still applies. It logs `filed <task-id> to run <job-id>` to stderr; if it instead reports a card still in flight, say so and stop — a second card would run the same audit against itself.
  2. `python3 /opt/data/scripts/kanban_notify_propagate.py --to <task-id>` — immediately, or that card completes silently (`SOUL.md` §0). The schedule's cards are meant to be silent; one a person asked for is not.

  Then complete your own card with one line per job: the job, the card id it was filed as, and nothing else. The report belongs to the card that does the work, and repeating it here sends the same content twice.

  **Never do the audit in the session that received the request.** Each card gets its own session and its own turn budget; several audits crammed into one turn share one budget between them. That is not hypothetical — on 2026-08-03 a single worker asked to run all five streams issued zero `kubectl` commands, hand-typed five empty findings documents, and published a fleet-wide all-clear.

## Delegation

- **Manage a cluster on request:** when a user asks to manage a specific existing cluster (e.g. "manage my cluster X in Y"), use the `manage-cluster` skill to create its Cluster Agent profile (`cluster_agent_profile.py create`).
- Single-cluster runtime debugging and workload operations are **not** done here. Delegate them to that cluster's **Cluster Agent** — a per-cluster Hermes profile you create and manage via the `cluster-agent-lifecycle` skill (`scripts/cluster_agent_profile.py`). Create it on cluster onboarding, and delete it on cluster teardown. Delegate tasks via the **kanban board**: `kanban_create(assignee="<profile-name>", ...)` (resolve the name with `cluster_agent_profile.py name`); the gateway dispatcher auto-spawns the Cluster Agent to work it and reports back on the card. Act on the returned RCA/patch (from the card `metadata`) via `submit-suggestion` (you own the GitOps write path).

## Cluster Credentials

To read a cluster other than the one you run on, pin a **per-target** kubeconfig and pass it through the environment. Resolve the project at runtime; never hardcode one (`SOUL.md` §1):

```bash
PROJECT="$GKE_PROJECT_ID"   # CLUSTER and LOCATION come from the request
export KUBECONFIG="$HERMES_HOME/.kubeconfigs/kubeconfig_${PROJECT}_${CLUSTER}_${LOCATION}.yaml"
gcloud container clusters get-credentials "$CLUSTER" --location="$LOCATION" --project="$PROJECT"
```

Two constraints make that exact path the one that works. It must live under `$HERMES_HOME`, because `gcloud` and `kubectl` here are credential-proxy shims and the sidecar rejects with a 400 any `KUBECONFIG` resolving outside the shared workspace — `/tmp` fails outright. And it must be one file per target, so concurrent reads of different clusters do not race on a single `current-context`. This mirrors `_thread_kubeconfig_path` in `scripts/platform_mcp_server.py`, which is the source of truth for the naming. There is no MCP tool that does this for you, and deliberately so: the internal helper it wraps returns the whole subprocess environment, which carries `API_SERVER_KEY`.

## Tool Notes

- **`search_files`: `pattern` is a regex, except when `target="files"`, where it is a glob.** Handing a glob to the default (content) mode fails with `rg: regex parse error: (?:*.yaml) … repetition operator missing expression`. Files by extension: `search_files(target="files", pattern="*.yaml")`. Content: `search_files(pattern="\.yaml")`.
- **In `target="files"` the glob matches the basename, not the path.** A pattern with no `/` and no leading `*` is rewritten to `*<pattern>`, so `pattern="cron"` matches only names _ending_ in `cron` and can never find anything inside a `cron/` directory — and `*cron*` fails for the same reason. Write the directory out: `pattern="**/cron/**"`.
- **`path` is not optional in practice.** Omitted, it searches the current working directory, which for a kanban card is that card's own workspace — almost always empty, so you get a confident `total_count: 0` for a file that exists. Always pass the directory you mean.

## Red Lines

- Don't run destructive commands on core infrastructure or cluster setups without asking.
- Never expose raw passwords or GCP/GKE keys.
- **Never point `KUBECONFIG` at a path under `/opt/data/profiles/cluster-*/`.** Those are the Cluster Agents' pinned identities, one cluster each. `get-credentials` writes to whatever `KUBECONFIG` names, so running it with one of those exported silently re-points that agent at the wrong cluster. Card `t_b9544b00` did exactly this and left the `adamparco-gitops` Cluster Agent holding credentials for a different cluster. Use the `$HERMES_HOME/.kubeconfigs/` path above.
