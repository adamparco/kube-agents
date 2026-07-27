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

package broker

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"net/http"
	"sync"
	"time"
)

// Anti-replay (06 §4.1). Three mechanisms, checked at pipeline step 2 -- BEFORE classification,
// because a replayed envelope must not reach the classifier at all: classification consults live
// cluster state, and an attacker who can make the broker classify at will has a state oracle.
//
// Three rather than one because each covers the other two's blind spot:
//
//  1. FRESHNESS bounds how long a captured envelope is worth anything, but on its own it lets the
//     same envelope be replayed unlimited times inside the window.
//  2. The NONCE makes each envelope single-use, but it is broker-local state and therefore lost on
//     restart -- which is why it fails closed rather than open.
//  3. The (identity, trace, key) TRIPLE survives a restart because it is derived from the record
//     store, and it catches the case where a caller legitimately re-obtains a nonce and resubmits
//     an identical action inside one conversation.
//
// A caller cannot satisfy all three by accident and cannot satisfy them at all with a captured
// body, which is the property being bought.
const (
	// FreshnessPast is how far in the past issuedAt may be.
	FreshnessPast = 120 * time.Second
	// FreshnessFuture is how far ahead it may be -- clock skew allowance, not a feature. Small on
	// purpose: a generous future window is a way to mint envelopes that stay valid for hours.
	FreshnessFuture = 30 * time.Second
	// NonceTTL is how long an issued nonce may go unused.
	NonceTTL = 120 * time.Second
	// MaxOutstandingNonces caps unredeemed nonces per caller. Without it, `GET /v1alpha1/nonce` in
	// a loop is an unauthenticated-shaped memory exhaustion against the one process that must stay
	// up for any mutation to be possible.
	MaxOutstandingNonces = 32
	// ReplayWindow is how long the triple and the idempotency record are retained.
	ReplayWindow = 24 * time.Hour
	// nonceBits is the CSPRNG width. 128 bits, so the birthday bound is irrelevant at any rate a
	// cluster can produce.
	nonceBytes = 16
	// sweepInterval bounds how often expired entries are reaped. Lazy rather than a goroutine:
	// a sweeper goroutine is a lifecycle to get wrong, and the maps are only reachable from the
	// request path anyway.
	sweepInterval = 60 * time.Second
)

// DedupEntry is what a repeated idempotency key returns instead of executing again (06 §4.1).
type DedupEntry struct {
	// ActionID is the name of the ActionRecord the first submission produced.
	ActionID string
	// Namespace is where that record lives.
	Namespace string
	// Decision is the outcome the first submission reached.
	Decision string
	// At is when it was recorded.
	At time.Time
}

type nonceEntry struct {
	caller  string
	expires time.Time
}

// ReplayGuard holds the broker-local anti-replay state.
//
// All of it is in memory and none of it is shared between replicas. That is a deliberate
// constraint on the deployment, not an oversight: a broker is one replica per agent (08 §2.3), so
// there is no second process to synchronise with, and introducing a shared store would put a
// network dependency in front of every mutation and turn its outage into a cluster-wide write
// freeze. The cost is the restart behaviour below, which is paid explicitly.
type ReplayGuard struct {
	mu sync.Mutex
	// Now is the clock. Injectable so the freshness and TTL boundaries can be tested at the
	// boundary rather than approached with sleeps.
	now func() time.Time

	startedAt time.Time
	lastSweep time.Time

	nonces      map[string]nonceEntry
	outstanding map[string]int
	triples     map[string]time.Time
	dedup       map[string]DedupEntry
}

// NewReplayGuard returns a guard whose start time is now. Pass nil for the clock to use time.Now.
func NewReplayGuard(now func() time.Time) *ReplayGuard {
	if now == nil {
		now = time.Now
	}
	t := now()
	return &ReplayGuard{
		now:         now,
		startedAt:   t,
		lastSweep:   t,
		nonces:      map[string]nonceEntry{},
		outstanding: map[string]int{},
		triples:     map[string]time.Time{},
		dedup:       map[string]DedupEntry{},
	}
}

// StartedAt is when this guard began accepting envelopes.
func (g *ReplayGuard) StartedAt() time.Time { return g.startedAt }

// IssueNonce mints a single-use nonce for caller (GET /v1alpha1/nonce).
//
// The nonce is broker-issued rather than caller-chosen. A caller-chosen nonce would only prove the
// caller can pick a number it has not picked before, which an attacker replaying a captured body
// can also do by editing one field -- and editing that field is free, because the nonce is
// excluded from the idempotency key.
func (g *ReplayGuard) IssueNonce(caller string) (string, error) {
	if caller == "" {
		return "", fmt.Errorf("broker: refusing to issue a nonce to an unidentified caller")
	}
	buf := make([]byte, nonceBytes)
	if _, err := rand.Read(buf); err != nil {
		// crypto/rand failing means the process cannot generate unpredictable values. Returning a
		// weak nonce would be worse than returning nothing: the mechanism would look present and
		// be guessable.
		return "", fmt.Errorf("broker: cannot generate a nonce: %w", err)
	}
	value := hex.EncodeToString(buf)

	g.mu.Lock()
	defer g.mu.Unlock()
	g.sweepLocked()

	if g.outstanding[caller] >= MaxOutstandingNonces {
		return "", &Refusal{
			Status: http.StatusTooManyRequests,
			Reason: "nonce-quota-exceeded",
			Detail: fmt.Sprintf(
				"%d nonces are already outstanding for this caller (the limit is %d); redeem or wait %s for them to expire",
				g.outstanding[caller], MaxOutstandingNonces, NonceTTL),
		}
	}
	g.nonces[value] = nonceEntry{caller: caller, expires: g.now().Add(NonceTTL)}
	g.outstanding[caller]++
	return value, nil
}

