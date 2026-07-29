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

package budget

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker"
)

var base = time.Date(2026, 7, 1, 12, 0, 0, 0, time.UTC)

const (
	ns    = "agent-cluster-admin"
	agent = "cluster-admin"
)

var webRef = agentv1alpha1.TargetRef{
	Group: "apps", Version: "v1", Kind: "Deployment", Namespace: "prod", Name: "web",
}

var apiRef = agentv1alpha1.TargetRef{
	Group: "apps", Version: "v1", Kind: "Deployment", Namespace: "prod", Name: "api",
}

// fakeJournal is the API-server seam. `err` faults the List; `calls` proves the ticker.
type fakeJournal struct {
	mu    sync.Mutex
	items []agentv1alpha1.ActionRecord
	err   error
	calls int
}

func (f *fakeJournal) List(_ context.Context, list client.ObjectList, _ ...client.ListOption) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	if f.err != nil {
		return f.err
	}
	out, ok := list.(*agentv1alpha1.ActionRecordList)
	if !ok {
		return fmt.Errorf("fakeJournal: unexpected list type %T", list)
	}
	out.Items = append([]agentv1alpha1.ActionRecord(nil), f.items...)
	return nil
}

func (f *fakeJournal) fault(err error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.err = err
}

func (f *fakeJournal) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.calls
}

// rec builds one journal entry. Options mutate it; the defaults are a completed, chargeable,
// self-initiated routine action by `agent` on `webRef`.
type opt func(*agentv1alpha1.ActionRecord)

func rec(id string, at time.Time, opts ...opt) agentv1alpha1.ActionRecord {
	r := agentv1alpha1.ActionRecord{
		ObjectMeta: metav1.ObjectMeta{Name: "ar-" + id, Namespace: ns},
		Spec: agentv1alpha1.ActionRecordSpec{
			ActionID:       id,
			AgentRef:       agentv1alpha1.AgentObjectRef{Name: agent, Namespace: ns},
			Trigger:        agentv1alpha1.ActionTrigger{Source: "watch"},
			Classification: agentv1alpha1.ActionClassification{Class: agentv1alpha1.RiskRoutine},
			Targets:        []agentv1alpha1.TargetRef{webRef},
		},
		Status: agentv1alpha1.ActionRecordStatus{
			Phase:      agentv1alpha1.PhaseVerified,
			Timestamps: &agentv1alpha1.ActionTimestamps{Submitted: ptrTime(at)},
		},
	}
	for _, o := range opts {
		o(&r)
	}
	return r
}

func ptrTime(t time.Time) *metav1.Time { m := metav1.NewTime(t); return &m }

func trigger(s agentv1alpha1.ActionTriggerSource) opt {
	return func(r *agentv1alpha1.ActionRecord) { r.Spec.Trigger.Source = s }
}
func class(c agentv1alpha1.ActionRiskClass) opt {
	return func(r *agentv1alpha1.ActionRecord) { r.Spec.Classification.Class = c }
}
func phase(p agentv1alpha1.ActionPhase) opt {
	return func(r *agentv1alpha1.ActionRecord) { r.Status.Phase = p }
}
func targets(t ...agentv1alpha1.TargetRef) opt {
	return func(r *agentv1alpha1.ActionRecord) { r.Spec.Targets = t }
}
func dryRun() opt {
	return func(r *agentv1alpha1.ActionRecord) { r.Spec.DryRun = true }
}
func byAgent(name string) opt {
	return func(r *agentv1alpha1.ActionRecord) { r.Spec.AgentRef.Name = name }
}
func noTimestamps(created time.Time) opt {
	return func(r *agentv1alpha1.ActionRecord) {
		r.Status.Timestamps = nil
		r.CreationTimestamp = metav1.NewTime(created)
	}
}

// spend produces n chargeable records, one second apart ending at `last`.
//
// Each one targets a DIFFERENT object, so that a run long enough to exhaust a bucket does not also
// trip the flap brake -- these are the fixtures for the budget tests, and flap has its own.
func spend(n int, last time.Time, opts ...opt) []agentv1alpha1.ActionRecord {
	out := make([]agentv1alpha1.ActionRecord, 0, n)
	for i := range n {
		at := last.Add(-time.Duration(n-1-i) * time.Second)
		o := append([]opt{targets(agentv1alpha1.TargetRef{
			Group: "apps", Version: "v1", Kind: "Deployment", Namespace: "prod",
			Name: fmt.Sprintf("svc-%d", i),
		})}, opts...)
		out = append(out, rec(fmt.Sprintf("spend-%d", i), at, o...))
	}
	return out
}

func newSource(t *testing.T, j Journal) *Source {
	t.Helper()
	s, err := NewSource(SourceConfig{
		Journal: j, Namespace: ns, AgentName: agent,
		Now: func() time.Time { return base },
	})
	if err != nil {
		t.Fatalf("NewSource: %v", err)
	}
	return s
}

