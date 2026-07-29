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
	"fmt"
	"net/http"
	"sort"
	"strings"
	"sync"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The brake (06 §4.4), as one decision function.
//
// 06 §4.4 has two tables. The first names five controls -- pause, resume, freeze, undo, contested
// -- and the second names nine conditions under which the broker must fail CLOSED. This file is
// both, because they are the same decision: every one of them ends in "does this envelope
// execute?", and nine conditions spread across nine call sites is nine chances to add a tenth call
// site that consults none of them.
//
// THE PROPERTY THAT MATTERS IS THAT THE ZERO VALUE REFUSES. `Decide(BrakeInputs{})` refuses, names
// a rule, and says why. That is not a nicety: every input here is a thing the broker LOOKED UP, and
// the realistic failure is not a wrong answer but an unasked question -- a new caller that
// populates six of the eight fields, or a field added to BrakeInputs in a later phase that an
// existing caller does not know to set. Go gives that caller a zero value silently. So every input
// is typed such that its zero value means "nobody has told me", and every rule reads "nobody has
// told me" as the unsafe case. The one deliberate exception is the budget, and it is argued at its
// field.
//
// WHAT IS NOT HERE. The brake decides; it does not act. Auto-pausing the agent (rows 3 and 9)
// means patching `spec.operations.paused`, which is the controller's job and is P9-T6c's and
// P9-T7's wiring; this package returns `AutoPause: true` and the caller does the patch. Keeping
// that seam is what lets the whole of 06 §4.4 be tested with no cluster, and it is why the
// nine-row table is transcribed once, in one file, rather than distributed across the controllers
// that respond to it.
//
// WHAT THE BRAKE MUST NOT DEPEND ON. 03 §6 and 06 §4.4 both require all five controls to work with
// the LLM, the router and the inference stack unavailable. This file imports the API types, the
// standard library, and nothing else. There is no client here, no chat surface, and no model: the
// inputs arrive already read, so a brake decision is a pure function of what the broker last
// managed to observe.

// MaxFreezeStaleness is 06 §4.4's 30 seconds: a FleetFreeze cache older than this is treated as
// unreadable, and an unreadable freeze list freezes the scope.
//
// The staleness clause is the more important half of that row, and the easier one to drop. An API
// error is loud and every implementation handles it. A watch that silently stopped delivering is
// not an error at all -- the informer's List succeeds, the cache answers instantly, and every
// answer is from before the incident started. That is the shape of "the freeze did not take
// effect" that nobody notices, because everything is green.
const MaxFreezeStaleness = 30 * time.Second

// PausedRetryAfterSeconds is what a paused agent's broker tells a caller to wait.
//
// 06 §4.4 requires `retryAfterSeconds` on the pause refusal and names no number, so this is a
// harness choice (recorded in the ledger). 60 s is short enough that the first retry after a human
// types `resume` lands promptly -- the broker learns of the resume from its informer, so the only
// latency the caller sees is this interval -- and long enough that an agent with a full work queue
// refusing on every item does not turn a pause into a request flood against the thing an operator
// is currently trying to calm down. A freeze answers with its own expiry instead, when it has one.
const PausedRetryAfterSeconds = 60

// MaxRetryAfterSeconds caps a computed retry-after at one hour. A freeze may legitimately expire in
// three days; telling a caller to sleep for three days converts a cleared freeze into an agent that
// is still, as far as anyone can see, frozen.
const MaxRetryAfterSeconds = 3600

// BrakeStage is where in the pipeline the brake is being consulted.
//
// Seven of the nine fail-closed rows are answerable at the gate -- V-BRK-011 fixes the pipeline
// order as classify ≺ gate ≺ snapshot ≺ execute, so by the time the gate runs, classification and
// undo-plan generation have already happened and their outcomes are inputs. Two rows are not: the
// snapshot has not been attempted yet at the gate (row 4), and row 9 is about an action that has
// already executed. Rather than pretend those are gate inputs -- which would mean modelling "not
// attempted yet" as a value that permits, and losing the zero-value-refuses property for exactly
// the two rows that fire when things have already gone wrong -- the stage is explicit.
//
// StageGate is the zero value on purpose: a caller who forgets to set the stage gets the strictest
// evaluation, not the most permissive.
type BrakeStage uint8

const (
	// StageGate is step 5 of the pipeline: the last point at which nothing has been written.
	StageGate BrakeStage = iota
	// StageSnapshot is after the pre-state snapshot was attempted and before any apply (row 4).
	StageSnapshot
	// StagePostExecute is after the write, when verification and rollback have been attempted
	// (row 9).
	StagePostExecute
)

// String names the stage for refusal detail and test failure output.
func (s BrakeStage) String() string {
	switch s {
	case StageGate:
		return "gate"
	case StageSnapshot:
		return "snapshot"
	case StagePostExecute:
		return "post-execute"
	default:
		return fmt.Sprintf("unknown-stage(%d)", uint8(s))
	}
}

// BrakeSignal is a three-valued "did this work?", whose zero value is the one that refuses.
//
// The same shape as verify.CapacitySignal and for the same reason: a caller who did not look must
// not be indistinguishable from a caller who looked and found everything fine. Two-valued booleans
// are how "the journal is reachable" and "nobody asked the journal" become the same answer.
type BrakeSignal uint8

const (
	// BrakeUnobserved is the zero value: nobody has answered this question.
	BrakeUnobserved BrakeSignal = iota
	// BrakeOK means the caller looked and it worked.
	BrakeOK
	// BrakeFailed means the caller looked and it did not.
	BrakeFailed
)

