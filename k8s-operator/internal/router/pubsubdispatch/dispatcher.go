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

// Package pubsubdispatch is the production router.Dispatcher: it re-publishes an authorized chat turn to
// the TARGET agent's own Pub/Sub topic (Decision 2). The target pod's existing credential proxy drains
// that topic exactly as it does today, so routing to a per-tier pod needs NO change to credential_proxy
// or the agent runtime — the router just fans the inbound event out to the right topic.
//
// It is a SEPARATE package from internal/router on purpose: the cloud.google.com/go/pubsub dependency
// (and its transitive gRPC/auth tree) stays out of the core package, so the security-load-bearing
// resolve/authorize/index unit tests compile and run without any GCP SDK. The GCP client honors
// PUBSUB_EMULATOR_HOST transparently, which is what lets the Phase-2 integration test run hermetically
// against the emulator (no real project, no credentials).
package pubsubdispatch

import (
	"context"
	"fmt"
	"sync"

	pubsub "cloud.google.com/go/pubsub/v2"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/router"
)

// Dispatcher publishes routed messages to per-target topics in a single GCP project (the router's own
// project — the same project the Agent CRs' topicName values live in). Publisher handles are cached
// because pubsub.Publisher is safe for concurrent use and batches publishes.
type Dispatcher struct {
	client *pubsub.Client

	mu         sync.Mutex
	publishers map[string]*pubsub.Publisher
}

// New constructs a Dispatcher for projectID. When PUBSUB_EMULATOR_HOST is set the client connects to the
// emulator with no credentials; otherwise it uses Application Default Credentials. The caller owns the
// returned Dispatcher and must Close it.
func New(ctx context.Context, projectID string) (*Dispatcher, error) {
	c, err := pubsub.NewClient(ctx, projectID)
	if err != nil {
		return nil, fmt.Errorf("pubsubdispatch: new client for project %q: %w", projectID, err)
	}
	return &Dispatcher{client: c, publishers: make(map[string]*pubsub.Publisher)}, nil
}

// publisher returns a cached publisher handle for a topic id.
func (d *Dispatcher) publisher(id string) *pubsub.Publisher {
	d.mu.Lock()
	defer d.mu.Unlock()
	if p, ok := d.publishers[id]; ok {
		return p
	}
	p := d.client.Publisher(id)
	d.publishers[id] = p
	return p
}

// Dispatch re-publishes msg to target.TopicName. It blocks until the publish is acknowledged so a
// publish failure surfaces to the gateway (which audits it) rather than being lost in a background
// buffer. The routing metadata is attached as attributes for downstream audit correlation; the payload
// is the original event (msg.Raw) or the text fallback.
func (d *Dispatcher) Dispatch(ctx context.Context, target router.Target, msg router.Message) error {
	if target.TopicName == "" {
		return fmt.Errorf("pubsubdispatch: target %s has no topicName", target.Handle)
	}
	data := msg.Raw
	if len(data) == 0 {
		data = []byte(msg.Text)
	}

	attrs := make(map[string]string, len(msg.Attrs)+4)
	for k, v := range msg.Attrs {
		attrs[k] = v
	}
	// Router-added correlation attributes (do not overwrite caller attrs blindly except our namespaced keys).
	attrs["kage_router"] = "true"
	attrs["kage_target_identity"] = target.Identity
	attrs["kage_target_handle"] = target.Handle
	attrs["kage_sender"] = msg.Sender

	res := d.publisher(target.TopicName).Publish(ctx, &pubsub.Message{Data: data, Attributes: attrs})
	if _, err := res.Get(ctx); err != nil {
		return fmt.Errorf("pubsubdispatch: publish to %q: %w", target.TopicName, err)
	}
	return nil
}

// Close stops all cached publishers and closes the client.
func (d *Dispatcher) Close() error {
	d.mu.Lock()
	for _, p := range d.publishers {
		p.Stop()
	}
	d.mu.Unlock()
	return d.client.Close()
}

// compile-time assertion: *Dispatcher is a router.Dispatcher.
var _ router.Dispatcher = (*Dispatcher)(nil)
