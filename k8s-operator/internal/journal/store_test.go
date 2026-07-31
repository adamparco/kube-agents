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

package journal

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/util/validation"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func testScheme(t *testing.T) *runtime.Scheme {
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

// newFakeStore builds a Store over a fake client that models the one API-server behaviour
// controller-runtime's fake does not: `status` is a subresource, so a POST of the object keeps spec
// and metadata and DISCARDS the status block. The fake enforces that on Update and Patch (see
// `withStatusSubresource` in pkg/client/fake) and NOT on Create, which makes it strictly more
// permissive than any real cluster at exactly the moment a record is born.
//
// That gap is not academic -- it is why the defect this store just fixed survived five phases with
// a green suite. A test written against the plain fake asserts `status.phase` came back because the
// fake never dropped it, and stays green against a Create that does no status write at all. The
// interceptor is here rather than in the one test that needs it so the whole package is measured
// against the cluster it will actually run on.
func newFakeStore(t *testing.T, objs ...client.Object) (*Store, client.Client) {
	t.Helper()
	c := fake.NewClientBuilder().
		WithScheme(testScheme(t)).
		WithStatusSubresource(&agentv1alpha1.ActionRecord{}).
		WithObjects(objs...).
		WithInterceptorFuncs(interceptor.Funcs{Create: dropStatusLikeTheApiServer}).
		Build()
	return NewStore(c, newMemBlob()), c
}

func dropStatusLikeTheApiServer(ctx context.Context, cl client.WithWatch, obj client.Object, opts ...client.CreateOption) error {
	if ar, ok := obj.(*agentv1alpha1.ActionRecord); ok {
		ar.Status = agentv1alpha1.ActionRecordStatus{}
	}
	return cl.Create(ctx, obj, opts...)
}

// record is a well-formed ActionRecord as the broker would build one at 06 §4.2 step 8: classified,
// targeted, and CREATED IN A PHASE. The phase is not incidental -- `Store.Create` refuses a record
// that is in no lifecycle position at all, because a journal entry with no phase is one the ChatOps
// gateway and the undo controller cannot legally transition out of.
func record(actionID, namespace, identity string) *agentv1alpha1.ActionRecord {
	return &agentv1alpha1.ActionRecord{
		ObjectMeta: metav1.ObjectMeta{Namespace: namespace},
		Status:     agentv1alpha1.ActionRecordStatus{Phase: agentv1alpha1.PhaseExecuting},
		Spec: agentv1alpha1.ActionRecordSpec{
			ActionID:            actionID,
			AgentIdentity:       identity,
			ActorServiceAccount: "developer-team-team-x-actor",
			Intent:              "scale api-gateway to 5",
			IdempotencyKey:      "sha256:" + strings.Repeat("a", 64),
			Classification: agentv1alpha1.ActionClassification{
				Class: agentv1alpha1.RiskElevated,
			},
			Trigger: agentv1alpha1.ActionTrigger{
				Source:  "chat",
				ChainID: "01JZQ8X9K7M4N2P6R8T0V3W5YZ",
			},
			Targets: []agentv1alpha1.TargetRef{
				{Version: "v1", Kind: "Deployment", Namespace: namespace, Name: "api-gateway"},
			},
		},
	}
}

func TestLabelsIndexTheCommonQuestions(t *testing.T) {
	ar := record("01JZQ8X9K7M4N2P6R8T0V3W5YZ", "team-x", "developer-team/my-project/cluster-a/team-x")
	ar.Status.Phase = agentv1alpha1.PhaseVerified

	got := Labels(ar)
	for k, want := range map[string]string{
		TierLabel:      "developer-team",
		ScopeLabel:     "team-x",
		RiskClassLabel: "elevated",
		TriggerLabel:   "chat",
		StatusLabel:    "Verified",
		// Lower-cased in the label, uppercase in spec: a selector a human types out of a Slack
		// thread has to match, and label selectors are byte-exact.
		ChainIDLabel: "01jzq8x9k7m4n2p6r8t0v3w5yz",
	} {
		if got[k] != want {
			t.Fatalf("label %s = %q, want %q", k, got[k], want)
		}
	}
	// undo-of is present only on an undo record; an empty label would make the selector
	// `kube-agents/undo-of` match every record in the cluster.
	if _, ok := got[UndoOfLabel]; ok {
		t.Fatalf("a non-undo record carries %s", UndoOfLabel)
	}
	ar.Spec.Trigger.Source = agentv1alpha1.ActionTriggerUndo
	ar.Spec.Trigger.UndoOf = "01JZQ8X9K7M4N2P6R8T0V3W5ZZ"
	if v := Labels(ar)[UndoOfLabel]; v != "01jzq8x9k7m4n2p6r8t0v3w5zz" {
		t.Fatalf("%s = %q on an undo record", UndoOfLabel, v)
	}
}

