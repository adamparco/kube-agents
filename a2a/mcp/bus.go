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
	cardTaskIDPrefix             = "task-card-"
	cardParentSkew               = 5 * time.Second
	cardParentGrace              = 20 * time.Minute
	maxPersistedStepTasks        = 200
	stepProgressMaxAge           = 24 * time.Hour
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
	orderedFetchBatchSize        = 512
	orderedFetchMaxWait          = 750 * time.Millisecond
	bootstrapTimeout             = 8 * time.Second
	bootstrapBackgroundTimeout   = 2 * time.Minute
	bootstrapTasksLookback       = 2 * time.Hour
	controlNoticeWait            = 2500 * time.Millisecond
	delegationChildWait          = 5 * time.Second
	delegationWakeWait           = 8 * time.Second
	defaultTurnWaitTimeout       = 150 * time.Second
	defaultStepStreamWindow      = 45 * time.Second
	initialStepStreamWindow      = 15 * time.Second
	minStepBatchWait             = 350 * time.Millisecond
	stepDebounceWait             = 200 * time.Millisecond
	activityStepBatchWait        = 8 * time.Second
	activityStepDebounceWait     = 600 * time.Millisecond
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
		streamDirectory,
		streamTopicsState,
		streamTopicsJournal,
		streamTasks,
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
	Index         int    `json:"index,omitempty"`
	Tool          string `json:"tool"`
	Input         string `json:"input,omitempty"`
	CallID        string `json:"call_id,omitempty"`
	Status        string `json:"status,omitempty"`
	ErrorType     string `json:"error_type,omitempty"`
	ErrorMessage  string `json:"error_message,omitempty"`
	ResultPreview string `json:"result_preview,omitempty"`
	SessionID     string `json:"session_id,omitempty"`
	Worker        string `json:"worker,omitempty"`
	DurationMs    int64  `json:"duration_ms,omitempty"`
	At            string `json:"at,omitempty"`
}

// DelegateItem represents a delegation request from a session worker to another agent.
type DelegateItem struct {
	Addressee   string `json:"addressee"`
	Text        string `json:"text"`
	ChildTaskID string `json:"childTaskId,omitempty"`
}

type timestampedChunk struct {
	ts     time.Time
	seq    int
	append bool
	text   string
}

func foldTimestampedChunks(chunks []timestampedChunk, sep string) string {
	if len(chunks) == 0 {
		return ""
	}
	ordered := chunks
	needSort := false
	for i := 1; i < len(chunks); i++ {
		if !chunks[i].ts.IsZero() && !chunks[i-1].ts.IsZero() && chunks[i].ts.Before(chunks[i-1].ts) {
			needSort = true
			break
		}
	}
	if needSort {
		ordered = append([]timestampedChunk(nil), chunks...)
		sort.SliceStable(ordered, func(i, j int) bool {
			ti, tj := ordered[i].ts, ordered[j].ts
			if !ti.Equal(tj) && !ti.IsZero() && !tj.IsZero() {
				return ti.Before(tj)
			}
			if ordered[i].append != ordered[j].append {
				return !ordered[i].append
			}
			return ordered[i].seq < ordered[j].seq
		})
	}
	var acc string
	for _, c := range ordered {
		if !c.append {
			acc = c.text
			continue
		}
		if acc != "" && c.text != "" && sep != "" && !strings.HasSuffix(acc, sep) && !strings.HasPrefix(c.text, sep) {
			acc += sep
		}
		acc += c.text
	}
	return acc
}

