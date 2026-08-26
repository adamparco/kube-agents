/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package verify

import (
	"context"
	"errors"
	"fmt"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/classify"
)

// Verdict is one evaluation of a per-kind predicate.
type Verdict string

const (
	// VerdictSatisfied is the only outcome that means the action worked.
	VerdictSatisfied Verdict = "Satisfied"
	// VerdictPending is "not yet" -- keep polling until the settle window closes.
	VerdictPending Verdict = "Pending"
	// VerdictFailed is "and it is not going to" -- a terminal cause was observed.
	VerdictFailed Verdict = "Failed"
	// VerdictIndeterminate is "this predicate could not evaluate its property".
	//
	// It is NOT a pass, and the driver never treats it as one. That is the same rule the harness
	// applies to its own checks: a check that could not run its property is deferred with a named
	// blocker, never green. Here the consequence is stronger than a report -- an indeterminate
	// predicate that is still indeterminate when the window closes rolls the action back, because
	// "we could not confirm it worked" and "it worked" must not be the same outcome for a system
	// whose whole claim is that it verifies.
	VerdictIndeterminate Verdict = "Indeterminate"
)

// Evaluation is one predicate's answer.
type Evaluation struct {
	Verdict Verdict
	// Name is the stable predicate identifier recorded in `status.verification.checks[].name`.
	Name string
	// Detail is the evidence, e.g. "3/3 replicas available, observedGeneration 7".
	Detail string
	// Cause is set on Pending and Failed. It is what the recovery ladder is driven from.
	Cause Cause
}

// ErrProbeUnsupported is returned by a Prober that cannot perform a capability. It produces
// VerdictIndeterminate rather than an error, because "this deployment has no connectivity prober"
// is a configuration fact about the broker, not a failure of the action.
var ErrProbeUnsupported = errors.New("probe capability not available in this broker")

// ErrTargetReplaced is returned by a Prober asked for an object whose name is now held by a
// DIFFERENT object -- same kind, same namespace, same name, different uid. Every row but one reads
// that as evidence about the wrong object and must not evaluate it; absencePredicate reads it as
// the target having gone away, which is what it was verifying.
//
// It is a sentinel rather than a string the predicates match on because the only implementation
// that can detect it lives in `internal/broker/probe`, which imports this package, so the shared
// vocabulary has to be declared here for both sides to name it.
var ErrTargetReplaced = errors.New("the object this action targeted was replaced")

// ConnectivityProbe is one leg of the NetworkPolicy affirmative probe. Both legs are required:
// asserting only that the denied path is refused passes for a policy that blocks everything.
type ConnectivityProbe struct {
	// From and To identify the endpoints, in whatever form the prober implementation understands.
	From, To string
	// Port is the destination port.
	Port int32
	// WantReachable is the expected answer. A probe set with no reachable leg is refused.
	WantReachable bool
}

// AccessQuery is one SubjectAccessReview the RBAC predicate must see answered a particular way.
type AccessQuery struct {
	User      string
	Groups    []string
	Verb      string
	Group     string
	Resource  string
	Namespace string
	Name      string
	// WantAllowed is the intended answer -- "the intended answer", not "allowed" (04 §5.1). A
	// removal of permission verifies by the review returning NO.
	WantAllowed bool
}

// ProviderStatus is a cloud provider's own view of a node pool or cluster.
type ProviderStatus struct {
	// State is the provider's reported state, e.g. RUNNING.
	State string
	// AtTargetState is whether State equals what the action asked for.
	AtTargetState bool
	// NodesReady and NodesExpected are the Kubernetes-side half. 04 §5.1 requires BOTH: a provider
	// that reports RUNNING while no node has registered is the failure this row exists to catch.
	NodesReady, NodesExpected int
}

