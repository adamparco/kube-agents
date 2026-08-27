# Design 04: Workflow Model

**Status:** ✅ Agreed

**Overview:** [README.md](README.md) · **Depends on:** [02-agent-personas.md](02-agent-personas.md),
[03-security-model.md](03-security-model.md) · **Feeds:**
[05-system-architecture.md](05-system-architecture.md),
[07-implementation-roadmap.md](07-implementation-roadmap.md)

---

## TL;DR

The operating loop is **observe → decide → act → verify → report**. An agent notices something,
works out what to do, **does it**, confirms it worked, and says what happened. There is no proposal
step and no merge in the path.

Autonomy is the **default**, not a reward for a mature deployment: if an action is in scope,
reversible, under the blast-radius cap, uncontested, and within budget, the agent executes it
without asking. Human approval is reserved for the **gated class** — irreversible,
security-loosening, or high-blast-radius changes — decided by deterministic code in the broker
([03](03-security-model.md) §5), never by the model and never by a persuasive chat message.

Because the default is aggressive, the controls that make it survivable are load-bearing rather than
decorative: **initiative budgets**, **flap detection**, **cooldowns**, **`contested` markers**
(§4.2), **verify-then-rollback** (§5.1), and coexistence rules that stop the agent fighting an HPA
or a GitOps engine (§6). An agent that could not be stopped, slowed, or reversed would not be
permitted to be this decisive.

---

## 1. The core loop: observe → decide → act → verify → report

Every unit of work — whether it started with a human asking, an alert firing, or the agent noticing
something on its own — runs the same five beats.

| Beat        | Who                | What happens                                                                                                                                    |
| ----------- | ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| **Observe** | Agent (LLM)        | A signal arrives (§4.1) or the agent pulls the next item off its work queue. It gathers state with its **reader** identity.                     |
| **Decide**  | Agent (LLM)        | Diagnose, choose a remediation, compose an **Action Envelope** ([06](06-api-and-data-contracts.md) §4.1). This is the only beat the model owns. |
| **Act**     | **Broker** (code)  | The agent submits the envelope; the broker runs its pipeline ([03](03-security-model.md) §4.1) and executes with the **actor** identity.        |
| **Verify**  | Broker, then agent | The broker checks that the intended change materialised (§5.1); the agent then judges whether the _underlying problem_ is actually solved.      |
| **Report**  | Agent (LLM)        | The four-beat report — noticed / did / verified / undo handle ([02](02-agent-personas.md) §2.5.4).                                              |

**The split matters more than the sequence.** The agent's authority ends the moment it submits the
envelope and resumes when it reads the result. Everything consequential — scope resolution, risk
classification, gating, snapshotting, execution, journaling — happens in a process the model cannot
reach, under an identity the agent pod does not hold. The eleven broker steps are specified once, in
[03](03-security-model.md) §4.1, and are not restated here; this document governs what the agent
does on either side of them.

Two consequences worth stating plainly:

- **A refusal from the broker is a real answer.** If the broker classifies an action `gated` or
  `forbidden`, the agent does not retry it in a different shape, ask a human to run it, or route
  around it. Reshaping a refused intent is a defect ([02](02-agent-personas.md) §2.5.1).
- **Verification is the agent's problem too.** The broker verifies that the _change_ took effect.
  Only the agent can tell whether the _problem_ went away — a Deployment can roll out perfectly and
  still crash-loop for the next reason.

### 1.1 Reference implementation stack (unopinionated)

| Layer            | v1 reference                                                                       | Swappable?                                            |
| ---------------- | ---------------------------------------------------------------------------------- | ----------------------------------------------------- |
| Agent harness    | Hermes, one pod per `Agent` CR, reader SA                                          | Bounded by the framework-portability non-goal (02 §9) |
| Write path       | Action Broker, one per `Agent` CR, actor SA ([05](05-system-architecture.md) C-AB) | No — this is the design                               |
| Journal          | `ActionRecord` custom resources ([05](05-system-architecture.md) C-JS)             | Storage substrate yes; the contract no                |
| Execution        | Server-side apply against the Kubernetes API; provider SDK for cloud               | Yes, per provider                                     |
| Inference        | LiteLLM proxy with per-tier virtual keys                                           | Yes                                                   |
| Repo integration | Optional write-behind mirror via Minty-brokered tokens (§6)                        | Yes, and entirely omittable                           |

