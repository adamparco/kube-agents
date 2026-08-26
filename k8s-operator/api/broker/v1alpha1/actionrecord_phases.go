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

package v1alpha1

import (
	"fmt"
	"sort"
)

// The 06 §4.3 status lifecycle, as the one place Go holds it.
//
// # Why this file exists
//
// Until it did, `ActionPhase` was a kubebuilder enum and nothing else: `status.phase` could go from
// `Verified` to `Pending`, from `Rejected` to `Executing`, or from nothing at all to `Undone`, and
// every one of those was accepted by the API server and by `journal.Store.SetPhase`. The lifecycle
// existed as an ASCII diagram in a doc comment, which is documentation of an intention rather than
// a property of the system. 06 §4.3's own status-RBAC table then rests on that lifecycle -- the
// ChatOps gateway is permitted `PendingApproval -> Pending/Rejected` **and nothing else**, and the
// undo controller `-> Undone` **only** -- so a lifecycle nothing enforces makes those two rows
// unenforceable in the direction that matters: not "who may write the field" (that is admission's
// job) but "what may the field become".
//
// # How the edge set was derived, and the two places the diagram is not the authority
//
// Most edges are read straight off the 06 §4.3 diagram. Two are not, and both were settled from the
// spec's own **principal list** rather than from its picture, because that list is the finer
// statement and it is the thing conformance can test:
//
//  1. The diagram draws `Failed --> RolledBack`. No principal in the 06 §4.3 status-RBAC table can
//     write that edge. The four writers are the owning broker, the undo controller (`phase` ->
//     `Undone` only), the ChatOps gateway (`PendingApproval` -> `Pending`/`Rejected`) and the
//     exporter (deliberately cannot touch `phase` at all). The broker reaches `Failed` only at 04
//     §5.1 rung 5, which is "an immediate page, not a retry loop" -- there is nothing after it to
//     do the rolling back. So the edge is dropped, and the phase table's own `Terminal` column,
//     which marks `Failed` terminal, is what the code implements.
//
//  2. The diagram's `Verified --> Undone` survives even though the same table marks `Verified`
//     terminal, because "terminal" there means terminal **for the broker's pipeline** -- no further
//     automatic progression -- and `Undone` is written later, out of band, by a different principal
//     the RBAC table names explicitly. Terminality and reachability are different claims and the
//     table is making the first one.
//
// # Self-edges are legal, and that is not a hole in the DAG
//
// `p -> p` is permitted for every phase. `journal.Store.SetPhase` re-reads before writing precisely
// because status conflicts between the broker and a controller are routine, and a conflict retry
// re-issues the same write. Refusing the repeat would convert a harmless retry into a lost
// transition, which is the failure the re-read loop exists to prevent. A same-phase write changes
// nothing an observer can see, so allowing it weakens no property the table asserts.
//
// # `Executing -> DryRun`, which looks wrong and is not
//
// The diagram says DryRun "is a terminal state reached from Pending". The broker reaches it from
// `Executing`, and that is deliberate: 03 §6's write-ahead rule requires the record to be durably
// `Executing` **before** anything could be applied, and `internal/broker/writeahead` refuses to
// proceed on any other status. A dry run that skipped `Executing` would take a different path
// through the one guarantee it exists to rehearse -- and since the whole of Phase 9 is dry-run, the
// write-ahead guarantee would then be untested in the only mode that runs. Both edges are legal;
// the broker takes the second.

// actionPhaseTransitions is the adjacency list. A phase absent from this map, or present with an
// empty set, is terminal.
//
// It is deliberately written as data rather than as a switch: `AllActionPhases` iterates it, the
// conformance test cross-joins it against the kubebuilder enum, and a phase added to the enum
// without an entry here fails that test rather than silently becoming an unreachable island.
var actionPhaseTransitions = map[ActionPhase][]ActionPhase{
	// Accepted and classified. The three ways forward are the three answers the classifier can
	// give, plus the refusal that can arrive from the brake at any point before execution.
	PhasePending: {PhasePendingApproval, PhaseExecuting, PhaseDryRun, PhaseRejected},

	// Parked. `Pending` and `Rejected` are the ChatOps gateway's two permitted writes; `Executing`
	// is the broker resuming an approved action directly; `Expired` is the approval TTL.
	PhasePendingApproval: {PhasePending, PhaseExecuting, PhaseRejected, PhaseExpired},

	// Mid-flight. `Verified` is 04 §5 rung 0, `RolledBack` is rung 3 succeeding, `Failed` is rung 5
	// (rollback failed -> page), and the self-edge is rung 1, the transient retry.
	PhaseExecuting: {PhaseVerified, PhaseFailed, PhaseRolledBack, PhaseDryRun},

	// Terminal for the broker; the undo controller may still reverse it. See the file comment.
	PhaseVerified: {PhaseUndone},

	// The six with no successor. Listed explicitly, with empty sets, so that "terminal" is a
	// statement this file makes rather than an absence a reader has to notice.
	PhaseFailed:     {},
	PhaseRolledBack: {},
	PhaseUndone:     {},
	PhaseRejected:   {},
	PhaseExpired:    {},
	PhaseDryRun:     {},
}