// Prober is everything the per-kind predicates need beyond the target object itself.
//
// It is one interface rather than seven because a predicate set is configured once, at broker
// startup, and a caller assembling seven optional dependencies gets six of them right. An
// implementation that cannot do a capability returns ErrProbeUnsupported and the predicate says
// Indeterminate -- which is visible, unlike a nil function pointer.
type Prober interface {
	// Get returns live state for a target.
	Get(ctx context.Context, ref agentv1alpha1.TargetRef) (*unstructured.Unstructured, error)

	// RestartCount is the total container restarts across the pods of a workload. Used for the
	// "no new restarts across the settle window" half of the Deployment/StatefulSet row.
	RestartCount(ctx context.Context, ref agentv1alpha1.TargetRef) (int64, error)

	// EndpointCount is the number of ready endpoints backing a Service.
	EndpointCount(ctx context.Context, ref agentv1alpha1.TargetRef) (int, error)

	// ProgrammedAddress is the address the dataplane actually programmed (a LoadBalancer ingress
	// IP, an Ingress address, a Gateway address). Empty means not programmed yet.
	ProgrammedAddress(ctx context.Context, ref agentv1alpha1.TargetRef) (string, error)

	// Connectivity runs one affirmative probe and reports whether the path was reachable.
	Connectivity(ctx context.Context, p ConnectivityProbe) (bool, error)

	// AdmissionEnforcing reports whether admission is observably enforcing the object -- a quota
	// whose `status.used` is being maintained, or a LimitRange the API server applies.
	AdmissionEnforcing(ctx context.Context, ref agentv1alpha1.TargetRef) (bool, error)

	// ProviderState is the cloud provider's own answer for a node pool or cluster.
	ProviderState(ctx context.Context, ref agentv1alpha1.TargetRef) (ProviderStatus, error)

	// AccessReview runs a SubjectAccessReview and reports whether the request would be allowed.
	AccessReview(ctx context.Context, q AccessQuery) (bool, error)
}

// Target is one object to verify, plus whatever extra evidence its row of 04 §5.1 requires.
type Target struct {
	Ref agentv1alpha1.TargetRef

	// Probes is required for the NetworkPolicy row.
	Probes []ConnectivityProbe
	// Access is required for the RBAC row.
	Access []AccessQuery
	// Capacity is the T-10 discriminator, passed through to CauseOf.
	Capacity CapacitySignal

	// BaselineRestarts is the restart total observed BEFORE the action. "No new restarts" is a
	// comparison, and a predicate handed no baseline cannot make it -- so it says so rather than
	// comparing against zero, which would fail every verification of an already-crashlooping app
	// and pass none of the ones that started crashlooping because of this change.
	//
	// CaptureRestartBaselines fills it, and must be called before the action mutates anything.
	BaselineRestarts *int64

	// ExpectAbsent inverts the target's row: the action's success condition is that the object is
	// GONE. Every row of 04 §5.1 asserts something about a live object, and `mustGet` maps NotFound
	// to VerdictFailed -- correctly, for a create or an apply, where absence after the action means
	// the write did not land. For a delete it is exactly backwards, and a delete whose object is
	// dutifully absent would be reported as failed and rolled back by recreating it.
	ExpectAbsent bool
}

// Predicate evaluates one row of the 04 §5.1 table.
type Predicate func(ctx context.Context, p Prober, t Target) Evaluation

// predicateFor picks the row for one target. Kind chooses it, except when the action's success
// condition is the object's absence -- see Target.ExpectAbsent. This is the only chooser the driver
// calls; PredicateFor stays exported for the callers that hold a bare ref and no action.
func predicateFor(t Target) Predicate {
	if t.ExpectAbsent {
		return absencePredicate
	}
	return PredicateFor(t.Ref)
}

// PredicateFor returns the predicate for a target's kind. Every kind resolves to something: the
// last row of 04 §5.1 is the custom-resource fallback, and a kind with no row would otherwise
// verify by returning 200, which is the exact thing the section opens by forbidding.
func PredicateFor(ref agentv1alpha1.TargetRef) Predicate {
	if p, ok := predicateByKind[classify.KindRef{Group: ref.Group, Kind: ref.Kind}]; ok {
		return p
	}
	if p, ok := predicateByKindAnyGroup[ref.Kind]; ok {
		return p
	}
	return customResourcePredicate
}

