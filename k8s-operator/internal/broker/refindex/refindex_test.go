package refindex

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	k8stypes "k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/undo"
)

// The hermetic half of Source's coverage: which kinds are scanned, which failures are fatal, and how
// a match is rendered. Every one of those is a decision this package makes whose wrong answer is
// silent.
//
// The other half -- that a real API server serves PartialObjectMetadata for an arbitrary kind, and
// that ownerReferences survive the metadata projection -- is NOT provable here and is not attempted
// here. controller-runtime's fake tracker does not model PartialObjectMetadata, so a green against it
// would be a green about code that never ran. Those properties live in the envtest suite
// (internal/controller/reference_index_envtest_test.go) and in the L2 probe
// (test/l2/reference_index_l2_test.go), which is what V-REV-010 is recorded against.

// listStub intercepts List and nothing else. The embedded nil client.Client is deliberate: a call
// this stub does not implement panics rather than quietly succeeding.
type listStub struct {
	client.Client
	list  func(ctx context.Context, l client.ObjectList, opts ...client.ListOption) error
	kinds []string
	opts  [][]client.ListOption
}

func (s *listStub) List(ctx context.Context, l client.ObjectList, opts ...client.ListOption) error {
	s.kinds = append(s.kinds, l.GetObjectKind().GroupVersionKind().Kind)
	s.opts = append(s.opts, opts)
	return s.list(ctx, l, opts...)
}

// discoveryStub is the four-method ServerResourcesInterface. `namespaced` and `all` are kept apart so
// a test can prove the cluster-scoped path reads a DIFFERENT surface rather than the same one twice.
type discoveryStub struct {
	namespaced []*metav1.APIResourceList
	all        []*metav1.APIResourceList
	err        error
}

func (d discoveryStub) ServerResourcesForGroupVersion(string) (*metav1.APIResourceList, error) {
	return nil, errors.New("refindex must not ask for a single groupVersion")
}

func (d discoveryStub) ServerGroupsAndResources() ([]*metav1.APIGroup, []*metav1.APIResourceList, error) {
	return nil, d.all, d.err
}

func (d discoveryStub) ServerPreferredResources() ([]*metav1.APIResourceList, error) {
	return d.all, d.err
}

func (d discoveryStub) ServerPreferredNamespacedResources() ([]*metav1.APIResourceList, error) {
	return d.namespaced, d.err
}

func listable(kind string, namespaced bool) metav1.APIResource {
	return metav1.APIResource{
		Name:       strings.ToLower(kind) + "s",
		Kind:       kind,
		Namespaced: namespaced,
		Verbs:      metav1.Verbs{"get", "list", "watch"},
	}
}

func item(ns, name, uid string, owners ...metav1.OwnerReference) metav1.PartialObjectMetadata {
	return metav1.PartialObjectMetadata{
		ObjectMeta: metav1.ObjectMeta{
			Namespace:       ns,
			Name:            name,
			UID:             k8stypes.UID(uid),
			OwnerReferences: owners,
		},
	}
}

func owns(uid string, controller bool) metav1.OwnerReference {
	return metav1.OwnerReference{
		APIVersion: "v1",
		Kind:       "ConfigMap",
		Name:       "cfg",
		UID:        k8stypes.UID(uid),
		Controller: &controller,
	}
}

// emptyLister answers every List with nothing.
func emptyLister() func(context.Context, client.ObjectList, ...client.ListOption) error {
	return func(context.Context, client.ObjectList, ...client.ListOption) error { return nil }
}

// serve fills the list for one kind and leaves every other kind empty. It also asserts the adapter
// asked for metadata rather than whole bodies: a scan that pulled full objects across every kind in
// a namespace is a different, much more expensive thing than the one this package documents.
func serve(kind string, items ...metav1.PartialObjectMetadata) func(context.Context, client.ObjectList, ...client.ListOption) error {
	return func(_ context.Context, l client.ObjectList, _ ...client.ListOption) error {
		pl, ok := l.(*metav1.PartialObjectMetadataList)
		if !ok {
			return fmt.Errorf("the scan listed %T, not PartialObjectMetadataList; it would pull whole object bodies", l)
		}
		if pl.GetObjectKind().GroupVersionKind().Kind == kind+"List" {
			pl.Items = items
		}
		return nil
	}
}

