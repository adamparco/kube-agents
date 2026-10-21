package mcpbridge

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"
)

const (
	jsonRPCVersion              = "2.0"
	defaultMCPProtocolVersion   = "2024-11-05"
	mcpServerName               = "kube-agents"
	mcpServerVersion            = "0.4.0"
	methodInitialize            = "initialize"
	methodInitialized           = "notifications/initialized"
	methodCancelledNotification = "notifications/cancelled"
	methodProgressNotification  = "notifications/progress"
	methodPing                  = "ping"
	methodToolsList             = "tools/list"
	methodToolsCall             = "tools/call"
	methodResourcesList         = "resources/list"
	methodResourcesRead         = "resources/read"
	methodPromptsList           = "prompts/list"
	toolDiscoverDeployments     = "discover_deployments"
	toolConnectDeployment       = "connect_deployment"
	toolConnectionStatus        = "connection_status"
	toolSendMessage             = "send_message"
	toolManageSession           = "manage_session"
	toolTaskControl             = "task_control"
	toolReadTopics              = "read_topics"
	toolInspectPR               = "inspect_pr"
	toolPollEvents              = "poll_events"
	resourceURIStatus           = "kube-agents://status"
	resourceURIDeployments      = "kube-agents://deployments"
	resourceURITopics           = "kube-agents://topics"
	resourceURITasks            = "kube-agents://tasks"
	resourceURIEvents           = "kube-agents://events"
	mimeTypeMarkdown            = "text/markdown"
	contentTypeText             = "text"
	contentTypeJSON             = "application/json"
	contentTypeNDJSON           = "application/x-ndjson"
	rpcErrParseError            = -32700
	rpcErrInvalidRequest        = -32600
	rpcErrMethodNotFound        = -32601
	rpcErrInvalidParams         = -32602
	rpcErrInternal              = -32603
	scannerInitialBufSize       = 64 * 1024
	scannerMaxBufSize           = 16 * 1024 * 1024
	actionList                  = "list"
	actionNew                   = "new"
	actionSwitch                = "switch"
	actionSessionOn             = "session_on"
	actionSessionStartAlias     = "start"
	actionSessionOff            = "session_off"
	actionSessionEndAlias       = "end"
	actionDelete                = "delete"
	actionRemoveAlias           = "remove"
	actionClearKilled           = "clear_killed"
	actionPruneAlias            = "prune"
	actionHelp                  = "help"
	actionTranscript            = "transcript"
	actionDebugOn               = "debug_on"
	actionDebugOff              = "debug_off"
	directProfileDefaultName    = "direct-gateway"
	directProfileDefaultNS      = "kubeagents-system"
	directProfileDefaultPAName  = "platform-agent"
	asyncReconnectTimeout       = 60 * time.Second
	keepAliveInitialDelay       = 500 * time.Millisecond
	keepAliveTickInterval       = 15 * time.Second
	keepAliveProbeTimeout       = 5 * time.Second
	summaryVerifyInterval       = 5 * time.Second
	convPruneInterval           = 15 * time.Second
	swarmSyncDebounce           = 250 * time.Millisecond
	remoteMCPPath               = "/mcp"
	remoteMCPSwarmPath          = "/mcp/swarm"
	remoteMCPHealthPath         = "/healthz"
	streamingStepContinuePrefix = "⏳ **[STREAMING_STEP_CONTINUE]**"
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

// ToolDefinition describes a single tool exposed by the MCP server.
type ToolDefinition struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	InputSchema map[string]any `json:"inputSchema"`
}

// TextContent represents a text content block inside an MCP tool result.
type TextContent struct {
	Type string `json:"type"`
	Text string `json:"text"`
}

// CallToolResult is the result of a tools/call invocation.
type CallToolResult struct {
	Content        []TextContent `json:"content"`
	IsError        bool          `json:"isError,omitempty"`
	CustomTitle    string        `json:"-"`
	CustomMarkdown string        `json:"-"`
}

// Resource describes an MCP resource URI exposed by the server.
type Resource struct {
	URI         string `json:"uri"`
	Name        string `json:"name"`
	Description string `json:"description,omitempty"`
	MIMEType    string `json:"mimeType,omitempty"`
}

// ServerOptions configures an MCP Server instance.
type ServerOptions struct {
	Store *ConfigStore
	// Cluster supplies Kubernetes discovery and port-forwarding; nil disables them.
	Cluster    ClusterAccess
	Runner     CommandRunner
	APIBaseURL string
	LSBaseURL  string
	Lineage    LineageResolver
	Swarm      SwarmSink
}

type gatewayBridgeSession struct {
	mu          sync.Mutex
	profile     DeploymentProfile
	tunnel      Tunnel
	baseURL     string
	bearerToken string
	httpClient  *http.Client
	closed      bool
}

func connectGatewayBridge(ctx context.Context, cluster ClusterAccess, prof DeploymentProfile) (*gatewayBridgeSession, error) {
	if prof.BusAuthValue == "" && prof.KubeContext != "" && prof.Namespace != "" {
		_ = cluster.PopulateDeploymentCredentials(ctx, &prof)
	}
	bearer := firstNonEmptyStr(strings.TrimSpace(prof.BusAuthValue), strings.TrimSpace(prof.ObserverAuthValue))
	direct := strings.TrimSpace(prof.DirectNATSURL)

	var tun Tunnel
	var baseURL string
	switch {
	case strings.HasPrefix(direct, "http://") || strings.HasPrefix(direct, "https://"):
		baseURL = strings.TrimRight(direct, "/")
	case direct != "" && prof.KubeContext == "":
		// Allow tests or local setups passing a host:port or nats:// URL without KubeContext
		// to map cleanly onto an HTTP URL when reachable, or stay configured for local state.
		trimmed := strings.TrimPrefix(strings.TrimPrefix(direct, "nats://"), "tls://")
		baseURL = "http://" + strings.TrimRight(trimmed, "/")
	default:
		pf, err := cluster.StartGatewayPortForward(ctx, prof)
		if err != nil {
			return nil, err
		}
		tun = pf
		baseURL = pf.MCPURL()
	}

	return &gatewayBridgeSession{
		profile:     prof,
		tunnel:      tun,
		baseURL:     baseURL,
		bearerToken: bearer,
		httpClient:  &http.Client{},
	}, nil
}

func (g *gatewayBridgeSession) Profile() DeploymentProfile {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.profile
}

func (g *gatewayBridgeSession) UpdateProfile(p DeploymentProfile) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.profile = p
}

func (g *gatewayBridgeSession) IsHealthy() bool {
	if g == nil {
		return false
	}
	g.mu.Lock()
	closed := g.closed
	baseURL := g.baseURL
	g.mu.Unlock()
	return !closed && baseURL != ""
}

func (g *gatewayBridgeSession) WaitForHealthy(ctx context.Context, timeout time.Duration) bool {
	if !g.IsHealthy() {
		return false
	}
	probeCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(probeCtx, http.MethodGet, g.baseURL+remoteMCPHealthPath, nil)
	if err != nil {
		return false
	}
	resp, err := g.httpClient.Do(req)
	if err != nil {
		if g.tunnel != nil {
			g.tunnel.Restart()
		}
		return false
	}
	_ = resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

func (g *gatewayBridgeSession) Close() {
	if g == nil {
		return
	}
	g.mu.Lock()
	if g.closed {
		g.mu.Unlock()
		return
	}
	g.closed = true
	tun := g.tunnel
	g.tunnel = nil
	g.mu.Unlock()
	if tun != nil {
		tun.Close()
	}
}

func (g *gatewayBridgeSession) callRemoteTool(
	ctx context.Context,
	toolName string,
	rawArgs json.RawMessage,
	meta rpcCallToolMeta,
	snap PersistentConfig,
	onProgress func(step int, msg string),
) (string, string, string, error) {
	metaMap := map[string]any{
		"conversations":                   snap.Conversations,
		"active_conversation":             snap.ActiveConversation,
		"kube-agents/conversations":       snap.Conversations,
		"kube-agents/active_conversation": snap.ActiveConversation,
	}
	if meta.ConversationID != "" {
		metaMap["antigravity.google/conversation_id"] = meta.ConversationID
	}
	if meta.ArtifactsDir != "" {
		metaMap["antigravity.google/artifacts_dir"] = meta.ArtifactsDir
	}
	if meta.ProgressToken != nil {
		metaMap["progressToken"] = meta.ProgressToken
	} else {
		metaMap["progressToken"] = "bridge-progress"
	}

	var argsAny any = map[string]any{}
	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &argsAny)
	}
	rpcPayload, err := json.Marshal(map[string]any{
		"jsonrpc": jsonRPCVersion,
		"id":      1,
		"method":  methodToolsCall,
		"params": map[string]any{
			"name":      toolName,
			"arguments": argsAny,
			"_meta":     metaMap,
		},
	})
	if err != nil {
		return "", "", "", err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, g.baseURL+remoteMCPPath, bytes.NewReader(rpcPayload))
	if err != nil {
		return "", "", "", err
	}
	req.Header.Set("Content-Type", contentTypeJSON)
	req.Header.Set("Accept", contentTypeNDJSON)
	if g.bearerToken != "" {
		req.Header.Set("Authorization", "Bearer "+g.bearerToken)
	}

	resp, err := g.httpClient.Do(req)
	if err != nil {
		if g.tunnel != nil {
			g.tunnel.Restart()
		}
		return "", "", "", err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return "", "", "", fmt.Errorf("gateway MCP HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}

	var finalResp rpcResponse
	gotFinal := false
	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, scannerInitialBufSize), scannerMaxBufSize)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var frame struct {
			Method string          `json:"method"`
			Params json.RawMessage `json:"params"`
			ID     json.RawMessage `json:"id"`
			Result json.RawMessage `json:"result"`
			Error  *rpcError       `json:"error"`
		}
		if json.Unmarshal([]byte(line), &frame) != nil {
			continue
		}
		if frame.Method == methodProgressNotification && onProgress != nil {
			var prog struct {
				Progress int    `json:"progress"`
				Message  string `json:"message"`
			}
			if json.Unmarshal(frame.Params, &prog) == nil && prog.Message != "" {
				onProgress(prog.Progress, prog.Message)
			}
			continue
		}
		if len(frame.ID) > 0 && string(frame.ID) != "null" {
			_ = json.Unmarshal([]byte(line), &finalResp)
			gotFinal = true
		}
	}
	if !gotFinal {
		return "", "", "", fmt.Errorf("empty JSON-RPC response from gateway MCP")
	}
	if finalResp.Error != nil {
		return "", "", "", fmt.Errorf("%s (code %d)", finalResp.Error.Message, finalResp.Error.Code)
	}
	rawRes, err := json.Marshal(finalResp.Result)
	if err != nil {
		return "", "", "", err
	}
	var toolRes CallToolResult
	if err := json.Unmarshal(rawRes, &toolRes); err != nil {
		return "", "", "", err
	}
	text := ""
	if len(toolRes.Content) > 0 {
		text = toolRes.Content[0].Text
	}
	var customTitle, customMarkdown string
	var customEnv struct {
		Custom bool   `json:"_custom_tool_result"`
		Title  string `json:"title"`
		Result string `json:"result"`
		MD     string `json:"markdown"`
	}
	if strings.HasPrefix(strings.TrimSpace(text), "{") && json.Unmarshal([]byte(text), &customEnv) == nil && customEnv.Custom {
		text = customEnv.Result
		customTitle = customEnv.Title
		customMarkdown = customEnv.MD
	}
	if toolRes.IsError {
		return text, customTitle, customMarkdown, fmt.Errorf("%s", text)
	}
	return text, customTitle, customMarkdown, nil
}

