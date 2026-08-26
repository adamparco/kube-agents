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

// Package budget is the production broker.Accountant: 06 §4.4 row 7's initiative budget and flap
// threshold, counted from the ActionRecord journal.
//
// # Why the journal, and not a counter
//
// The same argument the cooldown package makes, and it lands harder here. `Agent.status.budget`
// exists in 06 §1.1 and looks like the obvious home for a spend tally -- and the broker cannot write
// it: 06 §2.2.1 grants `get, list, watch` on `agents` and no write verb, and V-BRK-013 asserts that
// grant EXACTLY and is BLOCKING-ALWAYS. An in-process counter is worse than unavailable, it is
// wrong: a budget that resets when the pod restarts is a budget an agent clears by crashing, and
// "the broker restarted" is not a reason an agent should get its hourly allowance back. The journal
// is already the durable record of every action this agent took, the broker already writes it, and
// an agent cannot delete from it. The tally is a fold over it.
//
// So `status.budget` is a MIRROR for humans, written by a controller that does not exist yet, and
// this package is the authority. They will not disagree, because there is only one place the number
// comes from.
//
// # A cold accountant refuses, and that is the point of the type
//
// [broker.Accountant] takes no context and returns no error -- deliberately, so that row 7 cannot
// make the brake's availability depend on the availability it exists to survive. That leaves exactly
// one channel for "I have not read the journal yet": the [broker.BrakeBudget] it returns. And
// BrakeBudget's zero value PERMITS, alone among every brake input, because a zero spend tally means
// an agent that has done nothing yet.
//
// Those two facts together mean a source that answered honestly-but-emptily while cold would
// silently switch row 7 off for the first seconds of every broker's life, and permanently for a
// broker whose journal reads all fail. So a cold or stale source returns `Exhausted: true` with a
// Detail that says why. It is the one place in this package where the refusal is not about the
// agent's spend at all, and it is the clause V-PRO-029 exists to pin.
//
// # Two decisions the spec did not settle, recorded where the code makes them
//
// The rolling window and its "next boundary" are reconciled at [Window]; the flap key is at
// [flapKey]. Both are argued there rather than here so that a reader who disagrees is looking at the
// code the argument governs.
package budget

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/verify"
)

const (
	// HourWindow is 06 §1.1's "per rolling hour".
	HourWindow = time.Hour

	// DayWindow is 06 §1.1's "per rolling 24 h".
	DayWindow = 24 * time.Hour

	// MaxJournalStaleness is how old the last successful journal read may be before the accountant
	// stops counting and starts refusing. The same 30 s as broker.MaxFreezeStaleness,
	// policy.MaxPolicyStaleness and cooldown.MaxJournalStaleness, because it is the same failure: a
	// cached answer that is no longer evidence about now.
	MaxJournalStaleness = 30 * time.Second

	// DefaultRefreshInterval is how often Run re-reads. Three intervals inside MaxJournalStaleness,
	// exactly as policy.DefaultRefreshInterval: one lost poll must not start refusing actions, two
	// consecutive lost polls must.
	DefaultRefreshInterval = 10 * time.Second
)

// Journal is the one API-server verb this source needs. Narrow on purpose, the same way
// cooldown.Journal and policy.Lister are: 06 §2.2.1 gives the broker `get, list, watch, create` on
// `actionrecords`, and a dependency typed as the full client.Client would make it possible to write
// an update here and discover the missing verb at L2.
type Journal interface {
	List(ctx context.Context, list client.ObjectList, opts ...client.ListOption) error
}