// refreshed builds a Source and reads the journal once at `base`, the way startup does.
func refreshed(t *testing.T, items ...agentv1alpha1.ActionRecord) *Source {
	t.Helper()
	return refreshedAt(t, base, items...)
}

// refreshedAt is refreshed with the read happening at `readAt`.
//
// A test that asks about a moment well after `base` needs this rather than a later `q.Now`: a
// snapshot read at `base` and queried an hour later is STALE, and would refuse for that reason
// instead of exercising the window it meant to.
func refreshedAt(t *testing.T, readAt time.Time, items ...agentv1alpha1.ActionRecord) *Source {
	t.Helper()
	s, err := NewSource(SourceConfig{
		Journal: &fakeJournal{items: items}, Namespace: ns, AgentName: agent,
		Now: func() time.Time { return readAt },
	})
	if err != nil {
		t.Fatalf("NewSource: %v", err)
	}
	if err := s.Refresh(context.Background()); err != nil {
		t.Fatalf("Refresh: %v", err)
	}
	return s
}

// query is a BudgetQuery with the defaults most tests want: a stock agent, self-initiated, routine,
// one target, asked at `base`.
func query(opts ...func(*broker.BudgetQuery)) broker.BudgetQuery {
	q := broker.BudgetQuery{
		Agent:   &agentv1alpha1.Agent{},
		Trigger: "watch",
		Class:   agentv1alpha1.RiskRoutine,
		Targets: []agentv1alpha1.TargetRef{webRef},
		Now:     base,
	}
	for _, o := range opts {
		o(&q)
	}
	return q
}

func asked(c agentv1alpha1.ActionRiskClass) func(*broker.BudgetQuery) {
	return func(q *broker.BudgetQuery) { q.Class = c }
}
func from(s agentv1alpha1.ActionTriggerSource) func(*broker.BudgetQuery) {
	return func(q *broker.BudgetQuery) { q.Trigger = s }
}
func at(t time.Time) func(*broker.BudgetQuery) {
	return func(q *broker.BudgetQuery) { q.Now = t }
}
func on(t ...agentv1alpha1.TargetRef) func(*broker.BudgetQuery) {
	return func(q *broker.BudgetQuery) { q.Targets = t }
}

// agentWith builds an Agent whose budget spec is narrowed by `f`.
func agentWith(f func(*agentv1alpha1.InitiativeBudgetSpec)) func(*broker.BudgetQuery) {
	b := &agentv1alpha1.InitiativeBudgetSpec{}
	f(b)
	return func(q *broker.BudgetQuery) {
		q.Agent = &agentv1alpha1.Agent{Spec: agentv1alpha1.AgentSpec{
			Operations: &agentv1alpha1.OperationsSpec{InitiativeBudget: b},
		}}
	}
}

func i32(v int32) *int32 { return &v }

// --- the cold and stale arms: V-PRO-029's core -----------------------------------------------

// TestAColdAccountantRefuses is the heart of V-PRO-029.
//
// broker.BrakeBudget's zero value PERMITS -- alone among the brake's inputs -- because a zero spend
// tally is what an agent that has done nothing looks like. So a source that answered
// honestly-but-emptily before its first read would switch row 7 off for the opening seconds of every
// broker's life. It must refuse instead, and say why.
func TestAColdAccountantRefuses(t *testing.T) {
	s := newSource(t, &fakeJournal{}) // never refreshed

	got := s.Budget(query())
	if !got.Exhausted {
		t.Fatal("a broker that has never read its own journal reported the action within budget; " +
			"row 7 is off for the whole startup window and the zero BrakeBudget permits")
	}
	if got.Detail == "" {
		t.Error("the refusal carries no Detail, so 06 §4.4 row 7's reason reaches nobody")
	}
	if !strings.Contains(got.Detail, "journal") {
		t.Errorf("Detail %q does not say the journal is the thing it could not read; a human reading "+
			"this refusal cannot tell it apart from an agent that genuinely spent its allowance", got.Detail)
	}
}

// TestAColdAccountantNamesTheReadFailure distinguishes never-read from every-read-failed.
func TestAColdAccountantNamesTheReadFailure(t *testing.T) {
	j := &fakeJournal{err: errors.New("etcdserver: request timed out")}
	s := newSource(t, j)
	if err := s.Refresh(context.Background()); err == nil {
		t.Fatal("Refresh reported success against a faulted journal")
	}

	got := s.Budget(query())
	if !got.Exhausted {
		t.Fatal("a broker whose every journal read has failed reported the action within budget")
	}
	if !strings.Contains(got.Detail, "etcdserver") {
		t.Errorf("Detail %q does not carry the underlying read error, so the operator cannot tell a "+
			"broken journal from a slow one", got.Detail)
	}
}

