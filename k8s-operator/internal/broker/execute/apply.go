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

package execute

import (
	"context"
	"fmt"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// Step 9 of 03 §4.1: execute, via server-side apply, with the agent's field manager, dry-run first
// where supported.
//
// The ordering here is the load-bearing part, and it is three separate rules that a naive
// implementation collapses into one loop:
//
//   - WRITE-AHEAD (V-REV-002). The ActionRecord is durable BEFORE the first mutation. Not written,
//     not queued -- durable and re-readable. An action that mutates the cluster and then fails to
//     journal is an unattributable change, which is the one outcome the journal exists to make
//     impossible.
//   - DRY RUN BEFORE ANY REAL APPLY, FOR EVERY TARGET. Not "dry run each target then apply it";
//     all dry runs and all integrity checks complete before the first real mutation. A three-target
//     envelope whose third target fails its integrity check must not have applied the first two.
//     This is the same rule as the snapshot atomicity of V-BRK-018 and it is here for the same
//     reason: a half-applied action is a state nobody requested.
//   - INTEGRITY BEFORE APPLY (V-BRK-020). The comparison is against the server's own answer about
//     what it would do, which is why it has to happen between the dry run and the apply rather than
//     anywhere more convenient.
//
// What this file does NOT do is verify the result or recover from a failure. That is the ladder of
// 04 §5, and it lives in the verify package: a mid-sequence failure in the apply pass returns the
// partial Result alongside the error precisely so the recovery ladder has something to roll back.

// Applier is the API-server-facing seam.
//
// Each mutating method returns the object AS THE SERVER SAYS IT WOULD BE (dry run) or as it is
// (real apply). That return value is the input to the executed diff, and it must come from the
// server rather than from anything this process computed -- the entire point of V-BRK-020 is that
// the broker's model of what a payload does is exactly the thing not to trust.
type Applier interface {
	// Apply performs a server-side apply of obj with the given field manager.
	Apply(ctx context.Context, obj *unstructured.Unstructured, fieldManager string, dryRun bool) (*unstructured.Unstructured, error)

	// Patch applies a patch of the given media type and returns the resulting object.
	Patch(ctx context.Context, ref agentv1alpha1.TargetRef, patchType string, body []byte, fieldManager string, dryRun bool) (*unstructured.Unstructured, error)

	// Scale sets replicas through the scale subresource and returns the resulting PARENT object.
	//
	// The parent, not the Scale: the executed diff is a comparison of the target object before and
	// after, and a Scale object diffed against a Deployment reports every field of both as changed.
	// An implementation that can only see the Scale composes replicas onto the live object -- which
	// is exact for this verb, because the scale subresource cannot change anything else.
	Scale(ctx context.Context, ref agentv1alpha1.TargetRef, replicas int32, fieldManager string, dryRun bool) (*unstructured.Unstructured, error)

	// Delete removes the object, honouring the UID precondition when set.
	Delete(ctx context.Context, ref agentv1alpha1.TargetRef, opts DeleteOpts, dryRun bool) error

	// SupportsDryRun reports whether this target's API honours dry-run.
	//
	// "Where supported" (03 §4.1 step 9) is a real qualifier: aggregated API servers and some CRD
	// webhooks do not implement it. A false answer here does not skip the integrity check -- it
	// changes what the check compares against, and the Outcome records which it was.
	SupportsDryRun(ctx context.Context, ref agentv1alpha1.TargetRef) bool
}

// DeleteOpts is the subset of delete options the broker forwards. Defined here rather than reused
// from the envelope package so that this package depends only on the API types: an executor that
// imports the HTTP layer is an executor that cannot be tested without one.
type DeleteOpts struct {
	PropagationPolicy  string
	GracePeriodSeconds *int64

	// UID, when set, is the precondition that refuses the delete if the object was replaced. It is
	// filled from the SNAPSHOT's re-pinned ref, not from the envelope: the envelope's pin is from
	// classification time, and the object the broker is about to delete is the one it snapshotted.
	UID string
}

// Journal is the durability seam for the write-ahead rule.
type Journal interface {
	// ConfirmDurable returns nil only once the ActionRecord for actionID is readable back from
	// storage.
	//
	// Re-reading is the only version of this that is a check. A caller can stamp a timestamp, set a
	// flag, or return nil from a function named "flush"; none of those distinguish "the record is in
	// etcd" from "the record is in a buffer in this process that is about to be OOM-killed between
	// the write and the mutation" -- which is the exact window the rule exists to close.
	ConfirmDurable(ctx context.Context, actionID string) error
}

// Op is one operation to execute, already classified.
type Op struct {
	// Index is the position in the envelope's operation list, and the key that ties this op to its
	// snapshot, its classification and its record entry.
	Index int

	// Verb is one of create, apply, patch, scale, delete.
	Verb string

	// Ref is the target. For an existing object this is the snapshot's re-pinned ref.
	Ref agentv1alpha1.TargetRef

	// Desired is the object to apply, for create and apply.
	Desired *unstructured.Unstructured

	// PatchType and PatchBody carry the payload for patch.
	PatchType string
	PatchBody []byte

	// Replicas is the payload for scale.
	Replicas *int32

	// DeleteOpts is the payload for delete.
	DeleteOpts DeleteOpts

	// Classified is what the classifier was shown for this op, for the integrity check.
	Classified Classified
}

// Request is one action's worth of execution.
type Request struct {
	// ActionID names the ActionRecord this execution is journalled under.
	ActionID string

	// AgentIdentity is the `<tier>/<scope>` key the field manager is built from.
	AgentIdentity string

	// Ops are the operations, in envelope order.
	Ops []Op

	// Snapshots are the pre-states from CaptureAll, keyed by TargetIndex.
	Snapshots []Snapshot

	// DryRunOnly stops after the dry-run pass. It is how `dryRun: true` envelopes and shadow mode
	// (P9-T8) execute: every check runs, every diff is recorded, nothing is mutated.
	DryRunOnly bool
}

// Outcome is what happened to one op.
type Outcome struct {
	Index int
	Verb  string

	// DryRunUsed reports whether the executed diff came from the server's dry run or from the
	// submitted desired state.
	//
	// Recorded rather than assumed, because the difference is the strength of the integrity check.
	// A false here means the check compared the classified paths against what the broker BELIEVES
	// the payload does -- which catches a caller whose classification disagrees with its own
	// payload, but not a server-side merge that expands. Silently degrading from one to the other
	// is how a control becomes decorative.
	DryRunUsed bool

	// Diff is the executed diff: (pre-state, what the server says it would become).
	Diff DiffResult

	// Applied is the record entry, set only after a real mutation.
	Applied *agentv1alpha1.AppliedTarget
}

// Result is the execution's output. It is returned even on error, populated as far as execution got.
type Result struct {
	// FieldManager is the manager string every apply in this action used.
	FieldManager string

	// Outcomes are in envelope order.
	Outcomes []Outcome

	// DryRunOnly echoes the request, so a reader of the result cannot mistake a dry run for an apply.
	DryRunOnly bool

	// Mutated reports whether ANY real mutation was issued. It is what the recovery ladder reads to
	// decide whether there is anything to roll back, and it is set before the first mutating call
	// rather than after it -- a call that times out may well have landed.
	Mutated bool
}

// Executor runs step 9.
type Executor struct {
	Applier Applier
	Journal Journal
}

// Execute dry-runs every op, checks integrity on every op, and only then applies.
//
// On error the returned Result is non-nil and describes exactly how far execution got, including
// Mutated. A caller that discards the Result on error discards the rollback input.
func (e *Executor) Execute(ctx context.Context, req Request) (*Result, error) {
	res := &Result{DryRunOnly: req.DryRunOnly}

	if e == nil || e.Applier == nil {
		return res, fmt.Errorf("execute: no applier configured")
	}
	if req.ActionID == "" {
		return res, fmt.Errorf("execute: no action id; an execution that cannot be journalled must not run (03 §6)")
	}
	fm, err := FieldManager(req.AgentIdentity)
	if err != nil {
		return res, err
	}
	res.FieldManager = fm

	snaps := make(map[int]Snapshot, len(req.Snapshots))
	for _, s := range req.Snapshots {
		snaps[s.TargetIndex] = s
	}

	// Pass 1 -- dry run and integrity, for every op, before any mutation.
	res.Outcomes = make([]Outcome, 0, len(req.Ops))
	for _, op := range req.Ops {
		snap, ok := snaps[op.Index]
		if !ok {
			return res, fmt.Errorf(
				"execute: op %d (%s) has no snapshot; step 8 either did not run for it or ran under a different index, and executing without a pre-state produces an action that cannot be undone",
				op.Index, op.Verb)
		}
		if op.Classified.TargetIndex != op.Index {
			// The classification is carried per-op precisely so it cannot be misaligned; if it is,
			// the integrity check would compare one op's effect against another op's permission.
			return res, fmt.Errorf(
				"execute: op %d carries a classification for target %d",
				op.Index, op.Classified.TargetIndex)
		}

		outcome, err := e.preflight(ctx, op, snap, fm)
		res.Outcomes = append(res.Outcomes, outcome)
		if err != nil {
			return res, err
		}
	}

	if req.DryRunOnly {
		return res, nil
	}

	// Write-ahead: durable record, then mutate. Between these two lines is the only ordering in the
	// broker that cannot be recovered from if it is wrong.
	if e.Journal == nil {
		return res, fmt.Errorf("execute: no journal configured; the write-ahead rule (V-REV-002) cannot be satisfied and the action must not execute")
	}
	if err := e.Journal.ConfirmDurable(ctx, req.ActionID); err != nil {
		return res, fmt.Errorf("execute: the action record for %s is not durable, so nothing will be applied: %w", req.ActionID, err)
	}

	// Pass 2 -- mutate.
	res.Mutated = true
	for i, op := range req.Ops {
		snap := snaps[op.Index]
		applied, err := e.mutate(ctx, op, snap, fm)
		if err != nil {
			return res, fmt.Errorf("execute: op %d (%s %s %s/%s) failed after %d applied: %w",
				op.Index, op.Verb, op.Ref.Kind, op.Ref.Namespace, op.Ref.Name, i, err)
		}
		res.Outcomes[i].Applied = applied
	}
	return res, nil
}

// preflight dry-runs one op and checks the result against its classification.
func (e *Executor) preflight(ctx context.Context, op Op, snap Snapshot, fm string) (Outcome, error) {
	out := Outcome{Index: op.Index, Verb: op.Verb}

	var would *unstructured.Unstructured
	switch op.Verb {
	case "delete":
		// A delete needs no dry run to know its effect: the object goes away, so the executed diff
		// is the removal of everything in the pre-state. Dry-running it would answer a different
		// question (would the API server accept it), which is worth asking but is not this check.
		would = nil
		if !snap.Existed {
			// Deleting what is already gone. Nothing to diff, nothing to check; the mutate pass
			// still issues the delete so the API server, not the broker, decides.
			return out, nil
		}
	default:
		if !e.Applier.SupportsDryRun(ctx, op.Ref) {
			// Degrade honestly: check against the intended state and say so in the record.
			w, err := e.intended(op, snap)
			if err != nil {
				return out, err
			}
			would = w
			break
		}
		w, err := e.callVerb(ctx, op, fm, true)
		if err != nil {
			return out, fmt.Errorf("execute: op %d (%s) failed its dry run: %w", op.Index, op.Verb, err)
		}
		would = w
		out.DryRunUsed = true
	}

	diff, err := Diff(snap.Live, would)
	if err != nil {
		return out, fmt.Errorf("execute: op %d (%s): computing the executed diff: %w", op.Index, op.Verb, err)
	}
	out.Diff = diff

	if err := CheckIntegrity(op.Classified, diff); err != nil {
		return out, fmt.Errorf("execute: %w", err)
	}
	return out, nil
}

// intended is the fallback "would become" for a target whose API does not honour dry-run.
//
// It deliberately handles only the verbs whose effect is fully determined without the server:
// create and apply send a whole object, and scale sets one field. patch is NOT among them -- the
// server-side merge of a patch is exactly the computation this package refuses to model, and
// guessing it here would produce an integrity check that passes on the payload the check exists to
// catch. So a patch against a dry-run-less API is refused.
func (e *Executor) intended(op Op, snap Snapshot) (*unstructured.Unstructured, error) {
	switch op.Verb {
	case "create", "apply":
		if op.Desired == nil {
			return nil, fmt.Errorf("execute: op %d (%s) has no desired state", op.Index, op.Verb)
		}
		return op.Desired, nil
	case "scale":
		if op.Replicas == nil {
			return nil, fmt.Errorf("execute: op %d (scale) has no replica count", op.Index)
		}
		if snap.Live == nil {
			return nil, fmt.Errorf("execute: op %d (scale) has no pre-state to scale", op.Index)
		}
		w := snap.Live.DeepCopy()
		if err := unstructured.SetNestedField(w.Object, int64(*op.Replicas), "spec", "replicas"); err != nil {
			return nil, fmt.Errorf("execute: op %d (scale): %w", op.Index, err)
		}
		return w, nil
	case "patch":
		return nil, fmt.Errorf(
			"execute: op %d patches %s %s/%s, whose API does not support dry-run; the broker will not model a server-side merge itself, so the action is refused rather than executed with an integrity check that cannot see what the merge would do",
			op.Index, op.Ref.Kind, op.Ref.Namespace, op.Ref.Name)
	default:
		return nil, fmt.Errorf("execute: op %d has unknown verb %q", op.Index, op.Verb)
	}
}

// mutate performs the real change and builds the record entry.
func (e *Executor) mutate(ctx context.Context, op Op, snap Snapshot, fm string) (*agentv1alpha1.AppliedTarget, error) {
	entry := &agentv1alpha1.AppliedTarget{
		TargetIndex: int32(op.Index), //nolint:gosec // bounded by the envelope's operation cap
	}

	if op.Verb == "delete" {
		opts := op.DeleteOpts
		if opts.UID == "" {
			// Pin from the snapshot. Deleting by name alone races with a recreate: the object the
			// broker looked at and the object it deletes can be different objects with the same name.
			opts.UID = snap.Ref.UID
		}
		if err := e.Applier.Delete(ctx, op.Ref, opts, false); err != nil {
			return nil, err
		}
		diff, err := Diff(snap.Live, nil)
		if err != nil {
			return nil, err
		}
		entry.Diff = diff.Ops
		return entry, nil
	}

	after, err := e.callVerb(ctx, op, fm, false)
	if err != nil {
		return nil, err
	}
	diff, err := Diff(snap.Live, after)
	if err != nil {
		return nil, err
	}
	entry.Diff = diff.Ops
	if after != nil {
		entry.ResourceVersionAfter = after.GetResourceVersion()
	}
	return entry, nil
}

// callVerb dispatches one mutating verb, in either dry-run or real mode. One dispatch, used by both
// passes, so a verb cannot be dry-run through one code path and applied through another.
func (e *Executor) callVerb(ctx context.Context, op Op, fm string, dryRun bool) (*unstructured.Unstructured, error) {
	switch op.Verb {
	case "create", "apply":
		if op.Desired == nil {
			return nil, fmt.Errorf("op %d (%s) has no desired state", op.Index, op.Verb)
		}
		return e.Applier.Apply(ctx, op.Desired, fm, dryRun)
	case "patch":
		if len(op.PatchBody) == 0 {
			return nil, fmt.Errorf("op %d (patch) has an empty body", op.Index)
		}
		return e.Applier.Patch(ctx, op.Ref, op.PatchType, op.PatchBody, fm, dryRun)
	case "scale":
		if op.Replicas == nil {
			return nil, fmt.Errorf("op %d (scale) has no replica count", op.Index)
		}
		return e.Applier.Scale(ctx, op.Ref, *op.Replicas, fm, dryRun)
	case "delete":
		// Reached only if a caller routes a delete here; the passes handle deletes explicitly.
		return nil, fmt.Errorf("op %d: delete is not applied through callVerb", op.Index)
	default:
		return nil, fmt.Errorf("op %d has unknown verb %q", op.Index, op.Verb)
	}
}
