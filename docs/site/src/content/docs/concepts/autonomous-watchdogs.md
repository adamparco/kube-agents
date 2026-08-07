---
title: Autonomous watchdogs
description: Cron-scheduled jobs that make the Platform Agent proactive rather than reactive.
sidebar:
  order: 6
---

`agents/platform/cron/jobs.json` defines the scheduled jobs. Each one fires a pre-authored prompt at the Platform Agent on a cron schedule. The prompts typically point at a [governance SOP](/kube-agents/concepts/governance-sops/); the agent reads the SOP, executes the procedure, and either publishes to your GitOps repo — a proposed PR via `submit-suggestion`, or an audit ledger issue via `fleet-audit` — or posts a proactive Chat alert.

Watchdog runs execute autonomously: the agent config sets `approvals.cron_mode: approve` (see `deploy/shared/defaults/config.yaml`), so commands that would otherwise require human approval run without prompting when triggered by a scheduled job.

Full JSON is annotated on [Reference → Cron jobs](/kube-agents/reference/cron-jobs/), along with the Chat Agent profile's separate job file of `no_agent` script jobs (the named-profile cron ticker described in [What fires the schedule](#what-fires-the-schedule), Cluster Agent reconciliation, and first-run onboarding).

## The shipping jobs

The roster, with exact cron expressions, enabled state, and prompts, is generated from `jobs.json` on [Reference → Cron jobs](/kube-agents/reference/cron-jobs/). Seven jobs ship, all enabled: the six fleet audits below and `github-issue-resolver`.

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
- **Asking for a run cancels the silence.** `silent_ok` answers "would a channel want this?", and it cannot see that a person is waiting. So a job dispatched on demand — from Chat, or from a kanban card — always reports its outcome and its ledger issue URL, whatever the flag says. The Platform Agent dispatches the real job rather than re-enacting its work, then relays the run's report in the card's `result`, because that is what reaches Chat.

### The retired jobs

Five watchdogs — `blueprint-sync`, `policy-propagation`, `global-capacity-orchestrator`, `standardization-validator`, and `lifecycle-deprecation-manager` — shipped disabled for several releases and are no longer in the roster. As written none could produce a finding on a stock install: two compared clusters against a "master blueprint" document no install provides, one read policy templates from an unshipped `/opt/defaults/templates/`, one ran hourly with no defined output artifact, and one overlapped `security-patch-orchestrator`.

