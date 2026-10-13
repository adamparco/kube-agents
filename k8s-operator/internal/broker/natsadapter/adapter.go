package natsadapter

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/go-logr/logr"
	"github.com/nats-io/nats.go"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/broker/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/broker/pipeline"
)

const (
	// BrokerQueueGroup is the NATS queue group shared by broker subscribers.
	BrokerQueueGroup = "broker"

	proposePrefix = "a2a.broker.propose."
	approvePrefix = "a2a.broker.approve."
	eventsPrefix  = "a2a.broker.events."
	dlqPrefix     = "a2a.broker.dlq."
)

// ProposeSubject builds `a2a.broker.propose.<namespace>.<agent>`.
func ProposeSubject(namespace, agent string) string {
	return proposePrefix + namespace + "." + agent
}

// ApproveSubject builds `a2a.broker.approve.<namespace>.<actionId>`.
func ApproveSubject(namespace, actionID string) string {
	return approvePrefix + namespace + "." + actionID
}

// EventSubject builds `a2a.broker.events.<namespace>.<actionId>`.
func EventSubject(namespace, actionID string) string {
	return eventsPrefix + namespace + "." + actionID
}

// DLQSubject builds `a2a.broker.dlq.<namespace>.<actionId>`.
func DLQSubject(namespace, actionID string) string {
	return dlqPrefix + namespace + "." + actionID
}

// ActionProposalMessage is the wire payload published to `a2a.broker.propose.<namespace>.<agent>`.
type ActionProposalMessage struct {
	CapabilityRef broker.CapRef   `json:"capabilityRef"`
	TaskID        string          `json:"taskId,omitempty"`
	ActionID      string          `json:"actionId,omitempty"`
	Envelope      json.RawMessage `json:"envelope"`
}

// ActionApprovalMessage is the wire payload published to `a2a.broker.approve.<namespace>.<actionId>`.
type ActionApprovalMessage struct {
	CapabilityRef       broker.CapRef  `json:"capabilityRef,omitempty"`
	Capability          *broker.CapRef `json:"capability,omitempty"`
	Command             string         `json:"command,omitempty"`
	ActionID            string         `json:"actionId"`
	RecordName          string         `json:"recordName,omitempty"`
	Namespace           string         `json:"namespace,omitempty"`
	DecisionID          string         `json:"decisionId,omitempty"`
	Principal           string         `json:"principal,omitempty"`
	ApproverPrincipal   string         `json:"approverPrincipal,omitempty"`
	ApproverDisplayName string         `json:"approverDisplayName,omitempty"`
	Platform            string         `json:"platform,omitempty"`
	EventID             string         `json:"eventId,omitempty"`
	Conversation        string         `json:"conversation,omitempty"`
	Approved            bool           `json:"approved,omitempty"`
	Reason              string         `json:"reason,omitempty"`
}

func (m ActionApprovalMessage) effectiveCapRef() broker.CapRef {
	if m.CapabilityRef.Key != "" && m.CapabilityRef.Revision != 0 {
		return m.CapabilityRef
	}
	if m.Capability != nil {
		return *m.Capability
	}
	return m.CapabilityRef
}

func (m ActionApprovalMessage) effectivePrincipal() string {
	if p := strings.TrimSpace(m.ApproverPrincipal); p != "" {
		return p
	}
	return strings.TrimSpace(m.Principal)
}

func (m ActionApprovalMessage) isApproved() bool {
	switch strings.ToLower(strings.TrimSpace(m.Command)) {
	case "approve":
		return true
	case "reject":
		return false
	default:
		return m.Approved
	}
}

// ActionEventMessage is the wire payload published to `a2a.broker.events.<namespace>.<actionId>`.
type ActionEventMessage struct {
	ActionID   string                          `json:"actionId"`
	RecordName string                          `json:"recordName,omitempty"`
	Namespace  string                          `json:"namespace"`
	Phase      agentv1alpha1.ActionPhase       `json:"phase"`
	Decision   string                          `json:"decision"`
	RiskClass  agentv1alpha1.ActionRiskClass   `json:"riskClass,omitempty"`
	Reason     string                          `json:"reason,omitempty"`
	Message    string                          `json:"message,omitempty"`
	SessionID  string                          `json:"sessionId,omitempty"`
	ThreadRef  string                          `json:"threadRef,omitempty"`
	Report     *agentv1alpha1.ActionReport     `json:"report,omitempty"`
	Escalation *agentv1alpha1.ActionEscalation `json:"escalation,omitempty"`
}

// DLQMessage is emitted to `a2a.broker.dlq.<namespace>.<actionId>` when a proposal or approval
// fails validation or capability verification.
type DLQMessage struct {
	Subject   string `json:"subject"`
	ActionID  string `json:"actionId"`
	Reason    string `json:"reason"`
	Detail    string `json:"detail,omitempty"`
	Timestamp string `json:"timestamp"`
}

