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

package verify

import (
	"context"
	"sync"
	"time"
)

// The 04 §4.2 cooldown constants. "An exponentially backed-off quiet period for that target."
const (
	// BaseCooldown is the first quiet period after a target's first rolled-back remediation.
	BaseCooldown = 5 * time.Minute
	// MaxCooldown bounds the doubling.
	//
	// Recorded tension rather than a hidden one: 09 §6 V-PRO-021's neighbour V-PRO-017 requires
	// that "successive cooldowns strictly increase", and any cap eventually stops them increasing.
	// The cap is set where the sequence 5m, 10m, 20m, 40m, 80m, 160m, 320m reaches it only after
	// SEVEN consecutive rolled-back remediations of one target -- a fleet state that needs a human,
	// not a longer timer. V-PRO-017 is phase 13 and is not claimed here; whoever picks it up owns
	// the choice between an uncapped sequence and a strictly-increasing one.
	MaxCooldown = 8 * time.Hour
	// CooldownDecay is how long a target must stay quiet before its consecutive count resets. A
	// count that never decays turns one bad week into a permanently untouchable target.
	CooldownDecay = 24 * time.Hour
)

// MemoryCooldown is an in-process CooldownRegistry. It is the reference implementation of the
// backoff rule and the one the L1 suite exercises.
//
// It is deliberately not the production store: a cooldown that dies with the broker process is a
// cooldown an operator can clear by deleting a pod, and 04 §4.2 controls must survive that. The
// durable implementation belongs with the rest of the brake, which owns `Agent.status.operations`.
type MemoryCooldown struct {
	mu      sync.Mutex
	entries map[string]*cooldownEntry
}

type cooldownEntry struct {
	until time.Time
	// consecutive counts rollbacks since the last decay, not cooldowns ever.
	consecutive int
	last        time.Time
}

// NewMemoryCooldown returns an empty registry.
func NewMemoryCooldown() *MemoryCooldown {
	return &MemoryCooldown{entries: map[string]*cooldownEntry{}}
}

// Enter starts or extends a target's cooldown and returns when it expires.
func (m *MemoryCooldown) Enter(_ context.Context, key string, now time.Time) (time.Time, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	e, ok := m.entries[key]
	if !ok {
		e = &cooldownEntry{}
		m.entries[key] = e
	}
	if ok && now.Sub(e.last) > CooldownDecay {
		e.consecutive = 0
	}
	e.consecutive++
	e.last = now

	d := CooldownFor(e.consecutive)
	until := now.Add(d)
	// Extend, never shorten: a second rollback inside an active cooldown must not reset the clock
	// to a shorter one just because the arithmetic happened to land there.
	if until.After(e.until) {
		e.until = until
	}
	return e.until, nil
}

// Active reports whether a target is in cooldown.
func (m *MemoryCooldown) Active(_ context.Context, key string, now time.Time) (bool, time.Time, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	e, ok := m.entries[key]
	if !ok || !now.Before(e.until) {
		return false, time.Time{}, nil
	}
	return true, e.until, nil
}

// CooldownFor is the backoff curve: BaseCooldown doubled per consecutive rollback, capped.
// `consecutive` is 1-based -- the first rollback of a target gets BaseCooldown.
func CooldownFor(consecutive int) time.Duration {
	if consecutive < 1 {
		consecutive = 1
	}
	d := BaseCooldown
	for i := 1; i < consecutive; i++ {
		d *= 2
		if d >= MaxCooldown {
			return MaxCooldown
		}
	}
	return d
}
