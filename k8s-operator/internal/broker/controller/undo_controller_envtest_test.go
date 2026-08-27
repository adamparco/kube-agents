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
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/controller"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

// C-UC against a real API server (05 §1.3).
//
// The fake client in undo_controller_test.go proves the control flow. It cannot prove the part that
// bites in production, because it does not run the schema: a Condition reason that is not CamelCase,
// a message past 1024 characters, or an undoActionId that is not a ULID are all silently accepted by
// the fake and REJECTED by a real API server. Each of those turns a clean outcome into a request
// stuck in Executing with the real reason only in a controller log -- which is the state a human is
// staring at while trying to undo something.
//
// So everything below drives the actual reconciler against an actual apiserver, and the assertions
// are on what came back out of etcd rather than on what the controller thought it wrote.

const (
	envtestUndoNS       = "undo-envtest"
	envtestOrigActionID = "01J0000000000000000000000A"
	envtestUndoActionID = "01J0000000000000000000000B"
)

var envtestUndoNow = time.Date(2026, 7, 27, 12, 0, 0, 0, time.UTC)

type envtestReplayer struct {
	id    string
	err   error
	calls int
}

func (e *envtestReplayer) Replay(context.Context, *agentv1alpha1.ActionRecord, *agentv1alpha1.UndoRequest) (string, error) {
	e.calls++
	return e.id, e.err
}

func undoEnv(t *testing.T) (client.Client, context.Context) {
	t.Helper()
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		t.Skip("KUBEBUILDER_ASSETS unset; run via `make test` to exercise the undo controller against a real API server")
	}

	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		t.Fatalf("add clientgo scheme: %v", err)
	}
	if err := agentv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("add kube-agents scheme: %v", err)
	}

	testEnv := &envtest.Environment{
		CRDDirectoryPaths:     []string{filepath.Join("..", "..", "..", "config", "crd", "bases")},
		ErrorIfCRDPathMissing: true,
		Scheme:                scheme,
	}
	cfg, err := testEnv.Start()
	if err != nil {
		t.Fatalf("start envtest: %v", err)
	}
	t.Cleanup(func() { _ = testEnv.Stop() })

	k8s, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatalf("new client: %v", err)
	}
	ctx := context.Background()
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: envtestUndoNS}}
	if err := k8s.Create(ctx, ns); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("create namespace: %v", err)
	}
	return k8s, ctx
}

// envtestRecord creates a verified, undoable ActionRecord and brings its status up in a second
// write, because status is a subresource and Create ignores it.
func envtestRecord(t *testing.T, ctx context.Context, k8s client.Client, actionID string, mutate func(*agentv1alpha1.ActionRecord)) *agentv1alpha1.ActionRecord {
	t.Helper()
	ar := &agentv1alpha1.ActionRecord{
		ObjectMeta: metav1.ObjectMeta{Name: journal.RecordName(actionID), Namespace: envtestUndoNS},
		Spec: agentv1alpha1.ActionRecordSpec{
			ActionID:            actionID,
			AgentRef:            agentv1alpha1.AgentObjectRef{Name: "team-x-agent", Namespace: envtestUndoNS},
			AgentIdentity:       "developer-team/proj/cluster-a/team-x",
			ActorServiceAccount: "developer-team-team-x-actor",
			Requester: agentv1alpha1.ActionRequester{
				Kind: "human", ID: "alice@example.com", Platform: "k8s",
			},
			Trigger:        agentv1alpha1.ActionTrigger{Source: "chat", ChainID: actionID},
			Intent:         "scale the api deployment to 5",
			IdempotencyKey: idempotencyKeyFor(actionID),
			Classification: agentv1alpha1.ActionClassification{
				Class:    agentv1alpha1.RiskRoutine,
				Reasons:  []agentv1alpha1.ClassificationReason{{Rule: "scale", Class: "routine"}},
				Undoable: true,
			},
			Targets: []agentv1alpha1.TargetRef{{
				Group: "apps", Version: "v1", Kind: "Deployment", Namespace: envtestUndoNS, Name: "api",
			}},
			Undo: &agentv1alpha1.UndoPlan{
				Strategy:    agentv1alpha1.UndoRestore,
				GeneratedAt: metav1.NewTime(envtestUndoNow.Add(-time.Minute)),
				Validated:   true,
				Steps: []agentv1alpha1.UndoStep{{
					Op:     "apply",
					Target: agentv1alpha1.TargetRef{Group: "apps", Version: "v1", Kind: "Deployment", Namespace: envtestUndoNS, Name: "api"},
					Object: &runtime.RawExtension{Raw: []byte(`{"apiVersion":"apps/v1","kind":"Deployment","metadata":{"name":"api"}}`)},
				}},
			},
			Retention: agentv1alpha1.RetentionSpec{
				Class:               agentv1alpha1.RiskRoutine,
				TTL:                 "720h",
				ExpiresAt:           metav1.NewTime(envtestUndoNow.Add(720 * time.Hour)),
				UndoWindow:          "1h",
				UndoWindowExpiresAt: metav1.NewTime(envtestUndoNow.Add(time.Hour)),
			},
		},
	}
	if mutate != nil {
		mutate(ar)
	}
	// Capture the wanted status BEFORE Create. Create writes only spec -- status is a subresource
	// and the API server drops it -- so a mutate() that set a phase would otherwise be silently
	// discarded and every record would come back Verified. That is the failure this helper exists
	// to make impossible: a refusal test that quietly tests the happy path instead.
	want := *ar.Status.DeepCopy()
	if want.Phase == "" {
		want.Phase = agentv1alpha1.PhaseVerified
	}
	if err := k8s.Create(ctx, ar); err != nil {
		t.Fatalf("create ActionRecord %s: %v", actionID, err)
	}
	t.Cleanup(func() { _ = k8s.Delete(ctx, ar) })

	want.DeepCopyInto(&ar.Status)
	if err := k8s.Status().Update(ctx, ar); err != nil {
		t.Fatalf("set ActionRecord %s status: %v", actionID, err)
	}
	if ar.Status.Phase != want.Phase {
		t.Fatalf("the API server did not persist phase %q; got %q", want.Phase, ar.Status.Phase)
	}
	return ar
}

