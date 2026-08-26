package classify

import (
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

// V-GAT-009, the L1 half: `ChangePolicy` is stricter-only.
//
// Both halves of the property are tested here, because they are independent guarantees and a suite
// that only covered one would be satisfied by deleting the other:
//
//	ADMISSION   a rule declaring a class below the code floor for the same match is REFUSED.
//	THE BROKER  a lowering rule that reaches the classifier anyway is INERT.
//
// The second is the one that survives the webhook being down, so it is tested by handing the
// classifier a policy that admission would have refused and asserting the answer does not move.

// policyRule builds a CRD rule with the fields a test is not varying already filled in.
func policyRule(id string, class agentv1alpha1.ChangePolicyClass, when agentv1alpha1.ChangeRuleWhen) *agentv1alpha1.ChangeRule {
	return &agentv1alpha1.ChangeRule{
		ID:     id,
		When:   when,
		Class:  class,
		Reason: "a reason, because the human approving this has to read something",
	}
}

func kinds(gk ...string) []agentv1alpha1.KindRefSpec {
	out := make([]agentv1alpha1.KindRefSpec, 0, len(gk)/2)
	for i := 0; i+1 < len(gk); i += 2 {
		out = append(out, agentv1alpha1.KindRefSpec{Group: gk[i], Kind: gk[i+1]})
	}
	return out
}

func TestStricterOnlyRefusesAClassBelowTheFloor(t *testing.T) {
	cases := []struct {
		name      string
		rule      *agentv1alpha1.ChangeRule
		wantFloor string
	}{
		{
			name: "elevating a stateful delete is below destructive-stateful-delete",
			rule: policyRule("soften-pvc-deletes", agentv1alpha1.ChangePolicyClassElevated,
				agentv1alpha1.ChangeRuleWhen{
					Verbs: []agentv1alpha1.ChangeVerb{"delete"},
					Kinds: kinds("", "PersistentVolumeClaim"),
				}),
			wantFloor: RuleDestructiveStatefulDelete,
		},
		{
			name: "a subset of the stateful kinds is still contained",
			rule: policyRule("soften-some-deletes", agentv1alpha1.ChangePolicyClassElevated,
				agentv1alpha1.ChangeRuleWhen{
					Verbs: []agentv1alpha1.ChangeVerb{"delete"},
					Kinds: kinds("", "PersistentVolume", "apps", "StatefulSet"),
				}),
			wantFloor: RuleDestructiveStatefulDelete,
		},
		{
			name: "elevating a loosening write is below security-loosen",
			rule: policyRule("soften-loosening", agentv1alpha1.ChangePolicyClassElevated,
				agentv1alpha1.ChangeRuleWhen{
					Verbs:     []agentv1alpha1.ChangeVerb{"patch"},
					Direction: agentv1alpha1.ChangeDirectionLoosen,
				}),
			wantFloor: RuleSecurityLoosen,
		},
		{
			name: "a narrower field path is still inside traffic-shift-production",
			rule: policyRule("soften-selector-edits", agentv1alpha1.ChangePolicyClassElevated,
				agentv1alpha1.ChangeRuleWhen{
					Verbs:      []agentv1alpha1.ChangeVerb{"patch"},
					Kinds:      kinds("", "Service"),
					FieldPaths: []string{"spec.selector.app"},
				}),
			wantFloor: RuleTrafficShiftProduction,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := ValidateChangeRule(tc.rule)
			if err == nil {
				t.Fatalf("rule was admitted; it declares %s for a match the floor classifies higher", tc.rule.Class)
			}
			if !strings.Contains(err.Error(), tc.wantFloor) {
				t.Fatalf("error does not name the floor rule %q, so the author cannot tell what to narrow: %v", tc.wantFloor, err)
			}
		})
	}
}

