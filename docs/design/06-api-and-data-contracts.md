# Design 06: API & Data Contracts

**Status:** ✅ Agreed

**Overview:** [README.md](README.md) · **Depends on:** 01–05 · **Tier:** Buildable (bridging)

---

## TL;DR

The exact interfaces a builder implements against, for a system whose agents **act**:

- the **`Agent` CRD** (`kubeagents.x-k8s.io/v1alpha1`) — tier / scope / parentRef / harness /
  integration / deployment, **plus** the imperative additions: `spec.operations.paused` (the
  brake), an **approval-roster** reference, stricter-only **`ChangePolicy`** references, and action
  status (§1);
- the **identity contract** — the **reader/actor split** ([03](03-security-model.md) §3.1): two
  ServiceAccounts per agent, with literal per-tier RBAC templates for all six identities and their
  cloud IAM mapping (§2);
- the **ChatOps addressing & routing** contract, extended with the brake commands (§2b);
- the **journal & IaC-mirror repo layout** — the repo is a **mirror, not a control path** (§3);
- **the action contracts** — the **Action Envelope** an agent submits, the deterministic **risk
  classifier** and `ChangePolicy`, the **`ActionRecord`** with its **undo plan**, and the
  **pause / freeze / undo / contested** brake objects (§4 — the centrepiece);
- the **OKF** knowledge schema (§5), **session** state keys (§6), the **agent mesh** RPC (§7), the
  **audit & attribution** chain (§8), and the **MCP tool surface**, now write-capable **only as
  envelope builders** (§9).

Namespace convention `kubeagents-system`; all agent labels/annotations use the `kube-agents/`
prefix; API group `kubeagents.x-k8s.io`, version `v1alpha1`.

> **Reading order for an implementer.** §1 → §2 gives you the objects and identities to provision;
> §4 gives you the write path. Build §4 before granting anything in §2's actor column
> ([01](01-vision-scope.md) §6, ordering constraint).

---

## 1. Agent definition — the `Agent` CRD (per persona)

Each agent is defined by one instance of a single, tier-discriminated **`Agent` custom resource**
(`kubeagents.x-k8s.io/v1alpha1`), reconciled by the **kube-agents controller** into an isolated
agent pod **and its Action Broker** ([05](05-system-architecture.md) C1/C-AB,
[08](08-agent-runtime-and-identity.md) §2). The CR selects the **Hermes** harness with the persona's
profile/skills, carries the tier/scope/parent metadata, names the pod's **reader** identity, and
declares the **operational envelope** — pause state, approval roster, change policies, initiative
budget — under which its broker executes.

**The CRD is not an authority-granting surface** ([03](03-security-model.md) §3.4). It carries no
field that can grant RBAC, widen scope, or loosen risk classification. Every field that touches
safety is **stricter-only**: it can tighten the code floor and can never move below it.

### 1.1 CR shape

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: Agent
metadata:
  name: developer-team-team-x # <tier>-<scope-leaf> by convention
  namespace: team-x # developer-team tier MUST be created in its scoped namespace (§1.2)
spec:
  # ---- identity of the agent (immutable discriminators) --------------------------------------
  tier: developer-team # platform | cluster-admin | developer-team. Immutable (CEL + webhook).
  scope:
    projectId: my-project # all tiers
    clusterName: cluster-a # cluster-admin + developer-team
    namespace: team-x # developer-team only
  parentRef:
    name: cluster-admin-cluster-a # required for non-platform tiers

  # ---- runtime -------------------------------------------------------------------------------
  harness: # struct, not a string (k8s-operator/api/v1alpha1/common_types.go)
    clusterName: cluster-a
    location: us-central1
    projectId: my-project
    hermes: { dashboardEnabled: true, agentHome: /opt/data }
    memory: { memoryEnabled: true, provider: multiuser_memory }
  deployment:
    image: us-docker.pkg.dev/my-project/kube-agents/developer-team-agent
    tag: v1.4.0
    runtimeClassName: null # gVisor sandbox — deferred (08 §5.1)
  security:
    serviceAccountName: developer-team-agent # the READER SA (§2). Read-only. On the agent pod.

  # ---- operational envelope (NEW — the imperative model) -------------------------------------
  operations:
    paused: false # THE BRAKE (03 §6). Broker refuses new envelopes when true.
    pauseReason: "" # free text, set with paused; surfaced in chat and status
    dryRunOnly: false # shadow mode: classify+journal, never execute. Stricter-only.
    approvalRosterRef:
      name: team-x-approvers # ApprovalRoster consulted for `gated` actions (§4.4)
    changePolicyRefs: # stricter-only classification overlays (§4.2). Ordered, all applied.
      - name: baseline-conservative
      - name: team-x-pci
    initiativeBudget: # caps only; CLAMPED to the code ceiling, never above it (04 §4.2)
      actionsPerHour: 30 # code ceiling 200
      actionsPerDay: 200 # code ceiling 2000
      maxObjectsPerAction: 25 # code ceiling 100
      flapWindow: 30m # code floor 5m (a shorter window is rejected)
      flapThreshold: 3 # code ceiling 5
    notifyOn: elevated # routine | elevated | gated — minimum class that pings humans at once

  # ---- integrations --------------------------------------------------------------------------
  integration:
    github:
      gitRepo: https://github.com/acme/gitops
      mirror: # write-behind IaC/journal mirror (§3). NOT a control path.
        enabled: true
        mode: both # state | log | both
        branch: main
    googleChat:
      enabled: true
      projectId: my-project
      topicName: kage-chat
      subscriptionName: kage-chat-sub
      allowedUsers: ["users/1234567890"] # closed allowlist — required when enabled (§1.2)
    slack:
      enabled: false
      allowedUsers: []
