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

package journal

import (
	"fmt"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// TWO clocks, and the whole reason this file exists is that they are constantly conflated (05 §1.2,
// 06 §4.3):
//
//	ttl        -- an EVIDENCE horizon. How long the CR survives in etcd.
//	undoWindow -- a REVERSIBILITY horizon. How long undo is promised, always shorter.
//
// Outside the undo window the record may still exist and still carry a valid plan; the undo
// controller simply refuses the one-command path, because a snapshot goes stale and replaying a
// three-month-old restore would confidently rebuild a world that no longer exists. Keeping the
// evidence while withdrawing the promise is the honest arrangement, and it only works if the two
// numbers are separate.

// retentionClocks is the 05 §1.2 table, verbatim. A Rejected record has NO undo window -- nothing
// executed -- and is retained the longest anyway, because a refusal is security evidence and
// short-lived security evidence is worthless.
var retentionClocks = map[agentv1alpha1.ActionRiskClass]struct {
	ttl        time.Duration
	undoWindow time.Duration
}{
	agentv1alpha1.RiskRoutine:  {ttl: 30 * 24 * time.Hour, undoWindow: 7 * 24 * time.Hour},
	agentv1alpha1.RiskElevated: {ttl: 90 * 24 * time.Hour, undoWindow: 30 * 24 * time.Hour},
	agentv1alpha1.RiskGated:    {ttl: 365 * 24 * time.Hour, undoWindow: 90 * 24 * time.Hour},
	// forbidden never executes, so the record is a Rejected one: longest evidence, no undo promise.
	agentv1alpha1.RiskForbidden: {ttl: 365 * 24 * time.Hour, undoWindow: 0},
}

// hoursOf renders a duration in the `NNNh` form the CRD's pattern accepts. Days would read better
// and Go's duration parser does not have them, so a round-trip through time.ParseDuration would
// fail on the nicer string -- and a retention field the controller cannot parse back is worse than
// an unlovely one.
func hoursOf(d time.Duration) string { return fmt.Sprintf("%dh", int64(d/time.Hour)) }

// RetentionFor derives both clocks for a class, anchored at submitted.
//
// undoWindowExpiresAt is anchored at submitted here because at classification time the action has
// not executed. The broker re-anchors it to executionEnded once it has one (06 §4.3); until then,
// submitted is the conservative choice -- it can only make the promise shorter, never longer.
func RetentionFor(class agentv1alpha1.ActionRiskClass, submitted time.Time) (agentv1alpha1.RetentionSpec, error) {
	c, ok := retentionClocks[class]
	if !ok {
		// An unknown class must not fall back to a default. Defaulting here would give an
		// unclassifiable action the mildest retention available, which is precisely backwards.
		return agentv1alpha1.RetentionSpec{}, fmt.Errorf("journal: no retention clocks for risk class %q", class)
	}
	submitted = submitted.UTC()
	return agentv1alpha1.RetentionSpec{
		Class:               class,
		TTL:                 hoursOf(c.ttl),
		ExpiresAt:           metav1.NewTime(submitted.Add(c.ttl)),
		UndoWindow:          hoursOf(c.undoWindow),
		UndoWindowExpiresAt: metav1.NewTime(submitted.Add(c.undoWindow)),
	}, nil
}

// ReanchorUndoWindow moves the undo promise to start at executionEnded, which is when the change
// actually landed. It clamps to expiresAt: the CRD refuses a promise that outlives the record, and
// clamping here means a long-running gated action gets a slightly shortened window rather than a
// write rejection at the worst possible moment.
func ReanchorUndoWindow(r agentv1alpha1.RetentionSpec, executionEnded time.Time) (agentv1alpha1.RetentionSpec, error) {
	window, err := time.ParseDuration(r.UndoWindow)
	if err != nil {
		return r, fmt.Errorf("journal: parse undoWindow %q: %w", r.UndoWindow, err)
	}
	end := executionEnded.UTC().Add(window)
	if end.After(r.ExpiresAt.Time) {
		end = r.ExpiresAt.Time
	}
	r.UndoWindowExpiresAt = metav1.NewTime(end)
	return r, nil
}

// LengthenTTL applies a ChangePolicy's retention override. A policy may only make the evidence
// horizon LONGER -- stricter-only in the audit direction (06 §4.3). A shorter value is not an error
// the caller has to handle; it is simply ignored, because a policy that could shorten retention
// would be a supported way to delete evidence early.
func LengthenTTL(r agentv1alpha1.RetentionSpec, ttl time.Duration, submitted time.Time) (agentv1alpha1.RetentionSpec, error) {
	current, err := time.ParseDuration(r.TTL)
	if err != nil {
		return r, fmt.Errorf("journal: parse ttl %q: %w", r.TTL, err)
	}
	if ttl <= current {
		return r, nil
	}
	r.TTL = hoursOf(ttl)
	r.ExpiresAt = metav1.NewTime(submitted.UTC().Add(ttl))
	return r, nil
}

// UndoWindowOpen reports whether the one-command undo path is still promised at `now`. The undo
// controller REFUSES past this, and refusing is not the same as erroring: the record is still there
// and a human may reconstruct the change from the export sink by hand. The system just stops
// claiming one command will do it correctly.
func UndoWindowOpen(ar *agentv1alpha1.ActionRecord, now time.Time) bool {
	return now.UTC().Before(ar.Spec.Retention.UndoWindowExpiresAt.Time)
}

// Terminal reports whether a phase can no longer change. Only terminal records are eligible for
// retention deletion; deleting a record mid-flight would strand an executing action with no journal
// and force the broker to fail closed on its own bookkeeping.
func Terminal(phase agentv1alpha1.ActionPhase) bool {
	switch phase {
	case agentv1alpha1.PhaseVerified,
		agentv1alpha1.PhaseFailed,
		agentv1alpha1.PhaseRolledBack,
		agentv1alpha1.PhaseUndone,
		agentv1alpha1.PhaseRejected,
		agentv1alpha1.PhaseExpired,
		agentv1alpha1.PhaseDryRun:
		return true
	default:
		return false
	}
}

// DeletableAt is the retention controller's deletion predicate, in one place so the two conditions
// cannot drift apart.
//
// BOTH must hold. Past-TTL alone is not enough: the EXPORT is the durable record (05 §1.2), so
// deleting a CR the exporter has not yet confirmed destroys the evidence rather than aging it out --
// which is the difference between garbage collection and data loss, and it looks identical from
// inside the controller. The reason is returned so a controller log line can say WHY a record was
// kept, and an operator watching records pile up can tell a stuck exporter from a long TTL.
func DeletableAt(ar *agentv1alpha1.ActionRecord, now time.Time) (bool, string) {
	if !Terminal(ar.Status.Phase) {
		return false, fmt.Sprintf("phase %q is not terminal", ar.Status.Phase)
	}
	if ar.Status.Exported == nil || !ar.Status.Exported.Confirmed {
		return false, "the audit exporter has not confirmed this record; the export is the durable record (05 §1.2)"
	}
	if !now.UTC().After(ar.Spec.Retention.ExpiresAt.Time) {
		return false, fmt.Sprintf("retention.expiresAt %s has not passed", ar.Spec.Retention.ExpiresAt.UTC().Format(time.RFC3339))
	}
	return true, ""
}