// SourceConfig assembles a Source.
type SourceConfig struct {
	// Journal lists ActionRecord objects.
	Journal Journal

	// Namespace is the agent's own namespace. ActionRecords are namespaced and live beside the agent
	// that wrote them (06 §4.3), and the broker's Role is namespaced too -- a cluster-wide List would
	// be Forbidden, not merely broad.
	Namespace string

	// AgentName is this broker's own agent, from its own deployment, never from an envelope.
	//
	// It is needed because the journal has NO agent-name label. 06 §4.3 stamps tier and a scope
	// LEAF, and the leaf is not injective across a fleet -- two clusters each holding a `team-x`
	// namespace produce the same value -- so `internal/journal` documents outright that it is safe as
	// an index and must never be treated as an identity. The filter is therefore client-side on
	// `spec.agentRef.name`, which is exact. One agent's records live in one namespace, so the list is
	// small.
	AgentName string

	// RefreshInterval defaults to DefaultRefreshInterval. Values at or above MaxJournalStaleness are
	// rejected by NewSource, following policy.NewSource: a source that cannot refresh faster than it
	// goes stale refuses more often than it answers.
	RefreshInterval time.Duration

	// Now is injectable so a test can age a snapshot without sleeping.
	Now func() time.Time
}

// Source is the journal-derived broker.Accountant.
//
// The lifecycle is policy.Source's, not cooldown.Source's, and the reason is mechanical rather than
// stylistic: cooldown refreshes lazily from inside a ctx-taking method, and `Budget(q) BrakeBudget`
// has no ctx to refresh with and no error to report a failed refresh through. So reads happen out of
// band -- a synchronous [Source.Refresh] at startup and a backgrounded [Source.Run] afterwards -- and
// [Source.Budget] serves whatever the last successful read produced, or refuses if that is too old.
//
// The DERIVATION is cooldown's: one fold over a listing, keyed client-side, with no time index
// because the journal has none.
type Source struct {
	journal   Journal
	namespace string
	agentName string
	interval  time.Duration
	now       func() time.Time

	mu      sync.RWMutex
	snap    *snapshot
	lastErr error
}

var _ broker.Accountant = (*Source)(nil)

// snapshot is one read of the journal, folded into the two things row 7 asks about.
type snapshot struct {
	// observedAt is when the READ happened, not when it is served. Same rule as
	// broker.FreezeView.ObservedAt, and for the same reason: a snapshot that stamps itself at serve
	// time can never be stale.
	observedAt time.Time
	// charges are the actions that drew down a bucket, one entry per record.
	charges []charge
	// applications are the actions that were APPLIED to a target, one entry per (record, target).
	applications []application
}

// charge is one action's draw on one {origin, class} bucket.
type charge struct {
	at     time.Time
	origin agentv1alpha1.BudgetOrigin
	class  agentv1alpha1.ActionRiskClass
}

// application is one action reaching one target. Separate from charge because the two count
// different record sets -- see chargeable and applied.
type application struct {
	at  time.Time
	key string
}

