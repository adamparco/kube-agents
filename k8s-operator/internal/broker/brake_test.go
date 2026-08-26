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

package broker

import (
	"net/http"
	"sync"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

// V-CTR-015 (L1, 06 §4.4): the nine fail-closed rules are one decision function, each rule refuses
// in the absence of its own input, and the absence of each input is exercised.
//
// V-CTR-016 (L1, 09 §6.9) is the same tests read the other way round: `C-UC`'s preconditions are
// ONE shared predicate and every one of them refuses in isolation against a baseline that is
// accepted -- TestBrakeHealthyBaselineAllows is that baseline, TestBrakeZeroValueRefuses is the
// zero-valued record, and TestBrakeEachRuleFiresInIsolation is the in-isolation half.
//
// The tests are structured around one idea: for every rule there is a HEALTHY baseline that is
// allowed, and the test removes exactly one input from it. That is what makes the assertions mean
// something -- a test suite where every case refuses proves nothing, because a `Decide` that
// returned "refuse" unconditionally would pass all of it. Every negative here is paired with the
// positive it was derived from.

var brakeNow = time.Date(2026, 7, 27, 12, 0, 0, 0, time.UTC)

// ledger is a test Accountant. It answers with a fixed BrakeBudget and keeps every query it was
// asked, so a test can assert not just WHAT row 7 decided but WHAT IT ASKED ABOUT -- an accountant
// handed the agent and not the action cannot answer the question 04 §4.2 poses, and the answer it
// gives instead looks identical from the outside.
//
// Deliberately test-only. An exported "always solvent" accountant would be a supported way to
// switch row 7 off, which is the thing a nil Accountant now refuses in order to prevent.
type ledger struct {
	answer BrakeBudget
	asked  []BudgetQuery
}

func (l *ledger) Budget(q BudgetQuery) BrakeBudget {
	l.asked = append(l.asked, q)
	return l.answer
}

// solvent is the accountant for a baseline that should be allowed: nothing spent, nothing flapping.
func solvent() *ledger { return &ledger{} }

// healthy is the baseline: everything observed, everything fine. It must be ALLOWED, and every
// fail-closed case below is this value with one field removed.
func healthy() BrakeInputs {
	return BrakeInputs{
		Stage:      StageGate,
		Now:        brakeNow,
		Agent:      agentCR(false, ""),
		Scope:      &agentv1alpha1.ScopeSpec{ProjectID: "p", ClusterName: "c", Namespace: "n"},
		Freezes:    &FreezeView{ObservedAt: brakeNow.Add(-time.Second)},
		Journal:    BrakeOK,
		UndoPlan:   BrakeOK,
		Roster:     roster("r", 1, "U1", "U2"),
		Accountant: solvent(),
		Contested:  NewContestedIndex(),
		Class:      agentv1alpha1.RiskElevated,
		Targets:    []agentv1alpha1.TargetRef{deploy("web")},
	}
}

func agentCR(paused bool, reason string) *agentv1alpha1.Agent {
	a := &agentv1alpha1.Agent{}
	a.Name = "agent-1"
	a.Spec.Operations = &agentv1alpha1.OperationsSpec{}
	if paused {
		p := true
		a.Spec.Operations.Paused = &p
		a.Spec.Operations.PauseReason = reason
	}
	return a
}

func roster(name string, minApprovals int32, ids ...string) *agentv1alpha1.ApprovalRoster {
	r := &agentv1alpha1.ApprovalRoster{}
	r.Name = name
	r.Spec.MinApprovals = &minApprovals
	for _, id := range ids {
		r.Spec.Approvers = append(r.Spec.Approvers, agentv1alpha1.Approver{
			Platform: agentv1alpha1.ApproverPlatformSlack,
			ID:       id,
		})
	}
	return r
}

func deploy(name string) agentv1alpha1.TargetRef {
	return agentv1alpha1.TargetRef{
		Group: "apps", Version: "v1", Kind: "Deployment", Namespace: "n", Name: name,
	}
}

func freeze(name string, mutate func(*agentv1alpha1.FleetFreeze)) agentv1alpha1.FleetFreeze {
	f := agentv1alpha1.FleetFreeze{}
	f.Name = name
	f.Spec.Reason = "incident " + name
	f.Spec.RequestedBy = "slack:U1"
	if mutate != nil {
		mutate(&f)
	}
	return f
}

// --- the baseline must be allowed -------------------------------------------------------------

func TestBrakeHealthyBaselineAllows(t *testing.T) {
	// If this ever fails, every negative case below stops proving anything, so it runs first.
	d := Decide(healthy())
	if !d.Allowed() {
		t.Fatalf("the healthy baseline must be allowed, got %s / %s: %s", d.Effect, d.Rule, d.Detail)
	}
	if d.Refusal != nil {
		t.Errorf("an allow must carry no Refusal, got %+v", d.Refusal)
	}
	if !d.JournalReachable {
		t.Error("JournalReachable must be reported true when the journal was observed ok")
	}
	if d.AutoPause || d.Page || d.Escalate {
		t.Errorf("an allow must request no side effects, got autoPause=%v page=%v escalate=%v",
			d.AutoPause, d.Page, d.Escalate)
	}
}

// --- the negative control ---------------------------------------------------------------------

func TestBrakeZeroValueRefuses(t *testing.T) {
	// ¬ The whole design rests on this: a caller that populated nothing gets a refusal, not an
	// allow. If a field is ever added to BrakeInputs whose zero value permits, this is the test
	// that has to be argued with.
	d := Decide(BrakeInputs{})
	if d.Allowed() {
		t.Fatal("Decide(BrakeInputs{}) must not allow")
	}
	if d.Effect != BrakeRefuse {
		t.Errorf("effect = %s, want %s", d.Effect, BrakeRefuse)
	}
	if d.Rule != BrakeRuleFreezeUnreadable {
		t.Errorf("rule = %s, want %s (row 1 is first in the order)", d.Rule, BrakeRuleFreezeUnreadable)
	}
	if d.Refusal == nil {
		t.Fatal("a refusal must carry a Refusal")
	}
	if d.Refusal.Reason != ReasonScopeFrozen {
		t.Errorf("reason = %q, want %q", d.Refusal.Reason, ReasonScopeFrozen)
	}
	if !d.Refusal.Journal {
		t.Error("every brake refusal is journaled")
	}
	if d.JournalReachable {
		t.Error("an unobserved journal must not be reported reachable")
	}
}

func TestBrakeZeroDecisionIsNotAnAllow(t *testing.T) {
	// ¬ The other half of the same property, on the output side: a BrakeDecision nobody filled in
	// must not read as permission.
	if (BrakeDecision{}).Allowed() {
		t.Fatal("the zero BrakeDecision must not be Allowed()")
	}
}

// --- one input removed at a time ----------------------------------------------------------------

func TestBrakeEachRuleFiresInIsolation(t *testing.T) {
	cases := []struct {
		name      string
		mutate    func(*BrakeInputs)
		wantRule  BrakeRule
		wantEff   BrakeEffect
		reason    string // "" when the effect is not a refusal
		status    int
		autoPause bool
		page      bool
		escalate  bool
	}{
		{
			name:     "row 1: freeze list unreadable",
			mutate:   func(in *BrakeInputs) { in.Freezes = nil },
			wantRule: BrakeRuleFreezeUnreadable,
			wantEff:  BrakeRefuse,
			reason:   ReasonScopeFrozen,
			status:   http.StatusForbidden,
		},
		{
			name: "row 1: freeze cache stale beyond 30s",
			mutate: func(in *BrakeInputs) {
				in.Freezes = &FreezeView{ObservedAt: brakeNow.Add(-31 * time.Second)}
			},
			wantRule: BrakeRuleFreezeUnreadable,
			wantEff:  BrakeRefuse,
			reason:   ReasonScopeFrozen,
			status:   http.StatusForbidden,
		},
		{
			name: "row 1: freeze cache with no observation time is infinitely stale",
			mutate: func(in *BrakeInputs) {
				in.Freezes = &FreezeView{} // a caller who forgot to stamp the sync time
			},
			wantRule: BrakeRuleFreezeUnreadable,
			wantEff:  BrakeRefuse,
			reason:   ReasonScopeFrozen,
			status:   http.StatusForbidden,
		},
		{
			name:     "row 2: own Agent CR unreadable",
			mutate:   func(in *BrakeInputs) { in.Agent = nil },
			wantRule: BrakeRuleAgentUnreadable,
			wantEff:  BrakeRefuse,
			reason:   ReasonAgentPaused, // indistinguishable from pause to the caller, on purpose
			status:   http.StatusForbidden,
		},
		{
			name:      "row 3: journal unreachable",
			mutate:    func(in *BrakeInputs) { in.Journal = BrakeFailed },
			wantRule:  BrakeRuleJournalUnreachable,
			wantEff:   BrakeRefuse,
			reason:    ReasonJournalUnavailable,
			status:    http.StatusServiceUnavailable,
			autoPause: true,
		},
		{
			name:      "row 3: journal never observed",
			mutate:    func(in *BrakeInputs) { in.Journal = BrakeUnobserved },
			wantRule:  BrakeRuleJournalUnreachable,
			wantEff:   BrakeRefuse,
			reason:    ReasonJournalUnavailable,
			status:    http.StatusServiceUnavailable,
			autoPause: true,
		},
		{
			name:     "row 5: undo plan failed validation",
			mutate:   func(in *BrakeInputs) { in.UndoPlan = BrakeFailed },
			wantRule: BrakeRuleUndoPlanUnusable,
			wantEff:  BrakeRaiseToGated,
		},
		{
			name:     "row 5: undo plan never generated",
			mutate:   func(in *BrakeInputs) { in.UndoPlan = BrakeUnobserved },
			wantRule: BrakeRuleUndoPlanUnusable,
			wantEff:  BrakeRaiseToGated,
		},
		{
			name: "row 6: gated action with no roster parks",
			mutate: func(in *BrakeInputs) {
				in.Class = agentv1alpha1.RiskGated
				in.Roster = nil
			},
			wantRule: BrakeRuleRosterUnusable,
			wantEff:  BrakePark,
		},
		{
			name: "row 6: gated action with an empty roster parks",
			mutate: func(in *BrakeInputs) {
				in.Class = agentv1alpha1.RiskGated
				in.Roster = roster("empty", 1)
			},
			wantRule: BrakeRuleRosterUnusable,
			wantEff:  BrakePark,
		},
		{
			name: "row 6: a roster that can never reach its own threshold parks",
			mutate: func(in *BrakeInputs) {
				in.Class = agentv1alpha1.RiskGated
				in.Roster = roster("short", 3, "U1", "U2")
			},
			wantRule: BrakeRuleRosterUnusable,
			wantEff:  BrakePark,
		},
		{
			name: "row 7: budget exhausted",
			mutate: func(in *BrakeInputs) {
				in.Accountant = &ledger{answer: BrakeBudget{Exhausted: true, Detail: "3/3 this hour"}}
			},
			wantRule: BrakeRuleBudgetExhausted,
			wantEff:  BrakeRefuse,
			reason:   ReasonBudgetExhausted,
			status:   http.StatusTooManyRequests,
			escalate: true,
		},
		{
			name: "row 7: flap threshold breached",
			mutate: func(in *BrakeInputs) {
				in.Accountant = &ledger{answer: BrakeBudget{FlapBreached: true}}
			},
			wantRule: BrakeRuleFlapBreached,
			wantEff:  BrakeRefuse,
			reason:   ReasonFlapDetected,
			status:   http.StatusTooManyRequests,
			escalate: true,
		},
		{
			// The input this row was missing. A zero BrakeBudget means a tally of nothing and
			// permits; a nil Accountant means nobody is counting and must not.
			name:     "row 7: no accountant at all",
			mutate:   func(in *BrakeInputs) { in.Accountant = nil },
			wantRule: BrakeRuleBudgetExhausted,
			wantEff:  BrakeRefuse,
			reason:   ReasonBudgetExhausted,
			status:   http.StatusTooManyRequests,
			escalate: true,
		},
		{
			name: "row 8: a contested target",
			mutate: func(in *BrakeInputs) {
				in.Contested.Mark(deploy("web"), "01J0ACTION", brakeNow.Add(-time.Hour), "human undid it")
			},
			wantRule: BrakeRuleTargetContested,
			wantEff:  BrakeRefuse,
			reason:   ReasonTargetContested,
			status:   http.StatusForbidden,
		},
		{
			name:     "row 8: the contested index itself is unavailable",
			mutate:   func(in *BrakeInputs) { in.Contested = nil },
			wantRule: BrakeRuleTargetContested,
			wantEff:  BrakeRefuse,
			reason:   ReasonTargetContested,
			status:   http.StatusForbidden,
		},
		{
			name:     "control: the agent is paused",
			mutate:   func(in *BrakeInputs) { in.Agent = agentCR(true, "rolling back a bad config") },
			wantRule: BrakeRulePaused,
			wantEff:  BrakeRefuse,
			reason:   ReasonAgentPaused,
			status:   http.StatusForbidden,
		},
		{
			name: "control: a freeze covers the scope",
			mutate: func(in *BrakeInputs) {
				in.Freezes.Freezes = []agentv1alpha1.FleetFreeze{freeze("all", nil)}
			},
			wantRule: BrakeRuleFrozen,
			wantEff:  BrakeRefuse,
			reason:   ReasonScopeFrozen,
			status:   http.StatusForbidden,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			in := healthy()
			tc.mutate(&in)
			d := Decide(in)

			if d.Allowed() {
				t.Fatalf("must not allow; got %s / %s", d.Effect, d.Rule)
			}
			if d.Effect != tc.wantEff {
				t.Errorf("effect = %s, want %s", d.Effect, tc.wantEff)
			}
			if d.Rule != tc.wantRule {
				t.Errorf("rule = %s, want %s", d.Rule, tc.wantRule)
			}
			if d.Detail == "" {
				t.Error("every non-allow must say why; Detail is empty")
			}
			if d.AutoPause != tc.autoPause {
				t.Errorf("autoPause = %v, want %v", d.AutoPause, tc.autoPause)
			}
			if d.Page != tc.page {
				t.Errorf("page = %v, want %v", d.Page, tc.page)
			}
			if d.Escalate != tc.escalate {
				t.Errorf("escalate = %v, want %v", d.Escalate, tc.escalate)
			}

			if tc.wantEff != BrakeRefuse {
				// Park and RaiseToGated are not refusals: the caller is not told no, so there must
				// be no Refusal to render.
				if d.Refusal != nil {
					t.Errorf("%s must not carry a Refusal, got %+v", tc.wantEff, d.Refusal)
				}
				return
			}
			if d.Refusal == nil {
				t.Fatal("a refusal must carry a Refusal")
			}
			if d.Refusal.Reason != tc.reason {
				t.Errorf("reason = %q, want %q", d.Refusal.Reason, tc.reason)
			}
			if d.Refusal.Status != tc.status {
				t.Errorf("status = %d, want %d", d.Refusal.Status, tc.status)
			}
			if !d.Refusal.Journal {
				t.Error("every brake refusal is journaled (06 §4.1)")
			}
			if d.Refusal.SecurityEvent {
				t.Error("a brake refusal is not a security event; an agent hitting a control is not an attack")
			}
			if d.Refusal.RetryAfterSeconds <= 0 {
				t.Error("a temporary refusal must tell the caller when to come back")
			}
			if d.Refusal.Detail != d.Detail {
				t.Error("the Refusal detail and the decision Detail must be the same sentence")
			}
			// The decision is consumed by the step that asked for it; the Refusal is what travels to
			// the HTTP boundary where the pause can actually be requested. If the two disagree, the
			// row's auto-pause is decided here and dropped there -- which is exactly how row 3
			// shipped refusing correctly and pausing nothing (B-006).
			if d.Refusal.AutoPause != d.AutoPause {
				t.Errorf("Refusal.AutoPause = %v but the decision says %v; the field did not survive the return",
					d.Refusal.AutoPause, d.AutoPause)
			}
		})
	}
}