// TaskRecord holds the folded state and rich streaming artifacts of a task on TASKS.
type TaskRecord struct {
	TaskID              string `json:"taskId"`
	ContextID           string `json:"contextId"`
	CorrelationID       string `json:"correlationId,omitempty"`
	Addressee           string `json:"addressee"`
	Conversation        string `json:"conversation,omitempty"`
	DelegatedFromTaskID string `json:"delegatedFromTaskId,omitempty"`
	WakeFromChildTaskID string `json:"wakeFromChildTaskId,omitempty"`
	// CardParentTaskID links a Kanban specialist card task (task-card-*) spawned by the platform
	// agent to the foreground task that was running in the same conversation when the card was
	// created, so card progress renders as steps of that task instead of as unrelated push events.
	CardParentTaskID     string             `json:"cardParentTaskId,omitempty"`
	State                lib.TaskState      `json:"state"`
	Terminal             bool               `json:"terminal"`
	StatusText           string             `json:"statusText,omitempty"`
	PromptText           string             `json:"promptText,omitempty"`
	SteerTurns           []string           `json:"steerTurns,omitempty"`
	SteerTimestamps      []time.Time        `json:"steerTimestamps,omitempty"`
	IntermediateTurns    []string           `json:"intermediateTurns,omitempty"`
	Thinking             string             `json:"thinking,omitempty"`
	Activities           []ActivityItem     `json:"activities,omitempty"`
	ProgressText         string             `json:"progressText,omitempty"`
	ProgressChat         *chatmsg.Message   `json:"progressChat,omitempty"`
	Delegates            []DelegateItem     `json:"delegates,omitempty"`
	Notices              []string           `json:"notices,omitempty"`
	PRLinks              []string           `json:"prLinks,omitempty"`
	ResultText           string             `json:"resultText,omitempty"`
	ResultChat           *chatmsg.Message   `json:"resultChat,omitempty"`
	CreatedAt            time.Time          `json:"createdAt"`
	UpdatedAt            time.Time          `json:"updatedAt"`
	EnvelopeCount        int                `json:"envelopeCount"`
	EnvelopeCountsByKind map[string]int     `json:"envelopeCountsByKind,omitempty"`
	promptTS             time.Time          `json:"-"`
	lastStatusTS         time.Time          `json:"-"`
	lastProgressTS       time.Time          `json:"-"`
	lastResultTS         time.Time          `json:"-"`
	thinkingChunks       []timestampedChunk `json:"-"`
	progressChunks       []timestampedChunk `json:"-"`
	resultChunks         []timestampedChunk `json:"-"`
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
	// CardTasks are Kanban specialist card tasks (task-card-*) the platform agent fanned out while
	// working on PrimaryTask (or its delegated child); their progress is surfaced as steps.
	CardTasks             []*TaskRecord `json:"cardTasks,omitempty"`
	TimedOut              bool          `json:"timedOut"`
	PrevStepCount         int           `json:"prevStepCount,omitempty"`
	ReportedStepCount     int           `json:"reportedStepCount,omitempty"`
	PrevActivityCount     int           `json:"prevActivityCount,omitempty"`
	ReportedActivityCount int           `json:"reportedActivityCount,omitempty"`
}

