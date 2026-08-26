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

// Package agentindex computes the (tier, scope) identity key that both the admission webhook
// (one-agent-per-(tier,scope) cardinality, 06 §1.2) and the ChatOps router (derive-the-routing-table-
// from-the-cardinality-key, 06 §2b) key on. Keeping the derivation in one package means the webhook
// that guarantees uniqueness and the router that relies on it cannot drift by construction.
package agentindex

import (
	"fmt"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

// EffectiveTier returns the agent's tier, defaulting an empty value to platform (the CRD default) so
// stored objects written before defaulting compare equal to freshly-defaulted ones.
func EffectiveTier(agent *agentv1alpha1.Agent) agentv1alpha1.AgentTier {
	if agent.Spec.Tier == "" {
		return agentv1alpha1.TierPlatform
	}
	return agent.Spec.Tier
}

// Identity computes the (tier, scope) uniqueness/routing key from raw fields. The scope fields that
// matter are per-tier (06 §1.2): platform → projectId; cluster-admin → +clusterName; developer-team →
// +namespace. An empty tier is treated as platform. This is the single source of truth for the key,
// used both to index Agent CRs (from a CR) and to resolve a parsed chat handle (from parsed fields).
func Identity(tier agentv1alpha1.AgentTier, projectID, clusterName, namespace string) string {
	if tier == "" {
		tier = agentv1alpha1.TierPlatform
	}
	switch tier {
	case agentv1alpha1.TierClusterAdmin:
		return fmt.Sprintf("tier=%s;project=%s;cluster=%s", tier, projectID, clusterName)
	case agentv1alpha1.TierDeveloperTeam:
		return fmt.Sprintf("tier=%s;project=%s;cluster=%s;namespace=%s", tier, projectID, clusterName, namespace)
	default: // platform (and the empty/default case)
		return fmt.Sprintf("tier=%s;project=%s", tier, projectID)
	}
}

// ScopeIdentity returns the (tier, scope) key for an Agent CR (06 §1.2). Two agents that resolve to
// the same identity may not coexist, and exactly one CR resolves a given routing key.
func ScopeIdentity(agent *agentv1alpha1.Agent) string {
	var projectID, clusterName, namespace string
	if s := agent.Spec.Scope; s != nil {
		projectID = s.ProjectID
		clusterName = s.ClusterName
		namespace = s.Namespace
	}
	return Identity(EffectiveTier(agent), projectID, clusterName, namespace)
}
