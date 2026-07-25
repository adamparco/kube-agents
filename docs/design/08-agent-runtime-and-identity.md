# Design 08: Agent Runtime & Identity

**Status:** ✅ Agreed

**Overview:** [README.md](README.md) · **Depends on:** [02](02-agent-personas.md),
[03](03-security-model.md), [04](04-workflow-model.md), [06](06-api-and-data-contracts.md) ·
**Feeds:** [07](07-implementation-roadmap.md) · **Tier:** Buildable (bridging)

---

## TL;DR

The **kube-agents controller** (the existing Kubebuilder operator in `k8s-operator/`, extended)
reconciles each tier-discriminated **`Agent` CR** into **two workloads, always as a pair**:

- the **agent pod** — the **Hermes** harness, bound to the **read-only `<tier>-agent` reader SA**;
  this is where the LLM runs, and it holds **no write credential of any kind**;
- its **Action Broker** — a deterministic Go service with no model in it, bound to the
  **scoped-write `<tier>-<scope>-actor` actor SA**; this is the **only** process in the system that
  writes to a cluster or cloud API ([03](03-security-model.md) §4).

They are two workloads because **a Kubernetes pod has exactly one ServiceAccount**. The
reader/actor split of [03](03-security-model.md) §3.1 is therefore not a code convention that could
be bypassed by a clever prompt — it is a **process and network boundary the kubelet enforces**. The
agent reaches its broker over an in-cluster **Service** with **mTLS plus a `TokenReview`** of an
audience-bound projected token, and the broker derives `(tier, scope)` from the **authenticated
caller**, never from the request body. If the broker is unavailable the agent **fails closed** — it
keeps observing and reporting and registers no write tools; there is no fallback path, because the
reader SA cannot write anyway.

The controller still **mints no identity**: both SAs are pre-created, GitOps-managed manifests it
references **by name**, derived from `tier` + `scope` ([03](03-security-model.md) §3.4). It owns
workload lifecycle, `(tier, scope)` cardinality, placement, the **cross-object child ⊆ parent
ceiling webhook** (v1-required as of [03](03-security-model.md) §4.2), the pod labels
(`kube-agents/tier` / `scope` / `parent` / **`role: reader|actor`**), and the hardened pod-security
context. The **Scion** launch-primitive seam is retained, still gated off, native build the default.
Deferred: the untrusted-code-execution sandbox (§5.1) and per-request user down-scoping (§5.2).
Trade-offs in §4 — the inversion buys capability and **spends** several of the previous
generation's simplifications; §3 says which.

---

## 1. What this doc decides

Runtime packaging, deployment topology, and identity realisation for the personas in
[02](02-agent-personas.md) — _how each agent actually runs, authenticates, and acquires the ability
to act_. Concretely, this doc decides:

1. That an `Agent` CR reconciles to **two workloads** (agent + broker), not one, and why the
   Kubernetes ServiceAccount model forces that (§2.1, §2.2).
2. How the pair is kept in **lockstep** — creation order, readiness, drift, deletion, `pause`
   (§2.4).
3. The **transport and authentication** between an agent and its broker (§2.3).
4. What the agent does when its broker is **gone** (§2.4 — fail closed, always).
5. The **labels, security context, and placement** the controller stamps, and what keys off them
   (§2.5, §2.6).
6. The **controller's own authority** — what it may do, and the fact that it mints **no** RBAC or
   identity (§2.7).
7. Which hardening remains **deferred**, and which items the inversion **promoted to v1** (§3, §5).

It does **not** decide the broker's internal pipeline, risk classifier, or journal schema — those
are [03](03-security-model.md) §4/§5 and [06](06-api-and-data-contracts.md) §4. This doc is how the
runtime _realises_ them.

## 2. The solution

### 2.1 One `Agent` CR → two workloads, one identity each

| Workload          | Deployment        | ServiceAccount          | Image                                     | Contains                                                                  | Authority                                                                                         |
| ----------------- | ----------------- | ----------------------- | ----------------------------------------- | ------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| **Agent pod**     | `<agent>-gateway` | `<tier>-agent` (reader) | `<tier>-agent:<tag>` (baked per tier)     | Hermes harness, `SOUL.md`, skills, cron, ChatOps                          | **Read-only**, scoped to the tier. No write verb, anywhere.                                       |
| **Action Broker** | `<agent>-broker`  | `<tier>-<scope>-actor`  | `kube-agents-broker:<tag>` (tier-neutral) | Deterministic Go: classify → gate → snapshot → execute → verify → journal | **Read-write within the tier's scope**, minus the forbidden set ([03](03-security-model.md) §3.3) |

Both names are **derived from `tier` + `scope`** and looked up by name; neither is settable in a way
that could widen authority ([03](03-security-model.md) §3.4). The `Agent` CRD carries **no** field
naming an actor SA. (Delta: today's `spec.security.serviceAccountName` override applies to the
**reader** only and is constrained by admission to the tier template's name pattern;
[07](07-implementation-roadmap.md) converges the code.)