// --- the two rows that are not gate rows --------------------------------------------------------

func TestBrakeSnapshotStage(t *testing.T) {
	base := func() BrakeInputs {
		in := healthy()
		in.Stage = StageSnapshot
		return in
	}

	t.Run("a persisted snapshot allows", func(t *testing.T) {
		in := base()
		in.Snapshot = BrakeOK
		if d := Decide(in); !d.Allowed() {
			t.Fatalf("want allow, got %s / %s", d.Effect, d.Rule)
		}
	})

	for _, sig := range []BrakeSignal{BrakeFailed, BrakeUnobserved} {
		t.Run("row 4: snapshot "+sig.String(), func(t *testing.T) {
			in := base()
			in.Snapshot = sig
			d := Decide(in)
			if d.Allowed() {
				t.Fatal("a snapshot that is not proven persisted must refuse")
			}
			if d.Rule != BrakeRuleSnapshotFailed {
				t.Errorf("rule = %s, want %s", d.Rule, BrakeRuleSnapshotFailed)
			}
			if d.Refusal == nil || d.Refusal.Reason != ReasonSnapshotFailed {
				t.Errorf("reason = %+v, want %s", d.Refusal, ReasonSnapshotFailed)
			}
			if d.Refusal.Status != http.StatusServiceUnavailable {
				t.Errorf("status = %d, want 503", d.Refusal.Status)
			}
		})
	}
}

