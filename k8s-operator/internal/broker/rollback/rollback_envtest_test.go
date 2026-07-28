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

package rollback_test

import (
	"context"
	"fmt"
	"os"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/execute"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/rollback"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/undo"
)

// The half of V-REV-011 that needs a real API server.
//
// Every property in rollback_test.go is a refusal this package decides on its own, and a fake is the
// right tool for those: the decision is made before any request leaves the process. What is here is
// the opposite -- three claims about what the SERVER does, which this package's safety argument
// leans on entirely and which nothing in this repository could otherwise demonstrate:
//
//   - `create` fails when the name is taken and server-side apply ADOPTS. The planner's recreate
//     step has no uid precondition and says so, naming this difference as its only protection.
//     Asserting it against a fake would be asserting it against my own belief about the API server;
//     the whole point is that the belief is load-bearing. So both legs are run for real, side by
//     side, and the adopting one is shown adopting.
//   - a uid precondition on a delete is enforced by the server, not by us. The replayer passes it
//     and does not re-check the result, which is only correct if the server refuses.
//   - a deleted-and-recreated object gets a NEW uid. That is the whole premise of pinning one, and
//     envtest is the cheapest place a real deletion can happen.
//
// envtest is L1 by binding.md §Targets: a real API server, process-local, no cluster. It runs no
// controllers, which costs nothing here -- nothing in this file waits for a workload to converge.

var (
	testEnv *envtest.Environment
	k8s     client.Client
	scheme  = runtime.NewScheme()
)

func TestMain(m *testing.M) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		fmt.Fprintln(os.Stderr,
			"KUBEBUILDER_ASSETS unset; run via `make test` to exercise the replayer against a real API server")
		os.Exit(0)
	}
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		panic(err)
	}
	testEnv = &envtest.Environment{Scheme: scheme}
	cfg, err := testEnv.Start()
	if err != nil {
		panic(fmt.Sprintf("start envtest: %v", err))
	}
	k8s, err = client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		panic(fmt.Sprintf("new client: %v", err))
	}
	code := m.Run()
	_ = testEnv.Stop()
	os.Exit(code)
}

// liveReplayer is the production wiring: the real client applier and the real client reader.
func liveReplayer() *rollback.Replayer {
	return &rollback.Replayer{
		Writer: &execute.ClientApplier{Client: k8s},
		Reader: &execute.ClientReader{Client: k8s},
	}
}

func newNS(t *testing.T, ctx context.Context) string {
	t.Helper()
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{GenerateName: "rb-"}}
	if err := k8s.Create(ctx, ns); err != nil {
		t.Fatalf("create namespace: %v", err)
	}
	t.Cleanup(func() { _ = k8s.Delete(context.Background(), ns) })
	return ns.Name
}

func cmRef(ns, name string) agentv1alpha1.TargetRef {
	return agentv1alpha1.TargetRef{Version: "v1", Kind: "ConfigMap", Namespace: ns, Name: name}
}

// sanitizedBody runs the object through the SAME sanitizer the planner uses, rather than
// hand-building a body the replayer happens to accept. That makes these tests a real contract
// between the two packages: a change to the drop list that broke replay would fail here.
func sanitizedBody(t *testing.T, obj client.Object) *runtime.RawExtension {
	t.Helper()
	raw, err := runtime.DefaultUnstructuredConverter.ToUnstructured(obj)
	if err != nil {
		t.Fatalf("to unstructured: %v", err)
	}
	u := &unstructured.Unstructured{Object: raw}
	// A typed object round-tripped through a controller-runtime client has an EMPTY TypeMeta -- the
	// scheme carried the kind, so the struct did not have to. The real capture path reads an
	// unstructured straight off the API server and always has one, and Sanitize refuses a body with
	// no kind for exactly the reason this replayer needs it: a snapshot that does not say what it is
	// cannot be applied. Restoring it here keeps the fixture faithful to the production path rather
	// than working around the check.
	gvks, _, err := scheme.ObjectKinds(obj)
	if err != nil || len(gvks) == 0 {
		t.Fatalf("resolve kind for %T: %v", obj, err)
	}
	u.SetGroupVersionKind(gvks[0])

	sanitized, _, err := undo.Sanitize(u, false)
	if err != nil {
		t.Fatalf("sanitize: %v", err)
	}
	out, err := sanitized.MarshalJSON()
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return &runtime.RawExtension{Raw: out}
}

