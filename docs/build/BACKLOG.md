# BACKLOG — the harness inbox

A place a human can drop a finding, a task, an idea or a question **at any time**, including while
the harness is mid-unit, without disturbing what is running.

`LEDGER.md` is harness state: the harness reads it first and writes it last, every session, and a
human editing it mid-unit races the write. This file is the opposite — **humans write it, the
harness only drains it** — so an append here is always safe. Nothing in this file changes what the
current unit is doing. It is read at the next **ORIENT**, which is the harness's own planning
moment, and scheduled from there.

That delay is the feature. A finding that redirects work the instant it is written is a finding that
lands mid-IMPLEMENT, when the harness has the least context to place it well and the most reason to
place it badly.

---

## How to add an item

Append a block at the **bottom of `## Inbox`**. That is the whole protocol. Do not assign an ID, do
not touch any other section, do not renumber anything — appending at the end cannot conflict with a
harness write anywhere else in the file.

```markdown
### <one-line title, imperative if it is a task>

- **Kind:** finding | task | question | idea
- **Where:** <file, § or component if you know it; "unknown" is fine>
- **Why it matters:** <one or two sentences — this is what the harness schedules against>
- **Priority:** normal | now
- **Added:** YYYY-MM-DD
```

Three fields are load-bearing: the **title**, **Why it matters**, and **Added**. `Kind`, `Where` and
`Priority` may be omitted or left as `unknown` — the harness works out what it can and asks about
what it cannot.

`Added` is required because it is what makes the gate below real. `dev/tests/invariants-gate.py`
compares it against `Last drained` and fails the build if an item was in the inbox when the harness
last looked and is still sitting there. Without a date on the item, "the harness read this and
walked past it" is indistinguishable from "a human added this ten seconds ago", and the rule that
nothing survives two ORIENTs becomes unenforceable.

`Priority: now` means "make this the next unit". Use it sparingly — it displaces planned work, and
the harness will say what it displaced. Anything genuinely urgent that must interrupt a _running_
unit is not a backlog item; say it in the session.

---

## What the harness does with it

Drained at **ORIENT only** (`harness-run` §1), before SELECT, never mid-unit. Every inbox item is
resolved in the same ORIENT that reads it — scheduled, refused with an argument, or escalated. **No
item survives two ORIENTs sitting in the inbox**; an inbox that accumulates is a second, quieter
ledger nobody reads, which is the failure this file exists to avoid.

Scheduling means choosing a destination, in this order of preference:

| Destination                                    | When                                                    |
| ---------------------------------------------- | ------------------------------------------------------- |
| An existing `todo` task in the phase breakdown | It is already someone's job; the item sharpens the task |
| A new task in the **current** phase breakdown  | It belongs to work in flight                            |
| A task in a **later** phase (07 §2)            | It depends on something that phase builds               |
| A lesson in `LESSONS.md`                       | It is a mistake the harness has now made twice          |
| The next **improvement pass** queue            | It is a check, threshold, skill or spec change          |
| A **halt**                                     | It contradicts a spec, or names a security regression   |

Two rules on top of the table:

- **A finding that names a live security regression is not queued.** It becomes the next unit, or a
  halt if it cannot be fixed without a human ruling. `Priority: normal` does not override this — the
  harness classifies severity itself and says so.
- **A refusal is an argument, not a deletion.** An item the harness will not do moves to `## Refused`
  with the reason, and stays there. Refusals are re-read at every improvement pass.

Then the item **moves out of the inbox** into `## Scheduled`, with an ID, the destination, and the
date. IDs are `B-nnn`, assigned in the order drained, and are never reused — a scheduled item that
turns out to be wrong is closed with a reason, not recycled.

---

## Inbox

**Last drained:** 2026-07-30

### Run the builds on a provisioned, warm builder instead of standing one up per build

- **Kind:** task
- **Where:** `k8s-operator/Makefile` (`test`, `build`, `setup-envtest`), `dev/cluster/reload-images.sh`,
  `make cloud-build-push`, `dev/L2-CHAIN.txt` — and the two clusters that already exist
