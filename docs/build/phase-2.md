# Phase 2 — Cluster Admin Agent + cascade (task breakdown)

**Roadmap:** `docs/design/07-implementation-roadmap.md` §"Phase 2 — Cluster Admin Agent + cascade".
**Goal:** stand up the **second tier** (cluster-admin), **provisioned by the first** (platform) via the
GitOps cascade F4 — with the generic `Agent` CRD end-state, a deterministic multi-tier ChatOps router,
and the spoke bootstrap that resolves the cert-manager/controller chicken-and-egg. Every mutation still
flows only through a reviewed PR actuated by CI/CD; agents stay read-only.

**Phase acceptance (07 §2 "Accept") — decomposed a–e:**

- **(a)** Platform Agent **proposes** a cluster-admin agent (cascade F4); after human approval + merge,
  CI/CD applies it and the controller **runs** it.
- **(b)** The cluster-admin agent runs with a **read-only cluster identity** and can read **only its own
  cluster** (no write verbs; no other cluster).
- **(c)** A **slash command** / **`@cluster-<c>`** handle routes to it **without an inference call**.
- **(d)** A message from a **non-`allowedUsers`** requester is **refused before dispatch**.
- **(e)** RBAC granting an agent SA a **write verb** or a **wrong-scope** binding is **rejected at apply
  time by the `ValidatingAdmissionPolicy`**, even if merged.

**Touched Verification suites:** 06 §10 (contract/layout, cardinality, read-only identity, router
derive-from-cardinality-key), 08 §7 (pre-created identity; controller mints no RBAC), 05 §7 (spoke
bootstrap ordering), 05 §4 (F4 cascade), **03 §11 (attenuation — the load-bearing negative suite)**.
**Load-bearing subset active this phase:** 03 §11 (VAP read-verb allow-list + wrong-scope deny; read-only
SAR; trusted-human refusal-before-dispatch; no-write-tools on rendered config; no-break-glass) **plus**
the 08 §7 "controller mints no RBAC" assertion and the scratch-GKE cloud-GSA-viewer-only + cross-cluster
egress checks.

**Source of the breakdown:** two Explore agents mapped the current routing internals + the exact Phase 2
spec requirements, then a design judge-panel workflow (3 architects × 3 judges + synthesis) resolved the
two load-bearing architectural decisions below (CRD-rename approach; router topology). The synthesis
closed critical gaps the naive plans missed: a **standalone `kage-router`** (in-pod fan-in cannot route
between pods), a **`PubSubDispatcher`** cross-cluster transport that needs **zero edit** to the
security-sensitive `credential_proxy.py` sidecar (closing the unauthenticated-endpoint risk), a
**fail-closed before-dispatch allowlist** + the **closed pod-env backstop** (closing double-ingress), and
**pinned** cert-manager + controller bundles.

---

## Architecture decisions (load-bearing — resolved before breakdown)

**Decision 1 — CRD: hard-rename `PlatformAgent` → generic `Agent`, folding into the existing `AgentSpec`.**
06 §1/§1.1 + 08 §2 mandate **one** generic tier-discriminated `Agent` CRD; Phase 1 deferred the rename to
here. The common `AgentSpec` (`common_types.go`) already carries `Tier`/`Scope`/`ParentRef`/`IAC`/
`Deployment`/`Security`; `PlatformAgentSpec` merely inlines it and adds `Harness`+`Integration`. So we
**fold `Harness`+`Integration` up into the existing `AgentSpec` and delete the wrapper** — the fewest
renames, no name collision (the widely-used common `AgentSpec` is **not** renamed). No second Kind, no
cross-Kind deprecated alias, **no conversion webhook** (conversion webhooks convert between _versions_ of
one Kind, never between two Kinds sharing storage — so hard-rename is the only clean path). Safe because
`v1alpha1` is pre-GA, zero external CR authors, harness-controlled fleet → delete-old-CRD + re-apply, not
a data migration. The VAP is label-selected (`kube-agents/tier` + `<tier>-agent` SA name) so it is
**rename-immune**; the cardinality webhook keys on `(tier,scope)` not Kind, so its logic is unchanged.

**Decision 2 — Router: a standalone `kage-router` Deployment that OWNS chat ingress.** A new Deployment in
`kubeagents-system` (own KSA, read-only `list/watch` on `Agent` CRs), binary at `k8s-operator/cmd/router`,
core at `k8s-operator/internal/router`. Standalone is the **only** topology that can enforce
`allowedUsers` **before** dispatch across multiple per-tier pods and resolve `(tier,scope)` centrally
(the in-pod model is single-agent fan-in: replicas=1, one subscription, nothing routes between pods). A
controller-runtime **informer over `Agent` CRs builds the routing table** keyed by
`agentindex.ScopeIdentity` — the **same extracted function the cardinality webhook uses**, so router and
webhook cannot drift by construction. Dispatch is a pluggable `Dispatcher`: `FakeDispatcher` (proves
c/d in tests) and **`PubSubDispatcher`** (production + cross-cluster: re-publish the normalized event to
the target CR's `spec.integration.googleChat.topicName`, which the target pod's own subscription drains —
Pub/Sub is global, so this is cross-cluster-native where a per-agent ClusterIP Service is not, and it
needs **zero** `credential_proxy.py` edit). `Authorize` runs **before** any `Dispatch`, **fail closed**
(empty/absent `allowedUsers` ⇒ refuse all; does **not** honor the pod-env `*_ALLOW_ALL_USERS` escape).
The per-pod `credential_proxy` + Hermes stack stays **unchanged** as defense-in-depth; if the router is
absent the per-pod path still works. Exercised hermetically on Kind via the **Pub/Sub emulator**
(tested-path == shipped-path) and live on scratch GKE.

