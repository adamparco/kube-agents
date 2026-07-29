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
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

// C-BR's tests. The property underneath all of them: a rung-5 escalation either happened and the
// record says how, or it did not happen and the record still says "owed". The failure mode that
// matters is the third one -- a record that reads as fulfilled while the agent is still acting --
// because "a request with no fulfilment is a visible defect" is the only reason the audit trail is
// worth anything.
//
// The negative control (09 §6: mandatory for every check marked `¬`) is
// TestBrakeIgnoresRecordsThatOweNothing: with no request, or with one already fulfilled, the agent
// must come out unpaused and nothing may be emitted. Without it every assertion below would still
// pass if the reconciler paused unconditionally.

var brakeNow = time.Date(2026, 7, 27, 12, 0, 0, 0, time.UTC)

const (
	brakeNS       = "team-x"
	brakeAgent    = "developer-team-team-x"
	brakeActionID = "01J0000000000000000000000C"
)

// escalatedRecord is a record whose broker has asked for both a page and a pause and whose fan-out
// has not run.
func escalatedRecord() *agentv1alpha1.ActionRecord {
	return &agentv1alpha1.ActionRecord{
		ObjectMeta: metav1.ObjectMeta{Name: journal.RecordName(brakeActionID), Namespace: brakeNS},
		Spec: agentv1alpha1.ActionRecordSpec{
			ActionID:      brakeActionID,
			AgentIdentity: "developer-team/proj/cluster-a/team-x",
			AgentRef:      agentv1alpha1.AgentObjectRef{Name: brakeAgent, Namespace: brakeNS},
			Intent:        "scale the api deployment to 5",
		},
		Status: agentv1alpha1.ActionRecordStatus{
			Phase: agentv1alpha1.PhaseFailed,
			Escalation: &agentv1alpha1.ActionEscalation{
				PageRequested:  true,
				PauseRequested: true,
				Reason:         "rollback failed after the deployment never converged",
				RequestedAt:    &metav1.Time{Time: brakeNow.Add(-time.Minute)},
			},
		},
	}
}

func brakeAgentCR() *agentv1alpha1.Agent {
	return &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: brakeAgent, Namespace: brakeNS},
		Spec: agentv1alpha1.AgentSpec{
			Tier:  agentv1alpha1.TierDeveloperTeam,
			Scope: &agentv1alpha1.ScopeSpec{ProjectID: "proj", ClusterName: "cluster-a", Namespace: brakeNS},
		},
	}
}

// brakeHarness wires a reconciler over the given objects. The recorder is buffered generously so a
// test never blocks on an un-drained channel, and every fulfilment timestamp is brakeNow.
type brakeHarness struct {
	r      *BrakeReconciler
	c      client.Client
	events *record.FakeRecorder
}

func newBrakeHarness(t *testing.T, objs ...client.Object) *brakeHarness {
	t.Helper()
	return newBrakeHarnessWith(t, interceptor.Funcs{}, objs...)
}

func newBrakeHarnessWith(t *testing.T, funcs interceptor.Funcs, objs ...client.Object) *brakeHarness {
	t.Helper()
	s := undoScheme(t)
	c := fake.NewClientBuilder().
		WithScheme(s).
		WithObjects(objs...).
		WithStatusSubresource(&agentv1alpha1.ActionRecord{}, &agentv1alpha1.Agent{}).
		WithInterceptorFuncs(funcs).
		Build()
	rec := record.NewFakeRecorder(10)
	return &brakeHarness{
		r:      &BrakeReconciler{Client: c, Scheme: s, Recorder: rec, Now: func() time.Time { return brakeNow }},
		c:      c,
		events: rec,
	}
}

func (h *brakeHarness) reconcile(t *testing.T) error {
	t.Helper()
	_, err := h.r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Namespace: brakeNS, Name: journal.RecordName(brakeActionID)},
	})
	return err
}

func (h *brakeHarness) agent(t *testing.T) *agentv1alpha1.Agent {
	t.Helper()
	var a agentv1alpha1.Agent
	if err := h.c.Get(context.Background(), client.ObjectKey{Namespace: brakeNS, Name: brakeAgent}, &a); err != nil {
		t.Fatalf("read back agent: %v", err)
	}
	return &a
}

