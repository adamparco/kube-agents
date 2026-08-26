package livestate

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/classify"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/scope"
)

// The hermetic half of Source's coverage: the kind-selection, cache and failure-direction
// logic, all of which are decisions this file makes rather than answers the API server gives.
//
// The other half -- that the five methods read what a real API server actually serves -- is not
// provable here and is not attempted here. A fake client agrees with whatever shape the caller
// assumed; PartialObjectMetadata in particular is served by the metadata client and is not something
// controller-runtime's fake tracker models at all. Those properties live in the envtest suite and in
// the L2 script, at the levels where they can fail. Asserting them against a stub would be a green
// result about code that never ran ([[LSN-001]]'s shape, one layer in).

// listStub intercepts List and nothing else. The embedded nil client.Client is deliberate: if a test
// causes a call this stub does not implement, it panics rather than quietly succeeding.
type listStub struct {
	client.Client
	list  func(ctx context.Context, l client.ObjectList, opts ...client.ListOption) error
	calls int
	opts  [][]client.ListOption
}

func (s *listStub) List(ctx context.Context, l client.ObjectList, opts ...client.ListOption) error {
	s.calls++
	s.opts = append(s.opts, opts)
	return s.list(ctx, l, opts...)
}

// discoveryStub is the four-method ServerResourcesInterface. Only the fourth is ever called.
type discoveryStub struct {
	namespaced []*metav1.APIResourceList
	err        error
}

func (d discoveryStub) ServerResourcesForGroupVersion(string) (*metav1.APIResourceList, error) {
	return nil, errors.New("not used")
}

func (d discoveryStub) ServerGroupsAndResources() ([]*metav1.APIGroup, []*metav1.APIResourceList, error) {
	return nil, d.namespaced, d.err
}

func (d discoveryStub) ServerPreferredResources() ([]*metav1.APIResourceList, error) {
	return d.namespaced, d.err
}

func (d discoveryStub) ServerPreferredNamespacedResources() ([]*metav1.APIResourceList, error) {
	return d.namespaced, d.err
}

func listable(kind string) metav1.APIResource {
	return metav1.APIResource{
		Name:       lowerPlural(kind),
		Kind:       kind,
		Namespaced: true,
		Verbs:      metav1.Verbs{"get", "list", "watch"},
	}
}

// lowerPlural is only good enough to make the fixtures readable; nothing under test parses it.
func lowerPlural(kind string) string {
	out := make([]byte, 0, len(kind)+1)
	for i := 0; i < len(kind); i++ {
		c := kind[i]
		if c >= 'A' && c <= 'Z' {
			c += 'a' - 'A'
		}
		out = append(out, c)
	}
	return string(append(out, 's'))
}

func testScope() scope.Scope {
	return scope.Scope{ProjectID: "p", ClusterName: "c", Namespace: "team-a"}
}

// ---------------------------------------------------------------------------------------------
// countableKinds
// ---------------------------------------------------------------------------------------------

// Every reason a kind is dropped, in one fixture, with the survivors named exactly. The four
// exclusion reasons are independent and a filter that lost one of them would still drop most of
// this list, so each is asserted by the presence of its own survivor rather than by the count.
func TestCountableKindsDropsWhatItMust(t *testing.T) {
	groups := []*metav1.APIResourceList{
		{
			GroupVersion: "v1",
			APIResources: []metav1.APIResource{
				listable("ConfigMap"),
				listable("Pod"), // excluded by blast.go
				{Name: "nodes", Kind: "Node", Namespaced: false, Verbs: metav1.Verbs{"list"}},
				{Name: "bindings", Kind: "Binding", Namespaced: true, Verbs: metav1.Verbs{"create"}},
			},
		},
		{
			GroupVersion: "apps/v1",
			APIResources: []metav1.APIResource{
				listable("Deployment"),
				{Name: "deployments/scale", Kind: "Scale", Namespaced: true, Verbs: metav1.Verbs{"get", "update"}},
				listable("ReplicaSet"), // excluded by blast.go
			},
		},
		{
			// Unparseable. ParseGroupVersion accepts a bare token as a version, so the only string it
			// rejects is one with more than one slash -- which is the shape a corrupt or
			// non-conforming aggregated API server actually emits.
			GroupVersion: "too/many/slashes",
			APIResources: []metav1.APIResource{listable("Ghost")},
		},
	}

	got := countableKinds(groups)
	want := []schema.GroupVersionKind{
		{Group: "", Version: "v1", Kind: "ConfigMap"},
		{Group: "apps", Version: "v1", Kind: "Deployment"},
	}
	if len(got) != len(want) {
		t.Fatalf("countableKinds = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("countableKinds[%d] = %v, want %v (full: %v)", i, got[i], want[i], got)
		}
	}
}

