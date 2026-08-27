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

package approval_test

import (
	"testing"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval"
)

func TestParseCommandApprove(t *testing.T) {
	cmd, err := approval.ParseCommand("approve ar-01arz3ndektsv4rrffq69g5fav")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cmd.Verb != approval.VerbApprove {
		t.Errorf("verb = %q, want approve", cmd.Verb)
	}
	if cmd.ActionID != "ar-01arz3ndektsv4rrffq69g5fav" {
		t.Errorf("actionID = %q", cmd.ActionID)
	}
}

func TestParseCommandApproveRejectsAReason(t *testing.T) {
	if _, err := approval.ParseCommand("approve ar-x extra words"); err == nil {
		t.Error("expected an error; approve takes no reason")
	}
}

func TestParseCommandReject(t *testing.T) {
	cmd, err := approval.ParseCommand("reject ar-x too risky right now")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cmd.Verb != approval.VerbReject {
		t.Errorf("verb = %q, want reject", cmd.Verb)
	}
	if cmd.Reason != "too risky right now" {
		t.Errorf("reason = %q", cmd.Reason)
	}
}

func TestParseCommandRejectWithNoReason(t *testing.T) {
	cmd, err := approval.ParseCommand("reject ar-x")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cmd.Reason != "" {
		t.Errorf("reason = %q, want empty", cmd.Reason)
	}
}

func TestParseCommandRejectsV2Verbs(t *testing.T) {
	for _, text := range []string{"resume my-agent", "uncontest ar-x"} {
		if _, err := approval.ParseCommand(text); err == nil {
			t.Errorf("ParseCommand(%q): expected an error; v1 does not support v2 verbs", text)
		}
	}
}

func TestParseCommandRejectsUnrecognizedVerb(t *testing.T) {
	if _, err := approval.ParseCommand("delete ar-x"); err == nil {
		t.Error("expected an error for an unrecognized verb")
	}
}

func TestParseCommandRejectsEmptyAndOneWord(t *testing.T) {
	for _, text := range []string{"", "approve", "   "} {
		if _, err := approval.ParseCommand(text); err == nil {
			t.Errorf("ParseCommand(%q): expected an error", text)
		}
	}
}

func TestActionRecordName(t *testing.T) {
	cases := map[string]string{
		"01ARZ3NDEKTSV4RRFFQ69G5FAV":    "ar-01arz3ndektsv4rrffq69g5fav",
		"ar-01arz3ndektsv4rrffq69g5fav": "ar-01arz3ndektsv4rrffq69g5fav",
		"AR-01ARZ3NDEKTSV4RRFFQ69G5FAV": "ar-01arz3ndektsv4rrffq69g5fav",
	}
	for in, want := range cases {
		if got := approval.ActionRecordName(in); got != want {
			t.Errorf("ActionRecordName(%q) = %q, want %q", in, got, want)
		}
	}
}
