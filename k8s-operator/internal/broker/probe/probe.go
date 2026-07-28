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

// Package probe answers the eight questions the 04 §5.1 verification predicates ask, against a real
// cluster: the production implementation of verify.Prober.
//
// # What the package is for
//
// 04 §5.1 opens by saying verification is "per-kind and concrete, not 'the API call returned 200'".
// Everything in `internal/broker/verify` is the second half of that sentence -- the table of what
// each kind means by "verified" -- and none of it can look at anything. This package is the first
// half: the reads that make the table's answers come from the cluster rather than from the write's
// own return value. V-PRO-027 is the check on exactly that boundary.
//
// # The failure direction is per-method, and getting it uniform would be the bug
//
// The same discipline as internal/broker/livestate, for the same reason and with a different answer
// per method. Every probe has one answer that means "verified" and the question is what an
// unavailable answer must NOT be mistaken for:
//
//   - RestartCount: zero is the passing answer, so a selector that will not resolve, or a Pod list
//     that fails, is an ERROR. "I could not count the restarts" reported as 0 is the entire
//     Deployment row silently passing for a workload that is crashlooping.
//   - EndpointCount: zero is a legitimate observation -- a Service whose pods are not ready yet --
//     and the predicate turns it into Pending, which is correct. Only a failed read errors.
//   - ProgrammedAddress: empty is a legitimate observation, for the same reason.
//   - AccessReview: false is a legitimate observation. But a SubjectAccessReview carrying an
//     `evaluationError` is NOT a clean deny, and this is the sharpest edge in the package: the RBAC
//     row verifies a REVOCATION by the review answering no, so an authorizer that fell over would
//     otherwise verify every revocation the broker ever performs. It errors.
//   - AdmissionEnforcing: false is a legitimate observation. A shape this probe cannot observe is
//     ErrProbeUnsupported, which is Indeterminate rather than "not enforcing".
//   - ProviderState: AtTargetState=false is a legitimate observation; an unreadable CR errors.
//   - Connectivity: this deployment has no dataplane prober, so it is ErrProbeUnsupported by
//     construction. See "The connectivity hole" below -- it is a hole, and it is declared.
//   - Get: NotFound is passed through UNWRAPPED, because verify.mustGet reads it with
//     apierrors.IsNotFound and turns it into a Failed verdict naming the missing object.
//
// # Why Indeterminate is not free
//
// It is tempting to read ErrProbeUnsupported as a soft skip. It is not. verify.Driver.verifyOne
// polls until the settle window closes, and a verdict that is still Indeterminate at the deadline
// becomes Failed with CauseSettleWindowExpired -- terminal, which is an automatic rollback. So
// every ErrProbeUnsupported in this package is a rollback of an action that may well have worked,
// and each one is named in a doc comment rather than left to be discovered from a production
// incident. That is the correct direction (an unverifiable change is not a verified change) and it
// is expensive, which is why the set is kept as small as the available reads allow.
//
// # Nothing here is cached
//
// A settle window is a poll loop, and the whole point of the loop is that the answer changes. A
// cache with any TTL at all would make the second poll return the first poll's answer, which turns
// "wait for it to converge" into "wait, then report what it looked like before we waited".
package probe

import (
	"context"
	"fmt"

	authorizationv1 "k8s.io/api/authorization/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/verify"
)

// Source is the production verify.Prober.
//
// A direct client, not a cached one: see "Nothing here is cached" above. The zero value is not
// usable -- every method refuses a nil Client rather than panicking, because a prober assembled
// without its client would otherwise fail at the first settle window of the first real action.
type Source struct {
	// Client reads objects, lists pods and endpoint slices, and issues SubjectAccessReviews and
	// dry-run admission probes.
	Client client.Client

	// Dataplane is the prober for the NetworkPolicy row. Nil is the supported and expected value
	// today; see the Connectivity method for what that costs.
	Dataplane ConnectivityProber
}

// ConnectivityProber is the seam a dataplane probe plugs into. It is separate from Source because
// answering it requires something inside the cluster network -- a probe DaemonSet, an ephemeral
// container, a service mesh's own telemetry -- and none of those is a client read.
type ConnectivityProber interface {
	Probe(ctx context.Context, p verify.ConnectivityProbe) (bool, error)
}

var _ verify.Prober = (*Source)(nil)

// noClient is the refusal every method opens with.
func (s *Source) noClient(what string) error {
	return fmt.Errorf("probe.Source has no Client, so %s cannot be read from the cluster; a prober "+
		"that answers from anywhere else is 04 §5.1's \"the API call returned 200\"", what)
}