// Resumer is satisfied by *pipeline.Pipeline for resuming parked ActionRecords.
type Resumer interface {
	Resume(ctx context.Context, ar *agentv1alpha1.ActionRecord) (*broker.Result, error)
}

// Adapter bridges NATS proposal and approval subjects into the Broker's 11-step pipeline.
type Adapter struct {
	NC             *nats.Conn
	Verifier       *broker.CapabilityAssertionVerifier
	Pipeline       broker.Pipeline
	Resumer        Resumer
	Records        pipeline.RecordStore
	K8sClient      client.Reader
	Journal        broker.RejectionJournal
	Replay         *broker.ReplayGuard
	ExpectedCaller broker.ExpectedCaller
	Namespace      string
	Log            logr.Logger
	Now            func() time.Time

	mu                sync.Mutex
	subs              []*nats.Subscription
	seenCaps          map[string]time.Time
	seenNonces        map[string]time.Time
	seenTriples       map[string]time.Time
	verifiedApprovals map[string]broker.CapRef
}

// Config configures a NATS Adapter.
type Config struct {
	NC             *nats.Conn
	Verifier       *broker.CapabilityAssertionVerifier
	Pipeline       broker.Pipeline
	Resumer        Resumer
	Records        pipeline.RecordStore
	K8sClient      client.Reader
	Journal        broker.RejectionJournal
	Replay         *broker.ReplayGuard
	ExpectedCaller broker.ExpectedCaller
	Namespace      string
	Log            logr.Logger
	Now            func() time.Time
}