// predicateByKind is the 04 §5.1 table keyed exactly. Group is significant: a `Gateway` in
// `gateway.networking.k8s.io` is the row below, a `Gateway` in a service mesh's own group is not.
var predicateByKind = map[classify.KindRef]Predicate{
	{Group: "apps", Kind: "Deployment"}:  workloadPredicate,
	{Group: "apps", Kind: "StatefulSet"}: workloadPredicate,
	{Group: "apps", Kind: "DaemonSet"}:   daemonSetPredicate,

	{Group: "", Kind: "Service"}:                                          reachabilityPredicate,
	{Group: "networking.k8s.io", Kind: "Ingress"}:                         reachabilityPredicate,
	{Group: "gateway.networking.k8s.io", Kind: "Gateway"}:                 reachabilityPredicate,
	{Group: "gateway.networking.k8s.io", Kind: "HTTPRoute"}:               reachabilityPredicate,
	{Group: "networking.k8s.io", Kind: "NetworkPolicy"}:                   connectivityPredicate,
	{Group: "crd.projectcalico.org", Kind: "NetworkPolicy"}:               connectivityPredicate,
	{Group: "cilium.io", Kind: "CiliumNetworkPolicy"}:                     connectivityPredicate,
	{Group: "", Kind: "ResourceQuota"}:                                    enforcementPredicate,
	{Group: "", Kind: "LimitRange"}:                                       enforcementPredicate,
	{Group: "container.cnrm.cloud.google.com", Kind: "ContainerNodePool"}: providerPredicate,
	{Group: "container.cnrm.cloud.google.com", Kind: "ContainerCluster"}:  providerPredicate,
}

// predicateByKindAnyGroup covers the RBAC row, whose four kinds all live in one group but whose
// aggregated equivalents do not always. Consulted only after the exact table misses.
var predicateByKindAnyGroup = map[string]Predicate{
	"Role":               accessPredicate,
	"RoleBinding":        accessPredicate,
	"ClusterRole":        accessPredicate,
	"ClusterRoleBinding": accessPredicate,
}

// --- the rows ---------------------------------------------------------------------------------

// workloadPredicate is the Deployment / StatefulSet row: observedGeneration caught up, desired
// replicas Available, and no new restarts across the settle window.
func workloadPredicate(ctx context.Context, p Prober, t Target) Evaluation {
	const name = "rollout-complete"
	obj, ev := mustGet(ctx, p, t, name)
	if obj == nil {
		return ev
	}

	gen, _, _ := unstructured.NestedInt64(obj.Object, "metadata", "generation")
	observed, _, _ := unstructured.NestedInt64(obj.Object, "status", "observedGeneration")
	if observed < gen {
		return Evaluation{VerdictPending, name,
			fmt.Sprintf("observedGeneration %d has not caught up to generation %d", observed, gen),
			CauseDependencyConverging}
	}

	desired, found, _ := unstructured.NestedInt64(obj.Object, "spec", "replicas")
	if !found {
		desired = 1 // the API default; an absent spec.replicas means one.
	}
	available, _, _ := unstructured.NestedInt64(obj.Object, "status", "availableReplicas")
	if available < desired {
		return Evaluation{VerdictPending, name,
			fmt.Sprintf("%d/%d replicas available", available, desired),
			pendingCause(obj, t.Capacity)}
	}

	// "No new restarts" is the half that distinguishes a rollout that completed from one that
	// completed and is now crashlooping -- the availability numbers look identical for the first
	// few minutes of both.
	if t.BaselineRestarts == nil {
		return Evaluation{VerdictIndeterminate, name,
			fmt.Sprintf("%d/%d replicas available, but no pre-action restart baseline was captured, "+
				"so 'no new restarts across the settle window' cannot be evaluated", available, desired),
			CauseUnknown}
	}
	now, err := p.RestartCount(ctx, t.Ref)
	if err != nil {
		return probeFailure(name, "restart count", err)
	}
	if now > *t.BaselineRestarts {
		return Evaluation{VerdictPending, name,
			fmt.Sprintf("%d/%d replicas available but restarts rose from %d to %d during the settle window",
				available, desired, *t.BaselineRestarts, now),
			CauseDependencyConverging}
	}

	return Evaluation{VerdictSatisfied, name,
		fmt.Sprintf("observedGeneration %d, %d/%d replicas available, no new restarts (%d)",
			observed, available, desired, now), ""}
}

