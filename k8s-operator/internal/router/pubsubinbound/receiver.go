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

// Package pubsubinbound is the router's inbound edge: it pulls raw Google Chat events from a single
// shared subscription, parses each into a routing turn, and hands it to the Gateway — which resolves,
// authorizes BEFORE dispatch, and (only then) re-publishes to the target agent's own topic. It is a
// SEPARATE package from internal/router for the same reason as pubsubdispatch: the cloud.google.com/go
// Pub/Sub dependency stays out of the security-load-bearing core, so resolve/authorize/index unit-test
// with no GCP SDK. The GCP client honors PUBSUB_EMULATOR_HOST, so this receiver runs against the emulator
// in tests exactly as it runs against real Pub/Sub in production.
package pubsubinbound

import (
	"context"
	"errors"
	"fmt"

	pubsub "cloud.google.com/go/pubsub/v2"
	"github.com/go-logr/logr"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/router"
)

// Receiver is a controller-runtime manager.Runnable that drains the inbound subscription for the lifetime
// of its Start context. One Receiver = one active puller; it deliberately does NOT opt into leader
// election (NeedLeaderElection returns false) because the router runs a single replica and Pub/Sub's own
// at-least-once delivery is the redelivery mechanism — adding leader election would only widen the RBAC
// surface (leases) for no gain, working against the least-privilege point of the router's viewer role.
type Receiver struct {
	client  *pubsub.Client
	subID   string
	gateway *router.Gateway
	log     logr.Logger
}

// New constructs a Receiver that pulls subID in projectID and routes each turn through gw. When
// PUBSUB_EMULATOR_HOST is set the client connects to the emulator with no credentials. The caller owns
// the returned Receiver and must Close it after the manager stops.
func New(ctx context.Context, projectID, subID string, gw *router.Gateway, log logr.Logger) (*Receiver, error) {
	if subID == "" {
		return nil, errors.New("pubsubinbound: empty inbound subscription id")
	}
	if gw == nil {
		return nil, errors.New("pubsubinbound: nil gateway")
	}
	c, err := pubsub.NewClient(ctx, projectID)
	if err != nil {
		return nil, fmt.Errorf("pubsubinbound: new client for project %q: %w", projectID, err)
	}
	return &Receiver{client: c, subID: subID, gateway: gw, log: log}, nil
}

// Start drains the subscription until ctx is cancelled (manager shutdown), then returns. Receive blocks
// and dispatches callbacks concurrently; each callback routes exactly one turn and always Acks or Nacks:
//
//   - unparsable payload (poison) or non-MESSAGE event  → Ack + skip (redelivery cannot help)
//   - gateway returns a deterministic refusal            → Ack (turn handled; the refusal is terminal)
//   - gateway returns a transient dispatch failure       → Nack (Pub/Sub redelivers)
//   - gateway dispatched                                 → Ack
//
// The gateway audits every turn (including refusals); this layer only classifies the ack disposition and
// logs transient failures, never the message text.
func (r *Receiver) Start(ctx context.Context) error {
	r.log.Info("starting inbound receiver", "subscription", r.subID)
	err := r.client.Subscriber(r.subID).Receive(ctx, func(ctx context.Context, m *pubsub.Message) {
		r.handle(ctx, m)
	})
	if err != nil && !errors.Is(err, context.Canceled) {
		return fmt.Errorf("pubsubinbound: receive on %q: %w", r.subID, err)
	}
	return nil
}

// handle routes a single message and settles it (Ack/Nack). Split out from Start so the ack policy is
// unit-testable with a synthesized *pubsub.Message.
func (r *Receiver) handle(ctx context.Context, m *pubsub.Message) {
	ev, err := router.ParseChatEvent(m.Data)
	if err != nil {
		if errors.Is(err, router.ErrNotAMessageEvent) {
			m.Ack() // nothing to route (e.g. ADDED_TO_SPACE); redelivery would not change that.
			return
		}
		r.log.Error(err, "dropping unparsable inbound event", "messageID", m.ID)
		m.Ack() // poison message: Ack so it does not redeliver forever.
		return
	}

	_, herr := r.gateway.Handle(ctx, router.Message{
		Text:     ev.Text,
		Sender:   ev.Sender,
		ThreadID: ev.ThreadID(),
		// TraceID roots the attribution chain (Phase 5 T-A): the inbound Pub/Sub message id is unique per
		// turn and stable across redelivery, so it correlates this turn's audit record with the kage_trace_id
		// attribute the dispatcher stamps and the Trace-Id: trailer the agent later puts on its GitOps PR.
		TraceID: m.ID,
		Raw:     m.Data,
		Attrs:   map[string]string{"kage_space": ev.Space},
	})
	if herr != nil {
		if router.IsDeterministicRefusal(herr) {
			m.Ack() // terminal refusal (already audited by the gateway); redelivery is pointless.
			return
		}
		r.log.Error(herr, "transient dispatch failure; will retry", "messageID", m.ID)
		m.Nack() // e.g. a publish failure — let Pub/Sub redeliver.
		return
	}
	m.Ack()
}

// NeedLeaderElection reports false: the router runs a single replica and relies on Pub/Sub delivery, so
// the puller must run regardless of any leader-election state (see the type comment).
func (r *Receiver) NeedLeaderElection() bool { return false }

// Close releases the underlying Pub/Sub client. Call after the manager has stopped.
func (r *Receiver) Close() error { return r.client.Close() }
