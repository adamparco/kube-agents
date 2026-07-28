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
	"fmt"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// ApprovalRoster is who may say yes, and for how long (06 §4.4).
//
// It is the object every other brake control defers to. `resume` needs the roster rather than the
// pauser, because the human who hit the brake is not necessarily the one qualified to release it;
// `uncontest` needs the roster and nothing else; and a `gated` action needs a roster member's
// approval before a single byte is written. That concentration is the point — there is exactly one
// place to look to answer "who can authorise this", and exactly one place to change it.
//
// A missing or empty roster is NOT an open door. 06 §4.4's sixth fail-closed rule: a `gated` action
// with no roster to consult stays `PendingApproval` and expires. It is never auto-approved. The
// tempting alternative — "no roster configured, so skip the gate" — turns a deployment mistake into
// a silent removal of every approval requirement in the namespace.
//
// Namespaced, unlike FleetFreeze and ChangePolicy, because a roster is a statement about one team's
// changes and the people who own them. It is still a control-plane object under 03 §3.3 rule 3: no
// agent identity may write one, and the classifier's forbidden set names `create ApprovalRoster`
// explicitly, because an agent that can add itself to the roster can approve its own gated actions.

// ApproverPlatform is the chat platform an approver's immutable ID belongs to.
//
// The platform is a separate field from the ID rather than baked into one string because the pair
// canonicalizes to the 06 §1.2 V-11 principal `<platform>:<id>` and canonicalization has to be
// total. Given a single free-text field, `U02ABCDEF` is a well-formed Slack member ID, a
// well-formed nothing, and unresolvable without guessing.
// +kubebuilder:validation:Enum=slack;googlechat
type ApproverPlatform string

const (
	// ApproverPlatformSlack is a Slack workspace member ID (`U…`).
	ApproverPlatformSlack ApproverPlatform = "slack"
	// ApproverPlatformGoogleChat is a Google Chat user resource name (`users/…`).
	ApproverPlatformGoogleChat ApproverPlatform = "googlechat"
)

// Approver is one member of the roster.
type Approver struct {
	// Platform is the chat platform the ID belongs to.
	// +kubebuilder:validation:Required
	Platform ApproverPlatform `json:"platform"`

	// ID is the platform-native IMMUTABLE user ID — a Slack member ID, or a Google Chat
	// `users/<n>` resource name. Never an @handle, a display name, or an email.
	//
	// 06 §1.2 V-11 is a hard schema rule for a reason that is easy to miss: handles and emails are
	// reassignable. A roster naming `@alice` keeps approving after Alice leaves and the handle is
	// recycled, and nothing in the system notices, because the roster still matches. An immutable
	// ID that no longer resolves fails to match, which is the correct failure.
	//
	// The pattern excludes whitespace and the `:` that separates the two halves of the canonical
	// form — an ID containing a colon would make `<platform>:<id>` ambiguous to parse back.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=253
	// +kubebuilder:validation:Pattern=`^[^\s:]+(/[^\s:]+)*$`
	ID string `json:"id"`

	// DisplayName is for humans reading the roster and is never matched against.
	// +kubebuilder:validation:MaxLength=253
	// +optional
	DisplayName string `json:"displayName,omitempty"`
}

// Principal renders the approver in the canonical 06 §1.2 V-11 form `<platform>:<id>`, which is the
// only form anything compares against.
func (a Approver) Principal() string {
	return fmt.Sprintf("%s:%s", a.Platform, a.ID)
}

// SlackNotify is where Slack approval requests land.
type SlackNotify struct {
	// Channel is the Slack channel ID (`C…`), not a `#name`.
	// +kubebuilder:validation:MaxLength=253
	// +optional
	Channel string `json:"channel,omitempty"`
}

// GoogleChatNotify is where Google Chat approval requests land.
type GoogleChatNotify struct {
	// Space is the Google Chat space resource name (`spaces/…`).
	// +kubebuilder:validation:MaxLength=253
	// +optional
	Space string `json:"space,omitempty"`
}

