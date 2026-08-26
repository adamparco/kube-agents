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

// Package verify implements step 10 of the broker pipeline (03 §4.1): confirm the intended outcome
// actually occurred, and recover automatically when it did not (04 §5, §5.1).
//
// The package is deliberately split along the line between what is DECIDED and what is DONE:
//
//   - `ladder.go` records movement on the 04 §5 recovery ladder and refuses illegal movement.
//   - `outcome.go` classifies a failure as transient or terminal -- the input to that movement.
//   - `predicate.go` is the per-kind "did it actually work" table of 04 §5.1.
//   - `driver.go` runs the three together: poll to the settle window, classify, climb, roll back.
//   - `fanout.go` expands a label selector against live state exactly once.
//
// Nothing here calls a model. Verification is a predicate over cluster state, and a verification
// step that asked an LLM whether the change worked would be the same trust boundary the broker
// exists to draw, redrawn in the wrong place.
package verify

import (
	"fmt"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

// The five rungs of 04 §5, plus the zero value meaning "nothing has gone wrong yet".
//
// The numbers are the spec's, not an implementation detail: `status.recovery.rung` is a documented
// API field an operator reads, and 06's CRD pins it to 0..5.
const (
	// RungNone is the starting state: the action has not needed recovery.
	RungNone int32 = 0
	// RungRetry is rung 1 -- retry with backoff. The failure is transient.
	RungRetry int32 = 1
	// RungAlternative is rung 2 -- try one alternative approach. Bounded to one, not a search.
	RungAlternative int32 = 2
	// RungRollback is rung 3 -- roll back. Automatic, and the broker's own decision.
	RungRollback int32 = 3
	// RungEscalate is rung 4 -- escalate to the parent tier. A real mesh call.
	RungEscalate int32 = 4
	// RungPage is rung 5 -- page a human.
	RungPage int32 = 5
)

// RungName renders a rung for a human-readable error or report.
func RungName(rung int32) string {
	switch rung {
	case RungNone:
		return "none"
	case RungRetry:
		return "retry"
	case RungAlternative:
		return "alternative"
	case RungRollback:
		return "rollback"
	case RungEscalate:
		return "escalate"
	case RungPage:
		return "page"
	default:
		return fmt.Sprintf("rung-%d", rung)
	}
}

// Ladder is the append-only recorder for 04 §5. It is the PRODUCER half of the invariant: an
// illegal movement is refused at the point it is attempted, so a record that exists is a record
// that was legal to write.
//
// `ValidateRecovery` is the AUDITOR half, and the two are separate on purpose. A producer-only
// guarantee holds for exactly as long as every writer goes through this type, and `status.recovery`
// is a subresource the broker SA can write directly -- so the property has to be checkable from the
// stored object alone, by a reader that did not observe how it was built.
type Ladder struct {
	rec agentv1alpha1.ActionRecovery
}

// NewLadder starts a ladder at RungNone.
func NewLadder() *Ladder { return &Ladder{} }

// FromRecovery resumes an existing ladder, e.g. after the broker restarts mid-recovery. The
// recovery it is handed must already be legal -- a resumed ladder that started from an invalid
// history would launder the invalid part into a valid-looking future.
func FromRecovery(r agentv1alpha1.ActionRecovery) (*Ladder, error) {
	if err := ValidateRecovery(&r); err != nil {
		return nil, fmt.Errorf("cannot resume an invalid recovery ladder: %w", err)
	}
	l := &Ladder{rec: *r.DeepCopy()}
	return l, nil
}

// Rung is the current position.
func (l *Ladder) Rung() int32 { return l.rec.Rung }

// Recovery returns a deep copy suitable for writing to `status.recovery`.
func (l *Ladder) Recovery() agentv1alpha1.ActionRecovery { return *l.rec.DeepCopy() }

// Climb moves to `to`, recording the transition. It refuses anything ValidateRecovery would later
// reject, and it refuses BEFORE mutating, so a rejected climb leaves the ladder untouched.
//
// `reason` is mandatory whenever the move skips a rung. It is accepted (and recorded) on every
// move, because the reason for entering rung 3 is the most useful line in the whole record.
func (l *Ladder) Climb(to int32, at time.Time, reason string) error {
	from := l.rec.Rung
	if err := checkTransition(from, to, reason, l.rec.Transitions); err != nil {
		return err
	}
	l.rec.Transitions = append(l.rec.Transitions, agentv1alpha1.RecoveryTransition{
		At:     metav1.NewTime(at),
		From:   from,
		To:     to,
		Reason: reason,
	})
	l.rec.Rung = to
	return nil
}

// ValidateRecovery checks a stored `status.recovery` against every rule of 04 §5. It is the
// mechanization of V-PRO-021 and it reads only the object -- no knowledge of how it was produced.
//
// A nil recovery is valid: an action that never needed recovery has none.
func ValidateRecovery(r *agentv1alpha1.ActionRecovery) error {
	if r == nil {
		return nil
	}
	cur := RungNone
	for i, t := range r.Transitions {
		if t.From != cur {
			return fmt.Errorf("transition %d starts at rung %d but the ladder was at rung %d: "+
				"the history is not a chain, so some movement went unrecorded", i, t.From, cur)
		}
		if err := checkTransition(t.From, t.To, t.Reason, r.Transitions[:i]); err != nil {
			return fmt.Errorf("transition %d: %w", i, err)
		}
		if t.At.IsZero() {
			return fmt.Errorf("transition %d (%s -> %s) carries no timestamp",
				i, RungName(t.From), RungName(t.To))
		}
		if i > 0 && t.At.Time.Before(r.Transitions[i-1].At.Time) {
			return fmt.Errorf("transition %d (%s -> %s) is timestamped before the transition it follows",
				i, RungName(t.From), RungName(t.To))
		}
		cur = t.To
	}
	if r.Rung != cur {
		return fmt.Errorf("recovery.rung is %d but the transitions end at %d: "+
			"the summary field and the history disagree, and a reader trusts whichever it happened to read",
			r.Rung, cur)
	}
	return nil
}

// checkTransition holds every rule in one place so the producer and the auditor cannot drift.
// `prior` is the history BEFORE this transition.
func checkTransition(from, to int32, reason string, prior []agentv1alpha1.RecoveryTransition) error {
	if to < RungNone || to > RungPage {
		return fmt.Errorf("rung %d is outside the 04 §5 ladder (0..5)", to)
	}
	if to == from {
		// Non-decreasing would permit it, and permitting it would quietly undo the rung-2 bound
		// below: two 2->2 transitions are two alternatives. A retry that happens five times is
		// still one visit to rung 1; the backoff belongs in the attempt counter, not here.
		return fmt.Errorf("a transition from rung %d to itself records no movement — "+
			"repeated attempts at one rung are one visit to that rung", from)
	}
	if to < from {
		return fmt.Errorf("cannot descend from %s (rung %d) to %s (rung %d): "+
			"the ladder is non-decreasing, and descending is how a system retries forever "+
			"while looking like it is recovering",
			RungName(from), from, RungName(to), to)
	}
	if to > from+1 && reason == "" {
		return fmt.Errorf("skipping from %s (rung %d) to %s (rung %d) requires a reason — "+
			"04 §5 says the ladder is never skipped SILENTLY, which is a requirement on the record, "+
			"not on the movement",
			RungName(from), from, RungName(to), to)
	}
	// The two rules below are DERIVED, not independent: monotonicity and the no-movement rule
	// already make each rung enterable at most once, so neither can currently fire on a history
	// that got past the checks above. Mutation testing found exactly that -- both can be deleted
	// with the suite still green, and TestLadderPropertiesHoldOverEveryAcceptedHistory is what
	// actually holds the two properties down, by enumerating histories rather than poking an `if`.
	//
	// They are kept because they are the two properties 04 §5 names in words, and because the
	// implication runs one way: if a future spec revision ever relaxes monotonicity -- a second
	// alternative after an escalation returns is the obvious candidate -- these stop being derived
	// and start being the only thing enforcing the bound. A rule deleted for being redundant is a
	// rule nobody restores when the premise changes.
	if to == RungAlternative {
		for _, p := range prior {
			if p.To == RungAlternative {
				return fmt.Errorf("rung 2 has already been entered (at %s): 04 §5 bounds this to "+
					"one alternative, not a search", p.At.Time.UTC().Format(time.RFC3339))
			}
		}
	}
	if to == RungRetry {
		for _, p := range prior {
			if p.To >= RungRollback {
				return fmt.Errorf("cannot return to rung 1 after reaching %s: 04 §5 forbids "+
					"restarting at the bottom for the same target after a rollback",
					RungName(p.To))
			}
		}
	}
	return nil
}
