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
	profileRegionSufRe   = regexp.MustCompile(`-(?:us|europe|asia|australia|northamerica|southamerica|me|africa)-[a-z0-9-]+$`)
	cmdClusterFlagRe     = regexp.MustCompile(`(?:--cluster|--context)[=\s]+['"]?([A-Za-z0-9_.:-]+)['"]?`)

	inlineTokenValuePattern = regexp.MustCompile(`(?:gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{16,}|ya29\.[A-Za-z0-9._-]{16,}|AIza[A-Za-z0-9_-]{20,}|xox[abposr]-[A-Za-z0-9-]{10,}|(?:AKIA|ASIA)[A-Z0-9]{12,}|glpat-[A-Za-z0-9_-]{12,}|sk-[A-Za-z0-9_-]{16,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,})`)
	inlineAuthHeaderPattern = regexp.MustCompile(`(?i)(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}`)
	inlineURLCredPattern    = regexp.MustCompile(`(https?://[^/\s:@]+:)[^@\s/]+(@)`)
	inlineKVSecretPattern   = regexp.MustCompile(`(?i)\b(token|secret|password|passwd|passphrase|api[_-]?key|private[_-]?key|access[_-]?token|client[_-]?secret|pgpassword|dbpassword)\s*([=:])\s*(['"]?)[^\s'"]+(['"]?)`)
	kanbanCardIDPattern     = regexp.MustCompile(`\b(t_[A-Za-z0-9_-]{2,32})\b`)
)

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

// SyncKanbanCardMeta updates the task's known Kanban card titles and worker
// session bindings from the SQLite poll loop so subsequent worker tool calls
// carry their card ID and cluster title on the bus.
func (a *activityState) SyncKanbanCardMeta(cardID, title, workerSessionID string) {
	a.SyncKanbanCardDetails(cardID, title, "", workerSessionID)
}

