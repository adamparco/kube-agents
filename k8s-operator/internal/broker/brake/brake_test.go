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

package brake_test

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/brake"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/pipeline"
)

const (
	testNS    = "kubeagents-team-x"
	testAgent = "team-x-agent"
)

// fixtureNow is fixed so every staleness assertion is about arithmetic, never about wall time.
var fixtureNow = time.Date(2026, 7, 29, 12, 0, 0, 0, time.UTC)

func newScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	s := runtime.NewScheme()
	if err := agentv1alpha1.AddToScheme(s); err != nil {
		t.Fatalf("add scheme: %v", err)
	}
	return s
}

func agentFixture(rosterRef *agentv1alpha1.RosterRef) *agentv1alpha1.Agent {
	return &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: testAgent, Namespace: testNS},
		Spec: agentv1alpha1.AgentSpec{
			Scope: &agentv1alpha1.ScopeSpec{
				ProjectID:   "proj",
				ClusterName: "cluster-a",
				Namespace:   "team-x",
			},
			Operations: &agentv1alpha1.OperationsSpec{ApprovalRosterRef: rosterRef},
		},
	}
}

func rosterFixture(ns, name string) *agentv1alpha1.ApprovalRoster {
	return &agentv1alpha1.ApprovalRoster{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns},
		Spec: agentv1alpha1.ApprovalRosterSpec{
			Approvers: []agentv1alpha1.Approver{{Platform: "k8s", ID: "alice@example.com"}},
		},
	}
}

// clock is a hand-wound clock. Every test that cares about aging moves it explicitly, so a test
// that forgot to move it fails by not aging rather than by flaking on a slow machine.
type clock struct{ t time.Time }

func (c *clock) now() time.Time      { return c.t }
func (c *clock) add(d time.Duration) { c.t = c.t.Add(d) }

// counting wraps a client and counts reads, so the cache can be asserted on rather than assumed.
type counting struct {
	client.Client
	gets, lists int
}

func (c *counting) Get(ctx context.Context, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
	c.gets++
	return c.Client.Get(ctx, key, obj, opts...)
}

func (c *counting) List(ctx context.Context, list client.ObjectList, opts ...client.ListOption) error {
	c.lists++
	return c.Client.List(ctx, list, opts...)
}

// harness is one Source plus the knobs every test turns.
type harness struct {
	src     *brake.Source
	clk     *clock
	reader  *counting
	journal *counting
}

// errInjector is set per-test to fail a specific read. Returning nil means "let it through".
type errInjector struct {
	onGet  func(obj client.Object) error
	onList func(list client.ObjectList) error
}

func newHarness(t *testing.T, inj errInjector, objs ...client.Object) *harness {
	t.Helper()
	s := newScheme(t)

	funcs := interceptor.Funcs{
		Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			if inj.onGet != nil {
				if err := inj.onGet(obj); err != nil {
					return err
				}
			}
			return c.Get(ctx, key, obj, opts...)
		},
		List: func(ctx context.Context, c client.WithWatch, list client.ObjectList, opts ...client.ListOption) error {
			if inj.onList != nil {
				if err := inj.onList(list); err != nil {
					return err
				}
			}
			return c.List(ctx, list, opts...)
		},
	}

	base := fake.NewClientBuilder().WithScheme(s).WithObjects(objs...).WithInterceptorFuncs(funcs).Build()
	reader := &counting{Client: base}
	jrnl := &counting{Client: base}
	clk := &clock{t: fixtureNow}

	src, err := brake.NewSource(brake.SourceConfig{
		Reader:     reader,
		Journal:    jrnl,
		Accountant: brake.Unaccounted{},
		AgentName:  testAgent,
		Namespace:  testNS,
		Now:        clk.now,
	})
	if err != nil {
		t.Fatalf("NewSource: %v", err)
	}
	return &harness{src: src, clk: clk, reader: reader, journal: jrnl}
}

// --- construction -------------------------------------------------------------------------------