// NewSource validates the wiring. It does not read; call Refresh, which startup should do
// synchronously for the reason policy.Source.Refresh gives.
func NewSource(cfg SourceConfig) (*Source, error) {
	if cfg.Journal == nil {
		return nil, errors.New("budget: a Journal is required; an accountant that cannot list ActionRecords would report every bucket as empty")
	}
	if cfg.Namespace == "" {
		return nil, errors.New("budget: a Namespace is required; ActionRecords are namespaced and an unscoped List is Forbidden to the broker's Role (06 §2.2.1)")
	}
	if cfg.AgentName == "" {
		return nil, errors.New("budget: an AgentName is required; the journal carries no agent-name label, so without it the fold would charge every agent in the namespace to this one")
	}
	if cfg.RefreshInterval < 0 {
		return nil, fmt.Errorf("budget: RefreshInterval %s is negative", cfg.RefreshInterval)
	}
	if cfg.RefreshInterval == 0 {
		cfg.RefreshInterval = DefaultRefreshInterval
	}
	if cfg.RefreshInterval >= MaxJournalStaleness {
		return nil, fmt.Errorf(
			"budget: RefreshInterval %s is not shorter than MaxJournalStaleness %s; the snapshot would go stale between successful reads and row 7 would refuse actions it has current counts for",
			cfg.RefreshInterval, MaxJournalStaleness)
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	return &Source{
		journal:   cfg.Journal,
		namespace: cfg.Namespace,
		agentName: cfg.AgentName,
		interval:  cfg.RefreshInterval,
		now:       cfg.Now,
	}, nil
}

// Refresh performs one read and rebuilds the snapshot.
//
// A read failure RETAINS the previous snapshot and lets it age, exactly as policy.Source and
// cooldown.Source do: discarding on the first dropped request turns a control-plane blip into a
// broker that refuses everything. There is no second failure class -- unlike a ChangePolicy, an
// ActionRecord that will not parse cannot exist, because the list is typed.
//
// Exported because startup should be synchronous. A broker whose very first journal read fails
// should fail to start, loudly, rather than come up healthy and refuse every action with a message
// a reader of the logs has to reconstruct.
func (s *Source) Refresh(ctx context.Context) error {
	at := s.now()

	var list agentv1alpha1.ActionRecordList
	if err := s.journal.List(ctx, &list, client.InNamespace(s.namespace)); err != nil {
		s.mu.Lock()
		s.lastErr = err
		s.mu.Unlock()
		return fmt.Errorf("budget: listing ActionRecords in %s: %w", s.namespace, err)
	}

	snap := s.derive(list.Items, at)

	s.mu.Lock()
	s.snap = snap
	s.lastErr = nil
	s.mu.Unlock()
	return nil
}

// Run refreshes on a ticker until ctx is cancelled. Errors are retained for Budget to report and do
// not stop the loop: the next poll may well succeed, and stopping would convert a transient failure
// into a permanent one at the moment MaxJournalStaleness elapses.
func (s *Source) Run(ctx context.Context) {
	t := time.NewTicker(s.interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			_ = s.Refresh(ctx)
		}
	}
}

// Budget answers 06 §4.4 row 7 for one action. It implements broker.Accountant.
//
// No context, no error, no client -- see the interface's own doc comment for why that is a
// constraint rather than an oversight. Everything this method needs was read out of band by Refresh.
func (s *Source) Budget(q broker.BudgetQuery) broker.BrakeBudget {
	s.mu.RLock()
	snap, lastErr := s.snap, s.lastErr
	s.mu.RUnlock()

	now := q.Now
	if now.IsZero() {
		now = s.now()
	}

	// THE COLD AND STALE ARMS. Both return Exhausted, and the package comment argues why: this is
	// the only channel the interface leaves for "I could not count", and the permissive answer is
	// the zero value.
	if snap == nil {
		if lastErr != nil {
			return broker.BrakeBudget{Exhausted: true, Detail: fmt.Sprintf(
				"the broker has never read its own journal, so it cannot show this action is within budget (04 §4.2): %v", lastErr)}
		}
		return broker.BrakeBudget{Exhausted: true, Detail: "the broker has not yet read its own journal, so it cannot show this action is within budget (04 §4.2)"}
	}
	if age := now.Sub(snap.observedAt); age > MaxJournalStaleness {
		detail := fmt.Sprintf(
			"the broker last counted its own spend %s ago, past the %s staleness limit, so it cannot show this action is within budget (04 §4.2)",
			age.Truncate(time.Second), MaxJournalStaleness)
		if lastErr != nil {
			detail = fmt.Sprintf("%s; every read since has failed: %v", detail, lastErr)
		}
		return broker.BrakeBudget{Exhausted: true, Detail: detail}
	}

	limits := q.Agent.EffectiveInitiativeBudget()

	// FLAP FIRST, because it is the finding a human most needs and its Detail is the more specific
	// of the two. Row 7 reports one rule, and brake.budgetRefusal prefers the flap rule when both
	// fire, so computing flap first keeps the Detail and the rule agreeing.
	if key, seen, breached := snap.flap(q.Targets, limits, now); breached {
		return broker.BrakeBudget{
			FlapBreached: true,
			Detail: fmt.Sprintf(
				"%s has been acted on %d times in the last %s and the flap threshold is %d (04 §4.2); the repetition is evidence the diagnosis is wrong",
				key, seen, limits.FlapWindow, limits.FlapThreshold),
		}
	}

	// UNDO IS NEVER REFUSED FOR BUDGET REASONS (06 §1.1), and the carve-out is here rather than at
	// the call site because this is the thing that knows what an undo draws down. It is deliberately
	// BELOW the flap check: 05 §1.5 lists flap and budget as separate controls, and an undo that is
	// itself part of an oscillation is precisely the case a human needs to see. The day counter still
	// counts the undo for observability -- it is folded into the snapshot like any other charge --
	// it just never refuses one.
	if q.Trigger == agentv1alpha1.ActionTriggerUndo {
		return broker.BrakeBudget{}
	}

	origin := agentv1alpha1.BudgetOriginFor(q.Trigger)
	class := limits.For(origin)

	if perHour, bucketed := class.PerHour(q.Class); bucketed {
		if used := snap.count(origin, q.Class, now.Add(-HourWindow)); int32(used)+1 > perHour {
			return broker.BrakeBudget{Exhausted: true, Detail: fmt.Sprintf(
				"%d/%d %s %s this hour (04 §4.2); the bucket refills at %s",
				used, perHour, q.Class, origin, snap.retryAt(origin, q.Class, HourWindow, now).UTC().Format(time.RFC3339)),
			}
		}
	}

	if used := snap.count(origin, "", now.Add(-DayWindow)); int32(used)+1 > class.ActionsPerDay {
		return broker.BrakeBudget{Exhausted: true, Detail: fmt.Sprintf(
			"%d/%d %s actions today, all classes together (04 §4.2); the bucket refills at %s",
			used, class.ActionsPerDay, origin, snap.retryAt(origin, "", DayWindow, now).UTC().Format(time.RFC3339)),
		}
	}

	return broker.BrakeBudget{}
}

