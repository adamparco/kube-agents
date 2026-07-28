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
	"fmt"
	"strings"
	"sync"
)

// The observable pipeline order of 03 §4.1 (V-BRK-011, V-BRK-014).
//
// 03 §4.1 says every mutation passes through exactly one sequence and that six of its steps are
// "not skippable by any caller". Written as prose that is an aspiration; the only thing that makes
// it a property is a record of which steps ran, produced by the pipeline itself rather than by a
// test that reimplements the order it is checking.
//
// THE TRACE IS NOT A LOG. A log is written by call sites that remember to write it, and the step a
// refactor drops is exactly the step whose log line goes with it -- which reads, afterwards, as a
// pipeline that never had that step. StepTrace instead OWNS the ordering: Run refuses to record a
// step that is not the immediate successor of the last one recorded, and the pipeline reaches its
// next step only by going through Run. So "step 7 ran before step 4" and "step 5 was skipped" are
// not conditions a check has to hunt for -- they are errors the pipeline returns at the moment they
// are attempted, and the action does not execute.
//
// That inversion is what makes V-BRK-014 testable at L1. Fault-inject at step k and the trace ends
// at k, not because the test asserted it but because there is no path to k+1 that does not pass
// through a Run call the failure never reaches.

// Step is one of the eleven steps of the 03 §4.1 broker pipeline. The numbers are the spec's own
// and are load-bearing: Run compares them.
type Step int

const (
	// StepAuthenticate is step 1: mTLS plus TokenReview, and the derivation of (tier, scope) from
	// the authenticated identity rather than from the body.
	StepAuthenticate Step = 1
	// StepValidate is step 2: schema validation of the envelope, plus the idempotency-key
	// recomputation and the three anti-replay mechanisms. All of it is "is this submission
	// well-formed and not a repeat", and none of it looks at a cluster.
	StepValidate Step = 2
	// StepResolveScope is step 3: every live read the rest of the pipeline depends on, performed
	// once, here. Nothing below this line touches the API server for a decision input.
	StepResolveScope Step = 3
	// StepClassify is step 4: the 06 §4.2 evaluation order. Forbidden rejects.
	StepClassify Step = 4
	// StepBrake is step 5: the 06 §4.4 consultation at StageGate.
	StepBrake Step = 5
	// StepUndoPlan is step 6: the plan is attached to the record and the class is re-checked
	// against it.
	StepUndoPlan Step = 6
	// StepGate is step 7: a gated action parks as PendingApproval and nothing below runs.
	StepGate Step = 7
	// StepSnapshot is step 8: the pre-state is persisted into the ActionRecord.
	StepSnapshot Step = 8
	// StepExecute is step 9: server-side apply with the actor identity.
	StepExecute Step = 9
	// StepVerify is step 10: the 04 §5 verification and recovery ladder.
	StepVerify Step = 10
	// StepJournal is step 11: the terminal record write.
	StepJournal Step = 11
)

// FirstStep and LastStep bound the sequence. Used by the fault-injection check so that adding a
// step to the pipeline extends the check's coverage instead of leaving the new step untested --
// a hardcoded 1..10 in the test would be the headcount LSN-036 is about.
const (
	FirstStep = StepAuthenticate
	LastStep  = StepJournal
)

var stepNames = map[Step]string{
	StepAuthenticate: "authenticate",
	StepValidate:     "validate",
	StepResolveScope: "resolve-scope",
	StepClassify:     "classify",
	StepBrake:        "brake",
	StepUndoPlan:     "undo-plan",
	StepGate:         "gate",
	StepSnapshot:     "snapshot",
	StepExecute:      "execute",
	StepVerify:       "verify",
	StepJournal:      "journal",
}

// String names the step for a trace rendering and a test failure.
func (s Step) String() string {
	if n, ok := stepNames[s]; ok {
		return fmt.Sprintf("%d/%s", int(s), n)
	}
	return fmt.Sprintf("%d/unknown", int(s))
}

// StepStatus is how a step ended. There are four and they are not interchangeable: an operator
// reading a trace needs "the control said no" to look different from "the step could not run",
// because the first is the system working and the second is an incident.
type StepStatus string

const (
	// StepCompleted: the step did its work and the pipeline continued.
	StepCompleted StepStatus = "completed"
	// StepRefused: the step's own control stopped the action. A policy outcome, not a fault.
	StepRefused StepStatus = "refused"
	// StepFailed: the step could not run. A fault.
	StepFailed StepStatus = "failed"
	// StepSkipped: the step is not applicable to this action, with a reason. A dry-run's step 10
	// is the only case in the current pipeline. Recorded rather than omitted, because an omitted
	// step and a dropped step are indistinguishable in a trace and only one of them is fine.
	StepSkipped StepStatus = "skipped"
)

// StepEvent is one step's entry in the trace. Exactly one per step that was reached.
type StepEvent struct {
	Step   Step
	Status StepStatus
	// Detail is the human-facing why. On a refusal it is the refusal reason; on a completion it is
	// what the step decided, when the step decided anything worth carrying.
	Detail string
}

// String renders one event.
func (e StepEvent) String() string {
	if e.Detail == "" {
		return fmt.Sprintf("%s=%s", e.Step, e.Status)
	}
	return fmt.Sprintf("%s=%s(%s)", e.Step, e.Status, e.Detail)
}

