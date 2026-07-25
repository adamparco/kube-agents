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

---

## Index

| ID          | Tag                    | You will notice…                                                                        | Status   | Closed by                                       |
| ----------- | ---------------------- | --------------------------------------------------------------------------------------- | -------- | ----------------------------------------------- |
| **LSN-001** | images, live-verify    | A live check passes that the source says must fail (or vice versa); unit tests are fine | closed   | `binding.md` P1/P8 · 09 §9.3.1 · V-CMP-002      |
| **LSN-002** | admission, live-verify | A policy is applied, the offending object still runs, and nothing complains             | closed   | `binding.md` P3 · 09 §9.3.3                     |
| **LSN-003** | config, checks         | The check reads the value you wrote; the pod behaves as if you never wrote it           | **open** | `binding.md` P6 states it; no lint detects it   |
| **LSN-004** | rbac, policy-design    | A policy "blocks all writes" and the escape uses a verb that is not a write             | closed   | V-CTN-012 · V-CTR-004 · 09 §11.4                |
| **LSN-005** | destructive-tests      | A guard "only runs on test clusters" and the context name merely _contains_ a keyword   | closed   | `binding.md` P5 · 09 §9.3.5 · `invariants.md`   |
| **LSN-006** | netpol, levels         | Every NetworkPolicy assertion is green and traffic flows anyway                         | closed   | `binding.md` P4 · 09 §3, §9.3.4                 |
| **LSN-007** | completeness, wiring   | Every unit test passes and the feature does nothing in a real install                   | closed   | V-CMP-001 · V-CMP-004 · 09 §5.1                 |
| **LSN-008** | deferrals              | A phase report is all green and a whole class of checks never ran                       | closed   | V-MET-006 · 09 §9.6 · ledger Deferrals table    |
| **LSN-009** | ratchets, refactors    | A refactor lands, the suite is green, and the suite is smaller                          | closed   | V-MET-003 · V-MET-004 · `invariants.md` 8       |
| **LSN-010** | ci, formatting         | Prettier is clean locally and the Prettier Check is red on the PR                       | closed   | `binding.md` §Build · `invariants.md` mechanics |
| **LSN-011** | merge                  | One required check is red and one flag would merge it anyway                            | closed   | `binding.md` §Merge · PROTOCOL §7.4             |
| **LSN-012** | git, remotes           | The diff is enormous, or empty, or the PR shows none of the work                        | closed   | `binding.md` §Branching                         |
| **LSN-013** | version-assertions     | A vanilla cluster is reported as GKE (or the reverse)                                   | closed   | `binding.md` P7                                 |
| **LSN-014** | specs, audits          | Two specs each look correct and the thing they describe cannot be built                 | closed   | 09 §12.1/§12.2 · V-MET-010/011/012/013          |
| **LSN-015** | fixtures, topology     | Everything works with one of a thing and deadlocks with two                             | **open** | Bind to Phase 8 Accept (a) in `phase-8.md`      |

**Open: 2 of 15.** Threshold is 5 (`binding.md` §Thresholds).

---

## LSN-001 — A same-tag image is not evidence of the build under test

`images, live-verify` · **closed** · recurred **three times** (Phase 3, Phase 6, first live install)

| Field             |                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Trigger**       | A namespace-isolation escape was **admitted** on a live cluster while the source and every unit test were correct. Later: chaos-recreated pods never appeared, because a controller predating the Phase-5 hardening rendered pods the hardening VAP correctly rejected. Later still: a live install silently ran the published `ghcr.io/gke-labs` controller even though the operator had been built from local source. |
| **Root cause**    | The deployed image was `:v0.1.0`/`:latest` with `imagePullPolicy: IfNotPresent`. A same-tag image is **not** re-pulled, and the node keeps the old layer until a pod is recreated against a freshly loaded one. In the third instance, `provision_03` called `make deploy` without `IMG`, so `OPERATOR_IMAGE` was ignored outright.                                                                                     |
| **Generalize**    | **A deployed artifact is not evidence of the build under test unless its identity is verified.** Applies to every image, policy, CRD, and rendered manifest — not just this operator. The failure is silent by construction: the old logic under-enforces, so the run reads green.                                                                                                                                      |
| **Mechanization** | `binding.md` §Preconditions **P1** (rebuild → load/push → restart → digest match) and **P8** (provisioning honoured the built image); 09 §9.3.1; check **V-CMP-002**; `invariants.md` repo mechanics.                                                                                                                                                                                                                   |
| **Verify**        | Deploy a deliberately stale image and confirm P1 fails the run **before** any check result is recorded.                                                                                                                                                                                                                                                                                                                 |

