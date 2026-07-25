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
	"github.com/gke-labs/kube-agents/k8s-operator/internal/agentindex"
)

// --- Resolve: deterministic slash/handle modes, no inference (06 §2b) ---

func TestResolve_SlashAndHandleModes(t *testing.T) {
	tests := []struct {
		name     string
		text     string
		wantTier agentv1alpha1.AgentTier
		wantLeaf string
		wantMode Mode
	}{
		// Mode 1: slash commands (with and without the @kage mention).
		{"slash cluster short alias", "@kage /cluster-bravo drain node-3", agentv1alpha1.TierClusterAdmin, "bravo", ModeSlash},
		{"slash cluster canonical", "/cluster-admin-bravo status", agentv1alpha1.TierClusterAdmin, "bravo", ModeSlash},
		{"slash platform", "/platform-proj1 audit fleet", agentv1alpha1.TierPlatform, "proj1", ModeSlash},
		{"slash devteam short alias", "@kage /devteam-charlie scale up", agentv1alpha1.TierDeveloperTeam, "charlie", ModeSlash},
		// Mode 2: explicit @handle anywhere in the message; @kage itself is skipped.
		{"handle cluster canonical", "hey @cluster-admin-bravo please look", agentv1alpha1.TierClusterAdmin, "bravo", ModeHandle},
		{"handle cluster short alias", "@kage @cluster-bravo whats up", agentv1alpha1.TierClusterAdmin, "bravo", ModeHandle},
		{"handle platform", "@platform-proj1 report", agentv1alpha1.TierPlatform, "proj1", ModeHandle},
		{"handle devteam canonical", "ping @developer-team-charlie now", agentv1alpha1.TierDeveloperTeam, "charlie", ModeHandle},
		{"handle with trailing punctuation", "@cluster-bravo: what is the status", agentv1alpha1.TierClusterAdmin, "bravo", ModeHandle},
		// Precedence: canonical `cluster-admin-` must NOT mis-parse as the `cluster-` alias.
		{"canonical beats alias prefix", "/cluster-admin-west go", agentv1alpha1.TierClusterAdmin, "west", ModeSlash},
	}

	r := NewResolver()
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			res, err := r.Resolve(context.Background(), tt.text)
			if err != nil {
				t.Fatalf("Resolve(%q) unexpected error: %v", tt.text, err)
			}
			if res.Handle.Tier != tt.wantTier || res.Handle.Leaf != tt.wantLeaf {
				t.Errorf("Resolve(%q) handle = (%s,%s), want (%s,%s)", tt.text, res.Handle.Tier, res.Handle.Leaf, tt.wantTier, tt.wantLeaf)
			}
			if res.Mode != tt.wantMode {
				t.Errorf("Resolve(%q) mode = %s, want %s", tt.text, res.Mode, tt.wantMode)
			}
		})
	}
	// LOAD-BEARING: the entire slash/handle matrix spent ZERO inference (06 §2b: modes 1-2 are free).
	if got := r.InferenceCalls(); got != 0 {
		t.Fatalf("slash/handle matrix spent inference: InferenceCalls = %d, want 0", got)
	}
}

// --- Resolve: deterministic refusals; the router clarifies, never guesses (06 §2b) ---