// --- the difference between create and apply, demonstrated rather than asserted ------------------

// The plan's recreate step carries no uid precondition -- it cannot, the uid died with the object --
// and `plan.go` names its replacement in a comment: "what protects this step instead is that
// `create` fails if something already holds the name". This test is that comment, executed.
//
// The stranger leg is the one that matters. Somebody else has taken the name since the action ran.
// A replay that reached the API server through Apply would not error; it would take the object over
// and report the undo as complete, and the only trace would be a managedFields entry naming the
// agent. Both paths are run against the same server, a fresh object each time, so the difference is
// observed rather than argued.
func TestARecreateRefusesAStrangerAtTheSameNameWhereAnApplyWouldAdoptIt(t *testing.T) {
	ctx := context.Background()
	ns := newNS(t, ctx)

	// The pre-state: what the action deleted, and what the plan will try to put back.
	deleted := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: "contested"},
		Data:       map[string]string{"owner": "the-action"},
	}
	body := sanitizedBody(t, deleted)

	stranger := func(t *testing.T, name string) *corev1.ConfigMap {
		t.Helper()
		cm := &corev1.ConfigMap{
			ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: name},
			Data:       map[string]string{"owner": "somebody-else"},
		}
		if err := k8s.Create(ctx, cm); err != nil {
			t.Fatalf("create the stranger: %v", err)
		}
		return cm
	}

	t.Run("the replayer refuses, and the stranger is untouched", func(t *testing.T) {
		held := stranger(t, "contested")
		r := liveReplayer()

		err := r.Rollback(ctx, "a-1", identity, planOf(agentv1alpha1.UndoStep{
			Op: "create", Target: cmRef(ns, "contested"), Object: body,
		}))
		mustContain(t, err, "something already holds that name")

		var after corev1.ConfigMap
		if err := k8s.Get(ctx, client.ObjectKeyFromObject(held), &after); err != nil {
			t.Fatalf("re-read the stranger: %v", err)
		}
		if after.Data["owner"] != "somebody-else" {
			t.Fatalf("the refused rollback still changed the object: owner = %q", after.Data["owner"])
		}
		if after.UID != held.UID {
			t.Fatalf("the object was replaced: uid %s, want %s", after.UID, held.UID)
		}
	})

}

// The control for the test above, and the reason the recreate step is a create.
//
// It measures what apply ACTUALLY does at a name someone else holds, rather than asserting a belief
// about it. Two outcomes, both wrong in a different way, neither of them a refusal:
//
//   - Fields that do not collide: the apply SUCCEEDS and the two objects are merged. The result
//     holds the stranger's key and the snapshot's key together -- a state that has never existed
//     before, produced by an operation whose entire purpose is to return to one that has. The
//     ActionRecord would say the undo completed.
//   - Fields that do collide: a field-ownership conflict, because ClientApplier deliberately does
//     not force (03 §6 -- a conflict is the `contested` signal). Loud, but it describes managedFields
//     to an operator whose actual problem is that the object they are restoring no longer exists.
//
// If either leg ever starts refusing on its own, this test fails, and the right response is to
// re-examine the recreate step's safety argument -- not to relax the assertion.
func TestARecreateIntoATakenNameThroughApplyIsNeitherARefusalNorARestore(t *testing.T) {
	ctx := context.Background()
	ns := newNS(t, ctx)
	a := &execute.ClientApplier{Client: k8s}
	fm, err := execute.FieldManager(identity)
	if err != nil {
		t.Fatalf("field manager: %v", err)
	}

	// The snapshot the plan would replay: the object as it was before the action deleted it.
	preState := sanitizedBody(t, &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: "x"},
		Data:       map[string]string{"restored-key": "the-action"},
	})
	bodyAt := func(t *testing.T, name string) *unstructured.Unstructured {
		t.Helper()
		u := &unstructured.Unstructured{}
		if err := u.UnmarshalJSON(preState.Raw); err != nil {
			t.Fatalf("decode: %v", err)
		}
		u.SetName(name)
		return u
	}

	t.Run("no field collision: the apply succeeds and merges the two objects", func(t *testing.T) {
		held := &corev1.ConfigMap{
			ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: "merged"},
			Data:       map[string]string{"strangers-key": "somebody-else"},
		}
		if err := k8s.Create(ctx, held); err != nil {
			t.Fatalf("create the stranger: %v", err)
		}
		if _, err := a.Apply(ctx, bodyAt(t, "merged"), fm, false); err != nil {
			t.Fatalf("apply refused a taken name; if this is now the behaviour, the recreate step's "+
				"safety argument needs revisiting rather than this assertion: %v", err)
		}

		var after corev1.ConfigMap
		if err := k8s.Get(ctx, client.ObjectKeyFromObject(held), &after); err != nil {
			t.Fatalf("re-read: %v", err)
		}
		if after.Data["restored-key"] != "the-action" || after.Data["strangers-key"] != "somebody-else" {
			t.Fatalf("data = %v, want both keys -- the merge is the finding", after.Data)
		}
		if after.UID != held.UID {
			t.Fatalf("the stranger's object was replaced rather than merged into: %s", after.UID)
		}
	})

	t.Run("a field collision: a conflict about ownership, not about the object being gone", func(t *testing.T) {
		held := &corev1.ConfigMap{
			ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: "conflicted"},
			Data:       map[string]string{"restored-key": "somebody-else"},
		}
		if err := k8s.Create(ctx, held); err != nil {
			t.Fatalf("create the stranger: %v", err)
		}
		_, err := a.Apply(ctx, bodyAt(t, "conflicted"), fm, false)
		if err == nil {
			t.Fatal("apply overwrote a field another manager owns; ClientApplier must not force")
		}
		if !apierrors.IsConflict(err) {
			t.Fatalf("want a field-ownership conflict, got %v", err)
		}
		// The point: this error says nothing about the object having been deleted and recreated.
		if strings.Contains(err.Error(), "already exists") {
			t.Errorf("the conflict names the real problem, which would weaken the case for create: %v", err)
		}
	})
}

