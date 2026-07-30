# Overview 05: Delta vs. Upstream `gke-labs/kube-agents`

**Compares:** this repository (`adamparco/kube-agents`, branch `main`) against
[`gke-labs/kube-agents`](https://github.com/gke-labs/kube-agents) `main`, as of **2026-07-30**.

Measured, not asserted: **126 commits ahead**, **987 files changed**, **+180,414 / −22,133 lines**.

---

## 1. The one-line difference

**Upstream proposes. This fork acts.**

Upstream's agents are read-only by design and their only mutation path is a GitOps pull request that
a human reviews and a customer pipeline applies. This fork keeps that containment guarantee and adds
a way to _use_ it: a deterministic **Action Broker** that holds the write credential the agent never
sees, and a journal that makes every write attributable and reversible.

Everything below follows from that one change.

|                             | Upstream                                     | This fork                                                                       |
| --------------------------- | -------------------------------------------- | ------------------------------------------------------------------------------- |
| Mutation path               | Pull request → human review → customer CI/CD | **Action Envelope → Action Broker → API, journaled**                            |
| Time to remediate           | Merge latency (minutes to days)              | Seconds, unattended, for the routine class                                      |
| Who holds write credentials | Nobody in-cluster — the CI pipeline does     | A **separate broker pod** with a separate ServiceAccount; never the agent pod   |
| Human role                  | Reviewer of every change, **in the loop**    | Holder of the brake, **over the loop** — plus approver of the small gated class |
| Undo                        | `git revert` + a pipeline run                | `ActionRecord` undo plan, executed by a controller                              |
| Blast-radius control        | Branch protection + CODEOWNERS               | Broker-enforced caps, risk classification, admission policy                     |

Upstream's own posture line — _"GitOps-only mutations — infrastructure changes are proposed as pull
requests for human review"_ — is precisely the line this fork replaces.

---

## 2. Persona model

|                       | Upstream                                                                                     | This fork                                                                  |
| --------------------- | -------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| Agents                | **Chat Agent**, **Platform Agent**, **Cluster Agent**                                        | **Platform**, **Cluster Admin**, **Developer Team**                        |
| Basis of the split    | Function (front door / fleet custodian / single-cluster SRE)                                 | **The Kubernetes containment hierarchy** — project / cluster / namespace   |
| Deployment            | Co-located in a **single operator-deployed pod**                                             | **One pod per agent**, one agent per scope, each with its own identity     |
| Namespace-level agent | none                                                                                         | **Developer Team Agent**, one per namespace                                |
| Front door            | The Chat Agent — an LLM that receives every message and delegates over a shared kanban board | **`kage-router`** — deterministic Go. No model in the routing path         |
| Coordination          | Shared kanban board + `kanban_notify_propagate.py`                                           | Direct **agent mesh** calls with callee re-authorization (Phase 12)        |
| SRE                   | The Cluster Agent's job description                                                          | A **cross-cutting class of journeys** every tier performs in its own scope |

The Chat Agent is **gone**, not renamed. Routing became a deterministic component because
authorization must not be a model's judgement call: this fork authorizes _before_ dispatch, on a
platform-qualified principal, with natural language as the **last** resolution mode after slash
commands, `@handle`, thread affinity, and channel binding.

The tier split is what makes the containment claim checkable. "Cluster Agent" describes what an
agent _does_; "one agent per namespace, and it cannot reach outside it" is a property a test can
fail on.

---

## 3. What exists in this fork and does not exist upstream

### 3.1 Custom resources

| CRD              | Upstream | Fork | Function                                                                                                             |
| ---------------- | -------- | ---- | -------------------------------------------------------------------------------------------------------------------- |
| `PlatformAgent`  | ✅       | —    | Superseded                                                                                                           |
| `Agent`          | —        | ✅   | Generic, tier-parameterized. One CR renders **two** workloads and **two** identities                                 |
| `ActionRecord`   | —        | ✅   | The journal entry. Attribution, classification, pre-state snapshot, applied diff, verification result, **undo plan** |
| `ApprovalRoster` | —        | ✅   | Who may approve a gated action, and the approval TTL                                                                 |
| `ChangePolicy`   | —        | ✅   | Per-scope risk-class overrides that may **only tighten**, never relax                                                |
| `FleetFreeze`    | —        | ✅   | Fleet-wide stop                                                                                                      |
| `UndoRequest`    | —        | ✅   | A human-triggered reversal, executed by a controller                                                                 |

### 3.2 Binaries and packages

|             | Upstream                           | Fork                                                                                                                        |
| ----------- | ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `cmd/`      | `main.go`, `k8s-event-watcher`     | `main.go`, `k8s-event-watcher`, **`broker`**, **`router`**, **`eventingress`**                                              |
| `internal/` | `controller`, `webhook`, `testing` | those, plus **`broker`**, **`journal`**, **`scope`**, **`router`**, **`agentindex`**, **`agentlabels`**, **`eventingress`** |

### 3.3 Whole subsystems

- **The Action Broker** (`internal/broker`, `cmd/broker`) — deterministic Go, no LLM. An eleven-step
  pipeline: authenticate → validate → resolve scope → classify risk → check the brake → generate an
  undo plan → gate → snapshot → execute → verify → journal. `(tier, scope)` is derived from the
  **authenticated caller**, never from the envelope body, so an agent cannot ask for someone else's
  scope by writing it down.
- **The journal** (`internal/journal`) — a write-ahead log. The `ActionRecord` is created _before_
  execution, so a broker crash leaves a discoverable record rather than a silent mutation.
- **The scope engine** (`internal/scope`) — the containment boundary as testable code rather than as
  an RBAC accident.
- **The brake** — `pause`, `freeze`, `contested`, `undo`. Reachable through `kubectl` with the
  inference stack and chat both down.
- **`kage-router`** (`internal/router`, `cmd/router`) — five addressing modes in strict precedence,
  authorization before dispatch, one fleet-level Slack app.
- **Event ingress** (`cmd/eventingress`) — the cloud leg of the detection path.
- **The design set** (`docs/design/01`–`09`) — nine documents, including a formal conformance spec
  with stable check IDs.
- **The build harness** (`.claude/harness/`, `docs/build/`) — a spec-driven autonomous build loop
  with a ledger, an invariants gate, and a lessons file.
- **The verification harness** (`verification/`, `dev/L0-CHAIN.txt`, `dev/L2-CHAIN.txt`) — levels
  L0–L4, a phase ratchet, and a "deferred, never faked" rule.
- **The security review gate** (`scripts/review-gate/`) — a scorer whose verdict is the authoritative
  merge decision; any unmitigated high or critical exits non-zero.

---

## 4. What upstream has that this fork keeps, changes, or drops

| Upstream capability                                     | Here                                                                                                                                                                                                                  |
| ------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Hermes agent runtime, MCP servers, GKE hosted MCP       | **Kept unchanged**                                                                                                                                                                                                    |
| `SOUL.md` personas, governance SOPs, `SKILL.md` bundles | **Kept**, retiered and rewritten. Persona rewrite to the imperative operating character is Phase 13                                                                                                                   |
| Cron watchdogs (`cron/jobs.json`)                       | **Kept and generalized** — upstream has one (platform, plus a chat default); this fork has one per tier                                                                                                               |
| Read-only Kubernetes RBAC that cannot read Secrets      | **Kept for the agent pod, permanently.** The reader identity never gains a write verb in any phase. Write authority lives on a _different_ ServiceAccount in a _different_ pod                                        |
| Envoy credential-proxy sidecar (credential isolation)   | **Superseded by a stronger form.** The agent pod holds no credential to proxy: the broker is a separate pod with its own identity, so the isolation is a process and RBAC boundary rather than a network interception |
| gVisor / GKE Sandbox RuntimeClass                       | **Kept**, plus restricted PSS, read-only root filesystem, and enforced per-tier NetworkPolicies proven by the dataplane                                                                                               |
| GitOps-PR-only mutation                                 | **Replaced.** Retained in a different role as **write-behind IaC sync** (Phase 14) — the repo is updated _after_ the fact so the customer's IaC does not drift, rather than gating the change                         |
| Customer CI/CD on the critical path                     | **Demoted to optional and off the critical path**                                                                                                                                                                     |
| Mirror repo                                             | **Demoted**                                                                                                                                                                                                           |
| Chat Agent                                              | **Removed** — replaced by the deterministic router                                                                                                                                                                    |
| Google Chat + Slack ChatOps                             | **Kept**, restructured. Slack-first with Google Chat parity, both normalized into one internal message and one dispatch path (Phase 15)                                                                               |
| Provisioning scripts, `make gcp-provision`              | **Kept and extended** — 13 ordered steps, each with a matching teardown                                                                                                                                               |

---

## 5. Capability-by-capability

Legend: ✅ built · 🟡 Phase 9, in progress · ⬜ designed and scheduled · — absent

| Capability                                           | Upstream              | Fork                              | Where        |
| ---------------------------------------------------- | --------------------- | --------------------------------- | ------------ |
| **Observe & diagnose**                               |                       |                                   |              |
| Fleet/cluster observation                            | ✅                    | ✅                                | Phases 1–4   |
| Kubernetes event watching                            | ✅                    | ✅                                | Phase 4      |
| Drift detection                                      | ✅ (scheduled audits) | ✅                                | Phase 4      |
| Cost, capacity, compliance audits                    | ✅                    | ✅                                | Phases 1–5   |
| Cloud-event ingress (alerts, webhooks)               | —                     | ✅                                | Phase 4      |
| **Act**                                              |                       |                                   |              |
| Propose a change as a PR                             | ✅                    | ✅ (retained, demoted)            | Phase 1      |
| **Execute a change directly**                        | —                     | 🟡 shadow → ⬜ live               | Phase 9 → 10 |
| Risk classification in code                          | —                     | 🟡                                | Phase 9      |
| Pre-execution snapshot                               | —                     | 🟡                                | Phase 9      |
| Post-execution verification predicate                | —                     | 🟡                                | Phase 9      |
| Automatic rollback on failed verification            | —                     | ⬜                                | Phase 10     |
| **Reverse**                                          |                       |                                   |              |
| Undo plan generated per action                       | —                     | 🟡                                | Phase 9      |
| One-command undo (`/kage undo`)                      | —                     | 🟡                                | Phase 9      |
| Undo health measured continuously                    | —                     | ⬜                                | Phase 14     |
| **Contain**                                          |                       |                                   |              |
| Read-only agent identity                             | ✅                    | ✅ permanent                      | Phase 1      |
| Cannot read Secrets                                  | ✅                    | ✅                                | Phase 1      |
| Namespace-scoped tenant isolation, proven            | —                     | ✅                                | Phase 3      |
| Enforced per-tier egress                             | partial               | ✅                                | Phases 5, 8  |
| Admission policy denying agent writes                | —                     | ✅ `vap-agent-readonly`           | Phase 1      |
| Admission policy enforcing **scope + journal ref**   | —                     | ⬜ `vap-agent-scope`              | Phase 10     |
| Forbidden set (RBAC/IAM/CR/control plane/journal)    | n/a                   | ✅ specified, ⬜ enforced at RBAC | Phases 9, 11 |
| Child ⊆ parent scope webhook                         | —                     | ⬜                                | Phase 11     |
| Cloud IAM attenuation per tier                       | —                     | ⬜                                | Phase 11     |
| **Control**                                          |                       |                                   |              |
| Human review of every change                         | ✅ (the only control) | intentionally **not** the control | —            |
| Approval only for the gated class                    | —                     | ⬜                                | Phase 10     |
| Pause a single agent                                 | —                     | 🟡                                | Phase 9      |
| Fleet freeze                                         | —                     | 🟡                                | Phase 9      |
| Contest / do-not-redo                                | —                     | 🟡                                | Phase 9      |
| Brake works with inference down                      | n/a                   | 🟡                                | Phase 9      |
| **Coordinate**                                       |                       |                                   |              |
| Delegation                                           | ✅ via kanban board   | ⬜ direct mesh call               | Phase 12     |
| Escalation                                           | ✅ via repo files     | ⬜ direct mesh call               | Phase 12     |
| Callee re-authorizes in its own scope                | —                     | ⬜                                | Phase 12     |
| **Provision**                                        |                       |                                   |              |
| Create a cluster                                     | ✅                    | ✅                                | Phases 1–2   |
| Create the cluster's agent **as one action**         | —                     | ⬜                                | Phase 11     |
| Scope removal removes its agent                      | —                     | ⬜                                | Phase 12     |
| **Be proactive**                                     |                       |                                   |              |
| Cron watchdogs                                       | ✅                    | ✅                                | Phases 1–4   |
| Detection ending in **remediation**                  | —                     | ⬜                                | Phase 13     |
| Initiative budgets, flap detection, cooldown         | —                     | ⬜                                | Phase 13     |
| Coexistence with HPAs / operators / GitOps engines   | n/a                   | ⬜                                | Phase 13     |
| **Assure**                                           |                       |                                   |              |
| CI security review gate                              | partial               | ✅ authoritative merge gate       | Phase 5      |
| Formal conformance spec with stable check IDs        | —                     | ✅                                | Phase 0      |
| Chaos / failure-isolation suite                      | —                     | ✅                                | Phase 6      |
| Continuous production SLIs                           | —                     | ⬜                                | Phase 14     |
| **Interface**                                        |                       |                                   |              |
| ChatOps (Slack, Google Chat)                         | ✅                    | ✅ partial, ⬜ complete           | Phases 2, 15 |
| Deterministic routing, authorization before dispatch | —                     | ✅                                | Phases 2–3   |
| `kubectl`-native control surface                     | —                     | 🟡                                | Phase 9      |
| Multi-cluster hub-and-spoke                          | ✅                    | ✅ partial, ⬜ proven             | Phases 2, 15 |
| **Portability**                                      |                       |                                   |              |
| Cloud-agnostic provisioning seams (KCC + Terraform)  | partial               | ✅                                | Phase 7      |
| Non-GKE verification target                          | —                     | ✅                                | Phase 7      |
| Pluggable observability backend                      | —                     | ✅                                | Phase 7      |

---

## 6. Skill inventory

|                 | Upstream  | Fork                                                                                                                                      |
| --------------- | --------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| Platform        | 17 skills | 24 — adds `apply-change`, `detect-drift`, `propose-cluster-admin`, `read-knowledge`, and the workload skills                              |
| Cluster (Admin) | 6         | 12 — adds `apply-change`, `propose-developer-team`, `raise-escalation`, `read-knowledge`, `submit-suggestion`, and platform-domain skills |
| Developer Team  | n/a       | 8                                                                                                                                         |
| Chat            | 0         | n/a                                                                                                                                       |

New skill _classes_ that have no upstream analogue: **`apply-change`** (the sole path from an agent's
reasoning to a mutation), **`raise-escalation`** (structured upward hand-off), **`read-knowledge`**,
and the **`propose-*`** cascade skills that create the next tier down.

Upstream's `manage-cluster` and `cluster-agent-lifecycle` are the ancestors of the cascade path;
`workload-rebalancing` was folded into `gke-workload-scaling`.

> **Known gap, scheduled:** the Developer Team tier currently holds none of the seven workload skills
> its persona is assigned — they sit on Platform. The skill re-allocation lands in Phase 13 with the
> persona rewrite.

---

## 7. Things this fork deliberately does _not_ claim over upstream

- **Upstream's model is correct for its stated goal.** A read-only agent with a PR-only write path is
  the right answer when you cannot yet prove containment and reversibility. This fork spent Phases
  0–9 building that proof; the imperative conversion is what the proof buys.
- **The fork is behind on nothing upstream ships.** Every upstream capability is present, retiered,
  or explicitly superseded — see §4. Nothing was dropped for convenience.
- **Most of the fork's advantage is not yet live.** Eight of fifteen phases are done and **no agent
  anywhere holds write authority today**. The write path exists, runs on real clusters, and executes
  nothing. The honest statement is: _the machinery is built and dark._
- **The broker is a new dependency and a new target.** It fails closed — never falling back to direct
  writes — which trades availability for containment. One broker per agent means there is no
  fleet-wide writer to compromise, but the trade is real and stated rather than argued away.
- **Undo plans can lie.** A snapshot captures an object, not its side effects. Anything in that
  category is _gated_, not undone — which means a classifier gap is a reversibility gap. Upstream
  does not have this failure mode, because upstream does not act.

---

## 8. Reading the delta as a sequence

The fork's relationship to upstream, phase by phase:

| Phases    | Relationship to upstream                                                                                      |
| --------- | ------------------------------------------------------------------------------------------------------------- |
| **0–7**   | Rebuilt upstream's capability on a tiered, containment-first foundation. Same behaviour, checkable properties |
| **8**     | Hardened the boundary upstream leaves to convention                                                           |
| **9**     | Built machinery upstream has no analogue for — **and ran it dark**                                            |
| **10–11** | The actual divergence: agents get write authority upstream will not grant                                     |
| **12**    | Replaced upstream's board-and-file coordination with authenticated direct calls                               |
| **13**    | Turned upstream's watchdogs from reporters into fixers                                                        |
| **14**    | Made the containment claims continuously measured rather than reviewed                                        |
| **15**    | Finished the front door and the fleet topology upstream sketches                                              |