// idempotencyKeyFor produces the `sha256:<64 hex>` the CRD requires. A real digest rather than a
// constant so two records in one test are distinguishable in an API-server error message.
func idempotencyKeyFor(actionID string) string {
	sum := sha256.Sum256([]byte(actionID))
	return "sha256:" + hex.EncodeToString(sum[:])
}

func envtestUndoRequest(t *testing.T, ctx context.Context, k8s client.Client, name, actionID string, mutate func(*agentv1alpha1.UndoRequest)) *agentv1alpha1.UndoRequest {
	t.Helper()
	ur := &agentv1alpha1.UndoRequest{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: envtestUndoNS},
		Spec: agentv1alpha1.UndoRequestSpec{
			ActionRef:   agentv1alpha1.ActionRef{Name: journal.RecordName(actionID)},
			Reason:      "the scale-up made the noisy-neighbour problem worse",
			RequestedBy: "k8s:alice@example.com",
		},
	}
	if mutate != nil {
		mutate(ur)
	}
	if err := k8s.Create(ctx, ur); err != nil {
		t.Fatalf("create UndoRequest %s: %v", name, err)
	}
	t.Cleanup(func() { _ = k8s.Delete(ctx, ur) })
	return ur
}

func undoReconciler(k8s client.Client, rp controller.Replayer) *controller.UndoReconciler {
	return &controller.UndoReconciler{
		Client:   k8s,
		Scheme:   k8s.Scheme(),
		Replayer: rp,
		Now:      func() time.Time { return envtestUndoNow },
	}
}

func reconcileUndo(t *testing.T, ctx context.Context, r *controller.UndoReconciler, name string) (ctrl.Result, error) {
	t.Helper()
	return r.Reconcile(ctx, ctrl.Request{NamespacedName: types.NamespacedName{Namespace: envtestUndoNS, Name: name}})
}

// ---------------------------------------------------------------------------------------------

