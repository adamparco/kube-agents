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

package notify

import (
	"context"
	"fmt"

	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

// Reconciler is the approval notifier (chat-approval.md §2). It has NO write RBAC on ActionRecord
// at all — get/list/watch only — so this file contains no Status().Patch, no Update, nothing that
// touches the record it watches. Everything it changes lives in its own Deliverers and its own
// Store.
//
// Deliberately NOT annotated with +kubebuilder:rbac markers: those compose into the single
// aggregate `manager-role` ClusterRole (`make manifests` scans every package), and a marker here
// would hand the OPERATOR's manager identity read access to a CRD it does not otherwise touch. The
// notifier's grant is hand-written, the way config/rbac/brake_role.yaml is, and lives at
// config/chatops-gateway/rbac.yaml alongside the gateway's.
type Reconciler struct {
	client.Client
	Deliverers Deliverers
	Store      Store
}

// Reconcile implements the notifier's whole job: resolve the roster and the delivery target, then
// deliver-or-edit depending on whether the record is still asking or has resolved.
func (r *Reconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	ar := &agentv1alpha1.ActionRecord{}
	if err := r.Get(ctx, req.NamespacedName, ar); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	// Only records that have been, or still are, gated are this reconciler's business. A record
	// that never reached PendingApproval never had a roster consulted and has nothing to notify.
	if !relevantPhase(ar.Status.Phase) {
		return ctrl.Result{}, nil
	}

	recordName := journal.RecordName(ar.Spec.ActionID)

	roster, unusableReason := approval.ResolveRoster(ctx, r.Client, ar)
	if roster == nil {
		// Roster-unusable: send nothing, and if something was somehow delivered earlier under a
		// roster that has since become unusable (e.g. deleted), there is no channel left to edit —
		// chat-approval.md sequence 4 is explicit that this path opens nothing and notifies nobody.
		log.V(1).Info("approval roster unusable; not notifying", "record", recordName, "reason", unusableReason)
		return ctrl.Result{}, nil
	}

	target, hasTarget := ResolveTarget(roster)
	if !hasTarget {
		log.V(1).Info("roster has no notify destination; not notifying", "record", recordName)
		return ctrl.Result{}, nil
	}

	message := approval.RenderMessage(ar, roster)
	key := deliveryKey(ar)

	state, delivered, err := r.Store.Get(ctx, recordName)
	if err != nil {
		return ctrl.Result{}, err
	}

	if delivered && state.Key == key {
		return ctrl.Result{}, nil // already shows exactly this content
	}

	if !delivered {
		ref, err := r.Deliverers.Deliver(ctx, target, message)
		if err != nil {
			return ctrl.Result{}, fmt.Errorf("notify: delivering %s: %w", recordName, err)
		}
		return ctrl.Result{}, r.Store.Save(ctx, recordName, DeliveryState{
			Platform: target.Platform, Channel: target.Channel, Ref: ref, Key: key,
		})
	}

	if err := r.Deliverers.Update(ctx, target, state.Ref, message); err != nil {
		return ctrl.Result{}, fmt.Errorf("notify: updating %s: %w", recordName, err)
	}
	state.Key = key
	return ctrl.Result{}, r.Store.Save(ctx, recordName, state)
}

func relevantPhase(p agentv1alpha1.ActionPhase) bool {
	switch p {
	case agentv1alpha1.PhasePendingApproval, agentv1alpha1.PhasePending,
		agentv1alpha1.PhaseRejected, agentv1alpha1.PhaseExpired:
		return true
	default:
		return false
	}
}

// deliveryKey is the idempotence key chat-approval.md §2 specifies: "the record UID plus a
// generation of the rendered content (phase and approval counts)". A flapping watch reconciling
// the same unchanged record repeatedly produces the same key every time and is therefore a no-op
// past the first delivery.
func deliveryKey(ar *agentv1alpha1.ActionRecord) string {
	granted, rejected := 0, 0
	if ar.Status.Approvals != nil {
		granted = len(ar.Status.Approvals.Granted)
		rejected = len(ar.Status.Approvals.Rejected)
	}
	return fmt.Sprintf("%s:%s:%d:%d", ar.UID, ar.Status.Phase, granted, rejected)
}

// SetupWithManager wires the reconciler to watch ActionRecord.
func (r *Reconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&agentv1alpha1.ActionRecord{}).
		Named("approval-notifier").
		Complete(r)
}
