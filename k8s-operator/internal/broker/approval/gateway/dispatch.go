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

package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval"
)

// replayWindow bounds how long a platform event key is remembered for dedup. Slack and Google Chat
// both retry an unacknowledged webhook for a few minutes; five minutes covers that without
// remembering keys indefinitely.
const replayWindow = 5 * time.Minute

// CommandRequest is the JSON payload accepted by POST /v1alpha1/commands from
// a2a-gateway's zero-inference approval command interceptor.
type CommandRequest struct {
	Platform  string `json:"platform,omitempty"`
	SenderID  string `json:"senderId,omitempty"`
	Principal string `json:"principal,omitempty"`
	EventID   string `json:"eventId,omitempty"`
	ChannelID string `json:"channelId,omitempty"`
	ThreadID  string `json:"threadId,omitempty"`
	Text      string `json:"text,omitempty"`
	Command   string `json:"command,omitempty"`
	ActionID  string `json:"actionId,omitempty"`
	Reason        string `json:"reason,omitempty"`
	Namespace     string `json:"namespace,omitempty"`
	CapabilityRef string `json:"capabilityRef,omitempty"`
}

// CommandResponse is the JSON payload returned by POST /v1alpha1/commands.
type CommandResponse struct {
	OK         bool   `json:"ok"`
	Reply      string `json:"reply"`
	DecisionID string `json:"decisionId,omitempty"`
}

// Dispatcher is the platform-independent core: given an already-authenticated platform event (the
// caller has already verified the request came from the real Slack app or the real Google Chat
// app) and the extracted principal and command text, it parses, authorizes, and writes.
//
// This is the ONLY place in the gateway binary that calls approval.Write, so the VAP-facing surface
// this process exercises is exactly the one function.
type Dispatcher struct {
	Client client.Client
	// Now is injectable for tests; defaults to time.Now.
	Now func() time.Time
	// WaitTimeout, when > 0, bounds how long /approve and /undo poll for execution/undo completion
	// so the synchronous chat reply includes the verified outcome.
	WaitTimeout time.Duration

	dedup *dedup
}

func (d *Dispatcher) now() time.Time {
	if d.Now != nil {
		return d.Now()
	}
	return time.Now()
}

func (d *Dispatcher) dedupCache() *dedup {
	if d.dedup == nil {
		d.dedup = newDedup()
	}
	return d.dedup
}

// Handle parses and authorizes one command from principal, and returns the text to reply with in
// chat. eventKey identifies the platform delivery (a Slack request's signature, a Google Chat
// message's resource name) for replay dedup; an empty eventKey disables dedup for that call, which
// callers should treat as a bug in the platform adapter rather than a supported mode.
func (d *Dispatcher) Handle(ctx context.Context, eventKey, principal, text string) string {
	reply, _ := d.handle(ctx, eventKey, principal, text, "", "")
	return reply
}

// HandleCommand executes a structured CommandRequest and returns a CommandResponse.
func (d *Dispatcher) HandleCommand(ctx context.Context, req CommandRequest) CommandResponse {
	principal := resolveCommandPrincipal(req)
	if principal == "" {
		return CommandResponse{
			OK:    false,
			Reply: "approval command refused: missing or invalid sender principal",
		}
	}

	text := strings.TrimSpace(req.Text)
	if text == "" && req.Command != "" && req.ActionID != "" {
		if strings.TrimSpace(req.Reason) != "" {
			text = fmt.Sprintf("%s %s %s", strings.TrimSpace(req.Command), strings.TrimSpace(req.ActionID), strings.TrimSpace(req.Reason))
		} else {
			text = fmt.Sprintf("%s %s", strings.TrimSpace(req.Command), strings.TrimSpace(req.ActionID))
		}
	}

	eventKey := strings.TrimSpace(req.EventID)
	decisionID := eventKey
	if decisionID == "" {
		decisionID = fmt.Sprintf("%s:%d", principal, d.now().UnixNano())
	}

	reply, ok := d.handle(ctx, eventKey, principal, text, strings.TrimSpace(req.Namespace), strings.TrimSpace(req.CapabilityRef))
	return CommandResponse{
		OK:         ok,
		Reply:      reply,
		DecisionID: decisionID,
	}
}

