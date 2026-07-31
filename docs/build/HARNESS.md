# The Coding Harness

A small, reusable pattern for driving a large software build **autonomously, phase by phase, from a
written design set** — using Claude Code skills, a persistent ledger, and a verification loop instead
of a bespoke program.

This document is self-contained: it explains what the harness is, how it works, why it's safe to run
unattended, what it produced, and how to adapt the same pattern to another spec-driven project.

---

## 1. What it is

The kube-agents harness is **not a separate application**. It is five ordinary Claude Code artifacts
wired into a loop:

1. **A design set** — the source of truth (`docs/design/` 01–09), including a roadmap that splits the
   build into phases, each with explicit **Accept** criteria and **Verification** suites.
2. **Skills** — reusable prompts (`/harness-run`, `/harness-verify`, `/harness-milestone`,
   `/harness-improve`) that encode the per-phase loop, the verification procedure, the phase-close
   gate, and the pass that mechanizes lessons.
3. **A ledger** — a single Markdown file (`docs/build/LEDGER.md`) that is the harness's memory:
   current phase, task status, verification log, decisions, deviations, blockers, halt conditions.
4. **An inbox** — a single Markdown file (`docs/build/BACKLOG.md`) that is the _human's_ channel into
   a running build: append a finding at any time, and it is picked up at the next planning moment.
   See §2.1.
5. **A gate** — an invariants checklist (`.claude/harness/invariants.md`) every change must pass
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

| Step                 | What happens                                                                                           |
| -------------------- | ------------------------------------------------------------------------------------------------------ |
| 0. Orient            | Read the ledger, the invariants, and the current phase in the roadmap. Stop if a blocker/halt is open. |
| 1. Pick the unit     | First `todo`/`in-progress` task in the current phase breakdown. Keep it finishable in one run.         |
| 2. Break down        | On entering a new phase, expand its **Work** items into individually-verifiable tasks (`P<N>-T1`…).    |
| 3. Detailed design   | Only for architecturally non-trivial or spec-silent tasks; otherwise skip to implement.                |
| 4. Implement         | Branch, ground new code on existing patterns, format + build, Conventional Commits, scoped staging.    |
| 5. Verify            | Run phase **Accept** + touched **Verification** suites on the right target. Evidence required.         |
| 6. Regress           | Re-run prior-phase Accept + the two load-bearing suites. A regression is a halt, not a note.           |
| 7. Gate + checkpoint | Run the invariants checklist, update the ledger, open a PR, advance the phase.                         |

**Verification is adversarial.** A negative test that silently no-ops reads as green, so every
"passed" negative test is re-confirmed: did the admission policy actually _deny_ (non-zero apply,
explicit policy message), or was the manifest just malformed? "Verified" is never reported without a
command, log, or PR as evidence.

### 2.1 The human inbox — how a person steers a build that is already running

The loop above has no seat in it. That is the point, and it is also the problem: a human who notices
something at 2am — a leaked process, a check that is wrong, a spec sentence nobody implemented — has
nowhere to put it. The ledger is the wrong place, because the harness reads it first and writes it
last on every run, so an edit made mid-unit races that write and can be silently reverted.

`docs/build/BACKLOG.md` is the answer, and it is one rule:

> **A human writes it. The harness only drains it.**

A person appends a block to `## Inbox` at any time, including mid-unit, without coordinating with
anything. Appending at the end of one section cannot conflict with a harness write anywhere else in
the file, so the operation is always safe. Nothing changes immediately — and **the delay is the
feature**. A finding that redirects work the instant it is written lands mid-IMPLEMENT, when the
harness has the least context to place it well and the most reason to place it badly.

It is read at **ORIENT and only at ORIENT** — the harness's own planning moment, before it selects
the next unit. Every item is resolved in the same ORIENT that reads it: scheduled into a task, a
lesson, the improvement queue or a later phase; **refused with an argument**, which is a section of
its own and not a deletion; or escalated to a halt. Then it moves out of the inbox with an ID, its
destination and the date. Three properties make that trustworthy rather than aspirational:

- **Nothing survives two ORIENTs.** An item whose `Added` date precedes `Last drained` was in the
  inbox when the harness last looked and is still there — there is no reading of that which is not
  "read and ignored", so `dev/tests/invariants-gate.py` fails the build on it. An inbox that
  accumulates is a second, quieter ledger nobody reads.
- **The drain is committed before SELECT, as its own commit.** It is the one artifact ORIENT is
  required to _write_, and everything that follows it moves `HEAD` — a branch creation, a
  `git stash pop`, a `gh pr merge`. One of those silently reverted a completed drain once, and the
  reverted file passed every gate, because an empty inbox stamped with today's date is exactly what
  a correct drain looks like ([[LSN-043]]).
- **The harness never writes to the inbox.** Not its own findings, not a note to its next self. The
  affordance being protected is a human's and it is destroyed by sharing: the moment the harness can
  file there, an item in the inbox stops meaning _a person wants something_, and `Last drained`
  stops measuring whether the harness is listening. Harness findings go to the **ledger**, and the
  work they imply goes to a task in the **phase breakdown**. The sections below the inbox
  (`## Scheduled`, `## Refused`, `## Done`) are the harness's half of the file and it writes those
  freely; the same gate check enforces the split, and the structure that keeps the two halves
  legible, in `check_backlog_is_drained`.

The severity call is the harness's, not the author's. `Priority: normal` does not downgrade a
finding that names a live security regression — that becomes the next unit, or a halt, and the drain
says so in writing.

---

## 3. Components and where they live

