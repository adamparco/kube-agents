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

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/util/validation/field"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// log is for logging in this package.
var platformagentlog = logf.Log.WithName("platformagent-resource")

// SetupPlatformAgentWebhookWithManager registers the webhook for PlatformAgent in the manager.
func SetupPlatformAgentWebhookWithManager(mgr ctrl.Manager) error {
	return ctrl.NewWebhookManagedBy(mgr).
		For(&agentv1alpha1.PlatformAgent{}).
		WithDefaulter(&PlatformAgentCustomDefaulter{}).
		WithValidator(&PlatformAgentCustomValidator{
			Client: mgr.GetAPIReader(),
		}).
		Complete()
}

// +kubebuilder:webhook:path=/mutate-kubeagents-x-k8s-io-v1alpha1-platformagent,mutating=true,failurePolicy=fail,sideEffects=None,groups=kubeagents.x-k8s.io,resources=platformagents,verbs=create;update,versions=v1alpha1,name=mplatformagent.kb.io,admissionReviewVersions=v1

// PlatformAgentCustomDefaulter struct to implement CustomDefaulter.
type PlatformAgentCustomDefaulter struct {
	// TODO(user): Add fields if needed
}

var _ admission.CustomDefaulter = &PlatformAgentCustomDefaulter{}

// Default implements admission.CustomDefaulter so a webhook will be registered for the type PlatformAgent.
func (d *PlatformAgentCustomDefaulter) Default(ctx context.Context, obj runtime.Object) error {
	platformAgent, ok := obj.(*agentv1alpha1.PlatformAgent)
	if !ok {
		return fmt.Errorf("expected a PlatformAgent object but got %T", obj)
	}
	platformagentlog.Info("defaulting PlatformAgent", "name", platformAgent.Name)

	// TODO(user): fill in defaulting logic here

	return nil
}

// +kubebuilder:webhook:path=/validate-kubeagents-x-k8s-io-v1alpha1-platformagent,mutating=false,failurePolicy=fail,sideEffects=None,groups=kubeagents.x-k8s.io,resources=platformagents,verbs=create;update;delete,versions=v1alpha1,name=vplatformagent.kb.io,admissionReviewVersions=v1

// PlatformAgentCustomValidator struct to implement CustomValidator.
type PlatformAgentCustomValidator struct {
	Client client.Reader
}

var _ admission.CustomValidator = &PlatformAgentCustomValidator{}

// ValidateCreate implements admission.CustomValidator so a webhook will be registered for the type PlatformAgent.
func (v *PlatformAgentCustomValidator) ValidateCreate(ctx context.Context, obj runtime.Object) (admission.Warnings, error) {
	platformAgent, ok := obj.(*agentv1alpha1.PlatformAgent)
	if !ok {
		return nil, fmt.Errorf("expected a PlatformAgent object but got %T", obj)
	}
	platformagentlog.Info("validating PlatformAgent creation", "name", platformAgent.Name)

	return v.validatePlatformAgent(ctx, platformAgent)
}

// ValidateUpdate implements admission.CustomValidator so a webhook will be registered for the type PlatformAgent.
func (v *PlatformAgentCustomValidator) ValidateUpdate(ctx context.Context, oldObj, newObj runtime.Object) (admission.Warnings, error) {
	platformAgent, ok := newObj.(*agentv1alpha1.PlatformAgent)
	if !ok {
		return nil, fmt.Errorf("expected a PlatformAgent object but got %T", newObj)
	}
	platformagentlog.Info("validating PlatformAgent update", "name", platformAgent.Name)

	// Tier is immutable (06 §1.1). This is also enforced declaratively by a CEL rule on the CRD; the
	// webhook check is defense-in-depth and keeps the guarantee unit-testable. oldObj is nil in some
	// unit tests (and never nil in real admission), so guard the comparison.
	if oldAgent, ok := oldObj.(*agentv1alpha1.PlatformAgent); ok && oldAgent != nil {
		if effectiveTier(oldAgent) != effectiveTier(platformAgent) {
			return nil, apierrors.NewInvalid(
				schema.GroupKind{Group: "kubeagents.x-k8s.io", Kind: "PlatformAgent"},
				platformAgent.Name,
				field.ErrorList{field.Invalid(
					field.NewPath("spec", "tier"),
					platformAgent.Spec.Tier,
					fmt.Sprintf("tier is immutable (was %q)", effectiveTier(oldAgent)),
				)},
			)
		}
	}

	return v.validatePlatformAgent(ctx, platformAgent)
}

