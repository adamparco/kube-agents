# Build state

This directory holds the **state** of the autonomous build of kube-agents. The harness that produces
it lives in [`.claude/harness/`](../../.claude/harness/) — start at
[`.claude/harness/README.md`](../../.claude/harness/README.md).

| File                     | Role                                                                                                                                                                                                                                      |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [`LEDGER.md`](LEDGER.md) | **The single source of truth for build progress.** Read first, written last, on every run. Status, phase table, verification results by check ID, metrics, lessons pointer, deferrals, decisions, blockers, and the history of Phases 0–7 |
| `phase-<N>.md`           | The task breakdown for phase _N_, written on entering the phase; each task names the spec sections it implements and the **check IDs** that prove it                                                                                      |
| `phase-5/`, `spikes/`    | Supporting notes from earlier phases                                                                                                                                                                                                      |
| `../../verification/`    | Generated: the per-run evidence manifest (09 §9.4) and `traceability.yaml` (V-MET-011)                                                                                                                                                    |

## How the build runs

```
ORIENT ─▶ SELECT ─▶ [PLAN] ─▶ IMPLEMENT ─▶ VERIFY ─▶ CHECKPOINT
                                              │           │
                                              └── fail ───┘   (fix, re-verify; no advance)
   phase complete ─▶ REGRESS ─▶ MILESTONE ─▶ next phase
   blocker ────────────────────────────────▶ HALT (human only)
```

One invocation of `/harness-run` performs **one bounded unit of work** and checkpoints the ledger —
it does not try to finish a phase in one session. A unit is done only when its code builds, every
check ID it claims is green **with an evidence reference**, the ledger is updated, and the work is
committed on the phase branch. A phase closes via `/harness-milestone`, which runs the full gate
(phase acceptance + the 09 §10 ratchet + regression + `invariants.md`) before any PR opens.

The state machine, halt conditions, and merge rules are in
[`.claude/harness/PROTOCOL.md`](../../.claude/harness/PROTOCOL.md). Every project-specific value —
which cluster, which command, which remote, which threshold — is in
[`.claude/harness/binding.md`](../../.claude/harness/binding.md), not here and not in a skill.

## What decides "done"

- **What to build:** `docs/design/` 01–09. Never contradict a spec; if a spec is genuinely silent,
  pick the simplest option consistent with the invariants and record it under **Decisions &
  deviations** in the ledger.
- **In what order:** [`docs/design/07-implementation-roadmap.md`](../design/07-implementation-roadmap.md)
  §2 (phases 8–15 and their acceptance criteria), §3 (Definition of Done), §5 (the verification loop).
- **Whether it is proven:** [`docs/design/09-verification-and-validation.md`](../design/09-verification-and-validation.md)
  is the authoritative conformance spec — every check has a stable ID (`V-<SUITE>-<nnn>`), a level
  (L0 static → L4 soak), and a gate class. The harness runs checks by ID; it does not invent tests.
- **Whether it may merge:** [`.claude/harness/invariants.md`](../../.claude/harness/invariants.md).

## Safety posture

- **Destructive tests** (chaos, deliberately-bad RBAC, brokered destructive actions) run **only**
  against an ephemeral target matched by an **anchored** pattern — `kind-*` or `gke-scratch-*`.
  Anything else is a halt, not a judgement call.
- **Load-bearing suites halt the build.** V-CTN, V-BRK, V-REV, V-ISO, V-ADV and V-MET are
  BLOCKING-ALWAYS: a failure stops everything, and they may not be deferred, quarantined, retried to
  green, or weakened.
- **Deferred, never faked.** A check that cannot run is recorded `deferred` with a named blocker; a
  `pass` with no evidence reference is recorded as `skipped`.
- **PRs, not direct pushes.** Conventional Commits, the PR template, scoped staging, format against
  the base branch, push to the fork. Required checks are never bypassed — no `--admin`,
  no `--no-verify`.
- **A halt is cleared by a human.** The harness records what it tried, what it believes the cause is,
  and the narrowest question that would unblock it.

## Prior generation

Phases 0–7 built the **read-only** generation — agents that proposed GitOps PRs and held no write
verb. Their history, verification results, decisions and the first live-install findings are
preserved in `LEDGER.md`. On 2026-07-24 the model was inverted to **imperative agents that act**
through a per-scope Action Broker; the roadmap restarts at Phase 8 and `invariants.md` was
re-derived. The old results stay in the ledger as history — they are where most of
[`LESSONS.md`](../../.claude/harness/LESSONS.md) came from.
