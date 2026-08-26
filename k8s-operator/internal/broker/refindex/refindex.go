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

// Package refindex answers "what points at this object" against a real cluster: the production
// implementation of undo.ReferenceIndex, backed by a controller-runtime client and a discovery
// client.
//
// # The question is narrower than it sounds, and naming the boundary is the whole design
//
// 06 §4.3.1 says a recreate is unsafe because "a recreated object gets a new UID, so every
// ownerReference, PVC binding, and external reference pointing at the old one is dangling". The
// operative words are POINTING AT THE OLD ONE. A reference that resolves by NAME survives a recreate
// with the same name -- a Pod mounting `configMap: app-config` finds the new one, because it never
// held the old one's identity in the first place. A reference that resolves by UID does not survive,
// because the UID it holds now names nothing.
//
// So the domain of this package is UID-VALUED references, and that domain is almost exactly
// `metadata.ownerReferences`. Naming it that way is deliberate, because [[LSN-033]] is the lesson
// about a safety list that is complete for the domain its author had in mind: the way out is not to
// try harder to enumerate reference-shaped fields, it is to say which question the enumeration
// answers and to write down what falls outside. What falls outside is in "The residual" below.
//
// # Why this is not in package undo
//
// Same seam as internal/broker/livestate and internal/broker/policy, for the same reason: `undo`
// declares the interface so that plan generation stays reproducible from a fixture (undo/refs.go
// says so at the interface), and the 09 §7.3 round-trip corpus is hermetic only because nothing in
// that package can go and look anything up. An adapter living beside the planner would make every
// corpus case depend on what a cluster answered.
package refindex

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/discovery"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/undo"
)

// Source is the production undo.ReferenceIndex.
//
// # The failure direction, which is the opposite of the denominator's
//
// livestate.CountWorkloadObjects skips a kind it cannot list, because a smaller denominator makes
// every blast-radius fraction larger and the abort rule more likely to fire. This type must do the
// reverse, and the interface says why in undo/refs.go: "'nothing points at it' and 'I could not
// look' are the two answers this package must never conflate." A skipped kind here produces a
// SHORTER reference list, a shorter list makes the recreate look safer, and the failure that follows
// is silent and delayed -- the recreate succeeds, and the garbage collector deletes the children of
// the UID that no longer exists some minutes later.
//
// So: any kind that cannot be listed fails the whole call. The cost is real and is paid knowingly. A
// broker whose RBAC does not cover the target's namespace end to end will return an error for every
// delete, checkRecreatable will downgrade the strategy to `none`, and the action will gate. Gating a
// delete that could have been recreated is a cost; recreating an object whose owner-reference graph
// nobody could see is an outage.
//
// # Nothing here is cached
//
// livestate caches its two per-scope reads because the spec bounds their staleness at 60 seconds and
// the reads are expensive. This read is expensive too -- one metadata List per listable kind -- and
// is deliberately not cached anyway. It runs once per delete target, at plan-generation time, and
// the answer it produces is the premise of a decision that is about to be acted on. A cached
// "nothing points at it" is precisely the stale answer that turns this adapter into the bug it
// exists to prevent: the StatefulSet that adopted the PVC forty seconds ago is invisible to a cache
// whose window has not closed.
//
// # The residual
//
// Two classes of reference are outside what this can see, and both are recorded rather than papered
// over.
//
//   - A UID-valued reference in a field that is not `ownerReferences`. In the core API the only one
//     that matters is `PersistentVolume.spec.claimRef.uid`, and it cannot reach here: PVC and PV are
//     both on undo.nonRecreatableKinds, so undo.checkRecreatable short-circuits on the kind before
//     the index is consulted. A CRD may define such a field and this package will not find it.
//   - An out-of-cluster reference -- a dashboard, an external system, a human's bookmark holding the
//     UID. Nothing in the cluster can enumerate those, and 06 §4.3.1's own caveat text ("any
//     reference created after the snapshot was taken will not be restored") is the honest answer.
//
// Both residuals are in the LOOSENING direction: a reference this cannot see is a recreate that
// proceeds. That is why the visibility failure above is fatal -- the part of the graph the adapter
// CAN see is the part it must not be allowed to under-report.
type Source struct {
	// Client lists referrers. A direct client, not a cached one: an informer's answer to "does
	// anything own this" is exactly the answer that must not be a few seconds old.
	Client client.Client

	// Discovery enumerates the kinds a referrer could be. A nil Discovery is an error, not an empty
	// scan: a scan over no kinds finds no references and reports the object free.
	Discovery discovery.ServerResourcesInterface
}

