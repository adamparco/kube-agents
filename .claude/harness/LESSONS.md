# Lessons

The harness's durable memory. Every entry is a mistake this repository has **already paid for** —
mined from `docs/build/LEDGER.md` and encoded in `docs/design/09-verification-and-validation.md`
§11.

Lifecycle and the mechanization requirement: [`SELF-IMPROVEMENT.md`](SELF-IMPROVEMENT.md) §2–§3. A
lesson that ends as prose does not count; the first entry below was written down **three times**
before it became something that fails the build.

**How to use this file.** During ORIENT, read the lessons whose **tag** matches the area you are
about to touch. Lessons are written symptom-first, because you will meet the symptom before you know
the cause.

**Format, and why it is what it is.** Each lesson's fields were a two-column table until 2026-07-26,
when prettier's column padding turned out to be **53% of this file** — 160 KB, all of it read at
every orientation. They are bold-label paragraphs now, which prettier cannot pad, and the file is in
`.prettierignore` so the index table stays unpadded too. Write new lessons the same way.

**Nothing has been archived, and the criterion is why.** The prune that shrank the ledger was to
move out the bodies of closed lessons whose mechanization had held **three phases or more**. Applied
honestly that set is **empty**: every lesson here was seeded or opened inside Phase 8, so not one has
had three phases to hold for. The bodies all stay. Revisit at the next milestone, when the criterion
will start selecting.

---

## Index

| ID | Tag | You will notice… | Status | Closed by |
| --- | --- | --- | --- | --- |
| **LSN-001** | images, live-verify | A live check passes that the source says must fail (or vice versa); unit tests are fine | closed | `invariants-gate.py` `check_l2_scripts_declare_preconditions` (P1) · `preconditions.sh` |
| **LSN-002** | admission, live-verify | A policy is applied, the offending object still runs, and nothing complains | closed | `invariants-gate.py` same check, P3 arm · `preconditions.sh` `p3_force_recreate` |
| **LSN-003** | config, checks | The check reads the value you wrote; the pod behaves as if you never wrote it | closed | `invariants-gate.py` same check, P6 arm (shadowed-basename rule) |
| **LSN-004** | rbac, policy-design | A policy "blocks all writes" and the escape uses a verb that is not a write | closed | `invariants-gate.py` check 1 (read-verb ALLOW-list) |
| **LSN-005** | destructive-tests | A guard "only runs on test clusters" and the context name merely _contains_ a keyword | closed | `invariants-gate.py` `check_destructive_guards_are_anchored` |
| **LSN-006** | netpol, levels | Every NetworkPolicy assertion is green and traffic flows anyway | closed | `egress-enforcement-l2.sh` (L2-CHAIN, Dataplane V2) · `test_dataplane_precondition.py` |
| **LSN-007** | completeness, wiring | Every unit test passes and the feature does nothing in a real install | closed | `install-path-wired.py` · `test_image_provenance.py` · 09 §5.1 |
| **LSN-008** | deferrals | A phase report is all green and a whole class of checks never ran | closed | `invariants-gate.py` `check_deferrals_name_blockers` (V-MET-006) |
| **LSN-009** | ratchets, refactors | A refactor lands, the suite is green, and the suite is smaller | closed | `invariants-gate.py` ratchet + `check_retirements_name_replacements` |
| **LSN-010** | ci, formatting | Prettier is clean locally and the Prettier Check is red on the PR | closed | `.github/workflows/prettier.yml` · `toolchain-preflight.sh` |
| **LSN-011** | merge | One required check is red and one flag would merge it anyway | closed | `merge-provenance.py` (in `merge-provenance.yml`: push to main + daily) |
| **LSN-012** | git, remotes | The diff is enormous, or empty, or the PR shows none of the work | closed | `git-preflight.sh` (L0-CHAIN) |
| **LSN-013** | version-assertions | A vanilla cluster is reported as GKE (or the reverse) | closed | `verify-phase7.sh` B0 (server-side node `kubeletVersion`) |
| **LSN-014** | specs, audits | Two specs each look correct and the thing they describe cannot be built | closed | `spec-ids.py` (V-MET-010/012/013-for-check-IDs, L0-CHAIN) |
| **LSN-015** | fixtures, topology | Everything works with one of a thing and deadlocks with two | closed | `multi-agent-namespace-l2.sh` CLAIM 1 · `agent_manifests_test.go:185` |
| **LSN-016** | codegen, levels | Every gate is green and the artifact they describe will not install | closed | `closed-allowlist.py` check 3b · `closed-allowlist-l2.sh` L2-1 |
| **LSN-017** | checks, corpus | A check is green before the commit and red after it, with nothing changed in between | closed | `closed-allowlist.py` `tracked_files()` |
| **LSN-018** | targets, tooling | A `make` target applies to the wrong cluster and the context override is ignored | closed | `invariants-gate.py` `check_make_targets_are_context_explicit` |
| **LSN-019** | lessons, mechanization | A lesson says `closed`, the mechanization field is populated, and the defect recurs | closed | `invariants-gate.py` `check_closed_lessons_are_executable` |
| **LSN-020** | ci, tooling | A linter is green locally and red in CI, and the local run never ran the rule | closed | `toolchain-preflight.sh` (in `actionlint.yml`) |
| **LSN-021** | clis, callers | A command is "run" and does nothing; the only symptom is a downstream file not existing | closed | `cli-contract.py` (L0-CHAIN) |
| **LSN-022** | reverts, mutation | Work you finished an hour ago is simply gone, and there is no stash and no reflog entry | closed | `mutate.sh` · `test_mutate.py` (`unittest discover dev`) |
| **LSN-023** | checks, corpus | A check that requires code to call X is satisfied by the comment saying it calls X | closed | `invariants-gate.py` `_code_lines()` |
| **LSN-024** | checks, timing | A check reports a property absent; the object was correct and the read was simply early | closed | `invariants-gate.py` `check_l2_status_reads_are_polled` (P9, L0-CHAIN) |
| **LSN-025** | checks, identity | A label selector names a role, not an object, and still matches the generation you deleted | closed | `invariants-gate.py` `check_p3_pods_resolved_by_ownership` (P3, L0-CHAIN) |
| **LSN-026** | checks, infrastructure | Several unrelated security properties all "fail" at once, and the cluster is what is broken | closed | `invariants-gate.py` `check_l2_scripts_assert_cluster_health` (P10, L0-CHAIN) · `preconditions.sh` |
| **LSN-027** | infrastructure, preflight | A preflight reports the host is fine, and the cluster still refuses to start | closed | `invariants-gate.py` `check_cluster_creating_scripts_assert_capacity` · `dev/lib/substrate-capacity.sh` |
| **LSN-028** | netpol, substrate | An allowlist denies the destination it lists, and the over-block reads as a fixture bug | closed | `egress-enforcement.sh` §3/§4 (run by `verify-phase7.sh` → phase5, L2-CHAIN) |
| **LSN-029** | portability, substrate | A portability fallback runs the wrong tool first, and the failure arrives on stdout | closed | `invariants-gate.py` `check_platform_idioms_are_gnu_first` (L0-CHAIN) · `test_build_under_test_precondition.py` |
| **LSN-030** | git, reverts | Work you finished an hour ago is gone again, and the verb that ate it is not the one you guarded | closed | `git-destructive-guard.py` (PreToolUse hook, `.claude/settings.json`) · `test_git_destructive_guard.py` (`unittest discover dev`) |
| **LSN-031** | verification, security | Every rule passes its own test and three of them are switched off | closed | `classifier-corpus-lint.py` (L0-CHAIN) · `TestEverySecurityControlCanReachAGate` in `classify_test.go` |
| **LSN-032** | security, codegen, corpus | A deny-list names a group nobody serves, and the corpus that checks it agrees | closed | `api-group-single-sourced.py` (L0-CHAIN) · `TestForbiddenSetNamesTheLiveAPIGroup` in `classify_test.go` |
| **LSN-033** | security, scope, corpus | A safety list is complete for the domain its author had in mind, and empty for the one where the damage is | closed | `TestNonRecreatableKindsAreGatedByTheClassifier` in `internal/broker/undo/` · `undo-corpus-lint.py` (L0-CHAIN) · corpus §M |
| **LSN-034** | security, gating, api-design | A value compared against itself will never tell you it is the wrong shape | closed | `assertLeafOps` + `TestDiffEmitsOnlyLeafOps` / `TestDiffKeepsAnEmptyMapVisible` in `k8s-operator/internal/broker/execute/diff_test.go`, run on every PR by `k8s-operator-test.yml` · `subtreeOps` doc at the definition site |
| **LSN-035** | checks, mutation, negative-controls | A mutation survives, and the rule it broke turns out to be one no input can reach | closed | `dev/tests/negative-controls-name-their-rule.py` (L0, in `dev/L0-CHAIN.txt`) — blinds each control's probe with a shape-preserving constant that names no property; a control that still passes was only asserting that *something* failed. All 10 controls in `dev/tests/` converted to per-mutation signals · the ladder's own properties are held by `TestLadderPropertiesHoldOverEveryAcceptedHistory` |
| **LSN-036** | checks, renderers, allowlists, controller | A uniqueness check goes red on correct code, and the one-line green is an allowlist entry | closed | `pause-is-not-scale-to-zero.py` file-keyed `ALLOWED_REPLICAS_RHS` + `BROKER_REPLICAS_CONST` + stale-owner arm (L0-CHAIN) · `TestPauseDoesNotChangeTheRenderedBroker` in `pause_not_scale_to_zero_test.go` |
| **LSN-037** | builds, dockerfiles, ci, toolchain | An image build fails on symbols that `go build ./...` resolves fine | closed | `dev/tests/go-build-targets-packages.py` + `--negative-control` (L0-CHAIN) · package-path builds in all 3 Dockerfiles + 2 Makefile recipes |
| **LSN-039** | wiring, completeness, manifests, install-path | The manifest is correct, the check that reads it is green, and no install path ever applies it | closed | `dev/tests/identity-has-install-path.py` (**V-CMP-007**, L0-CHAIN) — manifest→step reachability over `k8s-operator/scripts/`, 7 properties, 8 negative controls including a reproduction of the original defect · the install path itself in `common.sh` (`render_agent_identity`, `apply_agent_identity`, `delete_agent_identity`) + `agent-identity.yaml.template` + `broker-operations-grant.yaml.template`, applied from `provision_08` and `provision_12` |
| **LSN-038** | checks, probes, discovery, negative-controls | A guard that fails safe still fails, and a green run is how it tells you | closed | `check_machinery_probes_resolve` + `CLOSED_MARKER` + the Go arm of `_invoked_by` in `invariants-gate.py` (L0-CHAIN) · `dev/test_invariants_gate.py` (19 negative controls) · `dev/tests/golex.py` shared by `scope-label-single-sourced.py` and `api-group-single-sourced.py` |
| **LSN-040** | seams, integration, assembly, broker | Two packages, each right, mean different things by the same field, and the first caller is the only thing that can tell | closed | **V-BRK-020** + **V-BRK-022** — `k8s-operator/internal/broker/pipeline/verbs_test.go` drives every verb of `broker.ValidOps()` end to end, discovered from the enum |
| **LSN-041** | admission, CEL, policy, security, fail-open, checks | A security artifact's comment says a control exists; grep says it never did, and the hole it described is live | **closed** | `dev/tests/journal-status-vap-parity.py` + `--negative-control` in `dev/L0-CHAIN.txt` — derives the required CEL variable set from the Go type, so a status field with no policy row fails the build |
| **LSN-042** | build, ci, deploy, kustomize, install-path, checks | Nothing in the repository ever built the thing the repository installs | closed | `make render` (a prerequisite of `build`/`test`) + `dev/tests/install-render-is-faithful.py` (**V-CMP-008**) for the overlay; `dev/tests/install-artifacts-are-rendered.py` (L0, in `dev/L0-CHAIN.txt`) for the general property — every kustomize directory an install path applies is one `render` builds, both sets derived, and `render` must stay a prerequisite of a CI-invoked target |
| **LSN-043** | harness, orient, durability, git | The ORIENT drain was done, verified green, and then silently reverted by a branch switch | closed | `.claude/skills/harness-run/SKILL.md` §1 step 6 (commit the drain before SELECT) + `dev/tests/invariants-gate.py` `_drain_is_committed` (L0) — fails on an uncommitted drain, i.e. one that removes inbox items or advances `Last drained`, while an uncommitted human *append* still passes |
| **LSN-044** | rbac, checks, vacuity, kubectl, L2 | `kubectl auth can-i patch foo/status` asks about a **name**, not a subresource, so the denial it reports is about an object that does not exist | closed | `dev/tests/cluster-check-hygiene.py` property 1 (L0, in `dev/L0-CHAIN.txt`) — no positional word of any `auth can-i` may contain `/`, `for`-lists resolved and expanded, and a resource the scanner cannot resolve obliges the script to carry a `*/*)` guard; local fix is `can()`/`subj()` in `dev/verify/brake-fanout-l2.sh` |
| **LSN-045** | checks, L2, fixtures, journal, append-only | An L2 suite that writes to an append-only journal can never delete its own namespace, and finds out on the second run | closed | `dev/tests/cluster-check-hygiene.py` property 2 (L0, in `dev/L0-CHAIN.txt`) — no script that creates a DELETE-protected object may delete a namespace, with the protected set derived from the VAPs' own `DELETE` matchConstraints and the CRDs' `spec.names.kind`; `brake-fanout-l2.sh` reuses namespaces, mints IDs per run and matches Events by UID |
| **LSN-046** | checks, deferrals, record-keeping, false-green, blocking-always, append-only | The deferral row named the category instead of its members, so the one gate arm that hunts deferred BLOCKING-ALWAYS checks matched nothing — and the false pass hiding behind it silenced that arm a second way | closed | `dev/tests/invariants-gate.py` `check_dagger_checks_are_deferred_by_id` (L0, in `dev/L0-CHAIN.txt`) — derives the † set from 09 §6.14 and requires every member to be named **by ID** in the Deferrals table and to hold no uncorrected pass; `_verification_evidence_rows` now emits one line per check ID and honours `**correction**` supersession; the BLOCKING-ALWAYS arm gained a not-yet-due clause keyed to the phase in 09 §6 that expires by itself and fails closed on an unparseable phase |
| **LSN-047** | mutation, tooling, harness, near-miss | The mutation sweep reports a clean restore and one of the files it restored is now the other one | closed | `dev/mutate.py` + `dev/test_mutate_sweep.py` (L0, in `dev/L0-CHAIN.txt` via `unittest discover dev`) — the sweep is **configured, not authored**: a spec under `verification/mutants/<CHECK-ID>.json` drives a runner that snapshots by **position**, and `.claude/skills/harness-run/SKILL.md` §5 + `harness-verify` §3 route every non-vacuity sweep to it, which is the half `dev/mutate.sh` could never supply |
| **LSN-048** | mutation, tooling, checks, vacuity, false-finding | A mutation sweep names a test that does not exist, and reports the mutation as **survived** — a hole that is not there, indistinguishable from one that is | closed | `dev/mutate.py` + `dev/test_mutate_sweep.py` (L0, in `dev/L0-CHAIN.txt` via `unittest discover dev`) — a spec carrying `-run` is refused outright, every catcher is checked against `go test -list` **before the first mutation**, and a mutant the sweep could not evaluate scores `BROKEN` rather than joining the survivor count |
| **LSN-049** | mutation, tooling, checks, vacuity, false-finding | The sweep's own shell quoting kept a mutation from ever being applied, and `rc == 0` was scored as "the suite passed with it in place" — an invented hole, reported twice | closed | `dev/mutate.py` + `dev/test_mutate_sweep.py` (L0, in `dev/L0-CHAIN.txt` via `unittest discover dev`) — needles and replacements live in JSON and are applied by `str.replace` in-process, so nothing crosses a shell parse; the applier refuses unless the needle appears exactly once, which is also how the sweep observes that the mutation LANDED; and `rc != 0` is not a catch, every row naming the test that must be in the failing set |
| **LSN-050** | harness, checks, coverage, false-green, pre-commit | Seven L0 checks discover files with `git ls-files` and no `--others`, so the chain is blind to any file not yet `git add`ed — which is every brand-new file at the one moment the chain is documented to run | closed | `dev/tests/gitcorpus.py` + `check_l0_corpus_is_not_index_only` in `dev/tests/invariants-gate.py` (L0, in `dev/L0-CHAIN.txt`) — one `--cached --others --exclude-standard` enumerator, now used by all ten call sites, and a gate arm that AST-parses every `.py` and `.sh` under `dev/` and fails any `ls-files` argv without `--others`. The gate covers the whole tree rather than just the chain, because the failure mode is the eighth script copying the seventh |
| **LSN-051** | harness, halts, spec-reading, PROTOCOL §8.5, rbac | A §8.5 halt was declared after comparing the broker's live reads against 06 §2.2.1, the *broker-operations* grant; the actor's authority is 06 §2.2, one level up, and it grants every read the halt said was ungranted — a subsection number reads like a refinement of its section, so having read the subsection felt like having read the section | **closed** | `harness-run` §7 now requires a §8.5 halt to quote **both** statements, each by document and section — you cannot quote a sentence you never found. `dev/tests/invariants-gate.py` (`check_spec_contradiction_halts_cite_both_sides`) pins the rule in the skill AND asserts every non-withdrawn `§8.5` HALT row in the ledger carries ≥2 distinct `NN §X` citations; struck-through withdrawn rows are exempt on purpose. Candidate 2 (pairing `§X.Y.Z` with `§X.Y`) refused as too narrow: it would have passed a halt citing two subsections of the same wrong section. Controls in `dev/test_invariants_gate.py` |
| **LSN-052** | harness, ci, goldens, checkpoint, binding §Build | `TestAgentsGolden` was red for three commits: the renderer gained a downward-API env var and the two golden fixtures were not regenerated. No step a unit is required to perform runs `go test` — L0 CI has no Go toolchain, `k8s-operator-test.yml` triggers only on PRs to `main` (once per phase, after dozens of commits), and CHECKPOINT §6.1's `make build` is fmt+vet+build, not test | **closed** | Candidate 1 adopted but **corrected by [[LSN-054]]**: the command is `make -C k8s-operator test`, never the bare `go test ./...` this row proposed, which is green over fifteen skipped envtest packages. PROTOCOL §3 done-condition 1 and `harness-run` §6 CHECKPOINT now require the §Build test entry point for every tree the unit touched; `dev/tests/invariants-gate.py` (`check_envtest_is_run_by_the_command_checkpoint_names`) asserts all four halves — the make target still resolves the assets, §Build names it and says why, CHECKPOINT names it, PROTOCOL §3 mentions tests at all. Candidate 2 was **not** an alternative and is closed separately as [[LSN-055]]. Controls in `dev/test_invariants_gate.py` |
| **LSN-053** | harness, checks, guardrail-9, rbac | A check is reshaped ahead of the implementation it will judge (Guardrail 9), it is green on today's tree, and nobody establishes that it is green on the tree the *next* unit will produce — so the next unit's first run reads as "my change broke the check" and the cheapest way out is to edit the check | **closed** | `harness-run` §4 now requires a check-only unit split under Guardrail 9 to exhibit BOTH trees, with the future-tree evidence as a committed `--negative-control` row rather than a `/tmp` probe — a probe proves it once, to one session. `dev/tests/invariants-gate.py` (`check_a_check_only_unit_exhibits_both_trees`) pins both halves of the rule and refuses to be green if no declared chain line runs a check with `--negative-control`, so the rule cannot outlive the affordance it names. Controls in `dev/test_invariants_gate.py` |
| **LSN-054** | harness, checkpoint, envtest, false-green, binding §Build, blocking-always | An envtest suite `t.Skip`s itself when `KUBEBUILDER_ASSETS` is unset, so the package reports `ok` and `go test ./...` is green over a test that is red — which is how a stale premise in a BLOCKING-ALWAYS check (**V-BRK-023**) survived to PR #83's first CI run. LSN-052's own preferred mechanization is that command, and it would have run straight past this | **closed** | Candidate 1 adopted, jointly with [[LSN-052]]: `binding.md` §Build names `make -C k8s-operator test` as the entry point for `k8s-operator/**` and marks the bare command as never one, and `dev/tests/invariants-gate.py` (`check_envtest_is_run_by_the_command_checkpoint_names`) pins it end to end. Candidate 2 (invert `requireEnv`) refused: it would redden every contributor's `go test ./...` to fix a harness rule, and the opt-out variable becomes the new silent skip. One level down, `dev/mutate.py` gains rule 7 (`suite.requires_env`) so a sweep over a skipping suite is BROKEN rather than nineteen verdicts — `go test -list` compiles rather than runs, so rule 5 could not see it — and `check_mutation_specs_declare_required_env` derives the requirement from the tree; it found a fifth spec (`V-REV-003`) the hand list had missed. Controls in `dev/test_invariants_gate.py` and `dev/test_mutate_sweep.py` |
| **LSN-055** | harness, ci, branching, attribution, checkpoint | The phase-9 branch took 25 CHECKPOINT commits and one `git push`, so `k8s-operator-test.yml` ran once, on the PR, against all 25 at once — and the red it found belonged to a commit 20 back. One CI run per branch makes CI a phase-end audit rather than a unit-level check, and destroys the attribution CHECKPOINT exists to produce | **closed** | Both halves landed together: `binding.md` §Branching gains a **Push cadence** row (`git push origin HEAD` at every CHECKPOINT), `harness-run` §6 condition 4 names it, and `.github/workflows/k8s-operator-test.yml` drops its `main`-only push filter. `dev/tests/invariants-gate.py` (`check_checkpoint_commits_reach_ci`) asserts the **pair**, so neither half can be removed on the grounds that the other covers it — a cadence pushing into a filtered trigger and a wide trigger nothing pushes to are both still one CI run. The cadence arm reads a table ROW, not the section body, because the prose explaining the rule contains the same words and kept the check green over its own rationale. Controls in `dev/test_invariants_gate.py` |
| **LSN-056** | harness, checks, ratchets, false-green, V-MET-003 | The assertion ratchet was last wound when the suite had **194** named tests in **34** files; the tree had **1290** in **137**. `check_assertion_ratchet` only fails when a *baselined* test disappears, so a baseline nobody regenerates silently narrows — it had been guarding 15% of the corpus and printing the same green as if it guarded all of it. Found while regenerating the baseline for an unrelated pass, not by any check | **closed** | Baseline wound 194 → 1290, plus `check_the_ratchet_baseline_covers_the_corpus` in `dev/tests/invariants-gate.py`: every named test in the tree must be in the baseline or in `retired`. Names, not just files — covering only files would let one file grow from 2 tests to 50 with 48 outside the ratchet. The instruction to regenerate had been in the baseline's own `_comment` the whole time, which is [[LSN-019]]: prose on the artifact is not a mechanization. Controls in `dev/test_invariants_gate.py` |
| **LSN-057** | harness, checks, corpus-discovery, false-positive, LSN-035 | `negative-controls-name-their-rule.py` discovered its corpus as *every `dev/tests/*.py` containing the string `--negative-control`*. The moment a check appeared that SEARCHES other files for that flag — `invariants-gate.py`'s new LSN-053 arm — the substring swept the searcher in and reported it as a control file with no control. The file's own docstring had already argued **WHY BEHAVIOURAL AND NOT STRUCTURAL** for the scoring half while the discovery half stayed textual | **closed** | `dev/tests/negative-controls-name-their-rule.py` (on `dev/L0-CHAIN.txt`): discovery is now a behaviour — a file is in the corpus if its own argv handling dispatches on the flag (`"--negative-control" in argv` and its spellings, or an `add_argument`) or if a usage line offers the flag against the file's own name. Splitting the two signals bought a new property for free — a usage line that offers the flag with nothing dispatching on it is its own finding, because the documented command then runs the ordinary check and prints its ordinary PASS. Two new cases in the file's own `--negative-control`, one of them the exact searcher that caused this |
| **LSN-058** | harness, tooling, false-red, concurrency, go | CHECKPOINT now requires both the L0 chain and `make -C k8s-operator test` ([[LSN-052]]/[[LSN-054]]), so the obvious saving is to run them at once. Doing that produced a red naming a file nobody wrote: five python suites create temp directories **inside** `k8s-operator/`, `controller-gen` runs with `paths="./..."`, and the directory is deleted while it is reading — `tmp109n_mmw/main.go:1: no such file or directory`, then `Error: not all generators ran successfully`. It points at no defect and vanishes on a serial re-run | **closed** | `dev/test_action_envelope.py`, `dev/test_envelope_wire_keys.py`, `dev/test_invariants_gate.py` (all three on `dev/L0-CHAIN.txt`): every `TemporaryDirectory(dir=…k8s-operator…)` takes `prefix="."`. Go tooling skips dot-directories under `./...`; Python globbing does not, so the suites that need to SEE the directory still do. Verified by re-running the two commands concurrently — the pairing that produced the red — and getting `GO=0 PYTHON=0` |
| **LSN-059** | harness, tooling, resource-leak, envtest, self-reinforcing | `make -C k8s-operator test` measures **2m09s** warm against a caller whose default time bound is **two minutes**, so it is killed seconds from finishing — and envtest starts a real etcd **and** kube-apiserver per test binary, stopped in `TestMain` after `m.Run()` returns, which a `SIGKILL` never reaches. The machine accumulated **32** adopted control planes, 30 at `ppid=1`, holding **1375 MB** of 16 GB, oldest ~31h. Nothing on the machine reaps them, no exit code mentions them, and every suite stays green: the only symptom is that the machine gets slower — which makes the next run likelier to hit the same bound and abandon the next cohort | **closed** | `dev/reap-envtest.sh` — anchored at the left edge of the asset root ([[LSN-005]] applied to a process) and predicated on `ppid == 1`, so a concurrent `make test` is never touched — wired into `k8s-operator/Makefile` as a **prerequisite** of `test` (the load-bearing half: it runs after however the previous run died) plus a `trap … EXIT INT TERM` (the tidy half). `dev/tests/invariants-gate.py` (`check_envtest_control_planes_are_reaped`, L0-CHAIN) holds all five halves including the two safety predicates inside the script; `dev/test_reap_envtest.py` (18 behavioural tests on real processes, `unittest discover dev`, L0-CHAIN); controls in `dev/test_invariants_gate.py`. The caller's own timeout is **not** mechanizable from this tree — argued in the body |
| **LSN-060** | verification, negative-control, l2, self-concealing | `broker-execute-l2.sh`'s L2-1 arm looked the ActionRecord up by raw action id. The object name is `journal.RecordName` = `"ar-" + strings.ToLower(actionID)` (06 §4.3), so the lookup **could not have found a record against any commit** — yet the suite had never reported it, because the only thing that had ever exercised that arm was `--negative-control`, which **synthesises the record document** and feeds it straight to the assertion block. The ¬ form skipped the very statement under test, so 13/13 green measured an arm that had never run | **closed** | The arm derives `record_name` from the action id and the lookup is now exercised by the live path (`dev/verify/broker-execute-l2.sh`, PASS L2-1 naming `ar-01kyv…`). `dev/tests/invariants-gate.py` (`check_negative_controls_exercise_the_statement_under_test`, L0-CHAIN) fails any `--negative-control` block that synthesises an input the live path obtains from the cluster, unless the arm names the statement it is skipping |
| **LSN-061** | go, kubernetes, api-semantics, subresource, silent-loss | `status` is a **subresource**: a Create keeps spec and metadata and **discards the whole status block**. `journal.Store.Create` had known that since Phase 5 — it re-writes `status.phase` afterwards — and put back exactly **one field** of the block it knew had been dropped. Everything else the broker composes at 06 §4.2 step 6 went with it, including the lifecycle clock the write-ahead rule exists to make observable; and the Create's reply body **overwrote the caller's copy**, so the pipeline reached step 8 holding a nil `status.timestamps` and **panicked on a live cluster** at the one moment a failure is unrecoverable. Symmetrically, `SetPhase` re-read the record and wrote the LIVE copy, discarding every field the caller had composed | **closed** | `mergeOwnedStatus` — the six fields 06 §4.3 assigns the owning broker SA, nil-guarded per field, snapshotted **before** the Create — used by both `Create` and `SetPhase` (`k8s-operator/internal/journal/store.go`). `state.clock()` makes the pipeline's stamping nil-safe. The journal package's fake client has modelled the subresource drop since it was written (`dropStatusLikeTheApiServer`); the pipeline's fake now models it too under `dropStatusOnCreate`. `verification/mutants/V-BRK-006.json` 15/15, M13/M14/M15 targeting exactly this. Run by `make -C k8s-operator test` (the L1 half) and by `dev/verify/broker-execute-l2.sh` (L2-CHAIN line 261), whose L2-2 arm is the property itself: `creationTimestamp` from the API server against `status.timestamps.executionStarted` from the broker |
| **LSN-062** | records, verification, orient | A harness record states that a file "does not exist", and the file is right there. `P9-T11g-2a`'s split note said `verification/traceability.yaml` did not exist. It had existed since `P8-T10` — 71 KB, 177 entries, V-MET-011's artifact, green on every run. The question actually asked was *"does 09 §8's `R-<doc>.<section>-<n>` requirement mapping exist?"* — a correct **no** — and the answer was written down against the **filename §8 happens to use for it** rather than against the question. The unit's conclusion survived; its published reason did not, and it shipped in a commit message, a phase-file row and a ledger cell before the next ORIENT caught it | **open** | Proposed for the next improvement pass: an L0 lint that reads `docs/build/*.md`, `.claude/harness/*.md` and `verification/*.yaml` headers for a repo path asserted absent ("does not exist", "is absent", "is not in the tree", "nothing at") and fails when the path is present — the claim is cheap to make, load-bearing for scheduling, and trivially checkable |
| **LSN-063** | checks, verification, mutation-testing | A negative-control row prints `MISS`, and the mutation it names never applied | **open** | Guarded in `requirements-are-enumerated.py` and, after it recurred one unit later, `coverage-ratchet-holds.py` (a mutated tuple equal to the base prints `BROKEN` and fails); a third recurrence in `load-bearing-coverage-is-full.py` widened it — a literal LINE NUMBER expires the same way a literal count does, and the mutation still applied, so only [[LSN-035]]'s per-row needle caught it; proposed for the next improvement pass: both halves for the other 18 `negative_control()` loops, hoisted into one helper and policed by `negative-controls-name-their-rule.py` |

**Open: 2 of 63**.

