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

// Command kage-chatops-gateway is the ChatOps gateway (docs/designs/broker/chat-approval.md §3):
// the one workload that runs as system:serviceaccount:kubeagents-system:kube-agents-chatops-gateway,
// and the only writer of ActionRecord.status.approvals in the system
// (config/policy/vap-agent-scope-journal.yaml). It also runs the approval notifier (§2), the
// delivery half of the same loop — v1 ships both in one Deployment, one pod, one ServiceAccount,
// because the VAP constrains what the identity can write regardless of which goroutine in this
// process does the writing.
//
// Two independent things run here: a controller-runtime manager driving the notifier's watch over
// ActionRecord, and a plain HTTP server receiving Slack and Google Chat events. They share nothing
// but the manager's client and the process's one identity.
package main

import (
	"context"
	"flag"
	"fmt"
	"net/http"
	"os"
	"sync/atomic"
	"time"

	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	ctrllog "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval/gateway"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/approval/notify"
	brokercontroller "github.com/gke-labs/kube-agents/k8s-operator/internal/broker/controller"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/execute"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/rollback"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/undo"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/journal"
)

var (
	scheme   = runtime.NewScheme()
	setupLog = ctrllog.Log.WithName("setup")
)

func init() {
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	utilruntime.Must(agentv1alpha1.AddToScheme(scheme))
}

type options struct {
	namespace              string
	addr                   string
	commandAddr            string
	metricsAddr            string
	healthProbeAddr        string
	leaderElect            bool
	slackSigningSecret     string
	slackBotToken          string
	googleChatSharedSecret string
	googleChatToken        string
	deliveryStateName      string
}