func (h *brakeHarness) record(t *testing.T) *agentv1alpha1.ActionRecord {
	t.Helper()
	var ar agentv1alpha1.ActionRecord
	if err := h.c.Get(context.Background(), client.ObjectKey{Namespace: brakeNS, Name: journal.RecordName(brakeActionID)}, &ar); err != nil {
		t.Fatalf("read back record: %v", err)
	}
	return &ar
}

// drainEvents returns every Event emitted so far without blocking.
func (h *brakeHarness) drainEvents() []string {
	var out []string
	for {
		select {
		case e := <-h.events.Events:
			out = append(out, e)
		default:
			return out
		}
	}
}

func TestBrakePausesAndPages(t *testing.T) {
	h := newBrakeHarness(t, escalatedRecord(), brakeAgentCR())
	if err := h.reconcile(t); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	paused, _, reason := h.agent(t).Spec.Operations.Brake()
	if !paused {
		t.Fatal("the agent was not paused; a failed rollback left it free to keep acting (05 §1.5)")
	}
	if !strings.Contains(reason, "rollback failed") {
		t.Errorf("pauseReason does not carry the escalation reason, so the human running resume cannot "+
			"see why it stopped; got %q", reason)
	}

	events := h.drainEvents()
	if len(events) != 1 {
		t.Fatalf("expected exactly one page, got %d: %v", len(events), events)
	}
	if !strings.Contains(events[0], EventReasonEscalated) || !strings.Contains(events[0], brakeActionID) {
		t.Errorf("the page does not name the reason or the action, so it cannot be traced back to a "+
			"record; got %q", events[0])
	}

	esc := h.record(t).Status.Escalation
	if esc.PausedAt == nil || !esc.PausedAt.Time.Equal(brakeNow) {
		t.Errorf("pausedAt = %v, want %v", esc.PausedAt, brakeNow)
	}
	if esc.PagedAt == nil || !esc.PagedAt.Time.Equal(brakeNow) {
		t.Errorf("pagedAt = %v, want %v", esc.PagedAt, brakeNow)
	}
	if esc.Failure != "" {
		t.Errorf("a clean fan-out recorded a failure: %q", esc.Failure)
	}
	// The request half must survive the receipt untouched -- admission denies C-BR any change to it,
	// so a mutation here is a write that would be REJECTED in a real cluster, not merely untidy.
	if !esc.PageRequested || !esc.PauseRequested || esc.Reason != "rollback failed after the deployment never converged" {
		t.Errorf("C-BR altered the request half while recording its receipt: %+v", esc)
	}
}

// The negative control. Neither shape owes a fan-out, and both are shapes the controller genuinely
// sees: no escalation at all is every other record in the journal, and an already-fulfilled one is
// every resync after the first.
func TestBrakeIgnoresRecordsThatOweNothing(t *testing.T) {
	for _, tc := range []struct {
		name  string
		mutID func(*agentv1alpha1.ActionRecord)
	}{
		{"no escalation at all", func(ar *agentv1alpha1.ActionRecord) { ar.Status.Escalation = nil }},
		{"neither a page nor a pause requested", func(ar *agentv1alpha1.ActionRecord) {
			ar.Status.Escalation.PageRequested = false
			ar.Status.Escalation.PauseRequested = false
		}},
		{"already paged and paused", func(ar *agentv1alpha1.ActionRecord) {
			ar.Status.Escalation.PagedAt = &metav1.Time{Time: brakeNow.Add(-time.Hour)}
			ar.Status.Escalation.PausedAt = &metav1.Time{Time: brakeNow.Add(-time.Hour)}
		}},
		{"already recorded as failed", func(ar *agentv1alpha1.ActionRecord) {
			ar.Status.Escalation.Failure = "agent team-x/developer-team-team-x not found: nothing to pause"
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ar := escalatedRecord()
			tc.mutID(ar)
			h := newBrakeHarness(t, ar, brakeAgentCR())
			if err := h.reconcile(t); err != nil {
				t.Fatalf("reconcile: %v", err)
			}
			if paused, _, _ := h.agent(t).Spec.Operations.Brake(); paused {
				t.Fatal("an agent was paused over a record that owed no fan-out; a brake that fires on " +
					"its own resync stops the fleet by itself")
			}
			if events := h.drainEvents(); len(events) != 0 {
				t.Fatalf("paged a human for a record that owed nothing: %v", events)
			}
		})
	}
}