// TestAStaleAccountantRefuses. A snapshot older than MaxJournalStaleness is no longer evidence about
// now, and continuing to count from it is the failure the staleness limit exists for.
func TestAStaleAccountantRefuses(t *testing.T) {
	s := refreshed(t) // empty journal, read at `base`

	// Inside the limit the empty journal permits.
	if got := s.Budget(query(at(base.Add(MaxJournalStaleness - time.Second)))); got.Exhausted {
		t.Fatalf("a snapshot inside the staleness limit refused: %s", got.Detail)
	}
	// Past it, it refuses.
	got := s.Budget(query(at(base.Add(MaxJournalStaleness + time.Second))))
	if !got.Exhausted {
		t.Fatal("a snapshot past MaxJournalStaleness still reported budget from stale counts")
	}
	if !strings.Contains(got.Detail, "ago") {
		t.Errorf("Detail %q does not say how old the count is", got.Detail)
	}
}

// TestRefreshRetainsThePreviousSnapshot. One dropped request must not turn into a broker that
// refuses everything -- the snapshot ages instead, and the staleness limit is what eventually
// refuses.
func TestRefreshRetainsThePreviousSnapshot(t *testing.T) {
	j := &fakeJournal{}
	s := newSource(t, j)
	if err := s.Refresh(context.Background()); err != nil {
		t.Fatalf("Refresh: %v", err)
	}
	j.fault(errors.New("connection reset"))
	if err := s.Refresh(context.Background()); err == nil {
		t.Fatal("Refresh reported success against a faulted journal")
	}

	if got := s.Budget(query(at(base.Add(time.Second)))); got.Exhausted {
		t.Errorf("one dropped journal read discarded the snapshot and refused the action: %s", got.Detail)
	}
	// And it still goes stale on schedule -- retention is not immortality.
	if got := s.Budget(query(at(base.Add(MaxJournalStaleness + time.Second)))); !got.Exhausted {
		t.Error("the retained snapshot never went stale")
	} else if !strings.Contains(got.Detail, "connection reset") {
		t.Errorf("Detail %q does not name the failure that kept it from refreshing", got.Detail)
	}
}

// --- the hourly and daily buckets --------------------------------------------------------------

// TestTheHourlyBucketRefusesTheOneOverTheCap, at the exact boundary in both directions.
func TestTheHourlyBucketRefusesTheOneOverTheCap(t *testing.T) {
	capacity := int(agentv1alpha1.DefaultSelfInitiatedBudget.ElevatedPerHour) // 6

	// One below the cap: the action is the last one that fits.
	s := refreshed(t, spend(capacity-1, base.Add(-time.Minute), class(agentv1alpha1.RiskElevated))...)
	if got := s.Budget(query(asked(agentv1alpha1.RiskElevated))); got.Exhausted {
		t.Fatalf("action %d of %d refused: %s", capacity, capacity, got.Detail)
	}

	// At the cap: this action would be the (cap+1)th.
	s = refreshed(t, spend(capacity, base.Add(-time.Minute), class(agentv1alpha1.RiskElevated))...)
	got := s.Budget(query(asked(agentv1alpha1.RiskElevated)))
	if !got.Exhausted {
		t.Fatalf("action %d exceeded the %d/hour elevated cap and was permitted", capacity+1, capacity)
	}
	if !strings.Contains(got.Detail, fmt.Sprintf("%d/%d", capacity, capacity)) {
		t.Errorf("Detail %q does not report the spend against the cap", got.Detail)
	}
}

// TestTheWindowRolls. Charges that have aged out stop counting -- and the boundary is when the
// OLDEST one ages out, not the top of the hour. See the [Window] doc comment.
func TestTheWindowRolls(t *testing.T) {
	capacity := int(agentv1alpha1.DefaultSelfInitiatedBudget.ElevatedPerHour)
	// A full hour's spend, the oldest of it 50 minutes ago.
	oldest := base.Add(-50 * time.Minute)
	items := make([]agentv1alpha1.ActionRecord, 0, capacity)
	for i := range capacity {
		items = append(items, rec(fmt.Sprintf("e%d", i), oldest.Add(time.Duration(i)*time.Minute), class(agentv1alpha1.RiskElevated)))
	}
	s := refreshed(t, items...)

	got := s.Budget(query(asked(agentv1alpha1.RiskElevated)))
	if !got.Exhausted {
		t.Fatal("a full hourly bucket permitted another action")
	}
	// The boundary is the oldest charge plus the window: 10 minutes from now, not 60.
	want := oldest.Add(HourWindow).UTC().Format(time.RFC3339)
	if !strings.Contains(got.Detail, want) {
		t.Errorf("Detail %q does not name the rolling boundary %s; a clock-aligned window would send "+
			"the agent away for longer than it needs to wait", got.Detail, want)
	}

	// Ten minutes and one second later the oldest charge has aged out and capacity is back. That is
	// the property a tumbling window would get wrong in the other direction: it would have let the
	// agent spend a whole second allowance at the top of the hour.
	rolled := oldest.Add(HourWindow + time.Second)
	if got := refreshedAt(t, rolled, items...).Budget(query(asked(agentv1alpha1.RiskElevated), at(rolled))); got.Exhausted {
		t.Errorf("the window did not roll: %s", got.Detail)
	}
}

