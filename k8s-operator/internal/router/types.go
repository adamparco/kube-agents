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

// Package router is the ChatOps gateway (C15, 05) that resolves every inbound chat message to exactly
// one (tier, scope) Agent CR and dispatches to that agent's per-tier pod (06 §2b). This file holds the
// pure, dependency-light core: the routing grammar (grammar.go), deterministic resolution + the
// inference boundary (resolve.go), and the before-dispatch allowlist check (authorize.go). The
// informer/index over Agent CRs, the Pub/Sub dispatcher, and the Deployment are layered on top
// (Phase 2 T15+) — none of them is needed to unit-test the security-load-bearing logic here.
//
// Two invariants are enforced structurally, not by convention:
//
//   - Routing is NEVER an authz signal (03 §4a). Resolve() only names the target; Authorize() decides
//     access by reading the TARGET CR's allowlist, independently of how the target was resolved.
//   - The router is fail-closed (03 §4a; Phase 2 acceptance d). An empty/absent allowlist refuses ALL
//     senders — the router never honors the pod-env ALLOW_ALL default (which is the in-pod gateway's
//     permissive v1 behavior, kept only as a defense-in-depth backstop behind this pre-dispatch check).
package router

import (
	"errors"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// Mode is the routing mode recorded on every chat turn's audit record (06 §2b). Resolution order is
// deterministic-first: slash → handle → sticky (thread affinity) → inference. Only inference can spend a
// model call; slash/handle/sticky never do. Phase 2 refused inference; Phase 3 adds sticky + inference.
type Mode string

const (
	// ModeSlash is a slash command: `@kage /<handle> <text>` (constant-time, no inference).
	ModeSlash Mode = "slash"
	// ModeHandle is an explicit `@handle` mention (constant-time, no inference).
	ModeHandle Mode = "handle"
	// ModeSticky is a thread-affinity follow-up (06 §6): a bare message routed to the agent the thread is
	// already bound to, spending NO inference. The binding is consulted only after slash/handle resolution
	// finds no target, and can only ever name an agent a prior authorized turn already dispatched to — so
	// sticky routing never precedes or replaces the per-turn Authorize check.
	ModeSticky Mode = "sticky"
	// ModeInference is the NL fallback (one router model call). Deferred to Phase 3; refused in Phase 2.
	ModeInference Mode = "inference"
)

// Handle is a parsed chat address: a tier plus the single scope leaf the handle carries (06 §2b).
// A handle names only its leaf — platform→project, cluster-admin→cluster, developer-team→namespace —
// so a full routing key needs the router's project context (see Handle.RouteKey), which is why the
// index resolves handles against Agent CRs rather than trusting the handle alone.
type Handle struct {
	// Tier is the addressed tier.
	Tier agentv1alpha1.AgentTier
	// Leaf is the scope value in the handle: project (platform) | cluster (cluster-admin) |
	// namespace (developer-team). Always lower-cased and RFC1123-label-shaped (see parseHandleToken).
	Leaf string
}

// Resolution is the outcome of Resolve: which agent was named and by which mode. On a refusal Resolve
// returns the Mode it attempted (so the audit record still captures intent, 06 §2b) alongside the error.
type Resolution struct {
	Handle Handle
	Mode   Mode
}

// Target is the routing-relevant projection of the resolved (tier, scope) Agent CR. The index builds
// it from the matched CR; Authorize and the dispatcher consume it. Keeping the projection explicit
// means the authz decision reads exactly the CR fields the contract names (06 §2b) and nothing else.
type Target struct {
	// Identity is agentindex.ScopeIdentity(cr) — the (tier, scope) key, also the routing-table key.
	Identity string
	// Tier is the target agent's tier (audit + dispatch selection).
	Tier agentv1alpha1.AgentTier
	// Handle is the canonical @handle of the target (audit attribution, 06 §2b).
	Handle string
	// TopicName is the target CR's integration.googleChat.topicName — the PubSubDispatcher re-publishes
	// here; the target pod's own proxy drains it (no credential_proxy.py edit, Decision 2).
	TopicName string
	// AllowedUsers is the target CR's integration.googleChat.allowedUsers — the CLOSED trusted-human
	// allowlist Authorize checks BEFORE dispatch. Empty/absent ⇒ the router refuses ALL (fail-closed);
	// it never reads the pod-env *_ALLOW_ALL_USERS flag the operator renders for the permissive default.
	AllowedUsers []string
}

// Decision is the result of the before-dispatch allowlist check. Reason is always populated (an
// allow reason for audit, a deny reason for the caller to surface) so every decision is attributable.
type Decision struct {
	Allowed bool
	Reason  string
}

// Candidate is a handle the router proposes as a possible target, with the confidence its proposer
// assigned it (06 §2b). Deterministic (index-assisted) candidates carry Confidence 1.0 — they are
// exact matches that are ambiguous only because more than one live agent shares the (tier, leaf), e.g.
// a developer-team namespace that exists in more than one cluster. The Phase-3 NL inferer (mode 3)
// assigns fractional confidences; the deterministic core, not the model, decides route vs. clarify.
type Candidate struct {
	Handle     Handle
	Confidence float64
}

// ClarifyError is a deterministic refusal that asks the human to disambiguate instead of guessing among
// several equally-plausible targets (06 §2b: low confidence / ambiguity → clarify, never guess). It is
// returned (as a wrapped error) when resolution names MORE THAN ONE live agent — today only the
// multi-cluster developer-team case (a namespace that exists in several clusters) — and, from Phase 3
// mode 3, when NL inference is uncertain. Callers branch on it with errors.Is(err, ErrClarify);
// errors.As(err, &ce) reads the candidate menu so the reply can offer the specific choices.
type ClarifyError struct {
	// Reason is the human-facing explanation (names the ambiguity, e.g. the clashing scopes).
	Reason string
	// Candidates is the menu of plausible targets to disambiguate between (never empty for a clarify).
	Candidates []Candidate
}

// Error makes *ClarifyError an error.
func (e *ClarifyError) Error() string { return e.Reason }

// Is lets errors.Is(err, ErrClarify) match any *ClarifyError, so the delivery layer classifies a
// clarify as a deterministic (Ack, don't retry) refusal without unwrapping the struct.
func (e *ClarifyError) Is(target error) bool { return target == ErrClarify }

// Sentinel errors. Every non-inference refusal is one of these deterministic values so callers can
// branch on cause and tests can assert exact behavior — the router never guesses (06 §2b: low
// confidence → clarify, not guess).
var (
	// ErrUnaddressed means the message named no agent by slash or handle and inference is unavailable
	// (Phase 2). It is the deterministic Phase-2 fallback: refuse and ask the human to address explicitly.
	ErrUnaddressed = errors.New("router: message names no agent (use a slash command or @handle)")
	// ErrMalformedHandle means a slash/handle token was present but its leaf was empty or not a valid
	// RFC1123 label — refused rather than coerced.
	ErrMalformedHandle = errors.New("router: malformed handle")
	// ErrUnknownTier means a handle token did not match any known tier prefix.
	ErrUnknownTier = errors.New("router: unknown tier in handle")
	// ErrInferenceUnavailable means resolution fell through to NL inference but no Inferer is wired.
	// This is the Phase-2 posture: mode 3 is refused WITHOUT invoking any model, so inference_calls==0.
	ErrInferenceUnavailable = errors.New("router: NL inference is disabled (Phase 2 routes by slash/handle only)")
	// ErrNeedsInference is the INTERNAL signal the deterministic Resolve returns when a message matches
	// no slash/handle (modes 1/2) and therefore needs the mode-3 NL fallback. It never escapes the
	// gateway: the gateway either calls Resolver.Infer (which returns a terminal outcome) or, with no
	// Inferer wired, Infer maps it to ErrInferenceUnavailable/ErrUnaddressed. Splitting resolution this
	// way guarantees Resolve never touches the model and never spends inference.
	ErrNeedsInference = errors.New("router: message needs NL inference (no slash/handle match)")
	// ErrMissingProjectContext means a cluster-admin handle could not be turned into a routing key
	// because the router was given no project context to fill the scope.
	ErrMissingProjectContext = errors.New("router: cluster-admin handle needs project context to form a routing key")
	// ErrClarify is the sentinel every *ClarifyError matches under errors.Is (see ClarifyError.Is). It
	// marks an ambiguous turn the router refuses to guess on — a deterministic refusal (Ack, don't
	// retry). The concrete *ClarifyError carries the candidate menu (read via errors.As).
	ErrClarify = errors.New("router: message is ambiguous; clarification requested")
)
