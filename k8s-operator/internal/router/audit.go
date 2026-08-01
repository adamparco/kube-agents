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

package router

import (
	"context"

	"github.com/go-logr/logr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// AuditRecord is the attributable trace of one routing decision (06 §2b: every chat turn is audited;
// invariant: every action is attributable). It captures WHO (Sender), the target and HOW it was named
// (Handle + Mode + Tier), the conversation it belongs to (ThreadID), and the OUTCOME (Allowed/Dispatched/
// Clarify + Reason) — deliberately NOT the raw message text, so audit logs don't accumulate chat
// contents/secrets. One record is emitted per turn, including refusals, so a refused-before-dispatch turn
// is as visible as a delivered one.
type AuditRecord struct {
	// Sender is the requester's platform id (attribution). Empty for an unidentified sender.
	Sender string
	// Mode is the resolution mode attempted (slash/handle/sticky/inference) — recorded even on refusal.
	Mode Mode
	// Handle is the canonical @handle resolved/attempted (empty if resolution failed before a handle formed).
	Handle string
	// Identity is the target's (tier,scope) key if the turn resolved to a live agent.
	Identity string
	// Tier is the resolved target's tier (attribution: which tier of agent handled/refused the turn). Empty
	// when the turn was refused before any target was resolved.
	Tier agentv1alpha1.AgentTier
	// ThreadID is the thread-affinity key the turn belonged to (conversation correlation). Never an authz
	// input; carried purely so an operator can trace a whole conversation across turns.
	ThreadID string
	// TraceID is the per-turn correlation id (Phase 5 T-A, 06 §8): the same id the dispatcher stamps as the
	// kage_trace_id attribute and the agent echoes onto the Action Envelope it submits. Recording it here is
	// what ties the requester (Sender) to the exact turn a later broker-executed mutation traces back to,
	// via that mutation's ActionRecord.trace.traceId (acceptance d).
	TraceID string
	// Allowed is the before-dispatch authorization decision.
	Allowed bool
	// Dispatched is true only if the message was actually delivered to the target topic.
	Dispatched bool
	// Clarify is true when the turn ended in a clarifying question (ambiguous handle or low-confidence NL) —
	// a deterministic refusal-to-guess, distinct from an authz denial. Never dispatched.
	Clarify bool
	// Reason is the human-readable cause of the outcome (allow reason, deny reason, or error summary).
	Reason string
}

// AuditSink receives one AuditRecord per routing decision. Implementations must not block the routing
// path for long; the default logr sink just logs.
type AuditSink interface {
	Record(ctx context.Context, rec AuditRecord)
}

// LogAuditSink writes each record to a logr.Logger at a fixed message. It is the production sink; a
// structured log line per turn is the Phase-2 audit trail (a durable sink can replace it later without
// touching the gateway, which depends only on the AuditSink interface).
type LogAuditSink struct {
	Log logr.Logger
}

// Record logs the audit fields as structured key/values.
func (s LogAuditSink) Record(_ context.Context, rec AuditRecord) {
	s.Log.Info("chatops route",
		"sender", rec.Sender,
		"mode", string(rec.Mode),
		"handle", rec.Handle,
		"identity", rec.Identity,
		"tier", string(rec.Tier),
		"threadID", rec.ThreadID,
		"traceID", rec.TraceID,
		"allowed", rec.Allowed,
		"dispatched", rec.Dispatched,
		"clarify", rec.Clarify,
		"reason", rec.Reason,
	)
}

// nopAuditSink drops records; used when no sink is configured so the gateway never nil-panics.
type nopAuditSink struct{}

func (nopAuditSink) Record(context.Context, AuditRecord) {}