// TestTheDailyBucketCountsEveryClassTogether -- 06 §1.1 says `actionsPerDay` is "all classes
// together", so a mix that trips no hourly cap can still exhaust the day.
func TestTheDailyBucketCountsEveryClassTogether(t *testing.T) {
	// A day's worth of routine actions spread out enough that no single hour is full: with
	// actionsPerDay narrowed to 4, four actions across the last four hours exhaust the day while
	// leaving every hourly bucket nearly empty.
	items := []agentv1alpha1.ActionRecord{
		rec("r1", base.Add(-4*time.Hour)),
		rec("e1", base.Add(-3*time.Hour), class(agentv1alpha1.RiskElevated)),
		rec("g1", base.Add(-2*time.Hour), class(agentv1alpha1.RiskGated)),
		rec("r2", base.Add(-time.Hour)),
	}
	s := refreshed(t, items...)
	narrow := agentWith(func(b *agentv1alpha1.InitiativeBudgetSpec) {
		b.SelfInitiated = &agentv1alpha1.BudgetClassSpec{ActionsPerDay: i32(4)}
	})

	got := s.Budget(query(narrow))
	if !got.Exhausted {
		t.Fatal("four actions of three different classes did not exhaust a 4/day budget; the daily " +
			"counter is partitioned by class and 06 §1.1 says it is not")
	}
	if !strings.Contains(got.Detail, "all classes together") {
		t.Errorf("Detail %q does not say the daily counter spans classes", got.Detail)
	}
	// The hourly bucket was never the constraint.
	if strings.Contains(got.Detail, "this hour") {
		t.Errorf("Detail %q blames the hourly bucket for a daily exhaustion", got.Detail)
	}
}

// TestTheOriginsAreSeparateBuckets. Spending the self-initiated allowance must not touch the
// human-requested one, and vice versa -- that separation is the whole point of the 06 §1.1 split.
func TestTheOriginsAreSeparateBuckets(t *testing.T) {
	// Exhaust selfInitiated elevated.
	capacity := int(agentv1alpha1.DefaultSelfInitiatedBudget.ElevatedPerHour)
	s := refreshed(t, spend(capacity, base.Add(-time.Minute), class(agentv1alpha1.RiskElevated))...)

	if got := s.Budget(query(asked(agentv1alpha1.RiskElevated))); !got.Exhausted {
		t.Fatal("the self-initiated bucket did not exhaust")
	}
	if got := s.Budget(query(asked(agentv1alpha1.RiskElevated), from("chat"))); got.Exhausted {
		t.Errorf("an agent that spent its own allowance can no longer do what a human asks: %s", got.Detail)
	}

	// And the reverse: a chat spend must not eat the self-initiated bucket.
	s = refreshed(t, spend(capacity, base.Add(-time.Minute), class(agentv1alpha1.RiskElevated), trigger("chat"))...)
	if got := s.Budget(query(asked(agentv1alpha1.RiskElevated))); got.Exhausted {
		t.Errorf("human-requested spend drew down the self-initiated bucket: %s", got.Detail)
	}
}

// TestAnUnrecognisedTriggerDrawsTheTighterBucket, both when charging and when asking. A trigger
// source added in a later phase must not get the human allowance by omission.
func TestAnUnrecognisedTriggerDrawsTheTighterBucket(t *testing.T) {
	capacity := int(agentv1alpha1.DefaultSelfInitiatedBudget.ElevatedPerHour)
	s := refreshed(t, spend(capacity, base.Add(-time.Minute), class(agentv1alpha1.RiskElevated), trigger("invented-later"))...)

	if got := s.Budget(query(asked(agentv1alpha1.RiskElevated))); !got.Exhausted {
		t.Error("spend from an unrecognised trigger was charged to no bucket the self-initiated query can see")
	}
	if got := s.Budget(query(asked(agentv1alpha1.RiskElevated), from("also-invented"))); !got.Exhausted {
		t.Error("a query from an unrecognised trigger drew on the larger human-requested bucket")
	}
}

// --- what does and does not charge --------------------------------------------------------------

