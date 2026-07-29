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
  `broker-execute-l2.sh` in P9-T9. (V-REV-006's level list was later widened to `L1, L2` by
  **T7c-3c-ii-b-1**, which supplied the missing L1 half — a real recorder writing to a real API
  server, which T5b did not have. V-REV-005 is still `L2` and still owed.) **V-BRK-014 is not merely a level mismatch: it is structurally
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

- **P9-T7d-6** — **make the install overlay render, and render faithfully.** Added 2026-07-28, run
  before T7c-3c-ii-b-2-b, **done 2026-07-28**. Pins six ambiguous `Certificate` replacement
  selectors to `name: serving-cert`; lifts `../mesh-ca` out of `config/default` into a new
  transformer-free `config/install`; repoints `deploy`, `undeploy`, GitOps bootstrap wave 10 and the
  `propose-cluster-admin` template at `config/install`; adds a `render` target wired into `build` and
  `test`. **Claims V-CMP-008 at L0** as `dev/tests/install-render-is-faithful.py`.

  **Why it exists, and why it is not part of 2-b.** Surveying for T7c-3c-ii-b-2-b — which gives C-BR
  its own ServiceAccount, RBAC and Deployment, and therefore has to render and apply them — found
  that `kustomize build config/default` exits non-zero and has done since PR #44 (`1385649`,
  2026-06-28) landed the mesh CA. `make deploy` is the sanctioned install path and
  `provision_03_gcp_gke_operator.sh` goes through it, so for a month the install did not work at all
  and nothing said so: no L0 line, no L2 line, and no CI workflow renders the overlay. 2-b is
  unachievable on top of it, so this is sequenced ahead, on the same precedent as T7d-5.

  **Two defects, one root cause, and they must ship together.** The visible one is the render error:
  the mesh CA added a second and third `Certificate`, which made two `replacements` selectors written
  as bare `kind: Certificate` match three objects. The one it was hiding is worse. `config/default`
  carries `namePrefix: kubeagents-` and `namespace: kubeagents-system`, a kustomize transformer
  reaches every resource beneath it with no per-resource opt-out, and the CA cannot survive either:
  the prefix renames `ClusterIssuer/kubeagents-mesh-ca` — the one string `meshCAIssuerName` in
  `mesh_trust.go` hardcodes — into `kubeagents-kubeagents-mesh-ca`, and the namespace moves the CA
  `Certificate` out of `cert-manager`, which is the only namespace a `ClusterIssuer` resolves
  `ca.secretName` from. Neither rewrite errors and both apply. **Pinning the selectors alone is
  strictly worse than the status quo**: today nothing installs; with only the pin, a broken trust
  root installs silently and surfaces days later as brokers that never become Ready behind agent
  `Certificate`s stuck `Pending`. That coupling is why this is one unit and not two.

  **Beyond the local fix, because the local fix does not reach a real cluster.** GitOps bootstrap
  wave 10 and the `propose-cluster-admin` skill's `10-controller` template both pull the overlay by
  URL, both were pinned to `config/default`, and that is the path a cluster actually takes. Left
  alone they would bootstrap a control plane with no trust root even after `make deploy` was correct.

  **The mechanization is deliberately split across two levels**, because "it renders" and "what it
  renders is the install" are different properties with different costs. The first is `make render`,
  a prerequisite of `build` and `test` — it needs the kustomize binary, so it cannot be an L0 line
  (`.github/workflows/l0-checks.yml` installs no dependencies on purpose; a check that needs a
  package is not L0), and CI reaches it because `k8s-operator-test.yml` runs `make -C k8s-operator
test`. The second is the L0 check, which asserts the **reference graph** rather than the output:
  no transforming kustomization may reach `config/mesh-ca` over the whole inclusion graph, not just
  the edge that broke, so re-nesting the CA under a new transforming layer next year fails too.

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
  **Split into four**, see "Why T7c split into four" below.
  - **P9-T7c-1** — `internal/broker/steps.go` and `internal/broker/pipeline/`: the observable step
    trace and the assembly of steps 3–11. **Claims V-BRK-011 and V-BRK-014 at L1. Done
    2026-07-28.**
  - **P9-T7c-2** — the two deferrals, **split again into 2a and 2b** when 2b turned out to be a
    halt. See "Why T7c-2 split" below.
    - **P9-T7c-2a** — the live `ChangePolicy` source (from T3b): `internal/broker/policy/` and the
      `pipeline.Config.Classifier` seam. **Re-records V-GAT-009 at L1 over the live loader. Done
      2026-07-28.** V-GAT-009's L2 instance stays open.
    - **P9-T7c-2b** — the `POST /v1alpha1/actions/{actionId}/replay` route plus the HTTP
      `Replayer` (from T6c). **DEFERRED out of Phase 9, 2026-07-29, by human ruling** on the halt
      recorded below. Blocker: **a human decision on which of 05 §1.3 and 03 §4.1 is authoritative
      about the `/replay` route** — the two specs disagree and PROTOCOL §8.5 forbids the harness
      picking a side. Nothing else in Phase 9 depends on the route, so the phase closes without it
      and it is rescheduled to the phase that resolves the spec.

      **V-BRK-021 is _not_ deferred and stays green.** It is BLOCKING-ALWAYS, and a BLOCKING-ALWAYS
      check may never be deferred. What is deferred is the _task_; the check continues to assert what
      it asserts over the routes that exist. This distinction is the whole reason the ruling was
      safe to take.
  - **P9-T7c-3** — **the runtime wiring.** Real client-backed adapters for the twelve seams
    `pipeline.Config` takes — `LiveState`, `Applier`, `Reader`, `BodyStore`, `Prober`,
    `Rollbacker`, `Pager`, `Pauser`, the cooldown registry, `ActionHistory`, `ReferenceIndex`,
    `BrakeSource` — and a `pipeline.New` call in `cmd/broker/main.go` where
    `broker.UnavailablePipeline{}` is today. Until it lands, **LSN-007 applies to the whole
    pipeline**: it is built, tested, and unreachable from the binary. **Split into four**, see "Why
    T7c-3 split into four" below.
    - **P9-T7c-3a** — `livestate.Source`, the `LiveState` adapter: the five reads every
      classification rung depends on. **Claims V-GAT-022 at L2. Done 2026-07-28.**
    - **P9-T7c-3b** — `undo.ReferenceIndex` and `execute.BodyStore`. **Allocates and claims
      V-REV-010 at L1 and L2. Done 2026-07-28.**
    - **P9-T7c-3c** — the verify adapters: `verify.Prober` (eight methods), `Rollbacker`, `Pager`,
      `Pauser`, `CooldownRegistry`. **Split into three at ORIENT**, see "Why T7c-3c split into
      three" below.
      - **P9-T7c-3c-i** — `internal/broker/probe`, the `verify.Prober`: the eight probes behind the
        eight rows of the 04 §5.1 table. **Allocates and claims V-PRO-027 at L1 and L2. Done
        2026-07-28** — seven of the eight rows exercised; the eighth, connectivity, is deferred with
        a named human owner, because a prober that can dial from another pod's network position is a
        deployable workload with its own RBAC and blast-radius argument, not a method body.
      - **P9-T7c-3c-ii** — `Rollbacker`, `Pager`, `Pauser`: the three effects rungs 3 and 5 of the
        04 §5 ladder actually have on the world. **Split into two at IMPLEMENT**, see "Why
        T7c-3c-ii split into two" below.
        - **P9-T7c-3c-ii-a** — `internal/broker/rollback`, the `verify.Rollbacker`: rung 3, the
          replay itself. **Allocates and claims V-REV-011 at L1 and L2. Done 2026-07-28.**
        - **P9-T7c-3c-ii-b** — `Pager` and `Pauser`, rung 5, **plus the controller-side C-BR
          reconciler they both have to go through.** Blocked on a component that does not exist,
          which is why they are not in ii-a. **Split into two at IMPLEMENT**, see "Why T7c-3c-ii-b
          split into two" below.
          - **P9-T7c-3c-ii-b-1** — the **request** side: `status.escalation` on the `ActionRecord`,
            `internal/broker/escalate` behind `verify.Pager` and `verify.Pauser`, and the
            `Pauser` interface change that lets a pause name the record it belongs to. **Claims
            V-REV-006 at L1**, whose level list is widened from `L2` to `L1, L2` — a strengthening,
            nothing removed.
          - **P9-T7c-3c-ii-b-2** — the **fan-out** side: the controller-side C-BR reconciler that
            turns a recorded escalation into `spec.operations.paused` and a page. **Claims V-REV-006
            at L2**, which needs the operator image rolled by digest — **P1 in full**, for the first
            time in this task chain. **Split again at IMPLEMENT** under the `harness-run` §2 sizing
            rule, because the deploy half is a different kind of work from the code half and
            carrying an oversized unit forward is what the rule forbids:
            - **P9-T7c-3c-ii-b-2-a** — the reconciler, its two rows in
              `vap-agent-scope-journal`, and the L1 suites. No deploy, no cluster, no new identity;
              the controller is deliberately wired into no manager yet. **Claims V-REV-006 at L1**
              — the fan-out half, completing the L1 story ii-b-1 opened. **Done 2026-07-28.**
            - **P9-T7c-3c-ii-b-2-b** — C-BR's own ServiceAccount, RBAC, Deployment and kustomize
              wiring, the `--controllers` selector in the manager binary, `make cloud-build-push`,
              a roll by digest, and **V-REV-006 at L2 with P1 in full**. **Done 2026-07-29** — 39
              assertions, exit 0, twice. The `ClusterRole` is hand-written and carries no
              `+kubebuilder:rbac` markers, because a marker on `BrakeReconciler` composes into the
              operator's `manager-role`; the `Deployment` lives in `config/manager` and not a base
              of its own, because the `images:` transformer reaches only what is beneath the
              kustomization declaring it; and `parseControllers` refuses to combine `brake` with
              any other controller, because a process runs as one ServiceAccount and 06 §4.3 keeps
              C-BR's and the exporter's authority over `ActionRecord.status` disjoint. Opened
              [[LSN-044]] and [[LSN-045]], both defects in the check this unit authored.
      - **P9-T7c-3c-iii** — a durable `CooldownRegistry`. `verify.MemoryCooldown` says in its own
        doc comment that it is "deliberately not the production store: a cooldown that dies with
        the broker process is a cooldown an operator can clear by deleting a pod, and 04 §4.2
        controls must survive that." **Allocates and claims V-PRO-028 at L1. Done 2026-07-29** —
        `internal/broker/cooldown`, derived from the `ActionRecord` journal, sharing the backoff
        fold (`verify.CooldownSeries`) with the reference implementation so the two agreeing is a
        property a test asserts rather than two transcriptions somebody keeps in step. See "What
        T7c-3c-iii asserts" below.
    - **P9-T7c-3d** — `pipeline.BrakeSource`, `broker.ContestedIndex`, and the `pipeline.New` call
      in `cmd/broker/main.go` that replaces `broker.UnavailablePipeline{}`, plus `policy.Source`
      construction with a synchronous startup `Refresh`. **Closes LSN-007.** Necessarily last: it
      is the only sub-unit that needs every other adapter to exist. **Split four ways at ORIENT**
      under the `harness-run` §2 sizing rule. The task text assumed the wiring was the work and
      that "every other adapter" already existed. It does not: reading the seams found **three
      production implementations missing outright**, two of which the pipeline refuses to start
      without and one of which no code has ever constructed. Wiring `pipeline.New` on top of them
      would either not compile or would compile into a broker that fails every non-dry-run
      execution at the write-ahead check — so the wiring is genuinely last, and there are three
      units in front of it rather than none.
      - **P9-T7c-3d-i** — `internal/broker/brake`, the production `pipeline.BrakeSource`. Gathers
        the four inputs 06 §4.4 needs that are reads: the broker's own `Agent` CR (row 2), the
        cluster-scoped `FleetFreeze` list stamped with `ObservedAt` (row 1), the resolved
        `ApprovalRoster` (row 6), and journal reachability (row 3). `Observe` returns no error by
        the interface's own design — an observer that could not read says so IN the view.
        **Allocates and claims V-CTR-017 at L1. Done 2026-07-29** — `internal/broker/brake`, direct
        reads on a 5s TTL, where `FreezeView.ObservedAt` is the instant of the **read** so the cache
        degrades into row 1 on `Decide`'s own arithmetic with no liveness tracking in the source.
        `refresh` attempts every read even after an earlier one fails, so the reported row is the
        one whose input actually failed. 20/20 mutations caught through `dev/mutate.sh` — **14/20
        on the first pass**; see "What T7c-3d-i asserts" below for the two real survivors, both of
        which were [[LSN-035]] in miniature.
      - **P9-T7c-3d-ii** — the 04 §4.2 budget and flap accountant, which fills the fifth input,
        `BrakeBudget` (row 7). Split out because it is not a read: it is journal-derived
        accounting over windows and thresholds, the same shape and size as the `cooldown` source
        T7c-3c-iii spent a whole unit on. **Split again, at ORIENT for ii, under the
        `harness-run` §2 sizing rule** — the seam turned out to be wrong, not merely unfilled, and
        fixing a cross-package seam plus writing a `cooldown`-sized package plus adding an API
        defaults helper is three units in a coat.
        - **P9-T7c-3d-ii-a** ✅ **done 2026-07-29** — **the seam.** `broker.Accountant` +
          `BudgetQuery{Agent, Trigger, Class, Targets, Now}`, queried from `pipeline.Config` at
          decision time. T7c-3d-i had put the accountant on `brake.SourceConfig`, reachable only
          through `pipeline.BrakeSource.Observe` — a **per-agent** observation taken **before**
          classification — but 04 §4.2 budgets an agent's `{origin, class}` bucket and flaps per
          target, so that accountant could never answer the question the spec poses. Row 8's
          `ContestedIndex` already had the right shape. `BrakeView.Budget`, `brake.Accountant`,
          `brake.Unaccounted` and `brake.SourceConfig.Accountant` are **deleted**. A nil accountant
          now refuses and escalates; a zero `BrakeBudget` still permits, and those two stopped
          being the same value. **V-CTR-018**, L1.
        - **P9-T7c-3d-ii-b** ✅ **done 2026-07-29** — **the accountant.**
          `internal/broker/budget.Source`: the journal-derived fold, `EffectiveInitiativeBudget()`
          over the 06 §1.1 defaults, and the origin partition. **V-PRO-029**, L1, newly allocated in
          09 §6.6 — the true sibling of V-PRO-028 (same suite, same source, same level, same phase,
          same journal-derived-and-refuses-when-blind argument). Row 7 now counts.

          **Recon 2026-07-29 — what ii-b must settle at PLAN, before any code.** The mechanical
          model is **`policy.Source`, not `cooldown.Source`**: cooldown refreshes lazily from inside
          a ctx-taking method, which `Accountant.Budget(q) BrakeBudget` cannot do. Copy cooldown's
          _derivation and test structure_; copy policy's `Refresh(ctx) error` + `Run(ctx)` ticker +
          ctx-free `Current()` _lifecycle_. Six things the spec does not settle, each to be recorded
          as a decision or escalated:
          - **The window model is contradictory and it changes the refusal.** 04 §4.2 and 06 §1.1
            say "rolling"; but `status.budget` carries `windowStart`/`dayWindowStart` with a
            clock-aligned example, and 06 requires `retryAfterSeconds` "to the next **window
            boundary**", which a sliding window does not have. If no reading preserves both, that is
            PROTOCOL §8.5 and a halt — do not pick a side quietly.
          - **Flap's `(target, intent)` key is unimplementable as written.** `spec.intent` is
            free-text model prose, and `internal/broker/idempotency.go` **deliberately excludes** it
            from the idempotency key for exactly this reason ("a retry that reworded itself would
            compute a different key"). Keyed literally on intent, flap under-fires against an LLM
            that rewords. No canonical intent identity exists anywhere in the tree.
          - `> N` vs `>= N` is undetermined (04 says "more than _N_"; 06 says "repeats", default 3),
            and **oscillation has no threshold or window at all** despite V-PRO-016 asserting it.
          - **The 06 §1.1 defaults exist in no Go file** — only the _ceilings_ do, in
            `internal/webhook/agent_webhook.go`. `EffectiveInitiativeBudget()` introduces the default
            table to Go for the first time; put defaults **and** ceilings in `api/v1alpha1` and have
            the webhook import them, or the two copies drift. Follow `ApprovalRoster.EffectiveTTL`,
            which already documents the right asymmetry: admission **rejects** an over-ceiling leaf,
            the runtime **clamps** one that got in anyway.
          - **A cold accountant must report `Exhausted: true`.** `Budget` has no error channel and a
            zero `BrakeBudget` permits, so "I have not read the journal yet" must be encoded as a
            refusal with a distinguishable `Detail` — otherwise every broker restart silently
            disables row 7, which is the hole ii-a just closed. **That clause is the heart of
            V-PRO-029** and has no V-PRO-028 analogue.
          - **Out of scope but must not be assumed done:** `AgentStatus` has no `budget` field and
            the broker has **no write verb on `agents`** (V-BRK-013, BLOCKING-ALWAYS), so 06's "names
            the empty bucket in `status.budget.exhaustedBuckets`" needs a controller, not the broker.
            Likewise `retryAfterSeconds` is currently the flat `PausedRetryAfterSeconds` (60), and
            `BrakeBudget` has no field an accountant could use to supply the real one.

          The journal _can_ answer the partition — `kube-agents/trigger` × `kube-agents/risk-class`
          are both labels — but there is **no agent-name label** (only tier and a non-injective scope
          leaf, so filter client-side on `Spec.AgentRef`) and **no time index** (filter client-side,
          exactly as `cooldown.derive` already does).

          **How the six were settled at PLAN, 2026-07-29 — no halt.** Each is a decision in the
          ledger; the argument lives beside the code it governs.

          1. **The window is rolling, and the two sentences do not contradict.** A sliding window
             _does_ have a next boundary: the instant its **oldest counted charge ages out**, which
             is exactly when capacity returns. That reading satisfies "rolling" _and_
             `retryAfterSeconds` to "the next window boundary", so this is not PROTOCOL §8.5.
             "Rolling" is normative three times across two documents; the clock-aligned reading
             appears once, in a YAML comment on a status field that has no writer. Direction matters
             too — a tumbling hour lets an agent spend a full allowance at 16:59 and another at
             17:01. Recorded at `budget.Window`; the boundary is computed by `snapshot.retryAt`.
          2. **Flap keys on the target alone**, which is **strictly stricter** than
             `(target, intent)`: every literal breach is also a target breach, so nothing the spec
             would catch is missed. Keying on prose would under-fire against an LLM that rewords,
             which is precisely why `idempotency.go` excludes intent. The residual runs the other
             way and is named rather than implied: three legitimately-different actions on one object
             inside the window now trip a brake the literal spec would not. Tolerable because 04
             §4.2's own remedy is "stop, mark, escalate" — a human looks — and both threshold and
             window are operator-tunable. Recorded at `budget.flapKey`.
          3. **`applied = prior + 1`, breach iff `applied > threshold`** — 04 §4.2's "more than _N_
             times" counts the action being decided. With the default 3, three priors are allowed and
             the fourth is refused.
          4. **Oscillation is out of scope.** V-PRO-016 is **phase 13, L2/L4** — it needs a live
             fleet, not a fold. Nothing in Phase 9 binds it.
          5. **`api/v1alpha1/budget.go` is the one Go definition site** for the whole 06 §1.1 table,
             defaults _and_ ceilings; `internal/webhook/agent_webhook.go` now imports the ceilings
             from it instead of transcribing them. `EffectiveInitiativeBudget` follows
             `ApprovalRoster.EffectiveTTL`'s asymmetry — admission rejects, runtime clamps — with one
             deliberate divergence: an **explicit `0` is honoured**, because a zero allowance is a
             real configuration and a zero TTL is not.
          6. **A cold or stale source returns `Exhausted: true`** with a `Detail` naming the
             blindness, distinct from the "you spent it" refusals. As predicted, this is the heart of
             V-PRO-029.

          **Two consequences worth stating rather than discovering.** Charging follows 06 §1.1
          exactly: `Rejected`, `forbidden` and dry-run charge nothing; `RolledBack` charges because it
          ran; `PendingApproval` and `Expired` charge because `gatedPerHour` counts **submissions**;
          `undo` is exempt from every hourly bucket and is never refused for budget, but flap still
          applies to it. And because `applied` excludes dry runs while the whole of Phase 9 is
          dry-run, **the flap brake cannot fire until T7c-3d-iv wires execution.** That is correct — a
          rehearsal did not touch the object — and `TestFlapCannotFireDuringPhaseNine` will start
          failing on the day it stops being true.
      - **P9-T7c-3d-iii** — the two small journal-derived adapters the pipeline needs and nobody
        wrote: `execute.Journal` (`ConfirmDurable`, three test stubs and no implementation — with
        it nil, `Executor.Journal` is nil and **every non-dry-run execution fails the write-ahead
        check**) and `classify.ActionHistory` (the novel-action question; `policy.SourceConfig`
        takes one and no production value exists). **Split at SELECT on 2026-07-29** under
        `harness-run` §2 sizing: the two adapters share only the phrase "journal-derived". One is a
        confirmation on the write path with its own envtest harness; the other is a lifecycle
        question about refresh and staleness on the classify path. Sized together they were one
        unit with two PLANs.
        - **P9-T7c-3d-iii-a — the write-ahead confirmer** ✅ (2026-07-29)
          New package `internal/broker/writeahead`: `Confirmer` is the production `execute.Journal`.
          It lives in its own package rather than in `internal/journal` because
          `internal/broker/execute` already imports `internal/journal`, so a journal-side adapter
          could not hold the `var _ execute.Journal` assertion without a cycle — the
          `internal/broker/bodystore` precedent, followed deliberately.
          **Check: V-BRK-023** (new, L1). The gap to **V-BRK-022** is not an error: that ID is
          reserved above by T7c-4 and IDs are never renumbered (09 §4).
          **What the check is actually about.** `ConfirmDurable` receives only `(ctx, actionID)`, so
          it cannot compare the stored record against caller intent. What it can check is the thing
          an in-process buffer cannot fake: **server-assigned `uid` and `resourceVersion`**. That is
          [[LSN-034]] applied to durability — a store that reported its own success would be
          comparing a value against itself. Four more arms follow from the same argument: a record
          on its way out (`deletionTimestamp`) is not durable; a record whose `spec.actionId`
          disagrees with the name it was derived from is somebody else's journal entry; an
          unreadable journal is refused rather than scored durable; and a misconfigured confirmer
          refuses **before reading**, the same direction as the nil accountant in ii-a.
          **The phase arm, and the measurement under it.** A record whose status label names a phase
          other than `Executing` is refused. It reads the **metadata label**, not `status.phase`,
          and the envtest half proves why: `ActionRecord` carries a status subresource, so
          `client.Create` drops `status` entirely, while `journal.Labels` reads the caller's phase at
          Create time and writes it into metadata, which survives. Reading `status.phase` here would
          have looked more correct and would have been vacuous — it is empty for every record this
          function will ever see.
          **The future the phase arm is for, recorded rather than hand-waved.**
          `journal.Store.Create` folds `AlreadyExists` into a nil return — correctly, since the
          record name is derived from the action id, which is what makes the broker's retry safe
          without a lock. But it means a nil from `Create` does not prove that _this_ call wrote what
          is now on the server. Today the two writers cannot collide: step 7 parks a gated action as
          `PendingApproval` and returns, step 8 is only reached by an action that was never parked,
          and no `/approve` handler exists. The moment an approval path re-enters the pipeline for an
          already-parked action, step 8's `Create` returns nil against the parked record, the
          pre-state it just set never reaches the server, and the executor would mutate live objects
          against a journal entry carrying no snapshot and therefore no undo plan. That is the
          write-ahead rule failing in the only direction that matters, and it now fails closed.
          `TestAParkedRecordDoesNotConfirmEvenThoughCreateSucceeded` reproduces the whole sequence
          against a real API server.
          **Evidence.** 17 test functions / 30 cases with subtests (10 hermetic, 7 envtest), 100.0% statement coverage on
          `writeahead.go`, and a **19/19 mutation sweep** with zero escaped and zero broken. The
          sweep names the test that must fail for each mutation rather than accepting "the package
          went red", and runs the whole package instead of a `-run` pattern — which sidesteps
          [[LSN-048]] by construction, since a pattern that matched nothing cannot score CAUGHT if
          there is no pattern. Two of the nineteen mutate `internal/journal/store.go` rather than the
          confirmer: they are what keeps the envtest half non-vacuous, because if journal stopped
          carrying the phase into metadata or stopped folding `AlreadyExists`, the phase arm would be
          reasoning about a world that no longer exists and nothing in `writeahead.go` would have
          changed.
          **One finding filed, not a halt:** `execute/apply.go` cites **(V-REV-002)** for the
          write-ahead rule, but 09's V-REV-002 is "undo `<id>` restores prior state, verified by diff
          against the snapshot". The write-ahead check is **V-BRK-006** (05 §1.2, L2/L4, phase 9).
          Same shape as the V-BRK-020/V-BRK-021 citation defects already recorded — a comment
          pointing at a check that does not assert the property it claims. To be swept with those.
        - **P9-T7c-3d-iii-b — `classify.ActionHistory`**, the journal-derived novel-action source.
          `policy.SourceConfig.History` takes one and no production value exists. Shape follows
          `policy.Source`: a refresh lifecycle, and blind ⇒ `false` ⇒ escalate, which is the safe
          direction. **The dangerous direction is a nil history**, and it is currently accepted:
          `classify.Classifier` guards its novel-action escalation with `c.knownActions != nil &&`,
          so a nil silently switches the escalation off, and `policy.NewSource` does not refuse one.
          That is the same hole ii-a closed for the accountant, one package over.
      - **P9-T7c-3d-iv** — the wiring itself: a discovery client (constructed nowhere today, and
        `refindex.Source` requires it non-nil), `pipeline.New` replacing
        `broker.UnavailablePipeline{}`, `policy.Source` with a synchronous startup `Refresh` and a
        backgrounded `Run`, `cooldown.NewSource`, and `broker.NewContestedIndex`. **Closes
        LSN-007**, which needs a new L0 source assertion to close honestly: no 09 §6 check asserts
        "the pipeline is constructed in `main.go`", and `install-path-wired.py` never reads Go.
  - **P9-T7c-4** — **the classify→execute integrity seam for `apply`, `scale` and merge-patch.**
    See LSN-040. Today only `create`, `delete` and JSON-patch `patch` traverse the pipeline; the
    other three fail closed at step 9. **Checks: V-BRK-022** (new, L1) — _every verb in the
    envelope's closed verb enum executes end to end through the assembled pipeline, with the verb
    set **discovered from the enum**_, which is LSN-040's own mechanization clause and the reason a
    hand-written table would have printed green throughout; plus **V-BRK-020** (the diff/integrity
    property this seam is the missing half of). Recon 2026-07-29 found five things the entry did
    not say:
    - **It is two fixes, not one.** `apply` refuses at `execute/integrity.go`'s `checkWholeObject`
      `default:` arm (`WholeObject=true`, which only `create`/`delete` may be); `scale` and
      merge-patch refuse at the earlier "shown no changed fields" arm (`WholeObject=false`,
      `TouchedPaths=nil`). `TestApplyFailsClosedAtTheIntegrityCheck` pins only the first; **nothing
      pins `scale` or merge-patch.**
    - **The SSOT is already named and already unwired.** `execute/diff.go`'s own doc says `Diff` is
      "called twice per action and the two calls are the whole of V-BRK-020" — call #1, before
      classification, **does not exist**. The fix is a reorder inside `pipeline.stepResolve`:
      `CaptureAll` already runs four lines later, so `snap.Live` is in hand. No new API read, no new
      interface, and the import direction already permits it.
    - **A quieter live hole than the one LSN-040 describes.** `scale` appears nowhere in
      `classify/resolve.go`, so it classifies with an empty `TouchedPaths` — meaning a
      `ChangePolicy` with `when.fieldPaths: [spec.replicas]` **can never fire on a scale**, and the
      policy author gets no error. 06 §4.2 says matching is on the touched set "**across the
      diff**", not across the submitted patch, which is the spec-level statement of the defect.
    - **`classify.KnownVerbs()` has zero callers.** Its doc says it exists "for the lint that joins
      it to the envelope's enum" and asserts "the corpus lint asserts the two agree". There is no
      such lint. Exporting `broker.validOps` for V-BRK-022 finally gives it one — two lessons closed
      by one export ([[LSN-041]] shape: prose describing a control that has never existed is worse
      than no prose, because it retires the question).
    - **Two false comments to delete while in there**, both asserting controls that do not exist:
      `pipeline.go`'s "the classifier derives those from Payload instead — so returning nil for them
      is the correct answer, not a gap" (it does not; `ScanPayload` returns `[]SecretHit`, never
      paths), and 05 §1.1's "the list is a code constant" about the dry-run carve-outs (there is no
      such constant; `SupportsDryRun` defaults to `true` and its optional hook is keyed on **ref,
      not verb**, so a `scale` cannot be recognised as a carve-out at all).
    - **One finding to file, not a halt:** V-BRK-020's row cites 03 §4.4 as its source, and 03 §4.4
      ("Reversibility as a security property") contains neither "strategic" nor "expand" nor
      "integrity". Same shape as the P9-T7c-2b halt, where 09 cites 03 §4.1 for V-BRK-021's "one
      mutating route" and 03 §4.1 does not contain it. The property is well-defined in 09 itself, so
      this is a citation defect rather than a spec contradiction.

