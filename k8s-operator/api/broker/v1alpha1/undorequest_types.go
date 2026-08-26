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
)

// UndoRequest is the fourth brake control (06 §4.4): reverse one action that already happened.
//
// It is a REQUEST, not the undo itself. Creating one asks the undo controller (`C-UC`) to replay
// the recorded undo plan from the referenced ActionRecord as a new, first-class action — classified
// by the same classifier, journaled to the same store, and verified by the same predicates. The
// alternative design, a controller that reaches into the cluster and reverts the change directly,
// would be the one write path in the system that skipped the broker, which is the property V-BRK
// exists to hold. An undo that bypassed classification could not be gated, could not be journaled,
// and could not itself be undone.
//
// The separation from the ActionRecord is what makes 05 §1.3 possible: undo works with the
// originating agent PAUSED OR DELETED. A paused agent refuses new envelopes, and an undo submitted
// through that agent would be refused by its own brake — the one moment a human most needs it to
// work. So the undo controller runs in `kubeagents-system` with its own ServiceAccount and its own
// narrow grant (06 §2: `phase` → `Undone` only, plus `undoneBy`, `contested`, `message`, on any
// record in any namespace), and asks the broker on the requester's behalf rather than the agent's.
//
// Namespaced, alongside the ActionRecord it reverses. `spec.actionRef` is therefore a same-namespace
// reference and carries no namespace field: a cross-namespace undo would let anyone who can create
// a CR in their own namespace reverse changes in someone else's.

// UndoPhase is the lifecycle of an undo request.
//
// Five phases, three terminal. `Refused` is separate from `Failed` on purpose: `Failed` means the
// undo was attempted and did not work, which invites a retry; `Refused` means it was never
// attempted, and retrying will produce the same answer. Collapsing them would make the two most
// common questions after an undo — "should I try again?" and "what stopped it?" — unanswerable from
// the phase alone.
// +kubebuilder:validation:Enum=Pending;Executing;Executed;Failed;Refused
type UndoPhase string

const (
	// UndoPending is the initial phase: accepted, not yet acted on.
	UndoPending UndoPhase = "Pending"
	// UndoExecuting means the undo action has been submitted to the broker.
	UndoExecuting UndoPhase = "Executing"
	// UndoExecuted is terminal and successful: the reverse action verified.
	UndoExecuted UndoPhase = "Executed"
	// UndoFailed is terminal: the undo was attempted and did not succeed.
	UndoFailed UndoPhase = "Failed"
	// UndoRefused is terminal: the undo was never attempted. No undo plan, a precondition that no
	// longer holds, or a requester who is not permitted to ask.
	UndoRefused UndoPhase = "Refused"
)

// IsTerminal reports whether an undo phase can still change.
func (p UndoPhase) IsTerminal() bool {
	switch p {
	case UndoExecuted, UndoFailed, UndoRefused:
		return true
	default:
		return false
	}
}

// ActionRef names an ActionRecord in the same namespace.
type ActionRef struct {
	// Name is the ActionRecord's name — the `ar-<lowercased action id>` form of 06 §4.3.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=253
	Name string `json:"name"`
}

// UndoRequestSpec is the request body. Every field is immutable after creation.
//
// Immutability is not tidiness. `status` records what was done in response to `spec`, and a spec
// that can be repointed at a different ActionRecord after the fact produces an object whose status
// describes an undo of something it no longer references — which is precisely the object a human
// reads when reconstructing an incident.
// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="an UndoRequest spec is immutable: create a new request rather than repointing one whose status already describes a different action"
type UndoRequestSpec struct {
	// ActionRef is the ActionRecord to reverse, in this request's own namespace.
	// +kubebuilder:validation:Required
	ActionRef ActionRef `json:"actionRef"`

	// Reason is why the action is being reversed. Required, and recorded on the resulting undo
	// action — it is the only part of an incident timeline that says what the human was thinking.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=512
	Reason string `json:"reason"`

	// RequestedBy is the canonical platform-qualified principal (06 §1.2 V-11) asking for the undo,
	// checked against the originating agent's `allowedUsers`.
	//
	// The `k8s:` platform is accepted here and not only `slack:`/`googlechat:` because 06 §4.4
	// requires undo to work through `kubectl` with chat down. A human running `kubectl apply` has a
	// Kubernetes username and no Slack ID, and a schema that could not express that identity would
	// make the API brake unusable in the exact failure it is specified for.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=3
	// +kubebuilder:validation:MaxLength=253
	// +kubebuilder:validation:Pattern=`^(slack|googlechat|k8s):\S+$`
	RequestedBy string `json:"requestedBy"`

	// MarkContested also marks the target contested, so the agent does not simply redo the change.
	// Default TRUE.
	//
	// Default-true because the common case is a human reversing something an agent decided to do,
	// and an agent whose reasoning produced that change once will produce it again on the next
	// reconcile. Undo without the marker is a loop: the human undoes, the agent redoes, and the
	// human concludes the brake does not work. Setting it false is for the rarer case where the
	// change was right and the timing was wrong.
	// +kubebuilder:default=true
	// +optional
	MarkContested *bool `json:"markContested,omitempty"`
}

// UndoRequestStatus is what the undo controller did about it.
type UndoRequestStatus struct {
	// Phase is the lifecycle position.
	// +optional
	Phase UndoPhase `json:"phase,omitempty"`

	// UndoActionID is the ULID of the reverse action the broker executed, once it exists. It is the
	// forward half of the 06 §4.3 undo linkage, whose reverse half is `undoneBy` on the original
	// record — two pointers rather than one because "was this ever undone?" and "what did this undo?"
	// are asked from opposite ends and neither can be answered by scanning.
	// +kubebuilder:validation:MaxLength=26
	// +kubebuilder:validation:Pattern=`^[0-9A-HJKMNP-TV-Z]{26}$`
	// +optional
	UndoActionID string `json:"undoActionId,omitempty"`

	// Message is the human-readable outcome, including the reason for a `Refused`.
	// +kubebuilder:validation:MaxLength=1024
	// +optional
	Message string `json:"message,omitempty"`

	// CompletionTime is when the request reached a terminal phase.
	// +optional
	CompletionTime *metav1.Time `json:"completionTime,omitempty"`

	// ObservedGeneration is the spec generation this status was computed from.
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// Conditions follow the standard Kubernetes convention.
	// +listType=map
	// +listMapKey=type
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Namespaced,shortName=undo,categories=kube-agents
// +kubebuilder:printcolumn:name="Action",type=string,JSONPath=`.spec.actionRef.name`,description="The action being reversed"
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="Undo",type=string,JSONPath=`.status.undoActionId`,description="The reverse action's ID"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// UndoRequest asks the undo controller to reverse one recorded action.
type UndoRequest struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	// +kubebuilder:validation:Required
	Spec UndoRequestSpec `json:"spec"`

	// +optional
	Status UndoRequestStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// UndoRequestList contains a list of UndoRequest.
type UndoRequestList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []UndoRequest `json:"items"`
}

// ContestedRequested applies the `markContested` default of true.
func (u *UndoRequest) ContestedRequested() bool {
	return u.Spec.MarkContested == nil || *u.Spec.MarkContested
}

func init() {
	SchemeBuilder.Register(&UndoRequest{}, &UndoRequestList{})
}
