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

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/agentindex"
)

// agentCR builds a minimal Agent CR for index tests.
func agentCR(name, tier, project, cluster, ns, topic string, allowed []string) *agentv1alpha1.Agent {
	a := &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "kubeagents-system"},
		Spec: agentv1alpha1.AgentSpec{
			Tier: agentv1alpha1.AgentTier(tier),
			Scope: &agentv1alpha1.ScopeSpec{
				ProjectID:   project,
				ClusterName: cluster,
				Namespace:   ns,
			},
			Integration: &agentv1alpha1.AgentIntegrationSpec{
				GoogleChat: &agentv1alpha1.GoogleChatSpec{
					TopicName:    topic,
					AllowedUsers: allowed,
				},
			},
		},
	}
	return a
}

// capturingSink records audit records for assertion.
type capturingSink struct{ recs []AuditRecord }

func (c *capturingSink) Record(_ context.Context, rec AuditRecord) { c.recs = append(c.recs, rec) }

func TestIndex_UpsertLookupRemove(t *testing.T) {
	t.Parallel()
	idx := NewIndex()

	pa := agentCR("platform-agent", "platform", "proj-x", "", "", "topic-platform", []string{"users/alice"})
	ca := agentCR("cluster-a-agent", "cluster-admin", "proj-x", "cluster-a", "", "topic-ca", []string{"users/bob"})
	idx.Upsert(pa)
	idx.Upsert(ca)
	if got := idx.Len(); got != 2 {
		t.Fatalf("Len = %d, want 2", got)
	}

	// Lookup by the SAME key agentindex produces from the CR (no-drift).
	if tgt, ok := idx.Lookup(agentindex.ScopeIdentity(ca)); !ok {
		t.Fatal("cluster-admin target not found by ScopeIdentity")
	} else if tgt.TopicName != "topic-ca" || tgt.Handle != "@cluster-admin-cluster-a" {
		t.Errorf("cluster-admin target = %+v, want topic-ca / @cluster-admin-cluster-a", tgt)
	}

	// Re-key: change the cluster-admin agent's cluster; the stale key must be evicted, not duplicated.
	caMoved := ca.DeepCopy()
	caMoved.Spec.Scope.ClusterName = "cluster-b"
	oldKey := agentindex.ScopeIdentity(ca)
	idx.Upsert(caMoved)
	if _, ok := idx.Lookup(oldKey); ok {
		t.Error("stale key still resolves after re-key; phantom route leaked")
	}
	if _, ok := idx.Lookup(agentindex.ScopeIdentity(caMoved)); !ok {
		t.Error("new key does not resolve after re-key")
	}
	if got := idx.Len(); got != 2 {
		t.Errorf("Len = %d after re-key, want 2", got)
	}

	// Remove by namespaced name (a delete carries no object).
	idx.Remove(types.NamespacedName{Namespace: "kubeagents-system", Name: "cluster-a-agent"})
	if _, ok := idx.Lookup(agentindex.ScopeIdentity(caMoved)); ok {
		t.Error("target still resolves after Remove")
	}
	if got := idx.Len(); got != 1 {
		t.Errorf("Len = %d after Remove, want 1", got)
	}
}