func TestLabelValuesAreAlwaysAcceptable(t *testing.T) {
	// Failing an action because a namespace name was 70 characters long would be the wrong trade:
	// the authoritative values are in spec and these are an index. So every value is coerced, and
	// the coercion is checked against the API server's own validator rather than against a
	// hand-rolled idea of what a label allows.
	for _, identity := range []string{
		"developer-team/my-project/cluster-a/" + strings.Repeat("x", 80),
		"platform/my project/cluster a/leaf!",
		"cluster-admin/p/c/-leading-and-trailing-",
		"",
		"noslashes",
		"developer-team/p/c/über-team",
	} {
		ar := record("01JZQ8X9K7M4N2P6R8T0V3W5YZ", "team-x", identity)
		ar.Status.Phase = agentv1alpha1.PhaseExecuting
		for k, v := range Labels(ar) {
			if errs := validation.IsValidLabelValue(v); len(errs) > 0 {
				t.Fatalf("identity %q produced label %s=%q, which the API server rejects: %v", identity, k, v, errs)
			}
		}
	}
}

func TestCreateDerivesTheNameAndIsIdempotent(t *testing.T) {
	ctx := context.Background()
	s, c := newFakeStore(t)
	ar := record("01JZQ8X9K7M4N2P6R8T0V3W5YZ", "team-x", "developer-team/my-project/cluster-a/team-x")

	if err := s.Create(ctx, ar); err != nil {
		t.Fatalf("Create: %v", err)
	}
	if ar.Name != "ar-01jzq8x9k7m4n2p6r8t0v3w5yz" {
		t.Fatalf("Name = %q, want the derived 06 §4.3 form", ar.Name)
	}
	if ar.Labels[TierLabel] != "developer-team" {
		t.Fatalf("Create did not apply the label set: %v", ar.Labels)
	}

	// A retried Create must be an AlreadyExists on the SAME record, reported as success. Treating it
	// as a failure would have the broker refuse an action that is already journaled -- fail-closed
	// pointed the wrong way, at a retry that was safe.
	retry := record("01JZQ8X9K7M4N2P6R8T0V3W5YZ", "team-x", "developer-team/my-project/cluster-a/team-x")
	if err := s.Create(ctx, retry); err != nil {
		t.Fatalf("a retried Create for the same action id failed: %v", err)
	}
	var list agentv1alpha1.ActionRecordList
	if err := c.List(ctx, &list); err != nil {
		t.Fatalf("List: %v", err)
	}
	if len(list.Items) != 1 {
		t.Fatalf("a retry produced %d records for one action; the journal must have exactly one entry per action id", len(list.Items))
	}
}

func TestCreateRefusesANonULIDActionID(t *testing.T) {
	// The CRD's pattern would reject this too, but only after the record has been built and sent.
	// Catching it here means the failure names the cause rather than surfacing as an admission error
	// the broker has to interpret -- and an id that is not a ULID cannot be joined to its writes,
	// which is the property V-BRK-003 rests on.
	s, _ := newFakeStore(t)
	ar := record("not-a-ulid", "team-x", "developer-team/p/c/team-x")
	if err := s.Create(context.Background(), ar); err == nil {
		t.Fatal("a record with a non-ULID action id was created")
	}
}

func TestCreateFailsClosedAndSaysSo(t *testing.T) {
	// 03 §6: if Create returns an error the caller MUST NOT execute. Two things are asserted, and
	// the second is the one that decays. An AlreadyExists is swallowed as a safe retry, so every
	// OTHER error has to surface -- and the message has to say what the caller must do, because the
	// caller most likely to get this wrong is a future one reading only the error text.
	c := fake.NewClientBuilder().
		WithScheme(testScheme(t)).
		WithStatusSubresource(&agentv1alpha1.ActionRecord{}).
		WithInterceptorFuncs(interceptor.Funcs{
			Create: func(context.Context, client.WithWatch, client.Object, ...client.CreateOption) error {
				return apierrors.NewInternalError(errors.New("etcd is unavailable"))
			},
		}).
		Build()
	s := NewStore(c, newMemBlob())

	err := s.Create(context.Background(), record("01JZQ8X9K7M4N2P6R8T0V3W5YZ", "team-x", "developer-team/p/c/team-x"))
	if err == nil {
		t.Fatal("Create reported success while the API server refused the record; the action would execute unjournaled")
	}
	if !strings.Contains(err.Error(), "fail closed") {
		t.Fatalf("the error does not tell the caller not to execute: %v", err)
	}
}

