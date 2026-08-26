package undo

import (
	"context"
	"fmt"
	"strings"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/classify"
)

// Operation is one envelope operation with its pre-state already captured.
//
// "Already captured" is the same discipline classify.ResolvedOp enforces, for the same reason: plan
// generation must be a pure function of its input so that the 09 §7.3 round-trip fixtures can pin
// it with no cluster. Everything that requires a read happens before Generate is called.
type Operation struct {
	// Verb is the envelope op: create, apply, patch, delete, scale, cloud.
	Verb string

	// Target identifies the object. UID and ResourceVersion are the values observed at
	// classification time and become the step's preconditions.
	Target agentv1alpha1.TargetRef

	// Existed reports whether the target was live before the action. It selects between the two
	// `apply` rows of the §4.3.1 table and it is not inferable from PreState being nil, because a
	// snapshot can also be absent because capturing it failed -- which is a refusal, not a create.
	Existed bool

	// PreState is the object as it was, UNSANITIZED. Generate sanitizes it, so that a caller cannot
	// hand in a half-sanitized object and get a plan whose steps disagree about which fields the
	// server owns.
	PreState *unstructured.Unstructured

	// SnapshotFailed records that the broker tried to capture PreState and could not. Distinct from
	// `PreState == nil` on purpose: for a create there is nothing to snapshot and nil is correct,
	// while for an apply nil means the plan cannot be built. Conflating them would turn a failed
	// snapshot into a confident `delete` plan against an object that already existed.
	SnapshotFailed bool

	// IsStatusTarget marks an operation against the status subresource, the one case where the
	// sanitizer keeps `status`.
	IsStatusTarget bool

	// PriorReplicas is the replica count before a `scale`, for the restore step.
	PriorReplicas *int32
}

// KindRef converts the target's group/kind into the classifier's KindRef. One conversion site: a
// group/version split that is wrong is wrong invisibly, since both halves are strings.
func (o Operation) KindRef() classify.KindRef {
	return classify.KindRef{Group: o.Target.Group, Kind: o.Target.Kind}
}

// Request is one envelope's worth of work.
type Request struct {
	Operations []Operation
	// GeneratedAt is stamped into the plan. Passed in rather than read from the clock, because a
	// generator that reads the clock cannot be fixture-tested for byte-identical output -- the same
	// rule that keeps `time` off the classifier's import allowlist.
	GeneratedAt metav1.Time
}

// Result is the plan plus the reasons it is what it is.
type Result struct {
	// Plan is always non-nil. A refusal is a plan with Strategy `none`, never a nil pointer and
	// never an error: the caller's next move is identical in both cases (raise to gated), and an
	// error return invites a caller to log-and-continue.
	Plan *agentv1alpha1.UndoPlan

	// Refusals names every operation that could not be inverted, and why. Empty when the plan is
	// complete. These strings reach a human in an approval prompt, so they say what was lost rather
	// than which function returned false.
	Refusals []string

	// Redactions records the Secret values replaced by digests, per operation index.
	Redactions map[int][]Redaction
}

// Undoable reports whether the envelope has a usable plan. This is the bool the broker feeds to
// classify.Input.UndoPlanPresent, and it is the entire interface between this package and the
// gating decision of 06 §4.2 step 6.
func (r *Result) Undoable() bool {
	return r != nil && r.Plan != nil && r.Plan.Strategy != agentv1alpha1.UndoNone
}

// Validated reports whether every step of the plan was dry-run against the API server and would
// apply. It is strictly stronger than Undoable: Validate sets the flag only on a plan it did not
// downgrade, and a downgrade sets the strategy to `none`, so Validated implies Undoable and the
// gap between them is exactly "a plan that exists and nothing checked".
//
// That gap is the one the broker shipped in for five phases. `Undoable` was the only question the
// pipeline asked, `Validate` was never called, and every record carried `validated: false` while
// `ValidateReplayable` -- the front door of both replay paths -- refuses precisely on that field.
// The plans were fine. Nothing could replay one.
func (r *Result) Validated() bool {
	return r != nil && r.Plan != nil && r.Plan.Validated
}