// CommandHandler returns an http.Handler serving POST /v1alpha1/commands.
func (d *Dispatcher) CommandHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var req CommandRequest
		dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64*1024))
		if err := dec.Decode(&req); err != nil {
			http.Error(w, fmt.Sprintf("invalid command payload: %v", err), http.StatusBadRequest)
			return
		}
		resp := d.HandleCommand(r.Context(), req)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(resp)
	})
}

func resolveCommandPrincipal(req CommandRequest) string {
	if p := strings.TrimSpace(req.Principal); p != "" {
		if strings.HasPrefix(p, "gchat:") {
			p = "googlechat:" + strings.TrimPrefix(p, "gchat:")
		}
		if isValidPrincipal(p) {
			return p
		}
	}
	platform := strings.ToLower(strings.TrimSpace(req.Platform))
	if platform == "gchat" {
		platform = "googlechat"
	}
	sender := strings.TrimSpace(req.SenderID)
	if sender == "" {
		return ""
	}
	if strings.HasPrefix(sender, "gchat:") {
		sender = "googlechat:" + strings.TrimPrefix(sender, "gchat:")
	}
	if isValidPrincipal(sender) {
		return sender
	}
	var candidate string
	switch platform {
	case "slack":
		candidate = approval.SlackPrincipal(sender)
	case "googlechat":
		candidate = approval.GoogleChatPrincipal(sender)
	case "discord":
		candidate = approval.DiscordPrincipal(sender)
	default:
		return ""
	}
	if !isValidPrincipal(candidate) {
		return ""
	}
	return candidate
}

func isValidPrincipal(p string) bool {
	plat, id, ok := strings.Cut(p, ":")
	if !ok || strings.TrimSpace(id) == "" {
		return false
	}
	switch plat {
	case "slack", "discord":
		return true
	case "googlechat":
		return strings.HasPrefix(id, "users/") && len(strings.TrimPrefix(id, "users/")) > 0
	default:
		return false
	}
}

