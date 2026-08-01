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

package broker

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

// Journaling a refusal (06 §4.1).
//
// A refused submission produces an ActionRecord in phase Rejected, and that record is about the
// SUBMISSION, not about a cluster object. Nothing was targeted, classified or written -- the whole
// point is that the broker stopped before any of that -- so the record exists to answer one
// question a reader will eventually ask: "did anything try this, and when?"
//
// The alternative, logging refusals and journaling only successes, fails the case that matters.
// An agent that has been prompt-injected into attempting `"bypass": true` produces no mutation and
// therefore no journal entry, so the journal -- the thing 05 §1.2 makes the durable record --
// would show a quiet week. The attempt is the evidence.

const (
	// RejectedTTL is the 05 §1.2 retention for a Rejected record: 365 days. Longer than routine or
	// elevated, because a refused attempt is only interesting in hindsight and hindsight arrives
	// late.
	RejectedTTL = "8760h"
	// RejectedUndoWindow is zero: there is nothing to undo. The field is required by the CRD and
	// the CEL rule insists the undo window never outlive the record, so it is spelled explicitly
	// rather than left to a default that would silently promise something.
	RejectedUndoWindow = "0h"
)

// RejectionJournal writes the Rejected record for a refused submission.
//
// An interface so the server can be tested without a cluster, and so a broker with no journal
// client wired is a compile-time visible configuration rather than a nil dereference on the
// refusal path -- which is the path least likely to be exercised before production.
type RejectionJournal interface {
	// Reject records the refusal and returns the action id of the record it wrote.
	//
	// The id is returned rather than kept private because 06 §4.4 row 3's auto-pause has to name a
	// record: the broker cannot pause an agent directly (see internal/broker/escalate), so it writes
	// the request onto `status.escalation` of an ActionRecord, and on a refusal the only record that
	// exists is this one. An empty id means no ActionRecord was written -- whether because of an
	// error or because this implementation does not write one -- and a caller must treat it exactly
	// as it treats an error for escalation purposes: there is nothing to hang the escalation on.
	Reject(ctx context.Context, id *Identity, body []byte, ref *Refusal) (string, error)
}

// StoreRejectionJournal is the production implementation, backed by the journal Store.
type StoreRejectionJournal struct {
	Store *journal.Store
	// Namespace is where records are written -- the agent's own namespace.
	Namespace string
	// AgentName is the Agent CR this broker serves.
	AgentName string
	// ActorServiceAccount is the broker's write identity, recorded so the journal says who COULD
	// have written rather than who asked.
	ActorServiceAccount string
	// Now is injectable for tests.
	Now func() time.Time
}