// daemonSetPredicate is the DaemonSet row: desired == ready on all eligible nodes.
func daemonSetPredicate(ctx context.Context, p Prober, t Target) Evaluation {
	const name = "daemonset-ready"
	obj, ev := mustGet(ctx, p, t, name)
	if obj == nil {
		return ev
	}
	desired, _, _ := unstructured.NestedInt64(obj.Object, "status", "desiredNumberScheduled")
	ready, _, _ := unstructured.NestedInt64(obj.Object, "status", "numberReady")
	gen, _, _ := unstructured.NestedInt64(obj.Object, "metadata", "generation")
	observed, _, _ := unstructured.NestedInt64(obj.Object, "status", "observedGeneration")
	if observed < gen {
		return Evaluation{VerdictPending, name,
			fmt.Sprintf("observedGeneration %d has not caught up to generation %d", observed, gen),
			CauseDependencyConverging}
	}
	if ready < desired {
		return Evaluation{VerdictPending, name,
			fmt.Sprintf("%d/%d eligible nodes ready", ready, desired), pendingCause(obj, t.Capacity)}
	}
	return Evaluation{VerdictSatisfied, name,
		fmt.Sprintf("%d/%d eligible nodes ready", ready, desired), ""}
}

// reachabilityPredicate is the Service / Ingress / Gateway row: endpoints populated AND the
// programmed address resolvable. Both, because an address with no backends serves errors and
// backends with no address serve nobody.
func reachabilityPredicate(ctx context.Context, p Prober, t Target) Evaluation {
	const name = "endpoints-and-address"
	if _, ev := mustGet(ctx, p, t, name); ev.Verdict != "" && ev.Verdict != VerdictSatisfied {
		return ev
	}

	n, err := p.EndpointCount(ctx, t.Ref)
	if err != nil {
		return probeFailure(name, "endpoint count", err)
	}
	if n == 0 {
		return Evaluation{VerdictPending, name, "no ready endpoints", CauseDependencyConverging}
	}

	addr, err := p.ProgrammedAddress(ctx, t.Ref)
	if err != nil {
		return probeFailure(name, "programmed address", err)
	}
	if addr == "" {
		return Evaluation{VerdictPending, name,
			fmt.Sprintf("%d ready endpoints but no programmed address yet", n),
			CauseDependencyConverging}
	}
	return Evaluation{VerdictSatisfied, name,
		fmt.Sprintf("%d ready endpoints, programmed address %s", n, addr), ""}
}

// connectivityPredicate is the NetworkPolicy row: an affirmative probe -- allowed path reachable,
// denied path refused.
func connectivityPredicate(ctx context.Context, p Prober, t Target) Evaluation {
	const name = "connectivity-probe"

	// A probe set that only checks denial passes for a policy that blocks everything, which is the
	// failure mode of every allowlist ever written. Requiring one of each is what makes the word
	// "affirmative" in 04 §5.1 mean something.
	var wantReachable, wantRefused int
	for _, pr := range t.Probes {
		if pr.WantReachable {
			wantReachable++
		} else {
			wantRefused++
		}
	}
	if wantReachable == 0 || wantRefused == 0 {
		return Evaluation{VerdictIndeterminate, name,
			fmt.Sprintf("an affirmative connectivity probe needs both directions; got %d reachable "+
				"and %d refused legs", wantReachable, wantRefused), CauseUnknown}
	}

	for _, pr := range t.Probes {
		got, err := p.Connectivity(ctx, pr)
		if err != nil {
			return probeFailure(name, "connectivity", err)
		}
		if got != pr.WantReachable {
			return Evaluation{VerdictPending, name,
				fmt.Sprintf("%s -> %s:%d reachable=%v, wanted %v",
					pr.From, pr.To, pr.Port, got, pr.WantReachable), CauseDependencyConverging}
		}
	}
	return Evaluation{VerdictSatisfied, name,
		fmt.Sprintf("%d allowed and %d denied paths behave as intended", wantReachable, wantRefused), ""}
}

