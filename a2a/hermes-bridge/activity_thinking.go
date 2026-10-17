package hermesbridge

import (
	"encoding/json"
	"fmt"
	"regexp"
	"slices"
	"strings"
	"time"
	"unicode/utf8"
)

var (
	thinkTagPattern      = regexp.MustCompile(`(?is)<think>(.*?)</think>`)
	scratchpadTagPattern = regexp.MustCompile(`(?is)<REASONING_SCRATCHPAD>(.*?)</REASONING_SCRATCHPAD>`)
	pyReprReasoningRe    = regexp.MustCompile(`reasoning(?:_content)?=['"]([^'"]+)['"]`)
	cmdClusterFlagRe     = regexp.MustCompile(`(?:--cluster|--context)[=\s]+['"]?([A-Za-z0-9_.:-]+)['"]?`)
	kanbanCardIDPattern  = regexp.MustCompile(`\b(t_[A-Za-z0-9_-]{2,32})\b`)

	inlineTokenValuePattern = regexp.MustCompile(`(?:gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{16,}|ya29\.[A-Za-z0-9._-]{16,}|AIza[A-Za-z0-9_-]{20,}|xox[abposr]-[A-Za-z0-9-]{10,}|(?:AKIA|ASIA)[A-Z0-9]{12,}|glpat-[A-Za-z0-9_-]{12,}|sk-[A-Za-z0-9_-]{16,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,})`)
	inlineAuthHeaderPattern = regexp.MustCompile(`(?i)(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}`)
	inlineURLCredPattern    = regexp.MustCompile(`(https?://[^/\s:@]+:)[^@\s/]+(@)`)
	inlineKVSecretPattern   = regexp.MustCompile(`(?i)\b(token|secret|password|passwd|passphrase|api[_-]?key|private[_-]?key|access[_-]?token|client[_-]?secret|pgpassword|dbpassword)\s*([=:])\s*(['"]?)[^\s'"]+(['"]?)`)
)

type sessionTrace struct {
	apiCalls          int
	thinkingPublished int
	lastThinking      string
	prevThinking      string
	sessionCards      map[string]string // worker sessionID -> cardID
	cardTitles        map[string]string // cardID -> short cluster/card title
	claimedSessions   map[string]bool   // worker sessionID claimed by this task
}

type hookAssistantMessage struct {
	Content                string          `json:"content"`
	ReasoningContent       string          `json:"reasoning_content"`
	Reasoning              string          `json:"reasoning"`
	ReasoningDetails       json.RawMessage `json:"reasoning_details"`
	AnthropicContentBlocks json.RawMessage `json:"anthropic_content_blocks"`
	ToolCalls              []struct {
		ID       string `json:"id"`
		Name     string `json:"name"`
		Function struct {
			Name      string          `json:"name"`
			Arguments json.RawMessage `json:"arguments"`
		} `json:"function"`
		Arguments json.RawMessage `json:"arguments"`
	} `json:"tool_calls"`
}

func (a *activityState) initSessionTraceLocked() {
	if a.trace.sessionCards == nil {
		a.trace.sessionCards = make(map[string]string)
		a.trace.cardTitles = make(map[string]string)
		a.trace.claimedSessions = make(map[string]bool)
	}
}

// SyncKanbanCardMeta updates the task's known Kanban card titles and worker
// session bindings from the SQLite poll loop.
func (a *activityState) SyncKanbanCardMeta(cardID, title, workerSessionID string) {
	a.SyncKanbanCardDetails(cardID, title, "", workerSessionID)
}