func namespacedTarget(uid string) agentv1alpha1.TargetRef {
	return agentv1alpha1.TargetRef{Version: "v1", Kind: "ConfigMap", Namespace: "team-a", Name: "cfg", UID: uid}
}

func clusterTarget(uid string) agentv1alpha1.TargetRef {
	return agentv1alpha1.TargetRef{Version: "v1", Kind: "Namespace", Name: "team-a", UID: uid}
}

// coreAndApps is the shared discovery fixture: three namespaced kinds across two groups, and a
// cluster-wide surface that deliberately contains a kind the namespaced surface does not.
func coreAndApps() discoveryStub {
	return discoveryStub{
		namespaced: []*metav1.APIResourceList{
			{GroupVersion: "v1", APIResources: []metav1.APIResource{listable("Pod", true), listable("ConfigMap", true)}},
			{GroupVersion: "apps/v1", APIResources: []metav1.APIResource{listable("Deployment", true)}},
		},
		all: []*metav1.APIResourceList{
			{GroupVersion: "v1", APIResources: []metav1.APIResource{listable("Pod", true), listable("Namespace", false)}},
			{GroupVersion: "rbac.authorization.k8s.io/v1", APIResources: []metav1.APIResource{listable("ClusterRole", false)}},
		},
	}
}

// ---------------------------------------------------------------------------------------------
// The match itself
// ---------------------------------------------------------------------------------------------

