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

package controller_test

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/classify"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/policy"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/scope"
)

// The ChangePolicy LOADER, against a real API server (V-GAT-009).
//
// changepolicy_cel_envtest_test.go proves what a real API server will store. This proves what the
// broker does with what it stored -- the half P9-T3b explicitly did not claim: "nothing reads a
// ChangePolicy out of a cluster yet". A policy that an operator applies, that `kubectl get` shows,
// and that no broker ever reads is worse than no policy, because every signal says it is in force.
//
// Deliberately runs with NO webhook. `failurePolicy: fail` protects admission's checks in a healthy
// cluster, and a test that relied on it would be proving a property of the webhook rather than of
// the broker. What is asserted here is that a policy which somehow reached etcd -- webhook down,
// cert rotating, object stored before a rule existed -- cannot loosen anything, because the LOADER
// refuses it using the same ValidateChangeRule the webhook runs.

// probeDelete classifies a single delete of the given kind through the source's current classifier.
// The verb and kind are chosen so that the code floor's own answer is known and low: `delete
// apps/Deployment` is routine, which leaves room for a policy to raise it and makes the raise
// visible.
func probeDelete(t *testing.T, src *policy.Source, group, kind string) classify.Class {
	t.Helper()
	c, err := src.Current()
	if err != nil {
		t.Fatalf("Current: %v", err)
	}
	out, err := c.Classify(&classify.Input{
		Caller: classify.Caller{Name: "platform-agent", Tier: string(agentv1alpha1.TierPlatform),
			Scope: scope.Scope{ProjectID: "adamparco-kage"}},
		Operations: []classify.ResolvedOp{{
			Verb: "delete", Kind: classify.KindRef{Group: group, Kind: kind},
			Namespace: "team-a", Name: "x", Exists: true, WholeObject: true,
			Direction: classify.DirectionAny,
		}},
		UndoPlanPresent: true,
	})
	if err != nil {
		t.Fatalf("Classify: %v", err)
	}
	return out.Class
}

func startPolicyEnv(t *testing.T) (client.Client, context.Context) {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		t.Fatalf("add clientgo scheme: %v", err)
	}
	if err := agentv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("add kube-agents scheme: %v", err)
	}
	testEnv := &envtest.Environment{
		CRDDirectoryPaths:     []string{filepath.Join("..", "..", "..", "config", "crd", "bases")},
		ErrorIfCRDPathMissing: true,
		Scheme:                scheme,
	}
	cfg, err := testEnv.Start()
	if err != nil {
		t.Fatalf("start envtest: %v", err)
	}
	t.Cleanup(func() { _ = testEnv.Stop() })

	k8s, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatalf("new client: %v", err)
	}
	return k8s, context.Background()
}

// alwaysSeen makes no action novel, so the novel-action escalation cannot supply the class change
// this test is attributing to a ChangePolicy.
type alwaysSeen struct{}

func (alwaysSeen) Seen(string, string, classify.KindRef, string) bool { return true }

func newPolicySource(t *testing.T, k8s client.Client) *policy.Source {
	t.Helper()
	src, err := policy.NewSource(policy.SourceConfig{
		Reader: k8s,
		Identity: func() (policy.Agent, error) {
			return policy.Agent{Tier: agentv1alpha1.TierPlatform, Scope: scope.Scope{ProjectID: "adamparco-kage"}}, nil
		},
		History: alwaysSeen{},
	})
	if err != nil {
		t.Fatalf("NewSource: %v", err)
	}
	return src
}

