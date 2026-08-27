# Chat approval for the action broker

The kube-agents action broker parks every `gated` action as `PendingApproval` and waits for a human ([04 §3](spec/04-workflow-model.md)). This document designs the loop that closes that gap — notification out to chat, approve/reject back in — against the Hermes chat stack that ships today, not against the original three-tier fleet design (never merged), whose C15 gateway ([05 §1.8](spec/05-system-architecture.md)) was specified and never built.

Five decisions are fixed. The loop is two components — an approval notifier for delivery and a ChatOps gateway for decision intake — shipped in v1 as one Deployment, one pod, one ServiceAccount. Approval means the phase flip `PendingApproval` → `Pending`, and the broker, never the gateway, resumes the action through its own pipeline. The components hold their own chat credentials in dedicated apps, in standalone deployment and at integration alike; what integration unifies is Secret management (integration.md §6). Delivery targets come from the roster, and a missing or unusable roster is never an open gate. The command surface is typed verbs first; buttons are a later convenience and never the authority.

## 1. What exists and what this doc adds

The contract this design must keep already compiles and is enforced on the branch:

- `ActionRecord.status.approvals` is writable only by `system:serviceaccount:kubeagents-system:kube-agents-chatops-gateway` (`config/policy/vap-agent-scope-journal.yaml:58`). Every other principal is denied, including a human cluster-admin; the normative writer table is [06 §4.3](spec/06-api-and-data-contracts.md).
- The gateway identity may move a record only from `PendingApproval` to `Pending` or `Rejected`, and may only _clear_ `status.contested`, never set it (`vap-agent-scope-journal.yaml:320–342`). It cannot touch `applied` or `verification` — it records a decision about an action; it does not get to describe what the action did.
- Roster semantics live in `k8s-operator/api/broker/v1alpha1/approvalroster_types.go`: `minApprovals` counts distinct principals (`EffectiveMinApprovals()`), `allowSelfApproval` defaults to false (four-eyes, `SelfApprovalAllowed()`), the approval TTL is clamped by `EffectiveTTL()` between the `MinApprovalTTL`/`DefaultApprovalTTL`/`MaxApprovalTTL` constants (per 06 §4.4), and `HasApprover` returns false on a nil roster, fail-closed.

What was missing when this document was first written — no `pipeline.ApprovalNotifier` implementation, no approve/reject chat verb, no gateway workload, no resumption loop — is built as of §6's inventory below; `cmd/broker/wiring.go:343`'s `Approvals: nil` comment is now stale and worth re-reading only as history of why the gateway identity previously existed solely as a principal string in the VAP. What remains genuinely absent is a production-grade Google Chat bearer verifier (§3) and the two chat apps' actual provisioning (§6).

## 2. The approval notifier

The approval notifier is `internal/broker/approval/notify` — a controller in `kubeagents-system` that watches ActionRecords in phase `PendingApproval` and delivers, refreshes, and resolves chat notifications. The in-pipeline seam it implements is small by design:

```go
type ApprovalNotifier interface {
	Notify(ctx context.Context, ar *agentv1alpha1.ActionRecord, roster *agentv1alpha1.ApprovalRoster) error
}
```

(`k8s-operator/internal/broker/pipeline/pipeline.go:168`.) The production implementation of `Notify` is a thin enqueue that never blocks step 7 — at most a nudge to the notifier's work queue. The watch loop is the delivery engine, because `PendingApproval` records also (re)appear after broker restarts and notifier outages, and a pipeline-synchronous notifier misses all of those.

For each parked record the notifier resolves the roster the same way the gateway will: `spec.agentRef` → the broker `Agent` CR → `spec.operations.approvalRosterRef` → the `ApprovalRoster`. Any broken link — missing ref, missing roster, unreadable object — is roster-unusable, sequence 4 below. The delivery target comes from the roster alone: `spec.notify.slack.channel` or `spec.notify.googleChat.space` (`ApprovalNotify`, `SlackNotify`, `GoogleChatNotify` in `approvalroster_types.go`). Absent notify config means no delivery; the action parks and expires.

