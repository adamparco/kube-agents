---
name: fleet-audit
description: Publish the findings of an autonomous fleet audit as one continuously-updated GitHub Pull Request per audit stream.
---

# fleet-audit — Audit Findings to a Durable Pull Request

Every autonomous audit watchdog ends the same way: findings must reach a human somewhere
durable, reviewable, and de-duplicated. This skill is that ending. It gives each audit stream
**exactly one open Pull Request** that is rewritten in place on every run, so an operator can
watch a single PR instead of drowning in chat logs.

`./skills/fleet-audit/scripts/audit_pr.py` owns every deterministic operation: credential
minting, branch handling, label creation, staging, committing, force-pushing, PR creation and
editing, the run-over-run delta, and every timestamp. **Your job is to inspect the fleet
read-only and emit a `findings.json`.** You never hand-write a PR body, never invent a
timestamp, and never call `gh pr create` yourself — that is precisely why every audit PR looks
the same and why the delta between runs is computable.

## Audit streams

Only these five audit ids may own a PR. Any other id is rejected before a single git or gh
command runs. The PR title is `[audit] <human name> — <n> findings (<c> critical)` (singular
`1 finding` when there is exactly one), where the human name is the one `cron/jobs.json` gives
that watchdog — **not** a prettified form of the audit id:

| Audit id                      | Rendered PR title                                                   |
| ----------------------------- | ------------------------------------------------------------------- |
| `compliance-audit`            | `[audit] Security & RBAC Posture Audit — 7 findings (2 critical)`   |
| `security-patch-orchestrator` | `[audit] Upgrade & Patch Readiness Audit — 7 findings (2 critical)` |
| `obtainability-audit`         | `[audit] Workload Reliability Audit — 7 findings (2 critical)`      |
| `fleet-wide-cost-analysis`    | `[audit] Fleet Waste Audit — 7 findings (2 critical)`               |
| `fleet-consistency-drift`     | `[audit] Fleet Consistency Drift Audit — 7 findings (2 critical)`   |

The mapping lives in `AUDITS` at the top of `audit_pr.py` and mirrors `cron/jobs.json`; a test
fails if the two drift apart. Do not restate a title anywhere else.

## The two-command lifecycle

Run both commands from your normal working directory — the same one `submit-suggestion` assumes,
where `./skills/...` resolves and `git` already addresses the GitOps repository. The script
resolves the target repository from that directory's `origin` remote and performs every git
operation there. Do not `cd` elsewhere between `start` and `finish`.

### Step 1 — `start`

Before inspecting anything, claim the workspace:

```bash
./skills/fleet-audit/scripts/audit_pr.py start --audit <audit-id>
```

This refreshes GitHub credentials, resolves the target repository, ensures the audit's labels
exist, resets `platform-agent/audit-<audit-id>` onto the latest `main`, and locates the audit's
existing open PR. It prints exactly one JSON line:

```json
{
  "branch": "platform-agent/audit-compliance-audit",
  "existing_pr": 42,
  "repo": "acme/fleet",
  "findings_path": "/opt/data/scratch/findings_compliance-audit.json"
}
```

Write your findings to the `findings_path` it gives you. Do not pick your own path.

### Step 2 — Inspect the fleet (reasoning phase)

Enumerate the clusters in scope and inspect them **read-only** (`kubectl get/describe`,
`gcloud ... describe/list`). For every deviation you intend to report, capture the exact command
you ran and the output that proves it.

If a remediation is a declarative file, write that file into the repository working tree now,
while you are on the audit branch, and name its repo-relative path in the finding. The harness
stages it for you.

### Step 3 — `finish`

```bash
./skills/fleet-audit/scripts/audit_pr.py finish --audit <audit-id> --findings-file <findings_path>
```

The script validates the document, stages only the named remediation files, commits with a
Conventional Commit subject, force-pushes the audit branch, renders the PR body, and then either
opens the stream's PR or edits the existing one in place and comments with the delta. It prints
one JSON line:

