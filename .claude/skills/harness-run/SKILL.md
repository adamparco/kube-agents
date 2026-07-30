---
name: harness-run
description: Drive the autonomous build forward by exactly one bounded unit of work — orient from the ledger, select or finish a unit, implement, verify by check ID, checkpoint. Use to start or continue the harness build, to resume after an interruption or a killed session, or when a scheduled firing asks for "the next unit". Not for running checks alone (use harness-verify), closing a phase (harness-milestone), or mechanizing lessons (harness-improve).
---

# harness-run — one bounded unit, then checkpoint

Executes the loop in `.claude/harness/PROTOCOL.md` §2: ORIENT → SELECT → [PLAN] → IMPLEMENT →
VERIFY → CHECKPOINT. **One unit per invocation.** Do not try to finish a phase in one run.

Project-specific values — ledger path, build commands, branch names, cluster targets, cadence —
come from `.claude/harness/binding.md`. Never hardcode them here or in the work.

---

## 1. ORIENT — always first, in this order

Context does not survive; files do. "I remember where I was" is false.

1. **Ledger** (`binding.md` §State) — current phase, current unit, blockers, halt flags, deferral
   list, metrics.
2. **`.claude/harness/invariants.md`** — the gate every change must pass.
3. **Current phase** in `docs/design/07-implementation-roadmap.md` §2, plus its breakdown file if
   one exists.
4. **`.claude/harness/LESSONS.md`** — read every lesson tagged to the area you are about to touch,
   and every open lesson. PROTOCOL §4 names this the easiest step to skip and the one that stops
   the harness repeating itself. Skipping it is how a fixed problem comes back.
5. **Halts and blockers.** If either is set: summarize and stop. The harness never self-clears a
   halt (PROTOCOL §8).
6. **Drain the human backlog** (`binding.md` §State) — the one moment it is read. Its `## Inbox`
   holds what a human dropped in while the last unit was running. Resolve **every** item here,
   before SELECT: schedule it into a task, a lesson, the improvement queue or a later phase; refuse
   it with an argument; or escalate it. Then move it out of the inbox with an ID and its
   destination, per that file's own rules, and stamp **`Last drained`** with today's date. **An item
   left in the inbox at the end of ORIENT is a defect** — it means the next ORIENT re-reads it with
   less context than this one had, and an inbox that accumulates is a second ledger nobody reads.
   `dev/tests/invariants-gate.py` fails the build on exactly that: an item added before
   `Last drained` and still sitting in the inbox.

   **Then commit the drain, before SELECT — its own commit, on the phase branch.** Not at
   CHECKPOINT. The drain is the one artifact ORIENT is required to _write_, and everything after it
   moves `HEAD`: a branch creation, a `git stash pop`, a `gh pr merge`. An uncommitted drain has
   already been silently reverted once ([[LSN-043]]), and the reverted file passes every gate,
   because an empty inbox with today's date is exactly what a correct drain looks like. Committing
   it here also lands it on the branch it was reasoned on. `invariants-gate.py` fails on a drain
   that is still only in the working tree.

**Resuming a killed session.** If the ledger shows a unit `in-progress` with uncommitted work,
first establish whether that work is sound (build clean + its claimed checks green). Then either
finish it or revert it cleanly. Never build on top of an unverified partial unit.

---

## 2. SELECT — one unit

| Situation                                                        | Unit                                                                |
| ---------------------------------------------------------------- | ------------------------------------------------------------------- |
| A drained backlog item names a **live security regression**      | Fix it — that is the unit. A halt if it needs a human ruling first. |
| Phase has no breakdown file                                      | **Break down the phase** (§3). That is the whole unit.              |
| A unit is `in-progress`                                          | Finish it. Prefer this over starting anything new.                  |
| A drained backlog item is `Priority: now`                        | It is the unit. Say in the ledger which planned task it displaced.  |
| Open lessons over the `binding.md` threshold, or cadence reached | Invoke `harness-improve`. Nothing else this run.                    |
| Phase acceptance appears met                                     | Invoke `harness-milestone`.                                         |
| Otherwise                                                        | The first `todo` task in the breakdown.                             |

**Sizing.** A unit must be implementable, verifiable, and checkpointable in this session with
margin. If it turns out oversized: split it in the breakdown, record the split in the ledger, and
do the first half. Do not carry an oversized unit forward.

---

## 3. PLAN — phase breakdown (only when entering a phase)

Write the phase breakdown file (`binding.md` §State). For each task record:

- what to build;
- the spec sections it implements (doc + §);
- the files it will touch;
- **the check IDs from `docs/design/09-verification-and-validation.md` §6 that prove it**, each
  with its level and gate class;
