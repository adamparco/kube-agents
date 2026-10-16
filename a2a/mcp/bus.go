package mcp

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

const (
	streamTasks                  = "TASKS"
	streamDirectory              = "DIRECTORY"
	streamTopicsState            = "TOPICS-STATE"
	streamTopicsJournal          = "TOPICS-JOURNAL"
	subjectAllA2A                = "a2a.>"
	subjectAllConsoleOut         = "chat.console.*.out"
	subjectAllNotify             = "chat.notify.*"
	subjectConsoleInPrefix       = "chat.console."
	subjectConsoleInSuffix       = ".in"
	subjectConsoleOutPrefix      = "chat.console."
	subjectConsoleOutSuffix      = ".out"
	subjectTasksPrefix           = "a2a.tasks."
	subjectTasksInSuffix         = ".in"
	subjectTasksOutSuffix        = ".out"
	subjectAgentsPrefix          = "a2a.agents."
	subjectAgentsCardSuffix      = ".card"
	subjectTopicsPrefix          = "a2a.topics."
	subjectTopicsJournalPrefix   = "a2a.topics.journal."
	inboxPrefixBase              = "_INBOX."
	natsClientNamePrefix         = "kube-agents-mcp-"
	natsObserverClientName       = "kube-agents-mcp-observer"
	observerUserGateway          = "gateway"
	topicClassState              = "state"
	topicClassJournal            = "journal"
	defaultNotifyTitle           = "Proactive Kube-Agents Notification"
	pushSummaryMaxChars          = 600
	ellipsisSuffix               = "..."
	consoleConvPrefix            = "console:"
	consoleFrameKindText         = "text"
	msgIDPrefix                  = "jetski-msg-"
	cmdSessionOn                 = "/session"
	cmdSessionOff                = "/session off"
	cmdStatus                    = "status"
	cmdStop                      = "stop"
	cmdHelp                      = "help"
	cmdDelegatePrefix            = "delegate:"
	modeAuto                     = "auto"
	modePlatform                 = "platform"
	modeSession                  = "session"
	modeDelegate                 = "delegate"
	orderedConsumerIdleThreshold = 5 * time.Second
	orderedFetchBatchSize        = 256
	orderedFetchMaxWait          = 750 * time.Millisecond
	bootstrapTimeout             = 8 * time.Second
	controlNoticeWait            = 2500 * time.Millisecond
	delegationChildWait          = 5 * time.Second
	delegationWakeWait           = 8 * time.Second
	defaultTurnWaitTimeout       = 150 * time.Second
	defaultStepStreamWindow      = 8 * time.Second
	initialStepStreamWindow      = 10 * time.Second
	minStepBatchWait             = 350 * time.Millisecond
	stepDebounceWait             = 200 * time.Millisecond
	pollTickInterval             = 100 * time.Millisecond
	progressHeartbeatInterval    = 2 * time.Second
	natsConnectTimeout           = 10 * time.Second
	natsReconnectWait            = 1 * time.Second
	maxConsoleNoticesRetained    = 100
	wakePromptPrefix             = "[delegated task "
	noticeSessionStartedSubstr   = "session started"
	noticeSessionRouteOnSubstr   = "session route on"
	noticeSessionStoppedSubstr   = "returning to platform"
	noticeSessionRouteOffSubstr  = "session route off"
	partKindText                 = "text"
	partKindData                 = "data"
	eventKindProactiveNotify     = "proactive_notify"
	eventKindTaskCompleted       = "task_completed"
	eventKindTaskFailed          = "task_failed"
	eventKindInputRequired       = "input_required"
	eventKindNoticePR            = "notice_pr"
	consumerGatewayRelay         = "gateway-relay"
	consumerBridgePlatform       = "bridge-platform"
	jsonNullLiteral              = "null"
	escapedUnicodeLt             = `\u003c`
	escapedUnicodeGt             = `\u003e`
	escapedUnicodeAmp            = `\u0026`
	literalLt                    = "<"
	literalGt                    = ">"
	literalAmp                   = "&"
)

var (
	allKnownStreams = []string{
		streamTasks,
		streamDirectory,
		streamTopicsState,
		streamTopicsJournal,
	}
	errNotConnected = errors.New("not connected to any kube-agents deployment")
)

// ProgressCallback receives live progress updates (step number and formatted status text)
// while an A2A turn is executing on the bus.
type ProgressCallback func(step int, message string)

type progressCallbackKey struct{}

// WithProgressCallback attaches a live ProgressCallback to ctx.
func WithProgressCallback(ctx context.Context, cb ProgressCallback) context.Context {
	if cb == nil {
		return ctx
	}
	return context.WithValue(ctx, progressCallbackKey{}, cb)
}

// WithoutProgressCallback returns a copy of ctx with any ProgressCallback removed.
func WithoutProgressCallback(ctx context.Context) context.Context {
	if ctx == nil {
		return nil
	}
	return context.WithValue(ctx, progressCallbackKey{}, nil)
}

func progressCallbackFromContext(ctx context.Context) ProgressCallback {
	if ctx == nil {
		return nil
	}
	if cb, ok := ctx.Value(progressCallbackKey{}).(ProgressCallback); ok {
		return cb
	}
	return nil
}

// ConsoleInFrame is the wire payload published to chat.console.<token>.in.
type ConsoleInFrame struct {
	MessageID string `json:"messageId"`
	Text      string `json:"text"`
	Kind      string `json:"kind,omitempty"`
}

// ConsoleOutFrame is the wire payload received on chat.console.<token>.out.
type ConsoleOutFrame struct {
	MessageID string `json:"messageId"`
	Text      string `json:"text"`
	Edit      any    `json:"edit,omitempty"`
	Timestamp time.Time
	Token     string
}

// ActivityItem represents a single tool invocation streamed via an activity artifact.
type ActivityItem struct {
	Tool       string `json:"tool"`
	Input      string `json:"input,omitempty"`
	CallID     string `json:"call_id,omitempty"`
	Status     string `json:"status,omitempty"`
	ErrorType  string `json:"error_type,omitempty"`
	DurationMs int64  `json:"duration_ms,omitempty"`
	At         string `json:"at,omitempty"`
}

// DelegateItem represents a delegation request from a session worker to another agent.
type DelegateItem struct {
	Addressee   string `json:"addressee"`
	Text        string `json:"text"`
	ChildTaskID string `json:"childTaskId,omitempty"`
}

// TaskRecord holds the folded state and rich streaming artifacts of a task on TASKS.
type TaskRecord struct {
	TaskID              string           `json:"taskId"`
	ContextID           string           `json:"contextId"`
	CorrelationID       string           `json:"correlationId,omitempty"`
	Addressee           string           `json:"addressee"`
	Conversation        string           `json:"conversation,omitempty"`
	DelegatedFromTaskID string           `json:"delegatedFromTaskId,omitempty"`
	WakeFromChildTaskID string           `json:"wakeFromChildTaskId,omitempty"`
	State               lib.TaskState    `json:"state"`
	Terminal            bool             `json:"terminal"`
	StatusText          string           `json:"statusText,omitempty"`
	PromptText          string           `json:"promptText,omitempty"`
	SteerTurns          []string         `json:"steerTurns,omitempty"`
	IntermediateTurns   []string         `json:"intermediateTurns,omitempty"`
	Thinking            string           `json:"thinking,omitempty"`
	Activities          []ActivityItem   `json:"activities,omitempty"`
	ProgressText        string           `json:"progressText,omitempty"`
	ProgressChat        *chatmsg.Message `json:"progressChat,omitempty"`
	Delegates           []DelegateItem   `json:"delegates,omitempty"`
	Notices             []string         `json:"notices,omitempty"`
	PRLinks             []string         `json:"prLinks,omitempty"`
	ResultText          string           `json:"resultText,omitempty"`
	ResultChat          *chatmsg.Message `json:"resultChat,omitempty"`
	CreatedAt           time.Time        `json:"createdAt"`
	UpdatedAt           time.Time        `json:"updatedAt"`
	EnvelopeCount       int              `json:"envelopeCount"`
}

