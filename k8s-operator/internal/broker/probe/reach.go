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
	"sort"

	discoveryv1 "k8s.io/api/discovery/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

// The Service / Ingress / Gateway row of 04 §5.1: "Endpoints populated and the programmed address
// resolvable". Both halves are live reads, and the two have opposite shapes -- the endpoint count
// walks OUTWARD from the target to the Services behind it, and the address walks nowhere at all,
// because an address is a fact the dataplane writes onto the object itself.

// serviceNameLabel is the well-known label the endpoint-slice controller stamps on every slice.
const serviceNameLabel = discoveryv1.LabelServiceName

// EndpointCount is the number of ready endpoints backing a target.
//
// # Why this walks
//
// verify.Prober documents this as "the number of ready endpoints backing a Service", but the row it
// serves covers Service, Ingress, Gateway and HTTPRoute, and only the first of those has endpoints
// of its own. The other three are routing objects: their endpoints are the endpoints of the
// Services they send traffic to. Counting zero for them -- which is what a Service-only
// implementation returns, since no EndpointSlice carries their name -- would hold every Ingress and
// every Gateway at Pending until its settle window expired, and then roll it back.
//
// So each kind resolves to a set of backing Services and the counts are summed. A Service resolves
// to itself.
//
// # The one case that errors
//
// A routing object that declares NO backend at all is an error, not zero. "This Ingress routes
// nowhere" and "this Ingress routes to pods that are not ready yet" are different facts with
// different remedies, and the predicate can only see the number. Zero would send the second
// message for the first situation.
//
// This has a consequence worth stating: a Gateway created before any HTTPRoute attaches to it
// declares no backend, so it errors, holds Pending, and is rolled back at the end of its window.
// That is a real false positive for the create-the-gateway-then-attach-routes order, and it is a
// FINDING against 04 §5.1's row rather than something this adapter may quietly decide -- the row
// says endpoints must be populated, and softening it here would be picking a spec answer inside an
// implementation unit. It is recorded in the ledger with the alternative named.
func (s *Source) EndpointCount(ctx context.Context, ref agentv1alpha1.TargetRef) (int, error) {
	if s.Client == nil {
		return 0, s.noClient("the endpoint count of " + describe(ref))
	}
	if ref.Namespace == "" {
		return 0, fmt.Errorf("%s has no namespace: endpoints are counted per namespace", describe(ref))
	}

	backends, err := s.backingServices(ctx, ref)
	if err != nil {
		return 0, err
	}
	if len(backends) == 0 {
		return 0, fmt.Errorf("%s names no backend Service, so it has no endpoints to populate; "+
			"reporting that as zero ready endpoints would say the backends are unready when the "+
			"fact is that there are none", describe(ref))
	}

	total := 0
	for _, name := range backends {
		n, err := s.readyEndpointsOf(ctx, ref.Namespace, name)
		if err != nil {
			return 0, err
		}
		total += n
	}
	return total, nil
}

// readyEndpointsOf counts the ready endpoints of one Service by its EndpointSlices.
//
// A nil Ready condition counts as ready, which is what the EndpointSlice API asks consumers to do:
// nil means "unknown", and the documented interpretation is ready. Counting nil as unready would
// report zero for every endpoint published by a controller that does not set the field, and the
// predicate reads zero as "not converged yet".
func (s *Source) readyEndpointsOf(ctx context.Context, namespace, service string) (int, error) {
	var slices discoveryv1.EndpointSliceList
	if err := s.Client.List(ctx, &slices,
		client.InNamespace(namespace),
		client.MatchingLabelsSelector{Selector: oneLabel(serviceNameLabel, service)},
	); err != nil {
		return 0, fmt.Errorf("listing the endpoint slices of Service %s/%s: %w", namespace, service, err)
	}

	n := 0
	for i := range slices.Items {
		for _, ep := range slices.Items[i].Endpoints {
			if ep.Conditions.Ready == nil || *ep.Conditions.Ready {
				n += len(ep.Addresses)
			}
		}
	}
	return n, nil
}

// backingServices resolves a target to the names of the Services whose endpoints back it, in the
// target's own namespace.
//
// Cross-namespace backend references are deliberately not followed. Gateway API allows them subject
// to a ReferenceGrant, and following one without checking the grant would have this adapter read
// across a tenant boundary the API server itself would refuse -- which is a worse outcome than an
// undercount, because the undercount only fails the verification. A cross-namespace backendRef is
// therefore skipped, and if it was the only one the caller gets the "names no backend" error.
func (s *Source) backingServices(ctx context.Context, ref agentv1alpha1.TargetRef) ([]string, error) {
	switch {
	case ref.Group == "" && ref.Kind == "Service":
		return []string{ref.Name}, nil

	case ref.Group == "networking.k8s.io" && ref.Kind == "Ingress":
		obj, err := s.Get(ctx, ref)
		if err != nil {
			return nil, fmt.Errorf("reading %s to find its backends: %w", describe(ref), err)
		}
		return ingressBackends(obj), nil

	case ref.Group == "gateway.networking.k8s.io" && ref.Kind == "HTTPRoute":
		obj, err := s.Get(ctx, ref)
		if err != nil {
			return nil, fmt.Errorf("reading %s to find its backends: %w", describe(ref), err)
		}
		return routeBackends(obj), nil

	case ref.Group == "gateway.networking.k8s.io" && ref.Kind == "Gateway":
		return s.gatewayBackends(ctx, ref)
	}
	return nil, fmt.Errorf("%s is not a kind whose endpoints this prober knows how to find; the "+
		"04 §5.1 reachability row covers Service, Ingress, Gateway and HTTPRoute", describe(ref))
}

