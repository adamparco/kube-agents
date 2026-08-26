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

// CooldownSeries is the 04 §4.2 backoff as a fold over ONE target's failure times.
//
// It exists so the curve has one definition site with two consumers that arrive at it from opposite
// directions. MemoryCooldown folds events in as they happen, one per rollback, in real time.
// internal/broker/cooldown folds a sorted slice recovered from the journal, all at once, possibly
// after a restart. Those two must agree exactly -- a durable store that reconstructs a DIFFERENT
// quiet period from the same history is worse than no durable store, because it looks like the
// reference implementation and answers differently. Sharing the fold is what makes the agreement a
// property a test can assert rather than two transcriptions somebody has to keep in step.
//
// The zero value is a target with no history: Consecutive 0, Until zero, and Active false for every
// instant.
type CooldownSeries struct {
	// Consecutive is rollbacks since the last decay, not cooldowns ever.
	Consecutive int
	// Last is the most recent failure time folded in.
	Last time.Time
	// Until is when the quiet period ends.
	Until time.Time
}

// Add folds one failure at `at` into the series and returns the new expiry.
//
// Two rules, both of which look like edge-case handling and are not:
//
// The count DECAYS. A failure more than CooldownDecay after the previous one starts the sequence
// again at BaseCooldown, because a target that misbehaved twice in March is not a target that
// deserves an eight-hour quiet period in July.
//
// The expiry EXTENDS AND NEVER SHORTENS. `at` is not guaranteed monotonic: the journal fold sorts,
// but two brokers writing the same journal have two clocks, and a replayed history can hand this
// method a failure that predates one it has already seen. Taking the max means the answer does not
// depend on the order the events arrived in -- which is exactly what a store reconstructed from a
// list, in whatever order the API server returned it, has to be able to promise.
func (s *CooldownSeries) Add(at time.Time) time.Time {
	if s.Consecutive > 0 && at.Sub(s.Last) > CooldownDecay {
		s.Consecutive = 0
	}
	s.Consecutive++
	if at.After(s.Last) {
		s.Last = at
	}

	if until := at.Add(CooldownFor(s.Consecutive)); until.After(s.Until) {
		s.Until = until
	}
	return s.Until
}

// Active reports whether the quiet period covers `now`.
func (s CooldownSeries) Active(now time.Time) bool { return now.Before(s.Until) }

// MemoryCooldown is an in-process CooldownRegistry. It is the reference implementation of the
// backoff rule and the one the L1 suite exercises.
//
// It is deliberately not the production store: a cooldown that dies with the broker process is a
// cooldown an operator can clear by deleting a pod, and 04 §4.2 controls must survive that. The
// durable implementation is internal/broker/cooldown, which recovers the same series from the
// ActionRecord journal.
//
// It is NOT the store because the broker cannot write one. 06 §2.2.1 gives the broker's operations
// grant `get, list, watch` on `agents` and no write verb at all, so there is no Agent field it may
// persist a counter to; what it may write is `actionrecords` and `actionrecords/status`, which is
// why the durable answer is derived from the journal rather than stored beside it. An earlier
// version of this comment named `Agent.status.operations` as the eventual home. That was wrong on
// the only point that mattered -- the broker has no verb that reaches it.
type MemoryCooldown struct {
	mu      sync.Mutex
	entries map[string]*cooldownEntry
}

type cooldownEntry struct {
	series CooldownSeries
	// seen is the action IDs already folded into series, so a repeated Enter for one action does
	// not charge the target twice. Cleared whenever the series decays, which bounds it to the
	// failures inside one CooldownDecay window.
	seen map[string]bool
}

// NewMemoryCooldown returns an empty registry.
func NewMemoryCooldown() *MemoryCooldown {
	return &MemoryCooldown{entries: map[string]*cooldownEntry{}}
}

// Enter starts or extends a target's cooldown and returns when it expires.
func (m *MemoryCooldown) Enter(_ context.Context, actionID, key string, now time.Time) (time.Time, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	e, ok := m.entries[key]
	if !ok {
		e = &cooldownEntry{seen: map[string]bool{}}
		m.entries[key] = e
	}
	if actionID != "" && e.seen[actionID] {
		// Already charged. See CooldownRegistry.Enter for why idempotency is the caller's promise
		// to be able to rely on rather than a defensive nicety.
		return e.series.Until, nil
	}

	before := e.series.Consecutive
	until := e.series.Add(now)
	if e.series.Consecutive <= before {
		// The series decayed and restarted; the IDs it was counting are outside the window now.
		e.seen = map[string]bool{}
	}
	if actionID != "" {
		e.seen[actionID] = true
	}
	return until, nil
}

// Active reports whether a target is in cooldown.
func (m *MemoryCooldown) Active(_ context.Context, key string, now time.Time) (bool, time.Time, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	e, ok := m.entries[key]
	if !ok || !e.series.Active(now) {
		return false, time.Time{}, nil
	}
	return true, e.series.Until, nil
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
