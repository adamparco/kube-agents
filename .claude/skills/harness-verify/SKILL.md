---
name: harness-verify
description: Run conformance checks by stable ID (V-<SUITE>-<nnn>) from docs/design/09-verification-and-validation.md at their assigned level, assert environment preconditions before trusting any live result, and record an evidence row per check in the ledger. Use after implementing a unit, at a phase gate, when re-running the ratchet for regression, or to re-test a deferred check whose blocker has cleared. Does not implement fixes — hand failures back to harness-run.
---

# harness-verify — run checks by ID, record evidence

The harness does not invent tests. It runs the checks `docs/design/09-verification-and-validation.md`
defines, **by ID**, at **their** level, and records evidence (PROTOCOL §6).

Targets, cluster names, and build commands come from `.claude/harness/binding.md`.

---

## 1. Select the check set

| Trigger          | Run                                                                          |
| ---------------- | ---------------------------------------------------------------------------- |
| A unit           | The check IDs the unit claims **+ every BLOCKING-ALWAYS check**              |
| A phase gate     | The above **+ the phase ratchet** (09 §10) **+ every prior phase's ratchet** |
| Regression sweep | Every suite green at the end of the previous phase. The ratchet only grows.  |
| Deferral re-test | The deferred IDs whose named blocker has cleared                             |

BLOCKING-ALWAYS suites: **V-CTN, V-BRK, V-REV, V-ISO, V-ADV, V-MET**.

Resolve each ID to its row in 09 §6 and record its level, gate class, and negative-control
requirement (`¬`) before running anything. A check marked `¬` whose negative control did not run is
not a pass.

---

## 2. Assert environment preconditions — before trusting any L2/L3 result

Every one of these has produced a real green on a broken property in this repo (09 §11). Assert them
first; a result gathered before they hold is discarded, not adjusted.

1. **Image freshness** (09 §11.1). Rebuild → load/push → restart, then assert every deployed
   first-party image **digest** matches the build under test. A same-tag image with
   `imagePullPolicy: IfNotPresent` is not refreshed — the cluster silently runs old admission logic
   and reads green.
2. **Policy activation** (09 §9.3.2). A freshly created `ValidatingAdmissionPolicyBinding` takes
   time to become effective. Poll a dry-run until it actually **rejects** before judging any
   admission property.
3. **No grandfathered objects** (09 §11.2). Admission does not evict existing pods. Force recreation
   before judging; a running object's state is not evidence the policy works.
4. **An enforcing network substrate** (09 §11.6). A cluster accepts a NetworkPolicy whether or not
   anything enforces it, so a green egress check on a non-enforcing dataplane is evidence about the
   API server. `p4_assert_enforcing_dataplane` holds an **allow-list** of dataplanes known to
   enforce — `calico-node`, `anetd` (Dataplane V2), `cilium`. Anything unrecognised → `deferred`,
   never `pass`.
5. **Anchored destructive-test guard** (09 §11.5). Before any test that deletes, kills, applies
   deliberately-bad RBAC, or drives a destructive action through the broker: confirm the context
   matches the sanctioned ephemeral pattern with an **anchored** match. Substring matching would
   accept a prod lookalike. Anything else → **halt**.

Also confirm you are asserting against the **runtime-authoritative** artifact, not a baked one that
the runtime shadows (09 §11.3), and name which artifact in the check's notes.

---

## 3. Run

Order L0 → L1 → L2 → L3 → L4 (09 §9.2). **Exception:** BLOCKING-ALWAYS suites run in full even
after an earlier level fails — knowing whether containment also broke is worth the minutes.

**Never substitute a lower level.** The level is a property of the requirement, not of convenience
(09 §3). Specifically: a structural check does not stand in for an enforcement check. Grepping that
a NetworkPolicy file exists is not evidence egress is denied; asserting the classifier returned
`gated` is not evidence the action did not execute — assert the target object is **unchanged**
(09 §11.10); asserting an undo plan exists is not evidence it restores (09 §11.11).

For checks that "passed" negatively, confirm the denial was the expected one. A malformed manifest
also fails to apply.

**Non-vacuity goes through `dev/mutate.py`**, against a spec committed as
`verification/mutants/<CHECK-ID>.json` — not a driver written for the occasion. The runner refuses
to produce a number it cannot back: no `-run` filter, every row naming the test that must fail, an
applier that refuses a needle it does not find exactly once, and a third verdict (`BROKEN`) for a
mutant it could not evaluate, so the denominator cannot silently shrink. See `harness-run` §5 and
[[LSN-047]]/[[LSN-048]]/[[LSN-049]] for the three ways a hand-rolled sweep lies.

Independent suites may be dispatched in parallel; each returns its own evidence.

---

## 4. Record evidence — one row per check (09 §9.4)

```
check_id, suite, level, target(gke|none), result(pass|fail|deferred|skipped|quarantined),
requirement_ids[], evidence_ref, duration_s, started_at, image_digests[], notes
```

`evidence_ref` points at the real artifact: command output, the denial message, an `ActionRecord`
ID, an audit-log query. **A `pass` with no evidence reference is recorded as `skipped`** — not as a
pass with a note.

Write rows to the ledger's verification log (`binding.md` §State), and emit/refresh the run manifest
and `verification/traceability.yaml` on a full run.

---

## 5. Handle results by gate class (09 §9.5)

| Class               | Action                                                                                            |
| ------------------- | ------------------------------------------------------------------------------------------------- |
| **BLOCKING-ALWAYS** | **Halt immediately.** Do not merge, do not advance, do not continue to other work. Surface.       |
| **BLOCKING-PHASE**  | Blocks advancing past the owning phase. Hand back to `harness-run` to fix.                        |
| **ADVISORY**        | Record. Report a regression against the recorded baseline as a failure; a missed absolute is not. |
| **DEFERRED**        | Record with a **named blocker, owner, and promotion condition**. Never as a pass.                 |

**Deferral discipline.** A BLOCKING-ALWAYS check may **not** be deferred. If it cannot run, the
build is not verifiable — that is the finding, and it is a halt (09 §9.6). Reclassifying a failure
as a deferral is a named reward hack (SELF-IMPROVEMENT §4); a deferral without an external blocker
is a failure wearing a different label.

Checks marked **†** in 09 §6.14 are blocked on a §12 specification tightening. Record them
`deferred` with that §12 row as the blocker. Do not let the implementation pick its own threshold.

---

## 6. Flakes (09 §9.7)

- **V-CTN, V-BRK, V-REV, V-ADV, V-MET are never retried to green.** A flaky containment test is a
  failure until the non-determinism is explained. Retry-to-green on a control is how a real gap gets
  papered over.
- Other suites may retry **once**. A check that needed a retry is **quarantined** and tracked, not
  ignored. Record the retry count.
- Quarantine is time-boxed and visible in the manifest. A quarantined BLOCKING-ALWAYS check blocks.

---

## 7. Close out

- Every selected ID has a row. A silently skipped BLOCKING-ALWAYS check fails V-MET-007.
- Update the deferral list and the metrics snapshot in the ledger.
- Return to the caller: IDs run, results by class, evidence refs, and any halt.

Do not fix anything from inside this skill. Failures go back to `harness-run`; check or spec changes
go to `harness-improve`.
