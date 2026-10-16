package mcp

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	jsonRPCVersion             = "2.0"
	defaultMCPProtocolVersion  = "2024-11-05"
	mcpServerName              = "kube-agents"
	mcpServerVersion           = "0.4.0"
	methodInitialize           = "initialize"
	methodInitialized          = "notifications/initialized"
	methodPing                 = "ping"
	methodToolsList            = "tools/list"
	methodToolsCall            = "tools/call"
	methodResourcesList        = "resources/list"
	methodResourcesRead        = "resources/read"
	methodPromptsList          = "prompts/list"
	toolDiscoverDeployments    = "discover_deployments"
	toolConnectDeployment      = "connect_deployment"
	toolConnectionStatus       = "connection_status"
	toolSendMessage            = "send_message"
	toolManageSession          = "manage_session"
	toolTaskControl            = "task_control"
	toolReadTopics             = "read_topics"
	toolInspectPR              = "inspect_pr"
	toolPollEvents             = "poll_events"
	resourceURIStatus          = "kube-agents://status"
	resourceURIDeployments     = "kube-agents://deployments"
	resourceURITopics          = "kube-agents://topics"
	resourceURITasks           = "kube-agents://tasks"
	resourceURIEvents          = "kube-agents://events"
	mimeTypeMarkdown           = "text/markdown"
	contentTypeText            = "text"
	rpcErrParseError           = -32700
	rpcErrInvalidRequest       = -32600
	rpcErrMethodNotFound       = -32601
	rpcErrInvalidParams        = -32602
	rpcErrInternal             = -32603
	scannerInitialBufSize      = 64 * 1024
	scannerMaxBufSize          = 16 * 1024 * 1024
	defaultTaskListLimit       = 20
	taskSummaryPreviewChars    = 90
	actionList                 = "list"
	actionNew                  = "new"
	actionSwitch               = "switch"
	actionSessionOn            = "session_on"
	actionSessionStartAlias    = "start"
	actionSessionOff           = "session_off"
	actionSessionEndAlias      = "end"
	actionHelp                 = "help"
	actionTranscript           = "transcript"
	actionGet                  = "get"
	actionStatus               = "status"
	actionSteer                = "steer"
	actionStop                 = "stop"
	actionCancelAlias          = "cancel"
	actionRead                 = "read"
	actionCards                = "cards"
	directProfileDefaultName   = "direct-nats"
	directProfileDefaultNS     = "kubeagents-system"
	directProfileDefaultPAName = "platform-agent"
)

type rpcRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

type rpcResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Result  any             `json:"result,omitempty"`
	Error   *rpcError       `json:"error,omitempty"`
}

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

type mcpToolDefinition struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	InputSchema map[string]any `json:"inputSchema"`
}

type mcpTextContent struct {
	Type string `json:"type"`
	Text string `json:"text"`
}

type mcpCallToolResult struct {
	Content []mcpTextContent `json:"content"`
	IsError bool             `json:"isError,omitempty"`
}

type mcpResource struct {
	URI         string `json:"uri"`
	Name        string `json:"name"`
	Description string `json:"description,omitempty"`
	MIMEType    string `json:"mimeType,omitempty"`
}

// ServerOptions configures an MCP Server instance.
type ServerOptions struct {
	Store      *ConfigStore
	Runner     CommandRunner
	APIBaseURL string
}

// Server implements the kube-agents Model Context Protocol (MCP) server over stdio.
type Server struct {
	store      *ConfigStore
	runner     CommandRunner
	apiBaseURL string
	dispatcher *PushDispatcher

	mu      sync.Mutex
	session *BusSession
	writeMu sync.Mutex
}

// NewServer constructs a new kube-agents MCP server.
func NewServer(opts ServerOptions) (*Server, error) {
	store := opts.Store
	if store == nil {
		var err error
		store, err = NewConfigStore("")
		if err != nil {
			return nil, err
		}
	}
	runner := opts.Runner
	if runner == nil {
		runner = DefaultCommandRunner
	}
	dispatcher := NewPushDispatcher(store, runner)
	return &Server{
		store:      store,
		runner:     runner,
		apiBaseURL: opts.APIBaseURL,
		dispatcher: dispatcher,
	}, nil
}

// Close shuts down the active bus session and background push dispatcher.
func (s *Server) Close() {
	s.mu.Lock()
	sess := s.session
	s.session = nil
	s.mu.Unlock()
	if sess != nil {
		sess.Close()
	}
	if s.dispatcher != nil {
		s.dispatcher.Close()
	}
}

// ServeStdio reads newline-delimited JSON-RPC 2.0 requests from r and writes responses to w.
func (s *Server) ServeStdio(ctx context.Context, r io.Reader, w io.Writer) error {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, scannerInitialBufSize), scannerMaxBufSize)
	for scanner.Scan() {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		resp := s.HandleJSONRPC(ctx, []byte(line))
		if len(resp) == 0 {
			continue
		}
		s.writeMu.Lock()
		_, writeErr := fmt.Fprintf(w, "%s\n", resp)
		s.writeMu.Unlock()
		if writeErr != nil {
			return writeErr
		}
	}
	return scanner.Err()
}

