# The kube-agents action broker

The kube-agents action broker is a standalone, independently deployable module. It has its own CRDs — API group `kubeagents.x-k8s.io`, Go package `k8s-operator/api/broker/v1alpha1`, kinds `Agent`, `ActionRecord`, `ApprovalRoster`, `ChangePolicy`, `FleetFreeze`, `UndoRequest` — its own binary (`kage-broker`, `k8s-operator/cmd/broker`), its own packages (`k8s-operator/internal/broker/**`, `internal/journal/**`), and its own RBAC and deployment manifests. It shares no Go identifiers with the shipping operator; the only shared surfaces are the API group and the one manager ClusterRole/webhook config.

The model is escalation. Agents run read-only. To mutate anything — kubectl apply/patch/scale/delete, or a cloud call — an agent builds an action envelope and submits it to the broker, which alone holds write credentials (the actor identity). The broker classifies the action (`routine`/`elevated`/`gated`/`forbidden`), refuses the `forbidden`, raises anything without a valid undo plan to `gated`, executes the `routine`, and parks `gated` actions as `PendingApproval` for approval through chat. The pipeline is [03 §4.1](spec/03-security-model.md); classification is [06 §4.2](spec/06-api-and-data-contracts.md).

## Where this came from

The verbatim spec series at `spec/01`–`09` designed the broker as part of the original three-tier fleet design (never merged): three agent tiers, per-agent broker pairs, and an `Agent` CRD that owned agent-pod rendering. Current `main` is a different world — a single `PlatformAgent` CRD, Hermes chat profiles, Slack and Google Chat ingress, and a credential-proxy sidecar that already enforces read-only kubectl (`CREDENTIAL_PROXY_ENFORCE_READ_ONLY`). This restart keeps the broker's decision machinery and journal contract and drops the fleet: what carries over and what does not is [architecture.md](architecture.md)'s subject, and the seams into the current world are [integration.md](integration.md)'s. One terminology note: "escalation" carries three senses. In this module it means an agent submitting a mutation to the broker. The fleet design also used `escalate` for a child agent's mesh call to its parent tier ([02 §2.3](spec/02-agent-personas.md)); that sense is dead here. A third sense is alive inside the module: the recovery ladder's verify-failure escalation, the page/pause records `escalate.Recorder` writes and `BrakeReconciler` consumes.

## Boundary

In: envelope intake and refusal (authentication, anti-replay, reserved keys), classification, undo planning, the brake, execution, verification, journaling, retention, the approval contract on `ActionRecord.status.approvals`, and the broker `Agent` CR as caller registration plus brake surface — tier, scope, `operations.paused`/`dryRunOnly`, `approvalRosterRef`, `changePolicyRefs`, `initiativeBudget`.

Out: agent-pod lifecycle. The module does not own its callers' Deployments. The pod-rendering code it carries (`internal/broker/controller/agent_manifests.go`, `pod_launcher.go`, `mesh_trust.go`, `pair_netpol.go`) is transitional — load-bearing for standalone testing today, dropped or replaced at integration because the shipping operator owns pod rendering for `PlatformAgent`. Also out: chat ingress and routing, which the Hermes chat stack owns, and the fleet design's tier hierarchy, cascade provisioning, and agent mesh.

## Status: built and not built

Built on this branch: the six CRDs; the broker binary and pipeline steps 1–11; the classifier, undo, brake, and budget machinery; the journal (`internal/journal`); the reconcilers (`AgentReconciler`, `BrakeReconciler`, `UndoReconciler`, `JournalReconciler`, `RetentionReconciler`); the two ValidatingAdmissionPolicies in `config/policy/vap-agent-scope-journal.yaml`; and now the approval-through-chat loop this document set designed: the notifier (`internal/broker/approval/notify`), the ChatOps gateway (`internal/broker/approval/gateway`, `cmd/chatops-gateway`), and the broker's own resumption/expiry loop (`internal/broker/pipeline/resume.go`, polled from `cmd/broker`, not watched — see chat-approval.md §2's note on why). Designed, not built:

- The reconcilers' runtime home, a module-owned controller manager. No entry point wires the five Agent-lifecycle reconcilers today (unrelated to the approval loop above, which needs none of them).
- A production Google Chat bearer verifier: v1 ships a shared-secret check (`gateway.SharedSecretVerifier`), not verification of Google's own signed JWT against its published JWKS — see `gateway.BearerVerifier`'s doc comment.
- `BodyStore` is also nil today (`wiring.go:312`).

Nothing in the shipping operator or agent path calls the module yet. What that means for a gated action, plus the wiring gaps above — architecture.md §8.

## The document set

[architecture.md](architecture.md) describes the module as it exists on this branch, with the decision machinery cited into the spec rather than restated. [chat-approval.md](chat-approval.md) is the new design work: the approval loop, designed against the current chat stack. [integration.md](integration.md) names the seams into the current world and the phased adoption plan. `spec/` is the verbatim frozen series — [spec/README.md](spec/README.md) is its provenance note, and `spec/00-series-readme.md` is the original series map.

Fact ownership: the spec owns the normative tables — the envelope schema, the risk-class rules, the brake rows, the undo strategy table, the reserved-key refusal table, the VAP writer table. These docs own design intent, deltas from the fleet design, and integration. A reader who wants the envelope schema or the risk rules reads 06, not these files. The spec files are frozen: code comments cite them by section, and `TestTheReservedKeyListIsTheOneTheSpecPublishes` (`k8s-operator/internal/broker/envelope_roundtrip_test.go:311`) parses 06 §4.1's refusal table as data. Corrections and deltas go in the live docs, never in `spec/`.

## Glossary

| Term            | Meaning                                                                                                                                                                                                                 |
| --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| action envelope | The JSON body an agent POSTs to the broker to request a mutation; `ActionEnvelope` on the wire. 06 §4.1.                                                                                                                |
| risk class      | The broker's computed classification of an action — `routine`, `elevated`, `gated`, or `forbidden` — never asserted by the caller. 06 §4.2.                                                                             |
| `gated`         | The class that parks an action as `PendingApproval` instead of executing it. 06 §4.2.                                                                                                                                   |
| ActionRecord    | The write-ahead journal record, one CR per action, whole-spec immutable after creation. 06 §4.3.                                                                                                                        |
| the brake       | Pause, freeze, undo, and contested, collectively — fail-closed human controls that live in code, never in a prompt. 06 §4.4.                                                                                            |
| roster          | An `ApprovalRoster`: the platform-prefixed principals who may release a `gated` action, with `minApprovals` counted over distinct principals and a TTL-clamped approval window. 06 §4.4.                                |
| actor identity  | The write-holding ServiceAccount, held only by the broker pod; the reader identity is the agent's read-only SA and never gains a write verb. [08 §2.2](spec/08-agent-runtime-and-identity.md).                          |
| escalation      | An agent's submission of a mutation to the broker — this module's sense, not the fleet design's mesh verb. 02 §2.3 (the dead sense).                                                                                    |
| shadow mode     | `Agent.spec.operations.dryRunOnly` forcing every submission to an effective dry run; an unreadable Agent CR produces the same effective dry run, fail-closed. `shadowed()`, `internal/broker/pipeline/pipeline.go:344`. |
| undo plan       | The per-verb reversal plan generated and dry-run-validated before execution; no plan means at least `gated`. 06 §4.3.1.                                                                                                 |
