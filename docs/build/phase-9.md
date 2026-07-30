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

| Task       | What to build                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | Spec             | Files                                                                                                                                                                                                                                                                        | Check IDs                                                                                         | Weight           |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- | ---------------- |
| **P9-T10** | Repair the inter-agent credential seam. Declare `agent_common` with an `env:` block carrying `API_SERVER_KEY` (and the `KUBERNETES_SERVICE_*`/`HERMES_HOME` set `platform_control` gets) in **both** definition sites, for **all three tiers**; the image-baked config must also stop listing a toolset entry for a server it never declares. Bind to **V-CMP-006** with a lint that fails any MCP server whose script reads a credential from the environment and whose config declares no `env`. **Do not weaken the fail-closed refusal.** Record for P15-T1: the per-tier `API_SERVER_KEY` values currently differ and `resolve_agent_credentials` sends the caller's own key as the target's bearer.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | 05 §1; 06 §4.1   | `agents/{platform,cluster-admin,developer-team}/config.yaml` · `k8s-operator/internal/controller/agent_manifests.go:156` · goldens · new `dev/test_mcp_env_declared.py` · L0-CHAIN                                                                                           | **V-CMP-006**                                                                                     | medium           |
| **P9-T1**  | `ActionRecord` CRD + journal store. Full 06 §4.3 schema: attribution, classification, targets, `preState` (with the >1 MiB `objectRef` path), undo plan, the ten-phase status lifecycle, the **two** retention clocks, bidirectional undo linkage, `chainId`. `spec` immutable by CEL; `status` field/principal table enforced by `vap-agent-scope-journal`. Includes the journal reconciler (`C-JR`) behind a **pluggable audit source**, the retention controller's post-export deletion predicate, and the Data Access audit-log probe of planning defect 3.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | 06 §4.3          | new `api/v1alpha1/actionrecord_types.go` · `config/crd/bases/…_actionrecords.yaml` · new `internal/journal/` · `internal/controller/journal_reconciler.go` · `internal/controller/retention_controller.go` · `config/policy/vap-agent-scope-journal.yaml`                    | V-BRK-003, V-BRK-015, V-REV-008, V-CTR-\*                                                         | high             |
| **P9-T2**  | Action Envelope + broker skeleton. New tier-neutral binary and image. `POST /v1alpha1/actions` + `GET /healthz` on **8443**, HTTP+JSON over TLS (not gRPC). mTLS **and** projected token with audience `kubeagents-broker`; `TokenReview`; `(tier, scope)` derived from the authenticated caller and **never** from the body. Idempotency key = `"sha256:" + lowerhex(SHA-256(JCS(K)))`, recomputed by the broker. The three anti-replay mechanisms. Exactly one listening port, one mutating route, no `/bin/sh` in the image.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | 06 §4.1; 03 §4.1 | new `cmd/broker/main.go` · new `internal/broker/{server,auth,envelope,idempotency}.go` · new `k8s-operator/Dockerfile.broker` · `tags.env` · `deploy/docker/cloudbuild.yaml` · `dev/cluster/reload-images.sh` · publish workflows · `verification/fixtures/envelopes/`       | **V-BRK-002**, V-BRK-007/008/009/010/017, V-BRK-021, V-RUN-010, V-CTR-005                         | **load-bearing** |
| **P9-T3**  | The risk classifier + `ChangePolicy`. Deterministic, table-driven, the 06 §4.2 evaluation order (scope ⇒ short-circuit, forbidden ⇒ short-circuit, max over inputs, `+1` capped at gated, `ChangePolicy` max, no-undo-plan raise). The seventeen code-floor rules including `secret-material-egress` (digest match, **not** entropy), `cross-tier-direct-operation` (ownership computed via the V-6 subset predicate, reused not reimplemented), and the production-label precedence ladder. Both path dialects, with the `/`-prefix rejection at admission. The **120–200 case corpus** of 09 §7.1 with asymmetry pairs. Classifier package imports no inference client.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | 03 §5; 06 §4.2   | new `internal/broker/classify/` · new `api/v1alpha1/changepolicy_types.go` · CRD · webhook rule (class ≥ floor) · new `verification/fixtures/classifier-corpus.yaml` · `dev/tests/classifier-corpus-lint.py` (V-MET-005) · L0-CHAIN                                          | **V-GAT-001/002/009/010/017/021/022**, V-GAT-011, V-GAT-012                                       | **load-bearing** |
| **P9-T4**  | Undo-plan generation for every supported verb — the 06 §4.3.1 strategy table (`create`→`delete`, `apply`/`patch`→`restore`, `scale`→`restore`, `delete`→`recreate`, cloud→`inverse`, else `none`), the sanitizer, `preconditions.uid` on every step, inbound-reference detection downgrading `recreate` to `none`, and dry-run validation of each step against the API server. The explicit **"cannot generate" path reclassifies as gated** — this is what makes reversibility true rather than aspirational, so it is tested directly and from both sides. The 09 §7.3 round-trip fixtures including the negative set.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | 06 §4.3.1        | new `internal/broker/undo/` · `verification/fixtures/undo/` · unit + envtest suites                                                                                                                                                                                          | **V-REV-003**, **V-REV-004**, V-REV-001, V-REV-009                                                | **load-bearing** |
| **P9-T5**  | Snapshot → execute → verify. Server-side apply with field manager **exactly** `kube-agents/<tier>/<scope>`, dry-run first where supported, per-kind verification predicates (04 §5.1), the recovery ladder recorded in `status.recovery`, and the atomicity rule (multi-target: if any snapshot fails, **nothing** is applied). Selector fan-out expanded **once**, before classification, against live state. Write-ahead ordering: the record's durable write precedes the mutation, which precedes the API response, which precedes the chat report.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | 04 §1, §5.1      | new `internal/broker/execute/` · `internal/broker/verify/` · envtest suites                                                                                                                                                                                                  | **V-BRK-006**, V-BRK-018, V-BRK-019, V-BRK-020, V-BRK-014, V-REV-002/005/006                      | high             |
| **P9-T6**  | The brake. `Agent.spec.operations` (`paused`, `pauseReason`, `dryRunOnly`, roster/policy refs, initiative budget) and `status.operations`/`status.broker`; cluster-scoped `FleetFreeze`; `UndoRequest`; `ApprovalRoster`; the `contested` index and its advisory annotation; the undo controller (`C-UC`). Every one of the nine fail-closed rules of 06 §4.4. All five controls must work through `kubectl` and the API **with inference down** — no dependency on the model, the router, or the agent pod. `pause` is **not** scale-to-zero.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | 03 §6; 06 §4.4   | `api/v1alpha1/agent_types.go` (+`operations`) · new `{fleetfreeze,undorequest,approvalroster}_types.go` · CRDs · `internal/controller/undo_controller.go` · `internal/broker/brake.go` · webhook · goldens                                                                   | **V-RUN-007/008/012/013**, **V-BRK-005**, V-REV-007, V-GAT-003/007                                | **load-bearing** |
| **P9-T7**  | Controller reconciles the pair. Render the broker Deployment, Service (`<agent>-broker`, 8443) and certificate Secret **before** the agent Deployment, both owned by the `Agent` CR; `BrokerReady`/`AgentReady` conditions with `Ready` their conjunction; the `wait-for-broker` init container with observe-and-report on timeout; `KUBEAGENTS_BROKER_ENDPOINT` injection; the `kube-agents/role` label on both halves; the broker's NetworkPolicy (ingress only from `role: reader` with matching `kube-agents/agent`) and the agent's egress-to-broker rule. **Mints no RBAC.** Regenerate goldens.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | 08 §2            | `internal/controller/agent_manifests.go` · `internal/controller/agent_controller.go` · `pod_launcher.go` (pair-atomic `LaunchSpec`) · `netpol-*.yaml.template` · goldens · `dev/tests/reference-render.py`                                                                   | **V-RUN-001/002/003/004/005/006/009/011**, V-BRK-012, **V-BRK-011**, **V-BRK-014**, V-ISO-001/002 | high             |
| **P9-T8**  | Shadow mode. The agent's `apply-change` MCP tool submits real envelopes; the broker classifies, plans undo, and journals a `DryRun` `ActionRecord` without calling a mutating API. `dryRunOnly` is stricter-only and cannot be cleared by the agent. Run against `gke-scratch-kube-agents-dev` for the duration of the phase and mine the journal for classifier gaps — every gap found becomes a corpus case, not a code tweak.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | 04 §1            | `agents/*/skills/` (the `apply-change` skill) · `deploy/*/scripts/` MCP tool · `internal/broker/server.go` (dry-run terminal path) · a journal-mining note in this file                                                                                                      | V-REV-001 (DryRun scope), V-GAT-019, V-CHR-\* (advisory)                                          | high             |
| **P9-T9a** | **Done 2026-07-30.** The review-gate path filter: `.github/workflows/review-gate.yml` widened from five manifest globs to sixteen, over a security surface **derived** from the repo (kubebuilder RBAC/webhook markers, `tls.Config`/`TokenReview`/`SubjectAccessReview`, and authority-granting manifest kinds) rather than restated; the matcher calibrated against two recorded PR outcomes.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | 07 §5            | `.github/workflows/review-gate.yml` · new `dev/test_review_gate_paths.py` · new `verification/mutants/V-MET-007.json`                                                                                                                                                        | **V-MET-007**                                                                                     | **load-bearing** |
| **P9-T9b** | Consolidated gate `dev/verify/verify-phase9.sh`: envelope round-trip, scope-spoof rejection, classifier fixture corpus, undo-plan coverage, brake liveness with inference down, fail-closed on journal loss, and the Accept (e) two-sided `can-i` sweep. The test-only tenant overlay of planning defect 2, with its three guards. Regression through `verify-phase8.sh`. New L0 and L2 chain lines. ~~**Also fix the review-gate path filter, found during P9-T2:**~~ **Split out as P9-T9a and done 2026-07-30** — `.github/workflows/review-gate.yml` triggers on `**/policy/**`, `**/agents/**`, `**/provisioning/**`, `**/namespaces/**` and `**/SOUL.md` — none of which match `k8s-operator/internal/**`, so PR [#33](https://github.com/adamparco/kube-agents/pull/33) added the broker, an authenticator and the one image whose SA can write, and the security gate did not run on it. The gate was written when the security surface was manifests; it now includes Go. Widen the filter to the broker, webhook, router and RBAC paths **in the unit that owns the gate**, not in a unit that would be reviewing itself. Expect the first run to need waiver triage: the suite was tuned on YAML. | 07 §5            | new `dev/verify/verify-phase9.sh` · new `dev/verify/broker-auth-l2.sh` · `dev/verify/broker-execute-l2.sh` · `dev/verify/actor-grant-sweep-l2.sh` · `dev/verify/fixtures/actor-tenant-grant.yaml` · `dev/tests/invariants-gate.py` · `dev/L0-CHAIN.txt` · `dev/L2-CHAIN.txt` | all of the above (V-MET-007 closed by T9a)                                                        | **load-bearing** |

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
      safe to take. **The blocker CLOSED 2026-07-30** — 09 §6 was edited by T7c-2c
      below, which is exactly the promotion condition the deferral row named. The task itself moves
      to Phase 10, where it is one unit with `/approve` against the one reshaped check.

    - **P9-T7c-2c** — **done 2026-07-30. The ruling arrived, and it is option (a): reshape
      V-BRK-021.** Re-records **V-BRK-021** at L0 over the new form; sweep 10/10. Scheduled
      2026-07-30 from `BACKLOG.md` **B-003**, a human ruling on the deferral row 2b opened. The row's
      promotion condition was one sentence — _"this row closes when 09 or 05 is edited"_ — so this
      task is what closes it. **`todo`, and it is the next unit**, ahead of T8b-4b-ii-2b and T9b,
      because it is L0 and this phase's own ordering rule puts the remaining L0 work in front of the
      remaining L2 work. Three things, all small: rewrite 09 §6's V-BRK-021 row so the assertion is
      **an equality against the registered handler set** rather than a count; make
      `Server.MutatingRoutes()` derived from, or cross-checked against, that registered set instead
      of the hand-written `[]string{ActionsPath}` at `server.go:177`; and stop
      `server_test.go:433`'s `strings.Count(src, "s.mux.HandleFunc(") != 4` being the thing the
      property rests on. **What it is not:** it does not implement `/replay` or `/approve`. Those
      stay in Phase 10 beside P10-T4 / P10-T7, where the item asks for them to be one unit against
      one reshaped check. It also does not settle V-BRK-021's L0-vs-L2 evidence gap — that is T9b's.
      **Why this is not a PROTOCOL §10.2 halt** even though the new form admits three mutating routes
      where the old admitted one: §10.2's remedy is a halt _for human review_, and the review has
      already happened — the deferral row named exactly two admissible rulings and a human picked (a)
      by name. The argument is recorded in full in `BACKLOG.md` §Scheduled under B-003.
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
          **Check: V-BRK-028** (new, L1). The gap to **V-BRK-022** is not an error: that ID is
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
        - **P9-T7c-3d-iii-b — `classify.ActionHistory`** ✅ (2026-07-29) — the journal-derived
          novel-action source, and the two ways 06 §4.2's escalation could be switched off.
          `internal/broker/history` (new package), `classify.New`/`classify.go`,
          `policy.NewSource`. **V-BRK-029** (new, L1, BLOCKING-ALWAYS) in 09 §6.14, bound in
          `traceability.yaml` under `06§10#29` and `06§10#36`. Evidence: **100.0% statement
          coverage** under `-race`, 17 hermetic functions / 58 cases plus 6 envtest functions,
          and a **35/37 mutation sweep, 0 escaped, 0 broken**.

          **The finding that shaped the whole task: the `ActionRecord` CRD records no verb.**
          Checked against both `api/v1alpha1/actionrecord_types.go` and 06 §4.3's canonical yaml.
          So a journal-derived history cannot read `patch` or `delete` off a record, and 06 §4.2's
          "has this agent done this before" has no field to answer from. Three obvious ways out,
          all rejected with the argument recorded in the package doc:

          - **Ignore the verb** — an agent that had patched Deployments would be familiar with
            _deleting_ one. That LOWERS a risk class, which invariant 4 forbids outright.
          - **Never answer true** — strictly stricter, and vacuous: the `+1` fires on 100% of
            traffic forever, whose end state is approval fatigue. A gate everyone rubber-stamps is
            less safe than no gate, so "safe direction" is not by itself a defence.
          - **Add a CRD field** — a 06 §4.3 spec amendment, which PROTOCOL §10.5 forbids. The same
            argument `cooldown` recorded for not inventing a CRD.

          **The resolution: 06 §4.3.1's undo-strategy table read BACKWARDS.** The verb is recovered
          up to an equivalence from two durable, enum'd fields — `spec.undo.strategy` and
          `spec.undo.steps[].op` — and the equivalence is the point rather than a compromise. Every
          collapse is between operations that are **the same mutation**: `delete/delete` is the
          plan for a `create` and for an `apply` over an absent object, which is the same write.
          The pairs that must NOT collapse do not: `recreate/create` is reachable only from
          `delete`, `restore/scale` only from `scale`, and `apply` requires **both** its classes,
          so an agent that has only ever created is still novel the first time it updates.
          `TestTheUndoPlanRecoversTheVerbClass` is that table with a `familiar` and a `novel` list
          per row — it is what stops the coarsening becoming a loosening, and mutating any row is
          CAUGHT.

          **Two spec silences resolved as Decisions, not §8.5 halts.** 06 §4.2 names a
          "trust-building window" and defines none: it is **the journal's own retention window**,
          because the evidence and the window are then the same object — nothing separate expires
          and the two cannot drift. And a **dry run builds no trust**: the whole of Phase 9 is dry
          runs, so counting them would have every agent arrive at Phase 10 familiar with everything
          it had never actually done. `Undone` is excluded for the sharper version of the same
          reason — the write stood and a human reversed it, so counting it would suppress the
          escalation on exactly the repeat a human just said no to.

          **The other half of the task was the nil.** `classify.Classifier` guarded the escalation
          with `c.knownActions != nil &&`, so a broker nobody had wired a history into ran with 06
          §4.2 **off** — a risk class lowered by an omission. Four changes close it: `classify.New`
          refuses a nil; the consumption guard is inverted to `nil ||` so unknown ⇒ novel ⇒
          escalate; `classify.AlwaysNovel{}` is the deliberate spelling of "no journal"; and
          `policy.NewSource` refuses a nil at **construction** rather than letting it surface as a
          failing poll seconds after startup. Same hole ii-a closed for the accountant, one package
          over.

          **The envtest half is what makes the design a measurement.** `ActionRecord` carries a
          status subresource, so `client.Create` **drops `status` entirely** — a record created
          with `Verified` comes back with an empty phase and confers nothing until
          `Status().Update()` writes it. Every hermetic test sets that field on a struct and
          "works"; only a real server shows that the filter reads the field the server actually
          stores. This is the mirror of iii-a's finding. Two of the sweep's mutations are on the
          **CRD yaml, not the Go** — dropping the status subresource, and widening the strategy
          enum — and both are CAUGHT only by envtest tests, which is what keeps that half from
          being decoration.

          **Two mutations are recorded REDUNDANT rather than deleted.** `seen == nil ||
readAt.IsZero()` is subsumed by the staleness ceiling (a zero read time is stale against
          any real clock), so no single mutation can make an unrefreshed source vouch — it takes
          two. And teaching `class` to emit `none/<op>` changes no answer, because `verbEvidence` is
          a closed vocabulary and nothing looks that class up. Both escapes are the design working;
          recording them beats deleting the rows, since a coverage claim nobody can audit is worse
          than an honest gap. **`classify.KnownVerbs()` finally has a caller** — the lint its own
          doc claimed existed (see T7c-4) — though only a partial one: `TestEveryKnownVerbHasEvidenceDefined`
          joins it to `verbEvidence`, not yet to the envelope's enum.

          **Nothing constructs a `history.Source` yet.** Wiring is T7c-3d-iv, which must not land
          before or without T8.
      - **P9-T7c-3d-iv** — the wiring itself: a discovery client (constructed nowhere today, and
        `refindex.Source` requires it non-nil), `pipeline.New` replacing
        `broker.UnavailablePipeline{}`, `policy.Source` with a synchronous startup `Refresh` and a
        backgrounded `Run`, `cooldown.NewSource`, and `broker.NewContestedIndex`. **Closes
        LSN-007**, which needs a new L0 source assertion to close honestly: no 09 §6 check asserts
        "the pipeline is constructed in `main.go`", and `install-path-wired.py` never reads Go.
        **Split into iv-a and iv-b during survey — see the section below.**
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
    - **Split into 4a and 4b at SELECT, 2026-07-29.** The recon above is five findings deep and the
      task carries two independent deliverables — a **conversion** between two packages' readings of
      the same word, and a **mechanization** that discovers its own verb set. Each has its own check
      and each is checkpointable alone, which is the `harness-run` §2 test. Doing them together
      would mean a unit whose diff spans `classify`, `execute`, `pipeline` and a new lint, verified
      by one check that did not exist when the work started.

      | Unit                 | Scope                                                                                                                                                                                                                                                                                                          | Checks               | Blocks on |
      | -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------- | --------- |
      | **P9-T7c-4a** — done | The conversion. `apply`, `scale` and merge-patch reach the classifier with real `TouchedPaths`; the `stepResolve` reorder that makes `snap.Live` available before `classify.Resolve`; `apply` stops being `WholeObject`; the two false comments deleted.                                                       | **V-BRK-020** — pass | nothing   |
      | **P9-T7c-4b** — done | The mechanization. **V-BRK-022** — every verb in the envelope's closed enum executes end to end through the assembled pipeline, the verb set **discovered from the enum** — plus exporting that enum and the lint joining it to `classify.KnownVerbs()`. Closes [[LSN-040]] and the `KnownVerbs` prose defect. | **V-BRK-022** — pass | **4a**    |

      **The complication LSN-040 warns about dissolves on inspection.** The lesson says feeding a
      computed diff into `classify.PatchOp.Value` is lossy for the typed rules, because
      `execute.DiffResult.Ops` render `Value` as a **string** and `DirectionOfBoolField` would see
      `"true"`. But `classify.PatchOp.Value` is `any`, and for an `apply` the **desired object is in
      hand** — so the diff supplies the _paths_ and the desired object supplies the _typed values_.
      Nothing has to be read back out of a rendered string. This is only true because both inputs are
      available at the same point, which is what the `stepResolve` reorder buys.

    - **What 4a actually landed — 2026-07-29.** The dissolution above held, and the reorder was the
      whole mechanism: `stepResolve` now runs `execute.CaptureAll` before `classify.Resolve`, and
      `fillTouchedPaths` sits between them. No new API read, no new interface, one new function on
      the path.

      **It closed a live fail-closed bug, not just a classification gap.** `classify.Resolve` set
      `WholeObject` for create, **apply** and delete; `execute.checkWholeObject` accepts only create
      and delete and its `default:` arm refuses anything else. So **no `apply` could execute through
      the assembled pipeline at all.** `TestApplyFailsClosedAtTheIntegrityCheck` recorded that
      deliberately in T7c-1 and instructed its own replacement by a positive counterpart; three
      end-to-end tests are that counterpart.

      **The security property is the negative half.** `classify.matches` returns false for any
      `when.fieldPaths` rule against an empty path set, so a rule reading
      `fieldPaths: [data.log-level]` was silently inert against `apply` while reading in a policy
      review as a control in force. `TestAFieldPathsRuleFiresOnAnApply` asserts both directions,
      because a test asserting only "the rule fires" would pass equally against an implementation
      reporting **every** field as touched — the other way to make a fieldPaths rule match, and
      exactly as wrong. That implementation is mutant M3 and the negative subtest is what kills it.

      **The sweep found a hole in the check itself.** The scale expectation was written as
      `Path: scaleReplicasPointer`, comparing the constant against itself; M8 changed the constant to
      `/spec/replica` and the package stayed green. Fixed by spelling the pointer the way a rule
      author would. Final score **14/14 caught, 0 escaped**.

      **Two findings filed rather than fixed** (never in the same unit as the implementation):
      09 §6's V-BRK-020 row cites **03 §4.4**, which contains none of "strategic", "expand" or
      "integrity"; and **05 §1.1** claims the dry-run carve-out list "is a code constant" where the
      implementation has an injected `ClientApplier.DryRunUnsupported func(TargetRef) bool` and no
      `V-BRK` check asserts an envelope per carve-out kind. The second is an **unimplemented spec
      requirement**, so it is scheduled rather than edited — narrowing the doc to match the code
      would be weakening a spec, which invariant 10 forbids.

      **What 4b inherits.** The conversion is in place for every verb that arrives as an object, so
      4b's end-to-end-per-verb check has something to assert against. `classify.knownVerbs` still
      contains `"cloud"` where `broker.validOps` does not, which 4b's lint has to account for
      rather than trip over.

    - **What 4b actually landed — 2026-07-29.** V-BRK-022 exists, discovers its verb set from
      `broker.ValidOps()`, and drives all five ops through the assembled pipeline. **It found two
      more gaps the first time it ran**, both the LSN-040 shape — a package that is individually
      right, and the assembly as its first caller:

      - **`delete` could never be verified.** Every row of 04 §5.1 asserts something about a live
        object and `verify.mustGet` maps NotFound to `VerdictFailed`. A delete that worked would
        have been reported failed and rolled back **by recreating what it deleted**. Fixed with
        `verify.Target.ExpectAbsent` and an `absencePredicate` chosen by the action rather than by
        the kind, with still-present as `Pending` (deletion is asynchronous; finalizers) so only the
        settle window expiring makes it a failure.
      - **No Deployment or StatefulSet action could ever be verified.** `verifyTargets` built
        `verify.Target{Ref: r}` and dropped every other field, so `BaselineRestarts` was always nil,
        `workloadPredicate` always returned `VerdictIndeterminate`, and the settle window always
        expired into `VerdictFailed`. Every workload change that worked would have rolled back.
        Fixed by `verify.CaptureRestartBaselines` at step 3, next to the snapshots, on the same
        all-or-nothing terms — a baseline read after the write is the post-action count compared
        against itself.

      Neither was reachable from either package's own tests: `verify` tested `workloadPredicate`
      with a baseline the test supplied, and the pipeline tested a ConfigMap, whose row needs none.
      This is the entire argument for a check that discovers its verb set instead of restating it.

      **A rig defect surfaced with them.** The pipeline test rig gave `verify.Driver` a fixed clock
      and a no-op `Sleep`, so any `Pending` verdict polled forever — the `scale` case hung the whole
      package for 120s rather than failing one subtest. The clock now advances by whatever the
      driver sleeps, which is the honest fake of a clock/sleep pair and no slower.

      **Two prose defects closed, both [[LSN-041]].** `classify.knownVerbs` claimed "the corpus lint
      asserts the two agree" and no such lint existed; it is now
      `TestClassifyKnownVerbsAgreeWithTheEnvelopeEnum`, a Go test in `package pipeline` — the lowest
      package importing both `broker` and `classify` — rather than a Python lint parsing Go source.
      The `cloud` divergence is declared in code as `classify.VerbsNotCarriedByAnEnvelopeOp` with a
      written reason, and the condition making it safe is a property
      (`TestNoCloudTargetReachesTheClassifier`) rather than a sentence.

      **`verify.ErrTargetReplaced`.** A UID mismatch is evidence about a stranger for every row and
      the answer for `absencePredicate` — the deleted object is gone and something else holds its
      name. `probe.Source` now wraps a sentinel instead of prose, and the envtest that asserts the
      refusal asserts the sentinel too; that assertion is the only place the two packages' halves of
      the contract are compared.

      **Sweep: 15/15 caught** (`verification/mutants/V-BRK-022.json`), baseline green, catchers
      verified against the suite.

      **One finding filed rather than fixed.** `agentv1alpha1.ChangeVerb`'s kubebuilder marker
      (`+kubebuilder:validation:Enum=create;apply;patch;delete;scale;cloud`) is a **third** copy of
      the verb set, and its doc comment claims it "mirrors the envelope's own" with nothing
      comparing them — the same prose-as-control shape the join test just closed twice. Both
      mismatch directions fail closed today (a `ChangePolicy` naming a verb the CRD rejects is
      refused at admission; a verb the CRD admits that classify does not know matches no rule), so
      it is a finding, not a live defect. Fixing it means joining the marker to `broker.ValidOps()`,
      which is a generated-manifest lint rather than a Go test, and belongs with the other
      corpus-lint work rather than folded in here.

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

> **Stale as of 2026-07-30 — do not read the V-BRK number off this line.** P9-T8b-1 allocated
> V-BRK-023 from it and `dev/tests/spec-ids.py` refused the commit: 023 through **027** were taken
> between the recon and the unit (023 write-ahead confirmation, 024–027 the pipeline units), so the
> new check became **V-BRK-028**. The list was correct when written and is a snapshot, not a
> reservation. **The gate caught it, so nothing shipped wrong** — which is the argument for
> `grep -rho "V-BRK-[0-9]\{3\}" docs/ verification/ dev/ k8s-operator/ .claude/ | sort -u | tail -1`
> at allocation time rather than trusting any written-down "next free".

---

## P9-T8 ships as two units — T8a (the mechanism) and T8b (the surface)

The precedent is T3a/T3b, T5a/T5b and P8-T8a/b/c. The row is not one deliverable: it is a **join in
the enforcement path** and a **skill plus an L2 soak that exercises it**, and only the first is
buildable today.

| Unit       | What                                                                                                                                                                         | Checks                    | Blocked on                                                                                        |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------- | ------------------------------------------------------------------------------------------------- |
| **P9-T8a** | The forcing join itself: `spec.operations.dryRunOnly` → the effective dry-run decision, in `pipeline.Submit`. Hermetic, L1.                                                  | **V-BRK-025** (new)       | nothing — **and it must precede T7c-3d-iv**                                                       |
| **P9-T8b** | The `apply-change` skill in all three tiers, the `submit_action`/`plan_action` MCP tools, `submit-suggestion`'s deletion (06 §9), and the L2 shadow soak with journal mining | V-REV-001 (L2), V-GAT-019 | **T7c-3d-iv.** Nothing executes and nothing journals in a real broker until the pipeline is wired |

**The ordering is the point, and it is the thing the recon above said PLAN had to settle.** T8b is
what the T8 row is mostly _about_, and it is the half that cannot be done. T8a is the half that the
recon showed T7c-3d-iv arms a trap for: the moment `UnavailablePipeline{}` is replaced, the broker
executes for real for any caller that omits `dryRun`, and the operator-side switch meant to prevent
that has to already exist. Shipping T8a now inverts the task table's ordering deliberately. **T8b
carries no BLOCKING-ALWAYS check**, so waiting on it costs nothing the gate will notice; V-GAT-019's
phase in 09 §6.14 is 10 regardless.

> **T8b was unblocked on 2026-07-30 and split again, into T8b-1…4.** See
> "P9-T8b splits into four" below; the row above is superseded by that table.

### What T8a found

**Nothing in the broker read `spec.operations.dryRunOnly`.** Second instance of [[LSN-007]] in this
phase: a documented field, a printer column, and a `status.operations.dryRunOnly` mirror — and an
operator who set it got an agent that executed. The field's own guard predicted it.
`OperationsSpec.Brake()` exists as one function returning all three brake values precisely so that
"a caller cannot consult `paused` and forget `dryRunOnly`, which is how shadow mode stops
shadowing" (`common_types.go:398-403`). `brake.go`'s local `agentPaused()` reached into `ops.Paused`
directly and forgot exactly that. Until this unit, **nothing in the tree called `Brake()` outside a
test.**

Four decisions, each of which had a plausible wrong answer:

- **Scoped to execution; classification is a deliberate exception.** The forced value reaches the
  executor, `stepVerify`, the terminal phase, the caller-facing message and the journaled record's
  `spec.dryRun`. It must **not** reach step 4: `classify.go:166` reads `if !in.DryRun &&
!hasUndoPlan(in)`, so feeding it the forced value suppresses the no-undo-plan escalation and the
  shadow record under-reports its class. That is both the permissive direction under invariant 4 and
  a defeat of shadow mode's purpose — a shadow is read as evidence, and one that under-reports is
  worse than no shadow at all. The sweep mutates this exception in both directions.
- **Derived, never written onto the envelope.** The obvious implementation — `env.DryRun = true` —
  works inside the package and breaks two packages away: `CompareIdempotencyKey` (`server.go:306`)
  recomputes the key over `dryRun` **before** `Pipeline.Submit` at `:344`, so every shadowed
  submission would return `400 idempotency-key-mismatch`. The recon anticipated the ordering
  constraint and got the direction backwards: the forcing must land after key verification, not
  before, and must not mutate the input it verified against. That mutation is row 15 of the sweep.
- **The field is `mayExecute`, not `dryRun`, and the polarity is the safety property.** Stored as
  permission-to-execute, the zero value — what the struct holds before anything computed the answer,
  and what a step inserted above the computation would read — is `false`, which reads back as "this
  is a dry run". A field spelled `dryRun bool` fails open on exactly the same mistake.
- **Unobservable means shadowed.** A nil `BrakeView.Agent` returns `true`. Asserted **directly on
  the predicate**, because the composed submission is over-determined: the brake's
  `agent-unreadable` row refuses a nil Agent at step 5 anyway, so a composed-only assertion would
  stay green with the predicate inverted. The over-determined claim is kept and labelled as such.

Two over-determinations surfaced this way and both were re-pointed at an assertion that can fail.
The other was the classification test: brake row 5 (`undo-plan-unusable → RaiseToGated`) gates all
three lattice rows independently, so `class` is identical whatever step 4 sees. The property lives
in `spec.classification.reasons`, and the test asserts it as an **equality against the real run**
(`reasons(shadowed) == reasons(real)`) with a third row as the non-vacuity control, rather than
against a hardcoded class that the brake would have satisfied on its own.

**Evidence.** 5 test functions / 11 cases in `internal/broker/pipeline/shadow_test.go`; `dryRun()`
and `shadowed()` at 100.0% statement coverage, `Submit` at 83.3%, package 77.0%, all under `-race`;
mutation sweep **15/15 caught, 0 escaped, 0 broken**, each row naming the specific test that catches
it and no `-run` pattern anywhere ([[LSN-048]]).

**The sweep mis-scored itself first, and that is [[LSN-049]].** Row 14's needle contains `""` (Go's
empty string literal). The sweep interpolated needles into a double-quoted `bash -c` argument, so
the quote closed early, the mutation was never applied, the python died, `&&` short-circuited — and
the run still exited 0, which the sweep printed as "ESCAPED, nothing failed" for a mutation the
suite catches cleanly. [[LSN-048]] with the sign flipped: there the tool hid a hole, here it
invented one. Needle and replacement now travel by environment variable to a helper that refuses
unless the target appears exactly once. Row 14 also caught a **mis-attribution** on the first pass —
the intuitive guess named `TestNothingComposesBackToExecuting`; the actual catcher is
`TestAnUnobservableAgentIsShadowed`. Requiring the _named_ test rather than "something went red" is
the only reason either defect was visible.

