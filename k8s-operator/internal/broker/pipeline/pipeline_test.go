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

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
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

// fakeDryRunner is undo.DryRunner: 06 §4.3.1's plan-time "would this step apply".
//
// Permissive by default, because a validated plan is the ordinary case and the alternative would
// make every unrelated test in this file assert its way past a gate. `err` makes the whole plan
// downgrade; `identities` records the field-manager key the pipeline asked for, which is the one
// thing about this seam a caller can get wrong invisibly.
type fakeDryRunner struct {
	err        error
	steps      []agentv1alpha1.UndoStep
	identities []string
}

func (f *fakeDryRunner) DryRun(_ context.Context, step agentv1alpha1.UndoStep) error {
	f.steps = append(f.steps, step)
	return f.err
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

	// restarts answers the workload row's "no new restarts" half. Zero is a real answer, not an
	// unset one: a Deployment whose pods have never restarted reports 0, and the predicate compares
	// it against a baseline rather than against zero.
	restarts int64

	// restartErr fails the baseline read itself, as distinct from `absent`.
	restartErr error
}

func (f *fakeProber) Get(_ context.Context, ref agentv1alpha1.TargetRef) (*unstructured.Unstructured, error) {
	if f.absent {
		return nil, apierrors.NewNotFound(schema.GroupResource{Group: ref.Group, Resource: ref.Kind}, ref.Name)
	}
	return f.obj.DeepCopy(), nil
}

