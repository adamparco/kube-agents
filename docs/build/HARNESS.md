# The Coding Harness

A small, reusable pattern for driving a large software build **autonomously, phase by phase, from a
written design set** — using Claude Code skills, a persistent ledger, and a verification loop instead
of a bespoke program.

This document is self-contained: it explains what the harness is, how it works, why it's safe to run
unattended, what it produced, and how to adapt the same pattern to another spec-driven project.

---

## 1. What it is

The kube-agents harness is **not a separate application**. It is four ordinary Claude Code artifacts
wired into a loop:

1. **A design set** — the source of truth (`docs/design/` 01–08), including a roadmap that splits the
   build into phases, each with explicit **Accept** criteria and **Verification** suites.
2. **Skills** — reusable prompts (`/harness-run`, `/harness-verify`) that encode the per-phase loop
   and the verification procedure.
3. **A ledger** — a single Markdown file (`docs/build/LEDGER.md`) that is the harness's memory:
   current phase, task status, verification log, decisions, deviations, blockers, halt conditions.
4. **A gate** — an invariants checklist (`.claude/harness/invariants.md`) every change must pass
   before it can merge.

A schedule (a durable Claude Code cron task) re-enqueues the loop on an interval so the build
progresses across days without a human in the seat. Everything else is just Claude Code doing normal
engineering: branches, Conventional Commits, prettier/build, PRs, CI.

The key idea: **state lives in files, not in a conversation.** Any session — day 1, day 5, a fresh
context window — reads the ledger and continues correctly. The harness is resumable because its
memory is durable.

---

## 2. The loop

Each invocation of `/harness-run` does **one coherent unit of work** and then checkpoints:

```
   read LEDGER.md  ──▶  pick next unit of work
        │
        ▼
   break down ─▶ (detailed design) ─▶ implement ─▶ verify ─▶ regress ─▶ gate ─▶ PR
      │  read the phase's Work items + referenced spec sections
      │  code on a branch; prettier / build; Conventional Commits
      │  run phase Accept criteria + the touched specs' Verification suites
      │  re-run prior-phase Accept + the load-bearing suites (no regressions)
      │  run the invariants checklist; every item PASS or justified N-A
      └─▶ iterate until green ─▶ update LEDGER ─▶ open PR ─▶ advance phase
```

The seven steps, as encoded in `harness-run`:

| Step             | What happens                                                                                     |
| ---------------- | ------------------------------------------------------------------------------------------------ |
| 0. Orient        | Read the ledger, the invariants, and the current phase in the roadmap. Stop if a blocker/halt is open. |
| 1. Pick the unit | First `todo`/`in-progress` task in the current phase breakdown. Keep it finishable in one run.    |
| 2. Break down    | On entering a new phase, expand its **Work** items into individually-verifiable tasks (`P<N>-T1`…). |
| 3. Detailed design | Only for architecturally non-trivial or spec-silent tasks; otherwise skip to implement.        |
| 4. Implement     | Branch, ground new code on existing patterns, format + build, Conventional Commits, scoped staging. |
| 5. Verify        | Run phase **Accept** + touched **Verification** suites on the right target. Evidence required.    |
| 6. Regress       | Re-run prior-phase Accept + the two load-bearing suites. A regression is a halt, not a note.      |
| 7. Gate + checkpoint | Run the invariants checklist, update the ledger, open a PR, advance the phase.                |

**Verification is adversarial.** A negative test that silently no-ops reads as green, so every
"passed" negative test is re-confirmed: did the admission policy actually *deny* (non-zero apply,
explicit policy message), or was the manifest just malformed? "Verified" is never reported without a
command, log, or PR as evidence.

---

## 3. Components and where they live

| Piece               | Path                                        | Role                                                        |
| ------------------- | ------------------------------------------- | ---------------------------------------------------------- |
| Design set          | `docs/design/` (01–08)                      | Source of truth; roadmap in `07-implementation-roadmap.md` |
| Ledger              | `docs/build/LEDGER.md`                       | Persistent build state; read first, updated last every run |
| Phase breakdowns    | `docs/build/phase-<N>.md`                    | Concrete task list for a phase, created on entry           |
| Orchestrator skill  | `.claude/skills/harness-run/SKILL.md`        | The per-phase loop (the 7 steps above)                     |
| Verify skill        | `.claude/skills/harness-verify/SKILL.md`     | Runs Accept + Verification suites, logs results with evidence |
| Invariants gate     | `.claude/harness/invariants.md`              | Load-bearing rules, checked before every merge             |
| Verify workflow     | `.claude/harness/verify-phase.workflow.js`   | Optional parallel fan-out: one agent per suite, then adversarial confirm |

The **verify workflow** is an optimization: instead of running verification suites serially, it
dispatches each suite to its own subagent (`pipeline`, no barrier), then pipes each result into a
second agent that adversarially re-checks it. It returns a per-suite `PASS/FAIL/SKIP` with evidence
and an `allGreen` flag. It requires explicit workflow opt-in; the serial path in `harness-verify`
works without it.

---

## 4. Safety posture — why it's safe to run unattended

The harness advances across phase boundaries on its own. That is only acceptable because of four
guardrails:

- **The product is read-only by design, and so is the build.** Agents never mutate cluster/cloud
  APIs; the only write path is a reviewed PR applied by the customer's CI/CD. The harness holds to the
  same rule — it opens PRs, it does not push to protected branches or force past checks.

- **Destructive tests are geofenced.** Negative-security and chaos tests (deleting agents, killing
  the hub, applying deliberately-bad RBAC) run **only on Kind or an ephemeral scratch cluster** — a
  destructive-test guard confirms the kube context matches `kind-*` / `gke-scratch-*` before any
  delete/kill/bad-apply, and **halts** otherwise. Prod is never a target.

