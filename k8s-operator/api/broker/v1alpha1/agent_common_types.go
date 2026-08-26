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

type HermesSpec struct {
	// DashboardEnabled toggles the AGENT_DASHBOARD environment variable.
	// +kubebuilder:default=true
	// +optional
	DashboardEnabled *bool `json:"dashboardEnabled,omitempty"`

	// PluginsDebug toggles the AGENT_PLUGINS_DEBUG environment variable.
	// +kubebuilder:default=false
	// +optional
	PluginsDebug *bool `json:"pluginsDebug,omitempty"`

	// AgentHome is the path to the AGENT_HOME directory.
	// +kubebuilder:default="/opt/data"
	// +optional
	AgentHome string `json:"agentHome,omitempty"`

	// ApiServerSecretRef securely references a Secret containing the API_SERVER_KEY.
	// +optional
	ApiServerSecretRef *corev1.SecretKeySelector `json:"apiServerSecretRef,omitempty"`
}

// HarnessSpec configures the core execution environment and framework-level settings for the agent.
// This extracts environmental context that doesn't belong in infrastructure blocks.
type HarnessSpec struct {
	// ClusterName is the logical name of the cluster (either where the agent is running or the target cluster).
	// +required
	ClusterName string `json:"clusterName,omitempty"`

	// Location is the geographical location or cloud region.
	// +required
	Location string `json:"location,omitempty"`

	// ProjectID is the GCP Project ID of the cluster.
	// +optional
	ProjectID string `json:"projectId,omitempty"`

	// Hermes configures the internal event-routing or agent framework.
	// +optional
	Hermes *HermesSpec `json:"hermes,omitempty"`

	// Memory configures agent memory settings.
	// +optional
	Memory *MemorySpec `json:"memory,omitempty"`
}

// MemorySpec configures memory and user profile settings for the agent framework.
type MemorySpec struct {
	// MemoryEnabled toggles framework memory persistence.
	// +kubebuilder:default=false
	// +optional
	MemoryEnabled *bool `json:"memoryEnabled,omitempty"`

	// Provider specifies the memory provider implementation (e.g. "multiuser_memory").
	// +kubebuilder:default="multiuser_memory"
	// +optional
	Provider string `json:"provider,omitempty"`

	// UserProfileEnabled toggles per-user memory profiling.
	// +kubebuilder:default=false
	// +optional
	UserProfileEnabled *bool `json:"userProfileEnabled,omitempty"`
}

// DeploymentSpec abstracts the Kubernetes Pod/Deployment configuration,
// completely decoupling the compute payload from the agent's application logic.
type DeploymentSpec struct {
	// Image specifies the container image repository.
	// +optional
	Image string `json:"image,omitempty"`

	// Tag specifies the container image tag.
	// +kubebuilder:default="latest"
	// +optional
	Tag *string `json:"tag,omitempty"`

	// ImagePullPolicy specifies if the image should be pulled.
	// +kubebuilder:default=IfNotPresent
	// +kubebuilder:validation:Enum=Always;Never;IfNotPresent
	// +optional
	ImagePullPolicy *corev1.PullPolicy `json:"imagePullPolicy,omitempty"`

	// BrowserArgs specifies custom command-line arguments to pass to the agent's browser (e.g. --no-sandbox).
	// +optional
	BrowserArgs []string `json:"browserArgs,omitempty"`

	// RuntimeClassName specifies the Pod runtime class (e.g. "gvisor").
	// +optional
	RuntimeClassName *string `json:"runtimeClassName,omitempty"`

	// Env is a list of environment variables to set in the container
	// +listType=map
	// +listMapKey=name
	// +optional
	Env []corev1.EnvVar `json:"env,omitempty"`

	// InitContainers specifies standard Kubernetes initContainers to run before the agent starts.
	// +listType=map
	// +listMapKey=name
	// +optional
	InitContainers []corev1.Container `json:"initContainers,omitempty"`

	// Sidecars specifies standard Kubernetes sidecar/application containers to run alongside the agent.
	// +listType=map
	// +listMapKey=name
	// +optional
	Sidecars []corev1.Container `json:"sidecars,omitempty"`

	// SidecarVolumes specifies custom volumes to mount for the sidecar containers.
	// +listType=map
	// +listMapKey=name
	// +optional
	SidecarVolumes []corev1.Volume `json:"sidecarVolumes,omitempty"`

	// ExtraVolumes specifies custom volumes to mount for the main container.
	// +listType=map
	// +listMapKey=name
	// +optional
	ExtraVolumes []corev1.Volume `json:"extraVolumes,omitempty"`

	// ExtraVolumeMounts specifies custom volume mounts for the main container.
	// +listType=map
	// +listMapKey=name
	// +optional
	ExtraVolumeMounts []corev1.VolumeMount `json:"extraVolumeMounts,omitempty"`

	// PodAnnotations specifies custom annotations to apply to the generated Pod template.
	// +optional
	PodAnnotations map[string]string `json:"podAnnotations,omitempty"`

	// ScaleToZero scales the deployment replicas to 0 when true (useful for saving costs during idle periods).
	// +optional
	ScaleToZero *bool `json:"scaleToZero,omitempty"`
}

