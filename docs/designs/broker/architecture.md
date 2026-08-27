# The action broker: architecture on the `broker` branch

This document describes the kube-agents action broker as it exists on the `broker` branch — the packages that compile, the contracts they enforce, and the seams left deliberately open. The module was ported from the original three-tier fleet design (never merged) and then isolated: it shares no Go identifiers with the shipping operator, only the API group `kubeagents.x-k8s.io` and the one manager ClusterRole/webhook config. The charter and boundary live in [README.md](README.md); the approval loop this module still lacks is designed in [chat-approval.md](chat-approval.md); the seams into the current world are [integration.md](integration.md)'s. Normative tables — risk rules, brake rows, undo strategies, the envelope schema — stay in the frozen spec and are cited here, never restated.

## 1. Shape of the module

The module deploys as four kinds of artifact. First, a per-caller broker Deployment running `kage-broker` (`k8s-operator/cmd/broker`, image `Dockerfile.broker`, shell-less), the one process holding the actor credential. Second, a module-owned controller manager running the five reconcilers of §5 — designed, not built; no entry point on the branch wires them. `config/manager/brake_deployment.yaml` is a stale artifact of the port — its `--controllers=brake` flag has no implementation in any binary on the branch — and is replaced by the module manager's own Deployment. What carries over is the hand-written ClusterRole (`config/rbac/brake_role.yaml`): get/list/watch on `actionrecords`, patch-only on `actionrecords/status`, get/list/watch plus patch on `agents`, and create/patch on core `events` — hand-written rather than kubebuilder-generated so the grant never folds into the operator's manager role. Third, the six CRDs (`config/crd/bases/kubeagents.x-k8s.io_*.yaml`): `Agent`, `ActionRecord`, `ApprovalRoster`, `ChangePolicy`, `FleetFreeze`, `UndoRequest`. Fourth, two ValidatingAdmissionPolicies in `config/policy/vap-agent-scope-journal.yaml`, covered in §5.

One invariant carried from the fleet design survives every repackaging decision: one broker process per actor identity, never a fleet-wide writer ([08 §5.2](spec/08-agent-runtime-and-identity.md)). A broker's tier, scope, and permitted caller come from its own Deployment — the `KAGE_AGENT_TIER`, `KAGE_AGENT_SCOPE`, `KAGE_NAMESPACE`, and `KAGE_READER_SERVICE_ACCOUNT` flags and env fallbacks (`cmd/broker/main.go:125–131`) — never from anything the caller sends.

## 2. The HTTP surface

`internal/broker/server.go` registers three exact-path routes plus a refusing catch-all: `ActionsPath = "/v1alpha1/actions"` (the only mutating route), `NoncePath`, and `HealthzPath`, on `Port = 8443` with `MaxRequestBytes` capping every submission. `bypassHeaders` — authority-shaped `X-Kube-Agents-*` request headers — are rejected before dispatch, mirroring the reserved body keys. `MutatingRoutes()` is derived from the registered set minus the declared non-mutating paths, not declared by hand, so a new route cannot silently escape the mutating classification. `NewServer(Config)` refuses to construct without an `Authenticator`, `ReplayGuard`, `RejectionJournal`, `AutoPauser`, and `Pipeline`.

Authentication (`auth.go`) runs mTLS and then TokenReview, both mandatory, with `TokenAudience = "kubeagents-broker"` on the projected token. `ExpectedCaller` is singular — "the ONE reader identity this broker accepts" (`auth.go:69`) — because a broker serves exactly one agent and a list would make attribution a guess. The resulting `Identity{Tier, Scope, …}` is filled from broker configuration, never from the request body.

Anti-replay and idempotency implement the contract in [06 §4.1](spec/06-api-and-data-contracts.md): `internal/broker/antireplay.go` (`ReplayGuard`, nonce issue and consumption), `idempotency.go` (`ComputeIdempotencyKey`, recomputed server-side over the JCS canonicalization in `jcs.go`), and `envelope.go` (`Envelope`, `ReservedKeys`, and `Refusal`, the error type the whole surface speaks). The freshness windows, key algorithm, and reserved-key refusal table are 06 §4.1's to state; the reserved-key list is additionally pinned to the spec by `TestTheReservedKeyListIsTheOneTheSpecPublishes` (`internal/broker/envelope_roundtrip_test.go:311`), which parses the spec table as data — one reason the spec files are frozen.

## 3. The pipeline, steps 1–11

The spec's eleven pipeline steps ([03 §4.1](spec/03-security-model.md), with the implementation mapping in [05 §1.1](spec/05-system-architecture.md)) map directly onto code. `steps.go` names them `StepAuthenticate(1)` through `StepJournal(11)`, and `StepTrace` enforces the ordering — a step recorded out of order is an error, not a log line. Steps 1–2 run in the server; steps 3–11 run in `internal/broker/pipeline`, where `Pipeline.Submit` mints the action's ULID and runs `stepResolve` through `stepJournal`.

