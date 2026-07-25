# Invariants gate (pre-merge checklist)

The six load-bearing rules from `docs/design/README.md`. **A change that violates one is wrong even
if it compiles and passes tests.** The harness runs this checklist before opening/merging any PR.
Detailed in `docs/design/03-security-model.md`.

> **These invariants were inverted on 2026-07-24.** kube-agents converted from read-only agents that
> proposed GitOps PRs to **imperative agents that act**. The previous gate required agents to hold no
> write verb and all mutation to flow through a merged PR; those rules are superseded. See
> `docs/design/README.md` §"What changed from the previous generation" and
> `docs/design/07-implementation-roadmap.md` for the conversion sequence.

Answer each as PASS / FAIL / N-A with one line of evidence (a file, a test name, a command output).

1. **Agents act.** The change does not make an agent file a proposal, open a ticket, or ask a human
   to run a command for work that is in scope, reversible, and below the gate threshold.
   - Check: no new "propose"/"suggest"/"open a PR" terminus on a remediation path; the path ends in
     an Action Envelope submitted to the broker.

2. **Scope is absolute.** No change lets an agent read or write outside its project / cluster /
   namespace, or widen its own authority.
   - Check: no new rule grants an agent identity anything beyond its tier template; the forbidden set
     (03 §3.3) is untouched; `vap-agent-scope` and the cross-object child ⊆ parent webhook still
     hold. **This invariant never goes red — not for one commit, not for one phase.**

3. **Every mutation is brokered, journaled, and reversible.** No code path mutates a cluster or cloud
   API outside the Action Broker; every executed action produces an `ActionRecord` with a valid undo
   plan.
   - Check: no direct client-go/`gcloud`/`kubectl` write in agent or skill code; writes carry the
     `kube-agents/action-id` annotation; undo-plan generation covers every verb the change adds. An
     action with no undo plan must be classified `gated`.

4. **Irreversible or high-blast-radius actions stop for a human.** The gated class is evaluated in
   code and can only be made stricter.
   - Check: no change lowers a risk class, empties the gated set, or lets a `ChangePolicy`, a prompt,
     a `SOUL.md` edit, or model output influence classification.

5. **Agents collaborate directly, without inheriting authority.** Delegation and escalation are real
   calls, and the callee re-authorizes in its own scope.
   - Check: no mesh path where the callee acts on the caller's identity, skips its own classifier, or
     bypasses its own pause state; loop prevention intact.

6. **Humans hold the brake.** `pause`, `freeze`, `undo`, and `contested` remain effective and
   independent of the model.
   - Check: the brake is implemented in the controller/broker, not in a skill or a prompt; it works
     with inference down; the broker fails closed when the journal or the freeze object is unreadable.

## Conversion-specific ordering checks (07 §5)

7. **Authority never precedes machinery.** No change grants an agent identity a write verb before the
   Action Broker, the risk classifier, the journal, and the undo path exist and are tested.
   - Check: if the diff adds a write verb to any agent RBAC or cloud IAM binding, the broker,
     classifier, `ActionRecord`, and undo path must already be present and covered by tests. An agent
     with write RBAC and no journal is worse than either the old system or the new one.

8. **Tests are replaced, never deleted.** A check asserting read-only-ness is removed only in the
   same commit that adds its imperative counterpart.
   - Check: the PR names the pair. A change that reduces the total number of security assertions is
     wrong.

## Harness self-discipline (`.claude/harness/PROTOCOL.md` §10, `SELF-IMPROVEMENT.md` §4)

These bind the **harness**, not the product. They exist because the harness can edit its own specs
and its own checks, which means the cheapest way to turn a build green is always to lower the bar.
Each maps to a named hack in `SELF-IMPROVEMENT.md` §4.

9. **No weakening to pass.** No spec, check, threshold, level, or gate is changed in the same unit of
   work as the implementation whose failure motivated the change.
   - Check: the diff does not modify both an implementation and the check that was failing it.
     Diagnose, record a lesson, and change the check as a separate, argued unit.

10. **Load-bearing suites are not autonomously weakenable.** Any change removing, relaxing, narrowing,
    or demoting the level of a BLOCKING-ALWAYS check is a **halt for human review**.
    - Check: the diff touches no BLOCKING-ALWAYS check definition. However good the argument, the
      harness does not get to make this call alone.

11. **Every pass carries evidence.** No check is recorded green without an evidence reference.
    - Check: every `pass` in the run manifest has a non-empty `evidence_ref`; a pass without one is
      recorded as `skipped`. No security or safety check was retried to green.

12. **Every deferral names an external blocker.** Deferral is legitimate; using it to hide a failure
    is not.
    - Check: each `deferred` result names a blocker outside the harness's control, and no
      BLOCKING-ALWAYS check is deferred.

13. **Every failure leaves a lesson.** A halt, a rework, or a discovered false green closes with a
    lesson record, and a lesson closes only with a mechanization ID or an argued refusal.
    - Check: `LESSONS.md` has an entry for this run's failures; the open-lesson count did not grow
      silently.

---

## Also enforce (repo mechanics, from AGENTS.md)

- Conventional Commits; scoped to the request; no unrelated formatting churn.
- `npx prettier --write` on changed md/json/yaml; `make`/`go build` if `k8s-operator/` changed;
  `docker build` the relevant Dockerfile target if the image changed.
- Use `.github/PULL_REQUEST_TEMPLATE.md`; do **not** use `gh pr create --fill`.
- Push PR branches to a fork, not upstream. Stage only targeted files.
- **Rebuild → load/push → restart before trusting any live gate.** A stale same-tag image with
  `imagePullPolicy: IfNotPresent` silently under-enforces admission and reads as green.

## Destructive-test guard

Before any test that deletes/kills resources, applies deliberately-bad RBAC, or exercises a
destructive **action** through the broker, confirm the target context is **Kind** or an **ephemeral
scratch GKE** cluster. If it is anything else (esp. a prod context), **halt and surface** — do not
run.
