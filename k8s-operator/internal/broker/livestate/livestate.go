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

// Package livestate is the classifier's eyes on the cluster: the production implementation of
// classify.LiveState, backed by a controller-runtime client and a discovery client.
//
// # Why this is not in package classify
//
// It was, for exactly as long as it took the L0 chain to object. V-GAT-017 (the L0 half lives in
// dev/tests/classifier-is-model-free.py) holds a CLOSED import allowlist over internal/broker/
// classify and that allowlist deliberately contains no Kubernetes client of any kind, because the
// classifier is handed already-resolved facts precisely so that it cannot go and look anything up.
// The check is right and the fix is this package, not a wider list. Two properties depend on the
// separation. The classifier stays hermetic, so its envelope corpus can permute every input and get
// a byte-identical answer -- a rule that reached for a client would be a rule the corpus cannot
// see. And the allowlist stays a conversation rather than a diff: the failure V-GAT-017 exists to
// prevent is not somebody importing an inference SDK on purpose, it is a plausible refactor that
// widens the list by one line at a time until the gate has an opinion in it.
//
// So classify declares the interface and this package implements it, which is the same seam
// internal/broker/policy already uses for ChangePolicy. Nothing here decides anything: a rule
// implemented in an adapter is a rule that classifies differently depending on what the cluster
// answered, which is the property the adapter exists to feed, not to hold.
package livestate

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/discovery"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/classify"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/scope"
)

// liveLog is this file's only side channel. The rest of the package is pure and logs nothing; the
// adapter needs one because a denominator that is quietly missing half the cluster is not visible
// in any value it returns.
var liveLog = logf.Log.WithName("classify-livestate")

// Source is the production classify.LiveState.
//
// Everything in package classify decides from a ResolvedOp and needs no cluster, which is what
// makes the 165-envelope corpus hermetic. This is where the facts that corpus is handed actually
// come from, and it is deliberately thin -- a rule implemented in here is a rule the corpus cannot
// see. See the package comment for why the two live apart.
//
// # The direction each failure has to fall
//
// Four of the five methods have their error returned to Resolve, which turns it into a step-3 fault
// and refuses the action. One does not: ComputeBlastRadius absorbs CountWorkloadObjects' error into
// BlastRadius.DenominatorUnavailable and yields a nil fraction. That asymmetry decides how this
// type handles partial visibility, and it decides it in the direction that is easy to get backwards
// -- see CountWorkloadObjects.
//
// # Why the caches are here and not in a shared informer
//
// Two of the reads are per-scope rather than per-object, and both are expensive: the denominator is
// a List of every workload kind in scope, and the digest set is a List of every Secret in scope.
// The spec bounds both at 60 seconds (classify.DenominatorMaxStalenessSeconds, classify.DigestCacheTTLSeconds) and
// says why: a stale denominator is a wrong fraction and a stale digest set misses a rotated value.
// A TTL cache keyed on scope honours exactly that bound and expires on a clock this type can be
// handed in a test. An informer would honour no bound at all, for the reason
// broker.MaxFreezeStaleness spells out.
//
// The per-object reads (GetObject, GetNamespaceLabels) are NOT cached. They are the live state an
// operation is classified against, they are one Get each, and a cached answer to "does this object
// exist right now" is the one answer that must not be a few seconds old.
type Source struct {
	// Client reads objects. A direct client, not a cached one: see the type comment.
	Client client.Client

	// Discovery enumerates the kinds the denominator counts. Optional -- a nil Discovery makes
	// CountWorkloadObjects return an error, which is the honest answer for a broker that cannot
	// find out what kinds exist, and which yields a nil fraction rather than a wrong one.
	Discovery discovery.ServerResourcesInterface

	// Now is injectable so a test can expire a cache without sleeping.
	Now func() time.Time

	mu       sync.Mutex
	counts   map[string]countEntry
	digests  map[string]digestEntry
	warnOnce sync.Map
}

type countEntry struct {
	total int
	at    time.Time
}

type digestEntry struct {
	set *classify.DigestSet
	at  time.Time
}

var _ classify.LiveState = (*Source)(nil)

func (l *Source) now() time.Time {
	if l.Now != nil {
		return l.Now()
	}
	return time.Now()
}