func TestBrakePostExecuteStage(t *testing.T) {
	base := func() BrakeInputs {
		in := healthy()
		in.Stage = StagePostExecute
		return in
	}

	t.Run("a verified action allows", func(t *testing.T) {
		in := base()
		in.Verified = BrakeOK
		if d := Decide(in); !d.Allowed() {
			t.Fatalf("want allow, got %s / %s", d.Effect, d.Rule)
		}
	})

	t.Run("verification failed but rollback worked is the ladder, not the brake", func(t *testing.T) {
		in := base()
		in.Verified = BrakeFailed
		in.RolledBack = BrakeOK
		d := Decide(in)
		if !d.Allowed() {
			t.Fatalf("a successful rollback must not halt the agent; got %s / %s", d.Effect, d.Rule)
		}
	})

	t.Run("row 9: unverified and unrolled halts and pages", func(t *testing.T) {
		in := base()
		in.Verified = BrakeFailed
		in.RolledBack = BrakeFailed
		d := Decide(in)
		if d.Allowed() {
			t.Fatal("an unverifiable, unrollbackable write must not be allowed to stand quietly")
		}
		if d.Effect != BrakeHalt {
			t.Errorf("effect = %s, want %s", d.Effect, BrakeHalt)
		}
		if d.Rule != BrakeRuleUnverifiedUnrolled {
			t.Errorf("rule = %s, want %s", d.Rule, BrakeRuleUnverifiedUnrolled)
		}
		if !d.AutoPause {
			t.Error("row 9 pauses the agent")
		}
		if !d.Page {
			t.Error("row 9 pages a human")
		}
		if d.Refusal != nil {
			t.Error("there is nothing to refuse after the write landed")
		}
	})

	t.Run("row 9: neither observed also halts", func(t *testing.T) {
		// ¬ The absence of the input, not just a negative value: a post-execute consultation where
		// nobody recorded whether verification ran is the same position as one where it failed.
		in := base()
		d := Decide(in)
		if d.Effect != BrakeHalt {
			t.Fatalf("effect = %s, want %s", d.Effect, BrakeHalt)
		}
	})
}