func (g *gatewayBridgeSession) queryRemoteSwarm(ctx context.Context, swarmReq RemoteSwarmRequest) (RemoteSwarmResponse, error) {
	payload, err := json.Marshal(swarmReq)
	if err != nil {
		return RemoteSwarmResponse{}, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, g.baseURL+remoteMCPSwarmPath, bytes.NewReader(payload))
	if err != nil {
		return RemoteSwarmResponse{}, err
	}
	req.Header.Set("Content-Type", contentTypeJSON)
	if g.bearerToken != "" {
		req.Header.Set("Authorization", "Bearer "+g.bearerToken)
	}
	resp, err := g.httpClient.Do(req)
	if err != nil {
		return RemoteSwarmResponse{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return RemoteSwarmResponse{}, fmt.Errorf("gateway /mcp/swarm HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	var out RemoteSwarmResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return RemoteSwarmResponse{}, err
	}
	return out, nil
}

// Server implements the kube-agents Model Context Protocol (MCP) thin bridge over stdio.
type Server struct {
	store      *ConfigStore
	cluster    ClusterAccess
	runner     CommandRunner
	apiBaseURL string
	lsBaseURL  string
	dispatcher *PushDispatcher
	lineage    LineageResolver
	swarm      SwarmSink

	mu                   sync.Mutex
	connectMu            sync.Mutex
	syncMu               sync.Mutex
	session              *gatewayBridgeSession
	lastRootConvID       string
	lastArtifactsDir     string
	swarmTimer           *time.Timer
	restampCancels       map[string]context.CancelFunc
	inFlightCancels      map[string]context.CancelFunc
	activeStreamingTasks map[string]*activeStreamingTask
	adoptedSubagents     map[string]string
	lastSummaryVerifyAt  time.Time
	lastConvPruneAt      time.Time
	writeMu              sync.Mutex
	notifyOut            io.Writer
}

type activeStreamingTask struct {
	TaskID       string
	ConvAlias    string
	AGYConvID    string
	ArtifactsDir string
	InFlight     bool
	CancelCall   context.CancelFunc
	StartedAt    time.Time
	UpdatedAt    time.Time
}

type rpcCallToolMeta struct {
	ProgressToken  any    `json:"progressToken,omitempty"`
	ConversationID string `json:"antigravity.google/conversation_id,omitempty"`
	ArtifactsDir   string `json:"antigravity.google/artifacts_dir,omitempty"`
}

type callMetaContextKey struct{}
type progressCallbackKey struct{}

func withCallMeta(ctx context.Context, meta rpcCallToolMeta) context.Context {
	return context.WithValue(ctx, callMetaContextKey{}, meta)
}

func callMetaFromContext(ctx context.Context) rpcCallToolMeta {
	if ctx == nil {
		return rpcCallToolMeta{}
	}
	if m, ok := ctx.Value(callMetaContextKey{}).(rpcCallToolMeta); ok {
		return m
	}
	return rpcCallToolMeta{}
}

// WithProgressCallback attaches a progress callback to ctx for streaming tool progress updates.
func WithProgressCallback(ctx context.Context, fn func(step int, message string)) context.Context {
	return context.WithValue(ctx, progressCallbackKey{}, fn)
}

func progressCallbackFromContext(ctx context.Context) func(step int, message string) {
	if ctx == nil {
		return nil
	}
	if fn, ok := ctx.Value(progressCallbackKey{}).(func(int, string)); ok {
		return fn
	}
	return nil
}

// NewServer constructs a new kube-agents MCP bridge server.
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
	lineage := opts.Lineage
	if lineage == nil {
		lineage = NewLineageResolverForStore(store)
	}
	swarm := opts.Swarm
	if swarm == nil {
		swarm = NewAGYSwarmSink()
	}
	dispatcher := NewPushDispatcher(store, runner)
	cluster := opts.Cluster
	if cluster == nil {
		cluster = noClusterAccess{}
	}
	return &Server{
		store:                store,
		cluster:              cluster,
		runner:               runner,
		apiBaseURL:           opts.APIBaseURL,
		lsBaseURL:            opts.LSBaseURL,
		dispatcher:           dispatcher,
		lineage:              lineage,
		swarm:                swarm,
		restampCancels:       make(map[string]context.CancelFunc),
		inFlightCancels:      make(map[string]context.CancelFunc),
		activeStreamingTasks: make(map[string]*activeStreamingTask),
		adoptedSubagents:     make(map[string]string),
	}, nil
}

// Close shuts down the active gateway bridge session and background push dispatcher.
func (s *Server) Close() {
	s.mu.Lock()
	if s.swarmTimer != nil {
		s.swarmTimer.Stop()
		s.swarmTimer = nil
	}
	for id, cancel := range s.restampCancels {
		cancel()
		delete(s.restampCancels, id)
	}
	for id, cancel := range s.inFlightCancels {
		cancel()
		delete(s.inFlightCancels, id)
	}
	for id, st := range s.activeStreamingTasks {
		if st != nil && st.CancelCall != nil {
			st.CancelCall()
		}
		delete(s.activeStreamingTasks, id)
	}
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

func normalizeRPCID(raw json.RawMessage) string {
	s := strings.TrimSpace(string(raw))
	return strings.Trim(s, `"`)
}

// ServeStdio reads newline-delimited JSON-RPC 2.0 requests from r and writes responses to w.
func (s *Server) ServeStdio(ctx context.Context, r io.Reader, w io.Writer) error {
	s.writeMu.Lock()
	s.notifyOut = w
	s.writeMu.Unlock()
	defer func() {
		s.writeMu.Lock()
		if s.notifyOut == w {
			s.notifyOut = nil
		}
		s.writeMu.Unlock()
	}()

	loopCtx, cancelLoop := context.WithCancel(ctx)
	defer cancelLoop()
	go s.runKeepAliveLoop(loopCtx)

	var wg sync.WaitGroup
	defer wg.Wait()

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
		rawBytes := []byte(line)
		var peek rpcRequest
		if json.Unmarshal(rawBytes, &peek) == nil && peek.Method == methodToolsCall && len(peek.ID) > 0 && string(peek.ID) != "null" {
			reqKey := normalizeRPCID(peek.ID)
			reqCtx, cancelReq := context.WithCancel(ctx)
			s.mu.Lock()
			if s.inFlightCancels == nil {
				s.inFlightCancels = make(map[string]context.CancelFunc)
			}
			s.inFlightCancels[reqKey] = cancelReq
			s.mu.Unlock()

			wg.Add(1)
			go func(rCtx context.Context, rCancel context.CancelFunc, key string, payload []byte) {
				defer wg.Done()
				defer func() {
					rCancel()
					s.mu.Lock()
					delete(s.inFlightCancels, key)
					s.mu.Unlock()
				}()
				resp := s.HandleJSONRPC(rCtx, payload)
				if len(resp) == 0 {
					return
				}
				s.writeMu.Lock()
				_, _ = fmt.Fprintf(w, "%s\n", resp)
				s.writeMu.Unlock()
			}(reqCtx, cancelReq, reqKey, rawBytes)
			continue
		}

		resp := s.HandleJSONRPC(ctx, rawBytes)
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

// RunCLI parses CLI flags (--config, --hook, --ui-action, --version) and runs either the Antigravity lifecycle
// hook handler, a Swarm UI Overview action, or the stdio MCP server with clean signal handling.
func RunCLI(args []string, cluster ClusterAccess) error {
	fs := flag.NewFlagSet("mcp", flag.ContinueOnError)
	configPath := fs.String("config", "", "path to persistent MCP configuration file (default: ~/.config/kube-agents/mcp-config.json)")
	hookMode := fs.Bool("hook", false, "run as an Antigravity SessionStart/PreInvocation lifecycle hook")
	uiAction := fs.String("ui-action", "", "execute a Swarm UI Overview action (connect, disconnect, refresh, init)")
	uiDeployment := fs.String("deployment", "", "target deployment name for --ui-action=connect")
	uiConvID := fs.String("conversation-id", "", "target Antigravity conversation ID for --ui-action")
	versionMode := fs.Bool("version", false, "print kube-agents-mcp version and exit")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *versionMode || (fs.NArg() > 0 && fs.Arg(0) == "version") {
		fmt.Printf("%s %s\n", mcpServerName, mcpServerVersion)
		return nil
	}
	store, err := NewConfigStore(*configPath)
	if err != nil {
		return err
	}
	if *hookMode {
		return HandleAGYHook(store, os.Stdin, os.Stdout)
	}
	if strings.TrimSpace(*uiAction) != "" {
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		return HandleUIAction(ctx, store, cluster, *uiAction, *uiDeployment, *uiConvID, os.Stdout)
	}
	rpcOut := os.Stdout
	os.Stdout = os.Stderr
	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo})))

	srv, err := NewServer(ServerOptions{Store: store, Cluster: cluster})
	if err != nil {
		return err
	}
	defer srv.Close()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	served := make(chan error, 1)
	go func() { served <- srv.ServeStdio(ctx, os.Stdin, rpcOut) }()
	select {
	case err := <-served:
		return err
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (s *Server) emitProgress(progressToken any, step int, message string) {
	if progressToken == nil || strings.TrimSpace(message) == "" {
		return
	}
	frame, err := json.Marshal(map[string]any{
		"jsonrpc": jsonRPCVersion,
		"method":  methodProgressNotification,
		"params": map[string]any{
			"progressToken": progressToken,
			"progress":      step,
			"total":         0,
			"message":       message,
		},
	})
	if err != nil {
		return
	}
	s.writeMu.Lock()
	defer s.writeMu.Unlock()
	if s.notifyOut != nil {
		_, _ = fmt.Fprintf(s.notifyOut, "%s\n", frame)
	}
}

// HandleJSONRPC processes a single JSON-RPC 2.0 request frame and returns the marshaled response.
func (s *Server) HandleJSONRPC(ctx context.Context, raw []byte) []byte {
	var req rpcRequest
	if err := json.Unmarshal(raw, &req); err != nil {
		return marshalRPCError(json.RawMessage("null"), rpcErrParseError, "invalid JSON-RPC frame")
	}
	if len(req.ID) == 0 || string(req.ID) == "null" {
		if req.Method == methodCancelledNotification {
			var cancelParams struct {
				RequestID json.RawMessage `json:"requestId"`
			}
			if len(req.Params) > 0 && json.Unmarshal(req.Params, &cancelParams) == nil {
				reqKey := normalizeRPCID(cancelParams.RequestID)
				s.mu.Lock()
				cancelFn := s.inFlightCancels[reqKey]
				s.mu.Unlock()
				if cancelFn != nil {
					cancelFn()
				}
			}
			return nil
		}
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
			Meta      rpcCallToolMeta `json:"_meta"`
		}
		if err := json.Unmarshal(req.Params, &callParams); err != nil || callParams.Name == "" {
			return marshalRPCError(req.ID, rpcErrInvalidParams, "tools/call requires tool name")
		}
		if callParams.Meta.ConversationID != "" && s.store != nil {
			lin := s.resolveLineage(callParams.Meta.ConversationID, callParams.Meta.ArtifactsDir)
			if s.shouldClaimCoordinatorLineage(callParams.Name, callParams.Arguments, lin, callParams.Meta.ArtifactsDir) {
				s.recordCallLineage(lin, callParams.Meta.ArtifactsDir)
				enabled := true
				_, _ = s.store.UpdatePushConfigWithLineage(lin, &enabled, "")
			}
		}
		callCtx := withCallMeta(ctx, callParams.Meta)
		if callParams.Meta.ProgressToken != nil {
			token := callParams.Meta.ProgressToken
			callCtx = WithProgressCallback(callCtx, func(step int, message string) {
				s.emitProgress(token, step, message)
			})
		}
		res := s.CallTool(callCtx, callParams.Name, callParams.Arguments)
		if !res.IsError && len(res.Content) > 0 && (res.CustomTitle != "" || res.CustomMarkdown != "") && (callParams.Meta.ConversationID != "" || callParams.Meta.ProgressToken != nil) {
			txt := res.Content[0].Text
			md := txt
			if res.CustomMarkdown != "" {
				md = res.CustomMarkdown
			}
			envelope := map[string]any{
				"_custom_tool_result": true,
				"_version":            1,
				"result":              txt,
				"markdown":            md,
			}
			if res.CustomTitle != "" {
				envelope["title"] = res.CustomTitle
			}
			if wrapped, err := json.Marshal(envelope); err == nil {
				res.Content[0].Text = string(wrapped)
			}
		}
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
func (s *Server) CallTool(ctx context.Context, name string, rawArgs json.RawMessage) CallToolResult {
	var text, customTitle, customMarkdown string
	var err error

	switch name {
	case toolDiscoverDeployments:
		text, err = s.handleDiscoverDeployments(ctx, rawArgs)
	case toolConnectDeployment:
		text, err = s.handleConnectDeployment(ctx, rawArgs)
	case toolConnectionStatus:
		text, err = s.handleConnectionStatus(ctx)
	case toolSendMessage:
		text, customTitle, customMarkdown, err = s.handleSendMessage(ctx, rawArgs)
	case toolManageSession:
		text, customTitle, customMarkdown, err = s.handleManageSession(ctx, rawArgs)
	case toolTaskControl:
		text, customTitle, customMarkdown, err = s.handleTaskControl(ctx, rawArgs)
	case toolReadTopics:
		text, err = s.handleReadTopics(ctx, rawArgs)
	case toolInspectPR:
		text, err = s.handleInspectPR(ctx, rawArgs)
	case toolPollEvents:
		text, err = s.handlePollEvents(ctx, rawArgs)
	default:
		return CallToolResult{
			Content: []TextContent{{Type: contentTypeText, Text: fmt.Sprintf("Unknown tool %q", name)}},
			IsError: true,
		}
	}

	isStreamingStep := strings.HasPrefix(text, streamingStepContinuePrefix)
	if isStreamingStep || !toolMutatesState(name) {
		s.scheduleSwarmSync()
	} else {
		s.syncSwarm(ctx)
	}

	if name != toolPollEvents && s.dispatcher != nil && !isStreamingStep {
		callerID := strings.TrimSpace(callMetaFromContext(ctx).ConversationID)
		if piggyback := s.dispatcher.DrainAndFormatPiggybackFor(callerID); piggyback != "" {
			text += piggyback
			if customMarkdown != "" {
				customMarkdown += piggyback
			}
		}
	}

	if err != nil {
		msg := fmt.Sprintf("❌ **Error in `%s`**: %v", name, err)
		if text != "" {
			msg = text + "\n\n" + msg
		}
		return CallToolResult{
			Content: []TextContent{{Type: contentTypeText, Text: msg}},
			IsError: true,
		}
	}
	return CallToolResult{
		Content:        []TextContent{{Type: contentTypeText, Text: text}},
		CustomTitle:    customTitle,
		CustomMarkdown: customMarkdown,
	}
}

func toolMutatesState(name string) bool {
	switch name {
	case toolConnectDeployment, toolSendMessage, toolManageSession, toolTaskControl:
		return true
	default:
		return false
	}
}

func (s *Server) handleDiscoverDeployments(ctx context.Context, rawArgs json.RawMessage) (string, error) {
	var args struct {
		KubeContext        string `json:"kube_context"`
		IncludeCredentials *bool  `json:"include_credentials"`
	}
	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &args)
	}
	includeCreds := true
	if args.IncludeCredentials != nil {
		includeCreds = *args.IncludeCredentials
	}
	discovered, err := s.cluster.DiscoverDeployments(ctx, args.KubeContext, includeCreds)
	if len(discovered) > 0 && s.store != nil {
		if strings.TrimSpace(args.KubeContext) == "" {
			_ = s.store.SyncDiscoveredDeployments(discovered)
		} else {
			_ = s.store.UpsertDeployments(discovered)
		}
	}
	var all []DeploymentProfile
	activeName := ""
	if s.store != nil {
		snap := s.store.Snapshot()
		activeName = snap.ActiveDeployment
		for _, p := range snap.Deployments {
			all = append(all, p)
		}
		sort.Slice(all, func(i, j int) bool {
			if all[i].Ready != all[j].Ready {
				return all[i].Ready
			}
			return all[i].Name < all[j].Name
		})
	} else {
		all = discovered
	}
	if err != nil && len(all) == 0 {
		return "", err
	}
	return FormatDeploymentsMarkdown(all, activeName), nil
}

