# Overview 04: The Build, Phase by Phase

**Summarizes:** [`docs/design/07-implementation-roadmap.md`](../design/07-implementation-roadmap.md)
· status from [`docs/build/LEDGER.md`](../build/LEDGER.md) and
[`docs/build/archive/LEDGER-phases-0-7.md`](../build/archive/LEDGER-phases-0-7.md)

**Snapshot date:** 2026-07-30. **8 of 15 phases complete.**

---

## The shape of the whole build

The build has two generations, and the seam between them is the point of the project.

```
   ┌── GENERATION 1: read-only ─────────────┐ ┌── GENERATION 2: imperative ──────────────┐
   │                                        │ │                                          │
   0 ──1──2──3──4──5──6──7   ▸   8   ▸   9  │ │  10 ──11──12──13──14──15                 │
   │                         │       │      │ │   │                                      │
 found-  agents observe   contain  build    │ │ grant                                    │
 ations  and propose PRs  the pod  the      │ │ the                                      │
                                   SAFETY   │ │ AUTHORITY                                │
                                   machinery│ │                                          │
   └────────── ✅ merged ──────────┴─🟡──────┘ └────── ⬜ not started ────────────────────┘
```

**The ordering is the single most important thing in the roadmap.** Phase 8 contains the pod.
Phase 9 builds the entire safety machinery with **zero** write authority. Only in Phase 10 does the
first agent get a write credential — and by then the classifier, the journal, the undo path, and the
brake all exist and are tested.

> **An agent with write RBAC and no journal is strictly worse than either the system we have or the
> one we want. Do not reorder these phases.**

Two further rules govern the whole conversion:

- **Tests are replaced, never deleted.** Several currently-green checks assert read-only-ness and
  must fail by design. Each is swapped for its imperative counterpart **in the same phase that
  removes it**, and the phase notes must name the pair. A phase that reduces the number of security
  assertions is wrong.
- **Containment never goes red.** Scope ceiling, no self-escalation, no cross-tenant reach: green
  from the first commit to the last. These are the only assertions the inversion does not touch, and
  they are the reason it is safe at all.

---

## Generation 1 — the read-only system (Phases 0–7) ✅

Built faithfully, works, and is now the thing being converted. Agents observe, diagnose, and open
GitOps pull requests; a customer CI/CD pipeline applies them on merge.

| Phase | Title                                  | What it delivered                                                                                                                                                                                                                                                          |
| ----- | -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **0** | Foundations                            | The repo scaffolding, the design set, and the build harness that constructs everything after it                                                                                                                                                                            |
| **1** | Read-only Platform Agent + GitOps loop | The `Agent` CRD, its controller, the validating webhook, per-tier read-only identities, the admission policy denying every write verb, and the pull-request write path                                                                                                     |
| **2** | Cluster Admin Agent + cascade          | The second tier; the CRD generalized from `PlatformAgent` to a generic `Agent`; the **kage-router** with deterministic slash and `@handle` routing and authorization-before-dispatch; spoke bootstrap; per-tier egress; the cascade skill                                  |
| **3** | Developer Team Agent + isolation proof | The third tier, namespaced identities, the placement clause that blocks a foreign-namespace escape, index-assisted routing, the NL resolve/infer split, thread affinity, and audit attribution                                                                             |
| **4** | Coordination & knowledge               | The session-inject seam with authentication, the Kubernetes **event watcher**, the **event ingress** cloud leg, the knowledge base, the escalation round-trip, **drift detection**, and per-tier heartbeat SOPs                                                            |
| **5** | Security gate & hardening              | The **review-gate** CI scorer that blocks an unmitigated high/critical finding; per-tier NetworkPolicies **proven enforced** by the dataplane; pod hardening (restricted PSS, read-only root filesystem) enforced by admission; end-to-end requester and trace attribution |
| **6** | Failure isolation & resilience         | The **chaos suite** — controller down, tier down, hub down — proving no cascade in any direction                                                                                                                                                                           |
| **7** | Cloud-agnostic seams                   | Matched KCC and Terraform provisioning exemplars, a second reference CI pipeline, a pluggable observability backend, and a vanilla non-GKE verification target                                                                                                             |