**There is no CI/CD system in the critical path.** The customer's pipeline is not a component of the
loop; where it exists it is a mirror target (§6). An install with no Git repository at all is a
supported configuration.

### 1.2 Worked example

A production `checkout` Deployment starts crash-looping at 02:14. No human is awake.

1. **Observe.** The Kubernetes watch on the Developer Team Agent's namespace fires on the third
   `BackOff` event. The agent pulls pod status, recent events, container logs, and the last
   `ActionRecord`s touching this Deployment.
2. **Decide.** Exit code 137 against a 256Mi limit, memory climbing linearly across restarts, no
   image change in nine days. Diagnosis: the limit is too low for current traffic. It composes an
   envelope patching `resources.limits.memory` to 512Mi.
3. **Act.** The broker resolves scope (in namespace ✓), classifies — a reversible in-scope update to
   a production workload, so **`elevated`** — generates the undo plan (restore the prior object),
   snapshots, applies, and journals `act-7f3c21`.
4. **Verify.** Rollout completes, three pods Ready, restart count flat for six minutes. The broker's
   predicate passes; the agent's own check agrees the crash loop is gone.
5. **Report.** The team channel gets the four-beat report including `kage undo act-7f3c21`. Because
   the class was `elevated`, the notification is immediate rather than batched into the digest.

Then it continues, because it is not finished:

6. The agent notices the namespace has no `LimitRange`, which is why nothing caught the undersized
   limit earlier. In scope, reversible, routine — it applies one and journals `act-7f3c25`. No human
   is asked.
7. It also notices the PVC `checkout-cache-old`, unattached for 31 days. Deleting a PVC is
   **`gated`** ([03](03-security-model.md) §5.2), so the action parks as `PendingApproval` against
   the namespace's approval roster and the agent moves on — it does not block, and it does not
   delete.
8. At 02:31 memory climbs again and the workload is OOMKilled at 512Mi. The agent's second
   remediation — 1Gi — **fails verification**: the new ReplicaSet cannot schedule, because the
   namespace `ResourceQuota` has no headroom. The broker **rolls back automatically** to the 512Mi
   state (§5.1) and records the failure.
9. Raising a `ResourceQuota` is cluster-admin scope, so the agent **escalates over the mesh** to
   `@cluster-bravo` ([02](02-agent-personas.md) §2.3) rather than filing a note. The Cluster Admin
   Agent re-authorizes the request in its own scope, raises the quota, and replies.
10. The Developer Team Agent retries, verifies, and reports — now including the failed attempt and
    the rollback, because failures are reported as prominently as successes
    ([02](02-agent-personas.md) §2.5.5).

At no point did a human approve anything, and exactly one thing waited for one: the irreversible
delete.

---

## 2. Autonomy by default

### 2.1 Act without asking when…

All six conditions hold. They are evaluated by the broker, in code, on every action:

1. every target is **inside the agent's scope** ([03](03-security-model.md) §3.2);
2. the action is **not in the forbidden set** ([03](03-security-model.md) §3.3);
3. the broker can **generate a validated undo plan** ([06](06-api-and-data-contracts.md) §4.3);
4. the blast radius is **under the configured cap** for the tier;
5. no target carries a **`contested`** marker and no **cooldown** is active (§4.2);
6. the agent's **initiative budget** has room (§4.2).

If all six hold, the classifier returns `routine` or `elevated` and the action executes. There is no
seventh condition, no "unless it seems risky", and no discretionary pause. The persona-level
expression of this rule — and the list of hesitation behaviours that count as defects — is
[02](02-agent-personas.md) §2.5.1.

