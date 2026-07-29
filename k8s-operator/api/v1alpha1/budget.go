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

import "time"

// The 06 §1.1 initiative-budget table, as the one place Go holds it.
//
// # Why the table is here and not in the webhook
//
// Until this file existed only the CEILINGS were in Go, transcribed as literals inside
// internal/webhook/agent_webhook.go, and the DEFAULTS were in no Go file at all -- they lived in
// the 06 §1.1 markdown table and nowhere else. That was survivable while nothing read a budget.
// The accountant (internal/broker/budget) reads one on every action, so the defaults had to enter
// Go somewhere, and putting them beside a second copy of the ceilings would have created exactly
// the drift the ceilings' own comment warns about: two transcriptions of one spec table, edited on
// different days, disagreeing about what an agent is allowed to do. The webhook now imports these.
//
// # Rejects at admission, clamps at runtime -- and the asymmetry is deliberate
//
// 06 §1.2 V-8 REJECTS a leaf above its ceiling rather than clamping it, because a clamp lets an
// operator who asked for 500 elevated actions per hour believe they got them. [ApprovalRoster.EffectiveTTL]
// documents the same split from the other side, and [Agent.EffectiveInitiativeBudget] is the runtime
// half here: admission refuses an over-ceiling leaf, and this clamps one that got in anyway --
// through a CR that predates the rule, a webhook that was down, or a direct etcd write. A runtime
// that trusted the leaf would let any of those three become an authority grant.

// BudgetOrigin is the origin half of the 06 §1.1 two-dimensional cap: origin × risk class.
//
// It is DERIVED from `spec.trigger.source` and never asserted separately, which is 06 §1.1's own
// rule ("Origin is derived from trigger.source in the envelope, never asserted separately"). A
// field an agent could set would be a field an agent could use to spend the human allowance.
type BudgetOrigin string

const (
	// OriginSelfInitiated is work the agent started itself: watch, alert, cron, delegation,
	// escalation. A `delegation` from a parent spends the CALLEE's self-initiated bucket (06 §7
	// rule 8) -- a chatty parent must not be able to spend a child's human allowance.
	OriginSelfInitiated BudgetOrigin = "selfInitiated"

	// OriginHumanRequested is work a human asked for: chat and undo. A separate, larger allowance,
	// because a human in the loop is itself a control.
	OriginHumanRequested BudgetOrigin = "humanRequested"
)

// BudgetOriginFor partitions a trigger source into its origin bucket (06 §1.1).
//
// The default arm is `selfInitiated` and that direction is the whole point: an unrecognised trigger
// source -- one added to the enum in a later phase and not taught to this function -- draws on the
// TIGHTER allowance. The other default would hand a new trigger the human-requested budget by
// omission.
func BudgetOriginFor(src ActionTriggerSource) BudgetOrigin {
	switch src {
	case "chat", ActionTriggerUndo:
		return OriginHumanRequested
	default:
		return OriginSelfInitiated
	}
}

// BudgetClassLimits is one origin's resolved allowance -- the 06 §1.1 row, with defaults applied and
// ceilings enforced. Plain int32s rather than pointers: this is the answer, not the request.
type BudgetClassLimits struct {
	// RoutinePerHour caps `routine` actions per rolling hour.
	RoutinePerHour int32
	// ElevatedPerHour caps `elevated` actions per rolling hour.
	ElevatedPerHour int32
	// GatedPerHour caps `gated` SUBMISSIONS per rolling hour; approval consumes nothing.
	GatedPerHour int32
	// ActionsPerDay caps all classes together per rolling 24 h.
	ActionsPerDay int32
}

