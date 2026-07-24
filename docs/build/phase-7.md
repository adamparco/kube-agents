# Phase 7 — Cloud-agnostic seams (task breakdown)

**Roadmap:** `docs/design/07-implementation-roadmap.md` §"Phase 7 — Cloud-agnostic seams (later)".
**Goal:** reduce GKE coupling ([01](../design/01-vision-scope.md) §6) by **exercising the
already-unopinionated seams** the earlier phases put in place — turning three _contracts_ into real,
tested artifacts: (1) generate **Terraform HCL** as well as KCC YAML, (2) actuate via a **second CI/CD**
(CircleCI) alongside the reference GitHub-Actions `apply.yml`, and (3) abstract **observability** behind
provider-neutral seams. This is the **final** roadmap phase; it adds **no new persona, no new agent
capability, and no new write path** — every change is a seam that keeps GKE as the zero-config default
while making a non-GKE target possible. 01 §6 is explicit that cloud-agnosticism is **"direction, not a
committed milestone"**, so this phase makes the seams **real and verifiable**, proves the cloud-neutral
core on **vanilla Kind**, and **honestly defers** the pieces that need a real second cloud (EKS/AKS
identity + a live apply) rather than faking them.

**Survey of the current state (what exists vs what is missing) — drives the breakdown:**

- **IaC:** the `Agent` CRD has `spec.iac.format` (`kcc | terraform`, default `kcc`) at
  `k8s-operator/api/v1alpha1/common_types.go:226-244`, but it has **no programmatic consumer** — the only
  artifact author is `agents/platform/skills/gke-cluster-creator/SKILL.md`, whose Terraform path is
  **LLM prose only** (lines 291-298 "author the equivalent HCL"), with **no template, generator, or
  committed HCL exemplar**. The actuation pipeline **already dispatches** `.tf`→`terraform apply` /
  `.y*ml`→`kubectl apply` (`examples/gitops-repo/.github/workflows/apply.yml` `apply_path()`), so the
  **pipeline seam is real; the artifact seam is not**.
- **Second CI/CD:** **none exists** — only `apply.yml` (GitHub Actions). CircleCI/Jenkins/Argo/Flux are
  named as valid in prose (`apply.yml` header, workflows `README.md`) but **unimplemented**.
- **Observability:** **hardcoded to GCP** — the OTLP endpoint is **baked at build time** to
  `http://opentelemetry-collector.gke-managed-otel.svc.cluster.local:4318/v1/traces`
  (`deploy/docker/Dockerfile:96-99`); the observability skill scripts hit `monitoring.googleapis.com`,
  `cloudtrace.googleapis.com`, GMP, and Cloud Logging directly
  (`agents/platform/skills/kube-agents-observability/*`). The only provider-neutral surfaces today are the
  OTLP _protocol_ (endpoint not abstracted) and the Go binaries' Prometheus `/metrics`.

**Phase acceptance (07 §2 "Accept") — decomposed a–c:**

- **(a)** **A second target using the customer's IaC of choice.** `spec.iac.format: terraform` yields a
  **real, valid Terraform HCL** provisioning artifact that is **semantically equivalent** to the KCC YAML
  a `kcc` target would produce, and the pipeline applies it (`.tf`→terraform).
- **(b)** **A second target using the customer's pipeline of choice.** A second reference CI/CD (CircleCI)
  actuates the same GitOps repo with the **same KCC/HCL dispatch and least-privilege per-target creds** as
  `apply.yml` — proving actuation is genuinely unopinionated, not GitHub-Actions-specific.
- **(c)** **The Phase 1–3 core concepts pass on a second (vanilla, non-GKE) target.** On vanilla Kind the
  load-bearing concepts hold with **no GKE dependency** — read-only agent SA (SAR), GitOps-PR-only
  mutation, namespace isolation, `(tier,scope)` cardinality webhook, and the attenuation VAP — and
  observability resolves through a provider-neutral seam (default GKE, overridable). The GKE-coupled
  **cloud identity** (WI→GSA) and a **live EKS/AKS apply** are deferred-not-faked (D1).

**Touched Verification suites:** this is a **seam/artifact** phase, so its net-new checks are **structural +
semantic** (HCL validity + KCC↔HCL parity; CircleCI validity + dispatch parity vs `apply.yml`; the OTLP /
observability-backend seam resolves by env). It **regresses** the two load-bearing suites — **03 §11**
(security negatives) and **05 §8** (chaos, now live) — plus every prior gate `verify-phase{2,3,4,5,6}.sh`
and `go test ./...`, because a cloud-agnostic refactor must not weaken the read-only ceiling, isolation, or
resilience. **04 §9 / 06 §4 / 06 §1.1** (IaC + actuation contracts) and **01 §6** (the cloud-agnostic
direction) are the specs this phase makes concrete.