// SecuritySpec manages Kubernetes RBAC, Pod Security, and Cloud Workload Identity,
// decoupling the operator from being strictly tied to GCP.
type SecuritySpec struct {
	// ServiceAccountName is the Kubernetes Service Account bound to the Deployment.
	// +optional
	ServiceAccountName string `json:"serviceAccountName,omitempty"`

	// ServiceAccountAnnotations specifies custom annotations to apply to the generated ServiceAccount.
	// +optional
	ServiceAccountAnnotations map[string]string `json:"serviceAccountAnnotations,omitempty"`
}

// IntegrationSpec isolates common platform-specific external connections.
type IntegrationSpec struct {
	// GitHub configures the GitHub integration.
	// +optional
	GitHub *GitHubSpec `json:"github,omitempty"`
}

// GitHubSpec contains the configuration for the GitHub integration.
type GitHubSpec struct {
	// GitRepo is the target GitOps repository URL for the agent environment.
	// +optional
	GitRepo string `json:"gitRepo,omitempty"`
}

// AgentTier is the persona / containment level of an agent. It is the tier discriminator the whole
// tier model, the (tier, scope) cardinality webhook, and the pre-created RBAC render overlay key on.
// Immutable after creation (06 §1.1).
// +kubebuilder:validation:Enum=platform;cluster-admin;developer-team
type AgentTier string

const (
	// TierPlatform is the root fleet-scoped tier (1 per project). The only tier in Phase 1.
	TierPlatform AgentTier = "platform"
	// TierClusterAdmin is the cluster-scoped tier (1 per cluster), parented by the platform agent.
	TierClusterAdmin AgentTier = "cluster-admin"
	// TierDeveloperTeam is the namespace-scoped tier (1 per namespace), parented by a cluster-admin.
	TierDeveloperTeam AgentTier = "developer-team"
)

// ScopeSpec identifies where an agent operates. Per-tier required fields (06 §1.2):
// platform → projectId; cluster-admin → projectId+clusterName; developer-team →
// projectId+clusterName+namespace. Together with tier it forms the identity key the cardinality
// webhook and the RBAC render overlay derive from.
type ScopeSpec struct {
	// ProjectID is the GCP project the agent is scoped to (all tiers).
	// +optional
	ProjectID string `json:"projectId,omitempty"`

	// ClusterName is the target cluster (cluster-admin and developer-team tiers).
	// +optional
	ClusterName string `json:"clusterName,omitempty"`

	// Namespace is the target namespace and the pod's placement namespace (developer-team tier only).
	// +optional
	Namespace string `json:"namespace,omitempty"`
}

// ParentRefSpec links a non-platform agent to its parent agent (06 §1.2). It is required for
// non-platform tiers. The cross-object ceiling — correct parent tier and child scope ⊂ parent scope
// — is enforced by the validating webhook as 06 §1.2 V-6 (P8-T9).
type ParentRefSpec struct {
	// Name is the parent Agent's name.
	// +optional
	Name string `json:"name,omitempty"`
}