// HandleJSONRPC processes a single JSON-RPC 2.0 request frame and returns the marshaled response
// (or nil if the frame was a notification).
func (s *Server) HandleJSONRPC(ctx context.Context, raw []byte) []byte {
	var req rpcRequest
	if err := json.Unmarshal(raw, &req); err != nil {
		return marshalRPCError(json.RawMessage("null"), rpcErrParseError, "invalid JSON-RPC frame")
	}
	if len(req.ID) == 0 || string(req.ID) == "null" {
		if strings.HasPrefix(req.Method, "notifications/") {
			return nil
		}
		if req.Method == "" {
			return marshalRPCError(json.RawMessage("null"), rpcErrInvalidRequest, "missing method")
		}
		return nil
	}

	switch req.Method {
	case methodInitialize:
		protoVer := defaultMCPProtocolVersion
		var initParams struct {
			ProtocolVersion string `json:"protocolVersion"`
		}
		if len(req.Params) > 0 && json.Unmarshal(req.Params, &initParams) == nil && initParams.ProtocolVersion != "" {
			protoVer = initParams.ProtocolVersion
		}
		return marshalRPCResult(req.ID, map[string]any{
			"protocolVersion": protoVer,
			"capabilities": map[string]any{
				"tools":     map[string]any{},
				"resources": map[string]any{},
			},
			"serverInfo": map[string]any{
				"name":    mcpServerName,
				"version": mcpServerVersion,
			},
		})
	case methodPing:
		return marshalRPCResult(req.ID, map[string]any{})
	case methodToolsList:
		return marshalRPCResult(req.ID, map[string]any{
			"tools": s.toolDefinitions(),
		})
	case methodToolsCall:
		var callParams struct {
			Name      string          `json:"name"`
			Arguments json.RawMessage `json:"arguments"`
		}
		if err := json.Unmarshal(req.Params, &callParams); err != nil || callParams.Name == "" {
			return marshalRPCError(req.ID, rpcErrInvalidParams, "tools/call requires tool name")
		}
		res := s.CallTool(ctx, callParams.Name, callParams.Arguments)
		return marshalRPCResult(req.ID, res)
	case methodResourcesList:
		return marshalRPCResult(req.ID, map[string]any{
			"resources": s.resourceDefinitions(),
		})
	case methodResourcesRead:
		var readParams struct {
			URI string `json:"uri"`
		}
		if err := json.Unmarshal(req.Params, &readParams); err != nil || readParams.URI == "" {
			return marshalRPCError(req.ID, rpcErrInvalidParams, "resources/read requires uri")
		}
		text, err := s.readResource(ctx, readParams.URI)
		if err != nil {
			return marshalRPCError(req.ID, rpcErrInvalidParams, err.Error())
		}
		return marshalRPCResult(req.ID, map[string]any{
			"contents": []map[string]any{
				{
					"uri":      readParams.URI,
					"mimeType": mimeTypeMarkdown,
					"text":     text,
				},
			},
		})
	case methodPromptsList:
		return marshalRPCResult(req.ID, map[string]any{
			"prompts": []any{},
		})
	default:
		return marshalRPCError(req.ID, rpcErrMethodNotFound, fmt.Sprintf("unknown method %q", req.Method))
	}
}

// CallTool invokes a named MCP tool with JSON arguments and appends any pending push events.
func (s *Server) CallTool(ctx context.Context, name string, rawArgs json.RawMessage) mcpCallToolResult {
	var text string
	var err error

	switch name {
	case toolDiscoverDeployments:
		text, err = s.handleDiscoverDeployments(ctx, rawArgs)
	case toolConnectDeployment:
		text, err = s.handleConnectDeployment(ctx, rawArgs)
	case toolConnectionStatus:
		text, err = s.handleConnectionStatus(ctx)
	case toolSendMessage:
		text, err = s.handleSendMessage(ctx, rawArgs)
	case toolManageSession:
		text, err = s.handleManageSession(ctx, rawArgs)
	case toolTaskControl:
		text, err = s.handleTaskControl(ctx, rawArgs)
	case toolReadTopics:
		text, err = s.handleReadTopics(ctx, rawArgs)
	case toolInspectPR:
		text, err = s.handleInspectPR(ctx, rawArgs)
	case toolPollEvents:
		text, err = s.handlePollEvents(ctx, rawArgs)
	default:
		return mcpCallToolResult{
			Content: []mcpTextContent{{Type: contentTypeText, Text: fmt.Sprintf("Unknown tool %q", name)}},
			IsError: true,
		}
	}

	if name != toolPollEvents && s.dispatcher != nil {
		if piggyback := s.dispatcher.DrainAndFormatPiggyback(); piggyback != "" {
			text += piggyback
		}
	}

	if err != nil {
		msg := fmt.Sprintf("❌ **Error in `%s`**: %v", name, err)
		if text != "" {
			msg = text + "\n\n" + msg
		}
		return mcpCallToolResult{
			Content: []mcpTextContent{{Type: contentTypeText, Text: msg}},
			IsError: true,
		}
	}
	return mcpCallToolResult{
		Content: []mcpTextContent{{Type: contentTypeText, Text: text}},
	}
}