- `{"status":"OPENED","pr_url":"…","new":7,"resolved":0}` — the stream had no open PR.
- `{"status":"UPDATED","pr_url":"…","new":2,"resolved":3}` — the existing PR was rewritten.
- `{"status":"CLEAN","pr_url":"…","new":0,"resolved":5}` — zero findings; the PR was closed.

Add `--dry-run` to validate and print the rendered body to stdout with **zero** git or gh side
effects. Use it whenever you are unsure your document is well formed.

## The findings document

```json
{
  "audit": "compliance-audit",
  "scope": {
    "clusters": [
      { "name": "prod-us-east", "location": "us-east1", "project": "acme-prod" }
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
      "remediation": {
        "kind": "manifest",
        "path": "clusters/prod-us-east/payments-netpol.yaml",
        "note": "Apply a default-deny NetworkPolicy."
      }
    }
  ]
}
```

Field rules the validator enforces — a violation exits non-zero naming the offending finding
index and field, and publishes nothing:

- `audit` must equal the `--audit` argument. An audit may only write to its own PR stream.
- `scope.clusters` must be **non-empty**. An audit that enumerated nothing is a failure, not a
  clean run — if you could not list the fleet, say so loudly instead of reporting zero findings.
- `scope.skipped` is where partial coverage is declared. Every cluster you could not reach goes
  here with a reason; the PR then states plainly that coverage was partial.
- `id` is a stable slug, unique within the file. **Stability is what makes the delta work**:
  the same underlying problem must produce the same id on every run, or it will churn as
  "resolved" then "new" forever. Derive it from the cluster/namespace/object, never from a
  timestamp, counter, or run id.
- `severity` is one of `critical`, `major`, `minor`.
- `namespace` may be empty for cluster-scoped objects.
- `evidence.command` is **required and non-empty**.
- `remediation.kind` is `manifest`, `gcloud`, or `manual`. `path` is required for `manifest`
  (repo-relative, no `..`, no absolute paths) and forbidden for the other two. For `gcloud`,
  put the exact command in `note` — it is rendered as a runnable block.

## Evidence rules

**A finding with no reproducible command is dropped, not softened.** If you cannot produce the
exact read-only command that a reviewer can paste into a terminal to see the same thing you saw,
the finding does not go in the file. Do not downgrade it to `minor`, do not hedge the title, do
not write "appears to". Omit it.

Corollaries:

- `evidence.excerpt` is the real output, copied. Never paraphrase it and never synthesise
  plausible-looking output. The harness trims long excerpts for you.
- Report what the command showed, not what you infer it implies. Inference belongs in `impact`.
- One finding per object. Do not roll up "12 namespaces lack NetworkPolicies" into one finding —
  each gets its own stable id so each can resolve independently.

## The clean run

If the audit finds nothing, still call `finish` with `"findings": []` and a populated
`scope.clusters`. The harness closes the stream's open PR with a comment recording the date and
the clusters covered.

Then your final response **MUST be exactly `[SILENT]`**. A clean audit is not news; the closed PR
is the record. Say nothing in chat.

When `finish` reports `OPENED` or `UPDATED` with a non-zero `new` count, report the PR URL and a
one-line summary. When it reports `UPDATED` with `new: 0`, that is also `[SILENT]` — nothing
changed since the last run.

## Red lines

- **Read-only against clusters.** An audit inspects; it never mutates a cluster. Remediation is
  proposed as files in the PR or as a command for a human to run, never executed.
- **Never `git add .` or `git add -A`.** The harness stages only the distinct paths you named in
  `remediation.path`, and refuses wildcard pathspecs outright. Do not run your own `git add`.
- **Never open a second PR for an audit stream.** Do not call `gh pr create`. If the stream
  already has an open PR, `finish` edits it in place; that is the whole point.
- **Never force-push a protected branch.** `main`, `master`, and `production` are refused.
- **Never hand-write the PR body, title, commit message, or timestamps.** They are generated so
  that the diff between two runs is meaningful.
- **A `manifest` remediation path must exist on disk** when `finish` runs. Write the file first.
  A missing path is a hard error, not a warning.
