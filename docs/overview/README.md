# kube-agents — High-Level Overview

**Audience:** anyone who needs to understand _what this project is building_ without reading the
2,900-line contract spec. Engineers, reviewers, stakeholders, and new contributors.

**Status of this document set:** descriptive, not normative. It summarizes and points at the
authoritative sources; where it disagrees with them, they win.

| This set is a summary of…                                  | Which is authoritative for…                                                         |
| ---------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| [`docs/design/01`–`09`](../design/)                        | _What_ to build — vision, personas, security, architecture, contracts, verification |
| [`docs/build/LEDGER.md`](../build/LEDGER.md)               | _Where the build is_ — phase status, evidence, deferrals, decisions                 |
| [`docs/design/07`](../design/07-implementation-roadmap.md) | _The build sequence_ — phases, acceptance criteria, ordering constraints            |

---

## The one-paragraph version

kube-agents replaces the human interface to Kubernetes — `kubectl`, `gcloud`, the Cloud Console —
with **AI agents that operate the infrastructure directly**. Three personas map onto the Kubernetes
containment hierarchy: a **Platform Agent** per project, a **Cluster Admin Agent** per cluster, a
**Developer Team Agent** per namespace. They hold real, scope-bounded write authority; they watch
their scope continuously; and when they find something wrong they **fix it** and report what they
did. No agent pod ever holds a write credential: every mutation is submitted as an **Action
Envelope** to a per-agent **Action Broker** — deterministic Go, no LLM in the loop — which
classifies risk, snapshots prior state, executes, verifies the outcome, and journals an
**`ActionRecord`** carrying an **undo plan**. Human approval is reserved for a small, explicit class
of changes that are irreversible or high-blast-radius, decided **in code, never by the model**.
Humans hold the brake: `pause`, `freeze`, `undo` — instant, no merge, and effective with the
inference stack down.

## The one-sentence version of what makes it different

> "The agent did it" and "we can take it back" are the same sentence.

---

## The six core invariants

Every document in this set, and every line of code in the repo, is downstream of these
([`docs/design/README.md`](../design/README.md#core-invariants)):

1. **Agents act.** Filing a proposal for work inside scope and below the gate threshold is a
   **defect**, not caution.
2. **Scope is absolute.** An agent reads and writes only within its own project / cluster /
   namespace, and may never widen its own authority. The one invariant that admits no exception.
3. **Every mutation is brokered, journaled, and reversible.** No agent touches an API directly. An
   action with no recorded undo path is a gated action by definition.
4. **Irreversible or high-blast-radius actions stop for a human.** The gated class is small,
   explicit, and evaluated in code.
5. **Agents collaborate directly.** Parent delegates down, child escalates up, and the **callee
   always re-authorizes in its own scope** — authority is never inherited.
6. **Humans hold the brake, not the steering wheel.** Oversight is exercised _after_ and _over_
   autonomous action, not as a precondition for it.

---

## The documents in this set

| #   | Document                                     | Read it for                                                                                                      |
| --- | -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| 01  | [Architecture](01-architecture.md)           | The major components, what each one does, how they are deployed, and the write path that connects them           |
| 02  | [Capabilities](02-capabilities.md)           | What the system can do, by area, with built / in-progress / designed status against each                         |
| 03  | [User interactions](03-user-interactions.md) | Who talks to the system, through which surfaces, and what the end-to-end journeys look like                      |
| 04  | [Phases](04-phases.md)                       | The whole build, phase 0 through 15 — what each phase delivers, why it lands where it does, and where we are now |
| 05  | [Delta vs. upstream](05-upstream-delta.md)   | Capability-by-capability difference between this fork and `gke-labs/kube-agents`                                 |

---

## Where the build is, in one table

_Snapshot as of 2026-07-30. The live number is always
[`docs/build/LEDGER.md`](../build/LEDGER.md) §Status._

| Generation                          | Phases | Status                                                                                                                                                   |
| ----------------------------------- | ------ | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Read-only generation** (complete) | 0–7    | ✅ All merged. Agents observe, diagnose, and open GitOps pull requests.                                                                                  |
| **Containment** (complete)          | 8      | ✅ Merged. Closes the human→agent boundary, enforces egress, makes a multi-tier install real.                                                            |
| **The safety machinery** (current)  | 9      | 🟡 In progress — the broker, envelope, classifier, journal, undo plan, and brake, built and exercised end-to-end **with zero write authority anywhere**. |
| **The imperative conversion**       | 10–15  | ⬜ Not started. First write authority (10), full authority (11), the mesh (12), proactivity (13), continuous assurance (14), reach and scale (15).       |

**8 of 15 phases done.** The single most important rule in the roadmap is the ordering: the safety
machinery is built **before** the authority. An agent with write RBAC and no journal is strictly
worse than either the system we have or the one we want.
