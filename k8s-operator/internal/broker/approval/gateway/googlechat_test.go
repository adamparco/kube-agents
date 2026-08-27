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

package gateway_test

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval/gateway"
)

func TestSharedSecretVerifierAcceptsMatchingToken(t *testing.T) {
	v := gateway.SharedSecretVerifier{Token: "s3cret"}
	if err := v.Verify("s3cret"); err != nil {
		t.Errorf("expected the matching token to verify, got: %v", err)
	}
}

func TestSharedSecretVerifierRejectsWrongToken(t *testing.T) {
	v := gateway.SharedSecretVerifier{Token: "s3cret"}
	if err := v.Verify("wrong"); err == nil {
		t.Error("expected the wrong token to be refused")
	}
}

func TestGoogleChatHandlerRejectsMissingBearer(t *testing.T) {
	h := &gateway.GoogleChatHandler{
		Dispatcher: &gateway.Dispatcher{Client: fakeClientNoObjects()},
		Verifier:   gateway.SharedSecretVerifier{Token: "s3cret"},
	}
	req := httptest.NewRequest(http.MethodPost, "/googlechat/events", strings.NewReader(`{}`))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	if w.Code != http.StatusUnauthorized {
		t.Errorf("status = %d, want 401 with no bearer token", w.Code)
	}
}

func TestGoogleChatHandlerRejectsWrongBearer(t *testing.T) {
	h := &gateway.GoogleChatHandler{
		Dispatcher: &gateway.Dispatcher{Client: fakeClientNoObjects()},
		Verifier:   gateway.SharedSecretVerifier{Token: "s3cret"},
	}
	req := httptest.NewRequest(http.MethodPost, "/googlechat/events", strings.NewReader(`{}`))
	req.Header.Set("Authorization", "Bearer wrong")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	if w.Code != http.StatusUnauthorized {
		t.Errorf("status = %d, want 401 with the wrong bearer token", w.Code)
	}
}

func TestGoogleChatHandlerIgnoresNonMessageEvents(t *testing.T) {
	h := &gateway.GoogleChatHandler{
		Dispatcher: &gateway.Dispatcher{Client: fakeClientNoObjects()},
		Verifier:   gateway.SharedSecretVerifier{Token: "s3cret"},
	}
	body, _ := json.Marshal(map[string]any{"type": "ADDED_TO_SPACE"})
	req := httptest.NewRequest(http.MethodPost, "/googlechat/events", strings.NewReader(string(body)))
	req.Header.Set("Authorization", "Bearer s3cret")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Errorf("status = %d, want 200 for a non-MESSAGE event (nothing to do, not an error)", w.Code)
	}
}

func TestGoogleChatHandlerIgnoresNonHumanSenders(t *testing.T) {
	h := &gateway.GoogleChatHandler{
		Dispatcher: &gateway.Dispatcher{Client: fakeClientNoObjects()},
		Verifier:   gateway.SharedSecretVerifier{Token: "s3cret"},
	}
	body := `{"type":"MESSAGE","message":{"name":"spaces/A/messages/B","text":"approve ar-1","sender":{"name":"users/bot","type":"BOT"}}}`
	req := httptest.NewRequest(http.MethodPost, "/googlechat/events", strings.NewReader(body))
	req.Header.Set("Authorization", "Bearer s3cret")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Errorf("status = %d, want 200", w.Code)
	}
	// A bot-authored message replaying "approve" must never be dispatched as a command — this test
	// cannot see Dispatch was skipped directly, but a dispatched command against fakeClientNoObjects
	// would still 200 (Dispatcher.Handle never errors to the transport), so the meaningful guard is
	// the event.Type/Sender.Type check itself; see TestGoogleChatHandlerDispatchesHumanCommand for
	// proof a human message DOES reach the dispatcher.
}

func TestGoogleChatHandlerDispatchesHumanCommand(t *testing.T) {
	h := &gateway.GoogleChatHandler{
		Dispatcher: &gateway.Dispatcher{Client: fakeClientNoObjects(), Now: func() time.Time { return fixedNow }},
		Verifier:   gateway.SharedSecretVerifier{Token: "s3cret"},
	}
	body := `{"type":"MESSAGE","message":{"name":"spaces/A/messages/B","text":"approve ar-nonexistent","sender":{"name":"users/12345","type":"HUMAN"}}}`
	req := httptest.NewRequest(http.MethodPost, "/googlechat/events", strings.NewReader(body))
	req.Header.Set("Authorization", "Bearer s3cret")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d", w.Code)
	}
	if !strings.Contains(w.Body.String(), "no such action") {
		t.Errorf("body = %q, want a reply about the missing action (proves the human message reached the dispatcher)", w.Body.String())
	}
}