// TopicSnapshot holds the latest envelope and parsed summary/data for a topic subject.
type TopicSnapshot struct {
	Stream        string          `json:"stream"`
	Subject       string          `json:"subject"`
	Topic         string          `json:"topic"`
	Class         string          `json:"class"`
	WriterSession string          `json:"writerSession"`
	WriterProfile string          `json:"writerProfile,omitempty"`
	TaskID        string          `json:"taskId,omitempty"`
	CorrelationID string          `json:"correlationId,omitempty"`
	Summary       string          `json:"summary,omitempty"`
	Data          json.RawMessage `json:"data,omitempty"`
	UpdatedAt     time.Time       `json:"updatedAt"`
}

// StreamHealth summarizes a JetStream stream's message/byte usage and consumer count.
type StreamHealth struct {
	Name      string   `json:"name"`
	Messages  uint64   `json:"messages"`
	Bytes     uint64   `json:"bytes"`
	MaxBytes  int64    `json:"maxBytes"`
	Consumers int      `json:"consumers"`
	Subjects  []string `json:"subjects,omitempty"`
}

// ConsumerHealth summarizes a JetStream durable consumer's liveness.
type ConsumerHealth struct {
	Stream        string `json:"stream"`
	Name          string `json:"name"`
	NumPending    uint64 `json:"numPending"`
	NumAckPending int    `json:"numAckPending"`
	PushBound     bool   `json:"pushBound"`
	LastActiveAgo string `json:"lastActiveAgo,omitempty"`
}

// BusStatusReport summarizes connection health, stream capacities, consumer liveness, and cards.
type BusStatusReport struct {
	Deployment       DeploymentProfile         `json:"deployment"`
	ConnectedURL     string                    `json:"connectedUrl"`
	BusUser          string                    `json:"busUser"`
	ObserverAttached bool                      `json:"observerAttached"`
	Streams          []StreamHealth            `json:"streams"`
	KeyConsumers     []ConsumerHealth          `json:"keyConsumers"`
	AgentCards       map[string]map[string]any `json:"agentCards"`
	TopicCount       int                       `json:"topicCount"`
	TaskCount        int                       `json:"taskCount"`
}

// TurnOutcome contains the complete result of sending a message or command to kube-agents.
type TurnOutcome struct {
	ConversationAlias string            `json:"conversationAlias"`
	ConversationToken string            `json:"conversationToken"`
	SentText          string            `json:"sentText"`
	ConsoleNotices    []ConsoleOutFrame `json:"consoleNotices,omitempty"`
	PrimaryTask       *TaskRecord       `json:"primaryTask,omitempty"`
	ChildTasks        []*TaskRecord     `json:"childTasks,omitempty"`
	WakeTask          *TaskRecord       `json:"wakeTask,omitempty"`
	TimedOut          bool              `json:"timedOut"`
	PrevStepCount     int               `json:"prevStepCount,omitempty"`
	ReportedStepCount int               `json:"reportedStepCount,omitempty"`
}

type authorityWire struct {
	Audience struct {
		Conversation string `json:"conversation"`
	} `json:"audience"`
	DelegatedFrom *struct {
		TaskID    string `json:"taskId"`
		ContextID string `json:"contextId"`
		Agent     string `json:"agent"`
	} `json:"delegatedFrom,omitempty"`
}

// BusSession represents a live connection to a kube-agents deployment's NATS JetStream bus.
type BusSession struct {
	profile      DeploymentProfile
	natsURL      string
	tunnel       *PortForwardTunnel
	nc           *nats.Conn
	js           jetstream.JetStream
	observerNC   *nats.Conn
	subs         []*nats.Subscription
	onPushEvent  func(PushEvent)
	bootstrapped bool

	mu                sync.RWMutex
	tasks             map[string]*TaskRecord
	seenEnvelopes     map[string]bool
	topics            map[string]TopicSnapshot
	agentCards        map[string]map[string]any
	consoleNotices    []ConsoleOutFrame
	watchedConvs      map[string]int
	watchedTasks      map[string]int
	prevReportedSteps map[string]int
	lastReportedSteps map[string]int
}

// ConnectBusSession dials a deployment's NATS JetStream bus (starting a local port-forward
// if DirectNATSURL is not set), loads historical state from the 4 streams, and subscribes
// to live events.
func ConnectBusSession(ctx context.Context, runner CommandRunner, prof DeploymentProfile, onPushEvent func(PushEvent)) (*BusSession, error) {
	if prof.DirectNATSURL == "" && prof.BusAuthValue == "" && prof.KubeContext != "" {
		_ = PopulateDeploymentCredentials(ctx, runner, &prof)
	}
	var tunnel *PortForwardTunnel
	natsURL := strings.TrimSpace(prof.DirectNATSURL)
	if natsURL == "" {
		if prof.KubeContext == "" || prof.Namespace == "" {
			return nil, errors.New("deployment profile requires either directNatsUrl or kubeContext + namespace")
		}
		var err error
		tunnel, err = StartPortForward(ctx, prof)
		if err != nil {
			return nil, err
		}
		natsURL = tunnel.NATSURL()
	}

	user := strings.TrimSpace(prof.BusUser)
	if user == "" {
		user = defaultBusUser
	}
	opts := []nats.Option{
		nats.Name(natsClientNamePrefix + user),
		nats.CustomInboxPrefix(inboxPrefixBase + user),
		nats.Timeout(natsConnectTimeout),
		nats.ReconnectWait(natsReconnectWait),
		nats.MaxReconnects(-1),
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, _ error) {}),
	}
	if prof.BusAuthValue != "" {
		opts = append(opts, nats.UserInfo(user, prof.BusAuthValue))
	}

	nc, err := nats.Connect(natsURL, opts...)
	if err != nil {
		if tunnel != nil {
			tunnel.Close()
		}
		return nil, fmt.Errorf("connecting to NATS bus at %s: %w", natsURL, err)
	}

	js, err := jetstream.New(nc)
	if err != nil {
		nc.Close()
		if tunnel != nil {
			tunnel.Close()
		}
		return nil, fmt.Errorf("initializing JetStream client: %w", err)
	}

	s := &BusSession{
		profile:       prof,
		natsURL:       natsURL,
		tunnel:        tunnel,
		nc:            nc,
		js:            js,
		onPushEvent:   onPushEvent,
		tasks:         make(map[string]*TaskRecord),
		seenEnvelopes: make(map[string]bool),
		topics:        make(map[string]TopicSnapshot),
		agentCards:    make(map[string]map[string]any),
		watchedConvs:      make(map[string]int),
		watchedTasks:      make(map[string]int),
		lastReportedSteps: make(map[string]int),
	}

	if err := s.startSubscriptions(); err != nil {
		s.Close()
		return nil, err
	}

	if prof.ObserverAuthValue != "" {
		obsOpts := []nats.Option{
			nats.Name(natsObserverClientName),
			nats.CustomInboxPrefix(inboxPrefixBase + observerUserGateway),
			nats.Timeout(natsConnectTimeout),
			nats.ReconnectWait(natsReconnectWait),
			nats.MaxReconnects(-1),
			nats.UserInfo(observerUserGateway, prof.ObserverAuthValue),
			nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, _ error) {}),
		}
		if obsNC, obsErr := nats.Connect(natsURL, obsOpts...); obsErr == nil {
			s.observerNC = obsNC
			if sub, subErr := obsNC.Subscribe(subjectAllNotify, s.handlePassiveNotify); subErr == nil {
				s.subs = append(s.subs, sub)
			}
		}
	}

	bootCtx, cancel := context.WithTimeout(ctx, bootstrapTimeout)
	s.bootstrapFromStreams(bootCtx)
	cancel()

	s.mu.Lock()
	s.bootstrapped = true
	s.mu.Unlock()

	return s, nil
}

