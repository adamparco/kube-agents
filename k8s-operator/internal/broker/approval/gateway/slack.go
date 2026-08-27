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
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval"
)

// slackClockSkew bounds how old a signed request may be before it is refused as a replay. Slack's
// own documented guidance for verifying requests.
const slackClockSkew = 5 * time.Minute

// SlackHandler verifies and dispatches Slack slash-command requests
// (POST application/x-www-form-urlencoded, per Slack's slash command contract) on the gateway's
// own dedicated app (chat-approval.md §3 — never the install's existing Slack app).
type SlackHandler struct {
	Dispatcher *Dispatcher
	// SigningSecret is the app's signing secret, used to verify the v0 HMAC-SHA256 signature Slack
	// attaches to every request. Required.
	SigningSecret string
	// Now is injectable for tests; defaults to time.Now.
	Now func() time.Time
}

func (h *SlackHandler) now() time.Time {
	if h.Now != nil {
		return h.Now()
	}
	return time.Now()
}

// VerifySignature implements Slack's request-signing scheme: the signature is
// HMAC-SHA256(signingSecret, "v0:"+timestamp+":"+rawBody), sent as "v0="+hex(mac) in
// X-Slack-Signature, alongside X-Slack-Request-Timestamp. Comparison is constant-time
// (hmac.Equal) so response timing cannot leak the secret one byte at a time.
func VerifySignature(signingSecret string, timestampHeader, signatureHeader string, body []byte, now time.Time) error {
	ts, err := strconv.ParseInt(timestampHeader, 10, 64)
	if err != nil {
		return fmt.Errorf("gateway: invalid X-Slack-Request-Timestamp %q: %w", timestampHeader, err)
	}
	skew := now.Sub(time.Unix(ts, 0))
	if skew < 0 {
		skew = -skew
	}
	if skew > slackClockSkew {
		return fmt.Errorf("gateway: slack request timestamp %d is outside the %s replay window", ts, slackClockSkew)
	}

	mac := hmac.New(sha256.New, []byte(signingSecret))
	mac.Write([]byte("v0:" + timestampHeader + ":"))
	mac.Write(body)
	want := "v0=" + hex.EncodeToString(mac.Sum(nil))

	if !hmac.Equal([]byte(want), []byte(signatureHeader)) {
		return fmt.Errorf("gateway: slack signature mismatch")
	}
	return nil
}

// ServeHTTP handles one slash-command POST. It always replies 200 with the reply text as
// Slack-formatted plain-text JSON (an "ephemeral" ack), because a non-200 or slow response makes
// Slack show the user a generic error regardless of what actually happened.
func (h *SlackHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, "reading body", http.StatusBadRequest)
		return
	}

	if err := VerifySignature(h.SigningSecret, r.Header.Get("X-Slack-Request-Timestamp"), r.Header.Get("X-Slack-Signature"), body, h.now()); err != nil {
		http.Error(w, err.Error(), http.StatusUnauthorized)
		return
	}

	form, err := url.ParseQuery(string(body))
	if err != nil {
		http.Error(w, "invalid form body", http.StatusBadRequest)
		return
	}

	userID := form.Get("user_id")
	if userID == "" {
		http.Error(w, "missing user_id", http.StatusBadRequest)
		return
	}
	text := strings.TrimSpace(form.Get("command") + " " + form.Get("text"))
	// Slack slash commands are registered per-verb ("/kage-approve", "/kage-reject") or as one
	// "/kage" command whose text carries the verb; either shape reduces to "<verb> <rest>" once the
	// leading slash and command name are stripped. Strip a leading "/kage" token if present so both
	// registrations work without two code paths.
	text = strings.TrimPrefix(strings.TrimSpace(text), "/kage")
	text = strings.TrimSpace(text)

	principal := approval.SlackPrincipal(userID)
	eventKey := r.Header.Get("X-Slack-Signature") // unique per request; see Dispatcher.Handle

	reply := h.Dispatcher.Handle(r.Context(), eventKey, principal, text)

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	fmt.Fprintf(w, `{"response_type":"ephemeral","text":%s}`, jsonString(reply))
}

func jsonString(s string) string {
	// Slash-command replies are short, operator-authored-or-templated text with no attacker-chosen
	// structure beyond what ParseCommand already rejected; a minimal escaper is enough and avoids
	// pulling in encoding/json for one field for a payload that is otherwise a literal.
	var b strings.Builder
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\n':
			b.WriteString(`\n`)
		default:
			if r < 0x20 {
				fmt.Fprintf(&b, `\u%04x`, r)
			} else {
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
	return b.String()
}
