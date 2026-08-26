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

// Package cooldown is the durable half of 04 §4.2's quiet period: the production implementation of
// verify.CooldownRegistry, recovered from the ActionRecord journal rather than held in a map.
//
// # What was wrong with the map
//
// verify.MemoryCooldown says it in its own doc comment: "a cooldown that dies with the broker
// process is a cooldown an operator can clear by deleting a pod, and 04 §4.2 controls must survive
// that." That is not a hypothetical. The whole point of the cooldown is the case where an agent and
// the cluster are fighting over one object, which is also the case in which somebody restarts the
// broker to see if that helps. An anti-thrash control that the thrashing itself resets is decoration.
//
// # Why the journal, and not a new field or a new CRD
//
// Three candidates, and two of them are unavailable rather than unattractive.
//
// `Agent.status.operations` is what the MemoryCooldown comment used to point at, and the broker
// cannot write it: 06 §2.2.1 grants `get, list, watch` on `agents` and no write verb, and
// V-BRK-013 asserts that grant EXACTLY and is BLOCKING-ALWAYS, so widening it is not a move
// available to an implementation. A new CRD holding cooldown state would be a 06 §1 amendment,
// which is spec work and not a silence an implementer may fill (PROTOCOL §10.5).
//
// The journal is neither. It already exists, the broker already writes it, an agent cannot delete
// from it (06 §2.2.1 withholds `update` and `delete` on `actionrecords` deliberately), and the
// retention floor -- 30 days for `routine`, the shortest class -- is comfortably longer than
// verify.CooldownDecay's 24 hours, so a record can never age out while it still governs a quiet
// period. Crucially the cooldown is ALREADY a function of the journal: "after a failed or
// rolled-back remediation of a target" (04 §4.2) is a query over `status.phase` and `spec.targets`.
// Storing a counter beside it would be a second copy of a fact the journal holds, and the two would
// eventually disagree.
//
// This is the same shape 06 §4.4 chose for the contested index, and for the reason recorded there:
// "the index is authoritative because a deleted object cannot hold an annotation". Derived from the
// durable record, cached for reading, never the record itself.
//
// # The window between the rollback and the write
//
// verify.Driver.enterCooldown runs inside rollBack, BEFORE its caller writes `status.phase`. A
// purely derived registry would therefore answer "no cooldown" for the entire interval between the
// rollback and the status update -- which is precisely the interval in which the next action
// arrives, because whatever is driving the flap is still driving it.
//
// So Source is a COMPOSITION: the journal is the durable set of failure events, and an in-process
// overlay holds the ones this broker has been told about but cannot yet see. They are unioned BY
// ACTION ID, never summed, which is why verify.CooldownRegistry.Enter takes the ID. A pending event
// retires by itself the moment the journal shows the same ID, with no bookkeeping and no window in
// which one rollback is counted twice.
//
// # What is still lost
//
// A rollback whose `status.phase` write never lands -- the broker is killed between the two -- is a
// failure event no future process can recover, because nothing durable records it. That residual is
// smaller than the one it replaces (a whole process's worth of cooldowns, versus the tail of one
// action) and it falls in the loosening direction, so it is named here rather than implied. Closing
// it means making the phase write part of the same transaction as the rollback, which the Kubernetes
// API does not offer.
package cooldown

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"sync"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/verify"
)

const (
	// DefaultCacheTTL is how long a journal read is reused before Active goes back to the API
	// server.
	//
	// 05 §1 step 5 calls the brake's reads "informer-cached", and this is that requirement met the
	// way the rest of the broker meets it. cmd/broker/main.go builds a DIRECT client and says why --
	// "the broker's reads are its own writes read back, and a cache would answer 'did the record
	// land?' from a watch that may not have caught up" -- and both broker caches that do exist
	// (livestate's per-scope reads, policy.Source's poll) are TTL-bounded for the reason
	// broker.MaxFreezeStaleness spells out: an informer that silently stops delivering is not an
	// error, it is instant confident answers from before the incident started.
	//
	// Five seconds because the read is once per action per step 5 and the thing it must not miss is
	// another replica's rollback landing seconds ago. This broker's OWN rollbacks do not depend on
	// the TTL at all -- the overlay has them synchronously.
	DefaultCacheTTL = 5 * time.Second

	// MaxJournalStaleness is how old the last successful journal read may be before Active refuses
	// to answer at all. Deliberately the same 30 seconds as broker.MaxFreezeStaleness and
	// policy.MaxPolicyStaleness, because it is the same failure: a cached answer that is no longer
	// evidence about now.
	//
	// Past it Active returns an error rather than false, and the direction is the whole point. A
	// registry that answers "not in cooldown" when it means "I could not look" hands the agent the
	// permissive answer at exactly the moment the cluster is unwell -- see the same argument at
	// broker.contestedRefusal, which refuses when the contested index is unavailable for precisely
	// this reason.
	MaxJournalStaleness = 30 * time.Second
)