**Spoke bootstrap (05 §7).** The cluster-provisioning PR carries a bundle under `clusters/<c>/bootstrap/`,
applied by `apply.yml` as kustomize overlays: **(1)** cert-manager **pinned v1.14.x** (raw manifest at
digest); **(2)** the controller bundle rendered by `kustomize build config/default` and committed as a
**pinned** manifest at the released image tag+digest + its cert-manager-CA-injected
`ValidatingWebhookConfiguration` + the in-tree VAP (needs k8s ≥ 1.30, no cert-manager); **(3)**
`kage-router`; **then** `clusters/<c>/agents/` (pre-created read-only identity, then the `Agent` CR).
**Strict ordered apply waves** with `kubectl wait` gates: cert-manager Ready → controller
CRDs+RBAC+webhook serving → **VAP + binding** → router → **pre-created identity** (must pass the
just-installed VAP) → **`Agent` CR last**. VAP **before** identity so bad-RBAC is rejected even during
provisioning; identity **before** the CR so `serviceAccountName` resolves and the pod always has a read
identity (Phase-1 identity-before-pod invariant). **Single-cluster collapse:** Phase 2 is proven on **one**
Kind cluster (k8s ≥ 1.30) playing hub+spoke; only cloud Workload Identity + cross-cluster spoke→hub
networking are reserved for scratch GKE.

**Cascade F4 (05 §4).** A trusted human message routed to `@platform-<projectId>` (mode 1/2, no
inference, allowlist checked before dispatch) — or a push trigger — asks the Platform Agent to onboard
cluster `<c>`. The read-only Platform Agent **mints nothing**; it invokes a **deterministic** render skill
keyed purely on `tier=cluster-admin` + `scope{projectId, clusterName=<c>}`, producing **one** GitOps
bundle: the cluster-admin `Agent` CR (`tier` immutable, `scope`, `parentRef`→platform, closed
`allowedUsers`, `serviceAccountName: cluster-admin-agent`) + the pre-created read-only identity (KSA +
`get/list/watch` ClusterRole + CRB, all `kube-agents/tier: cluster-admin` labeled, **plus the WI
annotation** to the viewer-only GSA) + the bootstrap bundle. The agent opens **one** PR via
`submit-suggestion` (never applies; holds no apply creds). Human review + merge → CI/CD applies in the
strict order; the identity hits the VAP at apply time (a write-verb or wrong-scope grant is **denied even
though merged** — acceptance e). The controller reconciles the pod: `resolveAgentImage` returns
`cluster-admin-agent:<tag>` by tier, the pod binds the pre-created read-only SA (acceptance b), controller
mints no RBAC. The router's informer observes the new CR → `@cluster-admin-<c>`/`@cluster-<c>` + slash
resolve to `(cluster-admin,<c>)` with no inference (c), enforcing that CR's `allowedUsers` before dispatch
(d).

---

## Ordering / dependency rule (critical)

**The CRD rename is ONE atomic, regen-verified unit and lands FIRST.** CRD name, controller
`For(&Agent{})`, and the webhook path change together or admission **silently** breaks — the rename must
not merge half-done. Order: **P2-T1 → (P2-T2 ∥ P2-T3) → P2-T4 → P2-T5 (regen + delete old CRD) → P2-T6
(fixtures + value-string assertions) → P2-T7 (live post-rename webhook-serving gate)**. No other track may
build against the API type until **P2-T6 is green** (avoids double-migrating half-written code).

After P2-T6, three streams run in **parallel**: **(i)** image+persona+identity (P2-T8→T11, T9→T10,
T12→T13); **(ii)** router (P2-T14→T15→T16→T17→T18→T19; P2-T14 needs only the `agentindex` extraction from
P2-T3); **(iii)** cascade (P2-T20 gates on P2-T12). Then bootstrap (P2-T21 gates on T5+T10+T12) → egress
(P2-T22). Kind verify (P2-T23 gates on T7,T11,T13,T18,T20,T21) → scratch GKE verify (P2-T24, only after
Kind is fully green) → docs (P2-T25) last.

> **STANDING BUILD-GREEN CONTRACT.** Every task must leave the tree **green** (compile + unit/golden)
> **and** must not regress the **03 §11 negative suite**. If a task cannot land without breaking green it
> is **split** until it can.
>
> **IDENTITY-BEFORE-POD invariant** (inherited from Phase 1): the pre-created read-only identity **and its
> VAP acceptance** must be provable **before** the controller reconciles a cluster-admin pod.
>
> **No task that weakens a negative check** (VAP admits write/wrong-scope, SAR shows a write, router
> dispatches a non-allowed sender or spends an inference call in mode 1/2, controller mints RBAC) may
> merge, regardless of other progress.

---

## Tasks