**Why T7c split into four.** T7c-1 was scoped as "assemble the pipeline and claim the two L1
checks", and the assembly turned out to be the small part. Three things came out of doing it.

The first is that **the two deferrals are not part of the assembly.** The `ChangePolicy` informer
and the replay route were parked on T7c because T7c was the next broker unit, not because they
share a seam with the pipeline — the informer feeds the classifier a policy set and the replay
route is a second HTTP handler. Neither is touched by wiring steps 3–11 together, and carrying them
would have made the unit oversized in exactly the way `harness-run` §2 warns about.

The second is that **the assembly and the wiring are different units with different verification.**
`pipeline.Config` has twelve dependencies that are interfaces precisely so the pipeline can be
driven at L1 with fakes; writing their real client-backed implementations is a dozen adapters whose
own property is "does this talk to a real API server correctly", which is L2. Mixing them would
have meant a unit where the L1 checks pass and the L2 half cannot run, i.e. a unit that cannot
checkpoint. T7c-1 therefore ends with the pipeline reachable from a test and not from `main`, which
is an honest partial state and is recorded as such in the `cmd/broker/main.go` comment.

The third is LSN-040, below — a gap the assembly found, which is a fix rather than part of the
assembly.

**What T7c-1 actually asserts.** `broker.StepTrace` is not a log. Its `Run` refuses to record a step
that is not the immediate successor of the last one recorded, and seals the trace on the first
refusal or fault, so "step 7 ran before step 4" and "a step ran after the pipeline stopped" are
errors the pipeline returns at the moment they are attempted rather than conditions a check hunts
for afterwards. That inversion is what makes V-BRK-014 an L1 property: fault-inject at step k and
the trace ends at k because there is no path to k+1 that does not go through a `Run` call the
failure never reaches.

