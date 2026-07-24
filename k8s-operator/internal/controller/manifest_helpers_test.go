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

package controller

import (
	"reflect"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func TestResolveAgentImage(t *testing.T) {
	tests := []struct {
		name         string
		deployment   *agentv1alpha1.DeploymentSpec
		defaultImage string
		expected     string
	}{
		{
			name:         "nil deployment",
			deployment:   nil,
			defaultImage: "ghcr.io/gke-labs/kube-agents/platform-agent:latest",
			expected:     "ghcr.io/gke-labs/kube-agents/platform-agent:latest",
		},
		{
			name: "empty image in deployment",
			deployment: &agentv1alpha1.DeploymentSpec{
				Image: "",
			},
			defaultImage: "ghcr.io/gke-labs/kube-agents/platform-agent:latest",
			expected:     "ghcr.io/gke-labs/kube-agents/platform-agent:latest",
		},
		{
			name: "custom image without tag or digest",
			deployment: &agentv1alpha1.DeploymentSpec{
				Image: "my-custom-image",
			},
			defaultImage: "ghcr.io/gke-labs/kube-agents/platform-agent:latest",
			expected:     "my-custom-image:latest",
		},
		{
			name: "custom image with tag in image field",
			deployment: &agentv1alpha1.DeploymentSpec{
				Image: "my-custom-image:v1.0.0",
			},
			defaultImage: "ghcr.io/gke-labs/kube-agents/platform-agent:latest",
			expected:     "my-custom-image:v1.0.0",
		},
		{
			name: "custom image with digest in image field",
			deployment: &agentv1alpha1.DeploymentSpec{
				Image: "my-custom-image@sha256:568c460a8a65c92c892837fcf4b46c6a461e7127e4e04052cfdf10a56f2e2124",
			},
			defaultImage: "ghcr.io/gke-labs/kube-agents/platform-agent:latest",
			expected:     "my-custom-image@sha256:568c460a8a65c92c892837fcf4b46c6a461e7127e4e04052cfdf10a56f2e2124",
		},
		{
			name: "custom image with explicit tag field",
			deployment: &agentv1alpha1.DeploymentSpec{
				Image: "my-custom-image",
				Tag:   ptr.To("v2.0.0"),
			},
			defaultImage: "ghcr.io/gke-labs/kube-agents/platform-agent:latest",
			expected:     "my-custom-image:v2.0.0",
		},
		{
			name: "custom image with empty tag field fallback to latest",
			deployment: &agentv1alpha1.DeploymentSpec{
				Image: "my-custom-image",
				Tag:   ptr.To(""),
			},
			defaultImage: "ghcr.io/gke-labs/kube-agents/platform-agent:latest",
			expected:     "my-custom-image:latest",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := resolveAgentImage(tt.deployment, tt.defaultImage)
			if result != tt.expected {
				t.Errorf("resolveAgentImage() = %q, expected %q", result, tt.expected)
			}
		})
	}
}

func TestDefaultImageForTier(t *testing.T) {
	tests := []struct {
		name string
		tier agentv1alpha1.AgentTier
		want string
	}{
		{"platform", agentv1alpha1.TierPlatform, defaultPlatformAgentImage},
		{"cluster-admin", agentv1alpha1.TierClusterAdmin, defaultClusterAdminAgentImage},
		{"empty defaults to platform", agentv1alpha1.AgentTier(""), defaultPlatformAgentImage},
		{"developer-team falls back to platform (Phase 3)", agentv1alpha1.TierDeveloperTeam, defaultPlatformAgentImage},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := defaultImageForTier(tt.tier); got != tt.want {
				t.Errorf("defaultImageForTier(%q) = %q, want %q", tt.tier, got, tt.want)
			}
		})
	}
}