func (s *Server) handleConnectDeployment(ctx context.Context, rawArgs json.RawMessage) (string, error) {
	var args struct {
		Deployment        string `json:"deployment"`
		KubeContext       string `json:"kube_context"`
		Namespace         string `json:"namespace"`
		DirectNATSURL     string `json:"direct_nats_url"`
		BusUser           string `json:"bus_user"`
		BusAuthValue      string `json:"bus_auth_value"`
		AGYConversationID string `json:"agy_conversation_id"`
	}
	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &args)
	}
	targetAGYID := firstNonEmptyStr(strings.TrimSpace(args.AGYConversationID), strings.TrimSpace(callMetaFromContext(ctx).ConversationID))
	if targetAGYID != "" && targetAGYID != defaultDisconnectedSwarmID {
		lin := s.resolveLineage(targetAGYID, callMetaFromContext(ctx).ArtifactsDir)
		s.recordCallLineage(lin, callMetaFromContext(ctx).ArtifactsDir)
		enabled := true
		_, _ = s.store.UpdatePushConfigWithLineage(lin, &enabled, "")
	}

	var prof DeploymentProfile
	if strings.TrimSpace(args.DirectNATSURL) != "" {
		name := firstNonEmptyStr(strings.TrimSpace(args.Deployment), directProfileDefaultName)
		ns := firstNonEmptyStr(strings.TrimSpace(args.Namespace), directProfileDefaultNS)
		if existing, err := s.store.SetActiveDeployment(name); err == nil {
			prof = existing
		} else {
			prof = DeploymentProfile{
				Name:              name,
				Namespace:         ns,
				PlatformAgentName: directProfileDefaultPAName,
				ClusterName:       name,
				Ready:             true,
			}
		}
		prof.DirectNATSURL = strings.TrimSpace(args.DirectNATSURL)
		if args.KubeContext != "" {
			prof.KubeContext = args.KubeContext
		}
		if args.BusUser != "" {
			prof.BusUser = args.BusUser
		}
		if args.BusAuthValue != "" {
			prof.BusAuthValue = args.BusAuthValue
		}
		_ = s.store.UpsertDeployments([]DeploymentProfile{prof})
		_, _ = s.store.SetActiveDeployment(prof.Name)
	} else {
		var err error
		prof, err = s.store.SetActiveDeployment(args.Deployment)
		if err != nil {
			discovered, dErr := s.cluster.DiscoverDeployments(ctx, args.KubeContext, true)
			if len(discovered) > 0 {
				_ = s.store.UpsertDeployments(discovered)
				prof, err = s.store.SetActiveDeployment(args.Deployment)
			}
			if err != nil {
				if dErr != nil {
					return "", fmt.Errorf("%w (discovery error: %v)", err, dErr)
				}
				return "", err
			}
		}
	}

	conv := s.conversationFor(ctx, "", args.AGYConversationID)
	if s.store != nil {
		s.store.SetConversationDisconnected(firstNonEmptyStr(args.AGYConversationID, callMetaFromContext(ctx).ConversationID, conv.Alias), false)
	}
	EnrichDeploymentVersionStatus(ctx, &prof)
	if s.store != nil {
		_ = s.store.UpsertDeployments([]DeploymentProfile{prof})
	}
	sess, err := s.connectSession(ctx, prof)
	if err != nil {
		return "", err
	}
	s.syncSwarm(ctx)

	if text, _, _, rErr := sess.callRemoteTool(ctx, toolConnectionStatus, nil, callMetaFromContext(ctx), s.store.Snapshot(), nil); rErr == nil && text != "" {
		return text, nil
	}
	return fmt.Sprintf("## 🔌 Connected to Kube-Agents Deployment `%s`\n\n- **Namespace / PlatformAgent**: `%s/%s`\n- **Gateway Endpoint**: `%s`\n- **Active Conversation**: **`%s`** (token `console:%s`)\n",
		prof.Name, prof.Namespace, prof.PlatformAgentName, sess.baseURL, conv.Alias, conv.Token), nil
}