func TestResolve_DeterministicRefusals(t *testing.T) {
	tests := []struct {
		name    string
		text    string
		wantErr error
		wantMod Mode
	}{
		// Fallthrough (no slash/handle) is now the deterministic ErrNeedsInference signal — Resolve stops
		// there without touching the model; the gateway decides escalate-or-refuse.
		{"unaddressed NL needs inference", "please scale my app to 5 replicas", ErrNeedsInference, ModeInference},
		{"empty message needs inference", "", ErrNeedsInference, ModeInference},
		{"only the bot mention needs inference", "@kage", ErrNeedsInference, ModeInference},
		{"unknown tier in slash", "/wombat-foo hi", ErrUnknownTier, ModeSlash},
		{"unknown tier in handle", "@wombat-foo hi", ErrUnknownTier, ModeHandle},
		{"empty leaf in slash", "/cluster- hi", ErrMalformedHandle, ModeSlash},
		{"invalid leaf chars in handle", "@cluster-Bad_Name hi", ErrMalformedHandle, ModeHandle},
	}

	r := NewResolver()
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			res, err := r.Resolve(context.Background(), tt.text)
			if !errors.Is(err, tt.wantErr) {
				t.Fatalf("Resolve(%q) err = %v, want %v", tt.text, err, tt.wantErr)
			}
			if res.Mode != tt.wantMod {
				t.Errorf("Resolve(%q) refusal mode = %s, want %s (audit must record attempted mode)", tt.text, res.Mode, tt.wantMod)
			}
		})
	}
	// LOAD-BEARING: even the fall-through-to-inference refusals spent ZERO inference in Phase 2.
	if got := r.InferenceCalls(); got != 0 {
		t.Fatalf("Phase-2 refusals spent inference: InferenceCalls = %d, want 0", got)
	}
}

// spyInferer records invocations (and the menu it was handed) so tests can prove exactly when — and
// only when — inference is spent, and that the deterministic core always passes the live handle menu.
type spyInferer struct {
	calls      int
	candidates []Candidate
	err        error
	gotKnown   []Handle
}

func (s *spyInferer) Infer(_ context.Context, _ string, known []Handle) ([]Candidate, error) {
	s.calls++
	s.gotKnown = known
	return s.candidates, s.err
}

// TestResolve_InferenceBoundary proves the split boundary from both sides: addressed messages resolve
// deterministically via Resolve and NEVER reach the inferer; an unaddressed message makes Resolve return
// ErrNeedsInference WITHOUT spending, and only the subsequent Infer call spends exactly once. This is
// what makes InferenceCalls==0 for modes 1/2 meaningful rather than vacuous.
func TestResolve_InferenceBoundary(t *testing.T) {
	known := []Handle{{Tier: agentv1alpha1.TierClusterAdmin, Leaf: "guessed"}}
	spy := &spyInferer{candidates: []Candidate{{Handle: known[0], Confidence: 0.95}}}
	r := WithInferer(spy)

	// Addressed messages (mode 1/2) resolve deterministically and must NOT invoke the inferer.
	for _, text := range []string{"/cluster-bravo go", "@platform-proj1 report"} {
		if _, err := r.Resolve(context.Background(), text); err != nil {
			t.Fatalf("Resolve(%q) unexpected error: %v", text, err)
		}
	}
	if spy.calls != 0 || r.InferenceCalls() != 0 {
		t.Fatalf("addressed messages spent inference: spy=%d counter=%d, want 0/0", spy.calls, r.InferenceCalls())
	}

	// An unaddressed message: Resolve signals ErrNeedsInference WITHOUT spending.
	if _, err := r.Resolve(context.Background(), "please help me"); !errors.Is(err, ErrNeedsInference) {
		t.Fatalf("Resolve(unaddressed) err = %v, want ErrNeedsInference", err)
	}
	if spy.calls != 0 || r.InferenceCalls() != 0 {
		t.Fatalf("Resolve spent inference on fallthrough: spy=%d counter=%d, want 0/0", spy.calls, r.InferenceCalls())
	}

	// Only Infer spends — exactly one call — and it routes the confident, in-menu candidate.
	res, err := r.Infer(context.Background(), "please help me", known)
	if err != nil {
		t.Fatalf("Infer(unaddressed) unexpected error: %v", err)
	}
	if res.Mode != ModeInference || res.Handle.Leaf != "guessed" {
		t.Errorf("inference result = (%s,%s), want (inference,guessed)", res.Mode, res.Handle.Leaf)
	}
	if spy.calls != 1 || r.InferenceCalls() != 1 {
		t.Fatalf("inference accounting off: spy=%d counter=%d, want 1/1", spy.calls, r.InferenceCalls())
	}
	if len(spy.gotKnown) != 1 || spy.gotKnown[0].Leaf != "guessed" {
		t.Errorf("Infer was not handed the live menu: %+v", spy.gotKnown)
	}
}

