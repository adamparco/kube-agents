# Design 01: Vision & Scope

**Status:** ✅ Agreed

**Overview:** [README.md](README.md)

---

## TL;DR

`kube-agents` makes **intelligent, autonomous agents the operators of Kubernetes** — not a
reporting layer over it. Humans express _intent_ and supervise _outcomes_; agents carry out fleet
management, tenancy, and troubleshooting by **acting directly** on the cluster and cloud APIs, so
that direct use of `kubectl`, `gcloud`, and the cloud console becomes the exception rather than the
rule.

Agents are **relentlessly proactive**: they watch their scope continuously, and when they find
something broken, drifting, wasteful, or unsafe they **fix it** and report what they did — they do
not wait to be asked, and they do not hand the work back to a human as a suggestion.

It serves **three layered audiences**, mapped onto the project → cluster → namespace containment
hierarchy: **platform teams** who own a project, **cluster administrators** who own a cluster, and
**developer teams** who operate within a namespace. **SRE is not a fourth agent** — it is a class
of critical user journeys (reliability, incident response, capacity, observability) that spans all
three personas, segmented by each persona's scope (see §3). The system is architected to be
**cloud-agnostic Kubernetes** in concept, with **GKE as the first fully supported target**.

---

## 1. The problem

The traditional Kubernetes presentation layer is static, imperative, and fragmented across
`kubectl`, `gcloud` and other cloud CLIs, and web consoles. This forces humans to:

- translate high-level intent ("make this tenant compliant", "this workload is unhealthy — fix it")
  into long sequences of low-level, tool-specific commands;
- react manually to drift, version skew, and policy violations that a system could detect and
  remediate on its own; and
- carry undocumented operational knowledge that doesn't scale across a fleet or a team.

The result is reactive, error-prone, expertise-gated operations.

**And the obvious half-measure does not solve it.** A system that detects the problem and then
files a suggestion has moved the bottleneck, not removed it: a human still has to read the
proposal, understand the context the agent already understood, approve it, and wait for it to
land. For the overwhelming majority of operational work — a crash-looping pod, a missing
NetworkPolicy, an undersized node pool, a drifted label — the review adds latency and no safety
that a scope ceiling and a working undo button do not already provide. The remedy is to let the
agent **finish the job**, and to invest the safety budget where it actually pays: in bounding what
an agent can reach, in making every action reversible, and in stopping only for changes that are
genuinely irreversible.

## 2. The vision (north star)

Replace that layer with **autonomous, intent-driven agents that operate the fleet**. In the target
state:

- Humans interact with the fleet primarily through natural-language **intent** via an agent. Agents
  remove the **human from the middle of the operational loop** — noticing the alert, root-causing
  it, designing the fix, and _carrying it out_.
- Agents **act by default**. When a fix is inside an agent's scope and reversible, the agent
  executes it, verifies the outcome, and reports. Asking a human to approve routine, reversible,
  in-scope work is a **defect**, not caution.
- Agents are **relentlessly proactive**. They hold a continuous watch on their scope, maintain a
  self-generated work queue of improvements, and pursue it when idle. An agent that only responds
  when spoken to is underperforming.
- Every mutation is **brokered, journaled, and reversible**. Agents never touch an API directly:
  they submit an **Action Envelope** to the **Action Broker**, which classifies risk, snapshots
  prior state, executes, verifies, and writes an `ActionRecord` carrying an **undo plan**
  ([04](04-workflow-model.md) §1, [06](06-api-and-data-contracts.md) §4).
- **A small, explicit class of changes stops for a human** — irreversible or high-blast-radius
  ones: data destruction, identity and IAM changes, fleet-wide or cross-tenant effects, production
  traffic shifts, and anything for which no undo plan can be generated. That list is evaluated **in
  code, never by the model** ([03](03-security-model.md) §5).
- **Humans hold the brake.** Any authorized human can `pause` an agent, `freeze` the fleet, or
  `undo` any action immediately. Oversight is exercised over outcomes, continuously — not as a
  precondition for every keystroke.
- **Scope is the ceiling, and it is absolute.** An agent acts only within its own project, cluster,
  or namespace, and can never widen its own authority. This is the invariant that makes the rest
  safe ([03](03-security-model.md) §3).

