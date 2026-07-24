# kube-agents — Build Ledger

**This file is the harness's memory across sessions and days.** Every harness run reads it
first to learn where the build is, then updates it before stopping. It is the single source of
truth for build progress — not chat history, not git log alone.

- **Source of truth for _what_ to build:** `docs/design/` (01–08). Never contradict a spec; if a
  spec is genuinely silent, pick the simplest option consistent with the invariants, implement it,
  and record it under **Decisions & deviations** below.
- **Source of truth for _how_ to build:** `docs/design/07-implementation-roadmap.md` (phases,
  acceptance, verification loop §5) + `.claude/harness/invariants.md` (the pre-merge gate).
- **How the loop works:** `.claude/skills/harness-run/SKILL.md`.

---

## Status

| Field                | Value                                                                                                                                                                                                                                                       |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Current phase        | **Phase 1 — Read-only Platform Agent + GitOps loop**                                                                                                                                                                                                        |
| Current task         | T1/T2/T4/T5/T6/T7/T8 done (read-only render + no runtime RBAC/KSA mint + pre-created identity + CRD fields + (tier,scope) cardinality/tier-immutability/closed-allowlist admission). Next: T9 (cloud GSA viewer-only), T3 (retire create_cluster), T10–T12. |
| Overall              | ✅ Phase 0 **merged to `main`**; 🟡 Phase 1 in progress — 7/12 tasks committed on `phase-1-readonly-platform-agent` (pushed to fork)                                                                                                                        |
| Autonomy             | **Fully autonomous** (advance across phase boundaries; halt only on hard blocker or failed load-bearing suite)                                                                                                                                              |
| Verification targets | **Kind (inner loop)** + **scratch GKE (identity/cloud criteria)**                                                                                                                                                                                           |
| Last updated         | 2026-07-24 — T8 committed (`6df6dd9`): per-(tier,scope) cardinality + tier immutability + closed-allowlist admission (webhook + 2 CEL rules); `go build/vet/test` green; branch pushed to fork                                                              |
| Last updated by      | harness-run (Phase 1 T8)                                                                                                                                                                                                                                    |

**Merge policy (set by user 2026-07-24):** phase PRs are **auto-merged once the pre-merge gate is
green** (invariants + load-bearing suites passing on Kind), then the build continues — no wait for
manual merge. Auto-merge is gated, not blind: any halt condition below blocks the merge and surfaces
instead. **Base/target = `main`** — PR #1 consolidated design + Phase 0 onto `main` (`11d95fd`), so
future phase branches base on `main` and PR → `main`. (Product runtime GitOps review is unaffected —
that is a separate, RBAC/VAP-enforced property of the built system.)

**Load-bearing halt conditions (stop and surface, do NOT merge or auto-advance):**

1. A **security negative test** (03 §11) fails — read-only, per-tier scope, attenuation, no break-glass.
2. A **failure-isolation chaos test** (05 §8) fails.
3. An **invariant** (`.claude/harness/invariants.md`) would be violated by the change.
4. A destructive test is about to run anywhere other than **Kind** or a **scratch GKE** cluster.
5. A spec conflict with no simplest-option resolution.

---

## Phase progress

Phases and acceptance criteria are defined in `docs/design/07-implementation-roadmap.md` §2.
Detailed task breakdowns live in `docs/build/phase-<N>.md` (created when the phase is entered).