// Profile returns the DeploymentProfile for this session.
func (s *BusSession) Profile() DeploymentProfile {
	return s.profile
}

// Close disconnects NATS connections and tears down any port-forward tunnel.
func (s *BusSession) Close() {
	for _, sub := range s.subs {
		_ = sub.Unsubscribe()
	}
	if s.observerNC != nil {
		s.observerNC.Close()
	}
	if s.nc != nil {
		s.nc.Close()
	}
	if s.tunnel != nil {
		s.tunnel.Close()
	}
}

func (s *BusSession) startSubscriptions() error {
	subA2A, err := s.nc.Subscribe(subjectAllA2A, func(msg *nats.Msg) {
		s.ingestA2AMessage(msg.Subject, msg.Data, true)
	})
	if err != nil {
		return fmt.Errorf("subscribing to %s: %w", subjectAllA2A, err)
	}
	s.subs = append(s.subs, subA2A)

	subConsole, err := s.nc.Subscribe(subjectAllConsoleOut, func(msg *nats.Msg) {
		s.ingestConsoleOut(msg.Subject, msg.Data)
	})
	if err != nil {
		return fmt.Errorf("subscribing to %s: %w", subjectAllConsoleOut, err)
	}
	s.subs = append(s.subs, subConsole)
	return s.nc.Flush()
}

func (s *BusSession) bootstrapFromStreams(ctx context.Context) {
	for _, streamName := range allKnownStreams {
		cons, err := s.js.OrderedConsumer(ctx, streamName, jetstream.OrderedConsumerConfig{
			DeliverPolicy:     jetstream.DeliverAllPolicy,
			InactiveThreshold: orderedConsumerIdleThreshold,
		})
		if err != nil {
			continue
		}
		info, infoErr := cons.Info(ctx)
		if infoErr != nil || info == nil {
			continue
		}
		pending := int(info.NumPending)
		if pending == 0 {
			continue
		}
		fetched := 0
		for fetched < pending && ctx.Err() == nil {
			batchSize := pending - fetched
			if batchSize > orderedFetchBatchSize {
				batchSize = orderedFetchBatchSize
			}
			msgs, fetchErr := cons.Fetch(batchSize, jetstream.FetchMaxWait(orderedFetchMaxWait))
			if fetchErr != nil {
				break
			}
			gotAny := false
			for msg := range msgs.Messages() {
				gotAny = true
				fetched++
				s.ingestA2AMessage(msg.Subject(), msg.Data(), false)
			}
			if !gotAny || msgs.Error() != nil {
				break
			}
		}
	}
}

func (s *BusSession) ingestA2AMessage(subject string, data []byte, isLive bool) {
	switch {
	case strings.HasPrefix(subject, subjectTasksPrefix):
		s.ingestTaskEnvelope(subject, data, isLive)
	case strings.HasPrefix(subject, subjectAgentsPrefix) && strings.HasSuffix(subject, subjectAgentsCardSuffix):
		s.ingestAgentCard(subject, data)
	case strings.HasPrefix(subject, subjectTopicsPrefix):
		s.ingestTopicEnvelope(subject, data)
	}
}

