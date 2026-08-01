# Fleet Audit — Issue Ledger and Remediation Pull Requests

> **STATUS — design of record; not yet implemented.** The shipped behaviour is the one described in
> `agents/platform/skills/fleet-audit/SKILL.md`: each audit stream owns one continuously-rewritten
> Pull Request. This document specifies the replacement — a GitHub **issue** per stream plus narrow,
> per-finding remediation PRs linked back to it. Nothing here is live.

**Status:** Approved design, awaiting implementation
**Scope:** How the five autonomous audit watchdogs publish findings and propose fixes.
**Supersedes:** the PR-as-report model introduced in `424a345`.

---

## 1. Why the Pull Request is the wrong container

The shipped `fleet-audit` skill gives every audit stream exactly one open PR, force-pushed and
rewritten on each run. The idempotency and delta machinery around that is sound; the _object_ is
not.

- **The code manufactures a diff to get a comment thread.**
  `audit_pr.py` commits with `--allow-empty` unconditionally, and logs "No manifest remediations;
  committing an empty report commit." Two of the five streams — RBAC posture and upgrade readiness —
  produce mostly `gcloud` and `manual` remediations, so their PRs are routinely prose wearing a
  commit. Needing a fake commit to obtain a durable, labelled, commentable object is the signal that
  the object should have been an issue.

- **The diff is all-or-nothing.** Every manifest for a run lands on one branch. A reviewer who
  agrees with five findings and rejects two has no move except "request changes" on a branch that is
  force-pushed out from under them on the next run.

- **Force-push orphans review.** Line comments on the previous diff detach every run.

- **Closing on a clean run reads as rejection.** A closed PR means _declined_; a closed issue means
  _done_. Same API call, opposite meaning to a human scanning notifications.

- **The model cannot express "fix merged, problem persists."** Once a PR merges, the story ends. An
  audit that keeps reproducing a finding after its fix shipped is exactly the signal a platform admin
  most needs, and today it is invisible.

## 2. The target model

Two tiers. The issue is the only always-on object.

### Tier 1 — the ledger issue

One open GitHub issue per audit stream, rewritten in place on every run.

- Title, body, labels, and every timestamp are generated. The agent never hand-writes them.
- The run-over-run delta mechanism is carried over unchanged: the hidden
  `<!-- audit-findings: [...] -->` marker moves from the PR body to the issue body and
  `parse_delta_block` / `compute_delta` are reused verbatim.
- Findings render as rows in a findings table with per-finding anchors, each row naming its
  remediation state and, where one exists, its remediation PR.
- A clean run closes the issue **as completed** and closes any remediation PRs still open for that
  stream.
- `[SILENT]` semantics are unchanged: a clean run, or an `UPDATED` run with zero new findings, says
  nothing in chat.

### Tier 2 — remediation pull requests

Narrow PRs, each proposing the fix for one finding (or one group of findings whose manifest paths
collide), based on `main`, linked to the ledger issue with `Part of #<issue>`.

- Branch: `platform-agent/fix-<audit-id>-<finding-id>`. **The branch name is the source of truth
  for the finding↔PR link.** It survives anyone editing the issue body, and a single
  `gh pr list --label audit:<audit-id> --state all --json number,headRefName,state,mergedAt`
  reconstructs the whole mapping in one API call. No body marker is needed and none is added.
- Body carries only that finding: evidence, impact, the recommendation, and the diff.
- Labels: `agent:audit`, `audit:<audit-id>`, `audit:remediation`, `severity:<highest>`.

## 3. Decisions

Recorded with rationale so a later reader does not re-litigate them.

### 3.1 Gating — hybrid: auto for critical manifests, pull-based for everything else

A remediation PR opens automatically **iff** the finding satisfies all of:

1. `severity == "critical"`, and
2. `remediation.kind == "manifest"`, and
3. no PR already exists on its branch in any state.

Every other finding stays prose in the ledger until a human asks for it. Rationale: the highest-risk
findings that have a mergeable diff should arrive ready to merge; the long tail must not turn five
streams into a notification firehose.

**The human trigger is an issue comment command:** `/remediate <finding-id>`, or `/remediate all` to
promote every eligible finding in the stream. On its next run the audit parses the ledger issue's
comments, promotes the named findings, and replies once with the PR links.

Only `manifest` remediations are promotable. `/remediate` naming a `gcloud` or `manual` finding is
refused with a comment explaining that its remediation is a command to run, not a file to merge — a
PR with no diff is precisely what this redesign exists to eliminate.