The broker image is **one tier-neutral binary** configured by tier/scope/template, not a per-tier
build. That keeps the highest-value credential holder in the system on the **smallest possible
supply chain**: no model, no chat surface, no plugin loader, no untrusted-input parser beyond the
Action Envelope schema.

**One broker per `Agent` CR.** There is no shared or fleet-wide broker. The blast radius of a broker
compromise is **exactly one scope** ([03](03-security-model.md) §3.1) — and that property is
structural, not configurational: the broker Deployment is owned by its `Agent` CR, and the
`(tier, scope)` cardinality webhook already guarantees one CR per scope.

### 2.2 Why two pods and not one pod with two containers

This is the load-bearing mechanical fact, so state it plainly: **a pod has exactly one
`spec.serviceAccountName`, and every container in that pod shares it.** A sidecar broker would run
under the _same_ identity as the harness. Whichever SA you chose, one of the two invariants would
break:

- bind the pod to the **reader** SA → the broker cannot write, so nothing can act;
- bind the pod to the **actor** SA → the LLM's container holds a write credential, and the entire
  "the agent pod holds no write credential" guarantee ([03](03-security-model.md) §3.1) evaporates.
  A projected token is a file on a shared kubelet mount and the pod network namespace is shared; a
  prompt-injected process with a shell would simply read the token and call the API server directly,
  skipping classification, gating, snapshotting, and the journal.

Splitting containers is not enough either: containers in a pod are not a security boundary for
credentials. **Separate pods** give separate ServiceAccounts, separate token mounts, separate
network identities for mTLS and NetworkPolicy, and separate restart/OOM domains. The extra pod is
the price of making [03](03-security-model.md) §3.1 an enforced property rather than an assertion.

The consequence to accept: because the two are separate pods, the agent→broker call is a **network
call, not a loopback**, and must be authenticated as such (§2.3).

### 2.3 The agent → broker path

- **Service.** The controller reconciles a headless-capable ClusterIP Service `<agent>-broker` in
  the agent's namespace, port `8643/TCP` (`envelope`), selecting `kube-agents/role: actor` +
  `kube-agents/agent: <cr-name>`. The agent's own Service (`<agent>`, `8642`) is unchanged.
- **Discovery.** The controller injects `KUBEAGENTS_BROKER_ENDPOINT` =
  `https://<agent>-broker.<ns>.svc.cluster.local:8643` into the agent container. The agent does not
  discover brokers dynamically and cannot be pointed at another agent's broker: the endpoint is
  operator-rendered config on a read-only mount, and a foreign broker would reject the caller in any
  case (below).
- **mTLS.** Both ends present certificates issued per-agent from the cluster's issuer
  (cert-manager, or the Kubernetes CSR API where cert-manager is not installed), mounted as a
  Secret. The broker requires client auth; the agent pins the broker's SAN.
- **`TokenReview`.** On top of mTLS the agent presents a **projected ServiceAccount token with
  audience `kubeagents-broker`** (a `serviceAccountToken` projected volume, short TTL, auto-rotated
  — _not_ the default API-server token). The broker calls `TokenReview` and extracts the caller's SA
  identity.
- **Scope derivation.** The broker maps the authenticated SA to `(tier, scope)` from its own
  configuration and **ignores any tier/scope in the envelope** ([03](03-security-model.md) §4.1
  step 1). It accepts exactly one reader identity — its own agent's. An envelope from any other
  caller is rejected as `forbidden` and raises a security event.
- **NetworkPolicy.** The broker accepts ingress **only** from pods labelled
  `kube-agents/role: reader` with a matching `kube-agents/agent`; the human brake and journal reads
  arrive via the API server and the journal store, not via this port. Agent-pod egress remains
  default-deny with the [03](03-security-model.md) §9 allowlist, plus its broker.

Two layers (mTLS **and** `TokenReview`) is deliberate: mTLS binds the transport, the token binds the
**Kubernetes identity** the broker actually authorizes on, and neither alone survives a stolen
certificate or a spoofed pod IP.

### 2.4 Lockstep, ordering, the brake, and failing closed

- **Reconciled as a pair.** One `Reconcile` renders the broker Deployment, Service, and certificate
  Secret **before** the agent Deployment, with both owned by the `Agent` CR via `OwnerReference`. An
  `Agent` never reaches `Ready` with only one of the two present; the CR's status carries separate
  `BrokerReady` and `AgentReady` conditions, and `Ready` is their conjunction. Drift on either is
  re-applied (server-side apply, field manager `agent-controller`), and deleting the CR garbage-
  collects both. Both SAs are left alone — they are GitOps-managed, not owned (§2.7).
- **Startup ordering is safe in both directions.** The agent pod runs a `wait-for-broker` init
  container that polls the broker's `/healthz` with a bounded timeout. On success the agent starts
  with its write tool surface registered. **On timeout it starts anyway, in observe-and-report
  mode** — a broker outage must not blind the fleet, and an agent that can only read is exactly as
  safe as the previous generation's agent. A broker that starts before its agent simply has no
  caller; it never initiates work.
