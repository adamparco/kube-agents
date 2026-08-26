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
	"fmt"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/undo"
)

// Rollbacker replays an undo plan. It is an interface rather than a call into `execute` because the
// replay is one bounded attempt with no dry-run pass -- the state it is restoring was captured
// before the action, and re-deriving it here would be a second opinion about the same bytes.
type Rollbacker interface {
	// Rollback replays the plan. It is called AT MOST ONCE per action: 04 §5.1 makes a failed
	// rollback an immediate page, "not a retry loop".
	//
	// agentIdentity is passed per-call rather than bound into the implementation because the
	// replayer derives its FIELD MANAGER from it, and the manager string is security-relevant:
	// V-BRK-019 fixes it at `kube-agents/<tier>/<scope>`, and `contested` in 03 §6 is a comparison
	// against it. An implementation constructed once with a static identity writes the right
	// manager until the process serves a second one, and the wrong one silently thereafter --
	// attributing one agent's rollback to another in every managedFields entry it touches.
	Rollback(ctx context.Context, actionID, agentIdentity string, plan agentv1alpha1.UndoPlan) error
}

// Pager raises a human page. A page that cannot be delivered is itself an error the driver
// surfaces -- a silent page is the failure mode this whole rung exists to prevent.
type Pager interface {
	Page(ctx context.Context, p PageRequest) error
}

// PageRequest is what a human is woken up with.
type PageRequest struct {
	ActionID      string
	AgentIdentity string
	Summary       string
	// RollbackError is the error the rollback returned, when there was one.
	RollbackError string
}

// Pauser auto-pauses an agent (03 §6). Pausing is a brake control owned by the operator's Agent CR;
// this interface is the seam the broker reaches it through.
//
// It takes the whole request rather than `(agentIdentity, reason)` because the broker cannot pause
// anything itself. 06 §2.2.1 gives it `get, list, watch` on `agents` and nothing more, so the only
// pause it can perform is a RECORDED REQUEST on the action's own journal entry, which C-BR fans out
// (05 §1.5, §1.7). A pause that does not name the record it belongs to cannot be written down at
// all, and an implementation handed only an identity would have to go looking for the record --
// re-deriving, from an index, a fact its caller already had.
type Pauser interface {
	Pause(ctx context.Context, p PauseRequest) error
}

// PauseRequest is the brake, asked for. Symmetric with PageRequest on purpose: 05 §1.5's auto-brake
// table treats page and pause as separate responses that happen to coincide at rung 5, and a shared
// struct would quietly encourage a caller to send one when it meant the other.
type PauseRequest struct {
	ActionID      string
	AgentIdentity string
	// Reason is carried through to `Agent.spec.operations.pauseReason`, so it is what a human sees
	// when they ask why the agent stopped.
	Reason string
}

// CooldownRegistry is the per-target quiet period of 04 §4.2. The driver only ever ENTERS cooldown;
// enforcing it is step 5 of the pipeline (the brake), which reads the same registry.
//
// This split is what makes "never restarts at the bottom for the same target after a rollback"
// (04 §5) enforceable at all. Within one ActionRecord the ladder's monotonicity already forbids
// returning to rung 1 -- but the real hazard is the NEXT action, with a fresh record and a fresh
// ladder that legitimately starts at rung 0. Only a target-keyed cooldown that outlives the record
// can stop that one.
type CooldownRegistry interface {
	// Enter starts or extends the cooldown for a target and returns when it expires.
	//
	// actionID names the ActionRecord whose remediation failed, and it is here for the same reason
	// Pauser.Pause takes a whole PauseRequest: the durable implementation
	// (internal/broker/cooldown) recovers the series from the journal, so it has to be able to tell
	// "this failure, which I am being told about now" from "this failure, which I have already read
	// back out of the journal". Handed only a target key it would have to guess, and both guesses
	// are wrong -- count it twice and one rollback buys a doubled quiet period, count it never and
	// the cooldown does not exist until the status write lands. The caller has the ID in its hand;
	// passing it costs nothing and makes Enter IDEMPOTENT PER ACTION, which is a promise callers may
	// rely on: entering twice for one action is exactly entering once.
	Enter(ctx context.Context, actionID, targetKey string, now time.Time) (time.Time, error)
	// Active reports whether a target is in cooldown, and until when.
	Active(ctx context.Context, targetKey string, now time.Time) (bool, time.Time, error)
}

