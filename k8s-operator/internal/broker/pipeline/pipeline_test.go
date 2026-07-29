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

package pipeline

import (
	"context"
	"errors"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/classify"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/execute"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/undo"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/verify"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/scope"
)

// V-BRK-011 (every mutation traverses the 03 §4.1 pipeline in order) and V-BRK-014 (a fault at any
// step stops the pipeline there) at L1.
//
// Both are asserted against broker.StepTrace, which the pipeline writes as it goes -- see the doc
// block at the top of steps.go for why the trace enforces the order rather than merely reporting
// it. What this file adds is the other half of that argument: a pipeline assembled from real
// component packages, driven end to end, with a fault injected into one dependency at a time.
//
// The fakes below are deliberately dumb. Every one of them answers from a field, so a test that
// wants step k to fail sets exactly one field and asserts the trace ends at k. A fake that
// reimplemented any of the logic it stands in for would make a passing test evidence about the
// fake.

const (
	testNamespace = "kube-agents"
	testAgent     = "platform"
	testProject   = "adamparco-kage"
	testCluster   = "gke-scratch-kube-agents-dev"
	testTenantNS  = "team-a"
)

var testClock = time.Date(2026, 7, 27, 12, 0, 0, 0, time.UTC)

// --- fakes -------------------------------------------------------------------------------------

// fakeLive is classify.LiveState. Every method answers from a field; getErr is the step-3 fault.
type fakeLive struct {
	getErr   error
	labels   map[string]string
	nsLabels map[string]string
	exists   bool
	owner    string
}

func (f *fakeLive) GetObject(context.Context, classify.KindRef, string, string) (map[string]string, map[string]string, bool, error) {
	if f.getErr != nil {
		return nil, nil, false, f.getErr
	}
	return f.labels, nil, f.exists, nil
}

func (f *fakeLive) GetNamespaceLabels(context.Context, string) (map[string]string, bool, error) {
	return f.nsLabels, true, nil
}

func (f *fakeLive) CountWorkloadObjects(context.Context, scope.Scope) (int, error) { return 12, nil }

func (f *fakeLive) SecretDigests(context.Context, scope.Scope) (*classify.DigestSet, error) {
	return &classify.DigestSet{}, nil
}

func (f *fakeLive) LowerTierOwner(context.Context, classify.Caller, classify.KindRef, string, string) (string, error) {
	return f.owner, nil
}

// seenEverything is classify.ActionHistory. It answers "yes, this agent has done this before" so
// the novel-action `+1` of 06 §4.2 does not turn every fixture into a gated action -- which would
// mean no test in this file ever reached step 8.
type seenEverything struct{}

func (seenEverything) Seen(string, string, classify.KindRef, string) bool { return true }

// fakeRefs is undo.ReferenceIndex.
type fakeRefs struct{ err error }

func (f fakeRefs) InboundReferences(context.Context, agentv1alpha1.TargetRef) ([]undo.InboundRef, error) {
	if f.err != nil {
		return nil, f.err
	}
	return nil, nil
}

// fakeReader is execute.Reader. absent makes Get return a NotFound, which is the normal answer for
// a create and is NOT an error to CaptureAll.
type fakeReader struct {
	obj    *unstructured.Unstructured
	absent bool
	err    error
}

func (f *fakeReader) Get(_ context.Context, ref agentv1alpha1.TargetRef) (*unstructured.Unstructured, error) {
	switch {
	case f.err != nil:
		return nil, f.err
	case f.absent:
		return nil, apierrors.NewNotFound(schema.GroupResource{Group: ref.Group, Resource: ref.Kind}, ref.Name)
	}
	return f.obj.DeepCopy(), nil
}

// fakeApplier is execute.Applier. It echoes the object it was given, which is what a server that
// accepts the payload verbatim would return, and counts REAL mutations separately from dry runs --
// the count V-BRK-014 asserts is zero when a step before 9 faults.
type fakeApplier struct {
	result    *unstructured.Unstructured
	dryRunErr error
	applyErr  error

	dryRuns   int
	mutations int
}

func (f *fakeApplier) record(dryRun bool) error {
	if dryRun {
		f.dryRuns++
		return f.dryRunErr
	}
	f.mutations++
	return f.applyErr
}

func (f *fakeApplier) Apply(_ context.Context, obj *unstructured.Unstructured, _ string, dryRun bool) (*unstructured.Unstructured, error) {
	if err := f.record(dryRun); err != nil {
		return nil, err
	}
	if f.result != nil {
		return f.result.DeepCopy(), nil
	}
	return obj.DeepCopy(), nil
}

func (f *fakeApplier) Patch(_ context.Context, _ agentv1alpha1.TargetRef, _ string, _ []byte, _ string, dryRun bool) (*unstructured.Unstructured, error) {
	if err := f.record(dryRun); err != nil {
		return nil, err
	}
	return f.result.DeepCopy(), nil
}

func (f *fakeApplier) Scale(_ context.Context, _ agentv1alpha1.TargetRef, _ int32, _ string, dryRun bool) (*unstructured.Unstructured, error) {
	if err := f.record(dryRun); err != nil {
		return nil, err
	}
	return f.result.DeepCopy(), nil
}

func (f *fakeApplier) Delete(_ context.Context, _ agentv1alpha1.TargetRef, _ execute.DeleteOpts, dryRun bool) error {
	return f.record(dryRun)
}

