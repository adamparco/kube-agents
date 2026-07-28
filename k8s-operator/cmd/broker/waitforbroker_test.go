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

// waitforbroker_test.go — V-RUN-003's runtime half.
//
// The render half (the init container is first, and its args carry a status file) is asserted in
// internal/controller/broker_manifests_test.go. What is asserted here is the property that makes
// that init container safe to put in front of every agent pod: 08 §2.4's observe-and-report
// contract. Three things have to hold together, and any one of them alone is the wrong behaviour:
//
//  1. A broker that never answers must NOT fail the pod. A non-zero exit is Init:CrashLoopBackOff,
//     which takes the agent's read path down with its write path — the outcome 08 §2.4 rules out.
//  2. It must still record `unavailable`. Exiting 0 with no verdict is the actual fail-open: the
//     agent would find no file and have to guess, and the safe guess is not the obvious one.
//  3. A CONFIGURATION fault must be loud. If the operator wired a missing cert path, "unavailable"
//     would be a lie that reads as a broker outage, and the pod would run degraded forever with
//     nothing pointing at the real cause.

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"fmt"
	"math/big"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker"
)

const testSAN = "agent-broker.kubeagents-system.svc.cluster.local"

func TestRunWaitForBrokerTimesOutIntoObserveAndReport(t *testing.T) {
	dir := t.TempDir()
	mesh := newMeshCA(t)
	o := mesh.waitOptions(t, dir, "https://127.0.0.1:1/unreachable")
	o.timeout = 150 * time.Millisecond
	o.interval = 25 * time.Millisecond

	start := time.Now()
	if err := runWaitForBroker(context.Background(), o); err != nil {
		t.Fatalf("a broker that never answers must not be an error (08 §2.4): %v", err)
	}
	if elapsed := time.Since(start); elapsed > 5*time.Second {
		t.Fatalf("waited %s; the timeout is meant to bound this", elapsed)
	}
	if got := readStatus(t, o.statusFile); got != statusUnavailable {
		t.Fatalf("status = %q, want %q — exiting 0 with no verdict is the fail-open", got, statusUnavailable)
	}
}

func TestRunWaitForBrokerRecordsReady(t *testing.T) {
	dir := t.TempDir()
	mesh := newMeshCA(t)
	endpoint := mesh.serveHealthz(t, http.StatusOK)

	o := mesh.waitOptions(t, dir, endpoint)
	if err := runWaitForBroker(context.Background(), o); err != nil {
		t.Fatalf("runWaitForBroker: %v", err)
	}
	if got := readStatus(t, o.statusFile); got != statusReady {
		t.Fatalf("status = %q, want %q", got, statusReady)
	}
}

// A broker that is up but failing its own health check cannot execute an envelope either, so it is
// `unavailable` and not `ready`. Without this the probe would be a liveness check on the TCP port,
// which the kubelet already does and which is not the question being asked.
func TestRunWaitForBrokerTreatsUnhealthyAsUnavailable(t *testing.T) {
	dir := t.TempDir()
	mesh := newMeshCA(t)
	endpoint := mesh.serveHealthz(t, http.StatusServiceUnavailable)

	o := mesh.waitOptions(t, dir, endpoint)
	o.timeout = 150 * time.Millisecond
	o.interval = 25 * time.Millisecond

	if err := runWaitForBroker(context.Background(), o); err != nil {
		t.Fatalf("runWaitForBroker: %v", err)
	}
	if got := readStatus(t, o.statusFile); got != statusUnavailable {
		t.Fatalf("status = %q, want %q", got, statusUnavailable)
	}
}

// Cancellation mid-wait is SIGTERM during init. It still has to leave a definite verdict rather
// than an absent file, for the same reason as the timeout path.
func TestRunWaitForBrokerRecordsAVerdictOnCancellation(t *testing.T) {
	dir := t.TempDir()
	mesh := newMeshCA(t)
	o := mesh.waitOptions(t, dir, "https://127.0.0.1:1/unreachable")
	o.timeout = time.Hour
	o.interval = 10 * time.Millisecond

	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()

	if err := runWaitForBroker(ctx, o); err != nil {
		t.Fatalf("cancellation is not a configuration fault: %v", err)
	}
	if got := readStatus(t, o.statusFile); got != statusUnavailable {
		t.Fatalf("status = %q, want %q", got, statusUnavailable)
	}
}

func TestRunWaitForBrokerRejectsMisconfigurationLoudly(t *testing.T) {
	dir := t.TempDir()
	mesh := newMeshCA(t)

	cases := map[string]func(o *waitOptions){
		"no endpoint":       func(o *waitOptions) { o.endpoint = "" },
		"no SAN":            func(o *waitOptions) { o.san = "" },
		"no status file":    func(o *waitOptions) { o.statusFile = "" },
		"no cert":           func(o *waitOptions) { o.certFile = "" },
		"no key":            func(o *waitOptions) { o.keyFile = "" },
		"no CA":             func(o *waitOptions) { o.clientCAFile = "" },
		"zero timeout":      func(o *waitOptions) { o.timeout = 0 },
		"zero interval":     func(o *waitOptions) { o.interval = 0 },
		"unreadable cert":   func(o *waitOptions) { o.certFile = filepath.Join(dir, "absent.crt") },
		"CA with no certs":  func(o *waitOptions) { o.clientCAFile = writeFile(t, dir, "empty-ca.pem", "not a certificate\n") },
		"unreadable CA":     func(o *waitOptions) { o.clientCAFile = filepath.Join(dir, "absent-ca.pem") },
		"key is not a pair": func(o *waitOptions) { o.keyFile = writeFile(t, dir, "junk.key", "not a key\n") },
	}

	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			statusDir := t.TempDir()
			o := mesh.waitOptions(t, statusDir, "https://127.0.0.1:1/unreachable")
			o.timeout = 50 * time.Millisecond
			o.interval = 10 * time.Millisecond
			mutate(&o)

			if err := runWaitForBroker(context.Background(), o); err == nil {
				t.Fatal("a configuration fault must be loud; got nil error")
			}
			// And it must NOT have written a verdict: `unavailable` here would read as a broker
			// outage and send whoever debugs it at the wrong half of the pair.
			if _, err := os.Stat(filepath.Join(statusDir, "broker-status")); !os.IsNotExist(err) {
				t.Fatalf("a configuration fault wrote a status verdict (stat err = %v)", err)
			}
		})
	}
}

