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

package notify_test

import (
	"context"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval/notify"
)

func notifierScheme(t *testing.T) *runtime.Scheme {
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

type recordingDeliverer struct {
	delivered  []approval.Message
	updated    []approval.Message
	deliverErr error
}

func (d *recordingDeliverer) Deliver(_ context.Context, _ notify.Target, m approval.Message) (string, error) {
	if d.deliverErr != nil {
		return "", d.deliverErr
	}
	d.delivered = append(d.delivered, m)
	return "ref-1", nil
}

func (d *recordingDeliverer) Update(_ context.Context, _ notify.Target, _ string, m approval.Message) error {
	d.updated = append(d.updated, m)
	return nil
}

type memStore struct {
	states map[string]notify.DeliveryState
}

func newMemStore() *memStore { return &memStore{states: map[string]notify.DeliveryState{}} }

func (m *memStore) Get(_ context.Context, name string) (notify.DeliveryState, bool, error) {
	s, ok := m.states[name]
	return s, ok, nil
}

func (m *memStore) Save(_ context.Context, name string, s notify.DeliveryState) error {
	m.states[name] = s
	return nil
}

func gatedRecord(name string) *agentv1alpha1.ActionRecord {
	return &agentv1alpha1.ActionRecord{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "default", UID: types.UID("uid-" + name)},
		Spec: agentv1alpha1.ActionRecordSpec{
			ActionID:            "01ARZ3NDEKTSV4RRFFQ69G5FAV",
			AgentRef:            agentv1alpha1.AgentObjectRef{Name: "my-agent", Namespace: "default"},
			AgentIdentity:       "platform/proj",
			ActorServiceAccount: "my-agent-actor",
			Requester:           agentv1alpha1.ActionRequester{Kind: "human", ID: "slack:U01"},
			Intent:              "scale it",
			IdempotencyKey:      "sha256:0000000000000000000000000000000000000000000000000000000000000",
			Classification:      agentv1alpha1.ActionClassification{Class: agentv1alpha1.RiskGated},
			Targets: []agentv1alpha1.TargetRef{
				{Version: "v1", Kind: "Deployment", Namespace: "default", Name: "web"},
			},
		},
		Status: agentv1alpha1.ActionRecordStatus{Phase: agentv1alpha1.PhasePendingApproval},
	}
}

func TestNotifierDeliversOnFirstReconcile(t *testing.T) {
	agent := &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: "my-agent", Namespace: "default"},
		Spec: agentv1alpha1.AgentSpec{
			Operations: &agentv1alpha1.OperationsSpec{ApprovalRosterRef: &agentv1alpha1.RosterRef{Name: "roster-1"}},
		},
	}
	roster := &agentv1alpha1.ApprovalRoster{
		ObjectMeta: metav1.ObjectMeta{Name: "roster-1", Namespace: "default"},
		Spec: agentv1alpha1.ApprovalRosterSpec{
			Approvers: []agentv1alpha1.Approver{{Platform: "slack", ID: "U02"}},
			Notify:    &agentv1alpha1.ApprovalNotify{Slack: &agentv1alpha1.SlackNotify{Channel: "C01"}},
		},
	}
	ar := gatedRecord("ar-1")
	c := fake.NewClientBuilder().WithScheme(notifierScheme(t)).WithObjects(agent, roster, ar).Build()

	del := &recordingDeliverer{}
	store := newMemStore()
	r := &notify.Reconciler{Client: c, Deliverers: notify.Deliverers{notify.PlatformSlack: del}, Store: store}

	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: client.ObjectKeyFromObject(ar)}); err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	if len(del.delivered) != 1 {
		t.Fatalf("delivered %d messages, want 1", len(del.delivered))
	}
	if del.delivered[0].Intent != "scale it" {
		t.Errorf("delivered message intent = %q", del.delivered[0].Intent)
	}
}

func TestNotifierDoesNotRedeliverUnchangedContent(t *testing.T) {
	agent := &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: "my-agent", Namespace: "default"},
		Spec: agentv1alpha1.AgentSpec{
			Operations: &agentv1alpha1.OperationsSpec{ApprovalRosterRef: &agentv1alpha1.RosterRef{Name: "roster-1"}},
		},
	}
	roster := &agentv1alpha1.ApprovalRoster{
		ObjectMeta: metav1.ObjectMeta{Name: "roster-1", Namespace: "default"},
		Spec: agentv1alpha1.ApprovalRosterSpec{
			Approvers: []agentv1alpha1.Approver{{Platform: "slack", ID: "U02"}},
			Notify:    &agentv1alpha1.ApprovalNotify{Slack: &agentv1alpha1.SlackNotify{Channel: "C01"}},
		},
	}
	ar := gatedRecord("ar-1")
	c := fake.NewClientBuilder().WithScheme(notifierScheme(t)).WithObjects(agent, roster, ar).Build()

	del := &recordingDeliverer{}
	store := newMemStore()
	r := &notify.Reconciler{Client: c, Deliverers: notify.Deliverers{notify.PlatformSlack: del}, Store: store}
	req := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(ar)}

	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatalf("first reconcile: %v", err)
	}
	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatalf("second reconcile: %v", err)
	}
	if len(del.delivered) != 1 {
		t.Errorf("delivered %d times for an unchanged record, want exactly 1 (flapping-watch dedup)", len(del.delivered))
	}
}