// GetObject returns the live labels and annotations of a target.
//
// A kind the API server does not serve is an ERROR, not exists=false. That is the less obvious of
// the two options and it is [[LSN-032]]'s lesson applied one layer out: a wrong API group does not
// announce itself, it silently never matches, and every party to the comparison agrees with it. An
// envelope naming `apps/Deploymnet` should be refused with the kind quoted back, not classified as
// an operation on an object that happens not to exist -- which for `create` is the ordinary case
// and would sail through.
func (l *Source) GetObject(ctx context.Context, kind classify.KindRef, namespace, name string) (map[string]string, map[string]string, bool, error) {
	obj := &metav1.PartialObjectMetadata{}
	gvk, err := l.gvkFor(kind)
	if err != nil {
		return nil, nil, false, err
	}
	obj.SetGroupVersionKind(gvk)

	err = l.Client.Get(ctx, client.ObjectKey{Namespace: namespace, Name: name}, obj)
	switch {
	case apierrors.IsNotFound(err):
		// Absent is an answer, and a common one: every `create` resolves through here.
		return nil, nil, false, nil
	case err != nil:
		return nil, nil, false, fmt.Errorf("get %s %s/%s: %w", kind, orCluster(namespace), name, err)
	}
	return obj.GetLabels(), obj.GetAnnotations(), true, nil
}

// GetNamespaceLabels returns a namespace's labels.
//
// Unlike GetObject, a missing namespace here is exists=false with no error, and the interface says
// so: "a create into a namespace that does not yet exist is a legitimate operation". The difference
// between the two methods is not inconsistency -- Namespace is a kind the server certainly serves,
// so the only thing a NotFound can mean is that the namespace is not there yet.
func (l *Source) GetNamespaceLabels(ctx context.Context, namespace string) (map[string]string, bool, error) {
	if namespace == "" {
		return nil, false, nil
	}
	var ns corev1.Namespace
	err := l.Client.Get(ctx, client.ObjectKey{Name: namespace}, &ns)
	switch {
	case apierrors.IsNotFound(err):
		return nil, false, nil
	case err != nil:
		return nil, false, fmt.Errorf("get namespace %q: %w", namespace, err)
	}
	return ns.Labels, true, nil
}

// CountWorkloadObjects returns the blast-radius denominator for a scope.
//
// # A hole in the count is kept; an empty count is refused
//
// This is the one method whose error is absorbed rather than surfaced, and working out which way to
// fail took reading what the absorption does. ComputeBlastRadius turns an error into a NIL
// fraction, and a nil fraction disarms AbortScopeFraction completely -- the rule cannot fire. A
// missing kind, by contrast, makes the denominator SMALLER, which makes every fraction LARGER,
// which makes the abort MORE likely.
//
// So the reflex -- "I could not see everything, therefore I must refuse to answer" -- is backwards
// here. Refusing is the loosening direction. Under-counting is the tightening one. This method
// therefore counts every kind it can list, skips the ones it cannot, and returns an error only when
// it established nothing at all: no discovery, or every single List denied. A tier whose RBAC
// covers three kinds gets a denominator over three kinds, biased small, with the rule still armed.
//
// The skipped kinds are logged once each rather than silently dropped, because "the denominator is
// small because this broker cannot see much" and "the denominator is small because the namespace is
// small" produce the same number and an operator needs to be able to tell them apart.
//
// # What counts
//
// Namespaced kinds the server says are listable, minus classify.ExcludedFromDenominator, minus every object
// carrying an ownerReference. blast.go explains the exclusions and explicitly leaves the
// ownerReference half here, "which can see the objects": a Deployment's ReplicaSets are named in
// the list, but a CRD's generated children are not, and both are the cluster creating things on
// your behalf. Listing PartialObjectMetadata rather than whole objects is what makes that check
// affordable -- ownerReferences are metadata, so nothing pulls a spec.
//
// Kinds are discovered, never enumerated in a literal here. A hardcoded list of "the workload
// kinds" is [[LSN-036]] waiting to happen: it would be correct on the day it was written and would
// silently stop covering the CRD a customer installed the week after.
func (l *Source) CountWorkloadObjects(ctx context.Context, s scope.Scope) (int, error) {
	key := scopeKey(s)
	if n, ok := l.cachedCount(key); ok {
		return n, nil
	}
	if l.Discovery == nil {
		return 0, errors.New("no discovery client: the set of kinds to count cannot be established, so there is no denominator")
	}

	groups, err := l.Discovery.ServerPreferredNamespacedResources()
	// A partial discovery result is usable and is the normal state of a cluster with a broken
	// aggregated API server. The error is retained only if it left us with nothing.
	if len(groups) == 0 {
		if err == nil {
			err = errors.New("the server reported no namespaced resources")
		}
		return 0, fmt.Errorf("discovering namespaced kinds: %w", err)
	}

	var opts []client.ListOption
	if s.Namespace != "" {
		opts = append(opts, client.InNamespace(s.Namespace))
	}

	total, listed, skipped := 0, 0, 0
	for _, gk := range countableKinds(groups) {
		var list metav1.PartialObjectMetadataList
		list.SetGroupVersionKind(gk.GroupVersion().WithKind(gk.Kind + "List"))
		if err := l.Client.List(ctx, &list, opts...); err != nil {
			// Forbidden is the expected case for a narrow tier and is not a defect. Anything else is
			// treated the same way for the reason in the doc comment: the count survives holes.
			skipped++
			l.warnSkipped(gk, err)
			continue
		}
		listed++
		for i := range list.Items {
			if len(list.Items[i].OwnerReferences) > 0 {
				continue
			}
			total++
		}
	}
	if listed == 0 {
		return 0, fmt.Errorf("none of the %d discovered kinds could be listed in scope %s; the denominator is unknown, not zero", skipped, key)
	}

	l.storeCount(key, total)
	return total, nil
}

