/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
)

// ActionRecord is the durable journal entry — one per Action Envelope, created BEFORE the action is
// reported complete (03 §4.1 step 11) and, in the write-ahead ordering of 04 §1, before the mutation
// itself. It is namespaced and lives in the agent's own namespace so that `kubectl get actionrecords`
// works, admission can protect it, and the undo controller can watch it (06 §4.3).
//
// Two properties of this type are load-bearing and easy to lose in a refactor:
//
//  1. `spec` is immutable after creation. Every field carries a CEL `self == oldSelf` transition
//     rule, and `vap-agent-scope` separately denies `update` and `delete` on the main resource to
//     every agent identity — including the actor SA that created the record (06 §2.2.1). A journal
//     an actor can edit is not a journal.
//  2. `status` is not freely writable either. Who may write which field is a table, not a sentence
//     (06 §4.3), enforced by `vap-agent-scope-journal`. A human cluster-admin may write NOTHING
//     here: without that, any cluster-admin could mark their own gated action granted and execute
//     it, and four-eyes would be decorative.
//
// The `spec`-level CEL rules below are the ones a reviewer would otherwise have to take on trust:
// undo linkage in both directions, the two retention clocks ordered correctly, and the snapshot
// either inline or by reference but never both and never neither.

// ActionRiskClass is the outcome of the deterministic classifier (06 §4.2). The ordering
// routine < elevated < gated < forbidden is the one the `max over inputs` rule uses; `forbidden`
// short-circuits and never reaches execution.
// +kubebuilder:validation:Enum=routine;elevated;gated;forbidden
type ActionRiskClass string

const (
	// RiskRoutine is reversible, low blast radius, inside the agent's own scope.
	RiskRoutine ActionRiskClass = "routine"
	// RiskElevated is consequential but still automatic.
	RiskElevated ActionRiskClass = "elevated"
	// RiskGated requires an approval from the roster before anything is written.
	RiskGated ActionRiskClass = "gated"
	// RiskForbidden is refused outright; the record exists as security evidence.
	RiskForbidden ActionRiskClass = "forbidden"
)

// ActionTriggerSource is what caused the action. It is recorded, never an authority input: a forged
// trigger mislabels a query and grants nothing (06 §4.3).
// +kubebuilder:validation:Enum=chat;watch;alert;cron;delegation;escalation;undo
type ActionTriggerSource string

// ActionTriggerUndo is the one trigger source that requires `undoOf` to be set, and the only one
// that makes this record the reverse of another.
const ActionTriggerUndo ActionTriggerSource = "undo"

// ActionPhase is the status lifecycle of 06 §4.3. Ten phases, six of them terminal:
//
//	                       ┌──────────► Rejected
//	                       │
//	Pending ──► PendingApproval ──► Executing ──► Verified ──► Undone
//	   │              │  │              │              │
//	   │              │  └► Expired     └► Failed ──► RolledBack
//	   │              └► Rejected
//	   └────────────────────────────► Executing   (routine / elevated: no gate)
//
// DryRun is terminal and is reached from Pending when spec.dryRun is true — the whole of Phase 9
// runs here, which is why it is a first-class phase and not an absence of one.
// +kubebuilder:validation:Enum=Pending;PendingApproval;Executing;Verified;Failed;RolledBack;Undone;Rejected;Expired;DryRun
type ActionPhase string

const (
	// PhasePending means accepted, classified, undo plan generated; not yet executing.
	PhasePending ActionPhase = "Pending"
	// PhasePendingApproval means gated and awaiting the roster. Nothing has been written.
	PhasePendingApproval ActionPhase = "PendingApproval"
	// PhaseExecuting means the snapshot is taken and server-side apply is in progress.
	PhaseExecuting ActionPhase = "Executing"
	// PhaseVerified means executed AND the intended outcome confirmed.
	PhaseVerified ActionPhase = "Verified"
	// PhaseFailed means execution errored; partial work rolled back where possible.
	PhaseFailed ActionPhase = "Failed"
	// PhaseRolledBack means executed, verification failed, pre-state automatically restored.
	PhaseRolledBack ActionPhase = "RolledBack"
	// PhaseUndone means a human ran undo and the plan replayed successfully.
	PhaseUndone ActionPhase = "Undone"
	// PhaseRejected means refused before execution.
	PhaseRejected ActionPhase = "Rejected"
	// PhaseExpired means a gated action whose approval TTL elapsed.
	PhaseExpired ActionPhase = "Expired"
	// PhaseDryRun means classified, planned, journaled, and deliberately not executed.
	PhaseDryRun ActionPhase = "DryRun"
)

