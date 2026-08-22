---
name: file-pull-request
description: Turn one promoted self-improvement finding into one upstream pull request — branch, minimal fix, the five-part body, and the honest statement of what could not be validated.
---

# File a Pull Request

Runs only in `fork` and `upstream` mode, once per finding that cleared the gate, in a turn of its
own. The investigation is over; do not re-investigate. Your job is to write the smallest change that
fixes the finding you were handed, and to describe it so a reviewer can judge it in one pass.

## 0. Before you write anything

- Re-read the finding. Open the file it names, at the revision in the checkout you were given.
- **If the code does not say what the finding says it says, stop and open nothing.** Print
  `SKIPPED: <why>` and end the turn. A stale finding filed as a pull request costs a human more
  than a missed fix does. This happens: the finding may be hours old and `main` moves.
- Check whether it is already fixed or already filed:
  `curl -sSf "https://api.github.com/search/issues?q=repo:gke-labs/kube-agents+is:pr+is:open+<key terms>"`.
  An existing pull request means stop.

## 1. Branch

```bash
cd <the source checkout>
git switch -c selfimprove/<signal>-<short-slug>
```

Branch from the deployed revision the checkout is already at, not from `main`. The finding is
evidenced against that commit and a reviewer needs the diff to line up with it.

## 2. The change

- **Smallest change that fixes the finding.** Nothing else. Not the adjacent bug you noticed, not
  the formatting, not the rename that would make the file nicer.
- Match the surrounding code: its naming, its idiom, its comment density.
- Add or extend a test when the repository has one covering that code. If it has none, say so in the
  body rather than building a test harness as part of a fix.
- Run what the change touches — `go build ./...` inside `k8s-operator/` for Go, the Python tests for
  Python, `make docs-check` for documentation. A pull request that does not build wastes the review.

## 3. Commit and push

Conventional Commits, and the type has to match the diff:

```
fix(operator): stop the reconciler retrying a Secret it cannot read
```

- `fix` for a bug, `perf` for latency, `docs` for documentation, `refactor` for an inefficiency with
  no behaviour change.
- No `Co-Authored-By` trailers and no "Generated with" attribution.
- Push to the fork named in your prompt. Never push to `gke-labs/kube-agents` directly.

## 4. The body — five parts, in this order

Use `.github/PULL_REQUEST_TEMPLATE.md`, never `--fill`. Write plain declaratives; do not grade your
own work. The five parts the design requires:

1. **The finding.** What is wrong and what it costs a user. Include the fingerprint, the severity,
   how many times it was seen and over what window. State that a self-improvement run found it —
   a reviewer who does not know that will read the pull request wrong.
2. **Evidence.** The verbatim log lines and timestamps, in a fenced block, with the query that
   produced each. This is the part a reviewer checks first and the part most likely to be thin.
3. **The fix and why.** The mechanism, then the change, then why this change and not the obvious
   alternative. Name the alternative.
4. **Live validation.** What you actually did against a running install, at each layer the change
   claims to touch. You are read-only, so this section is mostly what you **could not** do — write
   that plainly under **Testing → Live validation**: "Not live-tested: the self-improvement runner
   holds read-only grants and cannot deploy. Verified by static reading of `<file>:<line>` and by
   `<test>`." An empty section is not an answer, and a claim the diff does not support is worse than
   no claim.
5. **The change itself.** The diff. Keep it reviewable in one sitting.

Fill in **Self-Review** honestly: this pull request was written by an agent that could not run the
code it changed, and the reviewer needs to know that before the diff, not after.

## 5. Finish

- Print the pull request URL on the last line of your reply, alone, with nothing after it. The
  runner reads that line and records it in the ledger; without it the finding is filed but looks
  unfiled, and the next run files it again.
- Do not wait for review, do not merge, do not comment further.

## Refuse to file when

- The code does not match the finding (§0).
- The fix would touch the self-improvement loop's own gate, ledger, or grants. A loop that can widen
  its own permissions is the failure mode this whole design is arranged around. Report it as a
  finding for a human instead.
- The fix needs a credential, a cluster change, or a decision about product direction.
- You are not confident. Print `SKIPPED: <why>`. The finding stays in the ledger, the count keeps
  rising, and a later run with better evidence can file it.
