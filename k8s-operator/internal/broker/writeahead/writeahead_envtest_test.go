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

package writeahead

// The claims in this file are the ones a fake cannot establish, and for this package that is most
// of the substance.
//
// The confirmer's whole argument is that it checks something an in-process buffer cannot produce:
// identity assigned by the API server. A test double proves nothing about that either way -- it can
// be made to supply a uid and a resourceVersion, or made not to, and in both cases it is the test
// author's choice being read back rather than a fact about storage. Four claims therefore need a
// real server:
//
//   - journal.Store.Create against a real API server produces a record this confirmer ACCEPTS. If
//     the server did not assign both fields, or assigned them in a shape the confirmer rejects, then
//     the confirmer refuses every real action and every hermetic test above still passes.
//   - a record that was never created is NotFound, not some other error, so the "never landed" arm
//     is the one that fires in the case it was written for.
//   - client.Create DROPS status because ActionRecord has a status subresource, while journal.Labels
//     carries the phase into metadata, which survives. The phase arm reads the label for exactly
//     this reason, and this test is what makes that reason a measurement instead of a belief.
//   - a deleted record stops confirming.
//
// envtest is L1 by binding.md §Targets: a real API server, process-local, no cluster.

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

var (
	testEnv *envtest.Environment
	k8s     client.Client
	scheme  = runtime.NewScheme()
)

func TestMain(m *testing.M) {
	// Not a hard exit on a missing KUBEBUILDER_ASSETS: the hermetic half of this suite lives in
	// writeahead_test.go and must still run under a plain `go test ./...`. The environment is
	// optional and the tests that need it skip individually, via requireEnv.
	if os.Getenv("KUBEBUILDER_ASSETS") != "" {
		if err := clientgoscheme.AddToScheme(scheme); err != nil {
			panic(err)
		}
		if err := agentv1alpha1.AddToScheme(scheme); err != nil {
			panic(err)
		}
		testEnv = &envtest.Environment{
			CRDDirectoryPaths:     []string{filepath.Join("..", "..", "..", "config", "crd", "bases")},
			ErrorIfCRDPathMissing: true,
			Scheme:                scheme,
		}
		cfg, err := testEnv.Start()
		if err != nil {
			panic(fmt.Sprintf("start envtest: %v", err))
		}
		// A direct client, not a cached one -- the same choice cmd/broker makes, and for the same
		// reason. Using a cached client here would make this file agree with a confirmer that has
		// no durability property at all.
		k8s, err = client.New(cfg, client.Options{Scheme: scheme})
		if err != nil {
			panic(fmt.Sprintf("new client: %v", err))
		}
	}
	code := m.Run()
	if testEnv != nil {
		_ = testEnv.Stop()
	}
	os.Exit(code)
}

func requireEnv(t *testing.T) {
	t.Helper()
	if k8s == nil {
		t.Skip("KUBEBUILDER_ASSETS unset; run via `make test` to exercise the confirmer against a real API server")
	}
}

func newNS(t *testing.T, ctx context.Context) string {
	t.Helper()
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{GenerateName: "wa-"}}
	if err := k8s.Create(ctx, ns); err != nil {
		t.Fatalf("create namespace: %v", err)
	}
	return ns.Name
}

// fixtureNow is fixed so the retention clocks are about ordering, never about wall time.
var fixtureNow = time.Date(2026, 7, 29, 12, 0, 0, 0, time.UTC)