// Window is the rolling-window reading, recorded here because 06 has two sentences that look like
// they disagree and the resolution changes what a refused agent is told.
//
// 04 §4.2 says "per rolling window". The 06 §1.1 table says "per rolling hour" and "per rolling
// 24 h". And 06 §1.1's exhaustion rule asks for a `retryAfterSeconds` "to the next WINDOW BOUNDARY",
// which is the phrase that reads as a fixed, clock-aligned window -- reinforced by the
// `status.budget` example, whose `windowStart` is `17:00:00Z` on the hour.
//
// A sliding window has a next boundary, and it is not the top of the hour: it is the instant the
// OLDEST charge in the window ages out, which is exactly the moment the bucket regains capacity.
// That reading satisfies both sentences, so this is not the PROTOCOL §8.5 contradiction it looks
// like. The normative text says "rolling" three times in two documents; the clock-aligned reading
// appears once, in a YAML comment on a status field that no writer exists for. Rolling wins, and
// [snapshot.retryAt] computes the boundary.
//
// The direction matters too. A tumbling hour lets an agent spend its whole allowance at 16:59 and
// the whole of the next at 17:01 -- double the intended rate across a two-minute span, which is the
// burst an initiative budget exists to prevent. Rolling has no such seam.
const Window = "rolling"

// count returns how many charges fall inside the window opening at `from`.
//
// An empty class means "every class", which is the `actionsPerDay` bucket: 06 §1.1 says that counter
// is "all classes together".
func (s *snapshot) count(origin agentv1alpha1.BudgetOrigin, class agentv1alpha1.ActionRiskClass, from time.Time) int {
	n := 0
	for _, c := range s.charges {
		if c.origin != origin || c.at.Before(from) {
			continue
		}
		if class != "" && c.class != class {
			continue
		}
		n++
	}
	return n
}

