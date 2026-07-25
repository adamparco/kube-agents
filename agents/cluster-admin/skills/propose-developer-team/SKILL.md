---
name: propose-developer-team
description: Provision a read-only Developer Team Agent for a namespace (the F4 cascade, one tier down) — render the full per-namespace GitOps bundle (Agent CR + pre-created read-only identity + isolation manifests + egress policy) and open it as a reviewed Pull Request. Never mints privilege at runtime; ships no bootstrap/VAP waves.
---

# propose-developer-team — Provision a Developer Team Agent (F4 cascade, one tier down)

This skill equips the **Cluster Admin Agent** to provision a subordinate **Developer Team Agent** for a
namespace in its cluster — the **F4 provisioning cascade** one layer below the platform→cluster-admin
step (05 §5). The Cluster Admin Agent is READ-ONLY: it does not create the agent, its identity, the
namespace, or any RBAC at runtime. It **renders a declarative bundle** and opens a **GitOps Pull
Request**; a human reviews and merges; the customer's CI/CD applies it in the `namespaces/` wave and the
kube-agents controller reconciles the pod **bound to the pre-created ServiceAccount** (08 §4).

## When to Use

- A namespace (tenant) in this cluster needs its own read-only developer-team persona — for auditing
  its workloads and proposing changes to that one namespace via GitOps.
- **One developer-team agent per namespace** (the admission webhook enforces cardinality on the
  `(tier, scope)` key, which for this tier is derived from `scope.namespace`). If one already exists for
  the namespace, edit its CR instead of proposing a new one (the webhook will reject a duplicate).

> **You never run `kubectl`/`gcloud`/cloud mutations, and you never mint an identity at runtime.** The
> only write path is this reviewed PR actuated by CI/CD. The bundle ships the read-only identity as
> _desired state_ (pre-created by the pipeline), never created by the controller.

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
./skills/propose-developer-team/scripts/render_developer_team.py \
  --cluster <cluster> \
  --namespace <namespace> \
  --project-id <gcp-project-id> \
  --location <region> \
  --team-lead-chat-id users/<CHAT_USER_ID> \
  [--workload-identity [--gke-dataplane auto|v1|v2]] \
  [--hub-inference-cidr <hub LiteLLM private CIDR>] \
  [--hub-minty-cidr <hub Minty private CIDR>] \
  [--mcp-cidrs <MCP grounding CIDRs>] \
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

`--workload-identity` is the one widening you must match to the cluster rather than to the tenant. On a
GKE cluster with Workload Identity the tenant agent has **no cloud identity at all** without it; on a
cluster without WI it makes the raw node service account reachable. `--gke-dataplane` narrows the
IP↔port pairing once the cluster's dataplane is known (the two pairings are not interchangeable, and
the wrong one fails as an authentication timeout that never mentions the network).

The `--team-lead-chat-id` is the one value that still has a placeholder default: it applies cleanly and
matches no user, so the agent is unreachable until someone fills it in — reviewable and fail-closed.
Prefer to supply real values you already know (project, cluster, namespace, location, the team lead's
Chat ID). The script prints exactly which files it wrote — stage **only** those.

The four isolation manifests it emits (`10`/`20`/`30`/`40`) are the **same bytes** the installer applies
from `k8s-operator/scripts/*.template`; `local-dev/test_skill_templates.py` fails if they ever differ.
Do not hand-edit the assets — edit the installer template and regenerate.

### Step 2: Verify before proposing

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
- **No placeholders left** in anything the pipeline must apply (or they are explicitly flagged for the
  reviewer).

### Step 3: Open the PR via submit-suggestion

Hand the rendered tree to [submit-suggestion](../submit-suggestion/SKILL.md) on a branch in **your**
tier's namespace (`cluster-admin-agent/…`, since you are the proposer), staging **only** the rendered
files (never `git add .`):

```bash
git checkout -b cluster-admin-agent/provision-developer-team-<namespace>
git add clusters/<cluster>/namespaces/<namespace>
git commit -m "feat(<namespace>): provision read-only developer-team agent + isolation manifests"
./skills/submit-suggestion/scripts/submit_suggestion.py \
  --tier cluster-admin \
  --branch "cluster-admin-agent/provision-developer-team-<namespace>" \
  --title "Provision developer-team agent for <namespace>" \
  --body "F4 cascade (one tier down): read-only Developer Team Agent for <namespace> (Agent CR + pre-created namespaced read-only identity + per-namespace isolation manifests + default-deny egress). No bootstrap/VAP waves — the cluster already has them. Applied by CI/CD after review; the controller binds the pod to the pre-created SA and mints no RBAC. Fill any REPLACE_WITH_* placeholders before merge."
```

You propose as `--tier cluster-admin` (the branch namespace is scoped to your tier). The **developer-team
tier itself** is a recognized proposer too (`submit_suggestion.py --tier developer-team`, branch
`developer-team-agent/…`) — that is the leaf agent's own write path for proposing changes to its
namespace once it is running, not this cascade. Record and return the PR URL. The agent starts only
after a human merges and the pipeline applies the bundle.

## Best Practices

1. **Everything through a PR** — the bundle is desired state; a human reviews the identity + allowlist +
   isolation before anything runs.
2. **Least privilege in the cloud too** — for true cloud-side namespace isolation, give the tenant a
   per-namespace viewer GSA (or an IAM Condition scoping it to the namespace) so "reads only its own
   namespace" holds in the cloud, not just in RBAC (flagged in the rendered identity file).
3. **Tune the quota per tenant** — the rendered `ResourceQuota` ships sane defaults; size it to the
   tenant's real footprint before merge.
4. **One agent per namespace** — if the namespace already has a developer-team agent, edit its CR; don't
   propose a duplicate (the webhook will reject it anyway).
5. **Keep pins immutable** — repin the agent image to a content-addressable digest
   (`…/developer-team-agent@sha256:…`) for production.
