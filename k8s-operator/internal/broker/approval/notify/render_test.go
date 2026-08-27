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

package notify_test

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval/notify"
)

func askMessage() approval.Message {
	return approval.Message{
		RecordName:  "ar-01arz3ndektsv4rrffq69g5fav",
		Intent:      "scale web to 5",
		Class:       "gated",
		Reasons:     []string{"production-environment (+1): env=prod"},
		Targets:     []string{"apps/v1/Deployment default/web"},
		RequesterID: "slack:U01",
		Required:    2,
		Granted:     []string{"slack:U02"},
	}
}

func TestSlackBlocksAskShapeIsValidJSON(t *testing.T) {
	payload := notify.SlackBlocks(askMessage())
	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("blocks payload must be JSON-serializable: %v", err)
	}
	s := string(raw)
	for _, want := range []string{"scale web to 5", "ar-01arz3ndektsv4rrffq69g5fav", "kage_approve", "kage_reject"} {
		if !strings.Contains(s, want) {
			t.Errorf("blocks payload missing %q:\n%s", want, s)
		}
	}
	if _, ok := payload["text"].(string); !ok || payload["text"] == "" {
		t.Error("blocks payload must carry a non-empty fallback \"text\" for accessibility")
	}
}

func TestSlackBlocksResolvedMessageHasNoButtons(t *testing.T) {
	m := askMessage()
	m.Resolution = "approved"
	payload := notify.SlackBlocks(m)
	raw, _ := json.Marshal(payload)
	if strings.Contains(string(raw), "kage_approve") {
		t.Error("a resolved message must not still offer an approve button")
	}
}

func TestGoogleChatCardAskShapeIsValidJSON(t *testing.T) {
	payload := notify.GoogleChatCard(askMessage())
	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("card payload must be JSON-serializable: %v", err)
	}
	s := string(raw)
	for _, want := range []string{"scale web to 5", "ar-01arz3ndektsv4rrffq69g5fav"} {
		if !strings.Contains(s, want) {
			t.Errorf("card payload missing %q:\n%s", want, s)
		}
	}
}

func TestGoogleChatCardResolvedMessageIsPlainText(t *testing.T) {
	m := askMessage()
	m.Resolution = "rejected"
	payload := notify.GoogleChatCard(m)
	if _, hasCard := payload["cardsV2"]; hasCard {
		t.Error("a resolved googlechat message should be a plain text edit, not a fresh card")
	}
}