// A dependent whose ownerReference carries the target's UID is found; one carrying a different UID is
// not; one carrying none is not. All three in a single fixture, because a scan that matched
// everything and a scan that matched nothing both pass a one-directional test.
func TestOwnerReferenceMatchesOnUIDAndOnlyOnUID(t *testing.T) {
	c := &listStub{list: serve("Pod",
		item("team-a", "mine", "pod-1", owns("target-uid", true)),
		item("team-a", "someone-elses", "pod-2", owns("other-uid", true)),
		item("team-a", "unowned", "pod-3"),
	)}
	s := &Source{Client: c, Discovery: coreAndApps()}

	got, err := s.InboundReferences(context.Background(), namespacedTarget("target-uid"))
	if err != nil {
		t.Fatalf("InboundReferences: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("want exactly the one dependent owned by the target, got %d: %v", len(got), got)
	}
	if got[0].Ref.Kind != "Pod" || got[0].Ref.Name != "mine" {
		t.Fatalf("wrong dependent: %+v", got[0].Ref)
	}
	if got[0].Ref.Namespace != "team-a" || got[0].Ref.UID != "pod-1" {
		t.Fatalf("the referring object must be identified well enough to go and look at: %+v", got[0].Ref)
	}
	if got[0].Via != "ownerReference (controller)" {
		t.Fatalf("Via should name the controller edge, the one the garbage collector acts on; got %q", got[0].Via)
	}
}

// A non-controller ownerReference still dangles -- the GC does not delete the dependent, but its
// reference points at a UID that no longer exists. It must be reported, and labelled as the weaker
// edge it is: the caveat a human reads is the difference between "this will be deleted" and "this
// will point at nothing".
func TestANonControllerOwnerReferenceIsReportedAndLabelledDifferently(t *testing.T) {
	c := &listStub{list: serve("Pod", item("team-a", "weak", "pod-1", owns("target-uid", false)))}
	s := &Source{Client: c, Discovery: coreAndApps()}

	got, err := s.InboundReferences(context.Background(), namespacedTarget("target-uid"))
	if err != nil {
		t.Fatalf("InboundReferences: %v", err)
	}
	if len(got) != 1 || got[0].Via != "ownerReference" {
		t.Fatalf("want one plain ownerReference, got %v", got)
	}
}

// An object carrying two ownerReferences to the same target is reported once per edge. Deduplicating
// by object would hide the shape of the graph in the one case where it is strange.
func TestEveryMatchingEdgeIsReported(t *testing.T) {
	c := &listStub{list: serve("Pod",
		item("team-a", "twice", "pod-1", owns("target-uid", true), owns("target-uid", false)),
	)}
	s := &Source{Client: c, Discovery: coreAndApps()}

	got, err := s.InboundReferences(context.Background(), namespacedTarget("target-uid"))
	if err != nil {
		t.Fatalf("InboundReferences: %v", err)
	}
	if len(got) != 2 {
		t.Fatalf("want one entry per matching edge, got %d: %v", len(got), got)
	}
}

// The empty answer is a real answer and must not be an error. checkRecreatable reads an empty slice
// as "the recreate survives", so an adapter that erred on a clean scan would downgrade every delete
// in the product -- and from a suite of fail-closed tests alone it would look exactly like one that
// worked.
func TestACleanScanReturnsNoReferencesAndNoError(t *testing.T) {
	c := &listStub{list: emptyLister()}
	s := &Source{Client: c, Discovery: coreAndApps()}

	got, err := s.InboundReferences(context.Background(), namespacedTarget("target-uid"))
	if err != nil {
		t.Fatalf("a clean scan must not be an error: %v", err)
	}
	if len(got) != 0 {
		t.Fatalf("want no references, got %v", got)
	}
}

// ---------------------------------------------------------------------------------------------
// What is scanned
// ---------------------------------------------------------------------------------------------

// A namespaced target is scanned in its own namespace and nowhere else. The GC requires a namespaced
// owner and its dependent to share a namespace, so a cluster-wide list here would be slower AND would
// report edges the GC does not honour.
func TestANamespacedTargetIsScannedInItsOwnNamespace(t *testing.T) {
	c := &listStub{list: emptyLister()}
	s := &Source{Client: c, Discovery: coreAndApps()}

	if _, err := s.InboundReferences(context.Background(), namespacedTarget("target-uid")); err != nil {
		t.Fatalf("InboundReferences: %v", err)
	}
	if len(c.opts) == 0 {
		t.Fatal("nothing was listed at all")
	}
	for i, o := range c.opts {
		if len(o) != 1 {
			t.Fatalf("list %d carried %d options; the scan must be namespace-scoped", i, len(o))
		}
		ns, ok := o[0].(client.InNamespace)
		if !ok {
			t.Fatalf("list %d was not scoped to a namespace: %T", i, o[0])
		}
		if string(ns) != "team-a" {
			t.Fatalf("list %d was scoped to %q, not the target's namespace", i, string(ns))
		}
	}
}

// A cluster-scoped target reads the FULL discovery surface, not the namespaced one, and lists without
// a namespace restriction: a namespaced object may legitimately be owned by a cluster-scoped one, so
// scanning only cluster-scoped kinds would miss every such dependent.
func TestAClusterScopedTargetScansEveryKindEverywhere(t *testing.T) {
	c := &listStub{list: emptyLister()}
	s := &Source{Client: c, Discovery: coreAndApps()}

	if _, err := s.InboundReferences(context.Background(), clusterTarget("ns-uid")); err != nil {
		t.Fatalf("InboundReferences: %v", err)
	}
	for i, o := range c.opts {
		if len(o) != 0 {
			t.Fatalf("list %d was namespace-scoped; a cluster-scoped target's dependents may be in any namespace", i)
		}
	}
	if !contains(c.kinds, "ClusterRoleList") {
		t.Fatalf("the cluster-scoped path did not read the full surface; kinds listed: %v", c.kinds)
	}
	if contains(c.kinds, "DeploymentList") {
		t.Fatalf("the cluster-scoped path read the NAMESPACED surface; kinds listed: %v", c.kinds)
	}
}

// Subresources are not objects and hold no ownerReferences; a kind with no `list` verb cannot be
// scanned at all. Both are dropped BEFORE the scan rather than failing inside it -- a list on
// `deployments/scale` is a 404, and a 404 in this adapter is fatal.
func TestUnscannableEntriesAreDroppedBeforeTheScan(t *testing.T) {
	d := discoveryStub{namespaced: []*metav1.APIResourceList{{
		GroupVersion: "apps/v1",
		APIResources: []metav1.APIResource{
			listable("Deployment", true),
			{Name: "deployments/scale", Kind: "Scale", Namespaced: true, Verbs: metav1.Verbs{"get", "update"}},
			{Name: "bindings", Kind: "Binding", Namespaced: true, Verbs: metav1.Verbs{"create"}},
		},
	}}}
	c := &listStub{list: emptyLister()}
	s := &Source{Client: c, Discovery: d}

	if _, err := s.InboundReferences(context.Background(), namespacedTarget("target-uid")); err != nil {
		t.Fatalf("InboundReferences: %v", err)
	}
	if want := []string{"DeploymentList"}; !equal(c.kinds, want) {
		t.Fatalf("scanned %v, want %v", c.kinds, want)
	}
}

// The same kind served at two versions is one set of objects with one set of ownerReferences.
// ServerGroupsAndResources returns every served version, so without the dedup every dependent of a
// multi-version CRD would be reported twice and the caveat a human reads would be inflated.
func TestAKindServedAtTwoVersionsIsScannedOnce(t *testing.T) {
	d := discoveryStub{all: []*metav1.APIResourceList{
		{GroupVersion: "widgets.example.com/v1", APIResources: []metav1.APIResource{listable("Widget", false)}},
		{GroupVersion: "widgets.example.com/v1beta1", APIResources: []metav1.APIResource{listable("Widget", false)}},
	}}
	c := &listStub{list: emptyLister()}
	s := &Source{Client: c, Discovery: d}

	if _, err := s.InboundReferences(context.Background(), clusterTarget("ns-uid")); err != nil {
		t.Fatalf("InboundReferences: %v", err)
	}
	if len(c.kinds) != 1 {
		t.Fatalf("scanned %v; a kind served at two versions must be listed once", c.kinds)
	}
}

// ---------------------------------------------------------------------------------------------
// The failure direction -- the opposite of the blast-radius denominator's
// ---------------------------------------------------------------------------------------------

// The headline property, and the one V-REV-010's negative control pairs with.
//
// livestate.CountWorkloadObjects SKIPS a kind it cannot list, because a smaller denominator arms the
// abort rule harder. This adapter must not: a skipped kind produces a SHORTER reference list, a
// shorter list makes the recreate look safer, and the resulting failure is a garbage collector
// deleting children minutes later.
//
// Asserted with a Forbidden, which is the realistic case -- a broker whose Role does not cover one
// kind in the namespace -- rather than with a synthetic error nobody will ever see.
func TestAnUnlistableKindIsFatalAndNotSkipped(t *testing.T) {
	c := &listStub{list: func(_ context.Context, l client.ObjectList, _ ...client.ListOption) error {
		if l.GetObjectKind().GroupVersionKind().Kind == "PodList" {
			return apierrors.NewForbidden(schema.GroupResource{Resource: "pods"}, "", errors.New("no"))
		}
		return nil
	}}
	s := &Source{Client: c, Discovery: coreAndApps()}

	got, err := s.InboundReferences(context.Background(), namespacedTarget("target-uid"))
	if err == nil {
		t.Fatalf("a kind that could not be listed must fail the scan; got %d references and no error", len(got))
	}
	if got != nil {
		t.Fatalf("a failed scan must carry no partial list; got %v", got)
	}
	// The message has to be actionable: Forbidden is a grant somebody has to widen, and it must not
	// read like a transient cluster fault.
	if !strings.Contains(err.Error(), "may not list Pod") {
		t.Fatalf("the refusal should name the kind and the missing grant; got %q", err)
	}
}

// The negative control for the test above (09 §6: mandatory for every `¬` check). Same adapter, same
// fixture, the Forbidden removed: the scan must SUCCEED, and must actually have listed the kind the
// other test forbids. Without this, an adapter that erred unconditionally would pass the fail-closed
// test perfectly while gating every delete in the product -- a failure a green suite cannot see.
func TestTheSameScanSucceedsOnceTheGrantIsThere(t *testing.T) {
	c := &listStub{list: emptyLister()}
	s := &Source{Client: c, Discovery: coreAndApps()}

	got, err := s.InboundReferences(context.Background(), namespacedTarget("target-uid"))
	if err != nil {
		t.Fatalf("with every kind listable the scan must succeed: %v", err)
	}
	if len(got) != 0 {
		t.Fatalf("want a clean scan, got %v", got)
	}
	if !contains(c.kinds, "PodList") {
		t.Fatalf("the control did not exercise the kind the fail-closed test forbids; scanned %v", c.kinds)
	}
}

// A non-Forbidden List failure is fatal too, and says something different. Same direction, different
// remedy: one is a grant, the other is a cluster to go and look at.
func TestATransientListFailureIsAlsoFatalAndReadsDifferently(t *testing.T) {
	c := &listStub{list: func(_ context.Context, l client.ObjectList, _ ...client.ListOption) error {
		if l.GetObjectKind().GroupVersionKind().Kind == "DeploymentList" {
			return errors.New("etcdserver: request timed out")
		}
		return nil
	}}
	s := &Source{Client: c, Discovery: coreAndApps()}

	_, err := s.InboundReferences(context.Background(), namespacedTarget("target-uid"))
	if err == nil {
		t.Fatal("a kind that could not be listed must fail the scan")
	}
	if strings.Contains(err.Error(), "may not list") {
		t.Fatalf("a timeout must not be reported as a missing grant: %q", err)
	}
	if !strings.Contains(err.Error(), "listing Deployment") {
		t.Fatalf("the error should name the kind that failed; got %q", err)
	}
}

// A partial discovery result is fatal here and is NOT fatal in the denominator, for the same reason
// as above but one layer earlier: a kind discovery failed to report is a kind that is never listed
// and never found. The scan must not even start.
func TestAPartialDiscoveryResultIsFatal(t *testing.T) {
	d := coreAndApps()
	d.err = errors.New("unable to retrieve the complete list of server APIs: metrics.k8s.io/v1beta1")
	c := &listStub{list: emptyLister()}
	s := &Source{Client: c, Discovery: d}

	if _, err := s.InboundReferences(context.Background(), namespacedTarget("target-uid")); err == nil {
		t.Fatal("an incomplete kind set cannot support the claim that nothing points at the target")
	}
	if len(c.kinds) != 0 {
		t.Fatalf("the scan ran anyway, over %v", c.kinds)
	}
}

// Every remaining way of being unable to look, in one table. Each returns an error rather than an
// empty slice, because the interface's own words are that "nothing points at it" and "I could not
// look" must never be conflated -- and an empty slice IS the first of those two answers.
func TestEveryWayOfNotBeingAbleToLookIsAnError(t *testing.T) {
	full := coreAndApps()
	cases := []struct {
		name   string
		src    *Source
		target agentv1alpha1.TargetRef
		want   string
	}{
		{
			name:   "the target carries no UID",
			src:    &Source{Client: &listStub{list: emptyLister()}, Discovery: full},
			target: namespacedTarget(""),
			want:   "carries no UID",
		},
		{
			name:   "no client is wired",
			src:    &Source{Discovery: full},
			target: namespacedTarget("target-uid"),
			want:   "no API client",
		},
		{
			name:   "no discovery client is wired",
			src:    &Source{Client: &listStub{list: emptyLister()}},
			target: namespacedTarget("target-uid"),
			want:   "no discovery client",
		},
		{
			name:   "the server reports no resources at all",
			src:    &Source{Client: &listStub{list: emptyLister()}, Discovery: discoveryStub{}},
			target: namespacedTarget("target-uid"),
			want:   "no resources at all",
		},
		{
			name: "nothing discovered is listable",
			src: &Source{Client: &listStub{list: emptyLister()}, Discovery: discoveryStub{namespaced: []*metav1.APIResourceList{{
				GroupVersion: "v1",
				APIResources: []metav1.APIResource{{Name: "bindings", Kind: "Binding", Namespaced: true, Verbs: metav1.Verbs{"create"}}},
			}}}},
			target: namespacedTarget("target-uid"),
			want:   "no listable kind",
		},
		{
			name: "discovery reports an unparseable groupVersion",
			src: &Source{Client: &listStub{list: emptyLister()}, Discovery: discoveryStub{namespaced: []*metav1.APIResourceList{{
				GroupVersion: "too/many/slashes",
				APIResources: []metav1.APIResource{listable("Ghost", true)},
			}}}},
			target: namespacedTarget("target-uid"),
			want:   "unparseable groupVersion",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			refs, err := tc.src.InboundReferences(context.Background(), tc.target)
			if err == nil {
				t.Fatalf("want an error, got %d references", len(refs))
			}
			if refs != nil {
				t.Fatalf("an error must carry no partial answer; got %v", refs)
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error %q does not explain itself with %q", err, tc.want)
			}
		})
	}
}