| ID     | Task                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | Implements                                                              | Files                                                                                                                                                                                                                                                                                | Risk         | Acceptance signal                                                                                                                                                                                                                                                 | Status |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| P2-T1  | **Fold** `Harness`+`Integration` into the existing common `AgentSpec`; delete `PlatformAgentSpec`; rename `PlatformAgent`→`Agent`/`AgentList`, `PlatformAgentIntegrationSpec`→`AgentIntegrationSpec`; regen deepcopy. Do **not** rename the common `AgentSpec`.                                                                                                                                                                                                                                               | 06 §1/§1.1, 08 §2; Decision 1A-fold                                     | `api/v1alpha1/platformagent_types.go`→`agent_types.go`, `common_types.go`, `groupversion_info.go`, `zz_generated.deepcopy.go`                                                                                                                                                        | load-bearing | `make generate` clean; `go build ./...` passes; `Agent`/`AgentList` registered; single `AgentSpec`; deepcopy regenerated (not hand-edited)                                                                                                                        | ☑      |
| P2-T2  | Rename reconciler → `AgentReconciler`; `SetupWithManager` `For(&Agent{})`; update manifests/helpers/pod_launcher refs; `buildPlatformService`→`buildAgentService`.                                                                                                                                                                                                                                                                                                                                            | 06 §1.1 single controller                                               | `internal/controller/platformagent_controller.go`→`agent_controller.go`, `platformagent_manifests.go`→`agent_manifests.go`, `manifest_helpers.go`, `pod_launcher.go`                                                                                                                 | load-bearing | controller compiles + reconciles `Agent`; controller unit tests pass after the P2-T6 value-string fixes                                                                                                                                                           | ☑      |
| P2-T3  | Rename webhook → `AgentCustomValidator/Defaulter`; change `+kubebuilder:webhook` paths to `/validate\|mutate-kubeagents-x-k8s-io-v1alpha1-agent`; **extract** `scopeIdentity`/`effectiveTier` into a shared `internal/agentindex` package (used by webhook **and** router); keep cardinality + tier-immutability + closed-allowlist checks.                                                                                                                                                                   | 06 §1.2; 06 §2b derive-from-key; agentindex graft                       | `internal/webhook/platformagent_webhook.go`→`agent_webhook.go` (+`_test.go`), `internal/agentindex/identity.go` (new) (+`_test.go`)                                                                                                                                                  | load-bearing | webhook unit tests pass; duplicate `(tier,scope)` rejected; `tier` update rejected; `agentindex.ScopeIdentity` unit-tested + imported by webhook and (later) router                                                                                               | ☑      |
| P2-T4  | Add per-tier **scope + parentRef** validation via `agentindex`: cluster-admin CR **must** carry `scope.projectId`+`clusterName` and `parentRef=platform`; controller still mints **no** RBAC (references pre-created SA only).                                                                                                                                                                                                                                                                                | 06 §1.2 required scope/parentRef; 08 §4/§7                              | `internal/webhook/agent_webhook.go`, `internal/agentindex/identity.go`, `internal/controller/agent_manifests.go`, `agent_controller.go`                                                                                                                                              | load-bearing | webhook rejects a cluster-admin CR missing `clusterName` or `parentRef`; controller creates no Role/RoleBinding/ClusterRole                                                                                                                                       | ☑      |
| P2-T5  | Regen CRD bases + controller RBAC + webhook manifests for the `agents` resource; **delete** stale `platformagents` CRD; fix `config/crd/kustomization`, `config/default`, `config/webhook` name/namePrefix refs.                                                                                                                                                                                                                                                                                              | 06 §1, §10; generated-artifact correctness                              | `config/crd/bases/kubeagents.x-k8s.io_agents.yaml` (new), `..._platformagents.yaml` (delete), `config/crd/kustomization.yaml`, `config/rbac/role.yaml`, `config/webhook/manifests.yaml`, `config/default/kustomization.yaml`                                                         | load-bearing | `make manifests` clean; diff is only rename churn; `agents` CRD present, `platformagents` gone; controller RBAC lists `agents`; webhook path `/validate-…-agent`                                                                                                  | ☑      |
| P2-T6  | Migrate golden fixture + testutil loader + examples + templates + fleet CR to `kind: Agent`; fix the **value-string** assertions a symbol rename won't touch (the 7 `.Kind=="PlatformAgent"` owner-ref checks in the controller test + pod_launcher test); grep the **whole tree** for literal `PlatformAgent`; `golden -update` + review the semantic diff.                                                                                                                                                  | golden semantic parity; rename correctness                              | `internal/testing/testdata/platform/expected/platformagent.yaml`→`agent.yaml`, `golden_test.go`, `testutil/testutil.go`, controller+pod_launcher `_test.go`, `examples/platformagent.yaml`, `scripts/platform-agent.yaml.template`, `examples/gitops-repo/fleet/platform-agent.yaml` | high         | `go test ./...` green; golden `cmp.Diff` shows only `kind`/`ownerRef.kind` changed; grep for literal `PlatformAgent` returns only intentional/historical hits                                                                                                     | ☑      |
| P2-T7  | **Live** post-rename webhook-serving + admission gate on Kind (distinct from unit tests): apply the new `agents` CRD; confirm `/validate-…-agent` serves with cert-manager CA injected; live-admit a duplicate `(tier,scope)` rejection + a tier-immutability PATCH rejection. Closes the silent-admission-failure gap.                                                                                                                                                                                       | rename cutover safety; 06 §1.2 live admission                           | `dev/verify/verify-rename.sh` (new), `docs/build/phase-2.md`                                                                                                                                                                                                                         | high         | on Kind: `kubectl get agents` works, `platformagents` CRD gone; webhook serving; duplicate `(tier,scope)` apply **REJECTED** live; `tier` PATCH **REJECTED** live                                                                                                 | ☑      |
| P2-T8  | Make `resolveAgentImage` **tier-aware**: replace the single `defaultPlatformAgentImage` fallback with a tier→default map (`platform`→`platform-agent`, `cluster-admin`→`cluster-admin-agent`); `spec.deployment.image` still overrides.                                                                                                                                                                                                                                                                       | 07 §2(a); per-tier baked image v1                                       | `internal/controller/manifest_helpers.go` (+`_test.go`), `agent_manifests.go`                                                                                                                                                                                                        | medium       | unit: `tier=cluster-admin`, no explicit image → `cluster-admin-agent:<tag>`; `tier=platform` unchanged; explicit image overrides                                                                                                                                  | ☑      |
| P2-T9  | Author `agents/cluster-admin/` persona (`SOUL.md`, `config.yaml`, `AGENTS.md`, `skills/`, `cron`, `governance`, `defaults`, `scripts`) mirroring `agents/platform/`; cluster-scoped **read-only** mandate, **no** write/mutating tools.                                                                                                                                                                                                                                                                       | 07 §2(a); 03 §3/§11 read-only                                           | `agents/cluster-admin/**`                                                                                                                                                                                                                                                            | medium       | baked `config.yaml` has only read tools; persona scoped to one cluster read-only (the real gate is the rendered-config assertion in P2-T11)                                                                                                                       | ☑      |
| P2-T10 | Add Dockerfile stage `FROM agent-base AS cluster-admin` (COPY `agents/cluster-admin/*`, merge `config.yaml`) + its credential-proxy variant; pin base digest in `tags.env`; confirm Makefile auto-discovery builds `docker-build-cluster-admin`; **GATE** that the shared `FROM platform AS credential-proxy` image still builds unchanged.                                                                                                                                                                   | 07 §2(a) baked image build                                              | `deploy/docker/Dockerfile`, `tags.env`, `merge_configs.py`, `Makefile`                                                                                                                                                                                                               | medium       | `make docker-build-cluster-admin` → `cluster-admin-agent:<tag>`; `make docker-build-credential-proxy` still succeeds; Makefile `AGENTS` includes cluster-admin                                                                                                    | ☑      |
| P2-T11 | Per-tier **rendered** golden fixture for cluster-admin: add `testdata/cluster-admin` input+expected, iterate tiers in `golden_test`; assert the **operator-rendered** pod config (not just baked `config.yaml`) has **zero mutating tools**, binds the `cluster-admin-agent` SA + image, **and** the pod's `GOOGLE_CHAT_ALLOWED_USERS`/`SLACK_ALLOWED_USERS` backstop renders **CLOSED** (never `*_ALLOW_ALL_USERS`).                                                                                         | 03 §11 no-write-tools on rendered config; acceptance (b)                | `internal/testing/testdata/cluster-admin/{input.yaml,expected/agent.yaml}` (new), `golden_test.go`, `internal/controller/agent_manifests_test.go`                                                                                                                                    | load-bearing | golden semantic compare passes for cluster-admin; rendered config asserted free of write tools, bound to `cluster-admin-agent` SA + image, backstop allowlist closed                                                                                              | ☑      |
| P2-T12 | Author the cluster-admin `Agent` CR + pre-created read-only identity by **reusing/extending** the existing `examples/gitops-repo/policy/rbac-overlay/cluster-admin.yaml` (KSA + `get/list/watch` ClusterRole + CRB, tier-labeled) and **adding** the missing Workload-Identity annotation to the viewer-only GSA; place both under `clusters/<cluster>/agents/` with a kustomization.                                                                                                                         | 07 §2(b); 06 §2 / 03 §3 identity; 05 §7 layout                          | `clusters/<cluster>/agents/agent.yaml` (new), `.../identity/` (extends `rbac-overlay/cluster-admin.yaml` + WI annotation), `.../kustomization.yaml` (new), `examples/gitops-repo/policy/rbac-overlay/cluster-admin.yaml`                                                             | load-bearing | `kubectl apply --dry-run=server` admits all; VAP admits (read-only + correct scope); CR has `tier=cluster-admin`, `scope.clusterName+projectId`, `parentRef=platform`, closed `allowedUsers`, `serviceAccountName cluster-admin-agent`; KSA carries WI annotation | ☑      |
| P2-T13 | **VAP verification + negative fixtures**: confirm the existing `vap-agent-readonly.yaml` selector (`kube-agents/tier` label) is rename-immune and `failurePolicy=Fail`; add negatives — a cluster-admin-labeled ClusterRole with a **write** verb (`create`) and a **developer-team**-labeled ClusterRole (**wrong-scope**) — plus a positive read-only fixture; wire the VAP into Kind + bootstrap.                                                                                                          | 03 §4/§11 attenuation; acceptance (e)                                   | `examples/gitops-repo/policy/vap-agent-readonly.yaml`, `.../tests/vap_clusteradmin_negatives.yaml` (new), `.../tests/vap_positive.yaml` (new)                                                                                                                                        | load-bearing | write-verb cluster-admin ClusterRole **DENIED** at apply; developer-team ClusterRole **DENIED** as wrong-scope; read-only role **ACCEPTED**; `failurePolicy=Fail` confirmed; denial provably from the VAP                                                         | ☑      |
| P2-T14 | Router **pure core**: `internal/router` grammar (slash + `@handle` + aliases) + `Resolve(text)→(tier,scope,mode)` + `Authorize(target,sender)→(bool,reason)` + `Ingress`/`Dispatcher` interfaces; an **`inference_calls` counter**; table tests asserting `inference_calls==0` for all mode-1+2 paths. `Resolve` uses `agentindex.ScopeIdentity` for the key.                                                                                                                                                 | 06 §2b modes 1+2; acceptance (c); inference_calls metric                | `internal/router/{grammar,resolve,authorize,types}.go` (new) + `resolve_test.go` (new)                                                                                                                                                                                               | load-bearing | unit: `@cluster-<c>` and `@kage /cluster-admin-<c>` both → `(cluster-admin,c)` with mode set and `inference_calls==0`; unknown/ambiguous → deterministic refusal                                                                                                  | ☑      |
| P2-T15 | Router **informer/index** over `Agent` CRs keyed by `agentindex.ScopeIdentity` — the routing table **IS** the CR list, no separate registry; `(tier,scope)`→exactly one `Agent` CR; zero matches → deterministic unknown-target refusal.                                                                                                                                                                                                                                                                      | 06 §2b derive-from-key; Decision 2                                      | `internal/router/index.go` (new) (+`_test.go`)                                                                                                                                                                                                                                       | high         | index reuses `ScopeIdentity`; envtest: apply platform+cluster-admin CRs → table resolves both; delete → resolution refuses                                                                                                                                        | ☑      |
| P2-T16 | **Before-dispatch** allowlist check + audit: read target CR `integration.{googleChat,slack}.allowedUsers` from cache; **fail closed** on empty/absent; refuse non-members **before** any `Dispatcher` call; emit an audit record `{sender,tier,scope,mode,decision}` with a defined sink + schema test.                                                                                                                                                                                                       | 06 §2b before-dispatch; acceptance (d); 03 §11 trusted-human            | `internal/router/authorize.go`, `audit.go` (new), `authorize_test.go` (new)                                                                                                                                                                                                          | load-bearing | unit: sender not in allowlist → refuse, `FakeDispatcher.callCount==0`; empty allowlist → refuse-all; member → allowed; audit fields asserted against the sink                                                                                                     | ☑      |
| P2-T17 | **Dispatcher impls**: `FakeDispatcher` (records callCount+target+payload) and **`PubSubDispatcher`** (production + cross-cluster: re-publish normalized event to the target CR's `spec.integration.googleChat.topicName`; **no** `credential_proxy.py` edit). Define **ingress ownership**: router owns the chat-platform ingress subscription; per-agent pods drain only the router-forwarded per-agent topic (prevents double-processing).                                                                  | 06 §2b dispatch/pod-addressing; PubSubDispatcher                        | `internal/router/dispatch.go`, `dispatch_fake.go`, `dispatch_pubsub.go`, `dispatch_pubsub_test.go` (all new)                                                                                                                                                                         | high         | unit/emulator: `PubSubDispatcher` publishes to the resolved target topic; `FakeDispatcher` records target+payload; `credential_proxy.py` **UNCHANGED**; ingress-ownership cutover documented                                                                      | ☑      |
| P2-T18 | Router **envtest integration**: apply `Agent` CRs, drive `HTTPTestIngress` + `FakeDispatcher`; assert `@handle` **AND** slash both resolve to `(cluster-admin,c)` with `inference_calls==0`, and non-allowlist + empty-allowlist senders refused with `callCount==0`. Plus a **Pub/Sub-emulator** test of the real `PubSubDispatcher` happy path.                                                                                                                                                             | acceptance (c),(d); 03 §11 trusted-human; emulator                      | `internal/router/integration_test.go` (new), `testdata/` (new)                                                                                                                                                                                                                       | load-bearing | envtest proves (c) both modes no-inference and (d) refusal-before-dispatch entirely on Kind; emulator proves `PubSubDispatcher` tested-path==shipped-path                                                                                                         | ☑      |
| P2-T19 | **`kage-router` Deployment** + KSA + read-only RBAC (`list/watch agents` only) + `config/router` wiring; `HTTPTestIngress` endpoint (flag-guarded); live Google Chat Pub/Sub + Slack Socket Mode ingress adapters as thin shells over the core (cloud-only live tests).                                                                                                                                                                                                                                       | 07 §2(d) central router; 07 Risks incremental                           | `cmd/router/main.go` (new), `config/router/{deployment,rbac,kustomization}.yaml` (new), `internal/router/ingress_{gchat,slack}.go` (new)                                                                                                                                             | high         | `kage-router` runs on Kind with viewer-only RBAC on `agents`; `HTTPTestIngress` drives full normalize→resolve→authorize→dispatch; per-pod path unaffected                                                                                                         | ☑      |
| P2-T20 | **Cascade F4 skill**: `agents/platform/skills/propose-cluster-admin/` renders the cluster-admin `Agent` CR + read-only identity (+ bootstrap bundle) **deterministically** from tier+scope and opens **one** PR via `submit-suggestion` (read-only propose path; no runtime apply/mint). Add a Kind **dry-run** render acceptance.                                                                                                                                                                            | 05 §4 F4; 07 §2(e); acceptance (a)                                      | `agents/platform/skills/propose-cluster-admin/{SKILL.md,scripts/**}` (new), `examples/gitops-repo/knowledge/cluster-blueprint/**` (new), `agents/platform/config.yaml`                                                                                                               | load-bearing | dry-run render for `scope{project,cluster=<c>}` emits one PR bundle (CR+identity+bootstrap) under `clusters/<c>/` that YAML-validates and whose identity passes VAP `--dry-run`; no runtime mint/apply                                                            | ☑      |
| P2-T21 | **Spoke bootstrap bundle**: pinned cert-manager **v1.14.x** (raw manifest at digest) + pinned controller bundle (kustomize-rendered at released image tag+digest) + VAP + `kage-router` + `clusters/<self>/agents/`, with strict ordered CI/CD apply + `wait` gates; single-cluster collapse overlay for Kind.                                                                                                                                                                                                | 05 §7 spoke bootstrap; pin reproducibility                              | `clusters/<cluster>/bootstrap/{kustomization,cert-manager,controller,vap}.yaml` (new, pinned), `examples/gitops-repo/.github/workflows/apply.yml`, `dev/cluster/up.sh`                                                                                                               | high         | on a fresh Kind cluster the bundle brings up cert-manager→controller(webhook serving)→VAP→router→identity→cluster-admin pod **in order**; out-of-order (CR before identity, or identity before VAP) **fails** as expected; versions pinned                        | ☑      |
| P2-T22 | **Cross-cluster egress NetworkPolicy**: default-deny egress on the cluster-admin namespace, allow **only** spoke→hub Inference + Minty; validate on Kind CNI + scratch GKE.                                                                                                                                                                                                                                                                                                                                   | 07 Risks Phase-2 bootstrap networking                                   | `clusters/<cluster>/bootstrap/networkpolicy-egress.yaml` (new), `dev/kind/kind-config.yaml`                                                                                                                                                                                          | medium       | default-deny egress in place; egress reaches exactly Inference+Minty; all other egress blocked (Kind NP + scratch GKE)                                                                                                                                            | ☑      |
| P2-T23 | **Kind single-cluster hub+spoke verification harness**: controller+VAP+router+both agents; SAR `can-i` read-only suite **including** a positive "no other cluster" proof (assert the cluster-admin SA has no foreign kubeconfig/token AND run the optional two-Kind cross-cluster read-deny); operator-rendered no-write-tools; VAP negatives; router routing/refusal; no-break-glass grep. **Retire/extend** `tests/e2e/gchat_agent_test.py` to cover tier resolution + `@handle`/slash + allowlist refusal. | 07 §2 Accept (b,c,d,e); 03 §11 full negative suite                      | `dev/verify/verify-phase2.sh` (new), `tests/e2e/sar_readonly_test.sh` (new), `tests/e2e/gchat_agent_test.py` (retire/extend), `tests/e2e/vap_negative_test.sh` (new)                                                                                                                 | load-bearing | SAR `get/list/watch=yes` in-cluster, `create/update/delete=no`, foreign-cluster read **DENIED** (or explicit no-cred asserted + deferred to GKE); rendered config no-write; VAP denials; router (c)/(d); no-break-glass — **all green or HALT**                   | ☑      |
| P2-T24 | **Scratch GKE (`adamparco-kage`) cloud validation**: cluster-admin KSA↔GSA Workload Identity is cluster-scoped **viewer-only** (no write IAM); cross-cluster spoke→hub Inference+Minty over private with default-deny egress; **live** Google Chat slash+`@handle` routing + before-dispatch refusal; **live cascade F4** end-to-end (PR→merge→CI/CD apply→reconcile read-only pod).                                                                                                                          | 07 §2 Accept cloud portions; 08 §7 cloud identity; (a) live + (b) cloud | `dev/gke-scratch/verify-phase2.sh` (new), `docs/build/phase-2.md`                                                                                                                                                                                                                    | load-bearing | `gcloud … get-iam-policy` shows GSA viewer-only; egress reaches only Inference+Minty; live chat routes+refuses; Platform Agent PR merges and controller reconciles read-only pod                                                                                  | ☐      |
| P2-T25 | Author `docs/build/phase-2.md` (house style: task table, ordering rule, deferred, verification, HALT) incl. the **CRD-retirement runbook**; update `INSTALL.md`, `docs/build/LEDGER.md` resume point, and MEMORY `design-specs-effort` notes.                                                                                                                                                                                                                                                                 | house style; build ledger; CRD-retirement runbook                       | `docs/build/phase-2.md`, `INSTALL.md`, `docs/build/LEDGER.md`                                                                                                                                                                                                                        | low          | phase-2.md matches phase-1.md structure; runbook documents `platformagents` CRD retirement without orphaning the platform agent; LEDGER resume point updated                                                                                                      | ☑      |

---

## Key facts the design panel surfaced (do not relearn the hard way)

- **A cross-Kind "deprecated alias" is not a real Kubernetes capability.** Conversion webhooks convert
  between _versions_ of one Kind, never between two Kinds sharing storage. Hard-rename + delete-old-CRD is
  the only clean path — safe here only because `v1alpha1` is pre-GA with zero external CR authors and a
  harness-controlled fleet. (P2-T1/T5)
- **The rename is symbol churn PLUS value-string churn.** A Go symbol rename does **not** touch the 7
  `.Kind=="PlatformAgent"` owner-ref **string** assertions in the controller/pod_launcher tests, the
  golden fixture `kind:`, or the `+kubebuilder:webhook` **path** string. Grep the whole tree for the
  literal `PlatformAgent`. (P2-T6)
- **`zz_generated.deepcopy.go`, `config/rbac/role.yaml`, and CRD bases are generated** — never hand-edit;
  edit the Go types / kubebuilder markers then `make generate` / `make manifests`. (P2-T1/T5)
- **The operator render is authoritative; baked configs are shadowed** (inherited Phase-1 fact). The real
  no-write-tools + closed-allowlist gate is the **rendered** golden fixture, not `agents/cluster-admin/
config.yaml`. (P2-T11)
- **The VAP is rename-immune and the load-bearing backstop.** It is label-selected (`kube-agents/tier` +
  `<tier>-agent` SA name) and keys on the read-verb allow-list, so the rename does not touch it — but it
  **must** stay `failurePolicy=Fail` and the target cluster **must** be k8s ≥ 1.30 or the attenuation
  guarantee is gone. (P2-T13)
- **In-pod fan-in cannot route between pods.** The existing relay is single-agent (replicas=1, one
  subscription). Multi-tier routing + before-dispatch allowlist across pods **requires** the standalone
  `kage-router`. (P2-T19)
- **`PubSubDispatcher`, not HTTP-to-Service.** A per-agent ClusterIP Service is not resolvable across
  clusters, and an authenticated `/dispatch` endpoint on `credential_proxy.py` would enlarge the
  security-sensitive sidecar's attack surface. Re-publishing to the target CR's existing Pub/Sub topic is
  cross-cluster-native and needs **zero** sidecar edit. (P2-T17)
- **Fail closed.** The router's `Authorize` reads the **target** CR's `allowedUsers` and refuses on
  empty/absent; it does **not** honor the pod-env `*_ALLOW_ALL_USERS` escape. The pod-env backstop stays
  live as defense-in-depth (double ingress is closed by the ingress-ownership cutover in P2-T17). (P2-T16)
- **Identity before pod, VAP before identity** (inherited invariant, now at bootstrap scale). The spoke
  apply order is cert-manager → controller(webhook serving) → VAP → router → identity → `Agent` CR.
  (P2-T21)

---

## Deferred — do NOT build in Phase 2 (spec phasing)

- **NL inference routing (router mode 3, 06 §2b)** → Phase 3. Phase 2 is deterministic slash+`@handle`
  only; `Resolve` **refuses** rather than infers on unknown/ambiguous input.
- **developer-team (third tier) end-to-end** → Phase 3. Only the `@developer-team-`/`@devteam-` grammar
  **stubs** are parsed; no dev-team image, CR, identity, or bootstrap is built.
- **Cross-object child ⊆ parent RBAC ceiling / attenuation webhook (06 §1.2, 08 §5)** → deferred
  hardening. VAP v1 scopes CEL to a role's **own** rules only.
- **Per-user confused-deputy / SAR user-scoped authorization (03 §4a, 08 §5)** → deferred hardening. The
  router enforces `allowedUsers` **membership** only, not per-user K8s SAR.
- **Mounted agent profile** → Phase 2 uses **baked per-tier images** only (`cluster-admin-agent:<tag>`).
- **gVisor execution sandbox (08 §5.1)** → deferred.
- **HTTP-to-per-agent-Service as the primary router→pod transport** → rejected (see Key facts);
  `HTTPTestIngress`/stub is used for Kind wiring smoke tests only.
- **Replacing the per-pod Pub/Sub drain / in-pod env allowlist** → stays live as defense-in-depth; the
  router is layered on top, not a replacement.
- **Router as a multi-cluster fleet singleton / real-spoke routing-table federation** → Phase 2 uses the
  single-cluster hub+spoke collapse; one router per chat-owning cluster.

---

## Verification plan for this phase

Each step maps to an acceptance criterion (a–e) and/or the 03 §11 negative suite. **Kind is the inner
loop; scratch GKE covers only cloud identity/WI + cross-cluster networking + live chat/cascade.**

**Kind (inner loop):**

1. **V-K0 — rename regression (precondition for all).** `make generate && make manifests` git-diff empty;
   `go build/vet/test ./...` green incl. regenerated deepcopy + semantic golden with `kind: Agent`; grep
   tree for literal `PlatformAgent` clean; `kubectl get agents` works, `platformagents` CRD gone
   (P2-T1..T6). _Enables (a–e)._
2. **V-K1 — post-rename admission serving (P2-T7).** `ValidatingWebhookConfiguration` path
   `/validate-…-agent` serves with cert injected; live-apply a duplicate `(tier,scope)` → **REJECTED**;
   PATCH `tier` → **REJECTED**. _Guards the silent-admission-failure gap._
3. **V-K2 — attenuation admission / acceptance (e) (P2-T13).** A cluster-admin-labeled Role/ClusterRole
   with a write verb (`create`) → **DENIED**; a developer-team-labeled ClusterRole (wrong-scope) →
   **DENIED**; a read-only role → **ACCEPTED**; confirm VAP `failurePolicy=Fail`. _Maps (e) + 03 §11
   attenuation._
4. **V-K3 — read-only per-tier SAR / acceptance (b) (P2-T23).** `kubectl auth can-i
--as=system:serviceaccount:kubeagents-system:cluster-admin-agent`
   `get/list/watch=yes`, `create/update/patch/delete=no` across core+apps+rbac; **plus** a positive "no
   other cluster" proof — assert the SA has no kubeconfig/token for any foreign cluster and (optional
   two-Kind) a foreign-cluster read is **DENIED**. _Maps (b) + 03 §11 read-only SAR._