func (s *Server) handleConnectionStatus(ctx context.Context) (string, error) {
	sess, err := s.ensureConnected(ctx)
	if err != nil {
		return "", err
	}
	text, _, _, cErr := sess.callRemoteTool(ctx, toolConnectionStatus, nil, callMetaFromContext(ctx), s.store.Snapshot(), nil)
	if cErr != nil {
		prof := sess.Profile()
		conv := s.readOnlyConversationFor(ctx, "")
		return fmt.Sprintf("## 🔌 Kube-Agents Gateway Status\n\n- **Active Deployment**: **`%s`** (`%s/%s`)\n- **Gateway Endpoint**: `%s`\n- **Active Conversation**: **`%s`** (`console:%s`)\n",
			prof.Name, prof.Namespace, prof.PlatformAgentName, sess.baseURL, conv.Alias, conv.Token), nil
	}
	return text, nil
}

func (s *Server) handleSendMessage(ctx context.Context, rawArgs json.RawMessage) (string, string, string, error) {
	var args map[string]any
	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &args)
	}
	if args == nil {
		args = make(map[string]any)
	}
	convArg, _ := args["conversation"].(string)
	agyID, _ := args["agy_conversation_id"].(string)
	conv := s.conversationFor(ctx, convArg, agyID)
	args["conversation"] = conv.Alias
	msgText, _ := args["message"].(string)
	mode, _ := args["mode"].(string)
	trimmedMsg := strings.TrimSpace(msgText)
	if strings.EqualFold(mode, "session") || trimmedMsg == "/session" || strings.HasPrefix(trimmedMsg, "/session ") && !strings.EqualFold(trimmedMsg, "/session off") {
		_, _ = s.store.SetConversationSessionMode(conv.Alias, true)
	} else if strings.EqualFold(trimmedMsg, "/session off") {
		_, _ = s.store.SetConversationSessionMode(conv.Alias, false)
	}

	sess, err := s.ensureConnected(ctx)
	if err != nil {
		return "", "", "", err
	}
	meta := callMetaFromContext(ctx)
	callerID := firstNonEmptyStr(strings.TrimSpace(agyID), strings.TrimSpace(meta.ConversationID))
	callCtx, cancelCall := context.WithCancel(ctx)
	defer cancelCall()

	streamKey := firstNonEmptyStr(conv.Alias, callerID, "default")
	s.registerStreamingCallStart(streamKey, "", conv.Alias, callerID, meta.ArtifactsDir, cancelCall)
	updatedArgs, _ := json.Marshal(args)
	txt, title, md, callErr := sess.callRemoteTool(callCtx, toolSendMessage, updatedArgs, meta, s.store.Snapshot(), progressCallbackFromContext(ctx))
	s.finishStreamingCall(streamKey, conv.Alias, callerID, meta.ArtifactsDir, txt, callCtx.Err(), callErr)
	return txt, title, md, callErr
}

func (s *Server) handleManageSession(ctx context.Context, rawArgs json.RawMessage) (string, string, string, error) {
	var args struct {
		Action      string `json:"action"`
		Alias       string `json:"alias"`
		Description string `json:"description"`
		Debug       *bool  `json:"debug"`
	}
	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &args)
	}
	action := strings.ToLower(strings.TrimSpace(args.Action))
	if action == "" {
		action = actionList
	}
	meta := callMetaFromContext(ctx)

	switch action {
	case actionNew:
		lin := s.resolveLineage(meta.ConversationID, meta.ArtifactsDir)
		var entry ConversationEntry
		var err error
		if lin.IsSubagent && lin.ConversationID != "" {
			entry, err = s.store.EnsureConversationForSubagent(lin, args.Alias, true, args.Description)
		} else {
			activate := s.isCallerActiveCoordinatorOrSubagent(lin, meta.ArtifactsDir)
			entry, err = s.store.EnsureConversationWithOptions(args.Alias, true, args.Description, activate)
			if err == nil && lin.ConversationID != "" {
				s.store.BindConversationWithLineage(entry.Alias, lin)
			}
		}
		if err != nil {
			return "", "", "", err
		}
		return fmt.Sprintf("Created conversation **`%s`** (token `console:%s`).\n", entry.Alias, entry.Token), "", "", nil

	case actionSwitch:
		entry := s.conversationFor(ctx, args.Alias, "")
		return fmt.Sprintf("Switched active conversation to **`%s`** (token `console:%s`).\n", entry.Alias, entry.Token), "", "", nil

	case actionSessionOn, actionSessionStartAlias:
		entry := s.conversationFor(ctx, args.Alias, "")
		entry, _ = s.store.SetConversationSessionMode(entry.Alias, true)
		if sess, err := s.ensureConnected(ctx); err == nil {
			fwdArgs, _ := json.Marshal(map[string]any{"action": actionSessionOn, "alias": entry.Alias})
			if txt, title, md, rErr := sess.callRemoteTool(ctx, toolManageSession, fwdArgs, meta, s.store.Snapshot(), progressCallbackFromContext(ctx)); rErr == nil {
				return txt, title, md, nil
			}
		}
		return fmt.Sprintf("Enabled persistent `/session` worker mode on **`%s`** (`console:%s`).\n", entry.Alias, entry.Token), "", "", nil

	case actionSessionOff, actionSessionEndAlias:
		entry := s.conversationFor(ctx, args.Alias, "")
		entry, _ = s.store.SetConversationSessionMode(entry.Alias, false)
		if sess, err := s.ensureConnected(ctx); err == nil {
			fwdArgs, _ := json.Marshal(map[string]any{"action": actionSessionOff, "alias": entry.Alias})
			if txt, title, md, rErr := sess.callRemoteTool(ctx, toolManageSession, fwdArgs, meta, s.store.Snapshot(), progressCallbackFromContext(ctx)); rErr == nil {
				return txt, title, md, nil
			}
		}
		return fmt.Sprintf("Terminated `/session` worker mode on **`%s`** (`console:%s`).\n", entry.Alias, entry.Token), "", "", nil

	case actionDelete, actionRemoveAlias:
		target := strings.TrimSpace(args.Alias)
		deleted, ok := s.store.DeleteConversation(target)
		_, subRemoved := s.cleanupDeletedSessionSubagent(ctx, deleted.AGYConversationID, target, meta.ArtifactsDir)
		if sess, err := s.ensureConnected(ctx); err == nil {
			_, _, _, _ = sess.callRemoteTool(ctx, toolManageSession, rawArgs, meta, s.store.Snapshot(), nil)
		}
		if !ok && !subRemoved {
			return fmt.Sprintf("Conversation or session %q was not found.\n", target), "", "", nil
		}
		return fmt.Sprintf("Deleted session **`%s`**.\n", firstNonEmptyStr(deleted.Alias, target)), "", "", nil

	case actionClearKilled, actionPruneAlias:
		removed := s.store.ClearKilledSessions(nil)
		for _, r := range removed {
			s.cleanupDeletedSessionSubagent(ctx, r.AGYConversationID, r.Alias, meta.ArtifactsDir)
		}
		if sess, err := s.ensureConnected(ctx); err == nil {
			_, _, _, _ = sess.callRemoteTool(ctx, toolManageSession, rawArgs, meta, s.store.Snapshot(), nil)
		}
		return fmt.Sprintf("Cleared %d terminated/inactive session(s).\n", len(removed)), "", "", nil

	case actionDebugOn, actionDebugOff:
		enable := action == actionDebugOn
		entry := s.readOnlyConversationFor(ctx, args.Alias)
		if entry.Alias != "" {
			_ = s.store.SetConversationDebugMode(entry.Alias, enable)
		} else {
			_ = s.store.SetDebugMode(enable)
		}
		return fmt.Sprintf("Debug output set to `%v` for **`%s`**.\n", enable, firstNonEmptyStr(entry.Alias, "global")), "", "", nil
	}

	if sess, err := s.ensureConnected(ctx); err == nil {
		if txt, title, md, rErr := sess.callRemoteTool(ctx, toolManageSession, rawArgs, meta, s.store.Snapshot(), progressCallbackFromContext(ctx)); rErr == nil {
			return txt, title, md, nil
		}
	}
	snap := s.store.Snapshot()
	var sb strings.Builder
	sb.WriteString("## 💬 Kube-Agents Console Conversations\n\n")
	for alias, c := range snap.Conversations {
		fmt.Fprintf(&sb, "- **`%s`** (`console:%s`, sessionMode=`%v`)\n", alias, c.Token, c.SessionMode)
	}
	return sb.String(), "", "", nil
}

