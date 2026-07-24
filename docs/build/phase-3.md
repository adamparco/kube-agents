# Phase 3 — Developer Team Agent + isolation proof (task breakdown)

**Roadmap:** `docs/design/07-implementation-roadmap.md` §"Phase 3 — Developer Team Agent + isolation
proof". **Goal:** stand up the **third tier** (developer-team), **provisioned by the second**
(cluster-admin) via the GitOps cascade one layer down, and land the **load-bearing isolation property**:
a Developer Team Agent operates **only in its namespace** and is **provably unable** to read another
namespace or escalate — regardless of who is asking, because its identity is a namespace-scoped
read-only SA. In parallel, **complete the ChatOps router**: add the NL-inference mode (mode 3) where low
confidence → a clarifying question, never a guess; resolve developer-team routing; add thread affinity
across all three tiers; the model output is **never** trusted for authorization. Every mutation still
flows only through a reviewed PR actuated by CI/CD; agents stay read-only.

**Phase acceptance (07 §2 "Accept") — decomposed a–e:**

- **(a)** The Cluster Admin Agent **proposes** a developer-team agent (cascade); after human approval +
  merge, CI/CD applies it and the controller **runs** it **in the team's namespace**.
- **(b)** The dev-team agent runs with a **namespace-scoped read-only identity** and can read **only its
  own namespace** — `kubectl auth can-i` shows reads in another namespace = **no**, all writes = **no**,
  no cluster-scoped read, no privilege escalation. **This holds regardless of who asks** (it is RBAC, not
  model behavior — 03 §4a).
- **(c)** Cross-tier requests go via **shared state** (a reviewed GitOps PR / escalation), **never** a
  direct agent-to-agent call (invariant 3).
- **(d)** An **ambiguous / low-confidence NL message** triggers a **clarifying question**, not a
  mis-route (06 §10); a mis-inference can only ever land on an agent the sender is already allowlisted
  for, still refused-before-dispatch if not (03 §11).
- **(e)** **Thread affinity** across all three tiers — a thread stays bound to the agent it was first
  routed to until re-addressed (06 §2b/§6), spending **no** inference on sticky follow-ups.

**Touched Verification suites:** **03 §11** (the load-bearing negatives — SAR read-only + namespace
isolation; VAP attenuation admit/deny incl. dev-team wrong-scope; trusted-human refusal-before-dispatch
including NL mis-route; egress default-deny), **06 §10** (ChatOps routing: slash/`@handle` resolve with
no inference, ambiguous NL → clarify, refusal before dispatch, audit carries resolved agent + mode;
CR-schema + cardinality; read-only identity contract), plus **08 §7** (pre-created identity; controller
mints no RBAC — regress) and **05 §8** (chaos — regress). Load-bearing subset active this phase: **03
§11 in full**.

**Source of the breakdown:** two Explore agents mapped the router internals + the dev-team readiness gap,
then a **design judge-panel workflow** (2 tracks × 3 architects × 3 judges + synthesis) resolved the
seven load-bearing architectural decisions below. Track B's winning design scored 9/10 across all three
judges with **zero fatal flaws**; Track A's synthesis grafted the fixes the adversarial judges caught:
the **litellm/minty ExternalName reachability fix** (a team-namespace pod would otherwise point at
services that exist only in `kubeagents-system` and be silently non-functional), the **load-bearing
webhook placement clause** (without it a CR in `kubeagents-system` with `scope.namespace=team-x` passes
the cardinality webhook yet the pod escapes the per-namespace netpol/quota — a real isolation escape),
**keeping Workload Identity** (needed to drain the Pub/Sub subscription), and the **correct 06 §3 repo
layout** (Agent CR + per-agent identity under `agents/`, tenant netpol/quota under `namespaces/<ns>/`).

---

## Architecture decisions (load-bearing — resolved before breakdown)

### Track A — Developer-Team tier materialization

