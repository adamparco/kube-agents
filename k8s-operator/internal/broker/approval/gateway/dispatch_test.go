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

package gateway_test

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval/gateway"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
)

func TestDispatchApproveEndToEnd(t *testing.T) {
	agent := testAgent("my-agent", testNS, &agentv1alpha1.RosterRef{Name: "roster-1"})
	roster := testRoster("roster-1", testNS, 1, false, agentv1alpha1.Approver{Platform: "slack", ID: "U02"})
	ar := testRecord("ar-1", testNS, "slack:U01")
	c := fakeClientWith(t, agent, roster, ar)

	d := &gateway.Dispatcher{Client: c, Now: func() time.Time { return fixedNow }}
	reply := d.Handle(context.Background(), "event-1", "slack:U02", "approve "+ar.Name)

	if !strings.Contains(reply, "approved") {
		t.Fatalf("reply = %q, want an approval confirmation", reply)
	}

	live := &agentv1alpha1.ActionRecord{}
	if err := c.Get(context.Background(), client.ObjectKeyFromObject(ar), live); err != nil {
		t.Fatalf("get: %v", err)
	}
	if live.Status.Phase != agentv1alpha1.PhasePending {
		t.Errorf("phase = %q, want Pending (minApprovals=1 satisfied by one approval)", live.Status.Phase)
	}
	if live.Status.Approvals == nil || len(live.Status.Approvals.Granted) != 1 || live.Status.Approvals.Granted[0].Principal != "slack:U02" {
		t.Errorf("approvals = %+v", live.Status.Approvals)
	}
}

func TestDispatchRejectEndToEnd(t *testing.T) {
	agent := testAgent("my-agent", testNS, &agentv1alpha1.RosterRef{Name: "roster-1"})
	roster := testRoster("roster-1", testNS, 1, false, agentv1alpha1.Approver{Platform: "slack", ID: "U02"})
	ar := testRecord("ar-1", testNS, "slack:U01")
	c := fakeClientWith(t, agent, roster, ar)

	d := &gateway.Dispatcher{Client: c, Now: func() time.Time { return fixedNow }}
	reply := d.Handle(context.Background(), "event-1", "slack:U02", "reject "+ar.Name+" too risky")

	if !strings.Contains(reply, "rejected") {
		t.Fatalf("reply = %q, want a rejection confirmation", reply)
	}

	live := &agentv1alpha1.ActionRecord{}
	if err := c.Get(context.Background(), client.ObjectKeyFromObject(ar), live); err != nil {
		t.Fatalf("get: %v", err)
	}
	if live.Status.Phase != agentv1alpha1.PhaseRejected {
		t.Errorf("phase = %q, want Rejected", live.Status.Phase)
	}
	if len(live.Status.Approvals.Rejected) != 1 || live.Status.Approvals.Rejected[0].Comment != "too risky" {
		t.Errorf("rejected = %+v", live.Status.Approvals.Rejected)
	}
}

// V-CHAT-001: a principal not on the roster is refused, and the ActionRecord is untouched.
func TestDispatchRefusesNonRosterApprover(t *testing.T) {
	agent := testAgent("my-agent", testNS, &agentv1alpha1.RosterRef{Name: "roster-1"})
	roster := testRoster("roster-1", testNS, 1, false, agentv1alpha1.Approver{Platform: "slack", ID: "U02"})
	ar := testRecord("ar-1", testNS, "slack:U01")
	c := fakeClientWith(t, agent, roster, ar)

	d := &gateway.Dispatcher{Client: c, Now: func() time.Time { return fixedNow }}
	reply := d.Handle(context.Background(), "event-1", "slack:U99", "approve "+ar.Name)

	if !strings.Contains(reply, "not on the approval roster") {
		t.Fatalf("reply = %q, want a not-on-roster refusal", reply)
	}
	live := &agentv1alpha1.ActionRecord{}
	if err := c.Get(context.Background(), client.ObjectKeyFromObject(ar), live); err != nil {
		t.Fatalf("get: %v", err)
	}
	if live.Status.Approvals != nil {
		t.Errorf("approvals = %+v, want untouched after a refused command", live.Status.Approvals)
	}
}

