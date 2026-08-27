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

package notify

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval"
)

// SlackDeliverer posts and edits messages through the Slack Web API using the gateway's own bot
// token (chat-approval.md §3: a dedicated app, its token held only by this pod). BaseURL and
// HTTPClient are overridable so a test can point this at an httptest.Server instead of
// slack.com — there is no live Slack workspace to test against in this repository's CI.
type SlackDeliverer struct {
	// Token is the bot token, "xoxb-...". Required.
	Token string
	// BaseURL defaults to https://slack.com/api if empty.
	BaseURL string
	// HTTPClient defaults to http.DefaultClient if nil.
	HTTPClient *http.Client
}

func (s *SlackDeliverer) baseURL() string {
	if s.BaseURL != "" {
		return s.BaseURL
	}
	return "https://slack.com/api"
}

func (s *SlackDeliverer) httpClient() *http.Client {
	if s.HTTPClient != nil {
		return s.HTTPClient
	}
	return http.DefaultClient
}

type slackResponse struct {
	OK    bool   `json:"ok"`
	Error string `json:"error"`
	TS    string `json:"ts"`
}

func (s *SlackDeliverer) call(ctx context.Context, method string, body map[string]any) (slackResponse, error) {
	raw, err := json.Marshal(body)
	if err != nil {
		return slackResponse{}, fmt.Errorf("notify: marshaling slack %s request: %w", method, err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.baseURL()+"/"+method, bytes.NewReader(raw))
	if err != nil {
		return slackResponse{}, fmt.Errorf("notify: building slack %s request: %w", method, err)
	}
	req.Header.Set("Content-Type", "application/json; charset=utf-8")
	req.Header.Set("Authorization", "Bearer "+s.Token)

	resp, err := s.httpClient().Do(req)
	if err != nil {
		return slackResponse{}, fmt.Errorf("notify: calling slack %s: %w", method, err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return slackResponse{}, fmt.Errorf("notify: reading slack %s response: %w", method, err)
	}
	// Slack's Web API returns HTTP 200 for application-level errors too (ok:false, error:"...");
	// a non-2xx status is a transport/gateway problem the ok field would not otherwise catch.
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return slackResponse{}, fmt.Errorf("notify: slack %s: unexpected status %d: %s", method, resp.StatusCode, string(respBody))
	}
	var out slackResponse
	if err := json.Unmarshal(respBody, &out); err != nil {
		return slackResponse{}, fmt.Errorf("notify: decoding slack %s response: %w", method, err)
	}
	if !out.OK {
		return slackResponse{}, fmt.Errorf("notify: slack %s refused: %s", method, out.Error)
	}
	return out, nil
}

// Deliver posts a new message via chat.postMessage and returns its "ts" as the ref future Update
// calls need.
func (s *SlackDeliverer) Deliver(ctx context.Context, target Target, message approval.Message) (string, error) {
	payload := SlackBlocks(message)
	payload["channel"] = target.Channel
	out, err := s.call(ctx, "chat.postMessage", payload)
	if err != nil {
		return "", err
	}
	return out.TS, nil
}

// Update edits an existing message via chat.update, addressed by channel and the ts Deliver
// returned.
func (s *SlackDeliverer) Update(ctx context.Context, target Target, ref string, message approval.Message) error {
	payload := SlackBlocks(message)
	payload["channel"] = target.Channel
	payload["ts"] = ref
	_, err := s.call(ctx, "chat.update", payload)
	return err
}

var _ Deliverer = (*SlackDeliverer)(nil)
