package classify

import (
	"fmt"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
)

// Rule is the 06 §4.2 rule-table shape. The code floor is a list of these and a ChangePolicy
// contributes more of exactly the same shape -- "and nothing else can". That sameness is the point:
// a customer's policy is not a second, weaker language that the broker interprets differently, it
// is more rows in the table the floor already is, evaluated by the same matcher, combined by the
// same Max. There is no field here that can lower a class, which is why loosening is
// unrepresentable rather than merely forbidden (03 §5.3).
type Rule struct {
	// ID appears in classification.reasons[].rule and in the audit event. Unique across the floor
	// and every active ChangePolicy; a collision is a lint failure, not a last-one-wins.
	ID string `json:"id"`

	When When `json:"when"`

	// Class is the class this rule contributes, or ClassEscalate for the `+1` rules.
	Class RuleClass `json:"class"`

	// MaxObjects is a blast-radius cap contributed by this rule. Zero means "no opinion". A rule may
	// only LOWER the effective cap (see EffectiveMaxObjects) -- a ChangePolicy raising it would be a
	// loosening wearing a number.
	MaxObjects int `json:"maxObjects,omitempty"`

	// Reason is shown verbatim to the human who has to approve or explain the action.
	Reason string `json:"reason"`
}

// When is the match predicate. Every field is ANDed; an empty field matches everything. That
// default is the dangerous direction -- an empty `When{}` matches every operation -- and it is
// correct: `tighten-fanout` in 06 §4.2's own example is `when: {}` with a maxObjects, i.e. "cap
// everything". A rule with an empty When and a class is legal too (`gate-all-deletes-while-ramping`
// is close to it) and is exactly how a customer ramps down trust.
type When struct {
	// Verbs are envelope `op` values. Empty matches any.
	Verbs []string `json:"verbs,omitempty"`

	// Kinds are target kinds. Empty matches any.
	Kinds []KindRef `json:"kinds,omitempty"`

	// ExcludeKinds are never matched, applied AFTER Kinds. This is how `secret-material-egress`
	// says "any write that is not itself a Secret write" without enumerating every other kind.
	ExcludeKinds []KindRef `json:"excludeKinds,omitempty"`

	// OwnedByLowerTier is CODE FLOOR ONLY and is computed, never declared -- see ownership.go. It
	// is rejected on a ChangePolicy at admission, because a customer-supplied "this is owned by a
	// lower tier" would be an assertion about the hierarchy rather than a fact derived from it.
	OwnedByLowerTier bool `json:"ownedByLowerTier,omitempty"`

	// Namespaces / NamespaceSelector match the TARGET's namespace.
	Namespaces        []string              `json:"namespaces,omitempty"`
	NamespaceSelector *metav1.LabelSelector `json:"namespaceSelector,omitempty"`

	// LabelSelector is matched against the LIVE target object, not the desired state (06 §4.2).
	// That distinction is V-GAT-022: a payload that asserts `kube-agents/environment: production`
	// is a claim, and a rule that matched on it would let an agent choose its own class by writing
	// a label. Live state is the only thing the classifier believes.
	LabelSelector *metav1.LabelSelector `json:"labelSelector,omitempty"`

	// FieldPaths are DOTTED paths (see path.go). Fires when the change touches any of them.
	FieldPaths []string `json:"fieldPaths,omitempty"`

	// Direction is the security direction: `loosen` is what gates. Empty means `any`.
	Direction Direction `json:"direction,omitempty"`
}

// KindRef is a group+kind pair. `group: ""` is core, and is why this is a struct rather than a
// "group/kind" string: `"/Secret"` and `"Secret"` would both have to be accepted, and one of them
// would eventually be written as `"v1/Secret"` -- a version, in a field that has none.
type KindRef struct {
	Group string `json:"group"`
	Kind  string `json:"kind"`
}

func (k KindRef) String() string {
	if k.Group == "" {
		return k.Kind
	}
	return k.Group + "/" + k.Kind
}

// Direction is the security direction of a change.
type Direction string

const (
	// DirectionAny matches regardless of direction, and is the zero value.
	DirectionAny Direction = ""
	// DirectionLoosen is the direction that gates: a control was removed or widened.
	DirectionLoosen Direction = "loosen"
	// DirectionTighten is the direction that does not: a control was added or narrowed. 03 §5.2 --
	// "Agents are trusted to make things safer without asking, never to make them less safe."
	DirectionTighten Direction = "tighten"
)

// RuleClass is a rule's contribution: one of the four classes, or the `+1` escalation. It is a
// separate type from Class because `+1` is not a class -- it has no meaning on its own and cannot
// be the answer, only a modifier to one.
type RuleClass struct {
	// Escalate is the `+1` form. When true, Class is ignored.
	Escalate bool
	// Class is the contributed class when Escalate is false.
	Class Class
}

// ClassEscalate is the `+1` contribution.
var ClassEscalate = RuleClass{Escalate: true}