// InitiativeBudgetSpec caps how much an agent may do on its own initiative, per origin and per risk
// class (06 §1.1). Every leaf is a CAP, never a grant: the value is the lower of what is written
// here and the code ceiling in the 06 §1.1 table, and a value ABOVE its ceiling is rejected at
// admission by V-8 rather than silently clamped.
//
// Rejecting rather than clamping is the whole point of the rule. A clamp makes an operator who asked
// for 500 elevated actions per hour believe they got them; the CR reads 500, the runtime enforces 10,
// and the disagreement surfaces as an incident nobody can explain from the manifest. 06 §1.2 V-8 is
// explicit that the leaf is "rejected, not silently clamped".
//
// The fields carry no authority on their own. Nothing in this phase reads them — the broker that
// spends the budget arrives with the imperative model — but the CEILING has to exist before the
// spender does, which is 07 §5's ordering constraint (machinery before authority) applied to the
// budget itself.
type InitiativeBudgetSpec struct {
	// SelfInitiated caps actions the agent started itself: trigger.source ∈
	// {watch, alert, cron, delegation, escalation} (06 §1.1).
	// +optional
	SelfInitiated *BudgetClassSpec `json:"selfInitiated,omitempty"`

	// HumanRequested caps actions a human asked for: trigger.source ∈ {chat, undo} (06 §1.1).
	// Deliberately a separate, larger allowance — a human in the loop is itself a control.
	// +optional
	HumanRequested *BudgetClassSpec `json:"humanRequested,omitempty"`

	// MaxObjectsPerAction is the per-envelope object cap. Default 25, code ceiling 50 — 50 is where
	// the code floor gates regardless (06 §4.2), so a higher value is meaningless and is rejected
	// rather than accepted-and-ignored.
	// +optional
	MaxObjectsPerAction *int32 `json:"maxObjectsPerAction,omitempty"`

	// FlapWindow is the window in which repeats of the same (target, intent) count towards
	// FlapThreshold. Default 30m, code FLOOR 5m — this is the one leaf where a SMALLER value is the
	// dangerous direction, because a short window lets a flapping agent reset its own counter.
	// +optional
	FlapWindow *metav1.Duration `json:"flapWindow,omitempty"`

	// FlapThreshold is how many repeats of the same (target, intent) within FlapWindow trip the
	// flap brake. Default 3, code ceiling 5.
	// +optional
	FlapThreshold *int32 `json:"flapThreshold,omitempty"`
}

// BudgetClassSpec is the per-risk-class allowance for one origin (06 §1.1). The same shape is used
// for both self-initiated and human-requested work; only the ceilings differ, and those live in the
// webhook rather than here because V-8 must name the offending leaf's field path in its rejection.
type BudgetClassSpec struct {
	// RoutinePerHour caps `routine` actions per rolling hour.
	// +optional
	RoutinePerHour *int32 `json:"routinePerHour,omitempty"`

	// ElevatedPerHour caps `elevated` actions per rolling hour — deliberately an order of magnitude
	// tighter than routine.
	// +optional
	ElevatedPerHour *int32 `json:"elevatedPerHour,omitempty"`

	// GatedPerHour caps `gated` SUBMISSIONS per rolling hour. Approval consumes nothing: a human is
	// already in the loop, so charging the approval too would penalise the safest path.
	// +optional
	GatedPerHour *int32 `json:"gatedPerHour,omitempty"`

	// ActionsPerDay caps all classes together per rolling 24h.
	// +optional
	ActionsPerDay *int32 `json:"actionsPerDay,omitempty"`
}

// NotifyClass is the minimum risk class that pings humans at once (06 §1.1).
// +kubebuilder:validation:Enum=routine;elevated;gated
type NotifyClass string

const (
	// NotifyRoutine notifies on everything.
	NotifyRoutine NotifyClass = "routine"
	// NotifyElevated notifies on elevated and above — the default posture.
	NotifyElevated NotifyClass = "elevated"
	// NotifyGated notifies only when a human decision is actually required.
	NotifyGated NotifyClass = "gated"
)

