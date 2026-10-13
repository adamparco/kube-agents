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
	"strings"
	"testing"
	"time"

	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval/gateway"
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
	grantChatOpsExtraCRs(ctx, t, admin)
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
		ObjectMeta: metav1.ObjectMeta{Name: "roster-x", Namespace: testNS},
		Spec: agentv1alpha1.ApprovalRosterSpec{
			Approvers: []agentv1alpha1.Approver{
				{Platform: "slack", ID: "U02"},
				{Platform: "discord", ID: "99887766"},
			},
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

	// V-CHAT-003: two approvers racing the same record, or a retried slash command arriving after
	// the record already resolved, must not re-open it. Validation 4 admits a gateway write only
	// when the record's OLD phase is PendingApproval; once approval.Write above has already moved
	// it to Rejected, a second gateway write is denied at admission, not merely a no-op that
	// silently succeeds.
	t.Run("a late gateway write is denied once the record has already left PendingApproval", func(t *testing.T) {
		ar := mkPendingRecord(t)
		gatewayClient := as(chatOpsUser)

		fresh := &agentv1alpha1.ActionRecord{}
		if err := gatewayClient.Get(ctx, client.ObjectKeyFromObject(ar), fresh); err != nil {
			t.Fatalf("gateway reading the record: %v", err)
		}
		if err := approval.Write(ctx, gatewayClient, fresh, func(ar *agentv1alpha1.ActionRecord) {
			approval.ApplyReject(ar, roster, "slack:U02", "not now", now)
		}); err != nil {
			t.Fatalf("resolving the record ahead of the late write: %v", err)
		}
		if fresh.Status.Phase != agentv1alpha1.PhaseRejected {
			t.Fatalf("setup: phase = %q, want Rejected before the late write", fresh.Status.Phase)
		}

		err := approval.Write(ctx, gatewayClient, fresh, func(ar *agentv1alpha1.ActionRecord) {
			approval.ApplyApprove(ar, roster, "slack:U02", "", now)
		})
		if err == nil {
			t.Fatal("a late write against an already-resolved record was admitted")
		}
		if !apierrors.IsForbidden(err) {
			t.Fatalf("expected Forbidden, got: %v", err)
		}
	})

	t.Run("the ChatOps gateway is admitted when clearing status.contested via /uncontest and denied if setting it", func(t *testing.T) {
		seq++
		ar := newActionRecord(fmt.Sprintf("ar-chatops-%02d", seq), now)
		ar.Spec.ActorServiceAccount = brokerSA
		ar.Spec.ActionID = fmt.Sprintf("%s%02d", testULID[:24], seq%100)
		if err := admin.Create(ctx, ar); err != nil {
			t.Fatalf("create record: %v", err)
		}
		// Undo controller sets PhaseUndone and Contested=true.
		if err := statusUpdateAs(ctx, t, as, undoUser, ar, func(ar *agentv1alpha1.ActionRecord) {
			ar.Status.Phase = agentv1alpha1.PhaseUndone
			ar.Status.Contested = true
		}); err != nil {
			t.Fatalf("setting PhaseUndone + Contested=true as undo controller: %v", err)
		}

		gatewayClient := as(chatOpsUser)
		fresh := &agentv1alpha1.ActionRecord{}
		if err := gatewayClient.Get(ctx, client.ObjectKeyFromObject(ar), fresh); err != nil {
			t.Fatalf("gateway reading the record: %v", err)
		}
		decision := approval.AuthorizeUncontest(roster, fresh, "slack:U02")
		if !decision.Allowed {
			t.Fatalf("AuthorizeUncontest refused a legitimate uncontest: %s", decision.Reason)
		}
		if err := approval.Write(ctx, gatewayClient, fresh, approval.ApplyUncontest); err != nil {
			t.Fatalf("the ChatOps gateway was denied clearing status.contested via /uncontest: %v", err)
		}
		if fresh.Status.Contested {
			t.Fatalf("expected status.contested to be cleared to false, got true")
		}

		// Attempting to set status.contested: false -> true as ChatOps gateway must be denied by VAP.
		setErr := approval.Write(ctx, gatewayClient, fresh, func(ar *agentv1alpha1.ActionRecord) {
			ar.Status.Contested = true
		})
		if setErr == nil {
			t.Fatal("the ChatOps gateway was permitted to set status.contested=true")
		}
		if !apierrors.IsForbidden(setErr) {
			t.Fatalf("expected Forbidden when ChatOps gateway sets status.contested=true, got: %v", setErr)
		}
	})

	t.Run("the ChatOps gateway creates an UndoRequest CR on /undo and is denied if it sets status.phase=Undone or mutates status.timestamps", func(t *testing.T) {
		agentObj := &agentv1alpha1.Agent{
			ObjectMeta: metav1.ObjectMeta{Name: "cluster-a-agent", Namespace: testNS},
			Spec: agentv1alpha1.AgentSpec{
				Tier:       agentv1alpha1.TierClusterAdmin,
				Scope:      &agentv1alpha1.ScopeSpec{ProjectID: "proj-x", ClusterName: "cluster-a"},
				Operations: &agentv1alpha1.OperationsSpec{ApprovalRosterRef: &agentv1alpha1.RosterRef{Name: roster.Name}},
				Harness:    &agentv1alpha1.HarnessSpec{ClusterName: "cluster-a", Location: "us-central1"},
			},
		}
		if err := admin.Create(ctx, agentObj); err != nil && !apierrors.IsAlreadyExists(err) {
			t.Fatalf("create Agent CR: %v", err)
		}
		if err := admin.Create(ctx, roster.DeepCopy()); err != nil && !apierrors.IsAlreadyExists(err) {
			t.Fatalf("create ApprovalRoster CR: %v", err)
		}

		seq++
		ar := newActionRecord(fmt.Sprintf("ar-chatops-%02d", seq), now)
		ar.Spec.ActorServiceAccount = brokerSA
		ar.Spec.ActionID = fmt.Sprintf("%s%02d", testULID[:24], seq%100)
		ar.Spec.Undo = &agentv1alpha1.UndoPlan{
			Strategy:    agentv1alpha1.UndoRestore,
			GeneratedAt: metav1.NewTime(now),
			Validated:   true,
			Steps: []agentv1alpha1.UndoStep{{
				Op:     "apply",
				Target: ar.Spec.Targets[0],
				Object: &runtime.RawExtension{Raw: []byte(`{"replicas":2}`)},
			}},
		}
		if err := admin.Create(ctx, ar); err != nil {
			t.Fatalf("create record: %v", err)
		}
		if err := statusUpdateAs(ctx, t, as, brokerUser, ar, func(ar *agentv1alpha1.ActionRecord) {
			ar.Status.Phase = agentv1alpha1.PhaseVerified
		}); err != nil {
			t.Fatalf("moving record to Verified as owning broker: %v", err)
		}

		gatewayClient := as(chatOpsUser)
		d := &gateway.Dispatcher{Client: gatewayClient, Now: func() time.Time { return now }}
		reply := d.Handle(ctx, "event-undo-envtest-1", "discord:99887766", "/undo "+ar.Name+" rollback bad release")
		if !strings.Contains(reply, "undo requested") {
			t.Fatalf("Dispatcher.Handle(/undo) reply = %q, want undo confirmation", reply)
		}

		urs := &agentv1alpha1.UndoRequestList{}
		if err := gatewayClient.List(ctx, urs, client.InNamespace(testNS)); err != nil {
			t.Fatalf("list UndoRequests: %v", err)
		}
		var foundUR *agentv1alpha1.UndoRequest
		for i := range urs.Items {
			if urs.Items[i].Spec.ActionRef.Name == ar.Name {
				foundUR = &urs.Items[i]
				break
			}
		}
		if foundUR == nil {
			t.Fatalf("expected UndoRequest CR targeting %s, found none", ar.Name)
		}
		if foundUR.Spec.RequestedBy != "discord:99887766" || foundUR.Spec.Reason != "rollback bad release" {
			t.Errorf("UndoRequest spec = %+v", foundUR.Spec)
		}

		// Attempting to directly set status.phase = Undone as ChatOps gateway must be denied by VAP.
		fresh := &agentv1alpha1.ActionRecord{}
		if err := gatewayClient.Get(ctx, client.ObjectKeyFromObject(ar), fresh); err != nil {
			t.Fatalf("get record: %v", err)
		}
		phaseErr := approval.Write(ctx, gatewayClient, fresh, func(ar *agentv1alpha1.ActionRecord) {
			ar.Status.Phase = agentv1alpha1.PhaseUndone
		})
		if phaseErr == nil {
			t.Fatal("the ChatOps gateway was permitted to directly set status.phase = Undone")
		}
		if !apierrors.IsForbidden(phaseErr) {
			t.Fatalf("expected Forbidden when ChatOps gateway sets status.phase = Undone, got: %v", phaseErr)
		}

		// Attempting to mutate status.timestamps as ChatOps gateway must be denied by VAP.
		if err := gatewayClient.Get(ctx, client.ObjectKeyFromObject(ar), fresh); err != nil {
			t.Fatalf("get record: %v", err)
		}
		tsErr := approval.Write(ctx, gatewayClient, fresh, func(ar *agentv1alpha1.ActionRecord) {
			ar.Status.Timestamps = &agentv1alpha1.ActionTimestamps{Submitted: ptrTime(now)}
		})
		if tsErr == nil {
			t.Fatal("the ChatOps gateway was permitted to mutate status.timestamps")
		}
		if !apierrors.IsForbidden(tsErr) {
			t.Fatalf("expected Forbidden when ChatOps gateway mutates status.timestamps, got: %v", tsErr)
		}
	})

	t.Run("the owning broker is denied if it attempts to mutate status.approvals or status.contested", func(t *testing.T) {
		ar := mkPendingRecord(t)
		brokerClient := as(brokerUser)

		fresh := &agentv1alpha1.ActionRecord{}
		if err := brokerClient.Get(ctx, client.ObjectKeyFromObject(ar), fresh); err != nil {
			t.Fatalf("broker reading the record: %v", err)
		}
		appErr := approval.Write(ctx, brokerClient, fresh, func(ar *agentv1alpha1.ActionRecord) {
			ar.Status.Approvals = &agentv1alpha1.ActionApprovals{
				Required: 1,
				Granted:  []agentv1alpha1.ApprovalEntry{{Principal: "slack:U02", At: metav1.NewTime(now)}},
			}
		})
		if appErr == nil {
			t.Fatal("the owning broker was permitted to mutate status.approvals")
		}
		if !apierrors.IsForbidden(appErr) {
			t.Fatalf("expected Forbidden when owning broker mutates status.approvals, got: %v", appErr)
		}

		// Have undo controller set Contested=true, then verify owning broker cannot clear or set status.contested.
		if err := statusUpdateAs(ctx, t, as, undoUser, ar, func(ar *agentv1alpha1.ActionRecord) {
			ar.Status.Phase = agentv1alpha1.PhaseUndone
			ar.Status.Contested = true
		}); err != nil {
			t.Fatalf("undo controller setting Contested=true: %v", err)
		}
		if err := brokerClient.Get(ctx, client.ObjectKeyFromObject(ar), fresh); err != nil {
			t.Fatalf("broker reading the record: %v", err)
		}
		contestedErr := approval.Write(ctx, brokerClient, fresh, func(ar *agentv1alpha1.ActionRecord) {
			ar.Status.Contested = false
		})
		if contestedErr == nil {
			t.Fatal("the owning broker was permitted to mutate status.contested")
		}
		if !apierrors.IsForbidden(contestedErr) {
			t.Fatalf("expected Forbidden when owning broker mutates status.contested, got: %v", contestedErr)
		}
	})
}