// New constructs an Adapter and validates required dependencies.
func New(cfg Config) (*Adapter, error) {
	if cfg.NC == nil {
		return nil, errors.New("natsadapter: NATS connection is required")
	}
	if cfg.Verifier == nil {
		return nil, errors.New("natsadapter: CapabilityAssertionVerifier is required")
	}
	if cfg.Pipeline == nil {
		return nil, errors.New("natsadapter: Pipeline is required")
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	ns := strings.TrimSpace(cfg.Namespace)
	if ns == "" {
		ns = strings.TrimSpace(cfg.ExpectedCaller.Namespace)
	}
	return &Adapter{
		NC:                cfg.NC,
		Verifier:          cfg.Verifier,
		Pipeline:          cfg.Pipeline,
		Resumer:           cfg.Resumer,
		Records:           cfg.Records,
		K8sClient:         cfg.K8sClient,
		Journal:           cfg.Journal,
		Replay:            cfg.Replay,
		ExpectedCaller:    cfg.ExpectedCaller,
		Namespace:         ns,
		Log:               cfg.Log,
		Now:               cfg.Now,
		seenCaps:          make(map[string]time.Time),
		seenNonces:        make(map[string]time.Time),
		seenTriples:       make(map[string]time.Time),
		verifiedApprovals: make(map[string]broker.CapRef),
	}, nil
}

func (a *Adapter) now() time.Time {
	if a.Now != nil {
		return a.Now()
	}
	return time.Now()
}

// Start subscribes to the broker proposal and approval NATS subjects and flushes the connection.
func (a *Adapter) Start(ctx context.Context) error {
	nsToken := "*"
	if a.Namespace != "" {
		nsToken = a.Namespace
	}
	proposeSubj := proposePrefix + nsToken + ".*"
	approveSubj := approvePrefix + nsToken + ".*"

	subPropose, err := a.NC.QueueSubscribe(proposeSubj, BrokerQueueGroup, func(m *nats.Msg) {
		a.HandleProposeMsg(ctx, m)
	})
	if err != nil {
		return fmt.Errorf("natsadapter: subscribe %s: %w", proposeSubj, err)
	}

	subApprove, err := a.NC.QueueSubscribe(approveSubj, BrokerQueueGroup, func(m *nats.Msg) {
		a.HandleApproveMsg(ctx, m)
	})
	if err != nil {
		_ = subPropose.Unsubscribe()
		return fmt.Errorf("natsadapter: subscribe %s: %w", approveSubj, err)
	}

	if a.NC.IsConnected() {
		if err := a.NC.Flush(); err != nil {
			_ = subPropose.Unsubscribe()
			_ = subApprove.Unsubscribe()
			return fmt.Errorf("natsadapter: flush subscriptions: %w", err)
		}
	}

	a.mu.Lock()
	a.subs = []*nats.Subscription{subPropose, subApprove}
	a.mu.Unlock()
	return nil
}

// Stop unsubscribes active NATS subscriptions.
func (a *Adapter) Stop() {
	a.mu.Lock()
	subs := a.subs
	a.subs = nil
	a.mu.Unlock()
	for _, s := range subs {
		_ = s.Unsubscribe()
	}
}

// HandleProposeMsg processes one message from `a2a.broker.propose.<namespace>.<agent>`.
func (a *Adapter) HandleProposeMsg(ctx context.Context, m *nats.Msg) {
	namespace, agent, err := parseTwoTokens(m.Subject, proposePrefix)
	if err != nil {
		a.failProposal(ctx, m, "unknown", "unknown", nil, nil, &broker.Refusal{
			Status:  http.StatusBadRequest,
			Reason:  broker.ReasonInvalid,
			Detail:  err.Error(),
			Journal: true,
		})
		return
	}
	if a.Namespace != "" && namespace != a.Namespace {
		a.failProposal(ctx, m, namespace, "unknown", nil, nil, &broker.Refusal{
			Status:        http.StatusForbidden,
			Reason:        broker.ReasonScopeSpoof,
			Detail:        fmt.Sprintf("broker serves namespace %q, got %q", a.Namespace, namespace),
			Journal:       true,
			SecurityEvent: true,
		})
		return
	}

	var proposal ActionProposalMessage
	if err := json.Unmarshal(m.Data, &proposal); err != nil || len(proposal.Envelope) == 0 {
		a.failProposal(ctx, m, namespace, "unknown", nil, m.Data, &broker.Refusal{
			Status:  http.StatusBadRequest,
			Reason:  broker.ReasonMalformed,
			Detail:  "malformed ActionProposalMessage payload",
			Journal: true,
		})
		return
	}

	fallbackActionID := strings.TrimSpace(proposal.ActionID)
	if fallbackActionID == "" {
		fallbackActionID = strings.TrimSpace(proposal.TaskID)
	}
	if fallbackActionID == "" {
		fallbackActionID = "rejected"
	}

	callerID := a.callerIdentityFor(namespace, agent)

	// Step 1: Verify capability chain (`broker.propose`) and envelope SHA-256 binding.
	envSHA, withoutAssertSHA, err := broker.EnvelopeDigests(proposal.Envelope)
	if err != nil {
		a.failProposal(ctx, m, namespace, fallbackActionID, callerID, proposal.Envelope, &broker.Refusal{
			Status:  http.StatusBadRequest,
			Reason:  broker.ReasonMalformed,
			Detail:  "invalid envelope JSON: " + err.Error(),
			Journal: true,
		})
		return
	}

	tr := &broker.StepTrace{}
	var verifiedReq broker.Requester
	if err := tr.Run(broker.StepAuthenticate, func() (string, error) {
		scope := "namespace/" + namespace
		req, vErr := a.Verifier.VerifyProposal(ctx, proposal.CapabilityRef, scope, envSHA, withoutAssertSHA)
		if vErr != nil {
			return "", vErr
		}
		verifiedReq = req
		return callerID.Username, nil
	}); err != nil {
		a.failProposal(ctx, m, namespace, fallbackActionID, callerID, proposal.Envelope, asRefusal(err))
		return
	}

	// Step 2: Decode and validate envelope + anti-replay checks.
	now := a.now()
	var env *broker.Envelope
	if err := tr.Run(broker.StepValidate, func() (string, error) {
		decoded, dErr := broker.DecodeEnvelope(proposal.Envelope)
		if dErr != nil {
			return "", dErr
		}
		if decoded.Requester.DisplayName != "" && verifiedReq.DisplayName == "" {
			verifiedReq.DisplayName = decoded.Requester.DisplayName
		}
		decoded.Requester = verifiedReq
		if fErr := checkIssuedAtFreshness(decoded.IssuedAt, now); fErr != nil {
			return "", fErr
		}
		if iErr := a.verifyIdempotency(decoded, callerID, namespace, agent); iErr != nil {
			return "", iErr
		}
		if rErr := a.checkProposalReplay(proposal.CapabilityRef, decoded, callerID.Username, now); rErr != nil {
			return "", rErr
		}
		env = decoded
		return decoded.IdempotencyKey, nil
	}); err != nil {
		a.failProposal(ctx, m, namespace, fallbackActionID, callerID, proposal.Envelope, asRefusal(err))
		return
	}

	if a.Replay != nil {
		if dup, ok := a.Replay.LookupDedup(callerID.Username, env.IdempotencyKey); ok {
			phase := agentv1alpha1.PhaseVerified
			if dup.Decision == "gated" || dup.Decision == "pending-approval" {
				phase = agentv1alpha1.PhasePendingApproval
			}
			recName := recordNameForActionID(dup.ActionID)
			ev := ActionEventMessage{
				ActionID:   dup.ActionID,
				RecordName: recName,
				Namespace:  dup.Namespace,
				Decision:   "deduplicated",
				Phase:      phase,
				Message:    fmt.Sprintf("idempotency key matched action %s (original decision: %s)", dup.ActionID, dup.Decision),
				SessionID:  env.Trace.SessionID,
				ThreadRef:  env.Trace.ThreadID,
			}
			if a.K8sClient != nil {
				if ar, findErr := a.findActionRecord(ctx, namespace, recName, dup.ActionID); findErr == nil && ar != nil {
					ev.RecordName = ar.Name
					if ar.Status.Phase != "" {
						ev.Phase = ar.Status.Phase
					}
					ev.RiskClass = ar.Spec.Classification.Class
					ev.Report = ar.Status.Report
					ev.Escalation = ar.Status.Escalation
				}
			}
			a.publishEvent(namespace, dup.ActionID, ev, m.Reply)
			return
		}
	}

	res, err := a.Pipeline.Submit(ctx, callerID, env, tr)
	if err != nil {
		a.failProposal(ctx, m, namespace, fallbackActionID, callerID, proposal.Envelope, asRefusal(err))
		return
	}

	if a.Replay != nil {
		a.Replay.RememberDedup(callerID.Username, env.IdempotencyKey, broker.DedupEntry{
			ActionID:  res.ActionID,
			Namespace: res.Namespace,
			Decision:  res.Decision,
			At:        now,
		})
	}

	ev := ActionEventMessage{
		ActionID:   res.ActionID,
		RecordName: recordNameForActionID(res.ActionID),
		Namespace:  res.Namespace,
		Phase:      agentv1alpha1.ActionPhase(res.Phase),
		Decision:   res.Decision,
		Message:    res.Message,
		SessionID:  env.Trace.SessionID,
		ThreadRef:  env.Trace.ThreadID,
	}
	if a.K8sClient != nil {
		var ar agentv1alpha1.ActionRecord
		if err := a.K8sClient.Get(ctx, client.ObjectKey{Namespace: namespace, Name: ev.RecordName}, &ar); err == nil {
			ev.RiskClass = ar.Spec.Classification.Class
			ev.Report = ar.Status.Report
			ev.Escalation = ar.Status.Escalation
		}
	}
	a.publishEvent(namespace, res.ActionID, ev, m.Reply)
}

// HandleApproveMsg processes one message from `a2a.broker.approve.<namespace>.<actionId>`.
func (a *Adapter) HandleApproveMsg(ctx context.Context, m *nats.Msg) {
	namespace, actionID, err := parseTwoTokens(m.Subject, approvePrefix)
	if err != nil {
		a.publishDLQ("unknown", "unknown", m.Subject, broker.ReasonInvalid, err.Error())
		return
	}
	if a.Namespace != "" && namespace != a.Namespace {
		a.publishDLQ(namespace, actionID, m.Subject, broker.ReasonScopeSpoof, "namespace mismatch")
		return
	}

	var approvalMsg ActionApprovalMessage
	if err := json.Unmarshal(m.Data, &approvalMsg); err != nil {
		a.publishDLQ(namespace, actionID, m.Subject, broker.ReasonMalformed, "malformed ActionApprovalMessage")
		return
	}
	if approvalMsg.ActionID == "" {
		approvalMsg.ActionID = actionID
	}

	ar, err := a.findActionRecord(ctx, namespace, approvalMsg.RecordName, approvalMsg.ActionID)
	if err != nil {
		a.failApproval(namespace, approvalMsg.ActionID, m.Subject, m.Reply, &broker.Refusal{
			Status: http.StatusNotFound,
			Reason: broker.ReasonInvalidRequesterAssertion,
			Detail: err.Error(),
		})
		return
	}

	var expectedEnvSHA string
	if strings.TrimSpace(ar.Spec.EnvelopeJSON) != "" {
		expectedEnvSHA, _, _ = broker.EnvelopeDigests([]byte(ar.Spec.EnvelopeJSON))
	}

	capRef := approvalMsg.effectiveCapRef()
	scope := "namespace/" + namespace
	approver, err := a.Verifier.VerifyApproval(
		ctx,
		capRef,
		scope,
		expectedEnvSHA,
		a.origRequesterForApproval(ctx, ar),
	)
	if err != nil {
		a.failApproval(namespace, approvalMsg.ActionID, m.Subject, m.Reply, asRefusal(err))
		return
	}
	claimedPrincipal := approvalMsg.effectivePrincipal()
	if claimedPrincipal != "" && claimedPrincipal != approver {
		a.failApproval(namespace, approvalMsg.ActionID, m.Subject, m.Reply, &broker.Refusal{
			Status:        http.StatusUnauthorized,
			Reason:        broker.ReasonInvalidRequesterAssertion,
			Detail:        fmt.Sprintf("approval message principal %q does not match capability requester %q", claimedPrincipal, approver),
			Journal:       true,
			SecurityEvent: true,
		})
		return
	}

	if err := a.checkCapReplay(capRef, a.now()); err != nil {
		a.failApproval(namespace, approvalMsg.ActionID, m.Subject, m.Reply, asRefusal(err))
		return
	}

	a.RecordVerifiedApproval(namespace, ar.Name, capRef)

	switch strings.ToLower(strings.TrimSpace(approvalMsg.Command)) {
	case "undo", "uncontest":
		return
	}

	if !approvalMsg.isApproved() {
		reason := approvalMsg.Reason
		if reason == "" {
			reason = "rejected by " + approver
		}
		if a.Records != nil {
			_ = a.Records.SetPhase(ctx, ar, agentv1alpha1.PhaseRejected, reason)
		}
		ar.Status.Phase = agentv1alpha1.PhaseRejected
		sessID, threadRef := traceSessionAndThread(ar)
		ev := ActionEventMessage{
			ActionID:   ar.Spec.ActionID,
			RecordName: ar.Name,
			Namespace:  ar.Namespace,
			Phase:      agentv1alpha1.PhaseRejected,
			Decision:   "refused",
			RiskClass:  ar.Spec.Classification.Class,
			Reason:     "approval-rejected",
			Message:    reason,
			SessionID:  sessID,
			ThreadRef:  threadRef,
		}
		a.publishEvent(namespace, nonEmpty(ar.Spec.ActionID, actionID), ev, m.Reply)
		return
	}

	// Approved == true: if Resumer is configured, transition to Pending and resume unless
	// ResumeController has already executed and verified the ActionRecord.
	if a.Resumer != nil {
		var res *broker.Result
		switch ar.Status.Phase {
		case agentv1alpha1.PhaseVerified, agentv1alpha1.PhaseExecuting,
			agentv1alpha1.PhaseRolledBack, agentv1alpha1.PhaseFailed:
			res = &broker.Result{
				ActionID:  ar.Spec.ActionID,
				Namespace: ar.Namespace,
				Decision:  "accepted",
				Phase:     string(ar.Status.Phase),
				Message:   ar.Status.Message,
			}
		default:
			if ar.Status.Phase != agentv1alpha1.PhasePending {
				nowMeta := metav1.NewTime(a.now())
				ar.Status.Phase = agentv1alpha1.PhasePending
				if ar.Status.Approvals == nil {
					ar.Status.Approvals = &agentv1alpha1.ActionApprovals{
						Required: 1,
					}
				}
				ar.Status.Approvals.Granted = append(ar.Status.Approvals.Granted, agentv1alpha1.ApprovalEntry{
					Principal: approver,
					At:        nowMeta,
					Comment:   approvalMsg.Reason,
				})
			}
			var err error
			res, err = a.Resumer.Resume(ctx, ar)
			if err != nil {
				a.failApproval(namespace, approvalMsg.ActionID, m.Subject, m.Reply, &broker.Refusal{
					Status: http.StatusInternalServerError,
					Reason: "internal-error",
					Detail: err.Error(),
				})
				return
			}
			if a.K8sClient != nil {
				_ = a.K8sClient.Get(ctx, client.ObjectKeyFromObject(ar), ar)
			}
		}
		_ = a.PublishRecordEvent(ctx, ar, res)
		if m.Reply != "" {
			ev := a.buildRecordEvent(ar, res)
			a.replyIfPermitted(m.Reply, ev)
		}
	}
}

// RecordVerifiedApproval marks (namespace, recordName) as having a verified broker.approve capability.
func (a *Adapter) RecordVerifiedApproval(namespace, recordName string, ref broker.CapRef) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.verifiedApprovals[namespace+"/"+recordName] = ref
}

