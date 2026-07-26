# Project binding — kube-agents

The lookup table [`PROTOCOL.md`](PROTOCOL.md) defers to. Every project-specific value the protocol
and the skills reference lives here, so they stay project-agnostic and this file stays the one place
a fact can be wrong.

**Read at the start of every run.** If a value here is stale, fix it here — never work around it in
a skill, a task, or a check.

---

## §Specs

| Key                          | Value                                                                                                                                                                                              |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Design set**               | `docs/design/01`–`09` (`README.md` is the index and the invariant source)                                                                                                                          |
| **Roadmap**                  | `docs/design/07-implementation-roadmap.md` — §2 phases 8–15 + acceptance, §3 Definition of Done, §5 verification loop, §6 standing deferrals                                                       |
| **Conformance spec**         | **`docs/design/09-verification-and-validation.md`** — §3 levels, §4 suites, §6 check catalog, §7 fixtures, §8 traceability, §9 execution, §10 phase ratchet, §11 anti-false-green, §12 tightenings |
| **Check ID format**          | `V-<SUITE>-<nnn>` (09 §4). Never reused, never renumbered; retired IDs keep a replacement pointer                                                                                                  |
| **Suites**                   | V-CTN · V-BRK · V-REV · V-ISO · V-GAT · V-PRO · V-MSH · V-RUN · V-CTR · V-OBS · V-ADV · V-CHR · V-NFR · V-CMP · V-MET (09 §4)                                                                      |
| **Per-spec Verification §§** | 02 §10, 03 §11, 04 §9, 05 §8, 06 §10, 08 §7 — mapped to check IDs by V-MET-011, generated not inline                                                                                               |
| **Blocked-on-spec checks**   | Checks marked **†** in 09 §6.14 are blocked on a 09 §12 tightening (T-1…T-14). Record `deferred` with that row as the blocker; never let the implementation pick a number                          |
| **Spec-silence rule**        | Pick the simplest option consistent with the invariants, implement it, record it under **Decisions & deviations** in the ledger                                                                    |
| **Repo conventions**         | `AGENTS.md` (PR hygiene, local validation), `.github/PULL_REQUEST_TEMPLATE.md`                                                                                                                     |

---

## §State

| Artifact                     | Path                                            | Note                                                              |
| ---------------------------- | ----------------------------------------------- | ----------------------------------------------------------------- |
| **Ledger**                   | `docs/build/LEDGER.md`                          | Read first, written last, every invocation                        |
| **Phase breakdown**          | `docs/build/phase-<N>.md`                       | Written on entering a phase; tasks bound to check IDs             |
| **Lesson store**             | `.claude/harness/LESSONS.md`                    | Read the lessons tagged for the area you are about to touch       |
| **Run manifest**             | `verification/manifest-<phase>-<YYYYMMDD>.csv`  | 09 §9.4 schema; created by `harness-verify` on the first full run |
| **Traceability**             | `verification/traceability.yaml`                | Generated (V-MET-011); never hand-edited                          |
| **Uncovered list**           | Published by V-MET-009 on every full run        | A count is not enough                                             |
| **Prior-generation history** | LEDGER Phases 0–7 tables + "Outer loop" section | History. Append, never rewrite                                    |

---

## §Gates

| Gate                            | Value                                                                                                                                         |
| ------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| **Pre-merge gate**              | `.claude/harness/invariants.md` — 6 invariants + 2 conversion-ordering checks + repo mechanics + the destructive-test guard                   |
| **BLOCKING-ALWAYS suites**      | **V-CTN · V-BRK · V-REV · V-ISO · V-ADV · V-MET** (09 §4). A failure halts. May never be deferred, quarantined, retried to green, or weakened |
| **Load-bearing suites (07 §5)** | containment (03 §11) · reversibility (03 §11) · failure isolation (05 §8, extended with broker-down and journal-down)                         |
| **Phase ratchet**               | 09 §10. Once a suite enters it, it never leaves                                                                                               |
| **Assertion ratchet**           | V-MET-003 — the count of V-CTN/V-BRK/V-REV/V-ADV assertions never falls; V-MET-004 — no ID reuse                                              |
| **Coverage ratchet**            | V-MET-002 (load-bearing suites at full coverage) · V-MET-008 (elsewhere, never below baseline) · V-MET-009 (uncovered list published)         |
| **Never-red invariant**         | Invariant 2, scope is absolute — not for one commit, not for one phase                                                                        |
| **Required CI checks on a PR**  | Prettier Check · Operator Tests · Validate Repo Structure · Actionlint · Docker Build · Docs Build · Review Gate                              |
| **Known-benign red**            | `Auto-Request-Review` (fork bot; cannot see fork PRs). The **only** red that does not block                                                   |
| **Forbidden**                   | `gh pr merge --admin` · `--no-verify` · `gh pr create --fill` · `git add -A`/`.`                                                              |

