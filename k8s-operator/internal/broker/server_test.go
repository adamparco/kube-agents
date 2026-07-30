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
// source.
//
// Reshaped 2026-07-30 (P9-T7c-2c) on a human ruling. The old form asserted a NUMBER: one mutating
// route. That was never in 03 §4.1, which the check cites; it was the check's own paraphrase, true
// while one route existed and wrong the moment 05 §1.3's `replay` and `approve` are built. The
// replacement asserts what the source actually requires, and it is strictly more:
//
//	1. MutatingRoutes() EQUALS the registered set less the declared non-mutating allowlist. Derived,
//	   so the declaration cannot drift from the server it describes.
//	2. That set is a SUBSET of the 05 §1.3 route table -- a route the design does not name cannot
//	   exist; a route it does name may.
//	3. Every member of it traverses 03 §4.1's non-skippable steps: authenticate at step 1 and reach
//	   Pipeline.Submit for 3-11. Asserted over the call graph, because a mutating handler that
//	   returns early is a door that opens onto nothing the journal will ever see.
//	4. The registration point is unique. Exactly one call site touches the mux, and it is `handle`,
//	   which is what makes (1) ground truth rather than a second copy.
//
// What replaced what matters here: the old part 5 pinned `strings.Count(src, "s.mux.HandleFunc(")`
// to 4. That did catch a smuggled handler, but it needed editing for every legitimate route, which
// makes it a check whose maintenance instruction is "raise the number until it passes".

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
	// lastTr is the trace the handler had already filled in by the time it called. Recorded so the
	// handler's own half of the 03 §4.1 order is assertable without a real pipeline.
	lastTr *StepTrace
	result *Result
	err    error
}

func (p *stubPipeline) Submit(_ context.Context, id *Identity, e *Envelope, tr *StepTrace) (*Result, error) {
	p.calls++
	p.lastID, p.lastEn, p.lastTr = id, e, tr
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
	return newHarnessWithLog(t, logr.Discard())
}