// UndoStrategy is the inverse operation chosen by the 06 §4.3.1 strategy table.
// `none` is not a failure to try — it is the honest answer for an operation with no safe inverse,
// and it forces the classification to at least `gated`.
// +kubebuilder:validation:Enum=delete;restore;recreate;inverse;none
type UndoStrategy string

const (
	// UndoDelete reverses a create.
	UndoDelete UndoStrategy = "delete"
	// UndoRestore reverses an apply/patch/scale over an object that already existed.
	UndoRestore UndoStrategy = "restore"
	// UndoRecreate reverses a delete. Downgraded to `none` when inbound references exist.
	UndoRecreate UndoStrategy = "recreate"
	// UndoInverse reverses a cloud-provider operation with its documented inverse.
	UndoInverse UndoStrategy = "inverse"
	// UndoNone means no safe inverse could be generated; the action classifies at least gated.
	UndoNone UndoStrategy = "none"
)

// RequesterKind distinguishes a human principal from a machine one. `attributionUnverified` on the
// spec is what says whether the claim was signed, and the two are deliberately separate fields:
// an unsigned human claim is still recorded, just marked (06 §8).
// +kubebuilder:validation:Enum=human;system;agent
type RequesterKind string

// ActionRequester is who asked for the action. Identity here is evidence, never authority — the
// broker derives (tier, scope) from the authenticated caller and never from this body (06 §4.1).
type ActionRequester struct {
	// Kind distinguishes a human from a system or agent principal.
	// +kubebuilder:validation:Required
	Kind RequesterKind `json:"kind"`

	// ID is the platform-qualified principal, e.g. slack:U02ABCDEF.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	ID string `json:"id"`

	// Platform is the chat or automation platform the request arrived on.
	// +optional
	Platform string `json:"platform,omitempty"`

	// DisplayName is a human-readable label. Never matched against; display only.
	// +optional
	DisplayName string `json:"displayName,omitempty"`
}

// ActionTrigger records what caused the action, and carries the two identifiers that make a
// delegation reconstructable: `undoOf` (forward undo linkage) and `chainId` (causation).
// +kubebuilder:validation:XValidation:rule="self.source == 'undo' ? (has(self.undoOf) && size(self.undoOf) > 0) : (!has(self.undoOf) || size(self.undoOf) == 0)",message="spec.trigger.undoOf is required and non-empty exactly when spec.trigger.source is undo (06 §4.3 undo linkage)"
type ActionTrigger struct {
	// Source is what caused the action.
	// +kubebuilder:validation:Required
	Source ActionTriggerSource `json:"source"`

	// Ref is the object or signal that triggered it, e.g. pod/api-gateway-7d9c-4kk2.
	// +optional
	Ref string `json:"ref,omitempty"`

	// Detail is the human-readable trigger condition, e.g. CrashLoopBackOff x7/10m.
	// +optional
	Detail string `json:"detail,omitempty"`

	// UndoOf is the actionId this action reverts. Required and non-empty iff source is undo.
	// It lives in spec, so it is immutable and survives deletion of the original — which is the
	// whole reason the linkage is bidirectional rather than status-only (06 §4.3).
	// +optional
	UndoOf string `json:"undoOf,omitempty"`

	// ChainID is the ULID naming the delegation this action belongs to. An agent that starts a
	// chain sets it to its own actionId, so there is no empty case and no allocator.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	ChainID string `json:"chainId"`
}

// ActionTrace correlates the action with the telemetry pipeline. traceId is the key for LATENCY;
// spec.trigger.chainId is the key for CAUSATION, and they are deliberately separate — a retried
// mesh call gets a new trace and keeps its chain (06 §4.3).
type ActionTrace struct {
	// TraceID is the W3C trace identifier.
	// +optional
	TraceID string `json:"traceId,omitempty"`

	// SpanID is the originating span.
	// +optional
	SpanID string `json:"spanId,omitempty"`

	// SessionID is the harness session the action came from.
	// +optional
	SessionID string `json:"sessionId,omitempty"`
}

// ClassificationReason is one rule that fired, with the class it contributed. The classifier is
// table-driven and deterministic, so this list is a complete explanation of the outcome — that is
// what makes a classification reviewable rather than an opinion (06 §4.2).
type ClassificationReason struct {
	// Rule is the stable identifier of the rule that fired, e.g. production-environment.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Rule string `json:"rule"`

	// Class is the contribution: a class name, or the literal +1 for a one-step escalation.
	// +kubebuilder:validation:Required
	Class string `json:"class"`

	// Detail is the concrete evidence, e.g. the label that matched.
	// +optional
	Detail string `json:"detail,omitempty"`
}