func TestSetPhaseKeepsStatusAndLabelInStep(t *testing.T) {
	ctx := context.Background()
	s, c := newFakeStore(t)
	ar := record("01JZQ8X9K7M4N2P6R8T0V3W5YZ", "team-x", "developer-team/p/c/team-x")
	if err := s.Create(ctx, ar); err != nil {
		t.Fatalf("Create: %v", err)
	}

	if err := s.SetPhase(ctx, ar, agentv1alpha1.PhaseExecuting, "applying the patch"); err != nil {
		t.Fatalf("SetPhase: %v", err)
	}
	var got agentv1alpha1.ActionRecord
	if err := c.Get(ctx, client.ObjectKey{Namespace: "team-x", Name: ar.Name}, &got); err != nil {
		t.Fatalf("Get: %v", err)
	}
	if got.Status.Phase != agentv1alpha1.PhaseExecuting {
		t.Fatalf("status.phase = %q", got.Status.Phase)
	}
	if got.Status.Message != "applying the patch" {
		t.Fatalf("status.message = %q", got.Status.Message)
	}
	// The label is the index the ChatOps reporter selects on. If it drifts from status.phase, a
	// `/kage pending` that returns nothing looks exactly like a quiet cluster.
	if got.Labels[StatusLabel] != string(agentv1alpha1.PhaseExecuting) {
		t.Fatalf("%s = %q, want %q -- the index disagrees with the truth",
			StatusLabel, got.Labels[StatusLabel], agentv1alpha1.PhaseExecuting)
	}

	if err := s.SetPhase(ctx, ar, agentv1alpha1.PhaseVerified, "done"); err != nil {
		t.Fatalf("second SetPhase: %v", err)
	}
	if err := c.Get(ctx, client.ObjectKey{Namespace: "team-x", Name: ar.Name}, &got); err != nil {
		t.Fatalf("Get: %v", err)
	}
	if got.Labels[StatusLabel] != string(agentv1alpha1.PhaseVerified) {
		t.Fatalf("%s = %q after a second transition", StatusLabel, got.Labels[StatusLabel])
	}
}

// SetPhase re-reads the record and writes the LIVE copy, so for as long as it copied nothing across
// from the caller, every status field the caller had composed was discarded. The pipeline composes
// four of them — `status.applied` at step 9, `status.verification` and `status.recovery` at step 11,
// and the lifecycle clock throughout — and only `phase` and `message` ever reached etcd. A live
// record from `broker-execute-l2.sh` on 2026-07-31 read back with a phase, a message and nothing
// else: the audit trail said an action had happened and could not say what it did.
//
// `status.timestamps.executionStarted` is also V-BRK-006's L2 evidence, so this was not only a lossy
// record — it was a check that could not run.
func TestSetPhaseCarriesTheOutcomeTheBrokerOwns(t *testing.T) {
	ctx := context.Background()
	s, c := newFakeStore(t)
	ar := record("01JZQ8X9K7M4N2P6R8T0V3W5YZ", "team-x", "developer-team/p/c/team-x")
	if err := s.Create(ctx, ar); err != nil {
		t.Fatalf("Create: %v", err)
	}

	started := metav1.NewTime(time.Date(2026, 7, 31, 5, 45, 20, 0, time.UTC))
	ended := metav1.NewTime(time.Date(2026, 7, 31, 5, 45, 21, 0, time.UTC))
	ar.Status.Timestamps = &agentv1alpha1.ActionTimestamps{ExecutionStarted: &started, ExecutionEnded: &ended}
	ar.Status.Applied = []agentv1alpha1.AppliedTarget{{TargetIndex: 0, ResourceVersionAfter: "4711"}}
	ar.Status.Verification = &agentv1alpha1.ActionVerification{Passed: true}
	ar.Status.Recovery = &agentv1alpha1.ActionRecovery{Rung: 1}

	if err := s.SetPhase(ctx, ar, agentv1alpha1.PhaseVerified, "executed"); err != nil {
		t.Fatalf("SetPhase: %v", err)
	}

	var got agentv1alpha1.ActionRecord
	if err := c.Get(ctx, client.ObjectKey{Namespace: "team-x", Name: ar.Name}, &got); err != nil {
		t.Fatalf("Get: %v", err)
	}
	if got.Status.Timestamps == nil || got.Status.Timestamps.ExecutionStarted == nil {
		t.Fatalf("status.timestamps.executionStarted did not survive the write: %+v", got.Status.Timestamps)
	}
	if !got.Status.Timestamps.ExecutionStarted.Equal(&started) {
		t.Errorf("executionStarted = %v, want %v", got.Status.Timestamps.ExecutionStarted, started)
	}
	if !got.Status.Timestamps.ExecutionEnded.Equal(&ended) {
		t.Errorf("executionEnded = %v, want %v", got.Status.Timestamps.ExecutionEnded, ended)
	}
	if len(got.Status.Applied) != 1 {
		t.Errorf("status.applied = %+v, want the one target the caller composed", got.Status.Applied)
	}
	if got.Status.Verification == nil || !got.Status.Verification.Passed {
		t.Errorf("status.verification = %+v", got.Status.Verification)
	}
	if got.Status.Recovery == nil || got.Status.Recovery.Rung != 1 {
		t.Errorf("status.recovery = %+v", got.Status.Recovery)
	}
	if got.Status.Phase != agentv1alpha1.PhaseVerified || got.Status.Message != "executed" {
		t.Errorf("phase/message = %q/%q", got.Status.Phase, got.Status.Message)
	}
}