func (s *Server) handleDiscoverDeployments(ctx context.Context, rawArgs json.RawMessage) (string, error) {
	var args struct {
		KubeContext        string `json:"kube_context"`
		Context            string `json:"context"`
		Filter             string `json:"filter"`
		IncludeCredentials bool   `json:"include_credentials"`
	}
	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &args)
	}
	ctxFilter := firstNonEmptyStr(args.KubeContext, args.Context, args.Filter)
	discovered, err := DiscoverDeployments(ctx, s.runner, ctxFilter, args.IncludeCredentials)
	if len(discovered) > 0 {
		_ = s.store.UpsertDeployments(discovered)
	}
	snap := s.store.Snapshot()
	all := make([]DeploymentProfile, 0, len(snap.Deployments))
	for _, p := range snap.Deployments {
		all = append(all, p)
	}
	sort.Slice(all, func(i, j int) bool {
		if all[i].Name == snap.ActiveDeployment {
			return true
		}
		if all[j].Name == snap.ActiveDeployment {
			return false
		}
		if all[i].Ready != all[j].Ready {
			return all[i].Ready
		}
		return all[i].Name < all[j].Name
	})
	md := FormatDeploymentsMarkdown(all, snap.ActiveDeployment)
	if err != nil && len(all) == 0 {
		return md, err
	}
	return md, nil
}

func (s *Server) handleConnectDeployment(ctx context.Context, rawArgs json.RawMessage) (string, error) {
	var args struct {
		Deployment           string `json:"deployment"`
		Name                 string `json:"name"`
		Cluster              string `json:"cluster"`
		KubeContext          string `json:"kube_context"`
		Context              string `json:"context"`
		Namespace            string `json:"namespace"`
		PlatformAgentName    string `json:"platform_agent_name"`
		DirectNATSURL        string `json:"direct_nats_url"`
		BusUser              string `json:"bus_user"`
		BusAuthValue         string `json:"bus_auth_value"`
		JetskiConversationID string `json:"jetski_conversation_id"`
	}
	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &args)
	}
	if args.JetskiConversationID != "" {
		enabled := true
		_, _ = s.store.UpdatePushConfig(args.JetskiConversationID, &enabled, "")
	}

	var targetProfile DeploymentProfile
	selector := firstNonEmptyStr(args.Deployment, args.Name, args.Cluster, args.KubeContext, args.Context)

	if args.DirectNATSURL != "" {
		name := firstNonEmptyStr(args.Deployment, args.Name, directProfileDefaultName)
		targetProfile = DeploymentProfile{
			Name:              name,
			KubeContext:       firstNonEmptyStr(args.KubeContext, args.Context),
			Namespace:         firstNonEmptyStr(args.Namespace, directProfileDefaultNS),
			PlatformAgentName: firstNonEmptyStr(args.PlatformAgentName, directProfileDefaultPAName),
			DirectNATSURL:     args.DirectNATSURL,
			BusUser:           firstNonEmptyStr(args.BusUser, defaultBusUser),
			BusAuthValue:      args.BusAuthValue,
			Ready:             true,
			LastDiscoveredAt:  time.Now().UTC(),
		}
		_ = s.store.UpsertDeployments([]DeploymentProfile{targetProfile})
	} else {
		p, err := s.store.SetActiveDeployment(selector)
		if err != nil {
			discoverFilter := firstNonEmptyStr(args.KubeContext, args.Context, selector)
			discovered, dErr := DiscoverDeployments(ctx, s.runner, discoverFilter, true)
			if dErr != nil && len(discovered) == 0 {
				return "", fmt.Errorf("discovering deployments: %w", dErr)
			}
			_ = s.store.UpsertDeployments(discovered)
			p, err = s.store.SetActiveDeployment(selector)
			if err != nil {
				return "", err
			}
		}
		targetProfile = p
	}

	if targetProfile.DirectNATSURL == "" && targetProfile.BusAuthValue == "" {
		_ = PopulateDeploymentCredentials(ctx, s.runner, &targetProfile)
		_ = s.store.UpsertDeployments([]DeploymentProfile{targetProfile})
	}

	sess, err := s.connectSession(ctx, targetProfile)
	if err != nil {
		return "", err
	}
	_, _ = s.store.SetActiveDeployment(targetProfile.Name)

	rep := sess.StatusReport(ctx)
	snap := s.store.Snapshot()
	conv := s.store.ActiveConversation()
	return FormatBusStatusMarkdown(rep, conv, snap.Push, s.store.ResolveJetskiConversationID()), nil
}

func (s *Server) handleConnectionStatus(ctx context.Context) (string, error) {
	sess, err := s.ensureConnected(ctx)
	if err != nil {
		return "", err
	}
	rep := sess.StatusReport(ctx)
	snap := s.store.Snapshot()
	conv := s.store.ActiveConversation()
	return FormatBusStatusMarkdown(rep, conv, snap.Push, s.store.ResolveJetskiConversationID()), nil
}