> It did **not** close as "remember to rebuild the image". That sentence had already been written
> down twice and forgotten twice.

---

## LSN-002 — A running pod is not evidence a policy works

`admission, live-verify` · **closed**

| Field             |                                                                                                                                                                                                                                                 |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Trigger**       | `kubeagents-system` was labelled Pod Security `enforce: restricted`, the bundled LiteLLM and inference-replay pods had no `securityContext` — and everything stayed Ready. The gap only appeared when a clean cluster refused to schedule them. |
| **Root cause**    | Admission policies (PSA, VAP) evaluate **admission**. They do not evict, re-admit, or re-evaluate objects that already exist. A pre-policy pod grandfathers itself and masks a renderer that emits non-conforming objects.                      |
| **Generalize**    | Never infer enforcement from the state of a running object. An admission property is only observable at the moment of admission — force the recreation, or you are testing the past.                                                            |
| **Mechanization** | `binding.md` §Preconditions **P3**; 09 §9.3.3 / §11.2.                                                                                                                                                                                          |
| **Verify**        | Apply a policy, leave a violating pod running, and confirm the precondition forces recreation before the check is judged.                                                                                                                       |

---

## LSN-003 — The check read a config layer the runtime does not use

`config, checks` · **OPEN**

| Field             |                                                                                                                                                                                                                     |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Trigger**       | Checks asserted against the `config.yaml` baked into the image and passed. The runtime was reading the operator-rendered ConfigMap, which **shadows** it, and which said something different.                       |
| **Root cause**    | Two artifacts hold the same setting and only one is authoritative at runtime. The cheaper one to read is the wrong one.                                                                                             |
| **Generalize**    | A check must name the **runtime-authoritative** artifact and read that. Where a value is rendered, the rendered copy is the truth and the source is an input.                                                       |
| **Mechanization** | **Not yet mechanized.** 09 §11.3 assigns enforcement "per-check", which is a convention, not a mechanism — nothing fails a check that reads the shadowed layer. `binding.md` §Preconditions **P6** states the rule. |
| **Proposed**      | A V-MET lint requiring every L2/L3 check to declare the artifact it reads, and failing any that names an image-baked file where a rendered one exists. Raise at the next improvement pass.                          |

---

## LSN-004 — A deny-list on a security boundary is a finding

`rbac, policy-design` · **closed**

| Field             |                                                                                                                                                                                                                       |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Trigger**       | The VAP read-only ceiling was expressed as a **write-verb deny-list**. It admitted `impersonate` — which is equivalent to cluster-admin. Found by the Phase-0 pre-PR adversarial review, not by the suite.            |
| **Root cause**    | A deny-list is a claim about the complete set of dangerous verbs. That set is open-ended and grows with the API.                                                                                                      |
| **Generalize**    | **Security policies are allow-lists.** Enumerate what is permitted (`verbs ⊆ get/list/watch`) and deny by default. A deny-list on a boundary is wrong even when it currently happens to be complete.                  |
| **Mechanization** | Flipped to a read-verb allow-list (LEDGER Decisions, 2026-07-23); checks **V-CTN-012** (attenuation denied by `vap-agent-scope`) and **V-CTR-004** (template ↔ policy agreement, each mutated copy denied); 09 §11.4. |
| **Verify**        | `local-dev/tests/negative-attenuation.sh` includes the `impersonate` ClusterRole as a standing negative.                                                                                                              |

---

## LSN-005 — Guards match anchored patterns, never substrings

`destructive-tests` · **closed**

