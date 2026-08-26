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

package cooldown

import (
	"context"
	"errors"
	"fmt"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/verify"
)

var base = time.Date(2026, 7, 1, 12, 0, 0, 0, time.UTC)

const ns = "agent-cluster-admin"

var webRef = agentv1alpha1.TargetRef{
	Group: "apps", Version: "v1", Kind: "Deployment", Namespace: "prod", Name: "web",
}

var apiRef = agentv1alpha1.TargetRef{
	Group: "apps", Version: "v1", Kind: "Deployment", Namespace: "prod", Name: "api",
}

// fakeJournal is the API-server seam. `err` faults the List; `calls` proves the TTL.
type fakeJournal struct {
	items []agentv1alpha1.ActionRecord
	err   error
	calls int
}

func (f *fakeJournal) List(_ context.Context, list client.ObjectList, _ ...client.ListOption) error {
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

// record builds a journal entry for one failed remediation of `targets` at `at`.
func record(id string, phase agentv1alpha1.ActionPhase, at time.Time, targets ...agentv1alpha1.TargetRef) agentv1alpha1.ActionRecord {
	return agentv1alpha1.ActionRecord{
		ObjectMeta: metav1.ObjectMeta{
			Name:              "ar-" + id,
			Namespace:         ns,
			CreationTimestamp: metav1.NewTime(at.Add(-time.Minute)),
		},
		Spec: agentv1alpha1.ActionRecordSpec{ActionID: id, Targets: targets},
		Status: agentv1alpha1.ActionRecordStatus{
			Phase: phase,
			Recovery: &agentv1alpha1.ActionRecovery{
				Rung:        3,
				Transitions: []agentv1alpha1.RecoveryTransition{{At: metav1.NewTime(at), From: 2, To: 3}},
			},
		},
	}
}

func newSource(t *testing.T, j Journal) *Source {
	t.Helper()
	s, err := NewSource(SourceConfig{Journal: j, Namespace: ns, Now: func() time.Time { return base }})
	if err != nil {
		t.Fatalf("NewSource: %v", err)
	}
	return s
}

// TestSourceSurvivesARestart is V-PRO-028 and the whole reason this package exists.
//
// The Source has never had Enter called on it -- it is a brand-new process, the way it would be
// after somebody deleted the broker pod to see if that cleared things up. Everything it knows about
// the quiet period it read back out of the journal.
func TestSourceSurvivesARestart(t *testing.T) {
	ctx := context.Background()
	j := &fakeJournal{items: []agentv1alpha1.ActionRecord{
		record("A1", agentv1alpha1.PhaseRolledBack, base, webRef),
	}}
	s := newSource(t, j)

	active, until, err := s.Active(ctx, verify.TargetKey(webRef), base.Add(time.Minute))
	if err != nil {
		t.Fatalf("Active: %v", err)
	}
	if !active {
		t.Fatal("a fresh broker process reports no cooldown one minute after a rolled-back remediation; " +
			"deleting the pod cleared the 04 §4.2 control")
	}
	if want := base.Add(verify.BaseCooldown); !until.Equal(want) {
		t.Errorf("recovered cooldown expires %s, want %s", until, want)
	}

	// And it still expires on time -- a durable cooldown that never lifts is its own outage.
	if active, _, _ := s.Active(ctx, verify.TargetKey(webRef), until.Add(time.Second)); active {
		t.Error("the recovered cooldown outlived its expiry")
	}
}

// TestSourceAgreesWithMemoryCooldown is the property verify.CooldownSeries exists to make
// assertable: the durable store must reconstruct the SAME quiet period the reference implementation
// computed live. A durable store that answers differently is worse than none, because it looks
// authoritative.
func TestSourceAgreesWithMemoryCooldown(t *testing.T) {
	ctx := context.Background()
	history := []struct {
		id string
		at time.Time
	}{
		{"A1", base},
		{"A2", base.Add(3 * time.Minute)},
		{"A3", base.Add(20 * time.Minute)},
		{"A4", base.Add(90 * time.Minute)},
		// Well past the decay window: both implementations must restart the sequence.
		{"A5", base.Add(verify.CooldownDecay + 4*time.Hour)},
	}

	mem := verify.NewMemoryCooldown()
	items := make([]agentv1alpha1.ActionRecord, 0, len(history))
	for _, h := range history {
		if _, err := mem.Enter(ctx, h.id, verify.TargetKey(webRef), h.at); err != nil {
			t.Fatalf("MemoryCooldown.Enter: %v", err)
		}
		items = append(items, record(h.id, agentv1alpha1.PhaseRolledBack, h.at, webRef))
	}

	s := newSource(t, &fakeJournal{items: items})

	last := history[len(history)-1].at
	wantActive, wantUntil, err := mem.Active(ctx, verify.TargetKey(webRef), last.Add(time.Minute))
	if err != nil {
		t.Fatalf("MemoryCooldown.Active: %v", err)
	}
	gotActive, gotUntil, err := s.Active(ctx, verify.TargetKey(webRef), last.Add(time.Minute))
	if err != nil {
		t.Fatalf("Source.Active: %v", err)
	}
	if gotActive != wantActive || !gotUntil.Equal(wantUntil) {
		t.Errorf("the durable store says (active=%v, until=%s); the reference says (active=%v, until=%s)",
			gotActive, gotUntil, wantActive, wantUntil)
	}
	// Guard the guard: if the history were too short to be interesting both would trivially agree.
	if !wantActive {
		t.Fatal("the fixture history leaves no cooldown active; the agreement above proves nothing")
	}
}

// TestSourceIsIndependentOfListOrder pins the sort in seriesLocked. An API server may return a list
// in any order and a Go map iterates in a new one every time, so an unsorted fold would compute the
// decay against the wrong previous event and answer differently on consecutive identical reads.
func TestSourceIsIndependentOfListOrder(t *testing.T) {
	ctx := context.Background()
	forward := []agentv1alpha1.ActionRecord{
		record("A1", agentv1alpha1.PhaseRolledBack, base, webRef),
		record("A2", agentv1alpha1.PhaseRolledBack, base.Add(verify.CooldownDecay+time.Hour), webRef),
		record("A3", agentv1alpha1.PhaseRolledBack, base.Add(verify.CooldownDecay+2*time.Hour), webRef),
	}
	reversed := []agentv1alpha1.ActionRecord{forward[2], forward[1], forward[0]}
	// Inside the resulting window, or both orders would trivially agree on the zero time.
	at := base.Add(verify.CooldownDecay + 2*time.Hour + time.Minute)

	_, a, err := newSource(t, &fakeJournal{items: forward}).Active(ctx, verify.TargetKey(webRef), at)
	if err != nil {
		t.Fatalf("Active: %v", err)
	}
	_, b, err := newSource(t, &fakeJournal{items: reversed}).Active(ctx, verify.TargetKey(webRef), at)
	if err != nil {
		t.Fatalf("Active: %v", err)
	}
	if !a.Equal(b) {
		t.Errorf("list order changed the answer: %s from a forward list, %s from a reversed one", a, b)
	}
	// The A1 event is outside the decay window, so the answer must be the 2-consecutive one.
	if want := base.Add(verify.CooldownDecay + 2*time.Hour).Add(2 * verify.BaseCooldown); !a.Equal(want) {
		t.Errorf("expiry %s, want %s -- the decay was applied against the wrong previous event", a, want)
	}
}

// TestSourceCoversTheWindowBeforeTheStatusWrite is the overlay's reason to exist.
// verify.Driver.enterCooldown runs inside rollBack, before its caller writes status.phase, so a
// purely derived registry would report "no cooldown" for exactly the interval in which the next
// action arrives.
func TestSourceCoversTheWindowBeforeTheStatusWrite(t *testing.T) {
	ctx := context.Background()
	j := &fakeJournal{} // the record exists but has not reached PhaseRolledBack yet
	s := newSource(t, j)

	until, err := s.Enter(ctx, "A1", verify.TargetKey(webRef), base)
	if err != nil {
		t.Fatalf("Enter: %v", err)
	}
	if want := base.Add(verify.BaseCooldown); !until.Equal(want) {
		t.Errorf("Enter returned %s, want %s", until, want)
	}
	active, _, err := s.Active(ctx, verify.TargetKey(webRef), base.Add(time.Second))
	if err != nil {
		t.Fatalf("Active: %v", err)
	}
	if !active {
		t.Fatal("the target is unprotected between the rollback and the status write")
	}
}

// TestSourceCountsEachActionOnce is why verify.CooldownRegistry.Enter takes an action ID. Once the
// status write lands, the same failure is visible in both the journal and the overlay; summing them
// would double the quiet period every time a rollback caught up with itself.
func TestSourceCountsEachActionOnce(t *testing.T) {
	ctx := context.Background()
	j := &fakeJournal{}
	s := newSource(t, j)

	overlayOnly, err := s.Enter(ctx, "A1", verify.TargetKey(webRef), base)
	if err != nil {
		t.Fatalf("Enter: %v", err)
	}

	// The status write lands and the TTL lapses.
	j.items = []agentv1alpha1.ActionRecord{record("A1", agentv1alpha1.PhaseRolledBack, base, webRef)}
	at := base.Add(DefaultCacheTTL + time.Second)
	_, both, err := s.Active(ctx, verify.TargetKey(webRef), at)
	if err != nil {
		t.Fatalf("Active: %v", err)
	}
	if !both.Equal(overlayOnly) {
		t.Errorf("the same rollback in the journal and the overlay expires at %s, was %s -- it was counted twice",
			both, overlayOnly)
	}

	// Entering the same action again is still one failure.
	again, err := s.Enter(ctx, "A1", verify.TargetKey(webRef), at)
	if err != nil {
		t.Fatalf("Enter: %v", err)
	}
	if !again.Equal(overlayOnly) {
		t.Errorf("re-entering action A1 moved the expiry to %s from %s", again, overlayOnly)
	}
}

// TestSourceRefusesWhenTheJournalIsUnreadable pins the failure direction. Answering "not in
// cooldown" because the read failed hands the agent the permissive answer at precisely the moment
// the cluster is unwell.
func TestSourceRefusesWhenTheJournalIsUnreadable(t *testing.T) {
	ctx := context.Background()
	boom := errors.New("etcdserver: request timed out")
	s := newSource(t, &fakeJournal{err: boom})

	active, _, err := s.Active(ctx, verify.TargetKey(webRef), base)
	if err == nil {
		t.Fatal("Active answered with no error after a failed journal read")
	}
	if active {
		t.Error("Active reported a cooldown it could not see")
	}
	if !errors.Is(err, boom) {
		t.Errorf("the refusal does not name the underlying cause: %v", err)
	}
}

// TestSourceToleratesABlipWithinTheStalenessBound is the negative control on the test above, and the
// same distinction policy.Source draws: one dropped request must not turn into a broker that
// refuses everything.
func TestSourceToleratesABlipWithinTheStalenessBound(t *testing.T) {
	ctx := context.Background()
	j := &fakeJournal{items: []agentv1alpha1.ActionRecord{
		record("A1", agentv1alpha1.PhaseRolledBack, base.Add(-time.Hour), apiRef),
	}}
	s := newSource(t, j)
	if err := s.Refresh(ctx); err != nil {
		t.Fatalf("Refresh: %v", err)
	}

	j.err = errors.New("connection reset by peer")
	// Past the TTL so a read is attempted, well inside MaxJournalStaleness so the snapshot still counts.
	at := base.Add(DefaultCacheTTL + time.Second)
	if _, _, err := s.Active(ctx, verify.TargetKey(webRef), at); err != nil {
		t.Errorf("a single failed read inside the %s staleness bound refused: %v", MaxJournalStaleness, err)
	}
	// Past the bound it must refuse.
	if _, _, err := s.Active(ctx, verify.TargetKey(webRef), base.Add(MaxJournalStaleness+time.Second)); err == nil {
		t.Errorf("a snapshot older than %s was still treated as evidence about now", MaxJournalStaleness)
	}
}

// TestSourceStatesItsOwnCooldownWhenTheJournalIsUnreadable: an active cooldown this process entered
// itself is a fact it knows. Refusing to state it would be strictly worse for the caller.
func TestSourceStatesItsOwnCooldownWhenTheJournalIsUnreadable(t *testing.T) {
	ctx := context.Background()
	j := &fakeJournal{}
	s := newSource(t, j)
	if _, err := s.Enter(ctx, "A1", verify.TargetKey(webRef), base); err != nil {
		t.Fatalf("Enter: %v", err)
	}

	j.err = errors.New("the API server went away")
	active, until, err := s.Active(ctx, verify.TargetKey(webRef), base.Add(MaxJournalStaleness+time.Second))
	if err != nil {
		t.Fatalf("Active refused a cooldown it entered itself: %v", err)
	}
	if !active {
		t.Fatal("the overlay lost a cooldown this process entered")
	}
	if want := base.Add(verify.BaseCooldown); !until.Equal(want) {
		t.Errorf("overlay cooldown expires %s, want %s", until, want)
	}
}

// TestSourceEntersEvenWhenTheJournalIsUnreadable: the overlay write must not be conditional on the
// read. A cooldown skipped because the journal blipped is the exact failure this store prevents.
func TestSourceEntersEvenWhenTheJournalIsUnreadable(t *testing.T) {
	ctx := context.Background()
	s := newSource(t, &fakeJournal{err: errors.New("no")})

	until, err := s.Enter(ctx, "A1", verify.TargetKey(webRef), base)
	if err == nil {
		t.Error("Enter hid a failed journal read from its caller")
	}
	if want := base.Add(verify.BaseCooldown); !until.Equal(want) {
		t.Errorf("Enter returned %s, want %s -- the failure was not recorded", until, want)
	}
	active, _, _ := s.Active(ctx, verify.TargetKey(webRef), base.Add(time.Minute))
	if !active {
		t.Fatal("a rollback during a journal outage bought no quiet period at all")
	}
}

// TestSourceCountsOnlyFailedAndRolledBackPhases. 04 §4.2 is "after a failed or rolled-back
// remediation" -- the phases in which a write was attempted and did not stand. PhaseDryRun matters
// most here: the whole of phase 9 runs in it, and if dry runs earned cooldowns shadow mode would
// silence the fleet.
func TestSourceCountsOnlyFailedAndRolledBackPhases(t *testing.T) {
	ctx := context.Background()
	for _, tc := range []struct {
		phase     agentv1alpha1.ActionPhase
		wantQuiet bool
	}{
		{agentv1alpha1.PhaseRolledBack, true},
		{agentv1alpha1.PhaseFailed, true},
		{agentv1alpha1.PhaseDryRun, false},
		{agentv1alpha1.PhaseVerified, false},
		{agentv1alpha1.PhaseRejected, false},
		{agentv1alpha1.PhaseExpired, false},
		{agentv1alpha1.PhaseUndone, false},
		{agentv1alpha1.PhaseExecuting, false},
	} {
		t.Run(string(tc.phase), func(t *testing.T) {
			s := newSource(t, &fakeJournal{items: []agentv1alpha1.ActionRecord{
				record("A1", tc.phase, base, webRef),
			}})
			active, _, err := s.Active(ctx, verify.TargetKey(webRef), base.Add(time.Minute))
			if err != nil {
				t.Fatalf("Active: %v", err)
			}
			if active != tc.wantQuiet {
				t.Errorf("phase %s gives cooldown=%v, want %v", tc.phase, active, tc.wantQuiet)
			}
		})
	}
}

// TestSourceIsPerTarget: one record's failure must not quiet an unrelated object, and a record
// naming the same target twice is one failure of it.
func TestSourceIsPerTarget(t *testing.T) {
	ctx := context.Background()
	twice := record("A1", agentv1alpha1.PhaseRolledBack, base, webRef, webRef)
	s := newSource(t, &fakeJournal{items: []agentv1alpha1.ActionRecord{twice}})

	_, until, err := s.Active(ctx, verify.TargetKey(webRef), base.Add(time.Minute))
	if err != nil {
		t.Fatalf("Active: %v", err)
	}
	if want := base.Add(verify.BaseCooldown); !until.Equal(want) {
		t.Errorf("a record naming one target twice bought %s of quiet, want %s",
			until.Sub(base), verify.BaseCooldown)
	}

	active, _, err := s.Active(ctx, verify.TargetKey(apiRef), base.Add(time.Minute))
	if err != nil {
		t.Fatalf("Active: %v", err)
	}
	if active {
		t.Error("rolling back one Deployment silenced a different one")
	}
}

// TestSourceFansOutOverEveryTarget: 04 §4.2's cooldown is per target, and an envelope after selector
// fan-out names many. Quieting only the first would leave the rest of a failed batch retryable.
func TestSourceFansOutOverEveryTarget(t *testing.T) {
	ctx := context.Background()
	s := newSource(t, &fakeJournal{items: []agentv1alpha1.ActionRecord{
		record("A1", agentv1alpha1.PhaseRolledBack, base, webRef, apiRef),
	}})
	for _, ref := range []agentv1alpha1.TargetRef{webRef, apiRef} {
		active, _, err := s.Active(ctx, verify.TargetKey(ref), base.Add(time.Minute))
		if err != nil {
			t.Fatalf("Active(%s): %v", verify.TargetKey(ref), err)
		}
		if !active {
			t.Errorf("%s was in a rolled-back batch and is not in cooldown", verify.TargetKey(ref))
		}
	}
}

// TestSourceCachesWithinTheTTL. Step 5 runs on every action; a List per call would put the brake's
// own read on the hot path of everything the fleet does.
func TestSourceCachesWithinTheTTL(t *testing.T) {
	ctx := context.Background()
	j := &fakeJournal{}
	s := newSource(t, j)

	for i := 0; i < 5; i++ {
		if _, _, err := s.Active(ctx, verify.TargetKey(webRef), base.Add(time.Duration(i)*time.Second)); err != nil {
			t.Fatalf("Active: %v", err)
		}
	}
	if j.calls != 1 {
		t.Errorf("five reads inside a %s TTL made %d List calls, want 1", DefaultCacheTTL, j.calls)
	}
	if _, _, err := s.Active(ctx, verify.TargetKey(webRef), base.Add(DefaultCacheTTL+time.Second)); err != nil {
		t.Fatalf("Active: %v", err)
	}
	if j.calls != 2 {
		t.Errorf("a read past the TTL made %d List calls in total, want 2 -- the snapshot never refreshes", j.calls)
	}
}

// TestSourcePrunesTheOverlay. An overlay entry older than CooldownDecay can neither extend `until`
// (bounded by MaxCooldown, 8h) nor raise `consecutive` (the gap exceeds the decay window), so it is
// inert -- and an overlay that only grows is a leak in a process meant to run for months.
func TestSourcePrunesTheOverlay(t *testing.T) {
	ctx := context.Background()
	s := newSource(t, &fakeJournal{})

	for i := 0; i < 3; i++ {
		key := verify.TargetKey(agentv1alpha1.TargetRef{
			Group: "apps", Version: "v1", Kind: "Deployment", Namespace: "prod",
			Name: fmt.Sprintf("web-%d", i),
		})
		if _, err := s.Enter(ctx, fmt.Sprintf("A%d", i), key, base); err != nil {
			t.Fatalf("Enter: %v", err)
		}
	}
	s.mu.Lock()
	before := len(s.pending)
	s.mu.Unlock()
	if before != 3 {
		t.Fatalf("overlay holds %d targets, want 3", before)
	}

	// One read a day and a bit later. Everything above is spent.
	if _, _, err := s.Active(ctx, verify.TargetKey(webRef), base.Add(verify.CooldownDecay+time.Hour)); err != nil {
		t.Fatalf("Active: %v", err)
	}
	s.mu.Lock()
	after := len(s.pending)
	s.mu.Unlock()
	if after != 0 {
		t.Errorf("overlay still holds %d spent targets after %s", after, verify.CooldownDecay)
	}
}

func TestNewSourceRejectsBadWiring(t *testing.T) {
	ok := SourceConfig{Journal: &fakeJournal{}, Namespace: ns}
	for name, mutate := range map[string]func(*SourceConfig){
		"no journal":       func(c *SourceConfig) { c.Journal = nil },
		"no namespace":     func(c *SourceConfig) { c.Namespace = "" },
		"negative ttl":     func(c *SourceConfig) { c.CacheTTL = -time.Second },
		"ttl at the bound": func(c *SourceConfig) { c.CacheTTL = MaxJournalStaleness },
		"ttl past the bound": func(c *SourceConfig) {
			c.CacheTTL = MaxJournalStaleness + time.Second
		},
	} {
		t.Run(name, func(t *testing.T) {
			cfg := ok
			mutate(&cfg)
			if _, err := NewSource(cfg); err == nil {
				t.Errorf("NewSource accepted %s", name)
			}
		})
	}
	if _, err := NewSource(ok); err != nil {
		t.Errorf("NewSource rejected a valid config: %v", err)
	}
}

// TestFailureTimeTakesTheLatestStamp. There is no rolledBackAt field in 06 §4.3, so the derivation
// takes the max of the stamps that exist. Every fallback is earlier than the transition it stands in
// for, and an earlier failure time is an earlier expiry -- the loosening direction -- so a record
// carrying its ladder must never be dated from a weaker stamp.
func TestFailureTimeTakesTheLatestStamp(t *testing.T) {
	rec := record("A1", agentv1alpha1.PhaseRolledBack, base, webRef)
	submitted := metav1.NewTime(base.Add(-30 * time.Second))
	ended := metav1.NewTime(base.Add(-10 * time.Second))
	rec.Status.Timestamps = &agentv1alpha1.ActionTimestamps{Submitted: &submitted, ExecutionEnded: &ended}
	if got := failureTime(&rec); !got.Equal(base) {
		t.Errorf("failureTime = %s, want the ladder transition at %s", got, base)
	}

	// Ladderless: fall back to the latest lifecycle stamp rather than creationTimestamp.
	bare := record("A2", agentv1alpha1.PhaseFailed, base, webRef)
	bare.Status.Recovery = nil
	bare.Status.Timestamps = &agentv1alpha1.ActionTimestamps{Submitted: &submitted, ExecutionEnded: &ended}
	if got := failureTime(&bare); !got.Equal(ended.Time) {
		t.Errorf("failureTime = %s, want the executionEnded stamp at %s", got, ended.Time)
	}

	// Nothing at all: creationTimestamp, which record() sets a minute before `at`.
	naked := record("A3", agentv1alpha1.PhaseFailed, base, webRef)
	naked.Status.Recovery = nil
	if got, want := failureTime(&naked), base.Add(-time.Minute); !got.Equal(want) {
		t.Errorf("failureTime = %s, want creationTimestamp at %s", got, want)
	}
}

// TestSourceSkipsRecordsWithNoActionID. Impossible through admission, and counting one would be the
// double-count the ID-keyed union exists to prevent -- it cannot be matched against the overlay.
func TestSourceSkipsRecordsWithNoActionID(t *testing.T) {
	ctx := context.Background()
	bad := record("A1", agentv1alpha1.PhaseRolledBack, base, webRef)
	bad.Spec.ActionID = ""
	s := newSource(t, &fakeJournal{items: []agentv1alpha1.ActionRecord{bad}})

	active, _, err := s.Active(ctx, verify.TargetKey(webRef), base.Add(time.Minute))
	if err != nil {
		t.Fatalf("Active: %v", err)
	}
	if active {
		t.Error("an ID-less record was folded in; it can never be deduped against the overlay")
	}
}

func TestSourceSatisfiesTheRegistryInterface(t *testing.T) {
	var _ verify.CooldownRegistry = (*Source)(nil)
}
