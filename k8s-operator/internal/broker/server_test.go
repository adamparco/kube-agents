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
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/go-logr/logr"
)

// The HTTP surface, and V-BRK-021 in particular.
//
// V-BRK-021 is a claim about what does NOT exist: no debug route, no override parameter, no bypass
// header, no build-tag-guarded skip path. Claims of that shape cannot be proved by probing a
// running process -- a probe only covers the routes somebody thought to try -- so the assertions
// here are made against the server's own enumeration of its surface plus a scan of the package
// source. Both are needed: the enumeration proves the mux has one mutating route, and the source
// scan proves nobody added a second one and forgot to enumerate it.

// recordingJournal captures the refusals that were journaled.
type recordingJournal struct {
	refusals []*Refusal
	bodies   [][]byte
	err      error
}

func (j *recordingJournal) Reject(_ context.Context, _ *Identity, body []byte, ref *Refusal) error {
	j.refusals = append(j.refusals, ref)
	j.bodies = append(j.bodies, body)
	return j.err
}

func (j *recordingJournal) reasons() []string {
	out := make([]string, 0, len(j.refusals))
	for _, r := range j.refusals {
		out = append(out, r.Reason)
	}
	return out
}

// stubPipeline accepts everything and records what it saw.
type stubPipeline struct {
	calls  int
	lastID *Identity
	lastEn *Envelope
	result *Result
	err    error
}

func (p *stubPipeline) Submit(_ context.Context, id *Identity, e *Envelope) (*Result, error) {
	p.calls++
	p.lastID, p.lastEn = id, e
	if p.err != nil {
		return nil, p.err
	}
	if p.result != nil {
		return p.result, nil
	}
	return &Result{ActionID: "act-1", Namespace: testNamespace, Decision: "accepted", Phase: "Pending"}, nil
}

// harness is a fully wired server with every dependency observable.
type harness struct {
	server   *Server
	guard    *ReplayGuard
	journal  *recordingJournal
	security *MemorySecuritySink
	pipeline *stubPipeline
	clock    *clock
	reviewer *fakeReviewer
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	cl := newClock(testNow)
	h := &harness{
		guard:    NewReplayGuard(cl.Now),
		journal:  &recordingJournal{},
		security: &MemorySecuritySink{},
		pipeline: &stubPipeline{},
		clock:    cl,
		reviewer: &fakeReviewer{status: authenticatedAs(testUsername)},
	}
	// Started an hour ago, so the restart-fail-closed rule is not silently what refuses every case
	// in this file. TestRestartFailsClosed covers that rule on its own.
	h.guard.startedAt = testNow.Add(-time.Hour)

	s, err := NewServer(Config{
		Authenticator: testAuthenticator(h.reviewer, h.security),
		Guard:         h.guard,
		Pipeline:      h.pipeline,
		Journal:       h.journal,
		Security:      h.security,
		Log:           logr.Discard(),
		Namespace:     testNamespace,
	})
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	h.server = s
	return h
}

// post sends an authenticated, mTLS-bearing POST to the actions route.
func (h *harness) post(t *testing.T, body []byte, mutate ...func(*http.Request)) *httptest.ResponseRecorder {
	t.Helper()
	r := request(t, true, readerSPIFFE(), "Bearer tok")
	r.Method = http.MethodPost
	r.URL.Path = ActionsPath
	r.Body = io.NopCloser(bytes.NewReader(body))
	r.ContentLength = int64(len(body))
	r.Header.Set("Content-Type", "application/json")
	for _, m := range mutate {
		m(r)
	}
	w := httptest.NewRecorder()
	h.server.ServeHTTP(w, r)
	return w
}

func (h *harness) decode(t *testing.T, w *httptest.ResponseRecorder) Response {
	t.Helper()
	var resp Response
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("response is not JSON (%d): %s", w.Code, w.Body.String())
	}
	return resp
}

