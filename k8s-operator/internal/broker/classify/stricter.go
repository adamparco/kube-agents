package classify

import (
	"fmt"
	"reflect"
)

// The stricter-only property of `ChangePolicy` (V-GAT-009), which is guaranteed in two places that
// fail differently and are deliberately not the same code.
//
//	IN THE BROKER   classify.go step 3 takes Max over sources and EffectiveMaxObjects takes Min.
//	                There is no operation in either that can move a result the permissive way, so a
//	                lowering policy is not refused, it is INERT. This is the guarantee that holds
//	                when the webhook is down, not yet installed, or bypassed by a direct etcd write.
//	AT ADMISSION    this file. A policy rule whose class is below what the code floor would assign
//	                for the same match is rejected, naming the floor rule.
//
// The second is redundant to the first and is still worth having, for a reason that is about
// people rather than about correctness: a policy author who writes `class: routine` next to a
// `delete PersistentVolumeClaim` and sees it accepted will conclude the gate is off. It is not, but
// they will not find that out until an approval request arrives for something they believe they
// exempted, or -- worse -- they will build a runbook around an exemption that never existed. An
// inert rule and a refused rule have the same effect on the cluster and opposite effects on what
// the operator believes about it.

// ValidatePolicyRuleStricterOnly reports why a ChangePolicy rule is not stricter-only, or nil.
//
// The test is CONTAINMENT, not overlap: a floor rule constrains the policy rule only when the floor
// rule fires for EVERY operation the policy rule fires for. Overlap would be the obvious choice and
// is wrong in a way that makes the feature useless -- `security-loosen` matches every write in a
// loosening direction, so under overlap the only admissible class for any write rule would be
// `gated`, and a customer could never express "elevate all creates in this namespace" at all.
// Under containment that rule is admitted, honestly raises routine creates to elevated, and the
// broker still gates the loosening ones because it maxes.
func ValidatePolicyRuleStricterOnly(p Rule) error {
	// `+1` and cap-only rules cannot lower anything by construction: Escalate raises by one and is
	// capped at gated, and a cap is combined with Min.
	if p.Class.Escalate || p.Class == (RuleClass{}) {
		return nil
	}
	for _, f := range CodeFloor().Rules {
		if _, prefilter := prefilterRules[f.ID]; prefilter {
			// A prefilter rule's `When` narrows candidates; a runtime condition decides. Containment in
			// its When therefore does NOT mean the floor assigns its class, so comparing against it
			// would refuse honest policies. `secret-material-egress` is the whole of this set today:
			// its When is "any write that is not a Secret", which contains almost every policy rule
			// anyone would write, and treating that as "the floor gates this" would force every policy
			// rule in the product to be `gated`.
			continue
		}
		if f.Class.Escalate || f.Class == (RuleClass{}) {
			continue
		}
		if !whenContains(f.When, p.When) {
			continue
		}
		if p.Class.Class < f.Class.Class {
			return fmt.Errorf(
				"class %s is below the code floor: every operation this rule matches is also matched by the built-in rule %q, which classifies it %s. A ChangePolicy may only make things stricter (06 §4.2); to raise this rule's reach without lowering the floor, narrow its `when` so it no longer sits entirely inside %q",
				p.Class.Class, f.ID, f.Class.Class, f.ID)
		}
	}
	return nil
}

// FloorRuleIDsContaining names the floor rules that fire for every operation a policy rule fires
// for. Exported for the webhook's warnings and for tests; the empty result is the common case.
func FloorRuleIDsContaining(p Rule) []string {
	var out []string
	for _, f := range CodeFloor().Rules {
		if _, prefilter := prefilterRules[f.ID]; prefilter {
			continue
		}
		if whenContains(f.When, p.When) {
			out = append(out, f.ID)
		}
	}
	return out
}

