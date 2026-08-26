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

// Package history is the production classify.ActionHistory: the answer to "has this agent done
// this before?", recovered from the ActionRecord journal rather than held in a map.
//
// # The gap this fills, and the direction it was failing in
//
// classify.ActionHistory has existed since P9-T2 and had no production implementation. The
// classifier guarded its use with `c.knownActions != nil &&`, so a broker that was never handed one
// had the 06 §4.2 `novel-action` escalation SWITCHED OFF -- silently, and in the loosening
// direction, which invariant 4 does not permit. policy.NewSource accepted a nil History without
// complaint. This package supplies the value; the nil guard in classify.Classify is inverted in the
// same change, so a missing history now means "everything is novel" rather than "nothing is".
//
// # The verb the journal does not record
//
// 06 §4.2 states the rule as "first occurrence of `(op, kind)` for this agent in the trust-building
// window". 06 §4.3 defines the ActionRecord, and it does not carry the envelope's `op`. The record
// has `spec.targets` (group/version/kind/namespace/name), `spec.intent` (prose), and a
// classification -- and no verb anywhere. So a journal-derived history cannot read the verb off the
// record, and the three obvious ways out are each wrong:
//
//   - **Ignore the verb.** An agent that has patched Deployments in team-x would then be familiar
//     with DELETING one, and the `+1` that should fire would not. That is a risk class lowered by
//     an implementation detail, which invariant 4 forbids outright.
//   - **Never answer true.** Strictly stricter and therefore permitted, but it makes `novel-action`
//     fire on 100% of traffic forever. A control that fires always carries no signal, and the
//     operational end state is approval fatigue -- humans rubber-stamping a gate, which is less safe
//     than the gate not existing.
//   - **Add a field to the CRD.** 06 §4.3 spells the schema out. Amending it is spec work, not a
//     silence an implementer may fill (PROTOCOL §10.5) -- the same argument cooldown records for why
//     it did not invent a CRD to hold quiet periods.
//
// # What the journal DOES record: 06 §4.3.1, read backwards
//
// The undo plan is generated at step 6 from a table keyed on the original op, and BOTH of its
// outputs are durable, structured, enumerated fields: `spec.undo.strategy` and
// `spec.undo.steps[].op`. That table read in reverse recovers the verb up to an equivalence:
//
//	forward op                 strategy    step op    ⇒ class
//	create                     delete      delete       delete/delete
//	apply (object was absent)  delete      delete       delete/delete
//	apply (object existed)     restore     apply        restore/apply
//	patch                      restore     apply        restore/apply
//	scale                      restore     scale        restore/scale
//	delete                     recreate    create       recreate/create
//	cloud apply / setSize      inverse     (varies)     inverse/*
//	anything else              none        --           (no evidence)
//
// Two verbs collapse into one class only where they are the same mutation: `apply` over an object
// that did not exist IS a create, and `apply` over one that did IS a patch. Every pair that matters
// stays separate -- `delete` is `recreate/create` and nothing else reaches it, `scale` is
// `restore/scale` and nothing else reaches it. So this is a coarsening that loses no distinction
// invariant 4 cares about, and it is derived entirely from fields the broker already writes.
//
// The residual runs the other way, and is the safe one. `apply` is the union of two classes, so
// Seen requires the journal to show BOTH before calling an apply familiar. An agent that has only
// ever created is not yet familiar with applying, because its next apply may be the update it has
// never done. A record with `strategy: none` -- which is every action that could not be undone, and
// which 06 §4.3.1 gates for that reason -- contributes no evidence at all.
//
// # The trust-building window
//
// 06 §4.2 names one and the design set never defines it: "trust-building window" appears exactly
// once in the specs. That is a silence rather than a contradiction, so this picks the simplest
// reading consistent with every invariant: **the window is the journal's own retention window.**
// Records age out on the 06 §4.3 retention clock -- 30 days at the shortest class -- and when a
// record is gone the familiarity it conferred is gone with it. Nothing separate expires, nothing
// separate is stored, and the window cannot drift out of step with the evidence it is a window over.
//
// # Which records build trust
//
// Only `status.phase: Verified`, and only with `spec.dryRun: false`.
//
// `Verified` is the sole phase meaning the write landed AND the verifier agreed. `Executing` has not
// finished; `Failed` and `RolledBack` did not stand -- and cooldown.derive already counts those as
// evidence of the opposite; `Rejected` and `Expired` never touched the cluster. `Undone` is
// deliberately excluded even though its write did stand: a human reversed it, and treating the
// reversal as trust would suppress the escalation on precisely the repeat a human just said no to.
//
// The dry-run exclusion is what makes this correct for the phase it ships in. The whole of Phase 9
// runs `dryRun: true` and journals `PhaseDryRun`, so nothing an agent does before the broker has
// write authority can make anything familiar. An action that was never executed is not experience.
//
// # Staleness points the other way here
//
// policy.Source and cooldown.Source both REFUSE past their staleness ceiling, because a stale
// answer from either is the permissive answer. This one is the reverse: the seen-set only ever
// grows, so an old snapshot is a SMALLER set, which means more novelty, which means more
// escalation. Blindness is already the strict direction, which is why classify.ActionHistory can
// get away with returning a bare bool and no error.
//
// MaxHistoryStaleness therefore bounds nothing about safety. It exists so that a source whose
// refresh loop has silently died degrades to "escalate everything" -- loud, and visible in the
// reasons on every record -- instead of quietly serving an answer from before the incident.
package history

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
	// DefaultRefreshInterval is how often the source re-reads the journal.
	//
	// Ten of these fit inside MaxHistoryStaleness, where policy.Source fits three inside its own.
	// The ratio is generous because a lost poll costs the opposite thing here: policy going stale
	// refuses actions, history going stale escalates them, and escalation is survivable in a way
	// that a fleet-wide refusal is not.
	DefaultRefreshInterval = 30 * time.Second

	// MaxHistoryStaleness is how old the last successful read may be before Seen stops answering
	// true. See the package comment: this is not a safety bound -- past it the source reports
	// everything as novel, which is the escalating direction -- it is the bound that makes a dead
	// refresh loop visible instead of silent.
	MaxHistoryStaleness = 5 * time.Minute
)