func (s *BusSession) ingestTaskEnvelope(subject string, data []byte, isLive bool) {
	var env lib.Envelope
	if err := json.Unmarshal(data, &env); err != nil || env.TaskID == "" {
		return
	}

	s.mu.Lock()
	if env.EnvelopeID != "" && s.seenEnvelopes[env.EnvelopeID] {
		s.mu.Unlock()
		return
	}
	if env.EnvelopeID != "" {
		s.seenEnvelopes[env.EnvelopeID] = true
	}

	addressee := extractTaskAddressee(subject)
	rec, exists := s.tasks[env.TaskID]
	if !exists {
		rec = &TaskRecord{
			TaskID:        env.TaskID,
			ContextID:     env.ContextID,
			CorrelationID: env.CorrelationID,
			Addressee:     addressee,
			State:         lib.StateSubmitted,
			CreatedAt:     env.TS,
			UpdatedAt:     env.TS,
		}
		s.tasks[env.TaskID] = rec
	}
	if rec.ContextID == "" {
		rec.ContextID = env.ContextID
	}
	if rec.CorrelationID == "" {
		rec.CorrelationID = env.CorrelationID
	}
	if rec.Addressee == "" && addressee != "" {
		rec.Addressee = addressee
	}
	if env.TS.After(rec.UpdatedAt) {
		rec.UpdatedAt = env.TS
	}
	rec.EnvelopeCount++

	if len(env.Authority) > 0 {
		var auth authorityWire
		if json.Unmarshal(env.Authority, &auth) == nil {
			if auth.Audience.Conversation != "" && rec.Conversation == "" {
				rec.Conversation = auth.Audience.Conversation
			}
			if auth.DelegatedFrom != nil && auth.DelegatedFrom.TaskID != "" && rec.DelegatedFromTaskID == "" {
				rec.DelegatedFromTaskID = auth.DelegatedFrom.TaskID
				if parent, ok := s.tasks[rec.DelegatedFromTaskID]; ok {
					for i := range parent.Delegates {
						if parent.Delegates[i].ChildTaskID == "" {
							parent.Delegates[i].ChildTaskID = rec.TaskID
							break
						}
					}
				}
			}
		}
	}

	var pushToFire *PushEvent
	watched := s.isWatchedLocked(rec.Conversation, rec.TaskID, rec.DelegatedFromTaskID)

	switch env.Kind {
	case lib.KindMessage:
		var msg lib.Message
		if json.Unmarshal(env.Payload, &msg) == nil {
			text := joinMessageText(msg.Parts)
			if rec.PromptText == "" {
				rec.PromptText = text
				if childID := extractWakeChildTaskID(text); childID != "" {
					rec.WakeFromChildTaskID = childID
				}
			} else if text != "" {
				rec.SteerTurns = append(rec.SteerTurns, text)
			}
		}
	case lib.KindStatusUpdate:
		var st lib.StatusUpdate
		if json.Unmarshal(env.Payload, &st) == nil {
			prevTerminal := rec.Terminal
			prevState := rec.State
			rec.State = st.Status.State
			rec.Terminal = st.Final || st.Status.State.Terminal()
			if st.Status.Message != nil {
				rec.StatusText = joinMessageText(st.Status.Message.Parts)
			}
			if isLive && s.bootstrapped && !watched {
				if rec.Terminal && !prevTerminal {
					kind := eventKindTaskCompleted
					title := fmt.Sprintf("Task %s (%s) %s", rec.TaskID, rec.Addressee, rec.State)
					if rec.State == lib.StateFailed || rec.State == lib.StateRejected || rec.State == lib.StateCanceled {
						kind = eventKindTaskFailed
					}
					summary := rec.ResultText
					if summary == "" {
						summary = rec.StatusText
					}
					if summary == "" {
						summary = rec.ProgressText
					}
					pushToFire = &PushEvent{
						ID:           fmt.Sprintf("task-term-%s-%s", rec.TaskID, rec.State),
						Kind:         kind,
						Deployment:   s.profile.Name,
						Conversation: rec.Conversation,
						TaskID:       rec.TaskID,
						ContextID:    rec.ContextID,
						Subject:      subject,
						Title:        title,
						Summary:      truncateText(summary, pushSummaryMaxChars),
						PRLinks:      append([]string(nil), rec.PRLinks...),
						Timestamp:    env.TS,
					}
				} else if st.Status.State == lib.StateInputRequired && prevState != lib.StateInputRequired {
					pushToFire = &PushEvent{
						ID:           fmt.Sprintf("task-input-%s-%d", rec.TaskID, rec.EnvelopeCount),
						Kind:         eventKindInputRequired,
						Deployment:   s.profile.Name,
						Conversation: rec.Conversation,
						TaskID:       rec.TaskID,
						ContextID:    rec.ContextID,
						Subject:      subject,
						Title:        fmt.Sprintf("Input Required on Task %s (%s)", rec.TaskID, rec.Addressee),
						Summary:      firstNonEmptyStr(rec.StatusText, rec.ProgressText, rec.ResultText),
						Timestamp:    env.TS,
					}
				}
			}
		}
	case lib.KindArtifactUpdate:
		var au lib.ArtifactUpdate
		if json.Unmarshal(env.Payload, &au) == nil {
			art := au.Artifact
			text := joinMessageText(art.Parts)
			dataText := joinDataOrTextParts(art.Parts)
			chatMsg := extractChatMessage(art.Parts)
			switch art.Name {
			case lib.ArtifactThinking:
				if au.Append && rec.Thinking != "" && text != "" {
					if !strings.HasSuffix(rec.Thinking, "\n") && !strings.HasPrefix(text, "\n") {
						rec.Thinking += "\n"
					}
					rec.Thinking += text
				} else if au.Append {
					rec.Thinking += text
				} else {
					rec.Thinking = text
				}
			case lib.ArtifactActivity:
				for _, p := range art.Parts {
					raw := ""
					if p.Kind == partKindData && len(p.Data) > 0 {
						raw = string(p.Data)
					} else if p.Kind == partKindText && p.Text != "" {
						raw = p.Text
					}
					if item, ok := parseActivityItem(raw); ok {
						rec.Activities = upsertActivityItem(rec.Activities, item)
					}
				}
			case lib.ArtifactProgress:
				if au.Append {
					rec.ProgressText += text
				} else {
					rec.ProgressText = text
				}
				if chatMsg != nil {
					rec.ProgressChat = chatMsg
				}
			case lib.ArtifactDelegate:
				var del DelegateItem
				if json.Unmarshal([]byte(dataText), &del) == nil && del.Addressee != "" {
					rec.Delegates = append(rec.Delegates, del)
				}
			case lib.ArtifactNotice:
				if text != "" {
					rec.Notices = append(rec.Notices, text)
					prs := ExtractPRReferences(text)
					for _, pr := range prs {
						if !containsString(rec.PRLinks, pr) {
							rec.PRLinks = append(rec.PRLinks, pr)
						}
					}
					if isLive && s.bootstrapped && !watched && len(prs) > 0 {
						pushToFire = &PushEvent{
							ID:           fmt.Sprintf("notice-pr-%s-%d", rec.TaskID, len(rec.Notices)),
							Kind:         eventKindNoticePR,
							Deployment:   s.profile.Name,
							Conversation: rec.Conversation,
							TaskID:       rec.TaskID,
							ContextID:    rec.ContextID,
							Subject:      subject,
							Title:        fmt.Sprintf("Pull Request Notice on Task %s", rec.TaskID),
							Summary:      text,
							PRLinks:      prs,
							Timestamp:    env.TS,
						}
					}
				}
			case lib.ArtifactTurn:
				if text != "" {
					rec.IntermediateTurns = append(rec.IntermediateTurns, text)
				}
			case lib.ArtifactResult:
				if au.Append {
					rec.ResultText += text
				} else {
					rec.ResultText = text
				}
				if chatMsg != nil {
					rec.ResultChat = chatMsg
					for _, lnk := range chatMsg.Links {
						for _, pr := range ExtractPRReferences(lnk.URL) {
							if !containsString(rec.PRLinks, pr) {
								rec.PRLinks = append(rec.PRLinks, pr)
							}
						}
					}
				}
				for _, pr := range ExtractPRReferences(rec.ResultText) {
					if !containsString(rec.PRLinks, pr) {
						rec.PRLinks = append(rec.PRLinks, pr)
					}
				}
			}
		}
	}
	s.mu.Unlock()

	if pushToFire != nil && s.onPushEvent != nil {
		s.onPushEvent(*pushToFire)
	}
}

func (s *BusSession) isWatchedLocked(conv, taskID, parentTaskID string) bool {
	if conv != "" && s.watchedConvs[conv] > 0 {
		return true
	}
	if taskID != "" && s.watchedTasks[taskID] > 0 {
		return true
	}
	if parentTaskID != "" && s.watchedTasks[parentTaskID] > 0 {
		return true
	}
	return false
}

func (s *BusSession) ingestAgentCard(subject string, data []byte) {
	var env lib.Envelope
	if err := json.Unmarshal(data, &env); err != nil {
		return
	}
	var card map[string]any
	if err := json.Unmarshal(env.Payload, &card); err != nil {
		return
	}
	name := strings.TrimPrefix(subject, subjectAgentsPrefix)
	name = strings.TrimSuffix(name, subjectAgentsCardSuffix)
	s.mu.Lock()
	s.agentCards[name] = card
	s.mu.Unlock()
}

func (s *BusSession) ingestTopicEnvelope(subject string, data []byte) {
	var env lib.Envelope
	if err := json.Unmarshal(data, &env); err != nil {
		return
	}
	var art lib.Artifact
	if err := json.Unmarshal(env.Payload, &art); err != nil {
		return
	}
	var summary string
	var rawData json.RawMessage
	for _, p := range art.Parts {
		switch p.Kind {
		case partKindText:
			summary = strings.TrimSpace(p.Text)
		case partKindData:
			rawData = p.Data
		}
	}
	stream := streamTopicsState
	class := topicClassState
	if strings.HasPrefix(subject, subjectTopicsJournalPrefix) {
		stream = streamTopicsJournal
		class = topicClassJournal
	}
	topicName := art.Name
	if topicName == "" {
		parts := strings.Split(subject, ".")
		if len(parts) > 0 {
			topicName = parts[len(parts)-1]
		}
	}
	snap := TopicSnapshot{
		Stream:        stream,
		Subject:       subject,
		Topic:         topicName,
		Class:         class,
		WriterSession: env.From.Session,
		WriterProfile: env.From.Profile,
		TaskID:        env.TaskID,
		CorrelationID: env.CorrelationID,
		Summary:       summary,
		Data:          rawData,
		UpdatedAt:     env.TS,
	}
	s.mu.Lock()
	s.topics[subject] = snap
	s.mu.Unlock()
}

func (s *BusSession) ingestConsoleOut(subject string, data []byte) {
	var frame ConsoleOutFrame
	if err := json.Unmarshal(data, &frame); err != nil {
		return
	}
	token := strings.TrimPrefix(subject, subjectConsoleOutPrefix)
	token = strings.TrimSuffix(token, subjectConsoleOutSuffix)
	frame.Token = token
	frame.Timestamp = time.Now().UTC()

	isEdit := false
	switch v := frame.Edit.(type) {
	case bool:
		isEdit = v
	case string:
		isEdit = strings.TrimSpace(v) != ""
	}

	s.mu.Lock()
	if isEdit && frame.MessageID != "" {
		updated := false
		for i := len(s.consoleNotices) - 1; i >= 0; i-- {
			if s.consoleNotices[i].Token == token && s.consoleNotices[i].MessageID == frame.MessageID {
				s.consoleNotices[i].Text = frame.Text
				s.consoleNotices[i].Timestamp = frame.Timestamp
				updated = true
				break
			}
		}
		if !updated {
			s.consoleNotices = append(s.consoleNotices, frame)
		}
	} else {
		s.consoleNotices = append(s.consoleNotices, frame)
	}
	if len(s.consoleNotices) > maxConsoleNoticesRetained {
		s.consoleNotices = s.consoleNotices[len(s.consoleNotices)-maxConsoleNoticesRetained:]
	}
	s.mu.Unlock()
}

