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

package history

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/classify"
)

const (
	testAgent = "developer-team-team-x"
	testNS    = "team-x"
)

var (
	testAt   = time.Date(2026, 7, 29, 12, 0, 0, 0, time.UTC)
	deployKn = classify.KindRef{Group: "apps", Kind: "Deployment"}
	cmKn     = classify.KindRef{Kind: "ConfigMap"}
)

type fakeClock struct{ t time.Time }

func (c *fakeClock) now() time.Time { return c.t }

// fakeJournal answers with a fixed item list. It deliberately does NOT filter on the namespace
// option -- the test that cares asserts the option was PASSED, because a source that quietly listed
// cluster-wide would be Forbidden in production and green here.
type fakeJournal struct {
	mu    sync.Mutex
	items []agentv1alpha1.ActionRecord
	err   error
	opts  []client.ListOption
	calls int
}

func (f *fakeJournal) reads() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.calls
}

func (f *fakeJournal) List(_ context.Context, list client.ObjectList, opts ...client.ListOption) error {
	f.mu.Lock()
	f.calls++
	f.opts = opts
	f.mu.Unlock()
	f.mu.Lock()
	err := f.err
	f.mu.Unlock()
	if err != nil {
		return err
	}
	l, ok := list.(*agentv1alpha1.ActionRecordList)
	if !ok {
		return errors.New("wrong list type")
	}
	f.mu.Lock()
	l.Items = append([]agentv1alpha1.ActionRecord(nil), f.items...)
	f.mu.Unlock()
	return nil
}

// rec builds a Verified, non-dry-run record whose undo plan carries one step. Every field a filter
// in derive looks at is spelled out here rather than left to a zero value, so a test that flips one
// is flipping exactly one thing.
func rec(strategy agentv1alpha1.UndoStrategy, stepOp string, target agentv1alpha1.TargetRef, opts ...func(*agentv1alpha1.ActionRecord)) agentv1alpha1.ActionRecord {
	r := agentv1alpha1.ActionRecord{
		Spec: agentv1alpha1.ActionRecordSpec{
			AgentRef: agentv1alpha1.AgentObjectRef{Name: testAgent, Namespace: testNS},
			DryRun:   false,
			Targets:  []agentv1alpha1.TargetRef{target},
			Undo: &agentv1alpha1.UndoPlan{
				Strategy: strategy,
				Steps:    []agentv1alpha1.UndoStep{{Op: stepOp, Target: target}},
			},
		},
		Status: agentv1alpha1.ActionRecordStatus{Phase: agentv1alpha1.PhaseVerified},
	}
	for _, o := range opts {
		o(&r)
	}
	return r
}

func deployTarget() agentv1alpha1.TargetRef {
	return agentv1alpha1.TargetRef{Group: "apps", Version: "v1", Kind: "Deployment", Namespace: testNS, Name: "api-gateway"}
}

// sourceOver builds a refreshed source over the given records, at testAt.
func sourceOver(t *testing.T, items ...agentv1alpha1.ActionRecord) (*Source, *fakeJournal, *fakeClock) {
	t.Helper()
	j := &fakeJournal{items: items}
	clk := &fakeClock{t: testAt}
	s, err := NewSource(SourceConfig{Journal: j, Namespace: testNS, Now: clk.now})
	if err != nil {
		t.Fatalf("NewSource: %v", err)
	}
	if err := s.Refresh(context.Background()); err != nil {
		t.Fatalf("Refresh: %v", err)
	}
	return s, j, clk
}

// --- the verb dimension, which is the whole argument -------------------------------------------

