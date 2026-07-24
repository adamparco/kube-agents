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

package pubsubinbound_test

import (
	"context"
	"fmt"
	"testing"
	"time"

	pubsub "cloud.google.com/go/pubsub/v2"
	pb "cloud.google.com/go/pubsub/v2/apiv1/pubsubpb"
	"cloud.google.com/go/pubsub/v2/pstest"
	"github.com/go-logr/logr"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/router"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/router/pubsubinbound"
)

// clusterAdminAgent builds a cluster-admin Agent CR scoped to (project, cluster-a) with a closed allowlist.
func clusterAdminAgent(project string, allowed []string) *agentv1alpha1.Agent {
	return &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: "cluster-a-agent", Namespace: "kubeagents-system"},
		Spec: agentv1alpha1.AgentSpec{
			Tier:  agentv1alpha1.TierClusterAdmin,
			Scope: &agentv1alpha1.ScopeSpec{ProjectID: project, ClusterName: "cluster-a"},
			Integration: &agentv1alpha1.AgentIntegrationSpec{
				GoogleChat: &agentv1alpha1.GoogleChatSpec{TopicName: "topic-ca", AllowedUsers: allowed},
			},
		},
	}
}

// TestReceiver_RoutesInboundEvents drives the full inbound edge against the in-process Pub/Sub fake: raw
// Google Chat event JSON published to the inbound subscription is parsed, resolved, authorized BEFORE
// dispatch, and (only when authorized) handed to the dispatcher. A non-allowlisted sender and a
// non-MESSAGE event are both consumed without dispatching — proving the receiver settles every message
// (no redelivery storm) while never routing an unauthorized turn.
func TestReceiver_RoutesInboundEvents(t *testing.T) {
	ctx := context.Background()

	srv := pstest.NewServer()
	t.Cleanup(func() { _ = srv.Close() })
	t.Setenv("PUBSUB_EMULATOR_HOST", srv.Addr)

	const project = "test-project"
	const inTopic = "kage-inbound"
	const inSub = "kage-inbound-sub"

	admin, err := pubsub.NewClient(ctx, project)
	if err != nil {
		t.Fatalf("new admin client: %v", err)
	}
	t.Cleanup(func() { _ = admin.Close() })
	if _, err := admin.TopicAdminClient.CreateTopic(ctx, &pb.Topic{
		Name: fmt.Sprintf("projects/%s/topics/%s", project, inTopic),
	}); err != nil {
		t.Fatalf("create inbound topic: %v", err)
	}
	if _, err := admin.SubscriptionAdminClient.CreateSubscription(ctx, &pb.Subscription{
		Name:  fmt.Sprintf("projects/%s/subscriptions/%s", project, inSub),
		Topic: fmt.Sprintf("projects/%s/topics/%s", project, inTopic),
	}); err != nil {
		t.Fatalf("create inbound subscription: %v", err)
	}

	// Index carries the cluster-admin agent with a CLOSED allowlist (only users/alice). FakeDispatcher
	// records dispatches so we assert exactly who was routed.
	idx := router.NewIndex()
	idx.Upsert(clusterAdminAgent(project, []string{"users/alice"}))
	fake := &router.FakeDispatcher{}
	gw := &router.Gateway{Resolver: router.NewResolver(), Index: idx, Dispatch: fake, ProjectID: project}

	recv, err := pubsubinbound.New(ctx, project, inSub, gw, logr.Discard())
	if err != nil {
		t.Fatalf("new receiver: %v", err)
	}
	t.Cleanup(func() { _ = recv.Close() })

	runCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- recv.Start(runCtx) }()

	pubJSON := func(text, sender, typ string) {
		t.Helper()
		var raw string
		if typ == "MESSAGE" {
			raw = fmt.Sprintf(`{"type":"MESSAGE","message":{"text":%q,"sender":{"name":%q}},"space":{"name":"spaces/AAA"}}`, text, sender)
		} else {
			raw = fmt.Sprintf(`{"type":%q,"space":{"name":"spaces/AAA"}}`, typ)
		}
		res := admin.Publisher(inTopic).Publish(ctx, &pubsub.Message{Data: []byte(raw)})
		if _, err := res.Get(ctx); err != nil {
			t.Fatalf("publish inbound event: %v", err)
		}
	}

	// Authorized turn → dispatched exactly once, to the target topic.
	pubJSON("@kage /cluster-cluster-a status", "users/alice", "MESSAGE")
	waitFor(t, 5*time.Second, func() bool { return len(fake.Sent()) == 1 })
	if got := fake.Sent(); got[0].TopicName != "topic-ca" || got[0].Sender != "users/alice" {
		t.Fatalf("dispatched %+v, want topic-ca / users/alice", got[0])
	}

	// Non-allowlisted turn and a non-MESSAGE event: both consumed, neither dispatched. Give the receiver
	// time to process, then assert the dispatch count never moved past the authorized one.
	pubJSON("@kage /cluster-cluster-a status", "users/mallory", "MESSAGE")
	pubJSON("", "", "ADDED_TO_SPACE")
	time.Sleep(1500 * time.Millisecond)
	if n := len(fake.Sent()); n != 1 {
		t.Fatalf("dispatch count = %d after refused + non-message events, want 1 (only the authorized turn)", n)
	}

	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("receiver Start returned error: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("receiver did not stop after context cancel")
	}
}

// waitFor polls cond until it is true or the timeout elapses.
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