func (s *BusSession) handlePassiveNotify(msg *nats.Msg) {
	var req lib.NotifyRequest
	if err := json.Unmarshal(msg.Data, &req); err != nil {
		return
	}
	title := defaultNotifyTitle
	summary := strings.TrimSpace(req.Text)
	var parsedChat *chatmsg.Message
	if len(req.Chat) > 0 {
		if cm, cErr := chatmsg.Parse(req.Chat); cErr == nil {
			parsedChat = &cm
		}
	}
	if parsedChat != nil {
		if parsedChat.Headline != "" {
			title = parsedChat.Headline
		}
		if summary == "" {
			summary = parsedChat.Detail
		}
	}
	if summary == "" {
		summary = title
	}
	prs := ExtractPRReferences(summary)
	if parsedChat != nil {
		for _, lnk := range parsedChat.Links {
			for _, pr := range ExtractPRReferences(lnk.URL) {
				if !containsString(prs, pr) {
					prs = append(prs, pr)
				}
			}
		}
	}
	if s.onPushEvent != nil {
		s.onPushEvent(PushEvent{
			ID:           fmt.Sprintf("notify-%d", time.Now().UnixNano()),
			Kind:         eventKindProactiveNotify,
			Deployment:   s.profile.Name,
			Conversation: req.Conversation,
			ContextID:    req.ContextID,
			Subject:      msg.Subject,
			Title:        title,
			Summary:      summary,
			PRLinks:      prs,
			Raw:          append(json.RawMessage(nil), msg.Data...),
			Timestamp:    time.Now().UTC(),
		})
	}
}

// SendTurn publishes a message or control command to chat.console.<token>.in
// and streams back all thinking, tool activities, progress, delegation children,
// wake turns, and results.
func (s *BusSession) SendTurn(ctx context.Context, store *ConfigStore, conv ConversationEntry, rawText string, mode string, waitTimeout time.Duration) (*TurnOutcome, error) {
	return s.SendTurnWithOptions(ctx, store, conv, rawText, mode, waitTimeout, false)
}

// SendTurnWithOptions publishes a message or control command to chat.console.<token>.in.
// When streamSteps is true, it waits for the first step batch (or completion if fast) and
// returns immediately so Jetski can render incremental collapsed step updates as the task runs.
func (s *BusSession) SendTurnWithOptions(ctx context.Context, store *ConfigStore, conv ConversationEntry, rawText string, mode string, waitTimeout time.Duration, streamSteps bool) (*TurnOutcome, error) {
	if waitTimeout <= 0 {
		waitTimeout = defaultTurnWaitTimeout
	}
	text := strings.TrimSpace(rawText)
	if text == "" {
		return nil, errors.New("message text cannot be empty")
	}

	mode = strings.ToLower(strings.TrimSpace(mode))
	if mode == "" {
		mode = modeAuto
	}

	convName := consoleConvPrefix + conv.Token
	s.watchConv(convName, 1)
	defer s.watchConv(convName, -1)

	outcome := &TurnOutcome{
		ConversationAlias: conv.Alias,
		ConversationToken: conv.Token,
		SentText:          text,
	}

	if mode == modeSession && !conv.SessionMode && text != cmdSessionOn && text != cmdSessionOff {
		notices, err := s.sendControlCommand(ctx, conv.Token, cmdSessionOn)
		outcome.ConsoleNotices = append(outcome.ConsoleNotices, notices...)
		if err == nil && store != nil {
			_, _ = store.SetConversationSessionMode(conv.Alias, true)
		}
	} else if mode == modePlatform && conv.SessionMode && text != cmdSessionOff && text != cmdSessionOn {
		notices, err := s.sendControlCommand(ctx, conv.Token, cmdSessionOff)
		outcome.ConsoleNotices = append(outcome.ConsoleNotices, notices...)
		if err == nil && store != nil {
			_, _ = store.SetConversationSessionMode(conv.Alias, false)
		}
	}

	if mode == modeDelegate && !strings.HasPrefix(strings.ToLower(text), cmdDelegatePrefix) {
		text = cmdDelegatePrefix + " " + text
		outcome.SentText = text
	}

	if isPureControlCommand(text) {
		notices, err := s.sendControlCommand(ctx, conv.Token, text)
		outcome.ConsoleNotices = append(outcome.ConsoleNotices, notices...)
		if err != nil {
			return outcome, err
		}
		if store != nil {
			for _, n := range notices {
				lower := strings.ToLower(n.Text)
				if strings.Contains(lower, noticeSessionStartedSubstr) || strings.Contains(lower, noticeSessionRouteOnSubstr) {
					_, _ = store.SetConversationSessionMode(conv.Alias, true)
				} else if strings.Contains(lower, noticeSessionStoppedSubstr) || strings.Contains(lower, noticeSessionRouteOffSubstr) {
					_, _ = store.SetConversationSessionMode(conv.Alias, false)
				}
			}
		}
		return outcome, nil
	}

	baselineTasks := s.snapshotTaskIDsForConv(convName)
	activeBefore := s.findActiveTaskForConv(convName)
	noticeCursor := s.consoleNoticeCursor()
	preNotices := append([]ConsoleOutFrame(nil), outcome.ConsoleNotices...)

	if err := s.publishConsoleFrame(conv.Token, text); err != nil {
		return nil, err
	}

	if cb := progressCallbackFromContext(ctx); cb != nil {
		cb(1, fmt.Sprintf("📨 Sent message to kube-agents (`%s` / `console:%s`) — waiting for task acceptance on `TASKS`...", conv.Alias, conv.Token))
	}

	turnCtx, cancel := context.WithTimeout(ctx, waitTimeout)
	defer cancel()

	primaryID, err := s.awaitPrimaryTask(turnCtx, convName, baselineTasks, activeBefore, conv.Token, noticeCursor)
	outcome.ConsoleNotices = append(preNotices, s.consoleNoticesSince(conv.Token, noticeCursor)...)
	if err != nil || primaryID == "" {
		if len(outcome.ConsoleNotices) > 0 {
			return outcome, nil
		}
		return outcome, err
	}

	s.resetStepProgress(primaryID)
	timedOut := s.WaitForTaskStepOrSettlement(turnCtx, primaryID, streamSteps)
	built := s.BuildTaskOutcome(primaryID, conv.Alias, conv.Token, text, timedOut)
	built.ConsoleNotices = append(preNotices, s.consoleNoticesSince(conv.Token, noticeCursor)...)

	if cb := progressCallbackFromContext(ctx); cb != nil && built.PrimaryTask != nil {
		cb(999, FormatLiveTaskProgress(built.PrimaryTask, built.ChildTasks, built.WakeTask, time.Since(built.PrimaryTask.CreatedAt)))
	}

	return built, nil
}

// BuildTaskOutcome constructs a TurnOutcome for a primary task ID, including any delegated child
// tasks and session wake synthesis task.
func (s *BusSession) BuildTaskOutcome(primaryID string, convAlias string, convToken string, sentText string, timedOut bool) *TurnOutcome {
	primary := s.GetTask(primaryID)
	prevSteps, reportedSteps := s.getStepProgress(primaryID)
	outcome := &TurnOutcome{
		ConversationAlias: convAlias,
		ConversationToken: convToken,
		SentText:          sentText,
		PrimaryTask:       primary,
		TimedOut:          timedOut,
		PrevStepCount:     prevSteps,
		ReportedStepCount: reportedSteps,
	}
	if primary == nil {
		return outcome
	}
	if outcome.ConversationToken == "" && strings.HasPrefix(primary.Conversation, consoleConvPrefix) {
		outcome.ConversationToken = strings.TrimPrefix(primary.Conversation, consoleConvPrefix)
	}
	children := s.findChildTasks(primaryID)
	outcome.ChildTasks = children
	for _, child := range children {
		if wake := s.findWakeTask(child.TaskID); wake != nil {
			outcome.WakeTask = wake
		}
	}
	return outcome
}

