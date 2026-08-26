package classify

import (
	"errors"
	"fmt"
	"sort"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/scope"
)

// Classification is the classifier's whole output. It is stored verbatim on the ActionRecord, so
// every field here is something a human can be shown months later when asking why this happened.
type Classification struct {
	// Class is the decision.
	Class Class
	// Reasons are ordered most-serious-first and are the human-facing explanation.
	Reasons []Reason
	// PolicySources names every RuleSet that contributed, so a reader can tell a product floor from
	// their own policy.
	PolicySources []string
	// EffectiveMaxObjects is the cap that applied, 0 for none.
	EffectiveMaxObjects int
	// Abort is set when a hard cap was exceeded. Class is meaningless when it is: there is no
	// approval path, so nothing is pending.
	Abort *AbortError
}

// Classifier holds the rule sets. Constructed once per broker, with the ChangePolicy sets refreshed
// by the policy informer.
type Classifier struct {
	floor    RuleSet
	policies []RuleSet
	// knownActions answers "has this agent done this before?" for the novel-action escalation.
	knownActions ActionHistory
}

// ActionHistory answers the novel-action question. Backed by the ActionRecord journal in
// production; the corpus supplies the answer as a fixture field.
type ActionHistory interface {
	// Seen reports whether this agent has successfully performed this (verb, kind, namespace) shape
	// before. NOT this exact object -- an agent that has restarted twenty Deployments in a namespace
	// is not doing something novel when it restarts the twenty-first.
	Seen(agentName, verb string, kind KindRef, namespace string) bool
}

// AlwaysNovel is the explicit "there is no history here" value, for a caller that has no journal to
// read -- a corpus fixture, a unit test, a broker running before the journal exists.
//
// It is a TYPE and not a nil because the two answers are not the same and the nil used to give the
// wrong one. Nothing is familiar, so the `+1` fires on everything: strictly stricter, visible in the
// reasons on every record, and impossible to reach by forgetting.
type AlwaysNovel struct{}

// Seen always reports false.
func (AlwaysNovel) Seen(string, string, KindRef, string) bool { return false }

// New builds a classifier over the code floor and zero or more ChangePolicy rule sets.
//
// history is REQUIRED. It was optional until P9-T7c-3d-iii-b, and optional meant that a broker
// nobody had wired one into ran with 06 §4.2's `novel-action` escalation off -- a risk class lowered
// by an omission, which invariant 4 does not permit. A caller that genuinely has no history has
// AlwaysNovel, which is the same behaviour a nil used to produce read the other way round: not
// "nothing is novel", but "everything is".
func New(policies []RuleSet, history ActionHistory) (*Classifier, error) {
	if history == nil {
		return nil, errors.New("an ActionHistory is required: a classifier without one cannot evaluate the 06 §4.2 novel-action escalation, and omitting it silently would lower the class of every unfamiliar action; pass classify.AlwaysNovel{} to say so deliberately")
	}
	floor := CodeFloor()
	if err := floor.Validate(true); err != nil {
		// The floor failing its own validation is a build-time bug that reached runtime. Refusing to
		// start is the correct response: a broker running with a partially-valid floor is a broker
		// whose gates are unknown.
		return nil, fmt.Errorf("code floor is invalid: %w", err)
	}
	for _, p := range policies {
		if err := p.Validate(false); err != nil {
			return nil, err
		}
	}
	return &Classifier{floor: floor, policies: policies, knownActions: history}, nil
}