// V-CHAT-002: self-approval is refused.
func TestDispatchRefusesSelfApproval(t *testing.T) {
	agent := testAgent("my-agent", testNS, &agentv1alpha1.RosterRef{Name: "roster-1"})
	roster := testRoster("roster-1", testNS, 1, false, agentv1alpha1.Approver{Platform: "slack", ID: "U01"})
	ar := testRecord("ar-1", testNS, "slack:U01") // requester == the only approver
	c := fakeClientWith(t, agent, roster, ar)

	d := &gateway.Dispatcher{Client: c, Now: func() time.Time { return fixedNow }}
	reply := d.Handle(context.Background(), "event-1", "slack:U01", "approve "+ar.Name)

	if !strings.Contains(reply, "requested this action") {
		t.Fatalf("reply = %q, want a self-approval refusal", reply)
	}
}

// V-CHAT-007: an unresolvable roster refuses cleanly instead of erroring or approving.
func TestDispatchRefusesWhenRosterUnusable(t *testing.T) {
	ar := testRecord("ar-1", testNS, "slack:U01")
	c := fakeClientWith(t, ar) // no Agent CR

	d := &gateway.Dispatcher{Client: c, Now: func() time.Time { return fixedNow }}
	reply := d.Handle(context.Background(), "event-1", "slack:U02", "approve "+ar.Name)

	if !strings.Contains(reply, "no usable approval roster") {
		t.Fatalf("reply = %q, want a roster-unusable refusal", reply)
	}
}

func TestDispatchRefusesUnparseableCommand(t *testing.T) {
	d := &gateway.Dispatcher{Client: fakeClientNoObjects(), Now: func() time.Time { return fixedNow }}
	reply := d.Handle(context.Background(), "event-1", "slack:U02", "not a command")
	if !strings.Contains(reply, "unrecognized verb") {
		t.Fatalf("reply = %q", reply)
	}
}

func TestDispatchRefusesTooShortCommand(t *testing.T) {
	d := &gateway.Dispatcher{Client: fakeClientNoObjects(), Now: func() time.Time { return fixedNow }}
	reply := d.Handle(context.Background(), "event-1", "slack:U02", "approve")
	if !strings.Contains(reply, "not a recognized command") {
		t.Fatalf("reply = %q", reply)
	}
}

func TestDispatchRefusesUnknownAction(t *testing.T) {
	d := &gateway.Dispatcher{Client: fakeClientNoObjects(), Now: func() time.Time { return fixedNow }}
	reply := d.Handle(context.Background(), "event-1", "slack:U02", "approve ar-does-not-exist")
	if !strings.Contains(reply, "no such action") {
		t.Fatalf("reply = %q", reply)
	}
}

func TestDispatchSurfacesALookupListError(t *testing.T) {
	c := fake.NewClientBuilder().
		WithScheme(gatewayScheme(t)).
		WithInterceptorFuncs(interceptor.Funcs{
			List: func(context.Context, client.WithWatch, client.ObjectList, ...client.ListOption) error {
				return apierrors.NewInternalError(errors.New("etcd is unavailable"))
			},
		}).
		Build()
	d := &gateway.Dispatcher{Client: c, Now: func() time.Time { return fixedNow }}

	reply := d.Handle(context.Background(), "event-1", "slack:U02", "approve ar-1")
	if !strings.Contains(reply, "listing action records") {
		t.Fatalf("reply = %q, want it to name the lookup failure rather than read as \"no such action\"", reply)
	}
}

// Replayed platform events must not be authorized twice.
func TestDispatchDeduplicatesReplayedEvents(t *testing.T) {
	agent := testAgent("my-agent", testNS, &agentv1alpha1.RosterRef{Name: "roster-1"})
	roster := testRoster("roster-1", testNS, 2, false,
		agentv1alpha1.Approver{Platform: "slack", ID: "U02"},
		agentv1alpha1.Approver{Platform: "slack", ID: "U03"})
	ar := testRecord("ar-1", testNS, "slack:U01")
	c := fakeClientWith(t, agent, roster, ar)

	d := &gateway.Dispatcher{Client: c, Now: func() time.Time { return fixedNow }}
	first := d.Handle(context.Background(), "same-event", "slack:U02", "approve "+ar.Name)
	second := d.Handle(context.Background(), "same-event", "slack:U02", "approve "+ar.Name)

	if first == "" {
		t.Fatal("expected the first delivery to produce a reply")
	}
	if second != "" {
		t.Errorf("second delivery of the same event key produced %q, want empty (deduplicated)", second)
	}

	live := &agentv1alpha1.ActionRecord{}
	if err := c.Get(context.Background(), client.ObjectKeyFromObject(ar), live); err != nil {
		t.Fatalf("get: %v", err)
	}
	if len(live.Status.Approvals.Granted) != 1 {
		t.Errorf("granted count = %d, want 1 (the replay must not have run authorization again)", len(live.Status.Approvals.Granted))
	}
}
