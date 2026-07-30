---
name: overview-sync
description: Generate or refresh the high-level overview set in docs/overview/ — architecture, capabilities, user interactions, the phase-by-phase build, and the measured delta against upstream gke-labs/kube-agents. Reads docs/design/, docs/build/LEDGER.md and the git trees; writes only under docs/overview/. Use when a phase closes, when a spec or roadmap changes, when the upstream comparison goes stale, or to build the set from scratch. Not part of the harness state machine — it records no verification evidence and never advances the ledger.
---

# overview-sync — the descriptive layer, regenerated from its sources

`docs/overview/` is a **summary of** the authoritative documents, for readers who need to understand
what the project is building without reading a 2,900-line contract spec. It is **descriptive, not
normative**: where it disagrees with its sources, the sources win, and the set says so on its face.

This skill exists because a hand-written overview rots faster than anything else in the repo — every
status marker in it is a claim about a build that moves daily.

---

## 1. Sources, and what each one is authoritative for

| Source                                     | Authoritative for                                                                         | Never                     |
| ------------------------------------------ | ----------------------------------------------------------------------------------------- | ------------------------- |
| `docs/design/01`–`09`                      | _What_ is being built — vision, personas, security, architecture, contracts, verification | Edited by this skill      |
| `docs/design/07-implementation-roadmap.md` | The build sequence — phases, tasks, weights, acceptance, ordering constraints             | Edited by this skill      |
| `docs/build/LEDGER.md`                     | _Where the build is_ — phase status, current unit, deferrals, decisions                   | Edited by this skill      |
| `docs/build/archive/LEDGER-phases-0-7.md`  | Phase 0–7 rows, PR numbers, merge SHAs                                                    | Edited by this skill      |
| `git` trees (`HEAD` vs `upstream/main`)    | The upstream delta — **measured, never paraphrased**                                      | Used as a push or PR base |

**Write only under `docs/overview/`.** `docs/design/` is the harness's source of truth and
`docs/build/` is its state; a docs-summarizing skill has no business in either. If a source is
wrong, that is a finding to surface — not something to correct from here.

**On `upstream`.** `AGENTS.md`/`CLAUDE.md` forbid `upstream` as a push target or a **branch** diff
base, and that stands. Doc 05 is the one deliberate exception to _reading_ it: comparing `HEAD`
against `upstream/main` is the entire point of the delta document. Fetch it, read it, never push it,
and never base a working branch on it.

---

## 2. The set, and which file owns what

Six files. Each claim belongs to exactly one of them; do not restate a status in two places.

| File                      | Owns                                                                                                                                                        |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `README.md`               | Index, the one-paragraph thesis, the six core invariants, the pointer table, the generation-level status table                                              |
| `01-architecture.md`      | Topology diagram, the one-CR-two-workloads split, the component inventory (C1–C17 + C-\*), the broker pipeline, risk classes, blast-radius bounds, the SLIs |
| `02-capabilities.md`      | Capability areas with a status **and a delivering phase** on every row; the skill matrix; explicit non-capabilities                                         |
| `03-user-interactions.md` | Audiences, authorization tiers, addressing modes, the command surface, the `kubectl` brake surface, the reference journeys                                  |
| `04-phases.md`            | Every phase 0–15 — goal, deliverables, why it lands there, status; the Definition of Done; the named risks                                                  |
| `05-upstream-delta.md`    | The measured comparison against `gke-labs/kube-agents`                                                                                                      |

Status legend, used identically across the set: ✅ built · 🟡 in progress · ⬜ designed and scheduled
· — absent.

---

## 3. Establish the status snapshot — before writing a word

Every ✅/🟡/⬜ must trace to a ledger row. **Never infer a status from the presence of code.**

```bash
sed -n '1,80p' docs/build/LEDGER.md                      # Status + phase progress
grep -nE '^\| [0-9]+ \|' docs/build/archive/LEDGER-phases-0-7.md   # phases 0-7 rows, PRs, SHAs
git log --reverse --date=short --pretty='%ad %h %s' origin/main    # merge dates
```

⚠️ **Do not `Read` the whole ledger.** It is deliberately not prettier-formatted and a full read has
produced 74 KB of output in one call. Use `sed -n` ranges and targeted `grep -n`.

Record, explicitly, before drafting: the current phase, the current unit, the leaf-unit count if the
phase publishes one, and the merge PR for every closed phase. Date-stamp the snapshot in every file
that carries a status — the reader needs to know how old the claim is.