// SyncKanbanCardDetails updates the task's known Kanban card titles and worker
// session bindings from the SQLite poll loop.
func (a *activityState) SyncKanbanCardDetails(cardID, title, _ string, workerSessionID string) {
	if a == nil {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.initSessionTraceLocked()
	cardID = strings.TrimSpace(cardID)
	title = strings.TrimSpace(title)
	workerSessionID = strings.TrimSpace(workerSessionID)
	if cardID != "" && title != "" {
		a.trace.cardTitles[cardID] = shortCardClusterOrTitle(title)
	}
	if workerSessionID != "" && cardID != "" {
		a.trace.sessionCards[workerSessionID] = cardID
		a.trace.claimedSessions[workerSessionID] = true
	}
}

func extractKanbanCardIDFromCwd(cwd string) string {
	const marker = "/kanban/workspaces/"
	idx := strings.Index(cwd, marker)
	if idx < 0 {
		return ""
	}
	rest := cwd[idx+len(marker):]
	if slash := strings.IndexByte(rest, '/'); slash >= 0 {
		rest = rest[:slash]
	}
	return strings.TrimSpace(rest)
}

func (a *activityState) observeWorkerContextLocked(d hookDelivery, rawInput json.RawMessage, createdCardID string) {
	a.initSessionTraceLocked()
	sessID := strings.TrimSpace(d.SessionID)
	if cid := extractKanbanCardIDFromCwd(d.Cwd); cid != "" && sessID != "" && sessID != a.sessionID {
		a.trace.sessionCards[sessID] = cid
		a.trace.claimedSessions[sessID] = true
	}
	parsed := decodeToolArgsMap(rawInput)
	if d.ToolName == kanbanCreateTool && createdCardID != "" {
		if short := shortCardClusterOrTitle(extractArgStr(parsed, "title")); short != "" {
			a.trace.cardTitles[createdCardID] = short
		}
	}
	if strings.HasPrefix(d.ToolName, "kanban_") && sessID != "" && sessID != a.sessionID {
		if cid := extractArgStr(parsed, "card_id", "task_id", "id"); cid != "" {
			a.trace.sessionCards[sessID] = cid
			a.trace.claimedSessions[sessID] = true
		}
	}
}

func (a *activityState) workerLabelLocked(sessionID string) string {
	sessionID = strings.TrimSpace(sessionID)
	if sessionID == "" || sessionID == a.sessionID || strings.HasPrefix(sessionID, apiSessionIDPrefix) {
		return "platform"
	}
	if cid := a.trace.sessionCards[sessionID]; cid != "" {
		if title := a.trace.cardTitles[cid]; title != "" {
			return fmt.Sprintf("card:%s (%s)", cid, title)
		}
		return "card:" + cid
	}
	if len(a.trace.cardTitles) == 1 {
		for cid, title := range a.trace.cardTitles {
			if title != "" && !strings.HasPrefix(strings.ToLower(title), "fleet-wide") {
				return fmt.Sprintf("card:%s (%s)", cid, title)
			}
			return "card:" + cid
		}
	}
	return "worker:" + capRunes(sessionID, 24)
}

func (a *activityState) observePostAPIRequestLocked(d hookDelivery) {
	a.trace.apiCalls++
	if a.sessionID != "" && d.SessionID != "" && d.SessionID != a.sessionID && !a.waitingCard.Load() {
		return
	}
	isWorker := a.sessionID != "" && d.SessionID != "" && d.SessionID != a.sessionID
	if isWorker {
		a.observeWorkerContextLocked(d, nil, "")
	}
	cand := formatAPIRequestThinkingWithMode(d, a.trace.apiCalls, a.calls, a.sessionID != "")
	if (isWorker || a.cardsFiled.Load()) && cand == "Synthesizing findings into final response." {
		cand = ""
	}
	if cand != "" && cand != a.trace.prevThinking {
		a.trace.lastThinking = cand
		a.trace.prevThinking = cand
	}
}

func (a *activityState) enrichPreToolCallLocked(d hookDelivery, entry *ActivityEntry) {
	if a.sessionID == "" {
		return
	}
	entry.Input = redactInputDetailed(d.ToolName, d.ToolInput)
	a.observeWorkerContextLocked(d, d.ToolInput, "")
	entry.SessionID = capRunes(d.SessionID, activityCallIDCap)
	entry.Worker = a.workerLabelLocked(d.SessionID)
}

func (a *activityState) popOpenCallLocked(id string, d hookDelivery) ActivityEntry {
	if openEntry, seen := a.open[id]; seen {
		delete(a.open, id)
		a.openOrder = slices.DeleteFunc(a.openOrder, func(v string) bool { return v == id })
		return openEntry
	}
	if a.sessionID != "" && d.SessionID != "" {
		fallbackTool := capRunes(d.ToolName, activityToolNameCap)
		for _, openID := range a.openOrder {
			oe := a.open[openID]
			if oe.SessionID == d.SessionID && oe.Tool == fallbackTool {
				delete(a.open, openID)
				a.openOrder = slices.DeleteFunc(a.openOrder, func(v string) bool { return v == openID })
				return oe
			}
		}
	}
	return ActivityEntry{}
}

func (a *activityState) enrichPostToolCallLocked(d hookDelivery, openEntry ActivityEntry, status string, e *ActivityEntry) {
	rawToolInput := d.ToolInput
	if len(rawToolInput) == 0 && len(openEntry.Input) > 0 {
		rawToolInput = openEntry.Input
	}
	if status != ActivityStatusError && d.ToolName == hermesToolCallWrapper && strings.Contains(string(rawToolInput), kanbanCreateTool) {
		a.cardsFiled.Store(true)
	}
	if a.sessionID == "" {
		return
	}
	e.Input = redactInputDetailed(d.ToolName, d.ToolInput)
	if len(e.Input) == 0 && len(openEntry.Input) > 0 {
		e.Input = openEntry.Input
	}
	resPreview, errMsg, createdCardID := summarizeToolResult(d.ToolName, status, d.Extra.ErrorMessage, d.Extra.Result)
	a.observeWorkerContextLocked(d, rawToolInput, createdCardID)
	e.SessionID = capRunes(d.SessionID, activityCallIDCap)
	e.Worker = a.workerLabelLocked(d.SessionID)
	if status == ActivityStatusError && errMsg != "" {
		e.ErrorMessage = capRunes(errMsg, 320)
	}
	if resPreview != "" {
		e.ResultPreview = capRunes(resPreview, 320)
	}
}

// takeThinking returns the thinking text observed on the last delivery (if any) and clears it.
func (a *activityState) takeThinking() (string, bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	t := strings.TrimSpace(a.trace.lastThinking)
	a.trace.lastThinking = ""
	if t == "" {
		return "", false
	}
	return t, true
}

// thinkingUnderBudget bounds the thinking parts published for one task.
func (a *activityState) thinkingUnderBudget() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.trace.thinkingPublished < activityThinkingBudget {
		a.trace.thinkingPublished++
		return true
	}
	return false
}