// Generate produces the inverse of an envelope, before the envelope runs.
//
// ALL-OR-NOTHING, and that is a decision rather than an implementation convenience. 06 §4.1 makes
// an envelope the unit of execution -- the broker "applies it as a unit and undoes it as a unit" --
// so a plan that can reverse three of four operations is not a partial plan, it is a plan that
// leaves the system in a state no one designed. One un-invertible operation refuses the whole
// envelope, which gates the whole envelope, which is the outcome a human can actually reason about.
func Generate(ctx context.Context, req Request, idx ReferenceIndex) (*Result, error) {
	if len(req.Operations) == 0 {
		return nil, fmt.Errorf("cannot generate an undo plan for an envelope with no operations")
	}
	if req.GeneratedAt.IsZero() {
		return nil, fmt.Errorf("GeneratedAt is required: an undo plan with no generation time cannot be checked against the undo window")
	}

	res := &Result{
		Plan:       &agentv1alpha1.UndoPlan{GeneratedAt: req.GeneratedAt},
		Redactions: map[int][]Redaction{},
	}

	var steps []agentv1alpha1.UndoStep
	var caveats []string
	strategies := map[agentv1alpha1.UndoStrategy]bool{}

	for i, op := range req.Operations {
		stepSet, opCaveats, strategy, refusal, reds, err := generateOne(ctx, i, op, idx)
		if err != nil {
			return nil, err
		}
		if len(reds) > 0 {
			res.Redactions[i] = reds
		}
		if refusal != "" {
			res.Refusals = append(res.Refusals, fmt.Sprintf("operations[%d] (%s %s %s): %s", i, op.Verb, describeKind(op.KindRef()), op.Target.Name, refusal))
			continue
		}
		steps = append(steps, stepSet...)
		caveats = append(caveats, opCaveats...)
		strategies[strategy] = true
	}

	if len(res.Refusals) > 0 {
		// The CEL rule on UndoPlan requires a strategy other than `none` to carry steps. The
		// converse is asserted here rather than left to the API server: a refused plan carries NO
		// steps, so a caller that ignores Strategy and replays Steps replays nothing.
		res.Plan.Strategy = agentv1alpha1.UndoNone
		res.Plan.Steps = nil
		res.Plan.Caveats = res.Refusals
		res.Plan.Validated = false
		return res, nil
	}

	res.Plan.Strategy = combineStrategies(strategies)
	res.Plan.Steps = steps
	res.Plan.Caveats = dedupe(caveats)
	return res, nil
}