func TestPolicySourceOverARealAPIServer(t *testing.T) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		t.Skip("KUBEBUILDER_ASSETS unset; run via `make test` to exercise the ChangePolicy loader against an API server")
	}
	k8s, ctx := startPolicyEnv(t)
	src := newPolicySource(t, k8s)

	// The API server is a real one, so a create that has returned is not necessarily a create the
	// next List observes; every assertion below re-reads through Refresh, which is a live List.
	apply := func(t *testing.T, cp *agentv1alpha1.ChangePolicy) {
		t.Helper()
		if err := k8s.Create(ctx, cp); err != nil {
			t.Fatalf("create ChangePolicy %s: %v", cp.Name, err)
		}
		t.Cleanup(func() { _ = k8s.Delete(ctx, cp) })
	}
	refresh := func(t *testing.T) error {
		t.Helper()
		return src.Refresh(ctx)
	}

	// --- baseline: an empty cluster is the code floor alone -----------------------------------
	if err := refresh(t); err != nil {
		t.Fatalf("initial Refresh: %v", err)
	}
	baseline := probeDelete(t, src, "apps", "Deployment")
	if baseline >= classify.ClassGated {
		t.Fatalf("the baseline class for `delete apps/Deployment` is already %s; this test cannot "+
			"observe a policy raising it and would pass vacuously", baseline)
	}

	// --- tighten: a policy applied to a live cluster raises the class -------------------------
	gateDeletes := newChangePolicy("ramp-up", gatedDeleteRule("gate-all-deletes-while-ramping"))
	apply(t, gateDeletes)
	if err := refresh(t); err != nil {
		t.Fatalf("Refresh after applying the policy: %v", err)
	}
	if got := probeDelete(t, src, "apps", "Deployment"); got != classify.ClassGated {
		t.Fatalf("class = %s, want gated: a ChangePolicy stored in the cluster must be in force", got)
	}
	if snap, err := src.Snapshot(); err != nil {
		t.Fatalf("Snapshot: %v", err)
	} else if len(snap.Names()) != 1 || snap.Names()[0] != "ramp-up" {
		t.Fatalf("policySources = %v, want [ramp-up]; a human reading the record has to be able to "+
			"tell a product floor from their own policy", snap.Names())
	}

	// --- and removing it puts the class back --------------------------------------------------
	// A tightening that could not be lifted would make ChangePolicy a one-way door, and an operator
	// who applied one during an incident would be stuck with it afterwards.
	if err := k8s.Delete(ctx, gateDeletes); err != nil {
		t.Fatalf("delete ChangePolicy: %v", err)
	}
	if err := refresh(t); err != nil {
		t.Fatalf("Refresh after deleting the policy: %v", err)
	}
	if got := probeDelete(t, src, "apps", "Deployment"); got != baseline {
		t.Fatalf("class = %s after the policy was deleted, want the baseline %s", got, baseline)
	}

	// --- a policy that does not bind this agent has no effect ---------------------------------
	other := newChangePolicy("someone-elses", gatedDeleteRule("gate-their-deletes"))
	other.Spec.AgentSelector = &agentv1alpha1.ChangePolicyAgentSelector{
		Tiers: []agentv1alpha1.AgentTier{agentv1alpha1.TierDeveloperTeam},
	}
	apply(t, other)
	if err := refresh(t); err != nil {
		t.Fatalf("Refresh with a non-binding policy: %v", err)
	}
	if got := probeDelete(t, src, "apps", "Deployment"); got != baseline {
		t.Fatalf("class = %s, want the baseline %s: a policy selecting a different tier must not bind", got, baseline)
	}
	if snap, err := src.Snapshot(); err != nil {
		t.Fatalf("Snapshot: %v", err)
	} else if len(snap.Names()) != 0 {
		t.Fatalf("policySources = %v, want empty: a non-binding policy must not appear as a source", snap.Names())
	}
}

// TestALoweringPolicyStoredInTheClusterCannotTakeEffect is the "provably cannot loosen" half of
// V-GAT-009, driven through the live loader rather than through the combinator.
//
// The webhook is not running here, so the lowering policy really is in etcd -- the situation the
// webhook exists to prevent and cannot be relied on to have prevented. The loader refuses to build
// a snapshot over it, which means the broker refuses actions rather than running with a policy set
// it knows is wrong. It does NOT skip the rule and carry on: a skipped rule is a policy the operator
// believes is in force, in a cluster where it is not, with nothing anywhere saying so.
func TestALoweringPolicyStoredInTheClusterCannotTakeEffect(t *testing.T) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		t.Skip("KUBEBUILDER_ASSETS unset; run via `make test`")
	}
	k8s, ctx := startPolicyEnv(t)
	src := newPolicySource(t, k8s)

	if err := src.Refresh(ctx); err != nil {
		t.Fatalf("initial Refresh: %v", err)
	}
	// The floor gates `delete` of a StatefulSet. This rule says elevated for the same match.
	before := probeDelete(t, src, "apps", "StatefulSet")
	if before != classify.ClassGated {
		t.Fatalf("the floor classifies `delete apps/StatefulSet` as %s, not gated; this test's premise is stale", before)
	}

	lowering := newChangePolicy("please-let-me", agentv1alpha1.ChangeRule{
		ID:     "downgrade-stateful-deletes",
		When:   agentv1alpha1.ChangeRuleWhen{Verbs: []agentv1alpha1.ChangeVerb{"delete"}, Kinds: []agentv1alpha1.KindRefSpec{{Group: "apps", Kind: "StatefulSet"}}},
		Class:  agentv1alpha1.ChangePolicyClassElevated,
		Reason: "we do this all the time",
	})
	if err := k8s.Create(ctx, lowering); err != nil {
		t.Fatalf("the CRD rejected the lowering policy, so the loader is not what is being tested here: %v", err)
	}
	t.Cleanup(func() { _ = k8s.Delete(ctx, lowering) })

	err := src.Refresh(ctx)
	if err == nil {
		t.Fatal("Refresh accepted a lowering policy stored in the cluster")
	}
	if !strings.Contains(err.Error(), "please-let-me") {
		t.Fatalf("the refusal must name the policy an operator has to fix: %v", err)
	}
	// And the broker refuses rather than classifying against the set it does have.
	if _, err := src.Current(); err == nil {
		t.Fatal("Current answered while a policy in the cluster could not be loaded")
	}

	// Removing the bad policy restores service without a restart.
	if err := k8s.Delete(ctx, lowering); err != nil {
		t.Fatalf("delete: %v", err)
	}
	if err := src.Refresh(ctx); err != nil {
		t.Fatalf("Refresh after removing the bad policy: %v", err)
	}
	if got := probeDelete(t, src, "apps", "StatefulSet"); got != before {
		t.Fatalf("class = %s, want %s", got, before)
	}
}