// The 06 §1.1 table, one variable per cell group. Defaults and ceilings are deliberately the same
// shape so a reader can compare them column by column, and so [Agent.EffectiveInitiativeBudget] can
// resolve a leaf with one expression instead of a switch.
var (
	// DefaultSelfInitiatedBudget is the 06 §1.1 `selfInitiated` default row: 30/6/3 per hour, 200
	// per day.
	DefaultSelfInitiatedBudget = BudgetClassLimits{RoutinePerHour: 30, ElevatedPerHour: 6, GatedPerHour: 3, ActionsPerDay: 200}

	// CeilingSelfInitiatedBudget is the 06 §1.1 `selfInitiated` code ceiling: 50/10/5 per hour, 500
	// per day. A CR leaf above any of these is rejected by 06 §1.2 V-8, not clamped.
	CeilingSelfInitiatedBudget = BudgetClassLimits{RoutinePerHour: 50, ElevatedPerHour: 10, GatedPerHour: 5, ActionsPerDay: 500}

	// DefaultHumanRequestedBudget is the 06 §1.1 `humanRequested` default row: 120/40/20 per hour,
	// 800 per day.
	DefaultHumanRequestedBudget = BudgetClassLimits{RoutinePerHour: 120, ElevatedPerHour: 40, GatedPerHour: 20, ActionsPerDay: 800}

	// CeilingHumanRequestedBudget is the 06 §1.1 `humanRequested` code ceiling: 200/60/30 per hour,
	// 2000 per day.
	CeilingHumanRequestedBudget = BudgetClassLimits{RoutinePerHour: 200, ElevatedPerHour: 60, GatedPerHour: 30, ActionsPerDay: 2000}
)

const (
	// DefaultMaxObjectsPerAction is the 06 §1.1 default per-envelope object cap.
	DefaultMaxObjectsPerAction int32 = 25

	// CeilingMaxObjectsPerAction is 50, which is where the 06 §4.2 code floor gates regardless -- so
	// a higher value is meaningless and is rejected rather than accepted-and-ignored.
	CeilingMaxObjectsPerAction int32 = 50

	// DefaultFlapThreshold is the 06 §1.1 default: repeats of the same target within FlapWindow
	// before the flap brake trips.
	DefaultFlapThreshold int32 = 3

	// CeilingFlapThreshold is 5.
	CeilingFlapThreshold int32 = 5

	// DefaultFlapWindow is the 06 §1.1 default flap window.
	DefaultFlapWindow = 30 * time.Minute

	// FloorFlapWindow is the 5-minute code FLOOR -- the one budget leaf where SMALLER is the
	// dangerous direction, because a short window lets a flapping agent reset its own counter.
	// Enforced as a floor at admission (06 §1.2 V-8) and clamped up here.
	FloorFlapWindow = 5 * time.Minute
)

// ResolvedInitiativeBudget is `spec.operations.initiativeBudget` with every leaf answered: defaults
// applied where the CR is silent, ceilings and the flap-window floor enforced where it is not.
//
// The accountant consumes this rather than the raw [InitiativeBudgetSpec] so that "what is this
// agent allowed to do" is computed in one place. A caller that dereferenced the spec pointers itself
// would have to re-implement the defaults, and the copy that forgot one would fail OPEN -- a nil
// `elevatedPerHour` read as zero is a bucket that refuses everything, and read as unlimited is a
// bucket that refuses nothing.
type ResolvedInitiativeBudget struct {
	// SelfInitiated is the tighter allowance.
	SelfInitiated BudgetClassLimits
	// HumanRequested is the larger one.
	HumanRequested BudgetClassLimits
	// MaxObjectsPerAction is the per-envelope object cap.
	MaxObjectsPerAction int32
	// FlapWindow is the window repeats are counted in.
	FlapWindow time.Duration
	// FlapThreshold is how many applications of one target inside FlapWindow trip the flap brake.
	FlapThreshold int32
}

// For returns the limits for one origin.
func (r ResolvedInitiativeBudget) For(origin BudgetOrigin) BudgetClassLimits {
	if origin == OriginHumanRequested {
		return r.HumanRequested
	}
	return r.SelfInitiated
}

// PerHour returns the hourly cap for one risk class, and whether that class has an hourly bucket at
// all.
//
// `forbidden` has no bucket in the 06 §1.1 table and the second return value says so rather than
// answering zero. The difference matters: zero would mean "this agent may perform no forbidden
// actions per hour", which reads as a budget refusal, when the truth is that a forbidden action is
// refused by 06 §4.4 row 3 long before row 7 is consulted and never reaches a bucket to charge.
func (l BudgetClassLimits) PerHour(class ActionRiskClass) (int32, bool) {
	switch class {
	case RiskRoutine:
		return l.RoutinePerHour, true
	case RiskElevated:
		return l.ElevatedPerHour, true
	case RiskGated:
		return l.GatedPerHour, true
	default:
		return 0, false
	}
}

