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

// Package approval is the chat-approval loop's shared core: resolving the roster a gated
// ActionRecord answers to, authorizing an approve/reject command against it, and writing the
// decision back within the exact field set the ChatOps gateway identity is allowed
// (config/policy/vap-agent-scope-journal.yaml). Both the notifier and the gateway import this
// package rather than duplicating the resolution chain, so "roster-unusable" means the same thing
// in both places (docs/designs/broker/chat-approval.md §§2-3).
package approval

import (
	"context"
	"fmt"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

// ResolveRoster walks spec.agentRef -> the Agent CR -> spec.operations.approvalRosterRef -> the
// ApprovalRoster (chat-approval.md §2's chain). Any break returns a nil roster and a human-readable
// reason; it never returns a Go error, because every break in this chain has one meaning —
// roster-unusable, fail closed (06 §4.4 row 6) — and a typed error would tempt a caller to retry a
// "not found" as if it were transient.
func ResolveRoster(ctx context.Context, r client.Reader, ar *agentv1alpha1.ActionRecord) (*agentv1alpha1.ApprovalRoster, string) {
	if ar == nil {
		return nil, "no ActionRecord"
	}

	agent := &agentv1alpha1.Agent{}
	agentKey := client.ObjectKey{Name: ar.Spec.AgentRef.Name, Namespace: ar.Spec.AgentRef.Namespace}
	if err := r.Get(ctx, agentKey, agent); err != nil {
		if apierrors.IsNotFound(err) {
			return nil, fmt.Sprintf("agent %s not found", agentKey)
		}
		return nil, fmt.Sprintf("reading agent %s: %v", agentKey, err)
	}

	if agent.Spec.Operations == nil || agent.Spec.Operations.ApprovalRosterRef == nil {
		return nil, fmt.Sprintf("agent %s names no approvalRosterRef", agentKey)
	}
	ref := agent.Spec.Operations.ApprovalRosterRef
	ns := ref.Namespace
	if ns == "" {
		ns = agent.Namespace
	}

	roster := &agentv1alpha1.ApprovalRoster{}
	rosterKey := client.ObjectKey{Name: ref.Name, Namespace: ns}
	if err := r.Get(ctx, rosterKey, roster); err != nil {
		if apierrors.IsNotFound(err) {
			return nil, fmt.Sprintf("approvalroster %s not found", rosterKey)
		}
		return nil, fmt.Sprintf("reading approvalroster %s: %v", rosterKey, err)
	}

	// Admission requires at least one approver (MinItems=1 on Spec.Approvers), so this is belt and
	// suspenders against a roster edited to empty after admission, or read through a cache that has
	// not caught up with a delete-then-recreate. Either way the answer is the same one HasApprover
	// already gives a nil roster: nobody can approve, so treat it as unusable rather than as a
	// roster with a closed list of zero people.
	if len(roster.Spec.Approvers) == 0 {
		return nil, fmt.Sprintf("approvalroster %s has no approvers", rosterKey)
	}

	return roster, ""
}
