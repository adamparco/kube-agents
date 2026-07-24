---
name: raise-escalation
description: Raise a cross-tier request to your parent tier as an Operational Knowledge Framework (OKF) escalation entry submitted through a reviewed GitOps PR. This is how a lower tier asks a higher tier to act without ever calling it directly.
---

# raise-escalation — ask your parent tier, indirectly

When something is **outside your scope** and needs the tier above you to act — a cluster-wide change a
Developer Team can't make, a fleet-wide policy a Cluster Admin can't set — you do **not** call the
parent agent. Agents never call each other (invariant 3). Instead you leave a request in shared
knowledge and the parent picks it up on its own schedule.

This skill writes an OKF **`escalation`** entry (`knowledge/escalation/<slug>.md`, `type: escalation`,
06 §5) on a proposal branch and submits it via **`submit-suggestion`**. The request therefore travels
only as a **reviewed GitOps PR**; the parent tier reads it later with `read-knowledge` in its
**escalation-triage SOP**. The only egress is the Git remote (plus loopback for the submit helper) —
there is no direct agent→agent path.

## When to Use

- **A Developer Team agent** needs a change beyond its namespace (a cluster-scoped NetworkPolicy, a
  node-pool change, a quota it can't set): escalate to `cluster-admin`.
- **A Cluster Admin agent** needs a fleet-wide or platform-level change (a new blueprint, a
  cross-cluster policy): escalate to `platform`.
- You have an incident whose fix is **not in your authority** — raise it so the right tier can propose
  the change.

Do **not** use this to make a change you _can_ make yourself — use `submit-suggestion` directly for that.

## Indirect by construction

- The request reaches the parent **only** through the GitOps repo (a PR the parent reads), never a call.
- The entry's `to:` field is **advisory**. The parent's triage SOP **re-derives its own scope from its
  own CR and ignores `to:`**, so a forged or misrouted escalation can't widen anyone's authority.
- Pickup is **not** real-time: it is bounded by the parent's cron cadence **plus** human merge of the
  escalation PR. That is correct — an escalation is _a request, not yet a change_.

## Execution Instructions

Invoke the helper script `raise_escalation.py`. It derives your tier from `$AGENT_TIER` and your parent
from that tier, writes the entry on a `<tier>-agent/escalation-<slug>` branch, and hands off to
`submit-suggestion`:

```bash
# Raise an escalation (opens a real PR):
./skills/raise-escalation/scripts/raise_escalation.py \
  --repo "$GITOPS_REPO_URL" --ref main \
  --title "Namespace team-x needs a cluster-scoped egress policy" \
  --summary "team-x workloads must reach restricted.googleapis.com; this is cluster-scoped and outside our namespace authority." \
  --severity medium

# Preview the escalation + corrective-PR artifact with no push / no PR (hermetic):
./skills/raise-escalation/scripts/raise_escalation.py \
  --work-dir ./gitops --title "..." --summary "..." \
  --dry-run --artifact-dir ./.escalation-artifact
```

Flags: `--repo`/`--work-dir` (the GitOps repo, mutually exclusive), `--ref` (base branch, default
`main`), `--title`, `--summary`, `--severity` (default `medium`), `--slug` (default: derived from
title), `--to` (advisory parent override), `--created` (default: today), `--tier` (default
`$AGENT_TIER`), `--dry-run` / `--artifact-dir`.

## After Raising

Record the PR link (or, under `--dry-run`, the artifact path). Do **not** wait synchronously for the
parent — it will pick the escalation up on its next sweep. Track the lifecycle
(`open → ack → resolved`) as further PR edits to the same entry; never mutate anything directly.
