---
name: fleet-audit
description: Publish the findings of an autonomous fleet audit as one continuously-rewritten GitHub issue per audit stream, and propose fixes as narrow remediation pull requests.
---

# fleet-audit — Audit Findings to a Ledger Issue

Every autonomous audit watchdog ends the same way: findings must reach a human somewhere durable,
reviewable, and de-duplicated. This skill is that ending, in two tiers:

- **Tier 1 — the ledger.** Each audit stream owns **exactly one open GitHub issue**, rewritten in
  full on every run and closed as completed when the fleet comes back clean. An operator watches one
  issue per stream instead of drowning in chat logs.
- **Tier 2 — the fixes.** When a finding's remediation is a file in this repository, it travels
  separately as a **narrow pull request carrying only that fix**, linked back to the ledger.

The split is the point. A report is not a change, so a report is not a pull request — and a fix is
not a report, so it carries a real diff a reviewer can read in one screen.

`./skills/fleet-audit/scripts/audit_report.py` owns every deterministic operation: credential
minting, label creation, issue creation and rewriting, branch handling, staging, committing,
pushing, pull-request creation, closing, the run-over-run delta, and every timestamp. **Your job is
to inspect the fleet read-only and emit a `findings.json`.** You never hand-write an issue body or a
PR body, never invent a timestamp, and never call `gh issue create` or `gh pr create` yourself —
that is precisely why every ledger looks the same and why the delta between runs is computable.

## Audit streams

Only these five audit ids may own a ledger. Any other id is rejected before a single git or gh
command runs. The issue title is `[audit] <human name> — <n> findings (<c> critical)` (singular
`1 finding` when there is exactly one), where the human name is the one `cron/jobs.json` gives that
watchdog — **not** a prettified form of the audit id:

| Audit id                      | Rendered ledger title                                               |
| ----------------------------- | ------------------------------------------------------------------- |
| `compliance-audit`            | `[audit] Security & RBAC Posture Audit — 7 findings (2 critical)`   |
| `security-patch-orchestrator` | `[audit] Upgrade & Patch Readiness Audit — 7 findings (2 critical)` |
| `obtainability-audit`         | `[audit] Workload Reliability Audit — 7 findings (2 critical)`      |
| `fleet-wide-cost-analysis`    | `[audit] Fleet Waste Audit — 7 findings (2 critical)`               |
| `fleet-consistency-drift`     | `[audit] Fleet Consistency Drift Audit — 7 findings (2 critical)`   |

The mapping lives in `AUDITS` at the top of `audit_report.py` and mirrors `cron/jobs.json`; a test
fails if the two drift apart. Do not restate a title anywhere else.

## The two-command lifecycle

Run both commands from your normal working directory — the profile directory, where `./skills/...`
resolves. **You are not in a git checkout, and you do not need to be.** The
audit crons start in the profile directory; the harness clones the GitOps repository itself, into
`/opt/data/gitops/<audit-id>/<owner>__<name>` on the shared volume, and runs every git and gh call
inside it. The clone is keyed by audit id because the five streams share the volume with each other
and with every kanban worker: each one gets a tree nobody else writes in, so a colliding schedule
can no longer reset another stream's working copy out from under it. The repository comes from the
`Git Repo:` line of `/opt/data/SETTINGS.md`, which the operator writes at provisioning time and
which is readable before any clone exists.

### Step 1 — `start`

Before inspecting anything, claim the workspace:

```bash
./skills/fleet-audit/scripts/audit_report.py start --audit <audit-id>
```

This resolves the target repository, mints a repo-scoped GitHub token, clones or refreshes the
GitOps workspace and leaves it on a clean `main`, ensures the audit's labels exist, locates the
stream's open ledger issue, and clears any findings document a crashed run left behind. It creates
**no branch** — there is no report branch. It prints exactly one JSON line:

```json
{
  "issue": 128,
  "repo": "acme/fleet",
  "workspace": "/opt/data/gitops/compliance-audit/acme__fleet",
  "findings_path": "/opt/data/scratch/findings_compliance-audit.json",
  "pending_remediation_requests": ["netpol-missing-payments"]
}
```

Write your findings to the `findings_path` it gives you. Do not pick your own path.

