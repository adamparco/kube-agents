package classify

import (
	"context"
	"fmt"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/scope"
)

// Ownership for the `cross-tier-direct-operation` rule of 06 §4.2.
//
// The rule: when a cluster-admin agent writes directly to an object that a developer-team agent
// beneath it owns, the write GATES. It is not forbidden -- the cluster admin genuinely has the
// authority (V-6 admitted the child precisely because its scope is inside the parent's) and there
// are real reasons to reach past a subordinate, starting with the subordinate being wedged. What is
// wrong is doing it silently: the lower agent is managing that namespace and is about to be
// surprised by state it did not write, and the human who owns the lower agent has no idea their
// boundary was crossed. A gate turns "reached past you" into "asked first".
//
// The predicate is scope.StrictlyContains, imported and not reimplemented, because 06 §4.2 says so
// and because a second copy would let admission and classification disagree about who is beneath
// whom. See the package doc on internal/scope for what that disagreement costs.

// OwnerLookup finds the lower-tier owner of a target. Implemented over the operator's cached Agent
// lister in production; the corpus supplies ResolvedOp.LowerTierOwner directly and never calls it.
type OwnerLookup struct {
	// Agents is the live Agent CR set, in any order.
	Agents []agentv1alpha1.Agent
}

// Find returns the name of the Agent that owns the target and sits strictly beneath the caller, or
// "" if there is none.
//
// Three conditions, all required, from 06 §4.2's "a non-terminating Agent CR whose scope strictly
// contains the target and is strictly contained by the caller's":
//
//  1. NON-TERMINATING. An Agent with a deletion timestamp is on its way out; gating a cluster
//     admin's cleanup on approval from the agent being cleaned up is a deadlock with a human in it.
//  2. CONTAINS THE TARGET, non-strictly. The candidate must be responsible for this object -- and
//     the owner of a namespace is scoped EXACTLY to that namespace, so requiring strictness here
//     finds nothing at all. 06 §4.2's "strictly contains the target" is about the target OBJECT,
//     which is a point inside a scope rather than a scope of its own; the scope that owns it is the
//     one it sits in, not a wider one. Requiring strictness here was the first implementation and
//     it silently disabled the entire rule -- no agent ever qualified, so nothing ever gated.
//  3. STRICTLY CONTAINED BY THE CALLER'S. Strict on this side, and that half genuinely matters:
//     non-strict containment would make an agent its own lower-tier owner -- the caller's scope
//     trivially contains itself -- so every agent would gate on every write it makes, which is not
//     a conservative failure, it is a broker that has stopped working.
//
// When several agents qualify (a cluster-admin and a developer-team agent both between the caller
// and a deeply-scoped target), the DEEPEST wins: it is the one actually managing the object, and
// naming the intermediate one in the approval request would send the question to the wrong human.
func (l OwnerLookup) Find(caller Caller, target scope.Scope) (string, error) {
	if !target.IsWellFormed() {
		// Named rather than merely reported, because the shape of this failure does not point at its
		// cause. The target scope is BUILT, not submitted, so a malformed one is never the caller's
		// envelope being wrong -- it is ScopeOfTarget having been handed a caller with no cluster to
		// put in it, which means the broker's own Agent CR has no `spec.harness.clusterName`.
		return "", fmt.Errorf("target scope %+v is malformed: an empty level above a non-empty one "+
			"would act as a wildcard. The target scope is derived, not submitted, so this is the "+
			"broker's own identity and not the envelope: a caller whose scope names no cluster (the "+
			"platform tier, correctly) served by a broker whose Agent CR sets no spec.harness.clusterName", target)
	}
	best := ""
	bestDepth := -1
	for i := range l.Agents {
		a := &l.Agents[i]
		if a.DeletionTimestamp != nil {
			continue
		}
		if a.Name == caller.Name {
			continue
		}
		s := scope.Of(a)
		if !s.IsWellFormed() {
			// A malformed candidate is skipped, not fatal. Admission should have rejected it; if one
			// exists anyway, refusing to classify every operation in the cluster is a worse outcome
			// than not gating on one broken CR.
			continue
		}
		if ok, _ := scope.Contains(s, target); !ok {
			continue
		}
		if ok, _ := scope.StrictlyContains(caller.Scope, s); !ok {
			continue
		}
		if d := s.Depth(); d > bestDepth {
			best, bestDepth = a.Name, d
		}
	}
	return best, nil
}

