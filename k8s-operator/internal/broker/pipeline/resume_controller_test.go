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
	"time"

	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

func resumeControllerScheme(t *testing.T) *runtime.Scheme {
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

func TestResumeControllerResumesAnApprovedRecord(t *testing.T) {
	r := newRig(t, withUsableRoster)
	ar := park(t, r, createEnvelope())
	approve(ar)

	c := fake.NewClientBuilder().
		WithScheme(resumeControllerScheme(t)).
		WithObjects(ar).
		WithStatusSubresource(&agentv1alpha1.ActionRecord{}).
		Build()

	rc := &ResumeController{Client: c, Pipeline: r.pipeline, Records: r.records, Now: func() time.Time { return testClock }}
	if _, err := rc.Reconcile(context.Background(), ctrl.Request{NamespacedName: client.ObjectKeyFromObject(ar)}); err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	if r.applier.mutations == 0 {
		t.Error("expected the resume controller to have driven the action through to execution")
	}
}

func TestResumeControllerIgnoresPendingWithNoApprovals(t *testing.T) {
	r := newRig(t)
	ar := park(t, r, createEnvelope())
	ar.Status.Phase = agentv1alpha1.PhasePending
	ar.Status.Approvals = nil // never touched by the gateway

	c := fake.NewClientBuilder().WithScheme(resumeControllerScheme(t)).WithObjects(ar).WithStatusSubresource(&agentv1alpha1.ActionRecord{}).Build()
	rc := &ResumeController{Client: c, Pipeline: r.pipeline, Records: r.records}

	if _, err := rc.Reconcile(context.Background(), ctrl.Request{NamespacedName: client.ObjectKeyFromObject(ar)}); err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	if r.applier.mutations != 0 {
		t.Error("a Pending record with no approvals block must never be resumed")
	}
}

func TestResumeControllerExpiresPastDeadline(t *testing.T) {
	r := newRig(t)
	ar := park(t, r, createEnvelope())
	ar.CreationTimestamp.Time = testClock.Add(-100 * time.Hour) // long past any TTL

	c := fake.NewClientBuilder().WithScheme(resumeControllerScheme(t)).WithObjects(ar).WithStatusSubresource(&agentv1alpha1.ActionRecord{}).Build()
	rc := &ResumeController{Client: c, Records: r.records, Now: func() time.Time { return testClock }}

	if _, err := rc.Reconcile(context.Background(), ctrl.Request{NamespacedName: client.ObjectKeyFromObject(ar)}); err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	found := false
	for _, p := range r.records.phases {
		if p == agentv1alpha1.PhaseExpired {
			found = true
		}
	}
	if !found {
		t.Errorf("phases = %v, want Expired for a record long past its window", r.records.phases)
	}
}

func TestResumeControllerRequeuesBeforeDeadline(t *testing.T) {
	r := newRig(t)
	ar := park(t, r, createEnvelope())
	ar.CreationTimestamp.Time = testClock // just parked

	c := fake.NewClientBuilder().WithScheme(resumeControllerScheme(t)).WithObjects(ar).WithStatusSubresource(&agentv1alpha1.ActionRecord{}).Build()
	rc := &ResumeController{Client: c, Records: r.records, Now: func() time.Time { return testClock }}

	res, err := rc.Reconcile(context.Background(), ctrl.Request{NamespacedName: client.ObjectKeyFromObject(ar)})
	if err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	if res.RequeueAfter <= 0 {
		t.Error("expected a positive RequeueAfter before the deadline arrives")
	}
	for _, p := range r.records.phases {
		if p == agentv1alpha1.PhaseExpired {
			t.Error("must not expire a record before its deadline")
		}
	}
}

func TestResumeControllerIgnoresMissingRecord(t *testing.T) {
	c := fake.NewClientBuilder().WithScheme(resumeControllerScheme(t)).Build()
	rc := &ResumeController{Client: c}
	if _, err := rc.Reconcile(context.Background(), ctrl.Request{}); err != nil {
		t.Fatalf("Reconcile on a missing record should not error: %v", err)
	}
}
