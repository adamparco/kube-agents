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

package webhook

import (
	"context"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func TestPlatformAgentValidation(t *testing.T) {
	ctx := context.Background()

	t.Run("fails if another platform agent already exists in the project", func(t *testing.T) {
		existingAgent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "existing-agent",
				Namespace: "kubeagents-system",
			},
			Spec: agentv1alpha1.PlatformAgentSpec{},
		}

		scheme := runtime.NewScheme()
		_ = agentv1alpha1.AddToScheme(scheme)
		fakeClient := fake.NewClientBuilder().WithScheme(scheme).WithObjects(existingAgent).Build()

		val := &PlatformAgentCustomValidator{
			Client: fakeClient,
		}

		newAgent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "new-agent",
				Namespace: "default",
			},
			Spec: agentv1alpha1.PlatformAgentSpec{},
		}

		_, err := val.ValidateCreate(ctx, newAgent)
		if err == nil {
			t.Error("expected validation to fail when another PlatformAgent already exists in the cluster")
		}
	})

	t.Run("allows creation when existing platform agent is terminating", func(t *testing.T) {
		now := metav1.Now()
		existingAgent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:              "existing-agent",
				Namespace:         "kubeagents-system",
				DeletionTimestamp: &now,
				Finalizers:        []string{"kubeagents.x-k8s.io/platformagent-webhook-lock"},
			},
			Spec: agentv1alpha1.PlatformAgentSpec{},
		}

		scheme := runtime.NewScheme()
		_ = agentv1alpha1.AddToScheme(scheme)
		fakeClient := fake.NewClientBuilder().WithScheme(scheme).WithObjects(existingAgent).Build()

		val := &PlatformAgentCustomValidator{
			Client: fakeClient,
		}

		newAgent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "new-agent",
				Namespace: "default",
			},
			Spec: agentv1alpha1.PlatformAgentSpec{},
		}

		_, err := val.ValidateCreate(ctx, newAgent)
		if err != nil {
			t.Errorf("unexpected validation failure: %v", err)
		}
	})

	t.Run("allows update to the same existing platform agent", func(t *testing.T) {
		existingAgent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:      "existing-agent",
				Namespace: "kubeagents-system",
			},
			Spec: agentv1alpha1.PlatformAgentSpec{},
		}

		scheme := runtime.NewScheme()
		_ = agentv1alpha1.AddToScheme(scheme)
		fakeClient := fake.NewClientBuilder().WithScheme(scheme).WithObjects(existingAgent).Build()

		val := &PlatformAgentCustomValidator{
			Client: fakeClient,
		}

		_, err := val.ValidateUpdate(ctx, nil, existingAgent)
		if err != nil {
			t.Errorf("unexpected error when updating the same existing PlatformAgent: %v", err)
		}
	})

	t.Run("allows update when the agent under validation is terminating to prevent deadlocks", func(t *testing.T) {
		val := &PlatformAgentCustomValidator{}

		now := metav1.Now()
		agent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{
				Name:              "test-agent",
				Namespace:         "kubeagents-system",
				DeletionTimestamp: &now,
			},
			Spec: agentv1alpha1.PlatformAgentSpec{
				Harness: &agentv1alpha1.HarnessSpec{ProjectID: "my-project", ClusterName: "my-cluster"},
			},
		}

		_, err := val.ValidateUpdate(ctx, nil, agent)
		if err != nil {
			t.Errorf("unexpected validation failure when updating terminating agent: %v", err)
		}
	})
}

// newTestClient builds a fake client seeded with objs and the agent scheme.
func newTestClient(t *testing.T, objs ...client.Object) client.Client {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := agentv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("failed to register scheme: %v", err)
	}
	return fake.NewClientBuilder().WithScheme(scheme).WithObjects(objs...).Build()
}

// agentWithScope builds a minimal PlatformAgent at the given tier + scope for cardinality tests.
func agentWithScope(name, ns string, tier agentv1alpha1.AgentTier, projectID, clusterName, namespace string) *agentv1alpha1.PlatformAgent {
	return &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns},
		Spec: agentv1alpha1.PlatformAgentSpec{
			AgentSpec: agentv1alpha1.AgentSpec{
				Tier:  tier,
				Scope: &agentv1alpha1.ScopeSpec{ProjectID: projectID, ClusterName: clusterName, Namespace: namespace},
			},
		},
	}
}