// ok is true only for an affirmative observation. Unobserved is not ok, which is the whole point.
func (s BrakeSignal) ok() bool { return s == BrakeOK }

// String names the signal for refusal detail.
func (s BrakeSignal) String() string {
	switch s {
	case BrakeOK:
		return "ok"
	case BrakeFailed:
		return "failed"
	default:
		return "unobserved"
	}
}

// BrakeEffect is what the broker does about the decision.
//
// Its zero value is the empty string, which is not `BrakeAllow`. A `BrakeDecision{}` that somehow
// reached a caller is therefore not an allow, and `Allowed()` is the only way to ask.
type BrakeEffect string

const (
	// BrakeAllow: nothing in 06 §4.4 stops this envelope.
	BrakeAllow BrakeEffect = "Allow"
	// BrakeRefuse: the envelope does not execute, and Refusal says what the caller is told.
	BrakeRefuse BrakeEffect = "Refuse"
	// BrakeRaiseToGated: row 5. The action is not refused; it becomes `gated` and waits for a
	// human, because executing something the broker cannot undo is the thing gating exists for.
	BrakeRaiseToGated BrakeEffect = "RaiseToGated"
	// BrakePark: row 6. A gated action with no usable roster stays PendingApproval and expires. It
	// is NOT a refusal (the caller is not told no) and it is emphatically not an approval.
	BrakePark BrakeEffect = "Park"
	// BrakeHalt: row 9. The write landed, verification failed, rollback failed. There is nothing
	// left to refuse; what the brake does is stop the agent and wake a human.
	BrakeHalt BrakeEffect = "Halt"
)

// BrakeRule identifies which row of 06 §4.4 produced the decision.
//
// Eleven constants for two tables: the nine fail-closed rows, plus `paused` and `frozen`, which are
// controls rather than degraded states. They are distinguished because an operator reading a
// refusal needs to know whether the brake was PULLED or whether the broker could not see well
// enough to be sure -- those have different remedies, and the second one is an incident.
type BrakeRule string

const (
	// BrakeRuleNone is the zero value and means no rule fired. Only valid alongside BrakeAllow.
	BrakeRuleNone BrakeRule = ""

	// --- the five controls (06 §4.4, first table) ---

	// BrakeRulePaused is `spec.operations.paused: true`.
	BrakeRulePaused BrakeRule = "paused"
	// BrakeRuleFrozen is a FleetFreeze covering this scope.
	BrakeRuleFrozen BrakeRule = "frozen"

	// --- the nine fail-closed rows, in table order ---

	// BrakeRuleFreezeUnreadable is row 1: cannot read the FleetFreeze list, or the cache is stale
	// beyond MaxFreezeStaleness.
	BrakeRuleFreezeUnreadable BrakeRule = "freeze-unreadable"
	// BrakeRuleAgentUnreadable is row 2: cannot read its own Agent CR.
	BrakeRuleAgentUnreadable BrakeRule = "agent-unreadable"
	// BrakeRuleJournalUnreachable is row 3: cannot reach the journal store.
	BrakeRuleJournalUnreachable BrakeRule = "journal-unreachable"
	// BrakeRuleSnapshotFailed is row 4: cannot persist a pre-state snapshot.
	BrakeRuleSnapshotFailed BrakeRule = "snapshot-failed"
	// BrakeRuleUndoPlanUnusable is row 5: cannot generate or validate an undo plan.
	BrakeRuleUndoPlanUnusable BrakeRule = "undo-plan-unusable"
	// BrakeRuleRosterUnusable is row 6: the approval roster is missing or empty while a gated
	// action waits.
	BrakeRuleRosterUnusable BrakeRule = "roster-unusable"
	// BrakeRuleBudgetExhausted is row 7, first half: the initiative budget is spent.
	BrakeRuleBudgetExhausted BrakeRule = "budget-exhausted"
	// BrakeRuleFlapBreached is row 7, second half: the flap threshold was crossed.
	BrakeRuleFlapBreached BrakeRule = "flap-breached"
	// BrakeRuleTargetContested is row 8: a target carries a contested marker.
	BrakeRuleTargetContested BrakeRule = "target-contested"
	// BrakeRuleUnverifiedUnrolled is row 9: executed, cannot verify, cannot roll back.
	BrakeRuleUnverifiedUnrolled BrakeRule = "unverified-unrolled"
)

// BrakeDecision is the answer, with every side effect the caller owes named on it.
//
// AutoPause, Page and Escalate are fields rather than something the caller infers from the rule,
// for the same reason Refusal carries Journal and SecurityEvent: 06 §4.4 assigns them per row, and
// a switch at the call site drifts from the table the first time a row is added.
type BrakeDecision struct {
	// Effect is what happens to the envelope.
	Effect BrakeEffect
	// Rule is the row that decided. BrakeRuleNone only with BrakeAllow.
	Rule BrakeRule
	// Refusal is what the caller is told. Non-nil exactly when Effect is BrakeRefuse.
	Refusal *Refusal
	// AutoPause means the caller must patch `spec.operations.paused: true` (rows 3 and 9).
	AutoPause bool
	// Page means wake a human now, not at the next report (row 9).
	Page bool
	// Escalate means hand this to a human through the normal escalation path (row 7).
	Escalate bool
	// JournalReachable is mirrored into `status.broker.journalReachable` (row 3 says so
	// explicitly). Reported on every decision, not only the refusing one, so the status field is
	// driven by an observation rather than by the last time something broke.
	JournalReachable bool
	// Detail is the human-readable why, also used as the Refusal detail when there is one.
	Detail string
}