// VerifyApprovalForRecord implements pipeline.ApprovalCapabilityVerifier so ResumeController
// refuses to execute any Pending record whose approval was not backed by a verified capability.
func (a *Adapter) VerifyApprovalForRecord(ctx context.Context, ar *agentv1alpha1.ActionRecord) error {
	if ar == nil {
		return errors.New("action record is nil")
	}
	key := ar.Namespace + "/" + ar.Name
	a.mu.Lock()
	_, ok := a.verifiedApprovals[key]
	a.mu.Unlock()
	if ok {
		return nil
	}

	// Fallback: check if an approval entry or annotation carries a capability ref.
	var capStr string
	if ar.Annotations != nil {
		capStr = ar.Annotations["kubeagents.x-k8s.io/approval-capability-ref"]
	}
	if capStr == "" && ar.Status.Approvals != nil {
		for _, g := range ar.Status.Approvals.Granted {
			trimmed := strings.TrimSpace(g.Comment)
			if strings.HasPrefix(trimmed, "cap:root.") || (strings.HasPrefix(trimmed, "root.") && (strings.Contains(trimmed, "@") || strings.Contains(trimmed, "#"))) {
				capStr = trimmed
				break
			}
		}
	}
	if capStr == "" {
		return fmt.Errorf("no verified broker.approve capability recorded for %s", key)
	}
	ref, err := broker.ParseCapRef(capStr)
	if err != nil {
		return err
	}
	var expectedEnvSHA string
	if strings.TrimSpace(ar.Spec.EnvelopeJSON) != "" {
		expectedEnvSHA, _, _ = broker.EnvelopeDigests([]byte(ar.Spec.EnvelopeJSON))
	}
	if _, err := a.Verifier.VerifyApproval(ctx, ref, "namespace/"+ar.Namespace, expectedEnvSHA, a.origRequesterForApproval(ctx, ar)); err != nil {
		return err
	}
	a.RecordVerifiedApproval(ar.Namespace, ar.Name, ref)
	return nil
}

