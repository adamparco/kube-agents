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

// FleetFreeze is the third of the five brake controls (06 §4.4): stop a whole scope at once,
// without touching any of the agents in it.
//
// It is cluster-scoped and it is consulted on EVERY envelope, which makes the read path the
// interesting part rather than the schema. 06 §4.4's first fail-closed rule is that a broker which
// cannot read the freeze list — API error, or a cache stale beyond 30 s — treats the scope as
// frozen. A control that is only enforced when the API server is answering is a control that turns
// itself off in exactly the incident it was declared for.
//
// The object is deliberately inert. It carries no controller, no finalizer and no reconcile loop:
// creating it is the whole action. That is what lets 06 §4.4's "with inference down" requirement
// hold — `kubectl apply -f freeze.yaml` needs the API server and nothing else, so the brake does
// not depend on the operator being up, the router being reachable, or a model answering.
//
// Cluster-scoped for the same reason ChangePolicy is: a freeze names a fleet, and a namespaced
// freeze would be editable by anyone with write on the namespace whose agents it is freezing.
// FleetFreeze is a control-plane object (03 §3.3 rule 3, 06 §4.4) — no agent identity may create,
// edit or delete one, enforced independently by RBAC, by `vap-agent-scope`, and by the classifier's
// forbidden set.

// FreezeScope selects the agents a freeze covers. Omit narrower fields to widen (06 §4.4).
//
// The widening direction is the one that has to be safe, and it is: `{}` means THE ENTIRE FLEET.
// Every field left blank matches everything, so the failure mode of an incompletely-filled scope is
// a freeze that is too broad. The opposite convention — blank means "match nothing" — would make a
// typo in `clusterName` produce an object that exists, reports healthy, and freezes nobody.
type FreezeScope struct {
	// ProjectID restricts the freeze to one GCP project. Empty matches every project.
	// +kubebuilder:validation:MaxLength=253
	// +optional
	ProjectID string `json:"projectId,omitempty"`

	// ClusterName restricts it further to one cluster. Empty matches every cluster in scope.
	// +kubebuilder:validation:MaxLength=253
	// +optional
	ClusterName string `json:"clusterName,omitempty"`

	// Namespace restricts it further to one namespace. Empty matches every namespace in scope.
	// +kubebuilder:validation:MaxLength=253
	// +optional
	Namespace string `json:"namespace,omitempty"`
}

// FreezeClass is a risk class a freeze may continue to allow.
//
// The enum has exactly one member, and that is the specification, not an oversight. 06 §4.4:
// `allowClasses` "may list ONLY `routine`; never `gated`". A freeze that still lets gated actions
// through is not a freeze — the whole population of changes a human would want stopped during an
// incident is the elevated-and-above one, and `gated` in particular already has a human attached,
// which is precisely the human whose attention the incident is consuming.
//
// `elevated` is excluded too. It is defined as "executes immediately and notifies", and immediate
// execution is the thing being frozen.
// +kubebuilder:validation:Enum=routine
type FreezeClass string

// FreezeClassRoutine is the only class a freeze may leave running.
const FreezeClassRoutine FreezeClass = "routine"

// FleetFreezeSpec is the freeze body. There is no `enabled` field: the object's existence IS the
// freeze, so lifting one is a delete and cannot be confused with a freeze that is present but off.
type FleetFreezeSpec struct {
	// Scope selects the covered agents. Omitted or empty ⇒ the entire fleet.
	// +optional
	Scope FreezeScope `json:"scope,omitempty"`

	// Reason is why the fleet is frozen. Required and shown to every agent's operator when an
	// action is refused, because "refused: frozen" without a reason produces a support ticket and
	// a human who tries again.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=512
	Reason string `json:"reason"`

	// RequestedBy is the canonical platform-qualified principal (06 §1.2 V-11) who froze the fleet.
	//
	// Pattern-enforced rather than free text: a freeze is the most consequential thing a human can
	// do to the fleet, and "who did this" is the first question at the other end of it. A display
	// name is not an answer — it is not unique, it is mutable, and it cannot be looked up.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=3
	// +kubebuilder:validation:MaxLength=253
	// +kubebuilder:validation:Pattern=`^(slack|googlechat|k8s):\S+$`
	RequestedBy string `json:"requestedBy"`

	// ExpiresAt optionally self-clears the freeze. Absent means it NEVER self-clears.
	//
	// No-expiry is the default because the alternative default is a freeze that quietly ends while
	// the incident is still running. The cost of the chosen default is a stale freeze somebody has
	// to remember to delete, which is a visible, diagnosable failure: agents refuse and say why.
	// +optional
	ExpiresAt *metav1.Time `json:"expiresAt,omitempty"`

	// AllowUndo keeps undo and rollback working during the freeze. Default true.
	//
	// The default is the interesting half. A freeze is declared during an incident, and undo is how
	// a human reverses the change that caused it — a freeze that also blocked undo would trap the
	// fleet in exactly the state the human is trying to leave. 06 §4.4's first fail-closed rule
	// makes the same choice under failure: an unreadable freeze list refuses everything EXCEPT undo.
	// +kubebuilder:default=true
	// +optional
	AllowUndo *bool `json:"allowUndo,omitempty"`

	// AllowClasses are the risk classes that still execute. Default empty = nothing executes.
	//
	// Empty-means-nothing is the fail-closed direction, and it is the reverse of the convention
	// `Scope` uses one field up. That is deliberate: in a selector, blank must widen the freeze; in
	// a permission list, blank must narrow it. Both readings make the incompletely-filled object
	// stricter, which is the only property the two have to share.
	// +listType=set
	// +kubebuilder:validation:MaxItems=1
	// +optional
	AllowClasses []FreezeClass `json:"allowClasses,omitempty"`
}

