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

package pipeline

import (
	"context"
	"encoding/json"
	"fmt"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/classify"
)

// Resume is the resumption loop's whole job (docs/designs/broker/chat-approval.md §3): given a
// record the ChatOps gateway has already moved to Pending — the roster's minApprovals is already
// satisfied, that authorization already happened — re-classify against LIVE state and either
// refuse (the class rose, or a target moved out from under the plan) or execute.
//
// "Approval is permission, not a bypass" is the one sentence this function exists to make true. It
// re-runs exactly the read-and-decide steps a fresh submission would (stepResolve, stepClassify),
// which is what makes the re-classification real rather than a rubber stamp: a target deleted and
// recreated since the original submission is a different object with the same name, and this is
// what notices.
func (p *Pipeline) Resume(ctx context.Context, ar *agentv1alpha1.ActionRecord) (*broker.Result, error) {
	if ar.Status.Phase != agentv1alpha1.PhasePending {
		return nil, fmt.Errorf("pipeline: resume called on %s in phase %q, want %q", ar.Name, ar.Status.Phase, agentv1alpha1.PhasePending)
	}
	if ar.Spec.EnvelopeJSON == "" {
		return p.refuseResumption(ctx, ar, "the parked action has no preserved envelope to resume from (it predates this broker's resumption support)")
	}

	var env broker.Envelope
	if err := json.Unmarshal([]byte(ar.Spec.EnvelopeJSON), &env); err != nil {
		return p.refuseResumption(ctx, ar, fmt.Sprintf("the preserved envelope could not be decoded: %v", err))
	}

	originalClass, err := classify.ParseClass(string(ar.Spec.Classification.Class))
	if err != nil {
		return nil, fmt.Errorf("pipeline: resume: %s's recorded class %q does not parse: %w", ar.Name, ar.Spec.Classification.Class, err)
	}
	originalTargets := ar.Spec.Targets

	agent := p.cfg.Brake.Observe(ctx).Agent
	if agent == nil {
		return p.refuseResumption(ctx, ar, "the broker cannot currently read its own Agent CR, so it cannot re-derive the tier this action resumes under")
	}

	tr := &broker.StepTrace{}
	if err := tr.Skip(broker.StepAuthenticate, "resumed: identity was established at initial submission"); err != nil {
		return nil, err
	}
	if err := tr.Skip(broker.StepValidate, "resumed: envelope was already validated at initial submission"); err != nil {
		return nil, err
	}

	id := &broker.Identity{
		Username:       "system:serviceaccount:" + ar.Namespace + ":" + ar.Spec.ActorServiceAccount,
		Namespace:      ar.Namespace,
		ServiceAccount: ar.Spec.ActorServiceAccount,
		AgentName:      p.cfg.AgentName,
		Tier:           agent.Spec.Tier,
	}

	s := &state{id: id, env: &env, tr: tr, at: p.cfg.Now().UTC(), actionID: ar.Spec.ActionID, record: ar}
	s.mayExecute = !env.DryRun && !shadowed(p.cfg.Brake.Observe(ctx))

	for _, step := range []func(context.Context, *state) (*broker.Result, error){p.stepResolve, p.stepClassify} {
		res, err := step(ctx, s)
		if err != nil {
			return nil, err
		}
		if res != nil {
			return res, nil // forbidden, out of scope, or an abort — the same terminal outcomes a fresh submission gets
		}
	}

	if s.class.Class > originalClass {
		return p.refuseResumption(ctx, ar, fmt.Sprintf(
			"re-classification at approval time raised the risk from %s to %s; the roster approved a %s action, not this one (06 §4.4)",
			originalClass, s.class.Class, originalClass))
	}
	if moved, detail := preconditionsMoved(originalTargets, s.targets); moved {
		return p.refuseResumption(ctx, ar, "a target changed identity since classification and this approval no longer covers it: "+detail)
	}

	for _, step := range []func(context.Context, *state) (*broker.Result, error){p.stepBrake, p.stepUndoPlan} {
		res, err := step(ctx, s)
		if err != nil {
			return nil, err
		}
		if res != nil {
			return res, nil
		}
	}

	// stepGate is the ONLY step that acts on BrakeRaiseToGated/BrakePark in Submit's flow -- both
	// effects change nothing but s.class.Class, and stepGate is what reads that class and parks.
	// Resume never calls stepGate (see the tr.Skip below), so without this check a brake that fires
	// EITHER effect during the fresh re-evaluation above would be silently ignored: row 5 (the
	// re-generated undo plan is unusable) or row 6 (the roster shrank below minApprovals since the
	// original approval) would both fall through to execution instead of stopping it. Refusing
	// rather than re-parking either way: re-parking would mean re-running the whole approval loop
	// (a fresh notify, a fresh threshold) for a record that already has one round of approvals on
	// it, which is more state to reconcile correctly than the value of not making the requester
	// resubmit -- the same "re-propose, don't resurrect" choice chat-approval.md sequence 3 already
	// makes for a record that expired the ordinary way.
	if s.brakeEffect == broker.BrakeRaiseToGated || s.brakeEffect == broker.BrakePark {
		return p.refuseResumption(ctx, ar, fmt.Sprintf(
			"the brake's fresh check at resume time raised this action back to gated (%s); the approval that got it here no longer covers it (06 §4.4)",
			s.brakeEffect))
	}

	// stepUndoPlan's buildRecord path is for a record that does not exist yet; this one already
	// does. Persist the fresh snapshot and plan onto it directly instead — Create would be a no-op
	// against an existing name (journal.Store.Create's AlreadyExists branch), silently discarding
	// exactly the re-snapshot 06 §4.4 requires.
	freshRecord := p.buildRecord(s)
	if err := p.cfg.Records.UpdateForResume(ctx, ar, freshRecord.Spec.PreState, freshRecord.Spec.Undo); err != nil {
		return nil, fmt.Errorf("pipeline: resume: %s: %w", ar.Name, err)
	}
	s.record = ar

	if err := tr.Skip(broker.StepGate, "resumed: already approved, see status.approvals"); err != nil {
		return nil, err
	}
	if err := tr.Skip(broker.StepSnapshot, "resumed: pre-state and undo plan already persisted via UpdateForResume"); err != nil {
		return nil, err
	}

	if err := p.cfg.Records.SetPhase(ctx, s.record, agentv1alpha1.PhaseExecuting, "resuming after approval"); err != nil {
		return nil, fmt.Errorf("pipeline: resume: marking %s executing: %w", ar.Name, err)
	}

	for _, step := range []func(context.Context, *state) (*broker.Result, error){p.stepExecute, p.stepVerify, p.stepJournal} {
		res, err := step(ctx, s)
		if err != nil {
			return nil, err
		}
		if res != nil {
			return res, nil
		}
	}

	phase, _ := terminal(s)
	return &broker.Result{
		ActionID:  s.actionID,
		Namespace: p.cfg.Namespace,
		Decision:  "accepted",
		Phase:     string(phase),
		Message:   "the approved action executed and was verified",
	}, nil
}

