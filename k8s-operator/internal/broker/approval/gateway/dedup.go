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

// Package gateway is the ChatOps gateway: the one workload that runs as
// system:serviceaccount:kubeagents-system:kube-agents-chatops-gateway, and the only writer of
// ActionRecord.status.approvals in the system (docs/designs/broker/chat-approval.md §3). It parses
// typed approve/reject commands from a verified platform event, authorizes them against
// internal/broker/approval, and performs the sanctioned status write.
package gateway

import (
	"sync"
	"time"
)

// dedup remembers recently-seen platform event keys so a retried delivery of the same chat event
// does not run authorization twice (chat-approval.md §7: "replayed chat events are deduplicated by
// platform event ID in the gateway before authorization runs"). A plain in-memory map is
// sufficient because the gateway Deployment is single-replica with a leader lease (05 §1.8's
// one-socket rule, chat-approval.md §7) — there is never a second process for a key to be invisible
// to.
type dedup struct {
	mu   sync.Mutex
	seen map[string]time.Time
}

func newDedup() *dedup {
	return &dedup{seen: make(map[string]time.Time)}
}

// SeenRecently reports whether key was already recorded within window, and records it either way.
// The first call for a key is always false; every call also opportunistically evicts entries older
// than window, so the map cannot grow without bound across a long-running process.
func (d *dedup) SeenRecently(key string, window time.Duration, now time.Time) bool {
	d.mu.Lock()
	defer d.mu.Unlock()

	for k, t := range d.seen {
		if now.Sub(t) > window {
			delete(d.seen, k)
		}
	}

	last, ok := d.seen[key]
	d.seen[key] = now
	return ok && now.Sub(last) <= window
}