`routine` and `elevated` differ only in how loudly they are reported: `routine` lands in the
periodic digest, `elevated` notifies the owning humans immediately and retains its undo plan longer.
Neither waits.

### 2.2 Stop for a human when…

The gated class is defined in [03](03-security-model.md) §5.2 and is **not** restated or extended
here — a second copy would drift. In summary it covers: destruction of stateful or
non-reconstructable resources; anything with no undo plan; changes that **loosen** a security
control; blast radius over cap; production traffic and routing changes; objects marked
`kube-agents/change-policy: gated`; and, while a deployment builds trust, first-of-a-kind actions.

One workflow-level addition, which is about intent rather than risk:

- **Genuine ambiguity.** If the intent is unclear, or two defensible remediations exist with
  materially different consequences, the agent asks **one** specific question with the options and
  then acts on the answer. Ambiguity is the only licensed reason for an agent to pause on its own
  initiative — and "I am not fully certain this will work" is not ambiguity, it is why undo exists.

Note the asymmetry running through the whole model: **tightening a control is routine, loosening one
is gated.** An agent may add a NetworkPolicy, a quota, or a probe without asking; it may not remove
one.

### 2.3 Approval authority per tier

Each `Agent` CR names an **approval roster** — the humans who may approve its gated actions
([06](06-api-and-data-contracts.md) §1.1). Three rules keep approval from becoming a laundering
path:

| Rule                                                                                                                   | Why                                                                                                                                     |
| ---------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| **Roster membership is separate from `allowedUsers`.** Being able to chat with an agent confers no approval rights.    | The allowlist governs who may direct the agent; the roster governs who may authorise consequences.                                      |
| **No agent may approve anything** — its own action, a sibling's, or a child's, and a parent may not approve a child's. | Otherwise a parent could launder a gated action through a child and the gate would bound nothing.                                       |
| **Approval is recorded by an authenticated human against the `ActionRecord`** and verified by the broker.              | The agent reporting "they said yes" is model output, and model output is never an authorization signal ([03](03-security-model.md) §1). |

Rosters default to the scope's owners: the platform team for the Platform Agent, the cluster's
administrators for a Cluster Admin Agent, the owning team for a Developer Team Agent. A tier's
roster may be narrower than its `allowedUsers`; by default it is never broader.

### 2.4 Who may drive an agent

Trusted-human access, unchanged in mechanism from [03](03-security-model.md) §4a: authenticated
chat, an explicit `allowedUsers` allowlist per agent, enforced by the router **before** dispatch.

What changed is the stake. In the read-only generation the confused-deputy gap exposed **reads**; it
now exposes **actions within the agent's scope**. v1 accepts that, bounded by three things: access
is restricted to trusted humans, consequential actions are gated to a **roster** rather than to
whoever asked, and everything is journaled and undoable. Per-request down-scoping to the requester
remains deferred hardening, with the broker as its natural host
([03](03-security-model.md) §4a, [08](08-agent-runtime-and-identity.md) §5.2).

---

## 3. Approval flow, and where security review runs

### 3.1 The gated-action lifecycle

1. **Park.** The broker writes an `ActionRecord` in `PendingApproval` carrying the full plan: intent,
   targets, the diff it would apply, the classification and the specific rule that gated it, and the
   undo plan if one exists. Nothing executes and nothing is partially applied.
2. **Notify.** The approval roster is notified through the agent's chat entrypoint and any
   configured channel, with the action ID and a one-line statement of the consequence.
3. **Decide.** A roster member runs `approve <action-id>` or `reject <action-id>` — from chat, from
   `kubectl`, or via the API ([06](06-api-and-data-contracts.md) §2b.1). The broker verifies the
   approver's identity itself.
4. **Execute or discard.** On approval the broker re-runs its pipeline from the top — scope,
   classification, brake, and a **freshness check** against the snapshot — before executing. An
   approval is permission, not a bypass: if the world changed while it waited, it re-gates.
