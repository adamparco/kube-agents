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
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/util/validation/field"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/agentindex"
)

// agentGroupKind is the GroupKind used in admission error responses.
var agentGroupKind = schema.GroupKind{Group: "kubeagents.x-k8s.io", Kind: "Agent"}

// log is for logging in this package.
var agentlog = logf.Log.WithName("agent-resource")

// SetupAgentWebhookWithManager registers the webhook for Agent in the manager.
func SetupAgentWebhookWithManager(mgr ctrl.Manager) error {
	return ctrl.NewWebhookManagedBy(mgr).
		For(&agentv1alpha1.Agent{}).
		WithDefaulter(&AgentCustomDefaulter{}).
		WithValidator(&AgentCustomValidator{
			Client: mgr.GetAPIReader(),
		}).
		Complete()
}

// +kubebuilder:webhook:path=/mutate-kubeagents-x-k8s-io-v1alpha1-agent,mutating=true,failurePolicy=fail,sideEffects=None,groups=kubeagents.x-k8s.io,resources=agents,verbs=create;update,versions=v1alpha1,name=magent.kb.io,admissionReviewVersions=v1

// AgentCustomDefaulter struct to implement CustomDefaulter.
type AgentCustomDefaulter struct {
	// TODO(user): Add fields if needed
}

var _ admission.CustomDefaulter = &AgentCustomDefaulter{}

// Default implements admission.CustomDefaulter so a webhook will be registered for the type Agent.
func (d *AgentCustomDefaulter) Default(ctx context.Context, obj runtime.Object) error {
	agent, ok := obj.(*agentv1alpha1.Agent)
	if !ok {
		return fmt.Errorf("expected an Agent object but got %T", obj)
	}
	agentlog.Info("defaulting Agent", "name", agent.Name)

	// TODO(user): fill in defaulting logic here

	return nil
}

// +kubebuilder:webhook:path=/validate-kubeagents-x-k8s-io-v1alpha1-agent,mutating=false,failurePolicy=fail,sideEffects=None,groups=kubeagents.x-k8s.io,resources=agents,verbs=create;update;delete,versions=v1alpha1,name=vagent.kb.io,admissionReviewVersions=v1

// AgentCustomValidator struct to implement CustomValidator.
type AgentCustomValidator struct {
	Client client.Reader
}

var _ admission.CustomValidator = &AgentCustomValidator{}

// ValidateCreate implements admission.CustomValidator so a webhook will be registered for the type Agent.
func (v *AgentCustomValidator) ValidateCreate(ctx context.Context, obj runtime.Object) (admission.Warnings, error) {
	agent, ok := obj.(*agentv1alpha1.Agent)
	if !ok {
		return nil, fmt.Errorf("expected an Agent object but got %T", obj)
	}
	agentlog.Info("validating Agent creation", "name", agent.Name)

	return v.validateAgent(ctx, agent)
}

// ValidateUpdate implements admission.CustomValidator so a webhook will be registered for the type Agent.
func (v *AgentCustomValidator) ValidateUpdate(ctx context.Context, oldObj, newObj runtime.Object) (admission.Warnings, error) {
	agent, ok := newObj.(*agentv1alpha1.Agent)
	if !ok {
		return nil, fmt.Errorf("expected an Agent object but got %T", newObj)
	}
	agentlog.Info("validating Agent update", "name", agent.Name)

	// Tier is immutable (06 §1.1). This is also enforced declaratively by a CEL rule on the CRD; the
	// webhook check is defense-in-depth and keeps the guarantee unit-testable. oldObj is nil in some
	// unit tests (and never nil in real admission), so guard the comparison.
	if oldAgent, ok := oldObj.(*agentv1alpha1.Agent); ok && oldAgent != nil {
		if agentindex.EffectiveTier(oldAgent) != agentindex.EffectiveTier(agent) {
			return nil, apierrors.NewInvalid(
				agentGroupKind,
				agent.Name,
				field.ErrorList{field.Invalid(
					field.NewPath("spec", "tier"),
					agent.Spec.Tier,
					fmt.Sprintf("tier is immutable (was %q)", agentindex.EffectiveTier(oldAgent)),
				)},
			)
		}
	}

	return v.validateAgent(ctx, agent)
}

