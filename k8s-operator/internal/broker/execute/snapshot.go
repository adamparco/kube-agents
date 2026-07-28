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
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/undo"
)

// Step 8 of 03 §4.1: capture pre-state, all of it or none of it.
//
// The atomicity rule is V-BRK-018 and it is the whole reason this is a package-level function over a
// target LIST rather than a method called per target in the apply loop. In a three-target envelope
// where targets 1 and 2 snapshot and target 3 does not, the tempting behaviour is to apply the two
// that are undoable and report the third as failed. That produces a half-applied action whose undo
// plan restores two thirds of it -- a state no operator asked for and no rollback returns from.
// So: if any snapshot fails, NOTHING is applied. CaptureAll returns an error and no snapshots, which
// makes the safe behaviour the only reachable one for a caller that checks its errors.

const (
	// MaxInlineSnapshotBytes is the 1 MiB threshold of 06 §4.3. Above it the body moves to the
	// journal store and the ActionRecord keeps only the digest and the reference.
	//
	// The limit is not aesthetic. An ActionRecord is an etcd object, etcd's default per-object limit
	// is ~1.5 MiB, and a single Secret or ConfigMap at the API server's own 1 MiB ceiling would push
	// a record with three snapshots past it -- at which point the WRITE of the journal entry fails,
	// which by 03 §6 means the action does not execute. Fail-closed is correct, but failing closed
	// because of a size nobody budgeted for is an outage, not a control.
	MaxInlineSnapshotBytes = 1 << 20
)

// Reader reads live objects. Narrow on purpose: snapshotting must not be able to write.
type Reader interface {
	// Get returns the live object, or an error satisfying apierrors.IsNotFound if it is absent.
	Get(ctx context.Context, ref agentv1alpha1.TargetRef) (*unstructured.Unstructured, error)
}

// BodyStore holds snapshot bodies too large to inline. Optional: a broker with no store configured
// can still snapshot anything under the inline limit, and refuses -- loudly -- above it.
type BodyStore interface {
	// Put stores body under a key of its own choosing and returns the reference to record.
	Put(ctx context.Context, actionID string, targetIndex int, body []byte) (*agentv1alpha1.ObjectStoreRef, error)
}

// Snapshot is one target's pre-state, in both the forms the pipeline needs.
type Snapshot struct {
	// TargetIndex is the position in the envelope's operation list.
	TargetIndex int

	// Ref is the target, re-pinned from what was actually read: the UID and resourceVersion here are
	// the ones observed at step 8, which is what an undo precondition must check against.
	Ref agentv1alpha1.TargetRef

	// Existed reports whether the object was there. False is a normal outcome for a create, and is
	// NOT an error -- see CaptureAll.
	Existed bool

	// Live is the UNSANITIZED live object, held in memory only.
	//
	// It never reaches a record. It is what Diff reads (Diff sanitizes internally) and what the undo
	// planner needs to restore a Secret's actual values. Nil when the object did not exist.
	Live *unstructured.Unstructured

	// Record is the sanitized, persistable form. Nil when the object did not exist: there is no
	// pre-state to restore, and an empty snapshot claiming otherwise is worse than its absence.
	Record *agentv1alpha1.PreStateSnapshot

	// Redactions names the Secret keys whose values were replaced by digests, for the record's
	// redaction list.
	Redactions []undo.Redaction
}

// CaptureAll snapshots every target, or fails without snapshotting any.
//
// An absent object is not a failure. A create's target does not exist yet, and a delete whose object
// is already gone is an action with nothing to do; both must be distinguishable from "the API server
// would not answer", which is why apierrors.IsNotFound is the only error narrowed to Existed=false
// and every other error propagates.
//
// store may be nil. A body over MaxInlineSnapshotBytes with no store is an error, not a truncation
// and not a skipped snapshot: 03 §6 says an action whose pre-state cannot be persisted does not run,
// and silently recording a partial body would make the ActionRecord's undo plan a lie.
func CaptureAll(
	ctx context.Context,
	r Reader,
	actionID string,
	targets []agentv1alpha1.TargetRef,
	capturedAt metav1.Time,
	store BodyStore,
) ([]Snapshot, error) {
	if r == nil {
		return nil, fmt.Errorf("snapshot: no reader; pre-state cannot be captured and the action must not execute")
	}
	if actionID == "" {
		return nil, fmt.Errorf("snapshot: no action id; a stored body needs a key that names the action it belongs to")
	}

	out := make([]Snapshot, 0, len(targets))
	for i, ref := range targets {
		snap, err := capture(ctx, r, actionID, i, ref, capturedAt, store)
		if err != nil {
			// Named target, named index: a multi-target failure whose message says only "snapshot
			// failed" sends a human reading the record to the wrong object.
			return nil, fmt.Errorf(
				"snapshot: target %d (%s %s/%s) could not be captured, so none of the %d targets will be applied: %w",
				i, ref.Kind, ref.Namespace, ref.Name, len(targets), err)
		}
		out = append(out, snap)
	}
	return out, nil
}

