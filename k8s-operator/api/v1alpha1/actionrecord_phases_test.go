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

package v1alpha1

import (
	"os"
	"regexp"
	"sort"
	"strings"
	"testing"
)

// V-CTR-006 -- `ActionRecord` lifecycle: every legal transition succeeds, every illegal one is
// rejected (06 §4.3, L1).
//
// The check is written as a CLOSED truth table over the full square, not as a list of the
// transitions somebody remembered. 10 phases plus the empty from-phase is 110 ordered pairs; the
// table below names the legal ones and the test asserts the other 96 are refused. That direction
// matters more than it looks: a lifecycle check written as "these edges work" passes just as
// happily on an enforcement function that returns true for everything, which is the state this
// package was in until the transitions file existed.

// legalEdges is the expected answer, transcribed from 06 §4.3 INDEPENDENTLY of
// actionrecord_phases.go. It is deliberately a second copy: a test that iterated the production map
// to decide what to expect would assert that the map equals itself, and would stay green through
// any edit to it -- including deleting every entry.
var legalEdges = map[string]bool{
	// Creation. The from-side is the empty phase; only these four are a lifecycle position a record
	// can be born in.
	"->Pending":         true,
	"->PendingApproval": true,
	"->Executing":       true,
	"->Rejected":        true,

	"Pending->PendingApproval": true,
	"Pending->Executing":       true,
	"Pending->DryRun":          true,
	"Pending->Rejected":        true,

	"PendingApproval->Pending":   true,
	"PendingApproval->Executing": true,
	"PendingApproval->Rejected":  true,
	"PendingApproval->Expired":   true,

	"Executing->Verified":   true,
	"Executing->Failed":     true,
	"Executing->RolledBack": true,
	"Executing->DryRun":     true,

	"Verified->Undone": true,
}

// selfEdge is legal for every known phase, for the reason the production file states: SetPhase
// re-reads before writing, and a conflict retry re-issues the same write.
func expected(from, to ActionPhase) bool {
	if to == "" {
		return false
	}
	if from != "" && from == to {
		return true
	}
	return legalEdges[string(from)+"->"+string(to)]
}

func TestTheLifecycleIsAClosedTruthTableAndNotAListOfRememberedEdges(t *testing.T) {
	phases := append([]ActionPhase{""}, AllActionPhases()...)
	if len(phases) != 11 {
		t.Fatalf("the square is %d wide; 06 §4.3 has ten phases plus the empty from-phase", len(phases))
	}

	var legal, refused int
	for _, from := range phases {
		for _, to := range phases {
			want := expected(from, to)
			got := from.CanTransitionTo(to)
			if got != want {
				t.Errorf("%q -> %q: CanTransitionTo = %v, want %v", from, to, got, want)
			}
			if want {
				legal++
			} else {
				refused++
			}

			// The error-returning form must agree with the boolean one on every cell. Two
			// spellings of one rule is how a caller ends up validating with the lenient half.
			err := ValidateActionPhaseTransition(from, to)
			if (err == nil) != want {
				t.Errorf("%q -> %q: ValidateActionPhaseTransition err = %v, but CanTransitionTo says %v", from, to, err, want)
			}
		}
	}

	// Vacuity guard in both directions. A table that refused everything would satisfy every
	// "illegal is refused" assertion in this file, and one that allowed everything would satisfy
	// every "legal succeeds" assertion.
	if legal != 27 {
		t.Errorf("legal cells = %d, want 27 (17 named edges + 10 self-edges)", legal)
	}
	if refused != 94 {
		t.Errorf("refused cells = %d, want 94", refused)
	}
}

