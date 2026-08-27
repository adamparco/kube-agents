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

package main

import (
	"context"
	"errors"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

type recordingReconciler struct {
	reconciled []string
}

func (r *recordingReconciler) Reconcile(_ context.Context, req ctrl.Request) (ctrl.Result, error) {
	r.reconciled = append(r.reconciled, req.Name)
	return ctrl.Result{}, nil
}

type noopLog struct{}

func (noopLog) Error(error, string, ...any) {}

func resumeTestScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	s := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(s); err != nil {
		t.Fatalf("add clientgo scheme: %v", err)
	}
	if err := agentv1alpha1.AddToScheme(s); err != nil {
		t.Fatalf("add kube-agents scheme: %v", err)
	}
	return s
}

// recordWithPhase sets the status label alongside status.phase, the way journal.Store always keeps
// them (internal/journal/store.go's StatusLabel) -- resumeSweep now filters its List by that label
// server-side, so a fixture missing it would never be fetched regardless of what status.phase says.
func recordWithPhase(name string, phase agentv1alpha1.ActionPhase) *agentv1alpha1.ActionRecord {
	return &agentv1alpha1.ActionRecord{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: "kubeagents-system",
			Labels:    map[string]string{journal.StatusLabel: string(phase)},
		},
		Status: agentv1alpha1.ActionRecordStatus{Phase: phase},
	}
}

func TestResumeSweepReconcilesOnlyRelevantPhases(t *testing.T) {
	objs := []client.Object{
		recordWithPhase("ar-pending-approval", agentv1alpha1.PhasePendingApproval),
		recordWithPhase("ar-pending", agentv1alpha1.PhasePending),
		recordWithPhase("ar-verified", agentv1alpha1.PhaseVerified),
		recordWithPhase("ar-rejected", agentv1alpha1.PhaseRejected),
		recordWithPhase("ar-expired", agentv1alpha1.PhaseExpired),
	}
	c := fake.NewClientBuilder().WithScheme(resumeTestScheme(t)).WithObjects(objs...).Build()
	rc := &recordingReconciler{}

	resumeSweep(context.Background(), c, rc, "kubeagents-system", noopLog{})

	if len(rc.reconciled) != 2 {
		t.Fatalf("reconciled %v, want exactly the two live phases", rc.reconciled)
	}
	want := map[string]bool{"ar-pending-approval": true, "ar-pending": true}
	for _, name := range rc.reconciled {
		if !want[name] {
			t.Errorf("reconciled %q unexpectedly", name)
		}
	}
}

func TestResumeSweepIgnoresOtherNamespaces(t *testing.T) {
	other := recordWithPhase("ar-elsewhere", agentv1alpha1.PhasePendingApproval)
	other.Namespace = "some-other-ns"
	c := fake.NewClientBuilder().WithScheme(resumeTestScheme(t)).WithObjects(other).Build()
	rc := &recordingReconciler{}

	resumeSweep(context.Background(), c, rc, "kubeagents-system", noopLog{})

	if len(rc.reconciled) != 0 {
		t.Errorf("reconciled %v, want none outside the broker's own namespace", rc.reconciled)
	}
}

func TestResumeSweepContinuesAfterAReconcileError(t *testing.T) {
	objs := []client.Object{
		recordWithPhase("ar-1", agentv1alpha1.PhasePending),
		recordWithPhase("ar-2", agentv1alpha1.PhasePending),
	}
	c := fake.NewClientBuilder().WithScheme(resumeTestScheme(t)).WithObjects(objs...).Build()
	rc := &erroringReconciler{failOn: "ar-1"}

	resumeSweep(context.Background(), c, rc, "kubeagents-system", noopLog{})

	if len(rc.attempted) != 2 {
		t.Errorf("attempted %v, want both records reconciled despite the first erroring", rc.attempted)
	}
}

// The old resumeSweep fetched everything in the namespace and filtered client-side on status.phase;
// this record would have been reconciled under that implementation, since its status.phase is
// Pending. The fix (V-EFF-001) filters the List itself by journal.StatusLabel, and this label says
// Verified -- a stale index the store's own docs allow (internal/journal/store.go:289's "best-effort
// ordering, never best-effort truth") until JournalReconciler repairs it. This test only passes
// against a List that is actually driven by the label, not a re-read of status.phase.
func TestResumeSweepDoesNotFetchARecordWhoseLabelHasNotCaughtUp(t *testing.T) {
	stale := recordWithPhase("ar-stale-label", agentv1alpha1.PhasePending)
	stale.Labels[journal.StatusLabel] = string(agentv1alpha1.PhaseVerified)
	c := fake.NewClientBuilder().WithScheme(resumeTestScheme(t)).WithObjects(stale).Build()
	rc := &recordingReconciler{}

	resumeSweep(context.Background(), c, rc, "kubeagents-system", noopLog{})

	if len(rc.reconciled) != 0 {
		t.Errorf("reconciled %v, want none: the List must be filtered by the status label, not by re-checking status.phase after an unfiltered fetch", rc.reconciled)
	}
}

type erroringReconciler struct {
	failOn    string
	attempted []string
}

func (r *erroringReconciler) Reconcile(_ context.Context, req ctrl.Request) (ctrl.Result, error) {
	r.attempted = append(r.attempted, req.Name)
	if req.Name == r.failOn {
		return ctrl.Result{}, errors.New("boom")
	}
	return ctrl.Result{}, nil
}
