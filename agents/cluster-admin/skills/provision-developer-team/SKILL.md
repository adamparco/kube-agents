---
name: provision-developer-team
description: Provision a read-only Developer Team Agent for a namespace (the F4 cascade, one tier down) — render the full per-namespace GitOps bundle (Agent CR + pre-created read-only identity + isolation manifests + egress policy) and report exactly what was written, for a human to review and take forward. Never mints privilege at runtime; ships no bootstrap/VAP waves.
---

# provision-developer-team — Provision a Developer Team Agent (F4 cascade, one tier down)

This skill equips the **Cluster Admin Agent** to provision a subordinate **Developer Team Agent** for a
namespace in its cluster — the **F4 provisioning cascade** one layer below the platform→cluster-admin
step (05 §5). The Cluster Admin Agent is READ-ONLY: it does not create the agent, its identity, the
namespace, or any RBAC at runtime. It **renders a declarative bundle** and reports where it wrote it; a
human reviews and merges; the customer's CI/CD applies it in the `namespaces/` wave and the
kube-agents controller reconciles the pod **bound to the pre-created ServiceAccount** (08 §4).

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
nothing can mint authority is incoherent. The `assets/` tree and `scripts/render_developer_team.py`
here are the material P10-T0 derives the Go template from; do not delete them.

Nothing in the current path is weaker than the spec asks for. This skill emits a bundle into a human's
hands and mints nothing.

## When to Use

- A namespace (tenant) in this cluster needs its own read-only developer-team persona — for auditing
  its workloads and proposing changes to that one namespace via GitOps.
- **One developer-team agent per namespace** (the admission webhook enforces cardinality on the
  `(tier, scope)` key, which for this tier is derived from `scope.namespace`). If one already exists for
  the namespace, edit its CR instead of rendering a new one (the webhook will reject a duplicate).

> **You never run `kubectl`/`gcloud`/cloud mutations, and you never mint an identity at runtime.** You
> render local files and report them. Everything after that is a human's decision and the pipeline's
> apply. The bundle ships the read-only identity as _desired state_ (pre-created by the pipeline),
> never created by the controller.

## What the Bundle Contains

The render produces the complete per-namespace tree a fresh tenant needs — and, unlike the
cluster-admin cascade, **no `bootstrap/` and no VAP waves** (the cluster already has the control plane
and the agent-read-only ValidatingAdmissionPolicy, installed by the cluster-admin's own F4 bootstrap):

```
clusters/<cluster>/namespaces/<namespace>/
  00-namespace.yaml                    # the tenant Namespace (blast-radius boundary; NetworkPolicy label)
  10-resourcequota.yaml                # aggregate compute/object cap (03 §3)
  20-netpol-default-deny.yaml          # zero-trust ingress+egress baseline (03 §10)
  30-netpol-developer-team-egress.yaml # per-tier egress allowlist (DNS + A4 alias hop + external CIDRs)
  40-service-aliases.yaml              # ExternalName aliases (A4 graft: litellm, github-token-minter)
  50-developer-team-identity.yaml      # pre-created read-only KSA + namespaced Role/Binding + WI
  60-developer-team-agent.yaml         # the developer-team Agent CR (references the KSA by name)
  README.md                            # human-facing description (not applied)
```

The seven manifests are the "seven 06 §3 paths"; the README is documentation. The pipeline applies the
directory recursively in **numeric-prefix (lexical) order**, so the Namespace exists first, the identity
(50) applies before the CR (60) (identity-before-pod), and all of it lands in the `namespaces/` wave —
**before** `agents/`, under the already-enforcing VAP.

## Execution Instructions

### Step 1: Render the bundle

Run the render helper, substituting the namespace's parameters. It writes local files only (token
substitution over `assets/`) — no credentials, no mutation:

```bash
./skills/provision-developer-team/scripts/render_developer_team.py \
  --cluster <cluster> \
  --namespace <namespace> \
  --project-id <gcp-project-id> \
  --location <region> \
  --team-lead-chat-id users/<CHAT_USER_ID> \
  [--workload-identity [--gke-dataplane auto|v1|v2]] \
  [--hub-inference-cidr <hub LiteLLM private CIDR>] \
  [--hub-minty-cidr <hub Minty private CIDR>] \
  [--mcp-cidrs <MCP grounding CIDRs>] \
  --kube-apiserver-cidrs <apiserver IP(s) this cluster's pods reach> \
  --repo-root .
```

`--parent` defaults to `cluster-admin-<cluster>` (this Cluster Admin Agent — the F4 parent of the leaf
tier); pass it only to override.

The bracketed flags are **egress widenings**, and each is absent from the bundle unless you pass it.
They are not placeholders you can leave for the reviewer: a `REPLACE_WITH_*` in a `cidr:` field is not
a CIDR, the API server rejects the object, and the whole bundle stops applying (V-CMP-003). Omitting a
rule you needed produces a narrower policy and a connection that fails loudly; stubbing it produced a
bundle that could not be applied at all. GitHub is not a flag — its four published IPv4 blocks are
fixed in the egress template, the same for every tenant.