// ScopeOfTarget builds the scope a target object occupies, given the caller and the target's
// namespace.
//
// A target's project and cluster are not on the object -- they are the project and cluster the
// broker is serving. The project is the caller's, because a broker only ever serves one. The
// CLUSTER is the caller's only for the two tiers whose scope names one; see below. Only the
// namespace comes from the target, and it is assigned UNCONDITIONALLY, including when it is empty.
//
// The conditional version of that assignment -- `if namespace != "" { s.Namespace = namespace }` --
// is the bug corpus case gat-151 exists to catch, and it is a scope escape. A cluster-scoped target
// has no namespace, so the conditional left the CALLER's namespace in place and the target came out
// as the caller's own scope, which trivially contains itself. The effect: a developer-team agent
// scoped to one namespace was in-scope for every cluster-scoped object in the cluster -- every
// ClusterRole, every ValidatingWebhookConfiguration, every PersistentVolume -- and step 1 waved it
// through. Assigning unconditionally makes a cluster-scoped target {project, cluster, ""}, which a
// namespace-scoped caller does not contain, and the operation is forbidden at step 1 as it should
// be.
//
// A cluster-scoped target for a CLUSTER-ADMIN caller is that caller's own scope, which by condition
// 3 above can never be strictly contained by the caller: within one cluster, cluster-scoped objects
// are never owned by a lower tier, which is correct, because a developer-team agent's authority
// stops at its namespace.
//
// # Why the cluster is filled in from the caller's SERVING cluster
//
// 06 §1.2 gives the platform tier a scope of `projectId` alone -- its authority is the project, and
// naming a cluster in it would be wrong. So for a platform caller `caller.Scope` is {p, "", ""}, and
// `s.Namespace = namespace` on its own produced {p, "", ns}: an empty level above a non-empty one,
// which is exactly the hole `scope.IsWellFormed` exists to reject and `scope.Contains` would read as
// a wildcard. Every namespaced operation from a platform agent therefore died at step 3 with
// `target scope ... is malformed`, on every cluster, for as long as the ownership lookup has run
// against live state -- `resolveOwner` guards it with nothing but `live != nil`. Nothing below L2
// could see it: the one platform-tier unit test that reached Find passed `""` for the namespace,
// which is the single target shape that does not open the hole.
//
// The missing datum is real and is not the caller's authority: it is which cluster the target sits
// in. That is `harness.clusterName` on the broker's own Agent CR, and it is knowable for every tier
// because a broker serves one cluster. Filling it here is what makes {p, cluster-a, ns} comparable
// to a developer-team agent's {p, cluster-a, ns}, which is the whole point of condition 2 -- granted
// the hole, `Contains` failed at ClauseCluster and the cross-tier gate could not have fired for a
// platform caller even if the malformed-scope guard had let it try.
//
// UNCONDITIONALLY, again, and for the same reason the namespace assignment is unconditional. The
// conditional form -- fill the cluster only when the target is namespaced -- is the gat-151 shape
// one line up: a narrowing that looks like a no-op for the case in front of you and silently leaves
// a stale field in place for the case that is not. Here it would leave a platform agent's
// cluster-scoped writes at {p, "", ""}, which no cluster-admin scope contains, so reaching past a
// cluster-admin agent to rewrite a ClusterRole in its cluster would stay ungated. Filling it always
// gates that, which is what `cross-tier-direct-operation` says and one strictly safer than today.
//
// It does not move the blast-radius denominator: `CountWorkloadObjects` reads `s.Namespace` and
// nothing else, so a scope that gains a cluster name counts the same objects.
//
// An empty ServingCluster leaves the hole and fails closed at Find, which is the only safe
// direction: a broker that cannot say which cluster it serves cannot decide who owns the target.
func ScopeOfTarget(caller Caller, namespace string) scope.Scope {
	s := caller.Scope
	if s.ClusterName == "" {
		s.ClusterName = caller.ServingCluster
	}
	s.Namespace = namespace
	return s
}

// resolveOwner is the LiveState-facing helper the classifier calls during Resolve.
func resolveOwner(ctx context.Context, live LiveState, caller Caller, op *ResolvedOp) (string, error) {
	if live == nil {
		return "", nil
	}
	return live.LowerTierOwner(ctx, caller, op.Kind, op.Namespace, op.Name)
}