// generateOne builds the steps for a single operation.
func generateOne(ctx context.Context, i int, op Operation, idx ReferenceIndex) (
	steps []agentv1alpha1.UndoStep, caveats []string, strategy agentv1alpha1.UndoStrategy, refusal string, reds []Redaction, err error,
) {
	if op.Target.Kind == "" || op.Target.Name == "" {
		return nil, nil, agentv1alpha1.UndoNone, "", nil, fmt.Errorf("operations[%d]: target needs a kind and a name", i)
	}

	if op.SnapshotFailed {
		// 06 §4.4 fail-closed table: "Cannot persist a pre-state snapshot ⇒ refuse that envelope".
		// Reached here as a refusal rather than an error so the reason travels with the plan.
		return nil, nil, agentv1alpha1.UndoNone, "the pre-state snapshot could not be captured, so there is nothing to restore from", nil, nil
	}

	strategy, err = StrategyFor(op.Verb, op.Existed)
	if err != nil {
		return nil, nil, agentv1alpha1.UndoNone, "", nil, err
	}

	switch strategy {
	case agentv1alpha1.UndoNone:
		return nil, nil, agentv1alpha1.UndoNone, fmt.Sprintf("verb %q has no inverse in the 06 §4.3.1 strategy table", op.Verb), nil, nil

	case agentv1alpha1.UndoDelete:
		// create, or an apply over an object that was not there. The inverse is to remove what was
		// added -- exact, because removing something that did not exist restores the prior state
		// precisely.
		if IsEffectful(op.KindRef()) {
			return nil, nil, agentv1alpha1.UndoNone, fmt.Sprintf(
				"creating %s starts work whose effects leave the API server; deleting the object afterwards removes the record, not the effect",
				describeKind(op.KindRef()),
			), nil, nil
		}
		return []agentv1alpha1.UndoStep{{
				Op:     "delete",
				Target: op.Target,
				// The uid precondition CANNOT be filled in here -- see BindCreatedUID. It is left
				// unset deliberately and ValidateReplayable refuses the plan until it is bound.
				Preconditions: &agentv1alpha1.UndoPrecondition{},
			}}, []string{
				fmt.Sprintf("deletes %s %s, which is only correct while it is still the object this action created", op.Target.Kind, op.Target.Name),
			}, strategy, "", nil, nil

	case agentv1alpha1.UndoRestore:
		if op.PreState == nil {
			return nil, nil, agentv1alpha1.UndoNone, "no pre-state snapshot was supplied for an operation over an object that already existed", nil, nil
		}
		sanitized, reds, serr := Sanitize(op.PreState, op.IsStatusTarget)
		if serr != nil {
			return nil, nil, agentv1alpha1.UndoNone, fmt.Sprintf("the pre-state snapshot could not be sanitized: %v", serr), nil, nil
		}
		raw, merr := toRaw(sanitized)
		if merr != nil {
			return nil, nil, agentv1alpha1.UndoNone, "", nil, fmt.Errorf("operations[%d]: %w", i, merr)
		}
		step := agentv1alpha1.UndoStep{
			Op:            "apply",
			Target:        op.Target,
			Object:        raw,
			Preconditions: preconditionsFor(op.Target),
		}
		cav := []string{
			"restores spec and metadata; server-defaulted and controller-owned fields reconverge on their own",
		}
		if op.Verb == "scale" {
			step.Op = "scale"
			if op.PriorReplicas == nil {
				return nil, nil, agentv1alpha1.UndoNone, "a scale has no recorded prior replica count to restore", nil, nil
			}
			cav = []string{
				fmt.Sprintf("scales back to %d replicas; the pods that were running before are gone and will be replaced by new ones", *op.PriorReplicas),
			}
		}
		if len(reds) > 0 {
			cav = append(cav, fmt.Sprintf(
				"%d Secret value(s) are held as digests in this record; the restorable material lives in the journal store and is verified against those digests on replay",
				len(reds)))
		}
		return []agentv1alpha1.UndoStep{step}, cav, strategy, "", reds, nil

	case agentv1alpha1.UndoRecreate:
		if op.PreState == nil {
			return nil, nil, agentv1alpha1.UndoNone, "no pre-state snapshot was supplied for a delete, so there is nothing to recreate from", nil, nil
		}
		surviving, cav, downgrade, cerr := checkRecreatable(ctx, idx, op.Target, op.KindRef())
		if cerr != nil {
			return nil, nil, agentv1alpha1.UndoNone, "", nil, cerr
		}
		if surviving == agentv1alpha1.UndoNone {
			return nil, nil, agentv1alpha1.UndoNone, downgrade, nil, nil
		}
		sanitized, reds, serr := Sanitize(op.PreState, op.IsStatusTarget)
		if serr != nil {
			return nil, nil, agentv1alpha1.UndoNone, fmt.Sprintf("the pre-state snapshot could not be sanitized: %v", serr), nil, nil
		}
		raw, merr := toRaw(sanitized)
		if merr != nil {
			return nil, nil, agentv1alpha1.UndoNone, "", nil, fmt.Errorf("operations[%d]: %w", i, merr)
		}
		return []agentv1alpha1.UndoStep{{
			Op:     "create",
			Target: op.Target,
			Object: raw,
			// No uid precondition: the object is gone, so there is no uid to match, and requiring
			// one would refuse every recreate. What protects this step instead is that `create`
			// fails if something already holds the name -- which is the same guarantee arriving
			// through the API server rather than through the plan.
			Preconditions: nil,
		}}, cav, strategy, "", reds, nil

	case agentv1alpha1.UndoInverse:
		field, ok := CloudInverseField(op.KindRef())
		if !ok {
			return nil, nil, agentv1alpha1.UndoNone, fmt.Sprintf(
				"%s exposes no documented inverse for a provider-side change; the prior value cannot be restored by calling the API again",
				describeKind(op.KindRef()),
			), nil, nil
		}
		if op.PreState == nil {
			return nil, nil, agentv1alpha1.UndoNone, "no pre-state snapshot was supplied for a cloud operation", nil, nil
		}
		sanitized, reds, serr := Sanitize(op.PreState, op.IsStatusTarget)
		if serr != nil {
			return nil, nil, agentv1alpha1.UndoNone, fmt.Sprintf("the pre-state snapshot could not be sanitized: %v", serr), nil, nil
		}
		raw, merr := toRaw(sanitized)
		if merr != nil {
			return nil, nil, agentv1alpha1.UndoNone, "", nil, fmt.Errorf("operations[%d]: %w", i, merr)
		}
		return []agentv1alpha1.UndoStep{{
				Op:            "apply",
				Target:        op.Target,
				Object:        raw,
				Preconditions: preconditionsFor(op.Target),
			}}, []string{
				fmt.Sprintf("restores spec.%s to its prior value; resources the provider destroyed in the meantime are not recreated by this call", field),
			}, strategy, "", reds, nil
	}

	return nil, nil, agentv1alpha1.UndoNone, fmt.Sprintf("no handler for strategy %q", strategy), nil, nil
}

