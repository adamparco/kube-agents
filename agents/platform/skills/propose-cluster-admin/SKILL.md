---
name: propose-cluster-admin
description: Provision a read-only Cluster Admin Agent for a cluster (the F4 cascade) — render the full GitOps bundle (Agent CR + pre-created read-only identity + spoke bootstrap + egress policy) and open it as a reviewed Pull Request. Never mints privilege at runtime.
---

# propose-cluster-admin — Provision a Cluster Admin Agent (F4 cascade)

This skill equips the **Platform Agent** to provision a subordinate **Cluster Admin Agent** for a
cluster in its project — the **F4 provisioning cascade** (05 §5). The Platform Agent is READ-ONLY: it
does not create the agent, its identity, or any RBAC at runtime. It **renders a declarative bundle** and
opens a **GitOps Pull Request**; a human reviews and merges; the customer's CI/CD applies it in bootstrap
order and the kube-agents controller reconciles the pod **bound to the pre-created ServiceAccount** (08 §4).

## When to Use

- A new (or existing) cluster in the project needs its own read-only cluster-admin persona — for
  auditing, tenancy, and proposing changes (namespaces, a developer-team agent one layer down).
- **One cluster-admin agent per cluster** (the admission webhook enforces cardinality on the
  `(tier, scope)` key). If one already exists for the cluster, edit its CR instead of proposing a new one.

> **You never run `kubectl`/`gcloud`/cloud mutations, and you never mint an identity at runtime.** The
> only write path is this reviewed PR actuated by CI/CD. The bundle ships the read-only identity as
> _desired state_ (pre-created by the pipeline), never created by the controller.

## What the Bundle Contains

The render produces the complete tree a fresh spoke needs — and nothing that grants privilege at runtime:

```
clusters/<cluster>/
  bootstrap/                       # ordered control-plane waves (05 §7) — resolves the chicken-and-egg
    00-cert-manager/               #   cert-manager (webhook serving cert); pipeline waits for Ready
    10-controller/                 #   CRD + controller + webhooks + kage-router + mesh CA (config/install)
    20-policy/                     #   the agent-read-only VAP — ENFORCING before any identity applies
  agents/
    identity/cluster-admin-identity.yaml   # pre-created read-only KSA + ClusterRole/Binding + WI
    agent.yaml                             # the cluster-admin Agent CR (references the KSA by name)
    netpol-cluster-admin-egress.yaml       # per-tier default-deny egress (cross-cluster contract, 05 §5)
```

The pipeline applies them in order: `provisioning/` → `bootstrap/` (00 → 10 → 20) → `namespaces/` →
`agents/` (identity **before** the CR, under an already-enforcing VAP). See the rendered
`bootstrap/README.md` for the invariants.

## Execution Instructions

### Step 1: Render the bundle

Run the render helper, substituting the cluster's parameters. It writes local files only (token
substitution over `assets/`) — no credentials, no mutation:

```bash
./skills/propose-cluster-admin/scripts/render_cluster_admin.py \
  --cluster <cluster> \
  --project-id <gcp-project-id> \
  --location <region> \
  --admin-chat-id users/<CHAT_USER_ID> \
  --hub-inference-cidr <hub LiteLLM private CIDR> \
  --hub-minty-cidr <hub Minty private CIDR> \
  --github-cidrs <GitHub egress CIDR> \
  --mcp-cidrs <MCP grounding CIDR> \
  --repo-root .
```

Any flag you omit is written as a `REPLACE_WITH_*` placeholder for the reviewer to fill — the diff is
still reviewable. A placeholder **CIDR** is an invalid value that `kubectl apply` rejects, so the
pipeline cannot silently apply a half-configured egress policy; a placeholder **chat ID** is a valid
string that applies but fails closed (it matches no real user, so the router refuses everyone). Prefer
to supply real values you already know (project, cluster, location, the cluster admin's Chat ID). The
hub/GitHub/MCP CIDRs come from the fleet's networking config; if you don't have them, leave the
placeholders and call it out in the PR body so the reviewer fills them before merge.

The script prints exactly which files it wrote — stage **only** those.

### Step 2: Verify before proposing

- **Read-only identity:** the ClusterRole in `identity/` grants only `get/list/watch` (the VAP will
  reject anything else at apply time — do not rely on that; get it right here).
- **Closed allowlist:** `agent.yaml` `integration.googleChat.allowedUsers` is non-empty and names the
  intended human(s). An empty allowlist means the router refuses everyone (fail-closed) — and the
  webhook rejects an enabled integration with an empty list.
- **Scope + parent:** `scope.projectId`/`scope.clusterName` match the target, and `parentRef` names
  this Platform Agent.
- **No placeholders left** in anything the pipeline must apply (or they are explicitly flagged for the
  reviewer).

### Step 3: Open the PR via submit-suggestion

Hand the rendered tree to [submit-suggestion](../submit-suggestion/SKILL.md) on a branch in your tier's
namespace, staging **only** the rendered files (never `git add .`):

```bash
git checkout -b platform-agent/provision-cluster-admin-<cluster>
git add clusters/<cluster>/bootstrap clusters/<cluster>/agents
git commit -m "feat(<cluster>): provision read-only cluster-admin agent + spoke bootstrap"
./skills/submit-suggestion/scripts/submit_suggestion.py \
  --branch "platform-agent/provision-cluster-admin-<cluster>" \
  --title "Provision cluster-admin agent for <cluster>" \
  --body "F4 cascade: read-only Cluster Admin Agent for <cluster> (Agent CR + pre-created read-only identity + spoke bootstrap waves + default-deny egress). Applied by CI/CD after review; the controller binds the pod to the pre-created SA and mints no RBAC. Fill any REPLACE_WITH_* placeholders before merge."
```

Record and return the PR URL. The agent starts only after a human merges and the pipeline applies the
bundle.

## Best Practices

1. **Everything through a PR** — the bundle is desired state; a human reviews the identity + allowlist
   before anything runs.
2. **Least privilege in the cloud too** — for a project with more than one cluster-admin agent, give
   each a per-cluster viewer GSA (or an IAM Condition) so "reads only its own cluster" holds in the
   cloud, not just in RBAC (flagged in the rendered identity file).
3. **Keep pins immutable** — repin the bootstrap bases (cert-manager, `config/install`) to a
   content-addressable ref/digest for production.
4. **One agent per cluster** — if the cluster already has a cluster-admin agent, edit its CR; don't
   propose a duplicate (the webhook will reject it anyway).
