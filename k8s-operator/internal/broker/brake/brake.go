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

// Package brake is the production pipeline.BrakeSource: the four reads 06 §4.4 needs, gathered in
// one place so that broker.Decide stays a pure function of values.
//
// The decision itself lives in `internal/broker/brake.go` (package broker, func Decide). This
// package does not decide anything. It looks things up and says honestly what it could not see.
//
// # Why this is not in package broker
//
// The same seam as internal/broker/livestate, refindex, policy and cooldown, and for the sharpest
// version of the reason. broker.BrakeInputs' own doc comment says: "Nothing in this struct is a
// client, a context or a callback. That is what makes the brake testable without a cluster and,
// more importantly, what makes it evaluable when the cluster is the thing that is broken." A
// client field on BrakeInputs would put an API call inside the function whose entire job is to
// still work when API calls are failing.
//
// # Observe returns no error, and that is load-bearing
//
// pipeline.BrakeView says why: "a source that returned (view, err) would invite a caller to treat
// the error as fatal and skip the brake entirely, which is the one outcome 06 §4.4 exists to
// prevent." So every failure here is encoded IN the view, in the shape the row that cares about it
// reads -- a nil Agent is row 2, a nil Freezes is row 1, a nil Roster is row 6, a BrakeFailed
// Journal is row 3. There is no path out of this package that loses a failure.
//
// # The cache degrades into row 1 by itself
//
// The snapshot is stamped with the instant the read actually happened, not the instant it was
// served, and FreezeView.ObservedAt carries that stamp all the way into broker.Decide, which
// applies its own MaxFreezeStaleness test. So a refresh that starts failing does not need anything
// here to notice: ObservedAt stops advancing, and thirty seconds later row 1 fires on its own.
// That is the property the constructor's CacheTTL bound protects -- with a TTL shorter than
// MaxFreezeStaleness, a view served from cache is fresh by construction, and the only way to get a
// stale one is for the reads to be genuinely failing.
//
// This is the opposite arrangement from an informer, and deliberately so. broker.MaxFreezeStaleness
// argues the case in its own comment: "a watch that silently stopped delivering is not an error at
// all -- the informer's List succeeds, the cache answers instantly, and every answer is from before
// the incident started." Direct reads on a short TTL cannot fail that way. A read either happened
// at the stamped instant or it did not happen.
package brake

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/pipeline"
)

// DefaultCacheTTL is how long one assembled view is served before the reads are redone.
//
// Five seconds, the same number cooldown.DefaultCacheTTL uses and for the same reason: it is short
// enough to sit well inside broker.MaxFreezeStaleness with room for a slow read, and long enough
// that the two Observe calls one submission makes -- pipeline.callerScope and pipeline.stepBrake --
// share a single set of reads and therefore a single consistent answer.
const DefaultCacheTTL = 5 * time.Second

