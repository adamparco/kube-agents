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
	"strings"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// Every test here is shaped the same way and for the same reason as brake_test.go: a healthy
// baseline that is ALLOWED, and then one field spoiled at a time. A suite of refusals alone would
// pass against a predicate that refuses everything, which is a brake that is stuck on -- and a
// stuck-on undo brake looks exactly like a working one until the day somebody needs an undo.

var replayNow = time.Date(2026, 7, 27, 12, 0, 0, 0, time.UTC)

// replayable builds the healthy baseline: verified, never undone, a validated one-step plan, and an
// undo window that closes in an hour.
func replayable() *agentv1alpha1.ActionRecord {
	return &agentv1alpha1.ActionRecord{
		Spec: agentv1alpha1.ActionRecordSpec{
			ActionID: "01J0000000000000000000000A",
			Undo: &agentv1alpha1.UndoPlan{
				Strategy:    agentv1alpha1.UndoRestore,
				GeneratedAt: metav1.NewTime(replayNow.Add(-time.Minute)),
				Validated:   true,
				Steps: []agentv1alpha1.UndoStep{{
					Op:     "apply",
					Target: agentv1alpha1.TargetRef{Group: "apps", Version: "v1", Kind: "Deployment", Namespace: "team-x", Name: "api"},
					Object: &runtime.RawExtension{Raw: []byte(`{"apiVersion":"apps/v1","kind":"Deployment"}`)},
				}},
			},
			Retention: agentv1alpha1.RetentionSpec{
				UndoWindowExpiresAt: metav1.NewTime(replayNow.Add(time.Hour)),
			},
		},
		Status: agentv1alpha1.ActionRecordStatus{
			Phase: agentv1alpha1.PhaseVerified,
		},
	}
}

func TestReplayableBaselineIsAllowed(t *testing.T) {
	refusal, detail := Replayable(replayable(), replayNow)
	if refusal != ReplayAllowed {
		t.Fatalf("the healthy baseline must be replayable; got %q: %s", refusal, detail)
	}
	if detail != "" {
		t.Errorf("an allowed verdict carries no detail; got %q", detail)
	}
}

func TestReplayableZeroValueIsRefused(t *testing.T) {
	// A zero-valued record has no phase, no plan, and a zero undo window. Every one of those is a
	// refusal, and the predicate must not need any of them to be explicitly negative.
	refusal, detail := Replayable(&agentv1alpha1.ActionRecord{}, replayNow)
	if refusal == ReplayAllowed {
		t.Fatal("a zero-valued ActionRecord must not be replayable: absence is not consent")
	}
	if detail == "" {
		t.Error("a refusal must carry a detail; an operator cannot act on a bare reason code")
	}
}

func TestReplayableEachRefusalFiresInIsolation(t *testing.T) {
	cases := []struct {
		name    string
		spoil   func(*agentv1alpha1.ActionRecord)
		want    ReplayRefusal
		wantSub string
	}{
		{"nil record", nil, RefuseNoRecord, "exported journal"},
		{
			"already undone",
			func(ar *agentv1alpha1.ActionRecord) { ar.Status.UndoneBy = "01J0000000000000000000000B" },
			RefuseAlreadyUndone, "01J0000000000000000000000B",
		},
		{
			"phase pending",
			func(ar *agentv1alpha1.ActionRecord) { ar.Status.Phase = agentv1alpha1.PhasePending },
			RefuseNotExecuted, "Pending",
		},
		{
			"phase failed",
			func(ar *agentv1alpha1.ActionRecord) { ar.Status.Phase = agentv1alpha1.PhaseFailed },
			RefuseNotExecuted, "Failed",
		},
		{
			"phase rolled back",
			func(ar *agentv1alpha1.ActionRecord) { ar.Status.Phase = agentv1alpha1.PhaseRolledBack },
			RefuseNotExecuted, "RolledBack",
		},
		{
			"phase dry run",
			func(ar *agentv1alpha1.ActionRecord) { ar.Status.Phase = agentv1alpha1.PhaseDryRun },
			RefuseNotExecuted, "DryRun",
		},
		{
			"phase rejected",
			func(ar *agentv1alpha1.ActionRecord) { ar.Status.Phase = agentv1alpha1.PhaseRejected },
			RefuseNotExecuted, "Rejected",
		},
		{
			"phase expired",
			func(ar *agentv1alpha1.ActionRecord) { ar.Status.Phase = agentv1alpha1.PhaseExpired },
			RefuseNotExecuted, "Expired",
		},
		{
			"phase undone without linkage",
			func(ar *agentv1alpha1.ActionRecord) { ar.Status.Phase = agentv1alpha1.PhaseUndone },
			RefuseNotExecuted, "Undone",
		},
		{
			"no plan at all",
			func(ar *agentv1alpha1.ActionRecord) { ar.Spec.Undo = nil },
			RefusePlanUnusable, "no undo plan",
		},
		{
			"plan never dry-run",
			func(ar *agentv1alpha1.ActionRecord) { ar.Spec.Undo.Validated = false },
			RefusePlanUnusable, "never dry-run",
		},
		{
			"strategy none",
			func(ar *agentv1alpha1.ActionRecord) { ar.Spec.Undo.Strategy = agentv1alpha1.UndoNone },
			RefusePlanUnusable, "not undoable",
		},
		{
			"window closed",
			func(ar *agentv1alpha1.ActionRecord) {
				ar.Spec.Retention.UndoWindowExpiresAt = metav1.NewTime(replayNow.Add(-time.Second))
			},
			RefuseWindowExpired, "closed at",
		},
		{
			"window unset",
			func(ar *agentv1alpha1.ActionRecord) {
				ar.Spec.Retention.UndoWindowExpiresAt = metav1.Time{}
			},
			RefuseWindowExpired, "closed at",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var ar *agentv1alpha1.ActionRecord
			if tc.spoil != nil {
				ar = replayable()
				tc.spoil(ar)
			}
			refusal, detail := Replayable(ar, replayNow)
			if refusal != tc.want {
				t.Fatalf("refusal = %q, want %q (detail: %s)", refusal, tc.want, detail)
			}
			if !strings.Contains(detail, tc.wantSub) {
				t.Errorf("detail %q does not mention %q", detail, tc.wantSub)
			}
		})
	}
}

