package mcpbridge

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

const (
	pushDebounceWindow       = 500 * time.Millisecond
	pushSendTimeout          = 10 * time.Second
	agentAPICmdSendMsg       = "send-message"
	agentAPIFlagTitle        = "--title=Kube-Agents Push Notification"
	pushWakeupHeader         = "🔔 **[Kube-Agents Live Push Notification]**\nThe remote `kube-agents` deployment pushed the following asynchronous event(s) over the A2A JetStream bus:\n\n"
	piggybackHeader          = "\n\n---\n### 📬 Pending Kube-Agents Push Events\n"
	hookInjectHeader         = "🔔 **[Kube-Agents Pending Push Events]**\nThe following asynchronous event(s) arrived from `kube-agents` on the A2A JetStream bus:\n\n"
	emptyJSONResponse        = "{}\n"
	iconDefaultPush          = "📩"
	iconProactiveNotify      = "📣"
	iconTaskCompleted        = "✅"
	iconTaskFailed           = "❌"
	iconInputRequired        = "❓"
	iconNoticePR             = "🔀"
	maxPiggybackEvents       = 6
	piggybackSummaryMaxChars = 280
	maxWakeupEvents          = 12
)

// PushDispatcher coordinates storing asynchronous bus events and waking up
// the active Antigravity conversation via agentapi when reactive push is armed.
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
	if d.store == nil || !d.store.OwnsConsoleConversation(ev.Conversation) {
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
// The section is bounded (maxPiggybackEvents / piggybackSummaryMaxChars) so a
// burst of events can never dwarf the tool result it rides on.
func (d *PushDispatcher) DrainAndFormatPiggyback() string {
	return d.DrainAndFormatPiggybackFor("")
}

// DrainAndFormatPiggybackFor drains unread events matching agyConvID (or all unread events
// when agyConvID is empty) and formats them as a piggybacked Markdown section.
func (d *PushDispatcher) DrainAndFormatPiggybackFor(agyConvID string) string {
	if d.store == nil {
		return ""
	}
	events, err := d.store.DrainPendingEventsForAGY(agyConvID)
	if err != nil || len(events) == 0 {
		return ""
	}
	return piggybackHeader + FormatPushEventsMarkdownBounded(events, maxPiggybackEvents, piggybackSummaryMaxChars)
}

// FormatPushEventsMarkdownBounded renders at most maxEvents events (newest last), truncating each
// summary to maxSummaryChars and ending with a "+N more" line for the remainder.
func FormatPushEventsMarkdownBounded(events []PushEvent, maxEvents int, maxSummaryChars int) string {
	if len(events) == 0 {
		return ""
	}
	shown := events
	omitted := 0
	if maxEvents > 0 && len(events) > maxEvents {
		omitted = len(events) - maxEvents
		shown = events[len(events)-maxEvents:]
	}
	bounded := make([]PushEvent, 0, len(shown))
	for _, ev := range shown {
		cp := ev
		if maxSummaryChars > 0 {
			cp.Summary = truncateText(strings.Join(strings.Fields(cp.Summary), " "), maxSummaryChars)
		}
		bounded = append(bounded, cp)
	}
	out := FormatPushEventsMarkdown(bounded)
	if omitted > 0 {
		out += fmt.Sprintf("\n*(+ %d earlier event%s omitted; call `poll_events` with `peek_only: true` to review the full queue history)*\n", omitted, pluralSuffix(omitted))
	}
	return out
}

func pluralSuffix(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
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
		apiPath = defaultAgentAPIPath()
	}
	if _, err := os.Stat(apiPath); err != nil {
		return
	}

	pending := d.store.PeekPendingEvents()
	if len(pending) == 0 {
		return
	}

	var targetOrder []string
	grouped := make(map[string][]PushEvent)
	for _, ev := range pending {
		convID := d.store.ResolveAGYConversationIDForEvent(ev.Conversation)
		if convID == "" {
			continue
		}
		if _, exists := grouped[convID]; !exists {
			targetOrder = append(targetOrder, convID)
		}
		grouped[convID] = append(grouped[convID], ev)
	}

	for _, convID := range targetOrder {
		batch := grouped[convID]
		body := pushWakeupHeader + FormatPushEventsMarkdownBounded(batch, maxWakeupEvents, pushSummaryMaxChars)
		sendCtx, cancel := context.WithTimeout(ctx, pushSendTimeout)
		_, err := d.runner(sendCtx, apiPath, agentAPICmdSendMsg, agentAPIFlagTitle, convID, body)
		cancel()
		if err != nil {
			// The events stay queued: the next hook run or tool call delivers them inline.
			slog.Warn("kube-agents-mcp: reactive push via agentapi failed; events remain queued", "conversation", convID, "events", len(batch), "err", err)
			continue
		}
		// Acknowledge exactly what was sent: anything queued for this conversation while the
		// send was in flight is delivered on the next pass rather than dropped.
		sentIDs := make([]string, 0, len(batch))
		for _, ev := range batch {
			sentIDs = append(sentIDs, ev.ID)
		}
		_ = d.store.DrainPendingEventsByID(sentIDs)
		d.mu.Lock()
		d.dispatchedCount += len(batch)
		d.mu.Unlock()
		select {
		case d.wakeCh <- struct{}{}:
		default:
		}
	}
}