---

## §Build

Run from the repo root unless stated. `BASE` below is always `origin/main` (see §Branching).

| Purpose                            | Command                                                                                                                                                                                                                                                                                    |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Operator build**                 | `make -C k8s-operator build` (runs `manifests generate fmt vet`)                                                                                                                                                                                                                           |
| **Operator tests**                 | `make -C k8s-operator test` (envtest-backed)                                                                                                                                                                                                                                               |
| **Go build/vet/test**              | `cd k8s-operator && go build ./... && go vet ./... && go test ./...`                                                                                                                                                                                                                       |
| **Go format**                      | `make -C k8s-operator fmt`                                                                                                                                                                                                                                                                 |
| **Repo-structure lint**            | `make validate` (no skills under `agents/*/defaults/skills`)                                                                                                                                                                                                                               |
| **Format changed docs** ⚠️         | `git fetch origin main && npx prettier --write $(git diff --name-only --diff-filter=d origin/main...HEAD \| grep -E '\.(md\|ya?ml\|json)$')`                                                                                                                                               |
| **Format check, as CI does it**    | Same file set, `npx prettier --check`. CI diffs `origin/<base_ref>...HEAD` — the **whole branch**, not this session's edits (LSN-010)                                                                                                                                                      |
| **Actionlint**                     | `actionlint -color` (only when `.github/workflows/` changed)                                                                                                                                                                                                                               |
| **Operator image**                 | `make -C k8s-operator docker-build IMG=<repo>/k8s-operator:<tag>`                                                                                                                                                                                                                          |
| **Agent images**                   | `make docker-build` / `make docker-build-<agent>` / `make docker-build-credential-proxy`                                                                                                                                                                                                   |
| **amd64 images for GKE**           | `make cloud-build-push` — a local `docker build` on Apple Silicon yields arm64 images GKE nodes cannot run                                                                                                                                                                                 |
| **Kind image refresh** (mandatory) | `make -C k8s-operator docker-build IMG=…:dev-<tag>` → `kind load docker-image …:dev-<tag> --name kube-agents-dev` → `kubectl --context kind-kube-agents-dev -n kubeagents-system rollout restart deploy/kubeagents-controller-manager` → **compare running `imageID` to the built digest** |

⚠️ Prettier must run over the full changed set **against the base branch**, not the files touched
this session. Formatting a file in an earlier commit and not re-checking it has broken CI twice.

---

## §Test

Runnable entry points, and what each actually covers. A phase's own gate script is written as that
phase's last task (07 §2) and transitively re-runs its predecessor.

