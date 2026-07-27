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
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/agentindex"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/scope"
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

	// 2. Per-tier required scope + parentRef (06 §1.2 V-2/V-3/V-4). Presence and placement; the
	// cross-object ceiling that needs the parent CR is V-6, below.
	if fe := validateScopeAndParent(agent); fe != nil {
		return nil, apierrors.NewInvalid(agentGroupKind, agent.Name, field.ErrorList{fe})
	}

	// 2b. Reader-only ServiceAccount override (06 §1.2 V-10).
	if fe := validateServiceAccountOverride(agent); fe != nil {
		return nil, apierrors.NewInvalid(agentGroupKind, agent.Name, field.ErrorList{fe})
	}

	// 2c. Budget clamp (06 §1.2 V-8): every initiativeBudget leaf at or below its code ceiling,
	// flapWindow at or above its floor. Rejected, never silently clamped.
	if fe := validateInitiativeBudget(agent); fe != nil {
		return nil, apierrors.NewInvalid(agentGroupKind, agent.Name, field.ErrorList{fe})
	}

	// 3. Cross-object rules. Both V-5 (cardinality) and V-6 (the ceiling) need the same cluster-wide
	// view, so the list is fetched once and handed to both — two Lists could observe two different
	// worlds and admit a CR neither would have admitted alone.
	if v.Client != nil {
		var list agentv1alpha1.AgentList
		if err := v.Client.List(ctx, &list); err != nil {
			return nil, err
		}

		// 3a. One agent per (tier, scope) (06 §1.2 V-5). The identity key is derived per-tier:
		// platform is unique per project, cluster-admin per cluster, developer-team per namespace.
		// Agents in different tiers or scopes coexist. Enforced cluster-wide on the Hub cluster.
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
					// The path is spec.scope, not spec: the identity key is (tier, scope), spec.tier is
					// immutable under V-1, and so scope is the only half of the key the operator can
					// actually edit to resolve the conflict. A bare "spec" is a field path that names
					// nothing and sends the reader to the CRD instead of to their own manifest.
					field.ErrorList{field.Duplicate(
						field.NewPath("spec", "scope"),
						fmt.Sprintf("an agent already exists for %s (%s/%s); (tier, scope) must be unique", identity, item.Namespace, item.Name),
					)},
				)
			}
		}

		// 3b. Cross-object ceiling (06 §1.2 V-6): child tier is exactly one below the parent's, the
		// child's scope is a strict subset of the parent's, and the parent is neither terminating
		// nor paused.
		if fe := validateParentCeiling(agent, &list); fe != nil {
			return nil, apierrors.NewInvalid(agentGroupKind, agent.Name, field.ErrorList{fe})
		}
	}

	return nil, nil
}

// parentTierOf returns the tier immediately above the given one, and whether one exists. The chain
// is platform → cluster-admin → developer-team (06 §1.2 V-6); platform has no parent.
func parentTierOf(tier agentv1alpha1.AgentTier) (agentv1alpha1.AgentTier, bool) {
	switch tier {
	case agentv1alpha1.TierClusterAdmin:
		return agentv1alpha1.TierPlatform, true
	case agentv1alpha1.TierDeveloperTeam:
		return agentv1alpha1.TierClusterAdmin, true
	default:
		return "", false
	}
}

