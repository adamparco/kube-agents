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
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func i32(v int32) *int32 { return &v }

// TestTheTableMatchesTheSpec transcribes 06 §1.1 a SECOND time, by hand, and compares.
//
// A test that read the constants back would pass no matter what they said. These literals are the
// markdown table retyped from the document, so the test fails if either copy drifts -- which is the
// only failure mode a one-definition-site refactor can still have.
func TestTheTableMatchesTheSpec(t *testing.T) {
	for _, tc := range []struct {
		name string
		got  BudgetClassLimits
		want BudgetClassLimits
	}{
		{"selfInitiated default", DefaultSelfInitiatedBudget, BudgetClassLimits{30, 6, 3, 200}},
		{"selfInitiated ceiling", CeilingSelfInitiatedBudget, BudgetClassLimits{50, 10, 5, 500}},
		{"humanRequested default", DefaultHumanRequestedBudget, BudgetClassLimits{120, 40, 20, 800}},
		{"humanRequested ceiling", CeilingHumanRequestedBudget, BudgetClassLimits{200, 60, 30, 2000}},
	} {
		if tc.got != tc.want {
			t.Errorf("%s is %+v, and the 06 §1.1 table says %+v", tc.name, tc.got, tc.want)
		}
	}

	if DefaultMaxObjectsPerAction != 25 || CeilingMaxObjectsPerAction != 50 {
		t.Errorf("maxObjectsPerAction default/ceiling is %d/%d, want 25/50",
			DefaultMaxObjectsPerAction, CeilingMaxObjectsPerAction)
	}
	if DefaultFlapThreshold != 3 || CeilingFlapThreshold != 5 {
		t.Errorf("flapThreshold default/ceiling is %d/%d, want 3/5", DefaultFlapThreshold, CeilingFlapThreshold)
	}
	if DefaultFlapWindow != 30*time.Minute || FloorFlapWindow != 5*time.Minute {
		t.Errorf("flapWindow default/floor is %s/%s, want 30m/5m", DefaultFlapWindow, FloorFlapWindow)
	}

	// Every ceiling must be at or above its default, or the resolver would clamp an unconfigured
	// agent below the allowance the spec promises it.
	for _, p := range []struct {
		name         string
		def, ceiling BudgetClassLimits
	}{
		{"selfInitiated", DefaultSelfInitiatedBudget, CeilingSelfInitiatedBudget},
		{"humanRequested", DefaultHumanRequestedBudget, CeilingHumanRequestedBudget},
	} {
		if p.def.RoutinePerHour > p.ceiling.RoutinePerHour ||
			p.def.ElevatedPerHour > p.ceiling.ElevatedPerHour ||
			p.def.GatedPerHour > p.ceiling.GatedPerHour ||
			p.def.ActionsPerDay > p.ceiling.ActionsPerDay {
			t.Errorf("%s default %+v exceeds its own ceiling %+v", p.name, p.def, p.ceiling)
		}
	}

	// And humanRequested must be the LARGER allowance everywhere -- 06 §1.1 makes that the point of
	// the split. If they ever crossed, a human asking for something would be tighter-capped than the
	// agent doing it unprompted.
	if DefaultHumanRequestedBudget.RoutinePerHour <= DefaultSelfInitiatedBudget.RoutinePerHour ||
		DefaultHumanRequestedBudget.ElevatedPerHour <= DefaultSelfInitiatedBudget.ElevatedPerHour ||
		DefaultHumanRequestedBudget.GatedPerHour <= DefaultSelfInitiatedBudget.GatedPerHour ||
		DefaultHumanRequestedBudget.ActionsPerDay <= DefaultSelfInitiatedBudget.ActionsPerDay {
		t.Errorf("humanRequested %+v is not strictly larger than selfInitiated %+v",
			DefaultHumanRequestedBudget, DefaultSelfInitiatedBudget)
	}
}

// TestBudgetOriginForPartitionsEveryTrigger covers the whole `ActionTriggerSource` enum, plus a
// value that is not in it.
func TestBudgetOriginForPartitionsEveryTrigger(t *testing.T) {
	for src, want := range map[ActionTriggerSource]BudgetOrigin{
		"chat":       OriginHumanRequested,
		"undo":       OriginHumanRequested,
		"watch":      OriginSelfInitiated,
		"alert":      OriginSelfInitiated,
		"cron":       OriginSelfInitiated,
		"delegation": OriginSelfInitiated,
		"escalation": OriginSelfInitiated,
	} {
		if got := BudgetOriginFor(src); got != want {
			t.Errorf("BudgetOriginFor(%q) = %q, want %q", src, got, want)
		}
	}

	// The direction of the default arm is the security property: a trigger source added to the enum
	// in a later phase and not taught to this function must draw on the TIGHTER allowance.
	if got := BudgetOriginFor("some-trigger-invented-in-phase-12"); got != OriginSelfInitiated {
		t.Errorf("an unrecognised trigger draws the %q allowance; an unknown trigger must fall to the tighter %q bucket",
			got, OriginSelfInitiated)
	}
	if got := BudgetOriginFor(""); got != OriginSelfInitiated {
		t.Errorf("an empty trigger draws the %q allowance, want %q", got, OriginSelfInitiated)
	}
}