5. **V-K4 — no-write-tools on RENDERED config (P2-T11).** Golden + `agent_manifests_test` assert the
   **operator-rendered** cluster-admin pod config has zero mutating tools, binds the `cluster-admin-agent`
   SA + image, and its `*_ALLOWED_USERS` backstop is **CLOSED**. _Maps 03 §11 no-write-tools._
6. **V-K5 — routing / acceptance (c) (P2-T18).** Router envtest — `@cluster-<c>` and
   `@kage /cluster-admin-<c>` both resolve to `(cluster-admin,c)` with `inference_calls==0`. _Maps (c)._
7. **V-K6 — trusted-human refusal / acceptance (d) (P2-T18).** Non-allowlist sender and empty-allowlist
   sender → refused **before** dispatch, `FakeDispatcher.callCount==0`, audit record emitted +
   schema-checked. _Maps (d) + 03 §11 trusted-human._
8. **V-K7 — dispatch tested-path==shipped-path (P2-T18).** Pub/Sub emulator proves the real
   `PubSubDispatcher` re-publishes to the resolved target topic hermetically.
9. **V-K8 — cascade dry-run / acceptance (a) inner-loop (P2-T20).** `propose-cluster-admin` render in
   dry-run → a single `submit-suggestion` PR bundle (CR+identity+bootstrap) under `clusters/<c>/` that
   YAML-validates and whose identity passes VAP `--dry-run`. _Maps (a) inner-loop._