// BlastRadius is the size of the change, measured before execution. `cap` is the ceiling that
// applied; exceeding the hard cap aborts rather than escalates (06 §4.2).
type BlastRadius struct {
	// Objects is the number of distinct objects the envelope would mutate, after selector fan-out.
	// +kubebuilder:validation:Minimum=0
	Objects int32 `json:"objects"`

	// FractionOfScope is objects divided by the size of the agent's scope. Above 0.5 aborts.
	// +optional
	FractionOfScope string `json:"fractionOfScope,omitempty"`

	// Cap is the blast-radius ceiling that was in force for this action.
	// +kubebuilder:validation:Minimum=0
	// +optional
	Cap int32 `json:"cap,omitempty"`
}

// ActionClassification is the verbatim output of the 06 §4.2 classifier. It is recorded rather than
// recomputed at read time on purpose: the classifier reads live cluster state, so a replay months
// later would not reproduce the decision that was actually made.
type ActionClassification struct {
	// Class is the final risk class after the full six-step evaluation order.
	// +kubebuilder:validation:Required
	Class ActionRiskClass `json:"class"`

	// Reasons is every rule that fired, in evaluation order.
	// +optional
	// +listType=atomic
	Reasons []ClassificationReason `json:"reasons,omitempty"`

	// BlastRadius is the measured size of the change.
	// +optional
	BlastRadius *BlastRadius `json:"blastRadius,omitempty"`

	// Undoable records whether an undo plan could be generated. False forces class >= gated.
	Undoable bool `json:"undoable"`

	// PolicySources names every input that contributed, e.g. code-floor and any ChangePolicy.
	// +optional
	// +listType=atomic
	PolicySources []string `json:"policySources,omitempty"`
}

// TargetRef identifies one object the action operates on, pinned by uid and resourceVersion at
// classification time. The uid is what an undo precondition checks: replaying a restore against a
// different object that happens to share a name is the failure the pin exists to prevent.
type TargetRef struct {
	// Group is the API group, empty for core.
	// +optional
	Group string `json:"group,omitempty"`

	// Version is the API version.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Version string `json:"version"`

	// Kind is the object kind.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Kind string `json:"kind"`

	// Namespace is empty for cluster-scoped targets.
	// +optional
	Namespace string `json:"namespace,omitempty"`

	// Name is the object name.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`

	// UID pins the object identity at classification time.
	// +optional
	UID string `json:"uid,omitempty"`

	// ResourceVersion pins the observed version at classification time.
	// +optional
	ResourceVersion string `json:"resourceVersion,omitempty"`
}

// ObjectStoreRef points at a snapshot held outside the CR. Above 1 MiB the object body moves to the
// journal store and the CR keeps the digest only; the broker verifies that digest on undo and
// refuses to replay a snapshot that does not match (06 §4.3).
type ObjectStoreRef struct {
	// Store names the journal store backend that holds the body.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Store string `json:"store"`

	// Key is the store-scoped identifier of the body.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Key string `json:"key"`

	// SHA256 is the lower-hex digest of the stored body.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Pattern=`^[0-9a-f]{64}$`
	SHA256 string `json:"sha256"`
}

// PreStateSnapshot is what one target looked like before the action, sanitized. Exactly one of
// `object` and `objectRef` is set: a snapshot that is neither is a record that cannot be undone
// while claiming it can, which is the specific dishonesty the CEL rule below rejects.
//
// If the snapshot cannot be persisted at all, the action does not execute — fail-closed, the same
// rule as journalling (03 §6).
// +kubebuilder:validation:XValidation:rule="has(self.object) != has(self.objectRef)",message="preState entry must carry exactly one of object (inline) or objectRef (>1 MiB, stored out of band) — 06 §4.3 large snapshots"
type PreStateSnapshot struct {
	// TargetIndex is the position in spec.targets this snapshot belongs to.
	// +kubebuilder:validation:Minimum=0
	TargetIndex int32 `json:"targetIndex"`

	// CapturedAt is when the snapshot was taken, inside the broker at step 8.
	// +kubebuilder:validation:Required
	CapturedAt metav1.Time `json:"capturedAt"`

	// Object is the sanitized object body, inline. Set only when the body is at most 1 MiB.
	// +optional
	// +kubebuilder:pruning:PreserveUnknownFields
	Object *runtime.RawExtension `json:"object,omitempty"`

	// ObjectRef points at the body in the journal store. Set only when the body exceeds 1 MiB.
	// +optional
	ObjectRef *ObjectStoreRef `json:"objectRef,omitempty"`

	// SHA256 is the digest of the sanitized body, whether inline or stored.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Pattern=`^[0-9a-f]{64}$`
	SHA256 string `json:"sha256"`
}

