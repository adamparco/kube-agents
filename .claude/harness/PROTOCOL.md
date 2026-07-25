# Harness protocol

The operating contract for an autonomous build harness that turns a **design spec set** into a
**verified implementation**, over many sessions, without a human in the loop between milestones.

This document is **project-agnostic**. Everything project-specific — where the specs live, what the
gates are, how to build and test — is in [`binding.md`](binding.md). Read both.

Companion documents: [`SELF-IMPROVEMENT.md`](SELF-IMPROVEMENT.md) (how the harness gets better and
why it cannot cheat), [`invariants.md`](invariants.md) (the pre-merge gate),
[`LESSONS.md`](LESSONS.md) (the durable lesson store).

---

## 1. The contract

**Given** a design spec set with (a) phased work and per-phase acceptance criteria, and (b) a
conformance specification assigning every check a stable ID, a level, and a gate class — the harness
builds the system phase by phase, proves each phase against its checks, and merges validated
milestones, halting only on conditions it is not permitted to resolve alone.

Three properties make that safe rather than reckless, and they are the whole design:

| Property                       | Mechanism                                                                                      |
| ------------------------------ | ---------------------------------------------------------------------------------------------- |
| **It cannot silently regress** | Every previously-green load-bearing suite re-runs before every checkpoint. A regression halts. |
| **It cannot fool itself**      | It may not weaken a spec or a check to make a run go green (§10, SELF-IMPROVEMENT §4).         |
| **It cannot lose its place**   | All state is in files, never in context. Any session can be killed at any moment and resumed.  |

---

## 2. State machine

One invocation performs **one bounded unit of work** and checkpoints. It does not try to finish a
phase in one run.

```
                 ┌───────────────────────────────────────────┐
                 ▼                                           │
  ORIENT ──► SELECT ──► [PLAN] ──► IMPLEMENT ──► VERIFY ──► CHECKPOINT
     │          │                                   │            │
     │          │                                   ├─ fail ─────┤ (fix, re-verify; no advance)
     │          └─ phase complete ──► REGRESS ──► MILESTONE ──► (next phase)
     │                                                │
     └─ blocker / halt condition ──► HALT ◄───────────┘
                                       │
                     periodically ──► IMPROVE ──► (back to SELECT)
```

| State         | Entry condition                      | Exit                                                    |
| ------------- | ------------------------------------ | ------------------------------------------------------- |
| **ORIENT**    | Every invocation, always first       | State loaded, halt conditions checked                   |
| **SELECT**    | No halt active                       | One unit chosen, or phase found complete                |
| **PLAN**      | Entering a phase with no breakdown   | `phase-<N>.md` written with tasks bound to check IDs    |
| **IMPLEMENT** | Unit selected                        | Code written, local build/format clean                  |
| **VERIFY**    | Implementation done                  | The unit's checks green, with evidence recorded         |
| **REGRESS**   | Phase acceptance met                 | All prior green suites still green                      |
| **MILESTONE** | Regress clean + invariants pass      | Committed, merged, ledger advanced (§7)                 |
| **IMPROVE**   | Cadence reached, or a lesson is open | Lessons mechanized; harness/spec/check changes landed   |
| **HALT**      | Any halt condition (§8)              | **Human only.** The harness does not self-clear a halt. |

---

## 3. The unit of work

A unit is the largest amount of work that can be **implemented, verified, and checkpointed inside
one session** with margin to spare. In practice: one task from the current phase breakdown.

A unit is **done** when all of these hold — not when the code is written:

1. The implementation exists and the project's build/format/lint pass (`binding.md` §Build).
2. Every check ID the task claims is **run** and **green**, with an evidence reference recorded.
3. The ledger is updated: task status, verification log rows, any decision or lesson.
4. Work is committed on the phase branch.

**Prefer finishing an in-progress unit over starting a new one.** A half-finished unit is the one
thing that does not survive a session boundary cleanly.

**If a unit turns out to be too big**, split it in the breakdown, record the split, and do the first
half. Do not carry an oversized unit across sessions hoping it fits next time.

---

## 4. Orientation and resume

State lives in files. Context does not survive; the ledger does. Every invocation begins here, with
no exceptions and no shortcuts on the grounds that "I remember where I was" — you do not.

1. Read the **ledger** (`binding.md` §State) — current phase, current unit, blockers, halt flags.
2. Read **`invariants.md`** — the gate every change must pass.
3. Read the **current phase** in the roadmap and its breakdown file if one exists.
4. Read **`LESSONS.md`** — specifically any lesson tagged for the area you are about to touch. This
   is the step that stops the harness repeating itself, and it is the easiest one to skip.
5. Check **open halts and blockers**. If either is set, do not proceed: summarize and stop.

**Resuming after an interruption.** If the ledger shows a unit `in-progress` with uncommitted work,
first determine whether the work is sound (build + its checks), then either finish it or revert it
cleanly. Never build on top of an unverified partial unit.

---

## 5. Phase lifecycle

**Enter.** Read the phase's goal, work items, and acceptance criteria from the roadmap.

**Plan.** Write `phase-<N>.md` containing, for each task: what to build, which spec sections it
implements (doc + §), the files it will touch, the **check IDs** that prove it, and its weight.
Bind every acceptance bullet to at least one check ID — an acceptance criterion with no check is a
planning defect, not a testing gap. Mirror the task list into the ledger.

**Implement → Verify → Checkpoint**, one unit at a time (§3).

**Phase acceptance.** The phase is complete when every acceptance bullet's checks are green at the
level the conformance spec requires, and the phase's newly-required suites (the ratchet) are green.

**Regress.** Re-run every suite that was green at the end of the previous phase. The ratchet only
ever grows: a suite that entered it never leaves.

**Milestone.** §7.

---

