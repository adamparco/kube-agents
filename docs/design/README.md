# kube-agents — Design

**Status:** Design complete. The documents in this directory are the authoritative, build-ready
specification of kube-agents' end-state architecture — sufficient for an engineer or an agentic
coding harness to build the product end-to-end.

kube-agents replaces the _human interface_ to Kubernetes and GKE — `kubectl`, `gcloud`, and the
Cloud Console — with a tier of **imperative AI agents that operate infrastructure directly**. They
hold real, scope-bounded write authority; they watch their scope continuously; and when they find
something wrong they **fix it** and tell you what they did. Three personas map onto the Kubernetes
containment hierarchy:

- **Platform Agent** — one per project.
- **Cluster Admin Agent** — one per cluster.
- **Developer Team Agent** — one per namespace.

They differ in the _scope_ they act on, not in whether they may act. Every action is executed
through a deterministic **Action Broker** that classifies risk, snapshots prior state, executes,
verifies the outcome, and journals an **undo plan** — so "the agent did it" and "we can take it
back" are the same sentence. Human approval is reserved for the small class of changes that are
**irreversible or high-blast-radius**; everything else happens without asking.

> **These docs describe the end state, not current code.** The current implementation is the
> _previous_ generation of this system: read-only agents that opened GitOps pull requests and let a
> CI/CD pipeline apply them. That model is superseded. Where a doc leads the implementation it says
> so and flags the delta; the design is the source of truth the code converges toward.
> [07](07-implementation-roadmap.md) sequences the conversion from today's read-only codebase to
> the imperative end state.

---

## Core invariants

Load-bearing rules that hold across every persona, phase, and document. A change that violates one
is wrong even if it compiles and passes tests:

1. **Agents act.** Each agent holds **scoped write authority** over its own tier and exercises it —
   through its broker (invariant 3), synchronously, as part of answering the request. Filing a
   proposal, opening a ticket, or asking a human to run a command — when the action is inside the
   agent's scope and below the gate threshold — is a **defect**, not caution.
2. **Scope is absolute.** An agent may read and write **only** within its own project / cluster /
   namespace, and may **never** widen its own authority. Enforced by per-tier identity, admission
   policy, and the broker — never by agent goodwill. This is the one invariant that admits no
   exception.
3. **Every mutation is brokered, journaled, and reversible.** No agent touches a cluster or cloud
   API directly. All writes flow through the **Action Broker** (deterministic code, outside the LLM
   loop), which captures prior state, executes, verifies, and records an `ActionRecord` carrying an
   **undo plan**. An action with no recorded undo path is a gated action by definition.
4. **Irreversible or high-blast-radius actions stop for a human.** The gated class is small,
   explicit, and evaluated **in code, never by the model**: data destruction, identity/IAM changes,
   fleet-wide and cross-tenant blast radius, production traffic shifts, and anything the broker
   cannot generate an undo plan for. Everything outside that list is autonomous by default.
5. **Agents collaborate directly.** Tiers call each other — a parent **delegates** down, a child
   **escalates** up. The callee always re-authorizes the request **in its own scope, under its own
   gates**, and never inherits the caller's authority.
6. **Humans hold the brake, not the steering wheel.** Any authorized human can `pause` an agent,
   `freeze` the fleet, or `undo` any action, instantly and without a merge. Oversight is exercised
   **after** and **over** autonomous action, not as a precondition for it.

The security model (03) makes these enforceable; the roadmap (07) proves them with negative tests.

### What changed from the previous generation, and why it still holds together

This design set was previously built around read-only agents, GitOps-proposal-only mutation, and no
direct agent-to-agent calls. Those three rules are **inverted**. The safety properties they were
protecting are **not** discarded — they are re-implemented on mechanisms that survive an agent that
actually acts:

| Previous rule                          | Replaced by                                                             | The property it protected, preserved by                                                       |
| -------------------------------------- | ----------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| Agents are read-only                   | Scoped read-write authority per tier                                    | Blast radius → **scope ceiling** + the forbidden-verb set (no self-escalation), invariant 2   |
| All mutation flows through a merged PR | Direct execution via the **Action Broker**                              | Reviewability → **`ActionRecord` journal**; revertibility → **undo plan**; invariant 3        |
| Human approval before every change     | Human approval for the **gated class** only                             | Human control → **risk classifier in code** + pause/freeze/undo (invariants 4 and 6)          |
| Agents never call each other           | Direct delegation and escalation                                        | Escalation-by-proxy → **callee re-authorizes in its own scope**, invariant 5                  |
| The CI/CD pipeline is the sole writer  | The broker is the sole writer; CI/CD becomes optional write-behind sync | "One auditable choke point for writes" — same property, moved in-cluster and made synchronous |