// TestPerHourHasNoForbiddenBucket pins the two-value return.
func TestPerHourHasNoForbiddenBucket(t *testing.T) {
	l := DefaultSelfInitiatedBudget
	for class, want := range map[ActionRiskClass]int32{
		RiskRoutine:  30,
		RiskElevated: 6,
		RiskGated:    3,
	} {
		got, ok := l.PerHour(class)
		if !ok || got != want {
			t.Errorf("PerHour(%q) = (%d, %v), want (%d, true)", class, got, ok, want)
		}
	}
	if _, ok := l.PerHour(RiskForbidden); ok {
		t.Error("`forbidden` reports an hourly bucket; it has no row in 06 §1.1 and a zero cap would " +
			"read as a budget refusal for an action 06 §4.4 row 3 already refused")
	}
	if _, ok := l.PerHour(""); ok {
		t.Error("an empty risk class reports an hourly bucket")
	}
}

// TestEffectiveInitiativeBudgetDefaultsWhenSilent covers nil at every level, including a nil Agent.
func TestEffectiveInitiativeBudgetDefaultsWhenSilent(t *testing.T) {
	want := ResolvedInitiativeBudget{
		SelfInitiated:       DefaultSelfInitiatedBudget,
		HumanRequested:      DefaultHumanRequestedBudget,
		MaxObjectsPerAction: DefaultMaxObjectsPerAction,
		FlapWindow:          DefaultFlapWindow,
		FlapThreshold:       DefaultFlapThreshold,
	}
	for _, tc := range []struct {
		name  string
		agent *Agent
	}{
		{"nil agent", nil},
		{"no operations", &Agent{}},
		{"no initiativeBudget", &Agent{Spec: AgentSpec{Operations: &OperationsSpec{}}}},
		{"empty initiativeBudget", &Agent{Spec: AgentSpec{Operations: &OperationsSpec{
			InitiativeBudget: &InitiativeBudgetSpec{},
		}}}},
		{"empty class specs", &Agent{Spec: AgentSpec{Operations: &OperationsSpec{
			InitiativeBudget: &InitiativeBudgetSpec{
				SelfInitiated:  &BudgetClassSpec{},
				HumanRequested: &BudgetClassSpec{},
			},
		}}}},
	} {
		if got := tc.agent.EffectiveInitiativeBudget(); got != want {
			t.Errorf("%s: %+v, want the 06 §1.1 defaults %+v", tc.name, got, want)
		}
	}
}

// TestEffectiveInitiativeBudgetHonoursAnExplicitZero is the one place this function differs from
// ApprovalRoster.EffectiveTTL, so it gets its own test.
func TestEffectiveInitiativeBudgetHonoursAnExplicitZero(t *testing.T) {
	a := &Agent{Spec: AgentSpec{Operations: &OperationsSpec{InitiativeBudget: &InitiativeBudgetSpec{
		SelfInitiated: &BudgetClassSpec{ElevatedPerHour: i32(0), GatedPerHour: i32(0)},
	}}}}
	got := a.EffectiveInitiativeBudget().SelfInitiated

	if got.ElevatedPerHour != 0 || got.GatedPerHour != 0 {
		t.Errorf("elevated/gated resolved to %d/%d; an operator who wrote 0 had the allowance defaulted "+
			"back to %d/%d, silently returning authority they withheld",
			got.ElevatedPerHour, got.GatedPerHour,
			DefaultSelfInitiatedBudget.ElevatedPerHour, DefaultSelfInitiatedBudget.GatedPerHour)
	}
	// The leaves they did NOT write still default -- zero must not be contagious across the row.
	if got.RoutinePerHour != DefaultSelfInitiatedBudget.RoutinePerHour {
		t.Errorf("routinePerHour = %d, want the default %d: an unwritten leaf was zeroed by its siblings",
			got.RoutinePerHour, DefaultSelfInitiatedBudget.RoutinePerHour)
	}
}

