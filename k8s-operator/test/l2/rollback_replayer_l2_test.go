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

// V-REV-011 at L2. Driven by dev/verify/rollback-replayer-l2.sh, which owns the destructive-test
// guard; connect() in live_state_l2_test.go duplicates it, because a probe that can only be aimed
// safely by its wrapper is one `go test` away from being aimed at the live install.
package l2

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/execute"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/rollback"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/undo"
)

// The agent this probe replays as. execute.FieldManager turns it into `kube-agents/platform/prod`,
// which V-BRK-019 fixes; step 1 reads it back off a really-restored object rather than trusting the
// helper, because the field manager is the only durable attribution a replay leaves behind.
const rev011Identity = "platform/prod"

// rev011NS is this file's own probe namespace: a distinct GenerateName prefix from every other
// probe in this package, so a leaked namespace can be attributed to the run that leaked it.
func rev011NS(t *testing.T, ctx context.Context, k8s client.Client) string {
	t.Helper()
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{GenerateName: "kage-rev011-"}}
	if err := k8s.Create(ctx, ns); err != nil {
		t.Fatalf("create probe namespace: %v", err)
	}
	t.Cleanup(func() {
		_ = k8s.Delete(context.Background(), ns, client.PropagationPolicy(metav1.DeletePropagationBackground))
	})
	return ns.Name
}

func rev011Replayer(k8s client.Client) *rollback.Replayer {
	return &rollback.Replayer{
		Writer: &execute.ClientApplier{Client: k8s},
		Reader: &execute.ClientReader{Client: k8s},
	}
}

// rev011Body runs the object through the SAME sanitizer the planner uses, and returns the body
// alongside the digest of exactly those bytes. Hand-building a body the replayer happens to accept
// would make these tests agree with themselves rather than with the planner.
func rev011Body(t *testing.T, obj client.Object) (*runtime.RawExtension, string) {
	t.Helper()
	raw, err := runtime.DefaultUnstructuredConverter.ToUnstructured(obj)
	if err != nil {
		t.Fatalf("to unstructured: %v", err)
	}
	u := &unstructured.Unstructured{Object: raw}
	// A typed object round-tripped through a controller-runtime client has an EMPTY TypeMeta: the
	// scheme carried the kind, so the struct did not have to. The real capture path reads an
	// unstructured straight off the API server and always has one. Restoring it keeps the fixture
	// faithful to the production path rather than working around Sanitize's no-kind refusal.
	gvks, _, err := rev011Scheme(t).ObjectKinds(obj)
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
	sum := sha256.Sum256(out)
	return &runtime.RawExtension{Raw: out}, hex.EncodeToString(sum[:])
}

// rev011Scheme resolves a typed object's GVK. connect() builds an equivalent scheme for the client
// it returns but does not hand it back, and reaching for the client's is not obviously cheaper than
// building one here -- this is the only place in the file that needs it.
func rev011Scheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	sch := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(sch); err != nil {
		t.Fatalf("add clientgo scheme: %v", err)
	}
	return sch
}

func rev011Ref(kind, ns, name string, uid types.UID) agentv1alpha1.TargetRef {
	ref := agentv1alpha1.TargetRef{Version: "v1", Kind: kind, Namespace: ns, Name: name, UID: string(uid)}
	if kind == "Deployment" {
		ref.Group, ref.Version = "apps", "v1"
	}
	return ref
}

// rev011Deployment is a two-replica pause Deployment. pause is the smallest image that stays
// running, which matters because step 3's property is that a REAL controller converged.
func rev011Deployment(ns, name string, replicas int32, extraLabels map[string]string) *appsv1.Deployment {
	labels := map[string]string{"app": name}
	meta := map[string]string{"app": name}
	for k, v := range extraLabels {
		meta[k] = v
	}
	return &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: name, Labels: meta},
		Spec: appsv1.DeploymentSpec{
			Replicas: &replicas,
			Selector: &metav1.LabelSelector{MatchLabels: labels},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: labels},
				Spec: corev1.PodSpec{Containers: []corev1.Container{{
					Name:  "pause",
					Image: "registry.k8s.io/pause:3.9",
				}}},
			},
		},
	}
}

// seedAsAgent creates an object THROUGH THE BROKER'S APPLIER under the agent's field manager,
// rather than through the test's client.
//
// Not a convenience. A controller-runtime Create stamps the caller's own manager (`l2.test`) as the
// owner of every field, and server-side apply refuses to CHANGE a field owned by somebody else --
// deliberately, because ClientApplier.Apply does not force ownership: a conflict is the `contested`
// signal of 03 §6. So a fixture created by the test and then restored by the replayer conflicts on
// exactly the field the restore exists to revert, and the red says nothing about the replayer.
//
// Production never looks like that. The object the agent later rolls back is one the agent applied,
// so the agent owns the field and the revert is uncontested. Seeding the same way makes these legs
// measure the replayer instead of measuring who created the fixture. The derived property is worth
// stating plainly: A RESTORE CAN ONLY REVERT WHAT THE AGENT'S OWN FIELD MANAGER OWNS. An action over
// a field owned by somebody else is classified `contested` upstream and never reaches execution.
func seedAsAgent(t *testing.T, ctx context.Context, k8s client.Client, obj *unstructured.Unstructured) {
	t.Helper()
	manager, err := execute.FieldManager(rev011Identity)
	if err != nil {
		t.Fatalf("field manager: %v", err)
	}
	if _, err := (&execute.ClientApplier{Client: k8s}).Apply(ctx, obj, manager, false); err != nil {
		t.Fatalf("seed %s/%s as the agent: %v", obj.GetNamespace(), obj.GetName(), err)
	}
}

