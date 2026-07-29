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
	"context"
	"errors"
	"strings"
	"testing"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/scope"
)

// V-BRK-026. The identity a ChangePolicy's agentSelector is evaluated against is this broker's OWN
// agent, and it is resolved live -- once per poll -- rather than pinned when the process starts.
//
// The whole file turns on one asymmetry. A ChangePolicy can only TIGHTEN a classification, so a
// policy that binds when it should not is the safe direction and a policy that fails to bind when
// it should is the unsafe one. Everything below is therefore arranged to fail if a binding is LOST:
// a stale scope loses bindings, a malformed scope loses bindings, and an unreadable Agent CR
// collapsed into the zero Agent loses all the scoped and tiered ones at once. None of the three
// produces an error at the point of use -- they produce a `policySources` list that is shorter than
// it should be, in an ActionRecord that reads as entirely normal.

// scopedPolicy gates deletes for one exact namespace.
func scopedPolicy(name, project, cluster, namespace string) *agentv1alpha1.ChangePolicy {
	return cp(name, &agentv1alpha1.ChangePolicyAgentSelector{
		Scopes: []agentv1alpha1.ScopeSpec{{ProjectID: project, ClusterName: cluster, Namespace: namespace}},
	}, gateDeletes(name+"-r1"))
}

func mustNames(t *testing.T, s *Source) []string {
	t.Helper()
	snap, err := s.Snapshot()
	if err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	return snap.Names()
}

// TestTheIdentityIsResolvedOnEveryPoll is the mechanism: move the agent, and the very next poll
// binds a different policy set. A source that read its identity once at construction passes every
// other test in this package and fails this one.
func TestTheIdentityIsResolvedOnEveryPoll(t *testing.T) {
	id := &fakeIdentity{agent: devTeamAgent()} // p / c / team-a
	clk := &fakeClock{t: testAt}
	l := &fakeLister{items: []agentv1alpha1.ChangePolicy{
		*scopedPolicy("for-team-a", "p", "c", "team-a"),
		*scopedPolicy("for-team-b", "p", "c", "team-b"),
	}}
	s, err := NewSource(SourceConfig{Reader: l, Identity: id.resolve, History: nothingSeen{}, Now: clk.now})
	if err != nil {
		t.Fatalf("NewSource: %v", err)
	}

	if err := s.Refresh(context.Background()); err != nil {
		t.Fatalf("Refresh: %v", err)
	}
	if got := mustNames(t, s); len(got) != 1 || got[0] != "for-team-a" {
		t.Fatalf("bound = %v, want only for-team-a", got)
	}
	if id.calls != 1 {
		t.Fatalf("the identity was resolved %d times for one poll, want 1", id.calls)
	}

	// The agent moves. Nothing about the POLICY set changes -- the lister returns the same two
	// objects -- so a source that binds differently here can only be re-reading its own identity.
	id.agent = Agent{Tier: agentv1alpha1.TierDeveloperTeam, Scope: scope.Scope{ProjectID: "p", ClusterName: "c", Namespace: "team-b"}}
	if err := s.Refresh(context.Background()); err != nil {
		t.Fatalf("Refresh after the scope edit: %v", err)
	}
	if got := mustNames(t, s); len(got) != 1 || got[0] != "for-team-b" {
		t.Fatalf("bound = %v after the agent moved to team-b, want only for-team-b", got)
	}
	if id.calls != 2 {
		t.Fatalf("the identity was resolved %d times for two polls, want 2", id.calls)
	}
}

