# Fleet Audit — Issue Ledger and Remediation Pull Requests

> **STATUS — design of record; implemented.** The behaviour described here is the behaviour the
> harness ships: a GitHub **issue** per audit stream plus narrow, per-finding remediation PRs linked
> back to it. It replaces the model in which each audit stream owned one continuously-rewritten Pull
> Request.

**Scope:** How the five autonomous audit watchdogs publish findings and propose fixes.
**Supersedes:** the PR-as-report model introduced in `424a345`.

---

## 1. Why the Pull Request is the wrong container

The superseded `fleet-audit` path gives every audit stream exactly one open PR, force-pushed and
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
  `parse_delta_block` / `compute_delta` are reused verbatim. What the marker _lists_ is the set of
  findings the body actually rendered, which under the size budget of §7.1 may be a strict subset of
  the run's findings.
- Findings render as rows in a findings table with per-finding anchors, each row naming its
  remediation state and, where one exists, its remediation PR.
- A clean run closes the issue **as completed** and closes any remediation PRs still open for that
  stream.
- `[SILENT]` has one rule, and it is a conjunction of three: `new == 0` **and** `resolved == 0`
  **and** `partial == false`. If either counter is non-zero the agent reports the ledger issue URL
  and a one-line summary — a run that resolved five findings and found nothing new is _news_, the
  audit reporting that the fleet got better. And a run that could not read the whole fleet is never
  silent even when both counters are zero, because "I found nothing" and "I could not look" are
  different statements and only one of them is reassuring (§7, Partial coverage).

### Tier 2 — remediation pull requests

Narrow PRs, each proposing the fix for one finding (or one group of findings whose manifest paths
collide), based on `main`, linked to the ledger issue with `Part of #<issue>`.

- Branch: `platform-agent/fix-<audit-id>-<slug>-<digest>`, where the digest is the first ten hex
  characters of a SHA-256 over the group's **sorted set of remediation paths** and the slug is a
  readable fragment of the first path's stem. **The branch name is the source of truth for the
  finding↔PR link.** It survives anyone editing the issue body, and a single
  `gh pr list --label audit:<audit-id> --label audit:remediation --state all
--json number,headRefName,state,mergedAt,url,body,labels` reconstructs the whole mapping in one
  API call. No body marker is needed and none is added. `labels` is load-bearing rather than
  incidental: §3.3's harness-close-versus-human-close discriminator reads `audit:stale-closed` off
  it, so dropping the field from the projection would silently make every close look final.
- **The branch is keyed on the files, not on the finding ids.** An earlier draft of this section
  named the branch after the lowest-sorted member id, which does not survive contact with the way
  the model actually works: ids are regenerated from scratch every run, so the day an SOP heading is
  reworded — or the day one finding in a two-finding group is fixed and the survivor becomes the new
  lowest id — the branch name changes, the `headRefName` lookup misses, and the harness opens a
  second pull request proposing a fix that is already sitting in review. The set of paths a fix
  touches is the stable thing, so that is what the name is derived from.
- **The finding id is still constrained**, to `^[a-z0-9]([a-z0-9._-]{0,98}[a-z0-9])?$` with no `..`
  segment and no `.lock` suffix — even though the path digest took it out of the branch name. The
  original justification was that it was a git ref component; that is no longer true, and a rule
  whose stated reason has evaporated is a rule someone deletes. Two live reasons replace it. The id
  is the **join key** of the ledger's hidden delta block and of the `audit-persists:<id>` marker,
  both matched by line-anchored regexes that whitespace or a stray newline would silently break —
  and a silent break here means the delta reports every finding as new. And it is **typed by a
  human** in `/remediate <id>`, which rules out case variation and shell metacharacters. The git
  constraints are kept as a superset rather than relaxed: they cost nothing, the SOP-generated
  shapes already satisfy them, and a future change that puts an id back in a ref then finds the
  gate already in place. The
  SOPs already build ids deterministically from lowercased slugs; the rule makes that a hard gate
  rather than a convention, and `hack/check-docs-terminology.sh` now extracts the pattern from
  `FINDING_ID_RE` and fails the build if any document quotes a different one.
- Body carries only that finding: evidence, impact, the recommendation, and the diff.
- Labels: `agent:audit`, `audit:<audit-id>`, `audit:remediation`, `severity:<highest>`.

## 3. Decisions

Recorded with rationale so a later reader does not re-litigate them.

### 3.1 Gating — hybrid: auto for critical manifests, pull-based for everything else

A remediation PR opens automatically **iff** the finding satisfies all of:

1. `severity == "critical"`, and
2. `remediation.kind == "manifest"`, and
3. there is no **live** pull request on its branch.

Every other finding stays prose in the ledger until a human asks for it. Rationale: the highest-risk
findings that have a mergeable diff should arrive ready to merge; the long tail must not turn five
streams into a notification firehose. At most five auto-promotions per run (§13 Q4); the surplus is
named in the ledger.