// TestNewSourceRefusesEveryUnusableWiring. Each of these would produce a Source that answers
// confidently and wrongly, and every one of them fails in the permitting direction for at least one
// row -- which is why they are startup errors rather than defaults.
func TestNewSourceRefusesEveryUnusableWiring(t *testing.T) {
	ok := brake.SourceConfig{
		Reader:     &counting{Client: fake.NewClientBuilder().WithScheme(newScheme(t)).Build()},
		Journal:    &counting{Client: fake.NewClientBuilder().WithScheme(newScheme(t)).Build()},
		Accountant: brake.Unaccounted{},
		AgentName:  testAgent,
		Namespace:  testNS,
	}

	cases := []struct {
		name string
		want string
		mut  func(*brake.SourceConfig)
	}{
		{"nil reader", "a Reader is required", func(c *brake.SourceConfig) { c.Reader = nil }},
		{"nil journal", "a Journal is required", func(c *brake.SourceConfig) { c.Journal = nil }},
		{"nil accountant", "an Accountant is required", func(c *brake.SourceConfig) { c.Accountant = nil }},
		{"empty agent name", "an AgentName is required", func(c *brake.SourceConfig) { c.AgentName = "" }},
		{"empty namespace", "a Namespace is required", func(c *brake.SourceConfig) { c.Namespace = "" }},
		{"negative ttl", "is negative", func(c *brake.SourceConfig) { c.CacheTTL = -time.Second }},
		{
			"ttl at the staleness ceiling",
			"not shorter than broker.MaxFreezeStaleness",
			func(c *brake.SourceConfig) { c.CacheTTL = broker.MaxFreezeStaleness },
		},
		{
			"ttl past the staleness ceiling",
			"not shorter than broker.MaxFreezeStaleness",
			func(c *brake.SourceConfig) { c.CacheTTL = broker.MaxFreezeStaleness + time.Second },
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg := ok
			tc.mut(&cfg)
			_, err := brake.NewSource(cfg)
			if err == nil {
				t.Fatalf("NewSource accepted %s", tc.name)
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error %q does not mention %q", err, tc.want)
			}
		})
	}

	if _, err := brake.NewSource(ok); err != nil {
		t.Fatalf("NewSource rejected a good config: %v", err)
	}
}

// TestTheAccountantMustBeSpelledNotForgotten is the LSN-031 guard in test form. A nil Accountant is
// the one omission that would leave row 7 switched off while everything else looked wired, because
// the zero BrakeBudget permits. Unaccounted{} makes the hole a word in the source.
func TestTheAccountantMustBeSpelledNotForgotten(t *testing.T) {
	cfg := brake.SourceConfig{
		Reader:    &counting{Client: fake.NewClientBuilder().WithScheme(newScheme(t)).Build()},
		Journal:   &counting{Client: fake.NewClientBuilder().WithScheme(newScheme(t)).Build()},
		AgentName: testAgent,
		Namespace: testNS,
	}
	if _, err := brake.NewSource(cfg); err == nil {
		t.Fatal("a Source with no Accountant was accepted; row 7 would be silently unenforceable")
	}

	cfg.Accountant = brake.Unaccounted{}
	if _, err := brake.NewSource(cfg); err != nil {
		t.Fatalf("Unaccounted{} was rejected: %v", err)
	}

	if got := (brake.Unaccounted{}).Budget(context.Background(), nil); got != (broker.BrakeBudget{}) {
		t.Fatalf("Unaccounted returned %+v, want the zero budget", got)
	}
}

// --- the healthy read ---------------------------------------------------------------------------

