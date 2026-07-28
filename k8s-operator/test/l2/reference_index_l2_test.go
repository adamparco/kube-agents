//go:build l2

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

// V-REV-010 at L2. Driven by dev/verify/reference-index-l2.sh, which owns the destructive-test
// guard; connect() in live_state_l2_test.go duplicates it, because a probe that can only be aimed
// safely by its wrapper is one `go test` away from being aimed at the live install.
package l2

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/refindex"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/undo"
)

// refNamespace is this file's own probe namespace: a distinct GenerateName prefix from the
// V-GAT-022 probe's, so a leaked namespace can be attributed to the run that leaked it.
func refNamespace(t *testing.T, ctx context.Context, k8s client.Client) *corev1.Namespace {
	t.Helper()
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{GenerateName: "kage-rev010-"}}
	if err := k8s.Create(ctx, ns); err != nil {
		t.Fatalf("create probe namespace: %v", err)
	}
	t.Cleanup(func() {
		_ = k8s.Delete(context.Background(), ns, client.PropagationPolicy(metav1.DeletePropagationBackground))
	})
	return ns
}

func refTarget(kind, ns, name string, uid types.UID) agentv1alpha1.TargetRef {
	return agentv1alpha1.TargetRef{Version: "v1", Kind: kind, Namespace: ns, Name: name, UID: string(uid)}
}

// planDeleteL2 runs the production planner over a one-operation delete. The property under test is
// the DOWNGRADE, and the downgrade lives in undo.checkRecreatable -- calling the index directly
// would assert that the adapter can count, which is what the hermetic tests are for.
func planDeleteL2(t *testing.T, ctx context.Context, target agentv1alpha1.TargetRef, idx undo.ReferenceIndex) *undo.Result {
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
					"name": target.Name, "namespace": target.Namespace,
				},
				"data": map[string]any{"key": "value"},
			}},
		}},
		GeneratedAt: metav1.Now(),
	}, idx)
	if err != nil {
		t.Fatalf("undo.Generate: %v", err)
	}
	return res
}

