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

// Command kage-broker is the Action Broker (03 §4.1, 06 §4.1, 08 §2.3). It is the ONLY path by
// which an agent mutates anything: the agent container itself holds no write credentials, and this
// process holds them on its behalf, behind the envelope schema, the anti-replay rules, the risk
// classifier and the journal.
//
// It is tier-neutral. One binary and one image serve a platform agent, a cluster-admin agent and a
// developer-team agent; which one it is serving comes from its own flags -- set by the operator
// when it renders the pair -- and never from anything a caller sends. That is why `--tier` and
// `--scope` are startup configuration here rather than envelope fields: 03 §4.1 step 1 derives
// (tier, scope) from the authenticated identity, and a value the caller could supply would be an
// authority claim wearing the shape of a parameter.
//
// # What this process deliberately does not have
//
// One listening port and one mutating route (V-BRK-021). No metrics listener, no pprof, no admin
// socket, no second Service. Each of those would be a door, and the non-skippability argument for
// the broker is not "the pipeline checks everything" -- it is "there is nowhere else to go".
//
// The image it ships in has no shell (V-RUN-010). `kubectl exec` into this pod gets you a failed
// exec, not a prompt, which matters because this is the one pod in the mesh whose ServiceAccount
// can write.
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"strconv"
	"time"

	// NO `_ "k8s.io/client-go/plugin/pkg/client/auth"` here. The kubebuilder scaffold puts that blank
	// import in every command it generates, and it is a PLUGIN LOADER: it exists to register
	// out-of-process credential providers -- OIDC, and historically the cloud ones -- so that a
	// kubeconfig may name a binary for the client to fork. The broker authenticates one way, with the
	// projected token the kubelet mounts, and 08 §2.1 puts it on the smallest possible supply chain
	// precisely because it is the one pod in the mesh whose ServiceAccount can write. Registering
	// providers it will never use widens the runtime for nothing. Removed in P9-T9b-3 and kept out by
	// V-RUN-010 (`dev/tests/broker-supply-chain-minimal.py`), which fails on any blank import here.

	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	"k8s.io/client-go/kubernetes"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"

	// The narrow subpackages, NOT the `ctrl "sigs.k8s.io/controller-runtime"` convenience alias. The
	// alias is a facade over `pkg/manager`, `pkg/builder` and `pkg/controller` -- an entire
	// controller runtime, in a process that runs no controller -- and `pkg/manager` imports
	// `net/http/pprof`, whose package init registers /debug/pprof on http.DefaultServeMux. Nothing in
	// this process serves DefaultServeMux, so the handlers were unreachable; they were also linked
	// into the one image 08 §2.6 hardens hardest, one `http.ListenAndServe(addr, nil)` from being a
	// second door next to the write credential. The doc comment above has claimed "no pprof" since
	// this file was written; until P9-T9b-3 the binary disagreed with it. Four call sites, no
	// behaviour change, and V-RUN-010 keeps the alias out.
	"sigs.k8s.io/controller-runtime/pkg/client"
	ctrlconfig "sigs.k8s.io/controller-runtime/pkg/client/config"
	ctrllog "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	"sigs.k8s.io/controller-runtime/pkg/manager/signals"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/pipeline"
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

// options is every knob this process has. Collected in one struct so that `validate` can be a
// single function that runs before anything is dialled -- a broker that starts and then discovers
// it has no client CA is a broker that spent some seconds accepting connections it could not
// authenticate.
type options struct {
	agentName   string
	tier        string
	scope       string
	namespace   string
	readerSA    string
	trustDomain string

	certFile     string
	keyFile      string
	clientCAFile string

	shutdownGrace time.Duration
}