**Source of the breakdown:** the survey above (exact file:line map of the three seams); 06 §4's actuation &
IaC conventions ("KCC YAML _or_ Terraform HCL, selected by `spec.iac.format`"; "GitHub Actions, CircleCI,
Jenkins, or an existing GitOps engine"); 06 §1.1's `iac.format` contract; 01 §6's delta table (Provisioning
artifact / Observability / Actuation rows) and its "direction, not a committed milestone" framing; the
existing `apply.yml` `apply_path()` dispatch; and the `verify-phase{2..6}.sh` gate pattern + destructive-test
guard. The survey surfaced the decisive constraint that shapes the decisions: **the validation CLIs
`terraform` and `circleci` are absent on this host**, so — exactly as Calico stood in for kindnet's missing
NetworkPolicy enforcement in earlier phases — the load-bearing properties are proven **hermetically
(structural + semantic, via `go`/`python3`)** and CLI-level enforcement (`terraform validate`,
`circleci config validate`) is flagged **deferred-not-faked**.

---

## Architecture decisions (load-bearing — resolved before breakdown)

**D1 — "Second target" is proven at two honestly-separated layers: cloud-neutral **core** on vanilla Kind
(live), cloud **identity/apply** on a real second cloud (deferred-not-faked).** The Phase-1–3 acceptance
splits cleanly into (i) **cloud-neutral core concepts** — read-only agent SA + SAR isolation, GitOps-PR-only
mutation, namespace isolation, the `(tier,scope)` cardinality webhook, and the attenuation VAP — which are
**pure-Kubernetes** properties with **no GKE API dependency**, and (ii) **cloud-coupled identity** — the
Workload-Identity→viewer-GSA binding, which on EKS is IRSA and on AKS is AAD Workload Identity. Kind **is a
vanilla, non-GKE Kubernetes distribution**, so (i) is genuinely a "second target passes on core concepts"
result and is asserted **live** in `verify-phase7.sh` with an **explicit no-GKE-dependency check**. (ii)
needs a real AWS/Azure account + cluster and is **deferred-not-faked**, mirroring the Phase-2 V-G scratch-GKE
deferral. This is faithful to 01 §6 ("GKE/GCP is the first and only fully supported target today"; cloud
portability is "direction").

**D2 — IaC parity = real committed HCL + KCC↔HCL **semantic-equivalence** + HCL **structural validity**;
`terraform validate`/`apply` deferred (CLI absent).** Because both KCC and HCL artifacts are **model-authored**
(the generator is `gke-cluster-creator/SKILL.md` prose, not code), the verifiable seam is a **matched pair
of committed exemplars for equivalent clusters**: a **KCC** target (`clusters/cluster-a`) and a **Terraform**
target (`clusters/cluster-b`, `spec.iac.format: terraform`). A hermetic validator asserts (1) the HCL is
**structurally valid** (balanced blocks; required `terraform{}` provider pin + `resource
"google_container_cluster"` + `resource "google_container_node_pool"` with their required attributes) and
(2) the two artifacts are **semantically equivalent** (same location, release channel, node machine
type/count, networking shape) — i.e. `iac.format` selects a real, equivalent artifact the pipeline applies.
`terraform validate`/`fmt`/`apply` and a real cloud apply are the production checks, **deferred-not-faked**
(no `terraform` binary; no billable cloud). `SKILL.md` is tightened to name the exemplars as canonical so
the model's HCL matches the tested shape.

**D3 — Provider-neutral observability = a **seam with GKE as the default**, never a rip-out.** Two moves,
both keeping current GKE installs byte-for-byte unchanged when the new knobs are unset: (a) the OTLP endpoint
moves from **baked-at-build** to the **standard `OTEL_EXPORTER_OTLP_ENDPOINT` env** resolved by the
entrypoint, **defaulting to the existing `gke-managed-otel` URL**; (b) the observability skill's backend base
URLs move behind a **`KUBEAGENTS_OBS_BACKEND` / base-URL env selector** defaulting to `gcp`
(`monitoring.googleapis.com`/`cloudtrace.googleapis.com`), with a **documented Prometheus/OTLP-native path**
for a non-GCP target. Verifiable **hermetically**: an env override changes the resolved endpoint/backend; the
unset default is unchanged (no regression). A **live non-GCP backend** queried end-to-end is
deferred-not-faked (none running here).

**D4 — A second pipeline changes **no** trust boundary (invariant 2 unaffected).** The CircleCI config is the
**customer's actuation**, exactly like `apply.yml` — the pipeline is the privileged writer; the **agent still
holds no cluster/cloud write credential**. Adding a second reference pipeline therefore introduces no new
write path from the agent and is proven equivalent to `apply.yml` by a **dispatch-parity** check (same
KCC→kubectl / HCL→terraform routing, same merge-to-`main` trigger, same per-target least-priv creds). Its
validity is checked **structurally** (YAML parse + required `version`/jobs/workflow-filtered-to-main);
`circleci config validate` is deferred-not-faked (CLI absent).