// preconditionsFor pins the object identity the plan was generated against.
//
// The uid is what makes a restore safe to replay minutes or hours later. Without it, "apply this
// snapshot to Deployment team-x/api-gateway" is a name lookup, and a name is reused: if the
// Deployment was deleted and recreated by someone else between the action and the undo, the restore
// silently overwrites THEIR object with a snapshot of a different one. The resourceVersion is
// carried too, but as information rather than as a gate -- it moves on every controller write, so
// requiring it to match would make undo fail on nearly every live object.
func preconditionsFor(t agentv1alpha1.TargetRef) *agentv1alpha1.UndoPrecondition {
	if t.UID == "" {
		return &agentv1alpha1.UndoPrecondition{}
	}
	return &agentv1alpha1.UndoPrecondition{UID: t.UID}
}

// BindCreatedUID fills in the uid precondition for a `create`'s inverse, after execution.
//
// THE ONE PLACE THIS PACKAGE CANNOT OBEY 06 §4.3.1 LITERALLY, and the reason is a genuine tension
// in the spec rather than a shortcut here. §4.3.1 says the plan is generated at step 6, before
// execution, AND that a create's undo step carries "preconditions.uid = the UID the create
// returned". Those cannot both hold: at step 6 the object does not exist and has no uid.
//
// Resolved by splitting the two things "generated" was doing. The SHAPE of the plan -- can this be
// inverted at all, by what strategy, against what target -- is decided at step 6, which is what the
// gating decision of §4.2 step 6 actually needs. The uid is bound at step 9, when the create
// returns, by this function.
//
// What keeps the split honest is that forgetting to call it FAILS CLOSED: ValidateReplayable
// refuses any `delete` step whose uid precondition is empty, so an unbound plan cannot be replayed.
// The alternative -- deleting by name with no uid -- is precisely the "different object that
// happens to share a name" failure the pin exists to prevent, arriving through the mechanism meant
// to stop it.
func BindCreatedUID(plan *agentv1alpha1.UndoPlan, stepIndex int, uid string) error {
	if plan == nil {
		return fmt.Errorf("cannot bind a uid into a nil plan")
	}
	if stepIndex < 0 || stepIndex >= len(plan.Steps) {
		return fmt.Errorf("step index %d is out of range for a plan with %d step(s)", stepIndex, len(plan.Steps))
	}
	if uid == "" {
		return fmt.Errorf("refusing to bind an empty uid to step %d: an empty precondition is what ValidateReplayable exists to catch", stepIndex)
	}
	step := &plan.Steps[stepIndex]
	if step.Op != "delete" {
		return fmt.Errorf("step %d is a %q, not a delete: only a create's inverse takes a post-execution uid", stepIndex, step.Op)
	}
	if step.Preconditions == nil {
		step.Preconditions = &agentv1alpha1.UndoPrecondition{}
	}
	step.Preconditions.UID = uid
	return nil
}

