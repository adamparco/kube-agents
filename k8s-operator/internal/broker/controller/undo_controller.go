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
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/undo"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

// C-UC, the undo controller (05 §1.3).
//
// The design constraint that produces every choice in this file is one sentence from 05 §1.3: undo
// must work when the agent that made the change is "paused, asleep, or deleted". That rules out the
// obvious implementation. An undo skill inside the agent would be refused by the agent's own brake
// at the moment a human most needs it to work, because a paused agent refuses envelopes and pause is
// exactly the state a human puts an agent into before undoing its work. So undo is a control-plane
// controller with its own ServiceAccount and its own narrow grant, asking the broker on the
// REQUESTER's behalf rather than the agent's.
//
// The second constraint is the closing line of 05 §1.3: "Undo is never a raw `kubectl apply` of a
// stored snapshot." A controller that reverted the change directly would be the one write path in
// the system that skipped the broker -- unclassified, unjournaled, ungatable, and itself not
// undoable. So this controller does not touch the target objects at all beyond one advisory
// annotation. It asks the owning broker to run the recorded plan through the FULL pipeline, and the
// undo comes back as a first-class ActionRecord of its own.
//
// What is left for the controller is therefore small and entirely about bookkeeping and refusal:
// decide whether the record can bear an undo (undo.Replayable), ask, and then make the two records
// point at each other. That bookkeeping is the part that is easy to get subtly wrong, which is why
// the link state is durable rather than in-memory -- see linkPending below.

// Condition types on an UndoRequest. Three rather than one, because "we would not try", "we tried
// and it did not work" and "it worked but the paperwork is half-written" are three different things
// to a human reading the object during an incident, and collapsing them into a single Ready
// condition would make the third invisible.
const (
	// UndoConditionReplayable records the undo.Replayable verdict. False carries the refusal as its
	// reason.
	UndoConditionReplayable = "Replayable"
	// UndoConditionExecuted is true once the broker returned an undo action id.
	UndoConditionExecuted = "Executed"
	// UndoConditionLinkPending is the `undoLinkPending` flag of 05 §1.3 step 4. See linkPending.
	UndoConditionLinkPending = "UndoLinkPending"
)

// undoRequeueAfter paces the retry of a failed reverse link write. Seconds rather than the
// controller-runtime error backoff because a link that is half-written is a correctness gap a human
// may be reading right now, not a transient to be exponentially forgotten.
const undoRequeueAfter = 15 * time.Second

// ErrReplayerUnavailable is what the default Replayer returns. It is NOT terminal: a build with no
// replayer installed is a deployment problem, and writing UndoFailed for it would tell a human their
// undo was attempted and did not work, which is the opposite of true.
var ErrReplayerUnavailable = errors.New("undo: no replayer is installed in this build, so the recorded plan cannot be sent to the owning broker")

// Replayer is the seam onto 05 §1.3 steps 3 and 4 -- resolve the target agent's broker, scaling it
// up from zero and reconstituting it from the tier template if the Agent CR is gone, then
// POST /v1alpha1/actions/{actionId}/replay.
//
// An interface for the same reason broker.Pipeline is one: "the replay path is not installed" has to
// be a distinct, visible runtime state with its own error, rather than a code path that looks like
// success. The alternative -- a controller that logged a TODO and wrote UndoExecuted -- would be a
// lie in the journal, correct-looking to the requester and invisible in a test.
//
// The HTTP client half and the broker's route are P9-T7's, wired when the pipeline the route calls
// exists. Until then UnavailableReplayer is what runs, and it refuses loudly.
type Replayer interface {
	// Replay returns the action id of the new, first-class undo action the broker created. An error
	// wrapping ErrReplayRefused is terminal; anything else is retried.
	Replay(ctx context.Context, ar *agentv1alpha1.ActionRecord, req *agentv1alpha1.UndoRequest) (string, error)
}

// ErrReplayRefused marks a replay failure the broker will give the same answer to next time --
// scope, classification, a gate. Wrapping it moves the request to UndoFailed instead of retrying
// forever against a decision that will not change.
var ErrReplayRefused = errors.New("the broker refused the replay")

// UnavailableReplayer is the default. Every precondition-clean request gets a retryable error naming
// the build it is talking to.
type UnavailableReplayer struct{}