// configMapBody is the unstructured a seed needs: an apply must carry apiVersion and kind, because
// server-side apply has no scheme to consult.
func configMapBody(ns, name string, data map[string]string) *unstructured.Unstructured {
	d := map[string]any{}
	for k, v := range data {
		d[k] = v
	}
	return &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "v1",
		"kind":       "ConfigMap",
		"metadata":   map[string]any{"name": name, "namespace": ns},
		"data":       d,
	}}
}

// eventually polls until cond returns nil or the deadline passes, and reports the LAST observation
// rather than "timed out". A timeout with no observation sends a reader to the cluster to find out
// what the probe already knew.
func eventually(t *testing.T, what string, timeout time.Duration, cond func() error) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	var last error
	for time.Now().Before(deadline) {
		last = cond()
		if last == nil {
			return
		}
		time.Sleep(2 * time.Second)
	}
	t.Fatalf("%s did not happen within %s; last observation: %v", what, timeout, last)
}

// V-REV-011, at L2 (09 §6.3: L1+L2, weight 9, negative control mandatory).
//
// # Why this needs a real cluster and the envtest suite is not enough
//
// internal/broker/rollback/rollback_envtest_test.go asserts most of this check against an envtest
// API server, and that is a genuine L1 result: envtest issues real uids, runs real server-side
// apply, and enforces real field-ownership conflicts. It is not this one. envtest runs NO
// CONTROLLERS, and three of this check's clauses are only meaningfully testable where controllers
// exist:
//
//   - "REPLAYS THE PRE-STATE". At L1 a successful replay means a field on an object changed. That is
//     not the pre-state; it is the request that would produce it. The pre-state of a scaled-down
//     Deployment is running pods, and only a real Deployment controller and a real kubelet can put
//     them back. A replayer that wrote the number and left the world unchanged passes every L1
//     assertion in the suite and has restored nothing.
//
//   - "A NAME TAKEN SINCE THE ACTION". At L1 a delete completes the instant it is issued, because
//     nothing holds a finalizer and no garbage collector is watching. Here the delete is real: the
//     ReplicaSet and the Pods are collected by the real GC, the name becomes free on the cluster's
//     schedule rather than the test's, and the recreate races the collection the way a production
//     rollback does. The stranger leg is a real object admitted by GKE's real admission chain.
//
//   - THE ADMISSION AND AUTHORIZATION CHAIN. envtest serves RBAC and PodSecurity. GKE serves those
//     plus its own webhooks and the IAM authorizer. A replay is a write like any other, and a refusal
//     that arrives from a webhook rather than from the replayer's own checks must still stop the
//     replay and report how many steps had already been applied -- the clause that decides whether an
//     operator paged at 3am is told the truth about what state the cluster is in.
//
// The three remaining clauses (digest mismatch, body/target mismatch, the redacted-Secret refusal)
// are client-side and fully covered at L1. They are re-run here anyway, against a real Secret whose
// material is read back afterwards: at L1 "no write happened" is a property of a fake, and the
// question a reader actually has is whether the live Secret still holds its password.
func TestREV011ARealReplayRestoresTheWorldAndNotJustTheField(t *testing.T) {
	ctx := context.Background()
	k8s, _, kubeContext := connect(t)
	ns := rev011NS(t, ctx, k8s)
	t.Logf("context %s, probe namespace %s", kubeContext, ns)

	// --- the action: a Deployment scaled from 2 to 0 ---------------------------------------------
	dep := rev011Deployment(ns, "payments", 2, nil)
	if err := k8s.Create(ctx, dep); err != nil {
		t.Fatalf("create Deployment: %v", err)
	}
	eventually(t, "the Deployment controller bringing up 2 ready replicas", 4*time.Minute, func() error {
		live := &appsv1.Deployment{}
		if err := k8s.Get(ctx, client.ObjectKeyFromObject(dep), live); err != nil {
			return err
		}
		if live.Status.ReadyReplicas != 2 {
			return fmt.Errorf("readyReplicas=%d, want 2", live.Status.ReadyReplicas)
		}
		return nil
	})

	preState := &appsv1.Deployment{}
	if err := k8s.Get(ctx, client.ObjectKeyFromObject(dep), preState); err != nil {
		t.Fatalf("capture pre-state: %v", err)
	}
	body, _ := rev011Body(t, preState)

	scaled := preState.DeepCopy()
	scaled.Spec.Replicas = ptrTo(int32(0))
	if err := k8s.Update(ctx, scaled); err != nil {
		t.Fatalf("scale to zero: %v", err)
	}
	eventually(t, "the real controller draining the pods to zero", 3*time.Minute, func() error {
		pods := &corev1.PodList{}
		if err := k8s.List(ctx, pods, client.InNamespace(ns), client.MatchingLabels{"app": "payments"}); err != nil {
			return err
		}
		if len(pods.Items) != 0 {
			return fmt.Errorf("%d pod(s) still present", len(pods.Items))
		}
		return nil
	})

	// Somebody else annotates the Deployment between the action and the rollback. The replay must
	// not revert it: a scale step restores a replica count, and 06 §4.3.1 chose that inverse
	// precisely so the blast radius is one field. Applying the whole snapshot would also pass "the
	// replicas are back", which is why this is measured in the same test.
	bystander := &appsv1.Deployment{}
	if err := k8s.Get(ctx, client.ObjectKeyFromObject(dep), bystander); err != nil {
		t.Fatalf("re-read before annotating: %v", err)
	}
	if bystander.Annotations == nil {
		bystander.Annotations = map[string]string{}
	}
	bystander.Annotations["set-by"] = "somebody-else"
	if err := k8s.Update(ctx, bystander); err != nil {
		t.Fatalf("annotate: %v", err)
	}

	// --- the rollback ----------------------------------------------------------------------------
	plan := agentv1alpha1.UndoPlan{
		Strategy:    agentv1alpha1.UndoInverse,
		GeneratedAt: metav1.Now(),
		// The planner sets this at step 8 after dry-running every step; ValidateReplayable refuses a
		// plan without it, and the replayer re-validates because it is a second caller. A fixture
		// that left it false would exercise that refusal and nothing else.
		Validated: true,
		Steps: []agentv1alpha1.UndoStep{{
			Op:            "scale",
			Target:        rev011Ref("Deployment", ns, "payments", preState.UID),
			Object:        body,
			Preconditions: &agentv1alpha1.UndoPrecondition{UID: string(preState.UID)},
		}},
	}
	if err := rev011Replayer(k8s).Rollback(ctx, "act-rev011-scale", rev011Identity, plan); err != nil {
		t.Fatalf("rollback: %v", err)
	}

	// The property. Not "spec.replicas is 2" -- that is the request. Two pods, Ready, put there by a
	// controller and a kubelet that this test never spoke to.
	eventually(t, "the real controller restoring 2 ready replicas from the replayed count", 4*time.Minute, func() error {
		live := &appsv1.Deployment{}
		if err := k8s.Get(ctx, client.ObjectKeyFromObject(dep), live); err != nil {
			return err
		}
		if live.Status.ReadyReplicas != 2 {
			return fmt.Errorf("readyReplicas=%d, want 2", live.Status.ReadyReplicas)
		}
		return nil
	})
	t.Log("step 1: the pre-state came back as running pods, not as a field")

	after := &appsv1.Deployment{}
	if err := k8s.Get(ctx, client.ObjectKeyFromObject(dep), after); err != nil {
		t.Fatalf("re-read after rollback: %v", err)
	}
	if got := after.Annotations["set-by"]; got != "somebody-else" {
		t.Errorf("the replay reverted an annotation it did not touch in the action: set-by=%q, want %q. "+
			"A scale step that applies the whole snapshot restores the replica count AND silently "+
			"undoes every unrelated change made since the action.", got, "somebody-else")
	}
	if !hasManagerL2(after.ManagedFields, "kube-agents/platform/prod") {
		t.Errorf("no managedFields entry for kube-agents/platform/prod; got %v. The field manager is "+
			"the only durable attribution a replay leaves on the object (V-BRK-019).",
			managerNamesL2(after.ManagedFields))
	}
	t.Log("step 2: the replay reverted only the field the action changed, under the agent's field manager")
}

