# Review-gate waivers — format & workflow (P5-T2, decision R-C)

The review-gate (06 §7) blocks a merge on any **unmitigated `high`/`critical`** finding. "Mitigated"
has a concrete meaning: a matching, non-expired entry in the repo-root **`security-review-waivers.yaml`**.
This doc defines the waiver format, the fingerprint that keys it, and the human workflow.

## Why a repo file (not a PR label or comment)

A waiver is a **security decision** and must be as attributable and revertible as any other change
(06 §8, invariant 5). Keeping waivers in a versioned file means:

- the mitigation is reviewed through the **same PR flow** it exempts — a human `approved_by` is on record;
- it is **revertible** — deleting the entry re-arms the gate;
- it **expires** — a stale exemption can't silently protect a regression forever.

A PR label or a bot comment would be mutable off-PR and leave no durable trail — rejected for that reason.

## Fingerprint

Each waiver targets exactly one finding, keyed by a stable **fingerprint**:

```
fingerprint = sha256( agent + "\n" + file + "\n" + normalize(message) )[:16]
```

`normalize(message)` is: lowercase → collapse all runs of whitespace to a single space → strip
line/line-number tokens (`line 42`, `:42`, `L42`, bare trailing/inline integers) → trim. Normalization
makes the fingerprint **stable across re-runs** (a finding that shifts by a few lines, or whose wording
gets a number tweaked, keeps its id) while staying **specific to the finding** (agent + file + the
semantic message). It is intentionally **not** tied to `line` or `severity`, so re-running the review or
re-tagging severity does not silently invalidate an approved waiver.

Get the fingerprint(s) for a run's findings:

```
python3 scripts/review-gate/score_findings.py --fingerprint findings.json
```

This prints one `<fingerprint>  <severity>  <file>: <message>` line per finding — copy the id of the
high/critical you intend to waive.

## Format

`security-review-waivers.yaml` is a mapping with one key, `waivers`, a list of entries. **All fields are
required** on every entry:

```yaml
waivers:
  - fingerprint: 0123456789abcdef # 16 hex chars, from --fingerprint
    justification: hostPath is required by the CSI node daemonset and scoped read-only.
    approved_by: adamparco # GitHub handle of the approver
    expires: 2026-12-31 # ISO-8601 YYYY-MM-DD
```

An empty gate is `waivers: []` (the committed default — nothing is exempt).

## Scoring semantics (how the scorer consumes it)

For each finding after the skills' own triage (06 §7 step 3):

- `medium` / `low` → **advisory**, never blocks (no waiver needed).
- `high` / `critical` → **blocks**, **unless** a waiver entry has a matching `fingerprint` **and** its
  `expires` date is **today or later**.
- A finding with **no `severity`** is treated as `high` (fail-safe, R-B).
- An **expired** or **absent** waiver ⇒ the finding is unmitigated ⇒ the gate blocks.
- A **malformed** waiver entry (missing field, unparseable date) is **ignored** (fails safe toward
  blocking) and reported, never silently honored.

The scorer (`scripts/review-gate/score_findings.py`, P5-T3) is the **authoritative** enforcer and runs
**hermetically** — the agent-driven detector only supplies the findings JSON (decision R-A). The gate's
exit code is the merge decision: non-zero ⇒ blocked.

## Workflow to add a waiver

1. The review-gate blocks your PR on a high/critical finding you believe is acceptable.
2. Run `score_findings.py --fingerprint` on the run's `findings.json` to get the fingerprint.
3. Add an entry to `security-review-waivers.yaml` with a real `justification`, your `approved_by`, and a
   bounded `expires`.
4. Commit it **on the same PR** (or a prior reviewed one). Re-run the gate — the finding is now mitigated
   and no longer blocks; the exemption is on record and will lapse at `expires`.
