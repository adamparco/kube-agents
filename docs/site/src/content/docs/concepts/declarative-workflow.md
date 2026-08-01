---
title: Declarative workflow
description: All infrastructure changes route through Git. How submit-suggestion and Minty enforce it.
sidebar:
  order: 7
---

The Platform Agent's `SOUL.md` forbids direct infrastructure mutations. When the agent has a fix in mind — a policy update, a node pool tweak, a security patch, a namespace addition — it doesn't `kubectl apply`. It writes the change into your **GitOps repo** as a **pull request** via the `submit-suggestion` skill, using a short-lived GitHub token minted on demand by **Minty**.

## Why

- **Human review.** Every infrastructure change gets seen before it hits prod. The PR is the audit trail.
- **Rollback via revert.** A bad remediation is one revert away from undone.
- **Compatibility with your existing GitOps.** ArgoCD, Flux, RootSync — whichever reconciler you already run applies the merged change. The agent doesn't compete with your reconciler.
- **Least privilege on the cluster.** The agent's Kubernetes identity cannot mutate workloads or cluster state — its only write grant is a leader-election housekeeping Role confined to its own namespace — so even a misled persona cannot change a cluster through the Kubernetes API. Its GCP identity is a separate question, governed by the provisioning-time permission set (`read-only` by default, `gke-admin` as an opt-in). See [Security &amp; IAM](/kube-agents/reference/security-and-iam/#what-the-agent-can-and-cannot-do).

## The `submit-suggestion` skill

