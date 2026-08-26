// Package scope holds the ONE scope-containment predicate this project has, and exists because
// 06 §4.2 requires the classifier's `cross-tier-direct-operation` rule to use "the same subset
// predicate V-6 uses (§1.2), reused so the two cannot drift".
//
// Before this package there was exactly one implementation and it was inline in
// internal/webhook/agent_webhook.go's validateParentCeiling, interleaved with that function's
// field.Invalid messages. Copying it into the classifier would have produced two predicates that
// agree today, and the failure mode of their disagreeing later is not a compile error or a failing
// test -- it is an admission rule and a classification rule that describe different hierarchies, so
// an object the webhook says is beneath you is one the classifier says is not yours to touch, or
// (much worse) the reverse. Hence one function, two callers, and the webhook keeps its messages by
// asking WHICH clause failed rather than by re-deciding.
//
// # What "contains" means here, exactly
//
// A scope is a three-level path: project / cluster / namespace, narrowing left to right. An empty
// field means "not narrowed at that level", i.e. ALL of them -- so {project: p} contains
// {project: p, cluster: c} and every namespace within it. Emptiness is only meaningful as a
// suffix: {project: "", cluster: c} is not a scope any tier can hold (06 §1.2 requires projectId
// on cluster-admin and below), and IsWellFormed rejects it rather than letting a hole in the
// middle be read as a wildcard. A wildcard in the middle would make a namespace-scoped agent in
// project A contain a namespace of the same name in project B.
package scope

import (
	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

// Scope is a resolved (project, cluster, namespace) triple. It is a value type on purpose: the
// predicate below must not depend on which CR, informer or envelope a scope came from.
type Scope struct {
	ProjectID   string
	ClusterName string
	Namespace   string
}

// Of reads the scope off an Agent CR, tolerating a nil spec.scope the way the webhook does.
func Of(agent *agentv1alpha1.Agent) Scope {
	if agent == nil || agent.Spec.Scope == nil {
		return Scope{}
	}
	return FromSpec(agent.Spec.Scope)
}

// FromSpec converts the CRD's ScopeSpec. Nil is the empty scope, which contains everything -- see
// IsWellFormed for why that is safe to say here and not safe to act on.
func FromSpec(s *agentv1alpha1.ScopeSpec) Scope {
	if s == nil {
		return Scope{}
	}
	return Scope{ProjectID: s.ProjectID, ClusterName: s.ClusterName, Namespace: s.Namespace}
}

// IsZero reports whether nothing is narrowed. The empty scope contains every other scope, which is
// correct for a platform agent's project-wide reach only once ProjectID is set; a truly zero scope
// reaching Contains means a caller skipped IsWellFormed.
func (s Scope) IsZero() bool { return s == Scope{} }

// IsWellFormed reports whether the narrowing is a prefix -- no empty level above a non-empty one.
// Callers that derive a scope from an authenticated identity should check this before using
// Contains, because a hole in the middle silently becomes a wildcard.
func (s Scope) IsWellFormed() bool {
	if s.Namespace != "" && (s.ClusterName == "" || s.ProjectID == "") {
		return false
	}
	if s.ClusterName != "" && s.ProjectID == "" {
		return false
	}
	return true
}

// Depth is how many levels are narrowed: 0 fleet-wide, 1 project, 2 cluster, 3 namespace. Used to
// order two scopes that contain one another (i.e. are equal) and to phrase errors.
func (s Scope) Depth() int {
	switch {
	case s.Namespace != "":
		return 3
	case s.ClusterName != "":
		return 2
	case s.ProjectID != "":
		return 1
	default:
		return 0
	}
}

// Leaf is the value of the deepest narrowed level: the namespace for a namespace-scoped agent, the
// cluster for a cluster-scoped one, the project for a project-scoped one, and "" for the fleet.
//
// 06 §5.1 builds the actor ServiceAccount name out of this (`<tier>-<leaf>-actor`), so it is an
// identity input and not a display string. Two consequences follow.
//
// It reads the DEEPEST SET level rather than switching on the tier. For a well-formed scope those
// two always agree. For a malformed one -- a hole in the middle, which IsWellFormed exists to catch
// -- they disagree, and deepest-set is the narrower of the two answers. Narrower is the safe
// direction here: it names an identity that either does not exist (the pod fails to start) or holds
// less authority, never more.
//
// It is NOT injective, and callers that need injectivity must not get it from here. Distinct scopes
// share a leaf all the time -- a `payments` namespace in two different clusters is the obvious case
// -- so a leaf identifies an identity only once the tier and the namespace it lives in are also
// fixed, which is exactly the context an actor SA name has. agentlabels.RenderScope is the
// injective rendering; this is deliberately not a second one.
func (s Scope) Leaf() string {
	switch {
	case s.Namespace != "":
		return s.Namespace
	case s.ClusterName != "":
		return s.ClusterName
	default:
		return s.ProjectID
	}
}

// Clause names the level at which a containment test failed, so a caller can phrase its own error
// without re-deriving the answer. ClauseNone means containment holds.
type Clause int

const (
	// ClauseNone means the inner scope IS contained by the outer one.
	ClauseNone Clause = iota
	// ClauseProject means the projects differ.
	ClauseProject
	// ClauseCluster means the clusters differ within the same project.
	ClauseCluster
	// ClauseNamespace means the namespaces differ within the same cluster.
	ClauseNamespace
	// ClauseEqual means the two scopes are identical -- containment holds, STRICT containment does
	// not. It is returned only by StrictlyContains, and it is a distinct clause rather than a
	// boolean because "you narrowed nothing" and "you narrowed the wrong thing" are different
	// mistakes with different fixes.
	ClauseEqual
)

// Contains reports whether outer contains inner (non-strictly: a scope contains itself).
//
// The comparison walks left to right and stops at the first level the outer scope does not narrow.
// That is what makes {project: p} contain every cluster and namespace in p without enumerating
// them, and it is why IsWellFormed matters: an outer scope with a hole would stop early at the
// hole and swallow half the tree.
func Contains(outer, inner Scope) (bool, Clause) {
	if outer.ProjectID != "" && outer.ProjectID != inner.ProjectID {
		return false, ClauseProject
	}
	if outer.ClusterName != "" && outer.ClusterName != inner.ClusterName {
		return false, ClauseCluster
	}
	if outer.Namespace != "" && outer.Namespace != inner.Namespace {
		return false, ClauseNamespace
	}
	return true, ClauseNone
}

// StrictlyContains is Contains plus inequality: the inner scope must narrow something.
//
// This is the V-6 clause "scope(C) != scope(P)" and the 06 §4.2 clause "strictly contains the
// target and is strictly contained by the caller's". An equal-scope child is an authority clone
// rather than an attenuation, and an equal-scope "lower tier owner" is the caller itself, which
// would make every agent cross-tier with respect to its own objects.
func StrictlyContains(outer, inner Scope) (bool, Clause) {
	if ok, clause := Contains(outer, inner); !ok {
		return false, clause
	}
	if outer == inner {
		return false, ClauseEqual
	}
	return true, ClauseNone
}