// newHarnessWithLog is newHarness with an observable logger, for the tests that assert on the step
// trace the handler emits.
func newHarnessWithLog(t *testing.T, log logr.Logger) *harness {
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
		Log:           log,
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

// designMutatingRoutes is the 05 §1.3 route table -- every path the design sanctions as a door into
// the pipeline. `approve` and `replay` do not exist yet (P10-T4/T7 and P9-T7c-2b); they are listed
// because this is the DESIGN's table, and the assertion below is a subset, not an equality. A route
// the design names may exist; a route it does not name may not.
//
// Both unbuilt entries carry `{actionId}` as Go 1.22 wildcard syntax, which is how they will be
// registered. Nothing matches them today, which is the point: the subset holds trivially now and
// starts doing work the moment someone registers a fourth handler.
var designMutatingRoutes = map[string]bool{
	"/v1alpha1/actions":                    true,
	"/v1alpha1/actions/{actionId}/approve": true,
	"/v1alpha1/actions/{actionId}/replay":  true,
}

// V-BRK-021, part 1: the mutating surface is derived from what was registered, and bounded by what
// the design names.
//
// The equality is the load-bearing half. MutatingRoutes() is not a literal any more -- it is
// Registered() minus nonMutatingPaths -- so this recomputes the subtraction independently and
// compares. A handler registered without being added to the allowlist appears here as mutating,
// where the design-table subset refuses it. That is the drift the old `[]string{ActionsPath}` could
// not see: it would have kept reporting one route while the mux served two.
func TestMutatingSurfaceIsDerivedAndBoundedByTheDesign(t *testing.T) {
	h := newHarness(t)

	// Registered() is the ground truth, and it must actually contain something -- a server that
	// registered nothing would satisfy every set relation below vacuously.
	reg := h.server.Registered()
	if len(reg) == 0 {
		t.Fatal("Registered() is empty, so every assertion in this test is vacuous")
	}
	if !containsStr(reg, CatchAllPath) {
		t.Errorf("Registered() = %v, which omits the catch-all %q; an unregistered catch-all means "+
			"the mux's default handler serves unknown paths and part 2's 404s prove nothing about this server", reg, CatchAllPath)
	}

	// (0) The reporters FOLLOW the registrations.
	//
	// This arm exists because the sweep found the ones below insufficient without it, and the reason
	// is worth stating: on a server with one mutating route, a correct literal and a real derivation
	// return the same answer. Every set relation in this test held with `MutatingRoutes()` rewritten
	// back to `[]string{ActionsPath}` and with `Registered()` rewritten to the four production
	// paths -- because both literals were still true of THIS server. "Derived, not declared" is not a
	// property of one observation; it only becomes visible when the input varies.
	//
	// So vary it. A second server registering a path nothing else knows about: if either reporter is
	// a literal, it cannot mention that path, and if it is a derivation it must.
	probe := &Server{mux: http.NewServeMux()}
	probe.handle("/v1alpha1/only-here", func(http.ResponseWriter, *http.Request) {})
	if r := probe.Registered(); len(r) != 1 || r[0] != "/v1alpha1/only-here" {
		t.Errorf("a server registering only %q reports Registered() = %v; Registered() is restating "+
			"a fixed list instead of reporting what handle() recorded", "/v1alpha1/only-here", r)
	}
	if m := probe.MutatingRoutes(); len(m) != 1 || m[0] != "/v1alpha1/only-here" {
		t.Errorf("a server registering only %q reports MutatingRoutes() = %v; the mutating surface "+
			"is a literal, so it will keep naming the routes it was written with while the mux "+
			"serves others -- which is the drift this reshape of V-BRK-021 exists to end", "/v1alpha1/only-here", m)
	}
	if r := probe.Routes(); len(r) != 1 || r[0] != "/v1alpha1/only-here" {
		t.Errorf("a server registering only %q reports Routes() = %v", "/v1alpha1/only-here", r)
	}
	// And the catch-all is subtracted by Routes() rather than by coincidence.
	probe2 := &Server{mux: http.NewServeMux()}
	probe2.handle(CatchAllPath, func(http.ResponseWriter, *http.Request) {})
	if r := probe2.Routes(); len(r) != 0 {
		t.Errorf("a server registering only the catch-all reports Routes() = %v, want none", r)
	}

	// (1) The equality. Recomputed here rather than trusted, so that a MutatingRoutes() rewritten
	// back into a literal diverges from the registration and fails.
	var wantMutating []string
	for _, p := range reg {
		if !nonMutatingPaths[p] {
			wantMutating = append(wantMutating, p)
		}
	}
	sort.Strings(wantMutating)
	got := h.server.MutatingRoutes()
	if strings.Join(got, ",") != strings.Join(wantMutating, ",") {
		t.Fatalf("MutatingRoutes() = %v, but Registered() minus the declared non-mutating allowlist is %v; "+
			"the mutating surface must be derived from the registrations, not declared beside them", got, wantMutating)
	}
	if len(got) == 0 {
		t.Fatal("no route is classified as mutating; the broker's whole purpose is one, so either " +
			"the allowlist swallowed the submission route or nothing registered it")
	}

	// (2) The subset. This is where a fourth door fails.
	if unnamed := notInDesignTable(got); len(unnamed) > 0 {
		t.Errorf("%v mutate and are not in the 05 §1.3 route table; a mutating route the design "+
			"does not name is a second way into the executor, and 03 §4.1 says there is no other write path", unnamed)
	}

	// (3) The allowlist is itself bounded. Subtracting a declared set from the registrations makes
	// forgetting safe -- but only while the declared set stays honest. A new route added AND
	// declared non-mutating passes (1) and (2) trivially, because it never enters the mutating set
	// to be measured. So the allowlist may name only the three paths that genuinely cannot write:
	// the probe, the nonce issuer, and the absence of a route.
	inert := map[string]bool{HealthzPath: true, NoncePath: true, CatchAllPath: true}
	for p := range nonMutatingPaths {
		if !inert[p] {
			t.Errorf("nonMutatingPaths declares %q inert. Only %q, %q and %q are; anything else is a "+
				"route excusing itself from the mutating surface it belongs to, which is the one way "+
				"past both the equality and the design-table subset", p, HealthzPath, NoncePath, CatchAllPath)
		}
	}
	for p := range inert {
		if !nonMutatingPaths[p] {
			t.Errorf("%q is not declared in nonMutatingPaths, so it is reported as mutating; either "+
				"it grew the ability to write or the allowlist lost an entry", p)
		}
	}

	// Routes() is the registered set less the catch-all, and the submission route is in it.
	if r := h.server.Routes(); containsStr(r, CatchAllPath) || !containsStr(r, ActionsPath) {
		t.Errorf("Routes() = %v, want the registered paths without %q and with %q", r, CatchAllPath, ActionsPath)
	}
	if Port != 8443 {
		t.Fatalf("Port = %d, want 8443 (08 §2.3)", Port)
	}
}

// notInDesignTable is the subset test, extracted so it can be exercised on inputs this server does
// not produce. That is the whole point of pulling it out: the reshape's central claim is that the
// check accepts a route 05 §1.3 names and refuses one it does not, and the accepting half cannot be
// demonstrated against a server that has only ever had one route.
func notInDesignTable(mutating []string) []string {
	var out []string
	for _, p := range mutating {
		if !designMutatingRoutes[p] {
			out = append(out, p)
		}
	}
	return out
}

// The control the reshape exists for, and the reason V-BRK-021 was a spec contradiction until now.
//
// The old assertion was `len(MutatingRoutes()) != 1` plus a registration count pinned to 4. Both go
// red on `/v1alpha1/actions/{actionId}/replay` -- a route 05 §1.3 designs, that 03 §4.1 permits
// because it runs the full pipeline, and that C-UC cannot do its job without. A check that refuses
// what the design requires is not a strict check, it is a wrong one, and the harness had to halt
// rather than pick a side (P9-T7c-2b, 2026-07-29).
//
// So this asserts the accepting half directly: all three design routes pass, and a debug door in
// the same shape does not. Without it the subset arm is only ever exercised on a one-element set,
// where "subset of the table" and "equal to ActionsPath" are indistinguishable.
func TestTheSubsetArmAcceptsEveryRouteTheDesignNamesAndNothingElse(t *testing.T) {
	all := []string{
		"/v1alpha1/actions",
		"/v1alpha1/actions/{actionId}/approve",
		"/v1alpha1/actions/{actionId}/replay",
	}
	if len(designMutatingRoutes) != len(all) {
		t.Fatalf("designMutatingRoutes has %d entries, this control names %d; 05 §1.3's table has "+
			"three rows and both copies must move together", len(designMutatingRoutes), len(all))
	}
	if bad := notInDesignTable(all); len(bad) > 0 {
		t.Errorf("the subset arm refuses %v, which 05 §1.3 names; this is the exact failure that "+
			"made the old count a spec contradiction", bad)
	}
	for _, p := range []string{"/v1alpha1/debug/apply", "/v1alpha1/actions/force", "/admin", "/v1alpha1/actions/{actionId}"} {
		if bad := notInDesignTable([]string{p}); len(bad) == 0 {
			t.Errorf("the subset arm accepts %q, which no 05 §1.3 row names", p)
		}
	}
}

func containsStr(hay []string, needle string) bool {
	for _, s := range hay {
		if s == needle {
			return true
		}
	}
	return false
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
			t.Errorf("%s registers %d route(s); every route belongs in server.go, registered through handle()", name, n)
		}
	}
}