func TestStricterOnlyAdmitsHonestTightenings(t *testing.T) {
	cases := []struct {
		name string
		rule *agentv1alpha1.ChangeRule
	}{
		{
			// 06 §4.2's own example. Nothing in the floor fires for EVERY delete, so nothing constrains
			// this -- and if containment were implemented as overlap it would be refused, because
			// deletes of stateful kinds are gated.
			name: "gate-all-deletes-while-ramping",
			rule: policyRule("gate-all-deletes-while-ramping", agentv1alpha1.ChangePolicyClassGated,
				agentv1alpha1.ChangeRuleWhen{Verbs: []agentv1alpha1.ChangeVerb{"delete"}}),
		},
		{
			// The case that makes the difference between containment and overlap visible. Every write
			// overlaps `security-loosen`; none is contained in it unless it pins direction.
			name: "elevate every create in one namespace",
			rule: policyRule("elevate-team-a-creates", agentv1alpha1.ChangePolicyClassElevated,
				agentv1alpha1.ChangeRuleWhen{
					Verbs:      []agentv1alpha1.ChangeVerb{"create"},
					Namespaces: []string{"team-a"},
				}),
		},
		{
			name: "raising a stateful delete above the floor",
			rule: policyRule("forbid-pvc-deletes-harder", agentv1alpha1.ChangePolicyClassGated,
				agentv1alpha1.ChangeRuleWhen{
					Verbs: []agentv1alpha1.ChangeVerb{"delete"},
					Kinds: kinds("", "PersistentVolumeClaim"),
				}),
		},
		{
			// `+1` can only raise, and is capped at gated, so it never needs comparing to the floor.
			name: "escalate everything",
			rule: policyRule("escalate-all", agentv1alpha1.ChangePolicyClassEscalate,
				agentv1alpha1.ChangeRuleWhen{}),
		},
		{
			// 06 §4.2's other example: a cap-only rule contributes no class at all.
			name: "tighten-fanout",
			rule: &agentv1alpha1.ChangeRule{
				ID:         "tighten-fanout",
				MaxObjects: 10,
				Reason:     "cap blast radius below the code ceiling",
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if err := ValidateChangeRule(tc.rule); err != nil {
				t.Fatalf("rule was refused but only tightens: %v", err)
			}
		})
	}
}

// The prefilter carve-out. `secret-material-egress` matches "any write that is not a Secret", which
// contains almost every policy rule anyone would write -- so if it were compared like an ordinary
// floor rule, the only admissible class in the product would be `gated`.
func TestPrefilterRulesDoNotConstrainPolicies(t *testing.T) {
	r := policyRule("elevate-configmap-writes", agentv1alpha1.ChangePolicyClassElevated,
		agentv1alpha1.ChangeRuleWhen{
			Verbs: []agentv1alpha1.ChangeVerb{"create"},
			Kinds: kinds("", "ConfigMap"),
		})
	if err := ValidateChangeRule(r); err != nil {
		t.Fatalf("a ConfigMap rule was refused, which means secret-material-egress is being treated as unconditional: %v", err)
	}
	if got := FloorRuleIDsContaining(mustRule(t, r)); containsStr(got, RuleSecretMaterialEgress) {
		t.Fatalf("secret-material-egress appears in %v; a rule whose When is a prefilter must not constrain a policy", got)
	}
}

func TestRoutineAndForbiddenAreNotPolicyClasses(t *testing.T) {
	// Both are excluded by the CRD enum too; this is the half that holds for an object stored before
	// the enum existed, and for anything constructing the type in Go.
	for _, class := range []agentv1alpha1.ChangePolicyClass{"routine", "forbidden"} {
		r := policyRule("try-"+string(class), class, agentv1alpha1.ChangeRuleWhen{
			Verbs: []agentv1alpha1.ChangeVerb{"patch"},
		})
		err := ValidateChangeRule(r)
		if err == nil {
			t.Fatalf("class %q was admitted", class)
		}
		if !strings.Contains(err.Error(), string(class)) {
			t.Fatalf("class %q: error does not say which class is the problem: %v", class, err)
		}
	}
}

func TestOwnedByLowerTierIsCodeFloorOnly(t *testing.T) {
	r := policyRule("claim-ownership", agentv1alpha1.ChangePolicyClassGated,
		agentv1alpha1.ChangeRuleWhen{OwnedByLowerTier: true})
	err := ValidateChangeRule(r)
	if err == nil {
		t.Fatal("a policy declared when.ownedByLowerTier and was admitted; ownership is computed, not declared")
	}
	if !strings.Contains(err.Error(), "ownedByLowerTier") {
		t.Fatalf("error does not name the field: %v", err)
	}
}