// TestObserveGathersAllFourInputs. The baseline: everything readable produces a view in which every
// row's input is present and affirmative, and broker.Decide allows.
func TestObserveGathersAllFourInputs(t *testing.T) {
	h := newHarness(t, errInjector{},
		agentFixture(&agentv1alpha1.RosterRef{Name: "team-x-roster"}),
		rosterFixture(testNS, "team-x-roster"),
	)

	v := h.src.Observe(context.Background())

	if v.Agent == nil {
		t.Fatal("Agent is nil on a healthy read; row 2 would refuse")
	}
	if v.Agent.Name != testAgent {
		t.Fatalf("Agent is %q, want %q", v.Agent.Name, testAgent)
	}
	if v.Freezes == nil {
		t.Fatal("Freezes is nil on a healthy read; row 1 would refuse")
	}
	if !v.Freezes.ObservedAt.Equal(fixtureNow) {
		t.Fatalf("ObservedAt is %s, want the read instant %s", v.Freezes.ObservedAt, fixtureNow)
	}
	if v.Roster == nil {
		t.Fatal("Roster is nil though the reference resolves; row 6 would park every gated action")
	}
	if v.Journal != broker.BrakeOK {
		t.Fatalf("Journal is %s, want ok", v.Journal)
	}
	if v.Budget != (broker.BrakeBudget{}) {
		t.Fatalf("Budget is %+v, want the zero budget from Unaccounted", v.Budget)
	}

	if d := decideAt(v, fixtureNow); !d.Allowed() {
		t.Fatalf("a fully readable view refused: %s / %s", d.Rule, d.Detail)
	}
}

// decideAt runs the real brake over a view, so these tests assert what the BROKER will do rather
// than what the struct contains. A view that looks right and decides wrong is the failure mode a
// field-by-field assertion cannot see.
func decideAt(v pipeline.BrakeView, now time.Time) broker.BrakeDecision {
	return broker.Decide(broker.BrakeInputs{
		Stage:     broker.StageGate,
		Now:       now,
		Agent:     v.Agent,
		Scope:     scopeOf(v.Agent),
		Freezes:   v.Freezes,
		Journal:   v.Journal,
		UndoPlan:  broker.BrakeOK,
		Roster:    v.Roster,
		Budget:    v.Budget,
		Contested: broker.NewContestedIndex(),
		Class:     agentv1alpha1.RiskRoutine,
	})
}

func scopeOf(a *agentv1alpha1.Agent) *agentv1alpha1.ScopeSpec {
	if a == nil {
		return nil
	}
	return a.Spec.Scope
}

// --- each read fails on its own -------------------------------------------------------------------

// TestOneFailedReadDoesNotBlindTheOthers. The rows are independent and the view must say so: an
// unreadable Agent must not also erase the freeze list. Reporting the wrong incident sends whoever
// is paged to the wrong place, which is the argument broker.Decide's own ORDER comment makes.
func TestOneFailedReadDoesNotBlindTheOthers(t *testing.T) {
	boom := errors.New("apiserver said no")

	t.Run("agent unreadable, freezes still seen", func(t *testing.T) {
		h := newHarness(t, errInjector{onGet: func(obj client.Object) error {
			if _, ok := obj.(*agentv1alpha1.Agent); ok {
				return boom
			}
			return nil
		}}, agentFixture(nil))

		v := h.src.Observe(context.Background())
		if v.Agent != nil {
			t.Fatal("Agent survived a failed Get")
		}
		if v.Freezes == nil {
			t.Fatal("the freeze list was erased by an unrelated Agent failure")
		}
		if v.Journal != broker.BrakeOK {
			t.Fatalf("Journal is %s; the journal probe was collateral damage", v.Journal)
		}
		if d := decideAt(v, fixtureNow); d.Rule != broker.BrakeRuleAgentUnreadable {
			t.Fatalf("rule is %q, want agent-unreadable", d.Rule)
		}
	})

	t.Run("freezes unreadable, agent still seen", func(t *testing.T) {
		h := newHarness(t, errInjector{onList: func(list client.ObjectList) error {
			if _, ok := list.(*agentv1alpha1.FleetFreezeList); ok {
				return boom
			}
			return nil
		}}, agentFixture(nil))

		v := h.src.Observe(context.Background())
		if v.Freezes != nil {
			t.Fatal("Freezes survived a failed List")
		}
		if v.Agent == nil {
			t.Fatal("the Agent was erased by an unrelated freeze failure")
		}
		if d := decideAt(v, fixtureNow); d.Rule != broker.BrakeRuleFreezeUnreadable {
			t.Fatalf("rule is %q, want freeze-unreadable", d.Rule)
		}
	})

	t.Run("journal unreachable, everything else seen", func(t *testing.T) {
		h := newHarness(t, errInjector{onList: func(list client.ObjectList) error {
			if _, ok := list.(*agentv1alpha1.ActionRecordList); ok {
				return boom
			}
			return nil
		}}, agentFixture(nil))

		v := h.src.Observe(context.Background())
		if v.Journal != broker.BrakeFailed {
			t.Fatalf("Journal is %s, want failed", v.Journal)
		}
		if v.Agent == nil || v.Freezes == nil {
			t.Fatal("a journal failure erased an unrelated read")
		}
		d := decideAt(v, fixtureNow)
		if d.Rule != broker.BrakeRuleJournalUnreachable {
			t.Fatalf("rule is %q, want journal-unreachable", d.Rule)
		}
		if !d.AutoPause {
			t.Fatal("row 3 did not ask for an auto-pause")
		}
	})
}