// retryAt is the next window boundary for a full bucket: when its OLDEST charge ages out.
//
// See [Window]. If the bucket is somehow empty the boundary is now -- there is nothing to wait for,
// and a refusal computed from an empty bucket is a bug elsewhere, not a reason to invent a delay.
func (s *snapshot) retryAt(origin agentv1alpha1.BudgetOrigin, class agentv1alpha1.ActionRiskClass, window time.Duration, now time.Time) time.Time {
	oldest := time.Time{}
	from := now.Add(-window)
	for _, c := range s.charges {
		if c.origin != origin || c.at.Before(from) {
			continue
		}
		if class != "" && c.class != class {
			continue
		}
		if oldest.IsZero() || c.at.Before(oldest) {
			oldest = c.at
		}
	}
	if oldest.IsZero() {
		return now
	}
	return oldest.Add(window)
}

// flap reports the first target over the threshold, how many applications it has seen, and whether
// the threshold is breached.
//
// "More than N times in a window" (04 §4.2) counts THIS action as one of the applications, so the
// breach test is `seen+1 > threshold` -- with the default of 3, three prior applications are allowed
// and the fourth is refused.
func (s *snapshot) flap(targets []agentv1alpha1.TargetRef, limits agentv1alpha1.ResolvedInitiativeBudget, now time.Time) (string, int, bool) {
	from := now.Add(-limits.FlapWindow)
	for _, t := range targets {
		key := flapKey(t)
		seen := 0
		for _, a := range s.applications {
			if a.key == key && !a.at.Before(from) {
				seen++
			}
		}
		if int32(seen)+1 > limits.FlapThreshold {
			return key, seen, true
		}
	}
	return "", 0, false
}

// flapKey is the identity flap repeats are counted against, and it is the TARGET ALONE.
//
// 04 §4.2 words the control as "the same `(target, intent)` applied more than N times". That key is
// not implementable as written, and the reason is not an oversight in this package. `spec.intent` is
// free-text model prose: `internal/broker/idempotency.go` excludes it from the idempotency key
// DELIBERATELY, because "a retry that reworded itself -- which is what an LLM does on retry --
// computed a different key and executed twice". A flap detector keyed on intent has the same defect
// in the same direction: an agent that rewords its way around the threshold never trips it, and the
// agents this brake is for are exactly the ones that reword. No canonical intent identity exists
// anywhere in the data model -- the ActionRecord records `targets`, not the operations -- so there is
// nothing else to key on.
//
// Dropping intent from the key makes the control STRICTLY STRICTER: every `(target, intent)` breach
// is also a target breach, so nothing the spec would catch is missed. The residual runs the other
// way and is named rather than implied: three legitimately different actions on one object inside
// the window now trip a brake the literal spec would not have tripped. That is tolerable because the
// remedy 04 §4.2 prescribes is "stop acting on that target, mark it, escalate" -- a human looks --
// and because 04 §4.2 itself says three changes to one object in half an hour is "evidence the
// diagnosis is wrong". The threshold and window are both operator-tunable if a scope proves noisy.
//
// UID is excluded for the same reason verify.TargetKey excludes it: a target deleted and recreated
// during a flap is the same target to an operator, and keying on UID would hand a fresh count to
// every recreate -- which is the create/delete oscillation, the case flap most needs to catch.
func flapKey(t agentv1alpha1.TargetRef) string { return verify.TargetKey(t) }