"Live" rather than "in any state" is condition 3's whole point, and the distinction is between two
kinds of closed PR. One the harness closed itself as stale carries the `audit:stale-closed` label,
and if the finding comes back, re-proposing the fix is exactly right. One a **human** closed is a
considered rejection, and re-opening it every morning would be the harness overruling a person on a
schedule. A merged PR is likewise not re-promoted — that finding is `pr-merged-persists` (§4), which
is a different problem and gets a different treatment.

**The human trigger is an issue comment command:** `/remediate <finding-id>`, or `/remediate all` to
promote every eligible finding in the stream. On its next run the audit parses the ledger issue's
comments, promotes the named findings, and replies once with the PR links. The command is honoured
only from a commenter with write access to the repo — `authorAssociation` of `OWNER`, `MEMBER`, or
`COLLABORATOR` (§13 Q5).

Only `manifest` remediations are promotable. `/remediate` naming a `gcloud` or `manual` finding is
refused with a comment explaining that its remediation is a command to run, not a file to merge — a
PR with no diff is precisely what this redesign exists to eliminate.

_Rejected alternative:_ checkboxes in the issue task list. A checked box conventionally means "done",
not "please open a PR", and the semantics fight the body being rewritten every run. The comment
command is explicit, auditable, repeatable, and needs no state that the body rewrite could clobber.

_Idempotency:_ commands are never marked as processed — the comment is never edited, reacted to, or
otherwise mutated, and that is deliberate: a repo writer who closes a remediation PR must be able to
re-issue `/remediate` and have it take effect. For a **promoted** finding this needs no state at all;
the finding already has a branch and a PR discoverable by name, so re-reading the same command on a
later run is a no-op by construction.

The actions that must happen _exactly once_ have no such natural key, so each gets a **hidden marker
in a body**, the same technique the delta block already uses:

- `<!-- audit-persists:<finding-id> -->` in the merged remediation PR's body. Present means the
  persistence comment has already been posted for that finding; absent means post it.
- `<!-- audit-refused:<comment-node-id> -->` in the ledger issue body. Present means that specific
  `/remediate` comment has already been refused; absent means reply and record it.
- `<!-- audit-acked:<comment-node-id> -->`, likewise, for a `/remediate` that was **accepted**. A
  request that is honoured needs answering exactly as much as one that is refused — the requester is
  owed the PR links — and a standing comment on a long-lived ledger would otherwise be re-answered
  every morning for as long as the issue is open.
- `<!-- audit-stale-closed:<pr-number> -->` records that the harness, not a human, closed that PR.
  It backs the `audit:stale-closed` label of §3.3 in a place a label edit cannot reach.

Keying the `/remediate` markers on the **comment node id**, not the finding id, is what lets a later
`/remediate` for the same finding be answered again: the second comment is a different comment, and
a person asking twice is asking twice.

Idempotency lives in bodies the renderer owns, never in the command comment a human wrote.

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
- **Size cost, measured.** An SOP-shaped finding renders at roughly 968 characters today; the
  required `recommendation` (`action` + `rationale` + `risk`) takes that to roughly 1,439. Against
  GitHub's 65,536-character body limit, overflow therefore moves from N≈67 findings to **N≈45**.
  This is the reason §7.1 specifies a size budget. It is not an argument against the field: the
  reviewer's argument is worth more than the forty-fifth minor finding, so the field stays required
  and the renderer learns to truncate.

### 3.3 Stale remediation PRs are auto-closed — over complete coverage

When a **complete** run no longer reproduces a finding that has an open remediation PR, the PR is
closed with a generated comment naming the date, the exact command that no longer reproduces, and
its output. Over a partial run nothing is closed (§7.3): retiring a fix asserts that its finding is
gone, and an audit that could not read the cluster has no standing to assert it.

Accepted risk: this can close a PR a human was mid-review on. Mitigations, all three required:

- The closing comment states plainly that the PR may be reopened, and that the audit will re-open a
  fresh one if the finding returns.
- **The branch is not deleted on close.** Branches are cheap and any human fixup pushed to the branch
  survives. A branch is reset only when the same finding is promoted again.
- **The close is labelled `audit:stale-closed`.** Without a marker, next month's run cannot tell its
  own close from a maintainer's rejection, and it must treat those oppositely (§3.1). The label is
  the harness signing its own work; removing it makes the close permanent.

### 3.4 Replace the PR-report path outright

No flag, no deprecation window, no dual mode. `audit_pr.py` becomes `audit_report.py`, the
PR-as-report rendering is deleted, and all five SOPs plus the site docs are rewritten in the same
change.

