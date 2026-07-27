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
	"fmt"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

// JournalReconciler is `C-JR` (05 §1, 09 §5). It has exactly one job, and the job is easy to
// mistake for bookkeeping: it makes the journal DURABLE.
//
// The CR in etcd is the working copy. The export is the record that survives (05 §1.2), and the
// retention controller will not delete a record this reconciler has not confirmed. So a stalled
// exporter shows up as records piling up past their TTL -- visible, and the right way round. The
// failure this design refuses is the other one: garbage collection running on schedule against
// evidence that never became durable.
//
// It also repairs the `kube-agents/status` label, which the store writes as a second, separate call.
// A lost label write leaves status.phase authoritative and the index stale; repairing it here means
// the ChatOps reporter's label selector cannot quietly miss a record.
type JournalReconciler struct {
	client.Client
	Scheme *runtime.Scheme

	// Sink is the durable destination. Nil disables export -- which also disables retention
	// deletion, by construction, since nothing will ever be confirmed. That is the correct
	// degradation: a cluster with no configured sink keeps everything rather than losing it.
	Sink journal.AuditSink

	// ExportBudget is the 05 §1.2 60-second freshness target. A record whose phase changed longer
	// ago than this without being exported is logged as late, because a budget nobody measures is a
	// sentence in a design document.
	ExportBudget time.Duration

	// Now is injectable so the export-budget and retention logic are testable without sleeping.
	Now func() time.Time
}

// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=actionrecords,verbs=get;list;watch;update;patch;delete
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=actionrecords/status,verbs=get;update;patch

// The controller has NO `create` on actionrecords, deliberately. Records are created by the broker
// under the actor identity, and a controller that could mint them would be a way to manufacture a
// journal entry for a write that never happened -- the exact inverse of V-BRK-003. It has `delete`
// because the retention controller in this same manager needs it.

func (r *JournalReconciler) now() time.Time {
	if r.Now != nil {
		return r.Now()
	}
	return time.Now()
}

func (r *JournalReconciler) budget() time.Duration {
	if r.ExportBudget > 0 {
		return r.ExportBudget
	}
	return 60 * time.Second
}

// Reconcile exports the record's current phase and confirms it.
func (r *JournalReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	var ar agentv1alpha1.ActionRecord
	if err := r.Get(ctx, req.NamespacedName, &ar); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	if err := r.repairStatusLabel(ctx, &ar); err != nil {
		return ctrl.Result{}, err
	}

	// A record is exported when it reaches a terminal phase. Exporting every intermediate
	// transition would multiply sink volume by the length of the lifecycle for no added evidence:
	// the terminal record carries the timestamps, the applied diff, the verification result and the
	// report, so the intermediate states are reconstructable from it. The one property that would
	// be lost -- proof that a record PASSED THROUGH a phase -- is already held by the CR's own
	// resourceVersion history for as long as the CR exists, which is by definition longer than the
	// window in which an intermediate transition is interesting.
	if !journal.Terminal(ar.Status.Phase) {
		return ctrl.Result{}, nil
	}
	if ar.Status.Exported != nil && ar.Status.Exported.Confirmed {
		return ctrl.Result{}, nil
	}
	if r.Sink == nil {
		// Not an error, and not silent. Without a sink nothing is ever confirmed, so nothing is
		// ever deleted, and an operator needs to know that is why records are accumulating.
		log.Info("no audit sink configured; the record will be retained indefinitely because the export is the durable record (05 §1.2)",
			"actionId", ar.Spec.ActionID, "phase", ar.Status.Phase)
		return ctrl.Result{}, nil
	}

	now := r.now()
	if err := r.Sink.Export(ctx, journal.EntryFor(&ar, now)); err != nil {
		// Requeue with backoff. The record stays unconfirmed and therefore undeletable, which is
		// the safe state.
		return ctrl.Result{}, fmt.Errorf("export ActionRecord %s/%s to sink %q: %w", ar.Namespace, ar.Name, r.Sink.Name(), err)
	}

	confirmedAt := metav1.NewTime(now.UTC())
	ar.Status.Exported = &agentv1alpha1.ExportStatus{
		Confirmed: true,
		At:        &confirmedAt,
		Sink:      r.Sink.Name(),
	}
	if err := r.Status().Update(ctx, &ar); err != nil {
		if apierrors.IsConflict(err) {
			// Someone else wrote status between the Get and here. Re-reconcile; the export is
			// idempotent at the sink because it is keyed by action id and phase.
			return ctrl.Result{Requeue: true}, nil
		}
		return ctrl.Result{}, fmt.Errorf("confirm export on %s/%s: %w", ar.Namespace, ar.Name, err)
	}

	if late := r.exportLateness(&ar, now); late > 0 {
		log.Info("export missed the freshness budget (05 §1.2)",
			"actionId", ar.Spec.ActionID, "budget", r.budget(), "late", late.Round(time.Second))
	}
	log.V(1).Info("exported ActionRecord", "actionId", ar.Spec.ActionID, "phase", ar.Status.Phase, "sink", r.Sink.Name())
	return ctrl.Result{}, nil
}

// exportLateness reports how far past the budget the export was, measuring from the most specific
// timestamp the record carries. Falling back to creationTimestamp rather than skipping the
// measurement means a record with an incomplete timestamp block is still measured -- pessimistically,
// which is the direction that surfaces problems.
func (r *JournalReconciler) exportLateness(ar *agentv1alpha1.ActionRecord, now time.Time) time.Duration {
	anchor := ar.CreationTimestamp.Time
	if ts := ar.Status.Timestamps; ts != nil {
		for _, t := range []*metav1.Time{ts.Verified, ts.ExecutionEnded, ts.Classified, ts.Submitted} {
			if t != nil && t.After(anchor) {
				anchor = t.Time
			}
		}
	}
	if anchor.IsZero() {
		return 0
	}
	if d := now.Sub(anchor); d > r.budget() {
		return d - r.budget()
	}
	return 0
}

// repairStatusLabel brings `kube-agents/status` back in step with status.phase. The two are written
// by separate API calls -- metadata and status are separate subresources -- so a crash between them
// leaves an index that disagrees with the truth. status.phase always wins.
func (r *JournalReconciler) repairStatusLabel(ctx context.Context, ar *agentv1alpha1.ActionRecord) error {
	if ar.Status.Phase == "" {
		return nil
	}
	want := string(ar.Status.Phase)
	if ar.Labels[journal.StatusLabel] == want {
		return nil
	}
	patched := ar.DeepCopy()
	if patched.Labels == nil {
		patched.Labels = map[string]string{}
	}
	patched.Labels[journal.StatusLabel] = want
	if err := r.Patch(ctx, patched, client.MergeFrom(ar)); err != nil {
		return fmt.Errorf("repair %s label on %s/%s: %w", journal.StatusLabel, ar.Namespace, ar.Name, err)
	}
	// Adopt the WHOLE patched object, not just the labels. The patch bumped resourceVersion, and
	// leaving the caller's copy at the old one makes the status update immediately below conflict
	// every single time a label needed repairing. That conflict is handled -- it requeues -- so the
	// record does eventually export, one wasted reconcile later, and nothing anywhere reports a
	// problem. Exactly the kind of correct-but-never-first-time behaviour that survives review.
	patched.DeepCopyInto(ar)
	return nil
}

// SetupWithManager registers the reconciler.
func (r *JournalReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&agentv1alpha1.ActionRecord{}).
		Named("journal").
		Complete(r)
}
