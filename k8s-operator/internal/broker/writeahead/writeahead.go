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

// Package writeahead is the production execute.Journal: the confirmation that an ActionRecord is
// really in storage before the broker is allowed to touch a live object.
//
// # The gap this fills
//
// execute.Journal has existed since P9-T1 and had three test stubs and no implementation. With it
// nil, Executor.Journal is nil and execute.Apply refuses every non-dry-run execution outright --
// which is the fail-closed direction and is why the gap was survivable, not why it was acceptable.
// Nothing could execute at all.
//
// # Why this is a read and not a flag
//
// The interface's own doc says it: "a caller can stamp a timestamp, set a flag, or return nil from
// a function named flush; none of those distinguish the record is in etcd from the record is in a
// buffer in this process that is about to be OOM-killed between the write and the mutation". So the
// only implementation that is a check is one that goes and looks.
//
// Three consequences follow, and each of them is a refusal below rather than a comment.
//
// **The read must not be served from a cache.** A cached client answers "did the record land?" from
// a watch that may not have caught up, and worse, may answer YES from a copy of an object that is
// no longer there. cmd/broker already constructs its client with client.New rather than through a
// manager for exactly this reason and says so at the construction site. This package cannot check
// the client it was handed -- a client.Client does not report whether it is cached -- so it checks
// the thing a cache cannot fake: the SERVER-ASSIGNED identity of the object that came back. A UID
// and a resourceVersion are minted by the API server on admission to etcd. An in-process buffer
// echoing back the object it was handed has neither, and that is the exact failure mode the
// interface names. Requiring them is not belt-and-braces; it is the only part of this that a store
// which never stored anything cannot pass. See [[LSN-034]]: a value compared against itself will
// never tell you it is the wrong shape, and a store that reported its own success would be doing
// precisely that.
//
// **A record on its way out is not durable.** A non-nil deletionTimestamp means the object is being
// removed. It is readable, it has a UID, and executing against it would leave a mutation whose
// journal entry disappears moments later -- an unjournaled write arriving by a slower route.
//
// **The record must be the one the name promised.** journal.RecordName derives the object's name
// from the action id, so a Get is really a lookup by derived key. If the object sitting at that key
// carries a different spec.actionId, the derivation and the content disagree, and the broker is
// about to execute action A against action B's journal entry. Cheap to check, and the alternative
// is trusting a join nobody verified.
//
// # The phase arm, which is the one that is about the future
//
// A record whose status label names a phase other than Executing is refused. Empty is allowed --
// journal.Labels omits the label when the phase is unset, and a caller that has not set one is not
// doing anything wrong. Every NAMED phase other than Executing is refused, and PendingApproval is
// the reason the arm exists.
//
// journal.Store.Create folds AlreadyExists into a nil return, deliberately and correctly: the
// record name is derived from the action id, so a retried Create is the same record rather than a
// duplicate, and that is what makes the broker's retry safe without a lock. But it means a nil from
// Create does not prove that THIS call wrote what is now on the server. Today the two writers of a
// record cannot collide -- pipeline step 7 parks a gated action as PendingApproval and returns, and
// step 8 is only reached by an action that was never parked. The moment an approval path re-enters
// the pipeline for an already-parked action, step 8's Create will return nil against the parked
// record, the pre-state it just set will never reach the server, and the executor will mutate live
// objects against a journal entry that carries no snapshot and therefore no undo plan. That is the
// write-ahead rule failing in the only direction that matters.
//
// This arm makes that future fail closed instead of silently. It is deliberately a check on the
// LABEL and not on status.phase: ActionRecord has a status subresource, so client.Create drops
// status entirely and a freshly created record's status.phase is empty on the server no matter what
// the caller set. journal.Labels reads the caller's phase at Create time and writes it into
// metadata, which does survive. Reading status.phase here would have looked more correct and would
// have been vacuous -- it is empty for every record this function will ever see.
package writeahead