// Allowed is the only way to ask whether an envelope may proceed.
//
// A method rather than a bare field comparison so that a zero BrakeDecision -- the value a caller
// gets from a function that returned early, or from a struct nobody filled in -- answers false.
func (d BrakeDecision) Allowed() bool { return d.Effect == BrakeAllow }

// BrakeBudget is row 7's answer: the initiative budget and flap counters (04 §4.2, 06 §1.1).
//
// THIS IS THE ONE BRAKE VALUE WHOSE ZERO PERMITS, and the exception is deliberate. Every other
// input models a lookup that can fail, so "not observed" means "the broker is blind" and blindness
// refuses. Spend counters are not a lookup: they are the broker's own tally, and a zero tally means
// an agent that has done nothing yet. Refusing on a zero tally would make a freshly started broker
// refuse everything until something incremented a counter, which is not fail-closed, it is
// fail-stopped.
//
// The blindness case is therefore pushed one level out, to [Accountant]: a zero BrakeBudget is a
// tally, a nil Accountant is a broker with nobody counting, and only the second refuses. Keeping
// those two as the same value is what made the hole this type used to carry -- an unwired budget
// and a quiet agent were literally indistinguishable.
type BrakeBudget struct {
	// Exhausted means the class budget for this action's origin is spent.
	Exhausted bool
	// FlapBreached means the same target crossed the flap threshold inside the flap window.
	FlapBreached bool
	// Detail is a short human-readable summary, e.g. "3/3 elevated self-initiated this hour".
	Detail string
}

// BudgetQuery is what row 7 asks the accountant about ONE action.
//
// 04 §4.2 does not budget an agent, it budgets an agent's {origin, class} bucket, and the flap
// threshold is per target. So the question cannot be answered from an agent-level observation taken
// before the envelope was classified -- it needs the envelope. That is why the accountant is a
// dependency queried at decision time rather than a value gathered by [pipeline.BrakeSource], and
// it is the same reason [ContestedIndex] is not gathered there either.
type BudgetQuery struct {
	// Agent carries `spec.operations.initiativeBudget`, the ceiling half of the question. Nil is
	// possible in principle and moot in practice: row 2 has already refused an unreadable Agent
	// before row 7 is reached.
	Agent *agentv1alpha1.Agent
	// Trigger is the action's origin. 06 §1.1 partitions the budget by it -- self-initiated work
	// gets a fraction of what a human asks for -- and exempts `undo` from the hourly buckets.
	Trigger agentv1alpha1.ActionTriggerSource
	// Class is the risk class. Each origin has a per-class bucket.
	Class agentv1alpha1.ActionRiskClass
	// Targets are what the flap threshold is counted against.
	Targets []agentv1alpha1.TargetRef
	// Now is the evaluation time, so the accountant and the rest of the brake agree on which hour
	// this is.
	Now time.Time
}

// Accountant answers row 7: is this action inside the 04 §4.2 initiative budget, and is it flapping?
//
// NO CONTEXT, NO ERROR, NO CLIENT -- and that is a constraint, not an oversight. [Decide] is a pure
// function of its inputs, which is what makes the brake evaluable when the cluster is the thing
// that is broken; an implementation that reached out to the API server from inside row 7 would make
// the brake's own availability depend on the availability it exists to survive. Implementations
// keep a snapshot refreshed out of band and serve it from memory, the way [ContestedIndex] does.
//
// An implementation that cannot answer returns a BrakeBudget saying so -- it does not get to
// signal absence, because absence is modelled by the interface value itself being nil, which
// refuses. See [BrakeInputs.Accountant].
type Accountant interface {
	Budget(q BudgetQuery) BrakeBudget
}

// FreezeView is the observed FleetFreeze list, plus WHEN it was observed.
//
// The timestamp is not decoration: 06 §4.4's row 1 makes staleness beyond 30 s equivalent to an API
// error, and a list with no observation time cannot answer that. A nil FreezeView means the read
// failed; a FreezeView with a zero ObservedAt is treated as infinitely stale, so the field cannot
// be forgotten into permissiveness.
type FreezeView struct {
	// Freezes is every FleetFreeze the broker can see. Cluster-scoped, so this is all of them.
	Freezes []agentv1alpha1.FleetFreeze
	// ObservedAt is when the underlying cache last synced.
	ObservedAt time.Time
}

// stale reports whether this view is too old to be trusted at `now`.
func (v *FreezeView) stale(now time.Time) bool {
	if v == nil {
		return true
	}
	if v.ObservedAt.IsZero() {
		return true
	}
	return now.Sub(v.ObservedAt) > MaxFreezeStaleness
}