func main() {
	var o options
	// Every flag has an env fallback because the operator renders a Deployment, and a value that
	// can only be a flag has to be spelled into an argv array where a typo is invisible until the
	// pod crash-loops.
	flag.StringVar(&o.agentName, "agent-name", os.Getenv("KAGE_AGENT_NAME"),
		"The Agent CR this broker serves. Env: KAGE_AGENT_NAME.")
	flag.StringVar(&o.tier, "tier", os.Getenv("KAGE_AGENT_TIER"),
		"platform, cluster-admin or developer-team. Env: KAGE_AGENT_TIER.")
	flag.StringVar(&o.scope, "scope", os.Getenv("KAGE_AGENT_SCOPE"),
		"The agent's scope leaf: project for platform, cluster for cluster-admin, namespace for developer-team. Env: KAGE_AGENT_SCOPE.")
	flag.StringVar(&o.namespace, "namespace", os.Getenv("KAGE_NAMESPACE"),
		"The namespace this broker and its agent run in, and where ActionRecords are written. Env: KAGE_NAMESPACE.")
	flag.StringVar(&o.readerSA, "reader-service-account", os.Getenv("KAGE_READER_SERVICE_ACCOUNT"),
		"The ONE ServiceAccount permitted to submit actions here. Env: KAGE_READER_SERVICE_ACCOUNT.")
	flag.StringVar(&o.trustDomain, "trust-domain", envOr("KAGE_TRUST_DOMAIN", broker.DefaultTrustDomain),
		"SPIFFE trust domain the client certificate must belong to. Empty disables the certificate-to-token binding. Env: KAGE_TRUST_DOMAIN.")
	flag.StringVar(&o.certFile, "tls-cert-file", envOr("KAGE_TLS_CERT_FILE", "/etc/kage/tls/tls.crt"),
		"Server certificate. Env: KAGE_TLS_CERT_FILE.")
	flag.StringVar(&o.keyFile, "tls-key-file", envOr("KAGE_TLS_KEY_FILE", "/etc/kage/tls/tls.key"),
		"Server private key. Env: KAGE_TLS_KEY_FILE.")
	flag.StringVar(&o.clientCAFile, "client-ca-file", envOr("KAGE_CLIENT_CA_FILE", "/etc/kage/tls/ca.crt"),
		"CA bundle that client certificates are verified against. Env: KAGE_CLIENT_CA_FILE.")
	flag.DurationVar(&o.shutdownGrace, "shutdown-grace", 20*time.Second,
		"How long in-flight submissions have to finish after SIGTERM.")

	// --wait-for-broker runs this binary as the agent pod's init container instead of as the
	// broker (08 §2.4). See waitforbroker.go for why the probe needs this binary and why it
	// always exits 0. The three TLS flags above are shared; in this mode they name the AGENT's
	// half of the mesh keypair rather than the broker's.
	var w waitOptions
	var waitMode bool
	flag.BoolVar(&waitMode, "wait-for-broker", false,
		"Run as the agent pod's init container: poll the broker's /healthz over mTLS, record the verdict, exit 0.")
	flag.StringVar(&w.endpoint, "broker-endpoint", "",
		"Broker base URL to poll. Only used with --wait-for-broker.")
	flag.StringVar(&w.san, "broker-san", "",
		"DNS name the broker's certificate must carry. Only used with --wait-for-broker.")
	flag.DurationVar(&w.timeout, "wait-timeout", 120*time.Second,
		"How long to wait for the broker before starting in observe-and-report mode.")
	flag.DurationVar(&w.interval, "wait-interval", 2*time.Second,
		"Delay between /healthz polls.")
	flag.StringVar(&w.statusFile, "status-file", "",
		"Path to write the readiness verdict to. Only used with --wait-for-broker.")

	opts := zap.Options{Development: true}
	opts.BindFlags(flag.CommandLine)
	flag.Parse()
	ctrllog.SetLogger(zap.New(zap.UseFlagOptions(&opts)))

	if waitMode {
		w.certFile, w.keyFile, w.clientCAFile = o.certFile, o.keyFile, o.clientCAFile
		if err := runWaitForBroker(signals.SetupSignalHandler(), w); err != nil {
			// A configuration fault, not an unready broker — see runWaitForBroker. Exiting
			// non-zero here is correct precisely because it is NOT the timeout path.
			setupLog.Error(err, "wait-for-broker could not run")
			os.Exit(1)
		}
		return
	}

	if err := run(signals.SetupSignalHandler(), o); err != nil {
		setupLog.Error(err, "broker exited with error")
		os.Exit(1)
	}
}

