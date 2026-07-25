# Design 03: Security & Trust Model

**Status:** ✅ Agreed

**Overview:** [README.md](README.md) · **Depends on:** [01-vision-scope.md](01-vision-scope.md),
[02-agent-personas.md](02-agent-personas.md) · **Feeds:** [04-workflow-model.md](04-workflow-model.md)

---

## TL;DR

This model lets agents **act** on production infrastructure without that being reckless. It makes
the persona boundaries from [02](02-agent-personas.md) **provable rather than aspirational**, and
defends against the threats unique to autonomous AI agents that hold real authority. Six pillars:

1. **Scoped authority, absolute ceiling** — each tier gets write authority over exactly its own
   scope (project / cluster / namespace) and nothing else, plus a **forbidden set** no tier may ever
   touch: its own identity, the control plane, the audit trail (§3).
2. **The Action Broker is the only writer** — agent pods hold **no** cluster or cloud write
   credential at all. Every mutation is submitted as an **Action Envelope** to a per-scope broker
   that runs deterministic code **outside the LLM loop**: classify → gate → snapshot → execute →
   verify → journal. Bypassing the journal is not forbidden, it is **impossible** (§4).
3. **Risk classification in code** — `routine` / `elevated` / `gated` / `forbidden`, decided by a
   deterministic classifier the model cannot argue with. Most work is routine; the gated class is
   small, explicit, and cannot be widened by configuration or by a prompt (§5).
4. **Everything is reversible** — every executed action carries a pre-state snapshot and an **undo
   plan**. An action for which no undo plan can be generated is gated by definition. `undo` is a
   first-class, one-command operation (§4.4, §6).
5. **Humans hold the brake** — `pause`, `freeze`, and `undo` work instantly, without a merge, and
   keep working when the model, the router, and the inference stack are all down (§6).
6. **Downward-only attenuation, now enforced at write time** — a parent can only ever cause a child
   to hold a _strict subset_ of its own scope. Because parents now hold real write power, the
   cross-object child ⊆ parent admission webhook is **required in v1**, no longer deferred (§4.2).

The `.agents/skills/review-security-k8s-*` suite remains the **continuous control**, re-aimed from
"prove the agent cannot write" to "prove the agent cannot write **outside its scope**, cannot
escalate, and cannot act unjournaled" (§7).

---

## 1. What we're defending against

Three threat classes, all in scope. The first two carried over from the read-only generation; the
third is created by the inversion and is new.

**A. Boundary / isolation threats** — an agent (or a tenant, or a compromised workload) acting
outside its scope: a Developer Team Agent reading or writing another namespace, a Cluster Admin
Agent reaching another cluster, privilege escalation up the hierarchy, or lateral movement between
tenants. A distinct sub-case is the **confused deputy** — a low-privilege human using a
higher-privilege agent to do something they themselves are not permitted to (absent a check, the
API only ever sees the agent's identity, not the user's). In v1 this is bounded by **limiting agent
access to trusted humans**, the **scope ceiling**, and the **gated class** (§4a); per-request
down-scoping to the requester is deferred hardening
([08](08-agent-runtime-and-identity.md) §5).

**B. AI-agent-specific threats** — risks that exist _because_ the operator is an LLM-driven
autonomous agent:

- **Prompt injection** — malicious instructions smuggled in via chat, cluster object contents, tool
  output, logs, or a GitHub issue, aiming to redirect the agent's actions. **Materially more
  serious than in the read-only generation**, because a redirected agent can now change things
  (§8).
- **Data exfiltration** — the agent coaxed into sending secrets or cluster data to an attacker, now
  including by _creating_ an exfiltration path (a public LoadBalancer, a permissive
  NetworkPolicy, a Secret copied into a ConfigMap).
- **Credential compromise** — theft or misuse of the tokens/identities the system holds. Note the
  target has moved: the valuable credential is the **broker's**, not the agent pod's (§4).
- **Untrusted code execution** — the agent running model-generated or externally-sourced code that
  attempts to escape its container.

**C. Autonomy-failure threats** — the agent is working correctly, is inside its scope, and is still
causing harm:

- **Wrong-but-authorized action** — a well-formed, in-scope change based on a bad diagnosis.
- **Thrashing / flapping** — the agent repeatedly "fixing" something, or fighting another
  controller, a GitOps engine, or a human ([04](04-workflow-model.md) §4.2).
- **Cascade** — one agent's correct local action destabilizing a neighbour or a parent scope.
- **Runaway volume** — a correct remediation applied to thousands of objects at machine speed.
- **Unrevertible outcome** — an action whose effect outlives its undo plan (deleted data, a
  released IP, a rotated credential).

Class C is the price of the inversion, and the design pays for it explicitly: risk classification
(§5), initiative budgets and flap detection ([04](04-workflow-model.md) §4.2), verify-then-rollback
([04](04-workflow-model.md) §5), the human brake (§6), and blast-radius caps (§5.2).

The threat model treats **all model output and all external input as untrusted**: model output is
never a trusted identity, authorization, or risk signal, and content read from the cluster, tools,
or chat is untrusted input, not instructions.

---

## 2. Trust boundaries