// FleetFreezeStatus reports what the freeze is actually holding.
type FleetFreezeStatus struct {
	// AgentsFrozen is how many Agent CRs this freeze currently covers. Written by the controller so
	// a human can tell a fleet-wide freeze from one whose scope names a cluster that does not exist
	// — which is the likelier mistake and is otherwise invisible, because both objects look healthy.
	// +optional
	AgentsFrozen int32 `json:"agentsFrozen,omitempty"`

	// ActiveSince is when the freeze took effect.
	// +optional
	ActiveSince *metav1.Time `json:"activeSince,omitempty"`

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
// +kubebuilder:resource:scope=Cluster,shortName=ff,categories=kube-agents
// +kubebuilder:printcolumn:name="Reason",type=string,JSONPath=`.spec.reason`,description="Why the fleet is frozen"
// +kubebuilder:printcolumn:name="Frozen",type=integer,JSONPath=`.status.agentsFrozen`,description="Agents covered"
// +kubebuilder:printcolumn:name="Expires",type=string,JSONPath=`.spec.expiresAt`,description="When it self-clears, if ever"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// FleetFreeze stops every covered agent from executing, without modifying any of them.
type FleetFreeze struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	// +kubebuilder:validation:Required
	Spec FleetFreezeSpec `json:"spec"`

	// +optional
	Status FleetFreezeStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// FleetFreezeList contains a list of FleetFreeze.
type FleetFreezeList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []FleetFreeze `json:"items"`
}

// Covers reports whether this freeze applies to an agent at the given scope.
//
// It is defined on the type rather than in the broker because both the broker and the controller
// that writes `status.agentsFrozen` have to answer the same question, and two implementations of
// "is this agent frozen" is one implementation and one bug. The controller's count is what a human
// reads to confirm the freeze landed; if it disagreed with the broker's refusal, the count would be
// worse than absent.
//
// A blank field on the freeze matches any value, per 06 §4.4's widening rule.
func (f *FleetFreeze) Covers(scope *ScopeSpec) bool {
	if scope == nil {
		// An agent with no scope cannot be shown to be outside the freeze. 06 §4.4 is fail-closed,
		// and "we could not tell" resolves to frozen in every other rule in that section.
		return true
	}
	s := f.Spec.Scope
	if s.ProjectID != "" && s.ProjectID != scope.ProjectID {
		return false
	}
	if s.ClusterName != "" && s.ClusterName != scope.ClusterName {
		return false
	}
	if s.Namespace != "" && s.Namespace != scope.Namespace {
		return false
	}
	return true
}

// UndoAllowed reports whether undo still runs under this freeze, applying the `allowUndo` default.
//
// A helper rather than a bare nil-check at each call site because the default is `true` and a
// forgotten nil-check reads as `false` — the one direction 06 §4.4 says must not happen, since it
// would strand the fleet in the state the human is trying to undo.
func (f *FleetFreeze) UndoAllowed() bool {
	return f.Spec.AllowUndo == nil || *f.Spec.AllowUndo
}

// Allows reports whether an action of the given risk class still executes under this freeze.
//
// Everything not named in `allowClasses` is refused, and an empty list therefore refuses
// everything. `undo` is not a risk class and does not come through here — it is decided by
// UndoAllowed, because 06 §4.4 exempts it by origin rather than by class.
func (f *FleetFreeze) Allows(class ActionRiskClass) bool {
	for _, c := range f.Spec.AllowClasses {
		if string(c) == string(class) {
			return true
		}
	}
	return false
}

// Expired reports whether the freeze has self-cleared as of `now`. A freeze with no `expiresAt`
// never expires.
func (f *FleetFreeze) Expired(now metav1.Time) bool {
	return f.Spec.ExpiresAt != nil && now.After(f.Spec.ExpiresAt.Time)
}

func init() {
	SchemeBuilder.Register(&FleetFreeze{}, &FleetFreezeList{})
}