// The nil-guard, which is what makes the merge safe to run on EVERY transition rather than only the
// terminal one. SetPhase is called for plain lifecycle steps too, and on those the caller's copy is
// a record it read moments ago and never composed anything onto. An unguarded field-by-field copy
// would let such a caller erase a clock the server already holds -- the same data loss as no merge
// at all, arriving through the fix for it.
func TestSetPhaseDoesNotBlankWhatTheCallerNeverSet(t *testing.T) {
	ctx := context.Background()
	s, c := newFakeStore(t)
	ar := record("01JZQ8X9K7M4N2P6R8T0V3W5YZ", "team-x", "developer-team/p/c/team-x")
	if err := s.Create(ctx, ar); err != nil {
		t.Fatalf("Create: %v", err)
	}

	// The server holds a full outcome: the birth beats from the write-ahead Create, plus what an
	// earlier transition persisted.
	started := metav1.NewTime(time.Date(2026, 7, 31, 5, 45, 20, 0, time.UTC))
	var live agentv1alpha1.ActionRecord
	if err := c.Get(ctx, client.ObjectKey{Namespace: "team-x", Name: ar.Name}, &live); err != nil {
		t.Fatalf("Get: %v", err)
	}
	live.Status.Timestamps = &agentv1alpha1.ActionTimestamps{ExecutionStarted: &started}
	live.Status.Applied = []agentv1alpha1.AppliedTarget{{TargetIndex: 0, ResourceVersionAfter: "4711"}}
	live.Status.Verification = &agentv1alpha1.ActionVerification{Passed: true}
	if err := c.Status().Update(ctx, &live); err != nil {
		t.Fatalf("seeding the server's outcome: %v", err)
	}

	// The caller carries a phase and nothing else -- the shape of every non-terminal SetPhase.
	ar.Status.Timestamps = nil
	ar.Status.Applied = nil
	ar.Status.Verification = nil
	if err := s.SetPhase(ctx, ar, agentv1alpha1.PhaseVerified, "verified"); err != nil {
		t.Fatalf("SetPhase: %v", err)
	}

	var got agentv1alpha1.ActionRecord
	if err := c.Get(ctx, client.ObjectKey{Namespace: "team-x", Name: ar.Name}, &got); err != nil {
		t.Fatalf("Get: %v", err)
	}
	if got.Status.Timestamps == nil || got.Status.Timestamps.ExecutionStarted == nil {
		t.Fatalf("status.timestamps was erased by a caller that never set it: %+v", got.Status.Timestamps)
	}
	if !got.Status.Timestamps.ExecutionStarted.Equal(&started) {
		t.Errorf("executionStarted = %v, want the server's %v", got.Status.Timestamps.ExecutionStarted, started)
	}
	if len(got.Status.Applied) != 1 {
		t.Errorf("status.applied was erased: %+v", got.Status.Applied)
	}
	if got.Status.Verification == nil || !got.Status.Verification.Passed {
		t.Errorf("status.verification was erased: %+v", got.Status.Verification)
	}
}

// The other half of the same rule, and the one that makes it safe: 06 §4.3 hands `approvals`,
// `contested` and `undoneBy` to principals that are NOT this broker, and `exported` to the audit
// exporter. SetPhase reads the record fresh and must leave every one of them exactly as the server
// has it — a broker that copied its own stale idea of `approvals` back would be silently reversing a
// human decision, which is the worst thing a wholesale status copy could do.
func TestSetPhaseNeverWritesAnotherPrincipalsStatus(t *testing.T) {
	ctx := context.Background()
	s, c := newFakeStore(t)
	ar := record("01JZQ8X9K7M4N2P6R8T0V3W5YZ", "team-x", "developer-team/p/c/team-x")
	if err := s.Create(ctx, ar); err != nil {
		t.Fatalf("Create: %v", err)
	}

	// The other writers move first, straight against the server, as they would in a cluster.
	var live agentv1alpha1.ActionRecord
	if err := c.Get(ctx, client.ObjectKey{Namespace: "team-x", Name: ar.Name}, &live); err != nil {
		t.Fatalf("Get: %v", err)
	}
	live.Status.Approvals = &agentv1alpha1.ActionApprovals{Required: 2}
	live.Status.Contested = true
	live.Status.UndoneBy = "01JZQ8X9K7M4N2P6R8T0V3W5ZZ"
	if err := c.Status().Update(ctx, &live); err != nil {
		t.Fatalf("seeding another principal's status: %v", err)
	}

	// The broker's copy predates all three and disagrees about every one of them.
	ar.Status.Approvals = nil
	ar.Status.Contested = false
	ar.Status.UndoneBy = ""
	if err := s.SetPhase(ctx, ar, agentv1alpha1.PhaseVerified, "executed"); err != nil {
		t.Fatalf("SetPhase: %v", err)
	}

	var got agentv1alpha1.ActionRecord
	if err := c.Get(ctx, client.ObjectKey{Namespace: "team-x", Name: ar.Name}, &got); err != nil {
		t.Fatalf("Get: %v", err)
	}
	if got.Status.Approvals == nil || got.Status.Approvals.Required != 2 {
		t.Errorf("status.approvals = %+v; the broker blanked the ChatOps gateway's write", got.Status.Approvals)
	}
	if !got.Status.Contested {
		t.Error("status.contested was cleared by a phase change; only the gateway and the undo controller may clear it")
	}
	if got.Status.UndoneBy != "01JZQ8X9K7M4N2P6R8T0V3W5ZZ" {
		t.Errorf("status.undoneBy = %q; the undo controller's write was overwritten", got.Status.UndoneBy)
	}
}

