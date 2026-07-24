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
	"errors"
)

// ErrNoSuchTarget means the message resolved to a well-formed routing key, but no live Agent CR occupies
// it (unknown/undeployed target). A deterministic refusal — the router names only agents that exist.
var ErrNoSuchTarget = errors.New("router: no agent is deployed for that handle")

// Outcome is the terminal result of routing one turn, returned alongside the error (if any) so callers
// and tests can branch on exactly what happened without string-matching. Every Outcome is also emitted
// as an AuditRecord.
type Outcome struct {
	Resolution Resolution
	// Target is the resolved target; zero value if resolution/lookup failed before one was found.
	Target Target
	// Decision is the before-dispatch authorization result; zero value if the turn never reached authz.
	Decision Decision
	// Dispatched is true only if Authorize allowed the turn AND the dispatcher succeeded.
	Dispatched bool
}

// Gateway is the ChatOps front door (05 C15, 06 §2b). It composes the four decoupled pieces so the
// security-critical ORDER is guaranteed in one place and cannot be reordered by a caller:
//
//	resolve (name the target)  →  lookup (find the live CR)  →  Authorize (BEFORE dispatch)  →  dispatch
//
// Authorize always runs before Dispatch, reading the TARGET's allowlist — routing is never an authz
// signal (03 §4a). A refusal at any step returns without dispatching and is audited. The Gateway holds
// no cluster client; it reads only the in-memory Index the read-only Reconciler maintains.
type Gateway struct {
	// Resolver maps text → handle (deterministic; Phase 2 spends no inference).
	Resolver *Resolver
	// Index is the routing table (agentindex key → Target).
	Index *Index
	// Dispatch delivers authorized messages. Required.
	Dispatch Dispatcher
	// ProjectID is the router's GCP project context, needed to turn a cluster-admin handle (which carries
	// only the cluster leaf) into a full routing key. Platform handles ignore it.
	ProjectID string
	// Audit receives one record per turn (defaults to a no-op sink if nil).
	Audit AuditSink
}

// Handle routes one inbound message end to end. It returns the Outcome and, on any refusal/failure, a
// non-nil error naming the deterministic cause. Dispatch is reached ONLY when Authorize allows the turn.
func (g *Gateway) Handle(ctx context.Context, msg Message) (Outcome, error) {
	audit := g.Audit
	if audit == nil {
		audit = nopAuditSink{}
	}

	var out Outcome

	// 1. Resolve: name the target (deterministic; no inference in Phase 2).
	res, err := g.Resolver.Resolve(ctx, msg.Text)
	out.Resolution = res
	if err != nil {
		audit.Record(ctx, AuditRecord{Sender: msg.Sender, Mode: res.Mode, Handle: res.Handle.Canonical(), Reason: err.Error()})
		return out, err
	}

	// 2. Route key + index lookup: find the live CR that owns this (tier, scope). Same key the webhook
	//    enforces uniqueness on, so at most one CR can match.
	key, err := res.Handle.RouteKey(g.ProjectID)
	if err != nil {
		audit.Record(ctx, AuditRecord{Sender: msg.Sender, Mode: res.Mode, Handle: res.Handle.Canonical(), Reason: err.Error()})
		return out, err
	}
	target, ok := g.Index.Lookup(key)
	if !ok {
		audit.Record(ctx, AuditRecord{Sender: msg.Sender, Mode: res.Mode, Handle: res.Handle.Canonical(), Identity: key, Reason: ErrNoSuchTarget.Error()})
		return out, ErrNoSuchTarget
	}
	out.Target = target

	// 3. Authorize BEFORE dispatch: read the TARGET's allowlist, fail-closed. This is the single
	//    enforcement point that fronts every per-tier pod.
	dec := Authorize(target, msg.Sender)
	out.Decision = dec
	if !dec.Allowed {
		audit.Record(ctx, AuditRecord{Sender: msg.Sender, Mode: res.Mode, Handle: target.Handle, Identity: target.Identity, Allowed: false, Reason: dec.Reason})
		return out, ErrUnauthorized
	}

	// 4. Dispatch: deliver only now that the turn is authorized.
	if err := g.Dispatch.Dispatch(ctx, target, msg); err != nil {
		audit.Record(ctx, AuditRecord{Sender: msg.Sender, Mode: res.Mode, Handle: target.Handle, Identity: target.Identity, Allowed: true, Dispatched: false, Reason: "dispatch failed: " + err.Error()})
		return out, err
	}
	out.Dispatched = true
	audit.Record(ctx, AuditRecord{Sender: msg.Sender, Mode: res.Mode, Handle: target.Handle, Identity: target.Identity, Allowed: true, Dispatched: true, Reason: dec.Reason})
	return out, nil
}

// ErrUnauthorized is returned when Authorize refuses the turn before dispatch (Phase 2 acceptance d).
// The specific cause is in Outcome.Decision.Reason and the audit record.
var ErrUnauthorized = errors.New("router: sender is not authorized for the target (refused before dispatch)")