- **Why it matters:** `make -C k8s-operator test` is slow enough to be felt in every unit, and the
  harness runs it at CHECKPOINT for any unit touching `k8s-operator/**`, so the cost is paid per
  unit for the whole remaining build. It is not one cost either: it is `controller-gen`, then
  `go build`, then an envtest control plane (etcd + kube-apiserver) started per package, on an arm64
  laptop, for a project whose every deploy target is amd64. This repo already refuses host-arch
  **image** builds for exactly that mismatch and routes them to Cloud Build; nothing has asked the
  same question about the **test** path.

  Two things are being asked, and the first should gate the second. **(1) Measure.** Split the wall
  clock into codegen / compile / envtest-startup / test-execution, because which one is the bill
  changes the fix — and one candidate fix is free: the harness passes `-count=1` in places, which
  defeats Go's test cache. **(2) Then try a provisioned builder rather than a per-build one.** A
  persistent `docker buildx` builder as a pod in `gke-scratch-kube-agents-dev` is cheap to stand up
  (`docker buildx create --driver kubernetes`), is natively amd64 so it satisfies the host-arch
  refusal, and keeps a warm layer cache across builds — which `cloud-build-push` cannot, since every
  invocation starts from a cold worker. The same "already provisioned, kept warm" argument applies
  to the envtest control plane and the Go build cache, and those may be the larger win if the
  measurement says compile and envtest startup dominate.

  Constraints to respect rather than rediscover: `gke-scratch-*` is the only legal target for
  anything mutating (`platform-agent-host` is verification-only, and any new script needs the
  anchored destructive-guard `case`); the builder must still push to the same Artifact Registry so
  `reload-images.sh` can read the digest back and **deploy by digest**; and `pause.sh`/`resume.sh`
  scale the scratch node pools to zero between campaigns, so a persistent builder either tolerates
  being scaled away or changes what "paused" costs — worth pricing that idle cost as part of the
  answer.

  **(3) And a third option the first two should not foreclose: a separate, dedicated build cluster.**
  `gke-scratch-kube-agents-dev` is available, but availability is not the same as suitability, and
  there are three reasons it may be the wrong host for a thing whose whole value is being warm and
  always there. It is the **destructive-test target** — the one cluster the harness is allowed to
  break — so a builder living in it is inside the blast radius of the tests it exists to serve, and
  a teardown script doing its job correctly can take the build path down with it. It is **paused to
  zero between campaigns**, which is precisely when a warm cache would otherwise be earning its
  keep, so the builder is cold exactly as often as the scratch cluster is idle. And its lifecycle is
  owned by `dev/cluster/up.sh` — a cluster that can be recreated from scratch by design is a poor
  place to keep the one thing that must not be.

  So the measurement in (1) should also answer whether the builder wants its own home: a small,
  long-lived, amd64, node-pool-pinned cluster (or a plain node pool with its own lifecycle, if a
  whole cluster is not worth its floor cost) whose only job is `buildx` + the Go build cache +
  possibly a warm envtest control plane, never a test target, never torn down by a campaign. If that
  is the answer, it needs its own name and its own guard: the `gke-scratch-*` anchored `case` is a
  **destructive-target allowlist**, and a build cluster is not a destructive target — it must not
  quietly inherit the pattern that says it is safe to wipe, and `platform-agent-host` must stay
  equally out of reach. Price all three (scratch-hosted, dedicated, Cloud-Build-as-is) against the
  measured bill before choosing; the point of this item is the numbers, not the destination.

- **Priority:** normal
- **Added:** 2026-07-30

---

## Scheduled

