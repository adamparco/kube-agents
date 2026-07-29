# The kube-agents build harness

An autonomous build harness that turns the design set in `docs/design/` (01–09) into a **verified
implementation**, phase by phase, across many short sessions with no human in the loop between
milestones. It is not a program — it is a protocol, a project binding, four skills, a lesson store,
and a ledger that holds all the state.

Read [`PROTOCOL.md`](PROTOCOL.md) for the state machine and the contract, and
[`SELF-IMPROVEMENT.md`](SELF-IMPROVEMENT.md) for how it learns and why it cannot cheat. This page is
only the entry point.

---

## The four skills

| Skill                    | Use it to                                                                                                                             |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------- |
| **`/harness-run`**       | Drive the build forward by exactly **one bounded unit** — orient, select, implement, verify, checkpoint. The default invocation.      |
| **`/harness-verify`**    | Run conformance checks **by stable ID** at their assigned level, assert environment preconditions, record one evidence row per check. |
| **`/harness-milestone`** | Close a **completed phase** — full gate, PR with the verification table, merge, advance the ledger. Never for partial progress.       |
| **`/harness-improve`**   | Run the self-improvement pass — mechanize open lessons, investigate escapes, review deferrals, prune within the ratchets.             |

`harness-run` calls the others; you rarely need to invoke them directly.

## File map

| File                                                  | Role                                                                                                                                     |
| ----------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| [`PROTOCOL.md`](PROTOCOL.md)                          | The state machine, the unit of work, halt conditions, milestone rules. Project-agnostic                                                  |
| [`binding.md`](binding.md)                            | **The project binding** — specs, state paths, gates, build/test commands, targets, preconditions, branching, merge, schedule, thresholds |
| [`invariants.md`](invariants.md)                      | The pre-merge gate: the six load-bearing rules plus the conversion-ordering checks                                                       |
| [`LESSONS.md`](LESSONS.md)                            | The durable lesson store — every mistake this repo has already paid for, and what now catches it                                         |
| [`SELF-IMPROVEMENT.md`](SELF-IMPROVEMENT.md)          | Lesson lifecycle, mechanization, anti-reward-hacking, metrics, the improvement pass                                                      |
| `verify-phase.workflow.js`                            | Optional parallel fan-out of the phase suites; requires explicit opt-in                                                                  |
| `../skills/harness-*/SKILL.md`                        | The four skills above                                                                                                                    |
| `../../docs/build/LEDGER.md`                          | **All state.** Read first, written last, every run                                                                                       |
| `../../docs/build/phase-<N>.md`                       | The current phase's task breakdown, tasks bound to check IDs                                                                             |
| `../../docs/build/BACKLOG.md`                         | **The human inbox.** Append findings here any time, even mid-unit; drained at the next ORIENT                                            |
| `../../docs/design/09-verification-and-validation.md` | The conformance spec: every check ID, level, gate class, and the phase ratchet                                                           |

## Running it

```
/harness-run          # one unit of work, then checkpoint
/harness-verify       # run the current phase's checks by ID and record evidence
/harness-milestone    # only when a phase's acceptance is fully green
/harness-improve      # mechanize open lessons; never mixed with feature work
```

**Autonomously:** a durable Claude Code cron re-enqueues `/harness-run` on an interval — one firing,
one unit, one checkpoint. See `binding.md` §Schedule for the cadence and the re-arm procedure.

**Pausing and stopping.** Delete the scheduled task; nothing self-triggers. Any session can be
killed at any moment: all state is in the ledger, and the next invocation resumes from it. A halt is
recorded in the ledger and **only a human clears it** — the harness will not work around one, retry
past it, or quietly pick up something else instead.

## Safety posture

The harness holds real credentials and can merge its own work, so its bounds are mechanical rather
than advisory: destructive tests run only against an **anchored** allow-list of ephemeral targets
(`kind-*`, `gke-scratch-*`) and anything else halts; the pre-merge gate in `invariants.md` must pass
before any PR opens; required checks are never bypassed (`--admin` and `--no-verify` are forbidden);
BLOCKING-ALWAYS checks may not be deferred, quarantined, retried to green, or weakened, and any
attempt to relax one is a halt for human review; a `pass` with no evidence reference is recorded as
`skipped`; and the assertion and coverage ratchets mean the suite can only ever grow. The one thing
the harness must never do is make a run go green by lowering the bar — everything above exists to
make that expensive and visible rather than easy and quiet.
