package classify

import (
	"context"
	"fmt"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/scope"
)

// Blast radius, 06 §4.2. Three thresholds that are easy to confuse because they all count objects:
//
//	>50  literal operations in one envelope  -> 400 at SCHEMA validation, before classification
//	>50  objects affected                    -> gated
//	>100 objects affected                    -> ABORT (not gated: no approval path)
//	     fractionOfScope > 0.5               -> ABORT
//
// The first is a different check in a different place and is listed here only so the next reader
// does not try to implement it twice. The broker rejects a 51-operation envelope with a 400 before
// this package is called; a 51-OBJECT operation (one `delete` with a selector) reaches here and
// gates.

const (
	// MaxLiteralOperations is the schema cap, enforced by the broker's envelope validation. Named
	// here so the two numbers have one definition site.
	MaxLiteralOperations = 50

	// GateObjectThreshold is the count above which an operation gates.
	GateObjectThreshold = 50

	// AbortObjectThreshold is the count above which an operation is refused outright.
	AbortObjectThreshold = 100

	// AbortScopeFraction is the fraction of a scope above which an operation is refused.
	AbortScopeFraction = 0.5

	// MinDenominator floors the blast-radius denominator.
	//
	// Without it the fraction rule inverts its own purpose in exactly the place it matters least. A
	// namespace with 3 workloads: deleting 2 of them is fractionOfScope 0.67, over the abort line,
	// so a routine cleanup in a tiny dev namespace is refused with no approval path -- while the
	// same 2 deletions in a 400-object production namespace sail through at 0.005. The fraction is
	// meant to catch "you are taking out most of something big"; the floor stops it from firing on
	// "you are touching most of something trivially small". 20 is the spec's number.
	MinDenominator = 20
)

// AbortError is returned by the classifier when an operation exceeds a hard cap. It is not a class:
// there is no `PendingApproval` for it and no reason string that a human can act on by approving.
// The broker turns it into a 422 with the counts in the body.
type AbortError struct {
	Rule    string
	Detail  string
	Objects int
	// Fraction is nil when the abort was triggered by the count alone.
	Fraction *float64
}

func (e *AbortError) Error() string {
	return fmt.Sprintf("%s: %s", e.Rule, e.Detail)
}

// ComputeBlastRadius fills in an operation's object count and scope fraction.
//
// objects is the count the broker resolved (1 for a named target, N for a selector). The
// denominator comes from LiveState and is deliberately allowed to fail.
func ComputeBlastRadius(ctx context.Context, live LiveState, s scope.Scope, objects int) BlastRadius {
	br := BlastRadius{Objects: objects}
	if live == nil {
		br.DenominatorUnavailable = "no live-state client"
		return br
	}
	total, err := live.CountWorkloadObjects(ctx, s)
	if err != nil {
		// An unavailable denominator yields a NIL fraction, never 0.0.
		//
		// This is the difference between "we could not tell how much of the scope this is" and "this
		// is none of the scope", and in Go those are the same value unless someone insists otherwise.
		// A zero here would silently satisfy `fraction <= 0.5` and disarm the abort rule during
		// exactly the conditions -- API server unreachable, cache cold, informer resyncing -- when a
		// mass deletion is most likely to be the thing going wrong.
		br.DenominatorUnavailable = err.Error()
		return br
	}
	br.FractionOfScope = fraction(objects, total)
	if br.FractionOfScope == nil {
		br.DenominatorUnavailable = fmt.Sprintf("denominator %d is not usable", total)
	}
	return br
}

// fraction applies the floor and returns nil for a nonsensical denominator.
func fraction(objects, total int) *float64 {
	if total < 0 {
		return nil
	}
	if total < MinDenominator {
		total = MinDenominator
	}
	f := float64(objects) / float64(total)
	return &f
}

// CheckHardCaps returns an AbortError when an operation is over a hard cap, or nil.
//
// Count is checked before fraction so the reason names the simpler cause when both apply. A human
// reading "affects 340 objects" acts on it faster than "affects 0.71 of the scope", and the second
// number invites an argument about the denominator.
func CheckHardCaps(op *ResolvedOp) *AbortError {
	if op.BlastRadius.Objects > AbortObjectThreshold {
		return &AbortError{
			Rule:    "blast-radius-hard-cap",
			Detail:  fmt.Sprintf("affects %d objects, over the hard cap of %d; refused outright, with no approval path", op.BlastRadius.Objects, AbortObjectThreshold),
			Objects: op.BlastRadius.Objects,
		}
	}
	if f := op.BlastRadius.FractionOfScope; f != nil && *f > AbortScopeFraction {
		return &AbortError{
			Rule:     "blast-radius-hard-cap",
			Detail:   fmt.Sprintf("affects %d objects, %.0f%% of the objects in scope, over the hard cap of %.0f%%", op.BlastRadius.Objects, *f*100, AbortScopeFraction*100),
			Objects:  op.BlastRadius.Objects,
			Fraction: f,
		}
	}
	return nil
}

// EffectiveMaxObjects combines the code floor's cap with every other source, taking the MINIMUM.
//
// Min, not max, and it is the same asymmetry as the class combinator wearing different clothes. A
// class rule can only raise; a cap rule can only lower. Both mean "no source of policy can widen
// what another source allowed", and writing it as min() here rather than as a special case in the
// ChangePolicy webhook is what makes a customer's `maxObjects: 5000` harmless: it is accepted,
// stored, shown in the policy list, and never wins.
//
// A zero from any source means "no opinion" and is skipped, which is why this cannot be a plain
// min over a slice.
func EffectiveMaxObjects(caps ...int) int {
	eff := 0
	for _, c := range caps {
		if c <= 0 {
			continue
		}
		if eff == 0 || c < eff {
			eff = c
		}
	}
	return eff
}

// ExcludedFromDenominator lists the kinds that do NOT count as workload objects.
//
// The denominator is "workload objects in scope", and the exclusions are all instances of one idea:
// do not count things the cluster creates on your behalf. Pods, ReplicaSets, ControllerRevisions and
// EndpointSlices are generated per-replica and per-revision, so counting them makes the denominator
// track replica count rather than workload count -- scale a Deployment to 200 and every fraction in
// the namespace silently drops by an order of magnitude, disarming the abort rule by doing nothing
// but scaling up. Events are worse: they are unbounded, retention-dependent, and would make the
// denominator a function of how recently the cluster was noisy.
var ExcludedFromDenominator = []KindRef{
	{Group: "", Kind: "Pod"},
	{Group: "apps", Kind: "ReplicaSet"},
	{Group: "apps", Kind: "ControllerRevision"},
	{Group: "discovery.k8s.io", Kind: "EndpointSlice"},
	{Group: "", Kind: "Event"},
	{Group: "events.k8s.io", Kind: "Event"},
}

// IsExcludedFromDenominator reports whether a kind is excluded. Objects with an ownerReference are
// excluded too, by the same reasoning applied to kinds this list cannot enumerate (a CRD's
// generated children); that check lives in the LiveState implementation, which can see the objects.
func IsExcludedFromDenominator(k KindRef) bool {
	return matchesKind(ExcludedFromDenominator, k)
}

// DenominatorMaxStaleness is the freshness bound on the count, in seconds. A cached count older
// than this is treated as unavailable rather than used: a stale denominator is a wrong fraction,
// and a wrong fraction that is too LARGE disarms the abort.
const DenominatorMaxStalenessSeconds = 60