// IsTaskTreeSettled returns true if the primary task and any expected delegated child / wake tasks
// have reached a terminal or input-required state.
func (s *BusSession) IsTaskTreeSettled(primaryID string) bool {
	primary := s.GetTask(primaryID)
	if primary == nil {
		return false
	}
	if !primary.Terminal && primary.State != lib.StateInputRequired {
		return false
	}
	children := s.findChildTasks(primaryID)
	if len(primary.Delegates) > 0 && len(children) == 0 {
		if time.Since(primary.UpdatedAt) < delegationChildWait {
			return false
		}
		return true
	}
	if len(children) == 0 {
		return true
	}
	var wake *TaskRecord
	for _, child := range children {
		if !child.Terminal && child.State != lib.StateInputRequired {
			return false
		}
		if w := s.findWakeTask(child.TaskID); w != nil {
			wake = w
		}
	}
	if wake == nil && (len(primary.Delegates) > 0 || strings.HasPrefix(primary.Addressee, "s-")) {
		lastChild := children[len(children)-1]
		if time.Since(lastChild.UpdatedAt) < delegationWakeWait {
			return false
		}
		return true
	}
	if wake != nil && !wake.Terminal && wake.State != lib.StateInputRequired {
		return false
	}
	return true
}

// WaitForTaskStepOrSettlement waits for a task tree. When streamSteps is false, it waits until
// full settlement or ctx expiration. When streamSteps is true, it waits until either the task tree
// settles OR the next human-readable step arrives (advancing by 1 step at a time so each step
// appends a single item to Jetski's collapsible step list rather than creating duplicate sub-lists),
// returning true if the task is still in flight (timedOut).
func (s *BusSession) WaitForTaskStepOrSettlement(ctx context.Context, primaryID string, streamSteps bool) bool {
	s.watchTask(primaryID, 1)
	defer s.watchTask(primaryID, -1)

	baseline := s.getReportedStepCount(primaryID)
	if !streamSteps {
		timedOut := s.waitForTaskAndDelegations(ctx, primaryID)
		steps := CollectOutcomeSteps(s.BuildTaskOutcome(primaryID, "", "", "", timedOut))
		s.recordStepProgress(primaryID, baseline, len(steps))
		return timedOut
	}

	window := defaultStepStreamWindow
	if baseline == 0 {
		window = initialStepStreamWindow
	}
	stepCtx, cancel := context.WithTimeout(ctx, window)
	defer cancel()

	ticker := time.NewTicker(pollTickInterval)
	defer ticker.Stop()

	startedAt := time.Now()
	firstSeenAboveBaseline := time.Time{}

	for {
		settled := s.IsTaskTreeSettled(primaryID)
		steps := CollectOutcomeSteps(s.BuildTaskOutcome(primaryID, "", "", "", !settled))
		curCount := len(steps)

		if settled {
			s.recordStepProgress(primaryID, baseline, curCount)
			return false
		}

		now := time.Now()
		if curCount > baseline {
			if firstSeenAboveBaseline.IsZero() {
				firstSeenAboveBaseline = now
			}
			if now.Sub(startedAt) >= minStepBatchWait && now.Sub(firstSeenAboveBaseline) >= stepDebounceWait {
				s.recordStepProgress(primaryID, baseline, baseline+1)
				return true
			}
		}

		select {
		case <-stepCtx.Done():
			finalSettled := s.IsTaskTreeSettled(primaryID)
			finalSteps := CollectOutcomeSteps(s.BuildTaskOutcome(primaryID, "", "", "", !finalSettled))
			if finalSettled {
				s.recordStepProgress(primaryID, baseline, len(finalSteps))
				return false
			}
			nextCount := baseline
			if len(finalSteps) > baseline {
				nextCount = baseline + 1
			}
			s.recordStepProgress(primaryID, baseline, nextCount)
			return true
		case <-ticker.C:
		}
	}
}

func (s *BusSession) getReportedStepCount(taskID string) int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.lastReportedSteps == nil {
		return 0
	}
	return s.lastReportedSteps[taskID]
}

func (s *BusSession) getStepProgress(taskID string) (prev int, reported int) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.prevReportedSteps != nil {
		prev = s.prevReportedSteps[taskID]
	}
	if s.lastReportedSteps != nil {
		reported = s.lastReportedSteps[taskID]
	}
	return prev, reported
}

func (s *BusSession) resetStepProgress(taskID string) {
	s.recordStepProgress(taskID, 0, 0)
}

func (s *BusSession) recordStepProgress(taskID string, prev int, reported int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.prevReportedSteps == nil {
		s.prevReportedSteps = make(map[string]int)
	}
	if s.lastReportedSteps == nil {
		s.lastReportedSteps = make(map[string]int)
	}
	s.prevReportedSteps[taskID] = prev
	s.lastReportedSteps[taskID] = reported
}

func (s *BusSession) sendControlCommand(ctx context.Context, token string, cmdText string) ([]ConsoleOutFrame, error) {
	cursor := s.consoleNoticeCursor()
	if err := s.publishConsoleFrame(token, cmdText); err != nil {
		return nil, err
	}
	waitCtx, cancel := context.WithTimeout(ctx, controlNoticeWait)
	defer cancel()
	ticker := time.NewTicker(pollTickInterval)
	defer ticker.Stop()
	for {
		notices := s.consoleNoticesSince(token, cursor)
		if len(notices) > 0 {
			return notices, nil
		}
		select {
		case <-waitCtx.Done():
			return s.consoleNoticesSince(token, cursor), nil
		case <-ticker.C:
		}
	}
}

func (s *BusSession) publishConsoleFrame(token string, text string) error {
	frame := ConsoleInFrame{
		MessageID: fmt.Sprintf("%s%d", msgIDPrefix, time.Now().UnixNano()),
		Text:      text,
		Kind:      consoleFrameKindText,
	}
	raw, err := json.Marshal(frame)
	if err != nil {
		return err
	}
	subj := subjectConsoleInPrefix + token + subjectConsoleInSuffix
	if err := s.nc.Publish(subj, raw); err != nil {
		return fmt.Errorf("publishing to %s: %w", subj, err)
	}
	return s.nc.Flush()
}

func (s *BusSession) awaitPrimaryTask(
	ctx context.Context,
	convName string,
	baseline map[string]int,
	activeBefore *TaskRecord,
	token string,
	noticeCursor int,
) (string, error) {
	ticker := time.NewTicker(pollTickInterval)
	defer ticker.Stop()

	for {
		s.mu.RLock()
		for id, rec := range s.tasks {
			if rec.Conversation == convName && rec.DelegatedFromTaskID == "" && rec.WakeFromChildTaskID == "" {
				if _, existed := baseline[id]; !existed {
					s.mu.RUnlock()
					return id, nil
				}
			}
		}
		if activeBefore != nil {
			if cur, ok := s.tasks[activeBefore.TaskID]; ok && cur.EnvelopeCount > baseline[activeBefore.TaskID] {
				s.mu.RUnlock()
				return activeBefore.TaskID, nil
			}
		}
		s.mu.RUnlock()

		select {
		case <-ctx.Done():
			if activeBefore != nil {
				return activeBefore.TaskID, nil
			}
			if len(s.consoleNoticesSince(token, noticeCursor)) > 0 {
				return "", nil
			}
			return "", fmt.Errorf("timed out waiting for task creation on %s", convName)
		case <-ticker.C:
		}
	}
}