Their SOPs are retained under `agents/platform/governance/`, so reviving one is a matter of rewriting the SOP against something a stock install actually has and re-adding the job — see [Adding a watchdog](#adding-a-watchdog). Re-adding the entry alone will not help; the SOP is why they were retired.

On a cluster provisioned before they were dropped, the five entries remain on the volume's `cron/jobs.json` in the disabled state that release left them in — see [Disabling a watchdog](#disabling-a-watchdog) for why. They stay off; the image simply no longer has a say.

## What fires the schedule

Hermes' cron ticker is a thread inside `hermes gateway run`, and everything it touches — the job store, the execution ledger, the tick lock — resolves from that process's `HERMES_HOME`. It never enumerates profiles. This image runs a single gateway, homed at `/opt/data`, so the only roster that thread ticks is the Chat Agent's. The Platform Agent's lives at `/opt/data/profiles/platform/cron/jobs.json`, which the thread never opens.

`profile-cron-tick` is what ticks it. It is a `no_agent` script job on the Chat Agent's roster — the one store that does tick — and each minute it runs `hermes cron tick` as a subprocess against every named profile with work due:

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
  "enabled": true,
  "deliver": "local"
}
```

- **`id`** — stable identifier, referenced in observability and disable/enable ops. It outlives renames: `obtainability-audit` is now the Workload Reliability Audit, but the id stays put.
- **`schedule.expr`** — standard 5-field cron in the pod's local time zone (UTC unless the pod's TZ is overridden).
- **`prompt`** — verbatim message sent to the agent when the schedule fires. Governance jobs point at an SOP **relative to the profile home** (`governance/<sop>.md`), which is where `profile_scaffold.py` overlays the baked `/opt/platform-template/governance/` directory. An absolute `/opt/defaults/governance/...` path does not resolve — nothing is mounted there. The six audit prompts also state how long their SOP is and which section holds the checks, because a read that stops early lands in the preamble and the run reports a clean fleet it never inspected; a test in `audit_report.py`'s suite re-derives both numbers from the file so a stale citation fails there rather than at 06:20. What the prompts deliberately do **not** restate is the `[SILENT]` rule — each SOP's closing section states it in full, qualifiers included, and a shorter version in the prompt would both lose the qualifiers and tell the run what its answer looks like before it decides what to check.
- **`skills`** — optional array of skill names to preload. The six audits preload `fleet-audit`; `github-issue-resolver` preloads its namesake skill.
- **`enabled`** — set to `false` to disable a job without deleting its entry.
- **`deliver`** (optional) — controls the scheduler's own chat delivery leg, and it is `"local"` on all seven enabled jobs, meaning the scheduler delivers nowhere. That is deliberate, not a gap: the Platform Agent profile ships no `platforms:` section (contrast `agents/chat/config.yaml`) because a privileged fleet-management profile should not hold a chat destination of its own. Findings still reach a human, by a route that depends on the job: the six audit jobs rewrite the fleet-audit ledger issue on every run, `github-issue-resolver` reports on the individual issues it triages (it is barred from touching an `agent:audit` ledger at all), and a run dispatched from a card or from chat reports back through the response the Chat Agent relays. The alternative, `"all"`, resolves at fire time to whichever platform happens to have a home channel configured then — a routing decision made by the environment rather than by anyone reviewing it — and on the scheduled tick it resolves to nothing at all, stamping a `no delivery target resolved` error onto runs that succeeded.

## Disabling a watchdog

Edit `cron/jobs.json`, flip `enabled` to `false`, and redeploy the workspace (`provision_08_deploy_platform_agent.sh` or `dev/dev_rebuild_agent.sh`). The change is picked up on the next agent restart.

Flip the flag; do not delete the entry. `cron/jobs.json` is image-owned configuration and live scheduler state in the same file, so start-up merges the two rather than replacing one with the other (`profile_scaffold.py`). The image wins every key it ships — which is what makes `enabled: false` take effect — and the volume keeps every key the image is silent about, so each job's run history survives a rollout and a job the operator added through `cronjob(action='create')` is not swept away by one. The cost of that second half is that a merge cannot tell an operator's job from one the image dropped, so **deleting an entry does not stop it firing** on a cluster that already has it — it only ends the image's ability to hold it off.

Deleting an id from the roster is therefore a second step, not the first one. Ship `enabled: false`, let every live cluster merge that state, and only then drop the entry: from that point the volume's own copy keeps the job off with no help from the image. That is the path the five [retired watchdogs](#the-retired-jobs) took.

## Adding a watchdog

1. Write a governance SOP in `agents/platform/governance/<your-sop>.md`.
2. Add a job entry to `cron/jobs.json` pointing at it as `governance/<your-sop>.md`.
3. If the job files findings, add its id to the allowlist in `agents/platform/skills/fleet-audit/scripts/audit_report.py` and preload `"skills": ["fleet-audit"]`.
4. Run `make docs-generate` — the reference table is generated, and a cron expression missing from `CRON_CADENCE` in `scripts/generate_docs.py` renders its cadence as `—`.
5. Redeploy.

Keep the schedule realistic — LLM inference on every tick has cost. Hourly or daily is the sweet spot for most SOPs; sub-15-minute cadences should have a clear justification. Stagger start minutes so two audits never contend for the same session.

Budget the run as well as the schedule. Every job shares one per-turn tool-calling budget, `agent.max_turns` in the profile's `config.yaml` — 250 for the Platform Agent, against a Hermes default of 90 the fleet audits outgrew. A run that exhausts it is stopped mid-flight and recorded as a `timed_out` event, which reads misleadingly: no clock expired, the agent simply took more steps than it was allotted, and raising any of the `HERMES_*_TIMEOUT` values will not help. The six shipping audits finish well inside 250, but an SOP that gains checks and a fleet that gains clusters both spend against it. There is no per-job override — the scheduler honours a per-job `model` but not a per-job turn budget — so the profile-wide value is the only lever.

## Where to go next

- [Reference → Cron jobs](/kube-agents/reference/cron-jobs/) — full annotated `jobs.json`.
- [Governance SOPs](/kube-agents/concepts/governance-sops/) — the playbooks these watchdogs execute.
- [Declarative workflow](/kube-agents/concepts/declarative-workflow/) — how findings become a ledger issue and remediation PRs.