func (f *fakeApplier) SupportsDryRun(context.Context, agentv1alpha1.TargetRef) bool { return true }

// fakeWriteAhead is execute.Journal -- the V-REV-002 confirmation that the record is durable.
type fakeWriteAhead struct{ err error }

func (f fakeWriteAhead) ConfirmDurable(context.Context, string) error { return f.err }

// fakeProber is verify.Prober. Only Get matters for a ConfigMap (04 §5.1's custom-resource row);
// the rest exist to satisfy the interface and fail loudly if a future predicate reaches for them.
type fakeProber struct {
	obj    *unstructured.Unstructured
	absent bool
}

func (f *fakeProber) Get(_ context.Context, ref agentv1alpha1.TargetRef) (*unstructured.Unstructured, error) {
	if f.absent {
		return nil, apierrors.NewNotFound(schema.GroupResource{Group: ref.Group, Resource: ref.Kind}, ref.Name)
	}
	return f.obj.DeepCopy(), nil
}

func (f *fakeProber) RestartCount(context.Context, agentv1alpha1.TargetRef) (int64, error) {
	return 0, errors.New("fakeProber: RestartCount is not wired for this test")
}

func (f *fakeProber) EndpointCount(context.Context, agentv1alpha1.TargetRef) (int, error) {
	return 0, errors.New("fakeProber: EndpointCount is not wired for this test")
}

func (f *fakeProber) ProgrammedAddress(context.Context, agentv1alpha1.TargetRef) (string, error) {
	return "", errors.New("fakeProber: ProgrammedAddress is not wired for this test")
}

func (f *fakeProber) Connectivity(context.Context, verify.ConnectivityProbe) (bool, error) {
	return false, errors.New("fakeProber: Connectivity is not wired for this test")
}

func (f *fakeProber) AdmissionEnforcing(context.Context, agentv1alpha1.TargetRef) (bool, error) {
	return false, errors.New("fakeProber: AdmissionEnforcing is not wired for this test")
}

func (f *fakeProber) ProviderState(context.Context, agentv1alpha1.TargetRef) (verify.ProviderStatus, error) {
	return verify.ProviderStatus{}, errors.New("fakeProber: ProviderState is not wired for this test")
}

func (f *fakeProber) AccessReview(context.Context, verify.AccessQuery) (bool, error) {
	return false, errors.New("fakeProber: AccessReview is not wired for this test")
}

type fakeRollback struct {
	err   error
	calls int
	// identity is recorded, not ignored: it is what the replayer turns into a field manager, and
	// the pipeline is the only layer that knows which agent the request belongs to. A test that
	// accepted the argument and dropped it would pass just as happily if the pipeline passed "".
	identity string
}

func (f *fakeRollback) Rollback(_ context.Context, _ string, agentIdentity string, _ agentv1alpha1.UndoPlan) error {
	f.calls++
	f.identity = agentIdentity
	return f.err
}

type fakePager struct{ pages int }

func (f *fakePager) Page(context.Context, verify.PageRequest) error { f.pages++; return nil }

type fakePauser struct{ pauses int }

func (f *fakePauser) Pause(context.Context, verify.PauseRequest) error { f.pauses++; return nil }

type fakeCooldown struct{}

func (fakeCooldown) Enter(_ context.Context, _ string, now time.Time) (time.Time, error) {
	return now.Add(time.Hour), nil
}

func (fakeCooldown) Active(context.Context, string, time.Time) (bool, time.Time, error) {
	return false, time.Time{}, nil
}

// fakeRecords is the RecordStore. createErr faults steps 7 and 8; phases records what step 11 wrote.
type fakeRecords struct {
	createErr error
	phaseErr  error

	creates int
	stored  []*agentv1alpha1.ActionRecord
	phases  []agentv1alpha1.ActionPhase
}

func (f *fakeRecords) Create(_ context.Context, ar *agentv1alpha1.ActionRecord) error {
	f.creates++
	if f.createErr != nil {
		return f.createErr
	}
	f.stored = append(f.stored, ar.DeepCopy())
	return nil
}

func (f *fakeRecords) Get(_ context.Context, _, actionID string) (*agentv1alpha1.ActionRecord, error) {
	for _, ar := range f.stored {
		if ar.Spec.ActionID == actionID {
			return ar, nil
		}
	}
	return nil, apierrors.NewNotFound(schema.GroupResource{Resource: "actionrecords"}, actionID)
}

func (f *fakeRecords) SetPhase(_ context.Context, ar *agentv1alpha1.ActionRecord, phase agentv1alpha1.ActionPhase, _ string) error {
	if f.phaseErr != nil {
		return f.phaseErr
	}
	ar.Status.Phase = phase
	f.phases = append(f.phases, phase)
	return nil
}

// fakeBrake is the BrakeSource. The zero-ish view it returns is the one that ALLOWS: an agent that
// reads, a fresh freeze list, a reachable journal. Every fault case perturbs one field of it, which
// is the only way to be sure the perturbation is what refused.
type fakeBrake struct{ view BrakeView }

func (f *fakeBrake) Observe(context.Context) BrakeView { return f.view }

// --- the harness -------------------------------------------------------------------------------

// rig is one assembled pipeline plus every fake it was built from, so a test can both drive it and
// interrogate what the dependencies saw.
type rig struct {
	t *testing.T

	live    *fakeLive
	refs    fakeRefs
	planner Planner
	reader  *fakeReader
	applier *fakeApplier
	wal     fakeWriteAhead
	prober  *fakeProber
	rollup  *fakeRollback
	pager   *fakePager
	pauser  *fakePauser
	records *fakeRecords
	brake   *fakeBrake
	classes *fakeClassifierSource

	pipeline *Pipeline
}