| Field             |                                                                                                                                                                                                                       |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Trigger**       | The destructive-test guard matched contexts by substring (`*scratch*`, `*kind*`). A production context named `gke_prod_…_kube-agents-dev-prod` would have satisfied it and been treated as a disposable test cluster. |
| **Root cause**    | Glob substring matching on a name that an attacker — or an ordinary naming convention — controls.                                                                                                                     |
| **Generalize**    | A guard that decides whether destruction is permitted is a security control. Anchor it (`kind-*`, `gke-scratch-*` in a shell `case`), and give the guard **its own negative test**.                                   |
| **Mechanization** | `binding.md` §Preconditions **P5**; 09 §9.3.5 / §11.5; `invariants.md` destructive-test guard; implemented in `local-dev/tests/negative-attenuation.sh` and every `verify-phase*.sh`.                                 |
| **Verify**        | The guard is exercised against three prod-lookalike contexts and refuses all three (LEDGER, Phase 0).                                                                                                                 |

---

## LSN-006 — Well-formed is not enforced

`netpol, levels` · **closed**

| Field             |                                                                                                                                                                                                                            |
| ----------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Trigger**       | Per-tier NetworkPolicies were asserted structurally correct and the suite was green. The cluster ran **kindnet**, which ignores NetworkPolicy entirely — nothing was ever blocked.                                         |
| **Root cause**    | The property is runtime enforcement; the check was a file grep. The level was chosen for speed, not for what it could prove.                                                                                               |
| **Generalize**    | **An enforcement property is proven on an enforcing substrate, or recorded `deferred` — never green from a structural check.** A check's substrate is part of its definition (SELF-IMPROVEMENT §4, "stub the dependency"). |
| **Mechanization** | `binding.md` §Preconditions **P4** and the dedicated `kind-kube-agents-egress` Calico target; 09 §3 level-selection rule, §9.3.4, §11.6; `local-dev/tests/egress-enforcement.sh` defers loudly on a non-enforcing CNI.     |
| **Verify**        | Run the egress suite on the kindnet cluster and confirm it reports `DEFERRED`, not `PASS`.                                                                                                                                 |

---

## LSN-007 — Built, tested, and unreachable

`completeness, wiring` · **closed**

| Field             |                                                                                                                                                                                                       |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Trigger**       | `kage-router`, the event ingress, and the NetworkPolicies all had passing tests. In a live install the router was at 0 replicas, no install path applied the policies, and the ingress had no caller. |
| **Root cause**    | Completeness was measured as "the component exists and its tests pass". Nothing asserted it was reachable from the system it belongs to.                                                              |
| **Generalize**    | **Completeness = exists AND wired AND exercised.** Three probes, all recorded, or the component is not done.                                                                                          |
| **Mechanization** | **V-CMP-001** (all three probes per component ID in 05 §1), **V-CMP-004** (`replicas > 0` in the default install), **V-CMP-003** (no `REPLACE_WITH_*` in a shipped manifest); 09 §5.1, §11.9.         |
| **Verify**        | Park a required Deployment at 0 replicas and confirm V-CMP-004 fails.                                                                                                                                 |

---

## LSN-008 — Deferred read as done

`deferrals` · **closed**

| Field             |                                                                                                                                                                                                                                                                                                                                                                                        |
| ----------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Trigger**       | The scratch-GKE V-G checks were "pending" for five consecutive phases. Only the explicit ⏸ label in the ledger kept them from reading as part of a green phase.                                                                                                                                                                                                                        |
| **Root cause**    | A phase summary reports what ran. A check that never ran contributes nothing to the summary — so absence looks like success.                                                                                                                                                                                                                                                           |
| **Generalize**    | **Deferred is a first-class result**, with a named external blocker, an owner, and a promotion condition. A deferral without an external blocker is a failure wearing a different label, and reclassifying a failure as a deferral is a named reward hack. **A BLOCKING-ALWAYS check may never be deferred** — if it cannot run, the build is not verifiable, and that is the finding. |
| **Mechanization** | **V-MET-006**; 09 §9.6, §11.8; the ledger **Deferrals** table, reviewed at every improvement pass (`binding.md` §Thresholds).                                                                                                                                                                                                                                                          |
| **Verify**        | A deferral with no blocker fails V-MET-006.                                                                                                                                                                                                                                                                                                                                            |

---

## LSN-009 — A suite can shrink silently during a model change

`ratchets, refactors` · **closed** _(check defined; the mechanical pre-merge script lands in P8-T6)_