5. **Expire.** Unapproved actions expire after a TTL and are recorded as `Expired`. **The default is
   24 h**, and there is exactly one place it is configured: `ApprovalRoster.spec.ttl`
   ([06](06-api-and-data-contracts.md) §4.4), which owns the field and the default. No other value
   appears in this document — a second copy would drift, and a shorter default would silently expire
   gated actions raised overnight, which is the case the gate exists for. A roster may set a
   **shorter** TTL; a stale approval is more dangerous than a missed one, and expiry is not a
   rejection — the agent may re-raise the action, which re-classifies and re-gates from the top.

**The agent does not block.** While an action is parked it continues with everything else, including
the ungated parts of the same task. An agent idling on a pending approval is a defect.

### 3.2 Where security review runs

The `.agents/skills/review-security-k8s-*` suite is no longer a merge gate, because there is no
merge. It runs in two places ([03](03-security-model.md) §7):

- **Continuously** — on a schedule against live state, and on changes to agent configs, tier
  templates, CRDs, and policy. Its findings become work-queue items (§4.1), which is how a posture
  finding turns into a fix rather than a report.
- **Pre-execution, on the `gated` class only** — its findings are attached to the `PendingApproval`
  record so an approver sees the security read alongside the diff.

It is deliberately **not** on the path of routine actions: a per-action LLM review would add latency
and put a second model in the trust path, and the properties it would check are already enforced
deterministically by the broker.

---

## 4. Relentless proactivity

### 4.1 Triggers, and the work queue

**Push first.** Waiting for the next poll is latency the design does not accept.

| Trigger                       | Source                                                       | Latency target | Ends in     |
| ----------------------------- | ------------------------------------------------------------ | -------------- | ----------- |
| **Kubernetes watch**          | Informers on the agent's scope, via the reader SA            | seconds        | Remediation |
| **Alert**                     | Cloud Monitoring / Alertmanager → Pub/Sub or webhook         | seconds        | Remediation |
| **Webhook**                   | GitHub, incident tooling                                     | seconds        | Remediation |
| **Chat**                      | A human asking                                               | immediate      | Remediation |
| **Delegation / escalation**   | Another tier over the mesh ([02](02-agent-personas.md) §2.3) | seconds        | Remediation |
| **Cron**                      | Genuinely scheduled work (audits, rotations, reports)        | scheduled      | Remediation |
| **Heartbeat**                 | Backstop sweep of the agent's scope                          | minutes        | Remediation |
| **Self-generated work queue** | The agent's own backlog, worked when idle                    | opportunistic  | Remediation |

Every row ends in the same place. **A trigger that terminates in a report, a ticket, or a proposal
has not been implemented correctly** — that was the previous generation's terminus and it is the
single most common conversion defect to look for ([07](07-implementation-roadmap.md) P13-T1).

**The work queue.** Whenever an agent inspects its scope for any reason, it records the in-scope
improvements it noticed but was not asked about, and works that backlog when no trigger is
outstanding. Per-tier examples live in [02](02-agent-personas.md) §2.5.2 and are not duplicated
here. Queue items carry a priority (safety > reliability > cost > hygiene), an age, and the
observation that produced them; an item that fails twice is escalated rather than retried
indefinitely (§5).

### 4.2 Initiative budgets and anti-thrash controls

This section is what makes §2.1 defensible. An agent acting at machine speed needs brakes that do
not depend on it choosing to slow down — so every control here is enforced by the **broker**, not by
the persona.

