# Phase 9 — The Action Broker, dark (task breakdown)

Source of truth: [`docs/design/07-implementation-roadmap.md`](../design/07-implementation-roadmap.md)
§2 "Phase 9 — The Action Broker, dark". Conformance spec:
[`docs/design/09-verification-and-validation.md`](../design/09-verification-and-validation.md).
Contracts: [06](../design/06-api-and-data-contracts.md) §4 (action contracts), §2.2.1 (broker
operations grant); [03](../design/03-security-model.md) §4–§6; [08](../design/08-agent-runtime-and-identity.md)
§2 (the workload pair).

**Goal (07 §2, verbatim intent):** build the entire safety machinery — broker, envelope, classifier,
journal, undo, brake — and exercise it end-to-end **with no write authority anywhere**. The actor
ServiceAccounts are created but bound to empty roles; the broker runs every action in dry-run.

**Why this shape:** the hardest and most novel code in the project lands, gets reviewed, and gets
tested against real clusters while the worst possible bug is still a no-op. Phase 10 becomes a
permission change rather than a leap.

This is the largest phase in the roadmap by a wide margin: it introduces five CRDs, a second binary,
a second image, a second Deployment per `Agent` CR, and four BLOCKING-ALWAYS suites at once. It is
planned as ten units, and the sequencing below is load-bearing — three of the four planning defects
found in this pass are ordering or scoping problems that would otherwise have surfaced at the gate.

---

## Survey of the current state — Phase 9 is greenfield

Unlike Phase 8, which repaired things that existed and were wrong, Phase 9 builds things that do not
exist at all. The survey is therefore short, and its value is in being explicit about the absence, so
that "extend X" never gets planned against an X that is not there.

### Nothing of the action pipeline exists

| Artifact                   | Expected by           | Present today                                                                                        |
| -------------------------- | --------------------- | ---------------------------------------------------------------------------------------------------- |
| `ActionRecord` CRD         | 06 §4.3               | **absent** — `k8s-operator/config/crd/bases/` holds exactly one CRD, `…_agents.yaml`                 |
| `ChangePolicy` CRD         | 06 §4.2               | **absent**                                                                                           |
| `FleetFreeze` CRD          | 06 §4.4               | **absent**                                                                                           |
| `UndoRequest` CRD          | 06 §4.4               | **absent**                                                                                           |
| `ApprovalRoster` CRD       | 06 §4.4               | **absent**                                                                                           |
| Broker package             | 08 §2.1               | **absent** — `k8s-operator/internal/` is `agentindex controller eventingress router testing webhook` |
| Broker binary              | 08 §2.1               | **absent** — `k8s-operator/cmd/` is `main.go eventingress k8s-event-watcher router`                  |
| Broker image               | 08 §2.1               | **absent** — seven first-party images publish today; `kube-agents-broker` is not one                 |
| `spec.operations` on Agent | 06 §1.1               | **absent** — no `Operations`/`Paused`/`DryRun` identifier anywhere in `agent_types.go` (164 lines)   |
| `status.broker` on Agent   | 06 §1.1               | **absent**                                                                                           |
| Undo controller            | 05 §1.3, 09 §5 `C-UC` | **absent**                                                                                           |
| Journal reconciler         | 09 §5 `C-JR`          | **absent**                                                                                           |
| Classifier corpus          | 09 §7.1               | **absent** — `verification/` holds `traceability.yaml` and `results.csv`                             |