// Deduplication and ordering. Both matter for the same reason and neither is cosmetic: a kind
// counted twice inflates the denominator, which shrinks every fraction, which is the direction that
// disarms AbortScopeFraction.
func TestCountableKindsDedupesAcrossGroupVersions(t *testing.T) {
	groups := []*metav1.APIResourceList{
		{GroupVersion: "apps/v1beta2", APIResources: []metav1.APIResource{listable("StatefulSet")}},
		{GroupVersion: "apps/v1", APIResources: []metav1.APIResource{listable("StatefulSet"), listable("Deployment")}},
		{GroupVersion: "batch/v1", APIResources: []metav1.APIResource{listable("Job")}},
	}

	got := countableKinds(groups)
	if len(got) != 3 {
		t.Fatalf("want 3 kinds after dedupe, got %d: %v", len(got), got)
	}
	// First occurrence wins the version; the sort is by (group, kind) and ignores version.
	want := []schema.GroupVersionKind{
		{Group: "apps", Version: "v1", Kind: "Deployment"},
		{Group: "apps", Version: "v1beta2", Kind: "StatefulSet"},
		{Group: "batch", Version: "v1", Kind: "Job"},
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("countableKinds[%d] = %v, want %v (full: %v)", i, got[i], want[i], got)
		}
	}
}

// The exclusion list is read from blast.go rather than restated here. A literal copy would pass
// forever after someone added a kind to classify.ExcludedFromDenominator and not to the filter.
func TestCountableKindsHonoursEveryDeclaredExclusion(t *testing.T) {
	var res []metav1.APIResource
	for _, k := range classify.ExcludedFromDenominator {
		res = append(res, listable(k.Kind))
	}
	// One kind that is not excluded, so a filter that dropped everything cannot pass.
	res = append(res, listable("KageCanary"))

	byGroup := map[string][]metav1.APIResource{}
	for i, k := range classify.ExcludedFromDenominator {
		gv := "v1"
		if k.Group != "" {
			gv = k.Group + "/v1"
		}
		byGroup[gv] = append(byGroup[gv], res[i])
	}
	byGroup["kubeagents.x-k8s.io/v1alpha1"] = append(byGroup["kubeagents.x-k8s.io/v1alpha1"], res[len(res)-1])

	var groups []*metav1.APIResourceList
	for gv, rs := range byGroup {
		groups = append(groups, &metav1.APIResourceList{GroupVersion: gv, APIResources: rs})
	}

	got := countableKinds(groups)
	if len(got) != 1 || got[0].Kind != "KageCanary" {
		t.Fatalf("countableKinds kept an excluded kind: %v", got)
	}
}

// ---------------------------------------------------------------------------------------------
// CountWorkloadObjects — the failure direction this adapter exists to get right
// ---------------------------------------------------------------------------------------------

func TestCountWorkloadObjectsWithoutDiscoveryIsAnError(t *testing.T) {
	l := &Source{}
	if _, err := l.CountWorkloadObjects(context.Background(), testScope()); err == nil {
		t.Fatal("a nil Discovery must be an error; a zero denominator would be a lie the fraction rule believes")
	}
}

func TestCountWorkloadObjectsWithEmptyDiscoveryIsAnError(t *testing.T) {
	l := &Source{Discovery: discoveryStub{}}
	if _, err := l.CountWorkloadObjects(context.Background(), testScope()); err == nil {
		t.Fatal("discovery returning no namespaced kinds must be an error, not a denominator of zero")
	}
}

// A partial discovery result is the normal state of a cluster with a broken aggregated API server.
// It is usable, so the accompanying error is dropped on the floor rather than surfaced.
func TestCountWorkloadObjectsUsesPartialDiscovery(t *testing.T) {
	stub := &listStub{list: func(_ context.Context, l client.ObjectList, _ ...client.ListOption) error {
		l.(*metav1.PartialObjectMetadataList).Items = []metav1.PartialObjectMetadata{{}}
		return nil
	}}
	l := &Source{
		Client: stub,
		Discovery: discoveryStub{
			namespaced: []*metav1.APIResourceList{
				{GroupVersion: "apps/v1", APIResources: []metav1.APIResource{listable("Deployment")}},
			},
			err: errors.New("unable to retrieve the complete list of server APIs: metrics.k8s.io/v1beta1"),
		},
	}
	n, err := l.CountWorkloadObjects(context.Background(), testScope())
	if err != nil {
		t.Fatalf("a partial discovery result must still produce a denominator: %v", err)
	}
	if n != 1 {
		t.Fatalf("count = %d, want 1", n)
	}
}