// Journal is the one API-server verb this source needs. Narrow on purpose, the same way
// policy.Lister is: 06 §2.2.1 gives the broker `get, list, watch, create` on `actionrecords`, and a
// dependency typed as the full client.Client would make it possible to write an update here and
// discover the missing verb at L2.
type Journal interface {
	List(ctx context.Context, list client.ObjectList, opts ...client.ListOption) error
}

// SourceConfig assembles a Source.
type SourceConfig struct {
	// Journal lists ActionRecord objects.
	Journal Journal

	// Namespace is the agent's own namespace. ActionRecords are namespaced and live beside the
	// agent that wrote them (06 §4.3), and the broker's Role is namespaced too -- a cluster-wide
	// List would be Forbidden, not merely broad.
	Namespace string

	// CacheTTL defaults to DefaultCacheTTL. Values at or above MaxJournalStaleness are rejected by
	// NewSource, following policy.NewSource: a cache that cannot refresh faster than it goes stale
	// refuses more often than it answers.
	CacheTTL time.Duration

	// Now is injectable so a test can age a snapshot without sleeping.
	Now func() time.Time
}

// Source is the durable verify.CooldownRegistry.
//
// Read the package comment first; it holds the argument. This type is the mechanism: a TTL-bounded
// snapshot of the journal's failure events, an overlay of the events this process has been told
// about and cannot yet see, and one fold (verify.CooldownSeries) over their union.
type Source struct {
	journal   Journal
	namespace string
	ttl       time.Duration
	now       func() time.Time

	mu sync.Mutex
	// events is the journal-derived set, targetKey -> action ID -> failure time.
	events map[string]map[string]time.Time
	// pending is the overlay: entered here, not yet visible in the journal. Same shape, so the
	// union is a map merge and double-counting is impossible by construction rather than by care.
	pending map[string]map[string]time.Time
	readAt  time.Time
}

var _ verify.CooldownRegistry = (*Source)(nil)

