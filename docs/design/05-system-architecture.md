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

Every `Agent` CR reconciles into **two workloads**: the **agent pod** (Deployment `<agent>-gateway`,
Service `<agent>`, Hermes harness, holding the read-only **reader** identity) and its own **Action
Broker** (`C-AB`, Deployment and Service both `<agent>-broker`, deterministic Go, holding the
scoped-write **actor** identity). The broker is the only component in the system that can write to a
cluster or cloud API. It classifies, gates, snapshots, executes, verifies, and journals every
mutation as an **`ActionRecord`** in the journal store (`C-JS`), from which the **undo controller**
(`C-UC`) can replay a recorded undo plan at any time. Two watchdogs sit behind that path: the
**journal reconciler** (`C-JR`) joins the audit logs against the journal to catch a write with no
record, and the **anomaly detector** (`C-AD`) auto-pauses an agent whose action stream departs from
its own baseline. Agents reach each other directly over the **agent mesh** (`C-AM`) to delegate and
escalate; humans reach the **brake surface** (`C-BR`) to pause, freeze, or undo.

**Actuation is in-cluster and synchronous.** The customer's CI/CD pipeline is no longer in the
critical path — it survives only as an optional **write-behind IaC mirror** target
([04](04-workflow-model.md) §6). Everything runs in the `kubeagents-system` namespace convention
with telemetry to `gke-managed-otel`.

---

## 1. Component inventory

Component IDs are stable and cited from other documents. IDs `C1`–`C15` are carried over from the
read-only generation with updated responsibilities; `C16`–`C17` name pre-existing components that
previously had no ID; `C-AB`, `C-JS`, `C-JR`, `C-UC`, `C-AM`, `C-BR`, `C-AD`, `C-AS` are created by
the imperative model.

| #        | Component                                          | Responsibility                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | Tech / basis                                                                                                                                                                                 | Status                          |
| -------- | -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------- |
| C1       | **kube-agents controller (agent runtime)**         | Reconciles each `Agent` CR into **two** workloads — the agent pod (Deployment `<agent>-gateway`, Service `<agent>`, reader SA) and its **Action Broker** (Deployment and Service both `<agent>-broker`, actor SA) — plus the mesh certificate and NetworkPolicies. Owns `(tier,scope)` cardinality + placement + child ⊆ parent admission (`C-AS`), stamping of the **five** identity labels (`kube-agents/role`, `/agent`, `/tier`, `/scope`, `/parent` — [08](08-agent-runtime-and-identity.md) §2.5), lifecycle/relaunch, brake fan-out (`C-BR`), and journal export. **Generalizes today's `PlatformAgent` operator** | `k8s-operator/` (Go, Kubebuilder), extended; Scion model ([GoogleCloudPlatform/scion](https://github.com/GoogleCloudPlatform/scion))                                                         | Exists (extend)                 |
| **C-AB** | **Action Broker**                                  | **The only writer in the system.** One per `Agent` CR, bound to that agent's actor SA. Runs the eleven-step pipeline of [03](03-security-model.md) §4.1 as deterministic Go with **no LLM in the loop**: authenticate → validate → resolve scope → classify → check brake → generate undo plan → gate → snapshot → execute → verify → journal. Holds the only cluster/cloud write credential (§1.1)                                                                                                                                                                                                                       | Go (new binary `k8s-operator/cmd/broker/`), **HTTP+JSON over mTLS** on `:8443` ([06](06-api-and-data-contracts.md) §4.1), server-side apply, cloud SDK                                       | New (v1, load-bearing)          |
| **C-JS** | **Journal store**                                  | Durable home of `ActionRecord`s — pre-state snapshot, applied diff, verification result, undo plan, attribution. **Decision: `ActionRecord` custom resources** in the agent's own namespace, continuously exported to the audit sink. Survives the agent and broker pods (§1.2)                                                                                                                                                                                                                                                                                                                                           | `ActionRecord` CRD (`kubeagents.x-k8s.io/v1alpha1`) + exporter in the controller → Cloud Logging / object sink                                                                               | New (v1, load-bearing)          |
| **C-JR** | **Journal reconciler**                             | The **completeness backstop** behind `C-JS`. Continuously joins the Kubernetes and Cloud audit logs against exported `ActionRecord`s to find writes with no record, fabricated or **reused** `action-id`s, and cloud mutations — which no admission controller sees. The only enforcement available for **deletes** and for cloud calls, where the `action-id` annotation cannot be required at admission ([03](03-security-model.md) §4.3). Produces SLI 2 (§5.1, §1.6)                                                                                                                                                  | Controller-hosted reconciler in `k8s-operator/` (log-sink reader + journal watch), one per cluster + one hub-side for cloud logs                                                             | New (v1, load-bearing)          |
| **C-AD** | **Anomaly detector**                               | Watches each agent's own action stream (rate, risk-class mix, target-set novelty) against a per-agent learned baseline and **auto-pauses** the agent on a trip, via `C-BR`. This is the bound that [03](03-security-model.md) §6/§8.1 and [08](08-agent-runtime-and-identity.md) §4 place on an injected-but-in-scope agent and on standing actor credentials (§1.7)                                                                                                                                                                                                                                                      | Controller-hosted detector in `k8s-operator/` over the `ActionRecord` watch; no LLM, no external ML service                                                                                  | New (v1, load-bearing)          |
| **C-UC** | **Undo controller**                                | Executes `undo <action-id>` by replaying a recorded undo plan **through the target agent's broker** — including when the originating agent is paused, scaled to zero, or deleted. Watches `UndoRequest` objects and chat/`kubectl` invocations; reports restoration against the recorded snapshot (§1.3)                                                                                                                                                                                                                                                                                                                  | Controller in `k8s-operator/` (reconciles `UndoRequest` → broker call)                                                                                                                       | New (v1, load-bearing)          |
| **C-AM** | **Agent mesh**                                     | The direct agent-to-agent call path: **delegation** (parent → child) and **escalation** (child → parent). Replaces the read-only generation's indirect coordination through the repo. mTLS + `TokenReview`; the callee **re-authorizes in its own scope** and never inherits the caller's authority (§1.4)                                                                                                                                                                                                                                                                                                                | **HTTPS+JSON** on the **agent** pod at `<agent>.<ns>.svc:8444/v1alpha1/mesh/{delegate,escalate}`, terminating on the existing in-pod session-inject seam (`kind: delegation` / `escalation`) | New (v1)                        |
| **C-BR** | **Brake surface**                                  | `pause` (`spec.operations.paused` on the `Agent` CR), `freeze` (cluster-scoped `FleetFreeze`), `contested` markers, and their propagation to every broker. Must remain effective with inference, the router, and the hub all unavailable (§1.5)                                                                                                                                                                                                                                                                                                                                                                           | CRD fields + `FleetFreeze` CRD + broker informers + controller fan-out                                                                                                                       | New (v1, load-bearing)          |
| **C-AS** | **Admission backstop**                             | The independent, out-of-broker enforcement of [03](03-security-model.md) §4.3: `vap-agent-scope` (in-tree CEL VAP — reader SAs write nothing, actor SAs write only their tier template within scope, no RBAC naming an agent identity, no control-plane/journal tampering, **every actor write must carry `kube-agents/action-id`**) plus the controller's validating webhook (cardinality, placement, **child ⊆ parent ceiling — v1**)                                                                                                                                                                                   | `ValidatingAdmissionPolicy` (K8s ≥1.30) + `k8s-operator/internal/webhook/` + cert-manager                                                                                                    | Exists (invert + extend)        |
| C2       | **Platform Agent**                                 | Project/fleet operator; chat entrypoint for platform teams. Reader identity; acts through its own broker over project scope                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | Hermes harness (reconciled by C1, `agents/platform/`)                                                                                                                                        | Exists (convert)                |
| C3       | **Cluster Admin Agent**                            | Cluster operator; chat entrypoint for cluster admins. Reader identity; acts through its own broker over cluster scope                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | Hermes harness (`agents/cluster-admin/`)                                                                                                                                                     | Exists (convert)                |
| C4       | **Developer Team Agent**                           | Namespace operator; chat entrypoint for dev teams. Reader identity; acts through its own broker over namespace scope                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | Hermes harness (`agents/developer-team/`)                                                                                                                                                    | Exists (convert)                |
| C5       | **Inference service**                              | Unified Completions API for all agents; **per-tier/per-tenant virtual keys** for budget, rate-limit, and log isolation                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | LiteLLM (hosted models) / vLLM (local GPU)                                                                                                                                                   | Exists                          |
| C6       | **GitHub Token Broker (Minty)**                    | Brokers short-lived GitHub App tokens. **Now used only for the optional write-behind mirror (C13) and OKF writes** — it is no longer on any control path                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | GCP KMS + Workload Identity                                                                                                                                                                  | Exists (demoted)                |
| C7       | **Customer CI/CD pipeline** _(optional, demoted)_  | **No longer in the critical path at all.** In the read-only generation this was the privileged writer; in the imperative model the broker writes directly. C7 survives only as the customer's own reconciler downstream of the **write-behind IaC mirror** ([04](04-workflow-model.md) §6). kube-agents works with it absent, and must be verified that way (§8)                                                                                                                                                                                                                                                          | GitHub Actions / CircleCI / Argo / Config Sync / … (customer-provided)                                                                                                                       | Customer-provided (optional)    |
| C8       | **IaC artifacts + tooling** _(mirror format)_      | The declarative rendering of **already-executed** state written back to the mirror so a customer's GitOps engine does not fight the broker                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | **KCC YAML** or **Terraform HCL** (per customer requirements)                                                                                                                                | New (optional)                  |
| C9       | **OKF knowledge base**                             | Durable curated knowledge (SOPs, blueprints, runbooks) under the repo's `knowledge/` root                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | OKF markdown in git                                                                                                                                                                          | New                             |
| C10      | **mem0 + Qdrant** _(deferred post-v1)_             | Semantic/cognitive recall — **not in v1** ([02](02-agent-personas.md) §2.3)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | mem0ai + Qdrant vector store                                                                                                                                                                 | Deferred                        |
| C11      | **Session store**                                  | Per-user runtime session state and the agent's self-generated work queue                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | `session_kv.db` + `multiuser_memory` (PVC-backed)                                                                                                                                            | Exists                          |
| C12      | **Observability pipeline**                         | Traces/metrics/logs + attribution; carries the trace ID from chat through the envelope into the `ActionRecord`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | OTel → `gke-managed-otel` → Cloud Trace/Logging/Managed Prometheus                                                                                                                           | Exists                          |
| C13      | **Mirror repository** _(optional)_                 | Was "the GitOps repository, source of truth for all mutation". Now a **write-behind mirror** of executed state plus the home of OKF (C9). Compromising it cannot cause a cluster change ([03](03-security-model.md) §2)                                                                                                                                                                                                                                                                                                                                                                                                   | Git (GitHub)                                                                                                                                                                                 | Exists (demoted)                |
| C14      | **Authorization gateway** _(deferred — hardening)_ | User-scoped authorization (`SubjectAccessReview` + `testIamPermissions`) down-scoping each action to **agent scope ∩ requester permissions** ([03](03-security-model.md) §4a). **Not in v1.** When adopted it lands **inside C-AB**, which already authenticates the caller and resolves scope per action — it is no longer a separate gateway                                                                                                                                                                                                                                                                            | Broker-hosted check (was: standalone gateway)                                                                                                                                                | Deferred (relocated into C-AB)  |
| C15      | **ChatOps gateway & router**                       | Single chat ingress (Google Chat + Slack): normalizes both platforms, enforces the target agent's `allowedUsers` **before** dispatch, and routes each message to the addressed agent — by slash command, `@<tier>-<scope>` handle, or NL inference as fallback. Also delivers **gate prompts** and **action reports** back to the humans ([04](04-workflow-model.md) §3)                                                                                                                                                                                                                                                  | `k8s-operator/cmd/router/` + `internal/router/`, keyed on `internal/agentindex`                                                                                                              | Exists (extend)                 |
| C16      | **Kubernetes event watcher**                       | Streams, filters, and deduplicates warning events from the API server and injects them into the agent's session seam — the primary **push trigger** for the proactive loop (F6). Already exists and already works; in the imperative model its output ends in a **fix**, not a proposal                                                                                                                                                                                                                                                                                                                                   | `k8s-operator/cmd/k8s-event-watcher/` (sidecar daemon → `127.0.0.1:8699`)                                                                                                                    | Exists                          |
| C17      | **Event ingress relay**                            | Delivers non-chat machine push — Cloud Monitoring/Alertmanager alerts over Pub/Sub, GitHub webhooks, and **mesh escalations** — to the same in-pod session-inject seam, under one kind-discriminated delivery contract (`alert`, `github`, `escalation`, `k8s-event`, `delegation`)                                                                                                                                                                                                                                                                                                                                       | `k8s-operator/cmd/eventingress/` + `deploy/eventingress/`                                                                                                                                    | Exists (extend with mesh kinds) |

