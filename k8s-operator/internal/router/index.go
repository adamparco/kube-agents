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

package router

import (
	"sync"

	"k8s.io/apimachinery/pkg/types"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/agentindex"
)

// Index is the router's in-memory routing table: the (tier, scope) key of every live Agent CR mapped to
// the Target projection Authorize and the dispatcher consume (06 §2b, 05 C15). It is keyed by
// agentindex.ScopeIdentity — the SAME key the cardinality webhook enforces uniqueness on — so a parsed
// handle (via Handle.RouteKey) and an indexed CR resolve to one Target by construction, never by a
// second, drift-prone lookup path.
//
// The Index is a plain data structure with no Kubernetes client: it is fed by the read-only Reconciler
// (controller.go) in production and directly by tests. That split keeps the security-load-bearing lookup
// unit-testable without an API server, and keeps the Index safe for concurrent Lookup while the informer
// mutates it.
type Index struct {
	mu sync.RWMutex
	// byKey maps agentindex.ScopeIdentity(cr) -> Target. This is the routing table proper.
	byKey map[string]Target
	// keyByObject maps an Agent CR's namespaced name -> the identity key it currently occupies, so a
	// delete (which carries only the name, the object being gone) removes the right entry, and an update
	// that RE-KEYS an agent (a scope edit) evicts its stale key instead of leaking a phantom route.
	keyByObject map[types.NamespacedName]string
	// byTierLeaf is the secondary index (tier‖leaf -> the ScopeIdentity keys of the live agents sharing
	// that handle). A developer-team handle carries only its namespace leaf, so its full routing key
	// exists only on a live CR; this index resolves such a handle to the matching CR(s) without the
	// handle ever having to name a cluster. Maintained for ALL tiers (it also backs the live-handle
	// menu KnownHandles), so a scope edit that changes an agent's (tier, leaf) evicts its stale slot.
	byTierLeaf map[string][]string
	// tlByObject maps an Agent CR's namespaced name -> the tier‖leaf slot it currently occupies, so a
	// re-key (scope edit) or delete removes its entry from the RIGHT byTierLeaf slice.
	tlByObject map[types.NamespacedName]string
}

// NewIndex returns an empty routing table.
func NewIndex() *Index {
	return &Index{
		byKey:       make(map[string]Target),
		keyByObject: make(map[types.NamespacedName]string),
		byTierLeaf:  make(map[string][]string),
		tlByObject:  make(map[types.NamespacedName]string),
	}
}

// tierLeafKey is the byTierLeaf slot for a handle: tier and leaf joined by a NUL, which cannot appear in
// either a tier string or an RFC1123 leaf, so the two fields can never collide across a slot boundary.
func tierLeafKey(h Handle) string {
	return string(h.Tier) + "\x00" + h.Leaf
}

// Upsert inserts or updates the route for a. It computes the identity key from the CR (agentindex, the
// single source of truth). It first evicts a's prior footprint (removeLocked) so a scope edit that
// re-keys the agent — in EITHER the primary key or its (tier, leaf) slot — leaves no phantom route or
// stale secondary-index slice entry, then re-adds the current footprint. Concurrency-safe.
func (i *Index) Upsert(a *agentv1alpha1.Agent) {
	nn := types.NamespacedName{Namespace: a.Namespace, Name: a.Name}
	key := agentindex.ScopeIdentity(a)
	tl := tierLeafKey(HandleForAgent(a))
	target := targetFromAgent(a)

	i.mu.Lock()
	defer i.mu.Unlock()
	// Fully drop the object's previous footprint before re-adding: the primary key and the (tier, leaf)
	// slot can each change independently on a scope edit, so a conditional patch would leak one or both.
	i.removeLocked(nn)
	i.byKey[key] = target
	i.keyByObject[nn] = key
	i.byTierLeaf[tl] = append(i.byTierLeaf[tl], key)
	i.tlByObject[nn] = tl
}

// Remove deletes the route owned by nn, if any. Safe to call for an object that was never indexed.
func (i *Index) Remove(nn types.NamespacedName) {
	i.mu.Lock()
	defer i.mu.Unlock()
	i.removeLocked(nn)
}

