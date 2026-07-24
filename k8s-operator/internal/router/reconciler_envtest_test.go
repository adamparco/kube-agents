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
	"os"
	"path/filepath"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/agentindex"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/router"
)

// TestReconciler_SyncsIndexFromAPIServer proves the read-only informer path against a real API server
// (envtest): an Agent CR created through the API server is observed by the Reconciler and materializes
// in the routing Index at the SAME key agentindex derives; deleting the CR evicts the route. This is the
// only test that exercises the Get/list/watch wiring end to end. It SKIPS when KUBEBUILDER_ASSETS is
// unset (plain `go test ./...`), and runs under `make test`, which provisions the envtest binaries.
func TestReconciler_SyncsIndexFromAPIServer(t *testing.T) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		t.Skip("KUBEBUILDER_ASSETS unset; run via `make test` to exercise the envtest reconciler path")
	}

	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		t.Fatalf("add clientgo scheme: %v", err)
	}
	if err := agentv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("add agent scheme: %v", err)
	}

	testEnv := &envtest.Environment{
		CRDDirectoryPaths:     []string{filepath.Join("..", "..", "config", "crd", "bases")},
		ErrorIfCRDPathMissing: true,
		Scheme:                scheme,
	}
	cfg, err := testEnv.Start()
	if err != nil {
		t.Fatalf("start envtest: %v", err)
	}
	t.Cleanup(func() { _ = testEnv.Stop() })

	k8s, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatalf("new client: %v", err)
	}

	ctx := context.Background()

	// Create the CR in the default namespace (always present in envtest) to avoid a namespace dependency.
	agent := &agentv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: "cluster-a-agent", Namespace: "default"},
		Spec: agentv1alpha1.AgentSpec{
			Tier:  agentv1alpha1.TierClusterAdmin,
			Scope: &agentv1alpha1.ScopeSpec{ProjectID: "proj-x", ClusterName: "cluster-a"},
			Harness: &agentv1alpha1.HarnessSpec{
				ClusterName: "cluster-a",
				Location:    "us-central1-a",
			},
			Integration: &agentv1alpha1.AgentIntegrationSpec{
				GoogleChat: &agentv1alpha1.GoogleChatSpec{
					TopicName:    "kubeagents-cluster-admin-cluster-a-events",
					AllowedUsers: []string{"users/alice"},
				},
			},
		},
	}
	if err := k8s.Create(ctx, agent); err != nil {
		t.Fatalf("create agent CR: %v", err)
	}

	idx := router.NewIndex()
	r := &router.Reconciler{Client: k8s, Index: idx}
	nn := types.NamespacedName{Namespace: "default", Name: "cluster-a-agent"}

	// Reconcile the create: the route must appear at the agentindex key.
	if _, err := r.Reconcile(ctx, reconcile.Request{NamespacedName: nn}); err != nil {
		t.Fatalf("reconcile create: %v", err)
	}
	wantKey := agentindex.ScopeIdentity(agent)
	tgt, ok := idx.Lookup(wantKey)
	if !ok {
		t.Fatalf("route not indexed at key %q after create", wantKey)
	}
	if tgt.TopicName != "kubeagents-cluster-admin-cluster-a-events" || tgt.Handle != "@cluster-admin-cluster-a" {
		t.Errorf("indexed target = %+v, want topic/handle for cluster-a", tgt)
	}
	if len(tgt.AllowedUsers) != 1 || tgt.AllowedUsers[0] != "users/alice" {
		t.Errorf("indexed allowlist = %v, want [users/alice]", tgt.AllowedUsers)
	}

	// Reconcile the delete: the route must be evicted.
	if err := k8s.Delete(ctx, agent); err != nil {
		t.Fatalf("delete agent CR: %v", err)
	}
	if _, err := r.Reconcile(ctx, reconcile.Request{NamespacedName: nn}); err != nil {
		t.Fatalf("reconcile delete: %v", err)
	}
	if _, ok := idx.Lookup(wantKey); ok {
		t.Error("route still indexed after delete; phantom route survived")
	}
}