// The order of the checks is a contract, not an implementation detail: a record that is BOTH
// already-undone and out of window must report already-undone, because telling somebody their undo
// window expired sends them looking for a time problem that is not the problem.
func TestReplayableReportsTheMostSpecificReason(t *testing.T) {
	ar := replayable()
	ar.Status.UndoneBy = "01J0000000000000000000000B"
	ar.Status.Phase = agentv1alpha1.PhaseUndone
	ar.Spec.Undo = nil
	ar.Spec.Retention.UndoWindowExpiresAt = metav1.NewTime(replayNow.Add(-time.Hour))
	if refusal, _ := Replayable(ar, replayNow); refusal != RefuseAlreadyUndone {
		t.Fatalf("with every condition spoiled the reason must be %q; got %q", RefuseAlreadyUndone, refusal)
	}

	ar.Status.UndoneBy = ""
	ar.Status.Phase = agentv1alpha1.PhasePending
	if refusal, _ := Replayable(ar, replayNow); refusal != RefuseNotExecuted {
		t.Fatalf("phase must outrank the plan and the window; got %q", refusal)
	}

	ar.Status.Phase = agentv1alpha1.PhaseVerified
	if refusal, _ := Replayable(ar, replayNow); refusal != RefusePlanUnusable {
		t.Fatalf("the plan must outrank the window; got %q", refusal)
	}
}

// The window boundary is closed at the instant of expiry: `now == expiresAt` is expired. An undo
// promise that held at exactly its deadline would be a promise with no deadline.
func TestReplayableWindowBoundaryIsClosed(t *testing.T) {
	ar := replayable()
	expiry := ar.Spec.Retention.UndoWindowExpiresAt.Time

	if refusal, _ := Replayable(ar, expiry.Add(-time.Nanosecond)); refusal != ReplayAllowed {
		t.Errorf("one nanosecond before expiry the undo is still promised; got %q", refusal)
	}
	if refusal, _ := Replayable(ar, expiry); refusal != RefuseWindowExpired {
		t.Errorf("at the instant of expiry the window is closed; got %q", refusal)
	}
	if refusal, _ := Replayable(ar, expiry.Add(time.Nanosecond)); refusal != RefuseWindowExpired {
		t.Errorf("after expiry the window is closed; got %q", refusal)
	}
}

// Executed is deliberately narrower than journal.Terminal, and this pins the difference. If somebody
// later "simplifies" Executed to call journal.Terminal, four phases that never landed become
// undoable and undoing a DryRun would apply a snapshot of a change that was never made.
func TestExecutedIsNarrowerThanTerminal(t *testing.T) {
	undoable := map[agentv1alpha1.ActionPhase]bool{
		agentv1alpha1.PhaseVerified: true,
	}
	all := []agentv1alpha1.ActionPhase{
		agentv1alpha1.PhasePending, agentv1alpha1.PhasePendingApproval, agentv1alpha1.PhaseExecuting,
		agentv1alpha1.PhaseVerified, agentv1alpha1.PhaseFailed, agentv1alpha1.PhaseRolledBack,
		agentv1alpha1.PhaseUndone, agentv1alpha1.PhaseRejected, agentv1alpha1.PhaseExpired,
		agentv1alpha1.PhaseDryRun,
	}
	for _, p := range all {
		if got := Executed(p); got != undoable[p] {
			t.Errorf("Executed(%q) = %v, want %v", p, got, undoable[p])
		}
	}
	if Executed("") {
		t.Error("the empty phase must not be executed: a record with no phase has not finished anything")
	}
}

func TestReplayRefusalString(t *testing.T) {
	if got := ReplayAllowed.String(); got != "allowed" {
		t.Errorf("ReplayAllowed.String() = %q, want %q", got, "allowed")
	}
	if got := RefuseWindowExpired.String(); got != "undo-window-expired" {
		t.Errorf("RefuseWindowExpired.String() = %q", got)
	}
}
