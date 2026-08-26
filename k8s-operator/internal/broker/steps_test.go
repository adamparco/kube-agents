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
	"errors"
	"net/http"
	"strings"
	"sync"
	"testing"

	"github.com/go-logr/logr"
	"github.com/go-logr/logr/funcr"
)

// scaleFixture is a valid envelope that every case here starts from, so a fault is the ONLY
// difference between a submission that stops at step k and one that does not.
const scaleFixture = "platform.scale-deployment.json"

// ok is a step body that succeeds with no detail.
func ok() (string, error) { return "", nil }

func runAll(t *testing.T, tr *StepTrace, steps ...Step) {
	t.Helper()
	for _, s := range steps {
		if err := tr.Run(s, ok); err != nil {
			t.Fatalf("Run(%s): unexpected error: %v", s, err)
		}
	}
}

// TestStepTraceAdmitsOnlyTheNextStep is the property the whole trace rests on. Every other check in
// this file and in the pipeline package assumes it, so it is asserted directly rather than inferred.
func TestStepTraceAdmitsOnlyTheNextStep(t *testing.T) {
	t.Run("the full sequence runs", func(t *testing.T) {
		tr := &StepTrace{}
		for s := FirstStep; s <= LastStep; s++ {
			if err := tr.Run(s, ok); err != nil {
				t.Fatalf("Run(%s): %v", s, err)
			}
		}
		if got := tr.Reached(); got != LastStep {
			t.Fatalf("Reached() = %s, want %s", got, LastStep)
		}
		if n := len(tr.Events()); n != int(LastStep) {
			t.Fatalf("recorded %d events for %d steps", n, int(LastStep))
		}
	})

	t.Run("a skipped step is refused", func(t *testing.T) {
		tr := &StepTrace{}
		runAll(t, tr, StepAuthenticate, StepValidate, StepResolveScope)

		// Step 4 is due. Attempting 5 is the "the classifier is expensive, and this action is
		// obviously routine" shortcut, which is the exact shape 03 §4.1's non-skippable list
		// forbids.
		err := tr.Run(StepBrake, ok)
		var oo *OutOfOrderError
		if !errors.As(err, &oo) {
			t.Fatalf("Run(5) after step 3 returned %v, want *OutOfOrderError", err)
		}
		if oo.Attempted != StepBrake || oo.Expected != StepClassify {
			t.Fatalf("OutOfOrderError = %+v, want attempted 5 expected 4", oo)
		}
		if tr.Reached() != StepResolveScope {
			t.Fatalf("a refused step was recorded anyway: %s", tr)
		}
	})

	t.Run("a repeated step is refused", func(t *testing.T) {
		tr := &StepTrace{}
		runAll(t, tr, StepAuthenticate, StepValidate)
		if err := tr.Run(StepValidate, ok); err == nil {
			t.Fatal("re-running step 2 was admitted; a step that can run twice can be run once to " +
				"pass and once to matter")
		}
	})

	t.Run("a step body does not run when the step is out of order", func(t *testing.T) {
		// The ordering check has to happen BEFORE the work, not after it. A trace that refused to
		// record step 9 but ran the apply first would be a correct-looking trace over a mutated
		// cluster -- which is worse than no trace.
		tr := &StepTrace{}
		ran := false
		_ = tr.Run(StepExecute, func() (string, error) { ran = true; return "", nil })
		if ran {
			t.Fatal("the body of an out-of-order step was executed")
		}
	})
}

func TestStepTraceSealsOnTheFirstFailure(t *testing.T) {
	cases := []struct {
		name   string
		err    error
		status StepStatus
	}{
		{"a refusal is a policy outcome", &Refusal{Status: http.StatusForbidden, Reason: "forbidden"}, StepRefused},
		{"anything else is a fault", errors.New("the API server is unreachable"), StepFailed},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			tr := &StepTrace{}
			runAll(t, tr, StepAuthenticate, StepValidate, StepResolveScope)

			if err := tr.Run(StepClassify, func() (string, error) { return "", tc.err }); err == nil {
				t.Fatal("Run returned nil for a failing step body")
			}
			events := tr.Events()
			last := events[len(events)-1]
			if last.Step != StepClassify || last.Status != tc.status {
				t.Fatalf("last event = %s, want 4/classify=%s", last, tc.status)
			}
			if tr.Ran(StepClassify) {
				t.Fatal("Ran reported a failed step as having run")
			}

			// And nothing after. This is V-BRK-014's "steps 1...k and nothing after", enforced by
			// the type rather than asserted about a particular pipeline.
			if err := tr.Run(StepBrake, ok); err == nil {
				t.Fatalf("step 5 ran after step 4 failed: %s", tr)
			}
			if tr.Reached() != StepClassify {
				t.Fatalf("trace continued past the failure: %s", tr)
			}
		})
	}
}