// The unit's central claim, asserted rather than argued: a kind the broker cannot list is SKIPPED,
// and the count comes back from the kinds it could see. The alternative -- erroring -- is absorbed
// by ComputeBlastRadius into a nil fraction, which disarms AbortScopeFraction entirely. Skipping
// biases the denominator small, which biases every fraction large, which biases the abort toward
// firing. Tightening, not loosening.
func TestCountWorkloadObjectsSkipsKindsItCannotList(t *testing.T) {
	forbidden := apierrors.NewForbidden(schema.GroupResource{Group: "batch", Resource: "jobs"}, "", errors.New("nope"))
	stub := &listStub{list: func(_ context.Context, l client.ObjectList, _ ...client.ListOption) error {
		pl := l.(*metav1.PartialObjectMetadataList)
		if pl.GroupVersionKind().Group == "batch" {
			return forbidden
		}
		pl.Items = []metav1.PartialObjectMetadata{{}, {}, {}}
		return nil
	}}
	l := &Source{Client: stub, Discovery: discoveryStub{namespaced: []*metav1.APIResourceList{
		{GroupVersion: "apps/v1", APIResources: []metav1.APIResource{listable("Deployment")}},
		{GroupVersion: "batch/v1", APIResources: []metav1.APIResource{listable("Job"), listable("CronJob")}},
	}}}

	n, err := l.CountWorkloadObjects(context.Background(), testScope())
	if err != nil {
		t.Fatalf("a forbidden kind must not fail the count: %v", err)
	}
	if n != 3 {
		t.Fatalf("count = %d, want 3 (the one listable kind); a skipped kind must subtract from the denominator, not nil it", n)
	}
}

// The floor under the previous test. Skipping every kind is not "the scope is empty" -- nothing was
// established, so there is no denominator and the error is the honest answer.
func TestCountWorkloadObjectsErrorsWhenNothingCouldBeListed(t *testing.T) {
	stub := &listStub{list: func(_ context.Context, _ client.ObjectList, _ ...client.ListOption) error {
		return apierrors.NewForbidden(schema.GroupResource{Resource: "configmaps"}, "", errors.New("nope"))
	}}
	l := &Source{Client: stub, Discovery: discoveryStub{namespaced: []*metav1.APIResourceList{
		{GroupVersion: "v1", APIResources: []metav1.APIResource{listable("ConfigMap")}},
	}}}

	_, err := l.CountWorkloadObjects(context.Background(), testScope())
	if err == nil {
		t.Fatal("every kind denied must be an error, not a denominator of zero")
	}
	if got := err.Error(); !strings.Contains(got, "unknown, not zero") {
		t.Fatalf("error does not distinguish unknown from zero: %q", got)
	}
}

// blast.go excludes Pods and ReplicaSets by kind and leaves the general case here: "objects with an
// ownerReference are the cluster creating things on your behalf". A CRD's generated children are not
// in any static list, so this is the half that actually covers them.
func TestCountWorkloadObjectsSkipsOwnedObjects(t *testing.T) {
	owned := metav1.PartialObjectMetadata{
		ObjectMeta: metav1.ObjectMeta{OwnerReferences: []metav1.OwnerReference{{Kind: "Deployment", Name: "d"}}},
	}
	stub := &listStub{list: func(_ context.Context, l client.ObjectList, _ ...client.ListOption) error {
		l.(*metav1.PartialObjectMetadataList).Items = []metav1.PartialObjectMetadata{{}, owned, {}, owned}
		return nil
	}}
	l := &Source{Client: stub, Discovery: discoveryStub{namespaced: []*metav1.APIResourceList{
		{GroupVersion: "v1", APIResources: []metav1.APIResource{listable("ConfigMap")}},
	}}}

	n, err := l.CountWorkloadObjects(context.Background(), testScope())
	if err != nil {
		t.Fatal(err)
	}
	if n != 2 {
		t.Fatalf("count = %d, want 2; owned objects are the cluster's, not the operator's", n)
	}
}

