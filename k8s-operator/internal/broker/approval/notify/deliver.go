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
	"context"
	"fmt"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval"
)

// Platform names a chat platform a Target is delivered on.
type Platform string

const (
	PlatformSlack      Platform = "slack"
	PlatformGoogleChat Platform = "googlechat"
)

// Target is where one roster's notifications are delivered, resolved from ApprovalRoster.Spec.Notify.
type Target struct {
	Platform Platform
	// Channel is the Slack channel ID or the Google Chat space resource name, depending on Platform.
	Channel string
}

// ResolveTarget reads the roster's notify destination (chat-approval.md §2: "the delivery target
// comes from the roster alone"). It returns the zero Target and false when no destination is
// configured — the caller's response to that is "send nothing", never a fallback channel, because
// a fallback would deliver a gated action's details somewhere the roster author never named.
func ResolveTarget(roster *agentv1alpha1.ApprovalRoster) (Target, bool) {
	if roster == nil || roster.Spec.Notify == nil {
		return Target{}, false
	}
	if s := roster.Spec.Notify.Slack; s != nil && s.Channel != "" {
		return Target{Platform: PlatformSlack, Channel: s.Channel}, true
	}
	if g := roster.Spec.Notify.GoogleChat; g != nil && g.Space != "" {
		return Target{Platform: PlatformGoogleChat, Channel: g.Space}, true
	}
	return Target{}, false
}

// Deliverer sends and edits chat messages. Deliver posts a new message and returns an opaque
// reference (a Slack message timestamp, or a Google Chat message resource name) that a later
// Update call edits in place — the mechanism the notifier uses to mark a resolved record without
// posting a second message (chat-approval.md §2).
type Deliverer interface {
	Deliver(ctx context.Context, target Target, message approval.Message) (ref string, err error)
	Update(ctx context.Context, target Target, ref string, message approval.Message) error
}

// Deliverers dispatches to the right platform-specific Deliverer. It implements Deliverer itself so
// the reconciler holds one dependency regardless of how many platforms are configured.
type Deliverers map[Platform]Deliverer

func (d Deliverers) Deliver(ctx context.Context, target Target, message approval.Message) (string, error) {
	del, ok := d[target.Platform]
	if !ok {
		return "", fmt.Errorf("notify: no deliverer configured for platform %q", target.Platform)
	}
	return del.Deliver(ctx, target, message)
}

func (d Deliverers) Update(ctx context.Context, target Target, ref string, message approval.Message) error {
	del, ok := d[target.Platform]
	if !ok {
		return fmt.Errorf("notify: no deliverer configured for platform %q", target.Platform)
	}
	return del.Update(ctx, target, ref, message)
}
