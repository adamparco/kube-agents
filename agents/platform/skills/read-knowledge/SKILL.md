---
name: read-knowledge
description: Retrieve Operational Knowledge Framework (OKF) entries — runbooks, escalations, blueprints, observations — from the GitOps repo's knowledge/ tree via a strictly read-only, sparse checkout. This is the shared knowledge layer, not a channel for talking to another agent.
---

# read-knowledge — read-only OKF retrieval

This skill lets you look up shared operational knowledge — a **runbook** for a failure you're seeing, an
**escalation** a lower tier raised, a **cluster-blueprint**, a **tenancy-model**, a prior
**observation** — from the GitOps repository's `knowledge/` tree (OKF, 06 §5).

OKF is the **knowledge** layer and only that: durable, curated context one agent records and another
reads later. It is **not** a coordination channel. Cross-tier requests are direct, synchronous mesh
calls (02 §2.3); nothing is left here for a parent to discover by polling.

## When to Use

- **Before acting on an incident:** check for an existing `runbook` (`--type runbook`) that matches the
  symptom instead of re-deriving a fix.
- **When something is waiting on a human:** list the `escalation` entries (`--type escalation`) —
  which now hold only requests a human must resolve (a budget approval, a vendor ticket, a decision
  outside every agent's scope, 06 §5), never a request to another agent.
- **When onboarding a change:** read the relevant `cluster-blueprint` / `tenancy-model` for the target.

## Read-only by construction

This skill **cannot** write. Two properties are enforced by the helper script, not just by convention:

1. **Sparse, read-only checkout.** It fetches **only** `knowledge/` (cone sparse-checkout + shallow
   `--depth=1` + blob filter). It never materializes the deployable `clusters/` / `fleet/` / `policy/`
   trees, and asserts they are absent after checkout. A read can therefore never turn into a deployable
   working tree or accumulate into a commit.
2. **Hard-refuses writes.** Every git call is gated to a read-only allowlist; any `push` / `commit` /
   write intent exits non-zero **before** touching the repo. To change something, use
   `apply-change` — never this skill.

It uses a **contents:read**-scoped token (`GITHUB_READ_TOKEN`). There is no write credential in this
pod to reach for (02 §2.2).

The frontmatter parser is the **same shared module** `dev/okf-validate.py` uses, so what you read
is exactly what CI validates — no schema drift.

## Execution Instructions

Invoke the helper script `read_knowledge.py`:

```bash
# List every runbook in the knowledge base:
./skills/read-knowledge/scripts/read_knowledge.py \
  --repo "$GITOPS_REPO_URL" --ref main --type runbook

# List open escalations (a parent tier's triage sweep):
./skills/read-knowledge/scripts/read_knowledge.py \
  --repo "$GITOPS_REPO_URL" --type escalation

# Read one entry's full content by its knowledge/-relative link:
./skills/read-knowledge/scripts/read_knowledge.py \
  --repo "$GITOPS_REPO_URL" --link runbook/pod-crashloop.md
```

Flags:

- `--repo <url|path>` — the GitOps repo (or `--work-dir <dir>` to reuse an already-checked-out sparse copy).
- `--ref <branch>` — branch/ref to read (default `main`).
- `--type <type>` — filter by OKF `type` (`runbook`, `escalation`, `cluster-blueprint`, …).
- `--link <path>` — fetch one entry by its `knowledge/`-relative path; prints its full content.
- `--json` — machine-readable output.

## After Reading

Act on what you read within your own scope. If an entry (e.g. an `escalation`) implies a change,
**re-derive the scope from your own CR** — do not trust a `to:` field — and submit the change with
`apply-change`. Never mutate anything directly.