// enforcementPredicate is the ResourceQuota / LimitRange row: object present AND admission
// observably enforcing it. Presence alone is the "the API call returned 200" answer.
func enforcementPredicate(ctx context.Context, p Prober, t Target) Evaluation {
	const name = "admission-enforcing"
	if _, ev := mustGet(ctx, p, t, name); ev.Verdict != "" && ev.Verdict != VerdictSatisfied {
		return ev
	}
	enforcing, err := p.AdmissionEnforcing(ctx, t.Ref)
	if err != nil {
		return probeFailure(name, "admission enforcement", err)
	}
	if !enforcing {
		return Evaluation{VerdictPending, name,
			"object present but admission is not observably enforcing it", CauseDependencyConverging}
	}
	return Evaluation{VerdictSatisfied, name, "object present and admission enforcing", ""}
}

// providerPredicate is the node pool / cluster row: the provider reports the target state AND
// nodes register Ready.
func providerPredicate(ctx context.Context, p Prober, t Target) Evaluation {
	const name = "provider-and-nodes-ready"
	st, err := p.ProviderState(ctx, t.Ref)
	if err != nil {
		return probeFailure(name, "provider state", err)
	}
	if !st.AtTargetState {
		return Evaluation{VerdictPending, name,
			fmt.Sprintf("provider reports %q, not the target state", st.State), CauseDependencyConverging}
	}
	if st.NodesExpected == 0 {
		return Evaluation{VerdictIndeterminate, name,
			fmt.Sprintf("provider reports %q but the expected node count is unknown, so "+
				"'nodes register Ready' cannot be evaluated", st.State), CauseUnknown}
	}
	if st.NodesReady < st.NodesExpected {
		return Evaluation{VerdictPending, name,
			fmt.Sprintf("provider reports %q but %d/%d nodes are Ready",
				st.State, st.NodesReady, st.NodesExpected), capacityCause(t.Capacity)}
	}
	return Evaluation{VerdictSatisfied, name,
		fmt.Sprintf("provider reports %q and %d/%d nodes Ready",
			st.State, st.NodesReady, st.NodesExpected), ""}
}

// accessPredicate is the RBAC row: a SubjectAccessReview returns the INTENDED answer -- which for
// a revocation is "no".
func accessPredicate(ctx context.Context, p Prober, t Target) Evaluation {
	const name = "access-review"
	if len(t.Access) == 0 {
		return Evaluation{VerdictIndeterminate, name,
			"an RBAC change verifies by SubjectAccessReview and none was supplied; " +
				"the object existing proves only that the API server stored it", CauseUnknown}
	}
	for _, q := range t.Access {
		allowed, err := p.AccessReview(ctx, q)
		if err != nil {
			return probeFailure(name, "access review", err)
		}
		if allowed != q.WantAllowed {
			return Evaluation{VerdictPending, name,
				fmt.Sprintf("%s %s %s/%s: allowed=%v, intended %v",
					q.User, q.Verb, q.Resource, q.Name, allowed, q.WantAllowed),
				CauseDependencyConverging}
		}
	}
	return Evaluation{VerdictSatisfied, name,
		fmt.Sprintf("%d access reviews returned the intended answer", len(t.Access)), ""}
}

