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

package verify

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"testing"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime/schema"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
)

func notFound(kind, name string) error {
	return apierrors.NewNotFound(schema.GroupResource{Resource: kind}, name)
}

// --- the delete row -----------------------------------------------------------------------------

// TestAbsencePredicate covers the row 04 §5.1 has no line for and every other row gets backwards: a
// delete succeeds by the object NOT being there.
func TestAbsencePredicate(t *testing.T) {
	target := Target{Ref: ref("apps", "Deployment", "web"), ExpectAbsent: true}

	t.Run("gone is satisfied", func(t *testing.T) {
		ev := absencePredicate(context.Background(),
			&fakeProber{getErr: notFound("deployments", "web")}, target)
		if ev.Verdict != VerdictSatisfied {
			t.Fatalf("verdict = %s (%s), want Satisfied", ev.Verdict, ev.Detail)
		}
	})

	t.Run("still present is pending, not failed", func(t *testing.T) {
		// Pending is the load-bearing half. Deletion is asynchronous, so a first read that still
		// sees the object is normal; only the settle window expiring makes it a failure. Failing
		// here would report every graceful delete as failed and roll it back by recreating.
		live := obj("apps", "Deployment", "web", nil, nil)
		live.SetFinalizers([]string{"foregroundDeletion"})
		ev := absencePredicate(context.Background(), &fakeProber{obj: live}, target)
		if ev.Verdict != VerdictPending {
			t.Fatalf("verdict = %s (%s), want Pending", ev.Verdict, ev.Detail)
		}
		if ev.Cause != CauseDependencyConverging {
			t.Errorf("cause = %q, want %q", ev.Cause, CauseDependencyConverging)
		}
		if !strings.Contains(ev.Detail, "foregroundDeletion") {
			t.Errorf("detail %q names no finalizer, so an operator reading the record cannot tell "+
				"a stuck delete from a slow one", ev.Detail)
		}
	})

	t.Run("replaced at the same name is satisfied", func(t *testing.T) {
		// The target is gone; something else holds its name. For every other row that is evidence
		// about a stranger and must not be evaluated. For this one it is the answer.
		err := fmt.Errorf("web exists but is uid b, not the uid a this action targeted: %w", ErrTargetReplaced)
		ev := absencePredicate(context.Background(), &fakeProber{getErr: err}, target)
		if ev.Verdict != VerdictSatisfied {
			t.Fatalf("verdict = %s (%s), want Satisfied", ev.Verdict, ev.Detail)
		}
	})

	t.Run("an unreadable cluster is not an absence", func(t *testing.T) {
		ev := absencePredicate(context.Background(),
			&fakeProber{getErr: errors.New("connection refused")}, target)
		if ev.Verdict == VerdictSatisfied {
			t.Fatalf("verdict = Satisfied on a failed read: 'I could not look' was reported as "+
				"'it is gone' (%s)", ev.Detail)
		}
	})
}

// TestPredicateForPrefersAbsenceOverTheKindRow is the wiring half. A Deployment has a perfectly good
// row of its own, and applying it to a deleted object reads NotFound as VerdictFailed -- so the
// choice has to come from the action, not from the kind.
func TestPredicateForPrefersAbsenceOverTheKindRow(t *testing.T) {
	deployment := ref("apps", "Deployment", "web")

	if same(predicateFor(Target{Ref: deployment, ExpectAbsent: true}), workloadPredicate) {
		t.Error("an ExpectAbsent Deployment still resolves to workloadPredicate")
	}
	if !same(predicateFor(Target{Ref: deployment, ExpectAbsent: true}), absencePredicate) {
		t.Error("an ExpectAbsent Deployment does not resolve to absencePredicate")
	}
	if !same(predicateFor(Target{Ref: deployment}), workloadPredicate) {
		t.Error("a Deployment that was not deleted no longer resolves to its own row")
	}

	// And the consequence, end to end through the driver's chooser: the same target, the same
	// prober, opposite verdicts.
	p := &fakeProber{getErr: notFound("deployments", "web")}
	gone := predicateFor(Target{Ref: deployment, ExpectAbsent: true})(context.Background(), p, Target{Ref: deployment, ExpectAbsent: true})
	kept := predicateFor(Target{Ref: deployment})(context.Background(), p, Target{Ref: deployment})
	if gone.Verdict != VerdictSatisfied || kept.Verdict != VerdictFailed {
		t.Errorf("absent object: delete verdict %s, non-delete verdict %s; want Satisfied and Failed",
			gone.Verdict, kept.Verdict)
	}
}