// submittable returns a valid fixture body rewritten so it will pass the guard: a fresh nonce from
// this harness's guard, the current clock as issuedAt, and a recomputed idempotency key.
//
// Recomputing rather than editing the fixture's key is deliberate. The fixture's key is a golden
// value for the JCS implementation and TestValidFixtureIdempotencyKeys asserts it; if this helper
// hand-edited it, a canonicaliser regression would show up here as a confusing 400 instead of
// there as the clear failure it is.
func (h *harness) submittable(t *testing.T, fixture string) []byte {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(fixtureRoot, "valid", fixture))
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	var body map[string]any
	if err := json.Unmarshal(raw, &body); err != nil {
		t.Fatalf("fixture is not JSON: %v", err)
	}

	body["issuedAt"] = h.clock.Now().UTC().Format(time.RFC3339)
	body["nonce"] = mustNonce(t, h.guard, testUsername)

	// Round-trip through the decoder to compute the key the way the server will.
	keyless, err := json.Marshal(body)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	env, err := DecodeEnvelope(keyless)
	if err != nil {
		t.Fatalf("fixture %s does not decode: %v", fixture, err)
	}
	key, err := ComputeIdempotencyKey(testIdentity().AgentIdentity(), env)
	if err != nil {
		t.Fatalf("ComputeIdempotencyKey: %v", err)
	}
	body["idempotencyKey"] = key

	out, err := json.Marshal(body)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return out
}

// V-BRK-021, part 1: the route set is exactly three, and exactly one of them mutates.
func TestRouteSetIsThreeWithOneMutating(t *testing.T) {
	h := newHarness(t)

	want := []string{ActionsPath, HealthzPath, NoncePath}
	sort.Strings(want)
	got := h.server.Routes()
	if len(got) != len(want) {
		t.Fatalf("Routes() = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("Routes() = %v, want %v", got, want)
		}
	}
	if m := h.server.MutatingRoutes(); len(m) != 1 || m[0] != ActionsPath {
		t.Fatalf("MutatingRoutes() = %v, want exactly [%s]", m, ActionsPath)
	}
	if Port != 8443 {
		t.Fatalf("Port = %d, want 8443 (08 §2.3)", Port)
	}
}

// V-BRK-021, part 2: everything outside the route set is a 404, including the shapes a caller
// probing for a back door would try first.
func TestNoDebugRoutes(t *testing.T) {
	h := newHarness(t)
	probes := []string{
		"/debug/pprof/", "/debug/vars", "/metrics", "/admin", "/apply", "/exec",
		"/v1alpha1", "/v1alpha1/", "/v1alpha1/apply", "/v1alpha1/actions/force",
		"/v1alpha1/actions/", "/v1alpha1/actions/anything", "/v1/actions", "/",
		"/v1alpha1/undo", "/v1alpha1/classify", "/v1alpha1/execute",
	}
	for _, p := range probes {
		t.Run(p, func(t *testing.T) {
			r := request(t, true, readerSPIFFE(), "Bearer tok")
			r.URL.Path = p
			w := httptest.NewRecorder()
			h.server.ServeHTTP(w, r)
			if w.Code != http.StatusNotFound {
				t.Fatalf("%s returned %d, want 404", p, w.Code)
			}
		})
	}
	if h.pipeline.calls != 0 {
		t.Fatalf("the pipeline ran %d times while probing non-routes", h.pipeline.calls)
	}
}

// The mutating route takes POST and nothing else. GET in particular: a GET that mutated would be
// retried by every proxy and prefetcher between the agent and the broker.
func TestActionsRouteMethodSet(t *testing.T) {
	h := newHarness(t)
	for _, m := range []string{http.MethodGet, http.MethodPut, http.MethodPatch, http.MethodDelete, http.MethodHead, http.MethodOptions} {
		t.Run(m, func(t *testing.T) {
			r := request(t, true, readerSPIFFE(), "Bearer tok")
			r.Method = m
			w := httptest.NewRecorder()
			h.server.ServeHTTP(w, r)
			if w.Code != http.StatusMethodNotAllowed {
				t.Fatalf("%s %s returned %d, want 405", m, ActionsPath, w.Code)
			}
		})
	}
	// And the nonce route is GET-only, so it cannot be turned into a submission path.
	for _, m := range []string{http.MethodPost, http.MethodPut, http.MethodDelete} {
		r := request(t, true, readerSPIFFE(), "Bearer tok")
		r.Method, r.URL.Path = m, NoncePath
		w := httptest.NewRecorder()
		h.server.ServeHTTP(w, r)
		if w.Code != http.StatusMethodNotAllowed {
			t.Fatalf("%s %s returned %d, want 405", m, NoncePath, w.Code)
		}
	}
}

