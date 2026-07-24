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
	"errors"
	"testing"
)

func TestParseChatEvent(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name       string
		raw        string
		wantText   string
		wantSender string
		wantSpace  string
		wantErr    error
	}{
		{
			name:       "message event",
			raw:        `{"type":"MESSAGE","message":{"text":"@kage /cluster-cluster-a status","sender":{"name":"users/alice","type":"HUMAN"}},"space":{"name":"spaces/AAA"}}`,
			wantText:   "@kage /cluster-cluster-a status",
			wantSender: "users/alice",
			wantSpace:  "spaces/AAA",
		},
		{
			name:       "no type is treated as message",
			raw:        `{"message":{"text":"hi","sender":{"name":"users/bob"}}}`,
			wantText:   "hi",
			wantSender: "users/bob",
		},
		{
			name:    "added-to-space is skipped",
			raw:     `{"type":"ADDED_TO_SPACE","space":{"name":"spaces/AAA"}}`,
			wantErr: ErrNotAMessageEvent,
		},
		{
			name:    "malformed json is a poison message",
			raw:     `{"type":"MESSAGE","message":`,
			wantErr: errParseSentinelForTest, // any non-nil, non-ErrNotAMessageEvent error
		},
		{
			// sender omitted → empty Sender, which Authorize refuses fail-closed. The parser never invents one.
			name:     "message without sender yields empty sender",
			raw:      `{"type":"MESSAGE","message":{"text":"hello"}}`,
			wantText: "hello",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ev, err := ParseChatEvent([]byte(tc.raw))
			switch {
			case tc.wantErr == ErrNotAMessageEvent:
				if !errors.Is(err, ErrNotAMessageEvent) {
					t.Fatalf("err = %v, want ErrNotAMessageEvent", err)
				}
			case tc.wantErr == errParseSentinelForTest:
				if err == nil || errors.Is(err, ErrNotAMessageEvent) {
					t.Fatalf("err = %v, want a parse error", err)
				}
			default:
				if err != nil {
					t.Fatalf("unexpected err: %v", err)
				}
				if ev.Text != tc.wantText || ev.Sender != tc.wantSender || ev.Space != tc.wantSpace {
					t.Errorf("event = %+v, want text=%q sender=%q space=%q", ev, tc.wantText, tc.wantSender, tc.wantSpace)
				}
			}
		})
	}
}

// errParseSentinelForTest is a table marker for "expect any JSON parse error" (not a real sentinel).
var errParseSentinelForTest = errors.New("test: want parse error")

func TestIsDeterministicRefusal(t *testing.T) {
	t.Parallel()
	// Every router refusal sentinel must classify as deterministic (Ack, don't retry).
	refusals := []error{
		ErrUnaddressed,
		ErrMalformedHandle,
		ErrUnknownTier,
		ErrInferenceUnavailable,
		ErrMissingProjectContext,
		ErrClarify,
		ErrNoSuchTarget,
		ErrUnauthorized,
	}
	for _, e := range refusals {
		if !IsDeterministicRefusal(e) {
			t.Errorf("IsDeterministicRefusal(%v) = false, want true", e)
		}
		// Wrapped forms must classify identically (the gateway may wrap).
		if !IsDeterministicRefusal(errors.Join(errors.New("ctx"), e)) {
			t.Errorf("IsDeterministicRefusal(wrapped %v) = false, want true", e)
		}
	}

	// A concrete *ClarifyError (what the gateway actually returns on ambiguity) must also classify as a
	// deterministic refusal via its Is(ErrClarify) — the delivery layer Acks it, and errors.As recovers
	// the candidate menu for the reply.
	ce := &ClarifyError{Reason: "ambiguous", Candidates: []Candidate{{Handle: Handle{Tier: "developer-team", Leaf: "team-x"}, Confidence: 1}}}
	if !IsDeterministicRefusal(ce) {
		t.Error("IsDeterministicRefusal(*ClarifyError) = false, want true")
	}
	var got *ClarifyError
	if !errors.As(error(ce), &got) || len(got.Candidates) != 1 {
		t.Errorf("errors.As(*ClarifyError) failed to recover the candidate menu: %+v", got)
	}

	// A transient dispatch/publish failure must NOT be deterministic (Nack, retry), nor must nil.
	if IsDeterministicRefusal(errors.New("pubsubdispatch: publish to topic: rpc error")) {
		t.Error("a transient dispatch error was classified as a deterministic refusal (would drop on the floor)")
	}
	if IsDeterministicRefusal(nil) {
		t.Error("IsDeterministicRefusal(nil) = true, want false")
	}
}
