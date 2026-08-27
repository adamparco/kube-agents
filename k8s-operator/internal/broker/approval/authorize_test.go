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
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval"
)

var fixedNow = time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)

// V-CHAT-001: a principal not on the roster is refused.
func TestAuthorizeApproveRefusesNonRosterMember(t *testing.T) {
	roster := testRoster("r", testNS, 1, false, agentv1alpha1.Approver{Platform: "slack", ID: "U02"})
	ar := testRecord("ar-1", testNS, "slack:U01")

	d := approval.AuthorizeApprove(roster, ar, "slack:U99", fixedNow)
	if d.Allowed {
		t.Fatal("expected a non-roster principal to be refused")
	}
}

func TestAuthorizeApproveAllowsRosterMember(t *testing.T) {
	roster := testRoster("r", testNS, 1, false, agentv1alpha1.Approver{Platform: "slack", ID: "U02"})
	ar := testRecord("ar-1", testNS, "slack:U01")

	d := approval.AuthorizeApprove(roster, ar, "slack:U02", fixedNow)
	if !d.Allowed {
		t.Fatalf("expected the roster member to be allowed, got refused: %s", d.Reason)
	}
}

// V-CHAT-002: self-approval is refused when AllowSelfApproval is false, including for an
// AttributionUnverified requester ("deny on match, never allow on doubt").
func TestAuthorizeApproveRefusesSelfApproval(t *testing.T) {
	roster := testRoster("r", testNS, 1, false, agentv1alpha1.Approver{Platform: "slack", ID: "U01"})
	ar := testRecord("ar-1", testNS, "slack:U01")

	d := approval.AuthorizeApprove(roster, ar, "slack:U01", fixedNow)
	if d.Allowed {
		t.Fatal("expected self-approval to be refused when allowSelfApproval is false")
	}
}

func TestAuthorizeApproveRefusesSelfApprovalEvenWhenUnverified(t *testing.T) {
	roster := testRoster("r", testNS, 1, false, agentv1alpha1.Approver{Platform: "slack", ID: "U01"})
	ar := testRecord("ar-1", testNS, "slack:U01")
	ar.Spec.AttributionUnverified = true

	d := approval.AuthorizeApprove(roster, ar, "slack:U01", fixedNow)
	if d.Allowed {
		t.Fatal("an unverified requester still counts for the self-approval denial (deny on match, never allow on doubt)")
	}
}

func TestAuthorizeApproveAllowsSelfApprovalWhenEnabled(t *testing.T) {
	roster := testRoster("r", testNS, 1, true, agentv1alpha1.Approver{Platform: "slack", ID: "U01"})
	ar := testRecord("ar-1", testNS, "slack:U01")

	d := approval.AuthorizeApprove(roster, ar, "slack:U01", fixedNow)
	if !d.Allowed {
		t.Fatalf("expected self-approval to be allowed when allowSelfApproval is true, got: %s", d.Reason)
	}
}

// Reject has no self-approval restriction (chat-approval.md §4 sequence 2).
func TestAuthorizeRejectAllowsSelfReject(t *testing.T) {
	roster := testRoster("r", testNS, 1, false, agentv1alpha1.Approver{Platform: "slack", ID: "U01"})
	ar := testRecord("ar-1", testNS, "slack:U01")

	d := approval.AuthorizeReject(roster, ar, "slack:U01", fixedNow)
	if !d.Allowed {
		t.Fatalf("expected a self-reject to be allowed, got refused: %s", d.Reason)
	}
}

// V-CHAT-003 (partial; the admission-time half is the VAP's oldPhase guard, exercised in
// gateway envtest coverage): expiry is never an approval.
func TestAuthorizeRefusesAfterTheApprovalWindowCloses(t *testing.T) {
	roster := testRoster("r", testNS, 1, false, agentv1alpha1.Approver{Platform: "slack", ID: "U02"})
	ar := testRecord("ar-1", testNS, "slack:U01")
	expired := metav1.NewTime(fixedNow.Add(-time.Minute))
	ar.Status.Approvals = &agentv1alpha1.ActionApprovals{Required: 1, ExpiresAt: &expired}

	d := approval.AuthorizeApprove(roster, ar, "slack:U02", fixedNow)
	if d.Allowed {
		t.Fatal("expected refusal once the approval window has closed")
	}
}

func TestAuthorizeAllowsBeforeTheWindowCloses(t *testing.T) {
	roster := testRoster("r", testNS, 1, false, agentv1alpha1.Approver{Platform: "slack", ID: "U02"})
	ar := testRecord("ar-1", testNS, "slack:U01")
	future := metav1.NewTime(fixedNow.Add(time.Minute))
	ar.Status.Approvals = &agentv1alpha1.ActionApprovals{Required: 1, ExpiresAt: &future}

	d := approval.AuthorizeApprove(roster, ar, "slack:U02", fixedNow)
	if !d.Allowed {
		t.Fatalf("expected the command to be allowed before the window closes, got: %s", d.Reason)
	}
}

// V-CHAT-007: a roster that cannot be resolved refuses, it does not open the gate.
func TestAuthorizeRefusesWithNilRoster(t *testing.T) {
	ar := testRecord("ar-1", testNS, "slack:U01")
	d := approval.AuthorizeApprove(nil, ar, "slack:U02", fixedNow)
	if d.Allowed {
		t.Fatal("expected a nil (unusable) roster to refuse rather than open the gate")
	}
}

func TestAuthorizeRefusesWhenNotPendingApproval(t *testing.T) {
	roster := testRoster("r", testNS, 1, false, agentv1alpha1.Approver{Platform: "slack", ID: "U02"})
	ar := testRecord("ar-1", testNS, "slack:U01")
	ar.Status.Phase = agentv1alpha1.PhaseExpired

	d := approval.AuthorizeApprove(roster, ar, "slack:U02", fixedNow)
	if d.Allowed {
		t.Fatal("expected refusal for a record that is no longer PendingApproval")
	}
}

func TestAuthorizeRefusesNilRecord(t *testing.T) {
	roster := testRoster("r", testNS, 1, false, agentv1alpha1.Approver{Platform: "slack", ID: "U02"})
	d := approval.Authorize(roster, nil, "slack:U02", fixedNow)
	if d.Allowed {
		t.Fatal("expected refusal for a nil record")
	}
}