func TestBrakePausesWithoutPagingWhenOnlyAPauseIsAsked(t *testing.T) {
	ar := escalatedRecord()
	ar.Status.Escalation.PageRequested = false
	h := newBrakeHarness(t, ar, brakeAgentCR())
	if err := h.reconcile(t); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	if paused, _, _ := h.agent(t).Spec.Operations.Brake(); !paused {
		t.Fatal("the pause was not applied")
	}
	if events := h.drainEvents(); len(events) != 0 {
		t.Fatalf("paged a human who was not asked for: %v", events)
	}
	esc := h.record(t).Status.Escalation
	if esc.PagedAt != nil {
		t.Errorf("pagedAt was stamped without a page being requested: %v", esc.PagedAt)
	}
	if esc.PausedAt == nil {
		t.Error("pausedAt was not stamped, so the pause is unauditable")
	}
}

// An agent a human already paused keeps the human's reason. The postcondition C-BR owes is "this
// agent is stopped", and it holds; overwriting "paused during the migration" with an automated
// string would delete the more informative of the two and mislead whoever runs resume.
func TestBrakeDoesNotOverwriteAHumanPause(t *testing.T) {
	agent := brakeAgentCR()
	agent.Spec.Operations = &agentv1alpha1.OperationsSpec{
		Paused:      ptr.To(true),
		PauseReason: "paused by alice during the cluster migration",
	}
	h := newBrakeHarness(t, escalatedRecord(), agent)
	if err := h.reconcile(t); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	paused, _, reason := h.agent(t).Spec.Operations.Brake()
	if !paused {
		t.Fatal("C-BR un-paused an already-paused agent")
	}
	if reason != "paused by alice during the cluster migration" {
		t.Errorf("C-BR overwrote a human's pauseReason with its own; got %q", reason)
	}
	if esc := h.record(t).Status.Escalation; esc.PausedAt == nil {
		t.Error("pausedAt was not stamped for an agent that was already stopped; the postcondition held, " +
			"so the record must say so or the fan-out looks still-owed forever")
	}
}

// A missing Agent is terminal, not retryable: there is nothing to pause and no object to hang the
// Event on, and retrying forever would produce neither a pause nor a signal.
func TestBrakeRecordsAMissingAgentAsAFailure(t *testing.T) {
	h := newBrakeHarness(t, escalatedRecord())
	if err := h.reconcile(t); err != nil {
		t.Fatalf("a deleted Agent must not be a retryable error: %v", err)
	}

	esc := h.record(t).Status.Escalation
	if !strings.Contains(esc.Failure, "not found") {
		t.Errorf("failure does not say the agent was gone; an escalation that evaporated because the "+
			"Agent CR was deleted mid-incident must not look like one never requested; got %q", esc.Failure)
	}
	if esc.PausedAt != nil || esc.PagedAt != nil {
		t.Errorf("stamped a pause or a page that could not have happened: %+v", esc)
	}
	if events := h.drainEvents(); len(events) != 0 {
		t.Fatalf("emitted an Event with no object to hang it on: %v", events)
	}
}

