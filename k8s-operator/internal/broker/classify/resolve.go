package classify

import (
	"context"
	"fmt"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/scope"
)

// Resolve turns an envelope's operations into ResolvedOps by performing every live-state lookup the
// classification depends on, ONCE, before any rule runs.
//
// This split is what makes the classifier testable and its output reproducible. Everything below
// this line touches the cluster; everything in classify.go is a pure function of what this returns.
// The 120-200 corpus fixtures of 09 §7.1 are ResolvedOps written by hand, so they exercise the
// decision logic with no API server and no network, and they produce the same answer on every run
// forever -- which is the only way a corpus can be a regression test rather than a snapshot of one
// afternoon's cluster.
//
// It also means a classification can be RE-RUN later from the journal. The ActionRecord stores the
// resolved inputs, so the question "would this be classified the same way today?" is answerable,
// and the question "was this classified correctly at the time?" is answerable separately. Those are
// different questions and an implementation that reached for live state mid-rule could answer
// neither.

// RawOp is one operation as the envelope describes it, before resolution.
type RawOp struct {
	Verb      string
	Kind      KindRef
	Namespace string
	Name      string
	// Patch is the operation's changed-field set, in RFC 6902 shape: the submitted patch itself for
	// a JSON Patch, and for every other field-level verb the equivalent the caller computed -- the
	// diff against live state for `apply`, the leaf pointers of the body for a merge patch,
	// `/spec/replicas` for `scale`. Empty for create and delete, which set WholeObject instead.
	Patch []PatchOp
	// Payload is the decoded object for create/apply, scanned for secret material.
	Payload any
	// ObjectCount is how many objects this operation affects: 1 for a named target, N for a
	// selector-addressed one, resolved by the broker against live state before this call.
	ObjectCount int
}

// Resolve performs the lookups. A nil LiveState is legal and yields ops with no live data, which is
// how the classifier behaves in shadow mode before the informers are warm -- every live-dependent
// rule then fails to match, which is a LOOSENING, so callers in that state must treat the result as
// advisory. The broker refuses to enforce on a nil-LiveState classification.
func Resolve(ctx context.Context, live LiveState, caller Caller, raws []RawOp) ([]ResolvedOp, error) {
	out := make([]ResolvedOp, 0, len(raws))
	// Digest sets and namespace labels are fetched once per scope/namespace rather than once per
	// operation: a 50-operation envelope in one namespace should not make 50 identical List calls.
	nsCache := map[string]map[string]string{}
	var digests *DigestSet

	for i, raw := range raws {
		op := ResolvedOp{
			Verb:      raw.Verb,
			Kind:      raw.Kind,
			Namespace: raw.Namespace,
			Name:      raw.Name,
		}
		if raw.ObjectCount > 0 {
			op.BlastRadius.Objects = raw.ObjectCount
		} else {
			op.BlastRadius.Objects = 1
		}

		switch raw.Verb {
		case "create", "delete":
			op.WholeObject = true
		}
		op.TouchedPaths = TouchedPaths(raw.Patch)

		if live != nil {
			lbls, anns, exists, err := live.GetObject(ctx, raw.Kind, raw.Namespace, raw.Name)
			if err != nil {
				return nil, fmt.Errorf("operations[%d]: reading live target: %w", i, err)
			}
			op.Exists = exists
			op.LiveLabels = lbls
			if v, ok := anns[AnnotationRiskClass]; ok {
				op.ObjectClassOverride = v
			}

			if raw.Namespace != "" {
				nsLabels, cached := nsCache[raw.Namespace]
				if !cached {
					nsLabels, _, err = live.GetNamespaceLabels(ctx, raw.Namespace)
					if err != nil {
						return nil, fmt.Errorf("operations[%d]: reading namespace %q: %w", i, raw.Namespace, err)
					}
					nsCache[raw.Namespace] = nsLabels
				}
				op.NamespaceLabels = nsLabels
			}

			owner, err := resolveOwner(ctx, live, caller, &op)
			if err != nil {
				return nil, fmt.Errorf("operations[%d]: resolving lower-tier owner: %w", i, err)
			}
			op.LowerTierOwner = owner

			target := ScopeOfTarget(caller, raw.Namespace)
			op.BlastRadius = ComputeBlastRadius(ctx, live, target, op.BlastRadius.Objects)

			// The digest set is scoped to the CALLER, not to the target namespace. The rule is "a
			// Secret the caller can read", and the exfiltration shape is reading in one namespace and
			// writing in another -- so scoping the set to the write's namespace would miss precisely
			// the case worth catching.
			if digests == nil {
				digests, err = live.SecretDigests(ctx, caller.Scope)
				if err != nil {
					return nil, fmt.Errorf("resolving secret digests: %w", err)
				}
			}
			op.SecretMaterial = scanOperation(digests, raw)
		}

		op.Direction = directionOf(raw)
		out = append(out, op)
	}
	return out, nil
}

// scanOperation runs the secret-material scan over both the payload and the patch values. A patch
// is scanned per-op so the reported pointer is the one in the target document, not an index into
// the patch array -- "at /data/config" is actionable, "at /2/value" is not.
func scanOperation(ds *DigestSet, raw RawOp) []SecretHit {
	var hits []SecretHit
	if raw.Payload != nil {
		hits = append(hits, ScanPayload(ds, raw.Payload, "")...)
	}
	for _, p := range raw.Patch {
		if p.Value == nil {
			continue
		}
		hits = append(hits, ScanPayload(ds, p.Value, p.Path)...)
	}
	return hits
}

// directionOf computes the security direction of an operation from its verb, kind and patch.
func directionOf(raw RawOp) Direction {
	var changes []ControlChange
	if c, ok := DirectionOfWholeObject(raw.Verb, raw.Kind); ok {
		changes = append(changes, c)
	}
	for _, p := range raw.Patch {
		if c, ok := DirectionOfPatch(p); ok {
			changes = append(changes, c)
		}
		if b, ok := p.Value.(bool); ok {
			if d, known := DirectionOfBoolField(lastToken(p.Path), b); known {
				changes = append(changes, ControlChange{
					Control: ControlSecurityContext, Direction: d, Where: p.Path,
					Detail: fmt.Sprintf("sets %s to %v", lastToken(p.Path), b),
				})
			}
		}
	}
	return CombineDirection(changes)
}

// lastToken returns the final unescaped token of a JSON Pointer, which is the field name.
func lastToken(ptr string) string {
	toks := splitPointer(ptr)
	if len(toks) == 0 {
		return ""
	}
	return toks[len(toks)-1]
}

// ControlChangesOf exposes the per-control breakdown for the reasons attached to a `security-loosen`
// gate. A gated action must say WHICH control moved; the approver's next question is always that.
func ControlChangesOf(raw RawOp) []ControlChange {
	var changes []ControlChange
	if c, ok := DirectionOfWholeObject(raw.Verb, raw.Kind); ok {
		changes = append(changes, c)
	}
	for _, p := range raw.Patch {
		if c, ok := DirectionOfPatch(p); ok {
			changes = append(changes, c)
		}
	}
	return changes
}

// ResolveTargetScope is exported for the broker's own out-of-scope pre-check, so the two agree.
func ResolveTargetScope(caller Caller, namespace string) scope.Scope {
	return ScopeOfTarget(caller, namespace)
}