// TestTheJournalProbeHasNoStalenessTolerance. Row 1 gets a 30 s window because 06 §4.4 grants one
// ("API error, OR a cache stale beyond 30 s"); row 3 says only "cannot reach the journal store". A
// failed probe must refuse on the spot, not ride out the window the other row was given.
func TestTheJournalProbeHasNoStalenessTolerance(t *testing.T) {
	fail := false
	h := newHarness(t, errInjector{onList: func(list client.ObjectList) error {
		if _, ok := list.(*agentv1alpha1.ActionRecordList); ok && fail {
			return errors.New("journal down")
		}
		return nil
	}}, agentFixture(nil))

	if v := h.src.Observe(context.Background()); v.Journal != broker.BrakeOK {
		t.Fatalf("Journal is %s before the fault", v.Journal)
	}

	fail = true
	h.clk.add(brake.DefaultCacheTTL) // force a refresh, but stay far inside MaxFreezeStaleness
	v := h.src.Observe(context.Background())

	if v.Journal != broker.BrakeFailed {
		t.Fatalf("Journal is %s one TTL after the fault; the probe was given a tolerance row 3 does not have", v.Journal)
	}
	if v.Agent == nil {
		t.Fatal("the Agent aged out inside the staleness window")
	}
}

// --- the roster's three states ---------------------------------------------------------------------

// TestTheRosterHasThreeStatesNotTwo. "No reference configured" and "a reference that dangles" are
// both a nil roster and both are answers -- row 6 parks. "The Get failed" is not an answer, and
// clearing the roster on it would park gated actions for a reason that is not row 6.
func TestTheRosterHasThreeStatesNotTwo(t *testing.T) {
	t.Run("no reference is an answer", func(t *testing.T) {
		h := newHarness(t, errInjector{}, agentFixture(nil))
		if v := h.src.Observe(context.Background()); v.Roster != nil {
			t.Fatal("a roster appeared with no reference configured")
		}
	})

	t.Run("a dangling reference is an answer, not an error", func(t *testing.T) {
		h := newHarness(t, errInjector{}, agentFixture(&agentv1alpha1.RosterRef{Name: "missing"}))
		if err := h.src.Refresh(context.Background()); err != nil {
			t.Fatalf("a dangling roster reference was reported as an error: %v", err)
		}
		v := h.src.Observe(context.Background())
		if v.Roster != nil {
			t.Fatal("a roster resolved from a dangling reference")
		}
		// The gate is what matters: a missing roster parks a gated action, never approves it.
		d := broker.Decide(broker.BrakeInputs{
			Stage: broker.StageGate, Now: fixtureNow,
			Agent: v.Agent, Scope: scopeOf(v.Agent), Freezes: v.Freezes, Journal: v.Journal,
			UndoPlan: broker.BrakeOK, Roster: v.Roster, Contested: broker.NewContestedIndex(),
			Class: agentv1alpha1.RiskGated,
		})
		if d.Effect != broker.BrakePark {
			t.Fatalf("effect is %q, want Park -- a gated action with no roster must never be allowed", d.Effect)
		}
	})

	t.Run("a failed Get retains the last good roster", func(t *testing.T) {
		fail := false
		h := newHarness(t, errInjector{onGet: func(obj client.Object) error {
			if _, ok := obj.(*agentv1alpha1.ApprovalRoster); ok && fail {
				return errors.New("apiserver said no")
			}
			return nil
		}},
			agentFixture(&agentv1alpha1.RosterRef{Name: "team-x-roster"}),
			rosterFixture(testNS, "team-x-roster"),
		)

		if v := h.src.Observe(context.Background()); v.Roster == nil {
			t.Fatal("the roster did not resolve before the fault")
		}

		fail = true
		h.clk.add(brake.DefaultCacheTTL)
		if v := h.src.Observe(context.Background()); v.Roster == nil {
			t.Fatal("a transient roster Get failure cleared the roster; that parks gated actions for a reason that is not row 6")
		}
	})

	t.Run("an explicit namespace on the reference is honoured", func(t *testing.T) {
		h := newHarness(t, errInjector{},
			agentFixture(&agentv1alpha1.RosterRef{Name: "shared", Namespace: "kubeagents-system"}),
			rosterFixture("kubeagents-system", "shared"),
		)
		v := h.src.Observe(context.Background())
		if v.Roster == nil {
			t.Fatal("a cross-namespace roster reference did not resolve")
		}
		if v.Roster.Namespace != "kubeagents-system" {
			t.Fatalf("roster namespace is %q, want kubeagents-system", v.Roster.Namespace)
		}
	})
}

