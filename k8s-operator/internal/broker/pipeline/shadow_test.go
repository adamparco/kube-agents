package pipeline

// Shadow mode (06 §1.1, 06 §4.1) — V-BRK-025.
//
// `Agent.spec.operations.dryRunOnly` forces `dryRun: true` on every envelope the agent submits. Up
// to this file nothing in the broker read that field: `OperationsSpec.Brake` had been written to
// return it precisely so a caller "cannot consult `paused` and forget `dryRunOnly`, which is how
// shadow mode stops shadowing", and the broker consulted `paused` through a local nil-check and
// forgot `dryRunOnly`. Shadow mode was a documented field, a CRD column, a controller status
// mirror, and nothing else. An operator who set it got an agent that executed.
//
// That is the second instance of the shape [[LSN-007]] names — every unit test passes and the
// feature does nothing in a real install — and the reason it survived this long is that no test
// could fail: the envelope-side dry run works, and every existing test drives it from the envelope.
//
// What these tests pin, and why each one is here rather than implied by the others:
//
//  1. The forcing happens at all, for an envelope that asked to execute.
//  2. It is STRICTER-ONLY over the full 2x2 — the property invariant 4 turns on. "Cannot be
//     cleared" is a claim about a lattice, so it is checked as one and not as an anecdote.
//  3. An unreadable Agent is shadowed. Over-determined in the composed pipeline (the brake refuses
//     an unreadable Agent four steps earlier), so the fail-closed convention is asserted directly
//     on `shadowed` where the assertion can actually fail.
//  4. The forcing does NOT reach classification. This is the one that costs something to get
//     right, and the only one whose failure would be silent: `classify` suppresses the
//     no-undo-plan escalation for a dry run, so forcing the value into step 4 would make a
//     shadowed agent's un-undoable action read `routine` in the journal where the real thing is
//     `gated` — a shadow that under-reports, which is worse than no shadow because it is read as
//     evidence.
//  5. The shadow record cannot become the classifier's evidence of familiarity, which is the
//     cross-package half: V-BRK-024's history source keys off `spec.dryRun`.

import (
	"context"
	"strings"
	"testing"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/undo"
	"k8s.io/utils/ptr"
)

// shadowedAgent is the fixture the whole file turns on: the reference Agent with shadow mode set.
func shadowedAgent() *agentv1alpha1.Agent {
	a := testAgentCR()
	a.Spec.Operations = &agentv1alpha1.OperationsSpec{DryRunOnly: ptr.To(true)}
	return a
}

// withAgent replaces the Agent the brake observes, leaving every other input at its allowing value
// so that a refusal in these tests can only have come from the Agent.
func withAgent(a *agentv1alpha1.Agent) func(*rig) {
	return func(r *rig) { r.brake.view.Agent = a }
}

// TestShadowModeForcesTheDryRunTheEnvelopeDidNotAskFor is the whole feature in one submission.
//
// The envelope says `dryRun: false` and means it. Everything below is what the agent gets instead.
func TestShadowModeForcesTheDryRunTheEnvelopeDidNotAskFor(t *testing.T) {
	env := createEnvelope()
	env.DryRun = false

	r := newRig(t, withAgent(shadowedAgent()))
	tr, res, err := r.submit(env)
	if err != nil {
		t.Fatalf("submit: %v\ntrace: %s", err, tr)
	}

	if r.applier.mutations != 0 {
		t.Errorf("shadow mode issued %d real mutations; the field is the whole feature", r.applier.mutations)
	}
	// Not merely "did not mutate" -- a pipeline that refused at step 5 would also not mutate, and
	// would be a different thing entirely. Shadow mode runs the action all the way down and stops
	// at the write, so the dry-run pass must have happened.
	if r.applier.dryRuns == 0 {
		t.Error("shadow mode reached the applier zero times; it is supposed to check the action, not skip it")
	}
	if res.Phase != string(agentv1alpha1.PhaseDryRun) {
		t.Errorf("phase = %q, want %q", res.Phase, agentv1alpha1.PhaseDryRun)
	}
	if got := tr.Reached(); got != broker.LastStep {
		t.Errorf("reached %s, want %s -- a shadow run is a complete run\ntrace: %s", got, broker.LastStep, tr)
	}
	if tr.Ran(broker.StepVerify) {
		t.Errorf("step 10 ran; there is no outcome to verify\ntrace: %s", tr)
	}

	// The caller is TOLD. An agent that asked to execute, was silently shadowed, and read back a
	// bare success would make its next decision believing the cluster had changed.
	if !strings.Contains(res.Message, "nothing was mutated") {
		t.Errorf("the response message is %q and does not say nothing happened", res.Message)
	}

	if len(r.records.stored) != 1 {
		t.Fatalf("stored %d records, want 1", len(r.records.stored))
	}
	if !r.records.stored[0].Spec.DryRun {
		t.Error("the journaled record says spec.dryRun: false; the journal is now lying about what ran")
	}

	// THE FORCING IS DERIVED, NOT A MUTATION OF THE CALLER'S ENVELOPE, and that is load-bearing two
	// packages away. `ComputeIdempotencyKey` hashes `{agentIdentity, dryRun, operations}` over the
	// envelope, and the server recomputes it and refuses a mismatch (06 §4.1) BEFORE the pipeline is
	// called. A pipeline that flipped `env.DryRun` in place would be fine on its own and would turn
	// every shadowed submission into a `400 idempotency-key-mismatch` the moment the two ran
	// together -- shadow mode refusing everything rather than shadowing anything, which reads to an
	// operator as a broken broker rather than as a working feature.
	if env.DryRun {
		t.Error("the pipeline wrote the forced value back onto the envelope; the caller's idempotency key no longer describes the envelope the broker holds")
	}
}