// refuseResumption terminates a record that cannot safely resume, using the phase transition
// Pending -> Rejected: the same transition an outright reject uses, because from the requester's
// point of view "the roster approved something the cluster no longer matches" and "a roster member
// said no" have the same consequence — this action does not execute, and a re-raise is a new
// envelope (chat-approval.md §4 sequence 3's "not resurrected" rule, applied one step earlier).
func (p *Pipeline) refuseResumption(ctx context.Context, ar *agentv1alpha1.ActionRecord, reason string) (*broker.Result, error) {
	if err := p.cfg.Records.SetPhase(ctx, ar, agentv1alpha1.PhaseRejected, reason); err != nil {
		return nil, fmt.Errorf("pipeline: resume: refusing %s: %w", ar.Name, err)
	}
	return &broker.Result{
		ActionID:  ar.Spec.ActionID,
		Namespace: ar.Namespace,
		Decision:  "refused",
		Phase:     string(agentv1alpha1.PhaseRejected),
		Message:   reason,
	}, nil
}

// preconditionsMoved reports whether any target's identity changed between the original
// classification and this re-resolution: a UID that no longer matches means the object was deleted
// and recreated (or, for a create op with no prior UID, one that now unexpectedly exists), and
// the plan approved is not a plan for the object currently at that name.
func preconditionsMoved(original, fresh []agentv1alpha1.TargetRef) (bool, string) {
	if len(original) != len(fresh) {
		return true, fmt.Sprintf("the action now resolves to %d targets, originally %d", len(fresh), len(original))
	}
	for i := range original {
		if original[i].UID != fresh[i].UID {
			return true, fmt.Sprintf("target %d (%s/%s) had uid %q at classification and %q now",
				i, original[i].Kind, original[i].Name, original[i].UID, fresh[i].UID)
		}
	}
	return false, ""
}