// UndoPrecondition is what must still be true for an undo step to be safe to replay.
type UndoPrecondition struct {
	// UID refuses the undo if the object was replaced meanwhile.
	// +optional
	UID string `json:"uid,omitempty"`

	// ResourceVersion optionally pins the version as well, for steps that require it.
	// +optional
	ResourceVersion string `json:"resourceVersion,omitempty"`
}

// UndoStep is one operation in the undo plan. Each is dry-run against the API server when the plan
// is generated, which is what `UndoPlan.Validated` records.
type UndoStep struct {
	// Op is the operation to perform, e.g. apply, delete, create, scale.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Op string `json:"op"`

	// Target is the object the step acts on.
	// +kubebuilder:validation:Required
	Target TargetRef `json:"target"`

	// Object is the sanitized body to apply or recreate, where the op needs one.
	// +optional
	// +kubebuilder:pruning:PreserveUnknownFields
	Object *runtime.RawExtension `json:"object,omitempty"`

	// ObjectRef points at the body in the journal store for large snapshots.
	// +optional
	ObjectRef *ObjectStoreRef `json:"objectRef,omitempty"`

	// Preconditions must hold at replay time or the step refuses.
	// +optional
	Preconditions *UndoPrecondition `json:"preconditions,omitempty"`
}

// UndoPlan is generated at step 6, BEFORE execution (06 §4.3.1). Generating it afterwards would
// mean discovering that an action is irreversible only once it is already done.
//
// The CEL rule ties strategy to content: any strategy other than `none` must carry at least one
// step. A plan that claims a strategy and has nothing to replay is the shape a reviewer trusts and
// an operator finds out about at the worst moment.
// +kubebuilder:validation:XValidation:rule="self.strategy == 'none' || (has(self.steps) && size(self.steps) > 0)",message="an undo plan with a strategy other than none must contain at least one step (06 §4.3.1)"
type UndoPlan struct {
	// Strategy is the inverse chosen by the 06 §4.3.1 table.
	// +kubebuilder:validation:Required
	Strategy UndoStrategy `json:"strategy"`

	// GeneratedAt is when the plan was produced — before execution, always.
	// +kubebuilder:validation:Required
	GeneratedAt metav1.Time `json:"generatedAt"`

	// Validated records whether every step dry-ran cleanly against the API server.
	Validated bool `json:"validated"`

	// Steps is the ordered plan.
	// +optional
	// +listType=atomic
	Steps []UndoStep `json:"steps,omitempty"`

	// Caveats states what the undo will NOT restore, e.g. pods replaced by a rollout. Recorded so
	// the promise made to a human is the one the plan can keep.
	// +optional
	// +listType=atomic
	Caveats []string `json:"caveats,omitempty"`
}

// RetentionSpec carries TWO independent clocks, and conflating them is the mistake it exists to
// prevent (06 §4.3). `ttl` is how long the RECORD lives. `undoWindow` is how long undo is PROMISED,
// and it is deliberately shorter: the snapshot a restore replays goes stale, so a 90-day-old routine
// undo is a plausible-looking action that quietly restores the wrong world. Keeping the record while
// withdrawing the promise is the honest arrangement.
//
//	routine  30d / 7d   ·  elevated 90d / 30d  ·  gated 365d / 90d  ·  Rejected 365d / n/a
//
// +kubebuilder:validation:XValidation:rule="self.undoWindowExpiresAt <= self.expiresAt",message="retention.undoWindowExpiresAt must be at or before retention.expiresAt — the undo promise may never outlive the record (06 §4.3)"
type RetentionSpec struct {
	// Class is the final risk class the clocks were derived from.
	// +kubebuilder:validation:Required
	Class ActionRiskClass `json:"class"`

	// TTL is how long the record lives. A ChangePolicy may LENGTHEN it — stricter-only in the audit
	// direction — and may never shorten it.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Pattern=`^[0-9]+h$`
	TTL string `json:"ttl"`

	// ExpiresAt is submitted + ttl. The retention controller deletes on this, and only after the
	// exporter has confirmed the record landed in the audit sink.
	// +kubebuilder:validation:Required
	ExpiresAt metav1.Time `json:"expiresAt"`

	// UndoWindow is how long undo is promised. Must be at most ttl.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Pattern=`^[0-9]+h$`
	UndoWindow string `json:"undoWindow"`

	// UndoWindowExpiresAt is executionEnded + undoWindow, falling back to submitted for records that
	// never executed. Past it, the undo controller REFUSES rather than errors: the record is still
	// there and a human may reconstruct the change by hand — the system just stops claiming one
	// command will do it correctly.
	// +kubebuilder:validation:Required
	UndoWindowExpiresAt metav1.Time `json:"undoWindowExpiresAt"`
}