**No legacy reconciliation, and none is needed.** `finish` does not hunt for an open report PR on
the legacy `platform-agent/audit-<audit-id>` head branch, because no such PR can exist: the
PR-report path lives only on an unmerged feature branch, `main` carries no fleet-audit crons, and no
released image has ever opened a report PR. A guard keyed on the legacy branch name would be dead
code from birth — untriggerable in every environment that can run this skill, and therefore
untestable except against a fixture invented to justify it. The replacement is a replacement; there
is no fleet state left over from the model it replaces.

## 4. Finding lifecycle

The ledger renders each finding in exactly one state. Transitions are computed per run, never stored.

| State                | Condition                                            | Rendered as                      | Action taken                                            |
| -------------------- | ---------------------------------------------------- | -------------------------------- | ------------------------------------------------------- |
| `open`               | reproduces; no PR on its branch                      | `open`                           | none, unless it qualifies for auto-promotion            |
| `pr-open`            | reproduces; branch has an open PR                    | `fix proposed` + link            | **nothing** — the PR is left exactly as it is           |
| `pr-merged-persists` | reproduces; branch PR is merged                      | `⚠ fix merged, still reproduces` | comment once on the merged PR; never reopen it          |
| `refused`            | reproduces; branch PR is closed and was never merged | `fix refused` + link             | none — the close stands until someone says `/remediate` |
| `resolved`           | no longer reproduces; PR open or absent              | dropped; named in the delta      | close any open PR (§3.3), keep the branch               |
| `resolved-merged`    | no longer reproduces; branch PR is merged            | dropped; delta records the fix   | none — the ledger records that the merged fix is why    |

Two rows are easy to misread, and both were wrong in an earlier draft:

- **`pr-open` is not refreshed.** The draft said "refresh the PR body if the evidence changed",
  which would have the harness force-push over a reviewer's own commits every morning. An open
  remediation PR is left alone; the ledger links it, and the diff is whatever a human last made it.
- **`refused` is a human decision, not a rejected command.** It is what a finding looks like when
  someone closed its fix without merging — a considered "no" the harness must not overrule by
  re-proposing the same fix tomorrow. (A refused `/remediate` is a _reply_, not a finding state;
  there is no finding to put in a state, which is why the draft's condition for this row did not
  correspond to anything the renderer could compute.) The discriminator is the `audit:stale-closed`
  label: a close the harness performed is re-openable, a close a human performed is final, and the
  escape hatch either way is `/remediate <id>` from someone with write access.

`pr-merged-persists` is the state the current design cannot express and is a primary reason for the
change. It must be visually distinct in the ledger.

The two "once" obligations in the last column — the comment on a merged PR and the reply to a
refused command — are enforced by the hidden `audit-persists` / `audit-refused` markers of §3.1, not
by mutating anything a human wrote.

## 5. Grouping

The promotion unit is a **non-overlapping remediation group**, not a finding. Findings whose
`remediation.path` values intersect must share one PR, or their branches conflict on merge. In
practice groups are almost always singletons.

- Group key: the sorted tuple of manifest paths, unioned transitively across findings that share any
  path.
- Branch name for a group: `platform-agent/fix-<audit-id>-<slug>-<digest>` over that same sorted
  path tuple (§2), with every member finding named in the PR body and each linking back to the same
  PR from the ledger. Deriving the name from the group key rather than from a member id is what
  makes it stable across runs: the key is the set of files, and the group survives its members being
  renumbered or partially fixed.
- Promoting any member of a group promotes the whole group. The reply comment says so.

## 6. Script surface

`agents/platform/skills/fleet-audit/scripts/audit_report.py` — three subcommands.

### `start --audit <id>`

Resolves the repo, refreshes credentials, establishes the GitOps clone, ensures labels, locates the
stream's open ledger issue, and returns the scratch path for `findings.json`. Emits:

```json
{
  "issue": 128,
  "repo": "acme/fleet",
  "workspace": "/opt/data/gitops/acme__fleet",
  "findings_path": "/opt/data/scratch/findings_compliance-audit.json",
  "pending_remediation_requests": ["netpol-missing-payments"]
}
```

`pending_remediation_requests` is the parsed set of `/remediate` targets from the issue's comments,
surfaced early so the agent knows which findings need a manifest written during inspection.

`workspace` is the clone, and it is not decoration. The audit cron starts in the agent's profile
directory, which is not a working tree — so there is nothing to `git add` into and nothing for
`git config --get remote.origin.url` to answer. The harness therefore clones lazily on the way in,
and **every `remediation.path` is resolved against this directory**. A manifest written anywhere
else is a file the harness will never find.

That also fixes an ordering problem worth recording: the GitHub App token is repo-scoped, so it
cannot be minted before the repo is known, and the repo used to be derived from the clone the token
was needed to create. The repo now comes from the `Git Repo:` line of `/opt/data/SETTINGS.md`, which
the operator writes at provisioning time and which is present from the pod's first second, with the
git remote kept only as a fallback. This resolves §13 Q1 in passing: `github-issue-resolver` already
read that same line, so the two skills now agree by construction rather than by coincidence.