// The broker's grant is the reason this arm exists. 06 §2.2.1 gives broker-operations
// `actionrecords get list watch create` and `actionrecords/status get update patch`, and withholds
// `update` on the main resource deliberately -- the broker appends and advances status, it can never
// rewrite or remove a record. `kube-agents/status` is a LABEL, so keeping it in step is an `update`
// on the main resource and always will be. Before this arm, that meant every terminal transition the
// broker took returned a hard error for an action that had already executed and whose authoritative
// `status.phase` had already landed through the subresource the grant does allow -- surfaced to the
// caller as an HTTP 500 on a successful action, which is a false negative in the audit trail.
//
// Found live: `dev/verify/broker-execute-l2.sh` reached step 11 against
// `gke-scratch-kube-agents-dev` and failed with exactly this Forbidden.
func TestSetPhaseSurvivesAnIndexWriteTheGrantForbids(t *testing.T) {
	ctx := context.Background()
	forbidden := apierrors.NewForbidden(
		schema.GroupResource{Group: "kubeagents.x-k8s.io", Resource: "actionrecords"},
		"ar-01jzq8x9k7m4n2p6r8t0v3w5yz",
		errors.New(`User "system:serviceaccount:kubeagents-system:platform-p-actor" cannot update resource "actionrecords"`))

	var updates int
	c := fake.NewClientBuilder().
		WithScheme(testScheme(t)).
		WithStatusSubresource(&agentv1alpha1.ActionRecord{}).
		WithInterceptorFuncs(interceptor.Funcs{
			Create: dropStatusLikeTheApiServer,
			// Only the MAIN resource is refused. `Status().Update` routes through SubResourceUpdate,
			// which is left alone -- modelling the grant as it is written, not as a blanket denial,
			// because a blanket denial would also have caught a bug in the status write and this
			// test would then not be about the label at all.
			Update: func(ctx context.Context, cl client.WithWatch, obj client.Object, opts ...client.UpdateOption) error {
				updates++
				return forbidden
			},
		}).
		Build()
	s := NewStore(c, newMemBlob())

	ar := record("01JZQ8X9K7M4N2P6R8T0V3W5YZ", "team-x", "platform/my-project/cluster-a/team-x")
	if err := s.Create(ctx, ar); err != nil {
		t.Fatalf("Create: %v", err)
	}

	if err := s.SetPhase(ctx, ar, agentv1alpha1.PhaseVerified, "executed"); err != nil {
		t.Fatalf("SetPhase reported failure for an action whose outcome WAS journaled: %v", err)
	}
	if updates == 0 {
		t.Fatal("the label sync was never attempted, so this test proves nothing about tolerating its refusal")
	}

	var got agentv1alpha1.ActionRecord
	if err := c.Get(ctx, client.ObjectKey{Namespace: "team-x", Name: ar.Name}, &got); err != nil {
		t.Fatalf("Get: %v", err)
	}
	// The authoritative field landed. That is what makes swallowing the index failure legitimate.
	if got.Status.Phase != agentv1alpha1.PhaseVerified {
		t.Fatalf("status.phase = %q, want %q -- the outcome was not journaled at all", got.Status.Phase, agentv1alpha1.PhaseVerified)
	}
	if got.Status.Message != "executed" {
		t.Fatalf("status.message = %q", got.Status.Message)
	}
	// The index legitimately lags. `JournalReconciler.repairStatusLabel` closes it, running in the
	// operator, which does hold `update`.
	if got.Labels[StatusLabel] == string(agentv1alpha1.PhaseVerified) {
		t.Fatal("the label was refused by the API server and yet came back updated; the fake is not modelling the grant")
	}
	// And the caller's copy must not claim otherwise. Adopting live.Labels on this path would make
	// an in-memory read of the index disagree with the server -- the exact drift the whole
	// status/label pair exists to avoid.
	if ar.Labels[StatusLabel] == string(agentv1alpha1.PhaseVerified) {
		t.Fatalf("the caller's copy says %s=%q, but that write was refused", StatusLabel, ar.Labels[StatusLabel])
	}
	if ar.Status.Phase != agentv1alpha1.PhaseVerified {
		t.Fatalf("the caller's copy did not adopt the status that DID land: %q", ar.Status.Phase)
	}
}