**A1 — Placement: the dev-team `Agent` CR lives in its own team namespace.** `metadata.namespace ==
spec.scope.namespace == team-x`. The controller is **unchanged**: it already renders every sub-resource
(Deployment/PVC/Service/ConfigMap) into `agent.Namespace` and calls `ctrl.SetControllerReference`, so the
pod lands in `team-x` bound to the pre-created namespaced SA. **Add ONE clause** to
`validateScopeAndParent` (`agent_webhook.go`, after the existing `scope.namespace`-required check): for
`TierDeveloperTeam`, **reject unless `metadata.namespace == scope.namespace`**. This clause is
**load-bearing** — `agentindex.Identity` keys dev-team cardinality on `scope.namespace` **independent**
of `metadata.namespace`, so without it a CR could sit in `kubeagents-system` yet claim a team scope,
placing the pod outside the per-namespace netpol/quota. Rendering into `scope.namespace` from a
`kubeagents-system` CR is **rejected** (cross-namespace `ownerRefs` break GC; a namespaced SA can only
bind a pod in its own namespace). Controller `ClusterRole` **stays cluster-wide** — namespace-list
scoping is **deferred hardening**, not load-bearing (the controller mints no RBAC, V-K10; 03 §3 grants
it agent-pod writes in the placement namespace).