// The agent container polls this path while it is being written. Rename gives it all-or-nothing;
// this asserts the overwrite case, which is the one a plain os.WriteFile would tear.
func TestWriteStatusOverwritesAtomically(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "broker-status")

	if err := writeStatus(path, statusUnavailable); err != nil {
		t.Fatalf("writeStatus: %v", err)
	}
	if err := writeStatus(path, statusReady); err != nil {
		t.Fatalf("writeStatus (overwrite): %v", err)
	}
	if got := readStatus(t, path); got != statusReady {
		t.Fatalf("status = %q, want %q", got, statusReady)
	}

	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 {
		names := make([]string, 0, len(entries))
		for _, e := range entries {
			names = append(names, e.Name())
		}
		t.Fatalf("left temp files behind: %v", names)
	}
}

// --- test mesh ------------------------------------------------------------------------------

// meshCA is one CA signing both ends, which is the arrangement 08 §2.3 requires and the one a
// per-certificate self-signed issuer does NOT produce. Building it that way here means these tests
// fail if the client ever stops verifying the server, or stops presenting its own certificate.
type meshCA struct {
	caPEM    []byte
	caCert   *x509.Certificate
	caKey    *ecdsa.PrivateKey
	certFile string
	keyFile  string
	caFile   string
}

func newMeshCA(t *testing.T) *meshCA {
	t.Helper()
	dir := t.TempDir()

	caKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "kube-agents mesh CA (test)"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(time.Hour),
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &caKey.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	caCert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	caPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})

	m := &meshCA{caPEM: caPEM, caCert: caCert, caKey: caKey}
	m.caFile = writeFile(t, dir, "mesh-ca.pem", string(caPEM))
	certPEM, keyPEM := m.issue(t, "agent-reader", nil)
	m.certFile = writeFile(t, dir, "tls.crt", string(certPEM))
	m.keyFile = writeFile(t, dir, "tls.key", string(keyPEM))
	return m
}

func (m *meshCA) issue(t *testing.T, cn string, dnsNames []string) (certPEM, keyPEM []byte) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(time.Now().UnixNano()),
		Subject:      pkix.Name{CommonName: cn},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature | x509.KeyUsageKeyEncipherment,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth, x509.ExtKeyUsageClientAuth},
		DNSNames:     dnsNames,
		IPAddresses:  []net.IP{net.ParseIP("127.0.0.1")},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, m.caCert, &key.PublicKey, m.caKey)
	if err != nil {
		t.Fatal(err)
	}
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}),
		pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER})
}

// serveHealthz stands up an mTLS listener answering broker.HealthzPath with the given status, and
// returns its endpoint. The listener requires and verifies a client certificate, exactly as the
// real broker does — so a client that stopped presenting one would fail these tests rather than
// quietly downgrade to server-only TLS.
func (m *meshCA) serveHealthz(t *testing.T, status int) string {
	t.Helper()
	certPEM, keyPEM := m.issue(t, testSAN, []string{testSAN})
	cert, err := tls.X509KeyPair(certPEM, keyPEM)
	if err != nil {
		t.Fatal(err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(m.caPEM) {
		t.Fatal("test CA did not parse")
	}

	mux := http.NewServeMux()
	mux.HandleFunc(broker.HealthzPath, func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(status) })

	ln, err := tls.Listen("tcp", "127.0.0.1:0", &tls.Config{
		Certificates: []tls.Certificate{cert},
		ClientCAs:    pool,
		ClientAuth:   tls.RequireAndVerifyClientCert,
		MinVersion:   tls.VersionTLS13,
	})
	if err != nil {
		t.Fatal(err)
	}
	srv := &http.Server{Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() { _ = srv.Serve(ln) }()
	t.Cleanup(func() { _ = srv.Close() })

	return fmt.Sprintf("https://%s", ln.Addr().String())
}

func (m *meshCA) waitOptions(t *testing.T, statusDir, endpoint string) waitOptions {
	t.Helper()
	return waitOptions{
		endpoint: endpoint,
		// Pinned to the loopback name the test certificate carries as an IP SAN, not to the URL
		// host: the point of the field is that it is chosen, not inferred.
		san:          "127.0.0.1",
		timeout:      5 * time.Second,
		interval:     20 * time.Millisecond,
		statusFile:   filepath.Join(statusDir, "broker-status"),
		certFile:     m.certFile,
		keyFile:      m.keyFile,
		clientCAFile: m.caFile,
	}
}

func writeFile(t *testing.T, dir, name, content string) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func readStatus(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("no verdict written: %v", err)
	}
	return strings.TrimSpace(string(b))
}