// 06 §4.3.1 read backwards. Each row is a forward verb, the undo plan the table says it produces,
// and the verbs that plan must and must not make familiar. This is the table that stops the
// coarsening from becoming a loosening.
func TestTheUndoPlanRecoversTheVerbClass(t *testing.T) {
	cases := []struct {
		name     string
		strategy agentv1alpha1.UndoStrategy
		stepOp   string
		familiar []string // verbs this record alone must make familiar
		novel    []string // verbs it must NOT
	}{
		{
			name: "delete/delete is a create, and does not vouch for an update",
			// `create` and `apply`-on-an-absent-object are the same mutation, so they share a plan.
			// `apply` needs BOTH its classes, so one create is not enough for it.
			strategy: agentv1alpha1.UndoDelete, stepOp: "delete",
			familiar: []string{"create"},
			novel:    []string{"apply", "patch", "scale", "delete", "cloud"},
		},
		{
			name: "restore/apply is a patch, and does not vouch for a scale",
			// This is the pair the coarsening is ALLOWED to collapse: an apply over an object that
			// existed is a patch. `scale` shares the strategy and is separated by the step op.
			strategy: agentv1alpha1.UndoRestore, stepOp: "apply",
			familiar: []string{"patch"},
			novel:    []string{"apply", "create", "scale", "delete", "cloud"},
		},
		{
			name:     "restore/scale is a scale, and nothing else reaches it",
			strategy: agentv1alpha1.UndoRestore, stepOp: "scale",
			familiar: []string{"scale"},
			novel:    []string{"patch", "apply", "create", "delete", "cloud"},
		},
		{
			name: "recreate/create is a delete, and no update ever vouches for it",
			// The case that makes the whole design necessary: an agent that has patched Deployments
			// all day is still novel the first time it deletes one.
			strategy: agentv1alpha1.UndoRecreate, stepOp: "create",
			familiar: []string{"delete"},
			novel:    []string{"create", "apply", "patch", "scale", "cloud"},
		},
		{
			name:     "inverse is a cloud operation, whatever the provider called the step",
			strategy: agentv1alpha1.UndoInverse, stepOp: "setSize",
			familiar: []string{"cloud"},
			novel:    []string{"create", "apply", "patch", "scale", "delete"},
		},
		{
			name:     "none teaches nothing, because the action it describes was gated for being irreversible",
			strategy: agentv1alpha1.UndoNone, stepOp: "",
			familiar: nil,
			novel:    []string{"create", "apply", "patch", "scale", "delete", "cloud"},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s, _, _ := sourceOver(t, rec(tc.strategy, tc.stepOp, deployTarget()))
			for _, v := range tc.familiar {
				if !s.Seen(testAgent, v, deployKn, testNS) {
					t.Errorf("%s/%s must make %q familiar", tc.strategy, tc.stepOp, v)
				}
			}
			for _, v := range tc.novel {
				if s.Seen(testAgent, v, deployKn, testNS) {
					t.Errorf("%s/%s must NOT make %q familiar -- that is a risk class lowered by a coarsening", tc.strategy, tc.stepOp, v)
				}
			}
		})
	}
}

// `apply` is the union of two plans, so it is familiar only once the journal shows both. Asserted
// as a sequence rather than as a table row, because the property is that one half is not enough.
func TestApplyNeedsBothOfItsClasses(t *testing.T) {
	created := rec(agentv1alpha1.UndoDelete, "delete", deployTarget())
	updated := rec(agentv1alpha1.UndoRestore, "apply", deployTarget())

	s, _, _ := sourceOver(t, created)
	if s.Seen(testAgent, "apply", deployKn, testNS) {
		t.Error("an agent that has only ever created is not yet familiar with applying: its next apply may be the update it has never done")
	}
	s, _, _ = sourceOver(t, updated)
	if s.Seen(testAgent, "apply", deployKn, testNS) {
		t.Error("an agent that has only ever updated is not yet familiar with applying: its next apply may create")
	}
	s, _, _ = sourceOver(t, created, updated)
	if !s.Seen(testAgent, "apply", deployKn, testNS) {
		t.Error("both halves present and apply is still novel; the source can never retire the escalation")
	}
}

// A step with no op has no class, and a plan carrying one teaches nothing from that step. The CRD
// requires the field, so this is a shape the server should never store -- which is exactly why it is
// worth pinning: `class` is the one place a missing op could quietly become the empty string and
// then match the empty class of some other record. It must yield no class at all.
//
// Only `inverse` is exempt: its class is "inverse/*" precisely because its steps are provider calls
// whose ops this table does not enumerate.
func TestAStepWithNoOpTeachesNothing(t *testing.T) {
	for _, strategy := range []agentv1alpha1.UndoStrategy{
		agentv1alpha1.UndoRestore, agentv1alpha1.UndoDelete, agentv1alpha1.UndoRecreate,
	} {
		t.Run(string(strategy), func(t *testing.T) {
			if got := class(strategy, ""); got != "" {
				t.Errorf("class(%s, \"\") = %q; a step with no op must produce no class, not a half-formed one that could collide", strategy, got)
			}
			s, _, _ := sourceOver(t, rec(strategy, "", deployTarget()))
			for _, v := range classify.KnownVerbs() {
				if s.Seen(testAgent, v, deployKn, testNS) {
					t.Errorf("an op-less %s plan made %q familiar", strategy, v)
				}
			}
		})
	}
	// The exemption, stated so a future reader does not "fix" it into the loop above.
	if got := class(agentv1alpha1.UndoInverse, ""); got != "inverse/*" {
		t.Errorf("class(inverse, \"\") = %q; an inverse plan's class never depended on the step op", got)
	}
}