// --- ordering ------------------------------------------------------------------------------------

func TestBrakeRuleOrderIsPinned(t *testing.T) {
	// Which rule is reported when several fire decides what an operator goes and looks at, so the
	// order is a property, not an implementation detail. Each step removes the rule that just won
	// and asserts the next one takes over.
	in := healthy()
	in.Freezes = nil                                              // row 1
	in.Agent = nil                                                // row 2
	in.Journal = BrakeFailed                                      // row 3
	in.UndoPlan = BrakeFailed                                     // row 5
	in.Class = agentv1alpha1.RiskGated                            // with the roster below, row 6
	in.Roster = nil                                               // row 6
	in.Accountant = &ledger{answer: BrakeBudget{Exhausted: true}} // row 7
	in.Contested.Mark(deploy("web"), "A", brakeNow, "")           // row 8

	want := []BrakeRule{
		BrakeRuleFreezeUnreadable,
		BrakeRuleAgentUnreadable,
		BrakeRuleJournalUnreachable,
		BrakeRulePaused,
		BrakeRuleFrozen,
		BrakeRuleUndoPlanUnusable,
		BrakeRuleRosterUnusable,
		BrakeRuleBudgetExhausted,
		BrakeRuleTargetContested,
	}
	// Each entry clears the rule that just fired, so the next iteration sees the next one.
	clear := []func(*BrakeInputs){
		func(in *BrakeInputs) { in.Freezes = &FreezeView{ObservedAt: brakeNow} },
		func(in *BrakeInputs) { in.Agent = agentCR(true, "paused for the ordering test") },
		func(in *BrakeInputs) { in.Journal = BrakeOK },
		func(in *BrakeInputs) {
			in.Agent = agentCR(false, "")
			in.Freezes.Freezes = []agentv1alpha1.FleetFreeze{freeze("all", nil)}
		},
		func(in *BrakeInputs) { in.Freezes.Freezes = nil },
		func(in *BrakeInputs) { in.UndoPlan = BrakeOK },
		func(in *BrakeInputs) { in.Roster = roster("r", 1, "U1") },
		func(in *BrakeInputs) { in.Accountant = solvent() },
		func(in *BrakeInputs) { in.Contested = NewContestedIndex() },
	}

	for i, wantRule := range want {
		d := Decide(in)
		if d.Rule != wantRule {
			t.Fatalf("step %d: rule = %s, want %s (%s)", i, d.Rule, wantRule, d.Detail)
		}
		clear[i](&in)
	}
	if d := Decide(in); !d.Allowed() {
		t.Fatalf("after clearing every rule the baseline must allow again; got %s / %s: %s",
			d.Effect, d.Rule, d.Detail)
	}
}

