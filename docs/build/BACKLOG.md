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

_(empty — append new items here, below this line)_

---

## Scheduled

| ID  | Title | Kind | Scheduled into | On  |
| --- | ----- | ---- | -------------- | --- |

_(empty — B-001 and B-002 landed 2026-07-29; see `## Done`.)_

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