// ApprovalNotify is where approval requests are delivered (06 §2b.1).
//
// Notification is not authorisation. A roster member who approves from a thread they were not
// notified in is still a roster member, and an approval that arrives through a channel this block
// does not name is still valid. The block decides where the ask is delivered — a broken `notify`
// makes approvals slow, never permissive.
type ApprovalNotify struct {
	// Slack is the Slack destination.
	// +optional
	Slack *SlackNotify `json:"slack,omitempty"`

	// GoogleChat is the Google Chat destination.
	// +optional
	GoogleChat *GoogleChatNotify `json:"googleChat,omitempty"`
}

// RosterRef names an ApprovalRoster in a namespace.
type RosterRef struct {
	// Name is the roster's name.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=253
	Name string `json:"name"`

	// Namespace is where the roster lives. Empty means the referring object's own namespace.
	// +kubebuilder:validation:MaxLength=253
	// +optional
	Namespace string `json:"namespace,omitempty"`
}

// The 06 §4.4 TTL bounds. Named constants rather than literals in a CEL marker because the webhook,
// the CRD schema and the tests all have to agree on the same three numbers, and a marker string
// cannot reference a constant — so the test asserting the marker matches these is the seam.
const (
	// DefaultApprovalTTL is 06 §4.4's single canonical approval-TTL default, referenced by 04 §3.1
	// rather than restated there. 24 h and not a few hours because a gated action is by definition
	// irreversible or high-blast-radius, and the roster is a small group spanning time zones and
	// sleep: a 4-hour TTL expires most overnight requests, which teaches agents and operators to
	// re-submit rather than wait — the exact behaviour a gate exists to prevent.
	DefaultApprovalTTL = 24 * time.Hour

	// MaxApprovalTTL is the 72 h ceiling. Past it the cluster state the classification was computed
	// against is no longer the state the approver is looking at, so the broker re-classifies at
	// approval time and refuses if the class rose or a target's `resourceVersion` moved such that
	// the undo plan's `preconditions.uid` no longer matches.
	MaxApprovalTTL = 72 * time.Hour

	// MinApprovalTTL is the 1 h floor. Below it a gate stops being a gate and becomes a delay: an
	// approver who is asleep, in a meeting, or on a plane cannot answer, and the action expires
	// unreviewed. Expiry is never an approval, so a too-short TTL does not create a security hole
	// — it creates a gate that reliably fails, which trains people to route around it.
	MinApprovalTTL = 1 * time.Hour
)

// ApprovalRosterSpec is the roster body.
type ApprovalRosterSpec struct {
	// Approvers is the closed list of principals who may approve. Required and non-empty.
	//
	// A roster resource that exists with zero approvers is refused at admission rather than stored,
	// because the two things it could mean are opposites: "nobody may approve" (correct behaviour
	// under fail-closed rule 6, and better expressed by having no roster) and "I have not filled
	// this in yet" (the actual cause, every time).
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:MaxItems=128
	Approvers []Approver `json:"approvers"`

	// MinApprovals is how many distinct roster members must approve. Default 1.
	//
	// Bounded above by the roster size at admission: `minApprovals: 3` against a two-person roster
	// is not a strict policy, it is an action that can never be approved and will always expire.
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=128
	// +optional
	MinApprovals *int32 `json:"minApprovals,omitempty"`

	// AllowSelfApproval lets the human who requested an action also approve it. Default FALSE.
	//
	// Default-false is four-eyes. The requester is the one person guaranteed to already believe the
	// action is a good idea, so their approval carries no information — it converts the gate into a
	// confirmation dialog, which is the control-shaped object people click through.
	// +kubebuilder:default=false
	// +optional
	AllowSelfApproval *bool `json:"allowSelfApproval,omitempty"`

	// TTL is how long a gated action waits for approval before becoming `Expired`. Default 24h,
	// floor 1h, ceiling 72h (the DefaultApprovalTTL / MinApprovalTTL / MaxApprovalTTL constants).
	//
	// Expiry is never an approval: an `Expired` action is terminal (06 §4.3) and is not executed
	// afterwards, no matter who says yes later.
	// +kubebuilder:default="24h"
	// +optional
	TTL *metav1.Duration `json:"ttl,omitempty"`

	// Notify is where approval requests are delivered (06 §2b.1).
	// +optional
	Notify *ApprovalNotify `json:"notify,omitempty"`

	// EscalateTo is an optional roster the request escalates to on TTL expiry.
	//
	// Escalation does NOT extend the original action's life. The action still expires; what
	// escalation buys is that somebody senior is told it did. Letting a chain of rosters extend the
	// clock would make the effective TTL the sum of every roster in the chain, and the 72 h ceiling
	// exists precisely because a stale classification stops describing the cluster.
	// +optional
	EscalateTo *RosterRef `json:"escalateTo,omitempty"`
}

