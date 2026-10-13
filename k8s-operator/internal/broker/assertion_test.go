package broker

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
)

func startEmbeddedNATS(t *testing.T) *nats.Conn {
	t.Helper()
	opts := &natsserver.Options{
		Host:   "127.0.0.1",
		Port:   -1,
		NoLog:  true,
		NoSigs: true,
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
	return nc
}

func TestCapabilityAssertionVerifier_VerifyProposalAndEnvelope(t *testing.T) {
	nc := startEmbeddedNATS(t)

	rawEnv := []byte(`{"apiVersion":"kubeagents.x-k8s.io/v1alpha1","kind":"ActionEnvelope","intent":"scale web","requester":{"kind":"human","id":"slack:FORGED","platform":"slack","assertion":"hop.sess-1.1 @7"}}`)
	wantDigest, _, err := EnvelopeDigests(rawEnv)
	if err != nil {
		t.Fatalf("EnvelopeDigests: %v", err)
	}

	var gotReq CapVerifyRequest
	_, err = nc.Subscribe("a2a.cap.verify.broker", func(m *nats.Msg) {
		if !strings.HasPrefix(m.Reply, "a2a.cap.reply.broker.") {
			t.Errorf("unexpected reply subject %q", m.Reply)
			return
		}
		_ = json.Unmarshal(m.Data, &gotReq)
		resp, _ := json.Marshal(CapVerifyResponse{
			Allowed:        true,
			Requester:      "slack:U0123REAL",
			EnvelopeSHA256: wantDigest,
		})
		_ = m.Respond(resp)
	})
	if err != nil {
		t.Fatalf("subscribe verifier: %v", err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatalf("flush: %v", err)
	}

	v := NewCapabilityAssertionVerifier(nc, "broker")
	env := &Envelope{
		Requester: Requester{
			Kind:      "human",
			ID:        "slack:FORGED",
			Platform:  "slack",
			Assertion: "hop.sess-1.1 @7",
		},
	}
	verified, err := v.VerifyEnvelope(context.Background(), env, rawEnv, "namespace/prod")
	if err != nil {
		t.Fatalf("VerifyEnvelope unexpected error: %v", err)
	}
	if gotReq.Verb != VerbBrokerPropose || gotReq.Resource != "namespace/prod" || gotReq.Ref.Key != "hop.sess-1.1" || gotReq.Ref.Revision != 7 {
		t.Fatalf("unexpected verify request: %+v", gotReq)
	}
	if verified.ID != "slack:U0123REAL" || verified.Kind != "human" || verified.Platform != "slack" {
		t.Fatalf("unexpected verified requester: %+v", verified)
	}
	if env.Requester.ID != "slack:U0123REAL" {
		t.Fatalf("expected env.Requester.ID to be overwritten with verified requester, got %q", env.Requester.ID)
	}
}

func TestCapabilityAssertionVerifier_RefusesDigestMismatchDeniedAndTimeout(t *testing.T) {
	nc := startEmbeddedNATS(t)

	mode := "mismatch"
	_, err := nc.Subscribe("a2a.cap.verify.broker", func(m *nats.Msg) {
		switch mode {
		case "mismatch":
			resp, _ := json.Marshal(CapVerifyResponse{
				Allowed:        true,
				Requester:      "slack:U0123REAL",
				EnvelopeSHA256: strings.Repeat("a", 64),
			})
			_ = m.Respond(resp)
		case "denied":
			resp, _ := json.Marshal(CapVerifyResponse{
				Allowed: false,
				Reason:  "resource is outside the capability's scope",
			})
			_ = m.Respond(resp)
		case "timeout":
			// Do not respond.
		}
	})
	if err != nil {
		t.Fatalf("subscribe: %v", err)
	}
	_ = nc.Flush()

	v := NewCapabilityAssertionVerifier(nc, "broker")
	v.Timeout = 100 * time.Millisecond

	for _, tc := range []string{"mismatch", "denied", "timeout"} {
		mode = tc
		_, err := v.VerifyProposal(
			context.Background(),
			CapRef{Key: "hop.sess-1.1", Revision: 3},
			"namespace/prod",
			strings.Repeat("b", 64),
		)
		if err == nil {
			t.Fatalf("mode %s: expected refusal error, got nil", tc)
		}
		var ref *Refusal
		if !errors.As(err, &ref) {
			t.Fatalf("mode %s: expected *Refusal, got %T (%v)", tc, err, err)
		}
		if ref.Status != http.StatusUnauthorized || ref.Reason != ReasonInvalidRequesterAssertion {
			t.Fatalf("mode %s: unexpected refusal %+v", tc, ref)
		}
	}
}

func TestCapabilityAssertionVerifier_VerifyApprovalFourEyesAndDigest(t *testing.T) {
	nc := startEmbeddedNATS(t)
	digest := strings.Repeat("c", 64)
	approver := "slack:U999APPROVER"

	_, err := nc.Subscribe("a2a.cap.verify.broker", func(m *nats.Msg) {
		var req CapVerifyRequest
		_ = json.Unmarshal(m.Data, &req)
		if req.Verb != VerbBrokerApprove {
			t.Errorf("expected verb %q, got %q", VerbBrokerApprove, req.Verb)
		}
		resp, _ := json.Marshal(CapVerifyResponse{
			Allowed:        true,
			Requester:      approver,
			EnvelopeSHA256: digest,
		})
		_ = m.Respond(resp)
	})
	if err != nil {
		t.Fatalf("subscribe: %v", err)
	}
	_ = nc.Flush()

	v := NewCapabilityAssertionVerifier(nc, "broker")

	// Distinct approver succeeds.
	gotApprover, err := v.VerifyApproval(
		context.Background(),
		CapRef{Key: "root.approve-act1-dec1", Revision: 2},
		"namespace/prod",
		digest,
		"slack:U111REQUESTER",
	)
	if err != nil {
		t.Fatalf("VerifyApproval unexpected error: %v", err)
	}
	if gotApprover != approver {
		t.Fatalf("got approver %q, want %q", gotApprover, approver)
	}

	// Self-approval (four-eyes violation) is refused.
	if _, err := v.VerifyApproval(
		context.Background(),
		CapRef{Key: "root.approve-act1-dec1", Revision: 2},
		"namespace/prod",
		digest,
		approver,
	); err == nil {
		t.Fatal("expected four-eyes refusal when approver matches original requester")
	}
}

func TestParseCapRefAndVerifyApprovalWithoutDigest(t *testing.T) {
	for _, tc := range []struct {
		raw  string
		want CapRef
	}{
		{raw: `"cap:root.approve-act1-dec1#1"`, want: CapRef{Key: "root.approve-act1-dec1", Revision: 1}},
		{raw: `"root.approve-act1-dec1 @2"`, want: CapRef{Key: "root.approve-act1-dec1", Revision: 2}},
		{raw: `{"key":"hop.sess-1.1","revision":7}`, want: CapRef{Key: "hop.sess-1.1", Revision: 7}},
	} {
		var got CapRef
		if err := json.Unmarshal([]byte(tc.raw), &got); err != nil {
			t.Fatalf("Unmarshal(%s): %v", tc.raw, err)
		}
		if got != tc.want {
			t.Fatalf("Unmarshal(%s) = %+v, want %+v", tc.raw, got, tc.want)
		}
	}

	nc := startEmbeddedNATS(t)
	_, err := nc.Subscribe("a2a.cap.verify.broker", func(m *nats.Msg) {
		resp, _ := json.Marshal(CapVerifyResponse{
			Allowed:   true,
			Requester: "slack:U999APPROVER",
		})
		_ = m.Respond(resp)
	})
	if err != nil {
		t.Fatalf("subscribe: %v", err)
	}
	_ = nc.Flush()

	v := NewCapabilityAssertionVerifier(nc, "broker")
	gotApprover, err := v.VerifyApproval(
		context.Background(),
		CapRef{Key: "root.approve-act1-dec1", Revision: 1},
		"namespace/prod",
		strings.Repeat("d", 64),
		"slack:U111REQUESTER",
	)
	if err != nil {
		t.Fatalf("VerifyApproval with empty response EnvelopeSHA256 failed: %v", err)
	}
	if gotApprover != "slack:U999APPROVER" {
		t.Fatalf("got approver %q, want slack:U999APPROVER", gotApprover)
	}
}