// The whole of 05 §1.3 steps 2, 4 and 5, end to end, with every assertion read back from etcd.
func TestUndoControllerAgainstARealAPIServer(t *testing.T) {
	k8s, ctx := undoEnv(t)

	dep := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: "api", Namespace: envtestUndoNS},
		Spec: appsv1.DeploymentSpec{
			Selector: &metav1.LabelSelector{MatchLabels: map[string]string{"app": "api"}},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: map[string]string{"app": "api"}},
				Spec:       corev1.PodSpec{Containers: []corev1.Container{{Name: "api", Image: "example.com/api:1"}}},
			},
		},
	}
	if err := k8s.Create(ctx, dep); err != nil {
		t.Fatalf("create target deployment: %v", err)
	}
	t.Cleanup(func() { _ = k8s.Delete(ctx, dep) })

	envtestRecord(t, ctx, k8s, envtestOrigActionID, nil)
	envtestUndoRequest(t, ctx, k8s, "u-happy", envtestOrigActionID, nil)

	rp := &envtestReplayer{id: envtestUndoActionID}
	if _, err := reconcileUndo(t, ctx, undoReconciler(k8s, rp), "u-happy"); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	var ur agentv1alpha1.UndoRequest
	if err := k8s.Get(ctx, types.NamespacedName{Namespace: envtestUndoNS, Name: "u-happy"}, &ur); err != nil {
		t.Fatalf("get UndoRequest: %v", err)
	}
	// The API server accepted the status write. That is the assertion: every Condition reason is
	// CamelCase, the message fits, and undoActionId matched the ULID pattern.
	if ur.Status.Phase != agentv1alpha1.UndoExecuted {
		t.Fatalf("phase = %q, want %q (message: %s)", ur.Status.Phase, agentv1alpha1.UndoExecuted, ur.Status.Message)
	}
	if ur.Status.UndoActionID != envtestUndoActionID {
		t.Errorf("undoActionId = %q, want %q", ur.Status.UndoActionID, envtestUndoActionID)
	}
	if len(ur.Status.Conditions) != 3 {
		t.Errorf("want all three conditions persisted, got %d", len(ur.Status.Conditions))
	}
	for _, ct := range []string{controller.UndoConditionReplayable, controller.UndoConditionExecuted, controller.UndoConditionLinkPending} {
		if meta.FindStatusCondition(ur.Status.Conditions, ct) == nil {
			t.Errorf("condition %s missing from the persisted object", ct)
		}
	}
	if meta.IsStatusConditionTrue(ur.Status.Conditions, controller.UndoConditionLinkPending) {
		t.Error("UndoLinkPending must be false once the reverse link is written")
	}

	var ar agentv1alpha1.ActionRecord
	if err := k8s.Get(ctx, types.NamespacedName{Namespace: envtestUndoNS, Name: journal.RecordName(envtestOrigActionID)}, &ar); err != nil {
		t.Fatalf("get ActionRecord: %v", err)
	}
	if ar.Status.UndoneBy != envtestUndoActionID {
		t.Errorf("status.undoneBy = %q, want %q", ar.Status.UndoneBy, envtestUndoActionID)
	}
	if ar.Status.Phase != agentv1alpha1.PhaseUndone {
		t.Errorf("original phase = %q, want %q", ar.Status.Phase, agentv1alpha1.PhaseUndone)
	}
	if !ar.Status.Contested {
		t.Error("status.contested must be set: without it the agent redoes the change on its next reconcile")
	}

	var got appsv1.Deployment
	if err := k8s.Get(ctx, types.NamespacedName{Namespace: envtestUndoNS, Name: "api"}, &got); err != nil {
		t.Fatalf("get deployment: %v", err)
	}
	if got.Annotations[journal.ContestedAnnotation] != envtestOrigActionID {
		t.Errorf("advisory annotation %s = %q, want %q",
			journal.ContestedAnnotation, got.Annotations[journal.ContestedAnnotation], envtestOrigActionID)
	}
	// The stamp must be additive. A merge patch that replaced the annotation map would drop
	// kubectl.kubernetes.io/last-applied-configuration and every other operator's bookkeeping.
	if got.Spec.Template.Spec.Containers[0].Image != "example.com/api:1" {
		t.Error("stamping an annotation must not disturb the object it is stamped on")
	}
}

