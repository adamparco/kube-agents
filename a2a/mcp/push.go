package mcp

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"
	"sync"
	"time"
)

const (
	pushDebounceWindow  = 500 * time.Millisecond
	pushSendTimeout     = 10 * time.Second
	agentAPICmdSendMsg  = "send-message"
	agentAPIFlagTitle   = "--title=Kube-Agents Push Notification"
	pushWakeupHeader    = "🔔 **[Kube-Agents Live Push Notification]**\nThe remote `kube-agents` deployment pushed the following asynchronous event(s) over the A2A JetStream bus:\n\n"
	piggybackHeader     = "\n\n---\n### 📬 Pending Kube-Agents Push Events\n"
	hookInjectHeader    = "🔔 **[Kube-Agents Pending Push Events]**\nThe following asynchronous event(s) arrived from `kube-agents` on the A2A JetStream bus:\n\n"
	emptyJSONResponse   = "{}\n"
	iconDefaultPush     = "📩"
	iconProactiveNotify = "📣"
	iconTaskCompleted   = "✅"
	iconTaskFailed      = "❌"
	iconInputRequired   = "❓"
	iconNoticePR        = "🔀"
)

// PushDispatcher coordinates storing asynchronous bus events and waking up
// the active Jetski conversation via agentapi when reactive push is armed.
type PushDispatcher struct {
	store           *ConfigStore
	runner          CommandRunner
	wakeCh          chan struct{}
	cancel          context.CancelFunc
	doneCh          chan struct{}
	mu              sync.Mutex
	closed          bool
	dispatchedCount int
}

// NewPushDispatcher creates and starts a background PushDispatcher.
func NewPushDispatcher(store *ConfigStore, runner CommandRunner) *PushDispatcher {
	if runner == nil {
		runner = DefaultCommandRunner
	}
	ctx, cancel := context.WithCancel(context.Background())
	d := &PushDispatcher{
		store:  store,
		runner: runner,
		wakeCh: make(chan struct{}, 1),
		cancel: cancel,
		doneCh: make(chan struct{}),
	}
	go d.loop(ctx)
	return d
}

// Close stops the background push dispatcher.
func (d *PushDispatcher) Close() {
	d.mu.Lock()
	if d.closed {
		d.mu.Unlock()
		return
	}
	d.closed = true
	d.cancel()
	d.mu.Unlock()
	<-d.doneCh
}

// Enqueue persists a PushEvent and triggers the reactive wakeup loop.
func (d *PushDispatcher) Enqueue(ev PushEvent) {
	if d.store == nil {
		return
	}
	_ = d.store.AppendPendingEvents(ev)
	select {
	case d.wakeCh <- struct{}{}:
	default:
	}
}

// DispatchedCount returns the total number of events pushed via agentapi.
func (d *PushDispatcher) DispatchedCount() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.dispatchedCount
}

// DrainAndFormatPiggyback drains any unread events from the store and formats
// them as a piggybacked Markdown section to append to an MCP tool response.
func (d *PushDispatcher) DrainAndFormatPiggyback() string {
	if d.store == nil {
		return ""
	}
	events, err := d.store.DrainPendingEvents()
	if err != nil || len(events) == 0 {
		return ""
	}
	return piggybackHeader + FormatPushEventsMarkdown(events)
}

func (d *PushDispatcher) loop(ctx context.Context) {
	defer close(d.doneCh)
	for {
		select {
		case <-ctx.Done():
			return
		case <-d.wakeCh:
			select {
			case <-ctx.Done():
				return
			case <-time.After(pushDebounceWindow):
			}
			d.tryReactiveWakeup(ctx)
		}
	}
}