// Journal is the one API-server verb this source needs. Narrow on purpose, the same way
// policy.Lister and cooldown.Journal are: 06 §2.2.1 gives the broker `get, list, watch, create` on
// actionrecords, and a dependency typed as the full client.Client would make it possible to write an
// update here and discover the missing verb at L2.
type Journal interface {
	List(ctx context.Context, list client.ObjectList, opts ...client.ListOption) error
}

// SourceConfig assembles a Source.
type SourceConfig struct {
	// Journal lists ActionRecord objects. Required.
	Journal Journal

	// Namespace is the agent's own namespace. ActionRecords are namespaced and live beside the agent
	// that wrote them (06 §4.3), and the broker's Role is namespaced too -- a cluster-wide List would
	// be Forbidden, not merely broad. Required.
	Namespace string

	// RefreshInterval defaults to DefaultRefreshInterval. Values at or above MaxHistoryStaleness are
	// rejected: a source that cannot refresh faster than it goes stale would spend most of its life
	// reporting an empty seen-set.
	RefreshInterval time.Duration

	// Now is injectable so a test can age a snapshot without sleeping.
	Now func() time.Time
}

// key is the seen-set's element. A struct rather than a joined string because every field here is
// attacker-adjacent -- a namespace and a kind both come off the wire eventually -- and a delimiter
// in a joined key is a way for two different tuples to collide into one.
type key struct {
	agent     string
	class     string
	group     string
	kind      string
	namespace string
}

// Source is the journal-derived classify.ActionHistory.
//
// Read the package comment first; it holds the argument. This type is the mechanism: a periodically
// refreshed set of (agent, undo-class, kind, namespace) tuples the journal has positively
// witnessed, and a lookup that requires every class a verb could produce.
type Source struct {
	journal   Journal
	namespace string
	interval  time.Duration
	now       func() time.Time

	mu     sync.RWMutex
	seen   map[key]struct{}
	readAt time.Time
}

var _ classify.ActionHistory = (*Source)(nil)

