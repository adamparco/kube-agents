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

package policy

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// fakeLister answers List from a settable set of policies, or fails.
type fakeLister struct {
	items []agentv1alpha1.ChangePolicy
	err   error
	calls int
}

func (f *fakeLister) List(_ context.Context, list client.ObjectList, _ ...client.ListOption) error {
	f.calls++
	if f.err != nil {
		return f.err
	}
	l, ok := list.(*agentv1alpha1.ChangePolicyList)
	if !ok {
		return errors.New("fakeLister: unexpected list type")
	}
	l.Items = append([]agentv1alpha1.ChangePolicy(nil), f.items...)
	return nil
}

type fakeClock struct{ t time.Time }

func (c *fakeClock) now() time.Time          { return c.t }
func (c *fakeClock) advance(d time.Duration) { c.t = c.t.Add(d) }

func newTestSource(t *testing.T, l *fakeLister, clk *fakeClock) *Source {
	t.Helper()
	s, err := NewSource(SourceConfig{Reader: l, Agent: devTeamAgent(), History: nothingSeen{}, Now: clk.now})
	if err != nil {
		t.Fatalf("NewSource: %v", err)
	}
	return s
}

// TestSourceRefusesBeforeItHasEverRead: an unread source is not an unpoliced fleet. This is the
// cold-start half of fail-closed, and it is the one a "retain last good" design cannot cover.
func TestSourceRefusesBeforeItHasEverRead(t *testing.T) {
	s := newTestSource(t, &fakeLister{}, &fakeClock{t: testAt})
	if _, err := s.Current(); err == nil {
		t.Fatal("Current must refuse before the first successful read")
	} else if !strings.Contains(err.Error(), "has not been refreshed") {
		t.Fatalf("the error must say why: %v", err)
	}
}

func TestSourceRefusesWhenTheFirstReadFailed(t *testing.T) {
	l := &fakeLister{err: errors.New("connection refused")}
	s := newTestSource(t, l, &fakeClock{t: testAt})

	if err := s.Refresh(context.Background()); err == nil {
		t.Fatal("Refresh must return the List error")
	}
	_, err := s.Current()
	if err == nil {
		t.Fatal("Current must refuse when no read has ever succeeded")
	}
	if !strings.Contains(err.Error(), "connection refused") {
		t.Fatalf("the refusal must carry the underlying cause: %v", err)
	}
}

// TestSourceRetainsTheLastGoodSetAcrossAFailedRead, and the companion staleness test below, are the
// two halves of the same decision. Discarding on the first failed poll would turn a single dropped
// request into a fleet-wide outage on a resource that almost never changes; never discarding would
// classify against an hour-old policy set, which -- because the classifier maxes over sources -- is
// the loosening direction.
func TestSourceRetainsTheLastGoodSetAcrossAFailedRead(t *testing.T) {
	clk := &fakeClock{t: testAt}
	l := &fakeLister{items: []agentv1alpha1.ChangePolicy{*cp("live", nil, gateDeletes("r"))}}
	s := newTestSource(t, l, clk)

	if err := s.Refresh(context.Background()); err != nil {
		t.Fatalf("first Refresh: %v", err)
	}
	l.err = errors.New("apiserver unavailable")
	clk.advance(DefaultRefreshInterval)
	_ = s.Refresh(context.Background())

	c, err := s.Current()
	if err != nil {
		t.Fatalf("inside the staleness window a failed poll must not refuse: %v", err)
	}
	if c == nil {
		t.Fatal("Current returned no classifier and no error")
	}
	snap, err := s.Snapshot()
	if err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	if got := snap.Names(); len(got) != 1 || got[0] != "live" {
		t.Fatalf("Names = %v, want the last good set [live]", got)
	}
}

func TestSourceRefusesOnceTheLastGoodSetGoesStale(t *testing.T) {
	clk := &fakeClock{t: testAt}
	l := &fakeLister{items: []agentv1alpha1.ChangePolicy{*cp("live", nil, gateDeletes("r"))}}
	s := newTestSource(t, l, clk)

	if err := s.Refresh(context.Background()); err != nil {
		t.Fatalf("first Refresh: %v", err)
	}
	clk.advance(MaxPolicyStaleness)
	if _, err := s.Current(); err != nil {
		t.Fatalf("exactly at the limit must still answer: %v", err)
	}
	clk.advance(time.Second)
	_, err := s.Current()
	if err == nil {
		t.Fatal("past MaxPolicyStaleness the source must refuse; an unreadable policy set is not an empty one")
	}
	if !strings.Contains(err.Error(), "staleness limit") {
		t.Fatalf("the refusal must name the reason: %v", err)
	}
	// And a stale snapshot with a failing poll behind it names both.
	l.err = errors.New("apiserver unavailable")
	_ = s.Refresh(context.Background())
	if _, err := s.Current(); err == nil || !strings.Contains(err.Error(), "apiserver unavailable") {
		t.Fatalf("a stale snapshot must surface the read failure too: %v", err)
	}
}

// TestSourceRecoversAfterASuccessfulRead: the staleness refusal is a condition of the data, not a
// latch. A source that refused permanently after one bad minute would need a restart to recover,
// which is how a transient becomes an outage.
func TestSourceRecoversAfterASuccessfulRead(t *testing.T) {
	clk := &fakeClock{t: testAt}
	l := &fakeLister{err: errors.New("down")}
	s := newTestSource(t, l, clk)

	_ = s.Refresh(context.Background())
	if _, err := s.Current(); err == nil {
		t.Fatal("expected a refusal while down")
	}
	l.err = nil
	l.items = []agentv1alpha1.ChangePolicy{*cp("late", nil, gateDeletes("r"))}
	if err := s.Refresh(context.Background()); err != nil {
		t.Fatalf("Refresh after recovery: %v", err)
	}
	if _, err := s.Current(); err != nil {
		t.Fatalf("Current after recovery: %v", err)
	}
}