// fakeClassifierSource is a ClassifierSource a test can change between two submissions -- which is
// the only way to assert the property the seam exists for, that a policy applied while the broker
// is running is in force for the next action rather than the next restart.
type fakeClassifierSource struct {
	mu  sync.Mutex
	c   *classify.Classifier
	err error
}

func (f *fakeClassifierSource) Current() (*classify.Classifier, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.err != nil {
		return nil, f.err
	}
	return f.c, nil
}

func (f *fakeClassifierSource) set(c *classify.Classifier, err error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.c, f.err = c, err
}

func mustClassifier(t *testing.T, policies []classify.RuleSet) *classify.Classifier {
	t.Helper()
	c, err := classify.New(policies, seenEverything{})
	if err != nil {
		t.Fatalf("classify.New: %v", err)
	}
	return c
}

func liveConfigMap() *unstructured.Unstructured {
	return &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "v1",
		"kind":       "ConfigMap",
		"metadata": map[string]any{
			"name":            "app-config",
			"namespace":       testTenantNS,
			"uid":             "11111111-2222-3333-4444-555555555555",
			"resourceVersion": "1041",
		},
		"data": map[string]any{"log-level": "info"},
	}}
}

func testAgentCR() *agentv1alpha1.Agent {
	return &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: testAgent, Namespace: testNamespace},
		Spec: agentv1alpha1.AgentSpec{
			Tier: agentv1alpha1.TierPlatform,
			Scope: &agentv1alpha1.ScopeSpec{
				ProjectID:   testProject,
				ClusterName: testCluster,
				Namespace:   testTenantNS,
			},
		},
	}
}

func testIdentity() *broker.Identity {
	return &broker.Identity{
		Username:       "system:serviceaccount:kube-agents:platform-agent-reader",
		Namespace:      testNamespace,
		ServiceAccount: "platform-agent-reader",
		AgentName:      testAgent,
		Tier:           agentv1alpha1.TierPlatform,
		// A single segment, not the rendered project/cluster/namespace path: execute.FieldManager
		// accepts `<tier>` or `<tier>/<scope>` and rejects anything with a second separator. Which
		// of the two the `<scope>` segment is meant to name is a live open question in the ledger;
		// this fixture follows the convention every other broker test already uses.
		Scope: testProject,
	}
}

// createEnvelope is the reference happy-path submission: create one ConfigMap in the agent's own
// namespace. `create` rather than `apply` on purpose -- see TestApplyFailsClosedAtTheIntegrityCheck.
func createEnvelope() *broker.Envelope {
	return &broker.Envelope{
		APIVersion: broker.APIVersion,
		Kind:       broker.EnvelopeKind,
		Intent:     "create the application config map",
		Operations: []broker.Operation{{
			Op:     "create",
			Target: &broker.Target{Version: "v1", Kind: "ConfigMap", Namespace: testTenantNS, Name: "app-config"},
			DesiredState: map[string]any{
				"apiVersion": "v1",
				"kind":       "ConfigMap",
				"metadata":   map[string]any{"name": "app-config", "namespace": testTenantNS},
				"data":       map[string]any{"log-level": "info"},
			},
		}},
		Requester:      broker.Requester{Kind: "agent", ID: testAgent},
		Trigger:        broker.Trigger{Source: "chat"},
		Trace:          broker.Trace{TraceID: "0123456789abcdef0123456789abcdef"},
		IssuedAt:       testClock.Format(time.RFC3339),
		Nonce:          "0123456789abcdef0123456789abcdef",
		IdempotencyKey: "sha256:" + strings.Repeat("a", 64),
	}
}

// newRig assembles a pipeline whose every dependency permits, then applies the tweaks. A test
// perturbs exactly one thing; everything it does not name is the allowing default.
func newRig(t *testing.T, tweaks ...func(*rig)) *rig {
	t.Helper()

	r := &rig{
		t:       t,
		live:    &fakeLive{nsLabels: map[string]string{"env": "dev"}},
		reader:  &fakeReader{absent: true},
		applier: &fakeApplier{},
		prober:  &fakeProber{obj: liveConfigMap()},
		rollup:  &fakeRollback{},
		pager:   &fakePager{},
		pauser:  &fakePauser{},
		records: &fakeRecords{},
		brake: &fakeBrake{view: BrakeView{
			Agent:   testAgentCR(),
			Freezes: &broker.FreezeView{ObservedAt: testClock},
			Journal: broker.BrakeOK,
		}},
		classes: &fakeClassifierSource{c: mustClassifier(t, nil)},
	}
	for _, tw := range tweaks {
		tw(r)
	}

	p, err := New(Config{
		AgentName:           testAgent,
		Namespace:           testNamespace,
		ActorServiceAccount: "platform-agent-actor",
		Classifier:          r.classes,
		Live:                r.live,
		Refs:                r.refs,
		Planner:             r.planner,
		Reader:              r.reader,
		Executor:            &execute.Executor{Applier: r.applier, Journal: r.wal},
		Verifier: &verify.Driver{
			Prober:       r.prober,
			Rollback:     r.rollup,
			Pager:        r.pager,
			Pauser:       r.pauser,
			Cooldown:     fakeCooldown{},
			Now:          func() time.Time { return testClock },
			Sleep:        func(context.Context, time.Duration) error { return nil },
			PollInterval: time.Millisecond,
		},
		Records:   r.records,
		Brake:     r.brake,
		Contested: broker.NewContestedIndex(),
		Now:       func() time.Time { return testClock },
	})
	if err != nil {
		t.Fatalf("pipeline.New: %v", err)
	}
	r.pipeline = p
	return r
}

