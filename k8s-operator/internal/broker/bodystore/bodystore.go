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

// Package bodystore is the production execute.BodyStore: the adapter that puts a snapshot body too
// large to inline into the journal's blob sink and returns the ObjectStoreRef the record carries.
//
// # Why an adapter and not a direct call
//
// The two sides already exist and disagree about their signatures on purpose. execute.BodyStore
// takes (actionID, targetIndex) because the executor knows nothing about agent identity or sink
// layout; journal.BlobSink takes a flat key because a sink is a key-value store and should not know
// what an action is. The layout that joins them -- identity, action, target -- is journal's, and
// this package is where the executor's coordinates are turned into it by CALLING journal's own key
// function rather than by re-deriving the same string.
package bodystore

import (
	"context"
	"errors"
	"fmt"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/execute"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

// Journal is the production execute.BodyStore, backed by a journal.BlobSink.
//
// # It does not digest the body, and that is the point
//
// execute.capture computes the body's SHA-256 for the record and then compares it against the digest
// this Put returns, refusing the whole action on a mismatch. That comparison is only worth making if
// the two numbers have independent provenance: a store that hashed the bytes it was handed and
// returned its own answer would be handing back a value the caller is about to compare with itself,
// which is [[LSN-034]] exactly -- "a value compared against itself will never tell you it is the
// wrong shape". So this type returns what the SINK reported, unaltered, and lets the mismatch
// surface. journal.BlobSink's contract requires the sink to digest what it actually stored, which is
// the fact the comparison is trying to establish: not "did I hash this right" but "is what came to
// rest in the bucket the thing I sent".
//
// A sink that returns a well-formed digest without storing anything defeats this, and no in-process
// check can catch that. What catches it is undo: journal.LoadSnapshot re-verifies the digest against
// the bytes it reads back, and refuses to replay a plan whose body does not match.
type Journal struct {
	// Sink is where bodies land. Required: see Put.
	Sink journal.BlobSink

	// AgentIdentity is the first level of the key layout, so a human reading sink keys can see
	// which agent produced what without joining anything (journal.SnapshotKeyPrefix). Required, and
	// checked, because an empty identity silently collapses every agent's bodies into one prefix --
	// a cross-tenant key collision that looks like a formatting slip.
	AgentIdentity string
}

var _ execute.BodyStore = (*Journal)(nil)

// Put stores body and returns the reference the ActionRecord records.
//
// # Every failure here refuses the action, and none of them truncates
//
// 03 §6's fail-closed rule applied to the snapshot rather than to the record: an action executed
// without a recoverable pre-state is an action that cannot be undone, on a record that claims it
// can. execute.capture propagates this error, execute.CaptureAll turns it into a whole-envelope
// refusal, and the pipeline's step 3 stops before anything is classified -- so a sink outage costs
// availability for over-limit snapshots and never costs undoability. A nil Sink is the same
// refusal one layer earlier: execute.capture already refuses an over-limit body when no store is
// configured at all, and a store configured with no sink behind it must not be the thing that turns
// that refusal into a success.
func (j *Journal) Put(ctx context.Context, actionID string, targetIndex int, body []byte) (*agentv1alpha1.ObjectStoreRef, error) {
	if j.Sink == nil {
		return nil, errors.New("no blob sink is configured behind this body store; a pre-state that cannot be persisted refuses the action (03 §6)")
	}
	if j.AgentIdentity == "" {
		return nil, errors.New("no agent identity: sink keys are prefixed by identity, and an empty prefix would file one agent's snapshot bodies under another's")
	}
	if actionID == "" {
		return nil, errors.New("no action id: a stored body needs a key that names the action it belongs to")
	}
	if targetIndex < 0 {
		return nil, fmt.Errorf("target index %d is negative; the key would name a target that is not in the envelope", targetIndex)
	}
	if len(body) == 0 {
		// Reachable only from a caller that lost the body between marshalling and storing, which is
		// exactly the caller whose record would otherwise claim a persisted pre-state that is zero
		// bytes long. Refusing costs an action; accepting costs the undo.
		return nil, errors.New("the body is empty; a zero-byte pre-state is not a snapshot and must not be recorded as one")
	}

	key := journal.SnapshotKey(journal.SnapshotKeyPrefix(j.AgentIdentity, actionID), targetIndex)
	stored, err := j.Sink.Put(ctx, key, body)
	if err != nil {
		return nil, fmt.Errorf("persisting the %d-byte pre-state for target %d to sink %q: %w (the action must not execute)",
			len(body), targetIndex, j.Sink.Name(), err)
	}
	if stored == "" {
		// A sink that stored the body and reported no digest leaves the record with an empty
		// sha256, which the CRD's pattern rejects at admission -- late, after the sink write, and
		// reported as a validation error rather than as the sink fault it is.
		return nil, fmt.Errorf("sink %q accepted the body for %q and reported no digest; the reference cannot be recorded without one", j.Sink.Name(), key)
	}

	return &agentv1alpha1.ObjectStoreRef{
		Store: j.Sink.Name(),
		Key:   key,
		// The SINK's digest, not a locally recomputed one. See the type comment.
		SHA256: stored,
	}, nil
}
