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

package main

// waitforbroker.go — the `--wait-for-broker` mode (08 §2.4).
//
// This is the same binary in a second mode, run as an init container in the AGENT's pod rather than
// as the broker itself. It polls the broker's /healthz over mutual TLS until the broker answers or
// a bounded timeout expires, records which of those happened, and exits.
//
// # Why the broker binary and not a shell one-liner
//
// The poll is a real mTLS handshake: the broker listens with RequireAndVerifyClientCert, so
// anything that cannot present the agent's mesh certificate gets a handshake failure and cannot
// distinguish "broker is down" from "I have no certificate". That rules out a shell probe, and it
// rules out the kubelet's own httpGet probe (see the broker Deployment's tcpSocket readiness probe
// and the comment above it). The agent image is a Python harness with no guaranteed TLS-capable
// client on PATH; this binary is already built, already has the flag names, and ships with no
// shell — so the init container runs an image the node has just pulled for the sibling pod.
//
// # Why it always exits 0
//
// 08 §2.4: "On timeout it starts anyway, in observe-and-report mode — a broker outage must not
// blind the fleet, and an agent that can only read is exactly as safe as the previous generation's
// agent." A non-zero exit would put the pod in Init:CrashLoopBackOff and take the agent's READ path
// down with the write path, which is the one outcome the design rules out. So the verdict is a file
// the agent container reads, not an exit code the kubelet acts on.
//
// This is not a fail-open. The agent has no write verb of its own (03 §11); "started without a
// broker" means "cannot write" because RBAC says so, not because the agent chose to behave.

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker"
)

// The two values written to the status file. The agent harness reads this to decide whether to
// announce itself as able to execute; anything other than statusReady must be read as "cannot".
const (
	statusReady       = "ready"
	statusUnavailable = "unavailable"
)

// waitOptions is the second mode's configuration. Separate from `options` because it shares only
// the three TLS paths and validating them together would let a missing broker-side-only flag pass
// in wait mode and vice versa.
type waitOptions struct {
	endpoint string
	// san is the name the broker's certificate must carry. Pinned explicitly rather than inferred
	// from the URL host so that the check does not quietly weaken if the endpoint ever becomes an
	// IP or a short name — a certificate check against a host you did not choose is not a check.
	san      string
	timeout  time.Duration
	interval time.Duration

	statusFile   string
	certFile     string
	keyFile      string
	clientCAFile string
}

// runWaitForBroker polls and records. Its error return is for CONFIGURATION faults only — a missing
// flag, an unreadable certificate — which are the operator's bugs and should be loud. A broker that
// is simply not answering is not an error here; it is the `unavailable` verdict.
func runWaitForBroker(ctx context.Context, o waitOptions) error {
	if err := o.validate(); err != nil {
		return err
	}

	client, err := o.httpClient()
	if err != nil {
		return err
	}

	deadline := time.Now().Add(o.timeout)
	healthURL := o.endpoint + broker.HealthzPath
	attempts := 0

	for {
		attempts++
		if ok, err := probe(ctx, client, healthURL); ok {
			setupLog.Info("broker is ready", "url", healthURL, "attempts", attempts)
			return writeStatus(o.statusFile, statusReady)
		} else if err != nil {
			setupLog.V(1).Info("broker not ready yet", "url", healthURL, "attempt", attempts, "err", err.Error())
		}

		if !time.Now().Add(o.interval).Before(deadline) {
			// Timed out. Log at Info, not Error: this is a state the design has a mode for, and an
			// Error here would page someone for a degradation the CR's BrokerReady condition
			// already reports in the place an operator looks.
			setupLog.Info("broker did not become ready within the timeout; starting in observe-and-report mode",
				"url", healthURL, "timeout", o.timeout.String(), "attempts", attempts)
			return writeStatus(o.statusFile, statusUnavailable)
		}

		select {
		case <-ctx.Done():
			// SIGTERM during init. Record what we know rather than leaving the file absent, so the
			// agent container reads a definite "cannot write" instead of having to interpret a
			// missing file.
			return writeStatus(o.statusFile, statusUnavailable)
		case <-time.After(o.interval):
		}
	}
}

// probe is one mTLS GET of /healthz. A non-200 counts as not-ready rather than as an error,
// because a broker that is up but failing its own health check is exactly as unable to execute an
// envelope as one that is down.
func probe(ctx context.Context, client *http.Client, url string) (bool, error) {
	reqCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()

	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, url, nil)
	if err != nil {
		return false, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return false, err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return false, fmt.Errorf("healthz returned %d", resp.StatusCode)
	}
	return true, nil
}

// httpClient builds the mutual-TLS client. It presents the agent's mesh certificate and verifies
// the broker's against the same mesh CA, with the SAN pinned — the same two-sided check the broker
// performs in the other direction, which is what makes the connection prove both identities rather
// than one.
func (o waitOptions) httpClient() (*http.Client, error) {
	cert, err := tls.LoadX509KeyPair(o.certFile, o.keyFile)
	if err != nil {
		return nil, fmt.Errorf("load client keypair: %w", err)
	}
	caPEM, err := os.ReadFile(o.clientCAFile)
	if err != nil {
		return nil, fmt.Errorf("read CA bundle: %w", err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(caPEM) {
		return nil, fmt.Errorf("no certificates found in the CA bundle %s", o.clientCAFile)
	}
	return &http.Client{
		Transport: &http.Transport{
			TLSClientConfig: &tls.Config{
				Certificates: []tls.Certificate{cert},
				RootCAs:      pool,
				ServerName:   o.san,
				MinVersion:   tls.VersionTLS13,
			},
		},
	}, nil
}

func (o waitOptions) validate() error {
	for _, f := range []struct{ name, value string }{
		{"--broker-endpoint", o.endpoint},
		{"--broker-san", o.san},
		{"--status-file", o.statusFile},
		{"--tls-cert-file", o.certFile},
		{"--tls-key-file", o.keyFile},
		{"--client-ca-file", o.clientCAFile},
	} {
		if f.value == "" {
			return fmt.Errorf("missing required %s in --wait-for-broker mode", f.name)
		}
	}
	if o.timeout <= 0 {
		return fmt.Errorf("--wait-timeout must be positive, got %s", o.timeout)
	}
	if o.interval <= 0 {
		return fmt.Errorf("--wait-interval must be positive, got %s", o.interval)
	}
	return nil
}

// writeStatus records the verdict atomically — write a temp file in the same directory, then
// rename. The agent container polls this path, and a torn read of a half-written file could be
// neither of the two legal values; rename gives it all-or-nothing.
func writeStatus(path, verdict string) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".broker-status-")
	if err != nil {
		return fmt.Errorf("create temp status file in %s: %w", dir, err)
	}
	tmpName := tmp.Name()
	if _, err := tmp.WriteString(verdict + "\n"); err != nil {
		_ = tmp.Close()
		_ = os.Remove(tmpName)
		return fmt.Errorf("write status: %w", err)
	}
	if err := tmp.Close(); err != nil {
		_ = os.Remove(tmpName)
		return fmt.Errorf("close status: %w", err)
	}
	if err := os.Rename(tmpName, path); err != nil {
		_ = os.Remove(tmpName)
		return fmt.Errorf("rename status into place: %w", err)
	}
	return nil
}