// V-BRK-021, part 6: the registration point is unique.
//
// This replaced a count. The old assertion pinned `strings.Count(src, "s.mux.HandleFunc(")` to
// exactly 4 -- three routes plus the catch-all -- which caught a smuggled handler and also went red
// on every legitimate route addition, so its maintenance instruction was "raise the number until it
// passes". A check you edit to make it pass is a check that will one day be edited past a real
// finding.
//
// The property that actually matters is not how many registrations there are; it is that
// MutatingRoutes() sees all of them. It sees exactly the ones that go through handle(), so:
// registrations that bypass handle() are the failure, at any count.
func TestTheMuxHasExactlyOneRegistrationPoint(t *testing.T) {
	src, err := os.ReadFile("server.go")
	if err != nil {
		t.Fatalf("read server.go: %v", err)
	}
	body := string(src)

	const marker = "s.mux.HandleFunc("
	n := strings.Count(body, marker)
	if n == 0 {
		t.Fatal("server.go never calls s.mux.HandleFunc; either the server stopped registering " +
			"handlers or this scan is looking for a spelling that no longer exists, and a scan that " +
			"finds nothing reports the same green as a scan that found nothing wrong")
	}
	if n != 1 {
		t.Errorf("server.go has %d call sites for %s; there must be exactly one, inside handle(), "+
			"because handle() is what records the path into Registered() and therefore what makes "+
			"MutatingRoutes() a derivation instead of a guess", n, marker)
	}

	// And the one call site is inside handle(), not somewhere that skips the recording.
	fn := strings.Index(body, "func (s *Server) handle(path string, h http.HandlerFunc) {")
	if fn < 0 {
		t.Fatal("server.go has no `func (s *Server) handle(path string, h http.HandlerFunc)`; the " +
			"single registration point this check is named for does not exist")
	}
	end := strings.Index(body[fn:], "\n}\n")
	if end < 0 {
		t.Fatal("could not find the end of handle()")
	}
	if !strings.Contains(body[fn:fn+end], marker) {
		t.Error("handle() does not call s.mux.HandleFunc, so the sole registration this check " +
			"located is somewhere that does not record the path into Registered()")
	}
	if !strings.Contains(body[fn:fn+end], "s.registered = append(s.registered, path)") {
		t.Error("handle() registers a path on the mux without recording it in s.registered; " +
			"MutatingRoutes() would then under-report the surface, which is the exact drift this " +
			"reshape of V-BRK-021 exists to make impossible")
	}
}