func (s *Server) handleTaskControl(ctx context.Context, rawArgs json.RawMessage) (string, string, string, error) {
	sess, err := s.ensureConnected(ctx)
	if err != nil {
		return "", "", "", err
	}
	var args struct {
		Action            string `json:"action"`
		TaskID            string `json:"task_id"`
		Conversation      string `json:"conversation"`
		AGYConversationID string `json:"agy_conversation_id"`
	}
	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &args)
	}
	act := strings.ToLower(strings.TrimSpace(args.Action))
	taskID := strings.TrimSpace(args.TaskID)
	meta := callMetaFromContext(ctx)
	callerID := firstNonEmptyStr(strings.TrimSpace(args.AGYConversationID), strings.TrimSpace(meta.ConversationID))
	conv := s.readOnlyConversationFor(ctx, args.Conversation)

	if act == "stop" || act == "cancel" {
		s.clearStreamingTask(taskID, conv.Alias)
		return sess.callRemoteTool(ctx, toolTaskControl, rawArgs, meta, s.store.Snapshot(), progressCallbackFromContext(ctx))
	}

	if act == "" || act == "get" || act == "steer" {
		callCtx, cancelCall := context.WithCancel(ctx)
		defer cancelCall()
		streamKey := firstNonEmptyStr(taskID, conv.Alias, callerID, "default")
		s.registerStreamingCallStart(streamKey, taskID, conv.Alias, callerID, meta.ArtifactsDir, cancelCall)
		txt, title, md, callErr := sess.callRemoteTool(callCtx, toolTaskControl, rawArgs, meta, s.store.Snapshot(), progressCallbackFromContext(ctx))
		s.finishStreamingCall(streamKey, conv.Alias, callerID, meta.ArtifactsDir, txt, callCtx.Err(), callErr)
		return txt, title, md, callErr
	}

	return sess.callRemoteTool(ctx, toolTaskControl, rawArgs, meta, s.store.Snapshot(), progressCallbackFromContext(ctx))
}

func extractStreamingTaskID(text string) string {
	idx := strings.Index(text, "task-")
	if idx < 0 {
		return ""
	}
	rest := text[idx:]
	end := 0
	for end < len(rest) {
		c := rest[end]
		if (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '-' || c == '_' {
			end++
		} else {
			break
		}
	}
	if end <= len("task-") {
		return ""
	}
	return rest[:end]
}

func (s *Server) registerStreamingCallStart(key, taskID, convAlias, agyConvID, artifactsDir string, cancelCall context.CancelFunc) {
	if s == nil || key == "" {
		return
	}
	now := time.Now()
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.activeStreamingTasks == nil {
		s.activeStreamingTasks = make(map[string]*activeStreamingTask)
	}
	// Clear any completed/older entries for the same conversation if starting a new task
	for k, existing := range s.activeStreamingTasks {
		if existing != nil && existing.ConvAlias == convAlias && taskID != "" && existing.TaskID == taskID && k != key {
			delete(s.activeStreamingTasks, k)
		}
	}
	entry := s.activeStreamingTasks[key]
	if entry == nil {
		entry = &activeStreamingTask{
			StartedAt: now,
		}
		s.activeStreamingTasks[key] = entry
	}
	if taskID != "" {
		entry.TaskID = taskID
	}
	if convAlias != "" {
		entry.ConvAlias = convAlias
	}
	if agyConvID != "" {
		entry.AGYConvID = agyConvID
	}
	if artifactsDir != "" {
		entry.ArtifactsDir = artifactsDir
	}
	entry.InFlight = true
	entry.CancelCall = cancelCall
	entry.UpdatedAt = now
}

func (s *Server) finishStreamingCall(key, convAlias, agyConvID, artifactsDir, responseText string, ctxErr, callErr error) {
	if s == nil {
		return
	}
	extractedTaskID := extractStreamingTaskID(responseText)
	isContinue := strings.HasPrefix(responseText, streamingStepContinuePrefix) || strings.Contains(responseText, "STREAMING_STEP_CONTINUE")

	s.mu.Lock()
	if s.activeStreamingTasks == nil {
		s.activeStreamingTasks = make(map[string]*activeStreamingTask)
	}
	entry := s.activeStreamingTasks[key]
	knownTaskID := extractedTaskID
	if entry != nil && knownTaskID == "" {
		knownTaskID = entry.TaskID
	}
	delete(s.activeStreamingTasks, key)

	if ctxErr != nil {
		s.mu.Unlock()
		go s.stopRemoteTaskOnCancel(convAlias, knownTaskID)
		return
	}

	if isContinue && callErr == nil && knownTaskID != "" {
		now := time.Now()
		s.activeStreamingTasks[knownTaskID] = &activeStreamingTask{
			TaskID:       knownTaskID,
			ConvAlias:    convAlias,
			AGYConvID:    agyConvID,
			ArtifactsDir: artifactsDir,
			InFlight:     false,
			StartedAt:    now,
			UpdatedAt:    now,
		}
	}
	s.mu.Unlock()
}

func (s *Server) clearStreamingTask(taskID, convAlias string) {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for k, entry := range s.activeStreamingTasks {
		if (taskID != "" && (k == taskID || (entry != nil && entry.TaskID == taskID))) ||
			(taskID == "" && convAlias != "" && entry != nil && entry.ConvAlias == convAlias) {
			if entry != nil && entry.CancelCall != nil {
				entry.CancelCall()
			}
			delete(s.activeStreamingTasks, k)
		}
	}
}

func (s *Server) stopRemoteTaskOnCancel(convAlias, taskID string) {
	if s == nil {
		return
	}
	stopCtx, cancel := context.WithTimeout(context.Background(), 6*time.Second)
	defer cancel()
	sess, err := s.ensureConnected(stopCtx)
	if err != nil || sess == nil {
		return
	}
	stopArgs := map[string]any{
		"action": "stop",
	}
	if strings.TrimSpace(taskID) != "" {
		stopArgs["task_id"] = strings.TrimSpace(taskID)
	}
	if strings.TrimSpace(convAlias) != "" {
		stopArgs["conversation"] = strings.TrimSpace(convAlias)
	}
	raw, _ := json.Marshal(stopArgs)
	_, _, _, _ = sess.callRemoteTool(stopCtx, toolTaskControl, raw, rpcCallToolMeta{}, s.store.Snapshot(), nil)
	s.syncSwarmBackground()
}

func (s *Server) checkActiveStreamingTasksForCancel(ctx context.Context) {
	if s == nil || ctx.Err() != nil {
		return
	}
	now := time.Now()
	type candidate struct {
		key   string
		entry activeStreamingTask
	}
	var candidates []candidate

	s.mu.Lock()
	lsURL := s.lsBaseURL
	lastArt := s.lastArtifactsDir
	for k, st := range s.activeStreamingTasks {
		if st == nil {
			delete(s.activeStreamingTasks, k)
			continue
		}
		if now.Sub(st.UpdatedAt) > 10*time.Minute {
			delete(s.activeStreamingTasks, k)
			continue
		}
		if st.AGYConvID == "" {
			continue
		}
		candidates = append(candidates, candidate{key: k, entry: *st})
	}
	s.mu.Unlock()

	if len(candidates) == 0 {
		return
	}

	for _, cand := range candidates {
		brainDir := ""
		if s.lineage != nil {
			brainDir = s.lineage.BrainDir(firstNonEmptyStr(cand.entry.ArtifactsDir, lastArt))
		}
		checkCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
		canceled := IsAGYCascadeUserCanceled(checkCtx, lsURL, brainDir, cand.entry.AGYConvID)
		cancel()
		if !canceled {
			continue
		}
		s.mu.Lock()
		cur := s.activeStreamingTasks[cand.key]
		if cur == nil {
			s.mu.Unlock()
			continue
		}
		cancelFn := cur.CancelCall
		taskID := cur.TaskID
		convAlias := cur.ConvAlias
		delete(s.activeStreamingTasks, cand.key)
		s.mu.Unlock()

		if cancelFn != nil {
			cancelFn()
		}
		s.stopRemoteTaskOnCancel(convAlias, taskID)
	}
}

func (s *Server) handleReadTopics(ctx context.Context, rawArgs json.RawMessage) (string, error) {
	sess, err := s.ensureConnected(ctx)
	if err != nil {
		return "", err
	}
	txt, _, _, err := sess.callRemoteTool(ctx, toolReadTopics, rawArgs, callMetaFromContext(ctx), s.store.Snapshot(), nil)
	return txt, err
}

func (s *Server) handleInspectPR(ctx context.Context, rawArgs json.RawMessage) (string, error) {
	var args struct {
		PR        string `json:"pr"`
		Repo      string `json:"repo"`
		OutputDir string `json:"output_dir"`
	}
	if err := json.Unmarshal(rawArgs, &args); err != nil {
		return "", fmt.Errorf("invalid inspect_pr arguments: %w", err)
	}
	defaultRepo := strings.TrimSpace(args.Repo)
	if defaultRepo == "" && s.store != nil {
		if active, ok := s.store.ActiveDeployment(); ok {
			defaultRepo = active.GitHubRepo
		}
	}
	outDir := strings.TrimSpace(args.OutputDir)
	if outDir == "" {
		if artDir := strings.TrimSpace(callMetaFromContext(ctx).ArtifactsDir); artDir != "" {
			if repo, num, pErr := ParsePRReference(args.PR, defaultRepo); pErr == nil {
				safeRepo := strings.ReplaceAll(repo, "/", "-")
				outDir = filepath.Join(artDir, "scratch", "prs", fmt.Sprintf("%s-pr-%d", safeRepo, num))
			}
		}
	}
	insp, err := InspectPullRequest(ctx, s.runner, s.apiBaseURL, args.PR, defaultRepo, outDir)
	if err != nil {
		return "", err
	}
	return FormatPRInspectionMarkdown(insp), nil
}

func (s *Server) handlePollEvents(ctx context.Context, rawArgs json.RawMessage) (string, error) {
	var args struct {
		AGYConversationID string `json:"agy_conversation_id"`
		EnablePush        *bool  `json:"enable_push"`
		PeekOnly          bool   `json:"peek_only"`
	}
	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &args)
	}
	meta := callMetaFromContext(ctx)
	convID := firstNonEmptyStr(strings.TrimSpace(args.AGYConversationID), strings.TrimSpace(meta.ConversationID))
	if strings.TrimSpace(args.AGYConversationID) != "" || args.EnablePush != nil {
		lin := s.resolveLineage(convID, meta.ArtifactsDir)
		_, _ = s.store.UpdatePushConfigWithLineage(lin, args.EnablePush, "")
	}
	var events []PushEvent
	if args.PeekOnly {
		events = s.store.PeekPendingEventsForAGY(convID)
	} else {
		events, _ = s.store.DrainPendingEventsForAGY(convID)
	}
	if len(events) == 0 {
		return "No pending push events in queue.\n", nil
	}
	return "## 📬 Kube-Agents Push Events\n\n" + FormatPushEventsMarkdown(events), nil
}

