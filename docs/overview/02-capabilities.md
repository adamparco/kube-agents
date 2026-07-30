# Overview 02: Capabilities

**Summarizes:** [`docs/design/02`](../design/02-agent-personas.md) ·
[`03`](../design/03-security-model.md) · [`04`](../design/04-workflow-model.md) ·
[`07`](../design/07-implementation-roadmap.md) · status from
[`docs/build/LEDGER.md`](../build/LEDGER.md)

**Status legend.** ✅ built and merged · 🟡 in progress (Phase 9, current) · ⬜ designed, scheduled
in a later phase. Status is a snapshot as of **2026-07-30**; the ledger is the live record.

---

## 1. The core loop

Every capability below is an instance of one loop:

> **observe → decide → act → verify → report**

- **Observe** — read-only inspection of the agent's own scope, plus push triggers (Kubernetes
  warning events, alerts, webhooks, mesh calls, chat).
- **Decide** — the LLM diagnoses and chooses a concrete change. Its output is a **proposal to the
  broker**, never an API call.
- **Act** — the broker classifies, gates, snapshots, executes.
- **Verify** — re-read the targets against a declared success condition. Failure rolls back
  automatically.
- **Report** — say what was done, what it cost, and how to undo it.

The read-only generation stopped at "propose". The whole conversion is about replacing that terminus
with "fix, verify, report".

---

## 2. Capability areas

### 2.1 Imperative action — the write path

| Capability                                                                                           | Status | Phase                                       |
| ---------------------------------------------------------------------------------------------------- | ------ | ------------------------------------------- |
| Submit a change as an **Action Envelope** to a local broker                                          | 🟡     | 9                                           |
| Derive `(tier, scope)` from the **authenticated caller**, never from the request body                | 🟡     | 9                                           |
| Reject a scope-spoofing envelope                                                                     | 🟡     | 9                                           |
| Deterministic, table-driven **risk classification** into four classes                                | 🟡     | 9                                           |
| `ChangePolicy` resource that may make classification **stricter but provably never looser**          | 🟡     | 9                                           |
| Blast-radius bounds — gate above 50 objects, hard-abort above 100 or half the scope                  | 🟡     | 9                                           |
| Snapshot → server-side apply → verify, with dry-run first and automatic rollback on failure          | 🟡     | 9                                           |
| Journal an `ActionRecord` as a **write-ahead log** that survives a broker crash                      | 🟡     | 9                                           |
| **Actual write authority** — the first agent identity that can mutate a cluster                      | ⬜     | 10 (dev-team), 11 (cluster-admin, platform) |
| Admission enforces the journal reference — an unjournaled write is **rejected**, not merely detected | ⬜     | 10                                          |
| Cloud IAM scoped per actor identity, rather than project-wide                                        | ⬜     | 11                                          |

### 2.2 Reversibility

| Capability                                                                                                                            | Status | Phase |
| ------------------------------------------------------------------------------------------------------------------------------------- | ------ | ----- |
| **Undo-plan generation** for every supported verb, generated **before** execution                                                     | 🟡     | 9     |
| "Cannot generate an undo plan" **reclassifies the action as gated** — the rule that makes reversibility true rather than aspirational | 🟡     | 9     |
| `ActionRecord` carrying pre-state snapshot, applied diff, verification result, and undo plan                                          | 🟡     | 9     |
| Journal retention, TTL, and continuous audit export                                                                                   | 🟡     | 9     |
| `undo <action-id>` as **one command**, restoring prior state exactly                                                                  | ⬜     | 10    |
| Undo works when the originating agent is paused, scaled to zero, or deleted                                                           | ⬜     | 10    |
| Undo replayed as a first-class, re-classified, re-journaled action                                                                    | ⬜     | 10    |

### 2.3 Containment — the properties that never go red

These are the only assertions the inversion does not touch. They have been green since the first
commit and must stay green to the last.

| Capability                                                                                           | Status                                | Phase                |
| ---------------------------------------------------------------------------------------------------- | ------------------------------------- | -------------------- |
| Per-tier scope ceiling — an agent reads and writes only within its own scope                         | ✅                                    | 1–3, held throughout |
| A Developer Team Agent is **provably unable** to affect another namespace                            | ✅                                    | 3                    |
| Cardinality and placement enforced at admission (1 per project / cluster / namespace)                | ✅                                    | 2–3                  |
| The **forbidden set** — no agent may touch any agent's RBAC, IAM, `Agent` CR, or the control plane   | ✅ read-only form; ⬜ imperative form | 1–3 / 11             |
| Pod hardening enforced by admission policy (restricted PSS, read-only root filesystem)               | ✅                                    | 5                    |
| Per-tier default-deny egress, enforced by the dataplane                                              | ✅                                    | 5, 8                 |
| Tenant isolation manifests — resource quotas and namespace default-deny                              | ✅                                    | 8                    |
| Closed human→agent allowlist, with no "allow all users" escape hatch anywhere                        | ✅                                    | 8                    |
| **Cross-object child ⊆ parent** admission webhook — a parent cannot create a child wider than itself | ⬜                                    | 11                   |