func badRule() agentv1alpha1.ChangeRule {
	return agentv1alpha1.ChangeRule{
		ID:     "downgrade-attempt",
		When:   agentv1alpha1.ChangeRuleWhen{Verbs: []agentv1alpha1.ChangeVerb{"delete"}},
		Class:  agentv1alpha1.ChangePolicyClass(agentv1alpha1.RiskRoutine),
		Reason: "this should not load",
	}
}

func TestSourceRefusesToBuildOverABadPolicy(t *testing.T) {
	l := &fakeLister{items: []agentv1alpha1.ChangePolicy{*cp("bad-policy", nil, badRule())}}
	s := newTestSource(t, l, &fakeClock{t: testAt})

	if err := s.Refresh(context.Background()); err == nil {
		t.Fatal("Refresh must fail when a bound policy will not convert")
	}
	if _, err := s.Current(); err == nil || !strings.Contains(err.Error(), "bad-policy") {
		t.Fatalf("Current must refuse and name the policy: %v", err)
	}
}

// TestABadPolicyDiscardsTheSnapshotWhereADroppedReadDoesNot is the distinction the two failure
// classes turn on, asserted from one place so the pair cannot drift apart.
//
// A dropped read is transient and the last good set is worth keeping for a while. A policy that
// will not load is not transient at all -- it will fail every poll until a human edits the object
// -- so aging it out would mean 30 seconds of classifying against a set the broker already knows is
// wrong, and the operator who applied it would learn about it from a delayed timeout rather than at
// once.
func TestABadPolicyDiscardsTheSnapshotWhereADroppedReadDoesNot(t *testing.T) {
	good := []agentv1alpha1.ChangePolicy{*cp("live", nil, gateDeletes("r"))}

	t.Run("a dropped read retains", func(t *testing.T) {
		l := &fakeLister{items: good}
		s := newTestSource(t, l, &fakeClock{t: testAt})
		if err := s.Refresh(context.Background()); err != nil {
			t.Fatalf("Refresh: %v", err)
		}
		l.err = errors.New("connection reset")
		_ = s.Refresh(context.Background())
		if _, err := s.Current(); err != nil {
			t.Fatalf("a dropped read inside the staleness window must not refuse: %v", err)
		}
	})

	t.Run("an unloadable policy discards", func(t *testing.T) {
		l := &fakeLister{items: good}
		s := newTestSource(t, l, &fakeClock{t: testAt})
		if err := s.Refresh(context.Background()); err != nil {
			t.Fatalf("Refresh: %v", err)
		}
		l.items = append(append([]agentv1alpha1.ChangePolicy(nil), good...), *cp("bad-policy", nil, badRule()))
		_ = s.Refresh(context.Background())
		if _, err := s.Current(); err == nil {
			t.Fatal("Current answered from the last good snapshot while an unloadable policy sat in the cluster")
		} else if !strings.Contains(err.Error(), "bad-policy") {
			t.Fatalf("the refusal must name the policy to fix, not report staleness: %v", err)
		}
		// And it recovers the moment the object is fixed -- no restart, no waiting out a timer.
		l.items = good
		if err := s.Refresh(context.Background()); err != nil {
			t.Fatalf("Refresh after the fix: %v", err)
		}
		if _, err := s.Current(); err != nil {
			t.Fatalf("Current after the fix: %v", err)
		}
	})
}

func TestNewSourceRejectsAnIntervalThatCannotBeatStaleness(t *testing.T) {
	_, err := NewSource(SourceConfig{Reader: &fakeLister{}, RefreshInterval: MaxPolicyStaleness})
	if err == nil {
		t.Fatal("a refresh interval at or above MaxPolicyStaleness must be rejected: the source would refuse between successful reads")
	}
	if _, err := NewSource(SourceConfig{Reader: nil}); err == nil {
		t.Fatal("a nil Reader must be rejected")
	}
	s, err := NewSource(SourceConfig{Reader: &fakeLister{}})
	if err != nil {
		t.Fatalf("the default interval must be accepted: %v", err)
	}
	if s.interval != DefaultRefreshInterval {
		t.Fatalf("interval = %s, want the default %s", s.interval, DefaultRefreshInterval)
	}
	if 3*DefaultRefreshInterval > MaxPolicyStaleness {
		t.Fatalf("the default interval %s must fit at least three times into %s so one lost poll does not refuse",
			DefaultRefreshInterval, MaxPolicyStaleness)
	}
}

func TestRunRefreshesUntilCancelled(t *testing.T) {
	clk := &fakeClock{t: testAt}
	l := &fakeLister{}
	s, err := NewSource(SourceConfig{
		Reader: l, Agent: devTeamAgent(), History: nothingSeen{},
		RefreshInterval: time.Millisecond, Now: clk.now,
	})
	if err != nil {
		t.Fatalf("NewSource: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { s.Run(ctx); close(done) }()

	deadline := time.After(5 * time.Second)
	for {
		if _, err := s.Current(); err == nil {
			break
		}
		select {
		case <-deadline:
			cancel()
			t.Fatal("Run never produced a snapshot")
		case <-time.After(time.Millisecond):
		}
	}
	cancel()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("Run did not return after its context was cancelled")
	}
}