// TestANonLeafScopeEditIsInvisibleToTheDeployment is the REASON the mechanism above has to exist,
// and it is the case a reviewer is most likely to argue away with "a scope edit rolls the pod".
//
// It does not, in general. The operator renders `--scope=` + scope.Of(agent).Leaf() into the broker
// Deployment (internal/controller/broker_manifests.go), and Leaf() is the DEEPEST set level. For a
// cluster-admin agent that is clusterName, so editing projectId changes no rendered argument, no
// rollout happens, and the pod keeps running with whatever it read at startup. Only the platform
// tier -- whose leaf IS its projectId -- would be rescued by a rollout.
func TestANonLeafScopeEditIsInvisibleToTheDeployment(t *testing.T) {
	before := scope.Scope{ProjectID: "p1", ClusterName: "c"}
	after := scope.Scope{ProjectID: "p2", ClusterName: "c"}
	if before.Leaf() != after.Leaf() {
		t.Fatalf("this test is built on the leaf NOT moving; got %q -> %q", before.Leaf(), after.Leaf())
	}

	id := &fakeIdentity{agent: Agent{Tier: agentv1alpha1.TierClusterAdmin, Scope: before}}
	clk := &fakeClock{t: testAt}
	l := &fakeLister{items: []agentv1alpha1.ChangePolicy{
		*cp("p1-only", &agentv1alpha1.ChangePolicyAgentSelector{
			Scopes: []agentv1alpha1.ScopeSpec{{ProjectID: "p1"}},
		}, gateDeletes("r1")),
		*cp("p2-only", &agentv1alpha1.ChangePolicyAgentSelector{
			Scopes: []agentv1alpha1.ScopeSpec{{ProjectID: "p2"}},
		}, gateDeletes("r2")),
	}}
	s, err := NewSource(SourceConfig{Reader: l, Identity: id.resolve, History: nothingSeen{}, Now: clk.now})
	if err != nil {
		t.Fatalf("NewSource: %v", err)
	}
	if err := s.Refresh(context.Background()); err != nil {
		t.Fatalf("Refresh: %v", err)
	}
	if got := mustNames(t, s); len(got) != 1 || got[0] != "p1-only" {
		t.Fatalf("bound = %v, want only p1-only", got)
	}

	id.agent = Agent{Tier: agentv1alpha1.TierClusterAdmin, Scope: after}
	if err := s.Refresh(context.Background()); err != nil {
		t.Fatalf("Refresh after the projectId edit: %v", err)
	}
	// The failure this catches: still bound to p1-only, so the policy the operator wrote for p2 --
	// the project the agent is now IN -- never applies, and nothing anywhere reports an error.
	if got := mustNames(t, s); len(got) != 1 || got[0] != "p2-only" {
		t.Fatalf("bound = %v after the agent moved to p2, want only p2-only", got)
	}
}

// TestAnUnreadableIdentityRetainsAndThenRefuses: the Agent CR going unreadable is the transient
// shape (usually it IS the API server), so it is treated like a failed List and not like a policy
// that will not load -- retained, aged on the same clock, refused past MaxPolicyStaleness.
func TestAnUnreadableIdentityRetainsAndThenRefuses(t *testing.T) {
	id := &fakeIdentity{agent: devTeamAgent()}
	clk := &fakeClock{t: testAt}
	l := &fakeLister{items: []agentv1alpha1.ChangePolicy{*scopedPolicy("for-team-a", "p", "c", "team-a")}}
	s, err := NewSource(SourceConfig{Reader: l, Identity: id.resolve, History: nothingSeen{}, Now: clk.now})
	if err != nil {
		t.Fatalf("NewSource: %v", err)
	}
	if err := s.Refresh(context.Background()); err != nil {
		t.Fatalf("Refresh: %v", err)
	}

	id.err = errors.New("agents.kubeagents.io \"dev-team-a\" is forbidden")
	before := l.calls
	if err := s.Refresh(context.Background()); err == nil {
		t.Fatal("Refresh must report an unreadable identity")
	} else if !strings.Contains(err.Error(), "own agent identity") {
		t.Fatalf("the error must name what could not be resolved: %v", err)
	}
	// Not merely an error: the List is not even attempted. A poll that cannot be scored must not
	// spend a request, and more to the point must not leave a half-applied snapshot behind.
	if l.calls != before {
		t.Fatalf("the lister was called %d extra times after the identity failed; the poll should stop first", l.calls-before)
	}

	// Retained. Inside the staleness window the broker keeps classifying against the last policy set
	// it saw, for the last identity it saw -- which is the pair that was true together.
	clk.advance(MaxPolicyStaleness - 1)
	if _, err := s.Current(); err != nil {
		t.Fatalf("Current must answer from the retained snapshot inside the window: %v", err)
	}

	// And then it stops. An Agent CR that stays unreadable is not a broker that classifies forever.
	clk.advance(2)
	if _, err := s.Current(); err == nil {
		t.Fatal("Current must refuse past MaxPolicyStaleness")
	} else if !strings.Contains(err.Error(), "own agent identity") {
		t.Fatalf("the refusal must still name the identity failure, not just the age: %v", err)
	}
}