// SyncKanbanCardDetails updates the task's known Kanban card titles, assignee
// profiles, cluster names, and worker session bindings from the SQLite poll loop.
func (a *activityState) SyncKanbanCardDetails(cardID, title, assignee, workerSessionID string) {
	if a == nil {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	cardID = strings.TrimSpace(cardID)
	title = strings.TrimSpace(title)
	assignee = strings.TrimSpace(assignee)
	workerSessionID = strings.TrimSpace(workerSessionID)
	if cardID != "" && title != "" {
		shortTitle := shortCardClusterOrTitle(title)
		a.cardTitles[cardID] = shortTitle
		if shortTitle != "" {
			a.cardClusters[strings.ToLower(shortTitle)] = cardID
		}
	}
	if cardID != "" && assignee != "" {
		a.cardAssignees[strings.ToLower(assignee)] = cardID
	}
	if workerSessionID != "" && cardID != "" {
		a.sessionCards[workerSessionID] = cardID
		a.claimedSessions[workerSessionID] = true
	}
}

func extractKanbanCardIDFromCwd(cwd string) string {
	cwd = strings.TrimSpace(cwd)
	if cwd == "" {
		return ""
	}
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

func (a *activityState) observeWorkerHookContextLocked(d hookDelivery) {
	sessID := strings.TrimSpace(d.SessionID)
	if sessID == "" || sessID == a.sessionID || strings.HasPrefix(sessID, apiSessionIDPrefix) {
		return
	}
	if prof := strings.TrimSpace(d.Profile); prof != "" && prof != "platform" {
		a.sessionProfiles[sessID] = prof
	}
	if cid := extractKanbanCardIDFromCwd(d.Cwd); cid != "" {
		a.sessionCards[sessID] = cid
		a.claimedSessions[sessID] = true
		return
	}
	if prof := strings.ToLower(strings.TrimSpace(d.Profile)); prof != "" && prof != "platform" && a.sessionCards[sessID] == "" {
		if cid := a.cardAssignees[prof]; cid != "" {
			a.sessionCards[sessID] = cid
			a.claimedSessions[sessID] = true
			return
		}
		for k, cid := range a.cardAssignees {
			if k != "" && cid != "" && (strings.Contains(prof, k) || strings.Contains(k, prof)) {
				a.sessionCards[sessID] = cid
				a.claimedSessions[sessID] = true
				return
			}
		}
	}
}

func (a *activityState) qualifyWorkerThinkingLocked(sessionID, cand string) string {
	if len(a.cardTitles) <= 1 || len(a.cardTitles) > 6 {
		return cand
	}
	sessID := strings.TrimSpace(sessionID)
	cid := a.sessionCards[sessID]
	title := strings.TrimSpace(a.cardTitles[cid])
	if profCluster := extractClusterFromProfile(a.sessionProfiles[sessID]); profCluster != "" && (title == "" || strings.HasPrefix(strings.ToLower(title), "fleet-wide")) {
		title = profCluster
	}
	if title == "" || strings.HasPrefix(strings.ToLower(title), "fleet-wide") {
		return cand
	}
	lowerTitle := strings.ToLower(title)
	lines := strings.Split(cand, "\n")
	for i, line := range lines {
		head := line
		tail := ""
		if tab := strings.IndexByte(line, '\t'); tab >= 0 {
			head = line[:tab]
			tail = line[tab:]
		}
		headTrim := strings.TrimSpace(head)
		if headTrim == "" || strings.Contains(strings.ToLower(headTrim), lowerTitle) {
			continue
		}
		headNoDot := strings.TrimSuffix(headTrim, ".")
		lines[i] = headNoDot + " (" + title + ")." + tail
	}
	return strings.Join(lines, "\n")
}

func (a *activityState) hasCardID(cardID string) bool {
	if a == nil || cardID == "" {
		return false
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.cardTitles[cardID] != "" {
		return true
	}
	for _, cid := range a.cardAssignees {
		if cid == cardID {
			return true
		}
	}
	return false
}

func (a *activityState) hasAnyCardBindings() bool {
	if a == nil {
		return false
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	return len(a.cardTitles) > 0 || len(a.cardAssignees) > 0 || len(a.cardClusters) > 0 || len(a.pendingAssignees) > 0 || len(a.pendingClusters) > 0
}

func (a *activityState) observeCardBindingLocked(sessionID, toolName string, rawInput json.RawMessage, createdCardID string) {
	sessionID = strings.TrimSpace(sessionID)
	parsed := decodeToolArgsMap(rawInput)
	recordCreateMap := func(m map[string]any, cid string) {
		title := extractArgStr(m, "title")
		assignee := strings.ToLower(extractArgStr(m, "assignee"))
		shortTitle := shortCardClusterOrTitle(title)
		lowerCluster := strings.ToLower(shortTitle)
		if assignee != "" && !slices.Contains(a.pendingAssignees, assignee) {
			a.pendingAssignees = append(a.pendingAssignees, assignee)
		}
		if lowerCluster != "" && !strings.HasPrefix(lowerCluster, "fleet-wide") && !slices.Contains(a.pendingClusters, lowerCluster) {
			a.pendingClusters = append(a.pendingClusters, lowerCluster)
		}
		if cid != "" {
			if shortTitle != "" {
				a.cardTitles[cid] = shortTitle
				if !strings.HasPrefix(lowerCluster, "fleet-wide") {
					a.cardClusters[lowerCluster] = cid
				}
			}
			if assignee != "" {
				a.cardAssignees[assignee] = cid
			}
		}
	}
	if toolName == kanbanCreateTool {
		recordCreateMap(parsed, createdCardID)
		return
	}
	if toolName == hermesToolCallWrapper && parsed != nil {
		if calls, ok := parsed[wrapperCallsKey].([]any); ok {
			for _, item := range calls {
				if cm, ok := item.(map[string]any); ok {
					if sub, _ := cm[wrapperCallNameKey].(string); strings.TrimSpace(sub) == kanbanCreateTool {
						subArgs, _ := cm[wrapperCallArgsKey].(map[string]any)
						recordCreateMap(subArgs, "")
					}
				}
			}
		}
	}
	if strings.HasPrefix(toolName, "kanban_") {
		cid := extractArgStr(parsed, "card_id", "task_id", "id")
		if cid != "" && sessionID != "" && sessionID != a.sessionID {
			a.sessionCards[sessionID] = cid
			a.claimedSessions[sessionID] = true
		}
	}
	if sessionID != "" && sessionID != a.sessionID && !strings.HasPrefix(sessionID, apiSessionIDPrefix) && a.sessionCards[sessionID] == "" {
		if cid := a.matchCardFromTextLocked(string(rawInput)); cid != "" {
			a.sessionCards[sessionID] = cid
			a.claimedSessions[sessionID] = true
		}
	}
}

func (a *activityState) matchCardFromTextLocked(raw string) string {
	lower := strings.ToLower(raw)
	if lower != "" {
		for assignee, cid := range a.cardAssignees {
			if assignee != "" && cid != "" && strings.Contains(lower, assignee) {
				return cid
			}
		}
		for cluster, cid := range a.cardClusters {
			if cluster != "" && cid != "" && strings.Contains(lower, cluster) {
				return cid
			}
		}
		for cid := range a.cardTitles {
			if cid != "" && strings.Contains(lower, strings.ToLower(cid)) {
				return cid
			}
		}
	}
	if len(a.cardTitles) == 1 {
		for cid, title := range a.cardTitles {
			if !strings.HasPrefix(strings.ToLower(title), "fleet-wide") {
				return cid
			}
		}
	}
	return ""
}

func (a *activityState) hasWorkerSession(sessionID string) bool {
	if a == nil || sessionID == "" {
		return false
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.claimedSessions[sessionID] || a.sessionCards[sessionID] != ""
}

func (a *activityState) claimWorkerSession(sessionID string) {
	if a == nil || sessionID == "" {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.claimedSessions[sessionID] = true
}

func (a *activityState) matchesWorkerPayload(body []byte) bool {
	if a == nil || len(body) == 0 {
		return false
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	lower := strings.ToLower(string(body))
	for assignee := range a.cardAssignees {
		if assignee != "" && strings.Contains(lower, assignee) {
			return true
		}
	}
	for _, assignee := range a.pendingAssignees {
		if assignee != "" && strings.Contains(lower, assignee) {
			return true
		}
	}
	for cluster := range a.cardClusters {
		if cluster != "" && strings.Contains(lower, cluster) {
			return true
		}
	}
	for _, cluster := range a.pendingClusters {
		if cluster != "" && strings.Contains(lower, cluster) {
			return true
		}
	}
	for cid := range a.cardTitles {
		if cid != "" && strings.Contains(lower, strings.ToLower(cid)) {
			return true
		}
	}
	return false
}

func (a *activityState) workerLabelLocked(sessionID, toolName string, rawInput json.RawMessage, createdCardID string) string {
	sessionID = strings.TrimSpace(sessionID)
	if sessionID == "" || sessionID == a.sessionID || strings.HasPrefix(sessionID, apiSessionIDPrefix) {
		return "platform"
	}
	cid := a.sessionCards[sessionID]
	if cid == "" && strings.HasPrefix(toolName, "kanban_") {
		cid = extractArgStr(decodeToolArgsMap(rawInput), "card_id", "task_id", "id")
	}
	if cid == "" && createdCardID != "" {
		cid = createdCardID
	}
	if cid == "" {
		cid = a.matchCardFromTextLocked(string(rawInput))
		if cid != "" {
			a.sessionCards[sessionID] = cid
		}
	}
	if cid != "" {
		title := a.cardTitles[cid]
		if profCluster := extractClusterFromProfile(a.sessionProfiles[sessionID]); profCluster != "" && (title == "" || strings.HasPrefix(strings.ToLower(title), "fleet-wide")) {
			title = profCluster
		}
		if title != "" {
			return fmt.Sprintf("card:%s (%s)", cid, title)
		}
		return "card:" + cid
	}
	if profCluster := extractClusterFromProfile(a.sessionProfiles[sessionID]); profCluster != "" {
		return fmt.Sprintf("worker:%s (%s)", capRunes(sessionID, 16), profCluster)
	}
	return "worker:" + capRunes(sessionID, 24)
}

// takeThinking returns the thinking text observed on the last delivery (if any)
// and clears it.
func (a *activityState) takeThinking() (string, bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	t := strings.TrimSpace(a.lastThinking)
	a.lastThinking = ""
	if t == "" {
		return "", false
	}
	return t, true
}

// thinkingUnderBudget bounds the thinking parts published for one task.
func (a *activityState) thinkingUnderBudget() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.thinkingPublished < activityThinkingBudget {
		a.thinkingPublished++
		return true
	}
	return false
}

func extractPyReprField(pyRepr, field string) string {
	prefix := field + "="
	idx := strings.Index(pyRepr, prefix)
	if idx < 0 {
		return ""
	}
	rest := pyRepr[idx+len(prefix):]
	if len(rest) < 2 || strings.HasPrefix(rest, "None") {
		return ""
	}
	quote := rest[0]
	if quote != '\'' && quote != '"' {
		return ""
	}
	var sb strings.Builder
	escaped := false
	for i := 1; i < len(rest); i++ {
		ch := rest[i]
		if escaped {
			switch ch {
			case 'n':
				sb.WriteByte('\n')
			case 't':
				sb.WriteByte('\t')
			case 'r':
				sb.WriteByte('\r')
			default:
				sb.WriteByte(ch)
			}
			escaped = false
			continue
		}
		if ch == '\\' {
			escaped = true
			continue
		}
		if ch == quote {
			return strings.TrimSpace(sb.String())
		}
		sb.WriteByte(ch)
	}
	return ""
}

func formatAPIRequestThinkingWithMode(d hookDelivery, fallbackStep, completedTools int, detailed bool) string {
	step := fallbackStep
	if n := durationMillis(d.Extra.APICallCount); n > 0 {
		step = int(n)
	}
	var msg hookAssistantMessage
	if len(d.Extra.AssistantMessage) > 0 {
		if json.Unmarshal(d.Extra.AssistantMessage, &msg) != nil {
			var pyRepr string
			if json.Unmarshal(d.Extra.AssistantMessage, &pyRepr) == nil && pyRepr != "" {
				if rc := extractPyReprField(pyRepr, "reasoning_content"); rc != "" {
					msg.ReasoningContent = rc
				} else if r := extractPyReprField(pyRepr, "reasoning"); r != "" {
					msg.Reasoning = r
				}
			}
		}
	}
	if len(d.Extra.Response) > 0 {
		var resp struct {
			FinishReason     string               `json:"finish_reason"`
			AssistantMessage hookAssistantMessage `json:"assistant_message"`
		}
		if json.Unmarshal(d.Extra.Response, &resp) == nil {
			if d.Extra.FinishReason == "" && resp.FinishReason != "" {
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
			Text     string `json:"text"`
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
		matches := re.FindAllStringSubmatch(content, -1)
		for _, m := range matches {
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

	var plannedTools []string
	addPlannedTool := func(desc string) {
		desc = strings.TrimSpace(desc)
		if desc == "" {
			return
		}
		if !slices.Contains(plannedTools, desc) {
			plannedTools = append(plannedTools, desc)
		}
	}
	kanbanCreateCount := 0
	if detailed {
		for _, tc := range msg.ToolCalls {
			name := strings.TrimSpace(tc.Function.Name)
			if name == "" {
				name = strings.TrimSpace(tc.Name)
			}
			argsRaw := tc.Function.Arguments
			if len(argsRaw) == 0 {
				argsRaw = tc.Arguments
			}
			kanbanCreateCount += countKanbanCreateCalls(name, argsRaw)
		}
	}
	addedBatchKanban := false
	for _, tc := range msg.ToolCalls {
		name := strings.TrimSpace(tc.Function.Name)
		if name == "" {
			name = strings.TrimSpace(tc.Name)
		}
		argsRaw := tc.Function.Arguments
		if len(argsRaw) == 0 {
			argsRaw = tc.Arguments
		}
		for _, expanded := range expandPlannedToolCallWithMode(name, argsRaw, detailed, kanbanCreateCount, &addedBatchKanban) {
			addPlannedTool(expanded)
		}
	}

	explicit := strings.Join(reasoningParts, "\n")
	var out string
	switch {
	case len(plannedTools) > 0 && explicit != "":
		thoughtSingle := strings.Join(strings.Fields(explicit), " ")
		var lines []string
		for i, pt := range plannedTools {
			if i == 0 && thoughtSingle != "" {
				lines = append(lines, pt+"\t"+thoughtSingle)
			} else {
				lines = append(lines, pt)
			}
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

func countKanbanCreateCalls(toolName string, argsRaw json.RawMessage) int {
	if toolName == kanbanCreateTool {
		return 1
	}
	if toolName == hermesToolCallWrapper {
		parsed := decodeToolArgsMap(argsRaw)
		if calls, ok := parsed[wrapperCallsKey].([]any); ok {
			n := 0
			for _, item := range calls {
				if cm, ok := item.(map[string]any); ok {
					if sub, _ := cm[wrapperCallNameKey].(string); strings.TrimSpace(sub) == kanbanCreateTool {
						n++
					}
				}
			}
			return n
		}
	}
	return 0
}

func expandPlannedToolCallWithMode(toolName string, argsRaw json.RawMessage, detailed bool, kanbanCreateCount int, addedBatchKanban *bool) []string {
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
				subName, _ := cm[wrapperCallNameKey].(string)
				subName = capRunes(strings.TrimSpace(subName), activityToolNameCap)
				if subName == "" || subName == redactedValue {
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
	if m == nil {
		return ""
	}
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
	if raw == "" {
		return ""
	}
	if idx := strings.LastIndex(raw, "/clusters/"); idx >= 0 {
		raw = raw[idx+len("/clusters/"):]
	}
	if strings.HasPrefix(raw, "gke_") {
		parts := strings.Split(raw, "_")
		if len(parts) >= 4 {
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

func extractClusterFromProfile(profile string) string {
	profile = strings.ToLower(strings.TrimSpace(profile))
	if profile == "" || profile == "platform" || profile == "default" {
		return ""
	}
	if strings.HasPrefix(profile, "cluster-") {
		rest := strings.TrimPrefix(profile, "cluster-")
		rest = profileRegionSufRe.ReplaceAllString(rest, "")
		return capRunes(rest, 48)
	}
	return ""
}

func trimUnclosedParen(s string) string {
	if strings.Count(s, "(") > strings.Count(s, ")") {
		if idx := strings.LastIndex(s, "("); idx > 0 {
			s = strings.TrimSpace(s[:idx])
		}
	}
	return s
}

func shortCardClusterOrTitle(title string) string {
	title = strings.TrimSpace(title)
	if title == "" {
		return ""
	}
	lower := strings.ToLower(title)
	for _, sep := range []string{" for ", ": "} {
		if idx := strings.LastIndex(lower, sep); idx >= 0 && idx+len(sep) < len(title) {
			candidate := strings.TrimSpace(title[idx+len(sep):])
			if candidate != "" && !strings.Contains(candidate, " ") {
				return trimUnclosedParen(capRunes(candidate, 48))
			}
		}
	}
	return trimUnclosedParen(capRunes(title, 48))
}

// describePlannedTool formats a structured step title from a planned tool call
// and its arguments (when detailed is enabled and parsedArgs is non-nil).
func describePlannedTool(toolName string, parsedArgs map[string]any) string {
	switch {
	case toolName == "mcp__gke__list_clusters":
		return "Listing GKE clusters in the fleet."
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
	case toolName == "mcp__platform_control__list_cluster_profiles":
		return "Listing GKE cluster profiles in the fleet."
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
	case toolName == "kanban_heartbeat":
		if cid := extractArgStr(parsedArgs, "card_id", "id"); cid != "" {
			return "Updating progress on Kanban card " + cid + "."
		}
		return "Updating task status on the Kanban board."
	case toolName == "kanban_complete":
		if cid := extractArgStr(parsedArgs, "card_id", "id"); cid != "" {
			return "Completing Kanban card " + cid + "."
		}
		return "Updating task status on the Kanban board."
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
	case toolName == "execute_code":
		return "Running analysis script."
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
		case "kubectl":
			if sub != "" {
				base = "Running kubectl " + sub
			} else {
				base = "Querying Kubernetes cluster resources"
			}
		case "git":
			if sub != "" {
				base = "Running git " + sub
			} else {
				base = "Running git command"
			}
		case "gh":
			if sub != "" {
				base = "Running gh " + sub
			} else {
				base = "Running GitHub CLI command"
			}
		case "gcloud":
			base = "Running gcloud command"
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
			s := strings.ToLower(strings.Trim(arg, "'\""))
			if len(s) <= 24 {
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

// redactInputDetailed returns the tool input fit for the bus on the API
// executor: secret-looking keys are blanked via redactKeys and inline
// credential patterns inside string values are scrubbed via
// scrubInlineSecrets, while keeping non-secret strings (file paths, commands,
// cluster names, Kanban card notes) readable and bounded.
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
				st := strings.TrimSpace(s)
				if strings.HasPrefix(st, "{") || strings.HasPrefix(st, "[") {
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
			if s, ok := decoded.(string); ok {
				rawErrMsg = oneLinePreview(scrubInlineSecrets(s), 240)
			} else {
				rawErrMsg = oneLinePreview(scrubInlineSecrets(trimmed), 240)
			}
		}
		return rawErrMsg, rawErrMsg, createdCardID
	}

	switch {
	case toolName == kanbanCreateTool && createdCardID != "":
		if m, ok := decoded.(map[string]any); ok {
			st := extractArgStr(m, "status")
			if st == "" {
				st = "todo"
			}
			return fmt.Sprintf("card_id=%s status=%s", createdCardID, st), "", createdCardID
		}
		return "card_id=" + createdCardID, "", createdCardID
	case toolName == "kanban_heartbeat":
		return "heartbeat recorded", "", ""
	case toolName == "kanban_complete":
		return "card marked completed", "", ""
	case toolName == "read_file":
		if s := extractResultText(decoded, trimmed); s != "" {
			lines := strings.Count(s, "\n") + 1
			if head := oneLinePreview(scrubInlineSecrets(s), 120); head != "" {
				return fmt.Sprintf("%d lines (%d B) — %s", lines, len(s), head), "", ""
			}
			return fmt.Sprintf("%d lines (%d B)", lines, len(s)), "", ""
		}
	case toolName == "terminal" || toolName == "bash" || toolName == "kubectl" || toolName == "run_kubectl":
		if m, ok := decoded.(map[string]any); ok {
			outStr := extractArgStr(m, "output", "stdout", "result")
			if outStr == "" {
				outStr = extractArgStr(m, "stderr")
			}
			exitStr := ""
			if ec, has := m["exit_code"]; has {
				exitStr = fmt.Sprintf("exit=%v ", ec)
			} else if ec, has := m["returncode"]; has {
				exitStr = fmt.Sprintf("exit=%v ", ec)
			}
			if outStr != "" {
				lines := strings.Count(outStr, "\n") + 1
				return fmt.Sprintf("%s(%d lines) %s", exitStr, lines, oneLinePreview(scrubInlineSecrets(outStr), 140)), "", ""
			}
			if exitStr != "" {
				return strings.TrimSpace(exitStr), "", ""
			}
		}
		if s := extractResultText(decoded, ""); s != "" {
			lines := strings.Count(s, "\n") + 1
			return fmt.Sprintf("(%d lines) %s", lines, oneLinePreview(scrubInlineSecrets(s), 150)), "", ""
		}
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
		b, err := json.Marshal(scrubValueDetailed(redactKeys(t)))
		if err == nil {
			return string(b)
		}
	case []any:
		b, err := json.Marshal(scrubValueDetailed(redactKeys(t)))
		if err == nil {
			return string(b)
		}
	}
	if fallbackRaw != "" && fallbackRaw != "null" {
		return fallbackRaw
	}
	return ""
}

func oneLinePreview(s string, maxBytes int) string {
	s = strings.TrimSpace(s)
	if s == "" {
		return ""
	}
	for _, line := range strings.Split(s, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		collapsed := strings.Join(strings.Fields(line), " ")
		return capRunes(collapsed, maxBytes)
	}
	return capRunes(strings.Join(strings.Fields(s), " "), maxBytes)
}

func matchWorkerDeliveryRun(runs []*taskRun, d hookDelivery, body []byte) *taskRun {
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
		idX, idY := "", ""
		if x.origin != nil {
			idX = x.origin.TaskID
		}
		if y.origin != nil {
			idY = y.origin.TaskID
		}
		return strings.Compare(idX, idY)
	})
	for _, r := range waiting {
		if a := r.act.Load(); a != nil && a.hasWorkerSession(d.SessionID) {
			return r
		}
	}
	cid := extractKanbanCardIDFromCwd(d.Cwd)
	if cid != "" {
		for _, r := range waiting {
			if a := r.act.Load(); a != nil && a.hasCardID(cid) {
				a.SyncKanbanCardDetails(cid, "", d.Profile, d.SessionID)
				return r
			}
		}
	}
	for _, r := range waiting {
		if a := r.act.Load(); a != nil {
			if cid != "" && a.hasAnyCardBindings() {
				continue
			}
			if a.matchesWorkerPayload(body) {
				a.claimWorkerSession(d.SessionID)
				if cid != "" {
					a.SyncKanbanCardDetails(cid, "", d.Profile, d.SessionID)
				}
				return r
			}
		}
	}
	if a := waiting[0].act.Load(); a != nil {
		if a.hasAnyCardBindings() {
			return nil
		}
		if cid != "" {
			a.SyncKanbanCardDetails(cid, "", d.Profile, d.SessionID)
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