// Get returns live state for a target.
//
// # The UID pin, which is the part that is not a Get wrapper
//
// TargetRef carries the UID observed at classification time. When it is set, this method requires
// the live object to still have it. The case that matters is narrow and real: a settle window is
// minutes long, and an object deleted and recreated inside it -- by a human, by a GitOps engine, by
// the garbage collector -- is a DIFFERENT object at the same name. Verifying it would report the
// action's effect from a stranger, and it would report it as satisfied, because the replacement is
// usually healthy. A mismatch is an error rather than NotFound because the two are not the same
// fact and the operator reading the record needs to be able to tell them apart.
//
// An empty ref.UID skips the check: a `create` is classified before its target exists, so it has no
// UID to pin, and demanding one would make every create unverifiable.
func (s *Source) Get(ctx context.Context, ref agentv1alpha1.TargetRef) (*unstructured.Unstructured, error) {
	if s.Client == nil {
		return nil, s.noClient(describe(ref))
	}
	obj := objectFor(ref)
	// NotFound is returned unwrapped. verify.mustGet reads it with apierrors.IsNotFound to produce
	// "does not exist after the action", and wrapping it here would turn that named verdict into a
	// generic probe failure.
	if err := s.Client.Get(ctx, client.ObjectKey{Namespace: ref.Namespace, Name: ref.Name}, obj); err != nil {
		return nil, err
	}
	if ref.UID != "" && string(obj.GetUID()) != ref.UID {
		return nil, fmt.Errorf("%s exists but is uid %s, not the uid %s this action targeted: the "+
			"object was replaced during the settle window, so its state is evidence about a "+
			"different object", describe(ref), obj.GetUID(), ref.UID)
	}
	return obj, nil
}

// RestartCount is the total container restarts across the pods of a workload.
//
// Every failure here is an error and none is a zero. Zero restarts is what a healthy workload
// reports, so any path that reaches "0" without having counted is the Deployment row passing for a
// crashlooping app -- the exact half of 04 §5.1 that distinguishes a rollout that completed from
// one that completed and then fell over.
//
// The selector is read with metav1.LabelSelectorAsSelector rather than by pulling matchLabels out
// of the map. A selector expressed entirely in matchExpressions has an empty matchLabels, and a
// naive read of it produces the EMPTY selector -- which matches every pod in the namespace. That
// failure is silent, over-counts, and looks like a crashlooping app.
func (s *Source) RestartCount(ctx context.Context, ref agentv1alpha1.TargetRef) (int64, error) {
	if s.Client == nil {
		return 0, s.noClient("the restart count of " + describe(ref))
	}
	if ref.Namespace == "" {
		return 0, fmt.Errorf("%s has no namespace: restarts are counted over the pods of a "+
			"namespaced workload", describe(ref))
	}
	obj, err := s.Get(ctx, ref)
	if err != nil {
		return 0, fmt.Errorf("reading %s to find its pod selector: %w", describe(ref), err)
	}

	raw, found, err := unstructured.NestedMap(obj.Object, "spec", "selector")
	if err != nil || !found {
		return 0, fmt.Errorf("%s has no readable spec.selector, so the pods whose restarts this "+
			"row counts cannot be identified (found=%v): %w", describe(ref), found, err)
	}
	sel := &metav1.LabelSelector{}
	if err := runtimeConvert(raw, sel); err != nil {
		return 0, fmt.Errorf("spec.selector of %s is not a LabelSelector: %w", describe(ref), err)
	}
	selector, err := metav1.LabelSelectorAsSelector(sel)
	if err != nil {
		return 0, fmt.Errorf("spec.selector of %s does not compile: %w", describe(ref), err)
	}
	// An empty selector matches everything. A workload cannot legally have one, so arriving here
	// with one means the conversion above lost the selector rather than that the workload owns the
	// namespace -- and the difference between those two is thousands of unrelated restarts.
	if selector.Empty() {
		return 0, fmt.Errorf("spec.selector of %s compiled to the empty selector, which matches "+
			"every pod in %s; refusing to report that as this workload's restart count",
			describe(ref), ref.Namespace)
	}

	var pods corev1.PodList
	if err := s.Client.List(ctx, &pods,
		client.InNamespace(ref.Namespace),
		client.MatchingLabelsSelector{Selector: selector},
	); err != nil {
		return 0, fmt.Errorf("listing the pods of %s: %w", describe(ref), err)
	}

	var total int64
	for i := range pods.Items {
		for _, cs := range pods.Items[i].Status.ContainerStatuses {
			total += int64(cs.RestartCount)
		}
		// Init containers restart too, and an init container that cannot start is the most common
		// way a rollout looks available-then-not. Counting only app containers misses it entirely.
		for _, cs := range pods.Items[i].Status.InitContainerStatuses {
			total += int64(cs.RestartCount)
		}
	}
	return total, nil
}