// BrakeInputs is everything 06 §4.4 needs, already read.
//
// Nothing in this struct is a client, a context or a callback. That is what makes the brake
// testable without a cluster and, more importantly, what makes it evaluable when the cluster is the
// thing that is broken: the caller reads what it can, says honestly what it could not, and the
// decision is a pure function of that.
type BrakeInputs struct {
	// Stage is which of the three consultation points this is. Zero value is StageGate.
	Stage BrakeStage
	// Now is the evaluation time; freeze staleness and expiry are measured against it. A zero Now
	// makes every freeze view stale, which refuses.
	Now time.Time

	// Agent is the broker's own Agent CR. NIL MEANS IT COULD NOT BE READ (row 2).
	Agent *agentv1alpha1.Agent
	// Scope is the agent's scope, for matching freezes. Nil means unknown, and FleetFreeze.Covers
	// treats an unknown scope as covered.
	Scope *agentv1alpha1.ScopeSpec
	// Freezes is the observed freeze list. NIL MEANS THE READ FAILED (row 1).
	Freezes *FreezeView

	// Journal is journal-store reachability (row 3).
	Journal BrakeSignal
	// Snapshot is whether the pre-state snapshot persisted. Read only at StageSnapshot (row 4).
	Snapshot BrakeSignal
	// UndoPlan is whether an undo plan was generated AND validated (row 5).
	UndoPlan BrakeSignal
	// Verified is whether the executed action was verified. Read only at StagePostExecute (row 9).
	Verified BrakeSignal
	// RolledBack is whether rollback succeeded, when verification did not. Read only at
	// StagePostExecute (row 9).
	RolledBack BrakeSignal

	// Roster is the resolved ApprovalRoster for this agent. Nil means the reference dangles or
	// there is none (row 6).
	Roster *agentv1alpha1.ApprovalRoster
	// Accountant answers row 7. NIL MEANS NOBODY IS COUNTING, which refuses -- distinct from a
	// zero BrakeBudget, which means a tally of nothing and permits. See Accountant and BrakeBudget
	// for why those two had to stop being the same value.
	Accountant Accountant
	// Contested is the contested index. NIL MEANS THE INDEX IS UNAVAILABLE, which refuses -- a
	// broker that cannot tell whether a target is contested is in exactly the position row 1
	// describes for freezes.
	Contested *ContestedIndex

	// Class is the risk class the classifier already assigned. Consulted by the freeze
	// (`allowClasses`) and by row 6 (`gated` waits for a roster).
	Class agentv1alpha1.ActionRiskClass
	// Targets are the resolved targets, checked against the contested index.
	Targets []agentv1alpha1.TargetRef
	// Trigger is the envelope's origin. Two rows read it: the undo carve-out (see IsUndo and
	// undoExempt) and row 7's origin partition (see BudgetQuery). One field rather than a bool per
	// reader, so the two cannot disagree about the same envelope.
	Trigger agentv1alpha1.ActionTriggerSource
}

// IsUndo reports whether this envelope is an undo. See undoExempt for what that exempts and, more
// importantly, what it does not.
//
// Derived rather than stored: it used to be its own bool, which meant a caller could set the
// trigger and forget the flag, or set the flag on an envelope whose trigger said otherwise. The
// only honest source is the trigger, so there is only the trigger ([[LSN-031]] -- import the
// encoding, never restate it).
func (in BrakeInputs) IsUndo() bool { return in.Trigger == agentv1alpha1.ActionTriggerUndo }

// Decide evaluates 06 §4.4 and returns what the broker must do.
//
// ORDER. The three degraded-input rows (1, 2, 3) are evaluated before the two controls (paused,
// frozen), which are evaluated before the remaining condition rows (5, 6, 7, 8). The table's own
// order is followed inside each group. That grouping is a choice and it is the one place a reader
// might reasonably want a different answer, so: when several rows fire at once every one of them
// refuses, and the only thing the order decides is WHICH REASON THE HUMAN IS TOLD. Told
// "agent-paused" when the truth is that the broker cannot read the freeze list, an operator goes
// and looks at the agent, finds it running normally, and concludes the brake is broken. Told
// "freeze-unreadable", they go and look at the thing that is actually wrong. Degraded inputs are
// reported first because they are the only rows that are themselves incidents.
//
// Row 2 keeps the caller-visible reason `agent-paused` even though its Rule is
// `agent-unreadable`, because 06 §4.4 says to TREAT the agent as paused -- a caller must not be
// able to distinguish the two and back off differently. The truth is in the Rule and the Detail,
// which is where an operator looks and a client does not.
func Decide(in BrakeInputs) BrakeDecision {
	journalOK := in.Journal.ok()

	switch in.Stage {
	case StageSnapshot:
		return decideSnapshot(in, journalOK)
	case StagePostExecute:
		return decidePostExecute(in, journalOK)
	}
	return decideGate(in, journalOK)
}