func TestGateway_HandleAuthorizesBeforeDispatch(t *testing.T) {
	t.Parallel()
	idx := NewIndex()
	// Closed allowlist: only users/alice may reach the cluster-admin agent.
	ca := agentCR("cluster-a-agent", "cluster-admin", "proj-x", "cluster-a", "", "topic-ca", []string{"users/alice"})
	idx.Upsert(ca)

	fake := &FakeDispatcher{}
	sink := &capturingSink{}
	g := &Gateway{Resolver: NewResolver(), Index: idx, Dispatch: fake, ProjectID: "proj-x", Audit: sink}

	// Allowed sender via slash command → dispatched, no inference.
	out, err := g.Handle(context.Background(), Message{Text: "@kage /cluster-cluster-a status", Sender: "users/alice"})
	if err != nil {
		t.Fatalf("allowed turn errored: %v", err)
	}
	if !out.Dispatched || out.Resolution.Mode != ModeSlash {
		t.Fatalf("allowed turn: dispatched=%v mode=%s, want true/slash", out.Dispatched, out.Resolution.Mode)
	}
	if len(fake.Sent()) != 1 || fake.Sent()[0].TopicName != "topic-ca" {
		t.Fatalf("dispatch = %+v, want one send to topic-ca", fake.Sent())
	}

	// Non-allowlisted sender → refused BEFORE dispatch (no new send recorded).
	out, err = g.Handle(context.Background(), Message{Text: "@kage /cluster-cluster-a status", Sender: "users/mallory"})
	if !errors.Is(err, ErrUnauthorized) {
		t.Fatalf("non-allowlisted turn err = %v, want ErrUnauthorized", err)
	}
	if out.Dispatched {
		t.Error("non-allowlisted turn was dispatched")
	}
	if len(fake.Sent()) != 1 {
		t.Errorf("dispatch count = %d after refusal, want still 1 (never dispatched)", len(fake.Sent()))
	}

	// Inference must never have been spent across the whole matrix.
	if n := g.Resolver.InferenceCalls(); n != 0 {
		t.Errorf("InferenceCalls = %d, want 0", n)
	}

	// Audit: the refusal is recorded with allowed=false, dispatched=false.
	var sawRefusal bool
	for _, r := range sink.recs {
		if r.Sender == "users/mallory" && !r.Allowed && !r.Dispatched {
			sawRefusal = true
		}
	}
	if !sawRefusal {
		t.Error("refused-before-dispatch turn was not audited")
	}
}

func TestGateway_ClosedAllowlistFailsClosed(t *testing.T) {
	t.Parallel()
	idx := NewIndex()
	// Empty allowlist: the operator would render ALLOW_ALL for the pod, but the ROUTER must refuse ALL.
	ca := agentCR("cluster-a-agent", "cluster-admin", "proj-x", "cluster-a", "", "topic-ca", nil)
	idx.Upsert(ca)

	fake := &FakeDispatcher{}
	g := &Gateway{Resolver: NewResolver(), Index: idx, Dispatch: fake, ProjectID: "proj-x"}

	_, err := g.Handle(context.Background(), Message{Text: "@kage /cluster-cluster-a status", Sender: "users/alice"})
	if !errors.Is(err, ErrUnauthorized) {
		t.Fatalf("empty-allowlist turn err = %v, want ErrUnauthorized (fail-closed)", err)
	}
	if len(fake.Sent()) != 0 {
		t.Error("fail-closed target still dispatched")
	}
}

func TestGateway_NoSuchTarget(t *testing.T) {
	t.Parallel()
	g := &Gateway{Resolver: NewResolver(), Index: NewIndex(), Dispatch: &FakeDispatcher{}, ProjectID: "proj-x"}
	_, err := g.Handle(context.Background(), Message{Text: "@kage /cluster-ghost status", Sender: "users/alice"})
	if !errors.Is(err, ErrNoSuchTarget) {
		t.Fatalf("undeployed target err = %v, want ErrNoSuchTarget", err)
	}
}