type authorityWire struct {
	Audience struct {
		Conversation string `json:"conversation"`
	} `json:"audience"`
	Via *struct {
		TaskID  string `json:"taskId"`
		Session string `json:"session"`
	} `json:"via,omitempty"`
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
	foregroundTasks   map[string]time.Time
	prevReportedSteps map[string]int
	lastReportedSteps map[string]int
	prevReportedActs  map[string]int
	lastReportedActs  map[string]int
	lastWaitStepTitle map[string]string
	streamBootSeq     map[string]uint64
	backfilledTasks   map[string]bool
	backfillMu        sync.Mutex
	// stepProgressPath, when set, persists the per-task step cursor across process restarts.
	stepProgressPath string
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
		profile:           prof,
		natsURL:           natsURL,
		tunnel:            tunnel,
		nc:                nc,
		js:                js,
		onPushEvent:       onPushEvent,
		tasks:             make(map[string]*TaskRecord),
		seenEnvelopes:     make(map[string]bool),
		topics:            make(map[string]TopicSnapshot),
		agentCards:        make(map[string]map[string]any),
		watchedConvs:      make(map[string]int),
		watchedTasks:      make(map[string]int),
		foregroundTasks:   make(map[string]time.Time),
		lastReportedSteps: make(map[string]int),
		streamBootSeq:     make(map[string]uint64),
		backfilledTasks:   make(map[string]bool),
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

// EnableStepProgressPersistence loads any previously persisted per-task step cursors from path
// and keeps the file updated, so a restarted MCP server resumes mid-stream tasks at the step the
// user last saw instead of re-streaming them from step 1.
func (s *BusSession) EnableStepProgressPersistence(path string) {
	if s == nil || strings.TrimSpace(path) == "" {
		return
	}
	prev, last := loadStepProgress(path)
	s.mu.Lock()
	defer s.mu.Unlock()
	s.stepProgressPath = path
	if s.prevReportedSteps == nil {
		s.prevReportedSteps = make(map[string]int)
	}
	if s.lastReportedSteps == nil {
		s.lastReportedSteps = make(map[string]int)
	}
	for id, v := range prev {
		if _, seen := s.prevReportedSteps[id]; !seen {
			s.prevReportedSteps[id] = v
		}
	}
	for id, v := range last {
		if _, seen := s.lastReportedSteps[id]; !seen {
			s.lastReportedSteps[id] = v
		}
	}
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
	// Foreground budget first; whatever is left is finished in the background so a busy TASKS
	// stream (it grows by thousands of envelopes per hour of fan-outs) never leaves a reconnected
	// session with a truncated view of a task that is still being polled.
	remaining := s.bootstrapStreams(ctx, allKnownStreams)
	go func() {
		bgCtx, cancel := context.WithTimeout(context.Background(), bootstrapBackgroundTimeout)
		defer cancel()
		if len(remaining) > 0 {
			s.bootstrapStreams(bgCtx, remaining)
		}
		s.backfillDeepTasksHistory(bgCtx)
	}()
}

// backfillDeepTasksHistory asynchronously replays TASKS from 24h ago so conversation transcripts
// and task_control(list) include earlier turns without slowing down initial connection setup.
func (s *BusSession) backfillDeepTasksHistory(ctx context.Context) {
	if s == nil || s.js == nil || ctx.Err() != nil {
		return
	}
	start := time.Now().Add(-24 * time.Hour)
	cfg := jetstream.OrderedConsumerConfig{
		DeliverPolicy:     jetstream.DeliverByStartTimePolicy,
		OptStartTime:      &start,
		InactiveThreshold: orderedConsumerIdleThreshold,
	}
	cons, err := s.js.OrderedConsumer(ctx, streamTasks, cfg)
	if err != nil {
		return
	}
	info, infoErr := cons.Info(ctx)
	if infoErr != nil || info == nil || info.NumPending == 0 {
		return
	}
	pending := int(info.NumPending)
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

// bootstrapStreams replays streams into the session and returns the ones it did not finish
// before ctx expired. TASKS is replayed from bootstrapTasksLookback ago rather than from the
// start of the stream: older tasks are not pollable from a live Jetski conversation anyway.
func (s *BusSession) bootstrapStreams(ctx context.Context, streams []string) (unfinished []string) {
	for i, streamName := range streams {
		if ctx.Err() != nil {
			return streams[i:]
		}
		cfg := jetstream.OrderedConsumerConfig{
			DeliverPolicy:     jetstream.DeliverAllPolicy,
			InactiveThreshold: orderedConsumerIdleThreshold,
		}
		s.mu.RLock()
		lastSeq := s.streamBootSeq[streamName]
		s.mu.RUnlock()
		if lastSeq > 0 {
			cfg.DeliverPolicy = jetstream.DeliverByStartSequencePolicy
			cfg.OptStartSeq = lastSeq + 1
		} else if streamName == streamTasks {
			start := time.Now().Add(-bootstrapTasksLookback)
			cfg.DeliverPolicy = jetstream.DeliverByStartTimePolicy
			cfg.OptStartTime = &start
		}
		cons, err := s.js.OrderedConsumer(ctx, streamName, cfg)
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
		finished := false
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
				if meta, mErr := msg.Metadata(); mErr == nil && meta != nil && meta.Sequence.Stream > 0 {
					s.mu.Lock()
					if s.streamBootSeq == nil {
						s.streamBootSeq = make(map[string]uint64)
					}
					if meta.Sequence.Stream > s.streamBootSeq[streamName] {
						s.streamBootSeq[streamName] = meta.Sequence.Stream
					}
					s.mu.Unlock()
				}
				s.ingestA2AMessage(msg.Subject(), msg.Data(), false)
			}
			if !gotAny || msgs.Error() != nil {
				break
			}
		}
		finished = fetched >= pending || ctx.Err() == nil
		if !finished {
			return streams[i:]
		}
	}
	return nil
}

// GetTask returns a deep copy of a TaskRecord by task ID.
func (s *BusSession) GetTask(taskID string) *TaskRecord {
	s.ensureTaskBackfilled(taskID)
	s.mu.RLock()
	defer s.mu.RUnlock()
	rec, ok := s.tasks[taskID]
	if !ok {
		return nil
	}
	return cloneTaskRecord(rec)
}

