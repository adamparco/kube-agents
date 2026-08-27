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
	"context"
	"fmt"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

// ensureApprovals lazily seeds status.approvals the first time any command touches a record. The
// broker never writes this block (it is one of the three fields VAP validation 2 forbids the
// owning broker), so nothing populates Required/ExpiresAt at park time; the first ApplyApprove or
// ApplyReject for a record does it, anchored on the record's own creationTimestamp — the one
// timestamp nobody can move — rather than "now", so the window is "TTL since parked", not "TTL
// since somebody first tried".
func ensureApprovals(ar *agentv1alpha1.ActionRecord, roster *agentv1alpha1.ApprovalRoster) {
	if ar.Status.Approvals != nil {
		return
	}
	deadline := metav1.NewTime(ar.CreationTimestamp.Add(roster.EffectiveTTL()))
	ar.Status.Approvals = &agentv1alpha1.ActionApprovals{
		Required:  roster.EffectiveMinApprovals(),
		ExpiresAt: &deadline,
	}
}

// distinctGranted counts distinct principals in Granted — duplicates are never double-counted
// (chat-approval.md §7: "the same approver typing approve twice counts once").
func distinctGranted(ar *agentv1alpha1.ActionRecord) map[string]bool {
	seen := make(map[string]bool, len(ar.Status.Approvals.Granted))
	for _, e := range ar.Status.Approvals.Granted {
		seen[e.Principal] = true
	}
	return seen
}

// ApplyApprove records one approval and, if it is the one that reaches EffectiveMinApprovals,
// flips the phase PendingApproval -> Pending in the same write. The caller MUST have already
// called AuthorizeApprove and gotten Allowed==true; this function does not re-check authorization,
// only the write-time invariant that is always worth asserting regardless of caller discipline
// (an already-recorded principal is a no-op, not a second entry).
//
// The mutation happens on `ar` in place and the return value is the same pointer, so the caller
// (typically a client.MergeFrom-based patch, see gateway.Writer) sees exactly what changed.
func ApplyApprove(ar *agentv1alpha1.ActionRecord, roster *agentv1alpha1.ApprovalRoster, principal, comment string, now time.Time) {
	ensureApprovals(ar, roster)

	if distinctGranted(ar)[principal] {
		return // already recorded; re-approving is a no-op, not a second entry (chat-approval.md §7)
	}
	ar.Status.Approvals.Granted = append(ar.Status.Approvals.Granted, agentv1alpha1.ApprovalEntry{
		Principal: principal,
		At:        metav1.NewTime(now),
		Comment:   comment,
	})

	if int32(len(distinctGranted(ar))) >= ar.Status.Approvals.Required {
		ar.Status.Phase = agentv1alpha1.PhasePending
	}
}

// ApplyReject records a rejection and terminates the record: any single valid reject sets phase
// Rejected, unconditionally — reject has no threshold to meet (chat-approval.md §4 sequence 2).
func ApplyReject(ar *agentv1alpha1.ActionRecord, roster *agentv1alpha1.ApprovalRoster, principal, reason string, now time.Time) {
	ensureApprovals(ar, roster)
	ar.Status.Approvals.Rejected = append(ar.Status.Approvals.Rejected, agentv1alpha1.ApprovalEntry{
		Principal: principal,
		At:        metav1.NewTime(now),
		Comment:   reason,
	})
	ar.Status.Phase = agentv1alpha1.PhaseRejected
}

// Write patches exactly the fields ApplyApprove/ApplyReject touched — approvals and phase, the
// ChatOps gateway's whole write surface (config/policy/vap-agent-scope-journal.yaml validation 4)
// — using a merge patch computed against a copy taken before mutation. A full-object Status().Update
// here would risk carrying a stale copy of fields another writer changed concurrently (verification,
// recovery, timestamps) backward over their write; the merge patch only ever contains what actually
// changed in this call, matching the idiom in internal/broker/controller/undo_controller.go.
func Write(ctx context.Context, c client.Client, ar *agentv1alpha1.ActionRecord, mutate func(*agentv1alpha1.ActionRecord)) error {
	base := ar.DeepCopy()
	mutate(ar)
	if err := c.Status().Patch(ctx, ar, client.MergeFrom(base)); err != nil {
		return fmt.Errorf("approval: patching %s/%s status: %w", ar.Namespace, ar.Name, err)
	}
	return nil
}