// Every verb classify matches on must have a row, or it is silently never familiar. The join is
// against classify.KnownVerbs() and not against a hand-copied list, which is the same discipline
// classify.knownVerbs itself documents.
func TestEveryKnownVerbHasEvidenceDefined(t *testing.T) {
	for _, v := range classify.KnownVerbs() {
		if _, ok := verbEvidence[v]; !ok {
			t.Errorf("verb %q is in classify.KnownVerbs() with no verbEvidence row: it would be novel forever, and nobody would find out from a failing test", v)
		}
	}
	for v := range verbEvidence {
		found := false
		for _, k := range classify.KnownVerbs() {
			if k == v {
				found = true
			}
		}
		if !found {
			t.Errorf("verbEvidence has a row for %q, which is not a known verb; the table has drifted from the enum", v)
		}
	}
}

// --- which records build trust ------------------------------------------------------------------

func TestOnlyAVerifiedNonDryRunRecordBuildsTrust(t *testing.T) {
	cases := []struct {
		name string
		mut  func(*agentv1alpha1.ActionRecord)
		want bool
	}{
		{"verified and executed", func(*agentv1alpha1.ActionRecord) {}, true},
		{
			"a dry run is not experience -- this is the whole of Phase 9",
			func(r *agentv1alpha1.ActionRecord) { r.Spec.DryRun = true },
			false,
		},
		{
			"executing has not finished",
			func(r *agentv1alpha1.ActionRecord) { r.Status.Phase = agentv1alpha1.PhaseExecuting },
			false,
		},
		{
			"failed did not stand",
			func(r *agentv1alpha1.ActionRecord) { r.Status.Phase = agentv1alpha1.PhaseFailed },
			false,
		},
		{
			"rolled back did not stand either",
			func(r *agentv1alpha1.ActionRecord) { r.Status.Phase = agentv1alpha1.PhaseRolledBack },
			false,
		},
		{
			"rejected never touched the cluster",
			func(r *agentv1alpha1.ActionRecord) { r.Status.Phase = agentv1alpha1.PhaseRejected },
			false,
		},
		{
			"parked for approval is not a completed action",
			func(r *agentv1alpha1.ActionRecord) { r.Status.Phase = agentv1alpha1.PhasePendingApproval },
			false,
		},
		{
			// The write DID stand, and a human reversed it. Counting it would suppress the
			// escalation on exactly the repeat a human just said no to.
			"undone is a human saying no, not the agent earning trust",
			func(r *agentv1alpha1.ActionRecord) { r.Status.Phase = agentv1alpha1.PhaseUndone },
			false,
		},
		{
			"a record with no undo plan carries no verb evidence",
			func(r *agentv1alpha1.ActionRecord) { r.Spec.Undo = nil },
			false,
		},
		{
			"a record with no agent confers familiarity on nobody",
			func(r *agentv1alpha1.ActionRecord) { r.Spec.AgentRef.Name = "" },
			false,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s, _, _ := sourceOver(t, rec(agentv1alpha1.UndoRestore, "apply", deployTarget(), tc.mut))
			if got := s.Seen(testAgent, "patch", deployKn, testNS); got != tc.want {
				t.Fatalf("Seen = %v, want %v", got, tc.want)
			}
			// "nobody" has to include the empty name itself. Asking only about testAgent would pass
			// for a source that had cheerfully keyed the record under agent "" -- and a caller whose
			// own name failed to resolve would then be familiar with everything such a record held.
			if s.Seen("", "patch", deployKn, testNS) {
				t.Fatal("no record may make the empty agent name familiar with anything")
			}
		})
	}
}

// --- the key's other three dimensions -----------------------------------------------------------

