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
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval/gateway"
)

const testSigningSecret = "8f742231b10e8888abcd99yyyzzz85a5"

func signSlackRequest(secret, timestamp string, body []byte) string {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte("v0:" + timestamp + ":"))
	mac.Write(body)
	return "v0=" + hex.EncodeToString(mac.Sum(nil))
}

func TestVerifySignatureAccepts(t *testing.T) {
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	ts := strconv.FormatInt(now.Unix(), 10)
	body := []byte("token=x&command=%2Fkage&text=approve+ar-1&user_id=U01")
	sig := signSlackRequest(testSigningSecret, ts, body)

	if err := gateway.VerifySignature(testSigningSecret, ts, sig, body, now); err != nil {
		t.Fatalf("expected a valid signature to verify, got: %v", err)
	}
}

func TestVerifySignatureRejectsWrongSecret(t *testing.T) {
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	ts := strconv.FormatInt(now.Unix(), 10)
	body := []byte("text=approve+ar-1")
	sig := signSlackRequest("a-different-secret", ts, body)

	if err := gateway.VerifySignature(testSigningSecret, ts, sig, body, now); err == nil {
		t.Fatal("expected verification to fail with the wrong secret")
	}
}

func TestVerifySignatureRejectsTamperedBody(t *testing.T) {
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	ts := strconv.FormatInt(now.Unix(), 10)
	sig := signSlackRequest(testSigningSecret, ts, []byte("text=approve+ar-1"))

	if err := gateway.VerifySignature(testSigningSecret, ts, sig, []byte("text=approve+ar-EVIL"), now); err == nil {
		t.Fatal("expected verification to fail against a body different from the one signed")
	}
}

func TestVerifySignatureRejectsStaleTimestamp(t *testing.T) {
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	old := now.Add(-10 * time.Minute)
	ts := strconv.FormatInt(old.Unix(), 10)
	body := []byte("text=approve+ar-1")
	sig := signSlackRequest(testSigningSecret, ts, body)

	if err := gateway.VerifySignature(testSigningSecret, ts, sig, body, now); err == nil {
		t.Fatal("expected a signature outside the replay window to be refused")
	}
}

func TestVerifySignatureRejectsMalformedTimestamp(t *testing.T) {
	if err := gateway.VerifySignature(testSigningSecret, "not-a-number", "v0=whatever", []byte("x"), time.Now()); err == nil {
		t.Fatal("expected an error for a non-numeric timestamp")
	}
}

func newDispatcherWithClient() *gateway.Dispatcher {
	return &gateway.Dispatcher{Client: fakeClientNoObjects(), Now: func() time.Time { return fixedNow }}
}

func TestSlackHandlerRejectsUnsignedRequest(t *testing.T) {
	h := &gateway.SlackHandler{Dispatcher: newDispatcherWithClient(), SigningSecret: testSigningSecret, Now: func() time.Time { return fixedNow }}

	body := "user_id=U01&text=approve+ar-1"
	req := httptest.NewRequest(http.MethodPost, "/slack/commands", strings.NewReader(body))
	req.Header.Set("X-Slack-Request-Timestamp", strconv.FormatInt(fixedNow.Unix(), 10))
	req.Header.Set("X-Slack-Signature", "v0=deadbeef")
	w := httptest.NewRecorder()

	h.ServeHTTP(w, req)
	if w.Code != http.StatusUnauthorized {
		t.Errorf("status = %d, want 401 for a bad signature", w.Code)
	}
}

func TestSlackHandlerAcceptsSignedRequestAndDispatches(t *testing.T) {
	h := &gateway.SlackHandler{Dispatcher: newDispatcherWithClient(), SigningSecret: testSigningSecret, Now: func() time.Time { return fixedNow }}

	form := url.Values{}
	form.Set("user_id", "U01")
	form.Set("command", "/kage")
	form.Set("text", "approve ar-nonexistent")
	body := form.Encode()

	ts := strconv.FormatInt(fixedNow.Unix(), 10)
	sig := signSlackRequest(testSigningSecret, ts, []byte(body))

	req := httptest.NewRequest(http.MethodPost, "/slack/commands", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("X-Slack-Request-Timestamp", ts)
	req.Header.Set("X-Slack-Signature", sig)
	w := httptest.NewRecorder()

	h.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (slack always gets a 200 with the outcome in the body)", w.Code)
	}
	if !strings.Contains(w.Body.String(), "no such action") {
		t.Errorf("body = %q, want a reply naming the missing action", w.Body.String())
	}
}

func TestSlackHandlerRejectsMissingUserID(t *testing.T) {
	h := &gateway.SlackHandler{Dispatcher: newDispatcherWithClient(), SigningSecret: testSigningSecret, Now: func() time.Time { return fixedNow }}

	body := "text=approve+ar-1" // no user_id
	ts := strconv.FormatInt(fixedNow.Unix(), 10)
	sig := signSlackRequest(testSigningSecret, ts, []byte(body))

	req := httptest.NewRequest(http.MethodPost, "/slack/commands", strings.NewReader(body))
	req.Header.Set("X-Slack-Request-Timestamp", ts)
	req.Header.Set("X-Slack-Signature", sig)
	w := httptest.NewRecorder()

	h.ServeHTTP(w, req)
	if w.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400 for a missing user_id", w.Code)
	}
}