func (s *BusSession) ensureTaskBackfilled(taskID string) {
	taskID = strings.TrimSpace(taskID)
	if s == nil || s.js == nil || taskID == "" {
		return
	}
	s.mu.RLock()
	already := s.backfilledTasks[taskID]
	rec := s.tasks[taskID]
	complete := rec != nil && rec.Terminal && rec.PromptText != ""
	s.mu.RUnlock()
	if already || complete {
		return
	}
	s.backfillMu.Lock()
	defer s.backfillMu.Unlock()
	s.mu.RLock()
	if s.backfilledTasks[taskID] {
		s.mu.RUnlock()
		return
	}
	s.mu.RUnlock()

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	cfg := jetstream.OrderedConsumerConfig{
		FilterSubjects:    []string{subjectTasksPrefix + "*." + taskID + ".>"},
		DeliverPolicy:     jetstream.DeliverAllPolicy,
		InactiveThreshold: orderedConsumerIdleThreshold,
	}
	cons, err := s.js.OrderedConsumer(ctx, streamTasks, cfg)
	if err != nil {
		return
	}
	info, infoErr := cons.Info(ctx)
	if infoErr == nil && info != nil && info.NumPending > 0 {
		pending := int(info.NumPending)
		fetched := 0
		for fetched < pending && ctx.Err() == nil {
			batch := pending - fetched
			if batch > orderedFetchBatchSize {
				batch = orderedFetchBatchSize
			}
			msgs, fErr := cons.Fetch(batch, jetstream.FetchMaxWait(orderedFetchMaxWait))
			if fErr != nil {
				break
			}
			got := false
			for msg := range msgs.Messages() {
				got = true
				fetched++
				s.ingestA2AMessage(msg.Subject(), msg.Data(), false)
			}
			if !got || msgs.Error() != nil {
				break
			}
		}
	}
	s.mu.Lock()
	if s.backfilledTasks == nil {
		s.backfilledTasks = make(map[string]bool)
	}
	s.backfilledTasks[taskID] = true
	s.mu.Unlock()
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

// findCardParentLocked picks the foreground task a freshly-seen Kanban card task belongs to: the
// most recently updated non-terminal, non-card task in the same conversation (or context) that was
// created before the card. Caller must hold s.mu.
func (s *BusSession) findCardParentLocked(card *TaskRecord) string {
	var best *TaskRecord
	for _, other := range s.tasks {
		if other == card || strings.HasPrefix(other.TaskID, cardTaskIDPrefix) {
			continue
		}
		// Cards published out of turn (after the parent was canceled, timed out, or already
		// answered) still belong to that parent for a grace window.
		if other.Terminal && time.Since(other.UpdatedAt) > cardParentGrace {
			continue
		}
		sameConv := card.Conversation != "" && other.Conversation == card.Conversation
		sameCtx := card.ContextID != "" && other.ContextID == card.ContextID
		if !sameConv && !sameCtx {
			continue
		}
		if !card.CreatedAt.IsZero() && !other.CreatedAt.IsZero() && other.CreatedAt.After(card.CreatedAt.Add(cardParentSkew)) {
			continue
		}
		if best == nil || other.UpdatedAt.After(best.UpdatedAt) {
			best = other
		}
	}
	if best == nil {
		return ""
	}
	return best.TaskID
}

// findCardTasks returns Kanban card tasks linked to parentID (or to any of its delegated children),
// oldest first.
func (s *BusSession) findCardTasks(parentID string, childIDs ...string) []*TaskRecord {
	s.mu.RLock()
	defer s.mu.RUnlock()
	want := map[string]bool{parentID: true}
	for _, id := range childIDs {
		want[id] = true
	}
	var out []*TaskRecord
	for _, rec := range s.tasks {
		if rec.CardParentTaskID != "" && want[rec.CardParentTaskID] {
			out = append(out, cloneTaskRecord(rec))
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].CreatedAt.Equal(out[j].CreatedAt) {
			return out[i].TaskID < out[j].TaskID
		}
		return out[i].CreatedAt.Before(out[j].CreatedAt)
	})
	return out
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
	cp.SteerTimestamps = append([]time.Time(nil), rec.SteerTimestamps...)
	cp.IntermediateTurns = append([]string(nil), rec.IntermediateTurns...)
	cp.Activities = append([]ActivityItem(nil), rec.Activities...)
	cp.Delegates = append([]DelegateItem(nil), rec.Delegates...)
	cp.Notices = append([]string(nil), rec.Notices...)
	cp.PRLinks = append([]string(nil), rec.PRLinks...)
	cp.thinkingChunks = append([]timestampedChunk(nil), rec.thinkingChunks...)
	cp.progressChunks = append([]timestampedChunk(nil), rec.progressChunks...)
	cp.resultChunks = append([]timestampedChunk(nil), rec.resultChunks...)
	if rec.EnvelopeCountsByKind != nil {
		cp.EnvelopeCountsByKind = make(map[string]int, len(rec.EnvelopeCountsByKind))
		for k, v := range rec.EnvelopeCountsByKind {
			cp.EnvelopeCountsByKind[k] = v
		}
	}
	return &cp
}