// --- undo -----------------------------------------------------------------------------------------

func TestBrakeUndoExemptions(t *testing.T) {
	// The V-REV-007 reading, recorded in the ledger as a decision: undo is exempt BY ORIGIN from
	// the controls that say "this agent should not be acting", and from nothing else.
	exempt := []struct {
		name   string
		mutate func(*BrakeInputs)
	}{
		{"an unreadable freeze list", func(in *BrakeInputs) { in.Freezes = nil }},
		{"a stale freeze cache", func(in *BrakeInputs) {
			in.Freezes = &FreezeView{ObservedAt: brakeNow.Add(-time.Hour)}
		}},
		{"an unreadable Agent CR", func(in *BrakeInputs) { in.Agent = nil }},
		{"a paused agent", func(in *BrakeInputs) { in.Agent = agentCR(true, "paused") }},
		{"a freeze that allows undo", func(in *BrakeInputs) {
			in.Freezes.Freezes = []agentv1alpha1.FleetFreeze{freeze("all", nil)}
		}},
		{"a contested target", func(in *BrakeInputs) {
			in.Contested.Mark(deploy("web"), "A", brakeNow, "")
		}},
	}
	for _, tc := range exempt {
		t.Run("undo runs despite "+tc.name, func(t *testing.T) {
			in := healthy()
			in.Trigger = agentv1alpha1.ActionTriggerUndo
			tc.mutate(&in)
			if d := Decide(in); !d.Allowed() {
				t.Fatalf("undo must survive %s; got %s / %s: %s", tc.name, d.Effect, d.Rule, d.Detail)
			}
		})
	}

	notExempt := []struct {
		name     string
		mutate   func(*BrakeInputs)
		wantRule BrakeRule
	}{
		{"an unreachable journal", func(in *BrakeInputs) { in.Journal = BrakeFailed },
			BrakeRuleJournalUnreachable},
		{"an unusable undo plan", func(in *BrakeInputs) { in.UndoPlan = BrakeFailed },
			BrakeRuleUndoPlanUnusable},
		{"an exhausted budget", func(in *BrakeInputs) {
			in.Accountant = &ledger{answer: BrakeBudget{Exhausted: true}}
		}, BrakeRuleBudgetExhausted},
		{"an absent accountant", func(in *BrakeInputs) { in.Accountant = nil },
			BrakeRuleBudgetExhausted},
		{"an unavailable contested index", func(in *BrakeInputs) { in.Contested = nil },
			BrakeRuleTargetContested},
		{"a freeze with allowUndo: false", func(in *BrakeInputs) {
			in.Freezes.Freezes = []agentv1alpha1.FleetFreeze{freeze("hard", func(f *agentv1alpha1.FleetFreeze) {
				no := false
				f.Spec.AllowUndo = &no
			})}
		}, BrakeRuleFrozen},
	}
	for _, tc := range notExempt {
		t.Run("undo is still stopped by "+tc.name, func(t *testing.T) {
			in := healthy()
			in.Trigger = agentv1alpha1.ActionTriggerUndo
			tc.mutate(&in)
			d := Decide(in)
			if d.Allowed() {
				t.Fatalf("undo must not be exempt from %s", tc.name)
			}
			if d.Rule != tc.wantRule {
				t.Errorf("rule = %s, want %s", d.Rule, tc.wantRule)
			}
		})
	}

	t.Run("undo is still verified after it executes", func(t *testing.T) {
		in := healthy()
		in.Trigger = agentv1alpha1.ActionTriggerUndo
		in.Stage = StagePostExecute
		in.Verified = BrakeFailed
		in.RolledBack = BrakeFailed
		if d := Decide(in); d.Effect != BrakeHalt {
			t.Fatalf("an unverifiable undo halts like anything else; got %s", d.Effect)
		}
	})
}