| Entry point                                                                              | Level | Covers                                                                                                                                                                                 |
| ---------------------------------------------------------------------------------------- | ----- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cd k8s-operator && go test ./...`                                                       | L1    | Controller reconcile, webhooks, `kage-router`, scope resolution, rendered-manifest goldens                                                                                             |
| `make -C k8s-operator test`                                                              | L1    | The above plus envtest (real API-server admission of CRs)                                                                                                                              |
| `python3 -m unittest discover dev`                                                       | L0/L1 | GitOps skill tooling: `submit-suggestion`, `read-knowledge`, `raise-escalation`, `detect_drift`, heartbeat SOPs                                                                        |
| `python3 dev/okf-validate.py examples/gitops-repo/knowledge`                             | L0    | OKF knowledge entries typed, links resolve (negative control included)                                                                                                                 |
| `python3 -m unittest discover -s scripts/review-gate`                                    | L0    | Review-gate scorer + finding extraction                                                                                                                                                |
| `python3 scripts/review-gate/score_findings.py --waivers security-review-waivers.yaml F` | L0    | The authoritative merge decision: unmitigated high/critical ⇒ exit 1                                                                                                                   |
| `dev/tests/iac-parity.py`                                                                | L0    | KCC ↔ Terraform HCL structural validity + semantic equivalence; `apply.yml` per-format dispatch                                                                                        |
| `dev/tests/circleci-parity.py`                                                           | L0    | Second CI/CD dispatch parity; no agent-held write credential                                                                                                                           |
| `dev/tests/observability-seam.py`, `dev/tests/otel-endpoint.sh`                          | L0    | Provider-neutral observability seam; env override, unset default, fail-loud                                                                                                            |
| `dev/tests/negative-attenuation.sh [ctx]`                                                | L2    | **03 §11 load-bearing**: write-verb Role DENIED, `impersonate` ClusterRole DENIED, wrong-scope DENIED, read-only ADMITTED; distinguishes a policy denial from a malformed-object error |
| `dev/tests/egress-enforcement.sh [ctx=kind-kube-agents-dev]`                             | L2†   | Real egress enforcement on Calico. **Defers loudly** if the CNI does not enforce NetworkPolicy                                                                                         |
| `dev/verify/chaos-suite.sh [ctx]`                                                        | L2    | **05 §8 load-bearing**: C1 controller-down, C2 relaunch, C3 no-cascade, C4 hub-down                                                                                                    |
| `dev/verify/verify-phase<N>.sh [ctx]`                                                    | L2    | Phase `N` consolidated gate + regression of `N-1`. Current chain: 7 → 6 (+chaos) → {5,4,3,2} → 03 §11 → goldens → `go test`                                                            |
| `dev/cluster/up.sh` / `down.sh`                                                          | —     | Bring THE inner-loop Kind cluster up/down: 2 nodes, Calico, operator, VAP, agent images. Replaced `up-egress.sh` and `up-2node.sh`                                                     |
| `dev/gke-scratch/create.sh` / `destroy.sh`                                               | —     | Create/destroy the ephemeral scratch GKE cluster                                                                                                                                       |

† Only green on an enforcing dataplane. On kindnet the result is `deferred`, never `pass` (LSN-006).

---

## §Targets

| Level  | Target                                                                    | Concretely                                                                                                                                                                                                                                                                                                                                                                       |
| ------ | ------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **L0** | None — the working tree                                                   | greps, schema/CEL/manifest lints, prettier, python validators                                                                                                                                                                                                                                                                                                                    |
| **L1** | Process-local                                                             | `go test ./...`, envtest, `python3 -m unittest discover dev`                                                                                                                                                                                                                                                                                                                     |
| **L2** | **Kind `kube-agents-dev`** — the only inner-loop cluster                  | context **`kind-kube-agents-dev`**, `kindest/node:v1.31.2`, config `dev/kind/kind-config.yaml`, built by `dev/cluster/up.sh`. **2 nodes** (RWO excludes per node — LSN-015) and **Calico v3.28.0** (kindnet enforces no NetworkPolicy — LSN-006/P4). Was three clusters until 2026-07-26; they differed only in CNI and node count, which are orthogonal, so this is their union |
| **L3** | **Scratch GKE `kube-agents-scratch`**                                     | Autopilot + Workload Identity, region `us-central1`, current gcloud project; context renamed **`gke-scratch-kube-agents-scratch`**. **Ephemeral — destroy after use**                                                                                                                                                                                                            |
| **L3** | **Live GKE `platform-agent-host`** (project `adamparco-kage`, `us-east4`) | `k8s-operator/scripts/vars.sh`. Outer-loop install verification only. **Not ephemeral; NOT a destructive-test target**                                                                                                                                                                                                                                                           |
| **L4** | Soak / load                                                               | **No environment provisioned.** Record `deferred`, blocker "no soak target"                                                                                                                                                                                                                                                                                                      |

---

## §Preconditions

Assert **before** trusting any L2/L3 result (09 §9.3). Each exists because it produced a green
result on a broken property in this repository.

| #   | Precondition                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | Lesson           |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------- |
| P1  | **Images are current.** Rebuild → `kind load`/push → `rollout restart`, then assert every deployed first-party `imageID` digest matches the build under test. Same-tag + `IfNotPresent` does **not** refresh.                                                                                                                                                                                                                                                                                                                                                                                                     | LSN-001          |
| P2  | **Policies are live.** A new `ValidatingAdmissionPolicyBinding` activates with a delay — poll a dry-run apply until it actually **rejects** before judging.                                                                                                                                                                                                                                                                                                                                                                                                                                                       | —                |
| P3  | **No grandfathered objects.** Force-recreate the pods/objects the property is about; admission policies and PSA do not evict what already exists. Then reach the replacement by **identity** — `p3_pod_of_deploy`, i.e. the ownership chain — never by a label selector, which still matches the generation you just deleted, and never by `.items[N]`, which re-resolves a list GC is emptying under you.                                                                                                                                                                                                        | LSN-002, LSN-025 |
| P4  | **The CNI enforces NetworkPolicy.** `kubectl -n kube-system get ds calico-node`. Otherwise an egress result is `deferred`, never `pass`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | LSN-006          |
| P5  | **Destructive-test guard.** The context matches the **anchored** allow-list `kind-*` or `gke-scratch-*` (shell `case`, never a substring glob). Anything else is a **halt**, not a judgement call.                                                                                                                                                                                                                                                                                                                                                                                                                | LSN-005          |
| P6  | **Runtime-authoritative artifact.** Assert against what the runtime actually reads — the operator-rendered ConfigMap, not the image-baked `config.yaml` it shadows — and name which one the check reads.                                                                                                                                                                                                                                                                                                                                                                                                          | LSN-003          |
| P7  | **Server-side version.** Read `kubectl get nodes -o jsonpath='{.items[0].status.nodeInfo.kubeletVersion}'`. `kubectl version`'s first line is the **client** build and mis-flags the target.                                                                                                                                                                                                                                                                                                                                                                                                                      | LSN-013          |
| P8  | **Provisioning honours the built image.** On a live target, confirm `OPERATOR_IMAGE`/`AGENT_IMAGE` reached the deploy step — running digests must match the project registry, with zero `ghcr.io/gke-labs` containers.                                                                                                                                                                                                                                                                                                                                                                                            | LSN-001          |
| P9  | **Controller-written state is polled.** A `.status` subtree is written asynchronously after admission — reach any assertion on one through a bounded poll or `kubectl wait --for=`, never a `sleep` and never a bare read. An empty `.status` is indistinguishable from the property being absent.                                                                                                                                                                                                                                                                                                                | LSN-024          |
| P10 | **The cluster can run the experiment.** Before any L2 verdict, assert the target's control plane both converges _and_ has been stable: API server answers `/readyz`, a fresh namespace receives its `default` ServiceAccount (proof kube-controller-manager is doing work, not a proxy for it), `kube-scheduler` is Ready, and no control-plane container has restarted inside `P10_FLAP_WINDOW` (900s). `p10_assert_control_plane_healthy`. Failure is **rc 2, could-not-run — never rc 1**: a dead scheduler leaves fixture pods Pending, and every claim downstream then reports its security property ABSENT. | LSN-026          |

---

## §Branching

| Key                         | Value                                                                                                                                                    |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Phase branch**            | `phase-<N>-<slug>` (e.g. `phase-8-contain-the-pod`)                                                                                                      |
| **Base branch**             | `main`                                                                                                                                                   |
| **Push remote**             | **`origin` = `https://github.com/adamparco/kube-agents` — the fork.** This is where every phase branch, PR, and merge lives                              |
| **`upstream`**              | `https://github.com/gke-labs/kube-agents` — **never** a push target, never a PR base, never a diff base                                                  |
| **Diff / prettier base** ⚠️ | `origin/main`, always. Local `main` tracks **`upstream/main`** and is tens of commits behind — `git diff main...HEAD` is wrong. `git fetch origin` first |
| **Resolve before trusting** | `git remote -v` at run start; the remote that carries the work is not necessarily the one your branch tracks (LSN-012)                                   |
| **Commits**                 | Conventional Commits, one or more per unit, scoped staging by explicit path. Never `git add -A`/`.`                                                      |
| **Resume rule**             | A unit `in-progress` with uncommitted work is validated (build + its checks) and then finished or reverted cleanly — never built upon                    |