func (a *activityState) hasWorkerSession(sessionID string) bool {
	if a == nil || sessionID == "" {
		return false
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.trace.claimedSessions[sessionID] || a.trace.sessionCards[sessionID] != ""
}

func (a *activityState) hasCardID(cardID string) bool {
	if a == nil || cardID == "" {
		return false
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.trace.cardTitles[cardID] != ""
}

func formatAPIRequestThinkingWithMode(d hookDelivery, fallbackStep, completedTools int, detailed bool) string {
	step := fallbackStep
	if n := durationMillis(d.Extra.APICallCount); n > 0 {
		step = int(n)
	}
	var msg hookAssistantMessage
	if len(d.Extra.AssistantMessage) > 0 && json.Unmarshal(d.Extra.AssistantMessage, &msg) != nil {
		var pyRepr string
		if json.Unmarshal(d.Extra.AssistantMessage, &pyRepr) == nil && pyRepr != "" {
			if m := pyReprReasoningRe.FindStringSubmatch(pyRepr); len(m) > 1 {
				msg.ReasoningContent = m[1]
			}
		}
	}
	if len(d.Extra.Response) > 0 {
		var resp struct {
			FinishReason     string               `json:"finish_reason"`
			AssistantMessage hookAssistantMessage `json:"assistant_message"`
		}
		if json.Unmarshal(d.Extra.Response, &resp) == nil {
			if d.Extra.FinishReason == "" {
				d.Extra.FinishReason = resp.FinishReason
			}
			if msg.Content == "" {
				msg.Content = resp.AssistantMessage.Content
			}
			if msg.ReasoningContent == "" {
				msg.ReasoningContent = resp.AssistantMessage.ReasoningContent
			}
			if msg.Reasoning == "" {
				msg.Reasoning = resp.AssistantMessage.Reasoning
			}
			if len(msg.ReasoningDetails) == 0 {
				msg.ReasoningDetails = resp.AssistantMessage.ReasoningDetails
			}
			if len(msg.AnthropicContentBlocks) == 0 {
				msg.AnthropicContentBlocks = resp.AssistantMessage.AnthropicContentBlocks
			}
			if len(msg.ToolCalls) == 0 {
				msg.ToolCalls = resp.AssistantMessage.ToolCalls
			}
		}
	}

	var reasoningParts []string
	addReasoning := func(s string) {
		s = strings.TrimSpace(s)
		if s == "" {
			return
		}
		for _, existing := range reasoningParts {
			if existing == s || strings.Contains(existing, s) {
				return
			}
		}
		reasoningParts = append(reasoningParts, s)
	}

	addReasoning(msg.ReasoningContent)
	addReasoning(msg.Reasoning)

	if len(msg.ReasoningDetails) > 0 {
		var details []map[string]any
		if json.Unmarshal(msg.ReasoningDetails, &details) == nil {
			for _, item := range details {
				if txt, ok := item["text"].(string); ok {
					addReasoning(txt)
				}
				if sum, ok := item["summary"].(string); ok {
					addReasoning(sum)
				}
			}
		}
	}
	if len(msg.AnthropicContentBlocks) > 0 {
		var blocks []struct {
			Type     string `json:"type"`
			Thinking string `json:"thinking"`
		}
		if json.Unmarshal(msg.AnthropicContentBlocks, &blocks) == nil {
			for _, blk := range blocks {
				if blk.Type == "thinking" {
					addReasoning(blk.Thinking)
				}
			}
		}
	}

	content := msg.Content
	for _, re := range []*regexp.Regexp{thinkTagPattern, scratchpadTagPattern} {
		for _, m := range re.FindAllStringSubmatch(content, -1) {
			if len(m) > 1 {
				addReasoning(m[1])
			}
		}
		content = re.ReplaceAllString(content, "")
	}
	content = strings.TrimSpace(content)
	if content != "" && (d.Extra.FinishReason == "tool_calls" || len(msg.ToolCalls) > 0) {
		addReasoning(content)
	}

	kanbanCreateCount := 0
	if detailed {
		for _, tc := range msg.ToolCalls {
			name := firstNonEmptyToolName(tc.Function.Name, tc.Name)
			argsRaw := tc.Function.Arguments
			if len(argsRaw) == 0 {
				argsRaw = tc.Arguments
			}
			kanbanCreateCount += countKanbanCreateCalls(name, argsRaw)
		}
	}
	var plannedTools []string
	addedBatchKanban := false
	for _, tc := range msg.ToolCalls {
		name := firstNonEmptyToolName(tc.Function.Name, tc.Name)
		argsRaw := tc.Function.Arguments
		if len(argsRaw) == 0 {
			argsRaw = tc.Arguments
		}
		for _, expanded := range expandPlannedToolCall(name, argsRaw, detailed, kanbanCreateCount, &addedBatchKanban) {
			if expanded != "" && !slices.Contains(plannedTools, expanded) {
				plannedTools = append(plannedTools, expanded)
			}
		}
	}

	explicit := strings.Join(reasoningParts, "\n")
	var out string
	switch {
	case len(plannedTools) > 0 && explicit != "":
		thoughtSingle := strings.Join(strings.Fields(explicit), " ")
		lines := make([]string, len(plannedTools))
		copy(lines, plannedTools)
		if thoughtSingle != "" {
			lines[0] = plannedTools[0] + "\t" + thoughtSingle
		}
		out = strings.Join(lines, "\n")
	case explicit != "":
		out = explicit
	case len(plannedTools) > 0:
		out = strings.Join(plannedTools, "\n")
	case d.Extra.FinishReason == "stop" && (step > 1 || completedTools > 0):
		out = "Synthesizing findings into final response."
	default:
		return ""
	}
	return capRunes(out, activityThinkingCap)
}

func firstNonEmptyToolName(a, b string) string {
	if s := strings.TrimSpace(a); s != "" {
		return s
	}
	return strings.TrimSpace(b)
}

func countKanbanCreateCalls(toolName string, argsRaw json.RawMessage) int {
	if toolName == kanbanCreateTool {
		return 1
	}
	if toolName == hermesToolCallWrapper {
		if calls, ok := decodeToolArgsMap(argsRaw)[wrapperCallsKey].([]any); ok {
			n := 0
			for _, item := range calls {
				if cm, ok := item.(map[string]any); ok && strings.TrimSpace(fmt.Sprint(cm[wrapperCallNameKey])) == kanbanCreateTool {
					n++
				}
			}
			return n
		}
	}
	return 0
}

func expandPlannedToolCall(toolName string, argsRaw json.RawMessage, detailed bool, kanbanCreateCount int, addedBatchKanban *bool) []string {
	toolName = capRunes(strings.TrimSpace(toolName), activityToolNameCap)
	if toolName == "" {
		return nil
	}
	describe := func(tn string, pa map[string]any) string {
		if !detailed {
			pa = nil
		} else if tn == kanbanCreateTool && kanbanCreateCount > 1 {
			if addedBatchKanban != nil && *addedBatchKanban {
				return ""
			}
			if addedBatchKanban != nil {
				*addedBatchKanban = true
			}
			return fmt.Sprintf("Dispatching %d specialist tasks on the Kanban board.", kanbanCreateCount)
		}
		return describePlannedTool(tn, pa)
	}
	parsed := decodeToolArgsMap(argsRaw)
	if toolName == hermesToolCallWrapper && parsed != nil {
		if calls, ok := parsed[wrapperCallsKey].([]any); ok && len(calls) > 0 {
			var out []string
			for _, item := range calls {
				cm, ok := item.(map[string]any)
				if !ok {
					continue
				}
				subName := capRunes(strings.TrimSpace(fmt.Sprint(cm[wrapperCallNameKey])), activityToolNameCap)
				if subName == "" || subName == "<nil>" || subName == redactedValue {
					continue
				}
				subArgs, _ := cm[wrapperCallArgsKey].(map[string]any)
				if desc := describe(subName, subArgs); desc != "" {
					out = append(out, desc)
				}
			}
			if len(out) > 0 {
				return out
			}
		}
	}
	if desc := describe(toolName, parsed); desc != "" {
		return []string{desc}
	}
	return nil
}

func decodeToolArgsMap(raw json.RawMessage) map[string]any {
	trimmed := strings.TrimSpace(string(raw))
	if trimmed == "" || trimmed == "null" {
		return nil
	}
	var asString string
	if json.Unmarshal(raw, &asString) == nil {
		trimmed = strings.TrimSpace(asString)
	}
	var m map[string]any
	if json.Unmarshal([]byte(trimmed), &m) != nil {
		return nil
	}
	return m
}

func extractArgStr(m map[string]any, keys ...string) string {
	for _, k := range keys {
		if v, ok := m[k].(string); ok {
			v = strings.TrimSpace(v)
			if v != "" && v != redactedValue && !strings.HasPrefix(v, "<string,") {
				return scrubInlineSecrets(v)
			}
		}
	}
	return ""
}

func shortClusterRef(raw string) string {
	raw = strings.TrimSpace(raw)
	if idx := strings.LastIndex(raw, "/clusters/"); idx >= 0 {
		raw = raw[idx+len("/clusters/"):]
	} else if strings.HasPrefix(raw, "gke_") {
		if parts := strings.Split(raw, "_"); len(parts) >= 4 {
			raw = strings.Join(parts[3:], "_")
		}
	}
	return capRunes(raw, 48)
}

func shortFilePath(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return ""
	}
	if idx := strings.Index(raw, "/skills/"); idx >= 0 {
		return "skill " + capRunes(strings.TrimPrefix(raw[idx+len("/skills/"):], "/"), 56)
	}
	parts := strings.Split(strings.Trim(raw, "/"), "/")
	if len(parts) > 2 {
		parts = parts[len(parts)-2:]
	}
	return capRunes(strings.Join(parts, "/"), 56)
}

func shortCardClusterOrTitle(title string) string {
	title = strings.TrimSpace(title)
	lower := strings.ToLower(title)
	for _, sep := range []string{" for ", ": "} {
		if idx := strings.LastIndex(lower, sep); idx >= 0 && idx+len(sep) < len(title) {
			if cand := strings.TrimSpace(title[idx+len(sep):]); cand != "" && !strings.Contains(cand, " ") {
				return capRunes(cand, 48)
			}
		}
	}
	return capRunes(title, 48)
}

var staticPlannedToolTitles = map[string]string{
	"mcp__gke__list_clusters":                      "Listing GKE clusters in the fleet.",
	"mcp__platform_control__list_cluster_profiles": "Listing GKE cluster profiles in the fleet.",
	"execute_code":                                 "Running analysis script.",
}

func describePlannedTool(toolName string, parsedArgs map[string]any) string {
	if s, ok := staticPlannedToolTitles[toolName]; ok {
		return s
	}
	switch {
	case toolName == "mcp__gke__get_cluster":
		if c := shortClusterRef(extractArgStr(parsedArgs, "name", "cluster", "cluster_name")); c != "" {
			return "Inspecting GKE cluster " + c + "."
		}
		return "Inspecting GKE cluster configuration."
	case strings.HasPrefix(toolName, "mcp__gke__"):
		if c := shortClusterRef(extractArgStr(parsedArgs, "name", "cluster", "cluster_name")); c != "" {
			return "Querying GKE cluster state (" + c + ")."
		}
		return "Querying GKE cluster state."
	case strings.HasPrefix(toolName, "mcp__platform_control__"):
		if c := shortClusterRef(extractArgStr(parsedArgs, "cluster_name", "name", "cluster")); c != "" {
			return "Querying platform control state (" + c + ")."
		}
		return "Querying platform control state."
	case toolName == "terminal" || toolName == "kubectl" || toolName == "run_kubectl" || toolName == "bash":
		return describeShellTool(toolName, parsedArgs)
	case toolName == kanbanCreateTool:
		if t := shortCardClusterOrTitle(extractArgStr(parsedArgs, "title")); t != "" {
			return "Delegating specialist task: " + t + "."
		}
		return "Delegating task to specialist agent."
	case strings.HasPrefix(toolName, "kanban_"):
		return "Updating task status on the Kanban board."
	case toolName == "read_file":
		if p := shortFilePath(extractArgStr(parsedArgs, "path", "file_path", "filepath")); p != "" {
			return "Reading " + p + "."
		}
		return "Reading file in the workspace."
	case toolName == "search_files" || toolName == "find_by_name" || toolName == "list_dir" || toolName == "glob":
		if p := shortFilePath(extractArgStr(parsedArgs, "path", "pattern", "query")); p != "" {
			return "Inspecting workspace files (" + p + ")."
		}
		return "Inspecting files in the workspace."
	case toolName == "patch" || toolName == "edit_file" || toolName == "replace_in_file":
		if p := shortFilePath(extractArgStr(parsedArgs, "path", "file_path", "filepath")); p != "" {
			return "Editing " + p + "."
		}
		return "Editing a file in the agent workspace."
	case toolName == "write_file" || toolName == "create_file":
		if p := shortFilePath(extractArgStr(parsedArgs, "path", "file_path", "filepath")); p != "" {
			return "Writing " + p + "."
		}
		return "Writing a file in the agent workspace."
	case toolName == "tool_search":
		if q := extractArgStr(parsedArgs, "query", "pattern"); q != "" {
			return "Searching available tools (" + capRunes(q, 36) + ")."
		}
		return "Searching available tools."
	case toolName == "tool_describe":
		if n := extractArgStr(parsedArgs, "name", "tool_name"); n != "" {
			return "Inspecting tool schema (" + capRunes(n, 36) + ")."
		}
		return "Inspecting tool schema."
	case toolName == "skill_view":
		if s := extractArgStr(parsedArgs, "name", "skill", "skill_name"); s != "" {
			return "Loading skill " + capRunes(s, 48) + "."
		}
		return "Loading skill instructions."
	default:
		return "Running " + toolName + "."
	}
}

func describeShellTool(toolName string, parsedArgs map[string]any) string {
	if parsedArgs == nil {
		if toolName == "kubectl" || toolName == "run_kubectl" {
			return "Querying Kubernetes cluster resources."
		}
		return "Running cluster and workspace diagnostics."
	}
	cmd := extractArgStr(parsedArgs, "command", "cmd")
	cluster := shortClusterRef(extractArgStr(parsedArgs, "cluster", "cluster_name", "context"))
	if cluster == "" {
		if m := cmdClusterFlagRe.FindStringSubmatch(cmd); len(m) > 1 {
			cluster = shortClusterRef(m[1])
		}
	}
	base := "Running cluster and workspace command"
	if toolName == "kubectl" || toolName == "run_kubectl" {
		base = "Querying Kubernetes cluster resources"
	} else if bin, sub := extractLeadingCommand(cmd); bin != "" {
		switch bin {
		case "kubectl", "git", "gh", "helm", "gcloud":
			if sub != "" {
				base = "Running " + bin + " " + sub
			} else {
				base = "Running " + bin + " command"
			}
		}
	}
	if cluster != "" {
		return base + " (" + cluster + ")."
	}
	return base + "."
}

func extractLeadingCommand(cmd string) (bin, sub string) {
	for _, seg := range strings.FieldsFunc(cmd, func(r rune) bool { return r == '\n' || r == ';' || r == '&' || r == '|' }) {
		fields := strings.Fields(strings.TrimSpace(seg))
		for len(fields) > 0 && (strings.Contains(fields[0], "=") || fields[0] == "env" || fields[0] == "sudo" || fields[0] == "timeout") {
			fields = fields[1:]
		}
		if len(fields) == 0 {
			continue
		}
		b := strings.ToLower(fields[0])
		if slash := strings.LastIndexByte(b, '/'); slash >= 0 {
			b = b[slash+1:]
		}
		if b == "cd" || b == "export" || b == "set" || b == "echo" || b == "true" {
			continue
		}
		for _, arg := range fields[1:] {
			if strings.HasPrefix(arg, "-") {
				continue
			}
			if s := strings.ToLower(strings.Trim(arg, "'\"")); len(s) <= 24 {
				return b, s
			}
			break
		}
		return b, ""
	}
	return "", ""
}

func scrubInlineSecrets(s string) string {
	if s == "" || s == redactedValue {
		return s
	}
	s = inlineTokenValuePattern.ReplaceAllString(s, redactedValue)
	s = inlineAuthHeaderPattern.ReplaceAllString(s, "$1 "+redactedValue)
	s = inlineURLCredPattern.ReplaceAllString(s, "${1}"+redactedValue+"${2}")
	s = inlineKVSecretPattern.ReplaceAllString(s, "${1}${2}${3}"+redactedValue+"${4}")
	return s
}

func redactInputDetailed(tool string, raw json.RawMessage) json.RawMessage {
	trimmed := strings.TrimSpace(string(raw))
	if trimmed == "" || trimmed == "null" {
		return nil
	}
	dec := json.NewDecoder(strings.NewReader(trimmed))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return json.RawMessage(unparseableInput)
	}
	red := scrubValueDetailed(redactKeys(v))
	out, err := json.Marshal(red)
	if err != nil {
		return json.RawMessage(unparseableInput)
	}
	if len(out) <= activityInputCap*2 {
		return out
	}
	if tool == hermesToolCallWrapper {
		if capped, ok := capWrapperCalls(red); ok {
			return capped
		}
	}
	out, _ = json.Marshal(truncatedStandIn(out, activityInputHead))
	return out
}