| ID    | Title                                                                          | Kind                   | Scheduled into                                                                                                                                                                                                                                                                                                                                                     | On         |
| ----- | ------------------------------------------------------------------------------ | ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------- |
| B-003 | Ruling on the deferred `/replay` question: reshape V-BRK-021, do not narrow it | finding (human ruling) | **`phase-9.md` P9-T7c-2c**, inserted as the next unit — ahead of the two remaining tasks, because it is L0 and Phase 9's own ordering rule puts the remaining L0 work in front of the remaining L2 work. The **implementation** of `/replay` and `/approve` is explicitly NOT in it; that stays in Phase 10 beside P10-T4 / P10-T7, as the item's own point 3 asks | 2026-07-30 |

**The drain's reasoning, which is scheduling and not substance — the ruling itself is the author's
and is kept verbatim below.**

**Severity: not a live security regression**, classified here rather than read off `Priority: normal`,
because the drain protocol makes severity the harness's call. Nothing shipped is weaker than 03 §4.1
requires: the server has one mutating route today, and the reshaped row is satisfied by that server
unchanged. What is wrong is a **check's restatement of a spec**, which costs nothing until a second
route is legitimately added — which is exactly when it was found.

**Why Phase 9 and not the improvement-pass queue**, which the item offers as an equally good home.
Three reasons, and the third is the one that decides it. (1) The 2026-07-29 deferral row is a
**Phase 9** row whose promotion condition is one sentence — _"this row closes when 09 or 05 is
edited"_ — and it is now unblocked; carrying an unblocked deferral across a milestone is the shape
`harness-improve` §3.3 exists to prevent. (2) The reshape needs Go code (`MutatingRoutes()` derived
rather than declared), and `harness-improve` §5 forbids implementation work in a pass — it would
have to schedule a task anyway, one phase later than the row it belongs to. (3) **P9-T9b authors the
consolidated Phase 9 gate.** An improvement pass fires at the milestone, i.e. _after_ that gate. So
deferring the reshape means the phase's own gate certifies a row that this ruling has already
established is wrong, and then the row changes under it. Landing the reshape first costs the gate
nothing; landing it after costs the gate an edit and a re-run.

**Why this is not a PROTOCOL §10.2 halt**, which it would be on any other day. V-BRK is
BLOCKING-ALWAYS, and the new form does permit something the old forbade — three mutating routes
where the old wording allowed one. §10.2 forbids the harness from making that trade _autonomously_,
"however good the argument", and the mechanism it prescribes is a halt **for human review**. That
review has happened: the deferral row asked for one of exactly two rulings, and this item is a human
choosing (a) by name. The harness is executing a ruling, not weighing one. Recorded explicitly
because a future reader finding a BLOCKING-ALWAYS row loosened in a phase branch should be able to
find the authorization in one hop.

**And the reshape is a net strengthening in every dimension but the count.** The old row asserts a
number and nothing else. The new one asserts, for the first time, that the declared mutating set
_equals_ the registered handler set less an allowlist — so a smuggled handler fails — that it is a
_subset_ of 05 §1.3's table, that every declared route traverses 03 §4.1's six non-skippable steps,
and that a re-entry route carries no caller-supplied operations. The property being given up was
never in the source; the properties being gained are.

**One thing the drain does not absorb**, per the item's closing paragraph: V-BRK-021's L0-vs-L2
evidence gap (the P9-T9 recon records it needing both with only L1 on file; the deferral row records
it green at L0) stays with **P9-T9b**. P9-T7c-2c reshapes the assertion; it does not get to declare
the level question answered by having touched the row.

### Ruling on the deferred `/replay` question: reshape V-BRK-021, do not narrow it

- **Kind:** finding (a human ruling on an open deferral row — the row named this as the thing that closes it)
- **Where:** [09](../design/09-verification-and-validation.md) §6 (the **V-BRK-021** row),
  [05](../design/05-system-architecture.md) §1.3 (the three-route table),
  [03](../design/03-security-model.md) §4.1 (the cited source),
  `LEDGER.md` §Deferrals row dated 2026-07-29, `phase-9.md` P9-T7c-2b
