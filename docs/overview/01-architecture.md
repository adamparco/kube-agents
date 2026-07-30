# Overview 01: Architecture

**Summarizes:** [`docs/design/05-system-architecture.md`](../design/05-system-architecture.md) ·
[`08-agent-runtime-and-identity.md`](../design/08-agent-runtime-and-identity.md) ·
[`03-security-model.md`](../design/03-security-model.md)

---

## The shape in one picture

```
        ┌──────────────────── HUB CLUSTER (kubeagents-system) ─────────────────────┐
        │  C1 controller + C-AS admission   C15 router (Slack)  C5 inference       │
        │                                                                          │
 human ─┼─chat──▶ C2 Platform Agent ──envelope──▶ C-AB broker ──write──▶ K8s/Cloud │
        │           (reader SA)   ◀──report────   (actor SA)  │                    │
        │  C-UC undo ──replay──────────────────────▶          ▼                    │
        │                                          C-JS journal (ActionRecord CRs) │
        │  C-JR reconciler ◀─audit log + journal──────────────┤  C-AD ──▶ C-BR     │
        └───────┬──────────────────────────────────────────────┬───────────────────┘
                │ C-AM mesh (delegate ▼ / escalate ▲)          │ freeze fan-out, telemetry
    ┌───────────┴────────────────┐              ┌──────────────┴───────────────┐
    │  SPOKE CLUSTER A           │              │  SPOKE CLUSTER B             │
    │  C3 Cluster Admin Agent ──▶ its broker ──▶ cluster API                   │
    │  C4 Dev Team Agent (ns) ──▶ its broker ──▶ namespace objects             │
    │  C-JS journal (local etcd)  C-UC  C-JR  C-AD                             │
    └────────────────────────────┘              └──────────────────────────────┘
```

Two things to take from the diagram:

1. **Every write arrow starts at a broker.** Nothing else in the system holds a write credential.
2. **No write arrow crosses the hub boundary.** A spoke broker holds a local credential, talks to
   its own API server, and journals to its own etcd. A hub outage stops _reasoning_, not _acting_.

---

## 1. The unit of deployment: one `Agent` CR → two workloads

This is the single most important structural fact. Each `Agent` custom resource reconciles into a
**pair** of workloads with **two separate identities in two separate pods**:

| Workload              | Name                                            | Identity      | What runs there                                                       | Authority                                                       |
| --------------------- | ----------------------------------------------- | ------------- | --------------------------------------------------------------------- | --------------------------------------------------------------- |
| **The agent pod**     | Deployment `<agent>-gateway`, Service `<agent>` | **reader** SA | The LLM (Hermes harness) — observe, diagnose, decide, report          | **Read-only, tier-scoped. No write verb, ever.**                |
| **The Action Broker** | Deployment + Service `<agent>-broker`           | **actor** SA  | Deterministic Go — classify, gate, snapshot, execute, verify, journal | Read-write **within the tier's scope**, minus the forbidden set |

**Why two pods and not one pod with two containers:** a sidecar would share a network namespace, a
node, and a lifecycle with the LLM process, putting the actor token's projected volume one
container-escape away from the untrusted-content parser. The separate Deployment keeps the two
identities in two pods with two ServiceAccounts and forces the mTLS hop to be real.

**Why one broker per agent and not a shared one:** a multi-tenant broker would hold the write
authority of every scope it served — the definition of the fleet-wide writer the security model
exists to prevent. Per-agent brokers make the blast radius of a broker compromise **exactly one
scope**. The cost is pod count, which is the cheaper thing to spend.

---

## 2. Major components

Component IDs are stable and cited across the design set. `C1`–`C17` carry over from the read-only
generation; `C-AB`, `C-JS`, `C-JR`, `C-UC`, `C-AM`, `C-BR`, `C-AD`, `C-AS` are created by the
imperative model.

### 2.1 The write path — the components that make an agent safe to act

