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

package approval

import (
	"fmt"
	"strings"
	"time"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

// Message is a platform-neutral rendering of one ActionRecord's approval ask or its resolution.
// The notifier turns this into a Slack Block Kit payload or a Google Chat card; this type is the
// one place the CONTENT is decided, so both platforms show the same facts.
//
// Rendered from the record's structured fields only (spec.intent, spec.classification,
// spec.targets, status.approvals) — never from Rationale, which is recorded but is explicitly not
// a classification input and is kept out of the ask for the same reason: chat text is rendered
// from the record, never the reverse (06 §4.3's `report` principle, chat-approval.md §2).
type Message struct {
	// RecordName is the "ar-..." name, the identifier a human copies into "approve <id>".
	RecordName string

	// Intent is the one-line statement of what the action is for.
	Intent string

	// Class is the risk class that gated this action.
	Class agentv1alpha1.ActionRiskClass

	// Reasons is why it classified the way it did, in evaluation order.
	Reasons []string

	// Targets is a short, human-readable list of what the action touches.
	Targets []string

	// RequesterID is who asked (the platform-qualified principal, or a system identity).
	RequesterID string

	// Required and Granted describe roster progress.
	Required int32
	Granted  []string

	// ExpiresAt is when the approval window closes.
	ExpiresAt time.Time

	// Resolution is empty while the record is still PendingApproval, and one of "approved",
	// "rejected", or "expired" once it leaves that phase — the notifier uses it to decide whether
	// to post a new message or edit an existing one to a terminal state (chat-approval.md §2).
	Resolution string
}

// RenderMessage builds a Message from an ActionRecord and the roster it resolved against. The
// caller passes ar.Status.Approvals directly (it may be nil — a record can reach PendingApproval
// before any command has touched status.approvals, since only the gateway ever writes that block,
// see patch.go's ensureApprovals) and this function reads roster-derived defaults in that case, so
// the FIRST notification a human sees already states the true required count and deadline.
func RenderMessage(ar *agentv1alpha1.ActionRecord, roster *agentv1alpha1.ApprovalRoster) Message {
	m := Message{
		RecordName:  journal.RecordName(ar.Spec.ActionID),
		Intent:      ar.Spec.Intent,
		Class:       ar.Spec.Classification.Class,
		RequesterID: ar.Spec.Requester.ID,
	}

	for _, r := range ar.Spec.Classification.Reasons {
		m.Reasons = append(m.Reasons, fmt.Sprintf("%s (%s): %s", r.Rule, r.Class, r.Detail))
	}
	for _, t := range ar.Spec.Targets {
		m.Targets = append(m.Targets, targetSummary(t))
	}

	if ar.Status.Approvals != nil {
		m.Required = ar.Status.Approvals.Required
		if ar.Status.Approvals.ExpiresAt != nil {
			m.ExpiresAt = ar.Status.Approvals.ExpiresAt.Time
		}
		for _, e := range ar.Status.Approvals.Granted {
			m.Granted = append(m.Granted, e.Principal)
		}
	} else if roster != nil {
		m.Required = roster.EffectiveMinApprovals()
		m.ExpiresAt = ar.CreationTimestamp.Add(roster.EffectiveTTL())
	}

	switch ar.Status.Phase {
	case agentv1alpha1.PhasePending:
		m.Resolution = "approved"
	case agentv1alpha1.PhaseRejected:
		m.Resolution = "rejected"
	case agentv1alpha1.PhaseExpired:
		m.Resolution = "expired"
	}

	return m
}

func targetSummary(t agentv1alpha1.TargetRef) string {
	gv := t.Version
	if t.Group != "" {
		gv = t.Group + "/" + t.Version
	}
	if t.Namespace != "" {
		return fmt.Sprintf("%s/%s %s/%s", gv, t.Kind, t.Namespace, t.Name)
	}
	return fmt.Sprintf("%s/%s %s", gv, t.Kind, t.Name)
}

// Text renders a plain-text form, used by both the Slack Block Kit fallback text (Slack requires
// one for accessibility and for surfaces that cannot render blocks) and the Google Chat card's
// plain body.
//
// The voice follows 02 §2.5's gated-ask shape: the evidence, the risk class, who was asked, and
// the typed approve command — in that order, because a human deciding whether to approve reads the
// evidence first and the command last.
func (m Message) Text() string {
	var b strings.Builder
	switch m.Resolution {
	case "approved":
		fmt.Fprintf(&b, "%s: approved (%d/%d) — %s\n", m.RecordName, len(m.Granted), m.Required, m.Intent)
		return b.String()
	case "rejected":
		fmt.Fprintf(&b, "%s: rejected — %s\n", m.RecordName, m.Intent)
		return b.String()
	case "expired":
		fmt.Fprintf(&b, "%s: expired unreviewed — %s\n", m.RecordName, m.Intent)
		return b.String()
	}

	fmt.Fprintf(&b, "*%s* is asking to: %s\n", m.RequesterID, m.Intent)
	fmt.Fprintf(&b, "Risk class: *%s*\n", m.Class)
	for _, r := range m.Reasons {
		fmt.Fprintf(&b, "  • %s\n", r)
	}
	if len(m.Targets) > 0 {
		b.WriteString("Targets:\n")
		for _, t := range m.Targets {
			fmt.Fprintf(&b, "  • %s\n", t)
		}
	}
	fmt.Fprintf(&b, "Approvals: %d/%d", len(m.Granted), m.Required)
	if len(m.Granted) > 0 {
		fmt.Fprintf(&b, " (%s)", strings.Join(m.Granted, ", "))
	}
	b.WriteString("\n")
	if !m.ExpiresAt.IsZero() {
		fmt.Fprintf(&b, "Expires: %s\n", m.ExpiresAt.UTC().Format(time.RFC3339))
	}
	fmt.Fprintf(&b, "Reply: approve %s   |   reject %s <reason>\n", m.RecordName, m.RecordName)
	return b.String()
}