// derive folds a journal listing into the snapshot.
//
// The client-side filtering is not a shortcut. There is no agent-name label (see
// SourceConfig.AgentName) and no time index on ActionRecord, so both the identity filter and the
// window filter have to happen here -- exactly as cooldown.derive already does.
func (s *Source) derive(items []agentv1alpha1.ActionRecord, at time.Time) *snapshot {
	snap := &snapshot{observedAt: at}
	// Nothing older than the longest window can affect any answer this snapshot will be asked for,
	// and the snapshot is rebuilt whole on every Refresh, so the cutoff is applied once here rather
	// than carried through every count.
	cutoff := at.Add(-DayWindow)

	for i := range items {
		rec := &items[i]
		if rec.Spec.AgentRef.Name != s.agentName {
			continue
		}
		when := chargeTime(rec)
		if when.Before(cutoff) {
			continue
		}
		if chargeable(rec) {
			snap.charges = append(snap.charges, charge{
				at:     when,
				origin: agentv1alpha1.BudgetOriginFor(rec.Spec.Trigger.Source),
				class:  rec.Spec.Classification.Class,
			})
		}
		if applied(rec) {
			for _, t := range rec.Spec.Targets {
				snap.applications = append(snap.applications, application{at: when, key: flapKey(t)})
			}
		}
	}
	return snap
}

// chargeable reports whether a record drew down a bucket, per 06 §1.1's accounting rules.
//
// The rules, and what each excludes:
//
//   - "Rejected, forbidden, and deduplicated envelopes decrement nothing (they never executed)".
//     PhaseRejected covers the first; a `forbidden` classification covers the second and is checked
//     separately because a forbidden action can be journaled without reaching PhaseRejected; a
//     deduplicated envelope produces no second record at all, so there is nothing here to exclude.
//   - "DryRun decrements nothing". PhaseDryRun is the whole of Phase 9, which means this fold counts
//     nothing today and starts counting the moment execution is wired. That is correct rather than
//     unfortunate: a rehearsal is not a spend, and a budget that charged for dry runs would refuse
//     real work on the strength of work that never happened.
//   - "a RolledBack action DOES decrement, because it ran". So does Failed, Undone, and anything
//     mid-flight -- Pending, PendingApproval, Executing -- because a submission has been made.
//   - PhaseExpired is charged, and it is the one phase 06 §1.1 does not name. It is reached by a
//     gated action nobody approved in time, and 06 §1.1 says `gatedPerHour` "counts SUBMISSIONS, not
//     approvals". The submission happened. Not charging it would let an agent submit unlimited gated
//     actions as long as it never got any of them approved.
func chargeable(rec *agentv1alpha1.ActionRecord) bool {
	if rec.Spec.DryRun || rec.Spec.Classification.Class == agentv1alpha1.RiskForbidden {
		return false
	}
	switch rec.Status.Phase {
	case agentv1alpha1.PhaseRejected, agentv1alpha1.PhaseDryRun:
		return false
	default:
		return true
	}
}

// applied reports whether a record actually reached its targets, which is the set flap counts.
//
// 04 §4.2 says "APPLIED more than N times", and the difference from chargeable is real: a gated
// submission sitting in PendingApproval has drawn down a bucket and has touched nothing, so it
// cannot be evidence that an agent and something else are fighting over an object. The phases here
// are the ones in which a write was attempted.
func applied(rec *agentv1alpha1.ActionRecord) bool {
	if rec.Spec.DryRun {
		return false
	}
	switch rec.Status.Phase {
	case agentv1alpha1.PhaseExecuting, agentv1alpha1.PhaseVerified, agentv1alpha1.PhaseFailed,
		agentv1alpha1.PhaseRolledBack, agentv1alpha1.PhaseUndone:
		return true
	default:
		return false
	}
}

// chargeTime is when an action is treated as having been submitted.
//
// `status.timestamps.submitted` is the field that means exactly this, and `metadata.creationTimestamp`
// is the fallback beneath it -- the record is created at submission, so the two are the same instant
// to within the write. Unlike cooldown.failureTime this does NOT take the latest of every timestamp:
// a budget charge is stamped when the action was submitted, and using a later timestamp (execution,
// verification) would hold a long-running action in the window past the hour it was actually spent
// in.
func chargeTime(rec *agentv1alpha1.ActionRecord) time.Time {
	if ts := rec.Status.Timestamps; ts != nil && ts.Submitted != nil && !ts.Submitted.Time.IsZero() {
		return ts.Submitted.Time
	}
	return rec.CreationTimestamp.Time
}