`start` is also the **only** subcommand that scrubs the working tree — see the note under
`remediate`. Note the removed behaviour: it no longer resets a report branch. There is no report
branch.

### `finish --audit <id> --findings-file <path> [--dry-run]`

1. Validate the document (existing validator plus `recommendation`, the finding-id charset rule of
   §2, and the scope rules of §7.2).
2. Reconcile: one `gh pr list` call builds the finding→PR state map from head branch names.
3. Compute the delta against the ledger issue's `<!-- audit-findings -->` marker.
4. Compute coverage gaps (§7.3). A gap does not stop the run; it narrows what the run may conclude.
5. Clean run → close the ledger issue as completed, close every open remediation PR for the stream,
   print `CLEAN`. **Unless the run is partial**, in which case the status is still `CLEAN` but the
   issue stays open with a comment naming the gaps and no PR is retired.
6. Otherwise → render and create-or-edit the ledger issue, apply the severity label, post the delta
   comment when the delta is non-empty.
7. Auto-promote every eligible critical manifest finding (§3.1) — at most five per run, the surplus
   named in the ledger as awaiting `/remediate` (§13 Q4) — and every authorised `/remediate` target,
   which is uncapped, by invoking the same code path as `remediate`.
8. Close stale PRs (§3.3), unless partial; comment once on `pr-merged-persists` PRs; answer every
   `/remediate` exactly once, with an acknowledgement or a refusal. Each "once" guard reads the
   hidden markers of §3.1.

Exit contract — eight keys, always all eight:

- `{"status":"OPENED","issue_url":"…","new":7,"resolved":0,"prs_opened":["…"],"prs_closed":[],"partial":false,"coverage_gaps":[]}`
- `{"status":"UPDATED","issue_url":"…","new":2,"resolved":3,"prs_opened":[],"prs_closed":["…"],"partial":false,"coverage_gaps":[]}`
- `{"status":"CLEAN","issue_url":"…","new":0,"resolved":5,"prs_opened":[],"prs_closed":["…"],"partial":false,"coverage_gaps":[]}`
- `{"status":"CLEAN","issue_url":"…","new":0,"resolved":0,"prs_opened":[],"prs_closed":[],"partial":true,"coverage_gaps":["prod-eu-1: API server unreachable"]}`

`--dry-run` renders the issue body and every PR body it _would_ open to stdout with zero git or gh
side effects. It applies the **same** grouping, promotion, budget, and coverage-gap degradation as a
real run, so a dry run that looks right is evidence the real one will be — a dry run that took a
shorter path through the code would be worth very little.

### `remediate --audit <id> --findings-file <path> --finding <id>...`

The promotion primitive, callable directly and reused internally by `finish`. For each group: reset
the branch onto `main`, stage only the group's manifest paths (the existing wildcard-pathspec refusal
is retained), commit with a generated Conventional Commit subject, push, and create or edit the PR.

Manifest files must already exist under the workspace, but **a missing one is no longer a hard
error**. That finding degrades to `kind: manual`, keeps its evidence and recommendation, says in the
ledger that a fix was named but not written, and the report publishes. Killing a nine-critical
security report because one of the nine manifests was not written is the wrong shape of failure: it
throws away eight findings to punish one.

`remediate` degrades the same way, for the same reason at a smaller scale. A named target whose file
is missing is refused **by name** — logged, and returned in the `refused` key of its exit JSON so
the acknowledgement comment can say so — while the rest of the batch opens. This matters because
`/remediate all` expands to every manifest-remediation id in the document: refusing the batch would answer a request for
thirty fixes with zero, and leave the operator to work out which one was to blame. Only the case
where _every_ named target is refused is an error, since there is then no partial success to report
and an exit 0 with an empty list would read as "done".

Neither `finish` nor `remediate` scrubs the working tree, and that is load-bearing rather than
incidental. The agent writes its remediation manifests into the clone _between_ `start` and
`finish`, and they are untracked until a remediation branch stages them — so a `git clean -fd` on
the way into `finish` would delete every fix the audit just produced and then report each one as a
file the model forgot to write. Only `start` may reset, because at `start` there is nothing yet to
lose and anything present is debris from a run that did not finish.

## 7. Rendering