**The threshold was crossed and this file is the result** (`binding.md` §Thresholds: _"> 5 open ⇒
the next invocation is an improvement pass and nothing else"_). The improvement pass of 2026-07-25
mechanized all six open lessons and found three more while doing it — one from an escape in the
build (LSN-021), one from a mistake the pass itself made (LSN-022), and one from mutation-testing a
check the pass had just written (LSN-023). All three arrived closed.

The history is worth keeping, because the shape of it recurs. P8-T6 wired
`check_closed_lessons_are_executable` and it reopened **13 of 17** lessons on its first run — every
one of them closed against a check ID, a `binding.md` clause or a spec section, which is precisely
the thing LSN-019 says does not close a lesson. Eight were re-cited to artifacts that genuinely
exist and genuinely run; five had nothing behind them and were honestly open until now. Zero open is
not a claim that the harness is finished: it is a claim that every lesson written down so far has a
command behind it that exits non-zero when the defect returns, which is the only thing `closed` has
ever been allowed to mean here.

Two of the six had stood open behind a sentence that was true. LSN-011: "the flags this forbids
leave no trace a later check could find in the tree" — true, and the trace is on the forge, not in
the tree. LSN-003: "enforcement is per-check, which is a convention, not a mechanism" — true, and a
convention becomes a mechanism the moment something reads the declaration. A lesson called
unmechanizable is usually one where the obvious place to look is the wrong place; that is now the
first question this pass asks of anything still open.

---

## LSN-001 — A same-tag image is not evidence of the build under test

`images, live-verify` · **closed** 2026-07-25 (improvement pass) · recurred **three times** (Phase 3, Phase 6, first live install)

**Trigger.** A namespace-isolation escape was **admitted** on a live cluster while the source and
every unit test were correct. Later: chaos-recreated pods never appeared, because a controller
predating the Phase-5 hardening rendered pods the hardening VAP correctly rejected. Later still: a
live install silently ran the published `ghcr.io/gke-labs` controller even though the operator had
been built from local source.

**Root cause.** The deployed image was `:v0.1.0`/`:latest` with `imagePullPolicy: IfNotPresent`. A
same-tag image is **not** re-pulled, and the node keeps the old layer until a pod is recreated
against a freshly loaded one. In the third instance, `provision_03` called `make deploy` without
`IMG`, so `OPERATOR_IMAGE` was ignored outright.

**Generalize.** **A deployed artifact is not evidence of the build under test unless its identity is
verified.** Applies to every image, policy, CRD, and rendered manifest — not just this operator. The
failure is silent by construction: the old logic under-enforces, so the run reads green.

**Mechanization.** Two artifacts, because the lesson has two halves. **`dev/lib/preconditions.sh`**
`p1_assert_build_under_test` reads the pod's running `status.containerStatuses[].imageID` and
compares it against the local build's config digest — returning **three** states, not two, since
"could not look" is exactly how P1 became decorative (it maps to the caller's DEFERRED exit, never
to a pass). **`dev/tests/invariants-gate.py`** `check_l2_scripts_declare_preconditions` is what
makes it non-optional: every script in `L2-CHAIN.txt` must declare a `P1:` line naming the artifact
it is judging, and a script that declares one without calling `p1_assert_build_under_test` fails
with "the declaration is ahead of the code". Previously closed against `binding.md` **P1**/**P8**,
09 §9.3.1 and **V-CMP-002** — a precondition, two spec sections and a check ID, none of which
execute. V-CMP-002 (`test_image_provenance.py`) is real but proves the **publish** side; the defect
landed on the **running** side all three times.

**Verify.** Seven mutations run through `dev/mutate.sh` against `verify-phase7.sh` and
`tenant-isolation-l2.sh`, all caught, plus an unmutated control that stays green: delete the `P1:`
declaration · declare P1 as `yes` (names no artifact) · delete the `p1_assert_build_under_test` call
and keep the sentence describing it · delete the `P3:` declaration · waive P3 with a bare `none` ·
waive P1 with a bare `none` · point P6 at `/opt/data/config.yaml`. **Limit, stated:** a waiver with
a well-written but false argument passes — the lint measures that an argument was made, not that it
is true.

> It did **not** close as "remember to rebuild the image". That sentence had already been written
> down twice and forgotten twice.

---

## LSN-002 — A running pod is not evidence a policy works

`admission, live-verify` · **closed** 2026-07-25 (improvement pass)

**Trigger.** `kubeagents-system` was labelled Pod Security `enforce: restricted`, the bundled
LiteLLM and inference-replay pods had no `securityContext` — and everything stayed Ready. The gap
only appeared when a clean cluster refused to schedule them.

**Root cause.** Admission policies (PSA, VAP) evaluate **admission**. They do not evict, re-admit,
or re-evaluate objects that already exist. A pre-policy pod grandfathers itself and masks a renderer
that emits non-conforming objects.

**Generalize.** Never infer enforcement from the state of a running object. An admission property is
only observable at the moment of admission — force the recreation, or you are testing the past.

**Mechanization.** **`dev/tests/invariants-gate.py`** `check_l2_scripts_declare_preconditions`, P3
arm: a script that declares `P3 admission-recreate:` against a live object must contain, **in code
and not in a comment**, one of `p3_force_recreate` (in `dev/lib/preconditions.sh`), an explicit
`delete`, or `--dry-run=server` — the last because a server-side dry run admits in full and persists
nothing, so there is nothing to grandfather. A script that cannot honour P3 must waive it in writing
with an argument; "none" alone fails. Previously closed against `binding.md` **P3** and 09
§9.3.3/§11.2 — a precondition and two spec sections, none executable.

**Verify.** Deleting the `P3:` declaration and waiving it with a bare `none` both fail the gate
(mutations M4 and M5 above). The runtime half: `egress-enforcement-l2.sh` deletes its fixtures
before each negative and `closed-allowlist-l2.sh` re-applies server-side.

---

## LSN-003 — The check read a config layer the runtime does not use

`config, checks` · **closed** 2026-07-25 (improvement pass)

**Trigger.** Checks asserted against the `config.yaml` baked into the image and passed. The runtime
was reading the operator-rendered ConfigMap, which **shadows** it, and which said something
different.

**Root cause.** Two artifacts hold the same setting and only one is authoritative at runtime. The
cheaper one to read is the wrong one.

**Generalize.** A check must name the **runtime-authoritative** artifact and read that. Where a
value is rendered, the rendered copy is the truth and the source is an input.

**Mechanization.** **`dev/tests/invariants-gate.py`** `check_l2_scripts_declare_preconditions`, P6
arm — the lint proposed at the last pass, built at this one. Every L2 script declares `P6
runtime-authoritative:` naming the artifact it reads, and the declaration fails if it names a
**path** whose basename the operator also renders into a ConfigMap, unless it says `configmap` too.
The shadowed basenames are **derived, not listed**: `_shadowed_basenames()` reads the ConfigMap
`data` keys out of `k8s-operator/internal/**/*.go`, so a renderer that starts emitting a second key
is covered the day it lands. A hardcoded `{"config.yaml"}` would have been a memorial to this lesson
rather than a guard against it.

**Verify.** Rewriting `tenant-isolation-l2.sh`'s P6 to `/opt/data/config.yaml` fails the gate with
"the file is an input; the ConfigMap is the artifact that runs" (mutation M6 above). Adding a key to
a renderer's ConfigMap `data` map extends the check with no edit here.

---

## LSN-004 — A deny-list on a security boundary is a finding

`rbac, policy-design` · **closed**

**Trigger.** The VAP read-only ceiling was expressed as a **write-verb deny-list**. It admitted
`impersonate` — which is equivalent to cluster-admin. Found by the Phase-0 pre-PR adversarial
review, not by the suite.

**Root cause.** A deny-list is a claim about the complete set of dangerous verbs. That set is
open-ended and grows with the API.

**Generalize.** **Security policies are allow-lists.** Enumerate what is permitted (`verbs ⊆
get/list/watch`) and deny by default. A deny-list on a boundary is wrong even when it currently
happens to be complete.

**Mechanization.** **`dev/tests/invariants-gate.py` check 1** (`check_write_verbs_have_machinery`).
Every Role/ClusterRole carrying the `kube-agents/tier` label is scanned against an **allow-list** of
`get`/`list`/`watch`, copied verbatim from the `is-agent-rbac` CEL in `vap-agent-readonly.yaml`
rather than re-derived — so gate and runtime agree by construction. Anything else, including
`escalate`/`bind`/`impersonate`, fails unless the broker machinery exists. A deny-list of "write
verbs" is the shape of the original defect and cannot be reintroduced here without deleting the
comment that says so. Backed by **V-CTN-012**/**V-CTR-004** and 09 §11.4.

**Verify.** `dev/tests/negative-attenuation.sh` includes the `impersonate` ClusterRole as a standing
negative.

---

## LSN-005 — Guards match anchored patterns, never substrings

`destructive-tests` · **closed**

**Trigger.** The destructive-test guard matched contexts by substring (`*scratch*`, `*kind*`). A
production context named `gke_prod_…_kube-agents-dev-prod` would have satisfied it and been treated
as a disposable test cluster.

**Root cause.** Glob substring matching on a name that an attacker — or an ordinary naming
convention — controls.

**Generalize.** A guard that decides whether destruction is permitted is a security control. Anchor
it (`kind-*`, `gke-scratch-*` in a shell `case`), and give the guard **its own negative test**.

**Mechanization.** **`dev/tests/invariants-gate.py`** `check_destructive_guards_are_anchored`. Every
script under `dev/` that takes its context from the caller (`CTX="${1:-…}"`, 14 of them) must have a
`case "$CTX"` whose accepting arms match `^(kind

**Verify.** The guard is exercised against three prod-lookalike contexts and refuses all three
(LEDGER, Phase 0).

---

## LSN-006 — Well-formed is not enforced

`netpol, levels` · **closed**

**Trigger.** Per-tier NetworkPolicies were asserted structurally correct and the suite was green.
The cluster ran **kindnet**, which ignores NetworkPolicy entirely — nothing was ever blocked.

**Root cause.** The property is runtime enforcement; the check was a file grep. The level was chosen
for speed, not for what it could prove.

**Generalize.** **An enforcement property is proven on an enforcing substrate, or recorded
`deferred` — never green from a structural check.** A check's substrate is part of its definition
(SELF-IMPROVEMENT §4, "stub the dependency").

**Mechanization.** **`dev/verify/egress-enforcement-l2.sh`**, in `dev/L2-CHAIN.txt`: 18/18, each
negative preceded by a no-policy baseline that reaches the endpoint, against the **shipped rendered
policy** rather than a synthetic one. That last distinction is the lesson —
`dev/tests/egress-enforcement.sh` builds its own policy and so stays green whatever ships.
Precondition **P4**, `p4_assert_enforcing_dataplane` in `dev/lib/preconditions.sh`, is the half that
refuses to be fooled twice: it is an **allow-list of known-enforcing dataplanes** (`calico-node`,
`anetd`, `cilium`), so an unrecognised dataplane returns `deferred` rather than `pass`. A deny-list
(`if kindnet then defer`) gets this case right and the next one wrong, which is the same mistake one
substrate later. 09 §3/§9.3.4/§11.6 remain the statement of the rule; these are the things that
fail.

**Verify.** `dev/test_dataplane_precondition.py`, in `dev/L0-CHAIN.txt`, feeds the detector
fabricated `kubectl get ds` output for calico · anetd · kindnet · nothing and asserts only the first
two are accepted. Hermetic on purpose: the substrate it needs to prove the lesson against is the one
the loop no longer has.

**Substrate note (2026-07-26).** The kindnet cluster this lesson was learned on, and the dedicated
Calico target `kind-kube-agents-egress` that closed it, are both gone — L2 is one remote GKE cluster
on **Dataplane V2**, which enforces. The lesson does not weaken with them: it is why `V-CTN-020` was
a known liability for eight phases rather than a genuine pass, and it is the reason the dev cluster
had to be created with DPv2 (GKE cannot enable it on an existing cluster, so it is a create-time
choice or nothing). What changed is that the enforcing substrate is now the default one instead of a
second cluster stood up beside it. See also [[lsn-026]] and [[lsn-027]], the two other lessons whose
host left the loop that day.

---

## LSN-007 — Built, tested, and unreachable

`completeness, wiring` · **closed**

**Trigger.** `kage-router`, the event ingress, and the NetworkPolicies all had passing tests. In a
live install the router was at 0 replicas, no install path applied the policies, and the ingress had
no caller.

**Root cause.** Completeness was measured as "the component exists and its tests pass". Nothing
asserted it was reachable from the system it belongs to.

**Generalize.** **Completeness = exists AND wired AND exercised.** Three probes, all recorded, or
the component is not done.

**Mechanization.** `dev/tests/install-path-wired.py` — every numbered step script is invoked by its
driver, every driver reference resolves, provision/teardown are symmetric, teardown descends. Plus
**V-CMP-004** (`replicas > 0` in the default install) and **V-CMP-003** (no `REPLACE_WITH_*` in a
shipped manifest); 09 §5.1, §11.9. **P8-T5 adds `dev/test_image_provenance.py`** (**V-CMP-002**) for
a third form of unreachable this lesson's other two probes cannot see: wired correctly in-tree, but
pinning an image or tag no workflow publishes. `kage-router` — this lesson's own trigger — was in
that state for two further phases after it was first closed.

**Recurrence.** **2026-07-25, P8-T3.** This lesson was marked closed with mechanization
"**V-CMP-001**" — a check ID in 09 §6 that **no script implemented**. The closure asserted coverage
that did not exist, so the defect recurred: P8-T2 shipped `provision_13_apply_network_policies.sh`,
a correct step that renders and applies the three per-tier egress policies, and added it to no
driver. It also had no teardown. The unit's ledger row states the policies are "applied from an
install path"; for one commit they were not. Re-closed with a runnable check, which found both
defects on its first execution. The generalization — that a populated Mechanization field is not a
mechanization — is **LSN-019**.

**Verify.** Park a required Deployment at 0 replicas and confirm V-CMP-004 fails. For the wiring
half: comment out a `provision_NN` line in `provision.sh` and confirm `install-path-wired.py` exits
non-zero (this is one of its seven self-test controls). For the registry half: delete a publish
step, or retag any manifest to a tag no workflow produces, and confirm `python3 -m unittest
dev.test_image_provenance` exits non-zero (11 such mutations were run; all 11 fail).

---

## LSN-008 — Deferred read as done

`deferrals` · **closed**

**Trigger.** The scratch-GKE V-G checks were "pending" for five consecutive phases. Only the
explicit ⏸ label in the ledger kept them from reading as part of a green phase.

**Root cause.** A phase summary reports what ran. A check that never ran contributes nothing to the
summary — so absence looks like success.

**Generalize.** **Deferred is a first-class result**, with a named external blocker, an owner, and a
promotion condition. A deferral without an external blocker is a failure wearing a different label,
and reclassifying a failure as a deferral is a named reward hack. **A BLOCKING-ALWAYS check may
never be deferred** — if it cannot run, the build is not verifiable, and that is the finding.

**Mechanization.** **`dev/tests/invariants-gate.py`** `check_deferrals_name_blockers`, over the
ledger's **Deferrals** table: every row must name a blocker, an owner and a promotion condition, and
no row may defer a **BLOCKING-ALWAYS** suite unless the verification log records that check passing
at some other level. Mutation-tested by blanking one owner cell. Previously closed against
**V-MET-006** and 09 §9.6 — the check ID that describes this rule, which is exactly the closure
LSN-019 is about.

**Verify.** A deferral with no blocker fails V-MET-006.

---

## LSN-009 — A suite can shrink silently during a model change

`ratchets, refactors` · **closed** _(P8-T6: the pre-merge script now exists and runs)_

**Trigger.** The read-only → imperative conversion makes many read-only assertions genuinely
obsolete. Deleting one and gaining no replacement leaves the suite green and smaller, and nobody
notices.

**Root cause.** Green is a property of the checks that ran, not of the checks that should exist.
Removal is invisible to every signal except a count.

**Generalize.** **Tests are replaced, never deleted.** A retirement must name its replacement, the
replacement must exist first, and the total must not fall. Retired IDs are kept with a pointer,
never reused.

**Mechanization.** **`dev/tests/invariants-gate.py`** — `check_assertion_ratchet` (**V-MET-003**)
diffs the named-test inventory against `dev/assertion-baseline.json` across the whole corpus (Python
`test_*`, Go `Test*`, shell `check_*`/`assert_*`), and `check_retirements_name_replacements`
(**V-MET-004**) requires every retired entry to point at something that exists. The unit is the
**named test**, not the `assert` statement: "tests are replaced, never deleted" is a statement about
tests, and a raw-assert count fires on honest refactors — a gate that fires on honest work gets
disabled. Scope is deliberately **wider** than V-MET-003's letter (V-CTN/V-BRK/V-REV/V-ADV), because
only V-CTN-020 of those has an implementation today and the literal scope would guard one check and
read green forever.

**Verify.** Delete a security assertion with no replacement and confirm the ratchet fails the diff.

---

## LSN-010 — Prettier runs over the branch, not over your session

`ci, formatting` · **closed** · broke CI **twice**

**Trigger.** PR #3 went red on a golden fixture the session had never opened. PR #4 needed a
follow-up `style(phase2)` commit for skill docs and fixtures formatted in earlier commits.

**Root cause.** The session formatted the files it edited. CI runs `git diff --name-only
origin/<base_ref>...HEAD` and checks **every** changed `.md`/`.yaml`/`.yml` on the branch, including
files from earlier units.

**Generalize.** Run the project's formatter over the **full changed set relative to the base
branch**, computed the same way CI computes it — not over the files you happen to remember touching.

**Mechanization.** `.github/workflows/prettier.yml` is the binding verdict and runs on every PR;
`dev/toolchain-preflight.sh` records the local formatter's version so a skew is visible before the
PR rather than after it. `binding.md` §Build still carries the exact command with `origin/main` as
base.

**Verify.** `npx prettier --check` over the base-branch diff set is clean before every PR.

---

## LSN-011 — Never force a merge past a check

`merge` · **closed** 2026-07-25 (improvement pass)

**Trigger.** A milestone PR sits with the gate green except one red check, and `gh pr merge --admin`
is one flag away. Auto-merge makes this a standing temptation rather than an occasional one.

**Root cause.** The objective is "the phase is merged". Bypassing the check achieves the objective
without achieving the property.

**Generalize.** A red required check means the milestone is **not done**. Forcing it converts a slow
build into an untrustworthy one — every later green rests on an unverified base.

**Mechanization.** **`dev/tests/merge-provenance.py`**, run by
**`.github/workflows/merge-provenance.yml`** on every push to `main` and daily at 06:17 UTC. For
each squash commit on `main` it asserts: the subject names a PR, that PR's merge commit **is** this
commit (a `(#N)` in free text proves nothing), and every check run against the PR's head SHA
concluded green. The reopening note said the forbidden flags "leave no trace a later check could
find **in the tree**" — true, and the wrong place to look. The forge keeps the check-run conclusions
for the head SHA forever, so "was anything merged over a red check" is answerable exactly, after the
fact, without trusting anyone's memory. **Exit 2 = could not run** (no `gh`, no credential, no
network) and the workflow converts it to a failure, because "the audit could not run" and "the audit
found nothing" are the same output only in builds that turn out to have been lying to themselves.

**Scope.** The audit starts at the fork point (`INHERITED_FLOOR`) — the 187 commits authored
upstream resolve `(#N)` in **gke-labs**'s PR namespace and produced 187 false accusations on the
first run. The floor is itself checked: it must be an ancestor of a remote that does **not** carry
`docs/build/LEDGER.md`, so it cannot be walked forward past this build's own merges. What it still
cannot see, stated: a check that was never required and never ran leaves no red run to find
(V-MET-007 owns that), `--no-verify` skips local hooks that never produce a check run at all, and a
red re-run green **after** the merge reads as green.

**Escape found.** Its first correct run found one. **PR #10**, 2026-07-24, `docs: correct Kind test
flow to build & load local images`, merged with **prettier red** on `docs/build/HARNESS.md`. The
formatting defect is long since resolved; the merge is the LSN-011 instance and stays on the record
as a **carried** escape rather than being cleared, because clearing it would leave a check that has
never found anything. It predates every phase PR in this build (#11–#16), all of which are clean.

**Verify.** `python3 dev/tests/merge-provenance.py` → 15 merged PRs audited since the fork point, no
**new** red merge, 1 carried escape, exit 0. The one recurring benign failure — `Auto Request
Review`, a CODEOWNERS bot with no permission on a fork — is named and argued in the script rather
than filtered silently; a documented exception a reader can dispute is the difference between a
scope and an exemption.

---

## LSN-012 — The remote that carries the work is not the one you assume

`git, remotes` · **closed** 2026-07-25 (improvement pass)

**Trigger.** A diff against `main` shows tens of unrelated commits, or nothing at all, and a PR
opened against the wrong base contains none of the phase work.

**Root cause.** Local `main` tracks **`upstream/main`** (`gke-labs/kube-agents`), which has none of
the build; the work lives on the fork, currently **`origin`** (`adamparco/kube-agents`). Remote
names have changed across the build — `fork` then, `origin` now — so the name is not a reliable
handle.

**Generalize.** Resolve the work-carrying remote from `git remote -v` at run start and use it
explicitly for diff base, push, and PR base. Never rely on the branch's tracking ref for a base.

**Mechanization.** **`dev/git-preflight.sh`**, in `dev/L0-CHAIN.txt` and therefore in
`l0-checks.yml` on every PR. It identifies the work-carrying remote **by content** — the remote
whose `main` contains `docs/build/LEDGER.md` — then asserts local `main` tracks that remote and that
no branch tracks a different one. Resolving by content and not by name is the point: the name has
already changed once in this build (`fork`, then `origin`), so a check pinned to `origin` would be
checking a spelling. What it deliberately does **not** assert: that `origin` has any particular URL
(the upstream is a legitimate remote to have, and pinning a URL fails every other contributor's
clone — LSN-020's mistake), or fetch freshness (a check that needs the network is a check that fails
on a plane). CI has no local tracking refs, so assertions 2 and 3 say so out loud instead of passing
silently; assertion 1 is real there given `fetch-depth: 0`, which the same unit added to
`l0-checks.yml`.

**Verify.** On this clone at the time of writing, assertion 2 **failed** — local `main` tracked
`upstream/main` (gke-labs), exactly the state the lesson describes, undetected for six phases. Fixed
with `git branch -u origin/main main`; the check now exits 0 and would go red again the moment it
recurs.

---

## LSN-013 — Read the version from the server, not the client

`version-assertions` · **closed**

**Trigger.** The Phase-7 "this is a vanilla, non-GKE target" assertion false-failed on a Kind
cluster.

**Root cause.** It read the first `gitVersion` from `kubectl version`, which is the **client**
build. The host's `kubectl` is gcloud's, whose version string carries `-gke`, so a Kind cluster was
flagged as GKE.

**Generalize.** An assertion about the target must read a property **of the target**:
`.items[0].status.nodeInfo.kubeletVersion` from a node, not anything the local toolchain reports
about itself.

**Mechanization.** **`dev/verify/verify-phase7.sh`** B0, now listed in `dev/L2-CHAIN.txt`: it reads
`nodes[0].status.nodeInfo.kubeletVersion` from the **server** and fails when a target claimed to be
vanilla carries a `-gke` build string. The commented reason is the lesson itself — this host's
`kubectl` is gcloud's `-gke` client, so `kubectl version`'s first line would mis-flag every Kind
cluster as GKE. `binding.md` **P7** states the rule; this fails on it.

**Verify.** The Phase-7 gate passes on `kind-kube-agents-dev` with kubeletVersion `v1.31.2` and
would fail on a `-gke` node.

---

## LSN-014 — Two correct specs can describe an unbuildable system

`specs, audits` · **closed** 2026-07-25 (improvement pass)

**Trigger.** A systematic requirement-coverage audit found **eighteen** cross-document conflicts and
**ten** load-bearing mechanisms no component owned. The worst was invisible from either side: the
actor templates granted an agent what it acts on, and **nothing granted the broker permission to
write the `ActionRecord` journal it is required to write** — so invariant 3 could not be satisfied
by any implementation of the design as written. Others: broker port `8443` vs `8643`, `batchWindow:
5m` widening exactly the race the workflow spec claimed to close, an audit filter scoped to one
namespace that left three of four SLIs blind to the largest tier.

**Root cause.** Each document was internally consistent. Contradictions live in the **space
between** documents, where no single author is reading both statements at once, and a harness cannot
verify an implementation against a contradiction.

**Generalize.** Conflicts are found by a systematic cross-document audit, not by reading carefully.
Resolve them **in the source documents** — a resolution recorded only in the verification doc is a
fifth place for the truth to live. Keep the register, because a future edit can silently undo a
resolution.

**Mechanization.** **`dev/tests/spec-ids.py`**, in `dev/L0-CHAIN.txt` — the §14 lints that had been
written down as `L0` and never built. **V-MET-010**: every `V-XXX-NNN` cited anywhere in
`docs/design/**` resolves to a definition in 09, and every ID defined in 09 is cited by the doc it
says it verifies — both directions, because a check nobody cites is as broken as a citation with no
check. **V-MET-012**: every component in 05 §1 has a row in 09 §5.1 and every contract section in 06
has one in §5.2, so a spec cannot grow a load-bearing thing that no probe covers. **V-MET-013 for
check IDs**: one definition site, no duplicate rows. **V-MET-011** (the traceability matrix) is
**deferred** with a named blocker and a promotion condition in 09 §14 — deferring one lint in the
same edit that implements the other three is the honest form, and V-MET-006 enforces the shape of
that deferral.

**Escapes found.** Its first run found real drift, which is what a lint written weeks after the
audit is for. **`C-JR`** (journal reconciler) and **`C-AD`** (anomaly detector) — both "New (v1,
load-bearing)" in 05 §1 — had **no row anywhere in 09 §5.1**: two load-bearing components with no
Exists/Wired/Exercised probe, not listed as deferred or optional either, so their absence read as
coverage. Five 06 contracts (§2a, §2b, §3.1, §5, §6) were likewise absent from the §5.2 inventory.
All fixed in the specs in the same unit; the two that are genuinely deferred (§2a user-authorization
down-scoping, §6 mem0 backing) are now asserted **absent** rather than simply being missing.

**Verify.** Five mutations against the real specs, each caught by the intended group: delete the
`C-JR` row (V-MET-012a) · add a `## 11. Telemetry contract` section to 06 (V-MET-012b) · cite
`V-CTN-099` from 03 (V-MET-010 forward) · retarget a citation to a section that does not exist
(V-MET-010 reverse) · duplicate the `V-CTN-001` catalog row (V-MET-013). Plus 10 in-file self-test
controls. The §12 registers remain the record of the audit itself; §12.3's three self-created gaps
(N-1 policy generator, N-2 broker-digest allowlist, N-3 anomaly-baseline checkpoint) are tracked as
deferrals, not lessons.

---

## LSN-015 — A single-instance fixture cannot see a multi-instance conflict

`fixtures, topology` · **closed** (Phase 8, P8-T4)

**Trigger.** On the first real multi-tier install, the **second** agent in a namespace hung in
`ContainerCreating` with a multi-attach error. Every prior test had run exactly one agent per
namespace.

**Root cause.** The `system-metadata` PVC was a fixed namespace-scoped name with `ReadWriteOnce`,
while the data PVC was already per-agent. The designed topology co-locates tiers; the fixture never
did.

**Generalize.** A fixture that instantiates one of something cannot observe conflicts between two.
Where the design says N, the fixture must be N — cardinality is part of the property, not a
test-setup detail.

**Mechanization.** **`dev/verify/multi-agent-namespace-l2.sh`** (L2) creates **two Agent CRs of
different tiers in one namespace** and asserts each gets its own `<name>-system-metadata` claim,
that no bare namespace-scoped `system-metadata` claim exists, and that no PVC is referenced by both
Deployments. Backed at L0 by `agent_manifests_test.go:185`. Both exit non-zero if the naming
regresses — demonstrated by planting the old bare claim, which turned the run from exit 3 to exit 1
(2026-07-25).

**Residual.** The script's **CLAIM 2** — both pods actually Ready side by side — is **deferred, not
passed**, and says so with measured numbers. `ReadWriteOnce` excludes per **node**, so two pods
co-located on one node share an RWO claim without complaint: a single-node cluster cannot exhibit
the multi-attach, and "both pods came up" there would be evidence of nothing. This is LSN-015
applied to itself one level up — the fixture needs N=2 **nodes** for the same reason it needs N=2
agents. Unblocks on a 2-node Kind cluster with ≥6Gi allocatable (this host's Docker VM has 1.9Gi;
one agent pod requests ~2.7Gi). Carried to **P8-T8**'s live checklist.

**Closed by.** The defect itself is closed by construction: a multi-attach requires two pods
referencing one RWO claim, and the fixture proves no claim is shared. CLAIM 2 would only add that
nothing _else_ prevents coexistence — a multi-tier-install property, not this lesson's.

---

## LSN-016 — Nothing in a Go build ever compiles CEL

`codegen, levels` · **closed**

**Trigger.** P8-T1 rewrote the `allowedUsers` CEL rule to inspect entry content. `go build`, `go
vet`, `go test ./...`, `make manifests` and the new L0 validator were all green. The rule was not
valid CEL, and separately would not have installed even if it were.

**Root cause.** Two independent failures, both invisible below L2. (1) **`gofmt` edits markers.** A
kubebuilder marker is a line comment, and gofmt applies its legacy prose-quoting substitution to
comments: the adjacent-apostrophe pair in the CEL empty-string literal `''` became a single U+201D.
`make build` runs controller-gen _before_ `go fmt`, so the CRD generated in that same command was
correct and the corruption only reached the CRD on the _next_ generation. (2) **The API server
cost-bounds CEL.** A per-entry rule over a list with no `maxItems`, of strings with no `maxLength`,
is estimated as unbounded and the CRD is refused outright. Neither failure is reachable from
anything Go compiles.

**Generalize.** A generated artifact is not verified by the generator succeeding. When the toolchain
that _produces_ an expression is not the toolchain that _evaluates_ it, only the evaluator is
evidence — and for a CRD the evaluator is a live API server at install time. This is LSN-006
("well-formed is not enforced") one layer earlier: here the artifact was not even well-formed, and
five gates said it was.

**Mechanization.** Two layers, deliberately. **L0**: `dev/tests/closed-allowlist.py` check 3b
rejects any `+kubebuilder:` marker containing a typographic quote — the specific corruption, caught
in the tree. **L2**: `dev/verify/closed-allowlist-l2.sh` **L2-1** applies the CRD to a live API
server before asserting anything about its rules, which catches the general class (bad syntax, cost
overrun, unknown function) rather than one instance of it. The rule itself is now written quote-free
(`u.trim().size() > 0`) so the formatter has nothing to rewrite, and `allowedUsers` carries
`MaxItems=256` / `items:MaxLength=253`.

**Verify.** `make build && make manifests` twice in a row leaves the marker byte-identical, and
`closed-allowlist-l2.sh` L2-1 fails loudly if the CRD stops installing. Negative control:
reintroducing the quote pair trips check 3b (`--self-test`, control "gofmt-mangled marker
rejected").

---

## LSN-017 — A validator scoped to `git ls-files` is blind to the files the unit just wrote

`checks, corpus` · **closed**

**Trigger.** P8-T2's regression run failed `closed-allowlist.py` on
`dev/verify/closed-allowlist-l2.sh:23`. That file was written by P8-T1, committed unchanged in
`fa99f82`, and the same validator had reported exit 0 during P8-T1 — a green the ledger recorded as
evidence for **V-CTR-014 L0**.

**Root cause.** The corpus came from `git ls-files`, which lists **tracked** files. The unit's own
new file was still untracked when the check ran, so it was never scanned. The ordering that produces
this — write, check, record the green, then `git add` — is the normal ordering, which is what makes
it dangerous: the check is blind in exactly the window where the unit's new work lives, and it
reports that blindness as a pass.

**Generalize.** A check's corpus is part of the check. "Green" means nothing without knowing what
was scanned, and a corpus derived from git index state is a corpus that excludes new work by
construction. The corpus must be the tree **as it will exist after the commit**. Any other validator
enumerating via `git ls-files` inherits this — and here it is compounded by `.git/info/exclude`,
which hides the four force-added harness roots from `--others --exclude-standard` too.

**Mechanization.** `closed-allowlist.py` `tracked_files()` now unions three sources: `git ls-files`
(tracked), `git ls-files --others --exclude-standard` (new and not ignored), and a direct `rglob` of
the four force-added roots (`.claude/harness`, `.claude/skills`, `docs/build`, `dev`) filtered to
source suffixes. The offending file was also classified correctly — `closed-allowlist-l2.sh` is an
**assertion file**, permitted to name the retired identifier but still subject to the emission
guard.

**Verify.** Planted `SLACK_ALLOW_ALL_USERS` in an untracked scratch YAML; the check failed on it and
named the file. Before the fix it passed. The `--self-test` controls (9/9) still fire.

---

## LSN-018 — A `make` target applies to the wrong cluster and the context override is ignored

`targets, tooling` · **closed** (Phase 8, P8-T6)

**Trigger.** P8-T2 needed the Agent CRD on the new Calico cluster and ran `make -C k8s-operator
install KUBECTL="kubectl --context kind-kube-agents-egress"`. The override was silently ignored —
the Makefile pipes to bare `kubectl` — and the CRD landed on whatever `kubectl config
current-context` pointed at. It was the right cluster, by luck.

**Root cause.** The Makefile has no context parameter, so `install`/`deploy` are implicitly
addressed to ambient state. Passing `KUBECTL=` looks like it works because `make` accepts any
variable assignment, whether or not the Makefile reads it. A no-op override is worse than no
override: it produces a false sense of having been explicit.

**Generalize.** `binding.md` §Targets requires an explicit `--context` on every cluster command
because current-context may be the live GKE cluster `platform-agent-host`, which is
**install-verification only and not a destructive-test target**. A rule that shell commands follow
and build targets ignore is not enforced. Any command that reads ambient cluster state needs the
guard, not just the ones the harness happens to type by hand.

**Mechanization.** **`dev/tests/invariants-gate.py`** `check_make_targets_are_context_explicit`,
plus the `ctx-guard` target it enforces. `k8s-operator/Makefile` now derives `$(KUBECTL)` from
**`KUBE_CONTEXT`**, every one of the ten cluster-addressing targets depends on `ctx-guard`, no
deployment recipe may name a bare `kubectl`, and a command-line `KUBECTL=` — the exact no-op
override that triggered this — is a hard `$(error)` telling the caller which variable to use
instead. With `KUBE_CONTEXT` unset the guard reads the ambient context and **refuses** anything
outside anchored `kind-*`/`gke-scratch-*`, printing the command that would name it deliberately;
naming it explicitly is always allowed, because deploying to the live cluster on purpose is a real
operation and forgetting which cluster you are on is not. Verified: `KUBECTL=` refused,
`KUBE_CONTEXT=platform-agent-host` accepted, an ambient `platform-agent-host` refused with exit 2.

**Interim.** Verify placement after any `make install`/`deploy`: read back the object's
`creationTimestamp` with an explicit `--context`. That is how this was caught rather than assumed.

---

## LSN-019 — A lesson says `closed`, the mechanization field is populated, and the defect recurs

`lessons, mechanization` · **closed** (Phase 8, P8-T6)

**Trigger.** P8-T3 found `provision_13` invoked by no driver — the exact shape of **LSN-007**, which
the index recorded as `closed`. Its Mechanization field read "**V-CMP-001** (all three probes per
component ID in 05 §1)". V-CMP-001 is a check ID in a specification. No script implemented it. The
lesson had been closed against an intention.

**Root cause.** `SELF-IMPROVEMENT.md` §2–§3 requires a lesson to end in a mechanization rather than
prose, and the closure ritual is to populate the Mechanization field. A check ID satisfies the
ritual perfectly while enforcing nothing, because 09 is a specification and specifications do not
execute. The gap is invisible **because** the field is populated — a blank field would have been
noticed at the next ORIENT; a plausible one reads as done.

**Generalize.** **Closed means a command exits non-zero when the defect returns.** Not a check ID,
not a `binding.md` clause, not a precondition — those describe who should enforce, and describing
enforcement is what this repository keeps mistaking for enforcement (LSN-006: well-formed is not
enforced; LSN-016: five gates over uncompiled CEL; this one is the same error applied to the
harness's own memory). The status field must name an artifact and the artifact must be executable.

**Mechanization.** **`dev/tests/invariants-gate.py`** `check_closed_lessons_are_executable`. Every
index row marked `closed` must name at least one artifact that exists on disk **and** is invoked by
`dev/L0-CHAIN.txt`, `dev/L2-CHAIN.txt`, or a CI workflow with a real trigger. A
`workflow_dispatch`-only file does not count: nothing runs it unless a human presses a button, which
is the standing of a script nobody types. Its first run reopened **13 of 17** closed lessons; after
correcting the citations where a running artifact genuinely existed, **five** stayed reopened —
LSN-001, LSN-002, LSN-011, LSN-012, LSN-014. That is the outcome this lesson predicted, and the open
count crossing `binding.md`'s threshold of 5 is the designed response, not a reason to weaken the
rule.

**Interim.** When closing a lesson, paste the command and its non-zero exit on a reintroduced defect
into the ledger row. LSN-016 and LSN-017 were closed this way and neither has recurred; LSN-007 was
not, and did.

---

## LSN-020 — The linter passed locally because it skipped the rule, not because the rule held

`ci, tooling` · **closed** (Phase 8, P8-T6)

**Trigger.** P8-T5 recorded "`actionlint` on all 22 workflows → **exit 0**" in the ledger as
evidence, then went red on the PR with four `SC2140` findings. `actionlint` shells out to
`shellcheck` for `run:` blocks and **silently reports nothing when the binary is absent**. It was
not installed. The local run and the CI run were different checks wearing the same name and the same
exit code.

**Root cause.** The evidence recorded was the exit code of a command, and an exit code cannot
distinguish "the rule held" from "the rule never ran". Optional-dependency degradation is the normal
design of lint tooling — it is a convenience for the many contributors who lack the plugin — so
nothing was misconfigured and nothing warned. The failure mode is specific to graceful degradation:
a **missing** linter is loud, a **partial** one is silent.

**Generalize.** **A green from a tool is evidence only for the rules that tool actually ran.** This
is V-MET-014 ("a check that cannot fail is not evidence") turned on the toolchain instead of the
assertions: the harness has been careful that its _checks_ cannot be vacuous while trusting that its
_linters_ are whole. Before recording a lint pass as evidence, establish that the local tool has the
same rule set as the one whose verdict is binding — for actionlint that means `shellcheck` on
`PATH`, and the same question is open for every plugin-extensible linter in the chain.

**Mechanization.** **`dev/toolchain-preflight.sh`**, wired into `.github/workflows/actionlint.yml`
as a step before `actionlint` runs. It resolves `actionlint` through `PATH` **and** `$(go env
GOPATH)/bin` — a PATH-only lookup would have reported "not installed" on the very host where this
happened, skipped the shellcheck question, and exited 0 — then fails when `shellcheck` is absent,
because the runner has it and a local run without it checks no shell at all while exiting 0.
`pyflakes` is reported and **not** failed: whether the runner image supplies it is unmeasured, and
asserting parity we have not established would be this same mistake one level up. It also prints a
provenance line (`actionlint=v1.7.12 shellcheck=0.11.0 …`) for the ledger, and flags version skew
against the `ACTIONLINT_VERSION` the workflow pins — now equal at 1.7.12. Deliberately not a
unittest, so a contributor without shellcheck is not punished for the harness's defect.
Mutation-tested with a PATH that has actionlint and no shellcheck: exit 1.

**Interim.** `shellcheck` v0.11.0 installed on this host (`brew install shellcheck`); the P8-T5
re-run with it present found the four `SC2140` warnings that CI had found, and now exits 0 for the
reason claimed. Any ledger row citing a linter must name its **version and plugin set**, not just
the exit code.

---

## LSN-021 — A caller's flags outlive the parser they were written for

`clis, callers` · **closed** 2026-07-25 (improvement pass)

**Trigger.** `verify-phase3.sh` check **P3-K7** reported "no identity file rendered" for a day. The
renderer was correct, the check was correct, and the two had stopped being able to talk to each
other.

**Root cause.** P8-T4 removed `--github-cidrs` from `render_developer_team.py` — correctly: GitHub's
blocks are fixed in the egress template. `verify-phase3.sh` still passed it. `argparse` exits **2**
on an unknown flag, the invocation redirected `2>&1 >/dev/null`, so the bundle was never written and
the only visible symptom was a file that did not exist. The check then skipped its two VAP dry-run
assertions and went on to fail for a reason that named none of this. It cascaded: phase3 → phase6 →
phase7 all red, and the top-level symptom was three phases away from the cause.

**Generalize.** **A CLI's flags are a contract with callers it cannot see.** The parser and the
caller are edited by different units at different times, and nothing in a language toolchain
connects them — an unknown flag is a runtime error in a shell script, which is to say, no error at
all until someone reads the output. Two consequences: a renderer that fails must say **why**
(capture the rc and the stderr, never `>/dev/null` a command whose success you are about to assume),
and the flag surface needs a check that sweeps the **whole tree** rather than the callers you
remember.

**Mechanization.** **`dev/tests/cli-contract.py`**, in `dev/L0-CHAIN.txt`. It discovers every Python
CLI in the tree, extracts each parser's real flag set (`--help`, unioned across one level of
subparsers; an AST fallback for the four CLIs whose imports are unavailable offline), then greps
every `.sh/.md/.yml/.yaml/.py/.txt` and `Dockerfile` for invocations of those CLIs and fails on any
flag no parser accepts. The sweep is deliberately whole-tree: a check that knew about
`verify-phase3.sh` would have to be edited to notice the next caller, which is the same defect
wearing a check's clothes. `docs/build/**` is excluded as historical record.

**Also fixed.** `verify-phase3.sh` now captures the renderer's exit code and stderr and fails with
them. The absent-file symptom is still there, but it is no longer the _only_ symptom.

**Verify.** 8 self-test controls, all firing. Live: with the stale `--github-cidrs` restored,
`cli-contract.py` exits 1 and names file, line, flag and parser; with it removed, exit 0.
`verify-phase3.sh kind-kube-agents-dev` → ALL CHECKS PASSED, and `verify-phase7.sh` with it. **Not**
checked, stated: missing _required_ flags, flag _values_, and whether a flag belongs to the
subcommand actually being invoked (flags are unioned across subcommands, so `claim --report x`
passes).

---

## LSN-022 — `git checkout` restores from the index, and the index has never seen your work

`reverts, mutation` · **closed** 2026-07-25 (improvement pass) · **this pass's own mistake**

**Trigger.** Five L2 scripts had just gained hand-written PRECONDITIONS blocks and P1 assertions —
written, verified, not staged. A mutation test mutated three of the same files and reverted with
`git checkout <path>`. Two hours of work vanished. It was noticed only because the _next_ mutation's
output named three precondition fields that were supposed to be present and were not.

**Root cause.** `git checkout <path>` restores the path **from the index**, and the index held
`HEAD`. There was no reflog entry and no dangling blob to recover from: git had never been shown
those bytes. The revert was correct git and the wrong operation.

**Generalize.** A revert in a mutation test must be defined by **what the file contained a moment
ago**. Git can only answer _what the file contained at a commit or in the index_ — the same answer
exactly when the tree is clean, which is precisely when a mutation test is least necessary. The
general form: when a tool's undo is defined against a state you did not choose, it is not an undo.
This is also why mutation tests should not be run against a dirty tree casually — but "be careful"
is the sentence LSN-001 already survived three times, so the fix is a primitive, not a habit.

**Mechanization.** **`dev/mutate.sh`** — `mutate.sh FILE... -- COMMAND...` snapshots each file with
`cp -p`, runs the command, and restores from the snapshot on success, on failure, and on
SIGINT/SIGTERM, passing the command's exit code through. It refuses a path that does not exist (exit
2) rather than running a command over a file it cannot restore. **`dev/test_mutate.py`** (10 tests,
picked up by `python3 -m unittest discover dev` in `L0-CHAIN.txt`) is what keeps it honest, and its
central test is the LSN-022 scenario itself: a file with three live versions — HEAD, index, and
unstaged working tree — where only the working-tree bytes are correct to restore. A `git
checkout`-shaped implementation passes every other test in that file and fails this one.

**Found by it.** Writing the tests found two real defects in the first implementation. bash runs a
trap only when the **foreground** command returns, so `"$@"` on its own meant a SIGTERM was queued
behind the command: the script hung and the tree stayed mutated. And killing the command alone
orphaned what _it_ had spawned, which outlived the restore and could write the mutation back
afterwards — fixed by launching under `set -m` and signalling the process **group**. Both are the
failure this primitive exists to prevent, reached from inside the primitive.

**Verify.** 10 tests: restore after success, after failure, after SIGTERM; exit-code passthrough for
0/1/2/7/42; refusal on a missing path with the command never run; mode preservation; the
three-version git scenario; and a late-writing command that must not be able to undo the restore.
The whole precondition mutation battery for LSN-001/002/003 above was then run **through**
`mutate.sh`, and the tree came back byte-clean.

---

## LSN-023 — The check greps the whole file, and the comment describing the code satisfies it

`checks, corpus` · **closed** 2026-07-25 (improvement pass)

**Trigger.** Mutation-testing the _new_ preconditions lint, an hour after writing it: deleting the
`p1_assert_build_under_test` call from `verify-phase7.sh` while leaving its PRECONDITIONS block
intact left `invariants-gate.py` **green**. The lint's whole purpose is to catch a declaration that
the code does not back.

**Root cause.** The check asked `"p1_assert_build_under_test" not in text` over the file's full
contents. The declaration block four lines above the call says "**Asserted via
p1_assert_build_under_test**" — in a comment. A substring search cannot distinguish the claim from
the act, and here the claim was _required_ to be in the file by the same check. It was
self-satisfying by construction.

**Generalize.** **A check that verifies code does X must exclude the text that describes X from its
corpus.** This is LSN-017 ("a check's corpus is part of the check") pointed at a subtler boundary:
not which files, but which _lines within_ a file. The tell is a check whose subject and whose
evidence can appear in the same document — anything that lints a declaration against an
implementation, or a doc against the thing it documents, is in this class.

**Mechanization.** **`dev/tests/invariants-gate.py`** `_code_lines()`, applied to the P1 and P3
backing tests. It strips `#`-leading lines and trailing unquoted comments, so those tests read what
the script **does**. Quote tracking is deliberately naive: the only cost of getting it wrong is
keeping a line that was really a comment, which is the safe direction for a test that fails when a
string is **absent**.

**Verify.** The mutation that found it: delete the `p1_assert_build_under_test` call, keep the
sentence describing it. Before, exit 0; after, exit 1 with "the declaration is ahead of the code".
Both other precondition arms were re-run against the same corpus and still fire (M1–M8 in LSN-001's
Verify row).

---

## LSN-024 — The check reports the property absent, and the object was correct and the read was early

`checks, timing` · **closed** 2026-07-25 (P8-T8)

**Trigger.** Running `tenant-isolation-l2.sh` on the Calico target for the first time. It reported
`quota does not bound "requests.cpu" — it caps nothing on that axis` and the same for all five axes,
while section 2 of the **same run** proved the quota binds: a pod with no resources refused, a
200-CPU pod refused, `used.requests.cpu=500m` accounted. The quota was perfect. The check read
`.status.hard` with no wait, and the quota controller on that cluster took **21 seconds and five
empty polls** to write it. Five lines below, `.status.used` was read after a flat `sleep 3` against
that same 21-second controller, and had simply been getting lucky.

**Root cause.** A `.status` subtree is not part of the object you applied; it is a controller's
later reply to it. Reading one straight after the apply asks a question the cluster has not answered
yet, and an unanswered question comes back as the empty string — which is byte-identical to the
answer "this property does not exist". The check could not tell "not yet" from "not ever", so it
chose the alarming one. A `sleep` does not fix this, it just moves the guess: it encodes an
assumption about controller latency made on the day the number was typed, and every slower cluster
re-tests that assumption silently.

**Generalize.** **An assertion on state a controller writes must be reached by polling for the
value, not by waiting a while and hoping.** The field being read is itself the readiness signal, so
there is never a need to guess — poll it, or `kubectl wait --for=` something that implies it. This
is the same shape as P2 (a new VAP binding activates late, so poll a dry-run until it actually
rejects) generalized off admission policies and onto every `.status` read.

**Mechanization.** **`dev/tests/invariants-gate.py`** `check_l2_status_reads_are_polled`, registered
as precondition **P9** in `binding.md` and run on every PR through `L0-CHAIN.txt`. Over the
transitive L2 closure it finds every `jsonpath={.status.…}` read and requires each to sit inside a
poll loop, be preceded by a `kubectl wait --for=`, or live in a function whose call sites are
themselves loops (which is what makes `chaos-suite.sh`'s `is_ready()` correct). Deliberately a rule
about the **read** and not a ban on `sleep`: seven sleeps in this corpus wait for Calico to program
a NetworkPolicy, where no readiness field exists to poll and the sleep is the honest primitive. A
lint whose findings are mostly legitimate is one that teaches people to write exemptions.

**Verify.** **4/4 mutations caught**, two of which are byte-exact reconstructions of the real
defects: restoring the unwaited `.status.hard` read → flagged at line 143; restoring `sleep 3`
before the `.status.used` read → flagged at line 210. Plus deleting the `kubectl wait` that
synchronises the three `podIP` reads in `egress-enforcement-l2.sh` → flagged; and breaking the
corpus pattern so nothing matches → the check reports itself **VACUOUS** rather than green
(V-MET-014). Clean tree: 10 reads found across 15 scripts, all synchronised, gate 10/10 exit 0.

---

## LSN-025 — The selector names a role, not an object, and still matches the generation you deleted

`checks, identity` · **closed** 2026-07-25 (P8-T8)

**Trigger.** `verify-phase3.sh` failed **2 runs in 3**, always with empty reads — `pod SA is ''`,
`pod image '' not developer-team-agent:<tag>`, `pod tier label is ''`. The give-away was a run that
read `.spec.serviceAccountName` **successfully** and then got `''` for the image and the tier one
kubectl call later, and a run that pinned the pod name its **previous** run had created. The same
full-chain log showed the same script green under phase 6 and red under phase 7, which reads as
environmental until you notice both are the same coin toss. `verify-phase2.sh` carried a
byte-identical block and was green throughout.

**Root cause.** `p3_force_recreate` returns the instant the **Deployment's** uid changes. Pod
garbage collection happens after that, asynchronously, so at the moment the caller starts looking
the pod of the generation P3 has just deleted is still listed — and carries no `deletionTimestamp`
yet, so even filtering on that does not exclude it. A `-l app=…` poll therefore returns the OLD pod,
which is the precise object P3 exists to keep the assertion away from, and three separate
`.items[0]` reads then re-resolve a list that GC is emptying between them. Not a race that waiting
longer fixes: the wrong object was being waited for.

**Generalize.** **A label selector names a role, not an object.** Two generations of a workload
answer to it, and the one you just deleted answers first and longest. Anything that has just
replaced an object must reach the replacement by **identity** — the ownership chain, or a name
captured once — and every field of one assertion must come from that single pinned object.
Corollary, and the reason this kept being missed: the failure mode is an **empty** read, never a
wrong value, so it is indistinguishable from the property genuinely being absent (the symptom
[[lsn-024]] documents, arrived at by a different route).

**Mechanization.** `p3_pod_of_deploy` in **`dev/lib/preconditions.sh`** resolves Deployment uid →
owning ReplicaSets → a live Pod owned by one of them, and echoes one name. Enforced by
**`dev/tests/invariants-gate.py`** `check_p3_pods_resolved_by_ownership`, registered as the second
half of precondition **P3** and run on every PR through `L0-CHAIN.txt`: no script that calls
`p3_force_recreate` may index into `.items[]`. Scoped to those scripts on purpose — `.items[0]` off
`get nodes` on a single-node Kind is a far weaker claim, and a lint whose findings are mostly
legitimate teaches people to write exemptions.

**Verify.** **4/4 mutations caught**, two of them byte-exact reconstructions of the real defects:
restoring the selector poll + three `.items[0]` reads in `verify-phase3.sh` → flagged; the same in
`verify-phase2.sh` → flagged. Plus breaking the call pattern, and removing one of the two callers →
the check reports itself **VACUOUS** rather than green, naming the count it expected (V-MET-014).
Behavioural evidence, which is the part a lint cannot give: **6/6 clean runs** of each script, each
resolving a **different** pod name, where before two consecutive runs latched onto the same stale
pod. Gate 11/11, exit 0.

---

## LSN-026 — Several unrelated security properties "fail" at once, and the cluster is what is broken

`checks, infrastructure` · **closed** 2026-07-26 (P8-T8c) · opened 2026-07-25

**Trigger.** `verify-phase8.sh`'s first end-to-end run reported that tenant isolation did not hold,
that the egress default-deny did not hold, and that chaos C2 failed to replace a deleted pod. All
three were **false**. `kube-scheduler` and `kube-controller-manager` were in CrashLoopBackOff on
both Kind clusters — 31 and 29 restarts on `dev` — losing their leader leases because API-server
calls were timing out under host memory pressure. The Docker VM had **2 GiB total and 2 CPUs**
(Colima's stock defaults, never raised) and was carrying two Kind control planes. The give-away,
visible only in hindsight: three properties with no code in common do not regress in the same run.

**Root cause.** With no scheduler, fixture pods stay `Pending` forever; with no controller-manager,
a new namespace never gets its `default` ServiceAccount and pod creation fails `Forbidden`. Every
enforcement check is built the same way — create a fixture, try the thing that should be denied,
observe that it was denied. When the fixture never runs, the thing that should be denied never
happens, and the check observes exactly what a **working** policy looks like… on the way to
concluding the property is ABSENT. This is [[lsn-024]]'s shape — an empty read is indistinguishable
from the property being missing — aimed at infrastructure rather than at timing. Reachability was
being mistaken for health: every one of these clusters answered `kubectl version` throughout.

**Generalize.** **Before believing a claim about a cluster, assert the cluster can still run the
experiment — and probe the capability, not a proxy for it.** Reading `.status.phase` of the static
pods would have caught this particular outage, but it answers a question about pods when the
question is whether the control plane still converges; creating a namespace and waiting for the
ServiceAccount its controller must write is the same claim stated as an experiment, and costs about
a second. Two corollaries the first draft got wrong. **(a)** The verdict is `rc 2`, could-not-run,
**never `rc 1`** — an unhealthy cluster is not a failed security property, and "tenant isolation
does not hold" is a sentence someone acts on. **(b)** "Healthy now" is the wrong property: a
CrashLoopBackOff answers every liveness probe during its up-swing, and an L2 suite runs for half an
hour, so a recent restart is the available evidence against it. Steps 1–3 of this very check all
passed against the cluster that produced the three false failures.

**Mechanization.** `p10_assert_control_plane_healthy` in **`dev/lib/preconditions.sh`**, registered
as precondition **P10** in `binding.md`, called by **every one of the 15 cluster-reading scripts in
the L2 transitive closure** -- not by the phase gate on their behalf, because each is independently
runnable (which is how [[lsn-025]] was found) and on a sick cluster each still produces its own
false failure. The floor is a rule rather than a convention:
**`check_l2_scripts_assert_cluster_health`** in `dev/tests/invariants-gate.py` (L0-CHAIN, therefore
CI on every PR) fails any script in the closure whose CODE calls `kubectl` and does not call P10.
Scoped by what the script DOES, so there is no exemption ladder: `otel-endpoint.sh` is out of scope
because it reads a Dockerfile and never contacts a cluster, and adding a `kubectl` line to it puts
it in scope automatically -- the opposite of a roster someone must remember to extend. Memoized per
target, so the nested calls down a `verify-phase7 -> 5 -> 2` chain cost ~6 ms.

**Verify.** Three defects found in P10 by **running** it, each fixed and re-tested: probe namespaces
collided on `$$` (now server-assigned via `generateName` — verified on the exact shape that broke
it, three piped calls all reporting the true failure); nested callers re-probed (memoized, 0.600s →
0.006s, second target still live, failures never cached); and the managed-control-plane branch
returned early past the flap check. Behavioural evidence for the check itself: **rc 2 on both
clusters** while they were inside the 900s flap window after a VM restart, **rc 0 on both** once it
elapsed, with steps 1–3 passing throughout — i.e. the flap arm is the only thing standing between a
just-booted cluster and a green verdict. `P10_FLAP_WINDOW=1` flips it, confirming the window is what
decides. The BSD/GNU `date -u` parse was checked on this host: deltas come out positive (`+830s`),
where the pre-`-u` draft produced negatives that flagged every restart ever recorded. The gate arm
was mutation-tested three ways after it went green, since a check written against a corpus it
already satisfies proves nothing: deleting the call from `chaos-suite.sh` and commenting it out in
`tenant-isolation-l2.sh` (the LSN-023 shape -- paragraph kept, call gone) each turn it red naming
that file, and raising the caller floor by one produces the VACUOUS message rather than a pass.
Restoring all three returns 13/13.

**Substrate note (2026-07-26).** The two Kind-in-Colima clusters whose control planes crashlooped
here no longer exist; L2 is a managed GKE control plane, where this specific outage cannot happen.
That is not a reason to retire P10, and the lesson was careful about why before the move made it
matter: the mechanization is a **capability probe** — create a namespace, wait for the
ServiceAccount its controller must write — not a reading of `kube-scheduler`'s pod status. It
therefore says nothing about Kind, and `p10_assert_control_plane_healthy` already carried the
managed-control-plane branch (`sched_note`, "managed/unobservable scheduler; not asserted") before
the move, because a managed cluster does not show you its static pods. A wedged cluster of any kind
still fails the probe, and a GKE control plane mid-upgrade or out of quota is exactly the shape this
catches. All 15 callers and the gate arm are unchanged. See also [[lsn-006]] and [[lsn-027]].

---

## LSN-027 — A preflight reports the host is fine, and the cluster still refuses to start

`infrastructure, preflight` · **closed** 2026-07-26 (P8-T8c)

**Trigger.** `up-2node.sh`'s first run died at kubeadm `wait-control-plane`: `/healthz` context
deadline exceeded, 4m0s. The memory preflight written two days earlier for [[lsn-026]] had just
printed **5758Mi of headroom** and the host load was 1.79 across 6 cores, so the one resource anyone
had thought to measure was not merely adequate but abundant. The actual limit was
`fs.inotify.max_user_instances`, sitting at the Linux default of **128** and already consumed by the
two Kind clusters on the VM. Nothing in the kubeadm output contains the words inotify, watch, or
limit.

**Root cause.** Every Kind node's containerd opens fsnotify watchers; past the instance ceiling the
CRI plugin never registers, and the log line that says so is three layers below where the error
surfaces: containerd → `failed to create cni conf monitor: failed to create fsnotify watcher: too
many open files`; kubelet → `unknown service runtime.v1.RuntimeService`; kubeadm → an HTTP timeout
on `/healthz`. What the operator reads is a **timeout**, whose entire vocabulary is slow-and-small —
so they go and look at memory and CPU, where the preflight has already certified everything is fine.
A green preflight is not neutral here: it is positive evidence pointing the wrong way. And the
resource it measured was memory precisely because memory is what failed **last** time. A preflight
grown one incident at a time always measures the previous outage.

**Generalize.** **A host-capacity preflight is only evidence about the resources it names, and a
timeout is not a statement about size.** Three parts. **(a)** When a component times out, read _its_
log, not the log of the thing that reported the timeout — the reporter is usually the outermost
layer and knows least. **(b)** Do not act on a plausible guess about the resource; I suspected
inotify and was right, and it would have been wrong to fix it on that basis, because a guess that
happens to work teaches the harness to guess. `kind create --retain` keeps the failed node so the
containerd log can be read, and that is a one-line cost. **(c)** The preflight should print what it
checked and what it did not, so "headroom: 5758Mi" cannot be read as "the host is fine". Per-host
finite resources are a small enumerable set — memory, CPU, inotify instances, inotify watches, PIDs,
file descriptors, loop devices — and each has a failure signature that looks like something else.

**Mechanization.** **`dev/lib/substrate-capacity.sh`** — one definition site per substrate, each
entry point refusing with **rc 2** and the exact remediation. The two functions this lesson was
written against, `assert_host_capacity` (Docker VM memory, inotify instances), were **deleted on
2026-07-26 with the host they measured**; the live entry point is `assert_project_capacity`, and
that is this mechanization holding rather than lapsing — see the parse-the-annotations sentence
below. The floor is a rule rather than a habit: **`check_cluster_creating_scripts_assert_capacity`**
in `dev/tests/invariants-gate.py` (L0-CHAIN, therefore CI on every PR) fails any script under `dev/`
whose CODE runs a cluster-creating command without calling the preflight that covers it. The command
-> preflight map is PARSED out of `# @covers:` annotations in the library rather than hardcoded in
the gate, which is what carried this lesson intact through the move off the host that taught it
(2026-07-26): `assert_project_capacity` measures project, service APIs, regional CPUS quota and
Artifact Registry reachability, and the rule needed no edit to start policing it. Retiring a
substrate is a deletion, after which nothing matches that command and the map shrinks on its own --
a hardcoded table would instead have gone quietly green. This closed at the same moment three
clusters became one — with a single `up-*.sh` left there is exactly one caller, and the check is
what stops the second one from being written without a preflight, which is how the memory floor
briefly existed twice with two different values.

**Verify.** Mutation-tested on the live host in both directions: with `max_user_instances=128` the
script refused with the full remediation and created nothing; raised to 512, the same script ran
through to a 2-node cluster with CRD, controller, webhooks and VAP. The gate arm was mutation-tested
too — commenting out the `assert_host_capacity` call in `up.sh` turns the check red naming that
file, restoring it turns it green (12/12). It also found a real defect on its FIRST run, which is
the evidence that matters most: `egress-enforcement.sh` printed "Stand one up: kind create cluster
--config kind-config.yaml", advice that now yields a cluster with the default CNI disabled and no
Calico — every node NotReady. The diagnosis behind the lesson was verified rather than assumed: the
first failed cluster was re-created with `kind create --retain` and `docker exec` into the retained
node produced the containerd line naming the resource, which is the only reason this entry says
inotify instead of "probably memory again". Note for whoever hits this next: **the sysctl does not
survive a Colima restart**, so a host that passed yesterday can refuse today; that is the check
working, and the `provision:` block in the refusal text is the fix.

---

## LSN-028 — An allowlist denies the destination it lists, and the over-block reads as a fixture bug

`netpol, substrate` · **closed** 2026-07-26 (P8-T8c)

**Trigger.** First run of `dev/tests/egress-enforcement.sh` on the remote dev cluster. Baseline green
— both server pods reachable with no policy — then, with a default-deny plus a one-entry allowlist
applied, BOTH probes denied, including the one the allowlist explicitly names. Same script, same
fixture pods, same policy text had been green for weeks on Kind + Calico. The verdict line read
`egress enforcement FAILED … (HALT, Acc b)`, which is the loudest thing a security suite can print,
and the assertion that produced it was the ALLOW.

**Root cause.** The allow rule was `ipBlock: <allowed-server podIP>/32`. GKE Dataplane V2 is Cilium,
and Cilium resolves pod-to-pod traffic by **endpoint identity**: a CIDR selector names no identity,
so the rule selected nothing and the destination fell through to the default deny. Calico honours the
same rule, which is why the fixture had never been wrong before. `ipBlock` is neither broken nor
deprecated — it is the governing mechanism for addresses that have no endpoint identity, i.e.
everything **outside** the cluster (the metadata server at 169.254.169.254, the hub CIDR). The
production tier netpols this fixture calls itself "the same shape as" already split the two: selector
for in-cluster, ipBlock for external. The fixture had collapsed both onto ipBlock, and exactly one
dataplane in the world was ever going to complain about that.

**Generalize.** **An over-blocking allowlist fails SAFE, so the red line it produces points at the
fixture rather than at the policy — and the cheapest way to clear it is to stop asserting the thing
that broke.** Relaxing that ALLOW would have left a suite that only ever proves denial, on a suite
whose entire purpose is that a pure allowlist admits exactly what it lists: green, smaller, and
worthless. That is [[lsn-009]] arriving through a security failure instead of through a refactor.
Two rules follow. **(a)** A policy fixture must exercise each rule KIND against the destination CLASS
that rule kind governs. A fixture that uses one selector for every destination is testing whichever
dataplane interprets that selector most permissively, and it passes wherever it happens to run.
**(b)** When a check that has been green for weeks turns red on a new substrate, the substrate is the
first hypothesis and the fixture the second — and neither is a conclusion until it has been measured.
This is [[lsn-006]] inverted: there, every NetworkPolicy assertion was green on a dataplane that
enforced nothing. Here the dataplane enforces more literally than the fixture assumed. Both are one
error — believing a policy result without knowing which dataplane produced it.

**Mechanization.** **`dev/tests/egress-enforcement.sh`** §3/§4, run by `verify-phase5.sh` under
`verify-phase7.sh`, which is line 1 of `dev/L2-CHAIN.txt`. The allowlist now carries three rules and
each is asserted against what it actually governs: kube-system:53 for DNS, `podSelector:
role=allowed-server` on TCP 80 for the in-cluster pair, `ipBlock: 1.1.1.1/32` on TCP 443 for the
external pair — four assertions, two allow and two deny. The fix is portable, not conditional:
podSelector is honoured by Calico and Cilium alike, so there is no `if dataplane` branch here to go
stale (P4's allow-list of enforcing dataplanes is still what decides whether the suite may run at
all). The external arm is gated on both external hosts being reachable BEFORE the policy is applied
and is **SKIPPED, never passed** when they are not — an ipBlock deny on a cluster with no internet
egress is not evidence, it is the ambient state. The measured numbers live in the comment block above
the policy, because the next person to meet that red line will be looking for permission to delete
the assertion, and the comment is what is in front of them when they do.

**Verify.** Established by controlled experiment rather than by inference, on
`gke-scratch-kube-agents-dev` (anetd / Dataplane V2), same two pods throughout: no policy =>
REACHABLE; policy with `ipBlock: <podIP>/32` => BLOCKED; policy differing in that one rule,
`podSelector: role=allowed-server` => REACHABLE. The first attempt at that experiment ran under zsh
and reported all three as BLOCKED: `K="kubectl --context $C"` is not word-split there, so every probe
was a `command not found` and every result was a false negative that happened to agree with the
hypothesis. Re-run under `bash`, which is what produced the three lines above — a wrong shell is the
[[lsn-021]] shape, a command that "runs" and does nothing. Post-fix the suite is 4/4 with the
external arm live: on-allowlist in-cluster (10.68.0.53) ALLOWED, off-allowlist in-cluster
(10.68.0.54) DENIED, on-allowlist EXTERNAL (1.1.1.1:443) ALLOWED by ipBlock, off-allowlist EXTERNAL
(8.8.8.8:443) DENIED — `EGRESS ENFORCEMENT: PROVEN`. See also [[lsn-026]] and [[lsn-027]], the other
two entries whose trigger was a change of substrate.

---

## LSN-029 — A portability fallback runs the wrong tool first, and the failure arrives on stdout

`portability, substrate` · **closed** 2026-07-26 (P8-T8c)

**Trigger.** The L0 chain was green on the Mac for the whole campaign and red the first time it ran on
Linux. Moving the inner loop to remote GKE is what put it there: L0 now runs on `ubuntu-latest` and
inside Cloud Build, and both reported exactly one failure —
`test_a_dirty_build_newer_than_the_edit_is_fresh` in `dev/test_build_under_test_precondition.py`.
One test, on the machine that had never run the suite before, in code that had been stable for phases.

**Root cause.** `_p1_mtime` in `dev/lib/preconditions.sh` was written
`stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null` — BSD first, GNU as the fallback. The two
`stat` implementations do not merely spell the flag differently, they **collide**: BSD `-f` takes a
format string (`%m` = mtime), GNU `-f` reports **filesystem** status and ignores the argument as a
format. GNU then does the one thing the `A || B` idiom cannot absorb — it writes its answer to
**stdout**, not stderr, and exits non-zero. Measured on coreutils 9.7: 245 bytes across 6 lines of
filesystem block, after which `||` fires, the GNU branch appends the real epoch, and the caller
captures all seven lines as "the mtime". The comparison against it exits **rc 2**, which is neither of
the two values P1's three-state contract defines. `2>/dev/null` suppressed nothing, because nothing
had gone to stderr. Eleven lines below, `date -u -d … || date -j -u -f …` was already GNU-first and
already correct — the file contained both orderings and no rule saying which was which. Three more
`date -r` calls sat in P1's FAILURE messages, BSD-first, in a branch no green run has ever entered.

**Generalize.** **The `A || B` portability idiom is only sound when the losing branch is silent, and
"silent" is a property of the specific flag collision, not of the tool.** Where the flags merely
differ, the wrong one exits with a usage error on stderr and the fallback works. Where they collide —
`stat -f`, `date -r`, `date -j` — the wrong one succeeds at something else and pollutes stdout, so the
fallback runs but its output is a suffix. The rule that follows is one-directional and therefore
checkable: **write the GNU form first**, because GNU is the side whose collisions produce output.
Second: **a portability bug in a failure branch is invisible to every green run**, so CI running on
Linux does not find it — only reading the text does. The same substrate change surfaced a second bug
of the identical shape: `dev/test_capacity_preflight.py` scrubbed `PATH` to
`{stub_dir}:/usr/bin:/bin`, which hides the real `gcloud` on macOS (Homebrew and the SDK tarball
install elsewhere) and **exposes** it on Debian, where the SDK package ships `/usr/bin/gcloud`. A
hermetic, no-network L0 test was making live authenticated GCP calls and printing the project's real
CPU quota. One host assumption in a flag, one in a path, both invisible until the host changed —
which is [[lsn-026]], [[lsn-027]] and [[lsn-028]] arriving through the toolchain instead of through
the cluster.

**Mechanization.** **`invariants-gate.py` `check_platform_idioms_are_gnu_first`**, on the L0 chain. It
scans every `*.sh` under `dev/`, `hack/` and `k8s-operator/scripts/` for the three colliding pairs
(`stat -c` before `stat -f`, `date -d` before `date -r`, `date -d` before `date -j`) inside a 12-line
window, reading `_code_lines` so a comment describing the idiom cannot satisfy it ([[lsn-023]]), and
reporting `VACUOUS` rather than green if fewer than 20 scripts are in scope. It recovers the real file
line by matching the stripped text back to the original, so its finding is a clickable location and
not an index into a filtered list. `_p1_mtime` is now GNU-first with an explicit numeric validation on
each branch — `case "$t" in '' | *[!0-9]*)` — so a stdout leak from any future third implementation
is rejected rather than compared, and the three `date -r` call sites route through one
`_p1_human_epoch` helper. The hermeticity hole is closed by an **asserted post-condition** in
`dev/test_capacity_preflight.py`: after the scrub, `shutil.which("gcloud", path=e["PATH"])` must
resolve to the stub or to nothing, and a missing tool in the curated symlink farm raises
`AssertionError` — never `SkipTest`, because a skipped hermeticity test is the same green as a passing
one ([[lsn-021]]).

**Verify.** Both platforms, measured. Old `_p1_mtime` on GNU coreutils 9.7 returns the 6-line
filesystem block plus the epoch and drives the caller to rc 2; new `_p1_mtime` returns the exact epoch
3/3 on macOS and 3/3 on Linux, and rc 1 with empty output on a missing file. `test_build_under_test_precondition.py`
is 12/12 on each. The full L0 chain is **13/13 on macOS and 13/13 on Linux** — the Linux run inside
Cloud Build against the whole tree, with `gcloud` at `/usr/bin/gcloud` to keep the leak reachable had
it survived. The gate check was mutation-tested: reverting `_p1_mtime` to BSD-first fails the gate and
reports `dev/lib/preconditions.sh:98`, the actual line; restored, the gate is 14/14.

---

## LSN-030 — Work you finished an hour ago is gone again, and the verb that ate it is not the one you guarded

`git, reverts` · **closed** 2026-07-27 (improvement pass) · **this pass's own mistake** ·
recurrence of [[lsn-022]]

**Trigger.** P8-T10 was finished: the traceability matrix, the V-MET-011 lint, and the hierarchy
edits across 01/02/07/09 — written, run against the L0 chain, not yet staged. The next command was a
compound one-liner that ended `git fetch origin -q && git reset --hard origin/main -q`, issued while
still on the T9 branch. Seven modified tracked files were destroyed. `git fsck --lost-found` found
nothing, `git stash list` was empty, and VSCode local history had nothing, because git had never been
shown those bytes. The work came back only by replaying every `Edit` tool call for those paths out of
the session transcript JSONL (`applied=30 skipped=0`) and hand-repairing the two edits that had
originally been made through Bash. That is luck with a good audit log, not a recovery path.

**Root cause.** Two of them, and the second is the interesting one.

The proximate cause: the same line's earlier `git checkout main` had already **aborted** on the dirty
tree and said so, in the output I had just read. `git reset --hard` does not abort. Reading a refusal
and then issuing the verb whose entire purpose is to overwrite that refusal is the whole of the
mistake.

The real cause: [[lsn-022]] recorded this exact failure two days earlier, and its mechanization —
`dev/mutate.sh` — is scoped to **mutation tests**. The lesson generalized ("when a tool's undo is
defined against a state you did not choose, it is not an undo"); the guard did not. So a
correctly-closed lesson sat in this file while the identical loss arrived through a different verb
(`reset --hard`, not `checkout <path>`), in an ad-hoc shell line, nowhere near a mutation test. This
is [[lsn-019]] in a form its own check cannot see: `check_closed_lessons_are_executable` asks whether
a mechanization **exists and runs**, not whether it **covers the act** the lesson is about. A guard
scoped to one caller only ever protects that caller.

**Generalize.** Scope a mechanization to the **act**, not to the **caller**, whenever the act is
available everywhere. Discarding uncommitted work is available in every shell command this harness
will ever run, so the guard has to sit where every shell command passes.

**Mechanization.** **`dev/git-destructive-guard.py`**, wired as a `PreToolUse` hook on `Bash` in
**`.claude/settings.json`** — the first hook this repository has, and the reason that file now
exists. It splits the proposed command on `;`, `&&`, `||`, `|` and newlines (the incident's
`reset --hard` was the **third** segment of a line whose first segment had already failed), skips env
assignments and git's own global flags (`git -C <dir> reset --hard`), and refuses only if
`git status --porcelain` reports uncommitted work. Blocked: `reset --hard`, `checkout -- <path>`,
`checkout <ref> -- <path>`, `checkout <existing-path>`, `restore <path>`, `clean -f`, `stash drop`,
`stash clear`. Deliberately **not** blocked: `checkout <branch>`, `switch`, `merge`, `rebase`, `pull`
— each aborts on a dirty tree by itself, and that distinction is the lesson: the dangerous verbs are
exactly the ones that cannot warn you, because overwriting is what they are for. Exit 2 with the
reason on stderr blocks the call and hands the model the list of files it was about to lose plus the
`git stash push -u -m pre-destructive` that makes the command safe. A clean tree refuses nothing, so
this is free in the common case and safe to leave on permanently. Unparseable hook input exits 0: a
guard that wedges the session would be removed within the day, and then it guards nothing.

**Found by it.** The first command the guard ever blocked was the `cat >> LESSONS.md` heredoc writing
**this entry**, because the paragraphs above quote the incident command verbatim — and the refusal
reported the offending segment as `git reset --hard origin/main -q`, issued while`, prose sliced at a
period. A heredoc body is data; the shell never executes it. So `strip_heredocs()` removes bodies
(quoted, unquoted and `<<-` forms, several per line, closed in order) before splitting, keeping the
header line. This is not a weakening — it is the difference between a command and a string — and it
is load-bearing for adoption: a guard that cannot let you write down the thing it guards against is a
guard that gets switched off within the day. `TestHeredocBodiesAreData` pins it, including that a
real `git reset --hard` **after** the terminator is still caught.

**Verify.** **`dev/test_git_destructive_guard.py`**, 27 tests, picked up by
`python3 -m unittest discover dev` on the L0 chain. Each runs against a real throwaway repo with a
real dirty file, because the guard's job is to read real `git status` output. The first test is the
**literal** incident command, asserted refused on a dirty tree and allowed on a clean one; if this
file is ever refactored, that is the case that must survive, since it is the incident and not an
example of it. The rest cover every blocked verb, every allowed verb, all four separators, the
`-C`-flag form, `--soft`/`--mixed` and `clean -n` staying allowed, the five heredoc cases, and the
hook protocol end-to-end through the real process (exit 2 + `LSN-030` on stderr, exit 0 on a clean
tree, non-`Bash` tools ignored, garbage stdin non-blocking). Two tests assert the **wiring** rather
than the logic — that `.claude/settings.json` registers the script as a `PreToolUse` hook on `Bash`,
and that the script is executable — because a guard nothing invokes is a comment, which is precisely
how [[lsn-022]] came back.

---

## LSN-031 — Every rule passed its own test; three of them were switched off

`verification, security` · **closed** 2026-07-27 (P9-T3a) · found by writing a corpus, not by running one

**Trigger.** The classifier for 06 §4.2 was finished and green: seventeen code-floor rules, a
per-rule table test for each, plus direction, path-dialect, production-ladder, blast-radius and
secret-egress suites. Then the 09 §7.1 corpus got written — 165 already-resolved envelopes and the
class a human would defend for each. Three of them failed, and none of the three was a corpus
mistake.

1. **A scope escape.** `gat-151`: a developer-team agent scoped to `team-a` patching a
   **ClusterRole** classified `routine`. `ScopeOfTarget` read
   `if namespace != "" { s.Namespace = namespace }`, so a cluster-scoped target — which has no
   namespace — kept the **caller's** namespace and resolved to the caller's own scope, which
   trivially contains itself. Step 1 waved through every cluster-scoped object in the cluster for
   every namespace-scoped agent: every ClusterRole, every webhook configuration, every
   PersistentVolume.
2. **`security-loosen` could not fire on three kind families.** The rule carried a `Kinds` list
   *and* `Direction: loosen`, and the list had been written by hand from the RBAC/NetworkPolicy/
   webhook/quota kinds. `direction.go` understands three more: a Namespace's `pod-security.*`
   labels, a workload's `securityContext` and `serviceAccountName`, and a Service's `/spec/type`.
   A loosening on any of them computed a direction correctly and then matched no rule.
3. **`public-exposure` was near-dead on the path that matters.** Whole-object direction had one
   uniform rule — delete loosens, create tightens — which is right for a NetworkPolicy and exactly
   backwards for an Ingress. `public-exposure` requires `DirectionLoosen`, so **creating** an
   internet-facing Ingress came out `tighten` and gated nothing, while **deleting** one gated.

**Root cause.** Every rule was right about itself. What was wrong was the combination, in the same
shape all three times: **a decision the codebase had already made once, re-made by hand somewhere
downstream.** Which kinds are security controls is decided in `direction.go`; `floor.go` kept a
second copy, and two whitelists that must agree are an AND gate. Whether an object's *existence*
restricts or opens is a per-kind fact; the code inferred it from the verb instead, uniformly, and a
uniform answer to a two-valued question is confidently wrong on half the domain. And a target's
scope is a function of the target; the conditional made it a function of the caller whenever the
target had no namespace to offer.

Each failure resolves toward **silence** — no rule fires, the action is `routine`, nothing is
logged as suppressed. A gate that fails loud gets fixed on the first false positive. A gate that
fails quiet is indistinguishable from a gate with nothing to catch, which is what all seventeen
rules' unit tests were reporting.

**Generalize.** A per-rule test can only ever tell you a rule works *when it is reached*. Nothing in
a suite of per-rule tests asks **whether every rule is reachable**, and that is the question a
corpus answers, because a corpus is written from the outside — from what a human would defend —
rather than from the rule under test. Write the corpus before believing the unit tests. And when a
rule needs a fact the codebase already encodes, **import the encoding; never restate it** — the
restatement is not defence in depth, it is a second thing that has to be maintained and a silent
`AND` when it is not.

**Mechanization.** Four, each at the definition site:

- **`security-loosen` keys off direction alone** and carries no `Kinds` list (`floor.go`). The
  whitelist already happened once, in `ControlOfKind`; the dead `securityControlKinds` and
  `rbacKinds` were deleted rather than left beside the rule that stopped consulting them.
- **`Polarity` in `direction.go`** — a table, not an inference. `PolarityRestriction`
  (NetworkPolicy, webhook configs, ResourceQuota, LimitRange, PDB): create tightens.
  `PolarityOpening` (Ingress, Gateway, HTTPRoute, ComputeForwardingRule, ComputeFirewall, every
  RBAC kind): create loosens. The object-level twin of `boolFieldLoosensWhenTrue`, which exists for
  the same reason and was already right.
- **`ScopeOfTarget` assigns the namespace unconditionally**, with the escape written into the
  function's comment, because the conditional is the version that looks correct.
- **`TestEverySecurityControlCanReachAGate`** — for each of the eight controls of 03 §5.2, a
  loosening operation on a kind that control governs must reach a gate. A control the direction
  analysis models but no floor rule can act on is now a test failure rather than a quiet `routine`.

**Verify.** `go test ./internal/broker/classify/... -count=1`: the 165-case corpus, the reachability
test, six polarity cases in `TestWholeObjectDirection` (the old suite asserted "deleting a
ClusterRole loosens" — the **test** was wrong, and it encoded the bug), and a direct regression for
the scope escape in `classify_test.go`. On the L0 chain,
**`python3 dev/tests/classifier-corpus-lint.py`** requires that every floor rule have a case
asserting it FIRES **and** a case asserting it STAYS QUIET — the negative half is what a corpus
author skips, and it is the half that catches an over-eager gate, which is not a safe failure
either: it trains operators to approve without reading. **`python3
dev/tests/classifier-is-model-free.py`** is the same instinct applied to the package boundary.

---

## LSN-032 — A deny-list names a group nobody serves, and the corpus that checks it agrees

`security, codegen, corpus` · **closed** 2026-07-27 (P9-T3b) · found by reading the code, not by
running it

**Trigger.** None. Nothing failed. The forbidden set in `internal/broker/classify/floor.go` was
green under seventeen per-rule table tests and a 165-case corpus, and five of its nine entries were
keyed on `kubeagents.gke-labs.dev` — an API group this operator has never served. The operator
serves `kubeagents.x-k8s.io`, and has since the CRD was scaffolded. So `delete ActionRecord`,
`patch ChangePolicy`, `delete FleetFreeze`, `create ApprovalRoster` and `patch Agent` all fell
straight through 06 §4.2 step 2 and came out **`routine`** — the four control-plane objects an agent
must never touch, plus the Agent CR itself, classified as needing no approval at all. It was noticed
while reading the file for a different reason.

**Why nothing caught it.** A wrong API group is not a compile error, not a runtime error, and not
observable from either side of the comparison. The rule reads correctly. The fixture that exercises
the rule reads correctly. They agree with **each other**, which is all a test can see. The only
party that disagrees is the API server, and its way of disagreeing is to serve a group the rule
never mentions — the rule does not error, it simply never matches, and a forbidden rule that never
matches is indistinguishable from one with nothing to forbid.

**Root cause, and the part worth keeping.** The group is a fact the codebase already encodes exactly
once, in generated code (`SchemeGroupVersion` in `groupversion_info.go`) that is also what the scheme
registers and therefore what the API server actually serves. `floor.go` restated it as a string
literal. That is [[lsn-031]]'s shape — *a decision the codebase had already made once, re-made by
hand downstream* — one level worse, because LSN-031's restatements were of a **judgement** (which
kinds are security controls), which a careful reader can re-derive and check. This one is an
**identifier**, and there is nothing to re-derive: it is either byte-equal to what codegen produced
or it is dead, and a reader cannot tell which by reading.

**The second finding is about the corpus.** LSN-031 closed on "write the corpus before believing the
unit tests", and the corpus is exactly what should have caught this. It did not, because its
fixtures were **written from the rule table**, so they inherited the table's errors — twelve
occurrences of the same wrong group, copied across. A corpus is only independent evidence if it was
derived independently. Derived from the implementation, it is the implementation restated in YAML,
and it agrees with every defect the implementation has. That is a real limit on LSN-031's own
mechanization and is recorded here rather than left as a footnote on that entry.

**Mechanization.** Three, all at the definition site:

- **`KubeAgentsGroup = agentv1alpha1.GroupVersion.Group`** in `floor.go` — the literals are gone;
  `classify` now imports the API package and reads the group from the same variable the scheme
  registers. There is no spelling left to get wrong.
- **`TestForbiddenSetNamesTheLiveAPIGroup`** (`classify_test.go`) builds an operation for each of
  the five entries from `agentv1alpha1.GroupVersion.Group` — the value the API server uses, not a
  string in the test — and asserts `forbidden`. If the constant and the served group ever diverge,
  the test cannot pass, because both sides move together only when they are the same thing.
- **`dev/tests/api-group-single-sourced.py`** on the L0 chain — no code line in any `.go`, `.yaml`,
  `.sh` or `.json` file may name a `kubeagents.*` group other than the one parsed out of
  `groupversion_info.go`. Comments are stripped first ([[lsn-023]]: the paragraph describing this
  defect must not satisfy or fail the check that prevents it, and three files now discuss the old
  string by name). The OpenTelemetry `kubeagents.*` attribute keys are a **closed allowlist** rather
  than a pattern, so a typo in one of those — same silent failure, empty dashboard panel instead of
  an ungated delete — is a line in this file and not a diff nobody read.

**Verify.** `python3 dev/tests/api-group-single-sourced.py` → PASS, 96 files naming the served
group and none naming another. Mutation-tested: restoring `kubeagents.gke-labs.dev` at the
definition site makes it exit 1 and name `floor.go:241`. `go test ./internal/broker/classify/...
-count=1` covers the five forbidden entries against the live group.

**Generalize.** When a check compares two things you wrote, it is testing that you were consistent,
not that you were right. Ask what the **third party** is — the API server, the kernel, the remote —
and find the one artifact that party actually reads. If your code cannot read that same artifact,
the agreement between your rule and your fixture is worth nothing.

---

## LSN-033 — A safety list is complete for the domain its author had in mind, and empty for the one where the damage is

**Tags:** security, scope, corpus · **Phase:** 9 (P9-T4) · **Status:** closed

**What happened.** P9-T4 built the undo-plan generator, which needs a list of kinds whose deletion
it cannot reverse. `classify` already had a list of kinds whose deletion destroys data. Rather than
share one list — the two ask different questions, and a Secret is stateful and *is* recreatable — I
wrote both and mechanized the one-directional invariant that must hold between them: **anything the
undo package cannot restore must be gated by the classifier**. The test builds a real classifier and
runs a `delete` of each non-recreatable kind through it.

Thirteen of seventeen came back `routine`, reason `default-routine: no rule matched`.

The split was perfectly clean. All four that passed were Kubernetes-native — PVC, PV, VolumeSnapshot,
Namespace. All thirteen that failed were Config Connector. `delete SQLInstance`, `delete
StorageBucket`, `delete BigQueryDataset`, `delete ComputeDisk`, `delete ComputeSnapshot` and
`delete ContainerCluster` were all executable by an agent with no human ever seeing them, while
`delete ConfigMap` — six lines away in the same file — required approval. A separate instance of the
same shape sat beside it: `IAMServiceAccount` was on the identity list and `IAMServiceAccountKey`,
the credential itself, was not, so deleting an account gated and revoking its key did not.

**Why nothing caught it.** `statefulKinds` was not wrong. Every kind on it belongs on it, the rule
that reads it works, and its six corpus cases were green from the day they were written. The list
was **complete for the domain its author had in mind** — objects that live in etcd — and the domain
where the data actually lives was not absent from the list so much as absent from the question. A
missing entry looks exactly like a kind nobody has needed yet.

This is [[lsn-032]]'s corpus finding arriving from the other direction, and it is worth being precise
about the difference. LSN-032: the corpus was derived from the rule table, so it inherited the
table's *errors*. Here it inherited the table's *silence*, which is harder, because there is nothing
to compare. Section C of the classifier corpus asserts six kinds gate and three do not, and it is a
faithful, well-written test of a list with a hole in it. No amount of care applied to that section
finds a kind that is not in it. **A corpus derived from a list can only ever check the list's
interior.** What found this was a *second list, written from a different question* — and then only
because the two were forced to agree.

The near-miss is worth recording too. Sharing one list between the two packages was the obvious
move, and it was the wrong one twice over: it would have made one of the two questions wrong, and it
would have propagated the hole silently into the new consumer instead of exposing it. [[lsn-031]]
says two whitelists that must agree are an `AND` gate; the refinement here is that **two lists
answering different questions must not be merged — they must be reconciled by a test**, and the
reconciliation is where the evidence comes from.

**Mechanization.** Three, and the first is the one that matters:

- **`TestNonRecreatableKindsAreGatedByTheClassifier`** (`internal/broker/undo/invariant_test.go`) —
  runs every kind in `nonRecreatableKinds` through the **real classifier** with the weakest
  configuration the product ships (no policies, novel-action suppressed, `UndoPlanPresent: true` so
  no case can pass via step 6's backstop) and fails on `routine` or `elevated`. It compares two lists
  by *executing* one against the other rather than by diffing them, so it stays correct while their
  memberships legitimately diverge. It has a vacuity guard at ten kinds ([[lsn-013]]).
- **`statefulKinds` extended by twelve CNRM kinds** and `identityKinds` by one, at the definition
  site in `floor.go`, with the shape of the gap written into the comment beside the divider — the
  next person to add a kind sees why the list has two halves.
- **`dev/tests/undo-corpus-lint.py`** on the L0 chain, plus **section M of the classifier corpus**
  (16 new cases, three of them negative). The lint's negative-set check is keyed on *kinds* rather
  than fixture ids, so adding a storage kind to `strategy.go` without a fixture fails the lint. All
  ten of its checks were mutation-tested against a deliberately broken corpus and all ten fired.

**Verify.** `go test ./internal/broker/undo/ -count=1` → ok (the invariant fails on all 13 kinds if
the `floor.go` change is reverted). `python3 dev/tests/undo-corpus-lint.py` → PASS, 31 cases, 6 verbs
planned. `python3 dev/tests/classifier-corpus-lint.py` → PASS, 181 cases. L0 chain 18/18.

**Generalize.** Before trusting a safety list, name the domain its author was thinking in, then name
one it excludes. Not "is anything missing" — that question has no handle — but "what *kind* of thing
is uniformly absent". A list that is 100% Kubernetes-native in a product that manages cloud resources
is not a list with gaps; it is a list that answered a smaller question than the one it is being asked.
The tell is uniformity: when everything on a list shares a property the list is not about, the
property is a boundary somebody drew without noticing.

**Postscript.** 09 §5 already has a check for this — V-GAT-014, the gated-class conformance matrix,
scheduled for phase 11. It would have found the hole two phases from now, in a check whose whole
purpose is finding it. It was found here instead, by a test written for an unrelated reason, because
that test was the first thing to ask a question from outside the list. Scheduling a check for later
is not the same as the property holding in the meantime.

---

## LSN-034 — A value compared against itself will never tell you it is the wrong shape

**Tags:** security, gating, api-design · **Phase:** 9 (P9-T5a) · **Status:** closed

**What happened.** P9-T5a built the executor's diff — the function that turns a before/after pair of
objects into a list of `AppliedDiffOp{op, path, from, value}`. When a map key existed on one side and
not the other, it emitted a single whole-value op: the annotations block appearing for the first time
produced one `add` at `/metadata/annotations` whose `value` was the entire block, rather than one
`add` per annotation inside it.

Against the check the function was written for — V-BRK-020, the classify/execute integrity check,
which compares the diff the classifier saw with the diff the server's dry run would produce — this is
**perfectly correct**. Both sides are computed by the same function, so both are coarse in the same
places, and every comparison comes out right. It is invisible from there, and it will stay invisible
however hard that check is exercised.

The diff has a second consumer. It is also what `when.fieldPaths` is matched against, and
`classify.PointerPrefixMatch` requires the **rule** to be the prefix of the **diff path**. A rule
naming `metadata.annotations['kube-agents/restarted-at']` is *longer* than `/metadata/annotations`,
so against a coarse op it does not match. That turns "set the whole annotations block in one apply,
having first ensured there was no annotations block" into a way to write a gated annotation without
tripping the gate. The change happens; the gate says `routine`; nothing errors.

**Why nothing caught it.** Every test that could see the problem was aimed elsewhere. It surfaced in
`TestDiffEscapesPointerTokens`, which exists to pin the `~1` escaping of a slash in an annotation
key, and in `TestDiffTruncatesLoudly`, which reported `Truncated=false` for 250 added labels because
250 labels under a new `labels` map were **one op**. Neither test was about granularity. Both failed
for the same reason and neither said so.

The generalizable part is the asymmetry between the two consumers. The integrity check reads the diff
**relative to another diff**; the gate matcher reads it **against an externally-authored string**. A
self-relative consumer cannot detect a systematic error, because a systematic error cancels. So the
correctness bar for the value is set entirely by the *other* consumer — the one comparing it to
something outside the process — and if that consumer is the newer or the quieter of the two, the bar
never gets checked. [[lsn-032]] is the same geometry one level out: a corpus derived from the rule
table compares the implementation to itself and agrees with all of its defects.

Related but distinct from [[lsn-031]]: there, two lists that had to agree formed a silent `AND`. Here
there is one value and two readers, and the readers disagree about what precision means.

**Mechanization.**

- **`assertLeafOps` + `TestDiffEmitsOnlyLeafOps`** (`k8s-operator/internal/broker/execute/diff_test.go`) — asserts
  the property directly on the op, with no reference to any other diff: if `value` or `from` parses
  as a **non-empty JSON object**, the op names a subtree, and every field inside it has a path longer
  than the op's, so no rule naming one of them can prefix-match. Checked on both the add arm and the
  remove arm, which are separate code paths and were separately wrong. The error message states the
  gating consequence rather than the shape mismatch, so the next person to trip it does not have to
  re-derive why it matters. Mutation-tested by reverting `subtreeOps` to a no-op: it fires on both
  arms, and the two tests that originally caught the bug fire too.
- **`TestDiffKeepsAnEmptyMapVisible`** — the paired negative. Recursion alone would make
  `annotations: {}` vanish from the diff entirely, which is a change the executed record would not
  carry; the fallback to the whole-value op is deliberate and now pinned. This test stays green under
  the mutation above, which is what makes the pair a specification rather than a ratchet.
- **`subtreeOps`'s doc comment** carries the reason at the definition site — that the coarse version
  is consistent, and consistent is not the bar.

**Verify.** `go test ./internal/broker/execute/ -count=1` → ok, 82.6% coverage. Mutation: with
`subtreeOps` stubbed to return nothing, `TestDiffEmitsOnlyLeafOps`, `TestDiffEscapesPointerTokens` and
`TestDiffTruncatesLoudly` all fail and `TestDiffKeepsAnEmptyMapVisible` passes. L0 chain 18/18.

The 'Closed by' row names `k8s-operator-test.yml` as well as the test file, and that is not padding.
`invariants-gate.py`'s `_invoked_by` recognizes three ways an artifact can be automatic — a literal
line of a declared chain, a PR-triggered workflow of the same basename, and `dev/test_*.py` under
`unittest discover` — and **none of them is a Go test**. A Go mechanization is therefore invisible to
the gate unless the row also names the workflow that runs it. This lesson was the first to notice
because it is the first closed with a Go test and nothing else: [[lsn-032]] and [[lsn-033]] both name
Go invariant tests, and both satisfied the gate on the strength of the Python lint listed beside
them, with the Go half contributing nothing to the verdict. That is a gap in the gate, not in this
lesson, and fixing a check in the unit whose failure surfaced it is what PROTOCOL §10 forbids — it is
recorded as a finding in the ledger and belongs to the next `harness-improve` pass.

**Generalize.** When one value has two consumers, find the one that compares it to something *outside
the process* and set the precision bar there — the self-relative consumer is not a weaker check of the
same property, it is a check of a different property that happens to pass. The practical form: for any
derived value, ask "who reads this against a string a human wrote?" If the answer is anyone, that
reader owns the value's shape, even if it is not the reason the value exists.

**Postscript.** The scope boundary this leaves is worth stating rather than discovering later. The
diff is computed over `undo.Sanitize`d objects, so its blind spots are exactly that function's DROP
list — the integrity check cannot see a server-side change to `metadata.managedFields`,
`spec.clusterIP` or a `nodePort`. That is defensible today (all server-owned, none classifiable) and
it is a coupling nobody would find by reading either file alone: **the function that keeps secrets
out of the record also decides what the integrity check is able to notice.** It cost a test in this
unit, which asserted `/metadata/uid` would appear in an executed diff — `uid` is dropped, so the
assertion could not have held in either direction and proved nothing about the property it named.

---

## LSN-035 — A redundant guard and an unenforced guard look identical from a green suite

**Tags:** checks, mutation, negative-controls · **Phase:** 9 (P9-T5b) · **Status:** closed —
`dev/tests/negative-controls-name-their-rule.py`, in `dev/L0-CHAIN.txt`

**What happened.** V-PRO-021 is a `¬` check, so P9-T5b mutation-tested the recovery ladder: neuter
each of `ladder.go`'s eleven invariants in turn, confirm the suite goes red, restore. Nine died. The
two survivors were **the two properties the check text names by hand** — "allows at most one rung-2
alternative" and "never restarts at rung 1 after a rollback".

The first read of that is "two missing tests". Both rules had tests, and the tests passed, and the
tests were worthless. `TestLadderAllowsExactlyOneAlternative` built a history that entered rung 2
twice — and to get back to rung 2 a second time it had to come *down* from rung 3, so the
**non-decreasing** rule rejected it first and the one-alternative rule never executed. Same for the
rung-1 test. Every negative control written to exercise a rule was rejected by a *different* rule,
and a test that asserts "this is rejected" cannot tell you which rule did the rejecting.

The deeper finding is that this was not fixable by writing better histories. **Within a chain-valid
history those two rules are logically unreachable.** Monotonicity plus the no-self-transition rule
already make each rung enterable at most once, which *implies* both named properties. There is no
input that reaches either `if`. They are dead code that reads as a safety rail.

**Why it matters.** The two facts a green suite cannot distinguish:

1. a rule that is **redundant** — some other rule already covers every input, and deleting it
   changes nothing observable; and
2. a rule that is **unenforced** — the property it names is not actually held, and the tests that
   appear to cover it pass for an unrelated reason.

Mutation testing reports these **identically**: a survivor. So a survivor is not a verdict, it is a
question — *is this rule reachable at all?* — and the two answers need opposite responses. For (1)
the property is safe and the rule is documentation; for (2) the property is unheld and the check is
lying. Answering "add a test for the surviving rule" without asking the question produces, in case
(1), another test that passes for the wrong reason, which is where this started.

**The general shape.** `¬` in 09 §6 means "a negative control is mandatory", and the whole point is
to establish that the check *can* fail. But a negative control only proves the **suite** fails; it
never proves **which rule** made it fail. When several rules overlap, every negative control lands
on whichever fires first — typically the broadest one — and the narrow rules beneath it accumulate
tests that have never once executed them. The overlap is not a smell to remove: monotonicity and
"at most one alternative" *should* both be stated, because they come from different sentences of
04 §5. What is missing is any signal that one has stopped carrying its own weight.

**What was done here, and what is still open.** The ladder was repaired by moving the assertions off
the rules and onto the **properties**: `TestLadderPropertiesHoldOverEveryAcceptedHistory` enumerates
all 1554 histories of length ≤ 4 over the six rungs, validates each, and asserts that nothing
accepted violates either property — pinning the accepted count at exactly **30**
(C(5,1)+C(5,2)+C(5,3)+C(5,4), the strictly-ascending sequences over rungs 1..5), which is two-sided:
loosening the ladder makes it larger, over-tightening makes it smaller. Deleting monotonicity or the
no-movement rule now fails *that* test by name, confirmed by re-mutating. Both derived rules were
**kept**, with the finding written into `ladder.go` at the definition site, because the implication
runs one way: if a future revision relaxes monotonicity they stop being derived and become the only
thing enforcing the bound, and a rule deleted for being redundant is a rule nobody restores when the
premise changes.

That fixed one ladder. Exhaustive enumeration worked there only because the state space is tiny (six
rungs, four steps); it is not a general technique.

**Closed by `dev/tests/negative-controls-name-their-rule.py`.** The first of the two candidate
mechanizations, taken: every `¬` check must assert *which* rule its control exercised.

The check is **behavioural**, not structural. The obvious version greps for a three-element mutation
tuple, which is a check on today's code shape, is defeated by a control written in another style,
and would have scored `install-render-is-faithful.py` — the one file in the tree that already did
this properly — as a violation, because its breakages are numbered rather than signalled. Instead:
load each check advertising `--negative-control`, blind the probe its control actually calls, and
re-run. A control that asserts a per-mutation signal now fails; one that only asks "is the failure
list non-empty" still passes, and **that pass is the finding**. The instrument is the defect itself,
injected on purpose.

Three details are load-bearing, and each was a wrong first draft:

- **The blinding preserves emptiness.** A non-empty result becomes one constant; an empty result
  stays empty. Substituting unconditionally also changes *whether* the probe fired, so any control
  with a false-positive arm ("this correct spelling must NOT be flagged") went red on that arm and
  scored as discriminating without distinguishing anything. Preserving emptiness leaves the boolean
  signal intact and destroys only the identity — which is the property under test, stated exactly.
- **The probe is read off the control's own bytecode**, not off the module.
  `go-build-targets-packages.py` defines both `check` and `scan_text`; `check` walks the repository
  and its control never calls it, so blinding by module order was a silent no-op.
- **The sentinel preserves shape.** `scan_text` returns `(line, invocation, operand)` triples;
  handing that control a bare string is a crash, and a crash scores as *unscoreable* per
  [[LSN-038]], never as a pass. Strings become the sentinel, numbers become zero, in place.

Converting the corpus found what the lesson predicts. `cluster-check-hygiene.py`'s comment-stripping
control turned out to exercise property **2** (the DELETE-protected kinds), not property 1 (the
`auth can-i` slot) as its label claimed — the prose spelling of the can-i trap is in backticks, a
backtick means command substitution, and the tokenizer correctly declines to resolve it. Under a
non-emptiness assertion that control read as covering both. That is this lesson in one line, found
by its own mechanization on the day it landed.

Ten controls now discriminate. The check itself carries a `¬` scoring six controls of known
quality, including one whose module defines a decoy probe and one whose findings are tuples.

**Related.** [[lsn-032]] is this one's sibling and the reason it was recognized: there, a corpus
derived from the rule table agreed with the table's defects for three phases. Here, a negative
control derived from the rule set is intercepted by the rule set. Both are the same geometry — **a
check that shares a premise with the thing it checks cannot see a defect in the premise** — and
[[lsn-033]] is the version where it arrives as silence rather than as error.

---

## LSN-036 — A single-site check is a headcount, and headcounts go stale when the population grows

**Tags:** checks, renderers, allowlists, controller · **Phase:** 9 (P9-T7b) · **Status:** closed

**What happened.** V-RUN-012's L0 check exists to stop `pause` being implemented as a scale-to-zero.
Its strongest property was a headcount: `Replicas:` is assigned in **exactly one place**, and the
right-hand side is `&replicas`, from a decider that is structurally incapable of seeing the brake.
That was true and load-bearing for four phases, because `internal/controller` rendered exactly one
workload.

P9-T7b added the second half of the 08 §2.4 pair. The broker Deployment sets `Replicas:
ptr.To(int32(1))` — a constant, correct, and completely unrelated to the brake — and the check went
red on a correct render, with the message "a second replica decision".

**Why the obvious fix is the wrong one.** The one-line green is to add the literal to
`ALLOWED_REPLICAS_RHS`. It is also the moment the check stops meaning anything: a flat allowlist
says "these spellings are fine anywhere", so `Replicas: &replicas` would be legal in the broker
renderer and the broker's spelling legal in the agent's, and the *next* workload adds a third entry
by the same reasoning. A headcount widened once is a headcount that will be widened again.

**What the check was actually right about.** Working out whether the broker's site was dangerous is
where the value was. It is — and more so than the agent's. "Paused" means the agent must not write;
the broker is the only half of the pair that can; so *delete the broker* reads in a design review as
closing the write path at its source, and it is a smaller diff than touching the agent. What it
deletes is the explanation. 06 §4.4 requires a paused agent to keep saying why it is refusing, and
the refusal comes from the broker. Implemented this way, a pause reports itself to every operator as
a **broker outage**, and `wait-for-broker` drops the pods into observe-and-report — the same words
the design uses for an unrelated and much worse situation.

**The mechanization.** Three changes, all tightening:

1. The allowlist is **keyed by file**. Each workload's replica count may only be decided in the file
   that owns that workload. Strictly narrower than the flat set — before this, one spelling was
   legal in all eight controller sources.
2. The broker's operand must be a **`const`**. There is no decider to constrain here, so property
   1's argument ("the function cannot consult `paused` because it is not given it") is made by other
   means: a constant cannot be derived from an Agent.
3. A **stale-entry arm**: an owner that declares an allowed spelling but assigns no `Replicas:` at
   all is a failure, not a pass. Otherwise a keyed allowlist rots in the [[lsn-035]] direction —
   guarding nothing while looking like it guards something.

The negative control went from 5 mutations to 8, and the L1 property test gained
`TestPauseDoesNotChangeTheRenderedBroker`, which also pins that `spec.deployment.scaleToZero` idles
the reader **without** touching the broker.

**The general shape, and the thing to look for.** A check of the form "there is exactly one X"
encodes a fact about the *shape of the codebase today*, not about the property. It is the strongest
kind of check while the count holds and the most brittle the moment the count changes — and the
change that breaks it is usually a legitimate feature, arriving under deadline, whose author will
reach for the allowlist. When a uniqueness check fires on correct code, the question is not "how do
I let this through" but **"what makes the new site safe, and can the check assert that instead?"**
Here the answer was `const`. Grep for the geometry: `dev/tests/` currently holds several
single-definition-site checks ([[lsn-020]]'s API group, P9-T7a's label keys, this one). Each is one
new legitimate call site away from the same conversation, and each should say what makes a site
*eligible* rather than how many sites there may be.

**Related.** [[lsn-035]] is the failure mode arm 3 defends against. This is also the second time a
check written by an earlier unit of phase 9 caught the unit after it — P9-T6c's `undo_controller.go`
was the first, but that one only needed classifying; this one needed the implementation changed.

---

## LSN-037 — The build that ships is not the build you tested

**Tags:** builds, dockerfiles, ci, toolchain · **Phase:** 9 (P9-T7b) · **Status:** closed

P9-T7b added `waitforbroker.go` next to `cmd/broker/main.go`. Locally, `go build ./...`,
`go vet ./...` and `go test ./...` were clean. On the PR, six of seven checks were green and `build`
was red, eleven minutes in:

```
cmd/broker/main.go:128:8: undefined: waitOptions
cmd/broker/main.go:150:13: undefined: runWaitForBroker
```

`Dockerfile.broker` compiled `cmd/broker/main.go` — the file — instead of `./cmd/broker` — the
package. Passing a file list to `go build` compiles exactly those files and treats the rest of the
package as absent, so the new file's symbols were undefined in the only build that mattered.

**Why it survived so long.** The file-list form is the kubebuilder scaffold's default, and it is
*correct in every observable way* while a `main` package holds one file. It had been right for the
operator and the router for eight phases. Nothing was wrong with it until the population of the
directory changed, at which point it became wrong instantly and in a place nobody was looking. Both
spellings produce a working binary from an identical tree; they differ only in a future that had not
happened yet.

**Why the local signal did not help.** Every local command names packages (`./...`). Only the
container images named files. So the repository had two build systems that agreed on every input
except the one that had just changed, and the disagreeing one ran last, slowest, and furthest from
the edit — where a compile error reads as a broken base image or a bad cache rather than as one
token in a `RUN` line. The cost is not the fix; it is the eleven minutes plus the wrong first
hypothesis.

**Mechanization.** `dev/tests/go-build-targets-packages.py`, on the L0 chain with its negative
control. It sweeps every build input — Dockerfiles, Makefiles, shell scripts, workflow YAML — and
fails any `go build` whose target is a `.go` file. Three deliberate properties:

1. **Discovery, not enumeration.** Build inputs are globbed by filename pattern, so the next
   Dockerfile is covered the day it lands. This is [[lsn-036]] applied on the way in rather than
   learned again: the same unit that taught "do not write a check that lists what it knows about"
   is the unit that would have written one here.
2. **Non-vacuity.** Finding zero `go build` invocations is a FAIL, not a pass ([[lsn-035]]) — if the
   builds move somewhere the glob does not look, the check must say so rather than go quiet.
3. **Accepts the fix, and the prose.** Six non-mutations pin that `./cmd/broker`, `./...`, `.`, a
   trailing slash, `-o weird.go`, and a *comment describing the rule* all pass. A check that also
   rejects the correct spelling teaches people to route around it; a check that fires on the
   sentence explaining it is [[lsn-023]].

Fixed in all three kubebuilder Dockerfiles and both Makefile recipes, not only the broker: the other
four were latent, one second file away from an identical eleven-minute red.

**The general shape.** When a defect is invisible in every fast check and visible only in the slow
one, the two are not testing the same artifact. Ask what the slow check does differently — here,
"names files instead of packages" — and make the fast check assert that difference away. Related:
[[lsn-036]] for the discovery-not-enumeration arm, [[lsn-035]] for non-vacuity.

---

## LSN-038 — A guard that fails safe still fails, and a green run is how it tells you

**Tags:** checks, probes, discovery, negative-controls · **Phase:** 9 (P9-T7d-3) · **Status:** closed

Invariant 7 — _authority never precedes machinery_ — forbids a write verb on an agent identity until
the Action Broker, the risk classifier, the ActionRecord journal and the undo path all exist and are
tested. `invariants-gate.py` implemented it by probing for each of the four at a hardcoded pair of
candidate directories. Two of the four pairs were wrong: the classifier landed at
`internal/broker/classify/` and undo at `internal/broker/undo/`, neither of which the list guessed.

So from P9-T3a onward the gate believed half the machinery did not exist. It printed
`✓ invariant 7 — authority never precedes machinery` on every run of six merged units, and the tick
was true — no identity had a write verb yet, so the rule had nothing to fire on. The false belief
was doing nothing, correctly.

P9-T7d-3 adds the 06 §2.2.1 broker-operations grant: the first `create` verb on an agent identity in
the project's history. That unit turns the gate red on correct code, and the one-line diff to green
is to edit the candidate list — which is [[lsn-036]] exactly, the lesson about a check whose fix is
an allowlist entry. The unit that pays is never the unit that erred.

**Why "fails safe" was the whole problem.** A probe that wrongly reports machinery *present* fails
open, and everyone treats that as the serious direction. This one failed closed. But the direction
of a wrong answer only matters at the moment the answer is used, and this answer went unused for six
units — during which the only observable was a green tick. There is no run, no log line and no diff
that distinguishes "this property holds" from "this check has not been able to see its subject since
March". [[lsn-035]] is the same shape one level down: a rule no input reaches.

Two more instances of it surfaced in the same file once the pattern had a name:

- `"CLOSED" in blocker.upper()` decided whether a deferral row still owed a promote-when condition.
  A blocker reading _"blocked until the maintenance window closed"_ satisfies it, and the row stops
  being asked the one question that makes it a deferral rather than a shrug.
- `_invoked_by` knew that `python3 -m unittest discover dev` runs every `dev/test_*.py` without
  naming one, but not that `make -C k8s-operator test` runs every Go test without naming one. A
  lesson mechanized as a Go test therefore read as "run by nothing" and could not close on its own
  merits — so LSN-032, LSN-033 and LSN-034 each also cite whichever `.py` was nearby. The citations
  are true; they just do not describe the mechanization, and the pressure that produced them was a
  bug in the checker.

**Mechanization.** Four parts, all on the L0 chain.

1. **The probe's answer is itself a claim, checked every run.** `check_machinery_probes_resolve()`
   runs whether or not any write verb exists. Machinery that does not resolve must be *declared* in
   `UNBUILT_UNTIL_PHASE` with the phase by which it is expected, cross-checked against the ledger's
   current phase — the declare-or-fail idiom `pause-is-not-scale-to-zero.py` already uses. Silence
   is no longer an available answer, in either direction: a stale declaration on machinery that
   _does_ exist fails too.
2. **Discovery by glob, not by list** ([[lsn-036]]), and qualification by content: a match counts
   only if it holds a non-test `func` and a `func Test` beside it, which is what invariant 7's
   "exist **and are tested**" actually says. The classifier globs are package directories rather
   than a `classif*` stem, because that stem also matches `internal/router/classify.go` — the
   chat-intent classifier — and a probe resolving against the wrong subsystem fails *open*.
3. **`dev/test_invariants_gate.py`** — the first negative controls the gate has ever had. Nineteen
   tests, each breaking a property on purpose: the exact LSN-038 state fails, a future-phase
   declaration is accepted, an expired one fails, Go source with no test beside it is not machinery,
   a blocker that merely uses the word "closed" is still asked for its promote-when, and every
   branch of the new Go arm of `_invoked_by` including the `/e2e` exclusion the Makefile applies.
   Run by `python3 -m unittest discover dev`, already on the chain.
4. **One Go comment scanner, in `dev/tests/golex.py`.** Auditing for the same shape turned up two
   more copies of the line-oriented `line.split("//", 1)[0]` blanking, each with a docstring arguing
   it was safe there. In `scope-label-single-sourced.py` and `api-group-single-sourced.py` it is
   not: a key or an API group on the same line as a `https://` URL is truncated away and the check
   reports nothing. Both now import the literal-aware scanner, and `scope-label`'s negative control
   gained a sixth mutation — a respelled key hidden behind a URL — that only the shared scanner
   catches, so the port is load-bearing rather than tidy.

**The general shape.** When check A consults check B's answer, B's answer is an input like any other
and must be validated like any other. Ask of every guard: _if its subject vanished, what would the
run look like?_ If the answer is "the same", the guard is reporting on itself. The tell is a
predicate that can only ever make the caller stricter — nobody writes a negative control for those,
because the failure mode is not scary until the day it is the only thing standing between a correct
change and a red build. Related: [[lsn-035]] (unreachable rules), [[lsn-036]] (enumeration goes
stale), [[lsn-023]] (the prose satisfying the check).

---

## LSN-039 — Reachability was checked one link short

**Tag:** wiring, completeness, manifests, install-path · **Status: closed** (mechanized by
**V-CMP-007**, `dev/tests/identity-has-install-path.py`, in P9-T7d-5).

**Trigger.** A user asked why `examples/gitops-repo/policy/rbac-overlay/developer-team.yaml` says
`app.kubernetes.io/managed-by: gitops` when the design no longer has a GitOps control path. Chasing
the label found the label was the smaller half. The tree under `examples/gitops-repo/` that six
phases of checks have been reading, asserting against, and recording green is applied by nothing.

**The finding as first written was too broad, and the correction is part of the lesson.** The first
statement of it — quoted here because it stood in this file, in the ledger, in `phase-9.md`, in a
commit message and in PR #47's body — was _"no `provision_NN_*.sh` step creates any agent
ServiceAccount, Role or RoleBinding."_ That came from a grep over `*.sh`, and the identities are
created from `.yaml.template` files piped through `envsubst | kubectl apply -f -`, which that grep
could not see. The accurate finding, confirmed by a read-only sweep of the live
`platform-agent-host` before anything was changed, is three narrower claims:

1. The **cluster-admin and developer-team reader** SAs _were_ created imperatively, inline in
   `cluster-admin-agent.yaml.template` and `developer-team-agent.yaml.template`.
2. The **platform reader** SA was created by nothing. `kubeagents-platform-agent` on the live cluster
   is a bare hand-applied SA: its `last-applied-configuration` has no labels and no annotations, and
   the `iam.gke.io/gcp-service-account` annotation present on the object is absent from it — added by
   a separate hand `kubectl annotate`. (Note also that the live name is `kubeagents-platform-agent`
   while the exemplar says `platform-agent`; two names for one identity is its own tell.)
3. **No actor SA and no broker-operations grant existed on any install path at all**, so the broker
   Deployment T7d-3 renders would reference an identity nothing creates and the pod would not start.
   No install-path identity carried `kube-agents/role`, which is the label both VAP arms select on.

The over-broad version and the accurate version motivate the same unit and the same mechanization,
which is exactly why it survived four artifacts before being caught. **A finding stated one
generalization wider than the evidence is not a harmless rounding: it is a claim the next reader
cannot reproduce**, and the reader who tries and fails learns to discount the finding rather than to
narrow it. The evidence sweep that narrowed it is also what proved claim 2, which is the worst of the
three and which the broad version had flattened into the others.

**Why the existing mechanization did not catch it.** This is LSN-007 — "built, tested, and
unreachable" — and LSN-007 **is closed**, by `dev/tests/install-path-wired.py`. That check is good
and it is one link short. Its five properties are all about the *script* graph: every
`provision_NN_*.sh` is invoked by `provision.sh`, every driver line names a script that exists,
provision and teardown are symmetric and ordered. Every one of them passes on a repository in which
the steps run perfectly and apply none of the security manifests, because nothing in it walks the
other edge — **from a manifest to the step that applies it.** Its own docstring says the quiet part:
_"Deliberately NOT checked: that a step does the right thing. This is reachability only."_ It is
reachability of scripts. The manifests were never in the graph.

`k8s-operator/scripts/common.sh:656` had already written the finding down, for a different pair of
files, in 2026: _"Both manifests existed in examples/gitops-repo/ from Phase 3 and NO INSTALL PATH
APPLIED EITHER — the same defect class as the egress policies (LSN-006/LSN-007)."_ It fixed those two
by rendering them from `common.sh`, recorded the reasoning, and did not generalize. So the class has
now been paid for **three times**: the egress policies, the tenant quota and default-deny, and every
agent identity in the system.

**The shape.** A closed lesson is closed against the instance that produced it. Its mechanization
encodes the graph the author was looking at, and the next instance arrives one edge over — same
class, different node type. The tell is a check whose docstring narrows its own claim ("this is
reachability only", "this is the source half", "the L2 half is elsewhere"): every one of those
sentences is honest, and collectively they are a map of where the class is still free to recur.
A closed lesson should be re-read as _"what is the smallest change of node type that would walk past
this?"_ — and if the answer is easy, the lesson is closed against an instance, not against a class.

**Aggravating factor specific to this instance.** The exemplar tree is not obviously inert. It is
version-controlled, prettier-formatted, referenced by name from the specs (06 §2 line 455), read by
five `verify-phase*.sh` suites, applied selectively by `dev/cluster/up.sh` and by
`provision_03`, and now checked by V-BRK-013. Every one of those makes it look load-bearing. Partial
application is worse than none: it means "is this tree applied?" has the answer *yes, some of it*,
and no reader can tell which files are in the some.

**Mechanization (P9-T7d-5, `V-CMP-007` = `dev/tests/identity-has-install-path.py`, on L0-CHAIN).**
The plan above was to add a sixth property to `install-path-wired.py` over the exemplar tree. What
shipped instead is a separate check over the **install path** (`k8s-operator/scripts/`), and the
difference is deliberate: the property that makes an identity real is not "is this exemplar inert?"
but "does every identity the broker references get created by a step someone runs?" — the exemplar
tree could be deleted entirely and the pod would still not start. Seven properties, of which the
first two are the class:

1. Every `*.yaml.template` under the scripts tree is read by a **reachable** step.
2. Every manifest-emitting shell function — discovered by **body** (it names a template, or it runs
   `kubectl apply|delete`), never by a list ([[lsn-036]]) — is transitively reachable from a numbered
   step.
3. Every ServiceAccount created carries both `kube-agents/tier` and `kube-agents/role`.
4. Every RBAC ServiceAccount subject resolves to an SA some manifest creates.
5. Every `roleRef` resolves, or is in `BUILTIN_ROLES` — currently **empty**, with a comment saying an
   entry needs a sentence.
6. The Go and bash actor-name format strings agree, plus five non-vacuity floors ([[lsn-035]]).
7. Every tier with an `agents/<tier>/config.yaml` has an `apply_agent_identity <tier> …` call site —
   discovered from `agents/`, so a fourth tier is covered the day it appears.

Eight negative controls, all firing. The one that matters is **"one tier's identity stops being
applied while the others keep working"** — LSN-039 itself, reproduced, and the shape a per-tier check
written as a global count would miss.

**Property 4 forced a real design decision, and it is worth recording as a pattern.** The check first
failed on two bindings whose subjects looked dangling: the identity template creates
`${AGENT_READER_KSA}` while the tier templates beside it bind `${CLUSTER_ADMIN_KSA_NAME}` — the same
ServiceAccount under a different variable name, joined only at the `apply_agent_identity` call site.
Comparing the spellings reports a false dangling subject; ignoring the difference accepts any
spelling at all. The fix was to **parse the call sites** and derive the alias from the actual wiring,
which is both stricter than either alternative and what unlocked property 7. When a check finds two
names for one object, the answer is usually to read the thing that joins them, not to relax the
comparison.

**Not closed by this, and stated so it does not evaporate:** the declare-or-fail table over
`examples/gitops-repo/` — part (d) of the original plan, so that "this file is documentation" is a
claim someone made rather than a state something drifted into. V-CMP-007 says nothing about which
files in the exemplar tree are inert. That property is carried by the queued `managed-by: gitops`
sweep, which is the unit that touches that tree.

Related: [[lsn-007]] (the closed lesson this walked around), [[lsn-006]] (well-formed is not
enforced — the same gap between an artifact and its effect), [[lsn-036]] (enumeration goes stale),
[[lsn-035]] (a check whose subject was refactored away prints PASS forever).

---

## LSN-040 — Two packages, each right, about the same word

**Tag:** seams, integration, assembly, broker · **Phase:** 9 (P9-T7c-1 found it, P9-T7c-4a and
P9-T7c-4b fixed it) · **Status:** closed — mechanized by
`k8s-operator/internal/broker/pipeline/verbs_test.go` (**V-BRK-022**, L1), run by
`.github/workflows/k8s-operator-test.yml`'s `Run Controller Tests` on every PR. Non-vacuity:
`verification/mutants/V-BRK-022.json`, 15/15 caught.

**Trigger.** Assembling steps 3–11 into a working pipeline (P9-T7c-1) and driving one real envelope
through it end to end. The very first happy-path fixture — `apply` a ConfigMap — was refused at step
9 by an integrity check, with a message saying the classifier had been shown no changed fields.

**The finding.** `classify` and `execute` both have a concept named `WholeObject`, and they mean
different things by it.

- `classify.Resolve` sets `WholeObject` for `create`, `apply` **and** `delete`. From a rule's point
  of view that is correct: all three touch every field, so there is no path list to reason about.
- `execute.CheckIntegrity` accepts `WholeObject` for `create` and `delete` **only**, and its
  `default:` arm rejects anything else — "a field-level verb with no path set was not checked by
  anything". That is also correct: for an `apply` there _is_ a computed diff, and accepting
  "everything changed" would let a change pass the integrity check without any path being compared.
- `execute/integrity_test.go`'s `TestIntegrityWholeObjectIsNotAnEscapeHatch` asserts the second
  reading deliberately.

Neither package is wrong. What was missing is the conversion between them, which did not exist
because nothing had ever called one with the other's output. The pipeline is that first caller.

**Consequence, at the time of the finding.** Only `create`, `delete` and JSON-patch `patch` could
traverse the pipeline. `apply`, `scale` and merge-patch failed closed at step 9 — refused, nothing
mutated — so this was missing functionality and not a hole. It was recorded as a test
(`TestApplyFailsClosedAtTheIntegrityCheck`) rather than a comment, so the gap was a property with a
verdict instead of a surprise waiting for the next reader.

**Root cause, one level down.** `classify.RawOp.Patch` is documented as carrying "the RFC 6902 patch
for `patch`, **or the computed diff for `apply`**". `pipeline.rawOps` only fills it for the JSON
Patch media type. So an `apply` reaches the classifier with no `TouchedPaths` at all — which means
it is not only unexecutable, it was being classified without the per-path direction analysis every
`patch` gets. Fail-closed saved it; the classification was the quieter defect.

**Why this is a distinct class from [[lsn-007]], which it superficially resembles.** LSN-007 is
"built, tested, and unreachable" — one component with no caller. This is two components each with
callers and tests, which agree on a type signature and disagree on what a field of it means. No
check on either package could have found it, because each package's tests assert its own reading and
both readings are right. It is findable only by the first thing that puts them in a line, and
therefore the mechanization has to be at the seam, not in either package:

**Mechanization, as built.** P9-T7c-4a supplied the classifier the computed
`execute.Diff(snap.Live, desired)` for an `apply`, `/spec/replicas` for a `scale`, and a recursive
walk for a merge-patch (**V-BRK-020**). P9-T7c-4b added the part that is the lesson rather than the
bug: **V-BRK-022**, every verb in the envelope's closed enum driven end to end through the assembled
pipeline, the verb set read out of `broker.ValidOps()` rather than from a list someone wrote down
([[lsn-036]]). A table covering the three verbs that happened to work would have printed green
throughout the entire period in which the other three did not.

**What the mechanization found the day it was written, which is the whole argument for it.** With
`apply` fixed, three of five verbs passed and two did not — and both failures were the same shape as
the lesson: a package that is individually right, and an assembly that is the first caller to put it
in a line.

- **`delete` could never be verified.** Every row of 04 §5.1 asserts something about a live object
  and `verify.mustGet` maps NotFound to `VerdictFailed`. Correct for a create; exactly backwards for
  a delete, which succeeds by the object being gone. A delete that worked would have been reported
  failed and rolled back by recreating what it deleted. Fixed with `verify.Target.ExpectAbsent` and
  an `absencePredicate` chosen by the action rather than by the kind.
- **No Deployment or StatefulSet action could ever be verified.** `pipeline.verifyTargets` built
  `verify.Target{Ref: r}` and dropped every other field, so `BaselineRestarts` was always nil,
  `workloadPredicate` always returned `VerdictIndeterminate`, and the settle window always expired
  into `VerdictFailed` — a rollback of every workload change that worked. Fixed by capturing the
  baseline at step 3, alongside the snapshots, where it is still pre-action.

Neither was reachable from any package's own tests. `verify` tested `workloadPredicate` with a
baseline supplied by the test; the pipeline tested a ConfigMap, whose row needs none.

**Two further seam defects the same unit closed, both of the [[lsn-041]] shape.**
`classify.knownVerbs` said "the corpus lint asserts the two agree" and no such lint existed; it is
now `TestClassifyKnownVerbsAgreeWithTheEnvelopeEnum`, in `package pipeline` because that is the
lowest package importing both. Its one legitimate divergence, `cloud`, is declared in code as
`classify.VerbsNotCarriedByAnEnvelopeOp` with a written reason, and the condition that makes the
divergence safe is a property (`TestNoCloudTargetReachesTheClassifier`) rather than a sentence.

**Still open, filed not fixed.** `agentv1alpha1.ChangeVerb`'s kubebuilder enum is a **third** copy of
the verb set, and its doc comment claims it "mirrors the envelope's own" with nothing comparing them.
Both mismatch directions fail closed today, so it is a finding rather than a defect — see the
Phase 9 breakdown.

**Complication for whoever does it.** `execute.DiffResult.Ops` are
`agentv1alpha1.AppliedDiffOp{Op, Path, From, Value string}` — `Value` is a **rendered string**, so
feeding a computed diff straight into `classify.PatchOp.Value` is lossy for the typed rules
(`DirectionOfBoolField` would see `"true"`). Secret egress is unaffected: it is scanned from
`RawOp.Payload`, the desired state, independently of the patch.

Related: [[lsn-007]] (unreachable code), [[lsn-036]] (a list of verbs is a headcount),
[[lsn-015]] (one fixture cannot find a disagreement between two).

---

## LSN-041 — The comment claiming the hole was closed was the reason nobody looked

**Tags:** admission, CEL, policy, security, fail-open, checks · **Phase:** 9 (P9-T7c-3c-ii-b-1) ·
**Status:** closed — mechanized by `dev/tests/journal-status-vap-parity.py` (L0)

**What happened.** Adding one field to `ActionRecordStatus` meant adding a row to
`config/policy/vap-agent-scope-journal.yaml`, the ValidatingAdmissionPolicy that decides who may
write which part of the journal. The policy's own header comment said an unenumerated status field
was caught by a catch-all variable, `unknownStatusFieldChanged`. Grepping for that variable to see
what shape to follow returned nothing. It had never existed.

**What that cost.** Validation 1 admits any UPDATE for which `nothingChanged` holds, and
`nothingChanged` is a conjunction over the **enumerated** fields only. So the policy failed open on
every field it did not know about — not for one principal, for all of them, including the human
cluster-admin the policy exists to stop and every agent identity in the namespace. The field about
to be added is `status.escalation`, which `C-BR` fans out into `spec.operations.paused`. Shipping it
under that policy would have been an unauthenticated stop button for any agent in the namespace,
with an audit trail saying the stop was deserved.

**Why CEL could not close it where the comment said it was closed.** There is no catch-all to write.
CEL cannot enumerate a struct's keys at runtime, so the nearest expressible thing —
`oldObject.status != object.status` — denies the no-op re-Update every controller performs on a
conflict retry. The property is real, it is just not expressible at the site where the reader
expects it. That is exactly the situation in which a comment gets written instead of a check.

**The failure class.** Not "somebody wrote a wrong comment". The comment was the previous attempt at
this fix: someone identified the hazard, could not express the control, and wrote down the control
they wished existed. It then survived several reviews **because it read as protection** — a reviewer
scanning the policy for a fail-open found a sentence saying there wasn't one. Prose that describes a
control is worse than no prose, because it retires the question.

**The mechanization.** `dev/tests/journal-status-vap-parity.py`, two lines in `dev/L0-CHAIN.txt`
(the check and its negative control). It reads the **Go type** — `api/v1alpha1/actionrecord_types.go`,
the definition site — rather than the generated CRD, because the generated artifact is stale for the
entire duration of the edit that adds a field, which is the only moment this check matters. Five
properties: every status field has a CEL variable; every variable is conjoined into
`nothingChanged`; each variable reads its own field and **only** its own field (set-equality over
known field names, evaluated on every branch of the expression); no phantom variables naming fields
that do not exist; and non-vacuity floors so a refactor that empties either side is not silently
green.

**The negative control found a hole in the check itself.** Seven mutations; one survived — a
variable renamed onto another field's expression. The mutation rewrote only the first of four CEL
branches, so the field name still appeared elsewhere and a "mentions the field" test passed. Fixed
by strengthening the check to set-equality, **not** by strengthening the mutation. A negative
control you adjust until it fails is a second comment.

**The second control, which is a different property.** `escalation` has two writers by design: the
broker requests, `C-BR` fulfils. A new variable `escalationFulfilmentSet` plus validation 2b forbids
the **owning** broker from writing `pagedAt`, `pausedAt` or `failure`. A self-attested page is worse
than no record: the audit trail then asserts the promise was kept. And the exporter's row was
deliberately **not** widened to let `C-BR` through, even though `C-BR` will arrive on the same
service account — the exporter is the one principal whose write unlocks deletion, so `C-BR` gets its
own identity or nothing.

**What to do next time.** When a comment in a security artifact asserts that a control exists, the
first move is `grep` for the control, not a nod. If it is missing, the hole is live and the comment
is evidence that at least one person already talked themselves out of closing it.

Related: [[lsn-038]] (a guard that fails safe still fails, and a green run is how it tells you),
[[lsn-036]] (a headcount goes stale when the population grows), [[lsn-015]] (one fixture cannot find
a disagreement between two).

---

## LSN-042 — Nothing in the repository ever built the thing the repository installs

**Tags:** build, ci, deploy, kustomize, install-path, checks · **Phase:** 9 (P9-T7d-6) ·
**Status:** closed — `make render` + **V-CMP-008** for the overlay,
`dev/tests/install-artifacts-are-rendered.py` for the general property

**What happened.** Surveying for P9-T7c-3c-ii-b-2-b, `kustomize build config/default` failed:
`multiple matches for selector Certificate.v1.cert-manager.io/[noName].[noNs]:metadata.name`. Not
intermittent, not environmental — deterministic, and true of every commit since PR #44 landed the
mesh CA a month earlier. `config/default` is what `make deploy` renders, and
`provision_03_gcp_gke_operator.sh` goes through `make deploy`. The sanctioned install path had been
broken for a month.

**Why nothing said so.** Because nothing rendered it. `make build` and `make test` are Go targets.
`l0-checks.yml` cannot render — it installs no dependencies by design and kustomize is a downloaded
binary. `docker-build.yml` builds images. `k8s-operator-test.yml` runs envtest. The L2 chain
deploys, but the L2 chain does not run in PR CI. Every one of the eight required checks was green on
every commit in that window, and each of them was telling the truth about a different thing.

**What it was hiding, which is the worse half.** With the selectors pinned, the render succeeded —
and produced `ClusterIssuer/kubeagents-kubeagents-mesh-ca` and a CA `Certificate` in
`kubeagents-system`. `config/default` carries `namePrefix: kubeagents-` and
`namespace: kubeagents-system`; a kustomize transformer reaches every resource beneath it with no
per-resource opt-out; and the mesh CA is three objects whose name and namespace are both
load-bearing — the name because `meshCAIssuerName` in `mesh_trust.go` hardcodes it into every agent
`Certificate`'s `issuerRef`, the namespace because a `ClusterIssuer` resolves `ca.secretName` from
the cluster resource namespace and nowhere else. Neither rewrite is an error. Both apply cleanly.
The result is a control plane with no working trust root, reported as brokers that never become
Ready, hours and several layers from the cause.

**So the tempting fix was the dangerous one.** Pinning the selectors is a six-line diff that turns a
red render green. It is strictly worse than leaving it broken: today nothing installs, and nobody is
misled; with only the pin, a broken trust root installs silently. The failing render was the only
thing standing between the repository and a plausible-looking bad install. **A red that has been red
for a month is not a nuisance to clear — it is the last remaining report from a check that stopped
running.**

**The failure class.** Not "a selector was ambiguous". It is [[lsn-007]] ("built, tested, and
unreachable") one level up: not a component nothing wires, but an **artifact nothing builds**.
`install-path-wired.py` proves the scripts reach each other and `identity-has-install-path.py`
proves the manifests reach a script — and both are green on a repository whose overlay does not
render. Reachability of the applier says nothing about buildability of the applied.

**The mechanization, and why it is split across two levels.** "It renders" and "what it renders is
the install" are different properties. The first is `make render`, now a prerequisite of `build` and
`test`, which is how CI reaches it (`k8s-operator-test.yml` runs `make -C k8s-operator test`); it
needs the kustomize binary, so it is not L0. The second is
`dev/tests/install-render-is-faithful.py` (**V-CMP-008**), which asserts the **reference graph**
rather than the output — no transforming kustomization may reach `config/mesh-ca` anywhere in the
inclusion graph — so re-nesting the CA under some *new* transforming layer fails too. A check that
only knew about `config/default` would have passed the same bug with a different diff.

**The general property**, which is the part that mattered, is
`dev/tests/install-artifacts-are-rendered.py`, landed in the 2026-07-29 improvement pass: every
kustomize directory an install path **applies** must be one `make render` **builds**. Both sides are
read out of the Makefiles and scripts, so a sixth integration is enforced the day its `apply` line
lands rather than the day someone remembers ([[lsn-036]]).

It found four on its first run — LiteLLM base, the ChatGPT overlay, inference-replay and the GitHub
integration, all applied by `deploy-*` targets and rendered by nothing. All four rendered cleanly
once asked, so this was luck rather than a second outage, and luck is the reading that should worry
someone: the gap was identical to the one that cost a month, and the only difference was which
overlay happened to be broken.

The check has a second half that is not obvious and is the one most likely to be needed: `render`
must remain a **prerequisite of `build` and `test`**. `k8s-operator-test.yml` runs
`make -C k8s-operator test`, and that prerequisite is the entire route from CI to a rendered
install. Extending the recipe while dropping that word restores the original blind spot exactly,
looks like a cleanup in review, and is one of the seven negative-control mutations.

**Scoped out, with the argument.** `envsubst < x.yaml.template | kubectl apply` is an install path
with no builder, so there is no render to demand of it. Reachability of those is [[lsn-039]]'s
`identity-has-install-path.py`; structural validity of the substituted output needs the variables
and therefore a cluster. The failure this lesson is about needs a *build* to be silently broken, and
envsubst has no merge semantics, no selectors and no inclusion graph to be wrong about.

Related: [[lsn-007]] (built, tested, unreachable), [[lsn-039]] (the manifest is correct and no
install path applies it), [[lsn-036]] (a headcount goes stale when the population grows),
[[lsn-038]] (a guard that fails safe still fails, and a green run is how it tells you).

---

## LSN-043 — The drain was done, verified green, and then quietly reverted

**Tags:** harness, orient, durability, git · **Phase:** 9 (P9-T7d-6) ·
**Status:** closed — `harness-run` §1 step 6 + `_drain_is_committed` in `dev/tests/invariants-gate.py`

**What happened.** A human appended an item to `BACKLOG.md`'s inbox while PR #60's merge was in
flight. `gh pr merge --squash --delete-branch` refused to switch branches over the dirty file, so
the file was parked with `git stash push -- docs/build/BACKLOG.md`, the merge completed, and
`git stash pop` restored it. ORIENT then drained it properly: two IDs, a written ruling, the prose
preserved, `Last drained` stamped, and `invariants-gate.py` green 17/17 with the drain check
passing. Some steps later — a branch creation and a survey — `git status` showed `BACKLOG.md`
unmodified and identical to `origin/main`. The entire drain was gone.

**What that would have cost.** Silence. The inbox was empty and `Last drained` was today's date, so
`check_backlog_is_drained` passes on the reverted file exactly as it passes on the drained one — the
check asks whether anything is *sitting* in the inbox, which is the right question for the failure
it was built for and the wrong one here. B-001 and B-002 would simply have ceased to exist, with
every gate green. The only reason they were recovered is that a `cp` to `/tmp`, made for an
unrelated reason during the stash dance, happened to still be there.

**The failure class.** An ORIENT-mandated artifact lived only in the working tree across operations
that move `HEAD`. Every other ORIENT output is either read-only or written at CHECKPOINT, where it
gets committed; the drain is the one thing ORIENT is required to *write*, and the protocol leaves it
uncommitted until the end of a unit that may take hours and will certainly touch git. The window is
structural, not accidental.

**The mechanization**, landed in the 2026-07-29 improvement pass, is two halves that have to move
together — the procedure and the thing that enforces it:

- `.claude/skills/harness-run/SKILL.md` §1 step 6: the drain is committed as its own commit **before
  SELECT**, not carried to CHECKPOINT. Durable by construction rather than by vigilance, and it
  lands the drain on the branch it was reasoned on.
- `_drain_is_committed` in `dev/tests/invariants-gate.py`: fails when `BACKLOG.md` is dirty **in the
  direction a drain moves it** — inbox items removed, or `Last drained` advanced past HEAD's.

That direction test is the whole design. `BACKLOG.md` exists so a human can append to it mid-unit,
which means "dirty" is the normal, sanctioned state and cannot itself be the finding. An append adds
items and leaves the date alone; a drain removes items or advances the date. Only the second shape
is uncommitted-and-lossy. The check also asserts the skill still carries the sentence, because a
gate enforcing a rule the documentation no longer states is a rule nobody can follow on purpose.

Note what is *not* claimed: this cannot make the drain survive a `git checkout` of an uncommitted
file. Nothing can. It makes the window observable — the next gate run says so — where before, the
reverted file and the drained file were byte-identical to every check in the repository.

Related: [[lsn-038]] (a green run is how a guard tells you it failed safe), [[lsn-012]] (repository
mechanics resolved by content, never by name).

---

## LSN-044 — `auth can-i patch foo/status` asks about a name, not a subresource

**Tags:** rbac, checks, vacuity, kubectl, L2 · **Phase:** 9 (P9-T7c-3c-ii-b-2-b) ·
**Status:** closed — `dev/tests/cluster-check-hygiene.py` property 1, in `dev/L0-CHAIN.txt`

This header said `open` for one improvement pass after the index row said `closed`, which is its own
small defect: the count that decides whether the next invocation is an improvement pass is kept in
two places, and one of them was stale. `check_lesson_status_matches_its_index_row` in
`dev/tests/invariants-gate.py` now fails on the disagreement.

**What happened.** `brake-fanout-l2.sh` asserts C-BR's grant against the live API server by
impersonating its ServiceAccount. The `actionrecords/status` row was written the way the RBAC rule
itself is written:

```
kubectl auth can-i patch actionrecords.kubeagents.x-k8s.io/status --as=...
```

`kubectl` parses a positional `TYPE/NAME`, not `TYPE/SUBRESOURCE`. That command asks whether the
subject may patch **an ActionRecord literally named `status`** — and the answer is `no`, because
`brake-controller-role` grants `patch` on `actionrecords/status` and nothing on `actionrecords`.
The subresource has to be `--subresource=status`.

**What that would have cost.** The row was written as a `want_yes`, so it failed loudly and got
fixed in minutes. The dangerous direction is the other one. Every `want_no` in that section —
`may not delete actionrecords`, `may not create agents`, `may not get secrets` — is a denial
assertion, and a `want_no` written in the positional-subresource form is **vacuously green**: it
gets its `no` from a resource name that was never granted to anybody, not from the policy under
test. It would pass on a ClusterRole with `verbs: ["*"]` on the subresource. A whole seam of the
08 §2.7 authority boundary can be "proven" by a check that never asked the question.

**The failure class.** A CLI whose argument grammar collides with the notation of the thing being
tested. `actionrecords/status` is exactly how RBAC, the VAP, the ClusterRole YAML and every
comment in the tree spell that subresource, so transcribing it into `auth can-i` is the natural
motion and the resulting command is well-formed, exits 0, and prints a plausible answer. Nothing
about it looks wrong. This is [[lsn-034]]'s shape — a value compared against itself — in a
different costume: the check and the defect share a spelling.

**The mechanization, so far.** The check can no longer express the bug. `can()` refuses any
resource argument containing `/` with a message naming the fix, and `subj()` reconstructs the
`resource/subresource` label for the report from the actual `--subresource=` flag, so the printed
line and the executed query cannot drift:

```bash
can() { case "$2" in */*) bad "check bug: can() was passed '$2'..."; echo "malformed"; return ;; esac
        $K auth can-i "$1" "$2" --as="$BRAKE_SA" "${@:3}" 2>/dev/null; }