// submit runs the pipeline with steps 1 and 2 already recorded, exactly as the handler leaves them.
func (r *rig) submit(env *broker.Envelope) (*broker.StepTrace, *broker.Result, error) {
	r.t.Helper()
	tr := &broker.StepTrace{}
	for _, s := range []broker.Step{broker.StepAuthenticate, broker.StepValidate} {
		if err := tr.Run(s, func() (string, error) { return "ok", nil }); err != nil {
			r.t.Fatalf("seeding the trace at %s: %v", s, err)
		}
	}
	res, err := r.pipeline.Submit(context.Background(), testIdentity(), env, tr)
	return tr, res, err
}

// --- V-BRK-011: the whole sequence, in order -----------------------------------------------------

func TestPipelineTraversesEveryStepInOrder(t *testing.T) {
	r := newRig(t)
	tr, res, err := r.submit(createEnvelope())
	if err != nil {
		t.Fatalf("submit: %v\ntrace: %s", err, tr)
	}
	if res == nil || res.Decision != "accepted" {
		t.Fatalf("decision = %+v, want accepted\ntrace: %s", res, tr)
	}

	// Every step, and no gaps. Bounded by FirstStep/LastStep so a twelfth step added to the pipeline
	// is covered by this assertion the day it exists, instead of silently falling outside a
	// hardcoded 1..11 (LSN-036).
	for s := broker.FirstStep; s <= broker.LastStep; s++ {
		if !tr.Ran(s) {
			t.Errorf("step %s did not run\ntrace: %s", s, tr)
		}
	}
	if got := tr.Reached(); got != broker.LastStep {
		t.Errorf("reached %s, want %s\ntrace: %s", got, broker.LastStep, tr)
	}

	// Order, read off the trace rather than assumed from the loop above: Ran() is a set membership
	// test and would pass on a trace that recorded the steps in any sequence. StepTrace.Run refuses
	// out-of-order records, so this is a check on the check.
	events := tr.Events()
	if len(events) != int(broker.LastStep) {
		t.Fatalf("got %d events, want %d\ntrace: %s", len(events), broker.LastStep, tr)
	}
	for i, e := range events {
		if want := broker.Step(i + 1); e.Step != want {
			t.Errorf("event %d is %s, want %s\ntrace: %s", i, e.Step, want, tr)
		}
	}

	// The three orderings 03 §4.1 is actually about: classify before gate, gate before snapshot,
	// snapshot before execute. A pipeline that executed and then classified would satisfy "every
	// step ran".
	assertBefore(t, tr, broker.StepClassify, broker.StepGate)
	assertBefore(t, tr, broker.StepGate, broker.StepSnapshot)
	assertBefore(t, tr, broker.StepSnapshot, broker.StepExecute)
	assertBefore(t, tr, broker.StepExecute, broker.StepVerify)
	assertBefore(t, tr, broker.StepVerify, broker.StepJournal)

	// And the effects, not just the trace: one real mutation, a durable record before it, and a
	// terminal phase after it.
	if r.applier.mutations != 1 {
		t.Errorf("applier saw %d mutations, want 1", r.applier.mutations)
	}
	if r.records.creates != 1 {
		t.Errorf("record store saw %d creates, want 1", r.records.creates)
	}
	if len(r.records.phases) != 1 || r.records.phases[0] != agentv1alpha1.PhaseVerified {
		t.Errorf("terminal phases = %v, want [%s]", r.records.phases, agentv1alpha1.PhaseVerified)
	}
	if len(r.records.stored) != 1 {
		t.Fatalf("nothing was stored")
	}
	ar := r.records.stored[0]
	if ar.Spec.Undo == nil || ar.Spec.Undo.Strategy == agentv1alpha1.UndoNone {
		t.Errorf("the record carries no usable undo plan: %+v", ar.Spec.Undo)
	}
	if ar.Spec.ActorServiceAccount != "platform-agent-actor" {
		t.Errorf("actor = %q, want platform-agent-actor", ar.Spec.ActorServiceAccount)
	}
	if ar.Spec.Classification.Class != agentv1alpha1.RiskRoutine {
		t.Errorf("class = %q, want routine (reasons: %+v)", ar.Spec.Classification.Class, ar.Spec.Classification.Reasons)
	}
}

func TestDryRunSkipsVerificationAndMutatesNothing(t *testing.T) {
	env := createEnvelope()
	env.DryRun = true

	r := newRig(t)
	tr, res, err := r.submit(env)
	if err != nil {
		t.Fatalf("submit: %v\ntrace: %s", err, tr)
	}
	if res.Phase != string(agentv1alpha1.PhaseDryRun) {
		t.Errorf("phase = %q, want %q", res.Phase, agentv1alpha1.PhaseDryRun)
	}
	if r.applier.mutations != 0 {
		t.Errorf("a dry run issued %d real mutations", r.applier.mutations)
	}
	// Skipped, not omitted: the trace still reaches step 11, and step 10 says why it did nothing.
	if got := tr.Reached(); got != broker.LastStep {
		t.Errorf("reached %s, want %s\ntrace: %s", got, broker.LastStep, tr)
	}
	if tr.Ran(broker.StepVerify) {
		t.Errorf("step 10 is recorded as having run on a dry run\ntrace: %s", tr)
	}
	if ev := eventFor(tr, broker.StepVerify); ev.Status != broker.StepSkipped || ev.Detail == "" {
		t.Errorf("step 10 = %s, want a skip with a reason\ntrace: %s", ev, tr)
	}
}