The fault table covers steps 3–10 with one injected dependency failure each, step 11 has its own
test, and steps 1–2 belong to the handler and are covered in the `broker` package.
`TestEveryPipelineStepHasAFaultCase` closes the loop by iterating `broker.FirstStep..LastStep` and requiring
every step to appear — so a twelfth step added to the pipeline fails that test the day it exists
instead of quietly falling outside a hardcoded range (LSN-036). Each fault case also asserts the
world stopped, not just the trace: zero applier calls for any fault before step 9, and no record in
a phase that claims the action completed.

**One bug fixed in the pipeline itself.** `Submit` reported `Phase: string(s.verify.Phase)` to the
caller while step 11 journaled `terminal(s)`. Those agree on every path except a dry run, where the
verifier never runs: the record said `DryRun` and the HTTP response said nothing at all. Both now
derive from `terminal(s)`.

**Why T7c-2 split.** T7c-2 was "the two deferrals", and the two turned out to be unrelated in the
way that matters: one was unblocked and one is a halt.

**T7c-2b is halted on a spec contradiction.** 05 §1.3's route table names three broker routes —
`POST /v1alpha1/actions`, `.../{actionId}/approve` and `.../{actionId}/replay` — while V-BRK-021,
which is **BLOCKING-ALWAYS**, asserts "one listening port, **one mutating route**". Adding the
replay route makes the second. PROTOCOL §10.2 forbids resolving that autonomously, and the
alternative resolution is arguably the worse one: replay-as-submission (C-UC POSTs to
`/v1alpha1/actions` with `spec.trigger.undoOf`) keeps the route count at one but forces the
broker's `Authenticator.ExpectedCaller` to accept a **second identity submitting caller-supplied
operations**, which is a wider widening than a `/replay` route that accepts an action ID and no
operations at all. Sharpening the question: **09 §6 cites 03 §4.1 as V-BRK-021's source, and 03
§4.1 does not contain the phrase.** What it requires is step non-skippability, not a route count.
The narrowest question a human can answer is in the ledger's Blockers table.