- **Fail closed, structurally.** The agent's only write tool is the broker client. If the broker is
  unreachable the tool returns a hard error, the agent reports the outage, and the work item stays
  queued. **There is no degraded direct-write path — not because the harness is well-behaved, but
  because the reader SA has no write verb to fall back to** ([03](03-security-model.md) §11). "Ask
  a human to run `kubectl` instead" is the correct behaviour here and _only_ here; everywhere else
  it is a defect ([README](README.md) invariant 1).
- **`pause` and `freeze` do not depend on the controller.** The broker watches its own `Agent` CR
  (`spec.operations.paused`) and the cluster-scoped `FleetFreeze` object directly, and fails closed
  if it cannot read either ([03](03-security-model.md) §6). A paused agent's **pod keeps running** —
  it still observes and reports; only its broker refuses envelopes. Never implement `pause` by
  scaling the agent Deployment to zero: that would destroy the work queue the brake is specified to
  preserve, and it would make the brake depend on the controller being healthy.
- **Cardinality, placement, and the ceiling** are enforced by the controller's validating webhook:
  one `Agent` per `(tier, scope)`; a developer-team `Agent` must live in the namespace it scopes
  (`metadata.namespace == spec.scope.namespace`, so the pod lands inside the isolation controls that
  select on that namespace); and — **new in v1** — a child `Agent`'s scope must be a **strict subset
  of its `parentRef`'s** ([03](03-security-model.md) §4.2). The ceiling check needs cross-object
  reads that CEL cannot express, and the webhook server already exists, so it lands here.

### 2.5 Labels the controller stamps

Stamped on both Deployments and both pod templates:

| Label                  | Value                                                                                                 | Keyed on by                                                                                                                                                                                                                                                  |
| ---------------------- | ----------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `kube-agents/tier`     | `platform` \| `cluster-admin` \| `developer-team`                                                     | Per-tier egress NetworkPolicy; `vap-agent-scope` RBAC selection ([03](03-security-model.md) §4.2)                                                                                                                                                            |
| `kube-agents/scope`    | DNS-safe rendering of the scope key (`<project>.<cluster>.<ns>`, truncated + hash-suffixed when long) | Per-scope network and quota policy; operator queries; journal correlation                                                                                                                                                                                    |
| `kube-agents/parent`   | `parentRef.name` (empty for platform)                                                                 | Provisioning lineage; the attenuation/ceiling checks; blast-radius queries                                                                                                                                                                                   |
| **`kube-agents/role`** | **`reader` \| `actor`**                                                                               | **The admission policies.** `vap-agent-pod-hardening` uses it to assert that a pod bound to an actor SA carries `role: actor` (and vice-versa); `vap-agent-scope` uses it to distinguish reader RBAC from actor RBAC ([03](03-security-model.md) §4.2, §4.3) |
| `kube-agents/agent`    | the `Agent` CR name                                                                                   | Pairing an agent to its broker: Service selector, NetworkPolicy, "show me both halves" queries                                                                                                                                                               |

`kube-agents/role` is the one the inversion adds, and it is load-bearing rather than cosmetic: it is
how a cluster-wide policy can say "no pod carrying `role: reader` may mount an actor token" and "no
pod outside this label set may bind an actor SA at all" without enumerating agent names. The full,
unnormalized scope stays authoritative in `spec.scope`; the label is a selector, never an
authorization input.

RBAC objects are **not** labelled by the controller (it does not own them) — the render overlay
stamps `kube-agents/tier` and `kube-agents/role` and names them `<tier>-agent` / `<tier>-<scope>-actor`,
which is the selection convention `vap-agent-scope` keys on ([06](06-api-and-data-contracts.md) §2).

### 2.6 Pod hardening, placement, and the Scion seam

- **Hardened pod-security context by default, on both pods:** non-root (`runAsNonRoot`, UID 10000),
  seccomp `RuntimeDefault`, no privilege escalation, dropped capabilities. The broker additionally
  runs with a **read-only root filesystem**, no shell in the image, and no volume mounts other than
  its certificate Secret and its projected token — it has no reason to write to disk, and the
  smaller its runtime surface, the better, since it is now the highest-value target in the system
  ([03](03-security-model.md) §8).
- **Placement:** Platform → hub; Cluster Admin → its cluster; Developer Team → its namespace
  ([05](05-system-architecture.md) §3), derived from `tier` + `scope`. The broker is always
  co-located with its agent in the same namespace — cross-namespace ownership breaks garbage
  collection, and a namespaced SA can only be bound to a pod in its own namespace.
- **`runtimeClassName`** stays an optional per-agent field on the agent pod, validated against the
  cluster's `RuntimeClass`es and surfaced as a `Degraded` status when absent. It is the hook the
  deferred execution sandbox will use (§5.1).