func TestEveryPhaseInTheCrdEnumIsInTheLifecycle(t *testing.T) {
	// The kubebuilder marker is what the API server enforces; the transition table is what the
	// broker enforces. A phase in one and not the other is a value the cluster accepts and the code
	// has never heard of -- which is exactly how `status.phase` became a free-text field with an
	// enum comment.
	src, err := os.ReadFile("actionrecord_types.go")
	if err != nil {
		t.Fatalf("read actionrecord_types.go: %v", err)
	}
	m := regexp.MustCompile(`\+kubebuilder:validation:Enum=(\S+)\ntype ActionPhase string`).FindSubmatch(src)
	if m == nil {
		t.Fatal("no `+kubebuilder:validation:Enum=` marker found immediately above `type ActionPhase string`")
	}
	enum := strings.Split(string(m[1]), ";")
	if len(enum) != 10 {
		t.Fatalf("the CRD enum has %d members: %v", len(enum), enum)
	}

	inTable := map[string]bool{}
	for _, p := range AllActionPhases() {
		inTable[string(p)] = true
	}
	for _, p := range enum {
		if !inTable[p] {
			t.Errorf("phase %q is in the CRD enum and not in the transition table: the API server would accept it and nothing would know what it may become", p)
		}
		delete(inTable, p)
	}
	for p := range inTable {
		t.Errorf("phase %q is in the transition table and not in the CRD enum: unreachable, and a rule about it can never fire", p)
	}
}

func TestTheSixTerminalPhasesAreTheSixThePhaseTableMarks(t *testing.T) {
	// 06 §4.3's phase table has a `Terminal` column with six ticks. `Verified` is NOT one of the
	// six in the sense this predicate means -- see the file comment: the table's tick is about the
	// broker's pipeline stopping, and `Verified -> Undone` is a different principal, later.
	want := map[ActionPhase]bool{
		PhaseFailed:     true,
		PhaseRolledBack: true,
		PhaseUndone:     true,
		PhaseRejected:   true,
		PhaseExpired:    true,
		PhaseDryRun:     true,
	}
	for _, p := range AllActionPhases() {
		if got := p.IsTerminal(); got != want[p] {
			t.Errorf("%q.IsTerminal() = %v, want %v", p, got, want[p])
		}
		if p.IsTerminal() && len(p.Successors()) != 0 {
			t.Errorf("%q is terminal and lists successors %v", p, p.Successors())
		}
	}
	if ActionPhase("Concluded").IsTerminal() {
		t.Error("an unknown phase reported terminal: a typo would read as a completed action")
	}
	if ActionPhase("").IsTerminal() {
		t.Error("the empty phase reported terminal")
	}
}

func TestNoRecordMayBeBornInAPhaseThatAssertsAPastItDoesNotHave(t *testing.T) {
	// The five refused initial phases each claim an earlier phase happened. This is the forgery
	// arm: a journal entry created `Verified` is an action nothing observed executing.
	for _, p := range []ActionPhase{PhaseVerified, PhaseFailed, PhaseRolledBack, PhaseUndone, PhaseExpired} {
		if IsInitialActionPhase(p) {
			t.Errorf("a record may be created in %q, which asserts a phase it never passed through", p)
		}
		err := ValidateActionPhaseTransition("", p)
		if err == nil {
			t.Fatalf("creating a record in %q was accepted", p)
		}
		if !strings.Contains(err.Error(), "may not be created") {
			t.Errorf("creating a record in %q refused with the wrong message: %v", p, err)
		}
	}
	// DryRun is refused at creation for a reason worth pinning separately, because it is the one
	// terminal phase the broker DOES write on the happy path: it must arrive from `Executing`, so
	// that a dry run rehearses 03 §6's write-ahead ordering rather than sidestepping it.
	if IsInitialActionPhase(PhaseDryRun) {
		t.Error("a record may be created in DryRun, which skips the Executing the write-ahead guarantee is asserted against")
	}
	for _, p := range []ActionPhase{PhasePending, PhasePendingApproval, PhaseExecuting, PhaseRejected} {
		if !IsInitialActionPhase(p) {
			t.Errorf("a record may NOT be created in %q, but 06 §4.2 creates one there", p)
		}
	}
}