func (s *Server) ensureConnected(ctx context.Context) (*gatewayBridgeSession, error) {
	s.mu.Lock()
	cur := s.session
	s.mu.Unlock()
	if cur != nil && cur.IsHealthy() {
		return cur, nil
	}

	s.connectMu.Lock()
	defer s.connectMu.Unlock()

	s.mu.Lock()
	cur = s.session
	s.mu.Unlock()
	if cur != nil && cur.IsHealthy() {
		return cur, nil
	}

	prof, ok := s.store.ActiveDeployment()
	if !ok {
		discovered, err := s.cluster.DiscoverDeployments(ctx, "", true)
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
	return s.connectSessionLocked(ctx, prof)
}

func (s *Server) ensureConnectedAsync() {
	s.mu.Lock()
	cur := s.session
	s.mu.Unlock()
	if cur != nil && cur.IsHealthy() {
		return
	}
	if !s.connectMu.TryLock() {
		return
	}
	go func() {
		defer s.connectMu.Unlock()
		ctx, cancel := context.WithTimeout(context.Background(), asyncReconnectTimeout)
		defer cancel()
		s.mu.Lock()
		cur := s.session
		s.mu.Unlock()
		if cur != nil && cur.IsHealthy() {
			return
		}
		prof, ok := s.store.ActiveDeployment()
		if !ok {
			return
		}
		if _, err := s.connectSessionLocked(ctx, prof); err == nil {
			s.syncSwarmBackground()
		}
	}()
}

func (s *Server) runKeepAliveLoop(ctx context.Context) {
	initTimer := time.NewTimer(keepAliveInitialDelay)
	select {
	case <-ctx.Done():
		initTimer.Stop()
		return
	case <-initTimer.C:
		s.keepAliveTick(ctx)
	}

	ticker := time.NewTicker(keepAliveTickInterval)
	defer ticker.Stop()
	cancelWatchTicker := time.NewTicker(1 * time.Second)
	defer cancelWatchTicker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-cancelWatchTicker.C:
			s.checkActiveStreamingTasksForCancel(ctx)
		case <-ticker.C:
			s.keepAliveTick(ctx)
		}
	}
}

func (s *Server) keepAliveTick(ctx context.Context) {
	if s == nil || ctx.Err() != nil {
		return
	}
	s.mu.Lock()
	sess := s.session
	s.mu.Unlock()

	disconnected := s.store != nil && s.store.IsConversationDisconnected("")
	if !disconnected && (sess == nil || !sess.IsHealthy()) {
		s.ensureConnectedAsync()
		s.syncSwarmBackground()
		return
	}
	s.syncSwarmBackground()
}

func (s *Server) connectSession(ctx context.Context, prof DeploymentProfile) (*gatewayBridgeSession, error) {
	s.connectMu.Lock()
	defer s.connectMu.Unlock()
	return s.connectSessionLocked(ctx, prof)
}

func (s *Server) connectSessionLocked(ctx context.Context, prof DeploymentProfile) (*gatewayBridgeSession, error) {
	newSess, err := connectGatewayBridge(ctx, s.cluster, prof)
	if err != nil {
		return nil, err
	}
	if s.store != nil {
		_ = s.store.UpsertDeployments([]DeploymentProfile{newSess.Profile()})
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

func (s *Server) readOnlyConversationFor(ctx context.Context, convArg string) ConversationEntry {
	if s == nil || s.store == nil {
		return ConversationEntry{}
	}
	if trimmed := strings.TrimSpace(convArg); trimmed != "" {
		if c, ok := s.store.LookupConversation(trimmed); ok {
			return c
		}
		return ConversationEntry{
			Alias: trimmed,
			Token: strings.TrimPrefix(trimmed, consoleConvPrefix),
		}
	}
	meta := callMetaFromContext(ctx)
	if cid := strings.TrimSpace(meta.ConversationID); cid != "" {
		if c, ok := s.store.LookupConversation(cid); ok {
			return c
		}
	}
	return s.store.ActiveConversation()
}

func (s *Server) activeLiveRootCoordinatorID(brainDir string) string {
	bDir := strings.TrimSpace(brainDir)
	if s == nil || s.store == nil || bDir == "" {
		return ""
	}
	snap := s.store.Snapshot()
	isSub := func(cid string) bool {
		if cid == "" {
			return true
		}
		if s.lineage != nil {
			if l := s.lineage.ResolveLineage(cid, ""); l.IsSubagent {
				return true
			}
		}
		for _, entry := range snap.Conversations {
			if strings.TrimSpace(entry.AGYConversationID) == cid && isSessionSubagentEntry(entry) {
				return true
			}
		}
		return false
	}
	var candidates []string
	if strings.TrimSpace(snap.Push.AGYConversationID) != "" {
		candidates = append(candidates, strings.TrimSpace(snap.Push.AGYConversationID))
	}
	if active, ok := snap.Conversations[snap.ActiveConversation]; ok && strings.TrimSpace(active.AGYConversationID) != "" {
		candidates = append(candidates, strings.TrimSpace(active.AGYConversationID))
	}
	s.mu.Lock()
	if s.lastRootConvID != "" {
		candidates = append(candidates, s.lastRootConvID)
	}
	s.mu.Unlock()

	for _, cand := range candidates {
		if cand == "" || !IsAGYConversationAlive(bDir, cand) || isSub(cand) {
			continue
		}
		return cand
	}
	if fallbackID, _ := mostRecentLiveCoordinatorEntry(s.lineage, snap, bDir, ""); fallbackID != "" {
		return fallbackID
	}
	return ""
}

func (s *Server) isCallerActiveCoordinatorOrSubagent(lin ConversationLineage, artifactsDirHint string) bool {
	if lin.ConversationID == "" {
		return true
	}
	brainDir := ""
	if s != nil && s.lineage != nil {
		brainDir = s.lineage.BrainDir(artifactsDirHint)
	}
	currentRoot := s.activeLiveRootCoordinatorID(brainDir)
	if currentRoot == "" {
		return true
	}
	return lin.ConversationID == currentRoot || lin.RootConversationID == currentRoot
}

func (s *Server) shouldClaimCoordinatorLineage(
	name string,
	rawArgs json.RawMessage,
	lin ConversationLineage,
	artifactsDirHint string,
) bool {
	if lin.ConversationID == "" {
		return false
	}
	brainDir := ""
	if s != nil && s.lineage != nil {
		brainDir = s.lineage.BrainDir(artifactsDirHint)
	}
	currentRoot := s.activeLiveRootCoordinatorID(brainDir)
	if currentRoot == "" {
		return true
	}
	if lin.ConversationID == currentRoot || (lin.IsSubagent && lin.RootConversationID == currentRoot) {
		return true
	}
	if lin.IsSubagent {
		return false
	}
	var commonArgs struct {
		Conversation      string `json:"conversation"`
		Alias             string `json:"alias"`
		AGYConversationID string `json:"agy_conversation_id"`
	}
	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &commonArgs)
	}
	if expID := strings.TrimSpace(commonArgs.AGYConversationID); expID != "" && expID != lin.ConversationID {
		return false
	}
	if strings.HasPrefix(strings.TrimSpace(commonArgs.Conversation), "subagent-") ||
		strings.HasPrefix(strings.TrimSpace(commonArgs.Alias), "subagent-") {
		return false
	}
	if name == toolConnectDeployment {
		return true
	}
	// If the caller is a live top-level user conversation (not a subagent) and explicitly passed
	// its own agy_conversation_id, promote it to active root coordinator so switching between
	// Antigravity conversations updates immediately without letting passive background tools hijack.
	if strings.TrimSpace(commonArgs.AGYConversationID) == lin.ConversationID {
		return true
	}
	return false
}

func (s *Server) conversationFor(ctx context.Context, convArg string, explicitAGYID string) ConversationEntry {
	meta := callMetaFromContext(ctx)
	agyID := firstNonEmptyStr(explicitAGYID, meta.ConversationID)
	lin := s.resolveLineage(agyID, meta.ArtifactsDir)
	isActiveCoordFamily := explicitAGYID != "" || s.isCallerActiveCoordinatorOrSubagent(lin, meta.ArtifactsDir)
	if lin.ConversationID != "" && isActiveCoordFamily {
		s.recordCallLineage(lin, meta.ArtifactsDir)
	}
	if lin.IsSubagent && lin.ConversationID != "" {
		if c, err := s.store.EnsureConversationForSubagent(lin, convArg, false, ""); err == nil {
			return c
		}
	}
	if trimmed := strings.TrimSpace(convArg); trimmed != "" {
		activate := isActiveCoordFamily && !lin.IsSubagent
		if c, err := s.store.EnsureConversationWithOptions(trimmed, false, "", activate); err == nil {
			if lin.ConversationID != "" {
				if !isSessionSubagentEntry(c) && !c.SessionMode && strings.TrimSpace(c.SessionWorkerID) == "" && (c.AGYConversationID == "" || c.AGYConversationID == lin.ConversationID) {
					s.store.BindConversationWithLineage(c.Alias, lin)
					c.AGYConversationID = lin.ConversationID
				}
			}
			return c
		}
	}
	if lin.ConversationID != "" {
		return s.store.ConversationForAGYWithOptions(lin, isActiveCoordFamily)
	}
	return s.store.ActiveConversation()
}

