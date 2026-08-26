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

// Package escalate is the broker's half of rung 5.
//
// 04 §5.1: "A rollback that itself fails is an immediate page, not a retry loop. The agent is
// auto-paused, because the system can no longer keep its core promise." Two effects, and the broker
// can perform NEITHER of them.
//
// 06 §2.2.1 gives every broker identity `get, list, watch` on `agents` and no verb whatsoever on
// `events`. Pausing is a write to an `Agent`; paging is an Event. V-BRK-013 asserts that grant
// EXACTLY and is BLOCKING-ALWAYS, so "widen the grant so the pauser works" is not an available move
// -- it is the change PROTOCOL §10.2 exists to forbid. This is not an oversight in the grant either.
// 05 §1.7 states the invariant the grant is protecting: "exactly one code path that stops an agent".
// A broker that pauses directly is a second one, and the whole point of routing C-AD's anomaly trips
// through C-BR rather than letting the detector pause is that a brake with two owners has no owner.
//
// So this package writes the escalation DOWN, into the one surface the broker does own: the action's
// own journal entry, through `actionrecords/status`, which step 11 already writes. C-BR (05 §1.5)
// reads it and performs the pause and the page from the operator's identity.
//
// WHAT THIS COSTS, STATED PLAINLY. The pause is now asynchronous. Between the broker recording the
// request and C-BR acting on it, the agent is live and could submit another envelope. That window is
// real and it is the price of the invariant; the alternative prices are a second brake path or a
// widened grant, and both are worse. Two things bound it: the window is one controller reconcile,
// and the action that opened it has already been rolled back or has failed to roll back -- so what
// is live is an agent whose last action is known-bad, not one nobody is watching. P9-T7c-3c-ii-b-2
// owns closing the loop; the honest statement of the residual risk belongs here, where somebody
// deciding to "just add a client.Patch on the Agent" will read it.
//
// WHAT A FAILURE HERE MEANS. Every method returns its error and the driver surfaces it. A silent
// page is the failure mode the entire rung exists to prevent, and an escalation that could not be
// written is indistinguishable, from the outside, from one that was never requested -- so it must
// never be swallowed. `verify.Driver` treats a nil Pager or Pauser as an error for the same reason.
package escalate