func TestGatedActionParksAtStepSevenAndNothingBelowRuns(t *testing.T) {
	env := createEnvelope()
	env.RequireApproval = true

	r := newRig(t)
	tr, res, err := r.submit(env)
	if err != nil {
		t.Fatalf("submit: %v\ntrace: %s", err, tr)
	}
	if res.Decision != "gated" {
		t.Fatalf("decision = %q, want gated\ntrace: %s", res.Decision, tr)
	}
	if got := tr.Reached(); got != broker.StepGate {
		t.Fatalf("reached %s, want %s\ntrace: %s", got, broker.StepGate, tr)
	}
	assertNoStepAfter(t, tr, broker.StepGate)

	if r.applier.mutations != 0 || r.applier.dryRuns != 0 {
		t.Errorf("a parked action reached the applier: %d dry runs, %d mutations", r.applier.dryRuns, r.applier.mutations)
	}
	if len(r.records.stored) != 1 || r.records.stored[0].Status.Phase != agentv1alpha1.PhasePendingApproval {
		t.Fatalf("the parked record is %+v, want one PendingApproval", r.records.stored)
	}
	// A parked record carries no pre-state: it was captured at step 3, and by the time a human
	// approves it describes a cluster that has moved on.
	if len(r.records.stored[0].Spec.PreState) != 0 {
		t.Errorf("the parked record carries %d pre-states", len(r.records.stored[0].Spec.PreState))
	}
}

// --- V-BRK-014: a fault at step k stops the pipeline at step k ------------------------------------

var errInjected = errors.New("injected fault")

// faultCase is one step's fault: the dependency to break, and what breaking it means.
type faultCase struct {
	step  broker.Step
	what  string
	tweak func(*rig)
	env   func(*broker.Envelope)
}

// faultCases is the V-BRK-014 table. A package-level function rather than a local so that
// TestEveryPipelineStepHasAFaultCase can assert its coverage against the step range instead of
// against a second, hand-maintained list of the same steps.
func faultCases() []faultCase {
	boom := errInjected
	return []faultCase{
		{
			step:  broker.StepResolveScope,
			what:  "the live-state read fails, so nothing can be classified",
			tweak: func(r *rig) { r.live.getErr = boom },
		},
		{
			step: broker.StepClassify,
			what: "the undo planner errors, so invertibility is unknown",
			tweak: func(r *rig) {
				r.planner = PlannerFunc(func(context.Context, undo.Request, undo.ReferenceIndex) (*undo.Result, error) { return nil, boom })
			},
		},
		{
			step: broker.StepBrake,
			what: "the journal is unreachable (06 §4.4 row 3)",
			tweak: func(r *rig) {
				r.brake.view.Journal = broker.BrakeFailed
			},
		},
		{
			step: broker.StepUndoPlan,
			what: "the planner returned no plan object at all",
			tweak: func(r *rig) {
				r.planner = PlannerFunc(func(context.Context, undo.Request, undo.ReferenceIndex) (*undo.Result, error) {
					return &undo.Result{Refusals: []string{"the planner produced nothing"}}, nil
				})
			},
		},
		{
			step:  broker.StepGate,
			what:  "the parked record cannot be written, so nobody could approve it",
			tweak: func(r *rig) { r.records.createErr = boom },
			env:   func(e *broker.Envelope) { e.RequireApproval = true },
		},
		{
			step:  broker.StepSnapshot,
			what:  "the pre-state record is not durable (06 §4.4 row 4)",
			tweak: func(r *rig) { r.records.createErr = boom },
		},
		{
			step:  broker.StepExecute,
			what:  "the dry-run pass is refused, before any mutation",
			tweak: func(r *rig) { r.applier.dryRunErr = boom },
		},
		{
			step: broker.StepVerify,
			what: "verification fails and the rollback fails too (06 §4.4 row 9)",
			tweak: func(r *rig) {
				r.prober.absent = true
				r.rollup.err = boom
			},
		},
	}
}

func TestFaultAtAnyStepStopsThePipelineThere(t *testing.T) {
	for _, tc := range faultCases() {
		t.Run(tc.step.String(), func(t *testing.T) {
			r := newRig(t, tc.tweak)
			env := createEnvelope()
			if tc.env != nil {
				tc.env(env)
			}

			tr, res, err := r.submit(env)
			if err == nil {
				t.Fatalf("%s: submit succeeded (%+v)\ntrace: %s", tc.what, res, tr)
			}

			if got := tr.Reached(); got != tc.step {
				t.Fatalf("%s: reached %s, want %s\ntrace: %s", tc.what, got, tc.step, tr)
			}
			last := eventFor(tr, tc.step)
			if last.Status != broker.StepFailed && last.Status != broker.StepRefused {
				t.Errorf("%s: step %s ended %q, want failed or refused\ntrace: %s", tc.what, tc.step, last.Status, tr)
			}
			assertNoStepAfter(t, tr, tc.step)

			// The half that matters. A trace that stops is only evidence if the world stopped with
			// it: no real mutation was issued for any fault before step 9, and no record ever
			// reached a phase that claims the action completed.
			if tc.step < broker.StepExecute && r.applier.mutations != 0 {
				t.Errorf("%s: %d mutations were issued after a fault at %s", tc.what, r.applier.mutations, tc.step)
			}
			if tc.step < broker.StepExecute && r.applier.dryRuns != 0 {
				t.Errorf("%s: the applier was reached at all after a fault at %s", tc.what, tc.step)
			}
			for _, ph := range r.records.phases {
				if ph == agentv1alpha1.PhaseVerified {
					t.Errorf("%s: a record reached %s despite the fault", tc.what, ph)
				}
			}
		})
	}
}