**Left for T8b or later, deliberately:** no admission rule enforces `dryRunOnly` stricter-only (it
is nowhere asserted as monotonic the way `ChangePolicy` and `requireApproval` are);
`status.operations.dryRunOnly` still has no writer; and notification under dry-run remains
unspecified in 06 §4.1 (`notifyOn` is class-keyed and never mentions `dryRun`). None of the three
are execution-path holes — the join is what T7c-3d-iv needs, and the join is what shipped.

---

## P9-T7c-3d-iv splits — iv-a (the identity seam) and iv-b (the wiring)

Surveying iv found every production adapter already exists, so the unit looked like pure
construction. One seam was not ready to be constructed against.

**What the survey found.** `policy.SourceConfig` took `Agent Agent` — a **value**, captured in
`NewSource` and passed to `Build` on every poll for the rest of the process's life. Wiring it
requires answering "where does this broker's own `(tier, scope)` come from", and the honest answer
turns out not to be a startup read:

- `--scope` carries only `scope.Of(agent).Leaf()`, a single string. `policy.Agent` needs the whole
  triple, so the value has to come from the Agent CR either way.
- `spec.tier` is immutable (webhook + CEL). **`spec.scope` is not** — `agent_webhook.go:181` says so
  in as many words: "spec.tier is immutable under V-1, and so scope is the only half of the key the
  operator can actually edit."
