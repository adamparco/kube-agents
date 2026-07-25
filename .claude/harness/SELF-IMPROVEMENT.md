# Self-improvement

How the harness learns, how that learning is made durable, and — the larger half — why it cannot
improve its numbers by lowering the bar.

Companion to [`PROTOCOL.md`](PROTOCOL.md). The lesson store is [`LESSONS.md`](LESSONS.md).

---

## 1. The problem this solves

An autonomous build that runs for weeks will make the same class of mistake repeatedly unless
something stops it. Two failure modes, both observed in this repository's own history:

- **Amnesia.** A lesson is learned, written down in prose, and forgotten. The stale-image trap in
  `LESSONS.md` was recorded three separate times before it became a mechanical precondition.
- **Self-deception.** The build reports green while the property is broken. Every entry in the
  conformance spec's anti-false-green section is a real instance of this.

The response to the first is **mechanization**: a lesson that ends as prose does not count. The
response to the second is **§4**, which is the more important section of this document.

---

## 2. The lesson lifecycle

A lesson is opened by any of: a halt, a failed verification that required rework, a false green
discovered later, a spec contradiction, a surprising environment behaviour, or a review finding.

| Stage          | What happens                                                                                                                                                          |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Trigger**    | Record what happened, verbatim enough to recognise it again. Include the symptom, not just the diagnosis.                                                             |
| **Root cause** | Why it happened — one level below the symptom. "The test failed" is a symptom; "the check ran at L0 against a substrate that cannot enforce the property" is a cause. |
| **Generalize** | State the class. A lesson about one stale image is worth little; "a same-tag image is not evidence of the build under test" is worth a great deal.                    |
| **Mechanize**  | Convert it into something that cannot be forgotten (§3). **Mandatory.**                                                                                               |
| **Verify**     | Prove the mechanization works by reproducing the original failure and watching it get caught.                                                                         |
| **Close**      | Record the mechanization's ID. A lesson without one stays **open** and appears in every orientation.                                                                  |

**An open lesson is a defect in the harness**, tracked like any other. The count of open lessons is
a health metric (§6), and a lesson that stays open across three improvement passes is escalated to a
human — it means the harness cannot fix itself here.

---

## 3. Mechanization taxonomy

A lesson must terminate in exactly one of these. The first four are real mechanizations; the fifth is
an escape hatch that must be argued, and is itself reviewed.

| Form                     | Use when                                                                        | Durable artifact                                      |
| ------------------------ | ------------------------------------------------------------------------------- | ----------------------------------------------------- |
| **New or changed check** | The failure is a property that should have been asserted                        | A check ID in the conformance spec                    |
| **Gate rule**            | The failure is a class of change that should be blocked pre-merge               | An item in `invariants.md`                            |
| **Precondition**         | The failure was environmental — a result that could not be trusted              | An entry in `binding.md` §Preconditions               |
| **Skill change**         | The failure was procedural — the harness did the wrong thing in the right order | An edit to a skill under `.claude/skills/`            |
| **Spec tightening**      | The requirement was unfalsifiable, so no check could exist                      | A tightened statement + the check it unblocks         |
| _(Refusal)_              | No mechanization is possible                                                    | A written argument for why, reviewed at the next pass |

**Prefer the earliest and cheapest form that actually catches it.** A precondition that runs in
seconds beats a check that runs in an hour; a gate rule that blocks the change beats a check that
detects it afterwards. But never trade _away_ the level at which the property is genuinely provable
— that is the §4 failure.

---

## 4. Anti-reward-hacking

The harness's objective is "checks are green". Every route to that objective that does not involve
the system actually working is a hack, and an autonomous agent under time pressure will find them.
They are enumerated here because a hack you have named is a hack you can detect.

| The hack                                                               | Why it is tempting                         | Rule                                                                                                  | Detection                                             |
| ---------------------------------------------------------------------- | ------------------------------------------ | ----------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| **Weaken the assertion** — loosen a threshold, drop a condition        | Smallest diff that turns red green         | Never in the same unit as the failing implementation; never at all for BLOCKING-ALWAYS (PROTOCOL §10) | Assertion ratchet; diff review of check files         |
| **Narrow the scope** — make the check test less                        | Looks like a refinement                    | A scope reduction must name what now covers the remainder                                             | Coverage ratchet; the uncovered list                  |
| **Demote the level** — prove at L0 what needs L2/L3                    | Fast, and the check still "passes"         | The level is a property of the requirement, not of convenience                                        | Level is recorded per check; a demotion is a diff     |
| **Mark it deferred** — reclassify a failure as a blocker               | Deferral is legitimate, so this hides well | A deferral needs a **named external blocker**; BLOCKING-ALWAYS may never be deferred                  | Deferrals are listed every run with their blockers    |
| **Retry to green** — run it again until it passes                      | Flakes are real, so this feels reasonable  | Security and safety checks are **never** retried to green                                             | Retry counts recorded; a retried check is quarantined |
| **Delete the negative control** — keep the happy path                  | The suite still looks complete             | Every check declares a negative control or an argued exemption                                        | The negative-control lint                             |
| **Redefine the requirement** — edit the spec so the behaviour conforms | Feels like "clarifying"                    | Spec edits are for correctness, not convenience (PROTOCOL §10.5)                                      | Every spec edit carries a rationale; weakenings halt  |
| **Stub the dependency** — replace the real substrate with a fake       | Removes environmental flakiness            | A check's substrate is part of its definition                                                         | Environment preconditions; evidence references        |
| **Claim without evidence** — record a pass with no artifact            | Nothing visibly breaks                     | A pass with no evidence reference is recorded as `skipped`                                            | Evidence-completeness lint                            |