func TestARefusalNamesBothEndsAndWhatWouldHaveWorked(t *testing.T) {
	// A controller that logs "illegal transition" and nothing else forces a human to re-derive the
	// lifecycle from a doc comment mid-incident. Each arm below is a distinct diagnosis and they
	// must not collapse into one generic string.
	for _, tc := range []struct {
		name       string
		from, to   ActionPhase
		mustSay    []string
		mustNotSay []string
	}{
		{
			name:    "out of a terminal phase",
			from:    PhaseRejected,
			to:      PhaseExecuting,
			mustSay: []string{"terminal", `"Rejected"`, `"Executing"`},
		},
		{
			name:    "a legal phase reached the wrong way",
			from:    PhaseVerified,
			to:      PhaseExecuting,
			mustSay: []string{`"Verified" -> "Executing"`, "allows", "Undone"},
		},
		{
			name:       "a phase that does not exist",
			from:       PhaseExecuting,
			to:         ActionPhase("Concluded"),
			mustSay:    []string{`"Concluded"`, "not a member"},
			mustNotSay: []string{"terminal"},
		},
		{
			name:    "an unknown from-phase",
			from:    ActionPhase("Concluded"),
			to:      PhaseVerified,
			mustSay: []string{`"Concluded"`, "no transition out of it"},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			err := ValidateActionPhaseTransition(tc.from, tc.to)
			if err == nil {
				t.Fatalf("%q -> %q was accepted", tc.from, tc.to)
			}
			for _, s := range tc.mustSay {
				if !strings.Contains(err.Error(), s) {
					t.Errorf("message does not contain %q: %v", s, err)
				}
			}
			for _, s := range tc.mustNotSay {
				if strings.Contains(err.Error(), s) {
					t.Errorf("message wrongly contains %q: %v", s, err)
				}
			}
		})
	}
}

func TestTheTableCannotBeRewrittenThroughAnAccessor(t *testing.T) {
	// Successors returns package state. Handing out the live slice would let one caller's append
	// change the lifecycle for every other caller in the process, and the mutation would look like
	// a local variable.
	before := PhaseExecuting.Successors()
	got := PhaseExecuting.Successors()
	got = append(got[:0], PhaseUndone)
	_ = got
	after := PhaseExecuting.Successors()
	if len(after) != len(before) {
		t.Fatalf("Successors() shrank from %v to %v after a caller wrote to the returned slice", before, after)
	}
	for i := range before {
		if after[i] != before[i] {
			t.Fatalf("Successors() changed from %v to %v after a caller wrote to the returned slice", before, after)
		}
	}
	if PhaseExecuting.CanTransitionTo(PhaseUndone) {
		t.Error("Executing -> Undone became legal after a caller mutated a returned slice")
	}
}

func TestEveryPhaseIsReachableFromCreation(t *testing.T) {
	// A phase nothing can reach is a rule that can never fire, and an enum member no conformance
	// test can exercise. Walked as a real traversal rather than asserted per phase, so a future
	// edit that orphans a phase by deleting the one edge into it fails here.
	seen := map[ActionPhase]bool{}
	var queue []ActionPhase
	for _, p := range AllActionPhases() {
		if IsInitialActionPhase(p) {
			seen[p] = true
			queue = append(queue, p)
		}
	}
	for len(queue) > 0 {
		p := queue[0]
		queue = queue[1:]
		for _, n := range p.Successors() {
			if !seen[n] {
				seen[n] = true
				queue = append(queue, n)
			}
		}
	}
	var orphans []string
	for _, p := range AllActionPhases() {
		if !seen[p] {
			orphans = append(orphans, string(p))
		}
	}
	sort.Strings(orphans)
	if len(orphans) != 0 {
		t.Errorf("unreachable from any creation phase: %v", orphans)
	}
}