`--kube-apiserver-cidrs` is unbracketed above because it is the one flag here whose omission is **not**
the conservative choice. Egress rule 9 is what lets the tenant agent reach the API server at all —
without it every kubectl-shaped skill fails, and the broker can neither TokenReview the agent nor write
its ActionRecord, which surfaces as an authentication error that never mentions the network. It has no
default because there is nothing to default to: the address is per-cluster, and on a public GKE endpoint
it is a bare IP nobody publishes. The installer resolves it from the cluster at apply time; this bundle
is applied by the customer's CI/CD instead, so you must supply it. Read it off the target cluster with
`kubectl get service kubernetes -n default -o jsonpath='{.spec.clusterIP}'` plus the control-plane
endpoint, as `<ip>/32` each. If you cannot, say so when you report — a reviewer adding rule 9 by hand is
a worse outcome than a reviewer being told it is missing, and both beat a silently unreachable agent.

`--workload-identity` is the one widening you must match to the cluster rather than to the tenant. On a
GKE cluster with Workload Identity the tenant agent has **no cloud identity at all** without it; on a
cluster without WI it makes the raw node service account reachable. `--gke-dataplane` narrows the
IP↔port pairing once the cluster's dataplane is known (the two pairings are not interchangeable, and
the wrong one fails as an authentication timeout that never mentions the network).

The `--team-lead-chat-id` is the one value that still has a placeholder default: it applies cleanly and
matches no user, so the agent is unreachable until someone fills it in — reviewable and fail-closed.
Prefer to supply real values you already know (project, cluster, namespace, location, the team lead's
Chat ID). The script prints exactly which files it wrote.

The four isolation manifests it emits (`10`/`20`/`30`/`40`) are the **same bytes** the installer applies
from `k8s-operator/scripts/*.template`; `dev/test_skill_templates.py` fails if they ever differ.
Do not hand-edit the assets — edit the installer template and regenerate.

### Step 2: Verify what you rendered

- **Read-only, namespaced identity:** the grant in `50-developer-team-identity.yaml` is a **`Role`**
  (never a `ClusterRole`) with only `get/list/watch`. A `ClusterRole` labeled `tier=developer-team` is a
  wrong-scope grant the VAP hard-denies; write verbs are denied too. Get it right here — do not rely on
  the VAP.
- **Placement clause (load-bearing):** `60-developer-team-agent.yaml` has
  `metadata.namespace == spec.scope.namespace == <namespace>` — the admission webhook rejects the CR
  otherwise, and this is what keeps the pod inside the namespace's isolation controls.
- **Closed allowlist:** `integration.googleChat.allowedUsers` is non-empty and names the intended
  human(s). An empty allowlist means the router refuses everyone (fail-closed) — and the webhook rejects
  an enabled integration with an empty list.
- **Scope + parent:** `scope.projectId`/`scope.clusterName`/`scope.namespace` match the target, and
  `parentRef` names this Cluster Admin Agent (`cluster-admin-<cluster>`).
- **Isolation intact:** the default-deny NetworkPolicy is present (20), the egress policy has no
  `0.0.0.0/0` rule and does not allow the metadata server (30), and the ExternalName aliases match the
  ports the controller renders (litellm :80, minter :8080) (40).
- **No placeholders left** in anything the pipeline must apply (or they are explicitly flagged when you
  report).

### Step 3: Report the bundle — you do not submit it

**There is no submission step in this skill, and you must not invent one.** The `submit-suggestion`
skill that used to take the rendered tree and open a Pull Request has been **deleted from every tier**,
and the envelope path that replaces it is P11-T4 (see "Why this still renders a bundle" above). You
have no write credential of any kind: no `git` push, no `gh pr create`, no `kubectl apply`.

What you do instead is report, precisely enough that a human can act without re-deriving anything:

- **Where the bundle is** — the exact file list the render helper printed, and nothing you did not write.
- **What it provisions** — the namespace, the tier, and that the identity in it is a read-only,
  namespaced `Role`.
- **What is unfinished** — the `--team-lead-chat-id` placeholder if you left it, and any egress widening
  you could not resolve (`--kube-apiserver-cidrs` above all), named individually.
- **What has to happen next, and by whom** — a human reviews the bundle, takes it into the GitOps repo,
  and merges it; CI/CD applies the `namespaces/` wave in numeric order. Nothing is running until then.

Never describe the child as created, provisioned, or requested. It is rendered on disk, and that is
the whole of what happened.

## Best Practices

1. **Nothing takes effect without a human** — the bundle is desired state; a person reviews the
   identity + allowlist + isolation before anything runs.
2. **Least privilege in the cloud too** — for true cloud-side namespace isolation, give the tenant a
   per-namespace viewer GSA (or an IAM Condition scoping it to the namespace) so "reads only its own
   namespace" holds in the cloud, not just in RBAC (flagged in the rendered identity file).
3. **Tune the quota per tenant** — the rendered `ResourceQuota` ships sane defaults; size it to the
   tenant's real footprint before merge.
4. **One agent per namespace** — if the namespace already has a developer-team agent, edit its CR; don't
   render a duplicate (the webhook will reject it anyway).
5. **Keep pins immutable** — repin the agent image to a content-addressable digest
   (`…/developer-team-agent@sha256:…`) for production.
