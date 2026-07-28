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

package controller_test

import (
	"context"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/bodystore"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/refindex"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/undo"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

// refindex.Source and bodystore.Journal against a real API server (P9-T7c-3b).
//
// The hermetic files next to each package cover the decisions those adapters make -- which kinds are
// scanned, which failures are fatal, which refusals return no reference. None of that needs a cluster
// and none of it is repeated here.
//
// What needs a cluster is the half a stub cannot lie about:
//
//   - PartialObjectMetadata is served under a distinct accept header, and controller-runtime's fake
//     client does not model it at all. Every List in refindex_test.go therefore runs against a
//     hand-written answer. Here they run against apiserver, over live discovery, and the question is
//     whether ownerReferences actually survive the metadata projection -- because if they do not, the
//     adapter reports every object unreferenced and every hermetic test still passes.
//   - The API server is the judge of an ObjectStoreRef. The adapter builds one; only a real CRD can
//     say whether it is admissible.
//
// The headline claim is V-REV-010: a `recreate` downgrade is decided from live cluster state. Its
// negative control is mandatory (09 §6) and is the second half of
// TestRecreateDowngradeFollowsTheLiveOwnerGraph.

// ---------------------------------------------------------------------------------------------
// V-REV-010 — the downgrade follows the live owner graph
// ---------------------------------------------------------------------------------------------

// One target, one envelope, two cluster states.
//
//	negative  nothing owns the ConfigMap                 -> the plan MUST stay `recreate`
//	positive  a dependent acquires an ownerReference     -> the plan MUST fall to `none`, naming it
//
// The negative half is what makes the positive half mean anything. An adapter that reported a
// reference unconditionally -- or a planner that downgraded without consulting one -- would pass the
// positive assertion perfectly and would gate every delete in the product, which is a failure no
// amount of green can show.
func TestRecreateDowngradeFollowsTheLiveOwnerGraph(t *testing.T) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		t.Skip("KUBEBUILDER_ASSETS unset; run via `make test` to exercise refindex.Source against an API server")
	}
	k8s, disco, ctx := startLiveStateEnv(t)
	idx := &refindex.Source{Client: k8s, Discovery: disco}

	const nsName = "refindex-downgrade"
	mustNamespace(t, ctx, k8s, nsName, nil)

	owner := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: "owner", Namespace: nsName}}
	if err := k8s.Create(ctx, owner); err != nil {
		t.Fatalf("create owner: %v", err)
	}
	target := agentv1alpha1.TargetRef{
		Version: "v1", Kind: "ConfigMap", Namespace: nsName, Name: owner.Name, UID: string(owner.UID),
	}

	// --- negative control: nothing points at it, so the recreate survives -----------------------
	before := planDelete(t, ctx, target, idx)
	if before.Plan.Strategy != agentv1alpha1.UndoRecreate {
		t.Fatalf("with nothing owning it, the delete of a ConfigMap must plan as recreate; got %q (caveats %v)",
			before.Plan.Strategy, before.Plan.Caveats)
	}

	// A sibling that references the target BY NAME and not by UID must not change the answer. This is
	// the boundary the package draws: a name reference survives a recreate, so counting it would
	// downgrade plans that are perfectly safe.
	byName := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "mounts-it-by-name", Namespace: nsName},
		Spec: corev1.PodSpec{
			Containers: []corev1.Container{{
				Name:  "c",
				Image: "nginx",
				EnvFrom: []corev1.EnvFromSource{{
					ConfigMapRef: &corev1.ConfigMapEnvSource{
						LocalObjectReference: corev1.LocalObjectReference{Name: owner.Name},
					},
				}},
			}},
		},
	}
	if err := k8s.Create(ctx, byName); err != nil {
		t.Fatalf("create the by-name referrer: %v", err)
	}
	if named := planDelete(t, ctx, target, idx); named.Plan.Strategy != agentv1alpha1.UndoRecreate {
		t.Fatalf("a reference that resolves by NAME survives a recreate with the same name, but the plan fell to %q: %v",
			named.Plan.Strategy, named.Plan.Caveats)
	}

	// --- positive: a real ownerReference appears, and nothing else changes ----------------------
	dependent := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{
		Name: "dependent", Namespace: nsName,
		OwnerReferences: []metav1.OwnerReference{{
			APIVersion: "v1", Kind: "ConfigMap", Name: owner.Name, UID: owner.UID,
		}},
	}}
	if err := k8s.Create(ctx, dependent); err != nil {
		t.Fatalf("create dependent: %v", err)
	}

	after := planDelete(t, ctx, target, idx)
	if after.Undoable() {
		t.Fatalf("a delete whose object owns a live dependent must not plan as a recreate; got %q", after.Plan.Strategy)
	}
	if len(after.Plan.Steps) != 0 {
		t.Fatalf("a refused plan must carry no steps: %+v", after.Plan.Steps)
	}
	caveats := strings.Join(after.Plan.Caveats, " ")
	if !strings.Contains(caveats, "dependent") {
		t.Fatalf("the caveat must name the object that would be left pointing at a dead UID; got %v", after.Plan.Caveats)
	}
	if !strings.Contains(caveats, "ownerReference") {
		t.Fatalf("the caveat must say HOW the object refers, not just that it does; got %v", after.Plan.Caveats)
	}
}