The single occurrence of the word "broker" in the operator tree is a comment at
[agent_webhook.go:463](../../k8s-operator/internal/webhook/agent_webhook.go#L463), documenting that
`status.broker.actorServiceAccount` is status rather than spec. It describes a field that does not
yet exist; P9-T1/T7 make it real. `internal/router/classify.go` is chat-event classification and is
**not** related to risk classification — the name collision is a trap for a future reader and P9-T3
must not extend it.

### What Phase 8 leaves in place, and that Phase 9 builds onto

- The `Agent` CRD, its cardinality/scope/ceiling webhook (V-1…V-10, all ten enforced), and its
  goldens — `internal/controller/agent_manifests.go` is the render site and is golden-tested.
- Per-tier egress NetworkPolicy, tenant quota, and the namespace default-deny, all applied from the
  install path. The broker's `8443` ingress rule and the agent's egress-to-broker rule are new holes
  that must be punched deliberately, in the same templates (P9-T7).
- `dev/lib/preconditions.sh` P1–P10, `dev/L0-CHAIN.txt` (14 lines), `dev/L2-CHAIN.txt` (7 lines),
  `dev/tests/invariants-gate.py` (14 checks), and `dev/cluster/reload-images.sh` deploy-by-digest.
- `verification/traceability.yaml` **already cites the Phase 9 check IDs** — V-BRK-001…005,
  V-REV-001/002, V-GAT-003/005, V-RUN-001/002/004/009/013/014 all appear as mappings for bullets in
  02 §10, 03 §11, 05 §8 and 08 §7. The matrix is not a Phase 9 deliverable; making those IDs
  _executable_ is.

### The one live defect this phase carries (P9-T10)

`agent_common` is the MCP server implementing `call_agent`, the inter-agent transport. It is
declared inconsistently in the two definition sites and gets no credential in either:

- **Image-baked** `agents/platform/config.yaml` (and the two peer tiers): the `mcp_servers:` block
  declares **only** `platform_control`. `agent_common` is absent from `mcp_servers` entirely while
  being listed in both `platform_toolsets.cli` and `platform_toolsets.api_server`
  ([config.yaml:2-34](../../agents/platform/config.yaml#L2-L34)).
- **Runtime-authoritative** `renderConfigYAML()`: `agent_common` **is** declared, with `command` and
  `args` and **no `env:` block at all**
  ([agent_manifests.go:156-159](../../k8s-operator/internal/controller/agent_manifests.go#L156-L159)),
  while `platform_control` beside it declares six variables including `API_SERVER_KEY`
  ([config.yaml:10-16](../../agents/platform/config.yaml#L10-L16)).

Hermes passes an MCP server only what its config declares, so `agent_common_server.py` reads an
empty key and refuses every inter-agent request with `ERROR [500]: API_SERVER_KEY is not
configured`. On the live install `/cluster-admin` never answers. **The fail-closed refusal is
correct and must not be weakened** — the defect is the missing env, not the refusal.

---

## Planning defect 1: "bound to empty roles" is literally incompatible with journalling

07 §2 says the actor ServiceAccounts are "created but bound to **empty** roles". 06 §2.2.1 says every
actor identity additionally receives the **broker operations grant**, byte-identical across tiers —
`create` on `tokenreviews`, `get/list/watch/create` on `actionrecords`, `get/update/patch` on
`actionrecords/status`, `get/list/watch` on `fleetfreezes`, `agents`, `changepolicies` and
`approvalrosters`. Without it the broker cannot authenticate its caller (pipeline step 1), cannot
read the brake (step 5), and cannot write the journal (step 11) — which is precisely the thing
Phase 9 exists to exercise. 06 §2.2.1 states the consequence explicitly: a tier that cannot read
`fleetfreezes` "fails closed permanently … so omitting this grant does not fail safe — it bricks the
tier."

**Resolution — "empty" means empty of _tenant_ authority, and the phase asserts that in both
directions.** The Phase 9 actor Role/ClusterRole is **exactly** the 06 §2.2.1 grant and nothing else.
Accept (e)'s `auth can-i` sweep is therefore not "no write verb, full stop"; it is:

1. **negative** — no agent identity holds any write verb on any resource outside the grant's own
   resource set; and
2. **positive** — every actor identity holds exactly the grant, and holds **no** `update` or
   `delete` on `actionrecords` (the append-only property).

That is V-BRK-013 verbatim ("asserted in both directions"), which is already in the extended catalog
at 09 §6.14 and already assigned to Phase 9. The two-sided form is what makes the sweep falsifiable:
a one-sided "no write verbs" sweep passes on a broken install where the actor role is genuinely
empty and the broker has been fail-closed since boot.

**Consequence for the ledger:** Accept (e) is bound to V-BRK-013 in addition to V-CTN-004, and the
sweep script asserts the exclusion set by name rather than by "these are the ones that were there
when I wrote it".

---

## Planning defect 2: V-BRK and V-REV are BLOCKING-ALWAYS and half of them require a real write

09 §6 classifies **V-BRK, V-REV and V-ISO as BLOCKING-ALWAYS**; 09 §9.6 says a BLOCKING-ALWAYS check
**may not be deferred**. So "defer the execution-dependent half to Phase 10" is not available — the
gate would refuse the row, exactly as the V-MET-011 deferral was refused in Phase 8.

Sorting the two suites by whether their property needs an actual mutation:

| Dark-mode-native (refusal properties — 9 of 12 V-BRK)                                                                                                                                                                                                                                                                                                             | Needs a real mutation                                                                                       |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| V-BRK-001 (agent container cannot write), 002 (scope spoof), 004 (stripped annotation), 005 (journal down ⇒ refuse), 007/008/009/010 (mTLS, audience, neither-alone, foreign caller), 011 (pipeline order), 012 (one broker per CR), 013 (the grant), 017 (default-audience token), 021 (non-skippability); V-REV-003 (no undo plan ⇒ gated), V-REV-004 at **L1** | V-BRK-003 (journal reconciliation), 006 (write-ahead), 014/015/016; V-REV-001, 002, 005, 006, 007, 008, 009 |

**Resolution — one code path, two RBAC profiles, and the boundary is mechanized.**

- **The shipped profile** is the 06 §2.2.1 grant and nothing else. It is what `provision_*.sh`
  installs, what the live install runs, and what the Accept (e) sweep asserts.
- **A test-only overlay** — `dev/verify/fixtures/actor-tenant-grant.yaml` — adds namespace-scoped
  tenant authority to **one fixture agent** in a dedicated namespace on `gke-scratch-kube-agents-dev`
  so the snapshot → execute → verify → undo half can be exercised for real. It is applied by an L2
  script, torn down at the end of that script, and the Accept (e) sweep runs **after** teardown.
- **The seam is guarded, not trusted.** Three mechanized constraints, added in P9-T9:
  1. `invariants-gate.py` gains a check that no path under `k8s-operator/scripts/`, `deploy/` or
     `config/` references the overlay — it is reachable only from `dev/`. A test-only grant that
     drifts into the install path is the single worst outcome of this decision, so it is the one
     thing a lint refuses rather than a convention discourages.
  2. The fixture broker Deployment is rendered by the **same** `agent_manifests.go` renderer as the
     shipped one, so the fixture cannot become scenery (LSN-024's shape).
  3. Every `ActionRecord` produced under the overlay carries a fixture label, and the L2 script
     asserts the namespace is empty of them at teardown.

**What this buys and what it does not.** It buys genuinely green BLOCKING-ALWAYS suites instead of
vacuous ones — V-REV-001 over an empty population of executed records is a check that cannot fail
(V-MET-014), and shipping one is worse than shipping none. It does **not** prove the path under a
real agent identity driven by a real agent pod; that is Phase 10, and it is stated in the phase-9
results rows rather than implied.

**One reformulation recorded honestly.** V-REV-001 reads "100% of **executed** non-gated
`ActionRecord`s carry a validated undo plan". In shadow mode the fleet's population of executed
records is empty by construction, so the phase-9 instance asserts the same property over records in
the **`DryRun`** terminal phase, and the `results.csv` note says so. The overlay instance asserts it
over genuinely executed records. Both are recorded; neither is described as the other.

---

## Planning defect 3: V-BRK-003 needs an audit-log stream, and it may not be deferred

V-BRK-003 — "every audit-log write by an actor identity has a matching `ActionRecord`" — is L2/L3
and BLOCKING-ALWAYS. GKE does not expose API-server audit configuration to the customer; the stream
lands in Cloud Logging. The scratch cluster is in `adamparco-kage`, a project the harness can read,
so the stream is reachable via `gcloud logging read` — but **Data Access audit logs for the
Kubernetes API are off by default**, and turning them on is a project-level IAM policy change.

**Resolution.** The journal reconciler takes a **pluggable audit source** (interface, not a
hard-coded Cloud Logging client), so the L1 instance runs against a fixture stream and the L2
instance against Cloud Logging. P9-T1 opens with a five-minute probe: `gcloud logging read` for a
known write on the scratch cluster. If the stream is absent, enabling Data Access audit logs for
`k8s_cluster` on `adamparco-kage` is inside the harness's authority and is part of P9-T1 (it is a
scratch project, and the change is additive). The negative control is an injected unjournaled write
by the fixture actor identity under the P9-T9 overlay, which the reconciler must raise. Recorded
here at PLAN time because the alternative — discovering it at the gate — turns a BLOCKING-ALWAYS
check into a milestone-time emergency.

---

## Planning defect 4: Accept (a)–(e) does not cover the ratchet

The 09 §10 ratchet for Phase 9 is **V-BRK, V-REV, V-RUN, V-GAT (L1), V-ISO-001/002/006**. Accept
(a)–(e) covers the envelope round-trip, the classifier corpus, scope spoofing, the brake, and the
`can-i` sweep. It says nothing about **V-RUN** (the workload pair, its identities, labels, hardening,
startup ordering, and `pause`-is-not-scale-to-zero — fourteen checks) or **V-ISO-001/002/006**
(controller down / controller recovered / journal down). A phase closed on Accept alone would leave
seventeen ratchet checks unrun, and 09's Definition of Done requires the ratchet, not the Accept
list.

**Resolution.** The acceptance table below carries explicit **ratchet-only rows** with no Accept
bullet, marked as such. `verify-phase9.sh` runs the ratchet, not the Accept list; Accept is the
subset that 07 chose to name. This is stated because "Accept is green" reading as "the phase is
done" is 09 §11.8's failure mode with a different label.

Two smaller notes in the same family:

- **07 §2's task table lists P9-T10 before P9-T9.** The gate task is genuinely last; the numbering is
  a source-document ordering artifact and the sequencing below uses dependency order, not the
  printed order.
- **P9-T10 binds to V-CMP-006, which is not in the Phase 9 ratchet.** It is a live-install defect
  given a phase-9 slot by 07 §2 because that is when it was found. It has its own check and its own
  L2/L3 evidence, and it does not gate on any broker work — which is why it goes first.

---

## Acceptance → check binding (07 §2 "Accept", plus the 09 §10 ratchet)

Every bullet binds to at least one check ID. No bullet is unbound. The last four rows are ratchet
obligations with no corresponding Accept bullet — see planning defect 4.

| Accept                                                                                                            | Check IDs                                                                   | Level      | Target                          |
| ----------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- | ---------- | ------------------------------- |
| **(a)** an envelope flows end-to-end in shadow mode → well-formed `ActionRecord` + valid undo plan                | V-CTR-005, V-REV-001, V-REV-004, V-BRK-015, V-GAT-019                       | L1, L2     | dev                             |
| **(b)** classifier matches the fixture corpus (all four classes); `ChangePolicy` tightens, provably cannot loosen | V-GAT-001, V-GAT-002, V-GAT-009, V-GAT-010, V-GAT-017, V-GAT-021, V-GAT-022 | L0, L1, L2 | dev                             |
| **(c)** an envelope claiming a scope other than the caller's is rejected                                          | **V-BRK-002**, V-BRK-007, V-BRK-008, V-BRK-009, V-BRK-010, V-BRK-017        | L1, L2     | dev                             |
| **(d)** `pause`/`freeze` work with inference down; broker refuses when the journal is unavailable                 | **V-BRK-005**, V-RUN-007, V-RUN-008, V-RUN-012, V-RUN-013, **V-ISO-006**    | L0, L2     | dev                             |
| **(e)** no agent identity in the fleet holds a write verb — full `auth can-i` sweep                               | V-CTN-004, **V-BRK-013**, V-BRK-001, V-BRK-012                              | L0, L2     | dev + live (sweep is read-only) |
| _(ratchet only)_ the workload pair, its identities, labels, hardening, ordering                                   | V-RUN-001…006, V-RUN-009, V-RUN-010, V-RUN-011, V-RUN-014                   | L0, L2     | dev                             |
| _(ratchet only)_ journal integrity, write-ahead, pipeline order and non-skippability                              | V-BRK-003, V-BRK-004, V-BRK-006, V-BRK-011, V-BRK-014, V-BRK-016, V-BRK-021 | L0, L1, L2 | dev (+ overlay)                 |
| _(ratchet only)_ reversibility beyond coverage: correctness, attribution, rollback, retention                     | V-REV-002, V-REV-003, V-REV-005, V-REV-006, V-REV-007, V-REV-008, V-REV-009 | L1, L2     | dev (+ overlay)                 |
| _(ratchet only)_ failure isolation with the pair deployed                                                         | **V-ISO-001**, **V-ISO-002**                                                | L2         | dev                             |
| _(carried, not ratchet)_ the inter-agent credential seam                                                          | **V-CMP-006**                                                               | L0, L2, L3 | dev + live                      |

"dev" is `gke-scratch-kube-agents-dev` — the only destructive-test target. "live" is
`platform-agent-host`, verification only. "overlay" is the test-only tenant grant of planning
defect 2, applied and torn down inside one L2 script.

V-BRK, V-REV and V-ISO are **BLOCKING-ALWAYS**: not one of the rows above may close as `deferred`.
V-GAT and V-RUN are BLOCKING-PHASE and gate the milestone.

---

## Task breakdown

Ordered by dependency, then by risk. **P9-T10 ships first and alone** — it is independent of every
broker unit, it repairs a defect on a running install, and it is the smallest unit in the phase.

| Task       | What to build                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | Spec             | Files                                                                                                                                                                                                                                                                        | Check IDs                                                                                         | Weight           |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- | ---------------- |
| **P9-T10** | Repair the inter-agent credential seam. Declare `agent_common` with an `env:` block carrying `API_SERVER_KEY` (and the `KUBERNETES_SERVICE_*`/`HERMES_HOME` set `platform_control` gets) in **both** definition sites, for **all three tiers**; the image-baked config must also stop listing a toolset entry for a server it never declares. Bind to **V-CMP-006** with a lint that fails any MCP server whose script reads a credential from the environment and whose config declares no `env`. **Do not weaken the fail-closed refusal.** Record for P15-T1: the per-tier `API_SERVER_KEY` values currently differ and `resolve_agent_credentials` sends the caller's own key as the target's bearer.                                                                                                                                                                                                                                                                                                                                                                                                                                                  | 05 §1; 06 §4.1   | `agents/{platform,cluster-admin,developer-team}/config.yaml` · `k8s-operator/internal/controller/agent_manifests.go:156` · goldens · new `dev/test_mcp_env_declared.py` · L0-CHAIN                                                                                           | **V-CMP-006**                                                                                     | medium           |
| **P9-T1**  | `ActionRecord` CRD + journal store. Full 06 §4.3 schema: attribution, classification, targets, `preState` (with the >1 MiB `objectRef` path), undo plan, the ten-phase status lifecycle, the **two** retention clocks, bidirectional undo linkage, `chainId`. `spec` immutable by CEL; `status` field/principal table enforced by `vap-agent-scope-journal`. Includes the journal reconciler (`C-JR`) behind a **pluggable audit source**, the retention controller's post-export deletion predicate, and the Data Access audit-log probe of planning defect 3.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | 06 §4.3          | new `api/v1alpha1/actionrecord_types.go` · `config/crd/bases/…_actionrecords.yaml` · new `internal/journal/` · `internal/controller/journal_reconciler.go` · `internal/controller/retention_controller.go` · `config/policy/vap-agent-scope-journal.yaml`                    | V-BRK-003, V-BRK-015, V-REV-008, V-CTR-\*                                                         | high             |
| **P9-T2**  | Action Envelope + broker skeleton. New tier-neutral binary and image. `POST /v1alpha1/actions` + `GET /healthz` on **8443**, HTTP+JSON over TLS (not gRPC). mTLS **and** projected token with audience `kubeagents-broker`; `TokenReview`; `(tier, scope)` derived from the authenticated caller and **never** from the body. Idempotency key = `"sha256:" + lowerhex(SHA-256(JCS(K)))`, recomputed by the broker. The three anti-replay mechanisms. Exactly one listening port, one mutating route, no `/bin/sh` in the image.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | 06 §4.1; 03 §4.1 | new `cmd/broker/main.go` · new `internal/broker/{server,auth,envelope,idempotency}.go` · new `k8s-operator/Dockerfile.broker` · `tags.env` · `deploy/docker/cloudbuild.yaml` · `dev/cluster/reload-images.sh` · publish workflows · `verification/fixtures/envelopes/`       | **V-BRK-002**, V-BRK-007/008/009/010/017, V-BRK-021, V-RUN-010, V-CTR-005                         | **load-bearing** |
| **P9-T3**  | The risk classifier + `ChangePolicy`. Deterministic, table-driven, the 06 §4.2 evaluation order (scope ⇒ short-circuit, forbidden ⇒ short-circuit, max over inputs, `+1` capped at gated, `ChangePolicy` max, no-undo-plan raise). The seventeen code-floor rules including `secret-material-egress` (digest match, **not** entropy), `cross-tier-direct-operation` (ownership computed via the V-6 subset predicate, reused not reimplemented), and the production-label precedence ladder. Both path dialects, with the `/`-prefix rejection at admission. The **120–200 case corpus** of 09 §7.1 with asymmetry pairs. Classifier package imports no inference client.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | 03 §5; 06 §4.2   | new `internal/broker/classify/` · new `api/v1alpha1/changepolicy_types.go` · CRD · webhook rule (class ≥ floor) · new `verification/fixtures/classifier-corpus.yaml` · `dev/tests/classifier-corpus-lint.py` (V-MET-005) · L0-CHAIN                                          | **V-GAT-001/002/009/010/017/021/022**, V-GAT-011, V-GAT-012                                       | **load-bearing** |
| **P9-T4**  | Undo-plan generation for every supported verb — the 06 §4.3.1 strategy table (`create`→`delete`, `apply`/`patch`→`restore`, `scale`→`restore`, `delete`→`recreate`, cloud→`inverse`, else `none`), the sanitizer, `preconditions.uid` on every step, inbound-reference detection downgrading `recreate` to `none`, and dry-run validation of each step against the API server. The explicit **"cannot generate" path reclassifies as gated** — this is what makes reversibility true rather than aspirational, so it is tested directly and from both sides. The 09 §7.3 round-trip fixtures including the negative set.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | 06 §4.3.1        | new `internal/broker/undo/` · `verification/fixtures/undo/` · unit + envtest suites                                                                                                                                                                                          | **V-REV-003**, **V-REV-004**, V-REV-001, V-REV-009                                                | **load-bearing** |
| **P9-T5**  | Snapshot → execute → verify. Server-side apply with field manager **exactly** `kube-agents/<tier>/<scope>`, dry-run first where supported, per-kind verification predicates (04 §5.1), the recovery ladder recorded in `status.recovery`, and the atomicity rule (multi-target: if any snapshot fails, **nothing** is applied). Selector fan-out expanded **once**, before classification, against live state. Write-ahead ordering: the record's durable write precedes the mutation, which precedes the API response, which precedes the chat report.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | 04 §1, §5.1      | new `internal/broker/execute/` · `internal/broker/verify/` · envtest suites                                                                                                                                                                                                  | **V-BRK-006**, V-BRK-018, V-BRK-019, V-BRK-020, V-BRK-014, V-REV-002/005/006                      | high             |
| **P9-T6**  | The brake. `Agent.spec.operations` (`paused`, `pauseReason`, `dryRunOnly`, roster/policy refs, initiative budget) and `status.operations`/`status.broker`; cluster-scoped `FleetFreeze`; `UndoRequest`; `ApprovalRoster`; the `contested` index and its advisory annotation; the undo controller (`C-UC`). Every one of the nine fail-closed rules of 06 §4.4. All five controls must work through `kubectl` and the API **with inference down** — no dependency on the model, the router, or the agent pod. `pause` is **not** scale-to-zero.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | 03 §6; 06 §4.4   | `api/v1alpha1/agent_types.go` (+`operations`) · new `{fleetfreeze,undorequest,approvalroster}_types.go` · CRDs · `internal/controller/undo_controller.go` · `internal/broker/brake.go` · webhook · goldens                                                                   | **V-RUN-007/008/012/013**, **V-BRK-005**, V-REV-007, V-GAT-003/007                                | **load-bearing** |
| **P9-T7**  | Controller reconciles the pair. Render the broker Deployment, Service (`<agent>-broker`, 8443) and certificate Secret **before** the agent Deployment, both owned by the `Agent` CR; `BrokerReady`/`AgentReady` conditions with `Ready` their conjunction; the `wait-for-broker` init container with observe-and-report on timeout; `KUBEAGENTS_BROKER_ENDPOINT` injection; the `kube-agents/role` label on both halves; the broker's NetworkPolicy (ingress only from `role: reader` with matching `kube-agents/agent`) and the agent's egress-to-broker rule. **Mints no RBAC.** Regenerate goldens.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | 08 §2            | `internal/controller/agent_manifests.go` · `internal/controller/agent_controller.go` · `pod_launcher.go` (pair-atomic `LaunchSpec`) · `netpol-*.yaml.template` · goldens · `dev/tests/reference-render.py`                                                                   | **V-RUN-001/002/003/004/005/006/009/011**, V-BRK-012, **V-BRK-011**, **V-BRK-014**, V-ISO-001/002 | high             |
| **P9-T8**  | Shadow mode. The agent's `apply-change` MCP tool submits real envelopes; the broker classifies, plans undo, and journals a `DryRun` `ActionRecord` without calling a mutating API. `dryRunOnly` is stricter-only and cannot be cleared by the agent. Run against `gke-scratch-kube-agents-dev` for the duration of the phase and mine the journal for classifier gaps — every gap found becomes a corpus case, not a code tweak.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | 04 §1            | `agents/*/skills/` (the `apply-change` skill) · `deploy/*/scripts/` MCP tool · `internal/broker/server.go` (dry-run terminal path) · a journal-mining note in this file                                                                                                      | V-REV-001 (DryRun scope), V-GAT-019, V-CHR-\* (advisory)                                          | high             |
| **P9-T9**  | Consolidated gate `dev/verify/verify-phase9.sh`: envelope round-trip, scope-spoof rejection, classifier fixture corpus, undo-plan coverage, brake liveness with inference down, fail-closed on journal loss, and the Accept (e) two-sided `can-i` sweep. The test-only tenant overlay of planning defect 2, with its three guards. Regression through `verify-phase8.sh`. New L0 and L2 chain lines. **Also fix the review-gate path filter, found during P9-T2:** `.github/workflows/review-gate.yml` triggers on `**/policy/**`, `**/agents/**`, `**/provisioning/**`, `**/namespaces/**` and `**/SOUL.md` — none of which match `k8s-operator/internal/**`, so PR [#33](https://github.com/adamparco/kube-agents/pull/33) added the broker, an authenticator and the one image whose SA can write, and the security gate did not run on it. The gate was written when the security surface was manifests; it now includes Go. Widen the filter to the broker, webhook, router and RBAC paths **in the unit that owns the gate**, not in a unit that would be reviewing itself. Expect the first run to need waiver triage: the suite was tuned on YAML. | 07 §5            | new `dev/verify/verify-phase9.sh` · new `dev/verify/broker-auth-l2.sh` · `dev/verify/broker-execute-l2.sh` · `dev/verify/actor-grant-sweep-l2.sh` · `dev/verify/fixtures/actor-tenant-grant.yaml` · `dev/tests/invariants-gate.py` · `dev/L0-CHAIN.txt` · `dev/L2-CHAIN.txt` | all of the above + V-MET-007                                                                      | **load-bearing** |

**Every unit ships on its own branch off `origin/main`, its own PR, and is merged and the branch
deleted before the next begins** (`binding.md` §Branching, §Merge). A unit is not "done" until its
PR is merged green.

**P9-T3 ships as two units** (the P8-T8a/b/c precedent), because the row above is two deliverables
that share a heading and nothing else:

- **P9-T3a** — the classifier itself: `internal/broker/classify/`, the shared scope predicate
  extracted to `internal/scope/` and reused by the V-6 webhook rule, the 09 §7.1 corpus, and the two
  L0 lints. Covers V-GAT-001/010/011/012/017/021/022 at L1 and V-MET-005.
- **P9-T3b** — `ChangePolicy`: the CRD, the stricter-only admission rule, the `/`-prefix rejection
  on `fieldPaths`, and the broker taking the max over policy sources. Covers V-GAT-009.

The seam is real rather than administrative: `NewClassifier` already takes `[]RuleSet` and the code
floor is one of them, so T3b adds a source to a list T3a shipped. The order matters in one
direction only — a policy that can tighten needs something to tighten first.

**T3b shipped with one scope boundary worth stating, because it is easy to read as a gap.** Nothing
reads a `ChangePolicy` out of a cluster yet. `FromChangePolicy` converts a CR into the `RuleSet` the
classifier already consumes, and the max-over-sources property is proven against it at L1 — but the
informer that would supply live policies belongs to **P9-T7**, which is where a broker pipeline
first exists to consult one. Wiring a watch into a process that classifies nothing would be
scenery. **V-GAT-009 is therefore claimed at L1 and its L2 instance stays open.**

**T3b's scope note has a T3a sibling that only became visible in T4, and it is a defect rather than
a boundary.** T3a's 165-case corpus was green over a `statefulKinds` list that covered no Config
Connector kind at all, so thirteen irreversible cloud deletes — a database, a bucket, a dataset, two
disk kinds, a GKE cluster — classified `routine` with reason `no rule matched`. Section C of the
corpus tests that list faithfully and could never have found this: **a corpus derived from a list
can only check the list's interior.** What found it was T4's cross-package invariant, which runs the
real classifier over the undo generator's own list of kinds it cannot restore. Fixed at the
definition site with 16 new corpus cases (§M, three of them negative) and recorded as [[lsn-033]];
**V-GAT-001 is re-recorded at L1 as a correction, over 181 cases.**

**T4 claims V-REV-003 and V-REV-004 at L1 only.** V-REV-001 (coverage over executed
`ActionRecord`s) and V-REV-009 (a destructive undo is itself gated) are listed L2-only in 09 §5 and
belong to units that do not exist yet — there is nothing executing and no undo controller to gate.

**A level correction to that sentence, made in T5a rather than inherited.** The paragraph above
originally said the **L2** instances of V-REV-003/004 "need an envtest round-trip against a real API
server, which is P9-T5's". Envtest is **L1** by `binding.md` §Targets — a real API server, but
process-local, no cluster. So the **L2 instances still require `gke-scratch-kube-agents-dev` and
stay open**, assigned to the `broker-execute-l2.sh` line in P9-T9. T5a does not strengthen them at
L1 either: nothing in the executor invokes the undo generator, so no round-trip runs at any level
here — what T5a contributes is the pre-state snapshot those L2 instances will be diffed against.
Recording the correction because the alternative — letting a unit quietly redefine a level to the
one it can reach — is how a phase ratchet stops meaning anything.

**P9-T5 ships as two units**, the same seam as T3a/T3b and P8-T8a/b/c. The row is not one
deliverable: it is the write path and everything that happens after the write, and they fit
together only in the sense that one runs after the other.

- **P9-T5a** — the write path. `internal/broker/execute/`: the field manager (produced in one place,
  with its inverse), the one diff used at both ends of the pipeline, snapshot capture with the
  all-or-nothing rule, the executor's three orderings (dry-run-all-then-mutate, write-ahead journal,
  integrity-before-apply), and the API-server-backed `Reader`/`Applier`. **Claims exactly one check:
  V-BRK-020 at L1**, which is the only one of the five whose 09 §6 row lists an L1 instance at all.
  V-BRK-006 is `L2, L4`; V-BRK-018 and V-BRK-019 are `L2`; V-REV-002 is `L2` and phase 10. The
  property each names is implemented here and asserted by the suite — the write-ahead ordering as an
  exact call sequence, all-or-nothing snapshotting including the two-of-three case the check text
  names, the field-manager string and the dry-run-precedes-apply ordering — but **a property proven
  at a level the check does not list is not that check passing**, and the honest record is an
  implementation with its L2 instance still open. All four go to `broker-execute-l2.sh` in P9-T9.
  This is the same discipline that kept V-GAT-012/022 unclaimed in T3a and the L2 halves of
  V-REV-003/004 unclaimed in T4; writing it down again because the temptation is strongest exactly
  when the evidence is good.
- **P9-T5b** — what happens after the write. `internal/broker/verify/`: the per-kind verification
  predicates of 04 §5.1 with their settle windows, transient-versus-terminal classification, the
  recovery ladder in `status.recovery` with no silently skipped rungs, automatic rollback and the
  cooldown that follows it, rollback-failure paging plus auto-pause, and the selector fan-out
  expanded once against live state before classification. **Claims exactly one check: V-PRO-021 at
  L1.** The row's original "covers V-BRK-014, V-REV-005/006" was wrong in both directions and is
  corrected here rather than inherited. V-REV-005 and V-REV-006 are `L2`, phase 10 — the same shape
  as T5a's four, implemented and asserted here and deliberately not recorded, and both go to
  `broker-execute-l2.sh` in P9-T9. **V-BRK-014 is not merely a level mismatch: it is structurally
  unreachable from this unit.** It fault-injects at each of steps 1–10 and asserts the trace shows
  steps 1…k and nothing after; T5b owns step 10 alone, and there is no assembled pipeline to inject
  into until the brake (T6) supplies step 5 and the controller (T7) wires the pair. It is reassigned
  to **T7** below. V-PRO-022 is **deferred on 09 §12 row T-10**, and V-PRO-013 is `L2` and
  additionally blocked on **T-9**; the settle-window numbers this unit had to pick are recorded as a
  decision to be ratified, not as that check passing.

**Two scope boundaries T5a leaves open, stated rather than left to be discovered.** (1) `Executor`
has no caller — `broker.Pipeline` is still `UnavailablePipeline`, because the thing that would
assemble a Request from an envelope needs the brake (T6) and the controller wiring (T7) to exist.
(2) A `patch` against an API that does not honour dry-run is **refused**, not executed. The broker
will not model a server-side merge itself, and modelling it is the only other option: a guessed
merge produces an integrity check that passes on exactly the payload V-BRK-020 exists to catch.

**Two phase-9 pipeline checks are reassigned to T7, found while scoping T5b.** Both are `L1` and
both are in this phase's ratchet, and neither could be claimed by any unit that owns a single step.

- **V-BRK-014** (pipeline step trace) was assigned to T5. It fault-injects at each of steps 1–10 and
  asserts the trace shows steps 1…k and nothing after, with no mutation in the audit log. T5a owns
  step 9 and T5b owns step 10; the property is about the **sequence**, so it needs the whole thing
  assembled. Step 5 is the brake, which is T6, and the thing that constructs a pipeline at all is
  T7's wiring — `broker.Pipeline` is `UnavailablePipeline` until then.
- **V-BRK-011** (pipeline order is observable: classify ≺ gate ≺ snapshot ≺ execute) was in the
  ratchet at the top of this file and **assigned to no task at all** — a planning defect of the kind
  PLAN §3 says to resolve by naming a task rather than discovering it at MILESTONE. Same reasoning,
  same home.

Recording both rather than quietly widening T5b: a unit that claims a check it structurally cannot
exercise is worse than one that leaves it open, because the ratchet then reads as satisfied.

**P9-T6 ships as three units.** The row above is not one deliverable either: it is the brake's
objects, the brake's decision, and the one controller that acts on a brake object. The seam is the
same layering seam T3a/T3b and T5a/T5b used — schema, then the function that reads it, then the
thing that runs. Split before writing code rather than after, per SELECT §2.

- **P9-T6a** — the objects. The rest of `Agent.spec.operations` (06 §1.1's full seven fields),
  `status.operations` and `status.broker`, the three new CRDs (`FleetFreeze`, `ApprovalRoster`,
  `UndoRequest`), their admission webhooks, and `pause` proven inert in the renderer. **Claims
  exactly one check: V-RUN-012 at L0.**
- **P9-T6b** — `internal/broker/brake.go`: the nine fail-closed rules of 06 §4.4 as one decision
  function, plus the contested index. **Claims exactly one check: V-CTR-015 at L1**, allocated in
  09 §6.9 by this unit — see below.
- **P9-T6c** — `internal/controller/undo_controller.go` (`C-UC`), the advisory
  `kube-agents/contested: <action-id>` annotation, and its envtest.

**Seven of T6's eight checks are L2-only, and T6a claims none of them.** The same finding as T5a's
and T5b's, in the same direction. V-BRK-005, V-RUN-007, V-RUN-008, V-RUN-013, V-REV-007, V-GAT-003
and V-GAT-007 are all `L2` in 09 §6: every one of them is about the brake's OBSERVABLE effect on a
running fleet — an agent that stops writing, a freeze that covers a scope, an undo that reverses a
real object — and none is reachable from a Go test, however good. Only **V-RUN-012** lists an L0
instance, and that is what T6a claims. The seven go to `verify-phase9.sh` and `broker-execute-l2.sh`
in **P9-T9**, alongside the ten already routed there.

**T6b allocates V-CTR-015 rather than claiming nothing.** The consequence of the paragraph above is
that T6b — the unit that writes the most safety-critical function in the broker — has no check it
can reach, and would otherwise ship the nine fail-closed rules with their only coverage a shell
script in a later unit that has never been run. The rules are a pure decision function of already-read
inputs, so they are fully exercisable at L1 with no cluster; what was missing was a check ID saying
so. `V-CTR-015` (L1, 06 §4.4, BLOCKING-PHASE) is added to 09 §6.9 and mapped alongside V-CTR-007 on
`03§11#20`, `06§10#45` and `06§10#47`. It does not replace V-CTR-007, which stays L2 and stays T9's:
one asserts the decision function, the other asserts the objects behave that way on a real fleet.
Adding coverage for a property nothing asserted is a tightening, which is the direction PROTOCOL §10
permits; the precedent is P8's `V-CTR-014`.

**The one interpretation in T6b, flagged for a human.** 06 §4.4's pause row says the broker "refuses
new envelopes" and carves out no exception, but **V-REV-007** — "undo works with the originating
agent paused or deleted", BLOCKING-ALWAYS — requires one, because the same section makes an undo a
first-class classified, journaled action, i.e. an envelope through this broker. Resolved by reading
pause the way the same section already reads freeze (`allowUndo` defaults true): **undo is exempt by
origin, not by class.** An undo cannot widen what an agent may newly do, so the exemption preserves
every property pause protects; because an invariant-preserving resolution exists, PROTOCOL §8.5 makes
this a decision and not a halt. The boundary is narrow and tested both ways: undo is exempt from rows
1, 2, 8, pause, and freeze-with-`allowUndo`, and from nothing else — journal, snapshot, undo plan,
roster, budget and post-execution verification all apply to an undo exactly as to any other write.

**T6c allocates V-CTR-016, for the third time and the same reason.** T6c writes `C-UC`, the
controller that actually reverses a change, and every check 09 §6 routes at it is L2: V-REV-007 (L2,
phase 10) and V-REV-001/005/009 all assert an undo against a real fleet. Shipping the preconditions
of an undo with no check that has ever run is the failure mode T6a and T6b already argued; `V-CTR-016`
(L1, 05 §1.3, BLOCKING-PHASE) is added to 09 §6.9 and mapped onto `06§10#41` and `06§10#42`. It
displaces nothing — V-REV-007 stays L2 and stays T9's. Precedents: V-CTR-014 (P8), V-CTR-015 (T6b).
What it asserts is the property a per-branch test would miss: **the preconditions are one shared
predicate and each refuses in isolation against a baseline that is accepted**, plus the two things
that make the linkage trustworthy — that the window is closed AT its boundary, and that a failed
reverse write cannot leave 06 §4.3's bidirectional link one-way.

**Three decisions in T6c, none of them a halt.**

- **The replay route is P9-T7's, not T6c's.** 05 §1.3 step 4 calls
  `POST /v1alpha1/actions/{actionId}/replay`, which does not exist, while V-BRK-021 requires one
  listening port and one mutating route. T6c ships the `Replayer` interface plus `UnavailableReplayer`,
  the same shape `broker.Pipeline`/`UnavailablePipeline` already uses, because the route needs a
  Pipeline to call and T7 is where the pipeline gets constructed. Nothing in T6c claims the route
  exists, and `UnavailableReplayer` makes "not installed" a loud terminal state rather than a silent
  success — `TestUndoWithNoReplayerInstalledDoesNotClaimSuccess` pins it.
- **`undoLinkPending` is a Condition on the `UndoRequest`, not a field on the `ActionRecord`.** 05
  §1.3 names the flag but no API type carries it. It cannot live on the original: the case it exists
  for is precisely a failed write to the original. It is set in the SAME status write that records
  `undoExecuted`, and cleared only once the reverse link lands, so a crash between the two writes
  leaves a durable flag the next reconcile picks up rather than an undo that happened and a record
  that never heard about it.
- **The advisory `contested` annotation is best-effort, and Forbidden is swallowed.** 06 §4.4 says
  the broker "also stamps" it and 05 §1.3 step 5 has `C-UC` mark every target. `C-UC` attempts a raw
  merge patch per target and ignores `Forbidden` and `NotFound`, because the alternative — granting
  the undo controller patch on arbitrary GVKs in every namespace — gives it a write reach larger than
  any agent's, which is the exact shape 03 §3.3 rule 3 exists to prevent. The authoritative refusal
  was never the annotation: it is `status.contested` plus the broker's in-memory index, and 06 §4.4
  says so outright, since the commonest contested case is a human undoing a create and a deleted
  object cannot hold an annotation. Tested both directions.

**P9-T7 ships as seven units**, on the same layering seam T3a/T3b, T5a/T5b and T6a/b/c used: the
thing both halves depend on, then the rendering of the pair, then the objects that pair needs to
actually talk, then the pipeline that runs behind it. (T7d was split out of T7b mid-unit, then split
again into T7d-1/T7d-2, and T7d-2 split once more into T7d-2/T7d-3/T7d-4 when implementing it showed
that its three deliverables live in three different layers with three different verification levels.
**T7d-5 was then added ahead of T7d-4**, from a user question at T7d-3's checkpoint that found the
identities T7d-3 had just written had no install path. See "Why T7b stops at the render", "Why T7d
split in two" and "Why T7d-2 split again" below.)

- **P9-T7a** — `internal/agentlabels/`: the five 08 §2.5 label keys spelled once, and the injective
  scope renderer. Every other T7 deliverable stamps these; nothing else in T7 can be written without
  agreeing on them first. **Claims V-RUN-011 at L0 and L1.**
- **P9-T7b** — the pair itself, as the controller renders it: broker Deployment and
  `<agent>-broker` Service on 8443 applied **before** the agent Deployment, both owner-referenced;
  the pair-atomic `LaunchSpec` and `WorkloadPair`; `BrokerReady`/`AgentReady` with `Ready` their
  conjunction; the `wait-for-broker` init container with observe-and-report on timeout; the five
  `KUBEAGENTS_BROKER_*` env vars, injected last so a CR author cannot redirect them; the actor
  ServiceAccount **name**; goldens. **Claims V-RUN-003 and V-BRK-012, both L0.**
- **P9-T7d-1** — **trust**: the mesh CA (`kubeagents-mesh-selfsign` ClusterIssuer → an `isCA`
  `Certificate` in cert-manager's namespace → the `kubeagents-mesh-ca` ClusterIssuer) as static
  install-time manifests under `config/mesh-ca/`, plus the two per-agent cert-manager
  `Certificate`s behind `<agent>-broker-tls` and `<agent>-mesh-tls`, rendered by the controller and
  owner-referenced. **Claims no new L0 check**; six L1 property tests, of which the load-bearing one
  is the SPIFFE binding (below).
- **P9-T7d-2** — **the pair's own NetworkPolicies**, rendered by the controller and
  owner-referenced: `<agent>-broker-ingress` (the broker default-deny on ingress, admitting exactly
  the peer matching `kube-agents/agent: <name>` **and** `kube-agents/role: reader`) and
  `<agent>-to-broker` (the agent's one egress hop to :8443). **Claims no new L0 check** — six L1
  property tests over the selectors; the packet-level properties are V-ISO-001/002 at L2 in P9-T9.
- **P9-T7d-3** — **the actor identity**: the actor `ServiceAccount` per agent and the Role/RoleBinding
  carrying **exactly** 06 §2.2.1's broker-operations grant and nothing else, as GitOps artifacts under
  `policy/rbac-overlay/` with the derived exemplars under `examples/gitops-repo/`. **Claims V-BRK-013
  at L0** — the two-sided assertion planning defect 1 resolves Accept (e) into.
- **P9-T7d-4** — **the install-path egress holes**: the API-server rule the broker's own pipeline
  needs, added to `netpol-agent-egress.yaml.template` as a rendered optional block with the
  control-plane CIDR supplied by `vars.sh`, plus the regenerated exemplars. **Done 2026-07-28.**
  Verified by `dev/tests/reference-render.py` at L0 (**V-CTN-020**, L0 half) and by V-ISO at L2 in
  P9-T9.

  **Rule 9 is the one destination in this allowlist that cannot be pinned in a committed file**, and
  that shaped the whole unit. Every other address here is published and stable — Google's restricted
  VIP, GitHub's four blocks, GKE's two metadata pairings — so the exemplars can state them as facts.
  The API server's is per-cluster, and these clusters are public GKE (no `--master-ipv4-cidr` or
  `enable-private-nodes` anywhere under `k8s-operator/scripts/` or `dev/cluster/`), so the endpoint
  is a bare IP with no range anyone publishes. A committed exemplar could only pin a fiction about
  somebody's cluster. So `provision_13` resolves it at apply time — the `kubernetes` Service
  ClusterIP, the kubeconfig endpoint, or an explicit `KUBE_APISERVER_CIDR` — and **refuses to apply
  without one** unless `KUBE_APISERVER_EGRESS_ENABLED=false` is set deliberately.

  **That inverts this file's default on purpose.** Every other optional block is absent-unless-asked
  because absent is the safe direction. Rule 9's absent direction closes the broker's write path:
  no TokenReview (pipeline step 1), no FleetFreeze read (step 5), no ActionRecord write (step 11),
  and no kubectl-shaped skill for the reader — reported to the operator as an authentication error
  that never mentions the network. A default of "absent" would rebuild the hole this unit closes.

  **Two changes beyond the literal scope of the bullet.** First, `dev/tests/reference-render.py`
  gained a **source** property (10) as well as three behavioural ones: properties 7–9 are all true
  of a resolver nobody obeys, so an edit turning `provision_13`'s `else` arm into a warning would
  leave them green. Second — and this is the real find — the byte-for-byte gate caught the drift in
  the exemplars (regenerated; comment-only, no rule 9 in them) and `dev/test_skill_templates.py`
  then caught the third copy, which surfaced that the `propose-developer-team` bundle is a **second
  install path** for the developer-team tier: it is applied by the customer's CI/CD, not by
  `provision_13`, so a tenant provisioned through the F4 cascade would have shipped without rule 9.
  Hence `--kube-apiserver-cidrs` on `render_developer_team.py`, documented **unbracketed** in
  SKILL.md as the one flag whose omission is not the conservative choice, and bound in both
  `WIDE_ENV` and `WIDE_FLAGS` so the two halves cannot diverge.

  **Deliberately not in scope**: the L2 half of V-CTN-020 — that the policy is actually enforced and
  that the broker's pipeline actually completes over it — is P9-T9's, and P4 still governs it (on a
  non-enforcing dataplane an egress claim is `deferred`, never `pass`). Nor does this resolve a
  hostname in the kubeconfig `server:` URL: a policy pinned to whatever DNS answered at install time
  stops matching after a control-plane rotation, silently, and refusing is the better failure.

- **P9-T7d-5** — **the install path for the identities T7d-3 just wrote.** Added 2026-07-28, run
  before T7d-4, **done 2026-07-28**. Renders the reader and actor `ServiceAccount`s, the shared
  broker-operations `ClusterRole`/`Role` and the two bindings from `common.sh` — the
  `render_tenant_quota` / `render_wi_metadata_block` idiom, one source and one render — applied from
  numbered steps in `provision_08` (platform) and `provision_12` (cluster-admin, developer-team),
  with matching `delete_agent_identity` calls in both teardowns. **Claims V-CMP-007 at L0** as
  `dev/tests/identity-has-install-path.py`.

  **Why it exists.** T7d-3 shipped the actor identity into `policy/rbac-overlay/` and the per-cluster
  bundles, and a user question at CHECKPOINT surfaced that nothing on the install path creates it.
  The accurate finding, narrowed after a read-only sweep of the live `platform-agent-host`, has three
  parts: the cluster-admin and developer-team **reader** SAs _were_ created imperatively, inline in
  `cluster-admin-agent.yaml.template` and `developer-team-agent.yaml.template`; the **platform**
  reader SA was created by nothing at all — `kubeagents-platform-agent` on the live cluster is a bare
  hand-applied SA whose `last-applied-configuration` carries no labels and whose Workload Identity
  annotation is not in it either; and **no actor SA and no broker-operations grant existed anywhere
  on any install path**, so the broker Deployment T7d-3 renders would reference an identity that does
  not exist and the pod would not start. No install-path identity carried `kube-agents/role`, which
  is the label both VAP arms now select on.

  That is [[LSN-039]], an escape against the already-closed [[LSN-007]]: `install-path-wired.py`
  walks the _script_ graph and every one of its five properties passes on a repository whose steps
  run perfectly and apply none of the security manifests. `common.sh:656` had already found and fixed
  the same class for the tenant quota and the namespace default-deny without generalizing, so this is
  the third instance.

  **Two changes beyond the literal scope, both single-definition-site moves.** The inline
  `ServiceAccount` blocks were **deleted** from the two tier templates rather than relabelled, so the
  reader identity has exactly one source; what stays in those templates is the tier's authority.
  And `platform-agent.yaml.template` gained `spec.scope.projectId`: without it the platform actor
  renders `platform--actor` and the broker's `validate()` refuses an empty `--scope`, so the pair
  would come up `BrokerReady=false` forever. `spec.scope` is mutable (only `spec.tier` is immutable),
  and the added scope still strictly contains every cluster-admin scope under it.

  **Deliberately not in scope**, by user decision: the 45 stale `app.kubernetes.io/managed-by: gitops`
  sites across 24 files, and the 08 §2 / §2.7 / §4 "GitOps-managed" wording that contradicts 05 §C13
  and 06 §4. Those are documentation and a spec correction; this is a pod that will not start.
  **Also not in scope, and carried forward explicitly**: the planned declare-or-fail table over
  `examples/gitops-repo/` — V-CMP-007 walks the manifest→step edge for the **install path**
  (`k8s-operator/scripts/`), which is what makes the identities real, but it says nothing about which
  files in the exemplar tree are inert. That property belongs to the queued sweep unit, which is the
  unit that touches that tree.

**Why T7d split in two.** Two reasons, and the second one changed what T7d-2 is allowed to contain.

The first is the level seam, which is the same one that produced T7b/T7d: T7d-1's properties are
_renderable_ — one issuer for both ends, the right SPIFFE URI, key rotation on renewal, the Secret
names matching what T7b mounts — and every one of them is an L1 assertion that would otherwise
surface at L2 as a TLS handshake error, the least informative available report. T7d-2's properties
are packet-level (V-ISO-001/002 assert a packet is _dropped_) and are already routed to P9-T9.

The second is that **the actor ServiceAccounts cannot be controller-minted**, and finding that out
is what forced the split rather than merely justifying it. P1-T4/T5 (08 §4) already settled this for
the reader identity: the controller holds `serviceaccounts: get;list;watch` and a comment in
`agent_controller.go` reading _"Do not re-add RBAC write verbs"_, because agent identity is
pre-created and GitOps-managed, enforced by `vap-agent-readonly`. The actor SA is the _higher_-
authority half of the pair, so if the reader's may not be minted at runtime, the actor's certainly
may not — 06 §2.2.1's "the ability to name the actor identity is the ability to choose an authority
level" applies with more force, not less. The actor identity is therefore a GitOps-artifacts unit
(`policy/rbac-overlay/`, `examples/gitops-repo/`), not a controller unit, which is a different kind
of work from T7d-1 and shares no code with it.

**Why T7d-2 split again.** The sentence above lumped three deliverables under "GitOps artifacts", and
implementing the first one showed that only two of the three are. **The pair's NetworkPolicies are
controller output, not install-time YAML** — 08 §2.7's grant table gives the controller full CRUD on
`NetworkPolicies`, bounded to "objects the controller owns via `OwnerReference`", and
[05](../design/05-system-architecture.md) §1 C1 lists "the pair's NetworkPolicies" among what the
controller reconciles. They cannot be install-time artifacts anyway: they select on
`kube-agents/agent`, and the CR that value names does not exist when the installer runs. The per-tier
egress policies stay exactly where they are, because they select on `kube-agents/tier` and encode a
fleet decision a human makes in a PR. So the split is by **layer**, and it lines up with the levels:
selectors are L1, RBAC verbs are a static L0 assertion (V-BRK-013), and the install-path template has
its own L0 check in `reference-render.py`.

**What T7d-2 found and did not close: the broker cannot reach the API server.** The broker pod carries
`kube-agents/tier`, so the per-tier egress policy selects it and makes it default-deny on egress — and
that allowlist has no API-server rule. Its four destinations are DNS, the control namespace on
:80/:8080 (LiteLLM and the token minter), `restricted.googleapis.com`, and GitHub's published CIDRs.
None of those is the kube-apiserver, which the broker needs for **three of its eleven pipeline steps**:
TokenReview (step 1), the FleetFreeze read (step 5), and the ActionRecord write (step 11). Nothing
rendered by the controller can fix it — NetworkPolicy cannot name a Service, so "allow the
`kubernetes` endpoint" is not expressible, and the control-plane CIDR is per-cluster and known only at
install time. It is **P9-T7d-4**, and it is called out here rather than left in a code comment because
the symptom is a broker that authenticates nobody, which reads as an auth bug and sends the debugger
at `internal/broker/auth.go`. Note that the same gap has been latent for the READER since Phase 5 —
whether the agent's own API reads survive it is an L2 question, and P9-T9 is where it gets asked.

**Closed by T7d-4 as egress rule 9** (2026-07-28). The prediction above held in every respect except
one, and the exception is worth keeping: "the control-plane CIDR is per-cluster and known only at
install time" is right, but it understates the case for a public GKE endpoint, where there is no
_range_ at all — only a bare IP that changes when the control plane rotates. That is why rule 9 is
the one rule in the file with no committed exemplar and why `provision_13` refuses to apply without
resolving it, rather than shipping a default. It also emits **both** address forms (the `kubernetes`
Service ClusterIP and the kubeconfig endpoint), because whether NetworkPolicy sees egress before or
after DNAT is dataplane-specific — the same reason `GKE_DATAPLANE` defaults to `auto`.

**What T7d-1 found: the mesh certificate is half of the broker's identity check, not just its
transport.** `internal/broker/auth.go` authenticates the caller by TokenReview, compares the result
against the single `ExpectedCaller` it serves, and then _binds the two layers_ — it refuses with
`ReasonPeerMismatch` unless the client certificate's SPIFFE URI equals the ID derived from the
token. So `<agent>-mesh-tls` must carry `spiffe://cluster.local/ns/<ns>/sa/<readerSA>` exactly, or
every envelope in the fleet is refused at the transport layer, with an error message about trust
domains, discoverable only at L2 after a rollout. The format now has **one definition site**,
`broker.SPIFFEID`, called by both `auth.go` and the renderer; a test asserts both that the rendered
URI equals what that function produces _and_ that the function still produces the canonical
`spiffe://<td>/ns/<ns>/sa/<sa>` shape, since two callers agreeing with each other is not the same as
being right.

**And a uniqueness dependency worth stating.** Certificate and Secret names derive from `agent.Name`
and are unique because the API server says so; the _actor_ SPIFFE ID derives from `(tier, scope)`
and not from the name at all (06 §5.1 forbids the name being an input, since naming the identity is
choosing the authority). Its uniqueness therefore rests entirely on admission enforcing (tier, scope)
uniqueness fleet-wide. Two same-tier same-scope agents would get distinct certificates carrying the
_same_ actor identity — unrepresentable today, but the mesh's identity uniqueness is a property of
the **webhook**, not of the renderer, and nothing in the renderer would notice if that changed. This
was found by writing the collision test with a two-agent fixture and having it fail (LSN-015 again:
one CR could not have caught it).

**cert-manager's Go types are deliberately not a dependency.** Adding
`github.com/cert-manager/cert-manager/pkg/apis/certmanager/v1` was tried and reverted: it upgrades
every `k8s.io/*` module in the operator and pulls `sigs.k8s.io/gateway-api` — an unrelated API
surface — into the binary that reconciles the write-credential path. Two struct literals do not
justify that, and the controller only ever _writes_ these objects, so the type safety traded away is
type safety over a value nothing in this process reads. They are rendered as `unstructured`.

- **P9-T7c** — the pipeline behind the pair: **V-BRK-011** and **V-BRK-014** at L1, the
  `ChangePolicy` informer T3b deferred here (V-GAT-009's L2 instance stays open), and the
  `POST /v1alpha1/actions/{actionId}/replay` route plus the HTTP `Replayer` T6c deferred here.

**The split is driven by level as much as by size.** Of T7's fifteen listed check IDs only five are
reachable without a cluster — V-RUN-011 (L0, L1), V-RUN-003 (L0), V-BRK-012 (L0), V-BRK-011 (L1) and
V-BRK-014 (L1). V-RUN-001/002/004/005/009 and V-ISO-001/002 are `L2` in 09 §6: they assert that the
pair actually runs, that the init container actually blocks, and that the NetworkPolicy actually
drops a packet. Those go to **P9-T9** with the seventeen already routed there, and so does
V-RUN-003's own `L2` half — its `L0` half is the hardened `securityContext` P8 already renders,
re-asserted here against the broker by T7b's goldens and by `TestBrokerDeploymentPosture`.
V-RUN-006 ("agent with no broker fails closed into observe-and-report") is `L2` and **phase 10**, so
it is claimed by nothing in phase 9; T7b's `cmd/broker` tests exercise the same clause at L1 as
supporting evidence and no more.

**Why T7b stops at the render, and T7d exists.** T7b as first written also owned the TLS Secrets,
the broker NetworkPolicy and the agent's egress-to-broker rule. Two things came out of implementing
it that make those a different unit rather than the tail of this one.

The first is that **the Secrets cannot be rendered — only the certificates that fill them can, and
the issuer they need does not exist.** 08 §2.3 wants mutual TLS between two ends that verify each
other, which means one CA signing both. The only `Issuer` in this repo is the namespaced
`selfsigned-issuer` the webhook uses, and self-signing each `Certificate` separately gives the
broker end and the agent end **different** CAs — a pair that then fails the handshake it exists to
perform. So T7d has to introduce a mesh CA `ClusterIssuer` and a CA `Certificate` under it first,
which is a cert-manager API-types dependency and a cluster-scoped object, not a line in
`broker_manifests.go`. Nothing in T7b is blocked by the gap: the Deployment mounts
`<agent>-broker-tls` and `<agent>-mesh-tls` by name, and until T7d creates them the pair stays
`BrokerReady: false` — fail-closed, which is the required direction.

The second is that **the NetworkPolicy's whole property is at L2.** Its check IDs (V-ISO-001/002)
assert that a packet is actually dropped; rendering the YAML proves nothing they ask about, and both
are already routed to P9-T9. Pairing the policy with the certificates and the actor SAs — the three
things that turn a rendered pair into a talking one — keeps one unit's worth of "the pair runs"
together instead of splitting it across a render unit that cannot test it.

Same reason for the actor ServiceAccounts. T7b derives the **name** (`<tier>-<leaf>-actor`,
truncated per 06 §5.1) because the Deployment has to name something, and pins that derivation with a
test. Creating the SA, and binding it to the empty role 06 §2.2.1 requires, is T7d's.

**Why the label renderer is its own unit and not three constants at the top of `agent_manifests.go`.**
V-RUN-011 calls a scope-label collision "an authority bug, not a cosmetic one", and it is right in a
way that is easy to under-read. 03 §4.2 pins a pod to its ServiceAccount by asserting the pod's
`kube-agents/tier`, `kube-agents/scope` and `kube-agents/role` match the SA's; 08 §2.5 keys the mesh
NetworkPolicy and the per-scope quota on the same value. A scope key is
`<project>.<cluster>.<namespace>` — 30 + 40 + 63 characters against a 63-byte label ceiling — so
**truncation is the default path, not an edge case**, and truncation alone maps two namespaces in one
long-named cluster onto one label. The pinning selector then stops distinguishing two credentials it
exists to distinguish. So `RenderScope` is built to make injectivity an argument rather than a hope:
a short legal value passes through unchanged (output _is_ input), anything else becomes a readable
prefix plus a 10-hex digest **of a length-prefixed canonical encoding** of the three levels, and a
literal that would _look_ hashed is pushed into the hashed set so the two sets cannot overlap. The
residual is stated, not hidden: a 40-bit digest collision between two scopes that also share a
52-byte prefix.

**T7a found a real defect in its own renderer, and the corpus is why.** The first draft hashed the
readable join, so `{acme, prod.eu, payments}` and `{acme, prod, eu.payments}` both rendered
`acme.prod.eu.payments` — and both were short and legal, so both took the pass-through path and the
digest never ran. The join is ambiguous; the fix is to hash a length-prefixed encoding instead, and
to require every level to be a DNS-1123 label (which forbids `.`) before allowing pass-through. The
`¬` control 09 requires is `TestTheCollisionCorpusBreaksANaiveRenderer`, which runs a naive
`truncate(sanitize(key), 63)` over the same 16-entry corpus and asserts it collides — without it, a
corpus that everything survives would prove nothing about the corpus.

**The journal's `kube-agents/scope` means something else, and T7a does not change it.** 08 §2.5
defines the key as the rendering of the whole scope key; 06 §4.3's ActionRecord examples and the
06 §5.1 ServiceAccount table use the same key for the scope **leaf** (`team-x`, `cluster-a`). These
are different objects, so it is not a contradiction — but 03 §4.2 compares a pod's value to its SA's,
and a leaf is not injective across a fleet (two clusters each with a `team-x` namespace render the
same label). T7a single-sources the **key spellings** so that `internal/journal` and the renderer
cannot drift on the string, leaves the journal's value derivation exactly as it is, and declares it
in the lint's exemption table with that reason. Reconciling the two meanings is a spec question with
a real blast radius and no check pointed at it, so it is recorded as an open item rather than settled
inside an implementation unit (PROTOCOL §10).

**V-RUN-012 ships as two halves, and the negative control is not hypothetical.**
`resolveDeploymentReplicasAndStrategy` already renders `replicas: 0` for `spec.deployment.scaleToZero`,
an unrelated idling feature. So "make pause set replicas to 0" is one `||` three lines from code that
already does exactly that, it reads in a diff as tidy reuse, and it passes every test that does not
specifically render a paused agent. The L1 half
(`internal/controller/pause_not_scale_to_zero_test.go`) renders a paused and an unpaused Agent and
asserts the Deployment specs are deeply equal — deep equality rather than a replica assertion,
because any difference rolls the pod and V-RUN-012 requires the pod UID and start time to survive.
The L0 half (`dev/tests/pause-is-not-scale-to-zero.py`) asserts the shape that survives the renderer
moving. `scaleToZero` itself is the `¬` control: the same mechanism, reached through the field that
is allowed to use it.

**A spec rule with no check and no owner, found while writing T6a's principal patterns.** 06 §1.2
**V-11** requires platform-qualified principals (`^(slack|googlechat):\S+$`) on
`Agent.spec.integration.*.allowedUsers`. It is enforced nowhere in Go, it has **no check ID anywhere
in 09**, and no task in `docs/build/` names it — P8-T9 covered V-6, V-8 and V-10 only. Not fixed
here: adding a validation rule to satisfy a check that does not exist is a different unit of work
(PROTOCOL §10), and the missing check is the larger half of the problem. T6a does implement the V-11
FORM on every principal field it introduces (`FleetFreeze.spec.requestedBy`,
`UndoRequest.spec.requestedBy`, `Approver.Principal()`), extended with a third platform, `k8s:` — a
human running `kubectl` during an outage has a Kubernetes username and no Slack ID, and a schema
that could not express that identity would make the API brake unusable in the exact failure it is
specified for.

---

## Deferrals opened by this phase (each with a named external blocker)

Recorded at PLAN time so they are visible from the start rather than discovered at the gate. **No
BLOCKING-ALWAYS check appears here** — that is planning defect 2's entire reason for existing.

| Check / bullet                     | Blocker (external, named)                                                                                                     | Owner | Promote when                                          |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- | ----- | ----------------------------------------------------- |
| V-CMP-006 at **L3**                | The live install `platform-agent-host` must be rebuilt on the fixed render before the inter-agent call can be exercised there | human | The live install is rolled to a build carrying P9-T10 |
| V-BRK-006 at **L4** (soak)         | L4 is the multi-day soak level; no soak harness exists before Phase 14                                                        | —     | Phase 14 stands up the soak lane                      |
| V-REV-008 at **L4** (retention)    | The 30/90/365-day TTL clocks cannot be observed inside a phase; L2 asserts the fields and the deletion predicate              | —     | Phase 14                                              |
| V-RUN-014 at **L3**                | One Socket Mode connection fleet-wide is only observable on the live install                                                  | human | Live install rebuilt                                  |
| Accept (a)/(b) at **L3** (carried) | Standing from Phase 8 — no empty GCP project for a clean-clone install                                                        | human | An empty scratch project exists                       |

The three L4 rows are level deferrals of checks whose L1/L2 instances **do** run in this phase; the
BLOCKING-ALWAYS rule is about a check having no evidence at all, not about its deepest level.

---

## Notes carried into IMPLEMENT

- **P1 before every L2 judgement.** `dev/cluster/reload-images.sh` now has to grow a **broker**
  target (P9-T2). Until it does, no L2 broker claim is admissible — a broker image deployed by tag
  is LSN-001 with a new binary. Build all seven-plus-one images concurrently on Cloud Build; never a
  host-arch `docker build` (the Makefile exits 2 on arm64, deliberately).
- **The tree freezes for the duration of a gate run.** Every new commit invalidates the deployed
  image and therefore P1 for every suite still to come. Rebuild once per unit, not once per commit.
- **P6 / LSN-003:** assert against the **operator-rendered ConfigMap**, not the image-baked
  `config.yaml` it shadows, and name which one the check reads. P9-T10 touches both sites and is
  exactly where this bites.
- **LSN-015:** any per-agent resource is exercised with **≥2 agents in one namespace**. The broker is
  per-`Agent`-CR, so P9-T7's pair rendering must be checked with two CRs, not one.
- **LSN-024 / planning defect 2:** the fixture broker is rendered by the shipped renderer. A fixture
  that diverges from the render is scenery, and scenery passes.
- **The destructive-test guard stays anchored** on `gke-scratch-*`. Every new L2 script in P9-T9
  carries it, and `invariants-gate.py` asserts the anchoring. `platform-agent-host` is never a
  destructive-test target — and in this phase, where the overlay grants real write authority, that
  matters more than it ever has.
- **`internal/router/classify.go` is not the risk classifier.** P9-T3 creates
  `internal/broker/classify/`. Two things named `classify` in one tree is the kind of collision that
  produces a correct-looking import of the wrong package.
- **The classifier reads live state, never prose.** `intent` and `rationale` are journaled and are
  never classification inputs (V-GAT-017 asserts the package imports no inference client and that
  100 permuted rationales yield byte-identical classifications).
- **`ChangePolicy` cannot loosen, and that is structural, not validated-then-trusted**: there is no
  `allow`, no `maxClass`, no `exempt`, no downgrade path in the schema, _and_ the broker takes the
  maximum over all sources regardless. Both halves get a test; either alone is a convention.