// decideGate is the pre-execution consultation: everything except rows 4 and 9.
func decideGate(in BrakeInputs, journalOK bool) BrakeDecision {
	// --- degraded inputs: rows 1, 2, 3 ---

	if in.Freezes.stale(in.Now) && !in.IsUndo() {
		// Row 1. Undo is exempt BY THE SPEC's own words here ("refuse everything except undo"),
		// which is also the reading that keeps the fleet recoverable: the state an operator most
		// needs to reverse is the state where the control plane is unwell.
		return refuseBrake(in, BrakeRuleFreezeUnreadable, ReasonScopeFrozen, http.StatusForbidden,
			freezeUnreadableDetail(in), journalOK, PausedRetryAfterSeconds)
	}
	if in.Agent == nil && !undoExempt(in) {
		// Row 2.
		return refuseBrake(in, BrakeRuleAgentUnreadable, ReasonAgentPaused, http.StatusForbidden,
			"the broker cannot read its own Agent CR and is treating the agent as paused (06 §4.4)",
			journalOK, PausedRetryAfterSeconds)
	}
	if !journalOK {
		// Row 3. Not exempted for undo: 05 §1.2 makes the journal the durable record of every
		// action, and an undo is a first-class action. An unjournaled undo is an unrecorded write.
		d := refuseBrake(in, BrakeRuleJournalUnreachable, ReasonJournalUnavailable, http.StatusServiceUnavailable,
			"the journal store is "+in.Journal.String()+"; nothing executes unjournaled (06 §4.4), and the agent is being paused",
			journalOK, PausedRetryAfterSeconds)
		d.AutoPause = true
		return d
	}

	// --- the controls: paused, frozen ---

	if paused, reason := agentPaused(in.Agent); paused && !undoExempt(in) {
		detail := "the agent is paused (spec.operations.paused)"
		if reason != "" {
			detail += ": " + reason
		}
		return refuseBrake(in, BrakeRulePaused, ReasonAgentPaused, http.StatusForbidden,
			detail, journalOK, PausedRetryAfterSeconds)
	}
	if f := blockingFreeze(in); f != nil {
		return refuseBrake(in, BrakeRuleFrozen, ReasonScopeFrozen, http.StatusForbidden,
			frozenDetail(f), journalOK, freezeRetryAfter(f, in.Now))
	}

	// --- the remaining condition rows: 5, 6, 7, 8 ---

	if in.UndoPlan == BrakeFailed || in.UndoPlan == BrakeUnobserved {
		// Row 5. NOT a refusal: the action becomes gated and waits for a human. An unobserved
		// plan counts as a missing one -- "nobody generated a plan" and "the plan would not
		// validate" put the operator in the same position, which is being asked to accept a write
		// nothing can reverse.
		return BrakeDecision{
			Effect:           BrakeRaiseToGated,
			Rule:             BrakeRuleUndoPlanUnusable,
			JournalReachable: journalOK,
			Detail: "the undo plan is " + in.UndoPlan.String() +
				"; the action is raised to gated rather than executed on a hope (06 §4.4)",
		}
	}
	if in.Class == agentv1alpha1.RiskGated && !rosterUsable(in.Roster) {
		// Row 6. Also not a refusal. The action parks as PendingApproval and expires at the TTL;
		// the one outcome forbidden here is auto-approval, and the way that arrives in a codebase
		// is a well-meaning "there is nobody to ask, so proceed".
		return BrakeDecision{
			Effect:           BrakePark,
			Rule:             BrakeRuleRosterUnusable,
			JournalReachable: journalOK,
			Detail: "this action is gated and " + rosterDetail(in.Roster) +
				"; it stays PendingApproval and expires unapproved -- it is never auto-approved (06 §4.4)",
		}
	}
	if d, overBudget := budgetRefusal(in, journalOK); overBudget {
		// Row 7.
		return d
	}
	if d, contested := contestedRefusal(in, journalOK); contested {
		// Row 8.
		return d
	}

	return BrakeDecision{Effect: BrakeAllow, Rule: BrakeRuleNone, JournalReachable: journalOK}
}

// decideSnapshot is row 4, evaluated after the pre-state snapshot was attempted.
//
// It re-checks nothing from the gate. That is deliberate: re-running the gate here would mean an
// envelope could be admitted, snapshotted, and then refused for a freeze that arrived in between --
// which sounds safer and is not, because the snapshot has already been persisted and the caller
// would be told a reason that has nothing to do with why their action stopped. The gate is the gate.
func decideSnapshot(in BrakeInputs, journalOK bool) BrakeDecision {
	if in.Snapshot.ok() {
		return BrakeDecision{Effect: BrakeAllow, Rule: BrakeRuleNone, JournalReachable: journalOK}
	}
	return refuseBrake(in, BrakeRuleSnapshotFailed, ReasonSnapshotFailed, http.StatusServiceUnavailable,
		"the pre-state snapshot is "+in.Snapshot.String()+
			"; without it the action has no recorded state to return to, so it does not run (06 §4.4)",
		journalOK, PausedRetryAfterSeconds)
}

// decidePostExecute is row 9: the write landed and the broker can neither confirm it nor undo it.
//
// The AND is the whole row. Verification failing is ordinary and is the recovery ladder's business
// (04 §5.1); rollback failing after a successful verify is not a thing. This fires only where both
// are true, which is the state where the cluster contains a change nobody can describe and nobody
// can reverse -- and the only correct action is to stop the agent from making more of them.
func decidePostExecute(in BrakeInputs, journalOK bool) BrakeDecision {
	if in.Verified.ok() {
		return BrakeDecision{Effect: BrakeAllow, Rule: BrakeRuleNone, JournalReachable: journalOK}
	}
	if in.RolledBack.ok() {
		// Verification failed and the rollback worked. That is rung 3 of the ladder, not the brake.
		return BrakeDecision{Effect: BrakeAllow, Rule: BrakeRuleNone, JournalReachable: journalOK}
	}
	return BrakeDecision{
		Effect:           BrakeHalt,
		Rule:             BrakeRuleUnverifiedUnrolled,
		AutoPause:        true,
		Page:             true,
		JournalReachable: journalOK,
		Detail: "the action executed, verification is " + in.Verified.String() + " and rollback is " +
			in.RolledBack.String() + "; the agent is paused and a human is paged (06 §4.4, 03 §6)",
	}
}