This is a **full-replacement** ambition, reached by staging: agents augment humans first, and
assume more of the operational surface as trust, safety, and coverage grow. What stages is the
**breadth of scope and the size of the ungated class** — not whether the agent is allowed to act at
all.

## 3. Who it's for (tri-layered audiences & agents)

The audience model is three layers, each served by a dedicated agent persona whose scope maps onto
a level of the Kubernetes containment hierarchy (project → cluster → namespace):

| Layer                | Agent persona            | Cardinality     | User                   | Scope of action                                                                                                                                                        |
| -------------------- | ------------------------ | --------------- | ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Project / fleet**  | **Platform Agent**       | 1 per project   | Platform teams         | Operates the fleet: provisions and upgrades clusters, sets and enforces cross-cluster governance, global RBAC and policy, cost/capacity remediation, compliance fixes. |
| **Cluster**          | **Cluster Admin Agent**  | 1 per cluster   | Cluster administrators | Operates one cluster: node pools, add-ons, namespace/tenant provisioning within the cluster, cluster-scoped policy and quotas, cluster health remediation.             |
| **Namespace / team** | **Developer Team Agent** | 1 per namespace | Developer teams        | Operates one namespace: workload onboarding, scaling, rollout, troubleshooting and repair, observability — constrained by the boundaries the layers above set.         |

**SRE is a cross-cutting concern, not a persona.** Reliability work — incident response, capacity
planning, observability, rollout safety — appears as critical user journeys at every layer, scoped
to that layer's authority: the Platform Agent handles fleet-wide reliability and cross-cluster
capacity; the Cluster Admin Agent handles cluster health, node pools, and cluster-scoped rollouts;
the Developer Team Agent handles workload-level troubleshooting and repair within its namespace.
The same SRE CUJ is served by whichever persona owns the scope it applies to.

The three layers are related by **strict containment**, mirroring their resource scope:

- The **Platform Agent** operates at the project level and provisions/governs the clusters within it.
- The **Cluster Admin Agent** operates within one cluster and provisions/governs the namespaces
  within it — bounded by project-level policy from the Platform Agent.
- The **Developer Team Agent** operates within one namespace and cannot cross it — bounded by the
  cluster- and project-level guardrails above it.

Each layer _defines and constrains_ the layer beneath it and _operates within_ the constraints of
the layer above it. **No agent can act outside its scope, and no agent can grant itself or a child
more scope than it holds.** The concrete roles, boundaries, and relationships of these three
personas are specified in [02-agent-personas.md](02-agent-personas.md); how their boundaries are
enforced is in [03-security-model.md](03-security-model.md).

Layers **talk to each other directly**. A Cluster Admin Agent that needs a bigger node pool asks
the Platform Agent; a Platform Agent rolling out a policy delegates the per-namespace work to
Developer Team Agents. The callee always re-authorizes the request in its own scope
([02](02-agent-personas.md) §2.3).

## 4. Platform reach: cloud-agnostic, GKE-first

**Intent:** the core concepts — agent personas, brokered imperative action, tenancy isolation,
skill-based capability, the agent-orchestration/runtime model — are **Kubernetes-generic** and must
not assume a specific cloud.

**Reality:** **GKE/GCP is the first and only fully supported target today**, and much of the
implementation is deliberately GKE-optimized (Managed Prometheus/OTel, Workload Identity,
GKE-specific skills and console links). Agents run as **Hermes**-harness pods reconciled by the
**kube-agents controller** (the extended `k8s-operator/`), built on **Scion**'s verified per-pod
runtime model ([08](08-agent-runtime-and-identity.md)). Actuation is **in-cluster and synchronous**
— the Action Broker calls the Kubernetes and cloud APIs itself ([05](05-system-architecture.md)
C-AB) — so there is no dependency on a customer's CI/CD system in the critical path. Where a
customer runs a GitOps engine, the broker **writes executed state back** to their repo so the two
do not fight ([04](04-workflow-model.md) §6).

Portability is a design constraint, not a current feature. See the delta and its implications in §6.

## 5. Goals & non-goals

### Goals

