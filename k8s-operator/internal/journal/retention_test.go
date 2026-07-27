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
	"strings"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

var submitted = time.Date(2026, 7, 27, 12, 0, 0, 0, time.UTC)

const day = 24 * time.Hour

// The table is transcribed from 05 §1.2 rather than read out of the implementation, so a change to
// the constants has to be argued for against the design document and not just made.
func TestRetentionForMatchesTheDesignTable(t *testing.T) {
	for _, tc := range []struct {
		class           agentv1alpha1.ActionRiskClass
		ttl, undoWindow time.Duration
	}{
		{agentv1alpha1.RiskRoutine, 30 * day, 7 * day},
		{agentv1alpha1.RiskElevated, 90 * day, 30 * day},
		{agentv1alpha1.RiskGated, 365 * day, 90 * day},
		// A forbidden action never executed, so there is nothing to undo -- but the record of the
		// refusal is kept for a year, because "the agent tried to do this" is exactly the thing an
		// investigator wants and exactly the thing a short TTL would erase.
		{agentv1alpha1.RiskForbidden, 365 * day, 0},
	} {
		t.Run(string(tc.class), func(t *testing.T) {
			got, err := RetentionFor(tc.class, submitted)
			if err != nil {
				t.Fatalf("RetentionFor(%s): %v", tc.class, err)
			}
			if got.Class != tc.class {
				t.Fatalf("Class = %q, want %q", got.Class, tc.class)
			}
			if want := submitted.Add(tc.ttl); !got.ExpiresAt.UTC().Equal(want) {
				t.Fatalf("ExpiresAt = %s, want %s", got.ExpiresAt.UTC(), want)
			}
			if want := submitted.Add(tc.undoWindow); !got.UndoWindowExpiresAt.UTC().Equal(want) {
				t.Fatalf("UndoWindowExpiresAt = %s, want %s", got.UndoWindowExpiresAt.UTC(), want)
			}
			// The CRD's CEL rule enforces this too, but a spec built here that violated it would be
			// rejected at admission -- i.e. the action would fail -- so it is cheaper to be sure the
			// derivation cannot produce one.
			if got.UndoWindowExpiresAt.After(got.ExpiresAt.Time) {
				t.Fatalf("the undo promise outlives the record: %s > %s", got.UndoWindowExpiresAt.UTC(), got.ExpiresAt.UTC())
			}
			if !strings.HasSuffix(got.TTL, "h") || !strings.HasSuffix(got.UndoWindow, "h") {
				t.Fatalf("TTL=%q UndoWindow=%q must match the CRD's ^[0-9]+h$ pattern", got.TTL, got.UndoWindow)
			}
		})
	}
}

func TestRetentionForRefusesAnUnknownClass(t *testing.T) {
	// Defaulting an unknown class would silently give a new, unreviewed risk class the SHORTEST
	// retention in the table -- the one direction that loses evidence.
	if _, err := RetentionFor(agentv1alpha1.ActionRiskClass("catastrophic"), submitted); err == nil {
		t.Fatal("an unknown risk class was given retention clocks instead of an error")
	}
}

func TestReanchorUndoWindowMovesTheClockToExecution(t *testing.T) {
	r, err := RetentionFor(agentv1alpha1.RiskElevated, submitted)
	if err != nil {
		t.Fatalf("RetentionFor: %v", err)
	}
	// A gated action can sit in PendingApproval for hours. Anchoring the undo promise at submission
	// would spend the window waiting for a human.
	ended := submitted.Add(6 * time.Hour)
	got, err := ReanchorUndoWindow(r, ended)
	if err != nil {
		t.Fatalf("ReanchorUndoWindow: %v", err)
	}
	if want := ended.Add(30 * day); !got.UndoWindowExpiresAt.UTC().Equal(want) {
		t.Fatalf("UndoWindowExpiresAt = %s, want %s", got.UndoWindowExpiresAt.UTC(), want)
	}
	if !got.ExpiresAt.Equal(&r.ExpiresAt) {
		t.Fatalf("re-anchoring moved the record TTL as well: %s", got.ExpiresAt.UTC())
	}
}