func (d *Dispatcher) handle(ctx context.Context, eventKey, principal, text, namespace, capabilityRef string) (string, bool) {
	if eventKey != "" && d.dedupCache().SeenRecently(eventKey, replayWindow, d.now()) {
		return "", false // already handled; say nothing rather than reprocessing or replying twice
	}

	cmd, err := approval.ParseCommand(text)
	if err != nil {
		return err.Error(), false
	}

	ar, err := d.lookupInNamespace(ctx, namespace, cmd.ActionID)
	if err != nil {
		return err.Error(), false
	}

	roster, unusableReason := approval.ResolveRoster(ctx, d.Client, ar)

	switch cmd.Verb {
	case approval.VerbApprove:
		if roster == nil {
			return fmt.Sprintf("%s: this action has no usable approval roster (%s)", approval.ActionRecordName(cmd.ActionID), unusableReason), false
		}
		decision := approval.AuthorizeApprove(roster, ar, principal, d.now())
		if !decision.Allowed {
			return fmt.Sprintf("%s: %s", approval.ActionRecordName(cmd.ActionID), decision.Reason), false
		}
		if err := approval.Write(ctx, d.Client, ar, func(ar *agentv1alpha1.ActionRecord) {
			approval.ApplyApprove(ar, roster, principal, capabilityRef, d.now())
		}); err != nil {
			return fmt.Sprintf("%s: recording your approval failed: %v", approval.ActionRecordName(cmd.ActionID), err), false
		}
		if ar.Status.Phase == agentv1alpha1.PhasePending && d.WaitTimeout > 0 {
			d.waitForApproveCompletion(ctx, ar)
		}
		return formatApproveReply(cmd.ActionID, ar, principal), true

	case approval.VerbReject:
		if roster == nil {
			return fmt.Sprintf("%s: this action has no usable approval roster (%s)", approval.ActionRecordName(cmd.ActionID), unusableReason), false
		}
		decision := approval.AuthorizeReject(roster, ar, principal, d.now())
		if !decision.Allowed {
			return fmt.Sprintf("%s: %s", approval.ActionRecordName(cmd.ActionID), decision.Reason), false
		}
		if err := approval.Write(ctx, d.Client, ar, func(ar *agentv1alpha1.ActionRecord) {
			approval.ApplyReject(ar, roster, principal, cmd.Reason, d.now())
		}); err != nil {
			return fmt.Sprintf("%s: recording your rejection failed: %v", approval.ActionRecordName(cmd.ActionID), err), false
		}
		return formatRejectReply(cmd.ActionID, ar, principal, cmd.Reason), true

	case approval.VerbUndo:
		if roster == nil {
			return fmt.Sprintf("%s: this action has no usable approval roster (%s)", approval.ActionRecordName(cmd.ActionID), unusableReason), false
		}
		decision := approval.AuthorizeUndo(roster, ar, principal)
		if !decision.Allowed {
			return fmt.Sprintf("%s: %s", approval.ActionRecordName(cmd.ActionID), decision.Reason), false
		}
		reason := strings.TrimSpace(cmd.Reason)
		if reason == "" {
			reason = "undo requested via chatops"
		}
		markContested := true
		ur := &agentv1alpha1.UndoRequest{
			ObjectMeta: metav1.ObjectMeta{
				GenerateName: ar.Name + "-undo-",
				Namespace:    ar.Namespace,
			},
			Spec: agentv1alpha1.UndoRequestSpec{
				ActionRef:     agentv1alpha1.ActionRef{Name: ar.Name},
				Reason:        reason,
				RequestedBy:   principal,
				MarkContested: &markContested,
			},
		}
		if err := d.Client.Create(ctx, ur); err != nil {
			return fmt.Sprintf("%s: creating undo request failed: %v", approval.ActionRecordName(cmd.ActionID), err), false
		}
		var reverseAR *agentv1alpha1.ActionRecord
		if d.WaitTimeout > 0 {
			reverseAR = d.waitForUndoCompletion(ctx, ur, ar)
		}
		return formatUndoReply(cmd.ActionID, ar, ur, reverseAR, reason), true

	case approval.VerbUncontest:
		if roster == nil {
			return fmt.Sprintf("%s: this action has no usable approval roster (%s)", approval.ActionRecordName(cmd.ActionID), unusableReason), false
		}
		decision := approval.AuthorizeUncontest(roster, ar, principal)
		if !decision.Allowed {
			return fmt.Sprintf("%s: %s", approval.ActionRecordName(cmd.ActionID), decision.Reason), false
		}
		if err := approval.Write(ctx, d.Client, ar, func(ar *agentv1alpha1.ActionRecord) {
			approval.ApplyUncontest(ar)
		}); err != nil {
			return fmt.Sprintf("%s: clearing contested flag failed: %v", approval.ActionRecordName(cmd.ActionID), err), false
		}
		return formatUncontestReply(cmd.ActionID, ar, principal), true

	default:
		return fmt.Sprintf("unrecognized command %q", text), false
	}
}

func (d *Dispatcher) waitForApproveCompletion(ctx context.Context, ar *agentv1alpha1.ActionRecord) {
	waitCtx, cancel := context.WithTimeout(ctx, d.WaitTimeout)
	defer cancel()
	ticker := time.NewTicker(250 * time.Millisecond)
	defer ticker.Stop()

	key := client.ObjectKeyFromObject(ar)
	for {
		select {
		case <-waitCtx.Done():
			return
		case <-ticker.C:
			latest := &agentv1alpha1.ActionRecord{}
			if err := d.Client.Get(waitCtx, key, latest); err != nil {
				continue
			}
			*ar = *latest
			switch ar.Status.Phase {
			case agentv1alpha1.PhaseVerified, agentv1alpha1.PhaseRolledBack,
				agentv1alpha1.PhaseFailed, agentv1alpha1.PhaseRejected,
				agentv1alpha1.PhaseExpired, agentv1alpha1.PhaseUndone:
				return
			}
		}
	}
}

