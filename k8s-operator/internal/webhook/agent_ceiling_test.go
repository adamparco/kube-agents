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
	"strings"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The L1 half of V-CTR-002 for the three rules P8-T9 implements: V-6 (cross-object ceiling), V-8
// (budget clamp) and V-10 (reader-only ServiceAccount override). The L2 half —
// dev/verify/webhook-negatives-l2.sh — proves the same rules fire inside a real API server, which
// is the only place the CRD's CEL is ever compiled. Neither replaces the other: these tests can
// cover cases a live fixture cannot reach cheaply (a terminating parent, an ambiguous name), and
// only the live suite can prove the webhook is actually wired into admission.
//
// Every negative below asserts the FIELD PATH in the message, not merely that an error occurred.
// V-CTR-002 requires it, because a rejection that does not say what to fix sends the operator to
// the CRD instead of to their own manifest.

// requireFieldError asserts that err is non-nil and names wantPath.
func requireFieldError(t *testing.T, err error, wantPath string) {
	t.Helper()
	if err == nil {
		t.Fatalf("expected rejection naming %q, got admission", wantPath)
	}
	if !strings.Contains(err.Error(), wantPath) {
		t.Errorf("expected the rejection to name the field path %q, got: %v", wantPath, err)
	}
}

// platformParent builds a platform-tier Agent usable as a parent.
func platformParent(name, project string) *agentv1alpha1.Agent {
	return &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "kubeagents-system"},
		Spec: agentv1alpha1.AgentSpec{
			Tier:  agentv1alpha1.TierPlatform,
			Scope: &agentv1alpha1.ScopeSpec{ProjectID: project},
		},
	}
}

// clusterAdminChild builds a cluster-admin Agent parented by parentName.
func clusterAdminChild(name, project, cluster, parentName string) *agentv1alpha1.Agent {
	return &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "kubeagents-system"},
		Spec: agentv1alpha1.AgentSpec{
			Tier:      agentv1alpha1.TierClusterAdmin,
			Scope:     &agentv1alpha1.ScopeSpec{ProjectID: project, ClusterName: cluster},
			ParentRef: &agentv1alpha1.ParentRefSpec{Name: parentName},
		},
	}
}