// ownerReferences must survive the metadata projection. The whole adapter reads objects as
// PartialObjectMetadata, which is a different serialization path from a normal Get -- and one the
// fake client does not implement, so nothing below envtest can tell whether the field arrives. If it
// silently did not, InboundReferences would return an empty slice for every object in the cluster and
// every hermetic test would still be green.
func TestOwnerReferencesSurviveTheMetadataProjection(t *testing.T) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		t.Skip("KUBEBUILDER_ASSETS unset; run via `make test`")
	}
	k8s, disco, ctx := startLiveStateEnv(t)
	idx := &refindex.Source{Client: k8s, Discovery: disco}

	const nsName = "refindex-projection"
	mustNamespace(t, ctx, k8s, nsName, nil)

	owner := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: "owner", Namespace: nsName}}
	if err := k8s.Create(ctx, owner); err != nil {
		t.Fatalf("create owner: %v", err)
	}
	ownerRef := metav1.OwnerReference{APIVersion: "v1", Kind: "ConfigMap", Name: owner.Name, UID: owner.UID}
	target := agentv1alpha1.TargetRef{
		Version: "v1", Kind: "ConfigMap", Namespace: nsName, Name: owner.Name, UID: string(owner.UID),
	}

	// Dependents of two different kinds, so the answer cannot come from one lucky List, plus a Secret
	// owned by something else entirely.
	for i := 0; i < 2; i++ {
		cm := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{
			Name: fmt.Sprintf("child-cm-%d", i), Namespace: nsName,
			OwnerReferences: []metav1.OwnerReference{ownerRef},
		}}
		if err := k8s.Create(ctx, cm); err != nil {
			t.Fatalf("create child-cm-%d: %v", i, err)
		}
	}
	sa := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{
		Name: "child-sa", Namespace: nsName,
		OwnerReferences: []metav1.OwnerReference{ownerRef},
	}}
	if err := k8s.Create(ctx, sa); err != nil {
		t.Fatalf("create child-sa: %v", err)
	}
	stranger := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{
		Name: "someone-elses-child", Namespace: nsName,
		OwnerReferences: []metav1.OwnerReference{{
			APIVersion: "v1", Kind: "ConfigMap", Name: "ghost", UID: "00000000-0000-0000-0000-000000000000",
		}},
	}}
	if err := k8s.Create(ctx, stranger); err != nil {
		t.Fatalf("create the stranger: %v", err)
	}

	refs, err := idx.InboundReferences(ctx, target)
	if err != nil {
		t.Fatalf("InboundReferences over a real API server: %v", err)
	}
	got := map[string]string{}
	for _, r := range refs {
		got[r.Ref.Kind+"/"+r.Ref.Name] = r.Via
	}
	for _, want := range []string{"ConfigMap/child-cm-0", "ConfigMap/child-cm-1", "ServiceAccount/child-sa"} {
		if _, ok := got[want]; !ok {
			t.Fatalf("%s is owned by the target but was not found; refs = %v", want, got)
		}
	}
	if _, ok := got["ConfigMap/someone-elses-child"]; ok {
		t.Fatal("an ownerReference to a different UID was matched; the scan is matching on something other than identity")
	}
	if len(refs) != 3 {
		t.Fatalf("want exactly the three dependents, got %d: %v", len(refs), got)
	}
	// The UID of the REFERRER has to come back too, or a human following the caveat cannot tell
	// whether the object they are looking at is the one the plan meant.
	for _, r := range refs {
		if r.Ref.UID == "" {
			t.Fatalf("%s came back with no UID: %+v", r.Ref.Name, r.Ref)
		}
	}
}