10. **V-K9 — single-cluster hub+spoke bootstrap (P2-T21).** On a fresh Kind cluster apply the bundle in
    order (cert-manager → controller → VAP → router → identity → `Agent` CR); controller reconciles a
    cluster-admin pod bound to the `cluster-admin-agent` SA + `cluster-admin-agent:<tag>`; **out-of-order
    fails**. _Maps (a partial actuation) + (b)._
11. **V-K10 — no-break-glass (P2-T23).** Grep controller/router for `create/update/patch` on rbac
    resources → none; confirm neither binary holds apply credentials; only merged-PR/CI applies writes.
    _Maps 03 §11 no-break-glass._
12. **V-K11 — egress (P2-T22).** Default-deny egress + allow exactly Inference+Minty on a supporting Kind
    CNI. _Maps 07 Risks (inner-loop portion)._

**Scratch GKE (`adamparco-kage`; identity/cloud + cross-cluster only):**

13. **V-G1 — cloud identity/WI / acceptance (b, cloud half) (P2-T24).** `cluster-admin-agent` KSA bound
    via Workload Identity to a **viewer-only** GSA; `gcloud … get-iam-policy` shows no write/admin IAM.
14. **V-G2 — cross-cluster networking (07 Risks) (P2-T24).** With default-deny egress, spoke→hub reaches
    **exactly** Inference + Minty over private; all other egress blocked. This is also where the "no other
    cluster" clause of (b) is proven against a real second cluster/private endpoints.
