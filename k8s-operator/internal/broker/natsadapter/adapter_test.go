package natsadapter

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker"
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

type fakePipeline struct {
	submittedEnv *broker.Envelope
	submittedID  *broker.Identity
	resumedAR    *agentv1alpha1.ActionRecord
}

func (f *fakePipeline) Submit(_ context.Context, id *broker.Identity, env *broker.Envelope, tr *broker.StepTrace) (*broker.Result, error) {
	f.submittedID = id
	f.submittedEnv = env
	for _, s := range []broker.Step{
		broker.StepResolveScope,
		broker.StepClassify,
		broker.StepBrake,
		broker.StepUndoPlan,
		broker.StepGate,
		broker.StepSnapshot,
		broker.StepExecute,
		broker.StepVerify,
		broker.StepJournal,
	} {
		_ = tr.Run(s, func() (string, error) { return "ok", nil })
	}
	return &broker.Result{
		ActionID:  "01JQZK0000TESTACTION000001",
		Namespace: id.Namespace,
		Decision:  "accepted",
		Phase:     string(agentv1alpha1.PhaseVerified),
		Message:   "scaled deployment web to 3 replicas",
	}, nil
}

func (f *fakePipeline) Resume(_ context.Context, ar *agentv1alpha1.ActionRecord) (*broker.Result, error) {
	f.resumedAR = ar
	return &broker.Result{
		ActionID:  ar.Spec.ActionID,
		Namespace: ar.Namespace,
		Decision:  "accepted",
		Phase:     string(agentv1alpha1.PhaseVerified),
		Message:   "resumed and verified",
	}, nil
}

func buildValidEnvelopeBytes(t *testing.T, now time.Time, agentIdentity, nonce string) []byte {
	t.Helper()
	replicas := int32(3)
	env := &broker.Envelope{
		APIVersion: broker.APIVersion,
		Kind:       broker.EnvelopeKind,
		Intent:     "scale deployment web to 3 replicas",
		Operations: []broker.Operation{
			{
				Op: "scale",
				Target: &broker.Target{
					Group:     "apps",
					Version:   "v1",
					Kind:      "Deployment",
					Namespace: "prod",
					Name:      "web",
				},
				Scale: &broker.ScaleSpec{Replicas: &replicas},
			},
		},
		Requester: broker.Requester{
			Kind:     "human",
			ID:       "slack:SPOOFED",
			Platform: "slack",
		},
		Trigger: broker.Trigger{
			Source: "chat",
		},
		Trace: broker.Trace{
			TraceID:   strings.Repeat("a", 32),
			SessionID: "sess-1",
			ThreadID:  "thread-1",
		},
		IssuedAt: now.UTC().Format(time.RFC3339),
		Nonce:    nonce,
	}
	idem, err := broker.ComputeIdempotencyKey(agentIdentity, env)
	if err != nil {
		t.Fatalf("ComputeIdempotencyKey: %v", err)
	}
	env.IdempotencyKey = idem
	raw, err := json.Marshal(env)
	if err != nil {
		t.Fatalf("marshal envelope: %v", err)
	}
	return raw
}