// ---------------------------------------------------------------------------------------------
// Determinism
// ---------------------------------------------------------------------------------------------

// undo.joinRefs prints the first three matches and counts the rest, so the ORDER of this slice is
// part of the ActionRecord a human reads. Two scans of an unchanged cluster must produce the same
// three, or the recorded plan changes without the cluster changing.
func TestTheResultIsSortedSoTheRecordedCaveatIsStable(t *testing.T) {
	c := &listStub{list: serve("Pod",
		item("team-a", "zulu", "p1", owns("target-uid", true)),
		item("team-a", "alpha", "p2", owns("target-uid", true)),
		item("team-a", "mike", "p3", owns("target-uid", true)),
	)}
	s := &Source{Client: c, Discovery: coreAndApps()}

	got, err := s.InboundReferences(context.Background(), namespacedTarget("target-uid"))
	if err != nil {
		t.Fatalf("InboundReferences: %v", err)
	}
	names := make([]string, 0, len(got))
	for _, r := range got {
		names = append(names, r.Ref.Name)
	}
	if want := []string{"alpha", "mike", "zulu"}; !equal(names, want) {
		t.Fatalf("got %v, want %v", names, want)
	}
}

// The kinds are scanned in a stable order too, so a refusal names the same kind on every retry. An
// error that flaps between kinds reads as an unhealthy cluster rather than as a missing grant.
func TestKindsAreScannedInAStableOrder(t *testing.T) {
	var first []string
	for i := 0; i < 3; i++ {
		c := &listStub{list: emptyLister()}
		s := &Source{Client: c, Discovery: coreAndApps()}
		if _, err := s.InboundReferences(context.Background(), namespacedTarget("target-uid")); err != nil {
			t.Fatalf("InboundReferences: %v", err)
		}
		if i == 0 {
			first = c.kinds
			continue
		}
		if !equal(c.kinds, first) {
			t.Fatalf("run %d scanned %v, run 0 scanned %v", i, c.kinds, first)
		}
	}
	if want := []string{"ConfigMapList", "PodList", "DeploymentList"}; !equal(first, want) {
		t.Fatalf("scan order %v, want %v (group, then kind)", first, want)
	}
}