// Replay refuses.
func (UnavailableReplayer) Replay(context.Context, *agentv1alpha1.ActionRecord, *agentv1alpha1.UndoRequest) (string, error) {
	return "", ErrReplayerUnavailable
}

// UndoReconciler reconciles UndoRequest objects.
type UndoReconciler struct {
	client.Client
	Scheme *runtime.Scheme

	// Replayer talks to the owning broker. Nil means UnavailableReplayer.
	Replayer Replayer

	// Now is injectable so the undo-window boundary is testable without waiting out a TTL.
	Now func() time.Time
}

// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=undorequests,verbs=get;list;watch
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=undorequests/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=actionrecords,verbs=get;list;watch
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=actionrecords/status,verbs=get;update;patch

func (r *UndoReconciler) now() time.Time {
	if r.Now != nil {
		return r.Now()
	}
	return time.Now()
}

func (r *UndoReconciler) replayer() Replayer {
	if r.Replayer != nil {
		return r.Replayer
	}
	return UnavailableReplayer{}
}

// Reconcile drives one UndoRequest through Pending → Executing → Executed | Failed | Refused.
//
// The one non-obvious ordering: UndoConditionLinkPending is set to True in the SAME status write
// that sets UndoExecuted, and cleared only once the reverse link lands on the original record. It
// looks redundant -- the code sets it and then immediately tries to clear it -- but the redundancy
// is the point. If the process dies between the two writes, the next reconcile sees a terminal
// Executed request and would otherwise return early, leaving the 06 §4.3 linkage permanently
// one-way. 05 §1.3 names that exact failure and forbids it: the record is flagged rather than "left
// silently one-way".
func (r *UndoReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	var ur agentv1alpha1.UndoRequest
	if err := r.Get(ctx, req.NamespacedName, &ur); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	// Resume an interrupted finalize before anything else. Executed-with-link-pending is the only
	// terminal state that still has work in it.
	if ur.Status.Phase == agentv1alpha1.UndoExecuted {
		if !linkPending(&ur) {
			return ctrl.Result{}, nil
		}
		return r.finalizeLink(ctx, &ur)
	}
	if ur.Status.Phase.IsTerminal() {
		return ctrl.Result{}, nil
	}

	// Step 2 of 05 §1.3. Same namespace by construction: spec.actionRef carries no namespace field,
	// because a cross-namespace undo would let anyone who can create a CR in their own namespace
	// reverse changes in someone else's.
	var ar agentv1alpha1.ActionRecord
	arKey := types.NamespacedName{Namespace: ur.Namespace, Name: ur.Spec.ActionRef.Name}
	arErr := r.Get(ctx, arKey, &ar)
	if arErr != nil && !apierrors.IsNotFound(arErr) {
		return ctrl.Result{}, fmt.Errorf("load ActionRecord %s: %w", arKey, arErr)
	}
	found := arErr == nil

	var record *agentv1alpha1.ActionRecord
	if found {
		record = &ar
	}
	refusal, detail := undo.Replayable(record, r.now())
	if refusal != undo.ReplayAllowed {
		log.Info("refusing undo", "undoRequest", req.NamespacedName, "actionRef", ur.Spec.ActionRef.Name, "refusal", refusal.String())
		return ctrl.Result{}, r.refuse(ctx, &ur, refusal, detail)
	}

	// Mark Executing before asking. If the process dies after the broker accepted but before this
	// controller saw the answer, the next reconcile re-submits -- which is safe, and safe only
	// because the broker dedupes on the idempotency key derived from the same recorded plan. That
	// is the reason the undo goes through the broker rather than being applied here: at-least-once
	// delivery of a replay is survivable, at-least-once delivery of a raw apply is not.
	if ur.Status.Phase != agentv1alpha1.UndoExecuting {
		if err := r.patchStatus(ctx, &ur, func(u *agentv1alpha1.UndoRequest) {
			u.Status.Phase = agentv1alpha1.UndoExecuting
			u.Status.ObservedGeneration = u.Generation
			u.Status.Message = fmt.Sprintf("replaying the recorded undo plan for %s through its owning broker", ar.Spec.ActionID)
			setUndoCondition(u, UndoConditionReplayable, metav1.ConditionTrue, "PreconditionsMet", detailOrDefault(detail, "the record is terminal-and-successful, its undo plan validates, and the undo window is open"))
		}); err != nil {
			return ctrl.Result{}, err
		}
	}

	undoActionID, err := r.replayer().Replay(ctx, &ar, &ur)
	if err != nil {
		if errors.Is(err, ErrReplayRefused) {
			log.Info("the broker refused the replay; not retrying", "undoRequest", req.NamespacedName, "error", err)
			return ctrl.Result{}, r.fail(ctx, &ur, "ReplayRefused", err.Error())
		}
		// Retryable. Surface it on the object so a human does not have to read controller logs to
		// find out why an undo has been Executing for ten minutes.
		if perr := r.patchStatus(ctx, &ur, func(u *agentv1alpha1.UndoRequest) {
			setUndoCondition(u, UndoConditionExecuted, metav1.ConditionFalse, "ReplayError", err.Error())
		}); perr != nil {
			return ctrl.Result{}, perr
		}
		return ctrl.Result{}, fmt.Errorf("replay undo plan for %s: %w", ar.Spec.ActionID, err)
	}
	if undoActionID == "" {
		return ctrl.Result{}, fmt.Errorf("the replayer returned no action id for %s: an undo with no record of its own is exactly what 05 §1.3 forbids", ar.Spec.ActionID)
	}

	// Forward half of the linkage, plus the flag that guarantees the reverse half gets written.
	if err := r.patchStatus(ctx, &ur, func(u *agentv1alpha1.UndoRequest) {
		u.Status.Phase = agentv1alpha1.UndoExecuted
		u.Status.UndoActionID = undoActionID
		u.Status.ObservedGeneration = u.Generation
		u.Status.CompletionTime = ptrTime(r.now())
		u.Status.Message = fmt.Sprintf("undone by action %s", undoActionID)
		setUndoCondition(u, UndoConditionExecuted, metav1.ConditionTrue, "Replayed", fmt.Sprintf("the broker executed the reverse action as %s", undoActionID))
		setUndoCondition(u, UndoConditionLinkPending, metav1.ConditionTrue, "ReverseLinkNotYetWritten", "status.undoneBy has not been written on the original record yet")
	}); err != nil {
		return ctrl.Result{}, err
	}

	return r.finalizeLink(ctx, &ur)
}

