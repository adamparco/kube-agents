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

// Package pipeline assembles steps 3 through 11 of the 03 §4.1 broker pipeline.
//
// Everything it calls was built and unit-tested by earlier tasks in phase 9 -- the classifier, the
// brake, the undo planner, the snapshotter, the executor, the verification ladder. Every one of
// them was reachable only from its own tests. THAT is what this package is for: `broker.Pipeline`
// had exactly one implementation, `UnavailablePipeline`, which 503s, so a fully validated envelope
// arriving at a real broker got "the pipeline is not built into this binary" and none of the
// machinery below ran in production or in any test that crossed a package boundary (LSN-007).
//
// The assembly is deliberately thin. It owns no policy: every decision here is delegated to the
// package that owns it, and what this file contributes is the ORDER, the conversions between one
// package's output type and the next one's input type, and the record that says which steps ran.
// A rule reimplemented here would be a second opinion about a question already answered, and the
// second opinion is the one that drifts.
package pipeline

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/classify"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/execute"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/undo"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/verify"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/scope"
)

// MaxIntentLen bounds what is copied out of the envelope into the record's intent field. Same
// bound the rejection journal uses, and for the same reason: the intent is model-authored prose.
const MaxIntentLen = 512

// The one envelope string value this package has to recognize by name. It is validated by the
// envelope's own closed enum, so this is a read of a shared vocabulary rather than a second one.
const mediaJSONPatch = "application/json-patch+json"

// RecordStore is the ActionRecord persistence seam. `*journal.Store` satisfies it.
type RecordStore interface {
	Create(ctx context.Context, ar *agentv1alpha1.ActionRecord) error
	Get(ctx context.Context, namespace, actionID string) (*agentv1alpha1.ActionRecord, error)
	SetPhase(ctx context.Context, ar *agentv1alpha1.ActionRecord, phase agentv1alpha1.ActionPhase, message string) error
}

// BrakeView is everything 06 §4.4 needs ABOUT THE AGENT, gathered in one read so the brake stays a
// pure function.
//
// About the agent, not about the action: what an observation taken before classification cannot
// answer does not belong here. Row 7's budget and row 8's contested lookup both depend on the
// envelope's class and targets, so both are `Config` dependencies queried at decision time instead
// (see `broker.Accountant`, `broker.ContestedIndex`). Budget used to live in this struct and the
// mismatch showed: the accountant could only be handed an agent, so it could not answer the
// question 04 §4.2 actually asks.
//
// The nil-means-unreadable convention is `broker.BrakeInputs`', not this package's invention, and
// it is why Observe returns no error: a source that returned `(view, err)` would invite a caller to
// treat the error as fatal and skip the brake entirely, which is the one outcome 06 §4.4 exists to
// prevent. An observer that could not read says so IN the view, and the brake refuses.
type BrakeView struct {
	Agent   *agentv1alpha1.Agent
	Freezes *broker.FreezeView
	Roster  *agentv1alpha1.ApprovalRoster
	Journal broker.BrakeSignal
}

// BrakeSource gathers the brake's inputs.
type BrakeSource interface {
	Observe(ctx context.Context) BrakeView
}

// policyRetryAfterSeconds is what a caller is told to wait after a policy-unavailable refusal. Long
// enough that a broker whose API server is down does not get hammered by every agent it serves,
// short enough that a single dropped List does not stall an incident response for a minute.
const policyRetryAfterSeconds = 15

// ClassifierSource supplies the classifier for one submission.
//
// A seam rather than a `*classify.Classifier` because a ChangePolicy an operator applies has to
// take effect on the next action, not on the next broker restart. A fixed classifier built at
// startup would make `kubectl apply -f tighten.yaml` a no-op until someone noticed -- and the
// noticing would happen after the action the policy was written to stop.
//
// Current returns an error rather than a nil classifier when the policy set is unknown, and step 4
// turns that error into a refusal. That is the fail-closed direction, and it is forced: the
// classifier maxes over its sources (06 §4.2 step 3), so a policy set that is missing entries can
// only ever under-classify. "Classify against what we have" and "classify against nothing" are the
// same bug at different sizes.
type ClassifierSource interface {
	Current() (*classify.Classifier, error)
}

// StaticClassifier adapts a fixed classifier to ClassifierSource, for tests and for any caller
// whose policy set genuinely cannot change -- the code floor alone, for instance.
type StaticClassifier struct{ C *classify.Classifier }

// Current returns the fixed classifier.
func (s StaticClassifier) Current() (*classify.Classifier, error) {
	if s.C == nil {
		return nil, errors.New("pipeline: StaticClassifier holds no classifier")
	}
	return s.C, nil
}

// Planner produces the undo plan for an envelope's operations. undo.Generate is the implementation;
// the interface exists so that the failure mode is injectable -- see Config.Planner.
type Planner interface {
	Generate(ctx context.Context, req undo.Request, idx undo.ReferenceIndex) (*undo.Result, error)
}

// PlannerFunc adapts a function to Planner. undo.Generate satisfies it directly.
type PlannerFunc func(ctx context.Context, req undo.Request, idx undo.ReferenceIndex) (*undo.Result, error)

// Generate calls f.
func (f PlannerFunc) Generate(ctx context.Context, req undo.Request, idx undo.ReferenceIndex) (*undo.Result, error) {
	return f(ctx, req, idx)
}

// ApprovalNotifier tells the humans on the roster that a gated action is waiting (04 §3).
//
// A failure here is NOT a reason to execute. It is a reason to leave the action parked and say so,
// which is what step 7 does: a broker that could not reach anyone and proceeded anyway has
// converted "waiting for a human" into "nobody was watching".
type ApprovalNotifier interface {
	Notify(ctx context.Context, ar *agentv1alpha1.ActionRecord, roster *agentv1alpha1.ApprovalRoster) error
}

// Config assembles a Pipeline. Every field is required except BodyStore and Approvals.
type Config struct {
	// AgentName, Namespace and ActorServiceAccount identify the agent this broker serves. They come
	// from the broker's own deployment, never from an envelope.
	AgentName           string
	Namespace           string
	ActorServiceAccount string

	Classifier ClassifierSource
	Live       classify.LiveState
	Refs       undo.ReferenceIndex
	// Planner generates the undo plan. Defaults to undo.Generate; it is a seam for the same reason
	// every other dependency here is one. Whether a plan could be produced is a gating input
	// (06 §4.2 rule 6, 06 §4.4 row 5), and a pipeline whose planner cannot be made to fail is a
	// pipeline whose behaviour on an unplannable action is untested -- which is precisely the
	// behaviour that decides between "gated" and "executed with no way back".
	Planner   Planner
	Reader    execute.Reader
	BodyStore execute.BodyStore
	Executor  *execute.Executor
	Verifier  *verify.Driver
	Records   RecordStore
	Brake     BrakeSource
	// Accountant and Contested are the two brake inputs that are queried rather than observed,
	// because both need the classified envelope. See BrakeView for why they are not in it.
	Accountant broker.Accountant
	Contested  *broker.ContestedIndex
	Approvals  ApprovalNotifier

	// Now is injectable so a test can pin the retention clocks and the plan's GeneratedAt.
	Now func() time.Time
}