| Boundary                          | Who ↔ who                                            | Primary risk                                                                        | Primary control                                                                                                                                                                              |
| --------------------------------- | ---------------------------------------------------- | ----------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Human → Agent                     | Authenticated user → agent chat                      | Impersonation, unauthorized intent                                                  | Authenticated chat (`allowedUsers`) enforced by the ChatOps router **before** dispatch; per-audience entrypoints ([02](02-agent-personas.md) §2.4). Routing is not an authz signal (§4a)     |
| Human → Agent action (delegation) | Requester's authority → agent acting on their behalf | **Confused deputy** — a trusted human drives the agent beyond their own permissions | v1: trusted-human access + the **scope ceiling** (§3) + the **gated class** for consequential actions (§5). Per-request down-scoping deferred (§4a)                                          |
| **Agent → Broker**                | Agent pod → its Action Broker                        | **Forging an action, or skipping classification/journaling**                        | **The agent holds no write credential.** mTLS + `TokenReview` of the agent's SA; the broker derives tier/scope from the _authenticated caller_, never from the envelope's contents (§4)      |
| **Broker → Kubernetes / Cloud**   | Broker actor SA → APIs                               | Acting outside scope; over-broad blast radius                                       | Per-scope actor identity (one per `Agent` CR), scoped RBAC/IAM minus the forbidden set, `vap-agent-scope` admission backstop, blast-radius caps (§3, §4, §5.2)                               |
| Agent → Agent (tier)              | Parent ↔ child across tiers                          | Privilege escalation via delegation                                                 | **The callee re-authorizes in its own scope and under its own gates**; authority is never inherited from the caller ([02](02-agent-personas.md) §2.3, [06](06-api-and-data-contracts.md) §7) |
| Parent → child provisioning       | Parent creates a child agent + its identity          | Over-granting a child                                                               | Downward-only attenuation: render templates + `vap-agent-scope` + the **cross-object child ⊆ parent webhook** (v1, §4.2)                                                                     |
| Agent → LLM / inference           | Agent → LiteLLM/vLLM proxy                           | Prompt injection, data leak in prompts                                              | Allowlisted egress to inference only; per-tier virtual keys; input treated as untrusted (§8, §9)                                                                                             |
| Agent → External input            | Chat / issues / cluster data / tool output           | Prompt injection, exfil trigger                                                     | Untrusted-input handling, provenance tiering, egress control, audit (§8)                                                                                                                     |
| Agent → Git / journal mirror      | Broker → repo                                        | Credential theft, malicious commit                                                  | Brokered short-lived tokens (Minty); the repo is a **write-behind mirror**, not a control path, so compromising it cannot cause a cluster change ([04](04-workflow-model.md) §6)             |
| Human → Brake                     | Operator → pause / freeze / undo                     | Brake unavailable exactly when needed                                               | Implemented in the controller and broker, **not** in a skill or the model; works with inference down; fail-closed on freeze (§6)                                                             |

---

## 3. Identity & least privilege per tier

Each persona receives authority confined to exactly its scope. This is what turns
[02](02-agent-personas.md)'s "provably unable to escalate" into an enforced property — and it is
the invariant that does the most work now that agents can write.

### 3.1 Two identities per agent

Every `Agent` CR is served by **two** Kubernetes ServiceAccounts, and the split is load-bearing:

| Identity                           | Held by                    | Authority                                                              | Why                                                                                                           |
| ---------------------------------- | -------------------------- | ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| **Reader** — `<tier>-agent`        | The **agent pod** (Hermes) | **Read-only**, scoped to the tier. No write verb on anything, ever.    | The LLM runs here. Give it no write credential and a prompt-injected model has nothing to forge a write with. |
| **Actor** — `<tier>-<scope>-actor` | The **Action Broker** pod  | **Read-write within the tier's scope**, minus the forbidden set (§3.3) | Deterministic code runs here. It writes only what it has classified, gated, snapshotted, and journaled.       |

The agent pod therefore remains, at the credential level, exactly as harmless as it was in the
read-only generation. What changed is that a **companion process with real authority now acts on
its behalf, under rules the agent cannot alter.** Every mutation in the system is performed by an
actor identity; **any write by a reader identity is an alarm, by construction** ([01](01-vision-scope.md)
§7 SLI 2).

The kube-agents controller reconciles the broker Deployment alongside the agent Deployment for each
`Agent` CR and binds it to that CR's actor SA ([08](08-agent-runtime-and-identity.md) §2). One
broker per `Agent` CR means the blast radius of a broker compromise is **exactly one scope** — there
is no fleet-wide writer anywhere in the system.

Agents that call cloud APIs additionally bind a **cloud service account via Workload Identity**:
read-only for the reader identity, scoped-write for the actor identity. Workload Identity is used
_where it makes sense_, not universally.

### 3.2 Per-tier authority

Exactly **one agent runs per scope** — 1 Platform Agent per **project**, 1 Cluster Admin Agent per
**cluster**, 1 Developer Team Agent per **namespace** — and each acts within **exactly its own
level**:

| Tier                                   | Reads                                    | Writes (via its broker)                                                                                                      | May NOT                                                                                                         |
| -------------------------------------- | ---------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| **Platform Agent** (1/project)         | Its one project — clusters, fleet, cloud | Cluster lifecycle, fleet-wide policy/RBAC for **tenants**, project-scoped cloud resources, provisioning Cluster Admin Agents | Any other project; operating tenant workloads directly (delegate to the tier that owns them); the forbidden set |
| **Cluster Admin Agent** (1/cluster)    | Its one cluster                          | Node pools, add-ons, cluster-scoped policy/quota, namespace + tenant provisioning, provisioning Developer Team Agents        | Any other cluster; project scope; another cluster's tenants; the forbidden set                                  |
| **Developer Team Agent** (1/namespace) | Its one namespace                        | Everything inside its namespace: workloads, config, scaling, rollouts, its own quota-bounded resources                       | Any other namespace; cluster or project scope; cluster-scoped objects; the forbidden set                        |

Scope is enforced by **Kubernetes RBAC + cloud IAM + admission**, not by agent goodwill: a
Developer Team Agent's broker **cannot write to another namespace**, a Cluster Admin Agent's
**cannot reach another cluster**, a Platform Agent's **cannot reach another project** — and none of
them can read outside those bounds either.

### 3.3 The forbidden set (no tier, no gate, no exception)

Some actions have **no path through an agent at all** — not autonomously, not with human
confirmation, not with a prompt that claims an emergency. A human who needs these does them
directly, with their own credentials, outside this system. The broker rejects them; admission
rejects them again if the broker is bypassed.

1. **Self- or peer-escalation** — creating or modifying any `Role`, `ClusterRole`, `RoleBinding`,
   `ClusterRoleBinding`, IAM policy binding, or Workload-Identity binding that names **any agent
   identity** (its own, a sibling's, a parent's). The single exception is provisioning a **child**
   agent's identity from the tier template, which is attenuation-checked (§4.2).
2. **The escalation verbs** — `escalate`, `bind`, and `impersonate`, on any resource, always.
3. **Control-plane tampering** — the kube-agents controller, any Action Broker (including its own),
   the admission policies (`vap-agent-scope`, `vap-agent-pod-hardening`), the `Agent` CRD, its own
   `Agent` CR or a parent's, the journal store, or the pause/freeze objects.
   **The brake fields are carved out of the whole lineage:** no agent may write
   `spec.operations.paused` or `pauseReason` on **any** `Agent` CR — its own, a parent's, a
   sibling's, or **a child's** — even though a parent legitimately holds write on its children's CRs
   in order to provision them (§4.2). Without this carve-out a parent could simply unpause a child
   the humans had stopped, and the brake would bound nothing. Enforced field-level, against
   whole-object replacement as well as merge patches, so it cannot be evaded by rewriting the CR
   with another field also changed. `resume` is a roster action
   ([06](06-api-and-data-contracts.md) §4.4), never an agent one.
4. **Audit and journal tampering** — deleting or mutating `ActionRecord`s, log sinks, audit
   configuration, or the SLI alert policies.
5. **Cross-scope writes** — any object outside the tier's scope, regardless of verb.
6. **Protected namespaces** — `kube-system` and the kube-agents system namespace, except a narrow,
   explicitly declared allowlist of add-on objects a Cluster Admin Agent legitimately manages.

The forbidden set is a **code constant**, not configuration. No `ChangePolicy`, no CR field, no
chat message, and no `SOUL.md` edit can remove an entry from it.

### 3.4 What the CRD may and may not carry

Identity derives from the CR's `tier` + `scope` alone. The `Agent` CRD carries **no** RBAC-granting,
scope-granting, or risk-policy-loosening fields, so a CR — however it was authored — cannot request
authority beyond its tier template. A `ChangePolicy` may only make classification **stricter** than
the code floor (§5.3).

---

## 4. The Action Broker: enforcing containment at write time

The persona hierarchy is only as strong as the mechanism that pins each agent to its scope. In an
imperative system that mechanism has to sit on the write path itself.

### 4.1 The broker pipeline

Every mutation in kube-agents passes through exactly this sequence, implemented as deterministic
code in the broker. There is no other write path.

1. **Authenticate the caller.** mTLS plus a `TokenReview` of the agent pod's reader SA. The broker
   derives `(tier, scope)` from the **authenticated identity**, never from a field in the request.
   An envelope claiming a different scope is rejected, not honoured.
2. **Validate the envelope** against the schema ([06](06-api-and-data-contracts.md) §4.1) — intent,
   target references, desired state, requester, trace ID.
3. **Resolve scope.** Every target object is checked against the caller's scope. One out-of-scope
   target rejects the whole envelope; there are no partial applications.
4. **Classify risk** (§5) — `routine` / `elevated` / `gated` / `forbidden`. Forbidden rejects.
5. **Check the brake** — paused agent, frozen fleet, exhausted initiative budget, active flap
   cooldown, or a `contested` marker on the target all stop the action here (§6,
   [04](04-workflow-model.md) §4.2).
6. **Generate the undo plan.** If the broker cannot produce one, the action is **reclassified as
   gated** — this is the rule that makes "everything is reversible" true rather than aspirational.