// finalizeLink writes step 4's reverse link and step 5's contested marker on the ORIGINAL record,
// then clears the pending flag. Idempotent: every field it sets is set to the same value on a
// re-run, so a retry after a partial write converges rather than compounding.
func (r *UndoReconciler) finalizeLink(ctx context.Context, ur *agentv1alpha1.UndoRequest) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	var ar agentv1alpha1.ActionRecord
	arKey := types.NamespacedName{Namespace: ur.Namespace, Name: ur.Spec.ActionRef.Name}
	if err := r.Get(ctx, arKey, &ar); err != nil {
		if apierrors.IsNotFound(err) {
			// The original aged out between the replay and now. The forward link in the undo
			// record's spec.trigger.undoOf still names the relationship -- 06 §4.3 anticipates
			// exactly this, which is why the forward link is immutable spec on an object that
			// outlives the original. Nothing is left to write, so stop flagging.
			return ctrl.Result{}, r.patchStatus(ctx, ur, func(u *agentv1alpha1.UndoRequest) {
				setUndoCondition(u, UndoConditionLinkPending, metav1.ConditionFalse, "OriginalDeleted",
					"the original record reached its retention TTL before the reverse link was written; spec.trigger.undoOf on the undo record still names it")
			})
		}
		return ctrl.Result{RequeueAfter: undoRequeueAfter}, fmt.Errorf("reload original ActionRecord %s to write the reverse link: %w", arKey, err)
	}

	contest := ur.ContestedRequested()
	patch := client.MergeFrom(ar.DeepCopy())
	ar.Status.Phase = agentv1alpha1.PhaseUndone
	ar.Status.UndoneBy = ur.Status.UndoActionID
	if contest {
		ar.Status.Contested = true
	}
	ar.Status.Message = fmt.Sprintf("undone by action %s at the request of %s: %s",
		ur.Status.UndoActionID, ur.Spec.RequestedBy, ur.Spec.Reason)

	if err := r.Status().Patch(ctx, &ar, patch); err != nil {
		// 05 §1.3: retry, and flag rather than leave silently one-way. The flag is already set; all
		// that is needed is to keep the reason current and come back.
		if perr := r.patchStatus(ctx, ur, func(u *agentv1alpha1.UndoRequest) {
			setUndoCondition(u, UndoConditionLinkPending, metav1.ConditionTrue, "ReverseLinkWriteFailed", err.Error())
		}); perr != nil {
			return ctrl.Result{}, perr
		}
		return ctrl.Result{RequeueAfter: undoRequeueAfter}, fmt.Errorf("write reverse undo link on %s: %w", arKey, err)
	}

	if contest {
		r.annotateContested(ctx, &ar)
	}

	log.Info("undo complete",
		"actionId", ar.Spec.ActionID,
		"undoActionId", ur.Status.UndoActionID,
		"requestedBy", ur.Spec.RequestedBy,
		"contested", contest)

	return ctrl.Result{}, r.patchStatus(ctx, ur, func(u *agentv1alpha1.UndoRequest) {
		setUndoCondition(u, UndoConditionLinkPending, metav1.ConditionFalse, "Linked",
			fmt.Sprintf("status.undoneBy on %s names %s", ar.Name, u.Status.UndoActionID))
	})
}

