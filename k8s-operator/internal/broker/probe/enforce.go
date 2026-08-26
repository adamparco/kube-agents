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

package probe

import (
	"context"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/verify"
)

// probePodPrefix names the objects this file submits. They are only ever submitted with
// DryRunAll, so none of them is ever persisted -- the prefix exists so that a name appearing in an
// audit log or an admission webhook's own logs is attributable.
const probePodPrefix = "kage-admission-probe-"

// AdmissionEnforcing reports whether admission is observably enforcing the object.
//
// The row this serves is ResourceQuota / LimitRange, and 04 §5.1 says "object present AND admission
// observably enforcing it". The first half is already done by the predicate, which calls Get before
// calling this. The second half is the whole reason the row is not just presence, and the two kinds
// answer it in completely different ways because only one of them has a status.
func (s *Source) AdmissionEnforcing(ctx context.Context, ref agentv1alpha1.TargetRef) (bool, error) {
	if s.Client == nil {
		return false, s.noClient("whether admission is enforcing " + describe(ref))
	}
	switch {
	case ref.Group == "" && ref.Kind == "ResourceQuota":
		return s.quotaEnforcing(ctx, ref)
	case ref.Group == "" && ref.Kind == "LimitRange":
		return s.limitRangeEnforcing(ctx, ref)
	}
	return false, fmt.Errorf("%s is not a kind whose enforcement this prober can observe; the "+
		"04 §5.1 enforcement row covers ResourceQuota and LimitRange: %w",
		describe(ref), verify.ErrProbeUnsupported)
}

// quotaEnforcing answers from the quota's own status, which is written by the quota controller and
// by nobody else.
//
// Three conditions, and the third is the one that makes this more than a presence check:
//
//  1. `status.hard` is populated -- the controller has seen the object at all.
//  2. every key of `spec.hard` appears in `status.hard` with the same value -- the controller has
//     seen THIS generation of it. A quota whose limits were just tightened reports the old limits
//     in status until the controller catches up, and admission enforces what status says. Comparing
//     only presence would report the new limits as enforced while the old ones still are, which is
//     precisely the window an operator raises a quota to close.
//  3. `status.used` is non-nil -- the controller is accounting, not merely echoing.
func (s *Source) quotaEnforcing(ctx context.Context, ref agentv1alpha1.TargetRef) (bool, error) {
	var q corev1.ResourceQuota
	if err := s.Client.Get(ctx, client.ObjectKey{Namespace: ref.Namespace, Name: ref.Name}, &q); err != nil {
		return false, fmt.Errorf("reading %s to check whether the quota controller has it: %w",
			describe(ref), err)
	}
	if len(q.Status.Hard) == 0 {
		return false, nil
	}
	for name, want := range q.Spec.Hard {
		got, ok := q.Status.Hard[name]
		if !ok || got.Cmp(want) != 0 {
			return false, nil
		}
	}
	return q.Status.Used != nil, nil
}