| Field             |                                                                                                                                                                                                                                                            |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Trigger**       | The read-only → imperative conversion makes many read-only assertions genuinely obsolete. Deleting one and gaining no replacement leaves the suite green and smaller, and nobody notices.                                                                  |
| **Root cause**    | Green is a property of the checks that ran, not of the checks that should exist. Removal is invisible to every signal except a count.                                                                                                                      |
| **Generalize**    | **Tests are replaced, never deleted.** A retirement must name its replacement, the replacement must exist first, and the total must not fall. Retired IDs are kept with a pointer, never reused.                                                           |
| **Mechanization** | **V-MET-003** (assertion ratchet), **V-MET-004** (no ID reuse/renumbering), **V-MET-002/008/009** (coverage ratchet + published uncovered list); `invariants.md` check 8; 07 §5; 09 §11.7. P8-T6 makes it a pre-merge script rather than a checklist item. |
| **Verify**        | Delete a security assertion with no replacement and confirm the ratchet fails the diff.                                                                                                                                                                    |

---

## LSN-010 — Prettier runs over the branch, not over your session

`ci, formatting` · **closed** · broke CI **twice**

| Field             |                                                                                                                                                                                                         |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Trigger**       | PR #3 went red on a golden fixture the session had never opened. PR #4 needed a follow-up `style(phase2)` commit for skill docs and fixtures formatted in earlier commits.                              |
| **Root cause**    | The session formatted the files it edited. CI runs `git diff --name-only origin/<base_ref>...HEAD` and checks **every** changed `.md`/`.yaml`/`.yml` on the branch, including files from earlier units. |
| **Generalize**    | Run the project's formatter over the **full changed set relative to the base branch**, computed the same way CI computes it — not over the files you happen to remember touching.                       |
| **Mechanization** | `binding.md` §Build (the exact command, with `origin/main` as base); `invariants.md` "Also enforce"; the milestone gate formats and lints every changed file before the PR opens.                       |
| **Verify**        | `npx prettier --check` over the base-branch diff set is clean before every PR.                                                                                                                          |

---

## LSN-011 — Never force a merge past a check

`merge` · **closed**

| Field             |                                                                                                                                                                                                       |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Trigger**       | A milestone PR sits with the gate green except one red check, and `gh pr merge --admin` is one flag away. Auto-merge makes this a standing temptation rather than an occasional one.                  |
| **Root cause**    | The objective is "the phase is merged". Bypassing the check achieves the objective without achieving the property.                                                                                    |
| **Generalize**    | A red required check means the milestone is **not done**. Forcing it converts a slow build into an untrustworthy one — every later green rests on an unverified base.                                 |
| **Mechanization** | `binding.md` §Merge (forbidden list: `--admin`, `--no-verify`, `--fill`) and the single documented benign red (`Auto-Request-Review`); PROTOCOL §7.4; the milestone skill merges only after the gate. |
| **Verify**        | Every merged phase PR to date recorded its check results; `--admin` appears nowhere in the history.                                                                                                   |

---

## LSN-012 — The remote that carries the work is not the one you assume

`git, remotes` · **closed**

| Field             |                                                                                                                                                                                                                                                                                             |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Trigger**       | A diff against `main` shows tens of unrelated commits, or nothing at all, and a PR opened against the wrong base contains none of the phase work.                                                                                                                                           |
| **Root cause**    | Local `main` tracks **`upstream/main`** (`gke-labs/kube-agents`), which has none of the build; the work lives on the fork, currently **`origin`** (`adamparco/kube-agents`). Remote names have changed across the build — `fork` then, `origin` now — so the name is not a reliable handle. |
| **Generalize**    | Resolve the work-carrying remote from `git remote -v` at run start and use it explicitly for diff base, push, and PR base. Never rely on the branch's tracking ref for a base.                                                                                                              |
| **Mechanization** | `binding.md` §Branching — push remote, PR base, and the diff base `origin/main` with a `git fetch origin` first, plus the instruction to re-resolve remotes at run start.                                                                                                                   |
| **Verify**        | `git diff origin/main...HEAD` shows only the phase's work before the PR is opened.                                                                                                                                                                                                          |

---

## LSN-013 — Read the version from the server, not the client

`version-assertions` · **closed**

