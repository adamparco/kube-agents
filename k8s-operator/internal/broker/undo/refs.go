package undo

import (
	"context"
	"fmt"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/classify"
)

// InboundRef is one object that points at the target and would be left dangling by a recreate.
type InboundRef struct {
	// Ref identifies the referring object.
	Ref agentv1alpha1.TargetRef
	// Via names HOW it refers -- "ownerReference", "spec.volumes[0].persistentVolumeClaim", and so
	// on. Carried because the caveat a human reads has to be specific enough to check: "3 objects
	// reference this" is a number, and "a StatefulSet owns it" is a reason.
	Via string
}

func (r InboundRef) String() string {
	ns := r.Ref.Namespace
	if ns == "" {
		ns = "<cluster>"
	}
	return fmt.Sprintf("%s %s/%s (via %s)", r.Ref.Kind, ns, r.Ref.Name, r.Via)
}

// ReferenceIndex is the seam over "what points at this object".
//
// An interface for the same reason classify.LiveState is one: plan generation must be reproducible
// from a fixture. The 09 §7.3 round-trip corpus supplies the answer directly, so the fixtures are
// hermetic and a reviewer reading a recorded plan can see exactly which inbound references produced
// it, rather than having to reconstruct the cluster it was generated against.
type ReferenceIndex interface {
	// InboundReferences returns every object that points at the target. An implementation that
	// cannot answer returns an error; it must NOT return an empty slice, because "nothing points at
	// it" and "I could not look" are the two answers this package must never conflate.
	InboundReferences(ctx context.Context, target agentv1alpha1.TargetRef) ([]InboundRef, error)
}

// checkRecreatable decides whether a `recreate` survives, and downgrades it to `none` if not.
//
// 06 §4.3.1: "A recreated object gets a new UID, so every ownerReference, PVC binding, and external
// reference pointing at the old one is dangling. The broker detects inbound references during plan
// generation and downgrades `recreate` to `none`."
//
// The failure mode being prevented is quiet and delayed. The recreate SUCCEEDS: a new object appears
// with the right name, the right spec and the right labels, and the undo reports done. What is
// broken is everything holding the old UID -- the garbage collector sees owner references pointing
// at a UID that no longer exists and deletes the children, a bound PVC never re-binds, and the
// damage shows up minutes later as an unrelated outage. So the check is not "did the recreate
// work", it is "was recreating ever going to be enough".
//
// Returns the surviving strategy, the caveats to record, and any reason for a downgrade.
func checkRecreatable(
	ctx context.Context,
	idx ReferenceIndex,
	target agentv1alpha1.TargetRef,
	kind classify.KindRef,
) (agentv1alpha1.UndoStrategy, []string, string, error) {
	if IsNonRecreatable(kind) {
		return agentv1alpha1.UndoNone, nil, fmt.Sprintf(
			"deleting %s destroys state that no snapshot contains; recreating the object would produce an empty one with the same name",
			describeKind(kind),
		), nil
	}

	if idx == nil {
		// FAIL CLOSED, and note which of the two possible reasons this is. A generator with no
		// reference index has not proven the recreate is safe; it has failed to ask. Returning
		// `recreate` here would make the whole downgrade optional in exactly the deployment where
		// wiring was forgotten -- which is every deployment, once.
		return agentv1alpha1.UndoNone, nil, "no reference index is wired, so inbound references to the deleted object could not be checked", nil
	}

	refs, err := idx.InboundReferences(ctx, target)
	if err != nil {
		return agentv1alpha1.UndoNone, nil, fmt.Sprintf("could not determine whether anything references %s/%s: %v", target.Kind, target.Name, err), nil
	}
	if len(refs) > 0 {
		return agentv1alpha1.UndoNone, nil, fmt.Sprintf(
			"%d object(s) reference this one and would be left pointing at a UID that no longer exists: %s",
			len(refs), joinRefs(refs),
		), nil
	}

	return agentv1alpha1.UndoRecreate, []string{
		"recreates the object from its snapshot; the new object has a NEW uid, so any reference created after the snapshot was taken will not be restored",
	}, "", nil
}

func joinRefs(refs []InboundRef) string {
	const max = 3
	out := ""
	for i, r := range refs {
		if i == max {
			out += fmt.Sprintf(", and %d more", len(refs)-max)
			break
		}
		if i > 0 {
			out += ", "
		}
		out += r.String()
	}
	return out
}

// OwnerReferencesOf reads inbound owner references OUT OF THE OBJECT ITSELF.
//
// This is the half of the reference graph that needs no index: an object's own `ownerReferences`
// name the objects that own IT, which is the outbound direction and not what checkRecreatable
// wants. It is exported anyway because the executor needs it for a different question -- whether
// recreating a child before its parent exists will be garbage-collected immediately -- and putting
// the parsing in one place keeps that answer consistent with this one.
func OwnerReferencesOf(obj *unstructured.Unstructured) []agentv1alpha1.TargetRef {
	if obj == nil {
		return nil
	}
	var out []agentv1alpha1.TargetRef
	for _, o := range obj.GetOwnerReferences() {
		group, version := splitAPIVersion(o.APIVersion)
		out = append(out, agentv1alpha1.TargetRef{
			Group:     group,
			Version:   version,
			Kind:      o.Kind,
			Namespace: obj.GetNamespace(),
			Name:      o.Name,
			UID:       string(o.UID),
		})
	}
	return out
}
