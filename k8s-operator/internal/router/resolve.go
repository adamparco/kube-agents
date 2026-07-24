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
	"fmt"
	"sort"
	"strings"
	"sync/atomic"
)

// botMention is the gateway's own handle. It is skipped when scanning for a target so `@kage /foo` and
// `@kage @bar` address foo/bar, not kage.
const botMention = "@kage"

// Default confidence gates for the NL inference core (06 §2b). The DETERMINISTIC core — not the model —
// owns the route-vs-clarify decision: a top candidate below threshold, or within margin of the
// runner-up, is too weak or too ambiguous to route on and becomes a clarify (clarify, never guess).
const (
	defaultThreshold       = 0.75
	defaultAmbiguityMargin = 0.10
)

// Inferer is the NL fallback (mode 3): given the message text and the CURRENT live-handle menu, it
// PROPOSES scored candidates. It does not decide the route — the Resolver's deterministic core filters
// the proposals down to the known menu and applies the confidence/ambiguity gates. Passing `known` in
// means a hallucinated handle can never survive resolution (it is not in the menu), independently of
// what the model returns. Phase 2 wires no Inferer, so mode 3 spends nothing (see Resolver.Infer).
type Inferer interface {
	Infer(ctx context.Context, text string, known []Handle) ([]Candidate, error)
}

// Resolver maps inbound chat text to a target Handle. Resolution is split into two halves so the model
// boundary is structural, not conventional:
//
//   - Resolve: the DETERMINISTIC core (modes 1/2). Constant-time; never touches the Inferer; never
//     increments. On no slash/handle match it returns ErrNeedsInference and stops.
//   - Infer: the mode-3 NL core and the SOLE site that spends inference. The gateway calls it only after
//     Resolve returns ErrNeedsInference.
//
// A Resolver is safe for concurrent use. A nil Inferer is the Phase-2 posture: Infer refuses without a
// model call, so inference is never spent.
type Resolver struct {
	// inferer is nil in Phase 2. When nil, Infer refuses (ErrInferenceUnavailable) without any model call.
	inferer Inferer
	// inferenceCalls counts Inferer invocations. It is asserted ==0 across the deterministic matrix to
	// prove no slash/handle turn spends inference; it can only ever increment inside Infer.
	inferenceCalls atomic.Int64
	// threshold is the minimum top-candidate confidence to route on; below it the turn clarifies.
	threshold float64
	// ambiguityMargin is the minimum top−second confidence gap to route on; within it the turn clarifies.
	ambiguityMargin float64
}

// NewResolver returns the Phase-2 resolver: slash/handle only, inference disabled (refused, not spent),
// with the default confidence gates.
func NewResolver() *Resolver {
	return &Resolver{threshold: defaultThreshold, ambiguityMargin: defaultAmbiguityMargin}
}

// WithInferer returns a resolver that can fall back to inf for unaddressed messages (Phase 3). The
// deterministic modes 1/2 still never invoke it; only Infer does.
func WithInferer(inf Inferer) *Resolver {
	r := NewResolver()
	r.inferer = inf
	return r
}

// WithThreshold overrides the confidence gate (route only if top ≥ threshold) and the ambiguity margin
// (clarify if top−second < margin). It is the ONLY way to move the route↔clarify boundary, so the gates
// are fixed once at wiring time and cannot drift per call. Returns the receiver for chaining.
func (r *Resolver) WithThreshold(threshold, margin float64) *Resolver {
	r.threshold = threshold
	r.ambiguityMargin = margin
	return r
}

// InferenceCalls reports how many times an Inferer has been invoked. Always 0 for a NewResolver() and
// for any run that only ever takes modes 1/2.
func (r *Resolver) InferenceCalls() int64 { return r.inferenceCalls.Load() }

// Resolve is the DETERMINISTIC half of resolution (06 §2b), modes 1/2 only:
//
//  1. slash command  `@kage /<handle> …`  → ModeSlash  (constant-time)
//  2. explicit handle `… @<handle> …`     → ModeHandle (constant-time)
//
// A recognized-but-broken handle is a deterministic refusal (ErrMalformedHandle/ErrUnknownTier) — the
// router clarifies, it does not guess. On no slash/handle match Resolve returns ErrNeedsInference
// WITHOUT touching the Inferer or incrementing: the caller (the gateway) decides whether to escalate to
// Infer (mode 3) or refuse. The ctx is unused here but kept for signature symmetry with Infer.
func (r *Resolver) Resolve(_ context.Context, text string) (Resolution, error) {
	fields := strings.Fields(text)

	// Mode 1: slash command. A slash command is the leading token (after an optional @kage mention);
	// a `/word` mid-sentence is not a command.
	if tok, ok := leadingSlashToken(fields); ok {
		h, err := parseHandleToken(tok)
		if err != nil {
			return Resolution{Mode: ModeSlash}, err
		}
		return Resolution{Handle: h, Mode: ModeSlash}, nil
	}

	// Mode 2: explicit @handle mention (the first non-@kage @token).
	if tok, ok := firstAtHandle(fields); ok {
		h, err := parseHandleToken(tok)
		if err != nil {
			return Resolution{Mode: ModeHandle}, err
		}
		return Resolution{Handle: h, Mode: ModeHandle}, nil
	}

	// No deterministic match: signal the caller to escalate to inference (or refuse). No model, no spend.
	return Resolution{Mode: ModeInference}, ErrNeedsInference
}