15. **V-G3 — live chat routing/refusal / acceptance (c),(d) live (P2-T24).** Real Google Chat slash +
    `@handle` from an authorized sender route to the cluster-admin pod with **no inference**; non-allowed
    sender refused **before** dispatch.
16. **V-G4 — live cascade F4 / acceptance (a) end-to-end (P2-T24).** Platform Agent opens the
    cluster-admin PR → human merge → CI/CD applies bootstrap in order → spoke controller reconciles the
    read-only pod.

> **Scratch GKE incurs cost and is destructive-adjacent** — only V-G1..G4 use it; Kind covers the rest.
> Destructive tests **never** touch prod; scratch GKE only for cloud identity/WI + cross-cluster
> networking + live chat/cascade. If scratch GKE is unavailable in a run, land the code + full Kind
> verification and flag V-G1..G4 as **pending scratch-GKE** in the PR (do not fake it green).

---

## Verification results (2026-07-24 run)

**Kind inner loop — ALL GREEN.** Full stack deployed to a **fresh** `kind-kube-agents-dev` (K8s
v1.31.2): cert-manager v1.14.7 → `make deploy` (CRD + controller + webhook + `kage-router`) → VAP →
identity → `Agent` CR, in bootstrap order. Re-runnable via `dev/verify/verify-phase2.sh`.