`workspace` is the clone. **Every `remediation.path` is resolved against it**, so a manifest written
anywhere else is a file the harness will never find — the finding degrades to a manual one and no
pull request opens. `start` scrubs that directory before handing it to you; `finish` does not, which
is what lets the files you write in between survive.

`pending_remediation_requests` lists the findings a repository writer has already asked to be fixed,
parsed from the ledger's comments. **Write those manifests during inspection** — if the finding is
still reproducing at `finish`, its pull request opens immediately instead of a week later.

### Step 2 — Inspect the fleet (reasoning phase)

Enumerate the clusters in scope and inspect them **read-only** (`kubectl get/describe`,
`gcloud ... describe/list`). For every deviation you intend to report, capture the exact command you
ran and the output that proves it. Keep a per-cluster tally of which of your SOP's checks you have
actually run as you go — `finish` requires it as `checks_run`, and reconstructing it afterwards from
memory is how a check that never ran gets recorded as one that did.

If a remediation is a declarative file, write that file **under the `workspace` directory `start`
reported** and name its repo-relative path in the finding. The harness puts it on a branch of its
own.

**Do not leave unrelated uncommitted work in that tree during an audit.** Opening a remediation pull
request requires switching branches, and the harness forces the switch. It snapshots and restores
every path you declared, and returns you to the branch you started on — but a file it was never told
about is not covered by that guarantee.

### Step 3 — `finish`

```bash
./skills/fleet-audit/scripts/audit_report.py finish --audit <audit-id> --findings-file <findings_path>
```

The script validates the document, reconciles every finding against the pull requests already open
for this stream, rewrites (or opens) the ledger issue, comments the delta, opens pull requests for
the fixes that qualify, and closes the ones whose findings have stopped reproducing. It prints one
JSON line with eight fields — `status`, `issue_url`, `new`, `resolved`, `prs_opened`, `prs_closed`,
`partial`, and `coverage_gaps`:

- `{"status":"OPENED","new":7,"resolved":0,"prs_opened":["…"],"prs_closed":[],"partial":false,"coverage_gaps":[]}`
  — the stream had no open ledger.
- `{"status":"UPDATED","new":2,"resolved":3,"prs_opened":[],"prs_closed":["…"],"partial":false,"coverage_gaps":[]}`
  — the existing ledger was rewritten.
- `{"status":"CLEAN","new":0,"resolved":5,"prs_opened":[],"prs_closed":["…"],"partial":false,"coverage_gaps":[]}`
  — zero findings; the ledger closed as completed and its open fixes closed with it.

Add `--dry-run` to validate and print the rendered ledger body — and every PR body it _would_ open —
to stdout with **zero** git or gh side effects. It applies the same grouping and the same
degradation as the real run, so the branch names it names are the branch names it would create. It
resolves every `remediation.path` against the same `workspace` clone the real run uses, not against
the directory you happen to be standing in, so "the manifest is missing" is a finding of the dry run
and not a surprise at publish time. Use it whenever you are unsure your document is well formed.

Exit 0 means published. **Exit 2 means the run was rejected before publishing anything** — fix what
the message names and re-run; never delete the finding that tripped it. Three things reach exit 2:
the document failed a field rule, the file named by `--findings-file` is missing or is not valid
JSON, or `--audit` is not one of the five ids above. Exit 1 is fatal and means something else broke.

### Partial coverage

`partial` is `true` exactly when the run could not speak for the whole fleet: any entry in
`scope.skipped`, any cluster carrying a `limitations` note, or any cluster whose `checks_run` is
short of its SOP's roster. `coverage_gaps` says which, and why — so `partial` is `true` if and only
if `coverage_gaps` is non-empty, and you can report from either.

It does not mean "the description was truncated." A ledger too long for GitHub's body limit says so
in its own body and still carries true totals in its title; the audit saw everything, so nothing
about what the run may conclude changes. Coverage is the only thing `partial` tracks.

A gap changes what the run is _allowed to conclude_, because a finding's absence from an unread
cluster is not evidence that it was fixed. Over a partial run the harness:

- reports `resolved: 0` and posts no "resolved" delta, rather than announcing fixes it cannot see;
- closes **no** remediation pull request as stale, so a fix survives to the next complete run;
- does **not** close the ledger, even with zero findings — `status` is still `CLEAN`, but the issue
  stays open and gains a comment naming the gaps. The stream self-heals the day the fleet is fully
  readable again.