// TestEveryPipelineStepHasAFaultCase is the anti-headcount check (LSN-036).
//
// V-BRK-014 says "at any step". A table that happens to cover eight of the nine steps the pipeline
// owns is a check whose coverage silently drops the day a step is added -- and the step most likely
// to be added is the one nobody wrote a fault for. Steps 1 and 2 belong to the handler and are
// covered by TestHandlerTraceStopsAtTheFaultedStep in the broker package; step 11 is the terminal
// write, whose fault case is TestJournalFailureIsNotSuccess below.
func TestEveryPipelineStepHasAFaultCase(t *testing.T) {
	covered := map[broker.Step]bool{
		broker.StepAuthenticate: true, // broker.TestHandlerTraceStopsAtTheFaultedStep
		broker.StepValidate:     true, // broker.TestHandlerTraceStopsAtTheFaultedStep
		broker.StepJournal:      true, // TestJournalFailureIsNotSuccess
	}
	// faultCases() is the single source of truth for what is covered; re-listing its steps here
	// would be the second list that goes stale.
	for _, tc := range faultCases() {
		covered[tc.step] = true
	}
	for s := broker.FirstStep; s <= broker.LastStep; s++ {
		if !covered[s] {
			t.Errorf("step %s has no fault-injection case; V-BRK-014 is 'a fault at ANY step'", s)
		}
	}
}

func TestJournalFailureIsNotSuccess(t *testing.T) {
	r := newRig(t, func(r *rig) { r.records.phaseErr = errors.New("injected fault") })
	tr, res, err := r.submit(createEnvelope())
	if err == nil {
		t.Fatalf("submit succeeded with an unwritable journal (%+v)\ntrace: %s", res, tr)
	}
	if got := tr.Reached(); got != broker.StepJournal {
		t.Fatalf("reached %s, want %s\ntrace: %s", got, broker.StepJournal, tr)
	}
	if eventFor(tr, broker.StepJournal).Status != broker.StepFailed {
		t.Errorf("step 11 did not record a failure\ntrace: %s", tr)
	}
}

// TestApplyFailsClosedAtTheIntegrityCheck records a real gap this assembly uncovered.
//
// classify.ResolvedOp.WholeObject is set for create, apply AND delete, because to a rule "every
// field is touched" is true of all three. execute.Classified.WholeObject means something narrower
// -- create and delete only -- and execute.CheckIntegrity deliberately refuses any other verb that
// arrives with it set (execute.TestIntegrityWholeObjectIsNotAnEscapeHatch asserts exactly that).
// The two packages were each right and had never been connected, which is the LSN-007 shape.
//
// The consequence is that an `apply` cannot execute through this pipeline. That is FAIL-CLOSED, so
// it is missing functionality rather than a hole: the action is refused at step 9 and nothing is
// mutated. Closing it means giving the classifier the computed pre-state->desired diff for an
// apply (classify.RawOp.Patch is documented as carrying exactly that) and the same paths to the
// integrity check -- which is P9-T7c-4, not this unit.
//
// This test exists so the gap is a recorded property rather than a surprise. When T7c-4 lands it
// should be replaced by its positive counterpart, not deleted.
func TestApplyFailsClosedAtTheIntegrityCheck(t *testing.T) {
	env := createEnvelope()
	env.Operations[0].Op = "apply"

	r := newRig(t, func(r *rig) { r.reader = &fakeReader{obj: liveConfigMap()} })
	tr, res, err := r.submit(env)
	if err == nil {
		t.Fatalf("an apply executed (%+v); if P9-T7c-4 landed, replace this test\ntrace: %s", res, tr)
	}
	if got := tr.Reached(); got != broker.StepExecute {
		t.Fatalf("reached %s, want %s\ntrace: %s", got, broker.StepExecute, tr)
	}
	if r.applier.mutations != 0 {
		t.Errorf("the refused apply mutated %d objects", r.applier.mutations)
	}
}

// --- assertions ----------------------------------------------------------------------------------

func eventFor(tr *broker.StepTrace, s broker.Step) broker.StepEvent {
	for _, e := range tr.Events() {
		if e.Step == s {
			return e
		}
	}
	return broker.StepEvent{}
}

// precedes is the V-BRK-011 ordering predicate, kept separate from the assertion that reports it so
// that the negative control can call it directly. An assertion that only exists as a t.Errorf can
// only be shown to pass; it cannot be shown to be capable of failing.
func precedes(tr *broker.StepTrace, first, second broker.Step) bool {
	fi, si := -1, -1
	for i, e := range tr.Events() {
		switch e.Step {
		case first:
			fi = i
		case second:
			si = i
		}
	}
	return fi >= 0 && si >= 0 && fi < si
}

