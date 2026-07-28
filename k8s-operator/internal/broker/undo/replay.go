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

package undo

import (
	"fmt"
	"time"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

// Step 2 of 05 §1.3 — the undo controller's admission gate, as one pure predicate.
//
// It lives here rather than inside the controller for the reason journal.DeletableAt lives outside
// the retention controller: the property "an undo is only attempted when the record can actually
// bear one" has to be assertable by a check without standing up a manager, and two implementations
// that agree today are how the reachability failure in LSN-035 happened. The controller calls this
// and does nothing else to decide.
//
// Every branch REFUSES rather than repairs, and refusing is not erroring. The record is still there
// and a human can reconstruct the change from the export sink by hand; the system just stops
// claiming that one command will do it correctly. That distinction is why UndoRefused is a separate
// terminal phase from UndoFailed (06 §4.4): refused means never attempted, and retrying produces the
// same answer.

// ReplayRefusal is a stable, machine-readable reason an undo was not attempted. The values are
// written into UndoRequest conditions and into the ActionRecord message, so they are part of the
// contract a human reads during an incident and are not free text.
type ReplayRefusal string

const (
	// ReplayAllowed is the zero value, and it is deliberately the EMPTY refusal rather than a
	// positive "allowed" constant: a caller that forgets to check gets "" and a caller that
	// compares against a named allow constant would pass on a zero-valued struct.
	ReplayAllowed ReplayRefusal = ""

	// RefuseNoRecord — the referenced ActionRecord does not exist, or is nil. Past its retention TTL
	// the CR is gone and only the export remains, which no controller can replay.
	RefuseNoRecord ReplayRefusal = "action-record-missing"

	// RefuseAlreadyUndone — the record already carries the reverse half of the 06 §4.3 linkage.
	// Checked BEFORE the phase, because an undone record's phase is Undone and reporting that as
	// "not successful" would send a human looking for a failure that never happened.
	RefuseAlreadyUndone ReplayRefusal = "already-undone"

	// RefuseNotExecuted — the phase is not terminal-and-successful. A Pending or Executing record
	// has not finished, and Failed / Rejected / Expired / DryRun / RolledBack never changed the
	// world in a way that survives, so there is nothing to put back.
	RefuseNotExecuted ReplayRefusal = "action-not-executed"

	// RefusePlanUnusable — ValidateReplayable said no. The detail carries which rule.
	RefusePlanUnusable ReplayRefusal = "undo-plan-unusable"

	// RefuseWindowExpired — spec.retention.undoWindowExpiresAt has passed. The snapshot may still be
	// in the record, but the promise attached to it was time-boxed on purpose: the further the world
	// has moved, the more likely replaying a stale pre-state is a second incident.
	RefuseWindowExpired ReplayRefusal = "undo-window-expired"
)

// String makes a refusal printable in a log line without a cast.
func (r ReplayRefusal) String() string {
	if r == ReplayAllowed {
		return "allowed"
	}
	return string(r)
}

// Executed reports whether a phase means the action reached the world and its effect is still
// standing. It is narrower than journal.Terminal on purpose, and the difference is the whole point
// of this predicate.
//
// journal.Terminal answers "can this record still change?", which is the retention question. This
// answers "is there something to put back?", which is the undo question, and the two disagree on
// five phases. Failed, Rejected, Expired and DryRun are terminal and never landed. RolledBack is
// terminal and landed, but the recovery ladder (04 §5.1) already put it back — undoing it again
// would apply the pre-state on top of the pre-state, which is a no-op on a good day and a
// resurrection of a deleted object on a bad one.
//
// Undone is excluded here too, but a caller reaches RefuseAlreadyUndone first and gets the better
// message; this is the backstop for a record moved to Undone without the linkage being written.
func Executed(phase agentv1alpha1.ActionPhase) bool {
	return phase == agentv1alpha1.PhaseVerified
}

// Replayable is the gate. An empty refusal means go; anything else means the controller writes
// UndoRefused and stops. The second return is the human-readable detail, always non-empty when the
// refusal is, because "undo-plan-unusable" alone does not tell an operator which of the six rules in
// ValidateReplayable fired.
//
// The order is chosen so the reported reason is the most specific true one, not merely the first
// checkable one. Already-undone precedes phase, phase precedes plan, plan precedes the window:
// telling somebody their undo window expired on a record that was never executed would be true and
// useless.
func Replayable(ar *agentv1alpha1.ActionRecord, now time.Time) (ReplayRefusal, string) {
	if ar == nil {
		return RefuseNoRecord, "there is no ActionRecord to replay: past its retention TTL the CR is deleted and only the exported journal entry remains (05 §1.2)"
	}
	if ar.Status.UndoneBy != "" {
		return RefuseAlreadyUndone, fmt.Sprintf(
			"action %s was already undone by %s; undo it again by undoing that action, which is itself a record with its own plan",
			ar.Spec.ActionID, ar.Status.UndoneBy)
	}
	if !Executed(ar.Status.Phase) {
		return RefuseNotExecuted, fmt.Sprintf(
			"action %s is in phase %q; only a %s action changed the world in a way that is still standing and can be put back",
			ar.Spec.ActionID, ar.Status.Phase, agentv1alpha1.PhaseVerified)
	}
	if err := ValidateReplayable(ar.Spec.Undo); err != nil {
		return RefusePlanUnusable, fmt.Sprintf("action %s cannot be replayed: %v", ar.Spec.ActionID, err)
	}
	if !journal.UndoWindowOpen(ar, now) {
		return RefuseWindowExpired, fmt.Sprintf(
			"the undo window for action %s closed at %s; reconstruct the change from the exported record by hand rather than replaying a pre-state the world has moved past",
			ar.Spec.ActionID, ar.Spec.Retention.UndoWindowExpiresAt.UTC().Format(time.RFC3339))
	}
	return ReplayAllowed, ""
}