- **Load-bearing halts.** The harness stops and surfaces — rather than auto-advancing — when a
  security negative test (03 §11) or a chaos test (05 §8) fails, when a change would break an
  invariant, when a destructive test is aimed off-target, or when a spec conflict has no
  simplest-option resolution.

- **The invariants gate.** Five rules, checked before every merge, each answered PASS/FAIL/N-A with
  one line of evidence:
  1. **Read-only agents** — no change grants a write verb or write-capable tool/credential.
  2. **All mutation flows through GitOps** — no direct write, no break-glass; change → PR → approve → CI/CD applies.
  3. **Agents never call each other directly** — coordination only via shared state (GitOps repo + knowledge base).
  4. **Each tier is scope-bounded** by a pre-created read-only identity, not by convention.
  5. **Every change is reviewed, attributable, and revertible** — it's a PR, and the audit ties it to a requester.

> These invariants are domain-specific to kube-agents. The transferable idea is: **distill the design's
> non-negotiables into a short checklist and gate every merge on it.** A change that violates one is
> wrong even if it compiles and passes tests.

---

## 5. Running it

**Manual (recommended to start):**

```
/harness-run          # do the next unit of work, then checkpoint the ledger
/harness-verify       # run the current phase's Accept + touched Verification suites
```

**Autonomous (multi-day):** a durable scheduled task re-enqueues `/harness-run` on an interval so
the build progresses unattended. Claude Code cron tasks auto-expire after 7 days and fire only while
the REPL is idle — re-arm as needed.

**Stopping / pausing:** delete the scheduled task and `/harness-run` stops self-triggering. The
harness also stops on its own at any halt condition and surfaces the reason in the ledger.

---

## 6. What it produced

Run over 2026-07-23 → 2026-07-24, the harness built kube-agents end-to-end: **all eight roadmap
phases (0–7) delivered and merged to `main`**, each as its own PR with green CI.

| Phase | Scope                                             | PR  |
| ----- | ------------------------------------------------- | --- |
| 0     | Foundations (CRD, controller, admission backstop) | #1/#2 |
| 1     | Read-only Platform Agent + GitOps loop            | #3  |
| 2     | Cluster Admin Agent + cascade + router            | #4  |
| 3     | Developer Team Agent + namespace-isolation proof  | #5  |
| 4     | Coordination & knowledge (push-first + OKF)       | #6  |
| 5     | Security gate & hardening                          | #7  |
| 6     | Failure-isolation & resilience (chaos suite)      | #8  |
| 7     | Cloud-agnostic seams                              | #9  |

Verification ran on a live Kind cluster (inner loop) with scratch-cloud checks flagged and deferred
where infra wasn't provisioned — **deferred, never faked.** Each phase left a consolidated,
re-runnable gate script (`local-dev/kind/verify-phase<N>.sh`) so any phase can be re-verified later.

**The autonomy caught real bugs.** A pre-PR adversarial review gate on Phase 0 (22 agents, 17 findings
→ 5 confirmed → 4 fixed and re-verified) found two genuine security defects before they shipped:

- The admission backstop was a write-verb **deny-list** that silently admitted `impersonate`
  (= cluster-admin). Flipped to a read-verb **allow-list** (`get`/`list`/`watch` only).
- The destructive-test guard used unanchored substring globs that would let a prod-lookalike context
  through. Anchored to `kind-*` / `gke-scratch-*`.

Recurring lessons the ledger captured and later phases relied on:

- **Stale-image trap (inner loop):** after any webhook/controller change, rebuild → `kind load` →
  `rollout restart` before verifying, or a stale image silently under-enforces admission. This bit
  three times before it became a standing rule.
- **Prettier on the full changed set:** CI checks every changed `.md`/`.yaml` against the base branch,
  not just files touched this session — pre-check the whole diff before opening the PR.
- **Base-remote gotcha:** diff against the fork's `main`, not the upstream remote, or the diff looks
  enormous.

---

## 7. Adapting the pattern to another project

The harness is domain-specific, but the shape is portable. To reuse it:

1. **Write the design set first.** The harness is only as good as the spec it builds from. You need a
   roadmap that splits the work into phases, and — critically — each phase needs explicit,
   machine-checkable **Accept** criteria and a **Verification** procedure. No acceptance criteria, no
   autonomous loop.

2. **Write the two skills.** `harness-run` is the 7-step loop (orient → pick → break down → design →
   implement → verify → regress → gate + checkpoint). `harness-verify` is your project's concrete
   check procedure. Adapt the step contents; keep the shape.

3. **Distill your invariants.** Pull the design's non-negotiables into a short pre-merge checklist.
   Keep it to a handful of load-bearing rules, each with a one-line evidence check.

4. **Create the ledger.** One Markdown file: current phase/task, a per-phase task table, a
   verification log (suite → target → PASS/FAIL → evidence), a decisions/deviations table, and a
   blockers/halt-conditions section. This is the durable memory — treat "update the ledger" as the
   last step of every run.

5. **Define halt conditions.** Decide which failures the harness must **never** auto-advance past
   (for us: the two load-bearing suites and any invariant breach). Everything else it can iterate on.

6. **Geofence anything destructive.** If verification includes destructive tests, add an explicit
   target guard that halts unless the context is a throwaway environment.

7. **Schedule it** with a durable cron re-enqueuing the run skill, and let it go — checking the
   ledger and open PRs, not the transcript, to see where it is.

The whole point: **files carry the state, skills carry the procedure, the ledger carries the memory,
and the invariants carry the safety.** The conversation is disposable.