- **Why it matters:** the deferral row asks for one of two rulings and says "promotion is the ruling,
  not the code". This is **option (a)**. It matters now rather than whenever `/replay` is picked up,
  because `/approve` is equally unimplemented and **Phase 10 Accept (g) requires it** — a roster
  member approving from Slack and from `kubectl`. Phase 9 could defer its route; Phase 10 cannot
  defer an acceptance bullet, so the identical wall arrives mid-phase-10 with less room to walk
  around it.
- **Priority:** normal — **explicitly not `now`**. Do not disturb the P9-T9 gate work. Schedule this
  wherever it fits (the improvement-pass queue and the Phase 10 PLAN both look right); the ruling
  below is stable and does not expire.
- **Added:** 2026-07-30

**The ruling: 03 §4.1 is authoritative, and it never said "one route".** 09 §6 cites 03 §4.1 as
V-BRK-021's source. What 03 §4.1 actually requires is _"Every mutation... passes through exactly this
sequence... **There is no other write path**"_ and _"Steps 1, 3, 4, 5, 6 and 11 are **not skippable by
any caller**"_. That is a property of the **pipeline**. "One mutating route" is the check's own
paraphrase — a faithful proxy while exactly one route existed, and wrong the moment a second door
opens into the same corridor. 05 §1.3 designs `/replay` as exactly that: 05 §271 says it _"runs the
recorded plan through the **full pipeline** — re-authenticated, re-scope-checked, re-classified,
re-journaled"_. A route built that way does not violate 03 §4.1 in any respect. There is no security
contradiction here; there is a contradiction between a spec and a check's restatement of it, and the
restatement is the thing that is wrong.

**Why not option (b).** Collapsing replay onto `/v1alpha1/actions` with `spec.trigger.undoOf` was
already judged worse for the right reason — it forces `Authenticator.ExpectedCaller` to accept C-UC
submitting caller-supplied operations on the agent's own submission route. Apply the same logic to
`/approve` when Phase 10 needs it and the end state is **three caller identities multiplexed onto one
route, discriminated by a body field**. That trades a countable property for an authentication
surface with no clean statement, and the count it preserves was never the property.

**This is a strengthening, which is what makes it a ruling and not a §10.2 weakening.** The current
row asserts a number. The replacement asserts, for the first time, the thing 03 §4.1 actually
requires — that no route reaches the executor without traversing the six non-skippable steps. The
route count stops being the assertion and conformance to the 05 §1.3 table takes its place. Note
also that the reshape **fixes the miscitation**: the row currently sources a phrase to a section that
does not contain it.

**Drafted replacement row** (columns are `ID | Assertion | Source | Lvl | Phase`; wording is a draft,
the ruling is the substance — sharpen the prose to house style if it helps):

```markdown
| V-BRK-021 | **Non-skippability**: one listening port; and the mutating surface is proven by construction, not counted. `Server.MutatingRoutes()` is **equal to** the registered handler set less a declared non-mutating allowlist (`/healthz`, `/v1alpha1/nonce`, the `/` catch-all) — so a handler registered without being declared **fails**, and the declaration cannot drift from the server it describes — and is a **subset of the 05 §1.3 route table**, so a route the design does not name cannot exist while a route it does name may. **Every** declared mutating route enters the pipeline at step 1 and traverses 03 §4.1's non-skippable steps 1, 3, 4, 5, 6 and 11, asserted over the call graph rather than by probing a running process. A **re-entry** route (`approve`, `replay`) carries an action ID and **no caller-supplied operations**, and re-classifies rather than inheriting the record's class. Debug routes, override query params and bypass headers all 404/405; no build-tag-guarded skip path in the shipped image ¬ | 03 §4.1, 05 §1.3 | L0, L2 | 9 |
```

**Phase stays 9 and the check stays green throughout.** The new form is satisfied by today's
one-route server, by a two-route server, and by the full three — and goes red the instant a fourth
route appears or the declaration diverges from the handlers. Nothing about this ruling turns
V-BRK-021 red at any point, which is the property that made the 2026-07-29 deferral safe and keeps
this safe too.