func grantChatOpsExtraCRs(ctx context.Context, t *testing.T, c client.Client) {
	t.Helper()
	role := &rbacv1.ClusterRole{
		ObjectMeta: metav1.ObjectMeta{Name: "kube-agents-chatops-extra-test-writer"},
		Rules: []rbacv1.PolicyRule{{
			APIGroups: []string{"kubeagents.x-k8s.io"},
			Resources: []string{"agents", "approvalrosters", "undorequests"},
			Verbs:     []string{"get", "list", "watch", "create", "update", "patch", "delete"},
		}},
	}
	if err := c.Create(ctx, role); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("create extra test ClusterRole: %v", err)
	}
	binding := &rbacv1.ClusterRoleBinding{
		ObjectMeta: metav1.ObjectMeta{Name: "kube-agents-chatops-extra-test-writer"},
		RoleRef:    rbacv1.RoleRef{APIGroup: rbacv1.GroupName, Kind: "ClusterRole", Name: role.Name},
		Subjects:   []rbacv1.Subject{{APIGroup: rbacv1.GroupName, Kind: "Group", Name: testWriterGroup}},
	}
	if err := c.Create(ctx, binding); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("create extra test ClusterRoleBinding: %v", err)
	}
}

// statusUpdateAs applies mutate to ar's status as user and refreshes ar in place with the server's
// response, so subsequent calls in the same test see a resourceVersion the server will accept.
func statusUpdateAs(ctx context.Context, t *testing.T, as func(string) client.Client, user string, ar *agentv1alpha1.ActionRecord, mutate func(*agentv1alpha1.ActionRecord)) error {
	t.Helper()
	mutate(ar)
	return as(user).Status().Update(ctx, ar)
}