**What T7c-2a asserts.** `internal/broker/policy/` is what makes `ChangePolicy` load-bearing.
Before it, `classify.FromChangePolicy` had no caller: an operator could apply a policy, see it in
`kubectl get`, see `status.agentsMatched` count the agents, and have every action classify as
though it were not there. Three things came out of building it.

The first is that **the binding predicate did not exist.** `ChangePolicySpec.AgentSelector` had zero
consumers anywhere in the Go tree. `policy.Binds` is it: `Tiers` is exact membership (a tier is a
kind of authority, not an amount of one, so a `cluster-admin` policy does not bind the
developer-team agents beneath it), `Scopes` is `scope.Contains` — "at or beneath", as the field
documents — and the two clauses are **ANDed**. An ill-formed selector scope (a hole in the middle,
which `scope.Contains` would read as a wildcard and match cluster `c` in every project) is skipped
by the predicate _and_ refuses the whole snapshot in the loader; both halves are needed, because
the first alone would be silent.

The second is that **every decision in the package follows from one asymmetry.** The classifier
takes the maximum over its sources (06 §4.2 step 3), so a policy can only ever raise a class —
which means every way of failing to see a policy is a **loosening**, and there is no symmetric
failure to trade against. So a bad policy fails the whole snapshot naming the policy rather than
being skipped; an unresolvable policy set refuses the action rather than falling back to the code
floor; and `Build` runs the same `classify.ValidateChangeRule` the admission webhook runs, so a
rule the broker would refuse and admission accepted cannot exist.

The third is that **it polls rather than watches, and the deferral's own name was the wrong
design.** The deferral said "the `ChangePolicy` informer". An informer needs a freshness signal its
own cache cannot supply — this repo already wrote down why, in `broker.MaxFreezeStaleness`: "a
watch that silently stopped delivering is not an error at all — the informer's List succeeds, the
cache answers instantly, and every answer is from before the incident started." Every way of
building that signal ends in a periodic read against the API server, at which point the cache is
buying latency and not correctness. `ChangePolicy` is cluster-scoped, human-authored and will
number in the single digits, so the source polls every 10s against a 30s staleness limit — three
polls per window, so one lost poll does not refuse and two consecutive ones do — and freshness is
true by construction. The cost is that a tightening binds within 10s instead of within a round
trip, which for a policy a human just typed is not a cost.