| Phase | Title                                  | Status              | PR                                                    | Notes                                                                                                                                                        |
| ----- | -------------------------------------- | ------------------- | ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 0     | Foundations                            | ✅ merged to `main` | [#2](https://github.com/adamparco/kube-agents/pull/2) | A1–A4 green; pre-PR gate (4 fixes); PR #2 `8a31c76`; **on `main` via PR [#1](https://github.com/adamparco/kube-agents/pull/1) `11d95fd`** (design + Phase 0) |
| 1     | Read-only Platform Agent + GitOps loop | 🟡 In progress      | —                                                     | Breakdown in `phase-1.md` (12 tasks); write-surface mapped (7-agent workflow); implementing → verify on Kind → PR to `main`                                  |
| 2     | Cluster Admin Agent + cascade          | ⬜ Not started      | —                                                     |                                                                                                                                                              |
| 3     | Developer Team Agent + isolation proof | ⬜ Not started      | —                                                     |                                                                                                                                                              |
| 4     | Coordination & knowledge               | ⬜ Not started      | —                                                     |                                                                                                                                                              |
| 5     | Security gate & hardening              | ⬜ Not started      | —                                                     |                                                                                                                                                              |
| 6     | Failure-isolation & resilience         | ⬜ Not started      | —                                                     |                                                                                                                                                              |
| 7     | Cloud-agnostic seams                   | ⬜ Not started      | —                                                     |                                                                                                                                                              |

Legend: ⬜ not started · 🟡 in progress · 🟢 acceptance passing · ✅ merged · 🔴 blocked

---

## Verification log

Append one row per verification run. "Suite" = a phase-acceptance run or a spec Verification
section (02 §10, 03 §11, 04 §9, 05 §8, 06 §10, 08 §7). Keep the most recent result per suite.

| Date       | Phase | Suite                                                 | Target (Kind/GKE) | Result  | Evidence (log/PR/commit)                                                                                                                                                                                                                                                                                                                                                                   |
| ---------- | ----- | ----------------------------------------------------- | ----------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 2026-07-23 | 0     | A1 — GitOps tree matches 06 §3                        | local (files)     | ✅ PASS | `find examples/gitops-repo -type f` matches 06 §3 layout (clusters/<c>/{provisioning,namespaces/<ns>,agents}, fleet, knowledge, policy, .github/workflows)                                                                                                                                                                                                                                 |
| 2026-07-23 | 0     | A4 — OKF validator passes on knowledge/               | local (files)     | ✅ PASS | `python3 local-dev/okf-validate.py examples/gitops-repo/knowledge` → exit 0, 2 entries; negative control on a bad entry → exit 1 (not a silent no-op)                                                                                                                                                                                                                                      |
| 2026-07-23 | 0     | A3 / 03 §11 — attenuation admission (load-bearing)    | Kind v1.31.2      | ✅ PASS | `local-dev/tests/negative-attenuation.sh`: write-verb Role DENIED, wrong-scope ClusterRole DENIED, read-only Role ADMITTED; denials adversarially confirmed from the policy (message match)                                                                                                                                                                                                |
| 2026-07-23 | 0     | A2 — overlays policy-clean + SAR isolation            | Kind v1.31.2      | ✅ PASS | 3 real read-only overlays admitted by the VAP; SAR: developer-team-agent create secrets→no, list pods in team-x→yes, in kube-system→no                                                                                                                                                                                                                                                     |
| 2026-07-23 | 0     | Pre-PR adversarial gate (re-verify + review)          | Kind v1.31.2      | ✅ PASS | 22 agents; 17 findings → 5 confirmed → 4 in-scope fixed & re-verified; A1–A4 stayed green. Fixes: VAP read-verb allow-list, anchored destructive guard, negative-test stdin cleanup, OKF link-title. See `phase-0.md` §Pre-PR review gate.                                                                                                                                                 |
| 2026-07-23 | 0     | A3 / 03 §11 — impersonate deny + guard (load-bearing) | Kind v1.31.2      | ✅ PASS | New impersonate ClusterRole → DENIED by VAP (message from policy; object never created). Guard refuses 3 prod-lookalike contexts. good-reader `NotFound` after run (leak fixed).                                                                                                                                                                                                           |
| 2026-07-24 | 1     | 08 §7 — controller mints no RBAC/SA (T4/T5)           | local (go test)   | ✅ PASS | `go build/vet/test ./...` green. Golden fixture no longer emits SA/viewer-CRB/explorer-CR+CRB; `config/rbac/role.yaml` has no `clusterroles`/`clusterrolebindings` and `serviceaccounts`→`get;list;watch`. Reconcile unit test asserts no finalizer + no minted RBAC. Kind cluster assertion (auth can-i) still pending phase verify.                                                      |
| 2026-07-24 | 1     | 06 §1.2/§10 — (tier,scope) cardinality + A4 (T8)      | local (go test)   | ✅ PASS | `go build/vet/test ./internal/...` green. New webhook tests: duplicate (platform,project-x) rejected; different projects/tiers coexist; tier change on update rejected; enabled GoogleChat/Slack with empty allowlist rejected, non-empty admitted, disabled admitted. 2 CEL rules regenerated cleanly into the CRD. Envtest (CEL) + Kind apply-time rejection still pending phase verify. |

---

## Decisions & deviations

When a spec is genuinely silent and the harness picks an option, record it here (README rule #3).
This is what a reviewer reads to understand choices the specs didn't dictate.

| Date       | Phase | Decision                                                                                                                                                                          | Rationale                                                                                                                                                                       | Spec touched |
| ---------- | ----- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------ |
| 2026-07-23 | —     | Harness verification fans out via subagents (Agent tool); the Workflow script is an optional accelerator requiring explicit opt-in.                                               | Agent tool needs no per-run opt-in, keeping autonomous runs unblocked.                                                                                                          | —            |
| 2026-07-23 | 0     | Scaffold the customer GitOps repo as a **reference layout inside this repo** at `examples/gitops-repo/` (the template customers fork); Kind/scratch bootstrap under `local-dev/`. | 06 §3's GitOps repo is external in prod and can't be created here; a versioned reference template is the buildable equivalent and satisfies "tree matches 06 §3".               | 06 §3, 07 P0 |
| 2026-07-23 | 0     | VAP read-only ceiling expressed as a read-verb **allow-list** (verbs ⊆ get/list/watch), not a write-verb deny-list.                                                               | Deny-list omitted RBAC escalation verbs; `impersonate` is standalone cluster-admin. Allow-list also closes any future non-read verb. Found by the pre-PR gate.                  | 03 §4, §11   |
| 2026-07-23 | 0     | Per-tier RBAC "render overlay" delivered as static per-tier exemplars at `policy/rbac-overlay/`, not a kustomize/render script beside `agents/`.                                  | A2 needs only that the pre-created overlay is the sole RBAC path; a tier+scope render isn't needed until a 2nd scope (Phase 1/2). Location/shape deviation from 06 §2 recorded. | 06 §2, §3    |
| 2026-07-23 | 0     | Binding / `aggregationRule` / non-tier-labeled RBAC checks deferred to the cross-object child⊆parent admission webhook.                                                           | v1 VAP is scoped to a role's own rules (07); the binding path is held by human review + CODEOWNERS meanwhile.                                                                   | 07, 03 §11   |

---

## Blockers

Open items that halt autonomous progress. Clear a row when resolved (move detail to the PR).

| Date | Phase | Blocker                                                                                                                                                                                  | Needs |
| ---- | ----- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| —    | —     | None open. Phase 0 merged to `main` (PR #1 + #2). Harness implementing Phase 1 per `phase-1.md`; verify on Kind, then a single Phase 1 PR → `main` (auto-merged when the gate is green). | —     |