The pattern to notice: the old design put the safety check **before** the change and made it a
human's job. The new design puts an equally strict check **around** the change and makes it code's
job, so the human is freed to supervise outcomes instead of gating keystrokes.

---

## Terminology

The vocabulary changed with the model. Use these terms; do not reintroduce the old ones.

| Use this                                     | Not this                            | Meaning                                                                           |
| -------------------------------------------- | ----------------------------------- | --------------------------------------------------------------------------------- |
| **Action Broker**                            | actuation pipeline, CI/CD applier   | The in-cluster component that executes every agent mutation (05 C-AB, 06 §4)      |
| **Reader / actor identity**                  | the agent's ServiceAccount          | Read-only SA on the agent pod / scoped-write SA on its broker (03 §3.1)           |
| **Action Envelope**                          | pull request, suggestion            | The request an agent submits to the broker (06 §4.1)                              |
| **`ActionRecord`**                           | PR, commit, change artifact         | The durable journal entry + undo plan for one executed action (06 §4.3)           |
| **Risk class**                               | approval policy                     | `routine` / `elevated` / `gated` / `forbidden`, decided in code (03 §5, 06 §4.2)  |
| **Risk gate**                                | review gate, mandatory gate         | The human confirmation required for `gated` actions only (04 §3)                  |
| **Scope ceiling**                            | read-only ceiling                   | The absolute boundary of a tier's authority (03 §3)                               |
| **observe → decide → act → verify → report** | propose → review → reconcile        | The core agent loop (04 §1)                                                       |
| **Delegation / escalation**                  | indirect coordination, shared state | Direct, re-authorized agent-to-agent calls (02 §2.3, 06 §7)                       |
| **Initiative budget**                        | —                                   | The rate/flap controls that keep "relentless" from becoming "thrashing" (04 §4.2) |
| **Pause / freeze / undo**                    | break-glass                         | The human brake (03 §6, 06 §4.4)                                                  |
| **Write-behind IaC sync**                    | GitOps loop, propose-apply          | Optional mirroring of executed state back to the customer's repo (04 §6)          |

---

## The design set

Two tiers, meant to be read in order **01 → 08**:

- **Foundational (north star) — 01–04:** _what_ we are building and _why_.
- **Buildable (bridging) — 05–08:** _how_ it is assembled.

| #   | Document                                                     | Covers                                                                                                                                                                                                 |
| --- | ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 01  | [Vision & scope](01-vision-scope.md)                         | The "agents operate the fleet, humans set intent and supervise" thesis, in/out of scope, success criteria and the continuous SLIs                                                                      |
| 02  | [Agent personas](02-agent-personas.md)                       | The three-persona roster, their authority and limits, the high-agency operating character, direct delegation/escalation, cascading provisioning, ChatOps addressing                                    |
| 03  | [Security & trust model](03-security-model.md)               | Scoped write identity, the forbidden set, the Action Broker as the sole writer, risk classification, undo/pause/freeze, AI-agent threats, continuous assurance                                         |
| 04  | [Workflow model](04-workflow-model.md)                       | The observe → decide → act → verify → report loop, autonomy by default and the gated exception class, relentless proactivity with initiative budgets, the recovery ladder, failure isolation           |
| 05  | [System architecture](05-system-architecture.md)             | Component inventory (Action Broker, journal store, undo controller, agent mesh, ChatOps router), hub-and-spoke topology, data flows, shared services, scale/NFR targets                                |
| 06  | [API & data contracts](06-api-and-data-contracts.md)         | The `Agent` CRD, the per-tier read-write identity contract, the Action Envelope / `ActionRecord` / risk-class / undo contracts, the agent-mesh contract, OKF schema, ChatOps routing, MCP tool surface |
| 07  | [Implementation roadmap](07-implementation-roadmap.md)       | The phased conversion from today's read-only codebase to the imperative end state, per-phase acceptance criteria, the verification loop, the definition of done, and risks                             |
| 08  | [Agent runtime & identity](08-agent-runtime-and-identity.md) | The kube-agents controller reconciling each `Agent` CR (Hermes harness) into an isolated pod with a per-pod scoped read-write Workload-Identity SA, the broker sidecar seam, what is deferred and why  |

