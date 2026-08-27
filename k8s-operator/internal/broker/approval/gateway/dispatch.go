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
	"fmt"
	"time"

	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval"
)

// replayWindow bounds how long a platform event key is remembered for dedup. Slack and Google Chat
// both retry an unacknowledged webhook for a few minutes; five minutes covers that without
// remembering keys indefinitely.
const replayWindow = 5 * time.Minute

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
	if eventKey != "" && d.dedupCache().SeenRecently(eventKey, replayWindow, d.now()) {
		return "" // already handled; say nothing rather than reprocessing or replying twice
	}

	cmd, err := approval.ParseCommand(text)
	if err != nil {
		return err.Error()
	}

	ar, err := d.lookup(ctx, cmd.ActionID)
	if err != nil {
		return err.Error()
	}

	roster, unusableReason := approval.ResolveRoster(ctx, d.Client, ar)

	switch cmd.Verb {
	case approval.VerbApprove:
		if roster == nil {
			return fmt.Sprintf("%s: this action has no usable approval roster (%s)", approval.ActionRecordName(cmd.ActionID), unusableReason)
		}
		decision := approval.AuthorizeApprove(roster, ar, principal, d.now())
		if !decision.Allowed {
			return fmt.Sprintf("%s: %s", approval.ActionRecordName(cmd.ActionID), decision.Reason)
		}
		if err := approval.Write(ctx, d.Client, ar, func(ar *agentv1alpha1.ActionRecord) {
			approval.ApplyApprove(ar, roster, principal, "", d.now())
		}); err != nil {
			return fmt.Sprintf("%s: recording your approval failed: %v", approval.ActionRecordName(cmd.ActionID), err)
		}
		if ar.Status.Phase == agentv1alpha1.PhasePending {
			return fmt.Sprintf("%s: approved (%d/%d) — it will resume shortly", approval.ActionRecordName(cmd.ActionID), len(ar.Status.Approvals.Granted), ar.Status.Approvals.Required)
		}
		return fmt.Sprintf("%s: recorded your approval (%d/%d needed)", approval.ActionRecordName(cmd.ActionID), len(ar.Status.Approvals.Granted), ar.Status.Approvals.Required)

	case approval.VerbReject:
		if roster == nil {
			return fmt.Sprintf("%s: this action has no usable approval roster (%s)", approval.ActionRecordName(cmd.ActionID), unusableReason)
		}
		decision := approval.AuthorizeReject(roster, ar, principal, d.now())
		if !decision.Allowed {
			return fmt.Sprintf("%s: %s", approval.ActionRecordName(cmd.ActionID), decision.Reason)
		}
		if err := approval.Write(ctx, d.Client, ar, func(ar *agentv1alpha1.ActionRecord) {
			approval.ApplyReject(ar, roster, principal, cmd.Reason, d.now())
		}); err != nil {
			return fmt.Sprintf("%s: recording your rejection failed: %v", approval.ActionRecordName(cmd.ActionID), err)
		}
		return fmt.Sprintf("%s: rejected", approval.ActionRecordName(cmd.ActionID))

	default:
		return fmt.Sprintf("unrecognized command %q", text)
	}
}

// lookup finds the ActionRecord by its "ar-..." name across every namespace the gateway can read.
// ActionRecord is namespaced, but an approver typing an ID from a chat message has no reason to
// know which namespace the acting agent lived in, so this scans rather than requiring the caller
// to supply one. The gateway's Role grants get/list/watch cluster-wide on actionrecords for exactly
// this (config/rbac/chatops_gateway_role.yaml), and the fleet's PendingApproval backlog is small
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
