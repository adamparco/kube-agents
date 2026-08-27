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
	"net/url"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval"
)

// GoogleChatDeliverer posts and edits messages through the Google Chat REST API
// (spaces.messages.create/patch) using the gateway's own dedicated Chat app credentials
// (chat-approval.md §3). TokenSource is a func rather than a static string so a production caller
// can refresh an OAuth2 access token per call; BaseURL/HTTPClient are overridable for tests, the
// same seam SlackDeliverer uses.
type GoogleChatDeliverer struct {
	// TokenSource returns a bearer access token for the Authorization header. Required.
	TokenSource func(ctx context.Context) (string, error)
	// BaseURL defaults to https://chat.googleapis.com/v1 if empty.
	BaseURL string
	// HTTPClient defaults to http.DefaultClient if nil.
	HTTPClient *http.Client
}

func (g *GoogleChatDeliverer) baseURL() string {
	if g.BaseURL != "" {
		return g.BaseURL
	}
	return "https://chat.googleapis.com/v1"
}

func (g *GoogleChatDeliverer) httpClient() *http.Client {
	if g.HTTPClient != nil {
		return g.HTTPClient
	}
	return http.DefaultClient
}

type googleChatMessage struct {
	Name string `json:"name"`
}

func (g *GoogleChatDeliverer) do(ctx context.Context, method, path string, body map[string]any) (googleChatMessage, error) {
	if g.TokenSource == nil {
		return googleChatMessage{}, fmt.Errorf("notify: googlechat deliverer has no TokenSource configured")
	}
	token, err := g.TokenSource(ctx)
	if err != nil {
		return googleChatMessage{}, fmt.Errorf("notify: obtaining googlechat token: %w", err)
	}

	raw, err := json.Marshal(body)
	if err != nil {
		return googleChatMessage{}, fmt.Errorf("notify: marshaling googlechat request: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, method, g.baseURL()+path, bytes.NewReader(raw))
	if err != nil {
		return googleChatMessage{}, fmt.Errorf("notify: building googlechat request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json; charset=utf-8")
	req.Header.Set("Authorization", "Bearer "+token)

	resp, err := g.httpClient().Do(req)
	if err != nil {
		return googleChatMessage{}, fmt.Errorf("notify: calling googlechat %s %s: %w", method, path, err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return googleChatMessage{}, fmt.Errorf("notify: reading googlechat response: %w", err)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return googleChatMessage{}, fmt.Errorf("notify: googlechat %s %s: unexpected status %d: %s", method, path, resp.StatusCode, string(respBody))
	}
	var out googleChatMessage
	if err := json.Unmarshal(respBody, &out); err != nil {
		return googleChatMessage{}, fmt.Errorf("notify: decoding googlechat response: %w", err)
	}
	return out, nil
}

// Deliver posts a new message under the target space and returns the message's resource name
// ("spaces/.../messages/...") as the ref future Update calls need.
func (g *GoogleChatDeliverer) Deliver(ctx context.Context, target Target, message approval.Message) (string, error) {
	path := fmt.Sprintf("/%s/messages", target.Channel)
	out, err := g.do(ctx, http.MethodPost, path, GoogleChatCard(message))
	if err != nil {
		return "", err
	}
	return out.Name, nil
}

// Update edits an existing message via messages.patch, addressed by its resource name.
//
// updateMask names cardsV2 and text explicitly rather than "*" — Google Chat's patch semantics
// replace only the named fields, and an explicit mask is what makes a resolved-message edit
// idempotent under a retried call: a second identical PATCH changes nothing it did not already
// change once.
func (g *GoogleChatDeliverer) Update(ctx context.Context, target Target, ref string, message approval.Message) error {
	path := fmt.Sprintf("/%s?updateMask=%s", ref, url.QueryEscape("cardsV2,text"))
	_, err := g.do(ctx, http.MethodPatch, path, GoogleChatCard(message))
	return err
}

var _ Deliverer = (*GoogleChatDeliverer)(nil)
