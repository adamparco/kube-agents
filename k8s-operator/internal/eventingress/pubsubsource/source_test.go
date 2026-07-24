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

package pubsubsource_test

import (
	"context"
	"fmt"
	"sync"
	"testing"
	"time"

	pubsub "cloud.google.com/go/pubsub/v2"
	pb "cloud.google.com/go/pubsub/v2/apiv1/pubsubpb"
	"cloud.google.com/go/pubsub/v2/pstest"
	"github.com/go-logr/logr"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/eventingress"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/eventingress/pubsubsource"
)

// stubRelay records delivered events (and can be told to fail) so the source's normalize + ack policy
// are testable with no real seam.
type stubRelay struct {
	mu      sync.Mutex
	events  []eventingress.NormalizedEvent
	failErr error
}

func (s *stubRelay) Deliver(_ context.Context, event eventingress.NormalizedEvent) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.failErr != nil {
		return "", s.failErr
	}
	s.events = append(s.events, event)
	return "k8s-evt-stub", nil
}

func (s *stubRelay) count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.events)
}

func (s *stubRelay) first() eventingress.NormalizedEvent {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.events) == 0 {
		return nil
	}
	return s.events[0]
}

// TestSource_DrainsAlertsToRelay drives the real Pub/Sub subscribe path against the in-process fake: a
// Cloud Monitoring alert JSON published to the alert subscription is pulled, normalized, and delivered
// to the (stub) relay as a {kind:alert} event — proving the deferred cloud leg is real code (emulator),
// not a fake, exactly as the router's receiver test does.
func TestSource_DrainsAlertsToRelay(t *testing.T) {
	ctx := context.Background()
	srv := pstest.NewServer()
	t.Cleanup(func() { _ = srv.Close() })
	t.Setenv("PUBSUB_EMULATOR_HOST", srv.Addr)

	const project = "test-project"
	const topic = "kage-alerts"
	const sub = "kage-alerts-sub"

	admin, err := pubsub.NewClient(ctx, project)
	if err != nil {
		t.Fatalf("new admin client: %v", err)
	}
	t.Cleanup(func() { _ = admin.Close() })
	if _, err := admin.TopicAdminClient.CreateTopic(ctx, &pb.Topic{
		Name: fmt.Sprintf("projects/%s/topics/%s", project, topic),
	}); err != nil {
		t.Fatalf("create alert topic: %v", err)
	}
	if _, err := admin.SubscriptionAdminClient.CreateSubscription(ctx, &pb.Subscription{
		Name:  fmt.Sprintf("projects/%s/subscriptions/%s", project, sub),
		Topic: fmt.Sprintf("projects/%s/topics/%s", project, topic),
	}); err != nil {
		t.Fatalf("create alert subscription: %v", err)
	}

	relay := &stubRelay{}
	src, err := pubsubsource.New(ctx, project, sub, pubsubsource.AlertKind, relay, logr.Discard())
	if err != nil {
		t.Fatalf("new source: %v", err)
	}
	t.Cleanup(func() { _ = src.Close() })

	runCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- src.Start(runCtx) }()

	alertJSON := `{"incident":{"policy_name":"5xx-slo","summary":"burn","state":"open","severity":"critical"}}`
	res := admin.Publisher(topic).Publish(ctx, &pubsub.Message{Data: []byte(alertJSON)})
	if _, err := res.Get(ctx); err != nil {
		t.Fatalf("publish alert: %v", err)
	}

	// A poison (non-JSON) message must be Acked and dropped — never delivered.
	poison := admin.Publisher(topic).Publish(ctx, &pubsub.Message{Data: []byte("not-json")})
	if _, err := poison.Get(ctx); err != nil {
		t.Fatalf("publish poison: %v", err)
	}

	waitFor(t, 5*time.Second, func() bool { return relay.count() == 1 })
	ev := relay.first()
	if ev["kind"] != eventingress.KindAlert || ev["policy"] != "5xx-slo" {
		t.Fatalf("delivered event wrong: %#v", ev)
	}
	// Give the poison time to (not) deliver; count must stay 1.
	time.Sleep(750 * time.Millisecond)
	if n := relay.count(); n != 1 {
		t.Fatalf("delivery count = %d, want 1 (poison must be dropped, not delivered)", n)
	}

	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("source Start returned error: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("source did not stop after context cancel")
	}
}

func TestNew_Validates(t *testing.T) {
	ctx := context.Background()
	srv := pstest.NewServer()
	t.Cleanup(func() { _ = srv.Close() })
	t.Setenv("PUBSUB_EMULATOR_HOST", srv.Addr)

	if _, err := pubsubsource.New(ctx, "p", "", pubsubsource.AlertKind, &stubRelay{}, logr.Discard()); err == nil {
		t.Error("expected error for empty subscription id")
	}
	if _, err := pubsubsource.New(ctx, "p", "sub", pubsubsource.AlertKind, nil, logr.Discard()); err == nil {
		t.Error("expected error for nil relay")
	}
}

// waitFor polls cond until true or timeout.
func waitFor(t *testing.T, timeout time.Duration, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(25 * time.Millisecond)
	}
	if !cond() {
		t.Fatalf("condition not met within %s", timeout)
	}
}