// SecretDigests returns the digest set for a scope.
//
// # The values are read, hashed, and dropped in one call
//
// This method is the only code in the product that reads Secret VALUES in bulk, and secretegress.go
// already names the hazard it creates: "a long-lived in-memory map of plaintext secret values
// inside the broker is a much better target than the Secrets themselves, since it is pre-collected
// and cross-namespace." classify.NewDigestSet retains only hex digests and (namespace, secret, key) labels,
// so the plaintext's lifetime is this function -- and the copies this function made are zeroed
// before it returns, so a heap dump taken a second later has no window into them. The Secret
// objects the client decoded are beyond reach and are not pretended otherwise; what is in reach is
// zeroed.
//
// A failed List is an error, and here that IS the fail-closed direction -- the opposite of
// CountWorkloadObjects, for the opposite reason. Resolve surfaces this error and refuses the
// action, whereas an empty digest set would report "no secret material in this payload" for every
// payload, which is the exfiltration gate answering yes to everything.
//
// service-account-token Secrets are counted like any other. They hold a real credential, and a
// broker that special-cased them would be deciding that one class of credential is fine to write
// into a ConfigMap.
func (l *Source) SecretDigests(ctx context.Context, s scope.Scope) (*classify.DigestSet, error) {
	key := scopeKey(s)
	if ds, ok := l.cachedDigests(key); ok {
		return ds, nil
	}

	var opts []client.ListOption
	if s.Namespace != "" {
		opts = append(opts, client.InNamespace(s.Namespace))
	}
	var secrets corev1.SecretList
	if err := l.Client.List(ctx, &secrets, opts...); err != nil {
		return nil, fmt.Errorf("listing Secrets in scope %s for the material-egress scan: %w", key, err)
	}

	byNS := make(map[string]map[string]map[string][]byte, len(secrets.Items))
	for i := range secrets.Items {
		sec := &secrets.Items[i]
		ns := byNS[sec.Namespace]
		if ns == nil {
			ns = map[string]map[string][]byte{}
			byNS[sec.Namespace] = ns
		}
		ns[sec.Name] = sec.Data
	}
	ds := classify.NewDigestSet(byNS)

	// Zero what we can still reach. Deferring this would be tidier and would also keep the values
	// alive across the classify.NewDigestSet call for no reason.
	for i := range secrets.Items {
		for _, v := range secrets.Items[i].Data {
			for j := range v {
				v[j] = 0
			}
		}
		secrets.Items[i].Data = nil
		secrets.Items[i].StringData = nil
	}

	l.storeDigests(key, ds)
	return ds, nil
}

// LowerTierOwner returns the Agent CR that owns a target and sits strictly beneath the caller.
//
// The predicate is classify.OwnerLookup.Find, which already exists and is corpus-tested; this method's whole
// job is to hand it the live Agent set. A failed List is an error rather than "no owner": "no lower
// tier claims this object" is the answer that skips the ownership gate, so returning it when the
// truth is "I could not look" would drop the gate exactly when the API server is unhealthy.
func (l *Source) LowerTierOwner(ctx context.Context, caller classify.Caller, _ classify.KindRef, namespace, _ string) (string, error) {
	var agents agentv1alpha1.AgentList
	if err := l.Client.List(ctx, &agents); err != nil {
		return "", fmt.Errorf("listing Agents to find a lower-tier owner: %w", err)
	}
	return classify.OwnerLookup{Agents: agents.Items}.Find(caller, classify.ScopeOfTarget(caller, namespace))
}