> **The one component that is gone:** nothing in this inventory is a "privileged writer that acts on
> reviewed state". C7 is optional and downstream. If a design question resolves to "the pipeline
> will apply it", the answer is wrong — the broker applies it.

### 1.1 The Action Broker (`C-AB`) in detail

**Shape.** One `Deployment` named `<agent>-broker` and one ClusterIP `Service` of the **same name**,
**single replica**, in the same namespace as its agent, bound to
`ServiceAccount <tier>-<scope>-actor` ([03](03-security-model.md) §3.1). (Its agent counterpart is
Deployment `<agent>-gateway` behind Service `<agent>` — [08](08-agent-runtime-and-identity.md) §2.1.
The four names are distinct and every one of them is derived from `tier` + `scope`.) It is a small
static Go binary: no LLM client, no chat client, no untrusted-content parser, no plugin surface.
That minimalism is a security property — this is the highest-value credential in the system
([03](03-security-model.md) §8).

**Listeners.**

| Port    | Bind        | Protocol                        | Purpose                                                                                                                                                                                                                                                                                                                             |
| ------- | ----------- | ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `:8443` | pod IP      | **HTTPS + JSON**, mTLS required | The action API (below), reachable at `<agent>-broker.<ns>.svc:8443`                                                                                                                                                                                                                                                                 |
| `:8081` | **pod IP**  | HTTP, no client auth            | `/healthz` + `/readyz` for the **kubelet**, which dials the pod IP and therefore cannot reach a loopback listener. It is bound to the pod IP, not `127.0.0.1`, and is safe to expose because it returns only `ok`/`not-ready` with no scope, journal, or credential state; the NetworkPolicy admits `:8081` from the node CIDR only |
| `:9090` | **pod IP**  | HTTP, no client auth            | Prometheus/OTel `/metrics`, scraped by the collector from another pod — likewise not loopback-reachable. NetworkPolicy admits it from the `gke-managed-otel` collector only                                                                                                                                                         |
| —       | `127.0.0.1` | —                               | Nothing. There is no in-pod client; a loopback-only listener in this pod would have no caller                                                                                                                                                                                                                                       |

**Interface.** Three routes, HTTP+JSON, all authenticated the same way (step 1 below). The wire
format is owned by [06](06-api-and-data-contracts.md) §4.1 — this is the placement, not the schema.