// undoExempt reports whether this envelope is exempt from the agent-level brake.
//
// THE ONE INTERPRETATION IN THIS FILE, and it is recorded in the ledger. 06 §4.4's pause row says
// the broker "refuses new envelopes", full stop, and does not carve out undo the way the freeze row
// does with `allowUndo`. But 09's V-REV-007 -- "undo works with the originating agent paused or
// deleted", BLOCKING-ALWAYS -- requires exactly that carve-out, because 06 §4.4's undo row makes an
// undo "a first-class classified, journaled action", which is to say an envelope through this
// broker.
//
// The resolution is to read pause the way the same section reads freeze: undo is exempt BY ORIGIN,
// not by class. That is invariant-preserving, which is why it is a decision and not a halt
// (PROTOCOL §8.5) -- an undo cannot widen what an agent may newly do. It replays a plan recorded
// before the pause, against a pre-state the journal already holds. The alternative reading makes
// pause a trap: pausing a misbehaving agent would also disable the one control that repairs what it
// did, and an operator would have to un-pause the thing they are afraid of in order to clean up
// after it.
//
// WHAT IT DOES NOT EXEMPT, which matters more than what it does: the journal (row 3), the snapshot
// (row 4), the undo plan (row 5), the roster (row 6), the budget (row 7) and post-execution
// verification (row 9) all apply to an undo exactly as they apply to anything else. An undo is a
// write. The exemption is from the controls that say "this agent should not be acting right now",
// never from the machinery that makes any action recoverable.
func undoExempt(in BrakeInputs) bool { return in.IsUndo() }

// agentPaused reads the brake field, returning the reason alongside it.
func agentPaused(a *agentv1alpha1.Agent) (bool, string) {
	if a == nil {
		return false, ""
	}
	ops := a.Spec.Operations
	if ops == nil || ops.Paused == nil || !*ops.Paused {
		return false, ""
	}
	return true, ops.PauseReason
}

// blockingFreeze returns the first freeze that stops this envelope, or nil.
//
// "First" is by name, sorted, so that a scope covered by three freezes reports the same one on
// every call. A refusal whose cited freeze changes between two identical requests is a refusal an
// operator cannot act on -- they clear the one they were told about and the next attempt names a
// different one.
func blockingFreeze(in BrakeInputs) *agentv1alpha1.FleetFreeze {
	if in.Freezes == nil {
		return nil // row 1 already handled it; reaching here means the view was fresh.
	}
	candidates := make([]*agentv1alpha1.FleetFreeze, 0, len(in.Freezes.Freezes))
	for i := range in.Freezes.Freezes {
		f := &in.Freezes.Freezes[i]
		if f.Expired(metav1.NewTime(in.Now)) {
			continue
		}
		if !f.Covers(in.Scope) {
			continue
		}
		if in.IsUndo() {
			// 06 §4.4: `allowUndo` defaults true and keeps undo and rollback working during a
			// freeze. A freeze that set it false blocks undo too, which is a deliberate and
			// explicit choice by whoever created the freeze.
			if f.UndoAllowed() {
				continue
			}
			candidates = append(candidates, f)
			continue
		}
		if f.Allows(in.Class) {
			continue
		}
		candidates = append(candidates, f)
	}
	if len(candidates) == 0 {
		return nil
	}
	sort.Slice(candidates, func(i, j int) bool { return candidates[i].Name < candidates[j].Name })
	return candidates[0]
}

// rosterUsable is row 6's "missing / empty".
//
// EffectiveMinApprovals is compared against the roster size because a roster of two with
// `minApprovals: 3` can never approve anything, and an action gated against it is parked forever
// while looking, in every status field, like it is waiting for a human who will eventually arrive.
// Admission refuses that roster (P9-T6a), so this is the second line: a roster can lose members
// after admission, and shrinking a roster below its own threshold is exactly how it happens.
func rosterUsable(r *agentv1alpha1.ApprovalRoster) bool {
	if r == nil || len(r.Spec.Approvers) == 0 {
		return false
	}
	return int(r.EffectiveMinApprovals()) <= len(r.Spec.Approvers)
}

func rosterDetail(r *agentv1alpha1.ApprovalRoster) string {
	switch {
	case r == nil:
		return "no approval roster resolves for this agent"
	case len(r.Spec.Approvers) == 0:
		return "the approval roster " + r.Name + " has no approvers"
	default:
		return fmt.Sprintf("the approval roster %s requires %d approvals from %d approvers and can never reach it",
			r.Name, r.EffectiveMinApprovals(), len(r.Spec.Approvers))
	}
}

// budgetRefusal is row 7: the initiative budget and the flap threshold (04 §4.2, 06 §1.1).
//
// Both halves escalate; they are separate rules because the remedies differ -- a spent budget waits
// for the window to roll, a flap means the agent and something else are fighting over the same
// object and a human has to break the tie (04 §4.2).
//
// Undo is not exempt HERE, and that is not a contradiction of 06 §1.1's "undo is never refused for
// budget reasons". The row still runs; the accountant is what knows an undo does not draw down an
// hourly bucket, and it says so by not reporting Exhausted. Flap is a different control (05 §1.5
// lists them separately) and does apply: an undo that is itself part of an oscillation is exactly
// the case a human needs to see.
func budgetRefusal(in BrakeInputs, journalOK bool) (BrakeDecision, bool) {
	if in.Accountant == nil {
		// Nobody is counting. Distinct from a tally of zero, which permits -- see BrakeBudget.
		// Escalating rather than merely refusing, because unlike a spent budget this does not come
		// right when the hour rolls over; something is miswired and only a human fixes that.
		d := refuseBrake(in, BrakeRuleBudgetExhausted, ReasonBudgetExhausted, http.StatusTooManyRequests,
			"the broker cannot count its own initiative spend, so it cannot show this action is within budget (06 §4.4)",
			journalOK, PausedRetryAfterSeconds)
		d.Escalate = true
		return d, true
	}
	b := in.Accountant.Budget(BudgetQuery{
		Agent:   in.Agent,
		Trigger: in.Trigger,
		Class:   in.Class,
		Targets: in.Targets,
		Now:     in.Now,
	})
	if !b.Exhausted && !b.FlapBreached {
		return BrakeDecision{}, false
	}
	rule, reason := BrakeRuleBudgetExhausted, ReasonBudgetExhausted
	if b.FlapBreached {
		rule, reason = BrakeRuleFlapBreached, ReasonFlapDetected
	}
	detail := "the initiative budget or flap threshold stopped this action (04 §4.2)"
	if b.Detail != "" {
		detail = b.Detail
	}
	d := refuseBrake(in, rule, reason, http.StatusTooManyRequests, detail, journalOK, PausedRetryAfterSeconds)
	d.Escalate = true
	return d, true
}