| Field             |                                                                                                                                                                                                    |
| ----------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Trigger**       | The Phase-7 "this is a vanilla, non-GKE target" assertion false-failed on a Kind cluster.                                                                                                          |
| **Root cause**    | It read the first `gitVersion` from `kubectl version`, which is the **client** build. The host's `kubectl` is gcloud's, whose version string carries `-gke`, so a Kind cluster was flagged as GKE. |
| **Generalize**    | An assertion about the target must read a property **of the target**: `.items[0].status.nodeInfo.kubeletVersion` from a node, not anything the local toolchain reports about itself.               |
| **Mechanization** | `binding.md` §Preconditions **P7**; implemented and commented in `local-dev/kind/verify-phase7.sh`. No check ID yet — the rule is a precondition, which is the cheaper and earlier form.           |
| **Verify**        | The Phase-7 gate passes on `kind-kube-agents-dev` with kubeletVersion `v1.31.2` and would fail on a `-gke` node.                                                                                   |

---

## LSN-014 — Two correct specs can describe an unbuildable system

`specs, audits` · **closed**

| Field             |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Trigger**       | A systematic requirement-coverage audit found **eighteen** cross-document conflicts and **ten** load-bearing mechanisms no component owned. The worst was invisible from either side: the actor templates granted an agent what it acts on, and **nothing granted the broker permission to write the `ActionRecord` journal it is required to write** — so invariant 3 could not be satisfied by any implementation of the design as written. Others: broker port `8443` vs `8643`, `batchWindow: 5m` widening exactly the race the workflow spec claimed to close, an audit filter scoped to one namespace that left three of four SLIs blind to the largest tier. |
| **Root cause**    | Each document was internally consistent. Contradictions live in the **space between** documents, where no single author is reading both statements at once, and a harness cannot verify an implementation against a contradiction.                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| **Generalize**    | Conflicts are found by a systematic cross-document audit, not by reading carefully. Resolve them **in the source documents** — a resolution recorded only in the verification doc is a fifth place for the truth to live. Keep the register, because a future edit can silently undo a resolution.                                                                                                                                                                                                                                                                                                                                                                  |
| **Mechanization** | 09 **§12.1** conflict register (X-1…X-18, each with the docs it landed in) and **§12.2** ownership resolutions (G-1…G-10); the journal grant is now 06 §2.2.1 "Broker operations grant", byte-identical across tiers. Lints: **V-MET-010** (every referenced ID exists), **V-MET-011** (every spec Verification bullet maps to a check), **V-MET-012** (component/contract inventories complete), **V-MET-013** (doc-drift: single definition site).                                                                                                                                                                                                                |
| **Verify**        | The register is a table, so re-running the audit is a diff against it. §12.3 records the three gaps the audit itself created (N-1 policy generator, N-2 broker-digest allowlist, N-3 anomaly-baseline checkpoint) — open **items**, tracked as deferrals, not lessons.                                                                                                                                                                                                                                                                                                                                                                                              |

---

## LSN-015 — A single-instance fixture cannot see a multi-instance conflict

`fixtures, topology` · **OPEN**

| Field             |                                                                                                                                                                                                                                                                                                     |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Trigger**       | On the first real multi-tier install, the **second** agent in a namespace hung in `ContainerCreating` with a multi-attach error. Every prior test had run exactly one agent per namespace.                                                                                                          |
| **Root cause**    | The `system-metadata` PVC was a fixed namespace-scoped name with `ReadWriteOnce`, while the data PVC was already per-agent. The designed topology co-locates tiers; the fixture never did.                                                                                                          |
| **Generalize**    | A fixture that instantiates one of something cannot observe conflicts between two. Where the design says N, the fixture must be N — cardinality is part of the property, not a test-setup detail.                                                                                                   |
| **Mechanization** | **Not yet mechanized.** The closest existing bar is 07 §2 Phase 8 Accept (a) — a clean-clone install brings **all three tiers** to Ready. Bind that bullet to a check ID when `phase-8.md` is written, and add a fixture rule: any per-agent resource is exercised with ≥2 agents in one namespace. |
| **Proposed**      | A V-RUN or V-CMP check asserting no agent-scoped resource has a namespace-fixed name; raise at the next improvement pass.                                                                                                                                                                           |
