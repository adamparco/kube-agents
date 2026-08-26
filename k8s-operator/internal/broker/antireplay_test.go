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
	"net/http"
	"sync"
	"testing"
	"time"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

// The three anti-replay mechanisms of 06 §4.1, each tested for the blind spot the other two cover.
//
// Every case drives an injected clock. Sleeping to cross a 120-second boundary is not a test, it is
// a two-minute test that still only proves the boundary is somewhere near there; with an injected
// clock the assertion is at the boundary, on both sides of it, exactly.

// clock is a settable time source.
type clock struct {
	mu sync.Mutex
	t  time.Time
}

func newClock(t time.Time) *clock { return &clock{t: t} }

func (c *clock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *clock) advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
}

// A fixed, readable base instant. Not time.Now(): a test whose inputs move is a test that can fail
// on a leap second and pass on a rerun.
var testNow = time.Date(2026, 7, 27, 14, 30, 0, 0, time.UTC)

func testIdentity() *Identity {
	return &Identity{
		Username:       testUsername,
		Namespace:      testNamespace,
		ServiceAccount: testReaderSA,
		AgentName:      "platform-agent",
		Tier:           agentv1alpha1.TierPlatform,
		Scope:          "adamparco-kage",
	}
}

// guardEnvelope is a minimal envelope carrying the fields the guard reads: issuedAt, nonce, trace
// and idempotencyKey. It deliberately does NOT go through DecodeEnvelope -- the guard's contract is
// with the struct, and building it directly keeps a validation change from breaking these tests for
// a reason that has nothing to do with replay.
func guardEnvelope(issuedAt time.Time, nonce, traceID, key string) *Envelope {
	return &Envelope{
		IssuedAt:       issuedAt.UTC().Format(time.RFC3339),
		Nonce:          nonce,
		Trace:          Trace{TraceID: traceID},
		IdempotencyKey: key,
	}
}

const (
	testTraceID = "4bf92f3577b34da6a3ce929d0e0e4736"
	testKey     = "sha256:" + "0000000000000000000000000000000000000000000000000000000000000001"
)

// mustNonce issues a nonce and fails the test if it could not.
func mustNonce(t *testing.T, g *ReplayGuard, caller string) string {
	t.Helper()
	n, err := g.IssueNonce(caller)
	if err != nil {
		t.Fatalf("IssueNonce: %v", err)
	}
	return n
}

// Mechanism 1: the freshness window, asserted at both boundaries.
//
// The window is asymmetric on purpose -- 120s back, 30s forward -- and the asymmetry is the part
// worth pinning. A generous future bound is not a clock-skew allowance, it is a way to mint
// envelopes that stay valid for as long as the bound: an attacker with one captured signing path
// and a +1h window has an hour of replay, and nothing else in the system would notice.
func TestFreshnessWindow(t *testing.T) {
	cases := []struct {
		name   string
		offset time.Duration
		ok     bool
	}{
		{"now", 0, true},
		{"just inside the past bound", -FreshnessPast + time.Second, true},
		{"exactly at the past bound", -FreshnessPast, true},
		{"one second past the bound", -FreshnessPast - time.Second, false},
		{"an hour old", -time.Hour, false},
		{"just inside the future bound", FreshnessFuture - time.Second, true},
		{"exactly at the future bound", FreshnessFuture, true},
		{"one second beyond the future bound", FreshnessFuture + time.Second, false},
		{"an hour ahead", time.Hour, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			cl := newClock(testNow)
			// Started well before, so the restart guard is not what refuses these.
			g := NewReplayGuard(cl.Now)
			g.startedAt = testNow.Add(-time.Hour)

			issued := testNow.Add(c.offset)
			nonce := mustNonce(t, g, testUsername)
			err := g.Check(testIdentity(), guardEnvelope(issued, nonce, testTraceID, testKey))

			if c.ok {
				if err != nil {
					t.Fatalf("offset %s was refused: %v", c.offset, err)
				}
				return
			}
			ref := refusalOf(t, err)
			if ref.Reason != ReasonEnvelopeExpired {
				t.Fatalf("reason = %q, want %q", ref.Reason, ReasonEnvelopeExpired)
			}
			if ref.Status != http.StatusForbidden {
				t.Fatalf("status = %d, want 403", ref.Status)
			}
			if !ref.Journal || !ref.SecurityEvent {
				t.Fatalf("a stale envelope must be journaled AND alarmed; Journal=%v SecurityEvent=%v", ref.Journal, ref.SecurityEvent)
			}
		})
	}
}

