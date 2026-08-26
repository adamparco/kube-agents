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

package controller

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/undo"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

// C-UC's tests, and the property they are all circling: an undo either happens and both records say
// so, or it does not happen and the request says why. There is no third outcome, and the third
// outcome is the dangerous one -- an undo that ran while the original record still reads as
// standing, which is what a human consults before deciding whether to re-attempt a fix.

var undoNow = time.Date(2026, 7, 27, 12, 0, 0, 0, time.UTC)

const (
	undoNS       = "team-x"
	origActionID = "01J0000000000000000000000A"
	undoActionID = "01J0000000000000000000000B"
)

func undoScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	s := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(s); err != nil {
		t.Fatalf("add clientgo scheme: %v", err)
	}
	if err := agentv1alpha1.AddToScheme(s); err != nil {
		t.Fatalf("add kube-agents scheme: %v", err)
	}
	return s
}

// undoableRecord is the healthy original: verified, one target, a validated plan, an open window.
func undoableRecord() *agentv1alpha1.ActionRecord {
	return &agentv1alpha1.ActionRecord{
		ObjectMeta: metav1.ObjectMeta{Name: journal.RecordName(origActionID), Namespace: undoNS},
		Spec: agentv1alpha1.ActionRecordSpec{
			ActionID:      origActionID,
			AgentIdentity: "developer-team/proj/cluster-a/team-x",
			Intent:        "scale the api deployment to 5",
			Targets: []agentv1alpha1.TargetRef{{
				Group: "apps", Version: "v1", Kind: "Deployment", Namespace: undoNS, Name: "api",
			}},
			Undo: &agentv1alpha1.UndoPlan{
				Strategy:    agentv1alpha1.UndoRestore,
				GeneratedAt: metav1.NewTime(undoNow.Add(-time.Minute)),
				Validated:   true,
				Steps: []agentv1alpha1.UndoStep{{
					Op:     "apply",
					Target: agentv1alpha1.TargetRef{Group: "apps", Version: "v1", Kind: "Deployment", Namespace: undoNS, Name: "api"},
					Object: &runtime.RawExtension{Raw: []byte(`{"apiVersion":"apps/v1","kind":"Deployment"}`)},
				}},
			},
			Retention: agentv1alpha1.RetentionSpec{
				TTL:                 "720h",
				ExpiresAt:           metav1.NewTime(undoNow.Add(720 * time.Hour)),
				UndoWindow:          "1h",
				UndoWindowExpiresAt: metav1.NewTime(undoNow.Add(time.Hour)),
			},
		},
		Status: agentv1alpha1.ActionRecordStatus{Phase: agentv1alpha1.PhaseVerified},
	}
}

func undoRequest(name string, mutate func(*agentv1alpha1.UndoRequest)) *agentv1alpha1.UndoRequest {
	u := &agentv1alpha1.UndoRequest{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: undoNS, Generation: 1},
		Spec: agentv1alpha1.UndoRequestSpec{
			ActionRef:   agentv1alpha1.ActionRef{Name: journal.RecordName(origActionID)},
			Reason:      "the scale-up made the noisy-neighbour problem worse",
			RequestedBy: "k8s:alice@example.com",
		},
	}
	if mutate != nil {
		mutate(u)
	}
	return u
}

// stubReplayer records what it was asked and answers with whatever the test wants.
type stubReplayer struct {
	id    string
	err   error
	calls int
	sawAR string
	sawUR string
}

func (s *stubReplayer) Replay(_ context.Context, ar *agentv1alpha1.ActionRecord, ur *agentv1alpha1.UndoRequest) (string, error) {
	s.calls++
	s.sawAR = ar.Spec.ActionID
	s.sawUR = ur.Name
	return s.id, s.err
}

type undoFixture struct {
	c        client.Client
	r        *UndoReconciler
	replayer *stubReplayer
	ctx      context.Context
}