func (s *Server) handleSendMessage(ctx context.Context, rawArgs json.RawMessage) (string, error) {
	var args struct {
		Message              string `json:"message"`
		Prompt               string `json:"prompt"`
		Mode                 string `json:"mode"`
		Conversation         string `json:"conversation"`
		WaitTimeoutSeconds   int    `json:"wait_timeout_seconds"`
		TimeoutSeconds       int    `json:"timeout_seconds"`
		JetskiConversationID string `json:"jetski_conversation_id"`
	}
	if err := json.Unmarshal(rawArgs, &args); err != nil {
		return "", fmt.Errorf("invalid arguments: %w", err)
	}
	if args.JetskiConversationID != "" {
		enabled := true
		_, _ = s.store.UpdatePushConfig(args.JetskiConversationID, &enabled, "")
	}
	sess, err := s.ensureConnected(ctx)
	if err != nil {
		return "", err
	}
	var conv ConversationEntry
	if strings.TrimSpace(args.Conversation) != "" {
		conv, err = s.store.EnsureConversation(args.Conversation, false, "")
		if err != nil {
			return "", err
		}
	} else {
		conv = s.store.ActiveConversation()
	}

	waitTimeout := defaultTurnWaitTimeout
	if args.WaitTimeoutSeconds > 0 {
		waitTimeout = time.Duration(args.WaitTimeoutSeconds) * time.Second
	} else if args.TimeoutSeconds > 0 {
		waitTimeout = time.Duration(args.TimeoutSeconds) * time.Second
	}

	msgText := firstNonEmptyStr(args.Message, args.Prompt)
	outcome, err := sess.SendTurn(ctx, s.store, conv, msgText, args.Mode, waitTimeout)
	md := FormatTurnOutcomeMarkdown(outcome)
	return md, err
}

func (s *Server) handleManageSession(ctx context.Context, rawArgs json.RawMessage) (string, error) {
	var args struct {
		Action       string `json:"action"`
		Alias        string `json:"alias"`
		Conversation string `json:"conversation"`
		Description  string `json:"description"`
	}
	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &args)
	}
	targetAlias := firstNonEmptyStr(args.Alias, args.Conversation)
	action := strings.ToLower(strings.TrimSpace(args.Action))
	if action == "" {
		action = actionList
	}

	switch action {
	case actionList:
		snap := s.store.Snapshot()
		var sb strings.Builder
		sb.WriteString("## 💬 Saved Kube-Agents Conversations & Sessions\n\n")
		sb.WriteString("| Active | Alias | Console Token | Mode | Last Used | Description |\n")
		sb.WriteString("| :--- | :--- | :--- | :--- | :--- | :--- |\n")
		aliases := make([]string, 0, len(snap.Conversations))
		for k := range snap.Conversations {
			aliases = append(aliases, k)
		}
		sort.Strings(aliases)
		for _, a := range aliases {
			c := snap.Conversations[a]
			act := badgeStandby
			if a == snap.ActiveConversation {
				act = badgeActive
			}
			mLabel := "`platform`"
			if c.SessionMode {
				mLabel = "`/session` (persistent worker)"
			}
			fmt.Fprintf(&sb, "| %s | **`%s`** | `console:%s` | %s | `%s` | %s |\n",
				act, c.Alias, c.Token, mLabel, c.LastUsedAt.Format(time.RFC3339), firstNonEmptyStr(c.Description, "—"))
		}
		return sb.String(), nil

	case actionNew:
		alias := strings.TrimSpace(targetAlias)
		if alias == "" {
			alias = fmt.Sprintf("session-%d", time.Now().UTC().Unix())
		}
		entry, err := s.store.EnsureConversation(alias, true, args.Description)
		if err != nil {
			return "", err
		}
		return fmt.Sprintf("✅ Created and switched to new conversation **`%s`** (token `console:%s`).", entry.Alias, entry.Token), nil

	case actionSwitch:
		if strings.TrimSpace(targetAlias) == "" {
			return "", errors.New("switch requires alias")
		}
		entry, err := s.store.EnsureConversation(targetAlias, false, args.Description)
		if err != nil {
			return "", err
		}
		return fmt.Sprintf("✅ Switched active conversation to **`%s`** (token `console:%s`, sessionMode=`%v`).", entry.Alias, entry.Token, entry.SessionMode), nil

	case actionSessionOn, actionSessionStartAlias, actionSessionOff, actionSessionEndAlias, actionStatus, actionHelp:
		sess, err := s.ensureConnected(ctx)
		if err != nil {
			return "", err
		}
		var conv ConversationEntry
		if strings.TrimSpace(targetAlias) != "" {
			conv, err = s.store.EnsureConversation(targetAlias, false, args.Description)
			if err != nil {
				return "", err
			}
		} else {
			conv = s.store.ActiveConversation()
		}
		cmd := cmdSessionOn
		switch action {
		case actionSessionOff, actionSessionEndAlias:
			cmd = cmdSessionOff
		case actionStatus:
			cmd = cmdStatus
		case actionHelp:
			cmd = cmdHelp
		}
		outcome, err := sess.SendTurn(ctx, s.store, conv, cmd, modeAuto, controlNoticeWait)
		return FormatTurnOutcomeMarkdown(outcome), err

	case actionTranscript:
		sess, err := s.ensureConnected(ctx)
		if err != nil {
			return "", err
		}
		conv := s.store.ActiveConversation()
		if strings.TrimSpace(targetAlias) != "" {
			conv, _ = s.store.EnsureConversation(targetAlias, false, "")
		}
		tasks := sess.ListTasks(conv.Token, false, defaultTaskListLimit)
		if len(tasks) == 0 {
			return fmt.Sprintf("No tasks recorded yet on conversation **`%s`** (`console:%s`).", conv.Alias, conv.Token), nil
		}
		var sb strings.Builder
		fmt.Fprintf(&sb, "## 📜 Conversation Transcript: `%s` (`console:%s`)\n\n", conv.Alias, conv.Token)
		for i := len(tasks) - 1; i >= 0; i-- {
			t := tasks[i]
			fmt.Fprintf(&sb, "### Prompt: %s\n", firstNonEmptyStr(t.PromptText, "(no prompt text)"))
			sb.WriteString(FormatTaskRecordMarkdown(t, "Task"))
			sb.WriteString("\n---\n")
		}
		return sb.String(), nil

	default:
		return "", fmt.Errorf("unknown manage_session action %q (expected list, new, switch, session_on, session_off, status, help, transcript)", action)
	}
}