// limitRangeEnforcing answers by DRY-RUN ADMISSION, because a LimitRange has no status at all.
//
// # Why two dry runs and not one
//
// The obvious probe is "submit something that violates the range and see it rejected". It is wrong
// on its own: a namespace with Pod Security Admission, a validating webhook, or a quota will reject
// the probe pod too, and the rejection reads identically. Matching on the error text to find the
// word "LimitRanger" trades one guess for another, and it breaks the first time a distribution
// rewords its admission messages.
//
// So the probe has two legs, for the same reason connectivityPredicate does: submit a COMPLIANT pod
// and a VIOLATING pod, both dry-run, both otherwise identical. Enforcement is observed only when
// the compliant one is admitted and the violating one is not. If both are rejected, something other
// than this LimitRange is rejecting them and the honest answer is that enforcement was not
// observed. The differentiator is the difference between the two submissions, which is a fact this
// function controls, rather than the wording of an error, which is not.
//
// # Both dry runs are non-mutating
//
// DryRunAll makes the API server run the full admission chain and discard the result. Nothing is
// persisted, no name is consumed, and the object never reaches etcd.
//
// # What it cannot observe
//
// A LimitRange with no container-scoped cpu or memory `max` declares nothing this probe knows how
// to exceed, so there is no violating pod to construct. It returns ErrProbeUnsupported -- which is
// Indeterminate and therefore, at the end of the window, a rollback. Extending the probe to `min`,
// to `type: Pod`, and to PersistentVolumeClaim limits is real work with its own negative controls
// and belongs to whoever needs those shapes verified; guessing at them here would produce a probe
// whose green means nothing for the shapes it guessed wrong.
func (s *Source) limitRangeEnforcing(ctx context.Context, ref agentv1alpha1.TargetRef) (bool, error) {
	var lr corev1.LimitRange
	if err := s.Client.Get(ctx, client.ObjectKey{Namespace: ref.Namespace, Name: ref.Name}, &lr); err != nil {
		return false, fmt.Errorf("reading %s to build an admission probe for it: %w", describe(ref), err)
	}

	res, quantity, ok := containerMax(&lr)
	if !ok {
		return false, fmt.Errorf("%s declares no container-scoped cpu or memory maximum, so there "+
			"is no request this prober can construct that the range would reject: %w",
			describe(ref), verify.ErrProbeUnsupported)
	}

	// The violating request is twice the maximum, so it exceeds it under any rounding.
	over := quantity.DeepCopy()
	over.Add(quantity)

	compliantAdmitted, err := s.dryRunPodAdmitted(ctx, ref.Namespace, res, quantity)
	if err != nil {
		return false, err
	}
	violatingAdmitted, err := s.dryRunPodAdmitted(ctx, ref.Namespace, res, over)
	if err != nil {
		return false, err
	}

	// Both legs must behave. compliant=false means something else in the chain is refusing and this
	// probe learned nothing about the LimitRange; violating=true means the range is not enforcing.
	return compliantAdmitted && !violatingAdmitted, nil
}

// containerMax returns the first container-scoped cpu or memory maximum a LimitRange declares. cpu
// is preferred over memory only for determinism -- a probe that picked a different limit on
// different calls would make this method's answer depend on map iteration order.
func containerMax(lr *corev1.LimitRange) (corev1.ResourceName, resource.Quantity, bool) {
	for _, item := range lr.Spec.Limits {
		if item.Type != corev1.LimitTypeContainer {
			continue
		}
		for _, res := range []corev1.ResourceName{corev1.ResourceCPU, corev1.ResourceMemory} {
			if q, ok := item.Max[res]; ok {
				return res, q, true
			}
		}
	}
	return "", resource.Quantity{}, false
}

// dryRunPodAdmitted submits one dry-run Pod and reports whether admission accepted it.
//
// A rejection is `admitted=false, err=nil`: for this probe a refusal is data, not a failure. Any
// other error -- the connection dropped, the namespace vanished -- is returned, because reporting
// "not admitted" for a request that never reached admission would make an unreachable API server
// look like a working LimitRange.
func (s *Source) dryRunPodAdmitted(
	ctx context.Context, namespace string, res corev1.ResourceName, q resource.Quantity,
) (bool, error) {
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{GenerateName: probePodPrefix, Namespace: namespace},
		Spec: corev1.PodSpec{
			// Never scheduled and never pulled: this object is discarded inside the API server.
			Containers: []corev1.Container{{
				Name:  "probe",
				Image: "registry.k8s.io/pause:3.9",
				Resources: corev1.ResourceRequirements{
					Requests: corev1.ResourceList{res: q},
					Limits:   corev1.ResourceList{res: q},
				},
			}},
		},
	}
	err := s.Client.Create(ctx, pod, client.DryRunAll)
	if err == nil {
		return true, nil
	}
	if isAdmissionRefusal(err) {
		return false, nil
	}
	return false, fmt.Errorf("the dry-run admission probe in namespace %s did not reach a verdict: %w",
		namespace, err)
}

// isAdmissionRefusal reports whether an error is admission saying no, as opposed to the request
// failing to complete. Forbidden and Invalid are the two the admission chain produces; both mean
// the request was evaluated and refused.
//
// A 403 from RBAC is also Forbidden, and it is not admission refusing the pod -- it is the broker
// not being allowed to ask. That case is caught by the two-leg design rather than by this
// predicate: an RBAC 403 refuses BOTH legs, so `compliant && !violating` is false and the answer is
// "enforcement not observed", which is the truth.
func isAdmissionRefusal(err error) bool {
	return apierrors.IsForbidden(err) || apierrors.IsInvalid(err)
}