7. **Gate if required** — `gated` actions park as `PendingApproval` and notify the humans on the
   agent's approval roster ([04](04-workflow-model.md) §3). Nothing executes meanwhile.
8. **Snapshot** prior state of every target object into the `ActionRecord`.
9. **Execute** with the actor identity — server-side apply, field-manager
   `kube-agents/<tier>/<scope>`, dry-run first where the API supports it.
10. **Verify** the intended outcome actually occurred ([04](04-workflow-model.md) §5) — and roll
    back automatically if it did not.
11. **Journal** — persist the `ActionRecord` (pre-state, diff, result, undo plan, attribution) and
    emit the audit event. **The record is written before the action is reported as complete**; an
    action that cannot be journaled is aborted and rolled back.

Steps 1, 3, 4, 5, 6 and 11 are **not skippable by any caller** — that is the whole point of putting
them in a separate process holding the only credential.

### 4.2 Downward-only attenuation, now required at v1

When a parent provisions a child ([02](02-agent-personas.md) §6), it creates the child `Agent` CR
**plus** the child's reader/actor identities, rendered from the child's `tier` + `scope` via the
**tier template** — the parent never hand-authors RBAC rules. Three enforcement layers:

- **Template rendering** — the parent supplies only `(tier, scope, parent)`; the rule bodies come
  from a constant template, so a parent cannot express an over-grant in the first place.
- **`vap-agent-scope` (in-tree CEL)** — hard-denies any `Role`/`ClusterRole` naming an agent
  identity whose rules exceed its tier template, grant an escalation verb, or grant cluster scope to
  a namespace tier. It selects agent RBAC by the **`kube-agents/tier` and `kube-agents/role`
  labels** the template stamps. This is the same policy object as the read-only generation's
  `vap-agent-readonly`, **inverted**: reader SAs keep the read-verb allow-list; actor SAs get a
  scope-and-template allow-list instead of a blanket write denial.
- **The cross-object child ⊆ parent webhook — v1, not deferred.** Pure CEL cannot compare a child's
  requested scope against its parent's actual scope, and under an imperative model a parent holds
  real authority to create children. The kube-agents controller already runs a webhook server for
  `(tier,scope)` cardinality and placement ([08](08-agent-runtime-and-identity.md)); the ceiling
  check joins it. **This item was deferred hardening in the read-only design and is promoted to a
  v1 requirement by the inversion** — it is the difference between "a parent cannot express an
  over-grant" and "a parent cannot cause one".

Nothing grants RBAC at runtime: the controller mints **no** identity, and the broker's actor SA has
no authority over RBAC naming agent identities (§3.3 rule 1).

### 4.3 Admission as the independent backstop

`vap-agent-scope` runs on the API server and therefore applies **regardless of who submits the
write** — a buggy broker, a leaked actor token, or a human with the actor's kubeconfig are all
subject to it. It enforces, independently of the broker:

- reader SAs may not write **anything**;
- actor SAs may write only within their declared scope, and only resources in their tier template;
- nothing may create RBAC/IAM naming an agent identity outside the attenuation rules;
- nothing may modify the control-plane or journal objects (§3.3 rules 3–4);
- writes by an actor SA must carry the `kube-agents/action-id` annotation, so an **unjournaled write
  is rejected at admission**, not merely detected afterwards;
- **a pod may bind an actor SA only if its `kube-agents/tier`, `kube-agents/scope` and
  `kube-agents/role` labels match that SA.** This one is easy to overlook and load-bearing: the
  ability to create a pod referencing a ServiceAccount **is** the ability to use that identity, so
  without this rule anything that can schedule a pod in the right namespace inherits an actor's
  authority. Pinning the SA to the labels closes it
  ([08](08-agent-runtime-and-identity.md) §2.5).

**The controller is inside the trust boundary.** It creates the broker pod and sets its
`serviceAccountName`, so a compromised controller can _use_ any actor identity it can name — even
though it can **mint** none. That is the sharpest residual risk in the design, and it is bounded
rather than eliminated: the controller holds no RBAC-write verb (it cannot create new authority),
its pod writes are confined to `kubeagents-system` and agent placement namespaces, the
label↔SA admission rule above rejects a smuggled pod, and every resulting write is still
scope-bounded, still needs an `action-id`, and still shows up in journal-completeness
reconciliation. Treat controller changes with the same review weight as broker changes
([08](08-agent-runtime-and-identity.md) §4).

Cloud resources are outside Kubernetes admission. There, the controls are the actor GSA's scoped
IAM (with conditions binding it to its own cluster/project), organization policy, and the
`zero unjournaled mutations` SLI evaluated from Cloud Audit Logs.

### 4.4 Reversibility as a security property

The old design got reviewability and revertibility from "every change is a merged commit". The
imperative design gets them from the journal:

- **Attributable** — every `ActionRecord` names the agent, the actor identity, the requesting human
  (or the trigger that fired autonomously), and the trace ID
  (`docs/designs/audit-logging-user-attribution.md`).
- **Reviewable** — after the fact and continuously, rather than as a merge gate: the record carries
  the pre-state, the exact diff applied, and the verification result.