| ID       | Component              | High-level function                                                                                                                                                                                                                                                                                                                    |
| -------- | ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **C-AB** | **Action Broker**      | **The only writer in the system.** One per `Agent` CR. A small static Go binary — no LLM client, no chat client, no untrusted-content parser, no plugin surface. Runs the eleven-step pipeline (§3) on every mutation. Holds the only write credential in its scope.                                                                   |
| **C-JS** | **Journal store**      | The durable home of `ActionRecord`s — pre-state snapshot, applied diff, verification result, undo plan, attribution. Implemented as namespaced custom resources in the agent's own namespace, continuously exported to the audit sink. Survives the agent and broker pods.                                                             |
| **C-UC** | **Undo controller**    | Executes `undo <action-id>` by replaying a recorded undo plan **through the target agent's broker** — including when the originating agent is paused, scaled to zero, or deleted.                                                                                                                                                      |
| **C-BR** | **Brake surface**      | `pause` (a field on the `Agent` CR), `freeze` (a cluster-scoped `FleetFreeze`), `contested` markers, and their propagation to every broker. Must remain effective with inference, the router, and the hub all unavailable.                                                                                                             |
| **C-AS** | **Admission backstop** | The independent, out-of-broker enforcement: a CEL `ValidatingAdmissionPolicy` (`vap-agent-scope`) plus the controller's validating webhook. Readers write nothing; actors write only their tier template, in scope, and **only carrying a journal reference** — so an unjournaled write is rejected at admission, not merely detected. |

### 2.2 The watchdogs — the components that catch the write path failing

| ID       | Component              | High-level function                                                                                                                                                                                                                                                                                                          |
| -------- | ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **C-JR** | **Journal reconciler** | The completeness backstop. Continuously joins the Kubernetes and Cloud audit logs against exported `ActionRecord`s to find writes with no record, fabricated or reused action IDs, and cloud mutations — which no admission controller sees. This is the only enforcement available for **deletes** and for **cloud calls**. |
| **C-AD** | **Anomaly detector**   | Watches each agent's own action stream (rate, risk-class mix, target novelty) against a per-agent learned baseline and **auto-pauses** the agent on a trip. This is the bound placed on an injected-but-in-scope agent and on standing actor credentials.                                                                    |

### 2.3 The agents themselves

| ID  | Component                | Scope         | Cardinality     | Operates                                                                               |
| --- | ------------------------ | ------------- | --------------- | -------------------------------------------------------------------------------------- |
| C2  | **Platform Agent**       | GCP project   | 1 per project   | The fleet: clusters, cross-cluster policy, tenant RBAC, cost/capacity, compliance      |
| C3  | **Cluster Admin Agent**  | One cluster   | 1 per cluster   | Cluster internals: node pools, add-ons, namespaces, cluster-scoped policy and quotas   |
| C4  | **Developer Team Agent** | One namespace | 1 per namespace | Everything inside its namespace: workloads, config, scaling, rollouts, troubleshooting |

All three are the same _kind_ of thing — a persona (`SOUL.md`), a config, a scoped skill set,
governance SOPs, and a scope-appropriate identity pair — differing in **the scope they act on**,
never in **whether they may act**. A new persona is defined by changing the fills, not the frame.

**SRE is not a fourth agent.** It is a class of critical user journeys — reliability, incident
response, capacity, observability — served by whichever persona owns the scope it applies to.

### 2.4 Control plane, coordination, and shared services

