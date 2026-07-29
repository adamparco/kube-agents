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

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/event"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/predicate"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// BrakeReconciler is `C-BR`, the brake surface (05 §1.5). It is the FAN-OUT half of rung 5 of the
// 04 §5.1 ladder: the broker records an escalation it is structurally incapable of carrying out,
// and this controller carries it out.
//
// The split is not stylistic. 06 §2.2.1 gives the broker's operations grant `get, list, watch` on
// `agents` and no verb at all on `events` — it cannot pause an agent and it cannot page —
// and V-BRK-013 asserts that grant exactly and is BLOCKING-ALWAYS, so "let the broker pause
// directly" is not a shape an implementation may reach for. What the broker CAN write is
// `actionrecords/status`, which it already must. So the seam between the two halves is a FIELD,
// `status.escalation`, and this reconciler is its only reader.
//
// It fans out through the single stop path 05 §1.7 already names — "it calls C-BR, which performs
// the actual pause by patching `spec.operations.paused: true` with `pauseReason`… the same field a
// human uses, so `resume` is unchanged and there is no separate anomaly state to clear". There is
// deliberately no second way to stop an agent here: the anomaly detector (`C-AD`) and a failed
// rollback arrive at the same field through the same code.
//
// # What "the fan-out has not run yet" means
//
// The three fulfilment fields (`pagedAt`, `pausedAt`, `failure`) are written once, and their
// ABSENCE is load-bearing: a request with no fulfilment is a visible, queryable defect, which is
// the property that makes rung 5 auditable rather than aspirational. So this controller is careful
// about which errors it records and which it retries:
//
//   - A retryable error (conflict, timeout, anything transient) is RETURNED, so controller-runtime
//     backs off and tries again, and the record keeps saying "not yet". Writing `failure` for a
//     conflict would freeze an agent in the un-paused state over one lost optimistic-concurrency
//     race, which is the exact opposite of what a brake is for.
//   - A terminal error (the Agent is gone, admission refused the write, RBAC refused it) is
//     RECORDED in `failure`, because retrying it forever produces no pause and no signal.
//
// # Ordering
//
// Pause first, then page, then record. Stopping the agent is the load-bearing half — 05 §1.5's
// row for this case is "the world is in a state the system did not intend and cannot restore.
// Every further action compounds unknown state" — so if the process dies mid-fan-out, an agent
// that is paused but unpaged is strictly better than one that is paged but still acting. Dying
// before the receipt is written is safe in both directions: the pause is idempotent and the next
// reconcile finishes the job.
type BrakeReconciler struct {
	client.Client
	Scheme *runtime.Scheme

	// Recorder emits the page. An Event is the page EMISSION, not its delivery to a human: 05 §1.7
	// routes the human-facing page through the C15 router, and that outbound leg does not exist yet
	// (there is no implementation of `pipeline.Approvals.Notify` anywhere in the tree either). What
	// an Event buys today is a durable, cluster-native, RBAC-governed record that something asked
	// for a human, sitting on the object an operator is already looking at — and a consumer, since
	// `cmd/k8s-event-watcher` already forwards cluster Events onward. `pagedAt` therefore means
	// "the page was emitted", and the doc comment on ActionEscalation.PagedAt is what it is measured
	// against. Nothing here may be read as a claim that a human was reached.
	Recorder record.EventRecorder

	// Now is injectable so the fulfilment timestamps are assertable.
	Now func() time.Time
}

// EventReasonEscalated is the Event reason for a rung-5 page. One constant because a check that
// greps for the page has to grep for something stable.
const EventReasonEscalated = "AgentEscalated"

// maxPauseReason mirrors the +kubebuilder:validation:MaxLength on
// `Agent.spec.operations.pauseReason`. The escalation's own `reason` carries the same bound, so
// truncation here should never trigger — but "should never" plus an unbounded copy between two
// bounded fields is how a pause fails validation and does not happen.
const maxPauseReason = 512

// maxFanoutFailure mirrors the +kubebuilder:validation:MaxLength on ActionEscalation.Failure.
const maxFanoutFailure = 1024

func (r *BrakeReconciler) now() time.Time {
	if r.Now != nil {
		return r.Now()
	}
	return time.Now()
}