// --- freeze semantics -------------------------------------------------------------------------

func TestBrakeFreezeSemantics(t *testing.T) {
	t.Run("an expired freeze does not block", func(t *testing.T) {
		in := healthy()
		in.Freezes.Freezes = []agentv1alpha1.FleetFreeze{freeze("old", func(f *agentv1alpha1.FleetFreeze) {
			t := metav1.NewTime(brakeNow.Add(-time.Minute))
			f.Spec.ExpiresAt = &t
		})}
		if d := Decide(in); !d.Allowed() {
			t.Fatalf("an expired freeze must not block; got %s / %s", d.Effect, d.Rule)
		}
	})

	t.Run("a freeze for another cluster does not block", func(t *testing.T) {
		in := healthy()
		in.Freezes.Freezes = []agentv1alpha1.FleetFreeze{freeze("other", func(f *agentv1alpha1.FleetFreeze) {
			f.Spec.Scope.ClusterName = "somewhere-else"
		})}
		if d := Decide(in); !d.Allowed() {
			t.Fatalf("a freeze on another cluster must not block; got %s / %s", d.Effect, d.Rule)
		}
	})

	t.Run("allowClasses: routine lets routine through and still stops elevated", func(t *testing.T) {
		mk := func(class agentv1alpha1.ActionRiskClass) BrakeInputs {
			in := healthy()
			in.Class = class
			in.Freezes.Freezes = []agentv1alpha1.FleetFreeze{freeze("partial", func(f *agentv1alpha1.FleetFreeze) {
				f.Spec.AllowClasses = []agentv1alpha1.FreezeClass{agentv1alpha1.FreezeClassRoutine}
			})}
			return in
		}
		if d := Decide(mk(agentv1alpha1.RiskRoutine)); !d.Allowed() {
			t.Errorf("routine must survive a freeze that allows it; got %s / %s", d.Effect, d.Rule)
		}
		if d := Decide(mk(agentv1alpha1.RiskElevated)); d.Allowed() {
			t.Error("elevated must not survive a freeze that allows only routine")
		}
	})

	t.Run("the reported freeze is stable when several cover the scope", func(t *testing.T) {
		// An operator clears the freeze they were told about; if the name rotated between two
		// identical requests they would clear one and be refused by another, with no way to tell
		// how many are left.
		in := healthy()
		in.Freezes.Freezes = []agentv1alpha1.FleetFreeze{
			freeze("zulu", nil), freeze("alpha", nil), freeze("mike", nil),
		}
		for i := 0; i < 20; i++ {
			d := Decide(in)
			if d.Rule != BrakeRuleFrozen {
				t.Fatalf("want frozen, got %s", d.Rule)
			}
			if want := "FleetFreeze alpha covers this scope"; len(d.Detail) < len(want) || d.Detail[:len(want)] != want {
				t.Fatalf("detail = %q, want it to name the first freeze by name", d.Detail)
			}
		}
	})

	t.Run("a nil scope cannot be shown to be outside a freeze", func(t *testing.T) {
		// ¬ Fail-closed on the scope itself: an agent whose scope the broker does not know is
		// covered by every freeze.
		in := healthy()
		in.Scope = nil
		in.Freezes.Freezes = []agentv1alpha1.FleetFreeze{freeze("narrow", func(f *agentv1alpha1.FleetFreeze) {
			f.Spec.Scope.Namespace = "some-other-namespace"
		})}
		if d := Decide(in); d.Allowed() {
			t.Fatal("an unknown scope must be treated as covered")
		}
	})
}