// The negative control for the pair above: when the name is genuinely free, the recreate works. A
// replayer that refused every create would pass the first leg and be useless.
func TestARecreateOntoAFreeNameRestoresTheObject(t *testing.T) {
	ctx := context.Background()
	ns := newNS(t, ctx)

	gone := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: "restored"},
		Data:       map[string]string{"owner": "the-action"},
	}
	r := liveReplayer()
	if err := r.Rollback(ctx, "a-1", identity, planOf(agentv1alpha1.UndoStep{
		Op: "create", Target: cmRef(ns, "restored"), Object: sanitizedBody(t, gone),
	})); err != nil {
		t.Fatalf("recreate onto a free name: %v", err)
	}

	var after corev1.ConfigMap
	if err := k8s.Get(ctx, client.ObjectKey{Namespace: ns, Name: "restored"}, &after); err != nil {
		t.Fatalf("the recreated object is not there: %v", err)
	}
	if after.Data["owner"] != "the-action" {
		t.Fatalf("data = %v, want the pre-state", after.Data)
	}
	fm, _ := execute.FieldManager(identity)
	if !hasManager(after.ManagedFields, fm) {
		t.Errorf("managedFields %v does not name %q; the rollback is unattributable", after.ManagedFields, fm)
	}
}

// --- the uid pin, enforced by the server ----------------------------------------------------------

