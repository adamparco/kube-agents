---
name: harness-improve
description: Run the harness self-improvement pass — mechanize every open lesson into a check, gate rule, precondition, skill edit or spec tightening; investigate escapes by strengthening the check that should have caught them; review deferrals; prune within the ratchets; refine skills and specs. Use at a milestone, after a halt is cleared, when open lessons exceed the binding.md threshold, or when the same procedural mistake has happened twice. Runs as its own unit — never mixed with feature work.
---

# harness-improve — the self-improvement pass

Executes `.claude/harness/SELF-IMPROVEMENT.md` §7. **This is a whole unit of work on its own.** Do
not mix it with feature work: pruning and threshold changes made while trying to get something to
pass are exactly the situation in which judgement is worst (SELF-IMPROVEMENT §5).

---

## 1. When to run

- At every milestone (invoked by `harness-milestone`).
- Whenever a halt has just been cleared by a human.
- Whenever open lessons exceed the threshold in `binding.md`.
- Whenever the same procedure has gone wrong twice — the skill is wrong, not the instance.

---

## 2. Gather inputs first

From the ledger (`binding.md` §State) and `.claude/harness/LESSONS.md`:

1. **Open lessons** — every lesson with no mechanization ID.
2. **Metrics** (SELF-IMPROVEMENT §6): escape rate, rework rate, halt rate by cause, open lessons,
   deferral count, coverage, cycle time.
3. **The deferral list**, each with its blocker, owner, and promotion condition.
4. **The uncovered-requirements list** (V-MET-009 publishes it; a count is not enough).
5. **Recent halts**, with what unblocked them.
6. **The diff of the phase just completed.**

Metrics are inputs, never targets. Optimising cycle time directly invites SELF-IMPROVEMENT §4.

---

## 3. Procedure — in this order

### 3.1 Mechanize every open lesson

This comes first; it is the point. A lesson that ends as prose does not count — the stale-image trap
was written down three times before it became a precondition.

| Form                     | Use when                                                   | Durable artifact                            |
| ------------------------ | ---------------------------------------------------------- | ------------------------------------------- |
| **New or changed check** | A property should have been asserted                       | A check ID in 09 §6                         |
| **Gate rule**            | A class of change should be blocked pre-merge              | An item in `invariants.md`                  |
| **Precondition**         | The result could not be trusted for environmental reasons  | An entry in `binding.md` §Preconditions     |
| **Skill change**         | The harness did the right things in the wrong order        | An edit under `.claude/skills/`             |
| **Spec tightening**      | The requirement was unfalsifiable, so no check could exist | Tightened statement + the check it unblocks |
| _(Refusal)_              | No mechanization is possible                               | A written argument, reviewed next pass      |

Prefer the **earliest and cheapest form that actually catches it** — a precondition beats a check;
a gate rule that blocks the change beats a check that detects it after. But never trade away the
level at which the property is genuinely provable; that trade is the §4 failure.

Then **verify the mechanization**: reproduce the original failure and watch it get caught. Close the
lesson only with a mechanization ID, or an argued refusal. Anything else stays **open** and appears
in every orientation. A lesson open across three passes escalates to a human.

### 3.2 Investigate escapes

An escape is a defect found in phase N that a check from a phase **before** N should have caught. It
is the only metric that can detect a check that lies.

For each: identify **the check that should have caught it, and strengthen that check.** Adding a new
check at the point of discovery leaves the original blind spot intact and will read as progress.
Record both the original check ID and what changed about it.

### 3.3 Review deferrals

- Blocker cleared → the check is runnable now. Schedule it via `harness-verify`.
- Blocker stale or vague → re-examine. A deferral with no live external blocker is a failure wearing
  a different label.
- Any BLOCKING-ALWAYS check appearing in the deferral list → **halt**. That may not exist.
- Deferrals blocked on a 09 §12 tightening: decide whether this pass can resolve the tightening
  (spec work, §3.6) and unblock the `†` check.

### 3.4 Prune — bounded by the ratchets

Only here, never opportunistically.

| Candidate | Condition                                                                                    |
| --------- | -------------------------------------------------------------------------------------------- |
| Check     | Genuinely subsumed by another check **named in the retirement**, at the same or higher level |
| Lesson    | Mechanized, and the mechanization has held for several phases                                |
| Spec text | Describes removed behaviour, or duplicates a statement owned elsewhere                       |
| Fixture   | Its rule no longer exists and no check references it                                         |

Ratchets, mechanical and non-negotiable:

1. **Assertion ratchet** — the count of security assertions (V-CTN, V-BRK, V-REV, V-ADV) never
   falls. The replacement must exist **before** the retirement.
2. **Coverage ratchet** — coverage never falls below the recorded baseline; load-bearing suites stay
   at full coverage.
3. **Retire, never delete** — a retired check keeps its ID, gains `status: RETIRED`, and points at
   its successor.

Re-run V-MET after pruning. If a ratchet fell, revert the prune.

### 3.5 Refine the harness

If a procedure went wrong twice, fix the **skill**, not the instance. Skill edits under
`.claude/skills/` are a first-class mechanization form — name the lesson they close.

### 3.6 Refine the specs

Under PROTOCOL §10.5 — **correctness, not convenience**. Legitimate: resolving a contradiction,
adding detail discovered during implementation, recording a decision, tightening something
unfalsifiable (09 §12 is a standing list of these). Illegitimate: making a requirement vaguer so it
stops failing. Every spec edit carries a rationale in the ledger. A weakening is a halt, and the
**attempt** is recorded even when refused — a refused weakening is a signal about either the spec or
the implementation.

### 3.7 Record the pass

In the ledger: what was mechanized (lesson → mechanization ID), what was pruned (with replacements),
what changed in skills and specs, refusals with their arguments, and the metrics snapshot that
motivated it all.

---

## 4. A pass that changes nothing

Valid outcome — record it as such. But **two in a row while escape rate is non-zero is itself a
lesson**: it means the harness can no longer see its own failures. Open that lesson explicitly and
mechanize it like any other.

---

## 5. Standing prohibitions during a pass

- No implementation work. If a mechanization needs code, it becomes a task in the current phase
  breakdown, not something done here.
- No change that removes, relaxes, or narrows a BLOCKING-ALWAYS check — halt for human review, no
  matter how well argued (PROTOCOL §10.2).
- No threshold or check change motivated by a currently-failing implementation. That coupling is the
  rule-1 violation the separation of this pass exists to prevent.