// customResourcePredicate is the last row: the owning controller's Ready condition where one
// exists; otherwise object presence only.
func customResourcePredicate(ctx context.Context, p Prober, t Target) Evaluation {
	const name = "custom-resource-ready"
	obj, ev := mustGet(ctx, p, t, name)
	if obj == nil {
		return ev
	}
	status, found := readyCondition(obj)
	if !found {
		return Evaluation{VerdictSatisfied, name,
			"object present; the kind exposes no Ready condition, so presence is all 04 §5.1 claims", ""}
	}
	switch status {
	case "True":
		return Evaluation{VerdictSatisfied, name, "Ready=True", ""}
	case "False":
		return Evaluation{VerdictPending, name, "Ready=False", CauseDependencyConverging}
	default:
		return Evaluation{VerdictPending, name, "Ready=" + status, CauseDependencyConverging}
	}
}

// absencePredicate is the delete row: the object is gone, and stays gone.
//
// It is not in predicateByKind because it is not chosen by kind -- a Deployment verifies as a
// rollout when it was applied and as an absence when it was deleted, and only the action knows
// which. `predicateFor` makes that choice from Target.ExpectAbsent.
//
// Still-present is PENDING rather than failed, and the settle window is what makes that safe.
// Deletion is asynchronous: the API server returns 200 while the object still has finalizers on it,
// and a Deployment with a foreground propagation policy stays readable until its ReplicaSets and
// Pods are collected. Failing on the first read would report every graceful delete as failed. The
// window expiring turns it into VerdictFailed in the driver, which is where "still there after the
// whole window" becomes a rollback -- and a delete's rollback is a recreate from the snapshot, so
// getting this backwards in either direction resurrects objects.
func absencePredicate(ctx context.Context, p Prober, t Target) Evaluation {
	const name = "object-absent"
	obj, err := p.Get(ctx, t.Ref)
	if apierrors.IsNotFound(err) {
		return Evaluation{VerdictSatisfied, name,
			fmt.Sprintf("%s %s/%s is gone", t.Ref.Kind, t.Ref.Namespace, t.Ref.Name), ""}
	}
	if err != nil {
		// ErrTargetReplaced is the ANSWER here, not a probe failure. It means the object this
		// action targeted is gone and a different one now holds the name -- which for every other
		// row is evidence about the wrong object, and for this one is the delete having succeeded.
		// probeFailure would spend the whole settle window on it and then roll back by recreating.
		if errors.Is(err, ErrTargetReplaced) {
			return Evaluation{VerdictSatisfied, name,
				fmt.Sprintf("%s %s/%s is gone; a different object now holds the name (%v)",
					t.Ref.Kind, t.Ref.Namespace, t.Ref.Name, err), ""}
		}
		return probeFailure(name, "get", err)
	}
	if obj == nil {
		return Evaluation{VerdictIndeterminate, name,
			"the prober returned neither an object nor an error", CauseUnknown}
	}

	detail := fmt.Sprintf("%s %s/%s still exists", t.Ref.Kind, t.Ref.Namespace, t.Ref.Name)
	if fins := obj.GetFinalizers(); len(fins) > 0 {
		detail += fmt.Sprintf(" with finalizers %v", fins)
	}
	if ts := obj.GetDeletionTimestamp(); ts != nil {
		detail += ", deletionTimestamp " + ts.String()
	}
	return Evaluation{VerdictPending, name, detail, CauseDependencyConverging}
}

// --- restart baselines --------------------------------------------------------------------------

// restartBaselineKinds are the kinds whose row compares restart counts ACROSS the action, and which
// therefore cannot be evaluated from post-action state alone.
//
// It is a second table keyed the same way as predicateByKind, which is a shape worth being nervous
// about -- two lists of kinds that nothing compares are two lists that will disagree. They are
// joined by TestRestartBaselineKindsAreExactlyTheWorkloadRows, which holds this map to the rows of
// predicateByKind that resolve to workloadPredicate, in both directions.
var restartBaselineKinds = map[classify.KindRef]bool{
	{Group: "apps", Kind: "Deployment"}:  true,
	{Group: "apps", Kind: "StatefulSet"}: true,
}