- **Scion launch seam, unchanged and still gated off.** Pod construction goes through the
  `PodLauncher` interface (`k8s-operator/internal/controller/pod_launcher.go`): the **native build
  is the v1 default**, and the Scion launch primitive
  ([GoogleCloudPlatform/scion](https://github.com/GoogleCloudPlatform/scion) `pkg/api/types.go`,
  `pkg/runtime/k8s_runtime.go`) sits behind the `KUBEAGENTS_SCION_LAUNCH` gate with a mandatory
  native fallback and an availability probe. The inversion adds one requirement to that seam: the
  `LaunchSpec` contract must be able to express **both** members of the pair, so a future Scion path
  cannot launch an agent without its broker.

### 2.7 The controller mints no identity — and what it may do instead

The controller's own RBAC is deliberately narrow and, critically, **contains no verb that grants
authority to anything**:

- **May:** get/list/watch/create/update/patch/delete `Deployments`, `Services`, `ConfigMaps`, and
  `PersistentVolumeClaims` in `kubeagents-system` and each agent's placement namespace; read
  `ServiceAccounts`, `Namespaces`, `Nodes`, `Pods`, `Events`, `RuntimeClasses`; manage `Agent` CRs
  and their status; run its webhooks.
- **May not:** create or modify **any** `Role`, `ClusterRole`, `RoleBinding`, `ClusterRoleBinding`,
  ServiceAccount, IAM binding, or Workload-Identity binding. It cannot annotate a KSA either — the
  Workload-Identity annotation is part of the pre-created manifest, not something the runtime
  applies. It has **no** write access to tenant workloads, cloud resources, or `ActionRecord`s.

Both SAs, their RBAC, and their Workload-Identity bindings are ordinary manifests rendered from the
**tier template** and applied by the customer's pipeline ([06](06-api-and-data-contracts.md) §2,
[03](03-security-model.md) §4.2). Nothing grants RBAC at runtime. The controller consumes identity;
it never produces it. That is what keeps "an agent cannot widen its own authority" true even if the
controller itself is buggy — though not if it is fully compromised, which §4 states honestly.

## 3. Deliberately out of scope — and what the inversion spent

The previous version of this doc drew most of its simplicity from a single fact: **agents could not
write**. That fact is gone, so the ledger has to be re-drawn honestly.

**Still out of scope in v1 (each additive, each in the §5 path):**

- **A co-located multiplexer** — multiple personas sharing one pod. Still rejected, and the
  reader/actor split makes it _worse_, not better (§5.2).
- **A multi-tenant / fleet-wide broker.** Explicitly rejected for v1 (§5.2): it would recreate the
  single fleet-wide writer the whole design exists to avoid.
- **Per-run ephemeral downscoped tokens** for interactive or cron runs. The broker's per-action
  scope resolution covers the property these were protecting.
- **Cron trigger attestation** (external scheduler + signed job manifests). Cron fires inside the
  agent pod at reader authority; anything it wants to change goes through the same broker, same
  gates, same journal — so a self-triggered cron run is not a privilege path.
- **Per-request user-scoped authorization** — the requester's own `SubjectAccessReview` / IAM check
  and the down-scoping of the action to `agent scope ∩ requester permissions`
  ([03](03-security-model.md) §4a). Deferred, with the broker as its natural future host (§5.2).
- **The external authorization gateway as a separate component** ([05](05-system-architecture.md)
  C14) — the broker absorbs its enforcement role.
- **CLI credential shims** — unnecessary from the other direction now: a shell `kubectl apply` in
  the agent pod fails on RBAC, so there is nothing to shim.
- **Untrusted code execution and its gVisor sandbox** — v1 agents reason and submit envelopes; they
  do not run model-generated code (§5.1).
- **A mounted persona profile** — the per-tier baked image (`<tier>-agent:<tag>` built from
  `agents/<tier>/`) remains the v1 packaging.

**Simplifications the inversion spends (no longer available — call them out so nobody budgets for
them):**

| Was simple because…                                             | Now costs                                                                                                                                                                                                                               |
| --------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| One workload per agent                                          | **Two**, reconciled in lockstep, with a Service, certificates, a NetworkPolicy, and a startup ordering contract between them (§2.3, §2.4)                                                                                               |
| "Deny all writes" was one CEL line                              | `vap-agent-scope` must express a **per-tier, per-scope resource allow-list** plus the journal-annotation requirement — materially harder to write and to test ([03](03-security-model.md) §4.3)                                         |
| The controller was thin                                         | It is thinner than the broker, but it now owns the pair, the brake's plumbing, and the **v1-required ceiling webhook** (§2.4)                                                                                                           |
| No in-cluster component held a write credential                 | One does. It is the highest-value target in the system and is hardened accordingly (§2.6, §4)                                                                                                                                           |
| Ambient credentials were safe because the ceiling was read-only | Ambient credentials in the **agent** pod are still safe for exactly that reason; ambient credentials in the **broker** are safe only because no model runs there                                                                        |
| The human PR review was the safety net                          | Replaced by machinery the runtime must actually keep alive: classifier, journal store, undo plans, gates. Journal availability becomes a **runtime dependency**, and the broker fails closed without it ([03](03-security-model.md) §6) |
| The cross-object attenuation webhook could be deferred          | **Required in v1** ([03](03-security-model.md) §4.2). Removed from §5.2                                                                                                                                                                 |