// TestWhatChargesAndWhatDoesNot walks 06 §1.1's accounting rules one phase at a time.
func TestWhatChargesAndWhatDoesNot(t *testing.T) {
	for _, tc := range []struct {
		name    string
		opts    []opt
		charges bool
		why     string
	}{
		{"verified", []opt{phase(agentv1alpha1.PhaseVerified)}, true, "it ran"},
		{"executing", []opt{phase(agentv1alpha1.PhaseExecuting)}, true, "it is running"},
		{"failed", []opt{phase(agentv1alpha1.PhaseFailed)}, true, "it ran and did not work"},
		{"rolled back", []opt{phase(agentv1alpha1.PhaseRolledBack)}, true,
			"06 §1.1 says a rolled-back action decrements, because it ran"},
		{"undone", []opt{phase(agentv1alpha1.PhaseUndone)}, true, "it ran"},
		{"pending", []opt{phase(agentv1alpha1.PhasePending)}, true, "the submission was made"},
		{"pending approval", []opt{phase(agentv1alpha1.PhasePendingApproval), class(agentv1alpha1.RiskGated)}, true,
			"06 §1.1: gatedPerHour counts SUBMISSIONS, not approvals"},
		{"expired", []opt{phase(agentv1alpha1.PhaseExpired), class(agentv1alpha1.RiskGated)}, true,
			"an unlimited stream of gated submissions nobody approves is still a stream of submissions"},
		{"rejected", []opt{phase(agentv1alpha1.PhaseRejected)}, false, "06 §1.1: it never executed"},
		{"dry run", []opt{phase(agentv1alpha1.PhaseDryRun), dryRun()}, false, "06 §1.1: a rehearsal is not a spend"},
		{"forbidden", []opt{class(agentv1alpha1.RiskForbidden)}, false,
			"06 §1.1: a forbidden action is refused by row 3 and never reaches a bucket"},
		{"another agent", []opt{byAgent("platform")}, false,
			"the journal has no agent-name label, so the client-side filter is the only thing keeping " +
				"one agent from spending another's allowance"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			// Narrow the daily budget to 1 so a single charge is visible as a refusal.
			narrow := agentWith(func(b *agentv1alpha1.InitiativeBudgetSpec) {
				b.SelfInitiated = &agentv1alpha1.BudgetClassSpec{ActionsPerDay: i32(1)}
			})
			s := refreshed(t, rec("x", base.Add(-time.Minute), tc.opts...))

			got := s.Budget(query(narrow))
			if got.Exhausted != tc.charges {
				verb := map[bool]string{true: "charged", false: "did not charge"}
				t.Errorf("a %s record %s the bucket; %s", tc.name, verb[!tc.charges], tc.why)
			}
		})
	}
}

// TestUndoIsNeverRefusedForBudget. 06 §1.1: undo is exempt from the hourly buckets, because the one
// thing an agent must always be able to do is put something back.
func TestUndoIsNeverRefusedForBudget(t *testing.T) {
	// Exhaust the human-requested buckets undo would otherwise draw on, hourly and daily both.
	capacity := int(agentv1alpha1.DefaultHumanRequestedBudget.RoutinePerHour)
	s := refreshed(t, spend(capacity, base.Add(-time.Minute), trigger("chat"))...)

	if got := s.Budget(query(from("chat"))); !got.Exhausted {
		t.Fatal("the human-requested hourly bucket did not exhaust")
	}
	if got := s.Budget(query(from(agentv1alpha1.ActionTriggerUndo))); got.Exhausted {
		t.Errorf("an undo was refused for budget: %s; 06 §1.1 exempts it, and an agent that cannot "+
			"reverse itself is worse than one that cannot act", got.Detail)
	}

	// The same at the daily cap.
	narrow := agentWith(func(b *agentv1alpha1.InitiativeBudgetSpec) {
		b.HumanRequested = &agentv1alpha1.BudgetClassSpec{ActionsPerDay: i32(1)}
	})
	if got := s.Budget(query(from(agentv1alpha1.ActionTriggerUndo), narrow)); got.Exhausted {
		t.Errorf("an undo was refused against the daily cap: %s", got.Detail)
	}
}

// TestUndoIsStillSubjectToFlap. The carve-out is budget-only: 05 §1.5 lists flap and budget as
// separate controls, and an undo that is itself part of an oscillation is exactly the case a human
// needs to see.
func TestUndoIsStillSubjectToFlap(t *testing.T) {
	threshold := int(agentv1alpha1.DefaultFlapThreshold)
	items := make([]agentv1alpha1.ActionRecord, 0, threshold)
	for i := range threshold {
		items = append(items, rec(fmt.Sprintf("f%d", i), base.Add(-time.Duration(i+1)*time.Minute)))
	}
	s := refreshed(t, items...)

	got := s.Budget(query(from(agentv1alpha1.ActionTriggerUndo)))
	if !got.FlapBreached {
		t.Error("an undo on a flapping target was permitted; the budget exemption swallowed the flap check")
	}
	if got.Exhausted {
		t.Errorf("the undo was reported as budget-exhausted rather than flapping: %s", got.Detail)
	}
}

// --- flap -----------------------------------------------------------------------------------

// TestFlapCountsThisActionToo. 04 §4.2 says "applied more than N times", and the action being
// decided is the Nth+1. With the default threshold of 3, three prior applications are allowed and
// the fourth is refused.
func TestFlapCountsThisActionToo(t *testing.T) {
	threshold := int(agentv1alpha1.DefaultFlapThreshold) // 3

	// Two priors: this is the third application, and three is not "more than three".
	items := make([]agentv1alpha1.ActionRecord, 0, threshold)
	for i := range threshold - 1 {
		items = append(items, rec(fmt.Sprintf("f%d", i), base.Add(-time.Duration(i+1)*time.Minute)))
	}
	if got := refreshed(t, items...).Budget(query()); got.FlapBreached {
		t.Fatalf("application %d of a %d threshold tripped the flap brake: %s", threshold, threshold, got.Detail)
	}

	// Three priors: this is the fourth, which is more than three.
	items = append(items, rec("f-last", base.Add(-time.Duration(threshold)*time.Minute)))
	got := refreshed(t, items...).Budget(query())
	if !got.FlapBreached {
		t.Fatalf("application %d exceeded a %d threshold and was permitted", threshold+1, threshold)
	}
	if !strings.Contains(got.Detail, "prod/web") {
		t.Errorf("Detail %q does not name the target that is flapping", got.Detail)
	}
}

