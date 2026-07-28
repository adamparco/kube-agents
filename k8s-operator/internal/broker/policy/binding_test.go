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

package policy

import (
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/scope"
)

func cp(name string, sel *agentv1alpha1.ChangePolicyAgentSelector, rules ...agentv1alpha1.ChangeRule) *agentv1alpha1.ChangePolicy {
	return &agentv1alpha1.ChangePolicy{
		ObjectMeta: metav1.ObjectMeta{Name: name},
		Spec: agentv1alpha1.ChangePolicySpec{
			AgentSelector: sel,
			Rules:         rules,
		},
	}
}

func gateDeletes(id string) agentv1alpha1.ChangeRule {
	return agentv1alpha1.ChangeRule{
		ID:     id,
		When:   agentv1alpha1.ChangeRuleWhen{Verbs: []agentv1alpha1.ChangeVerb{"delete"}},
		Class:  agentv1alpha1.ChangePolicyClassGated,
		Reason: "trust-building period: all deletes are reviewed",
	}
}

func devTeamAgent() Agent {
	return Agent{
		Tier:  agentv1alpha1.TierDeveloperTeam,
		Scope: scope.Scope{ProjectID: "p", ClusterName: "c", Namespace: "team-a"},
	}
}

func TestBindsTierClause(t *testing.T) {
	a := devTeamAgent()

	for _, tc := range []struct {
		name  string
		tiers []agentv1alpha1.AgentTier
		want  bool
	}{
		{"empty tiers matches every tier", nil, true},
		{"exact match binds", []agentv1alpha1.AgentTier{agentv1alpha1.TierDeveloperTeam}, true},
		{"one of several binds", []agentv1alpha1.AgentTier{agentv1alpha1.TierPlatform, agentv1alpha1.TierDeveloperTeam}, true},
		{"a different tier does not bind", []agentv1alpha1.AgentTier{agentv1alpha1.TierPlatform}, false},
		// The tiers are kinds of authority, not amounts of it. A policy aimed at cluster-admin must
		// not silently also bind the developer-team agents beneath it -- an operator writing
		// "gate cluster-admin deletes" did not ask to gate every team's deletes.
		{"the tier above does not bind downwards", []agentv1alpha1.AgentTier{agentv1alpha1.TierClusterAdmin}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			p := cp("p", &agentv1alpha1.ChangePolicyAgentSelector{Tiers: tc.tiers}, gateDeletes("r"))
			if got := Binds(p, a); got != tc.want {
				t.Fatalf("Binds = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestBindsScopeClauseIsAtOrBeneath(t *testing.T) {
	a := devTeamAgent() // p / c / team-a

	for _, tc := range []struct {
		name   string
		scopes []agentv1alpha1.ScopeSpec
		want   bool
	}{
		{"empty scopes matches every scope", nil, true},
		{"the fleet-wide entry matches", []agentv1alpha1.ScopeSpec{{}}, true},
		{"the project above binds", []agentv1alpha1.ScopeSpec{{ProjectID: "p"}}, true},
		{"the cluster above binds", []agentv1alpha1.ScopeSpec{{ProjectID: "p", ClusterName: "c"}}, true},
		{"the exact scope binds", []agentv1alpha1.ScopeSpec{{ProjectID: "p", ClusterName: "c", Namespace: "team-a"}}, true},
		{"a sibling namespace does not bind", []agentv1alpha1.ScopeSpec{{ProjectID: "p", ClusterName: "c", Namespace: "team-b"}}, false},
		{"a different cluster does not bind", []agentv1alpha1.ScopeSpec{{ProjectID: "p", ClusterName: "other"}}, false},
		{"a different project does not bind", []agentv1alpha1.ScopeSpec{{ProjectID: "other"}}, false},
		{"a list is an OR", []agentv1alpha1.ScopeSpec{{ProjectID: "other"}, {ProjectID: "p"}}, true},
		// The hole-in-the-middle case: {clusterName: c} with no project would, under a naive
		// prefix walk, match cluster `c` in every project in the fleet. anyScopeContains skips it
		// rather than honouring it, and Build refuses the whole snapshot -- see the Build test.
		{"an ill-formed entry is not a wildcard", []agentv1alpha1.ScopeSpec{{ClusterName: "c"}}, false},
		{"an ill-formed entry does not poison a valid sibling", []agentv1alpha1.ScopeSpec{{ClusterName: "c"}, {ProjectID: "p"}}, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			p := cp("p", &agentv1alpha1.ChangePolicyAgentSelector{Scopes: tc.scopes}, gateDeletes("r"))
			if got := Binds(p, a); got != tc.want {
				t.Fatalf("Binds = %v, want %v", got, tc.want)
			}
		})
	}
}

// TestBindsANDsTheTwoClauses is the whole meaning of a two-clause selector. ORing them would widen
// every such policy in the fleet, and the operator could not diagnose it from the object they wrote.
func TestBindsANDsTheTwoClauses(t *testing.T) {
	sel := &agentv1alpha1.ChangePolicyAgentSelector{
		Tiers:  []agentv1alpha1.AgentTier{agentv1alpha1.TierDeveloperTeam},
		Scopes: []agentv1alpha1.ScopeSpec{{ProjectID: "p"}},
	}
	p := cp("p", sel, gateDeletes("r"))

	both := Agent{Tier: agentv1alpha1.TierDeveloperTeam, Scope: scope.Scope{ProjectID: "p", ClusterName: "c", Namespace: "team-a"}}
	tierOnly := Agent{Tier: agentv1alpha1.TierDeveloperTeam, Scope: scope.Scope{ProjectID: "elsewhere", ClusterName: "c", Namespace: "team-a"}}
	scopeOnly := Agent{Tier: agentv1alpha1.TierPlatform, Scope: scope.Scope{ProjectID: "p"}}

	if !Binds(p, both) {
		t.Error("an agent satisfying both clauses must bind")
	}
	if Binds(p, tierOnly) {
		t.Error("tier alone must not bind: the clauses are ANDed, so this policy is 'the developer-team agents in p', not 'every developer-team agent'")
	}
	if Binds(p, scopeOnly) {
		t.Error("scope alone must not bind: the clauses are ANDed, so this policy is not 'everything in p'")
	}
}

// TestBindsDefaultsToTheFleet: an absent selector is the documented "every agent", and it is also
// the fail-closed reading. A policy whose author left the selector out meant the fleet, and since
// the classifier only ever raises a class, binding too widely is the safe error.
func TestBindsDefaultsToTheFleet(t *testing.T) {
	if !Binds(cp("p", nil, gateDeletes("r")), devTeamAgent()) {
		t.Fatal("a nil AgentSelector must bind every agent")
	}
	if !Binds(cp("p", &agentv1alpha1.ChangePolicyAgentSelector{}, gateDeletes("r")), devTeamAgent()) {
		t.Fatal("an empty AgentSelector must bind every agent")
	}
	if Binds(nil, devTeamAgent()) {
		t.Fatal("a nil policy binds nothing")
	}
}

func TestIllFormedScopesNamesEveryOffendingIndex(t *testing.T) {
	p := cp("p", &agentv1alpha1.ChangePolicyAgentSelector{Scopes: []agentv1alpha1.ScopeSpec{
		{ProjectID: "p"},                 // 0: fine
		{ClusterName: "c"},               // 1: hole at project
		{Namespace: "n"},                 // 2: hole at project and cluster
		{ProjectID: "p", Namespace: "n"}, // 3: hole at cluster
	}}, gateDeletes("r"))

	got := IllFormedScopes(p)
	want := []int{1, 2, 3}
	if len(got) != len(want) {
		t.Fatalf("IllFormedScopes = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("IllFormedScopes = %v, want %v", got, want)
		}
	}
	if n := IllFormedScopes(cp("p", nil, gateDeletes("r"))); n != nil {
		t.Fatalf("a nil selector has no ill-formed scopes, got %v", n)
	}
}
