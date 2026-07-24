# Phase 1 — Read-only Platform Agent + GitOps loop (task breakdown)

**Roadmap:** `docs/design/07-implementation-roadmap.md` §"Phase 1 — Read-only Platform Agent + GitOps loop".
**Goal:** close the biggest delta — remove **all** direct mutation from the Platform Agent. The only
write path becomes a reviewed GitOps PR (`submit-suggestion`) that the CI/CD actuation pipeline applies
on merge.

**Phase acceptance (07):**

- **A1.** Platform Agent can provision a cluster **only** by opening a PR with a KCC or Terraform
  artifact that the CI/CD pipeline applies on merge.
- **A2.** A direct-mutation attempt **fails** (no RBAC, no tool) — at the K8s boundary **and** the cloud
  (GSA) boundary.
- **A3.** An audit record ties the change to requester + PR.
- **A4.** Only allowlisted (trusted) humans can reach the agent, and the agent can only **read within its
  tier + propose** — no direct mutation, no reads outside tier.

**Touched Verification suites:** 06 §10 (contract/layout, cardinality, read-only identity), 08 §7
(pre-created identity path; controller RBAC has no roles/rolebindings-create), 03 §11 (attenuation —
still green from Phase 0). **Load-bearing subset active this phase:** 03 §11 (read-only/attenuation)

- the 08 §7 "controller mints no RBAC" + the cloud-GSA-viewer-only assertion (scratch GKE).

**Source of the breakdown:** the `phase-1-write-surface-map` workflow (7 agents: 6 parallel area-mappers

- a completeness critic) mapped every current write/mutation/RBAC-mint path against 06 §9, 07 Phase 1,
  and 08 §2/§4. The critic caught one **load-bearing gap** (cloud GSA viewer-only IAM) that every mapper
  punted, and one named deliverable (the Scion spike) none listed.

---

## Ordering / dependency rule (critical)

**Pre-created identity must land before (or in the same change as) removing runtime minting.** Deleting
`reconcileRBAC` / `reconcileServiceAccount` without the pre-created KSA + view/explorer RBAC already
applied leaves the agent pod with **no read identity** and it fails. So: **T6/T7 (pre-created identity +
CRD fields) before T4/T5 (mint removal)**. On the verification cluster, apply the pre-created manifests,
then deploy the no-mint controller, then assert the agent still reads and mints nothing.

---

## Tasks