// The undo controller writes to fields the CRD marks as its own (06 §2). If a status write is
// rejected the request sticks, so each refusal path is driven through the real schema too.
func TestUndoControllerRefusalsPersist(t *testing.T) {
	k8s, ctx := undoEnv(t)

	cases := []struct {
		name       string
		actionID   string
		mutate     func(*agentv1alpha1.ActionRecord)
		create     bool
		wantReason string
	}{
		{
			name: "not executed", actionID: "01J000000000000000000000C0", create: true,
			mutate:     func(ar *agentv1alpha1.ActionRecord) { ar.Status.Phase = agentv1alpha1.PhaseFailed },
			wantReason: "ActionNotExecuted",
		},
		{
			name: "window expired", actionID: "01J000000000000000000000C1", create: true,
			mutate: func(ar *agentv1alpha1.ActionRecord) {
				ar.Spec.Retention.UndoWindowExpiresAt = metav1.NewTime(envtestUndoNow.Add(-time.Second))
			},
			wantReason: "UndoWindowExpired",
		},
		{
			name: "plan unusable", actionID: "01J000000000000000000000C2", create: true,
			mutate:     func(ar *agentv1alpha1.ActionRecord) { ar.Spec.Undo.Validated = false },
			wantReason: "UndoPlanUnusable",
		},
		{
			name: "missing record", actionID: "01J000000000000000000000C3", create: false,
			wantReason: "ActionRecordMissing",
		},
	}

	for i, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if tc.create {
				envtestRecord(t, ctx, k8s, tc.actionID, tc.mutate)
			}
			name := "u-refuse-" + string(rune('a'+i))
			envtestUndoRequest(t, ctx, k8s, name, tc.actionID, nil)

			rp := &envtestReplayer{id: envtestUndoActionID}
			if _, err := reconcileUndo(t, ctx, undoReconciler(k8s, rp), name); err != nil {
				t.Fatalf("a refusal is not an error: %v", err)
			}
			if rp.calls != 0 {
				t.Errorf("a refused undo must never reach the broker; replayer called %d times", rp.calls)
			}

			var ur agentv1alpha1.UndoRequest
			if err := k8s.Get(ctx, types.NamespacedName{Namespace: envtestUndoNS, Name: name}, &ur); err != nil {
				t.Fatalf("get UndoRequest: %v", err)
			}
			if ur.Status.Phase != agentv1alpha1.UndoRefused {
				t.Fatalf("phase = %q, want %q", ur.Status.Phase, agentv1alpha1.UndoRefused)
			}
			cond := meta.FindStatusCondition(ur.Status.Conditions, controller.UndoConditionReplayable)
			if cond == nil {
				t.Fatal("the API server persisted no Replayable condition; if the reason were not CamelCase the whole write would have been rejected")
			}
			if cond.Reason != tc.wantReason {
				t.Errorf("reason = %q, want %q", cond.Reason, tc.wantReason)
			}
			if cond.Status != metav1.ConditionFalse {
				t.Errorf("Replayable = %q, want False", cond.Status)
			}
		})
	}
}

// A build with no replayer must not produce a record that says an undo happened. Driven here as
// well as against the fake because the failure mode is a status write, and a status write that the
// API server rejects looks identical to one the controller never made.
func TestUndoControllerWithNoReplayerWritesNothingToTheOriginal(t *testing.T) {
	k8s, ctx := undoEnv(t)
	const actionID = "01J000000000000000000000D0"
	envtestRecord(t, ctx, k8s, actionID, nil)
	envtestUndoRequest(t, ctx, k8s, "u-noreplayer", actionID, nil)

	_, err := reconcileUndo(t, ctx, undoReconciler(k8s, nil), "u-noreplayer")
	if err == nil {
		t.Fatal("a missing replayer must surface as an error")
	}
	if !errors.Is(err, controller.ErrReplayerUnavailable) {
		t.Errorf("err = %v, want it to wrap ErrReplayerUnavailable", err)
	}

	var ar agentv1alpha1.ActionRecord
	if err := k8s.Get(ctx, types.NamespacedName{Namespace: envtestUndoNS, Name: journal.RecordName(actionID)}, &ar); err != nil {
		t.Fatalf("get ActionRecord: %v", err)
	}
	if ar.Status.Phase != agentv1alpha1.PhaseVerified || ar.Status.UndoneBy != "" || ar.Status.Contested {
		t.Errorf("the original must be untouched: phase=%q undoneBy=%q contested=%v",
			ar.Status.Phase, ar.Status.UndoneBy, ar.Status.Contested)
	}
	var ur agentv1alpha1.UndoRequest
	if err := k8s.Get(ctx, types.NamespacedName{Namespace: envtestUndoNS, Name: "u-noreplayer"}, &ur); err != nil {
		t.Fatalf("get UndoRequest: %v", err)
	}
	if ur.Status.Phase != agentv1alpha1.UndoExecuting {
		t.Errorf("phase = %q, want %q so the next reconcile retries", ur.Status.Phase, agentv1alpha1.UndoExecuting)
	}
}

