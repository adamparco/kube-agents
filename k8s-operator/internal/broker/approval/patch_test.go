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

package approval_test

import (
	"context"
	"sync"
	"testing"

	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval"
)

func TestApplyApproveSeedsApprovalsOnFirstWrite(t *testing.T) {
	roster := testRoster("r", testNS, 2, false,
		agentv1alpha1.Approver{Platform: "slack", ID: "U02"},
		agentv1alpha1.Approver{Platform: "slack", ID: "U03"})
	ar := testRecord("ar-1", testNS, "slack:U01")
	if ar.Status.Approvals != nil {
		t.Fatal("test fixture should start with no approvals block")
	}

	approval.ApplyApprove(ar, roster, "slack:U02", "", fixedNow)

	if ar.Status.Approvals == nil {
		t.Fatal("expected ApplyApprove to seed status.approvals")
	}
	if ar.Status.Approvals.Required != 2 {
		t.Errorf("required = %d, want 2 (from roster.EffectiveMinApprovals)", ar.Status.Approvals.Required)
	}
	wantDeadline := ar.CreationTimestamp.Add(roster.EffectiveTTL())
	if !ar.Status.Approvals.ExpiresAt.Time.Equal(wantDeadline) {
		t.Errorf("expiresAt = %v, want %v (creationTimestamp + roster TTL)", ar.Status.Approvals.ExpiresAt.Time, wantDeadline)
	}
	if len(ar.Status.Approvals.Granted) != 1 || ar.Status.Approvals.Granted[0].Principal != "slack:U02" {
		t.Errorf("granted = %+v", ar.Status.Approvals.Granted)
	}
	// One of two required: phase must NOT flip yet.
	if ar.Status.Phase != agentv1alpha1.PhasePendingApproval {
		t.Errorf("phase = %q, want still PendingApproval (only 1/2 approvals)", ar.Status.Phase)
	}
}

func TestApplyApproveFlipsPhaseWhenThresholdReached(t *testing.T) {
	roster := testRoster("r", testNS, 2, false,
		agentv1alpha1.Approver{Platform: "slack", ID: "U02"},
		agentv1alpha1.Approver{Platform: "slack", ID: "U03"})
	ar := testRecord("ar-1", testNS, "slack:U01")

	approval.ApplyApprove(ar, roster, "slack:U02", "", fixedNow)
	approval.ApplyApprove(ar, roster, "slack:U03", "", fixedNow)

	if ar.Status.Phase != agentv1alpha1.PhasePending {
		t.Errorf("phase = %q, want Pending once minApprovals is met", ar.Status.Phase)
	}
	if len(ar.Status.Approvals.Granted) != 2 {
		t.Errorf("granted count = %d, want 2", len(ar.Status.Approvals.Granted))
	}
}

// "the same approver typing approve twice counts once" (chat-approval.md §7).
func TestApplyApproveDuplicateDoesNotDoubleCount(t *testing.T) {
	roster := testRoster("r", testNS, 2, false,
		agentv1alpha1.Approver{Platform: "slack", ID: "U02"},
		agentv1alpha1.Approver{Platform: "slack", ID: "U03"})
	ar := testRecord("ar-1", testNS, "slack:U01")

	approval.ApplyApprove(ar, roster, "slack:U02", "", fixedNow)
	approval.ApplyApprove(ar, roster, "slack:U02", "", fixedNow) // same principal again

	if len(ar.Status.Approvals.Granted) != 1 {
		t.Errorf("granted count = %d, want 1 (duplicate must not double-count)", len(ar.Status.Approvals.Granted))
	}
	if ar.Status.Phase != agentv1alpha1.PhasePendingApproval {
		t.Errorf("phase = %q, want still PendingApproval (duplicate did not reach threshold)", ar.Status.Phase)
	}
}

func TestApplyRejectSetsTerminalPhaseUnconditionally(t *testing.T) {
	roster := testRoster("r", testNS, 5, false, agentv1alpha1.Approver{Platform: "slack", ID: "U02"})
	ar := testRecord("ar-1", testNS, "slack:U01")

	approval.ApplyReject(ar, roster, "slack:U02", "not now", fixedNow)

	if ar.Status.Phase != agentv1alpha1.PhaseRejected {
		t.Errorf("phase = %q, want Rejected", ar.Status.Phase)
	}
	if len(ar.Status.Approvals.Rejected) != 1 || ar.Status.Approvals.Rejected[0].Comment != "not now" {
		t.Errorf("rejected = %+v", ar.Status.Approvals.Rejected)
	}
}