| Piece              | Path                                        | Role                                                                     |
| ------------------ | ------------------------------------------- | ------------------------------------------------------------------------ |
| Design set         | `docs/design/` (01–09)                      | Source of truth; roadmap in `07-implementation-roadmap.md`               |
| Ledger             | `docs/build/LEDGER.md`                      | Persistent build state; read first, updated last every run               |
| **Human inbox**    | `docs/build/BACKLOG.md`                     | **The one file a person writes.** Drained at ORIENT; see §2.1            |
| Phase breakdowns   | `docs/build/phase-<N>.md`                   | Concrete task list for a phase, created on entry                         |
| Binding            | `.claude/harness/binding.md`                | Every project-specific value: paths, gates, commands, targets            |
| Lesson store       | `.claude/harness/LESSONS.md`                | Each mistake already paid for, and the check that now catches it         |
| Orchestrator skill | `.claude/skills/harness-run/SKILL.md`       | The per-phase loop (the 7 steps above)                                   |
| Verify skill       | `.claude/skills/harness-verify/SKILL.md`    | Runs Accept + Verification suites, logs results with evidence            |
| Milestone skill    | `.claude/skills/harness-milestone/SKILL.md` | Closes a phase: full gate, PR, merge, advance the ledger                 |
| Improve skill      | `.claude/skills/harness-improve/SKILL.md`   | Turns open lessons into mechanized checks; drains the improvement queue  |
| Invariants gate    | `.claude/harness/invariants.md`             | Load-bearing rules, checked before every merge                           |
| Verify workflow    | `.claude/harness/verify-phase.workflow.js`  | Optional parallel fan-out: one agent per suite, then adversarial confirm |

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
  the hub, applying deliberately-bad RBAC) run **only on the ephemeral scratch cluster** — a
  destructive-test guard confirms the kube context matches an anchored `gke-scratch-*` before any
  delete/kill/bad-apply, and **halts** otherwise. One accepted anchor, not two: a prefix that no
  cluster in the loop uses any more is a guard nobody maintains. Prod is never a target.

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

| Phase | Scope                                             | PR    |
| ----- | ------------------------------------------------- | ----- |
| 0     | Foundations (CRD, controller, admission backstop) | #1/#2 |
| 1     | Read-only Platform Agent + GitOps loop            | #3    |
| 2     | Cluster Admin Agent + cascade + router            | #4    |
| 3     | Developer Team Agent + namespace-isolation proof  | #5    |
| 4     | Coordination & knowledge (push-first + OKF)       | #6    |
| 5     | Security gate & hardening                         | #7    |
| 6     | Failure-isolation & resilience (chaos suite)      | #8    |
| 7     | Cloud-agnostic seams                              | #9    |

Verification ran on a live Kind cluster (inner loop) with scratch-cloud checks flagged and deferred
where infra wasn't provisioned — **deferred, never faked.** Each phase left a consolidated,
re-runnable gate script (`dev/verify/verify-phase<N>.sh`) so any phase can be re-verified later.

**The autonomy caught real bugs.** A pre-PR adversarial review gate on Phase 0 (22 agents, 17 findings
→ 5 confirmed → 4 fixed and re-verified) found two genuine security defects before they shipped:

- The admission backstop was a write-verb **deny-list** that silently admitted `impersonate`
  (= cluster-admin). Flipped to a read-verb **allow-list** (`get`/`list`/`watch` only).
- The destructive-test guard used unanchored substring globs that would let a prod-lookalike context
  through. Anchored to `kind-*` / `gke-scratch-*`.

Recurring lessons the ledger captured and later phases relied on:

- **Stale-image trap (inner loop):** a fixed tag plus `imagePullPolicy: IfNotPresent` means the
  kubelet keeps the copy it already has, so a rebuilt controller silently under-enforces admission
  and the gate reads green anyway. This bit three times before it became a standing rule, and a
  standing rule is addressed to whoever already remembers. The inner loop now deploys **by digest**
  — `dev/cluster/reload-images.sh` builds, pushes, reads the digest back out of the registry the
  kubelet pulls from, and sets `…@sha256:…` on the Deployment. A digest names one immutable
  manifest, so the trap stops being something a check has to detect and becomes unrepresentable;
  `rollout restart` goes with it, since a changed digest changes the spec, which _is_ a rollout.
  Precondition **P1** still runs, and still asserts the running pod's `imageID` digest is the build
  under test — the mechanism is what is now trusted, so the check that would catch it being
  bypassed is exactly the one to keep.
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

5. **Create a second file the human owns, and keep the loop out of it.** The ledger is not a mailbox:
   a person editing it mid-unit races the harness's own write. Give them an append-only inbox
   instead, drained at the loop's planning step and nowhere else, and enforce two things
   mechanically — that no item survives two drains, and that the harness's own findings go to the
   ledger rather than into the inbox. Without the first, the file becomes a place findings go to be
   ignored; without the second, "someone wants something" stops being a signal at all. See §2.1.

6. **Define halt conditions.** Decide which failures the harness must **never** auto-advance past
   (for us: the two load-bearing suites and any invariant breach). Everything else it can iterate on.

7. **Geofence anything destructive.** If verification includes destructive tests, add an explicit
   target guard that halts unless the context is a throwaway environment.

8. **Schedule it** with a durable cron re-enqueuing the run skill, and let it go — checking the
   ledger and open PRs, not the transcript, to see where it is.

The whole point: **files carry the state, skills carry the procedure, the ledger carries the memory,
and the invariants carry the safety.** The conversation is disposable.