| Artifact             | Contents                                                                                                                                                                                                   |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Ledger issue title   | `[audit] <human name> — <n> findings (<c> critical)`, singular `1 finding`. Names from `AUDITS`, still asserted against `cron/jobs.json` by test.                                                          |
| Ledger issue body    | Scope, findings table with state column, then per-finding detail: evidence, impact, recommendation, remediation, PR link. Hidden `<!-- audit-findings -->` marker last, listing the ids the body rendered. |
| Scope                | Clusters covered with their optional per-cluster `limitations`, `skipped` with reasons, partial-coverage banner. Both tables cap at 60 rows. See §7.2.                                                     |
| Size budget          | 60,000 characters, against GitHub's hard limit of 65,536. See §7.1.                                                                                                                                        |
| Delta comment        | New / resolved / newly-merged-but-persisting, by title. Reuses `render_delta_comment` with a fourth section.                                                                                               |
| Clean-close comment  | Date, clusters covered, PRs closed. Reuses `render_clean_comment`.                                                                                                                                         |
| Remediation PR title | `fix(<audit-id>): <finding title>`                                                                                                                                                                         |
| Remediation PR body  | `Part of #<issue>`, the single finding's evidence, impact, **Why this fix** (the recommendation), and the risk note. For a group, one section per member.                                                  |
| Stale-close comment  | Date, the command that no longer reproduces, its output, and the reopen note.                                                                                                                              |

### 7.1 Size budget

GitHub caps an issue body at **65,536 characters**; issue bodies and PR bodies carry the identical
limit, so nothing about moving from PR to issue relaxes it. The renderer targets **60,000**, leaving
headroom for the trailing marker and for anything a later section appends.

- **Per-finding trims.** Excerpts already trim to 40 lines / 2,000 characters. **Commands now trim
  to 2,000 characters too.** The SOPs mandate pasting the confirm command verbatim into
  `evidence.command`, which makes it the dominant per-finding term, and it was previously unbounded.
- **Table caps.** The scope and skipped tables cap at 60 rows each, with a trailing "…and N more"
  row. Without the cap a body with _zero findings_ overflows: 1,200 clusters plus 1,200 skipped
  entries renders 148,627 characters of pure scope.
- **Order of measurement.** Header, scope, and footer are rendered and measured first; whatever
  remains of the 60,000 is the findings budget. Findings are selected **severity-first**, so
  truncation only ever eats the least-severe end and criticals are structurally safe — a fleet with
  five criticals and three hundred minors publishes all five criticals no matter what.
- **Truncation is stated, counts are not.** When findings are omitted the body says so explicitly,
  and the title's counts remain the **true totals**. The reader is never told there are fewer
  findings than there are.
- **The delta marker describes what was rendered.** The hidden marker lists exactly the findings the
  body contains, not the full finding set. Otherwise the next run would see a truncated finding
  absent from the previous marker, or present in it and absent from the body, and report a finding
  that is very much still reproducing as _resolved_. The marker is itself a size term and was
  unbounded: 1,250 finding ids render 80,526 characters of marker alone, over the limit before a
  single word of prose, and `obtainability_audit_sop.md:67` permits exactly that many findings in
  one run.
- The clean-close comment is measured against the same budget, for the same reason: a clean run on a
  fleet with 900 skipped clusters must still be postable.

### 7.2 Scope, skipped, and limitations

`scope.clusters[]` gains an optional `limitations` string. It exists because "I read this cluster
successfully, but some checks did not run or do not apply" had nowhere to live and collided with
`scope.skipped` — and that collision produced **false all-clears**. One SOP line tells the agent to
put an Autopilot cluster in `scope.skipped` because a node-level check cannot apply there; another
tells it not to flag anything on a skipped cluster. Together they suppress every real finding on a
cluster the agent was explicitly told to audit.

The SOPs now state one question, and the schema has one answer for each branch of it:

> A cluster appears in exactly one scope list. Could you read it? Yes → `scope.clusters`; if some
> checks did not run there, name them in that cluster's `limitations`. No → `scope.skipped`. Nothing
> goes in both, and nothing in `scope.skipped` may appear in a finding.

The validator enforces both halves: the two lists must be disjoint, and a finding whose `cluster`
names a skipped entry is rejected. The rendered scope table carries a `limitations` column only when
at least one cluster has one, so the common case stays two columns wide.

### 7.3 Partial coverage

`coverage_gaps(data)` folds the two representations of "did not look" — every `scope.skipped` entry
and every cluster `limitations` note — into one list of human-readable strings, and a run with a
non-empty list is **partial**.

The reason this needs a name is that the whole ledger rests on one inference: _a finding that was in
yesterday's document and is not in today's has been fixed._ That inference is sound only over a
fleet the audit actually read. Absence of evidence from a cluster nobody could reach is not evidence
of absence, and acting on it does real damage — it announces fixes that did not happen, and it
closes the pull request that was going to make them happen.

So over a partial run the harness withholds exactly the conclusions that depend on complete
coverage, and nothing else:

- `resolved` is reported as `0` and no resolved-delta is posted. Findings that genuinely were fixed
  are simply reported the next time the fleet is fully readable.
- No remediation PR is stale-closed. Every open fix survives to the next complete run.
- Zero findings does not close the ledger. `status` is still `CLEAN` — the audit found nothing, and
  saying otherwise would be its own lie — but the issue stays open and gains a comment naming the
  gaps, so the stream self-heals the day the unreadable clusters come back.
