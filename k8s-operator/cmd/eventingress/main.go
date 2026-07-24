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

// Command eventingress is the deferrable cloud-push relay (Phase 4 D1, 04 §4). It delivers non-chat
// machine push — alerts (Cloud Monitoring / Alertmanager over Pub/Sub) and GitHub webhooks — to the
// agent's LOCAL session-inject seam (127.0.0.1:8699 after S1), reusing the exact bearer + owner +
// kind-discriminated envelope the k8s-event-watcher sidecar already speaks. It runs as a per-pod
// sidecar so delivery is a same-pod loopback call (invariant 3: no cross-tier network path).
//
// Two source modes:
//
//   - --source=pubsub    Drain PRE-CREATED alert / GitHub subscriptions (subscribe-only; never
//     publishes). This is the cloud transport, exercised on scratch GKE.
//   - --source=synthetic Read a single already-normalized {kind:...} event from --event-file and deliver
//     it once through the SAME relay, then exit 0. This is the Kind terminus (D1):
//     it proves the in-pod delivery path hermetically, with no Pub/Sub, without
//     faking the relay.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"github.com/go-logr/logr"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/eventingress"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/eventingress/pubsubsource"
)

type flags struct {
	source    string
	daemonURL string
	tokenEnv  string
	owner     string
	project   string
	alertSub  string
	githubSub string
	eventFile string
}

func parseFlags(args []string) (*flags, error) {
	fs := flag.NewFlagSet("eventingress", flag.ContinueOnError)
	f := &flags{}
	fs.StringVar(&f.source, "source", "pubsub", "Event source: pubsub (drain cloud subscriptions) or synthetic (deliver one --event-file then exit).")
	fs.StringVar(&f.daemonURL, "daemon-url", "http://127.0.0.1:8699", "Base URL of the local session-inject seam (no trailing slash). Loopback in-pod.")
	fs.StringVar(&f.tokenEnv, "token-env", "API_SERVER_KEY", "Env var name holding the seam bearer token (S1).")
	fs.StringVar(&f.owner, "owner", "", "X-Asserted-Caller owner claim (the agent's tier). Must match the seam's allowed owners (S1).")
	fs.StringVar(&f.project, "project", "", "GCP project for Pub/Sub (pubsub source). Honors PUBSUB_EMULATOR_HOST for tests.")
	fs.StringVar(&f.alertSub, "alert-subscription", "", "Pre-created Pub/Sub subscription ID for alerts (pubsub source). Empty = disabled.")
	fs.StringVar(&f.githubSub, "github-subscription", "", "Pre-created Pub/Sub subscription ID for GitHub webhooks (pubsub source). Empty = disabled.")
	fs.StringVar(&f.eventFile, "event-file", "", "Path to a single normalized {kind:...} JSON event (synthetic source).")
	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	return f, nil
}

func (f *flags) validate() error {
	switch f.source {
	case "synthetic":
		if f.eventFile == "" {
			return errors.New("--event-file is required when --source=synthetic")
		}
	case "pubsub":
		if f.project == "" {
			return errors.New("--project is required when --source=pubsub")
		}
		if f.alertSub == "" && f.githubSub == "" {
			return errors.New("at least one of --alert-subscription / --github-subscription is required when --source=pubsub")
		}
	default:
		return fmt.Errorf("--source must be pubsub or synthetic (got %q)", f.source)
	}
	return nil
}

func main() {
	f, err := parseFlags(os.Args[1:])
	if err != nil {
		// flag.ContinueOnError already printed usage.
		os.Exit(2)
	}
	if err := f.validate(); err != nil {
		fmt.Fprintln(os.Stderr, "eventingress:", err)
		os.Exit(2)
	}

	log := zap.New(zap.UseDevMode(true))

	token := os.Getenv(f.tokenEnv)
	relay, err := eventingress.NewRelay(eventingress.RelayConfig{
		DaemonURL:      f.daemonURL,
		BearerToken:    token,
		AssertedCaller: f.owner,
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, "eventingress:", err)
		os.Exit(1)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	switch f.source {
	case "synthetic":
		if err := runSynthetic(ctx, relay, f.eventFile); err != nil {
			fmt.Fprintln(os.Stderr, "eventingress:", err)
			os.Exit(1)
		}
	case "pubsub":
		if err := runPubsub(ctx, log, relay, f); err != nil {
			fmt.Fprintln(os.Stderr, "eventingress:", err)
			os.Exit(1)
		}
	}
}

// runSynthetic delivers one pre-normalized event and exits — the Kind terminus.
func runSynthetic(ctx context.Context, relay *eventingress.Relay, path string) error {
	raw, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("read event file %q: %w", path, err)
	}
	event, err := eventingress.ParseSyntheticEvent(raw)
	if err != nil {
		return err
	}
	sid, err := relay.Deliver(ctx, event)
	if err != nil {
		return err
	}
	fmt.Printf("delivered %v event to session %s\n", event["kind"], sid)
	return nil
}

// runPubsub starts the configured subscription sources and blocks until the context is cancelled. Each
// source runs its own Receive loop; the first non-cancel error from any source stops the process.
func runPubsub(ctx context.Context, log logr.Logger, relay *eventingress.Relay, f *flags) error {
	var sources []*pubsubsource.Source
	if f.alertSub != "" {
		s, err := pubsubsource.New(ctx, f.project, f.alertSub, pubsubsource.AlertKind, relay, log.WithName("alert"))
		if err != nil {
			return err
		}
		sources = append(sources, s)
	}
	if f.githubSub != "" {
		s, err := pubsubsource.New(ctx, f.project, f.githubSub, pubsubsource.GitHubKind, relay, log.WithName("github"))
		if err != nil {
			return err
		}
		sources = append(sources, s)
	}
	defer func() {
		for _, s := range sources {
			_ = s.Close()
		}
	}()

	errCh := make(chan error, len(sources))
	for _, s := range sources {
		go func(src *pubsubsource.Source) { errCh <- src.Start(ctx) }(s)
	}
	for range sources {
		if err := <-errCh; err != nil {
			return err
		}
	}
	return nil
}
