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

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

// The ChangePolicy schema-level validations, against a real API server (V-GAT-009).
//
// These are the checks that must hold WITH THE WEBHOOK DOWN. `failurePolicy: fail` protects the
// webhook's own checks, but a cluster mid-upgrade, a cert rotation, or a `kubectl delete
// validatingwebhookconfiguration` all leave the CRD as the only thing standing between a policy
// author and a stored object -- and the two things the CRD must catch on its own are the enum
// (`class: routine` is not a value) and the code-floor-only field.
//
// Everything here is a pair. A test that only asserted rejections would pass against a CRD that
// rejects everything, which is a policy CRD nobody can use.

func newChangePolicy(name string, rules ...agentv1alpha1.ChangeRule) *agentv1alpha1.ChangePolicy {
	return &agentv1alpha1.ChangePolicy{
		ObjectMeta: metav1.ObjectMeta{Name: name},
		Spec:       agentv1alpha1.ChangePolicySpec{Rules: rules},
	}
}

func gatedDeleteRule(id string) agentv1alpha1.ChangeRule {
	return agentv1alpha1.ChangeRule{
		ID:     id,
		When:   agentv1alpha1.ChangeRuleWhen{Verbs: []agentv1alpha1.ChangeVerb{"delete"}},
		Class:  agentv1alpha1.ChangePolicyClassGated,
		Reason: "trust-building period: all deletes are reviewed",
	}
}