### 2.4 Human control — the brake

| Capability                                                                                                                           | Status | Phase |
| ------------------------------------------------------------------------------------------------------------------------------------ | ------ | ----- |
| `pause` a single agent — it stops acting immediately, mid-queue                                                                      | 🟡     | 9     |
| `freeze` the whole fleet, cluster-scoped                                                                                             | 🟡     | 9     |
| `contested` markers on individual objects                                                                                            | 🟡     | 9     |
| **All of the above work through `kubectl` with the inference stack down** — no dependency on the model, the router, or the agent pod | 🟡     | 9     |
| Broker **fails closed** when the journal is unavailable — refuses to act rather than acting untracked                                | 🟡     | 9     |
| Approve / reject a gated action from `kubectl`                                                                                       | ⬜     | 10    |
| Approve / reject from Slack, with the broker re-verifying the clicking human against the roster                                      | ⬜     | 10    |
| A human-undone change is **not** redone by the agent                                                                                 | ⬜     | 13    |
| Automatic pause when an agent's action stream departs from its own baseline                                                          | ⬜     | 14    |

### 2.5 Proactivity

The **entire detection half is already built and working.** It currently ends in a pull request; the
conversion rewires its terminus.

| Capability                                                                                                   | Status | Phase |
| ------------------------------------------------------------------------------------------------------------ | ------ | ----- |
| Kubernetes warning-event watch, filtered and deduplicated, pushed into the agent                             | ✅     | 4     |
| Alert and webhook ingress under one kind-discriminated delivery contract                                     | ✅     | 4     |
| Drift detection against desired state                                                                        | ✅     | 4     |
| Scheduled heartbeat SOPs per tier as the backstop trigger                                                    | ✅     | 4     |
| Detection ends in a **remediation** rather than a proposal                                                   | ⬜     | 13    |
| **Initiative budgets** — per-agent rate limits; exhaustion escalates, never silently drops work              | ⬜     | 13    |
| **Flap detection** — a repeating condition escalates instead of looping                                      | ⬜     | 13    |
| Cooldown after a failed remediation                                                                          | ⬜     | 13    |
| A **self-generated work queue** — improvements found in passing, worked when idle                            | ⬜     | 13    |
| **Coexistence with other controllers** — HPAs, operators, and a customer's GitOps engine are not fought over | ⬜     | 13    |

### 2.6 Coordination between agents

| Capability                                                                                                                                                      | Status | Phase |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ | ----- |
| Escalation round-trip (currently indirect — written to the knowledge base, read back)                                                                           | ✅     | 4     |
| **Direct mesh calls** — `delegate` down, `escalate` up, over mTLS                                                                                               | ⬜     | 12    |
| **Callee re-authorization** — the request is classified and scope-checked by the _callee's_ broker under the _callee's_ identity. Authority is never inherited. | ⬜     | 12    |
| Exactly one hop along the parent/child edge, enforced by NetworkPolicy                                                                                          | ⬜     | 12    |
| Defined behavior for every reply branch: accepted, gated, refused, timeout, paused, unreachable                                                                 | ⬜     | 12    |
| Loop prevention — a cyclic delegation chain terminates                                                                                                          | ⬜     | 12    |

### 2.7 Cascading provisioning

The fleet is not sized in advance. Only the Platform Agent is installed; everything below it is
created at the moment the scope it manages is created, by the agent one level up, **as part of the
same action**.

| Capability                                                                                                           | Status | Phase                       |
| -------------------------------------------------------------------------------------------------------------------- | ------ | --------------------------- |
| Render a child agent's CR and identities (currently as a GitOps bundle for review)                                   | ✅     | 2–3                         |
| Provision a cluster **and** its Cluster Admin Agent as **one** journaled action                                      | ⬜     | 11                          |
| Provision a namespace **and** its Developer Team Agent as **one** journaled action                                   | ⬜     | 11                          |
| The **broker** renders the child's grants, never the agent skill — so an agent cannot mint its own child's authority | ⬜     | 10 (renderer), 11 (cascade) |
| Removing a scope removes its agent, both identities, and its egress policy                                           | ⬜     | 12                          |
| An unagented scope created out of band is detected and **agented** by the tier above                                 | ⬜     | 12                          |

### 2.8 Governance, assurance, and observability