func (s *Server) resolveLineage(conversationID string, artifactsDirHint string) ConversationLineage {
	id := strings.TrimSpace(conversationID)
	if id == "" {
		return ConversationLineage{}
	}
	s.mu.Lock()
	lastRoot := s.lastRootConvID
	s.mu.Unlock()
	return ResolveLineageWithStore(s.lineage, s.store, id, artifactsDirHint, lastRoot)
}

func (s *Server) scheduleSubagentSummaryRestamp(subagentID, rootID, role string) {
	if s == nil || !isCanonicalUUID(subagentID) || !isCanonicalUUID(rootID) || subagentID == rootID {
		return
	}
	s.mu.Lock()
	if s.restampCancels == nil {
		s.restampCancels = make(map[string]context.CancelFunc)
	}
	if prev, ok := s.restampCancels[subagentID]; ok {
		prev()
	}
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	s.restampCancels[subagentID] = cancel
	lsURL := s.lsBaseURL
	s.mu.Unlock()

	go func() {
		defer cancel()
		roles := map[string]string{subagentID: role}
		if _, err := ReparentLiveAGYSummaries(ctx, lsURL, rootID, roles, true); err != nil {
			return
		}
		ticker := time.NewTicker(500 * time.Millisecond)
		defer ticker.Stop()
		idleCount := 0
		wasRunning := false
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				statuses, err := ReparentLiveAGYSummaries(ctx, lsURL, rootID, roles, false)
				if err != nil {
					return
				}
				st := statuses[subagentID]
				running := st == "CASCADE_RUN_STATUS_RUNNING"
				if wasRunning && !running {
					if _, err := ReparentLiveAGYSummaries(ctx, lsURL, rootID, roles, true); err != nil {
						return
					}
				}
				wasRunning = running
				if st != "" && !running {
					idleCount++
					if idleCount >= 3 {
						return
					}
				} else {
					idleCount = 0
				}
			}
		}
	}()
}

func (s *Server) adoptActiveSessionSubagents(
	ctx context.Context,
	rootID string,
	brainDir string,
	conversations map[string]ConversationEntry,
) {
	if s == nil || rootID == "" || brainDir == "" || !IsAGYConversationAlive(brainDir, rootID) {
		return
	}
	if s.lineage != nil {
		if l := s.lineage.ResolveLineage(rootID, ""); l.IsSubagent {
			return
		}
	}
	adopter, _ := s.lineage.(interface {
		AdoptSubagent(brainDir, newParentID, subagentID, role string) error
	})
	subagentRoles := make(map[string]string)
	anyChanged := false

	for alias, c := range conversations {
		subID := strings.TrimSpace(c.AGYConversationID)
		if subID == "" || subID == rootID || !isSessionSubagentEntry(c) {
			continue
		}
		if c.SessionTerminated && !c.SessionMode {
			continue
		}
		if !IsAGYConversationAlive(brainDir, subID) {
			continue
		}
		role := firstNonEmptyStr(strings.TrimSpace(c.SubagentRole), humanizeSubagentRole(alias))
		cacheKey := rootID + "\x00" + role
		descPath := filepath.Join(brainDir, rootID, ".system_generated", "subagents", subID+".json")
		_, descErr := os.Stat(descPath)

		s.mu.Lock()
		if s.adoptedSubagents == nil {
			s.adoptedSubagents = make(map[string]string)
		}
		alreadyAdopted := s.adoptedSubagents[subID] == cacheKey && descErr == nil
		s.mu.Unlock()

		if !alreadyAdopted {
			if adopter != nil {
				_ = adopter.AdoptSubagent(brainDir, rootID, subID, role)
			}
			_, _ = ReparentTrajectoryDB(brainDir, subID, rootID)
			s.mu.Lock()
			s.adoptedSubagents[subID] = cacheKey
			s.mu.Unlock()
			anyChanged = true
		}

		if strings.TrimSpace(c.ParentAGYConversationID) != rootID || strings.TrimSpace(c.SubagentRole) != role {
			if s.store != nil {
				s.store.SetConversationParentAGYID(alias, rootID, role)
			}
			c.ParentAGYConversationID = rootID
			c.SubagentRole = role
			conversations[alias] = c
			anyChanged = true
		}
		subagentRoles[subID] = role
	}

	if len(subagentRoles) > 0 && isCanonicalUUID(rootID) {
		now := time.Now()
		s.mu.Lock()
		shouldVerify := anyChanged || s.lastSummaryVerifyAt.IsZero() || now.Sub(s.lastSummaryVerifyAt) >= summaryVerifyInterval
		lsURL := s.lsBaseURL
		s.mu.Unlock()

		if shouldVerify {
			syncCtx, cancel := context.WithTimeout(ctx, lsHTTPTimeout)
			_, err := ReparentLiveAGYSummaries(syncCtx, lsURL, rootID, subagentRoles, anyChanged)
			cancel()
			if err == nil {
				s.mu.Lock()
				s.lastSummaryVerifyAt = now
				s.mu.Unlock()
			}
		}
	}
}

func (s *Server) recordCallLineage(lin ConversationLineage, artifactsDirHint string) {
	rootID := firstNonEmptyStr(lin.RootConversationID, lin.ConversationID)
	if rootID == "" {
		return
	}
	s.mu.Lock()
	s.lastRootConvID = rootID
	if strings.TrimSpace(artifactsDirHint) != "" {
		s.lastArtifactsDir = strings.TrimSpace(artifactsDirHint)
	}
	s.mu.Unlock()
	if lin.IsSubagent && lin.RootConversationID != "" && lin.RootConversationID != lin.ConversationID {
		s.scheduleSubagentSummaryRestamp(lin.ConversationID, lin.RootConversationID, lin.SubagentRole)
	}
}

func (s *Server) scheduleSwarmSync() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.swarmTimer != nil {
		s.swarmTimer.Reset(swarmSyncDebounce)
		return
	}
	s.swarmTimer = time.AfterFunc(swarmSyncDebounce, func() {
		s.mu.Lock()
		s.swarmTimer = nil
		s.mu.Unlock()
		s.syncSwarmBackground()
	})
}

func (s *Server) syncSwarm(ctx context.Context) {
	if s == nil || s.swarm == nil || s.store == nil {
		return
	}
	s.syncMu.Lock()
	defer s.syncMu.Unlock()
	s.syncSwarmLocked(ctx)
}

func (s *Server) syncSwarmBackground() {
	if s == nil || s.swarm == nil || s.store == nil {
		return
	}
	if !s.syncMu.TryLock() {
		s.scheduleSwarmSync()
		return
	}
	defer s.syncMu.Unlock()
	s.syncSwarmLocked(context.Background())
}

func (s *Server) syncSwarmLocked(ctx context.Context) {
	meta := callMetaFromContext(ctx)
	s.mu.Lock()
	sess := s.session
	lastRoot := s.lastRootConvID
	lastArt := s.lastArtifactsDir
	lastPrune := s.lastConvPruneAt
	s.mu.Unlock()

	artHint := firstNonEmptyStr(meta.ArtifactsDir, lastArt)
	brainDir := ""
	if s.lineage != nil {
		brainDir = s.lineage.BrainDir(artHint)
	}
	now := time.Now().UTC()
	if brainDir != "" && (meta.ConversationID != "" || lastPrune.IsZero() || now.Sub(lastPrune) >= convPruneInterval) {
		s.mu.Lock()
		s.lastConvPruneAt = now
		s.mu.Unlock()
		s.store.PruneStaleConversations(brainDir)
	}
	activeCoord := s.activeLiveRootCoordinatorID(brainDir)
	callerID := firstNonEmptyStr(activeCoord, meta.ConversationID, lastRoot, s.store.ResolveAGYConversationID())
	if callerID == "" {
		return
	}
	lin := s.resolveLineage(callerID, artHint)
	rootID := firstNonEmptyStr(activeCoord, lin.RootConversationID, lin.ConversationID)
	if rootID == "" || brainDir == "" || !IsAGYConversationAlive(brainDir, rootID) {
		return
	}

	snap := s.store.Snapshot()
	var dep DeploymentProfile
	if sess != nil {
		dep = sess.Profile()
	} else if activeDep, ok := s.store.ActiveDeployment(); ok {
		dep = activeDep
	}
	EnrichDeploymentVersionStatus(ctx, &dep)

	var discovered []DeploymentProfile
	for _, p := range snap.Deployments {
		discovered = append(discovered, p)
	}
	if _, err := os.Stat(filepath.Join(brainDir, defaultDisconnectedSwarmID, systemGeneratedDirName, swarmDirName, swarmMembersFileName)); err != nil {
		SyncDefaultDisconnectedOverview(ctx, brainDir, s.store)
	}

	disconnected := s.store.IsConversationDisconnected(rootID)

	s.adoptActiveSessionSubagents(ctx, rootID, brainDir, snap.Conversations)

	var subagents map[string]ConversationLineage
	if s.lineage != nil {
		subagents = s.lineage.ListSubagents(rootID, artHint)
	}
	if len(subagents) > 0 && len(snap.Conversations) > 0 && brainDir != "" {
		boundSubIDs := make(map[string]bool, len(snap.Conversations))
		for _, c := range snap.Conversations {
			if cid := strings.TrimSpace(c.AGYConversationID); cid != "" {
				boundSubIDs[cid] = true
			}
		}
		for subID, subLin := range subagents {
			if boundSubIDs[subID] || !subLin.IsSessionSubagent() {
				continue
			}
			descPath := filepath.Join(brainDir, rootID, systemGeneratedDirName, subagentsDirName, subID+".json")
			if st, err := os.Stat(descPath); err == nil && now.Sub(st.ModTime()) > 2*time.Minute {
				if remover, ok := s.lineage.(interface {
					RemoveSubagent(brainDir, subagentID string) bool
				}); ok {
					remover.RemoveSubagent(brainDir, subID)
				} else {
					_ = os.Remove(descPath)
				}
				delete(subagents, subID)
			}
		}
	}

	if !disconnected && sess != nil {
		prevLedger, existingLines := ReadLocalSwarmState(brainDir, rootID)
		queryCtx, cancel := context.WithTimeout(ctx, keepAliveProbeTimeout)
		remoteResp, err := sess.queryRemoteSwarm(queryCtx, RemoteSwarmRequest{
			RootConversationID: rootID,
			Deployment:         dep,
			Conversations:      snap.Conversations,
			ActiveConversation: snap.ActiveConversation,
			Subagents:          subagents,
			Ledger:             prevLedger,
			ExistingBoardLines: existingLines,
			DrainEventsFor:     meta.ConversationID,
		})
		cancel()
		if err == nil {
			enrichRemoteOverviewState(&remoteResp, rootID, dep, discovered, snap.ActiveConversation, snap.Push.Enabled)
			if len(remoteResp.Conversations) > 0 {
				s.store.MergeRemoteConversations(remoteResp.Conversations, remoteResp.ActiveConversation)
			}
			if remoteResp.Deployment.Name != "" {
				sess.UpdateProfile(remoteResp.Deployment)
				_ = s.store.UpsertDeployments([]DeploymentProfile{remoteResp.Deployment})
			}
			if s.dispatcher != nil {
				for _, ev := range remoteResp.PendingEvents {
					s.dispatcher.Enqueue(ev)
				}
			}
			_ = s.swarm.ApplyRemoteSnapshot(ctx, rootID, brainDir, remoteResp)
			return
		}
	}

	_ = s.swarm.Sync(ctx, SwarmProjectionInput{
		RootConversationID:    rootID,
		BrainDir:              brainDir,
		Deployment:            dep,
		DiscoveredDeployments: discovered,
		Connected:             !disconnected && ((sess != nil && sess.IsHealthy()) || s.store.IsDefaultOverviewConnected(rootID)),
		ActiveConversation:    snap.ActiveConversation,
		PushArmed:             snap.Push.Enabled,
		Conversations:         snap.Conversations,
		Subagents:             subagents,
		Now:                   now,
	})
}

