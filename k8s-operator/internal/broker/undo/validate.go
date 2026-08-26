package undo

import (
	"context"
	"fmt"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// DryRunner is the seam over "would this step apply".
//
// 06 §4.3.1 requires each step to be dry-run against the API server at generation time. An
// interface rather than a client, for the reason every seam in this build is: the round-trip corpus
// must run with no cluster, and a fake here is what lets a fixture assert the DOWNGRADE that a
// failed dry-run causes -- which is the behaviour that matters and the one a real API server will
// not reproduce on demand.
type DryRunner interface {
	// DryRun applies one step with the server's dry-run flag set and returns nil if it would
	// succeed. The error is surfaced to a human verbatim, so an implementation should return the
	// API server's message rather than wrapping it into something tidier.
	DryRun(ctx context.Context, step agentv1alpha1.UndoStep) error
}

// Validate dry-runs every step and records the verdict on the plan.
//
// THE RESULT IS A DOWNGRADE, NOT AN ERROR. A step that will not apply means the plan does not work,
// and a plan that does not work is not a plan: the strategy drops to `none`, the steps are dropped
// with it, and the reason becomes a caveat. The action then gates, which is the whole mechanism of
// 09 V-REV-003 — "an action with no generatable undo plan is reclassified gated and never
// auto-executes". Returning an error instead would let a caller log it and proceed with
// `validated: false`, executing an action whose rollback has been PROVEN not to work.
//
// A nil DryRunner is also a downgrade. Not validating and validating successfully are different
// outcomes, and `Validated` is the field that says which one happened; defaulting an unwired
// broker to `true` would make the strongest claim in the record the one nothing checked.
func Validate(ctx context.Context, res *Result, dr DryRunner) error {
	if res == nil || res.Plan == nil {
		return fmt.Errorf("cannot validate a nil plan")
	}
	plan := res.Plan

	if plan.Strategy == agentv1alpha1.UndoNone {
		// Already refused. Nothing to dry-run, and `validated` stays false: a refusal that claimed
		// to be validated would read, to every consumer, as a plan that was checked and found good.
		plan.Validated = false
		return nil
	}

	if dr == nil {
		downgrade(res, "no dry-run client is wired, so no step in this plan has been checked against the API server")
		return nil
	}

	for i, step := range plan.Steps {
		if err := dr.DryRun(ctx, step); err != nil {
			downgrade(res, fmt.Sprintf(
				"step %d (%s %s %s/%s) would not apply: %v",
				i, step.Op, step.Target.Kind, step.Target.Namespace, step.Target.Name, err))
			return nil
		}
	}

	plan.Validated = true
	return nil
}

// downgrade turns a plan into a refusal, preserving the reason.
//
// Steps are DROPPED, not kept for reference. A refused plan that still carries steps is one
// `if plan.Steps != nil` away from being replayed by a caller who did not check the strategy, and
// this package has no way to stop that caller except by not handing them the steps.
func downgrade(res *Result, reason string) {
	res.Refusals = append(res.Refusals, reason)
	res.Plan.Strategy = agentv1alpha1.UndoNone
	res.Plan.Steps = nil
	res.Plan.Validated = false
	res.Plan.Caveats = append(res.Plan.Caveats, reason)
}

// GenerateAndValidate is the call the broker actually makes at step 6.
//
// Exists so that the two halves cannot be separated by accident. Generating without validating
// yields a plan whose `validated` is false, which ValidateReplayable refuses at replay time -- but
// that refusal arrives when a human is trying to undo an outage, which is the worst moment to
// discover the plan was never checked. Here the same fact arrives before execution, where its
// consequence is a gate.
func GenerateAndValidate(ctx context.Context, req Request, idx ReferenceIndex, dr DryRunner) (*Result, error) {
	res, err := Generate(ctx, req, idx)
	if err != nil {
		return nil, err
	}
	if err := Validate(ctx, res, dr); err != nil {
		return nil, err
	}
	return res, nil
}