import (
	"context"
	"fmt"
	"strings"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/verify"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

// maxReason is the schema bound on `status.escalation.reason`, mirrored here so the recorder can
// truncate rather than hand the API server a body it will reject.
//
// Truncating is the right trade and it is not obvious, so: the reason is a diagnostic, and the
// escalation is a brake. Failing the write because the diagnostic was long would trade the brake
// for the diagnostic. The tail is the part that gets cut, because these strings are built as
// "<what happened>: <underlying error>" and the front carries the classification.
const maxReason = 512

// Recorder writes rung 5 into the action's journal entry. It implements both verify.Pager and
// verify.Pauser -- one type rather than two because both write the same field on the same object
// under the same identity, and splitting them would mean two re-read-and-update cycles racing each
// other on one record.
type Recorder struct {
	// Client reads and writes ActionRecord status. It must be the BROKER's client: the whole point
	// of this package is that the write happens under an identity that cannot pause anything.
	Client client.Client
	// Namespace is where this broker's records live. A broker serves exactly one agent
	// (pipeline.Config: "AgentName, Namespace and ActorServiceAccount identify the agent this broker
	// serves"), and a record lives in that agent's namespace, so this is a constructor-time fact and
	// not a per-call argument. Wire it from the same `cfg.Namespace` the journal writes with; a
	// second source for it is a second thing that can disagree.
	Namespace string
	// Now is injectable for tests. nil means time.Now.
	Now func() time.Time
}

var (
	_ verify.Pager  = (*Recorder)(nil)
	_ verify.Pauser = (*Recorder)(nil)
)

func (r *Recorder) now() time.Time {
	if r.Now != nil {
		return r.Now()
	}
	return time.Now()
}

// Page records that rung 5 asked for a human.
func (r *Recorder) Page(ctx context.Context, p verify.PageRequest) error {
	// The rollback error is carried into the reason rather than into a field of its own. A human
	// woken up by this needs to know what failed to restore, and a separate field would be one more
	// place for the two to disagree; the summary is already built from the same error upstream.
	reason := p.Summary
	if p.RollbackError != "" && !strings.Contains(reason, p.RollbackError) {
		reason = fmt.Sprintf("%s (rollback error: %s)", reason, p.RollbackError)
	}
	return r.record(ctx, p.ActionID, p.AgentIdentity, reason, func(e *agentv1alpha1.ActionEscalation) {
		e.PageRequested = true
	})
}

// Pause records that rung 5 asked for the brake.
func (r *Recorder) Pause(ctx context.Context, p verify.PauseRequest) error {
	return r.record(ctx, p.ActionID, p.AgentIdentity, p.Reason, func(e *agentv1alpha1.ActionEscalation) {
		e.PauseRequested = true
	})
}

// record is the shared re-read-mutate-write cycle.
//
// It re-reads before writing for the same reason journal.Store.SetPhase does: the broker and the
// controllers both touch this record's status, so conflicts are routine rather than exceptional. It
// does NOT retry a conflict, because both callers are on the driver's one-attempt path -- a rung is
// climbed once (04 §5's ladder is non-decreasing), and a retry loop here would be the retry loop
// 04 §5.1 forbids, wearing a different hat. A conflict surfaces as an error the driver reports.
func (r *Recorder) record(
	ctx context.Context,
	actionID, agentIdentity, reason string,
	set func(*agentv1alpha1.ActionEscalation),
) error {
	if actionID == "" {
		// Not a defensive check. The escalation lives ON the record, so an empty actionID has no
		// destination -- and the failure mode without this line is a Get on the empty name, whose
		// NotFound reads as "the record was deleted" rather than "the caller passed nothing".
		return fmt.Errorf("escalate: no actionId on the escalation for %q: rung 5 has nowhere to be recorded", agentIdentity)
	}

	// journal.RecordName, not the actionID. An actionID is an uppercase ULID and is not a legal
	// object name at all, so getting this wrong does not produce a subtle mismatch -- it produces a
	// 404 on a record that exists, which reads exactly like the record having been deleted. Deriving
	// it here rather than taking a name from the caller keeps one definition site: the driver knows
	// action IDs, and the mapping from an action to its journal entry belongs to the journal.
	var live agentv1alpha1.ActionRecord
	key := types.NamespacedName{Namespace: r.Namespace, Name: journal.RecordName(actionID)}
	if err := r.Client.Get(ctx, key, &live); err != nil {
		if apierrors.IsNotFound(err) {
			return fmt.Errorf("escalate: no ActionRecord %s to record the escalation on: "+
				"the agent stays live and nobody is paged (04 §5.1)", key)
		}
		return fmt.Errorf("escalate: re-read %s before recording the escalation: %w", key, err)
	}

	if live.Status.Escalation == nil {
		live.Status.Escalation = &agentv1alpha1.ActionEscalation{}
	}
	e := live.Status.Escalation
	set(e)

	// The reason is set on every call rather than only on the first, so that a Page followed by a
	// Pause -- the order the driver uses -- leaves the record carrying the pause's wording, which is
	// what ends up in `Agent.spec.operations.pauseReason`. Both are built from the same governing
	// cause upstream, so this is a choice about phrasing and not about truth.
	if reason != "" {
		e.Reason = truncate(reason, maxReason)
	}
	if e.RequestedAt == nil {
		// First writer wins. The two calls are milliseconds apart and the timestamp is answering
		// "when did rung 5 happen", which has one answer per action.
		t := metav1.NewTime(r.now())
		e.RequestedAt = &t
	}

	if err := r.Client.Status().Update(ctx, &live); err != nil {
		return fmt.Errorf("escalate: recording the escalation on %s: %w", key, err)
	}
	return nil
}

// truncate cuts to n runes, not bytes: the reason is operator-facing text and a byte cut can split a
// multi-byte character, which the API server accepts and a terminal renders as a replacement glyph
// in the middle of an incident message.
func truncate(s string, n int) string {
	rs := []rune(s)
	if len(rs) <= n {
		return s
	}
	const ellipsis = "…"
	return string(rs[:n-len([]rune(ellipsis))]) + ellipsis
}