func (s *BusSession) waitForTaskAndDelegations(ctx context.Context, primaryID string) bool {
	if !s.waitUntilTaskSettled(ctx, primaryID) {
		return true
	}
	primary := s.GetTask(primaryID)
	if primary == nil {
		return false
	}

	children := s.findChildTasks(primaryID)
	if len(children) == 0 && len(primary.Delegates) > 0 {
		childWaitCtx, cancel := context.WithTimeout(ctx, delegationChildWait)
		children = s.awaitChildTasks(childWaitCtx, primaryID)
		cancel()
	}
	if len(children) == 0 {
		return false
	}

	for _, child := range children {
		s.watchTask(child.TaskID, 1)
		settled := s.waitUntilTaskSettled(ctx, child.TaskID)
		s.watchTask(child.TaskID, -1)
		if !settled {
			return true
		}
		wakeWaitCtx, cancel := context.WithTimeout(ctx, delegationWakeWait)
		wake := s.awaitWakeTask(wakeWaitCtx, child.TaskID)
		cancel()
		if wake != nil {
			s.watchTask(wake.TaskID, 1)
			wakeSettled := s.waitUntilTaskSettled(ctx, wake.TaskID)
			s.watchTask(wake.TaskID, -1)
			if !wakeSettled {
				return true
			}
		}
	}
	return false
}

func (s *BusSession) waitUntilTaskSettled(ctx context.Context, taskID string) bool {
	ticker := time.NewTicker(pollTickInterval)
	defer ticker.Stop()
	cb := progressCallbackFromContext(ctx)
	startedWait := time.Now()
	lastEnvCount := -1
	lastReportAt := time.Time{}
	step := 1
	for {
		rec := s.GetTask(taskID)
		if rec != nil {
			now := time.Now()
			if cb != nil && (rec.EnvelopeCount != lastEnvCount || now.Sub(lastReportAt) >= progressHeartbeatInterval) {
				step++
				lastEnvCount = rec.EnvelopeCount
				lastReportAt = now
				elapsed := now.Sub(startedWait)
				if !rec.CreatedAt.IsZero() && now.After(rec.CreatedAt) {
					elapsed = now.Sub(rec.CreatedAt)
				}
				children := s.findChildTasks(taskID)
				var wake *TaskRecord
				for _, ch := range children {
					if w := s.findWakeTask(ch.TaskID); w != nil {
						wake = w
					}
				}
				cb(step, FormatLiveTaskProgress(rec, children, wake, elapsed))
			}
			if rec.Terminal || rec.State == lib.StateInputRequired {
				return true
			}
		}
		select {
		case <-ctx.Done():
			return false
		case <-ticker.C:
		}
	}
}

func (s *BusSession) awaitChildTasks(ctx context.Context, parentID string) []*TaskRecord {
	ticker := time.NewTicker(pollTickInterval)
	defer ticker.Stop()
	for {
		if children := s.findChildTasks(parentID); len(children) > 0 {
			return children
		}
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
		}
	}
}

func (s *BusSession) awaitWakeTask(ctx context.Context, childID string) *TaskRecord {
	ticker := time.NewTicker(pollTickInterval)
	defer ticker.Stop()
	for {
		if wake := s.findWakeTask(childID); wake != nil {
			return wake
		}
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
		}
	}
}

// GetTask returns a deep copy of a TaskRecord by task ID.
func (s *BusSession) GetTask(taskID string) *TaskRecord {
	s.mu.RLock()
	defer s.mu.RUnlock()
	rec, ok := s.tasks[taskID]
	if !ok {
		return nil
	}
	return cloneTaskRecord(rec)
}

// ListTasks returns recent TaskRecords, optionally filtered by conversation or non-terminal state.
func (s *BusSession) ListTasks(conversationFilter string, onlyActive bool, limit int) []*TaskRecord {
	s.mu.RLock()
	defer s.mu.RUnlock()
	var list []*TaskRecord
	for _, rec := range s.tasks {
		if conversationFilter != "" && rec.Conversation != conversationFilter && rec.Conversation != consoleConvPrefix+conversationFilter {
			continue
		}
		if onlyActive && rec.Terminal {
			continue
		}
		list = append(list, cloneTaskRecord(rec))
	}
	sort.Slice(list, func(i, j int) bool {
		return list[i].UpdatedAt.After(list[j].UpdatedAt)
	})
	if limit > 0 && len(list) > limit {
		list = list[:limit]
	}
	return list
}

// ListTopics returns all known TopicSnapshots sorted by topic name.
func (s *BusSession) ListTopics() []TopicSnapshot {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]TopicSnapshot, 0, len(s.topics))
	for _, t := range s.topics {
		out = append(out, t)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Topic != out[j].Topic {
			return out[i].Topic < out[j].Topic
		}
		return out[i].Subject < out[j].Subject
	})
	return out
}

// ReadTopic finds a TopicSnapshot matching the given topic name, scope.topic, or full subject.
func (s *BusSession) ReadTopic(nameOrSubject string) (*TopicSnapshot, error) {
	query := strings.TrimSpace(nameOrSubject)
	s.mu.RLock()
	defer s.mu.RUnlock()
	if snap, ok := s.topics[query]; ok {
		cp := snap
		return &cp, nil
	}
	var matches []TopicSnapshot
	for _, snap := range s.topics {
		if snap.Topic == query || strings.HasSuffix(snap.Subject, "."+query) {
			matches = append(matches, snap)
		}
	}
	if len(matches) == 1 {
		return &matches[0], nil
	}
	if len(matches) > 1 {
		sort.Slice(matches, func(i, j int) bool {
			return matches[i].UpdatedAt.After(matches[j].UpdatedAt)
		})
		return &matches[0], nil
	}
	return nil, fmt.Errorf("topic %q has no entries or was not found", query)
}

// AgentCards returns a copy of all DIRECTORY agent cards.
func (s *BusSession) AgentCards() map[string]map[string]any {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make(map[string]map[string]any, len(s.agentCards))
	for k, v := range s.agentCards {
		out[k] = v
	}
	return out
}

// StatusReport queries JetStream stream & consumer health and returns a BusStatusReport.
func (s *BusSession) StatusReport(ctx context.Context) BusStatusReport {
	report := BusStatusReport{
		Deployment:       s.profile,
		ConnectedURL:     s.natsURL,
		BusUser:          firstNonEmptyStr(s.profile.BusUser, defaultBusUser),
		ObserverAttached: s.observerNC != nil && s.observerNC.IsConnected(),
		AgentCards:       s.AgentCards(),
	}
	s.mu.RLock()
	report.TopicCount = len(s.topics)
	report.TaskCount = len(s.tasks)
	s.mu.RUnlock()

	for _, name := range allKnownStreams {
		st, err := s.js.Stream(ctx, name)
		if err != nil {
			continue
		}
		info, err := st.Info(ctx)
		if err != nil || info == nil {
			continue
		}
		report.Streams = append(report.Streams, StreamHealth{
			Name:      name,
			Messages:  info.State.Msgs,
			Bytes:     info.State.Bytes,
			MaxBytes:  info.Config.MaxBytes,
			Consumers: info.State.Consumers,
			Subjects:  info.Config.Subjects,
		})
		if name == streamTasks {
			for _, cname := range []string{consumerGatewayRelay, consumerBridgePlatform} {
				if cons, cErr := st.Consumer(ctx, cname); cErr == nil {
					if cInfo, ciErr := cons.Info(ctx); ciErr == nil && cInfo != nil {
						report.KeyConsumers = append(report.KeyConsumers, ConsumerHealth{
							Stream:        name,
							Name:          cInfo.Name,
							NumPending:    cInfo.NumPending,
							NumAckPending: cInfo.NumAckPending,
							PushBound:     cInfo.PushBound || cInfo.NumWaiting > 0,
						})
					}
				}
			}
		}
	}
	return report
}