func (a *Adapter) origRequesterForApproval(ctx context.Context, ar *agentv1alpha1.ActionRecord) string {
	if ar == nil {
		return ""
	}
	if a.K8sClient != nil && ar.Spec.AgentRef.Name != "" {
		var ag agentv1alpha1.Agent
		if err := a.K8sClient.Get(ctx, client.ObjectKey{Namespace: ar.Namespace, Name: ar.Spec.AgentRef.Name}, &ag); err == nil {
			if ag.Spec.Operations != nil && ag.Spec.Operations.ApprovalRosterRef != nil && ag.Spec.Operations.ApprovalRosterRef.Name != "" {
				var roster agentv1alpha1.ApprovalRoster
				if err := a.K8sClient.Get(ctx, client.ObjectKey{Namespace: ar.Namespace, Name: ag.Spec.Operations.ApprovalRosterRef.Name}, &roster); err == nil {
					if roster.Spec.AllowSelfApproval != nil && *roster.Spec.AllowSelfApproval {
						return ""
					}
				}
			}
		}
	}
	return ar.Spec.Requester.ID
}

// PublishRecordEvent implements pipeline.EventPublisher, emitting terminal ActionRecord events
// to `a2a.broker.events.<namespace>.<actionId>`.
func (a *Adapter) PublishRecordEvent(_ context.Context, ar *agentv1alpha1.ActionRecord, res *broker.Result) error {
	if a == nil || ar == nil || res == nil {
		return nil
	}
	ev := a.buildRecordEvent(ar, res)
	actionID := nonEmpty(ar.Spec.ActionID, ar.Name)
	a.publishEvent(ar.Namespace, actionID, ev, "")
	return nil
}