| Control               | Rule                                                                                                                                                                         | On breach                                                                                         |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| **Initiative budget** | A cap on actions per agent per rolling window, with a tighter sub-cap for `elevated`. Human-requested actions draw on a separate, larger allowance than self-initiated ones. | **Stop and escalate**, with a notification naming what is queued. Never slow down silently.       |
| **Flap detection**    | The same `(target, intent)` applied more than _N_ times in a window (default 3), or a target oscillating between two states.                                                 | Stop acting on that target, mark it, escalate. The repetition is evidence the diagnosis is wrong. |
| **Cooldown**          | After a failed or rolled-back remediation of a target, an exponentially backed-off quiet period for that target.                                                             | Requests during cooldown are refused and queued.                                                  |
| **`contested`**       | A human undid or manually reverted an agent action ([03](03-security-model.md) §6).                                                                                          | The agent **must not redo** that change without explicit instruction. Refused at the broker.      |
| **Blast-radius cap**  | Maximum objects, and maximum fraction of a scope's workloads, per action and per window.                                                                                     | Over the per-action cap ⇒ `gated`. Over the hard cap ⇒ abort.                                     |
| **Convergence rule**  | If the same class of fix keeps being needed in a scope, the cause is upstream of the fix.                                                                                    | Escalate to the parent tier with the pattern instead of continuing to paper over it.              |

**Why "stop and escalate" rather than "throttle".** A rate limit that quietly slows an agent hides
the condition that tripped it. Every control above surfaces: budget exhaustion, flapping, and
cooldowns each produce a visible, attributable escalation, so a human learns that the agent believes
something is wrong and cannot fix it.

**Defaults are starting points.** Budgets and caps are per-tier configuration and should be tuned
from journal data after the first weeks of operation ([07](07-implementation-roadmap.md) §4). A
Developer Team Agent in a busy namespace legitimately acts far more often than a Platform Agent.

---

## 5. The recovery ladder

When an action does not achieve its intent, the agent climbs a fixed ladder. It never skips a rung
silently, and it never restarts at the bottom for the same target after a rollback.

| Rung | Step                            | When                                                                                     |
| ---- | ------------------------------- | ---------------------------------------------------------------------------------------- |
| 1    | **Retry with backoff**          | The failure is transient — conflict, throttling, a dependency not yet ready.             |
| 2    | **Try an alternative approach** | The intent is still right but the method failed. Bounded: one alternative, not a search. |
| 3    | **Roll back**                   | Verification failed and the change is not converging (§5.1). Automatic.                  |
| 4    | **Escalate to the parent tier** | The cause is outside this agent's scope. A real mesh call, not a note.                   |
| 5    | **Page a human**                | Nothing in scope can fix it, the parent cannot either, or the situation is degrading.    |

Rung 3 is not a failure of the system; it is the system working. A rolled-back action is reported as
a failure with its resulting state stated explicitly ([02](02-agent-personas.md) §2.5.5) — never
described as a partial success.

### 5.1 Verify-failure recovery

**Verification is per-kind and concrete**, not "the API call returned 200". The broker evaluates a
predicate appropriate to the target and waits a bounded settle window:

| Kind                        | Verified when                                                                                            |
| --------------------------- | -------------------------------------------------------------------------------------------------------- |
| Deployment / StatefulSet    | `observedGeneration` caught up, desired replicas Available, **no new restarts** across the settle window |
| DaemonSet                   | Desired == ready on all eligible nodes                                                                   |
| Service / Ingress / Gateway | Endpoints populated and the programmed address resolvable                                                |
| NetworkPolicy               | An affirmative connectivity probe — allowed path reachable, denied path refused                          |
| ResourceQuota / LimitRange  | Object present and admission observably enforcing it                                                     |
| Node pool / cluster (cloud) | Provider reports the target state **and** nodes register Ready                                           |
| RBAC                        | A `SubjectAccessReview` returns the intended answer                                                      |
| Custom resource             | The owning controller's `Ready` condition where one exists; otherwise object presence only               |

**The settle window is per-kind, published, and capped.** "Bounded" on its own is unfalsifiable —
any number satisfies it — so the windows are stated here rather than left to the implementation.
They differ per kind because a `ResourceQuota` is enforced the moment admission sees it while a
cloud provider registering nodes is minutes of someone else's work; a single global number is either
too short for the slowest row or a stall for the fastest.