// Reject builds and creates the record.
//
// It never returns the Store's AlreadyExists as an error (Store.Create already folds that away),
// and a genuine failure here is returned to the caller so the server can log it. Note what does
// NOT happen: a journal failure does not upgrade the refusal into a 500. The caller is already
// being refused; turning a journaling problem into a different refusal would tell them something
// untrue about their request, and the refusal itself is unaffected.
func (j *StoreRejectionJournal) Reject(ctx context.Context, id *Identity, body []byte, ref *Refusal) (string, error) {
	if j.Store == nil {
		return "", fmt.Errorf("broker: no journal store configured; the refusal %q was not recorded", ref.Reason)
	}
	now := time.Now
	if j.Now != nil {
		now = j.Now
	}
	at := now().UTC()

	actionID, err := journal.NewULID(at)
	if err != nil {
		return "", fmt.Errorf("broker: mint action id for refusal %q: %w", ref.Reason, err)
	}

	identity := "unauthenticated"
	requester := agentv1alpha1.ActionRequester{
		Kind: agentv1alpha1.RequesterKind("system"),
		ID:   "unauthenticated",
	}
	if id != nil {
		identity = id.AgentIdentity()
		// The calling workload is the only party here whose identity was actually verified. The
		// body may claim a human requester; it is not read, because a refused body is precisely
		// the body we have decided not to trust.
		requester = agentv1alpha1.ActionRequester{
			Kind:     agentv1alpha1.RequesterKind("agent"),
			ID:       id.Username,
			Platform: "mesh",
		}
	}

	source, undoOf := triggerFromBody(body)

	ar := &agentv1alpha1.ActionRecord{
		ObjectMeta: metav1.ObjectMeta{Namespace: j.Namespace},
		Spec: agentv1alpha1.ActionRecordSpec{
			ActionID:            actionID,
			AgentRef:            agentv1alpha1.AgentObjectRef{Name: j.AgentName, Namespace: j.Namespace},
			AgentIdentity:       identity,
			ActorServiceAccount: j.ActorServiceAccount,
			Requester:           requester,
			// Always true on this path. Even a body carrying a signed assertion is unverified
			// here, because we refused before verifying it.
			AttributionUnverified: true,
			Trigger: agentv1alpha1.ActionTrigger{
				Source:  source,
				Detail:  ref.Reason,
				UndoOf:  undoOf,
				ChainID: actionID,
			},
			Trace:          traceFromBody(body),
			Intent:         truncate("REFUSED "+ref.Reason+": "+ref.Detail, MaxIntentLen),
			IdempotencyKey: refusalIdempotencyKey(actionID),
			DryRun:         false,
			Classification: agentv1alpha1.ActionClassification{
				Class: agentv1alpha1.RiskForbidden,
				Reasons: []agentv1alpha1.ClassificationReason{{
					Rule:   "broker/" + ref.Reason,
					Class:  string(agentv1alpha1.RiskForbidden),
					Detail: truncate(ref.Detail, 256),
				}},
				// Nothing was written, so there is nothing to undo. `undoable: false` normally
				// forces a class of at least gated; here the class is already forbidden, so the
				// two agree.
				Undoable:      false,
				PolicySources: []string{"code-floor"},
			},
			Targets:   []agentv1alpha1.TargetRef{refusedTarget()},
			Retention: rejectedRetention(at),
		},
	}
	// Phase is status, and Store.Create writes spec. The status subresource is set by the
	// reconciler; the label below is what makes the record findable as a refusal immediately,
	// before that first reconcile.
	ar.Status.Phase = agentv1alpha1.PhaseRejected
	if err := j.Store.Create(ctx, ar); err != nil {
		return "", err
	}
	return actionID, nil
}

// refusedTarget is the sentinel that satisfies the CRD's `targets` MinItems=1 for a record that
// has no cluster target.
//
// It names the envelope itself rather than inventing a plausible object. A synthetic
// `v1/ConfigMap/unknown` would satisfy the schema and then read, months later, as an attempted
// ConfigMap write that never happened -- a false lead in exactly the investigation this record
// exists to support. Naming a kind that does not exist as a cluster resource makes the sentinel
// unmistakable, and a reader who greps for it finds this comment.
func refusedTarget() agentv1alpha1.TargetRef {
	return agentv1alpha1.TargetRef{
		Group:   "kubeagents.x-k8s.io",
		Version: "v1alpha1",
		Kind:    "ActionEnvelope",
		Name:    "refused-before-target-resolution",
	}
}

// refusalIdempotencyKey derives a key for a record that has no operations to hash.
//
// The CRD requires the `sha256:<64 hex>` shape, and the honest value -- "there was no valid set of
// operations to canonicalize" -- has no spelling in that pattern. Hashing the action id gives a
// well-formed key that is unique per record and can never collide with a real one, since a real
// key is computed over an operations list and this one is not.
func refusalIdempotencyKey(actionID string) string {
	return KeyPrefix + journal.Digest([]byte("refusal:"+actionID))
}

func rejectedRetention(at time.Time) agentv1alpha1.RetentionSpec {
	return agentv1alpha1.RetentionSpec{
		Class:               agentv1alpha1.RiskForbidden,
		TTL:                 RejectedTTL,
		ExpiresAt:           metav1.NewTime(at.Add(365 * 24 * time.Hour)),
		UndoWindow:          RejectedUndoWindow,
		UndoWindowExpiresAt: metav1.NewTime(at),
	}
}

