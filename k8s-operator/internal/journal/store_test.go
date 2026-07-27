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

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
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

func newFakeStore(t *testing.T, objs ...client.Object) (*Store, client.Client) {
	t.Helper()
	c := fake.NewClientBuilder().
		WithScheme(testScheme(t)).
		WithStatusSubresource(&agentv1alpha1.ActionRecord{}).
		WithObjects(objs...).
		Build()
	return NewStore(c, newMemBlob()), c
}

func record(actionID, namespace, identity string) *agentv1alpha1.ActionRecord {
	return &agentv1alpha1.ActionRecord{
		ObjectMeta: metav1.ObjectMeta{Namespace: namespace},
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