// TestFlapKeysOnTheTargetAlone is the decision recorded at [flapKey], and it is the one place this
// package is deliberately STRICTER than 04 §4.2's literal `(target, intent)` wording.
//
// Intent is free-text model prose. idempotency.go already excludes it because "a retry that reworded
// itself -- which is what an LLM does on retry -- computed a different key and executed twice". A
// flap detector keyed on intent has the same defect: an agent that rewords its way around the
// threshold never trips it.
func TestFlapKeysOnTheTargetAlone(t *testing.T) {
	threshold := int(agentv1alpha1.DefaultFlapThreshold)
	items := make([]agentv1alpha1.ActionRecord, 0, threshold)
	for i := range threshold {
		r := rec(fmt.Sprintf("f%d", i), base.Add(-time.Duration(i+1)*time.Minute))
		// Every one of these is a differently-worded intent aimed at the same object -- the
		// rewording an LLM does on retry.
		r.Spec.Intent = fmt.Sprintf("restart the web deployment (attempt %d)", i)
		items = append(items, r)
	}
	s := refreshed(t, items...)

	if got := s.Budget(query()); !got.FlapBreached {
		t.Error("three differently-worded actions on one target did not trip the flap brake; an agent " +
			"that rephrases itself walks straight through the 04 §4.2 control")
	}
	// A DIFFERENT target is untouched -- the key is the target, not the agent.
	if got := s.Budget(query(on(apiRef))); got.FlapBreached {
		t.Errorf("a flap on prod/web refused an action on prod/api: %s", got.Detail)
	}
}

// TestFlapFiresOnAnyOneOfSeveralTargets. A multi-target envelope is refused if ANY of its objects is
// flapping -- the brake is about the object, and bundling a flapping object with three quiet ones
// must not launder it.
func TestFlapFiresOnAnyOneOfSeveralTargets(t *testing.T) {
	threshold := int(agentv1alpha1.DefaultFlapThreshold)
	items := make([]agentv1alpha1.ActionRecord, 0, threshold)
	for i := range threshold {
		items = append(items, rec(fmt.Sprintf("f%d", i), base.Add(-time.Duration(i+1)*time.Minute)))
	}
	s := refreshed(t, items...)

	if got := s.Budget(query(on(apiRef, webRef))); !got.FlapBreached {
		t.Error("bundling the flapping target with a quiet one laundered it past the flap brake")
	}
}

// TestFlapWindowRolls -- applications older than the window stop counting, and the window is
// operator-tunable, which is the remedy for the over-firing this key's strictness can cause.
func TestFlapWindowRolls(t *testing.T) {
	threshold := int(agentv1alpha1.DefaultFlapThreshold)
	items := make([]agentv1alpha1.ActionRecord, 0, threshold)
	for i := range threshold {
		items = append(items, rec(fmt.Sprintf("f%d", i), base.Add(-time.Duration(i+1)*time.Minute)))
	}
	later := base.Add(time.Hour)
	s := refreshedAt(t, later, items...)

	// An hour later every application has aged out of the 30-minute default window.
	if got := s.Budget(query(at(later))); got.FlapBreached {
		t.Errorf("applications outside the flap window still counted: %s", got.Detail)
	}
	// But a widened window still sees them.
	wide := agentWith(func(b *agentv1alpha1.InitiativeBudgetSpec) {
		b.FlapWindow = &metav1.Duration{Duration: 3 * time.Hour}
	})
	if got := s.Budget(query(at(later), wide)); !got.FlapBreached {
		t.Error("a widened flap window did not see applications inside it")
	}
}

// TestFlapCountsOnlyAppliedRecords. A gated submission sitting in PendingApproval has drawn down a
// bucket and touched nothing, so it cannot be evidence that an agent is fighting with something over
// an object.
func TestFlapCountsOnlyAppliedRecords(t *testing.T) {
	for _, p := range []agentv1alpha1.ActionPhase{
		agentv1alpha1.PhasePending,
		agentv1alpha1.PhasePendingApproval,
		agentv1alpha1.PhaseRejected,
		agentv1alpha1.PhaseExpired,
		agentv1alpha1.PhaseDryRun,
	} {
		t.Run(string(p), func(t *testing.T) {
			threshold := int(agentv1alpha1.DefaultFlapThreshold)
			items := make([]agentv1alpha1.ActionRecord, 0, threshold+1)
			for i := range threshold + 1 {
				opts := []opt{phase(p)}
				if p == agentv1alpha1.PhaseDryRun {
					opts = append(opts, dryRun())
				}
				items = append(items, rec(fmt.Sprintf("f%d", i), base.Add(-time.Duration(i+1)*time.Minute), opts...))
			}
			if got := refreshed(t, items...).Budget(query()); got.FlapBreached {
				t.Errorf("%d %s records tripped the flap brake; none of them reached the target", threshold+1, p)
			}
		})
	}
}