func (v *AgentCustomValidator) validateAgent(ctx context.Context, agent *agentv1alpha1.Agent) (admission.Warnings, error) {
	// Skip validation for terminating agents to avoid deadlocks during deletion (e.g. finalizer removal)
	if agent.DeletionTimestamp != nil {
		return nil, nil
	}

	// 1. Closed-allowlist guardrail (A4): an enabled chat integration must carry a non-empty allowlist.
	if fe := validateClosedAllowlist(agent); fe != nil {
		return nil, apierrors.NewInvalid(agentGroupKind, agent.Name, field.ErrorList{fe})
	}

	// 2. Per-tier required scope + parentRef (06 §1.2). Non-platform tiers must identify their scope
	// (so the (tier, scope) key is well-formed) and link to a parent. The cross-object checks (that the
	// parent is actually the platform/parent tier; child ⊆ parent attenuation) are deferred to the
	// hardening webhook (08 §5); this enforces presence only.
	if fe := validateScopeAndParent(agent); fe != nil {
		return nil, apierrors.NewInvalid(agentGroupKind, agent.Name, field.ErrorList{fe})
	}

	// 3. Enforce one agent per (tier, scope) (06 §1.2). The identity key is derived per-tier: platform
	// is unique per project, cluster-admin per cluster, developer-team per namespace. Agents in
	// different tiers or scopes coexist. Enforced cluster-wide on the Hub/Management cluster.
	if v.Client != nil {
		var list agentv1alpha1.AgentList
		if err := v.Client.List(ctx, &list); err != nil {
			return nil, err
		}
		identity := agentindex.ScopeIdentity(agent)
		for i := range list.Items {
			item := &list.Items[i]
			// Skip terminating agents to prevent deadlocking a replacement deployment.
			if item.DeletionTimestamp != nil {
				continue
			}
			// Skip the object under validation itself (update path).
			if item.Name == agent.Name && item.Namespace == agent.Namespace {
				continue
			}
			if agentindex.ScopeIdentity(item) == identity {
				return nil, apierrors.NewInvalid(
					agentGroupKind,
					agent.Name,
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

// validateScopeAndParent enforces the per-tier required scope fields, parentRef, and placement
// (06 §1.2, P2-T4; developer-team placement is P3-T3):
//   - platform: no requirement (projectId is conventional but scope may be nil here).
//   - cluster-admin: scope.projectId + scope.clusterName required; parentRef.name required.
//   - developer-team: scope.projectId + scope.clusterName + scope.namespace required; parentRef.name
//     required; and metadata.namespace MUST equal scope.namespace (the load-bearing placement clause).
//
// Presence + placement only — the cross-object parent-tier / attenuation checks are deferred hardening
// (08 §5).
func validateScopeAndParent(agent *agentv1alpha1.Agent) *field.Error {
	tier := agentindex.EffectiveTier(agent)
	if tier == agentv1alpha1.TierPlatform {
		return nil
	}

	scopePath := field.NewPath("spec", "scope")
	var projectID, clusterName, namespace string
	if s := agent.Spec.Scope; s != nil {
		projectID, clusterName, namespace = s.ProjectID, s.ClusterName, s.Namespace
	}

	if projectID == "" {
		return field.Required(scopePath.Child("projectId"), fmt.Sprintf("scope.projectId is required for the %s tier", tier))
	}
	if clusterName == "" {
		return field.Required(scopePath.Child("clusterName"), fmt.Sprintf("scope.clusterName is required for the %s tier", tier))
	}
	if tier == agentv1alpha1.TierDeveloperTeam {
		if namespace == "" {
			return field.Required(scopePath.Child("namespace"), "scope.namespace is required for the developer-team tier")
		}
		// Placement clause (A1, load-bearing). A developer-team Agent MUST be created in the namespace it
		// scopes. The controller renders every sub-resource (pod, SA binding, and — via the tier label —
		// the target of the per-namespace default-deny NetworkPolicy + ResourceQuota) into
		// metadata.namespace, while the (tier, scope) cardinality key is derived from scope.namespace
		// INDEPENDENTLY of metadata.namespace. Without this clause an Agent in, e.g., kubeagents-system
		// could declare scope.namespace=team-x: it would pass the cardinality webhook yet place the pod
		// OUTSIDE team-x's isolation controls — a namespace-isolation escape (03 §3, §11). Rendering into
		// scope.namespace from a foreign metadata.namespace is not an option either: cross-namespace
		// ownerRefs break garbage collection and a namespaced SA can only bind a pod in its own namespace.
		if agent.Namespace != namespace {
			return field.Invalid(
				field.NewPath("metadata", "namespace"),
				agent.Namespace,
				fmt.Sprintf("a developer-team Agent must be created in its scoped namespace: metadata.namespace must equal spec.scope.namespace (%q)", namespace),
			)
		}
	}

	if agent.Spec.ParentRef == nil || agent.Spec.ParentRef.Name == "" {
		return field.Required(field.NewPath("spec", "parentRef", "name"), fmt.Sprintf("parentRef.name is required for the %s tier (must reference the parent agent)", tier))
	}

	return nil
}

// hasNonBlankEntry reports whether the list contains at least one entry that is
// not empty and not pure whitespace.
//
// Counting entries is not enough. A provisioning template that renders
// `allowedUsers: [ "${ALLOWED_USERS}" ]` from an unset variable produces a list
// of length one whose only element is "", which satisfies any size check while
// naming no principal at all. 06 §1.2 V-7 is explicit: an all-blank list is not
// an allowlist, it is empty.
func hasNonBlankEntry(users []string) bool {
	for _, u := range users {
		if strings.TrimSpace(u) != "" {
			return true
		}
	}
	return false
}

// validateClosedAllowlist enforces A4 / 06 §1.2 V-7: an enabled chat integration
// must carry an allowlist with at least one non-blank entry. There is no
// permissive fallback — an allowlist that names nobody is a configuration error,
// never an instruction to admit everybody.
//
// This is also enforced by CEL on the CRD; the webhook check keeps it
// unit-testable and defense-in-depth.
func validateClosedAllowlist(agent *agentv1alpha1.Agent) *field.Error {
	integration := agent.Spec.Integration
	if integration == nil {
		return nil
	}
	base := field.NewPath("spec", "integration")
	if gc := integration.GoogleChat; gc != nil && gc.Enabled != nil && *gc.Enabled && !hasNonBlankEntry(gc.AllowedUsers) {
		return field.Required(
			base.Child("googleChat", "allowedUsers"),
			"allowedUsers must contain at least one non-blank entry when the Google Chat integration is enabled (an empty or all-blank allowlist is not an allowlist)",
		)
	}
	if sl := integration.Slack; sl != nil && sl.Enabled != nil && *sl.Enabled && !hasNonBlankEntry(sl.AllowedUsers) {
		return field.Required(
			base.Child("slack", "allowedUsers"),
			"allowedUsers must contain at least one non-blank entry when the Slack integration is enabled (an empty or all-blank allowlist is not an allowlist)",
		)
	}
	return nil
}

// ValidateDelete implements admission.CustomValidator so a webhook will be registered for the type Agent.
func (v *AgentCustomValidator) ValidateDelete(ctx context.Context, obj runtime.Object) (admission.Warnings, error) {
	agent, ok := obj.(*agentv1alpha1.Agent)
	if !ok {
		return nil, fmt.Errorf("expected an Agent object but got %T", obj)
	}
	agentlog.Info("validating Agent deletion", "name", agent.Name)

	// TODO(user): fill in validation logic here
	return nil, nil
}