// ---------------------------------------------------------------------------------------------
// The seam: this adapter actually drives the downgrade
// ---------------------------------------------------------------------------------------------

// A compile-time `var _ undo.ReferenceIndex` proves the shape and nothing else. These three prove the
// wiring: the same adapter over the same fixture, differing only in what the cluster answers,
// produces a refused plan in one case and a `recreate` in the other. Together they are the L1 half of
// V-REV-010; the L2 probe runs the same pair against a real cluster.
func TestAnOwnedObjectIsDowngradedOutOfRecreate(t *testing.T) {
	idx := &Source{
		Client:    &listStub{list: serve("Pod", item("team-a", "child", "p1", owns("target-uid", true)))},
		Discovery: coreAndApps(),
	}

	res := generate(t, namespacedTarget("target-uid"), idx)
	if res.Undoable() {
		t.Fatalf("a delete of an owned object must not be planned as a recreate: %+v", res.Plan)
	}
	if res.Plan.Strategy != agentv1alpha1.UndoNone {
		t.Fatalf("want strategy none, got %q", res.Plan.Strategy)
	}
	if len(res.Plan.Steps) != 0 {
		t.Fatalf("a refused plan must carry no steps: %+v", res.Plan.Steps)
	}
	if joined := strings.Join(res.Plan.Caveats, " "); !strings.Contains(joined, "child") {
		t.Fatalf("the caveat must name the object that would be left dangling; got %v", res.Plan.Caveats)
	}
}