// Pipeline is steps 3-11, in order.
type Pipeline struct {
	cfg Config
}

// New validates the wiring and returns the pipeline.
//
// Every check here is a security property in the same sense `broker.NewServer`'s are: a pipeline
// missing its classifier would execute everything as routine, and one missing its record store
// would execute without a journal. Neither is a nil-pointer panic worth discovering in production,
// so both are startup errors.
func New(cfg Config) (*Pipeline, error) {
	switch {
	case cfg.AgentName == "":
		return nil, errors.New("pipeline: an agent name is required; the record must name the agent it is about")
	case cfg.Namespace == "":
		return nil, errors.New("pipeline: a namespace is required; it is where ActionRecords are written")
	case cfg.Classifier == nil:
		return nil, errors.New("pipeline: a ClassifierSource is required; without one every action would execute unclassified")
	case cfg.Live == nil:
		return nil, errors.New("pipeline: a LiveState is required; classify.Resolve on a nil LiveState yields ops with no live data, which LOOSENS every live-dependent rule (see classify.Resolve)")
	case cfg.Refs == nil:
		return nil, errors.New("pipeline: a ReferenceIndex is required; the undo planner must be able to tell 'nothing points at it' from 'I could not look'")
	case cfg.Reader == nil:
		return nil, errors.New("pipeline: a Reader is required; there is no pre-state without one and therefore no undo plan")
	case cfg.Executor == nil:
		return nil, errors.New("pipeline: an Executor is required")
	case cfg.Verifier == nil:
		return nil, errors.New("pipeline: a verify.Driver is required; an unverified write is 04 §5 rung 0 skipped")
	case cfg.Records == nil:
		return nil, errors.New("pipeline: a RecordStore is required; nothing executes unjournaled (03 §4.1 step 11)")
	case cfg.Brake == nil:
		return nil, errors.New("pipeline: a BrakeSource is required; a broker that cannot see the brake must not be able to skip it")
	case cfg.Accountant == nil:
		return nil, errors.New("pipeline: an Accountant is required; broker.BrakeInputs treats nil as nobody-counting, which refuses every action -- 04 §4.2 budgets are not optional and a broker must not be constructible without row 7")
	case cfg.Contested == nil:
		return nil, errors.New("pipeline: a ContestedIndex is required; broker.BrakeInputs treats nil as unavailable, which refuses every action")
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	if cfg.Planner == nil {
		cfg.Planner = PlannerFunc(undo.Generate)
	}
	return &Pipeline{cfg: cfg}, nil
}

// state is one submission's working set, threaded through the steps.
//
// A struct rather than a closure over local variables so that a step which forgot to set something
// leaves a zero value a later step can notice, instead of capturing a stale variable from the
// previous iteration of a loop that does not exist yet.
type state struct {
	id  *broker.Identity
	env *broker.Envelope
	tr  *broker.StepTrace

	actionID string
	at       time.Time
	caller   classify.Caller

	targets  []agentv1alpha1.TargetRef
	resolved []classify.ResolvedOp
	snaps    []execute.Snapshot

	class     *classify.Classification
	plan      *undo.Result
	brakeView BrakeView

	// mayExecute is the effective execution decision, and its POLARITY IS THE POINT.
	//
	// It is stored as permission-to-execute rather than as a dry-run flag so that the zero value --
	// what this struct holds before anything computed the answer, and what a future step added
	// above the computation would see -- is `false`, which `dryRun` reads back as "this is a dry
	// run". A field spelled `dryRun bool` has the opposite zero value: forgetting to set it means
	// executing for real, which is the one direction 06 §1.1 cannot tolerate. Nothing outside
	// Submit writes it, and `dryRun()` is the only reader.
	mayExecute bool

	record   *agentv1alpha1.ActionRecord
	exec     *execute.Result
	execFail *verify.Failure
	verify   verify.Result
}

// dryRun is the EFFECTIVE dry-run decision: the caller asked for one, or the agent is in shadow
// mode, or nobody has decided yet.
//
// Every step from 9 onwards asks this and never `s.env.DryRun`, because the envelope field is only
// half the answer. 06 §4.1's field table says `dryRun` is "forced `true` when
// `spec.operations.dryRunOnly` is set", and an agent cannot clear `dryRunOnly` -- `Agent` is a
// control-plane object no agent identity may write -- so the composition is one-way: a caller's dry
// run OR shadow mode is a dry run, and nothing anywhere composes back to executing.
//
// STEP 4 IS THE DELIBERATE EXCEPTION and still reads `s.env.DryRun`. See stepClassify.
func (s *state) dryRun() bool { return !s.mayExecute }

// shadowed reports whether `spec.operations.dryRunOnly` forces this submission to a dry run.
//
// UNREADABLE MEANS SHADOWED. A nil Agent is `BrakeView`'s "could not read" convention, and reading a
// blind broker as "not in shadow mode" is the fail-open direction 06 §4.4 exists to refuse. In the
// composed pipeline the brake's row 2 refuses an unreadable Agent at step 5, four steps before
// anything executes, so this arm is unreachable *there* -- but "unreachable given the brake" and
// "cannot execute unobserved" are different claims, and only the second one survives someone
// reordering the steps.
//
// `OperationsSpec.Brake` does the reading rather than a local nil-check on `DryRunOnly`, because
// that helper exists for exactly this call site: its doc says it keeps the three brake fields
// together so "a caller cannot consult `paused` and forget `dryRunOnly`, which is how shadow mode
// stops shadowing". Until this function, nothing in the broker called it, and shadow mode had
// indeed stopped shadowing.
func shadowed(v BrakeView) bool {
	if v.Agent == nil {
		return true
	}
	_, dryRunOnly, _ := v.Agent.Spec.Operations.Brake()
	return dryRunOnly
}

// Submit runs steps 3 through 11. Steps 1 and 2 are already on the trace when it is called.
//
// The trace is the caller's, not this function's: the handler records authentication and validation
// on it before the pipeline exists, so a fault at step 1 produces a one-entry trace and a fault at
// step 9 produces a nine-entry one, from the same object. That is the whole shape V-BRK-014 checks,
// and it only works because the trace spans the handler/pipeline boundary rather than starting here.
func (p *Pipeline) Submit(
	ctx context.Context,
	id *broker.Identity,
	env *broker.Envelope,
	tr *broker.StepTrace,
) (*broker.Result, error) {
	if tr == nil {
		return nil, fmt.Errorf("pipeline: no step trace; the pipeline order is only a property if it is recorded")
	}
	if id == nil || env == nil {
		return nil, fmt.Errorf("pipeline: submit called with no identity or no envelope, which means steps 1-2 did not run")
	}

	s := &state{id: id, env: env, tr: tr, at: p.cfg.Now().UTC()}

	actionID, err := journal.NewULID(s.at)
	if err != nil {
		return nil, fmt.Errorf("pipeline: minting an action id: %w", err)
	}
	s.actionID = actionID

	// SHADOW MODE, resolved once, here, before any step can read it (06 §1.1, 06 §4.1).
	//
	// Before the loop rather than inside a step, because it is a property of the submission and not
	// of a stage: there is exactly one write, it happens on the only path that reaches any read, and
	// a step inserted anywhere in the list below therefore cannot find it uncomputed. Putting it in
	// a step would mean the steps before that one see the zero value, and the zero value is a
	// decision too.
	//
	// Its own `Observe` rather than a share of step 5's, for a reason that is easy to invert: this
	// read is EARLIER, so a `dryRunOnly` cleared between here and step 5 leaves the submission in
	// shadow mode, and one set between here and step 5 is caught by step 5 refusing anyway. Both
	// races resolve toward not executing. Sharing step 5's observation would put the decision after
	// classification and reverse the first of those.
	s.mayExecute = !env.DryRun && !shadowed(p.cfg.Brake.Observe(ctx))

	// Steps 3-6 and 8-11 all abort the whole submission on error, and every one of them returns
	// through the trace, so a single error check per step is the entire control flow.
	for _, step := range []func(context.Context, *state) (*broker.Result, error){
		p.stepResolve,
		p.stepClassify,
		p.stepBrake,
		p.stepUndoPlan,
		p.stepGate,
		p.stepSnapshot,
		p.stepExecute,
		p.stepVerify,
		p.stepJournal,
	} {
		res, err := step(ctx, s)
		if err != nil {
			return nil, err
		}
		if res != nil {
			// A terminal, non-error outcome: parked for approval, or a completed dry run.
			return res, nil
		}
	}

	// The phase reported to the caller is the SAME phase step 11 wrote, derived by the same
	// function. Reading s.verify.Phase directly here would be a second derivation, and it disagrees
	// with the first on exactly the case where the verifier never ran: a dry run journals DryRun and
	// would have answered the caller with an empty phase.
	phase, _ := terminal(s)
	return &broker.Result{
		ActionID:  s.actionID,
		Namespace: p.cfg.Namespace,
		Decision:  "accepted",
		Phase:     string(phase),
		Message:   trace(s, "the action executed and was verified"),
		Status:    http.StatusAccepted,
	}, nil
}

// step wraps one trace step so every step body has the same shape: return a detail string and an
// error, and let the trace decide what to record and whether the pipeline may continue.
func (p *Pipeline) step(tr *broker.StepTrace, id broker.Step, fn func() (string, error)) error {
	return tr.Run(id, fn)
}

// --- step 3: resolve scope -------------------------------------------------------------------

// stepResolve performs EVERY live read the rest of the pipeline depends on.
//
// Two reads, not one, and they are different questions. classify.Resolve reads the labels, the
// namespace labels, the digest set and the blast-radius denominator -- the inputs to a RULE.
// execute.CaptureAll reads the whole object -- the input to a PLAN and, later, to a diff. Doing
// both here rather than at their point of use is what makes steps 4 and 6 pure functions, which is
// what makes the 09 §7.1 classification corpus and the §7.3 undo corpus hermetic.
//
// It is also why "resolve scope" is step 3 and not a formality. 03 §4.1 says one out-of-scope
// target rejects the whole envelope; the containment verdict itself is rendered at step 4, because
// 06 §4.2 makes scope the classifier's own evaluation-order step 1 and re-deciding it here would be
// a second implementation of `scope.Contains`. What step 3 owns is the resolution the verdict needs.
func (p *Pipeline) stepResolve(ctx context.Context, s *state) (*broker.Result, error) {
	tr := s.tr
	return nil, p.step(tr, broker.StepResolveScope, func() (string, error) {
		s.caller = classify.Caller{
			Name:  p.cfg.AgentName,
			Tier:  string(s.id.Tier),
			Scope: p.callerScope(ctx),
		}
		if !s.caller.Scope.IsWellFormed() {
			return "", &broker.Refusal{
				Status:        http.StatusServiceUnavailable,
				Reason:        "scope-unresolvable",
				Detail:        "the broker cannot read its own agent's scope; an empty level above a non-empty one acts as a wildcard, so nothing is classified against it",
				Journal:       true,
				SecurityEvent: true,
			}
		}

		raws, targets, err := rawOps(s.env)
		if err != nil {
			return "", err
		}
		s.targets = targets

		resolved, err := classify.Resolve(ctx, p.cfg.Live, s.caller, raws)
		if err != nil {
			return "", fmt.Errorf("step 3: resolving %d operations against live state: %w", len(raws), err)
		}
		s.resolved = resolved

		snaps, err := execute.CaptureAll(ctx, p.cfg.Reader, s.actionID, targets, metav1.NewTime(s.at), p.cfg.BodyStore)
		if err != nil {
			// V-BRK-018: all snapshots or none. CaptureAll already refuses to return a partial set;
			// what this branch adds is that a failure here stops the pipeline at step 3, so no
			// classification, no plan and no execution ever sees a half-read cluster.
			return "", fmt.Errorf("step 3: capturing pre-state for %d targets: %w", len(targets), err)
		}
		s.snaps = snaps

		return fmt.Sprintf("%d operations, %d pre-states", len(resolved), len(snaps)), nil
	})
}

// callerScope derives the caller's authority ceiling from the Agent CR this broker serves.
//
// From the CR, never from the envelope: 03 §4.1 step 1 says the broker derives (tier, scope) from
// the authenticated identity. The authenticator has already established that the caller IS this
// broker's agent, so the CR is the authenticated answer to "what is its scope".
func (p *Pipeline) callerScope(ctx context.Context) scope.Scope {
	return scope.Of(p.cfg.Brake.Observe(ctx).Agent)
}

// --- step 4: classify ------------------------------------------------------------------------

func (p *Pipeline) stepClassify(ctx context.Context, s *state) (*broker.Result, error) {
	tr := s.tr
	return nil, p.step(tr, broker.StepClassify, func() (string, error) {
		// The undo plan is generated here, before Classify, because 06 §4.2's evaluation order
		// makes "no valid undo plan ⇒ raise to at least gated" the classifier's OWN step 6 and
		// `classify.Input.UndoPlanPresent` is the field that carries it. 03 §4.1 numbers plan
		// generation as its step 6, one after the brake; those two orderings cannot both be
		// literal, because the brake's row 5 also reads the plan (broker.decideGate treats an
		// unobserved plan as a missing one and raises to gated), so a pipeline that generated the
		// plan after the brake would gate every action ever submitted.
		//
		// Resolved the way undo.BindCreatedUID resolves the same shape of tension in §4.3.1: by
		// splitting the two things "generate" was doing. Deciding whether the envelope is
		// invertible is a PURE function of the pre-state captured at step 3, and it belongs to
		// whichever step first needs the answer -- which is this one. Step 6 keeps the part of 03
		// §4.1's step 6 that is genuinely a step: attaching the plan to the record, and re-checking
		// that the class never fell.
		plan, err := p.cfg.Planner.Generate(ctx, undo.Request{
			Operations:  undoOps(s.env, s.snaps),
			GeneratedAt: metav1.NewTime(s.at),
		}, p.cfg.Refs)
		if err != nil {
			return "", fmt.Errorf("step 4: generating the undo plan: %w", err)
		}
		s.plan = plan

		in := &classify.Input{
			Caller:     s.caller,
			Operations: s.resolved,
			// THE ENVELOPE'S VALUE, DELIBERATELY -- not `s.dryRun()`, and this is the one place in
			// the pipeline where that is correct.
			//
			// `classify` reads DryRun in exactly one rule: step 6's "no undo plan raises to at
			// least gated" is suppressed for a dry run. Feeding shadow mode's forced `true` in here
			// would therefore make a shadowed agent classify an un-undoable action as `routine`
			// where the same envelope from an unshadowed agent classifies `gated`.
			//
			// That is backwards twice over. It is the permissive direction, which invariant 4
			// forbids on its own. And it defeats the point: shadow mode exists so an operator can
			// see "what an agent WOULD do before granting it the authority to do it"
			// (`OperationsSpec.DryRunOnly`), so the classification in a shadow record has to be the
			// classification the real action would get. A shadow that under-reports the class is
			// worse than no shadow, because it is read as evidence.
			//
			// So the forcing is scoped to execution, and classification stays a function of the
			// request. The envelope field keeps its 06 §4.1 meaning here and only here.
			DryRun:          s.env.DryRun,
			RequireApproval: s.env.RequireApproval,
			MaxObjects:      s.env.EffectiveMaxObjects(),
			UndoPlanPresent: plan.Undoable(),
		}
		// Resolved per submission, not held from startup. A ChangePolicy applied a second ago is in
		// force for this action; see ClassifierSource.
		//
		// An unresolvable policy set is a refusal and not a fallback to the code floor. The floor
		// alone is a WEAKER rule table than the floor plus policies, always -- the classifier maxes
		// over sources -- so "classify against the floor while the policy set is unknown" is a
		// silent downgrade of every policy in the cluster, arriving at exactly the moment the
		// cluster is unhealthy. Journaled and flagged as a security event because a broker that
		// cannot see its policies is a control-plane condition an operator has to be told about,
		// not a transient the caller absorbs.
		classifier, err := p.cfg.Classifier.Current()
		if err != nil {
			return "", &broker.Refusal{
				Status:            http.StatusServiceUnavailable,
				Reason:            broker.ReasonPolicyUnavailable,
				Detail:            err.Error(),
				Journal:           true,
				SecurityEvent:     true,
				RetryAfterSeconds: policyRetryAfterSeconds,
			}
		}

		cls, err := classifier.Classify(in)
		if err != nil {
			return "", fmt.Errorf("step 4: classifying: %w", err)
		}
		s.class = cls

		if cls.Abort != nil {
			return "", &broker.Refusal{
				Status:        http.StatusForbidden,
				Reason:        "blast-radius-cap-exceeded",
				Detail:        cls.Abort.Error(),
				Journal:       true,
				SecurityEvent: true,
			}
		}
		if cls.Class == classify.ClassForbidden {
			return "", &broker.Refusal{
				Status:        http.StatusForbidden,
				Reason:        "forbidden",
				Detail:        reasonsDetail(cls),
				Journal:       true,
				SecurityEvent: true,
			}
		}
		return fmt.Sprintf("%s (%s)", cls.Class, strings.Join(cls.PolicySources, "+")), nil
	})
}

// --- step 5: brake ---------------------------------------------------------------------------

func (p *Pipeline) stepBrake(ctx context.Context, s *state) (*broker.Result, error) {
	tr := s.tr
	var effect broker.BrakeEffect
	err := p.step(tr, broker.StepBrake, func() (string, error) {
		s.brakeView = p.cfg.Brake.Observe(ctx)
		d := broker.Decide(broker.BrakeInputs{
			Stage:      broker.StageGate,
			Now:        s.at,
			Agent:      s.brakeView.Agent,
			Scope:      scopeSpecOf(s.brakeView.Agent),
			Freezes:    s.brakeView.Freezes,
			Journal:    s.brakeView.Journal,
			UndoPlan:   signal(s.plan.Undoable()),
			Roster:     s.brakeView.Roster,
			Accountant: p.cfg.Accountant,
			Contested:  p.cfg.Contested,
			Class:      riskClass(s.class.Class),
			Targets:    s.targets,
			Trigger:    agentv1alpha1.ActionTriggerSource(s.env.Trigger.Source),
		})
		effect = d.Effect
		switch d.Effect {
		case broker.BrakeAllow:
			return "allow", nil
		case broker.BrakeRefuse:
			return string(d.Rule), d.Refusal
		case broker.BrakeRaiseToGated, broker.BrakePark:
			// Neither is a refusal. Both mean the action stops here and waits for a human, which
			// step 7 does; what the brake contributes is the reason, recorded on the trace.
			return string(d.Rule) + " -> " + string(d.Effect), nil
		default:
			return string(d.Rule), fmt.Errorf(
				"step 5: the brake returned effect %q at the gate stage, which is not one this step knows how to obey; refusing rather than guessing", d.Effect)
		}
	})
	if err != nil {
		return nil, err
	}
	if effect == broker.BrakeRaiseToGated || effect == broker.BrakePark {
		s.class.Class = classify.ClassGated
	}
	return nil, nil
}

// --- step 6: undo plan -----------------------------------------------------------------------

// stepUndoPlan attaches the plan and re-checks the class against it.
//
// The generation happened at step 4 (see the comment there). What is left is the part of 03 §4.1
// step 6 that is observable: the plan becomes part of the record, and the class is asserted not to
// have fallen. That assertion is the only thing standing between "the classifier says gated because
// there is no plan" and a future edit that recomputes the class somewhere in between.
func (p *Pipeline) stepUndoPlan(ctx context.Context, s *state) (*broker.Result, error) {
	tr := s.tr
	return nil, p.step(tr, broker.StepUndoPlan, func() (string, error) {
		if s.plan == nil || s.plan.Plan == nil {
			return "", fmt.Errorf("step 6: no undo plan object reached this step; step 4 did not run or did not record one")
		}
		if !s.plan.Undoable() && s.class.Class < classify.ClassGated {
			return "", fmt.Errorf(
				"step 6: the envelope has no usable undo plan (%s) but is classified %s; 06 §4.2 step 6 requires at least gated, so the classification and the plan disagree and the action does not run",
				strings.Join(s.plan.Refusals, "; "), s.class.Class)
		}
		s.record = p.buildRecord(s)
		return fmt.Sprintf("strategy=%s undoable=%t", s.plan.Plan.Strategy, s.plan.Undoable()), nil
	})
}

// --- step 7: gate ----------------------------------------------------------------------------

// stepGate parks a gated action. Nothing below step 7 runs for it, and that is enforced by sealing
// the trace rather than by the caller remembering to return.
func (p *Pipeline) stepGate(ctx context.Context, s *state) (*broker.Result, error) {
	tr := s.tr
	gated := s.class.Class >= classify.ClassGated

	err := p.step(tr, broker.StepGate, func() (string, error) {
		if !gated {
			return "not required (" + s.class.Class.String() + ")", nil
		}
		// The record for a parked action deliberately carries NO pre-state. It was captured at
		// step 3, but by the time a human approves, minutes or hours later, it describes a cluster
		// that has moved on -- and a stale pre-state on a PendingApproval record is an undo plan
		// that restores the wrong bytes. The approval path re-snapshots. Step 8 never ran, and the
		// record says so by omission rather than by carrying something that looks current.
		s.record.Spec.PreState = nil
		s.record.Status.Phase = agentv1alpha1.PhasePendingApproval
		if err := p.cfg.Records.Create(ctx, s.record); err != nil {
			return "", fmt.Errorf("step 7: parking action %s for approval: %w", s.actionID, err)
		}
		if p.cfg.Approvals != nil {
			if err := p.cfg.Approvals.Notify(ctx, s.record, s.brakeView.Roster); err != nil {
				// Parked either way. The action does not become more executable because nobody
				// could be reached, and the record is already durable, so the failure is
				// reportable rather than fatal.
				return "parked, roster not notified: " + err.Error(), nil
			}
		}
		return "parked for approval", nil
	})
	if err != nil {
		return nil, err
	}
	if !gated {
		return nil, nil
	}
	tr.Stop()
	return &broker.Result{
		ActionID:  s.actionID,
		Namespace: p.cfg.Namespace,
		Decision:  "gated",
		Phase:     string(agentv1alpha1.PhasePendingApproval),
		Message:   trace(s, "this action is gated and is waiting for a human: "+reasonsDetail(s.class)),
		Status:    http.StatusAccepted,
	}, nil
}

// --- step 8: snapshot ------------------------------------------------------------------------

// stepSnapshot persists the pre-state captured at step 3 into the ActionRecord, and consults the
// brake at StageSnapshot about whether that worked.
//
// This is the step that makes the record durable, which is what step 9's write-ahead confirmation
// re-reads. The pre-state was READ at step 3 because the plan needed it; it becomes a RECORD here,
// and 06 §4.4 row 4 is about the second of those, not the first.
func (p *Pipeline) stepSnapshot(ctx context.Context, s *state) (*broker.Result, error) {
	tr := s.tr
	return nil, p.step(tr, broker.StepSnapshot, func() (string, error) {
		s.record.Spec.PreState = execute.Records(s.snaps)
		s.record.Status.Phase = agentv1alpha1.PhaseExecuting

		persisted := p.cfg.Records.Create(ctx, s.record)

		d := broker.Decide(broker.BrakeInputs{
			Stage:     broker.StageSnapshot,
			Now:       s.at,
			Agent:     s.brakeView.Agent,
			Scope:     scopeSpecOf(s.brakeView.Agent),
			Freezes:   s.brakeView.Freezes,
			Journal:   s.brakeView.Journal,
			Snapshot:  signal(persisted == nil),
			UndoPlan:  signal(s.plan.Undoable()),
			Contested: p.cfg.Contested,
			Class:     riskClass(s.class.Class),
			Targets:   s.targets,
			Trigger:   agentv1alpha1.ActionTriggerSource(s.env.Trigger.Source),
		})
		if !d.Allowed() {
			if d.Refusal != nil {
				return string(d.Rule), d.Refusal
			}
			return string(d.Rule), fmt.Errorf("step 8: the brake stopped the action after the snapshot (%s): %s", d.Rule, d.Detail)
		}
		if persisted != nil {
			// Belt and braces. The brake owns the decision above and row 4 refuses on a failed
			// snapshot, so this branch should be unreachable -- but "should be unreachable" and
			// "cannot execute unjournaled" are different claims, and only the second one is the
			// invariant.
			return "record-not-durable", fmt.Errorf("step 8: the action record for %s was not written and the brake did not stop it: %w", s.actionID, persisted)
		}
		return fmt.Sprintf("%d pre-states recorded", len(s.record.Spec.PreState)), nil
	})
}

// --- step 9: execute -------------------------------------------------------------------------

func (p *Pipeline) stepExecute(ctx context.Context, s *state) (*broker.Result, error) {
	tr := s.tr
	err := p.step(tr, broker.StepExecute, func() (string, error) {
		ops, err := executeOps(s.env, s.resolved, s.snaps)
		if err != nil {
			return "", err
		}
		res, execErr := p.cfg.Executor.Execute(ctx, execute.Request{
			ActionID:      s.actionID,
			AgentIdentity: agentIdentity(s.id),
			Ops:           ops,
			Snapshots:     s.snaps,
			DryRunOnly:    s.dryRun(),
		})
		// The Result is kept even on error: it carries Mutated, which is the recovery ladder's
		// only input on whether there is anything to roll back. Discarding it here is the bug
		// execute.Execute's doc comment warns about.
		s.exec = res
		if res != nil {
			s.record.Status.Applied = appliedTargets(res)
		}

		// The two ways step 9 can go wrong are not the same thing, and collapsing them is the bug.
		//
		// NOTHING WAS MUTATED -- no applier, a misaligned request, a refusal in the dry-run pass
		// that runs before any real write. The step could not run. It is a fault: the trace ends
		// here, the action does not reach verification, and V-BRK-014's "no mutation exists in the
		// audit log" holds because none was attempted.
		//
		// SOMETHING WAS MUTATED and then the pass failed partway. The step DID run and produced an
		// outcome, and that outcome is a partially-applied cluster. Aborting here would leave it
		// applied, unverified and unrolled-back with nobody looking -- which is exactly the state
		// 06 §4.4 row 9 pages a human for and 04 §5 has a recovery ladder for. So the step is
		// recorded as having completed with a failure, and step 10 runs with that failure in hand.
		if execErr != nil {
			if res == nil || !res.Mutated {
				return "", execErr
			}
			s.execFail = &verify.Failure{Err: execErr, Message: execErr.Error()}
			return "partially applied, handing the failure to the recovery ladder: " + execErr.Error(), nil
		}

		if s.dryRun() {
			return fmt.Sprintf("dry run only, %d operations checked", len(res.Outcomes)), nil
		}
		return fmt.Sprintf("%d operations applied as %s", len(res.Outcomes), res.FieldManager), nil
	})
	if err != nil {
		return nil, err
	}
	return nil, nil
}

// --- step 10: verify -------------------------------------------------------------------------

func (p *Pipeline) stepVerify(ctx context.Context, s *state) (*broker.Result, error) {
	tr := s.tr

	if s.dryRun() {
		// A dry run mutated nothing, so there is nothing to verify: the predicates would evaluate
		// the pre-action cluster and report the action as having failed. Skipped WITH a reason
		// rather than quietly passed over -- see broker.StepTrace.Skip.
		if err := tr.Skip(broker.StepVerify, "dryRun: nothing was mutated, so there is no outcome to verify"); err != nil {
			return nil, err
		}
		return nil, nil
	}

	return nil, p.step(tr, broker.StepVerify, func() (string, error) {
		res, err := p.cfg.Verifier.Run(ctx, verify.Request{
			ActionID:         s.actionID,
			AgentIdentity:    agentIdentity(s.id),
			Targets:          verifyTargets(s.targets),
			UndoPlan:         *s.plan.Plan,
			ExecutionFailure: s.execFail,
		})
		if err != nil {
			return "", fmt.Errorf("step 10: verifying action %s: %w", s.actionID, err)
		}
		s.verify = res

		d := broker.Decide(broker.BrakeInputs{
			Stage:      broker.StagePostExecute,
			Now:        s.at,
			Agent:      s.brakeView.Agent,
			Scope:      scopeSpecOf(s.brakeView.Agent),
			Freezes:    s.brakeView.Freezes,
			Journal:    s.brakeView.Journal,
			Verified:   signal(res.Decision == verify.DecisionVerified),
			RolledBack: rollbackSignal(res),
			Contested:  p.cfg.Contested,
			Class:      riskClass(s.class.Class),
			Targets:    s.targets,
			Trigger:    agentv1alpha1.ActionTriggerSource(s.env.Trigger.Source),
		})
		if d.Effect == broker.BrakeHalt {
			// Row 9: executed, unverified, unrolled. There is nothing left to refuse -- the write
			// landed. The brake's job here is to stop the agent and wake a human, and the pipeline
			// must not report this as a success.
			return string(d.Rule), fmt.Errorf(
				"step 10: action %s executed, could not be verified and could not be rolled back (%s): %s",
				s.actionID, d.Rule, d.Detail)
		}
		return string(res.Decision), nil
	})
}

// --- step 11: journal ------------------------------------------------------------------------

// stepJournal writes the terminal phase.
//
// The record itself was made durable at step 8 -- it has to be, because step 9 refuses to mutate
// anything it cannot re-read (V-REV-002). What step 11 owns is the OUTCOME: the phase, the
// verification result and the recovery ladder, none of which existed when the record was created.
func (p *Pipeline) stepJournal(ctx context.Context, s *state) (*broker.Result, error) {
	tr := s.tr
	return nil, p.step(tr, broker.StepJournal, func() (string, error) {
		phase, message := terminal(s)
		if s.verify.Decision != "" {
			// Written whenever the driver ran, including when it concluded "not passed". A status
			// with no verification block on an action that WAS verified reads, later, as an action
			// nobody checked -- which is the one thing 04 §5 rung 0 is there to make impossible.
			v := s.verify.Verification
			s.record.Status.Verification = &v
		}
		if s.verify.Recovery.Rung != 0 || len(s.verify.Recovery.Transitions) > 0 {
			r := s.verify.Recovery
			s.record.Status.Recovery = &r
		}
		if err := p.cfg.Records.SetPhase(ctx, s.record, phase, message); err != nil {
			return "", fmt.Errorf("step 11: recording the outcome of %s: %w", s.actionID, err)
		}
		return string(phase), nil
	})
}

// terminal maps the verification outcome onto the record phase.
func terminal(s *state) (agentv1alpha1.ActionPhase, string) {
	if s.dryRun() {
		return agentv1alpha1.PhaseDryRun, "dry run: every check ran and nothing was mutated"
	}
	if s.verify.Phase != "" {
		return s.verify.Phase, string(s.verify.Decision)
	}
	return agentv1alpha1.PhaseVerified, "executed"
}

// --- record construction ---------------------------------------------------------------------

func (p *Pipeline) buildRecord(s *state) *agentv1alpha1.ActionRecord {
	retention, err := journal.RetentionFor(riskClass(s.class.Class), s.at)
	if err != nil {
		// RetentionFor only fails on a class it does not know, and the class came from the
		// classifier's own enum. Falling back to the longest retention is the safe direction: a
		// record kept too long is a storage cost, one dropped early is an unanswerable audit.
		retention, _ = journal.RetentionFor(agentv1alpha1.RiskGated, s.at)
	}

	ar := &agentv1alpha1.ActionRecord{
		ObjectMeta: metav1.ObjectMeta{Namespace: p.cfg.Namespace},
		Spec: agentv1alpha1.ActionRecordSpec{
			ActionID:            s.actionID,
			AgentRef:            agentv1alpha1.AgentObjectRef{Name: p.cfg.AgentName, Namespace: p.cfg.Namespace},
			AgentIdentity:       agentIdentity(s.id),
			ActorServiceAccount: p.cfg.ActorServiceAccount,
			Requester: agentv1alpha1.ActionRequester{
				Kind:     agentv1alpha1.RequesterKind(s.env.Requester.Kind),
				ID:       s.env.Requester.ID,
				Platform: s.env.Requester.Platform,
			},
			Trigger: agentv1alpha1.ActionTrigger{
				Source:  agentv1alpha1.ActionTriggerSource(s.env.Trigger.Source),
				Ref:     s.env.Trigger.Ref,
				Detail:  s.env.Trigger.Detail,
				UndoOf:  undoOf(s.env),
				ChainID: chainID(s.env, s.actionID),
			},
			Trace:          &agentv1alpha1.ActionTrace{TraceID: s.env.Trace.TraceID, SpanID: s.env.Trace.SpanID},
			Intent:         truncate(s.env.Intent, MaxIntentLen),
			Rationale:      truncate(s.env.Rationale, MaxIntentLen),
			IdempotencyKey: s.env.IdempotencyKey,
			// The EFFECTIVE value, not the envelope's. `spec.dryRun` on the record is the field
			// V-BRK-024's history source filters on to decide what an agent has actually done, and
			// the whole point of shadow mode is that it has done nothing. A shadow record carrying
			// `dryRun: false` would teach the classifier familiarity with an action that never ran.
			DryRun: s.dryRun(),
			Classification: agentv1alpha1.ActionClassification{
				Class:         riskClass(s.class.Class),
				Reasons:       append(recordReasons(s.class), undoReason(s.plan)...),
				Undoable:      s.plan.Undoable(),
				PolicySources: s.class.PolicySources,
			},
			Targets:   s.targets,
			PreState:  execute.Records(s.snaps),
			Undo:      s.plan.Plan,
			Retention: retention,
		},
	}
	return ar
}

// --- conversions -----------------------------------------------------------------------------
//
// Every function below turns one package's type into the next one's. They are boring on purpose
// and they are all in one place on purpose: a group/version split done inline at three call sites
// is a bug that is wrong invisibly, since both halves are strings (undo.Operation.KindRef makes the
// same argument about the same conversion).

// rawOps turns envelope operations into classifier inputs and the record's target list.
func rawOps(env *broker.Envelope) ([]classify.RawOp, []agentv1alpha1.TargetRef, error) {
	raws := make([]classify.RawOp, 0, len(env.Operations))
	targets := make([]agentv1alpha1.TargetRef, 0, len(env.Operations))
	for i, op := range env.Operations {
		ref, err := targetRef(op)
		if err != nil {
			return nil, nil, fmt.Errorf("operation %d: %w", i, err)
		}
		patch, err := patchOps(op)
		if err != nil {
			return nil, nil, fmt.Errorf("operation %d: %w", i, err)
		}
		targets = append(targets, ref)
		raws = append(raws, classify.RawOp{
			Verb:        op.Op,
			Kind:        classify.KindRef{Group: ref.Group, Kind: ref.Kind},
			Namespace:   ref.Namespace,
			Name:        ref.Name,
			Patch:       patch,
			Payload:     payloadOf(op),
			ObjectCount: 1,
		})
	}
	return raws, targets, nil
}

// targetRef normalizes the three target shapes onto one ref.
func targetRef(op broker.Operation) (agentv1alpha1.TargetRef, error) {
	switch {
	case op.Target != nil:
		return agentv1alpha1.TargetRef{
			Group:     op.Target.Group,
			Version:   op.Target.Version,
			Kind:      op.Target.Kind,
			Namespace: op.Target.Namespace,
			Name:      op.Target.Name,
		}, nil
	case op.TargetSelector != nil:
		// A selector is expanded against live state before classification (06 §4.2 blast radius).
		// That expansion is not built, and the honest thing is to say so rather than to classify
		// the selector as though it named one object -- which is precisely the "matches three
		// today and three hundred tomorrow" failure the spec calls out.
		return agentv1alpha1.TargetRef{}, &broker.Refusal{
			Status:  http.StatusNotImplemented,
			Reason:  "selector-expansion-unavailable",
			Detail:  "this broker cannot expand a targetSelector against live state, and classifying an unexpanded selector would understate its blast radius; name the targets explicitly",
			Journal: true,
		}
	case op.CloudTarget != nil:
		return agentv1alpha1.TargetRef{}, &broker.Refusal{
			Status:  http.StatusNotImplemented,
			Reason:  "cloud-target-unavailable",
			Detail:  "this broker executes Kubernetes operations only; the cloud write path is not built into this binary",
			Journal: true,
		}
	}
	return agentv1alpha1.TargetRef{}, fmt.Errorf("no target, targetSelector or cloudTarget; envelope validation should have refused this")
}

// patchOps turns a JSON Patch body into the classifier's op list, which is what TouchedPaths reads.
//
// Only the JSON Patch media type produces one. A merge patch and an apply patch are OBJECTS whose
// touched paths are the shape of the object itself, and the classifier derives those from Payload
// instead -- so returning nil for them is the correct answer, not a gap.
func patchOps(op broker.Operation) ([]classify.PatchOp, error) {
	if op.Patch == nil || op.Patch.Type != mediaJSONPatch {
		return nil, nil
	}
	raw, err := json.Marshal(op.Patch.Body)
	if err != nil {
		return nil, fmt.Errorf("re-encoding the json-patch body: %w", err)
	}
	var ops []classify.PatchOp
	if err := json.Unmarshal(raw, &ops); err != nil {
		// Envelope validation already accepted this body, so a failure here means the two
		// disagree about the shape -- and classifying a patch whose operations could not be read
		// would classify it as touching nothing at all.
		return nil, fmt.Errorf("a json-patch body that passed validation does not decode as a patch list: %w", err)
	}
	return ops, nil
}

// payloadOf is what the secret scanner and the merge-patch path read. A patch body counts: a merge
// patch carrying a secret value is the same exfiltration as a create carrying one.
func payloadOf(op broker.Operation) any {
	switch {
	case op.DesiredState != nil:
		return op.DesiredState
	case op.Patch != nil && op.Patch.Type != mediaJSONPatch:
		return op.Patch.Body
	default:
		return nil
	}
}

// patchBody re-encodes the patch for the API server. The envelope decoded it into `any`, so this
// is a round-trip rather than a passthrough -- and it is the only place that conversion happens.
func patchBody(op broker.Operation) (string, []byte, error) {
	if op.Patch == nil {
		return "", nil, nil
	}
	body, err := json.Marshal(op.Patch.Body)
	if err != nil {
		return "", nil, fmt.Errorf("re-encoding the patch body: %w", err)
	}
	return op.Patch.Type, body, nil
}

// undoOps pairs each envelope operation with the pre-state captured for it.
func undoOps(env *broker.Envelope, snaps []execute.Snapshot) []undo.Operation {
	byIndex := make(map[int]execute.Snapshot, len(snaps))
	for _, s := range snaps {
		byIndex[s.TargetIndex] = s
	}
	out := make([]undo.Operation, 0, len(env.Operations))
	for i, op := range env.Operations {
		snap, captured := byIndex[i]
		u := undo.Operation{
			Verb:   op.Op,
			Target: snap.Ref,
			// A snapshot that is absent from the map is a snapshot that was never attempted, which
			// is a different thing from one that failed. CaptureAll is all-or-nothing, so by the
			// time this runs either every index is present or step 3 already aborted -- but the
			// planner distinguishes the two cases and must be told the truth about which this is.
			SnapshotFailed: !captured,
			Existed:        snap.Existed,
			PreState:       snap.Live,
		}
		if op.Scale != nil && snap.Live != nil {
			u.PriorReplicas = replicasOf(snap.Live)
		}
		out = append(out, u)
	}
	return out
}

// executeOps builds the execution request, carrying each op's classification with it so the
// integrity check compares an operation's effect against its OWN permission.
func executeOps(env *broker.Envelope, resolved []classify.ResolvedOp, snaps []execute.Snapshot) ([]execute.Op, error) {
	if len(resolved) != len(env.Operations) {
		return nil, fmt.Errorf(
			"step 9: %d operations resolved for %d submitted; an execution whose ops and classifications are misaligned would check one operation's effect against another's permission",
			len(resolved), len(env.Operations))
	}
	byIndex := make(map[int]execute.Snapshot, len(snaps))
	for _, s := range snaps {
		byIndex[s.TargetIndex] = s
	}

	out := make([]execute.Op, 0, len(env.Operations))
	for i, op := range env.Operations {
		snap, ok := byIndex[i]
		if !ok {
			return nil, fmt.Errorf("step 9: operation %d has no pre-state; step 3 did not capture one", i)
		}
		e := execute.Op{
			Index: i,
			Verb:  op.Op,
			Ref:   snap.Ref,
			Classified: execute.Classified{
				TargetIndex:  i,
				Verb:         op.Op,
				WholeObject:  resolved[i].WholeObject,
				TouchedPaths: resolved[i].TouchedPaths,
			},
		}
		if op.DesiredState != nil {
			e.Desired = &unstructured.Unstructured{Object: op.DesiredState}
		}
		if op.Patch != nil {
			mt, body, err := patchBody(op)
			if err != nil {
				return nil, fmt.Errorf("step 9: operation %d: %w", i, err)
			}
			e.PatchType, e.PatchBody = mt, body
		}
		if op.Scale != nil {
			e.Replicas = op.Scale.Replicas
		}
		if op.Delete != nil {
			e.DeleteOpts = execute.DeleteOpts{
				PropagationPolicy:  op.Delete.PropagationPolicy,
				GracePeriodSeconds: op.Delete.GracePeriodSeconds,
				// The UID precondition comes from the SNAPSHOT, never from the envelope: the
				// envelope's pin is from classification time, and what is about to be deleted is
				// the object step 3 read (execute.DeleteOpts.UID says the same).
				UID: string(snap.Ref.UID),
			}
		}
		out = append(out, e)
	}
	return out, nil
}

func verifyTargets(refs []agentv1alpha1.TargetRef) []verify.Target {
	out := make([]verify.Target, 0, len(refs))
	for _, r := range refs {
		out = append(out, verify.Target{Ref: r})
	}
	return out
}

func appliedTargets(res *execute.Result) []agentv1alpha1.AppliedTarget {
	out := make([]agentv1alpha1.AppliedTarget, 0, len(res.Outcomes))
	for _, o := range res.Outcomes {
		if o.Applied != nil {
			out = append(out, *o.Applied)
		}
	}
	return out
}

func recordReasons(c *classify.Classification) []agentv1alpha1.ClassificationReason {
	out := make([]agentv1alpha1.ClassificationReason, 0, len(c.Reasons))
	for _, r := range c.Reasons {
		out = append(out, agentv1alpha1.ClassificationReason{Rule: r.Rule, Class: r.Class, Detail: r.Detail})
	}
	return out
}

func riskClass(c classify.Class) agentv1alpha1.ActionRiskClass {
	return agentv1alpha1.ActionRiskClass(c.String())
}

// signal converts a boolean observation into the brake's three-valued signal. There is no path
// here that produces BrakeUnobserved, because every caller of this function DID look -- an
// unobserved signal must come from not calling it at all.
func signal(ok bool) broker.BrakeSignal {
	if ok {
		return broker.BrakeOK
	}
	return broker.BrakeFailed
}

// rollbackSignal reports whether a rollback was attempted and worked. Unobserved when no rollback
// was attempted, which is the honest answer and the one 06 §4.4 row 9 needs: it fires only when
// verification failed AND rollback failed, and "no rollback was needed" is neither.
func rollbackSignal(res verify.Result) broker.BrakeSignal {
	switch res.Decision {
	case verify.DecisionRolledBack:
		return broker.BrakeOK
	case verify.DecisionPaged:
		// 04 §5: the driver pages precisely when the rollback itself failed. That is the input
		// row 9 of the brake table is asking about, and it is the only way to answer it.
		return broker.BrakeFailed
	default:
		return broker.BrakeUnobserved
	}
}

func scopeSpecOf(a *agentv1alpha1.Agent) *agentv1alpha1.ScopeSpec {
	if a == nil {
		return nil
	}
	return a.Spec.Scope
}

func agentIdentity(id *broker.Identity) string {
	return id.AgentIdentity()
}

// undoOf is the action this one reverses. The envelope has no `undoOf` field of its own: 06 §4.1
// carries it in `trigger.ref`, which is free-form for every other source and an action id for this
// one. Read here rather than at three call sites so the coupling is written down once.
func undoOf(env *broker.Envelope) string {
	if env.Trigger.Source == string(agentv1alpha1.ActionTriggerUndo) {
		return env.Trigger.Ref
	}
	return ""
}

// chainID groups an action with the one it reverses, so a mutation and its undo are one story in
// the journal (03 §8). A non-undo action chains to itself.
func chainID(env *broker.Envelope, actionID string) string {
	if u := undoOf(env); u != "" {
		return u
	}
	return actionID
}

// undoReason surfaces WHY a plan is unusable as classification reasons, because the API type has
// nowhere else to put it and "undoable: false" with no reason is an audit dead end.
func undoReason(plan *undo.Result) []agentv1alpha1.ClassificationReason {
	if plan == nil || plan.Undoable() || len(plan.Refusals) == 0 {
		return nil
	}
	return []agentv1alpha1.ClassificationReason{{
		Rule:   "undo-plan-unusable",
		Class:  string(agentv1alpha1.RiskGated),
		Detail: strings.Join(plan.Refusals, "; "),
	}}
}

func reasonsDetail(c *classify.Classification) string {
	parts := make([]string, 0, len(c.Reasons))
	for _, r := range c.Reasons {
		parts = append(parts, r.String())
	}
	if len(parts) == 0 {
		return "no reason was recorded, which is itself a classifier bug"
	}
	return strings.Join(parts, "; ")
}

func replicasOf(obj *unstructured.Unstructured) *int32 {
	n, found, err := unstructured.NestedInt64(obj.Object, "spec", "replicas")
	if err != nil || !found {
		return nil
	}
	r := int32(n) //nolint:gosec // a replica count above 2^31 is not representable in the API type either
	return &r
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n-1] + "…"
}

// trace annotates the caller-facing message. The EFFECTIVE value: an agent that asked to execute
// and was shadowed has to be told that nothing happened, or its next decision is made on the belief
// that something did.
func trace(s *state, msg string) string {
	if s.dryRun() {
		return msg + " (dry run: nothing was mutated)"
	}
	return msg
}