func TestGateway_DeterministicRefusalsNeverDispatch(t *testing.T) {
	t.Parallel()
	idx := NewIndex()
	idx.Upsert(agentCR("cluster-a-agent", "cluster-admin", "proj-x", "cluster-a", "", "topic-ca", []string{"users/alice"}))
	fake := &FakeDispatcher{}
	g := &Gateway{Resolver: NewResolver(), Index: idx, Dispatch: fake, ProjectID: "proj-x"}

	cases := []struct {
		name    string
		msg     Message
		wantErr error
	}{
		{"unaddressed", Message{Text: "hey what's up", Sender: "users/alice"}, ErrInferenceUnavailable},
		{"empty", Message{Text: "", Sender: "users/alice"}, ErrUnaddressed},
		// A dev-team handle now resolves via the index; with no dev-team CR indexed it is a no-such-target
		// refusal (not the old deferred sentinel), and still never dispatches.
		{"developer-team no such target", Message{Text: "@kage /devteam-teamns status", Sender: "users/alice"}, ErrNoSuchTarget},
		{"unknown tier", Message{Text: "@kage /wat-foo status", Sender: "users/alice"}, ErrUnknownTier},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := g.Handle(context.Background(), tc.msg)
			if !errors.Is(err, tc.wantErr) {
				t.Fatalf("err = %v, want %v", err, tc.wantErr)
			}
		})
	}
	if len(fake.Sent()) != 0 {
		t.Errorf("deterministic refusals dispatched %d messages, want 0", len(fake.Sent()))
	}
	if n := g.Resolver.InferenceCalls(); n != 0 {
		t.Errorf("InferenceCalls = %d, want 0", n)
	}
}

// TestIndex_LookupHandle covers the per-tier resolution mechanism: platform/cluster-admin via the exact
// RouteKey, developer-team via the byTierLeaf secondary index (0 / 1 / multi-cluster >1), the
// missing-project refusal, and slice-aware re-key eviction on a dev-team scope edit.
func TestIndex_LookupHandle(t *testing.T) {
	t.Parallel()
	const project = "proj-x"
	idx := NewIndex()
	idx.Upsert(agentCR("platform-agent", "platform", project, "", "", "topic-platform", []string{"users/alice"}))
	idx.Upsert(agentCR("cluster-a-agent", "cluster-admin", project, "cluster-a", "", "topic-ca", []string{"users/alice"}))
	// team-x exists in cluster-a AND cluster-b — the multi-cluster ambiguity the clarify path handles.
	idx.Upsert(agentCR("team-x-a", "developer-team", project, "cluster-a", "team-x", "topic-tx-a", []string{"users/alice"}))
	idx.Upsert(agentCR("team-x-b", "developer-team", project, "cluster-b", "team-x", "topic-tx-b", []string{"users/alice"}))
	// team-y exists in exactly one cluster — the unambiguous single-cluster case (Kind).
	idx.Upsert(agentCR("team-y-a", "developer-team", project, "cluster-a", "team-y", "topic-ty", []string{"users/alice"}))

	t.Run("platform resolves to its single occupant", func(t *testing.T) {
		got, err := idx.LookupHandle(Handle{Tier: agentv1alpha1.TierPlatform, Leaf: project}, project)
		if err != nil || len(got) != 1 || got[0].Handle != "@platform-"+project {
			t.Fatalf("platform lookup = %+v err=%v, want one @platform-%s", got, err, project)
		}
	})

	t.Run("cluster-admin resolves via the exact RouteKey", func(t *testing.T) {
		got, err := idx.LookupHandle(Handle{Tier: agentv1alpha1.TierClusterAdmin, Leaf: "cluster-a"}, project)
		if err != nil || len(got) != 1 || got[0].Handle != "@cluster-admin-cluster-a" {
			t.Fatalf("cluster-admin lookup = %+v err=%v, want one @cluster-admin-cluster-a", got, err)
		}
	})

	t.Run("cluster-admin without project context is refused", func(t *testing.T) {
		if _, err := idx.LookupHandle(Handle{Tier: agentv1alpha1.TierClusterAdmin, Leaf: "cluster-a"}, ""); !errors.Is(err, ErrMissingProjectContext) {
			t.Fatalf("err = %v, want ErrMissingProjectContext", err)
		}
	})

	t.Run("developer-team single-cluster resolves unambiguously", func(t *testing.T) {
		got, err := idx.LookupHandle(Handle{Tier: agentv1alpha1.TierDeveloperTeam, Leaf: "team-y"}, project)
		if err != nil || len(got) != 1 || got[0].TopicName != "topic-ty" {
			t.Fatalf("team-y lookup = %+v err=%v, want one topic-ty", got, err)
		}
	})

	t.Run("developer-team multi-cluster returns all matches (gateway clarifies)", func(t *testing.T) {
		got, err := idx.LookupHandle(Handle{Tier: agentv1alpha1.TierDeveloperTeam, Leaf: "team-x"}, project)
		if err != nil || len(got) != 2 {
			t.Fatalf("team-x lookup = %+v err=%v, want two matches (cluster-a + cluster-b)", got, err)
		}
	})

	t.Run("developer-team with no CR yields zero matches", func(t *testing.T) {
		got, err := idx.LookupHandle(Handle{Tier: agentv1alpha1.TierDeveloperTeam, Leaf: "ghost"}, project)
		if err != nil || len(got) != 0 {
			t.Fatalf("ghost lookup = %+v err=%v, want zero matches", got, err)
		}
	})

	t.Run("scope edit evicts the stale byTierLeaf slot", func(t *testing.T) {
		// Move team-y-a from namespace team-y to team-z: the old (tier, team-y) slot must lose it, the
		// new (tier, team-z) slot must gain it — no phantom left behind in the secondary index.
		moved := agentCR("team-y-a", "developer-team", project, "cluster-a", "team-z", "topic-ty", []string{"users/alice"})
		idx.Upsert(moved)
		if got, _ := idx.LookupHandle(Handle{Tier: agentv1alpha1.TierDeveloperTeam, Leaf: "team-y"}, project); len(got) != 0 {
			t.Errorf("stale (tier, team-y) slot still resolves after re-key: %+v", got)
		}
		if got, _ := idx.LookupHandle(Handle{Tier: agentv1alpha1.TierDeveloperTeam, Leaf: "team-z"}, project); len(got) != 1 {
			t.Errorf("re-keyed agent does not resolve under (tier, team-z): %+v", got)
		}
	})
}