- The run is never `[SILENT]` (§2). "I found nothing" and "I could not look" must not arrive in chat
  as the same silence.

What a gap does **not** do is suppress the report. Findings from the clusters that _were_ read are
published normally, and new fixes are still proposed for them. A partial audit is a partial audit,
not a failed one.

`partial` is `true` if and only if `coverage_gaps` is non-empty, on both `finish` branches. The
tempting generalisation — also raising it when the body budget (§7) dropped findings from the
description — was implemented and then removed, because the two are not the same kind of incomplete
and the flag has one job. A coverage gap means the audit did not look, which is precisely why it
suppresses the resolved count. Truncation means it looked, found everything, counted it all in the
title, and could not print the tail; resolution accounting is untouched, because the delta block
already carries only the ids the body rendered (§2). Folding them together produced
`partial: true` with an empty `coverage_gaps` — a flag five SOPs instruct the agent to explain to a
human, with nothing to explain it with. Truncation is surfaced where it belongs: a line in the body
naming the count it dropped, and a `WARNING` in the run log.

## 8. Labels

`ensure_labels` gains two entries; the rest are unchanged.

| Label                | Applies to  | Purpose                                      |
| -------------------- | ----------- | -------------------------------------------- |
| `agent:audit`        | issue + PRs | Everything this skill owns                   |
| `audit:<audit-id>`   | issue + PRs | Stream identity; how the ledger is found     |
| `audit:remediation`  | PRs only    | Distinguishes a fix PR from the ledger issue |
| `audit:stale-closed` | PRs only    | The harness closed this, not a human (§3.3)  |
| `severity:*`         | issue + PRs | Highest live severity, mutually exclusive    |

## 9. Red lines (carried forward, plus new)

Unchanged: read-only against clusters; never `git add .` or `-A`; never force-push a protected
branch; never hand-write a body, title, commit message, or timestamp.

Amended: a `manifest` path **should** exist under the workspace before publishing, and the agent is
still told to write it — but a missing one degrades that finding to `manual` rather than killing the
run (§6, `remediate`).

New:

- **Never open a second ledger issue for a stream.** The agent never calls `gh issue create`;
  `finish` owns it.
- **Never open a remediation PR for a non-`manifest` finding.**
- **Never reopen a merged remediation PR.** A persisting finding gets a comment and a ledger state,
  not a resurrection.
- **Never delete a remediation branch on close.**
- **Never report a cluster the audit could not read as clean.** An unreadable cluster goes in
  `scope.skipped`, a cluster read with a check missing gets a `limitations` note, and either way the
  run is partial (§7.3). Reporting an all-clear for a cluster nobody looked at is the one failure
  mode that makes the whole ledger untrustworthy.
- **Never put a credential in `evidence.excerpt`.** No Secret `data:` or `stringData:` block, no
  token, no password, no private key. The SOPs say this to the model; `audit_report.py` also
  enforces it on the way out, replacing a `data:` block, a secret-named field, a self-identifying
  token prefix, a PEM header, or an `Authorization:` value with `[redacted by audit_report.py]`. The
  backstop exists because the ledger is a public artefact and one leaked excerpt cannot be recalled;
  it is a backstop and not a licence, since it cannot recognise bare base64.

## 10. Work breakdown

Sequenced so each phase is independently reviewable. One PR per phase.

**Phase 1 — schema and pure helpers.** Add `recommendation`, the finding-id charset rule (§2), and
scope disjointness with `limitations` (§7.2) to the validator. Add group computation, branch naming,
state derivation, size budgeting, and `/remediate` command parsing as pure functions. Extend the
existing test module. No I/O, no behaviour change yet. The one exception to "no behaviour change" is
the `github-issue-resolver` exclusion (§13 Q3): it lands here so that it is never absent while a
ledger issue exists.

**Phase 2 — the ledger issue.** Port `find_existing_pr` → `find_existing_issue`, `render_body` →
`render_issue_body`, and the create/edit/comment/close paths from `gh pr` to `gh issue`. Delete the
report branch, the `--allow-empty` commit, and the force-push from `finish`. At the end of this phase
the skill publishes issues and opens no PRs at all.

**Phase 3 — remediation PRs.** The `remediate` subcommand, auto-promotion, the reconciliation query,
stale-close, and the `pr-merged-persists` comment.

