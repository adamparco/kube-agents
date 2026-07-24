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
	"time"
)

// defaultAffinityTTL bounds how long a thread stays bound to an agent without a new turn (06 §6). It is
// long enough to keep a working conversation sticky across normal back-and-forth, short enough that an
// abandoned thread's binding lapses (so a later, unrelated turn is resolved freshly rather than routed to
// a stale target). The durable §6 session store that replaces memAffinityStore carries the same TTL.
const defaultAffinityTTL = 30 * time.Minute

// AffinityStore remembers which agent a chat thread is currently talking to, so a bare follow-up sticks
// to that agent without re-resolving or spending inference (06 §6). It stores only the ROUTING KEY
// (agentindex.ScopeIdentity), never an authorization decision: the gateway still runs Authorize on every
// turn, so a binding can never precede or replace an access check. A binding is written ONLY after a
// successful authorized dispatch (see Gateway.Handle), which is why a thread can only ever become bound to
// an agent some prior turn was already allowed to reach.
//
// The Phase-3 implementation (memAffinityStore) is in-memory and per-router-replica; the router runs a
// single replica, so that is sufficient. The durable, cross-replica session store of 06 §6 is a drop-in
// replacement behind this interface — the gateway depends only on the three methods here.
type AffinityStore interface {
	// Lookup returns the routing key a thread is bound to and whether a live (non-expired) binding exists.
	// An empty threadID or an expired/absent binding returns ("", false).
	Lookup(threadID string) (key string, ok bool)
	// Bind records (or refreshes the TTL of) the thread→key binding. Called only after an authorized
	// dispatch. A no-op for an empty threadID or key.
	Bind(threadID, key string)
	// Drop removes any binding for the thread. The gateway calls it when a bound key no longer resolves to
	// a live agent (a stale binding), so the turn falls through to fresh resolution instead of dead-ending.
	Drop(threadID string)
}

// binding is one thread's current target plus the wall-clock instant it lapses.
type binding struct {
	key     string
	expires time.Time
}

// memAffinityStore is the Phase-3 in-memory AffinityStore: a TTL map guarded by a mutex, safe for the
// concurrent turns Pub/Sub delivers. The clock is injectable (now) so TTL expiry is testable without
// sleeping. It holds only (threadID → routing key) with an expiry — no message text, no sender, no authz
// state — so it can never become a shadow authorization cache.
type memAffinityStore struct {
	mu  sync.Mutex
	ttl time.Duration
	now func() time.Time
	m   map[string]binding
}

// newMemAffinityStore returns an empty in-memory store with the given TTL and the real clock.
func newMemAffinityStore(ttl time.Duration) *memAffinityStore {
	return &memAffinityStore{ttl: ttl, now: time.Now, m: make(map[string]binding)}
}

// NewAffinityStore returns the default Phase-3 in-memory affinity store (30m TTL). The gateway takes the
// AffinityStore interface, so a durable store can replace this without touching the routing path.
func NewAffinityStore() AffinityStore { return newMemAffinityStore(defaultAffinityTTL) }

// Lookup returns the bound key if the thread has a live binding, evicting it lazily on expiry.
func (s *memAffinityStore) Lookup(threadID string) (string, bool) {
	if threadID == "" {
		return "", false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	b, ok := s.m[threadID]
	if !ok {
		return "", false
	}
	if !s.now().Before(b.expires) { // now >= expires ⇒ lapsed
		delete(s.m, threadID)
		return "", false
	}
	return b.key, true
}

// Bind writes (or refreshes) the thread→key binding with a fresh TTL.
func (s *memAffinityStore) Bind(threadID, key string) {
	if threadID == "" || key == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.m[threadID] = binding{key: key, expires: s.now().Add(s.ttl)}
}

// Drop removes the thread's binding, if any.
func (s *memAffinityStore) Drop(threadID string) {
	if threadID == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.m, threadID)
}
