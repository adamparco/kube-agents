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
	"errors"
	"testing"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/controller"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

// The pair of controllers implements one property that neither implements alone: a record is
// deleted only after its export is durable (05 §1.2). Exercised here with a fake client rather than
// envtest, because the interesting inputs are a clock 31 days ahead and a sink that refuses -- and
// neither is available on a real cluster inside a test.

const (
	journalNS       = "team-x"
	journalActionID = "01JZQ8X9K7M4N2P6R8T0V3W5YZ"
	journalRecord   = "ar-01jzq8x9k7m4n2p6r8t0v3w5yz"
)

var (
	journalSubmitted = time.Date(2026, 7, 27, 12, 0, 0, 0, time.UTC)
	pastTTL          = journalSubmitted.Add(31 * 24 * time.Hour)
)

func journalScheme(t *testing.T) *runtime.Scheme {
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

// terminalRecord is a routine action that has finished: 30-day TTL, ready to export, not yet
// exported.
func terminalRecord(t *testing.T, phase agentv1alpha1.ActionPhase) *agentv1alpha1.ActionRecord {
	t.Helper()
	r, err := journal.RetentionFor(agentv1alpha1.RiskRoutine, journalSubmitted)
	if err != nil {
		t.Fatalf("RetentionFor: %v", err)
	}
	ended := metav1.NewTime(journalSubmitted)
	return &agentv1alpha1.ActionRecord{
		ObjectMeta: metav1.ObjectMeta{
			Name:              journalRecord,
			Namespace:         journalNS,
			CreationTimestamp: metav1.NewTime(journalSubmitted),
			Labels:            map[string]string{journal.TierLabel: "developer-team"},
		},
		Spec: agentv1alpha1.ActionRecordSpec{
			ActionID:            journalActionID,
			AgentIdentity:       "developer-team/my-project/cluster-a/team-x",
			ActorServiceAccount: "developer-team-team-x-actor",
			Intent:              "scale api-gateway to 5",
			IdempotencyKey:      "sha256:0000000000000000000000000000000000000000000000000000000000000000",
			Classification:      agentv1alpha1.ActionClassification{Class: agentv1alpha1.RiskRoutine},
			Trigger:             agentv1alpha1.ActionTrigger{Source: "chat", ChainID: journalActionID},
			Targets: []agentv1alpha1.TargetRef{
				{Version: "v1", Kind: "Deployment", Namespace: journalNS, Name: "api-gateway"},
			},
			Retention: r,
		},
		Status: agentv1alpha1.ActionRecordStatus{
			Phase:      phase,
			Timestamps: &agentv1alpha1.ActionTimestamps{Submitted: &ended, ExecutionEnded: &ended},
		},
	}
}

func journalClient(t *testing.T, objs ...client.Object) client.Client {
	t.Helper()
	return fake.NewClientBuilder().
		WithScheme(journalScheme(t)).
		WithStatusSubresource(&agentv1alpha1.ActionRecord{}).
		WithObjects(objs...).
		Build()
}

func journalReq() ctrl.Request {
	return ctrl.Request{NamespacedName: types.NamespacedName{Namespace: journalNS, Name: journalRecord}}
}

func getRecord(t *testing.T, c client.Client) *agentv1alpha1.ActionRecord {
	t.Helper()
	var ar agentv1alpha1.ActionRecord
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: journalNS, Name: journalRecord}, &ar); err != nil {
		t.Fatalf("get record: %v", err)
	}
	return &ar
}

func TestJournalReconcilerExportsTerminalRecords(t *testing.T) {
	ar := terminalRecord(t, agentv1alpha1.PhaseVerified)
	c := journalClient(t, ar)
	sink := &journal.MemorySink{}
	r := &controller.JournalReconciler{
		Client: c, Scheme: journalScheme(t), Sink: sink,
		Now: func() time.Time { return journalSubmitted },
	}

	if _, err := r.Reconcile(context.Background(), journalReq()); err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	if len(sink.Entries()) != 1 {
		t.Fatalf("%d entries exported, want 1", len(sink.Entries()))
	}
	got := getRecord(t, c)
	if got.Status.Exported == nil || !got.Status.Exported.Confirmed {
		t.Fatal("the record was exported but not confirmed; retention would keep it forever")
	}
	if got.Status.Exported.Sink != sink.Name() {
		t.Fatalf("status.exported.sink = %q, want %q -- a reader has to be able to go and look rather than trust a boolean",
			got.Status.Exported.Sink, sink.Name())
	}

	// A second pass must not re-export. The sink is append-only, so a reconciler that re-exported on
	// every resync would fill the audit stream with duplicates of every terminal record forever.
	if _, err := r.Reconcile(context.Background(), journalReq()); err != nil {
		t.Fatalf("second Reconcile: %v", err)
	}
	if len(sink.Entries()) != 1 {
		t.Fatalf("%d entries after a second reconcile; the record was re-exported", len(sink.Entries()))
	}
}

