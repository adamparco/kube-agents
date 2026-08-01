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

Run both commands from your normal working directory — the same one `submit-suggestion` assumes,
where `./skills/...` resolves and `git` already addresses the GitOps repository. The script resolves
the target repository from that directory's `origin` remote and performs every git operation there.
Do not `cd` elsewhere between `start` and `finish`.

### Step 1 — `start`

Before inspecting anything, claim the workspace:

```bash
./skills/fleet-audit/scripts/audit_report.py start --audit <audit-id>
```

This refreshes GitHub credentials, resolves the target repository, ensures the audit's labels exist,
locates the stream's open ledger issue, and clears any findings document a crashed run left behind.
It creates **no branch** — there is no report branch. It prints exactly one JSON line:

```json
{
  "issue": 128,
  "repo": "acme/fleet",
  "findings_path": "/opt/data/scratch/findings_compliance-audit.json",
  "pending_remediation_requests": ["netpol-missing-payments"]
}
```

Write your findings to the `findings_path` it gives you. Do not pick your own path.

`pending_remediation_requests` lists the findings a repository writer has already asked to be fixed,
parsed from the ledger's comments. **Write those manifests during inspection** — if the finding is
still reproducing at `finish`, its pull request opens immediately instead of a week later.

### Step 2 — Inspect the fleet (reasoning phase)

Enumerate the clusters in scope and inspect them **read-only** (`kubectl get/describe`,
`gcloud ... describe/list`). For every deviation you intend to report, capture the exact command you
ran and the output that proves it.

If a remediation is a declarative file, write that file into the repository working tree now and
name its repo-relative path in the finding. The harness puts it on a branch of its own.

**Do not leave unrelated uncommitted work in the tree during an audit.** Opening a remediation pull
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
JSON line:

- `{"status":"OPENED","issue_url":"…","new":7,"resolved":0,"prs_opened":["…"],"prs_closed":[]}` —
  the stream had no open ledger.
- `{"status":"UPDATED","issue_url":"…","new":2,"resolved":3,"prs_opened":[],"prs_closed":["…"]}` —
  the existing ledger was rewritten.
- `{"status":"CLEAN","issue_url":"…","new":0,"resolved":5,"prs_opened":[],"prs_closed":["…"]}` —
  zero findings; the ledger closed as completed and its open fixes closed with it.

Add `--dry-run` to validate and print the rendered ledger body — and every PR body it _would_ open —
to stdout with **zero** git or gh side effects. Use it whenever you are unsure your document is well
formed.

Exit 0 means published. **Exit 2 means the validator rejected the document and nothing was
published** — fix the document and re-run; never delete the finding that tripped it. Exit 1 is
fatal.

## The findings document

```json
{
  "audit": "compliance-audit",
  "scope": {
    "clusters": [
      {
        "name": "prod-us-east",
        "location": "us-east1",
        "project": "acme-prod"
      },
      {
        "name": "prod-autopilot",
        "location": "us-central1",
        "project": "acme-prod",
        "limitations": "Autopilot: node-level checks 2.6 and 2.7 do not apply."
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
- `id` is a stable slug, unique within the file, matching `^[a-z0-9][a-z0-9._-]{0,98}[a-z0-9]$` with
  no `..` run and no `.lock` suffix. Two rules ride on this. **Stability is what makes the delta
  work**: the same underlying problem must produce the same id on every run, or it will churn as
  "resolved" then "new" forever — derive it from the cluster/namespace/object, never from a
  timestamp, counter, or run id. And the id becomes a **git branch name component**, which is why
  the charset is narrow. Keep ids short: a 100-character id is legal, and unreadable.
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
- Report what the command showed, not what you infer it implies. Inference belongs in `impact`.
- One finding per object. Do not roll up "12 namespaces lack NetworkPolicies" into one finding — each
  gets its own stable id so each can resolve independently.

## What the ledger says about each finding

Every finding renders in exactly one state, computed fresh each run from whether it still reproduces
and what pull request sits on its branch. Nothing is stored between runs.

| State                | Meaning                                               | What the harness does                                                |
| -------------------- | ----------------------------------------------------- | -------------------------------------------------------------------- |
| `open`               | Reproduces; no pull request                           | Nothing, unless it qualifies for auto-promotion                      |
| `pr-open`            | Reproduces; a fix is open on its branch               | Refreshes that pull request                                          |
| `pr-merged-persists` | Reproduces; the fix **merged anyway**                 | Comments once on the merged PR; never reopens it                     |
| `resolved`           | Stopped reproducing                                   | Drops it from the ledger, names it in the delta, closes any open fix |
| `refused`            | A `/remediate` named it but the harness would not act | Replies once, saying why                                             |

`pr-merged-persists` is the state worth reading twice: a fix merged and the deviation is still
there. Either the remediation was incomplete or something outside this repository reverted it.

## Remediation pull requests

A pull request is opened for a finding only when its remediation is a `manifest` — there is nothing
to put in a diff otherwise. Two paths lead there:

- **Auto-promotion.** A finding that is `critical`, is a `manifest`, and has no pull request on its
  branch in any state is promoted automatically by `finish` — **at most five per run**. The surplus
  is named in the ledger as awaiting `/remediate`, so nothing is silently dropped.
- **`/remediate <finding-id>`**, or `/remediate all`, commented on the ledger by someone with write
  access to the repository. This path is uncapped: a human asked for that one by name. A request
  from a commenter without write access, or naming a non-`manifest` finding, gets one reply
  explaining why and no pull request.

**Findings whose remediation paths intersect share one pull request.** They have to: separate
branches touching the same file conflict on merge. The group's branch is
`platform-agent/fix-<audit-id>-<lowest-finding-id>`, and promoting any member promotes the whole
group — the pull request names every member. That is why several findings can point at one shared
manifest and still produce one clean diff.

The branch name is the only join key. There is no state file: `finish` reconstructs the entire
finding-to-pull-request mapping from one `gh pr list` call.

## Size

GitHub caps an issue body at 65,536 characters, and a pull request body at the same. The harness
targets 60,000 and will truncate the ledger's findings section to stay under it. Two consequences
you must not work around:

- **Findings are rendered severity-first**, so truncation only ever eats the least-severe end.
  Criticals are structurally safe.
- **The title's counts stay true.** If the body omits findings it says so explicitly. Never
  hand-trim your document to make it fit — the counts are how a reader learns the real total.

## The clean run

If the audit finds nothing, still call `finish` with `"findings": []` and a populated
`scope.clusters`. The harness comments the date and the clusters covered, closes the ledger issue
**as completed**, and closes every remediation pull request still open for the stream.

Then your final response **MUST be exactly `[SILENT]`**. A clean audit is not news; the closed issue
is the record. Say nothing in chat.

`UPDATED` with `new: 0` **and** `resolved: 0` is also exactly `[SILENT]` — nothing moved since the
last run, and the ledger already says everything you would. If **either** counter is non-zero,
report the issue URL and a one-line summary.

## Red lines

- **Read-only against clusters.** An audit inspects; it never mutates a cluster. Remediation is
  proposed as a file in a pull request or as a command for a human to run, never executed.
- **Never `git add .` or `git add -A`.** The harness stages only the distinct paths you named in
  `remediation.path`, through `git --literal-pathspecs`, and refuses glob metacharacters in a path
  outright. Do not run your own `git add`.
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
- **A `manifest` remediation path must exist on disk** when `finish` runs. Write the file first. A
  missing path is a hard error, not a warning.
