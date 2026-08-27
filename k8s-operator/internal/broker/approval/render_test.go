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
	"strings"
	"testing"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

func TestRenderMessageBeforeAnyApproval(t *testing.T) {
	roster := testRoster("r", testNS, 2, false, agentv1alpha1.Approver{Platform: "slack", ID: "U02"})
	ar := testRecord("ar-1", testNS, "slack:U01")
	ar.Spec.Classification.Reasons = []agentv1alpha1.ClassificationReason{
		{Rule: "production-environment", Class: "+1", Detail: "env=prod"},
	}

	m := approval.RenderMessage(ar, roster)

	if m.RecordName != journal.RecordName(ar.Spec.ActionID) {
		t.Errorf("recordName = %q", m.RecordName)
	}
	if m.Required != 2 {
		t.Errorf("required = %d, want roster's minApprovals (2) since status.approvals is nil", m.Required)
	}
	if m.ExpiresAt.IsZero() {
		t.Error("expected a derived expiry even before any command touches status.approvals")
	}
	if m.Resolution != "" {
		t.Errorf("resolution = %q, want empty while still PendingApproval", m.Resolution)
	}
	if len(m.Reasons) != 1 || !strings.Contains(m.Reasons[0], "production-environment") {
		t.Errorf("reasons = %v", m.Reasons)
	}
	text := m.Text()
	if !strings.Contains(text, ar.Spec.Intent) {
		t.Errorf("Text() should contain the intent: %q", text)
	}
	if !strings.Contains(text, "approve "+m.RecordName) {
		t.Errorf("Text() should show the typed approve command: %q", text)
	}
}

func TestRenderMessageAfterApproval(t *testing.T) {
	roster := testRoster("r", testNS, 1, false, agentv1alpha1.Approver{Platform: "slack", ID: "U02"})
	ar := testRecord("ar-1", testNS, "slack:U01")
	approval.ApplyApprove(ar, roster, "slack:U02", "", fixedNow)

	m := approval.RenderMessage(ar, roster)
	if m.Resolution != "approved" {
		t.Errorf("resolution = %q, want approved", m.Resolution)
	}
	if len(m.Granted) != 1 || m.Granted[0] != "slack:U02" {
		t.Errorf("granted = %v", m.Granted)
	}
}

func TestRenderMessageAfterRejection(t *testing.T) {
	roster := testRoster("r", testNS, 1, false, agentv1alpha1.Approver{Platform: "slack", ID: "U02"})
	ar := testRecord("ar-1", testNS, "slack:U01")
	approval.ApplyReject(ar, roster, "slack:U02", "no", fixedNow)

	m := approval.RenderMessage(ar, roster)
	if m.Resolution != "rejected" {
		t.Errorf("resolution = %q, want rejected", m.Resolution)
	}
}

func TestRenderMessageAfterExpiry(t *testing.T) {
	ar := testRecord("ar-1", testNS, "slack:U01")
	ar.Status.Phase = agentv1alpha1.PhaseExpired

	m := approval.RenderMessage(ar, nil)
	if m.Resolution != "expired" {
		t.Errorf("resolution = %q, want expired", m.Resolution)
	}
}

// The message must never leak Rationale — chat text is rendered from the record's structured
// fields only, never from prose an agent authored (06 §4.3's `report` principle).
func TestRenderMessageNeverIncludesRationale(t *testing.T) {
	ar := testRecord("ar-1", testNS, "slack:U01")
	ar.Spec.Rationale = "a very persuasive but classification-irrelevant justification"

	m := approval.RenderMessage(ar, nil)
	text := m.Text()
	if strings.Contains(text, "persuasive") {
		t.Error("rendered message must not include spec.rationale")
	}
}