// A namespaced scope must not count the whole cluster. Getting this wrong inflates the denominator
// by orders of magnitude and is invisible in every value the method returns.
func TestCountWorkloadObjectsScopesTheListToTheNamespace(t *testing.T) {
	stub := &listStub{list: func(_ context.Context, _ client.ObjectList, _ ...client.ListOption) error { return nil }}
	l := &Source{Client: stub, Discovery: discoveryStub{namespaced: []*metav1.APIResourceList{
		{GroupVersion: "v1", APIResources: []metav1.APIResource{listable("ConfigMap")}},
	}}}

	if _, err := l.CountWorkloadObjects(context.Background(), testScope()); err != nil {
		t.Fatal(err)
	}
	if len(stub.opts) != 1 || len(stub.opts[0]) != 1 {
		t.Fatalf("want exactly one List carrying one option, got %v", stub.opts)
	}
	var got client.ListOptions
	stub.opts[0][0].ApplyToList(&got)
	if got.Namespace != "team-a" {
		t.Fatalf("List namespace = %q, want team-a", got.Namespace)
	}

	// Cluster scope: no namespace option at all, not an empty one.
	stub.opts = nil
	l2 := &Source{Client: stub, Discovery: l.Discovery}
	if _, err := l2.CountWorkloadObjects(context.Background(), scope.Scope{ProjectID: "p", ClusterName: "c"}); err != nil {
		t.Fatal(err)
	}
	if len(stub.opts) != 1 || len(stub.opts[0]) != 0 {
		t.Fatalf("a cluster-scoped count must pass no namespace option, got %v", stub.opts)
	}
}

// ---------------------------------------------------------------------------------------------
// SecretDigests — the opposite failure direction, for the opposite reason
// ---------------------------------------------------------------------------------------------

func TestSecretDigestsFailsClosedOnListError(t *testing.T) {
	stub := &listStub{list: func(_ context.Context, _ client.ObjectList, _ ...client.ListOption) error {
		return errors.New("etcd is having a day")
	}}
	l := &Source{Client: stub}
	ds, err := l.SecretDigests(context.Background(), testScope())
	if err == nil {
		t.Fatal("an unreadable Secret list must be an error: an empty digest set is the exfiltration gate answering yes to everything")
	}
	if ds != nil {
		t.Fatal("no digest set may be returned alongside the error")
	}
}

// The digests are real (a payload carrying the value is found) and the plaintext this method could
// still reach is gone by the time it returns. The second half is asserted through a slice the test
// retains a reference to -- the only way to observe a zeroing from outside.
func TestSecretDigestsHashesThenZeroesThePlaintext(t *testing.T) {
	const value = "s3cr3t-database-password-9f2a"
	plaintext := []byte(value)

	stub := &listStub{list: func(_ context.Context, l client.ObjectList, _ ...client.ListOption) error {
		l.(*corev1.SecretList).Items = []corev1.Secret{{
			ObjectMeta: metav1.ObjectMeta{Namespace: "team-a", Name: "db-creds"},
			Data:       map[string][]byte{"password": plaintext},
			StringData: map[string]string{"password": value},
		}}
		return nil
	}}
	l := &Source{Client: stub}

	ds, err := l.SecretDigests(context.Background(), testScope())
	if err != nil {
		t.Fatal(err)
	}
	hits := classify.ScanPayload(ds, map[string]any{"data": map[string]any{"DB_PASSWORD": value}}, "")
	if len(hits) != 1 || hits[0].Namespace != "team-a" || hits[0].Secret != "db-creds" || hits[0].Key != "password" {
		t.Fatalf("digest set does not identify the secret it was built from: %v", hits)
	}

	for i, b := range plaintext {
		if b != 0 {
			t.Fatalf("plaintext byte %d survived at %q: the read values must not outlive this call", i, plaintext)
		}
	}
}

// ---------------------------------------------------------------------------------------------
// LowerTierOwner
// ---------------------------------------------------------------------------------------------