func (a *Adapter) buildRecordEvent(ar *agentv1alpha1.ActionRecord, res *broker.Result) ActionEventMessage {
	actionID := nonEmpty(ar.Spec.ActionID, ar.Name)
	sessID, threadRef := traceSessionAndThread(ar)
	return ActionEventMessage{
		ActionID:   actionID,
		RecordName: nonEmpty(ar.Name, res.ActionID),
		Namespace:  ar.Namespace,
		Phase:      agentv1alpha1.ActionPhase(res.Phase),
		Decision:   res.Decision,
		RiskClass:  ar.Spec.Classification.Class,
		Message:    res.Message,
		SessionID:  sessID,
		ThreadRef:  threadRef,
		Report:     ar.Status.Report,
		Escalation: ar.Status.Escalation,
	}
}

func traceSessionAndThread(ar *agentv1alpha1.ActionRecord) (sessionID, threadRef string) {
	if ar == nil {
		return "", ""
	}
	if ar.Spec.Trace != nil {
		sessionID = ar.Spec.Trace.SessionID
	}
	if strings.TrimSpace(ar.Spec.EnvelopeJSON) != "" {
		var env broker.Envelope
		if err := json.Unmarshal([]byte(ar.Spec.EnvelopeJSON), &env); err == nil {
			if sessionID == "" {
				sessionID = env.Trace.SessionID
			}
			threadRef = env.Trace.ThreadID
		}
	}
	return sessionID, threadRef
}

