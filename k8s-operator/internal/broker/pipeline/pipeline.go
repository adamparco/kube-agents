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
	"sort"
	"strings"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
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

// The envelope string values this package has to recognize by name. They are validated by the
// envelope's own closed enums, so these are reads of a shared vocabulary rather than second ones.
const (
	mediaJSONPatch  = "application/json-patch+json"
	mediaApplyPatch = "application/apply-patch+yaml"

	// scaleReplicasPointer is where a scale writes, as the classifier's rules name it. A constant
	// because it is asserted from two directions -- fillTouchedPaths writes it and the /scale
	// subresource's diff is checked against it -- and two spellings of the same pointer would make
	// the integrity check reject every scale.
	scaleReplicasPointer = "/spec/replicas"
)

// RecordStore is the ActionRecord persistence seam. `*journal.Store` satisfies it.
type RecordStore interface {
	Create(ctx context.Context, ar *agentv1alpha1.ActionRecord) error
	Get(ctx context.Context, namespace, actionID string) (*agentv1alpha1.ActionRecord, error)
	SetPhase(ctx context.Context, ar *agentv1alpha1.ActionRecord, phase agentv1alpha1.ActionPhase, message string) error
	// UpdateForResume persists a fresh pre-state snapshot and undo plan onto an already-created
	// record — the resumption loop's write, since Create is a no-op past the record's first write.
	UpdateForResume(ctx context.Context, ar *agentv1alpha1.ActionRecord, preState []agentv1alpha1.PreStateSnapshot, undo *agentv1alpha1.UndoPlan) error
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

// Planner produces the undo plan for an envelope's operations AND validates it.
// undo.GenerateAndValidate is the implementation; the interface exists so that the failure mode is
// injectable -- see Config.Planner.
//
// The dry-runner is a parameter rather than something the implementation closes over, and that is
// the whole correction of P9-T8b-4b-ii-2b-i. The seam used to be `Generate` alone, defaulted to
// `undo.Generate`, and `undo.GenerateAndValidate` -- whose own doc comment reads "the call the
// broker actually makes at step 6" -- had no caller outside its tests. Every ActionRecord the
// broker wrote therefore carried `undoPlan.validated: false`, and undo.ValidateReplayable refuses
// on exactly that field, so both replay paths refused every plan ever journaled. A signature that
// cannot express "generate without validating" is what stops that returning.
type Planner interface {
	Generate(ctx context.Context, req undo.Request, idx undo.ReferenceIndex, dr undo.DryRunner) (*undo.Result, error)
}

// PlannerFunc adapts a function to Planner. undo.GenerateAndValidate satisfies it directly.
type PlannerFunc func(ctx context.Context, req undo.Request, idx undo.ReferenceIndex, dr undo.DryRunner) (*undo.Result, error)

// Generate calls f.
func (f PlannerFunc) Generate(ctx context.Context, req undo.Request, idx undo.ReferenceIndex, dr undo.DryRunner) (*undo.Result, error) {
	return f(ctx, req, idx, dr)
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
	// Planner generates the undo plan and validates it. Defaults to undo.GenerateAndValidate; it is
	// a seam for the same reason every other dependency here is one. Whether a plan could be
	// produced is a gating input (06 §4.2 rule 6, 06 §4.4 row 5), and a pipeline whose planner
	// cannot be made to fail is a pipeline whose behaviour on an unplannable action is untested --
	// which is precisely the behaviour that decides between "gated" and "executed with no way back".
	Planner Planner

	// DryRunner yields the plan-time dry-run client for one submission's identity, which is
	// 06 §4.3.1's "validated by dry-running each step against the API server".
	//
	// A function of the identity rather than a fixed object, because the dry run has to be issued
	// with the SAME field manager the replay would use. Server-side apply reports a conflict for
	// every field owned by a different manager, and the fields an undo restores are frequently the
	// ones this agent set in an earlier action -- so a dry run under any other name would report
	// conflicts the real replay never hits, downgrade a working plan, and gate the action for a
	// reason that is an artifact of the check.
	//
	// REQUIRED, and it is the one seam in this Config whose absence is silent rather than loud. A
	// nil Executor 500s on the first action; a nil DryRunner produces a broker that serves every
	// request, journals every record with `validated: false`, and has a dead undo path that nobody
	// discovers until a human is trying to reverse an outage. New refuses it for that reason.
	DryRunner func(agentIdentity string) undo.DryRunner

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
	case cfg.DryRunner == nil:
		return nil, errors.New("pipeline: a DryRunner is required; 06 §4.3.1 validates each undo step against the API server before the action runs, and a broker without one journals `undoPlan.validated: false` on every record -- which undo.ValidateReplayable refuses, so nothing it ever did could be rolled back")
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	if cfg.Planner == nil {
		cfg.Planner = PlannerFunc(undo.GenerateAndValidate)
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

	// classifiedAt is step 4's finishing instant, held here because the record does not exist yet
	// when it is taken: `buildRecord` runs at step 6. Every other beat of the lifecycle clock is
	// stamped straight onto `s.record.Status.Timestamps`, because by then there is one.
	classifiedAt time.Time

	targets  []agentv1alpha1.TargetRef
	resolved []classify.ResolvedOp
	snaps    []execute.Snapshot

	// baselines are the pre-action observations step 10 cannot reconstruct, positional with
	// targets. Captured at step 3 because that is the last moment they are still "pre-action".
	baselines []*int64

	class     *classify.Classification
	plan      *undo.Result
	brakeView BrakeView

	// brakeEffect is stepBrake's own verdict, kept for callers that need to distinguish "the brake
	// raised the class" from "the classifier did" -- Resume is the one caller that does, because
	// stepGate (the only step Submit relies on to act on either) is not on the resume path.
	brakeEffect broker.BrakeEffect

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
// The ORDER of the two is load-bearing, and it is snapshot first. An apply, a scale and a merge
// patch all arrive as objects rather than as patch operations, so the field set 06 §4.2 matches
// `when.fieldPaths` against has to be COMPUTED, and for an apply computing it means diffing the
// desired object against live state. That makes the snapshot an input to classification and not
// merely a companion of it: resolve-then-capture would have to either read live state twice or
// classify an apply as touching no fields at all.
//
// It is also why "resolve scope" is step 3 and not a formality. 03 §4.1 says one out-of-scope
// target rejects the whole envelope; the containment verdict itself is rendered at step 4, because
// 06 §4.2 makes scope the classifier's own evaluation-order step 1 and re-deciding it here would be
// a second implementation of `scope.Contains`. What step 3 owns is the resolution the verdict needs.
func (p *Pipeline) stepResolve(ctx context.Context, s *state) (*broker.Result, error) {
	tr := s.tr
	return nil, p.step(tr, broker.StepResolveScope, func() (string, error) {
		ag := p.cfg.Brake.Observe(ctx).Agent
		s.caller = classify.Caller{
			Name:           p.cfg.AgentName,
			Tier:           string(s.id.Tier),
			Scope:          scope.Of(ag),
			ServingCluster: servingCluster(ag),
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

		snaps, err := execute.CaptureAll(ctx, p.cfg.Reader, s.actionID, targets, metav1.NewTime(s.at), p.cfg.BodyStore)
		if err != nil {
			// V-BRK-018: all snapshots or none. CaptureAll already refuses to return a partial set;
			// what this branch adds is that a failure here stops the pipeline at step 3, so no
			// classification, no plan and no execution ever sees a half-read cluster.
			return "", liveReadRefusal(err,
				fmt.Sprintf("step 3: capturing pre-state for %d targets", len(targets)))
		}
		s.snaps = snaps

		// Back-fill the identity CaptureAll actually observed: `Snapshot.Ref` is "re-pinned from
		// what was actually read" (its own doc comment), and TargetRef.UID's doc comment promises
		// exactly this ("pins the object identity at classification time") for a target that
		// existed. Indexed by TargetIndex rather than assumed positional, matching executeOps'
		// own byIndex construction below -- CaptureAll's return order is not a contract this file
		// otherwise relies on. This is what lets the resumption loop (chat-approval.md §3, 06 §4.4)
		// notice a target deleted and recreated between classification and approval: the record
		// carries the UID an approver's roster consented to, not just a name.
		byIndex := make(map[int]agentv1alpha1.TargetRef, len(snaps))
		for _, snap := range snaps {
			byIndex[snap.TargetIndex] = snap.Ref
		}
		for i := range targets {
			if ref, ok := byIndex[i]; ok {
				targets[i].UID = ref.UID
				targets[i].ResourceVersion = ref.ResourceVersion
			}
		}
		s.targets = targets

		// The other pre-action read. Its window of validity is the same as the snapshot's -- once
		// step 5 has written, "the restart count before the action" is unrecoverable, and a row that
		// cannot be evaluated is a rollback at the end of the settle window rather than a pass. So
		// it is captured here, next to the snapshots, and refuses on the same all-or-nothing terms.
		baselines, err := verify.CaptureRestartBaselines(ctx, p.cfg.Verifier.Prober, targets)
		if err != nil {
			return "", liveReadRefusal(err,
				fmt.Sprintf("step 3: capturing pre-action restart baselines for %d targets", len(targets)))
		}
		s.baselines = baselines

		// Between the two reads, not after them: an apply's changed-field set is a diff against the
		// live state CaptureAll just returned, and classify.Resolve is the consumer of it.
		if err := fillTouchedPaths(s.env, raws, snaps); err != nil {
			return "", err
		}

		resolved, err := classify.Resolve(ctx, p.cfg.Live, s.caller, raws)
		if err != nil {
			return "", liveReadRefusal(err,
				fmt.Sprintf("step 3: resolving %d operations against live state", len(raws)))
		}
		s.resolved = resolved

		return fmt.Sprintf("%d operations, %d pre-states", len(resolved), len(snaps)), nil
	})
}

// liveReadRefusal types a failure of one of step 3's live reads. V-BRK-031.
//
// All three reads above are made with the ACTOR identity, so "the read failed" has two entirely
// different meanings and only one of them is transient. An API-server timeout or a 500 may well
// succeed a minute later, which is what `snapshot-failed` and its Retry-After are for. An RBAC
// denial will not: the actor's tier template does not reach this object and will not reach it in a
// minute, so telling the caller to wait and retry is telling a loop to run forever against a
// permission boundary. `Refusal.RetryAfterSeconds` documents that split as a rule of the type --
// "zero means do not retry, which is the right answer for every schema and authorization refusal"
// -- and this is the site that was not honouring it.
//
// BEFORE THIS EXISTED, NEITHER ANSWER WAS GIVEN. The error went back as a bare `fmt.Errorf`,
// `server.go`'s `refuse` looked for a `*Refusal`, did not find one, and answered 500
// `internal-error` with a stack trace in the broker log. Two consequences, and the second is the
// one that matters:
//
//   - A caller could not tell its own authority ceiling from a broken broker. In shadow mode that
//     is EVERY action, because the phase-9 actor holds the 06 §2.2.1 grant and no tenant authority
//     at all -- so the entire pipeline reported itself as crashing while behaving exactly as
//     designed.
//   - `Journal` and `SecurityEvent` are carried ON the Refusal, so with no Refusal there is no
//     journal entry and no event. The envelope's disposition was recorded NOWHERE. An agent
//     enumerating what it may touch left no trace at all -- which is the opposite of what 06 §4.1's
//     per-reason table exists to guarantee.
//
// The forbidden arm journals and does NOT raise a security event, deliberately. The journal is the
// complete record of every envelope's disposition, and that is what makes a probing pattern findable
// by analysis. A security event is an alarm, and in shadow mode an alarm on every single action is
// an alarm that gets muted -- which would cost more than it buys, including for the events that do
// matter. 03 §6's security events are for identity violations: a caller that is not who it says it
// is. This is an authorization outcome for a correctly authenticated caller, and
// `forbidden-caller` (V-BRK-010) remains the identity case that alarms.
//
// `what` names WHICH of the three reads failed. A refusal that says only "the pre-action read
// failed" sends a human to the wrong object, which is the same argument `CaptureAll` makes about
// naming the target index.
func liveReadRefusal(err error, what string) error {
	if err == nil {
		return nil
	}
	// IsUnauthorized alongside IsForbidden because an expired or rejected credential is equally
	// permanent from the caller's side and equally not a broker fault; both are answers about
	// authority, and neither becomes true by waiting.
	if apierrors.IsForbidden(err) || apierrors.IsUnauthorized(err) {
		return &broker.Refusal{
			Status:  http.StatusForbidden,
			Reason:  broker.ReasonTargetForbidden,
			Detail:  what + ": " + err.Error(),
			Journal: true,
		}
	}
	return &broker.Refusal{
		Status:            http.StatusServiceUnavailable,
		Reason:            broker.ReasonSnapshotFailed,
		Detail:            what + ": " + err.Error(),
		Journal:           true,
		RetryAfterSeconds: broker.PausedRetryAfterSeconds,
	}
}

// servingCluster reads the cluster this broker is installed in off the Agent CR it serves.
//
// From the same CR as the caller's scope, and for the same reason (03 §4.1 step 1: the broker
// derives its caller from the authenticated identity, never from the envelope), but from a
// different field. `spec.scope.clusterName` is the authority ceiling and is empty for the platform
// tier by design; `spec.harness.clusterName` is where the agent runs and is populated for every
// tier by the install template. classify.ScopeOfTarget documents why the target scope needs the
// second one and what happens when it is missing.
//
// Nil-tolerant in both steps: a broker whose brake has not yet observed an Agent gets "", which
// fails closed at the ownership lookup rather than panicking mid-envelope.
func servingCluster(agent *agentv1alpha1.Agent) string {
	if agent == nil || agent.Spec.Harness == nil {
		return ""
	}
	return agent.Spec.Harness.ClusterName
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
		// Validated here too, in the same call, because the validation VERDICT is a classification
		// input: a step that will not apply downgrades the plan to `none`, which is what makes
		// UndoPlanPresent false below, which is what raises the action to gated. 06 §4.3.1 says so
		// directly -- "if generation or validation fails, the action is raised to gated" -- and
		// there is no later step at which that could still be arranged, because by step 6 the class
		// is already fixed.
		plan, err := p.cfg.Planner.Generate(ctx, undo.Request{
			Operations:  undoOps(s.env, s.snaps),
			GeneratedAt: metav1.NewTime(s.at),
		}, p.cfg.Refs, p.cfg.DryRunner(agentIdentity(s.id)))
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
		// After the refusal arms, not before: `status.timestamps.classified` is "when the classifier
		// returned" (06 §4.3), and a submission that ends in `forbidden` never got an answer to
		// carry forward. The record built for a refusal is a different object on a different path.
		s.classifiedAt = p.cfg.Now().UTC()
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
	s.brakeEffect = effect
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
		// VALIDATED, not merely Undoable, and the difference is the property this guard now carries.
		//
		// The classifier is fed `Undoable()`, because "is there a plan" is the input 06 §4.2 step 6
		// names. This asks the stronger question -- was every step of it dry-run and would it apply
		// -- and the two answers are identical only if something actually validated the plan.
		// Nothing did, for five phases: the seam was generate-only and every record shipped with
		// `validated: false` while ValidateReplayable, the front door of both replay paths, refuses
		// on exactly that field. Asking here means a planner that skips validation cannot reach
		// execution, whoever wired it, rather than producing an action whose rollback is already
		// known to be unusable and saying nothing.
		//
		// The dry-run suppression is classify.UndoPlanGateApplies rather than a second `!DryRun`
		// written out here. A dry-run envelope mutates nothing and terminates at PhaseDryRun, so
		// the classifier deliberately excuses it from the undo-plan floor, and one predicate is how
		// two sites stay unable to disagree about when it applies.
		//
		// It is shared for that reason and not because today's behaviour needs it: the brake's
		// 06 §4.4 row 5 -- a THIRD spelling, which does not suppress for dry runs -- has already
		// raised the class by the time this runs, so the second conjunct below is false whichever
		// way the first is written. That third site is the outstanding one; see
		// TestADryRunWhoseUndoPlanWouldNotApplyIsNotAServerFault.
		if classify.UndoPlanGateApplies(s.env.DryRun, s.plan.Validated()) && s.class.Class < classify.ClassGated {
			return "", fmt.Errorf(
				"step 6: the envelope has no validated undo plan (strategy=%s validated=%t; %s) but is classified %s; 06 §4.2 step 6 requires at least gated, so the classification and the plan disagree and the action does not run",
				s.plan.Plan.Strategy, s.plan.Plan.Validated, strings.Join(s.plan.Refusals, "; "), s.class.Class)
		}
		s.record = p.buildRecord(s)
		return fmt.Sprintf("strategy=%s undoable=%t validated=%t", s.plan.Plan.Strategy, s.plan.Undoable(), s.plan.Validated()), nil
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
		// The mutating payload DOES need to survive the park, unlike the pre-state: resumption
		// re-snapshots and re-plans, but nothing re-derives what the operations would WRITE, because
		// that never lived anywhere but the envelope the HTTP request carried (see EnvelopeJSON's
		// own doc comment). A marshal failure here is the broker's own bug, not a caller error, and
		// is a hard stop: a record parked without it can never resume, and reporting the park as
		// successful anyway would hide that until the approval that arrives too late to fix it.
		envelopeJSON, err := json.Marshal(s.env)
		if err != nil {
			return "", fmt.Errorf("step 7: marshaling the envelope for action %s to preserve across the approval wait: %w", s.actionID, err)
		}
		s.record.Spec.EnvelopeJSON = string(envelopeJSON)
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

		// V-BRK-006's L2 EVIDENCE IS THIS LINE, and it must be read off the wall clock rather than
		// off `s.at`. The check compares `metadata.creationTimestamp` -- assigned by the API SERVER
		// at step 8 -- against `status.timestamps.executionStarted`, and reads any inversion as a
		// broker that executed before it journaled. `s.at` is the submission instant, frozen before
		// step 3, so stamping it here would make every record ever written claim it started
		// executing several steps before the API server had heard of it: a fabricated violation of
		// the one ordering the write-ahead rule exists to establish.
		//
		// Stamped for a dry run too. `execute.Execute` issues real API calls with `client.DryRunAll`
		// -- a server-side dry run is authorized and admitted before it is discarded -- so a mutating
		// call WAS issued, which is what the field records. A shadow run that left this nil would
		// make the write-ahead ordering unobservable on exactly the path Phase 9 runs.
		s.clock().ExecutionStarted = ptrTime(p.cfg.Now())

		res, execErr := p.cfg.Executor.Execute(ctx, execute.Request{
			ActionID:      s.actionID,
			AgentIdentity: agentIdentity(s.id),
			Ops:           ops,
			Snapshots:     s.snaps,
			DryRunOnly:    s.dryRun(),
		})
		// Before the error branches below, for the same reason the Result is: "when the last
		// mutating call returned" is answerable whether or not the pass succeeded, and it is the
		// base for `undoWindowExpiresAt`. A partially-applied action is precisely the one whose undo
		// window a human needs, so it is the one case where losing this would matter most.
		s.clock().ExecutionEnded = ptrTime(p.cfg.Now())

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
		targets, err := verifyTargets(s.env, s.targets, s.baselines)
		if err != nil {
			return "", fmt.Errorf("step 10: assembling verification targets for action %s: %w", s.actionID, err)
		}
		res, err := p.cfg.Verifier.Run(ctx, verify.Request{
			ActionID:         s.actionID,
			AgentIdentity:    agentIdentity(s.id),
			Targets:          targets,
			UndoPlan:         *s.plan.Plan,
			ExecutionFailure: s.execFail,
		})
		if err != nil {
			return "", fmt.Errorf("step 10: verifying action %s: %w", s.actionID, err)
		}
		s.verify = res
		s.clock().Verified = ptrTime(p.cfg.Now())

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
		// THE LIFECYCLE CLOCK (06 §4.3), opened here so that the two beats that already happened
		// are durable from the record's very first write -- step 7 and step 8 both Create, and
		// `Store.Create` sends the whole status block through the subresource.
		//
		// Three readers depend on this and every one of them was silently degraded while nothing
		// wrote it: `budget.go` reads `submitted` to place an action in its window,
		// `cooldown.go` reads the execution pair, and `JournalReconciler.exportLateness` reads four
		// of the six to decide how late an export was -- falling back to `creationTimestamp` when
		// it finds none, which is exactly what it did on every record ever written.
		//
		// `approved` is deliberately absent and stays nil here. It is "when the roster was
		// satisfied", the roster is the ChatOps gateway's business, and 06 §4.3's principals table
		// gives `approvals` to that SA and not to this one. The broker stamping an approval time is
		// the broker asserting an approval happened.
		Status: agentv1alpha1.ActionRecordStatus{
			Timestamps: &agentv1alpha1.ActionTimestamps{
				Submitted:  ptrTime(s.at),
				Classified: ptrTime(s.classifiedAt),
			},
		},
	}
	return ar
}

// ptrTime is metav1.NewTime behind a pointer, with the zero instant rendered as nil rather than as
// the beginning of the epoch. `ActionTimestamps`'s own doc says "nil means the phase was never
// reached", and a `0001-01-01T00:00:00Z` in an audit record does not mean that -- it means a clock
// nobody set, wearing the shape of a real answer.
// clock returns the record's timestamp block, creating it if the record does not have one.
//
// Not defensive programming for its own sake: `status` is a SUBRESOURCE, so the API server drops
// the whole block from the object the broker POSTs at step 8 and hands back a record whose
// `status.timestamps` is nil. `journal.Store.Create` puts the broker-owned fields back, but the
// pipeline must not be one refactor of the store away from a nil dereference HERE -- at step 8,
// after the write-ahead record is durable and before the executor has run. A panic at that point
// leaves a record in `Executing` that no code path will ever advance, which is strictly worse than
// the missing timestamp it would be crashing about. Fail closed means refuse, not die.
func (s *state) clock() *agentv1alpha1.ActionTimestamps {
	if s.record.Status.Timestamps == nil {
		s.record.Status.Timestamps = &agentv1alpha1.ActionTimestamps{}
	}
	return s.record.Status.Timestamps
}

func ptrTime(t time.Time) *metav1.Time {
	if t.IsZero() {
		return nil
	}
	mt := metav1.NewTime(t.UTC())
	return &mt
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
// Only the JSON Patch media type produces one here, because only it arrives already in this shape.
// Every other field-level verb carries an OBJECT, and its op list is computed by fillTouchedPaths
// once live state is in hand -- returning nil for them at this point is incompleteness, not the
// answer.
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

// fillTouchedPaths gives the classifier the changed-field set for the verbs that do not carry one.
//
// A JSON Patch arrives as a list of operations and patchOps has already read it. Every other
// field-level verb arrives as an OBJECT, and until this function existed the classifier was shown
// an empty path set for all of them -- so `when.fieldPaths` could not fire on an apply, a scale or
// a merge patch, which is most of what an agent sends. 06 §4.2 matches fieldPaths against the
// fields the operation touches, and "the operation is an object, so we do not know" is not one of
// the answers that section admits.
//
// It runs between execute.CaptureAll and classify.Resolve because that is the only window in which
// both halves of the answer exist: an apply's touched fields are a diff against live state, and
// live state is what CaptureAll just read. See stepResolve for why that fixes the order of step 3.
//
// raws is indexed in lockstep with env.Operations -- rawOps builds one entry per operation and
// returns early on the first it cannot convert, so a short slice cannot reach here.
func fillTouchedPaths(env *broker.Envelope, raws []classify.RawOp, snaps []execute.Snapshot) error {
	live := make(map[int]*unstructured.Unstructured, len(snaps))
	for _, s := range snaps {
		live[s.TargetIndex] = s.Live
	}

	for i := range raws {
		op := env.Operations[i]
		var (
			ops []classify.PatchOp
			err error
		)
		switch {
		case op.Op == "apply":
			ops, err = diffPatchOps(i, live[i], op.DesiredState)

		case op.Op == "patch" && op.Patch != nil && op.Patch.Type == mediaApplyPatch:
			// An apply patch is an apply that came in through the patch verb. Same object, same
			// server-side merge, so the same answer -- classifying it by the shape of its body
			// instead would give the same change two different field sets depending on which
			// spelling the agent chose.
			desired, isObject := op.Patch.Body.(map[string]any)
			if !isObject {
				return fmt.Errorf("operation %d: an apply-patch body that passed envelope validation is not an object", i)
			}
			ops, err = diffPatchOps(i, live[i], desired)

		case op.Op == "patch" && op.Patch != nil && op.Patch.Type != mediaJSONPatch:
			ops = mergePatchOps(nil, op.Patch.Body)

		case op.Op == "scale" && op.Scale != nil && op.Scale.Replicas != nil:
			// The one verb whose touched field is fixed by the verb itself. Named rather than
			// diffed: a scale writes the /scale subresource, so a diff of the main object would be
			// a diff of something the operation does not write.
			ops = []classify.PatchOp{{
				Op:    "replace",
				Path:  scaleReplicasPointer,
				Value: int64(*op.Scale.Replicas),
			}}

		default:
			// create, delete, and a JSON Patch. The first two set WholeObject; the third already
			// has its op list.
			continue
		}
		if err != nil {
			return err
		}
		raws[i].Patch = ops
	}
	return nil
}

// diffPatchOps turns an apply's effect into classifier ops: the PATHS come from the diff against
// live state, and the VALUES are read back out of the desired object.
//
// That split is the point, and it is the answer to the objection that this conversion is lossy.
// execute.DiffResult renders its Value as a string, because its other consumer is an ActionRecord
// field with a length bound. The classifier needs the value with its type intact -- the secret scan
// walks structured payloads, and DirectionOfBoolField asks whether a value IS the bool true, which
// the string "true" is not. Neither package can supply both halves. At this seam both inputs are in
// hand at the same moment, so each supplies the half it actually has.
func diffPatchOps(index int, live *unstructured.Unstructured, desired map[string]any) ([]classify.PatchOp, error) {
	res, err := execute.Diff(live, &unstructured.Unstructured{Object: desired})
	if err != nil {
		return nil, fmt.Errorf("operation %d: computing the changed-field set against live state: %w", index, err)
	}
	if res.Truncated {
		// Symmetric with execute.CheckIntegrity, which refuses a truncated diff at the other end of
		// the pipeline for the same reason. A path set that is a PREFIX of the real one understates
		// the change, and a fieldPaths rule that would have fired on the two-hundred-and-first field
		// silently does not. Refusing is the only answer here that is not a quiet loosening.
		return nil, &broker.Refusal{
			Status: http.StatusRequestEntityTooLarge,
			Reason: "change-too-large-to-classify",
			Detail: fmt.Sprintf(
				"operation %d changes %d fields, over the %d the broker can classify and record; classifying a prefix of the change would understate it, so it is refused rather than partially classified",
				index, res.TotalOps, execute.MaxDiffOps),
			Journal: true,
		}
	}

	ops := make([]classify.PatchOp, 0, len(res.Ops))
	for _, d := range res.Ops {
		p := classify.PatchOp{Op: d.Op, Path: d.Path}
		if v, found := valueAtPointer(desired, d.Path); found {
			p.Value = v
		}
		ops = append(ops, p)
	}
	return ops, nil
}

// mergePatchOps walks a merge-patch body down to its leaves.
//
// RFC 7386 semantics decide what a leaf is: a null DELETES the field, a nested object recurses, and
// anything else -- scalar, array, empty object -- replaces wholesale. Rendering a null as `remove`
// rather than as a replace-with-nothing is what lets DirectionOfPatch see a merge patch that strips
// a securityContext as a loosening.
//
// Keys are walked in sorted order so two runs over the same body produce the same op list, which is
// what makes the classification corpus reproducible.
func mergePatchOps(prefix []string, body any) []classify.PatchOp {
	obj, isObject := body.(map[string]any)
	if !isObject || len(obj) == 0 {
		if len(prefix) == 0 {
			// A merge-patch body that is not an object touches nothing nameable. Envelope validation
			// already refused the array case; this is the scalar-at-the-root case.
			return nil
		}
		return []classify.PatchOp{{Op: "replace", Path: classify.JoinPointer(prefix...), Value: body}}
	}

	keys := make([]string, 0, len(obj))
	for k := range obj {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	var out []classify.PatchOp
	for _, k := range keys {
		path := append(append([]string{}, prefix...), k)
		if obj[k] == nil {
			out = append(out, classify.PatchOp{Op: "remove", Path: classify.JoinPointer(path...)})
			continue
		}
		out = append(out, mergePatchOps(path, obj[k])...)
	}
	return out
}

// valueAtPointer reads what the desired object holds at a pointer execute.Diff produced, and reports
// whether it is there at all. A `remove` op's pointer is not, and that is the right answer: nothing
// is being set, so there is no new value to scan or to read a direction from.
//
// Map tokens only. execute.Diff compares a slice as a single value rather than descending into it,
// so every pointer it emits is a chain of map keys. A numeric index arriving here would mean the
// diff changed shape underneath this function, and reporting "not found" is better than guessing at
// an indexing convention the producer no longer uses.
func valueAtPointer(desired map[string]any, ptr string) (any, bool) {
	var cur any = desired
	for _, tok := range classify.SplitPointer(ptr) {
		node, isObject := cur.(map[string]any)
		if !isObject {
			return nil, false
		}
		v, present := node[tok]
		if !present {
			return nil, false
		}
		cur = v
	}
	return cur, true
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

// verifyTargets assembles what step 10 verifies from the three things that know it: the refs, the
// envelope's op at the same index (which says what "success" means for that target), and the
// baselines step 3 captured.
//
// The three are positional with each other because `rawOps` builds refs one per operation, in
// order. The length check is not defensive noise -- a future step that filters targets would
// silently shift every op and every baseline by one, and verifying the wrong row against the wrong
// baseline is the failure this whole function exists to prevent.
func verifyTargets(env *broker.Envelope, refs []agentv1alpha1.TargetRef, baselines []*int64) ([]verify.Target, error) {
	if len(refs) != len(env.Operations) || len(baselines) != len(refs) {
		return nil, fmt.Errorf("internal: %d targets, %d operations and %d baselines must be "+
			"positional with each other", len(refs), len(env.Operations), len(baselines))
	}
	out := make([]verify.Target, 0, len(refs))
	for i, r := range refs {
		out = append(out, verify.Target{
			Ref: r,
			// A delete's success condition is the object's absence. Every other verb's is a live
			// object satisfying its row, which is what the kind table already answers.
			ExpectAbsent:     env.Operations[i].Op == "delete",
			BaselineRestarts: baselines[i],
		})
	}
	return out, nil
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