| ID     | Task                                                         | Implements             | Files                                                                                                                                                                                                                                                          | Risk         | Acceptance signal                                                                                                                       | Status |
| ------ | ------------------------------------------------------------ | ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------ | --------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| P1-T1  | Operator `renderConfigYAML()` → no cluster-creating tool     | 06 §9                  | `k8s-operator/internal/controller/platformagent_manifests.go` (gke server ~164-167; `mcp-gke` toolsets ~169-172; config mount `readOnly:true` ~557-561) + baked `agents/platform/config.yaml`, `deploy/shared/defaults/config.yaml`                            | load-bearing | rendered config.yaml has no `gke` write server / `mcp-gke` toolset; mount `readOnly:true`                                               | ✅     |
| P1-T2  | Remove dead kubectl write helpers                            | 06 §9                  | `agents/platform/scripts/platform_mcp_server.py` (`apply_manifest` 162-167, `delete_cluster_manifest` 170-175)                                                                                                                                                 | low          | helpers gone; grep finds no `kubectl apply/delete` write path; tests pass                                                               | ✅     |
| P1-T3  | Retire `create_cluster`; route infra via GitOps              | 06 §9, §3, §4          | `agents/platform/skills/gke-cluster-creator/SKILL.md`, `gke-cluster-lifecycle/SKILL.md`, `submit-suggestion/{SKILL.md,scripts/submit_suggestion.py}`                                                                                                           | load-bearing | creator authors KCC/Terraform + opens PR (no `create_cluster`); submit-suggestion tier-parameterized                                    | ⬜     |
| P1-T4  | Stop runtime RBAC-minting (controller)                       | 08 §4, 08 §7           | `platformagent_controller.go` (reconcileRBAC 296-318 + call 92-95; handleDeletion RBAC 148-179; Watches 492-511; marker :59), `platformagent_manifests.go` (buildPlatformExplorerRole 837-860, buildClusterRoleBinding 862-890), regen `config/rbac/role.yaml` | load-bearing | controller creates/binds no RBAC; `role.yaml` has no clusterroles/bindings create;bind                                                  | ✅     |
| P1-T5  | Stop minting agent KSA; reference pre-created only           | 08 §2 item4, §4        | `platformagent_controller.go` (reconcileServiceAccount 181-196 + call 88-90; marker :56), `manifest_helpers.go` (ReconcileServiceAccount 166-195), regen `role.yaml` (serviceaccounts → get;list;watch)                                                        | high         | controller does not create the SA or stamp WI; only references by name                                                                  | ✅     |
| P1-T6  | Pre-created platform identity + fleet platform `Agent` CR    | 06 §2, 08 §2/§4, 05 §3 | `examples/gitops-repo/fleet/**` (canonical `platform-agent` KSA+WI, `view` CRB, explorer ClusterRole/CRB, all `kube-agents/tier: platform`; platform-tier Agent CR migrating `PlatformAgent`)                                                                  | load-bearing | applying manifests gives agent read-only identity; VAP admits them; CR uses `serviceAccountName: platform-agent`, closed `allowedUsers` | ✅     |
| P1-T7  | CRD generalization — `tier`/`scope`/`parentRef`/`iac.format` | 06 §1, §1.1, §1.2      | `k8s-operator/api/v1alpha1/common_types.go` (AgentSpec: Tier enum+immutable+default `platform`; Scope; ParentRef; IAC{Format} enum default `kcc`); regen `zz_generated.deepcopy.go` + `config/crd/bases/...platformagents.yaml`                                | load-bearing | CRD accepts the new fields; `make generate manifests` clean; **additive, no Kind rename**                                               | ✅     |
| P1-T8  | `(tier,scope)` cardinality webhook + tier immutability       | 06 §1.2, §10           | `k8s-operator/internal/webhook/platformagent_webhook.go` (cardinality 102-126 → per-(tier,scope); ValidateUpdate tier-immutable)                                                                                                                               | high         | duplicate (tier,scope) CR rejected at apply; changing `tier` rejected; different tiers coexist                                          | ⬜     |
| P1-T9  | **Cloud GSA → viewer-only IAM** (the real cloud-write delta) | 07 Phase 1             | `k8s-operator/scripts/provision_04_gcp_iam.sh` (default set 156-193; project bindings 96-101), `common.sh:163` (`PLATFORM_AGENT_PERMISSION_SET` → read-only), `teardown_04_gcp_iam.sh:83-96` (reconcile)                                                       | load-bearing | GSA has no `container.clusterAdmin/admin`, `monitoring.admin`; `gcloud ... get-iam-policy` shows viewer-only (scratch GKE)              | ⬜     |
| P1-T10 | Actuation pipeline (`apply.yml`)                             | 06 §4, 05 §1 C7, 03 §4 | `examples/gitops-repo/.github/workflows/apply.yml` (on merge: `kubectl apply` KCC / `terraform apply` HCL for changed `fleet/**`+`clusters/**`; least-priv per-target creds)                                                                                   | high         | merged artifact is applied by the pipeline (the sole privileged writer); agent holds no write creds                                     | ⬜     |
| P1-T11 | Pin controller image + deploy manifests                      | 05 §3                  | `k8s-operator/config/manager/{manager.yaml:44,kustomization.yaml:5-8}`; agent image tag in the CR                                                                                                                                                              | medium       | controller + agent images pinned to immutable tag/digest                                                                                | ⬜     |
| P1-T12 | **Spike:** controller pod-construction → Scion launch prim.  | 08 §2                  | `platformagent_manifests.go` (buildDeployment ~280-540)                                                                                                                                                                                                        | low          | spike wired with **fallback to native Deployment build** if Scion K8s mode absent                                                       | ⬜     |

> **T13 (trusted-human lockdown, 03 §4a / 08 §2 item9)** is folded into **T6**: the deployed platform
> Agent CR must set an explicit **closed `allowedUsers`** for each enabled chat platform (empty = "all
> authenticated users", which is the open default we must close). The mechanism already exists and is
> rendered to pod env (`platformagent_manifests.go:406-445`); this is a required **config value**, not a
> code change.

---

## Key facts the mappers surfaced (do not relearn the hard way)

- **The operator render is authoritative, the baked configs are shadowed.** `renderConfigYAML()` writes
  `config.yaml` into a ConfigMap mounted **over** `/opt/data/config.yaml` (subPath). Editing **only**
  `agents/platform/config.yaml` / `deploy/shared/defaults/config.yaml` leaves the deployed agent
  write-capable. Fix the operator render **and** update the baked configs for consistency. (T1)
- **`create_cluster` has no local implementation.** It reaches the agent solely via the **remote `gke`
  MCP proxy** (`proxy.js → container.googleapis.com/mcp`) + the `mcp-gke` toolset. A remote MCP's toolset
  **cannot be subset client-side** — so the reliable removal is to **drop the server + toolset** (or front
  it with a genuine read-only allowlist proxy). Treat server + toolset as **one coordinated change**. (T1)
- **`developer_knowledge` is NOT a write path** — it's a `proxy.js` remote MCP to
  `developerknowledge.googleapis.com` (a read/knowledge API). Leave it. (T1)
- **The agent's K8s RBAC is already read-only** (runtime-minted `view` + get/list "explorer", no write
  verbs). The Phase 1 delta is **"stop minting at runtime"**, not "remove write verbs" (there are none).
  (T4)
- **The cloud GSA is the real cloud-write delta.** `PLATFORM_AGENT_PERMISSION_SET` defaults to
  `gke-admin` → `container.clusterAdmin` + `container.admin` + `monitoring.admin`, assumed by the pod via
  Workload Identity. This authorizes cloud mutation **even after** the tool surface is read-only. A
  read-only role set already exists in `provision_04_gcp_iam.sh:157-165` but is not the default. **This is
  the one every mapper missed.** (T9)
