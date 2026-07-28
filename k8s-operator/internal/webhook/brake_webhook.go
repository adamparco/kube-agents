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

package webhook

import (
	"context"
	"fmt"
	"strings"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/util/validation/field"
	ctrl "sigs.k8s.io/controller-runtime"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// Admission for the three brake objects of 06 §4.4.
//
// The division of labour with the CRD schema is the important part, and it is not the usual one.
// For most objects a webhook is where the interesting validation lives. For these three it is the
// opposite: the brake has to work when the operator is down, and a validating webhook is part of
// the operator. So every rule that a brake object CANNOT be allowed to violate is CRD-level CEL --
// enums, patterns, bounds, immutability -- and holds with this process dead. `failurePolicy=Fail`
// on the webhook would be worse than useless here: it would mean that killing the operator makes it
// impossible to CREATE a FleetFreeze, turning an outage of the control plane into an outage of the
// brake. So these three webhooks are `failurePolicy=Ignore`.
//
// What is left for the webhook is the class of mistake that produces an object which is admissible,
// well-formed, and does nothing -- or does something the author did not intend. Those cannot be
// expressed in CEL because they are cross-field or cross-object, and they are worth catching
// because a brake that silently covers nobody is the worst possible failure of a brake: it reports
// healthy, and the operator who created it stops looking.

var (
	fleetFreezeGroupKind    = schema.GroupKind{Group: "kubeagents.x-k8s.io", Kind: "FleetFreeze"}
	approvalRosterGroupKind = schema.GroupKind{Group: "kubeagents.x-k8s.io", Kind: "ApprovalRoster"}
	undoRequestGroupKind    = schema.GroupKind{Group: "kubeagents.x-k8s.io", Kind: "UndoRequest"}
)

var brakelog = logf.Log.WithName("brake-resources")

// brakeNow is the clock the FleetFreeze expiry warning reads. A package variable rather than a
// direct `time.Now()` call so the test can assert the warning fires on a past timestamp and stays
// quiet on a future one without sleeping -- a warning that is only reachable by waiting is a
// warning nobody proves works.
var brakeNow = time.Now

// The operator READS every brake object and writes only their status. It holds no `update` on any
// of the three specs, for the same reason it holds none on `changepolicies`: the operator is the
// process an agent talks to, and an operator SA that could edit a FleetFreeze would put a path to
// lifting the fleet's brake behind an LLM.
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=fleetfreezes,verbs=get;list;watch
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=fleetfreezes/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=approvalrosters,verbs=get;list;watch
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=approvalrosters/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=undorequests,verbs=get;list;watch
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=undorequests/status,verbs=get;update;patch

// +kubebuilder:webhook:path=/validate-kubeagents-x-k8s-io-v1alpha1-fleetfreeze,mutating=false,failurePolicy=ignore,sideEffects=None,groups=kubeagents.x-k8s.io,resources=fleetfreezes,verbs=create;update,versions=v1alpha1,name=vfleetfreeze.kb.io,admissionReviewVersions=v1
// +kubebuilder:webhook:path=/validate-kubeagents-x-k8s-io-v1alpha1-approvalroster,mutating=false,failurePolicy=ignore,sideEffects=None,groups=kubeagents.x-k8s.io,resources=approvalrosters,verbs=create;update,versions=v1alpha1,name=vapprovalroster.kb.io,admissionReviewVersions=v1
// +kubebuilder:webhook:path=/validate-kubeagents-x-k8s-io-v1alpha1-undorequest,mutating=false,failurePolicy=ignore,sideEffects=None,groups=kubeagents.x-k8s.io,resources=undorequests,verbs=create;update,versions=v1alpha1,name=vundorequest.kb.io,admissionReviewVersions=v1

// SetupBrakeWebhooksWithManager registers validators for all three brake objects.
func SetupBrakeWebhooksWithManager(mgr ctrl.Manager) error {
	if err := ctrl.NewWebhookManagedBy(mgr).
		For(&agentv1alpha1.FleetFreeze{}).
		WithValidator(&FleetFreezeCustomValidator{}).
		Complete(); err != nil {
		return err
	}
	if err := ctrl.NewWebhookManagedBy(mgr).
		For(&agentv1alpha1.ApprovalRoster{}).
		WithValidator(&ApprovalRosterCustomValidator{}).
		Complete(); err != nil {
		return err
	}
	return ctrl.NewWebhookManagedBy(mgr).
		For(&agentv1alpha1.UndoRequest{}).
		WithValidator(&UndoRequestCustomValidator{}).
		Complete()
}

// ---------------------------------------------------------------------------------------------
// FleetFreeze
// ---------------------------------------------------------------------------------------------

// FleetFreezeCustomValidator validates FleetFreeze objects. No client: every check is a function of
// the object alone, so admission cannot be made to hang by a slow API server.
type FleetFreezeCustomValidator struct{}

var _ admission.CustomValidator = &FleetFreezeCustomValidator{}

// ValidateCreate implements admission.CustomValidator.
func (v *FleetFreezeCustomValidator) ValidateCreate(_ context.Context, obj runtime.Object) (admission.Warnings, error) {
	ff, ok := obj.(*agentv1alpha1.FleetFreeze)
	if !ok {
		return nil, fmt.Errorf("expected a FleetFreeze object but got %T", obj)
	}
	brakelog.Info("validating FleetFreeze creation", "name", ff.Name)
	return ValidateFleetFreeze(ff)
}

// ValidateUpdate implements admission.CustomValidator.
func (v *FleetFreezeCustomValidator) ValidateUpdate(_ context.Context, _, newObj runtime.Object) (admission.Warnings, error) {
	ff, ok := newObj.(*agentv1alpha1.FleetFreeze)
	if !ok {
		return nil, fmt.Errorf("expected a FleetFreeze object but got %T", newObj)
	}
	brakelog.Info("validating FleetFreeze update", "name", ff.Name)
	return ValidateFleetFreeze(ff)
}

// ValidateDelete implements admission.CustomValidator.
//
// Deleting a freeze is how a freeze is lifted -- there is no `enabled: false` -- so this must
// always be permitted. WHO may delete one is an authorization question answered by RBAC and by
// `vap-agent-scope`; refusing it here would mean a stuck freeze could only be cleared by editing
// etcd, during the incident that produced it.
func (v *FleetFreezeCustomValidator) ValidateDelete(_ context.Context, _ runtime.Object) (admission.Warnings, error) {
	return nil, nil
}

// ValidateFleetFreeze is the whole FleetFreeze validation, exported so tests and any future
// server-side loader call exactly what the API server calls.
func ValidateFleetFreeze(ff *agentv1alpha1.FleetFreeze) (admission.Warnings, error) {
	var errs field.ErrorList
	var warnings admission.Warnings

	scope := field.NewPath("spec", "scope")
	s := ff.Spec.Scope

	// A scope with a hole in it does not mean what it looks like it means. `{clusterName: c}` with
	// no projectId reads as "cluster c", and matches a cluster named `c` in EVERY project -- which
	// is either exactly right or a fleet-wide freeze the author did not intend, and the object
	// looks identical in both cases. Refused rather than warned: 06 §4.4's widening rule makes the
	// mistake silent, and a freeze is the one object whose blast radius must be legible from the
	// YAML.
	if s.ProjectID == "" && (s.ClusterName != "" || s.Namespace != "") {
		errs = append(errs, field.Invalid(scope.Child("projectId"), "",
			"a scope that names a cluster or namespace must also name the project: with projectId empty, 06 §4.4's widening rule makes this freeze match that cluster or namespace name in EVERY project. Write the projectId, or omit the narrower fields to freeze the whole fleet deliberately"))
	}
	if s.ClusterName == "" && s.Namespace != "" {
		errs = append(errs, field.Invalid(scope.Child("clusterName"), "",
			"a scope that names a namespace must also name the cluster: otherwise this freezes every namespace with that name in every cluster of the project"))
	}

	// An already-expired freeze is admissible, well-formed, and freezes nothing. It is the single
	// likeliest FleetFreeze mistake -- a copied timestamp, or a timezone read the wrong way -- and
	// the object reports healthy afterwards. A warning rather than an error because the clock is not
	// the API server's to arbitrate and a legitimate `expiresAt` a second in the past during a
	// retry loop should not be fatal.
	if ff.Spec.ExpiresAt != nil && !ff.Spec.ExpiresAt.Time.After(brakeNow()) {
		warnings = append(warnings, fmt.Sprintf(
			"spec.expiresAt (%s) is already in the past: this freeze is created expired and will hold nothing. Omit expiresAt for a freeze that never self-clears",
			ff.Spec.ExpiresAt.Time.UTC().Format("2006-01-02T15:04:05Z")))
	}

	// The empty scope is correct and is how a fleet-wide freeze is written, so it is a warning and
	// not an error -- but it is warned about, because `{}` is also what an unfilled template looks
	// like, and the two produce very different mornings.
	if s.ProjectID == "" && s.ClusterName == "" && s.Namespace == "" {
		warnings = append(warnings, "spec.scope is empty: this freezes THE ENTIRE FLEET, every agent in every project. That is the documented meaning of an empty scope (06 §4.4) -- confirm it is what you meant")
	}

	if len(errs) > 0 {
		return warnings, apierrors.NewInvalid(fleetFreezeGroupKind, ff.Name, errs)
	}
	return warnings, nil
}

// ---------------------------------------------------------------------------------------------
// ApprovalRoster
// ---------------------------------------------------------------------------------------------

// ApprovalRosterCustomValidator validates ApprovalRoster objects.
type ApprovalRosterCustomValidator struct{}

var _ admission.CustomValidator = &ApprovalRosterCustomValidator{}

// ValidateCreate implements admission.CustomValidator.
func (v *ApprovalRosterCustomValidator) ValidateCreate(_ context.Context, obj runtime.Object) (admission.Warnings, error) {
	ar, ok := obj.(*agentv1alpha1.ApprovalRoster)
	if !ok {
		return nil, fmt.Errorf("expected an ApprovalRoster object but got %T", obj)
	}
	brakelog.Info("validating ApprovalRoster creation", "name", ar.Name, "namespace", ar.Namespace)
	return ValidateApprovalRoster(ar)
}

// ValidateUpdate implements admission.CustomValidator.
func (v *ApprovalRosterCustomValidator) ValidateUpdate(_ context.Context, _, newObj runtime.Object) (admission.Warnings, error) {
	ar, ok := newObj.(*agentv1alpha1.ApprovalRoster)
	if !ok {
		return nil, fmt.Errorf("expected an ApprovalRoster object but got %T", newObj)
	}
	brakelog.Info("validating ApprovalRoster update", "name", ar.Name, "namespace", ar.Namespace)
	return ValidateApprovalRoster(ar)
}

// ValidateDelete implements admission.CustomValidator.
//
// Deleting a roster does not open a gate -- 06 §4.4's sixth fail-closed rule is that a gated action
// with no roster stays PendingApproval and expires. So the failure mode of an over-eager delete is
// that approvals stop working, which is loud, and not that they start being skipped, which is not.
func (v *ApprovalRosterCustomValidator) ValidateDelete(_ context.Context, _ runtime.Object) (admission.Warnings, error) {
	return nil, nil
}

// ValidateApprovalRoster is the whole ApprovalRoster validation.
func ValidateApprovalRoster(ar *agentv1alpha1.ApprovalRoster) (admission.Warnings, error) {
	var errs field.ErrorList
	var warnings admission.Warnings

	approversPath := field.NewPath("spec", "approvers")
	seen := make(map[string]int, len(ar.Spec.Approvers))
	for i, a := range ar.Spec.Approvers {
		// Duplicates are refused because `minApprovals` counts DISTINCT members. A roster listing
		// one person twice with `minApprovals: 2` looks like four-eyes and is one pair of eyes; the
		// alternative -- deduplicating silently -- gives the author a roster that quietly satisfies
		// a policy review it does not meet.
		p := a.Principal()
		if prev, dup := seen[p]; dup {
			errs = append(errs, field.Duplicate(approversPath.Index(i).Child("id"),
				fmt.Sprintf("%s (already listed at approvers[%d]); minApprovals counts DISTINCT principals, so a duplicate makes a roster look larger than the number of humans on it", p, prev)))
			continue
		}
		seen[p] = i
	}

	// The cross-field rule CEL cannot express, and the one that turns a gate into a wall.
	// `minApprovals: 3` on a two-person roster is not a strict policy: no set of approvals can ever
	// satisfy it, so every gated action parks and expires unreviewed. Because expiry is never an
	// approval, nothing executes and nothing complains -- the agent simply stops being able to do
	// anything gated, and the roster looks fine.
	if n := ar.EffectiveMinApprovals(); int(n) > len(seen) {
		errs = append(errs, field.Invalid(field.NewPath("spec", "minApprovals"), n,
			fmt.Sprintf("cannot exceed the number of distinct approvers (%d): no gated action could ever collect %d approvals, so every one of them would park as PendingApproval and expire unreviewed. Expiry is never an approval (06 §4.4)", len(seen), n)))
	}

	// The TTL bounds. Refused rather than clamped, unlike the runtime's EffectiveTTL, because these
	// two functions answer different questions: admission is asked "is this what you meant?" and an
	// author who wrote `ttl: 5m` did not mean 1h. The runtime is asked "what do I do with the object
	// that is already stored", where refusing to evaluate would leave the action parked forever.
	if ttl := ar.Spec.TTL; ttl != nil && ttl.Duration != 0 {
		p := field.NewPath("spec", "ttl")
		switch {
		case ttl.Duration < agentv1alpha1.MinApprovalTTL:
			errs = append(errs, field.Invalid(p, ttl.Duration.String(),
				fmt.Sprintf("below the %s floor (06 §4.4): an approver who is asleep, in a meeting, or on a plane cannot answer inside this window, so the gate would reliably expire unreviewed rather than gate", agentv1alpha1.MinApprovalTTL)))
		case ttl.Duration > agentv1alpha1.MaxApprovalTTL:
			errs = append(errs, field.Invalid(p, ttl.Duration.String(),
				fmt.Sprintf("above the %s ceiling (06 §4.4): past it the cluster state the action was classified against is no longer the state the approver is approving, so the broker must re-classify at approval time rather than trust a stale verdict", agentv1alpha1.MaxApprovalTTL)))
		}
	}

	// Self-approval plus a single approver is four-eyes with the four eyes belonging to one person.
	// Not refused -- a one-person team is real, and refusing would leave them unable to use gated
	// actions at all -- but it must not be silent, because it is indistinguishable in the YAML from
	// a roster that enforces review.
	if ar.SelfApprovalAllowed() && len(seen) == 1 {
		warnings = append(warnings, "spec.allowSelfApproval is true on a single-approver roster: the requester and the approver are necessarily the same person, so every gated action is self-approved. The gate still journals and still delays, but it is not review")
	}

	// A roster nobody can be told about. Approvals still work -- notification is delivery, not
	// authorisation, and a member who finds the request some other way can approve it -- but the
	// ask lands nowhere, so in practice actions expire while the roster waits to be asked.
	if n := ar.Spec.Notify; n == nil || (n.Slack == nil && n.GoogleChat == nil) {
		warnings = append(warnings, "spec.notify names no destination: approval requests will be recorded but not delivered anywhere. Roster members can still approve if they find the request, but in practice gated actions will expire waiting")
	}

	if len(errs) > 0 {
		return warnings, apierrors.NewInvalid(approvalRosterGroupKind, ar.Name, errs)
	}
	return warnings, nil
}

// ---------------------------------------------------------------------------------------------
// UndoRequest
// ---------------------------------------------------------------------------------------------

// UndoRequestCustomValidator validates UndoRequest objects.
type UndoRequestCustomValidator struct{}

var _ admission.CustomValidator = &UndoRequestCustomValidator{}

// ValidateCreate implements admission.CustomValidator.
func (v *UndoRequestCustomValidator) ValidateCreate(_ context.Context, obj runtime.Object) (admission.Warnings, error) {
	ur, ok := obj.(*agentv1alpha1.UndoRequest)
	if !ok {
		return nil, fmt.Errorf("expected an UndoRequest object but got %T", obj)
	}
	brakelog.Info("validating UndoRequest creation", "name", ur.Name, "namespace", ur.Namespace)
	return ValidateUndoRequest(ur)
}

// ValidateUpdate implements admission.CustomValidator.
//
// The spec is immutable by CEL, so an update here is a status-adjacent or metadata change. It is
// still validated, because CEL immutability holds the spec and this holds the rest.
func (v *UndoRequestCustomValidator) ValidateUpdate(_ context.Context, _, newObj runtime.Object) (admission.Warnings, error) {
	ur, ok := newObj.(*agentv1alpha1.UndoRequest)
	if !ok {
		return nil, fmt.Errorf("expected an UndoRequest object but got %T", newObj)
	}
	return ValidateUndoRequest(ur)
}

// ValidateDelete implements admission.CustomValidator.
//
// Deleting an UndoRequest does not un-undo anything: the undo is a separate, journaled action, and
// the record of it lives in the journal rather than here. This object is the ask, not the receipt.
func (v *UndoRequestCustomValidator) ValidateDelete(_ context.Context, _ runtime.Object) (admission.Warnings, error) {
	return nil, nil
}

// ValidateUndoRequest is the whole UndoRequest validation.
//
// Deliberately thin, and the thinness is the design. Everything an UndoRequest could get wrong that
// a webhook could catch -- a malformed principal, a missing reason, a mutated spec -- is CRD-level
// CEL, so it holds with the operator dead. Everything else needs the referenced ActionRecord, and
// reading it here would be wrong twice over: it would make undo admission depend on an API read at
// exactly the moment the cluster is unhealthy, and it would duplicate a decision the undo
// controller has to make anyway, against state that can change between the two.
//
// So an UndoRequest naming a nonexistent action is ADMITTED, and refused by the controller with
// `phase: Refused` and a message. That is the better failure: the human gets a durable object
// explaining what happened, rather than an admission error in a terminal they may not be looking at
// -- and 06 §4.4 requires undo to work through `kubectl` with everything else down, which an
// admission-time cross-object read would quietly break.
func ValidateUndoRequest(ur *agentv1alpha1.UndoRequest) (admission.Warnings, error) {
	var errs field.ErrorList
	var warnings admission.Warnings

	// The `k8s:` platform is the API brake's identity and is what a human running `kubectl` has. It
	// is accepted by the CEL pattern for exactly that reason, and warned about here because it
	// cannot be checked against the agent's chat `allowedUsers` list: the controller falls back to
	// the requester's Kubernetes RBAC, which is a different and coarser authorisation.
	if strings.HasPrefix(ur.Spec.RequestedBy, "k8s:") {
		warnings = append(warnings, "spec.requestedBy uses the k8s: platform: this identity cannot be matched against the agent's chat allowedUsers, so authorisation falls back to the requester's Kubernetes RBAC on this namespace. That is the intended path for the API brake with chat down (06 §4.4)")
	}

	if !ur.ContestedRequested() {
		warnings = append(warnings, "spec.markContested is false: the target will not be marked contested, so the agent may legitimately redo this change on its next reconcile. Leave it unset unless the change was correct and only the timing was wrong")
	}

	if len(errs) > 0 {
		return warnings, apierrors.NewInvalid(undoRequestGroupKind, ur.Name, errs)
	}
	return warnings, nil
}