func TestFamiliarityDoesNotCrossAgentKindOrNamespace(t *testing.T) {
	s, _, _ := sourceOver(t, rec(agentv1alpha1.UndoRestore, "apply", deployTarget()))
	if !s.Seen(testAgent, "patch", deployKn, testNS) {
		t.Fatal("the exact tuple must be familiar, or the negatives below prove nothing")
	}
	cases := []struct {
		name      string
		agent, ns string
		kind      classify.KindRef
		why       string
	}{
		{"another agent", "platform-my-project", testNS, deployKn, "trust is per-agent; 06 §4.2 says 'for this agent'"},
		{"another namespace", testAgent, "team-y", deployKn, "the same shape in a namespace it has never touched is novel"},
		{"another kind", testAgent, testNS, cmKn, "patching a Deployment does not make patching a ConfigMap familiar"},
		{"the same kind in another group", testAgent, testNS, classify.KindRef{Group: "extensions", Kind: "Deployment"}, "group is part of the identity of a kind, not decoration"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if s.Seen(tc.agent, "patch", tc.kind, tc.ns) {
				t.Fatalf("must not be familiar: %s", tc.why)
			}
		})
	}
}

// A cluster-scoped target has an empty namespace on both sides, and must still match. The failure
// mode this catches is a source that treats "" as a wildcard or as unset.
func TestClusterScopedTargetsMatchOnAnEmptyNamespace(t *testing.T) {
	tgt := agentv1alpha1.TargetRef{Group: "rbac.authorization.k8s.io", Version: "v1", Kind: "ClusterRole", Name: "viewer"}
	s, _, _ := sourceOver(t, rec(agentv1alpha1.UndoRestore, "apply", tgt))
	kn := classify.KindRef{Group: "rbac.authorization.k8s.io", Kind: "ClusterRole"}
	if !s.Seen(testAgent, "patch", kn, "") {
		t.Fatal("a cluster-scoped target recorded with no namespace must be familiar when asked with no namespace")
	}
	if s.Seen(testAgent, "patch", kn, testNS) {
		t.Fatal("an empty recorded namespace must not match a named one: that would be a wildcard nobody asked for")
	}
}

// The step's own target is what is keyed, not the record's spec.targets. A record that names three
// targets and plans one step must confer familiarity for that one object's kind only -- otherwise a
// plan-level strategy would be attributed to objects it never touched.
func TestTheStepTargetIsWhatCounts(t *testing.T) {
	r := rec(agentv1alpha1.UndoRestore, "scale", deployTarget())
	// spec.targets deliberately LEADS with something the step never touches, so reading targets[0]
	// instead of the step's target is a different answer and not the same one by luck.
	r.Spec.Targets = []agentv1alpha1.TargetRef{
		{Version: "v1", Kind: "ConfigMap", Namespace: testNS, Name: "settings"},
		{Version: "v1", Kind: "Service", Namespace: testNS, Name: "api"},
		deployTarget(),
	}
	s, _, _ := sourceOver(t, r)
	if !s.Seen(testAgent, "scale", deployKn, testNS) {
		t.Error("the step's own target must be familiar")
	}
	if s.Seen(testAgent, "scale", cmKn, testNS) {
		t.Error("a target the undo plan does not step on must not inherit the plan's class")
	}
	if s.Seen(testAgent, "scale", classify.KindRef{Kind: "Service"}, testNS) {
		t.Error("nor does any other listed target")
	}
}

func TestAMultiStepPlanContributesEveryStep(t *testing.T) {
	r := rec(agentv1alpha1.UndoRestore, "apply", deployTarget())
	r.Spec.Undo.Steps = append(r.Spec.Undo.Steps, agentv1alpha1.UndoStep{
		Op:     "scale",
		Target: agentv1alpha1.TargetRef{Group: "apps", Version: "v1", Kind: "StatefulSet", Namespace: testNS, Name: "db"},
	})
	s, _, _ := sourceOver(t, r)
	if !s.Seen(testAgent, "patch", deployKn, testNS) {
		t.Error("the first step must count")
	}
	if !s.Seen(testAgent, "scale", classify.KindRef{Group: "apps", Kind: "StatefulSet"}, testNS) {
		t.Error("the second step must count too, with its own op and its own target")
	}
}

// --- blindness is the escalating answer ---------------------------------------------------------