// StepTrace is the ordered record of which steps ran, and the thing that enforces the order.
//
// The zero value is ready to use. It is safe for concurrent readers and writers because the
// handler that starts the trace and the pipeline that continues it are the same goroutine today
// and there is no reason for a future recovery path to have to know that.
type StepTrace struct {
	mu     sync.Mutex
	events []StepEvent
	// sealed is set by the first non-continuing status. Nothing may be recorded after it, which is
	// what makes "the trace shows steps 1...k and nothing after" a property of the type rather
	// than a discipline the pipeline has to keep.
	sealed bool
}

// OutOfOrderError is what Run and Skip return when a step is attempted out of sequence.
//
// A distinct type, because a caller must never be able to fold this into "the step failed". It is
// not a step failing: it is the pipeline no longer being the pipeline, and the only correct
// response is to abort the action and report a broker bug.
type OutOfOrderError struct {
	Attempted Step
	Expected  Step
	Sealed    bool
}

func (e *OutOfOrderError) Error() string {
	if e.Sealed {
		return fmt.Sprintf(
			"broker pipeline: step %s was attempted after the trace was sealed at %s; a step that "+
				"runs past a refusal or a fault is a step running on a decision that was already no",
			e.Attempted, e.Expected)
	}
	return fmt.Sprintf(
		"broker pipeline: step %s was attempted where step %s was due; 03 §4.1 is a sequence and "+
			"six of its steps are not skippable by any caller",
		e.Attempted, e.Expected)
}

// next reports the only step that may be recorded now, and whether the trace is closed.
// Caller holds the lock.
func (t *StepTrace) next() Step {
	if len(t.events) == 0 {
		return FirstStep
	}
	return t.events[len(t.events)-1].Step + 1
}

// admit checks the ordering and returns the error to fail with, or nil. Caller holds the lock.
func (t *StepTrace) admit(s Step) error {
	if t.sealed {
		return &OutOfOrderError{Attempted: s, Expected: t.events[len(t.events)-1].Step, Sealed: true}
	}
	if want := t.next(); s != want {
		return &OutOfOrderError{Attempted: s, Expected: want}
	}
	return nil
}

// Run executes one step of the pipeline and records exactly one event for it.
//
// fn returns the detail to record and its error. A nil error records StepCompleted and the pipeline
// may proceed to s+1. A *Refusal records StepRefused; anything else records StepFailed. Either way
// the trace is SEALED: the error is returned to the caller and no further step can be recorded, so
// a caller that forgets to check the error still cannot execute the next step.
func (t *StepTrace) Run(s Step, fn func() (string, error)) error {
	t.mu.Lock()
	if err := t.admit(s); err != nil {
		t.mu.Unlock()
		return err
	}
	t.mu.Unlock()

	// fn runs unlocked. A step that took the trace's lock for its whole duration would serialize
	// nothing useful and would deadlock the moment a step wanted to read its own trace.
	detail, err := fn()

	t.mu.Lock()
	defer t.mu.Unlock()
	switch {
	case err == nil:
		t.events = append(t.events, StepEvent{Step: s, Status: StepCompleted, Detail: detail})
	default:
		status := StepFailed
		if ref, ok := err.(*Refusal); ok {
			status = StepRefused
			if detail == "" {
				detail = ref.Reason
			}
		}
		t.events = append(t.events, StepEvent{Step: s, Status: status, Detail: detail})
		t.sealed = true
	}
	return err
}

// Skip records a step that does not apply to this action and lets the pipeline continue.
//
// It exists so that "not applicable" is written down. Without it the only way past an inapplicable
// step is to record it as completed -- which is a lie a trace reader cannot detect -- or to jump
// over it, which Run refuses. A skip needs a reason and there is no overload without one.
func (t *StepTrace) Skip(s Step, reason string) error {
	if strings.TrimSpace(reason) == "" {
		return fmt.Errorf("broker pipeline: step %s was skipped with no reason; an unexplained skip "+
			"is indistinguishable from a dropped step", s)
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	if err := t.admit(s); err != nil {
		return err
	}
	t.events = append(t.events, StepEvent{Step: s, Status: StepSkipped, Detail: reason})
	return nil
}

// Stop seals the trace on a step that completed but after which the pipeline deliberately does not
// continue -- a gated action parking at step 7 is the case. The step keeps its completed status;
// what changes is that nothing may follow it.
//
// Without this, parking would leave an unsealed trace that a later edit could append step 8 to.
func (t *StepTrace) Stop() {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.sealed = true
}

// Events returns a copy of the trace in order.
func (t *StepTrace) Events() []StepEvent {
	t.mu.Lock()
	defer t.mu.Unlock()
	out := make([]StepEvent, len(t.events))
	copy(out, t.events)
	return out
}

// Reached is the highest step that has an event, or 0 for an empty trace.
func (t *StepTrace) Reached() Step {
	t.mu.Lock()
	defer t.mu.Unlock()
	if len(t.events) == 0 {
		return 0
	}
	return t.events[len(t.events)-1].Step
}

// Ran reports whether a step has an event whose status is StepCompleted. A refused, failed or
// skipped step did not run, and this is the predicate V-BRK-011 asks with.
func (t *StepTrace) Ran(s Step) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	for _, e := range t.events {
		if e.Step == s {
			return e.Status == StepCompleted
		}
	}
	return false
}

// String renders the whole trace on one line, for a log field and for a test failure message.
func (t *StepTrace) String() string {
	var b strings.Builder
	for i, e := range t.Events() {
		if i > 0 {
			b.WriteString(" -> ")
		}
		b.WriteString(e.String())
	}
	if b.Len() == 0 {
		return "(no step reached)"
	}
	return b.String()
}