The message is rendered from the record's structured fields only — `spec.intent`, `spec.classification.class` and its `reasons`, `spec.targets`, `status.approvals.required` and `expiresAt`, and the typed approve/reject commands carrying the action ID (`spec.actionId`, the ULID from which `journal.RecordName` derives the `ar-…` name). Chat text is rendered from the record, never the reverse — 06 §4.3's `report` principle — and the message's voice follows [02 §2.5](spec/02-agent-personas.md)'s gated-ask shape: the evidence, the risk class, who was asked, and the typed approve command. On notifier restart, undelivered `PendingApproval` records are re-rendered and re-delivered; when a record leaves `PendingApproval` the notifier edits the message to mark it resolved with the terminal outcome. Deliveries are deduplicated by an idempotence key — the record UID plus a generation of the rendered content (phase and approval counts) — so a flapping watch does not spam the channel.

RBAC: get/list/watch on `actionrecords`, `agents`, and `approvalrosters`. Delivery needs no status write, and delivery state does not go on the record: annotating an ActionRecord would require granting the notifier patch on `actionrecords`, a write grant on the journal that nothing else about delivery needs. Delivery bookkeeping (message timestamps, last-rendered key) lives in a notifier-owned ConfigMap in `kubeagents-system`. The VAP is not widened.

## 3. The ChatOps gateway

The ChatOps gateway is `internal/broker/approval/gateway` (the HTTP surface, `cmd/chatops-gateway`) — the one workload that runs as the SA the VAP already names, and the only writer of `status.approvals` in the system. It ships in one process with the notifier (`internal/broker/approval/notify` runs as a controller inside the same manager `cmd/chatops-gateway` starts) rather than a second container as first drafted here — one pod, one SA either way, so the boundary is the principal, not the process or container.

Ingress is the gateway's own, at v1 and at integration: a dedicated Slack app in socket mode, and a dedicated Google Chat app with its own Pub/Sub topic and subscription, mirroring the shapes of `SlackSpec` and `GoogleChatSpec` in the PlatformAgent CRD (`k8s-operator/api/v1alpha1/platformagent_types.go`) without depending on them. Sharing the install's apps does not work: Slack treats a second socket-mode connection on one app as an HA replica — each event goes to exactly one socket, so the gateway and the existing ingress would steal each other's messages — and a second subscription on the existing Google Chat topic would deliver all chat traffic, raw platform IDs included, to the gateway. Because the apps are separate, the Hermes planning agent never sees gateway verbs — approve and reject arrive only on the gateway's own app surfaces. Tokens live in a Secret in `kubeagents-system` held only by this pod. The command grammar is typed-first: `approve <action-id>` and `reject <action-id> [reason]`, as app-mention or slash-command verbs; `resume <agent>` and `uncontest <action-id>` are v2 of the chat surface, below. Block Kit buttons are an optional later convenience and, per 05 §1.8, never the authority — a click delivers an action ID and a claimed principal, and the gateway re-resolves the clicking principal from the platform-verified payload and runs the same authorization pipeline as a typed command. v1 ships typed verbs only.

The principal is extracted from the platform event as the immutable platform ID — Slack user ID → `slack:U…`, Google Chat → `googlechat:users/…` — never a display name or email, per 06 §1.2 V-11 and the existing `*_ALLOWED_USERS` convention. Then, per command:

resolve the record → resolve the roster (the §2 chain) → `HasApprover(principal)` → four-eyes: with `allowSelfApproval` false, the requester may not approve — `spec.requester.id` already carries the platform-qualified form (`ActionRequester`, `actionrecord_types.go`), so this is canonical string equality → count distinct granted principals against `EffectiveMinApprovals()` → check the TTL against `status.approvals.expiresAt` → write.

The writes match the VAP exactly. An approve or reject appends an `ApprovalEntry{Principal, At, Comment}` to `status.approvals.granted` or `.rejected`. When the distinct granted count reaches `minApprovals`, the same update sets phase `Pending`; a reject sets phase `Rejected`. The v1 Role is exactly this surface: get/list/watch on `actionrecords`, `approvalrosters`, and `agents`, and patch on `actionrecords/status` — nothing else, and no patch on `agents`. It is hand-written and kept off the manager role the way `config/rbac/brake_role.yaml` is. v1 is exactly the approval loop: park → notify → approve/reject → sanctioned status write → re-entry or rejection. `resume` and `uncontest` arrive with v2 of the chat surface: `uncontest` clears `status.contested` to false, and `resume` patches `Agent.spec.operations.paused` to false — roster-gated in the gateway because 06 §4.4's two-tier rule makes braking wide and proceeding roster-only. That addition is bounded by RBAC, not by the VAP: v2 adds patch on `agents` to the Role.

