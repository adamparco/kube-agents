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

import "testing"

func TestNormalizeAlertCloudMonitoring(t *testing.T) {
	raw := []byte(`{
		"incident": {
			"policy_name": "5xx-slo-burn",
			"summary": "High 5xx error rate on checkout",
			"state": "open",
			"severity": "Critical",
			"resource": {"namespace_name": "checkout"},
			"documentation": {"content": "Error budget burning fast"}
		}
	}`)
	ev, err := NormalizeAlert(raw)
	if err != nil {
		t.Fatalf("NormalizeAlert: %v", err)
	}
	if ev["kind"] != KindAlert {
		t.Errorf("kind = %v, want %s", ev["kind"], KindAlert)
	}
	if ev["policy"] != "5xx-slo-burn" {
		t.Errorf("policy = %v, want 5xx-slo-burn", ev["policy"])
	}
	if ev["summary"] != "High 5xx error rate on checkout" {
		t.Errorf("summary = %v", ev["summary"])
	}
	if ev["severity"] != "critical" { // lowercased
		t.Errorf("severity = %v, want critical", ev["severity"])
	}
	if ev["namespace"] != "checkout" {
		t.Errorf("namespace = %v, want checkout", ev["namespace"])
	}
	if err := ev.validate(); err != nil {
		t.Errorf("normalized alert failed validate: %v", err)
	}
}

func TestNormalizeAlertThinPayloadDegrades(t *testing.T) {
	// An alert with almost nothing still normalizes to a valid {kind:alert} — worth waking the agent.
	ev, err := NormalizeAlert([]byte(`{"incident": {}}`))
	if err != nil {
		t.Fatalf("NormalizeAlert: %v", err)
	}
	if ev["kind"] != KindAlert || ev["policy"] != "unknown-policy" || ev["summary"] != "Alert" {
		t.Errorf("thin alert did not degrade to defaults: %#v", ev)
	}
	if _, ok := ev["namespace"]; ok {
		t.Errorf("thin alert should carry no namespace, got %v", ev["namespace"])
	}
}

func TestNormalizeGitHubIssue(t *testing.T) {
	raw := []byte(`{
		"action": "opened",
		"repository": {"full_name": "acme/infra"},
		"issue": {"number": 42, "title": "Bump memory limits", "html_url": "https://github.com/acme/infra/issues/42"}
	}`)
	ev, err := NormalizeGitHub("issues", raw)
	if err != nil {
		t.Fatalf("NormalizeGitHub: %v", err)
	}
	if ev["kind"] != KindGitHub {
		t.Errorf("kind = %v, want %s", ev["kind"], KindGitHub)
	}
	if ev["action"] != "opened" || ev["repo"] != "acme/infra" || ev["title"] != "Bump memory limits" {
		t.Errorf("github issue fields wrong: %#v", ev)
	}
	if ev["number"] != 42 {
		t.Errorf("number = %v, want 42", ev["number"])
	}
	if ev["event_type"] != "issues" {
		t.Errorf("event_type = %v, want issues", ev["event_type"])
	}
}

func TestNormalizeGitHubPullRequestFallback(t *testing.T) {
	// No issue block; number/title come from pull_request.
	raw := []byte(`{
		"action": "synchronize",
		"repository": {"full_name": "acme/infra"},
		"pull_request": {"number": 7, "title": "Fix drift"}
	}`)
	ev, err := NormalizeGitHub("pull_request", raw)
	if err != nil {
		t.Fatalf("NormalizeGitHub: %v", err)
	}
	if ev["number"] != 7 || ev["title"] != "Fix drift" {
		t.Errorf("PR fallback fields wrong: %#v", ev)
	}
}

func TestParseSyntheticEventRoundTrip(t *testing.T) {
	// The Kind terminus: an already-normalized event passes straight through.
	ev, err := ParseSyntheticEvent([]byte(`{"kind":"alert","summary":"synthetic burn","policy":"p1"}`))
	if err != nil {
		t.Fatalf("ParseSyntheticEvent: %v", err)
	}
	if ev["kind"] != KindAlert || ev["summary"] != "synthetic burn" {
		t.Errorf("synthetic event wrong: %#v", ev)
	}
}

func TestParseSyntheticEventRejectsBadKind(t *testing.T) {
	if _, err := ParseSyntheticEvent([]byte(`{"summary":"no kind"}`)); err == nil {
		t.Error("expected error for synthetic event with no kind")
	}
	if _, err := ParseSyntheticEvent([]byte(`{"kind":"pagerduty"}`)); err == nil {
		t.Error("expected error for synthetic event with unknown kind")
	}
	if _, err := ParseSyntheticEvent([]byte(`not json`)); err == nil {
		t.Error("expected error for non-JSON synthetic event")
	}
}

func TestValidateKnownKinds(t *testing.T) {
	for _, k := range []string{KindK8sEvent, KindAlert, KindGitHub, KindEscalation} {
		if err := (NormalizedEvent{"kind": k}).validate(); err != nil {
			t.Errorf("kind %q should validate: %v", k, err)
		}
	}
	if err := (NormalizedEvent{"kind": "  "}).validate(); err == nil {
		t.Error("blank kind should not validate")
	}
}