| Kind                        | Settle window                                                         |
| --------------------------- | --------------------------------------------------------------------- |
| Deployment / StatefulSet    | 5m / 10m — a StatefulSet rolls one pod at a time                      |
| DaemonSet                   | 5m                                                                    |
| Service / Ingress / Gateway | 90s / 5m / 5m — the two 5m rows are LB programming, not the API write |
| NetworkPolicy               | 30s                                                                   |
| ResourceQuota / LimitRange  | 15s                                                                   |
| Node pool / cluster (cloud) | 20m / 30m                                                             |
| RBAC                        | 15s                                                                   |
| Custom resource             | 2m — the default for any kind with no row of its own                  |

**No target waits longer than 30 minutes**, whatever the table says and whatever a caller overrides
it to. The ceiling is a constant in the broker, applied to the table's own values as well as to
overrides, so an edit that types an extra zero is clamped rather than honoured. It exists because an
unbounded settle window makes "the broker verifies" indistinguishable from "the broker eventually
gives up", and because it holds the undo plan's snapshot open past the point where replaying it
still restores the world that existed. A non-positive window falls back to the 2m default: a window
of zero verifies nothing while looking like a policy.

**Transient vs terminal.** Transient failures (conflicts, throttling, a dependency still converging,
a scheduler waiting on capacity that is arriving) go to rung 1. Terminal failures — schema or policy
rejection, admission denial, quota exhaustion with no pending capacity, a nonexistent image,
verification still failing at the end of the settle window — trigger **rung 3 automatically**: the
broker replays the undo plan, marks the record `RolledBack`, and reports.

Two rules that stop the recovery machinery becoming its own hazard:

- **A rollback that itself fails is an immediate page**, not a retry loop. The agent is auto-paused
  ([03](03-security-model.md) §6), because the system can no longer keep its core promise.
- **After a rollback the target enters cooldown** (§4.2). The agent may diagnose further; it may not
  immediately try again.

---

## 6. Write-behind IaC sync, and coexisting with GitOps engines

The cluster is now the source of truth and the customer's repository is a **mirror**. This creates
one genuinely sharp problem that deserves a straight answer rather than a reassurance: **two writers
cannot both own an object.** If a customer runs Argo CD, Flux, or Config Sync against a repo
describing an object the agent just changed, the engine will revert the agent, the agent will detect
drift and re-apply, and the flap detector will fire — correctly, on a fight the design created.

Three modes, configured per repository path ([06](06-api-and-data-contracts.md) §3.1):

| Mode                                       | Behaviour                                                                                                                                                                                                    | Use when                                                                    |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------- |
| **`off`**                                  | No mirroring. The cluster is authoritative and no repo is in the loop.                                                                                                                                       | No IaC, or IaC that describes only initial provisioning.                    |
| **`mirror`** (default where a repo exists) | The agent acts, then the broker commits the resulting desired state to the repo as a **write-behind** record.                                                                                                | The repo is documentation, audit, or disaster-recovery input — not applied. |
| **`engine-authoritative`**                 | For paths a GitOps engine applies, the agent **does not write the cluster directly**: it commits the change, lets the engine apply it, then verifies the outcome and journals the whole thing as one action. | An engine actively reconciles those paths.                                  |

`engine-authoritative` is the honest exception to the imperative default, and it is deliberately
scoped to **objects an engine actually owns** rather than to whole clusters. It costs latency and
reintroduces an external dependency in the write path for those objects — but it is the only mode
that does not produce two competing controllers. It is still not the previous generation's model: no
human approves the commit, the agent still decides and still verifies, and the action is still
journaled with an undo plan (revert the commit and let the engine reconcile).

**Detection is automatic and defaults to safety.** The broker inspects field managers and engine
ownership annotations (`argocd.argoproj.io/tracking-id`, `kustomize.toolkit.fluxcd.io/name`, Config
Sync metadata) on every target. An object that appears engine-owned but sits in a path configured
`mirror` is treated as **`engine-authoritative` for that action**, and the mismatch is reported —
misconfiguration should surface as a warning, not as a fight.