The **ChatOps gateway** ([02](02-agent-personas.md) §2.4, [05](05-system-architecture.md) C15) is
still **not** the deferred co-located multiplexer: it dispatches to separate per-tier agent pods and
never co-locates personas. What changed is that agents may now call each other directly
([02](02-agent-personas.md) §2.3) — and that costs the runtime nothing here, because the callee
re-authorizes in its own scope through **its own broker**, which is precisely the one-broker-per-
scope shape §2.1 already builds.

## 4. Security considerations

### Held — the load-bearing invariants

All of these survive the inversion unchanged, and each is a property of the runtime shape rather
than of agent behaviour:

- **One agent per scope, one broker per agent.** Enforced by the cardinality webhook plus
  `OwnerReference`s. No shared pod, no shared broker, no cross-tenant in-process state: a Developer
  Team Agent's broker **cannot write another namespace**, a Cluster Admin Agent's **cannot reach
  another cluster**, a Platform Agent's **cannot reach another project**
  ([03](03-security-model.md) §3.2).
- **The controller mints no RBAC and no identity.** It references pre-created, GitOps-managed
  manifests by name (§2.7). Nothing grants scope at runtime.
- **Identity derives from `tier` + `scope` alone.** The CRD carries no RBAC-granting, scope-granting
  or policy-loosening field ([03](03-security-model.md) §3.4), so a CR — however authored — cannot
  request authority beyond its tier template.
- **The agent pod holds no write credential.** Not "is instructed not to write" — _holds none_.
  Enforced by the ServiceAccount boundary the kubelet applies (§2.2) and re-checked at admission by
  the reader-SA write denial ([03](03-security-model.md) §4.3). Any write by a reader identity is a
  P1 alarm by construction ([01](01-vision-scope.md) §7 SLI 2).
- **Scope is absolute.** RBAC + cloud IAM + `vap-agent-scope` bound the actor SA; the broker
  scope-checks every target; admission checks again if the broker is bypassed. Three independent
  layers, none of which consults the model.
- **Fail-closed is structural.** Broker down ⇒ observe-and-report. Journal down ⇒ the broker does
  not act. Freeze object unreadable ⇒ the broker does not act (§2.4,
  [03](03-security-model.md) §6).
- **Hardened runtime floor.** Non-root, seccomp `RuntimeDefault`, no privilege escalation on both
  pods; read-only rootfs and no shell on the broker; default-deny egress with an explicit allowlist.

### Traded away — accepted for capability

- **A human no longer previews every change.** The previous design's review gate is replaced by
  classification in code plus after-the-fact review of `ActionRecord`s
  ([03](03-security-model.md) §4.4). For the reversible majority this is a better trade; for the
  irreversible minority it is not, which is exactly why that minority is gated
  ([03](03-security-model.md) §5).
- **A compromised broker can act within one scope.** Previously no in-cluster component could write
  at all. Now one can — bounded to a single scope, subject to admission independently of the broker,
  and required to carry a `kube-agents/action-id` annotation on every write.
- **Injected intent can cause any in-scope action.** A successful prompt injection can make the
  agent submit any envelope the agent was already authorized to submit
  ([03](03-security-model.md) §8.1). The runtime bounds the damage (scope, gates, budgets, undo); it
  does not prevent the class.
- **Higher pod count — roughly 2× the previous design.** Up to two pods per namespace at the
  developer-team tier. An operational and cost trade, not a security one; the broker is small
  (no model, no browser, modest requests) and scales to zero only when its agent does.
- **Standing credentials, not per-run ephemeral.** A compromised broker can use its actor SA for the
  duration of the compromise. Bounded by scope, admission, journal reconciliation, and anomaly
  auto-brake.
- **More moving parts to keep healthy.** Certificates, a Service, an init-container ordering
  contract, and a journal-store dependency are all new failure modes; [05](05-system-architecture.md)
  §8 chaos-tests them.

### Residual risks & mitigations