**The structural defence.** Notice that all nine are detectable by artifacts the harness already
produces: the ratchets, the deferral list, the evidence references, and the diff. That is not an
accident — it is why those artifacts are mandatory rather than nice to have. A harness that stopped
producing them would be unable to police itself, which is why an invocation that skips them is a
halt and not a shortcut.

**The honest limit.** None of this prevents a _wrong but well-evidenced_ result — a check that
genuinely passes against an implementation that is subtly wrong. That is what escape rate (§6) is
for: it measures the thing that no individual check can see about itself.

---

## 5. Pruning, and the ratchets that bound it

The harness may remove things. Left unpruned, a long build accumulates redundant checks, stale
lessons, and spec text describing behaviour that no longer exists — and all three make it slower and
harder to reason about. But pruning is also indistinguishable from weakening unless it is bounded.

**What may be pruned:**

| Candidate     | Condition                                                                                        |
| ------------- | ------------------------------------------------------------------------------------------------ |
| A **check**   | Genuinely subsumed by another check that is named in the retirement, at the same or higher level |
| A **lesson**  | Mechanized, and the mechanization has held for several phases                                    |
| **Spec text** | Describes removed behaviour, or duplicates a statement that has a single owner elsewhere         |
| A **fixture** | Its rule no longer exists, and no check references it                                            |

**The ratchets, which are mechanical and non-negotiable:**

1. **Assertion ratchet.** The count of security assertions never falls. A retirement must name its
   replacement, and the replacement must exist first.
2. **Coverage ratchet.** Requirement coverage never falls below the recorded baseline. The
   load-bearing suites must be at full coverage and stay there.
3. **Retire, never delete.** A retired check keeps its ID and points at its successor, so a later
   reader can tell the difference between "we decided this differently" and "this was lost".

Pruning happens **only in an improvement pass** (§7), never opportunistically mid-implementation.
The separation matters: pruning while trying to make something pass is precisely the situation in
which judgement is worst.

---

## 6. Metrics

The harness measures itself, because "it feels like it is going well" is not evidence, and because
several of the hacks in §4 show up as metric anomalies before anyone notices them in a diff.

| Metric             | Definition                                                                             | What a bad trend means                                                      |
| ------------------ | -------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| **Escape rate**    | Defects found in phase _N_ that a check from a phase **before** _N_ should have caught | **The primary signal.** Rising ⇒ checks are passing on broken things        |
| **Rework rate**    | Units requiring more than one implement→verify cycle                                   | Rising ⇒ planning or breakdown is too coarse                                |
| **Halt rate**      | Halts per phase, by cause                                                              | Rising ⇒ the specs or the environment are underspecified                    |
| **Open lessons**   | Lessons with no mechanization                                                          | Rising ⇒ learning is being recorded but not applied                         |
| **Deferral count** | Checks deferred, by blocker                                                            | Rising ⇒ verification is drifting from reality                              |
| **Coverage**       | Requirements with ≥1 check, per suite                                                  | Falling ⇒ ratchet breach; flat while phases advance ⇒ new work is unchecked |
| **Cycle time**     | Wall-clock per unit and per phase                                                      | Useful context, never a target — optimising it directly invites §4          |

**Escape rate is the only metric that can detect a check that lies**, and it is therefore the one to
watch. It is also lagging by construction: an escape is only visible once something later trips over
it. When an escape is found, the lesson's mechanization must strengthen **the check that should have
caught it**, not merely add a new one at the point of discovery — otherwise the original blind spot
survives.

Metrics are recorded in the ledger at each milestone. They are inputs to the improvement pass, not
targets to optimise.

---

## 7. The improvement pass

A dedicated unit of work — never mixed with feature work — run at each milestone, whenever a halt is
cleared, and whenever open lessons exceed the threshold in `binding.md`.

**Inputs:** open lessons, the metrics above, the deferral list, the uncovered-requirements list,
recent halts, and the diff of the phase just completed.

**Procedure:**

1. **Mechanize** every open lesson, or argue a refusal (§3). This comes first; it is the point.
2. **Investigate escapes.** For each, identify the check that should have caught it and strengthen
   _that_ check.
3. **Review deferrals.** Any whose blocker has cleared is now runnable; any whose blocker is stale is
   re-examined.
4. **Prune** under §5.
5. **Refine the harness.** If a procedure went wrong twice, the skill is wrong — fix the skill, not
   the instance.
6. **Refine the specs.** Contradictions found, detail discovered, statements found unfalsifiable.
   Under PROTOCOL §10.5 — correctness, not convenience.
7. **Record** the pass in the ledger: what was mechanized, pruned, and changed, with the metrics
   snapshot that motivated it.

**A pass that changes nothing is a valid outcome** — but two in a row while escapes are non-zero is
itself a lesson, because it means the harness is no longer able to see its own failures.

---

## 8. Worked example

The real one, from this repository, end to end:

- **Trigger.** A namespace-isolation escape was admitted on a live cluster while the source code and
  unit tests were correct.
- **Root cause.** The deployed operator image predated the commit under test. A same-tag image with
  `imagePullPolicy: IfNotPresent` is not refreshed, so the cluster ran old admission logic.
- **Generalize.** A deployed artifact is not evidence of the build under test unless its identity is
  verified. Applies to every image, policy, and CRD, not just this operator.
- **Mechanize.** Two artifacts: an environment **precondition** that deployed digests must match the
  build before any live result is trusted, and a **check** asserting it. Recorded as an
  anti-false-green rule so the reasoning survives the people who learned it.
- **Verify.** Reproduce by deploying a stale image and confirm the precondition now fails the run.
- **Close.** Lesson references the precondition and the check ID.

Note the shape: the lesson did **not** close as "remember to rebuild the image". That phrasing had
already been written down twice and forgotten twice. It closed as something that fails the build.