var _ undo.ReferenceIndex = (*Source)(nil)

// InboundReferences returns every object whose ownerReferences name the target's UID.
//
// # Matching is by UID and never by name
//
// The target ref reaching here was re-pinned by execute.CaptureAll from what was actually read, so
// its UID is the UID of the object the snapshot describes. Matching on (kind, namespace, name)
// instead would find references to a DIFFERENT object that happens to occupy the name today -- which
// is the very situation a recreate creates, so a name match would report the post-recreate world as
// evidence about the pre-recreate one. A target with no UID is refused rather than matched loosely:
// undo/refs.go turns an error into a downgrade, so refusing costs a gate and guessing costs an
// outage.
//
// # What is scanned
//
// A namespaced target is scanned against every listable namespaced kind IN ITS OWN NAMESPACE, because
// the garbage collector requires a namespaced owner and its dependent to share a namespace -- a
// cross-namespace ownerReference is not a reference the GC honours, it is an object the GC deletes.
// A cluster-scoped target is scanned against every listable kind everywhere, cluster-scoped and
// namespaced alike, because a namespaced object may legitimately be owned by a cluster-scoped one.
//
// Kinds come from discovery, never from a literal list here ([[LSN-036]]): a hardcoded set of "the
// kinds that own things" is correct on the day it is written and blind to the CRD installed the week
// after, and a blind spot in this particular scan is a recreate that proceeds.
func (s *Source) InboundReferences(ctx context.Context, target agentv1alpha1.TargetRef) ([]undo.InboundRef, error) {
	if target.UID == "" {
		return nil, fmt.Errorf(
			"%s %s/%s carries no UID, and an inbound-reference scan matches on UID: a name match would answer a question about whichever object holds the name now",
			target.Kind, orCluster(target.Namespace), target.Name)
	}
	if s.Client == nil {
		return nil, errors.New("no API client: the reference graph cannot be read, which is not the same as it being empty")
	}
	if s.Discovery == nil {
		return nil, errors.New("no discovery client: the set of kinds a referrer could be cannot be established, so an empty result would mean nothing")
	}

	kinds, err := s.referrerKinds(target.Namespace == "")
	if err != nil {
		return nil, err
	}

	var opts []client.ListOption
	if target.Namespace != "" {
		opts = append(opts, client.InNamespace(target.Namespace))
	}

	var out []undo.InboundRef
	for _, gvk := range kinds {
		var list metav1.PartialObjectMetadataList
		list.SetGroupVersionKind(gvk.GroupVersion().WithKind(gvk.Kind + "List"))
		if err := s.Client.List(ctx, &list, opts...); err != nil {
			// Fatal, and the message distinguishes the two reasons an operator would act on
			// differently: Forbidden is a grant that is missing and will stay missing until somebody
			// widens the Role, anything else is a cluster that is unwell and may recover.
			if apierrors.IsForbidden(err) {
				return nil, fmt.Errorf(
					"this broker may not list %s in %s, so it cannot prove nothing owns %s %s/%s; the recreate is refused rather than assumed safe: %w",
					gvk.Kind, orCluster(target.Namespace), target.Kind, orCluster(target.Namespace), target.Name, err)
			}
			return nil, fmt.Errorf(
				"listing %s in %s while scanning for references to %s %s/%s: %w",
				gvk.Kind, orCluster(target.Namespace), target.Kind, orCluster(target.Namespace), target.Name, err)
		}
		for i := range list.Items {
			item := &list.Items[i]
			for _, owner := range item.OwnerReferences {
				if string(owner.UID) != target.UID {
					continue
				}
				out = append(out, refFor(gvk, item, owner))
			}
		}
	}

	sortRefs(out)
	return out, nil
}

// refFor renders one matched dependent, naming HOW it refers.
//
// undo.InboundRef.Via exists because "3 objects reference this" is a number and "a StatefulSet owns
// it" is a reason, and a controller reference is the sharper of the two cases: it is the edge the
// garbage collector acts on, and it is the one that makes the dependent get deleted rather than
// merely orphaned.
func refFor(gvk schema.GroupVersionKind, item *metav1.PartialObjectMetadata, owner metav1.OwnerReference) undo.InboundRef {
	via := "ownerReference"
	if owner.Controller != nil && *owner.Controller {
		via = "ownerReference (controller)"
	}
	return undo.InboundRef{
		Ref: agentv1alpha1.TargetRef{
			Group:     gvk.Group,
			Version:   gvk.Version,
			Kind:      gvk.Kind,
			Namespace: item.GetNamespace(),
			Name:      item.GetName(),
			UID:       string(item.GetUID()),
		},
		Via: via,
	}
}