| Risk                                                                                                          | Bound / mitigation                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| ------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Compromised or injected agent pod**                                                                         | Holds no write credential; can only submit envelopes, each classified, gated, scope-checked, budgeted, journaled and undoable ([03](03-security-model.md) §4.1, §5, §8.1). Worst case is an in-scope, reversible, fully-attributed action — plus an anomaly-rate auto-pause ([03](03-security-model.md) §6)                                                                                                                                                                                                                                                                                                                                                                                     |
| **Compromised broker** (the new high-value target)                                                            | One scope only; no model and no untrusted-input parsing inside it; minimal image, read-only rootfs, no shell; `vap-agent-scope` applies **independently of the broker**, so a rogue broker still cannot write out of scope, touch the forbidden set, or write without an `action-id` ([03](03-security-model.md) §4.3)                                                                                                                                                                                                                                                                                                                                                                          |
| **Compromised controller** — it can bind an actor SA to a pod it creates, which is effectively use of that SA | The sharpest residual risk in this doc, and stated as such. Bounded by: the controller holds **no** RBAC/SA-write verb (it cannot create a _new_ authority, only re-use existing ones); its Deployment/Pod writes are limited to `kubeagents-system` and agent placement namespaces; `vap-agent-pod-hardening` requires the `kube-agents/role` label to match the bound SA class, so a smuggled pod is rejected at admission; every resulting write is still scope-bounded, still needs an `action-id`, and journal-completeness reconciliation flags fabricated ones ([01](01-vision-scope.md) §7 SLI 2). Treat the controller as **in the trust boundary** and review its changes accordingly |
| **Forged or replayed envelope**                                                                               | mTLS + audience-bound `TokenReview`; scope derived from the authenticated caller, never the body; a broker accepts exactly one reader identity; NetworkPolicy admits only its own agent (§2.3)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| **Broker unavailable at a critical moment**                                                                   | Agent degrades to observe-and-report and says so; work queue preserved; no direct-write fallback exists (§2.4). Availability targets and chaos coverage in [05](05-system-architecture.md) §6, §8                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| **Wrong-but-authorized action** (autonomy failure)                                                            | Not a runtime control — verify-then-rollback, initiative budgets, flap detection, `contested` markers ([04](04-workflow-model.md) §4.2, §5), undo and pause ([03](03-security-model.md) §6)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| **Confused deputy** — a trusted human drives the agent beyond their own rights                                | Accepted in v1, and a **larger** trade than in the read-only generation because it now exposes writes. Bounded by trusted-human access, the scope ceiling, and the gated class ([03](03-security-model.md) §4a); per-request down-scoping is the deferred fix (§5.2)                                                                                                                                                                                                                                                                                                                                                                                                                            |
| **Cron self-triggered by a compromised pod**                                                                  | Runs at reader authority; any change goes through the same broker, gates and journal — no privilege path                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |

## 5. Future hardening (only if/when needed)

### 5.1 Untrusted code execution & the execution sandbox (deferred)

Agents will eventually **generate and execute untrusted code** (model-written scripts, ad-hoc
analysis, tool code). That capability is **deferred past v1** — v1 agents reason, decide, and submit
Action Envelopes — but when it lands it must not run in the agent's own pod. This section fixes the
mechanism now so the CRD's `runtimeClassName` hook and the control-loop / execution-sandbox split
([03](03-security-model.md) §8) have a concrete, buildable target.

**Chosen mechanism: gVisor, via
[GKE Agent Sandbox](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/machine-learning/agent-sandbox).**
Of the three practical isolation runtimes — gVisor (userspace kernel), Kata Containers (lightweight
VM), Firecracker (microVM) — **gVisor is the lightest and the most Google-native**:

- **Lightweight.** gVisor's `Sentry` intercepts syscalls in userspace (no VM boot, no guest kernel):
  millisecond-to-sub-second start and ~50–100Mi overhead per pod, versus Kata's guest kernel + VMM
  at ~130–512Mi and 150–300ms cold start. It needs **no nested virtualization** and existing
  container images run unmodified — a near drop-in via `RuntimeClass`. Trade-off: partial syscall
  compatibility and some overhead on syscall-heavy I/O — acceptable for bounded agent code
  execution.
- **Google-native / low-lift.** gVisor is a Google project (the same isolation that sandboxes
  Gemini). **GKE Agent Sandbox** is purpose-built for safely running untrusted, AI-generated code
  and is the only native agent sandbox among the major clouds. It is **open source** (a Kubernetes
  SIG Apps subproject), so it is not GKE lock-in and runs on any conformant cluster. It reuses the
  `runtimeClassName` field the `Agent` CRD **already** exposes: `RuntimeClass` `gvisor` (handler
  `runsc`). Enable it on GKE Standard with a `--sandbox type=gvisor` node pool (`cos_containerd`
  image); on Autopilot request it per-pod. Its `SandboxWarmPool` keeps pre-booted pods so a new
  sandbox is claimable in **under a second** (~300 sandboxes/sec).

**Topology.** The agent's reasoning/control loop stays in its normal pod (allowlisted egress,
read-only reader SA). Untrusted code runs in a **separate, gVisor-sandboxed, air-gapped execution
pod** (default-deny `NetworkPolicy`, **no service-account token at all**, non-root, read-only
rootfs, dropped capabilities) — claimed from a warm pool per run and replenished after. Three
identities, then, and the ordering matters: sandbox (none) → agent (reader) → broker (actor). Code
generated inside the sandbox that wants to change something must exit as **data**, become an
envelope, and take the same brokered path as everything else.