func scrubValueDetailed(v any) any {
	switch t := v.(type) {
	case map[string]any:
		out := make(map[string]any, len(t))
		shaped := 0
		for k, val := range t {
			nk := k
			if tokenShapedKey(k) {
				shaped++
				nk = fmt.Sprintf("<key %d, %d chars>", shaped, utf8.RuneCountInString(k))
			}
			out[nk] = scrubValueDetailed(val)
		}
		return out
	case []any:
		for i := range t {
			t[i] = scrubValueDetailed(t[i])
		}
		return t
	case string:
		if t == redactedValue {
			return t
		}
		return capRunes(scrubInlineSecrets(t), 512)
	}
	return v
}

func summarizeToolResult(toolName, status, rawErrMsg string, rawResult json.RawMessage) (resPreview, errMsg, createdCardID string) {
	rawErrMsg = strings.TrimSpace(scrubInlineSecrets(rawErrMsg))
	trimmed := strings.TrimSpace(string(rawResult))
	var decoded any
	if trimmed != "" && trimmed != "null" {
		dec := json.NewDecoder(strings.NewReader(trimmed))
		dec.UseNumber()
		if dec.Decode(&decoded) == nil {
			if s, ok := decoded.(string); ok {
				if st := strings.TrimSpace(s); strings.HasPrefix(st, "{") || strings.HasPrefix(st, "[") {
					var inner any
					innerDec := json.NewDecoder(strings.NewReader(st))
					innerDec.UseNumber()
					if innerDec.Decode(&inner) == nil {
						decoded = inner
					}
				}
			}
		}
	}

	if m, ok := decoded.(map[string]any); ok {
		if cid := extractArgStr(m, "id", "card_id", "task_id"); strings.HasPrefix(cid, "t_") {
			createdCardID = cid
		}
		if rawErrMsg == "" {
			rawErrMsg = extractArgStr(m, "error", "error_message", "stderr", "message")
		}
	}
	if createdCardID == "" && toolName == kanbanCreateTool && trimmed != "" {
		if m := kanbanCardIDPattern.FindStringSubmatch(trimmed); len(m) > 1 {
			createdCardID = m[1]
		}
	}

	if status == ActivityStatusError {
		if rawErrMsg == "" && trimmed != "" && trimmed != "null" {
			rawErrMsg = oneLinePreview(scrubInlineSecrets(extractResultText(decoded, trimmed)), 240)
		}
		return rawErrMsg, rawErrMsg, createdCardID
	}

	if toolName == kanbanCreateTool && createdCardID != "" {
		return "card_id=" + createdCardID, "", createdCardID
	}
	if s := extractResultText(decoded, trimmed); s != "" {
		return oneLinePreview(scrubInlineSecrets(s), 200), "", createdCardID
	}
	return "", "", createdCardID
}