// The replayer passes the pin to Delete and does not re-check the outcome, which is only correct if
// the server refuses. This deletes and recreates at the same name so the replacement is a real one
// with a real new uid -- the exact situation the pin exists for, and the one a name-only delete
// would destroy.
func TestADeleteWithAPinnedUidWillNotRemoveAReplacement(t *testing.T) {
	ctx := context.Background()
	ns := newNS(t, ctx)

	original := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: "pinned"}}
	if err := k8s.Create(ctx, original); err != nil {
		t.Fatalf("create: %v", err)
	}
	originalUID := string(original.UID)

	if err := k8s.Delete(ctx, original); err != nil {
		t.Fatalf("delete: %v", err)
	}
	replacement := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: "pinned"},
		Data:       map[string]string{"owner": "somebody-else"},
	}
	if err := k8s.Create(ctx, replacement); err != nil {
		t.Fatalf("recreate at the same name: %v", err)
	}
	if string(replacement.UID) == originalUID {
		t.Fatalf("the API server reused the uid, so this test cannot demonstrate anything")
	}

	r := liveReplayer()

	t.Run("the stale pin does not delete the replacement", func(t *testing.T) {
		err := r.Rollback(ctx, "a-1", identity, planOf(agentv1alpha1.UndoStep{
			Op: "delete", Target: cmRef(ns, "pinned"),
			Preconditions: &agentv1alpha1.UndoPrecondition{UID: originalUID},
		}))
		if err == nil {
			t.Fatal("the rollback deleted an object it had not pinned")
		}
		var after corev1.ConfigMap
		if err := k8s.Get(ctx, client.ObjectKey{Namespace: ns, Name: "pinned"}, &after); err != nil {
			t.Fatalf("the replacement was destroyed by a stale pin: %v", err)
		}
	})

	// The control: the pin that DOES match removes the object. Without it, "the delete failed"
	// could be true of every delete this package performs.
	t.Run("the live pin deletes it", func(t *testing.T) {
		if err := r.Rollback(ctx, "a-2", identity, planOf(agentv1alpha1.UndoStep{
			Op: "delete", Target: cmRef(ns, "pinned"),
			Preconditions: &agentv1alpha1.UndoPrecondition{UID: string(replacement.UID)},
		})); err != nil {
			t.Fatalf("a delete with the live uid: %v", err)
		}
		var after corev1.ConfigMap
		err := k8s.Get(ctx, client.ObjectKey{Namespace: ns, Name: "pinned"}, &after)
		if !apierrors.IsNotFound(err) {
			t.Fatalf("the object is still there after a matching-pin delete: %v", err)
		}
	})
}

// A restore replays onto the object the plan pinned, and refuses once that object has been replaced
// -- even though a healthy object of the right kind and name is sitting right there. Only the uid
// tells them apart, and only a real deletion produces the new uid.
func TestARestoreRefusesAReplacementAndSucceedsOnTheOriginal(t *testing.T) {
	ctx := context.Background()
	ns := newNS(t, ctx)

	// The object is created THROUGH the broker's applier, under the agent's field manager, because
	// that is what the object an agent is about to change looks like in production -- and because
	// the alternative revealed something worth stating. Created with a plain client, `data.level` is
	// owned by that client's manager, and the rollback's apply then fails with a field-ownership
	// conflict rather than restoring anything. That is CORRECT (ClientApplier does not force; 03 §6
	// makes a conflict the `contested` signal) and it is not a rollback bug -- but it does mean a
	// restore can only revert what the agent's own manager owns. An action over a field owned by
	// somebody else is classified `contested` upstream and never reaches execution, which is what
	// keeps that from being a gap. Recorded in the ledger rather than left as a surprise here.
	a := &execute.ClientApplier{Client: k8s}
	fm, err := execute.FieldManager(identity)
	if err != nil {
		t.Fatalf("field manager: %v", err)
	}
	seed := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "v1", "kind": "ConfigMap",
		"metadata": map[string]any{"name": "target", "namespace": ns},
		"data":     map[string]any{"level": "info"},
	}}
	if _, err := a.Apply(ctx, seed, fm, false); err != nil {
		t.Fatalf("seed the object as the agent: %v", err)
	}

	var cm corev1.ConfigMap
	if err := k8s.Get(ctx, client.ObjectKey{Namespace: ns, Name: "target"}, &cm); err != nil {
		t.Fatalf("re-read: %v", err)
	}
	preState := sanitizedBody(t, &cm)
	pinned := &agentv1alpha1.UndoPrecondition{UID: string(cm.UID)}
	r := liveReplayer()

	t.Run("the action's change is reverted", func(t *testing.T) {
		acted := seed.DeepCopy()
		if err := unstructured.SetNestedField(acted.Object, "debug", "data", "level"); err != nil {
			t.Fatalf("compose the action: %v", err)
		}
		if _, err := a.Apply(ctx, acted, fm, false); err != nil {
			t.Fatalf("simulate the action: %v", err)
		}
		if err := r.Rollback(ctx, "a-1", identity, planOf(agentv1alpha1.UndoStep{
			Op: "apply", Target: cmRef(ns, "target"), Object: preState, Preconditions: pinned,
		})); err != nil {
			t.Fatalf("restore: %v", err)
		}
		var after corev1.ConfigMap
		if err := k8s.Get(ctx, client.ObjectKey{Namespace: ns, Name: "target"}, &after); err != nil {
			t.Fatalf("re-read: %v", err)
		}
		if after.Data["level"] != "info" {
			t.Fatalf("level = %q, want the pre-state's info", after.Data["level"])
		}
	})

	t.Run("the same plan is refused once the object has been replaced", func(t *testing.T) {
		var live corev1.ConfigMap
		if err := k8s.Get(ctx, client.ObjectKey{Namespace: ns, Name: "target"}, &live); err != nil {
			t.Fatalf("re-read: %v", err)
		}
		if err := k8s.Delete(ctx, &live); err != nil {
			t.Fatalf("delete: %v", err)
		}
		replacement := &corev1.ConfigMap{
			ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: "target"},
			Data:       map[string]string{"level": "somebody-else's"},
		}
		if err := k8s.Create(ctx, replacement); err != nil {
			t.Fatalf("recreate: %v", err)
		}

		err := r.Rollback(ctx, "a-2", identity, planOf(agentv1alpha1.UndoStep{
			Op: "apply", Target: cmRef(ns, "target"), Object: preState, Preconditions: pinned,
		}))
		mustContain(t, err, "was replaced after the action")

		var after corev1.ConfigMap
		if err := k8s.Get(ctx, client.ObjectKey{Namespace: ns, Name: "target"}, &after); err != nil {
			t.Fatalf("re-read: %v", err)
		}
		if after.Data["level"] != "somebody-else's" {
			t.Fatalf("the refused restore still overwrote a stranger's object: %v", after.Data)
		}
	})
}