## 6. Verification binding

The harness does not invent tests. It runs the checks the conformance spec defines, **by ID**.

- **Selection.** A unit runs: the checks its tasks claim, plus every BLOCKING-ALWAYS check. A phase
  gate runs those plus the phase ratchet.
- **Levels.** Run each check at the level the spec assigns. A check that requires a live target is
  not satisfied by a static stand-in — that substitution is the single most common false green
  (`SELF-IMPROVEMENT` §4).
- **Evidence.** Every result records `check_id, level, target, result, evidence_ref`. A `pass` with
  no evidence reference is recorded as `skipped`, not `pass`.
- **Gate classes.** BLOCKING-ALWAYS failures halt immediately. BLOCKING-PHASE failures block
  advancing. ADVISORY failures are recorded; a regression against a recorded baseline is reported.
- **Deferral.** A check that cannot run is `deferred` with a **named blocker** — never green, never
  silently skipped. A BLOCKING-ALWAYS check may not be deferred; if it cannot run, that is a halt,
  because the build is not verifiable.
- **Environment preconditions.** Before trusting any live result, confirm the preconditions in
  `binding.md` §Preconditions (image freshness, policy activation, enforcement substrate). These
  exist because each has produced a false green in this repo's history.

---

## 7. Milestones: commit, branch, merge

A **milestone** is a completed phase — not a completed task, and not "enough progress to be worth
saving". Precisely: phase acceptance green, phase ratchet green, regress clean, invariants pass.

**Protocol:**

1. Work accumulates on a phase branch (`binding.md` §Branching) with Conventional Commits as units
   complete. Committing per unit is normal and expected; **merging** is the milestone event.
2. At the milestone, before proposing a merge:
   - run the full gate (phase acceptance + ratchet + regress + invariants);
   - confirm no BLOCKING-ALWAYS check is deferred, quarantined, or skipped;
   - confirm the assertion and coverage ratchets did not fall (SELF-IMPROVEMENT §5);
   - format and lint every changed file.
3. Open a PR with the phase summary, the verification table (check IDs and results), the decisions
   made, and every deferral with its blocker.
4. Merge per `binding.md` §Merge. **Never bypass a required check.** If a check is red, the milestone
   is not done — fix it or halt. Forcing a merge past a gate is the one thing that converts a slow
   build into an untrustworthy one.
5. Update the ledger: phase ✅, merge commit recorded, ratchet extended, next phase entered.

**What a milestone must never do:** merge with a red load-bearing suite; merge with an unexplained
deferral; weaken a check to make the gate pass (§10); or merge work whose evidence references are
missing.

---

## 8. Halt conditions

On any of these the harness **stops and surfaces**. It does not retry around them, work on something
else instead, or clear the condition itself.

1. A **BLOCKING-ALWAYS** check fails, or cannot run.
2. A **regression** — a previously-green suite goes red.
3. An **invariant** would be violated by the change.
4. A **destructive operation** targeting anything but a sanctioned ephemeral environment.
5. A **spec contradiction** with no resolution that preserves every invariant.
6. A change that would **weaken** a spec, check, or gate in a way §10 forbids.
7. **Repeated failure**: the same unit fails verification three times with no new information. This
   is a signal that the diagnosis is wrong, and grinding is worse than stopping.
8. **Resource or credential exhaustion**, or an environment the harness cannot rebuild.

A halt is recorded in the ledger with: what triggered it, what was tried, what the harness believes
the cause is, and the narrowest question a human could answer to unblock it.

---

## 9. Autonomy

**Scheduling.** The harness runs on a timer (`binding.md` §Schedule) or on demand. Each firing is
one invocation of the loop: one unit, then checkpoint. Long builds are many short sessions, not one
long one.

**Session budget.** Aim to checkpoint well before context runs out. If a unit is going to overrun,
checkpoint what is verified, record the remainder as a task, and stop. **A clean stop mid-phase is
free; an unclean stop mid-unit costs a session to untangle.**

**Progress requirement.** Every invocation must end in one of: a completed unit, a recorded halt, a
recorded lesson, or a completed improvement pass. An invocation that ends with none of these is
itself a defect worth a lesson — it means the harness spun without learning anything.

**Interactive surfaces.** Where the built system has interactive surfaces (chat, CLI, UI), the
harness drives them for real at the level the check requires. A check that says a human-facing
command works is not satisfied by a unit test of its handler.

---

## 10. Guardrails on the harness itself

The harness edits its own specs, its own checks, and itself. That is deliberate — it is how it
improves — and it is also the most dangerous capability it has, because the easiest way to make a
build go green is to lower the bar.

**Absolute rules:**

1. **Never weaken to pass.** A spec, check, threshold, or gate may not be changed in the same unit of
   work as the implementation whose failure motivated the change. Diagnose, record, and change it as
   a separate, argued unit.
2. **Load-bearing suites are not autonomously weakenable.** Any change that removes, relaxes, or
   narrows a BLOCKING-ALWAYS check is a **halt for human review**, no matter how well argued.
3. **The ratchets are mechanical.** Assertion count and coverage may not fall (SELF-IMPROVEMENT §5).
4. **Retire, never delete.** A check that is genuinely obsolete keeps its ID with status `RETIRED`
   and a pointer to its replacement.
5. **Spec edits are for correctness, not convenience.** Legitimate: resolving a contradiction, adding
   detail discovered during implementation, recording a decision, tightening something unfalsifiable.
   Illegitimate: making a requirement vaguer so it stops failing.
6. **No destructive action outside sanctioned environments**, and no credential handling beyond what
   `binding.md` sanctions.

Every change under rule 5 is recorded in the ledger with its rationale. Every attempted change under
rules 1–3 is recorded even when refused — a refused weakening is a useful signal about either the
spec or the implementation.