// TestNothingComposesBackToExecuting is the stricter-only property as a lattice.
//
// Two inputs, four combinations, exactly one of which executes. `dryRunOnly` is the half an agent
// cannot reach -- `Agent` is a control-plane object no agent identity may write -- so the row that
// matters is {envelope false, agent true}: the agent asking for a real write and not getting one.
func TestNothingComposesBackToExecuting(t *testing.T) {
	for _, tc := range []struct {
		name        string
		envDryRun   bool
		shadow      bool
		wantExecute bool
	}{
		{"neither", false, false, true},
		{"the caller asked for a dry run", true, false, false},
		{"shadow mode, over an envelope that asked to execute", false, true, false},
		{"both", true, true, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := testAgentCR()
			if tc.shadow {
				agent = shadowedAgent()
			}
			env := createEnvelope()
			env.DryRun = tc.envDryRun

			r := newRig(t, withAgent(agent))
			tr, res, err := r.submit(env)
			if err != nil {
				t.Fatalf("submit: %v\ntrace: %s", err, tr)
			}

			executed := r.applier.mutations > 0
			if executed != tc.wantExecute {
				t.Fatalf("executed = %v (%d mutations), want %v\ntrace: %s",
					executed, r.applier.mutations, tc.wantExecute, tr)
			}
			// The phase and the mutation count have to agree. A record that says `Verified` while
			// nothing was written, or `DryRun` while something was, is the failure mode that makes
			// a journal unusable as evidence.
			wantPhase := string(agentv1alpha1.PhaseDryRun)
			if tc.wantExecute {
				wantPhase = string(agentv1alpha1.PhaseVerified)
			}
			if res.Phase != wantPhase {
				t.Errorf("phase = %q, want %q", res.Phase, wantPhase)
			}
			if r.records.stored[0].Spec.DryRun == tc.wantExecute {
				t.Errorf("spec.dryRun = %v with wantExecute = %v; the record disagrees with the cluster",
					r.records.stored[0].Spec.DryRun, tc.wantExecute)
			}
		})
	}
}

// TestAnUnobservableAgentIsShadowed asserts the fail-closed convention where it can fail.
//
// The composed claim -- a nil Agent never mutates -- is true today for a reason that has nothing to
// do with this code: the brake's row 2 refuses an unreadable Agent at step 5. So a pipeline-level
// test of it passes with `shadowed` deleted, and would be a vacuous check. The direct assertion is
// the honest one; the composed assertion is kept below it, labelled as over-determined, because the
// composition is what an operator actually depends on.
func TestAnUnobservableAgentIsShadowed(t *testing.T) {
	if !shadowed(BrakeView{Agent: nil}) {
		t.Error("a broker that cannot read its own Agent decided it was not in shadow mode; nil means unreadable, and unreadable is not permission")
	}
	if !shadowed(BrakeView{Agent: shadowedAgent()}) {
		t.Error("dryRunOnly: true did not read as shadowed")
	}
	if shadowed(BrakeView{Agent: testAgentCR()}) {
		t.Error("an agent with no operations block read as shadowed; that would stop every install from ever executing")
	}
	// A present-but-false value is not the same input as an absent one, and both must mean
	// not-shadowed -- otherwise `dryRunOnly: false` written explicitly turns shadow mode ON.
	explicit := testAgentCR()
	explicit.Spec.Operations = &agentv1alpha1.OperationsSpec{DryRunOnly: ptr.To(false)}
	if shadowed(BrakeView{Agent: explicit}) {
		t.Error("dryRunOnly: false read as shadowed")
	}

	// Over-determined: the brake refuses this at step 5, before the forcing could matter, so the
	// submission comes back as a refusal rather than a dry run. Asserted anyway, because "the brake
	// happens to be in front of it" is not a property of shadow mode.
	r := newRig(t, withAgent(nil))
	tr, _, err := r.submit(createEnvelope())
	if err == nil {
		t.Fatalf("a broker that cannot read its own Agent accepted an envelope\ntrace: %s", tr)
	}
	if r.applier.mutations != 0 {
		t.Errorf("a broker that could not read its Agent mutated %d objects", r.applier.mutations)
	}
}