Source: [`agents/platform/skills/submit-suggestion/`](https://github.com/gke-labs/kube-agents/tree/main/agents/platform/skills/submit-suggestion).

The agent invokes this skill whenever an SOP or on-request task decides "propose a change". The agent works inside the GitOps repo checkout (whose URL it resolves on startup from `/opt/data/SETTINGS.md`, per `SOUL.md §1`). The flow:

1. Starts from an up-to-date default branch (`git checkout main && git pull origin main`).
2. Creates a topic branch named `platform-agent/<change_type>-<target_id>` (e.g. `platform-agent/upgrade-policy-baseline`).
3. Applies the change (file writes, YAML patches), then stages **only** the specific files it edited — `git add .` / `git add -A` are explicitly forbidden — and commits using Conventional Commit messages.
4. Runs the packaged helper `./skills/submit-suggestion/scripts/submit_suggestion.py --branch … --title … --body …`, which mints a fresh GitHub App token (via `github_token_refresh.py`), pushes the branch, and opens a PR against `main` with `gh pr create`.
5. The script prints the PR URL to stdout; the agent posts it to Chat.

Safety red lines enforced by the skill: direct/manual cluster mutations are forbidden, blanket staging (`git add .`) is refused, and `submit_suggestion.py` hard-blocks force-pushes to the protected branches `main`, `master`, and `production`.

## The `fleet-audit` skill

Source: [`agents/platform/skills/fleet-audit/`](https://github.com/gke-labs/kube-agents/tree/main/agents/platform/skills/fleet-audit).

`submit-suggestion` fits a one-off change: the agent decides what to propose, writes the body, and opens a PR. A recurring [fleet audit](/kube-agents/concepts/autonomous-watchdogs/) does not fit that shape — a daily audit using `submit-suggestion` would open a near-identical PR every morning. `fleet-audit` is the second write path, and it inverts the division of labour: **the model produces evidence, the script produces the pull request.**

The agent's only output is a validated `findings.json` — one entry per deviation, each carrying the literal read-only command that proves it. `audit_pr.py` does the rest:

```bash
./skills/fleet-audit/scripts/audit_pr.py start --audit <audit-id>
# … the agent inspects the fleet read-only and writes findings.json …
./skills/fleet-audit/scripts/audit_pr.py finish --audit <audit-id> --findings-file <path>
```

`start` mints credentials, ensures the `audit:<id>` labels, resets `platform-agent/audit-<audit-id>` onto `main`, and reports whether the stream already has an open PR. `finish` validates the document, stages **only** the paths named in `remediation.path`, commits, force-pushes, renders the PR body, and then either opens the stream's PR or rewrites the existing one in place — commenting with just the delta since the last run. A run with no findings closes the PR instead.

Three properties follow from the script owning the body rather than the model:

- **One PR per audit stream.** The `--audit` id is checked against a fixed allowlist, and the branch name is derived from it rather than passed in, so a typo cannot open a sixth stream.
- **A computable delta.** The body carries a hidden `<!-- audit-findings: [...] -->` block; the next run diffs finding ids against it. This is why the SOPs require finding ids to be stable and free of timestamps.
- **No invented output.** The model never writes the title, body, commit message, or any timestamp — so two runs against an unchanged fleet produce an unchanged PR.

It shares `submit-suggestion`'s guardrails: same Minty token path, the same refusal of `git add .` / `git add -A`, and the same hard block on force-pushing `main`, `master`, or `production`.

## Minty (GitHub Token Minter)

Source: [`k8s-operator/config/integrations/github/`](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator/config/integrations/github).

Minty is a small in-cluster service that brokers GitHub App installation tokens without any long-lived secret ever touching the agent's pod.

### How it works

1. A GitHub App is created (once, by you) with the needed permissions (`contents:write`, `pull_requests:write`) and installed on the target repo.
2. The App's private key is imported into a **GCP KMS asymmetric signing key** (keyring `github-token-minter-keyring`, key `github-token-minter-key`, created by `provision_10_deploy_github_minter.sh`) — the raw key material never lives outside KMS.
3. When `submit-suggestion` needs a token, the credential broker calls Minty (default endpoint `http://github-token-minter.kubeagents-system.svc.cluster.local:8080/token`) using the agent's Workload Identity.
4. Minty asks KMS to sign a JWT with the imported private key.
5. Minty exchanges the JWT with GitHub for a **short-lived installation token scoped to the target repository**.
6. Minty returns the token to the caller.

### Recovery

If a git operation fails with an auth error (e.g. `fatal: Authentication failed`, `could not read Username`), `SOUL.md §3` requires the agent to run the packaged token refresher:

```bash
# outside a git repo
./scripts/github_token_refresh.py <owner>/<repo>
# inside a git repo (repo inferred from remote.origin.url)
./scripts/github_token_refresh.py
```

which triggers a fresh mint from Minty and caches it, then retries the command. The recovery ladder (`§4`) caps retries at **5 iterations or ~10 minutes per distinct blocker** before escalating.

## Complementary integrations

Alongside GitHub PR flows, the persona explicitly names other declarative pipelines it will use when they're the active workflow:

- **Config Connector** — for GCP resources modeled as Kubernetes CRs.
- **ArgoCD / Flux** — inspecting `RootSync` state and Application health as part of diagnostics.
- **GKE Hub fleet membership / Connect Gateway** — for multi-cluster targeting.

`SOUL.md §4` requires the agent to inspect these before manual intervention.

## Anti-patterns

Explicitly called out as forbidden in `SOUL.md`:

- Running raw `kubectl apply` against a live cluster for infrastructure changes.
- Configuring `git` credential helpers manually.
- Running ad-hoc `git clone` against the GitOps repo for change submission, or driving `git`/`gh` directly to open a PR. `SOUL.md §3.2` names exactly two packaged skills that may own the write path: `submit-suggestion` for a one-off change, `fleet-audit` for a scheduled audit run.
- Outputting raw tool schemas, JSON payloads, or exit codes in user-facing messages.

## Where to go next

- [Deploy → Token minter](/kube-agents/deploy/token-minter/) — Minty install details.
- [Concepts → Governance SOPs](/kube-agents/concepts/governance-sops/) — the playbooks that invoke `submit-suggestion`.
- [Reference → Attribution](/kube-agents/reference/attribution/) — how a PR ties back to the authenticated human who requested it.
