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

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/util/validation/field"
	ctrl "sigs.k8s.io/controller-runtime"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/classify"
)

// The `ChangePolicy` admission webhook (V-GAT-009).
//
// It enforces exactly one property that the broker does not already enforce by construction: that a
// policy which cannot work is refused rather than stored. The broker maxes over sources and mins
// over caps, so no admitted policy can loosen anything -- but "cannot loosen" and "the operator
// understands what this policy does" are different claims, and only the second needs a webhook.
// Everything here is therefore phrased as a message to the person who wrote the YAML.
//
// What is NOT here, and where it lives instead:
//
//   - "no agent identity may write a ChangePolicy" is RBAC plus `vap-agent-scope` plus the broker's
//     forbidden set (06 §10). A validating webhook that tried to enforce it would be checking the
//     requester's identity, which is what an authorizer is for, and would fail open the moment the
//     webhook was unreachable.
//   - the enum and shape checks are CRD-level CEL, so they hold with the webhook down.

// changePolicyGroupKind is the GroupKind used in admission error responses.
var changePolicyGroupKind = schema.GroupKind{Group: "kubeagents.x-k8s.io", Kind: "ChangePolicy"}

var changepolicylog = logf.Log.WithName("changepolicy-resource")

// SetupChangePolicyWebhookWithManager registers the webhook for ChangePolicy in the manager.
func SetupChangePolicyWebhookWithManager(mgr ctrl.Manager) error {
	return ctrl.NewWebhookManagedBy(mgr, &agentv1alpha1.ChangePolicy{}).
		WithValidator(&ChangePolicyCustomValidator{}).
		Complete()
}

// The operator READS ChangePolicies and never writes them. The verb list is the enforcement of "a
// human tightens policy" at the one identity that could plausibly be talked into doing otherwise:
// the operator is the process an agent talks to, so an operator SA holding `update` on
// `changepolicies` would put a write path to the policy behind an LLM.
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=changepolicies,verbs=get;list;watch
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=changepolicies/status,verbs=get;update;patch

// +kubebuilder:webhook:path=/validate-kubeagents-x-k8s-io-v1alpha1-changepolicy,mutating=false,failurePolicy=fail,sideEffects=None,groups=kubeagents.x-k8s.io,resources=changepolicies,verbs=create;update,versions=v1alpha1,name=vchangepolicy.kb.io,admissionReviewVersions=v1

// ChangePolicyCustomValidator validates ChangePolicy objects.
//
// No client. Every check is a function of the object and of the code floor, both of which are in
// the process already -- so this webhook cannot be made to fail by a slow API server, and its
// verdict does not depend on cluster state that could differ between admission and evaluation.
type ChangePolicyCustomValidator struct{}

var _ admission.Validator[*agentv1alpha1.ChangePolicy] = &ChangePolicyCustomValidator{}

// ValidateCreate implements admission.Validator.
func (v *ChangePolicyCustomValidator) ValidateCreate(_ context.Context, cp *agentv1alpha1.ChangePolicy) (admission.Warnings, error) {
	changepolicylog.Info("validating ChangePolicy creation", "name", cp.Name)
	return ValidateChangePolicy(cp)
}

// ValidateUpdate implements admission.Validator.
func (v *ChangePolicyCustomValidator) ValidateUpdate(_ context.Context, _, cp *agentv1alpha1.ChangePolicy) (admission.Warnings, error) {
	changepolicylog.Info("validating ChangePolicy update", "name", cp.Name)
	return ValidateChangePolicy(cp)
}

// ValidateDelete implements admission.Validator.
//
// Deleting a policy is a loosening, and it is allowed here on purpose: the thing that may not
// delete a ChangePolicy is an AGENT, and that is an authorization question answered by RBAC and by
// `vap-agent-scope`. A human with cluster-admin removing a policy they added is the intended
// workflow, and blocking it in a webhook would mean the only way out of a bad policy is editing
// etcd.
func (v *ChangePolicyCustomValidator) ValidateDelete(_ context.Context, _ *agentv1alpha1.ChangePolicy) (admission.Warnings, error) {
	return nil, nil
}

