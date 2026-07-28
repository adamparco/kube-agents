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
	"strings"
	"testing"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// flippingProber answers with the fake's object until `after` Gets have been served, then with
// `then`. It is how a test spends a settle window without spending wall time.
type flippingProber struct {
	*fakeProber
	then  *unstructured.Unstructured
	after int
	calls int
}

func (f *flippingProber) Get(context.Context, agentv1alpha1.TargetRef) (*unstructured.Unstructured, error) {
	f.calls++
	if f.calls > f.after {
		return f.then, nil
	}
	return f.fakeProber.obj, nil
}

type fakeRollbacker struct {
	calls int
	err   error
	plans []agentv1alpha1.UndoPlan
}

func (f *fakeRollbacker) Rollback(_ context.Context, _ string, plan agentv1alpha1.UndoPlan) error {
	f.calls++
	f.plans = append(f.plans, plan)
	return f.err
}

type fakePager struct {
	calls int
	err   error
	last  PageRequest
}

func (f *fakePager) Page(_ context.Context, p PageRequest) error {
	f.calls++
	f.last = p
	return f.err
}

type fakePauser struct {
	calls  int
	err    error
	agent  string
	reason string
}

func (f *fakePauser) Pause(_ context.Context, agent, reason string) error {
	f.calls++
	f.agent, f.reason = agent, reason
	return f.err
}

// clock advances by the poll interval on every Sleep, so a settle window is spent in nanoseconds of
// wall time and the number of polls is exactly determined.
type clock struct {
	now    time.Time
	sleeps int
}

func (c *clock) Now() time.Time { return c.now }
func (c *clock) Sleep(_ context.Context, d time.Duration) error {
	c.sleeps++
	c.now = c.now.Add(d)
	return nil
}

func replayablePlan() agentv1alpha1.UndoPlan {
	return agentv1alpha1.UndoPlan{
		Strategy:  agentv1alpha1.UndoRestore,
		Validated: true,
		Steps: []agentv1alpha1.UndoStep{{
			Op:     "apply",
			Target: ref("apps", "Deployment", "web"),
			Object: &runtime.RawExtension{Raw: []byte(`{"apiVersion":"apps/v1","kind":"Deployment"}`)},
		}},
	}
}

func deployTarget(observed, available int64) Target {
	return Target{Ref: ref("apps", "Deployment", "web"), BaselineRestarts: i64(0)}
}

func deployObj(observed, available int64) *fakeProber {
	return &fakeProber{
		obj: obj("apps", "Deployment", "web", map[string]any{"replicas": int64(3)},
			map[string]any{"observedGeneration": observed, "availableReplicas": available}),
		restarts: i64(0),
	}
}

func newDriver(p Prober, c *clock) *Driver {
	return &Driver{
		Prober:       p,
		Now:          c.Now,
		Sleep:        c.Sleep,
		PollInterval: 10 * time.Second,
	}
}