// TargetKey is the stable identity a cooldown is keyed on. UID is deliberately excluded: a target
// deleted and recreated during a flap is the same target to an operator, and keying on UID would
// hand a fresh cooldown to every recreate.
func TargetKey(ref agentv1alpha1.TargetRef) string {
	g := ref.Group
	if g == "" {
		g = "core"
	}
	return fmt.Sprintf("%s/%s/%s/%s", g, ref.Kind, ref.Namespace, ref.Name)
}

// Decision is what the driver concluded. It is returned rather than acted on, because rungs 1, 2
// and 4 belong to the agent and the parent tier -- the broker's own authority stops at rung 3.
type Decision string

const (
	// DecisionVerified means every predicate was satisfied.
	DecisionVerified Decision = "Verified"
	// DecisionRetry means a transient failure: the ladder is at rung 1 and the caller may retry.
	DecisionRetry Decision = "Retry"
	// DecisionRolledBack means a terminal failure was rolled back automatically.
	DecisionRolledBack Decision = "RolledBack"
	// DecisionPaged means the rollback itself failed: a human was paged and the agent paused.
	DecisionPaged Decision = "Paged"
)

// Request is one action's post-execution verification.
type Request struct {
	ActionID      string
	AgentIdentity string
	// Targets is every object the action touched, in spec.targets order.
	Targets []Target
	// UndoPlan is the plan generated at step 6. It is replayed on a terminal failure.
	UndoPlan agentv1alpha1.UndoPlan
	// Recovery resumes an in-flight ladder. Zero value starts a new one.
	Recovery agentv1alpha1.ActionRecovery

	// ExecutionFailure is the failure step 9 saw when the write itself did not land.
	//
	// When it is set the driver classifies it INSTEAD of verifying, because there is nothing to
	// verify against a write that never happened -- and polling the target's whole settle window
	// would report the pre-action state as a verification failure of an action that never touched
	// it. This is also the only way rung 1 is reached: 04 §5 rung 1 is "retry with backoff" for a
	// transient failure of the ACTION, while the driver's polling inside a settle window is
	// verification catching up with a write that did land, which is not a rung at all.
	ExecutionFailure *Failure
}

// Result is everything the caller writes to `status`.
type Result struct {
	Decision     Decision
	Phase        agentv1alpha1.ActionPhase
	Verification agentv1alpha1.ActionVerification
	Recovery     agentv1alpha1.ActionRecovery
	// Cause is the governing cause when verification did not pass.
	Cause Cause
	// Paged and Paused record what the driver actually did, not what it intended.
	Paged, Paused bool
	// CooldownUntil is set when a rollback put the target into cooldown.
	CooldownUntil time.Time
}

// Driver runs step 10: verify, and recover when verification fails.
type Driver struct {
	Prober   Prober
	Rollback Rollbacker
	Pager    Pager
	Pauser   Pauser
	Cooldown CooldownRegistry

	// Now and Sleep are injected so the settle window is testable without spending it.
	Now   func() time.Time
	Sleep func(ctx context.Context, d time.Duration) error

	// PollInterval is how often a pending predicate is re-evaluated. Zero means DefaultPollInterval.
	PollInterval time.Duration
}

// DefaultPollInterval is the re-evaluation cadence inside a settle window.
const DefaultPollInterval = 5 * time.Second

func (d *Driver) now() time.Time {
	if d.Now != nil {
		return d.Now()
	}
	return time.Now()
}