Each document opens with a **TL;DR** and closes with a **Verification** section of concrete,
mostly-runnable checks; most also carry **Goals / Non-goals**. The three decisive verification
suites are named in step 8 below.

---

## Building from these docs (for an engineer or agentic coding harness)

To build kube-agents end-to-end from this design set:

1. **Read 01 → 08 in order.** 01–04 give the intent and the invariants above; 05 the system to
   assemble; 06 the exact contracts; 07 the build sequence; 08 the runtime and identity model.
2. **Build the broker before the authority.** The single most dangerous ordering mistake is to grant
   agents write RBAC before the Action Broker, risk classifier, journal, and undo path exist. 07
   sequences this deliberately; do not reorder it.
3. **Build by phase, verify, iterate.** Follow [07](07-implementation-roadmap.md) §2. After each
   phase, run its **acceptance criteria** _and_ the **Verification** checks of every spec the phase
   touched (02 §10, 03 §11, 04 §9, 05 §8, 06 §10, 08 §7). Do not advance a phase — or open the final
   PR — until its checks pass. The verification loop is defined in
   [07](07-implementation-roadmap.md) §5.
4. **Decisions are already made — don't re-litigate.** Every decision is stated in its home spec
   (01–06 and 08). If you hit something genuinely unspecified, pick the simplest option consistent
   with the invariants, implement it, and flag it in your PR.
5. **Honor the invariants even when they contradict current code.** The code is mid-conversion and
   still enforces the _previous_ generation's rules (read-only RBAC, a deny-all-writes admission
   policy, a `submit-suggestion` skill). Those are the things being replaced, not precedents to
   follow.
6. **Keep the model out of the trust path.** The LLM decides _what_ to do; deterministic code
   decides whether it _may_. Risk classification, scope checks, gating, snapshotting, and journaling
   all live in the broker, never in a prompt, a skill, or a `SOUL.md` instruction. A prompt-injected
   agent must be unable to do anything a healthy one could not.
7. **Ground new code on existing patterns — don't invent structure.** New personas follow the
   Platform Agent's shape (`agents/platform/`: `SOUL.md` + `config.yaml` + `skills/` + governance
   SOPs), packaged as an `Agent` CR running the **Hermes** harness and reconciled by the
   **kube-agents controller** (the extended `k8s-operator/`). Per-agent identity is pre-created
   KSA / RBAC / Workload-Identity manifests the controller **references**, never mints.
8. **Verification checks are load-bearing, not extras.** The three decisive suites are the
   **containment negative tests** (03 §11 — scope ceiling, no self-escalation, no cross-tenant
   write), the **reversibility tests** (03 §11 — every action undoable, undo actually restores), and
   the **failure-isolation chaos tests** (05 §8). A build is not done until all three are green.
9. **Definition of done** is the product-level acceptance in [07](07-implementation-roadmap.md) §3,
   which makes [01](01-vision-scope.md) §7 concrete.
10. **Produce changes the way the repo requires** — see `AGENTS.md` (Conventional Commits, PR
    template, format before commit, stage only targeted files).

**What these docs intentionally leave to the builder:** field-by-field API schemas beyond the
snippets in [06](06-api-and-data-contracts.md), per-skill implementation logic, and account-specific
values (project IDs, secrets). Derive these from the contracts in 06 and the repo patterns below.

---

## Key references (repo)

- **Platform Agent persona:** `agents/platform/` — `SOUL.md`, `config.yaml`, `skills/`, `governance/`
- **kube-agents controller** (the agent runtime; generalized per tier): `k8s-operator/`
- **Agent harness — Hermes:** [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
- **Per-pod runtime model — Scion:** [GoogleCloudPlatform/scion](https://github.com/GoogleCloudPlatform/scion)
- **Security-review skills:** `.agents/skills/review-security-k8s-*` — retained as the continuous
  posture audit; their "agents must be read-only" assertions are re-aimed at the scope ceiling and
  the forbidden set (03 §7)
- **Glossary:** `docs/glossary.md`
- **Detailed feature designs:** `docs/designs/` (e.g. `audit-logging-user-attribution.md`)
- **Contribution mechanics:** `AGENTS.md`
- **Install prerequisites:** `INSTALL.md`