// Reconcile fans one recorded escalation out into a pause and a page.
//
// The RBAC this needs is NOT declared with kubebuilder markers here, and that is deliberate: the
// markers in this package all compose into the one operator ClusterRole, and C-BR must not share an
// identity with the journal exporter. `vap-agent-scope-journal` denies `status.escalation` to the
// exporter precisely because the exporter is the one principal whose write unlocks deletion of the
// record. C-BR gets its own ServiceAccount, its own grant and its own Deployment; that is
// P9-T7c-3c-ii-b-2-b, and until it lands this controller is not wired into any manager.
func (r *BrakeReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	var ar agentv1alpha1.ActionRecord
	if err := r.Get(ctx, req.NamespacedName, &ar); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	esc := ar.Status.Escalation
	if !fanoutPending(esc) {
		return ctrl.Result{}, nil
	}

	now := r.now()
	fulfilment := agentv1alpha1.ActionEscalation{}

	agent, terminal, err := r.resolveAgent(ctx, &ar)
	switch {
	case err != nil:
		return ctrl.Result{}, err
	case terminal != "":
		// No agent to pause and none to hang the Event on. Recording the failure is the whole
		// value of this branch: an escalation that silently evaporated because the Agent CR was
		// deleted mid-incident is indistinguishable from one that was never requested.
		fulfilment.Failure = terminal
	default:
		if esc.PauseRequested {
			pausedAt, why, err := r.pause(ctx, agent, esc.Reason, now)
			switch {
			case err != nil:
				return ctrl.Result{}, err
			case why != "":
				fulfilment.Failure = why
			default:
				fulfilment.PausedAt = pausedAt
			}
		}
		// A pause that failed does not cancel the page. The two are separate responses in 05 §1.5's
		// auto-brake table, and an un-pausable agent is MORE worth telling a human about, not less.
		if esc.PageRequested {
			r.page(agent, &ar, esc, fulfilment.Failure)
			fulfilment.PagedAt = &metav1.Time{Time: now}
		}
	}

	if err := r.recordFulfilment(ctx, &ar, fulfilment); err != nil {
		return ctrl.Result{}, err
	}

	log.Info("fanned out a rung-5 escalation",
		"actionId", ar.Spec.ActionID,
		"agent", ar.Spec.AgentRef.Namespace+"/"+ar.Spec.AgentRef.Name,
		"pauseRequested", esc.PauseRequested,
		"pageRequested", esc.PageRequested,
		"paused", fulfilment.PausedAt != nil,
		"paged", fulfilment.PagedAt != nil,
		"failure", fulfilment.Failure)
	return ctrl.Result{}, nil
}

// fanoutPending is the whole trigger condition, in one place so the watch predicate and Reconcile
// cannot disagree about what "still owed" means.
//
// A record with ANY fulfilment field set is done, including one that only has `failure`. Requests
// are one-shot (04 §5's ladder is non-decreasing, so a second escalation on the same record is a
// caller bug, not a retry) and a `failure` that got re-attempted on every resync would page a human
// once per resync interval.
func fanoutPending(esc *agentv1alpha1.ActionEscalation) bool {
	if esc == nil {
		return false
	}
	if !esc.PageRequested && !esc.PauseRequested {
		return false
	}
	return esc.PagedAt == nil && esc.PausedAt == nil && esc.Failure == ""
}

// resolveAgent reads the Agent named by the record's immutable `spec.agentRef`.
//
// By reference and not by re-deriving from `spec.agentIdentity`: the identity is `<tier>/<leaf>`,
// which is unique within a cluster but is a JOIN KEY, not an address, and resolving it would mean
// listing every Agent and picking one. `agentRef` is required, immutable, and was written by the
// broker that already had the Agent in hand.
//
// The three returns are (agent, terminal-reason, retryable-error) and exactly one is ever set.
func (r *BrakeReconciler) resolveAgent(ctx context.Context, ar *agentv1alpha1.ActionRecord) (*agentv1alpha1.Agent, string, error) {
	ref := ar.Spec.AgentRef
	var agent agentv1alpha1.Agent
	err := r.Get(ctx, client.ObjectKey{Namespace: ref.Namespace, Name: ref.Name}, &agent)
	switch {
	case err == nil:
		return &agent, "", nil
	case apierrors.IsNotFound(err):
		return nil, fmt.Sprintf("agent %s/%s not found: nothing to pause", ref.Namespace, ref.Name), nil
	default:
		return nil, "", fmt.Errorf("resolve agent %s/%s: %w", ref.Namespace, ref.Name, err)
	}
}

// pause patches `spec.operations.paused: true` and carries the escalation's reason across.
//
// An agent that is ALREADY paused is not re-paused and its reason is not rewritten. The reason on
// the spec is what the human running `resume` reads, and overwriting a human's "paused during the
// migration" with an automated string would delete the more informative of the two. The
// postcondition — this agent is stopped — holds either way, so `pausedAt` is still returned.
func (r *BrakeReconciler) pause(ctx context.Context, agent *agentv1alpha1.Agent, reason string, now time.Time) (*metav1.Time, string, error) {
	if paused, _, _ := agent.Spec.Operations.Brake(); paused {
		return &metav1.Time{Time: now}, "", nil
	}

	base := agent.DeepCopy()
	if agent.Spec.Operations == nil {
		agent.Spec.Operations = &agentv1alpha1.OperationsSpec{}
	}
	paused := true
	agent.Spec.Operations.Paused = &paused
	agent.Spec.Operations.PauseReason = truncateRunes(reason, maxPauseReason)

	// A merge patch and not an Update: the escalation touches two fields of a spec that a human may
	// be editing at the same moment, and losing their edit to a full-object write from a stale read
	// is a real way for an automated brake to become a source of incidents.
	if err := r.Patch(ctx, agent, client.MergeFrom(base)); err != nil {
		if apierrors.IsInvalid(err) || apierrors.IsForbidden(err) {
			return nil, truncateRunes(fmt.Sprintf("pause refused for agent %s/%s: %v", agent.Namespace, agent.Name, err), maxFanoutFailure), nil
		}
		return nil, "", fmt.Errorf("pause agent %s/%s: %w", agent.Namespace, agent.Name, err)
	}
	return &metav1.Time{Time: now}, "", nil
}