func (s *Server) handleTaskControl(ctx context.Context, rawArgs json.RawMessage) (string, error) {
	var args struct {
		Action             string `json:"action"`
		TaskID             string `json:"task_id"`
		Conversation       string `json:"conversation"`
		Message            string `json:"message"`
		OnlyActive         bool   `json:"only_active"`
		Limit              int    `json:"limit"`
		WaitTimeoutSeconds int    `json:"wait_timeout_seconds"`
	}
	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &args)
	}
	sess, err := s.ensureConnected(ctx)
	if err != nil {
		return "", err
	}
	action := strings.ToLower(strings.TrimSpace(args.Action))
	if action == "" {
		if args.TaskID != "" {
			action = actionGet
		} else {
			action = actionList
		}
	}

	switch action {
	case actionGet:
		if strings.TrimSpace(args.TaskID) == "" {
			return "", errors.New("task_control action 'get' requires task_id")
		}
		rec := sess.GetTask(strings.TrimSpace(args.TaskID))
		if rec == nil {
			return "", fmt.Errorf("task %q not found on TASKS stream", args.TaskID)
		}
		var sb strings.Builder
		sb.WriteString(FormatTaskRecordMarkdown(rec, "Task Detail"))
		children := sess.findChildTasks(rec.TaskID)
		for i, c := range children {
			sb.WriteString("\n---\n")
			sb.WriteString(FormatTaskRecordMarkdown(c, fmt.Sprintf("Delegated Child Task #%d", i+1)))
			if wake := sess.findWakeTask(c.TaskID); wake != nil {
				sb.WriteString("\n---\n")
				sb.WriteString(FormatTaskRecordMarkdown(wake, "Session Wake Synthesis Task"))
			}
		}
		return sb.String(), nil

	case actionList:
		limit := args.Limit
		if limit <= 0 {
			limit = defaultTaskListLimit
		}
		tasks := sess.ListTasks(strings.TrimSpace(args.Conversation), args.OnlyActive, limit)
		if len(tasks) == 0 {
			return "No matching tasks found on `TASKS` stream.\n", nil
		}
		var sb strings.Builder
		sb.WriteString("## 📋 Kube-Agents Tasks (`TASKS`)\n\n")
		sb.WriteString("| Task ID | Addressee | State | Conversation | Delegated From | Updated At | Prompt / Result Summary |\n")
		sb.WriteString("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n")
		for _, t := range tasks {
			summary := firstNonEmptyStr(t.PromptText, t.ResultText, t.ProgressText)
			if t.ResultChat != nil && t.ResultChat.Headline != "" {
				summary = t.ResultChat.Headline
			}
			fmt.Fprintf(&sb, "| **`%s`** | `%s` | %s | `%s` | `%s` | `%s` | %s |\n",
				t.TaskID,
				t.Addressee,
				formatTaskStateBadge(t.State),
				firstNonEmptyStr(t.Conversation, "—"),
				firstNonEmptyStr(t.DelegatedFromTaskID, "—"),
				t.UpdatedAt.Format(time.RFC3339),
				escapeTableCell(truncateText(summary, taskSummaryPreviewChars)),
			)
		}
		return sb.String(), nil

	case actionStatus:
		conv := s.resolveConvArg(args.Conversation)
		outcome, err := sess.SendTurn(ctx, s.store, conv, cmdStatus, modeAuto, controlNoticeWait)
		return FormatTurnOutcomeMarkdown(outcome), err

	case actionStop, actionCancelAlias:
		conv := s.resolveConvArg(args.Conversation)
		outcome, err := sess.SendTurn(ctx, s.store, conv, cmdStop, modeAuto, controlNoticeWait)
		return FormatTurnOutcomeMarkdown(outcome), err

	case actionSteer:
		if strings.TrimSpace(args.Message) == "" {
			return "", errors.New("task_control action 'steer' requires message")
		}
		conv := s.resolveConvArg(args.Conversation)
		waitTimeout := defaultTurnWaitTimeout
		if args.WaitTimeoutSeconds > 0 {
			waitTimeout = time.Duration(args.WaitTimeoutSeconds) * time.Second
		}
		outcome, err := sess.SendTurn(ctx, s.store, conv, args.Message, modeAuto, waitTimeout)
		return FormatTurnOutcomeMarkdown(outcome), err

	default:
		return "", fmt.Errorf("unknown task_control action %q (expected get, list, status, steer, stop)", action)
	}
}