| ID       | Component                    | High-level function                                                                                                                                                                                                                                                                                                                                                                                                |
| -------- | ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **C1**   | **kube-agents controller**   | Reconciles each `Agent` CR into the two workloads, the mesh certificate, and the NetworkPolicies. Owns cardinality, placement, and child ⊆ parent admission; stamps the five identity labels; owns lifecycle, brake fan-out, and journal export. **Mints no identity** — it references pre-created RBAC, never creates it.                                                                                         |
| **C-AM** | **Agent mesh**               | The direct agent-to-agent call path: **delegation** (parent → child) and **escalation** (child → parent), over mTLS with `TokenReview`. Exactly one hop along the parent/child edge — no siblings, no grandparents, no calls outside the lineage. Enforced by NetworkPolicy, so the topology is a network property.                                                                                                |
| **C15**  | **ChatOps gateway & router** | The fleet's single human entrypoint, Slack-first. Holds the **one** Slack app and its **single** Socket Mode connection, normalizes each event into one internal message, enforces the target agent's allowlist **before** dispatch, and routes to the addressed agent. Also delivers gate prompts and action reports back. Google Chat is the opt-in second ingress on the same normalizer and the same dispatch. |
| **C16**  | **Kubernetes event watcher** | Streams, filters, and deduplicates warning events from the API server into the agent's session seam — the primary **push trigger** for the proactive loop. Already built and working.                                                                                                                                                                                                                              |
| **C17**  | **Event ingress relay**      | Delivers non-chat machine push — Cloud Monitoring/Alertmanager alerts, GitHub webhooks, and mesh escalations — to the same in-pod session seam under one kind-discriminated contract.                                                                                                                                                                                                                              |
| **C5**   | **Inference service**        | Unified Completions API for all agents, with per-tier virtual keys for budget, rate-limit, and log isolation. LiteLLM (hosted) or vLLM (local GPU).                                                                                                                                                                                                                                                                |
| **C9**   | **OKF knowledge base**       | Durable curated knowledge — SOPs, blueprints, runbooks — as markdown in git. Purely a knowledge layer; **no longer a coordination channel**.                                                                                                                                                                                                                                                                       |
| **C11**  | **Session store**            | Per-user runtime session state and the agent's self-generated work queue.                                                                                                                                                                                                                                                                                                                                          |
| **C12**  | **Observability pipeline**   | Traces, metrics, logs, and attribution — carrying the trace ID from chat, through the envelope, into the `ActionRecord`.                                                                                                                                                                                                                                                                                           |

### 2.5 Demoted, optional, or deliberately absent

| ID  | Component                         | Status in the imperative model                                                                                                                                                                       |
| --- | --------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| C7  | **Customer CI/CD pipeline**       | **No longer in the critical path at all.** Was the privileged writer; now an optional downstream consumer of the write-behind mirror. The system must work with it absent, and is verified that way. |
| C13 | **Mirror repository**             | Was "the GitOps repository, source of truth for all mutation". Now a write-behind mirror of already-executed state. Compromising it cannot cause a cluster change.                                   |
| C6  | **GitHub token broker (Minty)**   | Demoted — used only for the optional mirror and knowledge-base writes. No longer on any control path.                                                                                                |
| C14 | **Authorization gateway**         | Deferred for v1 (trusted-human access plus the scope ceiling bounds it). If adopted it lands _inside_ the broker, not as a separate service.                                                         |
| C10 | **Semantic recall (mem0/Qdrant)** | Deferred post-v1. The journal and the knowledge base cover recall adequately.                                                                                                                        |

> **The one component that is gone:** nothing in this inventory is "a privileged writer that acts on
> reviewed state". If a design question resolves to "the pipeline will apply it", the answer is
> wrong — the broker applies it.

---

## 3. The write pipeline — eleven steps, no LLM

Every mutation in the system is this path. The agent can neither skip it nor influence it.

| Step                | What happens                                                                                                                                                                                |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1 **Authenticate**  | mTLS peer cert **and** `TokenReview` of the projected reader-SA token. **`(tier, scope)` comes from the authenticated caller — never from the envelope body.**                              |
| 2 **Validate**      | Envelope schema and target well-formedness; unknown fields rejected                                                                                                                         |
| 3 **Resolve scope** | Every target resolved to a concrete object or cloud path and checked against the caller's scope. Label selectors are expanded here. **One out-of-scope target rejects the whole envelope.** |
| 4 **Classify risk** | Deterministic, table-driven classifier. Floored by a code constant; a `ChangePolicy` may only ever make it **stricter**. Includes the blast-radius rules.                                   |
| 5 **Check brake**   | Read `paused`, `FleetFreeze`, initiative budgets, flap cooldown, and `contested` markers. **Fail-closed.**                                                                                  |
| 6 **Undo plan**     | Per-verb undo generation: create → delete; update → restore prior object; scale → prior replicas; delete → recreate from snapshot. **No plan ⇒ reclassify as gated.**                       |
| 7 **Gate**          | `gated` ⇒ park the record as `PendingApproval`, notify the roster, start the TTL. Nothing executes.                                                                                         |
| 8 **Snapshot**      | Read every target at its current version and store the prior state                                                                                                                          |
| 9 **Execute**       | Server-side apply with a stable field manager, dry-run first, the journal reference stamped on every object, cloud APIs via the actor identity                                              |
| 10 **Verify**       | Re-read targets and evaluate the envelope's declared success condition. **Failure ⇒ automatic rollback** via the step-6 plan.                                                               |
| 11 **Journal**      | Transition the record to a terminal phase with the applied diff, verification result, and validated undo plan; emit the audit event; **only then** return success                           |