- Establish intent-driven, agent-operated infrastructure as the primary interface to a K8s fleet.
- Serve platform, cluster-admin, and developer-team users as three distinct, layered personas
  (1 per project / 1 per cluster / 1 per namespace) with enforced containment boundaries.
- Make agents **act** on what they find — autonomously, within scope, without a human in the loop
  for routine reversible work.
- Make proactive detection **and remediation** of fleet drift a first-class, continuous behavior.
- Make every mutation attributable, journaled, and reversible, with a working one-command undo.
- Reserve human approval for the genuinely irreversible, and make that set explicit and
  code-evaluated.
- Keep core concepts cloud-agnostic even while GKE is the first supported target.

### Non-goals

- **Removing human control.** Humans set intent, define the gated class, supervise outcomes, and
  can pause, freeze, or undo anything at any moment. "Full replacement" is about who executes the
  work, not about who is accountable for it.
- **Unbounded autonomy.** An agent's scope is a hard ceiling. Autonomy is broad _within_ scope and
  exactly zero _outside_ it; that is not a tension, it is the design.
- **Acting without a trace.** Speed is never bought by skipping the journal. An action that cannot
  be recorded and undone is a gated action.
- Being a general-purpose chatbot. The scope is Kubernetes/fleet operations.
- Immediate multi-cloud support. Cloud-agnosticism is an architectural constraint now and a
  supported feature later — not a claim about today's runtime.
- Replacing the customer's IaC as the record of intent. Where a customer keeps desired state in a
  repo, the broker syncs to it ([04](04-workflow-model.md) §6); kube-agents does not demand
  ownership of it.

## 6. Known delta: the current code is the previous generation

Per the "docs lead, code follows" principle, we record this gap rather than hide it. The delta is
unusually large, because the shipped system was deliberately built to the **opposite** rule set:
read-only agents proposing GitOps pull requests. Converting it is the subject of
[07](07-implementation-roadmap.md).

| Area                  | End-state intent                                                                                            | Current reality (the read-only generation)                                                                                                                                                                     |
| --------------------- | ----------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Agent authority       | **Scoped read-write** RBAC + cloud IAM per tier, minus an explicit forbidden set                            | **Read-only by construction** — pre-created `view` + a `get/list/watch` "explorer" ClusterRole per tier; cloud GSAs reconciled to viewer-only                                                                  |
| Write path            | **Action Broker** in-cluster, synchronous, journaled                                                        | A pull request opened by the `submit-suggestion` skill, applied later by the customer's CI/CD (`examples/gitops-repo/.github/workflows/apply.yml`)                                                             |
| Admission policy      | **`vap-agent-scope`** — deny writes outside tier scope, deny the forbidden set, require a journal reference | **`vap-agent-readonly`** — a read-verb allow-list that denies every write verb to an agent identity. This policy is precisely inverted by the conversion and must be replaced, not relaxed                     |
| Approval              | Gated class only, evaluated in code                                                                         | Human review of every change, enforced by branch protection + CODEOWNERS                                                                                                                                       |
| Reversibility         | `ActionRecord` + undo plan + `undo` command                                                                 | `git revert` + re-apply by the pipeline; no in-cluster journal and no undo path exist                                                                                                                          |
| Agent-to-agent        | Direct delegation and escalation over the agent mesh, callee re-authorizes                                  | Explicitly forbidden; coordination is indirect through the repo and OKF (`raise-escalation` writes a file, the parent polls for it)                                                                            |
| Proactivity           | Continuous watch → **remediate** → verify → report, with initiative budgets                                 | Continuous watch → **propose** → wait. The Kubernetes event watcher (`cmd/k8s-event-watcher/`) and drift detection (`detect_drift.py`) already exist and already work — they just end in a PR instead of a fix |
| Tool surface          | Write-capable MCP/tooling restored behind the broker                                                        | Write tools deliberately removed (`create_cluster` retired, `gke` MCP describe/list only, `apply_manifest` helpers deleted)                                                                                    |
| Human brake           | `pause` / `freeze` / `undo`, instant, no merge                                                              | Not needed and not present — a read-only agent has nothing to stop                                                                                                                                             |
| Security-review suite | Re-aimed at scope ceiling, forbidden set, journal completeness, undo health                                 | `.agents/skills/review-security-k8s-agents-*` asserts read-only-ness as a positive finding                                                                                                                     |