func run(ctx context.Context, o options) error {
	tier, err := o.validate()
	if err != nil {
		return err
	}

	cfg, err := ctrlconfig.GetConfig()
	if err != nil {
		return fmt.Errorf("load kubeconfig: %w", err)
	}
	clientset, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		return fmt.Errorf("build clientset for TokenReview: %w", err)
	}
	// A direct client, not a cached one. The broker's reads are its own writes read back, and a
	// cache would answer "did the record land?" from a watch that may not have caught up -- which
	// is the one question where a stale yes is worse than a slow no.
	k8s, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		return fmt.Errorf("build API client: %w", err)
	}

	security := broker.LogSecuritySink{Log: ctrllog.Log.WithName("security")}

	// ONE store, shared by the pipeline's step 11 and by the rejection journal below. A second one
	// would be a second answer to "which namespace do records live in".
	records := journal.NewStore(k8s, nil)

	// Steps 3-11. See wiring.go for the assembly and for what is deliberately left nil.
	pipeCfg, sources, err := pipelineConfig(ctx, brokerDeps{
		RESTConfig:          cfg,
		Client:              k8s,
		Records:             records,
		AgentName:           o.agentName,
		Namespace:           o.namespace,
		ActorServiceAccount: brokerServiceAccount(),
	})
	if err != nil {
		return err
	}
	pipe, err := pipeline.New(pipeCfg)
	if err != nil {
		return err
	}
	// Before the listener opens. A broker that is accepting submissions while its brake has never
	// been read is a broker whose first few actions were decided by a source that had nothing in
	// it -- and startSources exits non-zero rather than degrading, for the reasons stated there.
	if err := startSources(ctx, sources); err != nil {
		return err
	}

	server, err := broker.NewServer(broker.Config{
		Authenticator: &broker.Authenticator{
			Reviewer: broker.APITokenReviewer{Client: clientset},
			Expected: broker.ExpectedCaller{
				Namespace:      o.namespace,
				ServiceAccount: o.readerSA,
				AgentName:      o.agentName,
				Tier:           tier,
				Scope:          o.scope,
			},
			TrustDomain: o.trustDomain,
			Security:    security,
		},
		Guard: broker.NewReplayGuard(time.Now),
		// The real pipeline, not broker.UnavailablePipeline. Until P9-T7c-3d-iv-b this line held the
		// 503 stub, and every envelope that survived auth, the schema, the key recomputation and the
		// three anti-replay mechanisms was refused by a broker whose steps 3-11 existed only in their
		// own tests -- LSN-007 exactly. V-BRK-027 is the assertion that keeps it wired.
		Pipeline: pipe,
		Journal: &broker.StoreRejectionJournal{
			Store:               records,
			Namespace:           o.namespace,
			AgentName:           o.agentName,
			ActorServiceAccount: brokerServiceAccount(),
		},
		Security:  security,
		Log:       ctrllog.Log.WithName("broker"),
		Namespace: o.namespace,
	})
	if err != nil {
		return err
	}

	tlsConfig, err := o.tlsConfig()
	if err != nil {
		return err
	}
	if o.trustDomain == "" {
		// Loud, because it removes the binding between the certificate and the token: with no trust
		// domain the broker still requires a client certificate, but stops checking that the
		// certificate names the same workload the token does.
		setupLog.Info("WARNING: --trust-domain is empty; the certificate-to-token binding is disabled and only a mesh sidecar that has already verified the peer makes that safe")
	}

	// ONE listener. Not a metrics listener alongside it, not a debug listener behind a flag: the
	// route inventory in V-BRK-021 is only meaningful if this is the whole surface.
	addr := net.JoinHostPort("", strconv.Itoa(broker.Port))
	httpServer := &http.Server{
		Addr:      addr,
		Handler:   server,
		TLSConfig: tlsConfig,
		// Bounded, because the broker is the one process in the pod that must stay up: an idle
		// half-open connection per attempt is a denial of service that needs no exploit.
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      60 * time.Second,
		IdleTimeout:       120 * time.Second,
		ErrorLog:          nil,
	}

	setupLog.Info("starting kage-broker",
		"agent", o.agentName, "tier", o.tier, "scope", o.scope, "namespace", o.namespace,
		"reader", o.readerSA, "addr", addr, "routes", server.Routes(), "mutating", server.MutatingRoutes(),
		"audience", broker.TokenAudience)

	errCh := make(chan error, 1)
	go func() {
		// Certificates and key come from TLSConfig, so both arguments are empty.
		if err := httpServer.ListenAndServeTLS("", ""); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errCh <- err
			return
		}
		errCh <- nil
	}()

	select {
	case err := <-errCh:
		return err
	case <-ctx.Done():
	}

	setupLog.Info("shutting down", "grace", o.shutdownGrace)
	shutdownCtx, cancel := context.WithTimeout(context.Background(), o.shutdownGrace)
	defer cancel()
	if err := httpServer.Shutdown(shutdownCtx); err != nil {
		return fmt.Errorf("graceful shutdown: %w", err)
	}
	return <-errCh
}