// ValidateReplayable is the gate between a generated plan and a replayed one.
//
// Called by the undo controller before it executes anything (P9-T6). Every rule here refuses rather
// than repairs, because each one describes a plan that would do damage while reporting success.
func ValidateReplayable(plan *agentv1alpha1.UndoPlan) error {
	if plan == nil {
		return fmt.Errorf("there is no undo plan")
	}
	if plan.Strategy == agentv1alpha1.UndoNone {
		return fmt.Errorf("the undo plan's strategy is none: this action was recorded as not undoable and must not be replayed")
	}
	if len(plan.Steps) == 0 {
		return fmt.Errorf("the undo plan claims strategy %q and has no steps", plan.Strategy)
	}
	if !plan.Validated {
		return fmt.Errorf("the undo plan was never dry-run against the API server, so nothing has checked that its steps would apply")
	}
	for i, s := range plan.Steps {
		if !isReplayableOp(s.Op) {
			return fmt.Errorf("step %d has op %q, which is not one of the replayable ops (%s)",
				i, s.Op, strings.Join(ReplayableOps(), ", "))
		}
		switch s.Op {
		case "delete":
			if s.Preconditions == nil || s.Preconditions.UID == "" {
				return fmt.Errorf(
					"step %d deletes %s %s/%s with no uid precondition; a delete by name alone would remove whatever holds that name now, which may not be the object this action created (see BindCreatedUID)",
					i, s.Target.Kind, s.Target.Namespace, s.Target.Name)
			}
		case "apply", "scale":
			if s.Object == nil && s.ObjectRef == nil {
				return fmt.Errorf("step %d is an %s with no object and no objectRef: there is nothing to apply", i, s.Op)
			}
		case "create":
			if s.Object == nil && s.ObjectRef == nil {
				return fmt.Errorf("step %d recreates %s %s with no snapshot body", i, s.Target.Kind, s.Target.Name)
			}
		}
	}
	return nil
}

// ReplayableOps is the set of step ops this package emits and the replayer implements.
//
// One definition site for a set that has to be agreed on by two packages that cannot see each
// other's switch statements. Until `internal/broker/rollback` existed, this validator's default arm
// said an unknown op was one "which the replayer does not implement" -- an assertion about the
// behaviour of a component that had not been written, and which nothing could have contradicted.
// Now that both ends exist, the membership test above and the replayer's dispatch read the same
// list, and `TestReplayerImplementsEveryReplayableOp` fails if one of them grows an entry the other
// has not. LSN-040.
//
// Adding an op here is therefore a three-part change on purpose: the planner emits it, this
// validator gets its detail arm, and the replayer gets a case. Any two without the third is a red.
func ReplayableOps() []string {
	return []string{"apply", "create", "delete", "scale"}
}

func isReplayableOp(op string) bool {
	for _, want := range ReplayableOps() {
		if op == want {
			return true
		}
	}
	return false
}

// combineStrategies reduces a multi-operation envelope's strategies to the one recorded on the plan.
//
// A single strategy stays itself. A mixture is reported as `restore`, which is the honest name for
// "several different inverses, replayed in order" -- and the reason a mixture is not `none` is that
// every operation in it produced steps, so the envelope IS reversible. The strategy field is a
// label for a human; the steps are the plan.
func combineStrategies(set map[agentv1alpha1.UndoStrategy]bool) agentv1alpha1.UndoStrategy {
	delete(set, agentv1alpha1.UndoNone)
	if len(set) == 1 {
		for s := range set {
			return s
		}
	}
	if len(set) == 0 {
		return agentv1alpha1.UndoNone
	}
	return agentv1alpha1.UndoRestore
}

func toRaw(obj *unstructured.Unstructured) (*runtime.RawExtension, error) {
	b, err := obj.MarshalJSON()
	if err != nil {
		return nil, fmt.Errorf("could not serialize the sanitized snapshot: %w", err)
	}
	return &runtime.RawExtension{Raw: b}, nil
}

func dedupe(in []string) []string {
	if len(in) == 0 {
		return nil
	}
	seen := map[string]bool{}
	var out []string
	for _, s := range in {
		if !seen[s] {
			seen[s] = true
			out = append(out, s)
		}
	}
	return out
}

func splitAPIVersion(apiVersion string) (group, version string) {
	if i := strings.Index(apiVersion, "/"); i >= 0 {
		return apiVersion[:i], apiVersion[i+1:]
	}
	return "", apiVersion
}