// A Forbidden is the RBAC model working as specified; every other failure is not, and must still
// reach the caller. Narrowing the tolerance is the difference between "this write is closed to me by
// design" and "this write is broken", and only the first one is safe to continue past.
func TestSetPhaseStillFailsOnAnIndexWriteThatIsNotForbidden(t *testing.T) {
	ctx := context.Background()
	c := fake.NewClientBuilder().
		WithScheme(testScheme(t)).
		WithStatusSubresource(&agentv1alpha1.ActionRecord{}).
		WithInterceptorFuncs(interceptor.Funcs{
			Create: dropStatusLikeTheApiServer,
			Update: func(ctx context.Context, cl client.WithWatch, obj client.Object, opts ...client.UpdateOption) error {
				return apierrors.NewInternalError(errors.New("etcdserver: request timed out"))
			},
		}).
		Build()
	s := NewStore(c, newMemBlob())

	ar := record("01JZQ8X9K7M4N2P6R8T0V3W5YZ", "team-x", "platform/my-project/cluster-a/team-x")
	if err := s.Create(ctx, ar); err != nil {
		t.Fatalf("Create: %v", err)
	}
	err := s.SetPhase(ctx, ar, agentv1alpha1.PhaseVerified, "executed")
	if err == nil {
		t.Fatal("SetPhase swallowed a transient index failure; only a Forbidden is a by-design refusal")
	}
	if !strings.Contains(err.Error(), StatusLabel) {
		t.Fatalf("the error does not say which write failed: %v", err)
	}
}

// V-CTR-006 at the enforcement point. The lifecycle itself is a truth table in
// `api/v1alpha1/actionrecord_phases_test.go`; what these assert is that the one function every
// phase change in the system goes through actually consults it, and that a refusal writes nothing.
// A predicate no writer calls is 09 §11.9 all over again.

func TestTheRecordIsBornInAPhaseTheApiServerCanSee(t *testing.T) {
	// `status` is a subresource: the Create call sends the whole object and the API server keeps
	// only spec + metadata. Until this was fixed, EVERY record read back `status.phase: ""` while
	// its `kube-agents/status` label said `Executing` -- so 06 §4.3, which makes `status.phase`
	// authoritative and the label a derived index, was inverted in practice. A record parked for
	// approval had no `PendingApproval` for the ChatOps gateway's one permitted transition to
	// leave from.
	ctx := context.Background()
	s, c := newFakeStore(t)
	ar := record("01JZQ8X9K7M4N2P6R8T0V3W5YZ", "team-x", "developer-team/p/c/team-x")
	ar.Status.Phase = agentv1alpha1.PhasePendingApproval
	if err := s.Create(ctx, ar); err != nil {
		t.Fatalf("Create: %v", err)
	}

	var got agentv1alpha1.ActionRecord
	if err := c.Get(ctx, client.ObjectKey{Namespace: "team-x", Name: ar.Name}, &got); err != nil {
		t.Fatalf("Get: %v", err)
	}
	if got.Status.Phase != agentv1alpha1.PhasePendingApproval {
		t.Fatalf("status.phase = %q after Create, want PendingApproval -- the authoritative field is empty and only the label knows", got.Status.Phase)
	}
	if got.Labels[StatusLabel] != string(agentv1alpha1.PhasePendingApproval) {
		t.Fatalf("%s = %q, want PendingApproval", StatusLabel, got.Labels[StatusLabel])
	}

	// And the transition the gateway is specified to make (06 §4.3's status-RBAC table) now has a
	// from-phase to make it from.
	if err := s.SetPhase(ctx, ar, agentv1alpha1.PhasePending, "approved by U02ABCDEF"); err != nil {
		t.Fatalf("PendingApproval -> Pending: %v", err)
	}
}

func TestARecordMayNotBeCreatedInAPhaseThatClaimsAPastItNeverHad(t *testing.T) {
	ctx := context.Background()
	for _, phase := range []agentv1alpha1.ActionPhase{
		agentv1alpha1.PhaseVerified,
		agentv1alpha1.PhaseUndone,
		agentv1alpha1.PhaseRolledBack,
		"",
	} {
		name := string(phase)
		if name == "" {
			name = "no phase at all"
		}
		t.Run(name, func(t *testing.T) {
			s, c := newFakeStore(t)
			ar := record("01JZQ8X9K7M4N2P6R8T0V3W5YZ", "team-x", "developer-team/p/c/team-x")
			ar.Status.Phase = phase

			err := s.Create(ctx, ar)
			if err == nil {
				t.Fatalf("a record was created in phase %q", phase)
			}
			// Create is the 03 §6 fail-closed point, so the refusal has to say so or a caller
			// reading only the text will treat it as a validation nit and carry on.
			if !strings.Contains(err.Error(), "fail closed") {
				t.Errorf("the refusal does not tell the caller not to execute: %v", err)
			}
			// Nothing may be left behind. A half-created record with a forged phase is worse than
			// no record: it is a journal entry for an action that was refused.
			var list agentv1alpha1.ActionRecordList
			if err := c.List(ctx, &list); err != nil {
				t.Fatalf("List: %v", err)
			}
			if len(list.Items) != 0 {
				t.Errorf("%d record(s) written despite the refusal", len(list.Items))
			}
		})
	}
}

