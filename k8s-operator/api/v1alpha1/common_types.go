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
// non-platform tiers. The cross-object checks (correct parent tier; child ⊆ parent attenuation
// ceiling) are deferred to the hardening admission webhook (08 §5) — Phase 1 carries the field only.
type ParentRefSpec struct {
	// Name is the parent Agent's name.
	// +optional
	Name string `json:"name,omitempty"`
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
	// Defaults to "platform" — the only tier in Phase 1, and the value today's PlatformAgent adopts.
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

	// IAC selects the IaC artifact this agent authors when proposing changes via GitOps (06 §1.1, §4).
	// +optional
	IAC *IACSpec `json:"iac,omitempty"`

	// Deployment abstracts the Kubernetes Pod/Deployment configuration.
	// +optional
	Deployment *DeploymentSpec `json:"deployment,omitempty"`

	// Security configures RBAC, Pod Security, and Workload Identity.
	// +optional
	Security *SecuritySpec `json:"security,omitempty"`
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
}
