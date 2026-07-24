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
	"fmt"
	"strings"
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

	// 1. Resolve: name the target. The deterministic core (modes 1/2) runs first and never spends
	//    inference. Only on ErrNeedsInference (no slash/handle match) does the gateway escalate to the
	//    mode-3 core, handing it the LIVE handle menu so a hallucinated handle cannot survive. The
	//    inferred handle then flows through the SAME lookup→authorize→dispatch spine as modes 1/2.
	res, err := g.Resolver.Resolve(ctx, msg.Text)
	if errors.Is(err, ErrNeedsInference) {
		res, err = g.Resolver.Infer(ctx, msg.Text, g.Index.KnownHandles())
	}
	out.Resolution = res
	if err != nil {
		audit.Record(ctx, AuditRecord{Sender: msg.Sender, Mode: res.Mode, Handle: res.Handle.Canonical(), Reason: err.Error()})
		return out, err
	}

	// 2. Index-assisted lookup: find the live CR(s) this handle names. Platform/cluster-admin resolve to
	//    the single occupant of their exact RouteKey; a developer-team handle (namespace leaf only) is
	//    resolved via the byTierLeaf secondary index — its cluster/project come from the matched CR, never
	//    the handle. The router names only agents that exist and never guesses between several:
	//      0 matches → ErrNoSuchTarget · 1 → route · >1 → clarify (ask which, never pick one).
	targets, err := g.Index.LookupHandle(res.Handle, g.ProjectID)
	if err != nil {
		audit.Record(ctx, AuditRecord{Sender: msg.Sender, Mode: res.Mode, Handle: res.Handle.Canonical(), Reason: err.Error()})
		return out, err
	}
	switch len(targets) {
	case 0:
		audit.Record(ctx, AuditRecord{Sender: msg.Sender, Mode: res.Mode, Handle: res.Handle.Canonical(), Reason: ErrNoSuchTarget.Error()})
		return out, ErrNoSuchTarget
	case 1:
		out.Target = targets[0]
	default:
		ce := clarifyForAmbiguousHandle(res.Handle, targets)
		audit.Record(ctx, AuditRecord{Sender: msg.Sender, Mode: res.Mode, Handle: res.Handle.Canonical(), Reason: ce.Error()})
		return out, ce
	}
	target := out.Target

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

// clarifyForAmbiguousHandle builds the *ClarifyError returned when a handle names more than one live
// agent (today only the multi-cluster developer-team case: one namespace present in several clusters).
// The handle spelling is identical for every match — that is precisely the ambiguity — so the candidate
// menu carries that one handle per match while the human-facing Reason names the distinguishing scope
// identities, giving the reviewer enough to re-issue an unambiguous address. The router never guesses.
func clarifyForAmbiguousHandle(h Handle, targets []Target) *ClarifyError {
	ids := make([]string, 0, len(targets))
	cands := make([]Candidate, 0, len(targets))
	for _, t := range targets {
		ids = append(ids, t.Identity)
		cands = append(cands, Candidate{Handle: h, Confidence: 1})
	}
	return &ClarifyError{
		Reason: fmt.Sprintf("%s matches %d agents (%s); re-address the specific one",
			h.Canonical(), len(targets), strings.Join(ids, ", ")),
		Candidates: cands,
	}
}