// --- staleness ------------------------------------------------------------------------------------

// TestAFailingRefreshAgesIntoARefusal is the package's central claim. Nothing here notices that the
// reads have stopped working; the stamped ObservedAt simply stops advancing, and broker.Decide's own
// MaxFreezeStaleness test fires. The assertion is on both sides of the boundary, because a check
// that only proves the refusal cannot tell a working staleness rule from a source that refuses
// always.
func TestAFailingRefreshAgesIntoARefusal(t *testing.T) {
	fail := false
	h := newHarness(t, errInjector{
		onGet: func(client.Object) error {
			if fail {
				return errors.New("apiserver down")
			}
			return nil
		},
		onList: func(list client.ObjectList) error {
			if _, ok := list.(*agentv1alpha1.FleetFreezeList); ok && fail {
				return errors.New("apiserver down")
			}
			return nil
		},
	}, agentFixture(nil))

	if d := decideAt(h.src.Observe(context.Background()), h.clk.t); !d.Allowed() {
		t.Fatalf("refused while healthy: %s", d.Rule)
	}

	fail = true

	// Inside the window: the retained view is still good, and the broker still works. This half is
	// the negative control -- without it, a source that refused unconditionally would pass.
	h.clk.add(broker.MaxFreezeStaleness - time.Second)
	if d := decideAt(h.src.Observe(context.Background()), h.clk.t); !d.Allowed() {
		t.Fatalf("refused %s into a %s window: %s / %s",
			broker.MaxFreezeStaleness-time.Second, broker.MaxFreezeStaleness, d.Rule, d.Detail)
	}

	// Past the window: row 1 fires, with nothing in this package having checked a clock.
	h.clk.add(2 * time.Second)
	d := decideAt(h.src.Observe(context.Background()), h.clk.t)
	if d.Allowed() {
		t.Fatal("still allowing past MaxFreezeStaleness with every read failing")
	}
	if d.Rule != broker.BrakeRuleFreezeUnreadable {
		t.Fatalf("rule is %q, want freeze-unreadable", d.Rule)
	}
}

// TestObservedAtIsTheReadInstantNotTheServingInstant. If the served view were stamped with `now`,
// every cached answer would look brand new and MaxFreezeStaleness could never fire -- the cache
// would have quietly disabled row 1. This is the single line that would do it.
func TestObservedAtIsTheReadInstantNotTheServingInstant(t *testing.T) {
	h := newHarness(t, errInjector{}, agentFixture(nil))

	readAt := h.clk.t
	if v := h.src.Observe(context.Background()); v.Freezes == nil {
		t.Fatal("no freeze view")
	}

	// Advance less than the TTL, so this Observe is served from cache without re-reading.
	h.clk.add(brake.DefaultCacheTTL - time.Second)
	v := h.src.Observe(context.Background())
	if v.Freezes == nil {
		t.Fatal("no freeze view from cache")
	}
	if !v.Freezes.ObservedAt.Equal(readAt) {
		t.Fatalf("ObservedAt is %s, want the original read instant %s -- a cached view was restamped", v.Freezes.ObservedAt, readAt)
	}
}