func (d *Dispatcher) waitForUndoCompletion(ctx context.Context, ur *agentv1alpha1.UndoRequest, ar *agentv1alpha1.ActionRecord) *agentv1alpha1.ActionRecord {
	waitCtx, cancel := context.WithTimeout(ctx, d.WaitTimeout)
	defer cancel()
	ticker := time.NewTicker(250 * time.Millisecond)
	defer ticker.Stop()

	urKey := client.ObjectKeyFromObject(ur)
	arKey := client.ObjectKeyFromObject(ar)
	for {
		select {
		case <-waitCtx.Done():
			return nil
		case <-ticker.C:
			latestUR := &agentv1alpha1.UndoRequest{}
			if err := d.Client.Get(waitCtx, urKey, latestUR); err == nil {
				*ur = *latestUR
			}
			latestAR := &agentv1alpha1.ActionRecord{}
			if err := d.Client.Get(waitCtx, arKey, latestAR); err == nil {
				*ar = *latestAR
			}
			if ur.Status.Phase.IsTerminal() || ar.Status.Phase == agentv1alpha1.PhaseUndone {
				undoID := ur.Status.UndoActionID
				if undoID == "" {
					undoID = ar.Status.UndoneBy
				}
				if undoID != "" {
					rev := &agentv1alpha1.ActionRecord{}
					if err := d.Client.Get(waitCtx, client.ObjectKey{Namespace: ar.Namespace, Name: approval.ActionRecordName(undoID)}, rev); err == nil {
						return rev
					}
				}
				return nil
			}
		}
	}
}

func canonicalActionID(cmdActionID string, ar *agentv1alpha1.ActionRecord) string {
	if ar != nil && strings.TrimSpace(ar.Spec.ActionID) != "" {
		return strings.TrimSpace(ar.Spec.ActionID)
	}
	trimmed := strings.TrimSpace(cmdActionID)
	if strings.HasPrefix(strings.ToLower(trimmed), "ar-") {
		return strings.ToUpper(trimmed[3:])
	}
	return strings.ToUpper(trimmed)
}

func formatTargetsList(ar *agentv1alpha1.ActionRecord) string {
	if ar == nil || len(ar.Spec.Targets) == 0 {
		return "(none)"
	}
	parts := make([]string, 0, len(ar.Spec.Targets))
	for _, t := range ar.Spec.Targets {
		gv := t.Version
		if t.Group != "" {
			gv = t.Group + "/" + t.Version
		}
		if t.Namespace != "" {
			parts = append(parts, fmt.Sprintf("%s/%s %s/%s", gv, t.Kind, t.Namespace, t.Name))
		} else {
			parts = append(parts, fmt.Sprintf("%s/%s %s", gv, t.Kind, t.Name))
		}
	}
	return strings.Join(parts, ", ")
}

func appendActionMetadata(b *strings.Builder, actionID string, ar *agentv1alpha1.ActionRecord) {
	if ar == nil {
		return
	}
	if actionID != "" {
		fmt.Fprintf(b, "• Action ID: %s\n", actionID)
	}
	if strings.TrimSpace(ar.Spec.Intent) != "" {
		fmt.Fprintf(b, "• Intent: %s\n", strings.TrimSpace(ar.Spec.Intent))
	}
	if len(ar.Spec.Targets) > 0 {
		fmt.Fprintf(b, "• Target(s): %s\n", formatTargetsList(ar))
	}
	if ar.Spec.Classification.Class != "" {
		fmt.Fprintf(b, "• Risk Class: %s\n", ar.Spec.Classification.Class)
	}
}