func parseFlags() options {
	var o options
	flag.StringVar(&o.namespace, "namespace", envOr("NAMESPACE", "kubeagents-system"), "namespace this gateway runs in and stores delivery state in")
	flag.StringVar(&o.addr, "addr", envOr("LISTEN_ADDR", ":8443"), "address the webhook HTTP server listens on")
	flag.StringVar(&o.commandAddr, "command-bind-address", envOr("COMMAND_BIND_ADDRESS", ""), "optional dedicated address for POST /v1alpha1/commands (always also served on --addr)")
	flag.StringVar(&o.metricsAddr, "metrics-bind-address", "0", "metrics endpoint; \"0\" disables it")
	flag.StringVar(&o.healthProbeAddr, "health-probe-bind-address", ":8081", "health probe endpoint")
	flag.BoolVar(&o.leaderElect, "leader-elect", true, "enable leader election; the gateway is single-replica by design (05 §1.8's one-socket rule) and this is what makes a rollout safe rather than a race")
	flag.StringVar(&o.deliveryStateName, "delivery-state-configmap", "chatops-gateway-delivery-state", "ConfigMap name the notifier tracks delivery state in")
	flag.Parse()

	o.slackSigningSecret = os.Getenv("SLACK_SIGNING_SECRET")
	o.slackBotToken = os.Getenv("SLACK_BOT_TOKEN")
	o.googleChatSharedSecret = os.Getenv("GOOGLECHAT_SHARED_SECRET")
	o.googleChatToken = os.Getenv("GOOGLECHAT_BOT_TOKEN")
	return o
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func buildHTTPMux(dispatcher *gateway.Dispatcher, slackHandler *gateway.SlackHandler, gchatHandler *gateway.GoogleChatHandler) *http.ServeMux {
	mux := gateway.NewServeMux(slackHandler, gchatHandler)
	if dispatcher != nil {
		mux.Handle("/v1alpha1/commands", dispatcher.CommandHandler())
	}
	return mux
}

func main() {
	o := parseFlags()
	ctrllog.SetLogger(zap.New(zap.UseDevMode(false)))

	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme:                  scheme,
		Metrics:                 metricsserver.Options{BindAddress: o.metricsAddr},
		HealthProbeBindAddress:  o.healthProbeAddr,
		LeaderElection:          o.leaderElect,
		LeaderElectionID:        "kage-chatops-gateway.kubeagents.x-k8s.io",
		LeaderElectionNamespace: o.namespace,
	})
	if err != nil {
		setupLog.Error(err, "unable to start manager")
		os.Exit(1)
	}

	deliverers := notify.Deliverers{}
	if o.slackBotToken != "" {
		deliverers[notify.PlatformSlack] = &notify.SlackDeliverer{Token: o.slackBotToken}
	}
	if o.googleChatToken != "" {
		deliverers[notify.PlatformGoogleChat] = &notify.GoogleChatDeliverer{
			TokenSource: func(context.Context) (string, error) { return o.googleChatToken, nil },
		}
	}
	if len(deliverers) == 0 {
		setupLog.Info("WARNING: no chat platform credentials configured; the notifier will resolve rosters and never deliver anything")
	}

	notifier := &notify.Reconciler{
		Client:     mgr.GetClient(),
		Deliverers: deliverers,
		Store:      &notify.ConfigMapStore{Client: mgr.GetClient(), Name: o.deliveryStateName, Namespace: o.namespace},
	}
	if err := notifier.SetupWithManager(mgr); err != nil {
		setupLog.Error(err, "unable to set up the approval notifier")
		os.Exit(1)
	}

	undoCfg := rest.CopyConfig(mgr.GetConfig())
	undoCfg.Impersonate = rest.ImpersonationConfig{
		UserName: "system:serviceaccount:" + o.namespace + ":kube-agents-undo-controller",
		Groups:   []string{"system:serviceaccounts", "system:serviceaccounts:" + o.namespace, "system:authenticated"},
	}
	if undoClient, err := client.New(undoCfg, client.Options{Scheme: scheme}); err != nil {
		setupLog.Error(err, "unable to build undo controller client")
		os.Exit(1)
	} else {
		undoRec := &brokercontroller.UndoReconciler{
			Client: undoClient,
			Scheme: scheme,
			Replayer: &liveBrokerReplayer{
				baseCfg: mgr.GetConfig(),
				scheme:  scheme,
			},
			Now: time.Now,
		}
		if err := undoRec.SetupWithManager(mgr); err != nil {
			setupLog.Error(err, "unable to set up the undo reconciler")
			os.Exit(1)
		}
	}

	if err := mgr.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		setupLog.Error(err, "unable to set up health check")
		os.Exit(1)
	}
	// Leadership-gated, not a bare Ping. The dedup cache in gateway.Dispatcher and the "one socket"
	// invariant chat-approval.md §7 documents are both per-PROCESS guarantees, but a rolling update
	// otherwise runs the old and new pod at once for the whole time the new one is Ready — Kubernetes
	// removes a NotReady pod from the Service's endpoints entirely, which is what actually keeps a
	// second live writer off the wire, not the Deployment's replica count. config/chatops-gateway/
	// deployment.yaml pairs this with strategy: Recreate, because a rolling update would otherwise
	// deadlock: the new pod can't become Ready without the lease, and the old pod holds the lease
	// until Kubernetes decides to terminate it, which a RollingUpdate delays until the new pod is
	// Ready.
	elected := &atomic.Bool{}
	go func() {
		<-mgr.Elected()
		elected.Store(true)
	}()
	if err := mgr.AddReadyzCheck("readyz", func(*http.Request) error {
		if !elected.Load() {
			return fmt.Errorf("not yet the elected leader")
		}
		return nil
	}); err != nil {
		setupLog.Error(err, "unable to set up ready check")
		os.Exit(1)
	}

	dispatcher := &gateway.Dispatcher{Client: mgr.GetClient(), WaitTimeout: 15 * time.Second}
	var slackHandler *gateway.SlackHandler
	if o.slackSigningSecret != "" {
		slackHandler = &gateway.SlackHandler{Dispatcher: dispatcher, SigningSecret: o.slackSigningSecret}
	} else {
		setupLog.Info("WARNING: SLACK_SIGNING_SECRET is empty; the Slack command route is not registered")
	}
	var gchatHandler *gateway.GoogleChatHandler
	if o.googleChatSharedSecret != "" {
		gchatHandler = &gateway.GoogleChatHandler{Dispatcher: dispatcher, Verifier: gateway.SharedSecretVerifier{Token: o.googleChatSharedSecret}}
	} else {
		setupLog.Info("WARNING: GOOGLECHAT_SHARED_SECRET is empty; the Google Chat events route is not registered")
	}

	httpServer := &http.Server{
		Addr:              o.addr,
		Handler:           buildHTTPMux(dispatcher, slackHandler, gchatHandler),
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       120 * time.Second,
	}

	var commandServer *http.Server
	if o.commandAddr != "" && o.commandAddr != "0" && o.commandAddr != o.addr {
		cmdMux := http.NewServeMux()
		cmdMux.Handle("/v1alpha1/commands", dispatcher.CommandHandler())
		commandServer = &http.Server{
			Addr:              o.commandAddr,
			Handler:           cmdMux,
			ReadHeaderTimeout: 10 * time.Second,
			ReadTimeout:       30 * time.Second,
			WriteTimeout:      30 * time.Second,
			IdleTimeout:       120 * time.Second,
		}
	}

	ctx := ctrl.SetupSignalHandler()
	errCh := make(chan error, 3)
	go func() {
		setupLog.Info("starting chatops webhook server", "addr", o.addr)
		if err := httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			errCh <- fmt.Errorf("webhook server: %w", err)
			return
		}
		errCh <- nil
	}()
	if commandServer != nil {
		go func() {
			setupLog.Info("starting chatops command server", "addr", o.commandAddr)
			if err := commandServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
				errCh <- fmt.Errorf("command server: %w", err)
				return
			}
			errCh <- nil
		}()
	}
	go func() {
		setupLog.Info("starting manager (approval notifier)")
		if err := mgr.Start(ctx); err != nil {
			errCh <- fmt.Errorf("manager: %w", err)
			return
		}
		errCh <- nil
	}()

	select {
	case err := <-errCh:
		if err != nil {
			setupLog.Error(err, "exiting")
			os.Exit(1)
		}
	case <-ctx.Done():
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = httpServer.Shutdown(shutdownCtx)
	if commandServer != nil {
		_ = commandServer.Shutdown(shutdownCtx)
	}
}