// actionPhaseInitial is the set a record may be CREATED in.
//
// Creation is not a transition and does not obey the table above: the record is written at the
// first moment its outcome is durable, which for a gated action is `PendingApproval` (06 §4.2
// step 7 parks it) and for a refused envelope is `Rejected` (there is no pre-refusal moment worth
// journaling, and 06 §4.3's retention table gives refusals their own 365-day row).
//
// The five phases NOT here -- `Verified`, `Failed`, `RolledBack`, `Undone`, `Expired` -- each assert
// that an earlier phase happened. A record born in one of them is manufactured history: a journal
// entry claiming an action was executed and verified, with no `Executing` it could have come from.
// That is the forgery this set exists to refuse, and it is why the set is a whitelist.
var actionPhaseInitial = map[ActionPhase]bool{
	PhasePending:         true,
	PhasePendingApproval: true,
	PhaseExecuting:       true,
	PhaseRejected:        true,
}

// AllActionPhases returns every phase in the lifecycle, sorted, so callers that must be exhaustive
// can iterate rather than transcribe. The sort makes test output and error messages stable.
func AllActionPhases() []ActionPhase {
	out := make([]ActionPhase, 0, len(actionPhaseTransitions))
	for p := range actionPhaseTransitions {
		out = append(out, p)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

// KnownActionPhase reports whether p is a phase the lifecycle knows about. The empty phase is not
// one: an ActionRecord whose `status.phase` is unset has not entered the lifecycle, and callers
// that need to say so should use IsInitialActionPhase on the phase they are about to write.
func KnownActionPhase(p ActionPhase) bool {
	_, ok := actionPhaseTransitions[p]
	return ok
}

// IsTerminal reports whether p has no successor. An unknown phase is NOT reported terminal --
// answering "yes" for something the table has never heard of would let a typo read as a completed
// action.
func (p ActionPhase) IsTerminal() bool {
	next, ok := actionPhaseTransitions[p]
	return ok && len(next) == 0
}

// Successors returns the phases p may legally become, excluding the universal self-edge. The
// returned slice is a copy: the table is package state and a caller that appended to it would
// rewrite the lifecycle for the whole process.
func (p ActionPhase) Successors() []ActionPhase {
	next := actionPhaseTransitions[p]
	out := make([]ActionPhase, len(next))
	copy(out, next)
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

// CanTransitionTo reports whether `p -> next` is a legal 06 §4.3 transition.
//
// The empty `p` is handled as the from-side of creation: an ActionRecord whose status subresource
// has never been written has no phase, and the first write to it must land on an initial phase. It
// is NOT treated as "anything goes" -- that reading is what let a record's first observed phase be
// `Undone`.
func (p ActionPhase) CanTransitionTo(next ActionPhase) bool {
	if !KnownActionPhase(next) {
		return false
	}
	if p == "" {
		return actionPhaseInitial[next]
	}
	if !KnownActionPhase(p) {
		return false
	}
	if p == next {
		return true
	}
	for _, ok := range actionPhaseTransitions[p] {
		if ok == next {
			return true
		}
	}
	return false
}

// IsInitialActionPhase reports whether a record may be created carrying p.
func IsInitialActionPhase(p ActionPhase) bool {
	return actionPhaseInitial[p]
}

// ValidateActionPhaseTransition returns nil when `from -> to` is legal and, when it is not, an error
// that names both ends and what WOULD have been legal.
//
// The suggestion list is not decoration. The caller of a refused transition is a controller, and the
// thing it needs in a log line is not "that was illegal" but "here is the phase you were probably
// supposed to write" -- the alternative is a human re-deriving the lifecycle from a doc comment
// during an incident.
func ValidateActionPhaseTransition(from, to ActionPhase) error {
	if from.CanTransitionTo(to) {
		return nil
	}
	if !KnownActionPhase(to) {
		return fmt.Errorf("phase %q is not a member of the 06 §4.3 lifecycle", to)
	}
	if from == "" {
		return fmt.Errorf("an ActionRecord may not be created in phase %q: creation is only legal into %v (06 §4.3) -- a record born in a later phase is a journal entry for an action nothing observed", to, initialPhases())
	}
	if !KnownActionPhase(from) {
		return fmt.Errorf("phase %q is not a member of the 06 §4.3 lifecycle, so no transition out of it is defined", from)
	}
	if from.IsTerminal() {
		return fmt.Errorf("%q is terminal (06 §4.3): it may not become %q", from, to)
	}
	return fmt.Errorf("%q -> %q is not a legal 06 §4.3 transition; from %q the lifecycle allows %v", from, to, from, from.Successors())
}

func initialPhases() []ActionPhase {
	out := make([]ActionPhase, 0, len(actionPhaseInitial))
	for p := range actionPhaseInitial {
		out = append(out, p)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}