func TestReanchorUndoWindowClampsToTheRecordTTL(t *testing.T) {
	// Gated is 365d/90d, so an execution that ends within 90 days of the TTL would otherwise produce
	// a promise that outlives the record -- which the CRD rejects. Clamping turns a write rejection
	// at the end of a long approval into a slightly shorter window.
	r, err := RetentionFor(agentv1alpha1.RiskGated, submitted)
	if err != nil {
		t.Fatalf("RetentionFor: %v", err)
	}
	ended := submitted.Add(300 * day)
	got, err := ReanchorUndoWindow(r, ended)
	if err != nil {
		t.Fatalf("ReanchorUndoWindow: %v", err)
	}
	if !got.UndoWindowExpiresAt.UTC().Equal(r.ExpiresAt.UTC()) {
		t.Fatalf("UndoWindowExpiresAt = %s, want it clamped to ExpiresAt %s",
			got.UndoWindowExpiresAt.UTC(), r.ExpiresAt.UTC())
	}
}

func TestLengthenTTLIsStricterOnly(t *testing.T) {
	r, err := RetentionFor(agentv1alpha1.RiskRoutine, submitted)
	if err != nil {
		t.Fatalf("RetentionFor: %v", err)
	}

	longer, err := LengthenTTL(r, 90*day, submitted)
	if err != nil {
		t.Fatalf("LengthenTTL: %v", err)
	}
	if want := submitted.Add(90 * day); !longer.ExpiresAt.UTC().Equal(want) {
		t.Fatalf("a longer TTL was not applied: ExpiresAt = %s, want %s", longer.ExpiresAt.UTC(), want)
	}

	// A ChangePolicy that could shorten retention would be a supported, self-service way to delete
	// evidence early -- so a shorter value is ignored rather than rejected, and the caller does not
	// get an error it might be tempted to handle by applying the value anyway.
	shorter, err := LengthenTTL(r, 1*day, submitted)
	if err != nil {
		t.Fatalf("LengthenTTL: %v", err)
	}
	if !shorter.ExpiresAt.UTC().Equal(r.ExpiresAt.UTC()) {
		t.Fatalf("a policy shortened retention from %s to %s", r.ExpiresAt.UTC(), shorter.ExpiresAt.UTC())
	}
	if shorter.TTL != r.TTL {
		t.Fatalf("TTL string was shortened to %q from %q", shorter.TTL, r.TTL)
	}
}

func TestUndoWindowOpen(t *testing.T) {
	ar := &agentv1alpha1.ActionRecord{}
	r, err := RetentionFor(agentv1alpha1.RiskRoutine, submitted)
	if err != nil {
		t.Fatalf("RetentionFor: %v", err)
	}
	ar.Spec.Retention = r

	if !UndoWindowOpen(ar, submitted.Add(6*day)) {
		t.Fatal("the undo window was closed inside the promised 7 days")
	}
	if UndoWindowOpen(ar, submitted.Add(8*day)) {
		t.Fatal("the undo window was open past its expiry; a stale snapshot would restore the wrong world (06 §4.3)")
	}
}