---

## §Merge

| Step               | Value                                                                                                                                            |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Open the PR**    | `gh pr create --base main --head <branch>` with a body built from `.github/PULL_REQUEST_TEMPLATE.md`. **Never `--fill`**                         |
| **PR body**        | Phase summary · verification table (`check_id, level, target, result, evidence_ref`) · decisions · every deferral with its named blocker         |
| **Merge**          | `gh pr merge <n> --squash --delete-branch`                                                                                                       |
| **Never**          | `--admin`, `--no-verify`, or any bypass of a required check. A red required check means the milestone is **not done** — fix it or halt (LSN-011) |
| **Auto-merge**     | Phase PRs auto-merge once the pre-merge gate is green (user decision, 2026-07-24). Gated, not blind: any halt condition blocks and surfaces      |
| **Benign red**     | `Auto-Request-Review` only                                                                                                                       |
| **After merge**    | Ledger: phase ✅, merge commit + PR URL, ratchet extended, baselines raised, metrics snapshot, deferrals carried, next phase opened              |
| **Share the link** | Always surface the PR URL when a PR is created or updated                                                                                        |

---

## §Schedule

| Key                | Value                                                                                                                                       |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------- |
| **On demand**      | `/harness-run` (one unit) · `/harness-verify` · `/harness-milestone` · `/harness-improve`                                                   |
| **Autonomous**     | A durable Claude Code cron re-enqueues `/harness-run`. Prior campaign: every 2h at `:37` (deleted 2026-07-24 when the old roadmap finished) |
| **Re-arm**         | Create a fresh cron when a campaign starts. Tasks auto-expire after **7 days** and fire only while the REPL is **idle**                     |
| **One firing**     | = one invocation = one bounded unit + checkpoint. Long builds are many short sessions                                                       |
| **Pause**          | Delete the scheduled task. Nothing self-triggers                                                                                            |
| **Autonomy level** | Fully autonomous across phase boundaries; halt only on a PROTOCOL §8 condition                                                              |