func (s *BusSession) watchConv(conv string, delta int) {
	s.mu.Lock()
	s.watchedConvs[conv] += delta
	if s.watchedConvs[conv] <= 0 {
		delete(s.watchedConvs, conv)
	}
	s.mu.Unlock()
}

func (s *BusSession) watchTask(taskID string, delta int) {
	s.mu.Lock()
	s.watchedTasks[taskID] += delta
	if s.watchedTasks[taskID] <= 0 {
		delete(s.watchedTasks, taskID)
	}
	s.mu.Unlock()
}

func (s *BusSession) snapshotTaskIDsForConv(conv string) map[string]int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make(map[string]int)
	for id, rec := range s.tasks {
		if rec.Conversation == conv {
			out[id] = rec.EnvelopeCount
		}
	}
	return out
}

func (s *BusSession) findActiveTaskForConv(conv string) *TaskRecord {
	s.mu.RLock()
	defer s.mu.RUnlock()
	var best *TaskRecord
	for _, rec := range s.tasks {
		if rec.Conversation == conv && !rec.Terminal {
			if best == nil || rec.UpdatedAt.After(best.UpdatedAt) {
				best = cloneTaskRecord(rec)
			}
		}
	}
	return best
}

func (s *BusSession) findChildTasks(parentID string) []*TaskRecord {
	s.mu.RLock()
	defer s.mu.RUnlock()
	var out []*TaskRecord
	for _, rec := range s.tasks {
		if rec.DelegatedFromTaskID == parentID {
			out = append(out, cloneTaskRecord(rec))
		}
	}
	sort.Slice(out, func(i, j int) bool {
		return out[i].CreatedAt.Before(out[j].CreatedAt)
	})
	return out
}

func (s *BusSession) findWakeTask(childID string) *TaskRecord {
	s.mu.RLock()
	defer s.mu.RUnlock()
	for _, rec := range s.tasks {
		if rec.WakeFromChildTaskID == childID {
			return cloneTaskRecord(rec)
		}
	}
	return nil
}

func (s *BusSession) consoleNoticeCursor() int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return len(s.consoleNotices)
}

func (s *BusSession) consoleNoticesSince(token string, cursor int) []ConsoleOutFrame {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if cursor < 0 {
		cursor = 0
	}
	if cursor >= len(s.consoleNotices) {
		return nil
	}
	var out []ConsoleOutFrame
	for _, n := range s.consoleNotices[cursor:] {
		if token == "" || n.Token == token {
			out = append(out, n)
		}
	}
	return out
}

func cloneTaskRecord(rec *TaskRecord) *TaskRecord {
	if rec == nil {
		return nil
	}
	cp := *rec
	cp.SteerTurns = append([]string(nil), rec.SteerTurns...)
	cp.IntermediateTurns = append([]string(nil), rec.IntermediateTurns...)
	cp.Activities = append([]ActivityItem(nil), rec.Activities...)
	cp.Delegates = append([]DelegateItem(nil), rec.Delegates...)
	cp.Notices = append([]string(nil), rec.Notices...)
	cp.PRLinks = append([]string(nil), rec.PRLinks...)
	return &cp
}

func extractTaskAddressee(subject string) string {
	trimmed := strings.TrimPrefix(subject, subjectTasksPrefix)
	parts := strings.Split(trimmed, ".")
	if len(parts) >= 2 {
		return parts[0]
	}
	return ""
}

func extractWakeChildTaskID(prompt string) string {
	if !strings.HasPrefix(prompt, wakePromptPrefix) {
		return ""
	}
	rest := strings.TrimPrefix(prompt, wakePromptPrefix)
	fields := strings.Fields(rest)
	if len(fields) > 0 {
		return strings.TrimRight(fields[0], "]")
	}
	return ""
}

func joinMessageText(parts []lib.Part) string {
	var chunks []string
	for _, p := range parts {
		if p.Kind == partKindText && p.Text != "" {
			chunks = append(chunks, p.Text)
		}
	}
	return strings.Join(chunks, "\n")
}

func joinDataOrTextParts(parts []lib.Part) string {
	for _, p := range parts {
		if p.Kind == partKindData && len(p.Data) > 0 {
			return string(p.Data)
		}
		if p.Kind == partKindText && p.Text != "" {
			return p.Text
		}
	}
	return ""
}

func extractChatMessage(parts []lib.Part) *chatmsg.Message {
	for _, p := range parts {
		if p.Kind == partKindData && len(p.Data) > 0 {
			if msg, err := chatmsg.Parse(p.Data); err == nil && msg.Schema != "" {
				return &msg
			}
		}
	}
	return nil
}

func parseActivityItem(raw string) (ActivityItem, bool) {
	var wire struct {
		Tool         string          `json:"tool"`
		Input        json.RawMessage `json:"input"`
		CallID       string          `json:"callId"`
		CallIDSnake  string          `json:"call_id"`
		Status       string          `json:"status"`
		ErrorType    string          `json:"errorType"`
		ErrTypeSnake string          `json:"error_type"`
		DurationMs   int64           `json:"durationMs"`
		DurMsSnake   int64           `json:"duration_ms"`
		At           string          `json:"at"`
	}
	if err := json.Unmarshal([]byte(raw), &wire); err != nil || wire.Tool == "" {
		return ActivityItem{}, false
	}
	inputStr := ""
	if len(wire.Input) > 0 {
		var s string
		if json.Unmarshal(wire.Input, &s) == nil {
			inputStr = s
		} else {
			inputStr = strings.TrimSpace(string(wire.Input))
			if inputStr == jsonNullLiteral {
				inputStr = ""
			}
			inputStr = strings.ReplaceAll(inputStr, escapedUnicodeLt, literalLt)
			inputStr = strings.ReplaceAll(inputStr, escapedUnicodeGt, literalGt)
			inputStr = strings.ReplaceAll(inputStr, escapedUnicodeAmp, literalAmp)
		}
	}
	dur := wire.DurationMs
	if dur == 0 {
		dur = wire.DurMsSnake
	}
	return ActivityItem{
		Tool:       wire.Tool,
		Input:      inputStr,
		CallID:     firstNonEmptyStr(wire.CallID, wire.CallIDSnake),
		Status:     wire.Status,
		ErrorType:  firstNonEmptyStr(wire.ErrorType, wire.ErrTypeSnake),
		DurationMs: dur,
		At:         wire.At,
	}, true
}

func upsertActivityItem(list []ActivityItem, item ActivityItem) []ActivityItem {
	if item.CallID != "" {
		for i := range list {
			if list[i].CallID == item.CallID {
				list[i] = item
				return list
			}
		}
	}
	return append(list, item)
}

func isPureControlCommand(text string) bool {
	lower := strings.ToLower(strings.TrimSpace(text))
	return lower == cmdSessionOn || lower == cmdSessionOff || lower == cmdStatus || lower == cmdStop || lower == cmdHelp
}

func firstNonEmptyStr(vals ...string) string {
	for _, v := range vals {
		if s := strings.TrimSpace(v); s != "" {
			return s
		}
	}
	return ""
}

func truncateText(s string, maxLen int) string {
	s = strings.TrimSpace(s)
	if len(s) <= maxLen {
		return s
	}
	return s[:maxLen] + ellipsisSuffix
}

func containsString(slice []string, target string) bool {
	for _, s := range slice {
		if s == target {
			return true
		}
	}
	return false
}
