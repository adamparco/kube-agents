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

package router

import (
	"context"
	"errors"
	"testing"
	"time"

	"k8s.io/apimachinery/pkg/types"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// TestMemAffinityStore covers the in-memory binding store in isolation: bind/lookup/drop, empty-input
// no-ops, lazy TTL expiry (lapsed exactly at now >= expires), and TTL refresh on re-bind. The clock is
// injected so expiry is deterministic without sleeping.
func TestMemAffinityStore(t *testing.T) {
	t.Parallel()
	base := time.Unix(1_700_000_000, 0)
	clock := base
	s := newMemAffinityStore(30 * time.Minute)
	s.now = func() time.Time { return clock }

	// Empty inputs are no-ops in both directions.
	s.Bind("", "k")
	s.Bind("thread-1", "")
	if _, ok := s.Lookup(""); ok {
		t.Fatal("empty threadID resolved")
	}
	if _, ok := s.Lookup("thread-1"); ok {
		t.Fatal("bind with empty key stored a binding")
	}

	// Bind then Lookup returns the exact routing key.
	s.Bind("thread-1", "developer-team\x00team-y")
	if k, ok := s.Lookup("thread-1"); !ok || k != "developer-team\x00team-y" {
		t.Fatalf("Lookup = (%q,%v), want the bound key", k, ok)
	}

	// Drop removes it.
	s.Drop("thread-1")
	if _, ok := s.Lookup("thread-1"); ok {
		t.Fatal("binding survived Drop")
	}

	// TTL expiry: a binding lapses exactly when now reaches the expiry instant.
	s.Bind("thread-2", "key-2")
	clock = base.Add(30 * time.Minute) // now == expires ⇒ lapsed
	if _, ok := s.Lookup("thread-2"); ok {
		t.Fatal("binding did not lapse at TTL")
	}

	// Refresh: re-binding before expiry pushes the deadline out.
	clock = base
	s.Bind("thread-3", "key-3") // expires base+30m
	clock = base.Add(20 * time.Minute)
	s.Bind("thread-3", "key-3") // refreshed: expires base+50m
	clock = base.Add(40 * time.Minute)
	if k, ok := s.Lookup("thread-3"); !ok || k != "key-3" {
		t.Fatalf("refreshed binding lapsed early: (%q,%v)", k, ok)
	}
}

// affinityFixture builds a gateway with two allowlisted agents (cluster-a and team-y, both for
// users/alice) plus the given affinity store and optional inferer, returning the index too so a test can
// mutate the live set. When inf is nil the resolver has NO inferer, so a fall-through (unbound/expired/
// stale) surfaces as ErrInferenceUnavailable — a clean signal that a turn was NOT routed stickily.
func affinityFixture(aff AffinityStore, inf Inferer) (*Gateway, *FakeDispatcher, *Index) {
	idx := NewIndex()
	idx.Upsert(agentCR("cluster-a-agent", "cluster-admin", "proj-x", "cluster-a", "", "topic-ca", []string{"users/alice"}))
	idx.Upsert(agentCR("team-y-a", "developer-team", "proj-x", "cluster-a", "team-y", "topic-ty", []string{"users/alice"}))
	r := NewResolver()
	if inf != nil {
		r = WithInferer(inf)
	}
	fake := &FakeDispatcher{}
	g := &Gateway{Resolver: r, Index: idx, Dispatch: fake, ProjectID: "proj-x", Affinity: aff}
	return g, fake, idx
}

// TestGateway_ThreadAffinity is the end-to-end sticky-routing matrix (06 §6 / phase-3 B4): a deterministic
// turn binds the thread; a bare follow-up sticks with ModeSticky and zero inference; an explicit @handle
// rebinds; a bound thread still runs Authorize (a non-allowlisted sender is refused and does NOT refresh
// the binding); and a binding is written ONLY after an authorized dispatch.
func TestGateway_ThreadAffinity(t *testing.T) {
	t.Parallel()

	t.Run("deterministic turn binds; bare follow-up sticks with no inference", func(t *testing.T) {
		// The spy would route to cluster-a if consulted — so ModeSticky + spy.calls==0 both prove it wasn't.
		spy := &spyInferer{candidates: []Candidate{{Handle: Handle{Tier: agentv1alpha1.TierClusterAdmin, Leaf: "cluster-a"}, Confidence: 0.99}}}
		g, fake, _ := affinityFixture(NewAffinityStore(), spy)
		const thread = "spaces/AAA/threads/T1"

		// Turn 1: explicit slash to team-y → dispatched, binds the thread.
		out, err := g.Handle(context.Background(), Message{Text: "@kage /devteam-team-y status", Sender: "users/alice", ThreadID: thread})
		if err != nil || !out.Dispatched || out.Resolution.Mode != ModeSlash {
			t.Fatalf("turn 1 = (mode=%s dispatched=%v err=%v), want slash/dispatched", out.Resolution.Mode, out.Dispatched, err)
		}

		// Turn 2: bare follow-up in the same thread → sticky to team-y, no inference.
		out, err = g.Handle(context.Background(), Message{Text: "any updates?", Sender: "users/alice", ThreadID: thread})
		if err != nil {
			t.Fatalf("sticky follow-up errored: %v", err)
		}
		if out.Resolution.Mode != ModeSticky || out.Target.TopicName != "topic-ty" {
			t.Fatalf("sticky turn = (mode=%s target=%s), want sticky/topic-ty", out.Resolution.Mode, out.Target.TopicName)
		}
		if spy.calls != 0 || g.Resolver.InferenceCalls() != 0 {
			t.Fatalf("sticky follow-up spent inference: spy=%d counter=%d, want 0/0", spy.calls, g.Resolver.InferenceCalls())
		}
		if n := len(fake.Sent()); n != 2 {
			t.Fatalf("dispatch count = %d, want 2 (turn 1 + sticky)", n)
		}
	})

	t.Run("explicit @handle rebinds the thread", func(t *testing.T) {
		g, _, _ := affinityFixture(NewAffinityStore(), nil)
		const thread = "spaces/AAA/threads/T2"

		// Bind to team-y, confirm sticky, then re-address cluster-a and confirm the follow-up sticks to it.
		if _, err := g.Handle(context.Background(), Message{Text: "@kage /devteam-team-y hi", Sender: "users/alice", ThreadID: thread}); err != nil {
			t.Fatalf("bind turn errored: %v", err)
		}
		if out, _ := g.Handle(context.Background(), Message{Text: "and now?", Sender: "users/alice", ThreadID: thread}); out.Target.TopicName != "topic-ty" {
			t.Fatalf("pre-rebind sticky target = %s, want topic-ty", out.Target.TopicName)
		}
		if _, err := g.Handle(context.Background(), Message{Text: "@kage /cluster-cluster-a drain", Sender: "users/alice", ThreadID: thread}); err != nil {
			t.Fatalf("rebind turn errored: %v", err)
		}
		out, err := g.Handle(context.Background(), Message{Text: "status?", Sender: "users/alice", ThreadID: thread})
		if err != nil {
			t.Fatalf("post-rebind sticky errored: %v", err)
		}
		if out.Resolution.Mode != ModeSticky || out.Target.TopicName != "topic-ca" {
			t.Fatalf("post-rebind sticky = (mode=%s target=%s), want sticky/topic-ca", out.Resolution.Mode, out.Target.TopicName)
		}
	})

	t.Run("bound thread still authorizes; non-allowlisted sender refused, binding not refreshed", func(t *testing.T) {
		g, fake, _ := affinityFixture(NewAffinityStore(), nil)
		const thread = "spaces/AAA/threads/T3"

		// alice binds the thread to team-y.
		if _, err := g.Handle(context.Background(), Message{Text: "@kage /devteam-team-y go", Sender: "users/alice", ThreadID: thread}); err != nil {
			t.Fatalf("bind turn errored: %v", err)
		}
		sentAfterBind := len(fake.Sent())

		// mallory posts bare in the bound thread → sticky RESOLVES team-y but Authorize refuses her.
		out, err := g.Handle(context.Background(), Message{Text: "let me in", Sender: "users/mallory", ThreadID: thread})
		if !errors.Is(err, ErrUnauthorized) {
			t.Fatalf("non-allowlisted sticky turn err = %v, want ErrUnauthorized", err)
		}
		if out.Dispatched || len(fake.Sent()) != sentAfterBind {
			t.Fatalf("unauthorized sticky turn dispatched (dispatched=%v sent delta=%d)", out.Dispatched, len(fake.Sent())-sentAfterBind)
		}
		// The refusal must not have refreshed/replaced the binding: alice still sticks to team-y.
		if out, _ := g.Handle(context.Background(), Message{Text: "back to it", Sender: "users/alice", ThreadID: thread}); out.Resolution.Mode != ModeSticky || out.Target.TopicName != "topic-ty" {
			t.Fatalf("binding damaged by refused turn: (mode=%s target=%s), want sticky/topic-ty", out.Resolution.Mode, out.Target.TopicName)
		}
	})

	t.Run("a refused turn writes no binding (bind only after authorized dispatch)", func(t *testing.T) {
		g, fake, _ := affinityFixture(NewAffinityStore(), nil)
		const thread = "spaces/AAA/threads/T4"

		// mallory addresses team-y explicitly but is not allowlisted → refused before dispatch.
		if _, err := g.Handle(context.Background(), Message{Text: "@kage /devteam-team-y hi", Sender: "users/mallory", ThreadID: thread}); !errors.Is(err, ErrUnauthorized) {
			t.Fatalf("mallory turn err = %v, want ErrUnauthorized", err)
		}
		if len(fake.Sent()) != 0 {
			t.Fatalf("refused turn dispatched %d", len(fake.Sent()))
		}
		// No binding should exist: alice's bare follow-up has nothing to stick to and falls through. With no
		// inferer wired that surfaces as ErrInferenceUnavailable — proving the refused turn bound nothing.
		if _, err := g.Handle(context.Background(), Message{Text: "hello?", Sender: "users/alice", ThreadID: thread}); !errors.Is(err, ErrInferenceUnavailable) {
			t.Fatalf("post-refusal bare turn err = %v, want ErrInferenceUnavailable (no binding written)", err)
		}
	})

	t.Run("TTL expiry drops the binding; bare follow-up is no longer sticky", func(t *testing.T) {
		base := time.Unix(1_700_000_000, 0)
		clock := base
		store := newMemAffinityStore(30 * time.Minute)
		store.now = func() time.Time { return clock }
		g, fake, _ := affinityFixture(store, nil)
		const thread = "spaces/AAA/threads/T5"

		if _, err := g.Handle(context.Background(), Message{Text: "@kage /devteam-team-y go", Sender: "users/alice", ThreadID: thread}); err != nil {
			t.Fatalf("bind turn errored: %v", err)
		}
		sentAfterBind := len(fake.Sent())

		clock = base.Add(31 * time.Minute) // past the TTL
		_, err := g.Handle(context.Background(), Message{Text: "still there?", Sender: "users/alice", ThreadID: thread})
		if !errors.Is(err, ErrInferenceUnavailable) {
			t.Fatalf("post-TTL bare turn err = %v, want ErrInferenceUnavailable (binding lapsed, fell through)", err)
		}
		if len(fake.Sent()) != sentAfterBind {
			t.Fatalf("post-TTL turn dispatched to the lapsed target (sent delta=%d)", len(fake.Sent())-sentAfterBind)
		}
	})

	t.Run("stale binding (agent removed) is dropped; turn falls through", func(t *testing.T) {
		g, fake, idx := affinityFixture(NewAffinityStore(), nil)
		const thread = "spaces/AAA/threads/T6"

		if _, err := g.Handle(context.Background(), Message{Text: "@kage /devteam-team-y go", Sender: "users/alice", ThreadID: thread}); err != nil {
			t.Fatalf("bind turn errored: %v", err)
		}
		sentAfterBind := len(fake.Sent())

		// The bound agent is deleted from the live index (informer removed the CR).
		idx.Remove(types.NamespacedName{Namespace: "kubeagents-system", Name: "team-y-a"})

		_, err := g.Handle(context.Background(), Message{Text: "you there?", Sender: "users/alice", ThreadID: thread})
		if !errors.Is(err, ErrInferenceUnavailable) {
			t.Fatalf("stale-binding turn err = %v, want ErrInferenceUnavailable (binding dropped, fell through)", err)
		}
		if len(fake.Sent()) != sentAfterBind {
			t.Fatalf("stale-binding turn dispatched to a departed agent (sent delta=%d)", len(fake.Sent())-sentAfterBind)
		}
	})
}