// A scale goes through the scale subresource against a real server, and touches nothing else. The
// second assertion is the one that distinguishes this from applying the snapshot: a field another
// manager changed after the action survives the rollback.
func TestAScaleRestoresTheCountWithoutRevertingAnyoneElsesChanges(t *testing.T) {
	ctx := context.Background()
	ns := newNS(t, ctx)

	dep := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: "api"},
		Spec: appsv1.DeploymentSpec{
			Replicas: ptr(int32(3)),
			Selector: &metav1.LabelSelector{MatchLabels: map[string]string{"app": "api"}},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: map[string]string{"app": "api"}},
				Spec: corev1.PodSpec{Containers: []corev1.Container{
					{Name: "c", Image: "registry.k8s.io/pause:3.9"},
				}},
			},
		},
	}
	if err := k8s.Create(ctx, dep); err != nil {
		t.Fatalf("create: %v", err)
	}
	preState := sanitizedBody(t, dep)

	// The action scales to 10; somebody else independently annotates the object.
	var live appsv1.Deployment
	if err := k8s.Get(ctx, client.ObjectKeyFromObject(dep), &live); err != nil {
		t.Fatalf("re-read: %v", err)
	}
	live.Spec.Replicas = ptr(int32(10))
	live.Annotations = map[string]string{"somebody-else/owns": "this"}
	if err := k8s.Update(ctx, &live); err != nil {
		t.Fatalf("simulate the action and a concurrent change: %v", err)
	}

	if err := liveReplayer().Rollback(ctx, "a-1", identity, planOf(agentv1alpha1.UndoStep{
		Op:     "scale",
		Target: agentv1alpha1.TargetRef{Group: "apps", Version: "v1", Kind: "Deployment", Namespace: ns, Name: "api"},
		Object: preState, Preconditions: &agentv1alpha1.UndoPrecondition{UID: string(dep.UID)},
	})); err != nil {
		t.Fatalf("scale rollback: %v", err)
	}

	var after appsv1.Deployment
	if err := k8s.Get(ctx, client.ObjectKeyFromObject(dep), &after); err != nil {
		t.Fatalf("re-read: %v", err)
	}
	if got := *after.Spec.Replicas; got != 3 {
		t.Fatalf("replicas = %d, want the pre-state's 3", got)
	}
	// The whole reason `scale` is a distinct op. Applying the snapshot would have removed this.
	if after.Annotations["somebody-else/owns"] != "this" {
		t.Fatalf("the rollback reverted a change it did not make: annotations = %v", after.Annotations)
	}
}

func ptr[T any](v T) *T { return &v }

func hasManager(entries []metav1.ManagedFieldsEntry, want string) bool {
	for _, e := range entries {
		if e.Manager == want {
			return true
		}
	}
	return false
}