// liveRecord builds a record the REAL CRD will accept -- every required field and every spec-level
// rule -- in the shape pipeline step 8 hands to Create. A fixture that only satisfied a fake would
// make this file and writeahead_test.go test two different objects.
func liveRecord(ns, actionID string, phase agentv1alpha1.ActionPhase) *agentv1alpha1.ActionRecord {
	sum := sha256.Sum256([]byte(actionID))
	ar := &agentv1alpha1.ActionRecord{
		ObjectMeta: metav1.ObjectMeta{Namespace: ns},
		Spec: agentv1alpha1.ActionRecordSpec{
			ActionID:            actionID,
			AgentRef:            agentv1alpha1.AgentObjectRef{Name: "team-x-agent", Namespace: ns},
			AgentIdentity:       "developer-team/my-project/cluster-a/team-x",
			ActorServiceAccount: "developer-team-team-x-actor",
			Requester:           agentv1alpha1.ActionRequester{Kind: "human", ID: "slack:U02ABCDEF", Platform: "slack"},
			Trigger:             agentv1alpha1.ActionTrigger{Source: "chat", ChainID: actionID},
			Intent:              "scale api-gateway to 5",
			IdempotencyKey:      "sha256:" + hex.EncodeToString(sum[:]),
			Classification: agentv1alpha1.ActionClassification{
				Class:    agentv1alpha1.RiskElevated,
				Reasons:  []agentv1alpha1.ClassificationReason{{Rule: "scale", Class: "elevated"}},
				Undoable: true,
			},
			Targets: []agentv1alpha1.TargetRef{
				{Group: "apps", Version: "v1", Kind: "Deployment", Namespace: ns, Name: "api-gateway"},
			},
			Retention: agentv1alpha1.RetentionSpec{
				Class:               agentv1alpha1.RiskElevated,
				TTL:                 "720h",
				ExpiresAt:           metav1.NewTime(fixtureNow.Add(720 * time.Hour)),
				UndoWindow:          "1h",
				UndoWindowExpiresAt: metav1.NewTime(fixtureNow.Add(time.Hour)),
			},
		},
	}
	ar.Status.Phase = phase
	return ar
}

// The end-to-end claim: what pipeline step 8 writes is what step 9 confirms.
func TestARealCreateProducesARecordTheConfirmerAccepts(t *testing.T) {
	requireEnv(t)
	ctx := context.Background()
	ns := newNS(t, ctx)

	store := journal.NewStore(k8s, nil)
	if err := store.Create(ctx, liveRecord(ns, testID, agentv1alpha1.PhaseExecuting)); err != nil {
		t.Fatalf("create record: %v", err)
	}

	c := &Confirmer{Records: store, Namespace: ns}
	if err := c.ConfirmDurable(ctx, testID); err != nil {
		t.Fatalf("a record a real API server accepted must confirm; got %v", err)
	}
}

// The server, not the fixture, supplies the two fields the confirmer's central check is about.
func TestTheServerAssignsTheIdentityTheConfirmerRequires(t *testing.T) {
	requireEnv(t)
	ctx := context.Background()
	ns := newNS(t, ctx)

	ar := liveRecord(ns, testID, agentv1alpha1.PhaseExecuting)
	if ar.UID != "" || ar.ResourceVersion != "" {
		t.Fatalf("the fixture must start with neither field set, or this proves nothing: uid=%q rv=%q", ar.UID, ar.ResourceVersion)
	}
	store := journal.NewStore(k8s, nil)
	if err := store.Create(ctx, ar); err != nil {
		t.Fatalf("create record: %v", err)
	}

	got, err := store.Get(ctx, ns, testID)
	if err != nil {
		t.Fatalf("read back: %v", err)
	}
	if got.UID == "" {
		t.Error("the API server assigned no uid; the confirmer's identity check would refuse every real action")
	}
	if got.ResourceVersion == "" {
		t.Error("the API server assigned no resourceVersion; same")
	}
}

// The arm that fires when a caller executes before journaling.
func TestARecordThatWasNeverWrittenIsNotFound(t *testing.T) {
	requireEnv(t)
	ctx := context.Background()
	ns := newNS(t, ctx)

	c := &Confirmer{Records: journal.NewStore(k8s, nil), Namespace: ns}
	err := c.ConfirmDurable(ctx, testID)
	if err == nil {
		t.Fatal("an action with no record must not confirm")
	}
	if !strings.Contains(err.Error(), "the write-ahead write never landed") {
		t.Fatalf("a real absent record must take the NotFound arm, not the unknown-read arm; got %v", err)
	}
}

// A record written into one namespace does not confirm from another. The confirmer reads its own
// namespace, and this is what stops a shared cluster from letting one agent's journal entry vouch
// for another agent's mutation.
func TestARecordInAnotherNamespaceDoesNotConfirm(t *testing.T) {
	requireEnv(t)
	ctx := context.Background()
	theirs, mine := newNS(t, ctx), newNS(t, ctx)

	store := journal.NewStore(k8s, nil)
	if err := store.Create(ctx, liveRecord(theirs, testID, agentv1alpha1.PhaseExecuting)); err != nil {
		t.Fatalf("create record: %v", err)
	}

	c := &Confirmer{Records: store, Namespace: mine}
	if err := c.ConfirmDurable(ctx, testID); err == nil {
		t.Fatal("a record in someone else's namespace confirmed this broker's action")
	}
}