**Phase 4 — migration and docs.** Rename `audit_pr.py` → `audit_report.py` and the test module to
match. Rewrite `SKILL.md`, the five governance SOPs, and the site pages — including the four stale
GitHub App permission lines of §13 Q2. There is no legacy reconciliation step (§3.4).

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
docs/site/src/content/docs/concepts/declarative-workflow.md   (:63 — stale App permissions)
docs/site/src/content/docs/concepts/governance-sops.md
docs/site/src/content/docs/overview/architecture.mdx
docs/site/src/content/docs/overview/proactive-autonomy.md
docs/site/src/content/docs/reference/cron-jobs.md
docs/site/src/content/docs/reference/security-and-iam.md
docs/site/src/content/docs/skills/index.mdx
scripts/generate_docs.py
```

Four more are prerequisites of the ledger rather than references to the PR path:

```
agents/platform/skills/github-issue-resolver/SKILL.md            (§13 Q3 — red line gains `agent:audit`)
agents/platform/skills/github-issue-resolver/scripts/resolver.py (§13 Q3 — poll query gains `-label:agent:audit`)
docs/site/src/content/docs/deploy/token-minter.md                (§13 Q2 — :26, :61, stale App permissions)
docs/site/src/content/docs/install/prerequisites.md              (§13 Q2 — :89, stale App permissions)
```

## 12. Testing

The module this design started from was 60 tests over 980 lines; most pure-helper coverage ported
unchanged. As shipped it is **274**. New cases:

- `recommendation` validation: each sub-field missing, empty, wrong type.
- Finding-id charset: an id containing `:`, a space, `..`, or `*`, one ending `.lock`, one starting
  or ending in `.`, `_`, or `-`, and one over 100 characters are each rejected; the SOP-generated
  shape is accepted.
- Scope: `clusters` and `skipped` must be disjoint; a finding naming a skipped cluster is rejected; a
  cluster with `limitations` is accepted and renders the extra column, and the column is absent when
  no cluster carries one.
- Grouping: disjoint paths, two findings one path, transitive union across three findings.
- Promotion eligibility: critical+manifest auto; critical+gcloud not; major+manifest only on request;
  already-has-PR is a no-op in every state; the sixth eligible critical in a run is withheld and
  named in the ledger, while six explicit `/remediate` targets all open.
- Command parsing: `/remediate <id>`, `/remediate all`, unknown id, non-manifest id, the command
  appearing inside a fenced code block (must not match), and a command from an `authorAssociation`
  of `NONE` or `CONTRIBUTOR` (refused, replied to once).
- State derivation across all six rows of the §4 table, including `pr-merged-persists`.
- Idempotency markers: a merged PR already carrying `audit-persists` gets no second comment; a ledger
  already carrying `audit-refused` for a comment node id does not re-refuse it; a different node id
  for the same finding does.
- Clean run closes the issue and every open remediation PR.
- `--dry-run` performs zero git and zero gh calls (assert on the mocked runner).

**Size-cap cases** (§7.1), asserting on rendered length rather than on shape:

- A run of 250 findings renders a body at or under the limit.
- The hidden delta block contains exactly the ids the body rendered — no more, no fewer.
- 5 critical plus 300 minor findings keeps all 5 criticals.
- 10 findings render untruncated, with no "omitted" notice and no trimmed command.
- The clean-run comment stays under the limit with 900 skipped clusters.
- A truncated body reports `partial: false` with empty `coverage_gaps`, and warns in the log —
  truncation is not a coverage gap (§7.3).
- `partial == bool(coverage_gaps)` over both `finish` branches and all three ways a gap arises: clean
  and complete, clean over a `skipped` entry, findings over a `skipped` entry, findings over a
  cluster carrying only a `limitations` note.
- The clean-run comment announces a close only when coverage is complete. Over a gap it says the
  ledger stays open, and it treats a `limitations` note as a gap — the earlier version rendered
  `scope.skipped` alone and posted "closed as completed" onto an issue that stayed open.

**Failure-path cases.** The existing suite has **zero** of these: its mock command recorder returns
exit 0 unconditionally, so not one test exercises a failing `gh` or `git` call. That is not a gap in
coverage of unlikely code, it hides a live defect — `find_existing_issue` and `find_existing_pr`
return `(None, None)` on transport failure, which makes a GitHub outage indistinguishable from "no
issue exists". The run then opens a **duplicate ledger**, or, on a clean run, prints `CLEAN` having
closed nothing. So:

- The recorder gains fault injection: a per-command exit code, stderr, and payload.
- A failing `gh issue list` must not be read as "no ledger exists".
- A failing `gh pr list` must not be read as "no PR on this branch" and must not re-promote.
- A clean run that cannot reach GitHub does not print `CLEAN`.

**Coverage-gap cases** (§7.3): a `scope.skipped` entry and a cluster `limitations` note each set
`partial`; a partial clean run leaves the ledger open, posts the gap comment, closes no PR, and
reports `resolved: 0`; a complete clean run does all four of the opposite things.

**Workspace cases.** Two of these run **real git** against a real bare origin rather than the
recorded runner, because the defect they cover is invisible to a mock: `ensure_workspace` executes
`git clean -fd`, and a mock happily records the command without deleting anything. The tests assert
that an untracked manifest written between `start` and `finish` survives the reattach, that
`reset=True` does remove it, and that `finish` issues neither `git clean` nor `git reset --hard`.
This is worth the cost of a real repository in the suite: the bug it guards against silently emptied
Tier 2 — every manifest the audit wrote would have been deleted moments before the harness looked
for it, and every finding would have been published as "the fix was named but never written".

**Repo-resolution cases** (§13 Q1): the `Git Repo:` line parsed from `https://`, `git@`, and bare
`owner/name` forms; the literal `None` the operator writes for an unset CR field; a missing
`SETTINGS.md` falling back to the git remote; SETTINGS winning when both are present; and an error
that names both sources when neither works. Plus ordering: the repo is resolved before the token is
minted, the token is minted for the resolved repo, and both happen before the clone.

