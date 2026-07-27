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

	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

// RetentionReconciler garbage-collects ActionRecords past their TTL, and its entire safety property
// is a single AND: terminal, exported, AND expired (05 §1.2). The predicate itself lives in
// journal.DeletableAt so this controller and any check that asserts the property read the same code
// rather than two implementations that agree today.
//
// A separate controller from `C-JR` on purpose. The exporter is edge-triggered on a phase change; a
// TTL elapsing is not an event, so retention has to be level-triggered on a timer. Folding the timer
// into the exporter would mean re-exporting every record on every sweep just to notice one was
// expired.
//
// V-REV-008 asserts the property this controller implements. Its L4 instance -- watching a real
// record actually age out over 30 days -- is a wall-clock obligation no test suite can discharge,
// which is why the predicate is pure, unit-tested against an injected clock, and shared.
type RetentionReconciler struct {
	client.Client
	Scheme *runtime.Scheme

	// Interval is how often the sweep runs. TTLs are measured in days, so a sweep more often than
	// hourly buys nothing and costs a full list of every record in the cluster.
	Interval time.Duration

	// Now is injectable: a retention test that had to wait 30 days would not be a test.
	Now func() time.Time
}

// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=actionrecords,verbs=get;list;watch;delete

func (r *RetentionReconciler) now() time.Time {
	if r.Now != nil {
		return r.Now()
	}
	return time.Now()
}

func (r *RetentionReconciler) interval() time.Duration {
	if r.Interval > 0 {
		return r.Interval
	}
	return time.Hour
}

// Reconcile is triggered per record and re-queues itself at the sweep interval. Requeueing rather
// than running a background loop keeps the work inside controller-runtime's rate limiter and its
// leader election -- two managers sweeping the same journal concurrently would race on deletes.
func (r *RetentionReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	var ar agentv1alpha1.ActionRecord
	if err := r.Get(ctx, req.NamespacedName, &ar); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	now := r.now()
	deletable, why := journal.DeletableAt(&ar, now)
	if !deletable {
		// Re-check at the interval. The record may become deletable through the passage of time
		// alone, which produces no watch event.
		log.V(2).Info("retaining ActionRecord", "actionId", ar.Spec.ActionID, "reason", why)
		return ctrl.Result{RequeueAfter: r.interval()}, nil
	}

	// Delete with a resourceVersion precondition. Between the read above and here the exporter or
	// the undo controller may have written status; deleting unconditionally would race a phase
	// transition that arrived a moment after the predicate said yes.
	if err := r.Delete(ctx, &ar, client.Preconditions{ResourceVersion: &ar.ResourceVersion}); err != nil {
		if client.IgnoreNotFound(err) == nil {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, fmt.Errorf("delete expired ActionRecord %s/%s: %w", ar.Namespace, ar.Name, err)
	}

	// The deletion is itself audit-worthy: it is the moment the CR stops being consultable and the
	// export becomes the only copy. Logging the sink name means a reader of this line knows where
	// the evidence went.
	sink := ""
	if ar.Status.Exported != nil {
		sink = ar.Status.Exported.Sink
	}
	log.Info("deleted ActionRecord past its retention TTL; the exported record remains",
		"actionId", ar.Spec.ActionID,
		"phase", ar.Status.Phase,
		"class", ar.Spec.Classification.Class,
		"expiresAt", ar.Spec.Retention.ExpiresAt.UTC().Format(time.RFC3339),
		"sink", sink)
	return ctrl.Result{}, nil
}

// SetupWithManager registers the reconciler.
func (r *RetentionReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&agentv1alpha1.ActionRecord{}).
		Named("retention").
		Complete(r)
}