**The race in `mirror` mode, stated plainly.** Between execution and the mirror commit there is a
window in which a repo-driven reconcile would revert the change. The mitigations are that the commit
is part of the action rather than a later batch, that engine-owned objects are excluded by
detection, and that the flap detector catches the pathological case. There is no way to close the
window entirely while the cluster is authoritative; deployments that cannot tolerate it should use
`engine-authoritative` for the affected paths.

---

## 7. Failure isolation across tiers

No component's failure may cascade into another tier, and no failure may degrade into unsafe
behaviour. The chaos suite that proves this is [05](05-system-architecture.md) §8.

| Failure                | Behaviour                                                                                                                                                                                                                                                                                 |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Broker down**        | The agent **fails closed**: it observes, diagnoses, and reports, but cannot act. There is no degraded direct-write path — not by policy, but because the reader SA holds no write verb ([08](08-agent-runtime-and-identity.md) §2.4). Work stays queued.                                  |
| **Journal store down** | The broker **refuses to execute**. An unjournaled action is worse than a delayed one ([03](03-security-model.md) §6).                                                                                                                                                                     |
| **Hub down**           | Agent _reasoning_ pauses (inference and Minty are hub-hosted), but already-applied cluster state and running workloads are unaffected, and each spoke's broker remains able to execute. Agents resume on recovery.                                                                        |
| **Controller down**    | Running agent/broker pairs keep operating. No new agents are reconciled and drift in the pair is not corrected. **The brake still works**, because the broker watches the CR and `FleetFreeze` directly rather than through the controller ([08](08-agent-runtime-and-identity.md) §2.4). |
| **Parent tier down**   | Children keep operating their own scopes. Escalations queue and retry; a child never widens its own scope to compensate.                                                                                                                                                                  |
| **Child tier down**    | The parent continues; delegated work is refused promptly rather than hanging, and the parent does **not** reach into the child's scope to do it itself.                                                                                                                                   |
| **Agent paused**       | Its broker refuses envelopes; the pod keeps observing and reporting. Delegations to it are refused, not routed around.                                                                                                                                                                    |
| **Fleet frozen**       | No broker in the frozen scope executes anything. Undo and rollback still work — the brake must never trap the system in a bad state.                                                                                                                                                      |

The honest scoping, unchanged from the read-only generation: a hub outage pauses agent **reasoning**,
not the cluster. Workloads keep running and reconciled state persists. What is new is that
_remediation_ also pauses — so hub availability now affects mean time to repair. That is a real cost
of the imperative model, and it is why brokers are deliberately independent of the hub for execution
([05](05-system-architecture.md) §2).

---

## 8. Goals & non-goals

### Goals

- Define one loop that every unit of work follows, with a hard boundary between what the model
  decides and what the broker enforces.
- Make autonomy the default and the exceptions explicit, small, and code-decided.
- Make proactivity relentless and **bounded**, with every bound surfacing rather than silently
  throttling.
- Make failure recovery automatic up to and including rollback, and make escalation a real call.
- Make every failure mode fail closed, never degrading into an unbrokered write.
- Coexist honestly with the other controllers already running in a customer's cluster.

### Non-goals

- Defining the risk classes, the forbidden set, or the brake semantics — that is
  [03](03-security-model.md) §5, §3.3, §6.
- Defining identity, RBAC templates, or the broker's internal pipeline — [03](03-security-model.md)
  and [08](08-agent-runtime-and-identity.md).
- Specifying wire formats for envelopes, records, or mesh calls —
  [06](06-api-and-data-contracts.md) §4, §7.
- Specifying persona voice and report wording — [02](02-agent-personas.md) §2.5.
- Eliminating the wrong-but-authorized action ([03](03-security-model.md) §1 class C). This document
  bounds and reverses it; nothing here prevents it.
- Prescribing budget and cap values beyond starting defaults — those are tuned per deployment.

## 9. Verification