| Route                                       | Caller                    | Purpose                                                                                                                                                                      |
| ------------------------------------------- | ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /v1alpha1/actions`                    | The agent pod (reader SA) | Submit one `ActionEnvelope`. Returns `{actionId, class, phase}` — synchronous for `routine`/`elevated`, `PendingApproval` for `gated`, `Rejected` for out-of-scope/forbidden |
| `POST /v1alpha1/actions/{actionId}/approve` | C15 router / C-BR         | Release a parked `gated` action after roster approval ([04](04-workflow-model.md) §3)                                                                                        |
| `POST /v1alpha1/actions/{actionId}/replay`  | C-UC undo controller      | Execute a recorded undo plan as a first-class, re-classified, re-journaled action                                                                                            |

**Not gRPC.** The transport is HTTP+JSON deliberately: one schema in one place
([06](06-api-and-data-contracts.md) §4.1) that is inspectable with `curl` during an incident, no
generated-stub build step in the highest-value binary in the system, and no second serialization
surface to review. The broker **never** accepts a tier, scope, or risk class from the request body —
those are derived from the authenticated caller and from code.

**Pipeline mapping.** The eleven steps of [03](03-security-model.md) §4.1, and where each is
implemented:

| Step            | Implementation                                                                                                                                                                                                                                                                                                                                                   |
| --------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1 Authenticate  | mTLS peer cert (cert-manager, SAN = the agent's `agentindex` identity key) **and** `TokenReview` of the projected reader-SA token. `(tier, scope)` comes from the CR the SA belongs to                                                                                                                                                                           |
| 2 Validate      | Envelope schema + target reference well-formedness; unknown fields rejected                                                                                                                                                                                                                                                                                      |
| 3 Resolve scope | Every target resolved to `(cluster, namespace, group/kind/name)` or a cloud resource path and checked against the caller's scope. **Label selectors are expanded against live state here**, so steps 4–8 see the concrete object list, not the selector. **One out-of-scope target rejects the whole envelope**                                                  |
| 4 Classify risk | Deterministic classifier over the [03](03-security-model.md) §5.2 inputs, floored by a code constant and only ever tightened by a `ChangePolicy` ([06](06-api-and-data-contracts.md) §4.2). Includes the blast-radius rules of §6 — **gate above 50 objects, abort above 100 or `fractionOfScope > 0.5`** — evaluated on the **expanded** target set from step 3 |
| 5 Check brake   | Informer-cached read of the `Agent` CR `paused` field, the `FleetFreeze` object, the per-class initiative budget counters, the flap cooldown, and any `contested` marker on a target. **Fail-closed** (§1.5). Budget or flap ⇒ refuse **and escalate**; the agent is not paused (§1.5)                                                                           |
| 6 Undo plan     | Per-verb undo generators (create → delete; update/patch → re-apply pre-state; scale → prior replicas; delete → recreate from snapshot, only where reconstructable). No plan ⇒ reclassify as `gated`                                                                                                                                                              |
| 7 Gate          | `gated` ⇒ persist `ActionRecord` in phase `PendingApproval`, notify the roster via C15, start the TTL. Nothing executes                                                                                                                                                                                                                                          |
| 8 Snapshot      | `GET` every target at its current `resourceVersion`; store inline if ≤ **1 MiB**, otherwise store a digest + sink reference (§1.2)                                                                                                                                                                                                                               |
| 9 Execute       | Server-side apply with field manager `kube-agents/<tier>/<scope>`, **`dry-run` first per the rule below**, the `kube-agents/action-id` annotation stamped on every object, cloud APIs via the actor GSA                                                                                                                                                          |
| 10 Verify       | Re-read targets and evaluate the envelope's declared success condition (readiness, observed generation, cloud operation result). Failure ⇒ automatic rollback via the step-6 plan                                                                                                                                                                                |
| 11 Journal      | Transition the `ActionRecord` to a terminal phase with the applied diff, verification result, and validated undo plan; emit the audit event; only then return success to the caller                                                                                                                                                                              |

**The dry-run rule (step 9), stated so it is checkable.** "Where supported" is not a test criterion,
so the rule is a closed one: the broker issues `dryRun=All` for **every Kubernetes target**, and a
non-nil dry-run error aborts the envelope before any real write, **except** for three enumerated
carve-outs where a dry run is either impossible or meaningless:

| Carve-out                                                | Why                                                                                                                                                                                               |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Subresource writes** (`/status`, `/scale`, `/rollout`) | Dry-run semantics are per-subresource and inconsistently implemented; a green dry run proves nothing about the real write                                                                         |
| **`pods/eviction`**                                      | An eviction is a request against the disruption budget, not an object write; there is nothing to dry-run                                                                                          |
| **Cloud API calls**                                      | No provider-uniform dry-run. The pre-flight is `testIamPermissions` + a provider `validateOnly`/preview call **where one exists**, and its absence is recorded on the record as `preflight: none` |

Anything outside those three that a server rejects with `dryRun` unsupported is a **failure**, not a
silent skip: the broker records `Failed` with the reason. The list is a code constant, and
`V-BRK`-class checks assert an envelope for each carve-out kind and each non-carve-out kind.

**Blast radius, and what `fractionOfScope` divides by.** Counting happens **after** step 3's
selector expansion, on the concrete object list — a selector matching 300 Deployments is a 300-object
action, not a one-target action. Three bounds, evaluated in this order
([06](06-api-and-data-contracts.md) §4.1–§4.2):

1. An envelope may carry at most **50 literal operations**; more is a schema rejection at step 2.
2. `objects > 50` **or** an expansion that crosses the per-tier caps of §6 ⇒ **`gated`**.
3. `objects > 100` **or** `fractionOfScope > 0.5` ⇒ **hard abort**, no gate offered — a human who
   wants this must narrow the envelope or use a break-glass path outside the agent.

`fractionOfScope`'s **denominator is the count of workload objects in the agent's own scope** at
resolution time — the `Deployment`/`StatefulSet`/`DaemonSet`/`CronJob`/`Job` population of the
agent's namespace (dev-team tier), of every non-system namespace in the cluster (cluster tier), or of
every managed cluster in the project (platform tier). It is deliberately _not_ "all objects": a
namespace with 4000 ConfigMaps would otherwise make any workload change look infinitesimal. The
denominator is recorded on the `ActionRecord` alongside the numerator so the ratio is auditable after
the fact rather than recomputed against a moved world.

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

**Sizing and snapshots.** Pre-state is stored inline when the serialized snapshot is ≤ **1 MiB** —
per-object, and for the envelope's snapshots in total ([06](06-api-and-data-contracts.md) §4.3).
Above that the broker writes a content-addressed blob to the export sink and keeps only
`{digest, sinkRef, size}` inline, so no record approaches etcd's 1.5 MiB object limit. Snapshots are
stripped of `managedFields` and of `Secret` `data` (a Secret's pre-state is recorded as a per-key
digest, never as material — undoing a Secret change restores from the digest-matched value only if
the broker still holds it in the sink under the sink's own encryption).

**Retention, TTL, and export.** Two independent clocks, often confused — keep them apart:

- **`spec.retention.ttl` — record retention.** How long the `ActionRecord` CR itself survives in
  etcd before the cleanup controller deletes it. This is an _evidence_ horizon.
- **`spec.retention.undoWindowExpiresAt` — the guaranteed undo window.** A real timestamp field on
  the record ([06](06-api-and-data-contracts.md) §4.3), set at creation. Inside it, `C-UC` will
  replay the plan on demand. Outside it, the record may still exist and still carry a valid plan,
  but `C-UC` refuses the one-command path — undo becomes a human operation reconstructed from the
  sink. This is a _reversibility_ horizon, and it is always **shorter** than the record's TTL.

| Class                  | Guaranteed undo window (`undoWindowExpiresAt`) | Record TTL (`retention.ttl`) |
| ---------------------- | ---------------------------------------------- | ---------------------------- |
| `routine`              | 7 days                                         | 30 days                      |
| `elevated`             | 30 days                                        | 90 days                      |
| `gated`                | 90 days                                        | 365 days                     |
| `Rejected` (forbidden) | n/a — nothing executed                         | 365 days                     |

A `Rejected` record has no undo window because it has nothing to undo; it is retained the longest
anyway, because a refusal is security evidence and short-lived security evidence is worthless.

The controller garbage-collects terminal records past their TTL, and **only after** the
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
   the one-liner `kubectl kage undo <action-id>` ([06](06-api-and-data-contracts.md) §2b). The
   requester is authenticated by the same `allowedUsers` check as any other command and recorded.
2. `C-UC` loads the `ActionRecord`, validates that the phase is terminal and successful, that the
   undo plan is present and marked valid, and that `spec.retention.undoWindowExpiresAt` is still in
   the future (§1.2).
3. It resolves the **target agent's broker** — the same `(tier, scope)` that executed the original.
   If that broker's Deployment is scaled to zero (`scaleToZero`, §3) it scales it to one and waits
   for readiness; if the agent is **paused**, the broker still serves the `replay` route — pause stops the
   _agent_ from submitting envelopes, it does not disable the broker. If the `Agent` CR was deleted,
   `C-UC` reconstitutes a broker from the recorded tier template, bound to the same actor SA, for
   the duration of the replay.
4. `POST /v1alpha1/actions/{actionId}/replay` runs the recorded plan through the **full pipeline** — re-authenticated, re-scope-checked,
   re-classified, re-snapshotted, executed, verified, and journaled as its own `ActionRecord`. An
   undo that is itself destructive is gated like anything else ([03](03-security-model.md) §6).
   **The linkage is written in both directions, and both are required:** the new record carries
   `spec.trigger.undoOf: <original-action-id>` (forward, immutable, set at creation) and the
   original's `status.undoneBy` is set to the new action id (reverse, written by `C-UC` on success).
   Neither alone is queryable in the direction the other answers — "what undid this?" reads the
   original, "what did this undo?" reads the undo record, and a field-selector query cannot traverse
   a link that only exists on the far object. If the reverse write fails, `C-UC` retries it and the
   record is flagged `undoLinkPending` rather than being left silently one-way
   ([06](06-api-and-data-contracts.md) §4.3).
5. On success `C-UC` diffs the post-undo state against the original pre-state snapshot, records the
   match (or the delta, if the world moved on), sets the **`contested`** marker on every target so
   the agent will not immediately re-apply the change ([04](04-workflow-model.md) §4.2), and reports
   the outcome to the requester.

Undo is never a raw `kubectl apply` of a stored snapshot: routing it through the broker is what
keeps the "every mutation is brokered and journaled" invariant true of undos as well.

### 1.4 The agent mesh (`C-AM`) in detail

The mesh carries **intent between agents** — never authority and never a pre-approved action. A
delegation is a request to consider; the callee decides, and the callee's own broker executes.

**Transport.** **HTTPS + JSON over mTLS**, served by the **agent pod** — not its broker — on the
agent's own `Service` at `<agent>.<ns>.svc:8444/v1alpha1/mesh/{delegate,escalate}`
([06](06-api-and-data-contracts.md) §7), or — across clusters — to the hub's VPC-internal endpoint
(§5). Note the deliberate separation: the mesh is a **reasoning** interface and lands on the agent's
port `8444`; the broker's action API is a **write** interface on `<agent>-broker.<ns>.svc:8443`
(§1.1). A mesh peer can never address another agent's broker, and the NetworkPolicy encodes exactly
that. Two message kinds, both async
request/acknowledge rather than a held synchronous call: the callee acks receipt with a correlation
ID and reports the outcome back over the mesh when its own loop finishes. In-pod, a mesh message
terminates on the **existing session-inject seam** that `C16` and `C17` already speak
(`POST /sessions` + `/sessions/{sid}/inject`, kind-discriminated), so `delegation` and `escalation`
join `alert`, `github`, and `k8s-event` under one delivery contract rather than adding a second one.

**Discovery.** Peers are resolved from `Agent` CRs, never from configuration:

- The controller stamps the **five** identity labels — `kube-agents/role` (`reader` | `actor`),
  `kube-agents/agent` (the CR name), `kube-agents/tier`, `kube-agents/scope`, `kube-agents/parent` —
  on every agent and broker Deployment, pod, and `Service`
  ([08](08-agent-runtime-and-identity.md) §2.5). _(Delta: today only `kube-agents/tier` is stamped —
  `k8s-operator/internal/controller/agent_manifests.go`; the other four are required by this design.
  `role` and `agent` are the pair that lets a Service select a broker and lets admission pin a pod to
  the SA class it binds — [03](03-security-model.md) §4.3.)_
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

**Loop and cascade control.** Every mesh message carries a delegation chain ID and a depth counter
in `MeshRequest.chain` ([06](06-api-and-data-contracts.md) §7); depth > 3 is rejected, and an
identity already present in `chain.visited` is rejected as a cycle.

**The chain ID is a label, and that is the mechanism behind "one queryable object graph."** The
promise is otherwise decorative, so state the implementation: the callee propagates
`MeshRequest.chain.id` into every envelope it submits as a result, and the broker stamps it as the
**label `kube-agents/chain-id`** on each resulting `ActionRecord`. A fleet-wide rollout is therefore
retrieved with one selector —
`kubectl get actionrecords -A -l kube-agents/chain-id=<id>` — across every tier, namespace, and
cluster that participated, and the same label is the join key in the exported audit stream. A label
rather than a spec field precisely because label selectors are server-side and indexed; a spec field
would force a client-side scan of the whole journal. The chain ID also rides the trace, so the OTel
view and the object view answer the same question. Delegations count against the **callee's**
initiative budget, not the caller's — a parent cannot spend a child's budget
([04](04-workflow-model.md) §4.2).

**When a peer is down.** Escalations are retried with exponential backoff and held in the escalating
agent's durable queue (C11) with a TTL; on expiry the escalation is surfaced to the human roster
over C15 rather than dropped. A child whose parent is unreachable **keeps operating within its own
scope** — the mesh is not on the child's critical path (§8 CH8).

### 1.5 The brake surface (`C-BR`) in detail

| Control     | Object                                                             | Where enforced                                                                                                             |
| ----------- | ------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------- |
| `pause`     | `spec.operations.paused: true` on the `Agent` CR _(new CRD field)_ | The broker refuses new `POST /v1alpha1/actions` calls; the controller stops the agent's trigger sources (C16/C17 delivery) |
| `freeze`    | Cluster-scoped `FleetFreeze` CR, optionally scoped to a subtree    | Every broker consults it on **every** envelope, **fail-closed** if it cannot be read                                       |
| `contested` | `kube-agents/contested: <action-id>` annotation on a target        | The broker rejects any envelope re-applying the same change to that target absent explicit instruction                     |
| `undo`      | `UndoRequest` (§1.3)                                               | `C-UC` → the broker's `replay` route                                                                                       |

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

**Auto-brake — and the line between "escalate" and "pause".** These are different responses to
different classes of trouble, and conflating them is a real bug: an agent that pauses itself every
time it hits an hourly budget is an agent that stops working every busy afternoon.

| Trigger                                          | Response                                                | Who                                              | Why not the other one                                                                                                               |
| ------------------------------------------------ | ------------------------------------------------------- | ------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------- |
| **Initiative budget exhausted**                  | **Refuse the envelope and escalate.** No pause          | Broker ([04](04-workflow-model.md) §4.2)         | The budget is a rate bound on a _healthy_ agent. Nothing suggests misbehaviour, and the agent must stay live to report and to serve |
| **Flap threshold breached** on a target          | **Refuse for that target, mark it, escalate.** No pause | Broker ([04](04-workflow-model.md) §4.2)         | The evidence is that one diagnosis is wrong, not that the agent is compromised. Its other work is unaffected                        |
| **Repeated `forbidden` attempts**                | **Auto-pause** + security event                         | Broker → `Agent.spec.operations.paused`          | Repeatedly reaching for authority it does not have is the signature of injection or a bad prompt, not of load                       |
| **Verification failed and rollback also failed** | **Auto-pause** + page                                   | Broker                                           | The world is in a state the system did not intend and cannot restore. Every further action compounds unknown state                  |
| **Anomaly trip** (rate / class mix / target set) | **Auto-pause** + page                                   | `C-AD` → `C-BR` (§1.7)                           | The stream itself is the evidence; the broker cannot see it, because each envelope is individually legitimate                       |
| **Journal store unreachable**                    | **Refuse to execute, then auto-pause**                  | Broker ([06](06-api-and-data-contracts.md) §4.4) | Unjournaled execution is the one thing that must never happen; refusing is not enough because the agent will keep trying            |

This matches [04](04-workflow-model.md) §4.2 ("stop and escalate, never slow down silently") and
[06](06-api-and-data-contracts.md) §4.4's fail-closed table. In all four pause cases the writer
sets the same `spec.operations.paused` field a human would, with `pauseReason`, so recovery is the
same one-line `resume` operation and there is no second, hidden brake state.

### 1.6 The journal reconciler (`C-JR`) in detail

**Why it must exist as a component.** "Every mutation is journaled" is enforced at admission for
Kubernetes `create`/`update`/`patch`: `C-AS` rejects an actor-identity write with no
`kube-agents/action-id` ([03](03-security-model.md) §4.3). That enforcement has three holes it
cannot close, by construction:

1. **Deletes.** A `DELETE` admission request carries **no client object**, so there is nothing to
   carry the annotation and nothing for a policy to inspect. Admission can see _that_ an actor
   deleted something; it cannot require an id.
2. **Cloud writes.** GCP APIs do not run Kubernetes admission at all. The Platform Agent's most
   consequential actions are invisible to `C-AS`.
3. **A fabricated or reused id.** `C-AS` checks that the annotation is _present_, not that it names
   a real record, and not that the record was for _this_ object. Any actor identity can stamp
   `action-id: <some id it already used>` on an unrelated write and pass admission.

Until this audit those three were cited across [03](03-security-model.md) §4.3/§7 and
[08](08-agent-runtime-and-identity.md) §4 as "journal-completeness reconciliation" with no component
behind them, which made SLI 2 unverifiable. `C-JR` is that component.

**Placement.** One instance per cluster, hosted in the kube-agents controller process (`C1`,
`kubeagents-system`), plus a hub-side instance for Cloud Audit Logs. It runs with the controller's
own identity, **not** an agent identity, and it holds **no write verb on any target resource** — its
only writes are metrics, events, and the brake (via `C-BR`). It is deliberately a separate reconcile
loop from the journal exporter: the exporter is on the availability path for garbage collection, and
a reconciler outage must not stop export or vice versa.

**Interfaces.**

| Direction | Interface                                                                                                                                                                                                                                             |
| --------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Reads     | (a) the **Kubernetes audit log** for the cluster, filtered to agent ServiceAccount principals (§5.1); (b) the **Cloud Audit Log** `activity` stream filtered to actor GSAs, hub instance only; (c) the **exported `ActionRecord` stream** from `C-JS` |
| Emits     | `kubeagents/unjournaled_mutation`, `kubeagents/action_id_fabricated`, `kubeagents/action_id_reused`, `kubeagents/cloud_mutation_unmatched`, each labelled by tier, scope, and agent; a Kubernetes `Warning` event on the `Agent` CR                   |
| Calls     | `C-BR` to auto-pause the implicated agent, and the C15 router to page                                                                                                                                                                                 |

**Cycle time.** A continuous tail with a **60 s reconcile cycle** and a **5 min late-arrival grace
window** before a mismatch is declared (log sinks and the journal exporter are both eventually
consistent, and a 60 s export budget plus clock skew makes anything tighter a false-positive
generator). Cloud Audit Log delivery is slower and less predictable, so the cloud instance uses a
**15 min** grace window. Both are code constants, reported in the metric's labels so an alert can be
read against the window that produced it.

**The matching rule — and why `action-id` alone is not enough.** `C-JR` matches on the pair
**`(action-id, target GVKNN)`**: the audit entry's `kube-agents/action-id` annotation _and_ its
`group/version/kind/namespace/name`, against the `spec.targets[]` of the record with that id.
Matching on the id alone would let a reused id launder an arbitrary number of unrelated writes —
stamp one legitimate id on a hundred objects and every one of them "has a record". Requiring the
target to appear in that record's own target list makes an id useful exactly once per object it was
actually planned for. Three outcomes:

| Finding                                                                              | Meaning                                                | Action                                                                                         |
| ------------------------------------------------------------------------------------ | ------------------------------------------------------ | ---------------------------------------------------------------------------------------------- |
| Write with **no** `action-id` (a delete, or a cloud call)                            | Unjournaled mutation — the hole admission cannot close | `unjournaled_mutation` += 1; **auto-pause the agent**; page. Treated as a suspected compromise |
| `action-id` present, **no record exists** with that id                               | Fabricated id                                          | `action_id_fabricated` += 1; **auto-pause**; page. There is no benign cause                    |
| `action-id` present, record exists, **target not in that record's `spec.targets[]`** | Reused id                                              | `action_id_reused` += 1; **auto-pause**; page                                                  |
| Record exists in `PendingApproval`/`Expired`/`Rejected` but the write happened       | Gate bypass                                            | Escalated as a §5.1 SLI 1 event, not merely SLI 2 — this is a containment failure              |
| Record exists, target matches, phase executed                                        | Journaled correctly                                    | Counted in the denominator; nothing else                                                       |

The **inverse** direction is also checked and is cheaper to get wrong: a record in a terminal
executed phase whose write **does not appear** in the audit log within the grace window means the
journal is claiming an action that did not happen. That emits `kubeagents/phantom_action` and pages,
because a journal that overstates is as dangerous as one that understates — undo plans would be
generated against a state that was never entered.

**SLO.** `C-JR` detects and reports any unmatched Kubernetes mutation **within 5 min p95 / 15 min
p99** of the write, and any unmatched cloud mutation **within 15 min p95 / 30 min p99**. Its own
liveness is an SLI input: if `C-JR` has not completed a cycle in **10 min**, `kubeagents/jr_stale`
fires — a silent reconciler is indistinguishable from a clean fleet, which is exactly the failure
mode a backstop must not have. `C-JR` being down does **not** block writes (it is off the action
path entirely, by design — see §2); it degrades detection, and that degradation is visible.

**Honest limits.** `C-JR` is a **detective** control, not a preventive one: it names the window
during which an unjournaled write went unnoticed, it does not shrink it to zero. That is why
admission enforcement stays the primary mechanism wherever it can apply, and why §6 states journal
durability as two claims rather than one.

### 1.7 The anomaly detector (`C-AD`) in detail

**Why it must exist as a component.** [03](03-security-model.md) §8.1 states the residual risk
plainly — a successful prompt injection can cause **any action the agent was already authorized to
perform** — and then bounds it with five mitigations, one of which is "it trips anomaly alerting …
an unusual action rate, class mix, or target set auto-pauses the agent".
[08](08-agent-runtime-and-identity.md) §4 leans on the same sentence twice as a bound on standing
actor credentials. No component owned it, so the bound was rhetorical. `C-AD` owns it.

The property it adds over everything else in §1: the broker judges **one envelope at a time**, and a
compromised agent's envelopes are each individually legitimate — in scope, under the caps, undoable.
The signal is only in the **stream**. `C-AD` is the only component that looks at the stream.

**Placement and interfaces.** A controller-hosted watcher in `C1`, one per cluster, reading the
`ActionRecord` watch for every `Agent` in that cluster (including `Rejected` and `PendingApproval`
records — refusals are signal). It holds no actor identity and cannot write to any target. On a trip
it calls **`C-BR`**, which performs the actual pause by patching `spec.operations.paused: true` with
`pauseReason: anomaly/<dimension>` — the same field a human uses, so `resume` is unchanged and there
is no separate anomaly state to clear. `C-AD` never pauses directly, so there is exactly one code
path that stops an agent.

**The three dimensions.**

| Dimension              | Statistic                                                                                                           | Default trip                                                                                                                                               |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Rate**               | Executed actions per rolling 15 min, compared to the agent's own trailing 14-day distribution for that hour-of-week | `> max(baseline_p99 × 3, baseline_p50 + 20)` — the floor stops a near-idle agent tripping on its third action of the week                                  |
| **Risk-class mix**     | Share of `elevated` + `gated` + `forbidden`-rejected in the last 50 actions, vs the trailing baseline share         | Elevated-and-above share exceeding `baseline + 0.25` absolute, **or** ≥ 3 `forbidden` rejections in 15 min (which also auto-pauses via §1.5 independently) |
| **Target-set novelty** | Fraction of the window's targets whose `(kind, name-prefix)` was never touched by this agent in the baseline        | `> 0.5` of a window of ≥ 10 actions. Touching things it has never touched is the clearest injection tell, and the hardest to fake as legitimate            |

Dimensions are evaluated independently and any one trips. Thresholds are per-tier configuration with
a code ceiling; they may be tightened by an operator, never loosened past the ceiling, and never by
an agent.

**Baseline.** Per **agent**, not per tier — a Developer Team Agent in a busy namespace and one in a
quiet namespace are not comparable, and a fleet baseline would be dominated by whichever tier is
noisiest. Built from that agent's own `ActionRecord` history over a **trailing 14 days**, bucketed
by hour-of-week to absorb the daily and weekly shape of real operations, recomputed hourly.
**Cold start:** a new agent has no baseline, so for its first **7 days** `C-AD` runs in
**observe-only** mode — it computes and exports the metrics and fires no pause. A brand-new agent is
also the one most likely to be doing bulk legitimate work (initial policy application), and pausing
it on day one would train operators to disable the detector. Records produced while an agent was
paused, frozen, or in a declared change window are excluded from the baseline so an incident does not
poison the following fortnight.

**Distinguishing a legitimate burst — the part that makes this more than a rate limiter.** A
fleet-wide policy rollout is, at the object level, exactly what a compromised agent looks like: many
actions, quickly, against targets it has not touched before. Four discriminators, evaluated before a
trip:

1. **Provenance.** A burst whose records carry the **`kube-agents/chain-id` label** of a single
   delegation chain (§1.4) originating from an authenticated **parent** agent is a rollout. The
   chain is the causal evidence that a legitimate operator up the hierarchy asked for this. `C-AD`
   collapses one chain to **one** event for rate purposes, however many records it produced.
2. **Homogeneity.** A rollout is the _same_ intent template and the _same_ classifier rule id across
   its targets; injection-driven bursts are heterogeneous. A burst whose records share one
   `(classifier rule id, intent template)` and one risk class is scored as a single logical action,
   not N.
3. **Requester.** A burst attributed to an authenticated human on the approval roster
   (`spec.requester.kind: human`) is not self-initiated activity and is scored separately from the
   self-initiated stream. The bound `C-AD` exists to enforce is on what the agent does **on its own
   initiative**.
4. **Declared change windows.** An operator may open a `ChangeWindow` — an annotation on the `Agent`
   CR with an expiry and a reason — which raises the rate threshold by a stated multiplier for its
   duration. It **never** relaxes the class-mix or novelty dimensions, and it is itself a journaled,
   roster-only action, so "the attacker just opens a change window" requires already holding roster
   authority.

What survives all four: many actions, in a short window, **not** in one chain, **not** homogeneous,
**not** human-requested, against novel targets. That is the shape `C-AD` is looking for, and it is
not a rate limiter's shape — a fleet rollout of 400 objects inside a single chain does not trip it,
while 12 heterogeneous self-initiated actions against never-before-touched Secrets does.

**Failure behaviour.** `C-AD` is **off the action path** and fails **open with respect to writes**:
if it is down, brokers keep executing (it is a detective control, and coupling the write path to a
detector would hand an attacker a denial-of-service by crashing it). It fails **closed with respect
to silence**: a detector that has not completed an evaluation cycle in **10 min** emits
`kubeagents/ad_stale` and pages, and its staleness is surfaced on the `Agent` CR status so an
operator can see that the bound they are relying on is not currently being enforced. Every trip,
every near-miss within 80% of a threshold, and every observe-only would-have-tripped is exported, so
thresholds can be tuned from evidence rather than from taste ([07](07-implementation-roadmap.md) §4).

**Honest limit.** A patient attacker who stays inside the baseline evades `C-AD` — a detector tuned
tightly enough to catch slow, in-baseline abuse would pause honest agents constantly. `C-AD` bounds
the **fast, broad** compromise; the **slow, narrow** one is bounded by scope, gating, and the fact
that every action it takes is journaled and undoable. Say so rather than claiming coverage the
mechanism does not have.

## 2. Topology (hub-and-spoke)

```
        ┌──────────────────────── HUB CLUSTER (kubeagents-system) ─────────────────────────┐
        │  C1 controller + C-AS admission   C15 ChatOps router   C5 inference   C6 Minty   │
        │                                                                                  │
 human ─┼─chat──▶ C2 Platform Agent ──envelope──▶ C-AB broker ──write──▶ GKE / GCP APIs    │
        │           (reader SA)   ◀──report────   (actor SA)  │                            │
        │  C-UC undo ──replay──────────────────────▶          ▼                            │
        │                                          C-JS journal (ActionRecord CRs)         │
        │  C-JR reconciler ◀─audit log + journal──────────────┤  C-AD detector ─▶ C-BR     │
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
    │  C-JR reconciler   C-AD detector            │  C-JR   C-AD                 │
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

