package classify

import "strings"

// This file implements the production-environment determination of 06 §4.2 -- the `+1` input that
// asks "is this thing production?".
//
// It is four lines of spec and the single most bug-prone paragraph in the classifier, because every
// obvious simplification is wrong in a way that fails OPEN. Reading the alias first, treating the
// labels as case-sensitive, or accepting `prod` all produce a classifier that quietly declines to
// escalate on a production cluster, and nothing about that failure is visible: the action succeeds,
// the digest says routine, and the first person to notice is the one asking why nobody approved it.

// The canonical label and its tolerated alias.
const (
	// LabelEnvironment is the canonical key. It wins.
	LabelEnvironment = "kube-agents/environment"
	// LabelEnvironmentAlias is the bare `env` convention this project tolerates because half the
	// world already uses it. Tolerated, not equal: see the ladder.
	LabelEnvironmentAlias = "env"
	// AnnotationRiskClass is the per-object class override.
	AnnotationRiskClass = "kube-agents/risk-class"
)

// ProductionSource names which rung of the ladder decided, for the reason string. A human asking
// "why was this gated?" needs to know which label on which object to change, and "it is production"
// does not tell them.
type ProductionSource string

const (
	// SourceNone means no rung matched: not production.
	SourceNone ProductionSource = ""
	// SourceObjectCanonical is the object's own kube-agents/environment.
	SourceObjectCanonical ProductionSource = "object label kube-agents/environment"
	// SourceObjectAlias is the object's env.
	SourceObjectAlias ProductionSource = "object label env"
	// SourceNamespaceCanonical is the namespace's kube-agents/environment.
	SourceNamespaceCanonical ProductionSource = "namespace label kube-agents/environment"
	// SourceNamespaceAlias is the namespace's env.
	SourceNamespaceAlias ProductionSource = "namespace label env"
)

// EnvironmentOf walks the precedence ladder of 06 §4.2 and returns the environment string and the
// rung that supplied it.
//
//  1. object    kube-agents/environment
//  2. object    env
//  3. namespace kube-agents/environment
//  4. namespace env
//
// FIRST MATCH WINS, and "match" means PRESENT, not "says production". That distinction is the whole
// reason this is a ladder and not a disjunction: an object labelled `kube-agents/environment:
// staging` inside a namespace labelled `env: production` is STAGING. The object is more specific
// and it has spoken, so the namespace's opinion is not consulted. A disjunction ("production if any
// of the four says so") would make it impossible to carve a staging namespace out of a production
// cluster -- and worse, it would make the carve-out look like it worked, since the label is
// accepted and simply ignored.
//
// Rung 1 beating rung 2 has the same shape at one level: an object carrying BOTH
// `kube-agents/environment: staging` and `env: production` is staging, because the canonical key
// wins even when it disagrees with the alias. The alias exists for objects that only have the
// alias.
func EnvironmentOf(objectLabels, namespaceLabels map[string]string) (string, ProductionSource) {
	if v, ok := lookupLabel(objectLabels, LabelEnvironment); ok {
		return v, SourceObjectCanonical
	}
	if v, ok := lookupLabel(objectLabels, LabelEnvironmentAlias); ok {
		return v, SourceObjectAlias
	}
	if v, ok := lookupLabel(namespaceLabels, LabelEnvironment); ok {
		return v, SourceNamespaceCanonical
	}
	if v, ok := lookupLabel(namespaceLabels, LabelEnvironmentAlias); ok {
		return v, SourceNamespaceAlias
	}
	return "", SourceNone
}

// lookupLabel reads a key and reports presence separately from value, so an empty-valued label
// counts as PRESENT. `kube-agents/environment: ""` is a deliberate "this object is explicitly not
// classified", and letting the ladder fall through to the namespace would override it.
func lookupLabel(lbls map[string]string, key string) (string, bool) {
	if lbls == nil {
		return "", false
	}
	v, ok := lbls[key]
	return v, ok
}

// IsProductionValue reports whether an environment label value means production.
//
// Two rules, both spelled out in 06 §4.2 and both counter-intuitive in isolation:
//
// CASE-INSENSITIVE AFTER TRIM. `Production`, ` production `, `PRODUCTION` all count. Kubernetes
// label values are case-sensitive, so this is a deliberate widening -- and it widens in the
// direction of MORE escalation, which is the only direction a tolerance is allowed to go here. A
// case-sensitive read would let `Production` (typed by a human, in a console, once) silently opt a
// cluster out of gating.
//
// `prod` IS NOT ACCEPTED. This is the one that looks like an oversight and is not. `prod` is
// ambiguous in the field -- it is used for production, and it is used as a prefix of team and
// product names (`prod-search`, `prodigy`) -- and the classifier cannot distinguish a namespace
// labelled `env: prod` meaning production from one meaning the product team. Accepting it would
// escalate work that is not production, which trains operators to approve gates reflexively, which
// is how a gate stops being a control. The mitigation is not a looser matcher, it is a lint: the
// ChangePolicy webhook warns when it sees `prod` in a selector, telling the author to write
// `production`.
func IsProductionValue(v string) bool {
	return strings.EqualFold(strings.TrimSpace(v), "production")
}

// IsProduction is the composed question the classifier actually asks.
func IsProduction(objectLabels, namespaceLabels map[string]string) (bool, ProductionSource) {
	v, src := EnvironmentOf(objectLabels, namespaceLabels)
	if src == SourceNone || !IsProductionValue(v) {
		return false, SourceNone
	}
	return true, src
}

// NearMissProdValue reports whether a value looks like an attempt to say production that this
// matcher will not accept. Used by the ChangePolicy webhook to warn, and by the corpus lint to
// assert the near-miss fixtures are present -- `prod` not being accepted is a decision that needs a
// test, because it is the sort of thing a later reader "fixes".
func NearMissProdValue(v string) bool {
	t := strings.ToLower(strings.TrimSpace(v))
	if t == "" || IsProductionValue(t) {
		return false
	}
	switch t {
	case "prod", "prd", "production-", "prodution", "producton", "live":
		return true
	}
	return false
}
