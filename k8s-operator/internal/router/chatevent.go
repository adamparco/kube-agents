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
	"encoding/json"
	"errors"
	"fmt"
)

// ChatEvent is the routing-relevant projection of a Google Chat event as delivered on the inbound
// Pub/Sub topic (the Chat app's events are published as JSON). The router needs only two fields to route
// a turn — the message text (to resolve a handle) and the sender's stable id (to authorize) — plus the
// space for audit correlation. Everything else in the event is intentionally ignored: the raw bytes are
// carried through to dispatch unchanged, so nothing is lost by projecting narrowly here.
type ChatEvent struct {
	// Type is the Chat event type (e.g. MESSAGE, ADDED_TO_SPACE). Only MESSAGE events carry a turn to route.
	Type string
	// Text is message.text — the human's message, fed to Resolve.
	Text string
	// Sender is message.sender.name — the requester's stable platform id (e.g. "users/123456789"), the
	// value Authorize matches against the target CR's allowedUsers. Never a display name (which is spoofable).
	Sender string
	// Space is space.name — carried into the audit record for correlation, never used for authz.
	Space string
	// Thread is message.thread.name — the Chat thread the turn belongs to. It is the primary thread-affinity
	// key (06 §6): follow-ups in the same thread stick to the agent the thread was first routed to.
	Thread string
}

// ThreadID is the affinity key for the turn: the Chat thread when present, else the space. A space with
// no explicit thread is itself one running conversation, so falling back to it keeps affinity working for
// flat (non-threaded) spaces; it is empty only if the event carried neither, in which case affinity is
// simply skipped. It is never an authz input — only a routing-continuity one.
func (e ChatEvent) ThreadID() string {
	if e.Thread != "" {
		return e.Thread
	}
	return e.Space
}

// ErrNotAMessageEvent means the event parsed cleanly but is not a MESSAGE (e.g. ADDED_TO_SPACE,
// REMOVED_FROM_SPACE). The receiver Acks and skips it: there is no turn to route, and redelivery would
// not change that. It is a distinct sentinel so the caller can tell "nothing to do" apart from "malformed".
var ErrNotAMessageEvent = errors.New("router: chat event is not a MESSAGE event")

// ParseChatEvent extracts the routing-relevant fields from a raw Google Chat event payload. It returns
// ErrNotAMessageEvent for a well-formed non-MESSAGE event (skip, don't retry) and a wrapped error for
// unparsable JSON (poison message — the receiver drops it rather than redelivering forever). The sender
// is read ONLY from message.sender.name (the stable id); a missing sender yields an empty Sender, which
// Authorize then refuses fail-closed — the parser never invents an identity.
func ParseChatEvent(raw []byte) (ChatEvent, error) {
	var envelope struct {
		Type    string `json:"type"`
		Message struct {
			Text   string `json:"text"`
			Sender struct {
				Name string `json:"name"`
			} `json:"sender"`
			Thread struct {
				Name string `json:"name"`
			} `json:"thread"`
		} `json:"message"`
		Space struct {
			Name string `json:"name"`
		} `json:"space"`
	}
	if err := json.Unmarshal(raw, &envelope); err != nil {
		return ChatEvent{}, fmt.Errorf("router: parse chat event: %w", err)
	}

	ev := ChatEvent{
		Type:   envelope.Type,
		Text:   envelope.Message.Text,
		Sender: envelope.Message.Sender.Name,
		Space:  envelope.Space.Name,
		Thread: envelope.Message.Thread.Name,
	}
	// An absent type is treated as a message (some Chat integrations omit it for direct message posts);
	// any explicitly non-MESSAGE type is skipped.
	if ev.Type != "" && ev.Type != "MESSAGE" {
		return ev, ErrNotAMessageEvent
	}
	return ev, nil
}