// NeedsRestartBaseline reports whether a target's row needs a pre-action restart count.
func NeedsRestartBaseline(ref agentv1alpha1.TargetRef) bool {
	return restartBaselineKinds[classify.KindRef{Group: ref.Group, Kind: ref.Kind}]
}

// CaptureRestartBaselines reads the pre-action restart total for every target whose row needs one,
// returning one entry per ref, positionally. A nil entry means the row does not need a baseline.
//
// It must be called BEFORE the action mutates anything -- that is the whole point of the value, and
// a baseline read afterwards is the post-action count compared against itself, which passes for a
// workload that started crashlooping because of this change. Step 3 is where the pipeline calls it,
// alongside the snapshot reads, for that reason.
//
// A target that does not exist yet baselines at ZERO rather than at nil. A create's object had no
// pods before the action, so every restart the settle window observes is a new one, which is
// exactly what a zero baseline expresses. Returning nil there would make workloadPredicate
// Indeterminate and roll back every successful Deployment create.
func CaptureRestartBaselines(ctx context.Context, p Prober, refs []agentv1alpha1.TargetRef) ([]*int64, error) {
	out := make([]*int64, len(refs))
	for i, ref := range refs {
		if !NeedsRestartBaseline(ref) {
			continue
		}
		if p == nil {
			return nil, fmt.Errorf("no prober is wired, so the pre-action restart baseline of "+
				"%s %s/%s cannot be read; its row cannot be evaluated without one",
				ref.Kind, ref.Namespace, ref.Name)
		}
		n, err := p.RestartCount(ctx, ref)
		if apierrors.IsNotFound(err) {
			zero := int64(0)
			out[i] = &zero
			continue
		}
		if err != nil {
			// Refuse rather than degrade to nil. An action executed with no baseline cannot be
			// verified, and 04 §5.1 turns "could not be verified" into a rollback at the end of the
			// settle window -- so a prober blip here would land the write and then undo it. Failing
			// before step 5 costs the caller a retry instead.
			return nil, fmt.Errorf("reading the pre-action restart baseline of %s %s/%s: %w",
				ref.Kind, ref.Namespace, ref.Name, err)
		}
		out[i] = &n
	}
	return out, nil
}

// --- shared helpers ---------------------------------------------------------------------------

// mustGet fetches the target. A nil object means the caller should return the Evaluation instead.
func mustGet(ctx context.Context, p Prober, t Target, name string) (*unstructured.Unstructured, Evaluation) {
	obj, err := p.Get(ctx, t.Ref)
	if apierrors.IsNotFound(err) {
		return nil, Evaluation{VerdictFailed, name,
			fmt.Sprintf("%s %s/%s does not exist after the action", t.Ref.Kind, t.Ref.Namespace, t.Ref.Name),
			CauseSchemaRejected}
	}
	if err != nil {
		return nil, probeFailure(name, "get", err)
	}
	if obj == nil {
		return nil, Evaluation{VerdictIndeterminate, name,
			"the prober returned neither an object nor an error", CauseUnknown}
	}
	return obj, Evaluation{}
}

// probeFailure turns a prober error into the right verdict. ErrProbeUnsupported is Indeterminate --
// the capability is missing, which is a fact about the broker. Anything else is classified.
func probeFailure(name, what string, err error) Evaluation {
	if errors.Is(err, ErrProbeUnsupported) {
		return Evaluation{VerdictIndeterminate, name,
			fmt.Sprintf("%s probe unavailable: %v", what, err), CauseUnknown}
	}
	c := CauseOf(Failure{Err: err})
	v := VerdictPending
	if DispositionOf(c) == Terminal {
		v = VerdictFailed
	}
	return Evaluation{v, name, fmt.Sprintf("%s probe failed: %v", what, err), c}
}