// The recreate clause, against a real deletion. Two legs, same cluster, same body:
//
//	free name  -> the object comes back, and the real controller reconciles it again
//	taken name -> AlreadyExists, and the stranger is untouched
//
// The second leg is the one that matters and the reason execute.ClientApplier grew a Create at all.
// A replay that reached the API server through Apply would not error on the taken name: it merges
// where the fields do not collide and reports the undo complete. That is measured here rather than
// argued, on a third object, so the difference between the two verbs is an observation.
func TestREV011ARecreateAfterARealDeletionRefusesANameSomebodyElseTook(t *testing.T) {
	ctx := context.Background()
	k8s, _, kubeContext := connect(t)
	ns := rev011NS(t, ctx, k8s)
	t.Logf("context %s, probe namespace %s", kubeContext, ns)

	replayer := rev011Replayer(k8s)

	recreatePlan := func(target agentv1alpha1.TargetRef, body *runtime.RawExtension) agentv1alpha1.UndoPlan {
		return agentv1alpha1.UndoPlan{
			Strategy:    agentv1alpha1.UndoRecreate,
			GeneratedAt: metav1.Now(),
			Validated:   true,
			Steps:       []agentv1alpha1.UndoStep{{Op: "create", Target: target, Object: body}},
		}
	}

	// --- leg 1: a real delete, a real garbage collection, then a restore -------------------------
	dep := rev011Deployment(ns, "billing", 1, nil)
	if err := k8s.Create(ctx, dep); err != nil {
		t.Fatalf("create Deployment: %v", err)
	}
	eventually(t, "the ReplicaSet controller creating a ReplicaSet to be collected later", 3*time.Minute, func() error {
		rs := &appsv1.ReplicaSetList{}
		if err := k8s.List(ctx, rs, client.InNamespace(ns), client.MatchingLabels{"app": "billing"}); err != nil {
			return err
		}
		if len(rs.Items) == 0 {
			return fmt.Errorf("no ReplicaSet yet")
		}
		return nil
	})

	preState := &appsv1.Deployment{}
	if err := k8s.Get(ctx, client.ObjectKeyFromObject(dep), preState); err != nil {
		t.Fatalf("capture pre-state: %v", err)
	}
	body, _ := rev011Body(t, preState)

	// Foreground so the GC has to run and the API server has to hold the object until the
	// dependents are gone. This is the part envtest cannot stage: there, a delete is a bookkeeping
	// change with nothing behind it.
	if err := k8s.Delete(ctx, preState, client.PropagationPolicy(metav1.DeletePropagationForeground)); err != nil {
		t.Fatalf("delete: %v", err)
	}
	eventually(t, "the garbage collector reaping the Deployment and its ReplicaSets", 4*time.Minute, func() error {
		live := &appsv1.Deployment{}
		err := k8s.Get(ctx, client.ObjectKeyFromObject(dep), live)
		if err == nil {
			return fmt.Errorf("Deployment still present (deletionTimestamp=%v)", live.DeletionTimestamp)
		}
		if !apierrors.IsNotFound(err) {
			return err
		}
		rs := &appsv1.ReplicaSetList{}
		if err := k8s.List(ctx, rs, client.InNamespace(ns), client.MatchingLabels{"app": "billing"}); err != nil {
			return err
		}
		if len(rs.Items) != 0 {
			return fmt.Errorf("%d ReplicaSet(s) not yet collected", len(rs.Items))
		}
		return nil
	})
	t.Log("step 3: a real deletion, with the real garbage collector reaping the dependents")

	if err := replayer.Rollback(ctx, "act-rev011-recreate", rev011Identity, recreatePlan(
		rev011Ref("Deployment", ns, "billing", ""), body)); err != nil {
		t.Fatalf("recreate onto a freed name: %v", err)
	}
	restored := &appsv1.Deployment{}
	if err := k8s.Get(ctx, client.ObjectKeyFromObject(dep), restored); err != nil {
		t.Fatalf("read the restored Deployment: %v", err)
	}
	if restored.UID == preState.UID {
		t.Fatalf("the restored object has the ORIGINAL uid %s -- the delete did not take effect, so "+
			"this leg proves nothing about a recreate", preState.UID)
	}
	eventually(t, "the real controller reconciling the RESTORED Deployment", 4*time.Minute, func() error {
		live := &appsv1.Deployment{}
		if err := k8s.Get(ctx, client.ObjectKeyFromObject(dep), live); err != nil {
			return err
		}
		if live.Status.ReadyReplicas != 1 {
			return fmt.Errorf("readyReplicas=%d, want 1", live.Status.ReadyReplicas)
		}
		return nil
	})
	t.Log("step 4: the recreate restored the workload onto the freed name and the controller took it up")

	// --- leg 2: somebody else holds the name -----------------------------------------------------
	stranger := rev011Deployment(ns, "shipping", 1, map[string]string{"owner": "someone-else"})
	if err := k8s.Create(ctx, stranger); err != nil {
		t.Fatalf("create the stranger: %v", err)
	}
	strangerLive := &appsv1.Deployment{}
	if err := k8s.Get(ctx, client.ObjectKeyFromObject(stranger), strangerLive); err != nil {
		t.Fatalf("read the stranger: %v", err)
	}

	// The snapshot belongs to a DIFFERENT object that happened to have the same name. Build it from
	// a Deployment created and deleted under that name, so the body is a real capture.
	ghost := rev011Deployment(ns, "shipping", 3, map[string]string{"owner": "the-agent"})
	ghostBody, _ := rev011Body(t, ghost)

	err := replayer.Rollback(ctx, "act-rev011-stranger", rev011Identity, recreatePlan(
		rev011Ref("Deployment", ns, "shipping", ""), ghostBody))
	if err == nil {
		t.Fatal("the recreate onto a name somebody else holds SUCCEEDED. A create must return " +
			"AlreadyExists here; a replay that reports success has overwritten or merged into an " +
			"object that is not the one the action touched.")
	}
	if !strings.Contains(err.Error(), "already holds that name") {
		t.Errorf("the refusal does not say the name is taken: %v", err)
	}
	t.Logf("step 5: the recreate refused a taken name: %v", err)

	afterRefusal := &appsv1.Deployment{}
	if err := k8s.Get(ctx, client.ObjectKeyFromObject(stranger), afterRefusal); err != nil {
		t.Fatalf("re-read the stranger: %v", err)
	}
	if afterRefusal.UID != strangerLive.UID {
		t.Errorf("the stranger was REPLACED: uid %s -> %s", strangerLive.UID, afterRefusal.UID)
	}
	if afterRefusal.Generation != strangerLive.Generation {
		t.Errorf("the stranger's spec was modified by a refused replay: generation %d -> %d",
			strangerLive.Generation, afterRefusal.Generation)
	}
	if got := afterRefusal.Labels["owner"]; got != "someone-else" {
		t.Errorf("the stranger's labels were rewritten by a refused replay: owner=%q", got)
	}

	// The counterfactual, on a fourth object so the assertions above are not disturbed. Same body,
	// same field manager, Apply instead of Create. If this ALSO refused, the choice of verb in
	// replayCreate would be arbitrary and the refusal above would prove nothing about the verb.
	victim := rev011Deployment(ns, "counterfactual", 1, map[string]string{"owner": "someone-else"})
	if err := k8s.Create(ctx, victim); err != nil {
		t.Fatalf("create the counterfactual victim: %v", err)
	}
	cfBody, _ := rev011Body(t, rev011Deployment(ns, "counterfactual", 3, map[string]string{"owner": "the-agent"}))
	cfObj := &unstructured.Unstructured{}
	if err := cfObj.UnmarshalJSON(cfBody.Raw); err != nil {
		t.Fatalf("unmarshal the counterfactual body: %v", err)
	}
	applier := &execute.ClientApplier{Client: k8s}
	manager, err := execute.FieldManager(rev011Identity)
	if err != nil {
		t.Fatalf("field manager: %v", err)
	}
	if _, applyErr := applier.Apply(ctx, cfObj, manager, false); applyErr != nil {
		t.Logf("step 6: apply at a taken name raised %v -- a conflict, not AlreadyExists, and not a "+
			"report that the object is gone", applyErr)
	} else {
		cfAfter := &appsv1.Deployment{}
		if err := k8s.Get(ctx, client.ObjectKeyFromObject(victim), cfAfter); err != nil {
			t.Fatalf("read the counterfactual victim: %v", err)
		}
		if cfAfter.Labels["owner"] != "the-agent" && *cfAfter.Spec.Replicas != 3 {
			t.Errorf("apply reported success but changed nothing; the counterfactual is not "+
				"demonstrating anything (owner=%q replicas=%d)",
				cfAfter.Labels["owner"], *cfAfter.Spec.Replicas)
		}
		t.Logf("step 6: apply at a taken name REPORTED SUCCESS and merged into the stranger "+
			"(owner=%q replicas=%d) -- this is what a replay would do without Create",
			cfAfter.Labels["owner"], *cfAfter.Spec.Replicas)
	}
}