// Write must touch only approvals and phase — the ChatOps gateway's exact VAP-permitted surface —
// and must not clobber a concurrent change to an unrelated field via a full-object write.
func TestWritePatchesOnlyWhatMutateChanged(t *testing.T) {
	ar := testRecord("ar-1", testNS, "slack:U01")
	ar.Status.Message = "set by someone else"
	c := fakeClient(t, ar)

	roster := testRoster("r", testNS, 1, false, agentv1alpha1.Approver{Platform: "slack", ID: "U02"})
	if err := approval.Write(context.Background(), c, ar, func(ar *agentv1alpha1.ActionRecord) {
		approval.ApplyApprove(ar, roster, "slack:U02", "", fixedNow)
	}); err != nil {
		t.Fatalf("Write: %v", err)
	}

	if ar.Status.Message != "set by someone else" {
		t.Errorf("message = %q; Write must not touch fields ApplyApprove did not change", ar.Status.Message)
	}
	if ar.Status.Phase != agentv1alpha1.PhasePending {
		t.Errorf("phase = %q, want Pending", ar.Status.Phase)
	}
}

func TestWriteConflictsWithAConcurrentEdit(t *testing.T) {
	ar := testRecord("ar-1", testNS, "slack:U01")
	c := fakeClient(t, ar)

	// Simulate a concurrent writer changing an unrelated field on the server after our copy of ar
	// was read, so Write's patch base is stale.
	live := &agentv1alpha1.ActionRecord{}
	if err := c.Get(context.Background(), client.ObjectKeyFromObject(ar), live); err != nil {
		t.Fatalf("get: %v", err)
	}
	live.Status.Message = "changed underneath us"
	if err := c.Status().Update(context.Background(), live); err != nil {
		t.Fatalf("update: %v", err)
	}

	roster := testRoster("r", testNS, 1, false, agentv1alpha1.Approver{Platform: "slack", ID: "U02"})
	err := approval.Write(context.Background(), c, ar, func(ar *agentv1alpha1.ActionRecord) {
		approval.ApplyApprove(ar, roster, "slack:U02", "", fixedNow)
	})
	// A merge patch computed from a stale base still applies over unrelated fields (it only names
	// approvals/phase), so this should SUCCEED and the concurrent message edit should survive —
	// which is exactly the property a full-object Update would have broken.
	if err != nil {
		t.Fatalf("Write: %v", err)
	}
	after := &agentv1alpha1.ActionRecord{}
	if err := c.Get(context.Background(), client.ObjectKeyFromObject(ar), after); err != nil {
		t.Fatalf("get: %v", err)
	}
	if after.Status.Message != "changed underneath us" {
		t.Errorf("message = %q; a merge patch must not clobber a concurrent unrelated field", after.Status.Message)
	}
	if after.Status.Phase != agentv1alpha1.PhasePending {
		t.Errorf("phase = %q, want Pending", after.Status.Phase)
	}
}

// Adversarial review finding (auth-security #1 / code-quality #1): two roster members approving
// within the same window each read status.approvals while it was still nil, so each computes a
// merge patch touching "granted" from its own stale base. Granted/Rejected are +listType=atomic,
// so a plain client.MergeFrom patch REPLACES the array server-side rather than merging it —
// without retry-on-conflict and an optimistic lock, whichever patch lands second silently
// overwrites the first approver's entry, and a roster requiring 2 distinct approvers never
// reaches quorum no matter how many people approve. This test is the reproduction that finding
// was built on, run against real goroutines racing the same fake client.
func TestWriteSurvivesTwoConcurrentApprovers(t *testing.T) {
	roster := testRoster("r", testNS, 2, false,
		agentv1alpha1.Approver{Platform: "slack", ID: "U02"},
		agentv1alpha1.Approver{Platform: "slack", ID: "U03"})
	ar := testRecord("ar-1", testNS, "slack:U01")
	c := fakeClient(t, ar)

	principals := []string{"slack:U02", "slack:U03"}
	errs := make(chan error, len(principals))
	var wg sync.WaitGroup
	for _, p := range principals {
		wg.Add(1)
		go func(principal string) {
			defer wg.Done()
			fresh := &agentv1alpha1.ActionRecord{}
			if err := c.Get(context.Background(), client.ObjectKeyFromObject(ar), fresh); err != nil {
				errs <- err
				return
			}
			errs <- approval.Write(context.Background(), c, fresh, func(ar *agentv1alpha1.ActionRecord) {
				approval.ApplyApprove(ar, roster, principal, "", fixedNow)
			})
		}(p)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatalf("Write: %v", err)
		}
	}

	after := &agentv1alpha1.ActionRecord{}
	if err := c.Get(context.Background(), client.ObjectKeyFromObject(ar), after); err != nil {
		t.Fatalf("get: %v", err)
	}
	if after.Status.Approvals == nil || len(after.Status.Approvals.Granted) != 2 {
		t.Fatalf("granted = %+v, want both concurrent approvals to have landed", after.Status.Approvals)
	}
	if after.Status.Phase != agentv1alpha1.PhasePending {
		t.Errorf("phase = %q, want Pending: both approvals landed, so minApprovals (2) is met", after.Status.Phase)
	}
}