// V-BRK-021, part 3: no query parameter is accepted, on the mutating route, ever.
//
// The check is an allowlist of zero rather than a denylist of known-bad names, and this test covers
// both the obviously hostile ones and an innocuous one. `?pretty=true` failing is the point: a
// denylist would let it through, and the next parameter added would be let through too.
func TestActionsRouteTakesNoQueryParameters(t *testing.T) {
	h := newHarness(t)
	for _, q := range []string{
		"force=true", "bypass=1", "dryRun=false", "skipVerify=true", "riskClass=routine",
		"tier=platform", "scope=other", "approved=true", "pretty=true", "x=1",
	} {
		t.Run(q, func(t *testing.T) {
			before := len(h.security.Records())
			w := h.post(t, []byte(`{}`), func(r *http.Request) { r.URL.RawQuery = q })
			if w.Code != http.StatusBadRequest {
				t.Fatalf("?%s returned %d, want 400", q, w.Code)
			}
			if got := h.decode(t, w).Reason; got != "unsupported-query-parameter" {
				t.Fatalf("reason = %q, want unsupported-query-parameter", got)
			}
			if len(h.security.Records()) != before+1 {
				t.Fatalf("a query parameter on the mutating route must raise a security event")
			}
		})
	}
	if h.pipeline.calls != 0 {
		t.Fatalf("the pipeline ran %d times for query-parameter probes", h.pipeline.calls)
	}
}

// V-BRK-021, part 4: bypass headers. Refused on EVERY route, not just the mutating one -- the check
// is in ServeHTTP ahead of the mux, so a future route cannot be added without inheriting it.
func TestBypassHeadersAreRefusedOnEveryRoute(t *testing.T) {
	for _, header := range bypassHeaders {
		for _, path := range []string{ActionsPath, NoncePath, HealthzPath, "/nope"} {
			t.Run(header+" "+path, func(t *testing.T) {
				h := newHarness(t)
				r := request(t, true, readerSPIFFE(), "Bearer tok")
				r.URL.Path = path
				r.Header.Set(header, "true")
				w := httptest.NewRecorder()
				h.server.ServeHTTP(w, r)

				if w.Code != http.StatusBadRequest {
					t.Fatalf("%s on %s returned %d, want 400", header, path, w.Code)
				}
				if got := h.decode(t, w).Reason; got != ReasonBypassKey {
					t.Fatalf("reason = %q, want %q", got, ReasonBypassKey)
				}
				// Journaled and alarmed. A header that is merely ignored leaves no evidence that
				// anything tried, and the attempt is the evidence.
				if len(h.journal.refusals) != 1 {
					t.Fatalf("journaled %d refusals, want 1", len(h.journal.refusals))
				}
				if len(h.security.Records()) != 1 {
					t.Fatalf("security records = %d, want 1", len(h.security.Records()))
				}
			})
		}
	}
}

// The bypass-header list covers the reserved body keys it mirrors. Without this, a key could be
// added to ReservedKeys and its header analogue silently left accepted -- and the header is the
// obvious second place to try once the body has refused you.
func TestBypassHeadersMirrorTheReservedKeys(t *testing.T) {
	have := map[string]bool{}
	for _, h := range bypassHeaders {
		have[strings.ToLower(strings.TrimPrefix(h, "X-Kube-Agents-"))] = true
	}
	// kebab-case of each reserved key, which is the header spelling convention.
	for key := range ReservedKeys {
		var b strings.Builder
		for i, r := range key {
			if r >= 'A' && r <= 'Z' {
				if i > 0 {
					b.WriteByte('-')
				}
				b.WriteRune(r + 32)
				continue
			}
			b.WriteRune(r)
		}
		spelled := b.String()
		// `namespace`, `actor` and `undoPlan` have no header analogue: they are not overrides, they
		// are fields that belong somewhere else or are broker-generated. The others are.
		switch key {
		case "namespace", "actor", "undoPlan", "class", "severity":
			continue
		}
		if !have[spelled] {
			t.Errorf("reserved body key %q has no X-Kube-Agents-%s header analogue in bypassHeaders", key, spelled)
		}
	}
}