// removeLocked drops every trace of nn from the primary and secondary indexes. Caller holds i.mu.
func (i *Index) removeLocked(nn types.NamespacedName) {
	if key, ok := i.keyByObject[nn]; ok {
		delete(i.byKey, key)
		delete(i.keyByObject, nn)
		if tl, ok := i.tlByObject[nn]; ok {
			i.byTierLeaf[tl] = removeString(i.byTierLeaf[tl], key)
			if len(i.byTierLeaf[tl]) == 0 {
				delete(i.byTierLeaf, tl) // don't leak empty slots
			}
			delete(i.tlByObject, nn)
		}
	}
}

// removeString returns s with the first occurrence of v removed (order-insensitive; the slice is a set).
func removeString(s []string, v string) []string {
	for idx, e := range s {
		if e == v {
			s[idx] = s[len(s)-1]
			return s[:len(s)-1]
		}
	}
	return s
}

// Lookup returns the Target for an identity key (agentindex.ScopeIdentity / Handle.RouteKey). The bool
// is false when no live agent occupies that key — the gateway turns that into a deterministic refusal,
// never a guess.
func (i *Index) Lookup(key string) (Target, bool) {
	i.mu.RLock()
	defer i.mu.RUnlock()
	t, ok := i.byKey[key]
	return t, ok
}

// LookupHandle resolves a parsed handle to the live agent(s) it names, routing each tier by the correct
// mechanism (the no-drift guarantee holds per tier):
//
//   - platform / cluster-admin: the full key is derivable from (leaf + project context), so it computes
//     the exact RouteKey and returns the single occupant (or none). A missing project context or an
//     unknown tier surfaces as the RouteKey error (ErrMissingProjectContext / ErrUnknownTier).
//   - developer-team: the handle names only a namespace, so it reads the byTierLeaf secondary index and
//     returns every live agent sharing that (tier, namespace) — 0, 1, or (multi-cluster future) more.
//
// It returns the matches WITHOUT deciding the outcome: the gateway maps 0 → ErrNoSuchTarget, 1 → route,
// >1 → clarify. It never guesses. The returned slice is freshly allocated and safe for the caller to keep.
func (i *Index) LookupHandle(h Handle, projectID string) ([]Target, error) {
	if h.Tier == agentv1alpha1.TierDeveloperTeam {
		i.mu.RLock()
		defer i.mu.RUnlock()
		keys := i.byTierLeaf[tierLeafKey(h)]
		out := make([]Target, 0, len(keys))
		seen := make(map[string]struct{}, len(keys))
		for _, k := range keys {
			if _, dup := seen[k]; dup {
				continue
			}
			seen[k] = struct{}{}
			if t, ok := i.byKey[k]; ok {
				out = append(out, t)
			}
		}
		return out, nil
	}

	// platform / cluster-admin (and any unknown tier): the exact key is computable from the handle.
	key, err := h.RouteKey(projectID)
	if err != nil {
		return nil, err
	}
	if t, ok := i.Lookup(key); ok {
		return []Target{t}, nil
	}
	return nil, nil
}

// Len reports how many routes are indexed (metrics/tests).
func (i *Index) Len() int {
	i.mu.RLock()
	defer i.mu.RUnlock()
	return len(i.byKey)
}

// targetFromAgent projects the routing-relevant fields out of an Agent CR (06 §2b). TopicName and
// AllowedUsers come from integration.googleChat — the Phase-2 dispatch platform (Pub/Sub re-publish to
// the target's own topic; the target pod's proxy drains it, Decision 2). AllowedUsers is carried verbatim
// so Authorize applies the CR's CLOSED allowlist and never the pod-env permissive default.
func targetFromAgent(a *agentv1alpha1.Agent) Target {
	t := Target{
		Identity: agentindex.ScopeIdentity(a),
		Tier:     agentindex.EffectiveTier(a),
		Handle:   HandleForAgent(a).Canonical(),
	}
	if in := a.Spec.Integration; in != nil && in.GoogleChat != nil {
		t.TopicName = in.GoogleChat.TopicName
		t.AllowedUsers = in.GoogleChat.AllowedUsers
	}
	return t
}