// TestAMalformedOwnScopeIsRefusedAndDiscarded. A hole in the middle of the broker's own scope is
// the mirror image of the ill-formed POLICY scope the loader already refuses: there the hole is in
// the outer scope and matches too much, here it is in the inner one and gets matched by too little.
//
// Discarded rather than retained, because it is a load failure and not a read failure -- the CR was
// read successfully and says something unusable, and it will keep saying it until a human edits it.
func TestAMalformedOwnScopeIsRefusedAndDiscarded(t *testing.T) {
	id := &fakeIdentity{agent: devTeamAgent()}
	clk := &fakeClock{t: testAt}
	l := &fakeLister{items: []agentv1alpha1.ChangePolicy{*scopedPolicy("for-team-a", "p", "c", "team-a")}}
	s, err := NewSource(SourceConfig{Reader: l, Identity: id.resolve, History: nothingSeen{}, Now: clk.now})
	if err != nil {
		t.Fatalf("NewSource: %v", err)
	}
	if err := s.Refresh(context.Background()); err != nil {
		t.Fatalf("Refresh: %v", err)
	}

	// projectId missing under a set namespace: scope.Contains would stop at the first unnarrowed
	// level, so `{scopes: [{projectId: p}]}` no longer binds this agent at all.
	id.agent = Agent{Tier: agentv1alpha1.TierDeveloperTeam, Scope: scope.Scope{ClusterName: "c", Namespace: "team-a"}}
	if err := s.Refresh(context.Background()); err == nil {
		t.Fatal("a malformed own scope must be refused")
	} else if !strings.Contains(err.Error(), "own agent scope") {
		t.Fatalf("the error must distinguish the broker's own scope from a policy's: %v", err)
	}

	// Discarded, not aged: refused NOW, with no clock advance, so the operator who broke the CR
	// learns immediately rather than thirty seconds later from a staleness message.
	if _, err := s.Current(); err == nil {
		t.Fatal("Current answered from the last good snapshot while the broker's own scope was unusable")
	} else if !strings.Contains(err.Error(), "own agent scope") {
		t.Fatalf("the refusal must name the malformed scope, not report staleness: %v", err)
	}
}

// TestTheZeroScopeIsAnIdentityAndNotAnError is the negative control, and the one that stops the
// three tests above from being satisfied by "refuse anything that is not fully narrowed".
//
// A platform Agent may legally carry no spec.scope at all -- validateScopeAndParent returns early
// for that tier -- so Scope{} is a real fleet-wide identity. It must classify, binding the policies
// that narrow nothing and not the ones that do. Refusing it would make the platform tier
// unserviceable; accepting it as a stand-in for "unreadable" would make an unreadable Agent CR
// classify as the widest agent in the fleet, which is why Identity errors instead of returning it.
func TestTheZeroScopeIsAnIdentityAndNotAnError(t *testing.T) {
	fleetWide := Agent{Tier: agentv1alpha1.TierPlatform}
	if !fleetWide.Scope.IsWellFormed() || !fleetWide.Scope.IsZero() {
		t.Fatal("the zero scope must be both zero and well-formed for this test to mean anything")
	}

	id := &fakeIdentity{agent: fleetWide}
	clk := &fakeClock{t: testAt}
	l := &fakeLister{items: []agentv1alpha1.ChangePolicy{
		*cp("everywhere", nil, gateDeletes("r1")),
		*scopedPolicy("for-team-a", "p", "c", "team-a"),
	}}
	s, err := NewSource(SourceConfig{Reader: l, Identity: id.resolve, History: nothingSeen{}, Now: clk.now})
	if err != nil {
		t.Fatalf("NewSource: %v", err)
	}
	if err := s.Refresh(context.Background()); err != nil {
		t.Fatalf("a scopeless platform agent must classify, not refuse: %v", err)
	}
	if got := mustNames(t, s); len(got) != 1 || got[0] != "everywhere" {
		t.Fatalf("bound = %v, want only the unscoped policy", got)
	}
}
