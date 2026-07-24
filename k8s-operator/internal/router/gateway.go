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
	// Clarify is non-nil when the turn ended in a clarifying question — an ambiguous @handle or a
	// low-confidence NL turn (the router refuses to guess, 06 §2b). It carries the candidate menu so the
	// caller can inspect what was asked; it is a deterministic refusal, never a dispatch.
	Clarify *ClarifyError
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
	// Affinity is the optional thread→agent binding store (06 §6). When set, a bare message on a bound
	// thread routes to that agent (ModeSticky) WITHOUT spending inference, and every authorized dispatch
	// (re)binds the thread. Nil disables affinity (the Phase-2 posture): every turn resolves from scratch.
	Affinity AffinityStore
	// Replier is the optional seam that delivers a clarifying question back to the human when the router
	// refuses to guess (ambiguous @handle or low-confidence NL). Nil means the question is audited but not
	// sent — the Phase-3 posture; the real Google Chat outbound reply wires in with Phase 5.
	Replier Replier
}

// Replier delivers a clarifying question back to the human when the router refuses to guess — an
// ambiguous @handle or a low-confidence NL turn (06 §2b: the router asks which agent, it never picks
// one). It is an OPTIONAL seam: when the gateway's Replier is nil the question is still audited but not
// sent, which is the Phase-3 posture (the real Google Chat outbound reply lands with the Phase-5
// inference proxy). A Replier MUST NOT make any access decision — by the time it runs the turn has
// already been refused-before-dispatch; it only surfaces the candidate menu so the human can re-address
// unambiguously.
type Replier interface {
	Clarify(ctx context.Context, msg Message, ce *ClarifyError) error
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
	//    inference. On ErrNeedsInference (no slash/handle match) the gateway first consults thread
	//    affinity — a bare follow-up on a bound thread routes stickily with NO inference (ModeSticky) —
	//    and only if there is no live binding does it escalate to the mode-3 core, handing it the LIVE
	//    handle menu so a hallucinated handle cannot survive. Deterministic resolution always wins over a
	//    binding; the inferred handle flows through the SAME lookup→authorize→dispatch spine as modes 1/2.
	res, err := g.Resolver.Resolve(ctx, msg.Text)
	sticky := false
	if errors.Is(err, ErrNeedsInference) {
		if t, ok := g.stickyTarget(msg); ok {
			// A live binding names a target: route to it directly, spending no inference.
			out.Target = t
			res, err, sticky = Resolution{Mode: ModeSticky}, nil, true
		} else {
			res, err = g.Resolver.Infer(ctx, msg.Text, g.Index.KnownHandles())
		}
	}
	out.Resolution = res
	if err != nil {
		// A low-confidence NL turn comes back from Infer as a *ClarifyError: route it through the same
		// clarify surface as an ambiguous @handle (audit Clarify==true, ask the human) rather than the
		// generic-refusal path. Any other error is a terminal refusal with no candidate menu.
		var ce *ClarifyError
		if errors.As(err, &ce) {
			g.emitClarify(ctx, audit, &out, msg, res, ce)
			return out, err
		}
		audit.Record(ctx, AuditRecord{Sender: msg.Sender, Mode: res.Mode, Handle: res.Handle.Canonical(), Tier: res.Handle.Tier, ThreadID: msg.ThreadID, TraceID: msg.TraceID, Reason: err.Error()})
		return out, err
	}

	// 2. Index-assisted lookup: find the live CR(s) this handle names. Skipped on the sticky path, which
	//    already holds a live Target from the binding. Platform/cluster-admin resolve to the single
	//    occupant of their exact RouteKey; a developer-team handle (namespace leaf only) is resolved via
	//    the byTierLeaf secondary index — its cluster/project come from the matched CR, never the handle.
	//    The router names only agents that exist and never guesses between several:
	//      0 matches → ErrNoSuchTarget · 1 → route · >1 → clarify (ask which, never pick one).
	if !sticky {
		targets, err := g.Index.LookupHandle(res.Handle, g.ProjectID)
		if err != nil {
			audit.Record(ctx, AuditRecord{Sender: msg.Sender, Mode: res.Mode, Handle: res.Handle.Canonical(), Tier: res.Handle.Tier, ThreadID: msg.ThreadID, TraceID: msg.TraceID, Reason: err.Error()})
			return out, err
		}
		switch len(targets) {
		case 0:
			audit.Record(ctx, AuditRecord{Sender: msg.Sender, Mode: res.Mode, Handle: res.Handle.Canonical(), Tier: res.Handle.Tier, ThreadID: msg.ThreadID, TraceID: msg.TraceID, Reason: ErrNoSuchTarget.Error()})
			return out, ErrNoSuchTarget
		case 1:
			out.Target = targets[0]
		default:
			ce := clarifyForAmbiguousHandle(res.Handle, targets)
			g.emitClarify(ctx, audit, &out, msg, res, ce)
			return out, ce
		}
	}
	target := out.Target

	// 3. Authorize BEFORE dispatch: read the TARGET's allowlist, fail-closed. This runs on EVERY path,
	//    including sticky — a thread binding routes but never authorizes, so a follow-up from a sender who
	//    is not on the bound agent's allowlist is still refused here, and the binding is NOT refreshed.
	dec := Authorize(target, msg.Sender)
	out.Decision = dec
	if !dec.Allowed {
		audit.Record(ctx, AuditRecord{Sender: msg.Sender, Mode: res.Mode, Handle: target.Handle, Identity: target.Identity, Tier: target.Tier, ThreadID: msg.ThreadID, TraceID: msg.TraceID, Allowed: false, Reason: dec.Reason})
		return out, ErrUnauthorized
	}

	// 4. Dispatch: deliver only now that the turn is authorized.
	if err := g.Dispatch.Dispatch(ctx, target, msg); err != nil {
		audit.Record(ctx, AuditRecord{Sender: msg.Sender, Mode: res.Mode, Handle: target.Handle, Identity: target.Identity, Tier: target.Tier, ThreadID: msg.ThreadID, TraceID: msg.TraceID, Allowed: true, Dispatched: false, Reason: "dispatch failed: " + err.Error()})
		return out, err
	}
	out.Dispatched = true

	// 5. Bind the thread to this agent — ONLY now, after a successful authorized dispatch. Every path
	//    (deterministic, sticky, inference) refreshes the binding, so an explicit @handle rebinds the
	//    thread to the newly-addressed agent and a sticky follow-up extends the TTL. Binding after (never
	//    before) Authorize is what keeps a binding from ever standing in for an access check.
	if g.Affinity != nil && msg.ThreadID != "" {
		g.Affinity.Bind(msg.ThreadID, target.Identity)
	}

	audit.Record(ctx, AuditRecord{Sender: msg.Sender, Mode: res.Mode, Handle: target.Handle, Identity: target.Identity, Tier: target.Tier, ThreadID: msg.ThreadID, TraceID: msg.TraceID, Allowed: true, Dispatched: true, Reason: dec.Reason})
	return out, nil
}