// TestTheShadowClassifiesAsTheRealThingWould pins the one place the forced value must NOT reach.
//
// Stated as an equality against the real run rather than against a hardcoded class, because the
// property is not "shadow mode says gated" -- it is "shadow mode says whatever the unshadowed agent
// would have said". A literal expectation would still pass if both sides drifted together, which is
// precisely the drift that matters.
//
// The lever is step 6 of `classify`: an envelope with no undo plan is raised to `gated`, and that
// raise is suppressed for a dry run. So feeding the forced value into step 4 makes row B agree with
// row C instead of row A -- a shadow record reading `routine` for an action that really gates.
//
// Row C is the non-vacuity control. If step 6's suppression is ever removed, all three rows agree,
// the equality in row B becomes free, and C fails to say so.
//
// NOTE WHAT THE OBSERVABLE IS, because the obvious one does not work. All three rows come back
// `class: gated`: the brake's row 5 refuses an unusable undo plan independently of the classifier,
// so it re-raises row C after step 6 let it through. The classifier's own answer survives in
// `classification.reasons` -- rows A and B cite `no-undo-plan`, row C cites `default-routine` -- and
// the reason list is exactly what an operator mines a shadow journal for. So the assertion is over
// the reasons, and the class is deliberately not asserted, because on this envelope it is
// over-determined and would pass either way.
func TestTheShadowClassifiesAsTheRealThingWould(t *testing.T) {
	// A refusal is a plan whose strategy is `none`, never a nil Plan (undo.Result). A nil one is a
	// step-6 fault, which is a different test in the fault table.
	noPlan := func(r *rig) {
		r.planner = PlannerFunc(func(context.Context, undo.Request, undo.ReferenceIndex, undo.DryRunner) (*undo.Result, error) {
			return &undo.Result{
				Plan:     &agentv1alpha1.UndoPlan{Strategy: agentv1alpha1.UndoNone},
				Refusals: []string{"nothing about this action can be inverted"},
			}, nil
		})
	}

	// reasonsOf runs one submission and returns the journaled classification reasons as a
	// comparable string, so the three rows can be compared as values rather than eyeballed.
	reasonsOf := func(t *testing.T, shadow, envDryRun bool) string {
		t.Helper()
		agent := testAgentCR()
		if shadow {
			agent = shadowedAgent()
		}
		env := createEnvelope()
		env.DryRun = envDryRun

		r := newRig(t, withAgent(agent), noPlan)
		tr, _, err := r.submit(env)
		if err != nil {
			t.Fatalf("submit: %v\ntrace: %s", err, tr)
		}
		if len(r.records.stored) != 1 {
			t.Fatalf("stored %d records, want 1\ntrace: %s", len(r.records.stored), tr)
		}
		rules := make([]string, 0, 4)
		for _, reason := range r.records.stored[0].Spec.Classification.Reasons {
			rules = append(rules, reason.Rule)
		}
		return strings.Join(rules, ",")
	}

	real := reasonsOf(t, false, false)     // A: the agent as it would run unshadowed
	shadow := reasonsOf(t, true, false)    // B: the same envelope, shadowed
	callerDry := reasonsOf(t, false, true) // C: the caller asking for a dry run itself

	if shadow != real {
		t.Errorf("the shadow journaled [%s] where the real action journals [%s] -- an operator reading this journal to decide whether to grant the agent authority is reading the wrong thing",
			shadow, real)
	}
	if callerDry == real {
		t.Fatalf("rows A and C both journal [%s], so the equality above is free and this test proves nothing; step 6's dry-run suppression must have changed", real)
	}
}

// TestAShadowRecordTeachesTheClassifierNothing is the cross-package tie to V-BRK-024.
//
// `history.Source` builds familiarity from records that are `dryRun: false` AND
// `status.phase: Verified`. A shadow record fails both, and it has to fail them for the same reason
// the feature exists: an action an agent never performed is not experience. If the record were
// written from the envelope's `dryRun` instead of the effective one, a whole shadow-mode soak would
// quietly teach the classifier that every action it never took was routine, and the `novel-action`
// escalation would be gone by the time the agent was let out of shadow mode.
func TestAShadowRecordTeachesTheClassifierNothing(t *testing.T) {
	env := createEnvelope()
	env.DryRun = false

	r := newRig(t, withAgent(shadowedAgent()))
	tr, _, err := r.submit(env)
	if err != nil {
		t.Fatalf("submit: %v\ntrace: %s", err, tr)
	}
	if !r.records.stored[0].Spec.DryRun {
		t.Error("spec.dryRun is false on a shadow record; history.derive would count it as experience")
	}
	// The terminal phase off the SetPhase log, not off `stored`: fakeRecords deep-copies at Create,
	// so the stored object is frozen at `Executing` and reading it here would assert on the wrong
	// moment. Both conditions have to hold -- history.derive requires `dryRun: false` AND
	// `Verified`, so either one alone leaves the other free to regress unnoticed.
	phases := r.records.phases
	if len(phases) == 0 || phases[len(phases)-1] != agentv1alpha1.PhaseDryRun {
		t.Errorf("terminal phase = %v, want %q", phases, agentv1alpha1.PhaseDryRun)
	}
}