// V-BRK-021, part 5: the source scan. No build tag anywhere in the package, and no second mutating
// handler registered on the mux.
//
// A build-tag-guarded skip path is the failure mode this exists for: it compiles out of the tested
// binary and into some other one, so no runtime test can see it, and "the tests pass" stops meaning
// anything about the shipped image.
func TestNoBuildTagGuardedPathsInPackage(t *testing.T) {
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatalf("read package dir: %v", err)
	}
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasSuffix(name, ".go") {
			continue
		}
		src, err := os.ReadFile(name)
		if err != nil {
			t.Fatalf("read %s: %v", name, err)
		}
		for i, line := range strings.Split(string(src), "\n") {
			trimmed := strings.TrimSpace(line)
			if !strings.HasPrefix(trimmed, "//") {
				// Build constraints must precede the package clause; once we are past it there is
				// nothing left to find.
				if strings.HasPrefix(trimmed, "package ") {
					break
				}
				continue
			}
			if strings.HasPrefix(trimmed, "//go:build") || strings.HasPrefix(trimmed, "// +build") {
				t.Errorf("%s:%d carries a build constraint: %s\n"+
					"the broker package must compile identically in every configuration; a guarded path is a skip path no test can see",
					name, i+1, trimmed)
			}
		}
		// A second registration on the mux would be a second door. Only production files: a test
		// building its own mux is not part of the shipped surface, and this file necessarily
		// mentions the string it is searching for.
		if strings.HasSuffix(name, "_test.go") || name == "server.go" {
			continue
		}
		if n := strings.Count(string(src), "mux.HandleFunc("); n > 0 {
			t.Errorf("%s registers %d route(s); every route belongs in server.go where Routes() enumerates it", name, n)
		}
	}

	// And the enumeration is not stale: exactly four registrations in server.go -- three routes
	// plus the catch-all -- matching Routes() plus "/".
	src, err := os.ReadFile("server.go")
	if err != nil {
		t.Fatalf("read server.go: %v", err)
	}
	if n := strings.Count(string(src), "s.mux.HandleFunc("); n != 4 {
		t.Fatalf("server.go registers %d handlers; Routes() enumerates 3 plus the catch-all, so this must be 4", n)
	}
}

// Health is unauthenticated by necessity -- the kubelet has no projected token -- and therefore
// returns a constant and touches nothing.
func TestHealthzNeedsNoCredentials(t *testing.T) {
	h := newHarness(t)
	r := httptest.NewRequest(http.MethodGet, HealthzPath, http.NoBody)
	w := httptest.NewRecorder()
	h.server.ServeHTTP(w, r)

	if w.Code != http.StatusOK || strings.TrimSpace(w.Body.String()) != "ok" {
		t.Fatalf("healthz = %d %q, want 200 \"ok\"", w.Code, w.Body.String())
	}
	if h.reviewer.calls != 0 {
		t.Fatalf("healthz performed %d TokenReviews; a probe must not touch the API server", h.reviewer.calls)
	}
}

