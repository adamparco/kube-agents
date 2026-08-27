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

package controller_test

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval"
)

// TestChatOpsGatewayApprovalPackageObeysThePolicy proves the composition this branch's unit tests
// cannot: that internal/broker/approval's Write/ApplyApprove/ApplyReject — the ONE code path that
// performs the ChatOps gateway's sanctioned write (docs/designs/broker/chat-approval.md §3) —
// actually succeeds when run as the real, VAP-named principal against a real API server, and is
// denied for everyone else, including the owning broker and a human. The gateway's fake-client
// tests (internal/broker/approval/gateway) prove the DECISION is correct; this proves the WRITE
// the decision leads to is the one write vap-agent-scope-journal.yaml allows for that identity.
func TestChatOpsGatewayApprovalPackageObeysThePolicy(t *testing.T) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		t.Skip("KUBEBUILDER_ASSETS unset; run via `make test` to exercise vap-agent-scope-journal")
	}

	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		t.Fatalf("add clientgo scheme: %v", err)
	}
	if err := agentv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("add kube-agents scheme: %v", err)
	}

	testEnv := &envtest.Environment{
		CRDDirectoryPaths:     []string{filepath.Join("..", "..", "..", "config", "crd", "bases")},
		ErrorIfCRDPathMissing: true,
		Scheme:                scheme,
	}
	cfg, err := testEnv.Start()
	if err != nil {
		t.Fatalf("start envtest: %v", err)
	}
	t.Cleanup(func() { _ = testEnv.Stop() })

	admin, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatalf("new admin client: %v", err)
	}
	ctx := context.Background()

	grantTestWriters(ctx, t, admin)
	applyPolicyFile(ctx, t, admin, filepath.Join("..", "..", "..", "config", "policy", "vap-agent-scope-journal.yaml"))

	as := func(user string) client.Client {
		impersonated := rest.CopyConfig(cfg)
		impersonated.Impersonate = rest.ImpersonationConfig{UserName: user, Groups: []string{testWriterGroup}}
		c, cErr := client.New(impersonated, client.Options{Scheme: scheme})
		if cErr != nil {
			t.Fatalf("impersonated client for %q: %v", user, cErr)
		}
		return c
	}

	now := time.Date(2026, 7, 27, 12, 0, 0, 0, time.UTC)
	seq := 0
	mkPendingRecord := func(t *testing.T) *agentv1alpha1.ActionRecord {
		t.Helper()
		seq++
		ar := newActionRecord(fmt.Sprintf("ar-chatops-%02d", seq), now)
		ar.Spec.ActorServiceAccount = brokerSA
		ar.Spec.ActionID = fmt.Sprintf("%s%02d", testULID[:24], seq%100)
		if err := admin.Create(ctx, ar); err != nil {
			t.Fatalf("create record: %v", err)
		}
		// The owning broker parks it — the one write validation 2 grants it, matching stepGate.
		if err := statusUpdateAs(ctx, t, as, brokerUser, ar, func(ar *agentv1alpha1.ActionRecord) {
			ar.Status.Phase = agentv1alpha1.PhasePendingApproval
		}); err != nil {
			t.Fatalf("parking the record as the owning broker: %v", err)
		}
		return ar
	}

	// Load-order guard, same reasoning as TestJournalStatusPolicy's waitForPolicy: without this, a
	// pass below could be "nothing is enforcing yet", not "the gateway is allowed".
	waitForPolicy(ctx, t, as(humanUser), mkPendingRecord(t))

	roster := &agentv1alpha1.ApprovalRoster{
		ObjectMeta: metav1.ObjectMeta{Name: "roster-x"},
		Spec: agentv1alpha1.ApprovalRosterSpec{
			Approvers: []agentv1alpha1.Approver{{Platform: "slack", ID: "U02"}},
		},
	}

	t.Run("the ChatOps gateway identity may approve through approval.Write", func(t *testing.T) {
		ar := mkPendingRecord(t)
		gatewayClient := as(chatOpsUser)

		fresh := &agentv1alpha1.ActionRecord{}
		if err := gatewayClient.Get(ctx, client.ObjectKeyFromObject(ar), fresh); err != nil {
			t.Fatalf("gateway reading the record: %v", err)
		}
		decision := approval.AuthorizeApprove(roster, fresh, "slack:U02", now)
		if !decision.Allowed {
			t.Fatalf("Authorize refused a legitimate approval: %s", decision.Reason)
		}
		if err := approval.Write(ctx, gatewayClient, fresh, func(ar *agentv1alpha1.ActionRecord) {
			approval.ApplyApprove(ar, roster, "slack:U02", "", now)
		}); err != nil {
			t.Fatalf("the ChatOps gateway was denied its own sanctioned write: %v", err)
		}
		if fresh.Status.Phase != agentv1alpha1.PhasePending {
			t.Errorf("phase = %q, want Pending", fresh.Status.Phase)
		}
	})

	t.Run("a human cannot perform the same write directly", func(t *testing.T) {
		ar := mkPendingRecord(t)
		humanClient := as(humanUser)

		fresh := &agentv1alpha1.ActionRecord{}
		if err := admin.Get(ctx, client.ObjectKeyFromObject(ar), fresh); err != nil {
			t.Fatalf("reading the record: %v", err)
		}
		err := approval.Write(ctx, humanClient, fresh, func(ar *agentv1alpha1.ActionRecord) {
			approval.ApplyApprove(ar, roster, "slack:U02", "", now)
		})
		if err == nil {
			t.Fatal("a human writing through approval.Write was admitted; four-eyes is decorative if this succeeds")
		}
		if !apierrors.IsForbidden(err) {
			t.Fatalf("expected Forbidden, got: %v", err)
		}
	})

	t.Run("the owning broker cannot perform the same write", func(t *testing.T) {
		ar := mkPendingRecord(t)
		brokerClient := as(brokerUser)

		fresh := &agentv1alpha1.ActionRecord{}
		if err := admin.Get(ctx, client.ObjectKeyFromObject(ar), fresh); err != nil {
			t.Fatalf("reading the record: %v", err)
		}
		err := approval.Write(ctx, brokerClient, fresh, func(ar *agentv1alpha1.ActionRecord) {
			approval.ApplyApprove(ar, roster, "slack:U02", "", now)
		})
		if err == nil {
			t.Fatal("the owning broker approved its own action through approval.Write")
		}
		if !apierrors.IsForbidden(err) {
			t.Fatalf("expected Forbidden, got: %v", err)
		}
	})

	t.Run("reject moves PendingApproval to Rejected", func(t *testing.T) {
		ar := mkPendingRecord(t)
		gatewayClient := as(chatOpsUser)

		fresh := &agentv1alpha1.ActionRecord{}
		if err := gatewayClient.Get(ctx, client.ObjectKeyFromObject(ar), fresh); err != nil {
			t.Fatalf("gateway reading the record: %v", err)
		}
		if err := approval.Write(ctx, gatewayClient, fresh, func(ar *agentv1alpha1.ActionRecord) {
			approval.ApplyReject(ar, roster, "slack:U02", "not now", now)
		}); err != nil {
			t.Fatalf("the ChatOps gateway was denied a reject: %v", err)
		}
		if fresh.Status.Phase != agentv1alpha1.PhaseRejected {
			t.Errorf("phase = %q, want Rejected", fresh.Status.Phase)
		}
	})
}

// statusUpdateAs applies mutate to ar's status as user and refreshes ar in place with the server's
// response, so subsequent calls in the same test see a resourceVersion the server will accept.
func statusUpdateAs(ctx context.Context, t *testing.T, as func(string) client.Client, user string, ar *agentv1alpha1.ActionRecord, mutate func(*agentv1alpha1.ActionRecord)) error {
	t.Helper()
	mutate(ar)
	return as(user).Status().Update(ctx, ar)
}