---

## §Thresholds

| Threshold                      | Value                                                                                                       |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| **Improvement-pass cadence**   | Every milestone (phase close); after any halt is cleared; whenever a threshold below trips                  |
| **Open-lesson threshold**      | **> 5 open** ⇒ the next invocation is an improvement pass and nothing else                                  |
| **Stuck lesson**               | Open across **3** improvement passes ⇒ escalate to a human; the harness cannot fix itself there             |
| **Repeated-failure limit**     | **3** verification failures on one unit with no new information ⇒ **HALT** (PROTOCOL §8.7)                  |
| **Repeated procedure mistake** | The same procedural error **twice** ⇒ fix the skill, not the instance                                       |
| **Retry-to-green**             | **0** for V-CTN, V-BRK, V-REV, V-ADV, V-MET. **1** elsewhere, after which the check is quarantined          |
| **Quarantine**                 | Time-boxed and visible in the manifest. A quarantined BLOCKING-ALWAYS check blocks                          |
| **Unit size**                  | One task from `phase-<N>.md`. If it will not fit a session, split it in the breakdown and do the first half |
| **Session budget**             | Checkpoint by ~60% context. A clean stop mid-phase is free; an unclean stop mid-unit costs a session        |
| **Deferral review**            | Every improvement pass — promote any whose blocker has cleared, re-examine any whose blocker is stale       |
| **Escape response**            | Strengthen **the check that should have caught it**, not merely add one at the point of discovery           |
| **Pass without evidence**      | Recorded as `skipped`. Always                                                                               |