func TestStepTraceRefusalDetailDefaultsToTheReason(t *testing.T) {
	tr := &StepTrace{}
	runAll(t, tr, StepAuthenticate)
	_ = tr.Run(StepValidate, func() (string, error) {
		return "", &Refusal{Status: http.StatusBadRequest, Reason: ReasonReplayedEnvelope}
	})
	if got := tr.String(); !strings.Contains(got, ReasonReplayedEnvelope) {
		t.Fatalf("the rendered trace %q does not name the reason it stopped for", got)
	}
}

func TestStepTraceSkip(t *testing.T) {
	t.Run("a reasonless skip is refused", func(t *testing.T) {
		tr := &StepTrace{}
		runAll(t, tr, StepAuthenticate)
		if err := tr.Skip(StepValidate, "   "); err == nil {
			t.Fatal("a skip with a whitespace reason was accepted; an unexplained skip and a " +
				"dropped step are the same thing to a reader")
		}
		if tr.Reached() != StepAuthenticate {
			t.Fatalf("the refused skip was recorded: %s", tr)
		}
	})

	t.Run("a skip advances but does not count as having run", func(t *testing.T) {
		tr := &StepTrace{}
		runAll(t, tr, StepAuthenticate)
		if err := tr.Skip(StepValidate, "not applicable"); err != nil {
			t.Fatalf("Skip: %v", err)
		}
		if tr.Ran(StepValidate) {
			t.Fatal("Ran reported a skipped step as having run")
		}
		runAll(t, tr, StepResolveScope)
	})

	t.Run("a skip obeys the ordering", func(t *testing.T) {
		tr := &StepTrace{}
		if err := tr.Skip(StepExecute, "because"); err == nil {
			t.Fatal("step 9 was skipped from an empty trace; skipping is not a way around the order")
		}
	})
}

func TestStepTraceStopSealsACompletedStep(t *testing.T) {
	tr := &StepTrace{}
	for s := FirstStep; s <= StepGate; s++ {
		runAll(t, tr, s)
	}
	tr.Stop()

	if !tr.Ran(StepGate) {
		t.Fatal("Stop changed the status of the step it sealed after")
	}
	if err := tr.Run(StepSnapshot, ok); err == nil {
		t.Fatalf("a parked action continued to step 8: %s", tr)
	}
}

func TestStepTraceIsConcurrencySafe(t *testing.T) {
	// Not a property of the pipeline -- it is single-goroutine today -- but of the type, so that a
	// future recovery path reading the trace from another goroutine does not have to know that.
	tr := &StepTrace{}
	runAll(t, tr, StepAuthenticate)
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); _ = tr.Run(StepValidate, ok); _ = tr.String(); _ = tr.Events() }()
	}
	wg.Wait()
	if n := len(tr.Events()); n != 2 {
		t.Fatalf("%d events after 8 concurrent attempts at one step; want 2", n)
	}
}

func TestEveryStepHasAName(t *testing.T) {
	// LSN-036: derived from the bounds, so a twelfth step added to the pipeline fails here rather
	// than rendering as "12/unknown" in every trace nobody reads closely.
	for s := FirstStep; s <= LastStep; s++ {
		if strings.Contains(s.String(), "unknown") {
			t.Errorf("step %d has no name", int(s))
		}
	}
	if got := Step(99).String(); !strings.Contains(got, "unknown") {
		t.Fatalf("Step(99) rendered as %q; an out-of-enum step must render visibly", got)
	}
}

// --- the handler's half of V-BRK-014 --------------------------------------------------------
//
// Steps 1 and 2 run in handleActions, before any Pipeline exists. Fault-injecting them therefore
// belongs here rather than in the pipeline package's test, which cannot reach them. Steps 3-10 are
// covered by internal/broker/pipeline.