// AgentObjectRef names an Agent CR.
type AgentObjectRef struct {
	// Name of the Agent CR.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`

	// Namespace of the Agent CR.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Namespace string `json:"namespace"`
}

// ActionRecordSpec is IMMUTABLE after creation. Every field below carries a transition rule, and
// the object-level rule is the backstop for fields added later without one.
// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="ActionRecord.spec is immutable after creation — the journal is append-only (06 §4.3)"
type ActionRecordSpec struct {
	// ActionID is the uppercase ULID naming this action. metadata.name is "ar-" + its lowercase form.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Pattern=`^[0-9A-HJKMNP-TV-Z]{26}$`
	ActionID string `json:"actionId"`

	// AgentRef is the Agent CR this action was taken by.
	// +kubebuilder:validation:Required
	AgentRef AgentObjectRef `json:"agentRef"`

	// AgentIdentity is the (tier, scope) key, e.g. developer-team/my-project/cluster-a/team-x.
	// The broker may only write status on records whose identity equals its own derived identity.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	AgentIdentity string `json:"agentIdentity"`

	// ActorServiceAccount is who actually wrote — the actor SA, not the reader.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	ActorServiceAccount string `json:"actorServiceAccount"`

	// Requester is who asked.
	// +kubebuilder:validation:Required
	Requester ActionRequester `json:"requester"`

	// AttributionUnverified is true when no signed requester assertion was present. The action is
	// still recorded — it is marked, not discarded, because an unattributed change that happened is
	// more useful to an investigator than a gap (06 §8).
	AttributionUnverified bool `json:"attributionUnverified"`

	// Trigger is what caused the action.
	// +kubebuilder:validation:Required
	Trigger ActionTrigger `json:"trigger"`

	// Trace correlates with the telemetry pipeline.
	// +optional
	Trace *ActionTrace `json:"trace,omitempty"`

	// Intent is the one-line statement of what the action was for.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Intent string `json:"intent"`

	// Rationale is recorded and is NEVER a classification input. The classifier reads live cluster
	// state, never prose — otherwise an agent could argue its way down a risk class (06 §4.2).
	// +optional
	Rationale string `json:"rationale,omitempty"`

	// IdempotencyKey is "sha256:" + lowerhex(SHA-256(JCS(K))), recomputed by the broker from the
	// canonicalized envelope and never trusted from the body (06 §4.1).
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Pattern=`^sha256:[0-9a-f]{64}$`
	IdempotencyKey string `json:"idempotencyKey"`

	// DryRun means the action was classified, planned and journaled but deliberately not executed.
	// The whole of Phase 9 runs this way.
	DryRun bool `json:"dryRun"`

	// Classification is the verbatim classifier output.
	// +kubebuilder:validation:Required
	Classification ActionClassification `json:"classification"`

	// Targets is every object the action operates on, after selector fan-out.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinItems=1
	// +listType=atomic
	Targets []TargetRef `json:"targets"`

	// PreState is the snapshot of every target. Absent on records that never reached Executing.
	// +optional
	// +listType=atomic
	PreState []PreStateSnapshot `json:"preState,omitempty"`

	// Undo is the plan generated before execution.
	// +optional
	Undo *UndoPlan `json:"undo,omitempty"`

	// Retention carries the two clocks.
	// +kubebuilder:validation:Required
	Retention RetentionSpec `json:"retention"`
}

// AppliedDiffOp is one entry of the normalized JSON-patch of what actually changed on the server.
//
// NOTE the path dialect. `path` here is an RFC 6901 JSON Pointer (slash-separated, `~1` escaping),
// which is NOT the dotted relaxed JSONPath used by `ChangePolicy.when.fieldPaths`. The two dialects
// live one screen apart in 06 §4.2 and are the most likely thing to be cross-wired; a `fieldPaths`
// entry beginning with a slash is rejected at admission for exactly this reason.
type AppliedDiffOp struct {
	// Op is the JSON-patch operation: add, remove, or replace.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Op string `json:"op"`

	// Path is an RFC 6901 JSON Pointer into the object.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Path string `json:"path"`

	// From is the previous value, rendered as a string.
	// +optional
	From string `json:"from,omitempty"`

	// Value is the new value, rendered as a string.
	// +optional
	Value string `json:"value,omitempty"`
}