func (s *Server) handleReadTopics(ctx context.Context, rawArgs json.RawMessage) (string, error) {
	var args struct {
		Action string `json:"action"`
		Topic  string `json:"topic"`
	}
	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &args)
	}
	sess, err := s.ensureConnected(ctx)
	if err != nil {
		return "", err
	}
	action := strings.ToLower(strings.TrimSpace(args.Action))
	if action == "" {
		if strings.TrimSpace(args.Topic) != "" {
			action = actionRead
		} else {
			action = actionList
		}
	}

	switch action {
	case actionList:
		return FormatTopicsMarkdown(sess.ListTopics()), nil
	case actionRead:
		if strings.TrimSpace(args.Topic) == "" {
			return FormatTopicsMarkdown(sess.ListTopics()), nil
		}
		snap, err := sess.ReadTopic(args.Topic)
		if err != nil {
			return "", err
		}
		return FormatTopicDetailMarkdown(snap), nil
	case actionCards:
		rep := sess.StatusReport(ctx)
		snap := s.store.Snapshot()
		return FormatBusStatusMarkdown(rep, s.store.ActiveConversation(), snap.Push, s.store.ResolveJetskiConversationID()), nil
	default:
		return "", fmt.Errorf("unknown read_topics action %q (expected list, read, cards)", action)
	}
}

func (s *Server) handleInspectPR(ctx context.Context, rawArgs json.RawMessage) (string, error) {
	var args struct {
		PR        string `json:"pr"`
		Repo      string `json:"repo"`
		OutputDir string `json:"output_dir"`
	}
	if err := json.Unmarshal(rawArgs, &args); err != nil || strings.TrimSpace(args.PR) == "" {
		return "", errors.New("inspect_pr requires 'pr' (GitHub PR URL, owner/repo#number, or PR number)")
	}
	defaultRepo := strings.TrimSpace(args.Repo)
	if defaultRepo == "" {
		if prof, ok := s.store.ActiveDeployment(); ok {
			defaultRepo = prof.GitHubRepo
		}
	}
	insp, err := InspectPullRequest(ctx, s.runner, s.apiBaseURL, args.PR, defaultRepo, args.OutputDir)
	if err != nil {
		return "", err
	}
	return FormatPRInspectionMarkdown(insp), nil
}

func (s *Server) handlePollEvents(ctx context.Context, rawArgs json.RawMessage) (string, error) {
	var args struct {
		JetskiConversationID string `json:"jetski_conversation_id"`
		EnablePush           *bool  `json:"enable_push"`
		AgentAPIPath         string `json:"agentapi_path"`
		PeekOnly             bool   `json:"peek_only"`
	}
	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &args)
	}
	if args.JetskiConversationID != "" || args.EnablePush != nil || args.AgentAPIPath != "" {
		_, err := s.store.UpdatePushConfig(args.JetskiConversationID, args.EnablePush, args.AgentAPIPath)
		if err != nil {
			return "", err
		}
	}
	_, _ = s.ensureConnected(ctx)

	var events []PushEvent
	if args.PeekOnly {
		events = s.store.PeekPendingEvents()
	} else {
		events, _ = s.store.DrainPendingEvents()
	}

	snap := s.store.Snapshot()
	activeConvID := s.store.ResolveJetskiConversationID()
	var sb strings.Builder
	sb.WriteString("## 🔔 Kube-Agents Bi-Directional Push & Event Queue\n\n")
	fmt.Fprintf(&sb, "- **Reactive `agentapi` Push Enabled**: `%v`\n", snap.Push.Enabled)
	fmt.Fprintf(&sb, "- **Bound Jetski Conversation ID**: `%s`\n", firstNonEmptyStr(activeConvID, "(not bound)"))
	fmt.Fprintf(&sb, "- **AgentAPI Binary**: `%s`\n", snap.Push.AgentAPIPath)
	if s.dispatcher != nil {
		fmt.Fprintf(&sb, "- **Events Dispatched via Reactive Wakeup**: `%d`\n", s.dispatcher.DispatchedCount())
	}
	sb.WriteString("\n")

	if len(events) == 0 {
		sb.WriteString("No unread push events currently queued.\n")
	} else {
		fmt.Fprintf(&sb, "### 📬 Drained %d Event(s)\n\n", len(events))
		sb.WriteString(FormatPushEventsMarkdown(events))
	}
	return sb.String(), nil
}

func (s *Server) resolveConvArg(convArg string) ConversationEntry {
	if strings.TrimSpace(convArg) != "" {
		if c, err := s.store.EnsureConversation(convArg, false, ""); err == nil {
			return c
		}
	}
	return s.store.ActiveConversation()
}

func (s *Server) ensureConnected(ctx context.Context) (*BusSession, error) {
	s.mu.Lock()
	if s.session != nil && s.session.nc != nil && s.session.nc.IsConnected() {
		sess := s.session
		s.mu.Unlock()
		return sess, nil
	}
	s.mu.Unlock()

	prof, ok := s.store.ActiveDeployment()
	if !ok {
		discovered, err := DiscoverDeployments(ctx, s.runner, "", true)
		if err != nil && len(discovered) == 0 {
			return nil, fmt.Errorf("%w: %v", errNotConnected, err)
		}
		if len(discovered) == 0 {
			return nil, errNoDeploymentsSaved
		}
		_ = s.store.UpsertDeployments(discovered)
		prof, ok = s.store.ActiveDeployment()
		if !ok {
			return nil, errNoDeploymentsSaved
		}
	}
	return s.connectSession(ctx, prof)
}

func (s *Server) connectSession(ctx context.Context, prof DeploymentProfile) (*BusSession, error) {
	var onPush func(PushEvent)
	if s.dispatcher != nil {
		onPush = s.dispatcher.Enqueue
	}
	newSess, err := ConnectBusSession(ctx, s.runner, prof, onPush)
	if err != nil {
		return nil, err
	}
	s.mu.Lock()
	oldSess := s.session
	s.session = newSess
	s.mu.Unlock()
	if oldSess != nil {
		oldSess.Close()
	}
	return newSess, nil
}