// annotateContested stamps the advisory `kube-agents/contested: <action-id>` on each target, and
// never fails the undo when it cannot.
//
// Best-effort is not laziness here, it is 06 §4.4's own reasoning: "the index is authoritative
// because a deleted object cannot hold an annotation". The commonest contested case is a human
// undoing a create, and after that undo the target does not exist. An implementation that treated a
// missing target as an error would fail hardest on the case it was written for.
//
// A Forbidden is swallowed for a different and more deliberate reason. Stamping an arbitrary target
// requires patch on an arbitrary GVK in an arbitrary namespace, and granting C-UC that would give
// the undo controller a write reach larger than any agent's -- the precise shape 03 §3.3 rule 3
// exists to prevent. So the grant stays narrow (actionrecords + undorequests, per 06 §2), the stamp
// succeeds wherever the deployment has chosen to allow it, and where it does not, the two
// authoritative signals -- status.contested and the broker's target index -- still refuse the redo.
// The annotation is a courtesy to a human running `kubectl get -o yaml`, and courtesies do not get
// to fail brakes.
func (r *UndoReconciler) annotateContested(ctx context.Context, ar *agentv1alpha1.ActionRecord) {
	log := logf.FromContext(ctx)
	for _, t := range ar.Spec.Targets {
		obj := &unstructured.Unstructured{}
		obj.SetGroupVersionKind(schema.GroupVersionKind{Group: t.Group, Version: t.Version, Kind: t.Kind})
		obj.SetNamespace(t.Namespace)
		obj.SetName(t.Name)
		obj.SetAnnotations(map[string]string{journal.ContestedAnnotation: ar.Spec.ActionID})

		// A merge patch rather than a read-modify-write: the annotation is one key, and re-reading
		// the object first would open a window in which somebody else's edit is clobbered by a
		// courtesy.
		body := []byte(fmt.Sprintf(`{"metadata":{"annotations":{%q:%q}}}`, journal.ContestedAnnotation, ar.Spec.ActionID))
		if err := r.Patch(ctx, obj, client.RawPatch(types.MergePatchType, body)); err != nil {
			log.V(1).Info("could not stamp the advisory contested annotation; status.contested and the broker's index still hold",
				"target", journal.TargetString(t), "error", err.Error())
			continue
		}
		log.V(2).Info("stamped the advisory contested annotation", "target", journal.TargetString(t), "actionId", ar.Spec.ActionID)
	}
}

