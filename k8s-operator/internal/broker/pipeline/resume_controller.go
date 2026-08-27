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

package pipeline

import (
	"context"
	"fmt"
	"time"

	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval"
)

// ResumeController watches ActionRecord for the two things nothing else does: an approval that has
// arrived (phase Pending, reached only via the ChatOps gateway's threshold write) and a deadline
// that has passed (chat-approval.md §3: "the resumption loop... owns the TTL clock"). It runs
// inside the broker, under the broker's own identity — it is the only thing named in the doc that
// does, both other pieces run as the gateway.
type ResumeController struct {
	client.Client
	Pipeline *Pipeline
	Records  RecordStore

	// Now is injectable for tests; defaults to time.Now.
	Now func() time.Time
}

func (r *ResumeController) now() time.Time {
	if r.Now != nil {
		return r.Now()
	}
	return time.Now()
}

func (r *ResumeController) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	ar := &agentv1alpha1.ActionRecord{}
	if err := r.Get(ctx, req.NamespacedName, ar); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	switch ar.Status.Phase {
	case agentv1alpha1.PhasePending:
		// Reached via a fresh Create only for a DryRunOnly-forced routine action (buildRecord sets
		// the initial phase from the classifier, and stepGate only parks class >= gated) — never
		// for anything with an approvals block, since only the gateway ever populates one. Nothing
		// but an approved gate reaches Pending WITH approvals, so that presence is what this
		// reconciler treats as "resume", not the bare phase.
		if ar.Status.Approvals == nil {
			return ctrl.Result{}, nil
		}
		res, err := r.Pipeline.Resume(ctx, ar)
		if err != nil {
			return ctrl.Result{}, fmt.Errorf("resume: %s: %w", ar.Name, err)
		}
		log.Info("resumed approved action", "record", ar.Name, "decision", res.Decision, "phase", res.Phase)
		return ctrl.Result{}, nil

	case agentv1alpha1.PhasePendingApproval:
		deadline, err := r.deadline(ctx, ar)
		if err != nil {
			log.Error(err, "computing approval deadline; requeueing rather than guessing", "record", ar.Name)
			return ctrl.Result{RequeueAfter: time.Minute}, nil
		}
		if r.now().Before(deadline) {
			return ctrl.Result{RequeueAfter: deadline.Sub(r.now())}, nil
		}
		if err := r.Records.SetPhase(ctx, ar, agentv1alpha1.PhaseExpired, "the approval window closed with insufficient approvals"); err != nil {
			return ctrl.Result{}, fmt.Errorf("resume: expiring %s: %w", ar.Name, err)
		}
		return ctrl.Result{}, nil

	default:
		return ctrl.Result{}, nil
	}
}

// deadline is the effective expiry instant, computed independently of whether the ChatOps gateway
// has ever written status.approvals — only the gateway may write that block (VAP validation 2), so
// a record nobody has tried to approve yet would never expire if this reconciler only read it.
// Falling back to the roster's TTL, or DefaultApprovalTTL when the roster itself is unusable,
// anchored on the record's own creationTimestamp — the one clock nobody, including this
// reconciler, can move — is what makes "a missing roster is never an open gate" true all the way
// to termination rather than just to "never notified".
func (r *ResumeController) deadline(ctx context.Context, ar *agentv1alpha1.ActionRecord) (time.Time, error) {
	if ar.Status.Approvals != nil && ar.Status.Approvals.ExpiresAt != nil {
		return ar.Status.Approvals.ExpiresAt.Time, nil
	}
	ttl := agentv1alpha1.DefaultApprovalTTL
	if roster, reason := approval.ResolveRoster(ctx, r.Client, ar); roster != nil {
		ttl = roster.EffectiveTTL()
	} else {
		logf.FromContext(ctx).V(1).Info("roster unusable while computing expiry deadline; using the default TTL", "record", ar.Name, "reason", reason)
	}
	return ar.CreationTimestamp.Add(ttl), nil
}