func newUndoFixture(t *testing.T, replayer *stubReplayer, objs ...client.Object) *undoFixture {
	return newUndoFixtureWith(t, replayer, interceptor.Funcs{}, objs...)
}

func newUndoFixtureWith(t *testing.T, replayer *stubReplayer, funcs interceptor.Funcs, objs ...client.Object) *undoFixture {
	t.Helper()
	s := undoScheme(t)
	c := fake.NewClientBuilder().
		WithScheme(s).
		WithStatusSubresource(&agentv1alpha1.UndoRequest{}, &agentv1alpha1.ActionRecord{}).
		WithInterceptorFuncs(funcs).
		WithObjects(objs...).
		Build()
	var rp Replayer
	if replayer != nil {
		rp = replayer
	}
	return &undoFixture{
		c:        c,
		r:        &UndoReconciler{Client: c, Scheme: s, Replayer: rp, Now: func() time.Time { return undoNow }},
		replayer: replayer,
		ctx:      context.Background(),
	}
}

func (f *undoFixture) reconcile(t *testing.T, name string) (ctrl.Result, error) {
	t.Helper()
	return f.r.Reconcile(f.ctx, ctrl.Request{NamespacedName: types.NamespacedName{Namespace: undoNS, Name: name}})
}

func (f *undoFixture) getUR(t *testing.T, name string) *agentv1alpha1.UndoRequest {
	t.Helper()
	var u agentv1alpha1.UndoRequest
	if err := f.c.Get(f.ctx, types.NamespacedName{Namespace: undoNS, Name: name}, &u); err != nil {
		t.Fatalf("get UndoRequest %s: %v", name, err)
	}
	return &u
}

func (f *undoFixture) getAR(t *testing.T) *agentv1alpha1.ActionRecord {
	t.Helper()
	var ar agentv1alpha1.ActionRecord
	if err := f.c.Get(f.ctx, types.NamespacedName{Namespace: undoNS, Name: journal.RecordName(origActionID)}, &ar); err != nil {
		t.Fatalf("get ActionRecord: %v", err)
	}
	return &ar
}

// ---------------------------------------------------------------------------------------------
// The happy path, and the bidirectional linkage it must leave behind
// ---------------------------------------------------------------------------------------------

func TestUndoHappyPathWritesBothHalvesOfTheLinkage(t *testing.T) {
	f := newUndoFixture(t, &stubReplayer{id: undoActionID}, undoableRecord(), undoRequest("u1", nil))

	if _, err := f.reconcile(t, "u1"); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	ur := f.getUR(t, "u1")
	if ur.Status.Phase != agentv1alpha1.UndoExecuted {
		t.Fatalf("phase = %q, want %q (message: %s)", ur.Status.Phase, agentv1alpha1.UndoExecuted, ur.Status.Message)
	}
	if ur.Status.UndoActionID != undoActionID {
		t.Errorf("undoActionId = %q, want %q", ur.Status.UndoActionID, undoActionID)
	}
	if ur.Status.CompletionTime == nil {
		t.Error("a terminal phase must carry a completionTime")
	}
	if ur.Status.ObservedGeneration != 1 {
		t.Errorf("observedGeneration = %d, want 1", ur.Status.ObservedGeneration)
	}

	// The reverse half, on the original. This is the one a human reads before re-attempting a fix.
	ar := f.getAR(t)
	if ar.Status.UndoneBy != undoActionID {
		t.Errorf("status.undoneBy = %q, want %q", ar.Status.UndoneBy, undoActionID)
	}
	if ar.Status.Phase != agentv1alpha1.PhaseUndone {
		t.Errorf("original phase = %q, want %q", ar.Status.Phase, agentv1alpha1.PhaseUndone)
	}
	if !ar.Status.Contested {
		t.Error("markContested defaults to true, so the original must come back contested")
	}
	if !strings.Contains(ar.Status.Message, "k8s:alice@example.com") {
		t.Errorf("the original's message must attribute the undo; got %q", ar.Status.Message)
	}

	// And the flag that exists to make the two writes atomic-enough must be down again.
	if linkPending(ur) {
		t.Error("UndoLinkPending must be cleared once the reverse link is written")
	}
	if !meta.IsStatusConditionTrue(ur.Status.Conditions, UndoConditionExecuted) {
		t.Error("Executed condition must be true")
	}
	if !meta.IsStatusConditionTrue(ur.Status.Conditions, UndoConditionReplayable) {
		t.Error("Replayable condition must be true")
	}

	if f.replayer.calls != 1 {
		t.Errorf("replayer called %d times, want 1", f.replayer.calls)
	}
	if f.replayer.sawAR != origActionID {
		t.Errorf("the replayer was handed action %q, want %q", f.replayer.sawAR, origActionID)
	}
}