func (s *Server) readResource(ctx context.Context, uri string) (string, error) {
	switch uri {
	case resourceURIStatus:
		return s.handleConnectionStatus(ctx)
	case resourceURIDeployments:
		return s.handleDiscoverDeployments(ctx, nil)
	case resourceURITopics:
		return s.handleReadTopics(ctx, nil)
	case resourceURITasks:
		return s.handleTaskControl(ctx, nil)
	case resourceURIEvents:
		raw, _ := json.Marshal(map[string]any{"peek_only": true})
		return s.handlePollEvents(ctx, raw)
	default:
		return "", fmt.Errorf("unknown resource URI %q", uri)
	}
}

func (s *Server) resourceDefinitions() []mcpResource {
	return []mcpResource{
		{
			URI:         resourceURIStatus,
			Name:        "Kube-Agents Bus & Connection Status",
			Description: "Live connection health, JetStream stream usage, durable consumer liveness, and agent directory cards.",
			MIMEType:    mimeTypeMarkdown,
		},
		{
			URI:         resourceURIDeployments,
			Name:        "Discovered Kube-Agents Deployments",
			Description: "All discovered and saved kube-agents deployments across Kubernetes contexts.",
			MIMEType:    mimeTypeMarkdown,
		},
		{
			URI:         resourceURITopics,
			Name:        "Kube-Agents Blackboard Topics",
			Description: "Standing fleet and cluster state on TOPICS-STATE and TOPICS-JOURNAL.",
			MIMEType:    mimeTypeMarkdown,
		},
		{
			URI:         resourceURITasks,
			Name:        "Recent Kube-Agents Tasks",
			Description: "Recent and in-flight tasks on the TASKS JetStream stream.",
			MIMEType:    mimeTypeMarkdown,
		},
		{
			URI:         resourceURIEvents,
			Name:        "Pending Push Events",
			Description: "Unread asynchronous push notifications and task events from kube-agents.",
			MIMEType:    mimeTypeMarkdown,
		},
	}
}

