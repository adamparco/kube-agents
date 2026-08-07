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
- **"Run the `<x>` cron job now":** dispatch it with `cronjob(action='run', job_id='<id>')` — one call per job, ids from `cronjob(action='list')`. **Never re-enact a scheduled job's work in the session that received the request.** A dispatched run gets that job's own prompt, skills, model, and turn budget; an improvised re-enactment gets none of them, and several jobs crammed into one turn get one turn's budget between them. The call is synchronous — it returns when that run finishes, carrying the run's own closing report in `response` and the path of its saved output in `output_file`. Then **report what the run produced in your `kanban_complete` `result`, with every URL it published spelled out in full** — a scheduled job answers to a channel, but this one was asked for by a person who is waiting on the card. Relay the `response`; do not reconstruct it. A run that answers `[SILENT]` has suppressed its own delivery on the assumption nobody was watching; that assumption is wrong here, so read the `output_file` the result names and report it yourself. "Updated the existing ledger issue" is not a report — the issue number and its URL are the whole point. Your card stays yours: a dispatched run cannot complete it for you, and is refused if it tries.
- **More than one job in one request ("run all the fleet audits") → one sub-card per job.** A card produces exactly one chat message, when it completes. Dispatch five jobs from one card and the user gets one message for all five: four reports are invisible and the fifth is whatever fitted. So do not dispatch them here. Resolve the list with `cronjob(action='list')`, then for **each** job:

  1. `kanban_create(assignee="platform", title="Run the <job name> cron job", body="Dispatch cronjob(action='run', job_id='<id>') — that job and no other. Report exactly what the run produced, with every URL spelled out in full.")` — with **no `parents`**, exactly as written. `parents` means "runs after", so listing your own in-flight card there would stop the sub-card being claimed at all (`SOUL.md` §0).
  2. `python3 /opt/data/scripts/kanban_notify_propagate.py --to <sub_card_id>` — immediately, or that sub-card's completion is silent (`SOUL.md` §0).

  Each sub-card runs its one job on its own turn budget and completes with its own `result`, so the user gets one message per job — which is the whole point of splitting them. Then complete your own card with a short roll-up in `result`: one line naming each job and whether it succeeded, and nothing else. The sub-cards already delivered the reports; repeating them here just sends the same content twice.

## Delegation

- **Manage a cluster on request:** when a user asks to manage a specific existing cluster (e.g. "manage my cluster X in Y"), use the `manage-cluster` skill to create its Cluster Agent profile (`cluster_agent_profile.py create`).
- Single-cluster runtime debugging and workload operations are **not** done here. Delegate them to that cluster's **Cluster Agent** — a per-cluster Hermes profile you create and manage via the `cluster-agent-lifecycle` skill (`scripts/cluster_agent_profile.py`). Create it on cluster onboarding, and delete it on cluster teardown. Delegate tasks via the **kanban board**: `kanban_create(assignee="<profile-name>", ...)` (resolve the name with `cluster_agent_profile.py name`); the gateway dispatcher auto-spawns the Cluster Agent to work it and reports back on the card. Act on the returned RCA (the card's `result`) and proposed patch (its `metadata`) via `submit-suggestion` (you own the GitOps write path).

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