**Arithmetic you perform is yours to verify.** Quote the ledger's own framing for its own counts, but
if you compute something from it (phases remaining, weighted totals, velocity), check it. The
ledger's "8 of 15 phases done" does not reconcile with "phases 0–8 merged" — carry the ledger's
wording where you are quoting it, and use unambiguous framing where you are calculating.

---

## 4. Measure the upstream delta — never paraphrase it

```bash
git fetch upstream                       # NOT --depth=1; see the trap below
git rev-list --count upstream/main..HEAD
git diff --shortstat upstream/main HEAD
git diff --name-status upstream/main HEAD
```

Then compare the trees structurally, which is what actually populates doc 05:

```bash
for ref in upstream/main HEAD; do
  git ls-tree -r --name-only $ref -- k8s-operator/api/v1alpha1   # CRD kinds
  git ls-tree -r --name-only $ref -- k8s-operator/cmd            # binaries
  git ls-tree -r --name-only $ref -- k8s-operator/internal | awk -F/ '{print $3}' | sort -u
  git ls-tree -r --name-only $ref -- agents | awk -F/ '{print $2}' | sort -u
done
git show upstream/main:README.md         # how upstream describes its own posture
```

⚠️ **The merge-base trap.** `git merge-base HEAD upstream/main` returns **empty** if upstream was
fetched shallow — no common ancestor is in the history. Do not try to fix the merge base; the
two-ref `diff`/`ls-tree`/`rev-list` forms above need no ancestor and give a complete answer.

Rules for doc 05:

- **Quote upstream's own words** for its posture (its README states the GitOps-only-mutation line
  directly). Characterizing a sibling project from memory is how a delta doc becomes wrong.
- Every capability row carries **both** sides and a phase. A row that only says what this fork has
  is a feature list, not a delta.
- Keep the section that states what the fork does **not** claim over upstream. A delta doc that only
  flatters one side is not being read as analysis.
- Distinguish **kept / changed / superseded / dropped**. "Upstream has X and we don't" is usually
  "we replaced X with Y", and the difference is the interesting part.

---

## 5. Generate or update

**Generate** (set does not exist): write all six, in order — `01` first so the component vocabulary
is fixed, then `02`/`03` against it, then `04`, then `05`, then `README.md` last so its index and
status table match what actually got written.

**Update** (the common case): touch only what the trigger invalidates.

| Trigger                                   | Refresh                                                                                                                                  |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| A phase closed                            | `README.md` status table · `04` (that phase → ✅, next → 🟡) · every `02` row whose phase just shipped · `05` capability-matrix statuses |
| A unit landed mid-phase                   | Usually nothing. Only if it changed a **capability**, not an implementation detail                                                       |
| `docs/design/07` changed                  | `04` in full · `02` phase columns · re-check `05`'s phase mapping                                                                        |
| `docs/design/05` or `06` changed          | `01` (components, pipeline, bounds) · `03` if the command or addressing surface moved                                                    |
| A new CRD, binary, or `internal/` package | `01` §component inventory · `05` §3.1/§3.2                                                                                               |
| Upstream moved                            | `05` in full — re-run §4; do not hand-patch a measured number                                                                            |
| Nothing changed but time passed           | The snapshot dates, or nothing at all. Do not churn prose to look current                                                                |

When updating a status marker, update its **date stamp** in the same edit. A stale date on a fresh
claim is worse than a stale claim, because it reads as verified.

---

## 6. Close out

```bash
npx prettier --write docs/overview/*.md && npx prettier --check docs/overview/*.md
make validate
```

`docs/overview/` **is** prettier-formatted (unlike `docs/build/LEDGER.md`, which is
`.prettierignore`d on purpose). CI checks the whole `origin/main...HEAD` changed set, so if this run
also touched markdown elsewhere, format that too.

Then confirm, by hand:

- Every relative link resolves — the set points into `../design/` and `../build/` constantly.
- Every status marker traces to a ledger row you actually read this run.
- Every number in `05` came from a command in §4 this run, not from the previous revision.
- `README.md`'s index lists exactly the files that exist.

Report: which files changed, what status transitions were recorded, and any place a source
contradicted itself. **Surface source contradictions — never silently pick one.**

---

## 7. What this skill does not do

- **It does not touch `docs/design/` or `docs/build/`.** Not to fix a typo, not to correct a status.
- **It records no verification evidence and advances no ledger row.** It is not a harness unit; it
  runs no check IDs and has no gate class. `harness-run`, `harness-verify`, `harness-milestone` and
  `harness-improve` are the build loop — this is not one of them and must not be mixed into one.
- **It does not decide anything.** If the roadmap and the ledger disagree about a phase's status,
  that is a finding for a human, not a judgement call to make in a summary document.
- **It does not commit or open a PR.** Leave the changes in the working tree.
