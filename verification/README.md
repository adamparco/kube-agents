# Verification evidence

The machine-readable half of the build's verification record. Prose, rules and current state stay in
[`../docs/build/LEDGER.md`](../docs/build/LEDGER.md); this directory holds the rows.

| File                          | What it is                                                                              |
| ----------------------------- | --------------------------------------------------------------------------------------- |
| `results.csv`                 | One row per check run — the 09 §9.4 evidence record. Moved out of the ledger 2026-07-26 |
| `manifest-<phase>-<date>.csv` | Emitted by a full run, with the complete 09 §9.4 field set. None committed yet          |

## `results.csv`

Columns: `date, phase, check_id, level, target, result, evidence_ref, notes` — the ledger table's
columns, unchanged. `evidence_ref` points at the real artifact (command output, a denial message, an
`ActionRecord` ID, an audit-log query). **A `pass` with no `evidence_ref` is recorded as `skipped`,
not as a pass.** Keep the most recent result per `(check_id, level, target)`.

Two things about the 37 rows that came over are wrong, and are carried across as-is rather than
cleaned up, because normalizing them would be inventing results:

- Ten rows have `result` of `correction` or `finding`. 09 §9.4's enum is
  `pass | fail | deferred | skipped | quarantined`, and neither word is in it.
- Five `(check_id, level, target)` keys appear twice, against the keep-the-most-recent rule, and
  every row carries the same date — so which is most recent is not recoverable from the data.

Both are for the next full run to write correctly. The `target` column reads `kind*` throughout:
that is what those runs actually ran on, and moving the inner loop to GKE on 2026-07-26 does not
retroactively change where a 2026-07-25 run happened.

`verification/` is excluded from the Cloud Build upload context (`.gcloudignore`) — it is evidence,
not build input.