- A scope edit does **not** reliably roll the pod. `broker_manifests.go:368` renders
  `"--scope=" + scope.Of(agent).Leaf()`, and `Leaf()` is the deepest **set** level. Edit a
  cluster-admin's `projectId` and the leaf is still its `clusterName`: no rendered argument changes,
  no rollout happens, and a pinned identity is stale for the life of the pod. Only the platform
  tier — whose leaf _is_ its `projectId` — would be rescued by the rollout.

**Why that is not a small staleness.** `Binds` ANDs the tier clause with
`scope.Contains(policyScope, agentScope)`, and a ChangePolicy can only **tighten**. So a binding
that is _lost_ is the loosening direction: the broker classifies lower than the operator wrote, and
the record's `policySources` omits the policy without an error anywhere. Three separate inputs lose
bindings the same silent way — a stale scope, an ill-formed scope, and the zero Agent.

**And the codebase had already answered this question the other way.** `pipeline.callerScope` reads
`scope.Of(p.cfg.Brake.Observe(ctx).Agent)` on **every submission**. Pinning in `policy.Source` would
have made it the one place the broker's own identity was frozen — an inconsistency neither side
reveals when read alone. Wiring the pinned field would have been the second half of the trap
[[LSN-041]] describes: a seam that looks wired, is wired, and is wired to the wrong thing.

| Unit               | Scope                                                                                                                                                                  | Checks                     | Blocks on |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------- | --------- |
| **P9-T7c-3d-iv-a** | `SourceConfig.Agent` → `Identity func() (Agent, error)`, resolved once per poll; `Build` refuses an ill-formed own scope; nil `Identity` refused at construction. L1.  | **V-BRK-026** (new)        | nothing   |
| **P9-T7c-3d-iv-b** | The wiring: discovery client, `pipeline.New`, the remaining sources, and the reflection-over-`pipeline.Config` assertion that closes [[LSN-007]]. **Done 2026-07-29.** | **V-BRK-027** (new) — pass | **iv-a**  |

### The distinction iv-a is built on

`Scope{}` is a **legal identity**, not an error value. `validateScopeAndParent` returns early for the
platform tier — "projectId is conventional but scope may be nil here" — so a scopeless platform
agent genuinely narrows nothing, and `Scope{}.IsWellFormed()` is true. "Fleet-wide" and "the Agent CR
could not be read" are therefore **the same value and different facts**, which is why `Identity`
returns an `error` rather than a zero `Agent`: collapsing them would make an unreadable CR classify
as the widest agent in the fleet. It is the same shape as T8a's `shadowed()` — an unobservable Agent
must not read as the permissive answer — and the negative control is the same kind too: the zero
scope must still classify, which is what stops the other assertions from being satisfied by
"refuse anything not fully narrowed".

The two failure classes stay apart on the axis the package already uses. An **unreadable** Agent CR
is transient (usually it _is_ an unreadable API server), so it is **retained** and aged out on
`MaxPolicyStaleness` — same clock, no second timer. An **ill-formed** scope was read successfully
and is unusable until a human edits it, so it is **discarded** and refuses immediately.

### What iv-a left for iv-b

- The `Identity` closure itself. iv-a defines the seam; nothing constructs one outside a test yet,
  so `main.go` still holds `broker.UnavailablePipeline{}`. The intended closure reads through the
  brake — `func() (policy.Agent, error) { v := brakeSrc.Observe(ctx); if v.Agent == nil { return
policy.Agent{}, errors.New(...) }; return policy.Agent{Tier: ..., Scope: scope.Of(v.Agent)}, nil }`
  — which reuses the TTL'd read the brake already performs rather than adding a second watcher.
- **A finding, not a fix.** `pipeline.go:412` guards `callerScope` with `IsWellFormed()` only, and
  `Scope{}` passes it. A nil `BrakeView.Agent` therefore yields the fleet-wide caller scope at
  step 3 — the permissive direction — and is caught downstream only because the brake's
  `agent-unreadable` row (`brake.go:203`) refuses at step 5. Composition saves it; the classification
  and the record written before that point are still built against an identity nobody read. Recorded
  rather than fixed here, because changing classification inside a wiring unit is exactly the mixing
  the protocol forbids. Sweep it with the V-BRK-020/021 and V-REV-002 citation defects.