**Known limit (pair, don't rely on it alone).** gVisor stops container escape and host-kernel
exploits; it does **not** constrain what the code does within the permissions it is granted, and a
documented metadata-server escape must be closed with `NetworkPolicy`. The sandbox layers **on top
of** the scope ceiling, the egress allowlist, and the broker pipeline — it does not replace them.

**Why deferred.** v1 agents don't execute untrusted code, so there is nothing to sandbox yet. The
gVisor **node pool already exists** in provisioning today (`make gcp-provision-02-gvisor`,
`INSTALL.md`), and `runtimeClassName` is already a validated CRD field, so the deferred piece is the
**capability plus its wiring**, not the infrastructure. The capability and its sandbox therefore
ship **together**, as a unit, post-v1 — never code execution first, sandbox later. Until then the
runtime floor is the hardened pod-security context (§2.6).

### 5.2 Delegation & co-location hardening (deferred), and what was promoted out of this list

**Removed from this list — the cross-object child ⊆ parent attenuation webhook.** In the read-only
design it sat here as deferred hardening. The inversion **promotes it to a v1 requirement**
([03](03-security-model.md) §4.2): a parent now holds real authority to create children, so "a
parent cannot _express_ an over-grant" is no longer sufficient — it must be unable to _cause_ one.
It is implemented in the controller's existing webhook server and specified in §2.4. It is noted
here explicitly so a reader tracking the change can see where it went.

**Explicitly rejected for v1 — the co-located multi-tenant broker.** Consolidating brokers to reduce
pod count would give one process an identity spanning many scopes, which is precisely the
**fleet-wide writer** the architecture is built to avoid: it would restore a single credential whose
compromise reaches every tenant, and it would move scope enforcement from a Kubernetes identity
boundary into that process's own request-routing logic — a code-correctness property instead of a
platform-enforced one. If pod cost ever forces consolidation, the only acceptable shape is
**one broker process per actor identity**, however they are packed; the identity may not be shared.
The same reasoning rejects a co-located persona multiplexer in the agent pod.

**Still deferred:**

- **Per-request user down-scoping** — authorize each request against the requester's own identity
  (`SubjectAccessReview` for K8s, `testIamPermissions` for GCP) and execute at
  **agent scope ∩ requester permissions** ([03](03-security-model.md) §4a, contract sketch
  [06](06-api-and-data-contracts.md) §2a). The **broker is now its natural host**: it already sits
  outside the LLM loop, already authenticates the caller, and already resolves scope per action, so
  this becomes an additional check in an existing pipeline step rather than a new component. This is
  materially easier to add than it was before the broker existed — and materially more valuable,
  since the gap now exposes writes.
- **Per-run ephemeral downscoped tokens** and **cron trigger attestation** (attested trigger +
  reviewed job manifest).
- **The external authorization gateway** ([05](05-system-architecture.md) C14) as a separate
  component — only if the broker turns out to be the wrong host for the above.
- **An L7 egress proxy** for hostname-precise allowlisting ([03](03-security-model.md) §9).

None are required for v1; each is additive and can be adopted independently.

## 6. Goals & non-goals

### Goals

- Realise [03](03-security-model.md) §3.1's reader/actor split as an **enforced Kubernetes
  boundary** — two workloads, two ServiceAccounts, authenticated in between — rather than a
  convention inside one process.
- Make **acting** the normal path and **failing closed** the only alternative: no degraded
  direct-write mode exists, in any failure mode, by construction.
- Keep the runtime **incapable of granting authority**: the controller references pre-created
  identity derived from `tier` + `scope`, and mints nothing.
- Keep the credential-holding component **small, deterministic, and single-scope** — one broker per
  `Agent` CR, no model inside it, no fleet-wide writer anywhere.
- Give the platform the labels and admission hooks (`kube-agents/role` chief among them) that let
  cluster-wide policy express the invariants without enumerating agents.
- Preserve the hardened, per-pod-identity model verified in **Scion** and the gated launch-primitive
  seam, without making v1 depend on it.
- Document the trade-offs **honestly**, including what the inversion spent, with an explicit
  upgrade path (§3, §5).

### Non-goals

- Specifying the broker's internal pipeline, risk classifier, undo-plan generation, or journal
  schema — [03](03-security-model.md) §4/§5 and [06](06-api-and-data-contracts.md) §4.
- Per-request user-scoped authorization, per-run ephemeral tokens, or co-location in v1 (§5.2).
- Untrusted code execution and its sandbox in v1 (§5.1).
- Framework portability beyond the Hermes runtime ([02](02-agent-personas.md) §9).
- Deploying Scion as a standalone per-cluster orchestrator (its K8s runtime is early; the controller
  owns lifecycle in v1).

## 7. Verification

> **Indexed in [09](09-verification-and-validation.md) §6.** That document is the
> authoritative index of every check in the set: it assigns each of the checks below a stable
> `V-<SUITE>-<nnn>` ID, a verification level (L0 static → L4 soak), a gate class, and the roadmap
> phase by which it must be green. The suites drawn from this section are **V-RUN, V-CTN**. This
> section states what to check and why; 09 states how it is run, gated, and proved complete.

Runtime-level checks. Marked **(carried)** where the check survives the inversion unchanged,
**(inverted)** where it replaces a read-only-generation check, **(new)** where the two-workload
model creates it. Security-property tests live in [03](03-security-model.md) §11; these prove the
runtime _shape_.

**The pair**

- **(new) Two workloads, and no more.** For each `Agent` CR, exactly one `<agent>-gateway` and one
  `<agent>-broker` Deployment exist, both with an `OwnerReference` to the CR; the controller creates
  no third workload and no ServiceAccount. Deleting the CR removes both and leaves both SAs intact.
- **(new) Correct identity on each.** The agent Deployment's `spec.serviceAccountName` is the
  `<tier>-agent` reader SA; the broker Deployment's is the `<tier>-<scope>-actor` actor SA. Neither
  is settable to the other's value (admission rejects).
- **(carried) Cardinality:** a second `Agent` CR for the same `(tier, scope)` is **rejected** by the
  controller's validating webhook. A developer-team `Agent` whose `metadata.namespace` differs from
  `spec.scope.namespace` is rejected.
- **(new) Ceiling webhook:** a parent provisioning a child whose scope is not a strict subset of its
  own is rejected ([03](03-security-model.md) §4.2).
- **(carried) Hardened runtime:** both pod templates assert non-root, seccomp `RuntimeDefault`, no
  privilege escalation; the broker additionally asserts a read-only root filesystem and no extra
  volume mounts; `runtimeClassName` (where set) is validated and surfaces `Degraded` when the class
  is absent.

**Identity, negatively**

- **(inverted) The agent SA can write nothing.** Sweep `kubectl auth can-i <verb> <resource>` as the
  **reader** SA across `create|update|patch|delete|deletecollection|escalate|bind|impersonate` × a
  representative resource set (including `*`/`*`, cluster and namespace scoped, in-scope and
  out-of-scope): **every** answer is `no`. Reads return `yes` only inside the tier scope.
- **(new) The broker SA can write only its tier template, within scope.** As the **actor** SA,
  `create|update|patch|delete` returns `yes` for templated resources inside the scope and `no` for
  every out-of-scope target, every non-templated resource, and every forbidden-set object
  ([03](03-security-model.md) §3.3).
- **(carried) The controller mints no RBAC.** Its `ClusterRole` contains no verb on
  `roles`/`clusterroles`/`rolebindings`/`clusterrolebindings`/`serviceaccounts` beyond
  `get;list;watch` on ServiceAccounts (assert by parsing the generated RBAC, not by inspection); the
  agent SAs, their RBAC, and their WI bindings exist only as pre-created manifests in the repo
  (grep + audit-log check that no runtime principal created them).

**Labels and selection**

- **(new) All four labels are stamped and selectable.**
  `kubectl get pods -l kube-agents/role=reader` returns exactly the agent pods;
  `-l kube-agents/role=actor` exactly the brokers;
  `-l kube-agents/agent=<name>` returns exactly that CR's pair; `kube-agents/tier`,
  `kube-agents/scope`, and `kube-agents/parent` carry the derived values, and the scope label is a
  valid DNS-safe label value for a maximally long scope.
- **(new) The role label is enforced, not decorative.** A pod carrying `kube-agents/role: reader`
  but binding an actor SA (and the converse) is **rejected by `vap-agent-pod-hardening`**.

**Behaviour**

- **(new) Fail closed on broker loss.** Scale the broker Deployment to zero, then ask the agent to
  perform a routine in-scope change. It must: report the broker as unavailable, keep the item
  queued, and attempt **no** direct API write (assert from the audit log — zero write attempts by
  the reader identity). Restore the broker; the change then executes and journals normally.
- **(new) Startup ordering is safe.** (a) Create an `Agent` and delay the broker's image pull: the
  agent pod starts in observe-and-report mode after the init-container timeout and does not crash-
  loop. (b) Start the broker first with no agent: it serves `/healthz`, initiates nothing, and logs
  no envelopes. (c) `Ready` on the CR requires both `AgentReady` and `BrokerReady`.
- **(new) Only its own agent may call a broker.** From agent A's pod, an envelope sent to agent B's
  broker Service is refused (`TokenReview` identity mismatch **and** NetworkPolicy), and the refusal
  raises a security event.
- **(carried→new) `pause` propagates.** Setting `spec.operations.paused: true` causes the broker to
  refuse new envelopes **within seconds and without a controller reconcile** (verify with the
  controller Deployment scaled to zero); the agent pod keeps running and keeps reporting; the queue
  survives; `freeze` behaves the same at scope granularity and fails closed when the `FleetFreeze`
  object is unreadable.
- **(carried) Cron:** a cron-triggered run reads at reader authority and routes every change through
  the broker — never a direct write.
- **(carried) Trusted-human access:** the entrypoint allowlist is enforced before dispatch; there is
  no per-request user permission check in v1 (deferred, §5.2).