func TestUndoIsIdempotentOnceTerminal(t *testing.T) {
	f := newUndoFixture(t, &stubReplayer{id: undoActionID}, undoableRecord(), undoRequest("u1", nil))
	for i := 0; i < 4; i++ {
		if _, err := f.reconcile(t, "u1"); err != nil {
			t.Fatalf("reconcile %d: %v", i, err)
		}
	}
	if f.replayer.calls != 1 {
		t.Fatalf("the replayer was called %d times across four reconciles; an undo must not be replayed once it is terminal", f.replayer.calls)
	}
}

func TestUndoMarkContestedFalseLeavesTheRecordUncontested(t *testing.T) {
	f := newUndoFixture(t, &stubReplayer{id: undoActionID}, undoableRecord(),
		undoRequest("u1", func(u *agentv1alpha1.UndoRequest) { u.Spec.MarkContested = ptr.To(false) }))

	if _, err := f.reconcile(t, "u1"); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	ar := f.getAR(t)
	if ar.Status.Contested {
		t.Error("markContested: false must not contest the target -- that is the 'the change was right, the timing was wrong' case")
	}
	// The undo itself still happened and is still linked. Opting out of the marker is not opting
	// out of the audit trail.
	if ar.Status.UndoneBy != undoActionID || ar.Status.Phase != agentv1alpha1.PhaseUndone {
		t.Errorf("the linkage is not optional: undoneBy=%q phase=%q", ar.Status.UndoneBy, ar.Status.Phase)
	}
}

// ---------------------------------------------------------------------------------------------
// Refusals -- every one of them terminal, none of them a retry
// ---------------------------------------------------------------------------------------------

func TestUndoRefusals(t *testing.T) {
	cases := []struct {
		name       string
		record     func() *agentv1alpha1.ActionRecord // nil means do not create one
		wantReason string
	}{
		{"missing record", nil, "ActionRecordMissing"},
		{
			"not executed",
			func() *agentv1alpha1.ActionRecord {
				ar := undoableRecord()
				ar.Status.Phase = agentv1alpha1.PhaseFailed
				return ar
			},
			"ActionNotExecuted",
		},
		{
			"already undone",
			func() *agentv1alpha1.ActionRecord {
				ar := undoableRecord()
				ar.Status.UndoneBy = "01J0000000000000000000000C"
				return ar
			},
			"AlreadyUndone",
		},
		{
			"plan unusable",
			func() *agentv1alpha1.ActionRecord {
				ar := undoableRecord()
				ar.Spec.Undo.Validated = false
				return ar
			},
			"UndoPlanUnusable",
		},
		{
			"window expired",
			func() *agentv1alpha1.ActionRecord {
				ar := undoableRecord()
				ar.Spec.Retention.UndoWindowExpiresAt = metav1.NewTime(undoNow.Add(-time.Second))
				return ar
			},
			"UndoWindowExpired",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			objs := []client.Object{undoRequest("u1", nil)}
			if tc.record != nil {
				objs = append(objs, tc.record())
			}
			f := newUndoFixture(t, &stubReplayer{id: undoActionID}, objs...)

			if _, err := f.reconcile(t, "u1"); err != nil {
				t.Fatalf("a refusal is not an error: %v", err)
			}
			ur := f.getUR(t, "u1")
			if ur.Status.Phase != agentv1alpha1.UndoRefused {
				t.Fatalf("phase = %q, want %q", ur.Status.Phase, agentv1alpha1.UndoRefused)
			}
			cond := meta.FindStatusCondition(ur.Status.Conditions, UndoConditionReplayable)
			if cond == nil {
				t.Fatal("a refusal must record a Replayable condition")
			}
			if cond.Status != metav1.ConditionFalse {
				t.Errorf("Replayable = %q, want False", cond.Status)
			}
			if cond.Reason != tc.wantReason {
				t.Errorf("reason = %q, want %q", cond.Reason, tc.wantReason)
			}
			if ur.Status.Message == "" {
				t.Error("a refusal must say why in status.message; an operator should not need controller logs")
			}
			if f.replayer.calls != 0 {
				t.Errorf("a refused undo must never reach the broker; replayer called %d times", f.replayer.calls)
			}
			// Refused is terminal: a second reconcile changes nothing and still does not replay.
			if _, err := f.reconcile(t, "u1"); err != nil {
				t.Fatalf("second reconcile: %v", err)
			}
			if f.replayer.calls != 0 {
				t.Errorf("a Refused request must not be retried; replayer called %d times", f.replayer.calls)
			}
		})
	}
}