// Reader is the read surface for the three CRs. Read-only on purpose: 06 §2.2.1 gives the broker no
// write verb on Agent, FleetFreeze or ApprovalRoster, and a dependency typed as the full
// client.Client would make it possible to write one here and find out at L2.
type Reader interface {
	Get(ctx context.Context, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error
	List(ctx context.Context, list client.ObjectList, opts ...client.ListOption) error
}

// Journal is the reachability probe for row 3, kept separate from Reader so that "the journal is
// down" is injectable without also breaking the Agent read. Those are different incidents with
// different rows, and a test that cannot produce one without the other cannot tell them apart.
type Journal interface {
	List(ctx context.Context, list client.ObjectList, opts ...client.ListOption) error
}

// SourceConfig assembles a Source. Every field is required except CacheTTL and Now.
type SourceConfig struct {
	// Reader reads the Agent, the FleetFreeze list and the ApprovalRoster.
	Reader Reader
	// Journal is probed for reachability (row 3).
	Journal Journal

	// AgentName and Namespace identify the Agent CR this broker serves. From the broker's own
	// deployment, never from an envelope -- the same rule pipeline.Config states for its copies.
	AgentName string
	Namespace string

	// CacheTTL is how long one assembled view is reused. Defaults to DefaultCacheTTL. Must be
	// shorter than broker.MaxFreezeStaleness; see NewSource.
	CacheTTL time.Duration

	// Now is injectable so a test can age a view without sleeping.
	Now func() time.Time
}

// Source is the production pipeline.BrakeSource.
//
// It holds the last SUCCESSFUL value of each read alongside the instant that read succeeded, rather
// than one snapshot with one timestamp. That shape is what lets a failing FleetFreeze list age into
// row 1 while a healthy Agent read keeps answering row 2 -- which is the honest description of a
// broker that can see some things and not others, and is the situation 06 §4.4's per-row structure
// is written for.
type Source struct {
	reader    Reader
	journal   Journal
	agentName string
	namespace string
	ttl       time.Duration
	now       func() time.Time

	mu sync.Mutex
	// Each value is paired with when its read last succeeded. A zero time means never.
	agent     *agentv1alpha1.Agent
	agentAt   time.Time
	freezes   []agentv1alpha1.FleetFreeze
	freezesAt time.Time
	roster    *agentv1alpha1.ApprovalRoster
	rosterAt  time.Time
	// journalSignal is the outcome of the MOST RECENT probe, not a retained value. See probe.
	journalSignal broker.BrakeSignal
	refreshedAt   time.Time
}

var _ pipeline.BrakeSource = (*Source)(nil)

// NewSource validates the wiring. It does not read; call Refresh, which startup should do
// synchronously so that a broker whose RBAC is wrong fails at boot rather than on the first
// envelope.
func NewSource(cfg SourceConfig) (*Source, error) {
	switch {
	case cfg.Reader == nil:
		return nil, errors.New("brake: a Reader is required; a source that cannot read the Agent CR would report every agent as unreadable and refuse every action (06 §4.4 row 2)")
	case cfg.Journal == nil:
		return nil, errors.New("brake: a Journal is required; row 3 is a probe, and a source that never probes would report the journal unobserved, which refuses")
	case cfg.AgentName == "":
		return nil, errors.New("brake: an AgentName is required; the brake is about one agent and an empty name would Get the wrong object or none")
	case cfg.Namespace == "":
		return nil, errors.New("brake: a Namespace is required; Agent and ApprovalRoster are namespaced")
	}
	if cfg.CacheTTL < 0 {
		return nil, fmt.Errorf("brake: CacheTTL %s is negative", cfg.CacheTTL)
	}
	if cfg.CacheTTL == 0 {
		cfg.CacheTTL = DefaultCacheTTL
	}
	if cfg.CacheTTL >= broker.MaxFreezeStaleness {
		return nil, fmt.Errorf(
			"brake: CacheTTL %s is not shorter than broker.MaxFreezeStaleness %s; a view served from cache could already be too old for 06 §4.4 row 1, which would make the cache itself the thing that freezes the fleet",
			cfg.CacheTTL, broker.MaxFreezeStaleness)
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	return &Source{
		reader:    cfg.Reader,
		journal:   cfg.Journal,
		agentName: cfg.AgentName,
		namespace: cfg.Namespace,
		ttl:       cfg.CacheTTL,
		now:       cfg.Now,
	}, nil
}

// Observe gathers the view, refreshing first if the cached one has expired.
//
// It returns no error by the interface's design; see the package comment. A refresh that fails
// leaves the previously-read values in place to age, and the aging is what produces the refusal.
func (s *Source) Observe(ctx context.Context) pipeline.BrakeView {
	now := s.now()

	s.mu.Lock()
	due := s.refreshedAt.IsZero() || now.Sub(s.refreshedAt) >= s.ttl
	s.mu.Unlock()

	if due {
		// The error is deliberately dropped: every failure it could report has already been
		// recorded into the fields this method is about to read, in the shape 06 §4.4 wants. Refresh
		// returns it for the benefit of startup, which is the one caller that should be loud.
		_ = s.refresh(ctx, now)
	}
	return s.assemble(ctx, now)
}

// Refresh performs one round of reads. Exported for synchronous startup, where a returned error
// should stop the process: an RBAC gap that makes the Agent CR unreadable turns into "refuse
// everything" at runtime, which is safe but is a much worse way to find out.
func (s *Source) Refresh(ctx context.Context) error { return s.refresh(ctx, s.now()) }

// refresh takes the instant to stamp successful reads at, so that Observe ages the result against
// the same clock it used to decide the refresh was due.
//
// Every read is attempted even after an earlier one fails. That is not politeness: the rows are
// independent, and short-circuiting would report "the freeze list is unreadable" as a consequence of
// an unrelated Agent read failing, which sends an operator to the wrong incident. The returned error
// joins whatever went wrong, for the startup caller.
func (s *Source) refresh(ctx context.Context, at time.Time) error {
	var errs []error

	var agent agentv1alpha1.Agent
	agentErr := s.reader.Get(ctx, types.NamespacedName{Namespace: s.namespace, Name: s.agentName}, &agent)
	if agentErr != nil {
		errs = append(errs, fmt.Errorf("brake: reading Agent %s/%s (06 §4.4 row 2): %w", s.namespace, s.agentName, agentErr))
	}

	var freezeList agentv1alpha1.FleetFreezeList
	freezeErr := s.reader.List(ctx, &freezeList)
	if freezeErr != nil {
		errs = append(errs, fmt.Errorf("brake: listing FleetFreeze (06 §4.4 row 1): %w", freezeErr))
	}

	// The roster is reachable only through the Agent, so a failed Agent read leaves the roster
	// alone rather than clearing it -- there is no evidence either way, and clearing it would park
	// gated actions for a reason that is not row 6.
	var (
		roster     *agentv1alpha1.ApprovalRoster
		rosterRead bool
		rosterErr  error
	)
	if agentErr == nil {
		roster, rosterRead, rosterErr = s.readRoster(ctx, &agent)
		if rosterErr != nil {
			errs = append(errs, rosterErr)
		}
	}

	journalErr := s.probe(ctx)
	if journalErr != nil {
		errs = append(errs, journalErr)
	}

	s.mu.Lock()
	if agentErr == nil {
		s.agent, s.agentAt = &agent, at
	}
	if freezeErr == nil {
		s.freezes, s.freezesAt = freezeList.Items, at
	}
	if rosterRead {
		s.roster, s.rosterAt = roster, at
	}
	if journalErr == nil {
		s.journalSignal = broker.BrakeOK
	} else {
		s.journalSignal = broker.BrakeFailed
	}
	s.refreshedAt = at
	s.mu.Unlock()

	return errors.Join(errs...)
}

// readRoster resolves spec.operations.approvalRosterRef.
//
// The bool is "this read produced an answer", and it is separate from the roster being nil because
// those are three states, not two: no reference configured (answer: nil, and that IS the answer),
// a reference that resolved, and a reference whose Get failed. Only the third leaves the previous
// value in place -- and even then the result is a park, never an approval.
//
// A dangling reference is NOT an error. common_types.go says so at the field: "Admission does not
// require the roster to exist, because ordering a roster before the agent that names it would make
// a two-object install order-dependent; the runtime handles the gap by refusing, not by skipping."
// So a NotFound is a definite nil, which is row 6, and it is reported as an answer.
func (s *Source) readRoster(ctx context.Context, agent *agentv1alpha1.Agent) (*agentv1alpha1.ApprovalRoster, bool, error) {
	ops := agent.Spec.Operations
	if ops == nil || ops.ApprovalRosterRef == nil || ops.ApprovalRosterRef.Name == "" {
		return nil, true, nil
	}
	ref := ops.ApprovalRosterRef
	ns := ref.Namespace
	if ns == "" {
		ns = s.namespace
	}
	var roster agentv1alpha1.ApprovalRoster
	if err := s.reader.Get(ctx, types.NamespacedName{Namespace: ns, Name: ref.Name}, &roster); err != nil {
		if apierrors.IsNotFound(err) {
			return nil, true, nil
		}
		return nil, false, fmt.Errorf("brake: reading ApprovalRoster %s/%s (06 §4.4 row 6): %w", ns, ref.Name, err)
	}
	return &roster, true, nil
}

// probe answers row 3: can the journal store be reached at all.
//
// A List capped at one item, because the question is reachability and not content -- and because
// `list` is a verb 06 §2.2.1 actually grants the broker on actionrecords, so a probe that passes
// here is evidence the real writes have the authority they need.
//
// There is NO staleness tolerance on this signal, unlike the freeze view, and the asymmetry is the
// spec's rather than a choice: 06 §4.4 row 1 says "API error, OR a cache stale beyond 30 s", and
// row 3 says only "cannot reach the journal store". Riding out a dropped request here would be
// inventing a tolerance for the one row whose consequence is auto-pausing the agent.
func (s *Source) probe(ctx context.Context) error {
	var probe agentv1alpha1.ActionRecordList
	if err := s.journal.List(ctx, &probe, client.InNamespace(s.namespace), client.Limit(1)); err != nil {
		return fmt.Errorf("brake: probing the ActionRecord store in %s (06 §4.4 row 3): %w", s.namespace, err)
	}
	return nil
}

// assemble builds the view from what is currently known, applying the staleness ceiling.
//
// broker.MaxFreezeStaleness is applied to the Agent and the roster as well as to the freeze list.
// 06 §4.4 states a ceiling only for row 1; the other rows say "cannot read" without saying how long
// a previously-successful read stays good. Reusing row 1's number is the narrowest choice that
// keeps every retained value bounded by something the spec actually wrote down, and it fails in the
// refusing direction: past the ceiling the value disappears and its row fires.
func (s *Source) assemble(ctx context.Context, now time.Time) pipeline.BrakeView {
	s.mu.Lock()
	var (
		agent   *agentv1alpha1.Agent
		freezes *broker.FreezeView
		roster  *agentv1alpha1.ApprovalRoster
	)
	if fresh(s.agentAt, now) {
		agent = s.agent
	}
	if fresh(s.freezesAt, now) {
		// ObservedAt is the read's own instant, not now: broker.Decide applies MaxFreezeStaleness
		// again, and handing it the serving time would make every cached view look brand new.
		freezes = &broker.FreezeView{Freezes: s.freezes, ObservedAt: s.freezesAt}
	}
	if fresh(s.rosterAt, now) {
		roster = s.roster
	}
	signal := s.journalSignal
	s.mu.Unlock()

	return pipeline.BrakeView{
		Agent:   agent,
		Freezes: freezes,
		Roster:  roster,
		Journal: signal,
	}
}

// fresh reports whether a value read at `at` may still be used at `now`. A zero `at` is never
// fresh, which is what makes "no successful read yet" and "a read that has gone stale" the same
// refusing answer.
func fresh(at, now time.Time) bool {
	if at.IsZero() {
		return false
	}
	return now.Sub(at) <= broker.MaxFreezeStaleness
}