// NewSource validates the wiring. It does not read; call Refresh, which startup should do
// synchronously for the reason policy.Source.Refresh gives.
func NewSource(cfg SourceConfig) (*Source, error) {
	if cfg.Journal == nil {
		return nil, errors.New("history: a Journal is required; a source that cannot list ActionRecords could only ever report every action as novel")
	}
	if cfg.Namespace == "" {
		return nil, errors.New("history: a Namespace is required; ActionRecords are namespaced and an unscoped List is Forbidden to the broker's Role (06 §2.2.1)")
	}
	if cfg.RefreshInterval < 0 {
		return nil, fmt.Errorf("history: RefreshInterval %s is negative", cfg.RefreshInterval)
	}
	if cfg.RefreshInterval == 0 {
		cfg.RefreshInterval = DefaultRefreshInterval
	}
	if cfg.RefreshInterval >= MaxHistoryStaleness {
		return nil, fmt.Errorf(
			"history: RefreshInterval %s is not shorter than MaxHistoryStaleness %s; the snapshot would go stale between reads and the source would report everything as novel",
			cfg.RefreshInterval, MaxHistoryStaleness)
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	return &Source{
		journal:   cfg.Journal,
		namespace: cfg.Namespace,
		interval:  cfg.RefreshInterval,
		now:       cfg.Now,
	}, nil
}

// Refresh performs one read and rebuilds the seen-set.
//
// A read failure RETAINS the previous snapshot and lets it age, exactly as policy.Source and
// cooldown.Source do: discarding on the first dropped request would turn a control-plane blip into
// a broker that escalates every action for the next few minutes. There is no second failure class
// here -- unlike a ChangePolicy, an ActionRecord that will not parse cannot exist, because the list
// is typed.
func (s *Source) Refresh(ctx context.Context) error {
	var list agentv1alpha1.ActionRecordList
	if err := s.journal.List(ctx, &list, client.InNamespace(s.namespace)); err != nil {
		return fmt.Errorf("history: listing ActionRecords in %s: %w", s.namespace, err)
	}

	seen := derive(list.Items)

	s.mu.Lock()
	s.seen = seen
	s.readAt = s.now()
	s.mu.Unlock()
	return nil
}

// Run refreshes on a ticker until ctx is done. Errors are dropped deliberately: every one of them is
// already reflected in the answer, because a retained snapshot ages out into "everything is novel".
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

// Seen reports whether the journal positively witnessed this agent performing this verb's shape on
// this kind in this namespace.
//
// Every path that is not a positive witness returns false, and false is the escalating answer. A nil
// receiver, an unrefreshed source, a stale snapshot, an unknown verb, a partially-matched verb: all
// of them mean "this source cannot vouch for it", and the 06 §4.2 `+1` fires. There is no error
// return on classify.ActionHistory and this is why one is not needed.
func (s *Source) Seen(agentName, verb string, kind classify.KindRef, namespace string) bool {
	if s == nil {
		return false
	}
	want, known := verbEvidence[verb]
	if !known {
		// A verb this package has no 06 §4.3.1 row for. Not an error -- classify.KnownVerbs is the
		// enum and a lint joins the two -- but if the enum ever gains a member before this table
		// does, the new verb must be novel rather than familiar.
		return false
	}

	s.mu.RLock()
	seen, readAt := s.seen, s.readAt
	s.mu.RUnlock()

	if seen == nil || readAt.IsZero() {
		return false
	}
	if s.now().Sub(readAt) > MaxHistoryStaleness {
		return false
	}

	// EVERY class, not any: see the package comment on `apply`.
	for _, class := range want {
		if _, ok := seen[key{agent: agentName, class: class, group: kind.Group, kind: kind.Kind, namespace: namespace}]; !ok {
			return false
		}
	}
	return true
}

// verbEvidence is 06 §4.3.1's strategy table read backwards: the undo-plan classes each forward verb
// produces. Seen requires every class listed, because a verb with two possible plans is familiar
// only once the journal shows both of them.
//
// Keys are classify.KnownVerbs(). A verb missing from here is never familiar, which is why the test
// that joins this table to that enum asserts coverage rather than equality of some derived count.
var verbEvidence = map[string][]string{
	"create": {"delete/delete"},
	"apply":  {"delete/delete", "restore/apply"},
	"patch":  {"restore/apply"},
	"scale":  {"restore/scale"},
	"delete": {"recreate/create"},
	"cloud":  {"inverse/*"},
}

// class names the evidence one undo step carries. Empty means "no evidence", and every unrecognised
// input lands there: an unknown strategy, an unknown step op, a `none` plan.
//
// `inverse` ignores the step op because 06 §4.3.1 does not fix one -- the step is "the provider's
// inverse call", whose name is the provider's. The strategy alone is enough there, since `inverse`
// is reachable from cloud operations and nothing else.
func class(strategy agentv1alpha1.UndoStrategy, stepOp string) string {
	switch strategy {
	case agentv1alpha1.UndoInverse:
		return "inverse/*"
	case agentv1alpha1.UndoDelete, agentv1alpha1.UndoRestore, agentv1alpha1.UndoRecreate:
		if stepOp == "" {
			return ""
		}
		return string(strategy) + "/" + stepOp
	default:
		// UndoNone, and any strategy value the enum gains later. An action with no safe inverse is
		// gated by 06 §4.3.1 and teaches this source nothing.
		return ""
	}
}

// derive folds a journal listing into the seen-set.
//
// The per-step target is used rather than spec.targets, because the step is where the op lives: a
// plan-level strategy crossed with the record's whole target list would attribute a `scale` to an
// object the scale never touched. For every strategy in the table the undo step acts on the same
// object the forward operation did, so the two agree where it matters and the step is the one that
// cannot be wrong.
func derive(items []agentv1alpha1.ActionRecord) map[key]struct{} {
	out := map[key]struct{}{}
	for i := range items {
		rec := &items[i]
		if rec.Spec.DryRun {
			// An action that was never executed is not experience. This is the whole of Phase 9.
			continue
		}
		if rec.Status.Phase != agentv1alpha1.PhaseVerified {
			continue
		}
		agent := rec.Spec.AgentRef.Name
		if agent == "" {
			// Impossible through admission (spec.agentRef is required) and cheap to tolerate: a
			// record with no agent would otherwise confer familiarity on the agent named "".
			continue
		}
		if rec.Spec.Undo == nil {
			continue
		}
		for _, step := range rec.Spec.Undo.Steps {
			cl := class(rec.Spec.Undo.Strategy, step.Op)
			if cl == "" {
				continue
			}
			out[key{
				agent:     agent,
				class:     cl,
				group:     step.Target.Group,
				kind:      step.Target.Kind,
				namespace: step.Target.Namespace,
			}] = struct{}{}
		}
	}
	return out
}