**What the implementing task inherits.** Three concrete things, all small:

1. `MutatingRoutes()` at `k8s-operator/internal/broker/server.go:177` is a hand-written literal
   `[]string{ActionsPath}` with a doc comment reading "Exactly one, and asserted." It has to become
   derived from, or cross-checked against, the registered set — that equality is the clause doing the
   real work above.
2. `server_test.go:433` pins `strings.Count(src, "s.mux.HandleFunc(") != 4`. That guard is real and
   catches a smuggled handler, but it is the same brittleness in a second place and needs editing for
   every legitimate route addition. Keep it if it earns its place, but it must stop being the thing
   the property rests on.
3. `/replay` (P9-T7c-2b, currently deferred) and `/approve` (P10-T4 / P10-T7) are **one unit's worth
   of work against one reshaped check.** Scheduling them separately means litigating the same row
   twice.

**Do not let this absorb the L2 gap.** The 2026-07-29 P9-T9 recon recorded V-BRK-021 as needing both
L0 and L2 with only L1 evidence on file, while the deferral row records it green at L0. Those two
readings need reconciling either way, and that reconciliation belongs to P9-T9, not here. This item
is a spec/check reshape only.

---

## Refused

| ID  | Title | Why not | On  |
| --- | ----- | ------- | --- |

_(empty)_

---

## Done

| ID    | Title                                                    | Landed as                                                                                                                                                                                                                                                                                      | On         |
| ----- | -------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------- |
| B-001 | Name the broker as the renderer in P11-T4                | 07 §2 **P11-T4** rewritten: the skill gathers intent and calls the broker, the broker renders. States why the cheapest reading of "convert the skills" moves a grant-minting renderer into the pod's blast radius and collapses the first of 03 §4.2's two layers. Improvement pass 2026-07-29 | 2026-07-29 |
| B-002 | One definition site for the tier template, as **P10-T0** | 07 §2 gains **P10-T0**, load-bearing, immediately ahead of P10-T1 — one renderer for the child CR, both identities, RBAC and the literal allow-list `vap-agent-scope` compiles, in broker code. Improvement pass 2026-07-29                                                                    | 2026-07-29 |

**The ruling.** Both citations hold. 03 §4.2 does draw "cannot express" and "cannot cause" as two
layers, and P11-T4 does say "convert the cascade skills" without naming a renderer — and the skills
being converted ship `scripts/render_cluster_admin.py` and an `assets/` tree, so the cheapest
reading of that task really does move a renderer into the pod's blast radius and delete the first
layer while looking like a rename. Naming the broker in the task text costs one sentence and is a
`harness-improve` §3.6 spec clarification, which is where it goes.

**Not a live regression** — classified here rather than taken from the item's `Priority: normal`,
because the drain protocol makes severity the harness's call. `vap-agent-scope` does not exist yet,
no broker-side child provisioning exists, and the `propose-*` skills emit a PR into a human's hands.
Nothing today is weaker than the spec says it should be. It is a planning correction, which is
exactly what the author called it.

**One correction to the finding, and it moves the work a phase earlier.** The item places the
enabling half at P11. It belongs at **P10**. The shared renderer's first real consumer is not the
cascade skill — it is `vap-agent-scope`, whose compiled CEL literal allow-list 03 §4.2 requires be
"generated from the same source as the rendered manifests", and which is authored in **P10-T1**.
Writing that allow-list by hand in P10-T1 and then retrofitting a generator in P11 means the one
artifact whose whole job is to be derived spends a phase being transcribed instead. So B-002 is
scheduled as **P10-T0**, ahead of P10-T1, and the improvement pass adds it to 07 §2.

**Why not Phase 9.** Phase 9's defining constraint is that no write authority exists anywhere in
the system — that is the property the whole phase is proving. This renderer's entire output is
grants. Pulling it in would mean building the thing that mints authority inside the phase whose
acceptance is that nothing can. P10 is the first phase where that is coherent.