// A namespaced target is scanned in its own namespace, and a cluster-scoped one everywhere. Both
// halves are asserted against a real server because the scan surface comes from live discovery: the
// hermetic test pins the behaviour against a three-kind fixture, and this one runs it over the
// forty-odd kinds an API server actually serves, which is the shape production sees.
func TestTheScanSurfaceMatchesTheTargetsScope(t *testing.T) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		t.Skip("KUBEBUILDER_ASSETS unset; run via `make test`")
	}
	k8s, disco, ctx := startLiveStateEnv(t)
	idx := &refindex.Source{Client: k8s, Discovery: disco}

	const homeNS = "refindex-scope-home"
	const otherNS = "refindex-scope-other"
	mustNamespace(t, ctx, k8s, homeNS, nil)
	other := mustNamespace(t, ctx, k8s, otherNS, nil)

	owner := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: "owner", Namespace: homeNS}}
	if err := k8s.Create(ctx, owner); err != nil {
		t.Fatalf("create owner: %v", err)
	}

	// A cross-namespace ownerReference is not a reference the garbage collector honours -- it is an
	// object the GC deletes. The API server accepts it, so it is a thing a cluster can contain; the
	// scan must not report it as a reason to refuse a recreate.
	foreign := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{
		Name: "cross-ns-child", Namespace: otherNS,
		OwnerReferences: []metav1.OwnerReference{{
			APIVersion: "v1", Kind: "ConfigMap", Name: owner.Name, UID: owner.UID,
		}},
	}}
	if err := k8s.Create(ctx, foreign); err != nil {
		t.Fatalf("create the cross-namespace child: %v", err)
	}

	refs, err := idx.InboundReferences(ctx, agentv1alpha1.TargetRef{
		Version: "v1", Kind: "ConfigMap", Namespace: homeNS, Name: owner.Name, UID: string(owner.UID),
	})
	if err != nil {
		t.Fatalf("InboundReferences: %v", err)
	}
	if len(refs) != 0 {
		t.Fatalf("a cross-namespace ownerReference was counted; the GC does not honour it: %v", refs)
	}

	// The cluster-scoped path: a namespaced object owned by a Namespace. This is only findable if the
	// scan lists namespaced kinds across ALL namespaces when the target is cluster-scoped.
	owned := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{
		Name: "owned-by-a-namespace", Namespace: homeNS,
		OwnerReferences: []metav1.OwnerReference{{
			APIVersion: "v1", Kind: "Namespace", Name: other.Name, UID: other.UID,
		}},
	}}
	if err := k8s.Create(ctx, owned); err != nil {
		t.Fatalf("create the namespace-owned object: %v", err)
	}

	clusterRefs, err := idx.InboundReferences(ctx, agentv1alpha1.TargetRef{
		Version: "v1", Kind: "Namespace", Name: other.Name, UID: string(other.UID),
	})
	if err != nil {
		t.Fatalf("InboundReferences for a cluster-scoped target: %v", err)
	}
	if len(clusterRefs) != 1 || clusterRefs[0].Ref.Name != "owned-by-a-namespace" {
		t.Fatalf("a cluster-scoped target's namespaced dependent was not found: %v", clusterRefs)
	}
	if clusterRefs[0].Ref.Namespace != homeNS {
		t.Fatalf("the dependent must be reported with the namespace it is in: %+v", clusterRefs[0].Ref)
	}
}