// ---------------------------------------------------------------------------------------------
// The replayer seam
// ---------------------------------------------------------------------------------------------

func TestUndoWithNoReplayerInstalledDoesNotClaimSuccess(t *testing.T) {
	// A build with no replayer is exactly the case a placeholder would have gotten wrong: the
	// tempting implementation writes Executed and logs a TODO, and the journal then says an undo
	// happened that never did.
	f := newUndoFixture(t, nil, undoableRecord(), undoRequest("u1", nil))

	_, err := f.reconcile(t, "u1")
	if err == nil {
		t.Fatal("a missing replayer must surface as an error, not as a silent success")
	}
	if !errors.Is(err, ErrReplayerUnavailable) {
		t.Errorf("error = %v, want it to wrap ErrReplayerUnavailable", err)
	}

	ur := f.getUR(t, "u1")
	if ur.Status.Phase != agentv1alpha1.UndoExecuting {
		t.Errorf("phase = %q; a build problem is not a terminal undo outcome", ur.Status.Phase)
	}
	if ur.Status.UndoActionID != "" {
		t.Errorf("undoActionId = %q, want empty", ur.Status.UndoActionID)
	}
	ar := f.getAR(t)
	if ar.Status.UndoneBy != "" || ar.Status.Phase == agentv1alpha1.PhaseUndone || ar.Status.Contested {
		t.Errorf("nothing may be written to the original when no undo ran: undoneBy=%q phase=%q contested=%v",
			ar.Status.UndoneBy, ar.Status.Phase, ar.Status.Contested)
	}
}