// TestV6_CrossObjectCeiling covers 06 §1.2 V-6.
func TestV6_CrossObjectCeiling(t *testing.T) {
	ctx := context.Background()
	const parentPath = "spec.parentRef.name"

	t.Run("admits a child strictly inside its parent's scope", func(t *testing.T) {
		val := &AgentCustomValidator{Client: newTestClient(t, platformParent("platform-agent", "project-x"))}
		child := clusterAdminChild("ca-1", "project-x", "cluster-1", "platform-agent")
		if _, err := val.ValidateCreate(ctx, child); err != nil {
			t.Errorf("expected a properly-attenuated child to be admitted, got: %v", err)
		}
	})

	t.Run("rejects a child whose parent does not exist", func(t *testing.T) {
		// The unverifiable case. Admitting here would create an agent whose authority ceiling was
		// never measured against anything.
		val := &AgentCustomValidator{Client: newTestClient(t)}
		child := clusterAdminChild("ca-1", "project-x", "cluster-1", "no-such-parent")
		_, err := val.ValidateCreate(ctx, child)
		requireFieldError(t, err, parentPath)
		if !strings.Contains(err.Error(), "does not exist") {
			t.Errorf("expected the message to say the parent does not exist, got: %v", err)
		}
	})

	t.Run("rejects a child whose parent is the wrong tier", func(t *testing.T) {
		// A developer-team agent parented by the PLATFORM agent skips a level: it would be bounded
		// by the project rather than by one cluster.
		parent := platformParent("platform-agent", "project-x")
		val := &AgentCustomValidator{Client: newTestClient(t, parent)}
		child := &agentv1alpha1.Agent{
			ObjectMeta: metav1.ObjectMeta{Name: "dt-1", Namespace: "team-x"},
			Spec: agentv1alpha1.AgentSpec{
				Tier:      agentv1alpha1.TierDeveloperTeam,
				Scope:     &agentv1alpha1.ScopeSpec{ProjectID: "project-x", ClusterName: "cluster-1", Namespace: "team-x"},
				ParentRef: &agentv1alpha1.ParentRefSpec{Name: "platform-agent"},
			},
		}
		_, err := val.ValidateCreate(ctx, child)
		requireFieldError(t, err, parentPath)
		if !strings.Contains(err.Error(), "cluster-admin") {
			t.Errorf("expected the message to name the tier that SHOULD have been the parent, got: %v", err)
		}
	})

	t.Run("rejects a child in a different project from its parent", func(t *testing.T) {
		val := &AgentCustomValidator{Client: newTestClient(t, platformParent("platform-agent", "project-x"))}
		child := clusterAdminChild("ca-1", "project-OTHER", "cluster-1", "platform-agent")
		_, err := val.ValidateCreate(ctx, child)
		requireFieldError(t, err, parentPath)
		if !strings.Contains(err.Error(), "project-x") {
			t.Errorf("expected the message to name the parent's project, got: %v", err)
		}
	})

	t.Run("rejects a child in a different cluster from its cluster-admin parent", func(t *testing.T) {
		parent := clusterAdminChild("ca-1", "project-x", "cluster-1", "platform-agent")
		val := &AgentCustomValidator{Client: newTestClient(t, platformParent("platform-agent", "project-x"), parent)}
		child := &agentv1alpha1.Agent{
			ObjectMeta: metav1.ObjectMeta{Name: "dt-1", Namespace: "team-x"},
			Spec: agentv1alpha1.AgentSpec{
				Tier:      agentv1alpha1.TierDeveloperTeam,
				Scope:     &agentv1alpha1.ScopeSpec{ProjectID: "project-x", ClusterName: "cluster-OTHER", Namespace: "team-x"},
				ParentRef: &agentv1alpha1.ParentRefSpec{Name: "ca-1"},
			},
		}
		_, err := val.ValidateCreate(ctx, child)
		requireFieldError(t, err, parentPath)
		if !strings.Contains(err.Error(), "cluster-1") {
			t.Errorf("expected the message to name the parent's cluster, got: %v", err)
		}
	})

	t.Run("rejects a child whose scope equals its parent's", func(t *testing.T) {
		// Strict subset. An equal-scope child has narrowed nothing and is an authority clone.
		parent := &agentv1alpha1.Agent{
			ObjectMeta: metav1.ObjectMeta{Name: "platform-agent", Namespace: "kubeagents-system"},
			Spec: agentv1alpha1.AgentSpec{
				Tier:  agentv1alpha1.TierPlatform,
				Scope: &agentv1alpha1.ScopeSpec{ProjectID: "project-x", ClusterName: "cluster-1"},
			},
		}
		val := &AgentCustomValidator{Client: newTestClient(t, parent)}
		child := clusterAdminChild("ca-1", "project-x", "cluster-1", "platform-agent")
		_, err := val.ValidateCreate(ctx, child)
		requireFieldError(t, err, parentPath)
		if !strings.Contains(err.Error(), "STRICT subset") {
			t.Errorf("expected the message to say the subset must be strict, got: %v", err)
		}
	})

	t.Run("rejects a child under a paused parent", func(t *testing.T) {
		// 03 §6: the brake covers provisioning. A pause that still permits fleet growth is not a pause.
		parent := platformParent("platform-agent", "project-x")
		parent.Spec.Operations = &agentv1alpha1.OperationsSpec{
			Paused:      ptr.To(true),
			PauseReason: "incident-4711",
		}
		val := &AgentCustomValidator{Client: newTestClient(t, parent)}
		child := clusterAdminChild("ca-1", "project-x", "cluster-1", "platform-agent")
		_, err := val.ValidateCreate(ctx, child)
		requireFieldError(t, err, parentPath)
		if !strings.Contains(err.Error(), "incident-4711") {
			t.Errorf("expected the pause REASON to be surfaced so the operator knows who to ask, got: %v", err)
		}
	})

	t.Run("admits a child under an explicitly unpaused parent", func(t *testing.T) {
		// Positive control for the arm above: paused=false must not be read as paused.
		parent := platformParent("platform-agent", "project-x")
		parent.Spec.Operations = &agentv1alpha1.OperationsSpec{Paused: ptr.To(false)}
		val := &AgentCustomValidator{Client: newTestClient(t, parent)}
		child := clusterAdminChild("ca-1", "project-x", "cluster-1", "platform-agent")
		if _, err := val.ValidateCreate(ctx, child); err != nil {
			t.Errorf("expected an unpaused parent to permit provisioning, got: %v", err)
		}
	})

	t.Run("rejects a child under a terminating parent", func(t *testing.T) {
		now := metav1.Now()
		parent := platformParent("platform-agent", "project-x")
		parent.DeletionTimestamp = &now
		parent.Finalizers = []string{"kubeagents.x-k8s.io/test"} // fake client requires one to keep the object
		val := &AgentCustomValidator{Client: newTestClient(t, parent)}
		child := clusterAdminChild("ca-1", "project-x", "cluster-1", "platform-agent")
		_, err := val.ValidateCreate(ctx, child)
		requireFieldError(t, err, parentPath)
		if !strings.Contains(err.Error(), "terminating") {
			t.Errorf("expected the message to say the parent is terminating, got: %v", err)
		}
	})

	t.Run("platform tier needs no parent", func(t *testing.T) {
		val := &AgentCustomValidator{Client: newTestClient(t)}
		if _, err := val.ValidateCreate(ctx, platformParent("platform-agent", "project-x")); err != nil {
			t.Errorf("expected the root tier to need no parent, got: %v", err)
		}
	})
}