// ValidateChangePolicy is the whole validation, exported so the broker's policy loader and the
// tests call the same function the API server does.
func ValidateChangePolicy(cp *agentv1alpha1.ChangePolicy) (admission.Warnings, error) {
	var errs field.ErrorList
	var warnings admission.Warnings

	rulesPath := field.NewPath("spec", "rules")
	seen := make(map[string]int, len(cp.Spec.Rules))

	for i := range cp.Spec.Rules {
		r := &cp.Spec.Rules[i]
		p := rulesPath.Index(i)

		if prev, dup := seen[r.ID]; dup {
			errs = append(errs, field.Duplicate(p.Child("id"),
				fmt.Sprintf("%s (already used by rules[%d]); rule ids appear in the audit journal, and two rules sharing one id make `classification.reasons[].rule` unreadable", r.ID, prev)))
		} else {
			seen[r.ID] = i
		}

		// The path dialect, checked before anything else that reads a path and reported against the
		// exact index, because 06 §4.2 specifies this message and because the mistake it catches is
		// silent: `/spec/replicas` is a well-formed dotted path with one segment literally named
		// "/spec/replicas", which matches nothing and reports nothing.
		for j, fp := range r.When.FieldPaths {
			if err := classify.ValidateDottedPath(fp); err != nil {
				errs = append(errs, field.Invalid(p.Child("when", "fieldPaths").Index(j), fp, err.Error()))
			}
		}

		if r.When.OwnedByLowerTier {
			errs = append(errs, field.Invalid(p.Child("when", "ownedByLowerTier"), true,
				"code-floor only: ownership is computed from the Agent hierarchy by the parent-ceiling predicate, never declared. A policy that could assert it would be making a claim about the hierarchy rather than reading one"))
		}

		if err := classify.ValidateChangeRule(r); err != nil {
			// One error per rule from the classifier's own validator, attached to the rule rather than
			// to a guessed field: the message names what is wrong and the index says where, and
			// pointing at `class` for a `reason` problem is worse than pointing at neither.
			if !alreadyReported(errs, p) {
				errs = append(errs, field.Invalid(p, ruleSummary(r), err.Error()))
			}
		}

		// A cap above the code ceiling is accepted, stored, listed -- and never wins, because
		// EffectiveMaxObjects takes the minimum. Warning rather than refusing is deliberate: refusing
		// would make the guarantee look like it lives here, and it lives in the combinator.
		if r.MaxObjects > classify.GateObjectThreshold {
			warnings = append(warnings, fmt.Sprintf(
				"spec.rules[%d].maxObjects is %d, above the code floor's own gate threshold of %d. It is accepted and will never win: the effective cap is the minimum across sources, so this rule cannot raise anything",
				i, r.MaxObjects, classify.GateObjectThreshold))
		}
	}

	if len(errs) > 0 {
		return warnings, apierrors.NewInvalid(changePolicyGroupKind, cp.Name, errs)
	}
	return warnings, nil
}

// alreadyReported avoids stacking a generic rule-level error on top of a specific field-level one
// for the same rule. A policy author fixing a `/`-prefixed path should not also have to read a
// second error that is a restatement of the first.
func alreadyReported(errs field.ErrorList, p *field.Path) bool {
	prefix := p.String()
	for _, e := range errs {
		if strings.HasPrefix(e.Field, prefix) {
			return true
		}
	}
	return false
}

// ruleSummary is the value shown in an admission error. The rule ID, not the whole rule: the ID is
// what the author searches their YAML for, and echoing the full struct into an error the API server
// concatenates makes the message unreadable at exactly the moment it matters.
func ruleSummary(r *agentv1alpha1.ChangeRule) string {
	if r.ID == "" {
		return "<rule with no id>"
	}
	return r.ID
}
