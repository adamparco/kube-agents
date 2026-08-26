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
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// AgentIntegrationSpec extends the common IntegrationSpec with chat-platform connections
// (Google Chat, Slack). It is carried inline on AgentSpec.Integration for every tier.
type AgentIntegrationSpec struct {
	IntegrationSpec `json:",inline"`

	// GoogleChat configures the Google Chat integration.
	// +optional
	GoogleChat *GoogleChatSpec `json:"googleChat,omitempty"`

	// Slack configures the Slack integration.
	// +optional
	Slack *SlackSpec `json:"slack,omitempty"`
}

// GoogleChatSpec contains the configuration for the Google Chat integration,
// enabling communication and event routing via Google Chat.
// +kubebuilder:validation:XValidation:rule="!has(self.enabled) || self.enabled == false || (has(self.projectId) && has(self.topicName) && has(self.subscriptionName))",message="projectId, topicName, and subscriptionName are required when Google Chat integration is enabled"
// The allowlist predicate is written with size() rather than a comparison against an empty string
// literal on purpose: gofmt rewrites a pair of adjacent ASCII apostrophes inside a line comment
// into a typographic close-quote (U+201D), which silently turns the marker into CEL the API server
// cannot compile — and it does so AFTER controller-gen has already emitted a correct CRD, so the
// corruption only surfaces on the next generation. A quote-free predicate cannot be mangled by the
// formatter (LSN-016).
// +kubebuilder:validation:XValidation:rule="!has(self.enabled) || self.enabled == false || (has(self.allowedUsers) && self.allowedUsers.exists(u, u.trim().size() > 0))",message="spec.integration.googleChat.allowedUsers must contain at least one non-blank entry when the Google Chat integration is enabled (an empty or all-blank allowlist is not an allowlist)"
type GoogleChatSpec struct {
	// Enabled toggles the Google Chat integration.
	// +kubebuilder:default=false
	// +optional
	Enabled *bool `json:"enabled,omitempty"`

	// ProjectID is the target GCP Project ID for Pub/Sub.
	// +optional
	ProjectID string `json:"projectId,omitempty"`

	// TopicName is the GCP Chat Topic Name.
	// +optional
	TopicName string `json:"topicName,omitempty"`

	// SubscriptionName is the GCP Chat Subscription Name.
	// +optional
	SubscriptionName string `json:"subscriptionName,omitempty"`

	// AllowedUsers is the closed allowlist of Google Chat principals. When the
	// integration is enabled it must contain at least one non-blank entry; an
	// empty or all-blank list is not an allowlist, and there is no permissive
	// fallback (06 §1.2 V-7).
	//
	// The bounds are load-bearing, not cosmetic: the API server refuses to install
	// a CRD whose CEL rules it cannot cost-bound, and a per-entry rule over an
	// unbounded list of unbounded strings is unbounded. MaxItems/MaxLength are what
	// make the V-7 rule above installable at all.
	// +listType=set
	// +kubebuilder:validation:MaxItems=256
	// +kubebuilder:validation:items:MaxLength=253
	// +optional
	AllowedUsers []string `json:"allowedUsers,omitempty"`

	// HomeChannel is the home channel Chat address.
	// +optional
	HomeChannel string `json:"homeChannel,omitempty"`

	// Mode controls output verbosity in Google Chat ("default" or "debug").
	// "default": Quiet mode (silences memory reviews, approval cards, and tool progress).
	// "debug": Full verbosity (surfaces tool progress, memory reviews, interim messages, and approval cards).
	// +kubebuilder:validation:Enum=default;debug
	// +kubebuilder:default:="default"
	// +optional
	Mode string `json:"mode,omitempty"`
}

// SlackSpec contains the configuration for the Slack integration.
// +kubebuilder:validation:XValidation:rule="!has(self.enabled) || self.enabled == false || (has(self.botTokenSecretRef) && has(self.appTokenSecretRef))",message="botTokenSecretRef and appTokenSecretRef are required when Slack integration is enabled"
// The size() form is deliberate — see the GoogleChatSpec marker above (LSN-016).
// +kubebuilder:validation:XValidation:rule="!has(self.enabled) || self.enabled == false || (has(self.allowedUsers) && self.allowedUsers.exists(u, u.trim().size() > 0))",message="spec.integration.slack.allowedUsers must contain at least one non-blank entry when the Slack integration is enabled (an empty or all-blank allowlist is not an allowlist)"
type SlackSpec struct {
	// Enabled toggles the Slack integration.
	// +kubebuilder:default=false
	// +optional
	Enabled *bool `json:"enabled,omitempty"`

	// BotTokenSecretRef securely references a Secret containing the SLACK_BOT_TOKEN.
	// +optional
	BotTokenSecretRef *corev1.SecretKeySelector `json:"botTokenSecretRef,omitempty"`

	// AppTokenSecretRef securely references a Secret containing the SLACK_APP_TOKEN.
	// +optional
	AppTokenSecretRef *corev1.SecretKeySelector `json:"appTokenSecretRef,omitempty"`

	// AllowedUsers is the closed allowlist of Slack member IDs. When the
	// integration is enabled it must contain at least one non-blank entry; an
	// empty or all-blank list is not an allowlist, and there is no permissive
	// fallback (06 §1.2 V-7). The bounds make the V-7 CEL rule cost-boundable and
	// therefore installable — see the GoogleChatSpec field above.
	// +listType=set
	// +kubebuilder:validation:MaxItems=256
	// +kubebuilder:validation:items:MaxLength=253
	// +optional
	AllowedUsers []string `json:"allowedUsers,omitempty"`

	// HomeChannel is the default channel ID for scheduled messages.
	// +optional
	HomeChannel string `json:"homeChannel,omitempty"`

	// HomeChannelName is the human-readable name for the home channel.
	// +optional
	HomeChannelName string `json:"homeChannelName,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status

// Agent is the Schema for the agents API. It is the single, generic, tier-discriminated agent
// resource (06 §1/§1.1, 08 §2); the former PlatformAgent Kind is now the platform-tier instance.
type Agent struct {
	metav1.TypeMeta `json:",inline"`

	// metadata is a standard object metadata
	// +optional
	metav1.ObjectMeta `json:"metadata,omitempty"`

	// spec defines the desired state of the Agent
	// +required
	Spec AgentSpec `json:"spec"`

	// status defines the observed state of the Agent
	// +optional
	Status AgentStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// AgentList contains a list of Agent
type AgentList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitzero"`
	Items           []Agent `json:"items"`
}

func init() {
	SchemeBuilder.Register(&Agent{}, &AgentList{})
}