---

## Ordering / dependency rule (critical)

1. **P7-T1 (IaC) and P7-T2 (pipeline) are independent** and may land in either order; both are **artifact +
   hermetic-validator** tasks. P7-T2's dispatch-parity check reads `apply.yml`, so it is easiest after
   confirming `apply.yml`'s current shape (unchanged this phase).
2. **P7-T3 (observability)** touches the image (`Dockerfile`/entrypoint) and skill scripts; if the agent
   image is rebuilt, follow the **inner-loop rebuild rule** (LEDGER Decisions, Phase 3/6) — but the
   entrypoint/skill seams are proven **hermetically**, so no live image rebuild is required for the gate.
3. **P7-T4 (vanilla core-concept acceptance)** re-uses the live Kind stack + `verify-phase{2,3}.sh`; it must
   run **after** T3 so the no-GKE-dependency assertion covers the observability seam too.
4. **P7-T5 (`verify-phase7.sh`) is last within the phase** — it runs the T1–T4 checks, then **regression**
   (`verify-phase6.sh` → transitively `verify-phase{2,3,4,5}.sh` + `chaos-suite.sh` + `negative-attenuation.sh`
   - goldens + `go test ./...`). Any regression or load-bearing-suite failure is a **HALT**, not a note.
5. **P7-T6** is docs + PR + (final phase) the **07 §3 Definition of Done** run.

---

## Tasks

| ID    | Task                                                                                                                                                                                                                     | Implements (doc §)                 | Files (primary)                                                                                                                                                                                                     | Acceptance signal                                                                                                                                                                                                                                                                                                                                                                                    | Status                                            |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| P7-T1 | **Terraform HCL provisioning exemplar + KCC↔HCL `iac.format` parity.** Add a matched KCC (`cluster-a`) + Terraform (`cluster-b`) target pair; make `iac.format` routing concrete in the skill; hermetic parity validator | 06 §1.1, §4; 07 P7 (a); 01 §6      | `examples/gitops-repo/clusters/cluster-a/provisioning/*.yaml` (KCC), `.../cluster-b/provisioning/*.tf` (+`README.md`), `agents/platform/skills/gke-cluster-creator/SKILL.md`, `local-dev/tests/iac-parity.py`       | `iac-parity.py` **exit 0**: HCL structurally valid (required `terraform`/`google_container_cluster`/`google_container_node_pool` blocks + attrs, balanced braces); KCC↔HCL **semantically equivalent** (location/channel/machine/count/network); `apply.yml` dispatches `cluster-b/*.tf`→terraform, `cluster-a/*.yaml`→kubectl; a bad-HCL negative control → exit ≠ 0                                | ✅ `a92abc7` — parity validator exit 0 (14 facts) |
| P7-T2 | **Second CI/CD reference (CircleCI)** mirroring `apply.yml` + dispatch-parity validator                                                                                                                                  | 06 §4; 07 P7 (b); invariant 2 (D4) | `examples/gitops-repo/.circleci/config.yml` (+`.circleci/README.md`), `examples/gitops-repo/.github/workflows/README.md`, `local-dev/tests/circleci-parity.py`                                                      | `circleci-parity.py` **exit 0**: `config.yml` valid YAML with `version: 2.1` + an apply job + a workflow **filtered to `main`**; **dispatch parity** — same KCC→kubectl / HCL→terraform routing + per-target least-priv creds as `apply.yml`; **no agent-held write credential** introduced (invariant 2); a malformed-config negative control → exit ≠ 0                                            | ✅ `0ae06d7` — parity validator exit 0 (8 checks) |
| P7-T3 | **Provider-neutral observability seam:** env-driven OTLP endpoint (default GKE) + backend-selectable obs skill (default GCP)                                                                                             | 01 §6 (Observability); 07 P7       | `deploy/docker/Dockerfile`, `deploy/shared/docker-entrypoint.sh`, `agents/platform/skills/kube-agents-observability/*.py` (+ SKILL.md), `local-dev/tests/observability-seam.py`, `local-dev/tests/otel-endpoint.sh` | Seam tests **exit 0**: `OTEL_EXPORTER_OTLP_ENDPOINT` set → entrypoint resolves that endpoint; **unset → the exact current `gke-managed-otel` default** (no regression); obs skill base URLs resolve from `KUBEAGENTS_OBS_BACKEND`/env with **`gcp` default unchanged** + a documented non-GCP path; a Prometheus/OTLP-native override resolves to non-`googleapis.com`                               | ✅ `0bfc908` — seam tests exit 0 (obs 6, otel 6)  |
| P7-T4 | **Vanilla (Kind, non-GKE) core-concept acceptance:** Phase 1–3 core concepts hold with **no GKE dependency**                                                                                                             | 07 P7 (c) Accept; 03 §11; 06 §2b   | `local-dev/kind/verify-phase7.sh` (section), reuse `verify-phase{2,3}.sh`, `negative-attenuation.sh`                                                                                                                | On vanilla Kind: read-only SAR (get/list/watch only, no writes/priv-esc), GitOps-PR-only, namespace isolation, `(tier,scope)` cardinality webhook, VAP attenuation, deterministic ChatOps routing (`inference_calls==0`) all **PASS**; explicit **no-GKE-dependency** assertion (core RBAC/webhook/VAP/router path references no `*.googleapis.com`/GKE-only API); cloud identity deferred-not-faked | ⬜                                                |
| P7-T5 | **`verify-phase7.sh` consolidated gate + full regression**                                                                                                                                                               | 07 §5; 03 §11; 05 §8; 04 §9; 08 §7 | `local-dev/kind/verify-phase7.sh` (new); reuse `iac-parity.py`, `circleci-parity.py`, `observability-seam.py`, `otel-endpoint.sh`, `verify-phase6.sh`, `go test`                                                    | `verify-phase7.sh kind-kube-agents-dev` **exit 0**: T1–T4 pass; **regression** `verify-phase6.sh` (→ chaos C1–C4 + `verify-phase{2,3,4,5}.sh` + 03 §11 negatives + goldens + `go test ./...`) **not regressed**; deferrals printed, never asserted green                                                                                                                                             | ⬜                                                |
| P7-T6 | **Docs (INSTALL Phase 7 + ToC, LEDGER, memory) + PR → `main` on fork; auto-merge; then 07 §3 Definition of Done**                                                                                                        | roadmap 07 §3; AGENTS.md           | `INSTALL.md`, `docs/build/LEDGER.md`, memory, PR                                                                                                                                                                    | PR opened on fork base `main`; CI green + only the benign Auto-Request-Review red; no HALT; PR URL shared; auto-merged once the gate passes; local `main` fast-forwarded; **Definition of Done (07 §3, 9 items)** recorded in the LEDGER with per-item evidence/deferral                                                                                                                             | ⬜                                                |