// TestTheAgentAndRosterAgeOutToo. 06 §4.4 states a ceiling only for row 1. Reusing that number for
// the values whose rows state none keeps every retained read bounded by something the spec wrote
// down, and it fails in the refusing direction.
//
// Each subtest fails ONE read and leaves the others working, because that is the only arrangement in
// which the ceiling on this value is the thing being measured. Failing everything at once makes the
// freeze list go stale too, and broker.Decide applies MaxFreezeStaleness to FreezeView.ObservedAt on
// its own -- so row 1 fires first and the refusal proves nothing about the Agent. A first mutation
// sweep of this file had exactly that hole: "the agent is served regardless of age" and "the roster
// is served regardless of age" both SURVIVED, because the only aging test in the file failed every
// read together and was really watching row 1 ([[LSN-035]]).
func TestTheAgentAndRosterAgeOutToo(t *testing.T) {
	rosterRef := &agentv1alpha1.RosterRef{Name: "team-x-roster"}

	t.Run("a Source that never read anything answers with nothing", func(t *testing.T) {
		h := newHarness(t, errInjector{onGet: func(client.Object) error { return errors.New("down") }},
			agentFixture(rosterRef), rosterFixture(testNS, "team-x-roster"))

		if v := h.src.Observe(context.Background()); v.Agent != nil || v.Roster != nil {
			t.Fatal("a Source that never read anything answered with a value")
		}
		if d := decideAt(h.src.Observe(context.Background()), h.clk.t); d.Rule != broker.BrakeRuleAgentUnreadable {
			t.Fatalf("rule is %q, want agent-unreadable", d.Rule)
		}
	})

	t.Run("a retained Agent ages out of the window", func(t *testing.T) {
		fail := false
		h := newHarness(t, errInjector{onGet: func(obj client.Object) error {
			if _, ok := obj.(*agentv1alpha1.Agent); ok && fail {
				return errors.New("apiserver down")
			}
			return nil
		}}, agentFixture(nil))

		if v := h.src.Observe(context.Background()); v.Agent == nil {
			t.Fatal("the healthy read did not populate the Agent")
		}
		fail = true

		// Inside the window: still served. The negative control -- without it a source that dropped
		// the Agent the instant a read failed would pass the half below, and retaining a good read
		// across a transient blip is the whole reason the value is kept at all.
		h.clk.add(broker.MaxFreezeStaleness - time.Second)
		v := h.src.Observe(context.Background())
		if v.Agent == nil {
			t.Fatalf("the Agent was dropped %s into a %s window", broker.MaxFreezeStaleness-time.Second, broker.MaxFreezeStaleness)
		}
		if v.Freezes == nil {
			t.Fatal("the freeze list stopped being re-read; this subtest would then be measuring row 1")
		}

		// Past it: gone, and row 2 fires. The freeze list is still being read successfully, so
		// freeze-unreadable cannot be what refuses here.
		h.clk.add(2 * time.Second)
		v = h.src.Observe(context.Background())
		if v.Agent != nil {
			t.Fatalf("the Agent was still served %s past the %s ceiling", broker.MaxFreezeStaleness+time.Second, broker.MaxFreezeStaleness)
		}
		if v.Freezes == nil {
			t.Fatal("the freeze list went stale; the refusal below would be row 1, not row 2")
		}
		if d := decideAt(v, h.clk.t); d.Rule != broker.BrakeRuleAgentUnreadable {
			t.Fatalf("rule is %q, want agent-unreadable", d.Rule)
		}
	})

	t.Run("a retained roster ages out of the window", func(t *testing.T) {
		fail := false
		h := newHarness(t, errInjector{onGet: func(obj client.Object) error {
			if _, ok := obj.(*agentv1alpha1.ApprovalRoster); ok && fail {
				return errors.New("apiserver down")
			}
			return nil
		}}, agentFixture(rosterRef), rosterFixture(testNS, "team-x-roster"))

		if v := h.src.Observe(context.Background()); v.Roster == nil {
			t.Fatal("the healthy read did not populate the roster")
		}
		fail = true

		h.clk.add(broker.MaxFreezeStaleness - time.Second)
		if v := h.src.Observe(context.Background()); v.Roster == nil {
			t.Fatal("the roster was dropped inside the window; a transient Get failure is not row 6")
		}

		h.clk.add(2 * time.Second)
		v := h.src.Observe(context.Background())
		if v.Roster != nil {
			t.Fatalf("the roster was still served past the %s ceiling", broker.MaxFreezeStaleness)
		}
		// The Agent is still readable, so the gated action parks for the roster and nothing else.
		if v.Agent == nil {
			t.Fatal("the Agent went stale too; the effect below would not be attributable to row 6")
		}
		d := broker.Decide(broker.BrakeInputs{
			Stage: broker.StageGate, Now: h.clk.t,
			Agent: v.Agent, Scope: scopeOf(v.Agent), Freezes: v.Freezes, Journal: v.Journal,
			UndoPlan: broker.BrakeOK, Roster: v.Roster, Contested: broker.NewContestedIndex(),
			Class: agentv1alpha1.RiskGated,
		})
		if d.Effect != broker.BrakePark {
			t.Fatalf("effect is %q, want Park -- an aged-out roster must park, not approve", d.Effect)
		}
	})
}

