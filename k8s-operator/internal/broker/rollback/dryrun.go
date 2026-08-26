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

package rollback

import (
	"context"
	"fmt"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/execute"
)

// PlanDryRunner is the production undo.DryRunner: 06 §4.3.1's "validated by dry-running each step
// against the API server", issued before the action runs.
//
// # Why it lives beside the replayer rather than beside the planner
//
// The question plan-time validation asks is not "is this plan well-formed" -- undo.ValidateReplayable
// already answers that, statically, with no cluster. It is "would the calls the REPLAYER is going
// to make succeed", and the only component that knows which calls those are is the replayer. So
// this type reuses `Replayer` outright: the same Writer, the same hydration, the same body
// refusals. A validator with its own op table would be answering a question about a different
// program, which is the LSN-040 shape that ReplayableOps() exists to prevent.
//
// Reusing hydration has a consequence worth stating, because it closes the worst hazard in this
// package one stage earlier than the package doc describes. `hydrate` refuses a Secret whose values
// the sanitizer replaced with digests -- "applying this body would replace each value with the hex
// of its own digest". Until now that refusal arrived at REPLAY time, during an incident, after the
// action had already run. Running it here turns it into a downgrade at generation time, which
// raises the action to `gated` before anything is mutated.
//
// # The two errors that are not failures
//
// An undo plan describes the world AFTER the action, and it is validated BEFORE the action. Two of
// the four steps therefore address an object whose existence is precisely what the action is about
// to change, and the API server's answer to those is fixed in advance:
//
//   - A `delete` step reverses a create. The object does not exist yet, so the dry run is a
//     NotFound.
//   - A `create` step reverses a delete. The object still exists, so the dry run is an
//     AlreadyExists.
//
// Both are treated as "would apply", and the argument is that neither is silent about the thing
// validation is actually for. Kubernetes authorizes before it looks the object up: a caller without
// the verb gets a 403, not a 404, so a NotFound is positive evidence that authn, authz and scope
// admitted the request. A create runs mutating and validating admission before storage, so an
// AlreadyExists additionally clears every webhook on the path. What the delete case does NOT clear
// is DELETE-time admission, which runs after the fetch -- that is a real gap in coverage and it is
// named here rather than papered over.
//
// Everything else is a failure: a 403, an Invalid, a webhook rejection, a missing scale target, a
// body the hydrator refuses. undo.Validate turns each of those into a downgrade to `none`, the
// classifier's 06 §4.2 step 6 rule then raises the action to `gated`, and a human decides. Nothing
// here returns an error that lets a caller proceed.
type PlanDryRunner struct {
	// Replayer performs the calls, in dry-run mode. Required.
	Replayer *Replayer

	// AgentIdentity is the `<tier>/<scope>` key of the agent whose action this plan reverses.
	//
	// It is here, and it is per-agent, because the dry run has to carry the SAME field manager the
	// replay would. Server-side apply reports a conflict for every field owned by a different
	// manager, and the fields an undo restores are frequently the ones this agent set in an earlier
	// action -- so a dry run issued under any other name manufactures conflicts the real replay
	// would never hit, downgrades a working plan, and gates the action for a reason that is an
	// artifact of the check. LSN-031 is the same rule arriving at the replay end.
	AgentIdentity string
}

// DryRun applies one step with the server's dry-run flag set.
//
// The op set is undo.ReplayableOps() and the dispatch mirrors Replayer.replayStep case for case;
// TestThePlanValidatorCoversEveryReplayableOp fails if they diverge.
func (d *PlanDryRunner) DryRun(ctx context.Context, step agentv1alpha1.UndoStep) error {
	if d.Replayer == nil || d.Replayer.Writer == nil {
		return fmt.Errorf("no writer is configured, so no step could be dry-run against the API server")
	}
	fm, err := execute.FieldManager(d.AgentIdentity)
	if err != nil {
		return err
	}

	switch step.Op {
	case "delete":
		return d.dryRunDelete(ctx, step)
	case "apply":
		return d.dryRunApply(ctx, step, fm)
	case "scale":
		return d.dryRunScale(ctx, step, fm)
	case "create":
		return d.dryRunCreate(ctx, step, fm)
	default:
		return fmt.Errorf("op %q has no replay implementation, so no plan containing it can be validated", step.Op)
	}
}