func extractResultText(decoded any, fallbackRaw string) string {
	switch t := decoded.(type) {
	case string:
		return strings.TrimSpace(t)
	case map[string]any:
		for _, k := range []string{"output", "content", "result", "text", "stdout", "message", "summary"} {
			if v, ok := t[k].(string); ok && strings.TrimSpace(v) != "" {
				return strings.TrimSpace(v)
			}
		}
		if b, err := json.Marshal(scrubValueDetailed(redactKeys(t))); err == nil {
			return string(b)
		}
	case []any:
		if b, err := json.Marshal(scrubValueDetailed(redactKeys(t))); err == nil {
			return string(b)
		}
	}
	if fallbackRaw != "" && fallbackRaw != "null" {
		return fallbackRaw
	}
	return ""
}

func oneLinePreview(s string, maxBytes int) string {
	for _, line := range strings.Split(strings.TrimSpace(s), "\n") {
		if line = strings.TrimSpace(line); line != "" {
			return capRunes(strings.Join(strings.Fields(line), " "), maxBytes)
		}
	}
	return ""
}

func matchWorkerDeliveryRun(runs []*taskRun, d hookDelivery) *taskRun {
	if strings.HasPrefix(d.SessionID, apiSessionIDPrefix) {
		return nil
	}
	var waiting []*taskRun
	for _, r := range runs {
		if a := r.act.Load(); a != nil && a.waitingCard.Load() {
			if !a.hasWorkerSession(d.SessionID) && workerSessionStartedBefore(d.SessionID, a.startedAt) {
				continue
			}
			waiting = append(waiting, r)
		}
	}
	if len(waiting) == 0 {
		return nil
	}
	slices.SortFunc(waiting, func(x, y *taskRun) int {
		ax, ay := x.act.Load(), y.act.Load()
		if ax != nil && ay != nil && !ax.startedAt.Equal(ay.startedAt) {
			if ax.startedAt.After(ay.startedAt) {
				return -1
			}
			return 1
		}
		return 0
	})
	for _, r := range waiting {
		if a := r.act.Load(); a != nil && a.hasWorkerSession(d.SessionID) {
			return r
		}
	}
	if cid := extractKanbanCardIDFromCwd(d.Cwd); cid != "" {
		for _, r := range waiting {
			if a := r.act.Load(); a != nil && a.hasCardID(cid) {
				a.SyncKanbanCardDetails(cid, "", d.Profile, d.SessionID)
				return r
			}
		}
	}
	return waiting[0]
}

func workerSessionStartedBefore(sessionID string, startedAt time.Time) bool {
	if len(sessionID) < 15 || sessionID[8] != '_' || startedAt.IsZero() {
		return false
	}
	ts, err := time.Parse("20060102_150405", sessionID[:15])
	if err != nil {
		return false
	}
	return ts.Before(startedAt.UTC().Add(-2 * time.Second))
}