func TestJournalReconcilerLeavesInFlightRecordsAlone(t *testing.T) {
	// Exporting every intermediate transition would multiply sink volume by the length of the
	// lifecycle for no added evidence -- the terminal record carries the timestamps, the applied
	// diff, the verification result and the report.
	for _, phase := range []agentv1alpha1.ActionPhase{
		agentv1alpha1.PhasePending,
		agentv1alpha1.PhasePendingApproval,
		agentv1alpha1.PhaseExecuting,
	} {
		t.Run(string(phase), func(t *testing.T) {
			c := journalClient(t, terminalRecord(t, phase))
			sink := &journal.MemorySink{}
			r := &controller.JournalReconciler{Client: c, Scheme: journalScheme(t), Sink: sink,
				Now: func() time.Time { return journalSubmitted }}

			if _, err := r.Reconcile(context.Background(), journalReq()); err != nil {
				t.Fatalf("Reconcile: %v", err)
			}
			if len(sink.Entries()) != 0 {
				t.Fatalf("a record in %s was exported", phase)
			}
			if got := getRecord(t, c); got.Status.Exported != nil {
				t.Fatalf("a record in %s was marked exported; retention could then delete an action that is still running", phase)
			}
		})
	}
}

func TestJournalReconcilerDoesNotConfirmWhenTheSinkRefuses(t *testing.T) {
	// The whole safety argument rests on this. If a failing export still set `confirmed`, the
	// retention controller would delete the CR on schedule and the evidence would exist nowhere.
	ar := terminalRecord(t, agentv1alpha1.PhaseVerified)
	c := journalClient(t, ar)
	sink := &journal.MemorySink{Err: errors.New("the bucket is unreachable")}
	r := &controller.JournalReconciler{Client: c, Scheme: journalScheme(t), Sink: sink,
		Now: func() time.Time { return journalSubmitted }}

	if _, err := r.Reconcile(context.Background(), journalReq()); err == nil {
		t.Fatal("Reconcile succeeded despite the sink refusing; the failure would never be retried")
	}
	got := getRecord(t, c)
	if got.Status.Exported != nil && got.Status.Exported.Confirmed {
		t.Fatal("a failed export was confirmed; the record would be garbage-collected with no durable copy anywhere (05 §1.2)")
	}
}

func TestJournalReconcilerWithNoSinkKeepsEverything(t *testing.T) {
	// A cluster with no configured sink must degrade towards keeping evidence, not losing it. The
	// visible symptom is records piling up past their TTL, which is the right way round: it is
	// noticeable and it is recoverable.
	c := journalClient(t, terminalRecord(t, agentv1alpha1.PhaseVerified))
	r := &controller.JournalReconciler{Client: c, Scheme: journalScheme(t),
		Now: func() time.Time { return journalSubmitted }}

	if _, err := r.Reconcile(context.Background(), journalReq()); err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	if got := getRecord(t, c); got.Status.Exported != nil {
		t.Fatal("a record was confirmed exported with no sink configured")
	}
}

func TestJournalReconcilerRepairsTheStatusLabel(t *testing.T) {
	// status.phase and the `kube-agents/status` label are written by separate API calls, because
	// metadata and status are separate subresources. A crash between them leaves the index
	// disagreeing with the truth -- and a `/kage pending` that returns nothing then looks exactly
	// like a quiet cluster.
	ar := terminalRecord(t, agentv1alpha1.PhaseVerified)
	ar.Labels[journal.StatusLabel] = string(agentv1alpha1.PhaseExecuting)
	c := journalClient(t, ar)
	sink := &journal.MemorySink{}
	r := &controller.JournalReconciler{Client: c, Scheme: journalScheme(t), Sink: sink,
		Now: func() time.Time { return journalSubmitted }}

	res, err := r.Reconcile(context.Background(), journalReq())
	if err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	got := getRecord(t, c)
	if got.Labels[journal.StatusLabel] != string(agentv1alpha1.PhaseVerified) {
		t.Fatalf("%s = %q, want status.phase to win", journal.StatusLabel, got.Labels[journal.StatusLabel])
	}
	// The repair is a Patch, which bumps resourceVersion. If the reconciler keeps its stale copy,
	// the status update in the same pass conflicts and requeues -- the record still exports, one
	// wasted round trip later, and nothing reports a problem. Asserting on the FIRST pass is what
	// makes that visible.
	if res.Requeue {
		t.Fatal("the label repair conflicted with the status update in the same pass")
	}
	if got.Status.Exported == nil || !got.Status.Exported.Confirmed {
		t.Fatal("repairing the label cost the record its export on this pass")
	}
	if len(sink.Entries()) != 1 {
		t.Fatalf("%d entries exported", len(sink.Entries()))
	}
}

