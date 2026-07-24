---
type: index
title: Operational Knowledge Framework (OKF) — index
tags: [okf, index]
---

# OKF index

The Operational Knowledge Framework is durable, human-reviewed knowledge shared indirectly across
agent tiers (06 §5). It is markdown + YAML frontmatter under `knowledge/`, lives **outside** the
paths the pipeline deploys, and is **never applied to a cluster**. Agents **read** OKF for context
and **propose** updates via PR; humans approve (curate-as-code). OKF holds durable knowledge only —
**not** session state.

Every entry must declare a `type` in frontmatter. The canonical starting set (open convention, not a
hard enum — add new types by PR):

| `type`              | Purpose                               |
| ------------------- | ------------------------------------- |
| `cluster-blueprint` | Standard cluster config baseline      |
| `tenancy-model`     | Namespace isolation standard          |
| `runbook`           | Operational procedure (SRE CUJ)       |
| `metric-definition` | Named metric/KPI definition           |
| `escalation`        | A cross-tier request not yet a change |
| `observation`       | A durable finding worth sharing       |

## Entries

- [Standard GKE cluster blueprint](cluster-blueprint/standard-gke.md) — `cluster-blueprint`
- [Workload CrashLoopBackOff runbook](runbook/pod-crashloop.md) — `runbook`

## Escalations

Cross-tier requests are raised as `escalation/<slug>.md` entries (`type: escalation`) by the
`raise-escalation` skill and picked up by the parent tier via `read-knowledge` (`--type escalation`) —
never a direct agent-to-agent call (invariant 3). The [`escalation/`](escalation/.gitkeep) directory is
seeded empty; entries appear here as lower tiers raise them.

_Markdown links between entries form the knowledge graph. An optional `log.md` may record history._