// V-REV-010, at L2 (09 §6.3: L2, weight 9, negative control mandatory).
//
// # Why this needs a real cluster and the envtest suite is not enough
//
// internal/controller/reference_index_envtest_test.go asserts the same downgrade against an envtest
// API server, and that is a genuine L1 result. It is not this one. Three things differ here, and
// each is a way the property holds at L1 and fails in production:
//
//   - The DISCOVERY SURFACE. The adapter's contract is that any kind it cannot list fails the whole
//     scan. envtest serves a bare API server plus this project's CRDs, so that clause is nearly free
//     there. GKE serves its own managed CRDs, the metrics aggregation layer, and whatever else is
//     installed -- which is where a kind that cannot be listed actually comes from. If this cluster
//     has one, the scan is supposed to REFUSE, and finding that out here is the point.
//   - Real RBAC. envtest's client is unconditionally cluster-admin, so the Forbidden branch is
//     unreachable there and is only ever exercised against a stub that was told to fail.
//   - A LIVE GARBAGE COLLECTOR. envtest runs no kube-controller-manager, so an ownerReference there
//     is an annotation with no consequences. Step 4 below is the only place in this repo where the
//     harm the downgrade prevents is demonstrated rather than described.
//
// The negative control (09 §6, mandatory for every `¬` check) is step 2.
func TestREV010RecreateDowngradeFollowsTheLiveOwnerGraph(t *testing.T) {
	k8s, disco, kubeContext := connect(t)
	ctx := context.Background()
	idx := &refindex.Source{Client: k8s, Discovery: disco}
	ns := refNamespace(t, ctx, k8s)
	t.Logf("context %s, probe namespace %s", kubeContext, ns.Name)

	owner := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: "owner", Namespace: ns.Name},
		Data:       map[string]string{"key": "value"},
	}
	if err := k8s.Create(ctx, owner); err != nil {
		t.Fatalf("create owner: %v", err)
	}
	target := refTarget("ConfigMap", ns.Name, owner.Name, owner.UID)

	// --- step 1: nothing points at it -----------------------------------------------------------
	t.Log("step 1: an unreferenced object keeps its recreate")
	before := planDeleteL2(t, ctx, target, idx)
	if before.Plan.Strategy != agentv1alpha1.UndoRecreate {
		t.Fatalf("with nothing owning it the plan must be recreate, got %q (caveats %v)",
			before.Plan.Strategy, before.Plan.Caveats)
	}

	// --- step 2: NEGATIVE CONTROL ---------------------------------------------------------------
	// A reference that resolves by NAME survives a recreate with the same name, so it must not
	// downgrade anything. This is the half that matters: an adapter that reported every
	// reference-shaped field it could find would pass step 3 perfectly and would gate every delete
	// of any object anything mentions -- which on a real cluster is every object.
	t.Log("step 2: negative control — a by-name reference does not downgrade the plan")
	byName := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "mounts-it-by-name", Namespace: ns.Name},
		Spec: corev1.PodSpec{
			RestartPolicy: corev1.RestartPolicyNever,
			Containers: []corev1.Container{{
				Name:  "c",
				Image: "gcr.io/google-containers/pause:3.9",
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
	named := planDeleteL2(t, ctx, target, idx)
	if named.Plan.Strategy != agentv1alpha1.UndoRecreate {
		t.Fatalf("a by-name reference downgraded the plan to %q; a recreate under the same name is "+
			"exactly what such a reference resolves to. caveats: %v", named.Plan.Strategy, named.Plan.Caveats)
	}

	// --- step 3: a real ownerReference appears --------------------------------------------------
	t.Log("step 3: a UID-valued ownerReference downgrades the plan to none")
	dependent := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{
		Name: "dependent", Namespace: ns.Name,
		OwnerReferences: []metav1.OwnerReference{{
			APIVersion: "v1", Kind: "ConfigMap", Name: owner.Name, UID: owner.UID,
		}},
	}}
	if err := k8s.Create(ctx, dependent); err != nil {
		t.Fatalf("create dependent: %v", err)
	}

	after := planDeleteL2(t, ctx, target, idx)
	if after.Undoable() {
		t.Fatalf("a delete whose object owns a live dependent must not plan as a recreate; got %q",
			after.Plan.Strategy)
	}
	if len(after.Plan.Steps) != 0 {
		t.Fatalf("a refused plan must carry no steps: %+v", after.Plan.Steps)
	}
	caveats := strings.Join(after.Plan.Caveats, " ")
	if !strings.Contains(caveats, "dependent") || !strings.Contains(caveats, "ownerReference") {
		t.Fatalf("the caveat must name the dependent and how it refers; got %v", after.Plan.Caveats)
	}
	t.Logf("step 3 caveat, as a human would read it: %s", caveats)
}

// The harm, demonstrated. Everything above asserts that a plan is refused; this asserts that the
// refusal was right, by doing on a real cluster what the plan would have done and watching it go
// wrong. envtest cannot host this at all -- it runs no garbage collector, so an ownerReference there
// has no consequences and the premise of 06 §4.3.1 is untestable below L2.
//
// The sequence is the recreate the downgrade forbids:
//
//	delete the owner  -> the GC deletes the dependent, because it holds the owner's UID
//	recreate the owner from its snapshot -> a NEW uid
//	the dependent is still gone, and recreating IT would point at nothing
//
// A `recreate` strategy would have reported this undo as done.
func TestREV010TheGarbageCollectorDoesWhatTheDowngradePrevents(t *testing.T) {
	k8s, _, _ := connect(t)
	ctx := context.Background()
	ns := refNamespace(t, ctx, k8s)

	owner := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: "owner", Namespace: ns.Name},
		Data:       map[string]string{"key": "value"},
	}
	if err := k8s.Create(ctx, owner); err != nil {
		t.Fatalf("create owner: %v", err)
	}
	oldUID := owner.UID

	dependent := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{
		Name: "dependent", Namespace: ns.Name,
		OwnerReferences: []metav1.OwnerReference{{
			APIVersion: "v1", Kind: "ConfigMap", Name: owner.Name, UID: oldUID,
		}},
	}}
	if err := k8s.Create(ctx, dependent); err != nil {
		t.Fatalf("create dependent: %v", err)
	}

	if err := k8s.Delete(ctx, owner); err != nil {
		t.Fatalf("delete owner: %v", err)
	}

	// The GC is asynchronous. Polling with a generous ceiling rather than a sleep: a fixed sleep that
	// is too short makes this test flaky in the direction of claiming the GC did nothing, which is
	// the conclusion the whole check argues against.
	deadline := time.Now().Add(3 * time.Minute)
	var collected bool
	for time.Now().Before(deadline) {
		err := k8s.Get(ctx, client.ObjectKey{Namespace: ns.Name, Name: "dependent"}, &corev1.ConfigMap{})
		if apierrors.IsNotFound(err) {
			collected = true
			break
		}
		if err != nil {
			t.Fatalf("polling for the dependent: %v", err)
		}
		time.Sleep(3 * time.Second)
	}
	if !collected {
		t.Fatal("the dependent survived its owner's deletion for three minutes. Either this cluster " +
			"runs no garbage collector -- in which case this probe is not testing what it claims -- or " +
			"cascading deletion has changed and 06 §4.3.1's premise needs re-examining before " +
			"V-REV-010 can be read as evidence of anything.")
	}
	t.Log("the garbage collector deleted the dependent, as 06 §4.3.1 says it does")

	// The recreate a `recreate` plan would have performed.
	restored := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: "owner", Namespace: ns.Name},
		Data:       map[string]string{"key": "value"},
	}
	if err := k8s.Create(ctx, restored); err != nil {
		t.Fatalf("recreate the owner: %v", err)
	}
	if restored.UID == oldUID {
		t.Fatalf("the recreated object kept UID %s; the whole downgrade rests on it not doing that", oldUID)
	}

	// And the dependent is still gone. This is the state an undo reporting `done` would have left.
	err := k8s.Get(ctx, client.ObjectKey{Namespace: ns.Name, Name: "dependent"}, &corev1.ConfigMap{})
	if !apierrors.IsNotFound(err) {
		t.Fatalf("the dependent came back on its own (err %v); if that were true the downgrade would be unnecessary", err)
	}
	t.Logf("recreating the owner produced uid %s (was %s) and did not bring the dependent back: "+
		"a `recreate` plan here would have reported the undo done and left the cluster short an object",
		restored.UID, oldUID)
}

