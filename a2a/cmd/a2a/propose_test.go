package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"strings"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/capability"
)

func startJetStreamNATS(t *testing.T) (string, *nats.Conn, jetstream.JetStream) {
	t.Helper()
	opts := &natsserver.Options{
		Host:      "127.0.0.1",
		Port:      -1,
		NoLog:     true,
		NoSigs:    true,
		JetStream: true,
		StoreDir:  t.TempDir(),
	}
	s, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatalf("new nats server: %v", err)
	}
	go s.Start()
	if !s.ReadyForConnections(5 * time.Second) {
		t.Fatal("nats server not ready")
	}
	t.Cleanup(s.Shutdown)

	nc, err := nats.Connect(s.ClientURL())
	if err != nil {
		t.Fatalf("connect nats: %v", err)
	}
	t.Cleanup(nc.Close)

	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("jetstream.New: %v", err)
	}
	return s.ClientURL(), nc, js
}

func TestRunPropose_MintsHopCapabilityAndAwaitsBrokerEvent(t *testing.T) {
	url, nc, js := startJetStreamNATS(t)
	ctx := context.Background()

	if _, err := js.CreateKeyValue(ctx, jetstream.KeyValueConfig{
		Bucket:  capability.Bucket,
		History: 1,
	}); err != nil {
		t.Fatalf("CreateKeyValue: %v", err)
	}

	// Mint a root capability delegated to sess-1 with verified requester slack:U0123ABC.
	minter := capability.NewMinter(js)
	rootRef, err := minter.Mint(ctx, "task-123", capability.Entry{
		Tier:      capability.TierDeveloperTeam,
		Scope:     capability.Scope("namespace/prod"),
		Delegate:  "sess-1",
		Requester: "slack:U0123ABC",
	})
	if err != nil {
		t.Fatalf("Mint: %v", err)
	}

	store, err := capability.NewStore(ctx, js)
	if err != nil {
		t.Fatalf("capability.NewStore: %v", err)
	}

	// Start the real capability verification service on the bus.
	svc := &capability.Service{
		Resolver: &capability.Resolver{Store: store},
	}
	sub, err := svc.Subscribe(ctx, nc)
	if err != nil {
		t.Fatalf("svc.Subscribe: %v", err)
	}
	defer func() { _ = sub.Unsubscribe() }()

	brokerVerifierClient, err := capability.NewClient(nc, "broker")
	if err != nil {
		t.Fatalf("capability.NewClient: %v", err)
	}

	rawEnvelope := `{"apiVersion":"kubeagents.x-k8s.io/v1alpha1","kind":"ActionEnvelope","intent":"scale web to 3","operations":[{"op":"scale","target":{"group":"apps","version":"v1","kind":"Deployment","namespace":"prod","name":"web"},"scale":{"replicas":3}}]}`
	canonEnv, err := jcsCanonicalize([]byte(rawEnvelope))
	if err != nil {
		t.Fatalf("jcsCanonicalize: %v", err)
	}
	wantSum := sha256.Sum256(canonEnv)
	wantSHA := hex.EncodeToString(wantSum[:])

	var seenKeys []string
	_, err = nc.Subscribe("a2a.broker.propose.prod.sre", func(m *nats.Msg) {
		var prop proposeMessage
		if err := json.Unmarshal(m.Data, &prop); err != nil {
			t.Errorf("unmarshal proposeMessage: %v", err)
			return
		}
		seenKeys = append(seenKeys, prop.CapabilityRef.Key)
		resp, err := brokerVerifierClient.Verify(ctx, prop.CapabilityRef, capability.VerbBrokerPropose, "namespace/prod")
		if err != nil {
			t.Errorf("broker verify failed: %v", err)
			return
		}
		if resp.Requester != "slack:U0123ABC" {
			t.Errorf("got requester %q, want slack:U0123ABC", resp.Requester)
		}
		if resp.EnvelopeSHA256 != wantSHA {
			t.Errorf("got envelopeSha256 %q, want %q", resp.EnvelopeSHA256, wantSHA)
		}

		evBytes, _ := json.Marshal(proposeEvent{
			ActionID:   "01JQZK0000ACTION1",
			RecordName: "action-01jqzk0000action1",
			Namespace:  "prod",
			Phase:      "Verified",
			Decision:   "accepted",
			RiskClass:  "routine",
			Message:    "scaled web to 3",
		})
		_ = nc.Publish("a2a.broker.events.prod.01JQZK0000ACTION1", evBytes)
	})
	if err != nil {
		t.Fatalf("subscribe broker propose: %v", err)
	}
	_ = nc.Flush()

	t.Setenv("NATS_URL", url)
	t.Setenv("A2A_BUS_USER", "sess-1")
	t.Setenv("A2A_SESSION", "sess-1")
	t.Setenv("A2A_NAMESPACE", "prod")
	t.Setenv("A2A_AGENT_NAME", "sre")
	t.Setenv("A2A_CAPABILITY_KEY", rootRef.Key)
	t.Setenv("A2A_CAPABILITY_REVISION", "1")
	t.Setenv("A2A_AUTHORITY_TIER", "developer-team")
	t.Setenv("A2A_AUTHORITY_SCOPE", "namespace/prod")
	t.Setenv("A2A_TASK_ID", "task-123")

	// First invocation mints hop.sess-1.1.
	var out1 bytes.Buffer
	if err := runPropose([]string{"--file", "-"}, strings.NewReader(rawEnvelope), &out1); err != nil {
		t.Fatalf("runPropose #1: %v", err)
	}
	var gotEv proposeEvent
	if err := json.Unmarshal(out1.Bytes(), &gotEv); err != nil {
		t.Fatalf("unmarshal stdout JSON: %v (raw: %q)", err, out1.String())
	}
	if gotEv.Decision != "accepted" || gotEv.ActionID != "01JQZK0000ACTION1" {
		t.Fatalf("unexpected propose output: %+v", gotEv)
	}

	// Second invocation in the same session auto-increments seq to hop.sess-1.2.
	var out2 bytes.Buffer
	if err := runPropose([]string{"--file", "-"}, strings.NewReader(rawEnvelope), &out2); err != nil {
		t.Fatalf("runPropose #2: %v", err)
	}
	if len(seenKeys) != 2 || seenKeys[0] != "hop.sess-1.1" || seenKeys[1] != "hop.sess-1.2" {
		t.Fatalf("expected hop keys [hop.sess-1.1 hop.sess-1.2], got %v", seenKeys)
	}
}