**Implication.** This is not a feature addition; it is an inversion of the system's central rule,
and several currently-green safety tests are expected to **fail by design** after the conversion
(most notably the 03 §11 "no write tools" and "no break-glass" checks, and the
`negative-attenuation.sh` write-verb denial). [07](07-implementation-roadmap.md) replaces each such
test with its imperative counterpart in the same phase that removes it — a test is never simply
deleted, and the load-bearing containment checks (cross-scope, self-escalation) **survive
unchanged and must stay green throughout**.

**Ordering constraint (load-bearing).** Build the broker, the risk classifier, the journal, and the
undo path **before** granting any agent write authority. An agent with write RBAC and no journal is
strictly worse than either the current system or the target one. [07](07-implementation-roadmap.md)
sequences this deliberately.

Separately, the pre-existing cloud-portability delta still stands: Managed Prometheus/OTel,
Workload Identity, `gcloud`-shaped skills, and GCP console links remain GKE-coupled, to be factored
behind provider-neutral seams over time. That is direction, not a committed milestone.

## 7. Success criteria (how we'll know it's working)

- A platform operator states an intent ("provision a cluster", "onboard this tenant with correct
  isolation") and the Platform Agent **completes it end-to-end**, with no manual `kubectl`/console
  steps, no human approval for the reversible parts, and a full action trail.
- A cluster administrator's namespace/tenant is **created and configured** by the Cluster Admin
  Agent within the guardrails set by the Platform Agent.
- A developer team's workload issue is **fixed** by their Developer Team Agent, which is provably
  unable to affect another namespace or escalate beyond it.
- The Platform Agent detects an injected drift (RBAC / NetworkPolicy / version skew) and
  **remediates it unprompted**, then reports the change and its undo handle.
- A human runs `undo <action-id>` on any executed action and the prior state is restored.
- A human runs `pause` on a misbehaving agent and it stops acting **immediately**, mid-queue.
- An attempted action outside an agent's scope, or in the forbidden set, is **rejected** — by the
  broker, and again by admission if the broker is bypassed.
- Every agent-driven mutation is attributable and auditable (see
  `docs/designs/audit-logging-user-attribution.md`).

_Four v1 SLIs, measured continuously from the audit log and the journal
([05](05-system-architecture.md) §5, `docs/designs/audit-logging-user-attribution.md`):_

1. **Zero cross-scope escapes** — alert on any agent read, write, or `SubjectAccessReview`-allow
   outside its tier scope. _(Carried over unchanged; still the most load-bearing signal.)_
2. **Zero unjournaled mutations** — alert on any cluster/cloud write by an agent identity with no
   corresponding `ActionRecord`. This is the imperative replacement for the old "zero direct
   mutations" SLI: the concern is no longer _that_ an agent wrote, but that it wrote **outside the
   broker**.
3. **Zero self-escalations** — alert on any agent action that modifies its own (or a sibling's or
   parent's) RBAC, IAM, `Agent` CR, or the kube-agents control plane.
4. **Undo health** — the fraction of `ActionRecord`s carrying a valid undo plan (target: 100% of
   non-gated actions) and the success rate of executed undos (target: no silent failures).

Proactivity is measured, not assumed: **mean time to remediate** by severity, **share of detected
issues resolved without a human**, and **actions per agent per day** — with the flap and revert
counters from [04](04-workflow-model.md) §4.2 as the counterweight, so "relentless" is never
achieved by thrashing.

## 8. Verification

The §7 success criteria are the top-level acceptance. Each is made concrete and machine-checkable in
the relevant spec's **Verification** section (02 §10, 03 §11, 04 §9, 05 §8, 06 §10, 08 §7), indexed
and gated by [09-verification-and-validation.md](09-verification-and-validation.md), and the
per-phase acceptance + **verification loop** in [07-implementation-roadmap.md](07-implementation-roadmap.md)
§2/§5. A build is "working" only when all of those checks pass at the level 09 requires — the four
SLIs in §7 are verified by `V-OBS-001…004`, which assert each alert exists, reads zero, **and fires
when deliberately tripped**.