- **Canonical KSA name mismatch.** Today's sample CR / controller fallback use
  `kubeagents-platform-agent` / `agent.Name`; the Phase-0 pre-created overlay + VAP convention use
  **`platform-agent`** (canonical `<tier>-agent`). Converge on `platform-agent` everywhere. (T5/T6)
- **`config/rbac/role.yaml` and `zz_generated.deepcopy.go` / CRD bases are generated** — never hand-edit.
  Edit the kubebuilder markers / Go types then run `make manifests` / `make generate`. (T4/T5/T7)
- **CRD change is additive, not a rename.** 06 §1.1: "today's `PlatformAgent` becomes the platform-tier
  instance." Add fields to `AgentSpec` and default `tier=platform`; a true `PlatformAgent`→`Agent` Kind
  rename is disruptive and better sequenced with Phase 2's second tier. (T7)
- **Tests/fixtures that WILL break and must be updated:** `platformagent_controller_test.go:181-219`
  (asserts explorer/viewer RBAC created + deleted), `platformagent_manifests_test.go` (~1076/1115/1158,
  explorer role + CRB builders), golden `internal/testing/testdata/platform/expected/platformagent.yaml`
  (embeds viewer CRB + explorer ClusterRole/CRB). (T4)

---

## Deferred — do NOT build in Phase 1 (spec phasing)

- **`parentRef` cross-object checks** (correct parent tier; **child ⊆ parent** attenuation ceiling) →
  deferred to the hardening admission webhook (06 §1.2, 08 §5). Phase 1 adds the **field** only.
- **Per-request user-scoped authorization + the external ChatOps gateway** → deferred hardening
  (08 §5). Phase 1 does the **trusted-human allowlist** backstop only (T6/T13).
- **The other mutating `gke-*` skills** (observability, reliability, workload-scaling, …) contain
  `gcloud`/`kubectl apply` prose. 06 §9 names **only** `gke-cluster-creator` (+ lifecycle). Sweeping the
  rest into author-manifest-then-PR is a **follow-on pass** — do not expand Phase 1 unless directed.

---

## Verification plan for this phase

**Kind (inner loop):**

1. `make build` / `go build ./...` in `k8s-operator` after each Go change; `make generate manifests` for
   CRD/RBAC regen; run the controller unit tests (updating the fixtures listed above).
2. **Read-only tool surface (T1):** render config for a platform CR; assert no `gke` write server / no
   `mcp-gke` toolset; assert config mount `readOnly: true`.
3. **No-mint controller (T4/T5, 08 §7):** apply pre-created identity (T6) → deploy the controller →
   create a platform CR → assert the controller created **no** ClusterRole/ClusterRoleBinding/SA
   (only the Deployment/ConfigMap/PVC/Service it legitimately owns); assert `config/rbac/role.yaml` has
   no `clusterroles`/`clusterrolebindings` `create`/`bind`.
4. **Read-only identity still works:** `kubectl auth can-i --as=system:serviceaccount:…:platform-agent`
   → `list pods` yes, `create/delete` no, cross-tier read no.
5. **Cardinality webhook (T8):** two CRs with the same `(tier,scope)` → 2nd rejected; changing `tier` on
   an existing CR → rejected; a different `(tier,scope)` → admitted.
6. **VAP still green (03 §11, load-bearing):** re-run `local-dev/tests/negative-attenuation.sh` — Phase 0
   backstop must stay passing; the pre-created identity manifests (T6) must be VAP-clean.
7. **GitOps loop (A1/A3):** dry-run `submit_suggestion.py` on an authored KCC artifact → opens a PR
   (branch `platform-agent/...`, targeted files only, Conventional Commit); the PR is the audit record.

**Scratch GKE (identity/cloud only):**

8. **Cloud GSA viewer-only (T9, A2 — load-bearing):** provision with the read-only set →
   `gcloud projects get-iam-policy` shows the platform GSA has **no** `container.clusterAdmin/admin` or
   `monitoring.admin`; a `gcloud container clusters create` as that identity **fails**.
9. **Workload Identity binding** on the pre-created KSA resolves to the viewer-only GSA.

**Halt (do not auto-merge) if:** the VAP suite regresses, the controller mints any RBAC/SA, the cloud
GSA retains admin IAM, or a destructive test would touch a non-Kind/non-scratch context.

---

## Notes / open items

- **Scratch GKE incurs cost** — only T9's cloud-GSA assertion and the WI check use it; Kind covers the
  rest. If scratch GKE is unavailable in a given run, land the code + Kind verification and flag T9's
  cloud assertion as **pending scratch-GKE** in the PR (do not fake it green).
- **Migration window (roadmap risk row):** sequence T6/T7 before T4/T5 so the agent never loses its read
  identity mid-change.
- The customer GitOps target repo (`integration.github.gitRepo`) is external in prod; here it is the
  Phase-0 `examples/gitops-repo/` reference tree — `submit-suggestion` + `apply.yml` operate against it.