func TestChangePolicyCEL(t *testing.T) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		t.Skip("KUBEBUILDER_ASSETS unset; run via `make test` to exercise the ChangePolicy schema rules")
	}

	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		t.Fatalf("add clientgo scheme: %v", err)
	}
	if err := agentv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("add kube-agents scheme: %v", err)
	}

	testEnv := &envtest.Environment{
		CRDDirectoryPaths:     []string{filepath.Join("..", "..", "config", "crd", "bases")},
		ErrorIfCRDPathMissing: true,
		Scheme:                scheme,
	}
	cfg, err := testEnv.Start()
	if err != nil {
		t.Fatalf("start envtest (a CEL compile error in the ChangePolicy CRD surfaces as a CRD-install failure): %v", err)
	}
	t.Cleanup(func() { _ = testEnv.Stop() })

	k8s, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatalf("new client: %v", err)
	}
	ctx := context.Background()

	create := func(t *testing.T, cp *agentv1alpha1.ChangePolicy) error {
		t.Helper()
		err := k8s.Create(ctx, cp)
		if err == nil {
			t.Cleanup(func() { _ = k8s.Delete(ctx, cp) })
		}
		return err
	}

	t.Run("06 §4.2's own example policy is accepted", func(t *testing.T) {
		cp := newChangePolicy("baseline-conservative",
			gatedDeleteRule("gate-all-deletes-while-ramping"),
			agentv1alpha1.ChangeRule{
				ID:         "tighten-fanout",
				MaxObjects: 10,
				Reason:     "cap blast radius below the code ceiling",
			},
		)
		cp.Spec.AgentSelector = &agentv1alpha1.ChangePolicyAgentSelector{
			Tiers:  []agentv1alpha1.AgentTier{agentv1alpha1.TierDeveloperTeam},
			Scopes: []agentv1alpha1.ScopeSpec{{ProjectID: "my-project", ClusterName: "cluster-a"}},
		}
		if err := create(t, cp); err != nil {
			t.Fatalf("the spec's own worked example was rejected: %v", err)
		}
	})

	t.Run("cluster-scoped", func(t *testing.T) {
		// A namespaced policy would be editable by anyone with write on that namespace, which is a
		// weaker bar than the object deserves. Asserted by writing one with no namespace and reading
		// it back cluster-wide.
		cp := newChangePolicy("scope-check", gatedDeleteRule("r1"))
		if err := create(t, cp); err != nil {
			t.Fatalf("create: %v", err)
		}
		var got agentv1alpha1.ChangePolicy
		if err := k8s.Get(ctx, client.ObjectKey{Name: "scope-check"}, &got); err != nil {
			t.Fatalf("get by name alone failed, so the resource is not cluster-scoped: %v", err)
		}
	})

	t.Run("class routine is not a value", func(t *testing.T) {
		cp := newChangePolicy("try-routine", agentv1alpha1.ChangeRule{
			ID:     "downgrade-attempt",
			When:   agentv1alpha1.ChangeRuleWhen{Verbs: []agentv1alpha1.ChangeVerb{"delete"}},
			Class:  agentv1alpha1.ChangePolicyClass("routine"),
			Reason: "please stop asking me about deletes",
		})
		if err := create(t, cp); err == nil {
			t.Fatal("class: routine was stored. It can never lower anything, so the author would believe in an exemption that does not exist")
		} else if !strings.Contains(err.Error(), "Unsupported value") {
			t.Fatalf("rejected, but not by the enum: %v", err)
		}
	})

	t.Run("class forbidden is not a value", func(t *testing.T) {
		cp := newChangePolicy("try-forbidden", agentv1alpha1.ChangeRule{
			ID:     "forbid-attempt",
			When:   agentv1alpha1.ChangeRuleWhen{Verbs: []agentv1alpha1.ChangeVerb{"delete"}},
			Class:  agentv1alpha1.ChangePolicyClass("forbidden"),
			Reason: "never ever",
		})
		if err := create(t, cp); err == nil {
			t.Fatal("class: forbidden was stored; the forbidden set is a code constant and is not addressable by a CR (06 §4.2)")
		}
	})

	t.Run("+1 is a value", func(t *testing.T) {
		// `+1` survives controller-gen's marker parsing, YAML's opinion about a leading plus, and the
		// API server's enum comparison. Three places it could be mangled, none of which a reading of
		// the Go source would reveal.
		cp := newChangePolicy("escalate-policy", agentv1alpha1.ChangeRule{
			ID:     "escalate-prod",
			When:   agentv1alpha1.ChangeRuleWhen{Namespaces: []string{"prod-payments"}},
			Class:  agentv1alpha1.ChangePolicyClassEscalate,
			Reason: "everything in payments is one class stricter",
		})
		if err := create(t, cp); err != nil {
			t.Fatalf("the +1 escalation form was rejected: %v", err)
		}
	})

	t.Run("ownedByLowerTier is refused by the CRD, not only the webhook", func(t *testing.T) {
		cp := newChangePolicy("try-ownership", agentv1alpha1.ChangeRule{
			ID:     "claim-ownership",
			When:   agentv1alpha1.ChangeRuleWhen{OwnedByLowerTier: true},
			Class:  agentv1alpha1.ChangePolicyClassGated,
			Reason: "I know who owns this",
		})
		if err := create(t, cp); err == nil {
			t.Fatal("when.ownedByLowerTier was stored; ownership is computed from the Agent hierarchy, never declared")
		} else if !strings.Contains(err.Error(), "code-floor only") {
			t.Fatalf("rejected, but not by the CEL rule that explains why: %v", err)
		}
	})

	t.Run("a rule contributing nothing is refused", func(t *testing.T) {
		cp := newChangePolicy("try-empty", agentv1alpha1.ChangeRule{
			ID:     "matches-and-does-nothing",
			When:   agentv1alpha1.ChangeRuleWhen{Verbs: []agentv1alpha1.ChangeVerb{"delete"}},
			Reason: "looks like a control in a policy review",
		})
		if err := create(t, cp); err == nil {
			t.Fatal("a rule with neither class nor maxObjects was stored; it reads as a control and is not one")
		}
	})

	t.Run("an unknown verb is refused", func(t *testing.T) {
		cp := newChangePolicy("try-verb", agentv1alpha1.ChangeRule{
			ID:     "bad-verb",
			When:   agentv1alpha1.ChangeRuleWhen{Verbs: []agentv1alpha1.ChangeVerb{"exec"}},
			Class:  agentv1alpha1.ChangePolicyClassGated,
			Reason: "gate every exec",
		})
		if err := create(t, cp); err == nil {
			t.Fatal("a verb the envelope cannot carry was stored; the rule would match nothing, and matching nothing is how a gate stops gating unnoticed")
		}
	})

	t.Run("at least one rule is required", func(t *testing.T) {
		if err := create(t, newChangePolicy("try-empty-policy")); err == nil {
			t.Fatal("a ChangePolicy with no rules was stored; it appears in the policy list and constrains nothing")
		}
	})
}