func TestAdapter_ProposeVerifiesCapabilityAndPublishesEvent(t *testing.T) {
	nc := startEmbeddedNATS(t)
	now := time.Date(2026, 10, 9, 12, 0, 0, 0, time.UTC)

	rawEnv := buildValidEnvelopeBytes(t, now, "system:serviceaccount:prod:sre-agent-reader", strings.Repeat("1", 32))
	envDigest, _, err := broker.EnvelopeDigests(rawEnv)
	if err != nil {
		t.Fatalf("EnvelopeDigests: %v", err)
	}

	_, err = nc.Subscribe("a2a.cap.verify.broker", func(m *nats.Msg) {
		resp, _ := json.Marshal(broker.CapVerifyResponse{
			Allowed:        true,
			Requester:      "slack:U777VERIFIED",
			EnvelopeSHA256: envDigest,
		})
		_ = m.Respond(resp)
	})
	if err != nil {
		t.Fatalf("subscribe verifier: %v", err)
	}

	fp := &fakePipeline{}
	adapter, err := New(Config{
		NC:        nc,
		Verifier:  broker.NewCapabilityAssertionVerifier(nc, "broker"),
		Pipeline:  fp,
		Resumer:   fp,
		Namespace: "prod",
		Now:       func() time.Time { return now },
	})
	if err != nil {
		t.Fatalf("New adapter: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if err := adapter.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer adapter.Stop()

	eventSub, err := nc.SubscribeSync("a2a.broker.events.prod.>")
	if err != nil {
		t.Fatalf("subscribe events: %v", err)
	}
	dlqSub, err := nc.SubscribeSync("a2a.broker.dlq.prod.>")
	if err != nil {
		t.Fatalf("subscribe dlq: %v", err)
	}
	_ = nc.Flush()

	propBytes, _ := json.Marshal(ActionProposalMessage{
		CapabilityRef: broker.CapRef{Key: "hop.sess-1.1", Revision: 5},
		TaskID:        "task-123",
		Envelope:      rawEnv,
	})
	if err := nc.Publish(ProposeSubject("prod", "sre"), propBytes); err != nil {
		t.Fatalf("publish propose: %v", err)
	}

	msg, err := eventSub.NextMsg(2 * time.Second)
	if err != nil {
		t.Fatalf("await event: %v", err)
	}
	var ev ActionEventMessage
	if err := json.Unmarshal(msg.Data, &ev); err != nil {
		t.Fatalf("unmarshal event: %v", err)
	}
	if ev.Decision != "accepted" || ev.Phase != agentv1alpha1.PhaseVerified {
		t.Fatalf("unexpected event: %+v", ev)
	}
	if fp.submittedEnv == nil || fp.submittedEnv.Requester.ID != "slack:U777VERIFIED" {
		t.Fatalf("expected submitted envelope requester to be overwritten with slack:U777VERIFIED, got %+v", fp.submittedEnv)
	}

	// Replaying the same capability reference is refused and sent to DLQ.
	if err := nc.Publish(ProposeSubject("prod", "sre"), propBytes); err != nil {
		t.Fatalf("publish replayed propose: %v", err)
	}
	dlqMsg, err := dlqSub.NextMsg(2 * time.Second)
	if err != nil {
		t.Fatalf("await dlq on replay: %v", err)
	}
	var dlq DLQMessage
	_ = json.Unmarshal(dlqMsg.Data, &dlq)
	if dlq.Reason != broker.ReasonReplayedEnvelope {
		t.Fatalf("expected DLQ reason %q on replay, got %+v", broker.ReasonReplayedEnvelope, dlq)
	}
}

func TestAdapter_ApproveVerifiesRootCapabilityFourEyesAndResumes(t *testing.T) {
	nc := startEmbeddedNATS(t)
	now := time.Date(2026, 10, 9, 12, 0, 0, 0, time.UTC)

	rawEnv := buildValidEnvelopeBytes(t, now, "system:serviceaccount:prod:sre-agent-reader", strings.Repeat("2", 32))
	envDigest, _, err := broker.EnvelopeDigests(rawEnv)
	if err != nil {
		t.Fatalf("EnvelopeDigests: %v", err)
	}

	ar := &agentv1alpha1.ActionRecord{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "action-01jqzk0000parked",
			Namespace: "prod",
		},
		Spec: agentv1alpha1.ActionRecordSpec{
			ActionID:     "01JQZK0000PARKED",
			EnvelopeJSON: string(rawEnv),
			Requester: agentv1alpha1.ActionRequester{
				Kind: "human",
				ID:   "slack:U111REQUESTER",
			},
			Classification: agentv1alpha1.ActionClassification{
				Class: agentv1alpha1.RiskGated,
			},
			Trace: &agentv1alpha1.ActionTrace{
				SessionID: "sess-1",
			},
		},
		Status: agentv1alpha1.ActionRecordStatus{
			Phase: agentv1alpha1.PhasePendingApproval,
		},
	}

	scheme := runtime.NewScheme()
	_ = agentv1alpha1.AddToScheme(scheme)
	k8sClient := fake.NewClientBuilder().WithScheme(scheme).WithObjects(ar).Build()

	approverPrincipal := "slack:U222APPROVER"
	_, err = nc.Subscribe("a2a.cap.verify.broker", func(m *nats.Msg) {
		resp, _ := json.Marshal(broker.CapVerifyResponse{
			Allowed:        true,
			Requester:      approverPrincipal,
			EnvelopeSHA256: envDigest,
		})
		_ = m.Respond(resp)
	})
	if err != nil {
		t.Fatalf("subscribe verifier: %v", err)
	}

	fp := &fakePipeline{}
	adapter, err := New(Config{
		NC:        nc,
		Verifier:  broker.NewCapabilityAssertionVerifier(nc, "broker"),
		Pipeline:  fp,
		Resumer:   fp,
		K8sClient: k8sClient,
		Namespace: "prod",
		Now:       func() time.Time { return now },
	})
	if err != nil {
		t.Fatalf("New adapter: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if err := adapter.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer adapter.Stop()

	eventSub, _ := nc.SubscribeSync("a2a.broker.events.prod.>")
	dlqSub, _ := nc.SubscribeSync("a2a.broker.dlq.prod.>")
	_ = nc.Flush()

	// 1. Self-approval attempt (four-eyes violation) must fail and emit DLQ.
	approverPrincipal = "slack:U111REQUESTER"
	selfApproveBytes, _ := json.Marshal(ActionApprovalMessage{
		CapabilityRef: broker.CapRef{Key: "root.approve-01JQZK0000PARKED-dec1", Revision: 1},
		ActionID:      "01JQZK0000PARKED",
		DecisionID:    "dec1",
		Principal:     "slack:U111REQUESTER",
		Approved:      true,
	})
	_ = nc.Publish(ApproveSubject("prod", "01JQZK0000PARKED"), selfApproveBytes)

	if _, err := dlqSub.NextMsg(2 * time.Second); err != nil {
		t.Fatalf("expected DLQ message on self-approval: %v", err)
	}
	// Drain refused event.
	_, _ = eventSub.NextMsg(2 * time.Second)
	if fp.resumedAR != nil {
		t.Fatal("self-approval must not resume ActionRecord")
	}

	// 2. Distinct approver succeeds and resumes ActionRecord.
	approverPrincipal = "slack:U222APPROVER"
	validApproveBytes, _ := json.Marshal(ActionApprovalMessage{
		CapabilityRef: broker.CapRef{Key: "root.approve-01JQZK0000PARKED-dec2", Revision: 2},
		ActionID:      "01JQZK0000PARKED",
		DecisionID:    "dec2",
		Principal:     "slack:U222APPROVER",
		Approved:      true,
		Reason:        "LGTM",
	})
	_ = nc.Publish(ApproveSubject("prod", "01JQZK0000PARKED"), validApproveBytes)

	evMsg, err := eventSub.NextMsg(2 * time.Second)
	if err != nil {
		t.Fatalf("await approval resume event: %v", err)
	}
	var ev ActionEventMessage
	_ = json.Unmarshal(evMsg.Data, &ev)
	if ev.Decision != "accepted" || ev.Phase != agentv1alpha1.PhaseVerified {
		t.Fatalf("unexpected approval event: %+v", ev)
	}
	if fp.resumedAR == nil || fp.resumedAR.Name != ar.Name {
		t.Fatalf("expected ActionRecord %s to be resumed, got %+v", ar.Name, fp.resumedAR)
	}
	if err := adapter.VerifyApprovalForRecord(ctx, ar); err != nil {
		t.Fatalf("expected VerifyApprovalForRecord to succeed after verified approval, got %v", err)
	}
}