// The path dialect. 06 §4.2 specifies the message because the mistake is silent: `/spec/replicas`
// is a well-formed dotted path with one segment literally named "/spec/replicas".
func TestJSONPointerInFieldPathsIsRejected(t *testing.T) {
	r := policyRule("wrong-dialect", agentv1alpha1.ChangePolicyClassGated,
		agentv1alpha1.ChangeRuleWhen{FieldPaths: []string{"/spec/replicas"}})
	err := ValidateChangeRule(r)
	if err == nil {
		t.Fatal("a JSON Pointer was accepted in when.fieldPaths; it would parse as one segment and match nothing")
	}
	if !strings.Contains(err.Error(), "expected a dotted field path, not a JSON Pointer") {
		t.Fatalf("error is not the message 06 §4.2 specifies: %v", err)
	}
}

func TestPolicyMayNotReuseAFloorRuleID(t *testing.T) {
	r := policyRule(RuleSecretWrite, agentv1alpha1.ChangePolicyClassGated,
		agentv1alpha1.ChangeRuleWhen{Verbs: []agentv1alpha1.ChangeVerb{"patch"}})
	if err := ValidateChangeRule(r); err == nil {
		t.Fatalf("a policy reused the built-in id %q; two reasons behind one id make the journal unreadable", RuleSecretWrite)
	}
}

// THE HALF THAT MATTERS. A lowering rule that reaches the classifier -- webhook down, object
// written before the webhook existed, direct etcd write -- changes nothing, because step 3 takes
// the max and there is no operation in it that can subtract.
func TestALoweringPolicyIsInertEvenIfItReachesTheClassifier(t *testing.T) {
	// A routine contribution is not merely inert, it is UNREPRESENTABLE: ClassRoutine is the zero
	// Class, so Contributes(ClassRoutine) is the zero RuleClass, which is the same value as "this
	// rule contributes no class at all". There is no bit pattern in this type meaning "lower it to
	// routine", which is a stronger guarantee than any check could give -- and it is why the rule
	// below needs a MaxObjects to be a valid rule at all.
	if Contributes(ClassRoutine) != (RuleClass{}) {
		t.Fatal("a routine contribution is now distinguishable from no contribution, which means the type can express a downgrade")
	}

	lowering := RuleSet{
		Source: "hand-crafted-lowering-policy",
		Rules: []Rule{{
			ID:         "pretend-pvc-deletes-are-routine",
			When:       When{Verbs: []string{"delete"}, Kinds: []KindRef{{Group: "", Kind: "PersistentVolumeClaim"}}},
			Class:      Contributes(ClassRoutine),
			MaxObjects: 1,
			Reason:     "this rule should have been refused at admission",
		}},
	}
	c := mustClassifier(t, []RuleSet{lowering}, seenAll{})
	got := classify(t, c, input(op("delete", "", "PersistentVolumeClaim", "team-a", "data")))
	wantClass(t, got, ClassGated)

	// And the floor's reason is still the one the human is shown.
	if !hasReason(got, RuleDestructiveStatefulDelete) {
		t.Fatalf("the floor's reason is missing from %v; a lowering policy must not be able to hide why", got.Reasons)
	}
}

// The cap half of the same asymmetry: a policy raising maxObjects is accepted and never wins.
func TestAPolicyCannotRaiseTheEffectiveCap(t *testing.T) {
	generous := RuleSet{
		Source: "generous",
		Rules: []Rule{{
			ID:         "raise-the-cap",
			When:       When{},
			MaxObjects: 5000,
			Reason:     "accepted, stored, listed, and never the minimum",
		}},
	}
	strict := RuleSet{
		Source: "strict",
		Rules: []Rule{{
			ID:         "lower-the-cap",
			When:       When{},
			MaxObjects: 10,
			Reason:     "cap blast radius below the code ceiling",
		}},
	}
	c := mustClassifier(t, []RuleSet{generous, strict}, seenAll{})
	got := classify(t, c, input(op("patch", "apps", "Deployment", "team-a", "web")))
	if got.EffectiveMaxObjects != 10 {
		t.Fatalf("effective cap = %d, want 10: caps combine with min, so no source can widen another's", got.EffectiveMaxObjects)
	}
}

