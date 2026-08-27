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

import "net/http"

// NewServeMux wires the two platform ingress routes. Each is a distinct path, and both run
// against dedicated apps (chat-approval.md §3) — nothing about the routing shares state between
// them beyond the one Dispatcher, which is exactly the point: one authorization path regardless of
// which platform a command arrived on.
//
// No /healthz or /readyz here on purpose: cmd/chatops-gateway wires those to the controller-runtime
// manager's own health server on a separate port, and its /readyz is leadership-gated (chat-
// approval.md §7's one-socket rule; a rolling update must not route webhook traffic to a pod that
// has not yet won the lease). A second, bare-200 readyz on this mux would contradict that one.
func NewServeMux(slack *SlackHandler, googleChat *GoogleChatHandler) *http.ServeMux {
	mux := http.NewServeMux()
	if slack != nil {
		mux.Handle("/slack/commands", slack)
	}
	if googleChat != nil {
		mux.Handle("/googlechat/events", googleChat)
	}
	return mux
}
