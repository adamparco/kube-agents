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
	"errors"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strconv"
	"strings"

	"github.com/go-logr/logr"
)

// The HTTP surface (08 §2.3, 03 §4.1).
//
// Three routes. One of them mutates. That ratio is the point of V-BRK-021: the broker's
// non-skippability argument is not "the pipeline checks everything", it is "there is nowhere else
// to go". A second mutating route -- a debug apply, an admin endpoint, a `?force=true` short
// circuit -- would not need to be exploited to break the model; it would only need to exist,
// because then "every mutation is journaled" would depend on which door was used.
const (
	// ActionsPath is the one mutating route.
	ActionsPath = "/v1alpha1/actions"
	// NoncePath issues the single-use nonces of 06 §4.1. Non-mutating with respect to the cluster.
	NoncePath = "/v1alpha1/nonce"
	// HealthzPath is the liveness and readiness probe. Unauthenticated by necessity -- the kubelet
	// has no projected token -- and therefore returns nothing but a constant.
	HealthzPath = "/healthz"

	// Port is the envelope port of 08 §2.3. Not configurable: the agent's Service, its
	// NetworkPolicy and the injected KUBEAGENTS_BROKER_ENDPOINT all name it, and a configurable
	// port is four places to disagree.
	Port = 8443

	// MaxRequestBytes caps a submission. Fifty operations each carrying a desiredState is a real
	// envelope; ten megabytes of it is not, and an unbounded read on the one process that must
	// stay up is a denial of service with no exploit required.
	MaxRequestBytes = 2 << 20
)

// bypassHeaders are request headers refused outright, mirroring the reserved body keys of 06 §4.1.
//
// They are listed rather than ignored for the same reason the body keys are: HTTP headers are the
// obvious second place to try, once the body has told you `bypass` is not accepted. A header that
// is silently dropped teaches a caller nothing; a 400 that names it is a record.
var bypassHeaders = []string{
	"X-Kube-Agents-Bypass",
	"X-Kube-Agents-Force",
	"X-Kube-Agents-Skip-Journal",
	"X-Kube-Agents-Skip-Verify",
	"X-Kube-Agents-Emergency",
	"X-Kube-Agents-Risk-Class",
	"X-Kube-Agents-Tier",
	"X-Kube-Agents-Scope",
	"X-Kube-Agents-Approved",
	"X-Kube-Agents-Dry-Run",
}

// Result is what the pipeline returns for an accepted submission.
type Result struct {
	ActionID  string
	Namespace string
	// Decision is the outcome word returned to the caller: accepted, deduplicated, rejected.
	Decision string
	Phase    string
	Message  string
	// Status is the HTTP status to return. Zero means 202 Accepted.
	Status int
}

// Pipeline is steps 3 through 11 of 03 §4.1 -- classify, plan undo, snapshot, journal, execute,
// verify, report. P9-T2 ships the front of the pipeline only; this interface is the seam the
// remaining tasks fill.
//
// It is an interface rather than a TODO in a handler so that "the pipeline is not installed" is a
// distinct, visible runtime state with its own status code, rather than a code path that looks
// like success. A skeleton that returned 202 for an action it never performed would be the worst
// possible placeholder: correct-looking to a caller, invisible in a test, and a lie in the journal.
type Pipeline interface {
	Submit(ctx context.Context, id *Identity, e *Envelope) (*Result, error)
}

// UnavailablePipeline is what a broker built without a pipeline runs. Every accepted, fully
// validated envelope gets a 503 that says exactly which build it is talking to.
type UnavailablePipeline struct{}

// Submit refuses.
func (UnavailablePipeline) Submit(context.Context, *Identity, *Envelope) (*Result, error) {
	return nil, &Refusal{
		Status: http.StatusServiceUnavailable,
		Reason: "pipeline-not-installed",
		Detail: "this broker validates and journals but cannot execute: the classification and execution pipeline is not built into this binary",
	}
}

// Config assembles a Server.
type Config struct {
	Authenticator *Authenticator
	Guard         *ReplayGuard
	Pipeline      Pipeline
	Journal       RejectionJournal
	Security      SecuritySink
	Log           logr.Logger
	// Namespace is the agent's namespace, echoed in responses so a caller can find its record
	// without knowing the deployment layout.
	Namespace string
}

