# Design 05: System Architecture

**Status:** ✅ Agreed

**Overview:** [README.md](README.md) · **Depends on:** 01–04 · **Feeds:**
[06-api-and-data-contracts.md](06-api-and-data-contracts.md),
[07-implementation-roadmap.md](07-implementation-roadmap.md),
[08-agent-runtime-and-identity.md](08-agent-runtime-and-identity.md) · **Tier:** Buildable (bridging)

---

## TL;DR

This doc assembles the whole system a builder must stand up. kube-agents is a **hub-and-spoke**
deployment: a **hub cluster** runs the kube-agents controller, the Platform Agent, and shared
services (inference, ChatOps router, GitHub token broker, observability); each **spoke (workload)
cluster** runs a Cluster Admin Agent and hosts Developer Team Agents in their namespaces.

Every `Agent` CR reconciles into **two workloads**: the **agent pod** (Hermes harness, holding the
read-only **reader** identity) and its own **Action Broker** (`C-AB`, deterministic Go, holding the
scoped-write **actor** identity). The broker is the only component in the system that can write to a
cluster or cloud API. It classifies, gates, snapshots, executes, verifies, and journals every
mutation as an **`ActionRecord`** in the journal store (`C-JS`), from which the **undo controller**
(`C-UC`) can replay a recorded undo plan at any time. Agents reach each other directly over the
**agent mesh** (`C-AM`) to delegate and escalate; humans reach the **brake surface** (`C-BR`) to
pause, freeze, or undo.

**Actuation is in-cluster and synchronous.** The customer's CI/CD pipeline is no longer in the
critical path — it survives only as an optional **write-behind IaC mirror** target
([04](04-workflow-model.md) §6). Everything runs in the `kubeagents-system` namespace convention
with telemetry to `gke-managed-otel`.

---

## 1. Component inventory

Component IDs are stable and cited from other documents. IDs `C1`–`C15` are carried over from the
read-only generation with updated responsibilities; `C16`–`C17` name pre-existing components that
previously had no ID; `C-AB`, `C-JS`, `C-UC`, `C-AM`, `C-BR`, `C-AS` are created by the imperative
model.

| #        | Component                                          | Responsibility                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | Tech / basis                                                                                                                         | Status                          |
| -------- | -------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------- |
| C1       | **kube-agents controller (agent runtime)**         | Reconciles each `Agent` CR into **two** workloads — the agent pod (reader SA) and its **Action Broker** Deployment (actor SA) — plus the per-agent `Service`, mesh certificate, and NetworkPolicies. Owns `(tier,scope)` cardinality + placement + child ⊆ parent admission (`C-AS`), label stamping (`kube-agents/tier`, `kube-agents/scope`, `kube-agents/parent`), lifecycle/relaunch, brake fan-out (`C-BR`), and journal export. **Generalizes today's `PlatformAgent` operator** | `k8s-operator/` (Go, Kubebuilder), extended; Scion model ([GoogleCloudPlatform/scion](https://github.com/GoogleCloudPlatform/scion)) | Exists (extend)                 |
| **C-AB** | **Action Broker**                                  | **The only writer in the system.** One per `Agent` CR, bound to that agent's actor SA. Runs the eleven-step pipeline of [03](03-security-model.md) §4.1 as deterministic Go with **no LLM in the loop**: authenticate → validate → resolve scope → classify → check brake → generate undo plan → gate → snapshot → execute → verify → journal. Holds the only cluster/cloud write credential (§1.1)                                                                                    | Go (new binary `k8s-operator/cmd/broker/`), gRPC over mTLS, server-side apply, cloud SDK                                             | New (v1, load-bearing)          |
| **C-JS** | **Journal store**                                  | Durable home of `ActionRecord`s — pre-state snapshot, applied diff, verification result, undo plan, attribution. **Decision: `ActionRecord` custom resources** in the agent's own namespace, continuously exported to the audit sink. Survives the agent and broker pods (§1.2)                                                                                                                                                                                                        | `ActionRecord` CRD (`kubeagents.x-k8s.io/v1alpha1`) + exporter in the controller → Cloud Logging / object sink                       | New (v1, load-bearing)          |
| **C-UC** | **Undo controller**                                | Executes `undo <action-id>` by replaying a recorded undo plan **through the target agent's broker** — including when the originating agent is paused, scaled to zero, or deleted. Watches `UndoRequest` objects and chat/`kubectl` invocations; reports restoration against the recorded snapshot (§1.3)                                                                                                                                                                               | Controller in `k8s-operator/` (reconciles `UndoRequest` → broker call)                                                               | New (v1, load-bearing)          |
| **C-AM** | **Agent mesh**                                     | The direct agent-to-agent call path: **delegation** (parent → child) and **escalation** (child → parent). Replaces the read-only generation's indirect coordination through the repo. mTLS + `TokenReview`; the callee **re-authorizes in its own scope** and never inherits the caller's authority (§1.4)                                                                                                                                                                             | gRPC/HTTPS to the callee's `Service`, terminating on the existing in-pod session-inject seam (`kind: delegation` / `escalation`)     | New (v1)                        |
| **C-BR** | **Brake surface**                                  | `pause` (`spec.operations.paused` on the `Agent` CR), `freeze` (cluster-scoped `FleetFreeze`), `contested` markers, and their propagation to every broker. Must remain effective with inference, the router, and the hub all unavailable (§1.5)                                                                                                                                                                                                                                        | CRD fields + `FleetFreeze` CRD + broker informers + controller fan-out                                                               | New (v1, load-bearing)          |
| **C-AS** | **Admission backstop**                             | The independent, out-of-broker enforcement of [03](03-security-model.md) §4.3: `vap-agent-scope` (in-tree CEL VAP — reader SAs write nothing, actor SAs write only their tier template within scope, no RBAC naming an agent identity, no control-plane/journal tampering, **every actor write must carry `kube-agents/action-id`**) plus the controller's validating webhook (cardinality, placement, **child ⊆ parent ceiling — v1**)                                                | `ValidatingAdmissionPolicy` (K8s ≥1.30) + `k8s-operator/internal/webhook/` + cert-manager                                            | Exists (invert + extend)        |
| C2       | **Platform Agent**                                 | Project/fleet operator; chat entrypoint for platform teams. Reader identity; acts through its own broker over project scope                                                                                                                                                                                                                                                                                                                                                            | Hermes harness (reconciled by C1, `agents/platform/`)                                                                                | Exists (convert)                |
| C3       | **Cluster Admin Agent**                            | Cluster operator; chat entrypoint for cluster admins. Reader identity; acts through its own broker over cluster scope                                                                                                                                                                                                                                                                                                                                                                  | Hermes harness (`agents/cluster-admin/`)                                                                                             | Exists (convert)                |
| C4       | **Developer Team Agent**                           | Namespace operator; chat entrypoint for dev teams. Reader identity; acts through its own broker over namespace scope                                                                                                                                                                                                                                                                                                                                                                   | Hermes harness (`agents/developer-team/`)                                                                                            | Exists (convert)                |
| C5       | **Inference service**                              | Unified Completions API for all agents; **per-tier/per-tenant virtual keys** for budget, rate-limit, and log isolation                                                                                                                                                                                                                                                                                                                                                                 | LiteLLM (hosted models) / vLLM (local GPU)                                                                                           | Exists                          |
| C6       | **GitHub Token Broker (Minty)**                    | Brokers short-lived GitHub App tokens. **Now used only for the optional write-behind mirror (C13) and OKF writes** — it is no longer on any control path                                                                                                                                                                                                                                                                                                                               | GCP KMS + Workload Identity                                                                                                          | Exists (demoted)                |
| C7       | **Customer CI/CD pipeline** _(optional, demoted)_  | **No longer in the critical path at all.** In the read-only generation this was the privileged writer; in the imperative model the broker writes directly. C7 survives only as the customer's own reconciler downstream of the **write-behind IaC mirror** ([04](04-workflow-model.md) §6). kube-agents works with it absent, and must be verified that way (§8)                                                                                                                       | GitHub Actions / CircleCI / Argo / Config Sync / … (customer-provided)                                                               | Customer-provided (optional)    |
| C8       | **IaC artifacts + tooling** _(mirror format)_      | The declarative rendering of **already-executed** state written back to the mirror so a customer's GitOps engine does not fight the broker                                                                                                                                                                                                                                                                                                                                             | **KCC YAML** or **Terraform HCL** (per customer requirements)                                                                        | New (optional)                  |
| C9       | **OKF knowledge base**                             | Durable curated knowledge (SOPs, blueprints, runbooks) under the repo's `knowledge/` root                                                                                                                                                                                                                                                                                                                                                                                              | OKF markdown in git                                                                                                                  | New                             |
| C10      | **mem0 + Qdrant** _(deferred post-v1)_             | Semantic/cognitive recall — **not in v1** ([02](02-agent-personas.md) §2.3)                                                                                                                                                                                                                                                                                                                                                                                                            | mem0ai + Qdrant vector store                                                                                                         | Deferred                        |
| C11      | **Session store**                                  | Per-user runtime session state and the agent's self-generated work queue                                                                                                                                                                                                                                                                                                                                                                                                               | `session_kv.db` + `multiuser_memory` (PVC-backed)                                                                                    | Exists                          |
| C12      | **Observability pipeline**                         | Traces/metrics/logs + attribution; carries the trace ID from chat through the envelope into the `ActionRecord`                                                                                                                                                                                                                                                                                                                                                                         | OTel → `gke-managed-otel` → Cloud Trace/Logging/Managed Prometheus                                                                   | Exists                          |
| C13      | **Mirror repository** _(optional)_                 | Was "the GitOps repository, source of truth for all mutation". Now a **write-behind mirror** of executed state plus the home of OKF (C9). Compromising it cannot cause a cluster change ([03](03-security-model.md) §2)                                                                                                                                                                                                                                                                | Git (GitHub)                                                                                                                         | Exists (demoted)                |
| C14      | **Authorization gateway** _(deferred — hardening)_ | User-scoped authorization (`SubjectAccessReview` + `testIamPermissions`) down-scoping each action to **agent scope ∩ requester permissions** ([03](03-security-model.md) §4a). **Not in v1.** When adopted it lands **inside C-AB**, which already authenticates the caller and resolves scope per action — it is no longer a separate gateway                                                                                                                                         | Broker-hosted check (was: standalone gateway)                                                                                        | Deferred (relocated into C-AB)  |
| C15      | **ChatOps gateway & router**                       | Single chat ingress (Google Chat + Slack): normalizes both platforms, enforces the target agent's `allowedUsers` **before** dispatch, and routes each message to the addressed agent — by slash command, `@<tier>-<scope>` handle, or NL inference as fallback. Also delivers **gate prompts** and **action reports** back to the humans ([04](04-workflow-model.md) §3)                                                                                                               | `k8s-operator/cmd/router/` + `internal/router/`, keyed on `internal/agentindex`                                                      | Exists (extend)                 |
| C16      | **Kubernetes event watcher**                       | Streams, filters, and deduplicates warning events from the API server and injects them into the agent's session seam — the primary **push trigger** for the proactive loop (F6). Already exists and already works; in the imperative model its output ends in a **fix**, not a proposal                                                                                                                                                                                                | `k8s-operator/cmd/k8s-event-watcher/` (sidecar daemon → `127.0.0.1:8699`)                                                            | Exists                          |
| C17      | **Event ingress relay**                            | Delivers non-chat machine push — Cloud Monitoring/Alertmanager alerts over Pub/Sub, GitHub webhooks, and **mesh escalations** — to the same in-pod session-inject seam, under one kind-discriminated delivery contract (`alert`, `github`, `escalation`, `k8s-event`, `delegation`)                                                                                                                                                                                                    | `k8s-operator/cmd/eventingress/` + `deploy/eventingress/`                                                                            | Exists (extend with mesh kinds) |