// NewSource validates the wiring. It does not read; call Refresh, which startup should do
// synchronously for the reason policy.Source.Refresh gives.
func NewSource(cfg SourceConfig) (*Source, error) {
	if cfg.Journal == nil {
		return nil, errors.New("cooldown: a Journal is required; a source that cannot list ActionRecords would report every target as quiet")
	}
	if cfg.Namespace == "" {
		return nil, errors.New("cooldown: a Namespace is required; ActionRecords are namespaced and an unscoped List is Forbidden to the broker's Role (06 §2.2.1)")
	}
	if cfg.CacheTTL < 0 {
		return nil, fmt.Errorf("cooldown: CacheTTL %s is negative", cfg.CacheTTL)
	}
	if cfg.CacheTTL == 0 {
		cfg.CacheTTL = DefaultCacheTTL
	}
	if cfg.CacheTTL >= MaxJournalStaleness {
		return nil, fmt.Errorf(
			"cooldown: CacheTTL %s is not shorter than MaxJournalStaleness %s; the snapshot would go stale between refreshes and the registry would refuse to answer",
			cfg.CacheTTL, MaxJournalStaleness)
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	return &Source{
		journal:   cfg.Journal,
		namespace: cfg.Namespace,
		ttl:       cfg.CacheTTL,
		now:       cfg.Now,
		events:    map[string]map[string]time.Time{},
		pending:   map[string]map[string]time.Time{},
	}, nil
}

// Refresh performs one read and rebuilds the snapshot.
//
// A read failure RETAINS the previous snapshot and lets it age, exactly as policy.Source does and
// for the same reason: discarding on the first dropped request turns a control-plane blip into a
// broker that refuses everything. There is no second failure class here -- unlike a ChangePolicy, an
// ActionRecord that will not parse cannot exist, because the list is typed.
func (s *Source) Refresh(ctx context.Context) error { return s.refresh(ctx, s.now()) }

// refresh takes the instant to stamp the read at. Enter and Active pass THEIR `now` rather than
// s.now(), so the freshness arithmetic is done on one clock; a snapshot stamped from the config
// clock and aged against the caller's is stale or fresh by accident.
func (s *Source) refresh(ctx context.Context, at time.Time) error {
	var list agentv1alpha1.ActionRecordList
	if err := s.journal.List(ctx, &list, client.InNamespace(s.namespace)); err != nil {
		return fmt.Errorf("cooldown: listing ActionRecords in %s: %w", s.namespace, err)
	}

	events := derive(list.Items)

	s.mu.Lock()
	s.events = events
	s.readAt = at
	s.mu.Unlock()
	return nil
}

// Enter records a failed or rolled-back remediation and returns the target's new expiry.
//
// The event goes into the overlay FIRST and unconditionally, before any read is attempted. That
// ordering is the one thing this method must not get wrong: a cooldown skipped because the journal
// happened to be unreadable for a second is the exact failure the durable store exists to prevent,
// and it fails in the direction where the agent keeps acting.
//
// The returned error therefore describes the READ, not the record. When it is non-nil the returned
// expiry is the best composition available -- which may UNDERSTATE the quiet period, because a
// journal that could not be read may hold earlier failures that would have raised `consecutive`,
// and never overstates it. verify.Driver.enterCooldown discards the value on error and reports the
// remaining targets, which is the right call for a field that is status reporting; enforcement is
// Active's job and Active sees the overlay either way.
func (s *Source) Enter(ctx context.Context, actionID, targetKey string, now time.Time) (time.Time, error) {
	s.mu.Lock()
	if s.pending[targetKey] == nil {
		s.pending[targetKey] = map[string]time.Time{}
	}
	// Idempotent per action: keep the first time seen. A second Enter for one action is the same
	// failure being reported twice, not a second failure.
	if _, dup := s.pending[targetKey][actionID]; !dup {
		s.pending[targetKey][actionID] = now
	}
	s.mu.Unlock()

	refreshErr := s.refreshIfStale(ctx, now)

	s.mu.Lock()
	defer s.mu.Unlock()
	s.prune(now)
	return s.seriesLocked(targetKey, now).Until, refreshErr
}

// Active reports whether a target is in cooldown, and until when.
//
// This is step 5's read (05 §1). It refuses -- returns an error -- rather than answering false when
// it cannot see the journal and has nothing in the overlay to go on. See MaxJournalStaleness for
// why that direction is not negotiable. When the overlay DOES cover the target, the answer is
// returned with no error even though the journal is unreadable: an active cooldown this process
// entered itself is a fact it knows, and refusing to state it would be strictly worse for the
// caller than stating it.
func (s *Source) Active(ctx context.Context, targetKey string, now time.Time) (bool, time.Time, error) {
	refreshErr := s.refreshIfStale(ctx, now)

	s.mu.Lock()
	defer s.mu.Unlock()
	s.prune(now)

	series := s.seriesLocked(targetKey, now)
	if series.Active(now) {
		return true, series.Until, nil
	}
	if refreshErr != nil && s.staleLocked(now) {
		seen := "has never been read successfully"
		if !s.readAt.IsZero() {
			seen = fmt.Sprintf("was last read %s ago, past the %s limit",
				now.Sub(s.readAt).Truncate(time.Second), MaxJournalStaleness)
		}
		return false, time.Time{}, fmt.Errorf(
			"cooldown: the journal %s, so this broker cannot show that %s is out of its 04 §4.2 quiet period: %w",
			seen, targetKey, refreshErr)
	}
	return false, time.Time{}, nil
}

// refreshIfStale reads when the snapshot is older than the TTL. Returns the read error, if any;
// callers decide what a failed read means for the question they are answering.
func (s *Source) refreshIfStale(ctx context.Context, now time.Time) error {
	s.mu.Lock()
	fresh := !s.readAt.IsZero() && now.Sub(s.readAt) < s.ttl
	s.mu.Unlock()
	if fresh {
		return nil
	}
	return s.refresh(ctx, now)
}

// seriesLocked folds the union of journal and overlay events for one target.
func (s *Source) seriesLocked(targetKey string, now time.Time) verify.CooldownSeries {
	merged := make(map[string]time.Time, len(s.events[targetKey])+len(s.pending[targetKey]))
	for id, at := range s.events[targetKey] {
		merged[id] = at
	}
	for id, at := range s.pending[targetKey] {
		// The journal wins: it is the durable record, and once it holds the event the overlay's
		// copy is a duplicate of it rather than an addition to it.
		if _, ok := merged[id]; !ok {
			merged[id] = at
		}
	}
	if len(merged) == 0 {
		return verify.CooldownSeries{}
	}

	ats := make([]time.Time, 0, len(merged))
	for _, at := range merged {
		ats = append(ats, at)
	}
	// Sorted, because verify.CooldownSeries.Add decays on the gap to the PREVIOUS event and a map
	// iterates in a different order every time. An unsorted fold would answer differently on two
	// consecutive calls over an unchanged journal, which is the shape of bug that survives a test
	// suite by passing most of the time.
	sort.Slice(ats, func(i, j int) bool { return ats[i].Before(ats[j]) })

	var series verify.CooldownSeries
	for _, at := range ats {
		series.Add(at)
	}
	return series
}

// prune drops events that can no longer affect any answer.
//
// An event at T is spent once BOTH of its effects are: it can extend `until` to at most
// T+MaxCooldown, and it can raise `consecutive` for a later event at most CooldownDecay after it.
// CooldownDecay (24h) is the longer of the two, so anything older than now-CooldownDecay is inert.
// Only the overlay is pruned -- the journal snapshot is rebuilt wholesale on every Refresh.
func (s *Source) prune(now time.Time) {
	cutoff := now.Add(-verify.CooldownDecay)
	for key, ids := range s.pending {
		for id, at := range ids {
			if at.Before(cutoff) {
				delete(ids, id)
			}
		}
		if len(ids) == 0 {
			delete(s.pending, key)
		}
	}
}

func (s *Source) staleLocked(now time.Time) bool {
	return s.readAt.IsZero() || now.Sub(s.readAt) > MaxJournalStaleness
}

// derive turns a journal listing into failure events per target key.
//
// WHICH RECORDS COUNT. 04 §4.2 says "after a failed or rolled-back remediation", which is
// `PhaseFailed` and `PhaseRolledBack` -- the two terminal phases in which a write was attempted and
// did not stand. Nothing else qualifies and the omissions are deliberate: `PhaseRejected` and
// `PhaseExpired` never touched the cluster, `PhaseUndone` is a HUMAN reversing a change and belongs
// to the contested index rather than to the anti-thrash backoff, and `PhaseDryRun` is the whole of
// Phase 9 -- an action that by construction never executed and so never failed at anything.
func derive(items []agentv1alpha1.ActionRecord) map[string]map[string]time.Time {
	out := map[string]map[string]time.Time{}
	for i := range items {
		rec := &items[i]
		switch rec.Status.Phase {
		case agentv1alpha1.PhaseFailed, agentv1alpha1.PhaseRolledBack:
		default:
			continue
		}
		id := rec.Spec.ActionID
		if id == "" {
			// Impossible through admission (spec.actionId is required and ULID-shaped) and cheap to
			// tolerate: an ID-less record cannot be deduplicated against the overlay, so counting it
			// would be the double-count the union exists to prevent.
			continue
		}
		at := failureTime(rec)
		for _, t := range rec.Spec.Targets {
			key := verify.TargetKey(t)
			if out[key] == nil {
				out[key] = map[string]time.Time{}
			}
			// One record naming a target twice is one failure of that target, not two.
			if _, ok := out[key][id]; !ok {
				out[key][id] = at
			}
		}
	}
	return out
}

// failureTime is when the remediation is treated as having failed.
//
// There is no `rolledBackAt` field in 06 §4.3, so this takes the LATEST of the timestamps that do
// exist. The recovery ladder's last transition is the precise one -- rung 3 is the rollback and rung
// 5 the page, both stamped by verify.Ladder at the moment they happen -- and the lifecycle clock
// and `metadata.creationTimestamp` are the fallbacks beneath it.
//
// Latest, not first, and the direction is deliberate. Every fallback is EARLIER than the transition
// it stands in for, and an earlier failure time is an earlier expiry, which is the loosening
// direction; taking the max keeps a malformed record as close to the truth as its own contents
// allow. The spread is bounded by the lifetime of one action -- seconds to minutes -- against a
// five-minute BaseCooldown, so a record missing its ladder is slightly lenient rather than wrong.
func failureTime(rec *agentv1alpha1.ActionRecord) time.Time {
	at := rec.CreationTimestamp.Time
	bump := func(t *metav1.Time) {
		if t != nil && t.Time.After(at) {
			at = t.Time
		}
	}
	if ts := rec.Status.Timestamps; ts != nil {
		bump(ts.Submitted)
		bump(ts.ExecutionStarted)
		bump(ts.ExecutionEnded)
		bump(ts.Verified)
	}
	if r := rec.Status.Recovery; r != nil {
		for i := range r.Transitions {
			if tr := r.Transitions[i].At; tr.Time.After(at) {
				at = tr.Time
			}
		}
	}
	return at
}