// gatewayBackends walks the HTTPRoutes attached to a Gateway and unions their backends.
//
// A Gateway has no backends of its own -- attachment is expressed on the route, pointing up. So the
// only way to answer is to list routes and filter by parentRef, which is what the Gateway
// controller itself does.
func (s *Source) gatewayBackends(ctx context.Context, ref agentv1alpha1.TargetRef) ([]string, error) {
	routes := &unstructured.UnstructuredList{}
	routes.SetAPIVersion("gateway.networking.k8s.io/" + ref.Version)
	routes.SetKind("HTTPRouteList")
	if err := s.Client.List(ctx, routes, client.InNamespace(ref.Namespace)); err != nil {
		return nil, fmt.Errorf("listing the HTTPRoutes attached to %s: %w", describe(ref), err)
	}

	seen := map[string]bool{}
	for i := range routes.Items {
		if !routeAttachedTo(&routes.Items[i], ref) {
			continue
		}
		for _, b := range routeBackends(&routes.Items[i]) {
			seen[b] = true
		}
	}
	return sortedKeys(seen), nil
}

// routeAttachedTo reports whether an HTTPRoute names this Gateway in spec.parentRefs. A parentRef
// with no namespace means the route's own namespace, which is the Gateway's here because the list
// above was scoped to it.
func routeAttachedTo(route *unstructured.Unstructured, gw agentv1alpha1.TargetRef) bool {
	parents, _, _ := unstructured.NestedSlice(route.Object, "spec", "parentRefs")
	for _, raw := range parents {
		p, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		if name, _ := p["name"].(string); name != gw.Name {
			continue
		}
		// kind defaults to Gateway and group to the Gateway API group; an explicit different value
		// means this parentRef is something else entirely.
		if kind, ok := p["kind"].(string); ok && kind != "Gateway" {
			continue
		}
		if ns, ok := p["namespace"].(string); ok && ns != "" && ns != gw.Namespace {
			continue
		}
		return true
	}
	return false
}

// ingressBackends collects the Service names an Ingress routes to: the default backend plus every
// path in every rule. Resource backends (an Ingress may point at an arbitrary object) have no
// endpoints and are skipped.
func ingressBackends(obj *unstructured.Unstructured) []string {
	seen := map[string]bool{}
	if name, found, _ := unstructured.NestedString(obj.Object,
		"spec", "defaultBackend", "service", "name"); found && name != "" {
		seen[name] = true
	}
	rules, _, _ := unstructured.NestedSlice(obj.Object, "spec", "rules")
	for _, raw := range rules {
		rule, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		paths, _, _ := unstructured.NestedSlice(rule, "http", "paths")
		for _, rawPath := range paths {
			p, ok := rawPath.(map[string]any)
			if !ok {
				continue
			}
			if name, found, _ := unstructured.NestedString(p, "backend", "service", "name"); found && name != "" {
				seen[name] = true
			}
		}
	}
	return sortedKeys(seen)
}

// routeBackends collects the Service names an HTTPRoute sends traffic to. A backendRef with an
// explicit kind other than Service, or an explicit namespace other than the route's, is skipped --
// see backingServices on why cross-namespace refs are not followed.
func routeBackends(obj *unstructured.Unstructured) []string {
	seen := map[string]bool{}
	rules, _, _ := unstructured.NestedSlice(obj.Object, "spec", "rules")
	for _, raw := range rules {
		rule, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		refs, _, _ := unstructured.NestedSlice(rule, "backendRefs")
		for _, rawRef := range refs {
			b, ok := rawRef.(map[string]any)
			if !ok {
				continue
			}
			if kind, ok := b["kind"].(string); ok && kind != "Service" {
				continue
			}
			if ns, ok := b["namespace"].(string); ok && ns != "" && ns != obj.GetNamespace() {
				continue
			}
			if name, ok := b["name"].(string); ok && name != "" {
				seen[name] = true
			}
		}
	}
	return sortedKeys(seen)
}

