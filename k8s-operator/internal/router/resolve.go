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
	"strings"
	"sync/atomic"
)

// botMention is the gateway's own handle. It is skipped when scanning for a target so `@kage /foo` and
// `@kage @bar` address foo/bar, not kage.
const botMention = "@kage"

// Inferer is the (deferred) NL fallback that maps free-text intent to a Handle (06 §2b mode 3). Phase 2
// ships NO implementation: Resolve refuses mode 3 WITHOUT constructing or invoking an Inferer, so the
// router spends zero inference and a chat message is never routed on model output (03 §4a). Phase 3 may
// wire one in; Resolver.InferenceCalls() then counts exactly its invocations.
type Inferer interface {
	Infer(ctx context.Context, text string) (Handle, error)
}

// Resolver maps inbound chat text to a target Handle by the deterministic order slash → handle →
// inference (06 §2b). It is safe for concurrent use. A Resolver with a nil inferer is the Phase-2
// posture: addressed messages resolve constant-time, everything else is refused with
// ErrUnaddressed/ErrInferenceUnavailable and inference is never spent.
type Resolver struct {
	// inferer is nil in Phase 2. When nil, mode 3 is refused without any model call.
	inferer Inferer
	// inferenceCalls counts Inferer invocations. It is asserted ==0 across the Phase-2 test matrix to
	// prove no chat routing spends inference; it can only ever increment on the mode-3 path.
	inferenceCalls atomic.Int64
}

// NewResolver returns the Phase-2 resolver: slash/handle only, inference disabled (refused, not spent).
func NewResolver() *Resolver { return &Resolver{} }

// WithInferer returns a resolver that falls back to inf for unaddressed messages (Phase 3). Kept so the
// mode-3 boundary is exercised by tests today; the Phase-2 binary uses NewResolver (inferer nil).
func WithInferer(inf Inferer) *Resolver { return &Resolver{inferer: inf} }

// InferenceCalls reports how many times an Inferer has been invoked. Always 0 for a NewResolver().
func (r *Resolver) InferenceCalls() int64 { return r.inferenceCalls.Load() }

// Resolve maps text to a target agent by deterministic order (06 §2b):
//
//  1. slash command  `@kage /<handle> …`      → ModeSlash  (constant-time)
//  2. explicit handle `… @<handle> …`         → ModeHandle (constant-time)
//  3. NL inference    (fallback)              → ModeInference
//
// Modes 1–2 never touch inference. Mode 3 is refused with ErrInferenceUnavailable when no Inferer is
// wired (Phase 2) — no model is called, so InferenceCalls stays 0. A recognized-but-broken handle is a
// deterministic refusal (ErrMalformedHandle/ErrUnknownTier); the router clarifies, it does not guess.
// On any refusal the returned Resolution still carries the attempted Mode for the audit record.
func (r *Resolver) Resolve(ctx context.Context, text string) (Resolution, error) {
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

	// Mode 3: NL inference fallback. Phase 2 has no Inferer — refuse WITHOUT a model call.
	if r.inferer == nil {
		if len(fields) == 0 {
			return Resolution{Mode: ModeInference}, ErrUnaddressed
		}
		return Resolution{Mode: ModeInference}, ErrInferenceUnavailable
	}
	r.inferenceCalls.Add(1)
	h, err := r.inferer.Infer(ctx, text)
	if err != nil {
		return Resolution{Mode: ModeInference}, err
	}
	return Resolution{Handle: h, Mode: ModeInference}, nil
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
