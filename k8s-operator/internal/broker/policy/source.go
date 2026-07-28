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

package policy

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/classify"
)

const (
	// MaxPolicyStaleness is how old the last successful ChangePolicy read may be before this source
	// refuses to answer. Deliberately the same 30s as broker.MaxFreezeStaleness, and for the same
	// reason spelled out there: an unreadable list is not an empty list. A policy set that cannot be
	// refreshed does not gradually become less trustworthy -- it becomes, at a knowable moment, a
	// set that may be missing a policy an operator applied specifically to stop what is about to be
	// classified.
	MaxPolicyStaleness = 30 * time.Second

	// DefaultRefreshInterval is how often the source re-reads. Three intervals fit inside
	// MaxPolicyStaleness on purpose: one lost poll -- a leader election blip, a rolling API server
	// -- must not refuse actions, and two consecutive lost polls must.
	DefaultRefreshInterval = 10 * time.Second
)

// Lister is the one API-server verb this source needs. Narrow on purpose: 03 §2 gives the broker
// `get, list, watch` on changepolicies and nothing else, and a dependency typed as the full
// client.Client would make it possible to write a create here and only discover the RBAC gap at L2.
type Lister interface {
	List(ctx context.Context, list client.ObjectList, opts ...client.ListOption) error
}

// SourceConfig assembles a Source.
type SourceConfig struct {
	// Reader lists ChangePolicy objects. Cluster-scoped, so no namespace option is ever set.
	Reader Lister

	// Agent is the identity the selector is evaluated against: this broker's own agent, from its own
	// deployment, never from an envelope.
	Agent Agent

	// History is passed through to classify.New on every rebuild.
	History classify.ActionHistory

	// RefreshInterval defaults to DefaultRefreshInterval. Values at or above MaxPolicyStaleness are
	// rejected by NewSource: a source that cannot refresh faster than it goes stale refuses more
	// often than it answers, which reads in production as "the broker is broken" rather than as the
	// misconfiguration it is.
	RefreshInterval time.Duration

	// Now is injectable so a test can age a snapshot without sleeping.
	Now func() time.Time
}

// Source is the broker's live view of the cluster's ChangePolicy set.
//
// # Why this polls rather than watches
//
// The deferral this closes was written as "the ChangePolicy informer", and an informer is the
// reflex. It is the wrong shape here, and this codebase has already written down why: see
// broker.MaxFreezeStaleness -- "a watch that silently stopped delivering is not an error at all --
// the informer's List succeeds, the cache answers instantly, and every answer is from before the
// incident started."
//
// A cache-backed source therefore needs a freshness signal that the cache itself cannot provide,
// and every way of building one ends in a periodic read against the API server. At that point the
// cache is buying latency, not correctness. ChangePolicy is cluster-scoped, human-authored, and
// will number in the single digits; a full List every ten seconds is a rounding error against the
// TokenReview this broker already performs per request. So the source polls, freshness is true by
// construction rather than by an auxiliary heartbeat, and the cost is that a tightening takes
// effect within RefreshInterval instead of within a round trip. For a policy a human just typed,
// that is not a cost.
//
// # Two failures that look alike and are not
//
// A poll can fail in two ways, and they need opposite handling. Collapsing them is the mistake this
// type is arranged to avoid.
//
// **The API server did not answer.** Transient by nature: a dropped connection, a rolling control
// plane, a leader election. The last good snapshot is RETAINED and allowed to age. Inside
// MaxPolicyStaleness the broker keeps classifying against the last policy set it genuinely saw;
// past it, the broker refuses. Discarding on the first dropped request would turn a blip into a
// fleet-wide outage on a resource whose contents almost never change. Never discarding would turn a
// permanently unreachable API server into a broker classifying against an hour-old policy set,
// which -- because the classifier maxes over sources -- is the loosening direction.
//
// **The policy set was read and will not load.** Not transient at all: a policy that fails
// conversion will fail it on every poll from now until a human edits the object. The snapshot is
// DISCARDED and the broker refuses immediately. Aging this one out would mean thirty seconds of
// classifying against a policy set the broker already knows is wrong, and the operator who applied
// the bad policy would get their signal late, from a timeout, instead of at once. In a healthy
// cluster admission makes this unreachable -- the webhook runs the same ValidateChangeRule the
// loader does -- so reaching it means the webhook was down or the object predates the rule, which
// are exactly the circumstances in which the conservative answer is the right one.
type Source struct {
	reader   Lister
	agent    Agent
	history  classify.ActionHistory
	interval time.Duration
	now      func() time.Time

	mu      sync.RWMutex
	snap    *Snapshot
	lastErr error
}