// A pause that admission or RBAC refuses is terminal too -- and the page still goes out. 05 §1.5
// treats the pause and the page as separate responses, and an agent that could not be stopped is
// MORE worth telling a human about, not less.
func TestBrakePagesEvenWhenThePauseIsRefused(t *testing.T) {
	h := newBrakeHarnessWith(t, interceptor.Funcs{
		Patch: func(ctx context.Context, c client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
			if _, ok := obj.(*agentv1alpha1.Agent); ok {
				return apierrors.NewForbidden(
					schema.GroupResource{Group: agentv1alpha1.GroupVersion.Group, Resource: "agents"},
					brakeAgent, errors.New("no write verb on agents"))
			}
			return c.Patch(ctx, obj, patch, opts...)
		},
	}, escalatedRecord(), brakeAgentCR())

	if err := h.reconcile(t); err != nil {
		t.Fatalf("a refused pause must be recorded, not retried: %v", err)
	}

	events := h.drainEvents()
	if len(events) != 1 {
		t.Fatalf("expected the page to go out anyway, got %d events: %v", len(events), events)
	}
	if !strings.Contains(events[0], "COULD NOT BE STOPPED") {
		t.Errorf("the page does not say the agent is still running, which is the part a human needs "+
			"first; got %q", events[0])
	}

	esc := h.record(t).Status.Escalation
	if esc.PausedAt != nil {
		t.Errorf("stamped pausedAt for a pause that was refused: %v", esc.PausedAt)
	}
	if esc.PagedAt == nil {
		t.Error("pagedAt was not stamped for a page that was emitted")
	}
	if !strings.Contains(esc.Failure, "pause refused") {
		t.Errorf("failure does not name the refused pause; got %q", esc.Failure)
	}
}

// The other half of the retryable/terminal rule, and the one with teeth. A conflict is a lost
// optimistic-concurrency race, not a verdict: recording it as `failure` would mark the fan-out done
// and freeze the agent in the UN-paused state over one retry.
func TestBrakeRetriesAConflictInsteadOfRecordingIt(t *testing.T) {
	h := newBrakeHarnessWith(t, interceptor.Funcs{
		Patch: func(ctx context.Context, c client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
			if _, ok := obj.(*agentv1alpha1.Agent); ok {
				return apierrors.NewConflict(
					schema.GroupResource{Group: agentv1alpha1.GroupVersion.Group, Resource: "agents"},
					brakeAgent, errors.New("the object has been modified"))
			}
			return c.Patch(ctx, obj, patch, opts...)
		},
	}, escalatedRecord(), brakeAgentCR())

	if err := h.reconcile(t); err == nil {
		t.Fatal("a conflict was swallowed; controller-runtime will not re-queue and the agent stays " +
			"un-paused with a record that says the fan-out is done")
	}

	if esc := h.record(t).Status.Escalation; !fanoutPending(esc) {
		t.Errorf("a retryable failure was written into the receipt: %+v", esc)
	}
	if events := h.drainEvents(); len(events) != 0 {
		t.Fatalf("paged before the pause was known to have failed terminally: %v", events)
	}
}

// Both fulfilment fields are bounded by the CRD. An unbounded copy from the escalation's reason into
// a 512-rune field is how a pause fails validation and does not happen -- and slicing bytes rather
// than runes produces invalid UTF-8 that the API server rejects for a different reason.
func TestBrakeTruncatesOnRuneBoundaries(t *testing.T) {
	ar := escalatedRecord()
	ar.Status.Escalation.Reason = strings.Repeat("é", maxPauseReason+40)
	h := newBrakeHarness(t, ar, brakeAgentCR())
	if err := h.reconcile(t); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	_, _, reason := h.agent(t).Spec.Operations.Brake()
	if n := len([]rune(reason)); n != maxPauseReason {
		t.Errorf("pauseReason is %d runes, want %d", n, maxPauseReason)
	}
	if !utf8Valid(reason) {
		t.Error("pauseReason was cut mid-rune; the API server will reject it as invalid UTF-8")
	}
}

func utf8Valid(s string) bool {
	for _, r := range s {
		if r == '�' {
			return false
		}
	}
	return true
}

// SetupWithManager refuses a nil Recorder rather than defaulting one. A C-BR that silently cannot
// page is a rung-5 escalation that half-happens and says nothing about it -- the failure mode is
// invisible precisely when it matters.
func TestBrakeSetupRefusesANilRecorder(t *testing.T) {
	r := &BrakeReconciler{Client: fake.NewClientBuilder().WithScheme(undoScheme(t)).Build()}
	if err := r.SetupWithManager(nil); err == nil {
		t.Fatal("SetupWithManager accepted a nil Recorder")
	} else if !strings.Contains(err.Error(), "Recorder") {
		t.Errorf("the error does not name the missing recorder: %v", err)
	}
}