// TestFlapCannotFireDuringPhaseNine is a fact about this phase, recorded so that nobody reads the
// dry-run exclusion as a bug.
//
// The whole of Phase 9 is dryRun, and [applied] excludes dry runs, so the flap brake counts nothing
// until execution is wired in T7c-3d-iv. That is correct: a rehearsal did not touch the object, and
// a flap detector that counted rehearsals would refuse real work on the strength of work that never
// happened. When this test starts failing, execution has landed.
func TestFlapCannotFireDuringPhaseNine(t *testing.T) {
	items := make([]agentv1alpha1.ActionRecord, 0, 10)
	for i := range 10 {
		items = append(items, rec(fmt.Sprintf("d%d", i), base.Add(-time.Duration(i+1)*time.Minute),
			phase(agentv1alpha1.PhaseDryRun), dryRun()))
	}
	s := refreshed(t, items...)
	got := s.Budget(query())
	if got.FlapBreached || got.Exhausted {
		t.Errorf("ten dry runs against one target produced %+v; in a dry-run-only phase the accountant "+
			"must count nothing", got)
	}
}

// --- the fold ---------------------------------------------------------------------------------

// TestChargeTimeIsSubmission, with the creationTimestamp fallback beneath it.
func TestChargeTimeIsSubmission(t *testing.T) {
	// A record whose submission is 90 minutes ago but which only finished a minute ago. Charged at
	// submission, it is outside the hour; charged at completion, it is inside -- and a long-running
	// action would be held in the window past the hour it was actually spent in.
	r := rec("slow", base.Add(-90*time.Minute))
	r.Status.Timestamps.ExecutionStarted = ptrTime(base.Add(-2 * time.Minute))
	narrow := agentWith(func(b *agentv1alpha1.InitiativeBudgetSpec) {
		b.SelfInitiated = &agentv1alpha1.BudgetClassSpec{RoutinePerHour: i32(1)}
	})
	if got := refreshed(t, r).Budget(query(narrow)); got.Exhausted {
		t.Errorf("an action submitted 90 minutes ago still occupies the hourly bucket: %s", got.Detail)
	}

	// With no timestamps at all, creationTimestamp stands in -- the record is created at submission.
	r = rec("nots", time.Time{}, noTimestamps(base.Add(-time.Minute)))
	if got := refreshed(t, r).Budget(query(narrow)); !got.Exhausted {
		t.Error("a record with no status timestamps was not charged; creationTimestamp is the fallback " +
			"and dropping the charge would let an unstamped record spend for free")
	}
}

// TestTheFoldDropsRecordsOlderThanTheLongestWindow. Not a behaviour test so much as a bound: the
// snapshot must not grow without limit as the journal does.
func TestTheFoldDropsRecordsOlderThanTheLongestWindow(t *testing.T) {
	s := newSource(t, &fakeJournal{items: []agentv1alpha1.ActionRecord{
		rec("recent", base.Add(-time.Hour)),
		rec("ancient", base.Add(-DayWindow-time.Minute)),
	}})
	if err := s.Refresh(context.Background()); err != nil {
		t.Fatalf("Refresh: %v", err)
	}
	if n := len(s.snap.charges); n != 1 {
		t.Errorf("the snapshot holds %d charges, want 1: a record older than the 24h window can affect "+
			"no answer and must not be retained", n)
	}
}

// --- lifecycle ---------------------------------------------------------------------------------

// TestNewSourceRefusesAnUnusableConfig.
func TestNewSourceRefusesAnUnusableConfig(t *testing.T) {
	ok := SourceConfig{Journal: &fakeJournal{}, Namespace: ns, AgentName: agent}

	for _, tc := range []struct {
		name string
		cfg  SourceConfig
		want string
	}{
		{"no journal", SourceConfig{Namespace: ns, AgentName: agent}, "Journal"},
		{"no namespace", SourceConfig{Journal: &fakeJournal{}, AgentName: agent}, "Namespace"},
		{"no agent name", SourceConfig{Journal: &fakeJournal{}, Namespace: ns}, "AgentName"},
		{"negative interval", withInterval(ok, -time.Second), "negative"},
		{"interval at the staleness limit", withInterval(ok, MaxJournalStaleness), "MaxJournalStaleness"},
		{"interval past the staleness limit", withInterval(ok, time.Hour), "MaxJournalStaleness"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, err := NewSource(tc.cfg)
			if err == nil {
				t.Fatalf("NewSource accepted a config with %s", tc.name)
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Errorf("error %q does not mention %q", err, tc.want)
			}
		})
	}

	if _, err := NewSource(ok); err != nil {
		t.Fatalf("NewSource rejected a valid config: %v", err)
	}
}