func (d *Driver) sleep(ctx context.Context, dur time.Duration) error {
	if d.Sleep != nil {
		return d.Sleep(ctx, dur)
	}
	t := time.NewTimer(dur)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

func (d *Driver) pollInterval() time.Duration {
	if d.PollInterval > 0 {
		return d.PollInterval
	}
	return DefaultPollInterval
}

// Run verifies every target and drives the recovery ladder. It never returns a Verified result on
// an unevaluated predicate, and it performs at most one rollback.
func (d *Driver) Run(ctx context.Context, req Request) (Result, error) {
	if d.Prober == nil {
		return Result{}, fmt.Errorf("verify.Driver has no Prober: a driver that cannot look at the "+
			"cluster would report every action of %s as verified", req.ActionID)
	}
	ladder, err := FromRecovery(req.Recovery)
	if err != nil {
		return Result{}, err
	}

	evals, governing := d.assess(ctx, req)
	res := Result{
		Recovery: ladder.Recovery(),
		Cause:    governing,
		Verification: agentv1alpha1.ActionVerification{
			Passed:      governing == "",
			CompletedAt: ptrTime(d.now()),
			Checks:      evals,
		},
	}

	if governing == "" {
		res.Decision = DecisionVerified
		res.Phase = agentv1alpha1.PhaseVerified
		return res, nil
	}

	if DispositionOf(governing) == Transient {
		if err := ladder.Climb(RungRetry, d.now(), Describe(governing)); err != nil {
			return res, fmt.Errorf("recording rung 1: %w", err)
		}
		res.Recovery = ladder.Recovery()
		res.Decision = DecisionRetry
		res.Phase = agentv1alpha1.PhaseExecuting
		return res, nil
	}

	return d.rollBack(ctx, req, ladder, res, governing)
}

// assess produces the recorded checks and the governing cause, from whichever of the two inputs
// step 10 actually has: a write that failed, or a write that landed and must now be verified.
func (d *Driver) assess(ctx context.Context, req Request) ([]agentv1alpha1.VerificationCheck, Cause) {
	if req.ExecutionFailure == nil {
		return d.verifyAll(ctx, req)
	}
	governing := CauseOf(*req.ExecutionFailure)
	detail := req.ExecutionFailure.Message
	if req.ExecutionFailure.Err != nil {
		detail = req.ExecutionFailure.Err.Error()
	}
	return []agentv1alpha1.VerificationCheck{{
		Name:   "execution",
		Passed: false,
		Detail: fmt.Sprintf("the write did not land: %s: %s", Describe(governing), detail),
	}}, governing
}

// verifyAll evaluates every target to its own settle window and returns the recorded checks plus
// the governing cause -- empty when everything passed.
//
// Targets are verified in order and the first non-passing one governs. Continuing past it would
// spend another target's whole window discovering a second symptom of the same failure, while the
// undo snapshot ages.
func (d *Driver) verifyAll(ctx context.Context, req Request) ([]agentv1alpha1.VerificationCheck, Cause) {
	var checks []agentv1alpha1.VerificationCheck
	for i, t := range req.Targets {
		ev := d.verifyOne(ctx, t)
		checks = append(checks, agentv1alpha1.VerificationCheck{
			Name:   fmt.Sprintf("%s[%d]", ev.Name, i),
			Passed: ev.Verdict == VerdictSatisfied,
			Detail: ev.Detail,
		})
		if ev.Verdict == VerdictSatisfied {
			continue
		}
		cause := ev.Cause
		if cause == "" {
			cause = CauseUnknown
		}
		return checks, cause
	}
	return checks, ""
}

// verifyOne polls one target's predicate until it is satisfied, definitively fails, or the settle
// window closes.
func (d *Driver) verifyOne(ctx context.Context, t Target) Evaluation {
	pred := predicateFor(t)
	window := SettleWindow(t.Ref)
	deadline := d.now().Add(window)

	var last Evaluation
	for {
		last = pred(ctx, d.Prober, t)
		switch last.Verdict {
		case VerdictSatisfied, VerdictFailed:
			return last
		}

		// A Pending carrying a TERMINAL cause is not pending. The terminal half of 04 §5.1 is
		// exactly the set of causes that do not resolve by waiting, and `pendingCause` reads them
		// off the object's own conditions -- a Deployment whose ReplicaFailure names an admission
		// denial has already been rejected, and spending its settle window on the chance that
		// admission changes its mind is time nobody gets back.
		if DispositionOf(last.Cause) == Terminal {
			last.Verdict = VerdictFailed
			return last
		}

		if !d.now().Before(deadline) {
			// 04 §5.1: verification still failing at the end of the settle window is TERMINAL,
			// whatever the predicate's own cause said while it was still hopeful. This is the one
			// place an Indeterminate turns into a rollback, and it is deliberate: "we could not
			// confirm it" is not "it worked".
			return Evaluation{
				Verdict: VerdictFailed,
				Name:    last.Name,
				Detail: fmt.Sprintf("%s (settle window %s expired with verdict %s)",
					last.Detail, window, last.Verdict),
				Cause: CauseSettleWindowExpired,
			}
		}
		if err := d.sleep(ctx, d.pollInterval()); err != nil {
			return Evaluation{
				Verdict: VerdictFailed,
				Name:    last.Name,
				Detail:  fmt.Sprintf("%s (verification interrupted: %v)", last.Detail, err),
				Cause:   CauseSettleWindowExpired,
			}
		}
	}
}

// rollBack is rung 3 and, if it fails, rung 5. Exactly one attempt at each.
func (d *Driver) rollBack(ctx context.Context, req Request, ladder *Ladder, res Result, governing Cause) (Result, error) {
	reason := fmt.Sprintf("automatic rollback: %s", Describe(governing))
	if err := ladder.Climb(RungRollback, d.now(), reason); err != nil {
		return res, fmt.Errorf("recording rung 3: %w", err)
	}
	res.Recovery = ladder.Recovery()

	rbErr := d.attemptRollback(ctx, req)
	if rbErr == nil {
		res.Decision = DecisionRolledBack
		res.Phase = agentv1alpha1.PhaseRolledBack
		res.CooldownUntil = d.enterCooldown(ctx, req)
		return res, nil
	}

	// 04 §5.1: "A rollback that itself fails is an immediate page, not a retry loop. The agent is
	// auto-paused, because the system can no longer keep its core promise."
	pageReason := fmt.Sprintf("rollback failed after %s: %v", Describe(governing), rbErr)
	if err := ladder.Climb(RungPage, d.now(), pageReason); err != nil {
		return res, fmt.Errorf("recording rung 5: %w", err)
	}
	res.Recovery = ladder.Recovery()
	res.Decision = DecisionPaged
	res.Phase = agentv1alpha1.PhaseFailed

	// Both are attempted even if the first errors: pausing without paging leaves a silently dead
	// agent, and paging without pausing leaves a live agent whose last action is unreversed.
	var firstErr error
	if d.Pager != nil {
		if err := d.Pager.Page(ctx, PageRequest{
			ActionID:      req.ActionID,
			AgentIdentity: req.AgentIdentity,
			Summary:       pageReason,
			RollbackError: rbErr.Error(),
		}); err != nil {
			firstErr = fmt.Errorf("paging a human failed after a failed rollback: %w", err)
		} else {
			res.Paged = true
		}
	} else {
		firstErr = fmt.Errorf("no Pager configured: a failed rollback on %s reached nobody", req.ActionID)
	}

	if d.Pauser != nil {
		if err := d.Pauser.Pause(ctx, PauseRequest{
			ActionID:      req.ActionID,
			AgentIdentity: req.AgentIdentity,
			Reason:        pageReason,
		}); err != nil {
			if firstErr == nil {
				firstErr = fmt.Errorf("auto-pausing the agent failed after a failed rollback: %w", err)
			}
		} else {
			res.Paused = true
		}
	} else if firstErr == nil {
		firstErr = fmt.Errorf("no Pauser configured: %s stays live after a failed rollback", req.AgentIdentity)
	}

	// Cooldown still applies -- the target has been through a failed remediation, which is exactly
	// the case 04 §4.2 wants quiet.
	res.CooldownUntil = d.enterCooldown(ctx, req)
	return res, firstErr
}

// attemptRollback replays the plan once. A plan that cannot be replayed is a rollback failure, not
// a skipped rung: step 6 guarantees an executed action has a plan, so arriving here without one
// means the guarantee did not hold and the system cannot keep its core promise either way.
func (d *Driver) attemptRollback(ctx context.Context, req Request) error {
	if d.Rollback == nil {
		return fmt.Errorf("no Rollbacker configured")
	}
	if err := undo.ValidateReplayable(&req.UndoPlan); err != nil {
		return fmt.Errorf("undo plan is not replayable: %w", err)
	}
	return d.Rollback.Rollback(ctx, req.ActionID, req.AgentIdentity, req.UndoPlan)
}

// enterCooldown puts every target into the 04 §4.2 quiet period. A registry error is recorded in
// the result rather than returned: the rollback already succeeded, and failing the whole call now
// would report a rolled-back action as an error.
func (d *Driver) enterCooldown(ctx context.Context, req Request) time.Time {
	if d.Cooldown == nil {
		return time.Time{}
	}
	var latest time.Time
	for _, t := range req.Targets {
		until, err := d.Cooldown.Enter(ctx, req.ActionID, TargetKey(t.Ref), d.now())
		if err != nil {
			continue
		}
		if until.After(latest) {
			latest = until
		}
	}
	return latest
}

func ptrTime(t time.Time) *metav1.Time {
	mt := metav1.NewTime(t)
	return &mt
}
