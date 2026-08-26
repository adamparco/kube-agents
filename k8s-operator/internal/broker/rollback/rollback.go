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

// Package rollback is the production verify.Rollbacker: the thing that actually replays an undo
// plan against a real API server.
//
// # The plan was written to a contract, and this package is the other party
//
// `internal/broker/undo` generates the plan before the action runs, and its safety argument is not
// self-contained. Three times, in its own comments, it justifies a decision by describing what the
// REPLAYER will do -- and until this package existed, all three were promises made to nobody:
//
//   - On the uid precondition: "the uid is what makes a restore safe to replay minutes or hours
//     later. Without it, 'apply this snapshot to Deployment team-x/api-gateway' is a name lookup,
//     and a name is reused." So the plan pins the uid and stops. Enforcing it is here.
//   - On the recreate step, which deliberately carries NO precondition: "the object is gone, so
//     there is no uid to match... What protects this step instead is that `create` fails if
//     something already holds the name -- which is the same guarantee arriving through the API
//     server rather than through the plan." That guarantee only exists if the replay is a genuine
//     `create`. Server-side apply has no such rule: at a name someone else has taken it MERGES the
//     snapshot into their object, reports success, and leaves behind something that is neither the
//     pre-state nor what the stranger had (see replayCreate for the measured behaviour).
//   - On a Secret whose values the sanitizer replaced with digests: "the restorable material lives
//     in the journal store and is verified against those digests on replay."
//
// The third one is the reason this package refuses more often than it acts. There IS no restorable
// material: `execute.Snapshot.Live` -- the only unredacted copy -- is documented as "held in memory
// only. It never reaches a record", and the body store receives the sanitized form. So the step's
// body carries, in `data`, the literal strings "sha256:<hex>". Replaying it does not fail. It
// SUCCEEDS, and every value in the Secret becomes the hex of its own digest, and every pod that
// mounts it gets sixty-four characters of garbage where its database password used to be -- from
// an operation whose entire purpose was to put things back. That is the single worst thing in this
// package's blast radius and it is one `Apply` call away, which is why the check is a refusal at
// the front door rather than a caveat in a doc comment.
//
// # What this package will not do
//
// It does not re-derive anything. There is no dry-run pass (verify.Rollbacker says so: "one bounded
// attempt with no dry-run pass -- the state it is restoring was captured before the action, and
// re-deriving it here would be a second opinion about the same bytes"), no strategy selection, and
// no repair. Every decision was made at plan time by a component that could see the pre-state; this
// one either carries the decision out exactly or refuses it and says which promise it could not
// keep.
//
// It also does not continue past a failure. A plan is an ordered sequence chosen to restore one
// coherent state, and half of it is a state nobody requested -- the same rule as the snapshot
// atomicity of V-BRK-018, arriving at the other end of the action.
package rollback