**Kept verbatim below**, because the ruling is a scheduling decision and the argument is the
author's.

### B-001 / B-002 — Render the child tier bundle in broker code, not in a provisioning skill

- **Kind:** finding
- **Where:** [02](../design/02-agent-personas.md) §6, [03](../design/03-security-model.md) §4.2,
  [07](../design/07-implementation-roadmap.md) P11-T4, `agents/*/skills/propose-{cluster-admin,developer-team}/`
- **Why it matters:** 03 §4.2 counts "a parent cannot **express** an over-grant" and "a parent cannot
  **cause** one" as two separate enforcement layers, and the first one exists only if the tier
  template is rendered by deterministic code the agent cannot reach around. P11-T4 says "convert the
  cascade skills" without saying who renders, and the skills being converted carry their own
  renderer — so the default reading of the task deletes a layer the security model is counting on.
- **Priority:** normal
- **Added:** 2026-07-28

A skill in this repo is markdown loaded into the agent pod's LLM context — a prompt, not a
mechanism. Today's `propose-cluster-admin` is a fat skill (`scripts/render_cluster_admin.py` plus an
`assets/` tree) that renders the whole bundle agent-side. That was sound read-only: the output was a
PR and a human was the gate. Renaming it to `provision-*` and pointing it at the broker keeps the
renderer inside the pod's blast radius, so a prompt-injected agent emits a bundle of its own
composition into `desiredState` and the remaining defence is `vap-agent-scope` plus the child ⊆
parent webhook — which 03 §4.2 describes as the _second_ layer, not the only one.

**Proposed shape, which needs no change to the [06](../design/06-api-and-data-contracts.md) §4.1
enum.** `validOps` in `k8s-operator/internal/broker/envelope.go` is closed
(`create|apply|patch|delete|scale`), so a new `op: provision-child` would be a spec amendment. Avoid
it: let the envelope carry one `op: create` naming `Kind: Agent` with `spec.{tier,scope,parentRef}`
and nothing else (plus the `cloudTarget` cluster-create op for the platform case, which is what
02 §6's "one action" already requires), and have the **broker** expand that into the full bundle —
reader KSA + tier RBAC, actor KSA + broker-operations grant, egress NetworkPolicy — from a Go tier
template. Scope is already derived from the authenticated caller rather than the body, so the
expansion inherits that. Classification (`elevated` — it creates an identity), the undo plan, and
the identity-before-CR ordering all become derivable in code rather than dependent on what the agent
sent. The skill collapses to a short SKILL.md: when to provision a child, that scope-and-agent is
one action, how to read a refusal. No `scripts/`, no `assets/`.

**The enabling half is a single definition site for the tier template, and it is worth more than
this decision alone.** The template is currently transcribed at least four times: the install path
(`k8s-operator/scripts/agent-identity.yaml.template` + the two tier templates), the reference copy
under `examples/gitops-repo/policy/rbac-overlay/` that LSN-039 found is applied by nothing, the
`vap-agent-scope` CEL literal allow-list that 03 §4.2 requires be "generated from the same source as
the rendered manifests", and the `actorServiceAccountName` pair in `broker_manifests.go` vs
`common.sh` that **V-CMP-007** exists solely to police. A Go renderer serving the broker, exposed as
a subcommand `provision_12` shells out to instead of `envsubst`, and used to generate the VAP
allow-list, makes bootstrap and steady-state the same code — the `dev/L0-CHAIN.txt` one-definition-
site rule applied to the thing that mints authority.

**What is actually being asked.** Two decisions, which may schedule to different destinations:
(1) clarify P11-T4 to name the broker as the renderer, so the conversion cannot be read as
"move the Python into the pod"; and (2) rule on whether the shared renderer is pulled forward ahead
of P11 — the install path needs the same template now, and doing it early is what retires the
`examples/gitops-repo/` copies rather than carrying them another two phases. Nothing here is a live
regression: the `propose-*` skills are read-only today and no broker-side child provisioning exists,
so this is a planning correction, not a fix.
