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
	"strings"
	"testing"
	"time"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/classify"
)

var testAt = time.Date(2026, 7, 28, 12, 0, 0, 0, time.UTC)

// nothingSeen makes every action novel. Constant, so a snapshot test is never testing the history.
type nothingSeen struct{}

func (nothingSeen) Seen(string, string, classify.KindRef, string) bool { return false }

func TestBuildOrdersPolicySourcesByName(t *testing.T) {
	// Deliberately not in name order, and deliberately not in reverse either: a Build that returned
	// input order and a Build that returned reverse order would both pass a two-element test.
	in := []*agentv1alpha1.ChangePolicy{
		cp("mid-policy", nil, gateDeletes("r1")),
		cp("aaa-policy", nil, gateDeletes("r2")),
		cp("zzz-policy", nil, gateDeletes("r3")),
	}
	snap, err := Build(in, devTeamAgent(), nothingSeen{}, testAt)
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	if got := strings.Join(snap.Names(), ","); got != "aaa-policy,mid-policy,zzz-policy" {
		t.Fatalf("Names = %q, want sorted; policySources[] lands in an ActionRecord and must not vary between two identical actions", got)
	}
	if !snap.ObservedAt().Equal(testAt) {
		t.Fatalf("ObservedAt = %v, want %v", snap.ObservedAt(), testAt)
	}
	if snap.Classifier() == nil {
		t.Fatal("a snapshot returned without an error must carry a classifier")
	}
}

func TestBuildKeepsOnlyBoundPolicies(t *testing.T) {
	other := &agentv1alpha1.ChangePolicyAgentSelector{Tiers: []agentv1alpha1.AgentTier{agentv1alpha1.TierPlatform}}
	in := []*agentv1alpha1.ChangePolicy{
		cp("binds", nil, gateDeletes("r1")),
		cp("does-not-bind", other, gateDeletes("r2")),
	}
	snap, err := Build(in, devTeamAgent(), nothingSeen{}, testAt)
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	if got := snap.Names(); len(got) != 1 || got[0] != "binds" {
		t.Fatalf("Names = %v, want [binds]", got)
	}
}

// TestBuildRefusesTheWholeSnapshotOnABadRule is the fail-closed property, and it is the reason this
// package returns errors where a more forgiving loader would log and continue.
//
// The classifier maxes over its sources. A policy that is silently skipped therefore does not
// produce a slightly-wrong classification -- it produces the classification the operator wrote the
// policy to prevent, in a record whose policySources[] does not mention the policy and which
// therefore looks entirely normal to whoever reads it afterwards.
func TestBuildRefusesTheWholeSnapshotOnABadRule(t *testing.T) {
	// `routine` can never have an effect and is refused by both admission and the loader; it stands
	// in here for any rule the two sides could disagree about.
	bad := agentv1alpha1.ChangeRule{
		ID:     "downgrade-attempt",
		When:   agentv1alpha1.ChangeRuleWhen{Verbs: []agentv1alpha1.ChangeVerb{"delete"}},
		Class:  agentv1alpha1.ChangePolicyClass(agentv1alpha1.RiskRoutine),
		Reason: "this should not load",
	}
	in := []*agentv1alpha1.ChangePolicy{
		cp("good-policy", nil, gateDeletes("r1")),
		cp("bad-policy", nil, bad),
	}
	snap, err := Build(in, devTeamAgent(), nothingSeen{}, testAt)
	if err == nil {
		t.Fatalf("Build succeeded with names %v; a bad policy must fail the whole snapshot, not be skipped", snap.Names())
	}
	if snap != nil {
		t.Fatal("Build must return no snapshot alongside its error; a partial policy set is an unknown one")
	}
	if !strings.Contains(err.Error(), "bad-policy") {
		t.Fatalf("the error must name the offending policy so an operator can find it: %v", err)
	}
}

// TestBuildRefusesAnIllFormedSelectorScope: an ill-formed scope is not "a policy that does not bind
// me". It is a policy whose containment answer is not trustworthy in either direction, so the
// broker declines to have an opinion about the whole set rather than quietly deciding it is exempt.
func TestBuildRefusesAnIllFormedSelectorScope(t *testing.T) {
	sel := &agentv1alpha1.ChangePolicyAgentSelector{Scopes: []agentv1alpha1.ScopeSpec{{ClusterName: "c"}}}
	in := []*agentv1alpha1.ChangePolicy{cp("holey", sel, gateDeletes("r1"))}

	_, err := Build(in, devTeamAgent(), nothingSeen{}, testAt)
	if err == nil {
		t.Fatal("Build must refuse a policy whose selector scope has a hole in the middle, even though Binds reports it does not bind")
	}
	if !strings.Contains(err.Error(), "holey") || !strings.Contains(err.Error(), "scopes[0]") {
		t.Fatalf("the error must name the policy and the offending index: %v", err)
	}
}

func TestBuildOnAnEmptyClusterIsTheCodeFloorAlone(t *testing.T) {
	snap, err := Build(nil, devTeamAgent(), nothingSeen{}, testAt)
	if err != nil {
		t.Fatalf("Build with no policies: %v", err)
	}
	if n := len(snap.Names()); n != 0 {
		t.Fatalf("Names = %v, want empty", snap.Names())
	}
	if snap.Classifier() == nil {
		t.Fatal("no policies still means a classifier -- the code floor always applies")
	}
}

// TestSnapshotNamesIsACopy: the snapshot is shared across every concurrent submission, so a caller
// that appended to the returned slice would be writing into another request's policy sources.
func TestSnapshotNamesIsACopy(t *testing.T) {
	snap, err := Build([]*agentv1alpha1.ChangePolicy{cp("a", nil, gateDeletes("r"))}, devTeamAgent(), nothingSeen{}, testAt)
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	got := snap.Names()
	got[0] = "mutated"
	if snap.Names()[0] != "a" {
		t.Fatal("Names must return a copy")
	}
}