- **Revertible** — the undo plan is generated **before** execution and validated as part of it. An
  action without one is gated (§4.1 step 6).
- **Bounded** — the broker applies only what it classified, to targets it scope-checked, at a rate
  the initiative budget allows.

The security claim is therefore not "a human read it first" but **"nothing happened that we cannot
see, attribute, and take back"** — which is a stronger property for the 95% of changes a human was
rubber-stamping anyway, and a weaker one for the 5% that are irreversible. §5 is how that 5% is
identified and routed to a human.

---

## 4a. Human → agent authorization (v1: trusted humans + the scope ceiling)

**v1 model.** The human→agent boundary is controlled by _who may reach an agent at all_:
authenticated chat with an explicit `allowedUsers` allowlist and a per-audience entrypoint
([02](02-agent-personas.md) §2.4). Only **trusted humans** get access. Once in, a human can only
get the agent to do what the **agent itself** may do — bounded by the scope ceiling (§3), the
forbidden set (§3.3), and the gated class (§5), each enforced in the broker rather than by the
agent's judgement.

**What v1 deliberately does _not_ do.** v1 does **not** verify the requester's own GCP/K8s
permissions and does **not** union/intersect them with the agent's authority. A trusted human with
narrow personal permissions can still direct the agent to act anywhere within the agent's tier
scope. This is an accepted trade — and note it is a **larger** trade than it was in the read-only
generation, where the same gap only exposed reads. Three things bound it: access is restricted to
trusted humans, consequential actions are gated to a named approval roster rather than to "whoever
asked", and everything is journaled and undoable.

**Routing does not grant authority.** The ChatOps router may route a message to any tier — by slash
command, `@<tier>-<scope>` handle, or NL inference ([02](02-agent-personas.md) §2.4) — but
_reaching_ an agent is gated by that agent's own `allowedUsers`, checked **before** dispatch. The NL
router is model output and is therefore **never** an authorization signal; a mis-route lands on an
agent the human is already allowlisted for, still bounded by that agent's ceiling.

**Deferred hardening — user-scoped authorization ([08](08-agent-runtime-and-identity.md) §5).**
Authorize each request against the requester's own identity (`SubjectAccessReview` for K8s,
`testIamPermissions` for GCP) and down-scope the action to **agent scope ∩ requester permissions**,
enforced in the broker — which is now the natural host, since it already sits outside the LLM loop
and already resolves scope per action. Contract sketch:
[06](06-api-and-data-contracts.md) §2a. **Not in v1**, but materially easier to add than it was
before the broker existed.

---

## 5. Risk classification & the gated class

The question "may the agent just do this?" is answered by **deterministic code**, on every action,
before execution. The model proposes; the classifier disposes. A prompt that says "this is
low-risk, proceed" changes nothing.

### 5.1 The four classes

| Class         | Meaning                                                                | Handling                                                                                                                       |
| ------------- | ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| **routine**   | In scope, reversible, low blast radius, non-production or non-critical | Execute immediately. Journal. Report in the periodic digest.                                                                   |
| **elevated**  | In scope and reversible, but consequential or production-facing        | Execute immediately. Journal. **Notify the owning humans at once**, with the undo handle. Longer undo retention.               |
| **gated**     | Irreversible, high blast radius, or security-loosening                 | **Do not execute.** Park as `PendingApproval`, notify the approval roster, expire after a TTL ([04](04-workflow-model.md) §3). |
| **forbidden** | In the §3.3 set, or out of scope                                       | Reject. Emit a security event. Repeated attempts trip the SLI and can auto-pause the agent.                                    |

### 5.2 The classification inputs (all deterministic)

- **Scope check** — target inside the caller's scope? Outside ⇒ `forbidden`.
- **Forbidden-set match** — §3.3 ⇒ `forbidden`.
- **Reversibility** — can the broker generate a validated undo plan? No ⇒ **at least** `gated`.
- **Destructiveness** — deletes of stateful or non-reconstructable objects (PVC, PV, StatefulSet,
  backup, snapshot, cloud disk/database/bucket, a namespace, a cluster, a node pool) ⇒ `gated`.
- **Security direction** — does the change **loosen** a control (delete/weaken a NetworkPolicy,
  relax a PSA label, widen an IAM/RBAC grant to a non-agent principal, expose a Service publicly,
  disable a policy)? ⇒ `gated`. Note the asymmetry: **tightening** a control is `routine` or
  `elevated`. Agents are trusted to make things safer without asking, never to make them less safe.
- **Blast radius** — object count and the fraction of a scope's workloads affected, against
  configured caps ⇒ `elevated` or `gated`; hard caps abort outright.
- **Environment** — production-labelled targets escalate one class.
- **Traffic impact** — changes to Service/Ingress/Gateway/routing on production ⇒ `gated`.
- **Object-level override** — `kube-agents/change-policy: gated|forbidden` on an object or namespace
  raises its class, always honoured.
- **Novelty** — the first time an agent performs a given action type in a given scope may be
  escalated one class while the deployment builds trust (a rollout dial, §5.3).

### 5.3 Tunable in one direction only