---

## Verification suites & Accept mapping

| Accept | Proof                                                                                                                                                                                                                                                                                                                                                                           | Task(s)      |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------ |
| (a)    | **P7-T1**: `iac-parity.py` — a real committed Terraform HCL exemplar (`cluster-b`, `iac.format: terraform`) is structurally valid **and** semantically equivalent to the KCC exemplar (`cluster-a`); `apply.yml` dispatches each format correctly. Live `terraform apply` deferred (D2).                                                                                        | P7-T1        |
| (b)    | **P7-T2**: `circleci-parity.py` — a second reference pipeline (`.circleci/config.yml`) actuates the same repo with the same KCC/HCL dispatch + least-priv per-target creds as `apply.yml`, adding no agent-held credential (invariant 2). `circleci config validate` deferred (D4).                                                                                             | P7-T2        |
| (c)    | **P7-T4**: on vanilla Kind, the Phase 1–3 cloud-neutral core concepts (read-only SAR, GitOps-PR-only, isolation, cardinality webhook, VAP attenuation, deterministic routing) pass with an explicit no-GKE-dependency assertion; **P7-T3** proves observability resolves through a provider-neutral seam. Cloud identity (WI/IRSA/AAD) + live second-cloud apply deferred (D1). | P7-T4, P7-T3 |

**Regression (must stay green — HALT on failure):** `verify-phase6.sh` (05 §8 chaos C1–C4, now live
load-bearing) → transitively `verify-phase{2,3,4,5}.sh` (prior-phase Accept incl. Calico egress + hardening
VAP) + `negative-attenuation.sh` (03 §11 read-only ceiling) + goldens + `go test ./...` (08 §7 controller
mints no RBAC). The seam changes are additive (default-preserving), so nothing prior should move.

**Deferred-not-faked (recorded, not silently dropped):** a **real second cloud** — EKS/AKS cluster +
cloud identity (IRSA / AAD Workload Identity) + a live `terraform apply` / cross-cloud pipeline run (D1/D2);
**CLI-level artifact validation** — `terraform validate`/`fmt`/`apply` and `circleci config validate` (the
`terraform`/`circleci` binaries are absent — structural + semantic parity is proven hermetically instead, the
same pattern as Calico standing in for kindnet); a **live non-GCP observability backend** queried end-to-end
(D3). Also still carried from earlier phases: the scratch-GKE **V-G** cloud checks (Phase 2), and the
**cross-object webhook, gVisor execution sandbox, and per-request user down-scoping** deferred hardening
(08 §5). None of these are asserted green.