**What this generation leaves in place, and Phase 9+ builds onto:** the `Agent` CRD and its
controller, the per-tier identity manifests and render templates (the shape is right; the rules
change), the admission machinery (the same object, inverted), **the entire detection half of
proactivity** — the event watcher, event ingress, and drift detector all built and working — the
ChatOps router, the personas and skills, the knowledge base, session state, attribution plumbing,
the chaos suite, and the whole verification harness.

> The conversion is far less than a rewrite.

---

## Phase 8 — Contain the pod, close the boundary, make the install real ✅

**Goal:** everything that must be true **before** any agent gets write authority. This phase adds no
imperative capability at all; it removes the reasons it would be unsafe to start.

**Why first:** the live install had an open human→agent boundary, no enforced egress, and a
multi-tier install that did not work without a local build. Granting write authority on top of that
would have compounded three known defects instead of fixing them.

| Delivered                                                                                                                                                                                                                                            |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Closed the allowlist bypass** — an empty-string entry made the allowlist look non-empty at every one of four layers, and the controller then emitted an "allow all users" flag. Fixed at all four layers and the escape hatch **deleted entirely** |
| **Enforced egress** — an enforcing dataplane, real CIDRs, policies actually applied, with the metadata-server conflict resolved so Workload Identity still mints tokens                                                                              |
| Applied the tenant isolation manifests provisioning had been skipping                                                                                                                                                                                |
| **Made a multi-tier install work** — replaced an OAuth bridge that hung headless, gave the dashboard its rendered config, and shipped the service aliases without which a namespace agent's calls fail DNS                                           |
| **Image provenance** — every image published, `:latest` retired, and deploys **by digest** so a stale same-tag image cannot silently under-enforce admission and read as green                                                                       |
| **Mechanized the harness invariants gate** — a pre-merge script that fails any diff granting a write verb before the machinery exists, and flags a net reduction in security assertions                                                              |

---

## Phase 9 — The Action Broker, dark 🟡 **(current)**

**Goal:** build the **entire** safety machinery — broker, envelope, classifier, journal, undo plan,
brake — and exercise it end to end **with no write authority anywhere**. Actor ServiceAccounts are
created but hold no tenant authority; every action is dry-run; every `ActionRecord` is a
would-have-executed.

**Why this shape, and it is the point rather than caution:** the hardest and most novel code in the
build lands, gets reviewed, and gets tested against real clusters **while the worst possible bug is
still a no-op**. Phase 10 then becomes a permission change rather than a leap.

| Task   | Work                                                                                                                                                                                                                                         |
| ------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| P9-T1  | **`ActionRecord` CRD + journal store** — attribution, classification, targets, pre-state snapshot, applied diff, verification result, undo plan, lifecycle, TTL, and the audit-export path                                                   |
| P9-T2  | **Action Envelope + broker skeleton** — the service, its API, mTLS and token authentication, and the load-bearing rule that `(tier, scope)` is derived from the **authenticated caller** and never from envelope contents                    |
| P9-T3  | **The risk classifier** — deterministic and table-driven, plus the `ChangePolicy` resource that may only make classification stricter                                                                                                        |
| P9-T4  | **Undo-plan generation** for every supported verb, with the "cannot generate" path that **reclassifies the action as gated**                                                                                                                 |
| P9-T5  | **Snapshot, execute, verify** — server-side apply, a stable field manager, dry-run first, per-kind verification predicates                                                                                                                   |
| P9-T6  | **The brake** — `paused`, `FleetFreeze`, `contested`, and the undo controller, **all working through `kubectl` with inference down**                                                                                                         |
| P9-T7  | **The controller reconciles the pair** — the broker Deployment alongside the agent Deployment for every CR, bound to the actor SA, still minting no RBAC                                                                                     |
| P9-T8  | **Shadow mode** — the agent's `apply-change` path submits real envelopes; the broker classifies, plans undo, and journals a would-have-executed record. Run against real clusters for the phase and **mine the journal for classifier gaps** |
| P9-T10 | Repair the inter-agent credential seam (a live defect found on the install)                                                                                                                                                                  |
| P9-T9  | The consolidated gate: envelope round-trip, scope-spoof rejection, classifier fixture corpus, undo-plan coverage, brake liveness with inference down, fail-closed on journal loss                                                            |