func TestPlatformAgentCardinality(t *testing.T) {
	ctx := context.Background()

	t.Run("rejects a duplicate (tier, scope)", func(t *testing.T) {
		existing := agentWithScope("agent-a", "kubeagents-system", agentv1alpha1.TierPlatform, "project-x", "", "")
		val := &PlatformAgentCustomValidator{Client: newTestClient(t, existing)}

		dup := agentWithScope("agent-b", "default", agentv1alpha1.TierPlatform, "project-x", "", "")
		if _, err := val.ValidateCreate(ctx, dup); err == nil {
			t.Error("expected rejection for a duplicate (platform, project-x)")
		}
	})

	t.Run("allows different scopes (projects) to coexist", func(t *testing.T) {
		existing := agentWithScope("agent-a", "kubeagents-system", agentv1alpha1.TierPlatform, "project-x", "", "")
		val := &PlatformAgentCustomValidator{Client: newTestClient(t, existing)}

		other := agentWithScope("agent-b", "default", agentv1alpha1.TierPlatform, "project-y", "", "")
		if _, err := val.ValidateCreate(ctx, other); err != nil {
			t.Errorf("expected platform agents for different projects to coexist, got: %v", err)
		}
	})

	t.Run("allows different tiers in the same project to coexist", func(t *testing.T) {
		existing := agentWithScope("agent-a", "kubeagents-system", agentv1alpha1.TierPlatform, "project-x", "", "")
		val := &PlatformAgentCustomValidator{Client: newTestClient(t, existing)}

		clusterAdmin := agentWithScope("agent-ca", "default", agentv1alpha1.TierClusterAdmin, "project-x", "cluster-1", "")
		if _, err := val.ValidateCreate(ctx, clusterAdmin); err != nil {
			t.Errorf("expected different tiers to coexist, got: %v", err)
		}
	})
}

func TestPlatformAgentTierImmutable(t *testing.T) {
	ctx := context.Background()
	val := &PlatformAgentCustomValidator{} // nil client: isolate the tier check from cardinality

	oldAgent := agentWithScope("agent-a", "kubeagents-system", agentv1alpha1.TierPlatform, "project-x", "", "")

	t.Run("rejects changing tier on update", func(t *testing.T) {
		newAgent := agentWithScope("agent-a", "kubeagents-system", agentv1alpha1.TierClusterAdmin, "project-x", "cluster-1", "")
		if _, err := val.ValidateUpdate(ctx, oldAgent, newAgent); err == nil {
			t.Error("expected rejection when tier changes on update")
		}
	})

	t.Run("allows a same-tier update", func(t *testing.T) {
		sameTier := agentWithScope("agent-a", "kubeagents-system", agentv1alpha1.TierPlatform, "project-x", "", "")
		if _, err := val.ValidateUpdate(ctx, oldAgent, sameTier); err != nil {
			t.Errorf("expected same-tier update to be allowed, got: %v", err)
		}
	})
}

func TestPlatformAgentClosedAllowlist(t *testing.T) {
	ctx := context.Background()
	val := &PlatformAgentCustomValidator{} // nil client: isolate the allowlist check from cardinality

	googleChatAgent := func(enabled bool, allowed []string) *agentv1alpha1.PlatformAgent {
		return &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{Name: "chat-agent", Namespace: "kubeagents-system"},
			Spec: agentv1alpha1.PlatformAgentSpec{
				Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
					GoogleChat: &agentv1alpha1.GoogleChatSpec{Enabled: ptr.To(enabled), AllowedUsers: allowed},
				},
			},
		}
	}

	t.Run("rejects google chat enabled with an empty allowlist", func(t *testing.T) {
		if _, err := val.ValidateCreate(ctx, googleChatAgent(true, nil)); err == nil {
			t.Error("expected rejection: enabled Google Chat with an empty allowlist is open to all users")
		}
	})

	t.Run("allows google chat enabled with a non-empty allowlist", func(t *testing.T) {
		if _, err := val.ValidateCreate(ctx, googleChatAgent(true, []string{"users/admin"})); err != nil {
			t.Errorf("unexpected rejection for a closed allowlist: %v", err)
		}
	})

	t.Run("allows google chat disabled with an empty allowlist", func(t *testing.T) {
		if _, err := val.ValidateCreate(ctx, googleChatAgent(false, nil)); err != nil {
			t.Errorf("unexpected rejection when the integration is disabled: %v", err)
		}
	})

	t.Run("rejects slack enabled with an empty allowlist", func(t *testing.T) {
		slackAgent := &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{Name: "slack-agent", Namespace: "kubeagents-system"},
			Spec: agentv1alpha1.PlatformAgentSpec{
				Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
					Slack: &agentv1alpha1.SlackSpec{Enabled: ptr.To(true)},
				},
			},
		}
		if _, err := val.ValidateCreate(ctx, slackAgent); err == nil {
			t.Error("expected rejection: enabled Slack with an empty allowlist is open to all users")
		}
	})
}
