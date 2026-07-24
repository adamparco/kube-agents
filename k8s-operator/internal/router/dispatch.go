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
	"context"
	"sync"
)

// Message is one inbound chat turn the gateway routes. Text and Sender are the fields the deterministic
// path reads — Text for resolution (Resolver), Sender for the before-dispatch allowlist (Authorize).
// Raw carries the original event payload so the dispatcher can re-publish it verbatim to the target's
// topic (the target pod's proxy expects the platform's native event shape); it falls back to Text.
type Message struct {
	// Text is the message body used for slash/@handle resolution.
	Text string
	// Sender is the platform user id (e.g. "users/123") checked against the target allowlist. Never a
	// routing signal — only an authz one.
	Sender string
	// Raw is the original event payload to re-publish unchanged; if empty the dispatcher publishes Text.
	Raw []byte
	// Attrs are original message attributes to preserve on re-publish (best-effort passthrough).
	Attrs map[string]string
}

// Dispatcher delivers an authorized message to a resolved Target. It is called ONLY after Authorize has
// allowed the turn (the gateway guarantees this ordering, 06 §2b / 03 §4a). Implementations must not
// perform any access decision of their own — authorization is complete before they are invoked.
type Dispatcher interface {
	Dispatch(ctx context.Context, target Target, msg Message) error
}

// FakeDispatcher records every dispatch instead of sending it. It is the test double used by the
// gateway unit tests and by any hermetic path that must assert "was this delivered, and to whom" without
// a Pub/Sub backend. Safe for concurrent use.
type FakeDispatcher struct {
	mu   sync.Mutex
	sent []Dispatched
	// Err, if set, is returned by every Dispatch (to exercise the gateway's dispatch-failure path).
	Err error
}

// Dispatched is a captured delivery: enough to assert the message reached the right target's topic.
type Dispatched struct {
	Identity  string
	TopicName string
	Handle    string
	Sender    string
	Text      string
}

// Dispatch records the delivery (or returns the preset error).
func (f *FakeDispatcher) Dispatch(_ context.Context, target Target, msg Message) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.Err != nil {
		return f.Err
	}
	f.sent = append(f.sent, Dispatched{
		Identity:  target.Identity,
		TopicName: target.TopicName,
		Handle:    target.Handle,
		Sender:    msg.Sender,
		Text:      msg.Text,
	})
	return nil
}

// Sent returns a copy of the recorded deliveries.
func (f *FakeDispatcher) Sent() []Dispatched {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]Dispatched, len(f.sent))
	copy(out, f.sent)
	return out
}