// TestInfer_RouteClarifyAndAccounting exercises the mode-3 core contract: the deterministic core owns
// the route/clarify decision and inference is spent exactly once for a routed OR clarified turn, and not
// at all for empty text or a missing inferer.
func TestInfer_RouteClarifyAndAccounting(t *testing.T) {
	known := []Handle{
		{Tier: agentv1alpha1.TierClusterAdmin, Leaf: "cluster-a"},
		{Tier: agentv1alpha1.TierClusterAdmin, Leaf: "cluster-b"},
	}

	t.Run("empty text refuses before spending", func(t *testing.T) {
		spy := &spyInferer{}
		r := WithInferer(spy)
		if _, err := r.Infer(context.Background(), "   ", known); !errors.Is(err, ErrUnaddressed) {
			t.Fatalf("err = %v, want ErrUnaddressed", err)
		}
		if spy.calls != 0 || r.InferenceCalls() != 0 {
			t.Fatalf("empty text spent inference: %d/%d, want 0/0", spy.calls, r.InferenceCalls())
		}
	})

	t.Run("no inferer refuses without spending", func(t *testing.T) {
		r := NewResolver()
		if _, err := r.Infer(context.Background(), "do something", known); !errors.Is(err, ErrInferenceUnavailable) {
			t.Fatalf("err = %v, want ErrInferenceUnavailable", err)
		}
		if r.InferenceCalls() != 0 {
			t.Fatalf("nil-inferer path incremented: %d, want 0", r.InferenceCalls())
		}
	})

	t.Run("confident in-menu candidate routes, spends one", func(t *testing.T) {
		spy := &spyInferer{candidates: []Candidate{{Handle: known[0], Confidence: 0.9}}}
		r := WithInferer(spy)
		res, err := r.Infer(context.Background(), "drain cluster a", known)
		if err != nil {
			t.Fatalf("unexpected err: %v", err)
		}
		if res.Handle != known[0] || res.Mode != ModeInference {
			t.Fatalf("res = %+v, want route to cluster-a via inference", res)
		}
		if r.InferenceCalls() != 1 {
			t.Fatalf("InferenceCalls = %d, want 1 for a routed turn", r.InferenceCalls())
		}
	})

	t.Run("low confidence clarifies, still spends one", func(t *testing.T) {
		spy := &spyInferer{candidates: []Candidate{{Handle: known[0], Confidence: 0.5}}}
		r := WithInferer(spy)
		var ce *ClarifyError
		_, err := r.Infer(context.Background(), "ambiguous ask", known)
		if !errors.Is(err, ErrClarify) || !errors.As(err, &ce) {
			t.Fatalf("err = %v, want ErrClarify", err)
		}
		if r.InferenceCalls() != 1 {
			t.Fatalf("InferenceCalls = %d, want 1 for a clarified turn", r.InferenceCalls())
		}
	})

	t.Run("near-tie clarifies even above threshold", func(t *testing.T) {
		spy := &spyInferer{candidates: []Candidate{
			{Handle: known[0], Confidence: 0.90},
			{Handle: known[1], Confidence: 0.88}, // within the 0.10 margin of the top
		}}
		r := WithInferer(spy)
		if _, err := r.Infer(context.Background(), "which cluster", known); !errors.Is(err, ErrClarify) {
			t.Fatalf("err = %v, want ErrClarify (near-tie)", err)
		}
	})

	t.Run("hallucinated candidate is filtered out, refuses after spending", func(t *testing.T) {
		spy := &spyInferer{candidates: []Candidate{
			{Handle: Handle{Tier: agentv1alpha1.TierClusterAdmin, Leaf: "does-not-exist"}, Confidence: 0.99},
		}}
		r := WithInferer(spy)
		if _, err := r.Infer(context.Background(), "route me to a ghost", known); !errors.Is(err, ErrUnaddressed) {
			t.Fatalf("err = %v, want ErrUnaddressed (hallucination filtered)", err)
		}
		if r.InferenceCalls() != 1 {
			t.Fatalf("InferenceCalls = %d, want 1 (the model WAS consulted)", r.InferenceCalls())
		}
	})
}