func formatApproveReply(cmdActionID string, ar *agentv1alpha1.ActionRecord, principal string) string {
	recName := approval.ActionRecordName(cmdActionID)
	granted, required := 0, int32(1)
	if ar != nil && ar.Status.Approvals != nil {
		granted = len(ar.Status.Approvals.Granted)
		required = ar.Status.Approvals.Required
	}
	actID := canonicalActionID(cmdActionID, ar)

	var b strings.Builder
	if ar != nil && ar.Status.Phase == agentv1alpha1.PhasePendingApproval {
		fmt.Fprintf(&b, "%s: recorded your approval (%d/%d needed)\n", recName, granted, required)
		appendActionMetadata(&b, actID, ar)
		fmt.Fprintf(&b, "• Approver: %s\n", principal)
		fmt.Fprintf(&b, "• Status: Waiting for %d more approval(s) before execution.\n", int(required)-granted)
		return strings.TrimRight(b.String(), "\n")
	}

	fmt.Fprintf(&b, "%s: approved (%d/%d) — it will resume shortly\n", recName, granted, required)
	appendActionMetadata(&b, actID, ar)
	fmt.Fprintf(&b, "• Approver: %s\n", principal)
	if ar != nil {
		switch ar.Status.Phase {
		case agentv1alpha1.PhaseVerified:
			fmt.Fprintf(&b, "• Phase: ✅ Verified\n")
			if ar.Status.Report != nil {
				if ar.Status.Report.Did != "" {
					fmt.Fprintf(&b, "• Did: %s\n", ar.Status.Report.Did)
				}
				if ar.Status.Report.Verified != "" {
					fmt.Fprintf(&b, "• Verified: %s\n", ar.Status.Report.Verified)
				}
			}
			if ar.Spec.Undo != nil && ar.Spec.Undo.Strategy != agentv1alpha1.UndoNone {
				fmt.Fprintf(&b, "• Next Step: Send `/undo %s` if you need to revert this change.\n", actID)
			} else if ar.Spec.Undo != nil && ar.Spec.Undo.Strategy == agentv1alpha1.UndoNone {
				fmt.Fprintf(&b, "• Undo Plan: none (this action is effectful/non-reversible and has no automatic `/undo`).\n")
			}
		case agentv1alpha1.PhaseRolledBack, agentv1alpha1.PhaseFailed:
			fmt.Fprintf(&b, "• Phase: ⚠️ %s\n", ar.Status.Phase)
			if ar.Status.Message != "" {
				fmt.Fprintf(&b, "• Detail: %s\n", ar.Status.Message)
			}
		case agentv1alpha1.PhaseRejected, agentv1alpha1.PhaseExpired:
			fmt.Fprintf(&b, "• Phase: 🛑 %s\n", ar.Status.Phase)
			if ar.Status.Message != "" {
				fmt.Fprintf(&b, "• Detail: %s\n", ar.Status.Message)
			}
		default:
			fmt.Fprintf(&b, "• Phase: %s (Action Broker is executing and verifying the mutation)\n", ar.Status.Phase)
			if ar.Spec.Undo != nil && ar.Spec.Undo.Strategy != agentv1alpha1.UndoNone {
				fmt.Fprintf(&b, "• Undo Hint: Once verified, send `/undo %s` to revert if needed.\n", actID)
			} else if ar.Spec.Undo != nil && ar.Spec.Undo.Strategy == agentv1alpha1.UndoNone {
				fmt.Fprintf(&b, "• Undo Plan: none (this action is effectful/non-reversible and has no automatic `/undo`).\n")
			}
		}
	}
	return strings.TrimRight(b.String(), "\n")
}

func formatRejectReply(cmdActionID string, ar *agentv1alpha1.ActionRecord, principal, reason string) string {
	recName := approval.ActionRecordName(cmdActionID)
	actID := canonicalActionID(cmdActionID, ar)
	var b strings.Builder
	fmt.Fprintf(&b, "%s: rejected\n", recName)
	appendActionMetadata(&b, actID, ar)
	fmt.Fprintf(&b, "• Rejected By: %s\n", principal)
	if strings.TrimSpace(reason) != "" {
		fmt.Fprintf(&b, "• Reason: %s\n", strings.TrimSpace(reason))
	}
	fmt.Fprintf(&b, "• Status: 🛑 Gated action cancelled (Phase: Rejected). No cluster changes were applied.")
	return b.String()
}