// AccessReview runs a SubjectAccessReview and reports whether the request would be allowed.
//
// # evaluationError is not a deny
//
// This is the method with the most dangerous plausible bug in the package. The RBAC row of 04 §5.1
// verifies a change by the review returning "the intended answer", and for the common case -- a
// revocation -- the intended answer is NO. A SubjectAccessReview whose authorizer errored comes
// back with Allowed=false and EvaluationError set. Reading only Allowed would therefore verify
// every revocation the broker ever performs, including the ones that did not take effect, and it
// would do it on exactly the days the authorizer is unhealthy.
//
// So a non-empty EvaluationError is an error even when Denied is set: a partial evaluation that
// happened to reach a deny is still a partial evaluation.
func (s *Source) AccessReview(ctx context.Context, q verify.AccessQuery) (bool, error) {
	if s.Client == nil {
		return false, s.noClient("a SubjectAccessReview")
	}
	if q.User == "" && len(q.Groups) == 0 {
		return false, fmt.Errorf("a SubjectAccessReview needs a subject: neither user nor groups " +
			"was set, and an empty subject reviews the anonymous user rather than the one whose " +
			"access the action changed")
	}
	if q.Verb == "" || q.Resource == "" {
		return false, fmt.Errorf("a SubjectAccessReview needs a verb and a resource; got verb=%q "+
			"resource=%q", q.Verb, q.Resource)
	}

	sar := &authorizationv1.SubjectAccessReview{
		Spec: authorizationv1.SubjectAccessReviewSpec{
			User:   q.User,
			Groups: q.Groups,
			ResourceAttributes: &authorizationv1.ResourceAttributes{
				Namespace: q.Namespace,
				Verb:      q.Verb,
				Group:     q.Group,
				Resource:  q.Resource,
				Name:      q.Name,
			},
		},
	}
	if err := s.Client.Create(ctx, sar); err != nil {
		return false, fmt.Errorf("submitting a SubjectAccessReview for %s %s %s: %w",
			q.User, q.Verb, q.Resource, err)
	}
	if sar.Status.EvaluationError != "" {
		return false, fmt.Errorf("the SubjectAccessReview for %s %s %s did not complete: %s "+
			"(allowed=%v, denied=%v); an incomplete evaluation is not a deny, and a revocation "+
			"verifies by a deny",
			q.User, q.Verb, q.Resource, sar.Status.EvaluationError, sar.Status.Allowed, sar.Status.Denied)
	}
	return sar.Status.Allowed, nil
}

// Connectivity runs one affirmative probe and reports whether the path was reachable.
//
// # The connectivity hole, stated rather than hidden
//
// There is no client read that answers "can this pod reach that pod on this port". A
// NetworkPolicy's own status says nothing about the dataplane -- which is the entire reason 04 §5.1
// asks for an affirmative probe on this row rather than for object presence. Answering it needs
// something that can open a socket from inside the cluster network, and that is a workload, not an
// adapter.
//
// So Connectivity is a seam, and with no ConnectivityProber wired it returns ErrProbeUnsupported.
// The consequence is concrete and is not softened here: a NetworkPolicy action verifies as
// Indeterminate, holds that verdict for the 30-second settle window, and is then rolled back. That
// is the conservative direction -- an unverified policy change is not a verified one -- but it
// means the broker cannot usefully make NetworkPolicy changes until a prober exists. The unit that
// supplies one owns a probe workload and its own RBAC, which is provisioning work rather than
// adapter work; it is recorded as a deferral rather than left implied by a nil field.
func (s *Source) Connectivity(ctx context.Context, p verify.ConnectivityProbe) (bool, error) {
	if s.Dataplane == nil {
		return false, fmt.Errorf("no dataplane prober is wired, so %s -> %s:%d cannot be tested: %w",
			p.From, p.To, p.Port, verify.ErrProbeUnsupported)
	}
	return s.Dataplane.Probe(ctx, p)
}

// --- shared helpers -----------------------------------------------------------------------------

// objectFor builds an empty typed-by-GVK unstructured for a target. Deliberately a copy of
// execute.objectFor rather than an export of it: the two packages are on opposite sides of the
// action (one writes, one observes), and a shared helper between them is a coupling that makes a
// change to how writes address an object silently change how verification addresses it.
func objectFor(ref agentv1alpha1.TargetRef) *unstructured.Unstructured {
	obj := &unstructured.Unstructured{}
	apiVersion := ref.Version
	if ref.Group != "" {
		apiVersion = ref.Group + "/" + ref.Version
	}
	obj.SetAPIVersion(apiVersion)
	obj.SetKind(ref.Kind)
	return obj
}

// describe renders a target for an error message. Errors from this package end up verbatim in
// `status.verification.checks[].detail`, which is what an operator reads at 3am.
func describe(ref agentv1alpha1.TargetRef) string {
	if ref.Namespace == "" {
		return fmt.Sprintf("%s %s", ref.Kind, ref.Name)
	}
	return fmt.Sprintf("%s %s/%s", ref.Kind, ref.Namespace, ref.Name)
}

// oneLabel compiles a single-key label selector, for the label-keyed lookups (EndpointSlice by
// service name, Node by node pool) that are not LabelSelectors on an object.
func oneLabel(k, v string) labels.Selector {
	return labels.SelectorFromSet(labels.Set{k: v})
}

// runtimeConvert decodes a nested unstructured map into a typed struct.
func runtimeConvert(raw map[string]any, into any) error {
	return runtime.DefaultUnstructuredConverter.FromUnstructured(raw, into)
}