func (d *PushDispatcher) tryReactiveWakeup(ctx context.Context) {
	if d.store == nil {
		return
	}
	snap := d.store.Snapshot()
	if !snap.Push.Enabled {
		return
	}
	apiPath := snap.Push.AgentAPIPath
	if apiPath == "" {
		apiPath = defaultAgentAPIPath
	}
	if _, err := os.Stat(apiPath); err != nil {
		return
	}

	pending := d.store.PeekPendingEvents()
	if len(pending) == 0 {
		return
	}

	convID := d.store.ResolveJetskiConversationIDForEvent(pending[len(pending)-1].Conversation)
	if convID == "" {
		return
	}

	body := pushWakeupHeader + FormatPushEventsMarkdown(pending)
	sendCtx, cancel := context.WithTimeout(ctx, pushSendTimeout)
	defer cancel()

	_, err := d.runner(sendCtx, apiPath, agentAPICmdSendMsg, agentAPIFlagTitle, convID, body)
	if err == nil {
		_, _ = d.store.DrainPendingEvents()
		d.mu.Lock()
		d.dispatchedCount += len(pending)
		d.mu.Unlock()
	}
}

// FormatPushEventsMarkdown formats a batch of PushEvents into structured Jetski Markdown.
func FormatPushEventsMarkdown(events []PushEvent) string {
	if len(events) == 0 {
		return ""
	}
	var sb strings.Builder
	for i, ev := range events {
		if i > 0 {
			sb.WriteString("\n")
		}
		icon := iconDefaultPush
		switch ev.Kind {
		case eventKindProactiveNotify:
			icon = iconProactiveNotify
		case eventKindTaskCompleted:
			icon = iconTaskCompleted
		case eventKindTaskFailed:
			icon = iconTaskFailed
		case eventKindInputRequired:
			icon = iconInputRequired
		case eventKindNoticePR:
			icon = iconNoticePR
		}
		fmt.Fprintf(&sb, "- %s **%s** (`%s`", icon, ev.Title, ev.Kind)
		if ev.TaskID != "" {
			fmt.Fprintf(&sb, ", task `%s`", ev.TaskID)
		}
		if ev.Deployment != "" {
			fmt.Fprintf(&sb, ", deployment `%s`", ev.Deployment)
		}
		if !ev.Timestamp.IsZero() {
			fmt.Fprintf(&sb, ", %s", ev.Timestamp.Format(time.RFC3339))
		}
		sb.WriteString(")\n")
		if strings.TrimSpace(ev.Summary) != "" {
			for _, line := range strings.Split(strings.TrimSpace(ev.Summary), "\n") {
				fmt.Fprintf(&sb, "  > %s\n", line)
			}
		}
		if len(ev.PRLinks) > 0 {
			fmt.Fprintf(&sb, "  - **Pull Requests**: %s *(use `inspect_pr` to view native diffs in Jetski)*\n", strings.Join(ev.PRLinks, ", "))
		}
	}
	return sb.String()
}

// HandleJetskiHook processes a Jetski SessionStart or PreInvocation hook payload from r,
// records the active conversationId for reactive agentapi push, drains any pending push events,
// and writes the hook JSON response to w.
func HandleJetskiHook(store *ConfigStore, r io.Reader, w io.Writer) error {
	if store == nil {
		var err error
		store, err = NewConfigStore("")
		if err != nil {
			_, _ = io.WriteString(w, emptyJSONResponse)
			return nil
		}
	}
	raw, _ := io.ReadAll(r)
	if len(raw) > 0 {
		var payload struct {
			ConversationID string `json:"conversationId"`
		}
		if json.Unmarshal(raw, &payload) == nil && strings.TrimSpace(payload.ConversationID) != "" {
			cid := strings.TrimSpace(payload.ConversationID)
			existing := store.Snapshot().Push.JetskiConversationID
			if existing != "" && !store.IsKnownJetskiConversation(cid) {
				_, _ = io.WriteString(w, emptyJSONResponse)
				return nil
			}
			enabled := true
			_, _ = store.UpdatePushConfig(cid, &enabled, "")
		}
	}
	events, err := store.DrainPendingEvents()
	if err != nil || len(events) == 0 {
		_, _ = io.WriteString(w, emptyJSONResponse)
		return nil
	}
	msg := hookInjectHeader + FormatPushEventsMarkdown(events)
	resp := map[string]any{
		"injectSteps": []map[string]any{
			{
				"ephemeralMessage": msg,
			},
		},
	}
	return json.NewEncoder(w).Encode(resp)
}