func (s *Server) toolDefinitions() []mcpToolDefinition {
	return []mcpToolDefinition{
		{
			Name:        toolDiscoverDeployments,
			Description: "Discover remote kube-agents deployments across Kubernetes contexts in ~/.kube/config and list saved deployment profiles.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"kube_context": map[string]any{
						"type":        "string",
						"description": "Optional specific kubectl context to probe. Omit to probe all contexts in parallel.",
					},
					"include_credentials": map[string]any{
						"type":        "boolean",
						"description": "Whether to pre-fetch and cache NATS bus credentials during discovery.",
					},
				},
			},
		},
		{
			Name:        toolConnectDeployment,
			Description: "Connect or switch to a remote kube-agents deployment (by name, cluster name, kubectl context, or direct NATS URL). Automatically manages port-forwarding, credentials, JetStream stream bootstrap, and real-time push subscriptions.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"deployment": map[string]any{
						"type":        "string",
						"description": "Deployment name, cluster name, or kubectl context to connect to. Omit to connect to the default/active deployment.",
					},
					"kube_context": map[string]any{
						"type":        "string",
						"description": "Optional kubectl context override.",
					},
					"namespace": map[string]any{
						"type":        "string",
						"description": "Optional Kubernetes namespace (default: kubeagents-system).",
					},
					"direct_nats_url": map[string]any{
						"type":        "string",
						"description": "Optional direct NATS URL (e.g. nats://127.0.0.1:4222) if connecting without kubectl port-forward.",
					},
					"bus_user": map[string]any{
						"type":        "string",
						"description": "Optional NATS bus user (default: console).",
					},
					"bus_auth_value": map[string]any{
						"type":        "string",
						"description": "Optional NATS bus password when using direct_nats_url.",
					},
					"jetski_conversation_id": map[string]any{
						"type":        "string",
						"description": "Optional active Jetski conversation UUID to bind for real-time agentapi push notifications.",
					},
				},
			},
		},
		{
			Name:        toolConnectionStatus,
			Description: "Inspect the current kube-agents connection, A2A v2 JetStream stream capacities (TASKS, DIRECTORY, TOPICS-STATE, TOPICS-JOURNAL), durable consumer health, registered agent cards, and active session/push state.",
			InputSchema: map[string]any{
				"type":       "object",
				"properties": map[string]any{},
			},
		},
		{
			Name:        toolSendMessage,
			Description: "Send a prompt, question, task, or command to kube-agents over the A2A v2 JetStream bus and stream back thinking, tool activity timelines, progress plans, delegations (session -> platform -> wake), notices, PR links, and structured kube-agents.chat/v1 results.",
			InputSchema: map[string]any{
				"type":     "object",
				"required": []string{"message"},
				"properties": map[string]any{
					"message": map[string]any{
						"type":        "string",
						"description": "The prompt, question, or command (/session, /session off, delegate: ..., status, stop) to send to kube-agents.",
					},
					"mode": map[string]any{
						"type":        "string",
						"enum":        []string{"auto", "platform", "session", "delegate"},
						"description": "Routing mode: 'auto' (use current conversation mode), 'platform' (route to Hermes platform agent), 'session' (ensure /session persistent Claude worker pod is active), or 'delegate' (delegate from session worker to platform agent and wait for wake synthesis).",
					},
					"conversation": map[string]any{
						"type":        "string",
						"description": "Optional conversation alias or console token to send on (defaults to active conversation).",
					},
					"wait_timeout_seconds": map[string]any{
						"type":        "integer",
						"description": "Seconds to wait for task completion before returning a live progress snapshot (default: 90). Background push remains armed if the task continues running.",
					},
					"jetski_conversation_id": map[string]any{
						"type":        "string",
						"description": "Optional active Jetski conversation UUID to bind for real-time agentapi push notifications.",
					},
				},
			},
		},
		{
			Name:        toolManageSession,
			Description: "Manage kube-agents conversations and persistent /session worker pods: list conversations, create a new conversation, switch conversations, toggle /session worker mode on/off, or view the conversation transcript.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"action": map[string]any{
						"type":        "string",
						"enum":        []string{"list", "new", "switch", "session_on", "session_off", "transcript"},
						"description": "Session management action to perform.",
					},
					"alias": map[string]any{
						"type":        "string",
						"description": "Conversation alias (e.g. 'upgrade-triage', 'default') for new/switch/session_on/session_off/transcript.",
					},
					"description": map[string]any{
						"type":        "string",
						"description": "Optional human-readable description when creating or switching a conversation.",
					},
				},
			},
		},
		{
			Name:        toolTaskControl,
			Description: "Inspect or control tasks on the TASKS JetStream stream: get a task's full folded state (thinking, tool activities, progress, delegation chain, result), list recent/active tasks, trigger gateway status replay, steer an in-flight task, or stop/cancel a running task.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"action": map[string]any{
						"type":        "string",
						"enum":        []string{"get", "list", "status", "steer", "stop"},
						"description": "Task control action to perform.",
					},
					"task_id": map[string]any{
						"type":        "string",
						"description": "Task ID to inspect when action is 'get'.",
					},
					"conversation": map[string]any{
						"type":        "string",
						"description": "Optional conversation alias or token filter.",
					},
					"message": map[string]any{
						"type":        "string",
						"description": "Steering instruction text when action is 'steer'.",
					},
					"only_active": map[string]any{
						"type":        "boolean",
						"description": "When action is 'list', only return non-terminal in-flight tasks.",
					},
					"limit": map[string]any{
						"type":        "integer",
						"description": "Maximum number of tasks to return when action is 'list' (default: 20).",
					},
				},
			},
		},
		{
			Name:        toolReadTopics,
			Description: "Read standing blackboard topics from TOPICS-STATE and TOPICS-JOURNAL (e.g. upgrade-readiness, blueprint, annotations) or DIRECTORY agent cards.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"action": map[string]any{
						"type":        "string",
						"enum":        []string{"list", "read", "cards"},
						"description": "Action: 'list' all topics, 'read' a specific topic's latest envelope and JSON data, or 'cards' for DIRECTORY agent cards.",
					},
					"topic": map[string]any{
						"type":        "string",
						"description": "Topic name (e.g. 'upgrade-readiness', 'blueprint', 'annotations') or full subject when action is 'read'.",
					},
				},
			},
		},
		{
			Name:        toolInspectPR,
			Description: "Fetch a GitHub Pull Request opened or referenced by kube-agents (metadata, changed files, unified diff) and save local .diff and per-file .patch files so Jetski can open the PR and diffs natively in its viewer.",
			InputSchema: map[string]any{
				"type":     "object",
				"required": []string{"pr"},
				"properties": map[string]any{
					"pr": map[string]any{
						"type":        "string",
						"description": "GitHub PR URL (https://github.com/owner/repo/pull/123), shorthand (owner/repo#123), or PR number (#123, resolved against the deployment's configured githubRepo).",
					},
					"repo": map[string]any{
						"type":        "string",
						"description": "Optional owner/repo override if passing a bare PR number.",
					},
					"output_dir": map[string]any{
						"type":        "string",
						"description": "Optional local directory to write .diff and .patch files into.",
					},
				},
			},
		},
		{
			Name:        toolPollEvents,
			Description: "Drain pending asynchronous push events received from kube-agents (proactive chat.notify.* alerts/cron/kanban cards, completed background/delegated tasks, PR notices) and/or configure real-time reactive push wakeups via agentapi.",
			InputSchema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"jetski_conversation_id": map[string]any{
						"type":        "string",
						"description": "Active Jetski conversation UUID to register for reactive agentapi push notifications.",
					},
					"enable_push": map[string]any{
						"type":        "boolean",
						"description": "Enable or disable reactive background wakeup via agentapi send-message.",
					},
					"peek_only": map[string]any{
						"type":        "boolean",
						"description": "If true, view queued events without marking them drained.",
					},
				},
			},
		},
	}
}

func marshalRPCResult(id json.RawMessage, result any) []byte {
	raw, _ := json.Marshal(rpcResponse{
		JSONRPC: jsonRPCVersion,
		ID:      id,
		Result:  result,
	})
	return raw
}

func marshalRPCError(id json.RawMessage, code int, msg string) []byte {
	raw, _ := json.Marshal(rpcResponse{
		JSONRPC: jsonRPCVersion,
		ID:      id,
		Error: &rpcError{
			Code:    code,
			Message: msg,
		},
	})
	return raw
}