// The scan's cost and its refusal clause, over whatever this cluster actually serves.
//
// The adapter fails the whole call on any kind it cannot list. That is a deliberate trade -- gating a
// delete that could have been recreated is a cost, recreating an object whose owner graph nobody
// could see is an outage -- but the cost is only knowable against a real discovery surface. If this
// cluster serves a kind this credential cannot list, the scan refuses here, and that refusal is the
// evidence: it says the trade is being made on this cluster, at this width of grant, today.
func TestREV010ScanCoversTheFullLiveDiscoverySurface(t *testing.T) {
	k8s, disco, _ := connect(t)
	ctx := context.Background()
	idx := &refindex.Source{Client: k8s, Discovery: disco}
	ns := refNamespace(t, ctx, k8s)

	groups, err := disco.ServerPreferredNamespacedResources()
	if err != nil {
		t.Fatalf("live discovery: %v", err)
	}
	kinds := 0
	for _, g := range groups {
		for _, r := range g.APIResources {
			if !strings.Contains(r.Name, "/") {
				kinds++
			}
		}
	}
	t.Logf("this cluster serves %d namespaced resources across %d groupVersions; the scan below "+
		"lists every listable one of them", kinds, len(groups))
	if kinds < 30 {
		t.Fatalf("only %d namespaced resources discovered; that is fewer than a bare API server serves, "+
			"so this is not the full GKE surface and the check is not measuring what it claims", kinds)
	}

	owner := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: "owner", Namespace: ns.Name}}
	if err := k8s.Create(ctx, owner); err != nil {
		t.Fatalf("create owner: %v", err)
	}

	start := time.Now()
	refs, err := idx.InboundReferences(ctx, refTarget("ConfigMap", ns.Name, owner.Name, owner.UID))
	elapsed := time.Since(start)
	if err != nil {
		// Not swallowed and not skipped. If this cluster has a kind the broker's credential cannot
		// list, every delete against it gates, and that is a fact about the deployment that belongs
		// in the evidence rather than in a comment.
		t.Fatalf("the scan refused over this cluster's live surface after %s: %v\n\n"+
			"This is the adapter's fail-closed clause firing for real. It is correct behaviour and it "+
			"is also a cost: while it holds, no delete on this cluster can be planned as a recreate. "+
			"The remedy is a grant, not a code change -- do not relax the clause.", elapsed, err)
	}
	if len(refs) != 0 {
		t.Fatalf("a freshly created ConfigMap has inbound references: %v", refs)
	}
	t.Logf("a full-surface scan of one namespace took %s", elapsed)

	// The other direction, so the timing above is not a measurement of a scan that quietly did
	// nothing: the same surface, with one dependent planted, must find exactly it.
	dependent := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{
		Name: "dependent", Namespace: ns.Name,
		OwnerReferences: []metav1.OwnerReference{{
			APIVersion: "v1", Kind: "ConfigMap", Name: owner.Name, UID: owner.UID,
		}},
	}}
	if err := k8s.Create(ctx, dependent); err != nil {
		t.Fatalf("create dependent: %v", err)
	}
	refs, err = idx.InboundReferences(ctx, refTarget("ConfigMap", ns.Name, owner.Name, owner.UID))
	if err != nil {
		t.Fatalf("the second scan refused: %v", err)
	}
	if len(refs) != 1 || refs[0].Ref.Name != "dependent" {
		t.Fatalf("the full-surface scan found %v, want exactly the planted dependent", refs)
	}
	t.Log(fmt.Sprintf("found %s", refs[0]))
}