**Progress:** 45 of 49 leaf units done. The broker has a deployment path and two real brokers have
run on a cluster. Remaining: the shadow soak with journal mining, and the consolidated gate.

**The planning defect worth knowing about.** The roadmap says actor SAs are "bound to **empty**
roles" — which is literally incompatible with journalling, because the broker's own operations grant
is what lets it authenticate, read the brake, and write the journal. Omitting it "does not fail safe
— it bricks the tier." So "empty" means **empty of tenant authority**, and the acceptance sweep
became a two-sided assertion. This is the kind of thing the dark phase exists to find.

---

## Phase 10 — First authority: the Developer Team tier ⬜

**Goal:** one tier, the smallest blast radius, becomes genuinely imperative. A Developer Team Agent
fixes things in its own namespace, autonomously, and everything about that is observable and
reversible.

**Why this tier first:** namespace scope is the tightest containment boundary in the system, the
isolation proof for it is already the strongest tested property in the codebase, and a mistake is
bounded to one tenant.

| Delivers                                                                                                                                                                                                                                                           |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **One definition site for the tier template, in broker code** — a single renderer turning `(tier, scope)` into the child's CR, identities, RBAC, **and** the literal allow-list the admission policy compiles. It lives in the broker, **never** in an agent skill |
| **Flip the admission policy** — readers still write nothing; actors may write only their tier template, only in scope, and **only carrying a journal reference**, so an unjournaled write is _rejected_, not merely detected                                       |
| The developer-team actor identity: a namespaced role, no cluster-scoped rule, no RBAC verbs, no escalation verbs                                                                                                                                                   |
| Shadow mode **off** for this tier — the broker executes                                                                                                                                                                                                            |
| **The gated-action approval flow, end to end** — park, notify, approve/reject/expire, resume                                                                                                                                                                       |
| **Verify-then-rollback live** — an action whose verification fails is rolled back automatically and reported as a failure                                                                                                                                          |
| A **minimal Slack approval surface** — the phase that introduces the gated class is the phase a human first _has_ to answer one. Ingress plus the operational verbs and approve/reject buttons. No agent dispatch, no NL: those are Phase 15                       |
| Enforce platform-qualified principals on the allowlist — a rule written since the contract spec and enforced nowhere, which stops being a schema blemish the moment the approval surface re-verifies a clicking user against that roster                           |

---

## Phase 11 — Full authority: Cluster Admin and Platform, with the ceiling enforced ⬜

**Goal:** the remaining two tiers become imperative, and provisioning a child agent becomes a direct
action rather than a pull request — which is exactly when the attenuation ceiling stops being
theoretical.

| Delivers                                                                                                                                                                                                                 |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Cluster-admin and platform actor identities, with the forbidden set excluded at the RBAC level as well as in the broker                                                                                                  |
| **The cross-object child ⊆ parent webhook — now required, no longer deferred.** Pure CEL cannot compare a child's requested scope to its parent's actual scope, and a parent now holds real authority to create children |
| **Cloud IAM attenuation** — every tier's cloud identity is currently bound at the _project_ level with no condition. Tolerable when viewer-only; unacceptable once it can write                                          |
| **Convert the cascade** — creating a cluster and creating its Cluster Admin Agent is **one** action, not two. The **broker renders** the child's grants; the skill only gathers intent                                   |
| Blast-radius caps and per-tier policy defaults — **start strict at the two upper tiers and narrow with evidence**                                                                                                        |

**The subtle rule here:** the cheapest reading of "convert the cascade skills" would move a
grant-minting renderer into the agent pod's blast radius while looking like a rename. The security
model draws "cannot express" and "cannot cause" as two independent layers; an agent that renders its
own child's grants collapses the first.