A partial run is never `[SILENT]`. Report the issue URL and say which clusters were not covered.

## The findings document

```json
{
  "audit": "compliance-audit",
  "scope": {
    "clusters": [
      {
        "name": "prod-us-east",
        "location": "us-east1",
        "project": "acme-prod",
        "checks_run": [
          "privileged-container",
          "host-namespace",
          "hostpath-mount",
          "cluster-admin-binding",
          "wildcard-rbac",
          "netpol-missing",
          "default-sa-automount",
          "workload-identity-off",
          "legacy-metadata",
          "public-control-plane",
          "podsecurity-gaps"
        ]
      },
      {
        "name": "prod-autopilot",
        "location": "us-central1",
        "project": "acme-prod",
        "checks_run": [
          "cluster-admin-binding",
          "wildcard-rbac",
          "netpol-missing",
          "default-sa-automount",
          "workload-identity-off",
          "public-control-plane",
          "podsecurity-gaps"
        ],
        "limitations": "Autopilot: node-level checks 2.1-2.3 and 2.9 do not apply."
      }
    ],
    "skipped": [{ "cluster": "dr-west", "reason": "control plane unreachable" }]
  },
  "findings": [
    {
      "id": "netpol-missing-payments",
      "severity": "critical",
      "title": "payments namespace has no NetworkPolicy",
      "cluster": "prod-us-east",
      "namespace": "payments",
      "object": "Namespace/payments",
      "evidence": {
        "command": "kubectl --context prod-us-east get networkpolicy -n payments",
        "excerpt": "No resources found in payments namespace."
      },
      "impact": "All east-west traffic into the PCI namespace is unrestricted.",
      "recommendation": {
        "action": "Apply a namespace default-deny NetworkPolicy, then allow the two known callers.",
        "rationale": "Default-deny at the namespace is the smallest change that closes the exposure. A mesh AuthorizationPolicy would only cover injected pods, and payments runs two that are not.",
        "risk": "Unlabelled cross-namespace traffic breaks on apply. Run `kubectl -n payments get pods --show-labels` first to confirm the callers."
      },
      "remediation": {
        "kind": "manifest",
        "path": "clusters/prod-us-east/payments-netpol.yaml",
        "note": "Apply a default-deny NetworkPolicy."
      }
    }
  ]
}
```

Field rules the validator enforces — a violation exits 2 naming the offending finding index and
field, and publishes nothing:

- `audit` must equal the `--audit` argument. An audit may only write to its own ledger.
- `scope.clusters` must be **non-empty**. An audit that enumerated nothing is a failure, not a clean
  run — if you could not list the fleet, say so loudly instead of reporting zero findings.