**Crash safety — the journal is a write-ahead log.** The `ActionRecord` is created **before** step 9,
already carrying the snapshot and the undo plan, and only _transitioned_ in step 11. A broker killed
between execute and journal leaves a discoverable record, not an invisible mutation.

**Why the agent cannot bypass it.** Three independent layers, none of which the model participates
in: the reader SA holds no write verb anywhere; the NetworkPolicy allows egress to its own broker
but not to the API server's write paths; and admission rejects any write from a reader identity, and
any write from an actor identity that lacks a journal reference.

---

## 4. Risk classification and the gated class

Four classes, decided **in code, never by the model**:

| Class         | Meaning                                                              | What happens                         |
| ------------- | -------------------------------------------------------------------- | ------------------------------------ |
| **routine**   | In scope, reversible, small blast radius                             | Executes immediately. No human.      |
| **elevated**  | In scope and reversible, but worth more scrutiny in the record       | Executes, with heightened journaling |
| **gated**     | Irreversible, high blast radius, security-loosening, or no undo plan | Parks for a human. Nothing executes. |
| **forbidden** | In the forbidden set — no tier, no gate, no exception                | Refused outright                     |

The classifier's inputs are all deterministic: scope, forbidden-set match, reversibility,
destructiveness, security direction, blast radius, environment, traffic impact, object override, and
novelty. **The agent does not decide its own risk level and must never claim to.**

**The forbidden set** — the things no agent may do at any tier, with no gate available: modify any
agent's RBAC or cloud IAM, modify any `Agent` CR, touch the kube-agents control plane or the
admission policy, or tamper with the journal. This is what makes "no self-escalation" a structural
property rather than a promise.

**Blast radius bounds**, evaluated after selector expansion: an envelope may carry at most 50
literal operations; more than 50 resolved objects ⇒ **gated**; more than 100 objects, or more than
half the agent's own scope ⇒ **hard abort**, no gate offered.

---

## 5. Topology: hub-and-spoke, and the property that justifies it

A **hub cluster** runs the controller, the Platform Agent, and the shared services (inference,
ChatOps router, token broker, observability). Each **spoke cluster** runs a Cluster Admin Agent and
hosts Developer Team Agents in their namespaces — **each with its own broker and its own journal in
its own cluster**.

**The load-bearing property: brokers do not depend on the hub to execute.** If the broker's
authority were centralized in the hub, a hub outage would block all remediation everywhere at
exactly the moment remediation matters most — and it would reintroduce the fleet-wide writer the
security model forbids. What remains hub-dependent is **reasoning, not acting**: with the hub down, a
spoke agent cannot call inference to diagnose something new, but its broker still executes queued
actions, still journals them, still honors the brake, and `undo` still works locally.

Two alternatives were considered and rejected: operator-per-cluster with no hub (duplicates shared
services, complicates fleet governance — available for small single-cluster installs), and a central
broker service brokering for the whole fleet (cross-cluster write credentials, a fleet-wide writer,
and the hub on the critical path of every remediation — three independent disqualifications).

---

## 6. Continuous assurance: four SLIs

The design's claims are measured in production, not proven once in a test:

1. **Zero cross-scope escapes** — alert on any agent read, write, or authorization-allow outside its
   tier scope.
2. **Zero unjournaled mutations** — alert on any cluster or cloud write by an agent identity with no
   matching `ActionRecord`. The concern is no longer _that_ an agent wrote, but that it wrote
   **outside the broker**.
3. **Zero self-escalations** — alert on any agent action that modifies its own, a sibling's, or a
   parent's RBAC, IAM, `Agent` CR, or the control plane.
4. **Undo health** — the fraction of records carrying a valid undo plan (target: 100% of non-gated
   actions) and the success rate of executed undos (target: no silent failures).

Proactivity is measured too, with a counterweight: mean time to remediate, share of issues resolved
without a human, actions per agent per day — bounded by flap and revert counters, so "relentless" is
never achieved by thrashing.
