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

package gateway

import (
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval"
)

// BearerVerifier authenticates the bearer token Google Chat attaches to every webhook delivery.
type BearerVerifier interface {
	Verify(token string) error
}

// SharedSecretVerifier is the v1 verifier: Google Chat's "app determines its own verification
// token" mode, a static shared secret configured on both the Chat API app and this Deployment's
// Secret. Comparison is constant-time so response timing cannot leak the secret.
//
// The stronger alternative — verifying Google's own signed bearer JWT against Google's published
// JWKS (issuer chat@system.gserviceaccount.com) — needs a JWT library this module does not
// otherwise depend on and network access to Google's JWKS endpoint from inside the cluster; v1
// ships the shared secret and BearerVerifier is the seam a JWKS-based verifier drops into without
// changing anything else in this package.
type SharedSecretVerifier struct {
	Token string
}

func (v SharedSecretVerifier) Verify(token string) error {
	if subtle.ConstantTimeCompare([]byte(v.Token), []byte(token)) != 1 {
		return fmt.Errorf("gateway: googlechat bearer token mismatch")
	}
	return nil
}

var _ BearerVerifier = SharedSecretVerifier{}

// googleChatSender is the subset of Google Chat's message-event payload the gateway needs.
type googleChatEvent struct {
	Type    string `json:"type"`
	Message struct {
		Name   string `json:"name"`
		Text   string `json:"text"`
		Sender struct {
			Name string `json:"name"`
			Type string `json:"type"`
		} `json:"sender"`
	} `json:"message"`
}

// GoogleChatHandler verifies and dispatches Google Chat message events on the gateway's own
// dedicated Chat app (chat-approval.md §3).
type GoogleChatHandler struct {
	Dispatcher *Dispatcher
	Verifier   BearerVerifier
}

func (h *GoogleChatHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	auth := r.Header.Get("Authorization")
	token, ok := strings.CutPrefix(auth, "Bearer ")
	if !ok {
		http.Error(w, "missing bearer token", http.StatusUnauthorized)
		return
	}
	if err := h.Verifier.Verify(token); err != nil {
		http.Error(w, err.Error(), http.StatusUnauthorized)
		return
	}

	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, "reading body", http.StatusBadRequest)
		return
	}
	var event googleChatEvent
	if err := json.Unmarshal(body, &event); err != nil {
		http.Error(w, "invalid event body", http.StatusBadRequest)
		return
	}
	if event.Type != "MESSAGE" {
		w.WriteHeader(http.StatusOK) // ADDED_TO_SPACE, REMOVED_FROM_SPACE, CARD_CLICKED — nothing to do in v1
		return
	}
	// HUMAN only. A bot- or app-authored message replaying "approve ar-..." (e.g. the gateway's own
	// resolved-message edit, which a space configuration could in principle echo back as a new
	// message) must never be treated as a command.
	if event.Message.Sender.Type != "HUMAN" {
		w.WriteHeader(http.StatusOK)
		return
	}

	principal := approval.GoogleChatPrincipal(event.Message.Sender.Name)
	reply := h.Dispatcher.Handle(r.Context(), event.Message.Name, principal, event.Message.Text)

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	if reply == "" {
		fmt.Fprint(w, `{}`)
		return
	}
	raw, _ := json.Marshal(map[string]string{"text": reply})
	w.Write(raw)
}