// Contributes returns the RuleClass for a fixed class.
func Contributes(c Class) RuleClass { return RuleClass{Class: c} }

func (rc RuleClass) String() string {
	if rc.Escalate {
		return "+1"
	}
	return rc.Class.String()
}

// ParseRuleClass reads the wire form, where `+1` is a legal value alongside the four class names.
func ParseRuleClass(s string) (RuleClass, error) {
	if s == "+1" {
		return ClassEscalate, nil
	}
	c, err := ParseClass(s)
	if err != nil {
		return RuleClass{}, err
	}
	return RuleClass{Class: c}, nil
}

// Validate checks a rule's own internal consistency. It does NOT check the stricter-only property
// -- that needs the code floor to compare against and lives in the ChangePolicy webhook.
func (r Rule) Validate(codeFloor bool) error {
	if r.ID == "" {
		return fmt.Errorf("rule id is required")
	}
	if r.Reason == "" {
		return fmt.Errorf("rule %q: reason is required; it is shown verbatim to the human who has to approve the action", r.ID)
	}
	if r.MaxObjects < 0 {
		return fmt.Errorf("rule %q: maxObjects must be positive", r.ID)
	}
	if !codeFloor && r.When.OwnedByLowerTier {
		return fmt.Errorf("rule %q: when.ownedByLowerTier is code-floor only; ownership is computed from the Agent hierarchy, not declared", r.ID)
	}
	for _, p := range r.When.FieldPaths {
		if err := ValidateDottedPath(p); err != nil {
			return fmt.Errorf("rule %q: when.fieldPaths[%q]: %w", r.ID, p, err)
		}
	}
	switch r.When.Direction {
	case DirectionAny, DirectionLoosen, DirectionTighten:
	default:
		return fmt.Errorf("rule %q: when.direction %q is not one of loosen, tighten, any", r.ID, r.When.Direction)
	}
	for _, v := range r.When.Verbs {
		if !knownVerbs[v] {
			return fmt.Errorf("rule %q: when.verbs[%q] is not an envelope op", r.ID, v)
		}
	}
	if r.Class == (RuleClass{}) && r.MaxObjects == 0 {
		// A rule contributing neither a class nor a cap matches things and then does nothing, which
		// reads in a policy review as a control that is present.
		return fmt.Errorf("rule %q: contributes neither a class nor a maxObjects, so it can never affect an outcome", r.ID)
	}
	return nil
}

// knownVerbs is the op set this package matches rules against. It is the envelope's op enum plus
// VerbsNotCarriedByAnEnvelopeOp, duplicated as a set here rather than imported from the broker
// package to keep this package free of a dependency cycle -- classify is imported BY the broker.
//
// The join to the definition site is TestClassifyKnownVerbsAgreeWithTheEnvelopeEnum, in the pipeline
// package because that is the lowest package that imports both. Until 2026-07-29 the comment here
// said "the corpus lint asserts the two agree" and no such lint existed, which is the
// [[LSN-041]] shape: the sentence claiming a control exists is the reason nobody writes it.
var knownVerbs = map[string]bool{
	"create": true, "apply": true, "patch": true, "delete": true, "scale": true, "cloud": true,
}

// VerbsNotCarriedByAnEnvelopeOp are the entries of knownVerbs that no `operations[].op` field can
// ever hold, each mapped to why it is nonetheless matchable. The map is exported because it is the
// declared half of a deliberate divergence: the join test requires knownVerbs to be exactly
// broker.ValidOps() plus these keys, so a verb added to either side without a written reason fails
// the build, and an entry here that becomes a real envelope op fails it too.
//
// Keeping the divergence in a variable rather than in the join test's source is the point. A verb
// that matches nothing is a rule that gates nothing, and 06 §4.2's whole premise is that a
// ChangePolicy naming a verb is a control in force -- so the exception needs a home a policy author
// can be pointed at, not a comment inside a test.
var VerbsNotCarriedByAnEnvelopeOp = map[string]string{
	"cloud": "a Config Connector write is a write, and the code floor gates it by name " +
		"(floor.go's writeVerbs). But no envelope carries `op: cloud` -- a cloud action arrives as an " +
		"ordinary verb against a *.cnrm.cloud.google.com kind, and this broker refuses every " +
		"cloudTarget outright with `cloud-target-unavailable`. So a ChangePolicy rule naming this " +
		"verb matches nothing today. That is safe only for as long as the refusal holds, which is why " +
		"TestNoCloudTargetReachesTheClassifier asserts it rather than leaving it to this sentence.",
}

// KnownVerbs returns the op set this package matches on, sorted, for the join that holds it to
// broker.ValidOps().
func KnownVerbs() []string {
	out := make([]string, 0, len(knownVerbs))
	for v := range knownVerbs {
		out = append(out, v)
	}
	// Sorted by the caller's convention rather than map order, which is randomised.
	for i := 1; i < len(out); i++ {
		for j := i; j > 0 && out[j] < out[j-1]; j-- {
			out[j], out[j-1] = out[j-1], out[j]
		}
	}
	return out
}