// AppliedTarget is what actually happened to one target.
type AppliedTarget struct {
	// TargetIndex is the position in spec.targets.
	// +kubebuilder:validation:Minimum=0
	TargetIndex int32 `json:"targetIndex"`

	// Diff is the normalized JSON-patch the server actually accepted.
	// +optional
	// +listType=atomic
	Diff []AppliedDiffOp `json:"diff,omitempty"`

	// ResourceVersionAfter is the object version after the apply.
	// +optional
	ResourceVersionAfter string `json:"resourceVersionAfter,omitempty"`
}

// VerificationCheck is one named post-condition and its outcome.
type VerificationCheck struct {
	// Name is the stable check identifier, e.g. rollout-complete.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`

	// Passed is the outcome.
	Passed bool `json:"passed"`

	// Detail is the evidence, e.g. 1/1 updated replicas available.
	// +optional
	Detail string `json:"detail,omitempty"`
}

// ActionVerification is the per-kind outcome confirmation of 04 §5.1. `Verified` means executed AND
// confirmed; without this block the phase would only mean "the apply returned 200".
type ActionVerification struct {
	// Passed is the conjunction of every check.
	Passed bool `json:"passed"`

	// CompletedAt is when verification finished.
	// +optional
	CompletedAt *metav1.Time `json:"completedAt,omitempty"`

	// Checks is every predicate that ran.
	// +optional
	// +listType=atomic
	Checks []VerificationCheck `json:"checks,omitempty"`
}

// RecoveryTransition is one movement on the recovery ladder. A SKIPPED rung must carry a reason —
// that is what makes "never skips a rung silently" (04 §5) checkable rather than aspirational.
type RecoveryTransition struct {
	// At is when the transition happened.
	// +kubebuilder:validation:Required
	At metav1.Time `json:"at"`

	// From is the rung left.
	// +kubebuilder:validation:Minimum=0
	From int32 `json:"from"`

	// To is the rung entered.
	// +kubebuilder:validation:Minimum=0
	To int32 `json:"to"`

	// Reason is why. Mandatory in practice for any transition that skips a rung.
	// +optional
	Reason string `json:"reason,omitempty"`
}

// ActionRecovery makes the 04 §5 ladder observable: 1 retry, 2 alternative, 3 rollback, 4 escalate,
// 5 page. `transitions` is append-only and non-decreasing in rung.
type ActionRecovery struct {
	// Rung is the current position on the ladder.
	// +kubebuilder:validation:Minimum=0
	// +kubebuilder:validation:Maximum=5
	Rung int32 `json:"rung"`

	// Transitions is the append-only history.
	// +optional
	// +listType=atomic
	Transitions []RecoveryTransition `json:"transitions,omitempty"`
}

// ActionReport is the four beats of 02 §2.5.4 as STRUCTURED fields, and the chat text is rendered
// FROM it — never the reverse. This is what makes the honesty requirement mechanically checkable: a
// report claiming a fix can be compared directly against status.verification, and a missing beat is
// a schema failure rather than a matter of interpretation. An implementation that emits chat prose
// and derives these fields afterwards is non-conforming, because the two can then disagree.
type ActionReport struct {
	// Noticed is what the agent observed.
	// +optional
	Noticed string `json:"noticed,omitempty"`

	// Did is what it changed.
	// +optional
	Did string `json:"did,omitempty"`

	// Verified is the evidence the change worked.
	// +optional
	Verified string `json:"verified,omitempty"`

	// Undo is the exact command a human can run to reverse it.
	// +optional
	Undo string `json:"undo,omitempty"`
}

// ApprovalEntry is one roster decision, recorded with who and when.
type ApprovalEntry struct {
	// Principal is the approving or rejecting identity.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Principal string `json:"principal"`

	// At is when the decision was recorded.
	// +kubebuilder:validation:Required
	At metav1.Time `json:"at"`

	// Comment is optional free text from the approver.
	// +optional
	Comment string `json:"comment,omitempty"`
}

// ActionApprovals is present only for gated actions. Only the ChatOps gateway SA may write it, and
// it enforces the roster, four-eyes and minApprovals before doing so — which is why a human
// cluster-admin may not patch this subresource by hand (06 §4.3, §4.4).
type ActionApprovals struct {
	// Required is the number of distinct approvals needed.
	// +kubebuilder:validation:Minimum=0
	Required int32 `json:"required"`

	// Granted is who approved.
	// +optional
	// +listType=atomic
	Granted []ApprovalEntry `json:"granted,omitempty"`

	// Rejected is who rejected.
	// +optional
	// +listType=atomic
	Rejected []ApprovalEntry `json:"rejected,omitempty"`

	// ExpiresAt is when the approval TTL elapses, after which the action becomes Expired.
	// +optional
	ExpiresAt *metav1.Time `json:"expiresAt,omitempty"`
}