// captureTraces returns a logger that collects every trace line the handler emits.
func captureTraces(lines *[]string, mu *sync.Mutex) logr.Logger {
	return funcr.New(func(_, args string) {
		if !strings.Contains(args, `"steps"=`) {
			return
		}
		mu.Lock()
		defer mu.Unlock()
		*lines = append(*lines, args)
	}, funcr.Options{Verbosity: 1})
}

func TestHandlerTraceStopsAtTheFaultedStep(t *testing.T) {
	cases := []struct {
		name string
		// fault makes the harness fail at the named step.
		fault func(*harness, *testing.T) []byte
		// wantReached is the last step that may appear in the trace.
		wantReached Step
		wantStatus  StepStatus
	}{
		{
			name: "step 1: the token is not the caller this broker serves",
			fault: func(h *harness, t *testing.T) []byte {
				body := h.submittable(t, scaleFixture)
				h.reviewer.status = authenticatedAs("system:serviceaccount:other:someone-else")
				return body
			},
			wantReached: StepAuthenticate,
			wantStatus:  StepRefused,
		},
		{
			name: "step 2: the envelope is not an envelope",
			fault: func(h *harness, _ *testing.T) []byte {
				return []byte(`{"apiVersion":"kubeagents.x-k8s.io/v1alpha1","kind":"NotAnEnvelope"}`)
			},
			wantReached: StepValidate,
			wantStatus:  StepRefused,
		},
		{
			name: "step 2: the nonce has already been spent",
			fault: func(h *harness, t *testing.T) []byte {
				body := h.submittable(t, scaleFixture)
				h.post(t, body) // spends the nonce
				h.pipeline.calls = 0
				return body
			},
			wantReached: StepValidate,
			wantStatus:  StepRefused,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var (
				mu    sync.Mutex
				lines []string
			)
			h := newHarnessWithLog(t, captureTraces(&lines, &mu))
			body := tc.fault(h, t)

			mu.Lock()
			lines = nil
			mu.Unlock()

			rec := h.post(t, body)
			if rec.Code < 400 {
				t.Fatalf("the faulted submission returned %d; the fault was not injected", rec.Code)
			}

			mu.Lock()
			got := append([]string(nil), lines...)
			mu.Unlock()
			if len(got) != 1 {
				t.Fatalf("want exactly one trace line per submission, got %d: %v", len(got), got)
			}
			line := got[0]

			if !strings.Contains(line, tc.wantReached.String()+"="+string(tc.wantStatus)) {
				t.Errorf("trace %s does not show %s=%s", line, tc.wantReached, tc.wantStatus)
			}
			// Nothing after k. Checked by name for every later step, so a new step inserted into
			// the pipeline is covered without editing this list (LSN-036).
			for s := tc.wantReached + 1; s <= LastStep; s++ {
				if strings.Contains(line, s.String()) {
					t.Errorf("trace %s continued to %s after the fault at %s", line, s, tc.wantReached)
				}
			}
			// And no mutation: the pipeline is the only thing that can mutate, and it was never
			// reached. This is the `¬` half of V-BRK-014.
			if h.pipeline.calls != 0 {
				t.Errorf("the pipeline was invoked %d times after a fault at %s", h.pipeline.calls, tc.wantReached)
			}
		})
	}
}

func TestHandlerHandsTheTraceToThePipelineAtStepTwo(t *testing.T) {
	// The positive control for the three negatives above: without it they would all still pass on
	// a handler that refused everything (09 §6, V-MET-014).
	h := newHarness(t)
	rec := h.post(t, h.submittable(t, scaleFixture))
	if rec.Code >= 400 {
		t.Fatalf("a valid envelope was refused with %d: %s", rec.Code, rec.Body.String())
	}
	if h.pipeline.calls != 1 {
		t.Fatalf("the pipeline was called %d times", h.pipeline.calls)
	}
	tr := h.pipeline.lastTr
	if tr == nil {
		t.Fatal("the pipeline was handed no trace; steps 1-2 and steps 3-11 would be two " +
			"separate accounts of one action")
	}
	if !tr.Ran(StepAuthenticate) || !tr.Ran(StepValidate) {
		t.Fatalf("the handler's own steps are missing from the trace it handed over: %s", tr)
	}
	if tr.Reached() != StepValidate {
		t.Fatalf("the handler recorded past step 2: %s", tr)
	}
	// And the pipeline can continue it -- the seam actually joins.
	if err := tr.Run(StepResolveScope, ok); err != nil {
		t.Fatalf("the pipeline could not continue the handler's trace: %v", err)
	}
}