// Classify runs the 06 §4.2 evaluation order over an envelope and returns one classification for
// the whole thing.
//
// ONE classification for N operations, taking the max. An envelope is atomic -- 06 §5 executes it
// as a unit and undoes it as a unit -- so classifying operations independently and executing the
// routine ones while the gated one waits would tear the transaction in half. The envelope is as
// risky as its riskiest operation.
func (c *Classifier) Classify(in *Input) (*Classification, error) {
	if err := in.Validate(); err != nil {
		return nil, err
	}

	out := &Classification{Class: ClassRoutine}
	sources := map[string]bool{}

	caps := []int{in.MaxObjects}

	for i := range in.Operations {
		op := &in.Operations[i]

		// STEP 1 -- scope. Out of scope is forbidden and STOPS: no other rule is consulted, no
		// escalation applies, and the envelope is refused whole.
		//
		// First because it is the only question whose answer makes the others meaningless. An
		// operation outside the caller's authority is not a risky action that needs review; it is an
		// action this agent has no standing to propose, and running the rest of the table on it
		// would produce reasons ("this would loosen a NetworkPolicy") that invite an approver to
		// think about the merits of something they should simply refuse.
		target := ScopeOfTarget(in.Caller, op.Namespace)
		if ok, clause := scope.Contains(in.Caller.Scope, target); !ok {
			out.Class = ClassForbidden
			out.Reasons = []Reason{{
				Rule:  RuleOutOfScope,
				Class: ClassForbidden.String(),
				Detail: fmt.Sprintf("%s %s %s is outside this agent's authority (%s)",
					op.Verb, op.Kind, opTargetName(op), describeClause(clause, in.Caller.Scope, target)),
			}}
			out.PolicySources = []string{"code-floor"}
			return out, nil
		}

		// STEP 2 -- forbidden set. Same short-circuit, same reason.
		if forbidden, why := IsForbidden(op); forbidden {
			out.Class = ClassForbidden
			out.Reasons = []Reason{{
				Rule:   RuleForbiddenSet,
				Class:  ClassForbidden.String(),
				Detail: fmt.Sprintf("%s %s %s %s", op.Verb, op.Kind, opTargetName(op), why),
			}}
			out.PolicySources = []string{"code-floor"}
			return out, nil
		}

		// Hard caps abort before any class is computed. An abort is not a class and cannot be
		// approved, so producing one alongside a `gated` would offer a human a button that does
		// nothing.
		if ab := CheckHardCaps(op); ab != nil {
			out.Abort = ab
			out.Reasons = append(out.Reasons, Reason{
				Rule: ab.Rule, Class: "abort", Detail: ab.Detail,
			})
			out.PolicySources = []string{"code-floor"}
			return out, nil
		}

		res, err := c.classifyOne(in, op)
		if err != nil {
			return nil, err
		}
		out.Class = Max(out.Class, res.class)
		out.Reasons = append(out.Reasons, res.reasons...)
		caps = append(caps, res.caps...)
		for _, s := range res.sources {
			sources[s] = true
		}
	}

	// STEP 6 -- no undo plan raises to at least gated.
	//
	// Last, and applied to the envelope rather than per-operation, because undo plans are generated
	// for the envelope (P9-T4). An action that cannot be undone is one whose only remedy is a human
	// noticing in time, which is precisely the situation a gate exists for -- so the floor here is
	// gated even when every other input says routine.
	if UndoPlanGateApplies(in.DryRun, hasUndoPlan(in)) {
		if out.Class < ClassGated {
			out.Class = ClassGated
			out.Reasons = append(out.Reasons, Reason{
				Rule:   RuleNoUndoPlan,
				Class:  ClassGated.String(),
				Detail: "no undo plan could be generated for this envelope, so it cannot be rolled back automatically",
			})
		}
	}

	// The caller's voluntary gate. Raises only.
	if in.RequireApproval && out.Class < ClassGated {
		out.Class = ClassGated
		out.Reasons = append(out.Reasons, Reason{
			Rule:   "caller-requested-approval",
			Class:  ClassGated.String(),
			Detail: "the agent asked for this to be approved before it runs",
		})
	}

	if len(out.Reasons) == 0 {
		out.Reasons = []Reason{{
			Rule:   RuleDefaultRoutine,
			Class:  ClassRoutine.String(),
			Detail: "no rule matched",
		}}
	}
	sortReasons(out.Reasons)

	// The floor is always a source: it is always consulted, even when nothing in it matched, and a
	// reader who sees only their own policy listed would conclude the product had no opinion.
	sources["code-floor"] = true
	out.PolicySources = sortedKeys(sources)
	out.EffectiveMaxObjects = EffectiveMaxObjects(caps...)
	return out, nil
}

// opResult is one operation's contribution. Returned as a value rather than accumulated into the
// Classifier so that Classify is safe to call concurrently -- the broker serves many agents from
// one classifier, and a rule set that recorded "I contributed" on itself would both race and carry
// one envelope's policy sources into the next one's output.
type opResult struct {
	class   Class
	reasons []Reason
	caps    []int
	sources []string
}

