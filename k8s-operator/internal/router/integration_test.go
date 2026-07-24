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

package router_test

import (
	"context"
	"errors"
	"fmt"
	"testing"
	"time"

	pubsub "cloud.google.com/go/pubsub/v2"
	pb "cloud.google.com/go/pubsub/v2/apiv1/pubsubpb"
	"cloud.google.com/go/pubsub/v2/pstest"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/router"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/router/pubsubdispatch"
)

// clusterAdminAgent builds a cluster-admin Agent CR scoped to (project, cluster-a) whose googleChat
// topic + closed allowlist drive the routing test.
func clusterAdminAgent(project, topic string, allowed []string) *agentv1alpha1.Agent {
	return &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: "cluster-a-agent", Namespace: "kubeagents-system"},
		Spec: agentv1alpha1.AgentSpec{
			Tier:  agentv1alpha1.TierClusterAdmin,
			Scope: &agentv1alpha1.ScopeSpec{ProjectID: project, ClusterName: "cluster-a"},
			Integration: &agentv1alpha1.AgentIntegrationSpec{
				GoogleChat: &agentv1alpha1.GoogleChatSpec{TopicName: topic, AllowedUsers: allowed},
			},
		},
	}
}

// TestGateway_PubSubDispatch_Emulator exercises the full Phase-2 routing path against an in-process
// Pub/Sub fake (pstest — no gcloud/Java/docker, always runnable): an authorized `@cluster-<c>` turn is
// resolved, authorized before dispatch, and re-published to the TARGET agent's own topic, where a
// verifier subscription drains it (Decision 2). A non-allowlisted sender is refused before any publish.
func TestGateway_PubSubDispatch_Emulator(t *testing.T) {
	ctx := context.Background()

	srv := pstest.NewServer()
	t.Cleanup(func() { _ = srv.Close() })
	// Point every pubsub client (dispatcher + verifier) at the in-process fake.
	t.Setenv("PUBSUB_EMULATOR_HOST", srv.Addr)

	const project = "test-project"
	const topicID = "kubeagents-cluster-admin-cluster-a-events"
	const subID = topicID + "-verify"

	// Admin/verifier client: create the target topic and a subscription to observe deliveries.
	admin, err := pubsub.NewClient(ctx, project)
	if err != nil {
		t.Fatalf("new admin client: %v", err)
	}
	t.Cleanup(func() { _ = admin.Close() })
	if _, err := admin.TopicAdminClient.CreateTopic(ctx, &pb.Topic{
		Name: fmt.Sprintf("projects/%s/topics/%s", project, topicID),
	}); err != nil {
		t.Fatalf("create topic: %v", err)
	}
	if _, err := admin.SubscriptionAdminClient.CreateSubscription(ctx, &pb.Subscription{
		Name:  fmt.Sprintf("projects/%s/subscriptions/%s", project, subID),
		Topic: fmt.Sprintf("projects/%s/topics/%s", project, topicID),
	}); err != nil {
		t.Fatalf("create subscription: %v", err)
	}

	// Router dispatcher (its own client, same emulator via PUBSUB_EMULATOR_HOST).
	disp, err := pubsubdispatch.New(ctx, project)
	if err != nil {
		t.Fatalf("new dispatcher: %v", err)
	}
	t.Cleanup(func() { _ = disp.Close() })

	// Index carries the cluster-admin agent with a CLOSED allowlist (only users/alice).
	idx := router.NewIndex()
	idx.Upsert(clusterAdminAgent(project, topicID, []string{"users/alice"}))

	g := &router.Gateway{Resolver: router.NewResolver(), Index: idx, Dispatch: disp, ProjectID: project}

	// Authorized turn: resolves via slash command, authorized, published to the target topic.
	out, err := g.Handle(ctx, router.Message{
		Text:   "@kage /cluster-cluster-a report status",
		Sender: "users/alice",
		Raw:    []byte("payload-for-cluster-a"),
	})
	if err != nil {
		t.Fatalf("authorized turn errored: %v", err)
	}
	if !out.Dispatched {
		t.Fatalf("authorized turn not dispatched: %+v", out)
	}

	// Drain the verifier subscription and assert the payload + routing attributes arrived.
	recvCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	var got *pubsub.Message
	if err := admin.Subscriber(subID).Receive(recvCtx, func(_ context.Context, m *pubsub.Message) {
		got = m
		m.Ack()
		cancel()
	}); err != nil && !errors.Is(err, context.Canceled) {
		t.Fatalf("receive: %v", err)
	}
	if got == nil {
		t.Fatal("no message delivered to the target topic")
	}
	if string(got.Data) != "payload-for-cluster-a" {
		t.Errorf("payload = %q, want payload-for-cluster-a", string(got.Data))
	}
	if got.Attributes["kage_sender"] != "users/alice" {
		t.Errorf("kage_sender attr = %q, want users/alice", got.Attributes["kage_sender"])
	}
	if got.Attributes["kage_target_handle"] != "@cluster-admin-cluster-a" {
		t.Errorf("kage_target_handle attr = %q, want @cluster-admin-cluster-a", got.Attributes["kage_target_handle"])
	}

	// Non-allowlisted turn: refused before dispatch — nothing new lands on the topic.
	_, err = g.Handle(ctx, router.Message{
		Text:   "@kage /cluster-cluster-a report status",
		Sender: "users/mallory",
		Raw:    []byte("should-never-arrive"),
	})
	if !errors.Is(err, router.ErrUnauthorized) {
		t.Fatalf("non-allowlisted turn err = %v, want ErrUnauthorized", err)
	}
	// Confirm the refused payload was never published (short bounded pull).
	negCtx, negCancel := context.WithTimeout(ctx, 1500*time.Millisecond)
	defer negCancel()
	var leaked *pubsub.Message
	_ = admin.Subscriber(subID).Receive(negCtx, func(_ context.Context, m *pubsub.Message) {
		leaked = m
		m.Ack()
		negCancel()
	})
	if leaked != nil {
		t.Errorf("refused turn leaked a message to the topic: %q", string(leaked.Data))
	}
}