// Server is the broker's HTTP handler.
type Server struct {
	cfg Config
	mux *http.ServeMux
}

// NewServer wires the routes. It returns an error rather than panicking on a missing dependency,
// because every one of these being present is a security property and a broker that started with
// a nil authenticator would be a broker that accepts everything.
func NewServer(cfg Config) (*Server, error) {
	switch {
	case cfg.Authenticator == nil:
		return nil, errors.New("broker: an Authenticator is required")
	case cfg.Guard == nil:
		return nil, errors.New("broker: a ReplayGuard is required")
	case cfg.Journal == nil:
		return nil, errors.New("broker: a RejectionJournal is required; refusals must be recorded")
	case cfg.Pipeline == nil:
		return nil, errors.New("broker: a Pipeline is required (use UnavailablePipeline to build without one)")
	}
	s := &Server{cfg: cfg, mux: http.NewServeMux()}

	// Registered by exact path. Go 1.22's pattern matcher would let `/v1alpha1/actions/` match a
	// subtree, and a subtree under the mutating route is a family of routes nobody enumerated.
	s.mux.HandleFunc(HealthzPath, s.handleHealthz)
	s.mux.HandleFunc(NoncePath, s.handleNonce)
	s.mux.HandleFunc(ActionsPath, s.handleActions)
	// Everything else. Explicit, so that a debug handler added by a future edit collides here
	// instead of quietly joining the surface.
	s.mux.HandleFunc("/", s.handleNotFound)
	return s, nil
}

// Routes reports every path the server serves, sorted. Exported so V-BRK-021 can assert the route
// set from a unit test rather than by probing a running process -- a probe can only prove the
// routes it thought to try.
func (s *Server) Routes() []string {
	out := []string{HealthzPath, NoncePath, ActionsPath}
	sort.Strings(out)
	return out
}

// MutatingRoutes reports the routes that can change cluster state. Exactly one, and asserted.
func (s *Server) MutatingRoutes() []string { return []string{ActionsPath} }

// ServeHTTP applies the checks that are true of every route before dispatching.
func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if ref := rejectBypassHeaders(r); ref != nil {
		s.refuse(r.Context(), w, r, nil, nil, ref)
		return
	}
	s.mux.ServeHTTP(w, r)
}

func (s *Server) handleHealthz(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeStatus(w, http.StatusMethodNotAllowed, Response{Reason: "method-not-allowed", Message: "GET only"})
		return
	}
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = io.WriteString(w, "ok\n")
}

func (s *Server) handleNotFound(w http.ResponseWriter, r *http.Request) {
	// A 404 with no hint of what would have worked. The route set is not a secret, but an endpoint
	// that suggests alternatives is an endpoint that enumerates itself.
	writeStatus(w, http.StatusNotFound, Response{
		Reason:  "no-such-route",
		Message: fmt.Sprintf("%s is not a broker route", r.URL.Path),
	})
}

func (s *Server) handleNonce(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeStatus(w, http.StatusMethodNotAllowed, Response{Reason: "method-not-allowed", Message: "GET only"})
		return
	}
	id, err := s.cfg.Authenticator.Authenticate(r.Context(), r)
	if err != nil {
		// Authenticate has already emitted any security event; this path only renders it.
		s.write(w, err)
		return
	}
	value, err := s.cfg.Guard.IssueNonce(id.Username)
	if err != nil {
		s.write(w, err)
		return
	}
	writeStatus(w, http.StatusOK, Response{Nonce: value, ExpiresInSeconds: int(NonceTTL.Seconds())})
}