// FormatPushEventsMarkdown formats a batch of PushEvents into structured Antigravity Markdown.
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
			fmt.Fprintf(&sb, "  - **Pull Requests**: %s *(use `inspect_pr` to view native diffs in Antigravity)*\n", strings.Join(ev.PRLinks, ", "))
		}
	}
	return sb.String()
}

// HandleAGYHook processes an Antigravity SessionStart or PreInvocation hook payload from r,
// records the active conversationId for reactive agentapi push, drains any pending push events,
// and writes the hook JSON response to w.
func HandleAGYHook(store *ConfigStore, r io.Reader, w io.Writer) error {
	if store == nil {
		var err error
		store, err = NewConfigStore("")
		if err != nil {
			_, _ = io.WriteString(w, emptyJSONResponse)
			return nil
		}
	}
	if shouldCheckLSHealth(store) {
		healthCtx, cancel := context.WithTimeout(context.Background(), lsHTTPTimeout)
		_ = EnsureAGYMCPServerReady(healthCtx, "")
		cancel()
		bDir := NewLineageResolverForStore(store).BrainDir("")
		SyncDefaultDisconnectedOverview(context.Background(), bDir, store)
	}
	raw := readHookStdinBounded(r)
	hookConvID := ""
	if len(raw) > 0 {
		var payload struct {
			ConversationID string `json:"conversationId"`
		}
		if json.Unmarshal(raw, &payload) == nil && strings.TrimSpace(payload.ConversationID) != "" {
			cid := strings.TrimSpace(payload.ConversationID)
			hookConvID = cid
			snap := store.Snapshot()
			existing := strings.TrimSpace(snap.Push.AGYConversationID)
			if existing != "" && !store.IsKnownAGYConversation(cid) {
				_, _ = io.WriteString(w, emptyJSONResponse)
				return nil
			}
			// Every filesystem walk below is confined to the brain directory that belongs to
			// this store, so a hook run against a non-default config can never prune the real
			// installation's conversations.
			resolver := NewLineageResolverForStore(store)
			lin := ResolveLineageWithStore(resolver, store, cid, "", existing)
			if lin.IsSubagent {
				_, _ = store.UpdatePushConfigWithLineage(lin, nil, "")
				rootID := strings.TrimSpace(lin.RootConversationID)
				if rootID != "" && rootID != cid {
					if shouldCheckLSHealth(store) {
						bDir := resolver.BrainDir("")
						store.PruneStaleConversations(bDir)
						PruneStaleKubeAgentsSwarms(bDir, rootID, nil)
					}
					syncCtx, cancel := context.WithTimeout(context.Background(), lsHTTPTimeout)
					_, _ = ReparentLiveAGYSummaries(syncCtx, "", rootID, map[string]string{
						cid: lin.SubagentRole,
					}, true)
					cancel()
				}
			} else {
				activeRootID := ""
				if act, ok := snap.Conversations[snap.ActiveConversation]; ok {
					activeRootID = strings.TrimSpace(act.AGYConversationID)
				}
				if existing != "" && cid != existing && (activeRootID == "" || cid != activeRootID) && !store.IsKnownAGYConversation(cid) {
					_, _ = io.WriteString(w, emptyJSONResponse)
					return nil
				}
				if existing == "" || cid == existing || cid == activeRootID {
					_, _ = store.UpdatePushConfigWithLineage(lin, nil, "")
				}
				if shouldCheckLSHealth(store) {
					bDir := resolver.BrainDir("")
					store.PruneStaleConversations(bDir)
					PruneStaleKubeAgentsSwarms(bDir, firstNonEmptyStr(existing, cid), nil)
					SyncDefaultDisconnectedOverview(context.Background(), bDir, store)
				}
			}
		}
	}
	events, err := store.DrainPendingEventsForAGY(hookConvID)
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

func readHookStdinBounded(r io.Reader) []byte {
	if r == nil {
		return nil
	}
	const maxHookStdinBytes = 64 * 1024
	if f, ok := r.(*os.File); ok {
		if fi, err := f.Stat(); err == nil && (fi.Mode()&os.ModeCharDevice) != 0 {
			return nil
		}
		ch := make(chan []byte, 1)
		go func() {
			b, _ := io.ReadAll(io.LimitReader(f, maxHookStdinBytes))
			ch <- b
		}()
		select {
		case b := <-ch:
			return b
		case <-time.After(150 * time.Millisecond):
			return nil
		}
	}
	b, _ := io.ReadAll(io.LimitReader(r, maxHookStdinBytes))
	return b
}

func shouldCheckLSHealth(store *ConfigStore) bool {
	if strings.TrimSpace(os.Getenv(envAGYLanguageServerURL)) != "" {
		return true
	}
	if store == nil || store.Path() == "" {
		return false
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return false
	}
	return store.Path() == filepath.Join(home, configDirRelPath, configFileName)
}