// The nonce route: authenticated, GET, and it returns a nonce the actions route will accept.
func TestNonceRouteIssuesUsableNonces(t *testing.T) {
	h := newHarness(t)
	r := request(t, true, readerSPIFFE(), "Bearer tok")
	r.Method, r.URL.Path = http.MethodGet, NoncePath
	w := httptest.NewRecorder()
	h.server.ServeHTTP(w, r)

	if w.Code != http.StatusOK {
		t.Fatalf("nonce route = %d, want 200: %s", w.Code, w.Body.String())
	}
	resp := h.decode(t, w)
	if !hex32Re.MatchString(resp.Nonce) {
		t.Fatalf("nonce %q is not 32 lowercase hex characters", resp.Nonce)
	}
	if resp.ExpiresInSeconds != int(NonceTTL.Seconds()) {
		t.Fatalf("expiresInSeconds = %d, want %d", resp.ExpiresInSeconds, int(NonceTTL.Seconds()))
	}

	// Unauthenticated callers get nothing to redeem.
	r2 := httptest.NewRequest(http.MethodGet, NoncePath, http.NoBody)
	w2 := httptest.NewRecorder()
	h.server.ServeHTTP(w2, r2)
	if w2.Code != http.StatusUnauthorized {
		t.Fatalf("unauthenticated nonce request = %d, want 401", w2.Code)
	}
}

// The happy path, end to end: a valid envelope reaches the pipeline with an identity built from the
// transport, and the response carries the record's coordinates.
func TestValidEnvelopeReachesThePipeline(t *testing.T) {
	h := newHarness(t)
	body := h.submittable(t, "platform.scale-deployment.json")

	w := h.post(t, body)
	if w.Code != http.StatusAccepted {
		t.Fatalf("status = %d, want 202: %s", w.Code, w.Body.String())
	}
	if h.pipeline.calls != 1 {
		t.Fatalf("pipeline calls = %d, want 1", h.pipeline.calls)
	}
	// The identity the pipeline received came from the broker's config, not the body.
	if h.pipeline.lastID.Tier != testIdentity().Tier || h.pipeline.lastID.Scope != testIdentity().Scope {
		t.Fatalf("pipeline saw tier=%q scope=%q, want the broker's own", h.pipeline.lastID.Tier, h.pipeline.lastID.Scope)
	}
	resp := h.decode(t, w)
	if resp.ActionID != "act-1" || resp.Decision != "accepted" {
		t.Fatalf("response = %+v, want the pipeline's record", resp)
	}
	if resp.TraceID != h.pipeline.lastEn.Trace.TraceID {
		t.Fatalf("response traceId %q does not echo the envelope's %q", resp.TraceID, h.pipeline.lastEn.Trace.TraceID)
	}
	if len(h.journal.refusals) != 0 {
		t.Fatalf("an accepted envelope journaled %d refusals", len(h.journal.refusals))
	}
}

// An identical action in a DIFFERENT trace is answered from the dedup record instead of executed
// twice. This is the retry path, and the pipeline must not run a second time.
func TestDeduplicatedSubmissionDoesNotReExecute(t *testing.T) {
	h := newHarness(t)
	body := h.submittable(t, "platform.scale-deployment.json")
	if w := h.post(t, body); w.Code != http.StatusAccepted {
		t.Fatalf("first submission = %d: %s", w.Code, w.Body.String())
	}

	// Same operations, new trace and nonce -- so the key is identical but the triple is not.
	var repeat map[string]any
	if err := json.Unmarshal(body, &repeat); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	repeat["nonce"] = mustNonce(t, h.guard, testUsername)
	repeat["trace"] = map[string]any{"traceId": "cccccccccccccccccccccccccccccccc"}
	second, err := json.Marshal(repeat)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}

	w := h.post(t, second)
	if w.Code != http.StatusOK {
		t.Fatalf("second submission = %d, want 200: %s", w.Code, w.Body.String())
	}
	if got := h.decode(t, w).Decision; got != "deduplicated" {
		t.Fatalf("decision = %q, want deduplicated", got)
	}
	if h.pipeline.calls != 1 {
		t.Fatalf("pipeline ran %d times; a deduplicated submission must not execute", h.pipeline.calls)
	}
}