func capture(
	ctx context.Context,
	r Reader,
	actionID string,
	index int,
	ref agentv1alpha1.TargetRef,
	capturedAt metav1.Time,
	store BodyStore,
) (Snapshot, error) {
	live, err := r.Get(ctx, ref)
	if apierrors.IsNotFound(err) {
		return Snapshot{TargetIndex: index, Ref: ref, Existed: false}, nil
	}
	if err != nil {
		return Snapshot{}, err
	}
	if live == nil {
		// A reader that returns (nil, nil) is a bug in the reader, but the consequence here is an
		// action that executes believing it has a pre-state it does not have.
		return Snapshot{}, fmt.Errorf("the reader returned no object and no error")
	}

	// Re-pin from what was read. The ref handed in was pinned at classification; if the object has
	// been replaced since, the snapshot is of the NEW object and the record must say so, so that the
	// undo precondition compares against the thing that was actually snapshotted.
	pinned := ref
	pinned.UID = string(live.GetUID())
	pinned.ResourceVersion = live.GetResourceVersion()

	clean, redactions, err := undo.Sanitize(live, false)
	if err != nil {
		return Snapshot{}, fmt.Errorf("sanitizing the pre-state: %w", err)
	}
	body, err := json.Marshal(clean.Object)
	if err != nil {
		return Snapshot{}, fmt.Errorf("encoding the sanitized pre-state: %w", err)
	}

	sum := sha256.Sum256(body)
	rec := &agentv1alpha1.PreStateSnapshot{
		TargetIndex: int32(index), //nolint:gosec // a target index is bounded by the envelope's operation cap
		CapturedAt:  capturedAt,
		SHA256:      hex.EncodeToString(sum[:]),
	}

	if len(body) <= MaxInlineSnapshotBytes {
		rec.Object = &runtime.RawExtension{Raw: body}
	} else {
		if store == nil {
			return Snapshot{}, fmt.Errorf(
				"the sanitized body is %d bytes, over the %d-byte inline limit, and no journal store is configured to hold it; the action is refused rather than recorded with a pre-state it does not have",
				len(body), MaxInlineSnapshotBytes)
		}
		refOut, err := store.Put(ctx, actionID, index, body)
		if err != nil {
			return Snapshot{}, fmt.Errorf("storing the %d-byte body: %w", len(body), err)
		}
		if refOut == nil {
			return Snapshot{}, fmt.Errorf("the body store returned no reference and no error")
		}
		if refOut.SHA256 != rec.SHA256 {
			// The store is supposed to digest what it received. A mismatch means it stored something
			// else, and the record would carry a digest that the undo path will later refuse -- at
			// undo time, under pressure, instead of now.
			return Snapshot{}, fmt.Errorf(
				"the body store reports digest %s for a body whose digest is %s", refOut.SHA256, rec.SHA256)
		}
		rec.ObjectRef = refOut
	}

	return Snapshot{
		TargetIndex: index,
		Ref:         pinned,
		Existed:     true,
		Live:        live,
		Record:      rec,
		Redactions:  redactions,
	}, nil
}

// Records returns the persistable snapshots, skipping targets that did not exist.
func Records(snaps []Snapshot) []agentv1alpha1.PreStateSnapshot {
	out := make([]agentv1alpha1.PreStateSnapshot, 0, len(snaps))
	for _, s := range snaps {
		if s.Record != nil {
			out = append(out, *s.Record)
		}
	}
	return out
}