**The envtest run found a design flaw the unit tests could not.** The first draft treated all poll
failures alike: retain the last good snapshot, let it age out at 30s. Against a real API server
that is wrong, because two failures were being conflated. A **read** failure (the List did not
answer) is transient and retaining is right. A **load** failure (the set was read and will not
convert) is not transient at all — it will fail every poll until a human edits the object — so
aging it out means 30 seconds of classifying against a set the broker already knows is wrong, and
the operator who applied the bad policy learns about it from a delayed timeout instead of at once.
The two are now handled oppositely and the distinction is asserted from one place so the pair
cannot drift.

**LSN-007 still applies.** Like T7c-1, this lands reachable from a test and not from
`cmd/broker/main.go`, which still installs `broker.UnavailablePipeline{}`. T7c-3 is what closes it.

**Why T7c-3 split into four.** Twelve adapters is not one unit, and the reason is not only size.
Each adapter's own property is "does this talk to a real API server correctly", which is an L2
claim, and L2 claims are bought one cluster round trip at a time. A single unit holding all twelve
would have had one checkpoint at the end and no honest partial state before it — exactly the shape
`harness-run` §2 tells us to split rather than carry. The four sub-units are cut along the seams
that already exist in the pipeline: 3a is everything **classification** reads, 3b is what **undo**
needs to exist before an action runs, 3c is everything **verification** does after, and 3d is the
wiring that makes the binary reach any of it. 3d must be last because it is the only one whose
precondition is all the others; the cost is that the broker keeps 503ing until it lands, which is
LSN-007 remaining true for three more units and is recorded rather than worked around.

**Why T7c-3c split into three.** Found at ORIENT, before any code: 3c is five interfaces, and only
the first of them is an adapter in the sense 3a and 3b were. `verify.Prober` is eight methods whose
cluster mechanics have almost nothing in common — an EndpointSlice enumeration, a restart-count
aggregation that has to resolve a workload's selector, a provider read that is really a Config
Connector CR plus a node-label count, a `SubjectAccessReview`, a dry-run admission observation —
and it is the whole of 04 §5.1's evidence surface. The other four are the ladder's **effects**:
`Rollbacker`, `Pager` and `Pauser` write to the world (3c-ii), and `CooldownRegistry` needs a
durable store rather than the in-process map, which `verify/cooldown.go` already says in its own
doc comment (3c-iii). Cutting between "what verification reads" and "what recovery does" also puts
the destructive L2 surface in exactly one sub-unit: 3c-i only reads and dry-runs.

**What T7c-3c-i asserts.** V-PRO-027, newly allocated. See the check text in 09 §6.6; the argument
for allocating it rather than claiming V-PRO-013 is in this unit's ledger row.

**Why T7c-3c-ii split into two.** Not a sizing call. `Pager` and `Pauser` cannot be written as
adapters at all, and finding that out took reading one line of 06 §2.2.1: the **broker's operations
grant is read-only on `agents` and carries no verb on `events`.** V-BRK-013 asserts that grant
**exactly**, and V-BRK-013 is BLOCKING-ALWAYS. So the broker process cannot pause an agent — that is
a write to an `Agent` — and cannot page — that is an Event. A `Pauser` implemented as a client call
from the broker would need the grant widened, which is precisely the change PROTOCOL §10.2 forbids
doing to get an implementation to work.

The invariant-preserving shape is the one 05 §1.7 already names: **"exactly one code path that stops
an agent."** The broker records the intent in the journal — which it can write — and a
**controller-side C-BR reconciler** fans it out into the pause and the page, from the operator's
identity, through the single stop path that already exists. That reconciler does not exist, and
writing it is a controller unit, not an adapter unit. So ii-b is `Pager` + `Pauser` + C-BR together,
and its precondition is a design decision recorded in the ledger rather than a missing file.

Splitting here also keeps the two halves honest about their verification. ii-a's property —
V-REV-011 — is provable today at L1 and L2 against a real cluster, with no deployed surface needed.
ii-b's property is that a rung-5 escalation reaches an agent that then stops, which is an
end-to-end claim over two processes and belongs with the wiring, not before it.

**What T7c-3c-ii-a asserts.** V-REV-011, newly allocated in 09 §6.3. The clause that motivated
allocating a new check rather than extending V-REV-004 is "**replays the pre-state**": at L1 a
successful replay means a field changed, and the pre-state of a scaled-down Deployment is running
pods. That distinction is only assertable where controllers run, so the check is L1+L2 from
allocation and its L2 half shipped in the same unit.

**Why T7c-3c-ii-b split into two.** The two halves have different provable properties, different
verification levels, and — the part that actually forced it — different **preconditions**.

The request half is "a rung-5 escalation is durably recorded where `C-BR` can see it". Nothing under
test runs from a deployed image, so P1 is waived by construction exactly as it was for ii-a, and the
property is provable at L1 against a real API server in envtest.

The fan-out half is "an escalation reaches the agent and the agent stops". That is a claim about the
**operator**, which means the evidence needs the operator image rebuilt, pushed and rolled **by
digest** — P1 in full, for the first time in this task chain, and the first time in Phase 9 that a
unit's verification depends on a deploy rather than on a client connection. Bundling the two would
mean either the request half waits on an image roll it does not need, or the fan-out half ships with
P1 waived, which is [[LSN-001]] with extra steps.

The seam between them is a field, not a function call, and that is the point: the broker cannot call
the controller, because 06 §2.2.1 gives it no verb that would let it. What it can do is write
`actionrecords/status`, which it already must.

**What T7c-3c-ii-b-1 asserts.** V-REV-006 at L1 — "a failed rollback pages **and** auto-pauses the
agent", 04 §5.1, `¬`, BLOCKING-ALWAYS. Its 09 §6.3 level list is widened from `L2` to `L1, L2`:
nothing is removed, relaxed or narrowed, so §10.2 is satisfied, and the L2 half stays owed by
ii-b-2. The L1 half is not the whole check and the ledger row says so — what it proves is that the
**request** is durable and complete: both halves recorded, the reason carried, the record named, and
an escalation that cannot be written surfaced as an error rather than swallowed. The negative
control is the direction that matters: a rung the driver never reached must leave `status.escalation`
absent, because a record that claims an escalation nobody requested is how a `C-BR` reconciler pauses
a healthy agent.

**Why T7c-3c-ii-b-2 split again, into 2-a and 2-b.** The same argument one level down, and the
sizing rule made the call: the reconciler is Go plus two CEL rows plus two L1 suites, and the deploy
is a new ServiceAccount, a new grant, a new Deployment, a manager selector, an image build and a roll
by digest. Bundling them means the code half cannot checkpoint until a cluster is reachable, and a
unit that cannot checkpoint is one killed session away from being redone.

The split is only honest because the code half claims something real on its own. It does: the L1 half
of V-REV-006 was opened by ii-b-1 with the **request** and left explicitly incomplete, and 2-a closes
it with the **fan-out** — a recorded escalation becomes a patched `spec.operations.paused`, a page,
and a receipt, with the `¬` proving the converse (a record that owes nothing must leave the agent
running and emit nothing). 2-b then claims the L2 half, which is the part that needs a cluster to
mean anything: at L1 the pause is a patch against a fake API server, and "the agent actually stopped"
is not something L1 can observe.

**What T7c-3c-ii-b-2-a asserts, beyond the reconciler.** Two rows in `vap-agent-scope-journal` that
turn the broker/C-BR seam from a convention into an admission decision — C-BR may write only the
fulfilment half, and may neither create the escalation nor edit what was requested. Without the
second row the controller holding the pause verb could author the justification for using it, which
is the concentration of authority the split exists to prevent. Both rows and the broker's mirror
denial are exercised against a real API server, in both directions, and mutation-tested.

**What T7c-3a asserts.** `livestate.Source` is the adapter behind every rung of 06 §4.2's
ladder: object labels and annotations, namespace labels, the blast-radius denominator, the secret
digest set, and lower-tier ownership. Four things came out of building it.

The first is that **its five methods do not share a failure direction, and treating them alike would
have been a security bug in both directions.** `CountWorkloadObjects` is the denominator of
`AbortScopeFraction`, and `ComputeBlastRadius` turns an error into a **nil** fraction — which
disarms the abort rule entirely. So a kind the caller cannot list is **skipped, not fatal**: a
smaller denominator makes every fraction larger, which makes the abort more likely. The reflex "I
could not see everything, therefore I must refuse to answer" is the loosening direction here.
`SecretDigests` is the opposite: an empty digest set is the exfiltration gate answering "no secrets
here" to every payload, so a failed List is an error. `LowerTierOwner` is likewise fatal — "the
Agent list did not answer" must not read as "nobody owns this". Each method's doc comment carries
its own argument, because the next person to touch one of them will otherwise make them consistent.