func TestTerminalCoversEveryEndState(t *testing.T) {
	terminal := []agentv1alpha1.ActionPhase{
		agentv1alpha1.PhaseVerified,
		agentv1alpha1.PhaseFailed,
		agentv1alpha1.PhaseRolledBack,
		agentv1alpha1.PhaseUndone,
		agentv1alpha1.PhaseRejected,
		agentv1alpha1.PhaseExpired,
		agentv1alpha1.PhaseDryRun,
	}
	for _, p := range terminal {
		if !Terminal(p) {
			t.Fatalf("%s is an end state but Terminal says otherwise; such a record would never export and never be deleted", p)
		}
	}
	// The inverse matters more: a phase wrongly called terminal is a record exported and then
	// deleted while the action is still running.
	for _, p := range []agentv1alpha1.ActionPhase{
		agentv1alpha1.PhasePending,
		agentv1alpha1.PhasePendingApproval,
		agentv1alpha1.PhaseExecuting,
		agentv1alpha1.ActionPhase(""),
		agentv1alpha1.ActionPhase("Whatever"),
	} {
		if Terminal(p) {
			t.Fatalf("%q was treated as terminal; an in-flight action would be garbage-collected out from under itself", p)
		}
	}
}

// DeletableAt is a three-way AND and each conjunct is a different disaster if dropped, so each is
// removed in turn rather than tested only in combination.
func TestDeletableAtRequiresAllThreeConditions(t *testing.T) {
	expired := submitted.Add(31 * day)

	base := func() *agentv1alpha1.ActionRecord {
		r, err := RetentionFor(agentv1alpha1.RiskRoutine, submitted)
		if err != nil {
			t.Fatalf("RetentionFor: %v", err)
		}
		at := metav1.NewTime(submitted)
		return &agentv1alpha1.ActionRecord{
			Spec: agentv1alpha1.ActionRecordSpec{Retention: r},
			Status: agentv1alpha1.ActionRecordStatus{
				Phase:    agentv1alpha1.PhaseVerified,
				Exported: &agentv1alpha1.ExportStatus{Confirmed: true, At: &at, Sink: "test"},
			},
		}
	}

	t.Run("all three hold", func(t *testing.T) {
		if ok, why := DeletableAt(base(), expired); !ok {
			t.Fatalf("a terminal, exported, expired record was retained: %s", why)
		}
	})

	t.Run("not terminal", func(t *testing.T) {
		ar := base()
		ar.Status.Phase = agentv1alpha1.PhaseExecuting
		ok, why := DeletableAt(ar, expired)
		if ok {
			t.Fatal("an Executing record was deleted; the action is still running and would lose its journal")
		}
		if !strings.Contains(why, "terminal") {
			t.Fatalf("reason %q does not name the terminal condition", why)
		}
	})

	t.Run("not exported", func(t *testing.T) {
		for _, tc := range []struct {
			name string
			set  func(*agentv1alpha1.ActionRecord)
		}{
			{"no export status at all", func(ar *agentv1alpha1.ActionRecord) { ar.Status.Exported = nil }},
			{"export attempted but unconfirmed", func(ar *agentv1alpha1.ActionRecord) {
				ar.Status.Exported = &agentv1alpha1.ExportStatus{Confirmed: false, Sink: "test"}
			}},
		} {
			t.Run(tc.name, func(t *testing.T) {
				ar := base()
				tc.set(ar)
				ok, why := DeletableAt(ar, expired)
				if ok {
					t.Fatal("an unexported record was deleted past its TTL; the export IS the durable record, so this is data loss on a schedule (05 §1.2)")
				}
				if !strings.Contains(why, "export") {
					t.Fatalf("reason %q does not name the export condition, so an operator watching records pile up cannot tell a stuck exporter from a long TTL", why)
				}
			})
		}
	})

	t.Run("not yet expired", func(t *testing.T) {
		if ok, _ := DeletableAt(base(), submitted.Add(29*day)); ok {
			t.Fatal("a record was deleted before its TTL elapsed")
		}
		// Exactly at expiry is not yet past it. Off-by-one here deletes a day early, every time.
		r, err := RetentionFor(agentv1alpha1.RiskRoutine, submitted)
		if err != nil {
			t.Fatalf("RetentionFor: %v", err)
		}
		if ok, _ := DeletableAt(base(), r.ExpiresAt.UTC()); ok {
			t.Fatal("a record was deleted at the instant of expiry rather than after it")
		}
	})
}