**Control-plane identities that are not agent identities.** Besides the per-agent reader/actor pair
([03](03-security-model.md) §3.1), three control-plane ServiceAccounts write to the journal or its
lifecycle, and each belongs to a component named here:

| ServiceAccount (in `kubeagents-system`) | Component        | What it may touch                                                                                                                                       |
| --------------------------------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `kube-agents-controller`                | C1               | Agent/broker workloads and their certs — **never** RBAC, SAs, or `ActionRecord`s ([08](08-agent-runtime-and-identity.md) §2.7)                          |
| `kube-agents-chatops-gateway`           | C15              | `actionrecords/status` approvals only — it records a human's approve/reject decision, and can do nothing else ([06](06-api-and-data-contracts.md) §4.3) |
| `kube-agents-retention-controller`      | C-JS (lifecycle) | `delete` of expired records only, past TTL and after export confirms — no read of content, no status writes                                             |

None of the three is an agent identity, so none is in scope for the agent RBAC templates; but all
three are in scope for the forbidden set's audit-tampering rule, and the retention controller's
deletion predicate is the one place in the system where an `ActionRecord` may legitimately be
removed.

| Component                                      |                                Hub cluster                                |              Spoke cluster               | Namespace                        |
| ---------------------------------------------- | :-----------------------------------------------------------------------: | :--------------------------------------: | -------------------------------- |
| kube-agents controller (C1) + admission (C-AS) |                                    ✅                                     | ✅ (reconciles that cluster's Agent CRs) | `kubeagents-system`              |
| Platform Agent (C2) + its broker (C-AB)        |                                    ✅                                     |                    —                     | `kubeagents-system`              |
| Cluster Admin Agent (C3) + its broker          |                                     —                                     |              ✅ (1/cluster)              | `kubeagents-system`              |
| Developer Team Agent (C4) + its broker         |                                     —                                     |             ✅ (1/namespace)             | the team's namespace             |
| Journal store (C-JS)                           |                                    ✅                                     |                    ✅                    | records in the agent's namespace |
| Journal reconciler (C-JR)                      |                ✅ (+ the **cloud**-log instance, hub only)                |         ✅ (K8s audit log only)          | `kubeagents-system` (in C1)      |
| Anomaly detector (C-AD)                        |                                    ✅                                     |                    ✅                    | `kubeagents-system` (in C1)      |
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
   condition, requester, trace ID ([06](06-api-and-data-contracts.md) §4.1) — as HTTP+JSON over
   mTLS to `POST https://<agent>-broker.<ns>.svc:8443/v1alpha1/actions`.
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
    attribution — and emits the audit event. Only then does the `POST /v1alpha1/actions` response return.
11. The agent **reports** to the human: what it did, what it observed, and the undo handle. For an
    `elevated` action the owning humans are notified immediately rather than in the digest
    ([03](03-security-model.md) §5.1).
12. _(Optional, asynchronous, off the critical path.)_ The broker renders the executed state as IaC
    and writes it behind to the mirror repository via a Minty-brokered token
    ([04](04-workflow-model.md) §6). Failure here is reported and retried; it **never** rolls back
    or delays the action.
13. _(Asynchronous, off the critical path, always on.)_ Within the next cycle **`C-JR`** (§1.6) joins
    step 8's audit-log entries against step 10's record on `(action-id, target GVKNN)` and confirms
    the two sides agree, and **`C-AD`** (§1.7) folds the record into the agent's action stream. Both
    are watchdogs, not gates: neither can delay or block an action, and neither being down stops
    step 8 — what stops is the _detection_ of a step that skipped the rest of the flow. `C-JR` is the
    only observer of steps that never reached step 3 at all (a delete, a cloud call), which is why
    it exists.

**F2 — Read / observe.** Agents read cluster and cloud state with their **reader** identity
(read-only RBAC + read-only cloud SA) and telemetry from C12. Reads are bounded by the agent's own
tier scope, not by the requester's permissions (v1, [03](03-security-model.md) §4a). Any write
attributed to a reader identity in the audit log is a P1 alarm by construction
([01](01-vision-scope.md) §7 SLI 2) — raised by `C-JR` (§1.6), which is the component that actually
reads the audit log and notices.

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
5. The whole cascade is journaled as a chain of `ActionRecord`s carrying one
   `kube-agents/chain-id` label (§1.4), so provisioning a cluster and its tenants is one undoable
   object graph retrievable with a single label selector — and, for the same reason, is scored by
   `C-AD` as one logical action rather than as a burst (§1.7).

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
   ([04](04-workflow-model.md) §4.2) — both of which **escalate without pausing** (§1.5).
7. Because F6 is the flow with no human in it, it is also the flow `C-AD` (§1.7) exists to bound: the
   proactive stream is exactly the self-initiated action stream it baselines. An F6 burst that is
   not attributable to a delegation chain, not homogeneous, and aimed at novel targets auto-pauses
   the agent — which is the difference between "relentless" and "compromised" being an observation
   rather than an assertion.

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
   own record — carrying `spec.trigger.undoOf` forward and setting `status.undoneBy` on the original
   in return (§1.3).
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
4. An approver on the roster responds. The approval is an authenticated
   `POST /v1alpha1/actions/{actionId}/approve` on the broker; the approver's identity is recorded in the record. A non-roster human's
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
  garbage collector deletes terminal records past their class TTL **only after** export is
  confirmed. `kubectl get actionrecords -A -o wide` is the primary human view; the ChatOps `history`
  and `undo` commands read the same objects.
- **Journal reconciler (C-JR):** see §1.6. Operationally: a controller-hosted loop with a 60 s cycle
  reading the cluster's audit log sink and the exported journal, one instance per cluster plus a
  hub-side instance for Cloud Audit Logs. It needs read access to the log sink (a Cloud Logging
  reader binding, or a local sink in a non-GCP install) and nothing else. It is the producer of SLI
  2 and the sole enforcement for deletes and cloud writes.
- **Anomaly detector (C-AD):** see §1.7. Controller-hosted, one per cluster, watching
  `ActionRecord`s only. Its baselines are recomputed hourly from the last 14 days and are held in
  memory with a periodic checkpoint to a ConfigMap, so a controller restart does not reset every
  agent to cold-start; a checkpoint older than 24 h is discarded and the agent re-enters
  observe-only until a baseline is rebuilt.
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

**The principal filter — a silent-blindness bug, called out because it is invisible when wrong.**
The natural filter is the namespace group `system:serviceaccounts:kubeagents-system`, and
`config/audit/audit-policy.yaml` uses exactly that today. **It is wrong, and wrong in the worst
possible direction:** only the Platform and Cluster Admin tiers live in `kubeagents-system`. A
Developer Team Agent's reader SA (`developer-team-agent`) and its broker's actor SA
(`developer-team-<ns>-actor`) both live in **the tenant's own namespace** (§3). A namespace-scoped
filter therefore omits the **most numerous tier in the fleet** — up to 200 agents per cluster against
one Cluster Admin — from SLIs 1, 2 and 3. Nothing fails; the dashboards read zero; the tier with the
highest cardinality and the lowest per-agent scrutiny is simply not being watched.

The canonical filter is therefore on the **ServiceAccount name pattern, across all namespaces**,
matching the two naming conventions that [03](03-security-model.md) §3.1 makes structural and
[08](08-agent-runtime-and-identity.md) §2.5 pins with `kube-agents/role`:

```text
principal MATCHES "^system:serviceaccount:[^:]+:(platform|cluster-admin|developer-team)-agent$"
       OR principal MATCHES "^system:serviceaccount:[^:]+:(platform|cluster-admin|developer-team)-[a-z0-9-]+-actor$"
```

— i.e. the `<tier>-agent` reader convention and the `<tier>-<scope>-actor` actor convention, in any
namespace. Two consequences to enforce rather than assume: (a) the audit policy must capture
`RequestResponse` at these principals **cluster-wide**, not under a namespace selector; and (b)
because the filter is now a name pattern, the **name pattern is load-bearing** — `C-AS` already
constrains agent SA names to the tier template ([03](03-security-model.md) §3.4), and that
constraint is what stops an agent identity from existing outside the filter's reach. A check that
enumerates every SA carrying `kube-agents/role` and asserts each one matches the pattern is the
mechanical guard, and it belongs in the same suite as the SLIs themselves.

| SLI                               | Source & computation                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | Target / alert                                                                                                                                                                                                                                       |
| --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **1. Zero cross-scope escapes**   | K8s audit log + Cloud Audit Logs, under the **SA name-pattern filter above** (`config/audit/audit-policy.yaml` must be corrected from its current namespace-scoped filter). Any request whose principal is an agent identity (reader **or** actor) and whose resource falls outside the scope registered for that identity, including `SubjectAccessReview` **allow** results. Metric `kubeagents/cross_scope_escape`                                                                                                                                                                                                                                                      | **0.** Any occurrence pages. Auto-pauses the agent                                                                                                                                                                                                   |
| **2. Zero unjournaled mutations** | Three detectors, of decreasing strength. (a) **Prevented at admission** — `C-AS` rejects any actor-identity `create`/`update`/`patch` lacking `kube-agents/action-id`, so the failure mode is a denial, not a silent write. (b) **Detected by `C-JR`** (§1.6) — the join of audit-log writes against exported `ActionRecord`s on **`(action-id, target GVKNN)`**, which is the _only_ coverage for **deletes**, **cloud writes**, and **fabricated or reused ids**. (c) **Reader-identity writes** — any write at all by a reader identity, from the same join. Metrics `kubeagents/unjournaled_mutation`, `/action_id_fabricated`, `/action_id_reused`, `/phantom_action` | **0.** Any occurrence pages and auto-pauses. Detection within `C-JR`'s SLO (5 min p95 K8s / 15 min p95 cloud), **not** instantaneously — see §6                                                                                                      |
| **3. Zero self-escalations**      | Audit-log filter, again under the **SA name-pattern filter above**, for any agent-identity request that creates/modifies `Role`/`ClusterRole`/`*Binding`/IAM policy/Workload-Identity binding **naming an agent identity**, uses `escalate`/`bind`/`impersonate`, or touches an `Agent` CR, a VAP, the controller, a broker, or an `ActionRecord`. Excludes the attenuation-checked child-provisioning path (F4). Metric `kubeagents/self_escalation_attempt`                                                                                                                                                                                                              | **0** executed; attempts are counted and page on repetition                                                                                                                                                                                          |
| **4. Undo health**                | Journal exporter gauges over a **rolling 7-day window**: `kubeagents/undo_plan_coverage` = executed non-gated records with `status.undoPlan.valid` ÷ all executed non-gated records; `kubeagents/undo_success_rate` = successful undos ÷ attempted undos. The denominator of the second is guaranteed by a **synthetic undo canary** — a scheduled routine action in a canary namespace, immediately undone and diffed, **hourly per cluster**, so the window always contains **≥ 150 undo attempts** and the ratio is never computed over a near-empty set                                                                                                                | Coverage **= 1.0**. Success rate **≥ 0.999 over ≥ 150 attempts / 7 d**, and **any single canary failure pages** regardless of the ratio. A window with < 150 attempts is reported `insufficient-data` and pages on the canary, never silently as 1.0 |

**Why SLI 4 is a rate with a floor and not "no silent failures."** The 01 §7 phrasing is
unfalsifiable: a rate with no denominator reads 1.0 when nothing happened, so a fleet whose undo path
is completely broken and a fleet with no undos look identical, and a single real failure among two
attempts reads 0.5 and pages while the same failure among two thousand does not. The canary supplies
the denominator (≥ 150 attempts per 7-day window per cluster, from an hourly schedule), the ratio
supplies the trend, and the unconditional page on any canary failure supplies the "zero tolerance"
the invariant actually wants. `insufficient-data` is a distinct state from `healthy` — a monitoring
system that cannot tell those apart is the thing this row exists to prevent.

Component ownership of the four: **1 and 3** are pure log-based metrics with **no kube-agents
component in the loop**, so they keep working if the kube-agents control plane is itself the thing
misbehaving. **2** is produced by **`C-JR`** (§1.6) for the after-the-fact half and by `C-AS` for the
prevented half. **4** is produced by the journal exporter plus the canary. `C-AD` (§1.7) feeds none
of the four directly; it is a control, not an SLI, and its own health (`kubeagents/ad_stale`) and
`C-JR`'s (`kubeagents/jr_stale`) are **meta-SLIs** that must be on the same dashboard — a green board
produced by a dead watchdog is the specific failure this set is arranged to make impossible.
Proactivity
counters (MTTR by severity, share of issues resolved without a human, actions per agent per day,
flap and revert counts) come from the same journal export and are graphed alongside — "relentless"
and "not thrashing" are read on one dashboard.

## 6. Non-functional requirements (targets — defaults, tune later)

| Dimension                  | Default target                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | Rationale                                                                                                                                                                                                      |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Fleet scale                | ≥ 50 spoke clusters per hub                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | Fleet-governance use case                                                                                                                                                                                      |
| Agents per cluster         | 1 Cluster Admin + ≤ 200 Dev Team (namespaces), each with its own broker                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | Namespace density on GKE                                                                                                                                                                                       |
| **Action latency (K8s)**   | Envelope → executed → journaled: **p95 < 5 s, p99 < 15 s** for a single-object routine write (classification + snapshot + SSA + verify + record)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | The action is inside a chat turn now, not behind a merge                                                                                                                                                       |
| **Action latency (cloud)** | p95 < 60 s for a synchronous cloud API; long-running operations (cluster create, node-pool resize) execute asynchronously with the record in `Executing` and a bounded poll, p95 < 15 min                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | Cloud control planes, not kube-agents, set this floor                                                                                                                                                          |
| **Chat turn latency**      | p95 < 10 s for read/plan; **p95 < 20 s from message to "done + undo handle"** for a routine single-object mutation. Deterministic routing adds no inference; NL routing adds one router call                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | Mutations are now synchronous, so they are inside the user-visible budget                                                                                                                                      |
| **Broker throughput**      | **Per broker:** ≥ 20 envelopes/s sustained, ≥ 5 concurrent executions, serialized per target object. **Per cluster:** ≥ 500 envelopes/s aggregate — a **control-plane** bound, not a sum of brokers (see below)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | A fleet-wide rollout fans out across brokers, so per-broker rate is modest                                                                                                                                     |
| **Time to remediate (F6)** | Detection → verified fix, p50 < 2 min for routine namespace-scope issues; p95 < 15 min including diagnosis                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | The product claim, measured                                                                                                                                                                                    |
| **Undo latency**           | `undo` → restored + verified: **p95 < 30 s** (K8s targets), < 5 min (cloud). Add ≤ 20 s when the target broker must be woken from `scaleToZero`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | The brake has to feel instant or it is not a brake                                                                                                                                                             |
| **Brake propagation**      | `pause` → broker refuses new envelopes **< 2 s p99**; in-flight action completes or rolls back < 30 s; local `FleetFreeze` apply → effective < 2 s; **fleet-wide freeze fan-out to the last spoke < 30 s**; all of it with inference down                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | [03](03-security-model.md) §6 requires the brake to work unconditionally                                                                                                                                       |
| **Journal durability**     | **Two claims, not one.** **(a) Kubernetes `create`/`update`/`patch`: 100%, _enforced_** — `C-AS` rejects an unannotated actor write and the broker fails closed, so the failure mode is a denial. **(b) Kubernetes `delete`, subresource writes, and all cloud writes: _reconciled within an SLO_, not enforced** — a `DELETE` admission request carries no object to annotate and cloud APIs run no Kubernetes admission, so coverage is `C-JR`'s detection SLO (§1.6): unmatched K8s mutation reported ≤ 5 min p95 / 15 min p99, unmatched cloud mutation ≤ 15 min p95 / 30 min p99, with **0** unmatched mutations tolerated at steady state. Export to the audit sink < 60 s. Record TTL 30/90/365 d by class (+365 d for `Rejected`); guaranteed undo window 7/30/90 d; sink retention ≥ 400 days                                                                                                                                        | SLI 2 is an invariant where it can be enforced and a bounded-latency detection where it cannot. Claiming "enforced, not measured" flatly would be false for exactly the two riskiest write classes             |
| **Blast-radius caps**      | Per action, on the **post-expansion** object set (§1.1): **> 50 objects ⇒ `gated`**; **> 100 objects or `fractionOfScope` > 0.5 ⇒ hard abort**; an envelope carries ≤ 50 literal operations. Plus the tier caps: ≤ 1 namespace for the dev-team tier, ≤ 1 node pool for the cluster tier. `fractionOfScope`'s denominator is **workload objects in the agent's scope** (§1.1)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | Bounds "correct remediation applied at machine speed" ([03](03-security-model.md) §1 class C)                                                                                                                  |
| **Initiative budget**      | **Per class**, per agent, per rolling hour: default ≤ 50 `routine` **+** ≤ 10 `elevated`. Carried by **per-class fields on the `Agent` CRD** ([06](06-api-and-data-contracts.md) §1.1) — a single class-agnostic counter cannot express it. Human-requested actions draw on a separate, larger allowance. Flap cooldown per target. Exhaustion **refuses and escalates; it does not pause** (§1.5, [04](04-workflow-model.md) §4.2)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | Relentless, not thrashing — and a budget that pauses the agent turns a rate bound into an outage                                                                                                               |
| **Availability**           | **Broker ≥ 99.9% per scope**, measured as successful-response minutes ÷ eligible minutes over a **rolling 28-day window per broker**, probed by a **1/min synthetic no-op envelope** from the agent pod (a `dryRun`-only classify-and-reject envelope against a canary object, which exercises auth, informers, classification, and the journal read path without writing). **Unavailable** = a probe that times out (> 10 s), returns 5xx, or fails TLS/`TokenReview`. **Not** unavailable, and excluded from the denominator: `403 agent-paused` / `403 fleet-frozen` / `403 target-contested` (the brake working is not an outage) and `429` (backpressure is a designed response). A window with < 90% probe coverage is `insufficient-data`, not `available`. **Spoke brokers execute with the hub down**; agent _reasoning_ pauses (hub-hosted inference). Controller down ⇒ running agents and brokers keep working, no new reconciles | No cascade; the hub is a reasoning dependency, not an acting one. Excluding the brake responses matters: a frozen fleet would otherwise read as a fleet-wide outage and train operators to distrust the metric |
| **Recovery**               | **Agent pod restart → Ready: p95 < 10 s, p99 < 30 s** from container start (PVC-backed state; the p99 covers PVC reattach). **Broker restart → serving: p95 < 5 s, p99 < 15 s**, then reconcile every non-terminal `ActionRecord` it owns within a further **30 s p95** — complete or roll back, never leave half-applied. Measured from the kubelet's container-start timestamp to the first successful readiness probe on `:8081/readyz` (§1.1)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | The write-ahead journal is what makes restart safe (§1.1). "A few s" is not a target — a percentile and a probe are                                                                                            |
| **Watchdog freshness**     | `C-JR` completes a cycle every 60 s (`jr_stale` at 10 min); `C-AD` completes an evaluation every 60 s (`ad_stale` at 10 min); both are off the write path and neither can block or delay an action                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | A backstop whose silence is indistinguishable from a clean fleet is not a backstop (§1.6, §1.7)                                                                                                                |
| **Footprint**              | Broker 50m/64Mi request, 200m/256Mi limit, 1 replica. Idle Dev Team agents `scaleToZero` **including their brokers**                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | Bounds the per-namespace cost of one-broker-per-agent                                                                                                                                                          |
| **Cost**                   | Shared inference in the hub; Spot-eligible agent pods; brokers are not Spot-eligible (they hold in-flight actions)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | Avoid per-cluster duplication without risking mid-action eviction                                                                                                                                              |

**Why the cluster aggregate is 500/s and not 4000/s.** 200 brokers × 20 envelopes/s is 4000/s, and
the two numbers look contradictory. They are not: the per-broker figure is a **per-process** target
and the cluster figure is a **shared-resource** bound, and the binding constraint is never the
brokers. It is, in order:

1. **The API server's mutating write path.** Every envelope is ≥ 1 `GET` (snapshot) + 1 dry-run apply
   - 1 apply + ≥ 1 `GET` (verify) + 2 `ActionRecord` writes — call it **6–8 API operations per
     envelope**, of which 3 are writes through etcd's serialized commit path. 500 envelopes/s is
     already ~3500 API ops/s and ~1500 writes/s against one control plane, which is the practical
     ceiling of a regional GKE control plane before admission and etcd latency degrade for
     _everything else in the cluster_.
2. **`ActionRecord` write amplification in etcd.** The journal shares etcd with the objects being
   mutated, so journal throughput and workload throughput compete (§1.2 accepts this trade
   deliberately).
3. **Admission.** `vap-agent-scope` evaluates on every actor write, and `C-AS`'s webhook on a subset.

The brokers themselves would sustain far more. **Where it is measured:** at the aggregate of
`kubeagents_envelopes_total` across a cluster's brokers, with the API server's
`apiserver_request_duration_seconds` p99 for mutating verbs as the co-observed guard — the target is
"≥ 500/s **while** the API server's mutating p99 stays inside its own SLO", not 500/s in isolation.
A load test that hits 500/s by melting the control plane has failed, not passed. Per-cluster
capacity therefore scales with the control plane, not with the broker count, which is the honest
statement of the limit.

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
- **The watchdogs (`C-JR`, `C-AD`) live in the controller and off the write path (v1).** Both are
  detective controls, so coupling them to execution would let an attacker create a fleet-wide denial
  of service by crashing a detector, and would put a log-sink dependency on the action path. They
  therefore fail **open with respect to writes** and **closed with respect to silence** — a stale
  watchdog pages, and its staleness is on the `Agent` CR status (§1.6, §1.7). They run in `C1`
  rather than as separate Deployments because both are single-writer, per-cluster, leader-elected
  loops over data the controller already watches; a separate pod would add a second identity and a
  second failure domain for no isolation gain, since neither holds a write credential.
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

> **Indexed in [09](09-verification-and-validation.md) §6.** That document is the
> authoritative index of every check in the set: it assigns each of the checks below a stable
> `V-<SUITE>-<nnn>` ID, a verification level (L0 static → L4 soak), a gate class, and the roadmap
> phase by which it must be green. The suites drawn from this section are **V-ISO (CH1–CH9 ≡ V-ISO-001…009), V-CMP, V-NFR, V-OBS**, plus
> **V-BRK** and **V-ADV** for the `C-JR` / `C-AD` watchdog checks added below. This
> section states what to check and why; 09 states how it is run, gated, and proved complete.

The failure-isolation / chaos suite. Scenarios are labelled **CH1–CH9**; **CH1–CH4 are the
scenarios `local-dev/kind/verify-phase6.sh` labels `C1`–`C4`** (renamed here so scenario labels do
not collide with the component IDs of §1). CH5–CH9 are created by the imperative model. Each is a
runnable check; a build is not done until all are green ([README](README.md) building note 8).

**Static placement and shape**

- **Two workloads per agent, and the four names:** for every `Agent` CR the controller reconciles a
  Deployment **`<agent>-gateway`** behind Service **`<agent>`**, bound to the **reader** SA, and a
  Deployment **`<agent>-broker`** behind a Service of the **same name**, bound to the **actor** SA —
  in the correct namespace, with `runAsNonRoot`, seccomp `RuntimeDefault`,
  `allowPrivilegeEscalation: false`, and — on the agent pod — no projected token with write RBAC.
  Assert all four names exactly; three of the four differ only by suffix and a wrong one fails
  silently as a Service with no endpoints.
- **Ports and transport:** the broker serves **HTTP+JSON** on `:8443` at `/v1alpha1/actions` (mTLS
  required, plaintext refused, gRPC absent); the agent serves the mesh on **`:8444`** at
  `/v1alpha1/mesh/{delegate,escalate}`; `:8081/healthz` and `:8081/readyz` are bound to the **pod
  IP** and answer a probe dialled from off-pod (a `127.0.0.1` bind here is a readiness outage that
  only appears under a real kubelet, so assert it with an off-pod request, not a `kubectl exec`).
- **Placement:** Platform Agent + broker in the hub (`kubeagents-system`); each Cluster Admin Agent +
  broker in its cluster; each Developer Team Agent + broker in its namespace; `ActionRecord`s in the
  agent's namespace; one `FleetFreeze` kind per cluster; `C-JR` and `C-AD` running in every
  cluster's controller, with the cloud-log `C-JR` instance in the hub only.
- **Labels — five, not three:** every agent and broker Deployment, pod, and `Service` carries
  `kube-agents/role`, `kube-agents/agent`, `kube-agents/tier`, `kube-agents/scope`, and
  `kube-agents/parent` ([08](08-agent-runtime-and-identity.md) §2.5); the broker Service selects on
  `role=actor` + `agent=<cr-name>`; and the mesh NetworkPolicy selects peers by `tier`/`scope`/
  `parent`. `kubectl get pods -l kube-agents/role=actor -A` must return exactly the brokers.
- **Audit filter reaches every tier:** enumerate every ServiceAccount carrying `kube-agents/role`
  across **all** namespaces and assert each matches the §5.1 name pattern, then assert the audit
  policy's principal filter is the name pattern and **not** a `kubeagents-system` namespace
  selector. A dev-team-tier write must appear in the SLI 1–3 inputs; if it does not, the largest
  tier in the fleet is unmonitored and every dashboard still reads green.

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

**Watchdogs — the two backstops, verified**

Not labelled `CH`: these are not failure-isolation scenarios but the checks that make SLI 2 and the
[03](03-security-model.md) §8.1 residual-risk bound real. They belong to **V-BRK** and **V-ADV**.

- **`C-JR` catches what admission cannot.** With a broker's actor credential, perform three writes
  that pass admission: (a) a `delete` of an in-scope object with no `ActionRecord`; (b) a cloud
  mutation with the actor GSA and no record; (c) a `patch` stamped with the `action-id` of a real
  record whose `spec.targets[]` does **not** contain that object. Assert each is reported within
  `C-JR`'s SLO with the correct metric (`unjournaled_mutation`, `cloud_mutation_unmatched`,
  `action_id_reused`) and that the agent is auto-paused. Check (c) is the one that fails if matching
  is on `action-id` alone — implement it first.
