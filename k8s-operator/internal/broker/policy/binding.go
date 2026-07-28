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

// Package policy is the broker's live view of the `ChangePolicy` objects in the cluster (03 §5.3,
// 06 §4.2).
//
// It exists because `classify.FromChangePolicy` had no caller. P9-T3b shipped the conversion and
// proved the stricter-only property over it at L1, and said so plainly: "nothing reads a
// ChangePolicy out of a cluster yet". A policy an operator applies and a broker never reads is
// worse than no policy at all -- `kubectl get changepolicy` shows it bound, `status.agentsMatched`
// counts the agents, and every action classifies as though it were not there. This package is what
// makes the object load-bearing.
//
// # The direction that makes every decision here obvious
//
// The classifier takes the MAXIMUM over its sources (06 §4.2 step 3). A policy can therefore only
// ever RAISE a class. Which means every way of failing to see a policy -- a watch that died, a
// cache that never synced, a rule that would not parse and got skipped, a selector evaluated too
// generously in the wrong direction -- has exactly one consequence, and it is a LOOSENING. There is
// no symmetric failure. So every ambiguous case in this package resolves the same way: if the
// policy set might be incomplete, refuse the action rather than classify it against what we happen
// to have.
//
// That is also why nothing here skips a bad policy. `FromChangePolicy` returns an error rather than
// dropping a rule for the same reason, and the loader escalates it: a snapshot with a policy
// missing from it is not a smaller policy set, it is an unknown one.
package policy

import (
	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/scope"
)

// Agent is the identity of the agent a broker serves: the two coordinates
// `ChangePolicySpec.AgentSelector` selects on. It is a value type because the binding predicate
// must not depend on whether the caller has an `Agent` CR, an envelope, or a pair of flags.
type Agent struct {
	Tier  agentv1alpha1.AgentTier
	Scope scope.Scope
}

// Binds reports whether cp applies to this agent, per `ChangePolicyAgentSelector`.
//
// Two clauses, ANDed, each of which matches everything when empty:
//
//   - Tiers: exact membership. A tier is a closed enum, not a hierarchy -- a policy naming
//     `cluster-admin` does not bind the platform agent above it, because the tiers are different
//     kinds of authority rather than different amounts of one.
//   - Scopes: `scope.Contains(policyScope, agentScope)` -- "at or beneath", as the field documents.
//     A policy naming project `p` binds the platform agent for `p` and every cluster-admin and
//     developer-team agent under it, so a fleet-wide policy does not need one entry per namespace.
//
// The two clauses are ANDed and not ORed, and that is the whole of the selector's meaning: `{tiers:
// [developer-team], scopes: [project p]}` is "the developer-team agents in p", not "every
// developer-team agent plus everything in p". ORing them would silently widen every two-clause
// policy in the fleet -- which, since a policy only tightens, would be a safe-direction bug, but a
// bug an operator could never diagnose from the object they wrote.
//
// A nil selector binds every agent. That is the field's documented default and it is the
// fail-closed one: a policy whose author left the selector out meant the fleet.
func Binds(cp *agentv1alpha1.ChangePolicy, a Agent) bool {
	if cp == nil {
		return false
	}
	sel := cp.Spec.AgentSelector
	if sel == nil {
		return true
	}
	if len(sel.Tiers) > 0 && !containsTier(sel.Tiers, a.Tier) {
		return false
	}
	if len(sel.Scopes) > 0 && !anyScopeContains(sel.Scopes, a.Scope) {
		return false
	}
	return true
}

func containsTier(tiers []agentv1alpha1.AgentTier, want agentv1alpha1.AgentTier) bool {
	for _, t := range tiers {
		if t == want {
			return true
		}
	}
	return false
}

// anyScopeContains is an OR across the list and an AND within each entry -- a list of scopes is a
// list of alternatives, which is the only reading under which a list is useful at all.
//
// An ill-formed policy scope is skipped rather than treated as a wildcard. `scope.Contains` stops
// at the first level the outer scope does not narrow, so `{clusterName: c}` with no project would
// bind cluster `c` in EVERY project -- a hole in the middle read as a wildcard, which is the exact
// failure `scope.IsWellFormed` exists to name. Skipping it can only narrow the policy's reach, and
// narrowing a policy's reach is the loosening direction, so the loader also refuses to build a
// snapshot containing one; see Snapshot. Both halves are needed: this one keeps the predicate
// honest, that one keeps the honesty from being silent.
func anyScopeContains(scopes []agentv1alpha1.ScopeSpec, agentScope scope.Scope) bool {
	for i := range scopes {
		s := scope.FromSpec(&scopes[i])
		if !s.IsWellFormed() {
			continue
		}
		if ok, _ := scope.Contains(s, agentScope); ok {
			return true
		}
	}
	return false
}

// IllFormedScopes returns the indices of `spec.agentSelector.scopes` entries that are not a proper
// prefix narrowing, so the loader can name them in its refusal.
//
// Separate from Binds because the two questions have different audiences: Binds answers "does this
// apply to me", which is asked on every snapshot rebuild, and this answers "is this policy
// writable at all", which is a diagnostic. Folding the second into the first would mean the
// predicate returned an error, and a predicate that returns an error has a caller who ignores it.
func IllFormedScopes(cp *agentv1alpha1.ChangePolicy) []int {
	if cp == nil || cp.Spec.AgentSelector == nil {
		return nil
	}
	var out []int
	for i := range cp.Spec.AgentSelector.Scopes {
		s := scope.FromSpec(&cp.Spec.AgentSelector.Scopes[i])
		if !s.IsWellFormed() {
			out = append(out, i)
		}
	}
	return out
}