// contestedRefusal is row 8: any target carrying a contested marker refuses the whole envelope.
//
// The whole envelope, not the contested target: an envelope is one action (06 §4.1), its targets
// are applied as a set, and refusing part of it would leave the cluster in a state no undo plan
// describes.
func contestedRefusal(in BrakeInputs, journalOK bool) (BrakeDecision, bool) {
	if in.Contested == nil {
		// The index is the authoritative record. Not having it is not the same as an empty one,
		// and the difference matters most during exactly the kind of incident that produces
		// contested markers.
		return refuseBrake(in, BrakeRuleTargetContested, ReasonTargetContested, http.StatusForbidden,
			"the contested index is unavailable, so the broker cannot show that these targets are uncontested (06 §4.4)",
			journalOK, PausedRetryAfterSeconds), true
	}
	if in.IsUndo() {
		// An undo is the explicit human instruction the row asks for. Refusing it would make a
		// contested target permanently unrecoverable by the one control designed to recover it.
		return BrakeDecision{}, false
	}
	for _, t := range in.Targets {
		if e, ok := in.Contested.Lookup(t); ok {
			return refuseBrake(in, BrakeRuleTargetContested, ReasonTargetContested, http.StatusForbidden,
				fmt.Sprintf("%s is contested by action %s since %s; an approval-roster member must uncontest it before the agent touches it again (06 §4.4)",
					ContestedKey(t), e.ActionID, e.Since.UTC().Format(time.RFC3339)),
				journalOK, PausedRetryAfterSeconds), true
		}
	}
	return BrakeDecision{}, false
}

func freezeUnreadableDetail(in BrakeInputs) string {
	if in.Freezes == nil {
		return "the broker cannot read the FleetFreeze list and is treating the scope as frozen (06 §4.4); undo still runs"
	}
	age := "unknown"
	if !in.Freezes.ObservedAt.IsZero() && !in.Now.IsZero() {
		age = in.Now.Sub(in.Freezes.ObservedAt).Truncate(time.Second).String()
	}
	return "the FleetFreeze cache was last synced " + age + " ago, beyond the " + MaxFreezeStaleness.String() +
		" limit, so the scope is treated as frozen (06 §4.4); undo still runs"
}

func frozenDetail(f *agentv1alpha1.FleetFreeze) string {
	d := "FleetFreeze " + f.Name + " covers this scope"
	if f.Spec.Reason != "" {
		d += ": " + f.Spec.Reason
	}
	if f.Spec.ExpiresAt != nil {
		d += " (expires " + f.Spec.ExpiresAt.UTC().Format(time.RFC3339) + ")"
	} else {
		d += " (no expiry; it is cleared by deleting the object)"
	}
	return d
}

// freezeRetryAfter answers with the freeze's own expiry when it has one.
//
// A caller told to retry in 60 s against a freeze that expires in four hours retries 240 times for
// nothing. A caller told to retry in four hours against a freeze a human deletes in ten minutes
// waits three hours and fifty minutes too long -- so the answer is capped, and the cap is why the
// hint is safe to give at all.
func freezeRetryAfter(f *agentv1alpha1.FleetFreeze, now time.Time) int {
	if f.Spec.ExpiresAt == nil || now.IsZero() {
		return PausedRetryAfterSeconds
	}
	secs := int(f.Spec.ExpiresAt.Time.Sub(now).Round(time.Second).Seconds())
	switch {
	case secs < PausedRetryAfterSeconds:
		return PausedRetryAfterSeconds
	case secs > MaxRetryAfterSeconds:
		return MaxRetryAfterSeconds
	default:
		return secs
	}
}

// refuseBrake builds the refusing decision.
//
// Every brake refusal is journaled and none is a security event. Journaled because 06 §4.1's
// reasoning for refusals applies with more force here -- "an agent tried to write while the fleet
// was frozen" is the single most interesting line in an incident timeline, and it produces no
// mutation and therefore no other record. Not a security event because none of these is an attack:
// an agent submitting work into a freeze is an agent doing its job against a control it cannot see,
// and alarming on it would train operators to ignore the alarm that means something.
func refuseBrake(in BrakeInputs, rule BrakeRule, reason string, status int, detail string, journalOK bool, retryAfter int) BrakeDecision {
	return BrakeDecision{
		Effect:           BrakeRefuse,
		Rule:             rule,
		JournalReachable: journalOK,
		Detail:           detail,
		Refusal: &Refusal{
			Status:            status,
			Reason:            reason,
			Detail:            detail,
			Journal:           true,
			RetryAfterSeconds: retryAfter,
		},
	}
}

// --- the contested index (06 §4.4) --------------------------------------------------------