// classifyOne runs steps 3, 4 and 5 for a single operation.
func (c *Classifier) classifyOne(in *Input, op *ResolvedOp) (opResult, error) {
	class := ClassRoutine
	var reasons []Reason
	var caps []int
	contributed := map[string]bool{}
	escalations := 0

	// STEP 3 -- the maximum over every matching rule, from the floor and from every policy.
	//
	// Max, over a union of sources, with no source able to contribute a lowering. This is the whole
	// of "stricter-only" (V-GAT-009): it is not enforced by checking a ChangePolicy for downgrades,
	// it is enforced by there being no operation in this loop that can reduce `class`.
	sets := append([]*RuleSet{&c.floor}, policyPtrs(c.policies)...)
	for _, rs := range sets {
		for _, r := range rs.Rules {
			ok, err := r.Matches(op)
			if err != nil {
				return opResult{}, err
			}
			if !ok {
				continue
			}
			// Some rows have a `When` that is a pre-filter and a runtime condition that decides. The
			// set of them is in floor.go so that this file and the ChangePolicy containment check
			// read the same list rather than two copies of an ID comparison.
			if cond, prefilter := prefilterRules[r.ID]; prefilter && !cond(op) {
				continue
			}
			contributed[rs.Source] = true
			if r.MaxObjects > 0 {
				caps = append(caps, r.MaxObjects)
			}
			if r.Class.Escalate {
				escalations++
				reasons = append(reasons, Reason{Rule: r.ID, Class: "+1", Detail: r.Reason})
				continue
			}
			if r.Class == (RuleClass{}) {
				continue
			}
			class = Max(class, r.Class.Class)
			reasons = append(reasons, Reason{
				Rule:   r.ID,
				Class:  r.Class.Class.String(),
				Detail: detailFor(r, op),
			})
		}
	}

	// The blast-radius gate. A count rule, not a table rule.
	if op.BlastRadius.Objects > GateObjectThreshold {
		class = Max(class, ClassGated)
		reasons = append(reasons, Reason{
			Rule:   RuleBlastRadiusCap,
			Class:  ClassGated.String(),
			Detail: fmt.Sprintf("affects %d objects, over the %d that run without approval", op.BlastRadius.Objects, GateObjectThreshold),
		})
	}

	// STEP 4 -- the `+1` escalations, CAPPED AT GATED.
	if prod, src := IsProduction(op.LiveLabels, op.NamespaceLabels); prod {
		escalations++
		reasons = append(reasons, Reason{
			Rule:   RuleProductionEnvironment,
			Class:  "+1",
			Detail: fmt.Sprintf("the target is production, per its %s", src),
		})
	}
	// `nil ||` and not `!= nil &&`. A broker that was never handed a history had the whole
	// novel-action escalation switched off, silently and in the loosening direction; New now refuses
	// a nil, and this arm is what makes the refusal unnecessary rather than load-bearing. Unknown
	// history means novel, which is the escalating answer -- see internal/broker/history.
	if c.knownActions == nil || !c.knownActions.Seen(in.Caller.Name, op.Verb, op.Kind, op.Namespace) {
		escalations++
		reasons = append(reasons, Reason{
			Rule:   RuleNovelAction,
			Class:  "+1",
			Detail: fmt.Sprintf("this agent has not done %s on a %s in %s before", op.Verb, op.Kind, namespaceOrCluster(op)),
		})
	}
	// Applied ONCE, not once per escalation. Two `+1`s do not make a `+2`: the cap at gated means
	// they would collapse to the same answer anyway for anything starting at routine, and applying
	// them repeatedly would make the count of escalations -- which is not a meaningful quantity --
	// visible in the result.
	if escalations > 0 {
		class = Escalate(class)
	}

	// STEP 5 is the ChangePolicy max, already folded into step 3's loop: policy rules ARE floor
	// rules in the same table, evaluated by the same matcher. Kept as a named step in 06 §4.2
	// because the spec describes the sources separately; there is deliberately no separate code path
	// here, since a second path is where the two would diverge.

	// The object's own override annotation. It can RAISE the class of a specific object -- a team
	// marking one Deployment "always ask me" -- and it is read from LIVE state, so an agent cannot
	// set it in the same payload it is being classified against.
	if op.ObjectClassOverride != "" {
		oc, err := ParseClass(op.ObjectClassOverride)
		if err != nil {
			// A malformed override is not ignored. Ignoring it would silently drop a control the
			// object's owner believed they had turned on; gating makes the typo visible to the person
			// who can fix it.
			class = Max(class, ClassGated)
			reasons = append(reasons, Reason{
				Rule:   RuleObjectOverride,
				Class:  ClassGated.String(),
				Detail: fmt.Sprintf("the target's %s annotation is %q, which is not a valid class", AnnotationRiskClass, op.ObjectClassOverride),
			})
		} else if oc > class {
			class = oc
			reasons = append(reasons, Reason{
				Rule:   RuleObjectOverride,
				Class:  oc.String(),
				Detail: fmt.Sprintf("the target carries %s: %s", AnnotationRiskClass, oc),
			})
		}
	}

	return opResult{class: class, reasons: reasons, caps: caps, sources: sortedKeys(contributed)}, nil
}