**A2 — Identity: a pre-created namespace-scoped read-only identity in `team-x`.** `ServiceAccount
developer-team-agent` + **namespaced `Role`** `developer-team-agent-explorer` (`get/list/watch` over
`["","apps","batch","networking.k8s.io"]`) + `RoleBinding`, all labeled `kube-agents/tier:
developer-team`, **keeping a Workload-Identity annotation** to a viewer + `roles/pubsub.subscriber` GSA
(needed to drain the Google Chat subscription). A **namespaced `Role` (never a `ClusterRole`)** is what
makes "cannot read another namespace" provable by RBAC and is exactly what the VAP requires (read-verbs
pass validation #1; a `ClusterRole` for this tier is denied by wrong-scope validation #2). Filed per **06
§3**: the Agent CR + per-agent identity under `clusters/<c>/agents/`, **not** `namespaces/<ns>/`.

**A3 — Image + persona + telemetry.** Add `defaultDeveloperTeamAgentImage` + a `case TierDeveloperTeam`
arm in `defaultImageForTier` (drop the current fallback-to-platform; update the test). Create
`agents/developer-team/` mirroring `agents/cluster-admin/` — a **namespace-scoped read-only LEAF
persona** (read-only MCP set, cluster-mutating tools absent, `submit-suggestion` kept, **no propose-child
cascade** since dev-team is the leaf tier) + a `FROM agent-base AS developer-team` Dockerfile stage. Also
fix `otelTelemetryEnvVars` hardcoded `"platform"` → `EffectiveTier(agent)` so traces attribute the right
tier.

**A4 — Isolation controls in `namespaces/team-x/`.** Four objects: **(1)** `netpol-default-deny`
(`podSelector {}`, both directions, no rules — every pod locked); **(2)** `netpol-developer-team-egress`
(selects `kube-agents/tier: developer-team`; pure allowlist mirroring the Phase-2 cluster-admin egress —
DNS, hub inference/minty CIDRs :443, `restricted.googleapis.com`, GitHub/MCP CIDRs :443, **plus** a
narrow intra-cluster allow to `kubeagents-system` on TCP 80/8080 for litellm/minty; **no** `0.0.0.0/0`,
**no** `169.254.169.254`); **(3)** `resourcequota` sized to admit the agent pod + tenant headroom;
**(4)** **ExternalName** Services `litellm` and `github-token-minter` in `team-x` pointing at the
`kubeagents-system` FQDNs — this is the graft that makes the team-namespace pod **functional** (the
rendered config hard-codes `litellm.<ns>.svc` / `github-token-minter.<ns>.svc`). No ingress allow needed
(dispatch is Pub/Sub-pull; invariant 3).

**A5 — Cascade: the Cluster Admin Agent proposes the dev-team agent (02 §6).** Clone
`propose-cluster-admin` → `agents/cluster-admin/skills/propose-developer-team/` (SKILL.md +
`render_developer_team.py` + assets). Renders **one** PR bundle following 06 §3 (Agent CR + identity
under `agents/`; namespace/netpols/quota/aliases under `namespaces/<ns>/`), sets `tier=developer-team`,
`scope{project,cluster,namespace}`, `metadata.namespace=<ns>` (satisfies A1), closed non-empty
`allowedUsers`; **emits no bootstrap/VAP waves** (the control plane + enforcing VAP already exist from
Phase 2). Mints nothing; `submit-suggestion` opens the PR.

**A6 — Kind gate.** `local-dev/kind/verify-phase3.sh` mirroring `verify-phase2.sh`, reusing
`negative-attenuation.sh` (already carries the dev-team ADMIT + wrong-scope DENY fixtures).

### Track B — Router completion (NL mode 3 + dev-team routing + thread affinity)

**B1 — The deterministic core owns the confidence/clarify decision; the model only proposes
candidates.** Change `Inferer` to `Infer(ctx, text string, known []Handle) ([]Candidate, error)` where
`Candidate{Handle, Confidence}`. Add unexported `Resolver.threshold` (default `0.75`) +
`ambiguityMargin` (default `0.10`), set via `WithThreshold`. **Split the pipeline** into a
deterministic-only `Resolve` (modes 1/2; on fallthrough returns new sentinel `ErrNeedsInference`; never
touches the inferer, never increments) and `Infer` (the **sole** mode-3 core and **sole** increment
site). `Infer` order: empty text → `ErrUnaddressed` **before** the increment; else increment **once**;
drop every candidate not in `known`; 0 survivors → `ErrUnaddressed`; `top < threshold` **or**
`(top−second) < margin` → **clarify**; else route the top. Clarify is a typed `*ClarifyError{Reason,
Candidates}` with `Is(ErrClarify)`, read via `errors.As`.

**B2 — Candidate validity: two independent barriers make a hallucinated handle un-dispatchable.** The
gateway passes the live handle menu (`Index.KnownHandles()`) into `Infer`; the core **re-filters** every
returned candidate against that set regardless of the model. The resolved handle then flows through the
**same spine as modes 1/2** — `Index.LookupHandle → Authorize → Dispatch` — so a hallucination is refused
twice (filtered, or `ErrNoSuchTarget` on lookup miss, and always still gated by `AllowedUsers`). The
`Resolver` depends only on `[]Handle`, never `*Index`, to avoid an import cycle.

**B3 — Dissolve `ErrDeveloperTeamRoutingDeferred` via index-assisted resolution.** A `@devteam-<ns>`
handle carries only a namespace leaf, so the full key can only come from a live CR. Add a secondary index
`byTierLeaf` (key `tier\x00leaf` → list of `ScopeIdentity` keys) maintained in `Upsert`/`Remove` for
**all** tiers (it also feeds `KnownHandles`), with slice-aware re-key eviction on a scope edit. Add
`Index.LookupHandle(h, projectID) ([]Target, error)`: platform/cluster-admin compute the exact
`RouteKey` (unchanged); developer-team reads `byTierLeaf` (cluster/project come from the matched CR,
never the handle). Gateway: 0 → `ErrNoSuchTarget`; 1 → route; >1 (same ns across clusters, a
multi-cluster future) → `ClarifyError` (never a guess). `Handle.RouteKey` drops the dev-team branch; the
sentinel is deleted. Single-cluster Kind yields exactly 1 match → unambiguous.

**B4 — Thread affinity as a seam consulted only for bare messages.** Parse `message.thread.name` →
`ChatEvent.Thread` → `Message.ThreadID = firstNonEmpty(thread, space)`. New `affinity.go` defines
`AffinityStore` with a Phase-3 in-memory `memAffinityStore` (30m TTL; the durable 06 §6 session store is
the drop-in upgrade). Ordering in `gateway.Handle`: deterministic `Resolve` **always wins** and
(re)binds the thread on an authorized dispatch; on `ErrNeedsInference` **with** a live binding →
`Index.Lookup(boundKey)`, `Mode=ModeSticky`, spend **no** inference (stale binding → drop + fall
through); **unbound** → NL inference. **Binding is written only after a successful authorized dispatch**
and `Authorize` runs every turn, so a binding can never precede or replace an authz check.

**B5 — Audit/attribution surface + clarify emission seam.** `AuditRecord` gains `Tier`, `Clarify`,
`ThreadID`; `Mode` carries `ModeSticky`. `Outcome` gains `Clarify`. Add an optional `Replier` seam
(no-op default) the gateway invokes on a clarify to emit the clarifying question with candidates —
proven in Phase 3 with a fake Replier (real Google Chat outbound wiring lands with the Phase-5 inference
proxy).

**B6 — Verification bundle.** Router package unit/table tests (FakeDispatcher + deterministic fake
Inferer + fake Replier; no model, no API server) covering the full matrix; the four pre-existing negative
tests stay green; the two `ErrDeveloperTeamRoutingDeferred` tests are **rewritten** (dev-team now
resolves), not deleted.

**Open questions (resolved for Phase 3, flagged for later):** dev-team GSA IAM (viewer +
`pubsub.subscriber`; metadata-server WI note is moot on Kind); ExternalName target profile (in-cluster
`kubeagents-system` FQDN for single-cluster; hub CIDRs already cover multi-cluster); in-memory affinity
store is per-router-instance (confirm Phase-3 runs a single router replica; durable §6 store is the
upgrade). None block the isolation gate.

---

## Ordering / dependency rule (critical)

The two tracks are **largely independent** — Track A is manifests/persona/webhook/controller Go; Track B
is `internal/router` Go — and can proceed in parallel. Within each track:

- **Track B order (per synthesis):** **P3-T8 (B3)** first — it introduces `ClarifyError`/`ErrClarify` +
  `byTierLeaf` that the rest build on → **P3-T9 (B1)** → **P3-T10 (B2)** → **P3-T11 (B4)** → **P3-T12
  (B5)**. Router unit tests co-locate with each task; **P3-T13** confirms the full matrix.
- **Track A order:** **P3-T1 (image wiring)** + **P3-T2 (otel fix)** + **P3-T3 (webhook clause)** are
  independent Go changes that each leave the tree green → **P3-T4 (persona + Dockerfile)** → **P3-T5
  (materialized identity + CR + VAP fixture)** → **P3-T6 (namespace isolation manifests)** → **P3-T7
  (cascade skill; gates on the T5/T6 manifest shapes as templates)**.
- **P3-T13 (verify + regress)** gates on all of T1–T12. **P3-T14 (docs + PR)** last.

> **STANDING BUILD-GREEN CONTRACT.** Every task must leave the tree **green** (compile + unit/golden)
> **and** must not regress the **03 §11 negative suite**. If a task cannot land without breaking green it
> is **split** until it can.
>
> **IDENTITY-BEFORE-POD invariant** (inherited): the pre-created read-only identity **and its VAP
> acceptance** must be provable **before** the controller reconciles a dev-team pod.
>
> **No task that weakens a negative check** (VAP admits write/wrong-scope; SAR shows a write or a
> cross-namespace read; router dispatches a non-allowed sender, spends inference in mode 1/2/sticky, or
> routes a hallucinated/low-confidence handle without clarifying; controller mints RBAC) may merge,
> regardless of other progress.

---

## Tasks

| ID     | Task                                                                                          | Track | Implements                           | Files                                                                                                                                                                                                                    | Acceptance signal                                                                                                                                                                                                                                                     | Status |
| ------ | --------------------------------------------------------------------------------------------- | ----- | ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| P3-T1  | Wire the developer-team baked image into `defaultImageForTier` (A3.1) + update tests          | A     | 07 §2(a); 06 §3                      | `k8s-operator/internal/controller/manifest_helpers.go`, `manifest_helpers_test.go`                                                                                                                                       | `go test ./internal/controller/...` green; `TestDefaultImageForTier(developer-team)==defaultDeveloperTeamAgentImage`; tier-aware image test covers dev-team + override                                                                                                | todo   |
| P3-T2  | Fix `otelTelemetryEnvVars` tier label `"platform"` → `EffectiveTier(agent)` (A3.2)            | A     | 05 telemetry attribution             | `k8s-operator/internal/controller/agent_manifests.go` (~:369)                                                                                                                                                            | `go test ./...` green; rendered OTEL attr for a dev-team CR shows `tier=developer-team`; platform/cluster-admin goldens unchanged                                                                                                                                     | todo   |
| P3-T3  | Add the developer-team placement clause to the admission webhook (A1)                         | A     | 03 §3; invariant 4; 06 §1.2          | `k8s-operator/internal/webhook/agent_webhook.go`, `agent_webhook_test.go`                                                                                                                                                | Unit: dev-team CR with `metadata.namespace != scope.namespace` REJECTED (scope-mismatch); `==` ADMITTED; `go test ./internal/webhook/...` green                                                                                                                       | todo   |
| P3-T4  | Create `agents/developer-team/` baked persona tree + Dockerfile target (A3.3)                 | A     | 07 §2(a); 03 §11 (read-only persona) | `agents/developer-team/**` (SOUL/AGENTS/config.yaml read-only, submit-suggestion, no propose-child), `deploy/docker/Dockerfile`                                                                                          | `make docker-build-developer-team` builds; config has no `create_cluster`/`apply_manifest`; no propose-child cascade                                                                                                                                                  | todo   |
| P3-T5  | Materialize namespaced read-only identity + Agent CR under `agents/` + VAP positive (A2)      | A     | 06 §2/§3; 03 §4; invariant 1         | `examples/gitops-repo/policy/rbac-overlay/developer-team.yaml` (+WI), `clusters/cluster-a/agents/identity/developer-team-team-x-identity.yaml`, `.../developer-team-team-x-agent.yaml`, `policy/tests/vap_positive.yaml` | SAR: `get/list/watch` in team-x = yes, writes = no, team-y = no, cluster-scoped = no; VAP: namespaced Role ADMITTED, dev-team ClusterRole DENIED wrong-scope; CR refs SA + `metadata.namespace==scope.namespace`                                                      | todo   |
| P3-T6  | Per-namespace isolation manifests: default-deny + dev-team egress netpol, quota, aliases (A4) | A     | 07 Phase 3; 03 §10; 05 §5            | `examples/gitops-repo/clusters/cluster-a/namespaces/team-x/{namespace,netpol-default-deny,netpol-developer-team-egress,resourcequota,externalname-aliases}.yaml`                                                         | Calico: agent pod egress to `169.254.169.254` + arbitrary host FAIL; inference/GitHub/MCP + litellm/minty (via ExternalName) SUCCEED; tenant pod no egress; team-y→team-x ingress refused; over-quota deploy rejected; agent pod Running                              | todo   |
| P3-T7  | Build the `propose-developer-team` cascade skill on the Cluster Admin Agent (A5)              | A     | 02 §6; invariants 2,3,5              | `agents/cluster-admin/skills/propose-developer-team/{SKILL.md,scripts/render_developer_team.py,assets/**}`                                                                                                               | Render into tmp repo emits the seven 06 §3 paths matching a golden; rendered identity passes VAP dry-run, write/scope tamper DENIED; rendered CR passes webhook dry-run; mints nothing; `submit_suggestion.py --tier developer-team` accepted; no bootstrap/VAP waves | todo   |
| P3-T8  | B3 — Index-assisted dev-team routing; dissolve `ErrDeveloperTeamRoutingDeferred`              | B     | 06 §2b/§10; 07 Phase 3               | `k8s-operator/internal/router/{index,grammar,types,classify,gateway}.go` + tests                                                                                                                                         | `@devteam-<ns>` 1 CR routes (InferenceCalls==0), 0 → `ErrNoSuchTarget`, >1 → `ErrClarify` w/ candidates; scope-edit re-key evicts stale `byTierLeaf` slice; grep-clean of the sentinel; 4 pre-existing negatives green                                                | todo   |
| P3-T9  | B1 — Deterministic-core confidence/clarify contract + `Inferer` candidates signature          | B     | 06 §2b; 03 §4a                       | `k8s-operator/internal/router/{resolve,types,gateway}.go`, `resolve_test.go`                                                                                                                                             | `Infer(ctx,text,known)([]Candidate,error)`; `Resolve` deterministic-only → `ErrNeedsInference`; `Infer` sole increment site; threshold flip route↔clarify by `WithThreshold` only; InferenceCalls==1 for route AND clarify, ==0 empty/deterministic                   | todo   |
| P3-T10 | B2 — Candidate validity: `KnownHandles` menu + core re-filter + unchanged spine               | B     | 03 §4a/§11; invariant 1              | `k8s-operator/internal/router/{index,resolve,gateway}.go` + tests                                                                                                                                                        | Hallucinated candidate → `ErrUnaddressed`/`ErrClarify`, never routable; mis-inference to a real agent excluding sender → `ErrUnauthorized` 0 sends; no live agent → `ErrNoSuchTarget` 0 sends; no import cycle                                                        | todo   |
| P3-T11 | B4 — Thread affinity: parse thread, `AffinityStore` seam, sticky ordering, bind-on-dispatch   | B     | 06 §2b/§6; 07 Phase 3                | `k8s-operator/internal/router/{chatevent,dispatch,gateway}.go`, `affinity.go` (new), `pubsubinbound/receiver.go` + tests                                                                                                 | Sticky follow-up dispatches `Mode==ModeSticky`, InferenceCalls==0; explicit `@handle` rebinds; bound thread + non-allowlisted sender → `ErrUnauthorized` 0 dispatch; TTL expiry drops binding; bind only after authorized dispatch                                    | todo   |
| P3-T12 | B5 — Audit/attribution surface (`Tier`/`Clarify`/`ThreadID`/`ModeSticky`) + Replier seam      | B     | 06 §2b/§10                           | `k8s-operator/internal/router/{audit,gateway}.go` + tests                                                                                                                                                                | Sticky turn records `Mode==ModeSticky`+Tier+Identity; NL clarify records `Clarify==true`, `Mode==ModeInference`, `dispatched==false`, fake Replier got a question naming candidates; delivered turn records Tier/Identity/ThreadID                                    | todo   |
| P3-T13 | Phase 3 verification: `verify-phase3.sh` (A6) + router go-test matrix (B6) + regress          | A+B   | 07 §5; 03 §11; 06 §10                | `local-dev/kind/verify-phase3.sh` (new), reuse `local-dev/tests/negative-attenuation.sh`, router tests                                                                                                                   | `verify-phase3.sh` exits 0 (placement, SAR isolation, VAP admit/deny, image, Calico egress + ExternalName reachability, cardinality dup + tier immutability); `go test ./...` green; **03 §11 + verify-phase2 not regressed**                                         | todo   |
| P3-T14 | Docs (INSTALL Phase 3 section, LEDGER, memory) + open PR → main on fork; auto-merge           | A+B   | roadmap; AGENTS.md                   | `INSTALL.md`, `docs/build/LEDGER.md`, memory                                                                                                                                                                             | PR opened on fork base `main`; all CI green + `mergeStateStatus: CLEAN`; no HALT; PR URL shared                                                                                                                                                                       | todo   |

## Verification suites & Accept mapping

| Phase-3 Accept                                           | Proof                                                                                                       |
| -------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| (a) dev-team agent runs **in its namespace**             | P3-T1/T3/T5 + Kind: pod in `team-x` bound to `developer-team-agent`, image `developer-team-agent:<tag>`     |
| (b) **provably unable** to read another ns / escalate    | P3-T5 SAR matrix (03 §11) + P3-T6 netpol + VAP wrong-scope deny — **RBAC, not model behavior**              |
| (c) cross-tier via **shared state**, never a direct call | P3-T7 cascade = a reviewed PR (invariants 2,3,5); no RPC path exists                                        |
| (d) ambiguous NL → **clarify not mis-route**             | P3-T9/T10 (03 §4a/§11, 06 §10): low-conf/near-tie/hallucination → `ErrClarify`; mis-route still authz-gated |
| (e) **thread affinity** across tiers                     | P3-T11: sticky follow-up `ModeSticky`, 0 inference; re-address rebinds; never bypasses allowlist            |
| **Regression (halt on fail)**                            | 03 §11 negative suite + `verify-phase2.sh` + 05 §8 chaos re-run green; 08 §7 "controller mints no RBAC"     |