Every decision input is a seam on `pipeline.Config`, each with one production implementation:

| Seam         | Production implementation                                                                                    |
| ------------ | ------------------------------------------------------------------------------------------------------------ |
| `Classifier` | `internal/broker/policy.Source` (ChangePolicy-aware classifier source)                                       |
| `Live`       | `livestate.Source` (scope counts, per-object reads)                                                          |
| `Refs`       | `refindex.Source` (inbound-reference lookup for recreate downgrades)                                         |
| `Planner`    | defaults to `undo.GenerateAndValidate`                                                                       |
| `DryRunner`  | `rollback.PlanDryRunner` (plan-time dry run with the replay field manager)                                   |
| `Executor`   | `execute.Executor`, with `writeahead.Confirmer` as its journal — the record is durable before the mutation   |
| `Verifier`   | `verify.Driver`, with `probe.Source`, `escalate.Recorder` (both `Pager` and `Pauser`), and `cooldown.Source` |
| `Records`    | `journal.Store`                                                                                              |
| `Brake`      | `brake.Source`                                                                                               |
| `Accountant` | `budget.Source`                                                                                              |
| `Contested`  | in-memory `broker.NewContestedIndex()`                                                                       |

Shadow mode is resolved here, once per submission. In this document set the term means exactly one thing: `Agent.spec.operations.dryRunOnly` forcing every submission to an effective dry run, per `shadowed()` (`internal/broker/pipeline/pipeline.go:344`). The effective decision is the caller's `dryRun` OR `spec.operations.dryRunOnly` OR an unreadable broker `Agent` CR — a nil Agent reads as shadowed, fail-closed. The composition runs one way only, toward not executing; nothing anywhere composes back to executing.

## 4. Decision machinery — cited, not restated

### Classification

Classification is deterministic: its inputs are resolved targets and live cluster state, never `intent`, `rationale`, or any prose, and the result is the maximum over every rule that fires — there is no downgrade operator. The implementation is `internal/broker/classify` (`Class`, `Classifier`, `ComputeBlastRadius`, with the blast thresholds in `blast.go`). The normative rule table and evaluation order are 06 §4.2. `ChangePolicy` is stricter-only by construction (06 §4.2; `changepolicy_types.go`): loosening is unrepresentable in the type, and the broker takes the maximum anyway.

### Undo

Every undo plan is generated and dry-run-validated before execution, at step 6; an operation with no valid plan raises the envelope per 06 §4.2. The implementation is `internal/broker/undo`: `StrategyFor` selects the per-verb strategy, `GenerateAndValidate` is what the pipeline calls, `Result.Undoable()` is deliberately weaker than `Validated()`, and `ValidateReplayable` is the replay front door that refuses an unvalidated plan. The strategy table and the snapshot sanitizer are 06 §4.3.1.

### Brake

`broker.Decide` is a pure function over the nine fail-closed rows of 06 §4.4 plus `paused` and `frozen`. Zero values refuse: an unanswered `BrakeSignal` is `BrakeUnobserved`, a nil `Accountant` means nobody is counting, a nil `ContestedIndex` means nobody is watching, and each refuses on its own. A `FleetFreeze` cache older than `MaxFreezeStaleness = 30s` is treated as engaged. The rows and their semantics are 06 §4.4's; they are not re-tabulated here.

### Budget and flap

`budget.Source` folds the journal into hourly and daily buckets per origin and class. Undo is exempt from the budget but not from flap detection ([06 §1.1](spec/06-api-and-data-contracts.md)), and exhaustion escalates to a human rather than silently slowing the agent ([04 §4.2](spec/04-workflow-model.md)).

## 5. Journal, records, and the controllers

`ActionRecord` is the write-ahead journal. Records are named `ar-<lowercased ULID>` (`journal.RecordName`), the whole spec is CEL-immutable after creation, and the phases with their terminality rules live in `actionrecord_phases.go` (`ValidateActionPhaseTransition`). Retention runs on two clocks — the record TTL and the shorter undo window — implemented in `internal/journal/retention.go`, where `DeletableAt` is the clock half of the deletion predicate. The identity-plus-exported half is the second VAP, `kube-agents-journal-retention`: admission has no clock, so it checks only who is deleting and whether `status.exported.confirmed` is true.

Five reconcilers in `internal/broker/controller` service the records:

- `BrakeReconciler` — C-BR in spec vocabulary — reads the verify-failure pause/page records (`escalate.Recorder`), patches `Agent.spec.operations.paused`, and emits the page Event.
- `UndoReconciler` replays undo plans through the pipeline and maintains the bidirectional linkage (`finalizeLink`) and contested markers (`annotateContested`).
- `JournalReconciler` writes `status.exported` and tracks export lateness.
- `RetentionReconciler` deletes records only after export is confirmed.
- `AgentReconciler` reconciles the workload pair (transitional — dropped or replaced at integration; §7).