func TestAnUnreferencedObjectKeepsItsRecreate(t *testing.T) {
	idx := &Source{Client: &listStub{list: emptyLister()}, Discovery: coreAndApps()}

	res := generate(t, namespacedTarget("target-uid"), idx)
	if res.Plan.Strategy != agentv1alpha1.UndoRecreate {
		t.Fatalf("want strategy recreate, got %q (caveats: %v)", res.Plan.Strategy, res.Plan.Caveats)
	}
	if !res.Undoable() {
		t.Fatal("an unreferenced object's delete is undoable")
	}
}

// A scan that could not run downgrades rather than erroring out of Generate. The behaviour lives in
// undo.checkRecreatable, but it is asserted from here because it is the whole reason this adapter is
// allowed to be fatal: fatal costs a gate, not a crash. The caveat must also say WHICH of the two it
// is -- "could not determine" and "nothing references this" lead a human to opposite conclusions.
func TestAScanThatCouldNotRunGatesRatherThanFailing(t *testing.T) {
	idx := &Source{
		Client: &listStub{list: func(context.Context, client.ObjectList, ...client.ListOption) error {
			return apierrors.NewForbidden(schema.GroupResource{Resource: "pods"}, "", errors.New("no"))
		}},
		Discovery: coreAndApps(),
	}

	res := generate(t, namespacedTarget("target-uid"), idx)
	if res.Undoable() {
		t.Fatal("a recreate whose reference graph could not be read must not be planned")
	}
	if joined := strings.Join(res.Plan.Caveats, " "); !strings.Contains(joined, "could not determine whether anything references") {
		t.Fatalf("the caveat must say the scan failed, not that nothing was found; got %v", res.Plan.Caveats)
	}
}

func generate(t *testing.T, target agentv1alpha1.TargetRef, idx undo.ReferenceIndex) *undo.Result {
	t.Helper()
	res, err := undo.Generate(context.Background(), undo.Request{
		Operations: []undo.Operation{{
			Verb:     "delete",
			Target:   target,
			Existed:  true,
			PreState: preState(target),
		}},
		GeneratedAt: metav1.Now(),
	}, idx)
	if err != nil {
		t.Fatalf("undo.Generate: %v", err)
	}
	return res
}

func preState(target agentv1alpha1.TargetRef) *unstructured.Unstructured {
	return &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "v1",
		"kind":       target.Kind,
		"metadata": map[string]any{
			"name":      target.Name,
			"namespace": target.Namespace,
			"uid":       target.UID,
		},
		"data": map[string]any{"key": "value"},
	}}
}

func contains(xs []string, want string) bool {
	for _, x := range xs {
		if x == want {
			return true
		}
	}
	return false
}

func equal(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