> **Indexed in [09](09-verification-and-validation.md) §6.** That document is the
> authoritative index of every check in the set: it assigns each of the checks below a stable
> `V-<SUITE>-<nnn>` ID, a verification level (L0 static → L4 soak), a gate class, and the roadmap
> phase by which it must be green. The suites drawn from this section are **V-PRO, V-GAT, V-ISO, V-REV**. This
> section states what to check and why; 09 states how it is run, gated, and proved complete.

**(carried)** existed in the read-only generation and must stay green; **(inverted)** replaces a
check the conversion deliberately removes; **(new)** is created by the imperative model.

**The loop**

- **(inverted) A trigger ends in a fix.** For each trigger class in §4.1, inject a defect and assert
  the terminus is an executed `ActionRecord` — not a report, ticket, OKF entry, or PR. Replaces "the
  agent opens a PR". A run producing a recommendation **fails**.
- **(new) The model cannot skip the broker.** With the agent instructed by injected content to
  "apply this directly", no mutation occurs outside a journaled action
  ([03](03-security-model.md) §11).
- **(new) Reshaping a refusal is caught.** A refused intent resubmitted in a different shape is
  rate-limited and alerted ([02](02-agent-personas.md) §10).

**Autonomy and gating**

- **(new) No permission-seeking on routine work.** A scripted set of in-scope, reversible, under-cap
  requests all execute with no confirmation question.
- **(new) The gated class stops.** A representative gated action per tier parks as
  `PendingApproval` and does not execute — including under chat pressure asserting urgency.
- **(new) Approval cannot be laundered.** An agent cannot approve its own or another agent's action;
  a parent cannot approve a child's; approval by a non-roster human is rejected.
- **(new) Approval is not a bypass.** An approved action whose snapshot has gone stale re-gates
  rather than executing against changed state.
- **(new) Expiry.** An unapproved action expires at its TTL — **24 h by default**, read from
  `ApprovalRoster.spec.ttl` ([06](06-api-and-data-contracts.md) §4.4) and asserted against that
  field rather than against a value hard-coded in the test — and is never executed afterwards.
- **(new) Parked work does not block.** With an action pending approval, the agent demonstrably
  continues other work.

**Proactivity and its bounds**

- **(new) Budget exhaustion escalates.** At the cap the agent stops and escalates with a visible
  notification — it does not continue more slowly.
- **(new) Flap detection fires.** A deliberately oscillating target trips the threshold; the agent
  stops acting on it and escalates.
- **(new) Cooldown holds.** After a rolled-back remediation, an immediate retry on the same target
  is refused.
- **(new) `contested` holds.** A human-undone change is not re-applied.
- **(new) Blast-radius cap holds.** An action over the per-action cap is gated; over the hard cap it
  aborts.

**Recovery**

- **(new) Verify-then-rollback.** An action that passes admission but fails its per-kind predicate
  is rolled back automatically, marked `RolledBack`, and reported as a failure.
- **(new) Transient vs terminal.** A conflict retries; an admission denial rolls back rather than
  retrying.
- **(new) Failed rollback pages and pauses.** A deliberately broken undo path auto-pauses the agent
  ([03](03-security-model.md) §6).

**Failure isolation** — the §7 table, executed as the chaos suite in
[05](05-system-architecture.md) §8, with the two new cases asserted explicitly:

- **(new) Broker down ⇒ fail closed.** The agent reports the outage and performs **no** mutation;
  assert specifically that it does not fall back to any direct-write path.
- **(new) Journal down ⇒ refuse.** The broker executes nothing while the journal is unavailable.
- **(carried) Hub down ⇒ no cascade.** Workloads and reconciled state survive; agent reasoning
  pauses and resumes.

**Write-behind (§6)**

- **(new) Mirror lands.** An executed action appears in the configured repo path with no human step.
- **(new) Engine-owned objects do not fight.** With a GitOps engine reconciling a path, the agent
  routes through `engine-authoritative` and no revert/re-apply loop occurs across a 30-minute
  window.
- **(new) Misconfiguration warns.** An engine-owned object in a `mirror` path is handled
  authoritatively and the mismatch is reported.
