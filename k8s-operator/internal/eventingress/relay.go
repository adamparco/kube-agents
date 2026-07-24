// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// Package eventingress delivers non-chat machine push (alerts, GitHub webhooks) to the agent's LOCAL
// session-inject seam. It is the shared terminus of decision D1 (Phase 4, 04 §4): every non-chat push
// converges on `POST {daemon}/sessions` then `POST /sessions/{sid}/inject` on the same in-pod daemon
// (127.0.0.1:8699 after S1), reusing the exact protocol the k8s-event-watcher sidecar already speaks —
// bearer auth + X-Asserted-Caller owner claim + a `{"message": "<json>"}` envelope whose inner JSON
// carries the `kind` discriminator (S2). The genuinely-cloud transport (Pub/Sub subscribe, GitHub
// webhook HMAC) lives in sub-packages so this core — normalization + the loopback delivery — unit-tests
// with no GCP SDK, exactly as the router keeps pubsubinbound out of its security core.
//
// INVARIANTS this package must never break:
//   - Read-only: the relay only POSTs to the local session seam; it performs no cluster/cloud mutation.
//     A push trigger changes only WHEN an agent wakes, never WHAT it may do (04 §4).
//   - Subscribe-only: the Pub/Sub source (pubsubsource) never creates a topic or publishes; it only
//     drains a pre-created subscription.
//   - Loopback delivery: the daemon URL is the in-pod seam; eventingress runs as a sidecar so this is a
//     same-pod loopback call, never a cross-pod / cross-tier network path (invariant 3).
package eventingress

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// RelayConfig configures the loopback delivery client. It is protocol-identical to the
// k8s-event-watcher's injectorConfig on purpose: a machine push must be indistinguishable to the seam
// whether it originates from the watcher sidecar or the eventingress sidecar (D1, "one delivery
// contract"). Kept as a separate type (rather than importing the watcher's package main) so this
// component stays independently buildable and testable.
type RelayConfig struct {
	// DaemonURL is the base endpoint of the local session seam (e.g. "http://127.0.0.1:8699"),
	// without a trailing slash.
	DaemonURL string

	// BearerToken authenticates the caller to the seam (S1). Read from the token env var by main.
	BearerToken string

	// AssertedCaller is the X-Asserted-Caller owner claim (the agent's tier). The seam's
	// _require_inject_auth checks it against SESSION_KV_ALLOWED_OWNERS when set (S1).
	AssertedCaller string

	// HTTPClient is optional; tests inject an httptest-backed client. A modest-timeout client is
	// used when nil.
	HTTPClient *http.Client
}

// Relay creates a session and injects a normalized event into it on the local seam.
type Relay struct {
	cfg    RelayConfig
	client *http.Client
}

// NewRelay validates config and returns a Relay. Mirrors newInjector: a missing daemon URL or bearer
// token is a hard error (the seam enforces auth, so an empty token would only produce 401s).
func NewRelay(cfg RelayConfig) (*Relay, error) {
	if cfg.DaemonURL == "" {
		return nil, errors.New("eventingress: DaemonURL is required")
	}
	if strings.HasSuffix(cfg.DaemonURL, "/") {
		return nil, fmt.Errorf("eventingress: DaemonURL must not end with '/' (got %q)", cfg.DaemonURL)
	}
	if cfg.BearerToken == "" {
		return nil, errors.New("eventingress: BearerToken is required")
	}
	client := cfg.HTTPClient
	if client == nil {
		client = &http.Client{Timeout: 10 * time.Second}
	}
	return &Relay{cfg: cfg, client: client}, nil
}

// createSessionResponse maps the seam's POST /sessions reply.
type createSessionResponse struct {
	SessionID string `json:"sessionID"`
}

// injectEnvelope is the seam's inject body: the normalized event is JSON-encoded into the "message"
// string, which inject_message then json.loads() and routes on its "kind" field (S2).
type injectEnvelope struct {
	Message string `json:"message"`
}

// CreateSession opens a new session on the seam and returns its ID.
func (r *Relay) CreateSession(ctx context.Context) (string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, r.cfg.DaemonURL+"/sessions", nil)
	if err != nil {
		return "", fmt.Errorf("eventingress: build POST /sessions: %w", err)
	}
	r.setAuth(req)
	resp, err := r.client.Do(req)
	if err != nil {
		return "", fmt.Errorf("eventingress: POST /sessions: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusCreated {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return "", fmt.Errorf("eventingress: POST /sessions: status %d: %s", resp.StatusCode, string(body))
	}
	var payload createSessionResponse
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return "", fmt.Errorf("eventingress: decode POST /sessions response: %w", err)
	}
	if payload.SessionID == "" {
		return "", errors.New("eventingress: POST /sessions returned empty sessionID")
	}
	return payload.SessionID, nil
}

// Inject posts a normalized event to a session. The event MUST carry a "kind" the seam recognizes
// (alert | github | escalation | k8s-event); an unknown/missing kind is rejected server-side (S2).
func (r *Relay) Inject(ctx context.Context, sessionID string, event NormalizedEvent) error {
	if sessionID == "" {
		return errors.New("eventingress: Inject: sessionID is required")
	}
	if err := event.validate(); err != nil {
		return err
	}
	inner, err := json.Marshal(map[string]any(event))
	if err != nil {
		return fmt.Errorf("eventingress: marshal event: %w", err)
	}
	wrapped, err := json.Marshal(injectEnvelope{Message: string(inner)})
	if err != nil {
		return fmt.Errorf("eventingress: wrap inject envelope: %w", err)
	}
	url := r.cfg.DaemonURL + "/sessions/" + sessionID + "/inject"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(wrapped))
	if err != nil {
		return fmt.Errorf("eventingress: build POST inject: %w", err)
	}
	r.setAuth(req)
	req.Header.Set("Content-Type", "application/json")
	resp, err := r.client.Do(req)
	if err != nil {
		return fmt.Errorf("eventingress: POST inject: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("eventingress: POST inject: status %d: %s", resp.StatusCode, string(body))
	}
	return nil
}

// Deliver is the whole D1 contract in one call: open a session, then inject the event into it. Sources
// (Pub/Sub, GitHub, synthetic) call this and nothing else.
func (r *Relay) Deliver(ctx context.Context, event NormalizedEvent) (string, error) {
	sid, err := r.CreateSession(ctx)
	if err != nil {
		return "", err
	}
	if err := r.Inject(ctx, sid, event); err != nil {
		return sid, err
	}
	return sid, nil
}

// setAuth stamps the bearer + owner claim on every seam request (S1). Extracted so CreateSession and
// Inject cannot drift on how they authenticate.
func (r *Relay) setAuth(req *http.Request) {
	req.Header.Set("Authorization", "Bearer "+r.cfg.BearerToken)
	if r.cfg.AssertedCaller != "" {
		req.Header.Set("X-Asserted-Caller", r.cfg.AssertedCaller)
	}
}