_Rejected alternative:_ checkboxes in the issue task list. A checked box conventionally means "done",
not "please open a PR", and the semantics fight the body being rewritten every run. The comment
command is explicit, auditable, repeatable, and needs no state that the body rewrite could clobber.

_Idempotency:_ commands are never marked as processed. A promoted finding already has a branch and a
PR discoverable by name, so re-reading the same command on a later run is a no-op by construction.

### 3.2 A first-class `recommendation` field

`remediation.note` is a one-liner and cannot carry the argument a reviewer needs. Findings gain a
required `recommendation` object:

```json
"recommendation": {
  "action": "Apply a default-deny NetworkPolicy to the payments namespace.",
  "rationale": "Namespace-scoped default-deny is the smallest change that closes east-west exposure without touching the mesh config; a mesh AuthorizationPolicy would also work but takes effect only for injected pods.",
  "risk": "Any unlabelled cross-namespace traffic into payments breaks on apply. Verify with the traffic query in the SOP first."
}
```

- All three sub-fields are required, non-empty strings, for **every** finding — not only promotable
  ones. Making it conditional would let the agent defer the hard thinking to promotion time, when the
  evidence is no longer in front of it.
- Rendered in the ledger under each finding, and as the PR body's "Why this fix" section.
- Cost: a validator change plus a prose section in all five governance SOPs.

### 3.3 Stale remediation PRs are always auto-closed

When a run no longer reproduces a finding that has an open remediation PR, the PR is closed with a
generated comment naming the date, the exact command that no longer reproduces, and its output.

Accepted risk: this can close a PR a human was mid-review on. Mitigations, both required:

- The closing comment states plainly that the PR may be reopened, and that the audit will re-open a
  fresh one if the finding returns.
- **The branch is not deleted on close.** Branches are cheap and any human fixup pushed to the branch
  survives. A branch is reset only when the same finding is promoted again.

### 3.4 Replace the PR-report path outright

No flag, no deprecation window, no dual mode. `audit_pr.py` becomes `audit_report.py`, the
PR-as-report rendering is deleted, and all five SOPs plus the site docs are rewritten in the same
change.

**One-time reconciliation.** On first run under the new code a stream may still have an open report
PR from the old path. `finish` detects an open PR whose head branch is the legacy
`platform-agent/audit-<audit-id>` and closes it with a comment pointing at the new ledger issue.
The guard keys on the legacy branch name, so it is self-limiting and can be deleted after one
release.

## 4. Finding lifecycle

The ledger renders each finding in exactly one state. Transitions are computed per run, never stored.

| State                | Condition                                   | Ledger renders                                      | Action taken                                   |
| -------------------- | ------------------------------------------- | --------------------------------------------------- | ---------------------------------------------- |
| `open`               | reproduces; no PR on its branch             | finding + recommendation                            | none (or auto-promote if critical + manifest)  |
| `pr-open`            | reproduces; branch has an open PR           | finding + link to PR                                | refresh the PR body if the evidence changed    |
| `pr-merged-persists` | reproduces; branch PR is merged             | finding + **⚠ fix merged, still reproduces** + link | comment once on the merged PR; never reopen it |
| `resolved`           | no longer reproduces; branch has an open PR | removed from the table; named in the delta comment  | close the PR (§3.3), keep the branch           |
| `resolved`           | no longer reproduces; no open PR            | removed from the table; named in the delta comment  | none                                           |
| `refused`            | `/remediate` named a non-`manifest` finding | unchanged                                           | one-time reply comment explaining why          |

`pr-merged-persists` is the state the current design cannot express and is a primary reason for the
change. It must be visually distinct in the ledger.

## 5. Grouping

The promotion unit is a **non-overlapping remediation group**, not a finding. Findings whose
`remediation.path` values intersect must share one PR, or their branches conflict on merge. In
practice groups are almost always singletons.

- Group key: the sorted tuple of manifest paths, unioned transitively across findings that share any
  path.
- Branch name for a multi-finding group: `platform-agent/fix-<audit-id>-<lowest-sorted-finding-id>`,
  with every member finding named in the PR body and each linking back to the same PR from the
  ledger.
- Promoting any member of a group promotes the whole group. The reply comment says so.

## 6. Script surface

`agents/platform/skills/fleet-audit/scripts/audit_report.py` — three subcommands.

### `start --audit <id>`

Unchanged in spirit. Refreshes credentials, resolves the repo, ensures labels, locates the stream's
open ledger issue, returns the scratch path for `findings.json`. Emits:

```json
{
  "issue": 128,
  "repo": "acme/fleet",
  "findings_path": "/opt/data/scratch/findings_compliance-audit.json",
  "pending_remediation_requests": ["netpol-missing-payments"]
}
```

`pending_remediation_requests` is the parsed set of `/remediate` targets from the issue's comments,
surfaced early so the agent knows which findings need a manifest written during inspection.

Note the removed behaviour: `start` no longer resets a report branch. There is no report branch.

### `finish --audit <id> --findings-file <path> [--dry-run]`

1. Validate the document (existing validator plus `recommendation`).
2. Reconcile: one `gh pr list` call builds the finding→PR state map from head branch names.
3. Compute the delta against the ledger issue's `<!-- audit-findings -->` marker.
4. Clean run → close the ledger issue as completed, close every open remediation PR for the stream,
   print `CLEAN`.
5. Otherwise → render and create-or-edit the ledger issue, apply the severity label, post the delta
   comment when the delta is non-empty.
6. Auto-promote every eligible critical manifest finding (§3.1) and every `/remediate` target, by
   invoking the same code path as `remediate`.
7. Close stale PRs (§3.3); comment once on `pr-merged-persists` PRs.
8. Legacy report-PR reconciliation (§3.4).

Exit contract:

- `{"status":"OPENED","issue_url":"…","new":7,"resolved":0,"prs_opened":["…"],"prs_closed":[]}`
- `{"status":"UPDATED","issue_url":"…","new":2,"resolved":3,"prs_opened":[],"prs_closed":["…"]}`
- `{"status":"CLEAN","issue_url":"…","new":0,"resolved":5,"prs_opened":[],"prs_closed":["…"]}`

`--dry-run` renders the issue body and every PR body it _would_ open to stdout with zero git or gh
side effects.

### `remediate --audit <id> --findings-file <path> --finding <id>...`

The promotion primitive, callable directly and reused internally by `finish`. For each group: reset
the branch onto `main`, stage only the group's manifest paths (the existing wildcard-pathspec refusal
is retained), commit with a generated Conventional Commit subject, push, and create or edit the PR.

Manifest files must already exist on disk — a missing path stays a hard error.

## 7. Rendering

| Artifact             | Contents                                                                                                                                                                                                                                    |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Ledger issue title   | `[audit] <human name> — <n> findings (<c> critical)`, singular `1 finding`. Names from `AUDITS`, still asserted against `cron/jobs.json` by test.                                                                                           |
| Ledger issue body    | Scope (clusters covered, `skipped` with reasons, partial-coverage banner), findings table with state column, then per-finding detail: evidence, impact, recommendation, remediation, PR link. Hidden `<!-- audit-findings -->` marker last. |
| Delta comment        | New / resolved / newly-merged-but-persisting, by title. Reuses `render_delta_comment` with a fourth section.                                                                                                                                |
| Clean-close comment  | Date, clusters covered, PRs closed. Reuses `render_clean_comment`.                                                                                                                                                                          |
| Remediation PR title | `fix(<audit-id>): <finding title>`                                                                                                                                                                                                          |
| Remediation PR body  | `Part of #<issue>`, the single finding's evidence, impact, **Why this fix** (the recommendation), and the risk note. For a group, one section per member.                                                                                   |
| Stale-close comment  | Date, the command that no longer reproduces, its output, and the reopen note.                                                                                                                                                               |

## 8. Labels

`ensure_labels` gains one entry; the rest are unchanged.

| Label               | Applies to  | Purpose                                      |
| ------------------- | ----------- | -------------------------------------------- |
| `agent:audit`       | issue + PRs | Everything this skill owns                   |
| `audit:<audit-id>`  | issue + PRs | Stream identity; how the ledger is found     |
| `audit:remediation` | PRs only    | Distinguishes a fix PR from the ledger issue |
| `severity:*`        | issue + PRs | Highest live severity, mutually exclusive    |

## 9. Red lines (carried forward, plus new)

Unchanged: read-only against clusters; never `git add .` or `-A`; never force-push a protected
branch; never hand-write a body, title, commit message, or timestamp; a `manifest` path must exist on
disk before publishing.

New:

- **Never open a second ledger issue for a stream.** The agent never calls `gh issue create`;
  `finish` owns it.
- **Never open a remediation PR for a non-`manifest` finding.**
- **Never reopen a merged remediation PR.** A persisting finding gets a comment and a ledger state,
  not a resurrection.