// refuse writes the terminal UndoRefused. Separate from fail so the two questions a human asks after
// an undo -- "should I try again?" and "what stopped it?" -- are answerable from the phase alone.
func (r *UndoReconciler) refuse(ctx context.Context, ur *agentv1alpha1.UndoRequest, refusal undo.ReplayRefusal, detail string) error {
	return r.patchStatus(ctx, ur, func(u *agentv1alpha1.UndoRequest) {
		u.Status.Phase = agentv1alpha1.UndoRefused
		u.Status.ObservedGeneration = u.Generation
		u.Status.CompletionTime = ptrTime(r.now())
		u.Status.Message = truncateMessage(detail)
		setUndoCondition(u, UndoConditionReplayable, metav1.ConditionFalse, conditionReason(refusal), detail)
	})
}

// fail writes the terminal UndoFailed: attempted, did not work, a retry is meaningful.
func (r *UndoReconciler) fail(ctx context.Context, ur *agentv1alpha1.UndoRequest, reason, detail string) error {
	return r.patchStatus(ctx, ur, func(u *agentv1alpha1.UndoRequest) {
		u.Status.Phase = agentv1alpha1.UndoFailed
		u.Status.ObservedGeneration = u.Generation
		u.Status.CompletionTime = ptrTime(r.now())
		u.Status.Message = truncateMessage(detail)
		setUndoCondition(u, UndoConditionExecuted, metav1.ConditionFalse, reason, detail)
	})
}

// patchStatus re-reads and patches, so a mutation is applied to the freshest object rather than to
// whatever this reconcile loaded several API calls ago. The mutation is also applied to the caller's
// copy, so a caller that keeps reading `ur` after the call sees what it just wrote.
func (r *UndoReconciler) patchStatus(ctx context.Context, ur *agentv1alpha1.UndoRequest, mutate func(*agentv1alpha1.UndoRequest)) error {
	var fresh agentv1alpha1.UndoRequest
	key := types.NamespacedName{Namespace: ur.Namespace, Name: ur.Name}
	if err := r.Get(ctx, key, &fresh); err != nil {
		return client.IgnoreNotFound(err)
	}
	patch := client.MergeFrom(fresh.DeepCopy())
	mutate(&fresh)
	if err := r.Status().Patch(ctx, &fresh, patch); err != nil {
		return fmt.Errorf("patch UndoRequest %s status: %w", key, err)
	}
	fresh.Status.DeepCopyInto(&ur.Status)
	return nil
}

// linkPending reports whether the reverse half of the 06 §4.3 linkage is still owed.
func linkPending(ur *agentv1alpha1.UndoRequest) bool {
	return meta.IsStatusConditionTrue(ur.Status.Conditions, UndoConditionLinkPending)
}

func setUndoCondition(ur *agentv1alpha1.UndoRequest, condType string, status metav1.ConditionStatus, reason, message string) {
	meta.SetStatusCondition(&ur.Status.Conditions, metav1.Condition{
		Type:               condType,
		Status:             status,
		Reason:             reason,
		Message:            truncateMessage(message),
		ObservedGeneration: ur.Generation,
	})
}

// conditionReason turns a kebab-case refusal into the CamelCase a Condition reason is constrained
// to. The refusal string itself stays in the message, because that is the value 06 §4.4 names and
// the one a script should match on.
func conditionReason(refusal undo.ReplayRefusal) string {
	switch refusal {
	case undo.RefuseNoRecord:
		return "ActionRecordMissing"
	case undo.RefuseAlreadyUndone:
		return "AlreadyUndone"
	case undo.RefuseNotExecuted:
		return "ActionNotExecuted"
	case undo.RefusePlanUnusable:
		return "UndoPlanUnusable"
	case undo.RefuseWindowExpired:
		return "UndoWindowExpired"
	default:
		return "Refused"
	}
}

// truncateMessage keeps a message inside the 1024-character bound the CRD puts on
// status.message, so a long ValidateReplayable detail is shortened rather than rejected -- losing
// the tail of an explanation beats losing the whole status write.
func truncateMessage(s string) string {
	const max = 1024
	if len(s) <= max {
		return s
	}
	return s[:max-3] + "..."
}

func detailOrDefault(detail, fallback string) string {
	if detail != "" {
		return detail
	}
	return fallback
}

func ptrTime(t time.Time) *metav1.Time {
	mt := metav1.NewTime(t.UTC())
	return &mt
}

// SetupWithManager registers the reconciler.
func (r *UndoReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&agentv1alpha1.UndoRequest{}).
		Named("undo").
		Complete(r)
}