// The staleness assertions below are all written against MaxHistoryStaleness, so on their own they
// would hold for any value it took -- including an hour, which is a snapshot no operator would call
// current (LSN-034). Pinned here to literals so the ceiling itself is a claim and not a variable
// both sides of the comparison read.
func TestTheStalenessCeilingIsShortEnoughToMeanSomething(t *testing.T) {
	if MaxHistoryStaleness != 5*time.Minute {
		t.Errorf("MaxHistoryStaleness = %s, want 5m; the ceiling is how long the broker may keep vouching after its reads stop, and every staleness test below is written relative to it",
			MaxHistoryStaleness)
	}
	if DefaultRefreshInterval != 30*time.Second {
		t.Errorf("DefaultRefreshInterval = %s, want 30s", DefaultRefreshInterval)
	}
}

// Every way of not knowing returns false, and false means novel means `+1`. There is no error
// return on classify.ActionHistory, and this is the test that says why one is not needed.
func TestEveryFormOfBlindnessReportsNovel(t *testing.T) {
	t.Run("a nil source", func(t *testing.T) {
		var s *Source
		if s.Seen(testAgent, "patch", deployKn, testNS) {
			t.Fatal("a nil source must report novel rather than panic or vouch")
		}
	})

	t.Run("a source that has never refreshed", func(t *testing.T) {
		clk := &fakeClock{t: testAt}
		s, err := NewSource(SourceConfig{Journal: &fakeJournal{}, Namespace: testNS, Now: clk.now})
		if err != nil {
			t.Fatalf("NewSource: %v", err)
		}
		if s.Seen(testAgent, "patch", deployKn, testNS) {
			t.Fatal("an unrefreshed source has witnessed nothing and must say so")
		}
	})

	t.Run("a snapshot past MaxHistoryStaleness", func(t *testing.T) {
		s, _, clk := sourceOver(t, rec(agentv1alpha1.UndoRestore, "apply", deployTarget()))
		clk.t = testAt.Add(MaxHistoryStaleness)
		if !s.Seen(testAgent, "patch", deployKn, testNS) {
			t.Fatal("exactly at the ceiling is still fresh; the boundary is > and not >=")
		}
		clk.t = testAt.Add(MaxHistoryStaleness + time.Second)
		if s.Seen(testAgent, "patch", deployKn, testNS) {
			t.Fatal("past the ceiling the source must stop vouching -- a dead refresh loop should escalate loudly, not answer from before the incident")
		}
	})

	t.Run("a verb with no 06 §4.3.1 row", func(t *testing.T) {
		s, _, _ := sourceOver(t, rec(agentv1alpha1.UndoRestore, "apply", deployTarget()))
		if s.Seen(testAgent, "exec", deployKn, testNS) {
			t.Fatal("a verb this table does not cover must be novel, not familiar by accident")
		}
	})

	t.Run("a read failure retains the old snapshot rather than vouching for more", func(t *testing.T) {
		j := &fakeJournal{items: []agentv1alpha1.ActionRecord{rec(agentv1alpha1.UndoRestore, "apply", deployTarget())}}
		clk := &fakeClock{t: testAt}
		s, err := NewSource(SourceConfig{Journal: j, Namespace: testNS, Now: clk.now})
		if err != nil {
			t.Fatalf("NewSource: %v", err)
		}
		if err := s.Refresh(context.Background()); err != nil {
			t.Fatalf("Refresh: %v", err)
		}
		j.mu.Lock()
		j.err = errors.New("etcdserver: request timed out")
		j.mu.Unlock()
		if err := s.Refresh(context.Background()); err == nil {
			t.Fatal("a failed List must be reported")
		}
		// Retained: still familiar, and the read time was NOT advanced, so the snapshot ages out on
		// schedule rather than being kept alive by failures.
		if !s.Seen(testAgent, "patch", deployKn, testNS) {
			t.Error("a dropped request must not discard the snapshot; that turns a blip into minutes of escalating everything")
		}
		clk.t = testAt.Add(MaxHistoryStaleness + time.Second)
		if s.Seen(testAgent, "patch", deployKn, testNS) {
			t.Error("a failed refresh must not refresh the clock: a permanently-unreachable API server would keep the snapshot alive forever")
		}
	})
}

// --- the wiring ---------------------------------------------------------------------------------