// "No lower tier claims this object" is the answer that SKIPS the ownership gate. Returning it
// because the Agent list could not be read would drop the gate exactly when the API server is sick.
func TestLowerTierOwnerListFailureIsAnErrorNotNoOwner(t *testing.T) {
	stub := &listStub{list: func(_ context.Context, _ client.ObjectList, _ ...client.ListOption) error {
		return errors.New("connection refused")
	}}
	l := &Source{Client: stub}
	caller := classify.Caller{Name: "platform", Tier: "platform", Scope: scope.Scope{ProjectID: "p", ClusterName: "c"}}

	owner, err := l.LowerTierOwner(context.Background(), caller, classify.KindRef{Group: "apps", Kind: "Deployment"}, "team-a", "api")
	if err == nil {
		t.Fatal("an unreadable Agent list must be an error, not an empty owner")
	}
	if owner != "" {
		t.Fatalf("owner = %q alongside an error", owner)
	}
}

// The predicate itself is classify.OwnerLookup.Find, which the corpus already covers. All this asserts is
// that the live Agent set reaches it -- the wiring, which is the only thing this adapter adds.
func TestLowerTierOwnerHandsTheLiveAgentSetToTheLookup(t *testing.T) {
	dev := agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: "team-a-dev", Namespace: "team-a"},
		Spec: agentv1alpha1.AgentSpec{
			Tier:  agentv1alpha1.TierDeveloperTeam,
			Scope: &agentv1alpha1.ScopeSpec{ProjectID: "p", ClusterName: "c", Namespace: "team-a"},
		},
	}
	stub := &listStub{list: func(_ context.Context, l client.ObjectList, _ ...client.ListOption) error {
		l.(*agentv1alpha1.AgentList).Items = []agentv1alpha1.Agent{dev}
		return nil
	}}
	l := &Source{Client: stub}
	caller := classify.Caller{Name: "platform", Tier: "platform", Scope: scope.Scope{ProjectID: "p", ClusterName: "c"}}

	owner, err := l.LowerTierOwner(context.Background(), caller, classify.KindRef{Group: "apps", Kind: "Deployment"}, "team-a", "api")
	if err != nil {
		t.Fatal(err)
	}
	want, err := classify.OwnerLookup{Agents: []agentv1alpha1.Agent{dev}}.Find(caller, classify.ScopeOfTarget(caller, "team-a"))
	if err != nil {
		t.Fatal(err)
	}
	if owner != want {
		t.Fatalf("owner = %q, want %q -- the adapter is not passing the live set through unchanged", owner, want)
	}
}

// ---------------------------------------------------------------------------------------------
// GetNamespaceLabels
// ---------------------------------------------------------------------------------------------

// A cluster-scoped operation names no namespace. The nil Client is the assertion: reaching the API
// server here would panic.
func TestGetNamespaceLabelsShortCircuitsOnClusterScope(t *testing.T) {
	l := &Source{}
	lbls, exists, err := l.GetNamespaceLabels(context.Background(), "")
	if err != nil || exists || lbls != nil {
		t.Fatalf("empty namespace = (%v, %v, %v), want (nil, false, nil) with no client call", lbls, exists, err)
	}
}

// ---------------------------------------------------------------------------------------------
// Caches
// ---------------------------------------------------------------------------------------------

// Both TTLs are spec constants, and both are read off the injected clock rather than the wall, so
// the boundary is testable exactly. The bound is inclusive: an entry exactly at the limit is still
// fresh, one second past it is not.
func TestCachesExpireAtTheSpecBound(t *testing.T) {
	base := time.Date(2026, 7, 28, 12, 0, 0, 0, time.UTC)
	now := base
	l := &Source{Now: func() time.Time { return now }}
	key := scopeKey(testScope())

	l.storeCount(key, 42)
	l.storeDigests(key, classify.NewDigestSet(nil))

	for _, tc := range []struct {
		name  string
		after time.Duration
		want  bool
	}{
		{"immediately", 0, true},
		{"one second before the bound", (classify.DenominatorMaxStalenessSeconds - 1) * time.Second, true},
		{"exactly at the bound", classify.DenominatorMaxStalenessSeconds * time.Second, true},
		{"one second past the bound", (classify.DenominatorMaxStalenessSeconds + 1) * time.Second, false},
	} {
		now = base.Add(tc.after)
		if n, ok := l.cachedCount(key); ok != tc.want || (ok && n != 42) {
			t.Fatalf("count cache %s: ok=%v n=%d, want ok=%v", tc.name, ok, n, tc.want)
		}
	}

	// The digest TTL is a separate constant and is asserted separately, so a future divergence
	// between the two bounds does not go unnoticed.
	now = base.Add(classify.DigestCacheTTLSeconds * time.Second)
	if _, ok := l.cachedDigests(key); !ok {
		t.Fatal("digest cache expired at exactly the bound; it should still be fresh")
	}
	now = base.Add((classify.DigestCacheTTLSeconds + 1) * time.Second)
	if _, ok := l.cachedDigests(key); ok {
		t.Fatal("digest cache did not expire one second past the bound")
	}
}