func formatUndoReply(cmdActionID string, ar *agentv1alpha1.ActionRecord, ur *agentv1alpha1.UndoRequest, reverseAR *agentv1alpha1.ActionRecord, reason string) string {
	recName := approval.ActionRecordName(cmdActionID)
	actID := canonicalActionID(cmdActionID, ar)
	var b strings.Builder
	fmt.Fprintf(&b, "%s: undo requested (%s)\n", recName, ur.Name)
	appendActionMetadata(&b, actID, ar)
	if ar != nil && ar.Spec.Undo != nil && ar.Spec.Undo.Strategy != "" {
		fmt.Fprintf(&b, "• Undo Strategy: %s\n", ar.Spec.Undo.Strategy)
	}
	fmt.Fprintf(&b, "• Reason: %s\n", reason)

	switch {
	case ur.Status.Phase == agentv1alpha1.UndoExecuted || (ar != nil && ar.Status.Phase == agentv1alpha1.PhaseUndone):
		undoID := ur.Status.UndoActionID
		if undoID == "" && ar != nil {
			undoID = ar.Status.UndoneBy
		}
		if undoID != "" {
			fmt.Fprintf(&b, "• Outcome: ✅ Undo executed (Phase: Undone, reverse action: %s)\n", undoID)
		} else {
			fmt.Fprintf(&b, "• Outcome: ✅ Undo executed (Phase: Undone)\n")
		}
		if reverseAR != nil && reverseAR.Status.Report != nil {
			if reverseAR.Status.Report.Did != "" {
				fmt.Fprintf(&b, "• Reverted: %s\n", reverseAR.Status.Report.Did)
			}
			if reverseAR.Status.Report.Verified != "" {
				fmt.Fprintf(&b, "• Verified: %s\n", reverseAR.Status.Report.Verified)
			}
		} else if ar != nil && ar.Spec.Undo != nil && len(ar.Spec.Undo.Steps) > 0 {
			steps := make([]string, 0, len(ar.Spec.Undo.Steps))
			for _, st := range ar.Spec.Undo.Steps {
				if st.Target.Namespace != "" {
					steps = append(steps, fmt.Sprintf("%s %s %s/%s", st.Op, st.Target.Kind, st.Target.Namespace, st.Target.Name))
				} else {
					steps = append(steps, fmt.Sprintf("%s %s %s", st.Op, st.Target.Kind, st.Target.Name))
				}
			}
			fmt.Fprintf(&b, "• Reverted: %s\n", strings.Join(steps, ", "))
		}
		fmt.Fprintf(&b, "• Contested: ⚠️ Target resource is now CONTESTED (automated agent mutations to this target are frozen).\n")
		fmt.Fprintf(&b, "• Next Step: Send `/uncontest %s` when ready to allow future agent mutations.", actID)
	case ur.Status.Phase == agentv1alpha1.UndoFailed || ur.Status.Phase == agentv1alpha1.UndoRefused:
		fmt.Fprintf(&b, "• Outcome: ❌ Undo %s: %s", strings.ToLower(string(ur.Status.Phase)), ur.Status.Message)
	default:
		fmt.Fprintf(&b, "• Status: ⏳ Undo controller is reverting the change and marking the target resource CONTESTED.\n")
		fmt.Fprintf(&b, "• Next Step: Once reverted, send `/uncontest %s` to unfreeze future agent mutations.", actID)
	}
	return strings.TrimRight(b.String(), "\n")
}

func formatUncontestReply(cmdActionID string, ar *agentv1alpha1.ActionRecord, principal string) string {
	recName := approval.ActionRecordName(cmdActionID)
	actID := canonicalActionID(cmdActionID, ar)
	var b strings.Builder
	fmt.Fprintf(&b, "%s: contested marker cleared\n", recName)
	appendActionMetadata(&b, actID, ar)
	fmt.Fprintf(&b, "• Cleared By: %s\n", principal)
	fmt.Fprintf(&b, "• Status: 🔓 Target resource is no longer contested — automated agent mutations can proceed again.")
	return b.String()
}

func (d *Dispatcher) lookupInNamespace(ctx context.Context, namespace, idOrName string) (*agentv1alpha1.ActionRecord, error) {
	if namespace != "" {
		name := approval.ActionRecordName(idOrName)
		ar := &agentv1alpha1.ActionRecord{}
		if err := d.Client.Get(ctx, client.ObjectKey{Namespace: namespace, Name: name}, ar); err == nil {
			return ar, nil
		}
	}
	return d.lookup(ctx, idOrName)
}

// lookup finds the ActionRecord by its "ar-..." name across every namespace the gateway can read.
// ActionRecord is namespaced, but an approver typing an ID from a chat message has no reason to
// know which namespace the acting agent lived in, so this scans rather than requiring the caller
// to supply one. The gateway's Role grants get/list/watch cluster-wide on actionrecords for exactly
// this (config/chatops-gateway/rbac.yaml), and the fleet's PendingApproval backlog is small
// enough that an unindexed list-and-scan is the right amount of mechanism.
func (d *Dispatcher) lookup(ctx context.Context, idOrName string) (*agentv1alpha1.ActionRecord, error) {
	name := approval.ActionRecordName(idOrName)
	list := &agentv1alpha1.ActionRecordList{}
	if err := d.Client.List(ctx, list); err != nil {
		return nil, fmt.Errorf("listing action records: %w", err)
	}
	for i := range list.Items {
		if list.Items[i].Name == name {
			return &list.Items[i], nil
		}
	}
	return nil, fmt.Errorf("no such action: %s", name)
}
