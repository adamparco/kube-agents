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

package pipeline

import (
	"context"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker"
)

// park submits a gated envelope and returns the resulting parked record — the state the
// resumption loop always starts from.
func park(t *testing.T, r *rig, env *broker.Envelope) *agentv1alpha1.ActionRecord {
	t.Helper()
	env.RequireApproval = true
	tr, res, err := r.submit(env)
	if err != nil {
		t.Fatalf("submit: %v\ntrace: %s", err, tr)
	}
	if res.Decision != "gated" {
		t.Fatalf("decision = %q, want gated\ntrace: %s", res.Decision, tr)
	}
	if len(r.records.stored) != 1 {
		t.Fatalf("stored %d records, want 1", len(r.records.stored))
	}
	ar := r.records.stored[0]
	if ar.Spec.EnvelopeJSON == "" {
		t.Fatal("parked record has no preserved envelope")
	}
	return ar
}

// approve simulates the ChatOps gateway's write: phase PendingApproval -> Pending with a
// satisfied approvals block. Resume trusts this has already happened; it never re-derives it.
func approve(ar *agentv1alpha1.ActionRecord) {
	now := metav1.Now()
	ar.Status.Phase = agentv1alpha1.PhasePending
	ar.Status.Approvals = &agentv1alpha1.ActionApprovals{
		Required: 1,
		Granted:  []agentv1alpha1.ApprovalEntry{{Principal: "slack:U02", At: now}},
	}
}

func TestResumeExecutesAnApprovedAction(t *testing.T) {
	r := newRig(t)
	ar := park(t, r, createEnvelope())
	approve(ar)

	res, err := r.pipeline.Resume(context.Background(), ar)
	if err != nil {
		t.Fatalf("Resume: %v", err)
	}
	if res.Decision != "accepted" {
		t.Fatalf("decision = %q, want accepted: %s", res.Decision, res.Message)
	}
	if r.applier.mutations == 0 {
		t.Error("expected the resumed action to actually mutate through the applier")
	}
	gotPhases := r.records.phases
	if len(gotPhases) < 2 || gotPhases[0] != agentv1alpha1.PhaseExecuting {
		t.Errorf("phases = %v, want to start with Executing", gotPhases)
	}
}

func TestResumePersistsAFreshPreStateAndUndoPlan(t *testing.T) {
	// applyEnvelope, not createEnvelope: a create against an absent object legitimately has no
	// pre-state to capture. An apply against an object that already exists does, which is what
	// this test needs to see freshly (re-)populated.
	r := newRig(t, func(r *rig) {
		r.reader = &fakeReader{obj: liveConfigMap(), absent: false}
	})
	ar := park(t, r, applyEnvelope("info"))
	if len(ar.Spec.PreState) != 0 {
		t.Fatal("a parked record must carry no pre-state (it is stale by the time anyone approves)")
	}
	approve(ar)

	if _, err := r.pipeline.Resume(context.Background(), ar); err != nil {
		t.Fatalf("Resume: %v", err)
	}
	if len(ar.Spec.PreState) == 0 {
		t.Error("expected Resume to persist a freshly-captured pre-state")
	}
	if ar.Spec.Undo == nil {
		t.Error("expected Resume to persist a freshly-generated undo plan")
	}
}

func TestResumeRejectsWhenNotPending(t *testing.T) {
	r := newRig(t)
	ar := park(t, r, createEnvelope()) // still PendingApproval, never approved

	if _, err := r.pipeline.Resume(context.Background(), ar); err == nil {
		t.Fatal("expected Resume to refuse a record that is not Pending")
	}
}

func TestResumeRejectsWithNoPreservedEnvelope(t *testing.T) {
	r := newRig(t)
	ar := park(t, r, createEnvelope())
	approve(ar)
	ar.Spec.EnvelopeJSON = "" // simulate a pre-resumption-support record

	res, err := r.pipeline.Resume(context.Background(), ar)
	if err != nil {
		t.Fatalf("Resume: %v", err)
	}
	if res.Decision != "refused" {
		t.Fatalf("decision = %q, want refused", res.Decision)
	}
	if r.records.phases[len(r.records.phases)-1] != agentv1alpha1.PhaseRejected {
		t.Errorf("final phase = %v, want Rejected", r.records.phases)
	}
}

// V-CHAT-006: a target that changed identity since classification (deleted and recreated, or —
// for a create op — one that now unexpectedly exists) refuses rather than proceeding on a stale
// precondition.
func TestResumeRefusesWhenATargetPreconditionMoved(t *testing.T) {
	r := newRig(t, func(r *rig) {
		r.reader = &fakeReader{obj: liveConfigMap(), absent: false} // the object EXISTS at park time
	})
	ar := park(t, r, applyEnvelope("info"))
	if len(ar.Spec.Targets) != 1 {
		t.Fatalf("targets = %+v, want exactly one", ar.Spec.Targets)
	}
	originalUID := ar.Spec.Targets[0].UID
	if originalUID == "" {
		t.Fatal("test fixture should have captured a non-empty original UID")
	}
	approve(ar)

	// Simulate the object having been deleted and recreated between park and approval: same name,
	// different UID.
	recreated := liveConfigMap().DeepCopy()
	recreated.SetUID("99999999-9999-9999-9999-999999999999")
	r.reader.obj = recreated

	res, err := r.pipeline.Resume(context.Background(), ar)
	if err != nil {
		t.Fatalf("Resume: %v", err)
	}
	if res.Decision != "refused" {
		t.Fatalf("decision = %q, want refused when a target's uid moved", res.Decision)
	}
	if r.applier.mutations != 0 {
		t.Error("a refused resumption must never reach the applier")
	}
	if got := r.records.phases[len(r.records.phases)-1]; got != agentv1alpha1.PhaseRejected {
		t.Errorf("final phase = %v, want Rejected", got)
	}
}

// V-CHAT-006 (the other half): if the class the roster approved is lower than what a fresh
// classification produces, Resume refuses rather than executing under the higher, unapproved
// class. Simulated directly on the stored record — the same envelope re-classifies to the same
// class every time in this rig, so what makes the class "rise" here is that the record's own
// spec.classification.class (read as the class the roster approved) is artificially lower than
// that, exactly as it would be if a live policy change raised the class between park and resume.
func TestResumeRefusesWhenStoredClassIsBelowReclassification(t *testing.T) {
	r := newRig(t)
	ar := park(t, r, createEnvelope())
	if ar.Spec.Classification.Class != agentv1alpha1.RiskGated {
		t.Fatalf("parked class = %s, want gated (RequireApproval forces it)", ar.Spec.Classification.Class)
	}
	approve(ar)
	ar.Spec.Classification.Class = agentv1alpha1.RiskRoutine // "the roster approved a routine action"

	res, err := r.pipeline.Resume(context.Background(), ar)
	if err != nil {
		t.Fatalf("Resume: %v", err)
	}
	if res.Decision != "refused" {
		t.Fatalf("decision = %q, want refused: the fresh classification (gated) exceeds the approved one (routine)", res.Decision)
	}
	if r.applier.mutations != 0 {
		t.Error("a refused resumption must never reach the applier")
	}
	if got := r.records.phases[len(r.records.phases)-1]; got != agentv1alpha1.PhaseRejected {
		t.Errorf("final phase = %v, want Rejected", got)
	}
}