| Suite                          | Result  | Evidence                                                                                                                                                                                                                                                                           |
| ------------------------------ | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| V-K0 rename regression         | ✅ PASS | `make generate/manifests` diff-clean; `go build/vet/test ./...` green; `agents` CRD present, `platformagents` gone; whole-tree `PlatformAgent` grep clean                                                                                                                          |
| V-K1 webhook serving           | ✅ PASS | CA injected on `/validate-…-agent`; valid duplicate `(tier,scope)` → `Duplicate value: … (tier, scope) must be unique`; `tier` PATCH → `tier is immutable`                                                                                                                         |
| V-K2 VAP attenuation           | ✅ PASS | `negative-attenuation.sh`: write-verb Role, impersonate ClusterRole, wrong-scope ClusterRole all **DENIED** (adversarially confirmed from the policy message); read-only Role **ADMITTED**                                                                                         |
| V-K3 read-only SAR             | ✅ PASS | cluster-admin SA: get/list/watch=yes; create/update/patch/delete/deletecollection=no; impersonate/escalate/bind/create-clusterroles=no; `* *`=no                                                                                                                                   |
| V-K4 no-write-tools (rendered) | ✅ PASS | `TestAgentsGolden` + `agent_manifests_test` green (go test)                                                                                                                                                                                                                        |
| V-K5 routing (c)               | ✅ PASS | router `resolve` table tests — both modes → `(cluster-admin,c)`, `inference_calls==0` (go test)                                                                                                                                                                                    |
| V-K6 refusal (d)               | ✅ PASS | non-allowlist + empty-allowlist → refused before dispatch, `callCount==0`, audit emitted (go test)                                                                                                                                                                                 |
| V-K7 dispatch path             | ✅ PASS | Pub/Sub-emulator test of the real `PubSubDispatcher` (go test)                                                                                                                                                                                                                     |
| V-K8 cascade dry-run (a)       | ✅ PASS | `propose-cluster-admin` render → placeholder-free bundle; rendered identity **ADMITTED** by VAP `--dry-run`; write-verb tamper **DENIED** with the policy message                                                                                                                  |
| V-K9 bootstrap ordering        | ✅ PASS | out-of-order (Agent CR before CRD) **FAILS**; in-order reconciles `cluster-admin-cluster-a-gateway` bound to pre-created `cluster-admin-agent` SA + `cluster-admin-agent:v0.1.0` + tier label (pod Pending only on single-node memory — spec correct)                              |
| V-K10 no-break-glass           | ✅ PASS | controller/router ClusterRoles grant no write on rbac resources; neither binary holds apply creds                                                                                                                                                                                  |
| V-K11 egress                   | ✅ PASS | on a throwaway **Calico** Kind cluster (kindnet does not enforce): default-deny + `1.1.1.1/32` allowlist → allowed CIDR reachable, `8.8.8.8`/`9.9.9.9`/`169.254.169.254` **BLOCKED**; unlabeled pod unaffected (selector scoping); netpol selects the reconciled pod by tier label |