// page emits the Event. It returns nothing because an EventRecorder returns nothing: emission is
// asynchronous and best-effort by construction, so there is no error here to honestly report.
// That is precisely why the failure of the PAUSE is folded into the message rather than left to be
// inferred — the Event is the thing a human sees first, and "we could not stop it either" is the
// part they need in the first line.
func (r *BrakeReconciler) page(agent *agentv1alpha1.Agent, ar *agentv1alpha1.ActionRecord, esc *agentv1alpha1.ActionEscalation, pauseFailure string) {
	outcome := "the agent was not asked to stop"
	switch {
	case pauseFailure != "":
		outcome = "AND THE AGENT COULD NOT BE STOPPED: " + pauseFailure
	case esc.PauseRequested:
		outcome = "the agent has been paused"
	}
	r.Recorder.Eventf(agent, corev1.EventTypeWarning, EventReasonEscalated,
		"action %s escalated to a human: %s — %s (ActionRecord %s/%s)",
		ar.Spec.ActionID, esc.Reason, outcome, ar.Namespace, ar.Name)
}

// recordFulfilment writes the receipt. It re-reads the record rather than patching from the copy
// Reconcile has held across two API calls, because the broker or the exporter may have written
// status in between and this write must not carry a stale version of their fields back.
//
// One status write, and the request half is never touched: `vap-agent-scope-journal` denies C-BR
// any change to `pageRequested`, `pauseRequested`, `reason` or `requestedAt`, so a mutation that
// dragged them along would be rejected by admission rather than silently accepted.
func (r *BrakeReconciler) recordFulfilment(ctx context.Context, ar *agentv1alpha1.ActionRecord, fulfilment agentv1alpha1.ActionEscalation) error {
	var fresh agentv1alpha1.ActionRecord
	if err := r.Get(ctx, client.ObjectKeyFromObject(ar), &fresh); err != nil {
		return client.IgnoreNotFound(err)
	}
	if !fanoutPending(fresh.Status.Escalation) {
		// Somebody else finished it between the read at the top of Reconcile and here. Not an
		// error, and not something to overwrite.
		return nil
	}

	base := fresh.DeepCopy()
	fresh.Status.Escalation.PagedAt = fulfilment.PagedAt
	fresh.Status.Escalation.PausedAt = fulfilment.PausedAt
	fresh.Status.Escalation.Failure = truncateRunes(fulfilment.Failure, maxFanoutFailure)

	if err := r.Status().Patch(ctx, &fresh, client.MergeFrom(base)); err != nil {
		return fmt.Errorf("record escalation fulfilment on %s/%s: %w", fresh.Namespace, fresh.Name, err)
	}
	return nil
}

// truncateRunes cuts to n RUNES, not bytes: the API server's MaxLength counts characters, and a
// byte slice through a multi-byte rune produces invalid UTF-8 that the server then rejects — a
// truncation that exists to prevent a rejection causing one.
func truncateRunes(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n])
}

// SetupWithManager registers the reconciler, filtered to records that actually owe a fan-out.
//
// The predicate is not an optimisation. Without it every ActionRecord in the cluster wakes this
// controller on every status write the broker makes, which for a journal is most writes in the
// system.
func (r *BrakeReconciler) SetupWithManager(mgr ctrl.Manager) error {
	if r.Recorder == nil {
		// Refused at wiring time rather than nil-panicking inside a reconcile, and refused rather
		// than defaulted, because a C-BR that silently cannot page is a rung-5 escalation that
		// half-happens and says nothing about it.
		return fmt.Errorf("BrakeReconciler.Recorder is nil: C-BR cannot page without an event recorder")
	}
	return ctrl.NewControllerManagedBy(mgr).
		For(&agentv1alpha1.ActionRecord{}, builder.WithPredicates(escalationPendingPredicate())).
		Named("brake").
		Complete(r)
}

func escalationPendingPredicate() predicate.Predicate {
	pending := func(o client.Object) bool {
		ar, ok := o.(*agentv1alpha1.ActionRecord)
		return ok && fanoutPending(ar.Status.Escalation)
	}
	return predicate.Funcs{
		CreateFunc:  func(e event.CreateEvent) bool { return pending(e.Object) },
		UpdateFunc:  func(e event.UpdateEvent) bool { return pending(e.ObjectNew) },
		DeleteFunc:  func(event.DeleteEvent) bool { return false },
		GenericFunc: func(e event.GenericEvent) bool { return pending(e.Object) },
	}
}