func TestDriverVerifies(t *testing.T) {
	c := &clock{now: base}
	rb := &fakeRollbacker{}
	d := newDriver(deployObj(7, 3), c)
	d.Rollback = rb

	res, err := d.Run(context.Background(), Request{
		ActionID: "a-1", AgentIdentity: "platform/prod",
		Targets: []Target{deployTarget(7, 3)}, UndoPlan: replayablePlan(),
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if res.Decision != DecisionVerified {
		t.Errorf("decision = %s, want Verified", res.Decision)
	}
	if res.Phase != agentv1alpha1.PhaseVerified {
		t.Errorf("phase = %s, want Verified", res.Phase)
	}
	if !res.Verification.Passed {
		t.Error("verification.passed is false on a Verified decision")
	}
	if len(res.Verification.Checks) != 1 || !res.Verification.Checks[0].Passed {
		t.Errorf("checks = %+v", res.Verification.Checks)
	}
	if res.Recovery.Rung != RungNone || len(res.Recovery.Transitions) != 0 {
		t.Errorf("a verified action climbed the ladder: %+v", res.Recovery)
	}
	if rb.calls != 0 {
		t.Errorf("a verified action was rolled back %d times", rb.calls)
	}
	if res.Verification.CompletedAt == nil {
		t.Error("verification has no completedAt")
	}
}

func TestDriverRefusesToRunWithoutAProber(t *testing.T) {
	// A driver that cannot look at the cluster would report every action as verified. That is the
	// whole product failing open, so it is an error at the door rather than a nil-check inside.
	d := &Driver{}
	if _, err := d.Run(context.Background(), Request{ActionID: "a-1"}); err == nil {
		t.Fatal("a Driver with no Prober ran")
	}
}

// TestDriverKeepsLookingInsideTheWindow: a target that has not converged yet is polled, not judged.
// A driver that returned on the first Pending would roll back every rollout that took longer than
// one API round-trip.
func TestDriverKeepsLookingInsideTheWindow(t *testing.T) {
	c := &clock{now: base}
	rb := &fakeRollbacker{}
	target := Target{Ref: ref("nobody.example.com", "Widget", "w1")}

	notReady := obj("nobody.example.com", "Widget", "w1", nil,
		map[string]any{"conditions": []any{map[string]any{"type": "Ready", "status": "False"}}})
	ready := obj("nobody.example.com", "Widget", "w1", nil,
		map[string]any{"conditions": []any{map[string]any{"type": "Ready", "status": "True"}}})

	d := newDriver(&flippingProber{fakeProber: &fakeProber{obj: notReady}, after: 2, then: ready}, c)
	d.Rollback = rb

	res, err := d.Run(context.Background(), Request{
		ActionID: "a-2", Targets: []Target{target}, UndoPlan: replayablePlan(),
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if res.Decision != DecisionVerified {
		t.Fatalf("decision = %s, want Verified once the target converged after %d polls",
			res.Decision, c.sleeps)
	}
	if c.sleeps != 2 {
		t.Errorf("the driver polled %d times, want 2", c.sleeps)
	}
	if rb.calls != 0 {
		t.Errorf("a target that converged inside its window was rolled back %d times", rb.calls)
	}
}

// TestDriverRetriesOnATransientExecutionFailure is rung 1. It is reached only through
// Request.ExecutionFailure, because 04 §5 rung 1 retries the ACTION -- polling a settle window is
// verification catching up with a write that did land, and is not a rung.
func TestDriverRetriesOnATransientExecutionFailure(t *testing.T) {
	c := &clock{now: base}
	rb := &fakeRollbacker{}
	p := deployObj(7, 3)
	d := newDriver(p, c)
	d.Rollback, d.Cooldown = rb, NewMemoryCooldown()

	conflict := apierrors.NewConflict(
		schema.GroupResource{Group: "apps", Resource: "deployments"}, "web", errors.New("modified"))
	res, err := d.Run(context.Background(), Request{
		ActionID: "a-14", AgentIdentity: "platform/prod",
		Targets: []Target{deployTarget(7, 3)}, UndoPlan: replayablePlan(),
		ExecutionFailure: &Failure{Err: conflict},
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if res.Decision != DecisionRetry {
		t.Fatalf("decision = %s, want Retry (cause %s)", res.Decision, res.Cause)
	}
	if res.Phase != agentv1alpha1.PhaseExecuting {
		t.Errorf("phase = %s, want Executing", res.Phase)
	}
	if res.Cause != CauseConflict {
		t.Errorf("cause = %s, want Conflict", res.Cause)
	}
	if res.Recovery.Rung != RungRetry {
		t.Errorf("rung = %d, want 1", res.Recovery.Rung)
	}
	if err := ValidateRecovery(&res.Recovery); err != nil {
		t.Errorf("the driver produced a recovery that does not validate: %v", err)
	}
	// A retry is not a rollback, so nothing was undone and no cooldown was entered.
	if rb.calls != 0 {
		t.Errorf("a transient failure was rolled back %d times", rb.calls)
	}
	if !res.CooldownUntil.IsZero() {
		t.Error("a retryable failure put the target into cooldown; the next attempt is now refused")
	}
	if p.getCalls != 0 {
		t.Errorf("the driver verified %d times against a write that never landed", p.getCalls)
	}
	if res.Verification.Passed {
		t.Error("verification.passed is true on a failed write")
	}
}

func TestDriverRollsBackATerminalExecutionFailure(t *testing.T) {
	c := &clock{now: base}
	rb := &fakeRollbacker{}
	d := newDriver(deployObj(7, 3), c)
	d.Rollback, d.Cooldown = rb, NewMemoryCooldown()

	denied := apierrors.NewForbidden(
		schema.GroupResource{Group: "apps", Resource: "deployments"}, "web",
		errors.New(`admission webhook "policy.example.com" denied the request`))
	res, err := d.Run(context.Background(), Request{
		ActionID: "a-15", AgentIdentity: "platform/prod",
		Targets: []Target{deployTarget(7, 3)}, UndoPlan: replayablePlan(),
		ExecutionFailure: &Failure{Err: denied},
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if res.Decision != DecisionRolledBack {
		t.Fatalf("decision = %s, want RolledBack", res.Decision)
	}
	if res.Cause != CauseAdmissionDenied {
		t.Errorf("cause = %s, want AdmissionDenied", res.Cause)
	}
	if rb.calls != 1 {
		t.Errorf("rollback attempted %d times, want 1", rb.calls)
	}
}

func TestDriverRollsBackOnATerminalFailure(t *testing.T) {
	c := &clock{now: base}
	rb := &fakeRollbacker{}
	cd := NewMemoryCooldown()

	// A nonexistent image is terminal on the first look: no waiting, no window spent.
	p := deployObj(7, 1)
	p.obj = obj("apps", "Deployment", "web", map[string]any{"replicas": int64(3)},
		map[string]any{
			"observedGeneration": int64(7), "availableReplicas": int64(0),
			"conditions": []any{map[string]any{
				"type": "ReplicaFailure", "status": "True",
				"message": `admission webhook "policy.example.com" denied the request`,
			}},
		})

	d := newDriver(p, c)
	d.Rollback, d.Cooldown = rb, cd

	res, err := d.Run(context.Background(), Request{
		ActionID: "a-3", AgentIdentity: "platform/prod",
		Targets: []Target{deployTarget(7, 0)}, UndoPlan: replayablePlan(),
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if res.Decision != DecisionRolledBack {
		t.Fatalf("decision = %s, want RolledBack (cause %s)", res.Decision, res.Cause)
	}
	if res.Phase != agentv1alpha1.PhaseRolledBack {
		t.Errorf("phase = %s, want RolledBack", res.Phase)
	}
	if res.Cause != CauseAdmissionDenied {
		t.Errorf("cause = %s, want AdmissionDenied", res.Cause)
	}
	if rb.calls != 1 {
		t.Fatalf("rollback attempted %d times, want exactly 1", rb.calls)
	}
	if res.Verification.Passed {
		t.Error("verification.passed is true on a rolled-back action")
	}

	// The ladder went straight to rung 3, which is legal only because the transition carries a
	// reason. 04 §5: never SILENTLY skipped.
	if res.Recovery.Rung != RungRollback {
		t.Errorf("rung = %d, want 3", res.Recovery.Rung)
	}
	if n := len(res.Recovery.Transitions); n != 1 {
		t.Fatalf("%d transitions, want 1", n)
	}
	if res.Recovery.Transitions[0].Reason == "" {
		t.Error("the rung-0-to-3 skip carries no reason")
	}
	if err := ValidateRecovery(&res.Recovery); err != nil {
		t.Errorf("the driver produced a recovery that does not validate: %v", err)
	}

	// And the target is quiet afterwards (04 §4.2).
	if res.CooldownUntil.IsZero() {
		t.Fatal("a rolled-back target was not put into cooldown")
	}
	active, _, _ := cd.Active(context.Background(), TargetKey(ref("apps", "Deployment", "web")), c.now)
	if !active {
		t.Error("the cooldown registry does not report the target as quiet")
	}
}

func TestDriverRollsBackWhenTheSettleWindowExpires(t *testing.T) {
	// The Indeterminate-to-rollback path: "we could not confirm it" is not "it worked".
	c := &clock{now: base}
	rb := &fakeRollbacker{}
	// No restart baseline, so the workload predicate is Indeterminate forever.
	p := deployObj(7, 3)
	d := newDriver(p, c)
	d.Rollback, d.Cooldown = rb, NewMemoryCooldown()
	d.PollInterval = time.Minute

	res, err := d.Run(context.Background(), Request{
		ActionID: "a-4", AgentIdentity: "platform/prod",
		Targets:  []Target{{Ref: ref("apps", "Deployment", "web")}}, // BaselineRestarts nil
		UndoPlan: replayablePlan(),
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if res.Decision != DecisionRolledBack {
		t.Fatalf("decision = %s, want RolledBack", res.Decision)
	}
	if res.Cause != CauseSettleWindowExpired {
		t.Errorf("cause = %s, want SettleWindowExpired", res.Cause)
	}
	// A Deployment's window is 5m and the poll interval is 1m, so the driver waits it out and no
	// longer. An unbounded wait is the failure this assertion pins.
	if want := 5 * time.Minute; c.now.Sub(base) > want {
		t.Errorf("the driver waited %s, past the %s settle window", c.now.Sub(base), want)
	}
	if !strings.Contains(res.Verification.Checks[0].Detail, "settle window") {
		t.Errorf("the recorded detail does not say the window expired: %q", res.Verification.Checks[0].Detail)
	}
}

func TestDriverPagesAndPausesWhenTheRollbackFails(t *testing.T) {
	c := &clock{now: base}
	rb := &fakeRollbacker{err: errors.New("the API server rejected the restore")}
	pager, pauser := &fakePager{}, &fakePauser{}
	cd := NewMemoryCooldown()

	p := deployObj(7, 0)
	p.obj = obj("apps", "Deployment", "web", map[string]any{"replicas": int64(3)},
		map[string]any{
			"observedGeneration": int64(7), "availableReplicas": int64(0),
			"conditions": []any{map[string]any{
				"type": "ReplicaFailure", "status": "True", "message": "is invalid",
			}},
		})

	d := newDriver(p, c)
	d.Rollback, d.Pager, d.Pauser, d.Cooldown = rb, pager, pauser, cd

	res, err := d.Run(context.Background(), Request{
		ActionID: "a-5", AgentIdentity: "developer-team/prod",
		Targets: []Target{deployTarget(7, 0)}, UndoPlan: replayablePlan(),
	})
	if err != nil {
		t.Fatalf("Run returned %v; a page delivered and a pause applied is not an error", err)
	}
	if res.Decision != DecisionPaged {
		t.Fatalf("decision = %s, want Paged", res.Decision)
	}
	if res.Phase != agentv1alpha1.PhaseFailed {
		t.Errorf("phase = %s, want Failed", res.Phase)
	}
	// "An immediate page, not a retry loop."
	if rb.calls != 1 {
		t.Errorf("rollback attempted %d times, want exactly 1", rb.calls)
	}
	if pager.calls != 1 || !res.Paged {
		t.Errorf("pages = %d, res.Paged = %v", pager.calls, res.Paged)
	}
	if pauser.calls != 1 || !res.Paused {
		t.Errorf("pauses = %d, res.Paused = %v", pauser.calls, res.Paused)
	}
	if pauser.agent != "developer-team/prod" {
		t.Errorf("paused %q, want the acting agent", pauser.agent)
	}
	if !strings.Contains(pager.last.RollbackError, "rejected the restore") {
		t.Errorf("the page does not carry the rollback error: %+v", pager.last)
	}
	if res.Recovery.Rung != RungPage {
		t.Errorf("rung = %d, want 5", res.Recovery.Rung)
	}
	if err := ValidateRecovery(&res.Recovery); err != nil {
		t.Errorf("the driver produced a recovery that does not validate: %v", err)
	}
	if res.CooldownUntil.IsZero() {
		t.Error("a target whose rollback failed was not put into cooldown")
	}
}

// TestDriverPagesAndPausesIndependently: neither failure may swallow the other. Pausing without
// paging leaves a silently dead agent; paging without pausing leaves a live agent whose last action
// is unreversed.
func TestDriverPagesAndPausesIndependently(t *testing.T) {
	terminal := func() *fakeProber {
		return &fakeProber{
			obj: obj("apps", "Deployment", "web", map[string]any{"replicas": int64(3)},
				map[string]any{
					"observedGeneration": int64(7), "availableReplicas": int64(0),
					"conditions": []any{map[string]any{
						"type": "ReplicaFailure", "status": "True", "message": "is invalid",
					}},
				}),
			restarts: i64(0),
		}
	}

	t.Run("the pager fails, the pause still happens", func(t *testing.T) {
		c := &clock{now: base}
		pager := &fakePager{err: errors.New("pagerduty is down")}
		pauser := &fakePauser{}
		d := newDriver(terminal(), c)
		d.Rollback = &fakeRollbacker{err: errors.New("restore failed")}
		d.Pager, d.Pauser = pager, pauser

		res, err := d.Run(context.Background(), Request{
			ActionID: "a-6", AgentIdentity: "x", Targets: []Target{deployTarget(7, 0)},
			UndoPlan: replayablePlan(),
		})
		if err == nil {
			t.Fatal("an undeliverable page was reported as success")
		}
		if pauser.calls != 1 || !res.Paused {
			t.Errorf("the agent was not paused when paging failed (calls=%d)", pauser.calls)
		}
		if res.Paged {
			t.Error("res.Paged is true after the pager errored")
		}
		if res.Decision != DecisionPaged {
			t.Errorf("decision = %s, want Paged", res.Decision)
		}
	})

	t.Run("the pauser fails, the page still happens", func(t *testing.T) {
		c := &clock{now: base}
		pager := &fakePager{}
		pauser := &fakePauser{err: errors.New("the Agent CR is gone")}
		d := newDriver(terminal(), c)
		d.Rollback = &fakeRollbacker{err: errors.New("restore failed")}
		d.Pager, d.Pauser = pager, pauser

		res, err := d.Run(context.Background(), Request{
			ActionID: "a-7", AgentIdentity: "x", Targets: []Target{deployTarget(7, 0)},
			UndoPlan: replayablePlan(),
		})
		if err == nil {
			t.Fatal("a failed auto-pause was reported as success")
		}
		if pager.calls != 1 || !res.Paged {
			t.Errorf("the page was not sent when pausing failed (calls=%d)", pager.calls)
		}
		if res.Paused {
			t.Error("res.Paused is true after the pauser errored")
		}
	})

	t.Run("no pager wired at all is an error, not a silent success", func(t *testing.T) {
		c := &clock{now: base}
		d := newDriver(terminal(), c)
		d.Rollback = &fakeRollbacker{err: errors.New("restore failed")}
		d.Pauser = &fakePauser{}

		if _, err := d.Run(context.Background(), Request{
			ActionID: "a-8", AgentIdentity: "x", Targets: []Target{deployTarget(7, 0)},
			UndoPlan: replayablePlan(),
		}); err == nil {
			t.Fatal("a failed rollback with no Pager reached nobody and returned nil")
		}
	})
}

// TestDriverTreatsAnUnreplayablePlanAsARollbackFailure: step 6 guarantees an executed action has a
// plan, so arriving here without one means the guarantee did not hold -- which is a page, not a
// skipped rung.
func TestDriverTreatsAnUnreplayablePlanAsARollbackFailure(t *testing.T) {
	for _, tc := range []struct {
		name string
		plan agentv1alpha1.UndoPlan
	}{
		{"no plan at all", agentv1alpha1.UndoPlan{}},
		{"strategy none", agentv1alpha1.UndoPlan{Strategy: agentv1alpha1.UndoNone}},
		{"never dry-run", agentv1alpha1.UndoPlan{
			Strategy: agentv1alpha1.UndoRestore,
			Steps:    replayablePlan().Steps,
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c := &clock{now: base}
			rb := &fakeRollbacker{}
			pager, pauser := &fakePager{}, &fakePauser{}
			p := &fakeProber{
				obj: obj("apps", "Deployment", "web", map[string]any{"replicas": int64(3)},
					map[string]any{
						"observedGeneration": int64(7), "availableReplicas": int64(0),
						"conditions": []any{map[string]any{
							"type": "ReplicaFailure", "status": "True", "message": "is invalid",
						}},
					}),
				restarts: i64(0),
			}
			d := newDriver(p, c)
			d.Rollback, d.Pager, d.Pauser = rb, pager, pauser

			res, err := d.Run(context.Background(), Request{
				ActionID: "a-9", AgentIdentity: "x",
				Targets: []Target{deployTarget(7, 0)}, UndoPlan: tc.plan,
			})
			if err != nil {
				t.Fatalf("Run: %v", err)
			}
			if res.Decision != DecisionPaged {
				t.Errorf("decision = %s, want Paged", res.Decision)
			}
			if rb.calls != 0 {
				t.Errorf("an unreplayable plan was handed to the rollbacker %d times", rb.calls)
			}
			if pager.calls != 1 {
				t.Errorf("pages = %d, want 1", pager.calls)
			}
		})
	}
}

// TestDriverStopsAtTheFirstFailingTarget: continuing would spend another target's whole window
// discovering a second symptom of the same failure, while the undo snapshot ages.
func TestDriverStopsAtTheFirstFailingTarget(t *testing.T) {
	c := &clock{now: base}
	p := &fakeProber{
		obj: obj("apps", "Deployment", "web", map[string]any{"replicas": int64(3)},
			map[string]any{
				"observedGeneration": int64(7), "availableReplicas": int64(0),
				"conditions": []any{map[string]any{
					"type": "ReplicaFailure", "status": "True", "message": "is invalid",
				}},
			}),
		restarts: i64(0),
	}
	d := newDriver(p, c)
	d.Rollback = &fakeRollbacker{}

	res, err := d.Run(context.Background(), Request{
		ActionID: "a-10", AgentIdentity: "x",
		Targets: []Target{
			deployTarget(7, 0),
			{Ref: ref("apps", "Deployment", "api"), BaselineRestarts: i64(0)},
			{Ref: ref("apps", "Deployment", "worker"), BaselineRestarts: i64(0)},
		},
		UndoPlan: replayablePlan(),
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if n := len(res.Verification.Checks); n != 1 {
		t.Errorf("%d checks recorded, want 1 — verification continued past the governing failure", n)
	}
	if res.Verification.Checks[0].Name != "rollout-complete[0]" {
		t.Errorf("check name = %q, want the index-qualified name", res.Verification.Checks[0].Name)
	}
}

func TestDriverResumesAnInFlightLadder(t *testing.T) {
	// A retried action arrives with rung 1 already recorded. Its rollback must chain off that, not
	// start a second history.
	c := &clock{now: base.Add(time.Hour)}
	p := &fakeProber{
		obj: obj("apps", "Deployment", "web", map[string]any{"replicas": int64(3)},
			map[string]any{
				"observedGeneration": int64(7), "availableReplicas": int64(0),
				"conditions": []any{map[string]any{
					"type": "ReplicaFailure", "status": "True", "message": "is invalid",
				}},
			}),
		restarts: i64(0),
	}
	d := newDriver(p, c)
	d.Rollback, d.Cooldown = &fakeRollbacker{}, NewMemoryCooldown()

	res, err := d.Run(context.Background(), Request{
		ActionID: "a-11", AgentIdentity: "x",
		Targets:  []Target{deployTarget(7, 0)},
		UndoPlan: replayablePlan(),
		Recovery: *recoveryOf(RungRetry, tr(0, 1, 1, "Conflict (Transient)")),
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if n := len(res.Recovery.Transitions); n != 2 {
		t.Fatalf("%d transitions, want 2 — the prior rung-1 visit was dropped", n)
	}
	if res.Recovery.Transitions[1].From != RungRetry || res.Recovery.Transitions[1].To != RungRollback {
		t.Errorf("the rollback transition is %d->%d, want 1->3",
			res.Recovery.Transitions[1].From, res.Recovery.Transitions[1].To)
	}
	if err := ValidateRecovery(&res.Recovery); err != nil {
		t.Errorf("resumed recovery does not validate: %v", err)
	}
}

func TestDriverRefusesAnInvalidInboundLadder(t *testing.T) {
	// Resuming an illegal history would launder it: everything after the resume point validates.
	c := &clock{now: base}
	d := newDriver(deployObj(7, 3), c)
	_, err := d.Run(context.Background(), Request{
		ActionID: "a-12",
		Targets:  []Target{deployTarget(7, 3)},
		Recovery: *recoveryOf(RungRetry, tr(0, 3, 1, "terminal"), tr(3, 1, 2, "again")),
	})
	if err == nil {
		t.Fatal("an invalid inbound recovery was resumed")
	}
}

func TestDriverReturnsRetryWhileTheWindowIsOpen(t *testing.T) {
	// A context cancellation mid-poll must not read as success. It is recorded as a failure with the
	// window as the cause, which rolls back -- the same rule as an expired window.
	ctx, cancel := context.WithCancel(context.Background())
	c := &clock{now: base}
	d := &Driver{
		Prober: deployObj(7, 1),
		Now:    c.Now,
		Sleep: func(context.Context, time.Duration) error {
			cancel()
			return context.Canceled
		},
		Rollback:     &fakeRollbacker{},
		PollInterval: time.Second,
	}
	res, err := d.Run(ctx, Request{
		ActionID: "a-13", Targets: []Target{deployTarget(7, 1)}, UndoPlan: replayablePlan(),
	})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if res.Verification.Passed {
		t.Fatal("an interrupted verification passed")
	}
	if res.Decision != DecisionRolledBack {
		t.Errorf("decision = %s, want RolledBack", res.Decision)
	}
}

func TestDefaultPollIntervalApplies(t *testing.T) {
	d := &Driver{}
	if got := d.pollInterval(); got != DefaultPollInterval {
		t.Errorf("pollInterval = %s, want %s", got, DefaultPollInterval)
	}
	d.PollInterval = time.Second
	if got := d.pollInterval(); got != time.Second {
		t.Errorf("pollInterval = %s, want 1s", got)
	}
}