- **`C-JR` catches the inverse.** Fabricate a terminal executed `ActionRecord` for a write that never
  happened; assert `phantom_action` fires.
- **`C-JR` is off the write path.** Stop `C-JR`; assert brokers keep executing and journaling
  normally, and that `jr_stale` fires within 10 min. A green board with a dead reconciler is a fail.
- **`C-AD` trips on the shape, not the count.** Two paired runs against a baselined agent: (a) a
  400-object rollout inside **one** `kube-agents/chain-id` from an authenticated parent — assert **no
  pause**; (b) ~12 self-initiated, heterogeneous actions against never-before-touched targets —
  assert **pause within one evaluation cycle**, with `pauseReason: anomaly/target-novelty`, written
  through `C-BR` to the same `spec.operations.paused` field a human uses. A detector that pauses (a)
  or misses (b) is a rate limiter wearing a different name.
- **`C-AD` cold start and staleness.** A newly created agent is in observe-only for 7 days and emits
  would-have-tripped metrics without pausing; a `C-AD` that has not evaluated in 10 min fires
  `ad_stale` and surfaces on the `Agent` CR status.
- **Auto-brake boundaries.** Exhaust an initiative budget and breach a flap threshold; assert both
  **refuse and escalate** and that `spec.operations.paused` stays `false`. Then trigger repeated
  `forbidden` attempts, a failed rollback, an anomaly trip, and journal loss; assert each **does**
  pause, each with a distinguishable `pauseReason`, and that `resume` is the single recovery path in
  all four cases (§1.5).

**Unopinionated actuation — the demotion, verified**

- Remove the mirror repository and any customer CI/CD entirely. Every flow in §4 still works: agents
  detect, act, verify, journal, report, and undo. Nothing requires a bundled GitOps engine (no
  Config Sync, no Connector) to be installed.
- With a mirror configured, break the Minty path. Actions still execute and journal; the mirror
  write is retried and reported as degraded; **no action is delayed, blocked, or rolled back** by
  the mirror's failure.