// ---------------------------------------------------------------------------------------------
// The ObjectStoreRef the adapter builds must be admissible
// ---------------------------------------------------------------------------------------------

// bodystore.Journal produces a value that goes straight into an ActionRecord. Its shape is enforced
// by the CRD -- a required store, a required key, and a lower-hex 64-character digest -- and only the
// API server can say whether the adapter satisfies it. A digest that came back from a sink in
// uppercase, or a key that came back empty, would be rejected here at write time, AFTER the sink
// write and after the mutation, and reported as a validation error rather than as the sink fault it
// is.
func TestTheAdaptersObjectStoreRefIsAdmissible(t *testing.T) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		t.Skip("KUBEBUILDER_ASSETS unset; run via `make test`")
	}
	k8s, _, ctx := startLiveStateEnv(t)

	body := []byte(`{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"big"}}`)
	store := &bodystore.Journal{Sink: memSink{name: "gs://kage-journal"}, AgentIdentity: "cluster-admin/proj-x/cluster-a"}
	ref, err := store.Put(ctx, testULID, 0, body)
	if err != nil {
		t.Fatalf("Put: %v", err)
	}

	rec := newActionRecord("ar-refindex-objectref", time.Now())
	rec.Spec.PreState = []agentv1alpha1.PreStateSnapshot{{
		TargetIndex: 0,
		CapturedAt:  metav1.Now(),
		ObjectRef:   ref,
		SHA256:      ref.SHA256,
	}}
	if err := k8s.Create(ctx, rec); err != nil {
		t.Fatalf("the API server rejected a reference this adapter produced: %v", err)
	}

	// The control: the same record with a digest the adapter could never emit must be REJECTED. Without
	// it, a CRD whose pattern had been dropped would accept both and the assertion above would be
	// about nothing.
	bad := newActionRecord("ar-refindex-objectref-bad", time.Now())
	bad.Spec.PreState = []agentv1alpha1.PreStateSnapshot{{
		TargetIndex: 0,
		CapturedAt:  metav1.Now(),
		ObjectRef: &agentv1alpha1.ObjectStoreRef{
			Store: ref.Store, Key: ref.Key, SHA256: strings.ToUpper(ref.SHA256),
		},
		SHA256: ref.SHA256,
	}}
	if err := k8s.Create(ctx, bad); err == nil {
		t.Fatal("an uppercase digest was accepted; the CRD's lower-hex pattern is not being enforced")
	}
}

// ---------------------------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------------------------

// planDelete runs the real planner over a one-operation delete envelope. Going through undo.Generate
// rather than calling the index directly is the point: the property under test is that the DOWNGRADE
// follows the cluster, and the downgrade lives in the planner.
func planDelete(t *testing.T, ctx context.Context, target agentv1alpha1.TargetRef, idx undo.ReferenceIndex) *undo.Result {
	t.Helper()
	res, err := undo.Generate(ctx, undo.Request{
		Operations: []undo.Operation{{
			Verb:    "delete",
			Target:  target,
			Existed: true,
			PreState: &unstructured.Unstructured{Object: map[string]any{
				"apiVersion": "v1",
				"kind":       target.Kind,
				"metadata": map[string]any{
					"name": target.Name, "namespace": target.Namespace, "uid": target.UID,
				},
			}},
		}},
		GeneratedAt: metav1.Now(),
	}, idx)
	if err != nil {
		t.Fatalf("undo.Generate: %v", err)
	}
	return res
}

// memSink is an honest journal.BlobSink over a map. There is no production sink yet -- it needs a
// provisioned bucket and Workload Identity, which is a provisioning unit and not this one -- so the
// adapter is exercised against a sink that behaves, and the assertion above is about the API server's
// verdict on what comes out, not about the sink.
type memSink struct {
	name string
}

func (m memSink) Name() string { return m.name }

func (m memSink) Put(_ context.Context, _ string, body []byte) (string, error) {
	return journal.Digest(body), nil
}

func (m memSink) Get(context.Context, string) ([]byte, error) {
	return nil, fmt.Errorf("memSink holds nothing")
}