// handleActions is the mutating route. Its order is the order of 03 §4.1, and none of it is
// conditional on anything the caller sends.
func (s *Server) handleActions(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()

	if r.Method != http.MethodPost {
		writeStatus(w, http.StatusMethodNotAllowed, Response{Reason: "method-not-allowed", Message: "POST only"})
		return
	}
	// No query parameters, at all. Not a list of forbidden ones: an allowlist of zero is the only
	// version of this check that a new parameter cannot slip past.
	if len(r.URL.Query()) > 0 {
		s.refuse(ctx, w, r, nil, nil, &Refusal{
			Status:        http.StatusBadRequest,
			Reason:        "unsupported-query-parameter",
			Detail:        "the actions route takes no query parameters; every input is in the signed envelope",
			SecurityEvent: true,
		})
		return
	}
	if ct := r.Header.Get("Content-Type"); ct != "" {
		if mediaType, _, _ := strings.Cut(ct, ";"); strings.TrimSpace(mediaType) != "application/json" {
			writeStatus(w, http.StatusUnsupportedMediaType, Response{
				Reason:  "unsupported-media-type",
				Message: "the envelope is application/json",
			})
			return
		}
	}

	// Step 1: identity. Before the body is read, so an unauthenticated peer cannot make the broker
	// allocate two megabytes.
	id, err := s.cfg.Authenticator.Authenticate(ctx, r)
	if err != nil {
		s.write(w, err)
		return
	}

	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, MaxRequestBytes))
	if err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			writeStatus(w, http.StatusRequestEntityTooLarge, Response{
				Reason:  "envelope-too-large",
				Message: fmt.Sprintf("the envelope exceeds %d bytes", MaxRequestBytes),
			})
			return
		}
		writeStatus(w, http.StatusBadRequest, Response{Reason: ReasonMalformed, Message: "could not read the request body"})
		return
	}

	env, err := DecodeEnvelope(body)
	if err != nil {
		s.refuse(ctx, w, r, id, body, err)
		return
	}

	// The recomputation, before the replay guard. Deliberately in that order: a key mismatch is a
	// client bug, and running the guard first would burn a single-use nonce on every one of them
	// until the caller hit its outstanding-nonce quota and started seeing a second, unrelated
	// error. Nothing is skipped by the ordering -- both checks run on every submission, and both
	// precede classification, which is what 06 §4.1 requires.
	if err := CompareIdempotencyKey(id.AgentIdentity(), env); err != nil {
		s.refuse(ctx, w, r, id, body, err)
		return
	}

	// Step 2: anti-replay. All three mechanisms, before classification.
	if err := s.cfg.Guard.Check(id, env); err != nil {
		s.refuse(ctx, w, r, id, body, err)
		return
	}

	// A repeat of a key already seen in a DIFFERENT trace is a retry, not a replay: return the
	// original record instead of doing the work twice.
	if prior, ok := s.cfg.Guard.LookupDedup(id.AgentIdentity(), env.IdempotencyKey); ok {
		writeStatus(w, http.StatusOK, Response{
			ActionID:  prior.ActionID,
			Namespace: prior.Namespace,
			Decision:  "deduplicated",
			Message:   "an identical action was already submitted; returning the original record",
			TraceID:   env.Trace.TraceID,
		})
		return
	}

	result, err := s.cfg.Pipeline.Submit(ctx, id, env)
	if err != nil {
		s.refuse(ctx, w, r, id, body, err)
		return
	}

	s.cfg.Guard.RememberDedup(id.AgentIdentity(), env.IdempotencyKey, DedupEntry{
		ActionID:  result.ActionID,
		Namespace: result.Namespace,
		Decision:  result.Decision,
		At:        s.cfg.Guard.now(),
	})

	status := result.Status
	if status == 0 {
		status = http.StatusAccepted
	}
	writeStatus(w, status, Response{
		ActionID:  result.ActionID,
		Namespace: result.Namespace,
		Decision:  result.Decision,
		Phase:     result.Phase,
		Message:   result.Message,
		TraceID:   env.Trace.TraceID,
	})
}

// rejectBypassHeaders refuses the header analogues of the reserved body keys.
func rejectBypassHeaders(r *http.Request) *Refusal {
	for _, h := range bypassHeaders {
		if r.Header.Get(h) == "" {
			continue
		}
		return &Refusal{
			Status:        http.StatusBadRequest,
			Reason:        ReasonBypassKey,
			Detail:        fmt.Sprintf("the request carries the header %s; the broker has no header-driven overrides", h),
			Journal:       true,
			SecurityEvent: true,
		}
	}
	return nil
}