// TestARosterThatDisappearsIsGoneAtOnce. readRoster's bool is "this read produced an answer", and it
// is what separates "the roster is not there" from "I could not look". Both leave a nil roster in the
// view on a Source that never had one, so the distinction is invisible until a roster that DID
// resolve goes away: an answered nil replaces the retained copy immediately, an unanswered one keeps
// approving gated actions against a roster that no longer exists until the staleness ceiling catches
// up. Thirty seconds of approving against a deleted roster is the fail-open, and it is why the two
// nil-returning branches of readRoster say true and not false.
func TestARosterThatDisappearsIsGoneAtOnce(t *testing.T) {
	ctx := context.Background()
	rosterRef := &agentv1alpha1.RosterRef{Name: "team-x-roster"}

	// Well inside MaxFreezeStaleness, so nothing here can be aging out -- only the answered nil can
	// clear the roster this fast.
	const soon = brake.DefaultCacheTTL

	t.Run("the roster object is deleted", func(t *testing.T) {
		h := newHarness(t, errInjector{}, agentFixture(rosterRef), rosterFixture(testNS, "team-x-roster"))
		if v := h.src.Observe(ctx); v.Roster == nil {
			t.Fatal("the roster did not resolve to begin with")
		}

		if err := h.reader.Delete(ctx, rosterFixture(testNS, "team-x-roster")); err != nil {
			t.Fatalf("delete roster: %v", err)
		}

		h.clk.add(soon)
		if v := h.src.Observe(ctx); v.Roster != nil {
			t.Fatalf("a deleted roster was still served %s later; gated actions would be approved against an object that no longer exists", soon)
		}
	})

	t.Run("the reference is removed from the Agent", func(t *testing.T) {
		h := newHarness(t, errInjector{}, agentFixture(rosterRef), rosterFixture(testNS, "team-x-roster"))
		if v := h.src.Observe(ctx); v.Roster == nil {
			t.Fatal("the roster did not resolve to begin with")
		}

		var a agentv1alpha1.Agent
		if err := h.reader.Get(ctx, client.ObjectKey{Namespace: testNS, Name: testAgent}, &a); err != nil {
			t.Fatalf("get agent: %v", err)
		}
		a.Spec.Operations.ApprovalRosterRef = nil
		if err := h.reader.Update(ctx, &a); err != nil {
			t.Fatalf("update agent: %v", err)
		}

		h.clk.add(soon)
		if v := h.src.Observe(ctx); v.Roster != nil {
			t.Fatalf("the roster survived %s after its reference was removed from the Agent", soon)
		}
	})
}