// ActionTimestamps is the lifecycle clock. Nil means the phase was never reached.
type ActionTimestamps struct {
	// Submitted is when the envelope was accepted.
	// +optional
	Submitted *metav1.Time `json:"submitted,omitempty"`

	// Classified is when the classifier returned.
	// +optional
	Classified *metav1.Time `json:"classified,omitempty"`

	// Approved is when the roster was satisfied. Nil for non-gated actions.
	// +optional
	Approved *metav1.Time `json:"approved,omitempty"`

	// ExecutionStarted is when the first mutating call was issued.
	// +optional
	ExecutionStarted *metav1.Time `json:"executionStarted,omitempty"`

	// ExecutionEnded is when the last mutating call returned. It is the base for undoWindowExpiresAt.
	// +optional
	ExecutionEnded *metav1.Time `json:"executionEnded,omitempty"`

	// Verified is when verification completed.
	// +optional
	Verified *metav1.Time `json:"verified,omitempty"`
}

// ActionRecordStatus is written by a closed set of principals, each restricted to a subset of these
// fields (06 §4.3). The Go type cannot express that — it is enforced by `vap-agent-scope-journal` —
// so the table is reproduced here beside the fields it governs:
//
//	the owning broker SA      phase, observedGeneration, applied, verification, recovery, report,
//	                          timestamps, message  — and only on its own agentIdentity. NEVER
//	                          approvals, contested, or undoneBy.
//	the undo controller       phase (to Undone only), undoneBy, contested, message — any namespace,
//	                          because undo must work for an agent that no longer exists.
//	the ChatOps gateway       approvals, phase (PendingApproval to Pending/Rejected), contested
//	                          (clear only).
//	the retention controller  nothing; delete only, and only post-export.
//	every reader SA           nothing.
//	a human cluster-admin     NOTHING. Deliberate: otherwise four-eyes is decorative.
type ActionRecordStatus struct {
	// Phase is the lifecycle position.
	// +optional
	Phase ActionPhase `json:"phase,omitempty"`

	// ObservedGeneration is the spec generation this status reflects.
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// Applied is what actually changed, per target.
	// +optional
	// +listType=atomic
	Applied []AppliedTarget `json:"applied,omitempty"`

	// Verification is the outcome confirmation.
	// +optional
	Verification *ActionVerification `json:"verification,omitempty"`

	// Recovery is the observable recovery ladder.
	// +optional
	Recovery *ActionRecovery `json:"recovery,omitempty"`

	// Report is the four beats, structured.
	// +optional
	Report *ActionReport `json:"report,omitempty"`

	// Escalation is rung 5: what the broker asked for, and what C-BR did about it.
	// +optional
	Escalation *ActionEscalation `json:"escalation,omitempty"`

	// Approvals is present only for gated actions.
	// +optional
	Approvals *ActionApprovals `json:"approvals,omitempty"`

	// Contested is set true when a human undoes or manually reverts this change (06 §4.4). It is the
	// signal that the agent and a human disagreed, and it is an input to the trust metrics.
	// +optional
	Contested bool `json:"contested,omitempty"`

	// UndoneBy is the actionId of the undo action, once executed — the reverse half of the
	// bidirectional linkage. It answers "was this ever undone?", the question a human asks before
	// re-attempting a fix.
	// +optional
	UndoneBy string `json:"undoneBy,omitempty"`

	// Timestamps is the lifecycle clock.
	// +optional
	Timestamps *ActionTimestamps `json:"timestamps,omitempty"`

	// Message is the one-line human summary.
	// +optional
	Message string `json:"message,omitempty"`

	// Exported records that the audit exporter has confirmed this record landed in the durable sink.
	// The retention controller may delete ONLY when this is set and expiresAt has passed: the
	// exported journal, not the CR, is the system of record, so deleting before export destroys the
	// evidence rather than aging it out (06 §4.3, 05 §1.2).
	// +optional
	Exported *ExportStatus `json:"exported,omitempty"`
}