> **The one component that is gone:** nothing in this inventory is a "privileged writer that acts on
> reviewed state". C7 is optional and downstream. If a design question resolves to "the pipeline
> will apply it", the answer is wrong — the broker applies it.

### 1.1 The Action Broker (`C-AB`) in detail

**Shape.** One `Deployment` named `<agent>-broker`, **single replica**, in the same namespace as its
agent, bound to `ServiceAccount <tier>-<scope>-actor` ([03](03-security-model.md) §3.1). It exposes
one gRPC service on `:8443` (mTLS only) reachable at `<agent>-broker.<ns>.svc`, and a
`:8081/healthz` + `:9090/metrics` on localhost-bound plaintext for the kubelet and the OTel
collector. It is a small static Go binary: no LLM client, no chat client, no untrusted-content
parser, no plugin surface. That minimalism is a security property — this is the highest-value
credential in the system ([03](03-security-model.md) §8).

**Interface.** Three RPCs, all authenticated the same way (§1.1 step 1):

| RPC                           | Caller                    | Purpose                                                                                                                                                               |
| ----------------------------- | ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Submit(ActionEnvelope)`      | The agent pod (reader SA) | Request one mutation. Returns `{actionId, class, phase}` — synchronous for `routine`/`elevated`, `PendingApproval` for `gated`, `Rejected` for out-of-scope/forbidden |
| `Approve(actionId, approver)` | C15 router / C-BR         | Release a parked `gated` action after roster approval ([04](04-workflow-model.md) §3)                                                                                 |
| `Replay(undoPlanRef)`         | C-UC undo controller      | Execute a recorded undo plan as a first-class, re-classified, re-journaled action                                                                                     |

The envelope schema is [06](06-api-and-data-contracts.md) §4.1. The broker **never** accepts a
tier, scope, or risk class from the envelope body — those are derived from the authenticated caller
and from code.

**Pipeline mapping.** The eleven steps of [03](03-security-model.md) §4.1, and where each is
implemented:

| Step            | Implementation                                                                                                                                                                                      |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1 Authenticate  | mTLS peer cert (cert-manager, SAN = the agent's `agentindex` identity key) **and** `TokenReview` of the projected reader-SA token. `(tier, scope)` comes from the CR the SA belongs to              |
| 2 Validate      | Envelope schema + target reference well-formedness; unknown fields rejected                                                                                                                         |
| 3 Resolve scope | Every target resolved to `(cluster, namespace, group/kind/name)` or a cloud resource path and checked against the caller's scope. **One out-of-scope target rejects the whole envelope**            |
| 4 Classify risk | Deterministic classifier over the [03](03-security-model.md) §5.2 inputs, floored by a code constant and only ever tightened by a `ChangePolicy` ([06](06-api-and-data-contracts.md) §4.2)          |
| 5 Check brake   | Informer-cached read of the `Agent` CR `paused` field, the `FleetFreeze` object, the initiative budget counters, the flap cooldown, and any `contested` marker on a target. **Fail-closed** (§1.5)  |
| 6 Undo plan     | Per-verb undo generators (create → delete; update/patch → re-apply pre-state; scale → prior replicas; delete → recreate from snapshot, only where reconstructable). No plan ⇒ reclassify as `gated` |
| 7 Gate          | `gated` ⇒ persist `ActionRecord` in phase `PendingApproval`, notify the roster via C15, start the TTL. Nothing executes                                                                             |
| 8 Snapshot      | `GET` every target at its current `resourceVersion`; store inline if ≤ 256 KiB, otherwise store a digest + sink reference (§1.2)                                                                    |
| 9 Execute       | Server-side apply with field manager `kube-agents/<tier>/<scope>`, `dry-run` first where supported, the `kube-agents/action-id` annotation stamped on every object, cloud APIs via the actor GSA    |
| 10 Verify       | Re-read targets and evaluate the envelope's declared success condition (readiness, observed generation, cloud operation result). Failure ⇒ automatic rollback via the step-6 plan                   |
| 11 Journal      | Transition the `ActionRecord` to a terminal phase with the applied diff, verification result, and validated undo plan; emit the audit event; only then return success to the caller                 |

**Crash safety — the journal is a write-ahead log.** The `ActionRecord` is **created before step 9**
in phase `Executing`, already carrying the snapshot and the undo plan, and only _transitioned_ in
step 11. A broker killed between execute and journal therefore leaves a discoverable record, not an
invisible mutation: on restart the broker reconciles every non-terminal record it owns — re-verify,
then complete or roll back. This is what makes "zero unjournaled mutations" survive a crash rather
than merely a well-behaved code path.

**Concurrency.** Envelopes are admitted concurrently but **serialized per target object** by an
in-process keyed mutex, so two actions never race on the same resource; cross-object ordering is not
guaranteed and envelopes must not depend on it. A single replica is deliberate: the write path wants
one arbiter of ordering and one holder of the credential, and the recovery time (§6) is short enough
that leader-elected replicas buy less than the added complexity and dual-credential exposure cost.

**Why the agent cannot bypass it.** The agent pod's reader SA holds no write verb anywhere, the
per-tier NetworkPolicy allows egress to its own broker but not to the API server's write paths, and
`C-AS` rejects any write from a reader identity — and any write from an actor identity that lacks a
`kube-agents/action-id`. Three independent layers, none of which the model participates in.

### 1.2 The journal store (`C-JS`) in detail

**Decision: `ActionRecord` is a namespaced custom resource** in group `kubeagents.x-k8s.io/v1alpha1`
([06](06-api-and-data-contracts.md) §4.3), created in the **agent's own namespace** — not the
target's. Rationale, in the order that decided it:

- **No new stateful service on the write path.** The broker fails closed when it cannot journal
  ([03](03-security-model.md) §6), so the journal's availability _is_ the system's write
  availability. Any external database would add a second thing that must be up for a pod to be
  restarted — and it would need its own credential, its own backup story, and its own network path
  through a default-deny egress policy. etcd is already required and already up.
- **RBAC-scoped by construction.** Records in the agent's namespace are covered by the same scope
  rules as everything else: a Developer Team Agent's broker can create records only in its own
  namespace, and no agent identity may update a terminal record or delete any record
  ([03](03-security-model.md) §3.3 rule 4, enforced by `C-AS`).
- **Watchable.** `C-UC`, the SLI exporter, and the ChatOps reporter all consume the journal as a
  stream. A watch is free here and awkward everywhere else.
- **Queryable with the tools already in the room.** `kubectl get actionrecords -A` with printer
  columns for age, tier, scope, class, phase, target, and undo validity is the debugging surface,
  with no new client.

**Rejected:** an external Postgres/Spanner (availability coupling above; correct if journal volume
ever outgrows etcd, which the retention policy below is designed to prevent); object storage alone
(not watchable, no per-scope RBAC, no atomic phase transitions); the mirror repository (that is the
previous generation's answer, and it is asynchronous — a git write cannot be the precondition for a
synchronous action).

**Sizing and snapshots.** Pre-state is stored inline when the serialized snapshot is ≤ 256 KiB;
above that the broker writes a content-addressed blob to the export sink and keeps only
`{digest, sinkRef, size}` inline, so no record approaches etcd's 1.5 MiB object limit. Snapshots are
stripped of `managedFields` and of `Secret` `data` (a Secret's pre-state is recorded as a per-key
digest, never as material — undoing a Secret change restores from the digest-matched value only if
the broker still holds it in the sink under the sink's own encryption).

**Retention, TTL, and export.**

| Class      | Guaranteed undo window | In-cluster retention |
| ---------- | ---------------------- | -------------------- |
| `routine`  | 7 days                 | 30 days              |
| `elevated` | 30 days                | 90 days              |
| `gated`    | 90 days                | 180 days             |

The controller garbage-collects terminal records past their retention, and **only after** the
exporter has confirmed the record landed in the audit sink. The **export is the durable record**:
every phase transition is emitted as a structured audit log entry (Cloud Logging → a
retention-locked bucket / BigQuery, or any customer sink) within 60 s, so etcd garbage collection
never destroys the audit trail and the four SLIs (§5) can be computed over a longer window than the
cluster keeps. Undo beyond the guaranteed window is best-effort from the sink and is a human
operation, not a one-command one.

**Durability and shared fate — stated honestly.** For a Cluster Admin or Developer Team Agent the
journal lives in the same etcd as the objects it describes: if that API server is gone, there are no
writes to journal, so the coupling costs nothing. For the **Platform Agent**, whose broker mutates
**cloud** resources, the journal lives in the hub cluster's etcd, which does **not** share fate with
GCP — a hub loss with cloud mutations in flight is exactly the case the write-ahead record and the
60 s export exist for. The journal must survive the agent and broker pods, and it does: records are
owned by the `Agent` CR only for _labelling_, never by `ownerReferences`, so deleting an agent does
not cascade-delete its history. Deleting an `Agent` CR leaves its journal in place for the retention
window, which is what makes `C-UC` able to undo the actions of an agent that no longer exists.

### 1.3 The undo controller (`C-UC`) in detail

`undo <action-id>` must work when the agent that made the change is paused, asleep, or deleted —
otherwise "humans hold the brake" is conditional on the thing being braked cooperating. `C-UC` is
therefore a controller in the kube-agents control plane, **not** a skill and not an agent behavior.

1. A human invokes undo — chat (`/undo <action-id>`, C15), `kubectl create -f` an `UndoRequest`, or
   the one-liner `kubectl kube-agents undo <action-id>`. The requester is authenticated by the same
   `allowedUsers` check as any other command and recorded.
2. `C-UC` loads the `ActionRecord`, validates that the phase is terminal and successful, that the
   undo plan is present and marked valid, and that the guaranteed undo window has not lapsed.
3. It resolves the **target agent's broker** — the same `(tier, scope)` that executed the original.
   If that broker's Deployment is scaled to zero (`scaleToZero`, §3) it scales it to one and waits
   for readiness; if the agent is **paused**, the broker still serves `Replay` — pause stops the
   _agent_ from submitting envelopes, it does not disable the broker. If the `Agent` CR was deleted,
   `C-UC` reconstitutes a broker from the recorded tier template, bound to the same actor SA, for
   the duration of the replay.
4. `Replay` runs the recorded plan through the **full pipeline** — re-authenticated, re-scope-checked,
   re-classified, re-snapshotted, executed, verified, and journaled as its own `ActionRecord` with
   `spec.trigger.undoOf: <action-id>`. An undo that is itself destructive is gated like anything
   else ([03](03-security-model.md) §6).
5. On success `C-UC` diffs the post-undo state against the original pre-state snapshot, records the
   match (or the delta, if the world moved on), sets the **`contested`** marker on every target so
   the agent will not immediately re-apply the change ([04](04-workflow-model.md) §4.2), and reports
   the outcome to the requester.

Undo is never a raw `kubectl apply` of a stored snapshot: routing it through the broker is what
keeps the "every mutation is brokered and journaled" invariant true of undos as well.

### 1.4 The agent mesh (`C-AM`) in detail

The mesh carries **intent between agents** — never authority and never a pre-approved action. A
delegation is a request to consider; the callee decides, and the callee's own broker executes.

**Transport.** gRPC over mTLS to the callee agent's `Service` (`<agent>.<ns>.svc:8443`), or — across
clusters — to the hub's VPC-internal endpoint (§5). Two message kinds, both async
request/acknowledge rather than a held synchronous RPC: the callee acks receipt with a correlation
ID and reports the outcome back over the mesh when its own loop finishes. In-pod, a mesh message
terminates on the **existing session-inject seam** that `C16` and `C17` already speak
(`POST /sessions` + `/sessions/{sid}/inject`, kind-discriminated), so `delegation` and `escalation`
join `alert`, `github`, and `k8s-event` under one delivery contract rather than adding a second one.

**Discovery.** Peers are resolved from `Agent` CRs, never from configuration:

- The controller stamps `kube-agents/tier`, `kube-agents/scope`, and `kube-agents/parent` on every
  agent Deployment, pod, and `Service`. _(Delta: today only `kube-agents/tier` is stamped —
  `k8s-operator/internal/controller/agent_manifests.go`; the other two are required by this design.)_
- Within a cluster, an agent finds its parent or a child by the `agentindex` identity key
  (`tier=…;project=…;cluster=…;namespace=…`, `k8s-operator/internal/agentindex/identity.go`) and
  reads `status.serviceStatus.endpoint` from the resolved CR. A child is any CR whose
  `spec.parentRef` names the caller; the parent is the CR named by the caller's own `spec.parentRef`.
- Across clusters (a spoke's Cluster Admin Agent escalating to the hub's Platform Agent), the parent
  endpoint is the hub's VPC-internal mesh endpoint, published to spokes as part of the same
  bootstrap bundle that carries the inference and Minty endpoints. There are **no cross-cluster
  Kubernetes credentials** — discovery data is configuration written at bootstrap, and authorization
  is per-call.
- The per-tier NetworkPolicy allowlists mesh peers by these labels, so a Developer Team Agent can
  reach its own Cluster Admin Agent and nothing else at that tier.

**Authentication and authorization.**

1. mTLS with per-agent certificates issued by cert-manager; the SAN encodes the caller's identity key.
2. A `TokenReview` of the caller's projected **reader** SA token, presented in the request metadata.
3. The callee derives the caller's `(tier, scope)` from the authenticated identity — **never** from
   the payload — and checks the structural rule: a parent may delegate only into a scope that is a
   strict subset of its own, and a child may escalate only to the CR named in its `parentRef`.
4. **The callee re-authorizes in its own scope, under its own gates.** The delegated intent enters
   the callee's normal loop; anything it decides to do goes to _its_ broker and is classified,
   gated, and journaled there ([03](03-security-model.md) §2, [02](02-agent-personas.md) §2.3). A
   parent cannot cause a child to exceed the child's scope, and a child cannot borrow the parent's.

**Loop and cascade control.** Every mesh message carries a delegation chain ID and a depth counter;
depth > 3 is rejected, and a chain ID already present in the callee's active set is rejected as a
cycle. The chain ID is carried into the trace and into every resulting `ActionRecord`, so a
fleet-wide rollout is one queryable object graph. Delegations count against the **callee's**
initiative budget, not the caller's — a parent cannot spend a child's budget
([04](04-workflow-model.md) §4.2).

**When a peer is down.** Escalations are retried with exponential backoff and held in the escalating
agent's durable queue (C11) with a TTL; on expiry the escalation is surfaced to the human roster
over C15 rather than dropped. A child whose parent is unreachable **keeps operating within its own
scope** — the mesh is not on the child's critical path (§8 CH8).

### 1.5 The brake surface (`C-BR`) in detail

| Control     | Object                                                             | Where enforced                                                                                             |
| ----------- | ------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------- |
| `pause`     | `spec.operations.paused: true` on the `Agent` CR _(new CRD field)_ | The broker refuses new `Submit` calls; the controller stops the agent's trigger sources (C16/C17 delivery) |
| `freeze`    | Cluster-scoped `FleetFreeze` CR, optionally scoped to a subtree    | Every broker consults it on **every** envelope, **fail-closed** if it cannot be read                       |
| `contested` | `kube-agents/contested: <action-id>` annotation on a target        | The broker rejects any envelope re-applying the same change to that target absent explicit instruction     |
| `undo`      | `UndoRequest` (§1.3)                                               | `C-UC` → broker `Replay`                                                                                   |

**Propagation.** Each broker runs informers on its own `Agent` CR and on `FleetFreeze`. A flip
reaches the broker's in-memory state in **< 2 s p99** (§6). Every envelope re-reads from the
informer cache and checks its freshness: if the watch has been broken for longer than the staleness
budget (30 s), the broker treats the brake state as **engaged** and refuses to act. The in-flight
action at the moment of a pause completes or rolls back — it is never left half-applied
([03](03-security-model.md) §6).

**Working when inference is down.** None of the four controls touches the LLM, the router, or the
inference proxy: they are Kubernetes objects read by Go informers. `kubectl patch agent … -p
'{"spec":{"operations":{"paused":true}}}'` and `kubectl apply -f fleetfreeze.yaml` are the
irreducible interfaces, and the chat commands are conveniences over them. This is why the brake
lives here and not in a skill.

**Fleet-wide freeze across clusters.** `FleetFreeze` is a **per-cluster object**, because there are
no cross-cluster Kubernetes credentials (§7). A fleet-wide freeze is fanned out
**controller-to-controller**: each spoke controller subscribes to the hub's freeze channel (the same
Pub/Sub path `C17` already drains) and writes the local `FleetFreeze`, target **< 30 s** to the last
spoke. Two deliberate asymmetries: loss of the _local_ freeze object read is fail-closed (refuse to
act), while loss of the _hub channel_ is **not** — it raises an alarm and holds the last known
state, because a hub network blip must not stop every remediation in the fleet. And a human can
always apply a `FleetFreeze` directly in any cluster with no dependency on the hub, the mesh, or
anything else being alive.

**Auto-brake.** The broker pauses its own agent on repeated `forbidden` attempts, a flap-threshold
breach, an exhausted initiative budget, a verification failure it could not roll back, or loss of
the journal store ([03](03-security-model.md) §6). Auto-pause writes the same CR field a human
would, so recovery is the same one-line operation.

## 2. Topology (hub-and-spoke)

```
        ┌──────────────────────── HUB CLUSTER (kubeagents-system) ─────────────────────────┐
        │  C1 controller + C-AS admission   C15 ChatOps router   C5 inference   C6 Minty   │
        │                                                                                  │
 human ─┼─chat──▶ C2 Platform Agent ──envelope──▶ C-AB broker ──write──▶ GKE / GCP APIs    │
        │           (reader SA)   ◀──report────   (actor SA)  │                            │
        │  C-UC undo ──replay──────────────────────▶          ▼                            │
        │                                          C-JS journal (ActionRecord CRs)         │
        │  C-BR brake (Agent.paused, FleetFreeze)  C12 OTel ──▶ audit sink + SLIs          │
        └───────┬──────────────────────────────────────────────────┬───────────────────────┘
                │ C-AM mesh (mTLS: delegate ▼ / escalate ▲)        │ freeze fan-out, telemetry,
                │ C5/C6 consumed over VPC-internal endpoints       │ inference (hub-dependent)
    ┌───────────┴──────────────────┐              ┌────────────────┴─────────────┐
    │  SPOKE CLUSTER A             │              │  SPOKE CLUSTER B             │
    │  C1 controller + C-AS        │              │  C1 controller + C-AS        │
    │  C3 Cluster Admin Agent      │              │  C3 Cluster Admin Agent      │
    │      └─▶ C-AB broker ─write─▶ cluster API   │      └─▶ C-AB broker ─▶ …    │
    │  C4 Dev Team Agent (ns team-a)              │  C4 Dev Team Agent (ns team-x)│
    │      └─▶ C-AB broker ─write─▶ ns objects    │      └─▶ C-AB broker ─▶ …    │
    │  C-JS journal (local etcd)   C-UC undo      │  C-JS journal   C-UC undo    │
    └──────────────────────────────┘              └──────────────────────────────┘

    optional, off the critical path:  C-AB ──write-behind──▶ C13 mirror repo ──▶ C7 CI/CD