import (
	"context"
	"errors"
	"fmt"
	"strings"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/execute"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/undo"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

// Writer is the API-server-facing seam the replay needs.
//
// It is declared here rather than reused from `execute.Applier` for one reason: `Create`. The
// executor never needs it -- an agent's `create` verb goes through server-side apply like every
// other write -- so adding it to `execute.Applier` would put a method on an interface that no
// caller in that package uses, and every fake would grow a body nothing exercises. But a RECREATE
// is precisely the operation that must fail when the name is taken, and apply is precisely the
// operation that does not. The seam is narrow, it is here, and the compile-time assertion below is
// what keeps it and the production client from drifting apart.
type Writer interface {
	// Create makes the object, failing with IsAlreadyExists if the name is taken.
	Create(ctx context.Context, obj *unstructured.Unstructured, fieldManager string, dryRun bool) (*unstructured.Unstructured, error)
	// Apply performs a server-side apply.
	Apply(ctx context.Context, obj *unstructured.Unstructured, fieldManager string, dryRun bool) (*unstructured.Unstructured, error)
	// Scale sets replicas through the scale subresource.
	Scale(ctx context.Context, ref agentv1alpha1.TargetRef, replicas int32, fieldManager string, dryRun bool) (*unstructured.Unstructured, error)
	// Delete removes the object, honouring the UID precondition.
	Delete(ctx context.Context, ref agentv1alpha1.TargetRef, opts execute.DeleteOpts, dryRun bool) error
}

var _ Writer = (*execute.ClientApplier)(nil)

// Replayer is the production verify.Rollbacker.
type Replayer struct {
	// Writer performs the mutations, with the ACTOR identity -- the same credential the action
	// used. A rollback that needed more authority than the action it reverses would be a privilege
	// escalation wearing a safety label.
	Writer Writer

	// Reader re-reads the live object so a precondition is checked against the cluster rather than
	// against the plan's own copy of what it expected to find.
	Reader execute.Reader

	// Sink hydrates a step whose body was too large to inline. Nil is legal -- a broker with no
	// store configured can still replay every inline step -- and a step that needs it then refuses
	// by name rather than by nil dereference.
	Sink journal.BlobSink
}

// Rollback replays every step of the plan, in order, stopping at the first failure.
//
// agentIdentity is the `<tier>/<scope>` key, and it is a PARAMETER rather than a field on the
// Replayer because the field manager derived from it is security-relevant (V-BRK-019: the manager
// is exactly `kube-agents/<tier>/<scope>`, and `contested` is a string comparison against it). A
// Replayer constructed once with a static identity would write a manager string that is right until
// the day the process serves a second identity, and wrong silently thereafter. LSN-031.
func (r *Replayer) Rollback(ctx context.Context, actionID, agentIdentity string, plan agentv1alpha1.UndoPlan) error {
	if r.Writer == nil {
		return fmt.Errorf("rollback: no Writer is configured, so the undo plan for %s cannot be replayed", actionID)
	}
	if r.Reader == nil {
		// Not optional. Every precondition in this package is a comparison against live state, and
		// a Replayer that cannot read is one that can only trust the plan -- which is the name
		// lookup the plan's uid pin exists to prevent.
		return fmt.Errorf("rollback: no Reader is configured, so no precondition in the undo plan for %s could be checked", actionID)
	}

	// Re-validated here even though verify.Driver.attemptRollback already did it. This is not
	// belt-and-braces: the OTHER caller of this package is the undo controller's replay path
	// (05 §1.3), which arrives from a human command rather than from the driver, and a validator
	// that only runs on one of two paths is a validator that will be discovered missing on the
	// other one during an incident.
	if err := undo.ValidateReplayable(&plan); err != nil {
		return fmt.Errorf("rollback: the undo plan for %s is not replayable: %w", actionID, err)
	}

	fm, err := execute.FieldManager(agentIdentity)
	if err != nil {
		return fmt.Errorf("rollback: %w", err)
	}

	for i, step := range plan.Steps {
		if err := r.replayStep(ctx, step, fm); err != nil {
			// The count of already-applied steps is in the message because it is the first thing a
			// human needs and the hardest thing to reconstruct afterwards: it is the difference
			// between "nothing happened" and "the world is now in a state neither the action nor
			// the undo intended".
			return fmt.Errorf(
				"rollback: undo plan for %s stopped at step %d of %d (%d step(s) already replayed, and they are NOT reverted): %w",
				actionID, i+1, len(plan.Steps), i, err)
		}
	}
	return nil
}

// replayStep dispatches one step.
//
// The accepted set is exactly undo.ValidateReplayable's, and `TestTheReplayerImplementsEveryOp`
// asserts that mechanically rather than by reading. The two used to disagree by construction:
// ValidateReplayable's default arm reads "op %q, which the replayer does not implement", written
// when there was no replayer to ask. LSN-040 is what a shared word meaning two things costs.
func (r *Replayer) replayStep(ctx context.Context, step agentv1alpha1.UndoStep, fieldManager string) error {
	switch step.Op {
	case "delete":
		return r.replayDelete(ctx, step)
	case "apply":
		return r.replayApply(ctx, step, fieldManager)
	case "scale":
		return r.replayScale(ctx, step, fieldManager)
	case "create":
		return r.replayCreate(ctx, step, fieldManager)
	default:
		return fmt.Errorf("op %q has no replay implementation", step.Op)
	}
}

// replayDelete reverses a create.
//
// The uid precondition is mandatory and ValidateReplayable already refuses a plan without one; it
// is re-checked here because this function is what would do the damage, and a guard that lives one
// package away from the operation it guards is a guard someone will route around.
//
// A NotFound is SUCCESS, and that is the one branch worth arguing. The step's goal is that the
// object the action created no longer exists; if it is already gone, the goal holds and erroring
// would turn a satisfied post-condition into a failed rollback -- which, at rung 3, pages a human
// and pauses the agent. The case is not hypothetical: a rollback races the owner's garbage
// collector whenever the created object had an owner that the same failure took out. What the
// NotFound cannot be is the wrong object, because if something else held the name the uid
// precondition would have made this a Conflict instead.
func (r *Replayer) replayDelete(ctx context.Context, step agentv1alpha1.UndoStep) error {
	if step.Preconditions == nil || step.Preconditions.UID == "" {
		return fmt.Errorf(
			"deleting %s %s/%s has no uid precondition: a delete by name alone removes whatever holds that name now",
			step.Target.Kind, step.Target.Namespace, step.Target.Name)
	}
	err := r.Writer.Delete(ctx, step.Target, execute.DeleteOpts{UID: step.Preconditions.UID}, false)
	switch {
	case err == nil:
		return nil
	case apierrors.IsNotFound(err):
		return nil
	default:
		return fmt.Errorf("deleting %s %s/%s: %w", step.Target.Kind, step.Target.Namespace, step.Target.Name, err)
	}
}

// replayApply reverses an update, a patch, or a cloud-provider change: it puts the pre-state back.
func (r *Replayer) replayApply(ctx context.Context, step agentv1alpha1.UndoStep, fieldManager string) error {
	obj, err := r.hydrate(ctx, step)
	if err != nil {
		return err
	}
	if err := r.requireSameObject(ctx, step); err != nil {
		return err
	}
	if _, err := r.Writer.Apply(ctx, obj, fieldManager, false); err != nil {
		return fmt.Errorf("restoring %s %s/%s: %w", step.Target.Kind, step.Target.Namespace, step.Target.Name, err)
	}
	return nil
}

// replayScale reverses a scale, through the scale subresource, using the replica count in the
// snapshot body.
//
// It does NOT apply the whole snapshot, even though the body is right there and applying it would
// certainly restore the replica count too. `scale` is a separate op because the PLANNER chose it as
// a separate op, and it chose it knowing something this function does not: that the action changed
// one field. Replaying the whole body would additionally revert every field some other manager has
// legitimately changed in the meantime -- a rollback with a blast radius larger than the action it
// reverses, which is the one shape a recovery step must never have.
func (r *Replayer) replayScale(ctx context.Context, step agentv1alpha1.UndoStep, fieldManager string) error {
	obj, err := r.hydrate(ctx, step)
	if err != nil {
		return err
	}
	replicas, found, err := unstructured.NestedInt64(obj.Object, "spec", "replicas")
	if err != nil || !found {
		return fmt.Errorf(
			"the snapshot for %s %s/%s carries no spec.replicas, so the prior replica count this scale must restore is not in the plan",
			step.Target.Kind, step.Target.Namespace, step.Target.Name)
	}
	if err := r.requireSameObject(ctx, step); err != nil {
		return err
	}
	if _, err := r.Writer.Scale(ctx, step.Target, int32(replicas), fieldManager, false); err != nil { //nolint:gosec // a replica count is bounded by the API's own int32 field
		return fmt.Errorf("scaling %s %s/%s back to %d: %w",
			step.Target.Kind, step.Target.Namespace, step.Target.Name, replicas, err)
	}
	return nil
}

// replayCreate reverses a delete.
//
// Create, not apply, and the difference is the whole of the recreate step's safety. The plan carries
// no uid precondition for a recreate -- it cannot, the uid died with the object -- and the planner's
// comment names its replacement: "`create` fails if something already holds the name".
//
// Server-side apply has no equivalent rule, and what it does instead was measured against a real API
// server rather than assumed (TestARecreateIntoATakenNameThroughApplyIsNeitherARefusalNorARestore).
// Both of its outcomes are worse than a refusal:
//
//   - Where the snapshot's fields do NOT collide with the stranger's, the apply SUCCEEDS. The two
//     objects are merged. What is left is neither the pre-state nor what the stranger had, and the
//     ActionRecord says the undo completed.
//   - Where they do collide, ClientApplier does not force ownership -- deliberately, because a
//     conflict is the `contested` signal of 03 §6 -- so the apply fails with a field-ownership
//     conflict. Loud, but describing the wrong problem: the operator is told about managedFields
//     when what happened is that the object they are restoring no longer exists.
//
// AlreadyExists is the only one of the three that says the true thing, so the create is the one that
// is used, and this function turns it into a sentence rather than passing the bare API error up.
func (r *Replayer) replayCreate(ctx context.Context, step agentv1alpha1.UndoStep, fieldManager string) error {
	obj, err := r.hydrate(ctx, step)
	if err != nil {
		return err
	}
	if _, err := r.Writer.Create(ctx, obj, fieldManager, false); err != nil {
		if apierrors.IsAlreadyExists(err) {
			return fmt.Errorf(
				"recreating %s %s/%s: something already holds that name, and it is not the object this action deleted (its uid died with it); refusing rather than overwriting it: %w",
				step.Target.Kind, step.Target.Namespace, step.Target.Name, err)
		}
		return fmt.Errorf("recreating %s %s/%s: %w", step.Target.Kind, step.Target.Namespace, step.Target.Name, err)
	}
	return nil
}

// requireSameObject enforces the plan's uid pin against live state.
//
// Three refusals, and the middle one is the one a reader will want to argue with:
//
//   - No pin at all. The planner writes an EMPTY precondition when the target it was given carried
//     no uid, and ValidateReplayable only demands a pin for `delete`. This package demands one for
//     every step that writes to an existing object, which makes it strictly stricter than the
//     validator that admitted the plan. That divergence is deliberate and it is recorded as a
//     finding rather than smoothed over: the honest fix is to tighten ValidateReplayable so such a
//     plan downgrades to `none` at GENERATION time and the action gates, and changing a validator
//     to suit the component that just failed against it is the coupling PROTOCOL §10.1 forbids
//     inside one unit of work.
//   - Absent. The plan chose `apply`, which means it believed the object exists; server-side apply
//     would CREATE it instead, turning a restore into a recreate -- an operation the planner
//     considered separately, and would have downgraded to `none` had an inbound reference existed.
//     Silently promoting one to the other discards that decision.
//   - Replaced. The name resolves and the uid does not match, which is the whole hazard the pin
//     names.
func (r *Replayer) requireSameObject(ctx context.Context, step agentv1alpha1.UndoStep) error {
	if step.Preconditions == nil || step.Preconditions.UID == "" {
		return fmt.Errorf(
			"the plan pins no uid for %s %s/%s, so replaying it would be a lookup by name, and a name is reused",
			step.Target.Kind, step.Target.Namespace, step.Target.Name)
	}
	live, err := r.Reader.Get(ctx, step.Target)
	if apierrors.IsNotFound(err) {
		return fmt.Errorf(
			"%s %s/%s no longer exists; this plan step restores an object's prior state and cannot bring it back, which is a different operation with a different safety argument",
			step.Target.Kind, step.Target.Namespace, step.Target.Name)
	}
	if err != nil {
		return fmt.Errorf("reading %s %s/%s to check the plan's uid precondition: %w",
			step.Target.Kind, step.Target.Namespace, step.Target.Name, err)
	}
	if got := string(live.GetUID()); got != step.Preconditions.UID {
		return fmt.Errorf(
			"%s %s/%s has uid %s but the plan pinned %s: the object holding that name was replaced after the action, and restoring this snapshot onto it would overwrite something nobody asked to change",
			step.Target.Kind, step.Target.Namespace, step.Target.Name, got, step.Preconditions.UID)
	}
	return nil
}

// hydrate produces the body a step writes, and refuses it if it is not the pre-state.
//
// Everything here is a comparison against something the plan itself asserts, because the plan is
// the only witness left: the object it describes has already been changed, and there is nothing
// live to check the body against.
func (r *Replayer) hydrate(ctx context.Context, step agentv1alpha1.UndoStep) (*unstructured.Unstructured, error) {
	raw, err := r.rawBody(ctx, step)
	if err != nil {
		return nil, err
	}

	// Unstructured's own UnmarshalJSON, NOT encoding/json into obj.Object. They differ in one way
	// that matters: encoding/json decodes every JSON number to float64, and the whole unstructured
	// contract is that integers are int64. Decoded the wrong way, `spec.replicas` becomes a
	// float64, unstructured.NestedInt64 reports it as ABSENT rather than as the wrong type, and the
	// scale step -- whose only job is to restore that number -- would refuse every plan it was
	// handed while blaming the planner for omitting a field that is right there in the bytes.
	obj := &unstructured.Unstructured{}
	if err := obj.UnmarshalJSON(raw); err != nil {
		return nil, fmt.Errorf("the snapshot body for %s %s/%s is not valid JSON: %w",
			step.Target.Kind, step.Target.Namespace, step.Target.Name, err)
	}

	if err := bodyAddressesTarget(obj, step.Target); err != nil {
		return nil, err
	}
	if err := bodyIsSanitized(obj); err != nil {
		return nil, fmt.Errorf("the snapshot body for %s %s/%s did not come from the 06 §4.3.1 sanitizer: %w",
			step.Target.Kind, step.Target.Namespace, step.Target.Name, err)
	}
	if keys := undo.RedactedSecretKeys(obj); len(keys) > 0 {
		return nil, fmt.Errorf(
			"REFUSING to restore Secret %s/%s: %d value(s) in this snapshot are digest placeholders, not material -- %s. "+
				"The plan's caveat says the restorable material 'lives in the journal store and is verified against those digests on replay'; "+
				"it does not, because the only unredacted copy is execute.Snapshot.Live, which never leaves memory. "+
				"Applying this body would replace each value with the hex of its own digest",
			obj.GetNamespace(), obj.GetName(), len(keys), strings.Join(keys, ", "))
	}
	return obj, nil
}

// rawBody resolves the inline or out-of-band body.
//
// The out-of-band path re-verifies the digest, exactly as journal.LoadSnapshot does and for the
// same reason: the bytes travelled through a store this process does not own, and hashing something
// already in memory is a cheap way to turn a silent wrong-world restore into a refusal.
//
// The INLINE path cannot do that, and the asymmetry is a gap rather than a decision. UndoStep.Object
// is a bare RawExtension with no digest beside it, while PreStateSnapshot -- the same body, in the
// same record, one field away -- carries a required sha256. An inline body is therefore trusted
// because it is small. Recorded as a finding; closing it is a CRD field addition, which is not this
// unit's work.
func (r *Replayer) rawBody(ctx context.Context, step agentv1alpha1.UndoStep) ([]byte, error) {
	switch {
	case step.Object != nil && len(step.Object.Raw) > 0:
		return step.Object.Raw, nil

	case step.ObjectRef != nil:
		if r.Sink == nil {
			return nil, fmt.Errorf(
				"the snapshot for %s %s/%s lives in store %q but this broker has no blob sink configured",
				step.Target.Kind, step.Target.Namespace, step.Target.Name, step.ObjectRef.Store)
		}
		if step.ObjectRef.Store != r.Sink.Name() {
			return nil, fmt.Errorf(
				"the snapshot for %s %s/%s lives in store %q but the configured sink is %q",
				step.Target.Kind, step.Target.Namespace, step.Target.Name, step.ObjectRef.Store, r.Sink.Name())
		}
		body, err := r.Sink.Get(ctx, step.ObjectRef.Key)
		if err != nil {
			return nil, fmt.Errorf("reading snapshot %q from sink %q: %w", step.ObjectRef.Key, r.Sink.Name(), err)
		}
		if got := journal.Digest(body); got != step.ObjectRef.SHA256 {
			return nil, fmt.Errorf(
				"the snapshot for %s %s/%s digests to %s but the plan says %s; refusing to replay a body that changed",
				step.Target.Kind, step.Target.Namespace, step.Target.Name, got, step.ObjectRef.SHA256)
		}
		return body, nil

	default:
		return nil, fmt.Errorf("the step for %s %s/%s carries neither an inline body nor an objectRef",
			step.Target.Kind, step.Target.Namespace, step.Target.Name)
	}
}

// bodyAddressesTarget refuses a step whose body and whose target are different objects.
//
// `Target` is what the precondition was checked against, what the plan prints for a human, and what
// the record says the undo touched. `Object` is what the API server actually receives. Nothing in
// the type system ties them together, so a plan can pin the uid of one object and write another --
// and every artifact a reviewer looks at afterwards would name the first one.
func bodyAddressesTarget(obj *unstructured.Unstructured, ref agentv1alpha1.TargetRef) error {
	wantAPIVersion := ref.Version
	if ref.Group != "" {
		wantAPIVersion = ref.Group + "/" + ref.Version
	}
	switch {
	case obj.GetKind() != ref.Kind:
		return fmt.Errorf("the plan targets kind %s but its body is a %s", ref.Kind, obj.GetKind())
	case obj.GetAPIVersion() != wantAPIVersion:
		return fmt.Errorf("the plan targets apiVersion %s but its body is %s", wantAPIVersion, obj.GetAPIVersion())
	case obj.GetName() != ref.Name:
		return fmt.Errorf("the plan targets %s but its body is named %s", ref.Name, obj.GetName())
	case obj.GetNamespace() != ref.Namespace:
		return fmt.Errorf("the plan targets namespace %q but its body is in %q", ref.Namespace, obj.GetNamespace())
	}
	return nil
}

// bodyIsSanitized refuses a body carrying fields the API server owns.
//
// undo.Sanitize drops every one of these, and its doc explains what each would do on replay:
// `resourceVersion` makes the apply a conflicting update, `uid` is rejected outright on a create,
// `managedFields` restores an ownership graph that no longer describes reality. Their presence
// therefore means the body did not come from the sanitizer -- which means something assembled a
// plan step by hand, and the interesting question is not what this field will do but what else
// that path skipped.
func bodyIsSanitized(obj *unstructured.Unstructured) error {
	var present []string
	for _, f := range undo.DroppedMetadataFields() {
		if _, found, _ := unstructured.NestedFieldNoCopy(obj.Object, "metadata", f); found {
			present = append(present, "metadata."+f)
		}
	}
	if len(present) > 0 {
		return errors.New("it carries server-owned field(s) " + strings.Join(present, ", "))
	}
	return nil
}