func assertBefore(t *testing.T, tr *broker.StepTrace, first, second broker.Step) {
	t.Helper()
	if !precedes(tr, first, second) {
		t.Errorf("%s must precede %s\ntrace: %s", first, second, tr)
	}
}

// stepsAfter is the "and nothing after" predicate of V-BRK-014: every step past `last` that the
// trace mentions. Bounded by LastStep rather than by a list of the steps that exist today, so a
// twelfth step is in range the day it is defined (LSN-036).
func stepsAfter(tr *broker.StepTrace, last broker.Step) []broker.Step {
	rendered := tr.String()
	var found []broker.Step
	for s := last + 1; s <= broker.LastStep; s++ {
		if strings.Contains(rendered, s.String()) {
			found = append(found, s)
		}
	}
	return found
}

func assertNoStepAfter(t *testing.T, tr *broker.StepTrace, last broker.Step) {
	t.Helper()
	if extra := stepsAfter(tr, last); len(extra) > 0 {
		t.Errorf("steps %v appear in a trace that should have stopped at %s\ntrace: %s", extra, last, tr)
	}
}

// --- V-GAT-009: the policy set is resolved per submission, and fails closed ----------------------

// gateCreates is a ChangePolicy that raises every create to gated, converted through the same
// FromChangePolicy the loader uses -- so this test is exercising the CRD path and not a rule table
// hand-written in the shape the classifier happens to want.
func gateCreates(t *testing.T, name string) classify.RuleSet {
	t.Helper()
	rs, err := classify.FromChangePolicy(&agentv1alpha1.ChangePolicy{
		ObjectMeta: metav1.ObjectMeta{Name: name},
		Spec: agentv1alpha1.ChangePolicySpec{Rules: []agentv1alpha1.ChangeRule{{
			ID:     "gate-creates-while-ramping",
			When:   agentv1alpha1.ChangeRuleWhen{Verbs: []agentv1alpha1.ChangeVerb{"create"}},
			Class:  agentv1alpha1.ChangePolicyClassGated,
			Reason: "trust-building period: creates are reviewed",
		}}},
	})
	if err != nil {
		t.Fatalf("FromChangePolicy: %v", err)
	}
	return rs
}

// TestAPolicyAppliedBetweenTwoSubmissionsBindsTheSecond is the property the ClassifierSource seam
// exists for. Before it, Config held a fixed *classify.Classifier built at startup, so a
// ChangePolicy an operator applied took effect on the next broker restart -- and the restart would
// happen some time after the action the policy was written to stop.
func TestAPolicyAppliedBetweenTwoSubmissionsBindsTheSecond(t *testing.T) {
	r := newRig(t)

	_, first, err := r.submit(createEnvelope())
	if err != nil {
		t.Fatalf("first submit: %v", err)
	}
	if first.Decision == "gated" {
		t.Fatalf("the baseline create is already gated, so this test could not detect the policy taking effect")
	}

	r.classes.set(mustClassifier(t, []classify.RuleSet{gateCreates(t, "ramp-up")}), nil)

	tr, second, err := r.submit(createEnvelope())
	if err != nil {
		t.Fatalf("second submit: %v\ntrace: %s", err, tr)
	}
	if second.Decision != "gated" {
		t.Fatalf("decision = %q, want gated: a policy applied while the broker is running must bind the next action\ntrace: %s",
			second.Decision, tr)
	}
	if got := tr.Reached(); got != broker.StepGate {
		t.Fatalf("reached %s, want %s\ntrace: %s", got, broker.StepGate, tr)
	}
}

// TestAnUnknownPolicySetRefusesRatherThanFallingBackToTheFloor.
//
// The tempting failure is the quiet one: the code floor is always available, so a broker that
// cannot read its ChangePolicy objects could carry on classifying against the floor alone and look
// completely healthy. It must not. The floor alone is a strictly WEAKER rule table than the floor
// plus policies -- the classifier maxes over sources -- so that fallback is a silent downgrade of
// every policy in the cluster, arriving at exactly the moment the cluster is unhealthy.
func TestAnUnknownPolicySetRefusesRatherThanFallingBackToTheFloor(t *testing.T) {
	r := newRig(t)
	r.classes.set(nil, errors.New("the ChangePolicy set was last read 45s ago, past the 30s staleness limit"))

	tr, _, err := r.submit(createEnvelope())
	if err == nil {
		t.Fatalf("submit succeeded with an unreadable policy set\ntrace: %s", tr)
	}
	var ref *broker.Refusal
	if !errors.As(err, &ref) {
		t.Fatalf("err = %v (%T), want a *broker.Refusal", err, err)
	}
	if ref.Reason != broker.ReasonPolicyUnavailable {
		t.Errorf("reason = %q, want %q", ref.Reason, broker.ReasonPolicyUnavailable)
	}
	if ref.Status != http.StatusServiceUnavailable {
		t.Errorf("status = %d, want %d", ref.Status, http.StatusServiceUnavailable)
	}
	if !ref.Journal {
		t.Error("a policy-unavailable refusal must be journaled; a refusal nobody records is a refusal nobody investigates")
	}
	if !ref.SecurityEvent {
		t.Error("a broker that cannot see its policies is a control-plane condition an operator has to be told about")
	}
	if ref.RetryAfterSeconds <= 0 {
		t.Error("the refusal is temporary and must say so, or every caller retries immediately and hammers a struggling API server")
	}
	if !strings.Contains(ref.Detail, "staleness limit") {
		t.Errorf("the detail must carry the source's own diagnosis, got %q", ref.Detail)
	}
	// The pipeline stops at step 4, before the brake and before any pre-state is written.
	if got := tr.Reached(); got != broker.StepClassify {
		t.Fatalf("reached %s, want %s\ntrace: %s", got, broker.StepClassify, tr)
	}
	assertNoStepAfter(t, tr, broker.StepClassify)
	if r.applier.mutations != 0 || r.applier.dryRuns != 0 {
		t.Errorf("an unclassified action reached the applier: %d dry runs, %d mutations", r.applier.dryRuns, r.applier.mutations)
	}
}

