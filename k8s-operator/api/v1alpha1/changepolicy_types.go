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

// ChangePolicy is the customer's half of the risk classifier (06 §4.2): additional rows in the same
// rule table the code floor already is, contributed by a human and evaluated by the same matcher.
//
// It is STRICTER-ONLY, and that property is guaranteed twice over, in two places that fail
// differently:
//
//  1. At admission. A rule declaring a class lower than the code floor would assign for the same
//     match is rejected, with the floor rule named. This is the half a policy author experiences.
//  2. In the broker. Step 3 of 06 §4.2 takes the MAXIMUM over every source and step 5's cap takes
//     the MINIMUM, and there is no operation in either loop that can move a result the other way.
//     This is the half that holds when the webhook is down, misconfigured, or bypassed.
//
// The second is the real guarantee: "even if it were somehow admitted it would have no effect"
// (06 §6). The first exists because a policy that silently does nothing is worse than one that is
// refused — an operator who wrote `class: routine` next to a gated action and saw it accepted will
// believe the gate is off, and will find out otherwise at the least convenient moment.
//
// Cluster-scoped, because a policy is a statement about a fleet of agents whose scopes are not all
// in one namespace, and because a namespaced policy would be editable by anyone with write on that
// namespace. ChangePolicy is a control-plane object (03 §3.3 rule 3): no agent identity may write
// one in either direction, which is enforced by RBAC, by `vap-agent-scope`, and by the broker's own
// forbidden set — three independent mechanisms, because this is the object that decides whether the
// others are consulted.

// ChangePolicyClass is the class a ChangePolicy rule may contribute.
//
// Deliberately NOT the same enum as ActionRiskClass, which has four values. Two of them cannot
// appear here:
//
//   - `routine` is missing because it is the identity element. The broker maxes over sources, so a
//     rule contributing routine changes nothing, ever; the only reason to write one is the belief
//     that it lowers something, which is the belief this type exists to correct. There is no
//     downgrade path, no `allow`, no `exempt` and no `maxClass` (06 §6).
//   - `forbidden` is missing because the forbidden set is a code constant and is not addressable by
//     ChangePolicy at all (06 §6). `forbidden` means "no path through an agent, not even with a
//     human saying yes" (03 §3.3), and a class with no approval path should not be reachable by
//     editing a CR: it would hand anyone who can write a ChangePolicy a fleet-wide denial with no
//     route around it. A customer who wants an action never to happen writes `gated` and does not
//     approve it — same outcome, with a human in the loop and a record of the refusal.
//
// `+1` is the escalation form of 06 §4.2 step 4. It raises by one class and is capped at gated, so
// it is a tightening under every input.
// +kubebuilder:validation:Enum=elevated;gated;+1
type ChangePolicyClass string

const (
	// ChangePolicyClassElevated executes immediately and notifies at once.
	ChangePolicyClassElevated ChangePolicyClass = "elevated"
	// ChangePolicyClassGated parks for a human approval.
	ChangePolicyClassGated ChangePolicyClass = "gated"
	// ChangePolicyClassEscalate is the `+1` of 06 §4.2 step 4, capped at gated.
	ChangePolicyClassEscalate ChangePolicyClass = "+1"
)

// ChangeDirection is the security direction a rule matches on (03 §5.2).
// +kubebuilder:validation:Enum=loosen;tighten;any
type ChangeDirection string

const (
	// ChangeDirectionLoosen matches changes that remove or widen a control. This is the direction
	// that gates.
	ChangeDirectionLoosen ChangeDirection = "loosen"
	// ChangeDirectionTighten matches changes that add or narrow one.
	ChangeDirectionTighten ChangeDirection = "tighten"
	// ChangeDirectionAny matches regardless, and is the default.
	ChangeDirectionAny ChangeDirection = "any"
)

// ChangeVerb is an Action Envelope `op` value. The enum is closed and mirrors the envelope's own:
// a rule naming a verb the envelope cannot carry matches nothing, and matching nothing is how a
// gate stops gating without anyone noticing.
// +kubebuilder:validation:Enum=create;apply;patch;delete;scale;cloud
type ChangeVerb string

