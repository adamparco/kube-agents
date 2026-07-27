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

| Task       | What to build                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | Spec             | Files                                                                                                                                                                                                                                                                        | Check IDs                                                                    | Weight           |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- | ---------------- |
| **P9-T10** | Repair the inter-agent credential seam. Declare `agent_common` with an `env:` block carrying `API_SERVER_KEY` (and the `KUBERNETES_SERVICE_*`/`HERMES_HOME` set `platform_control` gets) in **both** definition sites, for **all three tiers**; the image-baked config must also stop listing a toolset entry for a server it never declares. Bind to **V-CMP-006** with a lint that fails any MCP server whose script reads a credential from the environment and whose config declares no `env`. **Do not weaken the fail-closed refusal.** Record for P15-T1: the per-tier `API_SERVER_KEY` values currently differ and `resolve_agent_credentials` sends the caller's own key as the target's bearer.                                                                                                                                                                                                                                                                                                                                                                                                                                                  | 05 §1; 06 §4.1   | `agents/{platform,cluster-admin,developer-team}/config.yaml` · `k8s-operator/internal/controller/agent_manifests.go:156` · goldens · new `dev/test_mcp_env_declared.py` · L0-CHAIN                                                                                           | **V-CMP-006**                                                                | medium           |
| **P9-T1**  | `ActionRecord` CRD + journal store. Full 06 §4.3 schema: attribution, classification, targets, `preState` (with the >1 MiB `objectRef` path), undo plan, the ten-phase status lifecycle, the **two** retention clocks, bidirectional undo linkage, `chainId`. `spec` immutable by CEL; `status` field/principal table enforced by `vap-agent-scope-journal`. Includes the journal reconciler (`C-JR`) behind a **pluggable audit source**, the retention controller's post-export deletion predicate, and the Data Access audit-log probe of planning defect 3.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | 06 §4.3          | new `api/v1alpha1/actionrecord_types.go` · `config/crd/bases/…_actionrecords.yaml` · new `internal/journal/` · `internal/controller/journal_reconciler.go` · `internal/controller/retention_controller.go` · `config/policy/vap-agent-scope-journal.yaml`                    | V-BRK-003, V-BRK-015, V-REV-008, V-CTR-\*                                    | high             |
| **P9-T2**  | Action Envelope + broker skeleton. New tier-neutral binary and image. `POST /v1alpha1/actions` + `GET /healthz` on **8443**, HTTP+JSON over TLS (not gRPC). mTLS **and** projected token with audience `kubeagents-broker`; `TokenReview`; `(tier, scope)` derived from the authenticated caller and **never** from the body. Idempotency key = `"sha256:" + lowerhex(SHA-256(JCS(K)))`, recomputed by the broker. The three anti-replay mechanisms. Exactly one listening port, one mutating route, no `/bin/sh` in the image.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | 06 §4.1; 03 §4.1 | new `cmd/broker/main.go` · new `internal/broker/{server,auth,envelope,idempotency}.go` · new `k8s-operator/Dockerfile.broker` · `tags.env` · `deploy/docker/cloudbuild.yaml` · `dev/cluster/reload-images.sh` · publish workflows · `verification/fixtures/envelopes/`       | **V-BRK-002**, V-BRK-007/008/009/010/017, V-BRK-021, V-RUN-010, V-CTR-005    | **load-bearing** |
| **P9-T3**  | The risk classifier + `ChangePolicy`. Deterministic, table-driven, the 06 §4.2 evaluation order (scope ⇒ short-circuit, forbidden ⇒ short-circuit, max over inputs, `+1` capped at gated, `ChangePolicy` max, no-undo-plan raise). The seventeen code-floor rules including `secret-material-egress` (digest match, **not** entropy), `cross-tier-direct-operation` (ownership computed via the V-6 subset predicate, reused not reimplemented), and the production-label precedence ladder. Both path dialects, with the `/`-prefix rejection at admission. The **120–200 case corpus** of 09 §7.1 with asymmetry pairs. Classifier package imports no inference client.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | 03 §5; 06 §4.2   | new `internal/broker/classify/` · new `api/v1alpha1/changepolicy_types.go` · CRD · webhook rule (class ≥ floor) · new `verification/fixtures/classifier-corpus.yaml` · `dev/tests/classifier-corpus-lint.py` (V-MET-005) · L0-CHAIN                                          | **V-GAT-001/002/009/010/017/021/022**, V-GAT-011, V-GAT-012                  | **load-bearing** |
| **P9-T4**  | Undo-plan generation for every supported verb — the 06 §4.3.1 strategy table (`create`→`delete`, `apply`/`patch`→`restore`, `scale`→`restore`, `delete`→`recreate`, cloud→`inverse`, else `none`), the sanitizer, `preconditions.uid` on every step, inbound-reference detection downgrading `recreate` to `none`, and dry-run validation of each step against the API server. The explicit **"cannot generate" path reclassifies as gated** — this is what makes reversibility true rather than aspirational, so it is tested directly and from both sides. The 09 §7.3 round-trip fixtures including the negative set.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | 06 §4.3.1        | new `internal/broker/undo/` · `verification/fixtures/undo/` · unit + envtest suites                                                                                                                                                                                          | **V-REV-003**, **V-REV-004**, V-REV-001, V-REV-009                           | **load-bearing** |
| **P9-T5**  | Snapshot → execute → verify. Server-side apply with field manager **exactly** `kube-agents/<tier>/<scope>`, dry-run first where supported, per-kind verification predicates (04 §5.1), the recovery ladder recorded in `status.recovery`, and the atomicity rule (multi-target: if any snapshot fails, **nothing** is applied). Selector fan-out expanded **once**, before classification, against live state. Write-ahead ordering: the record's durable write precedes the mutation, which precedes the API response, which precedes the chat report.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | 04 §1, §5.1      | new `internal/broker/execute/` · `internal/broker/verify/` · envtest suites                                                                                                                                                                                                  | **V-BRK-006**, V-BRK-018, V-BRK-019, V-BRK-020, V-BRK-014, V-REV-002/005/006 | high             |
| **P9-T6**  | The brake. `Agent.spec.operations` (`paused`, `pauseReason`, `dryRunOnly`, roster/policy refs, initiative budget) and `status.operations`/`status.broker`; cluster-scoped `FleetFreeze`; `UndoRequest`; `ApprovalRoster`; the `contested` index and its advisory annotation; the undo controller (`C-UC`). Every one of the nine fail-closed rules of 06 §4.4. All five controls must work through `kubectl` and the API **with inference down** — no dependency on the model, the router, or the agent pod. `pause` is **not** scale-to-zero.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | 03 §6; 06 §4.4   | `api/v1alpha1/agent_types.go` (+`operations`) · new `{fleetfreeze,undorequest,approvalroster}_types.go` · CRDs · `internal/controller/undo_controller.go` · `internal/broker/brake.go` · webhook · goldens                                                                   | **V-RUN-007/008/012/013**, **V-BRK-005**, V-REV-007, V-GAT-003/007           | **load-bearing** |
| **P9-T7**  | Controller reconciles the pair. Render the broker Deployment, Service (`<agent>-broker`, 8443) and certificate Secret **before** the agent Deployment, both owned by the `Agent` CR; `BrokerReady`/`AgentReady` conditions with `Ready` their conjunction; the `wait-for-broker` init container with observe-and-report on timeout; `KUBEAGENTS_BROKER_ENDPOINT` injection; the `kube-agents/role` label on both halves; the broker's NetworkPolicy (ingress only from `role: reader` with matching `kube-agents/agent`) and the agent's egress-to-broker rule. **Mints no RBAC.** Regenerate goldens.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | 08 §2            | `internal/controller/agent_manifests.go` · `internal/controller/agent_controller.go` · `pod_launcher.go` (pair-atomic `LaunchSpec`) · `netpol-*.yaml.template` · goldens · `dev/tests/reference-render.py`                                                                   | **V-RUN-001/002/003/004/005/006/009/011**, V-BRK-012, V-ISO-001/002          | high             |
| **P9-T8**  | Shadow mode. The agent's `apply-change` MCP tool submits real envelopes; the broker classifies, plans undo, and journals a `DryRun` `ActionRecord` without calling a mutating API. `dryRunOnly` is stricter-only and cannot be cleared by the agent. Run against `gke-scratch-kube-agents-dev` for the duration of the phase and mine the journal for classifier gaps — every gap found becomes a corpus case, not a code tweak.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | 04 §1            | `agents/*/skills/` (the `apply-change` skill) · `deploy/*/scripts/` MCP tool · `internal/broker/server.go` (dry-run terminal path) · a journal-mining note in this file                                                                                                      | V-REV-001 (DryRun scope), V-GAT-019, V-CHR-\* (advisory)                     | high             |
| **P9-T9**  | Consolidated gate `dev/verify/verify-phase9.sh`: envelope round-trip, scope-spoof rejection, classifier fixture corpus, undo-plan coverage, brake liveness with inference down, fail-closed on journal loss, and the Accept (e) two-sided `can-i` sweep. The test-only tenant overlay of planning defect 2, with its three guards. Regression through `verify-phase8.sh`. New L0 and L2 chain lines. **Also fix the review-gate path filter, found during P9-T2:** `.github/workflows/review-gate.yml` triggers on `**/policy/**`, `**/agents/**`, `**/provisioning/**`, `**/namespaces/**` and `**/SOUL.md` — none of which match `k8s-operator/internal/**`, so PR [#33](https://github.com/adamparco/kube-agents/pull/33) added the broker, an authenticator and the one image whose SA can write, and the security gate did not run on it. The gate was written when the security surface was manifests; it now includes Go. Widen the filter to the broker, webhook, router and RBAC paths **in the unit that owns the gate**, not in a unit that would be reviewing itself. Expect the first run to need waiver triage: the suite was tuned on YAML. | 07 §5            | new `dev/verify/verify-phase9.sh` · new `dev/verify/broker-auth-l2.sh` · `dev/verify/broker-execute-l2.sh` · `dev/verify/actor-grant-sweep-l2.sh` · `dev/verify/fixtures/actor-tenant-grant.yaml` · `dev/tests/invariants-gate.py` · `dev/L0-CHAIN.txt` · `dev/L2-CHAIN.txt` | all of the above + V-MET-007                                                 | **load-bearing** |

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
The L2 instances of 003 and 004 need an envtest round-trip against a real API server, which is
**P9-T5**'s, since that is where the executor that produces the pre-states first exists.

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