// The measurement the phase arm is built on. Both halves are asserted, because the arm is only
// correct if BOTH are true: status is dropped (so reading status.phase would be vacuous) and the
// label survives (so reading the label is not).
func TestCreateDropsStatusAndKeepsThePhaseLabel(t *testing.T) {
	requireEnv(t)
	ctx := context.Background()
	ns := newNS(t, ctx)

	store := journal.NewStore(k8s, nil)
	if err := store.Create(ctx, liveRecord(ns, testID, agentv1alpha1.PhaseExecuting)); err != nil {
		t.Fatalf("create record: %v", err)
	}
	got, err := store.Get(ctx, ns, testID)
	if err != nil {
		t.Fatalf("read back: %v", err)
	}

	if got.Status.Phase != "" {
		t.Errorf("status.phase survived Create as %q; if that is now true the phase arm should read status, not the label", got.Status.Phase)
	}
	if got.Labels[journal.StatusLabel] != string(agentv1alpha1.PhaseExecuting) {
		t.Errorf("the %s label is %q, want %q; the phase arm reads this label and would be vacuous without it",
			journal.StatusLabel, got.Labels[journal.StatusLabel], agentv1alpha1.PhaseExecuting)
	}
}

// The trap the phase arm exists for, reproduced end to end. journal.Store.Create folds AlreadyExists
// into a nil return, so a second Create against a parked record SUCCEEDS from the caller's point of
// view while changing nothing on the server -- including the pre-state it just set. Without the
// phase arm the confirmer would say "durable" and the executor would mutate live objects against a
// journal entry that carries no snapshot and therefore no undo plan.
func TestAParkedRecordDoesNotConfirmEvenThoughCreateSucceeded(t *testing.T) {
	requireEnv(t)
	ctx := context.Background()
	ns := newNS(t, ctx)

	store := journal.NewStore(k8s, nil)
	// Step 7: park it.
	if err := store.Create(ctx, liveRecord(ns, testID, agentv1alpha1.PhasePendingApproval)); err != nil {
		t.Fatalf("park record: %v", err)
	}
	// Step 8, as a future approval path would re-enter it: same action id, now Executing.
	second := liveRecord(ns, testID, agentv1alpha1.PhaseExecuting)
	if err := store.Create(ctx, second); err != nil {
		t.Fatalf("the second Create must fold AlreadyExists into nil, or this trap does not exist: %v", err)
	}

	got, err := store.Get(ctx, ns, testID)
	if err != nil {
		t.Fatalf("read back: %v", err)
	}
	if got.Labels[journal.StatusLabel] != string(agentv1alpha1.PhasePendingApproval) {
		t.Fatalf("the server copy should still be the parked one (%q); got %q -- if the second Create really did overwrite it, this whole trap is gone and the phase arm can be reconsidered",
			agentv1alpha1.PhasePendingApproval, got.Labels[journal.StatusLabel])
	}

	c := &Confirmer{Records: store, Namespace: ns}
	err = c.ConfirmDurable(ctx, testID)
	if err == nil {
		t.Fatal("a parked record confirmed an execution a human never approved")
	}
	if !strings.Contains(err.Error(), "PendingApproval") {
		t.Fatalf("the refusal must name the phase it found; got %v", err)
	}
}

func TestADeletedRecordStopsConfirming(t *testing.T) {
	requireEnv(t)
	ctx := context.Background()
	ns := newNS(t, ctx)

	store := journal.NewStore(k8s, nil)
	ar := liveRecord(ns, testID, agentv1alpha1.PhaseExecuting)
	if err := store.Create(ctx, ar); err != nil {
		t.Fatalf("create record: %v", err)
	}
	c := &Confirmer{Records: store, Namespace: ns}
	if err := c.ConfirmDurable(ctx, testID); err != nil {
		t.Fatalf("precondition: the record must confirm before it is deleted; got %v", err)
	}

	if err := k8s.Delete(ctx, ar); err != nil {
		t.Fatalf("delete record: %v", err)
	}
	if err := c.ConfirmDurable(ctx, testID); err == nil {
		t.Fatal("a deleted record still confirmed")
	}
}