type liveBrokerReplayer struct {
	baseCfg *rest.Config
	scheme  *runtime.Scheme
}

func (l *liveBrokerReplayer) Replay(ctx context.Context, ar *agentv1alpha1.ActionRecord, req *agentv1alpha1.UndoRequest) (string, error) {
	plan := undo.HydratePlanFromRecord(ar)
	if plan == nil {
		return "", fmt.Errorf("action record %s has no undo plan: %w", ar.Name, brokercontroller.ErrReplayRefused)
	}
	actorSA := fmt.Sprintf("system:serviceaccount:%s:%s", ar.Namespace, ar.Spec.ActorServiceAccount)
	cfg := rest.CopyConfig(l.baseCfg)
	cfg.Impersonate = rest.ImpersonationConfig{
		UserName: actorSA,
		Groups:   []string{"system:serviceaccounts", "system:serviceaccounts:" + ar.Namespace, "system:authenticated"},
	}
	actorClient, err := client.New(cfg, client.Options{Scheme: l.scheme})
	if err != nil {
		return "", fmt.Errorf("build actor client for %s: %w", actorSA, err)
	}
	replayer := &rollback.Replayer{
		Writer: &execute.ClientApplier{Client: actorClient},
		Reader: &execute.ClientReader{Client: actorClient},
	}
	undoID, err := journal.NewULID(time.Now().UTC())
	if err != nil {
		return "", fmt.Errorf("mint undo action id: %w", err)
	}
	if err := replayer.Rollback(ctx, ar.Spec.ActionID, ar.Spec.AgentIdentity, *plan); err != nil {
		return "", err
	}
	return undoID, nil
}