func TestRetentionReconcilerKeepsUnexportedRecordsPastTTL(t *testing.T) {
	// The composed property, in the order it actually happens: a terminal record ages past its TTL
	// while the exporter is stuck. Deleting it here is the difference between garbage collection and
	// data loss, and from inside the controller the two look identical.
	c := journalClient(t, terminalRecord(t, agentv1alpha1.PhaseVerified))
	r := &controller.RetentionReconciler{Client: c, Scheme: journalScheme(t),
		Now: func() time.Time { return pastTTL }}

	res, err := r.Reconcile(context.Background(), journalReq())
	if err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	if res.RequeueAfter == 0 {
		t.Fatal("a retained record was not re-checked; a TTL elapsing produces no watch event, so nothing would ever look at it again")
	}
	getRecord(t, c) // fatals if it was deleted
}

func TestRetentionReconcilerDeletesOnlyAfterExportAndExpiry(t *testing.T) {
	ctx := context.Background()
	scheme := journalScheme(t)

	t.Run("exported but not yet expired", func(t *testing.T) {
		c := journalClient(t, terminalRecord(t, agentv1alpha1.PhaseVerified))
		if _, err := (&controller.JournalReconciler{Client: c, Scheme: scheme, Sink: &journal.MemorySink{},
			Now: func() time.Time { return journalSubmitted }}).Reconcile(ctx, journalReq()); err != nil {
			t.Fatalf("export: %v", err)
		}
		if _, err := (&controller.RetentionReconciler{Client: c, Scheme: scheme,
			Now: func() time.Time { return journalSubmitted.Add(24 * time.Hour) }}).Reconcile(ctx, journalReq()); err != nil {
			t.Fatalf("retention: %v", err)
		}
		getRecord(t, c) // fatals if it was deleted a day into a 30-day TTL
	})

	t.Run("exported and expired", func(t *testing.T) {
		c := journalClient(t, terminalRecord(t, agentv1alpha1.PhaseVerified))
		sink := &journal.MemorySink{}
		if _, err := (&controller.JournalReconciler{Client: c, Scheme: scheme, Sink: sink,
			Now: func() time.Time { return journalSubmitted }}).Reconcile(ctx, journalReq()); err != nil {
			t.Fatalf("export: %v", err)
		}
		if _, err := (&controller.RetentionReconciler{Client: c, Scheme: scheme,
			Now: func() time.Time { return pastTTL }}).Reconcile(ctx, journalReq()); err != nil {
			t.Fatalf("retention: %v", err)
		}
		var ar agentv1alpha1.ActionRecord
		err := c.Get(ctx, types.NamespacedName{Namespace: journalNS, Name: journalRecord}, &ar)
		if !apierrors.IsNotFound(err) {
			t.Fatalf("a terminal, exported, expired record survived garbage collection (err=%v)", err)
		}
		// ...and the evidence is still where the export said it was.
		if len(sink.Entries()) != 1 {
			t.Fatal("the record is gone and the sink is empty")
		}
	})

	t.Run("in flight past its TTL", func(t *testing.T) {
		// A record cannot legitimately be Executing 31 days later, but if one is, deleting it strands
		// a running action with no journal and forces the broker to fail closed on its own
		// bookkeeping.
		ar := terminalRecord(t, agentv1alpha1.PhaseExecuting)
		at := metav1.NewTime(journalSubmitted)
		ar.Status.Exported = &agentv1alpha1.ExportStatus{Confirmed: true, At: &at, Sink: "memory"}
		c := journalClient(t, ar)
		if _, err := (&controller.RetentionReconciler{Client: c, Scheme: scheme,
			Now: func() time.Time { return pastTTL }}).Reconcile(ctx, journalReq()); err != nil {
			t.Fatalf("retention: %v", err)
		}
		getRecord(t, c)
	})
}

func TestRetentionReconcilerIgnoresAVanishedRecord(t *testing.T) {
	// Two managers, or a resync racing a delete. A NotFound must not be an error, or the queue
	// backs off on a record that is already gone.
	c := journalClient(t)
	res, err := (&controller.RetentionReconciler{Client: c, Scheme: journalScheme(t),
		Now: func() time.Time { return pastTTL }}).Reconcile(context.Background(), journalReq())
	if err != nil {
		t.Fatalf("Reconcile on a missing record: %v", err)
	}
	if res.RequeueAfter != 0 {
		t.Fatal("a missing record was scheduled for another look")
	}
}