// validateParentCeiling enforces 06 §1.2 V-6 — the cross-object ceiling. For a candidate child C
// with parent P:
//
//	tier(P) is the tier immediately above tier(C)      (platform → cluster-admin → developer-team)
//	C.projectId   == P.projectId                        (always)
//	C.clusterName == P.clusterName                      when P.tier == cluster-admin
//	C.namespace   != ""                                 when C.tier == developer-team
//	scope(C) != scope(P)                                (strict subset, not equality)
//	P is not terminating, and P.spec.operations.paused is false
//
// This is the difference between "a parent cannot EXPRESS an over-grant" and "a parent cannot CAUSE
// one". V-2/V-3 already prove the child names a parent; only reading that parent proves the child is
// actually beneath it.
//
// # An unreadable parent is a rejection, not a pass
//
// If parentRef names an Agent that does not exist, the ceiling is not satisfied and not violated —
// it is UNVERIFIABLE, and this returns Invalid. That is the same rule preconditions P1, P4 and P10
// already encode for the verification side ("could not verify" never maps to a pass), applied to
// admission. The alternative — admit and hope — creates an agent whose authority ceiling was never
// measured against anything, which in Phase 10 is an unattenuated agent. Fail-closed also matches
// the install order the design already asserts: provision_08 deploys the platform agent before any
// cluster-admin exists to be parented by it.
//
// The cost is real and is accepted deliberately: a GitOps tree must be applied parent-first. A
// reconciler that retries to convergence (Argo, Flux, and `provision_08`'s ordered install) gets
// there on its own. A ONE-SHOT recursive apply does not — `clusters/` sorts before `fleet/`, so the
// children are submitted before their parent exists and are refused on the first pass. That is a
// real cost of this rule and it is why dev/verify/gitops-tree-applies-l2.sh, which dry-runs each
// file exactly once and can never converge, establishes the parent chain before it starts.
func validateParentCeiling(child *agentv1alpha1.Agent, list *agentv1alpha1.AgentList) *field.Error {
	childTier := agentindex.EffectiveTier(child)
	wantParentTier, hasParent := parentTierOf(childTier)
	if !hasParent {
		// Platform is the root: no parentRef to check. A platform Agent that carries one anyway is
		// not rejected here — V-3 governs presence, and an ignored field is not an over-grant.
		return nil
	}

	parentPath := field.NewPath("spec", "parentRef", "name")

	// V-3 has already guaranteed a non-empty name for non-platform tiers by the time we run.
	name := ""
	if child.Spec.ParentRef != nil {
		name = child.Spec.ParentRef.Name
	}

	// Resolve the parent by name. parentRef carries no namespace (06 §1.2), so resolution prefers a
	// same-namespace match and otherwise requires the name to be unique cluster-wide. An ambiguous
	// parent is rejected rather than arbitrarily picked: silently choosing one of two candidate
	// ceilings is how a child ends up measured against the wrong one.
	var parent *agentv1alpha1.Agent
	var candidates []*agentv1alpha1.Agent
	for i := range list.Items {
		item := &list.Items[i]
		if item.Name != name {
			continue
		}
		if item.Name == child.Name && item.Namespace == child.Namespace {
			continue // the object under validation cannot be its own parent
		}
		if item.Namespace == child.Namespace {
			parent = item
			break
		}
		candidates = append(candidates, item)
	}
	if parent == nil {
		switch len(candidates) {
		case 0:
			return field.Invalid(parentPath, name, fmt.Sprintf(
				"parent Agent %q does not exist, so the %s tier's authority ceiling cannot be verified; create the parent %s Agent first",
				name, childTier, wantParentTier))
		case 1:
			parent = candidates[0]
		default:
			return field.Invalid(parentPath, name, fmt.Sprintf(
				"parent Agent %q is ambiguous: %d Agents share that name in different namespaces, so which scope bounds this child is undefined",
				name, len(candidates)))
		}
	}

	if parent.DeletionTimestamp != nil {
		return field.Invalid(parentPath, name, fmt.Sprintf(
			"parent Agent %q is terminating; a child may not be created beneath a parent that is going away", name))
	}

	parentTier := agentindex.EffectiveTier(parent)
	if parentTier != wantParentTier {
		return field.Invalid(parentPath, name, fmt.Sprintf(
			"parent Agent %q is tier %q, but the %s tier must be parented by the tier immediately above it (%s)",
			name, parentTier, childTier, wantParentTier))
	}

	// The brake covers provisioning too (03 §6): a paused agent may not act as a parent, because
	// creating a child IS an action and a pause that still permits fleet growth is not a pause.
	if parent.Spec.Operations != nil && parent.Spec.Operations.Paused != nil && *parent.Spec.Operations.Paused {
		reason := parent.Spec.Operations.PauseReason
		if reason == "" {
			reason = "no reason given"
		}
		return field.Invalid(parentPath, name, fmt.Sprintf(
			"parent Agent %q is paused (%s); a paused agent may not provision children", name, reason))
	}

	// Scope subset. The predicate itself lives in internal/scope and is SHARED with the classifier's
	// `cross-tier-direct-operation` rule (06 §4.2: "the same subset predicate V-6 uses (§1.2),
	// reused so the two cannot drift"). This function keeps its own messages -- an admission
	// rejection has to say which field to edit -- by asking the predicate which clause failed
	// rather than by deciding containment a second time.
	c := scope.Of(child)
	p := scope.Of(parent)

	// CONTAINMENT is tested against a masked parent; STRICTNESS is tested against the raw one. The
	// two halves of V-6 genuinely read the parent's scope differently and collapsing them is a
	// behaviour change, which a test caught the first time this was refactored:
	//
	//   - Containment must ignore a PLATFORM parent's clusterName. A platform scope narrows to a
	//     project (06 §1.2); a clusterName on one is conventional at most, and comparing it would
	//     reject every cluster-admin child of a platform agent that happens to carry one.
	//   - Strictness must NOT ignore it. V-6's clause is `scope(C) != scope(P)` over the declared
	//     scopes, so a child that reproduces its parent's triple exactly has narrowed nothing and is
	//     an authority clone -- whether or not the field that makes them equal was load-bearing for
	//     containment.
	//
	// Hence one shared predicate for the part that must not drift from the classifier (which level
	// contains which), and a local equality for the part that is V-6's alone.
	pContain := p
	if parentTier != agentv1alpha1.TierClusterAdmin {
		pContain.ClusterName, pContain.Namespace = "", ""
	}

	if ok, clause := scope.Contains(pContain, c); !ok {
		switch clause {
		case scope.ClauseProject:
			return field.Invalid(parentPath, name, fmt.Sprintf(
				"scope.projectId %q is outside parent %q's project %q; a child's scope must be a subset of its parent's",
				c.ProjectID, name, p.ProjectID))
		case scope.ClauseCluster:
			return field.Invalid(parentPath, name, fmt.Sprintf(
				"scope.clusterName %q is outside parent %q's cluster %q; a child's scope must be a subset of its parent's",
				c.ClusterName, name, p.ClusterName))
		case scope.ClauseNamespace:
			return field.Invalid(parentPath, name, fmt.Sprintf(
				"scope.namespace %q is outside parent %q's namespace %q; a child's scope must be a subset of its parent's",
				c.Namespace, name, p.Namespace))
		}
	}

	if childTier == agentv1alpha1.TierDeveloperTeam && c.Namespace == "" {
		// V-2 already requires this; repeated here so the subset predicate is total rather than
		// relying on an earlier rule's ordering to make it so.
		return field.Invalid(parentPath, name,
			"a developer-team child must narrow its parent's scope with a namespace, but scope.namespace is empty")
	}

	// Strict subset: a child whose scope EQUALS its parent's has narrowed nothing.
	if c == p {
		return field.Invalid(parentPath, name, fmt.Sprintf(
			"scope is identical to parent %q's; a child's scope must be a STRICT subset (it must narrow project, cluster or namespace)", name))
	}

	return nil
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

// validateServiceAccountOverride enforces 06 §1.2 V-10 — the reader-only ServiceAccount override.
//
// `spec.security.serviceAccountName` may name only the tier's READER SA, whose name is the tier
// template `<tier>-agent`. The actor SA is derived from tier + scope (06 §2) and is published in
// `status.broker.actorServiceAccount` — status, not spec, so it is observable but not settable.
//
// The rule exists because the ability to name an identity is the ability to choose one. An
// unconstrained serviceAccountName lets a CR point its pod at any SA in the namespace, including the
// actor SA that holds the scoped write authority the reader is defined not to have — the exact
// self-escalation 03 §3.3/§3.4 make unrepresentable. Constraining the field to one literal value is
// what keeps "the LLM holds no write credential" a structural fact rather than a convention.
//
// An empty value is fine: it means "let the controller derive it", which is the safe default.
func validateServiceAccountOverride(agent *agentv1alpha1.Agent) *field.Error {
	if agent.Spec.Security == nil || agent.Spec.Security.ServiceAccountName == "" {
		return nil
	}
	tier := agentindex.EffectiveTier(agent)
	want := fmt.Sprintf("%s-agent", tier)
	if agent.Spec.Security.ServiceAccountName != want {
		return field.Invalid(
			field.NewPath("spec", "security", "serviceAccountName"),
			agent.Spec.Security.ServiceAccountName,
			fmt.Sprintf("may name only the %s tier's reader ServiceAccount %q (06 §1.2 V-10); the actor ServiceAccount is derived from tier and scope and is not settable from the spec", tier, want),
		)
	}
	return nil
}

// budgetCeiling is one leaf of the 06 §1.1 initiative-budget table: the field path under
// spec.operations.initiativeBudget, the value the CR carries, and the code ceiling it may not
// exceed.
type budgetCeiling struct {
	path    *field.Path
	value   *int32
	ceiling int32
}

// initiativeBudgetCeilings flattens the 06 §1.1 table into one slice, so the rule is checked
// leaf-by-leaf as V-8 requires and adding a leaf to the table means adding a row here rather than
// editing a condition. Ceilings are transcribed from 06 §1.1 and the unit test pins every one of
// them against the spec values, so a typo here is a red test rather than a quietly looser cap.
func initiativeBudgetCeilings(b *agentv1alpha1.InitiativeBudgetSpec, base *field.Path) []budgetCeiling {
	var out []budgetCeiling
	classes := []struct {
		name     string
		spec     *agentv1alpha1.BudgetClassSpec
		routine  int32
		elevated int32
		gated    int32
		perDay   int32
	}{
		{"selfInitiated", b.SelfInitiated, 50, 10, 5, 500},
		{"humanRequested", b.HumanRequested, 200, 60, 30, 2000},
	}
	for _, c := range classes {
		if c.spec == nil {
			continue
		}
		p := base.Child(c.name)
		out = append(out,
			budgetCeiling{p.Child("routinePerHour"), c.spec.RoutinePerHour, c.routine},
			budgetCeiling{p.Child("elevatedPerHour"), c.spec.ElevatedPerHour, c.elevated},
			budgetCeiling{p.Child("gatedPerHour"), c.spec.GatedPerHour, c.gated},
			budgetCeiling{p.Child("actionsPerDay"), c.spec.ActionsPerDay, c.perDay},
		)
	}
	out = append(out,
		budgetCeiling{base.Child("maxObjectsPerAction"), b.MaxObjectsPerAction, 50},
		budgetCeiling{base.Child("flapThreshold"), b.FlapThreshold, 5},
	)
	return out
}

// flapWindowFloor is the 06 §1.1 code floor for spec.operations.initiativeBudget.flapWindow. It is
// the one leaf where SMALLER is the dangerous direction: the flap brake counts repeats of the same
// (target, intent) within the window, so a short window lets a flapping agent reset its own counter
// and escape the brake entirely.
const flapWindowFloor = 5 * time.Minute

// validateInitiativeBudget enforces 06 §1.2 V-8 — the budget clamp, per class.
//
// Every leaf is compared against its code ceiling from the 06 §1.1 table and a value above it is
// REJECTED, not clamped. Rejecting is the load-bearing half of the rule: a clamp leaves the CR
// reading 500 while the runtime enforces 10, and that disagreement surfaces later as an incident
// nobody can explain from the manifest. A rejection is a sentence an operator reads at apply time.
//
// Accepted AT the ceiling, rejected one above it — the boundary is inclusive, which the negative
// suite pins from both sides so an off-by-one cannot pass as strictness.
func validateInitiativeBudget(agent *agentv1alpha1.Agent) *field.Error {
	if agent.Spec.Operations == nil || agent.Spec.Operations.InitiativeBudget == nil {
		return nil
	}
	b := agent.Spec.Operations.InitiativeBudget
	base := field.NewPath("spec", "operations", "initiativeBudget")

	for _, leaf := range initiativeBudgetCeilings(b, base) {
		if leaf.value == nil {
			continue
		}
		if *leaf.value > leaf.ceiling {
			return field.Invalid(leaf.path, *leaf.value, fmt.Sprintf(
				"exceeds the code ceiling of %d (06 §1.1); a value above the ceiling is rejected, not silently clamped", leaf.ceiling))
		}
		if *leaf.value < 0 {
			return field.Invalid(leaf.path, *leaf.value, "must not be negative")
		}
	}

	if b.FlapWindow != nil && b.FlapWindow.Duration < flapWindowFloor {
		return field.Invalid(base.Child("flapWindow"), b.FlapWindow.Duration.String(), fmt.Sprintf(
			"is below the code floor of %s (06 §1.1); a shorter window lets a flapping agent reset its own counter", flapWindowFloor))
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
