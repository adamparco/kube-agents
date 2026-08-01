---
title: Proactive autonomy
description: The background watchdogs that make kube-agents more than a chatbot — audit, remediate, verify, alert.
---

Most agent products are reactive: you ask, they answer. `kube-agents` is designed to _also_ act on its own. Cron-scheduled jobs, defined in [`agents/platform/cron/jobs.json`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/cron/jobs.json), fire the Platform Agent at governance SOPs on a rolling schedule. Findings become changes the agent submits, verifies and reports — plus proactive Chat messages.

## The hands-free loop

```text
Cron tick  →  Governance SOP  →  Platform Agent investigates  →  apply-change skill
                                                              →  Action Broker classifies + plans undo
                                                              →  executed, or parked for a human
                                                              →  ActionRecord + undo handle
                                                              →  Proactive Chat alert
```

Every step is real code shipping in the repo. The SOPs live in [`agents/platform/governance/`](https://github.com/gke-labs/kube-agents/tree/main/agents/platform/governance); the [`apply-change`](https://github.com/gke-labs/kube-agents/tree/main/agents/platform/skills/apply-change) skill builds the envelope; the Action Broker beside the agent pod is the only thing in the scope holding a write identity; the Chat integration is Google Chat by default with Slack as an opt-in.

## What runs on its own

The shipping schedule at time of writing:

| Job                             | Schedule             | What it does                                                                                    |
| ------------------------------- | -------------------- | ----------------------------------------------------------------------------------------------- |
| `blueprint-sync`                | Daily 09:00          | Audit clusters against master blueprints; reconcile drift declaratively.                        |
| `policy-propagation`            | Hourly               | Push updated security, network, and resource policies across clusters and namespaces.           |
| `global-capacity-orchestrator`  | Hourly               | Fleet-wide utilization audit; submit the rebalancing when regions are hot or cold.              |
| `fleet-wide-cost-analysis`      | Daily 10:00          | Aggregate cost usage; surface saving opportunities and right-sizing candidates.                 |
| `security-patch-orchestrator`   | Daily 11:00          | CVE scan; coordinate staggered emergency GKE upgrades.                                          |
| `obtainability-audit`           | Daily 12:00          | Find rigid capacity allocations; submit the patches that move workloads onto flexible capacity. |
| `compliance-audit`              | Weekly Sun 09:00     | Fleet-wide security/architectural policy compliance sweep.                                      |
| `standardization-validator`     | Weekly Sun 10:00     | Deep-diff of live cluster configs vs. corporate architectural patterns.                         |
| `lifecycle-deprecation-manager` | Monthly (1st, 09:00) | Track deprecated Kubernetes API versions ahead of the next GKE upgrade window.                  |
| `github-issue-resolver`         | Every 30 min         | Poll the target repo; triage and (within tight guardrails) resolve open issues.                 |

Schedules are literal `cron` expressions from `jobs.json`. See [Reference → Cron jobs](/kube-agents/reference/cron-jobs/) for the full table with cron expressions and prompts.

## Why this matters

The alternative for each of these is a person on a rotation, a static Terraform module, or an alert that pages someone in the middle of the night. `kube-agents` closes the loop:

- **Audit → fix, verified** — the agent doesn't just detect drift, it corrects it and reports the `ActionRecord` and undo handle. A finding that ends in a recommendation for work the agent was allowed to do is treated as a defect.
- **Fleet-wide read on the pod, scoped write nowhere near it** — the Platform Agent's own identity holds no write verb at all; the write authority lives in the broker beside it, under a different identity, behind a classifier the agent cannot argue with.
- **Recovery ladder before escalation** — `SOUL.md §8` climbs retry → one alternative → roll back → escalate → page a human, and never skips a rung silently.

The design goal: fleet issues stop rotting silently while the on-call queue is quiet.

## Safety rails

- **No write credential in the pod.** `SOUL.md §1` makes this a core truth, not a rule the model is asked to remember: there is no `kubectl apply` and no `gcloud` to reach for, because the identity that would authorize them is not there. Everything routes through `apply-change` (`SOUL.md §9`, §10).
- **High-blast-radius operations park for a human.** Cluster deletion, tenant offboarding, broad IAM revocation — the broker classifies these as gated from the target objects and the diff, not from how the request was phrased. The agent cannot approve its own gated action, and re-submitting a refused one in a different shape is treated as a security event.
- **Bounded recovery.** The ladder in `SOUL.md §8` ends at paging a human, and a rolled-back target goes into cooldown rather than being retried immediately.

## Where to go next

- [Autonomous watchdogs](/kube-agents/concepts/autonomous-watchdogs/) — how cron ticks become tool calls.
- [Declarative workflow](/kube-agents/concepts/declarative-workflow/) — the `apply-change` envelope and the broker pipeline behind it.
- [Governance SOPs](/kube-agents/concepts/governance-sops/) — the playbooks the watchdogs execute.