// triggerFromBody reads `trigger` out of a body the broker has already decided to refuse.
//
// Reading anything from a refused body needs justifying. The justification is that 06 §4.3 says
// the trigger is "recorded, never an authority input": a forged trigger mislabels a record and
// grants nothing. Recording the caller's claim is strictly more information than recording a
// default, and the record is already marked attributionUnverified.
//
// The fallback is `delegation`, which is the literal truth of an envelope arriving over the mesh
// from another workload -- and is the only enum value that does not assert a cause we did not
// observe. A claimed `undo` without an `undoOf` falls back too, because the CRD's CEL rule pairs
// the two and a record that fails admission records nothing at all.
func triggerFromBody(body []byte) (agentv1alpha1.ActionTriggerSource, string) {
	const fallback = agentv1alpha1.ActionTriggerSource("delegation")
	if len(body) == 0 {
		return fallback, ""
	}
	var probe struct {
		Trigger struct {
			Source string `json:"source"`
			UndoOf string `json:"undoOf"`
		} `json:"trigger"`
	}
	if err := json.Unmarshal(body, &probe); err != nil {
		return fallback, ""
	}
	if !validTriggerSources[probe.Trigger.Source] {
		return fallback, ""
	}
	if probe.Trigger.Source == string(agentv1alpha1.ActionTriggerUndo) {
		if probe.Trigger.UndoOf == "" || !journal.ValidULID(probe.Trigger.UndoOf) {
			return fallback, ""
		}
		return agentv1alpha1.ActionTriggerUndo, probe.Trigger.UndoOf
	}
	return agentv1alpha1.ActionTriggerSource(probe.Trigger.Source), ""
}

// traceFromBody recovers the correlation ids, for the same reason as the trigger: they are
// correlation, not authority, and a refusal a human cannot tie back to the conversation that
// caused it is a refusal they cannot act on. Only well-formed ids are kept, so a body stuffing
// junk into traceId cannot pollute the telemetry join.
func traceFromBody(body []byte) *agentv1alpha1.ActionTrace {
	if len(body) == 0 {
		return nil
	}
	var probe struct {
		Trace struct {
			TraceID   string `json:"traceId"`
			SpanID    string `json:"spanId"`
			SessionID string `json:"sessionId"`
		} `json:"trace"`
	}
	if err := json.Unmarshal(body, &probe); err != nil {
		return nil
	}
	if !hex32Re.MatchString(probe.Trace.TraceID) {
		return nil
	}
	return &agentv1alpha1.ActionTrace{
		TraceID:   probe.Trace.TraceID,
		SpanID:    probe.Trace.SpanID,
		SessionID: truncate(probe.Trace.SessionID, 128),
	}
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

// AgentIdentity is the `<tier>/<scope>` key the journal indexes on. Built from the broker's
// configuration, never from a request.
func (i *Identity) AgentIdentity() string {
	if i.Scope == "" {
		return string(i.Tier)
	}
	return string(i.Tier) + "/" + i.Scope
}

// LogRejectionJournal records refusals to a log only. It exists for the same reason MemorySink
// does in the journal package -- so a broker under test has a wired journal -- and is deliberately
// NOT selectable at runtime: main wires the Store, and a flag that could swap it would be a
// supported way to run a broker whose refusals are not durable.
type LogRejectionJournal struct {
	Records []string
}

// Reject appends a one-line summary.
//
// It returns an empty action id, and truthfully: a log line is not an ActionRecord, so there is no
// object for an escalation to be recorded on. A broker wired this way refuses exactly as it always
// did and logs that the auto-pause had nowhere to go, which is the honest behaviour for a journal
// that is not durable in the first place.
func (j *LogRejectionJournal) Reject(_ context.Context, id *Identity, _ []byte, ref *Refusal) (string, error) {
	who := "unauthenticated"
	if id != nil {
		who = id.Username
	}
	j.Records = append(j.Records, who+" "+ref.Reason+" "+ref.Detail)
	return "", nil
}