// ProgrammedAddress is the address the dataplane actually programmed. Empty means not programmed
// yet, which the predicate reads as Pending -- so every empty return here must be a real
// observation of an empty status, never a read that did not happen.
//
// The three shapes, all of them status fields written by a controller rather than by the caller:
//
//   - Service: `status.loadBalancer.ingress[].ip|hostname` for a LoadBalancer, and `spec.clusterIP`
//     for everything else. A ClusterIP Service's programmed address IS its cluster IP, assigned by
//     the API server; requiring a load-balancer ingress for it would leave every ClusterIP Service
//     permanently unverifiable, which is most of them.
//   - Ingress: `status.loadBalancer.ingress[].ip|hostname`, the same field the Service uses.
//   - Gateway / HTTPRoute: `status.addresses[].value`. A route has no address of its own, so it
//     borrows its parent Gateway's -- resolved by reading the parent, because an HTTPRoute whose
//     Gateway has no address is not reachable however healthy its backends are.
func (s *Source) ProgrammedAddress(ctx context.Context, ref agentv1alpha1.TargetRef) (string, error) {
	if s.Client == nil {
		return "", s.noClient("the programmed address of " + describe(ref))
	}

	if ref.Group == "gateway.networking.k8s.io" && ref.Kind == "HTTPRoute" {
		return s.parentGatewayAddress(ctx, ref)
	}

	obj, err := s.Get(ctx, ref)
	if err != nil {
		return "", fmt.Errorf("reading %s to find its programmed address: %w", describe(ref), err)
	}

	switch {
	case ref.Group == "" && ref.Kind == "Service":
		if addr := loadBalancerAddress(obj); addr != "" {
			return addr, nil
		}
		typ, _, _ := unstructured.NestedString(obj.Object, "spec", "type")
		if typ == "LoadBalancer" {
			// Still waiting on the cloud controller. The cluster IP exists already and returning it
			// would report an unprovisioned load balancer as programmed.
			return "", nil
		}
		clusterIP, _, _ := unstructured.NestedString(obj.Object, "spec", "clusterIP")
		if clusterIP == "None" {
			// A headless Service is addressed by its DNS name and its endpoints, and it never gets
			// an IP. Its endpoints are the whole of its reachability, and they are counted by the
			// other half of this row.
			return headlessAddress(ref), nil
		}
		return clusterIP, nil

	case ref.Group == "networking.k8s.io" && ref.Kind == "Ingress":
		return loadBalancerAddress(obj), nil

	case ref.Group == "gateway.networking.k8s.io" && ref.Kind == "Gateway":
		return gatewayAddress(obj), nil
	}

	return "", fmt.Errorf("%s is not a kind with a programmed address; the 04 §5.1 reachability row "+
		"covers Service, Ingress, Gateway and HTTPRoute", describe(ref))
}

// parentGatewayAddress resolves an HTTPRoute's address through the first Gateway it attaches to
// that has one.
func (s *Source) parentGatewayAddress(ctx context.Context, ref agentv1alpha1.TargetRef) (string, error) {
	obj, err := s.Get(ctx, ref)
	if err != nil {
		return "", fmt.Errorf("reading %s to find its parent Gateway: %w", describe(ref), err)
	}
	parents, _, _ := unstructured.NestedSlice(obj.Object, "spec", "parentRefs")
	if len(parents) == 0 {
		return "", fmt.Errorf("%s has no parentRefs, so it is attached to nothing and has no "+
			"address; an unattached route is not a route waiting to be programmed", describe(ref))
	}
	for _, raw := range parents {
		p, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		if kind, ok := p["kind"].(string); ok && kind != "Gateway" {
			continue
		}
		name, _ := p["name"].(string)
		if name == "" {
			continue
		}
		ns, _ := p["namespace"].(string)
		if ns == "" {
			ns = ref.Namespace
		}
		gw := agentv1alpha1.TargetRef{
			Group: "gateway.networking.k8s.io", Version: ref.Version,
			Kind: "Gateway", Namespace: ns, Name: name,
		}
		parent, err := s.Get(ctx, gw)
		if err != nil {
			return "", fmt.Errorf("reading the parent %s of %s: %w", describe(gw), describe(ref), err)
		}
		if addr := gatewayAddress(parent); addr != "" {
			return addr, nil
		}
	}
	// Every parent read cleanly and none is programmed yet: a real empty, which is Pending.
	return "", nil
}

// loadBalancerAddress reads the shared Service/Ingress load-balancer status shape. IP first,
// hostname second -- an AWS-style hostname is a valid programmed address, and a GCP-style IP is
// the one that appears when both could.
func loadBalancerAddress(obj *unstructured.Unstructured) string {
	ingress, _, _ := unstructured.NestedSlice(obj.Object, "status", "loadBalancer", "ingress")
	for _, raw := range ingress {
		e, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		if ip, _ := e["ip"].(string); ip != "" {
			return ip
		}
		if host, _ := e["hostname"].(string); host != "" {
			return host
		}
	}
	return ""
}

// gatewayAddress reads `status.addresses[].value`.
func gatewayAddress(obj *unstructured.Unstructured) string {
	addrs, _, _ := unstructured.NestedSlice(obj.Object, "status", "addresses")
	for _, raw := range addrs {
		a, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		if v, _ := a["value"].(string); v != "" {
			return v
		}
	}
	return ""
}

// headlessAddress is the in-cluster DNS name of a headless Service. It is returned rather than an
// empty string because a headless Service has reached its final addressable state the moment it
// exists -- there is nothing further for a controller to program -- and an empty return would hold
// it Pending until its window expired.
func headlessAddress(ref agentv1alpha1.TargetRef) string {
	return fmt.Sprintf("%s.%s.svc (headless)", ref.Name, ref.Namespace)
}

func sortedKeys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
