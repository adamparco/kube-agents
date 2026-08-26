package classify

import (
	"context"
	"fmt"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/scope"
)

// Input is everything the classifier is allowed to see.
//
// Read the field list as a whitelist, because that is what it is. 06 §4.2's rule that the
// classifier never reads prose (V-GAT-017) is enforced here and nowhere else: there is no `Intent`,
// no `Rationale`, no `Requester`, no `Trigger`, no free-text field of any kind. Adding one would
// make the violation possible; not having one makes it a compile error. The corresponding negative
// test is that this package's import graph contains no inference client.
type Input struct {
	// Caller is the authenticated agent identity's scope and tier. Derived by the broker from the
	// workload identity on the connection -- NOT from `agentIdentity` in the envelope body, which is
	// a claim (06 §3.2, and the property P9-T2 shipped).
	Caller Caller

	// Operations are the envelope's resolved operations, in envelope order.
	Operations []ResolvedOp

	// DryRun suppresses execution but NOT classification: a dry-run of a forbidden action is still
	// forbidden and still emits the security event. Carried so a reason can say so.
	DryRun bool

	// RequireApproval is the caller VOLUNTARILY asking for a gate. It can raise the class to gated
	// and can never lower it, which is why it is safe to accept from the envelope at all -- unlike
	// every other envelope-supplied hint, its only reachable effect is stricter.
	RequireApproval bool

	// MaxObjects is the caller's own declared cap, same asymmetry: it may lower the effective cap,
	// never raise it past the code floor's.
	MaxObjects int

	// UndoPlanPresent reports whether an undo plan could be generated for this envelope. Set by the
	// broker from P9-T4's generator; supplied directly by the corpus. Its absence raises the class
	// to at least gated (step 6), which is why it is a plain bool and not a pointer: an unset
	// pointer would be indistinguishable from "no plan", and here the two happen to want the same
	// conservative answer, so the simpler type cannot be got wrong.
	UndoPlanPresent bool
}

// Caller is the authenticated identity.
type Caller struct {
	// Name is the Agent CR name, used in reasons and to exclude the caller from ownership lookups.
	Name string
	// Tier is platform / cluster-admin / developer-team.
	Tier string
	// Scope is the caller's authority ceiling.
	Scope scope.Scope

	// ServingCluster is the logical name of the cluster this broker serves -- `spec.harness.clusterName`
	// on its own Agent CR, which 06 §1.2 keeps separate from `spec.scope.clusterName` precisely
	// because they answer different questions. `scope.clusterName` is "which cluster is this agent's
	// AUTHORITY bounded to", and the platform tier leaves it empty because its authority is the whole
	// project. `harness.clusterName` is "which cluster is this agent RUNNING in", and every tier has
	// exactly one answer, because 06 §2.2 bounds an actor to its cluster "by being installed only
	// there".
	//
	// It is on Caller and not on Scope because it is not authority. Putting it in the scope triple
	// would narrow a platform agent's ceiling to one cluster, which is the opposite of what the tier
	// means; ScopeOfTarget is the one place the two are combined, and it combines them to describe
	// the TARGET, never the caller.
	//
	// Empty is tolerated and fails closed -- see ScopeOfTarget.
	ServingCluster string
}

// ResolvedOp is one operation with the live-state lookups already performed.
//
// "Resolved" is the load-bearing word. Rule matching must be a pure function -- the corpus of
// 09 §7.1 is 120-200 fixtures that run with no cluster and must produce byte-identical
// classifications -- so every I/O the decision depends on happens in Resolve() before any rule
// runs, and the fixtures supply the same fields directly. A rule that could reach out to the API
// server mid-match would make the corpus untestable and the classification non-reproducible for
// the human reviewing it later.
type ResolvedOp struct {
	// Verb is the envelope op: create, apply, patch, delete, scale, cloud.
	Verb string
	// Kind identifies the target type.
	Kind KindRef
	// Namespace is empty for cluster-scoped targets.
	Namespace string
	// Name is the target object name.
	Name string

	// Exists reports whether the target is live right now. A `delete` of a nonexistent object is
	// still classified (it is still an attempt), but the destructive-stateful rules read this so a
	// no-op delete does not get billed as data loss.
	Exists bool

	// LiveLabels are the target's labels AS THEY ARE, not as the payload would set them. Empty when
	// the object does not exist. V-GAT-022 turns on this field.
	LiveLabels map[string]string
	// NamespaceLabels are the target namespace's labels, live.
	NamespaceLabels map[string]string

	// TouchedPaths is the union of `path` and `from` over the operation's changed-field set, as RFC
	// 6901 pointers. For a JSON Patch that is the patch's own `path`/`from`. For every other
	// field-level verb the caller computes it -- an apply's is the diff against live state, a merge
	// patch's is the leaf pointers of the body, a scale's is `/spec/replicas` -- and hands it in via
	// RawOp.Patch. Empty for the whole-object verbs, where WholeObject is set instead.
	TouchedPaths []string
	// WholeObject marks create/delete, where "which fields changed" is not a meaningful question --
	// every field is touched. Rules with fieldPaths do not fire on these.
	//
	// `apply` is NOT one of them, deliberately. It looks like a whole-object verb because it carries
	// a whole object, but an apply that changes one field is a one-field change, and calling it a
	// whole-object change means every fieldPaths rule stops firing on the verb agents use most.
	// The broker diffs the desired object against live state before calling Resolve, so an apply
	// arrives here with a real path set (see internal/broker/pipeline, fillTouchedPaths).
	WholeObject bool

	// Direction is the security direction computed by the direction analysis (see direction.go).
	Direction Direction

	// LowerTierOwner is the name of the Agent CR that owns this target and sits strictly beneath the
	// caller, or "" if none. Computed by ownership.go via the V-6 predicate; never supplied by the
	// envelope.
	LowerTierOwner string

	// SecretMaterial names the (namespace/secret, key) pairs whose values appear somewhere in this
	// operation's payload, found by digest match. Empty is the overwhelmingly common case.
	SecretMaterial []SecretHit

	// ObjectClassOverride is the target's own `kube-agents/risk-class` annotation, live, or "" if
	// absent. Parsed and validated during Resolve so a malformed annotation fails loudly rather than
	// being read as routine.
	ObjectClassOverride string

	// BlastRadius is the object count and scope fraction for this operation.
	BlastRadius BlastRadius
}