- weight.

Then bind **every phase acceptance bullet to at least one check ID**. An acceptance bullet with no
check is a planning defect — resolve it now by naming an existing check, or by opening a lesson if
none exists. Do not proceed with an unbound bullet.

Record the phase ratchet (09 §10) for this phase — the suites newly required at its end. Mirror the
task list into the ledger.

---

## 4. IMPLEMENT

- Work on the phase branch (`binding.md` §Branching).
- Ground new code on existing patterns in the repo rather than inventing a parallel shape.
- If a spec is genuinely silent, pick the simplest option consistent with every invariant and record
  it in the ledger's decisions table. If a spec is genuinely **contradictory**, that is a halt
  (PROTOCOL §8.5) — do not pick a side.
- Before verifying: run the project's build, format, and lint (`binding.md` §Build).

**Guardrails you will actually hit here** (PROTOCOL §10):

- A check fails and the smallest diff to green is editing the check, a threshold, or the spec.
  **That is a different unit of work.** Diagnose, record the finding, and take it to
  `harness-improve`. Never change a check in the same unit as the implementation whose failure
  motivated the change.
- If the check in question is **BLOCKING-ALWAYS** (V-CTN, V-BRK, V-REV, V-ISO, V-ADV, V-MET),
  weakening it is a **halt for human review**, however good the argument.
- Retire, never delete: an obsolete check keeps its ID with status `RETIRED` and a pointer to its
  replacement, which must exist first.
- A destructive test outside a sanctioned ephemeral target is a halt, not a judgement call.

---

## 5. VERIFY

Invoke **`harness-verify`** with the unit's claimed check IDs. It selects, runs at level, checks
environment preconditions, and writes evidence.

On failure: fix and re-run. Do not advance, and do not record a partial result as a pass. **Three
failures on the same unit with no new information is a halt** (PROTOCOL §8.7) — grinding past that
point means the diagnosis is wrong.

**A mutation sweep is configured, not authored.** When a unit demonstrates that its new check is
non-vacuous, the sweep runs through **`dev/mutate.py`** against a spec committed under
`verification/mutants/<CHECK-ID>.json`. Never a throwaway driver in `/tmp`, and never a hand-rolled
shell loop. Three lessons in four units came from re-authoring that layer each time — a snapshot
keyed by basename that restored the wrong file over the other ([[LSN-047]]), a `-run` pattern that
matched nothing and scored three unevaluated mutants as survivors ([[LSN-048]]), and a needle
containing `""` that closed a `bash -c` string so the applier died and its 0 was read as the suite
passing ([[LSN-049]]). `dev/mutate.sh` is still the right tool for a one-off "break this, run that,
put it back"; it is the layer below, and it cannot see any of the three.

Each row names the test that must fail, and `rc != 0` is not a catch. Three verdicts: `caught`,
`ESCAPED`, `BROKEN`. **A `BROKEN` row is not a finding** — it is the sweep saying it could not
evaluate the mutant. Strengthening a test against one produces a test that passes on the first run,
looks exactly like the fix, and leaves the mutant unmeasured.

---

## 6. CHECKPOINT

A unit is done only when all four hold (PROTOCOL §3):

1. Build, format, and lint pass.
2. Every claimed check ID ran and is green, each with an `evidence_ref`.
3. The ledger is updated: task status, verification log rows, decisions, any lesson opened.
   If this unit closed a scheduled backlog item, that item moves to `## Done` in
   `docs/build/BACKLOG.md` with what it landed as.
4. Work is committed on the phase branch, Conventional Commits, scoped staging.

Then stop. If the phase is now complete, hand to **`harness-milestone`** — do not merge from here.

---

## 7. Halt conditions (PROTOCOL §8)

Stop and surface. Do not retry around, do not switch to other work, do not clear it yourself.

1. A BLOCKING-ALWAYS check fails or cannot run.
2. A previously-green suite goes red.
3. A change would violate an invariant.
4. A destructive operation targets anything but a sanctioned ephemeral environment.
5. A spec contradiction with no invariant-preserving resolution.
6. A change that would weaken a spec, check, or gate in a way §10 forbids.
7. The same unit fails verification three times with no new information.
8. Resource or credential exhaustion, or an unrebuildable environment.

Record in the ledger: trigger, what was tried, believed cause, and **the narrowest question a human
could answer to unblock it**.

---

## 8. Every invocation must end in one of these

A completed unit · a recorded halt · a recorded lesson · a completed improvement pass. Ending with
none of these is itself a defect — open a lesson saying so (PROTOCOL §9).

A clean stop mid-phase is free. An unclean stop mid-unit costs a session. Checkpoint early.
