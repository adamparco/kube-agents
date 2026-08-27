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

package approval_test

import (
	"context"
	"testing"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval"
)

func TestResolveRosterHappyPath(t *testing.T) {
	ar := testRecord("ar-1", testNS, "slack:U01")
	agent := testAgent("my-agent", testNS, &agentv1alpha1.RosterRef{Name: "roster-1"})
	roster := testRoster("roster-1", testNS, 1, false, agentv1alpha1.Approver{Platform: "slack", ID: "U02"})
	c := fakeClient(t, agent, roster)

	got, reason := approval.ResolveRoster(context.Background(), c, ar)
	if got == nil {
		t.Fatalf("expected a resolved roster, got nil (reason: %s)", reason)
	}
	if got.Name != "roster-1" {
		t.Errorf("got roster %q, want roster-1", got.Name)
	}
}

func TestResolveRosterMissingAgentIsUnusable(t *testing.T) {
	ar := testRecord("ar-1", testNS, "slack:U01")
	c := fakeClient(t) // no Agent CR at all

	got, reason := approval.ResolveRoster(context.Background(), c, ar)
	if got != nil {
		t.Fatalf("expected nil roster for a missing agent, got %v", got)
	}
	if reason == "" {
		t.Error("expected a non-empty unusable reason")
	}
}

func TestResolveRosterMissingRosterRefIsUnusable(t *testing.T) {
	ar := testRecord("ar-1", testNS, "slack:U01")
	agent := testAgent("my-agent", testNS, nil) // no ApprovalRosterRef
	c := fakeClient(t, agent)

	got, reason := approval.ResolveRoster(context.Background(), c, ar)
	if got != nil {
		t.Fatalf("expected nil roster when the agent names no roster, got %v", got)
	}
	if reason == "" {
		t.Error("expected a non-empty unusable reason")
	}
}

func TestResolveRosterMissingRosterObjectIsUnusable(t *testing.T) {
	ar := testRecord("ar-1", testNS, "slack:U01")
	agent := testAgent("my-agent", testNS, &agentv1alpha1.RosterRef{Name: "does-not-exist"})
	c := fakeClient(t, agent)

	got, reason := approval.ResolveRoster(context.Background(), c, ar)
	if got != nil {
		t.Fatalf("expected nil roster for a dangling ref, got %v", got)
	}
	if reason == "" {
		t.Error("expected a non-empty unusable reason")
	}
}

// Belt and suspenders against admission's MinItems=1 on Spec.Approvers -- a roster edited to empty
// after admission, or read through a cache that has not caught up with a delete-then-recreate.
func TestResolveRosterWithNoApproversIsUnusable(t *testing.T) {
	ar := testRecord("ar-1", testNS, "slack:U01")
	agent := testAgent("my-agent", testNS, &agentv1alpha1.RosterRef{Name: "roster-1"})
	roster := testRoster("roster-1", testNS, 1, false) // no approvers
	c := fakeClient(t, agent, roster)

	got, reason := approval.ResolveRoster(context.Background(), c, ar)
	if got != nil {
		t.Fatalf("expected nil roster for a roster with no approvers, got %v", got)
	}
	if reason == "" {
		t.Error("expected a non-empty unusable reason")
	}
}

func TestResolveRosterCrossNamespace(t *testing.T) {
	ar := testRecord("ar-1", testNS, "slack:U01")
	agent := testAgent("my-agent", testNS, &agentv1alpha1.RosterRef{Name: "roster-1", Namespace: "shared"})
	roster := testRoster("roster-1", "shared", 1, false, agentv1alpha1.Approver{Platform: "slack", ID: "U02"})
	c := fakeClient(t, agent, roster)

	got, reason := approval.ResolveRoster(context.Background(), c, ar)
	if got == nil {
		t.Fatalf("expected the cross-namespace roster to resolve, got nil (reason: %s)", reason)
	}
}