// markContested: false is the "the change was right, the timing was wrong" case. It suppresses the
// marker and the annotation, and nothing else.
func TestUndoControllerHonoursMarkContestedFalse(t *testing.T) {
	k8s, ctx := undoEnv(t)
	const actionID = "01J000000000000000000000E0"

	dep := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: "api", Namespace: envtestUndoNS},
		Spec: appsv1.DeploymentSpec{
			Selector: &metav1.LabelSelector{MatchLabels: map[string]string{"app": "api"}},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: map[string]string{"app": "api"}},
				Spec:       corev1.PodSpec{Containers: []corev1.Container{{Name: "api", Image: "example.com/api:1"}}},
			},
		},
	}
	if err := k8s.Create(ctx, dep); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("create target deployment: %v", err)
	}
	t.Cleanup(func() { _ = k8s.Delete(ctx, dep) })

	envtestRecord(t, ctx, k8s, actionID, nil)
	envtestUndoRequest(t, ctx, k8s, "u-nocontest", actionID, func(u *agentv1alpha1.UndoRequest) {
		u.Spec.MarkContested = ptr.To(false)
	})

	if _, err := reconcileUndo(t, ctx, undoReconciler(k8s, &envtestReplayer{id: envtestUndoActionID}), "u-nocontest"); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	var ar agentv1alpha1.ActionRecord
	if err := k8s.Get(ctx, types.NamespacedName{Namespace: envtestUndoNS, Name: journal.RecordName(actionID)}, &ar); err != nil {
		t.Fatalf("get ActionRecord: %v", err)
	}
	if ar.Status.Contested {
		t.Error("markContested: false must leave the record uncontested")
	}
	if ar.Status.UndoneBy != envtestUndoActionID {
		t.Errorf("the linkage is not optional; undoneBy = %q", ar.Status.UndoneBy)
	}

	var got appsv1.Deployment
	if err := k8s.Get(ctx, types.NamespacedName{Namespace: envtestUndoNS, Name: "api"}, &got); err != nil {
		t.Fatalf("get deployment: %v", err)
	}
	if _, ok := got.Annotations[journal.ContestedAnnotation]; ok {
		t.Error("markContested: false must not stamp the advisory annotation")
	}
}

// A second undo of the same action is refused rather than executed twice. This is the property that
// makes the reverse link load-bearing: without status.undoneBy the controller has no way to know,
// and two undos of one create would delete an object somebody has since recreated by hand.
func TestUndoControllerRefusesASecondUndoOfTheSameAction(t *testing.T) {
	k8s, ctx := undoEnv(t)
	const actionID = "01J000000000000000000000F0"
	envtestRecord(t, ctx, k8s, actionID, nil)
	envtestUndoRequest(t, ctx, k8s, "u-first", actionID, nil)

	rp := &envtestReplayer{id: envtestUndoActionID}
	if _, err := reconcileUndo(t, ctx, undoReconciler(k8s, rp), "u-first"); err != nil {
		t.Fatalf("first undo: %v", err)
	}

	envtestUndoRequest(t, ctx, k8s, "u-second", actionID, nil)
	if _, err := reconcileUndo(t, ctx, undoReconciler(k8s, rp), "u-second"); err != nil {
		t.Fatalf("second undo: %v", err)
	}
	if rp.calls != 1 {
		t.Fatalf("the replayer was called %d times; the second request must be refused, not executed", rp.calls)
	}

	var ur agentv1alpha1.UndoRequest
	if err := k8s.Get(ctx, types.NamespacedName{Namespace: envtestUndoNS, Name: "u-second"}, &ur); err != nil {
		t.Fatalf("get UndoRequest: %v", err)
	}
	if ur.Status.Phase != agentv1alpha1.UndoRefused {
		t.Fatalf("phase = %q, want %q", ur.Status.Phase, agentv1alpha1.UndoRefused)
	}
	cond := meta.FindStatusCondition(ur.Status.Conditions, controller.UndoConditionReplayable)
	if cond == nil || cond.Reason != "AlreadyUndone" {
		t.Errorf("reason = %+v, want AlreadyUndone", cond)
	}
	if !strings.Contains(ur.Status.Message, envtestUndoActionID) {
		t.Errorf("the refusal must name the undo that already happened; got %q", ur.Status.Message)
	}
}