---

## Phase 12 — The mesh: delegation and escalation ⬜

**Goal:** replace indirect coordination with direct calls, **without turning delegation into
privilege escalation**.

| Delivers                                                                                                                                                                                                                                          |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The mesh transport and discovery — how an agent finds its parent and children, mutual authentication, the request/response schema                                                                                                                 |
| **Callee re-authorization** — the load-bearing rule. A delegated request is classified and scope-checked by the **callee's** broker under the **callee's** identity and gates. Authority is never inherited from the caller                       |
| Refusal, timeout, paused-callee, and loop-prevention semantics                                                                                                                                                                                    |
| Retire polling coordination: escalation becomes a direct call; the parent no longer polls a repo for escalation files                                                                                                                             |
| **Close the lifecycle loop in both directions** — removing a scope removes its agent and identities (gated, because it destroys an identity), and each tier's proactive loop **fixes** an unagented scope one level down rather than reporting it |

That last item is what makes the roster cardinalities — one per project, one per cluster, one per
namespace — **invariants the cascade maintains**, rather than quotas someone allocates. There is
never a scope with no agent or an agent with no scope.

---

## Phase 13 — Relentless proactivity ⬜

**Goal:** the detection machinery that already exists stops filing proposals and starts fixing
things — with the anti-thrash controls that make that safe.

**This is the phase the product promise lives in, and it is deliberately late.** Everything before it
exists so that an agent acting thousands of times a day is a good idea.

| Delivers                                                                                                                                                                                                                                                                                       |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Convert **every** detection path to end in remediation — event watcher, drift detector, alert and webhook ingress, heartbeat SOPs. This is rewiring a terminus, not new detection                                                                                                              |
| **Initiative budgets and anti-thrash** — per-agent rate budget, flap detection that escalates instead of retrying, cooldown after a failed remediation. Exhaustion escalates; it never silently drops work                                                                                     |
| The **self-generated work queue** — improvements found while doing other things, worked when idle                                                                                                                                                                                              |
| **Coexistence with other controllers** — HPAs, operators, and a customer's GitOps engine. Objects under a foreign field manager are gated or off-limits, and write-behind sync lands before the engine reconciles                                                                              |
| **Persona conversion** — rewrite the three `SOUL.md`s and the governance SOPs to the operating character: bias to action, the report format, the honesty rules. Also fixes the skill allocation, which currently gives the Developer Team **none** of the seven workload skills it is assigned |
| Provision the alert and webhook ingress that exists in code but is deployed only by a manual patch                                                                                                                                                                                             |

---

## Phase 14 — Continuous assurance ⬜

**Goal:** the properties this design claims are **measured continuously in production**, not proven
once in a test.

| Delivers                                                                                                                                                                       |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **The four SLIs** as audit-log-derived alerts — zero cross-scope escapes, zero unjournaled mutations, zero self-escalations, undo health. Nothing continuous exists today      |
| Proactivity metrics — MTTR by severity, share of issues resolved without a human, actions per agent per day, with flap and revert counters as the counterweight                |
| **Re-aim the security-review suite.** Every check that treats "the agent has no write verb" as the pass condition would otherwise report the whole system as critically broken |
| **Write-behind IaC sync** — mirror executed state to the customer's repo so their IaC does not drift, with the race and conflict semantics stated honestly                     |
| Per-tier inference virtual keys — budget, rate limit, scoped logging                                                                                                           |

**Acceptance worth quoting:** the security-review suite must pass against the imperative system
**and fail** when the classifier floor is lowered, the journal is bypassed, or a reader identity
gains a write verb.

---

## Phase 15 — Reach and scale ⬜

**Goal:** the remaining carried work — completing the ChatOps front door and the multi-cluster
topology.