// detailFor renders a rule's reason with the operation's specifics appended where they help.
func detailFor(r Rule, op *ResolvedOp) string {
	switch r.ID {
	case RuleSecretMaterialEgress:
		return fmt.Sprintf("%s: %s", r.Reason, describeHits(op.SecretMaterial))
	case RuleCrossTierDirectOperation:
		return fmt.Sprintf("%s (%s owns %s)", r.Reason, op.LowerTierOwner, opTargetName(op))
	}
	return r.Reason
}

func describeHits(hits []SecretHit) string {
	parts := make([]string, 0, len(hits))
	for _, h := range hits {
		parts = append(parts, h.String())
	}
	return joinComma(parts)
}

func joinComma(parts []string) string {
	out := ""
	for i, p := range parts {
		if i > 0 {
			out += ", "
		}
		out += p
	}
	return out
}

func opTargetName(op *ResolvedOp) string {
	if op.Namespace == "" {
		return op.Name
	}
	return op.Namespace + "/" + op.Name
}

func namespaceOrCluster(op *ResolvedOp) string {
	if op.Namespace == "" {
		return "this cluster"
	}
	return op.Namespace
}

func describeClause(c scope.Clause, caller, target scope.Scope) string {
	switch c {
	case scope.ClauseProject:
		return fmt.Sprintf("its authority is project %s", caller.ProjectID)
	case scope.ClauseCluster:
		return fmt.Sprintf("its authority is cluster %s", caller.ClusterName)
	case scope.ClauseNamespace:
		return fmt.Sprintf("its authority is namespace %s, and the target is in %s", caller.Namespace, target.Namespace)
	}
	return "out of scope"
}

// sortReasons puts the most serious first, stably, so the same input always produces the same
// ordering -- a requirement of V-GAT-017's byte-identical property.
func sortReasons(rs []Reason) {
	rank := func(r Reason) int {
		switch r.Class {
		case "forbidden":
			return 0
		case "abort":
			return 1
		case "gated":
			return 2
		case "elevated":
			return 3
		case "+1":
			return 4
		default:
			return 5
		}
	}
	sort.SliceStable(rs, func(i, j int) bool {
		if a, b := rank(rs[i]), rank(rs[j]); a != b {
			return a < b
		}
		return rs[i].Rule < rs[j].Rule
	})
}

func sortedKeys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func policyPtrs(ps []RuleSet) []*RuleSet {
	out := make([]*RuleSet, len(ps))
	for i := range ps {
		out[i] = &ps[i]
	}
	return out
}

// hasUndoPlan reports whether an undo plan exists for the envelope. Until P9-T4 lands the generator,
// this reads the flag the broker sets; the corpus supplies it directly.
func hasUndoPlan(in *Input) bool { return in.UndoPlanPresent }

// UndoPlanGateApplies is 06 §4.2 step 6 as a predicate: an envelope with no usable undo plan is
// raised to at least gated, unless it is a dry run.
//
// Exported because the pipeline asks the SAME question a second time at 03 §4.1 step 6, where it
// re-checks that the class did not fall away from the plan. Two spellings of one rule is how a
// pipeline ends up 500-ing on the envelopes the classifier deliberately excused: the dry-run
// suppression lives in this line and nowhere else, so a step 6 that forgets it cannot exist.
func UndoPlanGateApplies(dryRun, undoPlanPresent bool) bool { return !dryRun && !undoPlanPresent }