func TestUndoBrokerRefusalIsTerminalAndBrokerErrorIsNot(t *testing.T) {
	t.Run("refused is terminal", func(t *testing.T) {
		f := newUndoFixture(t, &stubReplayer{err: fmt.Errorf("the undo deletes a bound PVC: %w", ErrReplayRefused)},
			undoableRecord(), undoRequest("u1", nil))
		if _, err := f.reconcile(t, "u1"); err != nil {
			t.Fatalf("a terminal refusal is not a controller error: %v", err)
		}
		ur := f.getUR(t, "u1")
		if ur.Status.Phase != agentv1alpha1.UndoFailed {
			t.Fatalf("phase = %q, want %q", ur.Status.Phase, agentv1alpha1.UndoFailed)
		}
		if _, err := f.reconcile(t, "u1"); err != nil {
			t.Fatalf("second reconcile: %v", err)
		}
		if f.replayer.calls != 1 {
			t.Errorf("a broker refusal must not be retried; replayer called %d times", f.replayer.calls)
		}
	})

	t.Run("transient is retried", func(t *testing.T) {
		f := newUndoFixture(t, &stubReplayer{err: errors.New("dial tcp: connection refused")},
			undoableRecord(), undoRequest("u1", nil))
		if _, err := f.reconcile(t, "u1"); err == nil {
			t.Fatal("a transient replay error must surface so controller-runtime backs off and retries")
		}
		ur := f.getUR(t, "u1")
		if ur.Status.Phase != agentv1alpha1.UndoExecuting {
			t.Errorf("phase = %q, want %q so the next reconcile picks it up", ur.Status.Phase, agentv1alpha1.UndoExecuting)
		}
		cond := meta.FindStatusCondition(ur.Status.Conditions, UndoConditionExecuted)
		if cond == nil || cond.Status != metav1.ConditionFalse || !strings.Contains(cond.Message, "connection refused") {
			t.Errorf("the transient reason must be visible on the object, not only in logs; got %+v", cond)
		}
		if _, err := f.reconcile(t, "u1"); err == nil {
			t.Fatal("still failing, so still an error")
		}
		if f.replayer.calls != 2 {
			t.Errorf("a transient failure must be retried; replayer called %d times", f.replayer.calls)
		}
	})
}

func TestUndoReplayerReturningNoActionIDIsAnError(t *testing.T) {
	// An undo with no ActionRecord of its own is the thing 05 §1.3 forbids outright, and it is the
	// shape a half-written replayer produces.
	f := newUndoFixture(t, &stubReplayer{id: ""}, undoableRecord(), undoRequest("u1", nil))
	if _, err := f.reconcile(t, "u1"); err == nil {
		t.Fatal("an empty undo action id must be an error")
	}
	if ar := f.getAR(t); ar.Status.UndoneBy != "" {
		t.Errorf("nothing may be linked to an undo that has no id; got %q", ar.Status.UndoneBy)
	}
}

// ---------------------------------------------------------------------------------------------
// undoLinkPending -- 05 §1.3 step 4's "rather than being left silently one-way"
// ---------------------------------------------------------------------------------------------

func TestUndoLinkPendingSurvivesAFailedReverseWriteAndIsRetried(t *testing.T) {
	fail := true
	funcs := interceptor.Funcs{
		SubResourcePatch: func(ctx context.Context, c client.Client, sub string, obj client.Object, patch client.Patch, opts ...client.SubResourcePatchOption) error {
			if _, isAR := obj.(*agentv1alpha1.ActionRecord); isAR && fail {
				return apierrors.NewInternalError(errors.New("etcd said no"))
			}
			return c.SubResource(sub).Patch(ctx, obj, patch, opts...)
		},
	}
	f := newUndoFixtureWith(t, &stubReplayer{id: undoActionID}, funcs, undoableRecord(), undoRequest("u1", nil))

	res, err := f.reconcile(t, "u1")
	if err == nil {
		t.Fatal("a failed reverse-link write must surface as an error so the reconcile is retried")
	}
	if res.RequeueAfter != undoRequeueAfter {
		t.Errorf("RequeueAfter = %v, want %v: a half-written linkage is not a transient to be exponentially forgotten", res.RequeueAfter, undoRequeueAfter)
	}

	ur := f.getUR(t, "u1")
	if ur.Status.Phase != agentv1alpha1.UndoExecuted {
		t.Fatalf("phase = %q: the undo DID run, so the request is Executed even though the paperwork is not done", ur.Status.Phase)
	}
	if !linkPending(ur) {
		t.Fatal("UndoLinkPending must be true: 05 §1.3 forbids leaving the linkage silently one-way")
	}
	if ar := f.getAR(t); ar.Status.UndoneBy != "" {
		t.Fatalf("the reverse link should not have landed; got %q", ar.Status.UndoneBy)
	}

	// Now the write succeeds. A terminal-looking Executed request must still be picked up, which is
	// the whole reason the flag is durable rather than a local variable.
	fail = false
	if _, err := f.reconcile(t, "u1"); err != nil {
		t.Fatalf("retry reconcile: %v", err)
	}
	ar := f.getAR(t)
	if ar.Status.UndoneBy != undoActionID {
		t.Errorf("after the retry the reverse link must exist; got %q", ar.Status.UndoneBy)
	}
	if !ar.Status.Contested {
		t.Error("the contested marker rides the same write and must land with it")
	}
	if linkPending(f.getUR(t, "u1")) {
		t.Error("the flag must be cleared once the link is written")
	}
	// The retry finished the paperwork. It must NOT have replayed the undo a second time.
	if f.replayer.calls != 1 {
		t.Errorf("replayer called %d times, want 1: a link retry is not a second undo", f.replayer.calls)
	}
}