func (a *Adapter) callerIdentityFor(namespace, agent string) *broker.Identity {
	sa := a.ExpectedCaller.ServiceAccount
	if sa == "" {
		sa = agent + "-agent-reader"
	}
	tier := a.ExpectedCaller.Tier
	if tier == "" {
		tier = agentv1alpha1.TierDeveloperTeam
	}
	scope := a.ExpectedCaller.Scope
	if scope == "" {
		scope = namespace
	}
	agentName := a.ExpectedCaller.AgentName
	if agentName == "" {
		agentName = agent
	}
	return &broker.Identity{
		Username:       fmt.Sprintf("system:serviceaccount:%s:%s", namespace, sa),
		Namespace:      namespace,
		ServiceAccount: sa,
		AgentName:      agentName,
		Tier:           tier,
		Scope:          scope,
	}
}

func (a *Adapter) verifyIdempotency(env *broker.Envelope, id *broker.Identity, namespace, agent string) error {
	candidates := []string{
		id.Username,
		fmt.Sprintf("system:serviceaccount:%s:%s-agent-reader", namespace, agent),
		agent,
	}
	var lastErr error
	for _, c := range candidates {
		if c == "" {
			continue
		}
		if err := broker.CompareIdempotencyKey(c, env); err == nil {
			return nil
		} else {
			lastErr = err
		}
	}
	return lastErr
}

func (a *Adapter) checkProposalReplay(ref broker.CapRef, env *broker.Envelope, caller string, now time.Time) error {
	a.mu.Lock()
	defer a.mu.Unlock()

	capKey := ref.String()
	if _, seen := a.seenCaps[capKey]; seen {
		return &broker.Refusal{
			Status:        http.StatusForbidden,
			Reason:        broker.ReasonReplayedEnvelope,
			Detail:        "capability reference " + capKey + " was already consumed",
			Journal:       true,
			SecurityEvent: true,
		}
	}
	if _, seen := a.seenNonces[env.Nonce]; seen {
		return &broker.Refusal{
			Status:        http.StatusForbidden,
			Reason:        broker.ReasonReplayedEnvelope,
			Detail:        "envelope nonce was already consumed",
			Journal:       true,
			SecurityEvent: true,
		}
	}
	triple := caller + "\x1f" + env.Trace.TraceID + "\x1f" + env.IdempotencyKey
	if _, seen := a.seenTriples[triple]; seen {
		return &broker.Refusal{
			Status:        http.StatusForbidden,
			Reason:        broker.ReasonReplayedEnvelope,
			Detail:        "this (identity, traceId, idempotencyKey) was already submitted within the last 24 hours",
			Journal:       true,
			SecurityEvent: true,
		}
	}
	a.seenCaps[capKey] = now
	a.seenNonces[env.Nonce] = now
	a.seenTriples[triple] = now
	return nil
}

func (a *Adapter) checkCapReplay(ref broker.CapRef, now time.Time) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	capKey := ref.String()
	if _, seen := a.seenCaps[capKey]; seen {
		return &broker.Refusal{
			Status:        http.StatusForbidden,
			Reason:        broker.ReasonReplayedEnvelope,
			Detail:        "approval capability reference " + capKey + " was already consumed",
			Journal:       true,
			SecurityEvent: true,
		}
	}
	a.seenCaps[capKey] = now
	return nil
}

func (a *Adapter) findActionRecord(ctx context.Context, namespace, recordName, actionID string) (*agentv1alpha1.ActionRecord, error) {
	if a.K8sClient == nil {
		return nil, errors.New("k8s client is not configured on adapter")
	}
	candidates := []string{}
	if strings.TrimSpace(recordName) != "" {
		candidates = append(candidates, strings.TrimSpace(recordName))
	}
	if strings.TrimSpace(actionID) != "" {
		lower := strings.ToLower(strings.TrimSpace(actionID))
		candidates = append(candidates, recordNameForActionID(actionID), "ar-"+lower, "action-"+lower, strings.TrimSpace(actionID))
	}
	for _, name := range candidates {
		var ar agentv1alpha1.ActionRecord
		if err := a.K8sClient.Get(ctx, client.ObjectKey{Namespace: namespace, Name: name}, &ar); err == nil {
			return &ar, nil
		}
	}
	var list agentv1alpha1.ActionRecordList
	if err := a.K8sClient.List(ctx, &list, client.InNamespace(namespace)); err == nil {
		for i := range list.Items {
			if strings.EqualFold(list.Items[i].Spec.ActionID, actionID) || list.Items[i].Name == actionID {
				return &list.Items[i], nil
			}
		}
	}
	return nil, fmt.Errorf("action record %q not found in namespace %q", nonEmpty(recordName, actionID), namespace)
}