// SecretHit is one secret-material match. The KEY is named; the VALUE never leaves this struct's
// construction, and no field here can hold it. A reason string that quoted the matched value would
// copy the secret into the journal, the chat notification and the audit log -- the three places
// most likely to be read by someone who should not have it.
type SecretHit struct {
	Namespace string
	Secret    string
	Key       string
	// Where is the JSON Pointer at which the material was found, for the reason string.
	Where string
	// Form records HOW it matched: raw, base64 or url-encoded. Useful to a human deciding whether
	// this is a leak or a legitimate reference.
	Form string
}

func (h SecretHit) String() string {
	return fmt.Sprintf("%s/%s key %q (%s form) at %s", h.Namespace, h.Secret, h.Key, h.Form, h.Where)
}

// BlastRadius is the object count for one operation and the fraction of its scope that represents.
type BlastRadius struct {
	// Objects is how many objects the operation actually affects: 1 for a named target, N for a
	// selector-addressed one.
	Objects int
	// FractionOfScope is Objects divided by the workload-object denominator, or nil when the
	// denominator could not be computed. Nil is NOT zero -- see blast.go, where an unavailable
	// denominator must not read as "affects nothing".
	FractionOfScope *float64
	// DenominatorUnavailable records why the fraction is nil, for the reason string.
	DenominatorUnavailable string
}

// LiveState is the classifier's only I/O seam, used exclusively by Resolve.
//
// It is an interface with a fake in tests and a real implementation over the operator's cached
// client in production. The corpus never constructs one: corpus fixtures are already-resolved
// ResolvedOps, which is what makes them hermetic.
type LiveState interface {
	// GetObject returns the live labels and annotations of a target, and whether it exists.
	GetObject(ctx context.Context, kind KindRef, namespace, name string) (labels, annotations map[string]string, exists bool, err error)

	// GetNamespaceLabels returns a namespace's labels. Returns exists=false for a namespace that is
	// not there, which the production ladder treats as "no namespace-level label", not as an error:
	// a create into a namespace that does not yet exist is a legitimate operation.
	GetNamespaceLabels(ctx context.Context, namespace string) (lbls map[string]string, exists bool, err error)

	// CountWorkloadObjects returns the blast-radius denominator for a scope. See blast.go for the
	// exclusion list and the staleness bound.
	CountWorkloadObjects(ctx context.Context, s scope.Scope) (int, error)

	// SecretDigests returns the digest set for a scope, for the material-egress scan.
	SecretDigests(ctx context.Context, s scope.Scope) (*DigestSet, error)

	// LowerTierOwner returns the name of the Agent CR that owns a target and is strictly between the
	// caller's scope and the target, or "" if none.
	LowerTierOwner(ctx context.Context, caller Caller, kind KindRef, namespace, name string) (string, error)
}

// Validate checks the Input's own consistency before any rule sees it. Everything here is a broker
// bug or a malformed envelope that earlier validation should have caught, so the errors are blunt.
func (in *Input) Validate() error {
	if in.Caller.Name == "" {
		return fmt.Errorf("caller identity is empty; classification requires an authenticated caller")
	}
	if !in.Caller.Scope.IsWellFormed() {
		return fmt.Errorf("caller %q has a malformed scope %+v: an empty level above a non-empty one would act as a wildcard", in.Caller.Name, in.Caller.Scope)
	}
	if len(in.Operations) == 0 {
		return fmt.Errorf("envelope has no operations")
	}
	if in.MaxObjects < 0 {
		return fmt.Errorf("maxObjects must be positive, got %d", in.MaxObjects)
	}
	for i, op := range in.Operations {
		if !knownVerbs[op.Verb] {
			return fmt.Errorf("operations[%d]: %q is not an envelope op", i, op.Verb)
		}
		if op.Kind.Kind == "" {
			return fmt.Errorf("operations[%d]: kind is required", i)
		}
	}
	return nil
}