// KindRefSpec is a group+kind pair. `group: ""` is core.
//
// A struct rather than a `group/kind` string because the string form has three spellings for a core
// kind — `Secret`, `/Secret`, `v1/Secret` — and the third one carries a version this field does not
// have. Two of the three would have to be accepted and one silently ignored.
type KindRefSpec struct {
	// Group is the API group, empty for core.
	// +kubebuilder:validation:MaxLength=253
	// +optional
	Group string `json:"group,omitempty"`

	// Kind is the object kind, matched case-sensitively because Kubernetes kinds are.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=253
	Kind string `json:"kind"`
}

// ChangeRuleWhen is the match predicate. Every field is ANDed and an empty field matches
// everything, so `when: {}` matches every operation — which is correct and is what 06 §4.2's own
// `tighten-fanout` example relies on to cap the whole fleet's blast radius in one rule.
type ChangeRuleWhen struct {
	// Verbs are the envelope ops this rule matches. Empty matches any.
	// +listType=set
	// +kubebuilder:validation:MaxItems=6
	// +optional
	Verbs []ChangeVerb `json:"verbs,omitempty"`

	// Kinds are the target kinds. Empty matches any.
	// +kubebuilder:validation:MaxItems=128
	// +optional
	Kinds []KindRefSpec `json:"kinds,omitempty"`

	// ExcludeKinds are never matched, applied AFTER Kinds.
	// +kubebuilder:validation:MaxItems=128
	// +optional
	ExcludeKinds []KindRefSpec `json:"excludeKinds,omitempty"`

	// OwnedByLowerTier exists here only so that setting it can be REFUSED with an explanation.
	//
	// It is a code-floor field: ownership is computed from the Agent hierarchy by the V-6 predicate,
	// never declared. Leaving the field out of the schema instead would let the API server prune it
	// silently, and a policy author who wrote it would get a rule that matches strictly more than
	// what they wrote, with no error anywhere. A field that is present and rejected is a
	// conversation; a field that is absent and pruned is a surprise.
	//
	// +kubebuilder:validation:XValidation:rule="self == false",message="when.ownedByLowerTier is code-floor only: ownership is computed from the Agent hierarchy, not declared by a policy"
	// +optional
	OwnedByLowerTier bool `json:"ownedByLowerTier,omitempty"`

	// Namespaces matches the TARGET's namespace by name. Empty matches any.
	// +listType=set
	// +kubebuilder:validation:MaxItems=256
	// +kubebuilder:validation:items:MaxLength=253
	// +optional
	Namespaces []string `json:"namespaces,omitempty"`

	// NamespaceSelector matches the target namespace's labels.
	// +optional
	NamespaceSelector *metav1.LabelSelector `json:"namespaceSelector,omitempty"`

	// LabelSelector is matched against the LIVE target object, not the desired state.
	//
	// The distinction is the whole of V-GAT-022. A payload asserting
	// `kube-agents/environment: production` is a claim by the thing being classified; matching on it
	// would let an agent choose its own risk class by writing a label. Live state is the only thing
	// the classifier believes, and a `create` therefore never matches a live-object selector — there
	// is no live object yet to carry the label.
	// +optional
	LabelSelector *metav1.LabelSelector `json:"labelSelector,omitempty"`

	// FieldPaths fire when the change touches any of them. DOTTED relaxed JSONPath — the
	// kubectl/client-go dialect — e.g. `spec.template.spec.containers[*].image`.
	//
	// NOT a JSON Pointer. `/spec/replicas` is rejected at admission with
	// `expected a dotted field path, not a JSON Pointer` (06 §4.2), because it is otherwise a
	// perfectly well-formed dotted path with a single segment literally named `/spec/replicas`, and
	// a rule that matches nothing is indistinguishable from a rule that found nothing.
	// +listType=set
	// +kubebuilder:validation:MaxItems=64
	// +kubebuilder:validation:items:MaxLength=512
	// +optional
	FieldPaths []string `json:"fieldPaths,omitempty"`

	// Direction is the security direction; `loosen` is what gates. Empty means `any`.
	// +optional
	Direction ChangeDirection `json:"direction,omitempty"`
}