// TestV8_BudgetClamp covers 06 §1.2 V-8. The ceilings are transcribed from the 06 §1.1 table; this
// test is what pins the transcription, so a typo in the webhook is a red test rather than a quietly
// looser cap.
func TestV8_BudgetClamp(t *testing.T) {
	ctx := context.Background()

	// budget builds a platform Agent carrying the given initiativeBudget.
	budget := func(b *agentv1alpha1.InitiativeBudgetSpec) *agentv1alpha1.Agent {
		a := platformParent("platform-agent", "project-x")
		a.Spec.Operations = &agentv1alpha1.OperationsSpec{InitiativeBudget: b}
		return a
	}

	// Every leaf of the 06 §1.1 table, with its ceiling. AT the ceiling must be admitted; ONE ABOVE
	// must be rejected naming the leaf. Testing both sides is what stops an off-by-one from passing
	// as strictness.
	leaves := []struct {
		name    string
		path    string
		ceiling int32
		set     func(*agentv1alpha1.InitiativeBudgetSpec, int32)
	}{
		{"selfInitiated.routinePerHour", "spec.operations.initiativeBudget.selfInitiated.routinePerHour", 50,
			func(b *agentv1alpha1.InitiativeBudgetSpec, v int32) { b.SelfInitiated.RoutinePerHour = ptr.To(v) }},
		{"selfInitiated.elevatedPerHour", "spec.operations.initiativeBudget.selfInitiated.elevatedPerHour", 10,
			func(b *agentv1alpha1.InitiativeBudgetSpec, v int32) { b.SelfInitiated.ElevatedPerHour = ptr.To(v) }},
		{"selfInitiated.gatedPerHour", "spec.operations.initiativeBudget.selfInitiated.gatedPerHour", 5,
			func(b *agentv1alpha1.InitiativeBudgetSpec, v int32) { b.SelfInitiated.GatedPerHour = ptr.To(v) }},
		{"selfInitiated.actionsPerDay", "spec.operations.initiativeBudget.selfInitiated.actionsPerDay", 500,
			func(b *agentv1alpha1.InitiativeBudgetSpec, v int32) { b.SelfInitiated.ActionsPerDay = ptr.To(v) }},
		{"humanRequested.routinePerHour", "spec.operations.initiativeBudget.humanRequested.routinePerHour", 200,
			func(b *agentv1alpha1.InitiativeBudgetSpec, v int32) { b.HumanRequested.RoutinePerHour = ptr.To(v) }},
		{"humanRequested.elevatedPerHour", "spec.operations.initiativeBudget.humanRequested.elevatedPerHour", 60,
			func(b *agentv1alpha1.InitiativeBudgetSpec, v int32) { b.HumanRequested.ElevatedPerHour = ptr.To(v) }},
		{"humanRequested.gatedPerHour", "spec.operations.initiativeBudget.humanRequested.gatedPerHour", 30,
			func(b *agentv1alpha1.InitiativeBudgetSpec, v int32) { b.HumanRequested.GatedPerHour = ptr.To(v) }},
		{"humanRequested.actionsPerDay", "spec.operations.initiativeBudget.humanRequested.actionsPerDay", 2000,
			func(b *agentv1alpha1.InitiativeBudgetSpec, v int32) { b.HumanRequested.ActionsPerDay = ptr.To(v) }},
		{"maxObjectsPerAction", "spec.operations.initiativeBudget.maxObjectsPerAction", 50,
			func(b *agentv1alpha1.InitiativeBudgetSpec, v int32) { b.MaxObjectsPerAction = ptr.To(v) }},
		{"flapThreshold", "spec.operations.initiativeBudget.flapThreshold", 5,
			func(b *agentv1alpha1.InitiativeBudgetSpec, v int32) { b.FlapThreshold = ptr.To(v) }},
	}

	if len(leaves) != 10 {
		t.Fatalf("06 §1.1 defines ten int-valued budget leaves; this table has %d", len(leaves))
	}

	for _, leaf := range leaves {
		t.Run(leaf.name+" is rejected one above its ceiling", func(t *testing.T) {
			b := &agentv1alpha1.InitiativeBudgetSpec{
				SelfInitiated:  &agentv1alpha1.BudgetClassSpec{},
				HumanRequested: &agentv1alpha1.BudgetClassSpec{},
			}
			leaf.set(b, leaf.ceiling+1)
			val := &AgentCustomValidator{}
			_, err := val.ValidateCreate(ctx, budget(b))
			requireFieldError(t, err, leaf.path)
		})

		t.Run(leaf.name+" is admitted at its ceiling", func(t *testing.T) {
			b := &agentv1alpha1.InitiativeBudgetSpec{
				SelfInitiated:  &agentv1alpha1.BudgetClassSpec{},
				HumanRequested: &agentv1alpha1.BudgetClassSpec{},
			}
			leaf.set(b, leaf.ceiling)
			val := &AgentCustomValidator{}
			if _, err := val.ValidateCreate(ctx, budget(b)); err != nil {
				t.Errorf("expected the ceiling itself to be admitted (the boundary is inclusive), got: %v", err)
			}
		})
	}

	t.Run("flapWindow below the 5m floor is rejected", func(t *testing.T) {
		b := &agentv1alpha1.InitiativeBudgetSpec{
			FlapWindow: &metav1.Duration{Duration: time.Minute},
		}
		val := &AgentCustomValidator{}
		_, err := val.ValidateCreate(ctx, budget(b))
		requireFieldError(t, err, "spec.operations.initiativeBudget.flapWindow")
	})

	t.Run("flapWindow at the 5m floor is admitted", func(t *testing.T) {
		b := &agentv1alpha1.InitiativeBudgetSpec{
			FlapWindow: &metav1.Duration{Duration: 5 * time.Minute},
		}
		val := &AgentCustomValidator{}
		if _, err := val.ValidateCreate(ctx, budget(b)); err != nil {
			t.Errorf("expected the floor itself to be admitted, got: %v", err)
		}
	})

	t.Run("a longer flapWindow is admitted", func(t *testing.T) {
		// The floor is a floor, not an equality. A conservative operator asking for a 2h window is
		// asking for MORE braking, and a rule that refused it would punish caution.
		b := &agentv1alpha1.InitiativeBudgetSpec{
			FlapWindow: &metav1.Duration{Duration: 2 * time.Hour},
		}
		val := &AgentCustomValidator{}
		if _, err := val.ValidateCreate(ctx, budget(b)); err != nil {
			t.Errorf("expected a window above the floor to be admitted, got: %v", err)
		}
	})

	t.Run("an absent budget is admitted", func(t *testing.T) {
		val := &AgentCustomValidator{}
		if _, err := val.ValidateCreate(ctx, platformParent("platform-agent", "project-x")); err != nil {
			t.Errorf("expected an Agent with no operations block to be admitted, got: %v", err)
		}
	})
}