// EffectiveInitiativeBudget resolves 06 §1.1 for this Agent.
//
// # An explicit zero is honoured; an absent leaf is defaulted
//
// The leaves are pointers, and the two states a pointer distinguishes carry different meanings here.
// Nil means the CR is silent, so the 06 §1.1 default applies. A pointer to 0 means an operator wrote
// `elevatedPerHour: 0`, which is a real and useful configuration -- an agent allowed no elevated
// self-initiated work at all -- and defaulting it to 6 would silently hand back authority that was
// deliberately withheld. This is where the function differs from [ApprovalRoster.EffectiveTTL],
// which treats a zero duration as unset: a zero TTL is meaningless, a zero allowance is not.
//
// A negative leaf is clamped to 0 rather than defaulted, for the same reason: it cannot be an
// authority grant, and the nearest coherent reading of "-1 actions per hour" is "none".
func (a *Agent) EffectiveInitiativeBudget() ResolvedInitiativeBudget {
	out := ResolvedInitiativeBudget{
		SelfInitiated:       DefaultSelfInitiatedBudget,
		HumanRequested:      DefaultHumanRequestedBudget,
		MaxObjectsPerAction: DefaultMaxObjectsPerAction,
		FlapWindow:          DefaultFlapWindow,
		FlapThreshold:       DefaultFlapThreshold,
	}
	if a == nil || a.Spec.Operations == nil || a.Spec.Operations.InitiativeBudget == nil {
		return out
	}
	b := a.Spec.Operations.InitiativeBudget

	out.SelfInitiated = resolveClass(b.SelfInitiated, DefaultSelfInitiatedBudget, CeilingSelfInitiatedBudget)
	out.HumanRequested = resolveClass(b.HumanRequested, DefaultHumanRequestedBudget, CeilingHumanRequestedBudget)
	out.MaxObjectsPerAction = resolveLeaf(b.MaxObjectsPerAction, DefaultMaxObjectsPerAction, CeilingMaxObjectsPerAction)
	out.FlapThreshold = resolveLeaf(b.FlapThreshold, DefaultFlapThreshold, CeilingFlapThreshold)

	if b.FlapWindow != nil {
		// The one leaf clamped UP. A window shorter than the floor is the loosening direction, so
		// the runtime raises it to the floor exactly as admission refuses it.
		out.FlapWindow = b.FlapWindow.Duration
		if out.FlapWindow < FloorFlapWindow {
			out.FlapWindow = FloorFlapWindow
		}
	}
	return out
}

// resolveClass resolves one origin's four leaves against its own default and ceiling rows.
func resolveClass(spec *BudgetClassSpec, def, ceiling BudgetClassLimits) BudgetClassLimits {
	if spec == nil {
		return def
	}
	return BudgetClassLimits{
		RoutinePerHour:  resolveLeaf(spec.RoutinePerHour, def.RoutinePerHour, ceiling.RoutinePerHour),
		ElevatedPerHour: resolveLeaf(spec.ElevatedPerHour, def.ElevatedPerHour, ceiling.ElevatedPerHour),
		GatedPerHour:    resolveLeaf(spec.GatedPerHour, def.GatedPerHour, ceiling.GatedPerHour),
		ActionsPerDay:   resolveLeaf(spec.ActionsPerDay, def.ActionsPerDay, ceiling.ActionsPerDay),
	}
}

// resolveLeaf applies the nil-is-default / explicit-zero-is-honoured / clamp-to-ceiling rule to one
// leaf.
func resolveLeaf(v *int32, def, ceiling int32) int32 {
	if v == nil {
		return def
	}
	switch {
	case *v < 0:
		return 0
	case *v > ceiling:
		return ceiling
	default:
		return *v
	}
}
