---
name: provision-cluster-admin
description: Provision a read-only Cluster Admin Agent for a cluster (the F4 cascade) — render the full GitOps bundle (Agent CR + pre-created read-only identity + spoke bootstrap + egress policy) and report exactly what was written, for a human to review and take forward. Never mints privilege at runtime.
---

# provision-cluster-admin — Provision a Cluster Admin Agent (F4 cascade)

This skill equips the **Platform Agent** to provision a subordinate **Cluster Admin Agent** for a
cluster in its project — the **F4 provisioning cascade** (05 §5). The Platform Agent is READ-ONLY: it
does not create the agent, its identity, or any RBAC at runtime. It **renders a declarative bundle**
and reports where it wrote it; a human reviews and merges; the customer's CI/CD applies it in bootstrap
order and the kube-agents controller reconciles the pod **bound to the pre-created ServiceAccount** (08 §4).

## Why this still renders a bundle

The end-state for this skill is different from what you are reading: 02 §6 has the parent submit **one
Action Envelope** that creates the child `Agent` CR together with its reader and actor identities, and
02 §2.1 renames this skill accordingly. That conversion is **deliberately not done here**, and the
directory rename is the only part of it that has landed.

The reason is the `B-001 · B-002` ruling in `docs/build/BACKLOG.md`. The tier template that mints the
child's grants must be rendered by **deterministic broker code**, never by a renderer that lives in the
agent pod. 03 §4.2 counts "a parent cannot **express** an over-grant" and "a parent cannot **cause**
one" as two separate enforcement layers, and the first layer exists only while the renderer is
somewhere the LLM cannot reach around. Converting this skill by pointing its own `scripts/` at the
broker would delete that layer while looking like a rename.

So the work is split, and the order is load-bearing:

- **P10-T0** — the shared tier-template renderer lands in **broker code** (and becomes the single
  definition site the `vap-agent-scope` CEL allow-list in P10-T1 is generated from).
- **P11-T4** — the cascade skills convert to envelope submission, on top of that renderer. This
  `SKILL.md` collapses at that point: when to provision a child, that scope-and-agent is one action,
  how to read a refusal. No `scripts/`, no `assets/`.

It is not Phase 9 work. Phase 9's acceptance is that **no write authority exists anywhere in the
system**, and this renderer's entire output is grants — building it inside the phase that proves
nothing can mint authority is incoherent. The `assets/` tree and `scripts/render_cluster_admin.py`
here are the material P10-T0 derives the Go template from; do not delete them.

Nothing in the current path is weaker than the spec asks for. This skill emits a bundle into a human's
hands and mints nothing.

## When to Use

- A new (or existing) cluster in the project needs its own read-only cluster-admin persona — for
  auditing, tenancy, and proposing changes (namespaces, a developer-team agent one layer down).
- **One cluster-admin agent per cluster** (the admission webhook enforces cardinality on the
  `(tier, scope)` key). If one already exists for the cluster, edit its CR instead of proposing a new one.

> **You never run `kubectl`/`gcloud`/cloud mutations, and you never mint an identity at runtime.** You
> render local files and report them. Everything after that is a human's decision and the pipeline's
> apply. The bundle ships the read-only identity as _desired state_ (pre-created by the pipeline),
> never created by the controller.

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
./skills/provision-cluster-admin/scripts/render_cluster_admin.py \
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
placeholders and call it out when you report, so the reviewer fills them before merge.

The script prints exactly which files it wrote.

### Step 2: Verify what you rendered

- **Read-only identity:** the ClusterRole in `identity/` grants only `get/list/watch` (the VAP will
  reject anything else at apply time — do not rely on that; get it right here).
- **Closed allowlist:** `agent.yaml` `integration.googleChat.allowedUsers` is non-empty and names the
  intended human(s). An empty allowlist means the router refuses everyone (fail-closed) — and the
  webhook rejects an enabled integration with an empty list.
- **Scope + parent:** `scope.projectId`/`scope.clusterName` match the target, and `parentRef` names
  this Platform Agent.
- **No placeholders left** in anything the pipeline must apply (or they are explicitly flagged when you
  report).

### Step 3: Report the bundle — you do not submit it

**There is no submission step in this skill, and you must not invent one.** The `submit-suggestion`
skill that used to take the rendered tree and open a Pull Request has been **deleted from every tier**,
and the envelope path that replaces it is P11-T4 (see "Why this still renders a bundle" above). You
have no write credential of any kind: no `git` push, no `gh pr create`, no `kubectl apply`.

What you do instead is report, precisely enough that a human can act without re-deriving anything:

- **Where the bundle is** — the exact file list the render helper printed, and nothing you did not write.
- **What it provisions** — the cluster, the tier, and that the identity in it is read-only.
- **What is unfinished** — every `REPLACE_WITH_*` placeholder still in the tree, named individually,
  and what a correct value looks like.
- **What has to happen next, and by whom** — a human reviews the bundle, takes it into the GitOps repo,
  and merges it; CI/CD applies it in bootstrap order. Nothing is running until then.

Never describe the child as created, provisioned, or requested. It is rendered on disk, and that is
the whole of what happened.

## Best Practices

1. **Nothing takes effect without a human** — the bundle is desired state; a person reviews the
   identity + allowlist before anything runs.
2. **Least privilege in the cloud too** — for a project with more than one cluster-admin agent, give
   each a per-cluster viewer GSA (or an IAM Condition) so "reads only its own cluster" holds in the
   cloud, not just in RBAC (flagged in the rendered identity file).
3. **Keep pins immutable** — repin the bootstrap bases (cert-manager, `config/install`) to a
   content-addressable ref/digest for production.
4. **One agent per cluster** — if the cluster already has a cluster-admin agent, edit its CR; don't
   render a duplicate (the webhook will reject it anyway).