// NewSource validates the wiring. It does not read; call Refresh or Run.
func NewSource(cfg SourceConfig) (*Source, error) {
	if cfg.Reader == nil {
		return nil, errors.New("policy: a Reader is required; a source that cannot list ChangePolicy objects would report every agent as unpoliced")
	}
	if cfg.RefreshInterval < 0 {
		return nil, fmt.Errorf("policy: RefreshInterval %s is negative", cfg.RefreshInterval)
	}
	if cfg.RefreshInterval == 0 {
		cfg.RefreshInterval = DefaultRefreshInterval
	}
	if cfg.RefreshInterval >= MaxPolicyStaleness {
		return nil, fmt.Errorf(
			"policy: RefreshInterval %s is not shorter than MaxPolicyStaleness %s; the source would go stale between successful reads and refuse actions it has current policy for",
			cfg.RefreshInterval, MaxPolicyStaleness)
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	return &Source{
		reader:   cfg.Reader,
		agent:    cfg.Agent,
		history:  cfg.History,
		interval: cfg.RefreshInterval,
		now:      cfg.Now,
	}, nil
}

// Refresh performs one read and rebuilds the snapshot.
//
// Exported because startup should be synchronous: a broker whose very first policy read fails
// should fail to start, loudly, rather than come up healthy and refuse every action for reasons a
// reader of the logs has to reconstruct.
func (s *Source) Refresh(ctx context.Context) error {
	var list agentv1alpha1.ChangePolicyList
	if err := s.reader.List(ctx, &list); err != nil {
		// Read failure: retain. See the type comment.
		s.fail(fmt.Errorf("listing ChangePolicy: %w", err), false)
		return fmt.Errorf("policy: listing ChangePolicy: %w", err)
	}
	policies := make([]*agentv1alpha1.ChangePolicy, 0, len(list.Items))
	for i := range list.Items {
		policies = append(policies, &list.Items[i])
	}

	snap, err := Build(policies, s.agent, s.history, s.now())
	if err != nil {
		// Load failure: discard. The policy set was read successfully and is unusable, and it will
		// stay unusable until someone edits the object -- so there is nothing to wait out.
		s.fail(err, true)
		return fmt.Errorf("policy: %w", err)
	}

	s.mu.Lock()
	s.snap = snap
	s.lastErr = nil
	s.mu.Unlock()
	return nil
}

// Run refreshes on a ticker until ctx is cancelled. Errors are retained for Current to report and
// do not stop the loop: the next poll may well succeed, and stopping would convert a transient
// failure into a permanent one at the moment MaxPolicyStaleness elapses.
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

// Current implements the pipeline's ClassifierSource: the classifier for the policy set as of the
// last successful read, or an error naming exactly why there isn't one.
//
// The error text is the operator-facing artifact of every fail-closed decision in this package, so
// it says what was last seen and when, not just that something went wrong.
func (s *Source) Current() (*classify.Classifier, error) {
	s.mu.RLock()
	snap, lastErr := s.snap, s.lastErr
	s.mu.RUnlock()

	if snap == nil {
		if lastErr != nil {
			return nil, fmt.Errorf("policy: no ChangePolicy set has ever been read successfully: %w", lastErr)
		}
		return nil, errors.New("policy: no ChangePolicy set has been read yet; the source has not been refreshed")
	}
	if age := s.now().Sub(snap.ObservedAt()); age > MaxPolicyStaleness {
		if lastErr != nil {
			return nil, fmt.Errorf(
				"policy: the ChangePolicy set was last read %s ago, past the %s staleness limit, and every read since has failed: %w",
				age.Truncate(time.Second), MaxPolicyStaleness, lastErr)
		}
		return nil, fmt.Errorf(
			"policy: the ChangePolicy set was last read %s ago, past the %s staleness limit",
			age.Truncate(time.Second), MaxPolicyStaleness)
	}
	return snap.Classifier(), nil
}

// Snapshot is the current snapshot subject to the same staleness rule as Current, for callers that
// need the bound policy names (status reporting, diagnostics) rather than the classifier.
func (s *Source) Snapshot() (*Snapshot, error) {
	if _, err := s.Current(); err != nil {
		return nil, err
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.snap, nil
}

// fail records a failed poll. discard distinguishes the two failure classes in the type comment: a
// load failure invalidates what we hold, a read failure does not.
func (s *Source) fail(err error, discard bool) {
	s.mu.Lock()
	s.lastErr = err
	if discard {
		s.snap = nil
	}
	s.mu.Unlock()
}