import (
	"context"
	"errors"
	"fmt"

	apierrors "k8s.io/apimachinery/pkg/api/errors"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/execute"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

// Reader is the one journal verb this needs. Narrow on purpose, for the reason policy.Lister is
// narrow: 06 §2.2.1 gives the broker get, list and watch on actionrecords, and a dependency typed
// as the whole *journal.Store would make it possible to write a SetPhase here and only discover
// which RBAC verb that needed at L2. *journal.Store satisfies it.
type Reader interface {
	Get(ctx context.Context, namespace, actionID string) (*agentv1alpha1.ActionRecord, error)
}

// Confirmer is the production execute.Journal.
type Confirmer struct {
	// Records reads the journal back. Required.
	Records Reader

	// Namespace is where this broker's records live -- its agent's own namespace, from its own
	// deployment, never from an envelope. A confirmation that read some other namespace would be
	// answering a question nobody asked.
	Namespace string
}

var _ execute.Journal = (*Confirmer)(nil)

// ErrNotDurable is the sentinel every refusal below wraps. The executor turns any error into a
// refusal to mutate, so the distinction is for the operator reading the message rather than for
// control flow -- but a caller that wants to tell "the write-ahead check said no" from "the write-
// ahead check could not run" has errors.Is to do it with.
var ErrNotDurable = errors.New("the action record is not durable")

// ConfirmDurable returns nil only once the record for actionID has been read back from the server
// bearing server-assigned identity.
//
// Every failure path returns an error, including the ones that mean "I could not tell". There is no
// arm here that reports success on an inconclusive read: the caller's next statement mutates a live
// object, and "probably journaled" is not a state the write-ahead rule recognises.
func (c *Confirmer) ConfirmDurable(ctx context.Context, actionID string) error {
	if c == nil || c.Records == nil {
		return fmt.Errorf("%w: no journal reader is configured, so %q cannot be confirmed and nothing may execute", ErrNotDurable, actionID)
	}
	if c.Namespace == "" {
		return fmt.Errorf("%w: no namespace is configured, so there is nowhere to look for %q", ErrNotDurable, actionID)
	}

	ar, err := c.Records.Get(ctx, c.Namespace, actionID)
	if err != nil {
		if apierrors.IsNotFound(err) {
			// The write did not happen, or happened somewhere else. Distinguished from the read
			// failure below because they call for different operator responses: this one is a bug
			// in the caller's ordering, the other is a sick API server.
			return fmt.Errorf("%w: no record %s exists in %s, so the write-ahead write never landed", ErrNotDurable, journal.RecordName(actionID), c.Namespace)
		}
		return fmt.Errorf("%w: reading %s back from %s failed, so durability is unknown and the action must not execute: %w", ErrNotDurable, journal.RecordName(actionID), c.Namespace, err)
	}
	if ar == nil {
		// A reader that returns (nil, nil) is broken, and the failure mode if this were not here is
		// a nil dereference in the arms below -- which would panic the broker mid-request rather
		// than refuse the action.
		return fmt.Errorf("%w: the journal reader returned no record and no error for %s", ErrNotDurable, actionID)
	}

	// Server-assigned identity. The one property an in-process buffer cannot produce.
	if ar.UID == "" || ar.ResourceVersion == "" {
		return fmt.Errorf(
			"%w: the record read back for %s carries uid=%q resourceVersion=%q; both are assigned by the API server, so a record missing either was never admitted to storage",
			ErrNotDurable, actionID, ar.UID, ar.ResourceVersion)
	}
	if ar.DeletionTimestamp != nil {
		return fmt.Errorf("%w: the record for %s is being deleted (deletionTimestamp %s), so the mutation would outlive its journal entry", ErrNotDurable, actionID, ar.DeletionTimestamp.UTC().Format("2006-01-02T15:04:05Z"))
	}
	if ar.Spec.ActionID != actionID {
		return fmt.Errorf(
			"%w: the record at %s/%s carries spec.actionId %q, not %q -- the derived name and the content disagree, so this is some other action's journal entry",
			ErrNotDurable, c.Namespace, journal.RecordName(actionID), ar.Spec.ActionID, actionID)
	}
	if phase := ar.Labels[journal.StatusLabel]; phase != "" && phase != string(agentv1alpha1.PhaseExecuting) {
		return fmt.Errorf(
			"%w: the durable record for %s is in phase %q, not %q -- executing against it would apply changes the record does not describe",
			ErrNotDurable, actionID, phase, agentv1alpha1.PhaseExecuting)
	}
	return nil
}