// OperationsSpec carries the operational brakes and caps (06 §1.1, §4.4).
//
// Phase 8 introduced `paused`, `pauseReason` and `initiativeBudget` as SCHEMA ONLY, because 06 §1.2
// V-6 and V-8 are admission rules about those fields and 07 §5 requires a ceiling to be enforceable
// before anything can spend against it. Phase 9 completes the block with the remaining four fields
// of 06 §1.1 and gives the brake a reader.
//
// Everything here is settable through `kubectl` alone. That is a requirement, not a consequence:
// 06 §4.4 specifies that all five brake controls work "with inference down — no dependency on the
// model, the router, or the agent pod", and a brake reachable only through the chat surface shares
// a failure domain with the thing it is supposed to stop.
type OperationsSpec struct {
	// Paused is THE BRAKE (03 §6). When true the broker refuses new envelopes for this agent — and,
	// per 06 §1.2 V-6, a paused agent may not act as a PARENT either: provisioning a child is an
	// action, so the brake covers it.
	//
	// `paused` is NOT scale-to-zero (08 §2.4, V-RUN-007, V-RUN-012). The pod keeps running, keeps
	// its work queue, and keeps observing; only the write path closes. Scaling to zero would look
	// equivalent and is not: it discards in-memory queue state, it makes the agent unable to report
	// why it is refusing, and it means "resume" is a cold start rather than a released brake — so
	// the operator who paused for thirty seconds during a deploy gets an agent that comes back
	// having forgotten what it was doing.
	// +kubebuilder:default=false
	// +optional
	Paused *bool `json:"paused,omitempty"`

	// PauseReason is free text set alongside Paused and surfaced in chat and status. Bounded because
	// it is operator-supplied and echoed into messages.
	// +kubebuilder:validation:MaxLength=512
	// +optional
	PauseReason string `json:"pauseReason,omitempty"`

	// DryRunOnly is shadow mode (06 §1.1): classify and journal every action, execute none.
	//
	// STRICTER-ONLY, like every other overlay in this system. It forces `dryRun: true` on the
	// envelope (06 §4.1) and there is no field anywhere that can force it back off, so the composed
	// result of dry-run mode and any policy is always dry-run. An agent cannot clear it, because
	// `Agent` is a control-plane object no agent identity may write.
	//
	// Distinct from `paused`: a paused agent refuses envelopes and produces no record, while a
	// dry-run agent produces a full classified, journaled `DryRun` record of what it WOULD have
	// done. That difference is the whole value of shadow mode — it is how an operator finds out
	// what an agent would do before granting it the authority to do it.
	// +kubebuilder:default=false
	// +optional
	DryRunOnly *bool `json:"dryRunOnly,omitempty"`

	// ApprovalRosterRef names the ApprovalRoster consulted for `gated` actions, and for `resume`
	// and `uncontest` (06 §4.4).
	//
	// A dangling reference is NOT an open gate. 06 §4.4's sixth fail-closed rule: with no roster to
	// consult, a gated action stays `PendingApproval` and expires unapproved. Admission does not
	// require the roster to exist, because ordering a roster before the agent that names it would
	// make a two-object install order-dependent; the runtime handles the gap by refusing, not by
	// skipping.
	// +optional
	ApprovalRosterRef *RosterRef `json:"approvalRosterRef,omitempty"`

	// ChangePolicyRefs are stricter-only classification overlays (06 §4.2). Ordered, all applied.
	//
	// "Ordered" is for reporting, not for precedence — the classifier takes the MAXIMUM class over
	// every source and the MINIMUM over every cap, so no ordering of these refs can change the
	// result. The order is preserved because `classification.reasons[]` reads better when the rules
	// appear in the order the operator wrote them.
	// +kubebuilder:validation:MaxItems=16
	// +optional
	ChangePolicyRefs []PolicyRef `json:"changePolicyRefs,omitempty"`

	// InitiativeBudget caps self-initiated and human-requested work per risk class (06 §1.1).
	// +optional
	InitiativeBudget *InitiativeBudgetSpec `json:"initiativeBudget,omitempty"`

	// NotifyOn is the minimum class that pings humans at once. Default `elevated`.
	//
	// `elevated` rather than `routine` because a notification stream that includes every routine
	// action is a stream nobody reads, and an unread notification is indistinguishable from one
	// that was never sent. `gated` actions always notify regardless — they are blocked on a human
	// by definition, so there is nothing for this field to suppress.
	// +kubebuilder:default=elevated
	// +optional
	NotifyOn NotifyClass `json:"notifyOn,omitempty"`
}

// PolicyRef names a cluster-scoped ChangePolicy. No namespace field: ChangePolicy is cluster-scoped
// (06 §4.2), and a namespace here would be a field the API server accepts and nothing reads.
type PolicyRef struct {
	// Name is the ChangePolicy's name.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=253
	Name string `json:"name"`
}

// Brake reports the three spec-side brake states with their defaults applied.
//
// One function rather than three nil-checks at each call site: every one of these defaults to the
// PERMISSIVE value when absent, so a forgotten nil-dereference guard reads as "not paused, not
// dry-run" — the direction that fails open. Keeping the three together also means a caller cannot
// consult `paused` and forget `dryRunOnly`, which is how shadow mode stops shadowing.
func (o *OperationsSpec) Brake() (paused, dryRun bool, reason string) {
	if o == nil {
		return false, false, ""
	}
	paused = o.Paused != nil && *o.Paused
	dryRun = o.DryRunOnly != nil && *o.DryRunOnly
	return paused, dryRun, o.PauseReason
}