// Matches reports whether the rule fires for one resolved operation.
//
// Every clause is ANDed and every empty clause is true. The order below is cheapest-first: verb and
// kind are string compares on data already in hand, while the selectors need live labels the caller
// had to fetch. That ordering is a performance choice and nothing else -- the result does not
// depend on it, and a rule that matched by short-circuiting past a selector would be a bug.
func (r Rule) Matches(op *ResolvedOp) (bool, error) {
	if len(r.When.Verbs) > 0 && !contains(r.When.Verbs, op.Verb) {
		return false, nil
	}
	if len(r.When.Kinds) > 0 && !matchesKind(r.When.Kinds, op.Kind) {
		return false, nil
	}
	if len(r.When.ExcludeKinds) > 0 && matchesKind(r.When.ExcludeKinds, op.Kind) {
		return false, nil
	}
	if r.When.OwnedByLowerTier && op.LowerTierOwner == "" {
		return false, nil
	}
	if len(r.When.Namespaces) > 0 && !contains(r.When.Namespaces, op.Namespace) {
		return false, nil
	}
	if r.When.NamespaceSelector != nil {
		ok, err := selectorMatches(r.When.NamespaceSelector, op.NamespaceLabels)
		if err != nil || !ok {
			return false, err
		}
	}
	if r.When.LabelSelector != nil {
		// Live labels. A target that does not exist has none, and a selector cannot match nothing
		// into something -- a `create` is never matched by a live-object selector, which is correct:
		// there is no live object to have the label yet.
		ok, err := selectorMatches(r.When.LabelSelector, op.LiveLabels)
		if err != nil || !ok {
			return false, err
		}
	}
	if len(r.When.FieldPaths) > 0 {
		matched := false
		for _, dotted := range r.When.FieldPaths {
			for _, touched := range op.TouchedPaths {
				ok, err := PointerPrefixMatch(dotted, touched)
				if err != nil {
					return false, fmt.Errorf("rule %q: %w", r.ID, err)
				}
				if ok {
					matched = true
					break
				}
			}
			if matched {
				break
			}
		}
		if !matched {
			return false, nil
		}
	}
	if r.When.Direction != DirectionAny && r.When.Direction != op.Direction {
		return false, nil
	}
	return true, nil
}

func contains(hay []string, needle string) bool {
	for _, h := range hay {
		if h == needle {
			return true
		}
	}
	return false
}

func matchesKind(refs []KindRef, k KindRef) bool {
	for _, ref := range refs {
		// Kind comparison is case-sensitive, because Kubernetes kinds are. A rule naming
		// "persistentvolumeclaim" is a typo and matching it would teach the author that case does
		// not matter, right up until they write a rule for a CRD where it does.
		if ref.Group == k.Group && ref.Kind == k.Kind {
			return true
		}
	}
	return false
}

func selectorMatches(sel *metav1.LabelSelector, lbls map[string]string) (bool, error) {
	s, err := metav1.LabelSelectorAsSelector(sel)
	if err != nil {
		return false, fmt.Errorf("invalid label selector: %w", err)
	}
	return s.Matches(labels.Set(lbls)), nil
}

// Reason is one entry of classification.reasons[] -- the ordered explanation shown to humans.
type Reason struct {
	Rule   string `json:"rule"`
	Class  string `json:"class"`
	Detail string `json:"detail"`
}

// String renders a reason for a log line or a chat message.
func (r Reason) String() string {
	if r.Detail == "" {
		return fmt.Sprintf("%s (%s)", r.Rule, r.Class)
	}
	return fmt.Sprintf("%s (%s): %s", r.Rule, r.Class, r.Detail)
}

// RuleSet is a named list of rules -- the code floor, or one ChangePolicy's contribution. The name
// lands in classification.policySources[] so a human reading a gated action can tell whether the
// gate is the product's floor or their own policy, which is the difference between "argue with the
// vendor" and "edit our ChangePolicy".
type RuleSet struct {
	Source string
	Rules  []Rule
}

// Validate checks every rule and the uniqueness of IDs within the set.
func (rs RuleSet) Validate(codeFloor bool) error {
	seen := make(map[string]bool, len(rs.Rules))
	for _, r := range rs.Rules {
		if err := r.Validate(codeFloor); err != nil {
			return fmt.Errorf("%s: %w", rs.Source, err)
		}
		if seen[r.ID] {
			return fmt.Errorf("%s: duplicate rule id %q", rs.Source, r.ID)
		}
		seen[r.ID] = true
	}
	return nil
}

// IDs returns the rule IDs in order, for the corpus lint (V-MET-005).
func (rs RuleSet) IDs() []string {
	out := make([]string, len(rs.Rules))
	for i, r := range rs.Rules {
		out[i] = r.ID
	}
	return out
}