// A tightening policy really does tighten, and says who tightened it. Without the source name, a
// human reading a gated action cannot tell a product floor from their own policy -- the difference
// between "argue with the vendor" and "edit our ChangePolicy".
func TestATighteningPolicyRaisesAndNamesItself(t *testing.T) {
	cp := &agentv1alpha1.ChangePolicy{}
	cp.Name = "baseline-conservative"
	cp.Spec.Rules = []agentv1alpha1.ChangeRule{*policyRule(
		"gate-all-deletes-while-ramping", agentv1alpha1.ChangePolicyClassGated,
		agentv1alpha1.ChangeRuleWhen{Verbs: []agentv1alpha1.ChangeVerb{"delete"}})}

	rs, err := FromChangePolicy(cp)
	if err != nil {
		t.Fatalf("FromChangePolicy: %v", err)
	}
	c := mustClassifier(t, []RuleSet{rs}, seenAll{})

	// A delete the floor considers routine.
	got := classify(t, c, input(op("delete", "apps", "Deployment", "team-a", "web")))
	wantClass(t, got, ClassGated)
	if !containsStr(got.PolicySources, "baseline-conservative") {
		t.Fatalf("policySources = %v, want the policy's own name", got.PolicySources)
	}
	if !containsStr(got.PolicySources, "code-floor") {
		t.Fatalf("policySources = %v: the floor is always consulted and must always be listed, or a reader concludes the product had no opinion", got.PolicySources)
	}
}

// Round-tripping the CRD shape into the evaluation shape must not quietly widen a rule. A selector
// dropped in conversion is a rule that fires on more than its author wrote.
func TestConversionPreservesEveryMatchClause(t *testing.T) {
	sel := &metav1.LabelSelector{MatchLabels: map[string]string{"tier": "frontend"}}
	in := &agentv1alpha1.ChangeRule{
		ID: "everything-set",
		When: agentv1alpha1.ChangeRuleWhen{
			Verbs:             []agentv1alpha1.ChangeVerb{"patch", "apply"},
			Kinds:             kinds("apps", "Deployment"),
			ExcludeKinds:      kinds("", "ConfigMap"),
			Namespaces:        []string{"team-a"},
			NamespaceSelector: sel,
			LabelSelector:     sel,
			FieldPaths:        []string{"spec.template.spec.containers[*].image"},
			Direction:         agentv1alpha1.ChangeDirectionLoosen,
		},
		Class:      agentv1alpha1.ChangePolicyClassGated,
		MaxObjects: 7,
		Reason:     "everything at once",
	}
	got, err := FromChangeRule(in)
	if err != nil {
		t.Fatalf("FromChangeRule: %v", err)
	}
	checks := []struct {
		name string
		ok   bool
	}{
		{"verbs", len(got.When.Verbs) == 2 && got.When.Verbs[0] == "patch"},
		{"kinds", len(got.When.Kinds) == 1 && got.When.Kinds[0].Kind == "Deployment"},
		{"excludeKinds", len(got.When.ExcludeKinds) == 1},
		{"namespaces", len(got.When.Namespaces) == 1},
		{"namespaceSelector", got.When.NamespaceSelector != nil},
		{"labelSelector", got.When.LabelSelector != nil},
		{"fieldPaths", len(got.When.FieldPaths) == 1},
		{"direction", got.When.Direction == DirectionLoosen},
		{"class", got.Class == Contributes(ClassGated)},
		{"maxObjects", got.MaxObjects == 7},
		{"reason", got.Reason == "everything at once"},
	}
	for _, c := range checks {
		if !c.ok {
			t.Errorf("%s was lost or altered in conversion: %+v", c.name, got)
		}
	}

	// And the selectors are COPIES: a shared pointer would let a later edit of the CR mutate a rule
	// the classifier is already evaluating, from another goroutine.
	if got.When.LabelSelector == sel {
		t.Error("LabelSelector is the CR's own pointer, not a copy")
	}
}

func mustRule(t *testing.T, in *agentv1alpha1.ChangeRule) Rule {
	t.Helper()
	r, err := FromChangeRule(in)
	if err != nil {
		t.Fatalf("FromChangeRule: %v", err)
	}
	return r
}

func containsStr(hay []string, needle string) bool {
	for _, h := range hay {
		if h == needle {
			return true
		}
	}
	return false
}