// ChangeRule is one row of the rule table, in exactly the shape the code floor uses.
//
// The sameness is the design. A customer's policy is not a second, weaker language the broker
// interprets differently; it is more rows in the table the floor already is, matched by the same
// matcher and combined by the same Max. There is no field here that can lower a class.
// +kubebuilder:validation:XValidation:rule="has(self.class) || has(self.maxObjects)",message="a rule that contributes neither a class nor a maxObjects matches operations and then does nothing, which reads in a policy review as a control that is present"
type ChangeRule struct {
	// ID appears in `classification.reasons[].rule` and in the audit event, so it is what a human
	// sees when this rule is why they were asked. Unique within the policy, and distinct from every
	// code-floor rule ID.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=128
	// +kubebuilder:validation:Pattern=`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`
	ID string `json:"id"`

	// When is the match predicate. Omitted means "every operation".
	// +optional
	When ChangeRuleWhen `json:"when,omitempty"`

	// Class is this rule's contribution. Omitted means the rule contributes only a cap.
	// +optional
	Class ChangePolicyClass `json:"class,omitempty"`

	// MaxObjects is a blast-radius cap. It may only LOWER the effective cap: the broker takes the
	// minimum across sources, so a number above the code ceiling is accepted, stored, listed — and
	// never wins. Admission warns rather than refuses, because refusing would make the guarantee
	// look like it lives in the webhook, and it does not.
	// +kubebuilder:validation:Minimum=1
	// +optional
	MaxObjects int32 `json:"maxObjects,omitempty"`

	// Reason is shown VERBATIM to the human deciding whether to approve, at the moment they are
	// deciding, probably on a phone. Required, because a gate whose reason is blank teaches the
	// approver that the reasons are not worth reading.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=512
	Reason string `json:"reason"`
}

// ChangePolicyAgentSelector chooses which agents a policy binds. Omitted ⇒ every agent.
//
// There is no opt-out. A policy set by a parent's operator binds that parent's children too, and an
// agent cannot select itself out — the selector is a statement by the human who wrote the policy
// about who it covers, not a negotiation with the covered.
type ChangePolicyAgentSelector struct {
	// Tiers restricts the policy to these tiers. Empty matches every tier.
	// +listType=set
	// +kubebuilder:validation:MaxItems=3
	// +optional
	Tiers []AgentTier `json:"tiers,omitempty"`

	// Scopes restricts the policy to agents at or beneath these scopes. Empty matches every scope.
	//
	// "At or beneath": a policy naming project `p` binds the platform agent for `p` and every
	// cluster-admin and developer-team agent under it. Anything else would make a fleet-wide policy
	// require one entry per namespace, and the entry nobody remembered to add is the gap.
	// +kubebuilder:validation:MaxItems=64
	// +optional
	Scopes []ScopeSpec `json:"scopes,omitempty"`
}

// ChangePolicySpec is the policy body.
type ChangePolicySpec struct {
	// AgentSelector chooses which agents this policy applies to. Omitted ⇒ every agent.
	// +optional
	AgentSelector *ChangePolicyAgentSelector `json:"agentSelector,omitempty"`

	// Rules are additional rows in the classifier's rule table.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:MaxItems=256
	Rules []ChangeRule `json:"rules"`
}

// ChangePolicyStatus reports what the policy actually binds.
type ChangePolicyStatus struct {
	// AgentsMatched is how many Agent CRs this policy currently binds. Written by the controller so
	// a human can tell a policy that covers the fleet from one whose selector matches nothing —
	// which is by far the likelier mistake, and is invisible otherwise.
	// +optional
	AgentsMatched int32 `json:"agentsMatched,omitempty"`

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
// +kubebuilder:resource:scope=Cluster,shortName=cp,categories=kube-agents
// +kubebuilder:printcolumn:name="Rules",type=integer,JSONPath=`.status.agentsMatched`,description="Agents bound"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// ChangePolicy is a cluster-scoped, stricter-only addition to the risk classifier's rule table.
type ChangePolicy struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	// +kubebuilder:validation:Required
	Spec ChangePolicySpec `json:"spec"`

	// +optional
	Status ChangePolicyStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// ChangePolicyList contains a list of ChangePolicy.
type ChangePolicyList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ChangePolicy `json:"items"`
}

func init() {
	SchemeBuilder.Register(&ChangePolicy{}, &ChangePolicyList{})
}