The second is that **the fake client cannot honestly test this adapter.** controller-runtime
v0.19.0's fake tracker does not model `PartialObjectMetadata` at all — the type these reads are
built on, because a classifier has no business fetching object bodies. A fake agrees with whatever
shape the caller assumed, so a green there would be a green about code that never ran (LSN-001's
shape, one layer in). Hence a three-level split, each file stating in its header what it does **not**
attempt: hand-rolled stubs for the decision logic (which kinds are countable, which failures are
fatal, when a cache expires), envtest for "a real API server answers this way", and an L2 probe for
"a real GKE cluster with a real discovery surface and real RBAC answers this way".

The third is that **the adapter does not belong in package `classify`, and the L0 chain is what
said so.** It was written as `classify.ClientLiveState` and the first full L0 run rejected it:
V-GAT-017 holds a **closed import allowlist** over `internal/broker/classify` which deliberately
contains no Kubernetes client of any kind, because the classifier is handed already-resolved facts
precisely so that it cannot go and look anything up. Eight imports were refused at once. The
smallest diff to green was to widen the allowlist, and that is the move PROTOCOL §10.1 exists to
forbid — the check's own doc comment argues that the failure it prevents is not somebody importing
an SDK on purpose but a plausible refactor widening the list one line at a time. So the adapter
moved to `internal/broker/livestate` and became `livestate.Source`, which is the same
interface-here / implementation-there seam `internal/broker/policy` already uses for `ChangePolicy`.
Two properties depend on the split: the classifier stays hermetic, so the 165-envelope corpus can
permute every input and get a byte-identical answer, and the allowlist stays a conversation rather
than a diff. The package comment records the argument at the place someone would undo it.

The fourth is **a new pattern: `k8s-operator/test/l2/`, Go probes behind a `//go:build l2` tag.**
Without the tag the file does not exist to the toolchain, so `go test ./...`, `go vet ./...` and the
L0 chain stay hermetic and no CI runner can reach a cluster by accident. The destructive-test guard
is duplicated inside the probe rather than left to its wrapper, because a probe that creates and
deletes namespaces and can only be aimed safely by a shell script is one `go test` away from being
aimed at the live install. Its wrapper, `dev/verify/classify-live-state-l2.sh`, declares P10 and P6
and argues **in writing that P1 does not apply**: nothing under test runs from a deployed image, so
the working tree is the build under test by construction. That argument is now also the qualifier on
`dev/L2-CHAIN.txt`'s blanket P1 statement, and it names its own expiry — when 3d wires the broker,
the end-to-end successor in `broker-execute-l2.sh` needs P1 in full.

**What T7c-3b asserts.** Two adapters, one seam each: `refindex.Source` behind
`undo.ReferenceIndex`, and `bodystore.Journal` behind `execute.BodyStore`. Three things came out of
building them.

The first, and the reason the unit needed a check of its own, is that **the hard question is not
"can it find references" but "what counts as one"** — and the answer is the loosening direction, so
it is argued in the package doc where someone would undo it. 06 §4.3.1 says "every ownerReference,
PVC binding, and external reference pointing at **the old one**". What a recreate destroys is the
**UID**, so a reference bound to the UID is left dangling and a reference bound to the **name**
resolves to the new object — it is _repaired_ by the recreate, not broken by it. That collapses the
domain to UID-valued references, which in practice is `metadata.ownerReferences`. The tempting
generalization — report every reference-shaped field the scan can see — reads as extra safety and is
the opposite: on a real cluster nearly every object is named by something, so it would downgrade
nearly every `delete` to `none`, make the whole `recreate` strategy dead code, and be reported as a
tightening while it happened. A gate that always fires is indistinguishable from no gate. The
residual is written down because it is the direction that needs an argument: a UID-valued field
outside `ownerReferences` (only `PV.spec.claimRef.uid` in core Kubernetes, and unreachable anyway
because PV and PVC are on `undo.nonRecreatableKinds`, so the strategy short-circuits before the
index is consulted), and references held outside the cluster, which no in-cluster scan can see.
V-REV-010's mandatory negative control **is** that boundary: a Pod mounting the target ConfigMap by
name must change nothing.

The second is that **`refindex.Source` and `livestate.Source` fail in opposite directions, one week
apart, and neither is a copy of the other's default.** A kind `livestate.Source` cannot list is
skipped; a kind `refindex.Source` cannot list fails the entire scan, with `IsForbidden` given its
own message naming the grant that is missing. The direction is not a house style — it follows from
what a partial answer means to the caller. A missing kind shrinks the blast-radius **denominator**,
which makes every fraction larger and the abort _more_ likely, so skipping is the tightening move
there. A missing kind in a reference scan means a referrer might exist and be unseen, and the caller
reads an empty slice as "nothing points at it, the recreate is safe" — which
`undo.ReferenceIndex`'s own doc comment already forbids: "'nothing points at it' and 'I could not
look' are the two answers this package must never conflate." Both directions are mutation-tested.

The third is that **only a real cluster can demonstrate the harm, and until this unit nothing had.**
envtest runs no kube-controller-manager, so an `ownerReference` there is an annotation with no
consequences and 06 §4.3.1's premise — "the garbage collector sees owner references pointing at a
UID that no longer exists and deletes the children" — was a sentence in a spec that nothing
executed. `TestREV010TheGarbageCollectorDoesWhatTheDowngradePrevents` performs the sequence a
`recreate` plan would have performed: delete the owner, watch a real GC destroy the dependent,
recreate the owner from its snapshot, observe a new UID and the dependent still gone. That is the
state an undo reporting `done` would have left behind, and it is now on the record rather than
described. The probe fails loudly rather than skipping if no collection is observed, because "the
dependent survived" would otherwise read as evidence the downgrade is unnecessary.

`bodystore.Journal` is smaller and is [[LSN-034]] applied **before** the fact rather than after a
green run: `execute.capture` digests the body itself and compares against what the store returns,
which is only worth doing if the two numbers have independent provenance, so the adapter returns the
**sink's** digest unaltered. It calls `journal.SnapshotKey` — extracted this unit from `snapshot.go`,
which had the format string inline — rather than re-deriving the layout at a second site.

**Two findings, neither failure-driven, both carried to Deferrals rather than fixed here.** A
full-surface scan of one namespace on a 57-kind cluster takes **9.1 s**; it is sequential, O(kinds),
and sits in the request path at pipeline step 4, so a CRD-heavy cluster is worse and nothing
measures it. And **no production `journal.BlobSink` exists anywhere in the tree** — the interface has
been there since T1, `cmd/broker` passes `nil`, and `bodystore.Journal` is now complete with nothing
to talk to, so any pre-state over 1 MiB refuses its action outright. That is 03 §6's fail-closed
direction and not a hole, but it is an availability cost invisible until someone patches a large
ConfigMap, and a real sink needs a bucket, a GSA through Workload Identity and a lifecycle policy
matching 06 §4.3's TTLs — a provisioning unit, not an adapter unit.

**What T7c-3c-iii asserts.** V-PRO-028, newly allocated in 09 §6.6. Four things came out of building
it, and the first two changed an interface.

The first is that **the durable store had nowhere to live, and two of the three candidates were
unavailable rather than unattractive.** `verify.MemoryCooldown`'s doc comment pointed at
`Agent.status.operations`, which the broker cannot write: 06 §2.2.1 grants it `get, list, watch` on
`agents` and no write verb, and V-BRK-013 asserts that grant _exactly_ and is BLOCKING-ALWAYS, so
widening it is not a move an implementation gets to make. A new CRD would be a 06 §1 amendment,
which PROTOCOL §10.5 keeps out of an implementation unit. The comment was wrong on the only point
that mattered and now says so. What is left is the journal — and the journal is not a fallback: the
cooldown is **already** a function of it, because "after a failed or rolled-back remediation of a
target" is a query over `status.phase` and `spec.targets`. Storing a counter beside the records
would be a second copy of a fact they already hold, and the two would eventually disagree. This is
06 §4.4's contested-index shape and its argument — "the index is authoritative because a deleted
object cannot hold an annotation" — one control over.

The second is **the window between the rollback and the status write**, which is what put an action
ID in `verify.CooldownRegistry.Enter`. `enterCooldown` runs inside `rollBack`, before its caller
writes `status.phase`, so a purely derived registry reports "no cooldown" for exactly the interval
in which the next action arrives — whatever is driving the flap is still driving it. So `Source` is
a composition: journal plus an in-process overlay of the failures it has been told about and cannot
yet see. The union is **by action ID**, and the ID is why. Handed only a target key the store would
have to guess whether an event it sees is new, and both guesses are wrong — count it twice and one
rollback buys a doubled quiet period, count it never and the cooldown does not exist until the write
lands. A no-interface-change alternative was worked through and rejected: `max(journal, overlay)`
computed separately undercounts `consecutive` during the catch-up window (a journal holding two
prior failures plus a fresh overlay event yields 5 minutes where the correct answer is 20), and the
error is in the **loosening** direction at the moment the cooldown matters most.