// gvkFor resolves a classify.KindRef to the server's preferred version for that kind.
//
// classify.KindRef carries no version on purpose -- 06 §3 writes rules as (group, kind) because a rule about
// Deployments is not a rule about apps/v1 in particular. The RESTMapper is what turns that back
// into something the client can fetch, and it is the API server's own answer rather than a table
// here.
func (l *Source) gvkFor(kind classify.KindRef) (schema.GroupVersionKind, error) {
	if kind.Kind == "" {
		return schema.GroupVersionKind{}, errors.New("operation names no kind")
	}
	mapping, err := l.Client.RESTMapper().RESTMapping(schema.GroupKind{Group: kind.Group, Kind: kind.Kind})
	if err != nil {
		if meta.IsNoMatchError(err) {
			return schema.GroupVersionKind{}, fmt.Errorf(
				"this cluster serves no kind %q: the operation names a group or kind that does not exist here, which is refused rather than treated as an object that is merely absent", kind)
		}
		return schema.GroupVersionKind{}, fmt.Errorf("resolving kind %q: %w", kind, err)
	}
	return mapping.GroupVersionKind, nil
}

// countableKinds reduces a discovery result to the GroupVersions and Kinds the denominator counts.
//
// Deduplicated by (group, kind) because ServerPreferredNamespacedResources can still surface the
// same kind twice, and counting a kind twice inflates the denominator -- which is the disarming
// direction. Sorted so that two runs against the same cluster produce the same number of List calls
// in the same order, which is what makes a slow one attributable.
func countableKinds(groups []*metav1.APIResourceList) []schema.GroupVersionKind {
	seen := map[classify.KindRef]bool{}
	var out []schema.GroupVersionKind
	for _, g := range groups {
		gv, err := schema.ParseGroupVersion(g.GroupVersion)
		if err != nil {
			continue
		}
		for _, r := range g.APIResources {
			// Subresources ("deployments/scale") are not objects and are never listable anyway.
			if strings.Contains(r.Name, "/") || !r.Namespaced {
				continue
			}
			if !hasVerb(r.Verbs, "list") {
				continue
			}
			k := classify.KindRef{Group: gv.Group, Kind: r.Kind}
			if seen[k] || classify.IsExcludedFromDenominator(k) {
				continue
			}
			seen[k] = true
			out = append(out, gv.WithKind(r.Kind))
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Group != out[j].Group {
			return out[i].Group < out[j].Group
		}
		return out[i].Kind < out[j].Kind
	})
	return out
}

func hasVerb(verbs metav1.Verbs, want string) bool {
	for _, v := range verbs {
		if v == want {
			return true
		}
	}
	return false
}

// warnSkipped records a kind the denominator could not see, once per kind per process.
//
// Once, because this runs per classification and a broker serving a narrow tier would otherwise
// emit the same handful of lines for every action it ever handles, which is how a real signal gets
// filtered out.
func (l *Source) warnSkipped(gk schema.GroupVersionKind, err error) {
	if _, loaded := l.warnOnce.LoadOrStore(gk.String(), true); loaded {
		return
	}
	liveLog.Info("blast-radius denominator is missing a kind; the count is biased small, which biases the abort rule toward firing",
		"kind", gk.String(), "reason", err.Error())
}

func (l *Source) cachedCount(key string) (int, bool) {
	l.mu.Lock()
	defer l.mu.Unlock()
	e, ok := l.counts[key]
	if !ok || l.now().Sub(e.at) > classify.DenominatorMaxStalenessSeconds*time.Second {
		return 0, false
	}
	return e.total, true
}

func (l *Source) storeCount(key string, total int) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.counts == nil {
		l.counts = map[string]countEntry{}
	}
	l.counts[key] = countEntry{total: total, at: l.now()}
}

func (l *Source) cachedDigests(key string) (*classify.DigestSet, bool) {
	l.mu.Lock()
	defer l.mu.Unlock()
	e, ok := l.digests[key]
	if !ok || l.now().Sub(e.at) > classify.DigestCacheTTLSeconds*time.Second {
		return nil, false
	}
	return e.set, true
}

func (l *Source) storeDigests(key string, ds *classify.DigestSet) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.digests == nil {
		l.digests = map[string]digestEntry{}
	}
	l.digests[key] = digestEntry{set: ds, at: l.now()}
}

// scopeKey is the cache key. Every level is included and separated by a character that cannot
// appear in any of them, so two different scopes cannot collide into one cached answer.
func scopeKey(s scope.Scope) string {
	return s.ProjectID + "/" + s.ClusterName + "/" + s.Namespace
}

func orCluster(ns string) string {
	if ns == "" {
		return "<cluster>"
	}
	return ns
}