// EffectiveNotifyOn applies the `elevated` default.
func (o *OperationsSpec) EffectiveNotifyOn() NotifyClass {
	if o == nil || o.NotifyOn == "" {
		return NotifyElevated
	}
	return o.NotifyOn
}

// IACFormat selects the infrastructure-as-code artifact an agent authors when proposing a change.
// +kubebuilder:validation:Enum=kcc;terraform
type IACFormat string

const (
	// IACFormatKCC authors Config Connector (KCC) YAML — the default customer standard.
	IACFormatKCC IACFormat = "kcc"
	// IACFormatTerraform authors Terraform HCL.
	IACFormatTerraform IACFormat = "terraform"
)

// IACSpec configures which IaC artifact the agent proposes via GitOps (06 §1.1, §4). Consumers treat
// an empty Format as "kcc" (the default applies when the iac object is present; nil iac ⇒ kcc).
type IACSpec struct {
	// Format is the artifact format the agent authors (kcc or terraform).
	// +kubebuilder:default=kcc
	// +optional
	Format IACFormat `json:"format,omitempty"`
}

// AgentSpec defines the common infrastructure configuration shared across all agent types.
type AgentSpec struct {
	// Tier is the agent's persona / containment level (06 §1.1). Immutable after creation.
	// Defaults to "platform" — the value the platform-tier Agent adopts.
	// +kubebuilder:default=platform
	// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="tier is immutable"
	// +optional
	Tier AgentTier `json:"tier,omitempty"`

	// Scope identifies where the agent operates (per-tier required fields, 06 §1.2).
	// +optional
	Scope *ScopeSpec `json:"scope,omitempty"`

	// ParentRef links a non-platform agent to its parent (06 §1.2). Required for non-platform tiers.
	// +optional
	ParentRef *ParentRefSpec `json:"parentRef,omitempty"`

	// Operations carries the operational brakes and caps (06 §1.1). Schema only in Phase 8 — the
	// admission rules V-6 (a paused parent may not provision) and V-8 (no leaf above its code
	// ceiling) are what read it today.
	// +optional
	Operations *OperationsSpec `json:"operations,omitempty"`

	// IAC selects the IaC artifact this agent authors when proposing changes via GitOps (06 §1.1, §4).
	// +optional
	IAC *IACSpec `json:"iac,omitempty"`

	// Deployment abstracts the Kubernetes Pod/Deployment configuration.
	// +optional
	Deployment *DeploymentSpec `json:"deployment,omitempty"`

	// Security configures RBAC, Pod Security, and Workload Identity.
	// +optional
	Security *SecuritySpec `json:"security,omitempty"`

	// Harness configures the core execution environment and framework-level settings.
	// +required
	Harness *HarnessSpec `json:"harness,omitempty"`

	// Integration configures external connections (GitHub, Google Chat, Slack).
	// +optional
	Integration *AgentIntegrationSpec `json:"integration,omitempty"`
}

type DeploymentStatus struct {
	// Name is the exact name of the underlying Kubernetes Deployment.
	// +optional
	Name string `json:"name,omitempty"`

	// ReadyReplicas indicates how many replicas are fully ready.
	// +optional
	ReadyReplicas int32 `json:"readyReplicas,omitempty"`
}

type ServiceStatus struct {
	// Endpoint is the primary URL or IP (including protocol and port) to reach the agent.
	// +optional
	Endpoint string `json:"endpoint,omitempty"`
}

type StorageStatus struct {
	// Bound indicates if the primary PVC has been successfully provisioned.
	// +optional
	Bound bool `json:"bound,omitempty"`
}