The third is that **agreement with the reference implementation had to be a property, not a
convention.** A durable store that reconstructs a _different_ quiet period from the same history is
worse than no durable store, because it looks authoritative and answers differently. So the backoff
moved into `verify.CooldownSeries`, one fold with two consumers arriving from opposite directions:
`MemoryCooldown` folds events live, one per rollback; `cooldown.Source` folds a sorted slice
recovered all at once after a restart. `TestSourceAgreesWithMemoryCooldown` runs one history through
both and compares, and it guards itself — a history that left no cooldown active would make the
comparison vacuous, so the test fails on that too. The fold's two rules stopped being edge cases the
moment it had a second consumer: the sort in `seriesLocked` exists because a Go map iterates in a new
order every time and an unsorted fold would apply the decay against the wrong previous event,
answering differently on two consecutive reads of an unchanged journal.

The fourth is **the read**, which deviates from 05 §1 step 5's literal word. Step 5 says
"informer-cached"; this is a TTL-bounded snapshot over a List, for the reason `broker.MaxFreezeStaleness`
already spells out in this repo's own words — "a watch that silently stopped delivering is not an
error at all — the informer's List succeeds, the cache answers instantly, and every answer is from
before the incident started". `livestate` and `policy.Source` made the same call, and
`cmd/broker/main.go` builds a **direct** client on the same argument. Recorded as a ledger decision
rather than left as an unremarked divergence. Past `MaxJournalStaleness` the registry **refuses**
rather than reporting the target quiet, matching `broker.contestedRefusal`; inside the bound a
single dropped read ages the snapshot rather than discarding it, matching `policy.Source`. **The
residual, named because it loosens:** a rollback whose `status.phase` write never lands — the broker
is killed between the two — is a failure event no later process can recover, because nothing durable
records it. That is one action's tail against a whole process's worth of cooldowns, and closing it
would need the rollback and the phase write in one transaction, which the API server does not offer.

**What T7c-3d-i asserts.** V-CTR-017, newly allocated in 09 §6.9. Three things came out of building
it, and two of them came out of the mutation sweep rather than out of the design.

The first is that **`Observe` returning no error is not laxity, it is where the fail-closed table
lives.** `pipeline.BrakeSource` gives the observer no way to report failure out-of-band, and that is
correct: 06 §4.4 does not have a row for "the source errored", it has rows for _which input_ is
missing. So an unreadable Agent must arrive at `Decide` as a **nil Agent in the view**, not as a
returned error the caller has to remember to map back onto row 2. The consequence runs through the
whole file — `refresh` attempts all four reads even after the first one fails and `errors.Join`s the
results, because a source that short-circuits reports the row of whichever read it happened to try
first, which is a **misattributed refusal**: correct verdict, wrong reason, and the reason is what a
human reads at 3am. The same argument makes `readRoster` return three states rather than two — no
ref configured, a ref that resolves to nothing, and "I could not look" — where only the third
retains the previous answer.

The second is that **the cache degrades into row 1 by itself, and that is the reason the TTL is
bounded by a constant rather than chosen.** `FreezeView.ObservedAt` is stamped with the instant of
the **read**, never the instant of the serve, so a source whose refresh has been failing for 31
seconds hands `Decide` a view that `Decide` refuses on its own `MaxFreezeStaleness` arithmetic —
there is no liveness tracking anywhere in the source, and nothing to keep in step. `NewSource`
therefore refuses a `CacheTTL ≥ MaxFreezeStaleness` outright: a view served from cache could
otherwise already be too old for row 1 at the moment it is handed over, which would make the cache
itself the thing that freezes the fleet.