status:
  phase: Ready # Pending | Provisioning | Ready | Degraded | Paused | Failed
  address: developer-team-team-x.team-x.svc.cluster.local
  lastReconcileTime: "2026-07-24T18:02:11Z"
  conditions: [] # Ready, BrokerReady, JournalReachable, BudgetExhausted, Frozen, Paused
  deploymentStatus: { name: developer-team-team-x, readyReplicas: 1 }
  serviceStatus: { endpoint: https://developer-team-team-x.team-x.svc:8444 }
  storageStatus: { bound: true }

  # ---- action-pipeline status (NEW) ----------------------------------------------------------
  operations:
    paused: false
    pausedSince: null
    pausedBy: "" # chat user id or K8s username that set the brake
    reason: ""
    dryRunOnly: false
    frozenBy: "" # name of the FleetFreeze covering this scope, if any (§4.4)
  broker:
    endpoint: https://developer-team-team-x-broker.team-x.svc:8443
    actorServiceAccount: developer-team-team-x-actor
    ready: true
    journalReachable: true # false ⇒ the broker is fail-closed and executing nothing
  lastAction:
    actionId: 01J8Z2K9Q7V3X5M6N8P0R2T4W6
    name: ar-01j8z2k9q7v3x5m6n8p0r2t4w6
    riskClass: routine
    status: Verified
    intent: restart crash-looping deployment api-gateway
    target: apps/v1/Deployment team-x/api-gateway
    completionTime: "2026-07-24T17:58:44Z"
  pendingApprovals: 1 # count of ActionRecords in PendingApproval for this agent
  pendingApprovalRefs: # capped at 8 most recent; the journal is authoritative
    - ar-01j8z3a1b2c3d4e5f6g7h8j9k0
  budget:
    windowStart: "2026-07-24T17:00:00Z"
    actionsInWindow: 7
    actionsRemaining: 23
    exhausted: false
    cooldownUntil: null
  counters:
    actions24h: 41
    undone24h: 1
    contestedTargets: 1
    forbiddenAttempts24h: 0
    verificationFailures24h: 0
```

**No field names the actor.** `spec.security.serviceAccountName` names the **reader** SA only, and
V-10 constrains it to the tier template's name. The **actor** SA is _derived_ from `tier` + `scope`
(§2) and looked up by name — the CRD has, and must never gain, a `brokerServiceAccountName` /
`actorServiceAccountName` / equivalent. The ability to name the actor identity is the ability to
point a broker at a more privileged one, which is exactly the self-escalation
[03](03-security-model.md) §3.3 and §3.4 exist to make unrepresentable
([08](08-agent-runtime-and-identity.md) §2.1). The controller publishes the resolved name in
`status.broker.actorServiceAccount` — **status, not spec**: observable, not settable.

**Field provenance.** `tier` / `scope` / `parentRef` / `harness` / `deployment` / `security` /
`integration` and the base `status` fields exist today in
`k8s-operator/api/v1alpha1/{agent_types.go,common_types.go}` — keep their names and nesting.
`spec.operations` and `status.{operations,broker,lastAction,pendingApprovals,budget,counters}` are
**new** and are what the imperative model adds. `spec.iac.format` (`kcc` | `terraform`) survives but
its meaning narrows: it now selects the **mirror** artifact format (§3), not the actuation artifact.

**Retired.** Nothing in the CRD refers to proposals, suggestions, branches, or PRs. The
`submit-suggestion` propose path is gone (§9).

### 1.2 Per-tier field usage, cardinality & validation

| `tier`           | Required `spec.scope`                   | `parentRef`             | `metadata.namespace`    | Cardinality     | Reader SA              | Actor SA                           |
| ---------------- | --------------------------------------- | ----------------------- | ----------------------- | --------------- | ---------------------- | ---------------------------------- |
| `platform`       | `projectId`                             | — (root)                | `kubeagents-system`     | 1 per project   | `platform-agent`       | `platform-<project>-actor`         |
| `cluster-admin`  | `projectId`, `clusterName`              | parent = platform agent | `kubeagents-system`     | 1 per cluster   | `cluster-admin-agent`  | `cluster-admin-<cluster>-actor`    |
| `developer-team` | `projectId`, `clusterName`, `namespace` | parent = cluster-admin  | **= `scope.namespace`** | 1 per namespace | `developer-team-agent` | `developer-team-<namespace>-actor` |

**Validation rules (all v1, all enforced — not conventions).** V-1…V-5 exist today in
`k8s-operator/internal/webhook/agent_webhook.go`; **V-6 is new and required by the inversion**
([03](03-security-model.md) §4.2).

| #        | Rule                                                                                                                                                                                                                         | Where                                                                                     | Failure                               |
| -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- | ------------------------------------- |
| **V-1**  | `spec.tier` ∈ {`platform`,`cluster-admin`,`developer-team`}; **immutable** after create                                                                                                                                      | CRD enum + CEL `self == oldSelf` + webhook                                                | `Invalid`                             |
| **V-2**  | Per-tier required `scope` fields present (table above)                                                                                                                                                                       | validating webhook                                                                        | `Required`                            |
| **V-3**  | `parentRef.name` present for non-platform tiers                                                                                                                                                                              | validating webhook                                                                        | `Required`                            |
| **V-4**  | **Developer-team placement:** `metadata.namespace == spec.scope.namespace`                                                                                                                                                   | validating webhook                                                                        | `Invalid`                             |
| **V-5**  | **`(tier, scope)` cardinality:** exactly one non-terminating `Agent` per identity key                                                                                                                                        | validating webhook (cluster-wide `List`)                                                  | `Duplicate`                           |
| **V-6**  | **Cross-object ceiling — NEW, v1:** the child's scope must be a **strict subset** of `parentRef`'s scope, and the parent's tier must be the tier immediately above the child's                                               | validating webhook (reads the parent CR)                                                  | `Invalid`                             |
| **V-7**  | **Closed allowlist:** an enabled chat integration must carry a non-empty `allowedUsers`                                                                                                                                      | CRD CEL + validating webhook                                                              | `Required`                            |
| **V-8**  | **Budget clamp:** `initiativeBudget` values above the code ceiling (or windows below the code floor) are **rejected**, not silently clamped                                                                                  | validating webhook                                                                        | `Invalid`                             |
| **V-9**  | **No authority fields:** the schema is closed; an unknown field under `spec` — in particular anything named `rbac`, `rules`, `riskClass`, `allow`, `bypass`, `scopeOverride` — is pruned/refused                             | CRD structural schema (`x-kubernetes-preserve-unknown-fields` is **never** set on `spec`) | field pruned; CI test asserts absence |
| **V-10** | **Reader-only SA override:** `spec.security.serviceAccountName` may name only the **reader** SA and must match the tier template pattern `^<tier>-agent$`. There is **no** field anywhere in the CRD that names the actor SA | validating webhook                                                                        | `Invalid`                             |

**V-6 in detail** (the difference between "a parent cannot express an over-grant" and "a parent
cannot cause one"). For a candidate child `C` with parent `P`:

```text
tier(P) must be the immediate parent tier of tier(C)
  platform → cluster-admin → developer-team

scope(C) ⊂ scope(P), evaluated field-wise:
  C.projectId   == P.projectId                                   (always)
  C.clusterName == P.clusterName    when P.tier == cluster-admin
  C.namespace   != ""               when C.tier == developer-team
and scope(C) != scope(P)                                          (strict subset)

P must not be terminating, and P.spec.operations.paused must be false
  (a paused parent may not provision — the brake covers provisioning too)
```

Rejection is `Invalid` on `spec.parentRef.name` with the offending comparison in the message. The
same predicate is reused by the broker when classifying a child-provisioning envelope (§4.2) and by
`vap-agent-scope` when the child's RBAC objects are submitted ([03](03-security-model.md) §4.2), so
the ceiling is checked three times by three mechanisms that fail independently.

**Cardinality key.** `identity = tier + "/" + projectId [+ "/" + clusterName [+ "/" + namespace]]`,
computed by `internal/agentindex.ScopeIdentity`. The same function keys the ChatOps routing index
(§2b) and the actor SA name derivation (§2), so routing, identity, and cardinality can never drift.

---

## 2. Identity contract — the reader / actor split

Every `Agent` CR is served by **two** ServiceAccounts ([03](03-security-model.md) §3.1). This is
the single most load-bearing contract in the document: it is what makes "the LLM holds no write
credential" a structural fact rather than a policy.

| Identity   | SA name                | Namespace                                | Held by        | Authority                                                   | Labels                                                                             |
| ---------- | ---------------------- | ---------------------------------------- | -------------- | ----------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| **Reader** | `<tier>-agent`         | `kubeagents-system` (dev-team: its `ns`) | The agent pod  | `get`/`list`/`watch` within scope. **No write verb, ever.** | `kube-agents/tier: <tier>`, `kube-agents/role: reader`                             |
| **Actor**  | `<tier>-<scope>-actor` | same as the reader                       | The broker pod | Scoped read-write minus the forbidden set                   | `kube-agents/tier: <tier>`, `kube-agents/role: actor`, `kube-agents/scope: <leaf>` |

`<scope>` is the tier's scope **leaf**: project (platform), cluster (cluster-admin), namespace
(developer-team). If `<tier>-<scope>-actor` exceeds 253 characters the leaf is truncated to 40
characters and suffixed with the first 8 hex digits of `sha256(identity)`; the controller records
the resolved name in `status.broker.actorServiceAccount`.

**Provisioning rules (unchanged in shape from the read-only generation).**

1. Identity derives from `tier` + `scope` **alone**. The CR requests nothing.
2. The controller **references** these SAs by name (pod `serviceAccountName`); it **mints no RBAC**
   at runtime. The reader SA comes from `spec.security.serviceAccountName`; the actor SA name is
   **derived**, not configurable — a CR cannot point its broker at a different identity.
3. RBAC objects are rendered from a **constant per-tier template** by the render overlay
   (`policy/rbac-overlay/<tier>.yaml`) and applied out-of-band, or by a **parent** provisioning a
   child (§4.2 of [03](03-security-model.md)). The parent supplies only `(tier, scope, parent)`.
4. `kube-agents/role` is the label `vap-agent-scope` selects on. A reader-labelled SA bound to any
   rule containing a write verb is **denied at admission**, independently of who submits it.

### 2.1 Reader templates (3 tiers)

Read-only, identical in shape to the shipped `examples/gitops-repo/policy/rbac-overlay/*.yaml`;
the only change is the added `kube-agents/role: reader` label.

```yaml
# platform reader — cluster-wide read, plus CRD and provisioning-CR read.
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: platform-agent-explorer
  labels: { kube-agents/tier: platform, kube-agents/role: reader }
rules:
  - apiGroups:
      [
        "",
        apps,
        batch,
        networking.k8s.io,
        rbac.authorization.k8s.io,
        autoscaling,
        policy,
      ]
    resources: ["*"]
    verbs: [get, list, watch]
  - apiGroups: [apiextensions.k8s.io]
    resources: [customresourcedefinitions]
    verbs: [get, list, watch]
  - apiGroups: [kubeagents.x-k8s.io]
    resources:
      [agents, actionrecords, changepolicies, approvalrosters, fleetfreezes]
    verbs: [get, list, watch]
  - apiGroups: ["*.cnrm.cloud.google.com"] # KCC provisioning CRs, where the customer runs them
    resources: ["*"]
    verbs: [get, list, watch]
---
# cluster-admin reader — cluster-wide read within its one cluster (the cluster IS the boundary).
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: cluster-admin-agent-explorer
  labels: { kube-agents/tier: cluster-admin, kube-agents/role: reader }
rules:
  - apiGroups:
      [
        "",
        apps,
        batch,
        networking.k8s.io,
        rbac.authorization.k8s.io,
        autoscaling,
        policy,
        storage.k8s.io,
      ]
    resources: ["*"]
    verbs: [get, list, watch]
  - apiGroups: [kubeagents.x-k8s.io]
    resources:
      [agents, actionrecords, changepolicies, approvalrosters, fleetfreezes]
    verbs: [get, list, watch]
---
# developer-team reader — NAMESPACED Role. A ClusterRole labelled tier=developer-team is a
# wrong-scope grant and is denied by vap-agent-scope.
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: developer-team-agent-explorer
  namespace: team-x
  labels: { kube-agents/tier: developer-team, kube-agents/role: reader }
rules:
  - apiGroups: ["", apps, batch, networking.k8s.io, autoscaling, policy]
    resources: ["*"]
    verbs: [get, list, watch]
  - apiGroups: [kubeagents.x-k8s.io]
    resources: [actionrecords]
    verbs: [get, list, watch]
```

**Universal reader prohibitions** (asserted by test, not just by omission): no verb outside
`get`/`list`/`watch`; no `escalate`/`bind`/`impersonate`; no `create` on `subjectaccessreviews`
(that belongs to the deferred §2a); no access to `secrets` **write**; no `pods/exec`,
`pods/attach`, `pods/portforward`.

### 2.2 Actor templates (3 tiers)

These are the literal rule bodies the render overlay emits and `vap-agent-scope` validates against.
A rule not present here is not grantable to an actor identity.

```yaml
# ---------------------------------------------------------------------------------------------
# developer-team ACTOR — namespaced Role in its one namespace. The narrowest identity in the system.
# ---------------------------------------------------------------------------------------------
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: developer-team-team-x-actor
  namespace: team-x
  labels:
    {
      kube-agents/tier: developer-team,
      kube-agents/role: actor,
      kube-agents/scope: team-x,
    }
rules:
  - apiGroups: [""]
    resources:
      [
        pods,
        services,
        configmaps,
        secrets,
        serviceaccounts,
        persistentvolumeclaims,
        endpoints,
      ]
    verbs: [get, list, watch, create, update, patch, delete]
  - apiGroups: [""]
    resources: [pods/log, pods/status, events]
    verbs: [get, list, watch]
  - apiGroups: [""]
    resources: [pods/eviction]
    verbs: [create]
  - apiGroups: [apps]
    resources: [deployments, statefulsets, daemonsets, replicasets]
    verbs: [get, list, watch, create, update, patch, delete]
  - apiGroups: [apps]
    resources: [deployments/scale, statefulsets/scale, replicasets/scale]
    verbs: [get, update, patch]
  - apiGroups: [batch]
    resources: [jobs, cronjobs]
    verbs: [get, list, watch, create, update, patch, delete]
  - apiGroups: [autoscaling]
    resources: [horizontalpodautoscalers]
    verbs: [get, list, watch, create, update, patch, delete]
  - apiGroups: [policy]
    resources: [poddisruptionbudgets]
    verbs: [get, list, watch, create, update, patch, delete]
  - apiGroups: [networking.k8s.io]
    resources: [ingresses, networkpolicies]
    verbs: [get, list, watch, create, update, patch, delete]
  - apiGroups: [gateway.networking.k8s.io]
    resources: [httproutes, grpcroutes]
    verbs: [get, list, watch, create, update, patch, delete]
# NOT GRANTED, deliberately: rbac.authorization.k8s.io (any verb) — a namespace agent may not author
# RoleBindings, because a RoleBinding is the one namespaced object that can name an agent identity
# (03 §3.3 rule 1). Also not granted: resourcequotas/limitranges writes (the cluster-admin tier owns
# them — a tenant cannot raise its own quota), pods/exec, pods/attach, pods/portforward, and any
# cluster-scoped resource whatsoever.
```

```yaml
# ---------------------------------------------------------------------------------------------
# cluster-admin ACTOR — ClusterRole, bounded to its one cluster by *being installed only there*.
# ---------------------------------------------------------------------------------------------
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: cluster-admin-cluster-a-actor
  labels:
    {
      kube-agents/tier: cluster-admin,
      kube-agents/role: actor,
      kube-agents/scope: cluster-a,
    }
rules:
  # Tenancy: namespaces and their guardrails.
  - apiGroups: [""]
    resources:
      [
        namespaces,
        resourcequotas,
        limitranges,
        serviceaccounts,
        configmaps,
        secrets,
      ]
    verbs: [get, list, watch, create, update, patch, delete]
  - apiGroups: [networking.k8s.io]
    resources: [networkpolicies, ingresses, ingressclasses]
    verbs: [get, list, watch, create, update, patch, delete]
  # Tenant RBAC + child (developer-team) identity provisioning. NAMESPACED RBAC ONLY.
  - apiGroups: [rbac.authorization.k8s.io]
    resources: [roles, rolebindings]
    verbs: [get, list, watch, create, update, patch, delete]
  # Workloads across the cluster (add-ons and tenant remediation).
  - apiGroups: ["", apps, batch, autoscaling, policy]
    resources: ["*"]
    verbs: [get, list, watch, create, update, patch, delete]
  # Nodes: cordon/label/drain. No node CREATE (that is a node-pool operation, below).
  - apiGroups: [""]
    resources: [nodes]
    verbs: [get, list, watch, update, patch, delete]
  - apiGroups: [""]
    resources: [pods/eviction]
    verbs: [create]
  - apiGroups: [storage.k8s.io]
    resources: [storageclasses, csidrivers, volumeattachments]
    verbs: [get, list, watch, create, update, patch, delete]
  # Node pools for its own cluster, via KCC.
  - apiGroups: [container.cnrm.cloud.google.com]
    resources: [containernodepools]
    verbs: [get, list, watch, create, update, patch, delete]
  # Provisioning its children (developer-team Agent CRs). The child ⊆ parent ceiling is enforced by
  # the webhook (V-6), NOT by RBAC — RBAC cannot express "children only".
  - apiGroups: [kubeagents.x-k8s.io]
    resources: [agents]
    verbs: [get, list, watch, create, update, patch, delete]
  - apiGroups: [kubeagents.x-k8s.io]
    resources: [actionrecords]
    verbs: [get, list, watch]
# NOT GRANTED: clusterroles, clusterrolebindings (a developer-team child needs neither — its
# template is namespaced); admissionregistration.k8s.io; apiextensions.k8s.io; container-cluster
# lifecycle (containerclusters — that is the platform tier); anything in the platform's cloud scope.
```

```yaml
# ---------------------------------------------------------------------------------------------
# platform ACTOR — ClusterRole on the hub/management cluster + the project cloud identity.
# ---------------------------------------------------------------------------------------------
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: platform-my-project-actor
  labels:
    {
      kube-agents/tier: platform,
      kube-agents/role: actor,
      kube-agents/scope: my-project,
    }
rules:
  # Cluster + node-pool lifecycle, and project cloud resources, via KCC.
  - apiGroups: [container.cnrm.cloud.google.com]
    resources: [containerclusters, containernodepools]
    verbs: [get, list, watch, create, update, patch, delete]
  - apiGroups:
      [
        compute.cnrm.cloud.google.com,
        iam.cnrm.cloud.google.com,
        monitoring.cnrm.cloud.google.com,
      ]
    resources: ["*"]
    verbs: [get, list, watch, create, update, patch, delete]
  # Fleet policy for TENANTS (not for agents, and not the kube-agents VAPs).
  - apiGroups: [constraints.gatekeeper.sh, templates.gatekeeper.sh, kyverno.io]
    resources: ["*"]
    verbs: [get, list, watch, create, update, patch, delete]
  # Provisioning its children (cluster-admin Agent CRs) and their identities.
  - apiGroups: [kubeagents.x-k8s.io]
    resources: [agents]
    verbs: [get, list, watch, create, update, patch, delete]
  - apiGroups: [rbac.authorization.k8s.io]
    resources: [roles, rolebindings, clusterroles, clusterrolebindings]
    verbs: [get, list, watch, create, update, patch, delete]
  - apiGroups: [""]
    resources: [namespaces, serviceaccounts, configmaps, secrets]
    verbs: [get, list, watch, create, update, patch, delete]
  - apiGroups: [kubeagents.x-k8s.io]
    resources: [actionrecords]
    verbs: [get, list, watch]
# NOT GRANTED: admissionregistration.k8s.io (validatingadmissionpolicies / -bindings —
# vap-agent-scope is control plane, 03 §3.3 rule 3); apiextensions.k8s.io (the Agent CRD itself);
# resourcemanager IAM roles that could bind a principal at project level to an agent GSA.
```

**Three properties the platform actor's RBAC rules depend on, spelled out because they are easy to
lose:**

1. **Kubernetes' built-in escalation prevention does the heavy lifting on the RBAC grants.** An
   actor SA may `create` a `ClusterRole`, but the API server refuses to let it create one carrying
   permissions the actor does not itself hold — unless it holds `escalate`, which is in the
   forbidden set and appears in **no** template. Likewise `RoleBinding` creation is refused without
   `bind` unless the creator holds every permission being bound. Attenuation is therefore enforced
   by the API server, by `vap-agent-scope`, and by webhook V-6 — three independent mechanisms.
2. **RBAC cannot express "not its own object".** Nothing in `agents` or `roles` grants above
   prevents an agent from patching **its own** `Agent` CR or a **parent's**. That exclusion is
   enforced by the broker (forbidden-set match, §4.2) and by `vap-agent-scope` (which denies any
   write to an `Agent` CR whose identity key equals or is an ancestor of the writer's).
3. **RBAC cannot exclude a namespace.** The cluster-admin actor's cluster-wide workload grant would
   otherwise reach `kube-system` and `kubeagents-system`. The protected-namespace carve-out
   ([03](03-security-model.md) §3.3 rule 6) is enforced by the broker and by `vap-agent-scope`,
   which denies writes into protected namespaces except a named add-on allowlist.

#### 2.2.1 Broker operations grant (all tiers, identical)

The three actor templates above cover what an agent **acts on**. They do not cover what the broker
needs to **run its own pipeline** — and without this block the system cannot satisfy invariant 3,
because the broker would have no permission to write the journal it is required to write. Every
actor identity additionally receives exactly this rule set, byte-identical across tiers:

```yaml
# Broker operations — appended verbatim to every actor Role/ClusterRole.
- apiGroups: [authentication.k8s.io] # step 1: authenticate the calling agent
  resources: [tokenreviews]
  verbs: [create]
- apiGroups: [kubeagents.x-k8s.io] # step 11: journal — the broker owns its own records
  resources: [actionrecords]
  verbs: [get, list, watch, create]
- apiGroups: [kubeagents.x-k8s.io]
  resources: [actionrecords/status]
  verbs: [get, update, patch]
- apiGroups: [kubeagents.x-k8s.io] # step 5: brake — MUST be readable by every tier
  resources: [fleetfreezes]
  verbs: [get, list, watch]
- apiGroups: [kubeagents.x-k8s.io] # step 5: its own pause state
  resources: [agents]
  verbs: [get, list, watch]
- apiGroups: [kubeagents.x-k8s.io] # steps 4 and 7: classification and approval inputs
  resources: [changepolicies, approvalrosters]
  verbs: [get, list, watch]
```

Three properties of this grant are load-bearing and are asserted separately (09 §6.14,
`V-BRK-013`):

- **`create` but never `update`/`delete` on `actionrecords`.** The broker appends to the journal and
  advances `status`; it can never rewrite or remove a record, including its own. Tampering with the
  journal stays in the forbidden set (§3.3 rule 4) for every identity without exception.
- **`fleetfreezes` is readable by _every_ tier.** A tier that cannot read the freeze object fails
  closed permanently (§4.4), so omitting this grant does not fail safe — it bricks the tier.
- **The grant is identical across tiers and is not scoped.** It confers no authority over tenant
  resources, so widening it does not widen an agent's reach; keeping it uniform means one rule set
  to review rather than three.

### 2.3 Cloud IAM mapping (Workload Identity)

One Google service account per identity, bound to the KSA by the standard
`iam.gke.io/gcp-service-account` annotation. Actor GSAs carry **IAM Conditions** pinning them to
their own scope — the cloud equivalent of the RBAC scope ceiling, and the only enforcement available
outside Kubernetes admission.

| Identity                  | GSA                                        | Roles                                                                                                                            | IAM condition                                                                                                 |
| ------------------------- | ------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| platform **reader**       | `kubeagents-platform-gsa`                  | `roles/viewer`, `roles/container.viewer`, `roles/monitoring.viewer`                                                              | project-scoped                                                                                                |
| platform **actor**        | `kubeagents-platform-actor-gsa`            | `roles/container.admin`, `roles/compute.networkAdmin`, `roles/monitoring.editor`, a custom role for the KCC resources it manages | `resource.project == "<project>"`                                                                             |
| cluster-admin **reader**  | `kubeagents-cluster-admin-<cluster>-gsa`   | `roles/container.viewer`, `roles/monitoring.viewer`                                                                              | `resource.name.startsWith("projects/P/locations/L/clusters/C")`                                               |
| cluster-admin **actor**   | `kubeagents-cluster-admin-<cluster>-actor` | custom role: `container.clusters.update`, `container.nodePools.*`, `container.operations.get`                                    | same `startsWith` condition — **one GSA per cluster, never one per project**                                  |
| developer-team **reader** | `kubeagents-devteam-<ns>-gsa`              | `roles/monitoring.viewer` (logs/metrics for its workloads)                                                                       | log-filter / label condition on the namespace                                                                 |
| developer-team **actor**  | **none in v1**                             | —                                                                                                                                | a namespace tier has no cloud write surface; add a narrowly-conditioned GSA only when a concrete need appears |

**Never granted to any actor GSA, at any tier:** `roles/owner`, `roles/editor`,
`roles/iam.securityAdmin`, `roles/resourcemanager.projectIamAdmin`,
`roles/iam.serviceAccountTokenCreator` on an agent GSA, or any role permitting
`iam.serviceAccounts.setIamPolicy` / `resourcemanager.projects.setIamPolicy`. These are the cloud
expression of [03](03-security-model.md) §3.3 rule 1, and their absence is a checked property, not a
convention (§10).

---

## 2a. User-authorization contract — DEFERRED hardening (down-scope to the requester)

Implements [03](03-security-model.md) §4a: a human request's effective authority becomes
**agent scope ∩ the requester's own permissions**, eliminating the confused deputy.

> **Deferred — not in v1.** v1 secures the human→agent boundary with trusted-human access
> (`allowedUsers`, checked before dispatch), the scope ceiling, and the gated class.

**What changed: the broker is now its natural host.** In the read-only generation this check had no
obvious home; the broker already (a) sits outside the LLM loop, (b) authenticates the caller, and
(c) resolves every target's scope per action — which is exactly the machinery a per-request
down-scope needs. The contract when it lands:

- the router issues a **signed requester assertion** (§4.1 `requester.assertion`) carrying the
  authenticated principal and groups; the broker verifies the signature rather than trusting the
  envelope's `requester` block;
- for each Kubernetes target the broker issues a `SubjectAccessReview` for the **requester** with
  the envelope's own verb/resource/namespace, and proceeds only on `status.allowed == true`;
- for each cloud target it calls `iam.testIamPermissions` for the requester's principal;
- a denial refuses the whole envelope (no partial application, matching §4.1's atomicity rule) and
  is journaled as `Rejected` with `reason: requester-unauthorized`;
- autonomous actions (watch / alert / cron triggers) have no requester and continue to run under
  the agent's own scope.

The broker needs `create` on `subjectaccessreviews`. That grant is **absent from every v1 template**
in §2.2 and is added only with this hardening.

---

## 2b. ChatOps addressing & routing contract

How a human names the agent they want ([02](02-agent-personas.md) §2.4). The **ChatOps gateway**
([05](05-system-architecture.md) C15) resolves every inbound message to exactly one `(tier, scope)`
`Agent` CR, checks that agent's allowlist, and dispatches.

**Handle grammar.** An agent's handle is `<tier>-<scope-leaf>`:

| Tier             | Canonical handle           | Short alias          | Resolves to `(tier, scope)` |
| ---------------- | -------------------------- | -------------------- | --------------------------- |
| `platform`       | `@platform-<project>`      | —                    | `(platform, project)`       |
| `cluster-admin`  | `@cluster-admin-<cluster>` | `@cluster-<cluster>` | `(cluster-admin, cluster)`  |
| `developer-team` | `@developer-team-<ns>`     | `@devteam-<ns>`      | `(developer-team, ns)`      |

Prefix matching is longest-first (`cluster-admin-` before `cluster-`); leaves are lower-cased and
must be RFC-1123 labels, refused rather than coerced
(`k8s-operator/internal/router/grammar.go`). The map is **derived** from the same `(tier, scope)`
key the cardinality webhook enforces (§1.2) — there is no separate routing registry to drift.

**Resolution order:** (1) slash command → (2) explicit `@handle` → (3) sticky thread affinity (§6)
→ (4) NL inference (fallback; low confidence ⇒ clarify, never guess). Only mode 4 spends an
inference call. **Routing is never an authz signal**: `Resolve()` only names a target;
`Authorize()` independently reads the **target** CR's `allowedUsers`. The gateway is **fail-closed**
— an empty or absent allowlist refuses everyone.

### 2b.1 Operational commands (new — the imperative model)

These are **control-plane commands, not agent conversation**. They are executed by the gateway and
the controller against Kubernetes objects and **must work with the LLM, the agent pod, and the
inference stack all unavailable** ([03](03-security-model.md) §6). The gateway never forwards them
to the agent for interpretation.

| Command                          | Effect                                                                                   | Object touched                                      | Authorized by                                                                |
| -------------------------------- | ---------------------------------------------------------------------------------------- | --------------------------------------------------- | ---------------------------------------------------------------------------- |
| `/<handle> pause [reason]`       | Broker refuses new envelopes immediately; in-flight action completes or rolls back       | `Agent.spec.operations.{paused,pauseReason}`        | the target agent's `allowedUsers`                                            |
| `/<handle> resume`               | Clears the brake. Does **not** clear `contested` markers or a `FleetFreeze`              | `Agent.spec.operations.paused`                      | the target agent's **approval roster** (stricter than pause, deliberately)   |
| `/freeze <scope> [reason] [ttl]` | Nothing executes anywhere in scope. Undo and rollback still work                         | creates a `FleetFreeze` (§4.4)                      | approval roster of the agent owning the scope, or its parent's roster        |
| `/thaw <freeze-name>`            | Deletes the `FleetFreeze`                                                                | `FleetFreeze`                                       | the roster that created it, or a parent's roster                             |
| `/undo <action-id> [reason]`     | Replays the recorded undo plan as a new, classified, journaled action                    | creates an `UndoRequest` (§4.4)                     | the owning agent's `allowedUsers`                                            |
| `/approve <action-id> [note]`    | Releases a `PendingApproval` action to execute                                           | `ActionRecord.status` via the approvals subresource | **approval roster only**; never the requester of the action (§4.4 four-eyes) |
| `/reject <action-id> [note]`     | Terminates a `PendingApproval` action as `Rejected`                                      | `ActionRecord.status`                               | approval roster only                                                         |
| `/<handle> status`               | Renders `Agent.status` — paused/frozen, budget, pending approvals, last action, counters | read-only                                           | the target agent's `allowedUsers`                                            |
| `/actions [--since] [--class]`   | Lists recent `ActionRecord`s for the scope with their undo handles                       | read-only                                           | the target agent's `allowedUsers`                                            |

**Two-tier authorization, stated plainly.** `allowedUsers` gates _talking to an agent and stopping
it_; the **approval roster** gates _letting it proceed_ and _relaxing a stop_. Anyone trusted enough
to use an agent is trusted enough to hit its brake — braking is always the safe direction. Releasing
the brake, approving a gated action, or thawing a freeze requires roster membership. `pause` and
`undo` are therefore deliberately the **most** widely available commands in the system.

**Equivalent non-chat paths (required, because chat may be the thing that is broken):**

```bash
kubectl patch agent developer-team-team-x -n team-x --type=merge \
  -p '{"spec":{"operations":{"paused":true,"pauseReason":"suspect rollout loop"}}}'

kubectl apply -f - <<'EOF'
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: FleetFreeze
metadata: { name: incident-4471 }
spec:
  scope: { projectId: my-project }        # everything in the project
  reason: "INC-4471 — payments degraded"
  expiresAt: "2026-07-24T22:00:00Z"
EOF

kubectl kage undo 01J8Z2K9Q7V3X5M6N8P0R2T4W6 --reason "wrong diagnosis"
```

**Attribution (extends §8).** Every chat turn's audit record carries the resolved agent
(`tier`, `scope`), the **routing mode** (`slash` | `handle` | `sticky` | `inference`), the
requester, the trace/session IDs, and — for the commands above — the **object mutated** and, for
`approve`, the `action-id` released.

---

## 3. Journal & IaC-mirror repository layout

**The repository is no longer a control path.** In the read-only generation the customer's GitOps
repo was the mutation mechanism: an agent pushed a branch, a human merged, CI applied. The Action
Broker replaces all three steps ([03](03-security-model.md) §4). What remains is a **write-behind
mirror** — valuable, optional, and deliberately powerless: **compromising the repo cannot cause a
cluster change** ([03](03-security-model.md) §2, [04](04-workflow-model.md) §6).

**What is retired** (delete these paths and their machinery, do not repurpose them):

| Retired                                                 | Why                                                     | Replaced by                             |
| ------------------------------------------------------- | ------------------------------------------------------- | --------------------------------------- |
| `submit-suggestion` skill and the propose branch naming | Agents execute; they do not propose                     | §4.1 Action Envelope                    |
| `.github/workflows/apply.yml` as the **applier**        | The broker applies, synchronously, in-cluster           | §4 broker pipeline                      |
| Branch protection + CODEOWNERS as the **approval gate** | Approval is a risk-class decision, not a merge decision | §4.2 classifier + §4.4 `ApprovalRoster` |
| `knowledge/escalation/` as the cross-tier message bus   | Agents call each other directly                         | §7 agent mesh                           |

**What survives, and what it is now for:**

```text
<gitops-repo>/
├── clusters/<cluster>/            # MIRROR of executed desired state — for IaC continuity, not apply
│   ├── provisioning/              # KCC YAML or Terraform HCL, written back after execution
│   ├── namespaces/<ns>/           # Namespace, RBAC, NetworkPolicy, ResourceQuota, workloads
│   └── agents/                    # Agent CRs + per-agent reader/actor identity manifests
├── fleet/                         # project-level policy; platform-tier Agent CR + identities
├── knowledge/                     # OKF base (§5) — unchanged, still not applied to any cluster
├── policy/                        # vap-agent-scope, vap-agent-pod-hardening, ChangePolicies,
│                                  #   rbac-overlay/<tier>.yaml templates (§2)
├── journal/                       # EXPORTED action log (new)
│   └── <YYYY>/<MM>/<DD>.ndjson    # one ActionRecord summary per line, append-only
└── .github/workflows/             # optional: drift-detect + policy CI. NOT an applier.
```

**Bootstrap remains a human path.** `policy/` and the initial `agents/` + identity manifests are
applied by a human or the install pipeline **before** any agent exists — an agent cannot bootstrap
its own authority. After bootstrap, the parent-provisions-child flow (§4.2) creates children
in-cluster and the mirror records them.

### 3.1 Mirror contract

Configured per agent by `spec.integration.github.mirror` (§1.1). Executed by the broker **after**
step 11 of the pipeline — the `ActionRecord` is durable first, always.

| Field         | Type                       | Default            | Meaning                                                                                                                               |
| ------------- | -------------------------- | ------------------ | ------------------------------------------------------------------------------------------------------------------------------------- |
| `enabled`     | bool                       | `false`            | Mirroring off by default; the journal is the system of record                                                                         |
| `mode`        | `state` \| `log` \| `both` | `both`             | `state` writes desired-state files; `log` appends `journal/…ndjson`                                                                   |
| `branch`      | string                     | `main`             | Target branch. Commits are **direct** — no PR, because there is no review to perform                                                  |
| `paths`       | []string                   | derived from scope | Restricts what this agent may mirror; defaults to its own scope's subtree                                                             |
| `batchWindow` | duration                   | `5m`               | **`log` mode only.** Coalesce journal appends into one commit to avoid a commit per pod restart. Ignored for `state` mode — see below |

**`state` commits are synchronous; `log` commits may batch.** This distinction is load-bearing, not
an optimisation detail. [04](04-workflow-model.md) §6 rests the `mirror`-mode race mitigation on the
commit being "part of the action rather than a later batch" — a five-minute coalescing window would
widen exactly the window in which a GitOps engine reverts the agent. So:

- **`state`** — the desired-state write is performed **within the action**, immediately after step
  11, before the action is reported complete. `batchWindow` does not apply. Its latency is measured
  and bounded (09 §12, `V-PRO-014`).
- **`log`** — the journal append is an audit record with no reconciliation semantics, so coalescing
  it is safe and `batchWindow` applies.
- **`both`** — the state write is synchronous; the log append batches.

A mirror failure never blocks, delays, or reverts the action itself; it is retried and surfaced.

**Commit shape.** Conventional Commit, subject `chore(mirror): <intent>`, with trailers:

```text
chore(mirror): scale api-gateway to 6 replicas

kube-agents-action-id: 01J8Z2K9Q7V3X5M6N8P0R2T4W6
kube-agents-agent: developer-team/my-project/cluster-a/team-x
kube-agents-risk-class: elevated
kube-agents-requester: users/1234567890
kube-agents-trace-id: 4bf92f3577b34da6a3ce929d0e0e4736
[skip ci]
```

**Two hazards to design against, called out because both have bitten GitOps mirrors before:**

1. **The mirror must not trigger an applier.** If the customer still runs Argo/Flux/Actions against
   these paths, a mirror commit re-applies what the broker just applied — at best a no-op, at worst
   a fight. Mitigations, in order of preference: mirror to a dedicated branch; or have the broker
   set the same field manager (`kube-agents/<tier>/<scope>`) so server-side apply is idempotent; or
   `[skip ci]` as shown. Where a GitOps engine is authoritative for a path, the broker treats a
   drift it did not cause as a **`contested` target** (§4.4) rather than re-fixing it.
2. **Credentials.** Mirror pushes use a **Minty-brokered short-lived GitHub token**, scoped to the
   one repo, held by the **broker** — never by the agent pod. A mirror failure is logged and
   retried; it **never** blocks, reverts, or delays an action, because the mirror is not the record.

**Format.** `spec.iac.format` (`kcc` | `terraform`, default `kcc`) selects the artifact written under
`provisioning/`. Kubernetes objects are always mirrored as YAML manifests, normalized identically to
the undo-plan sanitizer (§4.3) so a mirror diff shows intent, not server noise.

---

## 4. Action contracts

This is the centre of the system. Everything else in this document exists to serve the sequence
below: an agent composes an **Action Envelope**, its **broker** classifies and executes it, and an
**`ActionRecord`** carrying an **undo plan** is the durable result.

```text
agent pod (reader SA, LLM)                broker pod (actor SA, deterministic code)
        │                                          │
        │  POST /v1alpha1/actions   ─────────────► │ 1  authenticate caller (mTLS + TokenReview)
        │      Action Envelope (§4.1)              │ 2  validate envelope schema
        │                                          │ 3  resolve scope from the CALLER, not the body
        │                                          │ 4  classify risk (§4.2)
        │                                          │ 5  check the brake (§4.4)
        │                                          │ 6  generate the undo plan (§4.3)
        │                                          │ 7  gate if required → PendingApproval
        │                                          │ 8  snapshot pre-state
        │                                          │ 9  execute (server-side apply, actor identity)
        │                                          │ 10 verify, roll back on failure
        │  ◄─────────────  ActionResponse          │ 11 journal the ActionRecord  ← before reporting
```

Steps 1, 3, 4, 5, 6 and 11 are not skippable by any caller ([03](03-security-model.md) §4.1).

### 4.1 Action Envelope

The request an agent submits to **its own** broker. Not a CRD — a request body, `POST`ed as JSON to
`https://<agent-name>-broker.<namespace>.svc:8443/v1alpha1/actions` over mTLS. A NetworkPolicy
admits **only** the agent pod of the same `Agent` CR to that endpoint.

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: ActionEnvelope

# ---- what and why -----------------------------------------------------------------------------
intent: "restart crash-looping deployment api-gateway (OOMKilled x7 in 10m) and raise its memory limit"
rationale:
  | # optional, ≤4096 chars — model reasoning, recorded, NEVER a classification input
  Container `api-gateway` exceeded its 256Mi limit under the 17:40 traffic step.
  Prior 24h p99 RSS is 340Mi. Raising to 512Mi and restarting.

# ---- the operations (applied atomically: all targets in scope, or the envelope is rejected) ----
operations:
  - op: patch # create | apply | patch | delete | scale
    target:
      group: apps
      version: v1
      kind: Deployment
      namespace: team-x
      name: api-gateway
    patch:
      type: application/merge-patch+json
      body:
        spec:
          template:
            spec:
              containers:
                - name: api-gateway
                  resources: { limits: { memory: 512Mi } }
  - op: patch
    target:
      {
        group: apps,
        version: v1,
        kind: Deployment,
        namespace: team-x,
        name: api-gateway,
      }
    patch:
      type: application/merge-patch+json
      body:
        spec:
          template:
            metadata:
              annotations: { kube-agents/restarted-at: "2026-07-24T17:58:02Z" }

# ---- provenance -------------------------------------------------------------------------------
requester:
  kind: human # human | agent | system
  id: users/1234567890 # chat user id, agent identity key, or "" for system
  platform: googlechat # googlechat | slack | kubectl | mesh | ""
  displayName: "A. Parco"
  assertion: "" # router-signed JWT. Empty ⇒ ActionRecord marks attributionUnverified (§2a)
trigger:
  source: watch # chat | watch | alert | cron | delegation | escalation | undo
  ref: "pod/api-gateway-7d9c-4kk2" # the object/alert/thread that caused this
  detail: "CrashLoopBackOff, 7 restarts in 10m"
trace:
  traceId: 4bf92f3577b34da6a3ce929d0e0e4736 # W3C trace-id, 32 hex
  spanId: 00f067aa0ba902b7
  sessionId: hermes-9f21c4 # Hermes session
  threadId: spaces/AAAA/threads/BBBB # chat thread, for the reply

# ---- execution controls (stricter-only) --------------------------------------------------------
idempotencyKey: sha256:9f2b…c41a # caller-computed; dedupes retries (see below)
dryRun: false # true ⇒ classify, plan, verify-plan, journal as DryRun; never execute
requireApproval: false # true ⇒ force this action to `gated` even if it classifies lower
maxObjects: 5 # caller's own cap; effective cap = min(this, budget, ChangePolicy, code)
deadlineSeconds: 120 # broker aborts and rolls back past this
```

**Field reference.**

| Field                       | Type                                                     | Req | Default      | Notes                                                                                                  |
| --------------------------- | -------------------------------------------------------- | --- | ------------ | ------------------------------------------------------------------------------------------------------ |
| `intent`                    | string, 1–512                                            | ✓   | —            | Human-readable, imperative, one line. Rendered in chat, the digest, and `ActionRecord`                 |
| `rationale`                 | string, ≤4096                                            |     | `""`         | Recorded for review. **Never** read by the classifier                                                  |
| `operations[]`              | array, 1–50                                              | ✓   | —            | Applied in order, atomically w.r.t. scope/classification (see atomicity, below)                        |
| `operations[].op`           | enum                                                     | ✓   | —            | `create` \| `apply` \| `patch` \| `delete` \| `scale`                                                  |
| `operations[].target`       | object                                                   | ✓   | —            | `{group, version, kind, namespace, name}`. `group: ""` for core. Cloud variant below                   |
| `operations[].desiredState` | object                                                   | (a) | —            | Full object for `create`/`apply`. Mutually exclusive with `patch`                                      |
| `operations[].patch`        | `{type, body}`                                           | (a) | —            | `type` ∈ `application/merge-patch+json`, `application/json-patch+json`, `application/apply-patch+yaml` |
| `operations[].delete`       | `{propagationPolicy, gracePeriodSeconds, preconditions}` |     | `Foreground` | Only with `op: delete`                                                                                 |
| `operations[].scale`        | `{replicas}`                                             | (a) | —            | Only with `op: scale`                                                                                  |
| `requester`                 | object                                                   | ✓   | —            | Attribution. **Not** an authorization input in v1 (§2a)                                                |
| `trigger`                   | object                                                   | ✓   | —            | `source` is a closed enum; drives autonomy metrics ([01](01-vision-scope.md) §7)                       |
| `trace`                     | object                                                   | ✓   | —            | `traceId` required; the chain in §8 depends on it                                                      |
| `idempotencyKey`            | string, ≤128                                             | ✓   | —            | See below                                                                                              |
| `dryRun`                    | bool                                                     |     | `false`      | Forced `true` when `spec.operations.dryRunOnly` is set                                                 |
| `requireApproval`           | bool                                                     |     | `false`      | Stricter-only: `true` raises to `gated`; `false` never lowers anything                                 |
| `maxObjects`                | int                                                      |     | `1`          | Guards fan-out for selector-shaped operations                                                          |
| `deadlineSeconds`           | int, 1–900                                               |     | `120`        | Clamped to the code ceiling                                                                            |

(a) exactly one of `desiredState` / `patch` / `scale` per operation, matching its `op`.

**Cloud target variant.** For non-Kubernetes resources, `target` takes the cloud shape; everything
else in the envelope is identical:

```yaml
- op: apply
  cloudTarget:
    provider: gcp
    service: container.googleapis.com
    resource: projects/my-project/locations/us-central1/clusters/cluster-a/nodePools/default
    method: setSize
  desiredState: { nodeCount: 6 }
```

**Idempotency.** `idempotencyKey` is a caller-computed digest over
`(agent identity, sorted target references, normalized desired state / patch body)`. The broker
keeps keys for 24h (configurable). A repeat within the window returns the **original**
`ActionRecord` reference with `deduplicated: true` and executes nothing — this is what makes an
agent retry after a timeout safe, and it is also the first line of defence against a flapping loop.

**Atomicity.** Scope resolution, classification, and the brake check apply to the envelope **as a
whole**: one out-of-scope or forbidden target rejects the entire envelope, with nothing applied
([03](03-security-model.md) §4.1 step 3). Execution itself is best-effort sequential: if operation
_k_ fails, the broker rolls back operations _1..k-1_ using the already-generated undo plan and
records `Failed` or `RolledBack`. An envelope should therefore group operations that belong to one
logical change (as above: limit + restart) and **not** batch unrelated work.

#### What the broker ignores — and what it refuses

This is the security-load-bearing half of the schema.

| The envelope claims…                                          | Broker behaviour                                                                                                                             |
| ------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| a `tier`, `scope`, `namespace` **authority**, or `actor`      | **Refused.** These are reserved top-level keys. Scope is derived from the authenticated caller's SA ([03](03-security-model.md) §4.1 step 1) |
| a `riskClass`, `class`, `severity`, or `approved: true`       | **Refused.** Classification is computed, never asserted                                                                                      |
| a `bypass`, `force`, `skipJournal`, `skipVerify`, `emergency` | **Refused**, and emits a security event — these names exist only to be rejected loudly                                                       |
| an `undoPlan`                                                 | **Refused.** The broker generates the plan; a caller-supplied one is an undo-poisoning vector                                                |
| any other unknown field                                       | **Refused** (`400 unknown field`). The schema is closed; nothing is silently dropped                                                         |
| `rationale` arguing an action is safe / urgent / approved     | **Recorded and ignored.** Model output is never a risk signal ([03](03-security-model.md) §8)                                                |
| `requester.id` (unsigned)                                     | **Recorded, not trusted.** Without a valid `assertion` the record carries `attributionUnverified: true`                                      |

"Refused" means HTTP `400`/`403`, no execution, and — for the reserved-key and `bypass` families —
an `ActionRecord` in status `Rejected` plus a security event, so an injected agent trying to talk
its way past the broker leaves evidence rather than a gap.

**Response.**

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: ActionResponse
actionId: 01J8Z2K9Q7V3X5M6N8P0R2T4W6 # ULID; also the ActionRecord name suffix
actionRecordRef: { name: ar-01j8z2k9q7v3x5m6n8p0r2t4w6, namespace: team-x }
status: Verified # §4.3 lifecycle value
riskClass: elevated
decision: executed # executed | pending-approval | rejected | deduplicated | dry-run
undoAvailable: true
undoCommand: "kubectl kage undo 01J8Z2K9Q7V3X5M6N8P0R2T4W6"
verification:
  { passed: true, checks: 2, detail: "rollout complete; 0 restarts in 5m" }
message: "raised memory limit to 512Mi and restarted api-gateway"
retryAfterSeconds: 0 # non-zero when paused/frozen/budget-exhausted
```

### 4.2 Risk classification & `ChangePolicy`

The classifier is **deterministic code in the broker**, evaluated on every envelope before
execution ([03](03-security-model.md) §5). Its inputs are the envelope's targets and the live
cluster state — never `intent`, never `rationale`, never anything a model wrote in prose.

**Output contract.**

```yaml
classification:
  class: gated # routine | elevated | gated | forbidden
  reasons: # ordered, every rule that fired — this is the explanation shown to humans
    - {
        rule: destructive-stateful-delete,
        class: gated,
        detail: "PersistentVolumeClaim team-x/pg-data",
      }
    - {
        rule: environment-production,
        class: "+1",
        detail: "namespace label env=production",
      }
  blastRadius: { objects: 1, fractionOfScope: 0.02, cap: 25 }
  undoable: false
  undoReason: "delete of a bound PVC is not reconstructable: PV data is not snapshotted"
  policySources: [code-floor, changepolicy/baseline-conservative]
```

**Evaluation order** (short-circuit at the first two):

1. **Scope** — every target inside the caller's derived scope? Any miss ⇒ `forbidden`, stop.
2. **Forbidden set** ([03](03-security-model.md) §3.3) ⇒ `forbidden`, stop.
3. Compute a class from each remaining input; the result is the **maximum** over all of them.
4. Apply the `+1` escalations (environment, novelty), capped at `gated`.
5. Apply every matching `ChangePolicy`, taking the **maximum** again (stricter-only, by construction).
6. If no valid undo plan can be generated (§4.3) ⇒ **raise to at least `gated`**.

**Rule table shape.** The code floor is a list of rules of exactly this form; a `ChangePolicy`
contributes additional rules in the same form, and nothing else can.

```yaml
- id: destructive-stateful-delete
  when:
    verbs: [delete]
    kinds:
      - { group: "", kind: PersistentVolumeClaim }
      - { group: "", kind: PersistentVolume }
      - { group: "", kind: Namespace }
      - { group: apps, kind: StatefulSet }
      - { group: container.cnrm.cloud.google.com, kind: ContainerCluster }
      - { group: container.cnrm.cloud.google.com, kind: ContainerNodePool }
  class: gated
  reason: "deletes data or capacity that cannot be reconstructed from a manifest"
```

| Field                                        | Type                                              | Meaning                                                                         |
| -------------------------------------------- | ------------------------------------------------- | ------------------------------------------------------------------------------- |
| `id`                                         | string, unique                                    | Appears in `classification.reasons[].rule` and in the audit event               |
| `when.verbs`                                 | []enum                                            | Envelope `op` values this rule matches. Empty = any                             |
| `when.kinds`                                 | []`{group, kind}`                                 | Target kinds. Empty = any                                                       |
| `when.namespaces` / `when.namespaceSelector` | []string / labelSelector                          | Target namespaces                                                               |
| `when.labelSelector`                         | labelSelector                                     | Matched against the **live** target object, not the desired state               |
| `when.fieldPaths`                            | []string (JSONPath)                               | Fires when the change touches these paths (e.g. `spec.type`, `spec.ingress`)    |
| `when.direction`                             | `loosen` \| `tighten` \| `any`                    | Security direction; `loosen` is what gates                                      |
| `class`                                      | `routine`\|`elevated`\|`gated`\|`forbidden`\|`+1` | The class this rule contributes                                                 |
| `maxObjects`                                 | int                                               | Blast-radius cap; exceeding it raises to `gated`, exceeding the hard cap aborts |
| `reason`                                     | string                                            | Shown verbatim to the human                                                     |

**The code floor, abridged** — the rules that must exist, mapping 1:1 onto
[03](03-security-model.md) §5.2:

| `id`                          | Fires on                                                                                             | Class       |
| ----------------------------- | ---------------------------------------------------------------------------------------------------- | ----------- |
| `out-of-scope`                | any target outside the caller's derived scope                                                        | `forbidden` |
| `forbidden-set`               | agent RBAC/IAM, escalation verbs, control plane, journal, protected namespaces                       | `forbidden` |
| `no-undo-plan`                | broker cannot generate a validated undo plan                                                         | `gated`     |
| `destructive-stateful-delete` | delete of PVC/PV/Namespace/StatefulSet/cluster/node pool/bucket/disk/snapshot/backup                 | `gated`     |
| `security-loosen`             | delete or weaken NetworkPolicy/PSA label/policy; widen an RBAC or IAM grant to a non-agent principal | `gated`     |
| `public-exposure`             | Service→`LoadBalancer`/`NodePort`, Ingress/Gateway added, `0.0.0.0/0` in an allow rule               | `gated`     |
| `traffic-shift-production`    | Service/Ingress/Gateway/HTTPRoute change on a production-labelled target                             | `gated`     |
| `identity-change`             | any write to ServiceAccount, Secret of type `*token*`, IAM binding                                   | `gated`     |
| `blast-radius-cap`            | `objects > maxObjects`                                                                               | `gated`     |
| `blast-radius-hard-cap`       | `objects > 100` or `fractionOfScope > 0.5`                                                           | abort       |
| `secret-write`                | create/update of a `Secret`                                                                          | `elevated`  |
| `production-environment`      | target namespace/object labelled `env=production` (or `kube-agents/environment: production`)         | `+1`        |
| `novel-action`                | first occurrence of `(op, kind)` for this agent in the trust-building window                         | `+1`        |
| `object-override`             | `kube-agents/change-policy: gated\|forbidden` annotation on the object or its namespace              | as stated   |
| _default_                     | anything else, in scope, reversible                                                                  | `routine`   |

**`ChangePolicy`** — cluster-scoped, stricter-only by construction:

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: ChangePolicy
metadata:
  name: baseline-conservative
spec:
  # Which agents this applies to. Omitted ⇒ every agent. A policy set by a parent's operator binds
  # its children too; agents cannot select themselves out.
  agentSelector:
    tiers: [developer-team]
    scopes: [{ projectId: my-project, clusterName: cluster-a }]
  rules: # same shape as the code-floor rule table above
    - id: gate-all-deletes-while-ramping
      when: { verbs: [delete] }
      class: gated
      reason: "trust-building period: all deletes are reviewed"
    - id: tighten-fanout
      when: {}
      maxObjects: 10
      reason: "cap blast radius below the code ceiling"
status:
  agentsMatched: 4
  conditions: []
```

**Why loosening is unrepresentable rather than merely forbidden.** There is no `allow`, no
`maxClass`, no `exempt`, no `class: routine` **downgrade** path: `class` on a `ChangePolicy` rule is
validated to be ≥ the class the code floor would assign for the same match, and the broker takes the
**maximum** of all sources regardless. A policy that tried to lower a class would be rejected at
admission; even if it were somehow admitted it would have no effect. The forbidden set is a code
constant and is not addressable by `ChangePolicy` at all.

**`ChangePolicy` objects are control-plane objects** ([03](03-security-model.md) §3.3 rule 3): no
actor template in §2.2 grants write on `changepolicies`, and `vap-agent-scope` denies it
independently. A human tightens policy; an agent cannot touch it in either direction.

#### Worked examples

**(1) Restart a crash-looping Deployment — `routine`.**
Envelope: `patch apps/v1 Deployment team-x/api-gateway`, adding a restart annotation.
Scope: `team-x` ⊆ developer-team scope `team-x` ✓. Forbidden set: no match. Undo plan: `restore` the
prior Deployment object (sanitized snapshot) — generated and validated ✓. Destructiveness: none.
Direction: neither. Blast radius: 1 object of ~50 → 0.02, cap 25 ✓. Environment: `team-x` carries no
production label. Novelty: this agent has patched Deployments 31 times this week.
⇒ **`routine`.** Executes immediately; appears in the periodic digest, no ping.

**(2) Scale a production Deployment 3 → 10 — `elevated`.**
Envelope: `scale apps/v1 Deployment payments-prod/checkout replicas: 10`.
Scope ✓, forbidden ✗, undo plan = `restore replicas: 3` ✓, destructive ✗, direction neither, blast
radius 1 ✓. Base class from the default rule: `routine`. Then `production-environment` fires (`+1`)
because the namespace is labelled `env=production`.
⇒ **`elevated`.** Executes immediately, pings the owning humans at once with the undo handle, and
gets the 90-day retention. Note what did **not** happen: no human blocked a reversible capacity fix
during a traffic spike — which is the entire point of the class existing between `routine` and
`gated`.

**(3) Delete a bound PVC to reclaim quota — `gated`.**
Envelope: `delete v1 PersistentVolumeClaim team-x/pg-data`.
Scope ✓ (in namespace). Forbidden set ✗. Undo-plan generation **fails**: recreating the PVC yields a
new volume, not the data — `no-undo-plan` fires (`gated`). Independently
`destructive-stateful-delete` fires (`gated`). Blast radius 1.
⇒ **`gated`, for two independent reasons.** Parks as `PendingApproval`, notifies
`team-x-approvers`, expires after the roster's TTL, executes nothing meanwhile. A chat message
insisting the volume is empty and the deletion urgent changes nothing — the classifier never reads
it ([03](03-security-model.md) §8.1).

**(4, contrast) Bind the agent's own reader SA to `cluster-admin` — `forbidden`.**
Rule `forbidden-set` fires at step 2 (RBAC naming an agent identity). Rejected outright, security
event emitted, no approval path offered anywhere; repeated attempts trip the SLI and auto-pause the
agent ([03](03-security-model.md) §6).

### 4.3 `ActionRecord`

The durable journal entry — one per envelope, created **before** the action is reported complete
([03](03-security-model.md) §4.1 step 11). An `ActionRecord` is a namespaced CRD
(`kubeagents.x-k8s.io/v1alpha1`), created in the agent's namespace, so `kubectl get actionrecords`
works, admission can protect it, and the undo controller can watch it. It is mirrored to the
durable log sink ([05](05-system-architecture.md)) and, optionally, exported to `journal/` (§3).

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: ActionRecord
metadata:
  name: ar-01j8z2k9q7v3x5m6n8p0r2t4w6 # "ar-" + lower-cased ULID
  namespace: team-x
  labels:
    kube-agents/tier: developer-team
    kube-agents/scope: team-x
    kube-agents/risk-class: elevated
    kube-agents/status: Verified
    kube-agents/trigger: watch
spec: # IMMUTABLE after creation (enforced by CEL + vap-agent-scope)
  actionId: 01J8Z2K9Q7V3X5M6N8P0R2T4W6
  agentRef: { name: developer-team-team-x, namespace: team-x }
  agentIdentity: developer-team/my-project/cluster-a/team-x # the (tier, scope) key
  actorServiceAccount: developer-team-team-x-actor # who actually wrote
  requester:
    {
      kind: human,
      id: users/1234567890,
      platform: googlechat,
      displayName: "A. Parco",
    }
  attributionUnverified: false # true when no signed requester assertion was present
  trigger:
    {
      source: watch,
      ref: pod/api-gateway-7d9c-4kk2,
      detail: "CrashLoopBackOff x7/10m",
    }
  trace:
    {
      traceId: 4bf92f3577b34da6a3ce929d0e0e4736,
      spanId: 00f067aa0ba902b7,
      sessionId: hermes-9f21c4,
    }
  intent: "raise api-gateway memory limit to 512Mi and restart"
  rationale: "…" # recorded, never a classification input
  idempotencyKey: sha256:9f2b…c41a
  dryRun: false

  classification: # verbatim output of §4.2
    class: elevated
    reasons:
      - {
          rule: production-environment,
          class: "+1",
          detail: "namespace label env=production",
        }
    blastRadius: { objects: 1, fractionOfScope: 0.02, cap: 25 }
    undoable: true
    policySources: [code-floor, changepolicy/baseline-conservative]

  targets:
    - {
        group: apps,
        version: v1,
        kind: Deployment,
        namespace: team-x,
        name: api-gateway,
        uid: 2f1c…,
        resourceVersion: "81422",
      }

  preState: # snapshot of every target, taken inside the broker at step 8
    - targetIndex: 0
      capturedAt: "2026-07-24T17:58:02Z"
      object: {
          apiVersion: apps/v1,
          kind: Deployment,
          metadata: { … },
          spec: { … },
        } # sanitized
      # object OR objectRef — see "large snapshots" below
      objectRef: null
      sha256: 3d1a…

  undo: # generated at step 6, BEFORE execution (§4.3.1)
    strategy: restore
    generatedAt: "2026-07-24T17:58:02Z"
    validated: true
    steps:
      - op: apply
        target:
          {
            group: apps,
            version: v1,
            kind: Deployment,
            namespace: team-x,
            name: api-gateway,
          }
        object: { … } # the sanitized preState object
        preconditions: { uid: 2f1c… } # refuse the undo if the object was replaced meanwhile
    caveats:
      - "restores spec only; pods created since will be replaced by the rollout"

  retention:
    class: elevated
    ttl: 2160h # 90d — see the retention table
    expiresAt: "2026-10-22T17:58:02Z"

status:
  phase: Verified
  observedGeneration: 1
  applied:
    - targetIndex: 0
      diff: # normalized JSON-patch of what actually changed on the server
        - {
            op: replace,
            path: /spec/template/spec/containers/0/resources/limits/memory,
            from: 256Mi,
            value: 512Mi,
          }
        - {
            op: add,
            path: /spec/template/metadata/annotations/kube-agents~1restarted-at,
            value: "2026-07-24T17:58:02Z",
          }
      resourceVersionAfter: "81430"
  verification:
    passed: true
    completedAt: "2026-07-24T18:03:11Z"
    checks:
      - {
          name: rollout-complete,
          passed: true,
          detail: "1/1 updated replicas available",
        }
      - {
          name: no-restarts-5m,
          passed: true,
          detail: "0 container restarts since apply",
        }
  recovery: # the recovery ladder (04 §5), recorded so it is observable
    rung: 1 # 1 retry · 2 alternative · 3 rollback · 4 escalate · 5 page
    transitions: # append-only; a skipped rung MUST carry a reason
      - { at: "2026-07-24T18:02:55Z", from: 0, to: 1, reason: conflict-retry }
  report: # the four beats (02 §2.5.4) as STRUCTURED fields, not prose
    noticed: "checkout OOMKilled every ~40s against a 256Mi limit"
    did: "raised limits.memory to 512Mi (elevated)"
    verified: "3/3 pods Ready, restart count flat for 6m"
    undo: "kage undo 01J8Z2K9Q7V3X5M6N8P0R2T4W6"
  approvals: # present only for gated actions
    required: 1
    granted: []
    rejected: []
    expiresAt: null
  contested: false # set true when a human undoes or manually reverts this change (§4.4)
  undoneBy: "" # actionId of the undo action, once executed
  timestamps:
    submitted: "2026-07-24T17:58:01Z"
    classified: "2026-07-24T17:58:01Z"
    approved: null
    executionStarted: "2026-07-24T17:58:02Z"
    executionEnded: "2026-07-24T17:58:04Z"
    verified: "2026-07-24T18:03:11Z"
  message: "raised memory limit to 512Mi and restarted api-gateway"
```

**`status.report` is structured, and the chat text is rendered from it — never the reverse.** The
four beats of [02](02-agent-personas.md) §2.5.4 are fields, not prose the harness has to parse back
out of a chat message. This is what makes the character and honesty requirements _mechanically_
checkable rather than a matter for an LLM judge: a report claiming a fix can be compared directly
against `status` and `verification.passed`, and a missing beat is a schema failure. An
implementation that emits chat prose and derives the fields afterwards is non-conforming, because
the two can then disagree.

**`status.recovery` makes the ladder observable.** [04](04-workflow-model.md) §5 requires that the
agent never skips a rung silently and never restarts at the bottom for the same target after a
rollback. Neither is checkable unless the rung is recorded, so it is: `transitions` is append-only,
non-decreasing in `rung`, and any skip carries a `reason`.

**Status lifecycle.**

```text
                          ┌──────────► Rejected            (forbidden, out of scope, brake, refused schema)
                          │
Pending ──► PendingApproval ──► Executing ──► Verified ──► Undone
   │              │  │              │              │
   │              │  └► Expired     └► Failed ──► RolledBack
   │              └► Rejected                (verification failed; broker restored pre-state)
   └────────────────────────────► Executing        (routine / elevated: no gate)

DryRun is a terminal state reached from Pending when dryRun=true.
```

| Phase             | Meaning                                                                                | Terminal |
| ----------------- | -------------------------------------------------------------------------------------- | -------- |
| `Pending`         | Accepted, classified, undo plan generated; not yet executing                           |          |
| `PendingApproval` | `gated`; awaiting the roster. Nothing has been written                                 |          |
| `Executing`       | Snapshot taken; server-side apply in progress                                          |          |
| `Verified`        | Executed **and** the intended outcome confirmed                                        | ✓        |
| `Failed`          | Execution errored; partial work rolled back where possible                             | ✓        |
| `RolledBack`      | Executed, verification failed, pre-state automatically restored                        | ✓        |
| `Undone`          | A human ran `undo`; the plan replayed successfully                                     | ✓        |
| `Rejected`        | Refused before execution (forbidden / out of scope / braked / rejected by an approver) | ✓        |
| `Expired`         | `gated` action whose approval TTL elapsed                                              | ✓        |
| `DryRun`          | Classified, planned, journaled; deliberately not executed                              | ✓        |

**Retention defaults** (`spec.retention.ttl`, per class; a cleanup controller deletes on
`expiresAt`, and the export in §3 plus the log sink outlive the CR):

| Class                  | TTL   | Rationale                                            |
| ---------------------- | ----- | ---------------------------------------------------- |
| `routine`              | 30 d  | Enough to notice and undo; keeps etcd small          |
| `elevated`             | 90 d  | Consequential changes stay undoable across a quarter |
| `gated`                | 365 d | Approval evidence; also the audit-review horizon     |
| `Rejected` (forbidden) | 365 d | Security evidence — never short-lived                |

**Large snapshots.** A `preState.object` above **1 MiB** (or an envelope whose total snapshot
exceeds 1 MiB) is written to the journal store instead and referenced by
`preState[].objectRef: {store, key, sha256}`; the CR keeps the digest only. The broker verifies the
digest on undo and refuses to replay a snapshot that does not match. **If the snapshot cannot be
persisted, the action does not execute** — fail-closed, same rule as journaling
([03](03-security-model.md) §6).

**Immutability.** `spec` is immutable; `status` is writable **only** by the broker and the undo
controller. `vap-agent-scope` denies `delete` and `update` on `actionrecords` to every agent
identity ([03](03-security-model.md) §3.3 rule 4) — including the actor SA that created it.

#### 4.3.1 The undo plan

Generated at step 6, **before** execution, and validated by dry-running each step against the API
server. If generation or validation fails, the action is raised to `gated` (§4.2 `no-undo-plan`).

| Original op                    | `strategy` | Steps                                                                               | Fidelity                                                                                 |
| ------------------------------ | ---------- | ----------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `create`                       | `delete`   | `delete` the created object, with `preconditions.uid` = the UID the create returned | **Exact.** Removing something that did not exist restores the prior state precisely      |
| `apply` (object existed)       | `restore`  | `apply` the sanitized `preState` object with `preconditions.uid`                    | **Exact for spec/metadata.** Server-defaulted and controller-owned fields reconverge     |
| `apply` (object did not exist) | `delete`   | as `create`                                                                         | Exact                                                                                    |
| `patch`                        | `restore`  | `apply` the sanitized `preState` object                                             | Exact for spec/metadata                                                                  |
| `scale`                        | `restore`  | `scale` back to the recorded `replicas`                                             | Exact for the field; pod identities are not preserved (and for a Deployment need not be) |
| `delete`                       | `recreate` | `create` from the sanitized snapshot                                                | **Structural only — see below.** This is why most deletes gate                           |
| cloud `apply`/`setSize`        | `inverse`  | the provider's inverse call with the recorded prior value                           | Exact where the provider exposes a true inverse; otherwise `none`                        |
| anything else                  | `none`     | —                                                                                   | ⇒ `gated`                                                                                |

**Sanitizer** (applied to every snapshot before it becomes an undo step — this normalization is what
makes `restore` idempotent and mirror diffs readable):

```text
DROP   metadata.{uid, resourceVersion, generation, creationTimestamp, managedFields,
                 deletionTimestamp, deletionGracePeriodSeconds, selfLink}
DROP   metadata.annotations["kubectl.kubernetes.io/last-applied-configuration"]
DROP   status                       (unless the target IS a status subresource)
DROP   spec.clusterIP, spec.clusterIPs, spec.healthCheckNodePort, .nodePort   (immutable, reassigned)
KEEP   metadata.{name, namespace, labels, annotations, ownerReferences, finalizers}
KEEP   spec (in full), and data/stringData for ConfigMap/Secret
REDACT Secret.data values in the CR copy → replaced with sha256 digests; the restorable ciphertext
       is written to the journal store under objectRef (never to the mirror repo, never to a log)
```

**What is NOT undoable — and is therefore `gated` by definition.** The list is short, explicit, and
the same list that appears in [03](03-security-model.md) §5.2 as `destructiveness`:

- **Data.** Deleting a bound `PersistentVolumeClaim`, a `PersistentVolume`, a cloud disk, bucket,
  database, snapshot, or backup. Recreation yields a new empty volume: structurally identical,
  materially different.
- **Namespace and cluster deletion.** Cascading, non-atomic, and not reconstructable from one
  snapshot; recreating the container does not recreate its contents.
- **Node pool deletion / shrink below in-use capacity.** Local state and in-flight work are lost.
- **Identity and credential operations.** Rotating or deleting a credential, revoking a key,
  releasing a static IP or DNS name — the old value is gone even if the object comes back.
- **Anything whose effect left the API.** A `Job` that sent mail, charged a card, or called a
  webhook; a traffic shift that already served requests. The object is restorable; the effect is not.
- **Objects with a new identity on recreation.** A recreated object gets a new UID, so every
  `ownerReference`, PVC binding, and external reference pointing at the old one is dangling. The
  broker detects inbound references during plan generation and downgrades `recreate` to `none`.

**Undo is itself an action.** Replaying a plan submits a new envelope with `trigger.source: undo`,
which is classified, snapshotted, verified, and journaled like any other — so an undo whose own
effect is destructive gates in turn ([03](03-security-model.md) §6).

### 4.4 Brake contract — `pause` / `resume` / `freeze` / `undo` / `contested`

All five live in the controller and the broker, never in a skill, a prompt, or the model. All five
must work with the LLM, the router, and the inference stack unavailable
([03](03-security-model.md) §6).

| Control     | Object / field                                                     | Scope       | Who may invoke                                            | Semantics                                                                                                                                                                              |
| ----------- | ------------------------------------------------------------------ | ----------- | --------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pause`     | `Agent.spec.operations.paused: true` (+ `pauseReason`)             | one agent   | the agent's `allowedUsers`; anyone with `patch` on the CR | Broker refuses **new** envelopes immediately (`403 agent-paused`, `retryAfterSeconds`). The in-flight action completes or rolls back — never half-applied. The work queue is preserved |
| `resume`    | `Agent.spec.operations.paused: false`                              | one agent   | the agent's **approval roster**                           | Clears only the pause. `contested` markers, freezes, and budget cooldowns survive                                                                                                      |
| `freeze`    | `FleetFreeze` (cluster-scoped)                                     | scope/fleet | approval roster of the owning agent or a parent's         | Nothing executes in the frozen scope. Consulted on **every** envelope. **Fail-closed**                                                                                                 |
| `undo`      | `UndoRequest` (namespaced) → new envelope                          | one action  | the owning agent's `allowedUsers`                         | Replays the recorded plan as a first-class classified, journaled action                                                                                                                |
| `contested` | `ActionRecord.status.contested: true` + advisory target annotation | one target  | set **automatically**; cleared by the roster              | The agent must not redo that change to that target without explicit human instruction                                                                                                  |

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: FleetFreeze
metadata: { name: incident-4471 }
spec:
  scope: # omit narrower fields to widen; {} means THE ENTIRE FLEET
    projectId: my-project
    clusterName: cluster-a # optional
    namespace: "" # optional
  reason: "INC-4471 — payments degraded, no automated changes"
  requestedBy: users/1234567890
  expiresAt: "2026-07-24T22:00:00Z" # optional; a freeze with no expiry never self-clears
  allowUndo: true # default true — undo and rollback keep working during a freeze
  allowClasses: [] # default empty = nothing executes. May list ONLY `routine`; never `gated`
status:
  agentsFrozen: 12
  activeSince: "2026-07-24T18:41:00Z"
```

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: UndoRequest
metadata: { name: undo-01j8z2k9q7v3x5m6n8p0r2t4w6, namespace: team-x }
spec:
  actionRef: { name: ar-01j8z2k9q7v3x5m6n8p0r2t4w6 }
  reason: "wrong diagnosis — the OOM was upstream"
  requestedBy: users/1234567890
  markContested: true # default true: also mark the target contested
status:
  phase: Executed # Pending | Executing | Executed | Failed | Refused
  undoActionId: 01J8Z4M2P8Q0R1S2T3U4V5W6X7
  message: "restored Deployment team-x/api-gateway to resourceVersion 81422"
```

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: ApprovalRoster
metadata: { name: team-x-approvers, namespace: team-x }
spec:
  approvers:
    - { platform: googlechat, id: "users/1234567890", displayName: "A. Parco" }
    - { platform: slack, id: "U02ABCDEF", displayName: "R. Ops" }
  minApprovals: 1 # default 1
  allowSelfApproval: false # default false — the human who requested an action may not approve it
  ttl: 4h # default 4h; a gated action past its TTL becomes `Expired`
  notify: # where approval requests land
    googleChat: { space: "spaces/AAAA" }
    slack: { channel: "C01ABCDEF" }
  escalateTo: { name: cluster-a-approvers, namespace: kubeagents-system } # optional, on TTL
```

**Fail-closed rules — the whole point of the brake.**

| Condition                                                               | Broker behaviour                                                           |
| ----------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| Cannot read the `FleetFreeze` list (API error, cache stale beyond 30 s) | **Treat the scope as frozen.** Refuse everything except `undo`             |
| Cannot read its own `Agent` CR                                          | Treat as paused                                                            |
| Cannot reach the journal store                                          | Refuse to execute; set `status.broker.journalReachable: false`; auto-pause |
| Cannot persist a pre-state snapshot                                     | Refuse that envelope                                                       |
| Cannot generate or validate an undo plan                                | Raise to `gated` (never execute on a hope)                                 |
| Approval roster missing / empty while a `gated` action waits            | Action stays `PendingApproval` and expires; it is **never** auto-approved  |
| Initiative budget exhausted, or flap threshold breached                 | Refuse and escalate to a human ([04](04-workflow-model.md) §4.2)           |
| Target carries a `contested` marker                                     | Refuse; report; require explicit human instruction to proceed              |
| Broker cannot verify an executed action **and** cannot roll it back     | Auto-pause the agent and page ([03](03-security-model.md) §6)              |

**`contested`, precisely.** When a human runs `undo`, or the broker observes a target reverted by a
non-agent field manager within the flap window, the broker records `contested: true` on the
originating `ActionRecord` and indexes the target reference. A later envelope whose target matches a
contested entry is refused with `403 target-contested` and the originating `actionId`. The index is
authoritative because a deleted object cannot hold an annotation; where the object exists the broker
**also** stamps `kube-agents/contested: <action-id>` on it as an advisory signal for humans. A
contested marker is cleared only by an approval-roster member (`/uncontest <action-id>` or removing
the annotation and patching the record's status) — never by the agent, and never by `resume`.

**Agents cannot touch any of it.** `Agent` CRs, `FleetFreeze`, `ApprovalRoster`, `ChangePolicy`, and
`ActionRecord` status are control-plane objects: absent from every actor template in §2.2, and
denied to agent identities by `vap-agent-scope` independently ([03](03-security-model.md) §3.3
rule 3). An agent can be stopped; it cannot stop being stoppable.

---

## 5. OKF knowledge contract

OKF = markdown + YAML frontmatter in the repo's **`knowledge/` root**. It lives outside the mirrored
state paths, so it is never applied to a cluster. Required frontmatter field: `type`.

| `type`              | Purpose                                   | Key frontmatter                                |
| ------------------- | ----------------------------------------- | ---------------------------------------------- |
| `cluster-blueprint` | Standard cluster config baseline          | `title, tags, resource, timestamp`             |
| `tenancy-model`     | Namespace isolation standard              | `title, tags`                                  |
| `runbook`           | Operational procedure (SRE CUJ)           | `title, tags, timestamp`                       |
| `metric-definition` | Named metric/KPI definition               | `title, tags, resource`                        |
| `escalation`        | A cross-tier request **not** yet acted on | `title, tags, timestamp, resource`             |
| `observation`       | A durable finding worth sharing           | `title, tags, timestamp`, **`actionRefs: []`** |

**Two changes from the read-only generation:**

1. **Observations record actions taken, not proposals made.** An `observation` now carries
   `actionRefs: [<action-id>, …]` linking the finding to the `ActionRecord`s that resolved it. The
   durable knowledge is "this failure mode recurs and _this fix worked_", not "here is a suggestion
   someone should apply".
2. **`escalation` is no longer a message bus.** Cross-tier requests go over the agent mesh (§7),
   synchronously. The `escalation` type survives only for requests a human must resolve — a budget
   approval, a vendor ticket, a decision outside every agent's scope.

Agents **read** OKF for context and **write** curated updates through the mirror (§3), attributed to
the `ActionRecord` that produced them. The six types are the canonical starting set; `type` is an
**open convention, not a hard enum**. Layout mirrors OKF: `knowledge/{index.md, <type>/…}`; markdown
links form the graph; optional `log.md` for history. OKF holds durable knowledge only — **not**
session state.

## 6. Session-state contract (mem0 deferred post-v1)

**Semantic recall (mem0/Qdrant) is deferred post-v1**; v1 ships no vector store. If introduced
later, scope every insert/query by the composite key `{tier}:{scope-id}` (e.g.
`developer-team:cluster-a/team-x`) with **server-side** isolation — one collection or
access-controlled key per scope, never a client-supplied filter, because a cross-scope read is an
isolation escape ([03](03-security-model.md) §3) — with TTL entries (30–90 d) that graduate durable
observations into OKF.

**Session state (existing, `multiuser_memory`):** `session_db.sqlite` keyed by
platform/space/thread; per-user memory in `memories/users/<safe_user_id>.md`; shared SOPs in
`memories/MEMORY.md`. Per-user isolation by runtime `user_id`. This stays as-is.

Two consumers beyond the agent itself: the gateway uses `thread_id` / `chat_id` for **routing thread
affinity** (§2b, mode `sticky`), and the broker uses `trace.threadId` to deliver the **action report
and its undo handle back into the thread that triggered it** — including asynchronously, when a
`gated` action is approved minutes later. Session state is never an authorization input.

## 7. Agent mesh contract

Replaces the review-gate contract that occupied this slot. Agents call each other **directly**: a
parent **delegates** down, a child **escalates** up ([02](02-agent-personas.md) §2.3, README
invariant 5). The mesh carries **requests, never authority**.

**Transport.** HTTPS + JSON, mTLS, served by the **agent pod** (not the broker) at
`https://<agent-name>.<namespace>.svc:8444/v1alpha1/mesh/{delegate,escalate}`. Discovery is the
`(tier, scope)` index over `Agent` CRs (§1.2) — the same key routing and cardinality use — resolved
to the CR's `status.serviceStatus.endpoint`. There is no registry and no broadcast.

```yaml
# ---- request -----------------------------------------------------------------------------------
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: MeshRequest
kind_: delegate # delegate (parent→child) | escalate (child→parent)
from:
  agentIdentity: cluster-admin/my-project/cluster-a # derived from the caller's SA, not trusted from here
to:
  tier: developer-team
  scope: { projectId: my-project, clusterName: cluster-a, namespace: team-x }
intent: "apply the new egress NetworkPolicy baseline to your namespace"
context: # untrusted DATA for the callee's model, never instructions to its broker
  policyName: egress-baseline-v3
  deadline: "2026-07-25T00:00:00Z"
  reference: "knowledge/tenancy-model/egress-baseline.md"
trace: { traceId: 4bf92f…, spanId: 00f067aa0ba902b7, sessionId: hermes-9f21c4 }
requester: { kind: agent, id: cluster-admin/my-project/cluster-a } # attribution only
chain: # LOOP PREVENTION — see below
  depth: 1
  visited: ["platform/my-project", "cluster-admin/my-project/cluster-a"]
idempotencyKey: sha256:1b7e…90fa
deadlineSeconds: 60
```

```yaml
# ---- response ----------------------------------------------------------------------------------
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: MeshResponse
outcome: accepted # accepted | completed | refused | deferred | paused | frozen | over-budget | loop-detected
taskId: hermes-task-77c1 # present when accepted (async work)
actionIds: [] # ActionRecord ids produced by the callee, filled in on completion
message: "accepted; will apply egress-baseline-v3 to team-x and report in this trace"
refusalReason: "" # required when outcome == refused
retryAfterSeconds: 0 # set for paused | frozen | over-budget
```

**The rules that make this safe.**

1. **Authentication.** mTLS plus a `TokenReview` of the caller's **reader** SA. The callee derives
   `from.agentIdentity` from the authenticated identity and **overwrites** whatever the body said —
   the `from` field is a convenience for logs, never an input to a decision.
2. **The callee re-authorizes in its own scope, under its own gates.** A mesh request is exactly as
   powerful as the same words typed by a human into the callee's chat: the callee composes its
   **own** Action Envelope, submits it to **its own** broker with **its own** actor identity, and
   gets its own classification, brake check, budget, and gates. Authority is never inherited, never
   forwarded, never pooled. A `gated` action requested by a parent still waits for the **child's**
   approval roster.
3. **Topology.** `delegate` is accepted only from the caller named in `spec.parentRef`; `escalate`
   only from an agent whose `parentRef` names the callee. Sibling and cross-tree calls are refused
   with `refused / not-in-lineage`. Verified against the CR graph, not the request body.
4. **Refusal is a first-class outcome.** A callee may refuse — out of scope for it, contradicts a
   local `ChangePolicy`, or it simply disagrees. `refused` with a `refusalReason` is a normal
   response, not an error; the caller must handle it and must not retry it as a different shape.
5. **Paused / frozen callee.** Returns `paused` or `frozen` with `retryAfterSeconds`. The caller
   **must not** route around it — no doing the work itself in the callee's scope (it has no
   authority there), and no asking a sibling. It reports the blockage to a human.
6. **Timeouts.** `deadlineSeconds` default 60, ceiling 300. Work that outlives the deadline returns
   `accepted` + `taskId` and reports asynchronously into the same `traceId`. A timed-out caller must
   treat the outcome as **unknown**, not failed, and reconcile by reading the callee's
   `ActionRecord`s for its `idempotencyKey`.
7. **Loop prevention.** `chain.visited` is the ordered list of agent identity keys already in the
   call chain, and `chain.depth` its length. On receipt the callee: refuses with `loop-detected` if
   its own identity is already in `visited`; refuses if `depth >= 4` (code ceiling; default budget
   3); otherwise appends itself before making any onward call. A request arriving with an absent or
   malformed `chain` is refused, not defaulted — a missing chain is exactly what a loop looks like
   after one bad hop.
8. **Rate.** Mesh requests consume the **callee's** initiative budget, not the caller's, so a
   chatty parent cannot spend a child's autonomy. Inbound mesh rate is separately capped per caller.

## 8. Audit & attribution contract

Extends `docs/designs/audit-logging-user-attribution.md`. The requirement is a **single unbroken
chain** from the human's message to the row in the cloud audit log:

```text
chat message            → requester + threadId + traceId          (router, §2b)
  → Action Envelope     → same traceId, requester, trigger        (§4.1)
    → ActionRecord      → actionId + actorServiceAccount + traceId (§4.3)
      → Kubernetes write→ annotation kube-agents/action-id + field manager
        → audit log     → actor SA + annotation + user-agent
```

**Every write carries `kube-agents/action-id`.** The broker stamps
`metadata.annotations["kube-agents/action-id"]` on every object it creates or updates, plus
`kube-agents/agent` (the identity key) and `kube-agents/risk-class`. Server-side apply uses field
manager `kube-agents/<tier>/<scope>`. `vap-agent-scope` **rejects** a write by an actor SA that
lacks the annotation, so an unjournaled write is impossible rather than merely detectable
([03](03-security-model.md) §4.3) — this is SLI 2 ([01](01-vision-scope.md) §7) enforced at
admission.

**Where an annotation cannot go** — deletes, subresource writes, and cloud API calls — correlation
is carried instead by the HTTP user-agent
`kube-agents-broker/<version> (agent=<identity>; action=<actionId>)`, which lands in both the
Kubernetes and the Cloud Audit Log, plus the `ActionRecord` itself. Every audit query in §10 must
accept either correlation path.

**OTel resource/span attributes** emitted by the broker on every action:

| Attribute                    | Example                                        |
| ---------------------------- | ---------------------------------------------- |
| `kubeagents.action_id`       | `01J8Z2K9Q7V3X5M6N8P0R2T4W6`                   |
| `kubeagents.agent_identity`  | `developer-team/my-project/cluster-a/team-x`   |
| `kubeagents.tier` / `.scope` | `developer-team` / `team-x`                    |
| `kubeagents.actor_sa`        | `developer-team-team-x-actor`                  |
| `kubeagents.risk_class`      | `elevated`                                     |
| `kubeagents.trigger_source`  | `watch`                                        |
| `kubeagents.requester`       | `users/1234567890`                             |
| `kubeagents.routing_mode`    | `slash` \| `handle` \| `sticky` \| `inference` |
| `kubeagents.undo_available`  | `true`                                         |
| `kubeagents.approved_by`     | `users/9876543210` (gated actions only)        |

Chat turns additionally record the **resolved agent** and **routing mode** (§2b). The durable
attribution for a mutation is the **`ActionRecord`** — not a merge commit, not a PR URL. Where the
mirror is enabled, the commit trailers (§3.1) provide a secondary, human-browsable index into it.

## 9. MCP tool surface — write-capable, as envelope builders only

The read-only generation's job here was **removal**: retire `create_cluster`, make the remote `gke`
MCP describe/list only, delete the `apply_manifest` helpers. That is now **inverted** — but not by
putting the deleted tools back.

**The distinction the whole section turns on:** a tool may **compose an Action Envelope and submit
it to the broker**. A tool may **never call a mutating API itself**. The first is an agent asking
for something to happen, under classification, gating, snapshotting, verification, and journaling.
The second is an unjournaled write by a process holding an LLM — the exact thing
[03](03-security-model.md) §4 exists to make impossible. A "write tool" that talks to
`container.googleapis.com` is not a faster version of the broker; it is a hole in it.

Concretely: the agent pod holds no write credential (§2), so a mutating tool inside it has nothing
to authenticate with. The tools below are therefore not privileged — they are **request builders**,
and their entire safety story is that there is nothing to escalate.

| Tool / server                                   | Read-only generation                                                       | Imperative end state                                                                                                                                                                                                                                      |
| ----------------------------------------------- | -------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **`submit_action`** (new, core)                 | —                                                                          | **The one mutation tool.** Takes `intent` + `operations` + `trigger`, fills `trace`/`requester` from the session, computes `idempotencyKey`, `POST`s to the broker, returns the `ActionResponse` (§4.1). Every other write-shaped tool is sugar over this |
| **`plan_action`** (new)                         | —                                                                          | Same, with `dryRun: true`. Returns the classification, blast radius, and undo plan **without executing** — how an agent checks whether something will gate                                                                                                |
| `apply_manifest` / `delete_cluster_manifest`    | Undecorated `kubectl` helpers; deleted                                     | **Return as envelope builders**: `apply_manifest(obj)` → `submit_action(op=apply, …)`. The `kubectl` shell-out never returns — the broker uses a typed client                                                                                             |
| `create_cluster` (remote `gke` MCP)             | Retired; provisioning became "author KCC YAML + open a PR"                 | **Returns as `provision_cluster`** — a builder emitting a `ContainerCluster` (or Terraform) desired state into an envelope. Classified `gated` (irreversible), so it lands in the platform roster's approval queue, then the **broker** applies it        |
| `gke` MCP wiring (`renderConfigYAML()`)         | Fronted read-only / dropped from `platform_toolsets`; ConfigMap mounted RO | **Stays describe/list/get.** A remote MCP writes with a credential the broker does not control and cannot journal — so the remote proxy remains read-only _permanently_. Cloud writes go envelope → broker → cloud API with the **actor GSA**             |
| `gke-cluster-creator` skill                     | Retired/adjusted to author YAML + open a PR                                | **Restored**: gathers requirements, grounds on the `cluster-blueprint` OKF, calls `plan_action`, shows the human the classification, calls `submit_action`                                                                                                |
| `submit-suggestion` skill                       | The sole mutation path                                                     | **Deleted.** No branch, no PR, no propose verb anywhere in the agent surface                                                                                                                                                                              |
| `raise-escalation` skill                        | Wrote a `knowledge/escalation/` file; the parent polled for it             | **Replaced** by the mesh call (§7). The OKF type survives only for human-resolved requests (§5)                                                                                                                                                           |
| Brake tools (`pause_self`, `freeze`, `approve`) | —                                                                          | **Never exist.** An agent may not pause itself (it would also be able to resume itself), may not freeze, and may not approve. `undo` is human-invoked (§4.4)                                                                                              |
| `get_action_status` / `list_actions` (new)      | —                                                                          | Read-only over the agent's own `ActionRecord`s — how an agent reports what it did and offers the undo handle                                                                                                                                              |

**Invariants a reviewer can check mechanically** (and §10 does):

- exactly one code path in the agent image performs mutation, and it is an HTTP call to
  `https://<agent>-broker…:8443/v1alpha1/actions`;
- no `kubectl apply|create|patch|delete|scale`, no `gcloud … create|update|delete`, and no mutating
  client-go verb appears anywhere in the agent image or its skills;
- the rendered runtime config (`renderConfigYAML()` → the mounted ConfigMap, mounted
  `readOnly: true`) exposes no mutating remote MCP tool — checked against the **rendered** config,
  never only the baked `agents/<tier>/config.yaml`.

## 10. Verification

> **Indexed in [09](09-verification-and-validation.md) §6.** That document is the
> authoritative index of every check in the set: it assigns each of the checks below a stable
> `V-<SUITE>-<nnn>` ID, a verification level (L0 static → L4 soak), a gate class, and the roadmap
> phase by which it must be green. The suites drawn from this section are **V-CTR, V-CMP**. This
> section states what to check and why; 09 states how it is run, gated, and proved complete.

Contract-level checks. Security behaviour is verified in [03](03-security-model.md) §11; these
verify that the **shapes** in this document are real.

**Schema round-trips and rejects (§1)**

- Every `Agent` CR in `examples/` and `deploy/` validates against the generated CRD; a round-trip
  (`kubectl apply` → `get -o yaml` → re-apply) is a no-op diff.
- **V-1…V-10 each have a negative test**: wrong tier enum; tier mutation; missing per-tier scope
  field; missing `parentRef`; a developer-team `Agent` in the wrong `metadata.namespace`; a second
  CR for the same `(tier, scope)`; **a child whose scope is not a strict subset of its parent's, and
  a child whose parent is the wrong tier (V-6)**; an enabled chat integration with an empty
  `allowedUsers`; an `initiativeBudget` above the code ceiling; a
  `spec.security.serviceAccountName` that is not the tier's reader SA. Each is rejected at apply
  time with the field path in the message.
- **No authority fields:** a CR carrying `spec.rbac`, `spec.rules`, `spec.riskClass`,
  `spec.scopeOverride`, `spec.brokerServiceAccountName`, or `spec.actorServiceAccountName` is
  pruned/rejected; a test greps the generated CRD schema to assert none of those property names
  exists and that `spec` sets no `x-kubernetes-preserve-unknown-fields`.

**Identity templates match what admission enforces (§2)**

- For each of the six identities, the rendered manifest exists, carries
  `kube-agents/{tier,role[,scope]}`, and is referenced/derived correctly (reader by
  `spec.security.serviceAccountName`; actor by the derivation, surfaced in
  `status.broker.actorServiceAccount`).
- **Templates ↔ policy agreement:** for every rule in every §2.2 actor template,
  `vap-agent-scope` **admits** it; for a mutated copy of each template with one extra
  apiGroup/resource/verb, `vap-agent-scope` **denies** it. This is the check that keeps the document
  and the policy from drifting — run it as a table test over all three tiers.
- `kubectl auth can-i --as=system:serviceaccount:…`: every reader returns **no** for
  `create|update|patch|delete` on everything, universally; every actor returns **yes** in scope for
  its templated resources and **no** out of scope, for `escalate`/`bind`/`impersonate`, and for
  writes to `changepolicies`/`fleetfreezes`/`approvalrosters`. On `actionrecords` an actor returns
  **yes** for `create` and for `update` on `actionrecords/status`, and **no** for `update`/`delete`
  of the record itself (§2.2.1) — the append-only property, asserted in both directions. On
  `agents` a non-platform actor returns **yes** only for creating/patching a **child** CR within
  its scope, and **no** for its own CR, a parent's, and for `spec.operations.paused` on any CR
  ([03](03-security-model.md) §3.3 rule 3).
- Cloud: no actor GSA holds `roles/owner`, `roles/editor`, `roles/iam.securityAdmin`,
  `roles/resourcemanager.projectIamAdmin`, or `iam.serviceAccounts.setIamPolicy`; every actor GSA
  binding carries the scope IAM condition from §2.3.

**Envelope (§4.1)**

- A valid envelope round-trips to an `ActionResponse` with a resolvable `actionRecordRef`.
- **Scope spoofing is rejected:** an envelope carrying a top-level `scope`, `tier`, `actor`,
  `riskClass`, `approved`, `bypass`, `force`, `skipJournal`, or `undoPlan` is refused `400`/`403`,
  executes nothing, and (for the reserved and bypass families) produces a `Rejected`
  `ActionRecord` plus a security event. A developer-team agent's envelope naming a target in
  another namespace is refused, whatever the body claims.
- **Atomicity:** a two-operation envelope with one out-of-scope target applies **neither**.
- **Idempotency:** the same `idempotencyKey` submitted twice within the window yields one
  `ActionRecord` and `decision: deduplicated`.
- **Unknown fields** are refused, not dropped (assert the error names the field).

**Classification (§4.2)**

- The three worked examples classify exactly as documented (`routine`, `elevated`, `gated`), with
  the named rules in `classification.reasons`.
- **Prose cannot move a class:** the same envelope with an `intent`/`rationale` asserting the action
  is safe, pre-approved, and urgent classifies identically — byte-for-byte the same
  `classification` block.
- **Stricter-only:** a `ChangePolicy` raising a class takes effect; one attempting to lower a class
  is rejected at admission; a hand-crafted policy object that somehow lowers a value has no effect
  because the broker takes the maximum (test both).
- No actor SA can create, update, or delete a `ChangePolicy`.

**`ActionRecord` and undo (§4.3)**

- **Undo-plan generation per verb:** `create` → `delete`; `apply`/`patch` on an existing object →
  `restore`; `apply` on a new object → `delete`; `scale` → `restore`; `delete` of a reconstructable
  object → `recreate`; cloud `setSize` → `inverse`. Each generated plan is dry-run-validated and
  then **actually replayed**, and the resulting object diffed against the recorded snapshot.
- **Unrevertible ⇒ gated:** deleting a bound PVC, deleting a namespace, and rotating a credential
  each produce `undoable: false` and park as `PendingApproval` — never execute.
- **Sanitizer:** a restore step contains no `resourceVersion`, `uid`, `managedFields`, or `status`,
  and a `Secret`'s values are digested in the CR while remaining restorable from the journal store.
- **Immutability:** `spec` updates and `delete` on an `ActionRecord` are rejected for every agent
  identity, including the actor that created it.
- Lifecycle: every phase in the §4.3 table is reachable in a test, and a `gated` action left past
  its roster TTL becomes `Expired`, not approved.

**Brake (§4.4)**

- `pause` stops the agent mid-queue; the in-flight action lands or rolls back, never half-applied.
- `resume` requires roster membership; `pause` and `undo` require only `allowedUsers`.
- A `FleetFreeze` blocks the scope; `allowUndo: true` still permits undo; **making the freeze object
  unreadable freezes the scope** rather than opening it.
- Approvals: `allowSelfApproval: false` refuses the requester's own approval; `minApprovals: 2`
  requires two distinct approvers; an empty roster never auto-approves.
- `contested`: an undone change is not re-applied; only a roster member can clear the marker.
- All of pause / freeze / undo / status work with the inference endpoint and the agent pod down.

**Mesh (§7)**

- A parent→child `delegate` succeeds; the callee's `ActionRecord` names the **callee's** actor SA,
  not the caller's.
- **Re-authorization:** a request that would be `gated` for the callee waits for the **callee's**
  roster even though the caller is a parent holding broader authority.
- **Lineage:** sibling and cross-tree calls are refused `not-in-lineage`; `from` is overwritten from
  the authenticated identity (a forged `from` changes nothing).
- **Loop prevention:** a request whose `visited` already contains the callee is refused
  `loop-detected`; `depth >= 4` is refused; an absent or malformed `chain` is refused, not
  defaulted; a deliberately constructed A→B→C→A cycle terminates at the first repeat.
- A paused or frozen callee returns `paused`/`frozen` + `retryAfterSeconds`, and the caller does not
  route around it.

**Audit, repo, OKF, MCP (§3, §5, §8, §9)**

- **Trace continuity:** for a sampled chat-initiated action, one `traceId` links the chat audit
  record, the envelope, the `ActionRecord`, and the Kubernetes/Cloud audit entry.
- Every object written by an actor SA carries `kube-agents/action-id`; a write with the annotation
  stripped is **rejected at admission**; deletes and cloud calls are correlated by the broker
  user-agent.
- **Repo layout** matches §3, `journal/<Y>/<M>/<D>.ndjson` parses, and — the retirement check —
  no `submit-suggestion` skill, propose branch prefix, or applier workflow remains.
- **Mirror is not a control path:** with the mirror enabled, a hand-authored commit to the mirrored
  paths changes nothing in the cluster; a mirror-push failure does not fail, delay, or revert the
  action.
- **OKF:** every `knowledge/` file carries a valid `type` and resolving links; `observation` files
  written after an action carry `actionRefs` that resolve to real `ActionRecord`s.
- **MCP:** exactly one mutation path exists in the agent image (the broker HTTP call); no mutating
  `kubectl`/`gcloud`/client-go verb appears in the image or its skills; the **rendered** runtime
  config exposes no mutating remote MCP tool and is mounted `readOnly: true`; `plan_action` with
  `dryRun: true` returns a classification and undo plan and writes nothing.