func (f *fakeProber) RestartCount(_ context.Context, ref agentv1alpha1.TargetRef) (int64, error) {
	// Injected before the absent check so a test can ask what a FAILED baseline read does, which is
	// a different question from what an absent workload does. V-BRK-031 needs both.
	if f.restartErr != nil {
		return 0, f.restartErr
	}
	// NotFound for an absent object, like probe.Source, which reads the workload to find its pod
	// selector before it can count anything. verify.CaptureRestartBaselines depends on the
	// difference: NotFound is "nothing was running, baseline zero", any other error is a refusal.
	if f.absent {
		return 0, apierrors.NewNotFound(schema.GroupResource{Group: ref.Group, Resource: ref.Kind}, ref.Name)
	}
	return f.restarts, nil
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

func (fakeCooldown) Enter(_ context.Context, _, _ string, now time.Time) (time.Time, error) {
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

	// finalStatus is the record's status as step 11 handed it to the store. See SetPhase.
	finalStatus *agentv1alpha1.ActionRecordStatus

	// dropStatusOnCreate models the API server without the store's restore. See Create.
	dropStatusOnCreate bool
}

func (f *fakeRecords) Create(_ context.Context, ar *agentv1alpha1.ActionRecord) error {
	f.creates++
	if f.createErr != nil {
		return f.createErr
	}
	f.stored = append(f.stored, ar.DeepCopy())
	// `status` is a SUBRESOURCE: the API server drops the block from the object the broker POSTs and
	// the reply overwrites the caller's copy. `journal.Store.Create` restores the broker-owned fields
	// with a follow-up status write, so by default this fake leaves `ar.Status` alone -- that is the
	// store's contract as it stands. `dropStatusOnCreate` models the raw server WITHOUT the restore,
	// which is the tree as it was: step 8 dereferenced the nil `status.timestamps` and took the
	// broker down after the record was durable and before the executor ran.
	if f.dropStatusOnCreate {
		ar.Status = agentv1alpha1.ActionRecordStatus{Phase: ar.Status.Phase}
	}
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
	// The TERMINAL snapshot, taken here rather than at Create, because most of the status the real
	// store persists does not exist yet when the record is born: `applied` lands at step 9,
	// `verification` and `recovery` at step 11, and four of the six lifecycle timestamps after the
	// Create. `stored` is a Create-time DeepCopy and therefore cannot answer anything about them.
	f.finalStatus = ar.Status.DeepCopy()
	return nil
}

func (f *fakeRecords) UpdateForResume(_ context.Context, ar *agentv1alpha1.ActionRecord, preState []agentv1alpha1.PreStateSnapshot, undo *agentv1alpha1.UndoPlan) error {
	ar.Spec.PreState = preState
	ar.Spec.Undo = undo
	for i, stored := range f.stored {
		if stored.Spec.ActionID == ar.Spec.ActionID {
			f.stored[i] = ar.DeepCopy()
			break
		}
	}
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
	// dryRunner is the plan-time DryRunner. Permissive by default -- every step would apply -- so
	// that a test which does not care about undo validation gets a validated plan and the step 6
	// guard stays out of its way. A test that DOES care sets errs.
	dryRunner *fakeDryRunner
	reader    *fakeReader
	applier   *fakeApplier
	wal       fakeWriteAhead
	prober    *fakeProber
	rollup    *fakeRollback
	pager     *fakePager
	pauser    *fakePauser
	records   *fakeRecords
	brake     *fakeBrake
	budget    *solventLedger
	classes   *fakeClassifierSource

	// verifyClock is the settle window's clock, and it ADVANCES -- see newRig. The pipeline's own
	// clock stays pinned at testClock so action timestamps remain golden.
	verifyClock time.Time

	// pipelineClock is what Config.Now returns, and pipelineTick is how far it moves per call.
	// The tick DEFAULTS TO ZERO, which is the pinned clock every existing test was written
	// against -- a fixed instant is what keeps the record's timestamps golden. A test that needs
	// to observe the ORDER of two stamps sets it with withAdvancingClock.
	pipelineClock time.Time
	pipelineTick  time.Duration

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

// applyEnvelope applies liveConfigMap's shape with `data.log-level` set to v. Paired with a
// fakeReader over liveConfigMap it is a one-field change, which is what makes it useful: the field
// set the classifier is shown must be that one field and not the whole object.
func applyEnvelope(v string) *broker.Envelope {
	return applyEnvelopeSetting("log-level", v)
}

// applyEnvelopeSetting is applyEnvelope with the changed key as a parameter, for asserting that a
// fieldPaths rule does NOT fire on a change to a field it does not name.
func applyEnvelopeSetting(key, v string) *broker.Envelope {
	env := createEnvelope()
	env.Intent = "apply the application config map"
	env.Operations[0].Op = "apply"
	env.Operations[0].DesiredState = map[string]any{
		"apiVersion": "v1",
		"kind":       "ConfigMap",
		"metadata":   map[string]any{"name": "app-config", "namespace": testTenantNS},
		"data":       map[string]any{"log-level": "info", key: v},
	}
	return env
}

// gateFieldPath is gateCreates' field-level sibling: gate any change touching a named dotted path,
// whatever the verb. No `verbs` clause on purpose -- the rule is about the field, and constraining
// it to `apply` would make the test agree with the implementation about which verbs carry paths.
func gateFieldPath(t *testing.T, name, dotted string) classify.RuleSet {
	t.Helper()
	rs, err := classify.FromChangePolicy(&agentv1alpha1.ChangePolicy{
		ObjectMeta: metav1.ObjectMeta{Name: name},
		Spec: agentv1alpha1.ChangePolicySpec{Rules: []agentv1alpha1.ChangeRule{{
			ID:     "review-" + strings.ReplaceAll(dotted, ".", "-"),
			When:   agentv1alpha1.ChangeRuleWhen{FieldPaths: []string{dotted}},
			Class:  agentv1alpha1.ChangePolicyClassGated,
			Reason: "changes to " + dotted + " are reviewed",
		}}},
	})
	if err != nil {
		t.Fatalf("FromChangePolicy: %v", err)
	}
	return rs
}

// createEnvelope is the reference happy-path submission: create one ConfigMap in the agent's own
// namespace. `create` rather than `apply` on purpose: it is the whole-object verb, so it exercises
// execute.checkWholeObject rather than the path comparison applyEnvelope drives.
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

// deploymentEnvelope is createEnvelope over an apps/v1 Deployment, for the one property that a
// ConfigMap cannot exercise: verify.NeedsRestartBaseline is keyed by kind, and only the workload
// kinds get a pre-action restart read at step 3. A test that wants to fault that read has to submit
// a target whose row actually needs it, or the prober is never called at all.
func deploymentEnvelope() *broker.Envelope {
	env := createEnvelope()
	env.Intent = "create the application deployment"
	env.Operations[0].Target = &broker.Target{
		Group: "apps", Version: "v1", Kind: "Deployment", Namespace: testTenantNS, Name: "app",
	}
	env.Operations[0].DesiredState = map[string]any{
		"apiVersion": "apps/v1",
		"kind":       "Deployment",
		"metadata":   map[string]any{"name": "app", "namespace": testTenantNS},
		"spec":       map[string]any{"replicas": int64(1)},
	}
	return env
}

// newRig assembles a pipeline whose every dependency permits, then applies the tweaks. A test
// perturbs exactly one thing; everything it does not name is the allowing default.
func newRig(t *testing.T, tweaks ...func(*rig)) *rig {
	t.Helper()

	r := &rig{
		t:             t,
		verifyClock:   testClock,
		pipelineClock: testClock,
		live:          &fakeLive{nsLabels: map[string]string{"env": "dev"}},
		reader:        &fakeReader{absent: true},
		applier:       &fakeApplier{},
		prober:        &fakeProber{obj: liveConfigMap()},
		rollup:        &fakeRollback{},
		pager:         &fakePager{},
		pauser:        &fakePauser{},
		records:       &fakeRecords{},
		brake: &fakeBrake{view: BrakeView{
			Agent:   testAgentCR(),
			Freezes: &broker.FreezeView{ObservedAt: testClock},
			Journal: broker.BrakeOK,
		}},
		budget:    &solventLedger{},
		classes:   &fakeClassifierSource{c: mustClassifier(t, nil)},
		dryRunner: &fakeDryRunner{},
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
		DryRunner: func(id string) undo.DryRunner {
			r.dryRunner.identities = append(r.dryRunner.identities, id)
			return r.dryRunner
		},
		Reader:   r.reader,
		Executor: &execute.Executor{Applier: r.applier, Journal: r.wal},
		Verifier: &verify.Driver{
			Prober:   r.prober,
			Rollback: r.rollup,
			Pager:    r.pager,
			Pauser:   r.pauser,
			Cooldown: fakeCooldown{},
			// The settle window's clock advances by whatever the driver sleeps. A fixed clock plus
			// a no-op sleep is not a fast test, it is an infinite one: verifyOne polls a Pending
			// predicate until the deadline, and a deadline that never arrives hangs the whole
			// package instead of failing one case. Nothing sleeps for real -- the advance is the
			// sleep -- so this stays as fast as the stub it replaces.
			Now: func() time.Time { return r.verifyClock },
			Sleep: func(_ context.Context, d time.Duration) error {
				r.verifyClock = r.verifyClock.Add(d)
				return nil
			},
			PollInterval: 15 * time.Second,
		},
		Records:    r.records,
		Brake:      r.brake,
		Accountant: r.budget,
		Contested:  broker.NewContestedIndex(),
		Now: func() time.Time {
			now := r.pipelineClock
			r.pipelineClock = r.pipelineClock.Add(r.pipelineTick)
			return now
		},
	})
	if err != nil {
		t.Fatalf("pipeline.New: %v", err)
	}
	r.pipeline = p
	return r
}

// withAdvancingClock moves Config.Now forward by d on every call, so two stamps taken at different
// steps are distinguishable. Only the tests about ORDER use it: for everything else a frozen clock
// is the better fixture, because it makes an accidental extra Now() call invisible rather than
// load-bearing.
func withAdvancingClock(d time.Duration) func(*rig) {
	return func(r *rig) { r.pipelineTick = d }
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
				r.planner = PlannerFunc(func(context.Context, undo.Request, undo.ReferenceIndex, undo.DryRunner) (*undo.Result, error) {
					return nil, boom
				})
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
				r.planner = PlannerFunc(func(context.Context, undo.Request, undo.ReferenceIndex, undo.DryRunner) (*undo.Result, error) {
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

// --- V-BRK-020: the classifier and the integrity check see the same fields ------------------------
//
// These replace TestApplyFailsClosedAtTheIntegrityCheck, which recorded the gap this task closed.
// The gap was that classify marked `apply` as a whole-object verb and execute.CheckIntegrity
// refuses any verb but create/delete carrying that flag, so no apply could execute at all. Its
// positive counterparts are below, and they assert more than "an apply now works": the point of
// giving an apply a real field set is that a `when.fieldPaths` rule can fire on it, and that the
// integrity check has something specific to compare the server's answer against.

// TestAnApplyIsClassifiedFieldByFieldAndExecutes is the direct counterpart of the gap test.
func TestAnApplyIsClassifiedFieldByFieldAndExecutes(t *testing.T) {
	env := applyEnvelope("debug")

	r := newRig(t, func(r *rig) { r.reader = &fakeReader{obj: liveConfigMap()} })
	tr, res, err := r.submit(env)
	if err != nil {
		t.Fatalf("an apply was refused: %v\ntrace: %s", err, tr)
	}
	if res.Phase != string(agentv1alpha1.PhaseVerified) {
		t.Fatalf("phase = %s, want %s\ntrace: %s", res.Phase, agentv1alpha1.PhaseVerified, tr)
	}
	if r.applier.mutations != 1 {
		t.Errorf("applier saw %d mutations, want 1", r.applier.mutations)
	}
}

// TestAFieldPathsRuleFiresOnAnApply is the security property, and it is the reason an apply is not
// a whole-object verb.
//
// A rule reading `when.fieldPaths: [data.log-level]` is the ordinary way an operator says "review
// changes to this setting". Before an apply carried a field set, op.TouchedPaths was empty for it,
// and classify.matches returns false for any fieldPaths rule against an empty path set -- so the
// rule was silently inert against the verb agents use most, while reading in a policy review as a
// control that was in force.
//
// The negative half is not decoration. A test that only asserted the rule fires would pass just as
// well against an implementation that reported EVERY field as touched, which is the other way to
// get a fieldPaths rule to match and is exactly as wrong.
func TestAFieldPathsRuleFiresOnAnApply(t *testing.T) {
	for _, tc := range []struct {
		what      string
		env       *broker.Envelope
		wantGated bool
	}{
		{
			what:      "an apply that changes the named field",
			env:       applyEnvelope("debug"),
			wantGated: true,
		},
		{
			what: "an apply that changes a different field",
			// Same object, same verb, same rule. Only the field differs, so a pass here can only
			// come from the path set being specific to what actually changed.
			env:       applyEnvelopeSetting("owner", "team-a"),
			wantGated: false,
		},
		{
			what: "an apply that re-asserts the live value",
			// The desired state matches live exactly, so the diff is empty and nothing is touched.
			env:       applyEnvelope("info"),
			wantGated: false,
		},
	} {
		t.Run(tc.what, func(t *testing.T) {
			r := newRig(t, func(r *rig) { r.reader = &fakeReader{obj: liveConfigMap()} })
			r.classes.set(mustClassifier(t, []classify.RuleSet{gateFieldPath(t, "review-log-level", "data.log-level")}), nil)

			tr, res, err := r.submit(tc.env)
			if err != nil {
				t.Fatalf("submit: %v\ntrace: %s", err, tr)
			}
			if gated := res.Decision == "gated"; gated != tc.wantGated {
				t.Fatalf("decision = %q (gated=%v), want gated=%v\ntrace: %s", res.Decision, gated, tc.wantGated, tr)
			}
		})
	}
}

// TestTheServersExtraFieldIsCaughtByTheIntegrityCheck is V-BRK-020 in its enforcing direction.
//
// The classifier is shown the diff of the SUBMITTED change. The dry run reports what the server
// would actually do. When the second is wider than the first -- an admission webhook, a defaulter,
// a mutating controller adding something nobody classified -- the action must not proceed. With an
// apply pinned to WholeObject this could never be reached, because the verb was refused before the
// comparison was made; the comparison passing is only meaningful if it can also fail.
func TestTheServersExtraFieldIsCaughtByTheIntegrityCheck(t *testing.T) {
	env := applyEnvelope("debug")

	// What the server says it would produce: the requested change PLUS a field the envelope never
	// mentioned and the classifier therefore never saw.
	expanded := liveConfigMap()
	expanded.Object["data"] = map[string]any{"log-level": "debug", "injected-by-a-webhook": "true"}

	r := newRig(t, func(r *rig) {
		r.reader = &fakeReader{obj: liveConfigMap()}
		r.applier = &fakeApplier{result: expanded}
	})
	tr, res, err := r.submit(env)
	if err == nil {
		t.Fatalf("an apply whose server-side effect exceeded its classification executed (%+v)\ntrace: %s", res, tr)
	}
	if got := tr.Reached(); got != broker.StepExecute {
		t.Fatalf("reached %s, want %s\ntrace: %s", got, broker.StepExecute, tr)
	}
	if !strings.Contains(err.Error(), "injected-by-a-webhook") {
		t.Errorf("the refusal does not name the unclassified field, so a human cannot tell what the server added: %v", err)
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

// solventLedger is broker.Accountant for the rig: every action is within budget, so row 7 never
// fires and the step under test is the one the case is named for.
//
// It records what it was asked, because the point of the accountant being a queried dependency
// rather than an observed value is that it gets the ACTION -- see TestTheAccountantIsAskedAboutThisAction.
type solventLedger struct{ asked []broker.BudgetQuery }

func (l *solventLedger) Budget(q broker.BudgetQuery) broker.BrakeBudget {
	l.asked = append(l.asked, q)
	return broker.BrakeBudget{}
}

// TestTheAccountantIsAskedAboutThisAction. Row 7 budgets an agent's {origin, class} bucket and
// flaps per target (04 §4.2, 06 §1.1), so an accountant handed only the agent cannot answer it.
// This is the assertion that the per-action dimensions actually cross the seam -- the previous
// shape gathered the budget in pipeline.BrakeView, before the envelope was even classified, and
// nothing failed.
func TestTheAccountantIsAskedAboutThisAction(t *testing.T) {
	r := newRig(t)
	env := createEnvelope()
	if _, _, err := r.submit(env); err != nil {
		t.Fatalf("submit: %v", err)
	}

	if len(r.budget.asked) != 1 {
		t.Fatalf("the accountant was asked %d times, want exactly once (at the gate)", len(r.budget.asked))
	}
	q := r.budget.asked[0]
	if q.Trigger != agentv1alpha1.ActionTriggerSource(env.Trigger.Source) {
		t.Errorf("Trigger = %q, want the envelope's %q; the origin partition of 06 §1.1 cannot be applied without it", q.Trigger, env.Trigger.Source)
	}
	if q.Class == "" {
		t.Error("Class is empty; each origin has a per-class bucket and an unclassified query cannot pick one")
	}
	if len(q.Targets) == 0 {
		t.Error("Targets is empty; the flap threshold is counted per target")
	}
	if q.Agent == nil {
		t.Error("Agent is nil; spec.operations.initiativeBudget is the ceiling half of the question")
	}
	if !q.Now.Equal(testClock) {
		t.Errorf("Now = %s, want the decision instant %s, so the accountant and the brake agree which hour this is", q.Now, testClock)
	}
}

// TestNewRejectsAPipelineThatCannotCountItsOwnSpend. The other half of row 7, and the half a unit
// test of broker.Decide cannot reach: a nil Accountant refuses every action at runtime, which is
// safe but is a broker that 503s in production over a wiring mistake. It has to fail at startup.
//
// This is the assertion that would have caught the shape this seam replaced. Before it, the budget
// was a field on the observed BrakeView, an unwired one was the zero BrakeBudget, and the zero
// BrakeBudget permits -- so a broker with no accountant was constructible, started clean, and
// enforced nothing ([[LSN-031]]).
// referenceConfig is the smallest Config New accepts. Every required-field test perturbs exactly
// one thing about it, and each first asserts that the unperturbed version is accepted -- otherwise
// a second missing field makes every one of those tests pass for the wrong reason.
func referenceConfig(t *testing.T) Config {
	t.Helper()
	return Config{
		AgentName:  testAgent,
		Namespace:  testNamespace,
		Classifier: &fakeClassifierSource{c: mustClassifier(t, nil)},
		Live:       &fakeLive{},
		Refs:       fakeRefs{},
		Reader:     &fakeReader{},
		Executor:   &execute.Executor{Applier: &fakeApplier{}, Journal: fakeWriteAhead{}},
		Verifier:   &verify.Driver{},
		Records:    &fakeRecords{},
		Brake:      &fakeBrake{},
		Accountant: &solventLedger{},
		Contested:  broker.NewContestedIndex(),
		DryRunner:  func(string) undo.DryRunner { return &fakeDryRunner{} },
	}
}

func TestNewRejectsAPipelineThatCannotCountItsOwnSpend(t *testing.T) {
	full := func() Config { return referenceConfig(t) }

	if _, err := New(full()); err != nil {
		t.Fatalf("the reference Config was rejected, so the case below proves nothing: %v", err)
	}

	cfg := full()
	cfg.Accountant = nil
	_, err := New(cfg)
	if err == nil {
		t.Fatal("New accepted a Config with no Accountant; row 7 would refuse every action at request time instead of failing at boot")
	}
	if !strings.Contains(err.Error(), "Accountant") {
		t.Fatalf("the error must name what is missing: %v", err)
	}
}

// --- V-REV-003: the undo plan is dry-run before the action, or the action gates ------------------
//
// The L1 row for V-REV-003 already existed and it proved the wrong half. It showed that
// undo.Validate DOWNGRADES when handed a nil dry-runner -- a property of the function -- and
// nothing anywhere asserted that a dry-runner is ever wired. It never was: pipeline.Config.Planner
// defaulted to the generate-only `undo.Generate`, cmd/broker/wiring.go left it unset on purpose,
// and undo.GenerateAndValidate ("the call the broker actually makes at step 6") had no non-test
// caller. Every ActionRecord the broker wrote carried `undoPlan.validated: false`;
// undo.ValidateReplayable refuses on exactly that field; the undo path was dead end to end and the
// suite was green. 09 §11.9, "component built, never wired".
//
// So these tests assert the wiring and the consequence, not the function.

func TestNewRejectsABrokerThatCannotDryRunItsUndoPlans(t *testing.T) {
	if _, err := New(referenceConfig(t)); err != nil {
		t.Fatalf("the reference Config was rejected, so the case below proves nothing: %v", err)
	}

	cfg := referenceConfig(t)
	cfg.DryRunner = nil
	_, err := New(cfg)
	if err == nil {
		t.Fatal("New accepted a Config with no DryRunner; such a broker starts clean, serves every request, and journals `validated: false` on every record it writes -- and nothing finds out until a human tries to undo one")
	}
	if !strings.Contains(err.Error(), "DryRunner") {
		t.Fatalf("the error must name what is missing: %v", err)
	}
}

// TestTheJournaledPlanWasDryRunAgainstTheAPIServer is the property V-REV-001 measures at L2, at the
// level where it can be measured without a cluster: an accepted action's record carries a plan that
// something actually checked.
func TestTheJournaledPlanWasDryRunAgainstTheAPIServer(t *testing.T) {
	r := newRig(t)
	tr, res, err := r.submit(createEnvelope())
	if err != nil {
		t.Fatalf("submit: %v\ntrace: %s", err, tr)
	}
	if res.Decision != "accepted" {
		t.Fatalf("decision = %q, want accepted\ntrace: %s", res.Decision, tr)
	}
	if len(r.records.stored) != 1 {
		t.Fatalf("nothing was stored")
	}
	plan := r.records.stored[0].Spec.Undo
	if plan == nil {
		t.Fatal("the record carries no undo plan at all")
	}
	if !plan.Validated {
		t.Errorf("the journaled plan has validated=false, so undo.ValidateReplayable would refuse it and this action can never be rolled back; plan = %+v", plan)
	}

	// And it was checked step by step, not stamped. A `validated: true` set by anything other than a
	// completed pass over the steps is the failure mode this whole unit exists to remove.
	if len(r.dryRunner.steps) != len(plan.Steps) || len(plan.Steps) == 0 {
		t.Fatalf("the dry-runner saw %d step(s) and the plan has %d; every step must have been checked", len(r.dryRunner.steps), len(plan.Steps))
	}
	for i, got := range r.dryRunner.steps {
		if got.Op != plan.Steps[i].Op || got.Target.Name != plan.Steps[i].Target.Name {
			t.Errorf("step %d dry-run was %s %s, but the plan's step %d is %s %s",
				i, got.Op, got.Target.Name, i, plan.Steps[i].Op, plan.Steps[i].Target.Name)
		}
	}
}

// TestADryRunIssuedUnderAnotherNameWouldBeAWrongAnswer pins the identity the pipeline asks for.
//
// Server-side apply reports a conflict for every field owned by a different field manager, and the
// fields an undo restores are frequently the ones this agent set in an earlier action. A dry run
// issued under any other name manufactures conflicts the real replay never hits -- so this seam is
// wrong in the OVER-gating direction, silently, and the only way to see it is to look at the key.
func TestADryRunIssuedUnderAnotherNameWouldBeAWrongAnswer(t *testing.T) {
	r := newRig(t)
	if _, _, err := r.submit(createEnvelope()); err != nil {
		t.Fatalf("submit: %v", err)
	}
	want := testIdentity().AgentIdentity()
	if len(r.dryRunner.identities) == 0 {
		t.Fatal("the pipeline never asked for a dry-runner, so no step was validated")
	}
	for _, got := range r.dryRunner.identities {
		if got != want {
			t.Errorf("the dry-runner was built for identity %q, want %q -- the replay's field manager", got, want)
		}
	}
}

// TestAStepThatWouldNotApplyGatesInsteadOfExecuting is 06 §4.3.1's second sentence: "if generation
// or validation fails, the action is raised to gated". A create is `routine` on every other input,
// so the gate here can only have come from the failed validation.
func TestAStepThatWouldNotApplyGatesInsteadOfExecuting(t *testing.T) {
	r := newRig(t, func(r *rig) {
		r.dryRunner = &fakeDryRunner{err: errors.New("configmaps is forbidden: User cannot delete resource")}
	})
	tr, res, err := r.submit(createEnvelope())
	if err != nil {
		t.Fatalf("submit: %v\ntrace: %s", err, tr)
	}
	if res.Decision != "gated" {
		t.Fatalf("decision = %q, want gated: a plan whose steps will not apply is not a plan\ntrace: %s", res.Decision, tr)
	}
	if r.applier.mutations != 0 {
		t.Errorf("applier saw %d mutations; a gated action mutates nothing", r.applier.mutations)
	}
	if len(r.records.stored) != 1 {
		t.Fatalf("the parked record was not written")
	}
	ar := r.records.stored[0]
	if ar.Spec.Undo == nil || ar.Spec.Undo.Strategy != agentv1alpha1.UndoNone {
		t.Errorf("undo strategy = %+v, want none: a plan that failed validation must not keep its steps, or a caller who checks only for their presence will replay them", ar.Spec.Undo)
	}
	if ar.Spec.Undo != nil && ar.Spec.Undo.Validated {
		t.Error("the downgraded plan still claims validated=true")
	}
	// The reason reaches a human, and it is the API server's own words rather than "validation
	// failed". The operator deciding whether to approve this needs to know it was an RBAC denial.
	var caveats string
	if ar.Spec.Undo != nil {
		caveats = strings.Join(ar.Spec.Undo.Caveats, "; ")
	}
	if !strings.Contains(caveats, "is forbidden") {
		t.Errorf("the plan's caveats do not carry the reason the step would not apply: %q", caveats)
	}
}

// TestADryRunWhoseUndoPlanWouldNotApplyIsNotAServerFault pins the asymmetry that wiring the
// validator exposed, and records where the rest of it lives.
//
// There are THREE sites that ask "is there a usable undo plan", not two: classify's 06 §4.2 step 6
// floor, the pipeline's own step 6 re-check, and the brake's 06 §4.4 row 5. Only the first
// suppresses for a dry run. Before this unit the difference could not show, because no plan was
// ever validated and so no plan was ever downgraded; wiring the dry-runner makes a dry run whose
// steps would 403 -- the ordinary case under a read-only shadow overlay -- the first envelope to
// reach the disagreement.
//
// Two of the three now read the SAME predicate, classify.UndoPlanGateApplies, rather than two
// spellings of one rule. Be precise about what that buys, because the tempting claim is bigger than
// the truth: it is a structural fix, not a behavioural one. Mutating step 6 back to its own
// spelling does NOT fail this test, and the reason is the third site -- the brake has already
// raised the class to gated by the time step 6 looks, so step 6's second conjunct is false either
// way. The predicate is asserted directly, in TestTheUndoPlanGateHasOneDefinitionSite; the value
// here is that the two cannot drift apart later, not that today's behaviour depends on it.
//
// The third site is not reconciled, and that is deliberate. The brake raises this dry run to gated,
// so it parks for approval instead of previewing -- over-gating rather than under-gating, safe, and
// a rule in the 06 §4.4 table, which is V-BRK surface. Changing a brake row is a unit of its own,
// not something to fold into the unit whose wiring surfaced it. Filed, asserted as-is below so that
// reconciling it later is a visible change to this test, and named in the ledger.
func TestADryRunWhoseUndoPlanWouldNotApplyIsNotAServerFault(t *testing.T) {
	r := newRig(t, func(r *rig) {
		r.dryRunner = &fakeDryRunner{err: errors.New("configmaps is forbidden: User cannot delete resource")}
	})
	env := createEnvelope()
	env.DryRun = true

	tr, res, err := r.submit(env)
	if err != nil {
		t.Fatalf("submit: %v -- a dry run whose undo plan could not be validated is not a server fault\ntrace: %s", err, tr)
	}
	// The one thing a dry run may never do, whatever it is classified.
	if r.applier.mutations != 0 {
		t.Errorf("applier saw %d mutations on a dry run", r.applier.mutations)
	}
	if got := eventFor(tr, broker.StepUndoPlan); got.Status != broker.StepCompleted {
		t.Errorf("step 6 = %s, want completed: the dry-run suppression has to hold at both sites that spell the rule\ntrace: %s", got, tr)
	}
	// The brake's row 5, asserted so that reconciling it later is a visible change to this test
	// rather than a silent one.
	if res.Decision != "gated" {
		t.Fatalf("decision = %q; today the brake raises this to gated at step 5\ntrace: %s", res.Decision, tr)
	}
	if !strings.Contains(tr.String(), string(broker.BrakeRuleUndoPlanUnusable)) {
		t.Errorf("the gate did not come from the brake's undo-plan row, so this test is measuring something else\ntrace: %s", tr)
	}
}

// TestStep6RefusesAPlanNothingValidated is the drift guard, and it is the reason step 6 asks
// Validated() rather than Undoable().
//
// The planner is a seam. A future one -- or a mis-wired present one -- can return a plan that looks
// entirely usable and that no dry-runner ever saw; `Undoable()` is true for it, the classifier is
// satisfied, and the action executes carrying a rollback that ValidateReplayable will refuse. The
// pipeline has to be able to tell those apart without trusting whoever supplied the planner.
func TestStep6RefusesAPlanNothingValidated(t *testing.T) {
	r := newRig(t, func(r *rig) {
		r.planner = PlannerFunc(func(context.Context, undo.Request, undo.ReferenceIndex, undo.DryRunner) (*undo.Result, error) {
			return &undo.Result{Plan: &agentv1alpha1.UndoPlan{
				Strategy: agentv1alpha1.UndoDelete,
				Steps: []agentv1alpha1.UndoStep{{
					Op:     "delete",
					Target: agentv1alpha1.TargetRef{Version: "v1", Kind: "ConfigMap", Namespace: testTenantNS, Name: "app-config"},
				}},
				// Never dry-run. This is the whole mutant.
				Validated: false,
			}}, nil
		})
	})
	tr, _, err := r.submit(createEnvelope())
	if err == nil {
		t.Fatalf("the pipeline executed a plan nothing had validated\ntrace: %s", tr)
	}
	if r.applier.mutations != 0 {
		t.Errorf("applier saw %d mutations", r.applier.mutations)
	}
	if got := tr.Reached(); got != broker.StepUndoPlan {
		t.Errorf("failed at %s, want %s -- the fault belongs to step 6\ntrace: %s", got, broker.StepUndoPlan, tr)
	}
}

// TestTheUndoPlanGateHasOneDefinitionSite. The suppression above is only safe if step 6 and the
// classifier cannot disagree about when it applies, and they cannot disagree only if there is one
// predicate. This is that predicate's own truth table, including the row that makes it worth
// having: a dry run with no plan is excused, and the same envelope for real is not.
func TestTheUndoPlanGateHasOneDefinitionSite(t *testing.T) {
	for _, tc := range []struct {
		dryRun, present, want bool
	}{
		{dryRun: false, present: false, want: true},
		{dryRun: false, present: true, want: false},
		{dryRun: true, present: false, want: false},
		{dryRun: true, present: true, want: false},
	} {
		if got := classify.UndoPlanGateApplies(tc.dryRun, tc.present); got != tc.want {
			t.Errorf("UndoPlanGateApplies(dryRun=%t, present=%t) = %t, want %t", tc.dryRun, tc.present, got, tc.want)
		}
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

// --- V-BRK-031: a permission boundary is an answer, not a crash ---------------------------------

// TestALiveReadFailureIsTypedByWhetherItCanEverSucceed is V-BRK-031's positive half.
//
// Step 3 makes three live reads with the ACTOR identity, and before this check none of their
// failures was a typed *Refusal. `server.go`'s `refuse` type-asserts for one, did not find one, and
// answered 500 `internal-error` -- so a caller could not tell its own authority ceiling from a
// broken broker, and, because `Journal` and `SecurityEvent` ride ON the Refusal, the envelope's
// disposition was recorded nowhere at all.
//
// The property is not "these failures are typed". It is that they are typed by WHETHER RETRYING
// COULD EVER HELP: an RBAC denial is permanent and carries no Retry-After, an API-server fault is
// transient and does. Both halves are asserted for every one of the three reads, because a fix
// applied to the snapshot alone would leave two sites answering 500 and would look identical from
// the arm that motivated it.
func TestALiveReadFailureIsTypedByWhetherItCanEverSucceed(t *testing.T) {
	forbidden := apierrors.NewForbidden(
		schema.GroupResource{Resource: "configmaps"}, "app-config",
		errors.New(`User "system:serviceaccount:kubeagents-system:platform-dev-actor" cannot get resource "configmaps"`))
	unauthorized := apierrors.NewUnauthorized("the actor's token was rejected")

	sites := []struct {
		read   string
		env    *broker.Envelope
		break_ func(*rig, error)
	}{
		{"the pre-state snapshot", createEnvelope(), func(r *rig, e error) { r.reader.err = e }},
		// A Deployment, not the ConfigMap the other two rows use: verify.NeedsRestartBaseline is
		// keyed by kind, and a ConfigMap's row does not compare restart counts across the action, so
		// the prober is never called and this row would pass green having exercised nothing.
		{"the restart baseline", deploymentEnvelope(), func(r *rig, e error) { r.prober.restartErr = e }},
		{"the live-state resolve", createEnvelope(), func(r *rig, e error) { r.live.getErr = e }},
	}
	classes := []struct {
		name       string
		err        error
		wantStatus int
		wantReason string
		wantRetry  bool
	}{
		{"an RBAC denial", forbidden, http.StatusForbidden, broker.ReasonTargetForbidden, false},
		{"a rejected credential", unauthorized, http.StatusForbidden, broker.ReasonTargetForbidden, false},
		{"an API-server fault", errInjected, http.StatusServiceUnavailable, broker.ReasonSnapshotFailed, true},
	}

	for _, site := range sites {
		for _, class := range classes {
			t.Run(site.read+"/"+class.name, func(t *testing.T) {
				r := newRig(t, func(r *rig) { site.break_(r, class.err) })
				_, _, err := r.submit(site.env)

				ref, ok := err.(*broker.Refusal)
				if !ok {
					t.Fatalf("%s failed with %v (%T), which is not a *broker.Refusal -- server.go "+
						"type-asserts for one and answers 500 internal-error without it", site.read, err, err)
				}
				if ref.Status != class.wantStatus || ref.Reason != class.wantReason {
					t.Errorf("%s / %s: HTTP %d %q, want %d %q",
						site.read, class.name, ref.Status, ref.Reason, class.wantStatus, class.wantReason)
				}
				if gotRetry := ref.RetryAfterSeconds > 0; gotRetry != class.wantRetry {
					t.Errorf("%s / %s: retryAfterSeconds=%d, want retryable=%v. Telling a fleet to "+
						"wait and retry a permission boundary is a loop that never terminates",
						site.read, class.name, ref.RetryAfterSeconds, class.wantRetry)
				}
				if !ref.Journal {
					t.Errorf("%s / %s: Journal=false. The journal is the complete record of every "+
						"envelope's disposition; an agent enumerating what it may touch must leave a trace",
						site.read, class.name)
				}
				if ref.SecurityEvent {
					t.Errorf("%s / %s: SecurityEvent=true. In shadow mode the actor holds no tenant "+
						"authority at all, so this fires on every action -- an alarm that gets muted. "+
						"03 §6's events are for identity violations; forbidden-caller is that case",
						site.read, class.name)
				}
				if !strings.Contains(ref.Detail, "step 3") {
					t.Errorf("%s / %s: detail %q does not name the step; a refusal that says only "+
						"'the read failed' sends a human to the wrong object", site.read, class.name, ref.Detail)
				}
			})
		}
	}
}

// TestLiveReadRefusalDiscriminatesRatherThanDefaulting is V-BRK-031's mandatory negative control.
//
// The test above passes on an implementation that answers 403 `target-forbidden` for every error it
// ever sees -- two thirds of its rows would fail, but a reader skimming a green run would not know
// that, and the shape of "type everything as the case that motivated the fix" is exactly how this
// gets written. So the discrimination itself is asserted directly, on the one function that makes
// it: the two classes must differ in reason, in status, and in retryability, and a nil error must
// stay nil rather than becoming a refusal of the pipeline's own healthy reads.
func TestLiveReadRefusalDiscriminatesRatherThanDefaulting(t *testing.T) {
	if got := liveReadRefusal(nil, "step 3: a read that worked"); got != nil {
		t.Fatalf("a nil error produced %v; every successful read in step 3 would refuse", got)
	}

	perm, ok := liveReadRefusal(
		apierrors.NewForbidden(schema.GroupResource{Resource: "configmaps"}, "c", errors.New("denied")),
		"step 3: x").(*broker.Refusal)
	if !ok {
		t.Fatal("a Forbidden did not produce a *broker.Refusal")
	}
	trans, ok := liveReadRefusal(errInjected, "step 3: x").(*broker.Refusal)
	if !ok {
		t.Fatal("an API-server fault did not produce a *broker.Refusal")
	}

	if perm.Reason == trans.Reason {
		t.Errorf("both classes answer %q; the helper is defaulting, not discriminating", perm.Reason)
	}
	if perm.Status == trans.Status {
		t.Errorf("both classes answer HTTP %d; a caller cannot branch on the status alone", perm.Status)
	}
	if perm.RetryAfterSeconds != 0 {
		t.Errorf("the permanent class carries retryAfterSeconds=%d; Refusal's own rule is that an "+
			"authorization refusal is never retryable", perm.RetryAfterSeconds)
	}
	if trans.RetryAfterSeconds <= 0 {
		t.Error("the transient class carries no retryAfterSeconds, so a recoverable outage reads as final")
	}

	// A NotFound never reaches here -- CaptureAll turns it into an empty pre-state, which is the
	// correct reading for a `create` whose target does not exist yet, and every other test in this
	// file runs on exactly that path (newRig's reader is `absent: true`). Asserted there, by
	// construction, rather than restated here as a case this helper is not the owner of.
}

// --- V-BRK-006: the lifecycle clock, and the ordering it is evidence for -------------------------

// V-BRK-006's L2 clause is read off two clocks that never meet: `metadata.creationTimestamp`, which
// the API SERVER assigns when step 8 writes the record, against
// `status.timestamps.executionStarted`, which the BROKER stamps when step 9 issues its first
// mutating call. A broker that journaled after executing inverts them, and nothing else in the tree
// would notice.
//
// Nothing wrote `status.timestamps` at all until this test existed. The field was declared, read in
// three places -- `budget.go`'s window, `cooldown.go`'s pair, `JournalReconciler.exportLateness`'s
// four-way fallback -- and populated by nobody, so `broker-execute-l2.sh` reached step 11 against a
// real cluster and had nothing to compare a creationTimestamp against.
func TestTheLifecycleClockIsStampedAndOrdered(t *testing.T) {
	// One second per Now() call, so the beats are distinguishable. A frozen clock would let a
	// pipeline that stamped every field from the same variable pass the ordering assertions below.
	r := newRig(t, withAdvancingClock(time.Second))
	tr, _, err := r.submit(createEnvelope())
	if err != nil {
		t.Fatalf("submit: %v\ntrace: %s", err, tr)
	}

	st := r.records.finalStatus
	if st == nil || st.Timestamps == nil {
		t.Fatalf("step 11 handed the store a record with no lifecycle clock at all: %+v", st)
	}
	ts := st.Timestamps

	for _, f := range []struct {
		name string
		at   *metav1.Time
	}{
		{"submitted", ts.Submitted},
		{"classified", ts.Classified},
		{"executionStarted", ts.ExecutionStarted},
		{"executionEnded", ts.ExecutionEnded},
		{"verified", ts.Verified},
	} {
		if f.at == nil {
			t.Errorf("status.timestamps.%s is nil after a submission that reached step 11", f.name)
		}
	}
	if t.Failed() {
		t.FailNow()
	}

	// `approved` is the ChatOps gateway's to write (06 §4.3), and this action was never gated.
	// A broker that stamped it would be asserting an approval that never happened.
	if ts.Approved != nil {
		t.Errorf("status.timestamps.approved = %v on an ungated action; only the roster's SA may set it", ts.Approved)
	}

	// The order the clock is supposed to encode. Written as a chain rather than five independent
	// comparisons so that a pipeline stamping the right fields in the wrong steps fails here.
	beats := []struct {
		name string
		at   *metav1.Time
	}{
		{"submitted", ts.Submitted},
		{"classified", ts.Classified},
		{"executionStarted", ts.ExecutionStarted},
		{"executionEnded", ts.ExecutionEnded},
		{"verified", ts.Verified},
	}
	for i := 1; i < len(beats); i++ {
		if beats[i].at.Time.Before(beats[i-1].at.Time) {
			t.Errorf("%s (%s) is before %s (%s) -- the lifecycle clock runs backwards",
				beats[i].name, beats[i].at.Time.Format(time.RFC3339),
				beats[i-1].name, beats[i-1].at.Time.Format(time.RFC3339))
		}
	}

	// THE WRITE-AHEAD ORDERING ITSELF, in the only form a unit test can put it: the record was
	// handed to Create -- the journal write -- before the executor was called. The L2 suite asserts
	// the same property against two real clocks; this asserts it against the call sequence, which is
	// the thing a unit test can actually see.
	assertBefore(t, tr, broker.StepSnapshot, broker.StepExecute)
	if ts.ExecutionStarted.Time.Before(r.records.stored[0].CreationTimestamp.Time) {
		t.Errorf("executionStarted %s precedes the record's creationTimestamp %s",
			ts.ExecutionStarted.Time, r.records.stored[0].CreationTimestamp.Time)
	}

	// And the two beats that existed BEFORE the record did are durable from its first write, not
	// only from step 11's. `stored` is the Create-time copy.
	born := r.records.stored[0].Status.Timestamps
	if born == nil || born.Submitted == nil || born.Classified == nil {
		t.Errorf("the record was created without the two beats that had already happened: %+v", born)
	}
}

// The outcome block the pipeline composes has to survive the trip to the store. Step 9 sets
// `status.applied`, step 11 sets `status.verification` and `status.recovery`, and for as long as
// `SetPhase` wrote only phase and message every one of them was dropped on the floor -- a live
// record from `broker-execute-l2.sh` read back with a phase, a message, and nothing else.
// The write-ahead record is durable at step 8 and the executor runs at step 9, so anything the
// pipeline does between them happens at the one moment a failure is unrecoverable: the journal
// already says an action is Executing, and nothing has executed. A nil dereference there does not
// fail the action -- it kills the process and leaves a record no code path will advance.
//
// This is not hypothetical. `status` is a subresource, the API server drops the block the broker
// POSTs, and the first version of the lifecycle clock stamped straight through the nil pointer that
// came back. It panicked on a live cluster on 2026-07-31. The store now restores the block, so this
// test asserts the OTHER guarantee -- that the pipeline does not depend on it having done so.
func TestTheClockSurvivesAServerThatKeepsNoStatus(t *testing.T) {
	r := newRig(t, withAdvancingClock(time.Second))
	r.records.dropStatusOnCreate = true

	tr, _, err := r.submit(createEnvelope())
	if err != nil {
		t.Fatalf("submit: %v\ntrace: %s", err, tr)
	}

	// The three beats the pipeline owns AFTER the Create must all be there: they are the ones it can
	// still stamp on a record the server handed back empty.
	st := r.records.finalStatus
	if st == nil || st.Timestamps == nil {
		t.Fatalf("status.timestamps is nil after a submission that reached step 11: %+v", st)
	}
	ts := st.Timestamps
	for _, b := range []struct {
		name string
		at   *metav1.Time
	}{
		{"executionStarted", ts.ExecutionStarted},
		{"executionEnded", ts.ExecutionEnded},
		{"verified", ts.Verified},
	} {
		if b.at == nil {
			t.Errorf("status.timestamps.%s is nil; the pipeline stamps it after the Create and must not need the server to have kept anything", b.name)
		}
	}
}

func TestStepElevenHandsTheStoreTheWholeOutcome(t *testing.T) {
	r := newRig(t)
	tr, _, err := r.submit(createEnvelope())
	if err != nil {
		t.Fatalf("submit: %v\ntrace: %s", err, tr)
	}
	st := r.records.finalStatus
	if st == nil {
		t.Fatal("step 11 handed the store no status at all")
	}
	if len(st.Applied) == 0 {
		t.Errorf("status.applied is empty after an action that mutated one target")
	}
	if st.Verification == nil {
		t.Errorf("status.verification is nil after a submission that ran step 10")
	}
	if st.Timestamps == nil {
		t.Errorf("status.timestamps is nil")
	}
}