// pendingCause reads the object's own conditions for a terminal reason before defaulting to
// "still converging". A Deployment whose ReplicaFailure condition names a quota rejection is not
// converging, and waiting out its window is time nobody gets back.
func pendingCause(obj *unstructured.Unstructured, sig CapacitySignal) Cause {
	conds, found, _ := unstructured.NestedSlice(obj.Object, "status", "conditions")
	if !found {
		return CauseDependencyConverging
	}
	for _, raw := range conds {
		c, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		typ, _ := c["type"].(string)
		status, _ := c["status"].(string)
		if typ != "ReplicaFailure" || status != "True" {
			continue
		}
		msg, _ := c["message"].(string)
		return CauseOf(Failure{Message: msg, Capacity: sig})
	}
	return CauseDependencyConverging
}

func readyCondition(obj *unstructured.Unstructured) (string, bool) {
	conds, found, _ := unstructured.NestedSlice(obj.Object, "status", "conditions")
	if !found {
		return "", false
	}
	for _, raw := range conds {
		c, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		if typ, _ := c["type"].(string); typ == "Ready" {
			status, _ := c["status"].(string)
			return status, true
		}
	}
	return "", false
}

// --- settle windows ---------------------------------------------------------------------------

// MaxSettleWindow is the code ceiling 09 §12 row T-9 asks for. No target waits longer, whatever the
// table below says and whatever a caller overrides it to: an unbounded settle window makes "the
// broker verifies" indistinguishable from "the broker eventually gives up", and it holds the undo
// plan's snapshot open past the point where replaying it restores the world that existed.
const MaxSettleWindow = 30 * time.Minute

// DefaultSettleWindow applies to any kind with no row of its own.
const DefaultSettleWindow = 2 * time.Minute

// settleWindows are the per-kind defaults T-9 asks to be published.
//
// These are the harness's working values, not a resolution of T-9: 09 §12 is explicit that the
// proposed defaults there "are starting points for that decision, not values a harness may assume",
// and V-PRO-013 stays deferred against that row. What the table does buy is falsifiability -- a
// number in one place, applied by one function, that a reviewer can disagree with.
var settleWindows = map[classify.KindRef]time.Duration{
	{Group: "apps", Kind: "Deployment"}:  5 * time.Minute,
	{Group: "apps", Kind: "StatefulSet"}: 10 * time.Minute, // ordered, one pod at a time
	{Group: "apps", Kind: "DaemonSet"}:   5 * time.Minute,

	{Group: "", Kind: "Service"}:                          90 * time.Second,
	{Group: "networking.k8s.io", Kind: "Ingress"}:         5 * time.Minute, // LB programming
	{Group: "gateway.networking.k8s.io", Kind: "Gateway"}: 5 * time.Minute,

	{Group: "networking.k8s.io", Kind: "NetworkPolicy"}: 30 * time.Second,

	{Group: "", Kind: "ResourceQuota"}: 15 * time.Second,
	{Group: "", Kind: "LimitRange"}:    15 * time.Second,

	{Group: "container.cnrm.cloud.google.com", Kind: "ContainerNodePool"}: 20 * time.Minute,
	{Group: "container.cnrm.cloud.google.com", Kind: "ContainerCluster"}:  30 * time.Minute,
}

// rbacSettleWindow is the RBAC row. Keyed by kind alone, matching PredicateFor's fallback.
const rbacSettleWindow = 15 * time.Second

// SettleWindow returns the bounded window for a target, always at or below MaxSettleWindow.
func SettleWindow(ref agentv1alpha1.TargetRef) time.Duration {
	if d, ok := settleWindows[classify.KindRef{Group: ref.Group, Kind: ref.Kind}]; ok {
		return clampWindow(d)
	}
	if _, ok := predicateByKindAnyGroup[ref.Kind]; ok {
		return rbacSettleWindow
	}
	return DefaultSettleWindow
}

// clampWindow enforces the ceiling. It is applied to the table's own values too, so a future edit
// that types an extra zero is clamped rather than honoured.
func clampWindow(d time.Duration) time.Duration {
	if d > MaxSettleWindow {
		return MaxSettleWindow
	}
	if d <= 0 {
		return DefaultSettleWindow
	}
	return d
}
