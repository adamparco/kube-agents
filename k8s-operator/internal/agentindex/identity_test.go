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

package agentindex

import (
	"testing"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func agent(tier agentv1alpha1.AgentTier, project, cluster, ns string) *agentv1alpha1.Agent {
	a := &agentv1alpha1.Agent{}
	a.Spec.Tier = tier
	if project != "" || cluster != "" || ns != "" {
		a.Spec.Scope = &agentv1alpha1.ScopeSpec{ProjectID: project, ClusterName: cluster, Namespace: ns}
	}
	return a
}

func TestEffectiveTier_DefaultsToPlatform(t *testing.T) {
	if got := EffectiveTier(agent("", "p", "", "")); got != agentv1alpha1.TierPlatform {
		t.Fatalf("empty tier: want platform, got %q", got)
	}
	if got := EffectiveTier(agent(agentv1alpha1.TierClusterAdmin, "p", "c", "")); got != agentv1alpha1.TierClusterAdmin {
		t.Fatalf("explicit tier: want cluster-admin, got %q", got)
	}
}

func TestScopeIdentity_PerTierKey(t *testing.T) {
	tests := []struct {
		name string
		a    *agentv1alpha1.Agent
		want string
	}{
		{"platform keys on project", agent(agentv1alpha1.TierPlatform, "proj1", "ignored", "ignored"), "tier=platform;project=proj1"},
		{"empty tier == platform", agent("", "proj1", "", ""), "tier=platform;project=proj1"},
		{"cluster-admin keys on project+cluster", agent(agentv1alpha1.TierClusterAdmin, "proj1", "clusterA", "ignored"), "tier=cluster-admin;project=proj1;cluster=clusterA"},
		{"developer-team keys on project+cluster+namespace", agent(agentv1alpha1.TierDeveloperTeam, "proj1", "clusterA", "team-ns"), "tier=developer-team;project=proj1;cluster=clusterA;namespace=team-ns"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := ScopeIdentity(tt.a); got != tt.want {
				t.Errorf("ScopeIdentity() = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestIdentity_MatchesScopeIdentity(t *testing.T) {
	// The router resolves a parsed handle via Identity(); indexing a CR uses ScopeIdentity(). They must
	// produce the same key for the same (tier, scope) — this is the no-drift guarantee.
	a := agent(agentv1alpha1.TierClusterAdmin, "proj1", "clusterA", "")
	if Identity(agentv1alpha1.TierClusterAdmin, "proj1", "clusterA", "") != ScopeIdentity(a) {
		t.Fatal("Identity() and ScopeIdentity() disagree for the same (tier, scope)")
	}
}

func TestScopeIdentity_DifferentClustersDoNotCollide(t *testing.T) {
	a := ScopeIdentity(agent(agentv1alpha1.TierClusterAdmin, "proj1", "clusterA", ""))
	b := ScopeIdentity(agent(agentv1alpha1.TierClusterAdmin, "proj1", "clusterB", ""))
	if a == b {
		t.Fatalf("distinct clusters produced the same key: %q", a)
	}
}