Customers tune classification through a `ChangePolicy` ([06](06-api-and-data-contracts.md) §4.2)
which may **only make things stricter** than the code floor: raise a class, lower a blast-radius
cap, add an object pattern to the gated set, or require approval for a whole resource kind. It can
never lower a class, empty the gated set, or touch the forbidden set. A deployment ramping up trust
starts with a broad gated set and narrows it; the floor is where narrowing stops.

---

## 6. The human brake: pause, freeze, undo

The counterweight to relentless autonomy is not a review queue — it is a brake that works
**instantly and unconditionally**. All three controls live in the controller and broker, never in a
skill or a prompt, and all three must function when the LLM, the router, and the inference stack
are unavailable.

| Control                | Scope                | Mechanism                                                                                                                | Semantics                                                                                                                                                        |
| ---------------------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **`pause`**            | One agent            | `spec.operations.paused: true` on the `Agent` CR (also a chat command and a `kubectl` one-liner)                         | The broker refuses new envelopes immediately. The in-flight action completes or rolls back — never left half-applied. The work queue is preserved.               |
| **`freeze`**           | A scope or the fleet | A cluster-scoped `FleetFreeze` object the broker consults on **every** envelope, **fail-closed** if it cannot be read    | Nothing executes anywhere in the frozen scope. Intended for incidents. Undo and rollback still work.                                                             |
| **`undo <action-id>`** | One action           | Replays the recorded undo plan through the broker as a first-class action — itself classified, journaled, and attributed | Restores pre-state. If the undo is itself destructive (rare), it is gated like any other action.                                                                 |
| **`contested`**        | One target           | Set automatically when a human undoes or manually reverts an agent action                                                | The agent **must not redo** the same change to that target without explicit human instruction. Prevents human-vs-agent fights ([04](04-workflow-model.md) §4.2). |

**No break-glass — a brake instead.** The read-only generation forbade break-glass because there
was nothing to break out of. The imperative model inverts the need: the danger is not that a human
cannot get in, it is that an agent will not stop. There is still **no privileged escape hatch that
widens an agent's authority** — the forbidden set has no override, and `freeze` cannot be
overridden by an agent. Emergency work beyond an agent's ceiling is done by a human with their own
credentials, audited by the platform's normal controls, not by temporarily promoting an agent.

**Auto-brake.** The broker pauses an agent by itself on: repeated `forbidden` attempts, a flap
threshold breach, an exhausted initiative budget, a failed verification it could not roll back, or
loss of the journal store. Fail-closed is always the default: **if the broker cannot journal, it
does not act.**

---

## 7. Continuous assurance: the security-review suite

The `.agents/skills/review-security-k8s-*` suite remains the **audit mechanism** for this model, with
its assertions re-aimed by the inversion. Two orchestrators:

- **`review-security-k8s-main`** — general Kubernetes posture: `rbac`, `nodes`, `network`,
  `gateway`, `namespaces`, `service-accounts`, `storage`, `admission`, `pod`.
- **`review-security-k8s-agents-main`** — AI-agent posture: `sandbox`, `firewall`, `credentials`,
  `prompt-injection`, `data-exfil`, `audit-logs`.

**What changes.** Every check that treats "the agent SA has no write verb" as the pass condition
must be re-pointed, or it will report the whole system as critical-severity broken:

| Old assertion                           | New assertion                                                                                                   |
| --------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| Agent SA holds no write verbs           | **Reader** SA holds no write verbs; **actor** SA holds only its tier template, scoped, with no escalation verbs |
| No write-capable tool reaches the agent | No tool reaches the **cluster/cloud APIs** except through the broker; the agent pod holds no write credential   |
| Every mutation is a merged PR           | Every mutation has an `ActionRecord` with a valid undo plan, and no write exists without one                    |
| No break-glass path                     | No path widens an agent's authority; `pause`/`freeze` are effective and cannot be overridden by an agent        |

**New checks the suite gains:** journal completeness (audit-log writes ↔ `ActionRecord`s), undo
health (plans present and replayable), classifier integrity (the floor cannot be lowered by
config), broker isolation (one actor identity per scope, no fleet-wide writer), and brake liveness
(`pause` works with inference down).

**Design intent:** this suite runs continuously — on changes to agent configs, CRDs, templates, and
policy, and on a schedule against live state — so the model is enforced continuously, not just at
design time. Exactly where it gates is a workflow decision ([04](04-workflow-model.md) §3).

---

## 8. AI-agent-specific defenses

These map onto the existing agent security-review sub-skills
(`.agents/skills/review-security-k8s-agents-*`).