| Delivers                                                                                                                                                                                                                                                                                                                               |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Complete the router, Slack-first** — agent dispatch, the handle grammar, channel binding, thread affinity. One fleet-level Slack app held by the router, because a Slack app token permits exactly **one** connection — which is why the previous generation's per-pod relay could never serve more than one tier. Retire that relay |
| **Every deterministic refusal must reply to the human.** Today the reply seam is unimplemented, so unknown-target, unauthorized, and malformed-handle errors are audited and then dropped — which is silence the user actually experiences                                                                                             |
| **Google Chat parity** as the opt-in secondary platform, normalized into the **same** internal message and the **same** dispatch, so no second dispatch path exists                                                                                                                                                                    |
| The production NL inferer and outbound replier. **NL is the last resolution mode** — slash, handle, thread affinity, and channel binding must all resolve without spending an inference call                                                                                                                                           |
| **Hub-and-spoke on real clusters** — private hub endpoints, a real spoke bootstrapped from empty, and the proof that **brokers keep executing local remediation during a hub outage**. If they cannot, a hub blip stops all self-healing fleet-wide                                                                                    |

---

## Definition of Done — the product-level acceptance

Every item must hold **on a live install**, and items 4–7 must be backed by a continuous SLI rather
than a point-in-time test.

1. A platform operator states an intent and the Platform Agent **completes it**.
2. A Cluster Admin Agent **creates and configures** a namespace and its Developer Team Agent.
3. A Developer Team Agent **fixes** a workload problem unprompted, and is provably unable to escape
   its namespace.
4. **Nothing writes but the broker.** Every mutation attributed to an agent identity has a matching
   `ActionRecord`.
5. **Everything is reversible.** 100% of executed non-gated actions carry a validated undo plan.
6. **Scope holds.** No agent reads or writes outside its tier; no agent can modify any agent's RBAC,
   IAM, CR, or the control plane.
7. **The gated class holds.** The floor cannot be lowered by configuration, by chat, or by injected
   content.
8. **The brake works** — within seconds, with inference down, and a human-reverted change is not
   redone.
9. Agents **coordinate directly**, and the callee re-authorizes.
10. Agents are **relentlessly proactive** — measured, with flap and revert counters healthy.
11. **Failure-isolation chaos tests pass with no cascade**, including broker-down (fail closed, no
    fallback to direct writes) and journal-down (refuse to act).

---

## The risks the roadmap names honestly

| Risk                                  | The design's answer                                                                                                                                                                                                                                                                                          |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| The inversion invalidates green tests | Replacement is mandatory and same-phase. A test is never simply deleted.                                                                                                                                                                                                                                     |
| The broker is a new high-value target | Structural mitigations: one broker per scope so there is no fleet-wide writer, no LLM inside it, minimal parsing of untrusted input, and admission enforcing scope independently of it                                                                                                                       |
| The broker is also a new dependency   | **Fail closed** — never a fallback to direct writes. Availability traded for containment.                                                                                                                                                                                                                    |
| **Undo plans can lie**                | A snapshot captures the object, not the side effects — a deleted volume's data, a released IP, a rotated credential. Anything in that category is **gated**, not undone. But the boundary is drawn by the classifier, so **classifier gaps become reversibility gaps** — which is what shadow mode mines for |
| **Wrong-but-authorized actions**      | The hardest residual class: in scope, correctly classified, well executed, based on a bad diagnosis. Verification, budgets, and undo bound it; nothing prevents it. **Expect the first real incidents here, not from a boundary breach**                                                                     |
| Agent versus controller               | Explicit coexistence rules in Phase 13, or the agent fights HPAs and GitOps engines and the flap detector only turns a fight into an escalation                                                                                                                                                              |
| Injected intent within scope          | A successful injection can cause any action the agent was **already authorized** to take. The dial is the size of the ungated class — start it broad.                                                                                                                                                        |
| Trust ramp                            | Teams will not accept namespace-wide write on day one. The stricter-only policy mechanism is the adoption path: ship with a broad gated set, narrow it with journal evidence                                                                                                                                 |
| **Approval fatigue in reverse**       | Too broad a gated class and the system degrades to the read-only generation with extra steps; too narrow and the first irreversible mistake is unrecoverable. **This boundary needs review with real data after Phase 13, not just at design time**                                                          |