func (v *PlatformAgentCustomValidator) validatePlatformAgent(ctx context.Context, platformAgent *agentv1alpha1.PlatformAgent) (admission.Warnings, error) {
	// Skip validation for terminating agents to avoid deadlocks during deletion (e.g. finalizer removal)
	if platformAgent.DeletionTimestamp != nil {
		return nil, nil
	}

	// 1. Closed-allowlist guardrail (A4): an enabled chat integration must carry a non-empty allowlist.
	if fe := validateClosedAllowlist(platformAgent); fe != nil {
		return nil, apierrors.NewInvalid(
			schema.GroupKind{Group: "kubeagents.x-k8s.io", Kind: "PlatformAgent"},
			platformAgent.Name,
			field.ErrorList{fe},
		)
	}

	// 2. Enforce one agent per (tier, scope) (06 §1.2). The identity key is derived per-tier: platform
	// is unique per project, cluster-admin per cluster, developer-team per namespace. Agents in
	// different tiers or scopes coexist. Enforced cluster-wide on the Hub/Management cluster.
	if v.Client != nil {
		var list agentv1alpha1.PlatformAgentList
		if err := v.Client.List(ctx, &list); err != nil {
			return nil, err
		}
		identity := scopeIdentity(platformAgent)
		for i := range list.Items {
			item := &list.Items[i]
			// Skip terminating agents to prevent deadlocking a replacement deployment.
			if item.DeletionTimestamp != nil {
				continue
			}
			// Skip the object under validation itself (update path).
			if item.Name == platformAgent.Name && item.Namespace == platformAgent.Namespace {
				continue
			}
			if scopeIdentity(item) == identity {
				return nil, apierrors.NewInvalid(
					schema.GroupKind{Group: "kubeagents.x-k8s.io", Kind: "PlatformAgent"},
					platformAgent.Name,
					field.ErrorList{field.Duplicate(
						field.NewPath("spec"),
						fmt.Sprintf("an agent already exists for %s (%s/%s); (tier, scope) must be unique", identity, item.Namespace, item.Name),
					)},
				)
			}
		}
	}

	return nil, nil
}

// effectiveTier returns the agent's tier, defaulting an empty value to platform (the CRD default) so
// stored objects written before defaulting compare equal to freshly-defaulted ones.
func effectiveTier(agent *agentv1alpha1.PlatformAgent) agentv1alpha1.AgentTier {
	if agent.Spec.Tier == "" {
		return agentv1alpha1.TierPlatform
	}
	return agent.Spec.Tier
}

// scopeIdentity returns the (tier, scope) uniqueness key for an agent (06 §1.2). Two agents that
// resolve to the same identity may not coexist. The scope fields that matter are per-tier: platform →
// projectId; cluster-admin → +clusterName; developer-team → +namespace.
func scopeIdentity(agent *agentv1alpha1.PlatformAgent) string {
	tier := effectiveTier(agent)
	var projectID, clusterName, namespace string
	if s := agent.Spec.Scope; s != nil {
		projectID = s.ProjectID
		clusterName = s.ClusterName
		namespace = s.Namespace
	}
	switch tier {
	case agentv1alpha1.TierClusterAdmin:
		return fmt.Sprintf("tier=%s;project=%s;cluster=%s", tier, projectID, clusterName)
	case agentv1alpha1.TierDeveloperTeam:
		return fmt.Sprintf("tier=%s;project=%s;cluster=%s;namespace=%s", tier, projectID, clusterName, namespace)
	default: // platform (and the empty/default case)
		return fmt.Sprintf("tier=%s;project=%s", tier, projectID)
	}
}

// validateClosedAllowlist enforces A4: an enabled chat integration must carry a non-empty allowlist.
// An empty/absent allowlist means "all authenticated users", which is the open default we must close.
// This is also enforced by CEL on the CRD; the webhook check keeps it unit-testable and defense-in-depth.
func validateClosedAllowlist(platformAgent *agentv1alpha1.PlatformAgent) *field.Error {
	integration := platformAgent.Spec.Integration
	if integration == nil {
		return nil
	}
	base := field.NewPath("spec", "integration")
	if gc := integration.GoogleChat; gc != nil && gc.Enabled != nil && *gc.Enabled && len(gc.AllowedUsers) == 0 {
		return field.Required(
			base.Child("googleChat", "allowedUsers"),
			"allowedUsers must be non-empty when the Google Chat integration is enabled (an empty allowlist admits all authenticated users)",
		)
	}
	if sl := integration.Slack; sl != nil && sl.Enabled != nil && *sl.Enabled && len(sl.AllowedUsers) == 0 {
		return field.Required(
			base.Child("slack", "allowedUsers"),
			"allowedUsers must be non-empty when the Slack integration is enabled (an empty allowlist admits all authenticated users)",
		)
	}
	return nil
}

// ValidateDelete implements admission.CustomValidator so a webhook will be registered for the type PlatformAgent.
func (v *PlatformAgentCustomValidator) ValidateDelete(ctx context.Context, obj runtime.Object) (admission.Warnings, error) {
	platformAgent, ok := obj.(*agentv1alpha1.PlatformAgent)
	if !ok {
		return nil, fmt.Errorf("expected a PlatformAgent object but got %T", obj)
	}
	platformagentlog.Info("validating PlatformAgent deletion", "name", platformAgent.Name)

	// TODO(user): fill in validation logic here
	return nil, nil
}