// TestGateway_DeveloperTeamRouting proves the end-to-end dev-team path the old ErrDeveloperTeamRouting-
// Deferred sentinel used to block: a single-cluster namespace routes and dispatches with ZERO inference;
// an undeployed namespace is ErrNoSuchTarget; a multi-cluster namespace is a clarify (never a guess), and
// neither refusal dispatches.
func TestGateway_DeveloperTeamRouting(t *testing.T) {
	t.Parallel()
	const project = "proj-x"
	idx := NewIndex()
	idx.Upsert(agentCR("team-y-a", "developer-team", project, "cluster-a", "team-y", "topic-ty", []string{"users/alice"}))
	idx.Upsert(agentCR("team-x-a", "developer-team", project, "cluster-a", "team-x", "topic-tx-a", []string{"users/alice"}))
	idx.Upsert(agentCR("team-x-b", "developer-team", project, "cluster-b", "team-x", "topic-tx-b", []string{"users/alice"}))

	fake := &FakeDispatcher{}
	g := &Gateway{Resolver: NewResolver(), Index: idx, Dispatch: fake, ProjectID: project}

	t.Run("single-cluster namespace routes and dispatches, no inference", func(t *testing.T) {
		out, err := g.Handle(context.Background(), Message{Text: "@kage /devteam-team-y status", Sender: "users/alice"})
		if err != nil {
			t.Fatalf("dev-team turn errored: %v", err)
		}
		if !out.Dispatched || out.Resolution.Mode != ModeSlash {
			t.Fatalf("dispatched=%v mode=%s, want true/slash", out.Dispatched, out.Resolution.Mode)
		}
		if out.Target.Handle != "@developer-team-team-y" || out.Target.TopicName != "topic-ty" {
			t.Fatalf("target = %+v, want @developer-team-team-y / topic-ty", out.Target)
		}
	})

	t.Run("undeployed namespace is ErrNoSuchTarget", func(t *testing.T) {
		_, err := g.Handle(context.Background(), Message{Text: "@kage @developer-team-ghost hi", Sender: "users/alice"})
		if !errors.Is(err, ErrNoSuchTarget) {
			t.Fatalf("err = %v, want ErrNoSuchTarget", err)
		}
	})

	t.Run("multi-cluster namespace clarifies with candidates, never dispatches", func(t *testing.T) {
		before := len(fake.Sent())
		out, err := g.Handle(context.Background(), Message{Text: "@kage /devteam-team-x status", Sender: "users/alice"})
		if !errors.Is(err, ErrClarify) {
			t.Fatalf("err = %v, want ErrClarify", err)
		}
		var ce *ClarifyError
		if !errors.As(err, &ce) || len(ce.Candidates) != 2 {
			t.Fatalf("clarify candidates = %+v, want 2", ce)
		}
		if out.Dispatched || len(fake.Sent()) != before {
			t.Errorf("clarify dispatched a message (dispatched=%v sent delta=%d)", out.Dispatched, len(fake.Sent())-before)
		}
	})

	if n := g.Resolver.InferenceCalls(); n != 0 {
		t.Errorf("InferenceCalls = %d across dev-team matrix, want 0", n)
	}
}