// --- the cache ---------------------------------------------------------------------------------

// TestTheCacheServesOneSubmissionFromOneRoundOfReads. pipeline.callerScope and pipeline.stepBrake
// both call Observe for the same envelope. Without the cache they would see two different reads,
// and an agent paused between them would have its scope derived from the running CR and its brake
// evaluated against the paused one.
func TestTheCacheServesOneSubmissionFromOneRoundOfReads(t *testing.T) {
	h := newHarness(t, errInjector{}, agentFixture(nil))

	h.src.Observe(context.Background())
	gets, lists := h.reader.gets, h.reader.lists
	jlists := h.journal.lists

	h.src.Observe(context.Background())
	if h.reader.gets != gets || h.reader.lists != lists || h.journal.lists != jlists {
		t.Fatalf("a second Observe inside the TTL re-read: gets %d->%d, lists %d->%d, journal %d->%d",
			gets, h.reader.gets, lists, h.reader.lists, jlists, h.journal.lists)
	}

	h.clk.add(brake.DefaultCacheTTL)
	h.src.Observe(context.Background())
	if h.reader.gets == gets {
		t.Fatal("the cache never expired; a view older than the TTL was served forever")
	}
}

// TestRefreshReportsEveryFailureForStartup. Observe drops the error on purpose -- the view carries
// it -- but startup wants it loud, because an RBAC gap that makes the Agent unreadable becomes
// "refuse everything" at runtime, which is safe and is a terrible way to find out.
func TestRefreshReportsEveryFailureForStartup(t *testing.T) {
	h := newHarness(t, errInjector{
		onGet:  func(client.Object) error { return errors.New("get denied") },
		onList: func(client.ObjectList) error { return errors.New("list denied") },
	}, agentFixture(nil))

	err := h.src.Refresh(context.Background())
	if err == nil {
		t.Fatal("Refresh reported success with every read failing")
	}
	for _, want := range []string{"row 2", "row 1", "row 3"} {
		if !strings.Contains(err.Error(), want) {
			t.Fatalf("the joined error does not name %s: %v", want, err)
		}
	}
}

// TestAFreezeIsCarriedThroughToARefusal. The freeze list is the one read whose CONTENT decides, so
// a source that returned an empty list on a populated cluster would look identical to a healthy one.
func TestAFreezeIsCarriedThroughToARefusal(t *testing.T) {
	freeze := &agentv1alpha1.FleetFreeze{
		ObjectMeta: metav1.ObjectMeta{Name: "incident-42"},
		Spec: agentv1alpha1.FleetFreezeSpec{
			Scope:  agentv1alpha1.FreezeScope{ProjectID: "proj"},
			Reason: "incident 42",
		},
	}
	h := newHarness(t, errInjector{}, agentFixture(nil), freeze)

	v := h.src.Observe(context.Background())
	if len(v.Freezes.Freezes) != 1 {
		t.Fatalf("the view carries %d freezes, want 1", len(v.Freezes.Freezes))
	}
	d := decideAt(v, fixtureNow)
	if d.Rule != broker.BrakeRuleFrozen {
		t.Fatalf("rule is %q, want frozen", d.Rule)
	}
}

// TestAPausedAgentIsCarriedThroughToARefusal, the same property for the Agent read.
func TestAPausedAgentIsCarriedThroughToARefusal(t *testing.T) {
	paused := true
	a := agentFixture(nil)
	a.Spec.Operations.Paused = &paused
	a.Spec.Operations.PauseReason = "operator pulled the brake"

	h := newHarness(t, errInjector{}, a)
	d := decideAt(h.src.Observe(context.Background()), fixtureNow)
	if d.Rule != broker.BrakeRulePaused {
		t.Fatalf("rule is %q, want paused", d.Rule)
	}
	if !strings.Contains(d.Detail, "operator pulled the brake") {
		t.Fatalf("the pause reason did not survive the read: %q", d.Detail)
	}
}
