---
title: Declarative workflow
description: How an agent changes anything — an Action Envelope to its own broker — and where Git still fits.
sidebar:
  order: 6
---

An agent pod holds **no write credential**. Nothing in it can change a cluster or a cloud resource: no `kubectl apply`, no `gcloud`, no client carrying a mutating token. When the agent has a fix in mind — a policy update, a node pool tweak, a security patch, a namespace addition — it describes the change declaratively as an **Action Envelope** and submits it to its own **Action Broker**, a companion workload holding the only write identity in that tier's scope. The broker classifies it, plans the undo, gates what needs a human, executes, verifies, and journals it.

## Why

- **A manipulated agent has nothing to forge a write with.** The worst a prompt-injected instruction can produce is an envelope — still scope-checked, classified, and gated by code that never read the text that fooled the model.
- **Risk is computed, not claimed.** The classifier reads the target objects and the diff, not the agent's confidence. The envelope has no field for the agent's tier, scope, risk class, or approval; their absence is the security property. The agent can ask for _more_ gating (`require_approval`) and never for less.
- **Every change is undoable at the moment it is reported.** The undo plan is generated _before_ execution. An action the broker cannot plan an undo for is reclassified as gated rather than proceeding quietly.
- **Enforced least privilege.** The reader identity on the agent pod holds no write verb on anything, so containment does not depend on the persona behaving.

## The `apply-change` skill

Source: [`agents/platform/skills/apply-change/`](https://github.com/gke-labs/kube-agents/tree/main/agents/platform/skills/apply-change). All three tiers hold it — it is the only write path any of them has.

Two tools, both reaching the tier's broker through the in-pod `platform_control` MCP server:

| Tool                                                | What it does                                                                                                    |
| --------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `plan_action(intent, operations, trigger_source)`   | Everything except the execution: the risk classification, the blast radius, and the undo plan. Changes nothing. |
| `submit_action(intent, operations, trigger_source)` | The same envelope, executed — subject to classification, the brake, gating and verification.                    |

Each envelope carries:

- **`intent`** — one sentence written for a human, saying what the change is _for_. It is stored on the record and it is what a person reads at 3am.
- **`operations`** — the concrete changes. Each has an `op` (`create`, `apply`, `patch`, `delete`, `scale`) and exactly one of `target`, `targetSelector`, or `cloudTarget`.
- **`trigger_source`** — what put the agent in motion, one of exactly seven words: `chat`, `undo`, `watch`, `alert`, `cron`, `delegation`, `escalation`. The first two mean a human asked; the other five mean the agent decided, which is what the platform's autonomy reporting counts.

The broker then runs a pipeline the agent can neither skip nor influence: authenticate the caller, derive `(tier, scope)` from the **authenticated identity** rather than from the envelope, resolve every target against that scope, classify risk in code, check the brake, generate an undo plan, gate if required, snapshot, execute, verify, journal an `ActionRecord`.

Three outcomes come back and they are not interchangeable: **submitted** (an `actionId`, and the change happened or is happening), **parked for approval** (nothing has changed and nothing will until a human approves), and **refused** (the reply begins with `REFUSED`; scope, the brake, or a policy said no).

## Where Git still fits

Git is no longer the gate on an infrastructure change. It is a record, sometimes an executor, and still the review surface for provisioning a new tier.

- **Write-behind IaC mirror.** Where one is configured, the broker commits the resulting state _after_ the change. It is a mirror, not an approval step, and never a reason to wait.
- **A GitOps engine that owns a path.** Where ArgoCD, Flux, or RootSync actually owns a path, the broker routes that action through the engine and verifies the outcome rather than racing the reconciler.
- **Provisioning the next tier down.** The cascade skills — [`provision-cluster-admin`](https://github.com/gke-labs/kube-agents/tree/main/agents/platform/skills/provision-cluster-admin) on the Platform Agent and [`provision-developer-team`](https://github.com/gke-labs/kube-agents/tree/main/agents/cluster-admin/skills/provision-developer-team) on the Cluster Admin Agent — still render a declarative bundle (the child `Agent` CR, its pre-created read-only identity, isolation manifests, egress policy) for a human to review and merge. That is deliberate, not leftover: the template that mints a child's grants is being moved into deterministic broker code first, so that a parent cannot _express_ an over-grant as well as being unable to cause one.
- **`github-issue-resolver`**, whose subject matter is GitHub issues and pull requests in the first place.

Each of those needs a GitHub token, which is what Minty is for.

## Minty (GitHub Token Minter)

Source: [`k8s-operator/config/integrations/github/`](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator/config/integrations/github).

Minty is a small in-cluster service that brokers GitHub App installation tokens without any long-lived secret ever touching the agent's pod.

### How it works

1. A GitHub App is created (once, by you) with the needed permissions (`contents:write`, `pull_requests:write`) and installed on the target repo.
2. The App's private key is wrapped in a **GCP KMS key** (created by `provision_10_deploy_github_minter.sh`) — the raw key material never lives outside KMS.
3. When something in the install needs a GitHub token — the write-behind mirror, a rendered cascade bundle, `github-issue-resolver` — it calls Minty via Workload Identity.
4. Minty asks KMS to sign a JWT with the wrapped private key.
5. Minty exchanges the JWT with GitHub for a **1-hour installation token**.
6. Minty returns the token to the caller.

### Recovery

If a git operation fails with an auth error (expired token, revoked installation), the recovery script forces a fresh mint and caches it:

```bash
./scripts/github_token_refresh.py <owner>/<repo>
```

`SOUL.md §8` is the ladder around it: retry with backoff, one alternative approach, roll back, escalate, page a human — never skipping a rung silently.

## Complementary integrations

The persona explicitly names the other declarative pipelines it will inspect rather than work around:

- **Config Connector** — for GCP resources modeled as Kubernetes CRs.
- **ArgoCD / Flux** — inspecting `RootSync` state and Application health as part of diagnostics.
- **GKE Hub fleet membership / Connect Gateway** — for multi-cluster targeting.

## Anti-patterns

Explicitly called out as forbidden in `SOUL.md`:

- Running raw `kubectl apply` or `gcloud` against a live cluster. If the agent reaches for one, the change belongs in an envelope.
- Opening a pull request, a GitHub issue, or an OKF entry to _propose_ work the agent was already allowed to do. An answer that ends in a proposal for in-scope work is a defect — the same kind of defect as an action that failed.
- Claiming to have decided its own risk class, or describing a parked action in the past tense.
- Configuring `git` credential helpers manually.
- Outputting raw tool schemas, JSON payloads, or exit codes in user-facing messages.

## Where to go next

- [Deploy → Token minter](/kube-agents/deploy/token-minter/) — Minty install details.
- [Concepts → Governance SOPs](/kube-agents/concepts/governance-sops/) — the playbooks that submit envelopes.
- [Reference → Attribution](/kube-agents/reference/attribution/) — how a change ties back to the authenticated human who requested it.