The third came out of the sweep, and it is the useful one. **The first pass was 14/20, and two of
the six survivors were real gaps rather than redundancy.** Survivor one: the only aging test in the
file failed _every_ read at once, so the freeze list went stale along with everything else and
`Decide` fired **row 1** first — the test was named for the Agent and was measuring the freeze
ceiling, which is [[LSN-035]] verbatim ("a negative control only proves the _suite_ fails; it never
proves _which rule_ made it fail"). Rewritten into three subtests that each fail exactly one read
and assert the other inputs are still present in the view, so the refusal is attributable to the
input under test. Survivor two was worse and is a security property: `readRoster`'s answered /
unanswered bool is **unobservable** until a roster that _did_ resolve goes away, because both nil
branches look identical on a source that never had one. Mutating it to `false` means a deleted
`ApprovalRoster` keeps approving gated actions from the retained copy until the staleness ceiling
catches up — **thirty seconds of approving against a roster that no longer exists.** Now covered by
`TestARosterThatDisappearsIsGoneAtOnce`, which advances the clock by only one cache TTL so aging
cannot be the cause of what it observes. Both tests were strengthened because the **sweep** found
them vacuous, not because an implementation was failing, so this is not the `harness-run` §4
coupling.

**Why V-CTR-017 rather than V-CTR-007, and why a new ID at all.** V-CTR-007 is the check whose _text_
names this property — "brake objects behave per contract, including fail-closed on unreadable
`FleetFreeze`" — and it is **L2**, because its property is a real API server refusing a real read,
which no fake client can produce. It is routed to P9-T9 with V-BRK-006 and it stays there;
`verification/results.csv` row 73 already says so in its own notes. V-CTR-015 is L1 and covers
`broker.Decide`, but it feeds the decision function inputs built by hand, so it is structurally blind
to whether the thing that builds them in production tells the truth. Neither covers this, so the
choice was a new ID or nothing — and allocating one for genuinely new coverage is a tightening,
which PROTOCOL §10 permits. Precedents: V-CTR-014 (P8), V-CTR-015 (T6b), V-CTR-016 (T6c), each on
T6b's written argument that leaving the broker's most safety-critical functions uncovered at L1
until P9-T9 means shipping them with their only check a shell script that has never run.

**The residual, named because it loosens.** Row 7 — the 04 §4.2 initiative budget and flap counters —
**cannot fire in production** after this unit. `broker.BrakeBudget`'s zero value permits by
deliberate design, and the only thing filling it is `brake.Unaccounted{}`. That is disclosed as a
required constructor field and a named type rather than a nil default precisely so it is greppable;
P9-T7c-3d-ii replaces it.

> **Superseded 2026-07-29 by P9-T7c-3d-ii-a.** The residual above was real, and the disclosure was
> the wrong instrument for it: an exported permissive accountant is a **supported way to switch a
> fail-closed rule off**, greppable or not. `brake.Unaccounted` is deleted, along with the seam it
> sat on. Row 7's blindness case now lives one level out — a nil `broker.Accountant` refuses and
> escalates, `pipeline.New` refuses to construct without one, and the only always-solvent
> implementations are test doubles in `_test.go` files. What remains for **ii-b** is an accountant
> that can answer with real numbers, not one that can be omitted.

**Why V-PRO-028 is L1 only.** Phase 9 runs entirely in `PhaseDryRun`, so no record on a real cluster
reaches `RolledBack` and there is nothing at L2 to recover from. The end-to-end property — a live
agent actually refused, and the refusals lengthening — is V-PRO-005 and V-PRO-017, both already L2
in phase 13. The distinction is written into 09 §6.6 beside the new row: those two pass perfectly
against a cooldown held only in broker memory, which is cleared by `kubectl delete pod`.

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

## Recon 2026-07-29 — P9-T8, and the trap that T7c-3d-iv arms

Read before T8's PLAN. Recorded now because the finding is not about T8: it is about what happens to
**T7c-3d-iv** if T8 is still unbuilt when the pipeline gets wired.

**Shadow mode is declared in the API and implemented nowhere in the enforcement path.** 06 §4.1's
field table (`06:1322`) says the envelope's `dryRun` is "**Forced `true` when
`spec.operations.dryRunOnly` is set**". That join does not exist in code. Grep for any non-test
assignment forcing dry-run true returns **zero hits**. The only read of the CR field anywhere in the
tree is inside `OperationsSpec.Brake()` itself, and `Brake()`'s `dryRun` return has exactly one
consumer — `pause_not_scale_to_zero_test.go:200`. Today `dryRun` is purely a property of the
**action**, set by the caller in the envelope body (`internal/broker/envelope.go:81`), folded into
the idempotency key, and honoured at `pipeline.go:790`. The spec wants an agent-level lattice ORed
above it. There isn't one.

**Why that is T7c-3d-iv's problem and not T8's.** Phase 9 is dry-run today for a reason that has
nothing to do with `dryRunOnly`: `cmd/broker/main.go:212` still wires `broker.UnavailablePipeline{}`
and returns 503, so no execution path is constructed. That is a _situation_, not a mechanism.
**T7c-3d-iv replaces that line.** The moment it lands, the broker executes for real for any caller
that omits `dryRun`, and the operator-side switch that is supposed to prevent it does not exist.
T8 must therefore either precede T7c-3d-iv or ship with it; the ordering in the task table is not
safe as written, and this is the sharpest thing PLAN has to settle.

- **Where the OR goes.** `BrakeView` already carries the whole `Agent`
  (`pipeline/pipeline.go:84-89`), so the value is one field access from `s.brakeView =
p.cfg.Brake.Observe(ctx)` at `pipeline.go:489`. It must be applied **before** the idempotency key
  is computed: `06:2723` says reordering operations must not change the key and **changing `dryRun`
  must**. Forcing it after key verification produces either a key mismatch or, worse, a silent
  divergence between the caller's key and the broker's.
- **`agentPaused()` bypasses the guard that was written to protect it.**
  `internal/broker/brake.go:579-589` reaches into `ops.Paused` directly instead of calling
  `Brake()` — and `Brake()`'s doc comment (`common_types.go:398-403`) says in as many words that it
  exists as one function so that "a caller cannot consult `paused` and forget `dryRunOnly`, which is
  how shadow mode stops shadowing". It is the only place the broker touches `spec.operations`, and
  it forgets exactly that. [[LSN-041]]'s shape: a comment says a control exists; grep says it never
  did.
- **The `brake_controller.go:230` lead was wrong and is worth correcting in place.** `Brake()`
  returns `(paused, dryRun, reason)`, so `paused, _, _ :=` drops `dryRun` and **`reason`**, not
  `dryRunOnly`-in-slot-3. And at that site the discard is defensible: C-BR pauses a rung-5 escalation
  whether or not the agent is also shadowed, and dropping `reason` is deliberate and documented at
  `:223-227` (overwriting a human's pause reason with an automated string deletes the more
  informative of the two). The smell is real; it is just not located there.
- **No admission rule enforces stricter-only.** `dryRunOnly` appears in no webhook, no VAP, no CEL.
  The "cannot be cleared by the agent" claim rests entirely on `Agent` being absent from the actor
  templates — it is nowhere asserted as a monotonicity rule the way `ChangePolicy` and
  `requireApproval` are.
- **`status.operations.dryRunOnly` has no writer.** The observed-state mirror is dead.
- **[[LSN-040]] is literally this shape.** `execute.Request.DryRunOnly` and
  `OperationsSpec.DryRunOnly` are two fields with the same name meaning different things, joined by
  nothing — the lesson is already open and already scheduled as P9-T7c-4.

**Two stale cells in the T8 row, and two missing artifacts.** `deploy/*/scripts/` does not exist
(the MCP servers live at `agents/*/scripts/`); `internal/broker/server.go` contains no occurrence of
`DryRun` at all (the terminal path is `pipeline/pipeline.go` + `execute/apply.go`). And the unit's
premise is unbuilt: **`agents/*/skills/apply-change` exists in no tier** — all three still ship
`submit-suggestion`, which 06 §9 says is deleted — and neither `submit_action` nor `plan_action`
exists as an MCP tool.

**Check IDs.** No check in 09 mentions shadow mode or `dryRunOnly`; V-BRK-019's "dry-run" is the
server-side-apply preflight (L2, phase 10) and is a different thing. **V-GAT-019's phase in 09 §6.14
is 10, not 9** — T8 should not claim it without saying so. V-REV-001 is the reformulated-over-`DryRun`
instance already argued in planning defect 2. One genuine spec gap for PLAN: **notification under
dry-run is unspecified** — `notifyOn` is class-keyed and never mentions `dryRun`.

**Next free IDs, cross-checked against 09, `traceability.yaml` and the whole repo:** V-BRK-**023**
(022 is pre-allocated to P9-T7c-4 by this file), V-CTR-**019**, V-PRO-**030** (029 pre-allocated to
ii-b), V-RUN-**015**, V-CMP-**025**. V-CMP 009 and 012–019 exist nowhere and were never allocated —
they are gaps, not retirements (no `RETIRED` row, which §9.6 requires), so take max+1.

---

## Recon 2026-07-29 — P9-T9, and the real size of the gate

**The Phase-9 gap, derived rather than remembered.** Parsing every row of 09 §6.1–§6.14 with
`Phase == 9` gives **50 checks**. Cross-joined against the 108 data rows of `verification/results.csv`,
counting a pass only at a level the catalog lists for that check (this file's own rule: "a property
proven at a level the check does not list is not that check passing") and retracting on a later
`**correction**` at the same level:

|                                                      |                                                |
| ---------------------------------------------------- | ---------------------------------------------- |
| Phase-9 checks in 09 §6                              | **50**                                         |
| Closed (every listed level has an un-retracted pass) | **15**                                         |
| **Open**                                             | **35** — 20 BLOCKING-ALWAYS, 15 BLOCKING-PHASE |
| Of those, zero `results.csv` rows of any kind        | **13**                                         |

**This supersedes the "22 open / 9 BLOCKING-ALWAYS" figure used earlier in this session, which does
not reproduce under any reading.** One judgement call is flagged: `results.csv` row 62 is a
`correction` on V-GAT-001 at L1 but is a re-record over a _larger_ corpus (181 cases, was 165), so it
is counted as a pass; counting it as a retraction gives 36 / 20 / 16.

**33 of the 35 gaps are the L2 level — but three are not, and those are the surprise:**

- **V-CTR-005 and V-CTR-006 are L1-only and have never been recorded.** Envelope schema round-trip;
  `ActionRecord` lifecycle transitions. This is L1 work that slipped past T1 and T2, and it needs no
  cluster.
- **V-RUN-010 is L0-only and unrecorded** — broker supply-chain minimality (no LLM SDK, no
  `/bin/sh`, one listening socket, mounts exactly {cert Secret, projected token}). An L0 lint nobody
  has written.
- **V-BRK-021 needs both L0 and L2**; its only evidence is L1. Its L0 half is a lint over the shipped
  image, and it is entangled with the unresolved P9-T7c-2b halt.

**Two of planning defect 2's three guards are not in the state that paragraph assumes.**

1. **Guard 1 does not exist.** `invariants-gate.py` has 18 `check_*` functions and none mentions the
   test-only overlay. The lint that refuses an overlay reference under `k8s-operator/scripts/`,
   `deploy/` or `config/` still has to be written — and it is the one guard the paragraph itself
   calls "the single worst outcome of this decision".
2. **Guard 3 collides with [[LSN-045]], which was learned after it was written.** "The L2 script
   asserts the namespace is empty of `ActionRecord`s at teardown" cannot be satisfied by deleting the
   namespace — `kube-agents-journal-retention` denies DELETE and strands it `Terminating` permanently
   — nor by deleting the records, which is denied until `status.exported.confirmed` is true, and
   fabricating that field was **declined as a standing rule**. Guard 3 needs re-stating as an
   assertion over labels within a reused namespace, in the `brake-fanout-l2.sh` idiom.

**`verify-phase8.sh` is the template, and five of its properties are load-bearing.** Lettered
sections bound to Accept bullets. Section A runs `dev/L0-CHAIN.txt` **as a file**, never as a copied
list, with a shrink guard (`< 13` lines ⇒ fail) so a chain that lost lines cannot read as green.
`run_l2` is the single place a sub-suite's rc is interpreted — `0` pass · `3` defer (never a pass) ·
`2` could-not-run · `*` fail. Section E detects the phase's own unfinished work **by artifact rather
than by memory**, so it flips green when the work lands and cannot be talked into it — that is the
mechanism P9-T9 should reuse for its own open items. Section F regresses through the predecessor
gate; section G prints deferrals and never asserts them green. There is **no default target** and
**no per-check-ID machine-readable output anywhere in any phase gate** — section E's hard-coded
per-ID arm is the closest thing, and `results.csv` is written by the harness agent, not by any
script.

**`dev/L2-CHAIN.txt` decision T9 owes.** Twelve executable lines, each carrying its own `--context`
even though all twelve now carry the same one (the argument against a run-loop default is written
into the file). **`verify-phase8.sh` is absent from the chain** — it runs `verify-phase7.sh` and then
the individual P8 suites. T9 must decide whether `verify-phase9.sh` becomes a line or replaces the
standing-regression line. The P1 narrowing at `L2-CHAIN:38-51` exempts the four Phase-9 client-side
probes in writing, each naming **`broker-execute-l2.sh` as the successor that needs P1 in full**.

**None of the five deliverables exist**, and `dev/verify/fixtures/` does not exist as a directory.

**Split this off early: the review-gate path filter is self-contained and needs no cluster.**
`.github/workflows/review-gate.yml:11-20` still matches nothing under `k8s-operator/internal/**`. It
is V-MET-007 — the one check ID the T9 row names explicitly — it does not depend on the gate work,
and it is the reason the security gate never ran on the broker. Doing it inside the gate unit means
doing it late; doing it in its own unit means it stops being true sooner. It still must not be done
in a unit that would be reviewing itself.

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