- `checks_run` is **required on every cluster**: the list of checks that actually ran against it,
  named by the backticked slug in the SOP heading that defines them (`netpol-missing`, not "2.6" and
  not prose). An unknown slug, a duplicate, or a missing field is rejected, and so is an empty list
  unless that cluster's `limitations` says why nothing ran. Enumerating a cluster and checking
  nothing on it is not a clean cluster — it is an audit that did not happen, and without this field
  the harness cannot tell the two apart. See [Scope, skipped, and limitations](#scope-skipped-and-limitations).
- `id` is a stable slug, unique within the file, matching `^[a-z0-9]([a-z0-9._-]{0,98}[a-z0-9])?$` with
  no `..` run and no `.lock` suffix. Two rules ride on this. **Stability is what makes the delta
  work**: the same underlying problem must produce the same id on every run, or it will churn as
  "resolved" then "new" forever — derive it from the cluster/namespace/object, never from a
  timestamp, counter, or run id. **The charset is narrow** because the id is the join key of the
  ledger's hidden delta block and of the `audit-persists:<id>` marker — both line-anchored regexes a
  space or a newline would break — and because an operator types it by hand in `/remediate <id>`.
  Keep ids short: a 100-character id is legal, and unreadable.
- `severity` is one of `critical`, `major`, `minor`.
- `namespace` may be empty for cluster-scoped objects.
- `evidence.command` is **required and non-empty**.
- `recommendation` is **required on every finding**, with all three of `action`, `rationale`, and
  `risk` non-empty. See below.
- `remediation.kind` is `manifest`, `gcloud`, or `manual`. `path` is required for `manifest`
  (repo-relative, no `..`, no absolute paths, no glob metacharacters) and forbidden for the other
  two. For `gcloud`, put the exact command in `note` — it is rendered as a runnable block.

### Scope, skipped, and limitations

**A cluster appears in exactly one scope list.** Ask one question:

> Could you read it? **Yes** → `scope.clusters`; if some checks did not run or do not apply there,
> name them in that cluster's `limitations`. **No** → `scope.skipped`, with a reason.

Nothing goes in both, and nothing in `scope.skipped` may appear in a finding. The validator enforces
both halves. This matters because the alternative produces **false all-clears**: put an Autopilot
cluster in `scope.skipped` because one node-level check cannot apply there, and every real finding
on a cluster you did audit gets suppressed along with it.

`limitations` is optional, and non-empty when present. The rendered scope table grows a
`limitations` column only when at least one cluster carries one.

**`checks_run` is not optional, and it is what the scope table counts.** Every cluster carries the
list of checks that ran against it; the table renders it as `7/11`, marked `⚠` where it falls short,
on every run whether or not anything was missed — a column that only appears on bad days is a column
nobody reads on good ones. A shortfall is a coverage gap in its own right: it makes the run
`partial` exactly as an unreadable cluster does, is named in `coverage_gaps`, and so the ledger will
not close on it. That is the point. A run that skipped eight of eleven checks and found nothing has
not found nothing; it has not looked, and before this field existed it published as `CLEAN` and
closed the ledger.

Which means the one way to defeat all of this is to write a slug for a check you did not run. The
harness cannot see the commands you issued, only the list you hand it, so an inflated `checks_run`
converts a partial audit straight back into a false all-clear. Add each slug as its check completes,
never in advance, and never round the list up to the roster because the SOP happens to define that
many.

### `recommendation`

Three fields, all required, all load-bearing for the human who has to decide:

- **`action`** — what to do. Imperative, one or two sentences.
- **`rationale`** — why _this_ fix and not the obvious alternative. **Name the alternative you
  considered and why you rejected it.** A rationale that restates the action is not a rationale.
- **`risk`** — what breaks on apply, and the read-only check to run first.

## Evidence rules

**A finding with no reproducible command is dropped, not softened.** If you cannot produce the exact
read-only command that a reviewer can paste into a terminal to see the same thing you saw, the
finding does not go in the file. Do not downgrade it to `minor`, do not hedge the title, do not write
"appears to". Omit it.

Corollaries:

- `evidence.excerpt` is the real output, copied. Never paraphrase it and never synthesise
  plausible-looking output. The harness trims long excerpts and long commands for you.
- **Never paste a Secret's `data:`, a token, a password, or a private key into an excerpt.** Report
  that the Secret exists and what is wrong with it; the command in `evidence.command` is how a
  reviewer sees the rest, under their own credentials.

  The harness redacts high-confidence credential shapes as a backstop — a `data:`/`stringData:`
  block, a field named like a secret, a self-identifying token prefix, a PEM header, an
  `Authorization:` value — replacing them with `[redacted by audit_report.py]`. It is deliberately
  conservative and **does not** touch bare base64, because legitimate audit output is full of it.
  Treat the backstop as a seatbelt, not a licence: it will not catch a credential that looks like
  ordinary output.

- Report what the command showed, not what you infer it implies. Inference belongs in `impact`.
- One finding per object. Do not roll up "12 namespaces lack NetworkPolicies" into one finding — each
  gets its own stable id so each can resolve independently.

## What the ledger says about each finding

Every finding renders in exactly one state, computed fresh each run from whether it still reproduces
and what pull request sits on its branch. Nothing is stored between runs.

| State                | Rendered as                           | Meaning                                    | What the harness does                                        |
| -------------------- | ------------------------------------- | ------------------------------------------ | ------------------------------------------------------------ |
| `open`               | `open`                                | Reproduces; no pull request                | Nothing, unless it qualifies for auto-promotion              |
| `pr-open`            | `fix proposed`                        | Reproduces; a fix is open on its branch    | **Nothing.** The pull request is left alone                  |
| `pr-merged-persists` | `⚠ fix merged, still reproduces`      | Reproduces; the fix **merged anyway**      | Comments once on the merged PR; never reopens it             |
| `refused`            | `fix refused`                         | Reproduces; a **human closed** the fix     | Nothing. The close stands until someone says `/remediate`    |
| `withdrawn`          | `fix withdrawn, awaiting re-proposal` | Reproduces; the **harness closed** the fix | Treats it as having no pull request — it is promotable again |

Every row above says "reproduces", and that is not an accident: **a finding that stopped reproducing
is not in the document at all**, so it has no row in the ledger to carry a state. Two further states
exist in the code — `resolved` and `resolved-merged` — but neither is ever rendered here. A
resolution is announced in the delta comment, by id and title recovered from the previous body, and
the finding's open pull request is closed as stale. A resolution whose fix had already **merged** is
the ordinary, expected ending, so nothing extra is closed and nothing extra is said.

Three of the five are easy to misread:

- **`pr-open` is not refreshed.** An open pull request is left exactly as it is, because a reviewer
  may have pushed onto it and a nightly force-push would silently discard their work. The ledger
  links it; the diff is whatever a human last made it.
- **`refused` is a human decision, not a rejected command.** It means someone closed the remediation
  pull request without merging it. That is a considered "no", and the harness never overrules it by
  re-opening the same fix tomorrow morning.
- **`withdrawn` is the other half of that.** A closed unmerged pull request is two different events,
  and the discriminator is the `audit:stale-closed` label the harness applies when it closes one as
  stale. Its finding is promotable again on the usual terms; a `refused` one is not. Do not strip
  that label — without it the close reads as a human rejection and the finding is never re-proposed.

The escape hatch for a `refused` finding is `/remediate <id>` from someone with write access, and it
must be written **after** the close. An older command in the thread is reported as _superseded_
rather than honoured: comments are never edited away, so a March request would otherwise re-open an
April close every morning forever. Post a fresh one.

`pr-merged-persists` is the state worth reading twice: a fix merged and the deviation is still
there. Either the remediation was incomplete or something outside this repository reverted it.

## Remediation pull requests

A pull request is opened for a finding only when its remediation is a `manifest` — there is nothing
to put in a diff otherwise. Two paths lead there:

- **Auto-promotion.** A finding that is `critical`, is a `manifest`, and has no live pull request on
  its branch is promoted automatically by `finish` — **at most five per run**. The surplus is named
  in the ledger as awaiting `/remediate`, so nothing is silently dropped. "Live" excludes a pull
  request the harness itself closed as stale (that one is re-openable) and includes one a human
  closed or merged (those are not).
- **`/remediate <finding-id>`**, or `/remediate all`, commented on the ledger by someone with write
  access to the repository. This path is uncapped: a human asked for that one by name.

Every `/remediate` gets exactly one answer, and the answer is never silence:

- Accepted — one acknowledgement comment on the ledger naming each target and **what happened to
  it**, never a count: the pull request URL, or "already open" and left untouched, or _superseded_
  by a human close written after the request, or that publishing failed and the next run will retry.
  "3 requests processed" is indistinguishable from "3 requests silently dropped".
- Refused — one reply saying why, for a commenter without write access, a `/remediate` naming a
  finding that is not in the current document, or one naming a non-`manifest` finding.
- Refused **on syntax**, likewise once, because a command the parser will not honour is a person
  waiting for a fix that is never coming. `/remediate` is only read at the start of its own line, so
  one written mid-sentence gets a reply pointing that out; one written with no target at all gets a
  reply too, because reading it as `all` would open every promotable pull request the cap allows on
  someone who typed the command and then went to look up the id. Both replies carry the correct
  syntax and the promotable ids — up to ten, then "and N more", since a refusal is help and not a
  second copy of the report.
- Overtaken by a **clean run** — answered anyway, and answered _before_ the ledger closes. A run that
  finds nothing still replies to every unanswered `/remediate` in the thread to say the finding no
  longer reproduces, and whether the ledger is closing or staying open on partial coverage. This is
  the one morning the issue disappears, taking with it the thread the requester would have re-asked
  on, so it is the one morning silence is least affordable. Authorization is not consulted here:
  nothing is being acted on for anybody, and "it no longer reproduces" is equally true and equally
  useful to a commenter without write access.

Two deliberate silences. A mid-sentence `/remediate` from someone _without_ write access gets
nothing: their correctly-typed command would have been refused anyway, and two replies to one
comment that was probably never a command is a bot picking an argument. And a `/remediate` inside a
code span is prose about the command, not an attempt at it — which is why every `/remediate` the
harness itself writes into a comment is backticked. Keep it that way when you quote one: an
unbackticked command in a harness-authored comment is read back on the next run, and a bot that
answers itself never stops.

All of these are guarded by a hidden marker carrying the triggering comment's node id, so a standing
`/remediate` in the thread is answered once rather than every morning forever.

Run the requested targets through the subcommand, which takes `--finding` once per id:

```bash
./skills/fleet-audit/scripts/audit_report.py remediate --audit <audit-id> \
  --findings-file <findings_path> --finding <id> [--finding <id> …] [--issue <n>]
```

**It opens exactly what you name, and nothing else.** The auto-promotion sweep does not ride along:
one `--finding` produces one pull request (or one, shared, for the group that path belongs to), never
five more for critical findings the requester never mentioned and cannot tell apart from the one they
did. Auto-promotion happens in `finish`, where the whole fleet is being reported on anyway.

It prints one JSON line — `status`, `prs_opened`, `already_open`, and `refused`:

- `{"status":"REMEDIATED","prs_opened":["…"],"already_open":["cluster-old"],"refused":["ns-quota"]}`

`refused` names the targets whose remediation is not a readable file inside the clone — either the
audit promised a manifest and never wrote it, or the path does not resolve inside the repository at
all. Both leave nothing to put in a diff; a `SECURITY:` line in the log says which one happened. The
other targets still open — `/remediate all` expands to every **manifest-remediation** id in the
document, and failing the batch over one unwritten file would answer a request for many fixes with
none. Say which were refused when you acknowledge the command.

Exit 2 means nothing was published, for one of three reasons — read the message before reporting
which: a named id is not in the document at all, a named target is not a `manifest`, or _every_
named target was refused because its file is not readable inside the clone. The first two are fixed
by dropping the bad id and asking again; only the third is about writing manifests.

**Findings whose remediation paths intersect share one pull request.** They have to: separate
branches touching the same file conflict on merge. Promoting any member promotes the whole group —
the pull request names every member. That is why several findings can point at one shared manifest
and still produce one clean diff.

The group's branch is `platform-agent/fix-<audit-id>-<slug>-<digest>`, where the digest is over the
group's **sorted path set** and the slug is a readable fragment of the first path. It is keyed on
the files, not on a finding id, and that is load-bearing: ids are regenerated every run, so a branch
named after one of them gets renamed the day that finding resolves — orphaning the open pull request
and opening a duplicate against the same file.

The branch name is the only join key. There is no state file: `finish` reconstructs the entire
finding-to-pull-request mapping from one `gh pr list` call.

## Size

GitHub caps an issue body at 65,536 characters, and a pull request body at the same. The harness
targets 60,000 and will truncate the ledger's findings section to stay under it. Three consequences
you must not work around:

- **Findings are rendered severity-first**, so truncation only ever eats the least-severe end.
  Criticals are structurally safe.
- **The title's counts stay true.** If the body omits findings it says so explicitly. Never
  hand-trim your document to make it fit — the counts are how a reader learns the real total.
- **Truncation does not make a run `partial`** — see [Partial coverage](#partial-coverage), which
  owns that rule.

Every free-text field is clipped on the way out — title 300 characters, impact and each
`recommendation` sub-field 1,500, `remediation.note` 2,000, `evidence.command` 2,000,
`evidence.excerpt` **40 lines and** 2,000 characters, whichever it hits first, and
`cluster` / `namespace` / `object` 320. That last group is not a style rule. The renderer always
emits the **first** finding whatever it costs, so before those three were clipped one oversized
identifier on one finding could overflow the whole body and publish nothing at all, every morning,
until that finding stopped reproducing.

Resolution accounting is unaffected by truncation, because the two halves of the delta are measured
against different sets: **new** is judged against what the body rendered, and **resolved** against
every finding in the document, rendered or not. A finding cut for space still reproduces and is
never reported as fixed.

## The clean run

If the audit finds nothing, still call `finish` with `"findings": []` and a populated
`scope.clusters`. With complete coverage the harness answers any `/remediate` still standing
unanswered in the thread, comments the date and the clusters covered, closes the ledger issue **as
completed**, and closes every remediation pull request still open for the stream. The answers come
first, deliberately: a reply posted after the close would land on an issue nobody is watching.

A clean run is usually not news, and the closed issue is the record — but "clean" alone does not
decide it. Read the `finish` JSON and apply the full rule:

> **`[SILENT]` iff `new == 0` and `resolved == 0` and `partial == false`.**

If any of the three fails, report the issue URL and a one-line summary. Two clean runs are _not_
silent, and both matter:

- **`resolved > 0`** — the fleet was carrying findings yesterday and is not today. Something got
  fixed, and that is the best thing this audit ever gets to say. Reporting `partial` failures while
  swallowing this one would leave the operator hearing only bad news.
- **`partial: true`** — the ledger stayed open because the fleet was not fully read. "I found
  nothing" and "I could not look" must not arrive as the same silence.

There is one case where the harness reports `new: 0, resolved: 0` without knowing it: if the
previous ledger body could not be read, the delta is unknowable, so it announces nothing rather than
declaring every live finding new. The run logs
`Previous ledger body was unreadable; skipping the delta comment` to stderr and the ledger is still
rewritten correctly. Treat it as `[SILENT]` — the issue carries the truth either way.

## Red lines

- **Read-only against clusters.** An audit inspects; it never mutates a cluster. Remediation is
  proposed as a file in a pull request or as a command for a human to run, never executed.
- **Never `git add .` or `git add -A`.** The harness stages only the distinct paths you named in
  `remediation.path`, through `git --literal-pathspecs`, and refuses glob metacharacters in a path
  outright. Do not run your own `git add`.
- **Every `remediation.path` stays inside the clone, and the harness proves it twice.** The string
  must be repo-relative with no `..`, no glob metacharacter, and no leading `:` — and before the
  file is read or staged it is re-resolved against the `workspace` root, where no path component may
  be a symlink and the resolved path must sit under the resolved root. Do not create a symlink in
  the clone and point a remediation through it: `manifests/vendor/x.yaml` is beyond reproach until
  `manifests/vendor` is a link to `/etc`, and then the contents of a file outside the repository are
  committed to a public pull request. Nothing is read from a path that fails either test. The
  finding degrades to `manual` with a note saying so, the run logs a `SECURITY:` line naming the
  path, and the report still publishes — but no pull request opens for that finding until the path
  is a real file inside the clone.
- **Never open a second ledger issue for a stream.** Do not call `gh issue create`. If the stream
  already has an open ledger, `finish` rewrites it in place; that is the whole point.
- **Never open a remediation pull request yourself**, and never for a non-`manifest` finding.
- **Never reopen a merged remediation pull request.** A persisting finding gets a comment and a
  ledger state, not a resurrection.
- **Never delete a remediation branch.** The harness closes stale pull requests and leaves the
  branch: if the finding comes back, the fix is pushed there again.
- **Never force-push a protected branch.** `main`, `master`, and `production` are refused.
- **Never hand-write a body, title, commit message, or timestamp.** They are generated so that the
  diff between two runs is meaningful.
- **Write every `manifest` remediation file before calling `finish`**, under the `workspace`
  directory. A path that is not on disk does not fail the run — that one finding degrades to
  `manual`, keeps its evidence and recommendation, and says in the ledger that the fix was named but
  not written. The report still publishes. Do not rely on this: a degraded finding is a fix a human
  now has to apply by hand.
- **Never report a cluster you could not read as clean.** Put it in `scope.skipped`, or name what
  did not run in that cluster's `limitations`. Both make the run `partial`, which is the mechanism
  that stops the harness from closing fixes and retiring the ledger on evidence it never gathered.
- **Never name a check in `checks_run` that you did not run.** It is the only claim in the document
  the harness has to take on trust, and padding it turns every protection above back off: the run
  stops being `partial`, the ledger closes, and a fleet nobody looked at publishes as clean.