All five run under the module-owned controller manager of §1 — designed, not built. No entry point wires them today: nothing outside tests imports `internal/broker/controller` (§8).

The status writer table — which principal may write which `ActionRecord.status` fields — is 06 §4.3's normative table; `config/policy/vap-agent-scope-journal.yaml` is the enforced artifact. The fact this document uses: a human cluster-admin is explicitly denied, so hand self-approval is impossible. The approval-side facts — the gateway identity and what it may write — are [chat-approval.md](chat-approval.md) §1's. The VAP has one acknowledged fail-open: the per-field enumeration is the allow-list, so a new, unenumerated status field would slip through unexamined; the parity check named in the file's own comments is not yet a file in the tree. One more nuance: the writer rows naming `kubeagents-controller` (`vap-agent-scope-journal.yaml:64` and `:450`) describe the fleet-era plan, retention inside the operator manager, and are updated at integration to the module manager's own ServiceAccount.

## 6. The Agent CR, reframed

The load-bearing reframe of this restart: in this module the broker `Agent` CR is caller registration and the brake surface, not the owner of a pod. The fields the broker reads are `spec.tier` (immutable), `spec.scope`, and `spec.operations` — `Paused`, `PauseReason`, `DryRunOnly`, `ApprovalRosterRef`, `ChangePolicyRefs`, `InitiativeBudget`, `NotifyOn` — through the helpers `Brake()` (which keeps the three brake fields together so a caller cannot consult `paused` and forget `dryRunOnly`) and `EffectiveInitiativeBudget()`. On the status side, `OperationsStatus` records the observed brake state and `BrokerStatus` the broker's own health, with `JournalReachable` a zero-value-false boolean: an unwritten status reads as unreachable, fail-closed.

An integrated `PlatformAgent` caller registers as tier `platform` on its broker `Agent` CR. `spec.tier` still feeds scope derivation and the classification rules that key on cross-tier ownership — inert with a single tier — and the `cluster-admin`/`developer-team` values are pruning candidates at integration.

What the CR is not, in this design, is the thing that owns the caller's Deployment. Registering a caller and running a caller are different concerns, and only the first is inside the module's boundary. The port still carries the fleet-era fields — `parentRef`, `harness`, `deployment`, `integration` — as transitional inputs to §7's rendering code; they are expected to shrink at integration, and integration.md §5 (d) owns that plan.

## 7. Transitional: the workload-pair rendering

`internal/broker/controller` still renders agent pods. `agent_manifests.go` builds the agent pod — the model, no write verb; `broker_manifests.go` builds the broker server (`buildBrokerService`, the `<agent>-broker` ClusterIP Service on 8443; `buildBrokerDeployment`; `buildWaitForBrokerContainer`); `pod_launcher.go` (`LaunchSpec`, `WorkloadPair`) enforces pair-atomicity per 08 §2.6; `mesh_trust.go` and `pair_netpol.go` issue the pair's certificates and NetworkPolicies. All of it is transitional — dropped or replaced at integration. It is load-bearing today for standalone testing, because it is how a broker gets a caller to test against, and it is slated to go because the shipping operator already owns pod rendering for `PlatformAgent`.

What must survive any replacement is not the code but three properties: the two-ServiceAccount split and its premise that containers are not a credential boundary (08 §2.2), the broker-first startup ordering, and one broker per actor identity (08 §5.2). integration.md §4 (c) names what replaces the renderer.

## 8. Nil and unwired in production

The production wiring leaves these gaps, stated here as facts with their anchors:

- `Approvals: nil` (`cmd/broker/wiring.go:343`) — still true, and still fine: [chat-approval.md](chat-approval.md)'s notifier is built and delivers by its own watch loop over `PendingApproval` records regardless of whether the broker's synchronous step-7 nudge ever fires (`notify.Reconciler`, driven by `cmd/chatops-gateway`, not by the broker). What chat-approval.md's build inventory (§6) still names as absent is a production Google Chat JWT verifier and the two chat apps' actual provisioning, not the loop itself.
- No entry point wires the reconcilers. Nothing outside tests imports `internal/broker/controller`; the module-owned controller manager that runs them (§1) is designed, not built.
- `config/manager/brake_deployment.yaml` names `--controllers=brake`, a flag no binary on the branch implements — a stale artifact of the port, replaced by the module manager's own Deployment.
- `BodyStore: nil` (`cmd/broker/wiring.go:312`). No production `journal.BlobSink` exists, so over-1MiB bodies are refused by name in `execute` rather than stored by reference. `Replayer.Sink` is nil for the same reason.
- `ContestedIndex` starts empty in memory. Rebuilding it from `ActionRecord.status.contested` on restart is open work; until then a restarted broker answers "not contested" for everything.
- Nothing reads `ApprovalRoster.spec.notify` today.

A gated action on this branch waits until its TTL expires, always. Refusal, classification, undo, brake, and journal all work while nothing new gains authority — the machinery-before-authority posture [07 §5](spec/07-implementation-roadmap.md) requires.