// whenContains reports whether every operation matched by `inner` is also matched by `outer`.
//
// CONSERVATIVE IN ONE DIRECTION, ON PURPOSE. It returns false whenever containment cannot be
// PROVEN from the two predicates alone. A false negative here means a lowering policy rule is
// admitted; that rule is then inert, because the broker maxes. A false positive would mean an
// honest tightening policy is refused with a message blaming a floor rule that does not actually
// cover it, and the author has no way to satisfy it. One failure mode is a missed warning, the
// other is a product that rejects correct input, so the doubt resolves toward admitting.
//
// Each clause reads the same way: if `outer` constrains on this axis, `inner` must constrain at
// least as tightly on the same axis.
func whenContains(outer, inner When) bool {
	if len(outer.Verbs) > 0 && !subsetOf(inner.Verbs, outer.Verbs) {
		return false
	}
	if len(outer.Kinds) > 0 && !kindsSubsetOf(inner.Kinds, outer.Kinds) {
		return false
	}
	if len(outer.ExcludeKinds) > 0 {
		// `outer` refuses these kinds, so containment requires `inner` never to reach one. An `inner`
		// with no Kinds at all matches every kind including the excluded ones, hence not contained.
		if len(inner.Kinds) == 0 {
			return false
		}
		for _, k := range inner.Kinds {
			if matchesKind(outer.ExcludeKinds, k) {
				return false
			}
		}
	}
	if outer.OwnedByLowerTier {
		// A computed fact a policy cannot state (it is rejected at admission), so no policy rule can
		// ever be contained in one that requires it. `cross-tier-direct-operation` is the only such
		// floor rule and it correctly constrains nothing here.
		return false
	}
	if len(outer.Namespaces) > 0 && !subsetOf(inner.Namespaces, outer.Namespaces) {
		return false
	}
	// Selectors are compared for structural equality rather than for logical implication. Deciding
	// "does this label selector imply that one" is real work (matchExpressions with In/NotIn/Exists
	// over overlapping key sets) and getting it subtly wrong is the false-positive failure above.
	// Equality is sound, is what a copied-then-edited rule actually looks like, and no floor rule
	// uses a selector today -- so this clause is a guard against a future floor rule, not a
	// restriction on anything shipping.
	if outer.NamespaceSelector != nil && !reflect.DeepEqual(outer.NamespaceSelector, inner.NamespaceSelector) {
		return false
	}
	if outer.LabelSelector != nil && !reflect.DeepEqual(outer.LabelSelector, inner.LabelSelector) {
		return false
	}
	if len(outer.FieldPaths) > 0 {
		if len(inner.FieldPaths) == 0 {
			return false
		}
		// Every path `inner` fires on must sit at or beneath a path `outer` fires on. The direction
		// mirrors PointerPrefixMatch: the shorter path is the broader rule, so `spec.template`
		// contains `spec.template.spec.containers[*].image` and not the other way round.
		for _, ip := range inner.FieldPaths {
			if !anyDottedPrefixOf(outer.FieldPaths, ip) {
				return false
			}
		}
	}
	if outer.Direction != DirectionAny && inner.Direction != outer.Direction {
		return false
	}
	return true
}

// subsetOf reports whether `inner` is a non-empty subset of `outer`. An EMPTY inner is not a
// subset: in `When` semantics an empty list means "any", which is the superset, not the empty set.
// This is the one place where the usual set convention is exactly backwards, and reading it as
// "{} ⊆ anything" would make every unconstrained policy rule appear contained in every floor rule.
func subsetOf(inner, outer []string) bool {
	if len(inner) == 0 {
		return false
	}
	for _, v := range inner {
		if !contains(outer, v) {
			return false
		}
	}
	return true
}

// kindsSubsetOf is subsetOf for KindRefs, with the same empty-means-any inversion.
func kindsSubsetOf(inner, outer []KindRef) bool {
	if len(inner) == 0 {
		return false
	}
	for _, k := range inner {
		if !matchesKind(outer, k) {
			return false
		}
	}
	return true
}

// anyDottedPrefixOf reports whether `path` sits at or beneath any of `prefixes`.
func anyDottedPrefixOf(prefixes []string, path string) bool {
	for _, pre := range prefixes {
		ok, err := dottedPrefixOf(pre, path)
		if err != nil {
			// An unparseable path cannot be shown to contain anything. Rule.Validate rejects these
			// separately with a message that names the offending path, so swallowing the error here
			// loses nothing a caller would have acted on.
			continue
		}
		if ok {
			return true
		}
	}
	return false
}

// dottedPrefixOf reports whether the dotted path `pre` is a prefix of the dotted path `path`,
// segment by segment. `[*]` in the prefix matches any single segment of the path.
func dottedPrefixOf(pre, path string) (bool, error) {
	pseg, err := parseDottedPath(pre)
	if err != nil {
		return false, err
	}
	tseg, err := parseDottedPath(path)
	if err != nil {
		return false, err
	}
	if len(pseg) > len(tseg) {
		return false, nil
	}
	for i, s := range pseg {
		t := tseg[i]
		if s.IsIndex && s.Index == anyIndex {
			continue
		}
		if s != t {
			return false, nil
		}
	}
	return true, nil
}