```

A grep of `dev/` and `k8s-operator/scripts/` confirms this was the only site — no other check was
vacuously green on it. That is luck, not structure: nothing stops the next L2 suite from writing it
the same way.

**Closed** by `dev/tests/cluster-check-hygiene.py` property 1, wired into `dev/L0-CHAIN.txt` in the
2026-07-29 improvement pass. It turned out not to be a three-line grep. `auth can-i` has three
argument shapes in this tree, and a grep sees only the first:

- **literal** — `auth can-i get nodes`. A slash here is the bug verbatim.
- **`for`-list** — `for pair in "impersonate users" …; do auth can-i $pair`, the idiom in
  `verify-phase2.sh` and `verify-phase3.sh`. The check enumerates the list and expands each item
  into the positional slots, so these sites get the strict treatment rather than a weaker one.
- **computed** — `can()` above, where the resource is `"$2"`. Statically unresolvable, so the
  obligation shifts: the script must carry the `*/*)` arm itself. That is strictly stronger than the
  static ban where it applies, because it also catches a slash that only appears at runtime.

Without the third clause the whole property is evaded by the most ordinary refactor there is —
hoisting a repeated query into a helper. Which is precisely how it was evaded here.

The first draft also demanded a runtime guard from `verify-phase2/3/4.sh`, none of which compute a
resource: it read `-n $NSX` as a positional, because it had no model of which flags take a separate
value. A check whose parser is wrong in the *strict* direction is merely noisy; the same parser
wrong in the other direction is a blind spot, which is why the tokenizer now consumes flag values
explicitly and one of the twelve negative-control mutations hides a slash behind a flag.

Related: [[lsn-034]] (a value compared against itself), [[lsn-035]] (a redundant guard and an
unenforced guard look identical from a green suite), [[lsn-038]] (a guard that fails safe still
fails).

---

## LSN-045 — The suite wrote to an append-only journal, then tried to delete the namespace

**Tags:** checks, L2, fixtures, journal, append-only · **Phase:** 9 (P9-T7c-3c-ii-b-2-b) ·
**Status:** closed — `dev/tests/cluster-check-hygiene.py` property 2, in `dev/L0-CHAIN.txt`

**What happened.** `brake-fanout-l2.sh` created a namespace, created `Agent`s and `ActionRecord`s
in it, and deleted the namespace on exit — the shape every other L2 suite in `dev/verify/` uses.
The first run passed and cleaned up. The second run could not create its namespace: the first one
had been `Terminating` for the entire interval and will be forever.

`kube-agents-journal-retention` denies DELETE of an `ActionRecord` to every principal except the
retention controller and the operator, **and denies it even to them unless
`status.exported.confirmed` is true**. The namespace controller is not on that list. The scratch
cluster has no audit sink, so `journal_reconciler.go` takes the branch that logs _"the record will
be retained indefinitely because the export is the durable record (05 §1.2)"_ and nothing ever sets
`exported.confirmed`. The namespace therefore cannot finish terminating, and its name cannot be
reused.

**This is the journal working.** Not a bug in the VAP, not a bug in the reconciler — an
append-only guarantee is supposed to outlive the convenience of whoever wrote the fixture. The
defect is entirely in the check's assumption that a namespace it created is a namespace it owns.

**The tempting fix is the one to name.** Patching `status.exported.confirmed: true` on the
suite's own records makes the namespace deletable in one line. It is also **writing an export
confirmation for an export that never happened** — forging the exact field 05 §1.2 makes the
durable record, in shipped tooling, where it becomes the idiom the next suite copies. It was
proposed during this unit and declined. No test's tidiness is worth teaching the repository that
the audit trail is editable when it is in the way.

**The redesign, and why each part is load-bearing.** The suite now never deletes a namespace:

- **Reuse, do not create.** `if ! kubectl get ns "$n"; then create; fi`. Namespaces are durable
  fixtures now, and the entry path deletes only the `Agent`s inside them, `--wait=true`.
- **Mint action IDs per run** from the run's epoch (`mk_action_id`), because leftover records from
  the previous run share the namespace and a fixed ULID would collide on create — or worse, be
  silently satisfied by the residue.
- **Select Events by `involvedObject.uid`**, not by name. `unbraked-agent` is recreated with the
  same name every run; an `AgentEscalated` Event from three runs ago would satisfy a name-scoped
  negative control and turn L2-4 — the whole "a quiet agent is not paged" half — vacuous. The
  suite reads both UIDs at fixture time and **exits** if either is empty rather than falling back.
- **Say what is left behind.** The exit banner names the records and explains why nothing may
  delete them, so the residue reads as design rather than as a failed cleanup.

**Residue to disclose.** Namespace `brake-fanout-l2` on `gke-scratch-kube-agents-dev` is
permanently `Terminating`, holding three `ActionRecord`s. It is inert and the suite no longer uses
that name (`brake-fanout-l2-braked` now). Clearing it means either forging an export confirmation
or removing the finalizer out from under the policy — **a human call, not the harness's.**

**Still open.** The general rule: an L2 suite that creates a resource governed by a retention
policy may not delete the namespace containing it. `brake-fanout-l2.sh` is today the only script in
`dev/verify/` or `dev/tests/` that creates an `ActionRecord` at all — so the sweep found one site
and it is fixed — but P9-T9's consolidated gate and the broker L2 suites are all going to create
them.

**Closed** by `dev/tests/cluster-check-hygiene.py` property 2, wired into `dev/L0-CHAIN.txt` in the
2026-07-29 improvement pass: no script that creates a DELETE-protected object may also delete a
namespace. Ten of the tree's shell scripts delete a namespace and that is fine — the pairing is what
is forbidden, so the check costs nothing until the two meet.

The set of protected kinds is **derived, not listed** ([[lsn-036]]). It is read out of the
`ValidatingAdmissionPolicy`s themselves — every resource any policy matches for `DELETE` — and the
plural is then resolved to a Kind through the CRD that declares it. Hardcoding `ActionRecord` would
have been one line and would have made the check a headcount of today's policies; as written, a
second retention policy over a second resource starts being enforced the day it lands, and a policy
naming a resource no CRD declares is itself a finding. Both directions of that derivation carry a
non-vacuity floor, because a property asserted over a set that quietly emptied reports success.

Related: [[lsn-037]] (the build that ships is not the build you tested — residue is the same class
of stale input), [[lsn-036]] (a single-site check is a headcount, and this sweep found exactly one
site).

---

## LSN-046 — The deferral named the category, so the gate had nothing to match

**Tags:** checks, deferrals, record-keeping, false-green, blocking-always, append-only ·
**Phase:** 9 (improvement pass, 2026-07-29) · **Status:** closed —
`check_dagger_checks_are_deferred_by_id` in `dev/tests/invariants-gate.py`, in `dev/L0-CHAIN.txt`

**What happened.** The ledger's Deferrals table carried one row reading `09 §6.14 checks marked
**†**` — the set of checks blocked on an unresolved 09 §12 specification tightening. Accurate,
readable, and the kind of row a human resolves in a minute by opening §6.14.

`check_deferrals_name_blockers` has an arm for the rule that matters most in that table: **a
BLOCKING-ALWAYS check may not be deferred** (09 §9.6). It finds them by running a check-ID regex
over the row's subject cell. Against `09 §6.14 checks marked **†**` it matches nothing and reports
clean. Four checks were inside that row. One of them, **V-CTN-021**, is a V-CTN check.

So the arm had been silent about a deferred BLOCKING-ALWAYS check since the row was written — not
because anyone concealed anything, but because **the arm could be satisfied by naming a set instead
of its members**, and a silence that can be bought that cheaply is not evidence. That is [[lsn-035]]
one level up from the checks that lesson was about: an assertion passing for a reason unrelated to
its property.

**Then the second one.** Writing the fix, I gave the arm a narrow exemption — a BLOCKING-ALWAYS
check whose 09 §6 phase has not arrived is unstarted, not deferred, which is the ordinary condition
of all future work. V-CTN-021 is required at phase 11 and the build is at 9. An exemption is only
safe if it expires, so I tested it at phases 9, 10, 11, 12 and unknown, expecting three failures.

All five came back exempt. The exemption was never firing at all: the arm short-circuits on "green
somewhere", and `verification/results.csv` row 47 (2026-07-27, P8-T9) recorded
`"V-CTR-002, V-CTN-021"` as **pass**. The evidence on that row is `webhook-negatives-l2.sh` proving
V-CTR-002 — each of 06 §1.2's V-1…V-10 negatively tested, naming its field path — which is real
evidence for V-CTR-002 and says nothing whatever about conformance of the 39 cells of the 02 §7
boundary matrix. There is no V-CTR-021, so it is not a one-letter slip from the neighbour. A
BLOCKING-ALWAYS check that had never been run, and currently **cannot** be run, was green for two
days.

**The lesson is the interaction, not either half.** A false pass is not inert. It is an input to
every check that asks "has this ever been green", and each of those goes quiet in a way that looks
exactly like the property holding. The category row hid V-CTN-021 from the arm; the false pass
would have kept hiding it even after the row was fixed. Two independent defects, each sufficient,
found in one sitting only because testing the exemption meant watching it fail — and it didn't.
**A carve-out you cannot watch expire is a carve-out you have only asserted.**

**The third one, found by the fix.** Appending the correction row did not clear it either. The
record is append-only and `_verification_evidence_rows` grepped it for the word: row 47 still
contains `V-CTN-021` and `**pass**` and always will. The `**correction**` convention the record has
followed since V-MET-014 (rows 18–19) was **decorative to every reader of the record** — a retracted
claim went on vouching for itself forever. Fixed by emitting passes per check ID and dropping any a
later correction retracts. Per-ID matters as much as supersession: row 47 is one cell naming two
checks, of which exactly one was really run, and emitting the row whole makes the honest half vouch
for the other half indefinitely.

**Closed by** three changes in `dev/tests/invariants-gate.py`, all on the L0 chain:

1. `check_dagger_checks_are_deferred_by_id` — the † set is **derived** from 09 §6.14 ([[lsn-036]]),
   never listed, and an empty derived set is a FAIL rather than a clean run. Arm one: every member
   must be named by ID in the Deferrals table. Arm two: no member may hold a pass that no later
   correction retracts, because "blocked on a tightening" and "passed" cannot both be true.
2. `_verification_evidence_rows` — per-check-ID emission with correction supersession, so the
   append-only record can express a retraction to a machine and not just to a reader. The last
   correction wins, so a check that was corrected and later genuinely re-passed still counts.
3. The not-yet-due clause on the BLOCKING-ALWAYS arm — granted only while the required phase has
   not arrived, only when both phase numbers parse, and lapsing with no edit to anything. An
   unparseable phase is the unknown case and the unknown case fails ([[lsn-038]]). At phase 11 the
   row goes red on its own, which is what makes T-6 a deadline rather than a note.

Verified by reproducing all three: 4 failures before the row was fixed, 1 more for the false green,
0 after the correction landed, and the exemption now failing at phases 11, 12 and unknown while
staying granted at 9 and 10.

Related: [[lsn-035]] (a control that proves only that *something* failed — the same shape, applied
to a gate arm instead of a negative control), [[lsn-036]] (derive the subject set, never enumerate
it), [[lsn-038]] (a guard that cannot run must not score as a pass).

---

## LSN-047 — The mutation sweep backed up two files under one name and restored the wrong one over the other

**Tags:** mutation, tooling, harness, near-miss · **Phase:** 9 (P9-T7c-3c-iii, 2026-07-29) ·
**Status:** closed — `dev/mutate.py` (the sweep is configured, not authored) + `harness-run` §5 and
`harness-verify` §3, which route every non-vacuity sweep to it. Tested by `dev/test_mutate_sweep.py`,
whose first case is this one: two files called `cooldown.go`, snapshotted and both restored.

**What happened.** P9-T7c-3c-iii added `internal/broker/cooldown/cooldown.go` and changed
`internal/broker/verify/cooldown.go`. The unit's mutation sweep was a throwaway Python driver that
snapshotted both files into one directory keyed by **basename**. Both files are called
`cooldown.go`. The second `cp` overwrote the first, and the `finally` block then restored the
`verify` copy over **both** paths.

The sweep's own output said `12/13 caught`, exit 0. It was correct — the sweep had run against
intact sources and only clobbered them on the way out. The damage surfaced on the next run, which
reported `2/13 caught` with eleven `anchor not found` lines: the anchors were missing because the
file was no longer the file.

**Why it is worth an ID even though nothing was lost.** The clobbered file was untracked and less
than an hour old, so `git` could not have restored it and it was rewritten from the session's own
transcript in two minutes. Change one variable — an hour of reasoning instead of ten minutes, or a
session boundary between writing and sweeping — and this is [[lsn-022]] verbatim, which is the
lesson that cost this repo two hours of finished work. **A near-miss that only missed because of a
property of this particular file is not a near-miss, it is the same defect with a lucky input.**

There is a second failure inside the first. The restore reported nothing. It could not: `cp
BAK/cooldown.go a/cooldown.go` succeeds whatever `BAK/cooldown.go` contains. A restore that cannot
distinguish "put back what was there" from "put back something else that was there" is a restore
that verifies its own mechanics and not its property — [[lsn-035]] applied to tooling instead of to
a check.

**What the fix is, and why the lesson is open anyway.** `dev/mutate.sh` — written to close
[[lsn-022]] — already does the right thing: it snapshots into `$snap/$n` keyed by **position**, not
by name, restores on success, failure and signal, and refuses upfront if any named file is missing.
It has a test suite. Nothing about it is inadequate.

The gap is that **nothing routed this unit's sweep to it.** `harness-run` names mutation testing
nowhere; the sweep is a habit the units have, carried in prose in `results.csv` rows, and a habit
reimplements its tool each time it is exercised. That is the [[lsn-019]] shape — a lesson marked
closed, with a real and working mechanization, whose defect recurs because the mechanization is a
thing you have to remember to pick up. An L0 lint cannot see a script in `/tmp`, so the enforceable
form here is a **skill change** (SELF-IMPROVEMENT §3.1): `harness-run` §5 should say that a mutation
sweep runs through `dev/mutate.sh`, which makes the tool the default path rather than the one you
reach for after remembering LSN-022. Left open and scheduled for the next improvement pass, because
a skill edit is a mechanization and mechanizations are that pass's work, not a unit's.

Related: [[lsn-022]] (the original: a git-shaped revert answering "what should this file contain"
instead of "what did it contain a moment ago"), [[lsn-030]] (the same class again, one verb over),
[[lsn-019]] (a closed lesson whose mechanization is not on any path the work takes), [[lsn-035]] (an
operation that verifies it ran rather than that it worked).

---

## LSN-048 — The sweep could not find its own test, and called that a hole

**Tag:** mutation, tooling, checks, vacuity, false-finding · **Status: closed** — `dev/mutate.py`
refuses a spec carrying `-run`, verifies every declared catcher against `go test -list` before the
first mutation, and scores an unevaluatable mutant `BROKEN` rather than counting it as a survivor.
`dev/test_mutate_sweep.py::TheCatcherMustExist` is this trigger, run for run.

**Trigger.** The P9-T7c-3d-ii-a mutation sweep, eight mutations over `broker/brake.go` and
`pipeline/pipeline.go`. It came back **5/8**, with three lines reading
`SURVIVED  <-- the check does not detect this`.

**The finding.** All three "survivors" named a test that does not exist. The sweep ran
`go test ./internal/broker -run 'TestBrakeFailClosed'`; the table test in that package is called
`TestBrakeEachRuleFiresInIsolation`. A `-run` pattern that matches nothing prints
`testing: warning: no tests to run` and **exits 0** — so the composing idiom this repo sanctioned in
[[lsn-047]], `dev/mutate.sh f -- sh -c 'mutate && ! check'`, evaluates `! 0` as false and scores the
mutation uncaught. Nothing about `dev/mutate.sh` is wrong here; it restored both files byte-perfectly
every time. The tool cannot see the `-run` pattern, and the pattern is the part that was broken.

**Why the direction of the lie is the whole lesson.** A sweep that falsely reported CAUGHT would be
a silent false green, and this repository has a well-developed reflex for that shape. This one
falsely reports a **hole** — which does not read as a malfunction, it reads as a *result*. The
natural next move on a survivor is to go strengthen the test. That test would pass on the first run,
because the property was already covered; it would look exactly like the fix; and the three
mutations would still never have been evaluated even once. The sweep's verdict on them is
**unknown**, and it is presented as known. A false red that recommends a plausible action is worse
than a false red that merely wastes time.

Two of the three, once the pattern resolved, were mutations worth catching: `IsUndo() -> true`,
which exempts **every** action from pause and freeze, and a row 7 that refuses without escalating —
a refusal nobody is ever told about. Both are caught. Had the three been "fixed" by adding tests for
the imagined holes, the sweep would have gone green at 8/8 with those two mutations still unmeasured.

**Class.** [[lsn-035]] applied to the harness instead of to a check: an operation that verifies it
*ran* rather than that it *worked*. It is also a near neighbour of [[lsn-038]] — there, the
predicate nobody writes a negative control for is the one that can only make its caller *stricter*;
here it is the one that can only make the sweep look *worse*. Both survive review for the same
reason: erring in the safe-looking direction is not read as erring.

**Mechanization, and why it is open.** A `go test -list <pattern>` probe before each mutation: if it
selects zero tests, print `BROKEN SWEEP` and score the mutation as neither caught nor survived, so
the count cannot silently shrink either. That guard produced this unit's 8/8 and lives in a
throwaway script, which is precisely the [[lsn-047]] condition. `dev/mutate.sh` is the wrong home —
it takes `-- COMMAND [ARGS...]` and has no business parsing a `go test` invocation — so the
enforceable form is the same `harness-run` §5 skill change LSN-047 is waiting on: a sweep should be
**configured, not authored**, and the sanctioned configuration should carry the guard. The two land
together or the next unit re-earns whichever one it forgets.

Related: [[lsn-047]] (the sanctioned mutation tool that nothing routes work to), [[lsn-035]] (a
negative control only proves the suite fails, never which rule), [[lsn-038]] (the input nobody
validates because it can only fail in the safe direction), [[lsn-019]] (a mechanization off the path
the work takes).

---

## LSN-049 — The sweep's own shell quoting invented a hole, and rc==0 called it a result

**Tag:** mutation, tooling, checks, vacuity, false-finding · **Status: closed** — `dev/mutate.py`
keeps mutation text out of every shell parse (JSON in, `str.replace` in-process), refuses a needle
it cannot find exactly once, and scores on the failing-test SET rather than on `rc`.
`dev/test_mutate_sweep.py::MutationTextNeverCrossesAShellParse` replays row 14's `""` needle.

**Trigger.** The P9-T8a sweep, 15 mutations over `pipeline/pipeline.go` and `common_types.go`. Row
14 — a nil `OperationsSpec` reading as shadowed — came back `ESCAPED <- nothing failed`, twice, on
a property the suite catches cleanly.

**The finding.** The needle for row 14 contains `""`, Go's empty string literal, because the line
being mutated is `return false, false, ""`. The sweep interpolated needles and replacements into a
**double-quoted `bash -c` argument**. The embedded quote closed the string early, the applier died
before touching the file, the `&&` short-circuited — and the surrounding `dev/mutate.sh` invocation
still exited **0**, because nothing in the chain reported the applier's death upward. The sweep read
that 0 as "the whole test suite passed with the mutation in place" and printed an escape for a
mutation that had never been applied.

**Why this is [[lsn-048]] with the sign flipped, and why the flip does not help.** There the tool
hid a hole (a `-run` pattern matching nothing exits 0). Here it invented one. Both reduce to the same
root: **the sweep scored an exit code it had not established was the test suite's.** `rc == 0` is
load-bearing in a mutation sweep and it is produced by a pipeline of things that are not the test
suite — a `-run` filter, a shell parse, an applier, a `&&`. Any of them can hand back a 0 that means
"I did nothing".

The invented-hole direction is not the safe one. It costs a real investigation (this one cost two
passes, the first of which "fixed" the wrong thing by re-attributing the row), and if the
investigation is lazy it terminates in a *weakened property* — the natural move on a mutation that
allegedly survives is to relax what you thought you were asserting.

**What actually caught it.** Requiring the sweep row to name the **specific test** that must appear
in the failing set, rather than accepting "something went red". That requirement had already caught a
mis-attribution on row 14's first pass (the intuitive guess was
`TestNothingComposesBackToExecuting`; the real catcher is `TestAnUnobservableAgentIsShadowed`), and
it is the reason the second defect was visible at all. A sweep that scores on `rc != 0` would have
gone green on both.

**Mechanization, and why it is open.** Three properties, all belonging to the same sanctioned-sweep
change that [[lsn-047]] and [[lsn-048]] are already waiting on, so that a sweep is **configured, not
authored**:

1. Mutation text never crosses a shell parse. Needle and replacement travel by environment or file
   to an applier that **refuses unless the target appears exactly once** — which is also the guard
   that turns a stale needle into `BROKEN`, not into a silent escape.
2. The applier's failure must be distinguishable from the suite's success. `&&` is not enough; the
   sweep must observe that the mutation landed before it scores anything.
3. Each row declares the test that must fail. `rc != 0` is not a catch.

Nothing here is `dev/mutate.sh`'s fault — it snapshotted and restored byte-perfectly through all 15
rows, as it did through LSN-048's eight. All three lessons are about the layer above it, which every
unit keeps re-authoring from scratch. Three re-earnings is the argument.

Related: [[lsn-048]] (the sweep that could not find its own test), [[lsn-047]] (the sanctioned tool
nothing routes work to), [[lsn-035]] (verifying that an operation ran, not that it worked),
[[lsn-019]] (a mechanization off the path the work takes).

---

## LSN-050 — The pre-commit gate cannot see a file that has not been committed

**Area:** harness, checks, coverage, false-green, pre-commit
**Status:** closed — `dev/tests/gitcorpus.py` (one enumerator, ten call sites) +
`check_l0_corpus_is_not_index_only` in `dev/tests/invariants-gate.py`, both on `dev/L0-CHAIN.txt`
**Earned:** 2026-07-29, P9-T7c-3d-iv-a

**What happened.** A new test file, `internal/broker/policy/identity_test.go`, contained a fake
API-server error string naming `agents.kubeagents.io`. The operator serves
`kubeagents.x-k8s.io`. `dev/tests/api-group-single-sourced.py` exists for exactly this — [[LSN-032]]
— and it is on the L0 chain, and the L0 chain was run in full before the push.

It printed nothing. CI failed a push later.

**Why.** `api-group-single-sourced.py` discovers its inputs with `git ls-files -z`. At the moment
the chain ran, the file was untracked: `?? k8s-operator/internal/broker/policy/identity_test.go`.
`ls-files` without `--others` lists the index, and an unstaged new file is not in the index. The
check ran, scanned 115 files, found no disagreement, and passed — correctly, over a corpus that did
not contain the defect.

**It is not one check.** Seven L0 scripts each carry their own `git ls-files` discovery with no
`--others`:

`api-group-single-sourced.py` · `cli-contract.py` · `install-artifacts-are-rendered.py` ·
`install-render-is-faithful.py` · `one-broker-per-agent.py` · `invariants-gate.py` ·
`scope-label-single-sourced.py`

**Why this is worse than an ordinary gap.** The blindness is perfectly correlated with novelty. A
file that has existed for a while is tracked and fully scanned; a file created in the current unit
is invisible until it is staged. So the checks are weakest on exactly the code that has never been
reviewed by anything, and they are silent about it — a pass over a corpus missing the file under
test is indistinguishable, in the output, from a pass over one containing it. This is [[LSN-035]]'s
shape at the harness layer: verifying that an operation *ran*, not that it ran over the thing you
meant.

And it is self-concealing in the ordinary workflow. The habit that hides it is the correct habit:
run the chain, then stage, then commit. Staging between the run and the commit is what makes the
next run see the file — so the check appears to work on every unit where the chain happens to be
re-run after `git add`, and fails silently on every unit where it does not.

**Mechanization.**

1. One shared discovery helper — `ls-files -z --cached --others --exclude-standard` — used by all
   seven scripts. `--exclude-standard` keeps `.gitignore`d build output out, so the corpus grows by
   exactly the new source files and nothing else.
2. A gate check in `invariants-gate.py`: no script on the L0 chain may call `git ls-files` without
   `--others`. Without this the helper is a convention, and the eighth script written next month
   copies the seventh.

Expect the first run of (1) to surface real findings in files that have been untracked across
several units.

**Why it is not fixed in the unit that found it.** Seven call sites, plus a corpus change that will
surface unrelated findings — a verification surface of its own, and PROTOCOL §10's rule that a check
change is never the same unit as the work that motivated it. Scheduled for the next improvement
pass. The immediate defect (the wrong group in one string) is fixed; the blindness is not.

Related: [[lsn-032]] (the check that was blind here), [[lsn-035]] (verifying the operation ran, not
that it worked), [[lsn-019]] (a mechanization off the path the work takes), [[lsn-041]] (a control
that is asserted to exist and does not).

---

## LSN-051 — A subsection is not the section, and the halt was declared against the wrong one

**Area:** harness, halts, spec-reading, PROTOCOL §8.5, rbac
**Status:** closed — see the index row; mechanized 2026-07-30
**Earned:** 2026-07-30, P9-T9b-5b-i

**What happened.** `broker-execute-l2.sh` drove the first real envelope through the broker pipeline
and stopped at step 3 on `cannot list resource "agents" … at the cluster scope`. I read outward from
that error, compared each of the executor's four live reads against **06 §2.2.1's** twenty triples,
found none of them covered, concluded that the actor grant could not authorize the gates 06 §3 and
06 §4.2 require, enumerated three resolutions that each weakened something a spec states in its own
words, and recorded **PROTOCOL §8.5 — a spec contradiction with no invariant-preserving resolution**.

There was no contradiction. **06 §2.2** — one level up, a different object, on the same page — is the
actor **scope** template, and it grants the platform actor `namespaces` and `secrets` cluster-wide
and `kubeagents.x-k8s.io/agents` cluster-wide, in rules whose header reads "these are the literal
rule bodies the render overlay emits." Three of the four reads are answered outright and the fourth
tolerates a `Forbidden` by construction. The halt cost a session and produced a blocker question a
human could only have answered by reading the section I had not.

**Why.** §2.2.1 is *sufficient for the broker's own operations* and its template says so at length;
I had read that template several times and internalized it as "the actor grant". The subsection
number reads like a refinement of the section, so having read §2.2.1 felt like having read §2.2. It
is not a refinement — it is a different grant, bound to the same identity, with a different job. My
own fixture header for `actor-tenant-grant.yaml` even cites "06 §2.2's tenant template" while its
body reasons entirely about §2.2.1's twenty triples, so the confusion was written down before it was
acted on and I did not notice it either time.

**What made it worse.** Two genuine defects had already come out of the same suite in the same
session (`brokerServiceAccount()`, and the fixture's missing `get namespaces`). Both were real, both
were found by reading outward from an error exactly this way, and the method's two-for-two record
was itself the reason the third reading went unchallenged. A halt is the one verdict that stops the
harness, and it got the *least* scrutiny of the three because it arrived last and looked like more
of the same.

**The finding that survived.** Stated correctly it is smaller and sharper: `LowerTierOwner` issues a
cluster-scoped `list agents` on every namespaced operation, `resolveOwner` guards it only with
`live != nil`, §2.2.1's `agents` rule is namespaced and scoped by its own comment to "step 5: its own
pause state", and 06 §2.2's template — the thing that *would* authorize it — has no renderer in this
tree. That is unimplemented spec, which is ordinary scheduled work (now P9-T9b-5b-0), not a halt.

**Mechanization candidates**, for the pass to choose between:

1. **A `harness-run` §7 rule: a §8.5 halt must quote the two spec statements that contradict, by
   document and section, in the ledger's blocker row.** This one would have caught it — there was
   only ever one statement, and writing the second down is where the absence becomes visible. It is
   the cheapest form and it lands on the skill, not on a check.
2. A lint that any spec citation of the form `NN §X.Y.Z` appearing in a halt or a blocker row is
   accompanied by a citation of `NN §X.Y`. Mechanical, and probably too narrow to be worth its
   false-positive rate.
3. Nothing enforceable, and rely on (1). Argue it if (1) is judged sufficient.

**Not a lesson about this halt only.** Every §8.5 to date has been declared by reading outward from
a runtime error, which is the right way to find one and the wrong way to confirm one: the error tells
you what the implementation did, and a contradiction is a property of two specs. The reading that
confirms it has to start at the spec.

Related: [[lsn-041]] (a control asserted to exist and not existing), [[lsn-035]] (verifying the
operation ran, not that it ran over the right thing), [[lsn-019]] (a mechanization off the path the
work takes).

**Mechanized 2026-07-30 (candidate 1).** `harness-run` §7 now requires a §8.5 halt to quote **both**
statements — two citations, two verbatim quotes, in the blocker row itself. `check_spec_contradiction_halts_cite_both_sides`
in `dev/tests/invariants-gate.py` pins the sentence in the skill and asserts the ledger obeys it:
every row that declares a §8.5 halt carries at least two distinct `NN §X` citations. Struck-through
withdrawn rows are exempt, deliberately — they are kept as the record of a halt that was wrong, and
a check that re-litigates them every run becomes noise and then becomes deleted.

Candidate 2 was **refused**, and the reason is worth keeping: pairing every `§X.Y.Z` with a `§X.Y`
would have passed a halt that cited two subsections of the same wrong section. It enforces the
shape of the mistake rather than the property, which is the [[lsn-023]] failure one level up.

---

## LSN-052 — The Go unit suite runs once per phase, so a golden was red for three commits

**What happened.** `TestAgentsGolden` had been failing since commit `6220874`, three commits and two
CHECKPOINTs earlier. That commit added the downward-API `KAGE_BROKER_SERVICE_ACCOUNT` env var to the
rendered broker container — a correct fix, verified against a live cluster — and did not regenerate
the two golden fixtures the renderer is compared to. It was found only because a full `go test ./...`
was run for an unrelated reason while working the next defect, and even then it was nearly
misdiagnosed twice: first as a Halt-2 regression caused by the working tree (it is not — a detached
worktree at `HEAD` reproduced it), then as a whitespace catastrophe (`-update` re-marshals every
golden with Go's 2-space list indentation, producing a 62 KB diff; `npx prettier --write` over the
regenerated files collapses it to the 10 real lines).

**Why nothing caught it.** Three layers each had a reason not to look, and the reasons are all
individually defensible:

- `dev/L0-CHAIN.txt` is the pre-commit chain, and it cannot hold this. L0 CI has Python 3.12 and no
  Go toolchain (V-MET-013 makes the file the single definition site, so a line that only runs on a
  developer's laptop would be a check that reports a verdict it did not compute — the shape already
  on the carried-findings list).
- `.github/workflows/k8s-operator-test.yml` does run `go test`, but on `pull_request: [main]` and
  `push: [main]` only. The harness commits to a phase branch for the whole of a phase and opens the
  PR at `/harness-milestone`, so on this workflow the Go suite fires **once per phase**, after
  dozens of commits, with every one of them a candidate cause.
- `harness-run` §6.1 requires "build, format, and lint pass". `make -C k8s-operator build` is
  manifests + generate + fmt + vet + build. It is not `test`, and it passed at every checkpoint.

So the honest statement is not "someone forgot to run the tests". It is that **no step any unit is
required to perform runs them**, and the one that does is scheduled at a cadence that cannot
attribute a failure to a commit.

**What made it survive.** A golden is exactly the check that rots quietly here: it is not claimed by
any check ID, so VERIFY never selects it, and its failure mode is a diff rather than a crash. The
three defects before this one were all found by driving a real envelope at a deployed broker, which
trains attention onto L2 — and L2 is the one level that cannot see a rendering mismatch, because the
cluster runs whatever the renderer emits and the golden is the only thing that disagrees.

**Mechanization candidates**, for the next improvement pass:

1. *(preferred, cheapest, earliest)* `binding.md` §Build gains a second command for any unit whose
   diff touches `k8s-operator/**`: `cd k8s-operator && go test ./...`. That puts it inside
   CHECKPOINT §6.1, where it attributes to one commit. It is a skill/binding change, not a check.
2. A `k8s-operator-test.yml` trigger on `push` to every branch, not just `main`. Correct but slower
   and off the local loop, and it does not stop the bad commit from being written.
3. A gate rule that a commit touching `internal/controller/*_manifests.go` must also touch
   `internal/testing/testdata/*/expected/`. Narrow, and it is the third gate rule of that shape;
   candidate 1 subsumes it.

Also worth recording as a procedural note wherever goldens are regenerated: `-update` then
`prettier --write`, then read the diff — never `-update` and commit.

Related: [[lsn-001]] (the artifact under test not being the artifact built), [[lsn-019]] (a
mechanization off the path the work takes), [[lsn-035]] (verifying the operation ran, not that it
ran over the right thing).

**Mechanized 2026-07-30 (candidate 1, corrected).** The command in this row is wrong and
[[lsn-054]] is why: `cd k8s-operator && go test ./...` reports `ok` for fifteen packages whose tests
skipped themselves. So the two lessons closed together, on `make -C k8s-operator test`. PROTOCOL §3
done-condition 1 and `harness-run` §6 CHECKPOINT now require the §Build test entry point for every
tree the unit touched, `binding.md` §Build names it and says why the bare command is not one, and
`check_envtest_is_run_by_the_command_checkpoint_names` in `dev/tests/invariants-gate.py` asserts all
four halves at once — including that the make target still resolves `KUBEBUILDER_ASSETS`, without
which every other clause is a rule about a command that stopped doing the thing.

Candidate 2 turned out not to be an alternative at all. It is a separate necessary half, and it
closed as [[lsn-055]] the same day.

## LSN-053 — A check split off from its implementation has two trees to be green on, not one

**What happened.** P9-T9b-5b-0-ii-a reshaped V-BRK-013 ahead of the render it will judge, because
Guardrail 9 puts a check change and the implementation that motivated it in different units. The
check passed on the tree as it stands. A throwaway probe — synthesise the widened policy and the
three read-only per-tier objects, then call `check()` on that dict — found a **false negative**: the
reshaped check would have PASSED a `developer-team` ClusterRole, which `vap-agent-readonly`'s
wrong-scope validation denies outright. 06 §2.2 gives all three tiers a template and says nothing
about the **kind** that carries it, so "render each tier's template" reads as a ClusterRole three
times: conformant to §2.2, admissible under the widened validation 3, and refused by the API server
anyway. Nothing in the unit as planned would have run that probe.

**Why it matters more than a missed assertion.** Consider what happens if the probe is skipped. The
next unit renders three ClusterRoles, V-BRK-013 stays green, `kubectl apply` fails on a live cluster,
and the diagnosis starts at the render. That is the good case. The bad case is the mirror image and
it is the one Guardrail 9 exists to prevent: a check reshaped ahead of its implementation that is
green today and **red** on the correct future tree presents, on the next unit's first run, as *"my
implementation broke the check"* — and the cheapest diff to green is to edit the check. The guardrail
separates the two units so that pressure cannot be applied; it does nothing about the pressure
arriving one unit later, disguised as a regression.

**Why nothing caught it.** The harness has a rule for the arm no input reaches ([[lsn-035]]:
non-vacuity, floors, and a negative control that names which property caught each mutation), and
5b-0-i had already applied it — the tier arm's mutations synthesise the object the render will emit.
But the synthesis was aimed at *breaking* the check, one perturbation at a time. Nothing asserted the
unperturbed future tree is **clean**. A negative control answers "does the check notice N specific
wrong things"; it is silent on "does the check accept the one right thing", and the right thing is
what the next unit is about to build.

**Mechanization candidates**, for the next improvement pass:

1. *(preferred)* A `harness-run` §4 rule: a unit whose only artifact is a check change, split from
   its implementation under Guardrail 9, must exhibit **both** trees — the current one and a
   synthesised future one — and the clean-future-tree assertion belongs in `--negative-control` as a
   row, not in a `/tmp` probe that vanishes with the session. Cheap, and it lands where the omission
   happens.
2. A convention that every negative control gains a positive control: one synthesised tree,
   representing the intended next state, asserted to produce zero findings. More general than 1 and
   more work; several checks have no meaningful "next state".
3. Nothing, on the argument that the probe was in fact run. Refused: it was run because this unit's
   plan happened to say "green on today's tree and on the tree 5b-0-ii-b will produce", and a
   plan-of-record is not a mechanism.

**Note the shape of the hole itself**, which is a second small lesson: the check knew validation 3
in detail and did not know validation 2 existed. Admission is conjunctive, so a check that models one
validation of a policy and reports "admission accepts this tree" is overclaiming by exactly the
validations it skipped. Fixed here by parsing the namespace-scoped tier **out of** the policy rather
than naming it — 03 §4 and the policy already say "developer-team is namespace-scoped", and a third
copy inside the check that exists to forbid third copies would be its own joke.

Related: [[lsn-035]] (an arm no input reaches is unexercised prose — this is its complement),
[[lsn-019]] (a mechanization off the path the work takes), [[lsn-007]] (every unit test passes and
the feature does nothing in a real install).

**Mechanized 2026-07-30 (candidate 1).** `harness-run` §4 now states the rule and names the
artifact: a check-only unit split under Guardrail 9 exhibits BOTH trees, and the future-tree
evidence is a committed `--negative-control` row, not a `/tmp` probe. A probe proves it once, to one
session; a control row proves it on every chain run and is the thing a reviewer can re-run when the
next unit's result surprises them.

`check_a_check_only_unit_exhibits_both_trees` pins both halves of the sentence, and refuses to be
green if no line of the declared chains runs a check with `--negative-control` — so the rule cannot
outlive the affordance it names, which is [[lsn-019]]'s shape applied to a rule instead of a lesson.

## LSN-054 — A skipped envtest is reported as a passing package, and `go test ./...` cannot tell

**What happened.** `TestCreateDropsStatusAndKeepsThePhaseLabel` asserted a premise about the
platform — that `client.Create` drops `status` on a subresource-bearing type, so a freshly created
`ActionRecord` reads back with an empty `status.phase`. The premise was true when written and
`304c1d5` deliberately made it false: 06 §4.3 makes `status.phase` authoritative and the label a
derived index, so `journal.Store.Create` now follows itself with a `Status().Update`. The test went
red at that commit. It was **discovered** twenty commits later, on PR #83's first CI run, as a
BLOCKING-ALWAYS failure against **V-BRK-023** — the write-ahead confirmation, which is the last
thing between a parked record and a live mutation.

**Why nothing local caught it.** `writeahead_envtest_test.go` gates every test on `requireEnv(t)`,
which calls `t.Skip` when `KUBEBUILDER_ASSETS` is unset. A skip is not a failure. `go test ./...`
prints `ok github.com/.../internal/broker/writeahead` and exits 0 over a test that is red, and does
it identically for the **fifteen** `*_envtest_test.go` files in this tree. Only
`make -C k8s-operator test` runs them, because only the make target has `setup-envtest` as a
prerequisite and exports the resolved path.

**Why this is worth its own row rather than a note on [[lsn-052]].** LSN-052 is the same shape one
level out — the Go suite runs once per phase, so a golden was red for three commits — and its
**preferred mechanization is `cd k8s-operator && go test ./...` in `binding.md` §Build**. That
command, adopted exactly as written, is green on this failure. The lesson is not "run the tests";
it is that the command a mechanization names has to be the one that runs the tests, and the two
differ here by an environment variable that no reader of the diff would notice. A mechanization that
would not have caught the escape that motivated it is [[lsn-019]] arriving inside the fix for
[[lsn-052]].

There is a second, sharper reading. The skip is not a bug — envtest assets are a heavy dependency
and skipping without them is why `go test ./...` is usable at all. What is wrong is that the
**default is quiet**. A suite that cannot run says so in a line nobody reads, in a run whose exit
code says everything is fine, and the cost is paid by whoever reads the CI failure three weeks
later.

**What the test itself did right, and is worth copying.** It asserted a *premise* rather than
trusting one, and its failure message named the remedy. That is why the fix took one reading of the
output instead of a bisect: the check had already written down what its own falsification would
mean. A test that encodes "this is what the platform does, and here is what to do if it stops" is
strictly better than a comment saying the same thing, because the comment cannot go red.

**Mechanization candidates**, for the next improvement pass:

1. _(preferred)_ `binding.md` §Build names **`make -C k8s-operator test`** for any unit touching
   `k8s-operator/**` — never bare `go test ./...`. This supersedes LSN-052 candidate 1 rather than
   sitting beside it, and both lessons close on the same edit.
2. Invert the default in `requireEnv`: **fail** when the assets are missing, and skip only when an
   explicit `KUBE_AGENTS_SKIP_ENVTEST=1` is set. Makes "I did not run envtest" a decision somebody
   took rather than a condition nobody observed. Wider blast radius — fifteen files, and it makes
   bare `go test ./...` red for everyone — so it wants pairing with 1, not instead of it.
3. An L0 gate arm that every `*_envtest_test.go` skip-guard is reachable only from a command
   `binding.md` actually names. General, and the most work; it is really a lint for candidate 1
   having been done.

Also worth recording as a hazard for `dev/mutate.py`: a spec whose catcher is an envtest test scores
**ESCAPED** when the assets are unset, and `go test -list` — the guard [[lsn-048]] added precisely so
a missing catcher cannot be read as a survivor — compiles rather than runs, so it lists the test
either way and stays silent. `verification/mutants/V-BRK-023.json` carries that warning as the first
key in the file, which is a comment, not a mechanism. The runner has no way to declare required
environment; giving it one is a candidate in its own right.

Related: [[lsn-052]] (the same blindness one level out, and the candidate this corrects),
[[lsn-019]] (a mechanization off the path the work takes), [[lsn-038]] (a guard that fails safe
still fails, and a green run is how it tells you), [[lsn-001]] (the artifact under test not being
the artifact built).

**Mechanized 2026-07-30 (candidate 1; candidate 2 refused).** Candidate 1 landed jointly with
[[lsn-052]] — see that lesson's closure for the four clauses
`check_envtest_is_run_by_the_command_checkpoint_names` asserts.

Candidate 2, inverting `requireEnv` to fail unless an opt-out is set, was **refused**. It reddens
every contributor's `go test ./...` to fix a rule about how the harness checkpoints, and the opt-out
variable simply becomes the new silent skip: `KUBE_AGENTS_SKIP_ENVTEST=1` in a shell profile is
indistinguishable, at the console, from the state this lesson is about.

**One level down, and this is the part that generalizes.** `dev/mutate.py` gains rule 7,
`suite.requires_env`: a spec declares the environment its suite needs and the sweep refuses to start
without it, BROKEN rather than nineteen verdicts. Rule 5 could not cover this — it checks catchers
against `go test -list`, which **compiles rather than runs**, so a catcher inside a skipping file is
listed exactly as a running one is. The guard that should have noticed was itself blind, and six of
nineteen mutants would have been reported as real holes in a suite that catches them cleanly.
`check_mutation_specs_declare_required_env` derives the requirement from the tree rather than from a
list, and on its first run it found a fifth spec — `V-REV-003` — that the hand-built list had missed.

## LSN-055 — Twenty-five commits, one push, one CI run, and the red belongs to none of the recent ones

**What happened.** The `phase-9-a-real-caller-at-the-door` branch accumulated twenty-five CHECKPOINT
commits and was pushed once, at the end. `k8s-operator-test.yml` triggers on pull requests to `main`,
so the suite ran for the first time on PR #83 — against all twenty-five at once. It was red, and the
commit responsible was `304c1d5`, twenty back. See [[lsn-054]] for the specific test.

**Why it matters beyond one bisect.** CHECKPOINT's product is *attribution*: one commit, one set of
checks, one claim about what is true after it. A branch pushed once at its end converts that into a
phase-end audit. Every check that only runs remotely is answered once, for a range, and the range is
the whole phase — so a failure carries no information about which unit caused it, and the harness
learns about it at the moment its context for the responsible unit is coldest. The cost is not the
`git bisect`; it is that twenty units were each declared complete against evidence that had not yet
been produced.

**Why this is not just [[lsn-052]] again.** LSN-052's candidate 2 is a `push` trigger on every
branch, and it is necessary — but it is not sufficient, and this row exists to say why. A push
trigger fires per push. With one push per branch, adding the trigger changes one CI run into one CI
run. The trigger and the push cadence are two separate facts and fixing either alone leaves the
property broken; that is exactly the kind of thing that gets half-mechanized and then believed.

**The counter-argument, recorded because it is real.** Pushing per commit means CI runs on
work-in-progress, burns minutes on commits nobody will ever look at, and produces red runs that are
expected — which is its own way of teaching people to ignore CI. The answer is probably that CI red
on a pushed branch is *already* how this harness is supposed to learn, since it has no other remote
signal, and that "expected red" is a symptom of the local chain being weaker than the remote one
rather than an argument for pushing less. That is a judgement for the improvement pass, not a
conclusion here.

**Mechanization candidates**, for the next improvement pass:

1. _(preferred)_ `binding.md` §Branching gains "push after every CHECKPOINT commit", **paired with**
   LSN-052 candidate 2 (a `push` trigger on `k8s-operator-test.yml` for non-`main` branches). Both
   halves or neither.
2. Push at every CHECKPOINT but keep the PR-only trigger, relying on the local chain for
   attribution. Cheaper, and it makes the push a durability measure ([[lsn-043]]) rather than a
   verification one — which is worth something on its own, but does not fix this.
3. Nothing, on the argument that [[lsn-054]] candidate 1 moves the missing check local, so remote
   cadence stops mattering. Tempting and probably wrong: it is only true for the checks that *can*
   run locally, and the ones that cannot are the ones this row is about.

Related: [[lsn-052]] (the trigger half of the same property), [[lsn-054]] (the specific escape that
exposed it), [[lsn-043]] (a branch's work is not durable until it is pushed).

**Mechanized 2026-07-30 (candidate 1, both halves).** `binding.md` §Branching gains a **Push
cadence** row — `git push origin HEAD` at every CHECKPOINT, not once per phase — `harness-run` §6
condition 4 names it, and `.github/workflows/k8s-operator-test.yml` drops the `branches: [main]`
filter from its `push` trigger. `check_checkpoint_commits_reach_ci` asserts the **pair**, which is
the only reason it is a check rather than a note: a cadence pushing into a filtered trigger and a
wide trigger nothing pushes to are both still exactly one CI run, so either half can be removed on
the entirely reasonable-sounding grounds that the other one covers it.

The cadence arm reads a table ROW rather than the §Branching body, and that correction came from its
own negative control. A body-wide search stayed green after the rule was deleted, because the prose
explaining the rule contains the same words — "25 CHECKPOINT commits and one push". The check was
pinning its own rationale.

---

## LSN-056 — The ratchet was wound at 194 tests, the suite reached 1290, and it ticked all the way

**Area:** harness, checks, ratchets, false-green, V-MET-003
**Status:** closed — mechanized 2026-07-30, in the pass that found it
**Earned:** 2026-07-30, improvement pass after the V-BRK-023 halt

**What happened.** The improvement pass closing [[lsn-051]]–[[lsn-055]] added fifteen tests, so it
ran `python3 dev/tests/invariants-gate.py --update-baseline` as invariant 8 requires. The diff was
1303 lines. The committed baseline held **194** named tests across **34** files; the tree holds
**1290** across **137**.

**Why nothing noticed.** `check_assertion_ratchet` asks one question — *did a test that was in the
baseline disappear?* — and that question is answerable, and answered correctly, by a baseline
covering any subset of the corpus. Every run for months printed `✓ invariant 8 / V-MET-003 —
assertion ratchet` while 85% of the suite sat outside the ratchet entirely. Deleting any of those
1096 tests would have been green.

This is [[lsn-035]] and [[lsn-038]]'s shape again — silent in the safe direction — with one extra
turn of the screw: the safe direction *narrowed over time*. A stale probe list is wrong on the day
it is written. A stale ratchet gets more wrong every time the project succeeds at anything.

**The instruction already existed.** The baseline's own `_comment` says _"Regenerate ... ONLY when
adding tests"_. It has said so since the file was created. That is precisely [[lsn-019]]: a
mechanization written as prose, on the artifact, off the path the work takes. Nobody reads a
generated JSON file's comment field while adding a test.

**Mechanized in the same pass** — the pass that found it is the pass that owed the check, and
`harness-improve` §3.2 says an escape is closed by strengthening the check that should have caught
it. `check_the_ratchet_baseline_covers_the_corpus` fails when any named test in the tree is absent
from both the baseline and `retired`, and its message is the command that fixes it.

Names rather than files, deliberately. A file-level rule would have caught this instance and left
the smaller version of it live: one baselined file growing from two tests to fifty, with
forty-eight unguarded. The cost is one command in any unit that adds a test, which is the discipline
the `_comment` was asking for and could not enforce.

**A limit, recorded rather than discovered later.** The extractor keys names per file, so two tests
with the same name in different classes of one file collapse to a single entry and deleting one is
invisible to the ratchet. Writing `test_green_on_the_tree_as_it_stands` in a sixth class of
`dev/test_invariants_gate.py` is what surfaced it: the corpus went up by four, not five. Left
as-is — the fix is in `inventory()`, which is not this pass's business — but written down here so
the next person meets it as a known limit and not as a fresh mystery.

Related: [[lsn-019]] (a mechanization off the path the work takes), [[lsn-035]] and [[lsn-038]]
(a check silent in the safe direction), [[lsn-054]] (a guard that was itself blind to the thing it
guarded against).

---

## LSN-057 — The corpus was discovered by mention, so the check that talked about the convention was scored as breaking it

**Tags:** harness, checks, corpus-discovery, false-positive, LSN-035
**Opened:** 2026-07-30 (improvement pass, running the L0 chain over the pass's own changes)
**Status:** closed — mechanized in the same pass

**What happened.** The full L0 chain came back with exactly one `FAIL:`, and it was
`dev/tests/negative-controls-name-their-rule.py` reporting that `dev/tests/invariants-gate.py`
_"advertises `--negative-control` but defines no `negative_control()`"_. The sentence is false about
that file. `invariants-gate.py` accepts no such flag; passing it changes nothing. What it had just
grown, in this very pass, is `check_a_check_only_unit_exhibits_both_trees` — [[LSN-053]]'s
mechanization — whose job is to read the declared chains and assert that **some line runs a check
with `--negative-control`**. The string is in the file because the file is a rule _about_ the
convention. The corpus was `"--negative-control" in p.read_text()`, so being about it and
participating in it were the same thing.

**Why it is worth a lesson and not a one-line fix.** Three reasons.

First, it is [[LSN-036]] one node over. There the defect was a uniqueness lint that went red on
correct code and whose one-line green was an allowlist entry; the rule extracted was _derive the
corpus from structure, not from a list_. Here the corpus was derived from a **substring**, which is
the same mistake with the list generated automatically — and the tempting one-line green was to
spell the literal differently in `invariants-gate.py` (`"--negative" + "-control"`), which leaves the
mine armed and buried for whoever writes the next check about the convention.

Second, **the file had already won this argument in its other half.** Its docstring carries a
paragraph headed WHY BEHAVIOURAL AND NOT STRUCTURAL, arguing that scoring a control by grepping for
a three-element mutation tuple checks today's code shape rather than the property, and would have
condemned the one file that got it right. Every word applies to discovery, and discovery was left as
a grep. A file can hold the correct general principle, stated at length, and apply it to only the
half of itself that was under discussion when it was written.

Third, the fix **found a property the substring version could not state.** Splitting "mentions the
flag" into "dispatches on it" and "offers it in a usage line" makes a third case visible: a docstring
that promises `python3 dev/tests/foo.py --negative-control` where nothing in `foo.py` reads argv. The
documented command then runs the ordinary check and prints its ordinary PASS, and the reader who
followed the docs records that as the control passing. That is the same family as the lesson the file
exists for — a green produced by not asking — and no file in the tree has it today, which is exactly
when a guard is cheap to add.

**The first fix was still a substring match, by a longer route.** `"--negative-control"\s+in\s+\w`
matched `if "--negative-control" in body:` — a check testing for the flag inside another file's
text — so the searcher was swept in again. The operand is the load-bearing part: the membership test
has to be against the **command line** (`argv`, `args`, `sys.argv`), not against any string that
happens to be in scope. The control caught it on the first run, which is the argument for writing the
control before believing the fix.

**Mechanization.** `controls()` takes a root (so the self-test can point it at a synthesized tree),
discovery is `DISPATCHES.search(text) or _promises(path, text)`, and `score()` opens with the
promise-without-dispatch arm. Corpus 12 → 11, all 11 discriminating; `MIN_CONTROLS = 9` unchanged and
still the non-vacuity floor. Two new cases in the file's own `--negative-control`: a file that only
searches other files for the flag must stay **out** of the corpus, and a file that promises it
without implementing it must come **in** and score as a finding.

**A note on what was not done.** `invariants-gate.py` genuinely does have negative controls — 59 of
them, as unittest classes in `dev/test_invariants_gate.py`, run by the L0 chain. It has never used
the `--negative-control` convention and does not need to; a gate with 29 arms would need a control
entry point per arm, which is what a test module is. The finding was not "this file is missing a
control", it was "this file was never a member of that corpus".

---

## LSN-058 — The two commands CHECKPOINT now requires cannot be run at the same time

**Tags:** harness, tooling, false-red, concurrency, go
**Opened:** 2026-07-30 (improvement pass, running the pass's own gates)
**Status:** closed — mechanized in the same pass

**What happened.** This pass made `make -C k8s-operator test` compulsory at CHECKPOINT alongside the
L0 chain ([[LSN-052]], [[LSN-054]]). The chain takes minutes and the operator suite takes minutes, so
the first thing anyone does with that pairing is run them concurrently. The operator suite then died
in its very first target:

```
/Users/…/k8s-operator/tmp109n_mmw/main.go:1: no such file or directory   (× 5)
Error: not all generators ran successfully
make: *** [manifests] Error 1
```

`tmp109n_mmw` is a `tempfile.TemporaryDirectory` created by a python test — five suites make them
**inside** `k8s-operator/`, because the code under test needs a real path in that tree. `controller-gen`
runs as `paths="./..."`, walks in, and the directory is removed underneath it before it finishes
reading.

**Why it is a lesson and not a footnote.** A red that names a file nobody wrote, points at no defect,
and disappears on a serial re-run is the most expensive kind. It costs a diagnosis at the exact
moment a unit is trying to close, and what it teaches is *re-run it* — a habit that is
indistinguishable from the habit of re-running a genuinely flaky failure until it goes away. The
cost also lands on a rule this same pass had just written: the reader who obeys "run both at
CHECKPOINT" is the reader who meets it.

**The tempting fix was a sentence, and a sentence would not have held.** "Do not parallelize these
two" in `binding.md` is [[LSN-019]]'s shape — prose on the artifact is not a mechanization — and the
next person to reach for `&` will not have read it, particularly since the two commands are named in
different sections.

**Mechanization.** `prefix="."` on all five `TemporaryDirectory(dir=…)` calls inside the module. Go's
`./...` skips directories whose names begin with `.` or `_`, so `controller-gen`, `go build` and
`go vet` never enter them; `pathlib.rglob` has no such rule, so the suites that need the directory to
be *visible* — `gate.discover_machinery`, `gate._invoked_by` — still see it. The two suites that
compile a scratch `main.go` pass the file by explicit path, so they never needed `./...` to reach it
in the first place. Each site carries a comment saying why the prefix is load-bearing, because a
lone `prefix="."` reads like a naming preference and the next refactor deletes it.

**Verified the way the defect was produced**, not the way it is described: the two commands were run
concurrently again, and returned `GO=0 PYTHON=0`.

---

## LSN-059 — The suite was green, the machine was dying, and the harness was the thing killing it

**Tags:** harness, tooling, resource-leak, envtest, self-reinforcing
**Opened:** 2026-07-30 (BACKLOG B-004, reported by the human)
**Status:** closed — mechanized in the same unit

**What happened.** A human looked at the process table on the dev laptop with no test run in flight
and found **sixteen `etcd` and sixteen `kube-apiserver`** processes, all of them executing out of
`k8s-operator/bin/k8s/`, thirty of them at `ppid=1`, holding **1375 MB** on a 16 GB machine. The
oldest was about thirty-one hours old. They had arrived in cohorts, and the cohorts matched test
runs.

`sigs.k8s.io/controller-runtime/pkg/envtest` starts a real control plane — one etcd, one
kube-apiserver — per **test binary**, and every one of the fourteen `*_envtest_test.go` packages
stops its own in `TestMain`, after `m.Run()` returns:

```go
code := m.Run()
if testEnv != nil { _ = testEnv.Stop() }
os.Exit(code)
```

A `SIGKILL` does not reach that line. `go test` dies, the control planes do not, launchd (or init)
adopts them, and nothing on the machine has ever reaped them.

**Why this is a harness defect and not only a test defect.** The harness is the thing sending the
signal. `make -C k8s-operator test` measures **2m09s** on this machine with a warm build cache —
`internal/controller` alone is 116s — against a caller whose default time bound is **two minutes**.
So the command is killed a few seconds from finishing, on a run that was about to pass, and the kill
is not an accident that happens occasionally: it is what happens every time, by construction. Then
the loop closes. Each kill leaves a cohort. Each cohort makes the machine slower. A slower machine
is likelier to exceed the same bound. This one runs in the wrong direction on its own, and
[[LSN-058]] is the standing proof that this class of background interference does not stay quiet —
it produced a red naming a file nobody wrote, at the exact moment a unit was trying to close.

**The symptom is the absence of a symptom.** No exit code mentions this. Every suite is green; the
tests that leaked are the tests that passed. Nothing in the tree is wrong. The only observable is
that the machine gets slower, which reads as the machine getting older. That is why it took a human
running `ps` to find it, and why it had been running for at least thirty-one hours before anyone
did. A defect whose entire signature is "everything is fine, but slower" cannot be caught by any
verdict the harness already collects — so the fix cannot be a verdict, it has to be a sweep.

**You cannot trap a SIGKILL, so the fix cannot live in teardown.** It has to run at the START of the
next run: that is the one moment guaranteed to occur after an abandoned cohort exists, whatever
killed the previous one. Hence `dev/reap-envtest.sh` as a **prerequisite** of the `test` target, with
the `trap … EXIT INT TERM` as a second, weaker line — the trap covers Ctrl-C and an ordinary
failure, which is everything except the death that actually causes this.

**Two properties make it safe to run automatically on every test invocation**, and both had to be
designed in rather than added afterwards, because a reaper wired into `make test` is a program that
kills processes on a developer's machine without being asked:

1. **Discovery is anchored at the left edge of an absolute path.** A process is in scope iff argv[0]
   _begins with_ the envtest asset root. This is [[LSN-005]]'s rule — the destructive guard that
   matched a context name by substring and would have accepted `prod-scratchpad` — applied to a
   process instead of a cluster. `pgrep etcd` would reap the etcd somebody is running for real work.
   It is also `ps -eo args=` and not `comm`, because on Linux `comm` is a 15-character truncated
   basename and the path this whole thing matches on would not be present at all.
2. **Only orphans are reaped.** The default predicate is `ppid == 1`, which is precisely and only the
   leak: a control plane with a live parent is somebody's test run in flight — including a
   **concurrent `make test` in another terminal**. Without this, wiring the sweep into `test` makes
   two simultaneous runs kill each other, which is a fix whose failure mode is worse than the leak.

`--all` drops the second property, is deliberately not what the Makefile passes, and announces
itself before acting.

**This is a case where the substring bug was not hypothetical.** While proving the reaper worked, a
hand-rolled `ps | grep "k8s-operator/bin/k8s/.*kube-apiserver"` written to count the processes
matched **its own shell's argv**, which contained the pattern as a literal. The count came back
nonzero when nothing was running, the wait loop exited immediately, and it killed a live `make` in
the middle of `controller-gen`. Twenty minutes after writing property 1 down. `dev/test_reap_envtest.py`
carries that exact shape as a test: a process whose argv merely _mentions_ the asset root must
survive `--all`.

**Mechanization.**

- `dev/reap-envtest.sh` — the sweep, both safety properties, `--list` as a non-destructive probe
  (exit 1 if orphans exist), and a refusal for any asset root that is relative or has fewer than
  three path components. `--dir /` is not interpreted, it is refused: a prefix match is only as safe
  as its prefix.
- `k8s-operator/Makefile` — `reap-envtest` as a prerequisite of `test`, plus the trap in the recipe.
- `dev/tests/invariants-gate.py` `check_envtest_control_planes_are_reaped` (on `dev/L0-CHAIN.txt`) —
  the reaper exists, it is a prerequisite and not only a trap, the trap is there too, the target
  actually runs the script, and the `ppid == 1` predicate and the left-edge anchor are both still in
  it. Each of those can be deleted leaving a tree where `make test` still runs and still passes,
  which is the whole reason the check enumerates them separately.
- `dev/test_reap_envtest.py` (`python3 -m unittest discover dev`, on `dev/L0-CHAIN.txt`) — 18
  behavioural tests against **real** processes with synthesised argv[0]s, never a stubbed `ps`. Both
  safety properties are tested in both directions: a parented control plane survives the default
  sweep and dies under `--all`; a sibling root sharing a name prefix, a process outside the root, and
  a process merely mentioning the root all survive `--all`.

**The caller's timeout is not mechanizable from inside this repository — an argued refusal.** The
other half of this defect is that the harness kills the command at all, and the time bound is a
property of the runtime invoking the harness, not of the tree. There is no file to assert about: a
check that could see it would have to observe an invocation, and by then the cohort exists. What
_can_ be held is the number, so `binding.md` §Build now carries the measurement and the requirement
(≥ 5 minutes, measured 2m09s warm, cold is worse), and the gate's fifth arm asserts that warning is
still there. This is deliberately **not** counted as closing the caller half. The reaper is what
closes the lesson: it bounds accumulation at one run's worth however the previous run died, which
holds even if every future invocation is killed. The §Build note only stops that one run's worth
being manufactured on purpose, every time, forever.

**Verified the way the defect was produced.** The leak was reproduced deliberately (the machine had
rebooted between the report and the fix, taking the evidence with it): a `go test` on a single
envtest package was SIGKILLed, and its etcd reparented from 7170 to 1. Then, with three orphans
holding ~304 MB **and** a live `go test ./internal/broker/execute/` in flight, the default sweep
killed exactly the three orphans and left the live pair alone — the live test finished `ok … 21.6s`.
Finally, end to end: two orphans created, `make -C k8s-operator test` run, whose first line was
`reap-envtest: reaped 2 envtest process(es) (~263 MB); 1 needed SIGKILL after 5s.`, the suite green,
the exit trap reporting nothing left to reap, and `--list` afterwards returning 0.

## LSN-060 — The negative control skipped the statement under test, so 13/13 measured nothing

**Tags:** verification, negative-control, l2, self-concealing
**Opened:** 2026-07-31 (P9-T9b-5b-0-iii)
**Status:** closed — mechanized in the same unit

**What happened.** `dev/verify/broker-execute-l2.sh`'s L2-1 arm asks the API server for the
ActionRecord the broker just created, then hands the document to a shared assertion block that L2-2
through L2-5 read. It looked the record up by the **raw action id**. Object names are
`journal.RecordName(actionID)` = `"ar-" + strings.ToLower(actionID)` — 06 §4.3, lowercased because a
name must be a DNS subdomain and a ULID is uppercase. That lookup could not have found a record
against **any** commit in the history of the file.

It had never been reported, because the only thing that had ever exercised the arm was
`--negative-control`. The control **synthesises** the record document — thirteen mutated JSON
documents, each fed straight to the assertion block — and never touches the lookup. So the suite had
a green 13/13 for an arm whose live statement had not once executed, and the first live run reported
`no such ActionRecord exists` **directly above a listing that showed the record**.

**The general shape.** A negative control proves the assertion block is not always-green. It proves
nothing about any statement it bypasses in order to inject its input, and the statements it bypasses
are exactly the ones that acquire the thing being asserted about: the API call, the parse, the
lookup. Those are also, in an L2 suite, where the interesting failures live — a synthesised document
is by construction well-formed and correctly named.

**An arm whose ¬ form skips the statement under test is an arm nothing has measured**, and it reads
in the output exactly like an arm that passed.

**Mechanization.** `dev/tests/invariants-gate.py` →
`check_negative_controls_exercise_the_statement_under_test` (L0-CHAIN). It fails any suite whose
`--negative-control` block constructs an input the live path obtains from the cluster, unless the
arm carries a comment naming the statement it is deliberately skipping and why. The L2-1 lookup now
derives `record_name` at the call site — derived rather than read off the broker's reply, because
the reply is the broker's claim about where it wrote and the point of the arm is to check it — and
the live run's PASS line names `ar-01kyveh3ngd099sz5dpntwfbyz`.

**Related:** [[LSN-048]] (a `-run` pattern that matched nothing scored three mutants as survivors) —
same family: the measurement apparatus reporting on work it never did.

## LSN-061 — `status` is a subresource, and Create put back one field of the block it knew was dropped

**Tags:** go, kubernetes, api-semantics, subresource, silent-loss
**Opened:** 2026-07-31 (P9-T9b-5b-0-iii)
**Status:** closed — mechanized in the same unit

**What happened.** Three defects, one root, found in sequence over a single unit.

First: `status.timestamps` had been a declared CRD field since Phase 5 with **three readers and no
writer**. `budget.go`'s window, `cooldown.go`'s `(verified, submitted)` pair and
`JournalReconciler.exportLateness`'s four-way fallback all degrade *silently* when it is absent, so
nothing ever went red. V-BRK-006's L2 clause — the write-ahead record is durable **before** the
mutation — had no `executionStarted` to compare `metadata.creationTimestamp` against, so the one
ordering the rule exists to establish was unobservable in production.

Second: `journal.SetPhase` re-reads the record into `live` and writes **that**, so everything the
pipeline had composed on its own copy was discarded — `status.applied` from step 9,
`status.verification` and `status.recovery` from step 11. A live record read back with a phase, a
message and nothing else: an audit trail that said an action had happened and could not say what it
did.

Third, and the one that bit hardest: `status` is a **subresource**. The API server keeps spec and
metadata from a Create and discards the whole status block. `Store.Create` had known that for five
phases — there is a paragraph of comment above the line where it re-writes `status.phase` afterwards
— and it put back exactly **one field** of the block it knew had been dropped. The birth beats
(`submitted`, `classified`) therefore never became durable on the write-ahead record, which is
precisely the artifact whose durability V-BRK-006 is about. And the Create's reply body **overwrote
the caller's `ar.Status`**, so the pipeline reached step 8 holding a nil `status.timestamps` and
stamped straight through it. The broker **panicked on `gke-scratch-kube-agents-dev`** at the one
moment a failure is unrecoverable: the journal already said `Executing`, nothing had executed, and
no code path will ever advance that record.

**What generalises.**

- **A field the API server drops is dropped in full.** Restoring the field you happened to notice
  reads, in code review, exactly like handling the drop. The comment above the fix was *correct* and
  the fix under it was one-sixth of the job.
- **A degraded reader is not a failing reader.** Four-way fallbacks and zero-value windows are how a
  never-written field survives five phases with a green suite. Every fallback is a place a missing
  writer can hide.
- **Fail closed means refuse, not die.** A nil dereference between "the journal is durable" and
  "the executor ran" is strictly worse than the missing datum it crashes about.
- **A fix and its overcorrection are the same unit of work.** Copying the caller's whole status back
  would have blanked `approvals`, `contested` and `undoneBy` — three fields 06 §4.3 hands to *other*
  principals — letting a phase change silently reverse a human approval. The mutation sweep's M8
  exists because the obvious repair for M7 is the more dangerous bug.

**Mechanization.** `mergeOwnedStatus` in `k8s-operator/internal/journal/store.go` carries exactly the
six fields 06 §4.3 assigns to the owning broker SA, nil-guarded per field, and is used by **both**
`Create` (snapshotted *before* the call, since the reply overwrites) and `SetPhase`. `state.clock()`
in the pipeline creates the block if absent so the stamping cannot dereference nil. The journal
package's fake client has modelled the subresource drop since it was written
(`dropStatusLikeTheApiServer`) — that is why the journal tests could catch it; the pipeline's fake
store now models it too, behind `dropStatusOnCreate`, defaulting off so the default fake remains the
store's real contract. `verification/mutants/V-BRK-006.json`, **15/15 caught**: M7 (SetPhase drops
the outcome), M8 (the wholesale overcorrection), M9 (the unguarded per-field copy, which **escaped**
the first sweep), M13 (Create restores only the phase — the tree as it shipped), M14 (the snapshot
taken one line too late, which restores an empty block and looks exactly like a working restore),
M15 (the panic itself).

**Related:** [[LSN-052]] / [[LSN-054]] (a green package that ran nothing) — the same failure mode one
layer up: the apparatus reporting success for work that did not happen.

---

## LSN-062 — "That file does not exist" was written about a file that does

**Tags:** records, verification, orient
**Opened:** 2026-07-31 (P9-T11g-2a → caught at P9-T11g-2b's ORIENT)
**Status:** open — mechanization proposed, deferred to the next improvement pass under Guardrail 9

**What happened.** `P9-T11g-2` was scheduled as _"one tool over `verification/traceability.yaml`, not
four"_. Splitting it, the unit published — in a phase-file row, a ledger cell, an outcome section and
a commit message — that `verification/traceability.yaml` **does not exist**. It has existed since
`P8-T10` (`ead358e`): 71 KB, 177 entries, V-MET-011's artifact, green on the L0 chain every run.

**How a false claim survived a unit that was otherwise careful.** The question the unit actually
worked on was _"does 09 §8's `R-<doc>.<section>-<n>` requirement→check mapping exist?"_, and the
answer to that is a correct and well-evidenced **no** — a repo-wide search returns two hits, both
sentences in §8 that _define_ the scheme. The unit then wrote that answer down **against the filename
§8 happens to use for the mapping**. Every subsequent sentence was consistent, because the reasoning
was about the property and only the label was wrong.

**The general shape.** _An absence is asserted about a NAME, but established about a PROPERTY._ The
two coincide often enough that the substitution is invisible: here the file's existence and the
requirement space's existence really are different facts, and only one was checked. `ls` was never
run. The correction cost nothing and the claim cost a commit message that cannot be edited.

Note the near-miss on the other side. Had the unit believed the file existed and been wrong, the
error would have surfaced the moment something tried to read it. **Claims of absence have no such
backstop** — nothing fails when you decline to open a file — so they are the direction that needs a
check rather than a habit.

**Proposed mechanization** (improvement pass; a check change, not this unit's): an L0 lint over
`docs/build/*.md`, `.claude/harness/*.md` and the header comments of `verification/*.yaml` that finds
a repo-relative path asserted absent — `does not exist`, `is absent`, `is not in the tree`,
`nothing at` — within a sentence of a backticked path, and fails when the path is present in the
tree. Struck-through text (`~~…~~`) is exempt, since a corrected claim is how the record is supposed
to look. Cheap to write, and it polices exactly the class of statement that schedules work.

**Related:** [[LSN-019]] (prose on the artifact is not a mechanization) — the same gap between what a
document says and what is true, in the opposite direction.

---

## LSN-063 — A negative-control mutation that stopped applying reported MISS, not BROKEN

**Tags:** checks, verification, mutation-testing
**Opened:** 2026-07-31 (P9-T11g-2b-ii-2a)
**Status:** open — guarded in the one harness that failed; the propagation is an improvement pass

**What happened.** `P9-T11g-2b-ii-2a` curated 222 of document 06's requirements into
`verification/requirements.yaml`, which moved `verification/coverage.yaml`'s `totals.covered` from
`0` to `222`. V-MET-009's negative control then went red on one row: _"a requirement is marked
covered without a check"_. The mutation was
`t.replace("  covered: 0", "  covered: 12", 1)` — a literal keyed to the value the tree happened to
hold on the day the control was written. With no `covered: 0` left to find, the replace returned the
string unchanged, the control re-ran the check against the **unmutated** tree, the check correctly
found nothing, and the row printed **`MISS`**.

**Why that is worse than a red.** `MISS` means _the check let the defect through_ — a finding about
the check under test, and the thing `harness-run` §5 says to fix. What actually happened is that no
defect was ever applied: the sweep could not evaluate the mutant, which is **`BROKEN`**, and
[[LSN-048]] and [[LSN-049]] both say in as many words that a `BROKEN` row is not a finding. The two
verdicts point at opposite repairs. `MISS` invites strengthening the check — which here would have
produced a check that passes on the first run, looks exactly like the fix, and leaves the mutant
still unmeasured, which is [[LSN-048]] arriving a third time through a different door.

**The general shape.** _A mutation keyed to a literal from the tree is a mutation with an expiry
date, and it expires silently._ Every value a control hardcodes — a count, an ID, a date, a first
list entry — is a bet that the tree will not move past it, and the whole point of a ratchet is that
the tree moves. The control cannot tell the difference between "the defect was injected and the
check missed it" and "nothing was injected" unless it is made to look.

**Mechanized here.** `dev/tests/requirements-are-enumerated.py`'s control loop now compares each
mutated input tuple against the base and prints `BROKEN` — counted as a failure, with its own
message — when they are equal. The offending mutation was also rewritten to increment whatever
`covered:` it finds rather than match a literal `0`, so it cannot expire again. Both were exhibited:
the repaired row is `caught`, and a deliberately no-op probe row prints `BROKEN  … the mutation did
not change its input; nothing was evaluated`.

**It recurred in the next unit, in the sibling file.** `P9-T11g-2b-ii-2b` curated the remaining
worklist, which moved the per-document floors — and V-MET-008's control went `10/11` with
`MISS  the floor is raised above the coverage under it`. That mutation was
`t.replace('  "02":\n    covered: 0', ..., 1)`: the same shape, the same literal `0`, expired by the
same edit, one file over. It now carries both repairs (increment-what-you-find, and the `BROKEN`
guard, probed). **Two of twenty-one loops are guarded; eighteen are not**, and the lesson has now
been paid for twice inside two units — which is the argument for the shared helper rather than for
repairing the third one when it fires.

**And a third time, one unit later, in a form the guard does not catch.**
`P9-T11g-2b-ii-2c-ii-a` added a paragraph to `verification/requirements.yaml`'s curation header —
seven lines — and V-MET-002's control went red on _"the enumeration artifact is truncated"_, whose
mutation was `"\n".join(t.splitlines()[:400])`. The cut had been a clean entry boundary on the day it
was written; with the header a different length it landed **mid-entry**, and the mutant tripped the
parser (`R-02.2.5.5-8 has no text:`) instead of the floor it was written to trip. This one **did**
change its input, so the `args == base` guard passed it through: the failure is not an unapplied
mutation but an applied one that **stopped meaning what it said**. The needle caught it — the row
demanded `VACUOUS: the enumeration yielded` and got a parse error — which is [[LSN-035]]'s
name-the-rule requirement doing the work the equality guard could not. Repaired by anchoring the cut
on the key regex (`t[: [m.start() for m in re.finditer(r'^"R-', t, re.M)][5]]`) so truncation stays
truncation however the header moves. **The scope is therefore wider than the guard:** a literal line
number, offset or slice index is the same expiry-dated bet as a literal count, and the mechanization
below has to cover both — the guard for mutations that stop applying, and the per-row needle
([[LSN-035]]) for mutations that still apply and now mean something else.

**Proposed mechanization** (improvement pass; a check change across 18 files, so not this unit):
eighteen other harnesses carry a `negative_control()` loop and none of them has the guard.
`dev/tests/negative-controls-name-their-rule.py` is already the meta-lint over controls and is the
place for it — either it requires the guard in each loop, or the loop moves to one shared helper the
meta-lint can point at. The second is better: a guard eighteen files each implement is eighteen
chances to implement it wrong.

**Related:** [[LSN-048]] (a `-run` pattern that matched nothing scored three unevaluated mutants as
survivors) · [[LSN-049]] (an applier that died and its `0` was read as the suite passing) ·
[[LSN-035]] (a check that goes quiet reads identically to a passing one).