// Infer is the mode-3 NL core and the SOLE site that spends inference. The gateway calls it only after
// Resolve returns ErrNeedsInference. Order (load-bearing):
//
//  1. empty text        → ErrUnaddressed, BEFORE any increment (a blank turn spends 0 inference).
//  2. no Inferer wired  → ErrInferenceUnavailable, no increment (the Phase-2 posture).
//  3. otherwise         → increment EXACTLY ONCE, then call the model.
//  4. drop every returned candidate not in `known` (a hallucinated handle cannot survive); 0 left →
//     ErrUnaddressed.
//  5. top < threshold OR (top−second) < margin → clarify (*ClarifyError with the surviving menu);
//     else route the top candidate.
//
// The deterministic core owns steps 4–5; the model only proposes. InferenceCalls increments once for a
// routed OR a clarified turn (the model was consulted either way) and zero for steps 1–2.
func (r *Resolver) Infer(ctx context.Context, text string, known []Handle) (Resolution, error) {
	if strings.TrimSpace(text) == "" {
		return Resolution{Mode: ModeInference}, ErrUnaddressed
	}
	if r.inferer == nil {
		return Resolution{Mode: ModeInference}, ErrInferenceUnavailable
	}

	r.inferenceCalls.Add(1)
	cands, err := r.inferer.Infer(ctx, text, known)
	if err != nil {
		return Resolution{Mode: ModeInference}, err
	}

	// Barrier: re-filter the model's proposals to the live menu regardless of what it returned.
	valid := filterKnown(cands, known)
	if len(valid) == 0 {
		return Resolution{Mode: ModeInference}, ErrUnaddressed
	}
	sort.SliceStable(valid, func(i, j int) bool { return valid[i].Confidence > valid[j].Confidence })

	top := valid[0]
	nearTie := len(valid) >= 2 && (top.Confidence-valid[1].Confidence) < r.ambiguityMargin
	if top.Confidence < r.threshold || nearTie {
		return Resolution{Mode: ModeInference}, &ClarifyError{
			Reason: fmt.Sprintf(
				"low-confidence NL match (top %.2f, threshold %.2f, margin %.2f); clarify rather than guess",
				top.Confidence, r.threshold, r.ambiguityMargin),
			Candidates: valid,
		}
	}
	return Resolution{Handle: top.Handle, Mode: ModeInference}, nil
}

// filterKnown returns the candidates whose handle is in the live menu, preserving input order. It is the
// deterministic barrier that makes a hallucinated handle un-routable no matter what the model proposes.
func filterKnown(cands []Candidate, known []Handle) []Candidate {
	allow := make(map[string]struct{}, len(known))
	for _, h := range known {
		allow[tierLeafKey(h)] = struct{}{}
	}
	out := make([]Candidate, 0, len(cands))
	for _, c := range cands {
		if _, ok := allow[tierLeafKey(c.Handle)]; ok {
			out = append(out, c)
		}
	}
	return out
}

// leadingSlashToken returns the handle token of a leading slash command, skipping one optional @kage
// mention. It only matches a slash at the START of the message (a slash command), not anywhere later.
func leadingSlashToken(fields []string) (string, bool) {
	i := 0
	if i < len(fields) && strings.EqualFold(fields[i], botMention) {
		i++
	}
	if i < len(fields) && strings.HasPrefix(fields[i], "/") && len(fields[i]) > 1 {
		return trimHandlePunct(fields[i][1:]), true
	}
	return "", false
}

// firstAtHandle returns the token of the first `@handle` mention that is not the bot's own @kage.
func firstAtHandle(fields []string) (string, bool) {
	for _, f := range fields {
		if !strings.HasPrefix(f, "@") || len(f) == 1 {
			continue
		}
		if strings.EqualFold(f, botMention) {
			continue
		}
		return trimHandlePunct(f[1:]), true
	}
	return "", false
}

// trimHandlePunct strips trailing sentence punctuation a human might append to a handle (e.g.
// "@cluster-bravo:" or "@cluster-bravo,"). Leading/interior characters are left to leaf validation.
func trimHandlePunct(s string) string {
	return strings.TrimRight(s, ".,:;!?)")
}