func TestUndoLinkPendingClearsWhenTheOriginalAgedOut(t *testing.T) {
	// The original can reach its retention TTL while the undo record persists (06 §4.3). There is
	// then nothing left to write, and flagging forever would be a permanent false alarm.
	f := newUndoFixture(t, &stubReplayer{id: undoActionID}, undoableRecord(), undoRequest("u1", nil))
	if _, err := f.reconcile(t, "u1"); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	// Re-open the flag by hand and delete the original underneath it.
	ur := f.getUR(t, "u1")
	if err := f.r.patchStatus(f.ctx, ur, func(u *agentv1alpha1.UndoRequest) {
		setUndoCondition(u, UndoConditionLinkPending, metav1.ConditionTrue, "ReverseLinkWriteFailed", "simulated")
	}); err != nil {
		t.Fatalf("re-open the flag: %v", err)
	}
	if err := f.c.Delete(f.ctx, f.getAR(t)); err != nil {
		t.Fatalf("delete the original: %v", err)
	}

	if _, err := f.reconcile(t, "u1"); err != nil {
		t.Fatalf("reconcile after the original aged out: %v", err)
	}
	ur = f.getUR(t, "u1")
	if linkPending(ur) {
		t.Error("with the original gone there is nothing left to link, so the flag must clear")
	}
	cond := meta.FindStatusCondition(ur.Status.Conditions, UndoConditionLinkPending)
	if cond == nil || cond.Reason != "OriginalDeleted" {
		t.Errorf("the cleared flag must say why; got %+v", cond)
	}
}

// ---------------------------------------------------------------------------------------------
// The advisory annotation
// ---------------------------------------------------------------------------------------------

func TestUndoStampsTheAdvisoryContestedAnnotation(t *testing.T) {
	dep := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: "api", Namespace: undoNS}}
	f := newUndoFixture(t, &stubReplayer{id: undoActionID}, undoableRecord(), undoRequest("u1", nil), dep)

	if _, err := f.reconcile(t, "u1"); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	var got appsv1.Deployment
	if err := f.c.Get(f.ctx, types.NamespacedName{Namespace: undoNS, Name: "api"}, &got); err != nil {
		t.Fatalf("get deployment: %v", err)
	}
	if got.Annotations[journal.ContestedAnnotation] != origActionID {
		t.Errorf("annotation %s = %q, want %q", journal.ContestedAnnotation, got.Annotations[journal.ContestedAnnotation], origActionID)
	}
}