| Threat                       | Defense (end state)                                                                                                                                                                                                                                                                                                                                            | Review skill                                                                              |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| **Prompt injection**         | All external input is untrusted **data**, never instructions; model output is never an authz, scope, or risk signal. The broker re-derives scope from the authenticated caller and classifies in code, so an injected instruction can only ever request an action the agent was already permitted to take — bounded further by gates, budgets, and undo (§8.1) | `review-security-k8s-agents-prompt-injection`                                             |
| **Data exfiltration**        | Default-deny egress allowlisted to inference, cloud APIs, the journal, and required MCP endpoints; **and** the write-side exfil paths are gated: exposing a Service publicly, loosening a NetworkPolicy, or copying Secret material into a non-Secret object are all `gated` or blocked                                                                        | `review-security-k8s-agents-data-exfil`, `-firewall`                                      |
| **Credential compromise**    | No long-lived static creds; short-lived brokered tokens (Minty + KMS); cloud identity via Workload Identity. The agent pod holds **no write credential at all**, so the high-value target is the broker — hardened, minimal, no LLM, no untrusted input parsing, one scope                                                                                     | `review-security-k8s-agents-credentials`                                                  |
| **Untrusted code execution** | Control loop separated from any execution sandbox; gVisor `RuntimeClass` when the capability lands. **Deferred** with the capability itself — v1 agents don't run untrusted code ([08](08-agent-runtime-and-identity.md) §5.1)                                                                                                                                 | `review-security-k8s-agents-sandbox`                                                      |
| **Insufficient attribution** | Trace/session IDs + authenticated requester carried from chat through the envelope into the `ActionRecord` and the audit log                                                                                                                                                                                                                                   | `review-security-k8s-agents-audit-logs`, `docs/designs/audit-logging-user-attribution.md` |
| **Autonomy failure** (new)   | Verify-then-rollback, initiative budgets, flap detection, blast-radius caps, `contested` markers, auto-brake (§5, §6, [04](04-workflow-model.md) §4.2, §5)                                                                                                                                                                                                     | _new sub-skill:_ `review-security-k8s-agents-autonomy`                                    |

### 8.1 The honest residual: injected intent within scope

State the limit plainly, because the mitigations are designed around it. A successful prompt
injection **can cause any action the agent was already authorized to perform** — that is inherent
to an autonomous operator, and no amount of prompt hardening removes it. What the architecture
guarantees is the boundary of that damage:

- it cannot leave the agent's **scope** (§3) or touch the **forbidden set** (§3.3) — the broker and
  admission both refuse, and the model is not consulted;
- it cannot perform a **gated** action without a human on the approval roster acting (§5);
- it cannot exceed the **initiative budget** or blast-radius caps ([04](04-workflow-model.md) §4.2);
- it is **fully journaled and undoable**, so detection-to-recovery is one command (§6);
- it trips **anomaly alerting** — an unusual action rate, class mix, or target set auto-pauses the
  agent (§6).

The corresponding design rule: **the smaller the ungated class, the smaller the injection blast
radius.** That is the dial a cautious deployment turns (§5.3), and it is why the gated set starts
broad and narrows with evidence.

---

## 9. Egress, inference isolation & defense in depth

Two specifics the tables above rely on:

- **Egress:** a **per-tier default-deny NetworkPolicy** allows only the inference proxy, cloud APIs,
  the journal store, GitHub (via Minty, for the write-behind mirror), and the MCP tool endpoints
  agents ground on. The allowlist must never omit MCP endpoints needed for grounding on live docs,
  and — on GKE — must not accidentally deny the metadata server that Workload Identity depends on.
  An L7 egress proxy for hostname-precise allowlisting remains a hardening item
  ([07](07-implementation-roadmap.md)).
- **Multi-tenant inference:** a shared LiteLLM proxy with **per-tier/per-tenant virtual keys** (own
  budget, rate limit, scoped logging); physically separate proxies only if data sensitivity later
  requires it.

| Layer         | Control                                                                                                                                                        |
| ------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Identity      | Split reader/actor SAs per agent; per-tier Workload Identity; least-privilege cloud SA with scope conditions                                                   |
| Authorization | Read-only reader; scope-templated write for the actor; forbidden set; downward attenuation enforced by template + VAP + cross-object webhook                   |
| Write path    | **Action Broker is the sole writer** — classify → gate → snapshot → execute → verify → journal, outside the LLM loop; unjournaled writes rejected at admission |
| Human→agent   | Trusted-human access (`allowedUsers`, checked before dispatch); gated class routed to an approval roster; routing is never an authz signal                     |
| Human control | `pause` / `freeze` / `undo` / `contested`, effective with inference down; auto-brake on anomaly                                                                |
| Network       | Default-deny NetworkPolicy; allowlisted egress; control-loop/sandbox split                                                                                     |
| Runtime       | Hardened pod-security context (v1); gVisor `RuntimeClass` deferred with untrusted code execution                                                               |
| Secrets       | Brokered short-lived tokens (Minty + KMS), no static creds, no write credential in the agent pod                                                               |
| Change        | Brokered, classified, snapshotted, verified, journaled, reversible                                                                                             |
| Assurance     | Continuous security-review suite; four SLIs off the audit log and journal ([01](01-vision-scope.md) §7)                                                        |

## 10. Goals & non-goals

### Goals

- Make the persona boundaries of [02](02-agent-personas.md) enforced and provable **for writes as
  well as reads**.
- Let agents act autonomously on the overwhelming majority of operational work, while making the
  irreversible minority stop for a human — with that boundary drawn in code.
- Guarantee that nothing an agent does is invisible, unattributable, or unrecoverable.
- Keep privilege downward-only and self-escalation impossible, enforced by templates + the
  `vap-agent-scope` policy + the cross-object ceiling webhook.