// TestInfer_ThresholdFlipByConfigOnly proves the route↔clarify boundary moves ONLY via WithThreshold:
// the identical model proposal routes under the default gate and clarifies under a raised one.
func TestInfer_ThresholdFlipByConfigOnly(t *testing.T) {
	known := []Handle{{Tier: agentv1alpha1.TierClusterAdmin, Leaf: "cluster-a"}}
	cands := []Candidate{{Handle: known[0], Confidence: 0.80}}

	rRoute := WithInferer(&spyInferer{candidates: cands})
	if res, err := rRoute.Infer(context.Background(), "x", known); err != nil || res.Handle != known[0] {
		t.Fatalf("default threshold: res=%+v err=%v, want route", res, err)
	}

	rClarify := WithInferer(&spyInferer{candidates: cands}).WithThreshold(0.85, 0.10)
	if _, err := rClarify.Infer(context.Background(), "x", known); !errors.Is(err, ErrClarify) {
		t.Fatalf("raised threshold: err = %v, want ErrClarify", err)
	}
}

// --- RouteKey: the no-drift guarantee (a resolved handle and an indexed CR produce the same key) ---

func TestHandle_RouteKey(t *testing.T) {
	const project = "proj1"

	t.Run("platform leaf is the project", func(t *testing.T) {
		h := Handle{Tier: agentv1alpha1.TierPlatform, Leaf: project}
		got, err := h.RouteKey("ignored-context")
		if err != nil {
			t.Fatal(err)
		}
		if want := agentindex.Identity(agentv1alpha1.TierPlatform, project, "", ""); got != want {
			t.Errorf("RouteKey = %q, want %q", got, want)
		}
	})

	t.Run("cluster-admin key matches the CR's ScopeIdentity", func(t *testing.T) {
		h := Handle{Tier: agentv1alpha1.TierClusterAdmin, Leaf: "cluster-a"}
		got, err := h.RouteKey(project)
		if err != nil {
			t.Fatal(err)
		}
		// Build the CR the index would hold and confirm both sides agree (no-drift).
		cr := &agentv1alpha1.Agent{}
		cr.Spec.Tier = agentv1alpha1.TierClusterAdmin
		cr.Spec.Scope = &agentv1alpha1.ScopeSpec{ProjectID: project, ClusterName: "cluster-a"}
		if want := agentindex.ScopeIdentity(cr); got != want {
			t.Errorf("RouteKey = %q, want ScopeIdentity %q", got, want)
		}
	})

	t.Run("cluster-admin without project context is refused", func(t *testing.T) {
		h := Handle{Tier: agentv1alpha1.TierClusterAdmin, Leaf: "cluster-a"}
		if _, err := h.RouteKey(""); !errors.Is(err, ErrMissingProjectContext) {
			t.Errorf("RouteKey(no project) err = %v, want ErrMissingProjectContext", err)
		}
	})

	t.Run("developer-team has no RouteKey branch (resolved via the index)", func(t *testing.T) {
		// A developer-team handle carries only a namespace leaf and cannot name a cluster, so its full
		// key exists only on a live CR: it is resolved through Index.LookupHandle (byTierLeaf), never
		// RouteKey. RouteKey therefore does not form a dev-team key — the routing path is exercised in
		// TestIndex_LookupHandle / TestGateway_DeveloperTeamRouting.
		h := Handle{Tier: agentv1alpha1.TierDeveloperTeam, Leaf: "team-ns"}
		if _, err := h.RouteKey(project); !errors.Is(err, ErrUnknownTier) {
			t.Errorf("RouteKey(devteam) err = %v, want ErrUnknownTier (no dev-team branch; use LookupHandle)", err)
		}
	})
}

