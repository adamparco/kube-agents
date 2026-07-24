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

// Package pubsubsource is eventingress's cloud transport leg: it drains a PRE-CREATED Pub/Sub
// subscription of alert (Cloud Monitoring / Alertmanager) or GitHub-webhook messages, normalizes each,
// and hands it to the loopback relay. It is a SEPARATE package from internal/eventingress for the same
// reason the router splits pubsubinbound: the cloud.google.com Pub/Sub SDK stays out of the
// normalization + delivery core, which then unit-tests with no GCP SDK. The GCP client honors
// PUBSUB_EMULATOR_HOST, so this source runs against the emulator in tests exactly as against real
// Pub/Sub.
//
// DEFERRED (scratch-GKE), NOT FAKED (D1): the live cloud transport is only exercised on scratch GKE.
// On Kind the whole cloud leg is replaced by eventingress's synthetic-file source, which drives the
// SAME relay — so the in-pod terminus is Kind-provable while this transport is honestly marked
// scratch-GKE-deferred (same treatment as the Phase-2 V-G items).
//
// SUBSCRIBE-ONLY INVARIANT: this package only ever calls Subscriber(subID).Receive on a subscription
// created out-of-band by GitOps. It never creates a topic or subscription and never publishes — the
// agent identity carries no Pub/Sub publisher role. There is deliberately no publish code path here.
package pubsubsource

import (
	"context"
	"errors"
	"fmt"

	pubsub "cloud.google.com/go/pubsub/v2"
	"github.com/go-logr/logr"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/eventingress"
)

// Kind selects how a subscription's messages are normalized. One Source drains one subscription of one
// kind (alerts and GitHub webhooks arrive on separate topics/subscriptions).
type Kind int

const (
	// AlertKind normalizes each message as a Cloud Monitoring alert (NormalizeAlert).
	AlertKind Kind = iota
	// GitHubKind normalizes each message as a GitHub webhook (NormalizeGitHub). The event type is read
	// from the message's "X-GitHub-Event" attribute, matching how the webhook-forwarder tags it.
	GitHubKind
)

// deliverer is the subset of *eventingress.Relay this source needs; an interface so the ack policy can
// be unit-tested with a stub that records calls (no seam, no SDK).
type deliverer interface {
	Deliver(ctx context.Context, event eventingress.NormalizedEvent) (string, error)
}

// Source drains one subscription for the lifetime of its Start context, normalizing each message per
// its Kind and delivering it to the local seam via the relay. Like the router's Receiver it does NOT
// opt into leader election — eventingress runs as a single per-pod sidecar and Pub/Sub's at-least-once
// delivery is the redelivery mechanism.
type Source struct {
	client *pubsub.Client
	subID  string
	kind   Kind
	relay  deliverer
	log    logr.Logger
}

// New constructs a Source that pulls subID in projectID and delivers via relay. When
// PUBSUB_EMULATOR_HOST is set the client connects to the emulator with no credentials. The caller owns
// the returned Source and must Close it after Start returns.
func New(ctx context.Context, projectID, subID string, kind Kind, relay deliverer, log logr.Logger) (*Source, error) {
	if subID == "" {
		return nil, errors.New("pubsubsource: empty subscription id")
	}
	if relay == nil {
		return nil, errors.New("pubsubsource: nil relay")
	}
	c, err := pubsub.NewClient(ctx, projectID)
	if err != nil {
		return nil, fmt.Errorf("pubsubsource: new client for project %q: %w", projectID, err)
	}
	return &Source{client: c, subID: subID, kind: kind, relay: relay, log: log}, nil
}

// Start drains the subscription until ctx is cancelled, then returns. Receive dispatches callbacks
// concurrently; each callback settles exactly once (see handle).
func (s *Source) Start(ctx context.Context) error {
	s.log.Info("starting eventingress source", "subscription", s.subID, "kind", s.kind)
	err := s.client.Subscriber(s.subID).Receive(ctx, func(ctx context.Context, m *pubsub.Message) {
		s.handle(ctx, m)
	})
	if err != nil && !errors.Is(err, context.Canceled) {
		return fmt.Errorf("pubsubsource: receive on %q: %w", s.subID, err)
	}
	return nil
}

// handle normalizes and delivers one message, then settles it. Ack/Nack policy mirrors the router's
// receiver: a poison (unparsable) message is Acked (redelivery cannot help); a transient delivery
// failure to the seam is Nacked (Pub/Sub redelivers). Split out from Start so the policy is
// unit-testable with a synthesized *pubsub.Message and a stub relay.
func (s *Source) handle(ctx context.Context, m *pubsub.Message) {
	event, err := s.normalize(m)
	if err != nil {
		s.log.Error(err, "dropping unparsable eventingress message", "messageID", m.ID)
		m.Ack() // poison: Ack so it does not redeliver forever.
		return
	}
	if _, derr := s.relay.Deliver(ctx, event); derr != nil {
		s.log.Error(derr, "transient delivery failure; will retry", "messageID", m.ID)
		m.Nack() // seam unreachable / 5xx — let Pub/Sub redeliver.
		return
	}
	m.Ack()
}

// normalize converts a raw message to a NormalizedEvent per the source's Kind.
func (s *Source) normalize(m *pubsub.Message) (eventingress.NormalizedEvent, error) {
	switch s.kind {
	case AlertKind:
		return eventingress.NormalizeAlert(m.Data)
	case GitHubKind:
		return eventingress.NormalizeGitHub(m.Attributes["X-GitHub-Event"], m.Data)
	default:
		return nil, fmt.Errorf("pubsubsource: unknown source kind %d", s.kind)
	}
}

// NeedLeaderElection reports false: a single per-pod sidecar relies on Pub/Sub delivery, so the puller
// runs regardless of any leader-election state (mirrors the router's receiver).
func (s *Source) NeedLeaderElection() bool { return false }

// Close releases the underlying Pub/Sub client. Call after Start has returned.
func (s *Source) Close() error { return s.client.Close() }