// --- retry-after ---------------------------------------------------------------------------------

func TestBrakeRetryAfter(t *testing.T) {
	t.Run("pause answers with the fixed interval", func(t *testing.T) {
		in := healthy()
		in.Agent = agentCR(true, "maintenance")
		d := Decide(in)
		if got := d.Refusal.RetryAfterSeconds; got != PausedRetryAfterSeconds {
			t.Errorf("retryAfter = %d, want %d", got, PausedRetryAfterSeconds)
		}
	})

	t.Run("a freeze answers with its own expiry", func(t *testing.T) {
		in := healthy()
		in.Freezes.Freezes = []agentv1alpha1.FleetFreeze{freeze("timed", func(f *agentv1alpha1.FleetFreeze) {
			t := metav1.NewTime(brakeNow.Add(10 * time.Minute))
			f.Spec.ExpiresAt = &t
		})}
		if got := Decide(in).Refusal.RetryAfterSeconds; got != 600 {
			t.Errorf("retryAfter = %d, want 600", got)
		}
	})

	t.Run("a long freeze is capped", func(t *testing.T) {
		in := healthy()
		in.Freezes.Freezes = []agentv1alpha1.FleetFreeze{freeze("long", func(f *agentv1alpha1.FleetFreeze) {
			t := metav1.NewTime(brakeNow.Add(72 * time.Hour))
			f.Spec.ExpiresAt = &t
		})}
		if got := Decide(in).Refusal.RetryAfterSeconds; got != MaxRetryAfterSeconds {
			t.Errorf("retryAfter = %d, want the %d cap", got, MaxRetryAfterSeconds)
		}
	})

	t.Run("a freeze with no expiry falls back to the fixed interval", func(t *testing.T) {
		in := healthy()
		in.Freezes.Freezes = []agentv1alpha1.FleetFreeze{freeze("forever", nil)}
		if got := Decide(in).Refusal.RetryAfterSeconds; got != PausedRetryAfterSeconds {
			t.Errorf("retryAfter = %d, want %d", got, PausedRetryAfterSeconds)
		}
	})
}

// --- the contested index ---------------------------------------------------------------------

func TestContestedIndexKeyIgnoresVersion(t *testing.T) {
	// The laundering case: the same object addressed through a second API version must not escape
	// its marker.
	x := NewContestedIndex()
	v1 := deploy("web")
	beta := deploy("web")
	beta.Version = "v1beta1"
	x.Mark(v1, "A1", brakeNow, "")
	if _, ok := x.Lookup(beta); !ok {
		t.Fatal("a contested marker must survive an API version change on the same object")
	}
}

func TestContestedIndexKeyIgnoresUID(t *testing.T) {
	// The delete-and-recreate case: undoing a `create` deletes the object, so the recreated one has
	// a new UID. If the key carried UID the marker would never match the thing it exists to stop.
	x := NewContestedIndex()
	original := deploy("web")
	original.UID = "uid-1"
	recreated := deploy("web")
	recreated.UID = "uid-2"
	x.Mark(original, "A1", brakeNow, "human deleted it")
	if _, ok := x.Lookup(recreated); !ok {
		t.Fatal("a contested marker must survive a delete and recreate")
	}
}

func TestContestedIndexDistinguishesObjects(t *testing.T) {
	// The paired positive: the key is not so loose that everything looks contested.
	x := NewContestedIndex()
	x.Mark(deploy("web"), "A1", brakeNow, "")

	other := deploy("api")
	if _, ok := x.Lookup(other); ok {
		t.Error("a different name must not match")
	}
	ns := deploy("web")
	ns.Namespace = "other"
	if _, ok := x.Lookup(ns); ok {
		t.Error("a different namespace must not match")
	}
	kind := deploy("web")
	kind.Kind = "StatefulSet"
	if _, ok := x.Lookup(kind); ok {
		t.Error("a different kind must not match")
	}
	group := deploy("web")
	group.Group = "batch"
	if _, ok := x.Lookup(group); ok {
		t.Error("a different group must not match")
	}
}