func TestHandleForAgent_And_Canonical(t *testing.T) {
	tests := []struct {
		name          string
		tier          agentv1alpha1.AgentTier
		scope         *agentv1alpha1.ScopeSpec
		wantLeaf      string
		wantCanonical string
	}{
		{"platform", agentv1alpha1.TierPlatform, &agentv1alpha1.ScopeSpec{ProjectID: "proj1"}, "proj1", "@platform-proj1"},
		{"cluster-admin", agentv1alpha1.TierClusterAdmin, &agentv1alpha1.ScopeSpec{ProjectID: "proj1", ClusterName: "cluster-a"}, "cluster-a", "@cluster-admin-cluster-a"},
		{"developer-team", agentv1alpha1.TierDeveloperTeam, &agentv1alpha1.ScopeSpec{ProjectID: "proj1", ClusterName: "cluster-a", Namespace: "team-x"}, "team-x", "@developer-team-team-x"},
		{"empty tier defaults platform", "", &agentv1alpha1.ScopeSpec{ProjectID: "proj1"}, "proj1", "@platform-proj1"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cr := &agentv1alpha1.Agent{}
			cr.Spec.Tier = tt.tier
			cr.Spec.Scope = tt.scope
			h := HandleForAgent(cr)
			if h.Leaf != tt.wantLeaf {
				t.Errorf("HandleForAgent leaf = %q, want %q", h.Leaf, tt.wantLeaf)
			}
			if got := h.Canonical(); got != tt.wantCanonical {
				t.Errorf("Canonical = %q, want %q", got, tt.wantCanonical)
			}
		})
	}
}

// --- Authorize: fail-closed, allowlist-only, routing-independent (03 §4a; Phase 2 acceptance d) ---

func TestAuthorize_FailClosed(t *testing.T) {
	closed := Target{Handle: "@cluster-admin-cluster-a", AllowedUsers: []string{"users/alice", "users/bob"}}

	tests := []struct {
		name    string
		target  Target
		sender  string
		want    bool
		wantSub string // substring the reason must contain
	}{
		{"sender in allowlist", closed, "users/alice", true, "in allowlist"},
		{"sender not in allowlist", closed, "users/mallory", false, "not in allowlist"},
		{"empty sender denied", closed, "", false, "empty sender"},
		{"absent allowlist refuses all", Target{Handle: "@x", AllowedUsers: nil}, "users/alice", false, "fail-closed"},
		{"empty allowlist refuses all", Target{Handle: "@x", AllowedUsers: []string{}}, "users/alice", false, "fail-closed"},
		{"lone-empty-entry allowlist refuses all", Target{Handle: "@x", AllowedUsers: []string{""}}, "users/alice", false, "fail-closed"},
		{"whitespace sender denied", closed, "   ", false, "empty sender"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := Authorize(tt.target, tt.sender)
			if got.Allowed != tt.want {
				t.Errorf("Authorize(%q) allowed = %v, want %v (reason: %s)", tt.sender, got.Allowed, tt.want, got.Reason)
			}
			if tt.wantSub != "" && !strings.Contains(got.Reason, tt.wantSub) {
				t.Errorf("Authorize(%q) reason = %q, want substring %q", tt.sender, got.Reason, tt.wantSub)
			}
		})
	}
}

// TestAuthorize_EmptyAllowlistRefusesEveryone pins the rule that made the router
// the one layer never affected by the pre-P8-T1 bypass: given an allowlist that
// names nobody, it refuses everybody. It reads only AllowedUsers and no
// environment flag, so the deleted *_ALLOW_ALL_USERS backstop could not reach it
// then and nothing can widen it now (V-CTR-014).
func TestAuthorize_EmptyAllowlistRefusesEveryone(t *testing.T) {
	// This is the CR state that used to make the operator emit ALLOW_ALL_USERS=true.
	namesNobody := Target{Handle: "@cluster-admin-cluster-a", AllowedUsers: nil}
	if d := Authorize(namesNobody, "users/anyone"); d.Allowed {
		t.Fatalf("router allowed a sender against an allowlist that names nobody: allowed=%v reason=%q; must be fail-closed", d.Allowed, d.Reason)
	}
}