// dryRunDelete checks the delete that reverses a create.
//
// The uid precondition is deliberately NOT required here, where Replayer.replayDelete requires it.
// At plan time there is no uid to require: the object has not been created, and undo.BindCreatedUID
// fills the pin in after execution. Demanding it now would downgrade every create in the fleet.
func (d *PlanDryRunner) dryRunDelete(ctx context.Context, step agentv1alpha1.UndoStep) error {
	uid := ""
	if step.Preconditions != nil {
		uid = step.Preconditions.UID
	}
	err := d.Replayer.Writer.Delete(ctx, step.Target, execute.DeleteOpts{UID: uid}, true)
	switch {
	case err == nil, apierrors.IsNotFound(err):
		return nil
	default:
		return fmt.Errorf("deleting %s %s/%s would not apply: %w",
			step.Target.Kind, step.Target.Namespace, step.Target.Name, err)
	}
}

// dryRunApply checks the restore. This is the step where validation earns its keep: the body is a
// real object, the target is live now and will still be live after the action, and the server's
// answer is the answer.
func (d *PlanDryRunner) dryRunApply(ctx context.Context, step agentv1alpha1.UndoStep, fm string) error {
	obj, err := d.Replayer.hydrate(ctx, step)
	if err != nil {
		return err
	}
	if _, err := d.Replayer.Writer.Apply(ctx, obj, fm, true); err != nil {
		return fmt.Errorf("restoring %s %s/%s would not apply: %w",
			step.Target.Kind, step.Target.Namespace, step.Target.Name, err)
	}
	return nil
}

// dryRunScale checks the scale-back, including that the snapshot carries the number it restores.
func (d *PlanDryRunner) dryRunScale(ctx context.Context, step agentv1alpha1.UndoStep, fm string) error {
	obj, err := d.Replayer.hydrate(ctx, step)
	if err != nil {
		return err
	}
	replicas, found, err := unstructured.NestedInt64(obj.Object, "spec", "replicas")
	if err != nil || !found {
		return fmt.Errorf(
			"the snapshot for %s %s/%s carries no spec.replicas, so the prior replica count this scale must restore is not in the plan",
			step.Target.Kind, step.Target.Namespace, step.Target.Name)
	}
	if _, err := d.Replayer.Writer.Scale(ctx, step.Target, int32(replicas), fm, true); err != nil { //nolint:gosec // a replica count is bounded by the API's own int32 field
		return fmt.Errorf("scaling %s %s/%s back to %d would not apply: %w",
			step.Target.Kind, step.Target.Namespace, step.Target.Name, replicas, err)
	}
	return nil
}

// dryRunCreate checks the recreate that reverses a delete.
//
// AlreadyExists is the expected answer and not a failure -- see the type doc. It is also, at REPLAY
// time, the refusal that protects a stranger who has taken the name; the two readings do not
// conflict, because they are answers to the same call issued on opposite sides of the delete.
func (d *PlanDryRunner) dryRunCreate(ctx context.Context, step agentv1alpha1.UndoStep, fm string) error {
	obj, err := d.Replayer.hydrate(ctx, step)
	if err != nil {
		return err
	}
	_, err = d.Replayer.Writer.Create(ctx, obj, fm, true)
	switch {
	case err == nil, apierrors.IsAlreadyExists(err):
		return nil
	default:
		return fmt.Errorf("recreating %s %s/%s would not apply: %w",
			step.Target.Kind, step.Target.Namespace, step.Target.Name, err)
	}
}