// emitClarify finalizes a turn the router refuses to guess (an ambiguous @handle or a low-confidence NL
// turn): it records the candidate menu on out, invokes the optional Replier to ask the human which agent
// they meant, and emits a single audit record marked Clarify (never Dispatched). A clarify is terminal
// and deterministic — there is nothing to dispatch and nothing to retry. A Replier failure does not
// change that outcome; it is folded into the audit reason so it is not silently dropped. Handle/Tier are
// taken from the resolved handle: populated for an ambiguous @handle (all candidates share that handle),
// empty for an NL clarify (the candidate menu carries the distinct handles instead).
func (g *Gateway) emitClarify(ctx context.Context, audit AuditSink, out *Outcome, msg Message, res Resolution, ce *ClarifyError) {
	out.Clarify = ce
	reason := ce.Error()
	if g.Replier != nil {
		if rerr := g.Replier.Clarify(ctx, msg, ce); rerr != nil {
			reason = reason + "; clarify reply failed: " + rerr.Error()
		}
	}
	audit.Record(ctx, AuditRecord{
		Sender:   msg.Sender,
		Mode:     res.Mode,
		Handle:   res.Handle.Canonical(),
		Tier:     res.Handle.Tier,
		ThreadID: msg.ThreadID, TraceID: msg.TraceID,
		Clarify: true,
		Reason:  reason,
	})
}

// stickyTarget resolves a bare turn to the agent its thread is bound to, if any (06 §6). It returns a
// live Target and true only when affinity is enabled, the message carries a thread, the thread has a
// non-expired binding, AND that bound key still resolves to a live agent in the index. A binding to an
// agent that has since been removed is STALE: stickyTarget drops it and returns false so the caller falls
// through to fresh NL inference rather than dead-ending on a departed target. It spends no inference and
// makes no authorization decision — the caller still runs Authorize on the returned target.
func (g *Gateway) stickyTarget(msg Message) (Target, bool) {
	if g.Affinity == nil || msg.ThreadID == "" {
		return Target{}, false
	}
	key, ok := g.Affinity.Lookup(msg.ThreadID)
	if !ok {
		return Target{}, false
	}
	t, ok := g.Index.Lookup(key)
	if !ok {
		g.Affinity.Drop(msg.ThreadID) // stale binding: the bound agent is gone.
		return Target{}, false
	}
	return t, true
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
