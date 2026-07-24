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
	"encoding/json"
	"fmt"
	"strings"
)

// Kind constants mirror the seam's _KNOWN_INJECT_KINDS discriminator (S2, session_kv_server.py). They
// are duplicated here (the seam is Python) and pinned by the round-trip test so a rename on either side
// is caught.
const (
	KindK8sEvent   = "k8s-event"
	KindAlert      = "alert"
	KindGitHub     = "github"
	KindEscalation = "escalation"
)

// knownKinds is the accept-set for validate(). A source that produces any other kind is a bug — the
// seam would 400 it, so we fail early and locally with a clearer error.
var knownKinds = map[string]struct{}{
	KindK8sEvent:   {},
	KindAlert:      {},
	KindGitHub:     {},
	KindEscalation: {},
}

// NormalizedEvent is the inner inject payload the seam json.loads() and routes on "kind" (S2). It is a
// free-form map (not a struct) because each kind has a different field set and the seam reads fields
// defensively with .get(); a map keeps eventingress from having to model every field the seam might
// grow. The only hard requirement is a recognized "kind".
type NormalizedEvent map[string]any

// validate rejects an event with no/unknown kind before it reaches the wire. This is a local
// fail-closed mirror of the seam's server-side check (S2) — it turns a would-be 400 into an error the
// source can log against the offending message.
func (e NormalizedEvent) validate() error {
	kind, _ := e["kind"].(string)
	kind = strings.TrimSpace(kind)
	if kind == "" {
		return fmt.Errorf("eventingress: event has no kind (expected one of alert, github, escalation, k8s-event)")
	}
	if _, ok := knownKinds[kind]; !ok {
		return fmt.Errorf("eventingress: unknown event kind %q", kind)
	}
	return nil
}

// cloudMonitoringPayload is the subset of Google Cloud Monitoring's notification webhook we read. The
// real payload nests everything under "incident"; fields we don't map are ignored (the seam reads
// defensively). See https://cloud.google.com/monitoring/support/notification-options#webhooks.
type cloudMonitoringPayload struct {
	Incident struct {
		PolicyName    string `json:"policy_name"`
		Summary       string `json:"summary"`
		State         string `json:"state"`
		ResourceName  string `json:"resource_name"`
		Severity      string `json:"severity"`
		Documentation struct {
			Content string `json:"content"`
		} `json:"documentation"`
		Metric struct {
			DisplayName string `json:"displayName"`
		} `json:"metric"`
		ResourceTypeDisplayName string            `json:"resource_type_display_name"`
		Labels                  map[string]string `json:"resource"`
	} `json:"incident"`
}

// NormalizeAlert converts a Cloud Monitoring notification webhook (the alert transport of 04 §4) into an
// {kind:alert} event. Namespace, when present in the incident's resource labels, is carried so the seam
// can scope the alert card. Unknown/absent fields degrade to sensible defaults rather than erroring —
// an alert with a thin payload is still worth waking the agent for.
func NormalizeAlert(raw []byte) (NormalizedEvent, error) {
	var p cloudMonitoringPayload
	if err := json.Unmarshal(raw, &p); err != nil {
		return nil, fmt.Errorf("eventingress: parse cloud-monitoring alert: %w", err)
	}
	inc := p.Incident
	summary := firstNonEmpty(inc.Summary, inc.Metric.DisplayName, "Alert")
	ev := NormalizedEvent{
		"kind":     KindAlert,
		"summary":  summary,
		"policy":   firstNonEmpty(inc.PolicyName, "unknown-policy"),
		"severity": strings.ToLower(firstNonEmpty(inc.Severity, inc.State, "warning")),
		"message":  firstNonEmpty(inc.Documentation.Content, inc.ResourceName),
	}
	if ns := inc.Labels["namespace_name"]; ns != "" {
		ev["namespace"] = ns
	}
	return ev, nil
}

// githubPayload is the subset of a GitHub webhook body common to issues and pull_request events.
type githubPayload struct {
	Action     string `json:"action"`
	Repository struct {
		FullName string `json:"full_name"`
	} `json:"repository"`
	Issue struct {
		Number  int    `json:"number"`
		Title   string `json:"title"`
		HTMLURL string `json:"html_url"`
	} `json:"issue"`
	PullRequest struct {
		Number  int    `json:"number"`
		Title   string `json:"title"`
		HTMLURL string `json:"html_url"`
	} `json:"pull_request"`
}

// NormalizeGitHub converts a GitHub webhook body into a {kind:github} event. eventType is the
// X-GitHub-Event header value (e.g. "issues", "pull_request"); it is carried through so the agent can
// distinguish issue from PR activity. Number/title are taken from whichever of issue/pull_request the
// payload carries.
func NormalizeGitHub(eventType string, raw []byte) (NormalizedEvent, error) {
	var p githubPayload
	if err := json.Unmarshal(raw, &p); err != nil {
		return nil, fmt.Errorf("eventingress: parse github webhook: %w", err)
	}
	number := p.Issue.Number
	title := p.Issue.Title
	if number == 0 && p.PullRequest.Number != 0 {
		number = p.PullRequest.Number
		title = p.PullRequest.Title
	}
	ev := NormalizedEvent{
		"kind":   KindGitHub,
		"action": firstNonEmpty(p.Action, eventType, "event"),
		"repo":   firstNonEmpty(p.Repository.FullName, "(unknown repo)"),
		"title":  title,
	}
	if eventType != "" {
		ev["event_type"] = eventType
	}
	if number != 0 {
		ev["number"] = number
	}
	return ev, nil
}

// ParseSyntheticEvent reads a pre-normalized event straight from JSON — the Kind terminus (D1). On Kind
// there is no Pub/Sub, so the synthetic source feeds an already-{kind:...} document directly through the
// SAME relay the cloud sources use, exercising the real delivery path (not a fake). The document must
// already carry a recognized kind; anything else is rejected here rather than at the seam.
func ParseSyntheticEvent(raw []byte) (NormalizedEvent, error) {
	var ev NormalizedEvent
	if err := json.Unmarshal(raw, &ev); err != nil {
		return nil, fmt.Errorf("eventingress: parse synthetic event: %w", err)
	}
	if err := ev.validate(); err != nil {
		return nil, err
	}
	return ev, nil
}

// firstNonEmpty returns the first non-empty (after trimming) string among its args, or "" if all empty.
func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if strings.TrimSpace(v) != "" {
			return v
		}
	}
	return ""
}