func withInterval(cfg SourceConfig, d time.Duration) SourceConfig {
	cfg.RefreshInterval = d
	return cfg
}

// TestTheRefreshIntervalFitsInsideTheStalenessLimit -- one lost poll must not start refusing
// actions, and two consecutive lost polls must.
func TestTheRefreshIntervalFitsInsideTheStalenessLimit(t *testing.T) {
	if DefaultRefreshInterval >= MaxJournalStaleness {
		t.Fatalf("the default interval %s is not shorter than the %s staleness limit",
			DefaultRefreshInterval, MaxJournalStaleness)
	}
	if 2*DefaultRefreshInterval >= MaxJournalStaleness {
		t.Errorf("a single lost poll (%s) puts the snapshot within one interval of the %s staleness "+
			"limit; one dropped request would start refusing actions",
			DefaultRefreshInterval, MaxJournalStaleness)
	}
}

// TestRunRefreshesUntilCancelled, and does not stop on an error -- the next poll may well succeed,
// and stopping converts a transient failure into a permanent one the moment staleness elapses.
func TestRunRefreshesUntilCancelled(t *testing.T) {
	j := &fakeJournal{err: errors.New("transient")}
	s, err := NewSource(SourceConfig{
		Journal: j, Namespace: ns, AgentName: agent,
		RefreshInterval: time.Millisecond,
		Now:             func() time.Time { return base },
	})
	if err != nil {
		t.Fatalf("NewSource: %v", err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); s.Run(ctx) }()

	// Let it fail several times, then clear the fault and let it recover.
	waitFor(t, func() bool { return j.count() >= 3 }, "three attempted reads")
	j.fault(nil)
	waitFor(t, func() bool { return s.Budget(query()).Detail == "" }, "recovery after a run of failures")

	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Run did not return after its context was cancelled")
	}
}

func waitFor(t *testing.T, cond func() bool, what string) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

// TestSourceIsAnAccountant. The compile-time assertion is in the package, but a broker wired to a
// Source that does not satisfy the interface is the failure this pins in a place a reader looks.
func TestSourceIsAnAccountant(t *testing.T) {
	var a broker.Accountant = refreshed(t)
	if got := a.Budget(query()); got.Exhausted || got.FlapBreached {
		t.Errorf("an empty journal produced %+v, want a permit", got)
	}
}

// TestBudgetUsesItsOwnClockWhenTheQueryHasNone. A zero Now must not be read as the epoch, which
// would make every snapshot infinitely stale and refuse everything.
func TestBudgetUsesItsOwnClockWhenTheQueryHasNone(t *testing.T) {
	s := refreshed(t)
	q := query()
	q.Now = time.Time{}
	if got := s.Budget(q); got.Exhausted {
		t.Errorf("a query with no Now was refused: %s; the zero time read as the epoch would make "+
			"every snapshot 2026 years stale", got.Detail)
	}
}

// TestTheQueryClockWinsOverTheSourceClock. The fallback above is a fallback, not the rule: when the
// caller says when, that is the instant every window is measured from.
//
// This needs its own test because the two clocks hold the same value in every other fixture here,
// which makes "ignore q.Now and use the source's clock" a mutation the rest of the suite cannot see
// -- both readings agree everywhere else. So the fixture is built at the boundary: the oldest charge
// ages out of the hour ten seconds after the source's clock and ten seconds before the query's.
func TestTheQueryClockWinsOverTheSourceClock(t *testing.T) {
	capacity := int(agentv1alpha1.DefaultSelfInitiatedBudget.ElevatedPerHour)

	// A full hour's elevated spend whose OLDEST charge sits ten seconds inside the window as the
	// source's clock reads it. The source believes it is `base`, and read the journal then.
	last := base.Add(-HourWindow + time.Duration(capacity)*time.Second)
	s := refreshed(t, spend(capacity, last, class(agentv1alpha1.RiskElevated))...)

	// At the source's own instant the bucket is full.
	if got := s.Budget(query(asked(agentv1alpha1.RiskElevated))); !got.Exhausted {
		t.Fatalf("the full hourly bucket permitted an action: %+v", got)
	}

	// Twenty seconds later -- comfortably inside MaxJournalStaleness, so the snapshot itself is still
	// good -- the oldest charge has aged out and capacity is back. Answering from the source's clock
	// instead would still refuse, and would go on refusing for as long as the ticker's own `Now` said
	// so, which is a refusal the caller cannot reason about.
	if got := s.Budget(query(asked(agentv1alpha1.RiskElevated), at(base.Add(20*time.Second)))); got.Exhausted {
		t.Errorf("the window did not roll for a query twenty seconds later: %s; every bucket was "+
			"measured from the source's clock rather than the caller's", got.Detail)
	}
}