// --- restart baselines --------------------------------------------------------------------------

// TestRestartBaselineKindsAreExactlyTheWorkloadRows is the join [[LSN-040]] asks for whenever a
// second list of kinds appears: restartBaselineKinds and predicateByKind are two tables keyed the
// same way, and nothing else compares them. A kind added to the predicate table as a workload
// without a baseline row verifies as Indeterminate forever; a kind here that is not a workload
// costs a live read per action for a number no predicate uses.
func TestRestartBaselineKindsAreExactlyTheWorkloadRows(t *testing.T) {
	for kind, pred := range predicateByKind {
		wantsBaseline := same(pred, workloadPredicate)
		if got := restartBaselineKinds[kind]; got != wantsBaseline {
			t.Errorf("%s/%s: predicate is workloadPredicate=%v but restartBaselineKinds says %v",
				kind.Group, kind.Kind, wantsBaseline, got)
		}
	}
	for kind := range restartBaselineKinds {
		if _, ok := predicateByKind[kind]; !ok {
			t.Errorf("%s/%s needs a restart baseline but has no row in predicateByKind at all",
				kind.Group, kind.Kind)
		}
	}
}

func TestCaptureRestartBaselines(t *testing.T) {
	deployment := ref("apps", "Deployment", "web")
	configMap := ref("", "ConfigMap", "settings")

	t.Run("only the kinds whose row needs one", func(t *testing.T) {
		p := &fakeProber{restarts: i64(4)}
		got, err := CaptureRestartBaselines(context.Background(), p,
			[]agentv1alpha1.TargetRef{configMap, deployment})
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if got[0] != nil {
			t.Errorf("ConfigMap baselined at %d; its row never compares restart counts", *got[0])
		}
		if got[1] == nil || *got[1] != 4 {
			t.Errorf("Deployment baseline = %v, want 4", got[1])
		}
	})

	t.Run("a target that does not exist yet baselines at zero", func(t *testing.T) {
		// A create's object had no pods before the action, so every restart the settle window
		// observes is a new one. nil here would make workloadPredicate Indeterminate, which the
		// driver turns into a rollback -- undoing every Deployment create that worked.
		p := &fakeProber{restErr: notFound("deployments", "web")}
		got, err := CaptureRestartBaselines(context.Background(), p, []agentv1alpha1.TargetRef{deployment})
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if got[0] == nil || *got[0] != 0 {
			t.Fatalf("baseline = %v, want a pointer to 0", got[0])
		}
	})

	t.Run("an unreadable baseline refuses before the action", func(t *testing.T) {
		// Not nil-and-continue. An action executed with no baseline cannot be verified, and 04 §5.1
		// turns "could not be verified" into a rollback -- so degrading here lands the write and
		// then undoes it. Refusing at step 3 costs the caller a retry instead.
		p := &fakeProber{restErr: errors.New("connection refused")}
		if _, err := CaptureRestartBaselines(context.Background(), p,
			[]agentv1alpha1.TargetRef{deployment}); err == nil {
			t.Fatal("a failed baseline read was accepted")
		}
	})

	t.Run("no prober at all refuses", func(t *testing.T) {
		if _, err := CaptureRestartBaselines(context.Background(), nil,
			[]agentv1alpha1.TargetRef{deployment}); err == nil {
			t.Fatal("a nil prober was accepted for a kind that needs a baseline")
		}
		// ... but costs nothing for a kind that does not need one.
		if _, err := CaptureRestartBaselines(context.Background(), nil,
			[]agentv1alpha1.TargetRef{configMap}); err != nil {
			t.Fatalf("a nil prober refused a ConfigMap, whose row asks it nothing: %v", err)
		}
	})
}

// same compares two Predicates by function identity. Go will not compare funcs with ==, and the
// alternative -- a third list naming which kinds are workloads -- is the thing these tests exist to
// stop.
func same(a, b Predicate) bool {
	return reflect.ValueOf(a).Pointer() == reflect.ValueOf(b).Pointer()
}
