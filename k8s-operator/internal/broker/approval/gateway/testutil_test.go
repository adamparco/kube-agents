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
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

const testNS = "default"

var fixedNow = time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)

func gatewayScheme(t *testing.T) *runtime.Scheme {
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

func fakeClientNoObjects() client.Client {
	return fake.NewClientBuilder().WithScheme(mustScheme()).Build()
}

func mustScheme() *runtime.Scheme {
	s := runtime.NewScheme()
	_ = clientgoscheme.AddToScheme(s)
	_ = agentv1alpha1.AddToScheme(s)
	return s
}

func fakeClientWith(t *testing.T, objs ...client.Object) client.Client {
	t.Helper()
	return fake.NewClientBuilder().
		WithScheme(gatewayScheme(t)).
		WithObjects(objs...).
		WithStatusSubresource(&agentv1alpha1.ActionRecord{}, &agentv1alpha1.Agent{}, &agentv1alpha1.ApprovalRoster{}).
		Build()
}

func testAgent(name, namespace string, rosterRef *agentv1alpha1.RosterRef) *agentv1alpha1.Agent {
	return &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace},
		Spec: agentv1alpha1.AgentSpec{
			Tier:       agentv1alpha1.TierPlatform,
			Operations: &agentv1alpha1.OperationsSpec{ApprovalRosterRef: rosterRef},
		},
	}
}

func testRoster(name, namespace string, minApprovals int32, allowSelf bool, approvers ...agentv1alpha1.Approver) *agentv1alpha1.ApprovalRoster {
	return &agentv1alpha1.ApprovalRoster{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace},
		Spec: agentv1alpha1.ApprovalRosterSpec{
			Approvers:         approvers,
			MinApprovals:      &minApprovals,
			AllowSelfApproval: &allowSelf,
		},
	}
}

func testRecord(name, namespace, requesterID string) *agentv1alpha1.ActionRecord {
	return &agentv1alpha1.ActionRecord{
		ObjectMeta: metav1.ObjectMeta{
			Name:              name,
			Namespace:         namespace,
			CreationTimestamp: metav1.NewTime(fixedNow.Add(-time.Hour)),
		},
		Spec: agentv1alpha1.ActionRecordSpec{
			ActionID:            "01ARZ3NDEKTSV4RRFFQ69G5FAV",
			AgentRef:            agentv1alpha1.AgentObjectRef{Name: "my-agent", Namespace: namespace},
			AgentIdentity:       "platform/proj",
			ActorServiceAccount: "my-agent-actor",
			Requester:           agentv1alpha1.ActionRequester{Kind: "human", ID: requesterID, Platform: "slack"},
			Intent:              "scale the thing",
			IdempotencyKey:      "sha256:0000000000000000000000000000000000000000000000000000000000000",
			Classification:      agentv1alpha1.ActionClassification{Class: agentv1alpha1.RiskGated},
			Targets: []agentv1alpha1.TargetRef{
				{Version: "v1", Kind: "Deployment", Namespace: namespace, Name: "web", UID: "uid-1"},
			},
		},
		Status: agentv1alpha1.ActionRecordStatus{Phase: agentv1alpha1.PhasePendingApproval},
	}
}
