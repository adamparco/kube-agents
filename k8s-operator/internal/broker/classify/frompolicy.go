package classify

import (
	"fmt"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// Conversion from the `ChangePolicy` CRD to the classifier's own rule table.
//
// There are two types for one concept, and that is deliberate rather than an accident of layering.
// The CRD type is a wire contract: versioned, deep-copied, CEL-validated, and something a customer
// writes. The classify.Rule is an evaluation shape: it holds a parsed RuleClass where the CRD holds
// a string, because `+1` is not a class and the classifier must not be able to treat it as one.
// Collapsing them would mean either putting kubebuilder markers on the evaluator or letting an
// unparsed string reach the Max, and the second is how a typo becomes a permissive default.
//
// Every conversion failure is returned rather than skipped. A policy with one bad rule does not
// load as a policy with the other rules -- an operator who wrote five rules and got four is worse
// off than one who got an error, because the four that loaded look like the whole policy.

// FromChangePolicy converts a ChangePolicy into a RuleSet the classifier can evaluate.
//
// The Source is the policy's own name, which lands in `classification.policySources[]`. That is the
// difference, for the human reading a gated action, between "argue with the vendor" and "edit our
// ChangePolicy" -- so it is the object's name and not a constant like "change-policy".
func FromChangePolicy(cp *agentv1alpha1.ChangePolicy) (RuleSet, error) {
	if cp == nil {
		return RuleSet{}, fmt.Errorf("nil ChangePolicy")
	}
	rs := RuleSet{Source: cp.Name, Rules: make([]Rule, 0, len(cp.Spec.Rules))}
	for i := range cp.Spec.Rules {
		r, err := FromChangeRule(&cp.Spec.Rules[i])
		if err != nil {
			return RuleSet{}, fmt.Errorf("ChangePolicy %q: rules[%d]: %w", cp.Name, i, err)
		}
		rs.Rules = append(rs.Rules, r)
	}
	return rs, nil
}

// FromChangeRule converts one CRD rule. Exported because the admission webhook validates a single
// rule at a time and must validate exactly what the broker will evaluate -- validating a different
// shape from the one that runs is how the two drift.
func FromChangeRule(in *agentv1alpha1.ChangeRule) (Rule, error) {
	out := Rule{
		ID:         in.ID,
		MaxObjects: int(in.MaxObjects),
		Reason:     in.Reason,
	}
	if in.Class != "" {
		rc, err := ParseRuleClass(string(in.Class))
		if err != nil {
			return Rule{}, fmt.Errorf("class: %w", err)
		}
		out.Class = rc
	}
	w := &in.When
	out.When = When{
		Kinds:             fromKindRefs(w.Kinds),
		ExcludeKinds:      fromKindRefs(w.ExcludeKinds),
		OwnedByLowerTier:  w.OwnedByLowerTier,
		Namespaces:        append([]string(nil), w.Namespaces...),
		NamespaceSelector: w.NamespaceSelector.DeepCopy(),
		LabelSelector:     w.LabelSelector.DeepCopy(),
		FieldPaths:        append([]string(nil), w.FieldPaths...),
	}
	for _, v := range w.Verbs {
		out.When.Verbs = append(out.When.Verbs, string(v))
	}
	switch w.Direction {
	case "", agentv1alpha1.ChangeDirectionAny:
		out.When.Direction = DirectionAny
	case agentv1alpha1.ChangeDirectionLoosen:
		out.When.Direction = DirectionLoosen
	case agentv1alpha1.ChangeDirectionTighten:
		out.When.Direction = DirectionTighten
	default:
		// Unreachable through the API server, which enforces the enum. Reached by a unit test, by a
		// stored object written before the enum existed, and by anything that constructs the type in
		// Go -- all of which are better served by an error than by a silent DirectionAny, which would
		// widen the rule rather than fail it.
		return Rule{}, fmt.Errorf("when.direction %q is not one of loosen, tighten, any", w.Direction)
	}
	return out, nil
}

func fromKindRefs(in []agentv1alpha1.KindRefSpec) []KindRef {
	if len(in) == 0 {
		return nil
	}
	out := make([]KindRef, len(in))
	for i, k := range in {
		out[i] = KindRef{Group: k.Group, Kind: k.Kind}
	}
	return out
}

// ValidateChangeRule runs every check a ChangePolicy rule must pass, in the order a policy author
// would want to hear about them: can it be read at all, is it internally consistent, is it
// stricter-only, and does it collide with a built-in.
//
// One function, called by the webhook and by the broker's policy loader both. A rule the broker
// would refuse to load but admission accepted is a policy that exists in the cluster and is not in
// effect, which is the failure mode this whole type is arranged to avoid.
func ValidateChangeRule(in *agentv1alpha1.ChangeRule) error {
	r, err := FromChangeRule(in)
	if err != nil {
		return err
	}
	// The two classes the CRD enum already excludes, re-checked here because this function is also
	// the broker's loader and a stored object predates whatever the enum says today.
	switch agentv1alpha1.ActionRiskClass(in.Class) {
	case agentv1alpha1.RiskRoutine:
		return fmt.Errorf(
			"class %q can never have an effect: the broker takes the maximum over every policy source, so a rule contributing routine changes nothing. There is no downgrade path, no exempt and no maxClass (06 §4.2) -- if the intent was to exempt this match from a gate, it cannot be expressed and would not work if it could",
			in.Class)
	case agentv1alpha1.RiskForbidden:
		return fmt.Errorf(
			"class %q is not addressable by a ChangePolicy: the forbidden set is a code constant (06 §4.2). Forbidden means no path at all, not even with a human approving, and a class with no approval path should not be reachable by editing a CR. Use gated and do not approve it -- same outcome, with a human in the loop and a record of the refusal",
			in.Class)
	}
	if err := r.Validate(false); err != nil {
		return err
	}
	if isFloorRuleID(r.ID) {
		return fmt.Errorf(
			"rule id %q is a built-in rule. Reusing it would put two different reasons behind one ID in the audit journal, and a reader of `classification.reasons[].rule` could not tell which one fired",
			r.ID)
	}
	return ValidatePolicyRuleStricterOnly(r)
}

func isFloorRuleID(id string) bool {
	for _, f := range AllFloorRuleIDs {
		if f == id {
			return true
		}
	}
	return false
}