// TestGateway_InferenceCandidateValidity proves B2's two INDEPENDENT barriers for the NL path, end to
// end through the gateway. Every case spends exactly one inference (the model IS consulted) yet a
// mis-proposal never dispatches: it is refused either by the FILTER (the model's handle is not in the
// live KnownHandles menu) or by the SPINE (Authorize reads the TARGET's allowlist). Routing an inferred
// handle is never itself an authz signal (03 §4a) — the sender must still be on the target's allowlist.
func TestGateway_InferenceCandidateValidity(t *testing.T) {
	t.Parallel()
	const project = "proj-x"

	newGateway := func(spy *spyInferer) (*Gateway, *FakeDispatcher) {
		idx := NewIndex()
		// cluster-a is allowlisted only for users/alice; team-y only for users/alice.
		idx.Upsert(agentCR("cluster-a-agent", "cluster-admin", project, "cluster-a", "", "topic-ca", []string{"users/alice"}))
		idx.Upsert(agentCR("team-y-a", "developer-team", project, "cluster-a", "team-y", "topic-ty", []string{"users/alice"}))
		fake := &FakeDispatcher{}
		g := &Gateway{Resolver: WithInferer(spy), Index: idx, Dispatch: fake, ProjectID: project}
		return g, fake
	}

	t.Run("hallucinated handle is filtered, never routed", func(t *testing.T) {
		// The model invents a cluster with no live agent (absent from KnownHandles).
		spy := &spyInferer{candidates: []Candidate{
			{Handle: Handle{Tier: agentv1alpha1.TierClusterAdmin, Leaf: "cluster-ghost"}, Confidence: 0.99},
		}}
		g, fake := newGateway(spy)
		out, err := g.Handle(context.Background(), Message{Text: "do something clever", Sender: "users/alice"})
		if !errors.Is(err, ErrUnaddressed) {
			t.Fatalf("err = %v, want ErrUnaddressed (hallucination filtered)", err)
		}
		if out.Dispatched || len(fake.Sent()) != 0 {
			t.Errorf("hallucination dispatched (dispatched=%v sent=%d)", out.Dispatched, len(fake.Sent()))
		}
		if g.Resolver.InferenceCalls() != 1 {
			t.Errorf("InferenceCalls = %d, want 1 (model was consulted)", g.Resolver.InferenceCalls())
		}
		// The gateway handed the inferer the LIVE menu, not an empty or foreign set.
		if len(spy.gotKnown) != 2 {
			t.Errorf("inferer got %d known handles, want the 2 live agents", len(spy.gotKnown))
		}
	})

	t.Run("mis-inference to a real agent the sender can't reach is refused before dispatch", func(t *testing.T) {
		// The model confidently proposes cluster-a for a sender who is NOT on cluster-a's allowlist.
		spy := &spyInferer{candidates: []Candidate{
			{Handle: Handle{Tier: agentv1alpha1.TierClusterAdmin, Leaf: "cluster-a"}, Confidence: 0.99},
		}}
		g, fake := newGateway(spy)
		out, err := g.Handle(context.Background(), Message{Text: "drain cluster a", Sender: "users/mallory"})
		if !errors.Is(err, ErrUnauthorized) {
			t.Fatalf("err = %v, want ErrUnauthorized (authz-gated after inference)", err)
		}
		if out.Dispatched || len(fake.Sent()) != 0 {
			t.Errorf("unauthorized inference dispatched (dispatched=%v sent=%d)", out.Dispatched, len(fake.Sent()))
		}
		if g.Resolver.InferenceCalls() != 1 {
			t.Errorf("InferenceCalls = %d, want 1", g.Resolver.InferenceCalls())
		}
	})

	t.Run("confident in-menu handle for an allowlisted sender routes via inference", func(t *testing.T) {
		spy := &spyInferer{candidates: []Candidate{
			{Handle: Handle{Tier: agentv1alpha1.TierDeveloperTeam, Leaf: "team-y"}, Confidence: 0.99},
		}}
		g, fake := newGateway(spy)
		out, err := g.Handle(context.Background(), Message{Text: "help team y with its deploy", Sender: "users/alice"})
		if err != nil {
			t.Fatalf("authorized inference errored: %v", err)
		}
		if !out.Dispatched || out.Resolution.Mode != ModeInference {
			t.Fatalf("dispatched=%v mode=%s, want true/inference", out.Dispatched, out.Resolution.Mode)
		}
		if len(fake.Sent()) != 1 || fake.Sent()[0].TopicName != "topic-ty" {
			t.Fatalf("dispatch = %+v, want one send to topic-ty", fake.Sent())
		}
		if g.Resolver.InferenceCalls() != 1 {
			t.Errorf("InferenceCalls = %d, want 1", g.Resolver.InferenceCalls())
		}
	})
}