// A cached answer for one scope must never be served for another. The pairs below are the ones a
// naive concatenation would collide.
func TestScopeKeyDistinguishesEveryLevel(t *testing.T) {
	scopes := []scope.Scope{
		{ProjectID: "p"},
		{ProjectID: "p", ClusterName: "c"},
		{ProjectID: "p", ClusterName: "c", Namespace: "team-a"},
		{ProjectID: "p", ClusterName: "c", Namespace: "team-b"},
		{ProjectID: "p", ClusterName: "c2", Namespace: "team-a"},
		{ProjectID: "p2", ClusterName: "c", Namespace: "team-a"},
	}
	seen := map[string]scope.Scope{}
	for _, s := range scopes {
		k := scopeKey(s)
		if prev, dup := seen[k]; dup {
			t.Fatalf("scopes %+v and %+v collide on cache key %q", prev, s, k)
		}
		seen[k] = s
	}
}

// The cache is keyed on the scope, so a second scope must produce a second read. A cache keyed on
// nothing would answer the whole cluster's denominator for every namespace.
func TestCountCacheDoesNotBleedAcrossScopes(t *testing.T) {
	stub := &listStub{list: func(_ context.Context, l client.ObjectList, opts ...client.ListOption) error {
		var lo client.ListOptions
		for _, o := range opts {
			o.ApplyToList(&lo)
		}
		n := 1
		if lo.Namespace == "team-b" {
			n = 5
		}
		l.(*metav1.PartialObjectMetadataList).Items = make([]metav1.PartialObjectMetadata, n)
		return nil
	}}
	l := &Source{Client: stub, Discovery: discoveryStub{namespaced: []*metav1.APIResourceList{
		{GroupVersion: "v1", APIResources: []metav1.APIResource{listable("ConfigMap")}},
	}}}
	ctx := context.Background()

	a, err := l.CountWorkloadObjects(ctx, scope.Scope{ProjectID: "p", ClusterName: "c", Namespace: "team-a"})
	if err != nil {
		t.Fatal(err)
	}
	b, err := l.CountWorkloadObjects(ctx, scope.Scope{ProjectID: "p", ClusterName: "c", Namespace: "team-b"})
	if err != nil {
		t.Fatal(err)
	}
	if a != 1 || b != 5 {
		t.Fatalf("counts = (%d, %d), want (1, 5): the cache is answering one scope with another's denominator", a, b)
	}

	// The third call is team-a again and must be served from the cache: no new List.
	before := stub.calls
	if again, err := l.CountWorkloadObjects(ctx, scope.Scope{ProjectID: "p", ClusterName: "c", Namespace: "team-a"}); err != nil || again != 1 {
		t.Fatalf("cached count = (%d, %v), want (1, nil)", again, err)
	}
	if stub.calls != before {
		t.Fatalf("a cache hit still issued %d List call(s)", stub.calls-before)
	}
}

// A failed count must not be cached. Caching it would hold the broker's blast-radius rule disarmed
// for a full staleness window after a blip that lasted one request.
func TestFailedCountIsNotCached(t *testing.T) {
	fail := true
	stub := &listStub{list: func(_ context.Context, l client.ObjectList, _ ...client.ListOption) error {
		if fail {
			return apierrors.NewForbidden(schema.GroupResource{Resource: "configmaps"}, "", errors.New("nope"))
		}
		l.(*metav1.PartialObjectMetadataList).Items = []metav1.PartialObjectMetadata{{}, {}}
		return nil
	}}
	l := &Source{Client: stub, Discovery: discoveryStub{namespaced: []*metav1.APIResourceList{
		{GroupVersion: "v1", APIResources: []metav1.APIResource{listable("ConfigMap")}},
	}}}
	ctx := context.Background()

	if _, err := l.CountWorkloadObjects(ctx, testScope()); err == nil {
		t.Fatal("expected the first count to fail")
	}
	fail = false
	n, err := l.CountWorkloadObjects(ctx, testScope())
	if err != nil || n != 2 {
		t.Fatalf("second count = (%d, %v), want (2, nil): the failure was cached", n, err)
	}
}