Expiry is never an approval, and the gateway never writes `Expired`. Marking overdue `PendingApproval` records `Expired` belongs to the broker's resumption loop (§4, sequence 3) — the owning broker may write phase under the VAP, and `PendingApproval → Expired` is a legal transition per `CanTransitionTo` (`actionrecord_phases.go`).

The resumption loop is the third piece and lives in the broker, not the gateway (`internal/broker/pipeline/resume.go`'s `Pipeline.Resume`, driven by `ResumeController`, both in the broker's own package). The gateway never executes anything; it flips phase. `cmd/broker` polls for the approval signature — phase `Pending` with a populated `status.approvals` block — every 5s rather than watching: a controller-runtime manager would give the broker's deliberately single-listener binary (`cmd/main.go`'s "ONE listener" invariant) a second port to disable, and the resumption loop's own latency budget, an hours-long TTL, has no use for sub-second wakeup. On a signal it re-runs the pipeline from classification, per 06 §4.4: re-classify against live state at approval time, refuse if the class rose, refuse if a target's uid no longer matches (`ActionRecord.spec.targets[].uid`, now back-filled from the live read at classification time — a small correctness fix to `stepResolve` this work required). Approval is permission, not a bypass. The same loop owns the TTL clock: a `PendingApproval` record past its deadline is moved to `Expired`, computed independently of whether `status.approvals` was ever written, since only the gateway may write it.

## 4. Sequences

1. **Happy path.** Gated envelope → broker parks the record `PendingApproval`, `Notify` enqueues → notifier resolves the roster and posts to `spec.notify`'s channel → an approver types `approve <action-id>` → gateway authorizes (roster, four-eyes, count, TTL) and the threshold update sets phase `Pending` → the broker's resumption loop re-classifies and executes through steps 8–11 → notifier edits the message: approved, executed, undo handle.
2. **Reject.** Any roster principal types `reject <action-id> [reason]` → gateway appends to `rejected` and sets phase `Rejected`, terminal → notifier edits the message and the reason lands in the thread. A re-raise is a new envelope.
3. **Expiry.** TTL passes with insufficient approvals → the broker's resumption loop marks the record `Expired`, terminal → notifier edits the message. An expired action is re-proposed, not resurrected (04 §3.1): resubmission re-enters the pipeline from the top.
4. **Roster unusable.** Missing `approvalRosterRef`, missing or unreadable roster, or empty approvers → no notification is sent, the record parks and expires. A missing roster is never an open gate — fail-closed row 6, 06 §4.4, and `HasApprover`'s nil-roster-false is the same rule in the gateway.
5. **Notification failure.** Slack down, token revoked, channel deleted → `Notify` returns an error and the action stays parked; the seam's doc comment (`pipeline.go:163–167`) is the normative statement — a broker that could not reach anyone and proceeded anyway has converted "waiting for a human" into "nobody was watching". The watch loop retries delivery with backoff; expiry still governs.

What the submitting agent tells its own requester at each of these outcomes — and how it follows a parked action to its terminal phase — is integration.md §2's caller outcome contract.

## 5. Identity and principal mapping

Roster principals use the same platform-prefixed grammar as the current world's `allowedUsers` and carry different authority: `allowedUsers` gates talking to the agent; the roster gates releasing a gated action. That is 06 §4.4's two-tier split — braking is wide, proceeding is roster-only — kept intact here.

One constraint on any integration-phase reuse of the existing relay path: the Hermes chat stack pseudonymises chat identities with a Session KV HMAC salt (`sessionKVSaltSecretRef`, `k8s-operator/api/v1alpha1/common_types.go`). That transform must not be applied on the approval path — the gateway compares raw platform IDs against the roster, and a pseudonymised principal matches nothing, which fails closed but makes approval permanently impossible through that path. This is one more reason the gateway keeps its own apps (§3) rather than teaching the relay to emit raw principals; integration.md §6 owns the seam.