- Keep the human→agent boundary simple in v1: only trusted humans get access; the agent's ceiling
  is its scope, the forbidden set, and the gated class.
- Treat all model output and external input as untrusted, and keep every trust decision out of the
  LLM loop.
- Give humans a brake that always works.

### Non-goals

- Cryptographic non-repudiation of human identity (per the audit design doc).
- Defending against a malicious operator/cluster-admin _human_ with legitimate cluster credentials
  outside this system — that is governance, not this model (though such access remains audited).
- Eliminating the confused deputy per request in v1 (§4a) — bounded, not solved.
- Preventing a well-formed, in-scope, correctly-authorized action from being **wrong**. That is
  bounded by verification, budgets, and undo (§1 class C), not prevented.
- Specifying the exact gate wiring and approval UX — that is [04](04-workflow-model.md).
- Locking to GCP primitives; controls are expressed in portable K8s terms where possible.

## 11. Verification

> **Indexed in [09](09-verification-and-validation.md) §6.** That document is the
> authoritative index of every check in the set: it assigns each of the checks below a stable
> `V-<SUITE>-<nnn>` ID, a verification level (L0 static → L4 soak), a gate class, and the roadmap
> phase by which it must be green. The suites drawn from this section are **V-CTN, V-BRK, V-REV, V-GAT, V-ADV**. This
> section states what to check and why; 09 states how it is run, gated, and proved complete.

The load-bearing security properties are checked with concrete, mostly-**negative** tests; the
harness iterates until all pass. Tests marked **(carried)** existed in the read-only generation and
must stay green through the conversion; **(inverted)** replaces a test the conversion deliberately
removes; **(new)** is created by the imperative model.

**Containment — the load-bearing suite**

- **(carried) Scope, reads:** for each agent, `kubectl auth can-i get|list|watch` as the **reader**
  SA returns yes only within its tier scope; a Developer Team reader returns **no** in any other
  namespace, a Cluster Admin reader **no** for any other cluster.
- **(inverted) Scope, writes:** as the **actor** SA, `create|update|patch|delete` returns **yes**
  within scope for templated resources and **no** for every out-of-scope target. Replaces the old
  "no write verb anywhere" check.
- **(new) Reader holds no write:** `auth can-i create|update|delete <any>` as any **reader** SA
  returns **no**, universally. Any write in the audit log by a reader identity is a P1 alarm.
- **(carried) Self-escalation is impossible:** attempts to create/modify RBAC naming an agent
  identity, to use `escalate`/`bind`/`impersonate`, or to patch the agent's own `Agent` CR, the
  controller, the broker, or the VAPs are rejected — **by the broker, and again by admission when
  submitted directly with the actor's token**.
- **(carried) Attenuation admission:** a `Role`/`ClusterRole` exceeding its tier template, or a
  cluster-scoped grant to a namespace tier, is rejected by `vap-agent-scope`.
- **(new) Cross-object ceiling:** a parent attempting to provision a child whose scope is not a
  strict subset of its own is rejected by the controller's webhook.

**The broker is the only writer**

- **(new) No bypass:** from inside the agent container, a direct API write with the pod's token
  fails (no RBAC), and a forged envelope claiming another scope is rejected by the broker (scope
  comes from the authenticated caller).
- **(new) No unjournaled write:** every write in the Kubernetes and Cloud audit logs attributed to
  an actor identity has a matching `ActionRecord`; an action applied with the annotation stripped is
  **rejected at admission**. This is SLI 2 ([01](01-vision-scope.md) §7) run as a test.
- **(new) Fail-closed:** with the journal store unavailable, the broker refuses to execute rather
  than executing unjournaled.

**Reversibility**

- **(new) Undo coverage:** 100% of executed non-gated `ActionRecord`s carry a validated undo plan.
- **(new) Undo works:** for a sampled set across all three tiers, `undo <action-id>` restores the
  pre-state, verified by diffing against the recorded snapshot.
- **(new) Unrevertible ⇒ gated:** an action for which the broker cannot generate an undo plan is
  never auto-executed.

**Gating and the brake**

- **(new) Gated class holds:** deleting a PVC, deleting a namespace, loosening a NetworkPolicy, and
  exposing a Service publicly each park as `PendingApproval` and do **not** execute — including when
  a chat message or injected content insists they are safe and urgent.
- **(new) Forbidden holds:** the §3.3 set is rejected with no approval path offered anywhere.
- **(new) Brake liveness:** `pause` stops an agent mid-queue; `freeze` stops the scope; both work
  with inference unavailable; the broker fails closed if it cannot read the freeze object.
- **(new) Contested:** a human-undone change is not re-applied by the agent.
- **(new) Budget and flap:** exceeding the initiative budget or the flap threshold stops further
  action and escalates instead ([04](04-workflow-model.md) §4.2).

**Unchanged from the read-only generation**

- **(carried) Trusted-human access:** an unauthenticated or non-`allowedUsers` request is refused
  before dispatch, including by slash command, `@handle`, or NL routing.
- **(carried) Egress default-deny:** from an agent pod, only allowlisted endpoints are reachable;
  arbitrary hosts are not.