Plus the existing gates: `make docs-generate`, `make docs-check`, `make validate`, `prettier`,
`astro build`, and a Docker build to prove in-image script paths.

## 13. Questions, resolved

**Q1. Does the ledger issue live in the GitOps repo?** `resolve_repo()` derives it from the working
directory's `origin`, which is the GitOps repo — correct for the PRs, but a platform admin may expect
audit issues in an ops/tracking repo instead. If they must differ, `start` needs an explicit
issue-repo argument and the App token needs scope on both.

_Resolved: the GitOps repo._ No new argument, no second token scope, no second place to look.

The divergence this question surfaced — `audit_report.py` deriving the repo from the working
directory's `origin` remote while `github-issue-resolver/scripts/resolver.py` derives it from the
`Git Repo:` line of `/opt/data/SETTINGS.md` — was first recorded as cosmetic and deferred. It was
not cosmetic. Reading the remote requires a working tree, the audit cron does not start in one, and
the clone that would create one needs a repo-scoped token that cannot be minted until the repo is
known. The old resolution was circular and would have failed on the first real run. `resolve_repo()`
now reads `SETTINGS.md` first and falls back to the remote, so the two skills agree by construction
and the repo is knowable before anything has been cloned (§6, `start`).

**Q2. Does the App token already carry `issues: write`?** `github-issue-resolver/scripts/resolver.py`
creates labels, comments, and closes issues with the same token, so issue write is established — but
issue _creation_ has not been exercised. Confirm before Phase 2.

_Resolved: already granted._ The design guessed; source settles it.
`k8s-operator/config/integrations/github/configmap.yaml.template:19` puts `issues: 'write'` in the
`platform-agent-scope` rule, and that directory's `README.md:24` names `Issues: Read & write` among
the App's permissions. Nothing to add.

The published documentation, however, was **stale in four places**, each listing only `contents` and
`pull_requests`: `docs/site/src/content/docs/deploy/token-minter.md:26` and `:61`,
`docs/site/src/content/docs/install/prerequisites.md:89`, and
`docs/site/src/content/docs/concepts/declarative-workflow.md:63`. An operator who followed them
created a GitHub App without issue permission, which makes Minty's scope request unsatisfiable and
fails the ledger at runtime with a 403 — a class of failure the operator cannot debug from the
documentation that caused it. Corrected as part of this change.

**Q3. Interaction with `github-issue-resolver`.** That skill autonomously polls, claims, and resolves
open issues. It must be taught to skip `agent:audit` issues, or it will try to "resolve" every ledger
the audits publish. This is a hard prerequisite, not a follow-up.

_Resolved: yes, and it is a one-token fix._ The `resolver.py` poll query filtered only `status:in-progress`,
`status:escalation-needed`, `agent:ignore`, and `status:resolved`. A ledger issue matched that poll
query on sight: it would be claimed, investigated, and closed as `status:resolved`, so the resolver
would silently eat every ledger the audits publish. `-label:agent:audit` is added to the poll query,
and `agent:audit` is added to that skill's inviolable red line so the exclusion survives a later
rewrite of the query. It lands in Phase 1 (§10), so the exclusion is never absent while ledger issues
exist.

**Q4. Volume ceiling.** Hybrid gating bounds auto-opened PRs to critical manifest findings, but a
genuinely bad fleet day could still open many at once. Consider a per-run cap with the withheld set
named in the ledger.

_Resolved: auto-promotion is capped at five PRs per `finish` run._ Withheld findings are named in the
ledger as awaiting `/remediate`, so nothing is lost, only deferred to a human's judgement about which
five matter first. An explicit `/remediate` is **uncapped** — a human asked for it, and a cap there
would just make them ask again.

**Q5. Who may issue `/remediate`?** §3.1 says what may be promoted but not who may ask. An
unqualified comment trigger is an unauthenticated write path: on a public or widely-collaborated
repo, a comment from a stranger would open branches and PRs in the GitOps repo.

_Resolved: honour the command only from an author whose `authorAssociation` is `OWNER`, `MEMBER`, or
`COLLABORATOR`._ Anyone else gets a single reply saying the command requires write access, recorded
by the `audit-refused` marker of §3.1 so they are not told twice. `gh issue view --json comments`
exposes `authorAssociation` on each comment, so this costs no extra API call.