**Scratch GKE (V-G1..V-G4) — ⏸ PENDING.** The `adamparco-kage` project is reachable, but the
infrastructure these suites require is **not provisioned** and much of it belongs to later phases:
a cluster-admin viewer GSA + WI binding (V-G1), a **second spoke cluster with private hub
Inference/Minty endpoints** (V-G2, cross-cluster private is Phase 3+; the inference proxy is Phase 5),
a **live Google Chat app** (V-G3), and a **live CI/CD GitOps pipeline** wired to a real cluster (V-G4).
Per the plan's directive, the code + full Kind verification is landed and V-G1..G4 are flagged
**pending scratch-GKE** — not faked green. Every load-bearing **security negative** (attenuation,
read-only SAR, cardinality/immutability, egress enforcement incl. metadata-server block, no-break-glass)
is genuinely proven on Kind + Calico.

---

## HALT conditions (do not merge / do not auto-advance)

1. **Any 03 §11 negative check fails:** VAP **admits** a write-verb or wrong-scope RBAC (V-K2), SAR shows
   any write for the cluster-admin SA or a foreign-cluster read succeeds (V-K3), the router dispatches a
   non-allowlist/empty-allowlist sender (V-K6), the router spends an inference call in mode 1/2 (V-K5), or
   the controller/router mints RBAC or holds apply creds (V-K10) → **HALT**.
2. **V-K0 not green:** build/vet/test red, `make manifests` diff non-empty, unexplained golden diff, or a
   stray literal `PlatformAgent` in the tree → **HALT** (rename not clean).
3. **VAP absent or `failurePolicy != Fail`, or target cluster k8s < 1.30** → **HALT** (attenuation
   backstop not guaranteed).
4. **cluster-admin pod comes up bound to anything other than the pre-created read-only
   `cluster-admin-agent` SA**, or with an image other than `cluster-admin-agent:<tag>`, or its
   `*_ALLOWED_USERS` backstop is `ALLOW_ALL` → **HALT**.
5. **Bootstrap applies out of order** (CR before identity, or identity before VAP), or
   cert-manager/controller webhook not serving before identity apply → **HALT**.
6. **Old `platformagents` CRD remains after cutover**, or the platform CR is orphaned during the rename →
   **HALT**.
7. **Post-rename webhook path `/validate-…-agent` does not serve or does not live-reject a duplicate
   `(tier,scope)`** (V-K1) → **HALT** (silent admission failure).

---

## CRD-retirement runbook (P2-T5/T7 cutover — avoid orphaning the platform agent)

The hard-rename replaces the `platformagents` CRD with `agents`. Because the objects are
GitOps-recreated (pre-GA, no external authors), the safe cutover is **recreate-then-delete**, never a
delete-first:

1. **Land the code + generated artifacts** (P2-T1..T6): the new `agents` CRD base exists; the fleet
   platform CR + examples are already `kind: Agent`.
2. **Apply the new CRD** to the target cluster (`kubectl apply -f config/crd/bases/…_agents.yaml`), then
   apply the migrated **platform** `Agent` CR (`kind: Agent`, `tier=platform`) from the fleet manifests.
   The controller now reconciles the platform agent under the new Kind — verify the existing platform pod
   is unaffected (same SA, same read identity).
3. **Confirm the webhook serves for the new Kind** (V-K1): `/validate-…-agent` live-rejects a duplicate
   `(tier,scope)` and a `tier` PATCH.
4. **Only then delete the stale CRD:** `kubectl delete crd platformagents.kubeagents.x-k8s.io`. Deleting
   it garbage-collects any leftover `PlatformAgent` objects — so the platform agent **must** already exist
   as `kind: Agent` (step 2) or it is orphaned. **HALT** (condition 6) if any `platformagents` object
   remains unmigrated at this point.
5. **Verify** `kubectl get agents` returns the platform (and, post-cascade, cluster-admin) CRs and
   `kubectl get platformagents` errors with `the server doesn't have a resource type`.

---

## Notes / open items (human decisions before live cutover)

These do not block Kind verification; they gate the **production** cutover and are flagged for a human:

- **Ingress-ownership cutover mechanics (production).** Does `kage-router` own a **single** shared Google
  Chat Pub/Sub ingress subscription and fan out by re-publish, or **per-space** subscriptions? Phase 2
  collapses this on Kind via the emulator; the production subscription topology — and the exact step that
  stops the per-agent pod from also draining raw inbound (to prevent double-processing) — needs a human
  decision. (P2-T17)
- **"No other cluster" clause of acceptance (b).** Confirm whether Phase 2 must stand up a **second Kind**
  cluster for a positive cross-cluster read-deny, or whether the single-cluster "SA has no foreign
  kubeconfig/token" assertion + the scratch-GKE per-cluster-WI proof is sufficient. The enforcement layer
  is **separate clusters + per-cluster Workload Identity**, NOT RBAC/VAP (the VAP does not catch
  cross-cluster grants). (P2-T23/T24)
- **cert-manager + controller-bundle pins.** Proposed cert-manager **v1.14.x** and a kustomize-rendered
  controller manifest at the released image digest; confirm the acceptable versions and whether
  `apply.yml` expresses ordering via `kubectl wait` gates or ArgoCD sync-waves. (P2-T21)
- **Canonical home for the F4 render templates.** `agents/platform/skills/propose-cluster-admin/` vs
  `examples/gitops-repo/knowledge/cluster-blueprint/`, and how the platform agent references them at
  runtime. (P2-T20)
- **Dockerfile credential-proxy stage per tier.** Parametrize the base-tier of the shared
  `FROM platform AS credential-proxy` stage vs duplicate a cluster-admin variant — which is lower
  long-term churn as dev-team arrives in Phase 3? (P2-T10)

---

## Verification results

_Pending — populated as V-K0..V-K11 (Kind) and V-G1..V-G4 (scratch GKE) run. Evidence logged in
`LEDGER.md` §Verification log; each step maps to acceptance (a–e) + the 03 §11 negative suite above._