// Every spoofing fixture, driven through the real server: refused, journaled, alarmed, and the
// pipeline never reached. The fixture-level test asserts the Refusal's flags; this asserts the
// server actually acts on them.
func TestSpoofingFixturesAreRefusedEndToEnd(t *testing.T) {
	for _, path := range fixtures(t, "spoofing") {
		t.Run(filepath.Base(path), func(t *testing.T) {
			h := newHarness(t)
			w := h.post(t, read(t, path))

			if w.Code != http.StatusBadRequest {
				t.Fatalf("status = %d, want 400: %s", w.Code, w.Body.String())
			}
			if len(h.journal.refusals) != 1 {
				t.Fatalf("journaled %d refusals, want 1 (reasons: %v)", len(h.journal.refusals), h.journal.reasons())
			}
			if len(h.security.Records()) != 1 {
				t.Fatalf("security records = %d, want 1", len(h.security.Records()))
			}
			// The offending key is a structured field, not only prose inside the detail: a log
			// query cannot select on prose.
			if rec := h.security.Records()[0]; rec.Key == "" {
				t.Fatalf("security record has no Key field: %+v", rec)
			}
			if h.pipeline.calls != 0 {
				t.Fatalf("the pipeline ran for a spoofing fixture")
			}
			// And the caller is told which key, so a legitimate client with a bad SDK can fix it.
			if !strings.Contains(h.decode(t, w).Message, h.security.Records()[0].Key) {
				t.Fatalf("the refusal does not name the offending key: %s", w.Body.String())
			}
		})
	}
}

// A malformed envelope is refused without being journaled or alarmed. The distinction is the whole
// value of the per-reason table in 06 §4.1: if every 400 raised a security event, the reserved-key
// events that matter would be buried under clients with schema bugs.
func TestMalformedEnvelopesAreNotSecurityEvents(t *testing.T) {
	for _, path := range fixtures(t, "malformed") {
		t.Run(filepath.Base(path), func(t *testing.T) {
			h := newHarness(t)
			w := h.post(t, read(t, path))
			if w.Code != http.StatusBadRequest {
				t.Fatalf("status = %d, want 400: %s", w.Code, w.Body.String())
			}
			if len(h.security.Records()) != 0 {
				t.Fatalf("a malformed envelope raised %d security events: %+v", len(h.security.Records()), h.security.Records())
			}
			if len(h.journal.refusals) != 0 {
				t.Fatalf("a malformed envelope was journaled %d times", len(h.journal.refusals))
			}
			if h.pipeline.calls != 0 {
				t.Fatalf("the pipeline ran for a malformed fixture")
			}
		})
	}
}

// The idempotency key is recomputed, and a caller-chosen one is refused.
//
// This is the check that stops the key being a dedup oracle. Without it a caller could send a write
// carrying the key of an action it wants suppressed and get back the earlier record's outcome
// without its own write ever happening.
func TestCallerSuppliedIdempotencyKeyIsRefused(t *testing.T) {
	h := newHarness(t)
	body := h.submittable(t, "platform.scale-deployment.json")

	var m map[string]any
	if err := json.Unmarshal(body, &m); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	m["idempotencyKey"] = "sha256:" + strings.Repeat("a", 64)
	tampered, err := json.Marshal(m)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}

	w := h.post(t, tampered)
	if w.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", w.Code)
	}
	if got := h.decode(t, w).Reason; got != ReasonIdempotencyKeyMismatch {
		t.Fatalf("reason = %q, want %q", got, ReasonIdempotencyKeyMismatch)
	}
	if h.pipeline.calls != 0 {
		t.Fatalf("the pipeline ran for a mismatched key")
	}
	// A mismatch is a client bug and must NOT be alarmed -- and it must not have burned the nonce,
	// or a caller with a bad SDK would hit the outstanding-nonce quota and get a second, unrelated
	// error on top of the first.
	if len(h.security.Records()) != 0 {
		t.Fatalf("a key mismatch raised %d security events; it is a client bug", len(h.security.Records()))
	}
	if n := h.guard.outstanding[testUsername]; n != 1 {
		t.Fatalf("outstanding nonces = %d, want 1; a key mismatch must not consume the nonce", n)
	}
}