// TestInferredHandleStaleMenuRefusedBySpine isolates B2's SECOND barrier: a candidate that PASSED the
// resolver's filter (it was in the menu snapshot the inferer saw) is still refused when the live index
// no longer holds it. The spine's zero-match (ErrNoSuchTarget territory), not the filter, catches a menu
// that went stale between snapshot and dispatch — which is exactly why the two barriers are independent
// rather than redundant. (Through the coupled gateway the two are recomputed together; this drives them
// apart deliberately to prove the spine stands on its own.)
func TestInferredHandleStaleMenuRefusedBySpine(t *testing.T) {
	t.Parallel()
	const project = "proj-x"
	idx := NewIndex()
	idx.Upsert(agentCR("team-y-a", "developer-team", project, "cluster-a", "team-y", "topic-ty", []string{"users/alice"}))
	staleMenu := idx.KnownHandles() // snapshot taken while team-y is live

	// team-y is deleted after the snapshot (the informer removed the CR).
	idx.Remove(types.NamespacedName{Namespace: "kubeagents-system", Name: "team-y-a"})

	spy := &spyInferer{candidates: []Candidate{
		{Handle: Handle{Tier: agentv1alpha1.TierDeveloperTeam, Leaf: "team-y"}, Confidence: 0.99},
	}}
	r := WithInferer(spy)

	// Barrier 1 (filter) PASSES against the stale snapshot: the resolver routes team-y.
	res, err := r.Infer(context.Background(), "help team y", staleMenu)
	if err != nil || res.Handle.Leaf != "team-y" {
		t.Fatalf("Infer on stale menu = (%+v, %v), want a routed team-y (filter passes on the snapshot)", res, err)
	}

	// Barrier 2 (spine) CATCHES it: the LIVE index no longer holds team-y, so lookup yields zero targets
	// and the gateway would refuse with ErrNoSuchTarget — never a dispatch to a departed agent.
	targets, err := idx.LookupHandle(res.Handle, project)
	if err != nil {
		t.Fatalf("LookupHandle errored: %v", err)
	}
	if len(targets) != 0 {
		t.Fatalf("stale handle resolved to %d live targets, want 0 (spine refuses)", len(targets))
	}
}