// Check runs all three mechanisms against an envelope from the authenticated identity.
//
// Every refusal here is journaled AND alarmed. A legitimate client does not replay: it either
// obtains a nonce and uses it once, or it gets a fresh one. So a refusal on this path is either a
// broken client -- worth an operator's attention on its own -- or somebody replaying, and the two
// are not distinguishable from inside the broker.
func (g *ReplayGuard) Check(id *Identity, e *Envelope) error {
	issued, err := ParseIssuedAt(e.IssuedAt)
	if err != nil {
		return invalid("issuedAt: %v", err)
	}

	g.mu.Lock()
	defer g.mu.Unlock()
	g.sweepLocked()
	now := g.now()

	// Mechanism 1: the freshness window.
	if issued.Before(now.Add(-FreshnessPast)) {
		return replayRefusal(ReasonEnvelopeExpired, fmt.Sprintf(
			"issuedAt %s is more than %s in the past", e.IssuedAt, FreshnessPast))
	}
	if issued.After(now.Add(FreshnessFuture)) {
		return replayRefusal(ReasonEnvelopeExpired, fmt.Sprintf(
			"issuedAt %s is more than %s in the future; check the caller's clock", e.IssuedAt, FreshnessFuture))
	}

	// Fail closed across a restart. Nonce state does not survive the process, so for any envelope
	// issued before this broker started we cannot say whether its nonce was already spent. The
	// alternative -- accepting it because we have no record of it -- would make a broker restart
	// the way to launder a replay, and a broker restart is something an attacker who can get a pod
	// OOM-killed can arrange. The cost is bounded: envelopes are valid for 120 seconds, so the
	// blast radius of a restart is at most 120 seconds of legitimate retries, each of which gets a
	// refusal that tells it exactly what to do.
	if issued.Before(g.startedAt) {
		return replayRefusal(ReasonReplayedEnvelope, fmt.Sprintf(
			"this broker started at %s and cannot prove the nonce of an envelope issued at %s was unused; obtain a fresh nonce and resubmit",
			g.startedAt.UTC().Format(time.RFC3339), e.IssuedAt))
	}

	// Mechanism 2: the single-use nonce. Consumed under the same lock that found it, so two
	// concurrent submissions of one captured envelope cannot both observe it as unspent.
	entry, ok := g.nonces[e.Nonce]
	if !ok {
		return replayRefusal(ReasonReplayedEnvelope,
			"the nonce is unknown, already spent, or expired; obtain one from GET /v1alpha1/nonce")
	}
	if entry.caller != id.Username {
		// A nonce issued to one caller and presented by another. Refused without consuming it:
		// consuming would let any authenticated caller burn another's nonces.
		return replayRefusal(ReasonReplayedEnvelope, "the nonce was issued to a different caller")
	}
	if !entry.expires.After(now) {
		g.consumeLocked(e.Nonce, entry)
		return replayRefusal(ReasonReplayedEnvelope, fmt.Sprintf("the nonce expired at %s", entry.expires.UTC().Format(time.RFC3339)))
	}
	g.consumeLocked(e.Nonce, entry)

	// Mechanism 3: the triple. Survives a restart if the caller's records do, and catches an
	// identical action resubmitted inside one conversation with a legitimately fresh nonce.
	//
	// Keyed on traceId as well as the idempotency key, which is what separates a replay from a
	// deduplication: the SAME action asked for again in the SAME trace is a replay; asked for in a
	// different trace it is a retry, and the dedup path below returns the first record instead.
	triple := id.Username + "\x1f" + e.Trace.TraceID + "\x1f" + e.IdempotencyKey
	if _, seen := g.triples[triple]; seen {
		return replayRefusal(ReasonReplayedEnvelope,
			"this (identity, traceId, idempotencyKey) was already submitted within the last 24 hours")
	}
	g.triples[triple] = now.Add(ReplayWindow)
	return nil
}

func replayRefusal(reason, detail string) *Refusal {
	return &Refusal{
		Status:        http.StatusForbidden,
		Reason:        reason,
		Detail:        detail,
		Journal:       true,
		SecurityEvent: true,
	}
}

func (g *ReplayGuard) consumeLocked(value string, entry nonceEntry) {
	delete(g.nonces, value)
	if n := g.outstanding[entry.caller]; n <= 1 {
		delete(g.outstanding, entry.caller)
	} else {
		g.outstanding[entry.caller] = n - 1
	}
}

// LookupDedup returns the record a previous submission of this key produced, if it is still within
// the 24-hour window.
func (g *ReplayGuard) LookupDedup(agentIdentity, key string) (DedupEntry, bool) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.sweepLocked()
	e, ok := g.dedup[agentIdentity+"\x1f"+key]
	return e, ok
}

// RememberDedup records the outcome of a submission so a later identical one can be answered from
// it. Called only after the record exists: remembering before would let a crash mid-flight leave a
// key pointing at a record nobody wrote.
func (g *ReplayGuard) RememberDedup(agentIdentity, key string, entry DedupEntry) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.dedup[agentIdentity+"\x1f"+key] = entry
}

// sweepLocked reaps expired entries, at most once per sweepInterval. The caller holds g.mu.
func (g *ReplayGuard) sweepLocked() {
	now := g.now()
	if now.Sub(g.lastSweep) < sweepInterval {
		return
	}
	g.lastSweep = now

	for value, entry := range g.nonces {
		if !entry.expires.After(now) {
			g.consumeLocked(value, entry)
		}
	}
	for triple, expires := range g.triples {
		if !expires.After(now) {
			delete(g.triples, triple)
		}
	}
	for key, entry := range g.dedup {
		if now.Sub(entry.At) >= ReplayWindow {
			delete(g.dedup, key)
		}
	}
}