// referrerKinds enumerates the kinds a dependent could be.
//
// Deduplicated by (group, kind): ServerPreferredResources can surface a kind twice, and listing it
// twice would report the same dependent twice, which inflates the caveat a human reads.
//
// Sorted, so two scans of the same cluster issue the same List calls in the same order. That is what
// makes a slow scan attributable to a kind, and it is what makes the refusal message deterministic
// when several kinds are unlistable -- an error naming a different kind on every retry reads as
// flapping rather than as a missing grant.
func (s *Source) referrerKinds(clusterScopedTarget bool) ([]schema.GroupVersionKind, error) {
	var (
		groups []*metav1.APIResourceList
		err    error
	)
	if clusterScopedTarget {
		_, groups, err = s.Discovery.ServerGroupsAndResources()
	} else {
		groups, err = s.Discovery.ServerPreferredNamespacedResources()
	}
	// A partial discovery result is the normal state of a cluster with one broken aggregated API
	// server, and unlike the denominator this scan cannot proceed on a partial one: a kind discovery
	// failed to report is a kind that is never listed and never found. So the error is fatal
	// whenever it is present at all, not only when it left nothing behind.
	if err != nil {
		return nil, fmt.Errorf(
			"discovering the kinds a referrer could be: %w (an inbound-reference scan over an incomplete kind set cannot report that nothing points at the target)", err)
	}
	if len(groups) == 0 {
		return nil, errors.New("the server reported no resources at all; a scan over no kinds finds no references, which is not the same answer as there being none")
	}

	seen := map[schema.GroupKind]bool{}
	var out []schema.GroupVersionKind
	for _, g := range groups {
		gv, parseErr := schema.ParseGroupVersion(g.GroupVersion)
		if parseErr != nil {
			return nil, fmt.Errorf("discovery reported an unparseable groupVersion %q: %w", g.GroupVersion, parseErr)
		}
		for _, r := range g.APIResources {
			// Subresources ("deployments/scale") are not objects and hold no ownerReferences of
			// their own.
			if strings.Contains(r.Name, "/") || !hasVerb(r.Verbs, "list") {
				continue
			}
			// An APIResource may carry its own group/version -- aggregated discovery populates them
			// and the per-group listing leaves them empty. Resolved into a LOCAL, because the outer
			// gv is the group's and overwriting it would leak one resource's version onto every
			// resource after it in the same group.
			rgv := gv
			if r.Group != "" || r.Version != "" {
				rgv = schema.GroupVersion{Group: r.Group, Version: r.Version}
			}
			// ServerGroupsAndResources returns EVERY served version, not the preferred one, so the
			// same kind arrives once per version. Deduplicating by GroupKind keeps one of them, and
			// one is right: every version of a kind is the same set of objects with the same
			// ownerReferences, so listing v1 and v1beta1 would report each dependent twice.
			gk := schema.GroupKind{Group: rgv.Group, Kind: r.Kind}
			if seen[gk] {
				continue
			}
			seen[gk] = true
			out = append(out, rgv.WithKind(r.Kind))
		}
	}
	if len(out) == 0 {
		return nil, errors.New("no listable kind was discovered; a scan over no kinds finds no references, which is not the same answer as there being none")
	}

	sort.Slice(out, func(i, j int) bool {
		if out[i].Group != out[j].Group {
			return out[i].Group < out[j].Group
		}
		if out[i].Kind != out[j].Kind {
			return out[i].Kind < out[j].Kind
		}
		return out[i].Version < out[j].Version
	})
	return out, nil
}

func hasVerb(verbs metav1.Verbs, want string) bool {
	for _, v := range verbs {
		if v == want {
			return true
		}
	}
	return false
}

// sortRefs makes the caveat text deterministic. undo.joinRefs prints the first three and counts the
// rest, so an unsorted slice would put a different three in the record on every generation and two
// runs over an unchanged cluster would produce two different plans.
func sortRefs(refs []undo.InboundRef) {
	sort.Slice(refs, func(i, j int) bool {
		a, b := refs[i].Ref, refs[j].Ref
		switch {
		case a.Group != b.Group:
			return a.Group < b.Group
		case a.Kind != b.Kind:
			return a.Kind < b.Kind
		case a.Namespace != b.Namespace:
			return a.Namespace < b.Namespace
		default:
			return a.Name < b.Name
		}
	})
}

func orCluster(ns string) string {
	if ns == "" {
		return "<cluster>"
	}
	return ns
}