func TestNotifierEditsOnResolution(t *testing.T) {
	agent := &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: "my-agent", Namespace: "default"},
		Spec: agentv1alpha1.AgentSpec{
			Operations: &agentv1alpha1.OperationsSpec{ApprovalRosterRef: &agentv1alpha1.RosterRef{Name: "roster-1"}},
		},
	}
	roster := &agentv1alpha1.ApprovalRoster{
		ObjectMeta: metav1.ObjectMeta{Name: "roster-1", Namespace: "default"},
		Spec: agentv1alpha1.ApprovalRosterSpec{
			Approvers: []agentv1alpha1.Approver{{Platform: "slack", ID: "U02"}},
			Notify:    &agentv1alpha1.ApprovalNotify{Slack: &agentv1alpha1.SlackNotify{Channel: "C01"}},
		},
	}
	ar := gatedRecord("ar-1")
	c := fake.NewClientBuilder().
		WithScheme(notifierScheme(t)).
		WithObjects(agent, roster, ar).
		WithStatusSubresource(&agentv1alpha1.ActionRecord{}).
		Build()

	del := &recordingDeliverer{}
	store := newMemStore()
	r := &notify.Reconciler{Client: c, Deliverers: notify.Deliverers{notify.PlatformSlack: del}, Store: store}
	req := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(ar)}

	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatalf("initial reconcile: %v", err)
	}

	live := &agentv1alpha1.ActionRecord{}
	if err := c.Get(context.Background(), req.NamespacedName, live); err != nil {
		t.Fatalf("get: %v", err)
	}
	live.Status.Phase = agentv1alpha1.PhaseRejected
	if err := c.Status().Update(context.Background(), live); err != nil {
		t.Fatalf("update: %v", err)
	}

	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatalf("resolution reconcile: %v", err)
	}
	if len(del.delivered) != 1 {
		t.Errorf("delivered = %d, want exactly 1 (the resolution is an EDIT)", len(del.delivered))
	}
	if len(del.updated) != 1 {
		t.Fatalf("updated = %d, want 1", len(del.updated))
	}
	if del.updated[0].Resolution != "rejected" {
		t.Errorf("updated message resolution = %q, want rejected", del.updated[0].Resolution)
	}
}

// V-CHAT-007 (notifier half): a record whose roster cannot be resolved sends nothing.
func TestNotifierSendsNothingWhenRosterUnusable(t *testing.T) {
	ar := gatedRecord("ar-1") // no Agent CR at all in the fake client
	c := fake.NewClientBuilder().WithScheme(notifierScheme(t)).WithObjects(ar).Build()

	del := &recordingDeliverer{}
	r := &notify.Reconciler{Client: c, Deliverers: notify.Deliverers{notify.PlatformSlack: del}, Store: newMemStore()}

	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: client.ObjectKeyFromObject(ar)}); err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	if len(del.delivered) != 0 {
		t.Errorf("delivered %d messages for a roster-unusable record, want 0", len(del.delivered))
	}
}

func TestNotifierSendsNothingWithNoNotifyDestination(t *testing.T) {
	agent := &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: "my-agent", Namespace: "default"},
		Spec: agentv1alpha1.AgentSpec{
			Operations: &agentv1alpha1.OperationsSpec{ApprovalRosterRef: &agentv1alpha1.RosterRef{Name: "roster-1"}},
		},
	}
	roster := &agentv1alpha1.ApprovalRoster{
		ObjectMeta: metav1.ObjectMeta{Name: "roster-1", Namespace: "default"},
		Spec: agentv1alpha1.ApprovalRosterSpec{
			Approvers: []agentv1alpha1.Approver{{Platform: "slack", ID: "U02"}},
			// Notify deliberately nil.
		},
	}
	ar := gatedRecord("ar-1")
	c := fake.NewClientBuilder().WithScheme(notifierScheme(t)).WithObjects(agent, roster, ar).Build()

	del := &recordingDeliverer{}
	r := &notify.Reconciler{Client: c, Deliverers: notify.Deliverers{notify.PlatformSlack: del}, Store: newMemStore()}

	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: client.ObjectKeyFromObject(ar)}); err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	if len(del.delivered) != 0 {
		t.Errorf("delivered %d messages with no notify destination, want 0", len(del.delivered))
	}
}

func TestNotifierIgnoresIrrelevantPhases(t *testing.T) {
	ar := gatedRecord("ar-1")
	ar.Status.Phase = agentv1alpha1.PhaseExecuting // not one of the phases the notifier cares about
	c := fake.NewClientBuilder().WithScheme(notifierScheme(t)).WithObjects(ar).Build()

	del := &recordingDeliverer{}
	r := &notify.Reconciler{Client: c, Deliverers: notify.Deliverers{notify.PlatformSlack: del}, Store: newMemStore()}

	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: client.ObjectKeyFromObject(ar)}); err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	if len(del.delivered) != 0 {
		t.Errorf("delivered %d messages for an Executing record, want 0", len(del.delivered))
	}
}
