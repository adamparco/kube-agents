---
title: Autonomous watchdogs
description: Cron-scheduled jobs that make the Platform Agent proactive rather than reactive.
sidebar:
  order: 6
---

`agents/chat/defaults/cron/jobs.json` defines the scheduled jobs. Each one carries a pre-authored prompt that reaches the Platform Agent on a cron schedule. The prompts typically point at a [governance SOP](/kube-agents/concepts/governance-sops/); the agent reads the SOP, executes the procedure, and either publishes to your GitOps repo — a proposed PR via `submit-suggestion`, or an audit ledger issue via `fleet-audit` — or posts a proactive Chat alert.

Watchdog runs execute autonomously: the agent config sets `approvals.cron_mode: approve` (see `deploy/shared/defaults/config.yaml`), so commands that would otherwise require human approval run without prompting when triggered by a scheduled job.

Full JSON is annotated on [Reference → Cron jobs](/kube-agents/reference/cron-jobs/), which also covers the four non-governance jobs in the same file: the named-profile cron ticker described in [What fires the schedule](#what-fires-the-schedule), Cluster Agent reconciliation, and the two first-run onboarding steps.

## How a watchdog fires

The schedule lives in one profile and the work happens in another, and it is worth knowing why before reading the roster.

Cron ticking is a property of a running **gateway**, and gateways are per profile. Only the `default` (Chat Agent) profile has one — the Platform Agent is reached through the kanban dispatcher, which spawns a worker per card and exits — so the gateway's own ticker never advances a schedule sitting in the Platform Agent's roster. All seven shipped watchdogs moved to the Chat Agent's roster for exactly this reason. The Platform Agent's roster is not dead, though: [`profile-cron-tick`](#what-fires-the-schedule) runs every named profile's cron store once a minute, so anything an operator schedules there does fire, with the platform profile's own persona and toolsets and no card involved. That is precisely why the seven moved watchdogs ship as disabled tombstones rather than being deleted; see [The retired jobs](#the-retired-jobs).

The Chat Agent cannot run an audit itself, and is not asked to. Its toolsets are deliberately stripped to `mcp-router`, `kanban` and `memory` (`agents/chat/config.yaml`), with no `terminal` and no `skills`. So every governance job on its roster is marked `no_agent`: a plain subprocess that prompts no model, files one kanban card assigned to `platform`, and exits. The card carries the job's `prompt` — read off the roster entry at tick time, never restated — and the gateway dispatcher spawns a Platform Agent worker on it with the full platform toolset.

`agents/chat/scripts/platform_cron_dispatch.py` is the script behind every job, invoked through a one-line `dispatch_<id>.py` wrapper that supplies the job id (the scheduler runs a script with no arguments, so the wrapper is the only place the id can live). Its module docstring is the reference for the rest. Four behaviours are worth knowing here:

- **A tick can decline to file.** A card of the same title still in flight means the last run has outlasted its own schedule, and the tick skips rather than run the audit concurrently with itself. `blocked` does not count as in-flight — it is waiting on a person, and one bad run must not switch the audit off indefinitely. Five blocked cards for one job do stop it, and raise a watchdog alert: past that many, the job is not being held up by a run that would otherwise have worked, and filing on into a board nothing sweeps is how a wedged job spends a day spawning workers in silence.
- **The card completes silently.** Chat notifications come from a subscription row written at `kanban_create` time from the originating chat session, and a cron script has no session — so the card's completion posts nowhere. That suits an audit whose deliverable is its ledger issue. To get a per-card message, add the subscription (`agents/platform/scripts/kanban_notify_propagate.py`), not a change to the script.
- **The worker is a kanban worker, not a cron run.** It does not inherit the entry's `model`; it gets the platform profile's own settings, including `agent.max_turns: 250`. None of the shipped jobs pins a `model`. `skills` does carry over, by a different road — the dispatch script passes each name to `kanban create --skill`, which the gateway expands into the worker's `--skills`, so the skill is preloaded before the first turn exactly as the cron scheduler used to prepend it. See [`skills`](#job-shape) below.
- **Finished cards are swept.** `github-issue-resolver` alone files forty-eight a day, so a filing tick also archives this job's finished cards past the newest three. Archiving is not deletion (`kanban list --archived`; `kanban gc` reclaims the workspaces), blocked cards are never swept, and no other job's history is touched.

## The shipping jobs

The roster, with exact cron expressions, enabled state, and prompts, is generated from `jobs.json` on [Reference → Cron jobs](/kube-agents/reference/cron-jobs/). Seven jobs ship, all enabled and all dispatched as kanban cards from the Chat Agent's roster: the six fleet audits below and `github-issue-resolver`.

### The six fleet audits

Each audit reads its SOP, executes read-only checks against the fleet, writes a validated findings file, and hands it to the [`fleet-audit`](/kube-agents/skills/) skill's `audit_report.py` helper. The helper owns every git and `gh` operation and renders every body itself — the model never writes one.

| Job                           | SOP                                  | Audits                                                                    |
| ----------------------------- | ------------------------------------ | ------------------------------------------------------------------------- |
| `compliance-audit`            | `compliance_audit_sop.md`            | Security and RBAC posture across the fleet                                |
| `obtainability-audit`         | `obtainability_audit_sop.md`         | Workload reliability: requests, PDBs, HPAs, probes, scheduling rigidity   |
| `security-patch-orchestrator` | `security_patch_orchestrator_sop.md` | Version currency and upgrade-policy hygiene against the cluster's channel |
| `fleet-wide-cost-analysis`    | `fleet_wide_cost_analysis_sop.md`    | Observable waste, in resource units — no billing export required          |
| `fleet-consistency-drift`     | `fleet_consistency_drift_sop.md`     | Clusters diverging from a baseline derived from the fleet itself          |
| `ai-security-audit`           | `ai_security_audit_sop.md`           | AI inference and training workloads: exposure, model provenance, weights  |

Two properties matter more than the check lists:

- **One ledger issue per audit, plus fixes on demand.** The helper finds the audit's existing open issue by its `audit:<id>` label and rewrites it in place, commenting only on what changed since the last run. A daily audit therefore produces one issue that stays current, not thirty near-identical PRs a month. A finding whose fix is a manifest is promoted into its own narrow pull request — automatically when it is critical, otherwise when a repo writer asks for it on the ledger. See [Declarative workflow](/kube-agents/concepts/declarative-workflow/#the-fleet-audit-skill).
- **Silence is a real outcome, but it has to be earned.** A run with no findings, which resolved none either, closes the audit's ledger issue as completed and returns `[SILENT]`, so a steadily quiet fleet generates no Chat traffic. The helper decides this, not the agent: `finish` returns `silent_ok`, `true` only when nothing was new, nothing resolved, no coverage gap remained, and no remediation pull request opened or closed. Two clean runs still speak. A run that could not read part of the fleet is never silent, however clean the part it did read: it leaves the ledger open, names the gaps, and reports — "I found nothing" and "I could not look" must not arrive as the same silence. And a run that came back clean after carrying findings reports what closed, because a fleet that just got fixed is the one piece of good news these watchdogs produce.
- **Asking for a run cancels the silence.** `silent_ok` answers "would a channel want this?", and it cannot see that a person is waiting. So a job asked for on demand always reports its outcome and its ledger issue URL, whatever the flag says. The Platform Agent does not re-enact the audit in the session that took the request: it runs `platform_cron_dispatch.py <job-id>` — the same code path the schedule uses, so the card gets the same prompt and the same in-flight guard — and copies its own chat subscription onto the new card so the report reaches the person who asked.

### The retired jobs

Five watchdogs — `blueprint-sync`, `policy-propagation`, `global-capacity-orchestrator`, `standardization-validator`, and `lifecycle-deprecation-manager` — shipped disabled for several releases and are no longer in the roster. As written none could produce a finding on a stock install: two compared clusters against a "master blueprint" document no install provides, one read policy templates from an unshipped `/opt/defaults/templates/`, one ran hourly with no defined output artifact, and one overlapped `security-patch-orchestrator`.

Their SOPs are retained under `agents/platform/governance/`, so reviving one is a matter of rewriting the SOP against something a stock install actually has and re-adding the job — see [Adding a watchdog](#adding-a-watchdog). Re-adding the entry alone will not help; the SOP is why they were retired.

On a cluster provisioned before they were dropped, the five entries remain on the Platform Agent profile's `profiles/platform/cron/jobs.json` in the disabled state that release left them in: `merge_cron_store` adds and overwrites but never prunes, so an id deleted from the shipped roster stays on the volume. They stay off; the image simply no longer has a say.

The seven live watchdogs did not leave the same file when they moved to the Chat Agent's roster — they stay behind as tombstones, full entries shipped `enabled: false`. Upstream could simply delete them, because nothing there ticks the Platform Agent's store; here [`profile-cron-tick`](#what-fires-the-schedule) does, and `merge_cron_store` never prunes, so on an upgraded cluster the old enabled copies would have fired in duplicate with the dispatch cards. Shipping the entries disabled is the same retirement path the five above took: the start-up merge flips `enabled` off on every volume, and the ids can be deleted only once every live cluster has merged that disabled form. `cronjob(action='list')` run against the Platform Agent will list all seven, disabled — the roster that decides when they run is the Chat Agent's, and only that one.

## What fires the schedule

Hermes' cron ticker is a thread inside `hermes gateway run`, and everything it touches — the job store, the execution ledger, the tick lock — resolves from that process's `HERMES_HOME`. It never enumerates profiles. This image runs a single gateway, homed at `/opt/data`, so the only roster that thread ticks is the Chat Agent's. The Platform Agent's lives at `/opt/data/profiles/platform/cron/jobs.json`, which the thread never opens.

`profile-cron-tick` is what ticks it. It is a `no_agent` script job on the Chat Agent's roster — the one store that does tick — and each minute it runs `hermes cron tick` as a subprocess against every named profile with work due. The seven shipped watchdogs never travel this path — their schedules live on the Chat Agent's roster and become kanban cards, as [How a watchdog fires](#how-a-watchdog-fires) describes — but any job an operator schedules on a named profile's own store fires through it:

```text
gateway ticker              →  profile-cron-tick  →  hermes cron tick
(HERMES_HOME=/opt/data)        (every 1m)            (HERMES_HOME=<profile>)
```

A watchdog therefore fires through the same execute → deliver → record path a manual `hermes cron tick` takes, with its own profile's persona, toolsets, and `max_turns`. Three consequences worth knowing:

- **A minute is the floor, not the guarantee.** A named profile's schedule is only ever inspected as often as the dispatcher runs, so a cadence finer than `* * * * *` cannot be honoured there. The dispatcher is itself scheduled as `* * * * *` rather than as a one-minute `interval`, and deliberately: Hermes re-anchors an `interval` job to the moment the last run _finished_, while the gateway ticker sleeps a fixed sixty seconds _after_ each tick returns — so the next due time always lands just past the next wake and a one-minute interval job quietly fires every two. A cron expression is immune, because the completion time is snapped up to the next wall-clock minute. What is left is narrower: a dispatch that actually ran a watchdog blocks for up to 45 seconds waiting on the subprocess (see `DEFAULT_BUDGET_SECONDS` in `profile_cron_tick.py`), and that usually costs the minute after it, so roughly one dispatch in twenty is collapsed and it is almost always the minute following a watchdog run. Quiet minutes are on time. Treat every schedule below as accurate to about a minute, which is well inside the sweet spot in [Adding a watchdog](#adding-a-watchdog) and does not constrain the shipping roster. Nothing here is latency-critical; if something ever is, it needs its own timer, not a finer cron expression.
- **Overlap and backlog are `tick()`'s problem, not the dispatcher's.** A tick takes an exclusive lock on the profile's store (`cron/.tick.lock`) while it decides what is due and advances every due job's `next_run_at`, then releases it once every due job has been dispatched rather than holding it until they finish — so an agent down for two days runs each missed daily audit once on return, not once per missed day. Overlap is held per job rather than per profile: a job takes `cron/.job-<id>.lock` for as long as it runs, so a second dispatch will not start the same job twice, but it will start a _different_ one. That distinction matters here because each platform tick is a separate `hermes cron tick` process. Holding the profile lock across execution — the upstream default — meant a fleet audit blocked every dispatch for its whole run; three `github-issue-resolver` firings were measured 418s, 179s and 1142s late behind one, each recovering within seconds of the audit finishing.
- **A broken ticker is loud.** Dispatches are silent on a quiet minute, but a tick that fails is recorded as a failed `profile-cron-tick` run with the subprocess output in `<profile-home>/cron/tick.log` — the one failure mode this job must not have is the silence it exists to end. Individual watchdog runs stay where every other run is: the profile's own execution ledger, and `cronjob(action='history')`.

## Job shape

Each job in `jobs.json` follows this schema:

```json
{
  "id": "compliance-audit",
  "name": "Security & RBAC Posture Audit",
  "schedule": {
    "kind": "cron",
    "expr": "20 6 * * *",
    "display": "20 6 * * *"
  },
  "prompt": "Run the daily fleet security and RBAC posture audit. Read the SOP at 'governance/compliance_audit_sop.md' in your profile home — all 406 lines of it, before you run anything. Its eleven checks are section 2, lines 102-314, so a read that stops early skips almost the entire audit and reports a clean fleet it never looked at. Then execute it exactly, using the fleet-audit skill to open and close the audit run.",
  "skills": ["fleet-audit"],
  "no_agent": true,
  "script": "dispatch_compliance_audit.py",
  "enabled": true,
  "deliver": "all"
}
```

- **`id`** — stable identifier, referenced in observability and disable/enable ops. It outlives renames: `obtainability-audit` is now the Workload Reliability Audit, but the id stays put.
- **`schedule.expr`** — standard 5-field cron in the pod's local time zone (UTC unless the pod's TZ is overridden).
- **`prompt`** — the body of the kanban card the tick files, copied verbatim. Governance jobs point at an SOP **relative to the profile home** (`governance/<sop>.md`), which is where `profile_scaffold.py` overlays the baked `/opt/platform-template/governance/` directory. An absolute `/opt/defaults/governance/...` path does not resolve — nothing is mounted there. The six audit prompts also state how long their SOP is and which section holds the checks, because a read that stops early lands in the preamble and the run reports a clean fleet it never inspected; a test in `audit_report.py`'s suite re-derives both numbers from the file so a stale citation fails there rather than at 06:20. What the prompts deliberately do **not** restate is the `[SILENT]` rule — each SOP's closing section states it in full, qualifiers included, and a shorter version in the prompt would both lose the qualifiers and tell the run what its answer looks like before it decides what to check.
- **`skills`** — the skills the work needs. A `no_agent` tick prompts no model, so the scheduler ignores the field; the dispatch script reads it instead and passes each name to `kanban create` as `--skill`, which the gateway expands to `--skills` when it spawns the worker, preloading the skill's text before the worker's first turn. That is the same force-load the cron scheduler performed by prepending skill content to the prompt — naming the skill in the card body alone would have left loading it to the worker's discretion. The body names them too, as the board's record of what the job expected. The six audits use `fleet-audit`; `github-issue-resolver` uses its namesake skill.
- **`no_agent`** and **`script`** — the tick is a subprocess, not an LLM turn. `script` names a `dispatch_<id>.py` wrapper in `agents/chat/scripts/`, which supplies the job id to `platform_cron_dispatch.py`.
- **`enabled`** — set to `false` to disable a job without deleting its entry.
- **`deliver`** — where a tick's stdout goes. A successful tick prints nothing and is delivered as a silent run, so this only matters on failure: `"all"` sends the watchdog alert to the configured target, while `"local"` resolves to no target at all and would drop it. All seven governance jobs use `"all"`, so a bridge that stops filing cards is visible rather than indistinguishable from a quiet fleet.

## Disabling a watchdog

Flip `enabled` to `false` in `agents/chat/defaults/cron/jobs.json`. The scheduler honours the flag directly — it stops ticking the job, so nothing is filed and nothing runs — and `platform_cron_dispatch.py` honours it a second time, so a hand-run of the wrapper cannot resurrect a retired audit either.

Flip the flag; do not delete the entry. An id can be dropped from the roster only once no live cluster still needs the image to hold the job off, which is the path the five [retired watchdogs](#the-retired-jobs) took.

**On an existing cluster the edit does not travel by itself.** The Chat Agent is the `default` profile, which is not scaffolded: it lives at `$HERMES_HOME` directly and the entrypoint seeds it with `cp -ru /opt/defaults/. "$TARGET_DIR/"` (`deploy/shared/docker-entrypoint.sh`, step 2). `cron/` is in neither force-sync list — step 2a covers `SOUL.md`, `AGENTS.md` and `CAPABILITIES.md`, step 2b covers `scripts/` — and since the scheduler writes `last_run_at` into the volume's copy on every tick, that copy's timestamp is permanently ahead of the image's, and `cp -u` skips it for good. Step 2c does merge the image's roster over the volume's, but only for the ids it explicitly allowlists: `profile-cron-tick` and the seven `dispatch_*.py` governance entries. Those eight do travel — redeploy one with `enabled: false` and the next pod restart flips it off on an existing cluster. The allowlist is a named list rather than the whole shipped roster because the two onboarding jobs delete themselves once the first-run report is delivered, and an unfiltered merge would put both back on every restart. For an entry outside it — an operator's own job, or a new one whose id nobody added to step 2c — the shipped roster reaches a **fresh** volume and no other: on a running cluster you can redeploy that watchdog with `enabled: false` and watch the job keep firing.

Two consequences worth stating plainly, because they cut in opposite directions:

- The same gap protects a job an operator added themselves. Nothing the image ships will overwrite or prune the live roster, so a hand-added entry survives every upgrade.
- It also means that for an id outside the allowlist, the live roster is the only thing that decides what runs. To switch such a watchdog off on a cluster that is already up, edit `enabled` in `$HERMES_HOME/cron/jobs.json` on the agent's PVC and restart the gateway. The `cronjob` tool is not a route to it: it is denied to the Chat Agent (`agents/chat/config.yaml`), and the Platform Agent's copy addresses that profile's own store, not this one.

The Platform Agent's profile behaves differently — `profile_scaffold.py`'s `merge_cron_store` genuinely merges image config over live state there, unfiltered. That is why the [retired watchdogs](#the-retired-jobs) and the seven tombstones stay disabled on it: the merge flips `enabled` off again on every pod restart, so a stale enabled copy on an upgraded volume cannot outlive the release that retired it.

## Adding a watchdog

1. Write a governance SOP in `agents/platform/governance/<your-sop>.md`.
2. Add a job entry to `agents/chat/defaults/cron/jobs.json` pointing at it as `governance/<your-sop>.md` — that is the Chat Agent's roster, the one the gateway ticks, and the dispatch route gives the run a kanban card, the in-flight guard, and a board history. The alternative is an entry in `agents/platform/cron/jobs.json`, fired directly by [`profile-cron-tick`](#what-fires-the-schedule) with no wrapper and no card — but remember that a tick holds the profile's store lock for the length of the run, and do not use the same id on both rosters, or the job will run twice. Every shipped watchdog takes the dispatch route.
3. For the dispatch route, copy one of the `dispatch_*.py` wrappers in `agents/chat/scripts/`, changing only the job id, and point the entry's `script` at it. `test_platform_cron_dispatch.py` fails if a job has no wrapper, or a wrapper no job.
4. If the job files findings, add its id to the allowlist in `agents/platform/skills/fleet-audit/scripts/audit_report.py` and set `"skills": ["fleet-audit"]`.
5. Run `make docs-generate` — the reference table is generated from the Chat Agent's roster (a platform-roster job would have to be documented in prose instead), and a cron expression missing from `CRON_CADENCE` in `scripts/generate_docs.py` renders its cadence as `—`.
6. Add the new id to step 2c's `--cron-jobs` allowlist in `deploy/shared/docker-entrypoint.sh`, unless the job removes itself at runtime the way the onboarding pair does. Without it the entry reaches fresh volumes only; with it, the start-up merge delivers it to every existing cluster — see [Disabling a watchdog](#disabling-a-watchdog) for why the roster otherwise never travels.
7. Redeploy (`provision_08_deploy_platform_agent.sh`, or `dev/dev_rebuild_agent.sh` for a dev workspace). The entry lands on the next pod restart; on a cluster whose id you deliberately left off the allowlist, add it to `$HERMES_HOME/cron/jobs.json` on the PVC by hand as well.

Keep the schedule realistic — LLM inference on every tick has cost. Hourly or daily is the sweet spot for most SOPs; sub-15-minute cadences should have a clear justification. Stagger start minutes so two audits never contend for the same session.

Budget the run as well as the schedule. Every job shares one per-turn tool-calling budget, `agent.max_turns` in the profile's `config.yaml` — 250 for the Platform Agent, against a Hermes default of 90 the fleet audits outgrew. A run that exhausts it is stopped mid-flight and recorded as a `timed_out` event, which reads misleadingly: no clock expired, the agent simply took more steps than it was allotted, and raising any of the `HERMES_*_TIMEOUT` values will not help. The six shipping audits finish well inside 250, but an SOP that gains checks and a fleet that gains clusters both spend against it. There is no per-job override on either route: a dispatched job's work happens in a kanban worker, which reads the profile's `config.yaml` and knows nothing about the roster entry that filed its card, and the cron scheduler behind a platform-roster entry honours a per-job `model` but not a per-job turn budget — so the profile-wide value is the only lever.

## Where to go next

- [Reference → Cron jobs](/kube-agents/reference/cron-jobs/) — full annotated `jobs.json`.
- [Governance SOPs](/kube-agents/concepts/governance-sops/) — the playbooks these watchdogs execute.
- [Declarative workflow](/kube-agents/concepts/declarative-workflow/) — how findings become a ledger issue and remediation PRs.
