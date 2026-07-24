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

// Command kage-router is the ChatOps multi-tier router (05 C15, 06 §2b). It runs a read-only
// controller-runtime manager that keeps an in-memory routing Index in sync with the Agent CRs, and an
// inbound Pub/Sub receiver that resolves each chat turn to exactly one (tier, scope) agent, authorizes
// the sender against THAT agent's allowlist BEFORE dispatch, and only then re-publishes the turn to the
// target agent's own topic. It never mutates any cluster object: its RBAC is get/list/watch on agents.
package main

import (
	"flag"
	"os"

	// Import all Kubernetes client auth plugins (e.g. GCP, OIDC) so in-cluster and exec auth both work.
	_ "k8s.io/client-go/plugin/pkg/client/auth"

	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/router"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/router/pubsubdispatch"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/router/pubsubinbound"
)

var (
	scheme   = runtime.NewScheme()
	setupLog = ctrl.Log.WithName("setup")
)

func init() {
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	utilruntime.Must(agentv1alpha1.AddToScheme(scheme))
}

func main() {
	var probeAddr string
	var projectID string
	var inboundSub string
	flag.StringVar(&probeAddr, "health-probe-bind-address", ":8081", "The address the probe endpoint binds to.")
	flag.StringVar(&projectID, "project-id", os.Getenv("KAGE_PROJECT_ID"),
		"GCP project the router publishes into (also fills cluster-admin handle scope). Env: KAGE_PROJECT_ID.")
	flag.StringVar(&inboundSub, "inbound-subscription", os.Getenv("KAGE_INBOUND_SUBSCRIPTION"),
		"Pub/Sub subscription id the router pulls inbound chat events from. Env: KAGE_INBOUND_SUBSCRIPTION.")
	opts := zap.Options{Development: true}
	opts.BindFlags(flag.CommandLine)
	flag.Parse()

	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&opts)))

	if projectID == "" {
		setupLog.Error(nil, "missing required --project-id / KAGE_PROJECT_ID")
		os.Exit(1)
	}
	if inboundSub == "" {
		setupLog.Error(nil, "missing required --inbound-subscription / KAGE_INBOUND_SUBSCRIPTION")
		os.Exit(1)
	}

	// The router serves only health probes; metrics are disabled (BindAddress "0") to keep the surface
	// minimal. No webhook server: the router mutates nothing.
	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme:                 scheme,
		Metrics:                metricsserver.Options{BindAddress: "0"},
		HealthProbeBindAddress: probeAddr,
		// Single-replica router; the inbound puller relies on Pub/Sub delivery, not leader election.
		LeaderElection: false,
	})
	if err != nil {
		setupLog.Error(err, "unable to start manager")
		os.Exit(1)
	}

	// Read-only informer: keep the routing Index in sync with the Agent CRs (get/list/watch only).
	idx := router.NewIndex()
	if err := (&router.Reconciler{Client: mgr.GetClient(), Index: idx}).SetupWithManager(mgr); err != nil {
		setupLog.Error(err, "unable to set up router index reconciler")
		os.Exit(1)
	}

	ctx := ctrl.SetupSignalHandler()

	// Production dispatcher: re-publishes authorized turns to the target agent's own topic (Decision 2).
	disp, err := pubsubdispatch.New(ctx, projectID)
	if err != nil {
		setupLog.Error(err, "unable to create pubsub dispatcher")
		os.Exit(1)
	}

	gw := &router.Gateway{
		Resolver:  router.NewResolver(), // Phase 2: slash/handle only; inference refused, never spent.
		Index:     idx,
		Dispatch:  disp,
		ProjectID: projectID,
		Audit:     router.LogAuditSink{Log: ctrl.Log.WithName("audit")},
	}

	recv, err := pubsubinbound.New(ctx, projectID, inboundSub, gw, ctrl.Log.WithName("inbound"))
	if err != nil {
		setupLog.Error(err, "unable to create inbound receiver")
		os.Exit(1)
	}
	if err := mgr.Add(recv); err != nil {
		setupLog.Error(err, "unable to add inbound receiver to manager")
		os.Exit(1)
	}

	if err := mgr.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		setupLog.Error(err, "unable to set up health check")
		os.Exit(1)
	}
	if err := mgr.AddReadyzCheck("readyz", healthz.Ping); err != nil {
		setupLog.Error(err, "unable to set up ready check")
		os.Exit(1)
	}

	setupLog.Info("starting kage-router", "project", projectID, "inboundSubscription", inboundSub)
	if err := mgr.Start(ctx); err != nil {
		setupLog.Error(err, "manager exited with error")
		_ = disp.Close()
		_ = recv.Close()
		os.Exit(1)
	}

	// Graceful shutdown: release the Pub/Sub clients after the manager has stopped its runnables.
	_ = disp.Close()
	_ = recv.Close()
}