- **A second finding.** No production `journal.BlobSink` exists — only the interface, plus
  `WriterSink`/`MemorySink`, which implement the _different_ `AuditSink`. The >1 MiB `objectRef`
  path (06 §4.3) has no implementation, so `BodyStore` and the rollback `Sink` must stay nil at
  iv-b. That is documented-legal ("a step that needs it then refuses by name rather than by nil
  dereference") and it is why those two fields need allowlist entries in iv-b's reflection check.

### What iv-b actually landed — 2026-07-29

`cmd/broker/wiring.go` (new) + the three-line change in `run` that replaces
`broker.UnavailablePipeline{}` with the pipeline it builds. **V-BRK-027** is the check.

Everything predicted above held: the identity closure reads through the brake, `BodyStore` and the
rollback `Sink` are nil with allowlist entries carrying the BlobSink reason, and `pipeline.go:412`
was left alone. Three things the survey had not predicted:

- **The assembly needed its own file, not eighty more lines in `run`.** `run` dials a kubeconfig, a
  clientset and a TLS keypair before it builds anything, so a `pipeline.Config` assembled inline is
  unreachable from a test — and a check that cannot see the wiring is no defence against the lesson
  it exists for. `pipelineConfig` is a function whose whole output is the config.
- **Order turned out to be load-bearing, so it is asserted.** `brake` must be refreshed before
  `policy`, because the policy source's first `Refresh` calls the identity closure, which reads the
  brake's cache. Get it backwards and startup fails naming ChangePolicy for a problem that is the
  Agent CR — the wrong RBAC rule, in the one message an operator will read. The five sources became
  an ordered `[]startable` with a fatal first read; pollers start only after all five reads succeed.
- **Discovery is not in the 06 §2.2.1 grant and does not need to be.** The grant has no
  `nonResourceURLs` and the VAP refuses any that does, but Kubernetes binds `system:discovery` to
  `system:authenticated`, so the two enumerating adapters get `/api` and `/apis` without the grant
  widening — which is why it can stay byte-identical across tiers.

**The mutation sweep found a hole in the check itself** (13 mutants, all now caught). M9 hoisted
`go s.run` above the refresh and the "no poller is left running" assertion stayed green: it used a
non-blocking `select ... default`, which only observes goroutines the scheduler happened to have run
already. Asserting the **absence** of an event needs a bounded wait, not a poll. Fixed before the
check was recorded. Also: a mutant that does not compile scores as an escape, which is how M2's
first form was caught — `Contested: nil` orphans the `broker` import, so it never built.

**Carried, still not fixed:** the `pipeline.go:412` `Scope{}` finding above; the V-BRK-020/021 and
`execute/apply.go` V-REV-002-for-V-BRK-006 citation defects; and `ContestedIndex` is wired **empty**
and known to be — rebuilding it from `ActionRecord.status.contested` is P9-T6c's, which is still not
scheduled anywhere. Empty answers "not contested" for everything, which is the loosening direction;
the only alternative available today is nil, and nil makes the brake refuse every action.

---

## P9-T8b splits into four — the survey that forced it

T8b was unblocked by T7c-3d-iv-b. Surveying its surface before starting it showed the row is four
deliverables wearing one name, and two of them cannot be checkpointed in a session:

- **`submit-suggestion`'s retirement** touches **~110 files** — all three tiers' `SKILL.md` and
  `scripts/submit_suggestion.py`, `dev/test_submit_suggestion.py`, `examples/gitops-repo/`,
  `agent_manifests.go`, `internal/testing/`, `verification/traceability.yaml`, `INSTALL.md`,
  `deploy/shared/defaults/config.yaml`, the design docs and the site. And 07 §2 phases the per-tier
  replacement as **P10-T3**, so doing it here is pulling Phase 10 work into Phase 9.
- **The L2 shadow soak** needs a live scratch cluster, rebuilt agent images through Cloud Build, and
  journal mining over real records. That is its own session and its own preconditions (P1, P3, P4).

The remaining two halves are hermetic and each is a unit. The split:

| Unit               | What                                                                                                                                                                                                  | Checks                                                                       | Blocked on             |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- | ---------------------- |
| **P9-T8b-1**       | The agent-side **envelope builder**: JCS, the §4.3.1 sanitizer, the 06 §4.1 operation sort, and the `idempotencyKey` — in Python, byte-identical across all three tiers, hermetic                     | **V-BRK-028** (new)                                                          | nothing                |
| **P9-T8b-2**       | `submit_action` / `plan_action` as MCP tools on top of the builder: nonce fetch, mTLS + projected-token transport, `trace`/`requester` from the session, `ActionResponse` rendering                   | **V-BRK-029** (new)                                                          | T8b-1                  |
| ~~**P9-T8b-3**~~   | ~~The `apply-change` skill in all three tiers, and `submit-suggestion`'s retirement (06 §9, §10)~~ — **split, see below**: the skill is Phase 9's, the retirement is Phase 10's                       | ~~V-GAT-019 (phase **10**)~~ — mis-bound                                     | —                      |
| **P9-T8b-3a**      | The `apply-change` skill in all three tiers, alongside `submit-suggestion` — **done 2026-07-30**                                                                                                      | **V-CTR-020** (new)                                                          | T8b-2b                 |
| **P9-T8b-3b**      | `submit-suggestion`'s retirement — **deferred into Phase 10 as P10-T3**                                                                                                                               | —                                                                            | Phase 10               |
| ~~**P9-T8b-4**~~   | ~~The L2 shadow soak with journal mining~~ — **split, see below**: the broker has no deployment path, so there is nothing to soak yet                                                                 | ~~V-REV-001 (L2)~~                                                           | —                      |
| **P9-T8b-4a**      | The broker's deployment path, and the L2 claim it makes checkable                                                                                                                                     | **V-BRK-012 (L2)**                                                           | a live scratch cluster |
| ~~**P9-T8b-4b**~~  | ~~The L2 shadow soak with journal mining~~ — **split again, see below**: nothing in `dev/` can present a credential to a broker, so there is no caller to soak with                                   | ~~V-REV-001 (L2)~~                                                           | —                      |
| **P9-T8b-4b-i**    | The in-cluster envelope driver, and the five transport checks it makes answerable — **done 2026-07-30**                                                                                               | **V-BRK-007/008/009/010/017 (L2)**                                           | T8b-4a                 |
| **P9-T8b-4b-ii-1** | Step 3's live reads answer a typed refusal, split by whether retrying can help                                                                                                                        | V-BRK-031 (L1, L2)                                                           | T8b-4b-i               |
| **P9-T8b-4b-ii-2** | The L2 shadow soak with journal mining, over the read-only tenant overlay                                                                                                                             | V-REV-001 (L2)                                                               | T8b-4b-ii-1            |
| **P9-T8b-4c**      | `session_trace()` emits `parentSpanId`, which the broker's closed schema refuses; fix the shipped client across all three tiers and add the assertion that would have caught it — **done 2026-07-30** | **V-BRK-032** (new, 09 §6.14) + **V-BRK-028** and **V-BRK-029** strengthened | —                      |
| **P9-T8b-4d**      | `trigger` becomes a parameter of `submit_action`/`plan_action` per 06 §9, across the three tiers' MCP tools and the `apply-change` skill that teaches them — **done 2026-07-30**                      | **V-CTR-020** and **V-BRK-029** strengthened; **V-BRK-032** extended         | T8b-4c                 |

**Why T8b-1 is the first half and not an arbitrary slice.** Everything downstream is transport and
prose; this is the only part with a _correctness_ obligation the broker will enforce. The broker
**recomputes** `idempotencyKey` and `CompareIdempotencyKey` refuses a mismatch — so an agent-side
builder that diverges by one byte does not degrade, it makes **every write in the fleet refused**,
with a message about a key rather than about the divergence. And the divergence is not hypothetical:
the key is computed over the operations _after_ `journal.Sanitize`, so a Python side that forgets to
digest a Secret's `data` gets a different key **and** has credential material in the hash input.

**This is a second definition site, deliberately, and the join is the check.** [[LSN-040]] and
[[LSN-041]] both say a second copy of a rule is only allowed when something mechanically compares it
to the first. There is no way to avoid the copy — the agent image is Python, the broker is Go, and
06 §9 puts the key computation in the MCP tool. So the copy is made and joined:
`verification/fixtures/envelopes/valid/` already carries **six envelopes, each with the key its own
operations hash to** plus `identities.json`, and `TestValidFixtureIdempotencyKeys` pins the Go side
against exactly that corpus. **V-BRK-028 runs the Python builder over the same six files and asserts
the same six keys.** No golden file, no second corpus, nothing to drift: the two implementations are
compared through an artifact that already exists and that the Go test already depends on. The corpus
is not incidental to this choice — it covers a Secret `apply` (the sanitizer), a selector fan-out
delete and a three-operation envelope with mixed verbs (the sort order), which are the three places
a re-implementation actually goes wrong.

**Where it lives.** `agents/<tier>/scripts/action_envelope.py`, byte-identical across the three
tiers — the shape `platform_mcp_server.py` and `agent_common_server.py` already have. The tests go
in **`dev/test_action_envelope.py`**, one copy, parameterised over all three tiers, rather than
three copies under `agents/*/scripts/`: that placement makes the tier-parity assertion free and
picked up by `python3 -m unittest discover dev`, which is already an L0 chain line — so no
`L0-CHAIN.txt` edit is owed. Nothing about tier parity is currently enforced for `agents/*/scripts/`
at all; the three copies of `platform_mcp_server.py` are identical by luck. That is a finding, filed
below, not fixed here.

**`agentIdentity` is the scope string, not the SA username.** `identities.json` reads
`platform/adamparco-kage` and `developer-team/adamparco-kage/gke-scratch-kube-agents-dev/checkout` —
the agent's own scope identity, which the pod knows from its rendered config. The broker's
`Identity.Username` (`system:serviceaccount:…`) is the _authentication_ subject and is a different
string; a builder that used it would compute keys nothing accepts. This is the sort of thing that is
obvious once seen and invisible from the spec text, so it is written down here.

### P9-T8b-2 splits again — the pod knows its tier and not its scope

**Recorded 2026-07-30, at SELECT for T8b-2.** T8b-1's builder refuses to compute a key without an
`agentIdentity`, deliberately: `compute_idempotency_key` raises rather than defaulting, because a
defaulted identity produces a well-formed key for the wrong agent. The obvious next question is
where the pod gets that string, and the survey found it cannot.

`agentIdentity` is `<tier>/<leaf>` — the format is `Identity.AgentIdentity()` in
`k8s-operator/internal/broker/rejection.go`, including its one-armed case for an empty scope. The
broker learns both halves as **startup flags**: `broker_manifests.go` renders
`--tier=agentindex.EffectiveTier(agent)` and `--scope=scope.Of(agent).Leaf()`. The agent pod is
rendered from the same CR a few hundred lines away and gets **`AGENT_TIER` and nothing else** — no
scope leaf, in any env var, in the rendered ConfigMap, or in the golden manifests. So the pod holds
half the identity, and the half it is missing is the one that differs between two agents of the same
tier.

**This is fail-closed, which is why it survived to be found here.** A wrong `agentIdentity` produces
a key the broker's recomputation refuses, so nothing unsafe happens — it is a total outage of the
write path dressed as a per-request 400, and it would land the first time anyone called
`submit_action`. Not an escape from shipped code: nothing imports the builder yet.

| Unit          | What                                                                                                                                                                                 | Checks              |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------- |
| **P9-T8b-2a** | The operator renders the agent's own identity into the agent pod, **joined** to the two flags the broker is started with, so the two cannot drift                                    | **V-BRK-030** (new) |
| **P9-T8b-2b** | The `submit_action` / `plan_action` MCP tools on top of the builder: nonce fetch, mTLS + projected-token transport, `trace`/`requester` from the session, `ActionResponse` rendering | **V-BRK-029**       |

**Why 2a is its own unit and not a line inside 2b.** It is the only part that changes the operator,
which means golden manifests, an envtest run and `Run Controller Tests` — a different substrate from
2b's Python entirely. And it is the half with a _drift_ obligation rather than a behavioural one: the
value has to equal what the broker was started with, forever, and the check that says so has to read
**both rendered manifests from one CR** and compose them through the production
`Identity.AgentIdentity()` rather than restating the `<tier>/<leaf>` format a third time ([[LSN-036]],
[[LSN-041]]). Bundling it under 2b would put that join inside a unit whose failures are all about
TLS and nonces, where a format regression reads as a transport bug.

**Where it goes: `agentBrokerEnvVars`, with the other five.** Not the general env block. Those five
are appended _after_ `mergeEnvVars` specifically so `spec.deployment.env` cannot win against them,
and the identity belongs to the same class: a CR author who could set it could not forge an identity
— the broker derives its own and refuses a mismatch — but they could make **every write from that
agent refused**, which is a denial of service authored in a field that looks like configuration.

---

### P9-T8b-3 splits in two — the skill is Phase 9's, the retirement is Phase 10's

**Recorded 2026-07-30, at SELECT for T8b-3.** The row above says "the `apply-change` skill in all
three tiers, and `submit-suggestion`'s retirement" and flags the whole thing as P10 work. Sizing it
showed the row is two units with opposite phase homes, and that the flag is right about one half and
wrong about the other.

**The skill belongs to Phase 9, because Phase 9's own task list asks for it.** P9-T8 is "the agent's
`apply-change` path submits real envelopes", and acceptance **(a)** is "an envelope flows end-to-end
in shadow mode". T8b-2b shipped the two tools; nothing yet tells an agent they exist, what an
operation looks like, or that it may not claim its own risk class. The soak in T8b-4 has nothing to
soak until that prose exists.

**The retirement belongs to Phase 10, and not for bookkeeping reasons.** 07 §2 phases it as P10-T3,
per tier — "turn shadow mode off for this tier and let the broker execute; wire the `apply-change`
skill (replacing `submit-suggestion` for this tier)". Retiring it in Phase 9 would delete the only
working write path in the product during the one phase whose defining property is that **no agent
holds write authority anywhere**. The replacement runs in dry-run by construction, so the fleet
would be left with a retired GitOps path and a no-op imperative one — every tier unable to change
anything, in a phase 07 §2 requires to be "independently shippable and leaves the system working".
07 §5's rule is the same rule in test form: replaced, never deleted, and swapped for its counterpart
**in the same phase that removes it**. Phase 9 cannot supply the counterpart; that is what Phase 9
_is_.

So `submit-suggestion` stays, and `apply-change` lands beside it. The two coexist for exactly one
phase, which is what a conversion looks like when the ordering rule is obeyed.

| Unit          | What                                                                                                                       | Checks              |
| ------------- | -------------------------------------------------------------------------------------------------------------------------- | ------------------- |
| **P9-T8b-3a** | The `apply-change` skill in all three tiers, alongside `submit-suggestion`, over the tools T8b-2b shipped                  | **V-CTR-020** (new) |
| **P9-T8b-3b** | `submit-suggestion`'s retirement — **deferred to Phase 10 as part of P10-T3**, per tier, as each tier's shadow mode is off | V-GAT-019 is not it |

**The row's check binding was wrong, and that is a finding rather than a renumbering.** T8b-3 cites
**V-GAT-019**, which is _parked-record completeness_ — intent, targets, rendered diff, class, the
gating rule id, and an undo plan or an explicit `undoable: false`. That is a property of a gated
`ActionRecord`, which is why its phase is 10: Phase 9 parks nothing. It says nothing about a skill,
and no skill can make it pass or fail. Nothing in 09 §6 covered the agent's own instructions for
using the write path at all, so **V-CTR-020** is allocated rather than reused — V-CTR is contract
conformance, which is where V-CTR-011's "no brake tool in any agent tool registry or skill manifest"
already lives.

**What is actually checkable about prose, and what is not.** A skill is instructions for an LLM, so
most of it cannot be asserted. Three things can, and each is a join rather than a reading:

- **The tools it tells the agent to call are tools that exist** — the names are read out of
  `platform_mcp_server.py`'s `@mcp.tool()` functions by AST, not listed in the test. A skill naming
  a tool the server does not register sends the agent to a dead call, and the symptom is an agent
  reporting that it cannot act, in prose, with no error anywhere.
- **The parameters it promises are the parameters the tool takes** — read from the same AST. This is
  the drift that actually happens: the signature changes and the prose does not.
- **What it says the agent cannot influence is genuinely absent from the signature** — tier, scope,
  risk class, approval. 02 §2.2 puts this in the persona's own voice ("the agent does not decide its
  own risk level and must never claim to"), and the reason it is worth checking is that the sentence
  is only true while the parameter is missing.

The rest — no `kubectl`/`gcloud`/`git push`/`gh pr create` anywhere in the body — is a grep, and a
weak one on its own. It is included because it is the one property the conversion is _about_: the
old skill's entire body is git and `gh` commands, and a copy-paste that left one behind would be a
mutating shell-out sitting in the instructions of an agent that holds no credential to run it, which
fails confusingly rather than safely.

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

---

### P9-T9 splits: T9a is the trigger, T9b is the gate

**Recorded 2026-07-30, at SELECT.** The recon above says to split the review-gate path filter off
early. Done — **P9-T9a** is that unit and it is closed; **P9-T9b** is the consolidated gate and
everything else the T9 row names. Two arguments for the ordering, and the second is the load-bearing
one:

- The filter needs no cluster, and every day it stays wrong is a day of Go security surface merging
  unreviewed. It has been wrong since PR [#33](https://github.com/adamparco/kube-agents/pull/33).
- **Every commit invalidates P1 for every L2 suite still to come** (see "Notes carried into
  IMPLEMENT"). So all remaining L0 work belongs in front of the remaining L2 work, not interleaved
  with it: land T9a, then build images once, then run T8b-4 and T9b's L2 sections against a tree
  that has stopped moving.

**What T9a asserts, and why it is a derivation rather than a longer list.** Widening the filter by
hand fixes today and rots tomorrow — the filter was correct when it was written, and what changed
was the repository, not the YAML. So the security surface is **derived** from three sources that
are each maintained for their own reasons: Go files carrying a `+kubebuilder:rbac`/`:webhook`
marker, Go files that build a `tls.Config` or issue a `TokenReview`/`SubjectAccessReview`, and
manifests declaring an authority-granting kind. 65 files today. A new package that authenticates
anything, or a new directory holding a ClusterRole, joins the required set the moment it is written
and the check goes red until the filter reaches it.

**The check is one-directional and says so.** It can prove a glob is missing; it cannot prove one is
unnecessary, and `- "**"` would satisfy it. That is deliberate: over-triggering costs CI minutes and
under-triggering costs a review, so the rule only pushes in the direction where being wrong is
cheap. Two globs in the widened filter (`k8s-operator/api/**`, `deploy/**`) are judgement rather
than derivation, and are marked as such in the workflow so the next reader does not mistake the list
for something wholly generated.

**The matcher is calibrated against GitHub, not against a reading of GitHub.** The whole answer
turns on one clause — whether a leading `**/` may match zero directories, which decides whether
`**/agents/**` ever reached `agents/platform/SOUL.md`. Rather than assert the documentation, the
test replays two runs that happened: PR #33, where the gate did not fire, and PR
[#79](https://github.com/adamparco/kube-agents/pull/79), where it did, both against the filter as it
stood at the time. If the matcher's reading of `**` were wrong in either direction, one of those
flips.

**Found by a gate, not by a reader.** The first version of the check enumerated the repo with a bare
`git ls-files`, and `invariants-gate.py` refused it on the spot under [[LSN-050]]: `ls-files` without
`--others` lists the index, so the check would have been blind to precisely the new, never-reviewed
file it exists to find — the same defect it is about, one level up. Now `gitcorpus.repo_files`.

**Split this off early: the review-gate path filter is self-contained and needs no cluster.**
`.github/workflows/review-gate.yml:11-20` still matches nothing under `k8s-operator/internal/**`. It
is V-MET-007 — the one check ID the T9 row names explicitly — it does not depend on the gate work,
and it is the reason the security gate never ran on the broker. Doing it inside the gate unit means
doing it late; doing it in its own unit means it stops being true sooner. It still must not be done
in a unit that would be reviewing itself.

---

### P9-T9b splits into five: T9b-1 … T9b-5

**Recorded 2026-07-30, at SELECT, under `harness-run` §2 sizing.** T9b as written is not one unit.
Its recon says "None of the five deliverables exist"; since then guard 1 has landed as
**V-CTN-037**, and `broker-auth-l2.sh` with `fixtures/actor-tenant-grant.yaml` has landed too, so
the residual is smaller than the row but still five separable pieces of work with different levels,
different preconditions, and no dependency between the first three.

Split, in the order they will be done. **The ordering is the phase's own rule, not a preference:**
every commit invalidates P1 for every L2 suite still to come, so all remaining L0/L1 work goes in
front of all remaining L2 work.

| Unit      | What it is                                                                                                                                                     | Checks                                            | Level | Blocker                       |
| --------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------- | ----- | ----------------------------- |
| **T9b-1** | The `ActionRecord` phase lifecycle as enforced data, at the journal's two write points                                                                         | V-CTR-006                                         | L1    | —                             |
| **T9b-2** | Envelope schema round-trip; refused keys ignored or rejected, never honoured (06 §4.1, `¬`)                                                                    | V-CTR-005                                         | L1    | —                             |
| **T9b-3** | Broker supply-chain minimality: no LLM SDK, plugin loader, interpreter or shell in the SBOM; no `/bin/sh` in the image; one listening socket; two mounts (`¬`) | V-RUN-010                                         | L0    | —                             |
| **T9b-4** | `dev/verify/verify-phase9.sh`, its `dev/L0-CHAIN.txt` / `dev/L2-CHAIN.txt` lines, and the V-BRK-021 L0-vs-L2 evidence reconciliation                           | (the gate itself)                                 | L0    | T9b-1..3 landed               |
| **T9b-5** | `broker-execute-l2.sh`, `actor-grant-sweep-l2.sh`, the tenant overlay's **write** half and the admission ruling it needs                                       | V-BRK-006/018/019, V-REV-002, V-REV-003 (L2 half) | L2    | P1 — images built after T9b-4 |

T9b-5 is also what unblocks **P9-T8b-4b-ii-2b-ii** (the envelope corpus soak, journal mining, guard
3 as a label assertion per [[LSN-045]], and V-REV-001 at L2), which is why that unit sits behind it
rather than beside it.

**The denominator moves by four**, not by five: T9b was already counted once.

---

### P9-T9b-1 — outcome, 2026-07-30

**V-CTR-006** — "`ActionRecord` lifecycle: every legal transition succeeds, every illegal one is
rejected (06 §4.3)". The check was scheduled as test-writing over an existing rule. There was no
rule. Two defects in shipped code, both found by trying to write the check:

1. **Nothing enforced the lifecycle anywhere.** `journal.Store.SetPhase` accepted `Verified →
Pending`, `Rejected → Executing`, and `"" → Undone`. The lifecycle existed as an ASCII diagram in
   a doc comment on `ActionPhase`, which is a statement of intent rather than a property of the
   system. 06 §4.3's status-RBAC table then rests on that lifecycle — the ChatOps gateway is
   permitted `PendingApproval → Pending/Rejected` **and nothing else**, the undo controller `→
Undone` **only** — so both rows were unenforceable in the direction admission cannot cover: not
   "who may write the field" but "what may the field become".
2. **`status.phase` was never populated at creation.** `status` is a subresource, so `client.Create`
   sent the block and the API server dropped it; only `Labels[kube-agents/status]` landed. Every
   record read back `status.phase: ""` while its label named a phase — the exact inversion of 06
   §4.3, which makes the field authoritative and the label a derived index. A parked record
   therefore had no `PendingApproval` for the gateway's one permitted transition to leave from.
   `rejection.go:156-158` carries a comment asserting "the status subresource is set by the
   reconciler"; no such reconciler exists.

**The ruling the table needed, which is not a halt.** 06 §4.3's diagram draws `Failed ──▶
RolledBack`. The same section's phase table marks `Failed` terminal, and `verify/driver.go`
implements the table: 04 §5.1 rung 3 succeeding writes `RolledBack`, rung 5 (rollback itself failed)
writes `Failed` and pages. Rather than pick between a picture and a column, the edge was settled
from the spec's own **principal list**: the four writers of `status.phase` are the owning broker,
the undo controller (`→ Undone` only), the ChatOps gateway, and the exporter (which deliberately
cannot touch `phase`). **No principal can write `Failed → RolledBack`.** That is an
invariant-preserving resolution derived from the finer of two statements in the same section, so it
is a decision, not a §8.5 contradiction. `Verified → Undone` survives the same test for the opposite
reason: "terminal" is a claim about the broker's pipeline stopping, and `Undone` is a different
principal, later. Both arguments live in `actionrecord_phases.go`'s file comment and in the ledger's
decisions table.

**The check is a closed truth table, not a list of remembered edges.** 121 ordered pairs (ten phases
plus the empty from-phase), with the expected answer transcribed from 06 §4.3 a **second** time
rather than read out of the production map — a test that iterates the map to decide what to expect
asserts that the map equals itself, and stays green through deleting every entry. Vacuity guards pin
27 legal cells and 94 refused. Alongside it: the CRD enum is read out of `actionrecord_types.go` as
data and cross-joined against the table in both directions; reachability is a real BFS from the
creation set, so orphaning a phase fails; and `Successors()` is asserted to hand back a copy.

**The escape the sweep found, and what it says about the fake client.** The first sweep ran 11/12.
`M10` — restoring defect (2) exactly — survived. The reason is that **controller-runtime's fake
client does not model the status subresource on `Create`**: `withStatusSubresource` is consulted in
`tracker.update` and not in `fakeClient.Create`, so the fake keeps a status block that every real
API server discards. The test asserting `status.phase` came back was green because the fake never
dropped it, and would have stayed green against a `Create` that wrote no status at all. That is the
mechanism by which the defect survived five phases under a green suite. Fixed at the helper rather
than in the one test that noticed: `newFakeStore` now installs a `Create` interceptor that zeroes
`Status` before delegating, so the whole package is measured against the cluster it will run on.
Second sweep: **12/12 caught**.

**Findings filed, not fixed.** (a) The ASCII lifecycle diagram in `06-api-and-data-contracts.md`,
reproduced verbatim in `actionrecord_types.go`, still draws `Failed ──▶ RolledBack` and still says
DryRun is "reached from Pending" — a spec-art correction for the next improvement pass, not a
behaviour change. (b) `rejection.go:156-158`'s "the status subresource is set by the reconciler" is
now moot for `phase` and the sentence is still wrong. (c) A candidate gate rule with a wider blast
radius than either: **a fake-client helper that models a subresource on `Update` and not on
`Create` is a suite that cannot see its own most likely defect** — every `WithStatusSubresource`
call site in this repository is a candidate, and none of the others has been audited.

Evidence: **V-CTR-006 (L1) pass** — `verification/results.csv`, `verification/mutants/V-CTR-006.json`
at 12/12.

---

### P9-T8b-4 splits: 4a is the deployment path, 4b is the soak

**Recorded 2026-07-30, at SELECT.** T8b-4 is "the L2 shadow soak with journal mining". Surveying
what the soak needs before starting it turned up a defect in the shipped system, not a gap in the
test scaffolding, and the defect has to be fixed before any L2 broker claim can be made at all:

- `pod_launcher.go:168` renders a **broker Deployment for every `Agent` CR**, and the `PodLauncher`
  interface deliberately offers no way to ask for just the agent half.
- The broker's image comes from `brokerImage()`, which reads `KUBEAGENTS_BROKER_IMAGE` off the
  controller's own Deployment and otherwise falls back to
  `ghcr.io/gke-labs/kube-agents/kage-broker:v0.1.0`.
- **`KUBEAGENTS_BROKER_IMAGE` is set nowhere in this repository** — not in `config/manager/`, not in
  the provisioning path, not in `reload-images.sh`. Checked, not assumed.
- That GHCR tag is one of the four the **V-CMP-002** deferral measured as unpullable (the other
  three answer 403; `platform-agent` answers 404).

So every `Agent` CR on every cluster renders a broker Deployment whose pod cannot pull. That is 09
§11.9 — built, never wired — in the component that holds the actor credential, and
`reload-images.sh` says so about itself in its own header: the `broker` target "repoints NOTHING …
Once P9-T7 lands this grows a `deploy_broker`". P9-T7 landed four units ago. The note aged into a
defect.

**The split.** 4a is the deployment path and the L2 claim that path makes true; 4b is the soak.

| Unit          | What                                                                                                                                                                                     | Checks             | Blocked on                           |
| ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------ | ------------------------------------ |
| **P9-T8b-4a** | `deploy_broker`; the `kage-broker` P1 mapping; the actor identity as a dev fixture rendered by the shipped renderer; `dev/verify/broker-per-agent-l2.sh` and its `dev/L2-CHAIN.txt` line | **V-BRK-012 (L2)** | a live scratch cluster               |
| **P9-T8b-4b** | The shadow soak proper: drive envelopes at the deployed broker and mine the journal — **split again into 4b-i and 4b-ii below**                                                          | V-REV-001 (L2)     | T8b-4a — nothing to drive until then |

**Why V-BRK-012 and not a new ID.** 09 §6.2 gives V-BRK-012 as `L0, L2` and `verification/results.csv`
records only the L0 row (2026-07-28, P9-T7b), whose own note ends "the lint reads source, so it says
nothing about a _deployed_ fleet — that is the `L2` half, P9-T9's". The L2 half is the open half, it
is BLOCKING-ALWAYS with a mandatory `¬`, and it is exactly the claim a working deployment path makes
checkable: one broker per CR, owned by that CR, on the digest under test, with a Service whose
endpoints resolve to its own broker pod and nobody else's. Adding an ID for "the broker deploys"
would be a second name for the same property (V-MET-013), so the row moves from T9b to here.

**LSN-015 is honoured by the fixture, not by a note.** The two shipped manifests
`examples/gitops-repo/fleet/platform-agent.yaml` and
`clusters/cluster-a/agents/agent.yaml` both live in `kubeagents-system` — a platform broker and a
cluster-admin broker co-located, which is 08 §2.6's shape and the only arrangement in which "the
Service selector pins `agent:` as well as `role:`" can fail. A one-CR fixture cannot fail it. They
are seeded through `seed_parent_agent`, so they are the shipped manifests and not this suite's
paraphrase of them (LSN-024).

**Three consequences for P1, all of which are work in 4a.**

1. `_p1_build_inputs` maps `k8s-operator` and `kage-router` and **returns 1 for everything else**,
   so P1 against a broker pod answers state 3 — could not verify — and a broker suite that mapped 3
   to a pass would be certifying whatever image happened to be running. `kage-broker` builds from
   the same `k8s-operator/` context and gains the mapping here.
2. The freshness half compares the deployed tag against `git rev-parse --short HEAD`, so the tree
   must be committed before the Cloud Build and must not move until the L2 run is over. Same
   discipline as T9a's ordering argument, one level tighter: this unit's own ledger commit is taken
   **after** the run, not between the build and it.
3. The broker image reaches the pod through the **controller's** environment, so P1 has two subjects
   here and both are asserted: the controller pod (which chose the image) and the broker pod (which
   is running it). A cluster where those two disagree is one where the rendered Deployment is a
   generation behind the env var, and every claim below it would be about the previous build.

---

### P9-T8b-4b splits again: 4b-i is a caller that can get in, 4b-ii is the soak

**Recorded 2026-07-30, at SELECT.** 4a made the broker _exist_ on a cluster. Sizing 4b — "drive
envelopes at the deployed broker and mine the journal" — turned up that the sentence hides two
different units, and the first one is not the soak:

- **Nothing in `dev/` can speak to a broker.** Not the shell, not a probe, not a fixture. The broker
  requires mutual TLS with a certificate this cluster's `kubeagents-mesh-ca` signed, a projected
  ServiceAccount token carrying audience `kubeagents-broker`, and the two bound to each other
  through a SPIFFE URI. `broker-per-agent-l2.sh` deliberately said so: it proves each broker pod
  _runs_, and states as a non-claim that it does not prove the broker _serves_, "because no client
  here holds a certificate".
- **The obvious shortcut does not work, and the reason is a finding.** Driving the shipped
  `broker_client.py` from the working tree over `kubectl port-forward` looked viable because the
  module's own docstring says "The server certificate is verified against `KUBEAGENTS_BROKER_SAN`,
  not against the host in the URL." It is not. `cfg.san` is read from the environment, required by
  `BrokerConfig.require()`, and then **never used again** — `build_ssl_context` sets
  `check_hostname = True` and `urllib` derives `server_hostname` from the URL, so the name actually
  verified is the endpoint's host. The two strings are equal today, so nothing is broken; the
  docstring describes an intent no code implements. Filed as a finding, not fixed here — fixing it
  is a change to shipped agent code and belongs in its own unit.
- So the caller has to be **in the cluster**, which is the honest arrangement anyway: it goes
  through the real Service, the real `<agent>-broker-ingress` NetworkPolicy and the real
  `<agent>-to-broker` egress hop, none of which a port-forward touches.

**What that unlocks is bigger than the soak.** Five checks in 09 §6.2 are the transport, every one
of them `L2`, phase 9, BLOCKING-ALWAYS, and carrying the mandatory `¬` — and **not one has a single
row in `verification/results.csv`**:

| ID            | Property                                                                                  | State before 4b-i |
| ------------- | ----------------------------------------------------------------------------------------- | ----------------- |
| **V-BRK-007** | mTLS is required — a plaintext or wrong-CA client is refused ¬                            | no evidence       |
| **V-BRK-008** | The projected token must carry audience `kubeagents-broker` ¬                             | no evidence       |
| **V-BRK-009** | Neither layer alone suffices ¬                                                            | no evidence       |
| **V-BRK-010** | A foreign agent's reader SA is refused **and raises a security event** ¬                  | no evidence       |
| **V-BRK-017** | The default-audience token is refused — `TokenReview` says `authenticated: true` for it ¬ | no evidence       |

They are the whole of acceptance bullet (c)'s L2 half, they were scheduled onto P9-T2 and never
gathered, and every one of them is a question about a **credential presented to a running broker**.
A driver that can present one answers all five; the soak needs the same driver and nothing more.

**The split.**

| Unit             | What                                                                                                               | Checks                                                                | Blocked on |
| ---------------- | ------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------- | ---------- |
| **P9-T8b-4b-i**  | The in-cluster envelope driver — a real reader identity at the broker's door — and `dev/verify/broker-auth-l2.sh`  | **V-BRK-007 · V-BRK-008 · V-BRK-009 · V-BRK-010 · V-BRK-017**, all L2 | T8b-4a     |
| **P9-T8b-4b-ii** | The shadow soak proper: a corpus of envelopes through the driver, then journal mining over the `DryRun` population | **V-REV-001 (L2)**                                                    | 4b-i       |

> **Split again — see "P9-T8b-4b-ii splits" below.** 4b-ii-1 types step 3's refusals (V-BRK-031);
> 4b-ii-2 is the soak, which needs the read-only tenant overlay before its population is non-empty.

**Why the driver is a pod and what that costs.** The three agent images in Artifact Registry are
stale (2026-07-24 to 2026-07-27) and build `FROM nousresearch/hermes-agent`, so rebuilding them to
get a Python interpreter next to the shipped scripts is a slow build for a fixture. Instead the pod
runs a stock `python:3.12-slim` and the **shipped** `broker_client.py` and `action_envelope.py`
arrive in a ConfigMap generated from the working tree — so the code under test is byte-for-byte the
file this repo ships, and only the interpreter around it is a fixture. Its environment is read off
the **rendered agent Deployment** (P6), never reconstructed from the naming functions, so a driver
pointed at the wrong endpoint is a driver that fails rather than one that quietly proves nothing.

**Name resolution is short-circuited, deliberately, and it is a non-claim.** The pod carries
`hostAliases` mapping the broker's SAN to the broker Service's ClusterIP. `<agent>-to-broker` makes
every reader-labelled pod default-deny on egress and opens exactly one hole — TCP 8443 to the actor
half of its own pair — with no DNS rule anywhere in it (the real agent pod gets DNS from the
per-tier egress policy, which is an install-time artifact this cluster does not carry). Resolving
the name locally means the driver reaches the broker through **precisely** the allowance the pair
policy grants and nothing else, which is a sharper demonstration than a working DNS lookup would
be. What it does not demonstrate is that cluster DNS publishes that name; that is
`broker-per-agent-l2.sh`'s L2-3, which reads the Endpoints the API server computed.

**A second finding, filed while writing the probe: `session_trace()` can build an envelope the
broker must refuse.** `broker_client.py:367` adds `parentSpanId` to the trace whenever `SPAN_ID` is
set in the agent's environment. `broker.Trace` has `traceId`, `spanId`, `sessionId` and `threadId`
and no such field, and `DecodeEnvelope` runs `dec.DisallowUnknownFields()` — so an agent in a traced
session has **every** mutation refused with a 400 `unknown-field`, and the refusal names a trace
field rather than anything the agent did. Nothing in the repo sets `SPAN_ID` today, so it is latent;
it is also exactly the shape of defect that only appears once tracing is wired, at which point it
looks like the broker rejecting everything. Not fixed here: it is one line in shipped agent code
that all three tiers carry byte-identically, so the fix is a `dev/test_agent_script_parity.py`-scoped
edit plus a test asserting the client's trace keys are a subset of `broker.Trace`'s JSON tags — the
mirror image of the `RESPONSE_FIELDS` assertion `dev/test_broker_client.py` already makes about the
reply, which is a check that exists in one direction only and is why this was never caught.
**Scheduled as P9-T8b-4c**, with its check ID to be assigned by that unit against 09 §6.2 rather
than guessed here. The driver pod leaves
`SPAN_ID` unset and says so in a comment, because setting it would be measuring the bug from the
fixture that discovered it.

### P9-T8b-4b-i — outcome, 2026-07-30

**Green: 14 PASS / 0 FAIL, rc 0, three consecutive runs.** All five rows now have their first
`verification/results.csv` entry. The `¬` is `broker-auth-l2.sh --negative-control` — three
transcripts of a misbehaving broker replayed through the identical assertion block, 8 of 8
credential arms red on all three — and it addresses no cluster, so it is a line in `dev/L0-CHAIN.txt`
rather than an L2-only ceremony.

**Five things the first run found, all of them in the fixture or in the spec, none in the broker.**
The broker was correct on every scenario from the first request it ever served.

1. The probe died at the plaintext scenario. `exc.read()` inside the `HTTPError` handler raised
   `ConnectionResetError`, which escaped and took four scenarios with it. Bodies are now read
   through a helper that cannot raise and reports the unreadable body as itself.
2. `trigger.source: "verification"` is not in the Go closed set of seven, so the baseline envelope
   was a 400. Fixed to `cron`; the 400 arm is now a loud failure rather than something tolerated,
   because it means the envelope never reached the pipeline and 4b-ii's soak would be built on the
   assumption that it did.
3. Plaintext is not a transport error. `net/http`'s TLS listener answers with a bare `400` before any
   handler — so the arm asserts _no handler answered_ (no `reason` field), not _no answer_.
4. The audience arm was red for 200 characters of display truncation. See the V-BRK-008 row.
5. `V-BRK-017`'s stated mechanism does not happen against a real API server. See the V-BRK-017 row.

**Findings filed, not fixed — none of them this unit's.**

- **An RBAC denial inside the pipeline surfaces as a 500 `internal-error` with a stack trace.** The
  baseline envelope is authenticated, decoded and classified, and then step 3 fails:
  `configmaps "..." is forbidden: User "system:serviceaccount:kubeagents-system:platform-<scope>-actor"
cannot get resource "configmaps"`. That is an entirely expected, caller-visible condition in dark
  mode — the actor is bound to no tenant authority by design — and `server.go`'s `refuse`/`write`
  have no typed `*Refusal` for it, so it falls through to the unclassified arm. A caller cannot tell
  a permission boundary from a broker bug. **P9-T8b-4b-ii cannot be built on this** and it is that
  unit's first order of business: either the pre-state snapshot's `Forbidden` becomes a typed
  refusal, or the soak fixture grants the platform actor read on its own namespace, and the
  distinction between those two is a real design question rather than a fixture detail.
- **`invariants-gate.py`'s LSN-005 check reads the FIRST `case "$CTX" in` in a file, including one
  inside a comment.** Found by walking into it: this suite briefly wrote its guard as
  `case "$MODE:$CTX"`, which is equally correct and which the gate cannot parse, and the comment
  explaining the fix then contained the literal idiom and shadowed the real guard below it. The
  false-positive direction cost ten minutes. The **false-negative** direction is the one that
  matters and it is live: a script whose comment shows a well-formed anchored guard and whose actual
  guard is a substring match would pass, which is LSN-005 itself, wearing a comment. For
  `harness-improve`.
- **Three `V-*` rows overlap and none of them says so.** `V-BRK-008` and `V-BRK-017` state the same
  property under two IDs; 09 §6 gives the plaintext arm to **both** `V-BRK-007` ("a plaintext or
  wrong-CA client is refused") and `V-BRK-009` ("valid token over plaintext"). Shared evidence is
  recorded as shared in the results rows rather than double-counted. Retire-never-delete applies, so
  this is §3.4 pruning work, not something to act on mid-unit.
- **`V-BRK-017`'s 09 §6 wording needs to say which level owns which clause** — the mechanism it
  names is unreachable at L2 by construction. Written out in full in its results row.

**One defect this unit introduced and then mechanized.** Extracting the eight credential assertions
into a function left the call site unwritten for exactly one commit. The suite ran, printed six green
lines, printed `PROVEN: V-BRK-007 · V-BRK-008 · V-BRK-009 · V-BRK-010 · V-BRK-017 at L2`, and exited
0 — having asserted none of them. Nothing could have caught it: `fail` stays 0 when no assertion
runs, and the `¬` mode calls the extracted function directly, so it was green too. The suite now
counts its own arms and fails the run if the count disagrees with `EXPECTED_ASSERTIONS`; that guard
was itself verified by temporarily setting it to 15 and watching the run go red. **A suite that
reports a verdict it did not compute is worse than a suite that fails**, and this is a general shape
— worth taking to `harness-improve` as a candidate rule for every `dev/verify/*.sh`, not just this
one.

### P9-T8b-4b-ii splits: 4b-ii-1 is a typed refusal, 4b-ii-2 is the soak

4b-i's first filed finding said the 500-on-RBAC-denial was 4b-ii's **first order of business, and
that the choice between typing the refusal and granting the actor a read was a real design question
rather than a fixture detail**. Investigating it answered the question and also showed that 4b-ii
was two units, not one.

**The design ruling: type the refusal. Both, in fact, but this one first and on its own.**

`k8s-operator/scripts/agent-identity.yaml.template` is unambiguous — _"Phase 9's actor authority is
the broker-operations grant and nothing else, and binding a cloud-write credential to it now would
hand the actor months of authority ahead of the controls that are supposed to bound it (P10-T1 owns
that)."_ So the shipped actor genuinely cannot read a tenant ConfigMap, and 06 §4.4's fail-closed
table already has the row: **cannot persist a pre-state snapshot → refuse that envelope.** What the
table does not say is _which_ refusal, and there turn out to be two, distinguished by whether
retrying could ever help. That is the whole of V-BRK-031.

Three things made it worse than a cosmetic status code, and the third is the one that decided it:

1. `ReasonSnapshotFailed` already existed and was wired only for the **persist** half at brake row
   4, never for the **capture** half at step 3. Half a reason code, in production, for a phase.
2. `Refusal.RetryAfterSeconds`'s own comment says _"zero means do not retry, which is the right
   answer for every schema and authorization refusal"_ — and this was the site not honouring it. A
   fleet told to wait sixty seconds and try again spends the rest of the phase retrying a permission
   boundary.
3. **`Journal` and `SecurityEvent` are fields ON the `Refusal`.** With no `Refusal`, there is no
   journal entry and no event: the envelope's disposition was recorded **nowhere**. An agent
   enumerating what it may touch left no trace at all, which is the exact opposite of what 06 §4.1's
   per-reason table exists to guarantee, and it is what makes a probing pattern findable afterwards.

The forbidden arm therefore journals and deliberately does **not** alarm. 03 §6's security events
are for identity violations — a caller that is not who it says it is — and `forbidden-caller`
(V-BRK-010) remains that case. This is an authorization outcome for a correctly authenticated
caller, and in shadow mode it fires on **every single action**, so an alarm here is an alarm that
gets muted, at a cost paid by the events that do matter.

**Why the soak could not simply follow in the same unit.** Because the same reading kills the naive
soak too. With the shipped grant, no envelope targeting a tenant resource reaches step 4 — so the
`DryRun` population is **empty**, and this file's own argument against a vacuous V-REV-001 (_"a check
over an empty population is a check that cannot fail (V-MET-014), and shipping one is worse than
shipping none"_) applies to the shadow instance exactly as it does to the executed one. The soak
needs planning defect 2's **read-only** tenant overlay, which does not exist yet. That overlay is
not a security weakening — invariant 7's mechanized allow-list is `get`/`list`/`watch`, and read
verbs are explicitly _not_ authority; it is the **write** half, owned by P9-T9b, that needs all
three guards.

| Unit               | What                                                                                                                                         | Checks                | Blocked on |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------- | --------------------- | ---------- |
| **P9-T8b-4b-ii-1** | Step 3's three live reads answer a typed `*Refusal`, split by whether retrying can help                                                      | **V-BRK-031** (L1+L2) | 4b-i       |
| **P9-T8b-4b-ii-2** | The read-only tenant overlay `dev/verify/fixtures/actor-tenant-grant.yaml`, planning defect 2's guard 1, the envelope corpus, journal mining | **V-REV-001** (L2)    | 4b-ii-1    |

4b-ii-1's L2 evidence is free: `broker-auth-l2.sh` already drives an envelope past authentication
into step 3 with the shipped actor, which _is_ the condition. Its 5xx arm was already reporting it.

### P9-T8b-4b-ii-1 — outcome, 2026-07-30

**Green at both levels.** L1: the 3 × 3 table over `{pre-state snapshot, restart baseline,
live-state resolve} × {Forbidden, Unauthorized, transient}` plus the negative control, in
`internal/broker/pipeline/pipeline_test.go`. L2: `broker-auth-l2.sh` at 14 PASS / 0 FAIL, rc 0,
where the **real** API server denied the **real** actor and the broker answered
`403 target-forbidden`, `retryAfterSeconds: 0` — where the run three days ago answered
`500 internal-error`.

**The `¬` is on the discrimination, not on the happy arm**, and that is the point. A check asserting
only "step 3 produces a refusal" passes on an implementation that answers 403 for everything, which
is precisely how this gets written wrong — the RBAC denial is what anyone debugging it sees. So
`TestLiveReadRefusalDiscriminatesRatherThanDefaulting` asserts against `liveReadRefusal` directly
that the two classes differ in reason, differ in status, and that exactly one is retryable.
`verification/mutants/V-BRK-031.json` scores **8/8 caught**, and its rows are chosen so that six of
the eight produce a perfectly well-formed `*Refusal` that says the wrong thing — a check that only
looked for "not a 500" would be green on every one of them.

**A `NotFound` is not in the table on purpose.** `CaptureAll` swallows it as a create's legal empty
pre-state and `CaptureRestartBaselines` baselines it at zero; the rig's default reader is
`absent: true`, so every other test in that file already runs that path. Restating it here would be
a case this helper does not own.

**The L2-0 arm now discriminates on the reason, not the status.** It read
`401 | 403) bad "refused at the AUTH layer"`, which was correct while `forbidden-caller` was the only
403 the actions route could produce. There are now two, and they are opposites: one is the
authenticator saying _this caller is not this broker's agent_, the other is the broker's own actor
identity hitting its ceiling. Leaving the arm alone would have turned a green suite red on a broker
doing exactly the right thing (halt condition 2). Guardrail 9 does not bite — the arm was
**passing** the 500 with a NOTE, so this is not a check edited to let a failing implementation
through — and the amendment is a strict **narrowing**: every reason that failed before still fails,
one previously-impossible reason is now named, and five new `¬` rows in the L0-runnable
`--negative-control` mode pin it, including _"a genuine auth refusal is still a failure"_ so the
narrowing cannot swallow the case the arm originally existed for. Its 5xx arm also stops
passing-with-a-note: the defect it was tolerating is closed, and tolerating it again would mean the
arm can no longer tell the tracked defect from a new one.

**Findings filed, not fixed.**

- **The scratch cluster's actor is `platform-your-gcp-project-id-actor`.** The scope leaf is the
  literal placeholder string from `vars.sh`, and it has been baked into a real ServiceAccount name,
  a real RoleBinding, and every RBAC denial message the broker logs. Harmless on scratch and wrong
  everywhere: the identity a write is attributed to is derived from it, so an install that never
  edited `vars.sh` would attribute every action to `your-gcp-project-id`. Nothing validates that a
  scope leaf is not a template default. A candidate gate rule for `harness-improve`, and it belongs
  with the existing `<scope>`-segment ambiguity already open in the ledger.
- **The probe's 400-character detail cap truncated the denial mid-namespace-name.** This is the same
  shape as 4b-i's finding 4, where a 200-character display cap cut the substring an assertion was
  reading. It is not load-bearing _yet_ — V-BRK-031's assertions read the reason, the status and the
  retry, none of which are in the detail — but the projector already caps at 1000 and the probe caps
  again at 400, so there are two caps and only one of them is documented as dangerous. For
  `harness-improve`, with 4b-i's finding.

### P9-T8b-4b-ii-2 splits: 2a is the overlay and its lint, 2b is the soak

Oversized on inspection, so split per `harness-run` §2. **2a** — the read-only tenant overlay and
planning defect 2's guard 1 — is hermetic L0 and depends on no cluster. **2b** — the envelope
corpus, journal mining and V-REV-001 at L2 — is a new L2 suite with a corpus and a `¬` mode. The
order follows this phase's own recorded rule that all remaining L0 work goes in front of the
remaining L2 work, so the images are built once against a tree that has stopped moving (every commit
invalidates P1).

| Unit                | What                                                                                        | Checks             | Blocked on |
| ------------------- | ------------------------------------------------------------------------------------------- | ------------------ | ---------- |
| **P9-T8b-4b-ii-2a** | The read-only tenant overlay, its applier, and the lint that confines it to `dev/`          | **V-CTN-037** (L0) | 4b-ii-1    |
| **P9-T8b-4b-ii-2b** | The envelope corpus soak over the overlay, journal mining, and guard 3 as a label assertion | **V-REV-001** (L2) | 2a         |

### P9-T8b-4b-ii-2a — outcome, 2026-07-30

**Green.** `dev/verify/fixtures/actor-tenant-grant.yaml` grants the deployed actor `get`/`list`/
`watch` on six workload kinds in one tenant namespace; `dev/lib/actor-overlay.sh` renders it,
applies it, and does not return until the API server's own authorizer agrees — and, in the same
breath, that the actor still cannot write there and still cannot read `kube-system`.
`check_test_only_grants_are_confined` (**V-CTN-037**, new in `invariants-gate.py`, 22/22 green) is
guard 1, with 12 negative controls in `dev/test_invariants_gate.py`.

**The labels were the whole design, and reading `vap-agent-readonly.yaml` settled them.** The
overlay carries `kube-agents/tier: platform` and deliberately **not** `kube-agents/role: actor`:

- `kube-agents/role: actor` is V-BRK-013's discovery key, and that check asserts every object
  wearing it equals 06 §2.2.1's twenty triples **exactly**. A fixture wearing it turns a green
  BLOCKING-ALWAYS check red — correctly — and the one-line green is an exception in the check.
- `kube-agents/tier` is `is-agent-rbac`'s predicate, so the overlay's read-onlyness is enforced at
  admission by the shipped policy rather than merely asserted by the file granting it. It is also
  invariant 7's predicate, which puts the overlay **inside** that invariant's population rather than
  beside it. Read verbs are explicitly not authority there, so nothing is weakened.

**A design question for T9b, surfaced here and not answerable here.** A **write** overlay cannot
wear `kube-agents/tier` — validation 1 denies it — and without the label it is governed by no
admission rule at all: `vap-agent-scope` does not exist until P10-T1, and `vap-agent-readonly`'s
`matchConstraints` cover `roles`/`clusterroles` but not `rolebindings`. T9b has to rule. Until it
does, guard 1 is the only thing between a test fixture and a real over-grant, which is exactly the
weight planning defect 2 assigned it.

**Guard 3 costs nothing because teardown never deletes a namespace.** The applier creates-or-reuses
the tenant namespace and `actor_overlay_revoke` deletes only the Role and the RoleBinding — revoking
the authority, which is the point. No script deletes a namespace, so `cluster-check-hygiene.py`
property 2 ([[LSN-045]]) is never engaged. Guard 3 becomes a label assertion in 2b.

**The lint is derived from a marker, and its scope limit is stated rather than silent.** Discovery
is by `kube-agents/test-only-grant`, never by a path — a rule keyed to one filename is a headcount
of one ([[LSN-036]]). Heredoc RBAC inside a `dev/**.sh` is **out of scope and said so in the
docstring**: a heredoc's disposition is not statically derivable, and `negative-attenuation.sh`
applies a ClusterRole granting `impersonate` on purpose, as an adversarial input proving the VAP
rejects it. Marking that would be a lie and exempting it by helper name would be an enumeration. The
consequence runs the other way too — the rule is enforceable on files and not on heredocs, so the
fixture was made a **file** in order to be inside it.

**`dev/test_review_gate_paths.py` caught the fixture on the first full chain run.** A file that
decides authority and does not trigger the security review gate is exactly what V-MET-007 derives,
and it named the new fixture within seconds of it existing. A `dev/verify/fixtures/**` glob — a
directory, not the filename, because nothing outside `dev/` may name such a file — was added to
`review-gate.yml`. This is the harness catching the harness, and it is worth recording as a
non-defect.

**Then the new check caught its own evidence row.** The first `verification/results.csv` row written
for V-CTN-037 quoted the marker verbatim and named the fixture by basename, and P1 and P3 both fired
on it: a CSV outside `dev/` is not prose, and the check does not know the difference between a
record of a grant and a path that applies one. The temptation was to add `verification/` to the
allow-list beside `.md` — which is a check edit in the unit that authored the check, and a widening
of exactly the kind [[LSN-036]] warns about, since every future evidence row would inherit the hole.
The row was reworded instead: it describes the marker rather than spelling it and points at
`dev/verify/fixtures/` rather than the filename. Cost: one sentence. The standing rule that follows
is worth more than the row — **evidence about a test-only grant describes it, it does not quote it**,
and the check enforces that for free.

**Findings filed, not fixed.**

- **`dev/assertion-baseline.json` is stale by roughly 1 000 assertions.** The committed baseline
  holds **34 files / 194 named tests**; the tree today yields **131 / 1 209**. The ratchet
  (V-MET-003, BLOCKING-ALWAYS) therefore has a floor 84 % below the actual assertion count and would
  not notice a thousand assertions being deleted. It is passing — `inventory() ⊇ baseline` — which
  is why nothing has said so. This unit regenerated it, saw the size of the jump, and **reverted**:
  raising a security ratchet by 1 015 names is a review event that deserves its own commit and its
  own reasoning, not a ride-along in a fixture unit. The ratchet is no weaker than it was this
  morning. For `harness-improve`, and it is the highest-value item on that list.
- **The heredoc half of V-CTN-037.** Closing it needs a way to tell an applied grant from an
  adversarial input. Two scripts already grant that way: `brake-fanout-l2.sh` applies and keeps a
  Role with `create`/`patch`/`update` on ActionRecords, and `negative-attenuation.sh` applies four
  documents of which three are supposed to be denied. For `harness-improve`.
- **`actionlint` is not installed on this host**, so the `review-gate.yml` edit was checked by
  prettier and by `dev/test_review_gate_paths.py` (which parses the workflow and reads
  `on.pull_request.paths`) rather than by the linter CLAUDE.md names. The edit is one entry appended
  to an existing list. A candidate precondition for `binding.md`: a workflow edit with no actionlint
  available is a stated gap, not a silent one.

### P9-T8b-4c — outcome, 2026-07-30

**Green: `dev.test_action_envelope` 44 tests, `dev.test_envelope_wire_keys` 6 tests, both exit 0;
full `dev/L0-CHAIN.txt` clean; `invariants-gate.py` 22/22; `spec-ids.py` OK at 251 IDs. Mutation:
V-BRK-028 20/20 caught (grown from 16), V-BRK-032 6/6 caught.**

The scheduled finding was one word: `session_trace()` put `parentSpanId` on the wire and
`broker.Trace` has no parent. The fix is `spanId`, which **preserves the information rather than
dropping it** — `ActionRecord.SpanID`'s own doc comment reads "the originating span", which is
exactly what the agent runtime's `SPAN_ID` is, and 06 §4.1's "a genuine retry necessarily carries a
fresh nonce and a fresh `spanId`" reads the same way. Discarding the value would have been the
cheaper diff and the wrong one.

**The defect had a parent, and the parent is a hole in a check.** `envelope.go` declares **six**
closed enums. `action_envelope.py` mirrored **three**. And `TestEnumsMatchTheBroker` — the class
whose entire job is "the two sides agree on the closed sets" — was three hand-written tests naming
those same three. **The set under test was the set that agreed.** The class could not have failed on
the three missing mirrors, because it did not know they existed.

That same hole had already fired live and been misread. `trigger.source: "verification"` came back
`400` during P9-T8b-4b-i and was written up as a fixture typo. It was not: nothing agent-side knew
`trigger.source` was closed, so nothing agent-side could refuse it, and because `DecodeEnvelope`
runs `DisallowUnknownFields` the broker's answer is total rather than field-scoped. Two symptoms,
one structure.

**So the response is `harness-improve` §3.2: strengthen the check that should have caught it.**
`TestEnumsMatchTheBroker` no longer names anything. It discovers every `valid<Name> =
map[string]bool{…}` in the Go source, maps each to its Python mirror by name, and asserts the two
**name sets** are equal in both directions before comparing members. Adding a fourth hand-written
test beside the other three would have closed today's gap and left the seventh enum exactly as
invisible as the fourth, fifth and sixth were this morning — and it would have read as progress.

**The vacuity guard is the equality, not a count.** The first draft asserted
`len(found) >= 6`, which is an enumeration of a number and goes stale the moment a seventh enum
lands. Two-directional name-set equality cannot pass vacuously: zero discovered enums is six
unexplained `VALID_*` constants on the Python side, and it also fails on a Python constant naming an
enum the broker does not have. It earned its keep immediately — the first discovery regex found five
of six, because `validRequesterKinds` is a one-line literal whose lazy DOTALL body ran past its own
closing brace and swallowed `validPlatforms` whole. That surfaced as a vacuity trip, **not** as a
member mismatch. A check whose failure mode is "I found fewer things than exist" is a check that
needs an arm looking at the count of things it found.

**The empty string is a member, not a falsy value.** `validPlatforms` and `validPropagation` both
carry `""`. A mirror that filtered on truthiness would reject every envelope omitting an optional
field — so the derivation copies members verbatim and never interprets them.

**V-BRK-032 is a second ID because it is a second property, and the split is declared.** The enum
join stays under V-BRK-028, whose file owns it. V-BRK-032 is the direction nothing covered: _every
key the agent builds is a key the decoder accepts_, and every key the decoder requires is one some
builder emits. It is asserted structurally over the builders' ASTs and then **measured against the
real `broker.DecodeEnvelope`**, compiled once and run on a maximal envelope from each tier. This
phase's findings list already carries three `V-*` rows that overlap without saying so; that is why
the split is written down rather than assumed.

**Two of this unit's own mistakes, both about the harness voting.** An unused `encoding/json` import
made the first decode program fail to compile — `rc 1`, indistinguishable from "the broker refused",
and green for the wrong reason. It was caught only because
`test_the_decoder_is_the_strict_one_this_check_assumes` demands the refusal **name** `parentSpanId`.
The build now happens once in `setUpClass` behind a loud assert, so a harness that does not compile
cannot produce a verdict at all ([[LSN-048]], [[LSN-049]]). And
`test_the_trace_key_the_defect_was_is_not_back` went red on the docstring explaining why
`parentSpanId` is gone — [[LSN-023]] in miniature — now scoped to AST string literals minus
docstrings: prose may discuss it, nothing may build it.

**M19 escaped the first sweep and the escape was real.** The consulted-ness test was a substring
search over `_check_client_side`, and every one of these constants is also interpolated into the
`EnvelopeError` it raises — so the check passed for a validator that inlines the members and
mentions the constant only when explaining the refusal. Rewritten to walk `ast.Compare` operands.
The mutant was rewritten to match: it swaps the comparison's operand for an inline `frozenset`
literal and leaves the error message referencing the constant, so behaviour is identical, every
other test stays green, and only the consulted-ness arm can catch it.

**Then the new enforcement found a third instance, and it was the worst one.** With
`VALID_TRIGGER_SOURCES` enforced client-side, `dev/test_broker_client.py` went red — eleven arms,
all reporting "nothing was POSTed". The cause is one line of shipped code:
`submit_action` passed `trigger or {"source": "agent"}`, and **`agent` is not one of the seven.**
Not a latent defect like `parentSpanId`, which needed `SPAN_ID` set, and not a fixture typo like
`trigger.source: "verification"`. This is the **default**, on the one mutation tool, reachable only
through an MCP server whose `submit_action` has no `trigger` parameter at all — so **every write
every agent could make was a `400 invalid-envelope`**, and had been since the file was written. It
survived because nothing has yet driven the MCP tool against a live broker: T8b-4b-i's driver builds
envelopes directly, and it uses `cron`.

A red sibling suite is halt condition 2, and this one is not a halt: the suite went red because the
implementation is wrong, the diagnosis is complete, and the fix is in the implementation. Nothing
about the check moved.

**The default is now `chat`, and the choice is not arbitrary.** 06 §4.1 splits the autonomy buckets
exactly at the interactive line — `humanRequested ∈ {chat, undo}`,
`selfInitiated ∈ {watch, alert, cron, delegation, escalation}` — and this function's only caller is
the MCP tool, which is reachable only from an interactive session. Defaulting to `watch` would file
human-requested work under autonomy in the metrics 01 §7 counts. Every autonomous origin arrives
through a caller that knows which one it is.

**But a default is still a default, and 06 §9 says the tool _takes_ `trigger`.** Making it a real
parameter touches three tiers' MCP tools and the `apply-change` skill that teaches them, which is
its own unit: **scheduled as P9-T8b-4d**.

**V-BRK-029 gains the arm that would have caught it**, in
`TestTheGoSideIsTheDefinition` — the class whose stated job is "every value Python restates is read
back out of Go and compared". It walks the `build_envelope` call, unwraps the `x or {…}` idiom the
defaults are written in, and asserts every literal landing in a closed-enum field is a member.
Nothing else could have: the enum mirror agrees with Go (V-BRK-028), every wire key is decodable
(V-BRK-032), the transport is correct (the rest of that file) — and a default is none of those
things. It is a **value**, and until this arm the only values under assertion were the ones the
tests themselves supplied. Sweep grown 15 → 18, **18/18 caught**: M16 restores `agent`, M17 does the
same to `requester.kind` so the scan is not a special case for one field, and M18 renames the call
target so the AST walk finds nothing — caught by the `checked` floor, not by any subTest.

**Findings filed, not fixed.** The escape itself — three hand-written tests where the source had six
enums — belongs on the next improvement pass as an escape, alongside the substring-search shape that
its own error messages satisfied. Both are instances of a check reading a name rather than a
structure. A third, sharper one joins them: **three defects of one class in three units**
(`trigger.source: "verification"`, `parentSpanId`, the `agent` default), and the class is _an
agent-side value the broker's closed schema refuses_. What they have in common is not the enum — it
is that the agent side had **no** local enforcement of anything the broker validates, so every such
defect could only be discovered by a live 400, one value at a time. That is now three mirrors and
three enforcements, and the general question for the improvement pass is whether the remaining
`envelope.go` validations (`hex32Re` on `traceId`, the required-field set, the per-op target
exclusivity) deserve the same treatment or whether the line is drawn correctly where it is.

### P9-T8b-4d — outcome, 2026-07-30

**`trigger` is a parameter now, and the argument for that is not tidiness.** 06 §9's tool table says
`submit_action` "takes `intent` + `operations` + `trigger`, fills `trace`/`requester` from the
session", and the tool took two of the three. T8b-4c could only replace a wrong default with a right
one, which fixes the 400 and leaves the actual problem: **`trigger.source` is the field 01 §7
counts.** It is what splits 06 §4.1's two autonomy buckets, so whatever a default says, it says it
for every caller that did not think about the question — and the direction it is wrong in is the
flattering one. An autonomous action filed as `chat` is a false statement about a human, the
quarter's answer to "how much of this did the agents decide on their own?" comes out too low, and
nothing anywhere reads as an error, because a defaulted enum member is a perfectly legal envelope.
The parameter is **required**, in the client and in both MCP tools: the caller states the origin or
there is no call.

**Flat strings, not a dict, and the reason is two constraints meeting.** V-BRK-029 requires each
`@mcp.tool()` body to be exactly one `return broker_client.<name>(…)` statement — logic in that
module is logic no L0 check can execute ([[LSN-007]]) — so a `{source, ref, detail}` dict cannot be
assembled inside the tool. And the schema the model reads is generated from the signature, so three
flat parameters (`trigger_source`, `trigger_ref`, `trigger_detail`) put the closed enum in the place
the model actually looks. `broker_client` assembles the dict, dropping `ref` and `detail` when empty
because both are `omitempty` on `broker.Trigger` and a blank string is a claim that there was
nothing to look at. The old `trigger: dict | None = None` was **removed** rather than kept beside
the new parameters — two ways to say the same thing is [[LSN-041]], and one of them would have gone
untested.

**Four arms added to V-BRK-029, and one of them exists because the unit deleted the surface the last
one watched.** T8b-4c's scan was pinned to `build_envelope`'s keyword defaults, which is where that
defect happened to live; T8b-4d removed the default, so the scan would have walked zero literals and
gone on reporting green. It now walks **every dict literal in the module** — the property was always
"no closed-enum value originates in this file unless it is a member", and `session_requester`'s two
`kind`s are inside it for the same reason the trigger was. Beside it: the origin is read back **off
the wire** for both tools in all three tiers (the first assertion anywhere that `trigger` survives
the trip), `trigger_source` is asserted to be in the no-default set of all four functions, and each
MCP tool's declared parameters are compared against what its one statement forwards — **by name**,
so `trigger_ref=trigger_detail` is caught too. Sweep grown 18 → 22, **22/22 caught**, with M16
rewritten to re-add a default whose value is _correct_, because the shape is the defect.

**V-CTR-020's two new mutants escaped on the first sweep, and this unit's own prose is why.** The arm
that guards required parameters asserted only that the backticked name appeared _somewhere_ in the
skill. The mutants delete a parameter's **definition** — and the paragraphs T8b-4d added to the
worked example mention both `intent` and `trigger_source` in passing, which kept the arm green over
a skill that no longer explains a required parameter. Being mentioned is not being documented; it is
[[LSN-023]] at one remove, a check satisfied by prose about the thing rather than by the thing. The
arm now requires the definitional bullet the file already uses for all three (`- **`name`** — …`),
which a cross-reference does not have. The second new mutant, M14, drops `escalation` out of the
seven-row table: every other arm passes on it, and an agent that was escalated to would pick the
nearest word it can see.

**Findings filed, not fixed.** The forwarding arm covers the MCP tools only; `plan_action` in
`broker_client.py` delegates in exactly the same shape and is covered only behaviourally (M21).
Generalizing "a single-statement delegation forwards every parameter it declares, under its own
name" to the whole write path is an improvement-pass item. And three needles in V-CTR-020 and two in
V-BRK-029 went `BROKEN` when the signatures and the skill text moved — not findings ([[LSN-048]]),
but five in one unit is the first time a spec's needles have been this brittle, and needles anchored
on a signature line are the pattern.

### P9-T7c-2c — outcome, 2026-07-30

**The number was never in the source.** V-BRK-021 asserted "one listening port, **one mutating
route**" and cited 03 §4.1. 03 §4.1 contains no route count. What it contains is _"there is no other
write path"_ and _"steps 1, 3, 4, 5, 6 and 11 are not skippable by any caller"_ — properties of the
**pipeline**, not of an integer. "One mutating route" was a faithful proxy while exactly one route
existed and became wrong the moment 05 §1.3's `replay` opened a second door into the same corridor,
which is what halted T7c-2b on 2026-07-29. A human ruled option (a) — reshape the check — and this
is that reshape. It is recorded as a **strengthening**, and PROTOCOL §10.2 is satisfied by the ruling
rather than argued around: §10.2's remedy for weakening a BLOCKING-ALWAYS check is a halt for human
review, and the review is the thing that scheduled this task ([`BACKLOG.md`](BACKLOG.md) B-003).

**The shape.** `MutatingRoutes()` was `[]string{ActionsPath}` with a doc comment reading "Exactly
one, and asserted." It is now `Registered()` less a declared non-mutating allowlist, where
`Registered()` is written by `handle` — the single function that touches the mux. The subtraction
runs in that direction on purpose: **the small set is the declared one**, so a route someone adds
and forgets to think about lands in the _mutating_ set, where the 05 §1.3 subset assertion refuses
it. Declaring the mutating routes and treating the remainder as harmless makes forgetting invisible,
which is precisely what the hand-written literal did.

**Four properties replaced one number**, and the count they replaced is now a consequence rather
than an assertion: equality against the registered set, subset of the design table, an allowlist
bounded to the three genuinely inert paths, and — new, and the clause that makes "non-skippability"
mean what 03 §4.1 says — every mutating route reaches `Authenticator.Authenticate` and
`Pipeline.Submit`, read off the call graph. A handler that answers 202 without touching the pipeline
is a write with no journal entry and looks like success to its caller; nothing before this looked
for it.

**Two escapes, and the reason is general enough to be worth the paragraph.** The first sweep caught
8 of 10. M3 rewrote `MutatingRoutes()` back into a literal and M4 rewrote `Registered()` into one —
and every set relation in the new test still held, because **on a server with one mutating route a
correct literal and a real derivation return the same answer**. "Derived, not declared" is not a
property of a single observation; it is a property of how the reporter responds when the input
changes, and a test that observes one server can never see it. Closed by building a second server
that registers a path nothing else knows about: a literal cannot mention it. The same blindness
applies to any check of the form "the accessor agrees with the facts" where the facts have only ever
had one shape — and it is the second time in three units that a new check's first sweep found it
weaker than its author believed, which is the argument for the sweep being part of VERIFY rather
than a flourish.

**One hole the sweep found in the design, not just the check.** M2 adds a route _and_ declares it
non-mutating: it never enters the set the equality and subset arms measure, so both hold. The first
draft's M2 was caught only because it happened to use a path `TestNoDebugRoutes` probes by name —
caught by a guess, which is not caught. The real closure is the third arm: `nonMutatingPaths` may
name only `/healthz`, `/v1alpha1/nonce` and the catch-all. A route excusing itself into the
allowlist now fails on the allowlist.

**What this did not do.** It did not implement `/replay` or `/approve` — those stay in Phase 10
beside P10-T4/T7, as one unit against one reshaped check, and the T7c-2b deferral row closes on the
09 edit exactly as its promotion condition said. It did not settle V-BRK-021's **L2** half: the
2026-07-29 P9-T9 recon records it needing L0+L2 with only L1 evidence on file while the deferral row
records it green at L0, and that reconciliation is **T9b's**. Touching a row does not earn the right
to answer a question about it. The re-entry clause is in the row as a conditional over an **empty**
population, and the suite logs it as empty rather than satisfied.

**Retired with it:** `strings.Count(src, "s.mux.HandleFunc(") != 4`. It did catch a smuggled
handler, and it also went red on every legitimate route, so its maintenance instruction was "raise
the number until it passes" — a check you edit to make it pass is a check that will one day be
edited past a real finding. Its replacement asserts that the count of _registration points_ is one,
which no legitimate route addition changes.

### P9-T8b-4b-ii-2b splits again: 2b-i wires the validator, 2b-ii is the soak

**The fifth time surveying the soak turned up a defect in shipped code rather than a gap in the test
scaffolding.** 4a found `KUBEAGENTS_BROKER_IMAGE` set nowhere; 4b-i found no client that could hold
a certificate; 4b-ii-1 found an RBAC denial surfacing as a 500; 2a found a write overlay with no
admission rule to govern it. This one is larger than all four.

**`undo.GenerateAndValidate` has no non-test caller.** The function whose own doc comment reads "the
call the broker actually makes at step 6" is called by nothing outside its own tests.
`pipeline.Config.Planner` is a `Generate`-only seam that defaults to `undo.Generate`;
`cmd/broker/wiring.go` leaves it unset and says so in its header — "the undo planner … is left unset
so the owning package supplies it" — and the owning package supplies the half that does not
validate. There is no `undo.DryRunner` implementation anywhere in the tree outside `undo`'s own
tests. Consequences, each read off the code rather than inferred:

- Every `ActionRecord` the shipped broker has ever written carries `undoPlan.validated: false`.
- `undo.ValidateReplayable` refuses on exactly that field — _"the undo plan was never dry-run against
  the API server, so nothing has checked that its steps would apply"_ — and it is the front door of
  both replay paths (`verify/driver.go` and `rollback.Rollback`). **Undo is non-functional end to
  end**, and the way a human would find that out is by trying to undo an outage.
- 06 §4.3.1 is normative that validation happens and that failing it raises to `gated`. That arm
  cannot fire at all today.
- **V-REV-001 at L2 is therefore 0 %, not 100 %** — which is the exact property 2b was built to
  measure. Running the soak first would have produced a red with no diagnosis attached.

The escape shape is 09 §11.9, _component built, never wired_. V-REV-003's L1 row (2026-07-27, P9-T4)
proved that `Validate` **downgrades correctly when the dry-runner is nil**. It proved the function
and never the wiring, and its own evidence note says so without noticing: "an unwired dry-runner …
each is a downgrade, not an error."

| Unit                  | What                                                                                                                               | Checks             | Blocked on                           |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------- | ------------------ | ------------------------------------ |
| **P9-T8b-4b-ii-2b-i** | Wire the validator: a required `DryRunner` on the pipeline, a real one over the actor's client, step 6 refuses an unvalidated plan | **V-REV-003** (L1) | —                                    |
| **2b-ii**             | The envelope corpus soak, journal mining, guard 3 as a label assertion, **V-REV-001** at L2                                        | **V-REV-001** (L2) | T9b's **write** overlay + its ruling |

2b-i goes first under this phase's own L0-before-L2 rule, and it is unblocked. **2b-ii is blocked and
the blocker is structural, not scheduling.** A server-side dry-run write is authorized as the write
verb, so under the read-only tenant overlay every undo step's dry-run is a 403, every plan downgrades
to `none`, and every action gates — a correct broker reporting 0 % coverage for a reason that has
nothing to do with undo. The last mile is the **write** overlay, which is T9b's and which is itself
waiting on the admission ruling 2a surfaced.

### P9-T8b-4b-ii-2b-i — outcome, 2026-07-30

**Undo is wired. `undoPlan.validated` is now a fact rather than a field.**

What landed, in the order the seam runs:

- **`pipeline.Config.DryRunner`** — a **required** `func(agentIdentity string) undo.DryRunner`.
  Required, so a broker with nothing to validate with refuses to start rather than serving every
  request and journalling `validated: false`. A **factory** keyed by identity, not a fixed object,
  because server-side apply reports a conflict for every field owned by a different manager and an
  undo commonly restores fields this agent set earlier — a dry run under any other name manufactures
  conflicts the real replay never hits, downgrades working plans, and gates the fleet for a reason
  that is an artifact of the check.
- **`Planner` gained a fourth parameter** and the default moved from `undo.Generate` to
  `undo.GenerateAndValidate`. A signature that cannot express "generate without validating" is what
  stops that returning.
- **`rollback.PlanDryRunner`** — the production `undo.DryRunner`, built **on the `Replayer`** rather
  than beside it. The question plan-time validation asks is not "is this plan well-formed" but
  "would the calls the replayer is going to make succeed", and only the replayer knows which calls
  those are. A validator with its own op table would answer a question about a different program.
- **`ClientApplier.Create` / `rollback.Writer.Create` gained `dryRun bool`**, so the validator goes
  through the one client this broker has instead of opening a second one (LSN-040).
- **`cmd/broker/wiring.go`** hoists the replayer beside the applier and hands the same object to the
  verifier's rollbacker and to the validator's factory.

**The design question 06 §4.3.1 does not answer, and the ruling taken.** An undo plan describes the
world **after** the action and is validated **before** it, so two of the four steps address an object
whose existence is exactly what the action is about to change: the `delete` that reverses a create
gets a NotFound, the `create` that reverses a delete gets an AlreadyExists. Read literally, validation
would downgrade both and gate every create and every delete in the fleet. The ruling: **the dry run
asks "would the API server accept this step from this identity", and those two answers are positive
evidence.** Kubernetes authorizes before it looks the object up — a caller without the verb gets 403,
not 404 — and a create clears mutating and validating admission before storage. Everything else (403,
Invalid, a webhook rejection, a missing scale target, a body `hydrate` refuses) is a failure that
downgrades to `none`, which the 06 §4.2 step 6 floor raises to gated. The one honest gap, DELETE-time
admission running after the fetch, is named in `dryrun.go` rather than papered over.

**A side effect worth having.** Reusing `Replayer.hydrate` moves the redacted-Secret refusal — the
worst thing in this package's blast radius — from replay time, during an incident, to generation
time, where it is a downgrade and the action gates before mutating anything.

**Three sites ask "is there a usable undo plan", not two.** classify's 06 §4.2 step 6 floor, the
pipeline's step 6 re-check, and the brake's 06 §4.4 row 5. Only the first suppresses for a dry run.
The first two now read one predicate, `classify.UndoPlanGateApplies`, so they cannot drift; be precise
about what that buys, because the tempting claim is bigger than the truth — mutating step 6 back to
its own spelling does **not** fail anything, since the brake has already raised the class by the time
step 6 looks. It is a structural fix, and it is asserted directly rather than through behaviour.
**The brake is the outstanding one and is filed, not fixed**: it raises a dry run whose plan cannot be
validated to gated, so it parks for approval instead of previewing. Over-gating, safe, and a row in
the 06 §4.4 table — V-BRK surface, and changing a brake row is a unit of its own rather than something
folded into the unit whose wiring surfaced it.

**A second escape found while verifying this one, and this one is not small.**
`internal/broker/rollback`'s `TestMain` did `os.Exit(0)` when `KUBEBUILDER_ASSETS` was unset. That is
a package-wide skip wearing the word `ok`: **the entire hermetic half of the package — including the
refusal that stops a redacted Secret being written back as sixty-four characters of hex — had never
run under `go test ./...`, which is what the L0 chain and PR CI execute.** The package reported `ok`
in 1.3 seconds while asserting nothing, and it was found only because `dev/mutate.py` refused the
sweep: `go test -list` returned no names, so the catchers "did not exist". LSN-048's guard caught a
defect it was not written for. Fixed to the shape escalate, history and writeahead already use — the
environment is optional, the six envtest tests skip individually via `requireEnv`. `probe` still has
the old shape; it has no hermetic tests today, so it costs nothing yet, and it is filed.

**Findings filed, not fixed** — three, and the first two are named above. (a) The brake's 06 §4.4 row 5
is the third spelling of the undo-plan gate and does not suppress for dry runs. (b) `internal/broker/probe`'s
`TestMain` still carries the `os.Exit(0)` shape. (c) New, and adjacent to (a): `BrakeInputs.UndoPlan` is fed
`signal(s.plan.Undoable())` at [pipeline.go:708](../../k8s-operator/internal/broker/pipeline/pipeline.go#L708)
and [:852](../../k8s-operator/internal/broker/pipeline/pipeline.go#L852) — the **weaker** predicate, the one
step 6 stopped asking in this unit. It cannot under-gate today, because `Undoable()` is implied by
`Validated()` and the brake only raises, so a plan that is undoable-but-unvalidated already reaches the brake
as `true` and step 6 catches it after. But it means the brake's view of "is there a usable undo plan" and the
pipeline's are two different questions wearing one field name, and the next person to add a lowering rule to
the 06 §4.4 table inherits that. Same V-BRK surface as (a) and the same unit.

Evidence: **V-REV-003 (L1) pass** — 13 new assertions across
`internal/broker/pipeline`, `internal/broker/rollback` and `cmd/broker`; mutation sweep
`verification/mutants/V-REV-003.json` **12/12 caught**, including two vacuity controls, with the
mutants aimed at the **wiring** (the required-field arm, the default planner, the composition root,
the identity threading) rather than at the function the old L1 row already proved.

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
