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

package verify

import (
	"strings"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

var base = time.Date(2026, 7, 27, 12, 0, 0, 0, time.UTC)

func at(min int) time.Time { return base.Add(time.Duration(min) * time.Minute) }

// tr builds a transition for the hand-authored histories the auditor is tested against. These are
// written directly rather than produced by Climb on purpose: the auditor's whole reason to exist is
// reading a `status.recovery` that some other writer produced.
func tr(from, to int32, min int, reason string) agentv1alpha1.RecoveryTransition {
	return agentv1alpha1.RecoveryTransition{
		At: metav1.NewTime(at(min)), From: from, To: to, Reason: reason,
	}
}

func recoveryOf(rung int32, ts ...agentv1alpha1.RecoveryTransition) *agentv1alpha1.ActionRecovery {
	return &agentv1alpha1.ActionRecovery{Rung: rung, Transitions: ts}
}

// --- V-PRO-021, rule by rule, each with its negative control -----------------------------------

func TestLadderRecordsEveryMovement(t *testing.T) {
	l := NewLadder()
	if l.Rung() != RungNone {
		t.Fatalf("a new ladder starts at rung %d, want 0", l.Rung())
	}
	if err := l.Climb(RungRetry, at(1), "conflict"); err != nil {
		t.Fatalf("climb to 1: %v", err)
	}
	if err := l.Climb(RungAlternative, at(2), "retry exhausted"); err != nil {
		t.Fatalf("climb to 2: %v", err)
	}
	if err := l.Climb(RungRollback, at(3), "terminal"); err != nil {
		t.Fatalf("climb to 3: %v", err)
	}

	r := l.Recovery()
	if r.Rung != RungRollback {
		t.Errorf("rung = %d, want 3", r.Rung)
	}
	if len(r.Transitions) != 3 {
		t.Fatalf("%d transitions recorded, want 3 — a rung reached without a transition is a rung "+
			"climbed silently", len(r.Transitions))
	}
	for i, want := range []int32{RungRetry, RungAlternative, RungRollback} {
		if r.Transitions[i].To != want {
			t.Errorf("transition %d ends at %d, want %d", i, r.Transitions[i].To, want)
		}
	}
	if err := ValidateRecovery(&r); err != nil {
		t.Fatalf("a ladder built by Climb does not validate: %v", err)
	}
}

func TestLadderRefusesDescent(t *testing.T) {
	l := NewLadder()
	mustClimb(t, l, RungRollback, at(1), "terminal failure")

	if err := l.Climb(RungRetry, at(2), "let us try again"); err == nil {
		t.Fatal("descending from rung 3 to rung 1 was accepted")
	}
	if err := l.Climb(RungAlternative, at(2), "an alternative"); err == nil {
		t.Fatal("descending from rung 3 to rung 2 was accepted")
	}
	if l.Rung() != RungRollback {
		t.Errorf("a refused climb moved the ladder to %d; a refusal must not mutate", l.Rung())
	}
	if n := len(l.Recovery().Transitions); n != 1 {
		t.Errorf("a refused climb appended a transition (%d total)", n)
	}
}

func TestLadderRefusesAMoveToItself(t *testing.T) {
	// Non-decreasing alone would permit 2 -> 2, and permitting it would silently undo the
	// one-alternative bound: two self-transitions at rung 2 are two alternatives.
	l := NewLadder()
	mustClimb(t, l, RungAlternative, at(1), "first alternative")
	if err := l.Climb(RungAlternative, at(2), "second alternative"); err == nil {
		t.Fatal("2 -> 2 was accepted, which is a second alternative wearing the first one's rung")
	}

	l2 := NewLadder()
	mustClimb(t, l2, RungRetry, at(1), "conflict")
	if err := l2.Climb(RungRetry, at(2), "another conflict"); err == nil {
		t.Fatal("1 -> 1 was accepted; repeated retries are one visit to rung 1")
	}
}

func TestLadderRequiresAReasonWhenARungIsSkipped(t *testing.T) {
	// 04 §5: "It never skips a rung silently." The requirement is on the RECORD, not the movement --
	// skipping straight to rollback is the normal terminal path and must stay legal.
	l := NewLadder()
	if err := l.Climb(RungRollback, at(1), ""); err == nil {
		t.Fatal("0 -> 3 with no reason was accepted")
	} else if !strings.Contains(err.Error(), "reason") {
		t.Errorf("the error does not say a reason is required: %v", err)
	}
	if err := l.Climb(RungRollback, at(1), "verification terminal: QuotaExhausted"); err != nil {
		t.Fatalf("0 -> 3 WITH a reason was refused: %v — the skip is legal, the silence is not", err)
	}

	// The paired negative control for the rule itself: a single-rung move needs no reason, so a
	// version of this check that demanded one everywhere would be caught here.
	l2 := NewLadder()
	if err := l2.Climb(RungRetry, at(1), ""); err != nil {
		t.Fatalf("0 -> 1 with no reason was refused: %v", err)
	}
}

func TestLadderAllowsExactlyOneAlternative(t *testing.T) {
	// Rung 2 is "bounded: one alternative, not a search". The only way back to 2 after leaving it
	// would be a descent, which is already refused -- so the interesting case is reaching it twice
	// through a legal-looking path, which is what this asserts cannot exist.
	l := NewLadder()
	mustClimb(t, l, RungAlternative, at(1), "first alternative")
	mustClimb(t, l, RungRollback, at(2), "alternative failed")

	// And the auditor must reject a hand-written history that does contain two.
	twice := recoveryOf(RungAlternative,
		tr(0, 2, 1, "first"),
		tr(2, 3, 2, "failed"),
		tr(3, 2, 3, "second"), // also a descent; the rung-2 rule must fire regardless
	)
	if err := ValidateRecovery(twice); err == nil {
		t.Fatal("a history entering rung 2 twice validated")
	}
}

// TestLadderPropertiesHoldOverEveryAcceptedHistory is the real V-PRO-021 mechanization.
//
// It exists because mutation testing found that the two rules 04 §5 names most explicitly -- "at
// most one alternative" and "never restarts at rung 1 after a rollback" -- can be deleted from
// checkTransition with every other test still green. They are not enforcing anything on their own:
// monotonicity plus the no-self-transition rule already make each rung enterable at most once, so
// the two special cases are implied. Poking at either `if` therefore proves nothing.
//
// What matters is the PROPERTY, not which line enforces it. This enumerates every history up to
// length four over the six rungs, and asserts that no history ValidateRecovery accepts violates
// either one. Delete monotonicity, or the no-movement rule, and this fails.
func TestLadderPropertiesHoldOverEveryAcceptedHistory(t *testing.T) {
	rungs := []int32{0, 1, 2, 3, 4, 5}

	var histories [][]int32
	var build func(prefix []int32, depth int)
	build = func(prefix []int32, depth int) {
		if len(prefix) > 0 {
			h := make([]int32, len(prefix))
			copy(h, prefix)
			histories = append(histories, h)
		}
		if depth == 0 {
			return
		}
		for _, r := range rungs {
			// A fresh slice per branch: append() reuses its backing array, and a shared one would
			// let a later branch rewrite a history this walk already recorded.
			next := make([]int32, len(prefix), len(prefix)+1)
			copy(next, prefix)
			build(append(next, r), depth-1)
		}
	}
	build(nil, 4)

	accepted := 0
	for _, h := range histories {
		// A reason on every transition, so the skip rule never fires and the movement rules are
		// what is being observed.
		rec := &agentv1alpha1.ActionRecovery{}
		cur := RungNone
		for i, to := range h {
			rec.Transitions = append(rec.Transitions, tr(cur, to, i+1, "reason"))
			cur = to
		}
		rec.Rung = cur
		if ValidateRecovery(rec) != nil {
			continue
		}
		accepted++

		alternatives, rolledBack := 0, false
		prev := RungNone
		for i, tn := range rec.Transitions {
			if tn.To <= prev {
				t.Errorf("history %v: transition %d moves %d -> %d, which is not an ascent",
					h, i, prev, tn.To)
			}
			if tn.To == RungAlternative {
				alternatives++
			}
			if tn.To >= RungRollback {
				rolledBack = true
			}
			if tn.To == RungRetry && rolledBack {
				t.Errorf("history %v: accepted a return to rung 1 after a rollback", h)
			}
			prev = tn.To
		}
		if alternatives > 1 {
			t.Errorf("history %v: accepted %d visits to rung 2; 04 §5 bounds this to one",
				h, alternatives)
		}
	}

	// The control on the test itself, and the tightest statement of what the ladder permits: a
	// ValidateRecovery that rejected everything would satisfy every assertion above vacuously.
	//
	// The accepted set is exactly the strictly-ascending sequences over rungs 1..5 -- rung 0 is
	// never a destination, since the only way to arrive at it is from itself. Up to length four
	// that is C(5,1)+C(5,2)+C(5,3)+C(5,4) = 5+10+10+5 = 30. A change that loosens the ladder makes
	// this larger; one that over-tightens it makes it smaller. Both are worth failing on.
	const wantAccepted = 30
	if accepted != wantAccepted {
		t.Fatalf("%d of %d histories accepted, want exactly %d (the strictly-ascending sequences "+
			"over rungs 1..5 of length <= 4)", accepted, len(histories), wantAccepted)
	}
}

func TestLadderNeverRestartsAtRungOneAfterARollback(t *testing.T) {
	// Within one record this is implied by monotonicity, and it is asserted here anyway because the
	// auditor reads records it did not build.
	after := recoveryOf(RungRetry,
		tr(0, 3, 1, "terminal"),
		tr(3, 1, 2, "trying again"),
	)
	err := ValidateRecovery(after)
	if err == nil {
		t.Fatal("a history returning to rung 1 after a rollback validated")
	}

	// The load-bearing half is CROSS-record and lives in the cooldown, because the next action gets
	// a fresh ladder that legitimately starts at rung 0. TestCooldownStopsTheNextActionRestarting
	// is that half; this test would pass with the cooldown deleted, which is why both exist.
}

// --- the auditor, on histories no producer built -----------------------------------------------

func TestValidateRecoveryAcceptsNil(t *testing.T) {
	if err := ValidateRecovery(nil); err != nil {
		t.Fatalf("an action that never needed recovery has none: %v", err)
	}
}

func TestValidateRecoveryCatchesABrokenChain(t *testing.T) {
	// Transition 1 starts at rung 3 having ended at rung 1: something moved and was not recorded.
	broken := recoveryOf(RungPage,
		tr(0, 1, 1, "conflict"),
		tr(3, 5, 2, "page"),
	)
	if err := ValidateRecovery(broken); err == nil {
		t.Fatal("a history with a gap in the chain validated")
	}
}

func TestValidateRecoveryCatchesTheSummaryDisagreeingWithTheHistory(t *testing.T) {
	// `rung` is what an operator's dashboard reads and the transitions are what an auditor reads.
	// A record where they disagree tells two different stories depending on which field you trust.
	lying := recoveryOf(RungRetry, tr(0, 3, 1, "terminal"))
	err := ValidateRecovery(lying)
	if err == nil {
		t.Fatal("recovery.rung=1 over a history ending at rung 3 validated")
	}
	if !strings.Contains(err.Error(), "disagree") {
		t.Errorf("the error does not name the disagreement: %v", err)
	}
}

func TestValidateRecoveryRequiresTimestamps(t *testing.T) {
	noTime := &agentv1alpha1.ActionRecovery{
		Rung:        RungRollback,
		Transitions: []agentv1alpha1.RecoveryTransition{{From: 0, To: 3, Reason: "terminal"}},
	}
	if err := ValidateRecovery(noTime); err == nil {
		t.Fatal("a transition with no timestamp validated; 'when' is half of an audit record")
	}
}

func TestValidateRecoveryRequiresTimeToMoveForward(t *testing.T) {
	backwards := recoveryOf(RungRollback,
		tr(0, 1, 5, "conflict"),
		tr(1, 3, 2, "terminal"),
	)
	if err := ValidateRecovery(backwards); err == nil {
		t.Fatal("a history whose second transition predates its first validated")
	}
}

func TestValidateRecoveryRejectsARungOffTheLadder(t *testing.T) {
	for _, rung := range []int32{6, 42, -1} {
		off := recoveryOf(rung, tr(0, rung, 1, "somewhere else entirely"))
		if err := ValidateRecovery(off); err == nil {
			t.Errorf("rung %d validated; the 04 §5 ladder has five rungs", rung)
		}
	}
}

// TestProducerAndAuditorAgree is the invariant that keeps the two halves from drifting.
//
// Every rule is written once in checkTransition and read by both Climb and ValidateRecovery, but
// "written once" is a property of today's code. This test asserts the OBSERVABLE version: for every
// history below, the producer accepting it and the auditor validating it are the same answer. A
// future refactor that gives either side its own copy of a rule fails here rather than in
// production, where the shape of the failure is a record that was legal to write and is not legal
// to read.
func TestProducerAndAuditorAgree(t *testing.T) {
	type step struct {
		to     int32
		min    int
		reason string
	}
	histories := [][]step{
		{},
		{{RungRetry, 1, "conflict"}},
		{{RungRetry, 1, "conflict"}, {RungAlternative, 2, "retry exhausted"}},
		{{RungRollback, 1, "terminal"}},
		{{RungRollback, 1, ""}},                                  // illegal: silent skip
		{{RungRetry, 1, ""}, {RungRetry, 2, ""}},                 // illegal: no movement
		{{RungRollback, 1, "terminal"}, {RungRetry, 2, "again"}}, // illegal: descent
		{{RungAlternative, 1, "alt"}, {RungRollback, 2, "failed"}, {RungPage, 3, "rollback failed"}},
		{{RungPage, 1, "nothing in scope can fix it"}},
		{{RungRetry, 1, "conflict"}, {RungPage, 2, ""}}, // illegal: silent skip 1 -> 5
	}

	for i, h := range histories {
		l := NewLadder()
		producerOK := true
		for _, s := range h {
			if err := l.Climb(s.to, at(s.min), s.reason); err != nil {
				producerOK = false
				break
			}
		}
		// Build the same history by hand so the auditor sees what a rogue writer would have stored.
		hand := &agentv1alpha1.ActionRecovery{}
		cur := RungNone
		for _, s := range h {
			hand.Transitions = append(hand.Transitions, tr(cur, s.to, s.min, s.reason))
			cur = s.to
		}
		hand.Rung = cur
		auditorOK := ValidateRecovery(hand) == nil

		if producerOK != auditorOK {
			t.Errorf("history %d: Climb accepted=%v but ValidateRecovery accepted=%v — "+
				"the two halves disagree about the same record", i, producerOK, auditorOK)
		}
	}
}

func TestFromRecoveryRefusesToResumeAnInvalidLadder(t *testing.T) {
	// Resuming from an illegal history would launder it: every later transition chains off a state
	// that was never legal, and the whole record validates from that point on.
	bad := *recoveryOf(RungRetry, tr(0, 3, 1, "terminal"), tr(3, 1, 2, "again"))
	if _, err := FromRecovery(bad); err == nil {
		t.Fatal("an invalid recovery was resumed")
	}

	good := *recoveryOf(RungRetry, tr(0, 1, 1, "conflict"))
	l, err := FromRecovery(good)
	if err != nil {
		t.Fatalf("a valid recovery was refused: %v", err)
	}
	if l.Rung() != RungRetry {
		t.Errorf("resumed at rung %d, want 1", l.Rung())
	}
	if err := l.Climb(RungRollback, at(2), "terminal"); err != nil {
		t.Fatalf("climbing a resumed ladder: %v", err)
	}
	if n := len(l.Recovery().Transitions); n != 2 {
		t.Errorf("a resumed ladder has %d transitions, want 2 — the prior history must survive", n)
	}
}

func TestRecoveryIsACopy(t *testing.T) {
	// A caller that mutates what Recovery() returned must not reach back into the ladder.
	l := NewLadder()
	mustClimb(t, l, RungRetry, at(1), "conflict")
	r := l.Recovery()
	r.Rung = RungPage
	r.Transitions[0].Reason = "rewritten"

	again := l.Recovery()
	if again.Rung != RungRetry || again.Transitions[0].Reason != "conflict" {
		t.Fatal("mutating the returned recovery changed the ladder")
	}
}

func TestRungNameCoversTheLadder(t *testing.T) {
	for rung, want := range map[int32]string{
		0: "none", 1: "retry", 2: "alternative", 3: "rollback", 4: "escalate", 5: "page",
	} {
		if got := RungName(rung); got != want {
			t.Errorf("RungName(%d) = %q, want %q", rung, got, want)
		}
	}
	if got := RungName(9); got != "rung-9" {
		t.Errorf("RungName(9) = %q, want a rendered fallback", got)
	}
}

func mustClimb(t *testing.T, l *Ladder, to int32, when time.Time, reason string) {
	t.Helper()
	if err := l.Climb(to, when, reason); err != nil {
		t.Fatalf("climb to %s: %v", RungName(to), err)
	}
}
