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

package eventingress

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
)

// fakeSeam is an httptest stand-in for the session-inject seam. It records the auth headers and the
// inject envelope so tests can assert the D1 delivery contract without the real FastAPI server.
type fakeSeam struct {
	gotCreateAuth   string
	gotCreateCaller string
	gotInjectAuth   string
	gotInjectCaller string
	gotInjectBody   map[string]any
	injectStatus    int // override the /inject response status (0 => 200)
}

func (s *fakeSeam) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/sessions", func(w http.ResponseWriter, r *http.Request) {
		s.gotCreateAuth = r.Header.Get("Authorization")
		s.gotCreateCaller = r.Header.Get("X-Asserted-Caller")
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(map[string]string{"sessionID": "k8s-evt-test01"})
	})
	mux.HandleFunc("/sessions/k8s-evt-test01/inject", func(w http.ResponseWriter, r *http.Request) {
		s.gotInjectAuth = r.Header.Get("Authorization")
		s.gotInjectCaller = r.Header.Get("X-Asserted-Caller")
		body, _ := io.ReadAll(r.Body)
		var env struct {
			Message string `json:"message"`
		}
		_ = json.Unmarshal(body, &env)
		_ = json.Unmarshal([]byte(env.Message), &s.gotInjectBody)
		if s.injectStatus != 0 {
			w.WriteHeader(s.injectStatus)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]string{"status": "injected"})
	})
	return mux
}

func newTestRelay(t *testing.T, srv *httptest.Server) *Relay {
	t.Helper()
	r, err := NewRelay(RelayConfig{
		DaemonURL:      srv.URL,
		BearerToken:    "secret-token",
		AssertedCaller: "platform",
		HTTPClient:     srv.Client(),
	})
	if err != nil {
		t.Fatalf("NewRelay: %v", err)
	}
	return r
}

func TestRelayDeliverSendsAuthAndEnvelope(t *testing.T) {
	seam := &fakeSeam{}
	srv := httptest.NewServer(seam.handler())
	defer srv.Close()

	relay := newTestRelay(t, srv)
	event := NormalizedEvent{"kind": KindAlert, "summary": "High error rate", "policy": "5xx-slo"}
	sid, err := relay.Deliver(context.Background(), event)
	if err != nil {
		t.Fatalf("Deliver: %v", err)
	}
	if sid != "k8s-evt-test01" {
		t.Errorf("session id = %q, want k8s-evt-test01", sid)
	}
	// D1/S1: bearer + owner claim on BOTH calls.
	for _, tc := range []struct{ name, auth, caller string }{
		{"create", seam.gotCreateAuth, seam.gotCreateCaller},
		{"inject", seam.gotInjectAuth, seam.gotInjectCaller},
	} {
		if tc.auth != "Bearer secret-token" {
			t.Errorf("%s Authorization = %q, want Bearer secret-token", tc.name, tc.auth)
		}
		if tc.caller != "platform" {
			t.Errorf("%s X-Asserted-Caller = %q, want platform", tc.name, tc.caller)
		}
	}
	// S2: the inner message carries the kind discriminator and fields verbatim.
	if got := seam.gotInjectBody["kind"]; got != KindAlert {
		t.Errorf("inject kind = %v, want %s", got, KindAlert)
	}
	if got := seam.gotInjectBody["summary"]; got != "High error rate" {
		t.Errorf("inject summary = %v, want High error rate", got)
	}
}

func TestRelayInjectRejectsUnknownKind(t *testing.T) {
	seam := &fakeSeam{}
	srv := httptest.NewServer(seam.handler())
	defer srv.Close()
	relay := newTestRelay(t, srv)

	// A missing kind must be rejected locally (before the wire) — the seam would 400 it anyway (S2).
	if _, err := relay.Deliver(context.Background(), NormalizedEvent{"summary": "no kind"}); err == nil {
		t.Fatal("expected error delivering event with no kind, got nil")
	}
	if _, err := relay.Deliver(context.Background(), NormalizedEvent{"kind": "pagerduty"}); err == nil {
		t.Fatal("expected error delivering event with unknown kind, got nil")
	}
}

func TestRelayInjectPropagatesSeamError(t *testing.T) {
	seam := &fakeSeam{injectStatus: http.StatusUnauthorized}
	srv := httptest.NewServer(seam.handler())
	defer srv.Close()
	relay := newTestRelay(t, srv)

	if _, err := relay.Deliver(context.Background(), NormalizedEvent{"kind": KindAlert}); err == nil {
		t.Fatal("expected error when seam returns 401, got nil")
	}
}

func TestNewRelayValidatesConfig(t *testing.T) {
	if _, err := NewRelay(RelayConfig{BearerToken: "x"}); err == nil {
		t.Error("expected error for empty DaemonURL")
	}
	if _, err := NewRelay(RelayConfig{DaemonURL: "http://x/", BearerToken: "x"}); err == nil {
		t.Error("expected error for trailing-slash DaemonURL")
	}
	if _, err := NewRelay(RelayConfig{DaemonURL: "http://x"}); err == nil {
		t.Error("expected error for empty BearerToken")
	}
}