// ExportStatus is the audit exporter's receipt. `sink` names where it landed so a reviewer can go
// and look, rather than trusting a boolean.
type ExportStatus struct {
	// Confirmed is true once the sink has acknowledged the record.
	Confirmed bool `json:"confirmed"`

	// At is when the acknowledgement was received.
	// +optional
	At *metav1.Time `json:"at,omitempty"`

	// Sink names the destination, e.g. cloud-logging or the journal repository.
	// +optional
	Sink string `json:"sink,omitempty"`
}

// ActionEscalation is rung 5 of the 04 §5.1 ladder, written down where somebody who can act on it
// will see it. It has TWO writers and they are deliberately in the same struct.
//
// The broker writes the REQUEST half (`pageRequested`, `pauseRequested`, `reason`, `requestedAt`)
// and can do nothing else about it. 06 §2.2.1 gives the broker's operations grant `get, list, watch`
// on `agents` and no verb at all on `events`: it cannot pause an agent, because that is a write to
// an `Agent`, and it cannot page, because that is an Event. V-BRK-013 asserts that grant exactly and
// is BLOCKING-ALWAYS, so the shape where the broker pauses directly is not one an implementation may
// reach for. What the broker CAN write is `actionrecords/status`, which it already must — so the
// escalation is recorded in the journal, and the brake surface (`C-BR`, 05 §1.5) fans it out from
// the operator's identity through the single stop path 05 §1.7 already names: "exactly one code path
// that stops an agent".
//
// The controller writes the FULFILMENT half (`pagedAt`, `pausedAt`, `failure`), which is what makes
// the promise auditable rather than aspirational. A request with no fulfilment after the fact is a
// visible, queryable defect; a page that was attempted and failed says so in `failure` instead of
// disappearing. Same two-writer shape as `exported`, and for the same reason: the receipt belongs
// next to the thing it is a receipt for.
//
// Requests are idempotent and monotone. A rung can only be climbed once per action (04 §5's ladder
// is non-decreasing), so a second escalation on the same record is a bug in the caller, not a
// retry — the fields are set, never accumulated.
type ActionEscalation struct {
	// PageRequested records that rung 5 asked for a human. It is separate from PauseRequested
	// because 05 §1.5's auto-brake table is explicit that the two are different responses to
	// different classes of trouble: a budget exhaustion escalates WITHOUT pausing, and conflating
	// them gives you an agent that stops working every busy afternoon.
	// +optional
	PageRequested bool `json:"pageRequested,omitempty"`

	// PauseRequested records that rung 5 asked for the brake.
	// +optional
	PauseRequested bool `json:"pauseRequested,omitempty"`

	// Reason is the one-line cause, carried through to `Agent.spec.operations.pauseReason` so the
	// human running `resume` can see what stopped it. Bounded because it is echoed into a spec
	// field with its own length limit; the broker truncates rather than failing the escalation,
	// since a pause that does not happen because its reason was long is the wrong trade.
	// +kubebuilder:validation:MaxLength=512
	// +optional
	Reason string `json:"reason,omitempty"`

	// RequestedAt is when the broker recorded the escalation.
	// +optional
	RequestedAt *metav1.Time `json:"requestedAt,omitempty"`

	// PagedAt is when C-BR delivered the page. Written by the controller.
	// +optional
	PagedAt *metav1.Time `json:"pagedAt,omitempty"`

	// PausedAt is when C-BR set `spec.operations.paused` on the agent. Written by the controller.
	// +optional
	PausedAt *metav1.Time `json:"pausedAt,omitempty"`

	// Failure is what went wrong in the fan-out, if anything. Written by the controller. An empty
	// Failure alongside a request with no timestamps means the fan-out has not run yet, which is a
	// different state from one that ran and failed — and telling them apart is the whole reason
	// this is a string rather than a boolean.
	// +kubebuilder:validation:MaxLength=1024
	// +optional
	Failure string `json:"failure,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=ar,categories=kube-agents
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="Class",type=string,JSONPath=`.spec.classification.class`
// +kubebuilder:printcolumn:name="Identity",type=string,JSONPath=`.spec.agentIdentity`
// +kubebuilder:printcolumn:name="DryRun",type=boolean,JSONPath=`.spec.dryRun`
// +kubebuilder:printcolumn:name="Undoable",type=boolean,JSONPath=`.spec.classification.undoable`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// ActionRecord is the append-only journal entry for one action.
type ActionRecord struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	// +kubebuilder:validation:Required
	Spec ActionRecordSpec `json:"spec"`

	// +optional
	Status ActionRecordStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// ActionRecordList contains a list of ActionRecord.
type ActionRecordList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ActionRecord `json:"items"`
}

func init() {
	SchemeBuilder.Register(&ActionRecord{}, &ActionRecordList{})
}
