---
name: harness-milestone
description: Close a completed phase — run the full pre-merge gate (phase acceptance, phase ratchet, regression, invariants), confirm no BLOCKING-ALWAYS check is deferred or quarantined, confirm the assertion and coverage ratchets held, open the PR with the verification table, merge per binding.md, and advance the ledger to the next phase. Use only when a phase's acceptance criteria are all green — never for "enough progress", a single task, or a partial phase.
---

# harness-milestone — commit, gate, merge, advance

Executes `.claude/harness/PROTOCOL.md` §7. Branch, merge, and PR conventions come from
`.claude/harness/binding.md` §Branching and §Merge.

---

## 1. Is this actually a milestone?

A milestone is **a completed phase**. Not a completed task, not "enough progress to be worth
saving". Precisely, all four:

1. Every phase acceptance bullet's bound check IDs are green at their required level.
2. The phase ratchet (09 §10) is green.
3. Regression is clean — every suite green at the end of the previous phase is still green.
4. `invariants.md` passes.

If any is unproven, **stop and return to `harness-run`**. Committing per unit on the phase branch is
normal; merging is the milestone event and only that.

---

## 2. Run the full gate — before proposing any merge

In this order.

1. **Phase acceptance.** Invoke `harness-verify` for every check ID bound to an acceptance bullet.
   An unbound acceptance bullet is a planning defect: bind it and run the check, or halt.
2. **Phase ratchet + all prior ratchets.** 09 §10. A suite that entered the ratchet never leaves.
3. **Regression sweep.** All previously-green suites. A regression is a halt (PROTOCOL §8.2), not a
   note.
4. **Invariants checklist.** Every item PASS or a justified N-A, each with one line of evidence —
   a file, a test name, or command output. Invariant 2 (scope is absolute) never goes red, not for
   one commit and not for one phase.
5. **Format and lint every changed file** (`binding.md` §Build).

### 2.1 Then confirm the things a green run can still hide

| Confirm                                                                        | Because                                                      |
| ------------------------------------------------------------------------------ | ------------------------------------------------------------ |
| **No BLOCKING-ALWAYS check is deferred, quarantined, or skipped**              | It may not be deferred at all (09 §9.6); a skip is V-MET-007 |
| Every `pass` carries an `evidence_ref`                                         | A pass without one is `skipped` (09 §9.4)                    |
| **Assertion ratchet did not fall** — V-MET-003                                 | Silent suite shrinkage during a conversion (09 §11.7)        |
| **Coverage ratchet did not fall** — V-MET-002 / V-MET-008                      | Load-bearing suites must be at full coverage                 |
| Retirements name a replacement that already exists — V-MET-004                 | Retire, never delete                                         |
| Every deferral has a named blocker, owner, and promotion condition — V-MET-006 | Deferred-read-as-done (09 §11.8)                             |
| Every check declares a negative control or an argued exemption — V-MET-014     | A suite of vacuous passes reads green                        |
| No check, threshold, or spec was weakened inside a feature unit                | PROTOCOL §10.1 — check the phase diff for it explicitly      |

Any of these failing means the milestone is not done.

---

## 3. Improvement pass

Invoke **`harness-improve`** before opening the PR. Its inputs include the diff of the phase just
completed, and a mechanization landing after the merge loses that context.

---

## 4. Open the PR

Use the repository's PR template (`binding.md` §Merge). Never `gh pr create --fill`. Contents:

1. **Phase summary** — goal, what shipped, what changed in the system's capability.
2. **Verification table** — one row per check ID: `check_id | level | target | result |
evidence_ref`. Include the ratchet and regression rows, not only the new work.
3. **Decisions** — every spec-silent choice made and its rationale; every spec edit and why it is
   correctness rather than convenience.
4. **Deferrals** — every deferred check with its **named blocker**, owner, and promotion condition.
   None may be BLOCKING-ALWAYS.
5. **Retirement pairs** — any check retired, with its replacement ID, per "tests are replaced, never
   deleted" (invariants §8).
6. **Metrics snapshot** — escape rate, rework, halts by cause, open lessons, coverage.

Push to the fork, not upstream. Stage only targeted files.

---

## 5. Merge

Follow `binding.md` §Merge exactly.

**The absolute rule: a red required check means the milestone is not done.** Fix it or halt.

- Never bypass a required check.
- Never force-merge, never `--admin`, never disable a gate to get through it.
- Never merge with an unexplained deferral, a missing evidence reference, or a weakened check.

Forcing a merge past a gate is the single change that converts a slow build into an untrustworthy
one. If a required check looks wrong, that is a `harness-improve` unit and a separate PR — not a
reason to merge this one.

---

## 6. Advance the ledger

Close the phase:

- Phase marked ✅, with the merge commit / PR URL recorded.
- Final verification table attached to the phase entry.
- Ratchet extended with the suites this phase added — recorded as permanent.
- Coverage and assertion baselines updated to the new (higher) values.
- Metrics snapshot recorded.
- Deferrals carried forward with their blockers.

Open the next phase:

- Next phase set as current, status 🟡, with no breakdown yet.
- Current unit cleared. `harness-run`'s next invocation will select "break down the phase".
- New phase branch per `binding.md` §Branching.

Then stop. Do not start the next phase's work in this invocation.

---

## 7. Halt instead of merging if

- Any BLOCKING-ALWAYS check is red, deferred, quarantined, or did not run.
- A regression appeared.
- An invariant would be violated.
- A ratchet fell.
- The only route to a green gate is changing a check, a threshold, or a spec.