// Mechanism 1's blind spot, stated: freshness alone permits unlimited replay inside the window.
// This is the test that says WHY there are two more mechanisms -- it passes the freshness check
// twice with the same body and is stopped by the nonce.
func TestFreshnessAloneWouldPermitReplay(t *testing.T) {
	cl := newClock(testNow)
	g := NewReplayGuard(cl.Now)
	g.startedAt = testNow.Add(-time.Hour)

	nonce := mustNonce(t, g, testUsername)
	env := guardEnvelope(testNow, nonce, testTraceID, testKey)

	if err := g.Check(testIdentity(), env); err != nil {
		t.Fatalf("first submission was refused: %v", err)
	}
	// Same body, one second later. Still fresh -- and still refused.
	cl.advance(time.Second)
	ref := refusalOf(t, g.Check(testIdentity(), env))
	if ref.Reason != ReasonReplayedEnvelope {
		t.Fatalf("reason = %q, want %q", ref.Reason, ReasonReplayedEnvelope)
	}
}

// Mechanism 2: the nonce is single-use, broker-issued, caller-bound and expiring.
func TestNonceIsSingleUse(t *testing.T) {
	cl := newClock(testNow)
	g := NewReplayGuard(cl.Now)
	g.startedAt = testNow.Add(-time.Hour)

	nonce := mustNonce(t, g, testUsername)
	if err := g.Check(testIdentity(), guardEnvelope(testNow, nonce, testTraceID, testKey)); err != nil {
		t.Fatalf("first use was refused: %v", err)
	}
	// Second use, with a DIFFERENT trace and key so only the nonce can be what refuses it.
	err := g.Check(testIdentity(), guardEnvelope(testNow, nonce, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", testKey))
	if got := refusalOf(t, err).Reason; got != ReasonReplayedEnvelope {
		t.Fatalf("reason = %q, want %q", got, ReasonReplayedEnvelope)
	}
}

func TestNonceMustBeBrokerIssued(t *testing.T) {
	cl := newClock(testNow)
	g := NewReplayGuard(cl.Now)
	g.startedAt = testNow.Add(-time.Hour)

	// A caller-chosen nonce. Well-formed, 32 hex characters, and worthless: it proves only that the
	// caller can pick a value, which is exactly what a replayer editing one field can also do.
	err := g.Check(testIdentity(), guardEnvelope(testNow, "deadbeefdeadbeefdeadbeefdeadbeef", testTraceID, testKey))
	if got := refusalOf(t, err).Reason; got != ReasonReplayedEnvelope {
		t.Fatalf("reason = %q, want %q", got, ReasonReplayedEnvelope)
	}
}

// A nonce issued to one caller and presented by another. Refused WITHOUT being consumed: consuming
// it would let any authenticated caller burn a peer's nonces by guessing nothing at all -- it would
// only need to present them, and it would be a denial of service against another agent's ability to
// act, delivered through the anti-replay mechanism itself.
func TestNonceIsBoundToItsCaller(t *testing.T) {
	cl := newClock(testNow)
	g := NewReplayGuard(cl.Now)
	g.startedAt = testNow.Add(-time.Hour)

	nonce := mustNonce(t, g, testUsername)

	other := testIdentity()
	other.Username = "system:serviceaccount:other-ns:other-sa"
	err := g.Check(other, guardEnvelope(testNow, nonce, testTraceID, testKey))
	if got := refusalOf(t, err).Reason; got != ReasonReplayedEnvelope {
		t.Fatalf("reason = %q, want %q", got, ReasonReplayedEnvelope)
	}

	// The rightful owner can still use it.
	if err := g.Check(testIdentity(), guardEnvelope(testNow, nonce, testTraceID, testKey)); err != nil {
		t.Fatalf("the nonce was consumed by the foreign caller's attempt: %v", err)
	}
}

func TestNonceExpires(t *testing.T) {
	cl := newClock(testNow)
	g := NewReplayGuard(cl.Now)
	g.startedAt = testNow.Add(-time.Hour)

	nonce := mustNonce(t, g, testUsername)

	// One tick past the TTL. issuedAt moves with the clock so freshness is not what refuses it.
	cl.advance(NonceTTL + time.Second)
	err := g.Check(testIdentity(), guardEnvelope(cl.Now(), nonce, testTraceID, testKey))
	if got := refusalOf(t, err).Reason; got != ReasonReplayedEnvelope {
		t.Fatalf("reason = %q, want %q", got, ReasonReplayedEnvelope)
	}
}

// The outstanding-nonce quota. Without it, `GET /v1alpha1/nonce` in a loop is memory exhaustion
// against the one process that must stay up for any mutation to be possible -- and it needs no
// exploit, only a client with a retry bug.
func TestNonceQuota(t *testing.T) {
	cl := newClock(testNow)
	g := NewReplayGuard(cl.Now)

	for i := 0; i < MaxOutstandingNonces; i++ {
		if _, err := g.IssueNonce(testUsername); err != nil {
			t.Fatalf("nonce %d of %d was refused: %v", i+1, MaxOutstandingNonces, err)
		}
	}
	_, err := g.IssueNonce(testUsername)
	ref := refusalOf(t, err)
	if ref.Status != http.StatusTooManyRequests {
		t.Fatalf("status = %d, want 429", ref.Status)
	}
	if ref.Reason != "nonce-quota-exceeded" {
		t.Fatalf("reason = %q, want nonce-quota-exceeded", ref.Reason)
	}
	// The quota is PER CALLER. A noisy agent must not be able to lock out a quiet one.
	if _, err := g.IssueNonce("system:serviceaccount:other-ns:other-sa"); err != nil {
		t.Fatalf("a second caller was refused a nonce because the first exhausted its quota: %v", err)
	}
	// Redeeming one frees a slot.
	nonce := mustNonce(t, g, "system:serviceaccount:other-ns:other-sa")
	other := testIdentity()
	other.Username = "system:serviceaccount:other-ns:other-sa"
	g.startedAt = testNow.Add(-time.Hour)
	if err := g.Check(other, guardEnvelope(testNow, nonce, testTraceID, testKey)); err != nil {
		t.Fatalf("redeeming: %v", err)
	}
	if n := g.outstanding["system:serviceaccount:other-ns:other-sa"]; n != 1 {
		t.Fatalf("outstanding after redeeming one of two = %d, want 1", n)
	}
}

func TestNonceIsUnpredictable(t *testing.T) {
	g := NewReplayGuard(newClock(testNow).Now)
	seen := map[string]bool{}
	for i := 0; i < MaxOutstandingNonces; i++ {
		n := mustNonce(t, g, testUsername)
		if len(n) != nonceBytes*2 {
			t.Fatalf("nonce %q is %d hex characters, want %d (%d bits)", n, len(n), nonceBytes*2, nonceBytes*8)
		}
		if !hex32Re.MatchString(n) {
			t.Fatalf("nonce %q is not 32 lowercase hex characters; the envelope schema would refuse it", n)
		}
		if seen[n] {
			t.Fatalf("nonce %q was issued twice", n)
		}
		seen[n] = true
	}
}

func TestNonceRefusesAnUnidentifiedCaller(t *testing.T) {
	g := NewReplayGuard(newClock(testNow).Now)
	if _, err := g.IssueNonce(""); err == nil {
		t.Fatal("a nonce was issued to an empty caller; it would be redeemable by anyone")
	}
}

// Mechanism 3: the (identity, traceId, idempotencyKey) triple.
//
// Its job is the case the nonce cannot see: a caller that legitimately obtains a FRESH nonce and
// resubmits the identical action inside the same conversation. Every input here is new except the
// triple, so nothing else can be what refuses it.
func TestTripleUniqueness(t *testing.T) {
	cl := newClock(testNow)
	g := NewReplayGuard(cl.Now)
	g.startedAt = testNow.Add(-time.Hour)

	first := mustNonce(t, g, testUsername)
	if err := g.Check(testIdentity(), guardEnvelope(testNow, first, testTraceID, testKey)); err != nil {
		t.Fatalf("first submission: %v", err)
	}

	second := mustNonce(t, g, testUsername)
	err := g.Check(testIdentity(), guardEnvelope(testNow, second, testTraceID, testKey))
	ref := refusalOf(t, err)
	if ref.Reason != ReasonReplayedEnvelope {
		t.Fatalf("reason = %q, want %q", ref.Reason, ReasonReplayedEnvelope)
	}
	if !ref.Journal || !ref.SecurityEvent {
		t.Fatalf("a replayed triple must be journaled AND alarmed; Journal=%v SecurityEvent=%v", ref.Journal, ref.SecurityEvent)
	}
}

// The triple is keyed on traceId as well as the key, and that is what separates a REPLAY from a
// DEDUPLICATION. Same action in the same trace: replay, refused. Same action in a different trace:
// a retry, which the dedup path answers with the original record instead.
func TestTripleDistinguishesTraceFromKey(t *testing.T) {
	cl := newClock(testNow)
	g := NewReplayGuard(cl.Now)
	g.startedAt = testNow.Add(-time.Hour)

	n1 := mustNonce(t, g, testUsername)
	if err := g.Check(testIdentity(), guardEnvelope(testNow, n1, testTraceID, testKey)); err != nil {
		t.Fatalf("first: %v", err)
	}

	// Different trace, same key: passes the guard. The server's dedup lookup handles it from here.
	n2 := mustNonce(t, g, testUsername)
	if err := g.Check(testIdentity(), guardEnvelope(testNow, n2, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", testKey)); err != nil {
		t.Fatalf("the same key in a different trace is a retry, not a replay, and must pass the guard: %v", err)
	}

	// Different identity, same trace and key: also passes. Two agents are two accountabilities.
	n3 := mustNonce(t, g, "system:serviceaccount:other-ns:other-sa")
	other := testIdentity()
	other.Username = "system:serviceaccount:other-ns:other-sa"
	if err := g.Check(other, guardEnvelope(testNow, n3, testTraceID, testKey)); err != nil {
		t.Fatalf("a different identity must not collide on another's triple: %v", err)
	}
}

// The restart behaviour, and the reason it is the way it is.
//
// Nonce state is broker-local and does not survive the process, so after a restart the broker
// cannot prove that any pre-restart envelope's nonce was unspent. It refuses them. The alternative
// -- accepting because there is no record -- would make "restart the broker" the way to launder a
// replay, and a broker restart is something an attacker who can get a pod OOM-killed can arrange.
func TestRestartFailsClosed(t *testing.T) {
	cl := newClock(testNow)
	g := NewReplayGuard(cl.Now) // started at testNow

	// An envelope issued 10 seconds before this broker came up. Comfortably fresh, well-formed,
	// and unprovable.
	issued := testNow.Add(-10 * time.Second)
	nonce := mustNonce(t, g, testUsername)

	err := g.Check(testIdentity(), guardEnvelope(issued, nonce, testTraceID, testKey))
	ref := refusalOf(t, err)
	if ref.Reason != ReasonReplayedEnvelope {
		t.Fatalf("reason = %q, want %q", ref.Reason, ReasonReplayedEnvelope)
	}
	if ref.Status != http.StatusForbidden || !ref.Journal || !ref.SecurityEvent {
		t.Fatalf("status=%d journal=%v security=%v, want 403 with both", ref.Status, ref.Journal, ref.SecurityEvent)
	}

	// The blast radius is bounded and this is the assertion of that bound: an envelope issued AFTER
	// the restart goes through immediately. A broker that failed closed on everything would be a
	// broker that never recovers.
	after := mustNonce(t, g, testUsername)
	if err := g.Check(testIdentity(), guardEnvelope(testNow.Add(time.Second), after, testTraceID, testKey)); err != nil {
		t.Fatalf("a post-restart envelope was refused: %v", err)
	}
}

// The order of the mechanisms. Freshness runs before the nonce, so a stale envelope does not burn
// one -- otherwise a caller with a slow clock would exhaust its quota and start getting a 429 that
// says nothing about the actual problem.
func TestStaleEnvelopeDoesNotConsumeItsNonce(t *testing.T) {
	cl := newClock(testNow)
	g := NewReplayGuard(cl.Now)
	g.startedAt = testNow.Add(-time.Hour)

	nonce := mustNonce(t, g, testUsername)
	stale := testNow.Add(-FreshnessPast - time.Minute)
	if got := refusalOf(t, g.Check(testIdentity(), guardEnvelope(stale, nonce, testTraceID, testKey))).Reason; got != ReasonEnvelopeExpired {
		t.Fatalf("reason = %q, want %q", got, ReasonEnvelopeExpired)
	}

	// The nonce survived, so a resubmission with a corrected timestamp works.
	if err := g.Check(testIdentity(), guardEnvelope(testNow, nonce, testTraceID, testKey)); err != nil {
		t.Fatalf("the nonce was consumed by a stale submission: %v", err)
	}
}

// An unparseable issuedAt is a 400 invalid-envelope, not a 403 replay. The distinction matters
// because 403s are alarmed: a client with a date-formatting bug would otherwise page somebody.
func TestGuardRejectsUnparseableIssuedAt(t *testing.T) {
	g := NewReplayGuard(newClock(testNow).Now)
	env := &Envelope{IssuedAt: "yesterday", Nonce: "deadbeefdeadbeefdeadbeefdeadbeef", Trace: Trace{TraceID: testTraceID}}
	ref := refusalOf(t, g.Check(testIdentity(), env))
	if ref.Reason != ReasonInvalid {
		t.Fatalf("reason = %q, want %q", ref.Reason, ReasonInvalid)
	}
	if ref.SecurityEvent {
		t.Fatal("a malformed timestamp is a client bug, not a security event")
	}
}

// Dedup records are per (agentIdentity, key) and are returned within the window.
func TestDedupRoundTrip(t *testing.T) {
	cl := newClock(testNow)
	g := NewReplayGuard(cl.Now)

	if _, ok := g.LookupDedup("platform/adamparco-kage", testKey); ok {
		t.Fatal("an unrecorded key was found")
	}
	g.RememberDedup("platform/adamparco-kage", testKey, DedupEntry{
		ActionID: "act-1", Namespace: testNamespace, Decision: "accepted", At: testNow,
	})

	got, ok := g.LookupDedup("platform/adamparco-kage", testKey)
	if !ok || got.ActionID != "act-1" || got.Decision != "accepted" {
		t.Fatalf("LookupDedup = %+v, %v; want the recorded entry", got, ok)
	}
	// A different agent with the same key is a different action. If this ever returned the entry
	// above, one agent could suppress another's write by computing its key.
	if _, ok := g.LookupDedup("developer-team/adamparco-kage/prod/checkout", testKey); ok {
		t.Fatal("a dedup entry leaked across agent identities")
	}
}

// Retention. Entries older than the 24-hour window are reaped, and the sweep is what stops an
// always-on broker's maps from being an unbounded memory leak dressed as a security control.
func TestSweepReapsExpiredState(t *testing.T) {
	cl := newClock(testNow)
	g := NewReplayGuard(cl.Now)
	g.startedAt = testNow.Add(-time.Hour)

	nonce := mustNonce(t, g, testUsername)
	if err := g.Check(testIdentity(), guardEnvelope(testNow, nonce, testTraceID, testKey)); err != nil {
		t.Fatalf("seed submission: %v", err)
	}
	g.RememberDedup("platform/adamparco-kage", testKey, DedupEntry{ActionID: "act-1", At: testNow})
	// An unredeemed nonce, to prove the sweep reaps those too and returns the quota slot.
	stray := mustNonce(t, g, testUsername)

	if len(g.triples) != 1 {
		t.Fatalf("triples = %d, want 1", len(g.triples))
	}

	// Past the replay window. LookupDedup sweeps on the way in.
	cl.advance(ReplayWindow + time.Minute)
	if _, ok := g.LookupDedup("platform/adamparco-kage", testKey); ok {
		t.Fatal("a dedup entry survived the 24-hour window")
	}
	if len(g.triples) != 0 {
		t.Fatalf("triples = %d after the window, want 0", len(g.triples))
	}
	if _, ok := g.nonces[stray]; ok {
		t.Fatal("an expired nonce was not reaped")
	}
	if n := g.outstanding[testUsername]; n != 0 {
		t.Fatalf("outstanding = %d after the sweep, want 0; reaping must return the quota slot", n)
	}
}

// Concurrency. Two goroutines submitting one captured envelope must produce exactly one success --
// the check-and-consume has to be atomic, and a read-then-write split would let both observe the
// nonce as unspent. Run under -race this is the test that catches the split.
func TestConcurrentReplayYieldsOneWinner(t *testing.T) {
	cl := newClock(testNow)
	g := NewReplayGuard(cl.Now)
	g.startedAt = testNow.Add(-time.Hour)

	nonce := mustNonce(t, g, testUsername)

	const racers = 16
	var wg sync.WaitGroup
	results := make([]error, racers)
	start := make(chan struct{})
	for i := 0; i < racers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			results[i] = g.Check(testIdentity(), guardEnvelope(testNow, nonce, testTraceID, testKey))
		}(i)
	}
	close(start)
	wg.Wait()

	wins := 0
	for _, err := range results {
		if err == nil {
			wins++
		}
	}
	if wins != 1 {
		t.Fatalf("%d of %d concurrent submissions of one envelope succeeded, want exactly 1", wins, racers)
	}
}
