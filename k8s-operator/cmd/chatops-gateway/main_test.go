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
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval/gateway"
)

func TestBuildHTTPMuxServesV1Alpha1Commands(t *testing.T) {
	ns := "kage-live-test"
	minApprovals := int32(1)
	agent := &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: ns},
		Spec: agentv1alpha1.AgentSpec{
			Tier:       agentv1alpha1.TierPlatform,
			Operations: &agentv1alpha1.OperationsSpec{ApprovalRosterRef: &agentv1alpha1.RosterRef{Name: "roster-1"}},
		},
	}
	roster := &agentv1alpha1.ApprovalRoster{
		ObjectMeta: metav1.ObjectMeta{Name: "roster-1", Namespace: ns},
		Spec: agentv1alpha1.ApprovalRosterSpec{
			Approvers:    []agentv1alpha1.Approver{{Platform: "discord", ID: "1001"}},
			MinApprovals: &minApprovals,
		},
	}
	ar := &agentv1alpha1.ActionRecord{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "ar-01arz3ndektsv4rrffq69g5fav",
			Namespace: ns,
		},
		Spec: agentv1alpha1.ActionRecordSpec{
			ActionID:            "01ARZ3NDEKTSV4RRFFQ69G5FAV",
			AgentRef:            agentv1alpha1.AgentObjectRef{Name: "test-agent", Namespace: ns},
			AgentIdentity:       "platform/proj",
			ActorServiceAccount: "test-agent-actor",
			Requester:           agentv1alpha1.ActionRequester{Kind: "human", ID: "discord:1002", Platform: "discord"},
			Intent:              "scale deployment",
			IdempotencyKey:      "sha256:0000000000000000000000000000000000000000000000000000000000000",
			Classification:      agentv1alpha1.ActionClassification{Class: agentv1alpha1.RiskGated},
		},
		Status: agentv1alpha1.ActionRecordStatus{
			Phase: agentv1alpha1.PhasePendingApproval,
		},
	}

	c := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, roster, ar).
		WithStatusSubresource(&agentv1alpha1.ActionRecord{}, &agentv1alpha1.Agent{}, &agentv1alpha1.ApprovalRoster{}).
		Build()

	dispatcher := &gateway.Dispatcher{
		Client: c,
		Now:    func() time.Time { return time.Date(2026, 10, 9, 15, 0, 0, 0, time.UTC) },
	}
	srv := httptest.NewServer(buildHTTPMux(dispatcher, nil, nil))
	defer srv.Close()

	payload, _ := json.Marshal(gateway.CommandRequest{
		Platform:  "discord",
		SenderID:  "1001",
		EventID:   "discord:m-1",
		ChannelID: "discord:g1/c1",
		ThreadID:  "discord:g1/c1",
		Text:      "/approve ar-01arz3ndektsv4rrffq69g5fav",
		Namespace: ns,
	})
	resp, err := http.Post(srv.URL+"/v1alpha1/commands", "application/json", bytes.NewReader(payload))
	if err != nil {
		t.Fatalf("POST /v1alpha1/commands: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}

	var out gateway.CommandResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decode CommandResponse: %v", err)
	}
	if !out.OK || !strings.Contains(out.Reply, "approved") {
		t.Fatalf("unexpected CommandResponse: %+v", out)
	}

	updated := &agentv1alpha1.ActionRecord{}
	if err := c.Get(context.Background(), client.ObjectKeyFromObject(ar), updated); err != nil {
		t.Fatalf("get ActionRecord: %v", err)
	}
	if updated.Status.Phase != agentv1alpha1.PhasePending {
		t.Fatalf("phase = %q, want Pending", updated.Status.Phase)
	}
	if updated.Status.Approvals == nil || len(updated.Status.Approvals.Granted) != 1 || updated.Status.Approvals.Granted[0].Principal != "discord:1001" {
		t.Fatalf("approvals = %+v, want 1 granted by discord:1001", updated.Status.Approvals)
	}
}