// TestEffectiveInitiativeBudgetClampsAnOverCeilingLeaf is the runtime half of the 06 §1.2 V-8 split.
//
// Admission REJECTS these. This is what happens when one gets in anyway -- a CR that predates the
// rule, a webhook that was down, a direct etcd write -- and the answer must not be "the leaf wins",
// because then any of those three is an authority grant.
func TestEffectiveInitiativeBudgetClampsAnOverCeilingLeaf(t *testing.T) {
	a := &Agent{Spec: AgentSpec{Operations: &OperationsSpec{InitiativeBudget: &InitiativeBudgetSpec{
		SelfInitiated: &BudgetClassSpec{
			RoutinePerHour:  i32(5000),
			ElevatedPerHour: i32(-1),
			ActionsPerDay:   i32(999999),
		},
		HumanRequested:      &BudgetClassSpec{GatedPerHour: i32(31)},
		MaxObjectsPerAction: i32(1000),
		FlapThreshold:       i32(99),
		FlapWindow:          &metav1.Duration{Duration: time.Second},
	}}}}
	got := a.EffectiveInitiativeBudget()

	if got.SelfInitiated.RoutinePerHour != CeilingSelfInitiatedBudget.RoutinePerHour {
		t.Errorf("routinePerHour = %d, want the ceiling %d", got.SelfInitiated.RoutinePerHour, CeilingSelfInitiatedBudget.RoutinePerHour)
	}
	if got.SelfInitiated.ActionsPerDay != CeilingSelfInitiatedBudget.ActionsPerDay {
		t.Errorf("actionsPerDay = %d, want the ceiling %d", got.SelfInitiated.ActionsPerDay, CeilingSelfInitiatedBudget.ActionsPerDay)
	}
	if got.HumanRequested.GatedPerHour != CeilingHumanRequestedBudget.GatedPerHour {
		t.Errorf("humanRequested gatedPerHour = %d, want the ceiling %d", got.HumanRequested.GatedPerHour, CeilingHumanRequestedBudget.GatedPerHour)
	}
	if got.MaxObjectsPerAction != CeilingMaxObjectsPerAction {
		t.Errorf("maxObjectsPerAction = %d, want the ceiling %d", got.MaxObjectsPerAction, CeilingMaxObjectsPerAction)
	}
	if got.FlapThreshold != CeilingFlapThreshold {
		t.Errorf("flapThreshold = %d, want the ceiling %d", got.FlapThreshold, CeilingFlapThreshold)
	}

	// A negative leaf becomes 0, not the default: "-1 actions per hour" cannot be an authority grant,
	// and the nearest coherent reading is "none".
	if got.SelfInitiated.ElevatedPerHour != 0 {
		t.Errorf("a negative elevatedPerHour resolved to %d, want 0", got.SelfInitiated.ElevatedPerHour)
	}

	// The flap window is the one leaf clamped UP, because short is the loosening direction: a
	// one-second window lets a flapping agent reset its own counter between every action.
	if got.FlapWindow != FloorFlapWindow {
		t.Errorf("a one-second flapWindow resolved to %s, want the %s floor", got.FlapWindow, FloorFlapWindow)
	}
}

// TestEffectiveInitiativeBudgetAcceptsAValidNarrowing proves the clamps are not simply pinning
// everything to the ceiling -- a legitimate tightening survives intact.
func TestEffectiveInitiativeBudgetAcceptsAValidNarrowing(t *testing.T) {
	a := &Agent{Spec: AgentSpec{Operations: &OperationsSpec{InitiativeBudget: &InitiativeBudgetSpec{
		SelfInitiated: &BudgetClassSpec{RoutinePerHour: i32(4), ActionsPerDay: i32(10)},
		FlapThreshold: i32(2),
		FlapWindow:    &metav1.Duration{Duration: time.Hour},
	}}}}
	got := a.EffectiveInitiativeBudget()

	if got.SelfInitiated.RoutinePerHour != 4 || got.SelfInitiated.ActionsPerDay != 10 {
		t.Errorf("narrowed leaves resolved to %d/hour and %d/day, want 4 and 10",
			got.SelfInitiated.RoutinePerHour, got.SelfInitiated.ActionsPerDay)
	}
	if got.FlapThreshold != 2 {
		t.Errorf("flapThreshold = %d, want the narrowed 2", got.FlapThreshold)
	}
	if got.FlapWindow != time.Hour {
		t.Errorf("flapWindow = %s, want the widened 1h", got.FlapWindow)
	}
	// The untouched origin keeps its whole default row.
	if got.HumanRequested != DefaultHumanRequestedBudget {
		t.Errorf("humanRequested = %+v, want the untouched default %+v", got.HumanRequested, DefaultHumanRequestedBudget)
	}
}

// TestForSelectsTheOriginRow, including the unknown-origin arm, which must be the tighter one for
// the same reason BudgetOriginFor's default arm is.
func TestForSelectsTheOriginRow(t *testing.T) {
	r := (&Agent{}).EffectiveInitiativeBudget()
	if got := r.For(OriginSelfInitiated); got != DefaultSelfInitiatedBudget {
		t.Errorf("For(selfInitiated) = %+v, want %+v", got, DefaultSelfInitiatedBudget)
	}
	if got := r.For(OriginHumanRequested); got != DefaultHumanRequestedBudget {
		t.Errorf("For(humanRequested) = %+v, want %+v", got, DefaultHumanRequestedBudget)
	}
	if got := r.For("nonsense"); got != DefaultSelfInitiatedBudget {
		t.Errorf("For(unknown origin) = %+v, want the tighter selfInitiated row %+v", got, DefaultSelfInitiatedBudget)
	}
}