// ContestedEntry is one contested target.
type ContestedEntry struct {
	// ActionID is the ULID of the ActionRecord whose change was contested. Reported to the caller
	// so a human can read what the agent did before deciding whether it may do it again.
	ActionID string
	// Since is when the marker was set.
	Since time.Time
	// Reason is free text from the human who undid it, when there was one.
	Reason string
}

// ContestedIndex is the authoritative record of contested targets.
//
// AUTHORITATIVE IS THE OPERATIVE WORD, and 06 §4.4 gives the reason in one line: a deleted object
// cannot hold an annotation. The advisory `kube-agents/contested` annotation the broker also stamps
// (P9-T6c) is for humans reading the object; if it were the source of truth, then undoing a
// `create` -- which is to say deleting the object -- would erase the very marker that stops the
// agent creating it again, and the agent's next reconcile would recreate the thing a human just
// removed. That is the loop this index exists to break.
//
// It is in-memory and therefore per-broker and lost on restart. That is a known gap, not a design:
// the durable record is `ActionRecord.status.contested`, and rebuilding this index from the journal
// on startup is P9-T6c's job. Written here so the reader of this file is not left believing an
// in-memory map is the durable answer.
type ContestedIndex struct {
	mu      sync.RWMutex
	entries map[string]ContestedEntry
}

// NewContestedIndex returns an empty index.
//
// A constructor rather than a usable zero value, on purpose: a `var x ContestedIndex` would have a
// nil map, Lookup would answer "not contested" for everything, and it would do so silently and
// forever. `BrakeInputs.Contested` being nil refuses instead, so the two ways of getting an
// unusable index -- forgetting to construct it, and not having one -- both fail closed.
func NewContestedIndex() *ContestedIndex {
	return &ContestedIndex{entries: make(map[string]ContestedEntry)}
}

// ContestedKey is the identity a contested marker is filed under.
//
// GROUP, KIND, NAMESPACE, NAME -- and deliberately NOT version, not UID, not resourceVersion.
//
// Version is excluded because `apps/v1` and `apps/v1beta1` name the same Deployment through two
// windows; keying on version would let an agent launder a contested marker by submitting the next
// envelope through a different API version, which is a one-word change in a client and looks like
// nothing in a diff.
//
// UID is excluded for the case the index exists to serve. A human undoing a `create` deletes the
// object; the agent recreating it produces a NEW UID. Keying on UID would mean the marker never
// matches the thing it is meant to stop, and the failure would be invisible -- the index would
// contain an entry, the lookup would return nothing, and the recreate would sail through.
//
// The cost is that a contested marker survives a delete-and-recreate by a human too, and a human
// who legitimately recreates a contested object has to uncontest it. That is the right trade: an
// unwanted refusal is a human typing one command, and a missed refusal is the agent re-fighting a
// human over production.
func ContestedKey(t agentv1alpha1.TargetRef) string {
	group := t.Group
	if group == "" {
		group = "core"
	}
	return strings.Join([]string{group, t.Kind, t.Namespace, t.Name}, "/")
}

// Mark records a target as contested. Marking an already-contested target keeps the ORIGINAL entry:
// the first contest is the one a human acted on, and overwriting it with a later action id would
// point the operator at the wrong record.
func (x *ContestedIndex) Mark(t agentv1alpha1.TargetRef, actionID string, at time.Time, reason string) {
	if x == nil {
		return
	}
	x.mu.Lock()
	defer x.mu.Unlock()
	if x.entries == nil {
		x.entries = make(map[string]ContestedEntry)
	}
	k := ContestedKey(t)
	if _, exists := x.entries[k]; exists {
		return
	}
	x.entries[k] = ContestedEntry{ActionID: actionID, Since: at, Reason: reason}
}

// Lookup reports whether a target is contested.
//
// A nil receiver answers false, which looks like the wrong default for a fail-closed file. It is
// not: `BrakeInputs.Contested == nil` is caught in contestedRefusal BEFORE any lookup happens, and
// that is where the refusal belongs. Making Lookup itself claim "contested" for a nil index would
// mean the only honest caller -- the one asking a question of a thing that does not exist -- gets
// an answer that reads as a fact about the target.
func (x *ContestedIndex) Lookup(t agentv1alpha1.TargetRef) (ContestedEntry, bool) {
	if x == nil {
		return ContestedEntry{}, false
	}
	x.mu.RLock()
	defer x.mu.RUnlock()
	e, ok := x.entries[ContestedKey(t)]
	return e, ok
}

// Clear removes a marker. 06 §4.4 restricts this to approval-roster members; the authorization is
// the caller's (the ChatOps `uncontest` path and the roster check live above this layer), because
// an index that also decided who may write to it would be two responsibilities in one lock.
func (x *ContestedIndex) Clear(t agentv1alpha1.TargetRef) {
	if x == nil {
		return
	}
	x.mu.Lock()
	defer x.mu.Unlock()
	delete(x.entries, ContestedKey(t))
}

// ClearByAction removes every marker set by one action, which is what `/kage uncontest <action-id>`
// asks for: a human names the action they are releasing, not each object it touched.
func (x *ContestedIndex) ClearByAction(actionID string) int {
	if x == nil || actionID == "" {
		return 0
	}
	x.mu.Lock()
	defer x.mu.Unlock()
	n := 0
	for k, e := range x.entries {
		if e.ActionID == actionID {
			delete(x.entries, k)
			n++
		}
	}
	return n
}

// Len is the number of contested targets, for status reporting and tests.
func (x *ContestedIndex) Len() int {
	if x == nil {
		return 0
	}
	x.mu.RLock()
	defer x.mu.RUnlock()
	return len(x.entries)
}
