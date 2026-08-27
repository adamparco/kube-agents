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
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval/gateway"
)

func TestNewServeMuxRoutesOnlyTheConfiguredPlatforms(t *testing.T) {
	slack := &gateway.SlackHandler{Dispatcher: &gateway.Dispatcher{Client: fakeClientNoObjects()}, SigningSecret: "s"}
	gchat := &gateway.GoogleChatHandler{Dispatcher: &gateway.Dispatcher{Client: fakeClientNoObjects()}, Verifier: gateway.SharedSecretVerifier{Token: "t"}}

	post := func(mux http.Handler, path string) int {
		req := httptest.NewRequest(http.MethodPost, path, nil)
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, req)
		return rec.Code
	}

	t.Run("both platforms configured", func(t *testing.T) {
		mux := gateway.NewServeMux(slack, gchat)
		// Neither handler is given a well-formed request, so each responds Unauthorized -- the
		// point is that the request reached the handler at all, rather than the mux 404ing it.
		if code := post(mux, "/slack/commands"); code == http.StatusNotFound {
			t.Errorf("/slack/commands 404'd; want it routed to the slack handler")
		}
		if code := post(mux, "/googlechat/events"); code == http.StatusNotFound {
			t.Errorf("/googlechat/events 404'd; want it routed to the googlechat handler")
		}
		if code := post(mux, "/nonexistent"); code != http.StatusNotFound {
			t.Errorf("unregistered path = %d, want 404", code)
		}
	})

	t.Run("slack only", func(t *testing.T) {
		mux := gateway.NewServeMux(slack, nil)
		if code := post(mux, "/slack/commands"); code == http.StatusNotFound {
			t.Errorf("/slack/commands 404'd; want it routed to the slack handler")
		}
		if code := post(mux, "/googlechat/events"); code != http.StatusNotFound {
			t.Errorf("/googlechat/events = %d, want 404 with no googlechat handler configured", code)
		}
	})

	t.Run("googlechat only", func(t *testing.T) {
		mux := gateway.NewServeMux(nil, gchat)
		if code := post(mux, "/googlechat/events"); code == http.StatusNotFound {
			t.Errorf("/googlechat/events 404'd; want it routed to the googlechat handler")
		}
		if code := post(mux, "/slack/commands"); code != http.StatusNotFound {
			t.Errorf("/slack/commands = %d, want 404 with no slack handler configured", code)
		}
	})

	t.Run("neither platform configured", func(t *testing.T) {
		mux := gateway.NewServeMux(nil, nil)
		if code := post(mux, "/slack/commands"); code != http.StatusNotFound {
			t.Errorf("/slack/commands = %d, want 404", code)
		}
		if code := post(mux, "/googlechat/events"); code != http.StatusNotFound {
			t.Errorf("/googlechat/events = %d, want 404", code)
		}
	})
}