// V-BRK-021, part 7: every mutating route traverses the non-skippable steps.
//
// 03 §4.1: steps 1, 3, 4, 5, 6 and 11 are "not skippable by any caller". The count this check used
// to assert was a proxy for that sentence; this is the sentence. Steps 3-11 belong to the pipeline
// and are asserted step by step by V-BRK-011/014, so what has to be true out here is that the
// handler for each mutating route (a) authenticates before anything else and (b) hands off to the
// pipeline. A mutating route that answers without doing both is a write path with no journal entry,
// which is precisely what 03 §4.1 says cannot exist.
//
// Over the source rather than by driving requests, because a request-driven version can only prove
// it about the handler it knew to call, and the whole subject here is a handler nobody knew about.
func TestEveryMutatingRouteReachesTheAuthenticatorAndThePipeline(t *testing.T) {
	src, err := os.ReadFile("server.go")
	if err != nil {
		t.Fatalf("read server.go: %v", err)
	}
	body := string(src)

	h := newHarness(t)
	mutating := h.server.MutatingRoutes()
	if len(mutating) == 0 {
		t.Fatal("no mutating route to check; this test would pass over a broker that cannot write at all")
	}

	// Path constant -> handler method, read out of the registrations themselves so a rewired route
	// is followed rather than assumed.
	handlerFor := map[string]string{}
	for _, line := range strings.Split(body, "\n") {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, "s.handle(") {
			continue
		}
		args := strings.TrimSuffix(strings.TrimPrefix(line, "s.handle("), ")")
		constName, method, ok := strings.Cut(args, ", ")
		if !ok {
			t.Fatalf("cannot parse registration %q", line)
		}
		handlerFor[pathOfConst(t, body, strings.TrimSpace(constName))] = strings.TrimSpace(method)
	}

	reEntryChecked := 0
	for _, route := range mutating {
		method, ok := handlerFor[route]
		if !ok {
			t.Errorf("mutating route %q has no registration this scan could follow; it cannot be "+
				"shown to traverse anything", route)
			continue
		}
		fnSrc := funcBody(t, body, strings.TrimPrefix(method, "s."))
		if !strings.Contains(fnSrc, "s.cfg.Authenticator.Authenticate(") {
			t.Errorf("%s serves the mutating route %q and never calls Authenticator.Authenticate; "+
				"step 1 of 03 §4.1 is not skippable by any caller", method, route)
		}
		if !strings.Contains(fnSrc, "s.cfg.Pipeline.Submit(") {
			t.Errorf("%s serves the mutating route %q and never calls Pipeline.Submit; steps 3-11 "+
				"-- classify, plan undo, snapshot, execute, journal -- would not run, so the write "+
				"would happen outside everything that records it", method, route)
		}

		// A re-entry route -- 05 §1.3's `approve` and `replay` -- executes a plan the broker already
		// recorded. It takes an action ID and MUST NOT read operations off the wire: if it did, the
		// journal's account of what was approved would not be what ran, and the route would be the
		// submission route wearing a different name and a different caller identity.
		//
		// The population is EMPTY today and this loop body does not execute. That is recorded rather
		// than papered over: an assertion over nothing is not an assertion, and the count below is
		// what tells a later reader whether this clause was live when the phase closed.
		if route == ActionsPath {
			continue
		}
		reEntryChecked++
		if strings.Contains(fnSrc, "DecodeEnvelope(") {
			t.Errorf("%s serves the re-entry route %q and decodes a caller-supplied envelope; a "+
				"re-entry route replays what was journaled, so caller operations on it are a second "+
				"submission path that bypasses the attribution the first one recorded", method, route)
		}
	}
	if reEntryChecked == 0 {
		t.Logf("no re-entry route exists yet (05 §1.3's approve/replay are P10-T4/T7 and P9-T7c-2b); " +
			"the re-entry clause of V-BRK-021 is vacuous at this phase and is recorded as empty, not as satisfied")
	}
}

// pathOfConst resolves a route constant's name to its literal value by reading the const block, so
// the scan follows a renamed or re-pointed constant instead of matching on spelling.
func pathOfConst(t *testing.T, body, name string) string {
	t.Helper()
	for _, line := range strings.Split(body, "\n") {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, name+" = ") {
			continue
		}
		return strings.Trim(strings.TrimPrefix(line, name+" = "), `"`)
	}
	t.Fatalf("route constant %s has no `%s = \"...\"` declaration in server.go", name, name)
	return ""
}

// funcBody returns the source of a method on *Server, from its signature to the closing brace in
// column zero.
func funcBody(t *testing.T, body, method string) string {
	t.Helper()
	start := strings.Index(body, "func (s *Server) "+method+"(")
	if start < 0 {
		t.Fatalf("no method (s *Server) %s in server.go", method)
	}
	end := strings.Index(body[start:], "\n}\n")
	if end < 0 {
		t.Fatalf("could not find the end of %s", method)
	}
	return body[start : start+end]
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