// --- negative control (09 §6: V-GAT-009 is marked ¬, so this is mandatory) ------------------------

// TestTheLoaderAssertionsCanFail feeds each predicate above the shape it is meant to catch.
//
// Every assertion in this file has the same structure -- "classify one operation and compare the
// class" -- and that structure has one silent failure mode: a probe that returns the same class no
// matter what is in the cluster. Under it, "the policy took effect" and "the policy was ignored"
// are indistinguishable and both tests pass.
func TestTheLoaderAssertionsCanFail(t *testing.T) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		t.Skip("KUBEBUILDER_ASSETS unset; run via `make test`")
	}
	k8s, ctx := startPolicyEnv(t)
	src := newPolicySource(t, k8s)
	if err := src.Refresh(ctx); err != nil {
		t.Fatalf("Refresh: %v", err)
	}

	// 1. The probe must distinguish two kinds the floor treats differently. If it does not, every
	//    "the class changed" assertion above is comparing a constant to itself.
	routine := probeDelete(t, src, "apps", "Deployment")
	gated := probeDelete(t, src, "apps", "StatefulSet")
	if routine == gated {
		t.Fatalf("the probe returns %s for both a routine and a gated delete; it cannot observe a class change at all", routine)
	}

	// 2. Current() must be capable of refusing. A source whose Current never errors would make
	//    every fail-closed assertion in this file vacuous. The clock jumps between the read and the
	//    question, which is precisely the shape of the failure the staleness rule exists to catch:
	//    a policy set that was fine when it was fetched and is not fine now.
	clk := &jumpingClock{at: time.Now()}
	jumped, err := policy.NewSource(policy.SourceConfig{
		Reader:   k8s,
		Identity: func() (policy.Agent, error) { return policy.Agent{Tier: agentv1alpha1.TierPlatform}, nil },
		History:  alwaysSeen{},
		Now:      clk.now,
	})
	if err != nil {
		t.Fatalf("NewSource: %v", err)
	}
	if err := jumped.Refresh(ctx); err != nil {
		t.Fatalf("Refresh: %v", err)
	}
	if _, err := jumped.Current(); err != nil {
		t.Fatalf("a freshly-read source must answer: %v", err)
	}
	clk.at = clk.at.Add(2 * policy.MaxPolicyStaleness)
	if _, err := jumped.Current(); err == nil {
		t.Error("Current answered on a snapshot older than MaxPolicyStaleness; the staleness rule cannot fire and the fail-closed tests prove nothing")
	}

	// 3. Refresh must be capable of failing. Same argument: a Refresh that always returns nil makes
	//    "the loader refused the lowering policy" unfalsifiable.
	bad := newChangePolicy("floor-collision", agentv1alpha1.ChangeRule{
		ID:     "destructive-stateful-delete", // a code-floor rule ID; the CRD has no opinion, the loader does
		When:   agentv1alpha1.ChangeRuleWhen{Verbs: []agentv1alpha1.ChangeVerb{"delete"}},
		Class:  agentv1alpha1.ChangePolicyClassGated,
		Reason: "reusing a built-in id",
	})
	if err := k8s.Create(ctx, bad); err != nil {
		t.Fatalf("create: %v", err)
	}
	t.Cleanup(func() { _ = k8s.Delete(ctx, bad) })
	if err := src.Refresh(ctx); err == nil {
		t.Error("Refresh accepted a rule reusing a code-floor rule ID; it cannot fail and therefore proves nothing")
	}
}

type jumpingClock struct{ at time.Time }

func (c *jumpingClock) now() time.Time { return c.at }