// A replayed envelope, through the server: 403, journaled and alarmed, pipeline untouched.
func TestReplayedEnvelopeIsRefusedAndJournaled(t *testing.T) {
	h := newHarness(t)
	body := h.submittable(t, "platform.scale-deployment.json")
	if w := h.post(t, body); w.Code != http.StatusAccepted {
		t.Fatalf("first submission = %d: %s", w.Code, w.Body.String())
	}

	w := h.post(t, body)
	if w.Code != http.StatusForbidden {
		t.Fatalf("replay = %d, want 403: %s", w.Code, w.Body.String())
	}
	if got := h.decode(t, w).Reason; got != ReasonReplayedEnvelope {
		t.Fatalf("reason = %q, want %q", got, ReasonReplayedEnvelope)
	}
	if len(h.journal.refusals) != 1 || len(h.security.Records()) != 1 {
		t.Fatalf("a replay must be journaled AND alarmed; journal=%d security=%d", len(h.journal.refusals), len(h.security.Records()))
	}
	if h.pipeline.calls != 1 {
		t.Fatalf("pipeline calls = %d, want 1; the replay must not have executed", h.pipeline.calls)
	}
}

// An unauthenticated request never reaches the body reader. Asserted so the ordering in
// handleActions -- authenticate, then read -- cannot be reversed by a refactor: reading first would
// let any peer make the broker allocate two megabytes.
func TestUnauthenticatedRequestDoesNotReadTheBody(t *testing.T) {
	h := newHarness(t)
	body := h.submittable(t, "platform.scale-deployment.json")

	w := h.post(t, body, func(r *http.Request) { r.TLS = nil })
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", w.Code)
	}
	if h.pipeline.calls != 0 {
		t.Fatalf("the pipeline ran for an unauthenticated request")
	}
	// The nonce was never redeemed, so the envelope is still submittable once the caller fixes its
	// transport -- a failed auth must not cost the caller a nonce.
	if n := h.guard.outstanding[testUsername]; n != 1 {
		t.Fatalf("outstanding nonces = %d, want 1", n)
	}
}

// The body size cap. An unbounded read on the one process that must stay up for any mutation to be
// possible is a denial of service that needs no exploit.
func TestOversizeEnvelopeIsRefused(t *testing.T) {
	h := newHarness(t)
	huge := append(bytes.Repeat([]byte("a"), MaxRequestBytes+1024), '"')
	w := h.post(t, append([]byte(`{"intent":"`), huge...))
	if w.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("status = %d, want 413", w.Code)
	}
	if h.pipeline.calls != 0 {
		t.Fatalf("the pipeline ran for an oversize body")
	}
}

// Content-Type is enforced when present. Absent is tolerated -- a strictly-conformant client sends
// it, and refusing a body that parses because a header was missing is a compatibility break with no
// security benefit, since the body is validated regardless.
func TestContentTypeHandling(t *testing.T) {
	cases := []struct {
		ct   string
		want int
	}{
		{"application/json", http.StatusAccepted},
		{"application/json; charset=utf-8", http.StatusAccepted},
		{"", http.StatusAccepted},
		{"text/plain", http.StatusUnsupportedMediaType},
		{"application/x-www-form-urlencoded", http.StatusUnsupportedMediaType},
		{"application/yaml", http.StatusUnsupportedMediaType},
	}
	for _, c := range cases {
		t.Run(c.ct, func(t *testing.T) {
			h := newHarness(t)
			body := h.submittable(t, "platform.scale-deployment.json")
			w := h.post(t, body, func(r *http.Request) {
				r.Header.Del("Content-Type")
				if c.ct != "" {
					r.Header.Set("Content-Type", c.ct)
				}
			})
			if w.Code != c.want {
				t.Fatalf("Content-Type %q returned %d, want %d: %s", c.ct, w.Code, c.want, w.Body.String())
			}
		})
	}
}

// UnavailablePipeline is a 503 that names itself, not a 202.
//
// A skeleton returning 202 for an action it never performed would be the worst available
// placeholder: correct-looking to a caller, invisible in a test, and a lie in the journal. This
// test is what stops the seam from being filled that way.
func TestUnavailablePipelineIs503(t *testing.T) {
	h := newHarness(t)
	s, err := NewServer(Config{
		Authenticator: testAuthenticator(h.reviewer, h.security),
		Guard:         h.guard,
		Pipeline:      UnavailablePipeline{},
		Journal:       h.journal,
		Security:      h.security,
		Log:           logr.Discard(),
		Namespace:     testNamespace,
	})
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	h.server = s

	w := h.post(t, h.submittable(t, "platform.scale-deployment.json"))
	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503: %s", w.Code, w.Body.String())
	}
	if got := h.decode(t, w).Reason; got != "pipeline-not-installed" {
		t.Fatalf("reason = %q, want pipeline-not-installed", got)
	}
}