// HandleUIAction executes an interactive Swarm UI Overview button action (connect, disconnect,
// refresh, init) and immediately updates the on-disk .system_generated/swarm/ files.
func HandleUIAction(ctx context.Context, store *ConfigStore, cluster ClusterAccess, action, deployment, convID string, w io.Writer) error {
	if store == nil {
		var err error
		store, err = NewConfigStore("")
		if err != nil {
			return err
		}
	}
	act := strings.ToLower(strings.TrimSpace(action))
	_ = EnsureAGYMCPServerReady(ctx, "")
	resolver := NewLineageResolverForStore(store)
	brainDir := resolver.BrainDir("")
	rootID := strings.TrimSpace(convID)
	if rootID != "" && rootID != defaultDisconnectedSwarmID {
		lin := ResolveLineageWithStore(resolver, store, rootID, "", "")
		rootID = firstNonEmptyStr(lin.RootConversationID, lin.ConversationID, rootID)
	} else if rootID == "" {
		rootID = store.ResolveAGYConversationID()
	}

	writeResp := func(payload map[string]any) error {
		if w == nil {
			return nil
		}
		return json.NewEncoder(w).Encode(payload)
	}

	switch act {
	case "disconnect":
		store.SetConversationDisconnected(rootID, true)
		snap := store.Snapshot()
		var dep DeploymentProfile
		if activeDep, ok := store.ActiveDeployment(); ok {
			dep = activeDep
		}
		EnrichDeploymentVersionStatus(ctx, &dep)
		var discovered []DeploymentProfile
		for _, p := range snap.Deployments {
			discovered = append(discovered, p)
		}
		seenRoots := make(map[string]bool)
		for _, candidateID := range []string{rootID, store.ResolveAGYConversationID()} {
			cid := strings.TrimSpace(candidateID)
			if cid == "" || cid == defaultDisconnectedSwarmID || seenRoots[cid] || !IsAGYConversationAlive(brainDir, cid) {
				continue
			}
			seenRoots[cid] = true
			lin := ResolveLineageWithStore(resolver, store, cid, "", "")
			_ = store.ConversationForAGYWithOptions(lin, true)
			store.SetConversationDisconnected(cid, true)
			_ = NewAGYSwarmSink().Sync(ctx, SwarmProjectionInput{
				RootConversationID:    cid,
				BrainDir:              brainDir,
				Deployment:            dep,
				DiscoveredDeployments: discovered,
				Connected:             false,
				ActiveConversation:    snap.ActiveConversation,
				PushArmed:             snap.Push.Enabled,
				Conversations:         snap.Conversations,
				Now:                   time.Now().UTC(),
			})
		}
		SyncDefaultDisconnectedOverview(ctx, brainDir, store)
		return writeResp(map[string]any{"ok": true, "action": "disconnect", "connected": false})

	case "connect":
		targetRootID := rootID
		if targetRootID == defaultDisconnectedSwarmID {
			targetRootID = ""
		}
		store.SetConversationDisconnected(targetRootID, false)
		if targetRootID != "" && brainDir != "" {
			_ = os.MkdirAll(filepath.Join(brainDir, targetRootID), 0755)
		}
		srv, err := NewServer(ServerOptions{Store: store, Cluster: cluster})
		if err != nil {
			return writeResp(map[string]any{"ok": false, "action": "connect", "error": err.Error()})
		}
		defer srv.Close()
		if targetRootID != "" {
			srv.mu.Lock()
			srv.lastRootConvID = targetRootID
			srv.mu.Unlock()
		}
		rawArgs, _ := json.Marshal(map[string]any{
			"deployment":          strings.TrimSpace(deployment),
			"agy_conversation_id": targetRootID,
		})
		callCtx := withCallMeta(ctx, rpcCallToolMeta{ConversationID: targetRootID})
		_, cErr := srv.handleConnectDeployment(callCtx, rawArgs)
		if cErr != nil {
			return writeResp(map[string]any{"ok": false, "action": "connect", "error": cErr.Error()})
		}
		SyncDefaultDisconnectedOverview(ctx, brainDir, store)
		return writeResp(map[string]any{"ok": true, "action": "connect", "deployment": deployment, "connected": true, "conversationId": targetRootID})

	default: // "refresh", "init"
		if act == "refresh" {
			if discovered, dErr := cluster.DiscoverDeployments(ctx, "", true); dErr == nil && len(discovered) > 0 {
				_ = store.SyncDiscoveredDeployments(discovered)
			}
		} else {
			snap := store.Snapshot()
			if len(snap.Deployments) == 0 {
				if discovered, dErr := cluster.DiscoverDeployments(ctx, "", true); dErr == nil && len(discovered) > 0 {
					_ = store.SyncDiscoveredDeployments(discovered)
				}
			}
		}
		SyncDefaultDisconnectedOverview(ctx, brainDir, store)
		if rootID != "" && rootID != defaultDisconnectedSwarmID && IsAGYConversationAlive(brainDir, rootID) {
			if store.IsConversationDisconnected(rootID) {
				snap := store.Snapshot()
				var dep DeploymentProfile
				if activeDep, ok := store.ActiveDeployment(); ok {
					dep = activeDep
				}
				EnrichDeploymentVersionStatus(ctx, &dep)
				var discovered []DeploymentProfile
				for _, p := range snap.Deployments {
					discovered = append(discovered, p)
				}
				_ = NewAGYSwarmSink().Sync(ctx, SwarmProjectionInput{
					RootConversationID:    rootID,
					BrainDir:              brainDir,
					Deployment:            dep,
					DiscoveredDeployments: discovered,
					Connected:             false,
					ActiveConversation:    snap.ActiveConversation,
					PushArmed:             snap.Push.Enabled,
					Conversations:         snap.Conversations,
					Now:                   time.Now().UTC(),
				})
			} else {
				lin := ResolveLineageWithStore(resolver, store, rootID, "", "")
				_ = store.ConversationForAGYWithOptions(lin, true)
				srv, err := NewServer(ServerOptions{Store: store, Cluster: cluster})
				if err == nil {
					defer srv.Close()
					srv.mu.Lock()
					srv.lastRootConvID = rootID
					srv.mu.Unlock()
					if act == "refresh" {
						_, _ = srv.ensureConnected(ctx)
					}
					callCtx := withCallMeta(ctx, rpcCallToolMeta{ConversationID: rootID})
					srv.syncSwarm(callCtx)
				}
			}
		}
		return writeResp(map[string]any{"ok": true, "action": act})
	}
}

func (s *Server) cleanupDeletedSessionSubagent(
	ctx context.Context,
	subagentID string,
	targetAlias string,
	artifactsDirHint string,
) (ConversationLineage, bool) {
	if s == nil {
		return ConversationLineage{}, false
	}
	brainDir := ""
	if s.lineage != nil {
		brainDir = s.lineage.BrainDir(artifactsDirHint)
	}
	rootID := s.activeLiveRootCoordinatorID(brainDir)
	subID := strings.TrimSpace(subagentID)
	if subID != "" && subID == rootID {
		subID = ""
	}

	var matchedLin ConversationLineage
	removedAny := false

	remover, _ := s.lineage.(interface {
		RemoveSubagent(brainDir, subagentID string) bool
		RemoveSubagentByTarget(brainDir, target string) (ConversationLineage, bool)
	})
	if remover != nil {
		if subID != "" {
			if remover.RemoveSubagent(brainDir, subID) {
				removedAny = true
			}
		}
		if strings.TrimSpace(targetAlias) != "" {
			if lin, ok := remover.RemoveSubagentByTarget(brainDir, targetAlias); ok {
				matchedLin = lin
				removedAny = true
				if subID == "" && lin.ConversationID != "" && lin.ConversationID != rootID {
					subID = lin.ConversationID
				}
			}
		}
	} else if brainDir != "" && subID != "" {
		if removeSubagentDescriptorsOnDisk(brainDir, subID) {
			removedAny = true
		}
	}

	if subID != "" {
		s.mu.Lock()
		delete(s.adoptedSubagents, subID)
		if cancel, ok := s.restampCancels[subID]; ok {
			cancel()
			delete(s.restampCancels, subID)
		}
		lsURL := s.lsBaseURL
		s.mu.Unlock()

		delCtx, cancel := context.WithTimeout(ctx, lsHTTPTimeout)
		_ = DeleteAGYCascadeTrajectory(delCtx, lsURL, subID)
		cancel()
	}
	return matchedLin, removedAny
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