func TestContestedIndexCoreGroupIsNotEmpty(t *testing.T) {
	// A core-group target ("" group) must not collide with a target whose kind happens to equal
	// the next segment. Naming the core group keeps the key unambiguous.
	x := NewContestedIndex()
	pod := agentv1alpha1.TargetRef{Version: "v1", Kind: "Pod", Namespace: "n", Name: "p"}
	x.Mark(pod, "A1", brakeNow, "")
	if got := ContestedKey(pod); got != "core/Pod/n/p" {
		t.Errorf("key = %q, want core/Pod/n/p", got)
	}
}

func TestContestedIndexFirstMarkWins(t *testing.T) {
	// The operator was pointed at the first action; a later one must not silently redirect them.
	x := NewContestedIndex()
	x.Mark(deploy("web"), "FIRST", brakeNow, "the original contest")
	x.Mark(deploy("web"), "SECOND", brakeNow.Add(time.Hour), "a later one")
	e, ok := x.Lookup(deploy("web"))
	if !ok {
		t.Fatal("marker vanished")
	}
	if e.ActionID != "FIRST" {
		t.Errorf("actionID = %q, want FIRST", e.ActionID)
	}
}

func TestContestedIndexClear(t *testing.T) {
	x := NewContestedIndex()
	x.Mark(deploy("web"), "A1", brakeNow, "")
	x.Mark(deploy("api"), "A1", brakeNow, "")
	x.Mark(deploy("db"), "A2", brakeNow, "")
	if x.Len() != 3 {
		t.Fatalf("len = %d, want 3", x.Len())
	}

	x.Clear(deploy("web"))
	if _, ok := x.Lookup(deploy("web")); ok {
		t.Error("Clear did not remove the marker")
	}

	if n := x.ClearByAction("A1"); n != 1 {
		t.Errorf("ClearByAction removed %d, want 1 (web was already cleared)", n)
	}
	if _, ok := x.Lookup(deploy("db")); !ok {
		t.Error("ClearByAction must not touch another action's markers")
	}
	if n := x.ClearByAction(""); n != 0 {
		t.Errorf("ClearByAction(\"\") removed %d, want 0 — an empty id must not clear everything", n)
	}
	if x.Len() != 1 {
		t.Errorf("len = %d, want 1", x.Len())
	}
}

func TestContestedIndexNilReceiverIsSafe(t *testing.T) {
	// A nil index answers "not contested" rather than panicking, because the refusal for a missing
	// index belongs in Decide, where it can say so. Verified here so the two halves stay honest.
	var x *ContestedIndex
	x.Mark(deploy("web"), "A", brakeNow, "")
	x.Clear(deploy("web"))
	if n := x.ClearByAction("A"); n != 0 {
		t.Errorf("ClearByAction on nil returned %d", n)
	}
	if _, ok := x.Lookup(deploy("web")); ok {
		t.Error("a nil index must not claim a target is contested")
	}
	if x.Len() != 0 {
		t.Error("a nil index has no entries")
	}
	// The refusal is Decide's job, and it does it.
	in := healthy()
	in.Contested = x
	if d := Decide(in); d.Rule != BrakeRuleTargetContested {
		t.Errorf("a nil index must refuse at the brake; got %s", d.Rule)
	}
}

func TestContestedIndexConcurrent(t *testing.T) {
	// The index is read on every envelope and written by the undo controller. Run under -race.
	x := NewContestedIndex()
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(3)
		go func(i int) { defer wg.Done(); x.Mark(deploy("web"), "A", brakeNow, "") }(i)
		go func() { defer wg.Done(); _, _ = x.Lookup(deploy("web")) }()
		go func() { defer wg.Done(); _ = x.Len() }()
	}
	wg.Wait()
	if x.Len() != 1 {
		t.Errorf("len = %d, want 1", x.Len())
	}
}

// --- the typed vocabulary ------------------------------------------------------------------------

func TestBrakeStageZeroIsTheStrictest(t *testing.T) {
	// ¬ A caller who forgets the stage must get the gate, not a post-execute evaluation that
	// consults almost nothing.
	if StageGate != 0 {
		t.Fatal("StageGate must be the zero BrakeStage")
	}
	if got := BrakeStage(0).String(); got != "gate" {
		t.Errorf("stage 0 = %q, want gate", got)
	}
}

func TestBrakeSignalZeroIsUnobserved(t *testing.T) {
	if BrakeUnobserved != 0 {
		t.Fatal("BrakeUnobserved must be the zero BrakeSignal")
	}
	if BrakeUnobserved.ok() {
		t.Fatal("an unobserved signal must not read as ok")
	}
	if !BrakeOK.ok() || BrakeFailed.ok() {
		t.Fatal("ok() must be true for BrakeOK alone")
	}
}

func TestBrakeEffectZeroIsNotAllow(t *testing.T) {
	if BrakeEffect("") == BrakeAllow {
		t.Fatal("the zero BrakeEffect must not equal BrakeAllow")
	}
}