// Response is the broker's JSON reply. One shape for success and refusal, so a client parses one
// thing and a refusal cannot be mistaken for a malformed success.
type Response struct {
	ActionID  string `json:"actionId,omitempty"`
	Namespace string `json:"namespace,omitempty"`
	Decision  string `json:"decision,omitempty"`
	Phase     string `json:"phase,omitempty"`
	Message   string `json:"message,omitempty"`
	// Reason is the machine-readable refusal reason. Empty on success.
	Reason  string `json:"reason,omitempty"`
	TraceID string `json:"traceId,omitempty"`
	// Nonce and ExpiresInSeconds are the GET /v1alpha1/nonce reply.
	Nonce            string `json:"nonce,omitempty"`
	ExpiresInSeconds int    `json:"expiresInSeconds,omitempty"`
	// RetryAfterSeconds mirrors the Retry-After header into the body, because 06 §4.4 specifies it
	// as a body field on the pause refusal and an agent runtime that parses JSON should not also
	// have to reach for headers to find out whether its refusal was temporary.
	RetryAfterSeconds int `json:"retryAfterSeconds,omitempty"`
}

// refuse renders a refusal and performs its journaling and alarming side effects.
//
// Every refusal on the mutating route goes through this one function, which is what makes the
// per-reason table in 06 §4.1 enforceable: the decision of what to journal and what to alarm is
// carried on the Refusal itself, so no call site can be the one that forgot.
func (s *Server) refuse(ctx context.Context, w http.ResponseWriter, r *http.Request, id *Identity, body []byte, err error) {
	ref, ok := err.(*Refusal)
	if !ok {
		s.cfg.Log.Error(err, "broker: unclassified error on the actions route")
		writeStatus(w, http.StatusInternalServerError, Response{Reason: "internal-error"})
		return
	}

	if ref.Journal {
		if jErr := s.cfg.Journal.Reject(ctx, id, body, ref); jErr != nil {
			// Logged, not escalated. The caller is already being refused for a reason that has
			// nothing to do with the journal, and changing their answer would misreport why.
			s.cfg.Log.Error(jErr, "broker: could not journal a refusal", "reason", ref.Reason)
		}
	}
	if ref.SecurityEvent && s.cfg.Security != nil {
		caller := "unauthenticated"
		if id != nil {
			caller = id.Username
		}
		s.cfg.Security.Security(ctx, SecurityRecord{
			Reason:     ref.Reason,
			Detail:     ref.Detail,
			Caller:     caller,
			RemoteAddr: r.RemoteAddr,
			Path:       r.URL.Path,
			TraceID:    traceIDOf(body),
			Key:        reservedKeyOf(ref),
		})
	}
	s.write(w, ref)
}

// write renders a Refusal, or a 500 for anything else.
func (s *Server) write(w http.ResponseWriter, err error) {
	ref, ok := err.(*Refusal)
	if !ok {
		s.cfg.Log.Error(err, "broker: unclassified error")
		writeStatus(w, http.StatusInternalServerError, Response{Reason: "internal-error"})
		return
	}
	if ref.RetryAfterSeconds > 0 {
		// The header as well as the body: HTTP clients, proxies and retry middleware already
		// understand Retry-After, and a refusal that only says "wait" in a field nobody reads is a
		// refusal that gets retried immediately.
		w.Header().Set("Retry-After", strconv.Itoa(ref.RetryAfterSeconds))
	}
	writeStatus(w, ref.Status, Response{
		Reason:            ref.Reason,
		Message:           ref.Detail,
		Decision:          "rejected",
		RetryAfterSeconds: ref.RetryAfterSeconds,
	})
}

func writeStatus(w http.ResponseWriter, status int, body Response) {
	w.Header().Set("Content-Type", "application/json")
	// Belt and braces against a proxy or a browser reinterpreting the body.
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func traceIDOf(body []byte) string {
	t := traceFromBody(body)
	if t == nil {
		return ""
	}
	return t.TraceID
}

// reservedKeyOf recovers the offending key name from the refusal detail, so the security record
// has it as a structured field rather than only inside prose a log query cannot select on.
func reservedKeyOf(ref *Refusal) string {
	if ref.Reason != ReasonReservedKey && ref.Reason != ReasonBypassKey {
		return ""
	}
	_, rest, found := strings.Cut(ref.Detail, `"`)
	if !found {
		return ""
	}
	key, _, found := strings.Cut(rest, `"`)
	if !found {
		return ""
	}
	return key
}