// validate refuses to start on anything missing. Every one of these is load-bearing, and a default
// for any of them would be a broker serving an agent it was not deployed for.
func (o *options) validate() (agentv1alpha1.AgentTier, error) {
	for _, f := range []struct{ name, value string }{
		{"--agent-name / KAGE_AGENT_NAME", o.agentName},
		{"--tier / KAGE_AGENT_TIER", o.tier},
		{"--scope / KAGE_AGENT_SCOPE", o.scope},
		{"--namespace / KAGE_NAMESPACE", o.namespace},
		{"--reader-service-account / KAGE_READER_SERVICE_ACCOUNT", o.readerSA},
		{"--tls-cert-file", o.certFile},
		{"--tls-key-file", o.keyFile},
		{"--client-ca-file", o.clientCAFile},
	} {
		if f.value == "" {
			return "", fmt.Errorf("missing required %s", f.name)
		}
	}

	tier := agentv1alpha1.AgentTier(o.tier)
	switch tier {
	case agentv1alpha1.TierPlatform, agentv1alpha1.TierClusterAdmin, agentv1alpha1.TierDeveloperTeam:
	default:
		return "", fmt.Errorf("--tier must be %s, %s or %s, got %q",
			agentv1alpha1.TierPlatform, agentv1alpha1.TierClusterAdmin, agentv1alpha1.TierDeveloperTeam, o.tier)
	}
	return tier, nil
}

// tlsConfig builds the mutual-TLS configuration.
//
// RequireAndVerifyClientCert, not VerifyClientCertIfGiven. The difference is the whole of V-BRK-007:
// with the permissive setting a caller that presents no certificate at all still gets a TLS
// connection, and the broker's own transport check becomes the only thing standing between an
// anonymous peer and the actions route. Refusing in the handshake means the request never becomes
// a request.
func (o *options) tlsConfig() (*tls.Config, error) {
	cert, err := tls.LoadX509KeyPair(o.certFile, o.keyFile)
	if err != nil {
		return nil, fmt.Errorf("load server keypair: %w", err)
	}
	caPEM, err := os.ReadFile(o.clientCAFile)
	if err != nil {
		return nil, fmt.Errorf("read client CA bundle: %w", err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(caPEM) {
		// An empty pool with RequireAndVerifyClientCert would refuse everything, which fails safe --
		// but it would fail safe as a mysterious handshake error on every request rather than as a
		// startup message naming the file.
		return nil, fmt.Errorf("no certificates found in the client CA bundle %s", o.clientCAFile)
	}
	return &tls.Config{
		Certificates: []tls.Certificate{cert},
		ClientAuth:   tls.RequireAndVerifyClientCert,
		ClientCAs:    pool,
		MinVersion:   tls.VersionTLS13,
	}, nil
}

// brokerServiceAccount reports the broker's own write identity for the journal's `actor` field.
// Recorded so a record says who COULD have written, distinct from who asked.
func brokerServiceAccount() string {
	if v := os.Getenv("KAGE_BROKER_SERVICE_ACCOUNT"); v != "" {
		return v
	}
	return "kage-broker"
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