// AgentStatus defines the observed state of an agent.
type AgentStatus struct {
	// Phase is the overall state (Pending, Provisioning, Ready, Failed).
	// +optional
	Phase string `json:"phase,omitempty"`

	// Address is the fully qualified domain name (FQDN) of the agent service.
	// +optional
	Address string `json:"address,omitempty"`

	// LastReconcileTime is the timestamp when the operator last updated this status.
	// +optional
	LastReconcileTime *metav1.Time `json:"lastReconcileTime,omitempty"`

	// Conditions represent the latest available observations of the instance's state.
	// +listType=map
	// +listMapKey=type
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`

	// DeploymentStatus tracks the state of the underlying compute.
	// +optional
	DeploymentStatus DeploymentStatus `json:"deploymentStatus,omitempty"`

	// ServiceStatus holds internal/external endpoints.
	// +optional
	ServiceStatus ServiceStatus `json:"serviceStatus,omitempty"`

	// StorageStatus tracks PVC binding state.
	// +optional
	StorageStatus StorageStatus `json:"storageStatus,omitempty"`

	// Operations is the observed brake state (06 §1.1).
	// +optional
	Operations *OperationsStatus `json:"operations,omitempty"`

	// Broker is the observed state of this agent's broker sidecar (06 §1.1).
	// +optional
	Broker *BrokerStatus `json:"broker,omitempty"`
}

// OperationsStatus is the OBSERVED brake state (06 §1.1) — what is actually in force, as opposed to
// `spec.operations`, which is what somebody asked for.
//
// The two are separate because they answer different questions and can legitimately disagree. An
// agent with `spec.operations.paused: false` can still be executing nothing, because a FleetFreeze
// covers its scope; `frozenBy` is the only place that shows it. Reading the spec alone during an
// incident produces the conclusion "the agent is not paused, so why is nothing happening".
type OperationsStatus struct {
	// Paused is whether the brake is actually engaged.
	// +optional
	Paused bool `json:"paused,omitempty"`

	// PausedSince is when it engaged.
	// +optional
	PausedSince *metav1.Time `json:"pausedSince,omitempty"`

	// PausedBy is the chat user ID or Kubernetes username that set the brake. Free text rather than
	// a V-11-patterned principal, unlike the spec-side `requestedBy` fields on the brake objects:
	// this is written by the controller from whatever the API server reported as the mutating user,
	// and a `system:serviceaccount:…` username has no platform prefix to canonicalize to. A pattern
	// here would force the controller to either discard the identity or invent one.
	// +kubebuilder:validation:MaxLength=253
	// +optional
	PausedBy string `json:"pausedBy,omitempty"`

	// Reason mirrors `spec.operations.pauseReason` at the moment the brake engaged, so editing the
	// spec reason later does not rewrite the history of why this pause happened.
	// +kubebuilder:validation:MaxLength=512
	// +optional
	Reason string `json:"reason,omitempty"`

	// DryRunOnly is whether shadow mode is in force.
	// +optional
	DryRunOnly bool `json:"dryRunOnly,omitempty"`

	// FrozenBy is the name of the FleetFreeze covering this scope, if any (06 §4.4). Empty means no
	// freeze — and, because a freeze is cluster-scoped and this field is per-agent, it is the only
	// per-agent answer to "am I covered", which otherwise requires listing every freeze and
	// evaluating each scope by hand.
	// +kubebuilder:validation:MaxLength=253
	// +optional
	FrozenBy string `json:"frozenBy,omitempty"`
}

// BrokerStatus is the observed state of the agent's broker sidecar (06 §1.1).
type BrokerStatus struct {
	// Endpoint is the broker's mTLS address: `https://<agent>-broker.<ns>.svc.cluster.local:8443`.
	// Reported so an operator can confirm which broker an agent is bound to without inspecting the
	// pod spec. It is a mirror, not an input — the agent reads the same value from an env var the
	// controller renders into the pod (08 §2.3), so editing this field points nothing anywhere.
	// +kubebuilder:validation:MaxLength=253
	// +optional
	Endpoint string `json:"endpoint,omitempty"`

	// ActorServiceAccount is the derived actor SA (06 §2). Reported, never settable: the CRD has,
	// and must never gain, a field that names it, because a spec-settable actor SA is a spec-settable
	// authority level.
	// +kubebuilder:validation:MaxLength=253
	// +optional
	ActorServiceAccount string `json:"actorServiceAccount,omitempty"`

	// Ready is whether the broker is accepting envelopes.
	// +optional
	Ready bool `json:"ready,omitempty"`

	// JournalReachable is false when the broker cannot reach the journal store, in which case it is
	// fail-closed and executing nothing (06 §4.4, fail-closed rule 3 — which also auto-pauses).
	//
	// It defaults to the Go zero value `false`, and that is the correct default for once: an agent
	// whose status has never been written has not demonstrated that its journal is reachable, and
	// the fail-closed reading of "unknown" is "unreachable". Every other boolean in this block
	// reads the same way — absent means the safe answer, not the convenient one.
	// +optional
	JournalReachable bool `json:"journalReachable,omitempty"`
}