Self-approval is the same principal string after canonicalization, compared against `spec.requester.id`. A requester with `attributionUnverified: true` still counts for the four-eyes denial: deny on match, never allow on doubt — unverified attribution weakens the record as evidence, not the gate.

## 6. Build inventory

Built: the notifier controller and the shared rendering/roster-resolution/authorization code in `internal/broker/approval` (and its `notify`/`gateway` subpackages); the gateway workload in `cmd/chatops-gateway`; the `kube-agents-chatops-gateway` ServiceAccount, ClusterRole, and namespaced Role/bindings in `config/chatops-gateway/` (a standalone kustomize base, deliberately not one of `config/default`'s resources — see that directory's `service_account.yaml` for why the name has to bypass the operator's `namePrefix`); the resumption and expiry loop inside the broker (`internal/broker/pipeline/resume.go`, `resume_controller.go`, polled from `cmd/broker/resume.go`); and the Deployment/Service manifests in `config/chatops-gateway/deployment.yaml`.

Still absent: the Slack/Google Chat token Secret objects themselves (the Deployment reads them as `optional: true` and the process warns and disables the affected route/platform if they are missing, rather than crash-looping); a production Google Chat JWT verifier (§3's `SharedSecretVerifier` is v1's); and provisioning the two chat apps, which is manual console steps documented alongside the module's deploy manifests, plus a second instance of the `chat-pubsub` Terraform module (`terraform/modules/chat-pubsub`) for the Google Chat topic and subscription.

## 7. Failure modes

Beyond sequence 5:

A compromised v1 gateway is bounded by the VAP and its Role: it can grant or reject approvals — flipping `PendingApproval` to `Pending` or `Rejected` — and clear `contested`, nothing else; with no patch on `agents` in v1, it cannot unpause an agent. v2's `resume` verb adds that, bounded by RBAC. It cannot touch `applied` or `verification`, cannot execute, and cannot approve its way past re-classification, because the broker re-runs the pipeline on resumption. The worst case is a wrongly released action that was already classified, snapshotted, undo-planned, and journaled.

Duplicate approvals do not lower the bar: the threshold is distinct principals, so the same approver typing `approve` twice counts once. Replayed chat events are deduplicated by platform event ID in the gateway before authorization runs. Two gateways would mean two socket connections and split ingress, so the Deployment is single-replica with a leader lease, echoing 05 §1.8's one-socket rule. Clock skew on the TTL is resolved in the broker's favor: the broker's clock governs expiry, the gateway treats `expiresAt` as advisory, and a gateway write that lands after the broker marked the record `Expired` fails at admission — the VAP's `oldPhase == 'PendingApproval'` guard makes late approvals fail closed.

A notifier posting to a channel the approvers cannot see is a misconfiguration the system survives (the action expires) but should surface; a roster status condition reporting last delivery outcome is the natural place. Optional, not required for v1.

## 8. Verification

Verification continues under [09 §§3–4 and §9.5](spec/09-verification-and-validation.md)'s discipline — stable IDs, lowest level that proves the property, gate classes, replace-never-delete — but the spec is frozen, so these checks cannot be added to 09's catalog. This doc holds the module's local check index for the approval loop, same ID discipline, `V-CHAT-nnn`. Gated-parks-and-does-not-execute is already V-GAT-003's property and is not renumbered here.

- V-CHAT-001 — a principal not on the roster is refused by the gateway, and the same write attempted directly is denied by the VAP.
- V-CHAT-002 — self-approval is refused when `allowSelfApproval` is false, including for an `attributionUnverified` requester.
- V-CHAT-003 — expiry is not approval: a record past its TTL becomes `Expired` and never executes, and a late approval write is rejected at admission.
- V-CHAT-004 — the VAP denies `status.approvals` writes from every other principal, including a human cluster-admin.
- V-CHAT-005 — notification failure leaves the gate closed: with delivery failing, the record stays `PendingApproval` and expires; nothing executes.
- V-CHAT-006 — an approved action is re-classified before execution; a risen class or a moved `preconditions.uid` refuses the resumption.
- V-CHAT-007 — the roster-unusable path sends nothing and opens nothing: broken ref, empty approvers, and unreadable roster all park and expire.

These land at integration.md §8's phases 2 and 3 — the notifier's checks gate phase 2, the gateway's and the resumption loop's gate phase 3 — before any write authority exists on the path they protect.