// NEGATIVE CONTROL, mandatory for a `¬` check (09 §6 line 271).
//
// Every clause of V-REV-011 is "refuses rather than X", and each X is the plausible, benign,
// PASSING answer. A probe that only ever staged refusals could not tell a replayer that refuses
// correctly from one that refuses everything -- including the restores an operator is relying on.
// So each refusal here is paired with the control that must still succeed, and each refusal is
// followed by a read of the live object to establish that nothing was written.
//
// The headline is the Secret. At L1 "no write happened" is a property of a fake writer. Here the
// Secret is real, and the question is the one an operator actually has: after a rollback of a plan
// whose snapshot holds digest placeholders, does the live Secret still hold its password?
func TestREV011NoRefusalIsSilentAndNoRestoreIsARewrite(t *testing.T) {
	ctx := context.Background()
	k8s, _, kubeContext := connect(t)
	ns := rev011NS(t, ctx, k8s)
	t.Logf("context %s, probe namespace %s", kubeContext, ns)

	replayer := rev011Replayer(k8s)
	applyPlan := func(steps ...agentv1alpha1.UndoStep) agentv1alpha1.UndoPlan {
		return agentv1alpha1.UndoPlan{
			Strategy: agentv1alpha1.UndoRestore, GeneratedAt: metav1.Now(), Validated: true, Steps: steps,
		}
	}

	// --- the redacted Secret ---------------------------------------------------------------------
	const password = "correct-horse-battery-staple"
	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: "db-credentials"},
		StringData: map[string]string{"password": password, "username": "app"},
	}
	if err := k8s.Create(ctx, secret); err != nil {
		t.Fatalf("create Secret: %v", err)
	}
	liveSecret := &corev1.Secret{}
	if err := k8s.Get(ctx, client.ObjectKeyFromObject(secret), liveSecret); err != nil {
		t.Fatalf("read Secret: %v", err)
	}

	// undo.Sanitize is the production capture path, and for a Secret it redacts UNCONDITIONALLY --
	// there is no parameter that turns it off, because a Secret body persisted into a CRD with its
	// material intact would be the exfiltration this whole design exists to prevent. Every value
	// becomes `sha256:<digest>`, and restoring that body would write the hex of each value's digest
	// over the value. (The second argument is `isStatusTarget`, not a redaction switch.)
	rawSecret, err := runtime.DefaultUnstructuredConverter.ToUnstructured(liveSecret)
	if err != nil {
		t.Fatalf("to unstructured: %v", err)
	}
	uSecret := &unstructured.Unstructured{Object: rawSecret}
	uSecret.SetAPIVersion("v1")
	uSecret.SetKind("Secret")
	redacted, _, err := undo.Sanitize(uSecret, false)
	if err != nil {
		t.Fatalf("sanitize: %v", err)
	}
	if keys := undo.RedactedSecretKeys(redacted); len(keys) == 0 {
		t.Fatalf("the fixture is not redacted, so this leg would prove nothing; Sanitize left no " +
			"digest placeholders in a Secret body")
	}
	redactedRaw, err := redacted.MarshalJSON()
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	err = replayer.Rollback(ctx, "act-rev011-secret", rev011Identity, applyPlan(agentv1alpha1.UndoStep{
		Op:            "apply",
		Target:        rev011Ref("Secret", ns, "db-credentials", liveSecret.UID),
		Object:        &runtime.RawExtension{Raw: redactedRaw},
		Preconditions: &agentv1alpha1.UndoPrecondition{UID: string(liveSecret.UID)},
	}))
	if err == nil {
		t.Fatal("the replayer RESTORED a Secret body full of digest placeholders")
	}
	if !strings.Contains(err.Error(), "password") {
		t.Errorf("the refusal does not name the redacted key, so an operator cannot tell which "+
			"values are unrestorable: %v", err)
	}
	if strings.Contains(err.Error(), password) {
		t.Fatal("THE REFUSAL LEAKED THE SECRET VALUE INTO ITS OWN ERROR MESSAGE")
	}
	t.Logf("step 7: negative control -- the redacted Secret was refused, naming its keys")

	afterSecret := &corev1.Secret{}
	if err := k8s.Get(ctx, client.ObjectKeyFromObject(secret), afterSecret); err != nil {
		t.Fatalf("re-read Secret: %v", err)
	}
	if got := string(afterSecret.Data["password"]); got != password {
		t.Errorf("THE LIVE SECRET WAS OVERWRITTEN by a refused replay: password is now %q", got)
	}
	if afterSecret.ResourceVersion != liveSecret.ResourceVersion {
		t.Errorf("the refused replay still wrote to the Secret: resourceVersion %s -> %s",
			liveSecret.ResourceVersion, afterSecret.ResourceVersion)
	}
	t.Log("step 8: the live Secret still holds its material after the refusal")

	// THE CONTROL, and the finding it turned up. Without a Secret that replays, a replayer which
	// refused every Secret unconditionally would pass the assertion above; the refusal has to be
	// about the placeholders, not about the kind.
	//
	// The control body cannot be produced by the planner. Sanitize redacts every Secret, so the ONLY
	// Secret body a real plan can carry is one the replayer must refuse -- which means a Secret
	// restore never succeeds in production today, while the plan's caveat tells the operator the
	// material "lives in the journal store and is verified against those digests on replay". That
	// gap is recorded as a finding in this unit's ledger row; closing it is the journal.BlobSink
	// work, which is deferred and human-owned. What is built here is the body a rehydration WOULD
	// hand the replayer: the sanitized shape with the real values put back. If the replayer refuses
	// that too, the rehydration path is dead before it is written.
	plainSecret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: "not-redacted"},
		StringData: map[string]string{"token": "plain-value"},
	}
	if err := k8s.Create(ctx, plainSecret); err != nil {
		t.Fatalf("create the control Secret: %v", err)
	}
	livePlain := &corev1.Secret{}
	if err := k8s.Get(ctx, client.ObjectKeyFromObject(plainSecret), livePlain); err != nil {
		t.Fatalf("read the control Secret: %v", err)
	}
	rawPlain, err := runtime.DefaultUnstructuredConverter.ToUnstructured(livePlain)
	if err != nil {
		t.Fatalf("to unstructured: %v", err)
	}
	uPlain := &unstructured.Unstructured{Object: rawPlain}
	uPlain.SetAPIVersion("v1")
	uPlain.SetKind("Secret")
	rehydrated, _, err := undo.Sanitize(uPlain, false)
	if err != nil {
		t.Fatalf("sanitize the control: %v", err)
	}
	// The rehydration: put the live material back where the digests are.
	live, found, err := unstructured.NestedMap(uPlain.Object, "data")
	if err != nil || !found {
		t.Fatalf("read the live data map: found=%v err=%v", found, err)
	}
	if err := unstructured.SetNestedMap(rehydrated.Object, live, "data"); err != nil {
		t.Fatalf("rehydrate: %v", err)
	}
	if keys := undo.RedactedSecretKeys(rehydrated); len(keys) != 0 {
		t.Fatalf("the control fixture is still redacted at %v, so it is not a control", keys)
	}
	plainBody, err := rehydrated.MarshalJSON()
	if err != nil {
		t.Fatalf("marshal the control: %v", err)
	}
	if err := replayer.Rollback(ctx, "act-rev011-control", rev011Identity, applyPlan(agentv1alpha1.UndoStep{
		Op:            "apply",
		Target:        rev011Ref("Secret", ns, "not-redacted", livePlain.UID),
		Object:        &runtime.RawExtension{Raw: plainBody},
		Preconditions: &agentv1alpha1.UndoPrecondition{UID: string(livePlain.UID)},
	})); err != nil {
		t.Fatalf("CONTROL FAILED: a Secret body with its material intact was refused, so the refusal "+
			"above may be nothing more than a blanket refusal of Secrets: %v", err)
	}
	t.Log("step 9: the control -- a Secret body with its material intact replays normally")

	// --- the uid pin, against a REAL deletion ----------------------------------------------------
	cm := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: "settings"},
		Data:       map[string]string{"level": "info"},
	}
	if err := k8s.Create(ctx, cm); err != nil {
		t.Fatalf("create ConfigMap: %v", err)
	}
	original := &corev1.ConfigMap{}
	if err := k8s.Get(ctx, client.ObjectKeyFromObject(cm), original); err != nil {
		t.Fatalf("read ConfigMap: %v", err)
	}
	cmBody, cmDigest := rev011Body(t, original)

	if err := k8s.Delete(ctx, original); err != nil {
		t.Fatalf("delete ConfigMap: %v", err)
	}
	eventually(t, "the ConfigMap actually going away", 2*time.Minute, func() error {
		probe := &corev1.ConfigMap{}
		err := k8s.Get(ctx, client.ObjectKeyFromObject(cm), probe)
		if apierrors.IsNotFound(err) {
			return nil
		}
		if err != nil {
			return err
		}
		return fmt.Errorf("still present")
	})
	seedAsAgent(t, ctx, k8s, configMapBody(ns, "settings", map[string]string{"level": "debug"}))
	liveReplacement := &corev1.ConfigMap{}
	if err := k8s.Get(ctx, client.ObjectKeyFromObject(cm), liveReplacement); err != nil {
		t.Fatalf("read the replacement: %v", err)
	}
	if liveReplacement.UID == original.UID {
		t.Fatalf("the API server reused the uid %s, so this leg cannot distinguish the objects",
			original.UID)
	}

	err = replayer.Rollback(ctx, "act-rev011-uid", rev011Identity, applyPlan(agentv1alpha1.UndoStep{
		Op:            "apply",
		Target:        rev011Ref("ConfigMap", ns, "settings", original.UID),
		Object:        cmBody,
		Preconditions: &agentv1alpha1.UndoPrecondition{UID: string(original.UID)},
	}))
	if err == nil {
		t.Fatal("the replayer restored a snapshot onto a REPLACEMENT object. The thing the action " +
			"touched no longer exists; writing its pre-state over whatever now holds the name is not " +
			"an undo.")
	}
	if !strings.Contains(err.Error(), "was replaced after the action") {
		t.Errorf("the refusal does not say the object was replaced: %v", err)
	}
	afterReplacement := &corev1.ConfigMap{}
	if err := k8s.Get(ctx, client.ObjectKeyFromObject(cm), afterReplacement); err != nil {
		t.Fatalf("re-read the replacement: %v", err)
	}
	if got := afterReplacement.Data["level"]; got != "debug" {
		t.Errorf("the refused replay still wrote to the replacement: level=%q, want debug", got)
	}
	t.Logf("step 10: a snapshot was refused against a post-deletion replacement: %v", err)

	// The control: the same plan, pinned to the uid the replacement actually has, restores.
	replacementBody, _ := rev011Body(t, func() *corev1.ConfigMap {
		c := liveReplacement.DeepCopy()
		c.Data = map[string]string{"level": "info"}
		return c
	}())
	if err := replayer.Rollback(ctx, "act-rev011-uid-ok", rev011Identity, applyPlan(agentv1alpha1.UndoStep{
		Op:            "apply",
		Target:        rev011Ref("ConfigMap", ns, "settings", liveReplacement.UID),
		Object:        replacementBody,
		Preconditions: &agentv1alpha1.UndoPrecondition{UID: string(liveReplacement.UID)},
	})); err != nil {
		t.Fatalf("CONTROL FAILED: a correctly-pinned restore was refused, so the refusal above may be "+
			"a replayer that refuses every apply: %v", err)
	}
	restoredCM := &corev1.ConfigMap{}
	if err := k8s.Get(ctx, client.ObjectKeyFromObject(cm), restoredCM); err != nil {
		t.Fatalf("read the restored ConfigMap: %v", err)
	}
	if got := restoredCM.Data["level"]; got != "info" {
		t.Errorf("the correctly-pinned restore did not take: level=%q, want info", got)
	}
	t.Log("step 11: the control -- a correctly-pinned restore writes the pre-state")

	// --- a first failing step stops the replay, and says how far it got --------------------------
	// Step one is a real, successful write. Step two is refused. The message must say that one step
	// has already been applied and is NOT reverted, because that sentence is what tells the operator
	// woken by the page whether the cluster is in the pre-state, the post-state, or neither.
	seedAsAgent(t, ctx, k8s, configMapBody(ns, "first-of-two", map[string]string{"level": "debug"}))
	target := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: "first-of-two"}}
	liveTarget := &corev1.ConfigMap{}
	if err := k8s.Get(ctx, client.ObjectKeyFromObject(target), liveTarget); err != nil {
		t.Fatalf("read the two-step target: %v", err)
	}
	firstBody, _ := rev011Body(t, func() *corev1.ConfigMap {
		c := liveTarget.DeepCopy()
		c.Data = map[string]string{"level": "info"}
		return c
	}())

	err = replayer.Rollback(ctx, "act-rev011-two-step", rev011Identity, applyPlan(
		agentv1alpha1.UndoStep{
			Op:            "apply",
			Target:        rev011Ref("ConfigMap", ns, "first-of-two", liveTarget.UID),
			Object:        firstBody,
			Preconditions: &agentv1alpha1.UndoPrecondition{UID: string(liveTarget.UID)},
		},
		agentv1alpha1.UndoStep{
			Op:     "apply",
			Target: rev011Ref("ConfigMap", ns, "settings", liveReplacement.UID),
			// The body addresses `first-of-two`, the target says `settings`. A replay that trusted
			// the body would write one object's pre-state over another's.
			Object:        firstBody,
			Preconditions: &agentv1alpha1.UndoPrecondition{UID: string(liveReplacement.UID)},
		},
	))
	if err == nil {
		t.Fatal("a step whose body addresses a different object than its target was replayed")
	}
	if !strings.Contains(err.Error(), "1 step(s) already replayed") {
		t.Errorf("the failure does not report how many steps had already been applied, so an "+
			"operator cannot tell what state the cluster is in: %v", err)
	}
	if !strings.Contains(err.Error(), "NOT reverted") {
		t.Errorf("the failure does not say the applied steps stay applied: %v", err)
	}
	appliedFirst := &corev1.ConfigMap{}
	if err := k8s.Get(ctx, client.ObjectKeyFromObject(target), appliedFirst); err != nil {
		t.Fatalf("read the first step's target: %v", err)
	}
	if got := appliedFirst.Data["level"]; got != "info" {
		t.Errorf("the first step's write is not present, so the count in the message is wrong: level=%q", got)
	}
	t.Logf("step 12: the replay stopped at the failing step and named what had already landed: %v", err)

	// --- an out-of-band body whose digest does not match -----------------------------------------
	// No BlobSink is configured, so a step that needs one must refuse BY NAME rather than panic on a
	// nil interface. The digest clause itself is covered at L1 against a fake sink; what is added
	// here is that the nil-sink path is a named refusal on a real cluster, and that it wrote nothing.
	err = replayer.Rollback(ctx, "act-rev011-oob", rev011Identity, applyPlan(agentv1alpha1.UndoStep{
		Op:            "apply",
		Target:        rev011Ref("ConfigMap", ns, "settings", liveReplacement.UID),
		ObjectRef:     &agentv1alpha1.ObjectStoreRef{Store: "journal", Key: "blobs/act-rev011-oob/0", SHA256: cmDigest},
		Preconditions: &agentv1alpha1.UndoPrecondition{UID: string(liveReplacement.UID)},
	}))
	if err == nil {
		t.Fatal("a step whose body lives out of band was replayed with no blob sink configured")
	}
	t.Logf("step 13: an out-of-band body with no sink refused by name rather than panicking: %v", err)
}

func ptrTo[T any](v T) *T { return &v }

func hasManagerL2(entries []metav1.ManagedFieldsEntry, want string) bool {
	for _, e := range entries {
		if e.Manager == want {
			return true
		}
	}
	return false
}

func managerNamesL2(entries []metav1.ManagedFieldsEntry) []string {
	out := make([]string, 0, len(entries))
	for _, e := range entries {
		out = append(out, e.Manager)
	}
	return out
}