```

**Why hub-and-spoke.** It matches the containment hierarchy and the failure-isolation goal: the hub
owns fleet/project concerns and hosts shared services once; each spoke runs its own Cluster Admin
and Developer Team agents, **each with its own broker and its own journal in its own cluster**.

**The load-bearing property: brokers do not depend on the hub to execute.** Every write path is
local — a spoke broker holds a local actor SA, talks to its own API server, and journals to its own
etcd. Nothing on the execute path traverses the hub. This is deliberate and it is the single most
important consequence of moving actuation in-cluster: if the broker's authority were centralized in
the hub (a fleet-wide writer, or a hub-hosted journal every spoke wrote to), **a hub outage would
block all remediation everywhere at exactly the moment remediation matters most** — and it would
reintroduce the fleet-wide writer that [03](03-security-model.md) §3.1 forbids. What remains
hub-dependent is _reasoning_, not _acting_: with the hub down, a spoke agent cannot call inference
to diagnose something new, but its broker still executes queued and in-flight actions, still
journals them, still honors the brake, and `undo` still works locally (§8 CH4).

> **Alternative considered:** operator-per-cluster with no hub. Rejected as the default because it
> duplicates shared services (inference, Minty, router) per cluster and complicates fleet-wide
> governance. Small single-cluster installs may collapse hub+spoke into one cluster — see §7.
>
> **Alternative rejected outright:** a central broker service in the hub, brokering for the whole
> fleet. It would need cross-cluster write credentials, it would be a fleet-wide writer, and it
> would put the hub on the critical path of every remediation. Three independent disqualifications.

## 3. Deployment placement

Two workloads per `Agent` CR. The broker is always **co-located with its agent** in the same cluster
and namespace, because that is where its scope, its API server, and its journal are.

| Component                                      |                                Hub cluster                                |              Spoke cluster               | Namespace                        |
| ---------------------------------------------- | :-----------------------------------------------------------------------: | :--------------------------------------: | -------------------------------- |
| kube-agents controller (C1) + admission (C-AS) |                                    ✅                                     | ✅ (reconciles that cluster's Agent CRs) | `kubeagents-system`              |
| Platform Agent (C2) + its broker (C-AB)        |                                    ✅                                     |                    —                     | `kubeagents-system`              |
| Cluster Admin Agent (C3) + its broker          |                                     —                                     |              ✅ (1/cluster)              | `kubeagents-system`              |
| Developer Team Agent (C4) + its broker         |                                     —                                     |             ✅ (1/namespace)             | the team's namespace             |
| Journal store (C-JS)                           |                                    ✅                                     |                    ✅                    | records in the agent's namespace |
| Undo controller (C-UC)                         |                                    ✅                                     |                    ✅                    | `kubeagents-system`              |
| Brake surface (C-BR)                           |                       ✅ (`FleetFreeze` + fan-out)                        |         ✅ (local `FleetFreeze`)         | cluster-scoped + the agent's ns  |
| ChatOps router (C15)                           |                                ✅ (shared)                                |            consumed remotely             | `kubeagents-system`              |
| Inference (C5), Minty (C6)                     |                                ✅ (shared)                                |            consumed remotely             | `kubeagents-system`              |
| Event watcher (C16)                            |                      ✅ (sidecar in each agent pod)                       |      ✅ (sidecar in each agent pod)      | with the agent                   |
| Event ingress (C17)                            |                                    ✅                                     |                    ✅                    | `kubeagents-system`              |
| Authorization gateway (C14) _(deferred)_       | — (v1: trusted-human access + scope ceiling; folded into C-AB if adopted) |               — (v1: n/a)                | n/a                              |
| Customer CI/CD (C7) _(optional)_               |                    external, downstream of the mirror                     |                 external                 | n/a (customer-provided)          |
| OTel collector (C12)                           |                                    ✅                                     |                    ✅                    | `gke-managed-otel`               |

**Resource footprint.** The broker is a small static Go binary with no model client: request
`50m / 64Mi`, limit `200m / 256Mi`, one replica, `PodDisruptionBudget` `maxUnavailable: 1`. At the
§6 density target of 200 Developer Team Agents per cluster that is ~10 vCPU / ~13 GiB of _requests_
for the broker fleet — real, but an order of magnitude below the agent pods themselves. Two dials
bound it: an idle Developer Team Agent's `scaleToZero` scales **both** its pod and its broker to
zero (nothing to broker while asleep; `C-UC` scales the broker back up on demand, §1.3), and the
broker's memory is dominated by informer caches, which are namespace-scoped for the dev-team tier.

**One broker per agent — and why not a shared one.** A single multi-tenant broker serving many
agents is **explicitly rejected for v1**. It would have to hold, or be able to assume, the write
authority of every scope it served — which is the definition of the fleet-wide writer that
[03](03-security-model.md) §3.1 exists to prevent. Its compromise, or a single scope-resolution bug
in it, would be a fleet-wide event rather than a one-scope event. Per-agent brokers make the blast
radius of a broker compromise **exactly one scope**, let RBAC do the containment instead of
in-process logic, and let the API server's own admission (`C-AS`) act as an independent second
check, since each broker presents a distinguishable identity. The cost is pod count, which is the
cheaper thing to spend.

**Broker as a separate Deployment, not a sidecar.** v1 runs the broker in its own pod. A sidecar
would share a network namespace, a node, and a lifecycle with the LLM process, putting the actor
token's projected volume one container-escape away from the untrusted-content parser, and coupling
broker availability to agent restarts. The separate Deployment keeps the two identities in two pods
with two SAs and forces the mTLS hop to be real. Co-location remains available later as a footprint
optimization for very high namespace density ([08](08-agent-runtime-and-identity.md) discusses the
seam); it is not the v1 shape.

cert-manager (v1.13+) provides TLS for the controller's **admission webhook** (cardinality,
placement, and the **child ⊆ parent ceiling**, now v1 — [03](03-security-model.md) §4.2) and issues
the **per-agent mesh and broker certificates**, so it is a hard v1 prerequisite (`INSTALL.md`). The
in-tree `vap-agent-scope` `ValidatingAdmissionPolicy` needs no cert-manager but requires
**Kubernetes ≥1.30** (GA) — including the test cluster ([07](07-implementation-roadmap.md) §2).

## 4. Primary data flows

**F1 — The action flow: the universal write path** ([04](04-workflow-model.md) §1,
[03](03-security-model.md) §4.1). Every mutation in the system is this flow; F4, F6, F7, and F8
differ only in what triggers it and where it pauses.

1. Intent arrives at the agent — a chat turn routed by C15, a push trigger from C16/C17, a mesh
   delegation, or an item the agent pulled off its own work queue. Human-initiated intent comes only
   from **trusted, allowlisted humans**; v1 does not check the requester's own permissions
   ([03](03-security-model.md) §4a).
2. The agent reasons over read-only state (F2) and decides on a concrete change. The model's output
   is a **proposal to the broker**, never an API call: the agent pod has no write credential.
3. The agent submits an **Action Envelope** — intent, target references, desired state, success
   condition, requester, trace ID ([06](06-api-and-data-contracts.md) §4.1) — over mTLS to
   `<agent>-broker.<ns>.svc`.
4. The broker authenticates the caller, derives `(tier, scope)` from the **authenticated identity**,
   validates the envelope, and resolves every target against that scope. Out-of-scope ⇒ rejected
   whole; no partial applications.
5. The broker **classifies risk in code** and checks the brake — paused, frozen, budget exhausted,
   flap cooldown, or `contested` all stop here. `forbidden` ⇒ rejected with a security event.
6. The broker **generates the undo plan**. No plan ⇒ the action is reclassified `gated` and the flow
   continues as F8.
7. The broker **snapshots** every target and writes the `ActionRecord` in phase `Executing` — the
   write-ahead record (§1.1).
8. The broker **executes** with the actor identity: server-side apply with field manager
   `kube-agents/<tier>/<scope>` and the `kube-agents/action-id` annotation, cloud calls via the
   actor GSA. `C-AS` independently rejects anything out of scope or unannotated.
9. The broker **verifies** the declared success condition. Failure ⇒ automatic rollback via the
   step-6 plan, and the record terminates as `RolledBack`.
10. The broker **journals** the terminal record — diff, verification result, validated undo plan,
    attribution — and emits the audit event. Only then does `Submit` return.
11. The agent **reports** to the human: what it did, what it observed, and the undo handle. For an
    `elevated` action the owning humans are notified immediately rather than in the digest
    ([03](03-security-model.md) §5.1).
12. _(Optional, asynchronous, off the critical path.)_ The broker renders the executed state as IaC
    and writes it behind to the mirror repository via a Minty-brokered token
    ([04](04-workflow-model.md) §6). Failure here is reported and retried; it **never** rolls back
    or delays the action.

**F2 — Read / observe.** Agents read cluster and cloud state with their **reader** identity
(read-only RBAC + read-only cloud SA) and telemetry from C12. Reads are bounded by the agent's own
tier scope, not by the requester's permissions (v1, [03](03-security-model.md) §4a). Any write
attributed to a reader identity in the audit log is a P1 alarm by construction
([01](01-vision-scope.md) §7 SLI 2).

**F3 — Delegation & escalation over the mesh** ([02](02-agent-personas.md) §2.3,
[06](06-api-and-data-contracts.md) §7). Direct, and re-authorized at every hop.

1. A Platform Agent rolling out a policy resolves its children from their `Agent` CRs (§1.4) and
   sends each a **delegation** carrying intent, a chain ID, and a depth counter — never an envelope
   and never a credential.
2. The callee authenticates the caller by mTLS + `TokenReview`, derives the caller's `(tier, scope)`
   from that identity, and checks the structural rule (delegation must target a strict subset of the
   caller's scope; escalation must go to the declared `parentRef`).
3. The delegated intent enters the callee's **own** loop. It reasons in its own scope, and anything
   it decides to do goes through **its own broker** (F1) — classified, gated, and journaled there,
   against the callee's own budget.
4. The callee acks immediately and reports the outcome back over the mesh with the correlation ID;
   the caller aggregates and reports the rollout to its human.
5. Escalation is the same path upward: a Developer Team Agent that needs a bigger quota escalates to
   its Cluster Admin Agent, which re-authorizes and acts in cluster scope, or escalates onward. If
   the parent is unreachable the escalation queues with a TTL and surfaces to humans on expiry — the
   child keeps operating within its own scope meanwhile.

**F4 — Provisioning cascade** ([02](02-agent-personas.md) §6). Platform Agent → Cluster Admin Agent
→ Developer Team Agent, now **executed rather than proposed**.

1. The parent renders the child's `Agent` CR **plus** the child's reader/actor identities from the
   **tier template**, supplying only `(tier, scope, parent)`. The parent never hand-authors RBAC.
2. The bundle goes to the parent's broker as one envelope. Creating a child agent is at least
   `elevated`, and creating a _cluster_ is `gated` by blast radius.
3. `C-AS` checks the bundle twice: `vap-agent-scope` rejects any rule exceeding the tier template,
   and the controller's **child ⊆ parent webhook** rejects a child whose scope is not a strict
   subset of the parent's.
4. On execution the child's controller reconciles the child agent pod **and its broker**, bound to
   the pre-created SAs. The controller mints no RBAC at runtime ([03](03-security-model.md) §4.2).
5. The whole cascade is journaled as a chain of `ActionRecord`s sharing one chain ID, so
   provisioning a cluster and its tenants is one undoable object graph.

**F5 — Chat ingress & routing (human → agent).** A message from Google Chat or Slack enters C15,
which normalizes the platform, resolves the **target agent** — deterministically from a slash
command or `@<tier>-<scope>` handle, or by NL inference as fallback ([02](02-agent-personas.md)
§2.4) — enforces that agent's `allowedUsers` **before** dispatch, and forwards to the addressed
agent's pod. Routing is a convenience, **never** an authz signal: a mis-route lands only on an agent
the human may already reach, still bounded by that agent's scope ceiling and gates. C15 is also the
**return path** for gate prompts (F8), action reports (F1 step 11), and `undo` invocations (F7). The
turn is audited with requester, resolved agent, and routing mode ([06](06-api-and-data-contracts.md)
§2b). This is human→agent dispatch, **not** agent-to-agent traffic, which uses the mesh (F3).

**F6 — The proactive flow: watch → diagnose → remediate → verify → report, with no human in the
path** ([04](04-workflow-model.md) §4). This is the flow the product exists for.

1. A trigger fires: C16 streams a filtered, deduplicated warning event from the API server; or C17
   relays a Cloud Monitoring / Alertmanager alert or a GitHub webhook; or the agent's periodic drift
   scan finds a delta against policy; or the agent pulls an item off its own improvement queue when
   idle. Push is preferred; the heartbeat is the backstop.
2. The trigger lands on the in-pod session-inject seam and starts an autonomous session — **no human
   is notified yet and none is waited on**.
3. The agent diagnoses using read-only access (F2): object state, events, logs, metrics, traces, and
   the OKF runbook for this signature.
4. The agent decides on a remediation and runs **F1 in full**. Routine and elevated actions execute
   immediately; only the gated class diverts to F8.
5. The broker verifies. If verification fails, the action rolls back automatically and the agent
   climbs the recovery ladder — retry with a different remedy, then escalate over the mesh, then
   escalate to a human ([04](04-workflow-model.md) §5).
6. The agent **reports after the fact**: an immediate notification for `elevated`, the periodic
   digest for `routine`, each carrying the undo handle. Repeated remediation of the same signature
   trips flap detection and the initiative budget rather than continuing
   ([04](04-workflow-model.md) §4.2).

Mean time to remediate, the share of detected issues resolved without a human, and actions per agent
per day are measured off this flow ([01](01-vision-scope.md) §7).

**F7 — The undo flow (human → restored state).** See §1.3 for the component detail.

1. A human runs `/undo <action-id>` in chat, or `kubectl` an `UndoRequest`. The requester is
   authenticated and recorded.
2. `C-UC` loads the `ActionRecord`, validates the phase, the undo plan, and the undo window.
3. `C-UC` resolves and, if necessary, wakes or reconstitutes the **originating scope's broker** —
   this works when the agent is paused, scaled to zero, or deleted.
4. The broker executes the plan through the **full pipeline**: re-authenticated, re-scope-checked,
   re-classified (a destructive undo is gated), re-snapshotted, executed, verified, journaled as its
   own record linked by `undoOf`.
5. `C-UC` diffs the result against the original pre-state snapshot, sets `contested` on the targets
   so the agent will not re-apply the change, and reports the outcome to the requester.

**F8 — The gated flow (park → notify → approve → execute)** ([04](04-workflow-model.md) §3).

1. F1 reaches step 5 or 6 and the classifier returns `gated` — irreversible, high blast radius,
   security-loosening, or no undo plan could be generated.
2. The broker persists the `ActionRecord` in phase `PendingApproval` with the full plan, the
   snapshot, the classification reason, and a TTL. **Nothing is executed and nothing is
   partially applied.**
3. The broker notifies the agent's **approval roster** through C15 — the named humans for that tier,
   not "whoever asked" ([03](03-security-model.md) §4a) — with the diff, the reason it was gated,
   and the expiry.
4. An approver on the roster responds. The approval is an authenticated call to `Approve(actionId,
approver)` on the broker; the approver's identity is recorded in the record. A non-roster human's
   approval is refused, including one from the requester if they are not on the roster.
5. On approval the broker resumes F1 from step 7 — **re-checking scope, class, and the brake first**,
   because the world may have moved while the action was parked. A changed target `resourceVersion`
   invalidates the snapshot and re-runs steps 6–8.
6. On TTL expiry the record terminates as `Expired`, the agent is told, and nothing happens. Expiry
   is the safe default; an expired action must be re-proposed, not resurrected.

## 5. Shared services detail

- **Inference (C5):** LiteLLM proxy for hosted models (Gemini/OpenAI), vLLM for local GPU models;
  exposes a unified Completions API. **Per-tier/per-tenant virtual keys** give each agent its own
  budget, rate limit, and log isolation on the shared proxy, so one tenant's agent cannot exhaust
  another's quota or read another's prompts. Prometheus metrics + OTel traces exported. Inference is
  on the **reasoning** path only — never on the write path, and never consulted for a trust decision
  ([03](03-security-model.md) §9).
- **Minty (C6):** the only credential path for repo writes; issues short-lived GitHub App tokens via
  KMS + Workload Identity. **Scope reduced by the inversion**: Minty now serves only the optional
  write-behind mirror (C13/C8) and OKF writes. No control path depends on it, so a Minty outage
  degrades mirror freshness and nothing else.
- **Journal store (C-JS):** see §1.2. Operationally: the CRD ships with the controller; the exporter
  runs in the controller and streams every phase transition to the audit sink within 60 s; the
  garbage collector deletes terminal records past their class retention **only after** export is
  confirmed. `kubectl get actionrecords -A -o wide` is the primary human view; the ChatOps `history`
  and `undo` commands read the same objects.
- **Cross-cluster connectivity (spoke → hub):** spokes consume the hub's inference (C5), Minty (C6),
  router (C15), and the mesh parent endpoint (C-AM) **remotely**, over **VPC-internal endpoints**
  (internal LoadBalancer / private `Service` on the shared VPC — never public). Each spoke's
  default-deny egress NetworkPolicy allowlists exactly those endpoints, plus cloud APIs, its own
  broker, and MCP grounding endpoints. Authn is the LiteLLM virtual key for inference, Workload
  Identity for Minty, and mTLS + `TokenReview` for the mesh — **no cross-cluster Kubernetes
  credentials** (§7).
- **OKF base (C9):** curated knowledge as markdown-in-git under the repo's **`knowledge/` root** —
  outside the paths any customer pipeline deploys, so it is never applied to a cluster.
- **Session state (C11):** per-user session state and the agent's self-generated work queue, on a
  PVC with atomic writes so an agent pod restart resumes mid-queue. The queue is **agent-local
  state, not a control surface**: pausing an agent preserves the queue; it does not drain it.
- **mem0/Qdrant (C10) — deferred post-v1:** semantic recall is not in v1. If introduced later,
  default to a single shared Qdrant in the hub with **server-side** scope isolation (per-scope
  collections / access-controlled keys) and treat recall as best-effort.
- **Observability (C12):** OTel → `gke-managed-otel` → Cloud Trace/Logging + Managed Prometheus. One
  trace ID spans chat turn → envelope → broker pipeline → API call → verification →
  `ActionRecord` → report, which is what makes an action reviewable after the fact
  (`docs/designs/audit-logging-user-attribution.md`).

### 5.1 How the four SLIs are produced

[01](01-vision-scope.md) §7's four SLIs are the continuous, production form of the negative tests in
[03](03-security-model.md) §11. Each is a log-based metric over the Kubernetes audit log, the Cloud
Audit Log, and the exported journal, with an alert policy attached. All four are **derived from
sources the agents cannot write to** — that is the point of computing them off the audit log rather
than off agent self-reporting.

| SLI                               | Source & computation                                                                                                                                                                                                                                                                                                                                                                                                                              | Target / alert                                                                                          |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| **1. Zero cross-scope escapes**   | K8s audit log (`config/audit/audit-policy.yaml` already captures `system:serviceaccounts:kubeagents-system`) + Cloud Audit Logs. Filter: any request whose principal is an agent identity (reader **or** actor) and whose resource falls outside the scope registered for that identity, including `SubjectAccessReview` **allow** results. Metric `kubeagents/cross_scope_escape`                                                                | **0.** Any occurrence pages. Auto-pauses the agent                                                      |
| **2. Zero unjournaled mutations** | Two independent detectors. (a) **At admission** — `C-AS` rejects any actor-identity write lacking `kube-agents/action-id`, so the failure mode is a denial, not a silent write. (b) **After the fact** — join actor-identity writes in the audit log against exported `ActionRecord`s on that annotation; emit `kubeagents/unjournaled_mutation` for any write with no matching record within 60 s, and for any write at all by a reader identity | **0.** Any occurrence pages. This is the imperative replacement for the old "zero direct mutations" SLI |
| **3. Zero self-escalations**      | Audit-log filter for any agent-identity request that creates/modifies `Role`/`ClusterRole`/`*Binding`/IAM policy/Workload-Identity binding **naming an agent identity**, uses `escalate`/`bind`/`impersonate`, or touches an `Agent` CR, a VAP, the controller, a broker, or an `ActionRecord`. Excludes the attenuation-checked child-provisioning path (F4). Metric `kubeagents/self_escalation_attempt`                                        | **0** executed; attempts are counted and page on repetition                                             |
| **4. Undo health**                | Journal exporter gauges: `kubeagents/undo_plan_coverage` = executed non-gated records with `status.undoPlan.valid` ÷ all executed non-gated records; `kubeagents/undo_success_rate` = successful undos ÷ attempted. Plus a **synthetic undo canary** per cluster — a scheduled routine action in a canary namespace, immediately undone and diffed                                                                                                | Coverage **1.0**; success rate **1.0** (any failure pages); canary failure pages                        |

The exporter that computes 2 and 4 runs in the controller and is the only component that reads the
journal in bulk; 1 and 3 are pure log-based metrics with no kube-agents component in the loop, so
they keep working if the kube-agents control plane is itself the thing misbehaving. Proactivity
counters (MTTR by severity, share of issues resolved without a human, actions per agent per day,
flap and revert counts) come from the same journal export and are graphed alongside — "relentless"
and "not thrashing" are read on one dashboard.

## 6. Non-functional requirements (targets — defaults, tune later)

| Dimension                  | Default target                                                                                                                                                                                                                            | Rationale                                                                                     |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| Fleet scale                | ≥ 50 spoke clusters per hub                                                                                                                                                                                                               | Fleet-governance use case                                                                     |
| Agents per cluster         | 1 Cluster Admin + ≤ 200 Dev Team (namespaces), each with its own broker                                                                                                                                                                   | Namespace density on GKE                                                                      |
| **Action latency (K8s)**   | Envelope → executed → journaled: **p95 < 5 s, p99 < 15 s** for a single-object routine write (classification + snapshot + SSA + verify + record)                                                                                          | The action is inside a chat turn now, not behind a merge                                      |
| **Action latency (cloud)** | p95 < 60 s for a synchronous cloud API; long-running operations (cluster create, node-pool resize) execute asynchronously with the record in `Executing` and a bounded poll, p95 < 15 min                                                 | Cloud control planes, not kube-agents, set this floor                                         |
| **Chat turn latency**      | p95 < 10 s for read/plan; **p95 < 20 s from message to "done + undo handle"** for a routine single-object mutation. Deterministic routing adds no inference; NL routing adds one router call                                              | Mutations are now synchronous, so they are inside the user-visible budget                     |
| **Broker throughput**      | ≥ 20 envelopes/s sustained and ≥ 5 concurrent executions per broker, serialized per target object; ≥ 500 envelopes/s aggregate per cluster at the density above                                                                           | A fleet-wide rollout fans out across brokers, so per-broker rate is modest                    |
| **Time to remediate (F6)** | Detection → verified fix, p50 < 2 min for routine namespace-scope issues; p95 < 15 min including diagnosis                                                                                                                                | The product claim, measured                                                                   |
| **Undo latency**           | `undo` → restored + verified: **p95 < 30 s** (K8s targets), < 5 min (cloud). Add ≤ 20 s when the target broker must be woken from `scaleToZero`                                                                                           | The brake has to feel instant or it is not a brake                                            |
| **Brake propagation**      | `pause` → broker refuses new envelopes **< 2 s p99**; in-flight action completes or rolls back < 30 s; local `FleetFreeze` apply → effective < 2 s; **fleet-wide freeze fan-out to the last spoke < 30 s**; all of it with inference down | [03](03-security-model.md) §6 requires the brake to work unconditionally                      |
| **Journal durability**     | **100%** of mutations journaled — enforced, not measured: the broker fails closed and admission rejects unannotated writes. Export to the audit sink < 60 s. In-cluster retention 30/90/180 days by class; sink retention ≥ 400 days      | SLI 2 is an invariant, not a percentile                                                       |
| **Blast-radius caps**      | Hard defaults per action: ≤ 50 objects, ≤ 10% of a scope's workloads, ≤ 1 namespace for the dev-team tier, ≤ 1 node pool for the cluster tier. Exceeding a cap aborts; approaching one escalates the class                                | Bounds "correct remediation applied at machine speed" ([03](03-security-model.md) §1 class C) |
| **Initiative budget**      | Default ≤ 50 `routine` + ≤ 10 `elevated` actions per agent per hour, with a flap cooldown per target; exhaustion escalates instead of continuing ([04](04-workflow-model.md) §4.2)                                                        | Relentless, not thrashing                                                                     |
| **Availability**           | Broker ≥ 99.9% per scope. **Spoke brokers execute with the hub down**; agent _reasoning_ pauses (hub-hosted inference). Controller down ⇒ running agents and brokers keep working, no new reconciles                                      | No cascade; the hub is a reasoning dependency, not an acting one                              |
| **Recovery**               | Agent pod restart < a few s (PVC-backed state); **broker restart < 5 s**, then reconcile every non-terminal `ActionRecord` it owns — complete or roll back, never leave half-applied                                                      | The write-ahead journal is what makes restart safe (§1.1)                                     |
| **Footprint**              | Broker 50m/64Mi request, 200m/256Mi limit, 1 replica. Idle Dev Team agents `scaleToZero` **including their brokers**                                                                                                                      | Bounds the per-namespace cost of one-broker-per-agent                                         |
| **Cost**                   | Shared inference in the hub; Spot-eligible agent pods; brokers are not Spot-eligible (they hold in-flight actions)                                                                                                                        | Avoid per-cluster duplication without risking mid-action eviction                             |

These are **defaults for a builder**, not commitments; revisit under load testing.

## 7. Deployment-model decisions

- **One Action Broker per `Agent` CR — no shared broker (v1).** Blast radius of a broker compromise
  or a scope-resolution bug is **exactly one scope**, containment is done by RBAC rather than
  in-process multi-tenancy, and each broker presents a distinguishable identity so `C-AS` can act as
  an independent second check. A shared multi-tenant broker is **explicitly rejected**: it would
  hold or be able to assume fleet-wide write authority, which is the one thing
  [03](03-security-model.md) §3.1 forbids outright. Cost: one small pod per agent (§3).
- **The broker is a separate Deployment, not a sidecar (v1).** Keeps the actor credential out of the
  pod that parses untrusted content, decouples broker availability from agent restarts, and makes
  the mTLS + `TokenReview` hop real rather than a loopback formality. Co-location stays available as
  a later footprint optimization at extreme namespace density.
- **`ActionRecord` is a namespaced CR, in the agent's namespace (v1).** No new stateful service on
  the write path (the broker fails closed without the journal, so the journal's availability is the
  system's write availability), RBAC-scoped by construction, watchable by `C-UC` and the exporter,
  and queryable with `kubectl`. Retention is class-based with a confirmed export to the audit sink
  before garbage collection, so etcd never becomes the long-term store (§1.2). Revisit only if
  journal volume outgrows etcd at fleet scale.
- **Brokers must keep executing during a hub outage.** No component on the write path — credential,
  API server, journal, brake object — is hub-hosted. A hub loss degrades _reasoning_ (inference,
  cross-cluster routing, fleet-wide coordination), never _acting_, _journaling_, _braking_, or
  _undoing_ in a spoke. This is the reason actuation was moved in-cluster in the first place, and
  it is verified as §8 CH4.
- **The journal is a write-ahead log.** The `ActionRecord` is created before execution and only
  transitioned after verification, so a broker killed mid-action leaves a discoverable record rather
  than an invisible mutation, and restart reconciliation can complete or roll it back (§1.1, §8 CH7).
- **The mesh reuses the existing one-delivery-contract seam.** `delegation` and `escalation` join
  `alert`, `github`, and `k8s-event` as kinds on the in-pod session-inject seam that `C16`/`C17`
  already speak, rather than introducing a second inbound path into the agent pod. One hardened
  ingress, one audit shape.
- **Fleet-wide freeze is fanned out controller-to-controller, with a local override.** No
  cross-cluster Kubernetes credentials exist, so `FleetFreeze` is a per-cluster object written by
  each cluster's own controller from the hub's freeze channel. Loss of the _local_ object read is
  fail-closed; loss of the _hub channel_ is not (it alarms and holds last state), so a hub blip
  cannot stop the fleet. A human can always apply `FleetFreeze` directly in any cluster (§1.5).
- **The customer's CI/CD pipeline is off the critical path.** It is neither required nor privileged.
  Where a customer runs a GitOps engine, the broker writes executed state **behind** to the mirror
  repo so the two do not fight ([04](04-workflow-model.md) §6); where they do not, nothing changes.
  A mirror failure never blocks, delays, or rolls back an action, and §8 verifies the system with
  the mirror removed entirely.
- **Controller runtime scope — one controller per cluster.** Each cluster's controller reconciles
  **only its own cluster's** `Agent` CRs, brokers, and journal. No cross-cluster credentials; a new
  spoke gets its controller at provisioning. Preserves failure isolation and least privilege.
- **Spoke bootstrap — provisioned by the parent's broker, not self-installed.** A fresh spoke has no
  controller, no broker, and no agent, so bootstrap is part of the **cluster-provisioning action**
  the Platform Agent's broker executes: the same action that creates the cluster installs
  cert-manager, the CRDs (`Agent`, `ActionRecord`, `FleetFreeze`, `UndoRequest`), `vap-agent-scope`,
  and the kube-agents controller, then applies the cluster-admin `Agent` CR with its pre-created
  reader/actor identities. Only then does the spoke's own controller reconcile the Cluster Admin
  Agent and its broker. This resolves the chicken-and-egg (an in-cluster agent cannot install its
  own runtime) using the credentials the parent already legitimately holds from creating the
  cluster, and it is journaled and undoable like any other action. Cluster creation is `gated`
  ([03](03-security-model.md) §5.2), so a human approves the cascade once, at the top.
- **Single-cluster install — collapse topology, not personas.** One cluster plays hub and spoke: the
  controller, all three agent tiers **each with their own broker**, and the shared services run in
  it. All three personas still run; the persona model, the identity split, and the isolation proof
  are identical to a multi-cluster install.
- **OKF location — `knowledge/` root in the mirror repo.** Reuses the Minty token path and lives
  outside any deployed path. A dedicated knowledge repo stays optional
  ([06](06-api-and-data-contracts.md) §5).
- **Semantic recall (mem0/Qdrant) — deferred post-v1.** OKF-in-git covers durable shared knowledge
  and the semantic-recall need is unproven. If later added: one shared Qdrant in the hub with
  server-side scope isolation; recall best-effort.

## 8. Verification

The failure-isolation / chaos suite. Scenarios are labelled **CH1–CH9**; **CH1–CH4 are the
scenarios `local-dev/kind/verify-phase6.sh` labels `C1`–`C4`** (renamed here so scenario labels do
not collide with the component IDs of §1). CH5–CH9 are created by the imperative model. Each is a
runnable check; a build is not done until all are green ([README](README.md) building note 8).

**Static placement and shape**

- **Two workloads per agent:** for every `Agent` CR, the controller reconciles an agent Deployment
  bound to the **reader** SA and a `<agent>-broker` Deployment bound to the **actor** SA, in the
  correct namespace, with `runAsNonRoot`, seccomp `RuntimeDefault`,
  `allowPrivilegeEscalation: false`, and — on the agent pod — no projected token with write RBAC.
- **Placement:** Platform Agent + broker in the hub (`kubeagents-system`); each Cluster Admin Agent +
  broker in its cluster; each Developer Team Agent + broker in its namespace; `ActionRecord`s in the
  agent's namespace; one `FleetFreeze` kind per cluster.
- **Labels:** every agent Deployment, pod, and `Service` carries `kube-agents/tier`,
  `kube-agents/scope`, and `kube-agents/parent`, and the mesh NetworkPolicy selects peers by them.

**CH1 — Controller down.** Kill the controller. Running agent pods **and their brokers** continue;
in-flight and newly submitted actions still execute, journal, and verify; no new reconciles occur;
on restart the controller resumes without recreating or disturbing running brokers. _(Accept: the
control plane is not on the action path.)_

**CH2 — Controller up after loss.** Delete an agent pod and its broker; the controller relaunches
both, rebinds the same reader/actor SAs, and the broker reconciles any non-terminal `ActionRecord`
it owns.

**CH3 — Parent agent down within a cluster.** Kill the Cluster Admin Agent (pod **and** broker). Its
Developer Team Agents and their brokers keep running and keep executing in-scope remediation — **no
cascade**. Escalations to the dead parent queue with a TTL rather than failing the child's own work.
Relaunch drains the queue.

**CH4 — Hub down.** Cut the hub. In each spoke: workloads keep running; the **spoke broker keeps
executing** already-triggered local remediation, journals it locally, and honors the local brake;
`undo` works locally; agent _reasoning_ pauses because inference is hub-hosted, and the agent says
so instead of guessing. Verify explicitly that **no write path traversed the hub** — no action fails
with a hub-connectivity error. Restore the hub: queued reasoning resumes, the freeze channel
reconnects, and the mirror (if configured) catches up.

**CH5 — Broker down: the agent must fail closed.** Scale an agent's broker to zero while the agent
has queued work and an operator is actively asking it to fix something. Assert:

- the agent reports that it **cannot act** and queues the work — it does not report success;
- the Kubernetes and Cloud audit logs show **zero writes** by that agent's identities during the
  window — in particular **no fallback to a direct write** with the reader token, no `kubectl` via a
  tool, no cloud SDK call;
- an attempted direct write from inside the agent container fails twice over: no RBAC, and `C-AS`
  denies it independently;
- on broker restart the queue drains and every drained action is journaled normally.

This is the single most important negative test in this suite: the agent's inability to act without
its broker is the property the whole identity split buys.

**CH6 — Journal store down.** Make `ActionRecord` writes fail (remove the CRD's storage version, or
deny the broker's create on it). The broker **refuses to execute** rather than executing
unjournaled; auto-brake pauses the agent; the audit log shows zero mutations by that actor identity
during the window; the failure is reported to humans. Restoring the journal restores service without
a broker restart.

**CH7 — Broker killed mid-action.** Submit a multi-object action and kill the broker between execute
and journal. Assert: the `ActionRecord` exists in `Executing` with its snapshot and undo plan (the
write-ahead property); on restart the broker re-verifies and either completes or rolls back; the
final state is never half-applied; no mutation exists in the audit log without a matching record.

**CH8 — Parent tier unreachable across clusters.** Sever the spoke→hub mesh path. The Cluster Admin
Agent and every Developer Team Agent below it **keep operating within their own scopes** — full F1
and F6 flows succeed. Escalations queue with a TTL and, on expiry, surface to the human roster over
whatever chat path remains; none are silently dropped. No child action is blocked merely because its
parent is unreachable.

**CH9 — No cascade, and the brake survives everything.** With inference, the router, and the hub all
unavailable simultaneously: `kubectl patch` a `pause` and assert the target broker refuses new
envelopes within the §6 budget; `kubectl apply` a local `FleetFreeze` and assert every broker in
that cluster stops; assert the broker fails closed if it cannot read the freeze object; assert an
in-flight action completed or rolled back rather than being abandoned. Then restore and assert
nothing was left half-applied. Separately, verify one tier's failure never propagates: killing any
single agent, broker, or controller leaves the other tiers' actions succeeding.

**Unopinionated actuation — the demotion, verified**

- Remove the mirror repository and any customer CI/CD entirely. Every flow in §4 still works: agents
  detect, act, verify, journal, report, and undo. Nothing requires a bundled GitOps engine (no
  Config Sync, no Connector) to be installed.
- With a mirror configured, break the Minty path. Actions still execute and journal; the mirror
  write is retried and reported as degraded; **no action is delayed, blocked, or rolled back** by
  the mirror's failure.