- **Never delete a remediation branch on close.**

## 10. Work breakdown

Sequenced so each phase is independently reviewable. One PR per phase.

**Phase 1 — schema and pure helpers.** Add `recommendation` to the validator. Add group computation,
branch naming, state derivation, and `/remediate` command parsing as pure functions. Extend the
existing test module. No I/O, no behaviour change yet.

**Phase 2 — the ledger issue.** Port `find_existing_pr` → `find_existing_issue`, `render_body` →
`render_issue_body`, and the create/edit/comment/close paths from `gh pr` to `gh issue`. Delete the
report branch, the `--allow-empty` commit, and the force-push from `finish`. At the end of this phase
the skill publishes issues and opens no PRs at all.

**Phase 3 — remediation PRs.** The `remediate` subcommand, auto-promotion, the reconciliation query,
stale-close, and the `pr-merged-persists` comment.

**Phase 4 — migration and docs.** Rename `audit_pr.py` → `audit_report.py` and the test module to
match. Legacy report-PR reconciliation. Rewrite `SKILL.md`, the five governance SOPs, and the site
pages.

## 11. Files touched

Twenty-one existing files reference the audit PR path today:

```
agents/platform/CAPABILITIES.md
agents/platform/SOUL.md                                     (§3.2 — GitOps write paths)
agents/platform/cron/jobs.json
agents/platform/governance/compliance_audit_sop.md
agents/platform/governance/fleet_consistency_drift_sop.md
agents/platform/governance/fleet_wide_cost_analysis_sop.md
agents/platform/governance/obtainability_audit_sop.md
agents/platform/governance/security_patch_orchestrator_sop.md
agents/platform/skills/fleet-audit/SKILL.md
agents/platform/skills/fleet-audit/scripts/audit_pr.py      → audit_report.py
agents/platform/skills/fleet-audit/scripts/test_audit_pr.py → test_audit_report.py
docs/README.md
docs/site/src/content/docs/concepts/autonomous-watchdogs.md
docs/site/src/content/docs/concepts/declarative-workflow.md
docs/site/src/content/docs/concepts/governance-sops.md
docs/site/src/content/docs/overview/architecture.mdx
docs/site/src/content/docs/overview/proactive-autonomy.md
docs/site/src/content/docs/reference/cron-jobs.md
docs/site/src/content/docs/reference/security-and-iam.md
docs/site/src/content/docs/skills/index.mdx
scripts/generate_docs.py
```

## 12. Testing

The existing module is 60 tests over 980 lines; most pure-helper coverage ports unchanged. New cases:

- `recommendation` validation: each sub-field missing, empty, wrong type.
- Grouping: disjoint paths, two findings one path, transitive union across three findings.
- Promotion eligibility: critical+manifest auto; critical+gcloud not; major+manifest only on request;
  already-has-PR is a no-op in every state.
- Command parsing: `/remediate <id>`, `/remediate all`, unknown id, non-manifest id, the command
  appearing inside a fenced code block (must not match).
- State derivation across all six rows of the §4 table, including `pr-merged-persists`.
- Clean run closes the issue and every open remediation PR.
- Legacy reconciliation fires exactly once, and only for the legacy branch name.
- `--dry-run` performs zero git and zero gh calls (assert on the mocked runner).

Plus the existing gates: `make docs-generate`, `make docs-check`, `make validate`, `prettier`,
`astro build`, and a Docker build to prove in-image script paths.

## 13. Open questions

1. **Does the ledger issue live in the GitOps repo?** `resolve_repo()` derives it from the working
   directory's `origin`, which is the GitOps repo — correct for the PRs, but a platform admin may
   expect audit issues in an ops/tracking repo instead. If they must differ, `start` needs an
   explicit issue-repo argument and the App token needs scope on both.
2. **Does the App token already carry `issues: write`?** `github-issue-resolver/scripts/resolver.py`
   creates labels, comments, and closes issues with the same token, so issue write is established —
   but issue _creation_ has not been exercised. Confirm before Phase 2.
3. **Interaction with `github-issue-resolver`.** That skill autonomously polls, claims, and resolves
   open issues. It must be taught to skip `agent:audit` issues, or it will try to "resolve" every
   ledger the audits publish. This is a hard prerequisite, not a follow-up.
4. **Volume ceiling.** Hybrid gating bounds auto-opened PRs to critical manifest findings, but a
   genuinely bad fleet day could still open many at once. Consider a per-run cap with the withheld
   set named in the ledger.