func TestNewSourceRefusesAnUnusableConfig(t *testing.T) {
	cases := []struct {
		name string
		cfg  SourceConfig
		want string
	}{
		{"no journal", SourceConfig{Namespace: testNS}, "a Journal is required"},
		{"no namespace", SourceConfig{Journal: &fakeJournal{}}, "a Namespace is required"},
		{"a negative interval", SourceConfig{Journal: &fakeJournal{}, Namespace: testNS, RefreshInterval: -time.Second}, "is negative"},
		{
			"an interval that cannot beat staleness",
			SourceConfig{Journal: &fakeJournal{}, Namespace: testNS, RefreshInterval: MaxHistoryStaleness},
			"not shorter than MaxHistoryStaleness",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := NewSource(tc.cfg)
			if err == nil {
				t.Fatal("want a refusal, got nil")
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("got %v, want it to name %q", err, tc.want)
			}
		})
	}

	s, err := NewSource(SourceConfig{Journal: &fakeJournal{}, Namespace: testNS})
	if err != nil {
		t.Fatalf("the default interval must be accepted: %v", err)
	}
	if s.interval != DefaultRefreshInterval {
		t.Fatalf("interval = %s, want the default %s", s.interval, DefaultRefreshInterval)
	}
	if 3*DefaultRefreshInterval > MaxHistoryStaleness {
		t.Fatalf("the default interval %s must fit at least three times into %s, so one lost poll does not start escalating everything",
			DefaultRefreshInterval, MaxHistoryStaleness)
	}
}

// The List is namespaced. A cluster-wide one would be Forbidden to the broker's Role (06 §2.2.1),
// which is an L2 discovery for something an L1 test can pin here.
func TestTheListIsScopedToTheBrokersOwnNamespace(t *testing.T) {
	_, j, _ := sourceOver(t)
	if len(j.opts) != 1 {
		t.Fatalf("want exactly one list option (the namespace), got %d", len(j.opts))
	}
	if got, ok := j.opts[0].(client.InNamespace); !ok || string(got) != testNS {
		t.Fatalf("list option = %#v, want client.InNamespace(%q)", j.opts[0], testNS)
	}
}

func TestRunRefreshesUntilCancelled(t *testing.T) {
	j := &fakeJournal{}
	clk := &fakeClock{t: testAt}
	s, err := NewSource(SourceConfig{Journal: j, Namespace: testNS, RefreshInterval: time.Millisecond, Now: clk.now})
	if err != nil {
		t.Fatalf("NewSource: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { s.Run(ctx); close(done) }()

	deadline := time.After(2 * time.Second)
	for {
		n := j.reads()
		if n >= 2 {
			break
		}
		select {
		case <-deadline:
			t.Fatalf("Run made %d reads in 2s; the ticker is not driving Refresh", n)
		case <-time.After(time.Millisecond):
		}
	}
	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Run did not return on context cancellation")
	}
}

func TestSourceIsAnActionHistory(t *testing.T) {
	// Compile-time in history.go; restated here so the reason survives a refactor that drops the
	// var. If classify.ActionHistory gains a method, this file is where the breakage is explained.
	var v any = &Source{}
	if _, ok := v.(interface {
		Seen(string, string, classify.KindRef, string) bool
	}); !ok {
		t.Fatal("Source no longer satisfies classify.ActionHistory's method set")
	}
}

// --- the hole this closed in the classifier -----------------------------------------------------

// The nil that used to switch the escalation off. Asserted here rather than only in classify,
// because the argument for refusing it is this package's reason for existing.
func TestClassifyRefusesANilHistoryAndAlwaysNovelSaysItDeliberately(t *testing.T) {
	if _, err := classify.New(nil, nil); err == nil {
		t.Fatal("classify.New must refuse a nil ActionHistory: it silently disabled the 06 §4.2 novel-action +1")
	} else if !strings.Contains(err.Error(), "AlwaysNovel") {
		t.Errorf("the refusal must name the deliberate alternative, or a caller will pass something worse: %v", err)
	}
	if _, err := classify.New(nil, classify.AlwaysNovel{}); err != nil {
		t.Fatalf("AlwaysNovel is the supported way to say there is no history: %v", err)
	}
	if (classify.AlwaysNovel{}).Seen(testAgent, "patch", deployKn, testNS) {
		t.Error("AlwaysNovel must report everything as novel; that is the strict reading of the nil it replaces")
	}
}