// TestBuildDeploymentTierAwareImage asserts the operator renders the per-tier baked image when a CR
// omits spec.deployment.image, and that an explicit image still overrides regardless of tier (P2-T8).
func TestBuildDeploymentTierAwareImage(t *testing.T) {
	newAgent := func(tier agentv1alpha1.AgentTier, image string) *agentv1alpha1.Agent {
		a := &agentv1alpha1.Agent{
			ObjectMeta: metav1.ObjectMeta{Name: "a", Namespace: "ns"},
			Spec:       agentv1alpha1.AgentSpec{Tier: tier},
		}
		if image != "" {
			a.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Image: image}
		}
		return a
	}

	tests := []struct {
		name  string
		agent *agentv1alpha1.Agent
		want  string
	}{
		{"cluster-admin default image", newAgent(agentv1alpha1.TierClusterAdmin, ""), defaultClusterAdminAgentImage},
		{"platform default image", newAgent(agentv1alpha1.TierPlatform, ""), defaultPlatformAgentImage},
		{"empty tier default image", newAgent(agentv1alpha1.AgentTier(""), ""), defaultPlatformAgentImage},
		{"explicit image overrides on cluster-admin", newAgent(agentv1alpha1.TierClusterAdmin, "gcr.io/x/custom:v9"), "gcr.io/x/custom:v9"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			dep := buildDeployment(tt.agent, "h1", "h2", "h3")
			got := dep.Spec.Template.Spec.Containers[0].Image
			if got != tt.want {
				t.Errorf("rendered container image = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestMergeEnvVars(t *testing.T) {
	tests := []struct {
		name     string
		defaults []corev1.EnvVar
		custom   []corev1.EnvVar
		expected []corev1.EnvVar
	}{
		{
			name:     "empty custom returns defaults",
			defaults: []corev1.EnvVar{{Name: "A", Value: "1"}},
			custom:   nil,
			expected: []corev1.EnvVar{{Name: "A", Value: "1"}},
		},
		{
			name:     "empty defaults returns custom",
			defaults: nil,
			custom:   []corev1.EnvVar{{Name: "B", Value: "2"}},
			expected: []corev1.EnvVar{{Name: "B", Value: "2"}},
		},
		{
			name:     "no overlap, appends custom",
			defaults: []corev1.EnvVar{{Name: "A", Value: "1"}},
			custom:   []corev1.EnvVar{{Name: "B", Value: "2"}},
			expected: []corev1.EnvVar{{Name: "A", Value: "1"}, {Name: "B", Value: "2"}},
		},
		{
			name:     "overlap, custom overrides default",
			defaults: []corev1.EnvVar{{Name: "A", Value: "1"}, {Name: "B", Value: "2"}},
			custom:   []corev1.EnvVar{{Name: "B", Value: "3"}},
			expected: []corev1.EnvVar{{Name: "A", Value: "1"}, {Name: "B", Value: "3"}},
		},
		{
			name:     "duplicate custom, last one wins",
			defaults: []corev1.EnvVar{{Name: "A", Value: "1"}},
			custom:   []corev1.EnvVar{{Name: "B", Value: "2"}, {Name: "B", Value: "3"}},
			expected: []corev1.EnvVar{{Name: "A", Value: "1"}, {Name: "B", Value: "3"}},
		},
		{
			name:     "duplicate custom overrides default, last one wins",
			defaults: []corev1.EnvVar{{Name: "A", Value: "1"}, {Name: "B", Value: "2"}},
			custom:   []corev1.EnvVar{{Name: "B", Value: "3"}, {Name: "B", Value: "4"}},
			expected: []corev1.EnvVar{{Name: "A", Value: "1"}, {Name: "B", Value: "4"}},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := mergeEnvVars(tt.defaults, tt.custom)
			if !reflect.DeepEqual(result, tt.expected) {
				t.Errorf("mergeEnvVars() = %v, expected %v", result, tt.expected)
			}
		})
	}
}

func TestResolveDeploymentReplicasAndStrategy(t *testing.T) {
	tests := []struct {
		name             string
		deployment       *agentv1alpha1.DeploymentSpec
		expectedReplicas int32
		expectedStrategy appsv1.DeploymentStrategyType
	}{
		{
			name:             "nil deployment returns defaults",
			deployment:       nil,
			expectedReplicas: 1,
			expectedStrategy: appsv1.RecreateDeploymentStrategyType,
		},
		{
			name: "scale to zero enabled",
			deployment: &agentv1alpha1.DeploymentSpec{
				ScaleToZero: ptr.To(true),
			},
			expectedReplicas: 0,
			expectedStrategy: appsv1.RecreateDeploymentStrategyType,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			replicas, strategy := resolveDeploymentReplicasAndStrategy(tt.deployment)
			if replicas != tt.expectedReplicas {
				t.Errorf("expected replicas %d, got %d", tt.expectedReplicas, replicas)
			}
			if strategy.Type != tt.expectedStrategy {
				t.Errorf("expected strategy %s, got %s", tt.expectedStrategy, strategy.Type)
			}
		})
	}
}

func TestMergeAnnotations(t *testing.T) {
	defaults := map[string]string{"a": "1", "b": "2"}
	custom := map[string]string{"b": "override", "c": "3"}
	result := mergeAnnotations(defaults, custom)
	expected := map[string]string{"a": "1", "b": "override", "c": "3"}
	if !reflect.DeepEqual(result, expected) {
		t.Errorf("expected %v, got %v", expected, result)
	}

	// Test immutability when custom is empty
	emptyCustomResult := mergeAnnotations(defaults, nil)
	if !reflect.DeepEqual(emptyCustomResult, defaults) {
		t.Errorf("expected %v, got %v", defaults, emptyCustomResult)
	}
	emptyCustomResult["a"] = "mutated"
	if defaults["a"] == "mutated" {
		t.Errorf("expected defaults map not to be mutated when result map is changed")
	}

	// Test nil when both empty
	if nilResult := mergeAnnotations(nil, nil); nilResult != nil {
		t.Errorf("expected nil when both defaults and custom are nil, got %v", nilResult)
	}
}