// TestV10_ReaderOnlyServiceAccount covers 06 §1.2 V-10.
func TestV10_ReaderOnlyServiceAccount(t *testing.T) {
	ctx := context.Background()
	const saPath = "spec.security.serviceAccountName"

	withSA := func(tier agentv1alpha1.AgentTier, sa string) *agentv1alpha1.Agent {
		a := platformParent("platform-agent", "project-x")
		a.Spec.Tier = tier
		a.Spec.Security = &agentv1alpha1.SecuritySpec{ServiceAccountName: sa}
		return a
	}

	t.Run("admits the tier's own reader SA", func(t *testing.T) {
		val := &AgentCustomValidator{}
		if _, err := val.ValidateCreate(ctx, withSA(agentv1alpha1.TierPlatform, "platform-agent")); err != nil {
			t.Errorf("expected the reader SA name to be admitted, got: %v", err)
		}
	})

	t.Run("admits an empty override", func(t *testing.T) {
		// Empty means "let the controller derive it", which is the safe default.
		val := &AgentCustomValidator{}
		if _, err := val.ValidateCreate(ctx, withSA(agentv1alpha1.TierPlatform, "")); err != nil {
			t.Errorf("expected an empty override to be admitted, got: %v", err)
		}
	})

	t.Run("rejects an actor SA", func(t *testing.T) {
		// The rule's whole purpose: an agent must not be able to point its pod at the identity that
		// holds the scoped write authority its reader is defined not to have.
		val := &AgentCustomValidator{}
		_, err := val.ValidateCreate(ctx, withSA(agentv1alpha1.TierPlatform, "platform-project-x-actor"))
		requireFieldError(t, err, saPath)
	})

	t.Run("rejects another tier's reader SA", func(t *testing.T) {
		val := &AgentCustomValidator{}
		_, err := val.ValidateCreate(ctx, withSA(agentv1alpha1.TierPlatform, "cluster-admin-agent"))
		requireFieldError(t, err, saPath)
	})

	t.Run("rejects an arbitrary privileged SA", func(t *testing.T) {
		val := &AgentCustomValidator{}
		_, err := val.ValidateCreate(ctx, withSA(agentv1alpha1.TierPlatform, "default"))
		requireFieldError(t, err, saPath)
	})
}