| Capability                                                                                                                               | Status | Phase |
| ---------------------------------------------------------------------------------------------------------------------------------------- | ------ | ----- |
| Full attribution — requester and trace ID flow from chat through to the mutation                                                         | ✅     | 5     |
| Security review gate that **blocks** an unmitigated high/critical finding, with expiring waivers                                         | ✅     | 5     |
| Failure-isolation chaos suite — controller down, tier down, hub down, **no cascade**                                                     | ✅     | 6     |
| Cloud-agnostic seams — matched KCC and Terraform paths, pluggable observability backend                                                  | ✅     | 7     |
| Image provenance — every image published, deployed **by digest**                                                                         | ✅     | 8     |
| The **four SLIs** as continuous audit-log-derived alerts, each proven to fire when tripped                                               | ⬜     | 14    |
| Proactivity metrics — MTTR by severity, share resolved without a human, actions per day                                                  | ⬜     | 14    |
| The security-review suite **re-aimed** at the imperative model, and failing when the classifier floor is lowered or the journal bypassed | ⬜     | 14    |
| Write-behind mirror of executed state to the customer's repo                                                                             | ⬜     | 14    |
| Per-tier inference virtual keys — attributable, independently rate-limited spend                                                         | ⬜     | 14    |

### 2.9 Skills — the per-persona capability surface

Skills are scoped to the persona whose authority they match, and **skills act**. A skill that ends in
a recommendation, a ticket, or a pull request for work the agent was allowed to do is a defect.

| Skill group                                                              |     Platform      |  Cluster Admin   | Developer Team  |
| ------------------------------------------------------------------------ | :---------------: | :--------------: | :-------------: |
| Cluster creation, lifecycle, cost analysis                               |        ✅         |                  |                 |
| Issue resolution, harness self-observability                             |        ✅         |                  |                 |
| Multi-tenancy                                                            | defines the model |    applies it    |                 |
| Compute classes, networking/edge, storage, backup & DR, reliability      |                   |        ✅        |                 |
| App onboarding, manifest generation, productionize, inference quickstart |                   |                  |       ✅        |
| Workload scaling, security, troubleshooting                              |                   |                  |       ✅        |
| Observability, drift detection **and remediation**                       |    fleet view     |   cluster view   |  workload view  |
| Knowledge-base reads                                                     |        ✅         |        ✅        |       ✅        |
| **`apply-change`** — build and submit an Action Envelope                 |        ✅         |        ✅        |       ✅        |
| **`delegate`** — one hop into a direct child's scope                     |  → cluster-admin  | → developer-team |        —        |
| **`escalate`** — one hop to the parent                                   |         —         |    → platform    | → cluster-admin |
| **`provision-*`** — create the child scope and its agent as one action   |        ✅         |        ✅        |        —        |

Four renames the conversion performs: `submit-suggestion` → `apply-change`, `raise-escalation` →
`escalate`, `propose-cluster-admin` → `provision-cluster-admin`, `propose-developer-team` →
`provision-developer-team`. **The skill allocation is also being corrected** — the Developer Team
Agent currently holds none of the seven workload skills it is assigned, while the Platform Agent
carries the whole superset (fixed in Phase 13).

---

## 3. The operating character — what the agent _will_ do

Scope and the broker decide what an agent **may** do. This decides what it **will** do, and it is
normative: an agent that is correct, in scope, and passive is **not** meeting spec.

| Situation                                      | Required behavior                                                                               |
| ---------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| In scope, reversible, below the gate threshold | **Do it, then report.** No pre-announcement, no confirmation question, no proposal.             |
| In scope, but classified `gated`               | Submit it, say plainly what happens on approval and who was asked, **keep working the rest**.   |
| In scope, but a prerequisite is missing        | Create the prerequisite if it is also in scope. Chain the work.                                 |
| Outside scope                                  | **Delegate or escalate immediately.** Do not file a ticket or ask the human to relay it.        |
| In the forbidden set                           | Refuse, state which rule and why, name the human path. **Do not reformulate.**                  |
| Genuinely ambiguous intent                     | Ask **one** specific question with the options, then act. Ambiguity is the only licensed pause. |

**Treated as bugs in a persona review:** saying "you should run `kubectl rollout restart`" when the
agent could have run it; opening an issue or a PR for work inside its own authority; ending a
diagnosis without an action, an escalation, or an explicit statement of what is blocking; "Shall I
proceed?" for anything routine and reversible; deferring to the next heartbeat something it could do
this turn.

---

## 4. Explicit non-capabilities

Stated so nobody builds them by accident:

- **Not a general-purpose chatbot.** The scope is Kubernetes and fleet operations.
- **Not unbounded autonomy.** Autonomy is broad _within_ scope and exactly zero _outside_ it.
- **Not acting without a trace.** Speed is never bought by skipping the journal. An action that
  cannot be recorded and undone is a gated action.
- **Not removing human control.** Humans set intent, define the gated class, supervise outcomes, and
  can pause, freeze, or undo anything at any moment.
- **Not replacing the customer's IaC as the record of intent.** Where a customer keeps desired state
  in a repo, the broker syncs to it.
- **Not multi-cloud today.** Cloud-agnosticism is an architectural constraint now and a supported
  feature later — GKE is the first and only fully supported target.
- **Not running untrusted code.** The gVisor sandbox and untrusted-code execution ship together, and
  neither is in v1.