// ApprovalRosterStatus reports what the roster is bound to.
type ApprovalRosterStatus struct {
	// AgentsReferencing is how many Agent CRs name this roster. Written by the controller so an
	// operator can see a roster that nothing consults — the failure that looks identical to a
	// working one right up until the first gated action expires unreviewed.
	// +optional
	AgentsReferencing int32 `json:"agentsReferencing,omitempty"`

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
// +kubebuilder:resource:scope=Namespaced,shortName=roster,categories=kube-agents
// +kubebuilder:printcolumn:name="Approvers",type=integer,JSONPath=`.status.agentsReferencing`,description="Agents consulting this roster"
// +kubebuilder:printcolumn:name="TTL",type=string,JSONPath=`.spec.ttl`,description="How long a gated action waits"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// ApprovalRoster is the closed list of principals who may approve a gated action, resume a paused
// agent, or clear a contested marker.
type ApprovalRoster struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	// +kubebuilder:validation:Required
	Spec ApprovalRosterSpec `json:"spec"`

	// +optional
	Status ApprovalRosterStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// ApprovalRosterList contains a list of ApprovalRoster.
type ApprovalRosterList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ApprovalRoster `json:"items"`
}

// EffectiveTTL applies 06 §4.4's default and bounds.
//
// It CLAMPS rather than rejects, and that is the opposite of what 06 §1.2 V-8 does to an
// out-of-range initiative budget. The difference is which way the mistake cuts. An oversized budget
// leaf is a request for more authority, so silently reducing it would leave an operator believing
// they had it; a TTL is not authority in either direction, and this function's callers are on the
// read path deciding whether an action has expired — a broker that refused to evaluate an approval
// because the roster's TTL was malformed would fail OPEN on the thing that matters, by leaving the
// action parked forever with no expiry. Admission is where an out-of-range TTL is refused, and this
// is what the runtime does with one that got in anyway.
func (r *ApprovalRoster) EffectiveTTL() time.Duration {
	if r == nil || r.Spec.TTL == nil || r.Spec.TTL.Duration == 0 {
		return DefaultApprovalTTL
	}
	switch d := r.Spec.TTL.Duration; {
	case d < MinApprovalTTL:
		return MinApprovalTTL
	case d > MaxApprovalTTL:
		return MaxApprovalTTL
	default:
		return d
	}
}

// EffectiveMinApprovals applies the default of 1.
func (r *ApprovalRoster) EffectiveMinApprovals() int32 {
	if r == nil || r.Spec.MinApprovals == nil || *r.Spec.MinApprovals < 1 {
		return 1
	}
	return *r.Spec.MinApprovals
}

// SelfApprovalAllowed applies the default of false.
func (r *ApprovalRoster) SelfApprovalAllowed() bool {
	return r != nil && r.Spec.AllowSelfApproval != nil && *r.Spec.AllowSelfApproval
}

// HasApprover reports whether a canonical `<platform>:<id>` principal is on the roster.
//
// A nil roster answers false, which is fail-closed rule 6: an action whose roster could not be
// loaded has nobody who can approve it, so it waits and expires. It is never auto-approved, and the
// nil case must not be mistaken for "no restriction".
func (r *ApprovalRoster) HasApprover(principal string) bool {
	if r == nil {
		return false
	}
	for _, a := range r.Spec.Approvers {
		if a.Principal() == principal {
			return true
		}
	}
	return false
}

func init() {
	SchemeBuilder.Register(&ApprovalRoster{}, &ApprovalRosterList{})
}