func TestSetPhaseRefusesATransitionTheLifecycleDoesNotHave(t *testing.T) {
	ctx := context.Background()
	for _, tc := range []struct {
		name     string
		start    agentv1alpha1.ActionPhase
		to       agentv1alpha1.ActionPhase
		contains string
	}{
		{"out of a terminal phase", agentv1alpha1.PhaseRejected, agentv1alpha1.PhaseExecuting, "terminal"},
		{"backwards into execution", agentv1alpha1.PhaseExecuting, agentv1alpha1.PhasePending, "not a legal"},
		{"skipping execution entirely", agentv1alpha1.PhasePendingApproval, agentv1alpha1.PhaseVerified, "not a legal"},
		{"a phase the lifecycle has never heard of", agentv1alpha1.PhaseExecuting, agentv1alpha1.ActionPhase("Concluded"), "not a member"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s, c := newFakeStore(t)
			ar := record("01JZQ8X9K7M4N2P6R8T0V3W5YZ", "team-x", "developer-team/p/c/team-x")
			ar.Status.Phase = tc.start
			if err := s.Create(ctx, ar); err != nil {
				t.Fatalf("Create in %q: %v", tc.start, err)
			}

			err := s.SetPhase(ctx, ar, tc.to, "should not land")
			if err == nil {
				t.Fatalf("%q -> %q was written", tc.start, tc.to)
			}
			if !strings.Contains(err.Error(), tc.contains) {
				t.Errorf("refusal does not say %q: %v", tc.contains, err)
			}

			// A refused transition must leave BOTH the field and the index untouched. Writing one
			// of the two would be worse than writing neither: the reconciler repairs the label
			// from status.phase, so a stray label write is silently reverted and a stray status
			// write is silently adopted.
			var got agentv1alpha1.ActionRecord
			if err := c.Get(ctx, client.ObjectKey{Namespace: "team-x", Name: ar.Name}, &got); err != nil {
				t.Fatalf("Get: %v", err)
			}
			if got.Status.Phase != tc.start {
				t.Errorf("status.phase = %q after a refused transition, want %q", got.Status.Phase, tc.start)
			}
			if got.Labels[StatusLabel] != string(tc.start) {
				t.Errorf("%s = %q after a refused transition, want %q", StatusLabel, got.Labels[StatusLabel], tc.start)
			}
			if got.Status.Message == "should not land" {
				t.Error("status.message was written by a refused transition")
			}
		})
	}
}

func TestTheTransitionIsJudgedAgainstTheLiveRecordAndNotTheCallersCopy(t *testing.T) {
	// Two writers, one record. The caller's in-memory copy says `Executing`; the cluster has moved
	// to `Verified` since. `Executing -> Failed` is legal and `Verified -> Failed` is not, so
	// validating against the stale copy would let a lost update wear a legal transition's clothes.
	// This is the same reason SetPhase re-reads before writing at all.
	ctx := context.Background()
	s, c := newFakeStore(t)
	ar := record("01JZQ8X9K7M4N2P6R8T0V3W5YZ", "team-x", "developer-team/p/c/team-x")
	if err := s.Create(ctx, ar); err != nil {
		t.Fatalf("Create: %v", err)
	}

	// Somebody else finishes the action.
	stale := ar.DeepCopy()
	if err := s.SetPhase(ctx, ar, agentv1alpha1.PhaseVerified, "verified elsewhere"); err != nil {
		t.Fatalf("SetPhase: %v", err)
	}
	if stale.Status.Phase != agentv1alpha1.PhaseExecuting {
		t.Fatalf("the stale copy is not stale: %q", stale.Status.Phase)
	}

	err := s.SetPhase(ctx, stale, agentv1alpha1.PhaseFailed, "late failure report")
	if err == nil {
		t.Fatal("a Verified record was moved to Failed through a caller holding an Executing copy")
	}
	var got agentv1alpha1.ActionRecord
	if err := c.Get(ctx, client.ObjectKey{Namespace: "team-x", Name: ar.Name}, &got); err != nil {
		t.Fatalf("Get: %v", err)
	}
	if got.Status.Phase != agentv1alpha1.PhaseVerified {
		t.Fatalf("status.phase = %q, want Verified", got.Status.Phase)
	}
}