// NewServer refuses to build without any of its dependencies. Each of them being present is a
// security property, and a broker that started with a nil authenticator would accept everything.
func TestNewServerRequiresEveryDependency(t *testing.T) {
	full := func() Config {
		return Config{
			Authenticator: testAuthenticator(&fakeReviewer{}, nil),
			Guard:         NewReplayGuard(nil),
			Pipeline:      UnavailablePipeline{},
			Journal:       &recordingJournal{},
			Log:           logr.Discard(),
		}
	}
	for name, drop := range map[string]func(*Config){
		"authenticator": func(c *Config) { c.Authenticator = nil },
		"guard":         func(c *Config) { c.Guard = nil },
		"pipeline":      func(c *Config) { c.Pipeline = nil },
		"journal":       func(c *Config) { c.Journal = nil },
	} {
		t.Run(name, func(t *testing.T) {
			cfg := full()
			drop(&cfg)
			if _, err := NewServer(cfg); err == nil {
				t.Fatalf("NewServer succeeded with no %s", name)
			}
		})
	}
	if _, err := NewServer(full()); err != nil {
		t.Fatalf("NewServer with every dependency failed: %v", err)
	}
}

// A journal that cannot write must not change the caller's answer. The caller is being refused for
// a reason that has nothing to do with the journal, and reporting a journal failure instead would
// misstate why -- while a 500 would invite a retry that hits the same refusal.
func TestJournalFailureDoesNotChangeTheRefusal(t *testing.T) {
	h := newHarness(t)
	h.journal.err = errJournalDown

	w := h.post(t, read(t, filepath.Join(fixtureRoot, "spoofing", "bypass-key.bypass.json")))
	if w.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", w.Code)
	}
	if got := h.decode(t, w).Reason; got != ReasonBypassKey {
		t.Fatalf("reason = %q, want %q", got, ReasonBypassKey)
	}
}

// Every response is JSON with nosniff, including the refusals. One shape for success and refusal so
// a client parses one thing and cannot mistake a refusal for a malformed success.
func TestResponsesAreAlwaysJSON(t *testing.T) {
	h := newHarness(t)
	for _, body := range [][]byte{
		h.submittable(t, "platform.scale-deployment.json"),
		[]byte(`{"nope":1}`),
		[]byte(`not json`),
	} {
		w := h.post(t, body)
		if ct := w.Header().Get("Content-Type"); ct != "application/json" {
			t.Fatalf("Content-Type = %q, want application/json (status %d)", ct, w.Code)
		}
		if w.Header().Get("X-Content-Type-Options") != "nosniff" {
			t.Fatal("missing X-Content-Type-Options: nosniff")
		}
		var resp Response
		if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
			t.Fatalf("body is not a Response: %s", w.Body.String())
		}
	}
}

// reservedKeyOf recovers the offending key from the refusal detail.
func TestReservedKeyOf(t *testing.T) {
	for key := range ReservedKeys {
		_, err := DecodeEnvelope([]byte(`{"` + key + `":true}`))
		ref := refusalOf(t, err)
		if got := reservedKeyOf(ref); got != key {
			t.Fatalf("reservedKeyOf for %q = %q", key, got)
		}
	}
	// Not a reserved-key refusal: no key to report, and inventing one would put a field name in a
	// security record that nothing in the request actually carried.
	if got := reservedKeyOf(&Refusal{Reason: ReasonMalformed, Detail: `something "quoted"`}); got != "" {
		t.Fatalf("reservedKeyOf on a non-reserved refusal = %q, want empty", got)
	}
}

var errJournalDown = &Refusal{Reason: ReasonJournalUnavailable, Detail: "etcd is unreachable"}