func (a *Adapter) failProposal(
	ctx context.Context,
	m *nats.Msg,
	namespace string,
	actionID string,
	id *broker.Identity,
	rawBody []byte,
	ref *broker.Refusal,
) {
	if ref.Journal && a.Journal != nil {
		if jid, err := a.Journal.Reject(ctx, id, rawBody, ref); err == nil && jid != "" && actionID == "rejected" {
			actionID = jid
		}
	}
	ev := ActionEventMessage{
		ActionID:   actionID,
		RecordName: recordNameForActionID(actionID),
		Namespace:  namespace,
		Phase:      agentv1alpha1.PhaseRejected,
		Decision:   "refused",
		Reason:     ref.Reason,
		Message:    ref.Detail,
	}
	a.publishEvent(namespace, actionID, ev, m.Reply)
	a.publishDLQ(namespace, actionID, m.Subject, ref.Reason, ref.Detail)
}

func (a *Adapter) failApproval(namespace, actionID, subject, reply string, ref *broker.Refusal) {
	ev := ActionEventMessage{
		ActionID:  actionID,
		Namespace: namespace,
		Phase:     agentv1alpha1.PhaseRejected,
		Decision:  "refused",
		Reason:    ref.Reason,
		Message:   ref.Detail,
	}
	a.publishEvent(namespace, actionID, ev, reply)
	a.publishDLQ(namespace, actionID, subject, ref.Reason, ref.Detail)
}

func (a *Adapter) publishEvent(namespace, actionID string, ev ActionEventMessage, reply string) {
	if strings.TrimSpace(actionID) == "" {
		actionID = "unknown"
	}
	payload, err := json.Marshal(ev)
	if err != nil {
		return
	}
	_ = a.NC.Publish(EventSubject(namespace, actionID), payload)
	a.replyIfPermitted(reply, ev)
}

func (a *Adapter) replyIfPermitted(reply string, ev ActionEventMessage) {
	reply = strings.TrimSpace(reply)
	if reply == "" {
		return
	}
	if strings.HasPrefix(reply, "a2a.cap.") || strings.HasPrefix(reply, "$KV.") || strings.HasPrefix(reply, "$JS.") {
		return
	}
	if payload, err := json.Marshal(ev); err == nil {
		_ = a.NC.Publish(reply, payload)
	}
}

func (a *Adapter) publishDLQ(namespace, actionID, subject, reason, detail string) {
	if strings.TrimSpace(namespace) == "" {
		namespace = "unknown"
	}
	if strings.TrimSpace(actionID) == "" {
		actionID = "unknown"
	}
	msg := DLQMessage{
		Subject:   subject,
		ActionID:  actionID,
		Reason:    reason,
		Detail:    detail,
		Timestamp: a.now().UTC().Format(time.RFC3339),
	}
	if payload, err := json.Marshal(msg); err == nil {
		_ = a.NC.Publish(DLQSubject(namespace, actionID), payload)
	}
}

func parseTwoTokens(subject, prefix string) (string, string, error) {
	rest, ok := strings.CutPrefix(subject, prefix)
	if !ok {
		return "", "", fmt.Errorf("subject %q does not have prefix %q", subject, prefix)
	}
	first, second, ok := strings.Cut(rest, ".")
	if !ok || first == "" || second == "" || strings.Contains(second, ".") {
		return "", "", fmt.Errorf("subject %q must have exactly two tokens after %q", subject, prefix)
	}
	return first, second, nil
}

func recordNameForActionID(actionID string) string {
	actionID = strings.TrimSpace(actionID)
	if actionID == "" || actionID == "rejected" || actionID == "unknown" {
		return ""
	}
	if strings.HasPrefix(actionID, "ar-") || strings.HasPrefix(actionID, "action-") {
		return actionID
	}
	return "ar-" + strings.ToLower(actionID)
}

func nonEmpty(a, b string) string {
	if strings.TrimSpace(a) != "" {
		return a
	}
	return b
}

func asRefusal(err error) *broker.Refusal {
	var ref *broker.Refusal
	if errors.As(err, &ref) {
		return ref
	}
	return &broker.Refusal{
		Status:  http.StatusInternalServerError,
		Reason:  "internal-error",
		Detail:  err.Error(),
		Journal: true,
	}
}

func checkIssuedAtFreshness(issuedAt string, now time.Time) error {
	issued, err := broker.ParseIssuedAt(issuedAt)
	if err != nil {
		return &broker.Refusal{
			Status: http.StatusBadRequest,
			Reason: broker.ReasonInvalid,
			Detail: "issuedAt: " + err.Error(),
		}
	}
	if issued.Before(now.Add(-broker.FreshnessPast)) || issued.After(now.Add(broker.FreshnessFuture)) {
		return &broker.Refusal{
			Status:        http.StatusForbidden,
			Reason:        broker.ReasonEnvelopeExpired,
			Detail:        fmt.Sprintf("issuedAt %s is outside the freshness window", issuedAt),
			Journal:       true,
			SecurityEvent: true,
		}
	}
	return nil
}

