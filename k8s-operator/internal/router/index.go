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
}

// NewIndex returns an empty routing table.
func NewIndex() *Index {
	return &Index{
		byKey:       make(map[string]Target),
		keyByObject: make(map[types.NamespacedName]string),
	}
}

// Upsert inserts or updates the route for a. It computes the identity key from the CR (agentindex, the
// single source of truth) and, if a's key changed since last seen (a scope edit), evicts the old key so
// no phantom route survives. Concurrency-safe.
func (i *Index) Upsert(a *agentv1alpha1.Agent) {
	nn := types.NamespacedName{Namespace: a.Namespace, Name: a.Name}
	key := agentindex.ScopeIdentity(a)
	target := targetFromAgent(a)

	i.mu.Lock()
	defer i.mu.Unlock()
	if prev, ok := i.keyByObject[nn]; ok && prev != key {
		// The agent's scope changed: drop the route it used to own so a stale key can't resolve.
		delete(i.byKey, prev)
	}
	i.byKey[key] = target
	i.keyByObject[nn] = key
}

// Remove deletes the route owned by nn, if any. Safe to call for an object that was never indexed.
func (i *Index) Remove(nn types.NamespacedName) {
	i.mu.Lock()
	defer i.mu.Unlock()
	if key, ok := i.keyByObject[nn]; ok {
		delete(i.byKey, key)
		delete(i.keyByObject, nn)
	}
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