func TestNewRejectsAMissingClassifierSource(t *testing.T) {
	if _, err := New(Config{AgentName: testAgent, Namespace: testNamespace}); err == nil {
		t.Fatal("New must reject a Config with no ClassifierSource")
	} else if !strings.Contains(err.Error(), "ClassifierSource") {
		t.Fatalf("the error must name what is missing: %v", err)
	}
	if _, err := (StaticClassifier{}).Current(); err == nil {
		t.Fatal("an empty StaticClassifier must return an error rather than a nil classifier")
	}
	c := mustClassifier(t, nil)
	got, err := StaticClassifier{C: c}.Current()
	if err != nil || got != c {
		t.Fatalf("StaticClassifier.Current = (%v, %v), want the classifier it holds", got, err)
	}
}

// --- negative controls (09 §6: V-BRK-014 and V-GAT-009 are marked ¬, so this is mandatory) --------

// TestTheseAssertionsCanFail is the negative control for both checks in this file.
//
// Everything above asserts that a correct pipeline produces a correct trace. None of it establishes
// that an INCORRECT trace would be rejected -- and a predicate that cannot fail turns the whole file
// into a suite of vacuous passes (09 §6, LSN-035). So each predicate is fed the shape it is supposed
// to catch, and is required to catch it.
func TestTheseAssertionsCanFail(t *testing.T) {
	// A full, correct trace. Built by running the real pipeline rather than by hand, so the control
	// is exercising the same objects the checks are.
	full := newRig(t)
	complete, _, err := full.submit(createEnvelope())
	if err != nil {
		t.Fatalf("building the reference trace: %v", err)
	}

	// 1. "nothing after step k" must fire when there IS something after step k.
	if extra := stepsAfter(complete, broker.StepBrake); len(extra) == 0 {
		t.Error("stepsAfter found nothing past step 5 in a trace that runs to step 11; the " +
			"V-BRK-014 'and nothing after' assertion cannot fail and therefore proves nothing")
	}

	// 2. The ordering predicate must fire on the reverse of a true ordering.
	if precedes(complete, broker.StepExecute, broker.StepClassify) {
		t.Error("precedes() says execute comes before classify; the V-BRK-011 ordering assertion is inverted")
	}

	// 3. "every step ran" must fire on a trace that stopped early. A fault at step 5 leaves steps
	//    6..11 un-run, and Ran() has to say so -- including for the refused step itself, which
	//    reached the pipeline but did not complete.
	stopped := newRig(t, func(r *rig) { r.brake.view.Journal = broker.BrakeFailed })
	partial, _, err := stopped.submit(createEnvelope())
	if err == nil {
		t.Fatal("the reference fault did not stop the pipeline")
	}
	for s := broker.StepBrake; s <= broker.LastStep; s++ {
		if partial.Ran(s) {
			t.Errorf("Ran(%s) is true on a trace that faulted at step 5; the V-BRK-011 "+
				"per-step assertion would pass on a pipeline that skipped six steps\ntrace: %s", s, partial)
		}
	}

	// 4. V-GAT-009's control. "The second submission is gated" proves a policy took effect only if
	//    a broker with no such policy does NOT gate the same envelope. A change that gated every
	//    action -- an inverted comparison in Max, a floor rule widened by accident -- would make
	//    TestAPolicyAppliedBetweenTwoSubmissionsBindsTheSecond pass while proving nothing about
	//    ChangePolicy at all. Both directions are asserted here, from one place, over one envelope.
	unpoliced := newRig(t)
	_, without, err := unpoliced.submit(createEnvelope())
	if err != nil {
		t.Fatalf("the unpoliced reference submission failed: %v", err)
	}
	policed := newRig(t, func(r *rig) {
		r.classes.set(mustClassifier(t, []classify.RuleSet{gateCreates(t, "ramp-up")}), nil)
	})
	_, with, err := policed.submit(createEnvelope())
	if err != nil {
		t.Fatalf("the policed reference submission failed: %v", err)
	}
	if without.Decision == with.Decision {
		t.Errorf("the same envelope decides %q both with and without a gating ChangePolicy; the "+
			"V-GAT-009 assertion cannot distinguish a policy taking effect from one being ignored",
			with.Decision)
	}

	// 5. The policy-unavailable control. A healthy source must not produce the refusal the
	//    fail-closed test looks for, or that test would pass on a broker that refused everything.
	var ref *broker.Refusal
	if errors.As(err, &ref) && ref.Reason == broker.ReasonPolicyUnavailable {
		t.Error("a healthy ClassifierSource produced a policy-unavailable refusal; the fail-closed " +
			"assertion would pass on a broker that never classifies anything")
	}
}