func TestARetriedCreateDoesNotRewindThePhase(t *testing.T) {
	// Store.Create folds AlreadyExists into nil so a broker retry is safe. That fold now has a
	// second obligation: the retry carries the record as the broker built it, phase `Executing`,
	// and the live record may be `Verified` by then. Re-stamping the initial phase would be a
	// transition nothing validated, taken by the one path whose whole contract is that it changed
	// nothing.
	ctx := context.Background()
	s, c := newFakeStore(t)
	ar := record("01JZQ8X9K7M4N2P6R8T0V3W5YZ", "team-x", "developer-team/p/c/team-x")
	if err := s.Create(ctx, ar); err != nil {
		t.Fatalf("Create: %v", err)
	}
	if err := s.SetPhase(ctx, ar, agentv1alpha1.PhaseVerified, "done"); err != nil {
		t.Fatalf("SetPhase: %v", err)
	}

	retry := record("01JZQ8X9K7M4N2P6R8T0V3W5YZ", "team-x", "developer-team/p/c/team-x")
	if err := s.Create(ctx, retry); err != nil {
		t.Fatalf("retried Create: %v -- a safe retry must stay safe", err)
	}
	var got agentv1alpha1.ActionRecord
	if err := c.Get(ctx, client.ObjectKey{Namespace: "team-x", Name: ar.Name}, &got); err != nil {
		t.Fatalf("Get: %v", err)
	}
	if got.Status.Phase != agentv1alpha1.PhaseVerified {
		t.Fatalf("status.phase = %q after a retried Create, want Verified", got.Status.Phase)
	}
}

func TestGetByActionID(t *testing.T) {
	ctx := context.Background()
	s, _ := newFakeStore(t)
	ar := record("01JZQ8X9K7M4N2P6R8T0V3W5YZ", "team-x", "developer-team/p/c/team-x")
	if err := s.Create(ctx, ar); err != nil {
		t.Fatalf("Create: %v", err)
	}
	got, err := s.Get(ctx, "team-x", "01JZQ8X9K7M4N2P6R8T0V3W5YZ")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if got.Spec.ActionID != ar.Spec.ActionID {
		t.Fatalf("Get returned %q", got.Spec.ActionID)
	}
	if _, err := s.Get(ctx, "team-x", "01JZQ8X9K7M4N2P6R8T0V3W5ZZ"); err == nil {
		t.Fatal("Get returned a record for an action id that was never journaled")
	}
}

func TestListBySelector(t *testing.T) {
	ctx := context.Background()
	s, _ := newFakeStore(t)
	for i, class := range []agentv1alpha1.ActionRiskClass{
		agentv1alpha1.RiskRoutine, agentv1alpha1.RiskElevated, agentv1alpha1.RiskElevated,
	} {
		ar := record("01JZQ8X9K7M4N2P6R8T0V3W5Y"+string(rune('A'+i)), "team-x", "developer-team/p/c/team-x")
		ar.Spec.Classification.Class = class
		if err := s.Create(ctx, ar); err != nil {
			t.Fatalf("Create: %v", err)
		}
	}
	all, err := s.List(ctx, "team-x", nil)
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if len(all) != 3 {
		t.Fatalf("a nil selector returned %d records, want 3", len(all))
	}
	elevated, err := s.List(ctx, "team-x", map[string]string{RiskClassLabel: "elevated"})
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if len(elevated) != 2 {
		t.Fatalf("selector returned %d records, want 2", len(elevated))
	}
}

func TestChainCrossesNamespaces(t *testing.T) {
	// A delegation crosses tiers by definition: the platform agent's record and the developer-team
	// record it caused do not share a namespace. A namespaced Chain would answer "what did that one
	// chat message do" with half the answer and no indication that half was missing.
	ctx := context.Background()
	s, _ := newFakeStore(t)
	const chain = "01JZQ8X9K7M4N2P6R8T0V3WCCC"

	for i, ns := range []string{"kubeagents-system", "cluster-a", "team-x"} {
		ar := record("01JZQ8X9K7M4N2P6R8T0V3W5Y"+string(rune('A'+i)), ns, "developer-team/p/c/"+ns)
		ar.Spec.Trigger.ChainID = chain
		if err := s.Create(ctx, ar); err != nil {
			t.Fatalf("Create in %s: %v", ns, err)
		}
	}
	// A record in another chain, to prove the selector is doing work.
	other := record("01JZQ8X9K7M4N2P6R8T0V3W5ZZ", "team-x", "developer-team/p/c/team-x")
	other.Spec.Trigger.ChainID = "01JZQ8X9K7M4N2P6R8T0V3WDDD"
	if err := s.Create(ctx, other); err != nil {
		t.Fatalf("Create: %v", err)
	}

	got, err := s.Chain(ctx, chain)
	if err != nil {
		t.Fatalf("Chain: %v", err)
	}
	if len(got) != 3 {
		t.Fatalf("Chain returned %d records across namespaces, want 3", len(got))
	}
	// Callers pass the uppercase spec value; the label is lower-cased. Chain has to bridge that or
	// every lookup silently returns nothing.
	lower, err := s.Chain(ctx, strings.ToLower(chain))
	if err != nil {
		t.Fatalf("Chain (lower): %v", err)
	}
	if len(lower) != 3 {
		t.Fatalf("Chain is case-sensitive: the lower-cased id returned %d records", len(lower))
	}
}
