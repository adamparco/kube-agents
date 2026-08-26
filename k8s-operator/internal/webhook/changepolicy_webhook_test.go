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

package webhook

import (
	"context"
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

// The ChangePolicy admission surface (V-GAT-009).
//
// The property under test is NOT "a bad policy cannot loosen anything" -- the broker guarantees that
// by construction and `classify.TestALoweringPolicyIsInertEvenIfItReachesTheClassifier` proves it
// with the webhook out of the picture entirely. What is tested here is the second, weaker, and much
// more easily broken claim: that a policy which cannot do what its author intended is REFUSED, at
// the moment they type it, with a message that says which of their rules and why.
//
// So every assertion below is about the text and the field path of an error, not about a boolean.
// An admission webhook that rejects the right objects with the wrong message sends the author to
// read the source, and the population of people who write a ChangePolicy is exactly the population
// that should not have to.

func policy(name string, rules ...agentv1alpha1.ChangeRule) *agentv1alpha1.ChangePolicy {
	return &agentv1alpha1.ChangePolicy{
		ObjectMeta: metav1.ObjectMeta{Name: name},
		Spec:       agentv1alpha1.ChangePolicySpec{Rules: rules},
	}
}

func rule(id string, when agentv1alpha1.ChangeRuleWhen, class agentv1alpha1.ChangePolicyClass) agentv1alpha1.ChangeRule {
	return agentv1alpha1.ChangeRule{ID: id, When: when, Class: class, Reason: "because the policy author said so"}
}

func secretKinds() []agentv1alpha1.KindRefSpec {
	return []agentv1alpha1.KindRefSpec{{Group: "", Kind: "Secret"}}
}

func mustReject(t *testing.T, cp *agentv1alpha1.ChangePolicy, wantFragments ...string) error {
	t.Helper()
	_, err := ValidateChangePolicy(cp)
	if err == nil {
		t.Fatalf("policy %q was admitted; expected a rejection mentioning %v", cp.Name, wantFragments)
	}
	for _, f := range wantFragments {
		if !strings.Contains(err.Error(), f) {
			t.Errorf("rejection message does not contain %q.\ngot: %v", f, err)
		}
	}
	return err
}

func TestValidateChangePolicyAdmitsAWorkingPolicy(t *testing.T) {
	// 06 §4.2's own worked example. If this ever starts failing, the webhook has become stricter
	// than the specification it implements, and the failure mode of that is a customer who cannot
	// express the policy the docs told them to write.
	cp := policy("baseline-conservative",
		rule("gate-all-deletes-while-ramping",
			agentv1alpha1.ChangeRuleWhen{Verbs: []agentv1alpha1.ChangeVerb{"delete"}},
			agentv1alpha1.ChangePolicyClassGated),
		agentv1alpha1.ChangeRule{
			ID:         "tighten-fanout",
			MaxObjects: 10,
			Reason:     "cap blast radius below the code ceiling",
		},
		rule("escalate-payments",
			agentv1alpha1.ChangeRuleWhen{Namespaces: []string{"prod-payments"}},
			agentv1alpha1.ChangePolicyClassEscalate),
	)

	warnings, err := ValidateChangePolicy(cp)
	if err != nil {
		t.Fatalf("the spec's own example policy was rejected: %v", err)
	}
	if len(warnings) != 0 {
		t.Errorf("a policy with nothing surprising in it produced warnings: %v", warnings)
	}
}

func TestValidateChangePolicyNamesTheFloorRuleItIsBelow(t *testing.T) {
	// `elevated` on a delete of a Secret. The floor's `destructive-stateful-delete` classifies every
	// operation this matches as `gated`, so the rule is provably lower everywhere it applies.
	//
	// The author's real intent is almost always "I want deletes of Secrets to be noisier", and the
	// fix is to leave the floor alone. The message therefore has to name the rule they are arguing
	// with -- "class elevated is below the code floor" alone leaves them guessing which floor.
	err := mustReject(t,
		policy("try-lower",
			rule("secrets-are-fine-actually",
				agentv1alpha1.ChangeRuleWhen{
					Verbs: []agentv1alpha1.ChangeVerb{"delete"},
					Kinds: secretKinds(),
				},
				agentv1alpha1.ChangePolicyClassElevated)),
		"below the code floor",
		"destructive-stateful-delete",
		"spec.rules[0]",
	)
	if !strings.Contains(err.Error(), "narrow this rule") && !strings.Contains(err.Error(), "narrow") {
		t.Errorf("the message says what is wrong but not what to do about it: %v", err)
	}
}

// V-CTR-021 -- the two path dialects are never interchangeable, at all three places one could be
// accepted for the other. This is the first: admission. The second is `classify.ValidateChangeRule`
// (TestJSONPointerInFieldPathsIsRejected, classify/stricter_test.go), which catches objects that
// never met the webhook; the third is the matcher itself (TestPointerPrefixMatchRejectsPointerAsRule,
// classify/path_test.go), which matches nothing rather than helpfully normalising. The sweep in
// verification/mutants/V-CTR-021.json is what shows no single layer is load-bearing alone.
func TestValidateChangePolicyRejectsAJSONPointerFieldPath(t *testing.T) {
	// The exact message is specified by 06 §4.2, and the reason it is specified is that this mistake
	// is otherwise SILENT: `/spec/replicas` is a well-formed dotted path whose single segment is
	// literally named `/spec/replicas`. The rule parses, admits, stores, lists -- and matches
	// nothing, forever.
	err := mustReject(t,
		policy("pointer-dialect",
			agentv1alpha1.ChangeRule{
				ID: "gate-replica-changes",
				When: agentv1alpha1.ChangeRuleWhen{
					FieldPaths: []string{"/spec/replicas"},
				},
				Class:  agentv1alpha1.ChangePolicyClassGated,
				Reason: "scaling production is reviewed",
			}),
		"expected a dotted field path, not a JSON Pointer",
		"spec.rules[0].when.fieldPaths[0]",
	)

	// One error, not two. The rule-level validator also refuses this path, and stacking a restatement
	// on top of the specific error makes the author look for a second, nonexistent problem.
	if n := strings.Count(err.Error(), "JSON Pointer"); n != 1 {
		t.Errorf("the same defect was reported %d times; expected once: %v", n, err)
	}
}

func TestValidateChangePolicyRejectsOwnedByLowerTier(t *testing.T) {
	// Ownership is computed from the Agent hierarchy. A policy that could assert it would be making
	// a claim about the hierarchy rather than reading one, and the claim would be believed.
	mustReject(t,
		policy("claim-ownership",
			rule("i-know-who-owns-this",
				agentv1alpha1.ChangeRuleWhen{OwnedByLowerTier: true},
				agentv1alpha1.ChangePolicyClassGated)),
		"spec.rules[0].when.ownedByLowerTier",
		"code-floor only",
		"computed from the Agent hierarchy",
	)
}

func TestValidateChangePolicyRejectsAFloorRuleIDCollision(t *testing.T) {
	// Rule IDs land in `classification.reasons[].rule` and in the audit event. A policy rule wearing
	// a floor rule's ID makes the journal say the floor fired when it did not, which corrupts the
	// one record a human has when reconstructing why an action was gated.
	mustReject(t,
		policy("shadow-the-floor",
			rule("secret-write",
				agentv1alpha1.ChangeRuleWhen{Kinds: secretKinds()},
				agentv1alpha1.ChangePolicyClassGated)),
		"secret-write",
		"spec.rules[0]",
	)
}

func TestValidateChangePolicyRejectsDuplicateRuleIDs(t *testing.T) {
	err := mustReject(t,
		policy("two-of-a-kind",
			rule("gate-deletes",
				agentv1alpha1.ChangeRuleWhen{Verbs: []agentv1alpha1.ChangeVerb{"delete"}},
				agentv1alpha1.ChangePolicyClassGated),
			rule("gate-deletes",
				agentv1alpha1.ChangeRuleWhen{Verbs: []agentv1alpha1.ChangeVerb{"cloud"}},
				agentv1alpha1.ChangePolicyClassGated)),
		"spec.rules[1].id",
		"already used by rules[0]",
	)
	if strings.Contains(err.Error(), "spec.rules[0].id") {
		t.Errorf("the FIRST use was flagged as the duplicate; the author would delete the wrong rule: %v", err)
	}
}

func TestValidateChangePolicyWarnsButAdmitsACapAboveTheCeiling(t *testing.T) {
	// A cap above the code ceiling is inert, not dangerous: EffectiveMaxObjects takes the minimum.
	// Refusing it would put the guarantee in the webhook, where a bypass removes it. Warning keeps
	// the guarantee in the combinator and still tells the author their number will never win.
	cp := policy("optimistic-cap", agentv1alpha1.ChangeRule{
		ID:         "raise-the-cap",
		MaxObjects: 5000,
		Reason:     "we do big rollouts",
	})

	warnings, err := ValidateChangePolicy(cp)
	if err != nil {
		t.Fatalf("a cap above the ceiling was refused; it is inert, and refusing it implies the webhook is what makes it inert: %v", err)
	}
	if len(warnings) != 1 {
		t.Fatalf("expected exactly one warning about the inert cap, got %v", warnings)
	}
	for _, want := range []string{"spec.rules[0].maxObjects", "5000", "minimum", "cannot raise"} {
		if !strings.Contains(warnings[0], want) {
			t.Errorf("warning does not contain %q: %s", want, warnings[0])
		}
	}
}

func TestValidateChangePolicyReportsEveryBadRuleAtOnce(t *testing.T) {
	// An author fixing a policy one admission round-trip per mistake will stop reading the messages.
	err := mustReject(t,
		policy("several-problems",
			rule("bad-path",
				agentv1alpha1.ChangeRuleWhen{FieldPaths: []string{"/spec/replicas"}},
				agentv1alpha1.ChangePolicyClassGated),
			rule("bad-ownership",
				agentv1alpha1.ChangeRuleWhen{OwnedByLowerTier: true},
				agentv1alpha1.ChangePolicyClassGated),
			rule("bad-floor",
				agentv1alpha1.ChangeRuleWhen{
					Verbs: []agentv1alpha1.ChangeVerb{"delete"},
					Kinds: secretKinds(),
				},
				agentv1alpha1.ChangePolicyClassElevated)),
		"spec.rules[0].when.fieldPaths[0]",
		"spec.rules[1].when.ownedByLowerTier",
		"spec.rules[2]",
	)
	t.Logf("combined message: %v", err)
}

func TestValidateDeleteAllowsAHumanToRemoveAPolicy(t *testing.T) {
	// Deleting a policy IS a loosening, and it is allowed here on purpose: the thing that may not
	// delete a ChangePolicy is an agent identity, which is an authorization question answered by RBAC
	// and by `vap-agent-scope`. Blocking it here would mean the only way out of a bad policy is
	// editing etcd.
	v := &ChangePolicyCustomValidator{}
	warnings, err := v.ValidateDelete(context.Background(), policy("going-away"))
	if err != nil {
		t.Fatalf("a human with cluster-admin could not remove a policy: %v", err)
	}
	if len(warnings) != 0 {
		t.Errorf("unexpected warnings on delete: %v", warnings)
	}
}

// TestValidatorRejectsTheWrongType is gone: admission.Validator is now generic
// (admission.Validator[*ChangePolicy]), so passing an *Agent to ValidateCreate/ValidateUpdate is a
// compile error, not a runtime type assertion this test could exercise.

func TestValidateCreateAndUpdateApplyTheSameRules(t *testing.T) {
	// The way a policy gets weaker in practice is an edit, not a create. A webhook wired for
	// `create` only would let every rule here be removed by `kubectl edit`.
	v := &ChangePolicyCustomValidator{}
	ctx := context.Background()
	bad := policy("try-lower",
		rule("secrets-are-fine-actually",
			agentv1alpha1.ChangeRuleWhen{
				Verbs: []agentv1alpha1.ChangeVerb{"delete"},
				Kinds: secretKinds(),
			},
			agentv1alpha1.ChangePolicyClassElevated))
	good := policy("fine",
		rule("gate-deletes",
			agentv1alpha1.ChangeRuleWhen{Verbs: []agentv1alpha1.ChangeVerb{"delete"}},
			agentv1alpha1.ChangePolicyClassGated))

	if _, err := v.ValidateCreate(ctx, bad); err == nil {
		t.Error("ValidateCreate admitted a below-floor rule")
	}
	if _, err := v.ValidateUpdate(ctx, good, bad); err == nil {
		t.Error("ValidateUpdate admitted a below-floor rule; a policy could be weakened by editing it")
	}
	if _, err := v.ValidateUpdate(ctx, bad, good); err != nil {
		t.Errorf("ValidateUpdate judged the OLD object rather than the new one: %v", err)
	}
}
