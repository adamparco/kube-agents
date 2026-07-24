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
	"strings"
	"testing"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// fakeReplier captures each clarify it is asked to deliver, so a test can assert the router asked the
// human which agent it meant (and carried the candidate menu). err, when set, makes the reply fail — the
// gateway must fold that into the audit reason without changing the terminal (clarify) outcome.
type fakeReplier struct {
	calls []replierCall
	err   error
}

type replierCall struct {
	msg Message
	ce  *ClarifyError
}

func (f *fakeReplier) Clarify(_ context.Context, msg Message, ce *ClarifyError) error {
	f.calls = append(f.calls, replierCall{msg: msg, ce: ce})
	return f.err
}

// lastRecord returns the final audit record emitted for a turn (one per turn on the terminal outcome).
func lastRecord(t *testing.T, s *capturingSink) AuditRecord {
	t.Helper()
	if len(s.recs) == 0 {
		t.Fatal("no audit records emitted")
	}
	return s.recs[len(s.recs)-1]
}

// TestGateway_AuditAttributionSurface proves B5's attribution surface (phase-3 B5 / 06 §2b: every turn is
// attributable). A delivered turn records tier + identity + threadID; a sticky follow-up records
// ModeSticky with the bound agent's tier/identity; a clarify (ambiguous @handle OR low-confidence NL)
// records Clarify==true with the right mode, never dispatches, and drives the optional Replier with the
// candidate menu. The Replier is a pure output seam — it makes no access decision, and its failure never
// changes the (deterministic) clarify outcome.
func TestGateway_AuditAttributionSurface(t *testing.T) {
	t.Parallel()

	t.Run("delivered turn records tier, identity, and threadID", func(t *testing.T) {
		idx := NewIndex()
		idx.Upsert(agentCR("cluster-a-agent", "cluster-admin", "proj-x", "cluster-a", "", "topic-ca", []string{"users/alice"}))
		sink := &capturingSink{}
		g := &Gateway{Resolver: NewResolver(), Index: idx, Dispatch: &FakeDispatcher{}, ProjectID: "proj-x", Audit: sink}
		const thread = "spaces/AAA/threads/T1"

		out, err := g.Handle(context.Background(), Message{Text: "@kage /cluster-cluster-a status", Sender: "users/alice", ThreadID: thread})
		if err != nil || !out.Dispatched {
			t.Fatalf("delivered turn = (dispatched=%v err=%v), want dispatched", out.Dispatched, err)
		}
		rec := lastRecord(t, sink)
		if !rec.Dispatched || !rec.Allowed || rec.Clarify {
			t.Fatalf("record flags = (dispatched=%v allowed=%v clarify=%v), want dispatched+allowed, not clarify", rec.Dispatched, rec.Allowed, rec.Clarify)
		}
		if rec.Tier != agentv1alpha1.TierClusterAdmin || rec.Identity != out.Target.Identity || rec.ThreadID != thread {
			t.Errorf("record attribution = (tier=%s identity=%s thread=%s), want cluster-admin / %s / %s",
				rec.Tier, rec.Identity, rec.ThreadID, out.Target.Identity, thread)
		}
	})

	t.Run("delivered turn ties Sender to TraceID and carries it to dispatch (attribution)", func(t *testing.T) {
		// Phase 5 T-A (acceptance d): the per-turn trace id is recorded on the audit record alongside the
		// requester (tying who↔which-turn) AND handed to the dispatcher, which the production dispatcher
		// stamps as kage_trace_id so the agent can echo it as the PR Trace-Id trailer.
		idx := NewIndex()
		idx.Upsert(agentCR("cluster-a-agent", "cluster-admin", "proj-x", "cluster-a", "", "topic-ca", []string{"users/alice"}))
		sink := &capturingSink{}
		fake := &FakeDispatcher{}
		g := &Gateway{Resolver: NewResolver(), Index: idx, Dispatch: fake, ProjectID: "proj-x", Audit: sink}
		const trace = "projects/p/topics/inbound/messages/12345"

		out, err := g.Handle(context.Background(), Message{Text: "@kage /cluster-cluster-a status", Sender: "users/alice", TraceID: trace})
		if err != nil || !out.Dispatched {
			t.Fatalf("delivered turn = (dispatched=%v err=%v), want dispatched", out.Dispatched, err)
		}
		rec := lastRecord(t, sink)
		if rec.TraceID != trace || rec.Sender != "users/alice" {
			t.Errorf("audit tie = (sender=%s traceID=%s), want users/alice / %s", rec.Sender, rec.TraceID, trace)
		}
		sent := fake.Sent()
		if len(sent) != 1 || sent[0].TraceID != trace || sent[0].Sender != "users/alice" {
			t.Errorf("dispatch attribution = %+v, want one delivery with sender users/alice + traceID %s", sent, trace)
		}
	})

	t.Run("sticky follow-up records ModeSticky with tier and identity", func(t *testing.T) {
		idx := NewIndex()
		idx.Upsert(agentCR("team-y-a", "developer-team", "proj-x", "cluster-a", "team-y", "topic-ty", []string{"users/alice"}))
		sink := &capturingSink{}
		// No inferer: a bare follow-up can ONLY route via the thread binding, so ModeSticky is unambiguous.
		g := &Gateway{Resolver: NewResolver(), Index: idx, Dispatch: &FakeDispatcher{}, ProjectID: "proj-x", Audit: sink, Affinity: NewAffinityStore()}
		const thread = "spaces/BBB/threads/T2"

		// Turn 1: explicit slash binds the thread to team-y.
		if _, err := g.Handle(context.Background(), Message{Text: "@kage /devteam-team-y status", Sender: "users/alice", ThreadID: thread}); err != nil {
			t.Fatalf("bind turn errored: %v", err)
		}
		// Turn 2: bare follow-up sticks.
		out, err := g.Handle(context.Background(), Message{Text: "any updates?", Sender: "users/alice", ThreadID: thread})
		if err != nil || out.Resolution.Mode != ModeSticky || !out.Dispatched {
			t.Fatalf("sticky turn = (mode=%s dispatched=%v err=%v), want sticky/dispatched", out.Resolution.Mode, out.Dispatched, err)
		}
		rec := lastRecord(t, sink)
		if rec.Mode != ModeSticky || rec.Tier != agentv1alpha1.TierDeveloperTeam || rec.Identity != out.Target.Identity {
			t.Errorf("sticky record = (mode=%s tier=%s identity=%s), want sticky / developer-team / %s",
				rec.Mode, rec.Tier, rec.Identity, out.Target.Identity)
		}
		if rec.ThreadID != thread || !rec.Dispatched {
			t.Errorf("sticky record thread/dispatched = (%s, %v), want %s / true", rec.ThreadID, rec.Dispatched, thread)
		}
	})

	t.Run("NL clarify records Clarify+ModeInference, no dispatch; Replier gets the candidate menu", func(t *testing.T) {
		idx := NewIndex()
		idx.Upsert(agentCR("cluster-a-agent", "cluster-admin", "proj-x", "cluster-a", "", "topic-ca", []string{"users/alice"}))
		idx.Upsert(agentCR("team-y-a", "developer-team", "proj-x", "cluster-a", "team-y", "topic-ty", []string{"users/alice"}))
		// Near-tie proposals (0.80 vs 0.78, within the 0.10 margin) → the deterministic core clarifies.
		spy := &spyInferer{candidates: []Candidate{
			{Handle: Handle{Tier: agentv1alpha1.TierDeveloperTeam, Leaf: "team-y"}, Confidence: 0.80},
			{Handle: Handle{Tier: agentv1alpha1.TierClusterAdmin, Leaf: "cluster-a"}, Confidence: 0.78},
		}}
		sink := &capturingSink{}
		rep := &fakeReplier{}
		fake := &FakeDispatcher{}
		g := &Gateway{Resolver: WithInferer(spy), Index: idx, Dispatch: fake, ProjectID: "proj-x", Audit: sink, Replier: rep}
		const thread = "spaces/CCC/threads/T3"

		out, err := g.Handle(context.Background(), Message{Text: "do the cluster thing for team y", Sender: "users/alice", ThreadID: thread})
		if !errors.Is(err, ErrClarify) {
			t.Fatalf("err = %v, want ErrClarify", err)
		}
		if out.Clarify == nil || len(out.Clarify.Candidates) != 2 {
			t.Fatalf("out.Clarify = %+v, want 2 candidates", out.Clarify)
		}
		if out.Dispatched || len(fake.Sent()) != 0 {
			t.Errorf("NL clarify dispatched (dispatched=%v sent=%d)", out.Dispatched, len(fake.Sent()))
		}
		rec := lastRecord(t, sink)
		if !rec.Clarify || rec.Mode != ModeInference || rec.Dispatched || rec.ThreadID != thread {
			t.Errorf("clarify record = (clarify=%v mode=%s dispatched=%v thread=%s), want true / inference / false / %s",
				rec.Clarify, rec.Mode, rec.Dispatched, rec.ThreadID, thread)
		}
		// The fake Replier was asked to deliver exactly ONE clarify, in the same thread, whose menu names
		// both candidate handles the router is asking between.
		if len(rep.calls) != 1 {
			t.Fatalf("Replier calls = %d, want 1", len(rep.calls))
		}
		got := rep.calls[0]
		if got.msg.ThreadID != thread || got.ce == nil {
			t.Fatalf("Replier call = (thread=%s ce=%v), want %s with a candidate menu", got.msg.ThreadID, got.ce, thread)
		}
		menu := map[string]bool{}
		for _, c := range got.ce.Candidates {
			menu[c.Handle.Canonical()] = true
		}
		if !menu["@developer-team-team-y"] || !menu["@cluster-admin-cluster-a"] {
			t.Errorf("Replier clarify menu = %v, want both candidate handles named", menu)
		}
	})

	t.Run("ambiguous handle clarify records Clarify with the handle's tier; Replier gets both scopes", func(t *testing.T) {
		idx := NewIndex()
		idx.Upsert(agentCR("team-x-a", "developer-team", "proj-x", "cluster-a", "team-x", "topic-tx-a", []string{"users/alice"}))
		idx.Upsert(agentCR("team-x-b", "developer-team", "proj-x", "cluster-b", "team-x", "topic-tx-b", []string{"users/alice"}))
		sink := &capturingSink{}
		rep := &fakeReplier{}
		fake := &FakeDispatcher{}
		g := &Gateway{Resolver: NewResolver(), Index: idx, Dispatch: fake, ProjectID: "proj-x", Audit: sink, Replier: rep}

		out, err := g.Handle(context.Background(), Message{Text: "@kage /devteam-team-x status", Sender: "users/alice"})
		if !errors.Is(err, ErrClarify) {
			t.Fatalf("err = %v, want ErrClarify", err)
		}
		if out.Clarify == nil || len(out.Clarify.Candidates) != 2 {
			t.Fatalf("out.Clarify = %+v, want 2 candidates", out.Clarify)
		}
		if out.Dispatched || len(fake.Sent()) != 0 {
			t.Errorf("ambiguous-handle clarify dispatched (dispatched=%v sent=%d)", out.Dispatched, len(fake.Sent()))
		}
		rec := lastRecord(t, sink)
		if !rec.Clarify || rec.Mode != ModeSlash || rec.Dispatched {
			t.Errorf("record = (clarify=%v mode=%s dispatched=%v), want true / slash / false", rec.Clarify, rec.Mode, rec.Dispatched)
		}
		// The scope is ambiguous but the TIER is known from the handle — record it for attribution.
		if rec.Tier != agentv1alpha1.TierDeveloperTeam || rec.Handle != "@developer-team-team-x" {
			t.Errorf("record = (tier=%s handle=%s), want developer-team / @developer-team-team-x", rec.Tier, rec.Handle)
		}
		if len(rep.calls) != 1 || rep.calls[0].ce == nil || len(rep.calls[0].ce.Candidates) != 2 {
			t.Fatalf("Replier calls = %+v, want one call with 2 candidates", rep.calls)
		}
	})

	t.Run("nil Replier: a clarify is still audited but nothing is sent", func(t *testing.T) {
		idx := NewIndex()
		idx.Upsert(agentCR("team-x-a", "developer-team", "proj-x", "cluster-a", "team-x", "topic-tx-a", []string{"users/alice"}))
		idx.Upsert(agentCR("team-x-b", "developer-team", "proj-x", "cluster-b", "team-x", "topic-tx-b", []string{"users/alice"}))
		sink := &capturingSink{}
		g := &Gateway{Resolver: NewResolver(), Index: idx, Dispatch: &FakeDispatcher{}, ProjectID: "proj-x", Audit: sink} // Replier nil
		_, err := g.Handle(context.Background(), Message{Text: "@kage /devteam-team-x status", Sender: "users/alice"})
		if !errors.Is(err, ErrClarify) {
			t.Fatalf("err = %v, want ErrClarify", err)
		}
		if rec := lastRecord(t, sink); !rec.Clarify {
			t.Error("clarify not audited when Replier is nil")
		}
	})

	t.Run("Replier failure is folded into the audit reason; the outcome stays a clarify", func(t *testing.T) {
		idx := NewIndex()
		idx.Upsert(agentCR("team-x-a", "developer-team", "proj-x", "cluster-a", "team-x", "topic-tx-a", []string{"users/alice"}))
		idx.Upsert(agentCR("team-x-b", "developer-team", "proj-x", "cluster-b", "team-x", "topic-tx-b", []string{"users/alice"}))
		sink := &capturingSink{}
		rep := &fakeReplier{err: errors.New("chat outbound 503")}
		g := &Gateway{Resolver: NewResolver(), Index: idx, Dispatch: &FakeDispatcher{}, ProjectID: "proj-x", Audit: sink, Replier: rep}

		out, err := g.Handle(context.Background(), Message{Text: "@kage /devteam-team-x status", Sender: "users/alice"})
		if !errors.Is(err, ErrClarify) {
			t.Fatalf("err = %v, want ErrClarify (a failed reply does not change the outcome)", err)
		}
		if out.Clarify == nil {
			t.Error("out.Clarify nil after a failed reply")
		}
		rec := lastRecord(t, sink)
		if !rec.Clarify || !strings.Contains(rec.Reason, "clarify reply failed") || !strings.Contains(rec.Reason, "503") {
			t.Errorf("record reason = %q, want it to fold in the reply failure", rec.Reason)
		}
	})
}