func TestUndoSucceedsWhenTheTargetCannotBeAnnotated(t *testing.T) {
	// 06 §4.4 is explicit that the index is authoritative "because a deleted object cannot hold an
	// annotation" -- and undoing a create deletes the target, so the commonest contested case has
	// no object left to stamp. A missing target, or a Forbidden from a narrow grant, must not fail
	// the undo or the marker that actually stops the redo.
	for _, tc := range []struct {
		name  string
		funcs interceptor.Funcs
	}{
		{"target does not exist", interceptor.Funcs{}},
		{"patch is forbidden", interceptor.Funcs{
			Patch: func(ctx context.Context, c client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
				return apierrors.NewForbidden(schema.GroupResource{Group: "apps", Resource: "deployments"}, obj.GetName(),
					errors.New("undo controller has no patch grant on arbitrary targets"))
			},
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := newUndoFixtureWith(t, &stubReplayer{id: undoActionID}, tc.funcs, undoableRecord(), undoRequest("u1", nil))
			if _, err := f.reconcile(t, "u1"); err != nil {
				t.Fatalf("a courtesy annotation must never fail the undo: %v", err)
			}
			ur := f.getUR(t, "u1")
			if ur.Status.Phase != agentv1alpha1.UndoExecuted {
				t.Errorf("phase = %q, want %q", ur.Status.Phase, agentv1alpha1.UndoExecuted)
			}
			ar := f.getAR(t)
			if !ar.Status.Contested {
				t.Error("the AUTHORITATIVE marker is status.contested, and it must be set whether or not the annotation landed")
			}
			if ar.Status.UndoneBy != undoActionID {
				t.Errorf("undoneBy = %q, want %q", ar.Status.UndoneBy, undoActionID)
			}
		})
	}
}

func TestUndoDoesNotAnnotateWhenContestIsDeclined(t *testing.T) {
	dep := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: "api", Namespace: undoNS}}
	f := newUndoFixture(t, &stubReplayer{id: undoActionID}, undoableRecord(), dep,
		undoRequest("u1", func(u *agentv1alpha1.UndoRequest) { u.Spec.MarkContested = ptr.To(false) }))

	if _, err := f.reconcile(t, "u1"); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	var got appsv1.Deployment
	if err := f.c.Get(f.ctx, types.NamespacedName{Namespace: undoNS, Name: "api"}, &got); err != nil {
		t.Fatalf("get deployment: %v", err)
	}
	if _, ok := got.Annotations[journal.ContestedAnnotation]; ok {
		t.Error("markContested: false must not stamp the advisory annotation either")
	}
}

// ---------------------------------------------------------------------------------------------
// Small pieces
// ---------------------------------------------------------------------------------------------

func TestUndoConditionReasonsAreCamelCaseForEveryRefusal(t *testing.T) {
	// A Condition reason is constrained to CamelCase by the API machinery, so a kebab-case refusal
	// leaking through would make the whole status write fail -- turning a clean refusal into a
	// stuck request.
	for _, refusal := range []undo.ReplayRefusal{
		undo.RefuseNoRecord, undo.RefuseAlreadyUndone, undo.RefuseNotExecuted,
		undo.RefusePlanUnusable, undo.RefuseWindowExpired, undo.ReplayAllowed,
	} {
		got := conditionReason(refusal)
		if got == "" || strings.ContainsAny(got, "-_ ") {
			t.Errorf("conditionReason(%q) = %q, which is not a legal Condition reason", refusal, got)
		}
	}
}

func TestTruncateMessageStaysInsideTheCRDBound(t *testing.T) {
	long := strings.Repeat("x", 5000)
	got := truncateMessage(long)
	if len(got) != 1024 {
		t.Errorf("len = %d, want 1024 (the status.message maxLength)", len(got))
	}
	if !strings.HasSuffix(got, "...") {
		t.Error("a truncated message must show that it was truncated")
	}
	if short := truncateMessage("fine"); short != "fine" {
		t.Errorf("a short message must pass through unchanged; got %q", short)
	}
}

func TestUnavailableReplayerRefuses(t *testing.T) {
	id, err := UnavailableReplayer{}.Replay(context.Background(), nil, nil)
	if id != "" {
		t.Errorf("id = %q, want empty", id)
	}
	if !errors.Is(err, ErrReplayerUnavailable) {
		t.Errorf("err = %v, want ErrReplayerUnavailable", err)
	}
	if errors.Is(err, ErrReplayRefused) {
		t.Error("an uninstalled replayer is a deployment problem, not a broker refusal: it must not be terminal")
	}
}
